"""スキャンページの位置合わせ(原紙へのレジストレーション)。

各ページを (1) Hough で推定した傾きを打ち消して絶対水平へ正立化し、(2) 原紙
(クリーン正本・同じく水平化)との位相相関で平行移動量を推定して原紙と同位置へ
ずらす。回転＋平行移動を反映した**新しいPDF**を pypdfium2 で書き出す(元PDFは
別途 .bak へ退避してから上書きする運用)。

差分検出は原紙との画素差で記入を判定するため、印刷物(枠線・ラベル)が画素単位で
重なるほど精度が上がる。スキャンの微小な傾き・位置ずれを原紙基準で吸収する。

依存はライセンス方針内(OpenCV: Apache/BSD, pypdfium2: BSD, Pillow: HPND)。
"""
from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from enquete.deskew import _rotate_pil, estimate_skew_angle

# 推定シフトの妥当範囲(用紙寸法に対する割合)。これを超える推定は誤りとみなし、
# 平行移動を 0 に丸める(加筆過多・罫線希薄なページでの暴走を防ぐ)。
MAX_SHIFT_FRAC = 0.08


def _to_gray01(pil: Image.Image) -> np.ndarray:
    """PIL画像を 0=黒〜1=白 の float32 グレースケールへ。"""
    return np.asarray(pil.convert("L"), dtype=np.float32) / 255.0


def estimate_translation(
    page_gray: np.ndarray, ref_gray: np.ndarray
) -> tuple[float, float, float]:
    """page を ref に重ねるための平行移動 (sx, sy) と相関応答を返す。

    位相相関(FFT)でサブピクセル推定する。ハニング窓で端の不連続を抑制。
    返す (sx, sy) は「page の内容を (sx, sy) だけ動かすと ref に重なる」量。
    寸法が違う場合は推定不能として (0, 0, 0) を返す(倍率差は平行移動で直せない)。
    """
    if page_gray.shape != ref_gray.shape:
        return 0.0, 0.0, 0.0
    h, w = ref_gray.shape
    if h < 8 or w < 8:
        return 0.0, 0.0, 0.0
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    a = np.ascontiguousarray(page_gray, dtype=np.float32)
    b = np.ascontiguousarray(ref_gray, dtype=np.float32)
    (dx, dy), resp = cv2.phaseCorrelate(a, b, win)
    return float(dx), float(dy), float(resp)


def _level_gray(pil: Image.Image) -> tuple[Image.Image, np.ndarray, float]:
    """Hough で水平化した画像・そのグレースケール・適用した補正角を返す。"""
    gray = _to_gray01(pil)
    deg = estimate_skew_angle(gray)
    if deg:
        pil = _rotate_pil(pil, deg)
        gray = _to_gray01(pil)
    return pil, gray, deg


def write_aligned_pdf(
    src_pdf: str | Path,
    ref_pdf: str | Path,
    out_pdf: str | Path,
    render_scale: float = 3.0,
    level_reference: bool = True,
    progress=None,
) -> list[dict]:
    """各ページを水平化＋原紙への平行移動で整列した新PDFを out_pdf へ書き出す。

    原紙は ref_pdf の1ページ目を基準にする。level_reference=True なら原紙も Hough で
    水平化する(スキャン原紙)。False なら原紙をそのまま基準にする(電子データ出力の
    PDF＝厳密に正立。Hough の誤検出を避け基準フレームを厳密化)。元の各ページと同じ
    点サイズの新規ページに整列済みラスタ画像を全面貼付する。progress(i, n) があれば
    各ページ後に呼ぶ。戻り値はページごとの {deg, dx, dy, applied} のリスト(ログ用)。
    """
    src_pdf = Path(src_pdf)
    ref_pdf = Path(ref_pdf)
    out_pdf = Path(out_pdf)
    src = pdfium.PdfDocument(str(src_pdf))
    ref = pdfium.PdfDocument(str(ref_pdf))
    out = pdfium.PdfDocument.new()
    report: list[dict] = []
    try:
        # 原紙(基準)1ページ目をグレースケール化(全ページ共通)。スキャン原紙のみ
        # 水平化する。電子PDFの原紙は厳密に正立なので水平化しない。
        ref_page = ref[0]
        ref_pil = ref_page.render(scale=render_scale).to_pil().convert("RGB")
        ref_page.close()
        if level_reference:
            _, ref_gray, _ = _level_gray(ref_pil)
        else:
            ref_gray = _to_gray01(ref_pil)
        rh, rw = ref_gray.shape

        n = len(src)
        for i in range(n):
            page = src[i]
            w_pt, h_pt = page.get_size()
            pil = page.render(scale=render_scale).to_pil().convert("RGB")
            page.close()
            # (1) 水平化
            leveled, gray_d, deg = _level_gray(pil)
            # (2) 原紙への平行移動
            dx, dy, _resp = estimate_translation(gray_d, ref_gray)
            applied = True
            if abs(dx) > rw * MAX_SHIFT_FRAC or abs(dy) > rh * MAX_SHIFT_FRAC:
                dx = dy = 0.0  # 推定が大きすぎる=信頼できない → 移動なし
                applied = False
            if dx or dy:
                leveled = leveled.transform(
                    leveled.size,
                    Image.AFFINE,
                    (1, 0, -dx, 0, 1, -dy),
                    resample=Image.Resampling.BICUBIC,
                    fillcolor=(255, 255, 255),
                )
            report.append(
                {"page": i, "deg": round(deg, 3), "dx": round(dx, 1),
                 "dy": round(dy, 1), "applied": applied}
            )

            new_page = out.new_page(w_pt, h_pt)
            image = pdfium.PdfImage.new(out)
            image.set_bitmap(pdfium.PdfBitmap.from_pil(leveled.convert("RGB")))
            image.set_matrix(pdfium.PdfMatrix(w_pt, 0, 0, h_pt, 0, 0))
            new_page.insert_obj(image)
            new_page.gen_content()
            new_page.close()
            if progress is not None:
                progress(i + 1, n)

        buf = io.BytesIO()
        out.save(buf)
        out_pdf.write_bytes(buf.getvalue())
        return report
    finally:
        out.close()
        ref.close()
        src.close()
