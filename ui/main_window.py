from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QPoint, QRunnable, QSettings, Qt, QThread, QThreadPool, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
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
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from core.i18n import get_language, set_language, tr
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
        group_by_chapter: bool = False,
        combine_single_pdf: bool = False,
        target_file: str = "",
    ) -> None:
        super().__init__()
        self.processor = processor
        self.sections = sections
        self.out_dir = out_dir
        self.pattern = pattern
        self.smart_crop = smart_crop
        self.group_by_chapter = group_by_chapter
        self.combine_single_pdf = combine_single_pdf
        self.target_file = target_file

    def run(self) -> None:
        try:
            if self.combine_single_pdf:
                out_file = self.processor.export_combined_sections(
                    self.sections,
                    self.target_file,
                    smart_crop=self.smart_crop,
                    progress_callback=lambda cur, tot, name: self.progress.emit(cur, tot, name),
                )
                self.completed.emit([out_file], str(out_file))
            else:
                created = self.processor.export_sections(
                    self.sections,
                    self.out_dir,
                    filename_pattern=self.pattern,
                    smart_crop=self.smart_crop,
                    group_by_chapter=self.group_by_chapter,
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

    ROLE_INDEX = Qt.ItemDataRole.UserRole
    ROLE_UID = Qt.ItemDataRole.UserRole + 1

    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("ZayuR3X", "PDFChapterSplitter")
        saved_lang = str(self.settings.value("language", "en"))
        set_language(saved_lang)

        self.setMinimumSize(920, 600)
        self.resize(1220, 760)
        self.setAcceptDrops(True)

        self.processor = PDFProcessor()
        self.sections: List[BookSection] = []
        self._undo_stack: List[List[BookSection]] = []
        self.current_page: int = 1
        self._active_section: Optional[BookSection] = None
        self._updating_tree: bool = False
        self._render_request_id: int = 0
        self._first_fit_done: bool = False
        self._export_worker: Optional[ExportWorker] = None
        self._is_closing: bool = False
        self._status_key: str = "status_ready"
        self._status_kwargs: Dict[str, Any] = {}

        self.thread_pool = QThreadPool(self)
        self._setup_ui()
        self.retranslate_ui()

    def _set_status(self, key: str, **kwargs: Any) -> None:
        self._status_key = key
        self._status_kwargs = kwargs
        self.lbl_status.setText(tr(key, **kwargs))

    def _get_saved_dir(self, key: str) -> str:
        val = str(self.settings.value(key, ""))
        return val if val and Path(val).exists() else ""

    def _save_dir(self, key: str, path_str: str) -> None:
        p = Path(path_str)
        target = p if p.is_dir() else p.parent
        if target.exists():
            self.settings.setValue(key, str(target))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                event.acceptProposedAction()
                self._load_pdf_file(url.toLocalFile())
                return

    def _setup_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 6)
        root_layout.setSpacing(6)

        top_bar = QHBoxLayout()

        self.btn_open = QPushButton()
        self.btn_open.clicked.connect(self.open_pdf_dialog)

        self.lbl_file_info = QLabel()
        self.lbl_file_info.setStyleSheet("font-weight: bold; padding-left: 6px;")
        self.lbl_file_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self.btn_export_combined = QPushButton()
        self.btn_export_combined.setEnabled(False)
        self.btn_export_combined.setStyleSheet(
            "background-color: #00838f; color: white; font-weight: bold; padding: 5px 10px;"
        )
        self.btn_export_combined.clicked.connect(self.export_combined_pdf)

        self.btn_export_batch = QPushButton()
        self.btn_export_batch.setEnabled(False)
        self.btn_export_batch.setStyleSheet(
            "background-color: #1565c0; color: white; font-weight: bold; padding: 5px 12px;"
        )
        self.btn_export_batch.clicked.connect(self.export_checked_sections)

        self.cmb_lang = QComboBox()
        self.cmb_lang.setFixedWidth(60)
        self.cmb_lang.addItem("EN", "en")
        self.cmb_lang.addItem("TR", "tr")
        lang_idx = self.cmb_lang.findData(get_language())
        if lang_idx >= 0:
            self.cmb_lang.setCurrentIndex(lang_idx)
        self.cmb_lang.currentIndexChanged.connect(self.on_language_changed)

        top_bar.addWidget(self.btn_open)
        top_bar.addWidget(self.lbl_file_info, stretch=1)
        top_bar.addWidget(self.btn_export_combined)
        top_bar.addWidget(self.btn_export_batch)
        top_bar.addWidget(self.cmb_lang)
        root_layout.addLayout(top_bar)

        naming_bar = QHBoxLayout()
        self.lbl_filename = QLabel()
        naming_bar.addWidget(self.lbl_filename)

        self.txt_pattern = QLineEdit("{index}_{title}")
        self.txt_pattern.textChanged.connect(self.update_filename_preview)
        naming_bar.addWidget(self.txt_pattern, stretch=2)

        self.cmb_presets = QComboBox()
        self.cmb_presets.currentTextChanged.connect(self.on_preset_changed)
        naming_bar.addWidget(self.cmb_presets)

        self.lbl_name_preview = QLabel()
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
        self.chk_hide_front = QCheckBox()
        self.chk_hide_front.setChecked(True)
        self.chk_hide_front.toggled.connect(self.populate_tree)

        self.chk_smart_crop = QCheckBox()
        self.chk_smart_crop.setChecked(True)

        self.chk_group_folders = QCheckBox()
        self.chk_group_folders.setChecked(False)

        tree_controls_1.addWidget(self.chk_hide_front)
        tree_controls_1.addWidget(self.chk_smart_crop)
        tree_controls_1.addWidget(self.chk_group_folders)
        tree_controls_1.addStretch()
        left_layout.addLayout(tree_controls_1)

        self.txt_tree_search = QLineEdit()
        self.txt_tree_search.setClearButtonEnabled(True)
        self.txt_tree_search.textChanged.connect(self.filter_tree)
        left_layout.addWidget(self.txt_tree_search)

        tree_controls_2 = QHBoxLayout()
        self.btn_expand_all = QPushButton()
        self.btn_expand_all.clicked.connect(self.expand_all_tree)

        self.btn_collapse_all = QPushButton()
        self.btn_collapse_all.clicked.connect(self.collapse_all_tree)

        self.btn_select_menu = QPushButton()
        self.btn_select_menu.setEnabled(False)
        select_menu = QMenu(self.btn_select_menu)
        self.act_select_chapters = select_menu.addAction("")
        self.act_select_chapters.triggered.connect(self.select_chapters_only)
        self.act_select_all = select_menu.addAction("")
        self.act_select_all.triggered.connect(self.select_all_visible)
        self.btn_select_menu.setMenu(select_menu)

        self.btn_uncheck_all = QPushButton()
        self.btn_uncheck_all.setEnabled(False)
        self.btn_uncheck_all.clicked.connect(self.uncheck_all_sections)

        self.btn_delete_selected = QPushButton()
        self.btn_delete_selected.setEnabled(False)
        self.btn_delete_selected.clicked.connect(self.delete_selected_tree_items)

        self.btn_undo_delete = QPushButton()
        self.btn_undo_delete.setEnabled(False)
        self.btn_undo_delete.clicked.connect(self.undo_delete)

        tree_controls_2.addWidget(self.btn_expand_all)
        tree_controls_2.addWidget(self.btn_collapse_all)
        tree_controls_2.addWidget(self.btn_select_menu)
        tree_controls_2.addWidget(self.btn_uncheck_all)
        tree_controls_2.addWidget(self.btn_delete_selected)
        tree_controls_2.addWidget(self.btn_undo_delete)
        tree_controls_2.addStretch()
        left_layout.addLayout(tree_controls_2)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.open_tree_context_menu)
        self.tree.itemDoubleClicked.connect(self.on_tree_item_double_clicked)

        self.del_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.tree)
        self.del_shortcut.activated.connect(self.delete_selected_tree_items)

        self.undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        self.undo_shortcut.activated.connect(self.undo_delete)

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
        self.btn_prev = QPushButton()
        self.btn_prev.clicked.connect(self.show_prev_page)

        self.spin_page = QSpinBox()
        self.spin_page.setMinimum(1)
        self.spin_page.setMaximum(1)
        self.spin_page.setKeyboardTracking(False)
        self.spin_page.valueChanged.connect(self.show_page)

        self.lbl_page_status = QLabel()
        self.lbl_page_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self.btn_next = QPushButton()
        self.btn_next.clicked.connect(self.show_next_page)

        self.btn_zoom_out = QPushButton("-")
        self.btn_zoom_out.setFixedWidth(28)
        self.btn_zoom_out.clicked.connect(self.zoom_out_preview)

        self.btn_fit = QPushButton()
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

        self.range_group = QGroupBox()
        range_vbox = QVBoxLayout(self.range_group)
        range_vbox.setSpacing(4)

        row0 = QHBoxLayout()
        self.chk_auto_start = QCheckBox()
        self.chk_auto_start.setChecked(True)
        row0.addWidget(self.chk_auto_start)

        self.lbl_range_summary = QLabel()
        self.lbl_range_summary.setStyleSheet("font-weight: bold; color: #64b5f6;")
        row0.addStretch()
        row0.addWidget(self.lbl_range_summary)
        range_vbox.addLayout(row0)

        row1 = QHBoxLayout()
        self.lbl_start = QLabel()
        row1.addWidget(self.lbl_start)
        self.spin_range_start = QSpinBox()
        self.spin_range_start.setMinimum(1)
        self.spin_range_start.setMaximum(1)
        self.spin_range_start.valueChanged.connect(self.on_manual_start_edited)
        row1.addWidget(self.spin_range_start)

        self.btn_set_start_here = QPushButton()
        self.btn_set_start_here.clicked.connect(self.lock_current_page_as_start)
        row1.addWidget(self.btn_set_start_here)

        row1.addSpacing(8)
        self.lbl_end = QLabel()
        row1.addWidget(self.lbl_end)
        self.spin_range_end = QSpinBox()
        self.spin_range_end.setMinimum(1)
        self.spin_range_end.setMaximum(1)
        self.spin_range_end.valueChanged.connect(self.update_range_summary)
        row1.addWidget(self.spin_range_end)

        self.btn_set_end_here = QPushButton()
        self.btn_set_end_here.clicked.connect(self.set_current_page_as_range_end)
        row1.addWidget(self.btn_set_end_here)
        row1.addStretch()
        range_vbox.addLayout(row1)

        row2 = QHBoxLayout()
        self.lbl_name = QLabel()
        row2.addWidget(self.lbl_name)
        self.txt_range_title = QLineEdit()
        self.txt_range_title.textChanged.connect(self.update_filename_preview)
        row2.addWidget(self.txt_range_title, stretch=1)

        self.btn_add_to_tree = QPushButton()
        self.btn_add_to_tree.clicked.connect(self.add_custom_range_to_tree)
        row2.addWidget(self.btn_add_to_tree)

        self.btn_save_range = QPushButton()
        self.btn_save_range.setEnabled(False)
        self.btn_save_range.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 5px 12px;"
        )
        self.btn_save_range.clicked.connect(self.save_single_range_pdf)
        row2.addWidget(self.btn_save_range)
        range_vbox.addLayout(row2)

        right_layout.addWidget(self.range_group)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([580, 640])
        root_layout.addWidget(splitter, stretch=1)

        status_bar = QHBoxLayout()
        self.lbl_status = QLabel()
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedWidth(220)
        status_bar.addWidget(self.lbl_status, stretch=1)
        status_bar.addWidget(self.progress_bar)
        root_layout.addLayout(status_bar)

    def on_language_changed(self, index: int) -> None:
        lang = self.cmb_lang.itemData(index)
        if not lang or lang == get_language():
            return
        set_language(lang)
        self.settings.setValue("language", lang)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(tr("app_title"))

        self.btn_open.setText(tr("btn_open"))
        self.btn_open.setToolTip(tr("btn_open_tip"))
        self.cmb_lang.setToolTip(tr("lang_tooltip"))

        if self.processor.total_pages > 0 and self.processor.file_path:
            self.lbl_file_info.setText(
                tr("file_info", name=self.processor.file_path.name, total=self.processor.total_pages)
            )
        else:
            self.lbl_file_info.setText(tr("no_file_loaded"))

        self.btn_export_combined.setToolTip(tr("btn_combine_tip"))
        self.btn_export_batch.setToolTip(tr("btn_export_tip"))

        self.lbl_filename.setText(tr("lbl_filename"))
        self.txt_pattern.setPlaceholderText(tr("pattern_placeholder"))
        self.txt_pattern.setToolTip(tr("pattern_tip"))

        prev_preset_idx = max(0, self.cmb_presets.currentIndex())
        self.cmb_presets.blockSignals(True)
        self.cmb_presets.clear()
        self.cmb_presets.addItems([
            "{index}_{title}",
            "{book}_{index}_{title}",
            tr("preset_pages"),
            tr("preset_notes"),
        ])
        self.cmb_presets.setCurrentIndex(min(prev_preset_idx, self.cmb_presets.count() - 1))
        self.cmb_presets.blockSignals(False)

        self.chk_hide_front.setText(tr("chk_hide_front"))
        self.chk_smart_crop.setText(tr("chk_smart_crop"))
        self.chk_smart_crop.setToolTip(tr("chk_smart_crop_tip"))
        self.chk_group_folders.setText(tr("chk_group_folders"))
        self.chk_group_folders.setToolTip(tr("chk_group_folders_tip"))

        self.txt_tree_search.setPlaceholderText(tr("search_placeholder"))

        self.btn_expand_all.setText(tr("btn_expand"))
        self.btn_expand_all.setToolTip(tr("btn_expand_tip"))
        self.btn_collapse_all.setText(tr("btn_collapse"))
        self.btn_collapse_all.setToolTip(tr("btn_collapse_tip"))
        self.btn_select_menu.setText(tr("btn_select"))
        self.btn_select_menu.setToolTip(tr("btn_select_tip"))
        self.act_select_chapters.setText(tr("act_select_chapters"))
        self.act_select_all.setText(tr("act_select_all"))
        self.btn_uncheck_all.setToolTip(tr("btn_uncheck_tip"))
        self.btn_delete_selected.setText(tr("btn_delete"))
        self.btn_delete_selected.setToolTip(tr("btn_delete_tip"))
        self.btn_undo_delete.setText(tr("btn_undo"))
        self.btn_undo_delete.setToolTip(tr("btn_undo_tip"))

        self.tree.setHeaderLabels([
            tr("col_title"),
            tr("col_start"),
            tr("col_end"),
            tr("col_pages"),
            tr("col_book_no"),
        ])

        self._updating_tree = True
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            idx = item.data(self.COL_TITLE, self.ROLE_INDEX)
            if idx is not None and 0 <= idx < len(self.sections):
                item.setText(self.COL_COUNT, tr("page_span_short", count=self.sections[idx].page_count))
            iterator += 1
        self._updating_tree = False

        self.btn_prev.setText(tr("btn_prev"))
        self.btn_next.setText(tr("btn_next"))
        self.btn_fit.setText(tr("btn_fit"))
        if self.processor.total_pages > 0:
            label = self.processor.get_page_label(self.current_page)
            self.lbl_page_status.setText(
                tr("page_status", current=self.current_page, total=self.processor.total_pages, label=label)
            )
        else:
            self.lbl_page_status.setText(tr("page_status_empty"))

        self.preview.retranslate_ui()

        self.range_group.setTitle(tr("quick_split"))
        self.chk_auto_start.setText(tr("chk_auto_start"))
        self.lbl_start.setText(tr("lbl_start"))
        self.btn_set_start_here.setText(tr("btn_lock_start"))
        self.btn_set_start_here.setToolTip(tr("btn_lock_start_tip"))
        self.lbl_end.setText(tr("lbl_end"))
        self.btn_set_end_here.setText(tr("btn_set_end"))
        self.btn_set_end_here.setToolTip(tr("btn_set_end_tip"))
        self.lbl_name.setText(tr("lbl_name"))
        self.txt_range_title.setPlaceholderText(tr("range_title_placeholder"))
        self.btn_add_to_tree.setText(tr("btn_add_list"))
        self.btn_save_range.setText(tr("btn_save_pdf"))

        self._update_checked_count()
        self.update_range_summary()
        self.update_filename_preview()
        self.lbl_status.setText(tr(self._status_key, **self._status_kwargs))

    def on_preset_changed(self, text: str) -> None:
        if text:
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

    def _update_checked_count(self) -> None:
        count = sum(1 for sec in self.sections if sec.selected)
        has_doc = self.processor.total_pages > 0
        self.btn_uncheck_all.setText(tr("btn_uncheck_count", count=count) if count else tr("btn_uncheck"))
        self.btn_uncheck_all.setEnabled(count > 0)
        self.btn_export_combined.setText(tr("btn_combine_count", count=count) if count else tr("btn_combine"))
        self.btn_export_combined.setEnabled(has_doc and count > 0)
        self.btn_export_batch.setText(tr("btn_export_count", count=count) if count else tr("btn_export"))
        self.btn_export_batch.setEnabled(has_doc and count > 0)

    def _sync_tree_checkstates(self) -> None:
        self._updating_tree = True
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            idx = item.data(self.COL_TITLE, self.ROLE_INDEX)
            if idx is not None and 0 <= idx < len(self.sections):
                item.setCheckState(
                    self.COL_TITLE,
                    Qt.CheckState.Checked if self.sections[idx].selected else Qt.CheckState.Unchecked,
                )
            iterator += 1
        self._updating_tree = False
        self._update_checked_count()

    def open_pdf_dialog(self) -> None:
        start_dir = self._get_saved_dir("last_open_dir")
        file_path, _ = QFileDialog.getOpenFileName(
            self, tr("dlg_open_title"), start_dir, tr("dlg_pdf_filter")
        )
        if file_path:
            self._load_pdf_file(file_path)

    def _load_pdf_file(self, file_path: str) -> None:
        try:
            self.sections = self.processor.load_pdf(file_path)
        except Exception as exc:
            QMessageBox.critical(self, tr("msg_error_title"), tr("msg_open_error", error=exc))
            return

        self._save_dir("last_open_dir", file_path)
        self._undo_stack.clear()
        self.btn_undo_delete.setEnabled(False)

        for sec in self.sections:
            sec.selected = False

        total = self.processor.total_pages
        self.lbl_file_info.setText(tr("file_info", name=Path(file_path).name, total=total))
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

        self.txt_tree_search.blockSignals(True)
        self.txt_tree_search.clear()
        self.txt_tree_search.blockSignals(False)

        self.tree.clear()
        self.populate_tree()
        self.update_range_summary()
        self.update_filename_preview()
        self._set_status("status_loaded", name=Path(file_path).name)

        if total > 0:
            self.show_page(initial_start)

    def populate_tree(self) -> None:
        expanded_uids = set()
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            it = iterator.value()
            if it.isExpanded():
                uid = it.data(self.COL_TITLE, self.ROLE_UID)
                if uid is not None:
                    expanded_uids.add(uid)
            iterator += 1

        self._updating_tree = True
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()

        hide_front = self.chk_hide_front.isChecked()
        parent_stack: List[tuple[int, QTreeWidgetItem]] = []

        for idx, sec in enumerate(self.sections):
            if hide_front and sec.is_front:
                continue

            page_span = tr("page_span_short", count=sec.page_count)
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
            item.setData(self.COL_TITLE, self.ROLE_INDEX, idx)
            item.setData(self.COL_TITLE, self.ROLE_UID, id(sec))
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEditable
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

            if id(sec) in expanded_uids:
                item.setExpanded(True)

            parent_stack.append((sec.level, item))

        self.tree.setUpdatesEnabled(True)
        self._updating_tree = False
        self.btn_delete_selected.setEnabled(False)
        self.btn_select_menu.setEnabled(self.tree.topLevelItemCount() > 0)
        self._update_checked_count()
        if self.txt_tree_search.text().strip():
            self.filter_tree()

    def filter_tree(self) -> None:
        query = self.txt_tree_search.text().strip().lower()

        def apply_filter(item: QTreeWidgetItem) -> bool:
            self_matches = not query or query in item.text(self.COL_TITLE).lower()
            child_matches = False
            for i in range(item.childCount()):
                if apply_filter(item.child(i)):
                    child_matches = True
            visible = self_matches or child_matches
            item.setHidden(not visible)

            if query and not self_matches:
                item.setForeground(self.COL_TITLE, QColor("#888888"))
            else:
                item.setData(self.COL_TITLE, Qt.ItemDataRole.ForegroundRole, None)

            if query and child_matches:
                item.setExpanded(True)
            return visible

        self.tree.setUpdatesEnabled(False)
        for i in range(self.tree.topLevelItemCount()):
            apply_filter(self.tree.topLevelItem(i))
        self.tree.setUpdatesEnabled(True)

    def on_tree_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if column == self.COL_TITLE:
            self.tree.editItem(item, self.COL_TITLE)

    def open_tree_context_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        if not item:
            return
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)

        menu = QMenu(self)
        rename_action = menu.addAction(tr("ctx_rename"))
        delete_action = menu.addAction(tr("ctx_delete"))
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == rename_action:
            self.tree.editItem(item, self.COL_TITLE)
        elif chosen == delete_action:
            self.delete_selected_tree_items()

    def delete_selected_tree_items(self) -> None:
        selected_items = self.tree.selectedItems()
        if not selected_items:
            return

        def collect_subtree_indices(item: QTreeWidgetItem, out: set[int]) -> None:
            if item.isHidden():
                return
            idx = item.data(self.COL_TITLE, self.ROLE_INDEX)
            if idx is not None:
                out.add(idx)
            for i in range(item.childCount()):
                collect_subtree_indices(item.child(i), out)

        target_indices: set[int] = set()
        for item in selected_items:
            collect_subtree_indices(item, target_indices)

        indices = sorted(target_indices, reverse=True)
        if not indices:
            return

        self._undo_stack.append(list(self.sections))
        self._updating_tree = True
        for idx in indices:
            if 0 <= idx < len(self.sections):
                self.sections.pop(idx)

        self._active_section = None
        self.populate_tree()
        self.btn_undo_delete.setEnabled(True)
        self._set_status("status_deleted", count=len(indices))

    def undo_delete(self) -> None:
        if not self._undo_stack:
            return
        self.sections = self._undo_stack.pop()
        self._active_section = None
        self.populate_tree()
        self.btn_undo_delete.setEnabled(bool(self._undo_stack))
        self._set_status("status_undone")

    def on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_tree or column != self.COL_TITLE:
            return
        idx = item.data(self.COL_TITLE, self.ROLE_INDEX)
        if idx is None or not (0 <= idx < len(self.sections)):
            return

        sec = self.sections[idx]
        sec.selected = item.checkState(self.COL_TITLE) == Qt.CheckState.Checked
        self._update_checked_count()

        new_title = item.text(self.COL_TITLE).strip()
        if new_title and new_title != sec.title:
            sec.title = new_title
            if self._active_section is sec:
                self.txt_range_title.setText(new_title)
            self.update_filename_preview()
            self._set_status("status_renamed", title=new_title)
        elif not new_title:
            self._updating_tree = True
            item.setText(self.COL_TITLE, sec.title)
            self._updating_tree = False

    def on_tree_selection_changed(self) -> None:
        if self._updating_tree:
            return
        selected_items = [it for it in self.tree.selectedItems() if not it.isHidden()]
        self.btn_delete_selected.setEnabled(bool(selected_items))
        if not selected_items:
            return

        indices = sorted(
            item.data(self.COL_TITLE, self.ROLE_INDEX)
            for item in selected_items
            if item.data(self.COL_TITLE, self.ROLE_INDEX) is not None
            and 0 <= item.data(self.COL_TITLE, self.ROLE_INDEX) < len(self.sections)
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

    def select_chapters_only(self) -> None:
        for sec in self.sections:
            sec.selected = False

        for i in range(self.tree.topLevelItemCount()):
            top_item = self.tree.topLevelItem(i)
            if top_item.isHidden():
                continue
            idx = top_item.data(self.COL_TITLE, self.ROLE_INDEX)
            if idx is not None and 0 <= idx < len(self.sections):
                self.sections[idx].selected = True

        self._sync_tree_checkstates()

    def select_all_visible(self) -> None:
        query = self.txt_tree_search.text().strip().lower()
        for sec in self.sections:
            sec.selected = False

        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            if not item.isHidden():
                if query and query not in item.text(self.COL_TITLE).lower():
                    iterator += 1
                    continue
                idx = item.data(self.COL_TITLE, self.ROLE_INDEX)
                if idx is not None and 0 <= idx < len(self.sections):
                    self.sections[idx].selected = True
            iterator += 1

        self._sync_tree_checkstates()

    def uncheck_all_sections(self) -> None:
        for sec in self.sections:
            sec.selected = False
        self._sync_tree_checkstates()

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
            tr("page_status", current=clamped, total=self.processor.total_pages, label=label)
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
        self._set_status("status_start_locked", page=self.current_page)

    def set_current_page_as_range_end(self) -> None:
        if self.processor.total_pages == 0:
            return
        self.chk_auto_start.blockSignals(True)
        self.chk_auto_start.setChecked(False)
        self.chk_auto_start.blockSignals(False)
        self.spin_range_end.setValue(self.current_page)

    def update_range_summary(self) -> None:
        if self.processor.total_pages == 0:
            self.lbl_range_summary.setText(tr("range_zero"))
            self.lbl_range_summary.setStyleSheet("font-weight: bold; color: #64b5f6;")
            return

        s_p = self.spin_range_start.value()
        e_p = self.spin_range_end.value()
        if s_p > e_p:
            self.lbl_range_summary.setText(tr("range_invalid"))
            self.lbl_range_summary.setStyleSheet("font-weight: bold; color: #ef5350;")
            return

        count = e_p - s_p + 1
        s_lbl = self.processor.get_page_label(s_p)
        e_lbl = self.processor.get_page_label(e_p)
        key = "range_summary_one" if count == 1 else "range_summary_many"
        self.lbl_range_summary.setText(tr(key, count=count, start=s_lbl, end=e_lbl))
        self.lbl_range_summary.setStyleSheet("font-weight: bold; color: #64b5f6;")
        self.update_filename_preview()

    def update_filename_preview(self) -> None:
        pattern = self.txt_pattern.text()
        s_p = self.spin_range_start.value()
        e_p = self.spin_range_end.value()
        title = self.txt_range_title.text().strip() or tr("sample_topic")

        sample_sec = BookSection(
            title=title,
            start_page=s_p,
            end_page=e_p,
            page_label=self.processor.get_page_label(s_p) or "1",
        )
        sample_name = self.processor.format_output_filename(pattern, 1, sample_sec)
        self.lbl_name_preview.setText(tr("name_preview", name=sample_name))

    def add_custom_range_to_tree(self) -> None:
        if self.processor.total_pages == 0:
            return
        s_p = self.spin_range_start.value()
        e_p = self.spin_range_end.value()
        if s_p > e_p:
            QMessageBox.warning(self, tr("msg_invalid_range_title"), tr("msg_start_gt_end"))
            return

        title = self.txt_range_title.text().strip() or tr("default_range_title", start=s_p, end=e_p)
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
        self._set_status("status_added", title=title, start=s_p, end=e_p)

    def save_single_range_pdf(self) -> None:
        if self.processor.total_pages == 0:
            return

        s_p = self.spin_range_start.value()
        e_p = self.spin_range_end.value()
        if s_p > e_p:
            QMessageBox.warning(
                self,
                tr("msg_invalid_range_title"),
                tr("msg_start_gt_end_detail", start=s_p, end=e_p),
            )
            return

        title = self.txt_range_title.text().strip() or tr("default_range_title", start=s_p, end=e_p)
        next_t = self._active_section.next_title if self._active_section else ""
        next_sp = self._active_section.next_start_page if self._active_section else None

        temp_sec = BookSection(
            title=title,
            start_page=s_p,
            end_page=e_p,
            page_label=self.processor.get_page_label(s_p),
        )
        suggested_name = self.processor.format_output_filename(self.txt_pattern.text(), 1, temp_sec)
        last_save_dir = self._get_saved_dir("last_save_dir")
        initial_target = str(Path(last_save_dir) / suggested_name) if last_save_dir else suggested_name

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            tr("dlg_save_range_title", start=s_p, end=e_p),
            initial_target,
            tr("dlg_pdf_filter"),
        )
        if not save_path:
            return

        self._save_dir("last_save_dir", save_path)

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
            QMessageBox.information(self, tr("msg_done_title"), tr("msg_range_saved", path=out))
        except Exception as exc:
            QMessageBox.critical(self, tr("msg_error_title"), tr("msg_save_error", error=exc))

    def _validate_selected_sections(self) -> Optional[List[BookSection]]:
        selected = [s for s in self.sections if s.selected]
        if not selected:
            QMessageBox.warning(self, tr("msg_warning_title"), tr("msg_select_at_least_one"))
            return None

        for sec in selected:
            if sec.start_page > sec.end_page:
                QMessageBox.warning(
                    self,
                    tr("msg_invalid_section_range_title"),
                    tr(
                        "msg_invalid_section_range",
                        title=sec.title,
                        start=sec.start_page,
                        end=sec.end_page,
                    ),
                )
                return None
        return selected

    def export_combined_pdf(self) -> None:
        selected = self._validate_selected_sections()
        if not selected:
            return

        suggested_name = f"{self.processor.book_stem}_{tr('combined_suffix')}.pdf"
        last_save_dir = self._get_saved_dir("last_save_dir")
        initial_target = str(Path(last_save_dir) / suggested_name) if last_save_dir else suggested_name

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            tr("dlg_combine_title", count=len(selected)),
            initial_target,
            tr("dlg_pdf_filter"),
        )
        if not save_path:
            return

        self._save_dir("last_save_dir", save_path)
        self.btn_export_batch.setEnabled(False)
        self.btn_export_combined.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(selected))
        self.progress_bar.setValue(0)

        self._export_worker = ExportWorker(
            self.processor,
            self.sections,
            "",
            self.txt_pattern.text(),
            self.chk_smart_crop.isChecked(),
            combine_single_pdf=True,
            target_file=save_path,
        )
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.completed.connect(self._on_combined_completed)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.start()

    def export_checked_sections(self) -> None:
        selected = self._validate_selected_sections()
        if not selected:
            return

        last_save_dir = self._get_saved_dir("last_save_dir")
        out_dir = QFileDialog.getExistingDirectory(
            self, tr("dlg_export_dir_title"), last_save_dir
        )
        if not out_dir:
            return

        self._save_dir("last_save_dir", out_dir)

        self.btn_export_batch.setEnabled(False)
        self.btn_export_combined.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(selected))
        self.progress_bar.setValue(0)

        self._export_worker = ExportWorker(
            self.processor,
            self.sections,
            out_dir,
            self.txt_pattern.text(),
            self.chk_smart_crop.isChecked(),
            group_by_chapter=self.chk_group_folders.isChecked(),
        )
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.completed.connect(self._on_export_completed)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.start()

    def _on_export_progress(self, current: int, total: int, filename: str) -> None:
        self.progress_bar.setValue(current)
        self._set_status("status_processing", current=current, total=total, name=filename)

    def _on_combined_completed(self, created: list, out_file: str) -> None:
        self.progress_bar.setVisible(False)
        self._update_checked_count()
        self._set_status("status_combined_done", name=Path(out_file).name)
        QMessageBox.information(
            self,
            tr("msg_done_title"),
            tr("msg_combined_saved", path=out_file),
        )

    def _on_export_completed(self, created: list, out_dir: str) -> None:
        self.progress_bar.setVisible(False)
        self._update_checked_count()
        self._set_status("status_export_done", count=len(created))
        QMessageBox.information(
            self,
            tr("msg_done_title"),
            tr("msg_export_saved", count=len(created), path=out_dir),
        )

    def _on_export_failed(self, error_msg: str) -> None:
        self.progress_bar.setVisible(False)
        self._update_checked_count()
        self._set_status("status_error")
        QMessageBox.critical(
            self, tr("msg_export_error_title"), tr("msg_export_error", error=error_msg)
        )

    def closeEvent(self, event) -> None:
        self._is_closing = True
        self._render_request_id += 1
        self.thread_pool.clear()
        self.thread_pool.waitForDone(1500)
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.wait(2000)
        self.processor.close()
        super().closeEvent(event)