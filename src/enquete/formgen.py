"""クリーン(ベクトル)PDFからフォーム定義(Survey)を自動生成する。

テキスト層を持つPDFを前提に、□(U+25A1)グリフの位置とラベル、設問見出しを
抽出し、PDFの読み順・構造をなぞった Survey を構築する。

ヒューリスティクス:
- 設問見出し: 行頭の番号(１． / 2. / ○ / 〇)で設問を区切る
- グループ小見出し: 【…】 をグループ名にする
- 単一/複数: 見出しに「複数回答可」があれば multi、無ければ single
- 選択肢(2方式):
  - □式: □ の直後〜次の□までをラベルに、□グリフ矩形を正規化ROIに(marker=box)
  - 番号〇囲み式: 行内の「N. ラベル」列挙を選択肢に、番号グリフ矩形をROIに
    (marker=circle_item)。見出し行(列挙が1個以下)と選択肢行(列挙が2個以上)を
    区別する。○ラベル：… の小見出しは同一行の列挙を選択肢にする。

スキャン画像PDF(テキスト層なし)はこの方法では扱えない(OCR工程で別途対応)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pypdfium2 as pdfium

from enquete.schema import (
    FREE_TEXT,
    MARKER_CIRCLE_ITEM,
    MULTI_CHOICE,
    SINGLE_CHOICE,
    CheckboxDetection,
    Option,
    Question,
    Rect,
    Survey,
)

_CHECKBOX = "□"
_LINE_TOL = 6.0  # 同一行とみなす垂直方向の許容差(pt)
# 行頭の番号見出し(全角/半角の数字＋句点、または ○/〇)
_NUM_HEADER = re.compile(r"^\s*([0-9０-９]+)\s*[．.、]\s*(.*)$")
_SUB_HEADER = re.compile(r"^\s*[○〇]\s*(.+?)\s*[：:]\s*(.*)$")
_GROUP = re.compile(r"【(.+?)】")
# 自由記述: 記入を促すキーワード、または短い丸括弧プロンプト
_WRITE_KW = re.compile(
    r"お書き|ご記入|記入くださ|ご自由|コメント|ご感想|お名前|氏名|メールアドレス|ご住所|フリガナ"
)
_FREETEXT_PAREN = re.compile(r"^[（(](.{1,14})[）)]$")


def has_text_layer(pdf_path: str | Path) -> bool:
    """PDF にテキスト層があるか(=ベクトルでフォーム抽出可能か)を判定する。"""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        tp = doc[0].get_textpage()
        return tp.count_chars() > 0
    finally:
        doc.close()


def _extract_lines(textpage) -> list[list[tuple]]:
    """文字を視覚的な行にまとめて返す。各要素は (ch, l, b, r, t)。"""
    n = textpage.count_chars()
    items = []
    for i in range(n):
        ch = textpage.get_text_range(i, 1)
        if not ch or ch in "\r\n\t":
            continue
        l, b, r, t = textpage.get_charbox(i)
        items.append((ch, l, b, r, t, (b + t) / 2.0))
    items.sort(key=lambda e: -e[5])  # 上(yが大)から下へ
    lines: list[list[tuple]] = []
    cur: list[tuple] = []
    ref_y: float | None = None
    for e in items:
        cy = e[5]
        if ref_y is None or abs(cy - ref_y) <= _LINE_TOL:
            cur.append(e)
            ref_y = cy if ref_y is None else ref_y
        else:
            lines.append(cur)
            cur = [e]
            ref_y = cy
    if cur:
        lines.append(cur)
    for ln in lines:
        ln.sort(key=lambda e: e[1])  # 行内は左から右へ
    return lines


def _line_text(line: list[tuple]) -> str:
    return "".join(e[0] for e in line)


def _clean(s: str) -> str:
    """ラベル整形: CJK等(非ASCII)に隣接する空白を除去する。

    PDFのカーニングで語中に空白が入ることがある(例 '第 5 番'・'公式 LINE')。
    片側が非ASCIIなら空白を削除し、両側ともASCIIの空白(英単語間)だけ残す。
    """
    s = s.strip()
    return re.sub(r"(?<=[^\x00-\x7F])\s+|\s+(?=[^\x00-\x7F])", "", s)


def _line_box(line: list[tuple], w: float, h: float) -> Rect:
    """行の外接矩形を正規化(左上原点)で返す。"""
    ls = min(e[1] for e in line)
    bs = min(e[2] for e in line)
    rs = max(e[3] for e in line)
    ts = max(e[4] for e in line)
    return (ls / w, (h - ts) / h, rs / w, (h - bs) / h)


def _is_structured(text: str) -> bool:
    """選択肢/見出しを含む構造行か(自由記述プロンプトの直後判定に使う)。"""
    return (
        _CHECKBOX in text
        or bool(_NUM_HEADER.match(text))
        or bool(_SUB_HEADER.match(text))
        or bool(_GROUP.search(text))
    )


def _next_nonempty_text(lines: list[list[tuple]], i: int) -> str | None:
    for j in range(i + 1, len(lines)):
        t = _line_text(lines[j]).strip()
        if t:
            return t
    return None


def _next_structured_box(
    lines: list[list[tuple]], i: int, w: float, h: float
) -> Rect | None:
    """i より後の最初の構造行(□/見出し)の矩形。記入領域の下端推定に使う。"""
    for j in range(i + 1, len(lines)):
        t = _line_text(lines[j]).strip()
        if t and _is_structured(t):
            return _line_box(lines[j], w, h)
    return None


_FREETEXT_LABEL_LIMIT = 28


def _freetext_label(text: str) -> str:
    """自由記述ラベルを整形(◆※・複数回答可注記を除去)。長い場合は…で省略。"""
    s = text.replace("◆", "").replace("※", "").strip()
    s = re.sub(r"（複数.*?可）|\(複数.*?可\)", "", s)
    s = _clean(s)
    if not s:
        return "自由記述"
    if len(s) > _FREETEXT_LABEL_LIMIT:
        return s[:_FREETEXT_LABEL_LIMIT].rstrip() + "…"
    return s


def generate_survey_from_pdf(
    pdf_path: str | Path, survey_id: str | None = None, title: str | None = None
) -> Survey:
    """ベクトルPDFからフォーム定義を生成する。"""
    pdf_path = Path(pdf_path)
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[0]
        w, h = page.get_size()
        tp = page.get_textpage()
        lines = _extract_lines(tp)
    finally:
        doc.close()

    questions: list[Question] = []
    seq = 0
    cur_q: dict | None = None  # {no,label,type,options}
    cur_group: str | None = None
    first_text = ""

    def flush() -> None:
        nonlocal cur_q
        if cur_q and cur_q["options"]:
            questions.append(
                Question(
                    id=cur_q["id"],
                    label=cur_q["label"],
                    type=cur_q["type"],
                    no=cur_q["no"],
                    options=cur_q["options"],
                )
            )
            # 選択肢の空欄括弧(例「その他（  ）」)は自由入力欄として別途生成する。
            for k, (base, region) in enumerate(cur_q.get("fills", []), 1):
                questions.append(
                    Question(
                        id=f"{cur_q['id']}_fill{k}",
                        label=f"{base}（自由記述）",
                        type=FREE_TEXT,
                        no=None,
                        region=region,
                        multiline=False,
                    )
                )
        cur_q = None

    for i, line in enumerate(lines):
        text = _line_text(line).strip()
        if not text:
            continue
        if not first_text:
            first_text = text

        # 自由記述プロンプト: □を含まず、記入キーワード/短い丸括弧で、かつ
        # 直後が構造行(□・見出し)でない行。記入領域を下方に概算する。
        if _CHECKBOX not in text and (
            _FREETEXT_PAREN.match(text) or _WRITE_KW.search(text)
        ):
            nxt = _next_nonempty_text(lines, i)
            if nxt is None or not _is_structured(nxt):
                flush()
                cur_group = None
                seq += 1
                box = _line_box(line, w, h)
                top = box[3]
                nbox = _next_structured_box(lines, i, w, h)
                bottom = nbox[1] if (nbox and nbox[1] > top) else min(1.0, top + 0.10)
                # 番号見出しなら番号を残す(no)、ラベルは番号以降を整形
                m_ft = _NUM_HEADER.match(text)
                ft_no = m_ft.group(1) if m_ft else None
                raw = m_ft.group(2) if m_ft else text
                questions.append(
                    Question(
                        id=f"q{seq}",
                        label=_freetext_label(raw),
                        type=FREE_TEXT,
                        no=ft_no,
                        region=(0.09, top, 0.93, min(1.0, bottom)),
                        multiline=True,
                    )
                )
                continue

        items = _enumerate_options(line, w, h)
        m_num = _NUM_HEADER.match(text)
        m_sub = _SUB_HEADER.match(text)
        qtype = MULTI_CHOICE if "複数回答可" in text else SINGLE_CHOICE

        if m_sub:
            # ○ラベル：選択肢… (同一行に番号〇囲み式の選択肢が並ぶ)
            flush()
            cur_group = None
            seq += 1
            cur_q = {
                "id": f"q{seq}", "no": None,
                "label": _clean(m_sub.group(1)), "type": qtype, "options": [],
            }
            _add_circle_options(cur_q, items, None)
        elif m_num and len(items) <= 1:
            # 設問見出し(行頭番号＋プロンプト)。番号〇囲み式の選択肢は後続行に並ぶ。
            flush()
            cur_group = None
            seq += 1
            label = _clean(re.sub(r"（複数回答可）|\(複数回答可\)", "", m_num.group(2)))
            cur_q = {
                "id": f"q{seq}", "no": m_num.group(1),
                "label": label, "type": qtype, "options": [],
            }
        elif cur_q is not None and items:
            # 番号〇囲み式の選択肢行 → 現在の設問へ追加
            _add_circle_options(cur_q, items, cur_group)

        # グループ小見出し
        for gm in _GROUP.finditer(text):
            cur_group = _clean(gm.group(1))

        # この行の □ を現在の設問の選択肢として収集(□式PDF)
        _collect_options(line, w, h, cur_q, cur_group)

    flush()

    return Survey(
        survey_id=survey_id or _slug(pdf_path.stem),
        title=title or _clean(first_text) or pdf_path.stem,
        questions=questions,
        detection=CheckboxDetection(),
        source_pdf=pdf_path.name,
        page=0,
    )


def _is_dig(ch: str) -> bool:
    return ch.isdigit() or ("０" <= ch <= "９")


_ENUM_SEPS = "．.、"


def _enumerate_options(
    line: list[tuple], w: float, h: float
) -> list[tuple[str, str, Rect]]:
    """行内の「N. ラベル」列挙を (番号, ラベル, 番号の正規化ROI) のリストで返す。

    番号は「数字の連続＋区切り(．.、)」。ラベルは区切りの次から次の番号開始まで。
    ラベル中の区切りを伴わない数字(例「第5番」)は番号と見なさない。
    """
    starts: list[tuple[int, int, str]] = []  # (num_start, sep_idx, num_str)
    i, n = 0, len(line)
    while i < n:
        if _is_dig(line[i][0]):
            j = i
            while j < n and _is_dig(line[j][0]):
                j += 1
            # 数字と区切りの間に空白が入る場合がある(例 "1 .大変良かった")
            k = j
            while k < n and line[k][0] in " 　":
                k += 1
            if k < n and line[k][0] in _ENUM_SEPS:
                num = "".join(line[m][0] for m in range(i, j))
                starts.append((i, k, num))
                i = k + 1
                continue
        i += 1

    out: list[tuple[str, str, Rect]] = []
    for idx, (s, sep, num) in enumerate(starts):
        label_start = sep + 1
        label_end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        raw = "".join(line[k][0] for k in range(label_start, label_end))
        label = _clean(raw.lstrip("．.、 　"))
        ls = min(line[k][1] for k in range(s, sep))
        bs = min(line[k][2] for k in range(s, sep))
        rs = max(line[k][3] for k in range(s, sep))
        ts = max(line[k][4] for k in range(s, sep))
        box = (ls / w, (h - ts) / h, rs / w, (h - bs) / h)
        out.append((num, label, box))
    return out


def _add_circle_options(
    cur_q: dict | None, items: list[tuple[str, str, Rect]], group: str | None
) -> None:
    """番号〇囲み式の列挙を現在の設問へ circle_item 選択肢として追加する。"""
    if cur_q is None:
        return
    for _num, label, box in items:
        if label:
            cur_q["options"].append(
                Option(
                    value=label,
                    checkbox=box,
                    group=group,
                    marker_type=MARKER_CIRCLE_ITEM,
                )
            )


_BLANK_PAREN = re.compile(r"[（(][\s　]*[）)]\s*$")


def _fillin_region(label_items: list[tuple], w: float, h: float) -> Rect | None:
    """選択肢ラベル中の「（…）」の中身が空白のみなら、内側の正規化Rectを返す。

    例「その他（　　）」は記入欄。一方「X(旧 Twitter)」のように中身に文字が
    ある括弧はラベルなので None を返す(自由入力欄ではない)。
    """
    op = cl = None
    for idx, e in enumerate(label_items):
        if e[0] in "（(" and op is None:
            op = idx
        elif e[0] in "）)" and op is not None:
            cl = idx
            break
    if op is None or cl is None or cl <= op:
        return None
    inner = label_items[op + 1 : cl]
    if any(e[0] not in " 　" for e in inner):
        return None  # 括弧内に文字がある → 記入欄ではない
    left = label_items[op][3]   # （ の右端
    right = label_items[cl][1]  # ） の左端
    if right - left < 4.0:      # 記入の余地がない狭い括弧は無視
        return None
    bottom = min(label_items[op][2], label_items[cl][2])
    top = max(label_items[op][4], label_items[cl][4])
    # 手書きの上下はみ出しを拾えるよう縦に余裕を持たせる
    pad = 0.4 * (top - bottom)
    top, bottom = top + pad, bottom - pad
    return (
        left / w,
        max(0.0, (h - top) / h),
        right / w,
        min(1.0, (h - bottom) / h),
    )


def _collect_options(
    line: list[tuple], w: float, h: float, cur_q: dict | None, group: str | None
) -> None:
    if cur_q is None:
        return
    i = 0
    while i < len(line):
        ch, l, b, r, t, _ = line[i]
        if ch == _CHECKBOX:
            # 直後〜次の□までをラベルに
            label_items: list[tuple] = []
            j = i + 1
            while j < len(line) and line[j][0] != _CHECKBOX:
                label_items.append(line[j])
                j += 1
            raw = "".join(e[0] for e in label_items)
            value = _clean(raw)
            if value:
                checkbox = (l / w, (h - t) / h, r / w, (h - b) / h)
                fill = _fillin_region(label_items, w, h)
                if fill is not None:
                    # 「その他（  ）」→ 選択肢「その他」＋ 括弧内を自由入力欄に
                    base = _clean(_BLANK_PAREN.sub("", raw)) or value
                    cur_q["options"].append(
                        Option(value=base, checkbox=checkbox, group=group)
                    )
                    cur_q.setdefault("fills", []).append((base, fill))
                else:
                    cur_q["options"].append(
                        Option(value=value, checkbox=checkbox, group=group)
                    )
            i = j
        else:
            i += 1


def _slug(stem: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", stem).strip("_").lower()
    return s or "survey"
