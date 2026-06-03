"""OCR バックエンドのパッケージ。

起動中の OS で使えるバックエンドだけを選択肢に出し、自動選択もその中から行う。
- macOS: Apple Vision(ocrmac) を優先 → Chrome screen-ai(locro)。
- Windows: Windows 標準OCR(Windows.Media.Ocr) を優先 → Chrome screen-ai。
- Linux: Chrome screen-ai。
Apple Vision は mac 専用、Windows OCR は Windows 専用、screen-ai は3OS共通。
"""
from __future__ import annotations

import sys

from enquete.ocr.base import OcrBackend, OcrLine
from enquete.ocr.ocrmac_backend import OcrmacBackend
from enquete.ocr.screenai_backend import ScreenAiBackend
from enquete.ocr.winrt_backend import WinRtOcrBackend

# OCR バックエンドの選好キー(QSettings `ocr/backend` に保存)。
BACKEND_AUTO = "auto"
BACKEND_OCRMAC = "ocrmac"
BACKEND_WINRT = "winrt"
BACKEND_SCREENAI = "screenai"

# (キー, 実装クラス, 対応OS)。前にあるものほど auto 選択で優先。
# OS は sys.platform 値("darwin"/"win32"/"linux")。
_REGISTRY: list[tuple[str, type, tuple[str, ...]]] = [
    (BACKEND_OCRMAC, OcrmacBackend, ("darwin",)),
    (BACKEND_WINRT, WinRtOcrBackend, ("win32",)),
    (BACKEND_SCREENAI, ScreenAiBackend, ("darwin", "win32", "linux")),
]

__all__ = [
    "BACKEND_AUTO",
    "BACKEND_OCRMAC",
    "BACKEND_SCREENAI",
    "BACKEND_WINRT",
    "OcrBackend",
    "OcrLine",
    "OcrmacBackend",
    "ScreenAiBackend",
    "WinRtOcrBackend",
    "available_backends",
    "backend_choices",
    "make_backend",
    "get_backend",
    "get_default_backend",
]


def _applicable() -> list[tuple[str, type]]:
    """現在の OS で使える (キー, クラス) を優先順で返す。"""
    here = sys.platform
    return [(key, cls) for key, cls, plats in _REGISTRY if here in plats]


def _candidates() -> list[OcrBackend]:
    """現在 OS で使えるバックエンド候補(優先順)。"""
    return [cls() for _key, cls in _applicable()]


def make_backend(key: str) -> OcrBackend | None:
    """キーに対応するバックエンドを生成。未知キーなら None(OS 非対応は問わない)。"""
    for k, cls, _plats in _REGISTRY:
        if k == key:
            return cls()
    return None


def available_backends() -> list[OcrBackend]:
    """この環境で利用可能なバックエンドを優先順で返す。"""
    return [b for b in _candidates() if b.available()]


def get_default_backend() -> OcrBackend | None:
    """利用可能な最優先バックエンドを返す。無ければ None。"""
    for backend in _candidates():
        if backend.available():
            return backend
    return None


def get_backend(preference: str = BACKEND_AUTO) -> OcrBackend | None:
    """選好に従いバックエンドを返す。

    - ``auto``: 利用可能な最優先バックエンド。
    - 明示キー: そのバックエンドが利用可能なら返す。不可なら None。
    無ければ None(呼び出し側で「編集モードのみ」案内に使う)。
    """
    if preference == BACKEND_AUTO:
        return get_default_backend()
    backend = make_backend(preference)
    if backend is not None and backend.available():
        return backend
    return None


def backend_choices() -> list[tuple[str, str, bool]]:
    """選択UI用の (key, ラベル, この環境で利用可能か) を優先順で返す。

    現在 OS に対応するバックエンドだけを列挙する(先頭は常に ``auto``)。
    """
    out: list[tuple[str, str, bool]] = [
        (BACKEND_AUTO, "自動（利用可能なものを使用）", True)
    ]
    for _key, cls in _applicable():
        backend = cls()
        out.append((_key, backend.name, backend.available()))
    return out
