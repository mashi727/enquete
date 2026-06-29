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

# PDF への結果JSON・原紙基準画像の埋め込み/抽出(pikepdf=QPDF。ネイティブ libqpdf を含む)
_collect("pikepdf", "lxml")

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

pyz = PYZ(a.pure)

_exe_common = dict(
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
        strip=False,
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
                "CFBundleShortVersionString": "0.1.22",
                "CFBundleVersion": "0.1.22",
                "NSHighResolutionCapable": True,
                "LSMinimumSystemVersion": "12.0",
            },
        )
