"""フォーム定義の構造エディタ(GUI一覧)。

設問・選択肢をツリー表示し、ラベル/選択肢名の編集・削除・種別変更・追加・
並べ替えを行う。JSON を直接触らずにフォーム定義を校正できる。作業は survey の
ディープコピー上で行い、「適用」で applied(survey) を発火する。
"""
from __future__ import annotations

import copy

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from enquete.schema import (
    FREE_TEXT,
    LAYOUT_HORIZONTAL,
    LAYOUT_VERTICAL,
    MARKER_BOX,
    MARKER_CIRCLE_ITEM,
    MARKER_NUMBER,
    MULTI_CHOICE,
    SINGLE_CHOICE,
    Option,
    Question,
    Survey,
)

_Q_TYPES = [("単一選択", SINGLE_CHOICE), ("複数選択", MULTI_CHOICE), ("自由記述", FREE_TEXT)]
_M_TYPES = [("□チェック", MARKER_BOX), ("番号●", MARKER_NUMBER), ("丸囲み(手動)", MARKER_CIRCLE_ITEM)]
_LAYOUTS = [("縦並び", LAYOUT_VERTICAL), ("横並び", LAYOUT_HORIZONTAL)]
_Q_LABEL = {t: n for n, t in _Q_TYPES}
_M_LABEL = {t: n for n, t in _M_TYPES}
_LAYOUT_LABEL = {t: n for n, t in _LAYOUTS}
_ROLE = Qt.ItemDataRole.UserRole


class FormEditorDialog(QDialog):
    applied = Signal(object)  # 編集後の Survey(ディープコピー)
    changed = Signal()  # live モード: 構造/ラベル/種別を変更した(主画面を更新)
    targetSelected = Signal(object)  # live モード: 選択中の Question/Option(位置編集対象)

    def __init__(
        self, survey: Survey, parent: QWidget | None = None, live: bool = False
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("フォーム編集")
        self.resize(560, 640)
        # live=True は生 survey を直接編集(PDF側の位置編集と同じオブジェクトを共有)。
        # 非 live はディープコピー上で編集し「適用」で反映する(従来挙動)。
        self._live = live
        self._survey = survey if live else copy.deepcopy(survey)
        self._loading = False
        self._populating = False  # 再描画中の中間選択で targetSelected を出さない
        # 編集ウィンドウは常に最前面に出す(PDFを操作中も隠れない)。
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["内容（ダブルクリックで編集）", "種別 / 番号"])
        self._tree.setColumnWidth(0, 360)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.currentItemChanged.connect(self._on_selection)
        # 左右矢印で 子→親を畳む / 親を展開 する独自操作
        self._tree.installEventFilter(self)

        self._type_combo = QComboBox()
        self._type_combo.setEnabled(False)
        self._type_combo.activated.connect(self._on_type_changed)

        self._layout_combo = QComboBox()
        for name, _t in _LAYOUTS:
            self._layout_combo.addItem(name)
        self._layout_combo.setEnabled(False)
        self._layout_combo.activated.connect(self._on_layout_changed)

        def btn(text: str, slot) -> QPushButton:
            b = QPushButton(text)
            b.clicked.connect(slot)
            return b

        row1 = QHBoxLayout()
        # 追加は1つに統合: 選択中が選択肢なら選択肢を、設問(または未選択)なら設問を追加。
        add_btn = btn("追加", self._add_smart)
        add_btn.setToolTip("選択肢を選んでいれば選択肢を、設問を選んでいれば設問を追加します")
        row1.addWidget(add_btn)
        row1.addWidget(btn("削除", self._delete))
        row2 = QHBoxLayout()
        row2.addWidget(btn("↑", self._move_up))
        row2.addWidget(btn("↓", self._move_down))
        row2.addWidget(QLabel("種別:"))
        row2.addWidget(self._type_combo, 1)
        row2.addWidget(QLabel("並び:"))
        row2.addWidget(self._layout_combo)

        buttons = QDialogButtonBox()
        apply_btn = QPushButton("適用")
        buttons.addButton(apply_btn, QDialogButtonBox.ButtonRole.ApplyRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        apply_btn.clicked.connect(self._emit_applied)
        buttons.rejected.connect(self.close)
        # live は変更が即時反映されるため「適用」は不要(混乱を避けて隠す)。
        if self._live:
            apply_btn.setVisible(False)

        layout = QVBoxLayout(self)
        intro = (
            "設問・選択肢を編集できます。項目を選ぶと、その位置（□・記入範囲）を"
            "PDF上でドラッグして調整できます。"
            if self._live
            else "設問・選択肢を編集して「適用」してください。"
        )
        layout.addWidget(QLabel(intro))
        layout.addWidget(self._tree, 1)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addWidget(buttons)

        self._populate()

    # ----------------------------------------------- 左右矢印の展開/折りたたみ
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._tree and isinstance(event, QKeyEvent):
            if event.type() == QEvent.Type.KeyPress:
                item = self._tree.currentItem()
                if item is not None:
                    key = event.key()
                    parent = item.parent()
                    if key == Qt.Key.Key_Left:
                        if parent is not None:
                            # 子を選択中 → 親を畳んで親へ移動
                            parent.setExpanded(False)
                            self._tree.setCurrentItem(parent)
                            return True
                        if item.isExpanded():  # 展開中の親 → 畳む
                            item.setExpanded(False)
                            return True
                    elif key == Qt.Key.Key_Right:
                        if parent is None and item.childCount() > 0:
                            item.setExpanded(True)  # 親 → 展開
                            return True
        return super().eventFilter(watched, event)

    # --------------------------------------------------------------- populate
    def _populate(self) -> None:
        # 再描画で展開状態が失われないよう、設問オブジェクトの id で記憶/復元する
        prev_expanded: dict[int, bool] = {}
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            q = it.data(0, _ROLE)
            if q is not None:
                prev_expanded[id(q)] = it.isExpanded()
        self._loading = True
        self._populating = True
        self._tree.clear()
        for q in self._survey.questions:
            qi = QTreeWidgetItem([q.label, self._q_info(q)])
            qi.setData(0, _ROLE, q)
            qi.setFlags(qi.flags() | Qt.ItemFlag.ItemIsEditable)
            self._tree.addTopLevelItem(qi)
            for o in q.options:
                oi = QTreeWidgetItem([o.value, _M_LABEL.get(o.marker_type, o.marker_type)])
                oi.setData(0, _ROLE, o)
                oi.setFlags(oi.flags() | Qt.ItemFlag.ItemIsEditable)
                qi.addChild(oi)
            # 既知の設問は前回の展開状態を維持、新規(初回含む)は展開で表示
            qi.setExpanded(prev_expanded.get(id(q), True))
        self._loading = False
        self._populating = False

    @staticmethod
    def _q_info(q: Question) -> str:
        no = f"{q.no}．" if q.no else ""
        # 既定は横並びのため、縦のときだけ明示表示する
        suffix = " ・縦" if q.layout == LAYOUT_VERTICAL and q.options else ""
        return f"{no}{_Q_LABEL.get(q.type, q.type)}{suffix}"

    # ------------------------------------------------------------- selection
    def _selected(self) -> object | None:
        it = self._tree.currentItem()
        return it.data(0, _ROLE) if it is not None else None

    def _parent_question(self, opt: Option) -> Question | None:
        for q in self._survey.questions:
            if opt in q.options:
                return q
        return None

    def _on_selection(self, *_: object) -> None:
        ref = self._selected()
        self._loading = True
        self._type_combo.clear()
        if isinstance(ref, Question):
            for name, _t in _Q_TYPES:
                self._type_combo.addItem(name)
            self._type_combo.setCurrentText(_Q_LABEL.get(ref.type, ""))
            self._type_combo.setEnabled(True)
            self._layout_combo.setCurrentText(_LAYOUT_LABEL.get(ref.layout, "縦並び"))
            self._layout_combo.setEnabled(ref.type != FREE_TEXT)
        elif isinstance(ref, Option):
            for name, _t in _M_TYPES:
                self._type_combo.addItem(name)
            self._type_combo.setCurrentText(_M_LABEL.get(ref.marker_type, ""))
            self._type_combo.setEnabled(True)
            self._layout_combo.setEnabled(False)
        else:
            self._type_combo.setEnabled(False)
            self._layout_combo.setEnabled(False)
        self._loading = False
        # live: 選んだ項目を位置編集の対象として主画面へ伝える(再描画中の中間選択は除く)。
        if self._live and not self._populating:
            self.targetSelected.emit(ref)

    # -------------------------------------------------------------- editing
    def _on_item_changed(self, item: QTreeWidgetItem, col: int) -> None:
        if self._loading or col != 0:
            return
        ref = item.data(0, _ROLE)
        text = item.text(0)
        if isinstance(ref, Question):
            ref.label = text
        elif isinstance(ref, Option):
            ref.value = text
        self._emit_changed()

    def _on_type_changed(self, idx: int) -> None:
        if self._loading:
            return
        ref = self._selected()
        if isinstance(ref, Question):
            ref.type = _Q_TYPES[idx][1]
            if ref.type == FREE_TEXT:
                ref.options = []  # 自由記述は選択肢を持たない
                if ref.region is None:
                    ref.region = (0.1, 0.5, 0.9, 0.6)  # 仮(PDFで範囲指定で調整)
            self._populate_keep(ref)
        elif isinstance(ref, Option):
            ref.marker_type = _M_TYPES[idx][1]
            self._populate_keep(ref)

    def _on_layout_changed(self, idx: int) -> None:
        if self._loading:
            return
        ref = self._selected()
        if isinstance(ref, Question):
            ref.layout = _LAYOUTS[idx][1]
            self._populate_keep(ref)

    # ------------------------------------------------------------ structure
    def _new_qid(self) -> str:
        existing = {q.id for q in self._survey.questions}
        k = 1
        while f"e{k}" in existing:
            k += 1
        return f"e{k}"

    def _ask_add(
        self, title: str, choices: list[tuple[str, str]], default_name: str
    ) -> tuple[str, str] | None:
        """追加ダイアログ: 種別(ドロップダウン)と名称を入力させる。

        choices=[(表示名, 値)]。戻り値 (選んだ値, 入力名) / キャンセルなら None。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        form = QFormLayout(dlg)
        combo = QComboBox()
        for name, _t in choices:
            combo.addItem(name)
        name_edit = QLineEdit(default_name)
        name_edit.selectAll()
        form.addRow("種別:", combo)
        form.addRow("名称:", name_edit)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        typ = dict((n, t) for n, t in choices)[combo.currentText()]
        return typ, (name_edit.text().strip() or default_name)

    def _add_smart(self) -> None:
        """「追加」: 種別と名称をダイアログで入力し、選択行の直後に追加する。

        選択中が選択肢なら選択肢を、設問(または未選択)なら設問を追加する。
        """
        ref = self._selected()
        if isinstance(ref, Option):
            got = self._ask_add("選択肢を追加", _M_TYPES, "新しい選択肢")
            if got is not None:
                self._add_option(got[0], got[1], after=ref)
        else:
            got = self._ask_add("設問を追加", _Q_TYPES, "新しい設問")
            if got is not None:
                self._add_question(got[0], got[1])

    def _add_question(self, qtype: str = SINGLE_CHOICE, label: str = "新しい設問") -> None:
        q = Question(id=self._new_qid(), label=label, type=qtype)
        # 選択式は空だと選択肢を足せないので、最初の選択肢を1つ用意しておく。
        if qtype != FREE_TEXT:
            q.options.append(
                Option(value="新しい選択肢", checkbox=(0.1, 0.1, 0.12, 0.111),
                       marker_type=MARKER_BOX)
            )
        else:
            q.region = (0.1, 0.5, 0.9, 0.6)  # 仮(位置編集で調整)
        # 挿入位置: 選択行(設問。選択肢選択時はその親設問)の直後。
        ref = self._selected()
        idx = len(self._survey.questions)
        if isinstance(ref, Question):
            idx = self._survey.questions.index(ref) + 1
        elif isinstance(ref, Option):
            pq = self._parent_question(ref)
            if pq is not None:
                idx = self._survey.questions.index(pq) + 1
        self._survey.questions.insert(idx, q)
        self._populate_keep(q)

    def _add_option(
        self, marker_type: str = MARKER_BOX, value: str = "新しい選択肢",
        after: Option | None = None,
    ) -> None:
        q = self._parent_question(after) if after is not None else None
        if q is None:
            ref = self._selected()
            q = ref if isinstance(ref, Question) else (
                self._parent_question(ref) if isinstance(ref, Option) else None
            )
        if q is None or q.type == FREE_TEXT:
            return
        # ROI: 直後に置く基準(選択中の選択肢、無ければ末尾)の少し下に仮配置。
        anchor = after if (after is not None and after in q.options) else (
            q.options[-1] if q.options else None
        )
        if anchor is not None:
            x0, y0, x1, y1 = anchor.checkbox
            dy = (y1 - y0) or 0.012
            box = (x0, min(0.99, y0 + dy * 1.5), x1, min(1.0, y1 + dy * 1.5))
        else:
            box = (0.1, 0.1, 0.12, 0.111)
        o = Option(value=value, checkbox=box, marker_type=marker_type)
        # 挿入位置: 選択中の選択肢の直後。無ければ末尾。
        if after is not None and after in q.options:
            q.options.insert(q.options.index(after) + 1, o)
        else:
            q.options.append(o)
        self._populate_keep(o)

    def _delete(self) -> None:
        ref = self._selected()
        if isinstance(ref, Question):
            self._survey.questions.remove(ref)
        elif isinstance(ref, Option):
            q = self._parent_question(ref)
            if q is not None:
                q.options.remove(ref)
        self._populate()
        self._emit_changed()

    def _move(self, delta: int) -> None:
        ref = self._selected()
        if isinstance(ref, Question):
            seq = self._survey.questions
        elif isinstance(ref, Option):
            pq = self._parent_question(ref)
            seq = pq.options if pq is not None else None
        else:
            seq = None
        if seq is None:
            return
        i = seq.index(ref)
        j = i + delta
        if 0 <= j < len(seq):
            seq[i], seq[j] = seq[j], seq[i]
            self._populate_keep(ref)

    def _move_up(self) -> None:
        self._move(-1)

    def _move_down(self) -> None:
        self._move(+1)

    def _populate_keep(self, ref: object) -> None:
        """再描画し、ref に対応する項目を再選択する。"""
        self._populate()
        self._select_ref(ref)
        self._emit_changed()

    def select_target(self, qid: str, value: str | None) -> None:
        """主画面(PDFクリック)からの指定で、対応する設問/選択肢をツリー選択する。

        相互参照のループを避けるため、ここでは targetSelected を再送しない。
        """
        target: object | None = None
        for q in self._survey.questions:
            if q.id != qid:
                continue
            if value is None:
                target = q
            else:
                target = next((o for o in q.options if o.value == value), None)
            break
        if target is None:
            return
        self._populating = True  # targetSelected を抑止
        try:
            self._select_ref(target)
        finally:
            self._populating = False

    def _select_ref(self, ref: object) -> None:
        def walk(item: QTreeWidgetItem) -> bool:
            if item.data(0, _ROLE) is ref:
                self._tree.setCurrentItem(item)
                return True
            for k in range(item.childCount()):
                if walk(item.child(k)):
                    return True
            return False

        for k in range(self._tree.topLevelItemCount()):
            if walk(self._tree.topLevelItem(k)):
                return

    # ---------------------------------------------------------------- apply
    def _emit_changed(self) -> None:
        """live モードのみ: 構造/ラベル変更を主画面へ通知する。"""
        if self._live:
            self.changed.emit()

    def _emit_applied(self) -> None:
        self.applied.emit(copy.deepcopy(self._survey))

    def current_survey(self) -> Survey:
        """現在の編集内容(作業コピー)のディープコピーを返す。

        モーダルで使う場合、「適用」を押していなくても閉じた時点の編集結果を
        確実に取得できる。
        """
        return copy.deepcopy(self._survey)
