"""アンケート設問定義のデータモデルと (de)シリアライズ。

設問・選択肢・自由記述領域を dataclass へマップする。座標はすべて
ページに対する正規化値(左上原点, 0..1)で、checkbox は □ グリフ矩形
[x0, y0, x1, y1]、free_text は記入領域 region [x0, y0, x1, y1]。

フォーム定義は内蔵せず、ツールの「フォーム作成／読み込み」で外部から与える。
dict との相互変換(survey_from_dict / survey_to_dict)が中核。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 設問タイプ
SINGLE_CHOICE = "single_choice"
MULTI_CHOICE = "multi_choice"
FREE_TEXT = "free_text"

# 選択肢の並べ方
LAYOUT_VERTICAL = "vertical"
LAYOUT_HORIZONTAL = "horizontal"

# マーカー種別(回答方式)。検出方法を種別ごとに切り替える。
MARKER_BOX = "box"  # □ にチェック(枠内インク, 空欄≈0)
MARKER_NUMBER = "number_mark"  # （1）等の番号に ●/〇。OpenCVの外接円半径で検出。
MARKER_CIRCLE_ITEM = "circle_item"  # ①等(丸数字)を〇で囲む。自動検出せず手動校正。

Rect = tuple[float, float, float, float]


@dataclass
class Option:
    # フォーム編集で値・種別を更新するため可変(非 frozen)。
    value: str
    checkbox: Rect  # マーカー基準矩形(□グリフ or 番号グリフ)。正規化・左上原点。
    group: str | None = None
    has_text: bool = False
    marker_type: str = MARKER_BOX


@dataclass
class Question:
    # region/layout は編集で更新するため可変(非 frozen)。
    id: str
    label: str
    type: str
    no: str | None = None
    options: list[Option] = field(default_factory=list)
    region: Rect | None = None
    multiline: bool = False
    layout: str = LAYOUT_HORIZONTAL  # 選択肢の並べ方(縦/横)。既定は横並び。


@dataclass
class CheckboxDetection:
    # 検出パラメータは UI から調整・保存するため可変(非 frozen)。
    render_scale: float = 2.0
    roi_inset: float = 0.30  # box: 枠線を除外する内側マージン
    ink_threshold: float = 0.25  # box: チェック判定の暗画素比率
    dark_level: float = 0.5  # 「暗い」とみなすグレー閾値(共通)
    # number_mark 用: 番号マーカーROIを異方拡張(x広め/y狭め=隣接行の混入回避)し、
    # OpenCVで最大輪郭の外接円半径(÷ROI幅)を測る。●/〇の丸を形状で捉える。
    mark_expand_x: float = 0.9  # x方向の拡張率
    mark_expand_y: float = 0.25  # y方向の拡張率(行間混入を避けるため小さめ)
    mark_threshold: float = 0.11  # 外接円半径比のマーク判定閾値
    # circle_item(丸数字)用: 印刷グリフ自体が円で絶対閾値が効かないため、番号の
    # 「周囲リング(手書きの囲み○が出る帯)」のインク量を測り、設問内の分布から
    # 外れ値だけを「囲み」と判定する。
    circle_expand: float = 0.15  # 内側除外マージン(印刷グリフを除く)
    circle_ring_expand: float = 0.8  # 外側リングの拡張率(囲み○を取り込む)
    circle_min_ink: float = 0.06  # リングを「囲み」と認める最小インク比率(絶対下限)
    # 差分検出: 1ページ目(クリーン基準)との差分で記入を判定する。印刷物(枠線・
    # ラベル)が相殺され、回答マークだけが残るので分離が良い。基準は先頭ページ。
    use_diff: bool = False  # 差分検出を使うか
    diff_level: float = 0.25  # 「加筆インク」とみなす明度差(基準−記入)の閾値
    diff_threshold: float = 0.08  # 差分でのチェック判定(ROI内の加筆画素比率)


@dataclass(frozen=True)
class Survey:
    survey_id: str
    title: str
    questions: list[Question]
    detection: CheckboxDetection
    source_pdf: str | None = None
    page: int = 0


def _as_rect(v: list[float]) -> Rect:
    x0, y0, x1, y1 = v
    return (float(x0), float(y0), float(x1), float(y1))


def survey_from_dict(data: dict) -> Survey:
    """schema 相当の dict から Survey を構築する(ファイル/サイドカー共通)。"""
    det_raw = data.get("checkbox_detection", {})
    detection = CheckboxDetection(
        render_scale=float(det_raw.get("render_scale", 2.0)),
        roi_inset=float(det_raw.get("roi_inset", 0.30)),
        ink_threshold=float(det_raw.get("ink_threshold", 0.25)),
        dark_level=float(det_raw.get("dark_level", 0.5)),
        mark_expand_x=float(det_raw.get("mark_expand_x", 0.9)),
        mark_expand_y=float(det_raw.get("mark_expand_y", 0.25)),
        mark_threshold=float(det_raw.get("mark_threshold", 0.11)),
        circle_expand=float(det_raw.get("circle_expand", 0.15)),
        circle_ring_expand=float(det_raw.get("circle_ring_expand", 0.8)),
        circle_min_ink=float(det_raw.get("circle_min_ink", 0.06)),
        use_diff=bool(det_raw.get("use_diff", False)),
        diff_level=float(det_raw.get("diff_level", 0.25)),
        diff_threshold=float(det_raw.get("diff_threshold", 0.08)),
    )

    questions: list[Question] = []
    for q in data["questions"]:
        options = [
            Option(
                value=o["value"],
                checkbox=_as_rect(o["checkbox"]),
                group=o.get("group"),
                has_text=bool(o.get("has_text", False)),
                marker_type=o.get("marker_type", MARKER_BOX),
            )
            for o in q.get("options", [])
        ]
        region = _as_rect(q["region"]) if q.get("region") is not None else None
        questions.append(
            Question(
                id=q["id"],
                label=q["label"],
                type=q["type"],
                no=q.get("no"),
                options=options,
                region=region,
                multiline=bool(q.get("multiline", False)),
                layout=q.get("layout", LAYOUT_HORIZONTAL),
            )
        )

    return Survey(
        survey_id=data["survey_id"],
        title=data["title"],
        questions=questions,
        detection=detection,
        source_pdf=data.get("source_pdf"),
        page=int(data.get("page", 0)),
    )


def survey_to_dict(survey: Survey) -> dict:
    """Survey を schema 相当の dict へ変換する(サイドカーへの埋め込み用)。"""
    questions = []
    for q in survey.questions:
        qd: dict = {"id": q.id, "label": q.label, "type": q.type}
        if q.no is not None:
            qd["no"] = q.no
        if q.options:
            opts = []
            for o in q.options:
                od: dict = {"value": o.value, "checkbox": list(o.checkbox)}
                if o.group is not None:
                    od["group"] = o.group
                if o.has_text:
                    od["has_text"] = True
                if o.marker_type != MARKER_BOX:
                    od["marker_type"] = o.marker_type
                opts.append(od)
            qd["options"] = opts
        if q.region is not None:
            qd["region"] = list(q.region)
        if q.multiline:
            qd["multiline"] = True
        if q.layout != LAYOUT_HORIZONTAL:
            qd["layout"] = q.layout  # 既定(横)以外(=縦)のときだけ明示保存
        questions.append(qd)
    return {
        "survey_id": survey.survey_id,
        "title": survey.title,
        "source_pdf": survey.source_pdf,
        "page": survey.page,
        "checkbox_detection": {
            "render_scale": survey.detection.render_scale,
            "roi_inset": survey.detection.roi_inset,
            "ink_threshold": survey.detection.ink_threshold,
            "dark_level": survey.detection.dark_level,
            "mark_expand_x": survey.detection.mark_expand_x,
            "mark_expand_y": survey.detection.mark_expand_y,
            "mark_threshold": survey.detection.mark_threshold,
            "circle_expand": survey.detection.circle_expand,
            "circle_ring_expand": survey.detection.circle_ring_expand,
            "circle_min_ink": survey.detection.circle_min_ink,
            "use_diff": survey.detection.use_diff,
            "diff_level": survey.detection.diff_level,
            "diff_threshold": survey.detection.diff_threshold,
        },
        "questions": questions,
    }
