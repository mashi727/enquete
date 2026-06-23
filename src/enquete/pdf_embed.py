"""PDF へアンケート結果・原紙基準画像を埋め込み/抽出する(QPDF/pikepdf)。

サイドカー JSON を廃し、記入済み PDF 自身に自己完結で結果を保持する。添付名は固定:

- ``enquete-data.json``  … {schemaVersion, survey(原紙由来のフォーム定義), pages(結果)}
- ``enquete-genshi.png`` … 差分検出の基準(原紙)画像。再電子化を自己完結にするため任意で同梱。

保存は temp → atomic replace のフル書き出し(ロスレス・本文は再ラスタライズしない)。
インクリメンタル追記ではなくフル書き出しなのでファイルは肥大しない。初回保存時は
``<stem>.bak.pdf`` へ原本を退避する。

依存はライセンス方針内(pikepdf = QPDF/MPL-2.0・再配布可)。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pikepdf

DATA_NAME = "enquete-data.json"
BASELINE_NAME = "enquete-genshi.png"
SCHEMA_VERSION = 1


def read_data(pdf_path: str | Path) -> dict | None:
    """埋め込み結果データ(dict)を返す。無ければ/壊れていれば None。"""
    try:
        with pikepdf.open(str(pdf_path)) as pdf:
            if DATA_NAME not in pdf.attachments:
                return None
            raw = pdf.attachments[DATA_NAME].get_file().read_bytes()
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001  破損・非対応は「無し」扱い
        return None


def has_embedded_data(pdf_path: str | Path) -> bool:
    """結果データが埋め込まれているか。"""
    try:
        with pikepdf.open(str(pdf_path)) as pdf:
            return DATA_NAME in pdf.attachments
    except Exception:  # noqa: BLE001
        return False


def read_baseline(pdf_path: str | Path) -> bytes | None:
    """埋め込み原紙基準画像(PNG bytes)を返す。無ければ None。"""
    try:
        with pikepdf.open(str(pdf_path)) as pdf:
            if BASELINE_NAME not in pdf.attachments:
                return None
            return pdf.attachments[BASELINE_NAME].get_file().read_bytes()
    except Exception:  # noqa: BLE001
        return None


def write_data(
    pdf_path: str | Path,
    data: dict,
    baseline_png: bytes | None = None,
    *,
    backup: bool = True,
) -> Path:
    """結果データ(と任意で基準画像)を PDF へ埋め込み、ロスレスに上書きする。

    baseline_png=None のときは既存の基準画像を保持する(指定時のみ置換)。一時ファイルへ
    フル書き出ししてから atomic replace。初回のみ原本を ``<stem>.bak.pdf`` へ退避する。
    戻り値は書き出した PDF のパス。
    """
    src = Path(pdf_path)
    payload = dict(data)
    payload.setdefault("schemaVersion", SCHEMA_VERSION)
    json_bytes = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    tmp = src.with_name(f"{src.stem}.embed_tmp.pdf")
    bak = src.with_name(f"{src.stem}.bak.pdf")
    try:
        with pikepdf.open(str(src)) as pdf:
            pdf.attachments[DATA_NAME] = pikepdf.AttachedFileSpec(
                pdf, json_bytes, mime_type="application/json"
            )
            if baseline_png is not None:
                pdf.attachments[BASELINE_NAME] = pikepdf.AttachedFileSpec(
                    pdf, baseline_png, mime_type="image/png"
                )
            pdf.save(str(tmp))  # フル書き出し(未参照の旧添付は引き継がれない=肥大しない)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if backup and not bak.exists():
        shutil.copy2(src, bak)
    os.replace(tmp, src)
    return src
