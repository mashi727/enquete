# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — enquete (Windows / macOS 共通)。

ビルド:  pyinstaller enquete.spec --noconfirm
  - 実行時に読み込む .ui を data として同梱する。
  - pypdfium2 のネイティブライブラリ(libpdfium)を確実に収集する。
  - macOS: Apple Vision OCR(ocrmac + pyobjc)を収集し .app を生成。
  - Windows: Windows 標準 OCR(winsdk)があれば収集。
  - screen-ai(locro)とそのモデル、PyMuPDF(AGPL)は **同梱しない**
    (非再配布 / ライセンス方針)。
  - 配布サイズ削減: QtWidgets アプリで不要な PySide6 モジュール(QML/Quick・
    QtPdf・QtNetwork・QtOpenGL バインディング等)・プラグイン・翻訳(ja/en 以外)を
    収集後に除去し、mac/Linux ではシンボルを strip する(Linux 実測 -約14%)。
"""
import os
import sys

from PyInstaller.utils.hooks import collect_all

# 実行時リソース: QUiLoader が読む .ui と、ウィンドウ/Dock 用アイコン(同 ui/)。
datas = [
    ("src/enquete/ui/main_window.ui", "enquete/ui"),
    ("src/enquete/ui/icon.png", "enquete/ui"),
]
binaries = []
hiddenimports = []


def _collect(*packages):
    """指定パッケージの datas/binaries/hiddenimports を収集して足し込む。"""
    for pkg in packages:
        try:
            d, b, h = collect_all(pkg)
            datas.extend(d)
            binaries.extend(b)
            hiddenimports.extend(h)
        except Exception as exc:  # noqa: BLE001  未導入パッケージはスキップ
            print(f"[enquete.spec] collect_all({pkg!r}) skipped: {exc}")


# PDF レンダリング/オーサリング(ネイティブ libpdfium を含む)
_collect("pypdfium2", "pypdfium2_raw")

if sys.platform == "darwin":
    # Apple Vision OCR(ocrmac は pyobjc 経由で Vision/Quartz/AppKit を使う)
    _collect("ocrmac", "Vision", "Quartz", "AppKit", "Foundation", "CoreFoundation", "objc")
elif sys.platform == "win32":
    # Windows 標準 OCR(任意。未導入なら編集モードのみで起動)
    _collect("winsdk")

# locro(MIT・screen-ai ラッパ)は同梱する。外部依存は PIL のみで軽量。
# 実行時に Chrome/Dropbox/サーバから DLL・モデル(非再配布)を取得して使う。
# cli.py だけが typer を使う(本アプリは未使用)ので、それは除外する。
hiddenimports += [
    "locro",
    "locro._dll",
    "locro._download",
    "locro._platform",
    "locro._protobuf",
    "locro.models",
    "locro.ocr",
]

# 同梱しない/不要な重量級パッケージ
excludes = [
    "locro.cli",      # typer 依存の CLI(本アプリは未使用)
    "typer",
    "fitz",           # PyMuPDF
    "pymupdf",        # PyMuPDF(AGPL)
    "tkinter",
    "test",
    "unittest",
    # --- PySide6: QtWidgets アプリでは使わないバインディングを除外 ---
    # (.abi3.so バインディングを落とす。例: QtOpenGL は 8.5MB と巨大だが未使用)
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.QtQmlModels",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",          # 8.5MB バインディング・本アプリ未使用
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",             # PDF は pypdfium2 を使用
    "PySide6.QtPdfWidgets",
    "PySide6.QtNetwork",         # ネットワーク機能は未使用
    "PySide6.QtNetworkAuth",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtHelp",
    "PySide6.QtDesigner",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtConcurrent",
    "PySide6.QtHttpServer",
]

# 実行ファイルのアイコン(Windows は .ico、macOS は後段の BUNDLE で .icns)
exe_icon = "assets/icon.ico" if sys.platform == "win32" else None

a = Analysis(
    ["src/enquete/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# 収集後のプルーニング: excludes だけでは落ちない Qt 共有ライブラリ・プラグイン・
# 翻訳ファイルや、opencv の未使用コーデックを TOC から除去してサイズを削る。
# ここで落とすのは「QtWidgets アプリ + pypdfium2 + screen-ai」に不要なものだけ。
# ---------------------------------------------------------------------------

# 落とす Qt 共有ライブラリ名の部分一致(libQt6<Name> / Qt6<Name>)。
# QML/Quick・PDF・Network・VirtualKeyboard・Wayland 等は本アプリでは不要。
_QT_LIB_DROP = (
    "Quick", "Qml", "Pdf", "Network", "VirtualKeyboard",
    "Wayland", "WlShell", "Sql", "Test", "Designer", "Svg",
    "Multimedia", "SpatialAudio", "Bluetooth", "Nfc", "Charts",
    "DataVisualization", "Graphs", "3D", "WebEngine", "WebChannel",
    "WebSockets", "Sensors", "SerialPort", "SerialBus", "Positioning",
    "Location", "RemoteObjects", "Scxml", "StateMachine", "TextToSpeech",
    "Help", "NetworkAuth", "HttpServer", "Concurrent",
)

# 落とす Qt プラグインのサブディレクトリ。日本語入力(platforminputcontexts)・
# 画像(imageformats)・プラットフォーム(platforms)・テーマ(platformthemes)・
# スタイル(styles)は残す。
_QT_PLUGIN_DROP = (
    "plugins/qmltooling", "plugins/wayland-shell-integration",
    "plugins/wayland-graphics-integration-client",
    "plugins/wayland-decoration-client", "plugins/egldeviceintegrations",
    "plugins/virtualkeyboard", "plugins/sqldrivers", "plugins/multimedia",
    "plugins/assetimporters", "plugins/sceneparsers", "plugins/renderers",
    "plugins/renderplugins", "plugins/geometryloaders", "plugins/texttospeech",
    "plugins/position", "plugins/sensors", "plugins/webview",
    # QtNetwork を除外したため、それに依存する以下も不要(読み込めない)。
    "plugins/tls", "plugins/networkinformation",
)

# opencv: cv2.abi3.so は ffmpeg(libavcodec/format/util/swscale)+libaom+libvpx 等を
# 直接/間接に必須リンクしており、いずれも除去すると `import cv2` が失敗する。
# 本アプリは動画機能を使わないが、これらは安全には外せないため温存する。
_OPENCV_LIB_DROP: tuple[str, ...] = ()


def _qt_translation_keep(dest: str) -> bool:
    """Qt 翻訳(.qm)は日本語/英語のみ残す(他言語は約 6MB を占めるため除去)。"""
    name = os.path.basename(dest)
    return name.endswith(("_ja.qm", "_en.qm"))


def _should_drop(dest: str) -> bool:
    d = dest.replace(os.sep, "/")
    # Qt 翻訳: ja/en 以外を除去
    if "/Qt/translations/" in d or "/translations/" in d and d.endswith(".qm"):
        return not _qt_translation_keep(d)
    # Qt プラグイン: 不要サブディレクトリを除去
    if any(p in d for p in _QT_PLUGIN_DROP):
        return True
    # Qt 共有ライブラリ: 不要モジュールを除去
    base = os.path.basename(d)
    if base.startswith(("libQt6", "Qt6")) or ".abi3.so" in base or base.startswith("Qt") and base.endswith(".dll"):
        for token in _QT_LIB_DROP:
            if f"Qt6{token}" in base or f"Qt{token}." in base:
                return True
    # opencv: 未使用コーデック
    if any(lib in base for lib in _OPENCV_LIB_DROP):
        return True
    return False


_before = (len(a.binaries), len(a.datas))
a.binaries = TOC([e for e in a.binaries if not _should_drop(e[0])])
a.datas = TOC([e for e in a.datas if not _should_drop(e[0])])
print(
    f"[enquete.spec] pruned binaries {_before[0]}->{len(a.binaries)}, "
    f"datas {_before[1]}->{len(a.datas)}"
)

pyz = PYZ(a.pure)

_exe_common = dict(
    name="enquete",
    debug=False,
    bootloader_ignore_signals=False,
    strip=(sys.platform != "win32"),  # mac/Linux はシンボルを strip してサイズ削減
    upx=False,
    console=False,  # GUI アプリ(コンソール窓を出さない)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=exe_icon,
)

if sys.platform == "win32":
    # Windows: onefile。単一の enquete.exe に全依存を埋め込む(ダブルクリックで起動)。
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        exclude_binaries=False,
        runtime_tmpdir=None,
        **_exe_common,
    )
else:
    # macOS: onedir + .app バンドル。
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_common)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=(sys.platform != "win32"),
        upx=False,
        upx_exclude=[],
        name="enquete",
    )
    if sys.platform == "darwin":
        app = BUNDLE(
            coll,
            name="enquete.app",
            icon="assets/icon.icns",
            bundle_identifier="com.LeonOrchestra.enquete",
            info_plist={
                "CFBundleName": "enquete",
                "CFBundleDisplayName": "enquete",
                "CFBundleShortVersionString": "0.1.6",
                "CFBundleVersion": "0.1.6",
                "NSHighResolutionCapable": True,
                "LSMinimumSystemVersion": "12.0",
            },
        )
