"""ChromeScreenAI (clv-locro) による OCR バックエンド。Windows/Linux/macOS 対応。

Chrome の screen-ai エンジンを ctypes 経由で呼ぶ clv-locro(MIT, ソース配布) の
ラッパ。エンジン本体(chrome_screen_ai.dll / libchromescreenai.so)とモデルは
**非再配布**のため同梱できず、利用者が事前に `locro download` で手元の Chrome から
コピーしておく必要がある(docs/ocr_windows.md 参照)。

locro の bounding_box は ピクセル・左上原点(x, y, width, height)。本アプリの
正規化(0..1)・左上原点 Rect=(x0,y0,x1,y1) に変換して返す。文字単位の矩形は
提供されない(行/語のみ)ため、box_for_range は語 boxの和集合で近似する。
Apple Vision の文字単位 boundingBoxForRange より粗いが、番号マーカーの ROI
抽出には実用上十分(最終的に人手で校正する前提)。
"""
from __future__ import annotations

import io
from collections.abc import Sequence

from enquete.ocr.base import OcrLine
from enquete.schema import Rect


def _norm_box(bb, w: float, h: float) -> Rect | None:
    """locro の BoundingBox(px, 左上原点) を 正規化左上原点 Rect に変換。"""
    if bb is None or w <= 0 or h <= 0:
        return None
    x0 = bb.x / w
    y0 = bb.y / h
    x1 = (bb.x + bb.width) / w
    y1 = (bb.y + bb.height) / h
    return (x0, y0, x1, y1)


def _build_resolver(text: str, words: Sequence[object], w: float, h: float):
    """行内の語 box から、任意の文字範囲 [start,start+length) の矩形を解決する。

    locro は文字単位 box を持たないため、行テキスト中で各語の文字スパンを推定し、
    要求範囲と重なる語の box を和集合して近似 Rect を返す。
    """
    spans: list[tuple[int, int, object]] = []
    cursor = 0
    for word in words:
        wt = getattr(word, "text", "") or ""
        bb = getattr(word, "bounding_box", None)
        idx = text.find(wt, cursor) if wt else -1
        if idx < 0:
            idx = cursor  # 不一致時は現在位置から並べる(順序は保たれる前提)
        start, end = idx, idx + len(wt)
        cursor = end
        spans.append((start, end, bb))

    def resolve(start: int, length: int) -> Rect | None:
        end = start + length
        boxes = [
            bb
            for (ws, we, bb) in spans
            if bb is not None and ws < end and we > start
        ]
        if not boxes:
            return None
        x0 = min(b.x for b in boxes) / w
        y0 = min(b.y for b in boxes) / h
        x1 = max(b.x + b.width for b in boxes) / w
        y1 = max(b.y + b.height for b in boxes) / h
        return (x0, y0, x1, y1)

    return resolve


def _lines_from_page(page: object, w: float, h: float) -> list[OcrLine]:
    """locro の OcrPage(blocks→lines→words) を本アプリの OcrLine 列に変換。

    純関数。テスト時はダミー page を渡して座標変換/範囲解決を検証できる。
    """
    out: list[OcrLine] = []
    for block in getattr(page, "blocks", []) or []:
        for line in getattr(block, "lines", []) or []:
            text = getattr(line, "text", "") or ""
            words = list(getattr(line, "words", []) or [])
            bbox = _norm_box(getattr(line, "bounding_box", None), w, h)
            if bbox is None:
                continue
            confs = [
                c
                for c in (getattr(wd, "confidence", None) for wd in words)
                if c is not None
            ]
            conf = float(sum(confs) / len(confs)) if confs else 1.0
            out.append(
                OcrLine(
                    text=text,
                    bbox=bbox,
                    confidence=conf,
                    range_resolver=_build_resolver(text, words, w, h),
                )
            )
    return out


# screen-ai のネイティブライブラリは 1 プロセスにつき 1 度しか初期化できない
# (複数回 ScreenAI() を作ると abort する)。プロセス内シングルトンで共有する。
_ENGINE_TRIED = False
_ENGINE: object | None = None


def _get_engine() -> object | None:
    """ScreenAI をプロセス内で一度だけ遅延生成して共有する。失敗時 None。"""
    global _ENGINE_TRIED, _ENGINE
    if _ENGINE_TRIED:
        return _ENGINE
    _ENGINE_TRIED = True
    try:
        from locro import ScreenAI  # type: ignore

        _ENGINE = ScreenAI()
    except Exception:
        # locro 未導入、またはモデル未取得(download_component)など。
        _ENGINE = None
    return _ENGINE


def setup_component(target_dir: "object | None" = None) -> str:
    """screen-ai コンポーネント(DLL＋モデル)を取得してローカルに配置する。

    locro の download_component() を呼ぶ。取得元は自動フォールバック
    (インストール済み Chrome → Dropbox zip → Google サーバの順)。
    取得先(バージョンディレクトリ)のパス文字列を返す。

    取得に成功したら、まだエンジン未生成ならエンジン構築フラグをリセットし、
    次回 available()/初回OCR で新しいコンポーネントを使えるようにする。
    locro 未導入(同梱されていない)・取得失敗時は例外を送出する。
    """
    from locro import download_component  # type: ignore

    path = download_component(target_dir)
    global _ENGINE_TRIED
    if _ENGINE is None:
        _ENGINE_TRIED = False  # 取得後に再構築できるよう未試行へ戻す
    return str(path)


class ScreenAiBackend:
    name = "Chrome screen-ai (locro)"

    def _ensure_engine(self) -> object | None:
        return _get_engine()

    def available(self) -> bool:
        """利用可否を軽量判定する(50MBのライブラリはロードしない)。

        既に構築済みなら True。未構築なら locro が import 可能で、かつ
        screen-ai コンポーネント(Chrome 由来 or `locro download` 済み)が
        ファイルシステム上に存在するかだけを確認する。実ロードは初回OCR時。
        """
        if _ENGINE is not None:
            return True
        import importlib.util

        if importlib.util.find_spec("locro") is None:
            return False
        try:
            from locro._dll import find_screen_ai_dir  # type: ignore

            find_screen_ai_dir()  # 見つからなければ例外
            return True
        except Exception:
            return False

    def recognize_lines(
        self, image_data: bytes, languages: Sequence[str] = ("ja-JP",)
    ) -> list[OcrLine]:
        # languages は screen-ai では未使用(言語自動判定)。
        engine = self._ensure_engine()
        if engine is None:
            return []
        from PIL import Image

        img = Image.open(io.BytesIO(image_data))
        img.load()
        w, h = float(img.width), float(img.height)
        page = engine.ocr_pil_image(img)  # OcrPage を想定
        return _lines_from_page(page, w, h)
