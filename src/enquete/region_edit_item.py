"""自由記述の記入領域を編集するためのハンドル付き矩形(QGraphicsObject)。

PDF表示シーン上に半透明の矩形＋8ハンドル(四隅・各辺中点)を描き、辺/角の
ドラッグで拡大縮小、内側のドラッグで移動できる。形状確定(マウス解放)時に
changed を発火する。座標はシーン(ポイント空間)。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

_GREEN = QColor(20, 140, 60)


class RegionEditItem(QGraphicsObject):
    changed = Signal()

    def __init__(self, rect: QRectF, handle: float) -> None:
        super().__init__()
        self._rect = QRectF(rect).normalized()
        self._handle = max(1e-3, handle)  # ハンドル一辺(シーン単位)
        self._mode: object | None = None  # 'move' or 0..7(ハンドル番号)
        self._press = QPointF()
        self._key_step = 2.0  # キーボード操作1回の移動/サイズ変更量(シーン単位)
        self.setAcceptHoverEvents(True)
        # キーボードで微調整できるようフォーカスを受け取れるようにする。
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setZValue(25)

    def rect(self) -> QRectF:
        return QRectF(self._rect)

    def set_handle_size(self, handle: float) -> None:
        self.prepareGeometryChange()
        self._handle = max(1e-3, handle)
        self.update()

    def boundingRect(self) -> QRectF:
        m = self._handle
        return self._rect.adjusted(-m, -m, m, m)

    def _handles(self) -> dict[int, QRectF]:
        r = self._rect
        h = self._handle
        cx = (r.left() + r.right()) / 2
        cy = (r.top() + r.bottom()) / 2
        pts = {
            0: (r.left(), r.top()),
            1: (cx, r.top()),
            2: (r.right(), r.top()),
            3: (r.right(), cy),
            4: (r.right(), r.bottom()),
            5: (cx, r.bottom()),
            6: (r.left(), r.bottom()),
            7: (r.left(), cy),
        }
        return {i: QRectF(x - h / 2, y - h / 2, h, h) for i, (x, y) in pts.items()}

    def paint(self, painter, option, widget=None) -> None:  # noqa: ARG002
        border = QPen(_GREEN)
        border.setCosmetic(True)
        border.setWidthF(1.5)
        painter.setPen(border)
        painter.setBrush(QBrush(QColor(20, 140, 60, 40)))
        painter.drawRect(self._rect)
        # ハンドル(白地・緑枠)
        hpen = QPen(_GREEN)
        hpen.setCosmetic(True)
        painter.setPen(hpen)
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        for hr in self._handles().values():
            painter.drawRect(hr)

    def mousePressEvent(self, event) -> None:
        pos = event.pos()
        self._mode = None
        for i, hr in self._handles().items():
            if hr.contains(pos):
                self._mode = i
                break
        if self._mode is None and self._rect.contains(pos):
            self._mode = "move"
        self._press = pos
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._mode is None:
            return
        d = event.pos() - self._press
        self._press = event.pos()
        r = QRectF(self._rect)
        if self._mode == "move":
            r.translate(d)
        else:
            i = self._mode
            if i in (0, 1, 2):
                r.setTop(r.top() + d.y())
            if i in (4, 5, 6):
                r.setBottom(r.bottom() + d.y())
            if i in (2, 3, 4):
                r.setRight(r.right() + d.x())
            if i in (0, 6, 7):
                r.setLeft(r.left() + d.x())
        self.prepareGeometryChange()
        self._rect = r.normalized()
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ARG002
        self._mode = None
        self.changed.emit()

    def keyPressEvent(self, event) -> None:
        """矢印キーで移動、Shift＋矢印で中心を保ったまま拡大(右/上)・縮小(左/下)。"""
        arrows = (
            Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
        )
        key = event.key()
        if key not in arrows:
            super().keyPressEvent(event)
            return
        step = self._key_step
        r = QRectF(self._rect)
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # 中心固定の拡大縮小(縦横を等量)。右/上=拡大, 左/下=縮小。
            grow = step if key in (Qt.Key.Key_Right, Qt.Key.Key_Up) else -step
            new = r.adjusted(-grow / 2, -grow / 2, grow / 2, grow / 2)
            if new.width() >= 2.0 and new.height() >= 2.0:  # 最小サイズ保護
                r = new
        else:
            dx = -step if key == Qt.Key.Key_Left else (step if key == Qt.Key.Key_Right else 0)
            dy = -step if key == Qt.Key.Key_Up else (step if key == Qt.Key.Key_Down else 0)
            r.translate(dx, dy)
        self.prepareGeometryChange()
        self._rect = r.normalized()
        self.update()
        self.changed.emit()
        event.accept()

    def hoverMoveEvent(self, event) -> None:
        self.setCursor(self._cursor_at(event.pos()))

    def _cursor_at(self, pos: QPointF) -> Qt.CursorShape:
        for i, hr in self._handles().items():
            if hr.contains(pos):
                if i in (0, 4):
                    return Qt.CursorShape.SizeFDiagCursor
                if i in (2, 6):
                    return Qt.CursorShape.SizeBDiagCursor
                if i in (1, 5):
                    return Qt.CursorShape.SizeVerCursor
                return Qt.CursorShape.SizeHorCursor
        if self._rect.contains(pos):
            return Qt.CursorShape.SizeAllCursor
        return Qt.CursorShape.ArrowCursor

    def cursor_for(self, scene_pos: QPointF) -> Qt.CursorShape:
        pos = self.mapFromScene(scene_pos)
        for i, hr in self._handles().items():
            if hr.contains(pos):
                if i in (0, 4):
                    return Qt.CursorShape.SizeFDiagCursor
                if i in (2, 6):
                    return Qt.CursorShape.SizeBDiagCursor
                if i in (1, 5):
                    return Qt.CursorShape.SizeVerCursor
                return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeAllCursor
