"""アンケート入力フォーム(右ペイン)。survey_schema から動的生成する。

- single_choice: 排他ラジオ + 「（なし）」で未選択を表現
- multi_choice : チェックボックス群(group があれば小見出し)
- free_text    : 1行(QLineEdit) / 複数行(QPlainTextEdit)

検出結果は set_detection() で反映し、ユーザーが校正できる。最終値は
get_results() で取得する(保存・出力は後工程)。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QTextCursor
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from enquete.schema import (
    FREE_TEXT,
    LAYOUT_HORIZONTAL,
    MULTI_CHOICE,
    SINGLE_CHOICE,
    Question,
    Survey,
)


class FlowLayout(QLayout):
    """折り返し可能な横並びレイアウト(Qt公式 FlowLayout 例の簡略版)。"""

    def __init__(self, spacing: int = 8) -> None:
        super().__init__()
        self._items: list = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        sp = self.spacing()
        for it in self._items:
            sz = it.sizeHint()
            nx = x + sz.width() + sp
            if nx - sp > rect.right() and line_h > 0:
                x = rect.x()
                y = y + line_h + sp
                nx = x + sz.width() + sp
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), sz))
            x = nx
            line_h = max(line_h, sz.height())
        return y + line_h - rect.y()
from enquete.detect import QuestionResult

# 単一選択で「未選択」を表す特別値
_NONE_VALUE = "（なし）"


class FormPane(QWidget):
    """schema からフォームを組み立て、検出結果の反映と校正値の取得を担う。"""

    resultsChanged = Signal()
    reviewedToggled = Signal(bool)
    optionHovered = Signal(str, str)  # (question_id, value)
    optionHoverEnded = Signal()
    regionRequested = Signal(str)  # 自由記述の範囲指定要求 (question_id)
    regionHovered = Signal(str)  # 自由記述欄のフォーカス/ホバー (question_id)

    def __init__(self, survey: Survey, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._survey = survey
        self._reviewed_check: QCheckBox | None = None
        # 選択肢ウィジェット -> (question_id, value)。ホバー連動ハイライト用。
        self._option_widgets: dict[QWidget, tuple[str, str]] = {}
        # 自由記述エディタ -> question_id。フォーカス/ホバー連動ハイライト用。
        self._text_widgets: dict[QWidget, str] = {}
        # qid -> {value: QCheckBox}（複数選択）
        self._checks: dict[str, dict[str, QCheckBox]] = {}
        # qid -> (QButtonGroup, {value: QRadioButton})（単一選択）
        self._radios: dict[str, tuple[QButtonGroup, dict[str, QRadioButton]]] = {}
        # qid -> エディタ（自由記述）
        self._texts: dict[str, QLineEdit | QPlainTextEdit] = {}
        # qid -> 「範囲を編集」トグルボタン（自由記述）
        self._region_buttons: dict[str, QPushButton] = {}
        # 折り返し選択肢の行 -> 対応するチェック/ラジオボタン（行クリックでトグル）
        self._option_buttons: dict[QWidget, QAbstractButton] = {}
        # 範囲確定時の再認識スコープ（確認済み以外すべて / このページのみ）
        self._rescope_all_btn: QRadioButton | None = None

        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(14)

        title = QLabel(self._survey.title)
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: bold;")  # サイズは親(FormPane)の指定を継承
        root.addWidget(title)

        # 「このページを確認済みにする」は下段アクションバーのボタンに集約したため、
        # 右ペイン(フォーム)にはチェックボックスを置かない。set_reviewed は
        # _reviewed_check が None でも no-op で安全に動作する。
        self._reviewed_check = None

        # 自由記述があるときだけ、範囲確定時の再認識スコープを選ばせる
        if any(q.type == FREE_TEXT for q in self._survey.questions):
            lbl = QLabel("「範囲を確定」したときの再認識の対象:")
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-weight: bold;")
            rb_all = QRadioButton()
            rb_page = QRadioButton()
            rb_all.setChecked(True)
            grp = QButtonGroup(self)
            grp.addButton(rb_all)
            grp.addButton(rb_page)
            self._rescope_all_btn = rb_all
            root.addWidget(lbl)
            root.addWidget(self._wrap_control(rb_all, "確認済み以外のすべてのページ"))
            root.addWidget(self._wrap_control(rb_page, "このページのみ"))

        for q in self._survey.questions:
            root.addWidget(self._build_question(q))

        root.addStretch(1)

    def _build_question(self, q: Question) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QLabel(f"{q.no}．{q.label}" if q.no else q.label)
        header.setWordWrap(True)
        header.setStyleSheet("font-weight: bold;")
        layout.addWidget(header)

        if q.type == SINGLE_CHOICE:
            self._build_single(q, layout)
        elif q.type == MULTI_CHOICE:
            self._build_multi(q, layout)
        elif q.type == FREE_TEXT:
            self._build_free_text(q, layout)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)
        return box

    def _layout_options(self, q: Question, layout: QVBoxLayout, make_widget) -> None:
        """選択肢ウィジェットをグループ小見出しつきで配置する。

        q.layout が horizontal なら、各グループの選択肢を折り返し横並び
        (FlowLayout)で並べる。vertical なら従来どおり縦に積む。
        """
        horizontal = q.layout == LAYOUT_HORIZONTAL
        current_group: str | None = "__start__"
        flow: FlowLayout | None = None
        for opt in q.options:
            if opt.group and opt.group != current_group:
                current_group = opt.group
                layout.addWidget(self._group_label(opt.group))
                flow = None  # グループが変わったら新しい行に
            wdg = make_widget(opt)  # 既に _wrap_control 内でホバー登録済み
            if horizontal:
                if flow is None:
                    flow = FlowLayout()
                    layout.addLayout(flow)
                flow.addWidget(wdg)
            else:
                layout.addWidget(wdg)

    def _build_single(self, q: Question, layout: QVBoxLayout) -> None:
        group = QButtonGroup(self)
        group.setExclusive(True)
        mapping: dict[str, QRadioButton] = {}

        def make(opt) -> QWidget:
            rb = QRadioButton()
            group.addButton(rb)
            mapping[opt.value] = rb
            rb.toggled.connect(self._emit_changed)
            return self._wrap_control(rb, opt.value, hover=(q.id, opt.value))

        self._layout_options(q, layout, make)
        # 未選択を表現する選択肢(常に縦の末尾)
        none_rb = QRadioButton()
        none_rb.setChecked(True)
        group.addButton(none_rb)
        mapping[_NONE_VALUE] = none_rb
        layout.addWidget(self._wrap_control(none_rb, _NONE_VALUE))
        self._radios[q.id] = (group, mapping)

    def _build_multi(self, q: Question, layout: QVBoxLayout) -> None:
        mapping: dict[str, QCheckBox] = {}

        def make(opt) -> QWidget:
            cb = QCheckBox()
            mapping[opt.value] = cb
            cb.toggled.connect(self._emit_changed)
            return self._wrap_control(cb, opt.value, hover=(q.id, opt.value))

        self._layout_options(q, layout, make)
        self._checks[q.id] = mapping

    def _build_free_text(self, q: Question, layout: QVBoxLayout) -> None:
        if q.multiline:
            editor: QLineEdit | QPlainTextEdit = QPlainTextEdit()
            editor.setFixedHeight(72)
            editor.textChanged.connect(self._emit_changed)
        else:
            editor = QLineEdit()
            editor.textChanged.connect(self._emit_changed)
        layout.addWidget(editor)
        self._texts[q.id] = editor
        # フォーカス/ホバーで記入領域をPDF上に示すための登録
        self._text_widgets[editor] = q.id
        editor.installEventFilter(self)

        # PDF上で記入領域をハンドルで編集するトグルボタン。
        # 押す=編集開始、もう一度押す=確定して設定スコープのページを再認識(controller側)。
        btn = QPushButton("範囲を編集")
        btn.setCheckable(True)
        btn.setMinimumHeight(34)
        btn.setStyleSheet(
            "QPushButton{padding:6px 12px;}"
            "QPushButton:checked{background:#1a7f37;color:white;font-weight:bold;}"
        )
        btn.clicked.connect(lambda _=False, qid=q.id: self.regionRequested.emit(qid))
        self._region_buttons[q.id] = btn
        layout.addWidget(btn)

    def _group_label(self, text: str) -> QLabel:
        lbl = QLabel(f"［{text}］")
        lbl.setStyleSheet("color: #555;")
        return lbl

    def _emit_changed(self, *_: object) -> None:
        self.resultsChanged.emit()

    def _register_option(self, widget: QWidget, qid: str, value: str) -> None:
        """選択肢ウィジェットをホバー連動の対象として登録する。"""
        self._option_widgets[widget] = (qid, value)
        widget.installEventFilter(self)

    def _wrap_control(
        self,
        button: QAbstractButton,
        text: str,
        hover: tuple[str, str] | None = None,
        label_style: str = "",
    ) -> QWidget:
        """チェック/ラジオを「印＋折り返しラベル」の行にして返す。

        ボタン自身のテキストは折り返さない(Qtの制約)ため、テキストは
        word-wrap する QLabel に出し、行クリックでボタンをトグルする。
        hover=(qid,value) を渡すとホバー連動(ROI強調)の対象にも登録する。
        """
        button.setText("")
        row = QWidget(self)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        label = QLabel(text)
        label.setWordWrap(True)
        if label_style:
            label.setStyleSheet(label_style)
        lay.addWidget(label, 1)
        self._option_buttons[row] = button
        row.installEventFilter(self)  # 行クリックでトグル
        if hover is not None:
            self._option_widgets[row] = hover  # Enter/Leave でROI強調
            # ボタン自身もフォーカス連動の対象に(クリック/Tab移動でPDFが追従)。
            self._option_widgets[button] = hover
            button.installEventFilter(self)
        return row

    def _next_text_editor(self, current: QObject, forward: bool):
        """自由記述エディタの並び順で次/前のエディタを返す(末尾→先頭へラップ)。"""
        editors = list(self._texts.values())
        if current not in editors or not editors:
            return None
        i = editors.index(current)  # type: ignore[arg-type]
        return editors[(i + (1 if forward else -1)) % len(editors)]

    def _focus_adjacent_text(self, current: QObject, forward: bool) -> None:
        """自由記述エディタ間でフォーカスを巡回(末尾→先頭へラップ)する。"""
        nxt = self._next_text_editor(current, forward)
        if nxt is None:
            return
        nxt.setFocus(Qt.FocusReason.TabFocusReason)
        if isinstance(nxt, QLineEdit):
            nxt.selectAll()
        elif isinstance(nxt, QPlainTextEdit):
            nxt.moveCursor(QTextCursor.MoveOperation.End)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        et = event.type()
        # 自由記述の編集中 Tab/Shift+Tab は「次/前の自由記述欄」へ巡回させる
        # (QPlainTextEdit が Tab を取り込まないよう横取りして消費する)。
        if (
            watched in self._text_widgets
            and et == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
        ):
            if event.key() == Qt.Key.Key_Tab:
                self._focus_adjacent_text(watched, True)
                return True
            if event.key() == Qt.Key.Key_Backtab:  # Shift+Tab
                self._focus_adjacent_text(watched, False)
                return True
        # 折り返し選択肢の行(ラベル/余白)クリックで対応ボタンをトグル
        if (
            et == QEvent.Type.MouseButtonPress
            and watched in self._option_buttons
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            btn = self._option_buttons[watched]
            btn.setFocus(Qt.FocusReason.MouseFocusReason)  # PDF を対応箇所へ追従
            btn.click()
            return True
        # 選択肢: ホバー(Enter/Leave)に加え、フォーカス(クリック/Tab)でもPDFを追従。
        # 校正中はフォーカス中の項目を強調し続け、外れたときだけ消す。
        if watched in self._option_widgets and et in (
            QEvent.Type.Enter,
            QEvent.Type.FocusIn,
        ):
            qid, value = self._option_widgets[watched]
            self.optionHovered.emit(qid, value)
        elif watched in self._option_widgets and et in (
            QEvent.Type.Leave,
            QEvent.Type.FocusOut,
        ):
            if not self._option_active(watched):
                self.optionHoverEnded.emit()
        elif watched in self._text_widgets and et in (
            QEvent.Type.Enter,
            QEvent.Type.Leave,
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
        ):
            # フォーカス中 もしくは ホバー中 は領域を表示、どちらでもなければ消す
            if watched.hasFocus() or watched.underMouse():
                self.regionHovered.emit(self._text_widgets[watched])
            else:
                self.optionHoverEnded.emit()
        return super().eventFilter(watched, event)

    def _option_active(self, watched: QObject) -> bool:
        """選択肢(行 or ボタン)が、まだフォーカス中かホバー中か。

        行(row)が watched のときは対応ボタンのフォーカスも見る。どちらかが生きて
        いれば強調を維持する(クリック直後に Leave で消えてしまうのを防ぐ)。
        """
        btn = self._option_buttons.get(watched, watched)
        focused = isinstance(btn, QWidget) and btn.hasFocus()
        hovered = isinstance(watched, QWidget) and watched.underMouse()
        return bool(focused or hovered)

    # -------------------------------------------------------------- detection
    def set_detection(self, results: dict[str, QuestionResult]) -> None:
        """検出結果をフォームに反映する(選択式のみ。自由記述は OCR 工程で)。"""
        for qid, (group, mapping) in self._radios.items():
            res = results.get(qid)
            group.blockSignals(True)
            value = res.selected if res is not None else None
            target = mapping.get(value) if value else mapping.get(_NONE_VALUE)
            if target is not None:
                target.setChecked(True)
            group.blockSignals(False)

        for qid, mapping in self._checks.items():
            res = results.get(qid)
            checked = set(res.checked) if res is not None else set()
            for value, cb in mapping.items():
                cb.blockSignals(True)
                cb.setChecked(value in checked)
                cb.blockSignals(False)
        self.resultsChanged.emit()

    def set_results_dict(self, results: dict[str, object]) -> None:
        """保存済みの校正結果(get_results 形式)をフォームへ復元する。"""
        for qid, (group, mapping) in self._radios.items():
            value = results.get(qid)
            group.blockSignals(True)
            target = mapping.get(value) if isinstance(value, str) else None
            (target or mapping[_NONE_VALUE]).setChecked(True)
            group.blockSignals(False)

        for qid, mapping in self._checks.items():
            chosen = results.get(qid) or []
            chosen_set = set(chosen) if isinstance(chosen, list) else set()
            for value, cb in mapping.items():
                cb.blockSignals(True)
                cb.setChecked(value in chosen_set)
                cb.blockSignals(False)

        for qid, editor in self._texts.items():
            text = results.get(qid)
            text = text if isinstance(text, str) else ""
            editor.blockSignals(True)
            if isinstance(editor, QPlainTextEdit):
                editor.setPlainText(text)
            else:
                editor.setText(text)
            editor.blockSignals(False)
        self.resultsChanged.emit()

    def set_reviewed(self, reviewed: bool) -> None:
        """確認済みチェックを同期する(シグナルは出さない)。"""
        if self._reviewed_check is not None:
            self._reviewed_check.blockSignals(True)
            self._reviewed_check.setChecked(reviewed)
            self._reviewed_check.blockSignals(False)

    def region_rescope_all(self) -> bool:
        """範囲確定時の再認識スコープが「確認済み以外すべて」なら True。

        ラジオ未生成（自由記述なし）の場合も True（=全件側）を既定とする。
        """
        return self._rescope_all_btn is None or self._rescope_all_btn.isChecked()

    def set_region_edit_enabled(self, enabled: bool) -> None:
        """自由記述の「範囲を編集」ボタンの有効/無効を一括で切り替える。

        校正モード(校正作業のみ)では範囲編集はフォーム設定の作業なので無効化する。
        """
        for btn in self._region_buttons.values():
            btn.setEnabled(enabled)

    def set_region_editing(self, active_qid: str | None) -> None:
        """「範囲を編集」トグルの表示状態を同期する(編集中の設問だけ押下状態)。

        controller が編集の開始/終了に合わせて呼ぶ。シグナルは出さない。
        """
        for qid, btn in self._region_buttons.items():
            active = qid == active_qid
            btn.blockSignals(True)
            btn.setChecked(active)
            btn.setText("範囲を確定（以降を再認識）" if active else "範囲を編集")
            btn.blockSignals(False)

    def toggle_option(self, qid: str, value: str) -> None:
        """指定選択肢のチェック状態をトグルする(PDF上ダブルクリック連動)。

        複数選択は ON/OFF を反転。単一選択は、既に選択済みなら「(なし)」にして
        解除、未選択なら選択する。状態変化で resultsChanged が発火する。
        """
        if qid in self._checks:
            cb = self._checks[qid].get(value)
            if cb is not None:
                cb.setChecked(not cb.isChecked())
        elif qid in self._radios:
            _group, mapping = self._radios[qid]
            rb = mapping.get(value)
            if rb is None:
                return
            if rb.isChecked():
                none_rb = mapping.get(_NONE_VALUE)
                if none_rb is not None:
                    none_rb.setChecked(True)  # 解除(=未選択へ)
            else:
                rb.setChecked(True)

    def set_text(self, question_id: str, text: str) -> None:
        """自由記述欄へテキストを設定(OCR 工程から使用)。"""
        editor = self._texts.get(question_id)
        if isinstance(editor, QPlainTextEdit):
            editor.setPlainText(text)
        elif isinstance(editor, QLineEdit):
            editor.setText(text)

    # ----------------------------------------------------------------- output
    def get_results(self) -> dict[str, object]:
        """現在の校正値を取得する(保存・出力用)。"""
        out: dict[str, object] = {}
        for q in self._survey.questions:
            if q.type == SINGLE_CHOICE:
                _, mapping = self._radios[q.id]
                sel = next(
                    (v for v, rb in mapping.items() if rb.isChecked()), _NONE_VALUE
                )
                out[q.id] = None if sel == _NONE_VALUE else sel
            elif q.type == MULTI_CHOICE:
                mapping = self._checks[q.id]
                out[q.id] = [v for v, cb in mapping.items() if cb.isChecked()]
            elif q.type == FREE_TEXT:
                editor = self._texts[q.id]
                if isinstance(editor, QPlainTextEdit):
                    out[q.id] = editor.toPlainText()
                else:
                    out[q.id] = editor.text()
        return out
