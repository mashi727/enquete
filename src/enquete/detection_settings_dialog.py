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
    QVBoxLayout,
    QWidget,
)

from enquete.schema import CheckboxDetection


class DetectionSettingsDialog(QDialog):
    """検出パラメータの調整 UI。detection を直接書き換えて変更を通知する。"""

    valuesChanged = Signal()
    overlayToggled = Signal(bool)

    def __init__(
        self,
        detection: CheckboxDetection,
        overlay_enabled: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("検出設定")
        self._detection = detection
        self._loading = False

        self._ink = self._spin(0.0, 1.0, 0.01, 2)
        self._inset = self._spin(0.0, 0.45, 0.01, 2)
        self._dark = self._spin(0.0, 1.0, 0.05, 2)
        self._scale = self._spin(1.0, 4.0, 0.5, 1)
        self._circle_ring = self._spin(0.2, 2.0, 0.1, 1)  # 丸数字: リング拡張率
        self._circle_min = self._spin(0.0, 0.5, 0.01, 2)  # 丸数字: 囲み判定の下限
        self._use_diff = QCheckBox("差分検出を使う（先頭ページをクリーン基準に）")
        self._diff_thr = self._spin(0.0, 0.5, 0.01, 2)  # 差分: チェック判定の下限
        self._overlay = QCheckBox("ROIオーバーレイを表示")

        form = QFormLayout()
        form.addRow("インク閾値 (ink_threshold)", self._ink)
        form.addRow("枠線除外 (roi_inset)", self._inset)
        form.addRow("暗画素レベル (dark_level)", self._dark)
        form.addRow("検出解像度 (render_scale)", self._scale)
        form.addRow("丸数字 リング幅 (circle_ring_expand)", self._circle_ring)
        form.addRow("丸数字 囲み下限 (circle_min_ink)", self._circle_min)
        form.addRow("", self._use_diff)
        form.addRow("差分 チェック下限 (diff_threshold)", self._diff_thr)
        form.addRow("", self._overlay)

        hint = QLabel(
            "チェック漏れ→閾値を下げる / 誤検出→上げる。\n"
            "変更は即座に現在ページへ反映され、保存時にサイドカーへ記録されます。"
        )
        hint.setStyleSheet("color: #666; font-size: 11px;")
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

        self._load_from_detection(overlay_enabled)

        for sb in (
            self._ink, self._inset, self._dark, self._scale,
            self._circle_ring, self._circle_min, self._diff_thr,
        ):
            sb.valueChanged.connect(self._on_value_changed)
        self._use_diff.toggled.connect(self._on_value_changed)
        self._overlay.toggled.connect(self.overlayToggled.emit)
        buttons.rejected.connect(self.close)

    @staticmethod
    def _spin(lo: float, hi: float, step: float, decimals: int) -> QDoubleSpinBox:
        sb = QDoubleSpinBox()
        sb.setRange(lo, hi)
        sb.setSingleStep(step)
        sb.setDecimals(decimals)
        return sb

    def _load_from_detection(self, overlay_enabled: bool) -> None:
        self._loading = True
        self._ink.setValue(self._detection.ink_threshold)
        self._inset.setValue(self._detection.roi_inset)
        self._dark.setValue(self._detection.dark_level)
        self._scale.setValue(self._detection.render_scale)
        self._circle_ring.setValue(self._detection.circle_ring_expand)
        self._circle_min.setValue(self._detection.circle_min_ink)
        self._use_diff.setChecked(self._detection.use_diff)
        self._diff_thr.setValue(self._detection.diff_threshold)
        self._overlay.setChecked(overlay_enabled)
        self._loading = False

    def _on_value_changed(self, *_: object) -> None:
        if self._loading:
            return
        self._detection.ink_threshold = self._ink.value()
        self._detection.roi_inset = self._inset.value()
        self._detection.dark_level = self._dark.value()
        self._detection.render_scale = self._scale.value()
        self._detection.circle_ring_expand = self._circle_ring.value()
        self._detection.circle_min_ink = self._circle_min.value()
        self._detection.use_diff = self._use_diff.isChecked()
        self._detection.diff_threshold = self._diff_thr.value()
        self.valuesChanged.emit()
