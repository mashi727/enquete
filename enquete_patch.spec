# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 年齢・性別 複数選択化パッチ(単体・ドラッグ&ドロップGUI)。

本体(enquete)とは別の小さな実行ファイルを生成する。検出(画素解析)のみで OCR は
使わないため、ocrmac/Vision/locro/winsdk 等は同梱しない(本体より軽量)。
"""
import sys

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []


def _collect(*packages):
    for pkg in packages:
        try:
            d, b, h = collect_all(pkg)
            datas.extend(d)
            binaries.extend(b)
            hiddenimports.extend(h)
        except Exception as exc:  # noqa: BLE001
            print(f"[enquete_patch.spec] collect_all({pkg!r}) skipped: {exc}")


# PDF レンダリング(libpdfium)と 埋め込み/抽出(pikepdf=QPDF/libqpdf)。
_collect("pypdfium2", "pypdfium2_raw")
_collect("pikepdf", "lxml")

# OCR・screen-ai・PyMuPDF は不要(検出は画素解析のみ)。重量級は除外。
excludes = [
    "ocrmac", "Vision", "Quartz", "AppKit", "objc",
    "locro", "winsdk", "typer",
    "fitz", "pymupdf", "tkinter", "test", "unittest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia", "PySide6.QtBluetooth",
]

exe_icon = "assets/icon.ico" if sys.platform == "win32" else None

a = Analysis(
    ["src/enquete/patch_app.py"],
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
    name="enquete-patch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=exe_icon,
)

if sys.platform == "win32":
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        exclude_binaries=False, runtime_tmpdir=None, **_exe_common,
    )
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_common)
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False, upx=False, upx_exclude=[], name="enquete-patch",
    )
    if sys.platform == "darwin":
        app = BUNDLE(
            coll,
            name="enquete-patch.app",
            icon="assets/icon.icns",
            bundle_identifier="com.LeonOrchestra.enquete.patch",
            info_plist={
                "CFBundleName": "enquete-patch",
                "CFBundleDisplayName": "年齢・性別パッチ",
                "CFBundleShortVersionString": "0.1.25",
                "CFBundleVersion": "0.1.25",
                "NSHighResolutionCapable": True,
                "LSMinimumSystemVersion": "12.0",
            },
        )
