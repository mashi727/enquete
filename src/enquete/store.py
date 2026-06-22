"""校正結果のサイドカー保存。

PDF `foo.pdf` に対して同ディレクトリ・同名の `foo.json` を作り、
フォーム定義(schema)と全ページの校正結果・確認フラグを自己完結で保持する。
同名 JSON があれば、その定義と結果で作業を再開・検証できる。
"""
from __future__ import annotations

import json
from pathlib import Path

from enquete.schema import Survey, survey_from_dict, survey_to_dict


class SurveyDocument:
    """1つの PDF に対応するサイドカー JSON(フォーム定義＋ページ校正結果)。"""

    def __init__(self, pdf_path: str | Path, survey: Survey | None) -> None:
        self.pdf_path = Path(pdf_path)
        self.json_path = self.pdf_path.with_suffix(".json")
        self.survey = survey
        # index(str) -> {"results": {...}, "reviewed": bool}
        self._pages: dict[str, dict] = {}

    # ------------------------------------------------------------------ load
    @classmethod
    def load(
        cls, pdf_path: str | Path, default_survey: Survey | None
    ) -> SurveyDocument:
        """サイドカーがあれば読み込み(その定義を正とする)、無ければ新規。"""
        pdf_path = Path(pdf_path)
        json_path = pdf_path.with_suffix(".json")
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            survey = survey_from_dict(data["survey"])
            doc = cls(pdf_path, survey)
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
        """サイドカー JSON を書き出す。保存先パスを返す。"""
        assert self.survey is not None, "フォーム未設定のサイドカーは保存できません"
        data = {
            "source_pdf": self.pdf_path.name,
            "survey": survey_to_dict(self.survey),
            "pages": self._pages,
        }
        self.json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return self.json_path
