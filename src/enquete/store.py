"""校正結果の永続化（PDFへの埋め込み）。

記入済み PDF 自身に、フォーム定義(survey)・全ページの校正結果/確認フラグ・差分検出の
基準(原紙)画像を**添付として埋め込む**(`pdf_embed`)。サイドカー JSON は作らない。
同じ PDF を開けば、その埋め込み定義と結果で作業を再開・検証できる(自己完結)。

保存はロスレスなフル書き出しで作業 PDF をその場上書きする。配布時点の原本は、PDF を
最初に開いたとき `<stem>.bak.pdf` へ退避してあるので常に復元できる(main_window 側)。
"""
from __future__ import annotations

from pathlib import Path

from enquete import pdf_embed
from enquete.schema import Survey, survey_from_dict, survey_to_dict


class SurveyDocument:
    """1つの PDF に対応する埋め込みデータ(フォーム定義＋ページ校正結果＋基準画像)。"""

    def __init__(
        self,
        pdf_path: str | Path,
        survey: Survey | None,
        baseline_png: bytes | None = None,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.survey = survey
        self.baseline_png = baseline_png  # 差分検出の基準(原紙)画像 PNG
        # index(str) -> {"results": {...}, "reviewed": bool}
        self._pages: dict[str, dict] = {}

    # ------------------------------------------------------------------ load
    @classmethod
    def load(
        cls, pdf_path: str | Path, default_survey: Survey | None
    ) -> SurveyDocument:
        """PDF に埋め込みデータがあれば読み込み(その定義を正とする)、無ければ新規。"""
        pdf_path = Path(pdf_path)
        data = pdf_embed.read_data(pdf_path)
        if data is not None and data.get("survey") is not None:
            survey = survey_from_dict(data["survey"])
            doc = cls(pdf_path, survey, baseline_png=pdf_embed.read_baseline(pdf_path))
            pages = data.get("pages", {})
            if isinstance(pages, dict):
                doc._pages = {str(k): dict(v) for k, v in pages.items()}
            return doc
        return cls(pdf_path, default_survey)

    # ----------------------------------------------------------------- pages
    def get_results(self, index: int) -> dict | None:
        page = self._pages.get(str(index))
        return page.get("results") if page else None

    def is_reviewed(self, index: int) -> bool:
        page = self._pages.get(str(index))
        return bool(page.get("reviewed")) if page else False

    def set_results(self, index: int, results: dict) -> None:
        page = self._pages.setdefault(str(index), {})
        page["results"] = results

    def set_reviewed(self, index: int, reviewed: bool) -> None:
        page = self._pages.setdefault(str(index), {})
        page["reviewed"] = reviewed

    def set_baseline(self, baseline_png: bytes | None) -> None:
        """差分検出の基準(原紙)画像を設定する(以後の保存で PDF へ埋め込む)。"""
        self.baseline_png = baseline_png

    def reviewed_indices(self) -> set[int]:
        return {int(k) for k, v in self._pages.items() if v.get("reviewed")}

    def pages_dict(self) -> dict[str, dict]:
        """全ページの校正結果(index文字列 -> {results, reviewed})の複製を返す。"""
        return {k: dict(v) for k, v in self._pages.items()}

    def shift_pages(self, insert_at: int, count: int) -> None:
        """insert_at 以降のページ結果を count ぶん後ろへずらす(ページ挿入時)。

        挿入された位置(insert_at..insert_at+count-1)は空きのままになる。
        """
        if count <= 0:
            return
        shifted: dict[str, dict] = {}
        for k, v in self._pages.items():
            i = int(k)
            shifted[str(i + count if i >= insert_at else i)] = v
        self._pages = shifted

    # ------------------------------------------------------------------ save
    def save(self) -> Path:
        """フォーム定義・結果・基準画像を作業 PDF へ埋め込み上書きする。保存先を返す。"""
        assert self.survey is not None, "フォーム未設定では保存できません"
        data = {
            "source_pdf": self.pdf_path.name,
            "survey": survey_to_dict(self.survey),
            "pages": self._pages,
        }
        # 原本退避(.bak)は呼び出し側(初回オープン時・生スキャンのみ)で行うため、
        # ここでは退避しない。
        pdf_embed.write_data(
            self.pdf_path, data, baseline_png=self.baseline_png, backup=False
        )
        return self.pdf_path
