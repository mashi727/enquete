"""電子化済みPDFの「年齢」「性別」設問を 単一選択→複数選択 に変換する中核処理。

お子様連れ等で1枚に複数の年齢/性別がマークされたアンケートに対応するため、当該設問
だけ複数選択へ変え、全ページを再検出して「閾値を超えたすべての□」を拾い直し、PDFへ
埋め込み直す(他設問の結果・確認フラグは保持)。CLI(scripts/)とドラッグ&ドロップGUI
(patch_app)の両方から使う。Qt(QImage/レンダリング)を使うので QApplication が必要。
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QImage

from enquete import pdf_embed
from enquete.detect import detect_checkboxes, qimage_to_gray
from enquete.pdf import PdfDocument
from enquete.schema import (
    MULTI_CHOICE,
    SINGLE_CHOICE,
    survey_from_dict,
    survey_to_dict,
)

DEFAULT_LABELS = ["年齢", "性別"]


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


def patch_pdf(
    path: str | Path,
    labels: list[str] | None = None,
    scale: float = 3.0,
    dry_run: bool = False,
    backup: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """1つのPDFを処理する。戻り値は結果サマリ dict。

    status: "patched" / "dry-run" / "skipped"(埋め込みなし) / "no-target"(対象設問なし)
    """
    path = Path(path)
    labels = labels or DEFAULT_LABELS
    data = pdf_embed.read_data(path)
    if not data or not data.get("survey"):
        return {"status": "skipped", "pdf": path.name}
    survey = survey_from_dict(data["survey"])
    targets = [
        q for q in survey.questions
        if q.type == SINGLE_CHOICE and any(lbl in q.label for lbl in labels)
    ]
    if not targets:
        return {"status": "no-target", "pdf": path.name}

    baseline = pdf_embed.read_baseline(path)
    pages = {str(k): dict(v) for k, v in (data.get("pages") or {}).items()}
    doc = PdfDocument(path)
    clean = _clean_gray(baseline, doc, scale) if baseline else None
    for q in targets:
        q.type = MULTI_CHOICE

    n_changed = 0
    npages = len(doc)
    for i in range(npages):
        gray = qimage_to_gray(doc.render(i, scale=scale))
        use_clean = clean if (clean is not None and clean.shape == gray.shape) else None
        det = detect_checkboxes(survey, gray, use_clean)
        rec = pages.setdefault(str(i), {})
        res = dict(rec.get("results") or {})
        for q in targets:
            old = res.get(q.id)
            r = det.get(q.id)
            new = list(r.checked) if r is not None else []
            old_set = set(old) if isinstance(old, list) else ({old} if old else set())
            if set(new) != old_set:
                n_changed += 1
            res[q.id] = new
        rec["results"] = res
        if progress is not None:
            progress(i + 1, npages)
    doc.close()

    summary = {
        "status": "dry-run" if dry_run else "patched",
        "pdf": path.name,
        "labels": [q.label for q in targets],
        "pages": npages,
        "changed": n_changed,
    }
    if dry_run:
        return summary
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
    return summary


def collect_pdfs(paths: list[str | Path]) -> list[Path]:
    """ファイル/ディレクトリの混在から対象PDF一覧を作る(.bak/.prepatch は除外)。"""
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out += sorted(
                f for f in p.glob("*.pdf")
                if not f.name.endswith((".bak.pdf", ".prepatch.pdf"))
            )
        elif p.is_file() and p.suffix.lower() == ".pdf" and not p.name.endswith(
            (".bak.pdf", ".prepatch.pdf")
        ):
            out.append(p)
    # 重複除去(順序維持)
    seen: set[str] = set()
    uniq: list[Path] = []
    for f in out:
        k = str(f.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq
