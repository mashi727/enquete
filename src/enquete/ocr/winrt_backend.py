"""Windows 標準 OCR(Windows.Media.Ocr) バックエンド。Windows 専用。

winsdk(WinRT の Python 射影) 経由で OS 標準の OCR エンジンを使う。OS標準・無料・
再配布の問題なし(Chrome や Google ライブラリに非依存)。日本語は Windows の
「日本語(OCR)」言語機能が入っていれば利用可能。

WinRT の bounding_rect は ピクセル・左上原点。本アプリの正規化(0..1)左上原点
Rect に変換。行box は語box群の和集合、box_for_range も語box和集合で近似する。

注意: このバックエンドは Windows 実機での動作検証が未了(開発機は macOS のため
available() は False を返し実行されない)。
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from enquete.ocr.base import OcrLine
from enquete.schema import Rect


def _make_engine(languages: Sequence[str]):
    """OCRエンジンを生成。指定言語(既定 ja)を優先、無ければユーザー既定言語。"""
    from winsdk.windows.globalization import Language  # type: ignore
    from winsdk.windows.media.ocr import OcrEngine  # type: ignore

    for tag in list(languages) + ["ja", "ja-JP"]:
        try:
            lang = Language(tag)
            if OcrEngine.is_language_supported(lang):
                eng = OcrEngine.try_create_from_language(lang)
                if eng is not None:
                    return eng
        except Exception:
            continue
    return OcrEngine.try_create_from_user_profile_languages()


async def _recognize_async(png_bytes: bytes, languages: Sequence[str]):
    from winsdk.windows.graphics.imaging import BitmapDecoder  # type: ignore
    from winsdk.windows.storage.streams import (  # type: ignore
        DataWriter,
        InMemoryRandomAccessStream,
    )

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png_bytes)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    engine = _make_engine(languages)
    if engine is None:
        return None, 0, 0
    result = await engine.recognize_async(bmp)
    return result, int(bmp.pixel_width), int(bmp.pixel_height)


def _rect_union_norm(rects, w: float, h: float) -> Rect | None:
    if not rects or w <= 0 or h <= 0:
        return None
    x0 = min(r.x for r in rects) / w
    y0 = min(r.y for r in rects) / h
    x1 = max(r.x + r.width for r in rects) / w
    y1 = max(r.y + r.height for r in rects) / h
    return (x0, y0, x1, y1)


def _build_resolver(text: str, word_specs, w: float, h: float):
    """行内の語スパン(開始,終了,rect)から任意文字範囲の矩形を近似解決する。"""
    spans: list[tuple[int, int, object]] = []
    cursor = 0
    for wtext, rect in word_specs:
        idx = text.find(wtext, cursor) if wtext else -1
        if idx < 0:
            idx = cursor
        start, end = idx, idx + len(wtext)
        cursor = end
        spans.append((start, end, rect))

    def resolve(start: int, length: int) -> Rect | None:
        end = start + length
        rects = [r for (s, e, r) in spans if s < end and e > start]
        return _rect_union_norm(rects, w, h)

    return resolve


class WinRtOcrBackend:
    name = "Windows OCR (Windows.Media.Ocr)"

    def available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            from winsdk.windows.media.ocr import OcrEngine  # type: ignore
        except Exception:
            return False
        try:
            # 何らかの言語でエンジンが作れること(言語機能の有無)
            return _make_engine(("ja", "ja-JP")) is not None
        except Exception:
            return False

    def recognize_lines(
        self, image_data: bytes, languages: Sequence[str] = ("ja-JP",)
    ) -> list[OcrLine]:
        if sys.platform != "win32":
            return []
        try:
            result, w, h = asyncio.run(_recognize_async(image_data, languages))
        except Exception:
            return []
        if result is None or w <= 0 or h <= 0:
            return []
        out: list[OcrLine] = []
        for line in result.lines:
            words = list(line.words)
            specs = [(wd.text, wd.bounding_rect) for wd in words]
            bbox = _rect_union_norm([r for _t, r in specs], float(w), float(h))
            if bbox is None:
                continue
            out.append(
                OcrLine(
                    text=line.text,
                    bbox=bbox,
                    confidence=1.0,
                    range_resolver=_build_resolver(line.text, specs, float(w), float(h)),
                )
            )
        return out
