from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, QPoint, QRunnable, Qt, QThread, QThreadPool, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import BookSection
from core.pdf_processor import PDFProcessor
from ui.widgets import PagePreviewWidget


class RenderSignals(QObject):
    finished = Signal(int, int, object)


class PageRenderTask(QRunnable):
    def __init__(self, processor: PDFProcessor, request_id: int, page_num: int, zoom: float) -> None:
        super().__init__()
        self.processor = processor
        self.request_id = request_id
        self.page_num = page_num
        self.zoom = zoom
        self.signals = RenderSignals()

    def run(self) -> None:
        png_bytes = self.processor.render_page_png(self.page_num, zoom=self.zoom)
        self.signals.finished.emit(self.request_id, self.page_num, png_bytes)


class ExportWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(list, str)
    failed = Signal(str)

    def __init__(
        self,
        processor: PDFProcessor,
        sections: List[BookSection],
        out_dir: str,
        pattern: str,
        smart_crop: bool,
    ) -> None:
        super().__init__()
        self.processor = processor
        self.sections = sections
        self.out_dir = out_dir
        self.pattern = pattern
        self.smart_crop = smart_crop

    def run(self) -> None:
        try:
            created = self.processor.export_sections(
                self.sections,
                self.out_dir,
                filename_pattern=self.pattern,
                smart_crop=self.smart_crop,
                progress_callback=lambda cur, tot, name: self.progress.emit(cur, tot, name),
            )
            self.completed.emit(created, self.out_dir)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    COL_TITLE = 0
    COL_START = 1
    COL_END = 2
    COL_COUNT = 3
    COL_LABEL = 4

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Bölüm Ayırıcı")
        self.setMinimumSize(920, 600)
        self.resize(1220, 760)

        self.processor = PDFProcessor()
        self.sections: List[BookSection] = []
        self.current_page: int = 1
        self._active_section: Optional[BookSection] = None
        self._updating_tree: bool = False
        self._render_request_id: int = 0
        self._first_fit_done: bool = False
        self._export_worker: Optional[ExportWorker] = None
        self._is_closing: bool = False

        self.thread_pool = QThreadPool(self)
        self._setup_ui()

    def _setup_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 6)
        root_layout.setSpacing(6)

        top_bar = QHBoxLayout()

        self.btn_open = QPushButton("PDF Kitap Aç")
        self.btn_open.clicked.connect(self.open_pdf_dialog)

        self.lbl_file_info = QLabel("Henüz dosya yüklenmedi")
        self.lbl_file_info.setStyleSheet("font-weight: bold; padding-left: 6px;")
        self.lbl_file_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self.btn_export_batch = QPushButton("Seçili Bölümleri Dışa Aktar")
        self.btn_export_batch.setEnabled(False)
        self.btn_export_batch.setStyleSheet(
            "background-color: #1565c0; color: white; font-weight: bold; padding: 5px 12px;"
        )
        self.btn_export_batch.clicked.connect(self.export_checked_sections)

        top_bar.addWidget(self.btn_open)
        top_bar.addWidget(self.lbl_file_info, stretch=1)
        top_bar.addWidget(self.btn_export_batch)
        root_layout.addLayout(top_bar)

        naming_bar = QHBoxLayout()
        naming_bar.addWidget(QLabel("Dosya Adı:"))

        self.txt_pattern = QLineEdit("{index}_{title}")
        self.txt_pattern.setPlaceholderText("Örn: Fizik1_{index}_{title}")
        self.txt_pattern.setToolTip(
            "Değişkenler: {index}, {title}, {book}, {start}, {end}, {label}"
        )
        self.txt_pattern.textChanged.connect(self.update_filename_preview)
        naming_bar.addWidget(self.txt_pattern, stretch=2)

        self.cmb_presets = QComboBox()
        self.cmb_presets.addItems([
            "{index}_{title}",
            "{book}_{index}_{title}",
            "{title}_s{start}-{end}",
            "DersNotu_{index}_{title}",
        ])
        self.cmb_presets.currentTextChanged.connect(self.on_preset_changed)
        naming_bar.addWidget(self.cmb_presets)

        self.lbl_name_preview = QLabel("Örnek: 01_1.1_Konu.pdf")
        self.lbl_name_preview.setStyleSheet("color: #4caf50; font-weight: bold; padding-left: 6px;")
        self.lbl_name_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        naming_bar.addWidget(self.lbl_name_preview, stretch=2)

        root_layout.addLayout(naming_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 2, 4, 0)
        left_layout.setSpacing(4)

        tree_controls_1 = QHBoxLayout()
        self.chk_hide_front = QCheckBox("Ön Sayfaları Gizle")
        self.chk_hide_front.setChecked(True)
        self.chk_hide_front.toggled.connect(self.populate_tree)

        self.chk_smart_crop = QCheckBox("Bölüm Maskeleme")
        self.chk_smart_crop.setChecked(True)
        self.chk_smart_crop.setToolTip(
            "Sayfa ortasında başlayan konuların üstündeki ve geçiş sayfasındaki sonraki konunun "
            "altındaki kısımları temizler."
        )

        tree_controls_1.addWidget(self.chk_hide_front)
        tree_controls_1.addWidget(self.chk_smart_crop)
        tree_controls_1.addStretch()
        left_layout.addLayout(tree_controls_1)

        tree_controls_2 = QHBoxLayout()
        self.btn_expand_all = QPushButton("Aç")
        self.btn_expand_all.clicked.connect(self.expand_all_tree)

        self.btn_collapse_all = QPushButton("Daralt")
        self.btn_collapse_all.clicked.connect(self.collapse_all_tree)

        self.btn_check_all = QPushButton("Tümünü Seç")
        self.btn_check_all.clicked.connect(self.select_all_sections)

        self.btn_uncheck_all = QPushButton("Seçimi Temizle")
        self.btn_uncheck_all.clicked.connect(self.uncheck_all_sections)

        tree_controls_2.addWidget(self.btn_expand_all)
        tree_controls_2.addWidget(self.btn_collapse_all)
        tree_controls_2.addWidget(self.btn_check_all)
        tree_controls_2.addWidget(self.btn_uncheck_all)
        tree_controls_2.addStretch()
        left_layout.addLayout(tree_controls_2)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(
            ["İçindekiler (Bölüm / Konu Başlığı)", "Başlangıç", "Bitiş", "Sayfa", "Kitap No"]
        )
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.open_tree_context_menu)

        self.del_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.tree)
        self.del_shortcut.activated.connect(self.delete_selected_tree_items)

        header = self.tree.header()
        header.setSectionResizeMode(self.COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_START, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_END, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_COUNT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_LABEL, QHeaderView.ResizeMode.ResizeToContents)

        self.tree.itemChanged.connect(self.on_tree_item_changed)
        self.tree.itemSelectionChanged.connect(self.on_tree_selection_changed)
        left_layout.addWidget(self.tree)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 2, 0, 0)
        right_layout.setSpacing(4)

        nav_bar = QHBoxLayout()
        self.btn_prev = QPushButton("< Önceki")
        self.btn_prev.clicked.connect(self.show_prev_page)

        self.spin_page = QSpinBox()
        self.spin_page.setMinimum(1)
        self.spin_page.setMaximum(1)
        self.spin_page.setKeyboardTracking(False)
        self.spin_page.valueChanged.connect(self.show_page)

        self.lbl_page_status = QLabel("Sayfa: 0 / 0")
        self.lbl_page_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self.btn_next = QPushButton("Sonraki >")
        self.btn_next.clicked.connect(self.show_next_page)

        self.btn_zoom_out = QPushButton("-")
        self.btn_zoom_out.setFixedWidth(28)
        self.btn_zoom_out.clicked.connect(self.zoom_out_preview)

        self.btn_fit = QPushButton("Sığdır")
        self.btn_fit.clicked.connect(self.fit_preview_width)

        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setFixedWidth(28)
        self.btn_zoom_in.clicked.connect(self.zoom_in_preview)

        nav_bar.addWidget(self.btn_prev)
        nav_bar.addWidget(self.spin_page)
        nav_bar.addWidget(self.lbl_page_status, stretch=1)
        nav_bar.addWidget(self.btn_next)
        nav_bar.addWidget(self.btn_zoom_out)
        nav_bar.addWidget(self.btn_fit)
        nav_bar.addWidget(self.btn_zoom_in)
        right_layout.addLayout(nav_bar)

        self.preview = PagePreviewWidget()
        right_layout.addWidget(self.preview, stretch=1)

        range_group = QGroupBox("Hızlı Bölme")
        range_vbox = QVBoxLayout(range_group)
        range_vbox.setSpacing(4)

        row0 = QHBoxLayout()
        self.chk_auto_start = QCheckBox("Aktif sayfayı başlangıç al")
        self.chk_auto_start.setChecked(True)
        row0.addWidget(self.chk_auto_start)

        self.lbl_range_summary = QLabel("0 sayfa")
        self.lbl_range_summary.setStyleSheet("font-weight: bold; color: #64b5f6;")
        row0.addStretch()
        row0.addWidget(self.lbl_range_summary)
        range_vbox.addLayout(row0)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Başlangıç:"))
        self.spin_range_start = QSpinBox()
        self.spin_range_start.setMinimum(1)
        self.spin_range_start.setMaximum(1)
        self.spin_range_start.valueChanged.connect(self.on_manual_start_edited)
        row1.addWidget(self.spin_range_start)

        self.btn_set_start_here = QPushButton("Sabitle")
        self.btn_set_start_here.setToolTip("Aktif sayfayı başlangıç olarak sabitler")
        self.btn_set_start_here.clicked.connect(self.lock_current_page_as_start)
        row1.addWidget(self.btn_set_start_here)

        row1.addSpacing(8)
        row1.addWidget(QLabel("Bitiş:"))
        self.spin_range_end = QSpinBox()
        self.spin_range_end.setMinimum(1)
        self.spin_range_end.setMaximum(1)
        self.spin_range_end.valueChanged.connect(self.update_range_summary)
        row1.addWidget(self.spin_range_end)

        self.btn_set_end_here = QPushButton("Bitiş Yap")
        self.btn_set_end_here.setToolTip("Aktif sayfayı bitiş sayfası yapar")
        self.btn_set_end_here.clicked.connect(self.set_current_page_as_range_end)
        row1.addWidget(self.btn_set_end_here)
        row1.addStretch()
        range_vbox.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Ad:"))
        self.txt_range_title = QLineEdit()
        self.txt_range_title.setPlaceholderText("Bölüm adı...")
        self.txt_range_title.textChanged.connect(self.update_filename_preview)
        row2.addWidget(self.txt_range_title, stretch=1)

        self.btn_add_to_tree = QPushButton("+ Listeye Ekle")
        self.btn_add_to_tree.clicked.connect(self.add_custom_range_to_tree)
        row2.addWidget(self.btn_add_to_tree)

        self.btn_save_range = QPushButton("PDF Kaydet")
        self.btn_save_range.setEnabled(False)
        self.btn_save_range.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 5px 12px;"
        )
        self.btn_save_range.clicked.connect(self.save_single_range_pdf)
        row2.addWidget(self.btn_save_range)
        range_vbox.addLayout(row2)

        right_layout.addWidget(range_group)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([580, 640])
        root_layout.addWidget(splitter, stretch=1)

        status_bar = QHBoxLayout()
        self.lbl_status = QLabel("Hazır")
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedWidth(220)
        status_bar.addWidget(self.lbl_status, stretch=1)
        status_bar.addWidget(self.progress_bar)
        root_layout.addLayout(status_bar)

    def on_preset_changed(self, text: str) -> None:
        self.txt_pattern.setText(text)

    def expand_all_tree(self) -> None:
        self.tree.expandAll()

    def collapse_all_tree(self) -> None:
        self.tree.collapseAll()

    def show_prev_page(self) -> None:
        self.show_page(self.current_page - 1)

    def show_next_page(self) -> None:
        self.show_page(self.current_page + 1)

    def zoom_out_preview(self) -> None:
        self.preview.zoom_out()

    def fit_preview_width(self) -> None:
        self.preview.fit_to_width()

    def zoom_in_preview(self) -> None:
        self.preview.zoom_in()

    def open_pdf_dialog(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "PDF Kitap Seç", "", "PDF Dosyaları (*.pdf)"
        )
        if not file_path:
            return

        try:
            self.sections = self.processor.load_pdf(file_path)
        except Exception as exc:
            QMessageBox.critical(self, "Hata", f"PDF açılırken hata oluştu:\n{exc}")
            return

        total = self.processor.total_pages
        self.lbl_file_info.setText(f"{Path(file_path).name}  ({total} sayfa)")
        self.btn_export_batch.setEnabled(total > 0)
        self.btn_save_range.setEnabled(total > 0)
        self._first_fit_done = False
        self.chk_auto_start.setChecked(True)

        first_content = next((s for s in self.sections if not s.is_front), None)
        self._active_section = first_content
        initial_start = first_content.start_page if first_content else 1
        initial_end = first_content.end_page if first_content else 1
        initial_title = first_content.title if first_content else ""

        for spin in (self.spin_page, self.spin_range_start, self.spin_range_end):
            spin.blockSignals(True)
            spin.setMaximum(max(1, total))
            spin.blockSignals(False)

        self.spin_range_start.setValue(initial_start)
        self.spin_range_end.setValue(initial_end)
        self.txt_range_title.setText(initial_title)

        self.populate_tree()
        self.update_range_summary()
        self.update_filename_preview()

        if total > 0:
            self.show_page(initial_start)

    def populate_tree(self) -> None:
        self._updating_tree = True
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()

        hide_front = self.chk_hide_front.isChecked()
        parent_stack: List[tuple[int, QTreeWidgetItem]] = []

        for idx, sec in enumerate(self.sections):
            if hide_front and sec.is_front:
                continue

            page_span = f"{sec.page_count} s."
            book_no = (
                sec.page_label
                if sec.page_label == sec.end_label or not sec.end_label
                else f"{sec.page_label}–{sec.end_label}"
            )

            item = QTreeWidgetItem([
                sec.title,
                str(sec.start_page),
                str(sec.end_page),
                page_span,
                book_no,
            ])
            item.setData(self.COL_TITLE, Qt.ItemDataRole.UserRole, idx)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(
                self.COL_TITLE,
                Qt.CheckState.Checked if sec.selected else Qt.CheckState.Unchecked,
            )

            for col in (self.COL_START, self.COL_END, self.COL_COUNT, self.COL_LABEL):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignCenter)
            item.setForeground(self.COL_LABEL, QColor("#9e9e9e"))

            while parent_stack and parent_stack[-1][0] >= sec.level:
                parent_stack.pop()

            if parent_stack:
                parent_stack[-1][1].addChild(item)
            else:
                self.tree.addTopLevelItem(item)

            parent_stack.append((sec.level, item))

        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

        self.tree.setUpdatesEnabled(True)
        self._updating_tree = False

    def open_tree_context_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        if not item:
            return
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)

        menu = QMenu(self)
        delete_action = menu.addAction("Listeden Sil")
        if menu.exec(self.tree.viewport().mapToGlobal(pos)) == delete_action:
            self.delete_selected_tree_items()

    def delete_selected_tree_items(self) -> None:
        selected_items = self.tree.selectedItems()
        if not selected_items:
            return

        indices = sorted(
            {
                item.data(self.COL_TITLE, Qt.ItemDataRole.UserRole)
                for item in selected_items
                if item.data(self.COL_TITLE, Qt.ItemDataRole.UserRole) is not None
            },
            reverse=True,
        )
        for idx in indices:
            if 0 <= idx < len(self.sections):
                self.sections.pop(idx)

        self.populate_tree()
        self.lbl_status.setText(f"{len(indices)} öğe listeden silindi.")

    def on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_tree or column != self.COL_TITLE:
            return
        idx = item.data(self.COL_TITLE, Qt.ItemDataRole.UserRole)
        if idx is not None and 0 <= idx < len(self.sections):
            self.sections[idx].selected = item.checkState(self.COL_TITLE) == Qt.CheckState.Checked

    def on_tree_selection_changed(self) -> None:
        selected_items = self.tree.selectedItems()
        if not selected_items:
            return

        indices = sorted(
            item.data(self.COL_TITLE, Qt.ItemDataRole.UserRole)
            for item in selected_items
            if item.data(self.COL_TITLE, Qt.ItemDataRole.UserRole) is not None
        )
        if not indices:
            return

        first_sec = self.sections[indices[0]]
        last_sec = self.sections[indices[-1]]
        self._active_section = first_sec if len(indices) == 1 else BookSection(
            title=first_sec.title,
            start_page=min(self.sections[i].start_page for i in indices),
            end_page=max(self.sections[i].end_page for i in indices),
            next_title=last_sec.next_title,
            next_start_page=last_sec.next_start_page,
        )

        self.chk_auto_start.blockSignals(True)
        self.chk_auto_start.setChecked(False)
        self.chk_auto_start.blockSignals(False)

        self.spin_range_start.blockSignals(True)
        self.spin_range_end.blockSignals(True)
        self.spin_range_start.setValue(self._active_section.start_page)
        self.spin_range_end.setValue(self._active_section.end_page)
        self.spin_range_start.blockSignals(False)
        self.spin_range_end.blockSignals(False)

        self.txt_range_title.setText(first_sec.title)
        self.update_range_summary()
        self.update_filename_preview()
        self.show_page(first_sec.start_page)

    def select_all_sections(self) -> None:
        hide_front = self.chk_hide_front.isChecked()
        for sec in self.sections:
            if hide_front and sec.is_front:
                continue
            sec.selected = True
        self.populate_tree()

    def uncheck_all_sections(self) -> None:
        for sec in self.sections:
            sec.selected = False
        self.populate_tree()

    def show_page(self, page_num: int) -> None:
        if self.processor.total_pages == 0:
            return

        clamped = max(1, min(page_num, self.processor.total_pages))
        self.current_page = clamped

        self.spin_page.blockSignals(True)
        self.spin_page.setValue(clamped)
        self.spin_page.blockSignals(False)

        label = self.processor.get_page_label(clamped)
        self.lbl_page_status.setText(
            f"Fiziksel: {clamped}/{self.processor.total_pages} | Kitap: {label}"
        )

        if self.chk_auto_start.isChecked():
            self._active_section = None
            self.spin_range_start.blockSignals(True)
            self.spin_range_start.setValue(clamped)
            self.spin_range_start.blockSignals(False)

            if self.spin_range_end.value() < clamped:
                self.spin_range_end.blockSignals(True)
                self.spin_range_end.setValue(clamped)
                self.spin_range_end.blockSignals(False)

            self.update_range_summary()

        self._render_request_id += 1
        task = PageRenderTask(self.processor, self._render_request_id, clamped, zoom=1.45)
        task.signals.finished.connect(self._on_page_rendered)
        self.thread_pool.start(task)

    def _on_page_rendered(self, request_id: int, page_num: int, png_bytes: Optional[bytes]) -> None:
        if self._is_closing or request_id != self._render_request_id:
            return
        self.preview.set_image_bytes(png_bytes)
        if not self._first_fit_done and png_bytes:
            self._first_fit_done = True
            self.preview.fit_to_width()

    def on_manual_start_edited(self) -> None:
        self._active_section = None
        self.chk_auto_start.blockSignals(True)
        self.chk_auto_start.setChecked(False)
        self.chk_auto_start.blockSignals(False)
        self.update_range_summary()

    def lock_current_page_as_start(self) -> None:
        if self.processor.total_pages == 0:
            return
        self._active_section = None
        self.chk_auto_start.blockSignals(True)
        self.chk_auto_start.setChecked(False)
        self.chk_auto_start.blockSignals(False)

        self.spin_range_start.setValue(self.current_page)
        if self.spin_range_end.value() < self.current_page:
            self.spin_range_end.setValue(self.current_page)
        self.lbl_status.setText(f"Başlangıç {self.current_page} olarak sabitlendi.")

    def set_current_page_as_range_end(self) -> None:
        if self.processor.total_pages == 0:
            return
        self.chk_auto_start.blockSignals(True)
        self.chk_auto_start.setChecked(False)
        self.chk_auto_start.blockSignals(False)
        self.spin_range_end.setValue(self.current_page)

    def update_range_summary(self) -> None:
        s_p = self.spin_range_start.value()
        e_p = self.spin_range_end.value()
        if s_p > e_p:
            self.lbl_range_summary.setText("Geçersiz aralık!")
            self.lbl_range_summary.setStyleSheet("font-weight: bold; color: #ef5350;")
            return

        count = e_p - s_p + 1
        s_lbl = self.processor.get_page_label(s_p)
        e_lbl = self.processor.get_page_label(e_p)
        self.lbl_range_summary.setText(f"{count} sayfa (Kitap s. {s_lbl}–{e_lbl})")
        self.lbl_range_summary.setStyleSheet("font-weight: bold; color: #64b5f6;")
        self.update_filename_preview()

    def update_filename_preview(self) -> None:
        pattern = self.txt_pattern.text()
        s_p = self.spin_range_start.value()
        e_p = self.spin_range_end.value()
        title = self.txt_range_title.text().strip() or "1.1_Konu"

        sample_sec = BookSection(
            title=title,
            start_page=s_p,
            end_page=e_p,
            page_label=self.processor.get_page_label(s_p) or "1",
        )
        sample_name = self.processor.format_output_filename(pattern, 1, sample_sec)
        self.lbl_name_preview.setText(f"Örnek: {sample_name}")

    def add_custom_range_to_tree(self) -> None:
        if self.processor.total_pages == 0:
            return
        s_p = self.spin_range_start.value()
        e_p = self.spin_range_end.value()
        if s_p > e_p:
            QMessageBox.warning(self, "Geçersiz Aralık", "Başlangıç sayfası bitişten büyük olamaz.")
            return

        title = self.txt_range_title.text().strip() or f"Bolum_s{s_p}-{e_p}"
        next_t = self._active_section.next_title if self._active_section else ""
        next_sp = self._active_section.next_start_page if self._active_section else None

        new_sec = BookSection(
            title=title,
            start_page=s_p,
            end_page=e_p,
            level=1,
            page_label=self.processor.get_page_label(s_p),
            end_label=self.processor.get_page_label(e_p),
            selected=True,
            is_front=False,
            next_title=next_t,
            next_start_page=next_sp,
        )
        self.sections.append(new_sec)
        self.populate_tree()
        self.lbl_status.setText(f"Listeye eklendi: {title} ({s_p}–{e_p})")

    def save_single_range_pdf(self) -> None:
        if self.processor.total_pages == 0:
            return

        s_p = self.spin_range_start.value()
        e_p = self.spin_range_end.value()
        if s_p > e_p:
            QMessageBox.warning(
                self,
                "Geçersiz Aralık",
                f"Başlangıç sayfası ({s_p}), bitiş sayfasından ({e_p}) büyük olamaz.",
            )
            return

        title = self.txt_range_title.text().strip() or f"Bolum_s{s_p}-{e_p}"
        next_t = self._active_section.next_title if self._active_section else ""
        next_sp = self._active_section.next_start_page if self._active_section else None

        temp_sec = BookSection(
            title=title,
            start_page=s_p,
            end_page=e_p,
            page_label=self.processor.get_page_label(s_p),
        )
        suggested_name = self.processor.format_output_filename(self.txt_pattern.text(), 1, temp_sec)

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            f"PDF Kaydet (Sayfa {s_p} – {e_p})",
            suggested_name,
            "PDF Dosyası (*.pdf)",
        )
        if not save_path:
            return

        try:
            out = self.processor.export_merged_range(
                start_page=s_p,
                end_page=e_p,
                target_file=save_path,
                title=title,
                next_title=next_t,
                next_start_page=next_sp,
                smart_crop=self.chk_smart_crop.isChecked(),
            )
            QMessageBox.information(
                self,
                "Tamamlandı",
                f"Bölüm başarıyla kaydedildi:\n{out}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Hata", f"Kayıt hatası:\n{exc}")

    def export_checked_sections(self) -> None:
        selected = [s for s in self.sections if s.selected]
        if not selected:
            QMessageBox.warning(self, "Uyarı", "Dışa aktarmak için ağaçtan en az bir başlık seçin.")
            return

        for sec in selected:
            if sec.start_page > sec.end_page:
                QMessageBox.warning(
                    self,
                    "Geçersiz Sayfa Aralığı",
                    f"'{sec.title}' bölümünde başlangıç ({sec.start_page}) bitişten ({sec.end_page}) büyük.",
                )
                return

        out_dir = QFileDialog.getExistingDirectory(self, "Bölümlerin Kaydedileceği Klasörü Seç")
        if not out_dir:
            return

        self.btn_export_batch.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(selected))
        self.progress_bar.setValue(0)

        self._export_worker = ExportWorker(
            self.processor,
            self.sections,
            out_dir,
            self.txt_pattern.text(),
            self.chk_smart_crop.isChecked(),
        )
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.completed.connect(self._on_export_completed)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.start()

    def _on_export_progress(self, current: int, total: int, filename: str) -> None:
        self.progress_bar.setValue(current)
        self.lbl_status.setText(f"Kaydediliyor ({current}/{total}): {filename}")

    def _on_export_completed(self, created: list, out_dir: str) -> None:
        self.btn_export_batch.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_status.setText(f"Tamamlandı: {len(created)} dosya oluşturuldu.")
        QMessageBox.information(
            self,
            "Tamamlandı",
            f"Toplam {len(created)} adet PDF bölümü başarıyla oluşturuldu:\n{out_dir}",
        )

    def _on_export_failed(self, error_msg: str) -> None:
        self.btn_export_batch.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_status.setText("Hata oluştu.")
        QMessageBox.critical(self, "Kayıt Hatası", f"Dosyalar oluşturulurken hata:\n{error_msg}")

    def closeEvent(self, event) -> None:
        self._is_closing = True
        self._render_request_id += 1
        self.thread_pool.clear()
        self.thread_pool.waitForDone(1500)
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.wait(2000)
        self.processor.close()
        super().closeEvent(event)