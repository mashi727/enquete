"""スキャン/写真PDFから OCR でフォーム定義(Survey)を生成する。

テキスト層を持たない記入済みスキャンが対象。OCRで各ページの行を取り、
番号マーカー(（1）（2）… / ①②③…)を検出して個別ROI(box_for_range)を得る。
記入による隠れ・スキャンのばらつきに対し、複数ページ・複数PDFの結果を
位置でクラスタリングして統合し、頑健なマーカー位置を割り出す。

設問構造は番号見出し・【…】グループ・「複数(回答)可」のヒューリスティック
(formgen と同様)で組み立てる。マーカー種別は number_mark。
出力はベクトル版と同じ form-only Survey。座標粗さは後段のROIオーバーレイ＋
校正で詰める前提。
"""
from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice

from enquete.formgen import _WRITE_KW, _clean, _freetext_label
from enquete.ocr.base import OcrBackend, OcrLine
from enquete.pdf import PdfDocument
from enquete.schema import (
    FREE_TEXT,
    MARKER_CIRCLE_ITEM,
    MARKER_NUMBER,
    MULTI_CHOICE,
    SINGLE_CHOICE,
    CheckboxDetection,
    Option,
    Question,
    Rect,
    Survey,
)

RENDER_SCALE = 2.5  # OCR用ラスタ倍率(対象スキャンのネイティブ≈2.78px/pt 相当)
MAX_PAGES = 8  # フォーム作成にサンプリングする最大ページ数
# クラスタ統合の許容差(正規化座標)
_TOL_X = 0.030
_TOL_Y = 0.014

# マーカー: （1）(1) … / ①〜⑳
_PAREN = r"[（(][0-9０-９]{1,2}[）)]"
_CIRCLED = r"[①-⑳]"
_MARKER = re.compile(rf"{_PAREN}|{_CIRCLED}")
_PAREN_RE = re.compile(_PAREN)
_GROUP = re.compile(r"【(.+?)】")
# 見出し: 行頭の素の番号(括弧なし) + テキスト
_HEADER = re.compile(r"^([0-9０-９]{1,2})[\s．.、]*([^\s0-9０-９].*)$")
_SUBHEADER = re.compile(r"^[○〇]\s*(.+?)\s*[：:]\s*(.*)$")
# 自由記述の小見出し: （ご感想等）のような短い丸括弧プロンプト(指示文は除外)
_FREETEXT = re.compile(r"^[（(](.{1,14})[）)]$")
_INSTRUCTION = re.compile(r"ください|下さい|記入|複数|印を|お付け|どなた")


@dataclass
class _Item:
    kind: str  # 'header' | 'group' | 'option' | 'freetext'
    text: str
    roi: Rect  # option=マーカー矩形 / header,group,freetext=行矩形
    multi: bool = False  # header のみ: 複数回答可か
    marker_type: str = MARKER_NUMBER  # option のみ
    no: str | None = None  # freetext/header の番号(あれば)


@dataclass
class _Cluster:
    kind: str
    rois: list[Rect] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    marker_types: list[str] = field(default_factory=list)
    nos: list[str] = field(default_factory=list)
    multi_votes: int = 0
    count: int = 0

    def best_marker_type(self) -> str:
        if not self.marker_types:
            return MARKER_NUMBER
        return max(set(self.marker_types), key=self.marker_types.count)

    def best_no(self) -> str | None:
        if not self.nos:
            return None
        return max(set(self.nos), key=self.nos.count)

    def center(self) -> tuple[float, float]:
        x = statistics.fmean((r[0] + r[2]) / 2 for r in self.rois)
        y = statistics.fmean((r[1] + r[3]) / 2 for r in self.rois)
        return x, y

    def median_roi(self) -> Rect:
        xs0 = statistics.median(r[0] for r in self.rois)
        ys0 = statistics.median(r[1] for r in self.rois)
        xs1 = statistics.median(r[2] for r in self.rois)
        ys1 = statistics.median(r[3] for r in self.rois)
        return (xs0, ys0, xs1, ys1)

    def best_text(self) -> str:
        # 最頻のテキスト(同数なら最長)
        counts: dict[str, int] = {}
        for t in self.texts:
            counts[t] = counts.get(t, 0) + 1
        return max(counts, key=lambda t: (counts[t], len(t)))


def _png_bytes(qimage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    qimage.save(buf, "PNG")
    buf.close()
    return ba.data()


def _parse_line(line: OcrLine) -> list[_Item]:
    """1行を header / group / option(s) に分類して返す。"""
    text = line.text.strip()
    if not text:
        return []

    gm = _GROUP.search(text)
    if gm and not _MARKER.match(text):
        return [_Item("group", _clean(gm.group(1)), line.bbox)]

    # オプション行: 行頭がマーカー
    if _MARKER.match(text):
        items: list[_Item] = []
        marks = list(_MARKER.finditer(text))
        for i, m in enumerate(marks):
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            label = _clean(text[start:end])
            if not label:
                continue
            roi = line.box_for_range(m.start(), m.end() - m.start())
            if roi is None:
                roi = _interp_box(line.bbox, m.start(), m.end() - m.start(), len(text))
            # パレン式は自動検出(number_mark)、丸数字式は手動(circle_item)
            mtype = MARKER_NUMBER if _PAREN_RE.fullmatch(m.group()) else MARKER_CIRCLE_ITEM
            items.append(_Item("option", label, roi, marker_type=mtype))
        return items

    # 自由記述プロンプト(見出し判定より前): 記入キーワード、または短い丸括弧。
    # 番号見出し由来なら番号を保持する。
    is_paren = _FREETEXT.match(text)
    if (is_paren and not _INSTRUCTION.search(text)) or _WRITE_KW.search(text):
        hm = _HEADER.match(text)
        if hm:
            return [_Item("freetext", hm.group(2), line.bbox, no=hm.group(1))]
        inner = is_paren.group(1) if is_paren else text
        return [_Item("freetext", inner, line.bbox)]

    sm = _SUBHEADER.match(text)
    if sm:
        return [_Item("header", _clean(sm.group(1)), line.bbox, multi="複数" in text)]

    hm = _HEADER.match(text)
    if hm:
        label = _clean(re.sub(r"（複数.*?可）|\(複数.*?可\)", "", hm.group(2)))
        return [_Item("header", label, line.bbox, multi="複数" in text)]

    return []


def _interp_box(line_box: Rect, start: int, length: int, total: int) -> Rect:
    """box_for_range が取れない場合の近似(行矩形を文字位置で線形補間)。"""
    x0, y0, x1, y1 = line_box
    w = x1 - x0
    a = x0 + w * (start / max(1, total))
    b = x0 + w * ((start + length) / max(1, total))
    return (a, y0, b, y1)


_ROW_TOL = 0.008  # 同一行とみなす中心yの許容差(正規化)


def _reading_order(clusters: list[_Cluster]) -> list[_Cluster]:
    """クラスタを読み順(上→下、同一行内は左→右)に並べ替える。

    OCR座標のばらつきで同一行のyが微妙に異なるため、近いyを1行に束ねてから
    行内をxで整列する(yのみのソートだと同一行が前後する問題を回避)。
    """
    rows: list[list[_Cluster]] = []
    ref_y: float | None = None
    for c in sorted(clusters, key=lambda c: c.center()[1]):
        y = c.center()[1]
        if ref_y is None or abs(y - ref_y) > _ROW_TOL:
            rows.append([c])
            ref_y = y
        else:
            rows[-1].append(c)
    ordered: list[_Cluster] = []
    for row in rows:
        row.sort(key=lambda c: c.center()[0])
        ordered.extend(row)
    return ordered


def _cluster_items(all_items: Sequence[_Item]) -> list[_Cluster]:
    clusters: list[_Cluster] = []
    for it in all_items:
        cx = (it.roi[0] + it.roi[2]) / 2
        cy = (it.roi[1] + it.roi[3]) / 2
        best = None
        best_d = 1e9
        for c in clusters:
            if c.kind != it.kind:
                continue
            ccx, ccy = c.center()
            if abs(cx - ccx) <= _TOL_X and abs(cy - ccy) <= _TOL_Y:
                d = abs(cx - ccx) + abs(cy - ccy)
                if d < best_d:
                    best, best_d = c, d
        if best is None:
            best = _Cluster(kind=it.kind)
            clusters.append(best)
        best.rois.append(it.roi)
        best.texts.append(it.text)
        if it.kind == "option":
            best.marker_types.append(it.marker_type)
        if it.no is not None:
            best.nos.append(it.no)
        best.count += 1
        if it.multi:
            best.multi_votes += 1
    return clusters


def generate_survey_from_scans(
    pdf_paths: Sequence[str | Path],
    backend: OcrBackend,
    *,
    max_pages: int = MAX_PAGES,
    render_scale: float = RENDER_SCALE,
    languages: Sequence[str] = ("ja-JP",),
) -> Survey:
    """複数のスキャンPDFから number_mark 方式のフォームを生成する。"""
    # サンプリング対象ページ(全PDFのページを集めて均等抽出)
    page_refs: list[tuple[PdfDocument, int]] = []
    docs = [PdfDocument(p) for p in pdf_paths]
    for doc in docs:
        for i in range(len(doc)):
            page_refs.append((doc, i))
    if len(page_refs) > max_pages:
        step = len(page_refs) / max_pages
        page_refs = [page_refs[int(k * step)] for k in range(max_pages)]

    all_items: list[_Item] = []
    n_pages = 0
    for doc, idx in page_refs:
        image = doc.render(idx, scale=render_scale)
        lines = backend.recognize_lines(_png_bytes(image), languages)
        for ln in lines:
            all_items.extend(_parse_line(ln))
        n_pages += 1

    clusters = _cluster_items(all_items)
    # 支持数の少ない(散発的=ノイズの)クラスタを除外
    min_support = max(1, round(n_pages * 0.34))
    clusters = [c for c in clusters if c.count >= min_support]
    clusters = _reading_order(clusters)  # 行スナップした読み順(上→下, 行内は左→右)

    questions: list[Question] = []
    cur: dict | None = None
    cur_group: str | None = None
    seq = 0

    def flush() -> None:
        nonlocal cur
        if cur and cur["options"]:
            questions.append(
                Question(
                    id=cur["id"],
                    label=cur["label"],
                    type=cur["type"],
                    no=cur["no"],
                    options=cur["options"],
                )
            )
        cur = None

    for i, c in enumerate(clusters):
        if c.kind == "group":
            cur_group = c.best_text()
        elif c.kind == "header":
            flush()
            cur_group = None
            seq += 1
            cur = {
                "id": f"q{seq}",
                "no": str(seq),
                "label": c.best_text(),
                "type": MULTI_CHOICE if c.multi_votes else SINGLE_CHOICE,
                "options": [],
            }
        elif c.kind == "freetext":
            # 直後が選択肢クラスタなら区切り見出し(section intro)とみなし除外
            nxt_c = clusters[i + 1] if i + 1 < len(clusters) else None
            if nxt_c is not None and nxt_c.kind == "option" and (
                nxt_c.center()[1] - c.center()[1]
            ) <= _TOL_Y * 3:
                continue
            # 自由記述: プロンプト下端〜次要素上端 を記入領域として概算
            flush()
            seq += 1
            top = c.median_roi()[3]
            nxt = clusters[i + 1].center()[1] if i + 1 < len(clusters) else None
            bottom = nxt if (nxt is not None and nxt > top) else min(1.0, top + 0.10)
            questions.append(
                Question(
                    id=f"q{seq}",
                    label=_freetext_label(c.best_text()),
                    type=FREE_TEXT,
                    no=c.best_no(),
                    region=(0.09, top, 0.93, min(1.0, bottom)),
                    multiline=True,
                )
            )
        elif c.kind == "option":
            if cur is None:
                seq += 1
                cur = {
                    "id": f"q{seq}",
                    "no": str(seq),
                    "label": "",
                    "type": SINGLE_CHOICE,
                    "options": [],
                }
            cur["options"].append(
                Option(
                    value=c.best_text(),
                    checkbox=c.median_roi(),
                    group=cur_group,
                    marker_type=c.best_marker_type(),
                )
            )
    flush()

    title = Path(pdf_paths[0]).stem if pdf_paths else "survey"
    return Survey(
        survey_id=_slug(title),
        title=title,
        questions=questions,
        detection=CheckboxDetection(render_scale=render_scale),
        source_pdf=Path(pdf_paths[0]).name if pdf_paths else None,
        page=0,
    )


def _slug(stem: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", stem).strip("_").lower()
    return s or "survey"
