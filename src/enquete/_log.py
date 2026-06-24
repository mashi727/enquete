"""実行ログ。配布版(PyInstaller・console=False)は標準エラーが捨てられ、
描画中の例外や Qt 警告が画面にも出ないため、原因調査ができない。
ここでファイルへのログ・未捕捉例外フック・Qt メッセージハンドラを設定し、
「真っ白」のような不可視の失敗でも原因(例外/Qt 警告)を残せるようにする。

ログの場所:
  - Windows: %LOCALAPPDATA%\\enquete\\enquete.log
  - macOS:   ~/Library/Logs/enquete/enquete.log
  - その他:   ~/.enquete/enquete.log

環境変数 ENQUETE_LOG でパスを上書きできる。ENQUETE_DEBUG=1 で DEBUG 出力。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("enquete")

_configured = False


def log_path() -> Path:
    """OS ごとの既定ログファイルパス(環境変数 ENQUETE_LOG で上書き可)。"""
    override = os.environ.get("ENQUETE_LOG")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "enquete"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / "enquete"
    else:
        base = Path.home() / ".enquete"
    return base / "enquete.log"


def _install_excepthook() -> None:
    """未捕捉例外をログに残す(GUI ビルドでは標準エラーが見えないため)。"""
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        log.error("未捕捉例外", exc_info=(exc_type, exc, tb))
        try:
            prev(exc_type, exc, tb)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


def _install_qt_handler() -> None:
    """Qt の内部メッセージ(警告/重大)をログへ転送する。

    白画面の典型原因(例: "QPainter::begin: Paint device returned engine == 0"、
    画像フォーマット非対応、メモリ確保失敗)は Qt 警告として出るが、
    GUI ビルドでは捨てられる。ここで拾ってファイルに残す。
    """
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # noqa: BLE001  Qt 未ロードなら何もしない
        return

    level_of = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message):  # noqa: ANN001
        logging.getLogger("enquete.qt").log(level_of.get(mode, logging.INFO), "%s", message)

    qInstallMessageHandler(handler)


def setup_logging() -> Path | None:
    """ログ出力を初期化する。設定したログファイルのパスを返す(失敗時 None)。

    アプリ起動の最初期に一度だけ呼ぶ。多重呼び出しは無視する。
    """
    global _configured
    if _configured:
        return log_path()

    level = logging.DEBUG if os.environ.get("ENQUETE_DEBUG") else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as exc:  # noqa: BLE001  書き込めない環境でも起動は止めない
        path = None
        print(f"[enquete] ログファイルを開けません: {exc}", file=sys.stderr)

    # 標準エラーが生きている環境(ソース起動・console ビルド)では端末にも出す。
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _install_excepthook()
    _install_qt_handler()
    _configured = True

    log.info("enquete 起動 platform=%s frozen=%s", sys.platform, getattr(sys, "frozen", False))
    return path
