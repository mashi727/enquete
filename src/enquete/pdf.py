"""PDF レンダリング。ライセンス方針により pypdfium2(BSD) のみを使用する
(PyMuPDF / OpenCV は不可)。pypdfium2 のビットマップを QImage に直接変換し、
Pillow / numpy への依存も避ける。
"""
from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
from PySide6.QtGui import QImage


class PdfDocument:
    """1 つの PDF を保持し、ページを QImage にレンダリングする。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._doc = pdfium.PdfDocument(str(self.path))

    def __len__(self) -> int:
        return len(self._doc)

    def page_size(self, index: int) -> tuple[float, float]:
        """ページ index の (幅, 高さ) をポイント単位(72dpi 基準)で返す。"""
        page = self._doc[index]
        try:
            size = page.get_size()
            return float(size[0]), float(size[1])
        finally:
            page.close()

    def render(self, index: int, scale: float = 1.0) -> QImage:
        """ページ index を倍率 scale で描画して QImage を返す。

        scale=1.0 で 72dpi 相当。サムネイルは小さめ、本表示は大きめの
        scale を指定する。
        """
        page = self._doc[index]
        try:
            bitmap = page.render(scale=scale, rev_byteorder=True)
            try:
                fmt = (
                    QImage.Format.Format_RGBA8888
                    if bitmap.n_channels == 4
                    else QImage.Format.Format_RGB888
                )
                # bytes() でバッファを複製し、.copy() で QImage を独立させる
                # (元のビットマップ破棄後も安全に使えるようにするため)。
                return QImage(
                    bytes(bitmap.buffer),
                    bitmap.width,
                    bitmap.height,
                    bitmap.stride,
                    fmt,
                ).copy()
            finally:
                bitmap.close()
        finally:
            page.close()

    def close(self) -> None:
        self._doc.close()
