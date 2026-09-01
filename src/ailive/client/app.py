from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QCloseEvent,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QTextDocument,
    QTextLayout,
)
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ailive.client.audio import AudioLine, AudioQueuePlayer
from ailive.client.local_synthesis import (
    cleanup_history,
    prepare_output_batch,
    unique_wav_path,
    write_audio_tokens_wav,
)
from ailive.client.network import (
    TTSWorker,
    VoiceSyncWorker,
    list_voices,
    server_url_from_parts,
    upload_voice,
)
from ailive.client.storage import application_root, prepare_user_data
from ailive.client.update_service import (
    APP_VERSION,
    UpdateCheckWorker,
    UpdateDownloadWorker,
    launch_rollback,
    launch_updater,
)
from ailive.domain import ScriptLine
from ailive.parser import (
    SPECIAL_MARKER_PATTERN,
    parse_script,
    resolve_random_choices,
    split_script_sentences,
)


def natural_sort_key(value: object) -> list[object]:
    """Sort numbered names as 1, 2, 10 instead of 1, 10, 2."""
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value))
    ]


class ScriptLineEditor(QTextEdit):
    enterPressed = Signal(str, int)
    emptyRowDeleteRequested = Signal()
    selectAllScriptRequested = Signal()

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(2)

    def text(self) -> str:
        return self.toPlainText().replace("\n", "")

    def setText(self, text: str) -> None:
        self.setPlainText(text)

    def cursorPosition(self) -> int:
        return self.textCursor().position()

    def setCursorPosition(self, position: int) -> None:
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(position, len(self.toPlainText()))))
        self.setTextCursor(cursor)

    def cursorPositionAt(self, position: object) -> int:
        return self.cursorForPosition(position).position()

    def insert(self, text: str) -> None:
        self.textCursor().insertText(text)

    def keyPressEvent(self, event: object) -> None:
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.selectAllScriptRequested.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.enterPressed.emit(self.text(), self.cursorPosition())
            return
        if (
            event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)
            and not self.toPlainText()
        ):
            self.emptyRowDeleteRequested.emit()
            return
        super().keyPressEvent(event)


class ScriptTableWidget(QTableWidget):
    deleteRequested = Signal()
    undoRequested = Signal()
    pasteRequested = Signal(str)
    enterOnScriptRequested = Signal(int)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._press_position: object | None = None
        self._press_index: object | None = None

    def mousePressEvent(self, event: object) -> None:
        index = self.indexAt(event.position().toPoint())
        can_edit = (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and index.isValid()
            and index.column() == 3
        )
        self._press_position = event.position().toPoint() if can_edit else None
        self._press_index = index if can_edit else None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: object) -> None:
        release_position = event.position().toPoint()
        press_position = self._press_position
        press_index = self._press_index
        super().mouseReleaseEvent(event)
        self._press_position = None
        self._press_index = None
        if press_position is None or press_index is None or not press_index.isValid():
            return
        distance = (release_position - press_position).manhattanLength()
        if distance < QApplication.startDragDistance():
            clicked_row = press_index.row()
            self.edit(press_index)
            QTimer.singleShot(
                0,
                lambda row=clicked_row, point=release_position: self._place_editor_cursor(
                    row, point
                ),
            )

    def _place_editor_cursor(self, row: int, viewport_position: object) -> None:
        editor = QApplication.focusWidget()
        if not isinstance(editor, ScriptLineEditor):
            return
        if int(editor.property("script_row")) != row:
            return
        local_position = editor.mapFrom(self.viewport(), viewport_position)
        editor.setCursorPosition(editor.cursorPositionAt(local_position))

    def keyPressEvent(self, event: object) -> None:
        if event.matches(QKeySequence.StandardKey.Paste):
            clipboard_text = QApplication.clipboard().text()
            if clipboard_text:
                self.pasteRequested.emit(clipboard_text)
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_script_column()
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undoRequested.emit()
            return
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and self.currentRow() >= 0
            and self.currentColumn() == 3
        ):
            self.enterOnScriptRequested.emit(self.currentRow())
            return
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and (
            self.selectedIndexes() or self.currentRow() >= 0
        ):
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)

    def select_script_column(self) -> None:
        self.clearSelection()
        if self.rowCount() > 0:
            self.setRangeSelected(
                QTableWidgetSelectionRange(0, 3, self.rowCount() - 1, 3),
                True,
            )
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)


class ScriptTextDelegate(QStyledItemDelegate):
    advanceRequested = Signal(int, str, int)
    emptyRowDeleteRequested = Signal(int)
    cursorTracked = Signal(int, int)
    selectAllScriptRequested = Signal()

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: object
    ) -> None:
        styled_option = QStyleOptionViewItem(option)
        self.initStyleOption(styled_option, index)
        text = styled_option.text
        styled_option.text = ""
        style = (
            styled_option.widget.style()
            if styled_option.widget is not None
            else QApplication.style()
        )
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled_option, painter)

        painter.save()
        text_rect = styled_option.rect.adjusted(6, 2, -5, -2)
        painter.setClipRect(text_rect)
        is_selected = bool(styled_option.state & QStyle.StateFlag.State_Selected)
        normal_color = QColor("#edf2f5" if is_selected else "#d7e0e6")
        pause_color = QColor("#d6a057")
        document = QTextDocument()
        document.setDefaultFont(styled_option.font)
        document.setDocumentMargin(0)
        document.setTextWidth(max(1, text_rect.width()))
        document.setPlainText(text)
        layout = document.firstBlock().layout()
        formats: list[object] = []
        for match in SPECIAL_MARKER_PATTERN.finditer(text):
            marker_format = QTextLayout.FormatRange()
            marker_format.start = match.start()
            marker_format.length = match.end() - match.start()
            marker_format.format.setForeground(pause_color)
            formats.append(marker_format)
        base_format = QTextLayout.FormatRange()
        base_format.start = 0
        base_format.length = len(text)
        base_format.format.setForeground(normal_color)
        layout.setFormats([base_format, *formats])
        document_height = document.documentLayout().documentSize().height()
        vertical_offset = max(0.0, (text_rect.height() - document_height) / 2.0)
        painter.translate(text_rect.left(), text_rect.top() + vertical_offset)
        context = QAbstractTextDocumentLayout.PaintContext()
        document.documentLayout().draw(painter, context)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: object) -> QSize:
        width = max(120, option.rect.width() - 15)
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        document.setTextWidth(width)
        document.setPlainText(str(index.data() or ""))
        return QSize(option.rect.width(), max(34, int(document.size().height()) + 6))

    def createEditor(self, parent: object, option: object, index: object) -> object:
        editor = ScriptLineEditor(parent)
        editor.setProperty("script_row", index.row())
        editor.enterPressed.connect(
            lambda text, cursor, current=editor: self._finish_and_advance(
                current, text, cursor
            )
        )
        editor.emptyRowDeleteRequested.connect(
            lambda current=editor: self._delete_empty_row(current)
        )
        editor.cursorPositionChanged.connect(
            lambda current=editor: self.cursorTracked.emit(
                int(current.property("script_row")), current.cursorPosition()
            )
        )
        editor.textChanged.connect(
            lambda current=editor: self._resize_editor_row(current)
        )
        editor.selectAllScriptRequested.connect(self.selectAllScriptRequested)
        return editor

    def setEditorData(self, editor: object, index: object) -> None:
        if isinstance(editor, ScriptLineEditor):
            editor.setPlainText(str(index.data() or ""))
            editor.setCursorPosition(len(editor.toPlainText()))
            self._resize_editor_row(editor)
            return
        super().setEditorData(editor, index)

    def setModelData(self, editor: object, model: object, index: object) -> None:
        if isinstance(editor, ScriptLineEditor):
            model.setData(index, editor.text(), Qt.ItemDataRole.EditRole)
            return
        super().setModelData(editor, model, index)

    def _resize_editor_row(self, editor: ScriptLineEditor) -> None:
        table = editor.parent()
        while table is not None and not isinstance(table, QTableWidget):
            table = table.parent()
        if not isinstance(table, QTableWidget):
            return
        row = int(editor.property("script_row"))
        editor.document().setTextWidth(max(120, editor.viewport().width()))
        height = max(34, int(editor.document().size().height()) + 6)
        table.setRowHeight(row, height)

    def _delete_empty_row(self, editor: object) -> None:
        row = int(editor.property("script_row"))
        self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
        self.emptyRowDeleteRequested.emit(row)

    def _finish_and_advance(self, editor: object, text: str, cursor: int) -> None:
        row = int(editor.property("script_row"))
        self.commitData.emit(editor)
        self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
        self.advanceRequested.emit(row, text, cursor)


class MainWindow(QMainWindow):
    target_buffer_lines = 3
    rolling_buffer_lines = 20

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("云祺 AI直播工作台")
        self.resize(1680, 920)

        self.voice_profiles: list[dict[str, object]] = []
        self.output_devices = list(QMediaDevices.audioOutputs())
        self.player = AudioQueuePlayer(self)
        self.worker: TTSWorker | None = None
        self.playback_session_id = 0
        self._retired_workers: list[TTSWorker] = []
        self.local_worker: TTSWorker | None = None
        self.interjection_worker: TTSWorker | None = None
        self.playback_lines: list[ScriptLine] = []
        self.interjection_lines: dict[str, ScriptLine] = {}
        self.interjection_auto_pause = False
        self.next_submit_index = 0
        self.inflight_count = 0
        self.finished_count = 0
        self.playback_started = False
        self.pause_requested = False
        self.manually_paused = False
        self.line_positions: dict[str, int] = {}
        self.playback_start_line_id: str | None = None
        self.blocked_line_ids: set[str] = set()
        self.countdown_value = 0
        self.countdown_active = False
        self.play_asap_requested = False
        self.local_synthesis_active = False
        self.local_synthesis_stopping = False
        self.local_lines: list[ScriptLine] = []
        self.local_line_index = 0
        self.local_attempts: dict[str, int] = {}
        self.local_spoken_texts: dict[str, str] = {}
        self.local_success_count = 0
        self.local_failure_count = 0
        self.local_failure_messages: list[str] = []
        self.local_output_dir: Path | None = None
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._advance_start_countdown)
        app_root = application_root()
        user_paths = prepare_user_data(app_root)
        self.data_dir = user_paths["root"]
        self.scripts_dir = user_paths["scripts"]
        self.reference_audio_dir = user_paths["reference_audio"]
        self.config_dir = user_paths["settings"]
        self.local_audio_dir = user_paths["local_audio"]
        self.audio_history_dir = user_paths["audio_history"]
        self.voice_metadata_path = self.config_dir / "音色文案.json"
        self.voice_mapping_path = self.config_dir / "云端音色映射.json"
        self.script_row_settings_path = user_paths["row_settings"] / "话术行配置.json"
        self.interjection_presets_path = user_paths["interjections"] / "插播话术.json"
        self.update_check_worker: UpdateCheckWorker | None = None
        self.update_download_worker: UpdateDownloadWorker | None = None
        self.update_progress_dialog: QProgressDialog | None = None
        self._migrate_voice_metadata()
        self.local_voice_paths: dict[str, Path] = {}
        self.local_voice_remote_ids: dict[str, str] = {}
        self.voice_sync_worker: VoiceSyncWorker | None = None
        self.current_project_path: Path | None = None
        self._last_edit_row = -1
        self._last_edit_cursor = -1
        self._history: list[list[dict[str, object]]] = []
        self._restoring_history = False

        self._build_ui()
        self._restore_saved_window_layout()
        self._connect_player()
        self._add_demo_rows()
        self.table.itemChanged.connect(self._on_table_item_changed)
        self._ensure_trailing_row()
        self._reset_history()
        self._refresh_local_voices()
        self._load_interjection_presets()
        self._refresh_project_library()
        self._restore_last_project()
        self._refresh_output_devices()
        # The cloud instance may still be loading the model when the desktop
        # client opens.  Reconnect quietly in the background so the user only
        # needs to enter the instance public IP once.
        self._auto_connect_attempts = 0
        self._manual_disconnect = False
        QTimer.singleShot(800, self._auto_connect_saved_server)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 6, 8, 8)
        root_layout.setSpacing(6)

        top_toolbar = QWidget()
        top_toolbar.setObjectName("topToolbar")
        top_toolbar.setFixedHeight(40)
        top_toolbar_layout = QHBoxLayout(top_toolbar)
        top_toolbar_layout.setContentsMargins(6, 0, 3, 0)
        self.header_connection_status = QLabel("状态：等待连接")
        self.header_connection_status.setObjectName("topStatus")
        top_toolbar_layout.addWidget(self.header_connection_status)
        top_toolbar_layout.addStretch()
        self.update_button = QToolButton()
        self.update_button.setObjectName("topActionButton")
        self.update_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.update_button.setText("检查更新")
        self.update_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.update_button.setIconSize(QSize(20, 20))
        self.update_button.setToolTip(f"当前版本 {APP_VERSION}，检查并一键更新")
        self.update_button.setFixedSize(112, 36)
        self.update_button.clicked.connect(self._check_for_updates)
        top_toolbar_layout.addWidget(self.update_button)
        self.rollback_button = QToolButton()
        self.rollback_button.setObjectName("topActionButton")
        self.rollback_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack)
        )
        self.rollback_button.setText("恢复上一版")
        self.rollback_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.rollback_button.setIconSize(QSize(20, 20))
        self.rollback_button.setToolTip("关闭客户端并恢复到上一次正常版本")
        self.rollback_button.setFixedSize(122, 36)
        self.rollback_button.clicked.connect(self._rollback_to_previous_version)
        top_toolbar_layout.addWidget(self.rollback_button)
        self.voice_library_button = QToolButton()
        self.voice_library_button.setObjectName("topActionButton")
        self.voice_library_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        )
        self.voice_library_button.setText("音色库")
        self.voice_library_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.voice_library_button.setIconSize(QSize(22, 22))
        self.voice_library_button.setToolTip("打开音色库")
        self.voice_library_button.setFixedSize(102, 36)
        top_toolbar_layout.addWidget(self.voice_library_button)
        global_save_button = QToolButton()
        global_save_button.setObjectName("topActionButton")
        global_save_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        global_save_button.setText("全局保存")
        global_save_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        global_save_button.setIconSize(QSize(22, 22))
        global_save_button.setToolTip("全局保存")
        global_save_button.setFixedSize(112, 36)
        global_save_button.clicked.connect(self._save_all_settings)
        top_toolbar_layout.addWidget(global_save_button)
        root_layout.addWidget(top_toolbar)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter = self.main_splitter
        main_splitter.setObjectName("mainSplitter")

        library_panel = QWidget()
        library_panel.setObjectName("libraryPanel")
        library_layout = QVBoxLayout(library_panel)
        library_layout.setContentsMargins(6, 6, 6, 6)
        library_layout.setSpacing(6)
        library_title = QLabel("直播话术")
        library_title.setObjectName("sectionTitle")
        library_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        library_layout.addWidget(library_title)
        self.project_search_edit = QLineEdit()
        self.project_search_edit.setVisible(False)
        self.project_search_edit.textChanged.connect(self._filter_project_library)
        self.project_list = QListWidget()
        self.project_list.setObjectName("projectList")
        self.project_list.setSpacing(2)
        library_layout.addWidget(self.project_list, 1)
        confirm_project_button = QPushButton("确认切换话术")
        confirm_project_button.setObjectName("confirmProjectButton")
        confirm_project_button.setToolTip("切换到当前选中的话术")
        confirm_project_button.clicked.connect(self._confirm_project_switch)
        library_layout.addWidget(confirm_project_button)
        library_buttons = QHBoxLayout()
        for text, icon, tooltip, callback in (
            ("＋", None, "新建话术", self._new_project),
            ("−", None, "删除选中的话术", self._delete_project),
            (
                "",
                QStyle.StandardPixmap.SP_BrowserReload,
                "刷新话术列表",
                self._refresh_project_library,
            ),
            (
                "",
                QStyle.StandardPixmap.SP_DirOpenIcon,
                "打开话术文件夹",
                self._open_scripts_folder,
            ),
        ):
            button = QToolButton()
            button.setText(text)
            if icon is not None:
                button.setIcon(self.style().standardIcon(icon))
            button.setToolTip(tooltip)
            button.setMinimumHeight(34)
            button.setObjectName("libraryToolButton")
            button.clicked.connect(callback)
            library_buttons.addWidget(button, 1)
        library_layout.addLayout(library_buttons)
        main_splitter.addWidget(library_panel)

        center = QWidget()
        center.setObjectName("editorPanel")
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(4)
        self.current_file_label = QLabel("未保存话术")
        self.current_file_label.setVisible(False)
        editor_tabs = QTabWidget()
        editor_tabs.setObjectName("editorTabs")
        editor_page = QWidget()
        editor_layout = QVBoxLayout(editor_page)
        editor_layout.setContentsMargins(6, 8, 6, 6)
        row_actions = QHBoxLayout()
        mode_label = QLabel("运行模式：")
        mode_label.setObjectName("modeLabel")
        row_actions.addWidget(mode_label)
        self.ai_mode_radio = QRadioButton("AI模式")
        self.ai_mode_radio.setChecked(True)
        row_actions.addWidget(self.ai_mode_radio)
        self.local_mode_radio = QRadioButton("本地模式")
        self.local_mode_radio.toggled.connect(self._update_run_mode_ui)
        row_actions.addWidget(self.local_mode_radio)
        row_actions.addStretch()
        insert_pause_button = QPushButton("插入停顿")
        insert_pause_button.setObjectName("secondaryButton")
        insert_pause_button.clicked.connect(self._insert_pause)
        row_actions.addWidget(insert_pause_button)
        save_editor_button = QPushButton("保存话术")
        save_editor_button.setObjectName("saveButton")
        save_editor_button.clicked.connect(self.save_project)
        row_actions.addWidget(save_editor_button)
        editor_layout.addLayout(row_actions)
        self.table = ScriptTableWidget(0, 4)
        self.table.setObjectName("scriptTable")
        self.table.setHorizontalHeaderLabels(["序号", "参考音色", "语速", "话术（#500#表示停顿）"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setWordWrap(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.script_text_delegate = ScriptTextDelegate(self.table)
        self.script_text_delegate.advanceRequested.connect(self._advance_script_row)
        self.script_text_delegate.emptyRowDeleteRequested.connect(self.remove_row)
        self.script_text_delegate.cursorTracked.connect(self._track_edit_cursor)
        self.table.enterOnScriptRequested.connect(self._advance_table_row_at_end)
        self.script_text_delegate.selectAllScriptRequested.connect(
            self.table.select_script_column
        )
        self.table.setItemDelegateForColumn(3, self.script_text_delegate)
        self.table.setColumnWidth(0, 44)
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnHidden(0, False)
        self.table.setColumnHidden(1, True)
        self.table.setColumnHidden(2, True)
        self.table.horizontalHeader().setVisible(False)
        script_font = QFont("Microsoft YaHei UI", 12)
        script_font.setWeight(QFont.Weight.Medium)
        self.table.setFont(script_font)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)
        self.table.deleteRequested.connect(self.remove_selected)
        self.table.undoRequested.connect(self._undo_last_change)
        self.table.pasteRequested.connect(self._paste_script_text)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().sectionResized.connect(
            lambda *_args: QTimer.singleShot(0, self._resize_script_rows)
        )
        editor_layout.addWidget(self.table, 1)
        self.script_stats_label = QLabel("字数：0 | 句数：0")
        self.script_stats_label.setObjectName("scriptStats")
        editor_layout.addWidget(self.script_stats_label)
        editor_tabs.addTab(editor_page, "话术预览与编辑")

        interjection_page = QWidget()
        interjection_layout = QVBoxLayout(interjection_page)
        interjection_layout.setContentsMargins(6, 8, 6, 6)
        interjection_actions = QHBoxLayout()
        interjection_hint = QLabel("预设插播话术。当前句播完后优先插播，再继续直播话术。")
        interjection_hint.setObjectName("modeLabel")
        interjection_actions.addWidget(interjection_hint)
        interjection_actions.addStretch()
        add_interjection_button = QPushButton("新增一句")
        add_interjection_button.setObjectName("secondaryButton")
        add_interjection_button.clicked.connect(self._add_interjection_row)
        interjection_actions.addWidget(add_interjection_button)
        delete_interjection_button = QPushButton("删除选中")
        delete_interjection_button.setObjectName("secondaryButton")
        delete_interjection_button.clicked.connect(self._delete_interjection_rows)
        interjection_actions.addWidget(delete_interjection_button)
        save_interjection_button = QPushButton("保存插播话术")
        save_interjection_button.setObjectName("secondaryButton")
        save_interjection_button.clicked.connect(self._save_interjection_presets)
        interjection_actions.addWidget(save_interjection_button)
        interjection_layout.addLayout(interjection_actions)

        self.interjection_table = ScriptTableWidget(0, 5)
        self.interjection_table.setObjectName("scriptTable")
        self.interjection_table.setHorizontalHeaderLabels(
            ["序号", "参考音频", "语速", "插播话术", "操作"]
        )
        self.interjection_table.horizontalHeader().setStretchLastSection(False)
        self.interjection_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.interjection_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Fixed
        )
        self.interjection_table.horizontalHeader().setVisible(False)
        self.interjection_table.verticalHeader().setVisible(False)
        self.interjection_table.setWordWrap(True)
        self.interjection_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.interjection_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.interjection_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.interjection_table.setColumnWidth(0, 60)
        self.interjection_table.setColumnWidth(4, 126)
        self.interjection_table.setColumnHidden(1, True)
        self.interjection_table.setColumnHidden(2, True)
        self.interjection_table.setFont(script_font)
        self.interjection_text_delegate = ScriptTextDelegate(
            self.interjection_table
        )
        self.interjection_text_delegate.advanceRequested.connect(
            self._advance_interjection_row
        )
        self.interjection_text_delegate.emptyRowDeleteRequested.connect(
            self._delete_interjection_row
        )
        self.interjection_text_delegate.selectAllScriptRequested.connect(
            self.interjection_table.select_script_column
        )
        self.interjection_table.setItemDelegateForColumn(
            3, self.interjection_text_delegate
        )
        self.interjection_table.enterOnScriptRequested.connect(
            self._advance_interjection_row_at_end
        )
        self.interjection_table.deleteRequested.connect(
            self._delete_interjection_rows
        )
        self.interjection_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.interjection_table.customContextMenuRequested.connect(
            self._show_interjection_context_menu
        )
        self.interjection_table.itemChanged.connect(
            lambda _item: self._renumber_interjection_rows()
        )
        interjection_layout.addWidget(self.interjection_table, 1)
        self.interjection_status_label = QLabel(
            "先选择一条预设话术。序号右键可设置对应参考音频。"
        )
        self.interjection_status_label.setObjectName("scriptStats")
        interjection_layout.addWidget(self.interjection_status_label)
        editor_tabs.addTab(interjection_page, "插播话术")
        center_layout.addWidget(editor_tabs)
        main_splitter.addWidget(center)

        settings_tabs = QTabWidget()
        settings_tabs.setObjectName("settingsTabs")
        synthesis_page = QWidget()
        synthesis_layout = QVBoxLayout(synthesis_page)
        synthesis_layout.setContentsMargins(8, 8, 8, 8)

        public_group = QGroupBox("公共设置")
        public_form = QFormLayout(public_group)
        self.default_speed_spin = QDoubleSpinBox()
        self.default_speed_spin.setRange(0.5, 2.0)
        self.default_speed_spin.setSingleStep(0.05)
        self.default_speed_spin.setDecimals(2)
        self.default_speed_spin.setSuffix("x")
        self.default_speed_spin.setValue(1.0)
        self.buffer_target_spin = QSpinBox()
        self.buffer_target_spin.setRange(1, 10)
        self.buffer_target_spin.setValue(self.target_buffer_lines)
        self.buffer_target_spin.valueChanged.connect(self._change_buffer_target)
        self.low_randomness_check = QCheckBox("降低随机性（语气更稳定）")
        self.low_randomness_check.setToolTip(
            "降低采样温度并缩小采样范围，保留少量自然变化"
        )
        self.disable_randomness_check = QCheckBox("关闭随机性（尽量固定）")
        self.disable_randomness_check.setToolTip(
            "关闭主模型和子模型随机采样，相同条件下结果更固定"
        )
        self.low_randomness_check.toggled.connect(
            self._on_low_randomness_toggled
        )
        self.disable_randomness_check.toggled.connect(
            self._on_disable_randomness_toggled
        )
        public_form.addRow("默认语速 (0.5-2.0)", self.default_speed_spin)
        public_form.addRow("开始播放最低缓存", self.buffer_target_spin)
        public_form.addRow("", self.low_randomness_check)
        public_form.addRow("", self.disable_randomness_check)
        synthesis_layout.addWidget(public_group)

        connection_group = QGroupBox("云端TTS")
        connection_form = QFormLayout(connection_group)
        configured_url = os.environ.get("AILIVE_SERVER_URL", "http://127.0.0.1:8000")
        parsed_url = urlsplit(configured_url)
        self.model_combo = QComboBox()
        self.model_combo.addItem("Qwen3-TTS 1.7B Base", "qwen3-tts-1.7b-base")
        self.connection_mode_combo = QComboBox()
        self.connection_mode_combo.addItem("SSH安全隧道（推荐）", "http")
        self.connection_mode_combo.addItem("公网直连（HTTP）", "http")
        self.connection_mode_combo.addItem("公网直连（HTTPS）", "https")
        self.connection_mode_combo.setCurrentIndex(1)
        self.server_host_edit = QLineEdit(parsed_url.hostname or "127.0.0.1")
        self.server_port_spin = QSpinBox()
        self.server_port_spin.setRange(1, 65535)
        self.server_port_spin.setValue(parsed_url.port or 8000)
        self.token_edit = QLineEdit(os.environ.get("AILIVE_API_TOKEN", ""))
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.default_voice_combo = QComboBox()
        self.default_voice_combo.addItem("请选择默认音色", None)
        self.default_voice_combo.currentIndexChanged.connect(self._on_default_voice_changed)
        self.reference_text_edit = QLineEdit()
        self.reference_text_edit.setReadOnly(True)
        self.reference_text_edit.setPlaceholderText("选择音色后显示参考音频原文")
        self.connection_status = QLabel("未连接")
        self.connection_button = QPushButton("连接并读取音色")
        self.connection_button.clicked.connect(self._toggle_connection)
        connection_form.addRow("模型", self.model_combo)
        connection_form.addRow("服务地址", self.server_host_edit)
        connection_form.addRow("端口号", self.server_port_spin)
        connection_form.addRow("默认音色", self.default_voice_combo)
        connection_form.addRow("参考音频", self.reference_text_edit)
        connection_form.addRow("连接状态", self.connection_status)
        connection_form.addRow(self.connection_button)
        synthesis_layout.addWidget(connection_group)

        tts_log_group = QGroupBox("TTS服务日志")
        tts_log_layout = QVBoxLayout(tts_log_group)
        self.tts_log_view = QTextEdit()
        self.tts_log_view.setReadOnly(True)
        self.tts_log_view.setPlaceholderText("云端TTS连接和生成日志显示在这里")
        tts_log_layout.addWidget(self.tts_log_view)
        synthesis_layout.addWidget(tts_log_group, 1)
        settings_tabs.addTab(synthesis_page, "合成")

        voice_page = QWidget()
        voice_page_layout = QVBoxLayout(voice_page)
        voice_group = QGroupBox("参考音色库")
        voice_layout = QVBoxLayout(voice_group)
        self.voice_list = QListWidget()
        voice_layout.addWidget(self.voice_list, 1)
        voice_buttons = QHBoxLayout()
        refresh_voice_button = QPushButton("刷新音色")
        refresh_voice_button.clicked.connect(self._refresh_local_voices)
        add_voice_button = QPushButton("打开参考音频文件夹")
        add_voice_button.clicked.connect(self._open_reference_audio_folder)
        voice_buttons.addWidget(refresh_voice_button)
        voice_buttons.addWidget(add_voice_button)
        voice_layout.addLayout(voice_buttons)
        voice_page_layout.addWidget(voice_group)
        settings_tabs.addTab(voice_page, "音色")
        self.voice_library_button.clicked.connect(self._open_voice_library_dialog)

        output_page = QWidget()
        output_layout = QVBoxLayout(output_page)
        output_group = QGroupBox("音频输出")
        output_form = QFormLayout(output_group)
        self.output_combo = QComboBox()
        self.output_combo.currentIndexChanged.connect(self._change_output_device)
        self.buffer_label = QLabel("0句")
        self.current_line_label = QLabel("—")
        self.buffer_target_label = QLabel(f"{self.target_buffer_lines}句")
        output_form.addRow("输出设备", self.output_combo)
        output_form.addRow("已缓冲", self.buffer_label)
        output_form.addRow("当前话术", self.current_line_label)
        output_form.addRow("缓冲目标", self.buffer_target_label)
        output_layout.addWidget(output_group)
        output_layout.addStretch()
        settings_tabs.addTab(output_page, "输出设置")
        main_splitter.addWidget(settings_tabs)
        main_splitter.setSizes([280, 1040, 400])

        self.bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        bottom_splitter = self.bottom_splitter
        bottom_splitter.setObjectName("bottomSplitter")
        current_group = QGroupBox("当前播放的话术")
        current_layout = QVBoxLayout(current_group)
        self.current_text_view = QTextEdit()
        self.current_text_view.setReadOnly(True)
        self.current_text_view.setPlaceholderText("当前播放的完整话术显示在这里")
        current_layout.addWidget(self.current_text_view)
        bottom_splitter.addWidget(current_group)

        log_group = QGroupBox("生成、连接和播放日志")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("运行日志显示在这里")
        log_layout.addWidget(self.log_view)
        bottom_splitter.addWidget(log_group)

        control_group = QGroupBox("直播控制")
        controls = QVBoxLayout(control_group)
        self.start_button = QPushButton("启动")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setMinimumHeight(40)
        self.pause_button = QPushButton("暂停/继续")
        self.pause_button.setObjectName("playbackToggleButton")
        self.pause_button.setMinimumHeight(40)
        self.pause_button.setEnabled(False)
        self.start_button.clicked.connect(self.toggle_start_stop)
        self.pause_button.clicked.connect(self.toggle_pause_resume)
        controls.addWidget(self.start_button)
        controls.addWidget(self.pause_button)
        self.status_label = QLabel("等待连接TTS服务")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        controls.addWidget(self.status_label)
        self.local_progress_label = QLabel("")
        self.local_progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.local_progress_label.setVisible(False)
        controls.addWidget(self.local_progress_label)
        bottom_splitter.addWidget(control_group)
        bottom_splitter.setSizes([320, 930, 400])

        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.setObjectName("verticalContentSplitter")
        self.vertical_splitter.setChildrenCollapsible(False)
        self.vertical_splitter.addWidget(main_splitter)
        self.vertical_splitter.addWidget(bottom_splitter)
        self.vertical_splitter.setStretchFactor(0, 1)
        self.vertical_splitter.setStretchFactor(1, 0)
        self.vertical_splitter.setSizes([720, 136])
        root_layout.addWidget(self.vertical_splitter, 1)

        self.setCentralWidget(root)

    def _connect_player(self) -> None:
        self.player.lineStarted.connect(self._on_line_started)
        self.player.lineFinished.connect(self._on_line_finished)
        self.player.sentenceEndPaused.connect(self._on_sentence_end_paused)
        self.player.bufferDepthChanged.connect(self._on_buffer_depth_changed)
        self.player.buffering.connect(self._on_player_buffering)
        self.player.errorOccurred.connect(self._on_playback_error)

    def _add_demo_rows(self) -> None:
        self.add_row("来，所有同学进入直播间#500#咱们开始今天的课程#1000#", speed=1.0)
        self.add_row("我们来看第一道题#300#请大家认真思考。", speed=1.1)
        self.add_row("这道题选择A选项#500#接下来我们看下一题。", speed=0.95)

    def _server_url(self) -> str:
        return server_url_from_parts(
            self.server_host_edit.text(),
            self.server_port_spin.value(),
            str(self.connection_mode_combo.currentData()),
        )

    def _token(self) -> str:
        return self.token_edit.text().strip()

    def _save_connection_settings(self) -> None:
        settings = self._layout_settings()
        settings.setValue("tts/serverHost", self.server_host_edit.text().strip())
        settings.setValue("tts/serverPort", self.server_port_spin.value())
        settings.setValue(
            "tts/defaultVoice", str(self.default_voice_combo.currentData() or "")
        )
        settings.setValue(
            "tts/serverScheme", str(self.connection_mode_combo.currentData())
        )
        settings.sync()

    def _connect_and_save(self, _checked: bool = False) -> None:
        if not self.default_voice_combo.currentData():
            QMessageBox.information(self, "请选择音色", "请先选择默认音色，再连接TTS服务")
            return
        self._manual_disconnect = False
        self._save_connection_settings()
        self._auto_connect_attempts = 0
        self.refresh_voices(show_error=True)

    def _toggle_connection(self, _checked: bool = False) -> None:
        if self.connection_status.text() == "已连接":
            self._disconnect_tts()
        else:
            self._connect_and_save()

    def _set_connection_locked(self, connected: bool) -> None:
        """Lock one complete TTS configuration while it is connected."""
        editable = not connected
        for widget in (
            self.model_combo,
            self.connection_mode_combo,
            self.server_host_edit,
            self.server_port_spin,
            self.token_edit,
            self.default_voice_combo,
        ):
            widget.setEnabled(editable)
        # The transcript is derived from the selected default voice.  It is
        # always read-only, but disabling it while connected makes the lock
        # state unambiguous to the user.
        self.reference_text_edit.setEnabled(editable)
        self.connection_button.setText(
            "断开连接" if connected else "连接并读取音色"
        )

    def _disconnect_tts(self) -> None:
        self._manual_disconnect = True
        if self.worker is not None or self.playback_started or self.countdown_active:
            self.stop_playback()
        self.local_voice_remote_ids.clear()
        self.connection_status.setText("未连接")
        self.connection_status.setStyleSheet("color: #c9a56f; font-weight: 600;")
        self.header_connection_status.setText("等待连接")
        self.header_connection_status.setProperty("connected", False)
        self.header_connection_status.style().unpolish(self.header_connection_status)
        self.header_connection_status.style().polish(self.header_connection_status)
        self._set_connection_locked(False)
        self._log("已断开TTS服务，可修改模型、地址、端口和默认音色")

    def _auto_connect_saved_server(self) -> None:
        if self._manual_disconnect or self.connection_status.text() == "已连接":
            return
        if not self.default_voice_combo.currentData():
            return
        self._auto_connect_attempts += 1
        self.refresh_voices(show_error=False)
        if (
            self.connection_status.text() != "已连接"
            and self._auto_connect_attempts < 120
        ):
            QTimer.singleShot(5000, self._auto_connect_saved_server)

    def refresh_voices(self, show_error: bool = True) -> None:
        try:
            remote_profiles = list_voices(self._server_url(), self._token())
            # Cloud voices are implementation-only cache entries.  Keep them
            # out of the UI and map matching local WAVs to their cached IDs.
            local_profiles = self._local_voice_profiles()
            remote_ids = {
                str(profile.get("reference_id", "")) for profile in remote_profiles
            }
            saved_mapping = self._load_voice_remote_mapping()
            self.local_voice_remote_ids = {
                local_id: remote_id
                for local_id, remote_id in saved_mapping.items()
                if remote_id in remote_ids
            }
            for local_profile in local_profiles:
                cloud_name = self._cloud_voice_name(local_profile)
                match = next(
                    (
                        remote
                        for remote in remote_profiles
                        if str(remote.get("name", ""))
                        in {str(local_profile.get("name", "")), cloud_name}
                        and str(remote.get("reference_text", "")).strip()
                        == str(local_profile.get("reference_text", "")).strip()
                    ),
                    None,
                )
                if match is not None:
                    self.local_voice_remote_ids[str(local_profile["reference_id"])] = str(
                        match["reference_id"]
                    )
            self._save_voice_remote_mapping()
            self.voice_profiles = local_profiles
            self._populate_voice_views()
            selected_default = self.default_voice_combo.currentData()
            self.default_voice_combo.clear()
            self.default_voice_combo.addItem("请选择默认音色", None)
            for profile in self.voice_profiles:
                self.default_voice_combo.addItem(
                    str(profile.get("selector_name", profile["name"])),
                    str(profile["reference_id"]),
                )
            selected_index = self.default_voice_combo.findData(selected_default)
            if selected_index >= 0:
                self.default_voice_combo.setCurrentIndex(selected_index)
            elif len(local_profiles) == 1:
                self.default_voice_combo.setCurrentIndex(1)
            self._refresh_voice_boxes()
            self.connection_status.setText("已连接")
            self.connection_status.setStyleSheet("color: #71b58d; font-weight: 600;")
            self.header_connection_status.setText("服务已连接")
            self.header_connection_status.setProperty("connected", True)
            self.header_connection_status.style().unpolish(self.header_connection_status)
            self.header_connection_status.style().polish(self.header_connection_status)
            self._set_connection_locked(True)
            self._save_connection_settings()
            self._log(f"已读取{len(local_profiles)}个本地参考音色")
        except (httpx.HTTPError, ValueError) as error:
            self.connection_status.setText("连接失败")
            self.connection_status.setStyleSheet("color: #d47777; font-weight: 600;")
            self.header_connection_status.setText("连接失败")
            self.header_connection_status.setProperty("connected", False)
            self.header_connection_status.style().unpolish(self.header_connection_status)
            self.header_connection_status.style().polish(self.header_connection_status)
            self._set_connection_locked(False)
            if show_error:
                QMessageBox.warning(self, "连接失败", str(error))

    def _local_voice_profiles(self) -> list[dict[str, object]]:
        profiles: list[dict[str, object]] = []
        self.local_voice_paths.clear()
        metadata = self._load_voice_metadata()
        for audio_path in sorted(
            self.reference_audio_dir.rglob("*.wav"),
            key=lambda path: natural_sort_key(
                path.relative_to(self.reference_audio_dir).as_posix()
            ),
        ):
            if not audio_path.is_file():
                continue
            if audio_path.suffix.lower() != ".wav":
                continue
            digest = hashlib.sha1(str(audio_path.resolve()).encode("utf-8")).hexdigest()[:12]
            reference_id = f"local:{digest}"
            metadata_key = self._voice_metadata_key(audio_path)
            reference_text = str(
                metadata.get(metadata_key, metadata.get(audio_path.name, audio_path.stem))
            ).strip()
            self.local_voice_paths[reference_id] = audio_path
            profiles.append(
                {
                    "reference_id": reference_id,
                    "name": audio_path.stem,
                    "selector_name": self._voice_selector_name(audio_path),
                    "display_name": self._voice_display_name(audio_path),
                    "menu_name": self._voice_menu_name(audio_path, reference_text),
                    "audio_path": str(audio_path),
                    "reference_text": reference_text,
                    "local": True,
                }
            )
        return profiles

    def _cloud_voice_name(self, profile: dict[str, object]) -> str:
        """Return a stable server-side name that always fits the API limit."""
        reference_id = str(profile.get("reference_id", "")).removeprefix("local:")
        audio_path = Path(str(profile.get("audio_path", "")))
        folder = self._voice_selector_name(audio_path).strip() or "voice"
        return f"{folder[:72]}-{reference_id[:12]}"[:100]

    def _voice_display_name(self, audio_path: Path) -> str:
        try:
            relative = audio_path.resolve().relative_to(
                self.reference_audio_dir.resolve()
            )
        except ValueError:
            return audio_path.stem
        parent = relative.parent.as_posix()
        return audio_path.stem if parent == "." else f"{parent} / {audio_path.stem}"

    def _voice_metadata_key(self, audio_path: Path) -> str:
        """Use a relative path so equal WAV names in different folders stay distinct."""
        try:
            return audio_path.resolve().relative_to(
                self.reference_audio_dir.resolve()
            ).as_posix()
        except ValueError:
            return audio_path.name

    def _voice_selector_name(self, audio_path: Path) -> str:
        """Show the containing folder as the voice name in global TTS settings."""
        try:
            relative = audio_path.resolve().relative_to(
                self.reference_audio_dir.resolve()
            )
        except ValueError:
            return audio_path.stem
        parent = relative.parent.as_posix()
        return audio_path.stem if parent == "." else parent

    def _voice_menu_name(self, audio_path: Path, reference_text: str) -> str:
        """Compact label for the sequence-number context menu."""
        folder = self._voice_selector_name(audio_path)
        preview = reference_text.strip()[:5] or audio_path.stem[:5]
        return f"{folder} / {preview}" if folder else preview

    def _load_voice_metadata(self) -> dict[str, str]:
        if not self.voice_metadata_path.exists():
            return {}
        try:
            data = json.loads(self.voice_metadata_path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in data.items()}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _migrate_voice_metadata(self) -> None:
        old_path = self.reference_audio_dir / "音色文案.json"
        if not old_path.exists() or self.voice_metadata_path.exists():
            return
        try:
            old_path.replace(self.voice_metadata_path)
        except OSError:
            shutil.copy2(old_path, self.voice_metadata_path)
            old_path.unlink(missing_ok=True)

    def _save_voice_metadata(self, metadata: dict[str, str]) -> None:
        self.voice_metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_script_row_settings(self) -> dict[str, list[dict[str, object]]]:
        if not self.script_row_settings_path.exists():
            return {}
        try:
            data = json.loads(
                self.script_row_settings_path.read_text(encoding="utf-8")
            )
            if not isinstance(data, dict):
                return {}
            return {
                str(key): list(value)
                for key, value in data.items()
                if isinstance(value, list)
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _save_current_text_row_settings(self, path: Path) -> None:
        if path.suffix.lower() not in {".txt", ".tst"}:
            return
        rows = [row for row in self._snapshot_rows() if str(row["text"]).strip()]
        settings = self._load_script_row_settings()
        settings[str(path.resolve())] = rows
        self.script_row_settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _saved_text_rows(self, path: Path) -> list[dict[str, object]]:
        settings = self._load_script_row_settings()
        exact = settings.get(str(path.resolve()))
        if exact is not None:
            return exact
        # Distribution packages move between computers, so an absolute path
        # saved on the build machine cannot be reused. Fall back to the unique
        # script filename and save an exact local path on the next edit.
        matching = [
            rows
            for saved_path, rows in settings.items()
            if Path(saved_path).name.casefold() == path.name.casefold()
        ]
        return matching[0] if len(matching) == 1 else []

    def _populate_voice_views(self) -> None:
        self.voice_list.clear()
        for profile in self.voice_profiles:
            if profile.get("local"):
                self.voice_list.addItem(
                    str(profile.get("display_name", profile["name"]))
                )

    def _populate_local_default_voice_combo(self) -> None:
        """Populate the global selector even while the cloud is disconnected."""
        selected_default = self.default_voice_combo.currentData() or getattr(
            self, "_saved_default_voice_id", ""
        )
        self.default_voice_combo.blockSignals(True)
        try:
            self.default_voice_combo.clear()
            self.default_voice_combo.addItem("请选择默认音色", None)
            for profile in self.voice_profiles:
                self.default_voice_combo.addItem(
                    str(profile.get("selector_name", profile["name"])),
                    str(profile["reference_id"]),
                )
            selected_index = self.default_voice_combo.findData(selected_default)
            if selected_index >= 0:
                self.default_voice_combo.setCurrentIndex(selected_index)
            elif len(self.voice_profiles) == 1:
                self.default_voice_combo.setCurrentIndex(1)
        finally:
            self.default_voice_combo.blockSignals(False)
        self._on_default_voice_changed()

    def _refresh_local_voices(self, _checked: bool = False, *, log_result: bool = True) -> None:
        self.voice_profiles = self._local_voice_profiles()
        self._populate_voice_views()
        self._populate_local_default_voice_combo()
        self._refresh_voice_boxes()
        self._auto_match_reference_audio()
        if log_result:
            self._log(f"已读取{len(self.local_voice_paths)}个本地参考音频")

    def _open_reference_audio_folder(self) -> None:
        try:
            os.startfile(str(self.reference_audio_dir))  # type: ignore[attr-defined]
        except OSError as error:
            QMessageBox.critical(self, "无法打开文件夹", str(error))

    def _open_voice_library_dialog(self) -> None:
        self._refresh_local_voices()
        dialog = QDialog(self)
        dialog.setWindowTitle("我的音色库")
        dialog.resize(980, 690)
        layout = QVBoxLayout(dialog)
        title = QLabel("音色与参考音频")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        content = QSplitter(Qt.Orientation.Horizontal)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.addWidget(QLabel("音色"))
        voice_list = QListWidget()
        left_layout.addWidget(voice_list, 1)
        left_buttons = QHBoxLayout()
        add_voice_button = QPushButton("新增音色")
        rename_voice_button = QPushButton("重命名")
        delete_voice_button = QPushButton("删除音色")
        left_buttons.addWidget(add_voice_button)
        left_buttons.addWidget(rename_voice_button)
        left_buttons.addWidget(delete_voice_button)
        left_layout.addLayout(left_buttons)
        content.addWidget(left_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.addWidget(QLabel("参考音频"))
        audio_list = QListWidget()
        right_layout.addWidget(audio_list, 1)
        audio_buttons = QHBoxLayout()
        upload_audio_button = QPushButton("上传音频")
        delete_audio_button = QPushButton("删除音频")
        preview_audio_button = QPushButton("试听")
        audio_buttons.addWidget(upload_audio_button)
        audio_buttons.addWidget(delete_audio_button)
        audio_buttons.addStretch()
        audio_buttons.addWidget(preview_audio_button)
        right_layout.addLayout(audio_buttons)
        right_layout.addWidget(QLabel("参考音频转录文案"))
        reference_text_edit = QTextEdit()
        reference_text_edit.setPlaceholderText("在这里修改与参考音频完全一致的文案")
        reference_text_edit.setMinimumHeight(150)
        right_layout.addWidget(reference_text_edit)
        content.addWidget(right_panel)
        content.setSizes([250, 700])
        layout.addWidget(content, 1)

        footer = QHBoxLayout()
        footer.addWidget(QLabel("转录模型已使用 GPU 加载，可以上传 WAV 音频。"))
        footer.addStretch()
        save_button = QPushButton("保存参考音频")
        save_button.setObjectName("saveButton")
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        footer.addWidget(save_button)
        footer.addWidget(close_button)
        layout.addLayout(footer)

        profiles: list[dict[str, object]] = []

        def reload_profiles() -> None:
            nonlocal profiles
            profiles = self._local_voice_profiles()
            voice_list.clear()
            audio_list.clear()
            reference_text_edit.clear()
            for profile in profiles:
                voice_list.addItem(str(profile["name"]))
            if profiles:
                voice_list.setCurrentRow(0)

        def show_profile(row: int) -> None:
            audio_list.clear()
            reference_text_edit.clear()
            if row < 0 or row >= len(profiles):
                return
            profile = profiles[row]
            audio_list.addItem(Path(str(profile["audio_path"])).name)
            reference_text_edit.setPlainText(str(profile["reference_text"]))

        def save_reference_text() -> None:
            row = voice_list.currentRow()
            if row < 0 or row >= len(profiles):
                QMessageBox.information(dialog, "保存参考音频", "请先选择一个音色")
                return
            audio_path = Path(str(profiles[row]["audio_path"]))
            text = reference_text_edit.toPlainText().strip()
            if not text:
                QMessageBox.warning(dialog, "无法保存", "参考音频文案不能为空")
                return
            metadata = self._load_voice_metadata()
            metadata[self._voice_metadata_key(audio_path)] = text
            self._save_voice_metadata(metadata)
            self._refresh_local_voices()
            profiles[row]["reference_text"] = text
            QMessageBox.information(dialog, "保存成功", "参考音频文案已保存")

        def import_wav(destination_name: str | None = None) -> Path | None:
            filename, _ = QFileDialog.getOpenFileName(
                dialog,
                "选择WAV参考音频",
                str(self.reference_audio_dir),
                "WAV参考音频 (*.wav)",
            )
            if not filename:
                return None
            source = Path(filename)
            destination = self.reference_audio_dir / (
                f"{destination_name}.wav" if destination_name else source.name
            )
            if source.resolve() != destination.resolve():
                if destination.exists():
                    answer = QMessageBox.question(
                        dialog,
                        "覆盖音频",
                        f"“{destination.name}”已经存在，是否覆盖？",
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        return None
                shutil.copy2(source, destination)
            return destination

        def add_voice() -> None:
            name, accepted = QInputDialog.getText(dialog, "新增音色", "请输入音色名称")
            name = name.strip()
            if not accepted or not name:
                return
            if (self.reference_audio_dir / f"{name}.wav").exists():
                QMessageBox.warning(dialog, "无法新增", "同名音色已经存在")
                return
            destination = import_wav(name)
            if destination is None:
                return
            metadata = self._load_voice_metadata()
            metadata[self._voice_metadata_key(destination)] = destination.stem
            self._save_voice_metadata(metadata)
            reload_profiles()
            voice_list.setCurrentRow(len(profiles) - 1)

        def rename_voice() -> None:
            row = voice_list.currentRow()
            if row < 0 or row >= len(profiles):
                QMessageBox.information(dialog, "重命名", "请先选择一个音色")
                return
            audio_path = Path(str(profiles[row]["audio_path"]))
            name, accepted = QInputDialog.getText(
                dialog, "重命名音色", "请输入新的音色名称", text=audio_path.stem
            )
            name = name.strip()
            if not accepted or not name or name == audio_path.stem:
                return
            destination = audio_path.with_name(f"{name}.wav")
            if destination.exists():
                QMessageBox.warning(dialog, "无法重命名", "同名音色已经存在")
                return
            metadata = self._load_voice_metadata()
            old_key = self._voice_metadata_key(audio_path)
            text = metadata.pop(
                old_key,
                metadata.pop(audio_path.name, str(profiles[row]["reference_text"])),
            )
            audio_path.rename(destination)
            metadata[self._voice_metadata_key(destination)] = text
            self._save_voice_metadata(metadata)
            reload_profiles()

        def delete_selected_voice() -> None:
            row = voice_list.currentRow()
            if row < 0 or row >= len(profiles):
                QMessageBox.information(dialog, "删除音色", "请先选择一个音色")
                return
            audio_path = Path(str(profiles[row]["audio_path"]))
            answer = QMessageBox.question(
                dialog,
                "确认删除",
                f"确定删除音色“{audio_path.stem}”及其WAV音频吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            metadata = self._load_voice_metadata()
            metadata.pop(self._voice_metadata_key(audio_path), None)
            metadata.pop(audio_path.name, None)
            self._save_voice_metadata(metadata)
            audio_path.unlink(missing_ok=True)
            reload_profiles()

        def upload_audio() -> None:
            row = voice_list.currentRow()
            if row < 0 or row >= len(profiles):
                QMessageBox.information(dialog, "上传音频", "请先选择一个音色")
                return
            destination = import_wav(str(profiles[row]["name"]))
            if destination is not None:
                reload_profiles()
                voice_list.setCurrentRow(row)

        def preview_audio() -> None:
            row = voice_list.currentRow()
            if row < 0 or row >= len(profiles):
                QMessageBox.information(dialog, "试听", "请先选择一个音色")
                return
            try:
                os.startfile(str(profiles[row]["audio_path"]))  # type: ignore[attr-defined]
            except OSError as error:
                QMessageBox.critical(dialog, "无法试听", str(error))

        voice_list.currentRowChanged.connect(show_profile)
        add_voice_button.clicked.connect(add_voice)
        rename_voice_button.clicked.connect(rename_voice)
        delete_voice_button.clicked.connect(delete_selected_voice)
        upload_audio_button.clicked.connect(upload_audio)
        delete_audio_button.clicked.connect(delete_selected_voice)
        preview_audio_button.clicked.connect(preview_audio)
        save_button.clicked.connect(save_reference_text)
        reload_profiles()
        dialog.exec()

    def _sync_selected_local_voices(self) -> None:
        profiles = self._selected_local_voice_profiles()
        selected_local_ids = {
            str(profile["reference_id"]) for profile in profiles
        }
        profile_by_id = {
            str(profile["reference_id"]): profile for profile in profiles
        }

        remote_profiles = list_voices(self._server_url(), self._token())
        for local_id in selected_local_ids:
            remote_id = self.local_voice_remote_ids.get(local_id)
            if remote_id:
                continue
            profile = profile_by_id[local_id]
            reference_text = str(profile.get("reference_text", "")).strip()
            existing = next(
                (
                    item
                    for item in remote_profiles
                    if str(item.get("name", ""))
                    in {str(profile["name"]), self._cloud_voice_name(profile)}
                    and str(item.get("reference_text", "")).strip() == reference_text
                ),
                None,
            )
            if existing is not None:
                self.local_voice_remote_ids[local_id] = str(existing["reference_id"])
                self._log(f"已命中云端音色缓存：{profile['name']}")
                continue
            uploaded = upload_voice(
                self._server_url(),
                self._cloud_voice_name(profile),
                reference_text,
                self.local_voice_paths[local_id],
                self._token(),
            )
            self.local_voice_remote_ids[local_id] = str(uploaded["reference_id"])
            self._log(f"本地音色已准备：{profile['name']}")
        self._save_voice_remote_mapping()

    def _selected_local_voice_profiles(self) -> list[dict[str, object]]:
        selected_local_ids: set[str] = set()
        default_reference_id = str(self.default_voice_combo.currentData() or "")
        if default_reference_id.startswith("local:"):
            selected_local_ids.add(default_reference_id)
        for table in (self.table, self.interjection_table):
            for row in range(table.rowCount()):
                text_item = table.item(row, 3)
                if text_item is None or not text_item.text().strip():
                    continue
                voice_box = table.cellWidget(row, 1)
                if isinstance(voice_box, QComboBox):
                    reference_id = str(voice_box.currentData() or "")
                    if reference_id.startswith("local:"):
                        selected_local_ids.add(reference_id)

        profile_by_id = {
            str(profile.get("reference_id", "")): profile
            for profile in self.voice_profiles
        }
        profiles: list[dict[str, object]] = []
        for local_id in sorted(selected_local_ids):
            profile = profile_by_id.get(local_id)
            if profile is None:
                # A saved selection can still point at a valid cloud voice even
                # when the client moved and its absolute local ID changed.
                if local_id in self.local_voice_remote_ids:
                    continue
                raise ValueError("已选择的参考音频不存在，请刷新音色后重新选择")
            prepared = dict(profile)
            prepared["cloud_name"] = self._cloud_voice_name(profile)
            profiles.append(prepared)
        return profiles

    def _repair_missing_row_voice_selections(self) -> int:
        """Migrate stale per-row local IDs after updates or moving to another PC."""
        valid_ids = {
            str(profile.get("reference_id", "")) for profile in self.voice_profiles
        }
        references = [
            (
                self._reference_match_text(str(profile.get("reference_text", ""))),
                str(profile.get("reference_id", "")),
            )
            for profile in self.voice_profiles
            if profile.get("reference_id") and profile.get("reference_text")
        ]
        repaired = 0
        for table, label in ((self.table, "话术"), (self.interjection_table, "插播")):
            for row in range(table.rowCount()):
                voice_box = table.cellWidget(row, 1)
                text_item = table.item(row, 3)
                if not isinstance(voice_box, QComboBox) or text_item is None:
                    continue
                reference_id = str(voice_box.currentData() or "")
                if (
                    not reference_id.startswith("local:")
                    or reference_id in valid_ids
                    or reference_id in self.local_voice_remote_ids
                ):
                    continue
                variants = self._reference_match_variants(text_item.text())
                replacement = self._best_reference_match(variants, references)
                replacement_index = voice_box.findData(replacement) if replacement else -1
                if replacement_index >= 0:
                    voice_box.setCurrentIndex(replacement_index)
                    repaired += 1
                    self._log(f"已恢复第{row + 1}句{label}的参考音频")
                else:
                    # Do not let an invisible stale ID block the whole script.
                    # Falling back to the visible right-side default preserves
                    # playback and makes the effective voice unambiguous.
                    voice_box.setCurrentIndex(0)
                    self._log(
                        f"第{row + 1}句{label}的旧参考音频编号已失效，"
                        "本次改用右侧默认音色"
                    )
        return repaired

    def _load_voice_remote_mapping(self) -> dict[str, str]:
        try:
            if not self.voice_mapping_path.exists():
                return {}
            payload = json.loads(self.voice_mapping_path.read_text(encoding="utf-8"))
            servers = payload.get("servers", {}) if isinstance(payload, dict) else {}
            mapping = servers.get(self._server_url(), {}) if isinstance(servers, dict) else {}
            if not isinstance(mapping, dict):
                return {}
            return {
                str(local_id): str(remote_id)
                for local_id, remote_id in mapping.items()
                if local_id and remote_id
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _save_voice_remote_mapping(self) -> None:
        try:
            payload: dict[str, object] = {"version": 1, "servers": {}}
            if self.voice_mapping_path.exists():
                loaded = json.loads(
                    self.voice_mapping_path.read_text(encoding="utf-8")
                )
                if isinstance(loaded, dict):
                    payload = loaded
            servers = payload.setdefault("servers", {})
            if not isinstance(servers, dict):
                servers = {}
                payload["servers"] = servers
            servers[self._server_url()] = dict(self.local_voice_remote_ids)
            self.voice_mapping_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return

    def add_voice(self) -> None:
        audio_filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择参考音频",
            "",
            "WAV参考音频 (*.wav)",
        )
        if not audio_filename:
            return
        name, accepted = QInputDialog.getText(self, "音色名称", "请输入音色名称")
        if not accepted or not name.strip():
            return
        reference_text, accepted = QInputDialog.getMultiLineText(
            self,
            "参考音频原文",
            "请输入与参考音频完全一致的文字",
        )
        if not accepted or not reference_text.strip():
            return
        try:
            profile = upload_voice(
                self._server_url(),
                name.strip(),
                reference_text.strip(),
                Path(audio_filename),
                self._token(),
            )
            self._log(f"音色已上传: {profile['name']}")
            self.refresh_voices()
        except (OSError, httpx.HTTPError, ValueError) as error:
            QMessageBox.critical(self, "上传失败", str(error))

    def _add_interjection_row(
        self,
        _checked: bool = False,
        *,
        text: str = "",
        reference_id: str | None = None,
        speed: float = 1.0,
        line_id: str | None = None,
    ) -> None:
        self._insert_interjection_row(
            self.interjection_table.rowCount(),
            text=text,
            reference_id=reference_id,
            speed=speed,
            line_id=line_id,
        )

    def _insert_interjection_row(
        self,
        row: int,
        *,
        text: str = "",
        reference_id: str | None = None,
        speed: float = 1.0,
        line_id: str | None = None,
    ) -> None:
        self.interjection_table.insertRow(row)
        identifier = QTableWidgetItem(str(row + 1))
        preset_id = line_id or f"interjection-preset-{uuid.uuid4().hex[:8]}"
        identifier.setData(
            Qt.ItemDataRole.UserRole,
            preset_id,
        )
        identifier.setFlags(identifier.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.interjection_table.setItem(row, 0, identifier)
        voice_box = self._new_voice_box(reference_id)
        voice_box.currentIndexChanged.connect(
            lambda _index, current_row=row: self._update_interjection_tooltip(
                current_row
            )
        )
        self.interjection_table.setCellWidget(row, 1, voice_box)
        speed_box = QDoubleSpinBox()
        speed_box.setRange(0.5, 2.0)
        speed_box.setSingleStep(0.05)
        speed_box.setDecimals(2)
        speed_box.setSuffix("x")
        speed_box.setValue(speed)
        self.interjection_table.setCellWidget(row, 2, speed_box)
        self.interjection_table.setItem(row, 3, QTableWidgetItem(text))
        play_button = QPushButton("插播此话术")
        play_button.setObjectName("rowInterjectionButton")
        play_button.setToolTip("当前句完整播放结束后，优先插播这一句")
        play_button.clicked.connect(
            lambda _checked=False, identifier=preset_id: self._queue_interjection_by_id(
                identifier
            )
        )
        self.interjection_table.setCellWidget(row, 4, play_button)
        self.interjection_table.setRowHeight(row, 44)
        self._renumber_interjection_rows()
        self._update_interjection_tooltip(row)

    def _advance_interjection_row(self, row: int, text: str, cursor: int) -> None:
        left_text = text[:cursor].strip()
        right_text = text[cursor:].strip()
        current_item = self.interjection_table.item(row, 3)
        if current_item is not None:
            current_item.setText(left_text)
        speed_box = self.interjection_table.cellWidget(row, 2)
        speed = (
            float(speed_box.value())
            if isinstance(speed_box, QDoubleSpinBox)
            else 1.0
        )
        next_row = row + 1
        self._insert_interjection_row(next_row, text=right_text, speed=speed)
        self.interjection_table.setCurrentCell(next_row, 3)
        next_item = self.interjection_table.item(next_row, 3)
        if next_item is not None:
            self.interjection_table.scrollToItem(next_item)
            QTimer.singleShot(
                0, lambda item=next_item: self.interjection_table.editItem(item)
            )

    def _advance_interjection_row_at_end(self, row: int) -> None:
        item = self.interjection_table.item(row, 3)
        text = item.text() if item is not None else ""
        self._advance_interjection_row(row, text, len(text))

    def _delete_interjection_row(self, row: int) -> None:
        if 0 <= row < self.interjection_table.rowCount():
            self.interjection_table.removeRow(row)
        self._renumber_interjection_rows()
        if self.interjection_table.rowCount() == 0:
            self._add_interjection_row()

    def _renumber_interjection_rows(self) -> None:
        for row in range(self.interjection_table.rowCount()):
            identifier = self.interjection_table.item(row, 0)
            if identifier is not None:
                identifier.setText(str(row + 1))

    def _delete_interjection_rows(self) -> None:
        rows = sorted(
            {index.row() for index in self.interjection_table.selectedIndexes()},
            reverse=True,
        )
        if not rows and self.interjection_table.currentRow() >= 0:
            rows = [self.interjection_table.currentRow()]
        for row in rows:
            self.interjection_table.removeRow(row)
        self._renumber_interjection_rows()
        if self.interjection_table.rowCount() == 0:
            self._add_interjection_row()

    def _interjection_preset_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for row in range(self.interjection_table.rowCount()):
            text_item = self.interjection_table.item(row, 3)
            text = text_item.text().strip() if text_item is not None else ""
            if not text:
                continue
            identifier = self.interjection_table.item(row, 0)
            voice_box = self.interjection_table.cellWidget(row, 1)
            speed_box = self.interjection_table.cellWidget(row, 2)
            rows.append(
                {
                    "line_id": str(
                        identifier.data(Qt.ItemDataRole.UserRole)
                        if identifier is not None
                        else f"interjection-preset-{uuid.uuid4().hex[:8]}"
                    ),
                    "text": text,
                    "reference_id": (
                        str(voice_box.currentData())
                        if isinstance(voice_box, QComboBox)
                        and voice_box.currentData()
                        else None
                    ),
                    "speed": (
                        float(speed_box.value())
                        if isinstance(speed_box, QDoubleSpinBox)
                        else 1.0
                    ),
                }
            )
        return rows

    def _save_interjection_presets(self) -> None:
        rows = self._interjection_preset_rows()
        self.interjection_presets_path.write_text(
            json.dumps({"version": 1, "lines": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.interjection_status_label.setText(
            f"已保存 {len(rows)} 条插播话术及其参考音频。"
        )
        self._log(f"已保存插播话术: {len(rows)}条")

    def _load_interjection_presets(self) -> None:
        self.interjection_table.blockSignals(True)
        try:
            self.interjection_table.setRowCount(0)
            rows: list[dict[str, object]] = []
            if self.interjection_presets_path.exists():
                data = json.loads(
                    self.interjection_presets_path.read_text(encoding="utf-8")
                )
                if isinstance(data, dict) and isinstance(data.get("lines"), list):
                    rows = [item for item in data["lines"] if isinstance(item, dict)]
            for item in rows:
                self._add_interjection_row(
                    text=str(item.get("text", "")),
                    reference_id=(
                        str(item["reference_id"])
                        if item.get("reference_id")
                        else None
                    ),
                    speed=float(item.get("speed", 1.0)),
                    line_id=str(item.get("line_id") or "") or None,
                )
            if self.interjection_table.rowCount() == 0:
                self._add_interjection_row()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.interjection_table.setRowCount(0)
            self._add_interjection_row()
            self._log(f"读取插播话术失败: {error}")
        finally:
            self.interjection_table.blockSignals(False)

    def _update_interjection_tooltip(self, row: int) -> None:
        if row < 0 or row >= self.interjection_table.rowCount():
            return
        identifier = self.interjection_table.item(row, 0)
        voice_box = self.interjection_table.cellWidget(row, 1)
        if identifier is None or not isinstance(voice_box, QComboBox):
            return
        reference_id = str(voice_box.currentData() or "")
        profile = next(
            (
                item
                for item in self.voice_profiles
                if str(item.get("reference_id", "")) == reference_id
            ),
            None,
        )
        if profile is None:
            identifier.setToolTip("尚未选择本句参考音频")
            return
        identifier.setToolTip(
            f"参考音频: {profile.get('name', '')}\n\n参考原文:\n"
            f"{str(profile.get('reference_text', '')).strip()}"
        )

    def _show_interjection_context_menu(self, position: object) -> None:
        index = self.interjection_table.indexAt(position)
        if not index.isValid():
            return
        row = index.row()
        voice_box = self.interjection_table.cellWidget(row, 1)
        speed_box = self.interjection_table.cellWidget(row, 2)
        if not isinstance(voice_box, QComboBox):
            return
        menu = QMenu(self)
        play_action = menu.addAction("插播此话术")
        menu.addSeparator()
        voice_menu = menu.addMenu("选择参考音频")
        voice_actions: dict[object, int] = {}
        clear_voice_action = voice_menu.addAction("清除本句参考音频")
        voice_menu.addSeparator()
        for combo_index in range(1, voice_box.count()):
            action = voice_menu.addAction(voice_box.itemText(combo_index))
            action.setCheckable(True)
            action.setChecked(combo_index == voice_box.currentIndex())
            voice_actions[action] = combo_index
        speed_actions: dict[object, float] = {}
        speed_menu = menu.addMenu("调节语速")
        if isinstance(speed_box, QDoubleSpinBox):
            for speed in (0.75, 0.9, 1.0, 1.1, 1.25, 1.5):
                action = speed_menu.addAction(f"{speed:.2f}x")
                action.setCheckable(True)
                action.setChecked(abs(speed_box.value() - speed) < 0.001)
                speed_actions[action] = speed
        menu.addSeparator()
        delete_action = menu.addAction("删除本句插播话术")
        selected = menu.exec(self.interjection_table.viewport().mapToGlobal(position))
        if selected == play_action:
            self.interjection_table.setCurrentCell(row, 3)
            self._queue_selected_interjection()
        elif selected == clear_voice_action:
            voice_box.setCurrentIndex(0)
        elif selected in voice_actions:
            voice_box.setCurrentIndex(voice_actions[selected])
        elif selected in speed_actions and isinstance(speed_box, QDoubleSpinBox):
            speed_box.setValue(speed_actions[selected])
        elif selected == delete_action:
            self.interjection_table.removeRow(row)
            self._renumber_interjection_rows()
        self._update_interjection_tooltip(row)

    def add_row(
        self,
        text: str = "",
        reference_id: str | None = None,
        speed: float = 1.0,
        line_id: str | None = None,
    ) -> None:
        self._insert_row(
            self.table.rowCount(), text, reference_id, speed, line_id
        )

    def _insert_row(
        self,
        row: int,
        text: str = "",
        reference_id: str | None = None,
        speed: float = 1.0,
        line_id: str | None = None,
    ) -> None:
        self.table.insertRow(row)
        identifier = QTableWidgetItem(str(row + 1))
        identifier.setData(
            Qt.ItemDataRole.UserRole,
            line_id or f"line-{uuid.uuid4().hex[:8]}",
        )
        identifier.setFlags(identifier.flags() & ~Qt.ItemFlag.ItemIsEditable)
        identifier.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 0, identifier)
        voice_box = self._new_voice_box(reference_id)
        voice_box.currentIndexChanged.connect(self._update_voice_tooltips)
        self.table.setCellWidget(row, 1, voice_box)

        speed_box = QDoubleSpinBox()
        speed_box.setRange(0.5, 2.0)
        speed_box.setSingleStep(0.05)
        speed_box.setDecimals(2)
        speed_box.setSuffix("x")
        speed_box.setValue(speed)
        self.table.setCellWidget(row, 2, speed_box)
        self.table.setItem(row, 3, QTableWidgetItem(text))
        self._renumber()
        QTimer.singleShot(0, self._resize_script_rows)
        self._update_voice_tooltips()
        self._update_script_stats()

    def _update_script_stats(self) -> None:
        if not hasattr(self, "script_stats_label"):
            return
        texts = [self._row_text(row) for row in range(self.table.rowCount())]
        texts = [text for text in texts if text]
        character_count = sum(len(text) for text in texts)
        self.script_stats_label.setText(f"字数：{character_count} | 句数：{len(texts)}")

    def _resize_script_rows(self) -> None:
        if not hasattr(self, "table"):
            return
        for row in range(self.table.rowCount()):
            index = self.table.model().index(row, 3)
            option = QStyleOptionViewItem()
            option.font = self.table.font()
            option.rect.setWidth(max(120, self.table.columnWidth(3)))
            height = self.script_text_delegate.sizeHint(option, index).height()
            self.table.setRowHeight(row, height)

    def _row_text(self, row: int) -> str:
        item = self.table.item(row, 3)
        return item.text().strip() if item is not None else ""

    def _snapshot_rows(self) -> list[dict[str, object]]:
        snapshot: list[dict[str, object]] = []
        for row in range(self.table.rowCount()):
            voice_box = self.table.cellWidget(row, 1)
            speed_box = self.table.cellWidget(row, 2)
            identifier = self.table.item(row, 0)
            reference_id = (
                str(voice_box.currentData())
                if isinstance(voice_box, QComboBox) and voice_box.currentData()
                else None
            )
            profile = next(
                (
                    item
                    for item in self.voice_profiles
                    if str(item.get("reference_id", "")) == reference_id
                ),
                None,
            )
            snapshot.append(
                {
                    "text": self._row_text(row),
                    "reference_id": reference_id,
                    "reference_path": (
                        self._voice_metadata_key(Path(str(profile["audio_path"])))
                        if profile is not None
                        else None
                    ),
                    "reference_text": (
                        str(profile.get("reference_text", ""))
                        if profile is not None
                        else None
                    ),
                    "speed": speed_box.value() if isinstance(speed_box, QDoubleSpinBox) else 1.0,
                    "line_id": (
                        identifier.data(Qt.ItemDataRole.UserRole) if identifier else None
                    ),
                }
            )
        return snapshot

    def _restore_saved_reference_id(self, saved: dict[str, object]) -> str | None:
        reference_id = str(saved.get("reference_id") or "")
        valid_ids = {
            str(profile.get("reference_id", "")) for profile in self.voice_profiles
        }
        if reference_id in valid_ids or reference_id in self.local_voice_remote_ids:
            return reference_id or None

        saved_path = str(saved.get("reference_path") or "").replace("\\", "/")
        if saved_path:
            path_matches = [
                profile
                for profile in self.voice_profiles
                if self._voice_metadata_key(Path(str(profile.get("audio_path", ""))))
                .replace("\\", "/")
                .casefold()
                == saved_path.casefold()
            ]
            if len(path_matches) == 1:
                return str(path_matches[0]["reference_id"])

        saved_text = str(saved.get("reference_text") or "").strip()
        if saved_text:
            text_matches = [
                profile
                for profile in self.voice_profiles
                if str(profile.get("reference_text", "")).strip() == saved_text
            ]
            if len(text_matches) == 1:
                return str(text_matches[0]["reference_id"])
        return reference_id or None

    def _remember_history(self) -> None:
        if self._restoring_history:
            return
        snapshot = self._snapshot_rows()
        if self._history and self._history[-1] == snapshot:
            return
        self._history.append(snapshot)
        if len(self._history) > 100:
            self._history.pop(0)

    def _reset_history(self) -> None:
        self._history = [self._snapshot_rows()]

    def _undo_last_change(self) -> None:
        if len(self._history) <= 1:
            self._log("没有可以撤回的操作")
            return
        self._history.pop()
        snapshot = self._history[-1]
        self._restoring_history = True
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for row_data in snapshot:
                self.add_row(
                    str(row_data["text"]),
                    str(row_data["reference_id"]) if row_data["reference_id"] else None,
                    float(row_data["speed"]),
                    str(row_data["line_id"]) if row_data["line_id"] else None,
                )
        finally:
            self.table.blockSignals(False)
            self._restoring_history = False
        self._update_script_stats()
        self._log("已撤回上一步操作")

    def _ensure_trailing_row(self) -> None:
        """Keep one ready-to-edit row below the final sentence."""
        if self.table.rowCount() == 0:
            self.add_row()
            return
        last_row = self.table.rowCount() - 1
        if not self._row_text(last_row):
            return
        default_speed = (
            self.default_speed_spin.value()
            if hasattr(self, "default_speed_spin")
            else 1.0
        )
        self.add_row(speed=default_speed)

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 3:
            return
        # Pasted multi-line text becomes consecutive editable sentences.
        parts = split_script_sentences(item.text())
        if len(parts) > 1:
            row = item.row()
            speed_box = self.table.cellWidget(row, 2)
            speed = speed_box.value() if isinstance(speed_box, QDoubleSpinBox) else 1.0
            self.table.blockSignals(True)
            try:
                item.setText(parts[0])
                for text in parts[1:]:
                    self.add_row(text, speed=speed)
            finally:
                self.table.blockSignals(False)
        self._ensure_trailing_row()
        QTimer.singleShot(0, self._resize_script_rows)
        self._update_script_stats()
        self._auto_match_reference_audio([item.row()])
        self._remember_history()

    def _paste_script_text(self, clipboard_text: str) -> None:
        parts = split_script_sentences(clipboard_text)
        if not parts:
            return
        selected_script_rows = {
            index.row() for index in self.table.selectedIndexes() if index.column() == 3
        }
        replace_all = (
            self.table.rowCount() > 0
            and len(selected_script_rows) == self.table.rowCount()
        )
        start_row = 0 if replace_all else max(0, self.table.currentRow())
        self.table.blockSignals(True)
        try:
            if replace_all:
                self.table.setRowCount(0)
            for offset, text in enumerate(parts):
                row = start_row + offset
                if row >= self.table.rowCount():
                    self.add_row()
                item = self.table.item(row, 3)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, 3, item)
                if not replace_all and offset == 0 and item.text().strip():
                    item.setText(f"{item.text()}{text}")
                else:
                    item.setText(text)
        finally:
            self.table.blockSignals(False)
        self._ensure_trailing_row()
        self._renumber()
        self._update_script_stats()
        self._auto_match_reference_audio(
            range(start_row, start_row + len(parts))
        )
        self._remember_history()
        self.table.setCurrentCell(start_row + len(parts) - 1, 3)

    def _save_all_settings(self) -> None:
        self.save_project()
        self._save_interjection_presets()
        self._save_window_layout()
        self._log("全局设置、窗口大小和分栏比例已保存")

    def _rollback_to_previous_version(self) -> None:
        answer = QMessageBox.question(
            self,
            "恢复上一版本",
            "将关闭客户端并恢复上一版本。话术、参考音频和个人设置不会改变，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._save_all_settings()
            launch_rollback(application_root())
        except Exception as error:  # noqa: BLE001 - show launch failure in the UI
            QMessageBox.warning(self, "无法恢复", str(error))
            return
        QApplication.quit()

    def _check_for_updates(self) -> None:
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                self,
                "检查更新",
                f"当前是开发运行模式（版本 {APP_VERSION}），打包后的客户端支持一键更新。",
            )
            return
        if self.update_check_worker is not None:
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("正在检查…")
        worker = UpdateCheckWorker()
        self.update_check_worker = worker
        worker.available.connect(self._update_available)
        worker.current.connect(self._update_is_current)
        worker.failed.connect(self._update_failed)
        worker.finished.connect(self._update_check_finished)
        worker.start()

    def _update_check_finished(self) -> None:
        self.update_check_worker = None
        if self.update_download_worker is None:
            self.update_button.setEnabled(True)
            self.update_button.setText("检查更新")

    def _update_available(self, manifest: dict[str, object]) -> None:
        version = str(manifest.get("version") or "")
        notes = str(manifest.get("notes") or "本次更新包含功能改进和问题修复。")
        answer = QMessageBox.question(
            self,
            "发现新版本",
            f"发现新版本 {version}（当前 {APP_VERSION}）。\n\n{notes}\n\n"
            "点击“是”后将自动下载、替换客户端并重新打开。\n"
            "您的话术、参考音频和设置不会被替换。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.update_button.setText("正在下载…")
        progress = QProgressDialog("正在下载客户端更新…", "取消", 0, 100, self)
        progress.setWindowTitle("一键更新")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(False)
        progress.setMinimumDuration(0)
        self.update_progress_dialog = progress
        worker = UpdateDownloadWorker(manifest)
        self.update_download_worker = worker
        worker.progress.connect(progress.setValue)
        worker.downloaded.connect(self._update_downloaded)
        worker.failed.connect(self._update_failed)
        worker.finished.connect(self._update_download_finished)
        progress.canceled.connect(worker.requestInterruption)
        worker.start()

    def _update_is_current(self, version: str) -> None:
        QMessageBox.information(self, "检查更新", f"当前已是最新版本 {version}。")

    def _update_downloaded(self, package_path: str) -> None:
        if self.update_progress_dialog is not None:
            self.update_progress_dialog.setValue(100)
            self.update_progress_dialog.close()
        try:
            self._save_all_settings()
            launch_updater(Path(package_path), application_root())
        except Exception as error:  # noqa: BLE001 - show update failure in the UI
            self._update_failed(str(error))
            return
        QMessageBox.information(
            self,
            "正在安装更新",
            "客户端将自动关闭、完成替换并重新打开。",
        )
        QApplication.quit()

    def _update_download_finished(self) -> None:
        self.update_download_worker = None
        self.update_progress_dialog = None
        self.update_button.setEnabled(True)
        self.update_button.setText("检查更新")

    def _update_failed(self, message: str) -> None:
        if self.update_progress_dialog is not None:
            self.update_progress_dialog.close()
        self.update_button.setEnabled(True)
        self.update_button.setText("检查更新")
        QMessageBox.warning(self, "更新失败", f"暂时无法完成更新：\n{message}")

    def _layout_settings(self) -> QSettings:
        # Keep layout beside the project instead of the Windows user registry.
        # The client may be launched from Explorer, PowerShell or an elevated
        # launcher; all of them must read the exact same saved layout.
        return QSettings(
            str(self.config_dir / "window-layout.ini"),
            QSettings.Format.IniFormat,
        )

    def _save_window_layout(self) -> None:
        settings = self._layout_settings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/maximized", self.isMaximized())
        settings.setValue("layout/verticalSplitter", self.vertical_splitter.sizes())
        settings.setValue("layout/mainSplitter", self.main_splitter.sizes())
        settings.setValue("layout/bottomSplitter", self.bottom_splitter.sizes())
        settings.setValue("tts/randomness", self._randomness_mode())
        settings.setValue("tts/serverHost", self.server_host_edit.text().strip())
        settings.setValue("tts/serverPort", self.server_port_spin.value())
        settings.setValue(
            "tts/defaultVoice", str(self.default_voice_combo.currentData() or "")
        )
        settings.setValue(
            "tts/serverScheme", str(self.connection_mode_combo.currentData())
        )
        settings.sync()

    def _restore_saved_window_layout(self) -> None:
        settings = self._layout_settings()
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        main_sizes = self._saved_splitter_sizes(
            settings.value("layout/mainSplitter")
        )
        vertical_sizes = self._saved_splitter_sizes(
            settings.value("layout/verticalSplitter")
        )
        bottom_sizes = self._saved_splitter_sizes(
            settings.value("layout/bottomSplitter")
        )
        if len(main_sizes) == 3 and all(size > 0 for size in main_sizes):
            self.main_splitter.setSizes(main_sizes)
        if len(vertical_sizes) == 2 and all(size > 0 for size in vertical_sizes):
            self.vertical_splitter.setSizes(vertical_sizes)
        if len(bottom_sizes) == 3 and all(size > 0 for size in bottom_sizes):
            self.bottom_splitter.setSizes(bottom_sizes)
        self._set_randomness_mode(str(settings.value("tts/randomness", "normal")))
        saved_host = str(settings.value("tts/serverHost", "")).strip()
        if saved_host:
            self.server_host_edit.setText(saved_host)
        try:
            saved_port = int(settings.value("tts/serverPort", 8000))
        except (TypeError, ValueError):
            saved_port = 8000
        self.server_port_spin.setValue(saved_port)
        self._saved_default_voice_id = str(
            settings.value("tts/defaultVoice", "")
        ).strip()
        saved_scheme = str(settings.value("tts/serverScheme", "http"))
        scheme_index = self.connection_mode_combo.findData(saved_scheme)
        if scheme_index >= 0:
            self.connection_mode_combo.setCurrentIndex(scheme_index)

    @staticmethod
    def _saved_splitter_sizes(value: object) -> list[int]:
        if not isinstance(value, (list, tuple)):
            return []
        try:
            return [int(size) for size in value]
        except (TypeError, ValueError):
            return []

    def show_with_saved_layout(self) -> None:
        maximized = self._layout_settings().value(
            "window/maximized", False, type=bool
        )
        if maximized:
            self.showMaximized()
        else:
            self.show()
        QTimer.singleShot(0, self._restore_saved_window_layout)

    def _advance_script_row(self, row: int, text: str, cursor: int) -> None:
        left_text = text[:cursor].strip()
        right_text = text[cursor:].strip()
        current_item = self.table.item(row, 3)
        if current_item is not None:
            current_item.setText(left_text)

        next_row = row + 1
        speed_box = self.table.cellWidget(row, 2)
        speed = speed_box.value() if isinstance(speed_box, QDoubleSpinBox) else 1.0
        # Enter always creates a real new row. Existing following rows (and
        # their reference-audio assignments) are shifted down unchanged.
        self._insert_row(next_row, right_text, speed=speed)

        self._ensure_trailing_row()
        self.table.setCurrentCell(next_row, 3)
        next_item = self.table.item(next_row, 3)
        if next_item is not None:
            self.table.scrollToItem(next_item)
            QTimer.singleShot(0, lambda: self.table.editItem(next_item))
        self._remember_history()

    def _advance_table_row_at_end(self, row: int) -> None:
        """Handle Enter when the script cell is selected but not being edited."""
        text = self._row_text(row)
        self._advance_script_row(row, text, len(text))

    def _new_voice_box(self, reference_id: str | None = None) -> QComboBox:
        voice_box = QComboBox()
        voice_box.addItem("使用右侧默认音色", None)
        for profile in self.voice_profiles:
            voice_box.addItem(
                str(profile.get("menu_name", profile["name"])),
                str(profile["reference_id"]),
            )
        if reference_id:
            index = voice_box.findData(reference_id)
            if index < 0:
                voice_box.addItem(f"缺失: {reference_id}", reference_id)
                index = voice_box.count() - 1
            voice_box.setCurrentIndex(index)
        return voice_box

    def _show_table_context_menu(self, position: object) -> None:
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        row = index.row()
        column = index.column()
        menu = QMenu(self)

        if column == 0:
            # Files may have been added or moved into subfolders after startup.
            # Rescan before building the sequence-number context menu.
            voice_box = self.table.cellWidget(row, 1)
            if not isinstance(voice_box, QComboBox):
                return
            inherit_action = menu.addAction("使用右侧默认音色")
            inherit_action.setCheckable(True)
            inherit_action.setChecked(voice_box.currentData() is None)
            menu.addSeparator()
            select_voice_action = menu.addAction("选择参考音频…")
            choose_file_action = menu.addAction("选择本地参考音频…")
            refresh_action = menu.addAction("刷新参考音频")
            open_folder_action = menu.addAction("打开参考音频文件夹")
            selected = menu.exec(self.table.viewport().mapToGlobal(position))
            if selected == inherit_action:
                voice_box.setCurrentIndex(0)
                self._log(f"第{row + 1}句已改用右侧默认音色")
            elif selected == select_voice_action:
                self._select_reference_audio_for_row(row)
            elif selected == choose_file_action:
                self._choose_local_voice_for_row(row)
            elif selected == refresh_action:
                self._refresh_local_voices()
            elif selected == open_folder_action:
                self._open_reference_audio_folder()
            return

        if column == 3:
            speed_box = self.table.cellWidget(row, 2)
            voice_box = self.table.cellWidget(row, 1)
            if not isinstance(speed_box, QDoubleSpinBox):
                return
            start_here_action = menu.addAction(f"▶ 从第{row + 1}句开始播放")
            cancel_start_action = None
            if self.playback_start_line_id:
                cancel_start_action = menu.addAction("取消指定位置（恢复从头播放）")
            selected_rows = sorted(
                {item.row() for item in self.table.selectedIndexes() if item.column() == 3}
                or {row}
            )
            selected_ids = {
                str(self.table.item(selected_row, 0).data(Qt.ItemDataRole.UserRole))
                for selected_row in selected_rows
                if self.table.item(selected_row, 0) is not None
                and self._row_text(selected_row)
            }
            all_blocked = bool(selected_ids) and selected_ids <= self.blocked_line_ids
            block_action = menu.addAction(
                "恢复播放选中行" if all_blocked else "禁播选中行（直接跳过）"
            )
            menu.addSeparator()
            voice_menu = menu.addMenu("选择参考音频")
            inherit_voice_action = None
            select_voice_action = None
            if isinstance(voice_box, QComboBox):
                inherit_voice_action = voice_menu.addAction("使用右侧默认音色")
                inherit_voice_action.setCheckable(True)
                inherit_voice_action.setChecked(voice_box.currentData() is None)
                voice_menu.addSeparator()
                select_voice_action = voice_menu.addAction("打开参考音频选择窗口…")
            voice_menu.addSeparator()
            choose_file_action = voice_menu.addAction("选择本地参考音频…")
            refresh_voice_action = voice_menu.addAction("刷新参考音频")
            open_voice_folder_action = voice_menu.addAction("打开参考音频文件夹")
            menu.addSeparator()
            speed_menu = menu.addMenu("调节语速")
            speed_actions: dict[object, float] = {}
            for speed in (0.5, 0.75, 0.8, 0.9, 1.0, 1.1, 1.2, 1.25, 1.5, 2.0):
                action = speed_menu.addAction(f"{speed:.2f}x")
                action.setCheckable(True)
                action.setChecked(abs(speed_box.value() - speed) < 0.001)
                speed_actions[action] = speed
            speed_menu.addSeparator()
            custom_action = speed_menu.addAction("自定义语速…")
            menu.addSeparator()
            delete_action = menu.addAction("删除本句话术")
            selected = menu.exec(self.table.viewport().mapToGlobal(position))
            if selected == start_here_action:
                self._set_playback_start_row(row)
                return
            if cancel_start_action is not None and selected == cancel_start_action:
                self._clear_playback_start()
                return
            if selected == block_action:
                self._set_rows_blocked(selected_ids, not all_blocked)
                return
            if (
                isinstance(voice_box, QComboBox)
                and selected == inherit_voice_action
            ):
                voice_box.setCurrentIndex(0)
                self._log(f"第{row + 1}句已改用右侧默认音色")
                return
            if (
                select_voice_action is not None
                and selected == select_voice_action
                and isinstance(voice_box, QComboBox)
            ):
                self._select_reference_audio_for_row(row)
                return
            if selected == choose_file_action:
                self._choose_local_voice_for_row(row)
                return
            if selected == refresh_voice_action:
                self._refresh_local_voices()
                return
            if selected == open_voice_folder_action:
                self._open_reference_audio_folder()
                return
            if selected in speed_actions:
                speed_box.setValue(speed_actions[selected])
            elif selected == custom_action:
                speed, accepted = QInputDialog.getDouble(
                    self,
                    "调节语速",
                    f"第{row + 1}句话术语速（0.5-2.0）",
                    speed_box.value(),
                    0.5,
                    2.0,
                    2,
                )
                if accepted:
                    speed_box.setValue(speed)
            elif selected == delete_action:
                self.remove_row(row)
                return
            else:
                return
            self._log(f"第{row + 1}句语速已设为：{speed_box.value():.2f}x")

    def _select_reference_audio_for_row(self, row: int) -> None:
        """Open one persistent picker for assigning several script rows."""
        self._reference_audio_picker(row)

    def _reference_audio_picker(self, start_row: int = 0) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("选择参考音频")
        dialog.setModal(True)
        dialog.resize(980, 560)
        dialog.setMinimumSize(820, 480)
        dialog.setMaximumSize(1180, 720)

        layout = QVBoxLayout(dialog)
        title = QLabel("选择参考音频")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        search_edit = QLineEdit()
        search_edit.setObjectName("searchInput")
        search_edit.setPlaceholderText("搜索文件夹、音频文件名或参考文案")
        layout.addWidget(search_edit)

        content = QSplitter(Qt.Orientation.Horizontal)
        folder_panel = QWidget()
        folder_layout = QVBoxLayout(folder_panel)
        folder_layout.setContentsMargins(0, 0, 4, 0)
        folder_layout.addWidget(QLabel("参考音频文件夹"))
        folder_list = QListWidget()
        folder_layout.addWidget(folder_list)
        content.addWidget(folder_panel)

        audio_panel = QWidget()
        audio_layout = QVBoxLayout(audio_panel)
        audio_layout.setContentsMargins(4, 0, 0, 0)
        audio_layout.addWidget(QLabel("参考音频（显示文案前5个字）"))
        audio_list = QListWidget()
        audio_layout.addWidget(audio_list)
        content.addWidget(audio_panel)

        script_panel = QWidget()
        script_layout = QVBoxLayout(script_panel)
        script_layout.setContentsMargins(4, 0, 0, 0)
        script_header = QHBoxLayout()
        script_header.addWidget(QLabel("话术序号（可连续设置）"))
        script_filter = QComboBox()
        script_filter.addItem("全部话术", "all")
        script_filter.addItem("只看未选择", "unassigned")
        script_header.addWidget(script_filter)
        script_layout.addLayout(script_header)
        script_list = QListWidget()
        script_layout.addWidget(script_list)
        content.addWidget(script_panel)
        content.setSizes([190, 300, 450])
        layout.addWidget(content, 1)

        footer = QHBoxLayout()
        hint = QLabel("悬浮音频可查看完整文案和文件名")
        hint.setObjectName("hintLabel")
        footer.addWidget(hint)
        footer.addStretch()
        close_button = QPushButton("关闭")
        apply_button = QPushButton("应用到本句")
        apply_button.setObjectName("saveButton")
        apply_button.setEnabled(False)
        footer.addWidget(close_button)
        footer.addWidget(apply_button)
        layout.addLayout(footer)

        profiles = [
            profile
            for profile in self.voice_profiles
            if profile.get("local") and profile.get("audio_path")
        ]
        reference_root = self.reference_audio_dir.resolve()
        folder_name_cache: dict[str, str] = {}

        def folder_name(profile: dict[str, object]) -> str:
            reference_id = str(profile.get("reference_id", ""))
            cached = folder_name_cache.get(reference_id)
            if cached is not None:
                return cached
            audio_path = Path(str(profile["audio_path"]))
            try:
                parent = audio_path.resolve().relative_to(reference_root).parent.as_posix()
            except ValueError:
                parent = audio_path.parent.name
            result = "根目录" if parent == "." else parent
            folder_name_cache[reference_id] = result
            return result

        profiles.sort(
            key=lambda profile: natural_sort_key(
                f"{folder_name(profile)}/{Path(str(profile['audio_path'])).name}"
            )
        )
        profile_by_id = {
            str(profile.get("reference_id", "")): profile
            for profile in profiles
            if profile.get("reference_id")
        }
        profile_label_by_id = {
            reference_id: str(profile.get("menu_name", profile.get("name", "")))
            for reference_id, profile in profile_by_id.items()
        }
        profile_search_text = {
            reference_id: " ".join(
                (
                    folder_name(profile),
                    Path(str(profile["audio_path"])).name,
                    str(profile.get("reference_text", "")),
                )
            ).casefold()
            for reference_id, profile in profile_by_id.items()
        }

        def matching_profiles(query: str) -> list[dict[str, object]]:
            normalized = query.strip().casefold()
            if not normalized:
                return profiles
            return [
                profile
                for profile in profiles
                if normalized
                in profile_search_text.get(str(profile.get("reference_id", "")), "")
            ]

        filtered_profiles: list[dict[str, object]] = []

        def target_row() -> int:
            item = script_list.currentItem()
            return (
                int(item.data(Qt.ItemDataRole.UserRole))
                if item is not None
                else -1
            )

        def current_target_reference_id() -> str:
            row = target_row()
            voice_box = self.table.cellWidget(row, 1) if row >= 0 else None
            return (
                str(voice_box.currentData() or "")
                if isinstance(voice_box, QComboBox)
                else ""
            )

        def profile_label(reference_id: str) -> str:
            return profile_label_by_id.get(reference_id, "")

        def populate_script_list(preferred_row: int | None = None) -> None:
            previous_row = target_row() if preferred_row is None else preferred_row
            only_unassigned = script_filter.currentData() == "unassigned"
            script_list.blockSignals(True)
            script_list.clear()
            selected_index = -1
            for row in range(self.table.rowCount()):
                text = self._row_text(row)
                if not text:
                    continue
                voice_box = self.table.cellWidget(row, 1)
                reference_id = (
                    str(voice_box.currentData() or "")
                    if isinstance(voice_box, QComboBox)
                    else ""
                )
                if only_unassigned and reference_id:
                    continue
                status = "✓" if reference_id else "○"
                summary = text if len(text) <= 18 else f"{text[:18]}…"
                selected_voice = profile_label(reference_id)
                suffix = f"  [{selected_voice}]" if selected_voice else ""
                script_list.addItem(f"{status} {row + 1}  {summary}{suffix}")
                item = script_list.item(script_list.count() - 1)
                item.setData(Qt.ItemDataRole.UserRole, row)
                item.setToolTip(
                    f"第{row + 1}句\n{text}\n\n"
                    f"参考音频：{selected_voice or '尚未单独选择'}"
                )
                if row == previous_row:
                    selected_index = script_list.count() - 1
            if script_list.count():
                script_list.setCurrentRow(max(0, selected_index))
            script_list.blockSignals(False)
            select_target_audio()

        def select_target_audio() -> None:
            reference_id = current_target_reference_id()
            if not reference_id:
                apply_button.setEnabled(
                    script_list.currentItem() is not None
                    and audio_list.currentItem() is not None
                )
                return
            profile = profile_by_id.get(reference_id)
            if profile is None:
                return
            wanted_folder = folder_name(profile)
            folder_changed = False
            for folder_row in range(folder_list.count()):
                folder_item = folder_list.item(folder_row)
                if str(folder_item.data(Qt.ItemDataRole.UserRole)) == wanted_folder:
                    if folder_list.currentRow() != folder_row:
                        folder_list.blockSignals(True)
                        folder_list.setCurrentRow(folder_row)
                        folder_list.blockSignals(False)
                        folder_changed = True
                    break
            if folder_changed:
                populate_audio_list()
            audio_list.blockSignals(True)
            for audio_row in range(audio_list.count()):
                audio_item = audio_list.item(audio_row)
                if str(audio_item.data(Qt.ItemDataRole.UserRole)) == reference_id:
                    audio_list.setCurrentRow(audio_row)
                    break
            audio_list.blockSignals(False)
            apply_button.setEnabled(
                audio_list.currentItem() is not None
                and script_list.currentItem() is not None
            )

        def populate_audio_list() -> None:
            audio_list.clear()
            selected_folder_item = folder_list.currentItem()
            if selected_folder_item is None:
                apply_button.setEnabled(False)
                return
            selected_folder = str(
                selected_folder_item.data(Qt.ItemDataRole.UserRole) or ""
            )
            folder_profiles = [
                profile
                for profile in filtered_profiles
                if folder_name(profile) == selected_folder
            ]
            for profile in folder_profiles:
                audio_path = Path(str(profile["audio_path"]))
                reference_text = str(profile.get("reference_text", "")).strip()
                preview = reference_text[:5] or audio_path.stem[:5]
                audio_list.addItem(preview)
                item = audio_list.item(audio_list.count() - 1)
                item.setData(Qt.ItemDataRole.UserRole, str(profile["reference_id"]))
                item.setToolTip(
                    f"文件夹：{selected_folder}\n"
                    f"文件名：{audio_path.name}\n\n"
                    f"参考文案：\n{reference_text or '未填写'}"
                )
                if str(profile["reference_id"]) == current_target_reference_id():
                    audio_list.setCurrentItem(item)
            if audio_list.currentRow() < 0 and audio_list.count():
                audio_list.setCurrentRow(0)
            apply_button.setEnabled(
                audio_list.currentItem() is not None
                and script_list.currentItem() is not None
            )

        def populate_folder_list(query: str = "") -> None:
            nonlocal filtered_profiles
            previous = (
                str(folder_list.currentItem().data(Qt.ItemDataRole.UserRole) or "")
                if folder_list.currentItem() is not None
                else ""
            )
            filtered_profiles = matching_profiles(query)
            folders = sorted(
                {folder_name(profile) for profile in filtered_profiles},
                key=natural_sort_key,
            )
            current_profile = profile_by_id.get(current_target_reference_id())
            current_folder = (
                folder_name(current_profile) if current_profile is not None else ""
            )
            folder_list.blockSignals(True)
            folder_list.clear()
            preferred_row = -1
            for folder in folders:
                folder_list.addItem(folder)
                item = folder_list.item(folder_list.count() - 1)
                item.setData(Qt.ItemDataRole.UserRole, folder)
                if folder == previous:
                    preferred_row = folder_list.count() - 1
                if folder == current_folder:
                    preferred_row = folder_list.count() - 1
            if folder_list.count():
                folder_list.setCurrentRow(max(0, preferred_row))
            else:
                audio_list.clear()
                apply_button.setEnabled(False)
            folder_list.blockSignals(False)
            if folder_list.count():
                populate_audio_list()

        def persist_assignment() -> None:
            path = self.current_project_path
            if path is None:
                return
            if path.suffix.lower() == ".json":
                path.write_text(
                    json.dumps(self._project_data(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                self._save_current_text_row_settings(path)

        def next_unassigned_row(after_row: int) -> int:
            row_count = self.table.rowCount()
            for candidate in list(range(after_row + 1, row_count)) + list(
                range(after_row + 1)
            ):
                if not self._row_text(candidate):
                    continue
                voice_box = self.table.cellWidget(candidate, 1)
                if isinstance(voice_box, QComboBox) and not voice_box.currentData():
                    return candidate
            return after_row

        def apply_to_target() -> None:
            row = target_row()
            selected_audio = audio_list.currentItem()
            if row < 0 or selected_audio is None:
                return
            reference_id = str(
                selected_audio.data(Qt.ItemDataRole.UserRole) or ""
            )
            voice_box = self.table.cellWidget(row, 1)
            if not reference_id or not isinstance(voice_box, QComboBox):
                return
            index = voice_box.findData(reference_id)
            if index < 0:
                return
            voice_box.setCurrentIndex(index)
            self._update_voice_tooltips()
            self._remember_history()
            persist_assignment()
            self._log(f"第{row + 1}句参考音频已设为：{voice_box.currentText()}")
            populate_script_list(next_unassigned_row(row))

        folder_list.currentRowChanged.connect(lambda _row: populate_audio_list())
        audio_list.currentRowChanged.connect(
            lambda _row: apply_button.setEnabled(
                audio_list.currentItem() is not None
                and script_list.currentItem() is not None
            )
        )
        script_list.currentRowChanged.connect(lambda _row: select_target_audio())
        script_filter.currentIndexChanged.connect(
            lambda _index: populate_script_list()
        )
        audio_list.itemDoubleClicked.connect(lambda _item: apply_to_target())
        search_timer = QTimer(dialog)
        search_timer.setSingleShot(True)
        search_timer.setInterval(160)
        search_timer.timeout.connect(
            lambda: populate_folder_list(search_edit.text())
        )
        search_edit.textChanged.connect(lambda _text: search_timer.start())
        close_button.clicked.connect(dialog.accept)
        apply_button.clicked.connect(apply_to_target)
        populate_folder_list()
        populate_script_list(start_row)
        dialog.exec()

    def _choose_local_voice_for_row(self, row: int) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择本地参考音频",
            str(self.reference_audio_dir),
            "音频文件 (*.wav *.mp3 *.flac *.m4a)",
        )
        if not filename:
            return
        source = Path(filename)
        destination = self.reference_audio_dir / source.name
        if source.resolve() != destination.resolve():
            counter = 2
            while destination.exists():
                destination = self.reference_audio_dir / (
                    f"{source.stem}-{counter}{source.suffix.lower()}"
                )
                counter += 1
            shutil.copy2(source, destination)

        metadata = self._load_voice_metadata()
        reference_text = str(
            metadata.get(
                self._voice_metadata_key(destination),
                metadata.get(destination.name, ""),
            )
        ).strip()
        if not reference_text:
            reference_text, accepted = QInputDialog.getMultiLineText(
                self,
                "参考音频原文",
                "请输入与参考音频完全一致的文字\n（可直接使用音频文件名作为原文）",
                destination.stem,
            )
            if not accepted or not reference_text.strip():
                QMessageBox.information(
                    self, "尚未选择", "必须填写参考音频原文才能克隆音色。"
                )
                return
            metadata[self._voice_metadata_key(destination)] = reference_text.strip()
            self._save_voice_metadata(metadata)

        self._refresh_local_voices()
        local_id = next(
            (
                reference_id
                for reference_id, audio_path in self.local_voice_paths.items()
                if audio_path.resolve() == destination.resolve()
            ),
            None,
        )
        voice_box = self.table.cellWidget(row, 1)
        if local_id and isinstance(voice_box, QComboBox):
            index = voice_box.findData(local_id)
            if index >= 0:
                voice_box.setCurrentIndex(index)
                self._log(f"第{row + 1}句已选择本地音色：{destination.stem}")

    def _refresh_voice_boxes(self) -> None:
        for row in range(self.table.rowCount()):
            existing = self.table.cellWidget(row, 1)
            selected = existing.currentData() if isinstance(existing, QComboBox) else None
            voice_box = self._new_voice_box(selected)
            voice_box.currentIndexChanged.connect(self._update_voice_tooltips)
            self.table.setCellWidget(row, 1, voice_box)
        for row in range(self.interjection_table.rowCount()):
            existing = self.interjection_table.cellWidget(row, 1)
            selected = existing.currentData() if isinstance(existing, QComboBox) else None
            voice_box = self._new_voice_box(selected)
            voice_box.currentIndexChanged.connect(
                lambda _index, current_row=row: self._update_interjection_tooltip(
                    current_row
                )
            )
            self.interjection_table.setCellWidget(row, 1, voice_box)
            self._update_interjection_tooltip(row)
        self._update_voice_tooltips()

    @staticmethod
    def _reference_match_text(text: str) -> str:
        """Normalize text only for exact transcript-to-script matching."""
        text = re.sub(r"#\d+(?:\.\d+)?#", "", text)
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE)

    @classmethod
    def _reference_match_variants(cls, script_text: str) -> set[str]:
        """Expand bracket choices while keeping matching exact after normalization."""
        variants = [script_text]
        choice_pattern = re.compile(r"\[([^\[\]]+)\]")
        while len(variants) <= 256:
            expanded: list[str] = []
            changed = False
            for variant in variants:
                match = choice_pattern.search(variant)
                if match is None:
                    expanded.append(variant)
                    continue
                changed = True
                choices = [part.strip() for part in match.group(1).split("|") if part.strip()]
                for choice in choices:
                    expanded.append(
                        f"{variant[:match.start()]}{choice}{variant[match.end():]}"
                    )
            variants = expanded
            if not changed:
                break
        return {cls._reference_match_text(variant) for variant in variants}

    def _auto_match_reference_audio(self, rows: object = None) -> int:
        """Bind the best local transcript match only when similarity is >= 90%."""
        references: list[tuple[str, str]] = []
        for profile in self.voice_profiles:
            reference_id = str(profile.get("reference_id", ""))
            reference_text = self._reference_match_text(
                str(profile.get("reference_text", ""))
            )
            if reference_id and reference_text:
                references.append((reference_text, reference_id))

        row_numbers = range(self.table.rowCount()) if rows is None else rows
        matched = 0
        for row in row_numbers:
            if not isinstance(row, int) or row < 0 or row >= self.table.rowCount():
                continue
            variants = self._reference_match_variants(self._row_text(row))
            reference_id = self._best_reference_match(variants, references)
            voice_box = self.table.cellWidget(row, 1)
            if (
                not any(variants)
                or reference_id is None
                or not isinstance(voice_box, QComboBox)
                or voice_box.currentData() is not None
            ):
                continue
            index = voice_box.findData(reference_id)
            if index >= 0:
                voice_box.setCurrentIndex(index)
                matched += 1
        if matched:
            self._update_voice_tooltips()
        return matched

    @staticmethod
    def _best_reference_match(
        variants: set[str],
        references: list[tuple[str, str]],
        threshold: float = 0.90,
    ) -> str | None:
        best_id: str | None = None
        best_score = 0.0
        for reference_text, reference_id in references:
            for variant in variants:
                if not variant or not reference_text:
                    continue
                score = (
                    1.0
                    if variant == reference_text
                    else SequenceMatcher(
                        None, variant, reference_text, autojunk=False
                    ).ratio()
                )
                if score > best_score:
                    best_score = score
                    best_id = reference_id
        return best_id if best_score >= threshold else None

    def _update_voice_tooltips(self, _index: int = -1) -> None:
        default_reference_id = str(self.default_voice_combo.currentData() or "")
        for row in range(self.table.rowCount()):
            identifier = self.table.item(row, 0)
            voice_box = self.table.cellWidget(row, 1)
            if identifier is None or not isinstance(voice_box, QComboBox):
                continue
            explicit_reference_id = str(voice_box.currentData() or "")
            if explicit_reference_id:
                reference_id = explicit_reference_id
                source_label = "本句序号单独指定"
            else:
                reference_id = default_reference_id
                source_label = "右侧默认音色"
            profile = next(
                (
                    item
                    for item in self.voice_profiles
                    if str(item.get("reference_id", "")) == reference_id
                ),
                None,
            )
            if profile is None:
                identifier.setToolTip("尚未选择默认参考音频")
                continue
            name = str(profile.get("name", ""))
            reference_text = str(profile.get("reference_text", "")).strip()
            same_as_filename = self._same_voice_name_and_text(name, reference_text)
            tooltip = f"参考音频：{name}\n来源：{source_label}"
            if reference_text and not same_as_filename:
                tooltip += f"\n\n参考原文：\n{reference_text}"
            elif not reference_text:
                tooltip += "\n\n参考原文：未填写"
            identifier.setToolTip(tooltip)

    @staticmethod
    def _same_voice_name_and_text(name: str, reference_text: str) -> bool:
        normalized_name = Path(name).stem.strip().rstrip("。！？!?，,；; ")
        normalized_text = reference_text.strip().rstrip("。！？!?，,；; ")
        return bool(normalized_name) and normalized_name == normalized_text

    def _track_edit_cursor(self, row: int, cursor: int) -> None:
        self._last_edit_row = row
        self._last_edit_cursor = cursor

    def _insert_pause(self) -> None:
        focus_widget = QApplication.focusWidget()
        editor = focus_widget if isinstance(focus_widget, ScriptLineEditor) else None
        if editor is not None:
            row = int(editor.property("script_row"))
            editor.insert("#500#")
            self._track_edit_cursor(row, editor.cursorPosition())
            self._remember_history()
            return

        row = self._last_edit_row if self._last_edit_row >= 0 else self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "插入停顿", "请先选中一句话术")
            return
        item = self.table.item(row, 3)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, 3, item)
        text = item.text()
        cursor = self._last_edit_cursor if self._last_edit_row == row else len(text)
        cursor = max(0, min(cursor, len(text)))
        item.setText(f"{text[:cursor]}#500#{text[cursor:]}")
        self.table.setCurrentCell(row, 3)
        self.table.editItem(item)
        QTimer.singleShot(0, lambda: self._restore_editor_cursor(row, cursor + 5))
        self._remember_history()

    def _restore_editor_cursor(self, row: int, cursor: int) -> None:
        editor = QApplication.focusWidget()
        if not isinstance(editor, ScriptLineEditor):
            return
        if int(editor.property("script_row")) != row:
            return
        editor.setCursorPosition(min(cursor, len(editor.text())))

    def _apply_default_voice(self) -> None:
        reference_id = self.default_voice_combo.currentData()
        if not reference_id:
            QMessageBox.information(self, "请选择音色", "请先选择一个默认音色")
            return
        for row in range(self.table.rowCount()):
            voice_box = self.table.cellWidget(row, 1)
            if isinstance(voice_box, QComboBox):
                index = voice_box.findData(reference_id)
                if index >= 0:
                    voice_box.setCurrentIndex(index)
        self._log("默认音色已应用到全部话术")

    def _on_default_voice_changed(self, _index: int = -1) -> None:
        reference_id = self.default_voice_combo.currentData()
        profile = next(
            (
                item
                for item in self.voice_profiles
                if str(item["reference_id"]) == str(reference_id)
            ),
            None,
        )
        self.reference_text_edit.setText(
            str(profile.get("reference_text", "")) if profile is not None else ""
        )
        self.reference_text_edit.setCursorPosition(0)
        self.reference_text_edit.deselect()
        self._update_voice_tooltips()
        if reference_id:
            self._log("默认音色已更新，未单独指定的整段话术将自动使用该音色")

    def _randomness_mode(self) -> str:
        if self.disable_randomness_check.isChecked():
            return "off"
        if self.low_randomness_check.isChecked():
            return "low"
        return "normal"

    def _set_randomness_mode(self, mode: str) -> None:
        low = mode == "low"
        off = mode == "off"
        self.low_randomness_check.blockSignals(True)
        self.disable_randomness_check.blockSignals(True)
        try:
            self.low_randomness_check.setChecked(low)
            self.disable_randomness_check.setChecked(off)
        finally:
            self.low_randomness_check.blockSignals(False)
            self.disable_randomness_check.blockSignals(False)

    def _on_low_randomness_toggled(self, checked: bool) -> None:
        if checked:
            self.disable_randomness_check.blockSignals(True)
            self.disable_randomness_check.setChecked(False)
            self.disable_randomness_check.blockSignals(False)

    def _on_disable_randomness_toggled(self, checked: bool) -> None:
        if checked:
            self.low_randomness_check.blockSignals(True)
            self.low_randomness_check.setChecked(False)
            self.low_randomness_check.blockSignals(False)

    def _change_buffer_target(self, value: int) -> None:
        self.target_buffer_lines = value
        if hasattr(self, "buffer_target_label"):
            self.buffer_target_label.setText(f"{value}句")

    def _refresh_project_library(self) -> None:
        selected_path = self.current_project_path
        self.project_list.clear()
        files = sorted(
            [
                *self.scripts_dir.glob("*.json"),
                *self.scripts_dir.glob("*.txt"),
                *self.scripts_dir.glob("*.tst"),
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files:
            self.project_list.addItem(path.name)
            item = self.project_list.item(self.project_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            if selected_path is not None and path == selected_path:
                self.project_list.setCurrentItem(item)
        self._filter_project_library(self.project_search_edit.text())

    def _filter_project_library(self, query: str) -> None:
        normalized = query.strip().casefold()
        for index in range(self.project_list.count()):
            item = self.project_list.item(index)
            item.setHidden(bool(normalized) and normalized not in item.text().casefold())

    def _update_current_file_label(self) -> None:
        if not hasattr(self, "current_file_label"):
            return
        label = self.current_project_path.name if self.current_project_path else "未保存话术"
        self.current_file_label.setText(label)

    def _remember_current_project(self) -> None:
        settings = self._layout_settings()
        if self.current_project_path is None:
            settings.remove("session/lastProject")
        else:
            settings.setValue(
                "session/lastProject", str(self.current_project_path.resolve())
            )
        settings.sync()

    def _restore_last_project(self) -> None:
        saved_path = str(
            self._layout_settings().value("session/lastProject", "") or ""
        ).strip()
        if not saved_path:
            first_item = self.project_list.item(0)
            if first_item is None:
                return
            saved_path = str(first_item.data(Qt.ItemDataRole.UserRole) or "").strip()
        path = Path(saved_path)
        if path.is_file() and path.suffix.lower() in {".txt", ".tst", ".json"}:
            self._load_project_file(path, remember=False)

    def _new_project(self) -> None:
        name, accepted = QInputDialog.getText(self, "新建话术", "请输入话术名称")
        name = name.strip()
        if not accepted or not name:
            return
        if any(character in name for character in '<>:"/\\|?*'):
            QMessageBox.warning(self, "名称无效", "话术名称不能包含特殊符号")
            return
        base_name = Path(name).stem if Path(name).suffix.lower() in {".txt", ".json"} else name
        path = self.scripts_dir / f"{base_name}.txt"
        if path.exists():
            QMessageBox.warning(self, "无法新建", "同名话术已经存在")
            return
        path.write_text("", encoding="utf-8")
        self.current_project_path = path
        self._save_current_text_row_settings(path)
        self._remember_current_project()
        self._update_current_file_label()
        self.table.setRowCount(0)
        self.add_row()
        self._refresh_project_library()
        self._log(f"已新建话术: {path.name}")

    def _delete_project(self) -> None:
        item = self.project_list.currentItem()
        if item is None:
            QMessageBox.information(self, "删除话术", "请先在左侧选择一个话术")
            return
        path = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        answer = QMessageBox.question(
            self,
            "删除话术",
            f"确定删除“{path.name}”吗？\n文件会移入话术回收文件夹。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        trash_dir = self.scripts_dir / ".trash"
        trash_dir.mkdir(exist_ok=True)
        destination = trash_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.name}"
        try:
            shutil.move(str(path), str(destination))
        except OSError as error:
            QMessageBox.critical(self, "删除失败", str(error))
            return
        if self.current_project_path == path:
            self.current_project_path = None
            self._remember_current_project()
            self._update_current_file_label()
        self._refresh_project_library()
        self._log(f"话术已移入回收文件夹: {path.name}")

    def _open_scripts_folder(self) -> None:
        try:
            os.startfile(str(self.scripts_dir))  # type: ignore[attr-defined]
        except OSError as error:
            QMessageBox.critical(self, "无法打开文件夹", str(error))

    def _open_project_item(self, item: object) -> None:
        if not hasattr(item, "data"):
            return
        path = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        self._load_project_file(path)

    def _confirm_project_switch(self) -> None:
        item = self.project_list.currentItem()
        if item is None:
            QMessageBox.information(self, "切换话术", "请先选择需要切换的话术")
            return
        path = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        if self.current_project_path is not None and path == self.current_project_path:
            self._log(f"当前已经是话术: {path.name}")
            return
        answer = QMessageBox.question(
            self,
            "确认切换话术",
            f"确定切换到“{path.name}”吗？\n当前未保存的修改不会自动保存。",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._load_project_file(path)

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            self.table.removeRow(row)
        self._renumber()
        if self.table.rowCount() == 0:
            self.add_row()
        self._update_script_stats()
        self._remember_history()

    def remove_current_row(self) -> None:
        self.remove_row(self.table.currentRow())

    def add_empty_row(self) -> None:
        self.add_row(
            speed=(
                self.default_speed_spin.value()
                if hasattr(self, "default_speed_spin")
                else 1.0
            )
        )
        row = self.table.rowCount() - 1
        self.table.setCurrentCell(row, 3)
        item = self.table.item(row, 3)
        if item is not None:
            QTimer.singleShot(0, lambda: self.table.editItem(item))
        self._remember_history()

    def remove_row(self, row: int) -> None:
        if row < 0 or row >= self.table.rowCount():
            QMessageBox.information(self, "删除话术", "请先点击要删除的那句话术")
            return
        self.table.removeRow(row)
        self._renumber()
        if self.table.rowCount() > 0:
            self.table.setCurrentCell(min(row, self.table.rowCount() - 1), 3)
        self._update_script_stats()
        self._remember_history()

    def _renumber(self) -> None:
        for row in range(self.table.rowCount()):
            identifier = self.table.item(row, 0)
            if identifier is None:
                continue
            line_id = str(identifier.data(Qt.ItemDataRole.UserRole) or "")
            prefix = "▶ " if line_id == self.playback_start_line_id else ""
            blocked = "⛔ " if line_id in self.blocked_line_ids else ""
            identifier.setText(f"{blocked}{prefix}{row + 1}")
            identifier.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            text_item = self.table.item(row, 3)
            if text_item is not None:
                text_item.setForeground(
                    QColor("#637783" if line_id in self.blocked_line_ids else "#ecf4f6")
                )

    def _set_rows_blocked(self, line_ids: set[str], blocked: bool) -> None:
        if blocked:
            self.blocked_line_ids.update(line_ids)
            removed = self.player.remove_queued_lines(line_ids)
            self.playback_lines = [
                line for line in self.playback_lines if line.line_id not in line_ids
            ]
            self._log(f"已禁播 {len(line_ids)} 句，跳过本地缓存 {removed} 句")
        else:
            self.blocked_line_ids.difference_update(line_ids)
            self._log(f"已恢复 {len(line_ids)} 句；下次启动时生效")
        self._renumber()
        self._fill_generation_buffer()

    def _set_playback_start_row(self, row: int) -> None:
        identifier = self.table.item(row, 0)
        if identifier is None or not self._row_text(row):
            return
        self.playback_start_line_id = str(
            identifier.data(Qt.ItemDataRole.UserRole) or ""
        )
        self._renumber()
        self.table.setCurrentCell(row, 3)
        self._log(f"已指定从第{row + 1}句开始播放")

    @staticmethod
    def _slice_lines_from_id(
        lines: list[ScriptLine], start_line_id: str | None
    ) -> list[ScriptLine]:
        if not start_line_id:
            return lines
        for index, line in enumerate(lines):
            if line.line_id == start_line_id:
                return lines[index:]
        return lines

    def _clear_playback_start(self) -> None:
        self.playback_start_line_id = None
        self._renumber()
        self._log("已取消指定位置，下次从第一句开始播放")

    def _project_data(self) -> dict[str, object]:
        return {"version": 1, "lines": [line.to_dict() for line in self._collect_lines()]}

    def _collect_lines(self) -> list[ScriptLine]:
        lines: list[ScriptLine] = []
        default_reference_id = str(self.default_voice_combo.currentData() or "")
        for row in range(self.table.rowCount()):
            voice_box = self.table.cellWidget(row, 1)
            speed_box = self.table.cellWidget(row, 2)
            text_item = self.table.item(row, 3)
            text = text_item.text().strip() if text_item else ""
            if not text:
                continue
            line_id = str(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
            if line_id in self.blocked_line_ids:
                continue
            explicit_reference_id = str(voice_box.currentData() or "")
            # A voice selected on this row overrides the right-side default
            # for this row only. It must never leak into following rows.
            reference_id = self._resolve_row_reference_id(
                explicit_reference_id, default_reference_id
            )
            if not reference_id:
                raise ValueError("请先在右侧选择默认音色")
            parse_script(text)
            lines.append(
                ScriptLine(
                    line_id=line_id,
                    reference_id=str(reference_id),
                    speed=float(speed_box.value()),
                    language="Chinese",
                    randomness=self._randomness_mode(),
                    text=text,
                )
            )
        return lines

    def _resolve_row_reference_id(
        self, explicit_reference_id: str, default_reference_id: str
    ) -> str:
        """Resolve one row independently: row override, otherwise default."""
        reference_id = explicit_reference_id or default_reference_id
        return self.local_voice_remote_ids.get(reference_id, reference_id)

    def _queue_interjection_by_id(self, preset_id: str) -> None:
        for row in range(self.interjection_table.rowCount()):
            identifier = self.interjection_table.item(row, 0)
            if (
                identifier is not None
                and str(identifier.data(Qt.ItemDataRole.UserRole)) == preset_id
            ):
                self.interjection_table.setCurrentCell(row, 3)
                self._queue_selected_interjection()
                return
        QMessageBox.information(self, "插播话术", "这条插播话术已经不存在")

    def _queue_selected_interjection(self) -> None:
        row = self.interjection_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "插播话术", "请先选择一条插播话术")
            return
        if self.worker is None or not (self.playback_started or self.countdown_active):
            QMessageBox.information(self, "插播话术", "请先启动直播话术播放")
            return
        text_item = self.interjection_table.item(row, 3)
        text = text_item.text().strip() if text_item is not None else ""
        voice_box = self.interjection_table.cellWidget(row, 1)
        speed_box = self.interjection_table.cellWidget(row, 2)
        reference_id = (
            str(voice_box.currentData() or "")
            if isinstance(voice_box, QComboBox)
            else ""
        )
        if not text:
            QMessageBox.information(self, "插播话术", "这条插播话术还是空白的")
            return
        if not reference_id:
            QMessageBox.information(
                self, "插播话术", "请在本句序号上右键选择对应参考音频"
            )
            return
        try:
            parse_script(text)
            self._sync_selected_local_voices()
        except (OSError, httpx.HTTPError, ValueError) as error:
            QMessageBox.warning(self, "无法插播", str(error))
            return

        task_line = ScriptLine(
            line_id=f"interjection-{uuid.uuid4().hex[:10]}",
            reference_id=self._resolve_row_reference_id(reference_id, ""),
            speed=(
                float(speed_box.value())
                if isinstance(speed_box, QDoubleSpinBox)
                else 1.0
            ),
            language="Chinese",
            randomness=self._randomness_mode(),
            text=text,
        )
        self.interjection_lines[task_line.line_id] = task_line
        if self.interjection_worker is None:
            self.interjection_worker = TTSWorker(self._server_url(), self._token())
            self.interjection_worker.lineReady.connect(self._on_interjection_ready)
            self.interjection_worker.lineError.connect(self._on_interjection_error)
            self.interjection_worker.start()

        if (
            self.player.has_current_line
            and not self.player.is_paused
            and not self.pause_requested
        ):
            self.player.request_pause_after_current_line()
            self.interjection_auto_pause = True
        self.interjection_worker.submit(task_line)
        self.interjection_status_label.setText(
            f"已排队: 第{row + 1}条。将在当前句播完后插播。"
        )
        self._log(f"插播话术已提交: {text[:30]}")

    def _on_interjection_ready(self, generated: object) -> None:
        if not isinstance(generated, AudioLine):
            return
        source_line = self.interjection_lines.get(generated.line_id)
        self.player.enqueue_priority(generated)
        rtf = (
            generated.generation_seconds / generated.audio_seconds
            if generated.audio_seconds > 0
            else 0.0
        )
        summary = source_line.text if source_line is not None else ""
        summary = summary if len(summary) <= 30 else summary[:30] + "..."
        self._log(
            f"插播TTS: ✓ {summary} | 生成 {generated.generation_seconds:.2f}s | "
            f"音频 {generated.audio_seconds:.2f}s | RTF:{rtf:.2f}"
        )
        if self.interjection_auto_pause:
            if self.player.is_paused:
                self.interjection_auto_pause = False
                self.player.resume()
            elif self.player.has_current_line:
                self.player.cancel_pause_after_current_line()
                self.interjection_auto_pause = False
        self.interjection_status_label.setText("插播音频已就绪，等待当前句结束。")

    def _on_interjection_error(self, line_id: str, message: str) -> None:
        self.interjection_lines.pop(line_id, None)
        if self.interjection_auto_pause:
            if self.player.is_paused:
                self.player.resume()
            elif self.player.has_current_line:
                self.player.cancel_pause_after_current_line()
            self.interjection_auto_pause = False
        self.interjection_status_label.setText(f"插播生成失败: {message}")
        self._log(f"插播生成失败: {message}")

    def save_project(self) -> None:
        path = self.current_project_path
        if path is None:
            filename, _ = QFileDialog.getSaveFileName(
                self,
                "保存话术",
                str(self.scripts_dir / "话术.txt"),
                "文本文档 (*.txt);;AI直播话术 (*.json)",
            )
            if not filename:
                return
            path = Path(filename)
        if path.suffix.lower() == ".json":
            try:
                data = self._project_data()
            except ValueError as error:
                QMessageBox.warning(self, "无法保存", str(error))
                return
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            if path.suffix.lower() != ".txt":
                path = path.with_suffix(".txt")
            texts = [self._row_text(row) for row in range(self.table.rowCount())]
            path.write_text(
                "\n".join(text for text in texts if text),
                encoding="utf-8",
            )
        self.current_project_path = path
        self._save_current_text_row_settings(path)
        self._remember_current_project()
        self._update_current_file_label()
        self._refresh_project_library()
        self._log(f"话术已保存: {path.name}")

    def open_project(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "打开话术",
            str(self.scripts_dir),
            "话术文件 (*.json *.txt *.tst)",
        )
        if not filename:
            return
        self._load_project_file(Path(filename))

    def _load_project_file(self, path: Path, *, remember: bool = True) -> None:
        try:
            self.playback_start_line_id = None
            self.blocked_line_ids.clear()
            self.table.blockSignals(True)
            self.table.setRowCount(0)
            if path.suffix.lower() in {".txt", ".tst"}:
                source_text = path.read_text(encoding="utf-8-sig")
                saved_rows = self._saved_text_rows(path)
                for index, text in enumerate(split_script_sentences(source_text)):
                    saved = saved_rows[index] if index < len(saved_rows) else {}
                    same_text = str(saved.get("text", "")).strip() == text.strip()
                    self.add_row(
                        text,
                        self._restore_saved_reference_id(saved) if same_text else None,
                        float(saved.get("speed", 1.0)) if same_text else 1.0,
                        str(saved.get("line_id"))
                        if same_text and saved.get("line_id")
                        else None,
                    )
                self.current_project_path = path
                if remember:
                    self._remember_current_project()
                self._update_current_file_label()
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
                for item in data["lines"]:
                    line = ScriptLine.from_dict(item)
                    self.add_row(
                        line.text,
                        line.reference_id,
                        line.speed,
                        line.line_id,
                    )
                self.current_project_path = path
                if remember:
                    self._remember_current_project()
                self._update_current_file_label()
            if self.table.rowCount() == 0:
                self.add_row()
            self.table.blockSignals(False)
            self._ensure_trailing_row()
            self._repair_missing_row_voice_selections()
            self._auto_match_reference_audio()
            self._reset_history()
            self._refresh_project_library()
            self._log(f"已打开话术: {path.name}")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.table.blockSignals(False)
            QMessageBox.critical(self, "无法打开", str(error))

    def toggle_start_stop(self) -> None:
        if self.voice_sync_worker is not None:
            return
        if self.local_mode_radio.isChecked():
            if self.local_synthesis_active:
                self.stop_local_synthesis()
            else:
                self.start_local_synthesis()
            return
        if self.worker is not None or self.playback_started or self.countdown_active:
            self.stop_playback()
        else:
            self.start_playback()

    def _update_run_mode_ui(self, local_mode: bool) -> None:
        if self.local_synthesis_active or self.worker is not None:
            return
        self.local_progress_label.setVisible(local_mode)
        self.pause_button.setEnabled(False if local_mode else self.playback_started)
        self.start_button.setText("开始合成" if local_mode else "启动")
        self.status_label.setText(
            "本地模式：等待开始" if local_mode else "等待连接TTS服务"
        )
        if local_mode:
            self.local_progress_label.setText("已完成 0/0 | 失败 0")

    def _set_run_mode_locked(self, locked: bool) -> None:
        self.ai_mode_radio.setEnabled(not locked)
        self.local_mode_radio.setEnabled(not locked)

    def start_local_synthesis(self) -> None:
        try:
            self._repair_missing_row_voice_selections()
            self._sync_selected_local_voices()
            source_lines = self._collect_lines()
        except (OSError, httpx.HTTPError, ValueError) as error:
            QMessageBox.warning(self, "无法合成", str(error))
            return
        if not source_lines:
            QMessageBox.warning(self, "无法合成", "至少需要一句话术")
            return

        resolved_lines: list[ScriptLine] = []
        spoken_texts: dict[str, str] = {}
        for line in source_lines:
            spoken_text = resolve_random_choices(line.text)
            parse_script(spoken_text)
            resolved_lines.append(replace(line, text=spoken_text))
            spoken_texts[line.line_id] = spoken_text

        script_name = (
            self.current_project_path.stem
            if self.current_project_path is not None
            else "未保存话术"
        )
        try:
            removed = cleanup_history(self.audio_history_dir, retention_days=7)
            output_dir, archived = prepare_output_batch(
                self.local_audio_dir,
                self.audio_history_dir,
                script_name,
            )
        except OSError as error:
            QMessageBox.warning(self, "无法准备本地文件夹", str(error))
            return

        self.local_lines = resolved_lines
        self.local_spoken_texts = spoken_texts
        self.local_attempts = {}
        self.local_line_index = 0
        self.local_success_count = 0
        self.local_failure_count = 0
        self.local_failure_messages = []
        self.local_output_dir = output_dir
        self.local_synthesis_stopping = False
        self.local_synthesis_active = True
        self._set_run_mode_locked(True)
        self.start_button.setText("停止合成")
        self.pause_button.setEnabled(False)
        self.local_progress_label.setVisible(True)
        self._update_local_progress()
        self.status_label.setText("正在本地合成，不播放音频")
        self._log(f"本地合成开始，共{len(resolved_lines)}句")
        if archived is not None:
            self._log(f"上一批音频已移入历史文件夹：{archived.name}")
        if removed:
            self._log(f"已清理{len(removed)}个超过7天的历史批次")

        self.local_worker = TTSWorker(self._server_url(), self._token())
        self.local_worker.connected.connect(self._mark_worker_connected)
        self.local_worker.lineReady.connect(self._on_local_line_ready)
        self.local_worker.lineError.connect(self._on_local_line_error)
        self.local_worker.start()
        self._submit_current_local_line()

    def _submit_current_local_line(self) -> None:
        if not self.local_synthesis_active or self.local_worker is None:
            return
        if self.local_line_index >= len(self.local_lines):
            self._finish_local_synthesis()
            return
        line = self.local_lines[self.local_line_index]
        attempt = self.local_attempts.get(line.line_id, 0) + 1
        self.local_attempts[line.line_id] = attempt
        self.local_worker.submit(line)
        self.status_label.setText(
            f"正在合成第{self.local_line_index + 1}/{len(self.local_lines)}句"
        )
        self._log(
            f"本地合成提交：第{self.local_line_index + 1}句"
            + (f"（第{attempt}次尝试）" if attempt > 1 else "")
        )

    def _on_local_line_ready(self, generated: object) -> None:
        if not self.local_synthesis_active or not isinstance(generated, AudioLine):
            return
        if self.local_line_index >= len(self.local_lines):
            return
        line = self.local_lines[self.local_line_index]
        if generated.line_id != line.line_id:
            return
        try:
            if self.local_output_dir is None:
                raise OSError("本地合成目录不存在")
            spoken_text = self.local_spoken_texts.get(line.line_id, line.text)
            output_path = unique_wav_path(self.local_output_dir, spoken_text)
            write_audio_tokens_wav(generated.tokens, output_path)
        except (OSError, ValueError) as error:
            self._record_local_failure(line, f"保存WAV失败：{error}")
            return

        self.local_success_count += 1
        self.local_line_index += 1
        rtf = (
            generated.generation_seconds / generated.audio_seconds
            if generated.audio_seconds > 0
            else 0.0
        )
        self._log(
            f"本地TTS: ✓ {output_path.name} | "
            f"生成 {generated.generation_seconds:.2f}s | "
            f"音频 {generated.audio_seconds:.2f}s | RTF:{rtf:.2f}"
        )
        self._update_local_progress()
        self._submit_current_local_line()

    def _on_local_line_error(self, line_id: str, message: str) -> None:
        if not self.local_synthesis_active or self.local_line_index >= len(self.local_lines):
            return
        line = self.local_lines[self.local_line_index]
        if line.line_id != line_id:
            return
        attempt = self.local_attempts.get(line_id, 1)
        if attempt < 3:
            self._log(f"本地合成第{self.local_line_index + 1}句失败，自动重试：{message}")
            self._submit_current_local_line()
            return
        self._record_local_failure(line, message)

    def _record_local_failure(self, line: ScriptLine, message: str) -> None:
        number = self.local_line_index + 1
        self.local_failure_count += 1
        self.local_failure_messages.append(f"第{number}句：{message}")
        self._log(f"本地合成第{number}句连续3次失败，已跳过：{message}")
        self.local_line_index += 1
        self._update_local_progress()
        self._submit_current_local_line()

    def _update_local_progress(self) -> None:
        total = len(self.local_lines)
        completed = self.local_success_count + self.local_failure_count
        self.local_progress_label.setText(
            f"已完成 {completed}/{total} | 成功 {self.local_success_count} | "
            f"失败 {self.local_failure_count}"
        )

    def stop_local_synthesis(self) -> None:
        if not self.local_synthesis_active:
            return
        self.local_synthesis_stopping = True
        self.local_synthesis_active = False
        self._shutdown_local_worker()
        self._set_run_mode_locked(False)
        self.start_button.setText("开始合成")
        self.pause_button.setEnabled(False)
        self.status_label.setText("本地合成已停止，已生成音频予以保留")
        self._update_local_progress()
        self._log(
            f"本地合成已停止：成功{self.local_success_count}句，"
            f"失败{self.local_failure_count}句"
        )

    def _finish_local_synthesis(self) -> None:
        if not self.local_synthesis_active:
            return
        self.local_synthesis_active = False
        self._shutdown_local_worker()
        self._set_run_mode_locked(False)
        self.start_button.setText("开始合成")
        self.pause_button.setEnabled(False)
        self._update_local_progress()
        QApplication.beep()
        if self.local_failure_count:
            QTimer.singleShot(250, QApplication.beep)
            self.status_label.setText("本地合成完成，部分话术失败")
            details = "\n".join(self.local_failure_messages[:10])
            QMessageBox.warning(
                self,
                "本地合成完成",
                f"成功 {self.local_success_count} 句，失败 {self.local_failure_count} 句。"
                + (f"\n\n{details}" if details else ""),
            )
        else:
            self.status_label.setText("本地合成全部完成")
            QMessageBox.information(
                self,
                "本地合成完成",
                f"已成功生成 {self.local_success_count} 句音频。\n"
                f"保存位置：{self.local_output_dir}",
            )
        self._log(
            f"本地合成结束：成功{self.local_success_count}句，"
            f"失败{self.local_failure_count}句"
        )

    def _shutdown_local_worker(self) -> None:
        if self.local_worker is None:
            return
        worker = self.local_worker
        self.local_worker = None
        worker.shutdown()
        worker.wait(3000)

    def stop_playback(self) -> None:
        self.countdown_timer.stop()
        self.countdown_active = False
        self.countdown_value = 0
        self.player.reset()
        self._shutdown_worker()
        self.playback_lines = []
        self.line_positions = {}
        self.next_submit_index = 0
        self.inflight_count = 0
        self.finished_count = 0
        self.playback_started = False
        self.pause_requested = False
        self.manually_paused = False
        self.play_asap_requested = False
        self.start_button.setText("启动")
        self.start_button.setEnabled(True)
        self.pause_button.setText("暂停/继续")
        self.pause_button.setEnabled(False)
        self.current_line_label.setText("—")
        self.status_label.setText("已停止，可重新点击启动")
        self._log("用户停止播放：已清空播放缓存和生成任务")

    def start_playback(self) -> None:
        try:
            self._repair_missing_row_voice_selections()
            all_lines = self._collect_lines()
            lines = self._slice_lines_from_id(
                all_lines, self.playback_start_line_id
            )
            profiles = self._selected_local_voice_profiles()
        except (OSError, httpx.HTTPError, ValueError) as error:
            QMessageBox.warning(self, "无法播放", str(error))
            return
        if not lines:
            QMessageBox.warning(self, "无法播放", "至少需要一句话术")
            return
        self._refresh_output_devices()
        if not self.output_devices:
            QMessageBox.warning(self, "无法播放", "没有检测到可用音频输出设备")
            return

        if not profiles:
            self._start_playback_after_voice_sync()
            return

        self.start_button.setText("准备音色中…")
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.status_label.setText(f"正在准备参考音色 0/{len(profiles)}")
        self._log(f"启动前准备参考音色，共{len(profiles)}个")
        worker = VoiceSyncWorker(
            self._server_url(),
            self._token(),
            profiles,
            {**self._load_voice_remote_mapping(), **self.local_voice_remote_ids},
        )
        self.voice_sync_worker = worker
        worker.progress.connect(self._on_voice_sync_progress)
        worker.completed.connect(self._on_voice_sync_completed)
        worker.failed.connect(self._on_voice_sync_failed)
        worker.start()

    def _on_voice_sync_progress(self, current: int, total: int, message: str) -> None:
        self.status_label.setText(f"正在准备参考音色 {current}/{total}")
        self._log(f"音色准备 {current}/{total}：{message}")

    def _finish_voice_sync_worker(self) -> None:
        worker = self.voice_sync_worker
        if worker is None:
            return
        worker.wait(1000)
        self.voice_sync_worker = None

    def _on_voice_sync_completed(self, mapping: object) -> None:
        if isinstance(mapping, dict):
            self.local_voice_remote_ids.update(
                {
                    str(local_id): str(remote_id)
                    for local_id, remote_id in mapping.items()
                }
            )
        self._save_voice_remote_mapping()
        self._finish_voice_sync_worker()
        self._log("参考音色准备完成，开始生成直播音频")
        self._start_playback_after_voice_sync()

    def _on_voice_sync_failed(self, message: str) -> None:
        self._finish_voice_sync_worker()
        self.start_button.setText("启动")
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.status_label.setText("参考音色准备失败")
        self._log(f"参考音色准备失败：{message}")
        QMessageBox.warning(self, "无法播放", f"参考音色准备失败：\n{message}")

    def _start_playback_after_voice_sync(self) -> None:
        try:
            all_lines = self._collect_lines()
            lines = self._slice_lines_from_id(
                all_lines, self.playback_start_line_id
            )
        except (OSError, httpx.HTTPError, ValueError) as error:
            self.start_button.setText("启动")
            self.start_button.setEnabled(True)
            QMessageBox.warning(self, "无法播放", str(error))
            return

        self._shutdown_worker()
        self.player.reset()
        self.playback_lines = lines
        table_positions = {
            str(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)): row + 1
            for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
        }
        self.line_positions = {
            line.line_id: table_positions.get(line.line_id, index + 1)
            for index, line in enumerate(lines)
        }
        self.next_submit_index = 0
        self.inflight_count = 0
        self.finished_count = 0
        self.playback_started = False
        self.pause_requested = False
        self.manually_paused = False
        self.countdown_timer.stop()
        self.countdown_active = False
        self.countdown_value = 0
        self.play_asap_requested = False
        self.start_button.setText("停止")
        self.pause_button.setText("暂停/继续")

        session_id = self.playback_session_id
        self.worker = TTSWorker(self._server_url(), self._token())
        self.worker.connected.connect(
            lambda current_session=session_id: self._on_worker_connected(
                current_session
            )
        )
        self.worker.lineReady.connect(
            lambda generated, current_session=session_id: self._on_worker_line_ready(
                current_session, generated
            )
        )
        self.worker.lineError.connect(
            lambda line_id, message, current_session=session_id: self._on_worker_line_error(
                current_session, line_id, message
            )
        )
        self.worker.start()

        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(True)
        initial_target = min(self.target_buffer_lines, len(self.playback_lines))
        self.status_label.setText(f"正在生成初始{initial_target}句缓冲…")
        self._log(f"开始任务，共{len(lines)}句话术")
        self._fill_generation_buffer()

    def _mark_worker_connected(self) -> None:
        self.connection_status.setText("已连接")
        self.header_connection_status.setText("服务已连接")
        self.header_connection_status.setProperty("connected", True)
        self.header_connection_status.style().unpolish(self.header_connection_status)
        self.header_connection_status.style().polish(self.header_connection_status)
        self._set_connection_locked(True)

    def _on_worker_connected(self, session_id: int) -> None:
        if session_id != self.playback_session_id:
            return
        self._mark_worker_connected()

    def _on_worker_line_ready(self, session_id: int, generated: object) -> None:
        if session_id != self.playback_session_id:
            return
        self._on_line_ready(generated)

    def _on_worker_line_error(
        self, session_id: int, line_id: str, message: str
    ) -> None:
        if session_id != self.playback_session_id:
            return
        self._on_line_error(line_id, message)

    def _fill_generation_buffer(self) -> None:
        if self.worker is None:
            return
        # The UI value is only the number of ready sentences required before
        # playback begins. Keep the worker's own serial job queue filled to the
        # 20-sentence rolling target as well. TTSWorker processes one request at
        # a time, so queueing 20 jobs does not create concurrent cloud requests;
        # it merely prevents a busy Qt event loop from starving the worker after
        # the former three-job allowance has completed.
        generation_target = self.rolling_buffer_lines
        inflight_target = generation_target
        while (
            self.player.buffered_line_count + self.inflight_count < generation_target
            and self.inflight_count < inflight_target
            and self.next_submit_index < len(self.playback_lines)
        ):
            line = self.playback_lines[self.next_submit_index]
            if line.line_id in self.blocked_line_ids:
                self.next_submit_index += 1
                continue
            self.worker.submit(line)
            self.next_submit_index += 1
            self.inflight_count += 1
            self._log(f"提交生成: 第{self.line_positions[line.line_id]}句")

    def _on_line_ready(self, generated: object) -> None:
        if not isinstance(generated, AudioLine):
            return
        self.inflight_count = max(0, self.inflight_count - 1)
        if generated.line_id in self.blocked_line_ids:
            self._log(
                f"已跳过禁播的第{self.line_positions.get(generated.line_id, 0)}句"
            )
            self._fill_generation_buffer()
            return
        self.player.enqueue(generated)
        source_line = next(
            (line for line in self.playback_lines if line.line_id == generated.line_id),
            None,
        )
        summary = source_line.text if source_line is not None else ""
        summary = summary if len(summary) <= 30 else summary[:30] + "..."
        rtf = (
            generated.generation_seconds / generated.audio_seconds
            if generated.audio_seconds > 0
            else 0.0
        )
        self._log(
            "Qwen3TTS: ✓ "
            f"{summary} | 生成 {generated.generation_seconds:.2f}s | "
            f"音频 {generated.audio_seconds:.2f}s | RTF:{rtf:.2f}"
        )

        initial_target = min(self.target_buffer_lines, len(self.playback_lines))
        ready_target = 1 if self.play_asap_requested else initial_target
        if (
            not self.playback_started
            and not self.manually_paused
            and self.player.buffered_line_count >= ready_target
        ):
            self._begin_start_countdown()
        self._fill_generation_buffer()

    def _on_line_error(self, line_id: str, message: str) -> None:
        self.inflight_count = max(0, self.inflight_count - 1)
        number = self.line_positions.get(line_id, 0)
        self._log(f"第{number}句生成失败: {message}")
        self.status_label.setText(f"生成失败：第{number}句")
        self.player.reset()
        self.countdown_timer.stop()
        self.countdown_active = False
        self._shutdown_worker()
        self.playback_started = False
        self.pause_requested = False
        self.manually_paused = False
        self.play_asap_requested = False
        self.inflight_count = 0
        self.pause_button.setEnabled(False)
        self.pause_button.setText("暂停/继续")
        self.start_button.setText("启动")
        self.start_button.setEnabled(True)

    def _begin_start_countdown(self) -> None:
        if self.countdown_active or self.playback_started:
            return
        self.countdown_active = True
        self._log("已达到缓冲水位线，准备开始播放")
        self.countdown_value = 1
        self.start_button.setText("停止")
        self.status_label.setText("准备播放：1")
        self.countdown_timer.start()

    def _advance_start_countdown(self) -> None:
        if not self.countdown_active:
            return
        self.countdown_value += 1
        if self.countdown_value <= 3:
            self.start_button.setText("停止")
            self.status_label.setText(f"准备播放：{self.countdown_value}")
            return
        self.countdown_timer.stop()
        self.countdown_active = False
        self.playback_started = True
        self.start_button.setText("停止")
        self.status_label.setText("开始播放")
        self.player.start()
        self.pause_button.setEnabled(True)
        self._fill_generation_buffer()

    def toggle_pause_resume(self) -> None:
        if self.manually_paused:
            if self.playback_started:
                self.resume_playback()
            else:
                self._resume_before_playback()
        elif not self.playback_started:
            self._pause_before_playback()
        else:
            self.request_sentence_end_pause()

    def _pause_before_playback(self) -> None:
        self.countdown_timer.stop()
        self.countdown_active = False
        self.countdown_value = 0
        self.play_asap_requested = False
        self.manually_paused = True
        self.start_button.setText("停止")
        self.pause_button.setText("继续播放")
        self.status_label.setText("播放已暂停，云端继续生成并缓存")
        self._log("播放已暂停，生成和缓存继续运行")
        self._fill_generation_buffer()

    def _resume_before_playback(self) -> None:
        self.manually_paused = False
        self.play_asap_requested = True
        self.pause_button.setText("暂停/继续")
        if self.player.buffered_line_count >= 1:
            self._begin_start_countdown()
        else:
            self.status_label.setText("等待云端生成初始缓存…")
        self._fill_generation_buffer()

    def request_sentence_end_pause(self) -> None:
        if not self.player.has_current_line:
            return
        if self.pause_requested:
            self.player.cancel_pause_after_current_line()
            self.pause_requested = False
            self.pause_button.setText("暂停/继续")
            self.status_label.setText("已取消句末暂停")
            self._fill_generation_buffer()
            return
        self.player.request_pause_after_current_line()
        self.pause_requested = True
        self.pause_button.setText("取消句末暂停")
        self.status_label.setText("将在当前句完整播放后暂停")

    def resume_playback(self) -> None:
        try:
            self.manually_paused = False
            self.pause_requested = False
            self.player.resume()
            self.pause_button.setText("暂停/继续")
            self.pause_button.setEnabled(True)
            self.status_label.setText("继续播放下一句")
            self._fill_generation_buffer()
        except RuntimeError as error:
            QMessageBox.warning(self, "无法继续", str(error))

    def _on_line_started(self, line_id: str) -> None:
        interjection = self.interjection_lines.get(line_id)
        if interjection is not None:
            self._log("音频开始: 插播话术")
            self.current_line_label.setText("插播话术")
            self.status_label.setText("正在播放插播话术")
            self.current_text_view.setPlainText(interjection.text)
            self.interjection_status_label.setText("正在插播此话术")
            return
        number = self.line_positions.get(line_id, 0)
        self._log(f"音频开始: 第{number}句")
        self.current_line_label.setText(f"第{number}句")
        self.status_label.setText(f"正在播放第{number}句")
        self._track_playing_line(line_id)
        for line in self.playback_lines:
            if line.line_id == line_id:
                self.current_text_view.setPlainText(line.text)
                break

    def _track_playing_line(self, line_id: str) -> None:
        for row in range(self.table.rowCount()):
            identifier = self.table.item(row, 0)
            if (
                identifier is not None
                and str(identifier.data(Qt.ItemDataRole.UserRole)) == line_id
            ):
                self.table.setCurrentCell(row, 3)
                self.table.selectRow(row)
                text_item = self.table.item(row, 3)
                if text_item is not None:
                    self.table.scrollToItem(
                        text_item,
                        QAbstractItemView.ScrollHint.PositionAtCenter,
                    )
                return

    def _on_line_finished(self, line_id: str) -> None:
        interjection = self.interjection_lines.pop(line_id, None)
        if interjection is not None:
            self._log(
                f"✅ 插播完毕 | 队列剩余: {self.player.buffered_line_count}"
            )
            self.interjection_status_label.setText("插播完成，已继续直播话术。")
            if self.finished_count >= len(self.playback_lines):
                self._mark_playback_complete()
                return
            self._fill_generation_buffer()
            return
        self.finished_count += 1
        number = self.line_positions.get(line_id, 0)
        self._log(
            f"✅ 播放完毕 (第{number}句) | 队列剩余: {self.player.buffered_line_count}"
        )
        if self.finished_count >= len(self.playback_lines):
            if self.interjection_lines:
                self.status_label.setText("直播话术已结束，正在完成插播话术")
                return
            self._mark_playback_complete()
        else:
            self._fill_generation_buffer()

    def _mark_playback_complete(self) -> None:
        self.playback_started = False
        self.countdown_active = False
        self.manually_paused = False
        self.pause_requested = False
        self.play_asap_requested = False
        self.inflight_count = 0
        self.status_label.setText("全部话术播放完成")
        self.current_line_label.setText("已完成")
        self.pause_button.setEnabled(False)
        self.pause_button.setText("暂停/继续")
        self.start_button.setText("启动")
        self.start_button.setEnabled(True)
        self._shutdown_worker()

    def _on_sentence_end_paused(self) -> None:
        if self.interjection_auto_pause:
            self.status_label.setText("当前句已播完，等待插播音频生成")
            self.current_line_label.setText("等待插播")
            self.interjection_status_label.setText("正在生成插播音频，请稍候。")
            return
        self.manually_paused = True
        self.pause_requested = False
        self.pause_button.setText("继续播放")
        self.pause_button.setEnabled(True)
        self.status_label.setText("已在句末暂停，可使用话筒插话")
        self.current_line_label.setText("句末暂停")
        self._log("已在当前句完整结束后暂停，云端继续生成并缓存")
        self._fill_generation_buffer()

    def _on_buffer_depth_changed(self, depth: int) -> None:
        self.buffer_label.setText(f"{depth}句")
        if self.worker is not None:
            self._fill_generation_buffer()

    def _on_player_buffering(self) -> None:
        if self.finished_count < len(self.playback_lines) and not self.manually_paused:
            self.status_label.setText("等待云端生成下一句…")
            self._fill_generation_buffer()

    def _on_playback_error(self, message: str) -> None:
        self._log(f"播放错误: {message}")
        self.status_label.setText("音频播放错误")
        QMessageBox.critical(self, "播放错误", message)

    def _refresh_output_devices(self) -> None:
        self.output_devices = list(QMediaDevices.audioOutputs())
        self.output_combo.clear()
        for device in self.output_devices:
            self.output_combo.addItem(device.description())
        default_device = QMediaDevices.defaultAudioOutput()
        for index, device in enumerate(self.output_devices):
            if device.id() == default_device.id():
                self.output_combo.setCurrentIndex(index)
                break
        self._change_output_device(self.output_combo.currentIndex())

    def _change_output_device(self, index: int) -> None:
        if 0 <= index < len(self.output_devices):
            self.player.set_output_device(self.output_devices[index])

    def _shutdown_worker(self) -> None:
        self.playback_session_id += 1
        if self.worker is not None:
            worker = self.worker
            self.worker = None
            if worker not in self._retired_workers:
                self._retired_workers.append(worker)
                worker.finished.connect(
                    lambda retired=worker: self._release_retired_worker(retired)
                )
            worker.shutdown()
            worker.wait(3000)
            if not worker.isRunning():
                self._release_retired_worker(worker)
        if self.interjection_worker is not None:
            self.interjection_worker.shutdown()
            self.interjection_worker.wait(3000)
            self.interjection_worker = None
        self.interjection_lines.clear()
        self.interjection_auto_pause = False

    def _release_retired_worker(self, worker: TTSWorker) -> None:
        try:
            self._retired_workers.remove(worker)
        except ValueError:
            pass

    def _log(self, message: str) -> None:
        timestamped = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.log_view.append(timestamped)
        self.tts_log_view.append(timestamped)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_window_layout()
        self._save_interjection_presets()
        self.countdown_timer.stop()
        if self.voice_sync_worker is not None:
            self.voice_sync_worker.requestInterruption()
            self.voice_sync_worker.wait(3000)
            self.voice_sync_worker = None
        self.local_synthesis_active = False
        self._shutdown_local_worker()
        self._shutdown_worker()
        self.player.reset()
        super().closeEvent(event)


def configure_application(application: QApplication) -> None:
    icon_path = (
        Path(getattr(sys, "_MEIPASS", application_root()))
        / "assets"
        / "yunqi-ai-live-icon-v2.png"
    )
    if icon_path.is_file():
        application.setWindowIcon(QIcon(str(icon_path)))
    application.setFont(QFont("Microsoft YaHei UI", 10))
    application.setStyleSheet(
        """
        * { outline: none; }
        QWidget { background: #182027; color: #d9e1e6; }
        QMainWindow, QWidget#appRoot { background: #10161b; }
        QWidget#topToolbar {
            background: #151d23;
            border-bottom: 1px solid #34434d;
        }
        QWidget#libraryPanel { background: #182027; border: 1px solid #34434d; }
        QWidget#editorPanel { background: transparent; }
        QLabel#topStatus { color: #84aeca; font-size: 12px; font-weight: 600; }
        QLabel#modeLabel { color: #d9e1e6; font-weight: 600; }
        QLabel#scriptStats { color: #83aec9; font-weight: 600; padding: 2px 4px; }
        QLabel#sectionTitle {
            color: #e3e9ed;
            font-size: 15px;
            font-weight: 700;
            padding: 0;
        }
        QLabel#dialogTitle {
            color: #e3e9ed;
            font-size: 20px;
            font-weight: 700;
            padding: 6px 2px 10px 2px;
        }
        QDialog { background: #10161b; }
        QLabel#editorHelp {
            color: #a7b3bc;
            background: #202c34;
            border-radius: 6px;
            padding: 7px 10px;
        }
        QLabel#hintLabel { color: #8fb5cd; padding: 4px 8px; }
        QGroupBox {
            background: #182027;
            border: 1px solid #34434d;
            border-radius: 8px;
            margin-top: 12px;
            padding: 12px 10px 10px 10px;
            font-weight: 600;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 5px;
            color: #b7c2c9;
        }
        QListWidget, QTableWidget, QTextEdit, QLineEdit {
            background: #1d262d;
            color: #d9e1e6;
            border: 1px solid #34434d;
            border-radius: 6px;
            gridline-color: #303c45;
            selection-background-color: #355a70;
            selection-color: #edf2f5;
            padding: 4px;
        }
        QTableWidget#scriptTable {
            background: #202a32;
            alternate-background-color: #1d272f;
            color: #d7e0e6;
            border-color: #3a4852;
            gridline-color: #35414a;
            selection-background-color: #355a70;
            selection-color: #edf2f5;
        }
        QLineEdit#searchInput {
            background: #222d35;
            min-height: 28px;
            padding: 2px 9px;
        }
        QListWidget#projectList::item {
            min-height: 32px;
            border-radius: 4px;
            padding-left: 8px;
        }
        QListWidget#projectList::item:hover { background: #283740; }
        QListWidget#projectList::item:selected {
            background: #355a70;
            color: #edf2f5;
            border-left: 3px solid #6f9fbc;
        }
        QTableWidget::item:selected {
            background: #355a70;
            color: #edf2f5;
            border-top: 1px solid #6f9fbc;
            border-bottom: 1px solid #6f9fbc;
        }
        QTableWidget::item:hover { background: #2a3943; }
        QTableCornerButton::section {
            background: #202a31;
            border: none;
        }
        QTabWidget::pane {
            border: 1px solid #34434d;
            border-radius: 0 0 8px 8px;
            background: #171f25;
            top: -1px;
        }
        QTabBar::tab {
            background: #171f25;
            color: #95a3ac;
            border: 1px solid #34434d;
            border-bottom: none;
            padding: 9px 14px;
            min-width: 70px;
        }
        QTabBar::tab:hover { color: #d3dce2; background: #202b33; }
        QTabBar::tab:selected {
            color: #dce7ed;
            background: #24323b;
            border-top: 2px solid #6f9fbc;
        }
        QHeaderView::section {
            background: #202a31;
            color: #b8c3ca;
            border: none;
            border-right: 1px solid #34434d;
            border-bottom: 1px solid #3a4852;
            padding: 9px;
            font-weight: 600;
        }
        QPushButton {
            background: #273945;
            border: 1px solid #456174;
            border-radius: 6px;
            padding: 8px 15px;
            color: #dce4e9;
            font-weight: 600;
        }
        QPushButton:hover { background: #334b5a; border-color: #6f9fbc; }
        QPushButton:pressed { background: #1f2d36; padding-top: 9px; padding-bottom: 7px; }
        QPushButton:disabled { color: #697780; background: #1b2329; border-color: #303b43; }
        QPushButton#primaryButton {
            background: #356f91;
            border-color: #6f9fbc;
            color: #f0f4f6;
            font-size: 18px;
            font-weight: 700;
        }
        QPushButton#primaryButton:hover { background: #417f9f; }
        QPushButton#playbackToggleButton {
            background: #25333c;
            border-color: #465b68;
            color: #e2e8ec;
            font-size: 16px;
            font-weight: 700;
        }
        QPushButton#playbackToggleButton:hover { background: #304550; }
        QPushButton#saveButton {
            background: #356f91;
            border-color: #6f9fbc;
            color: #f0f4f6;
            font-weight: 700;
        }
        QPushButton#saveButton:hover { background: #417f9f; }
        QPushButton#rowInterjectionButton {
            background: #356f91;
            border-color: #6f9fbc;
            color: #f0f4f6;
            font-weight: 700;
            padding: 6px 10px;
        }
        QPushButton#rowInterjectionButton:hover { background: #417f9f; }
        QPushButton#confirmProjectButton {
            background: #2e586f;
            border-color: #5f8ba5;
            color: #edf2f5;
            font-weight: 700;
            min-height: 22px;
        }
        QPushButton#confirmProjectButton:hover {
            background: #386b86;
            border-color: #7aa5be;
        }
        QPushButton#dangerButton { color: #e39a9a; }
        QPushButton#dangerButton:hover { background: #563235; border-color: #c87878; }
        QToolButton {
            background: #222d35;
            border: 1px solid #3c4b55;
            border-radius: 5px;
            padding: 5px;
            font-size: 16px;
        }
        QToolButton:hover { background: #334b5a; color: #edf2f5; }
        QToolButton#topActionButton {
            background: #273945;
            border: 1px solid #456174;
            border-radius: 6px;
            color: #dce4e9;
            padding: 5px 10px;
            font-size: 13px;
            font-weight: 700;
        }
        QToolButton#topActionButton:hover {
            background: #356f91;
            border: 1px solid #6f9fbc;
            color: #f0f4f6;
        }
        QToolButton#topActionButton:pressed {
            background: #22333d;
            border-color: #5f8ba5;
        }
        QComboBox, QDoubleSpinBox, QSpinBox {
            background: #202a31;
            color: #d9e1e6;
            border: 1px solid #3c4b55;
            border-radius: 5px;
            min-height: 22px;
        }
        QComboBox { padding: 6px; }
        QDoubleSpinBox, QSpinBox {
            padding: 4px 30px 4px 8px;
        }
        QDoubleSpinBox::up-button, QSpinBox::up-button {
            subcontrol-origin: border;
            subcontrol-position: top right;
            width: 24px;
            height: 15px;
            background: #29363e;
            border-left: 1px solid #465761;
            border-bottom: 1px solid #465761;
            border-top-right-radius: 4px;
        }
        QDoubleSpinBox::down-button, QSpinBox::down-button {
            subcontrol-origin: border;
            subcontrol-position: bottom right;
            width: 24px;
            height: 15px;
            background: #29363e;
            border-left: 1px solid #465761;
            border-top: 1px solid #465761;
            border-bottom-right-radius: 4px;
        }
        QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
        QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {
            background: #3a505d;
        }
        QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover, QLineEdit:hover {
            border-color: #668399;
        }
        QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus,
        QTextEdit:focus, QListWidget:focus, QTableWidget:focus {
            border-color: #6f9fbc;
        }
        QMenu {
            background: #20292f;
            color: #d9e1e6;
            border: 1px solid #465761;
            padding: 5px;
        }
        QMenu::item { padding: 7px 26px 7px 10px; border-radius: 4px; }
        QMenu::item:selected { background: #355a70; color: #edf2f5; }
        QToolTip {
            color: #e2e8ec;
            background: #252f36;
            border: 1px solid #526673;
            padding: 6px;
        }
        QScrollBar:vertical {
            background: #151c21;
            width: 10px;
            margin: 0;
        }
        QScrollBar::handle:vertical {
            background: #465964;
            min-height: 28px;
            border-radius: 5px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QSplitter::handle { background: #10161b; }
        QSplitter::handle:horizontal { width: 7px; }
        QSplitter::handle:vertical { height: 7px; }
        QSplitter#verticalContentSplitter::handle:vertical {
            background: #3f515c;
            height: 8px;
        }
        QSplitter#verticalContentSplitter::handle:vertical:hover {
            background: #6f9fbc;
        }
        """
    )


def run() -> None:
    application = QApplication(sys.argv)
    configure_application(application)
    window = MainWindow()
    window.show_with_saved_layout()
    raise SystemExit(application.exec())


if __name__ == "__main__":
    run()
