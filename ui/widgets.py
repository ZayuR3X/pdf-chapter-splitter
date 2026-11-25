from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from core.i18n import tr


class PagePreviewWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._scale_factor: float = 1.0
        self._load_failed: bool = False

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.image_label = QLabel(tr("preview_placeholder"))
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("color: #888; font-size: 14px; padding: 20px;")

        self.scroll_area.setWidget(self.image_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)

    def retranslate_ui(self) -> None:
        if self._pixmap is None:
            key = "preview_failed" if self._load_failed else "preview_placeholder"
            self.image_label.setText(tr(key))

    def set_image_bytes(self, png_bytes: Optional[bytes]) -> None:
        if not png_bytes:
            self._pixmap = None
            self._load_failed = True
            self.image_label.clear()
            self.image_label.setText(tr("preview_failed"))
            return

        self._load_failed = False
        image = QImage.fromData(png_bytes, "PNG")
        self._pixmap = QPixmap.fromImage(image)
        self._render_scaled()

    def zoom_in(self) -> None:
        self._scale_factor = min(2.5, self._scale_factor + 0.15)
        self._render_scaled()

    def zoom_out(self) -> None:
        self._scale_factor = max(0.3, self._scale_factor - 0.15)
        self._render_scaled()

    def fit_to_width(self) -> None:
        if not self._pixmap or self._pixmap.isNull():
            return
        viewport_width = max(200, self.scroll_area.viewport().width() - 28)
        self._scale_factor = viewport_width / self._pixmap.width()
        self._render_scaled()

    def _render_scaled(self) -> None:
        if not self._pixmap or self._pixmap.isNull():
            return
        target_width = int(self._pixmap.width() * self._scale_factor)
        scaled = self._pixmap.scaledToWidth(
            max(100, target_width),
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)