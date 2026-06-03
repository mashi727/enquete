"""ページの傾き(skew)推定と、補正済みPDFの書き出し。

スキャンした各ページの傾きを OpenCV で推定し、補正角でページを正立化した
**新しいPDF**を pypdfium2 で生成する(元PDFは保持。再ラスタライズのため
画像系PDF向け)。補正角の符号は「PIL.Image.rotate に渡す値(反時計回り正)」で
統一する。表示プレビュー側は Qt のため -角度で回す(QPainter は時計回り正)。

依存はライセンス方針内(OpenCV: Apache/BSD, pypdfium2: BSD, Pillow: HPND)。
"""
from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

MAX_SKEW_DEG = 15.0


def estimate_skew_angle(gray: np.ndarray, max_angle: float = MAX_SKEW_DEG) -> float:
    """グレースケール(0=黒,1=白)からページ傾きの補正角(度)を推定する。

    近水平の直線(罫線・テキスト行のエッジ)の傾きを Hough で集めて中央値を取り、
    それを打ち消す補正角を返す(PIL.Image.rotate に渡せる反時計回り正の値)。
    推定できなければ 0.0。|角度|<0.3°は 0 に丸める(微小揺れの回避)。
    """
    u8 = (np.clip(gray, 0.0, 1.0) * 255.0).astype(np.uint8)
    h, w = u8.shape
    if h < 8 or w < 8:
        return 0.0
    edges = cv2.Canny(u8, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 360.0,
        threshold=120,
        minLineLength=int(w * 0.25),
        maxLineGap=20,
    )
    if lines is None:
        return 0.0
    angs: list[float] = []
    for x1, y1, x2, y2 in lines[:, 0, :]:
        if x2 == x1:
            continue
        a = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if abs(a) <= max_angle:  # 近水平のみ
            angs.append(a)
    if not angs:
        return 0.0
    med = float(np.median(angs))
    # 画像座標(y下向き)で時計回りに med 傾いている → 反時計回りに med 戻す。
    # PIL.rotate(+med) は反時計回り med なので、補正角 = med。
    corr = med
    if abs(corr) < 0.3:
        return 0.0
    return max(-max_angle, min(max_angle, corr))


def _rotate_pil(img: Image.Image, deg: float) -> Image.Image:
    if not deg:
        return img
    return img.rotate(
        deg, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(255, 255, 255)
    )


def write_deskewed_pdf(
    src_pdf: str | Path,
    angles: dict[int, float],
    out_pdf: str | Path,
    render_scale: float = 3.0,
    progress=None,
) -> Path:
    """各ページを angles[index](補正角・度)で正立化した新PDFを書き出す。

    元の各ページと同じ点サイズの新規ページに、回転済みラスタ画像を全面貼付する。
    angles に無いページは 0°(無回転)。progress(i, n) があれば各ページ後に呼ぶ。
    戻り値は出力パス。
    """
    src_pdf = Path(src_pdf)
    out_pdf = Path(out_pdf)
    src = pdfium.PdfDocument(str(src_pdf))
    out = pdfium.PdfDocument.new()
    try:
        n = len(src)
        for i in range(n):
            page = src[i]
            w_pt, h_pt = page.get_size()
            pil = page.render(scale=render_scale).to_pil().convert("RGB")
            page.close()
            pil = _rotate_pil(pil, float(angles.get(i, 0.0)))
            new_page = out.new_page(w_pt, h_pt)
            image = pdfium.PdfImage.new(out)
            image.set_bitmap(pdfium.PdfBitmap.from_pil(pil))
            image.set_matrix(pdfium.PdfMatrix(w_pt, 0, 0, h_pt, 0, 0))
            new_page.insert_obj(image)
            new_page.gen_content()
            new_page.close()
            if progress is not None:
                progress(i + 1, n)
        buf = io.BytesIO()
        out.save(buf)
        out_pdf.write_bytes(buf.getvalue())
        return out_pdf
    finally:
        out.close()
        src.close()
