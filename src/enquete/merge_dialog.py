"""分割(Split)・マージ(Merge)ダイアログ。

- SplitDialog: 開いている PDF＋フォームを N 分割し、各チャンク(PDF＋同一
  フォームを複製した JSON)を出力する。手分け作業のための配布物を作る。
- MergeDialog: 各担当が校正したチャンク(サイドカーJSON)を集め、PDFを連結
  しつつページ番号をオフセット付け替えして1つに統合する。

実処理は Qt 非依存の enquete.merge に委譲する。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from enquete import merge as M
from enquete.schema import Survey


class SplitDialog(QDialog):
    """PDF を N 分割して配布用チャンクを書き出すダイアログ。"""

    def __init__(
        self, pdf_path: Path, survey: Survey, total_pages: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("アンケートを分割")
        self.resize(520, 360)
        self._pdf_path = pdf_path
        self._survey = survey
        self._total = total_pages
        self.result_paths: list[Path] = []

        self._spin = QSpinBox()
        self._spin.setRange(1, total_pages)
        self._spin.setValue(min(2, total_pages))
        self._spin.valueChanged.connect(self._update_preview)

        self._preview = QLabel()
        self._preview.setWordWrap(True)
        self._preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        default_dir = pdf_path.parent / f"{pdf_path.stem}_split"
        self._out_edit = QLineEdit(str(default_dir))
        browse = QPushButton("参照…")
        browse.clicked.connect(self._choose_dir)

        top = QHBoxLayout()
        top.addWidget(QLabel(f"総ページ数: {total_pages}　分割数:"))
        top.addWidget(self._spin)
        top.addStretch(1)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("出力フォルダ:"))
        out_row.addWidget(self._out_edit, 1)
        out_row.addWidget(browse)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._run)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "PDF を均等に分割し、各パートに同じフォーム定義を複製します"
                "（ページ結果は空）。各担当に配布して校正後、マージで統合します。"
            )
        )
        layout.addLayout(top)
        layout.addWidget(self._preview, 1)
        layout.addLayout(out_row)
        layout.addWidget(buttons)
        self._update_preview()

    def _update_preview(self) -> None:
        try:
            ranges = M.plan_split(self._total, self._spin.value())
        except ValueError as e:
            self._preview.setText(f"⚠ {e}")
            return
        lines = [
            f"パート{i + 1}: ページ {off + 1}〜{off + cnt}（{cnt}ページ）"
            for i, (off, cnt) in enumerate(ranges)
        ]
        self._preview.setText("\n".join(lines))

    def _choose_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "出力フォルダを選択", str(self._pdf_path.parent)
        )
        if d:
            self._out_edit.setText(d)

    def _run(self) -> None:
        out_dir = Path(self._out_edit.text().strip())
        if not out_dir:
            QMessageBox.warning(self, "分割", "出力フォルダを指定してください。")
            return
        try:
            self.result_paths = M.split_pdf(
                self._pdf_path, self._survey, self._spin.value(), out_dir
            )
        except (ValueError, OSError) as e:
            QMessageBox.critical(self, "分割", f"分割に失敗しました:\n{e}")
            return
        names = "\n".join(f"・{p.name}" for p in self.result_paths)
        QMessageBox.information(
            self,
            "分割完了",
            f"{len(self.result_paths)} パートを書き出しました:\n{out_dir}\n\n{names}",
        )
        self.accept()


class MergeDialog(QDialog):
    """チャンク(サイドカーJSON)を集めて連結・統合するダイアログ。"""

    def __init__(self, parent: QWidget | None = None, start_dir: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("アンケートをマージ")
        self.resize(600, 480)
        self._start_dir = start_dir or str(Path.cwd())
        self.merged_pdf: Path | None = None

        self._list = QListWidget()
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )

        add_btn = QPushButton("JSONを追加…")
        add_btn.clicked.connect(self._add_files)
        rm_btn = QPushButton("削除")
        rm_btn.clicked.connect(self._remove_selected)
        up_btn = QPushButton("↑")
        up_btn.clicked.connect(lambda: self._move(-1))
        down_btn = QPushButton("↓")
        down_btn.clicked.connect(lambda: self._move(+1))
        auto_btn = QPushButton("由来から自動整列")
        auto_btn.clicked.connect(self._auto_sort)

        btn_row = QHBoxLayout()
        for b in (add_btn, rm_btn, up_btn, down_btn, auto_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)

        self._partial = QCheckBox("パートが欠けていても揃った分だけマージする")

        self._out_edit = QLineEdit()
        out_browse = QPushButton("参照…")
        out_browse.clicked.connect(self._choose_out)
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("出力PDF:"))
        out_row.addWidget(self._out_edit, 1)
        out_row.addWidget(out_browse)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if self._ok_btn is not None:
            self._ok_btn.setText("マージ実行")
        buttons.accepted.connect(self._run)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "各担当が校正したチャンクのサイドカーJSONを追加してください。"
                "由来メタがあれば順序は自動整列できます。"
            )
        )
        layout.addWidget(self._list, 1)
        layout.addLayout(btn_row)
        layout.addWidget(self._partial)
        layout.addLayout(out_row)
        layout.addWidget(buttons)

    # ----------------------------------------------------------- list ops
    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "チャンクのサイドカーJSONを選択", self._start_dir,
            "Sidecar JSON (*.json)",
        )
        existing = {self._list.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(self._list.count())}
        for p in paths:
            if p in existing:
                continue
            try:
                inp = M.load_merge_input(p)
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, "マージ", f"{Path(p).name}: {e}")
                continue
            meta = inp.split_meta
            if meta:
                label = (
                    f"パート{meta.get('part')}/{meta.get('parts')}"
                    f"（{meta.get('original_pdf')} 先頭{meta.get('page_offset')}）"
                    f"  {Path(p).name}"
                )
            else:
                label = f"（由来情報なし）{Path(p).name}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self._list.addItem(item)
        if not self._out_edit.text() and self._list.count() > 0:
            first = M.load_merge_input(
                self._list.item(0).data(Qt.ItemDataRole.UserRole)
            )
            meta = first.split_meta
            base = (
                Path(meta["original_pdf"]).stem if meta else "merged"
            )
            self._out_edit.setText(
                str(first.pdf_path.parent / f"{base}_merged.pdf")
            )

    def _remove_selected(self) -> None:
        for item in self._list.selectedItems():
            self._list.takeItem(self._list.row(item))

    def _move(self, delta: int) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        new = row + delta
        if not (0 <= new < self._list.count()):
            return
        item = self._list.takeItem(row)
        self._list.insertItem(new, item)
        self._list.setCurrentRow(new)

    def _paths(self) -> list[str]:
        return [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
        ]

    def _auto_sort(self) -> None:
        # (json パス, ラベル, MergeInput) の組で扱い、由来メタで整列する
        try:
            triples = []
            for i in range(self._list.count()):
                it = self._list.item(i)
                jpath = it.data(Qt.ItemDataRole.UserRole)
                triples.append((jpath, it.text(), M.load_merge_input(jpath)))
            _, warnings = M.order_and_validate(
                [t[2] for t in triples], allow_partial=self._partial.isChecked()
            )
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "自動整列", str(e))
            return
        if all(t[2].split_meta for t in triples):
            triples.sort(key=lambda t: int(t[2].split_meta["page_offset"]))
        self._list.clear()
        for jpath, label, _inp in triples:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, jpath)
            self._list.addItem(item)
        if warnings:
            QMessageBox.information(self, "自動整列", "\n".join(warnings))

    def _choose_out(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "統合PDFの保存先", self._out_edit.text() or self._start_dir,
            "PDF Files (*.pdf)",
        )
        if path:
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            self._out_edit.setText(path)

    # ----------------------------------------------------------- run
    def _run(self) -> None:
        paths = self._paths()
        if len(paths) < 2:
            QMessageBox.warning(self, "マージ", "2つ以上のチャンクを追加してください。")
            return
        out = self._out_edit.text().strip()
        if not out:
            QMessageBox.warning(self, "マージ", "出力PDFを指定してください。")
            return
        try:
            inputs = [M.load_merge_input(p) for p in paths]
            res = M.merge_documents(
                inputs, out, allow_partial=self._partial.isChecked()
            )
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "マージ", f"マージに失敗しました:\n{e}")
            return
        self.merged_pdf = res.out_pdf
        msg = (
            f"統合しました:\n{res.out_pdf}\n\n"
            f"総ページ: {res.total_pages}\n"
            f"結果あり: {res.total_results} ページ / 確認済み: {res.reviewed_count} ページ"
        )
        if res.warnings:
            msg += "\n\n⚠ 警告:\n" + "\n".join(f"・{w}" for w in res.warnings)
        QMessageBox.information(self, "マージ完了", msg)
        self.accept()
