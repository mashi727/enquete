"""OCRバックエンドの抽象。

プラットフォーム別実装(mac=ocrmac/Apple Vision、将来 Windows=ScreenAI など)を
差し替え可能にする。フォーム作成では行テキストに加え、行内の部分文字列(番号
マーカー等)の矩形が要るため、OcrLine は box_for_range を提供する。

座標はすべてページに対する正規化値(左上原点, 0..1)の Rect=(x0,y0,x1,y1)。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from enquete.schema import Rect


@dataclass
class OcrLine:
    text: str
    bbox: Rect  # 行全体(左上原点)
    confidence: float = 1.0
    # 部分文字列の矩形を返す解決関数(バックエンドが提供。無ければ None)
    range_resolver: Callable[[int, int], Rect | None] | None = field(
        default=None, repr=False
    )

    def box_for_range(self, start: int, length: int) -> Rect | None:
        """行内の [start, start+length) の文字範囲の矩形(左上原点)を返す。"""
        if self.range_resolver is None:
            return None
        return self.range_resolver(start, length)


@runtime_checkable
class OcrBackend(Protocol):
    """OCRバックエンドのインターフェース。"""

    name: str

    def available(self) -> bool:
        """この環境で利用可能か。"""
        ...

    def recognize_lines(
        self, image_data: bytes, languages: Sequence[str] = ("ja-JP",)
    ) -> list[OcrLine]:
        """画像(エンコード済みバイト列)を認識し、行のリストを返す。"""
        ...
