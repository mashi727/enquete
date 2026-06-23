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
from enquete.ocr.base import OcrBackend, OcrLine
from enquete.schema import FREE_TEXT, MULTI_CHOICE, SINGLE_CHOICE, Rect, Survey


def _group_rows(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """OCR断片を「視覚的な行」へまとめる(縦の重なりで同一行と判定)。

    OCR は大きな字間(メールの @ 前後など)で1行を複数断片に分けて返すことがある。
    上端Yだけで並べると、同じ行でもベースラインの揺れで前後が入れ替わり改行されて
    しまうため、縦方向の重なりが小さい方の高さの 50% を超える断片を同一行に束ねる。
    """
    rows: list[list[OcrLine]] = []
    for ln in sorted(lines, key=lambda x: x.bbox[1]):  # 上→下
        y0, y1 = ln.bbox[1], ln.bbox[3]
        h = max(1e-6, y1 - y0)
        placed = False
        for row in rows:
            ry0 = min(r.bbox[1] for r in row)
            ry1 = max(r.bbox[3] for r in row)
            overlap = min(y1, ry1) - max(y0, ry0)
            if overlap > 0.5 * min(h, ry1 - ry0):
                row.append(ln)
                placed = True
                break
        if not placed:
            rows.append([ln])
    return rows


def _ocr_region(
    image: QImage,
    region: Rect,
    backend: OcrBackend,
    languages: Sequence[str],
    single_line: bool = False,
) -> str:
    """ページ画像の正規化矩形 region を切り出してOCRし、読み順に連結する。

    OCR が返す断片を視覚的な行へまとめ、行内は左→右で連結、行間は改行で連結する。
    single_line=True(単一行欄)なら、全断片を左→右で1行に連結する(メール欄など、
    @ 前後の字間で2段に割れるのを防ぐ)。
    """
    w, h = image.width(), image.height()
    x0, y0, x1, y1 = region
    rx, ry = max(0, int(x0 * w)), max(0, int(y0 * h))
    rw, rh = min(w - rx, int((x1 - x0) * w)), min(h - ry, int((y1 - y0) * h))
    if rw <= 0 or rh <= 0:
        return ""
    crop = image.copy(rx, ry, rw, rh)
    lines = backend.recognize_lines(_png_bytes(crop), languages)
    if not lines:
        return ""
    if single_line:
        ordered = sorted(lines, key=lambda ln: ln.bbox[0])  # 左→右
        return " ".join(ln.text.strip() for ln in ordered if ln.text.strip()).strip()
    # 複数行: 行をまとめ、行内は左→右、行間は改行
    out_rows: list[str] = []
    for row in _group_rows(lines):
        row_sorted = sorted(row, key=lambda ln: ln.bbox[0])
        text = " ".join(ln.text.strip() for ln in row_sorted if ln.text.strip())
        if text:
            out_rows.append(text)
    return "\n".join(out_rows).strip()


def recognize_region(
    image: QImage,
    region: Rect,
    backend: OcrBackend,
    languages: Sequence[str] = ("ja-JP",),
    single_line: bool = False,
) -> str:
    """1領域だけをOCRしてテキストを返す(自由記述欄の再認識などに使用)。"""
    return _ocr_region(image, region, backend, languages, single_line=single_line)


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
                out[q.id] = _ocr_region(
                    image, q.region, backend, languages,
                    single_line=not q.multiline,
                )
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
