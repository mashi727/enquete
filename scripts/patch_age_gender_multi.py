#!/usr/bin/env python3
"""電子化済みPDFの「年齢」「性別」設問を 単一選択→複数選択 に変換するパッチ。

お子様連れ等で1枚のアンケートにお子様とご自身など複数の年齢・性別がマークされる
ケースに対応する。当該設問だけ複数選択へ変え、全ページを再検出して「閾値を超えた
すべての□」を拾い直し、PDFへ埋め込み直す(他設問の結果・確認フラグは保持)。

使い方:
  uv run python scripts/patch_age_gender_multi.py <PDF または ディレクトリ> [オプション]

オプション:
  --labels 年齢 性別     対象とする設問ラベル(部分一致)。既定: 年齢 性別
  --scale 3.0            レンダリング倍率(埋め込み基準画像と同じ既定3.0)
  --dry-run             変更内容を表示するだけで書き込まない
  --no-backup           パッチ前の <名前>.prepatch.pdf を作らない

電子化済み(埋め込みあり)PDFのみが対象。埋め込みの無いPDFはスキップする。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QByteArray, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from enquete import pdf_embed  # noqa: E402
from enquete.detect import detect_checkboxes, qimage_to_gray  # noqa: E402
from enquete.pdf import PdfDocument  # noqa: E402
from enquete.schema import (  # noqa: E402
    MULTI_CHOICE,
    SINGLE_CHOICE,
    survey_from_dict,
    survey_to_dict,
)


def _clean_gray(baseline_png: bytes, doc: PdfDocument, scale: float):
    """埋め込み基準画像(PNG)をページ0のレンダリング寸法へ合わせた clean gray。"""
    base = QImage()
    base.loadFromData(QByteArray(baseline_png), "PNG")
    if base.isNull():
        return None
    ref = doc.render(0, scale=scale)
    base = base.scaled(
        ref.width(), ref.height(),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return qimage_to_gray(base)


def patch_pdf(path: Path, labels: list[str], scale: float, dry_run: bool,
              backup: bool) -> bool:
    data = pdf_embed.read_data(path)
    if not data or not data.get("survey"):
        print(f"  - スキップ(埋め込みなし): {path.name}")
        return False
    survey = survey_from_dict(data["survey"])
    targets = [
        q for q in survey.questions
        if q.type == SINGLE_CHOICE and any(lbl in q.label for lbl in labels)
    ]
    if not targets:
        print(f"  - 対象設問なし: {path.name}")
        return False

    baseline = pdf_embed.read_baseline(path)
    pages = {str(k): dict(v) for k, v in (data.get("pages") or {}).items()}
    doc = PdfDocument(path)
    clean = _clean_gray(baseline, doc, scale) if baseline else None

    for q in targets:
        q.type = MULTI_CHOICE

    n_changed = 0
    for i in range(len(doc)):
        gray = qimage_to_gray(doc.render(i, scale=scale))
        use_clean = clean if (clean is not None and clean.shape == gray.shape) else None
        det = detect_checkboxes(survey, gray, use_clean)
        rec = pages.setdefault(str(i), {})
        res = dict(rec.get("results") or {})
        for q in targets:
            old = res.get(q.id)
            r = det.get(q.id)
            new = list(r.checked) if r is not None else []
            # 旧(単一の文字列 or None)→新(リスト)。値が変われば数える。
            old_set = set(old) if isinstance(old, list) else ({old} if old else set())
            if set(new) != old_set:
                n_changed += 1
            res[q.id] = new
        rec["results"] = res
    npages = len(doc)
    doc.close()

    names = "・".join(q.label for q in targets)
    print(f"  ✓ {path.name}: 「{names}」を複数選択化 / {npages}ページ再検出"
          f"(値が変化: {n_changed}ページ)")
    if dry_run:
        return True

    if backup:
        bak = path.with_name(f"{path.stem}.prepatch.pdf")
        if not bak.exists():
            shutil.copy2(path, bak)
    out = {
        "source_pdf": path.name,
        "survey": survey_to_dict(survey),
        "pages": pages,
    }
    pdf_embed.write_data(path, out, baseline_png=baseline, backup=False)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="PDF ファイル または ディレクトリ")
    ap.add_argument("--labels", nargs="+", default=["年齢", "性別"])
    ap.add_argument("--scale", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    target = Path(args.target)
    if target.is_dir():
        pdfs = sorted(p for p in target.glob("*.pdf")
                      if not p.name.endswith((".bak.pdf", ".prepatch.pdf")))
    elif target.is_file():
        pdfs = [target]
    else:
        print(f"見つかりません: {target}")
        return 1
    if not pdfs:
        print("対象PDFがありません。")
        return 1

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_PluginApplication, False)
    _app = QApplication.instance() or QApplication([])

    print(f"対象ラベル: {args.labels} / {'(ドライラン)' if args.dry_run else '書込み'}")
    done = 0
    for p in pdfs:
        if patch_pdf(p, args.labels, args.scale, args.dry_run, not args.no_backup):
            done += 1
    print(f"完了: {done}/{len(pdfs)} 件を処理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
