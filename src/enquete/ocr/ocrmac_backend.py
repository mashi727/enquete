"""Apple Vision (ocrmac 相当) による OCR バックエンド。macOS 専用。

pyobjc-framework-vision を直接用い、行テキストと行内部分文字列の矩形
(VNRecognizedText.boundingBoxForRange:)を取得する。Vision の矩形は正規化・
左下原点なので、本アプリの左上原点 Rect に変換して返す。
"""
from __future__ import annotations

import sys
from collections.abc import Sequence

from enquete.ocr.base import OcrLine
from enquete.schema import Rect


def _cgrect_to_topleft(rect) -> Rect:
    """Vision の正規化 CGRect(左下原点) を 左上原点 Rect(x0,y0,x1,y1) に変換。"""
    x, y = rect.origin.x, rect.origin.y
    w, h = rect.size.width, rect.size.height
    return (x, 1.0 - (y + h), x + w, 1.0 - y)


class OcrmacBackend:
    name = "Apple Vision (ocrmac)"

    def available(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import Vision  # noqa: F401
        except ImportError:
            return False
        return True

    def recognize_lines(
        self, image_data: bytes, languages: Sequence[str] = ("ja-JP",)
    ) -> list[OcrLine]:
        import Vision
        from Foundation import NSData

        data = NSData.dataWithBytes_length_(image_data, len(image_data))
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(0)  # 0=accurate
        req.setUsesLanguageCorrection_(True)
        if languages:
            req.setRecognitionLanguages_(list(languages))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
        ok = handler.performRequests_error_([req], None)
        if isinstance(ok, tuple):
            ok = ok[0]
        if not ok:
            return []

        lines: list[OcrLine] = []
        for obs in req.results():
            candidate = obs.topCandidates_(1)
            if not candidate:
                continue
            cand = candidate[0]
            text = cand.string()
            line_box = _cgrect_to_topleft(obs.boundingBox())

            # cand を捕捉して部分文字列矩形を遅延解決(Python参照で生存維持)
            def make_resolver(c):
                def resolve(start: int, length: int) -> Rect | None:
                    result = c.boundingBoxForRange_error_((start, length), None)
                    rect_obs = result[0] if isinstance(result, tuple) else result
                    if rect_obs is None:
                        return None
                    return _cgrect_to_topleft(rect_obs.boundingBox())

                return resolve

            lines.append(
                OcrLine(
                    text=text,
                    bbox=line_box,
                    confidence=float(obs.confidence()),
                    range_resolver=make_resolver(cand),
                )
            )
        return lines
