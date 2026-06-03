"""チェックボックスのマーク検出。

レンダリングしたページのグレースケール画像に対し、各 □ の正規化矩形 ROI
内側(枠線を inset で除外)の暗画素比率(=インク量)を測り、閾値以上なら
チェック済みと判定する。OCR には依存しない純粋な画素解析(OS非依存)。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import cv2
import numpy as np
from PySide6.QtGui import QImage

from enquete.schema import (
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


@dataclass
class QuestionResult:
    question_id: str
    type: str
    ratios: dict[str, float] = field(default_factory=dict)  # 選択肢ごとのインク比率
    checked: list[str] = field(default_factory=list)  # 閾値以上(チェック済み)
    selected: str | None = None  # 単一選択での確定値(最大比率)


def qimage_to_gray(image: QImage) -> np.ndarray:
    """QImage を float グレースケール配列(0=黒, 1=白)に変換する。"""
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    w, h, bpl = gray.width(), gray.height(), gray.bytesPerLine()
    buf = bytes(gray.constBits())
    # bytesPerLine は幅より大きい(パディング)場合があるので stride で整形
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl)[:, :w]
    return arr.astype(np.float32) / 255.0


def _ink_ratio(gray: np.ndarray, rect: Rect, inset: float, dark_level: float) -> float:
    """正規化矩形 rect の内側(inset で枠線を除外)の暗画素比率を返す。"""
    h, w = gray.shape
    x0, y0, x1, y1 = rect
    # inset で内側に縮める(枠線を ROI から除外)
    dx = (x1 - x0) * inset
    dy = (y1 - y0) * inset
    px0 = int(round((x0 + dx) * w))
    px1 = int(round((x1 - dx) * w))
    py0 = int(round((y0 + dy) * h))
    py1 = int(round((y1 - dy) * h))
    px0, px1 = max(0, px0), min(w, px1)
    py0, py1 = max(0, py0), min(h, py1)
    if px1 <= px0 or py1 <= py0:
        return 0.0
    roi = gray[py0:py1, px0:px1]
    return float((roi < dark_level).mean())


def _expand_rect(rect: Rect, fx: float, fy: float) -> Rect:
    """矩形を x へ fx×幅 / y へ fy×高さ だけ広げる(0..1 でクランプ)。"""
    x0, y0, x1, y1 = rect
    dx = (x1 - x0) * fx
    dy = (y1 - y0) * fy
    return (
        max(0.0, x0 - dx),
        max(0.0, y0 - dy),
        min(1.0, x1 + dx),
        min(1.0, y1 + dy),
    )


def _circle_radius_ratio(gray: np.ndarray, rect: Rect) -> float:
    """ROI内の最大輪郭の外接円半径 ÷ ROI幅 を返す(OpenCV)。

    ●/〇 のような丸いマークは、印刷番号より大きな外接円を生む。インク量では
    分離できない細い手書き円を、形状(大きさ)で捉えるための指標。
    """
    h, w = gray.shape
    x0, y0, x1, y1 = rect
    px0, py0 = max(0, int(x0 * w)), max(0, int(y0 * h))
    px1, py1 = min(w, int(x1 * w)), min(h, int(y1 * h))
    if px1 <= px0 or py1 <= py0:
        return 0.0
    roi = gray[py0:py1, px0:px1]
    u8 = (roi * 255.0).astype(np.uint8)
    # インク(暗)を白に反転して二値化(Otsu)。膨張はしない(隣接印刷文字を
    # 連結して外接円を過大にしないため)。
    _, binary = cv2.threshold(u8, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return 0.0
    largest = max(contours, key=cv2.contourArea)
    _, radius = cv2.minEnclosingCircle(largest)
    return float(radius) / max(1, roi.shape[1])


def _option_ratio(
    opt: Option, gray: np.ndarray, det: CheckboxDetection
) -> tuple[float, float]:
    """選択肢のマーク指標と判定閾値を、マーカー種別に応じて返す。

    - box: □内側(枠線を inset 除外)の暗画素比率。空欄≈0、閾値=ink_threshold。
    - number_mark: 番号ROIを異方拡張(x広め/y狭め)し、最大輪郭の外接円半径比を
      OpenCVで測る。●/〇 を形状で捉える。閾値=mark_threshold。
    """
    if opt.marker_type == MARKER_CIRCLE_ITEM:
        # 丸数字を〇で囲む方式は自動検出が困難なため手動校正に委ねる
        # (印刷の丸数字自体が円で、形状特徴が効かない)。常に未チェック。
        return 0.0, 1.0
    if opt.marker_type == MARKER_NUMBER:
        roi = _expand_rect(opt.checkbox, det.mark_expand_x, det.mark_expand_y)
        return _circle_radius_ratio(gray, roi), det.mark_threshold
    return _ink_ratio(gray, opt.checkbox, det.roi_inset, det.dark_level), det.ink_threshold


def detect_checkboxes(
    survey: Survey, gray: np.ndarray, clean: np.ndarray | None = None
) -> dict[str, QuestionResult]:
    """全選択式設問についてマーク状態を検出して返す。

    各選択肢の marker_type に応じて検出方法を切り替える。free_text 設問は
    ここでは扱わない(OCR は別工程)。

    clean(クリーン基準ページのグレースケール)が与えられ、かつ
    survey.detection.use_diff が真なら**差分検出**を行う。基準との明度差
    (加筆インク)を「暗さ」に見立てた疑似グレースケールに変換し、同じ検出器を
    そのまま適用する(印刷の枠線・ラベルが相殺され回答マークだけが残る)。
    """
    det = survey.detection
    work = gray
    if clean is not None and det.use_diff and clean.shape == gray.shape:
        # 加筆インク = 基準より暗くなった量。疑似グレー = 1 - 加筆量。
        added = np.clip(clean - gray, 0.0, 1.0)
        work = 1.0 - added
        # 加筆画素 = added > diff_level ⟺ work < (1 - diff_level)
        det = dataclasses.replace(
            det,
            dark_level=1.0 - det.diff_level,
            ink_threshold=det.diff_threshold,
        )
    results: dict[str, QuestionResult] = {}
    for q in survey.questions:
        if q.type not in (SINGLE_CHOICE, MULTI_CHOICE):
            continue
        results[q.id] = _detect_question(q, work, det)
    return results


def _detect_question(
    q: Question, gray: np.ndarray, det: CheckboxDetection
) -> QuestionResult:
    res = QuestionResult(question_id=q.id, type=q.type)
    if q.options and any(o.marker_type == MARKER_CIRCLE_ITEM for o in q.options):
        return _detect_circle_item(q, gray, det)

    margins: dict[str, float] = {}  # 閾値超過の余裕(単一選択の確定に使用)
    for opt in q.options:
        ratio, threshold = _option_ratio(opt, gray, det)
        res.ratios[opt.value] = ratio
        margins[opt.value] = ratio - threshold
        if ratio >= threshold:
            res.checked.append(opt.value)
    if q.type == SINGLE_CHOICE and margins:
        # 単一選択は「閾値をどれだけ超えたか」が最大のものを確定値に
        best = max(margins, key=lambda v: margins[v])
        if margins[best] >= 0:
            res.selected = best
    return res


def _dark_count(gray: np.ndarray, rect: Rect, dark_level: float) -> tuple[int, int]:
    """正規化矩形内の (暗画素数, 総画素数) を返す。"""
    h, w = gray.shape
    x0, y0, x1, y1 = rect
    px0, py0 = max(0, int(x0 * w)), max(0, int(y0 * h))
    px1, py1 = min(w, int(x1 * w)), min(h, int(y1 * h))
    if px1 <= px0 or py1 <= py0:
        return 0, 0
    roi = gray[py0:py1, px0:px1]
    return int((roi < dark_level).sum()), int(roi.size)


def _ring_ink(gray: np.ndarray, box: Rect, det: CheckboxDetection) -> float:
    """番号の「周囲リング」の暗画素比率。

    外側ROI(囲み○が出る帯)から内側(印刷グリフ)を除いた帯のインク比率を返す。
    印刷グリフ自体の底上げを除けるので、囲み有無の分離が良くなる。
    """
    inner = _expand_rect(box, det.circle_expand, det.circle_expand)
    outer = _expand_rect(box, det.circle_ring_expand, det.circle_ring_expand)
    do, to = _dark_count(gray, outer, det.dark_level)
    di, ti = _dark_count(gray, inner, det.dark_level)
    ring_dark, ring_total = do - di, to - ti
    if ring_total <= 0:
        return 0.0
    return max(0.0, ring_dark) / ring_total


def _detect_circle_item(
    q: Question, gray: np.ndarray, det: CheckboxDetection
) -> QuestionResult:
    """丸数字(circle_item)設問: 番号の周囲リングのインク量(=手書き囲み○)を測り、
    設問内の分布から「囲み」のものだけを判定する。

    - 各選択肢のリング指標を算出(_ring_ink)。
    - 最小〜最大の中点をしきい値にして高クラスタを分離(双峰=囲み有/無に強い)。
    - ただしリング比率が circle_min_ink(絶対下限)未満は採用しない(白紙の誤検出防止)。
    単一選択は marked の最大を selected、複数選択は marked 全部を checked にする。
    無印(白紙)なら何も立てない。
    """
    res = QuestionResult(question_id=q.id, type=q.type)
    for opt in q.options:
        res.ratios[opt.value] = _ring_ink(gray, opt.checkbox, det)
    if not res.ratios:
        return res

    vals = list(res.ratios.values())
    lo, hi = min(vals), max(vals)
    floor = det.circle_min_ink
    if hi - lo < floor:
        # 分離なし(全選択肢が同程度): 下限超なら全マーク(全員囲み)、未満なら無印(白紙)
        marked = list(res.ratios) if lo >= floor else []
    else:
        thr = lo + 0.5 * (hi - lo)  # 低クラスタと高クラスタの中点
        marked = [v for v, r in res.ratios.items() if r > thr and r >= floor]

    if q.type == SINGLE_CHOICE:
        if marked:
            best = max(marked, key=lambda v: res.ratios[v])
            res.selected = best
            res.checked = [best]
    else:
        res.checked = marked
    return res
