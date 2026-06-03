"""assets/icon.svg から各プラットフォーム用アイコンを生成する。

生成物:
  - assets/icon.png         1024px マスター(参照用)
  - assets/icon.icns        macOS アプリ用(iconutil・macOS でのみ生成可)
  - assets/icon.ico         Windows アプリ用(複数サイズ埋め込み)
  - src/enquete/ui/icon.png 実行時のウィンドウ/Dock アイコン(512px)

実行:  QT_QPA_PLATFORM=offscreen uv run python scripts/generate_icons.py

SVG のラスタライズは PySide6(QtSvg)、.ico 生成は Pillow を使う。
.icns は macOS の iconutil に依存する(他OSではスキップ)。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "assets" / "icon.svg"


def main() -> int:
    renderer = QSvgRenderer(QByteArray(SVG.read_bytes()))
    if not renderer.isValid():
        print(f"invalid SVG: {SVG}", file=sys.stderr)
        return 1

    def render(size: int) -> QImage:
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        renderer.render(p)
        p.end()
        return img

    def save_png(size: int, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        render(size).save(str(path), "PNG")

    # マスター PNG と実行時アイコン
    save_png(1024, ROOT / "assets" / "icon.png")
    save_png(512, ROOT / "src" / "enquete" / "ui" / "icon.png")

    # Windows .ico(Pillow)
    try:
        from PIL import Image
    except ImportError:
        print("Pillow 未導入のため .ico はスキップ", file=sys.stderr)
    else:
        with tempfile.TemporaryDirectory() as td:
            base_png = Path(td) / "ico_256.png"
            save_png(256, base_png)
            sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
            Image.open(base_png).convert("RGBA").save(
                ROOT / "assets" / "icon.ico", format="ICO", sizes=sizes
            )
        print("wrote assets/icon.ico")

    # macOS .icns(iconutil)
    if sys.platform == "darwin":
        specs = [
            (16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
            (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
            (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x"),
        ]
        with tempfile.TemporaryDirectory(suffix=".iconset") as iconset:
            for px, name in specs:
                save_png(px, Path(iconset) / f"icon_{name}.png")
            rc = subprocess.run(
                ["iconutil", "-c", "icns", iconset, "-o", str(ROOT / "assets" / "icon.icns")]
            ).returncode
            print(f"iconutil rc={rc}")
    else:
        print("macOS 以外のため .icns はスキップ(既存ファイルを使用)", file=sys.stderr)

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
