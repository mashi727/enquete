#!/usr/bin/env python3
"""電子化済みPDFの「年齢」「性別」設問を 単一選択→複数選択 にするパッチ(CLI)。

お子様連れ等で1枚に複数の年齢/性別がマークされたアンケート対応。中核は
enquete.age_gender_patch.patch_pdf。GUI(ドラッグ&ドロップ)版は enquete.patch_app。

使い方:
  uv run python scripts/patch_age_gender_multi.py <PDF または ディレクトリ> [オプション]
    --labels 年齢 性別   対象設問ラベル(部分一致)。既定: 年齢 性別
    --scale 3.0          レンダリング倍率(埋め込み基準画像と同じ既定3.0)
    --dry-run            変更内容を表示するだけで書き込まない
    --no-backup          パッチ前の <名前>.prepatch.pdf を作らない
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from enquete.age_gender_patch import collect_pdfs, patch_pdf  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="PDF ファイル または ディレクトリ")
    ap.add_argument("--labels", nargs="+", default=["年齢", "性別"])
    ap.add_argument("--scale", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    pdfs = collect_pdfs([args.target])
    if not pdfs:
        print(f"対象PDFがありません: {args.target}")
        return 1

    _app = QApplication.instance() or QApplication([])  # QImage/レンダリング用
    print(f"対象ラベル: {args.labels} / {'(ドライラン)' if args.dry_run else '書込み'}")
    done = 0
    for p in pdfs:
        r = patch_pdf(
            p, labels=args.labels, scale=args.scale,
            dry_run=args.dry_run, backup=not args.no_backup,
        )
        st = r["status"]
        if st in ("patched", "dry-run"):
            done += 1
            print(f"  ✓ {r['pdf']}: 「{'・'.join(r['labels'])}」を複数選択化 "
                  f"/ {r['pages']}ページ再検出（値が変化: {r['changed']}ページ）")
        elif st == "no-target":
            print(f"  - {r['pdf']}: 対象設問なし")
        else:
            print(f"  - {r['pdf']}: 埋め込みなし(スキップ)")
    print(f"完了: {done}/{len(pdfs)} 件を処理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
