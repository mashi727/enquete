# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — enquete (Windows / macOS 共通)。

ビルド:  pyinstaller enquete.spec --noconfirm
  - 実行時に読み込む .ui を data として同梱する。
  - pypdfium2 のネイティブライブラリ(libpdfium)を確実に収集する。
  - macOS: Apple Vision OCR(ocrmac + pyobjc)を収集し .app を生成。
  - Windows: Windows 標準 OCR(winsdk)があれば収集。
  - screen-ai(locro)とそのモデル、PyMuPDF(AGPL)は **同梱しない**
    (非再配布 / ライセンス方針)。
"""
import sys

from PyInstaller.utils.hooks import collect_all

# 実行時リソース: QUiLoader が Path(__file__).parent/"ui"/"main_window.ui" を読む。
datas = [("src/enquete/ui/main_window.ui", "enquete/ui")]
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

# 同梱しない/不要な重量級パッケージ
excludes = [
    "locro",          # screen-ai ラッパ(モデル非再配布)
    "fitz",           # PyMuPDF
    "pymupdf",        # PyMuPDF(AGPL)
    "tkinter",
    "test",
    "unittest",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtBluetooth",
]

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

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="enquete",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI アプリ(コンソール窓を出さない)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="enquete",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="enquete.app",
        icon=None,
        bundle_identifier="com.LeonOrchestra.enquete",
        info_plist={
            "CFBundleName": "enquete",
            "CFBundleDisplayName": "enquete",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )
