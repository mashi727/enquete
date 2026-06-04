"""エントリポイント:  uv run python -m enquete"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from enquete.main_window import DEFAULT_FORM_FONT_PT, MainWindowController, load_main_window

# ウィンドウ/Dock/タスクバー用アイコン(.ui と同じ ui/ に同梱)
ICON_PATH = Path(__file__).resolve().parent / "ui" / "icon.png"


def main() -> int:
    app = QApplication(sys.argv)
    # QSettings の保存先キー(macOS: ~/Library/Preferences/com.LeonOrchestra.enquete.plist)
    app.setOrganizationName("LeonOrchestra")
    app.setOrganizationDomain("leon-orchestra.example")
    app.setApplicationName("enquete")
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    # アプリ全体(メニュー・ツールバー・ダイアログ等)の既定フォントを 14pt に統一
    font = app.font()
    font.setPointSize(DEFAULT_FORM_FONT_PT)
    app.setFont(font)

    window = load_main_window()
    # コントローラはシグナル受信のため寿命を保つ必要がある
    controller = MainWindowController(window)
    # 保存済みのウィンドウ状態(サイズ/分割比/フィットモード)を復元してから表示
    controller.restore_window_state()
    # 終了時にウィンドウ状態を保存(破棄前に一度だけ発火)
    app.aboutToQuit.connect(controller.save_window_state)
    window.show()
    return app.exec()


if __name__ == "__main__":
    # 状態保存は aboutToQuit(app.exec 内)で完了済み。終了後の破棄フェーズで
    # PySide6＋ネイティブ拡張(Vision/OpenCV 等)の解放順序により間欠的に
    # segfault(exit 139)するため、破棄を踏まず即時終了する。
    rc = main()
    # GUI(windowed)ビルドでは sys.stdout/stderr が None のことがあるため安全に flush。
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None:
            try:
                _stream.flush()
            except Exception:  # noqa: BLE001  終了直前なので失敗は無視
                pass
    os._exit(rc)
