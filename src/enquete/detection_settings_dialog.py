"""検出パラメータ調整ダイアログ(非モーダル)。

ink_threshold / roi_inset / dark_level / render_scale をスピンボックスで
調整し、変更のたびに valuesChanged を発火する(呼び出し側が即再検出)。
ROIオーバーレイ表示のトグルも提供する。変更した検出パラメータは現在の
フォーム定義(survey.detection)に反映され、サイドカー保存時に記録される。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from enquete.schema import CheckboxDetection


class DetectionSettingsDialog(QDialog):
    """検出パラメータの調整 UI。detection を直接書き換えて変更を通知する。"""

    valuesChanged = Signal()
    overlayToggled = Signal(bool)
    ratiosToggled = Signal(bool)
    applyRequested = Signal()  # 「適用（再電子化）」: 現在の検出設定で全ページ再電子化

    def __init__(
        self,
        detection: CheckboxDetection,
        overlay_enabled: bool,
        parent: QWidget | None = None,
        show_ratios: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("検出設定")
        self._detection = detection
        self._loading = False

        self._ink = self._spin(0.0, 1.0, 0.01, 2)
        self._inset = self._spin(0.0, 0.45, 0.01, 2)
        self._box_expand = self._spin(0.0, 1.0, 0.05, 2)  # box: 判定ROIの外側拡張率
        self._dark = self._spin(0.0, 1.0, 0.05, 2)
        self._scale = self._spin(1.0, 4.0, 0.5, 1)
        self._circle_ring = self._spin(0.2, 2.0, 0.1, 1)  # 丸数字: リング拡張率
        self._circle_min = self._spin(0.0, 0.5, 0.01, 2)  # 丸数字: 囲み判定の下限
        self._use_diff = QCheckBox("差分検出を使う（原紙をクリーン基準に）")
        self._diff_thr = self._spin(0.0, 0.5, 0.01, 2)  # 差分: チェック判定の下限
        self._overlay = QCheckBox("ROIオーバーレイを表示")
        self._ratios = QCheckBox("インク比率の数値を表示（検出チューニング用）")

        # 各項目の説明(ラベル・入力欄どちらをホバーしても出るよう両方に設定)。
        tips = {
            self._ink: "チェック判定の閾値。判定範囲内の暗い画素の割合がこの値以上なら"
            "「チェックあり」と判定します。\n下げると拾いやすく（チェック漏れが減る）／"
            "上げると厳しく（誤検出が減る）。",
            self._inset: "判定範囲を内側へ縮める割合。印刷された□の枠線を判定対象から"
            "除外するためのマージンです。\n大きいほど枠線の影響を避けますが、内寄りの薄い"
            "チェックは拾いにくくなります。",
            self._box_expand: "判定範囲を外側へ広げる割合。□からはみ出したチェックも"
            "拾いたいときに上げます（0で枠どおり）。\n差分検出時は枠線が相殺されるので"
            "安全に広げられます。PDF上の赤マーカーも連動して拡大します。",
            self._dark: "「暗い画素」とみなすグレースケール閾値（0=黒〜1=白）。\n"
            "これより暗い画素をインク（記入）として数えます。",
            self._scale: "検出時のレンダリング解像度の倍率。\n高いほど精細に判定できますが"
            "処理は重く（遅く）なります。",
            self._circle_ring: "丸数字（①など）を〇で囲む方式での、番号の周囲リング"
            "（手書きの囲み○が現れる帯）を外側へ広げる率。",
            self._circle_min: "丸数字の囲み判定で、リングを「囲みあり」と認める最小インク"
            "比率（絶対的な下限）。",
            self._use_diff: "原紙（クリーンな基準）との差分で記入を判定します。\n印刷物"
            "（枠線・ラベル）が相殺され、回答マークだけが残るので分離精度が上がります。",
            self._diff_thr: "差分検出時のチェック判定の下限。判定範囲内の「加筆された画素」"
            "の割合がこの値以上なら「チェックあり」と判定します。",
            self._overlay: "検出範囲の枠と「選択済み（集計対象）」マークを PDF 上に"
            "重ねて表示します。",
            self._ratios: "各判定範囲のインク比率の数値を PDF 上に表示します"
            "（検出チューニング時の確認用）。",
        }
        for wdg, tip in tips.items():
            wdg.setToolTip(tip)

        def row(text: str, wdg) -> None:
            """ラベル付き行を追加(ラベルにも同じツールチップを付ける)。"""
            label = QLabel(text)
            label.setToolTip(wdg.toolTip())
            form.addRow(label, wdg)

        form = QFormLayout()
        row("インク閾値 (ink_threshold)", self._ink)
        row("枠線除外 (roi_inset)", self._inset)
        row("判定範囲の拡張 (box_expand)", self._box_expand)
        row("暗画素レベル (dark_level)", self._dark)
        row("検出解像度 (render_scale)", self._scale)
        row("丸数字 リング幅 (circle_ring_expand)", self._circle_ring)
        row("丸数字 囲み下限 (circle_min_ink)", self._circle_min)
        form.addRow("", self._use_diff)
        row("差分 チェック下限 (diff_threshold)", self._diff_thr)
        form.addRow("", self._overlay)
        form.addRow("", self._ratios)

        hint = QLabel(
            "チェック漏れ→閾値を下げる / 誤検出→上げる。\n"
            "変更は即座に現在ページへ反映され、保存時にサイドカーへ記録されます。"
        )
        hint.setStyleSheet("color: #666; font-size: 11px;")
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._apply_btn = QPushButton("適用（再電子化）")
        self._apply_btn.setToolTip(
            "現在の検出設定のまま全ページを再電子化します"
            "（原紙の再指定なし・既存の読み取り結果は上書き）"
        )
        buttons.addButton(self._apply_btn, QDialogButtonBox.ButtonRole.ApplyRole)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

        self._load_from_detection(overlay_enabled, show_ratios)

        for sb in (
            self._ink, self._inset, self._box_expand, self._dark, self._scale,
            self._circle_ring, self._circle_min, self._diff_thr,
        ):
            sb.valueChanged.connect(self._on_value_changed)
        self._use_diff.toggled.connect(self._on_value_changed)
        self._overlay.toggled.connect(self.overlayToggled.emit)
        self._ratios.toggled.connect(self.ratiosToggled.emit)
        self._apply_btn.clicked.connect(self._on_apply)
        buttons.rejected.connect(self.close)

    @staticmethod
    def _spin(lo: float, hi: float, step: float, decimals: int) -> QDoubleSpinBox:
        sb = QDoubleSpinBox()
        sb.setRange(lo, hi)
        sb.setSingleStep(step)
        sb.setDecimals(decimals)
        return sb

    def _load_from_detection(self, overlay_enabled: bool, show_ratios: bool = False) -> None:
        self._loading = True
        self._ink.setValue(self._detection.ink_threshold)
        self._inset.setValue(self._detection.roi_inset)
        self._box_expand.setValue(self._detection.box_expand)
        self._dark.setValue(self._detection.dark_level)
        self._scale.setValue(self._detection.render_scale)
        self._circle_ring.setValue(self._detection.circle_ring_expand)
        self._circle_min.setValue(self._detection.circle_min_ink)
        self._use_diff.setChecked(self._detection.use_diff)
        self._diff_thr.setValue(self._detection.diff_threshold)
        self._overlay.setChecked(overlay_enabled)
        self._ratios.setChecked(show_ratios)
        self._loading = False

    def _on_value_changed(self, *_: object) -> None:
        if self._loading:
            return
        self._detection.ink_threshold = self._ink.value()
        self._detection.roi_inset = self._inset.value()
        self._detection.box_expand = self._box_expand.value()
        self._detection.dark_level = self._dark.value()
        self._detection.render_scale = self._scale.value()
        self._detection.circle_ring_expand = self._circle_ring.value()
        self._detection.circle_min_ink = self._circle_min.value()
        self._detection.use_diff = self._use_diff.isChecked()
        self._detection.diff_threshold = self._diff_thr.value()
        self.valuesChanged.emit()

    def _on_apply(self) -> None:
        """現在のUI値を detection へ確定してから「再電子化」を要求する。"""
        self._on_value_changed()  # 念のため最新値を反映(box_expand 等)
        self.applyRequested.emit()
