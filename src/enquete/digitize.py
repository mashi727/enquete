"""アンケート1ページ分の電子化(データ化)。

設問の種類＝領域ごとに最適な方式を使い分け、1ページの全設問を一度に
data 化する:
- box / number_mark / circle_item の選択式 → 画像解析(detect.py)
- free_text(自由記述) → 選択中OCRバックエンドで領域内テキスト読取

戻り値は form_pane.get_results() / サイドカー pages[i].results と同形の
dict[設問id -> 値]。QImage を受け取る純関数なので headless/offscreen で検証可能。
"""
from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtGui import QImage

from enquete.detect import detect_checkboxes, qimage_to_gray
from enquete.formgen_ocr import _png_bytes
from enquete.ocr.base import OcrBackend
from enquete.schema import FREE_TEXT, MULTI_CHOICE, SINGLE_CHOICE, Rect, Survey


def _ocr_region(
    image: QImage, region: Rect, backend: OcrBackend, languages: Sequence[str]
) -> str:
    """ページ画像の正規化矩形 region を切り出してOCRし、行を読み順に連結する。"""
    w, h = image.width(), image.height()
    x0, y0, x1, y1 = region
    rx, ry = max(0, int(x0 * w)), max(0, int(y0 * h))
    rw, rh = min(w - rx, int((x1 - x0) * w)), min(h - ry, int((y1 - y0) * h))
    if rw <= 0 or rh <= 0:
        return ""
    crop = image.copy(rx, ry, rw, rh)
    lines = backend.recognize_lines(_png_bytes(crop), languages)
    # 読み順: 上→下、同程度の高さなら左→右
    ordered = sorted(lines, key=lambda ln: (round(ln.bbox[1], 3), ln.bbox[0]))
    return "\n".join(ln.text for ln in ordered).strip()


def recognize_region(
    image: QImage,
    region: Rect,
    backend: OcrBackend,
    languages: Sequence[str] = ("ja-JP",),
) -> str:
    """1領域だけをOCRしてテキストを返す(自由記述欄の再認識などに使用)。"""
    return _ocr_region(image, region, backend, languages)


def digitize_page(
    image: QImage,
    survey: Survey,
    backend: OcrBackend | None,
    languages: Sequence[str] = ("ja-JP",),
    clean: "object | None" = None,
) -> dict[str, object]:
    """1ページを電子化し、設問id -> 値 の dict を返す。

    backend が None の場合、自由記述はOCRせず空文字を返す(選択式のみ data 化)。
    clean(基準ページのグレースケール)が与えられ、survey.detection.use_diff が
    真なら、選択式は差分検出で判定する。
    """
    gray = qimage_to_gray(image)
    detected = detect_checkboxes(survey, gray, clean)
    out: dict[str, object] = {}
    for q in survey.questions:
        if q.type == SINGLE_CHOICE:
            r = detected.get(q.id)
            out[q.id] = r.selected if r is not None else None
        elif q.type == MULTI_CHOICE:
            r = detected.get(q.id)
            out[q.id] = list(r.checked) if r is not None else []
        elif q.type == FREE_TEXT:
            if backend is not None and q.region is not None:
                out[q.id] = _ocr_region(image, q.region, backend, languages)
            else:
                out[q.id] = ""
    return out


def is_empty_value(value: object) -> bool:
    """設問値が「未入力」か(None / 空文字 / 空リスト)。"""
    return value is None or value == "" or value == []


def merge_results(
    existing: dict[str, object], new: dict[str, object], *, overwrite: bool
) -> dict[str, object]:
    """既存結果に新結果を統合する。

    overwrite=True: 新しい非空の値で上書き(空では消さない)。
    overwrite=False: 既存が空の項目だけ新しい非空値で補完(人手入力を保護)。
    """
    out = dict(existing)
    for qid, val in new.items():
        if is_empty_value(val):
            continue
        if overwrite or is_empty_value(existing.get(qid)):
            out[qid] = val
    return out
