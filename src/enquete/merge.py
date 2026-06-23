"""アンケート(結果を埋め込んだ自己完結PDF)の分割・マージ。

手分け作業のため、1つの調査PDFを重複なしのチャンクに**分割**して配布し、
各担当が校正した結果を1つに**マージ**する。結果はページ結果を PDF のページ
番号で索引するため、マージでは PDF を連結しつつ各ページ番号を連結順のオフセット
へ付け替える(PDFを連結しないと番号が破綻する)。チャンク・統合物とも、結果と
原紙基準画像を PDF 自身へ埋め込む(サイドカーJSONは作らない)。

Qt 非依存。PDF 連結は pypdfium2(BSD) の import_pages/save、埋め込み/抽出は
pikepdf(QPDF) を用いる。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

from enquete import pdf_embed
from enquete.schema import Survey, survey_from_dict, survey_to_dict


# --------------------------------------------------------------- 分割レイアウト
def plan_split(total_pages: int, n_parts: int) -> list[tuple[int, int]]:
    """総ページ数を n_parts に均等分割し、各パートの (開始index, ページ数) を返す。

    余りは先頭側のパートへ1ページずつ配分する。空パートは作らない。
    """
    if total_pages <= 0:
        raise ValueError("ページ数が0です。")
    if n_parts < 1:
        raise ValueError("分割数は1以上にしてください。")
    if n_parts > total_pages:
        raise ValueError(
            f"分割数({n_parts})が総ページ数({total_pages})を超えています。"
        )
    base, extra = divmod(total_pages, n_parts)
    ranges: list[tuple[int, int]] = []
    offset = 0
    for i in range(n_parts):
        count = base + (1 if i < extra else 0)
        ranges.append((offset, count))
        offset += count
    return ranges


def insert_pdf(
    base_pdf: str | Path,
    insert_pdf_path: str | Path,
    insert_at: int,
    out_pdf: str | Path,
) -> int:
    """base_pdf の insert_at 位置に insert_pdf_path の全ページを挿入して out へ。

    挿入したページ数を返す。位置は [0, len(base)] にクランプする。
    """
    base = pdfium.PdfDocument(str(base_pdf))
    ins = pdfium.PdfDocument(str(insert_pdf_path))
    try:
        n = len(ins)
        at = max(0, min(int(insert_at), len(base)))
        base.import_pages(ins, pages=None, index=at)
        with open(out_pdf, "wb") as fh:
            base.save(fh)
        return n
    finally:
        ins.close()
        base.close()


def _part_stem(stem: str, part: int, parts: int) -> str:
    width = max(2, len(str(parts)))
    return f"{stem}_part{part:0{width}d}of{parts:0{width}d}"


# --------------------------------------------------------------------- 分割本体
def split_pdf(
    pdf_path: str | Path,
    survey: Survey,
    n_parts: int,
    out_dir: str | Path,
    pages: dict[str, dict] | None = None,
    baseline_png: bytes | None = None,
) -> list[Path]:
    """PDF を n_parts に分割し、各チャンクを「結果を埋め込んだ自己完結PDF」で書き出す。

    各チャンクPDFに同一 survey・担当ページの結果(0始まりに振り直し)・由来メタ split{}・
    原紙基準画像を**埋め込む**(サイドカーJSONは作らない)。担当者はチャンクPDFを開いて
    校正し、PDFごと or 提出JSONで返す。戻り値はチャンク PDF のパス一覧。
    """
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = pages or {}
    src = pdfium.PdfDocument(str(pdf_path))
    try:
        total = len(src)
        ranges = plan_split(total, n_parts)
        survey_dict = survey_to_dict(survey)
        stem = pdf_path.stem
        out_paths: list[Path] = []
        for i, (offset, count) in enumerate(ranges):
            part = i + 1
            part_stem = _part_stem(stem, part, n_parts)
            out_pdf = out_dir / f"{part_stem}.pdf"
            # PDF: 該当ページ範囲を新規ドキュメントへインポートして保存
            dst = pdfium.PdfDocument.new()
            dst.import_pages(src, pages=list(range(offset, offset + count)))
            with open(out_pdf, "wb") as fh:
                dst.save(fh)
            dst.close()
            # 担当ページの読み取り結果を 0 始まりへ振り直して同梱
            chunk_pages: dict[str, dict] = {}
            for k, rec in pages.items():
                try:
                    idx = int(k)
                except (TypeError, ValueError):
                    continue
                if offset <= idx < offset + count:
                    chunk_pages[str(idx - offset)] = dict(rec)
            # チャンクPDFへ埋め込み: フォーム複製＋由来メタ＋(電子化済みなら)結果
            data = {
                "source_pdf": out_pdf.name,
                "survey": survey_dict,
                "pages": chunk_pages,
                "split": {
                    "original_pdf": pdf_path.name,
                    "page_offset": offset,
                    "page_count": count,
                    "part": part,
                    "parts": n_parts,
                },
            }
            pdf_embed.write_data(
                out_pdf, data, baseline_png=baseline_png, backup=False
            )
            out_paths.append(out_pdf)
        return out_paths
    finally:
        src.close()


# --------------------------------------------------------------------- マージ入力
@dataclass
class MergeInput:
    """マージ1件分: チャンク PDF とそのサイドカー(生 dict)。"""

    pdf_path: Path
    sidecar: dict

    @property
    def split_meta(self) -> dict | None:
        meta = self.sidecar.get("split")
        return meta if isinstance(meta, dict) else None

    @property
    def page_count(self) -> int:
        # 由来メタがあれば優先。無ければ実 PDF のページ数。
        meta = self.split_meta
        if meta and isinstance(meta.get("page_count"), int):
            return int(meta["page_count"])
        doc = pdfium.PdfDocument(str(self.pdf_path))
        try:
            return len(doc)
        finally:
            doc.close()


def load_merge_input(path: str | Path) -> MergeInput:
    """マージ入力(チャンクPDF or 提出JSON)を読み、MergeInput を返す。

    - PDF を渡した場合: 埋め込み結果(enquete-data.json)を読む。
    - JSON を渡した場合(提出JSON/旧サイドカー): source_pdf(同ディレクトリ)を優先し、
      無ければ同名(同 stem)の .pdf を探して対応 PDF を解決する。
    """
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        data = pdf_embed.read_data(path)
        if data is None:
            raise ValueError(
                f"{path.name} に埋め込みの読み取り結果がありません"
                "(電子化済みのPDFを指定してください)。"
            )
        return MergeInput(pdf_path=path, sidecar=data)
    data = json.loads(path.read_text(encoding="utf-8"))
    src_name = data.get("source_pdf")
    candidates = []
    if isinstance(src_name, str) and src_name:
        candidates.append(path.parent / src_name)
    candidates.append(path.with_suffix(".pdf"))
    for cand in candidates:
        if cand.exists():
            return MergeInput(pdf_path=cand, sidecar=data)
    raise FileNotFoundError(
        f"{path.name} に対応する PDF が見つかりません"
        f"(探索: {', '.join(str(c.name) for c in candidates)})。"
    )


# --------------------------------------------------------------- 整列・検証
def order_and_validate(
    inputs: list[MergeInput], allow_partial: bool = False
) -> tuple[list[MergeInput], list[str]]:
    """マージ順を決め、由来メタで整合性を検証する。警告メッセージ一覧も返す。

    全入力に split メタがあれば page_offset で整列し、同一元・全パート・抜け
    /重複なしを検証する。1つでも欠ければ allow_partial=False では例外。
    メタが無い入力があれば与えられた順序のまま(検証なし・警告)。
    """
    warnings: list[str] = []
    metas = [inp.split_meta for inp in inputs]
    if not inputs:
        raise ValueError("マージ対象が指定されていません。")
    if any(m is None for m in metas):
        warnings.append(
            "由来メタデータ(split)の無い入力があるため、指定された順序のまま"
            "連結します(整合性チェックは行いません)。"
        )
        return list(inputs), warnings

    # 全件メタあり: 同一元か
    originals = {m["original_pdf"] for m in metas}  # type: ignore[index]
    if len(originals) > 1:
        raise ValueError(
            "異なる元PDF由来のチャンクが混在しています: "
            + ", ".join(sorted(originals))
        )
    parts_decl = {int(m["parts"]) for m in metas}  # type: ignore[index]
    if len(parts_decl) > 1:
        raise ValueError("パート総数(parts)の宣言が一致しません。")
    parts_total = next(iter(parts_decl))

    ordered = sorted(inputs, key=lambda x: int(x.split_meta["page_offset"]))  # type: ignore[index]
    seen_parts = sorted(int(x.split_meta["part"]) for x in ordered)  # type: ignore[index]
    expected = list(range(1, parts_total + 1))
    if seen_parts != expected:
        missing = sorted(set(expected) - set(seen_parts))
        dups = sorted({p for p in seen_parts if seen_parts.count(p) > 1})
        msg_parts = []
        if missing:
            msg_parts.append(f"欠番パート: {missing}")
        if dups:
            msg_parts.append(f"重複パート: {dups}")
        detail = " / ".join(msg_parts) or f"パート構成: {seen_parts}"
        if not allow_partial:
            raise ValueError(
                f"全{parts_total}パートが揃っていません({detail})。"
                "部分マージを許可するか、欠けたパートを追加してください。"
            )
        warnings.append(f"部分マージ: {detail}。揃っている分のみ連結します。")

    # 連続性(オフセットと枚数の整合)を検証
    cursor = None
    for x in ordered:
        off = int(x.split_meta["page_offset"])  # type: ignore[index]
        cnt = int(x.split_meta["page_count"])  # type: ignore[index]
        if cursor is not None and off != cursor and not allow_partial:
            raise ValueError(
                f"ページ範囲に抜けまたは重複があります"
                f"(直前の終端={cursor}, 次の開始={off})。"
            )
        cursor = off + cnt
    return ordered, warnings


# --------------------------------------------------------------------- マージ本体
@dataclass
class MergeResult:
    out_pdf: Path
    total_pages: int
    total_results: int
    reviewed_count: int
    warnings: list[str]


def merge_documents(
    inputs: list[MergeInput],
    out_pdf: str | Path,
    *,
    allow_partial: bool = False,
) -> MergeResult:
    """チャンク群を連結し、統合結果を埋め込んだ自己完結 PDF を書き出す。

    survey は先頭入力を正とし、相違があれば警告。ページレコード(results,
    reviewed)は連結順のオフセットで付け替えて1つの pages へ統合し、原紙基準画像
    (先頭チャンクの埋め込み)も引き継いで PDF へ埋め込む(サイドカーJSONは作らない)。
    """
    out_pdf = Path(out_pdf)
    ordered, warnings = order_and_validate(inputs, allow_partial=allow_partial)

    # survey の一致確認(先頭を正とする)
    base_survey = ordered[0].sidecar.get("survey")
    for inp in ordered[1:]:
        if inp.sidecar.get("survey") != base_survey:
            warnings.append(
                f"{inp.pdf_path.name} のフォーム定義が先頭と異なります。"
                "先頭の定義を採用します。"
            )
    # 妥当性確認のためデシリアライズ(失敗すれば例外で気づける)
    survey: Survey = survey_from_dict(base_survey)

    merged_pages: dict[str, dict] = {}
    total_results = 0
    reviewed_count = 0

    dst = pdfium.PdfDocument.new()
    sources: list[pdfium.PdfDocument] = []
    try:
        running = 0
        for inp in ordered:
            src = pdfium.PdfDocument(str(inp.pdf_path))
            sources.append(src)
            n = len(src)
            dst.import_pages(src, pages=None)  # 全ページを末尾へ追記
            pages = inp.sidecar.get("pages", {})
            if isinstance(pages, dict):
                for k, rec in pages.items():
                    try:
                        local = int(k)
                    except (TypeError, ValueError):
                        continue
                    if local < 0 or local >= n:
                        warnings.append(
                            f"{inp.pdf_path.name}: ページ {local} は範囲外のため除外。"
                        )
                        continue
                    new_index = running + local
                    merged_pages[str(new_index)] = dict(rec)
                    if rec.get("results"):
                        total_results += 1
                    if rec.get("reviewed"):
                        reviewed_count += 1
            running += n
        with open(out_pdf, "wb") as fh:
            dst.save(fh)
        total_pages = running
    finally:
        for s in sources:
            s.close()
        dst.close()

    data = {
        "source_pdf": out_pdf.name,
        "survey": survey_to_dict(survey),
        "pages": merged_pages,
    }
    # 原紙基準画像は先頭チャンクの埋め込みを引き継ぐ(再電子化を自己完結にするため)。
    baseline_png = pdf_embed.read_baseline(ordered[0].pdf_path)
    pdf_embed.write_data(out_pdf, data, baseline_png=baseline_png, backup=False)
    return MergeResult(
        out_pdf=out_pdf,
        total_pages=total_pages,
        total_results=total_results,
        reviewed_count=reviewed_count,
        warnings=warnings,
    )
