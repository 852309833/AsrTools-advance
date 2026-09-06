import logging
import os
import platform
import subprocess
import sys
import webbrowser

# FIX: 修复中文路径报错 https://github.com/WEIFENG2333/AsrTools/issues/18  设置QT_QPA_PLATFORM_PLUGIN_PATH
plugin_path = os.path.join(sys.prefix, 'Lib', 'site-packages', 'PyQt5', 'Qt5', 'plugins')
os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = plugin_path
print(os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'])

from PyQt5.QtCore import Qt, QRunnable, QThreadPool, QObject, pyqtSignal as Signal, pyqtSlot as Slot, QSize, QThread, \
    pyqtSignal
from PyQt5.QtGui import QCursor, QColor, QFont, QGuiApplication, QIcon
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFileDialog,
                             QTableWidgetItem, QHeaderView, QSizePolicy, QAbstractItemView,
                             QSystemTrayIcon, QStyle)
from qfluentwidgets import (ComboBox, PushButton, LineEdit, TableWidget, FluentIcon as FIF,
                            Action, RoundMenu, InfoBar, InfoBarPosition,
                            FluentWindow, BodyLabel, MessageBox, SystemTrayMenu)

from .bk_asr.BcutASR import BcutASR
from .bk_asr.JianYingASR import JianYingASR
from .bk_asr.KuaiShouASR import KuaiShouASR
from .bk_asr.WhisperASR import WhisperASR
from .bk_asr.ASRData import ASRData, ASRDataSeg
from .media_utils import prepare_audio, build_export_path, split_audio_chunks, cleanup_paths, get_app_root

ASR_ENGINES = {
    '必剪 (B站)': 'B 接口',
    '剪映 (J)': 'J 接口',
    '快手 (K)': 'K 接口',
    'Whisper (本地)': 'Whisper',
}

DEFAULT_EXPORT_FORMAT = 'TXT'
EXPORT_FORMATS = ['TXT', 'SRT', 'ASS']
GITHUB_URL = 'https://github.com/852309833/AsrTools-advance'

STATUS_COLORS = {
    "未处理": QColor("gray"),
    "待处理": QColor("#1976D2"),
    "处理中": QColor("orange"),
    "已处理": QColor("green"),
    "错误": QColor("red"),
}

SUPPORTED_MEDIA_EXTS = (
    '.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma',
    '.mp4', '.avi', '.mov', '.ts', '.mkv', '.wmv', '.flv', '.webm', '.rmvb',
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


class WorkerSignals(QObject):
    finished = Signal(str, str, str)
    errno = Signal(str, str)


def _create_asr_engine(engine: str, audio_path: str, use_cache: bool = True):
    if engine == 'B 接口':
        return BcutASR(audio_path, use_cache=use_cache)
    if engine == 'J 接口':
        return JianYingASR(audio_path, use_cache=use_cache)
    if engine == 'K 接口':
        return KuaiShouASR(audio_path, use_cache=use_cache)
    if engine == 'Whisper':
        return WhisperASR(audio_path, use_cache=use_cache)
    raise ValueError(f"未知的 ASR 引擎: {engine}")


def _fallback_engines(preferred: str) -> list:
    order = ['B 接口', 'J 接口', 'K 接口', 'Whisper']
    if preferred not in order:
        return [preferred, 'Whisper']
    return [preferred] + [e for e in order if e != preferred]


def _run_asr_on_file(audio_path: str, preferred_engine: str, use_cache: bool = True) -> tuple[ASRData, str]:
    last_error = None
    for engine in _fallback_engines(preferred_engine):
        try:
            logging.info(f"识别引擎 {engine}: {os.path.basename(audio_path)}")
            asr = _create_asr_engine(engine, audio_path, use_cache=use_cache)
            return asr.run(), engine
        except Exception as e:
            last_error = e
            logging.error(f"引擎 {engine} 失败: {e}")
    raise RuntimeError(f"全部引擎均失败，最后错误: {last_error}")


def _merge_chunk_results(parts: list[tuple[ASRData, int]]) -> ASRData:
    merged: list[ASRDataSeg] = []
    for data, offset_ms in parts:
        for seg in data.segments:
            text = (seg.text or '').strip()
            if not text:
                continue
            merged.append(ASRDataSeg(text, seg.start_time + offset_ms, seg.end_time + offset_ms))
    return ASRData(merged)


def _normalize_path_text(text: str) -> str:
    return (text or "").strip().strip('"').strip("'")


def _open_path_in_explorer(path: str) -> None:
    path = os.path.abspath(path)
    if platform.system() == "Windows":
        if os.path.isfile(path):
            subprocess.Popen(['explorer', '/select,', path])
        else:
            os.startfile(path)
    elif platform.system() == "Darwin":
        if os.path.isfile(path):
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["open", path])
    else:
        target = path if os.path.isdir(path) else os.path.dirname(path)
        subprocess.Popen(["xdg-open", target])


class ASRWorker(QRunnable):
    """ASR处理工作线程：视频→MP3→(长音频分片)→文本"""

    def __init__(self, file_path, asr_engine, export_format, output_dir=""):
        super().__init__()
        self.file_path = file_path
        self.asr_engine = asr_engine
        self.export_format = export_format
        self.output_dir = output_dir or ""
        self.signals = WorkerSignals()
        self.audio_path = None

    @Slot()
    def run(self):
        is_temp_audio = False
        chunk_temps: list[str] = []
        try:
            use_cache = True
            logging.info(f"开始处理: {self.file_path}")
            self.audio_path, is_temp_audio = prepare_audio(self.file_path)
            logging.info(f"步骤2/2: 使用 MP3 进行识别 → {self.audio_path}")

            chunks, chunk_temps = split_audio_chunks(self.audio_path)
            parts: list[tuple[ASRData, int]] = []
            used_engine = self.asr_engine

            for index, (chunk_path, offset_ms) in enumerate(chunks, start=1):
                logging.info(
                    f"识别分片 {index}/{len(chunks)} "
                    f"(偏移 {offset_ms/1000:.0f}s): {os.path.basename(chunk_path)}"
                )
                chunk_result, used_engine = _run_asr_on_file(
                    chunk_path, self.asr_engine, use_cache=use_cache
                )
                parts.append((chunk_result, offset_ms))

            result = _merge_chunk_results(parts) if len(parts) > 1 else parts[0][0]
            if not result.segments:
                logging.warning(f"未识别到有效语音: {self.file_path}")

            save_ext = self.export_format.lower()
            if save_ext == 'srt':
                result_text = result.to_srt()
            elif save_ext == 'ass':
                result_text = result.to_ass()
            else:
                result_text = result.to_txt()

            logging.info(f"完成处理: {self.file_path} 引擎={used_engine} 分片={len(chunks)}")
            save_path = build_export_path(self.file_path, self.export_format, self.output_dir)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(result_text)
            logging.info(f"导出文件已保存: {save_path}")
            self.signals.finished.emit(self.file_path, result_text, save_path)
        except Exception as e:
            logging.error(f"处理文件 {self.file_path} 时出错: {str(e)}")
            self.signals.errno.emit(self.file_path, f"处理时出错: {str(e)}")
        finally:
            cleanup_paths(chunk_temps)
            if is_temp_audio and self.audio_path and os.path.isfile(self.audio_path):
                try:
                    os.remove(self.audio_path)
                except OSError:
                    pass


class UpdateCheckerThread(QThread):
    msg = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            from check_update import check_update, check_internet_connection
            if not check_internet_connection():
                self.msg.emit("错误", "无法连接到互联网，请检查网络连接。", "")
                return
            config = check_update(self)
            if config:
                if config['fource']:
                    self.msg.emit("更新", "检测到新版本，请下载最新版本。", config['update_download_url'])
                else:
                    self.msg.emit("可更新", "检测到新版本，请下载最新版本。", config['update_download_url'])
        except Exception:
            pass


class ReorderableTableWidget(TableWidget):
    """支持行内拖拽排序的表格；处理中的行不可拖动。"""
    rowsReordered = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        self.setDragDropOverwriteMode(False)

    def startDrag(self, supportedActions):
        row = self.currentRow()
        if row >= 0:
            status_item = self.item(row, 1)
            if status_item and status_item.text() == "处理中":
                return
        super().startDrag(supportedActions)

    def dropEvent(self, event):
        super().dropEvent(event)
        if event.isAccepted():
            self.rowsReordered.emit()


class ASRWidget(QWidget):
    """ASR处理界面"""

    def __init__(self):
        super().__init__()
        self.init_ui()
        self.max_threads = 3
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(self.max_threads)
        self.processing_queue = []
        self.workers = {}
        # file_path -> 最近一次成功导出的文本路径
        self.export_paths = {}

    def init_ui(self):
        layout = QVBoxLayout(self)

        # ASR引擎选择区域
        engine_layout = QHBoxLayout()
        engine_label = BodyLabel("选择接口:", self)
        engine_label.setFixedWidth(70)
        self.combo_box = ComboBox(self)
        self.combo_box.addItems(list(ASR_ENGINES.keys()))
        self.combo_box.setCurrentIndex(0)
        engine_layout.addWidget(engine_label)
        engine_layout.addWidget(self.combo_box)
        layout.addLayout(engine_layout)

        # 导出格式选择区域
        format_layout = QHBoxLayout()
        format_label = BodyLabel("导出格式:", self)
        format_label.setFixedWidth(70)
        self.format_combo = ComboBox(self)
        self.format_combo.addItems(EXPORT_FORMATS)
        self.format_combo.setCurrentText(DEFAULT_EXPORT_FORMAT)
        format_layout.addWidget(format_label)
        format_layout.addWidget(self.format_combo)
        layout.addLayout(format_layout)

        # 输入路径：可粘贴文件/文件夹地址，也可浏览选择
        input_layout = QHBoxLayout()
        input_label = BodyLabel("输入路径:", self)
        input_label.setFixedWidth(70)
        self.file_input = LineEdit(self)
        self.file_input.setPlaceholderText("粘贴文件/文件夹完整路径，或拖拽到下方列表；也可点右侧按钮选择")
        self.file_input.setClearButtonEnabled(True)
        self.file_button = PushButton("选择文件", self)
        self.file_button.clicked.connect(self.select_file)
        self.folder_button = PushButton("选择文件夹", self)
        self.folder_button.clicked.connect(self.select_folder)
        self.add_path_button = PushButton("添加路径", self)
        self.add_path_button.clicked.connect(self.add_path_from_input)
        self.file_input.returnPressed.connect(self.add_path_from_input)
        input_layout.addWidget(input_label)
        input_layout.addWidget(self.file_input)
        input_layout.addWidget(self.add_path_button)
        input_layout.addWidget(self.file_button)
        input_layout.addWidget(self.folder_button)
        layout.addLayout(input_layout)

        # 输出路径：选填，空则保存到每个源文件同目录
        output_layout = QHBoxLayout()
        output_label = BodyLabel("输出目录:", self)
        output_label.setFixedWidth(70)
        self.output_input = LineEdit(self)
        self.output_input.setPlaceholderText("选填。留空则输出到原文件所在目录")
        self.output_input.setClearButtonEnabled(True)
        self.output_button = PushButton("选择目录", self)
        self.output_button.clicked.connect(self.select_output_dir)
        output_layout.addWidget(output_label)
        output_layout.addWidget(self.output_input)
        output_layout.addWidget(self.output_button)
        layout.addLayout(output_layout)

        tip = BodyLabel("提示：可拖拽调整处理顺序（越靠前越先处理）；重试错误项会优先排队。", self)
        tip.setTextColor(QColor(120, 120, 120), QColor(160, 160, 160))
        layout.addWidget(tip)

        # 文件列表表格（支持拖拽排序）
        self.table = ReorderableTableWidget(self)
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(['文件名', '状态'])
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.rowsReordered.connect(self._sync_queue_with_table_order)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.table)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 100)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 底部操作区
        action_layout = QHBoxLayout()
        self.clear_selected_button = PushButton("清空选中", self)
        self.clear_selected_button.clicked.connect(self.clear_selected_rows)
        self.clear_all_button = PushButton("清空全部", self)
        self.clear_all_button.clicked.connect(self.clear_all_rows)
        self.clear_processed_button = PushButton("清空已处理", self)
        self.clear_processed_button.setToolTip("只移除状态为「已处理」的任务，不影响未处理/处理中/错误项")
        self.clear_processed_button.clicked.connect(self.clear_processed_rows)
        self.reset_button = PushButton("重置状态", self)
        self.reset_button.setToolTip("将已处理/错误的任务重置为未处理，方便重新跑")
        self.reset_button.clicked.connect(self.reset_selected_or_all_status)
        self.process_button = PushButton("开始处理", self)
        self.process_button.clicked.connect(self.process_files)
        self.process_button.setEnabled(False)
        action_layout.addWidget(self.clear_selected_button)
        action_layout.addWidget(self.clear_processed_button)
        action_layout.addWidget(self.clear_all_button)
        action_layout.addWidget(self.reset_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.process_button)
        layout.addLayout(action_layout)

        self.setAcceptDrops(True)

    def get_output_dir(self) -> str:
        return _normalize_path_text(self.output_input.text())

    def select_file(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择音频或视频文件",
            "",
            "Media Files (*.mp3 *.wav *.ogg *.flac *.aac *.m4a *.wma *.mp4 *.avi *.mov *.ts *.mkv *.wmv *.flv *.webm *.rmvb)",
        )
        for file in files:
            self.add_file_to_table(file)
        if files:
            self.file_input.setText(os.path.dirname(files[0]))
        self.update_start_button_state()

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择包含音视频的文件夹")
        if not folder:
            return
        self.file_input.setText(folder)
        self._add_path_recursive(folder)
        self.update_start_button_state()

    def select_output_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录（可选）")
        if folder:
            self.output_input.setText(folder)

    def add_path_from_input(self):
        """从输入框添加文件或文件夹路径到列表。"""
        path = _normalize_path_text(self.file_input.text())
        if not path:
            InfoBar.warning(
                title='路径为空',
                content="请先粘贴或输入文件/文件夹完整路径。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        if not os.path.exists(path):
            InfoBar.error(
                title='路径不存在',
                content=f"找不到：{path}",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        before = self.table.rowCount()
        if os.path.isdir(path):
            self._add_path_recursive(path)
        elif path.lower().endswith(SUPPORTED_MEDIA_EXTS):
            self.add_file_to_table(path)
        else:
            InfoBar.warning(
                title='不支持的格式',
                content=f"不支持该文件类型：{os.path.splitext(path)[1]}",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        added = self.table.rowCount() - before
        InfoBar.success(
            title='已添加',
            content=f"新增 {added} 个文件到列表。" if added else "没有新增文件（可能已在列表中）。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )
        self.update_start_button_state()

    def _add_path_recursive(self, folder: str):
        for root, _dirs, files_in_dir in os.walk(folder):
            for name in files_in_dir:
                if name.lower().endswith(SUPPORTED_MEDIA_EXTS):
                    self.add_file_to_table(os.path.join(root, name))

    def add_file_to_table(self, file_path):
        file_path = os.path.abspath(file_path)
        if self.find_row_by_file_path(file_path) != -1:
            return

        row_count = self.table.rowCount()
        self.table.insertRow(row_count)
        item_filename = self.create_non_editable_item(os.path.basename(file_path))
        item_status = self.create_non_editable_item("未处理")
        item_status.setForeground(STATUS_COLORS["未处理"])
        self.table.setItem(row_count, 0, item_filename)
        self.table.setItem(row_count, 1, item_status)
        item_filename.setData(Qt.UserRole, file_path)

    def create_non_editable_item(self, text):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _set_file_status(self, file_path: str, status: str):
        row = self.find_row_by_file_path(file_path)
        if row == -1:
            return
        item_status = self.create_non_editable_item(status)
        item_status.setForeground(STATUS_COLORS.get(status, QColor("gray")))
        self.table.setItem(row, 1, item_status)

    def show_context_menu(self, pos):
        """显示右键菜单：有选中行时操作选中项；空白处可清空全部。不会退出软件。"""
        menu = RoundMenu(parent=self)
        current_row = self.table.rowAt(pos.y())
        selected_rows = sorted({idx.row() for idx in self.table.selectedIndexes()})

        if current_row >= 0 and current_row not in selected_rows:
            self.table.selectRow(current_row)
            selected_rows = [current_row]

        if selected_rows:
            reprocess_action = Action(FIF.SYNC, "重新处理")
            delete_action = Action(FIF.DELETE, "删除选中")
            open_dir_action = Action(FIF.FOLDER, "打开源文件目录")
            open_output_action = Action(FIF.DOCUMENT, "打开输出文件")
            copy_path_action = Action(FIF.COPY, "复制源路径")
            reset_action = Action(FIF.CANCEL, "重置为未处理")
            menu.addActions([
                reprocess_action,
                reset_action,
                delete_action,
            ])
            menu.addSeparator()
            menu.addActions([open_dir_action, open_output_action, copy_path_action])
            menu.addSeparator()

            reprocess_action.triggered.connect(self.reprocess_selected_files)
            delete_action.triggered.connect(self.clear_selected_rows)
            open_dir_action.triggered.connect(self.open_file_directory)
            open_output_action.triggered.connect(self.open_output_file)
            copy_path_action.triggered.connect(self.copy_selected_path)
            reset_action.triggered.connect(lambda: self.reset_rows_status(selected_rows))

        clear_processed_action = Action(FIF.ACCEPT, "清空已处理")
        clear_all_action = Action(FIF.BROOM, "清空全部任务")
        if selected_rows:
            menu.addSeparator()
            menu.addAction(clear_processed_action)
        else:
            menu.addAction(clear_processed_action)
        menu.addAction(clear_all_action)
        clear_processed_action.triggered.connect(self.clear_processed_rows)
        clear_all_action.triggered.connect(self.clear_all_rows)

        # 显式指定 parent，避免菜单销毁时连带异常关闭窗口
        menu.exec(self.table.viewport().mapToGlobal(pos), ani=True)

    def clear_selected_rows(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            InfoBar.info(
                title='未选中',
                content="请先选中要清空的文件。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )
            return
        for row in rows:
            self._remove_row(row)
        self.update_start_button_state()
        InfoBar.success(
            title='已清空选中',
            content=f"已移除 {len(rows)} 个任务。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=1800,
            parent=self,
        )

    def _rows_by_status(self, status: str) -> list[int]:
        rows = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item and item.text() == status:
                rows.append(row)
        return rows

    def clear_processed_rows(self):
        rows = sorted(self._rows_by_status("已处理"), reverse=True)
        if not rows:
            InfoBar.info(
                title='没有已处理任务',
                content="列表中没有状态为「已处理」的项目。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )
            return
        for row in rows:
            self._remove_row(row)
        self.update_start_button_state()
        InfoBar.success(
            title='已清空已处理',
            content=f"已移除 {len(rows)} 个已处理任务。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=1800,
            parent=self,
        )

    def clear_all_rows(self):
        if self.table.rowCount() == 0:
            return
        w = MessageBox("清空全部", "确定清空列表中的全部任务吗？不会删除磁盘上的音视频文件。", self)
        if not w.exec():
            return
        # 从后往前删，避免索引错乱
        for row in range(self.table.rowCount() - 1, -1, -1):
            self._remove_row(row)
        self.processing_queue.clear()
        self.update_start_button_state()
        InfoBar.success(
            title='已清空全部',
            content="任务列表已清空，可继续添加文件。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )

    def _remove_row(self, row: int):
        item = self.table.item(row, 0)
        if not item:
            return
        file_path = item.data(Qt.UserRole)
        if file_path in self.workers:
            worker = self.workers.pop(file_path, None)
            if worker:
                try:
                    worker.signals.finished.disconnect(self.update_table)
                    worker.signals.errno.disconnect(self.handle_error)
                except TypeError:
                    pass
        if file_path in self.processing_queue:
            self.processing_queue = [p for p in self.processing_queue if p != file_path]
        self.export_paths.pop(file_path, None)
        self.table.removeRow(row)

    def reset_selected_or_all_status(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not rows:
            rows = list(range(self.table.rowCount()))
        self.reset_rows_status(rows)

    def reset_rows_status(self, rows):
        changed = 0
        for row in rows:
            status_item = self.table.item(row, 1)
            if not status_item:
                continue
            status = status_item.text()
            if status in ("已处理", "错误", "待处理"):
                file_path = self.table.item(row, 0).data(Qt.UserRole)
                if status == "待处理" and file_path in self.processing_queue:
                    self.processing_queue = [p for p in self.processing_queue if p != file_path]
                item_status = self.create_non_editable_item("未处理")
                item_status.setForeground(STATUS_COLORS["未处理"])
                self.table.setItem(row, 1, item_status)
                changed += 1
        self.update_start_button_state()
        if changed:
            InfoBar.success(
                title='已重置',
                content=f"已将 {changed} 个任务重置为未处理。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=1800,
                parent=self,
            )

    def open_file_directory(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            return
        current_item = self.table.item(current_row, 0)
        if not current_item:
            return
        file_path = current_item.data(Qt.UserRole)
        try:
            _open_path_in_explorer(file_path)
        except Exception as e:
            InfoBar.error(
                title='无法打开目录',
                content=str(e),
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def open_output_file(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            return
        file_path = self.table.item(current_row, 0).data(Qt.UserRole)
        save_path = self.export_paths.get(file_path)
        if not save_path or not os.path.exists(save_path):
            # 按当前输出设置推断
            save_path = build_export_path(file_path, self.format_combo.currentText(), self.get_output_dir())
        if not os.path.exists(save_path):
            InfoBar.warning(
                title='尚未生成输出',
                content="该任务还没有可打开的输出文件。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return
        try:
            _open_path_in_explorer(save_path)
        except Exception as e:
            InfoBar.error(
                title='无法打开输出文件',
                content=str(e),
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def copy_selected_path(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            return
        file_path = self.table.item(current_row, 0).data(Qt.UserRole)
        QGuiApplication.clipboard().setText(file_path or "")
        InfoBar.success(
            title='已复制',
            content=file_path,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=1800,
            parent=self,
        )

    def _sync_queue_with_table_order(self):
        """按表格从上到下重排待处理队列（不影响正在处理中的任务）。"""
        queued = set(self.processing_queue)
        ordered = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item:
                continue
            file_path = item.data(Qt.UserRole)
            if file_path in queued and file_path not in self.workers:
                ordered.append(file_path)
        self.processing_queue = ordered

    def _enqueue_paths(self, file_paths: list[str], *, priority: bool = False):
        """将文件加入处理队列。priority=True 时插到队首（重试错误/已处理优先）。"""
        added = False
        paths = [p for p in file_paths if p and p not in self.workers]
        if not paths:
            return

        if priority:
            # 倒序插入队首，保证多选时表格靠前的项优先；不触发全表同步以免被排到后面
            for file_path in reversed(paths):
                if file_path in self.processing_queue:
                    self.processing_queue.remove(file_path)
                self.processing_queue.insert(0, file_path)
                self._set_file_status(file_path, "待处理")
                added = True
        else:
            for file_path in paths:
                if file_path not in self.processing_queue:
                    self.processing_queue.append(file_path)
                    self._set_file_status(file_path, "待处理")
                    added = True
            self._sync_queue_with_table_order()

        if added:
            self.process_next_in_queue()

    def add_to_queue(self, file_path, priority=False):
        self._enqueue_paths([file_path], priority=priority)

    def reprocess_selected_files(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]

        priority_paths = []
        for row in rows:
            file_path = self.table.item(row, 0).data(Qt.UserRole)
            status = self.table.item(row, 1).text()
            if status == "处理中":
                InfoBar.warning(
                    title='正在处理中',
                    content=f"{os.path.basename(file_path)} 正在处理，请稍后再试。",
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=2500,
                    parent=self,
                )
                continue
            priority_paths.append(file_path)

        if priority_paths:
            self._enqueue_paths(priority_paths, priority=True)

    def process_files(self):
        to_queue = []
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 1)
            if not status_item or status_item.text() != "未处理":
                continue
            file_path = self.table.item(row, 0).data(Qt.UserRole)
            if file_path not in self.workers:
                to_queue.append(file_path)

        if to_queue:
            self._enqueue_paths(to_queue, priority=False)
        elif not self.processing_queue and not self.workers:
            InfoBar.info(
                title='没有可处理任务',
                content="列表中没有「未处理」的文件。可右键重置状态后重试。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        self._sync_queue_with_table_order()
        self.process_next_in_queue()

    def process_next_in_queue(self):
        while self.thread_pool.activeThreadCount() < self.max_threads and self.processing_queue:
            file_path = self.processing_queue.pop(0)
            if file_path not in self.workers:
                self.process_file(file_path)

    def process_file(self, file_path):
        selected_engine = ASR_ENGINES.get(self.combo_box.currentText(), self.combo_box.currentText())
        selected_format = self.format_combo.currentText()
        worker = ASRWorker(file_path, selected_engine, selected_format, self.get_output_dir())
        worker.signals.finished.connect(self.update_table)
        worker.signals.errno.connect(self.handle_error)
        self.thread_pool.start(worker)
        self.workers[file_path] = worker

        row = self.find_row_by_file_path(file_path)
        if row != -1:
            self._set_file_status(file_path, "处理中")
            self.update_start_button_state()

    def update_table(self, file_path, result, save_path):
        row = self.find_row_by_file_path(file_path)
        if row != -1:
            self._set_file_status(file_path, "已处理")
            self.export_paths[file_path] = save_path

            InfoBar.success(
                title='处理完成',
                content=f"已保存:\n{save_path}",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=1800,
                parent=self,
            )

        self.workers.pop(file_path, None)
        self.process_next_in_queue()
        self.update_start_button_state()

    def handle_error(self, file_path, error_message):
        row = self.find_row_by_file_path(file_path)
        if row != -1:
            self._set_file_status(file_path, "错误")

            InfoBar.error(
                title='处理出错',
                content=error_message,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3500,
                parent=self,
            )

        self.workers.pop(file_path, None)
        self.process_next_in_queue()
        self.update_start_button_state()

    def find_row_by_file_path(self, file_path):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.UserRole) == file_path:
                return row
        return -1

    def update_start_button_state(self):
        has_unprocessed = any(
            self.table.item(row, 1) and self.table.item(row, 1).text() == "未处理"
            for row in range(self.table.rowCount())
        )
        self.process_button.setEnabled(has_unprocessed)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for file in files:
            if os.path.isdir(file):
                self._add_path_recursive(file)
            elif file.lower().endswith(SUPPORTED_MEDIA_EXTS):
                self.add_file_to_table(file)
        self.update_start_button_state()


class InfoWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        REPO_DESCRIPTION = """
    🚀 无需复杂配置：无需 GPU 和繁琐的本地配置，小白也能轻松使用。
    🖥️ 高颜值界面：基于 PyQt5 和 qfluentwidgets，界面美观且用户友好。
    ⚡ 效率超人：多线程并发 + 批量处理，文字转换快如闪电。
    📄 多格式支持：默认导出 .txt 纯文本，也支持 .srt 和 .ass 字幕文件。
    📁 路径更灵活：可粘贴输入路径；输出目录选填，留空则保存在源文件目录。
    🧹 列表好管理：未处理 / 待处理 / 处理中 / 已处理 状态清晰可辨。
    📌 后台运行：点关闭缩到系统托盘（右下角），任务继续执行。
        """

        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignTop)

        title_label = BodyLabel("  AsrTools-advance", self)
        title_label.setFont(QFont("Segoe UI", 30, QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title_label)

        desc_label = BodyLabel(REPO_DESCRIPTION, self)
        desc_label.setFont(QFont("Segoe UI", 12))
        main_layout.addWidget(desc_label)

        github_button = PushButton("GitHub 仓库", self)
        github_button.setIcon(FIF.GITHUB)
        github_button.setIconSize(QSize(20, 20))
        github_button.setMinimumHeight(42)
        github_button.clicked.connect(lambda _: webbrowser.open(GITHUB_URL))
        main_layout.addWidget(github_button)


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('ASR Processing Tool')
        self._force_quit = False
        self.tray_icon = None

        app_icon = self._app_icon()
        self.setWindowIcon(app_icon)
        QApplication.instance().setWindowIcon(app_icon)

        self.asr_widget = ASRWidget()
        self.asr_widget.setObjectName("main")
        self.addSubInterface(self.asr_widget, FIF.ALBUM, 'ASR Processing')

        self.info_widget = InfoWidget()
        self.info_widget.setObjectName("info")
        self.addSubInterface(self.info_widget, FIF.GITHUB, 'About')

        self.navigationInterface.setExpandWidth(200)
        self.resize(900, 680)

        self._init_system_tray()
        self._hook_titlebar_close()

        self.update_checker = UpdateCheckerThread(self)
        self.update_checker.msg.connect(self.show_msg)
        self.update_checker.start()

    def _hook_titlebar_close(self):
        """拦截标题栏关闭按钮，避免直接退出程序。"""
        close_btn = getattr(self.titleBar, 'closeBtn', None)
        if not close_btn:
            return
        try:
            close_btn.clicked.disconnect()
        except TypeError:
            pass
        close_btn.clicked.connect(self.minimize_to_tray)

    def minimize_to_tray(self):
        self.hide()
        if not self.tray_icon:
            return
        if not self.tray_icon.isVisible():
            self.tray_icon.show()
        self.tray_icon.showMessage(
            "ASR Processing Tool",
            "程序已缩到系统托盘，任务继续在后台处理。",
            QSystemTrayIcon.Information,
            3000,
        )

    def _app_icon(self) -> QIcon:
        for name in ('app_icon.ico', 'app_icon.png'):
            path = get_app_root() / name
            if path.is_file():
                icon = QIcon(str(path))
                if not icon.isNull():
                    return icon
        if getattr(sys, 'frozen', False):
            icon = QIcon(sys.executable)
            if not icon.isNull():
                return icon
        icon = self.windowIcon()
        if not icon.isNull():
            return icon
        return self.style().standardIcon(QStyle.SP_ComputerIcon)

    def _init_system_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logging.warning("当前系统不支持系统托盘")
            return

        self.tray_icon = QSystemTrayIcon(self._app_icon(), self)
        self.tray_icon.setToolTip("ASR Processing Tool - 后台运行中")

        tray_menu = SystemTrayMenu(parent=self)
        show_action = Action(FIF.VIEW, "显示主窗口")
        quit_action = Action(FIF.POWER_BUTTON, "退出程序")
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        show_action.triggered.connect(self.show_from_tray)
        quit_action.triggered.connect(self.quit_application)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_application(self):
        self._force_quit = True
        if self.tray_icon:
            self.tray_icon.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):
        if self._force_quit:
            event.accept()
            return

        event.ignore()
        self.minimize_to_tray()

    def show_msg(self, title, content, update_download_url):
        w = MessageBox(title, content, self)
        if w.exec() and update_download_url:
            webbrowser.open(update_download_url)
        if title == "更新":
            self._force_quit = True
            sys.exit(0)


def start():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    # 保持托盘引用，避免窗口隐藏后被回收
    app._main_window = window
    if window.tray_icon:
        app._tray_icon = window.tray_icon
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    start()
