"""年齢・性別 複数選択化パッチ — ドラッグ＆ドロップ GUI(単体配布用)。

校正作業中の電子化済みPDF(または複数ファイル/フォルダ)をウィンドウへドラッグ＆ドロップ
すると、「年齢」「性別」設問を 単一選択→複数選択 に変換し再検出してその場で保存する。
お子様連れ等で1枚に複数の年齢/性別がマークされたアンケートに対応するためのもの。

エントリポイント: python -m enquete.patch_app / PyInstaller(enquete_patch.spec)。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from enquete.age_gender_patch import DEFAULT_LABELS, collect_pdfs, patch_pdf

_DROP_STYLE = (
    "QLabel { border: 2px dashed #888; border-radius: 8px; padding: 28px;"
    " font-size: 15px; color: #333; background: #f5f7fa; }"
)


class PatchWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("年齢・性別 複数選択化パッチ")
        self.resize(640, 460)
        self.setAcceptDrops(True)

        self._drop = QLabel(
            "ここに電子化済みPDF（またはフォルダ）をドラッグ＆ドロップ\n\n"
            "「年齢」「性別」を複数選択に変換し、その場で保存します。\n"
            "（変更前は <名前>.prepatch.pdf に退避します）"
        )
        self._drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop.setStyleSheet(_DROP_STYLE)
        self._drop.setWordWrap(True)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._status = QLabel("待機中…")

        lay = QVBoxLayout(self)
        lay.addWidget(self._drop)
        lay.addWidget(QLabel("処理ログ:"))
        lay.addWidget(self._log, 1)
        lay.addWidget(self._status)

        self._busy = False

    # ------------------------------------------------------------- ドロップ
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self._busy and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        if self._busy:
            return
        paths = [
            url.toLocalFile() for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        pdfs = collect_pdfs(paths)
        if not pdfs:
            self._append("対象のPDFが見つかりませんでした（電子化済みPDFを入れてください）。")
            return
        self._run(pdfs)

    # --------------------------------------------------------------- 実行
    def _append(self, text: str) -> None:
        self._log.appendPlainText(text)
        self._log.ensureCursorVisible()

    def _run(self, pdfs: list[Path]) -> None:
        self._busy = True
        self._drop.setText("処理中… 完了までお待ちください")
        QApplication.processEvents()
        ok = changed = skipped = 0
        for n, pdf in enumerate(pdfs, 1):
            self._status.setText(f"{n}/{len(pdfs)}: {pdf.name} を処理中…")
            QApplication.processEvents()

            def _prog(i: int, total: int, name: str = pdf.name) -> None:
                self._status.setText(f"{name}: {i}/{total} ページ再検出中…")
                QApplication.processEvents()

            try:
                r = patch_pdf(pdf, labels=DEFAULT_LABELS, progress=_prog)
            except Exception as exc:  # noqa: BLE001
                self._append(f"✗ {pdf.name}: エラー {exc}")
                continue
            st = r.get("status")
            if st == "patched":
                ok += 1
                changed += r.get("changed", 0)
                self._append(
                    f"✓ {r['pdf']}: 「{'・'.join(r['labels'])}」を複数選択化 "
                    f"/ {r['pages']}ページ再検出（値が変化: {r['changed']}ページ）"
                )
            elif st == "no-target":
                skipped += 1
                self._append(f"- {r['pdf']}: 対象設問（年齢・性別）が見つかりません")
            else:  # skipped
                skipped += 1
                self._append(f"- {r['pdf']}: 電子化されていません（スキップ）")
        self._append(
            f"\n完了: {ok} 件にパッチ適用（うち値が変化 {changed} ページ）"
            f" / {skipped} 件スキップ\n"
            "── 変化したページは校正モードで年齢・性別の複数チェックをご確認ください。\n"
        )
        self._status.setText("完了。続けてドロップできます。")
        self._drop.setText(
            "ここに電子化済みPDF（またはフォルダ）をドラッグ＆ドロップ\n\n"
            "「年齢」「性別」を複数選択に変換し、その場で保存します。"
        )
        self._busy = False


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = PatchWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
