# SATGgui.py -- BlueSky GUI plugin for SATG command front-end (no GUI echo log)
# Place in: bluesky/plugins/
#
# PyQt6; lazy window creation to avoid QApplication race.

from typing import Dict, List, Optional, Tuple
import os
import json
from datetime import datetime

from PyQt6.QtCore import Qt, QTime
from PyQt6.QtWidgets import (
    QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QCheckBox, QComboBox, QPushButton, QSpinBox,
    QDoubleSpinBox, QFileDialog, QSlider, QListWidget, QListWidgetItem, QTextEdit,
    QDialog, QDialogButtonBox, QTimeEdit, QScrollArea, QRadioButton, QButtonGroup,
    QInputDialog, QMessageBox
)
from PyQt6 import sip
from bluesky import stack
from bluesky.ui.qtgl.console import process_cmdline
try:
    import bluesky as bs
except Exception:
    bs = None
import os, re

# --- helpers ---------------------------------------------------------------

def _emit(cmd: str):
    """Send a BlueSky console command (no GUI echo here)."""
    stack.stack(cmd)

def _qpath(path: str) -> str:
    if not path:
        return path
    return f"\"{path}\"" if (" " in path and not (path.startswith('"') and path.endswith('"'))) else path

def _kv(key: str, val):
    if val is None:
        return ""
    if isinstance(val, str) and val.strip() == "":
        return ""
    return f"{key}={val}"

def _join_tokens(*tokens):
    return " ".join([t for t in tokens if t])

_SID_FILE_RE = re.compile(r'^SID-([0-9]{2,3}[LRC]?)-([-A-Za-z0-9_]+)\.scn$', re.IGNORECASE)
DEFAULT_STAR_RATE = 20

class SIDSchedDialog(QDialog):
    SLOT_MINUTES = 15

    def __init__(self, runways: List[str], existing: Dict[str, Dict[str, object]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("SID Runway Schedule")
        self.runways = runways
        self.data = {rw: dict(existing.get(rw, {})) for rw in runways}
        for rw in self.runways:
            if rw not in self.data or not self.data[rw].get("caps"):
                self.data[rw] = {"start": 0.0, "end": 60.0, "caps": [0]}
        self.current_runway = self.runways[0]
        self.sliders: List[QSlider] = []

        layout = QVBoxLayout(self)

        self.runway_combo = QComboBox(self)
        for rw in self.runways:
            self.runway_combo.addItem(f"RW{rw}", rw)
        self.runway_combo.currentIndexChanged.connect(self._on_runway_changed)
        layout.addWidget(self.runway_combo)

        time_row = QHBoxLayout()
        self.start_edit = QTimeEdit(self)
        self.start_edit.setDisplayFormat("HH:mm")
        self.end_edit = QTimeEdit(self)
        self.end_edit.setDisplayFormat("HH:mm")
        time_row.addWidget(QLabel("Start:", self))
        time_row.addWidget(self.start_edit)
        time_row.addWidget(QLabel("End:", self))
        time_row.addWidget(self.end_edit)
        layout.addLayout(time_row)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.slot_widget = QWidget(self.scroll_area)
        self.slot_layout = QHBoxLayout(self.slot_widget)
        self.slot_layout.setContentsMargins(6, 6, 6, 6)
        self.scroll_area.setWidget(self.slot_widget)
        layout.addWidget(self.scroll_area, 1)

        self.clear_btn = QPushButton("Clear schedule for this runway", self)
        self.clear_btn.clicked.connect(self._clear_current_schedule)
        layout.addWidget(self.clear_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.start_edit.timeChanged.connect(self._on_time_changed)
        self.end_edit.timeChanged.connect(self._on_time_changed)

        self._load_runway(self.current_runway)

    def _round_time(self, time: QTime) -> QTime:
        minutes = time.hour() * 60 + time.minute()
        rounded = (minutes // self.SLOT_MINUTES) * self.SLOT_MINUTES
        rounded = max(0, min(rounded, 24 * 60 - self.SLOT_MINUTES))
        return QTime(rounded // 60, rounded % 60)

    def _time_to_minutes(self, time: QTime) -> float:
        return time.hour() * 60 + time.minute()

    def _minutes_to_time(self, minutes: float) -> QTime:
        minutes = max(0, min(int(minutes), 24 * 60))
        return QTime(minutes // 60, minutes % 60)

    def _on_runway_changed(self):
        self._save_current()
        data = self.runway_combo.currentData()
        if data:
            self.current_runway = data
            self._load_runway(self.current_runway)

    def _on_time_changed(self):
        start = self._round_time(self.start_edit.time())
        end = self._round_time(self.end_edit.time())
        if end <= start:
            end = start.addSecs(self.SLOT_MINUTES * 60)
        self.start_edit.blockSignals(True)
        self.end_edit.blockSignals(True)
        self.start_edit.setTime(start)
        self.end_edit.setTime(end)
        self.start_edit.blockSignals(False)
        self.end_edit.blockSignals(False)
        self._update_slot_controls()


class StarSchedDialog(QDialog):
    SLOT_MINUTES = 15

    def __init__(self, procs: List[tuple[str, str]], existing: Dict[str, Dict[str, object]], parent=None):
        """procs: list of (path, label)."""
        super().__init__(parent)
        self.setWindowTitle("STAR Procedure Schedule")
        self.procs = [p for p, _ in procs]
        self.labels = {p: lbl for p, lbl in procs}
        self.data = {p: dict(existing.get(p, {})) for p in self.procs}
        for p in self.procs:
            if p not in self.data or not self.data[p].get("caps"):
                self.data[p] = {"start": 0.0, "end": 60.0, "caps": [0]}
        self.current_proc = self.procs[0]
        self.sliders: List[QSlider] = []

        layout = QVBoxLayout(self)

        self.proc_combo = QComboBox(self)
        for path in self.procs:
            self.proc_combo.addItem(self.labels.get(path, os.path.basename(path)), path)
        self.proc_combo.currentIndexChanged.connect(self._on_proc_changed)
        layout.addWidget(self.proc_combo)

        time_row = QHBoxLayout()
        self.start_edit = QTimeEdit(self)
        self.start_edit.setDisplayFormat("HH:mm")
        self.end_edit = QTimeEdit(self)
        self.end_edit.setDisplayFormat("HH:mm")
        time_row.addWidget(QLabel("Start:", self))
        time_row.addWidget(self.start_edit)
        time_row.addWidget(QLabel("End:", self))
        time_row.addWidget(self.end_edit)
        layout.addLayout(time_row)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.slot_widget = QWidget(self.scroll_area)
        self.slot_layout = QHBoxLayout(self.slot_widget)
        self.slot_layout.setContentsMargins(6, 6, 6, 6)
        self.scroll_area.setWidget(self.slot_widget)
        layout.addWidget(self.scroll_area, 1)

        self.clear_btn = QPushButton("Clear schedule for this STAR", self)
        self.clear_btn.clicked.connect(self._clear_current_schedule)
        layout.addWidget(self.clear_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.start_edit.timeChanged.connect(self._on_time_changed)
        self.end_edit.timeChanged.connect(self._on_time_changed)

        self._load_proc(self.current_proc)

    def _round_time(self, time: QTime) -> QTime:
        minutes = time.hour() * 60 + time.minute()
        rounded = (minutes // self.SLOT_MINUTES) * self.SLOT_MINUTES
        rounded = max(0, min(rounded, 24 * 60 - self.SLOT_MINUTES))
        return QTime(rounded // 60, rounded % 60)

    def _time_to_minutes(self, time: QTime) -> float:
        return time.hour() * 60 + time.minute()

    def _minutes_to_time(self, minutes: float) -> QTime:
        minutes = max(0, min(int(minutes), 24 * 60))
        return QTime(minutes // 60, minutes % 60)

    def _on_proc_changed(self):
        self._save_current()
        data = self.proc_combo.currentData()
        if data:
            self.current_proc = data
            self._load_proc(self.current_proc)

    def _on_time_changed(self):
        start = self._round_time(self.start_edit.time())
        end = self._round_time(self.end_edit.time())
        if end <= start:
            end = start.addSecs(self.SLOT_MINUTES * 60)
        self.start_edit.blockSignals(True)
        self.end_edit.blockSignals(True)
        self.start_edit.setTime(start)
        self.end_edit.setTime(end)
        self.start_edit.blockSignals(False)
        self.end_edit.blockSignals(False)
        self._update_slot_controls()

    def _clear_current_schedule(self):
        self.data[self.current_proc] = {"start": 0.0, "end": 60.0, "caps": [0]}
        self._load_proc(self.current_proc)

    def _save_current(self):
        caps = [slider.value() for slider in self.sliders]
        self.data[self.current_proc] = {
            "start": self._time_to_minutes(self.start_edit.time()),
            "end": self._time_to_minutes(self.end_edit.time()),
            "caps": caps,
            "slot": float(self.SLOT_MINUTES),
        }

    def _load_proc(self, proc_path: str):
        info = self.data.get(proc_path, {"start": 0.0, "end": 60.0, "caps": [0]})
        start_time = self._minutes_to_time(info.get("start", 0.0))
        end_time = self._minutes_to_time(info.get("end", max(info.get("start", 0.0) + self.SLOT_MINUTES, self.SLOT_MINUTES)))
        if end_time <= start_time:
            end_time = start_time.addSecs(self.SLOT_MINUTES * 60)

        self.start_edit.blockSignals(True)
        self.end_edit.blockSignals(True)
        self.start_edit.setTime(start_time)
        self.end_edit.setTime(end_time)
        self.start_edit.blockSignals(False)
        self.end_edit.blockSignals(False)

        self._build_slot_controls(info.get("caps", [0]))

    def _build_slot_controls(self, caps: List[int]):
        while self.sliders:
            slider = self.sliders.pop()
            slider.deleteLater()
        for i in reversed(range(self.slot_layout.count())):
            widget = self.slot_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()

        for idx, cap in enumerate(caps):
            container = QWidget(self.slot_widget)
            vlay = QVBoxLayout(container)
            vlay.setContentsMargins(4, 4, 4, 4)
            slider = QSlider(Qt.Orientation.Vertical, container)
            slider.setRange(0, 20)
            slider.setValue(int(cap))
            slider.valueChanged.connect(self._on_slider_changed)
            lbl = QLabel(str(int(cap)), container)
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            slider.label = lbl  # type: ignore
            vlay.addWidget(slider, 1)
            vlay.addWidget(lbl)
            self.slot_layout.addWidget(container)
            self.sliders.append(slider)

        self._update_slot_controls()

    def _update_slot_controls(self):
        start_minutes = self._time_to_minutes(self.start_edit.time())
        end_minutes = self._time_to_minutes(self.end_edit.time())
        duration = max(end_minutes - start_minutes, self.SLOT_MINUTES)
        slots = int(round(duration / self.SLOT_MINUTES))
        current = len(self.sliders)
        if slots != current:
            caps = [self.sliders[i].value() if i < current else 0 for i in range(slots)]
            self._build_slot_controls(caps)
        for slider in self.sliders:
            slider.label.setText(str(slider.value()))  # type: ignore

    def _on_slider_changed(self, value: int):
        slider = self.sender()
        if isinstance(slider, QSlider):
            slider.label.setText(str(value))  # type: ignore

    @property
    def result_data(self) -> Dict[str, Dict[str, object]]:
        self._save_current()
        return self.data

    def _clear_current_schedule(self):
        self.start_edit.setTime(QTime(0, 0))
        self.end_edit.setTime(QTime(1, 0))
        self._update_slot_controls(default=True)
        self.data[self.current_runway] = {"start": 0.0, "end": 60.0, "caps": [0]}

    def _load_runway(self, runway: str):
        info = self.data.get(runway, {"start": 0.0, "end": 60.0, "caps": [0]})
        start_time = self._minutes_to_time(info.get("start", 0.0))
        end_time = self._minutes_to_time(info.get("end", start_time.minute() + self.SLOT_MINUTES))
        if end_time <= start_time:
            end_time = start_time.addSecs(self.SLOT_MINUTES * 60)
        self.start_edit.blockSignals(True)
        self.end_edit.blockSignals(True)
        self.start_edit.setTime(start_time)
        self.end_edit.setTime(end_time)
        self.start_edit.blockSignals(False)
        self.end_edit.blockSignals(False)
        self._update_slot_controls(existing_caps=info.get("caps", [0]))

    def _save_current(self):
        caps = [slider.value() for slider in self.sliders]
        start = self._time_to_minutes(self.start_edit.time())
        end = self._time_to_minutes(self.end_edit.time())
        if end <= start:
            end = start + self.SLOT_MINUTES
        if any(caps):
            self.data[self.current_runway] = {"start": start, "end": end, "caps": caps}
        else:
            # no capacity -> treat as cleared
            self.data[self.current_runway] = {"start": start, "end": end, "caps": []}

    def _update_slot_controls(self, existing_caps: Optional[List[int]] = None, default: bool = False):
        # clear existing
        while self.slot_layout.count():
            item = self.slot_layout.takeAt(0)
            if item:
                w = item.layout()
                if w:
                    while w.count():
                        child = w.takeAt(0)
                        widget = child.widget()
                        if widget:
                            widget.deleteLater()
                else:
                    widget = item.widget()
                    if widget:
                        widget.deleteLater()
        self.sliders = []

        start = self._time_to_minutes(self.start_edit.time())
        end = self._time_to_minutes(self.end_edit.time())
        if end <= start:
            end = start + self.SLOT_MINUTES
        slot_count = max(1, int(round((end - start) / self.SLOT_MINUTES)))

        if existing_caps is None or default:
            existing_caps = [0] * slot_count
        else:
            if len(existing_caps) < slot_count:
                existing_caps = list(existing_caps) + [0] * (slot_count - len(existing_caps))
            elif len(existing_caps) > slot_count:
                existing_caps = list(existing_caps[:slot_count])

        for idx in range(slot_count):
            slot_start = start + idx * self.SLOT_MINUTES
            slot_end = slot_start + self.SLOT_MINUTES
            time_label = QLabel(f"{int(slot_start//60):02d}:{int(slot_start%60):02d}\n-\n{int(slot_end//60):02d}:{int(slot_end%60):02d}", self.slot_widget)
            slider = QSlider(Qt.Orientation.Vertical, self.slot_widget)
            slider.setRange(0, 120)
            slider.setValue(int(existing_caps[idx]))
            spin = QSpinBox(self.slot_widget)
            spin.setRange(0, 120)
            spin.setValue(int(existing_caps[idx]))

            def make_slider_cb(target):
                def _cb(val):
                    target.blockSignals(True)
                    target.setValue(val)
                    target.blockSignals(False)
                return _cb

            slider.valueChanged.connect(make_slider_cb(spin))
            spin.valueChanged.connect(make_slider_cb(slider))

            col = QVBoxLayout()
            col.addWidget(time_label, alignment=Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(slider, alignment=Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(spin, alignment=Qt.AlignmentFlag.AlignHCenter)
            self.slot_layout.addLayout(col)
            self.sliders.append(slider)

    def accept(self):
        self._save_current()
        super().accept()

    def reject(self):
        super().reject()

    @property
    def result_data(self) -> Dict[str, Dict[str, object]]:
        return {rw: dict(cfg) for rw, cfg in self.data.items()}


class DestDialog(QDialog):
    def __init__(self, procedures: List[tuple], existing: Dict[str, List[str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Procedure Destinations")
        self._procedures = procedures  # list of (path, label)
        self._edits = {}

        layout = QVBoxLayout(self)

        note = QLabel("Enter comma separated ICAO codes for each procedure. Existing airports are shown below; add or remove entries as needed.")
        note.setWordWrap(True)
        layout.addWidget(note)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        wrap = QWidget(scroll)
        form = QFormLayout(wrap)
        for path, label in procedures:
            container = QWidget(wrap)
            row_layout = QHBoxLayout(container)
            row_layout.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(container)
            edit.setClearButtonEnabled(True)
            existing_list = existing.get(path, [])
            if existing_list:
                edit.setText(", ".join(existing_list))
            apply_btn = QPushButton("Apply", container)
            apply_btn.setAutoDefault(False)
            apply_btn.clicked.connect(lambda _, key=path: self._apply_single(key))
            apply_all_btn = QPushButton("Apply to all", container)
            apply_all_btn.setAutoDefault(False)
            apply_all_btn.clicked.connect(lambda _, key=path: self._apply_all(key))
            row_layout.addWidget(edit, 1)
            row_layout.addWidget(apply_btn)
            row_layout.addWidget(apply_all_btn)
            form.addRow(QLabel(label, wrap), container)
            self._edits[path] = edit
        scroll.setWidget(wrap)
        layout.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _parse_codes(self, text: str) -> List[str]:
        return [c.strip().upper() for c in re.split(r'[;,\\s]+', text) if c.strip()]

    def _apply_single(self, key: str):
        edit = self._edits.get(key)
        if not edit:
            return
        codes = self._parse_codes(edit.text())
        edit.setText(", ".join(codes))

    def _apply_all(self, key: str):
        edit = self._edits.get(key)
        if not edit:
            return
        codes = self._parse_codes(edit.text())
        text = ", ".join(codes)
        for other in self._edits.values():
            other.setText(text)

    @property
    def result_data(self) -> Dict[str, List[str]]:
        data: Dict[str, List[str]] = {}
        for path, edit in self._edits.items():
            codes = self._parse_codes(edit.text())
            if codes:
                data[path] = codes
        return data
# --- top strip -------------------------------------------------------------

class TopStrip(QWidget):
    """Top strip with base dir controls and a single RESET button."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_window = parent  # Store reference to main window for config access
        lay = QHBoxLayout(self)

        btn_browse = QPushButton("Browse Base Folder", self)
        btn_show = QPushButton("Show Paths", self)
        btn_help = QPushButton("SATG_HELP", self)
        btn_save_config = QPushButton("Save Config", self)
        btn_load_config = QPushButton("Load Config", self)
        btn_edit_configs = QPushButton("Edit Configs", self)
        btn_reset = QPushButton("Reset", self)
        btn_reset.setToolTip("Full BlueSky reset")
        btn_save_config.setToolTip("Save current tab configuration")
        btn_load_config.setToolTip("Load saved tab configuration")
        btn_edit_configs.setToolTip("Manage saved configurations")

        lay.addWidget(btn_browse)
        lay.addWidget(btn_show)
        lay.addWidget(btn_help)
        lay.addStretch(1)
        lay.addWidget(btn_save_config)
        lay.addWidget(btn_load_config)
        lay.addWidget(btn_edit_configs)
        lay.addWidget(btn_reset)

        btn_browse.clicked.connect(self._choose_base)
        btn_show.clicked.connect(lambda: _emit("SATG_DIR"))
        btn_help.clicked.connect(lambda: _emit("SATG_HELP"))
        btn_save_config.clicked.connect(self._save_config)
        btn_load_config.clicked.connect(self._load_config)
        btn_edit_configs.clicked.connect(self._edit_configs)
        btn_reset.clicked.connect(lambda: _emit("RESET"))

    def _choose_base(self):
        path = QFileDialog.getExistingDirectory(self, "Choose SATG base directory")
        if path:
            _emit(_join_tokens("SATG_DIR", _qpath(path)))
    
    def _save_config(self):
        """Save current tab configuration to a file."""
        if not self.main_window:
            QMessageBox.warning(self, "Error", "Cannot access main window")
            return
            
        # Get current tab
        current_tab = self.main_window.tabs.currentWidget()
        tab_name = self.main_window.tabs.tabText(self.main_window.tabs.currentIndex())
        
        # Get save name from user
        name, ok = QInputDialog.getText(self, "Save Configuration", 
                                       f"Enter name for {tab_name} configuration:")
        if not ok or not name.strip():
            return
            
        name = name.strip()
        
        # Create configSaves directory if it doesn't exist
        config_dir = os.path.join("satg_data", "configSaves")
        os.makedirs(config_dir, exist_ok=True)
        
        # Extract configuration from current tab
        config_data = self._extract_tab_config(current_tab, tab_name)
        if not config_data:
            QMessageBox.warning(self, "Error", f"Cannot save configuration for {tab_name} tab")
            return
            
        # Save to file
        filename = f"{name}_{tab_name.replace(' ', '_').lower()}.json"
        filepath = os.path.join(config_dir, filename)
        
        try:
            config_data['saved_at'] = datetime.now().isoformat()
            config_data['tab_type'] = tab_name
            
            with open(filepath, 'w') as f:
                json.dump(config_data, f, indent=2)
                
            QMessageBox.information(self, "Success", f"Configuration saved as:\n{filename}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save configuration:\n{str(e)}")
    
    def _load_config(self):
        """Load a saved configuration into the current tab."""
        if not self.main_window:
            QMessageBox.warning(self, "Error", "Cannot access main window")
            return
            
        # Get current tab
        current_tab = self.main_window.tabs.currentWidget()
        tab_name = self.main_window.tabs.tabText(self.main_window.tabs.currentIndex())
        
        # Check if configSaves directory exists
        config_dir = os.path.join("satg_data", "configSaves")
        if not os.path.exists(config_dir):
            QMessageBox.information(self, "No Configurations", "No saved configurations found.\nSave a configuration first.")
            return
            
        # Find config files for this tab type
        tab_suffix = f"_{tab_name.replace(' ', '_').lower()}.json"
        config_files = [f for f in os.listdir(config_dir) if f.endswith(tab_suffix)]
        
        if not config_files:
            QMessageBox.information(self, "No Configurations", f"No saved configurations found for {tab_name} tab.")
            return
            
        # Let user choose which config to load
        config_names = [f.replace(tab_suffix, '') for f in config_files]
        name, ok = QInputDialog.getItem(self, "Load Configuration", 
                                       f"Select {tab_name} configuration to load:", 
                                       config_names, 0, False)
        if not ok:
            return
            
        # Load the selected configuration
        filename = f"{name}{tab_suffix}"
        filepath = os.path.join(config_dir, filename)
        
        try:
            with open(filepath, 'r') as f:
                config_data = json.load(f)
                
            if config_data.get('tab_type') != tab_name:
                QMessageBox.warning(self, "Error", "Configuration is for a different tab type")
                return
                
            # Apply configuration to current tab
            success = self._apply_tab_config(current_tab, tab_name, config_data)
            if success:
                QMessageBox.information(self, "Success", f"Configuration '{name}' loaded successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to apply some configuration settings")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load configuration:\n{str(e)}")
    
    def _extract_tab_config(self, tab_widget, tab_name: str) -> Optional[Dict]:
        """Extract configuration data from a tab widget."""
        config = {}
        
        if tab_name == "Realistic Replay":
            config.update(self._extract_rl_config(tab_widget))
        elif tab_name == "Geometric Conflicts":
            config.update(self._extract_gc_config(tab_widget))
        elif tab_name == "Random Conflicts":
            config.update(self._extract_rc_config(tab_widget))
        elif tab_name == "Procedures":
            config.update(self._extract_proc_config(tab_widget))
        else:
            return None
            
        return config
    
    def _apply_tab_config(self, tab_widget, tab_name: str, config_data: Dict) -> bool:
        """Apply configuration data to a tab widget."""
        try:
            if tab_name == "Realistic Replay":
                return self._apply_rl_config(tab_widget, config_data)
            elif tab_name == "Geometric Conflicts":
                return self._apply_gc_config(tab_widget, config_data)
            elif tab_name == "Random Conflicts":
                return self._apply_rc_config(tab_widget, config_data)
            elif tab_name == "Procedures":
                return self._apply_proc_config(tab_widget, config_data)
            else:
                return False
        except Exception:
            return False
    
    def _extract_proc_config(self, tab_widget) -> Dict:
        """Extract Procedures tab configuration."""
        config = {}
        
        # Files loaded
        config['proc_files'] = getattr(tab_widget, '_proc_files', [])
        config['wpt_files'] = getattr(tab_widget, '_wpt_files', [])
        
        # Origins and destinations
        config['origins'] = getattr(tab_widget, '_origins', {})
        config['destinations'] = getattr(tab_widget, '_destinations', {})
        
        # Generic parameters
        config['gen_flights'] = tab_widget.gen_flights.value()
        config['gen_minsep'] = tab_widget.gen_minsep.value()
        config['spawn_r'] = tab_widget.spawn_r.value()
        config['spawn_env'] = tab_widget.spawn_env.value()
        
        # SID parameters
        config['sid_flights'] = tab_widget.sid_flights.value()
        config['sid_alt'] = tab_widget.sid_alt.value()
        config['sid_spd'] = tab_widget.sid_spd.value()
        config['sid_mode'] = tab_widget.sid_mode.currentIndex()
        
        # STAR parameters
        config['star_flights'] = tab_widget.star_flights.value()
        config['star_alt_fl'] = tab_widget.star_alt_fl.value()
        config['star_mach'] = tab_widget.star_mach.value()
        config['star_mode'] = tab_widget.star_mode.currentIndex()
        config['star_rate_basis'] = tab_widget.star_rate_basis.currentIndex()
        config['star_final_alt_fl'] = tab_widget.star_final_alt_fl.value()
        config['star_final_spd'] = tab_widget.star_final_spd.value()
        
        # Scenario parameters
        config['scn_name'] = tab_widget.scn.text()
        config['seed'] = tab_widget.seed.value()
        config['overwrite'] = tab_widget.overwrite.isChecked()
        config['dest_enable'] = tab_widget.dest_enable.isChecked()
        
        return config
    
    def _apply_proc_config(self, tab_widget, config_data: Dict) -> bool:
        """Apply configuration to Procedures tab."""
        try:
            # Clear existing files from GUI lists and backend first
            tab_widget.lst_wpt.clear()
            tab_widget.lst_proc.clear()
            tab_widget._wpt_files.clear()
            tab_widget._proc_files.clear()
            
            # Apply generic parameters
            if 'gen_flights' in config_data:
                tab_widget.gen_flights.setValue(config_data['gen_flights'])
            if 'gen_minsep' in config_data:
                tab_widget.gen_minsep.setValue(config_data['gen_minsep'])
            if 'spawn_r' in config_data:
                tab_widget.spawn_r.setValue(config_data['spawn_r'])
            if 'spawn_env' in config_data:
                tab_widget.spawn_env.setValue(config_data['spawn_env'])
                
            # Apply SID parameters
            if 'sid_flights' in config_data:
                tab_widget.sid_flights.setValue(config_data['sid_flights'])
            if 'sid_alt' in config_data:
                tab_widget.sid_alt.setValue(config_data['sid_alt'])
            if 'sid_spd' in config_data:
                tab_widget.sid_spd.setValue(config_data['sid_spd'])
            if 'sid_mode' in config_data:
                tab_widget.sid_mode.setCurrentIndex(config_data['sid_mode'])
                
            # Apply STAR parameters
            if 'star_flights' in config_data:
                tab_widget.star_flights.setValue(config_data['star_flights'])
            if 'star_alt_fl' in config_data:
                tab_widget.star_alt_fl.setValue(config_data['star_alt_fl'])
            if 'star_mach' in config_data:
                tab_widget.star_mach.setValue(config_data['star_mach'])
            if 'star_mode' in config_data:
                tab_widget.star_mode.setCurrentIndex(config_data['star_mode'])
            if 'star_rate_basis' in config_data:
                tab_widget.star_rate_basis.setCurrentIndex(config_data['star_rate_basis'])
            if 'star_final_alt_fl' in config_data:
                tab_widget.star_final_alt_fl.setValue(config_data['star_final_alt_fl'])
            if 'star_final_spd' in config_data:
                tab_widget.star_final_spd.setValue(config_data['star_final_spd'])
                
            # Apply scenario parameters
            if 'scn_name' in config_data:
                tab_widget.scn.setText(config_data['scn_name'])
            if 'seed' in config_data:
                tab_widget.seed.setValue(config_data['seed'])
            if 'overwrite' in config_data:
                tab_widget.overwrite.setChecked(config_data['overwrite'])
            if 'dest_enable' in config_data:
                tab_widget.dest_enable.setChecked(config_data['dest_enable'])
                
            # Restore origins and destinations data BEFORE loading files
            if 'origins' in config_data:
                tab_widget._origins = dict(config_data['origins'])
            if 'destinations' in config_data:
                tab_widget._destinations = dict(config_data['destinations'])
                
            # Load files and let the normal GUI mechanism handle everything
            files_loaded = []
            files_failed = []
            
            # Load waypoint files
            if 'wpt_files' in config_data and config_data['wpt_files']:
                for file_path in config_data['wpt_files']:
                    if os.path.exists(file_path):
                        try:
                            # Load the file via backend
                            _emit(_join_tokens("SATG_PROC_LOAD_WPT", _qpath(file_path)))
                            # Add to internal list 
                            tab_widget._wpt_files.append(file_path)
                            # Let the normal GUI method handle the rest
                            tab_widget.lst_wpt.addItem(file_path)
                            files_loaded.append(os.path.basename(file_path))
                        except Exception:
                            files_failed.append(os.path.basename(file_path))
                    else:
                        files_failed.append(f"{os.path.basename(file_path)} (not found)")
                        
            # Load procedure files using simple approach
            if 'proc_files' in config_data and config_data['proc_files']:
                for file_path in config_data['proc_files']:
                    if file_path not in tab_widget._proc_files and os.path.exists(file_path):
                        try:
                            # Load the file via backend
                            _emit(_join_tokens("SATG_PROC_LOAD_PROC", _qpath(file_path)))
                            # Add to internal list
                            tab_widget._proc_files.append(file_path)
                            # Use the simple GUI creation method
                            self._add_single_proc_file_to_gui(tab_widget, file_path)
                            files_loaded.append(os.path.basename(file_path))
                        except Exception:
                            files_failed.append(os.path.basename(file_path))
                    elif not os.path.exists(file_path):
                        files_failed.append(f"{os.path.basename(file_path)} (not found)")
            
            # The origins and destinations are already set in the internal state (lines 754-757)
            # and the _add_single_proc_file_to_gui method will use them automatically
            # No need for additional field updates - this actually causes problems!
            
            # IMPORTANT: Call the sync methods like the original _add_proc does
            if 'proc_files' in config_data and config_data['proc_files']:
                tab_widget._refresh_sid_runway_rows()
                tab_widget._refresh_star_rate_rows()
                tab_widget._sync_destination_edits()
                tab_widget._sync_origin_edits()
                tab_widget._update_dest_state()
            
            # Show file loading results
            if files_loaded or files_failed:
                message_parts = []
                if files_loaded:
                    message_parts.append(f"Successfully loaded files:\n" + "\n".join(f"  • {f}" for f in files_loaded))
                if files_failed:
                    message_parts.append(f"Failed to load files:\n" + "\n".join(f"  • {f}" for f in files_failed))
                    
                QMessageBox.information(self, "File Loading Results", "\n\n".join(message_parts))
                
            return True
        except Exception as e:
            print(f"Error applying proc config: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _add_single_proc_file_to_gui(self, tab_widget, file_path: str):
        """Add a single procedure file to GUI using the same logic as _add_proc."""
        # This extracts the single-file GUI creation logic from _add_proc
        item = QListWidgetItem(tab_widget.lst_proc)
        item.setData(Qt.ItemDataRole.UserRole, file_path)
        container = QWidget(tab_widget.lst_proc)
        row_layout = QHBoxLayout(container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        is_sid = tab_widget._is_sid_file(file_path)
        is_star = tab_widget._is_star_file(file_path)

        origin_edit: Optional[QLineEdit] = None
        origin_all_btn: Optional[QPushButton] = None
        if is_sid:
            origin_edit = QLineEdit(container)
            origin_edit.setPlaceholderText("Origin ICAO")
            origin_edit.setMaxLength(4)
            origin_edit.setMaximumWidth(90)
            if tab_widget._origins.get(file_path):
                origin_edit.setText(tab_widget._origins[file_path])
                # Immediately call the backend to set the ICAO (needed for config loading)
                tab_widget._update_origin_entry(file_path, tab_widget._origins[file_path])
            origin_edit.editingFinished.connect(lambda path=file_path, ref=origin_edit: tab_widget._update_origin_entry(path, ref.text()))

            origin_all_btn = QPushButton("Origin -> all", container)
            origin_all_btn.setAutoDefault(False)
            origin_all_btn.clicked.connect(lambda _, path=file_path: tab_widget._apply_origin_to_all(path))

            row_layout.addWidget(origin_edit)
            row_layout.addWidget(origin_all_btn)

        label = QLabel(file_path, container)
        label.setStyleSheet("color: #555;")
        row_layout.addWidget(label, 2)

        dest_edit = QLineEdit(container)
        dest_edit.setPlaceholderText("Destinations (comma separated)")
        existing = tab_widget._destinations.get(file_path, [])
        if existing:
            dest_edit.setText(", ".join(existing))
            # Immediately call the backend to set the destinations (needed for config loading)
            tab_widget._update_destination_entry(file_path, ", ".join(existing))
        dest_edit.editingFinished.connect(lambda path=file_path, ref=dest_edit: tab_widget._update_destination_entry(path, ref.text()))

        dest_all_btn = QPushButton("Dest -> all", container)
        dest_all_btn.setAutoDefault(False)
        dest_all_btn.clicked.connect(lambda _, path=file_path: tab_widget._apply_dest_to_all(path))

        row_layout.addWidget(dest_edit, 1)
        row_layout.addWidget(dest_all_btn)

        item.setSizeHint(container.sizeHint())
        tab_widget.lst_proc.setItemWidget(item, container)
        
        # CRITICAL: Populate the _proc_widgets dictionary (this was missing!)
        tab_widget._proc_widgets[file_path] = {
            "item": item,
            "origin": origin_edit,
            "origin_all": origin_all_btn,
            "dest": dest_edit,
            "dest_all": dest_all_btn,
            "is_sid": is_sid,
            "is_star": is_star,
        }
    
    def _extract_rl_config(self, tab_widget) -> Dict:
        """Extract Realistic Replay tab configuration."""
        config = {}
        
        # File lists
        config['flights_files'] = getattr(tab_widget, '_chosen_flights_files', []).copy()
        config['tracks_files'] = getattr(tab_widget, '_chosen_tracks_files', []).copy()
        
        # Jitter settings
        config['j_on'] = tab_widget.j_on.isChecked()
        config['j_dist'] = tab_widget.j_dist.currentIndex()
        config['j_seed'] = tab_widget.j_seed.value()
        config['j_dt'] = tab_widget.j_dt.value()
        config['j_dlat'] = tab_widget.j_dlat.value()
        config['j_dlon'] = tab_widget.j_dlon.value()
        config['j_dfl'] = tab_widget.j_dfl.value()
        config['j_nsig'] = tab_widget.j_nsig.value()
        config['j_pct'] = tab_widget.j_pct.value()
        
        # Options
        config['autodel_chk'] = tab_widget.autodel_chk.isChecked()
        
        # Scenario settings
        config['scn_name'] = tab_widget.scn_name.text()
        config['rl_seed'] = tab_widget.rl_seed.value()
        config['rl_overwrite'] = tab_widget.rl_overwrite.isChecked()
        
        return config
    
    def _apply_rl_config(self, tab_widget, config_data: Dict) -> bool:
        """Apply configuration to Realistic Replay tab."""
        try:
            # Clear existing file lists
            tab_widget._chosen_flights_files.clear()
            tab_widget.lst_flights_files.clear()
            tab_widget._chosen_tracks_files.clear()
            tab_widget.lst_tracks_files.clear()
            
            # Restore file lists
            if 'flights_files' in config_data:
                tab_widget._chosen_flights_files.extend(config_data['flights_files'])
                for file_path in config_data['flights_files']:
                    tab_widget.lst_flights_files.addItem(file_path)
            
            if 'tracks_files' in config_data:
                tab_widget._chosen_tracks_files.extend(config_data['tracks_files'])
                for file_path in config_data['tracks_files']:
                    tab_widget.lst_tracks_files.addItem(file_path)
            
            # Restore jitter settings
            if 'j_on' in config_data:
                tab_widget.j_on.setChecked(config_data['j_on'])
            if 'j_dist' in config_data:
                tab_widget.j_dist.setCurrentIndex(config_data['j_dist'])
            if 'j_seed' in config_data:
                tab_widget.j_seed.setValue(config_data['j_seed'])
            if 'j_dt' in config_data:
                tab_widget.j_dt.setValue(config_data['j_dt'])
            if 'j_dlat' in config_data:
                tab_widget.j_dlat.setValue(config_data['j_dlat'])
            if 'j_dlon' in config_data:
                tab_widget.j_dlon.setValue(config_data['j_dlon'])
            if 'j_dfl' in config_data:
                tab_widget.j_dfl.setValue(config_data['j_dfl'])
            if 'j_nsig' in config_data:
                tab_widget.j_nsig.setValue(config_data['j_nsig'])
            if 'j_pct' in config_data:
                tab_widget.j_pct.setValue(config_data['j_pct'])
            
            # Restore options
            if 'autodel_chk' in config_data:
                tab_widget.autodel_chk.setChecked(config_data['autodel_chk'])
            
            # Restore scenario settings
            if 'scn_name' in config_data:
                tab_widget.scn_name.setText(config_data['scn_name'])
            if 'rl_seed' in config_data:
                tab_widget.rl_seed.setValue(config_data['rl_seed'])
            if 'rl_overwrite' in config_data:
                tab_widget.rl_overwrite.setChecked(config_data['rl_overwrite'])
            
            return True
        except Exception as e:
            print(f"Error applying RL config: {e}")
            return False
    
    def _extract_gc_config(self, tab_widget) -> Dict:
        """Extract Geometric Conflicts tab configuration."""
        config = {}
        
        # Minima panel settings
        if hasattr(tab_widget, '_minima'):
            minima = tab_widget._minima
            config['hsep'] = minima._hsep.value()
            config['vsep'] = minima._vsep.value()
        
        # Absolute page settings
        if hasattr(tab_widget, '_absolute_page'):
            abs_page = tab_widget._absolute_page
            config['gc_actypes'] = abs_page.gc_actypes.text()
            config['gc_lat'] = abs_page.gc_lat.text()
            config['gc_lon'] = abs_page.gc_lon.text()
            config['gc_wp'] = abs_page.gc_wp.text()
            config['gc_use_coords_rb'] = abs_page.gc_use_coords_rb.isChecked()
            config['gc_use_wp_rb'] = abs_page.gc_use_wp_rb.isChecked()
            config['gc_fl_value'] = abs_page.gc_fl_value.text()
            config['gc_fl_range'] = abs_page.gc_fl_range.text()
            config['gc_cas_value'] = abs_page.gc_cas_value.text()
            config['gc_cas_range'] = abs_page.gc_cas_range.text()
            config['gc_tcpa_value'] = abs_page.gc_tcpa_value.text()
            config['gc_tcpa_range'] = abs_page.gc_tcpa_range.text()
            config['gc_angle_value'] = abs_page.gc_angle_value.text()
            config['gc_angle_range'] = abs_page.gc_angle_range.text()
            config['gc_alt_offset_value'] = abs_page.gc_alt_offset_value.text()
            config['gc_alt_offset_range'] = abs_page.gc_alt_offset_range.text()
            config['gc_name'] = abs_page.gc_name.text()
            config['gc_seed'] = abs_page.gc_seed.value()
            config['gc_overwrite_cb'] = abs_page.gc_overwrite_cb.isChecked()
            config['show_cpa_cb'] = abs_page.show_cpa_cb.isChecked()
        
        return config
    
    def _apply_gc_config(self, tab_widget, config_data: Dict) -> bool:
        """Apply configuration to Geometric Conflicts tab."""
        try:
            # Minima panel settings
            if hasattr(tab_widget, '_minima'):
                minima = tab_widget._minima
                if 'hsep' in config_data:
                    minima._hsep.setValue(config_data['hsep'])
                if 'vsep' in config_data:
                    minima._vsep.setValue(config_data['vsep'])
            
            # Absolute page settings
            if hasattr(tab_widget, '_absolute_page'):
                abs_page = tab_widget._absolute_page
                if 'gc_actypes' in config_data:
                    abs_page.gc_actypes.setText(config_data['gc_actypes'])
                if 'gc_lat' in config_data:
                    abs_page.gc_lat.setText(config_data['gc_lat'])
                if 'gc_lon' in config_data:
                    abs_page.gc_lon.setText(config_data['gc_lon'])
                if 'gc_wp' in config_data:
                    abs_page.gc_wp.setText(config_data['gc_wp'])
                if 'gc_use_coords_rb' in config_data:
                    abs_page.gc_use_coords_rb.setChecked(config_data['gc_use_coords_rb'])
                if 'gc_use_wp_rb' in config_data:
                    abs_page.gc_use_wp_rb.setChecked(config_data['gc_use_wp_rb'])
                if 'gc_fl_value' in config_data:
                    abs_page.gc_fl_value.setText(config_data['gc_fl_value'])
                if 'gc_fl_range' in config_data:
                    abs_page.gc_fl_range.setText(config_data['gc_fl_range'])
                if 'gc_cas_value' in config_data:
                    abs_page.gc_cas_value.setText(config_data['gc_cas_value'])
                if 'gc_cas_range' in config_data:
                    abs_page.gc_cas_range.setText(config_data['gc_cas_range'])
                if 'gc_tcpa_value' in config_data:
                    abs_page.gc_tcpa_value.setText(config_data['gc_tcpa_value'])
                if 'gc_tcpa_range' in config_data:
                    abs_page.gc_tcpa_range.setText(config_data['gc_tcpa_range'])
                if 'gc_angle_value' in config_data:
                    abs_page.gc_angle_value.setText(config_data['gc_angle_value'])
                if 'gc_angle_range' in config_data:
                    abs_page.gc_angle_range.setText(config_data['gc_angle_range'])
                if 'gc_alt_offset_value' in config_data:
                    abs_page.gc_alt_offset_value.setText(config_data['gc_alt_offset_value'])
                if 'gc_alt_offset_range' in config_data:
                    abs_page.gc_alt_offset_range.setText(config_data['gc_alt_offset_range'])
                if 'gc_name' in config_data:
                    abs_page.gc_name.setText(config_data['gc_name'])
                if 'gc_seed' in config_data:
                    abs_page.gc_seed.setValue(config_data['gc_seed'])
                if 'gc_overwrite_cb' in config_data:
                    abs_page.gc_overwrite_cb.setChecked(config_data['gc_overwrite_cb'])
                if 'show_cpa_cb' in config_data:
                    abs_page.show_cpa_cb.setChecked(config_data['show_cpa_cb'])
            
            return True
        except Exception as e:
            print(f"Error applying GC config: {e}")
            return False
    
    def _extract_rc_config(self, tab_widget) -> Dict:
        """Extract Random Conflicts tab configuration."""
        config = {}
        
        # Common settings
        config['n'] = tab_widget.n.value()
        config['c_lat'] = tab_widget.c_lat.text()
        config['c_lon'] = tab_widget.c_lon.text()
        config['c_rad'] = tab_widget.c_rad.value()
        config['hsep'] = tab_widget.hsep.value()
        config['vsep'] = tab_widget.vsep.value()
        
        # Area type
        config['circle_rb'] = tab_widget.circle_rb.isChecked()
        config['polygon_rb'] = tab_widget.polygon_rb.isChecked()
        config['polygon_name_input'] = tab_widget.polygon_name_input.text()
        config['show_circle_cb'] = tab_widget.show_circle_cb.isChecked()
        config['include_polygon_cb'] = tab_widget.include_polygon_cb.isChecked()
        
        # Absolute conflicts
        config['abs_enabled'] = tab_widget.abs_enabled.isChecked()
        config['abs_actypes'] = tab_widget.abs_actypes.text()
        config['abs_fl_value'] = tab_widget.abs_fl_value.text()
        config['abs_fl_range'] = tab_widget.abs_fl_range.text()
        config['abs_cas_value'] = tab_widget.abs_cas_value.text()
        config['abs_cas_range'] = tab_widget.abs_cas_range.text()
        config['abs_tcpa_value'] = tab_widget.abs_tcpa_value.text()
        config['abs_tcpa_range'] = tab_widget.abs_tcpa_range.text()
        config['abs_angle_value'] = tab_widget.abs_angle_value.text()
        config['abs_angle_range'] = tab_widget.abs_angle_range.text()
        config['abs_alt_offset_value'] = tab_widget.abs_alt_offset_value.text()
        config['abs_alt_offset_range'] = tab_widget.abs_alt_offset_range.text()
        
        # Relative conflicts
        config['rel_enabled'] = tab_widget.rel_enabled.isChecked()
        config['rel_type'] = tab_widget.rel_type.text()
        config['rel_fl_value'] = tab_widget.rel_fl_value.text()
        config['rel_fl_range'] = tab_widget.rel_fl_range.text()
        config['rel_spd_value'] = tab_widget.rel_spd_value.text()
        config['rel_spd_range'] = tab_widget.rel_spd_range.text()
        config['rel_dcpa_value'] = tab_widget.rel_dcpa_value.text()
        config['rel_dcpa_range'] = tab_widget.rel_dcpa_range.text()
        config['rel_tlosh_value'] = tab_widget.rel_tlosh_value.text()
        config['rel_tlosh_range'] = tab_widget.rel_tlosh_range.text()
        config['rel_dh_value'] = tab_widget.rel_dh_value.text()
        config['rel_dh_range'] = tab_widget.rel_dh_range.text()
        config['rel_dpsi_value'] = tab_widget.rel_dpsi_value.text()
        config['rel_dpsi_range'] = tab_widget.rel_dpsi_range.text()
        config['rel_tlosv_value'] = tab_widget.rel_tlosv_value.text()
        config['rel_tlosv_range'] = tab_widget.rel_tlosv_range.text()
        
        # Scenario settings
        config['scn'] = tab_widget.scn.text()
        config['seed'] = tab_widget.seed.value()
        config['gc_overwrite_cb'] = tab_widget.gc_overwrite_cb.isChecked()
        
        return config
    
    def _apply_rc_config(self, tab_widget, config_data: Dict) -> bool:
        """Apply configuration to Random Conflicts tab."""
        try:
            # Common settings
            if 'n' in config_data:
                tab_widget.n.setValue(config_data['n'])
            if 'c_lat' in config_data:
                tab_widget.c_lat.setText(config_data['c_lat'])
            if 'c_lon' in config_data:
                tab_widget.c_lon.setText(config_data['c_lon'])
            if 'c_rad' in config_data:
                tab_widget.c_rad.setValue(config_data['c_rad'])
            if 'hsep' in config_data:
                tab_widget.hsep.setValue(config_data['hsep'])
            if 'vsep' in config_data:
                tab_widget.vsep.setValue(config_data['vsep'])
            
            # Area type
            if 'circle_rb' in config_data:
                tab_widget.circle_rb.setChecked(config_data['circle_rb'])
            if 'polygon_rb' in config_data:
                tab_widget.polygon_rb.setChecked(config_data['polygon_rb'])
            if 'polygon_name_input' in config_data:
                tab_widget.polygon_name_input.setText(config_data['polygon_name_input'])
            if 'show_circle_cb' in config_data:
                tab_widget.show_circle_cb.setChecked(config_data['show_circle_cb'])
            if 'include_polygon_cb' in config_data:
                tab_widget.include_polygon_cb.setChecked(config_data['include_polygon_cb'])
            
            # Absolute conflicts
            if 'abs_enabled' in config_data:
                tab_widget.abs_enabled.setChecked(config_data['abs_enabled'])
            if 'abs_actypes' in config_data:
                tab_widget.abs_actypes.setText(config_data['abs_actypes'])
            if 'abs_fl_value' in config_data:
                tab_widget.abs_fl_value.setText(config_data['abs_fl_value'])
            if 'abs_fl_range' in config_data:
                tab_widget.abs_fl_range.setText(config_data['abs_fl_range'])
            if 'abs_cas_value' in config_data:
                tab_widget.abs_cas_value.setText(config_data['abs_cas_value'])
            if 'abs_cas_range' in config_data:
                tab_widget.abs_cas_range.setText(config_data['abs_cas_range'])
            if 'abs_tcpa_value' in config_data:
                tab_widget.abs_tcpa_value.setText(config_data['abs_tcpa_value'])
            if 'abs_tcpa_range' in config_data:
                tab_widget.abs_tcpa_range.setText(config_data['abs_tcpa_range'])
            if 'abs_angle_value' in config_data:
                tab_widget.abs_angle_value.setText(config_data['abs_angle_value'])
            if 'abs_angle_range' in config_data:
                tab_widget.abs_angle_range.setText(config_data['abs_angle_range'])
            if 'abs_alt_offset_value' in config_data:
                tab_widget.abs_alt_offset_value.setText(config_data['abs_alt_offset_value'])
            if 'abs_alt_offset_range' in config_data:
                tab_widget.abs_alt_offset_range.setText(config_data['abs_alt_offset_range'])
            
            # Relative conflicts
            if 'rel_enabled' in config_data:
                tab_widget.rel_enabled.setChecked(config_data['rel_enabled'])
            if 'rel_type' in config_data:
                tab_widget.rel_type.setText(config_data['rel_type'])
            if 'rel_fl_value' in config_data:
                tab_widget.rel_fl_value.setText(config_data['rel_fl_value'])
            if 'rel_fl_range' in config_data:
                tab_widget.rel_fl_range.setText(config_data['rel_fl_range'])
            if 'rel_spd_value' in config_data:
                tab_widget.rel_spd_value.setText(config_data['rel_spd_value'])
            if 'rel_spd_range' in config_data:
                tab_widget.rel_spd_range.setText(config_data['rel_spd_range'])
            if 'rel_dcpa_value' in config_data:
                tab_widget.rel_dcpa_value.setText(config_data['rel_dcpa_value'])
            if 'rel_dcpa_range' in config_data:
                tab_widget.rel_dcpa_range.setText(config_data['rel_dcpa_range'])
            if 'rel_tlosh_value' in config_data:
                tab_widget.rel_tlosh_value.setText(config_data['rel_tlosh_value'])
            if 'rel_tlosh_range' in config_data:
                tab_widget.rel_tlosh_range.setText(config_data['rel_tlosh_range'])
            if 'rel_dh_value' in config_data:
                tab_widget.rel_dh_value.setText(config_data['rel_dh_value'])
            if 'rel_dh_range' in config_data:
                tab_widget.rel_dh_range.setText(config_data['rel_dh_range'])
            if 'rel_dpsi_value' in config_data:
                tab_widget.rel_dpsi_value.setText(config_data['rel_dpsi_value'])
            if 'rel_dpsi_range' in config_data:
                tab_widget.rel_dpsi_range.setText(config_data['rel_dpsi_range'])
            if 'rel_tlosv_value' in config_data:
                tab_widget.rel_tlosv_value.setText(config_data['rel_tlosv_value'])
            if 'rel_tlosv_range' in config_data:
                tab_widget.rel_tlosv_range.setText(config_data['rel_tlosv_range'])
            
            # Scenario settings
            if 'scn' in config_data:
                tab_widget.scn.setText(config_data['scn'])
            if 'seed' in config_data:
                tab_widget.seed.setValue(config_data['seed'])
            if 'gc_overwrite_cb' in config_data:
                tab_widget.gc_overwrite_cb.setChecked(config_data['gc_overwrite_cb'])
            
            return True
        except Exception as e:
            print(f"Error applying RC config: {e}")
            return False
    
    def _edit_configs(self):
        """Open a dialog to manage saved configurations."""
        config_dir = os.path.join("satg_data", "configSaves")
        if not os.path.exists(config_dir):
            QMessageBox.information(self, "No Configurations", "No saved configurations found.")
            return
        
        # Get all config files
        config_files = [f for f in os.listdir(config_dir) if f.endswith('.json')]
        if not config_files:
            QMessageBox.information(self, "No Configurations", "No saved configurations found.")
            return
        
        # Create config management dialog
        dialog = ConfigManagerDialog(config_dir, config_files, self)
        dialog.exec()

# Config Manager Dialog Class
class ConfigManagerDialog(QDialog):
    """Dialog for managing saved configurations."""
    
    def __init__(self, config_dir: str, config_files: List[str], parent=None):
        super().__init__(parent)
        self.config_dir = config_dir
        self.config_files = config_files
        self.setWindowTitle("Manage Saved Configurations")
        self.setMinimumSize(600, 400)
        
        layout = QVBoxLayout(self)
        
        # Info label
        info_label = QLabel("Manage your saved SATG configurations:")
        layout.addWidget(info_label)
        
        # Config list
        self.config_list = QListWidget()
        self.refresh_list()
        layout.addWidget(self.config_list)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        btn_rename = QPushButton("Rename")
        btn_duplicate = QPushButton("Duplicate")
        btn_delete = QPushButton("Delete")
        btn_close = QPushButton("Close")
        
        btn_rename.clicked.connect(self._rename_config)
        btn_duplicate.clicked.connect(self._duplicate_config)
        btn_delete.clicked.connect(self._delete_config)
        btn_close.clicked.connect(self.accept)
        
        button_layout.addWidget(btn_rename)
        button_layout.addWidget(btn_duplicate)
        button_layout.addWidget(btn_delete)
        button_layout.addStretch()
        button_layout.addWidget(btn_close)
        
        layout.addLayout(button_layout)
    
    def refresh_list(self):
        """Refresh the configuration list."""
        self.config_list.clear()
        self.config_files = [f for f in os.listdir(self.config_dir) if f.endswith('.json')]
        
        for config_file in sorted(self.config_files):
            # Parse config info
            try:
                with open(os.path.join(self.config_dir, config_file), 'r') as f:
                    config_data = json.load(f)
                
                name = config_file.replace('.json', '')
                tab_type = config_data.get('tab_type', 'Unknown')
                saved_at = config_data.get('saved_at', 'Unknown')
                
                # Format datetime if available
                if saved_at != 'Unknown':
                    try:
                        dt = datetime.fromisoformat(saved_at)
                        saved_at = dt.strftime('%Y-%m-%d %H:%M')
                    except:
                        pass
                
                item_text = f"{name} ({tab_type}) - {saved_at}"
                item = QListWidgetItem(item_text)
                item.setData(Qt.ItemDataRole.UserRole, config_file)
                self.config_list.addItem(item)
                
            except Exception:
                # If config file is corrupted, still show it
                item = QListWidgetItem(f"{config_file} (Error reading file)")
                item.setData(Qt.ItemDataRole.UserRole, config_file)
                self.config_list.addItem(item)
    
    def _get_selected_config(self):
        """Get the selected configuration filename."""
        current_item = self.config_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "No Selection", "Please select a configuration.")
            return None
        return current_item.data(Qt.ItemDataRole.UserRole)
    
    def _rename_config(self):
        """Rename the selected configuration."""
        config_file = self._get_selected_config()
        if not config_file:
            return
        
        # Extract current name and tab type
        current_name = config_file.replace('.json', '')
        if '_' in current_name:
            name_part = current_name.rsplit('_', 1)[0]
            tab_part = current_name.rsplit('_', 1)[1]
        else:
            name_part = current_name
            tab_part = ""
        
        new_name, ok = QInputDialog.getText(self, "Rename Configuration", 
                                          f"Enter new name for '{name_part}':", text=name_part)
        if not ok or not new_name.strip():
            return
        
        new_name = new_name.strip()
        if tab_part:
            new_filename = f"{new_name}_{tab_part}.json"
        else:
            new_filename = f"{new_name}.json"
        
        old_path = os.path.join(self.config_dir, config_file)
        new_path = os.path.join(self.config_dir, new_filename)
        
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Name Exists", f"Configuration '{new_filename}' already exists.")
            return
        
        try:
            os.rename(old_path, new_path)
            self.refresh_list()
            QMessageBox.information(self, "Success", f"Configuration renamed to '{new_filename}'")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to rename configuration:\n{str(e)}")
    
    def _duplicate_config(self):
        """Duplicate the selected configuration."""
        config_file = self._get_selected_config()
        if not config_file:
            return
        
        # Extract current name and tab type
        current_name = config_file.replace('.json', '')
        if '_' in current_name:
            name_part = current_name.rsplit('_', 1)[0]
            tab_part = current_name.rsplit('_', 1)[1]
        else:
            name_part = current_name
            tab_part = ""
        
        new_name, ok = QInputDialog.getText(self, "Duplicate Configuration", 
                                          f"Enter name for duplicate of '{name_part}':", 
                                          text=f"{name_part}_copy")
        if not ok or not new_name.strip():
            return
        
        new_name = new_name.strip()
        if tab_part:
            new_filename = f"{new_name}_{tab_part}.json"
        else:
            new_filename = f"{new_name}.json"
        
        old_path = os.path.join(self.config_dir, config_file)
        new_path = os.path.join(self.config_dir, new_filename)
        
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Name Exists", f"Configuration '{new_filename}' already exists.")
            return
        
        try:
            # Read and modify the configuration
            with open(old_path, 'r') as f:
                config_data = json.load(f)
            
            # Update metadata
            config_data['saved_at'] = datetime.now().isoformat()
            
            # Save duplicate
            with open(new_path, 'w') as f:
                json.dump(config_data, f, indent=2)
            
            self.refresh_list()
            QMessageBox.information(self, "Success", f"Configuration duplicated as '{new_filename}'")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to duplicate configuration:\n{str(e)}")
    
    def _delete_config(self):
        """Delete the selected configuration."""
        config_file = self._get_selected_config()
        if not config_file:
            return
        
        # Confirm deletion
        reply = QMessageBox.question(self, "Confirm Deletion", 
                                   f"Are you sure you want to delete configuration '{config_file}'?\n\n"
                                   f"This action cannot be undone.",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            os.remove(os.path.join(self.config_dir, config_file))
            self.refresh_list()
            QMessageBox.information(self, "Success", f"Configuration '{config_file}' deleted successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete configuration:\n{str(e)}")

# --- RL tab (Realistic Replay) --------------------------------------------

class RLTab(QWidget):
    """Realistic Replay: load -> jitter -> autodel/make/run."""
    def __init__(self, parent=None):
        super().__init__(parent)
        main = QVBoxLayout(self)

        # 1) Load (Required)
        gb_load = QGroupBox("1) Load data - Required")
        gb_load_layout = QVBoxLayout(gb_load)
        
        # Description
        desc1 = QLabel("Select flight data and track data files (both required)")
        desc1.setStyleSheet("color: #666; font-style: italic;")
        gb_load_layout.addWidget(desc1)
        
        # Flight data files section (top)
        flights_section = QWidget()
        flights_layout = QVBoxLayout(flights_section)
        flights_layout.setContentsMargins(0, 0, 0, 0)
        
        flights_label = QLabel("Flight data files:")
        flights_layout.addWidget(flights_label)
        
        self.lst_flights_files = QListWidget()
        self.lst_flights_files.setMaximumHeight(100)
        self.lst_flights_files.setToolTip("CSV files containing flight information (callsign, origin, destination, etc.)")
        self._chosen_flights_files = []
        flights_layout.addWidget(self.lst_flights_files)
        
        # Flight files buttons
        flights_buttons = QHBoxLayout()
        btn_add_flights = QPushButton("Add")
        btn_add_flights.setToolTip("Add flight data CSV files")
        btn_add_flights.clicked.connect(self._add_flights_files)
        btn_remove_flights = QPushButton("Remove")
        btn_remove_flights.clicked.connect(self._remove_flights_files)
        btn_clear_flights = QPushButton("Clear")
        btn_clear_flights.clicked.connect(self._clear_flights_files)
        
        flights_buttons.addWidget(btn_add_flights)
        flights_buttons.addWidget(btn_remove_flights)
        flights_buttons.addWidget(btn_clear_flights)
        flights_buttons.addStretch()
        flights_layout.addLayout(flights_buttons)
        
        gb_load_layout.addWidget(flights_section)
        
        # Track data files section (bottom)
        tracks_section = QWidget()
        tracks_layout = QVBoxLayout(tracks_section)
        tracks_layout.setContentsMargins(0, 0, 0, 0)
        
        tracks_label = QLabel("Track data files:")
        tracks_layout.addWidget(tracks_label)
        
        self.lst_tracks_files = QListWidget()
        self.lst_tracks_files.setMaximumHeight(100)
        self.lst_tracks_files.setToolTip("CSV files containing flight track points (time, position, altitude, etc.)")
        self._chosen_tracks_files = []
        tracks_layout.addWidget(self.lst_tracks_files)
        
        # Track files buttons
        tracks_buttons = QHBoxLayout()
        btn_add_tracks = QPushButton("Add")
        btn_add_tracks.setToolTip("Add track data CSV files")
        btn_add_tracks.clicked.connect(self._add_tracks_files)
        btn_remove_tracks = QPushButton("Remove")
        btn_remove_tracks.clicked.connect(self._remove_tracks_files)
        btn_clear_tracks = QPushButton("Clear")
        btn_clear_tracks.clicked.connect(self._clear_tracks_files)
        
        tracks_buttons.addWidget(btn_add_tracks)
        tracks_buttons.addWidget(btn_remove_tracks)
        tracks_buttons.addWidget(btn_clear_tracks)
        tracks_buttons.addStretch()
        tracks_layout.addLayout(tracks_buttons)
        
        gb_load_layout.addWidget(tracks_section)
        
        main.addWidget(gb_load)

        # 2) Jitter (Optional)
        gb_j = QGroupBox("2) Jitter - Optional")
        gb_j_layout = QVBoxLayout(gb_j)
        
        # Create a scroll area for jitter section
        jitter_scroll = QScrollArea()
        jitter_scroll.setWidgetResizable(True)
        jitter_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        jitter_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        jitter_scroll.setMaximumHeight(350)  # Limit height to trigger scrolling
        
        # Create the form widget that will go inside the scroll area
        jitter_form_widget = QWidget()
        fj = QFormLayout(jitter_form_widget)
        fj.setContentsMargins(5, 5, 5, 5)
        desc2 = QLabel("Apply noise to time/position/FL")
        desc2.setStyleSheet("color: #666; font-style: italic;")

        self.j_on = QCheckBox("Enable jitter"); self.j_on.setChecked(False)
        self.j_dist = QComboBox(); self.j_dist.addItems(["uniform", "normal"])
        self.j_seed = QSpinBox(); self.j_seed.setRange(-2**31, 2**31-1); self.j_seed.setSpecialValueText("")
        self.j_seed.setValue(0)

        self.j_dt   = QDoubleSpinBox(); self.j_dt.setDecimals(3); self.j_dt.setRange(0.0, 1e6); self.j_dt.setValue(0.0)
        self.j_dlat = QDoubleSpinBox(); self.j_dlat.setDecimals(6); self.j_dlat.setRange(0.0, 10.0); self.j_dlat.setValue(0.0)
        self.j_dlon = QDoubleSpinBox(); self.j_dlon.setDecimals(6); self.j_dlon.setRange(0.0, 10.0); self.j_dlon.setValue(0.0)
        self.j_dfl  = QSpinBox();       self.j_dfl.setRange(0, 5000); self.j_dfl.setValue(0)
        self.j_nsig = QDoubleSpinBox(); self.j_nsig.setDecimals(2); self.j_nsig.setRange(0.0, 10.0); self.j_nsig.setValue(0.0)

        self.j_pct = QSlider(Qt.Orientation.Horizontal)
        self.j_pct.setRange(0, 100)
        self.j_pct.setValue(100)     
        self.j_pct.setSingleStep(1)
        self.j_pct_label = QLabel("100%")
        self.j_pct.valueChanged.connect(lambda v: self.j_pct_label.setText(f"{v}%"))

        fj.addRow(desc2)
        fj.addRow(self.j_on)
        fj.addRow("dist:", self.j_dist)
        fj.addRow("seed:", self.j_seed)
        fj.addRow("dt [s]:", self.j_dt)
        fj.addRow("dlat [deg]:", self.j_dlat)
        fj.addRow("dlon [deg]:", self.j_dlon)
        fj.addRow("dfl [FL]:", self.j_dfl)
        fj.addRow("nsig (normal):", self.j_nsig)
        row_pct = QWidget(); hb_pct = QHBoxLayout(row_pct); hb_pct.setContentsMargins(0,0,0,0)
        hb_pct.addWidget(self.j_pct, 1); hb_pct.addWidget(self.j_pct_label)
        fj.addRow("Jitter % of flights:", row_pct)

        # Set the form widget as the scroll area's widget
        jitter_scroll.setWidget(jitter_form_widget)
        
        # Add the scroll area to the group box
        gb_j_layout.addWidget(jitter_scroll)
        main.addWidget(gb_j)

        # 3) Options
        gb_options = QGroupBox("3) Options")
        options_layout = QVBoxLayout(gb_options)
        options_layout.setContentsMargins(8, 8, 8, 8)
        
        self.autodel_chk = QCheckBox("Auto-delete at last waypoint")
        self.autodel_chk.setChecked(True)
        options_layout.addWidget(self.autodel_chk)
        
        main.addWidget(gb_options)

        # 4) Create Scenario
        actions_gb = QGroupBox("4) Create Scenario")
        actions_main_layout = QVBoxLayout(actions_gb)
        actions_main_layout.setContentsMargins(8, 8, 8, 8)
        actions_main_layout.setSpacing(10)
        
        # Scenario controls form
        scenario_form = QFormLayout()
        scenario_form.setContentsMargins(0, 0, 0, 0)
        scenario_form.setSpacing(8)
        
        self.scn_name = QLineEdit("replay")
        self.scn_name.setPlaceholderText("Scenario name, e.g. replay_01")
        
        self.rl_seed = QSpinBox()
        self.rl_seed.setRange(0, 2**31-1)
        self.rl_seed.setValue(0)
        self.rl_seed.setToolTip("Seed for jitter (0=random)")
        
        self.rl_overwrite = QCheckBox("Overwrite scenario if it exists")
        self.rl_overwrite.setChecked(False)
        
        scenario_form.addRow("Scenario name:", self.scn_name)
        scenario_form.addRow("Seed (0=random):", self.rl_seed)
        scenario_form.addRow(self.rl_overwrite)
        
        actions_main_layout.addLayout(scenario_form)
        
        # Action buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)
        
        btn_make = QPushButton("CREATE SCENARIO")
        btn_run_only = QPushButton("RUN SCENARIO")
        btn_run = QPushButton("CREATE & RUN SCENARIO")
        
        btn_make.clicked.connect(self._make)
        btn_run_only.clicked.connect(self._run_only)
        btn_run.clicked.connect(self._run)
        
        buttons_layout.addWidget(btn_make)
        buttons_layout.addWidget(btn_run_only)
        buttons_layout.addWidget(btn_run)
        buttons_layout.addStretch(1)
        
        actions_main_layout.addLayout(buttons_layout)

        main.addWidget(actions_gb)
        main.addStretch(1)

    def _add_flights_files(self):
        """Add flight data files to the list"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose flight data CSV files", 
            filter="CSV files (*.csv);;All files (*)"
        )
        if not files:
            return
            
        # Add new files that aren't already in the list
        new_files = [f for f in files if f not in self._chosen_flights_files]
        self._chosen_flights_files.extend(new_files)
        
        # Update the list widget
        for file_path in new_files:
            self.lst_flights_files.addItem(file_path)

    def _remove_flights_files(self):
        """Remove selected flight files from the list"""
        selected_items = self.lst_flights_files.selectedItems()
        for item in selected_items:
            file_path = item.text()
            # Remove from internal list
            if file_path in self._chosen_flights_files:
                self._chosen_flights_files.remove(file_path)
            # Remove from list widget
            self.lst_flights_files.takeItem(self.lst_flights_files.row(item))

    def _clear_flights_files(self):
        """Clear all flight files from the list"""
        self._chosen_flights_files.clear()
        self.lst_flights_files.clear()

    def _add_tracks_files(self):
        """Add track data files to the list"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose track data CSV files", 
            filter="CSV files (*.csv);;All files (*)"
        )
        if not files:
            return
            
        # Add new files that aren't already in the list
        new_files = [f for f in files if f not in self._chosen_tracks_files]
        self._chosen_tracks_files.extend(new_files)
        
        # Update the list widget
        for file_path in new_files:
            self.lst_tracks_files.addItem(file_path)

    def _remove_tracks_files(self):
        """Remove selected track files from the list"""
        selected_items = self.lst_tracks_files.selectedItems()
        for item in selected_items:
            file_path = item.text()
            # Remove from internal list
            if file_path in self._chosen_tracks_files:
                self._chosen_tracks_files.remove(file_path)
            # Remove from list widget
            self.lst_tracks_files.takeItem(self.lst_tracks_files.row(item))

    def _clear_tracks_files(self):
        """Clear all track files from the list"""
        self._chosen_tracks_files.clear()
        self.lst_tracks_files.clear()

    def _get_data_files_for_backend(self):
        """Get the combined list of data files to send to backend"""
        all_files = self._chosen_flights_files + self._chosen_tracks_files
        if not all_files:
            return "AUTO"  # Fallback to AUTO if no files selected
        # Use pipe separator to avoid issues with commas and spaces
        return "|".join(all_files)


    def _emit_jitter_if_needed(self):
        if not hasattr(self, "j_on"):
            return

        if not self.j_on.isChecked():
            _emit("SATG_RL_JITTER off")
            return

        # Collect values (positional order)
        mode = "on"
        dist = self.j_dist.currentText() if hasattr(self, "j_dist") else "normal"

        # Use zeros for unset numeric fields so the parser is happy and backend treats them as no-noise.
        # Use scenario seed if jitter seed is 0 and scenario seed is set
        jitter_seed = int(self.j_seed.value()) if hasattr(self, "j_seed") else 0
        if jitter_seed == 0 and hasattr(self, "rl_seed"):
            jitter_seed = int(self.rl_seed.value())
        
        seed = jitter_seed
        dt   = float(self.j_dt.value())   if hasattr(self, "j_dt")   else 0.0
        dlat = float(self.j_dlat.value()) if hasattr(self, "j_dlat") else 0.0
        dlon = float(self.j_dlon.value()) if hasattr(self, "j_dlon") else 0.0
        dfl  = int(self.j_dfl.value())    if hasattr(self, "j_dfl")  else 0
        nsig = float(self.j_nsig.value()) if hasattr(self, "j_nsig") else 0.0
        pct  = int(self.j_pct.value())    if hasattr(self, "j_pct")  else 100

        # Build a strictly positional command; no key=value anywhere.
        cmd = f"SATG_RL_JITTER {mode} {dist} {seed} {dt} {dlat} {dlon} {dfl} {nsig} {pct}"
        _emit(cmd)

    def _emit_autodel_from_toggle(self):
        """Emit SATG_RL_AUTODEL based on the checkbox state."""
        _emit("SATG_RL_AUTODEL " + ("on" if self.autodel_chk.isChecked() else "off"))

    def _validate_files(self):
        """Check if both flight and track files are selected and show warnings if not"""
        has_flights = len(self._chosen_flights_files) > 0
        has_tracks = len(self._chosen_tracks_files) > 0
        
        if not has_flights and not has_tracks:
            # No files selected, will use AUTO
            return True
        elif not has_flights:
            # Only track files selected
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.question(self, "Missing Flight Data", 
                                       "No flight data files selected. Realistic replay typically requires both flight data and track data files.\n\nContinue anyway?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            return reply == QMessageBox.StandardButton.Yes
        elif not has_tracks:
            # Only flight files selected
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.question(self, "Missing Track Data", 
                                       "No track data files selected. Realistic replay typically requires both flight data and track data files.\n\nContinue anyway?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            return reply == QMessageBox.StandardButton.Yes
        else:
            # Both types selected, all good
            return True

    def _make(self):
        name = self.scn_name.text().strip()
        if not name:
            return
        
        if not self._validate_files():
            return
            
        self._emit_autodel_from_toggle()
        self._emit_jitter_if_needed()
        ow = 1 if self.rl_overwrite.isChecked() else 0
        data_files = self._get_data_files_for_backend()
        _emit(f"SATG_RL_MAKE {name} {ow} {data_files}")

    def _run_only(self):
        """Run an existing scenario without creating it"""
        name = self.scn_name.text().strip()
        if not name:
            return
        
        # Just load the existing scenario file
        _emit(f"IC scenario/{name}.scn")

    def _run(self):
        name = self.scn_name.text().strip()
        if not name:
            return
        
        if not self._validate_files():
            return
            
        self._emit_autodel_from_toggle()
        self._emit_jitter_if_needed()
        ow = 1 if self.rl_overwrite.isChecked() else 0
        data_files = self._get_data_files_for_backend()
        _emit(f"SATG_RL_RUN {name} {ow} {data_files}")


# --- GC tab (Geometric Conflicts) ------------------------------------------

class GCMinimaPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Separation minima", parent)
        layout = QFormLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._hsep = QDoubleSpinBox(self)
        self._hsep.setRange(0.0, 50.0)
        self._hsep.setDecimals(2)
        self._hsep.setSingleStep(0.1)
        self._hsep.setValue(5.0)
        layout.addRow("Horizontal [NM]:", self._hsep)

        self._vsep = QSpinBox(self)
        self._vsep.setRange(0, 5000)
        self._vsep.setSingleStep(50)
        self._vsep.setValue(1000)
        layout.addRow("Vertical [ft]:", self._vsep)

    def hsep_value(self) -> float:
        return float(self._hsep.value())

    def vsep_value(self) -> int:
        return int(self._vsep.value())


class GCAbsolutePage(QWidget):
    def __init__(self, minima_panel: GCMinimaPanel, parent=None):
        super().__init__(parent)
        self._minima = minima_panel

        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(10)

        gb1 = QGroupBox("1) CPA options")
        gb1_layout = QVBoxLayout(gb1)
        
        # Create a scroll area for CPA options
        cpa_scroll = QScrollArea()
        cpa_scroll.setWidgetResizable(True)
        cpa_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        cpa_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        cpa_scroll.setMaximumHeight(350)  # Limit height to trigger scrolling
        
        # Create the form widget that will go inside the scroll area
        cpa_form_widget = QWidget()
        f1 = QFormLayout(cpa_form_widget)
        f1.setContentsMargins(5, 5, 5, 5)
        f1.setSpacing(8)

        # CPA reference row
        cpa_row = QWidget()
        cpa_layout = QHBoxLayout(cpa_row)
        cpa_layout.setContentsMargins(0, 0, 0, 0)
        cpa_layout.setSpacing(12)

        coord_box = QGroupBox("Coordinate")
        coord_form = QFormLayout(coord_box)
        coord_form.setContentsMargins(6, 6, 6, 6)
        coord_form.setSpacing(6)

        self.gc_use_coords_rb = QRadioButton("Use coordinates")
        self.gc_use_coords_rb.setChecked(True)
        coord_form.addRow(self.gc_use_coords_rb)
        self.gc_lat = QLineEdit("52.100")
        self.gc_lat.setClearButtonEnabled(True)
        self.gc_lon = QLineEdit("4.500")
        self.gc_lon.setClearButtonEnabled(True)
        coord_form.addRow("Latitude [deg]:", self.gc_lat)
        coord_form.addRow("Longitude [deg]:", self.gc_lon)

        wp_box = QGroupBox("Waypoint")
        wp_form = QFormLayout(wp_box)
        wp_form.setContentsMargins(6, 6, 6, 6)
        wp_form.setSpacing(6)
        self.gc_use_wp_rb = QRadioButton("Use waypoint")
        wp_form.addRow(self.gc_use_wp_rb)
        self.gc_wp = QLineEdit("")
        self.gc_wp.setPlaceholderText("e.g. SUGOL or EHAM")
        self.gc_wp.setClearButtonEnabled(True)
        wp_form.addRow("Identifier:", self.gc_wp)

        self.gc_cpa_mode = QButtonGroup(self)
        self.gc_cpa_mode.addButton(self.gc_use_coords_rb)
        self.gc_cpa_mode.addButton(self.gc_use_wp_rb)

        cpa_layout.addWidget(coord_box, 1)
        cpa_layout.addWidget(wp_box, 1)
        f1.addRow("CPA reference:", cpa_row)

        self.gc_use_coords_rb.toggled.connect(self._update_cpa_reference_mode)
        self.gc_use_wp_rb.toggled.connect(self._update_cpa_reference_mode)
        self._update_cpa_reference_mode()

        # Connect parameter change signals to update CPA reference display
        self.gc_lat.textChanged.connect(self._update_cpa_if_visible)
        self.gc_lon.textChanged.connect(self._update_cpa_if_visible)
        self.gc_wp.textChanged.connect(self._update_cpa_if_visible)

        self.gc_tcpa_value = QLineEdit("120.0")
        self.gc_tcpa_value.setClearButtonEnabled(True)
        self.gc_tcpa_range = QLineEdit()
        self.gc_tcpa_range.setClearButtonEnabled(True)
        f1.addRow(
            "TCPA [s]:",
            self._make_value_range_row(self.gc_tcpa_value, self.gc_tcpa_range, "upper [s] (optional)"),
        )

        self.gc_angle_value = QLineEdit("90.0")
        self.gc_angle_value.setClearButtonEnabled(True)
        self.gc_angle_range = QLineEdit()
        self.gc_angle_range.setClearButtonEnabled(True)
        f1.addRow(
            "CPA angle [deg] (cross only):",
            self._make_value_range_row(self.gc_angle_value, self.gc_angle_range, "upper [deg] (optional)"),
        )

        self.gc_alt_offset_value = QLineEdit("0")
        self.gc_alt_offset_value.setClearButtonEnabled(True)
        self.gc_alt_offset_range = QLineEdit()
        self.gc_alt_offset_range.setClearButtonEnabled(True)
        f1.addRow(
            "Altitude offset dH [ft] (optional):",
            self._make_value_range_row(self.gc_alt_offset_value, self.gc_alt_offset_range, "upper [ft] (optional)"),
        )

        self.gc_actypes = QLineEdit("A320,B738,A350,B78X")
        f1.addRow("AC types:", self.gc_actypes)

        # CPA reference visualization option
        self.show_cpa_cb = QCheckBox("Show CPA reference on screen")
        self.show_cpa_cb.setChecked(False)
        self.show_cpa_cb.setToolTip("Draw the CPA reference point on BlueSky screen (coordinates mode only)")
        self.show_cpa_cb.toggled.connect(self._toggle_cpa_display)
        f1.addRow(self.show_cpa_cb)

        # Set the form widget as the scroll area's widget
        cpa_scroll.setWidget(cpa_form_widget)
        
        # Add the scroll area to the group box
        gb1_layout.addWidget(cpa_scroll)
        main.addWidget(gb1)

        gb2 = QGroupBox("2) Flight profile")
        gb2_layout = QVBoxLayout(gb2)
        
        # Create a scroll area for flight profile
        profile_scroll = QScrollArea()
        profile_scroll.setWidgetResizable(True)
        profile_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        profile_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        profile_scroll.setMaximumHeight(200)  # Limit height to trigger scrolling
        
        # Create the form widget that will go inside the scroll area
        profile_form_widget = QWidget()
        f2 = QFormLayout(profile_form_widget)
        f2.setContentsMargins(5, 5, 5, 5)
        f2.setSpacing(8)

        self.gc_fl_value = QLineEdit("310")
        self.gc_fl_value.setClearButtonEnabled(True)
        self.gc_fl_range = QLineEdit()
        self.gc_fl_range.setClearButtonEnabled(True)
        f2.addRow(
            "FL (value or range):",
            self._make_value_range_row(self.gc_fl_value, self.gc_fl_range, "upper FL (optional)"),
        )

        self.gc_cas_value = QLineEdit("250")
        self.gc_cas_value.setClearButtonEnabled(True)
        self.gc_cas_range = QLineEdit()
        self.gc_cas_range.setClearButtonEnabled(True)
        f2.addRow(
            "CAS [kt] (value or range):",
            self._make_value_range_row(self.gc_cas_value, self.gc_cas_range, "upper [kt] (optional)"),
        )

        # Set the form widget as the scroll area's widget
        profile_scroll.setWidget(profile_form_widget)
        
        # Add the scroll area to the group box
        gb2_layout.addWidget(profile_scroll)
        main.addWidget(gb2)

        gb3 = QGroupBox("3) Actions")
        f3 = QFormLayout(gb3)
        f3.setContentsMargins(8, 8, 8, 8)
        f3.setSpacing(8)

        name_row = QWidget()
        name_layout = QHBoxLayout(name_row)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(6)
        name_layout.addWidget(QLabel("Scenario name:"))
        self.gc_name = QLineEdit("gc_scn")
        name_layout.addWidget(self.gc_name)
        f3.addRow(name_row)

        # Add seed field like GC Relative has
        self.gc_seed = QSpinBox()
        self.gc_seed.setRange(0, 2_000_000_000)
        self.gc_seed.setValue(0)
        f3.addRow("Seed (0 = random):", self.gc_seed)

        self.gc_overwrite_cb = QCheckBox("Overwrite scenario if it exists")
        self.gc_overwrite_cb.setChecked(True)
        f3.addRow(self.gc_overwrite_cb)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(10)
        btn_cre = QPushButton("CREATE SCENARIO")
        btn_run = QPushButton("RUN SCENARIO")
        btn_both = QPushButton("CREATE & RUN SCENARIO")
        btn_layout.addWidget(btn_cre)
        btn_layout.addWidget(btn_run)
        btn_layout.addWidget(btn_both)
        btn_layout.addStretch(1)
        f3.addRow(btn_row)

        main.addWidget(gb3)

        btn_cre.clicked.connect(self._gc_create)
        btn_run.clicked.connect(self._gc_run_only)
        btn_both.clicked.connect(self._gc_create_and_run)

    def _make_value_range_row(self, value_widget: QLineEdit, range_widget: QLineEdit, placeholder: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        value_widget.setMaximumWidth(90)
        value_widget.setPlaceholderText("required")
        value_widget.setToolTip("Required value (used as deterministic or lower bound).")
        range_widget.setPlaceholderText(placeholder)
        range_widget.setToolTip("Optional upper bound; leave empty for a fixed value.")
        range_widget.setMinimumWidth(140)
        layout.addWidget(value_widget)
        layout.addWidget(range_widget, 1)
        return row

    def _extract_numeric_field(
        self,
        label: str,
        primary: QLineEdit,
        secondary: QLineEdit,
        *,
        allow_float: bool,
        allow_negative: bool,
    ) -> Optional[Tuple[str, bool, float, float]]:
        primary_txt = primary.text().strip()
        if not primary_txt:
            _emit(f"ECHO Please fill the left {label} field.")
            return None
        try:
            primary_val = self._coerce_number(primary_txt, allow_float, allow_negative, label, "value")
        except ValueError as exc:
            _emit(f"ECHO {exc}")
            return None
        secondary_txt = secondary.text().strip()
        if secondary_txt:
            try:
                secondary_val = self._coerce_number(secondary_txt, allow_float, allow_negative, label, "upper bound")
            except ValueError as exc:
                _emit(f"ECHO {exc}")
                return None
            lo_val = float(primary_val)
            hi_val = float(secondary_val)
            if hi_val < lo_val:
                lo_val, hi_val = hi_val, lo_val
            normalized = f"{self._format_number(lo_val, allow_float)}:{self._format_number(hi_val, allow_float)}"
            return normalized, True, lo_val, hi_val

        normalized = self._format_number(primary_val, allow_float)
        return normalized, False, float(primary_val), float(primary_val)

    def _coerce_number(self, text: str, allow_float: bool, allow_negative: bool, label: str, desc: str):
        stripped = text.strip()
        if not stripped:
            raise ValueError(f"{label} {desc} is empty.")
        try:
            value = float(stripped)
        except ValueError as exc:
            raise ValueError(f"{label} {desc} must be numeric.") from exc
        if not allow_negative and value < 0.0:
            raise ValueError(f"{label} {desc} must be >= 0.")
        if allow_float:
            return value
        return int(round(value))

    def _format_number(self, value, allow_float: bool) -> str:
        if allow_float:
            txt = f"{float(value):.6f}".rstrip("0").rstrip(".")
            return txt if txt and txt != "-0" else "0"
        return str(int(round(float(value))))

    def _format_mach(self, value: float) -> str:
        txt = f"{float(value):.3f}".rstrip("0").rstrip(".")
        if not txt or txt == "0":
            txt = "0"
        if txt.startswith("."):
            txt = "0" + txt
        return f"M{txt}"

    def _normalize_speed_text(self, text: str) -> Tuple[str, float, bool]:
        raw = text.strip()
        if not raw:
            raise ValueError("CAS/Mach value is empty.")
        compact = raw.replace(" ", "")
        upper = compact.upper()
        if upper.startswith("M"):
            num_txt = upper[1:]
            if not num_txt:
                raise ValueError("Mach value must include digits after the 'M' prefix.")
            try:
                value = float(num_txt)
            except ValueError as exc:
                raise ValueError("Mach value must be numeric, e.g. M0.78.") from exc
            if value <= 0.0:
                raise ValueError("Mach value must be greater than zero.")
            return self._format_mach(value), value, True
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("CAS value must be numeric knots or use the 'M' prefix for Mach.") from exc
        if value <= 0.0:
            raise ValueError("CAS value must be greater than zero.")
        return self._format_number(value, allow_float=True), value, False

    def _emit_gc_conf(self):
        _emit(f"SATG_GC_CONF {self._minima.hsep_value()} {self._minima.vsep_value()}")

    def _emit_gc_range(self):
        fl_field = self._extract_numeric_field("FL", self.gc_fl_value, self.gc_fl_range, allow_float=False, allow_negative=False)
        if fl_field is None:
            return
        fl_txt, _, _, _ = fl_field

        cas_field = self._extract_numeric_field("CAS [kt]", self.gc_cas_value, self.gc_cas_range, allow_float=False, allow_negative=False)
        if cas_field is None:
            return
        cas_txt, _, _, _ = cas_field

        toks = ["SATG_GC_RANGE", _kv("fl", fl_txt), _kv("cas", cas_txt)]
        _emit(_join_tokens(*toks))

    def _emit_gc_cre(self):
        name = self.gc_name.text().strip()
        if not name:
            _emit("ECHO Please provide a scenario name.")
            return

        location_tokens: List[str] = []
        if self.gc_use_coords_rb.isChecked():
            lat_txt = self.gc_lat.text().strip()
            lon_txt = self.gc_lon.text().strip()
            if not lat_txt or not lon_txt:
                _emit("ECHO Please provide CPA latitude and longitude.")
                return
            location_tokens.extend([_kv("lat", lat_txt), _kv("lon", lon_txt)])
        else:
            wp_txt = self.gc_wp.text().strip().upper()
            if not wp_txt:
                _emit("ECHO Please provide a waypoint identifier for the CPA.")
                return
            location_tokens.append(_kv("wp", wp_txt))

        tcpa_field = self._extract_numeric_field("TCPA [s]", self.gc_tcpa_value, self.gc_tcpa_range, allow_float=True, allow_negative=False)
        if tcpa_field is None:
            return
        tcpa_txt, _, tcpa_lo, tcpa_hi = tcpa_field
        if tcpa_lo <= 0 or tcpa_hi <= 0:
            _emit("ECHO TCPA [s] must be greater than zero.")
            return

        angle_field = self._extract_numeric_field("CPA angle [deg]", self.gc_angle_value, self.gc_angle_range, allow_float=True, allow_negative=False)
        if angle_field is None:
            return
        angle_txt, _, angle_lo, angle_hi = angle_field
        if angle_lo < 0 or angle_hi > 180:
            _emit("ECHO CPA angle must stay within 0-180 degrees.")
            return

        alt_field = self._extract_numeric_field("Initial altitude offset [ft]", self.gc_alt_offset_value, self.gc_alt_offset_range, allow_float=False, allow_negative=True)
        if alt_field is None:
            return
        alt_txt, alt_is_range, alt_lo, _ = alt_field

        toks = ["SATG_GC_CRE", _kv("name", name)]
        toks.extend(location_tokens)
        toks.extend([
            _kv("tcpa", tcpa_txt),
            _kv("overwrite", 1 if self.gc_overwrite_cb.isChecked() else 0),
            _kv("angle", angle_txt),
        ])

        if alt_is_range or abs(alt_lo) > 1e-6:
            toks.append(_kv("dh", alt_txt))

        seed_val = int(self.gc_seed.value())
        if seed_val > 0:
            toks.append(_kv("seed", seed_val))

        self._emit_gc_types()
        self._emit_gc_conf()
        self._emit_gc_range()
        _emit(_join_tokens(*toks))

    def _emit_gc_types(self):
        raw = self.gc_actypes.text().strip()
        if not raw:
            _emit("SATG_GC_TYPES")
            return
        parts = [seg.strip().upper() for seg in re.split(r"[,\s]+", raw.replace("|", " ")) if seg.strip()]
        if not parts:
            _emit("SATG_GC_TYPES")
            return
        cmd = "SATG_GC_TYPES " + " ".join(parts)
        _emit(cmd)

    def _gc_create(self):
        self._emit_gc_cre()

    def _gc_run_only(self):
        name = self.gc_name.text().strip()
        if name:
            _emit("SATG_GC_RUN " + name)

    def _gc_create_and_run(self):
        self._gc_create()
        self._gc_run_only()

    def _update_cpa_reference_mode(self):
        use_coords = self.gc_use_coords_rb.isChecked()
        self.gc_lat.setEnabled(use_coords)
        self.gc_lon.setEnabled(use_coords)
        self.gc_wp.setEnabled(not use_coords)
        
        # Enable/disable CPA visualization checkbox based on mode
        if hasattr(self, 'show_cpa_cb'):
            self.show_cpa_cb.setEnabled(use_coords)
            if not use_coords:
                # Hide CPA reference when switching to waypoint mode
                self._hide_cpa_reference()
        
        # Update CPA display if visible
        self._update_cpa_if_visible()

    def _toggle_cpa_display(self):
        """Show or hide the CPA reference point on the BlueSky screen."""
        if self.show_cpa_cb.isChecked():
            self._show_cpa_reference()
        else:
            self._hide_cpa_reference()
            
    def _update_cpa_if_visible(self):
        """Update CPA reference display if it's currently visible."""
        if hasattr(self, 'show_cpa_cb') and self.show_cpa_cb.isChecked():
            self._show_cpa_reference()
            
    def _show_cpa_reference(self):
        """Draw the CPA reference point on the BlueSky screen."""
        try:
            if self.gc_use_coords_rb.isChecked():
                # Use coordinate mode - show visual indicator
                lat = float(self.gc_lat.text())
                lon = float(self.gc_lon.text())
                ref_name = "GC_CPA_REF_COORD"
                # Draw a small circle to mark the CPA reference point
                _emit(f"CIRCLE {ref_name} {lat} {lon} 0.5")  # 0.5 NM radius for visibility
            else:
                # Use waypoint mode - don't show visual indicator, just hide coordinate marker
                _emit(f"CIRCLE GC_CPA_REF_COORD 0 0 0")  # Hide coordinate marker
            
        except (ValueError, AttributeError):
            # Invalid coordinates, don't draw anything
            pass
            
    def _hide_cpa_reference(self):
        """Remove the CPA reference point from the BlueSky screen."""
        # Hide both coordinate and waypoint reference markers
        _emit(f"CIRCLE GC_CPA_REF_COORD 0 0 0")  # Hide coordinate marker


class GCRelativePage(QWidget):
    def __init__(self, minima_panel: GCMinimaPanel, parent=None):
        super().__init__(parent)
        self._minima = minima_panel

        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(10)

        target_box = QGroupBox("2) Target aircraft")
        target_box_layout = QVBoxLayout(target_box)
        
        # Create a scroll area for the target fields
        target_scroll = QScrollArea()
        target_scroll.setWidgetResizable(True)
        target_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        target_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        target_scroll.setMaximumHeight(300)  # Limit height to trigger scrolling
        
        # Create the form widget that will go inside the scroll area
        target_form_widget = QWidget()
        target_form = QFormLayout(target_form_widget)
        target_form.setContentsMargins(5, 5, 5, 5)
        
        combo_row = QWidget()
        combo_layout = QHBoxLayout(combo_row)
        combo_layout.setContentsMargins(0, 0, 0, 0)
        self.target_combo = QComboBox()
        self.target_combo.setEditable(False)
        self.target_combo.setToolTip("Active traffic IDs captured live; blank means auto-generate when creating a target.")
        self.refresh_btn = QPushButton("Refresh")
        combo_layout.addWidget(self.target_combo, 1)
        combo_layout.addWidget(self.refresh_btn)
        combo_layout.addStretch(1)
        target_form.addRow("Live traffic:", combo_row)

        self.include_target_cb = QCheckBox("Create / override target using the fields below")
        target_form.addRow(self.include_target_cb)

        self.target_acid = QLineEdit("")
        self.target_acid.setPlaceholderText("optional – auto if left blank")
        self.target_acid.setClearButtonEnabled(True)
        target_form.addRow("Target callsign:", self.target_acid)
        self.target_type = QLineEdit("A320,B738,A350,B78X")
        self.target_type.setClearButtonEnabled(True)
        target_form.addRow("Aircraft type:", self.target_type)

        self.target_lat_value = QLineEdit("52.100")
        self.target_lat_value.setClearButtonEnabled(True)
        self.target_lat_range = QLineEdit()
        self.target_lat_range.setClearButtonEnabled(True)
        target_form.addRow(
            "Latitude [deg]:",
            self._make_numeric_row(self.target_lat_value, self.target_lat_range, "upper [deg] (optional)"),
        )

        self.target_lon_value = QLineEdit("4.500")
        self.target_lon_value.setClearButtonEnabled(True)
        self.target_lon_range = QLineEdit()
        self.target_lon_range.setClearButtonEnabled(True)
        target_form.addRow(
            "Longitude [deg]:",
            self._make_numeric_row(self.target_lon_value, self.target_lon_range, "upper [deg] (optional)"),
        )

        self.target_hdg_value = QLineEdit("0.0")
        self.target_hdg_value.setClearButtonEnabled(True)
        self.target_hdg_range = QLineEdit()
        self.target_hdg_range.setClearButtonEnabled(True)
        target_form.addRow(
            "Track [deg]:",
            self._make_numeric_row(self.target_hdg_value, self.target_hdg_range, "upper [deg] (optional)"),
        )

        self.target_alt_value = QLineEdit("20000")
        self.target_alt_value.setClearButtonEnabled(True)
        self.target_alt_range = QLineEdit()
        self.target_alt_range.setClearButtonEnabled(True)
        target_form.addRow(
            "Altitude [ft]:",
            self._make_numeric_row(self.target_alt_value, self.target_alt_range, "upper [ft] (optional)"),
        )

        self.target_spd_value = QLineEdit("250.0")
        self.target_spd_value.setClearButtonEnabled(True)
        self.target_spd_range = QLineEdit()
        self.target_spd_range.setClearButtonEnabled(True)
        target_form.addRow(
            "CAS [kt]:",
            self._make_numeric_row(self.target_spd_value, self.target_spd_range, "upper [kt] (optional)"),
        )
        
        # Set the form widget as the scroll area's widget
        target_scroll.setWidget(target_form_widget)
        
        # Add the scroll area to the group box
        target_box_layout.addWidget(target_scroll)
        main.addWidget(target_box)

        intr_box = QGroupBox("3) Intruder setup")
        intr_box_layout = QVBoxLayout(intr_box)
        
        # Create a scroll area for the intruder fields
        intr_scroll = QScrollArea()
        intr_scroll.setWidgetResizable(True)
        intr_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        intr_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        intr_scroll.setMaximumHeight(280)  # Limit height to trigger scrolling
        
        # Create the form widget that will go inside the scroll area
        intr_form_widget = QWidget()
        intr_form = QFormLayout(intr_form_widget)
        intr_form.setContentsMargins(5, 5, 5, 5)
        
        self.intr_acid = QLineEdit("")
        self.intr_acid.setPlaceholderText("optional – auto if left blank")
        self.intr_acid.setClearButtonEnabled(True)
        intr_form.addRow("Intruder callsign (optional):", self.intr_acid)
        self.intr_type = QLineEdit("A320,B738,A350,B78X")
        self.intr_type.setClearButtonEnabled(True)
        intr_form.addRow("Aircraft type:", self.intr_type)

        self.intr_dpsi_value = QLineEdit("90.0")
        self.intr_dpsi_value.setClearButtonEnabled(True)
        self.intr_dpsi_range = QLineEdit()
        self.intr_dpsi_range.setClearButtonEnabled(True)
        intr_form.addRow(
            "Conflict angle dpsi [deg]:",
            self._make_numeric_row(self.intr_dpsi_value, self.intr_dpsi_range, "upper [deg] (optional)"),
        )

        self.intr_dcpa_value = QLineEdit("2.0")
        self.intr_dcpa_value.setClearButtonEnabled(True)
        self.intr_dcpa_range = QLineEdit()
        self.intr_dcpa_range.setClearButtonEnabled(True)
        intr_form.addRow(
            "CPA distance dcpa [NM]:",
            self._make_numeric_row(self.intr_dcpa_value, self.intr_dcpa_range, "upper [NM] (optional)"),
        )

        self.intr_tlosh_value = QLineEdit("120.0")
        self.intr_tlosh_value.setClearButtonEnabled(True)
        self.intr_tlosh_range = QLineEdit()
        self.intr_tlosh_range.setClearButtonEnabled(True)
        intr_form.addRow(
            "Horizontal TL tlosh [s]:",
            self._make_numeric_row(self.intr_tlosh_value, self.intr_tlosh_range, "upper [s] (optional)"),
        )

        self.intr_dh_value = QLineEdit("0")
        self.intr_dh_value.setClearButtonEnabled(True)
        self.intr_dh_range = QLineEdit()
        self.intr_dh_range.setClearButtonEnabled(True)
        intr_form.addRow(
            "Vertical offset dH [ft] (optional):",
            self._make_numeric_row(self.intr_dh_value, self.intr_dh_range, "upper [ft] (optional)"),
        )

        self.intr_tlosv_value = QLineEdit("0")
        self.intr_tlosv_value.setClearButtonEnabled(True)
        self.intr_tlosv_range = QLineEdit()
        self.intr_tlosv_range.setClearButtonEnabled(True)
        self.intr_tlosv_value.setToolTip("0 uses horizontal TL")
        intr_form.addRow(
            "Vertical TL tlosv [s] (optional):",
            self._make_numeric_row(self.intr_tlosv_value, self.intr_tlosv_range, "upper [s] (optional)"),
        )
        spd_row = QWidget()
        spd_layout = QHBoxLayout(spd_row)
        spd_layout.setContentsMargins(0, 0, 0, 0)
        spd_layout.setSpacing(6)
        self.intr_spd_value = QLineEdit("")
        self.intr_spd_value.setClearButtonEnabled(True)
        self.intr_spd_value.setPlaceholderText("value (e.g. 250 or M0.78)")
        self.intr_spd_range = QLineEdit("")
        self.intr_spd_range.setClearButtonEnabled(True)
        self.intr_spd_range.setPlaceholderText("upper (optional)")
        spd_layout.addWidget(self.intr_spd_value)
        spd_layout.addWidget(self.intr_spd_range, 1)
        intr_form.addRow("CAS/Mach (optional):", spd_row)
        
        # Set the form widget as the scroll area's widget
        intr_scroll.setWidget(intr_form_widget)
        
        # Add the scroll area to the group box
        intr_box_layout.addWidget(intr_scroll)
        main.addWidget(intr_box)

        scen_box = QGroupBox("4) Scenario output (write mode)")
        scen_form = QFormLayout(scen_box)
        self.scn_name = QLineEdit("gc_relative")
        scen_form.addRow("Scenario name:", self.scn_name)
        
        # Add seed field like CPA has
        self.gc_rel_seed = QSpinBox()
        self.gc_rel_seed.setRange(0, 2_000_000_000)
        self.gc_rel_seed.setValue(0)
        scen_form.addRow("Seed (0 = random):", self.gc_rel_seed)
        
        self.overwrite_cb = QCheckBox("Overwrite scenario if it exists")
        scen_form.addRow(self.overwrite_cb)
        main.addWidget(scen_box)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(10)
        self.btn_write = QPushButton("CREATE SCENARIO")
        self.btn_run = QPushButton("RUN SCENARIO")
        self.btn_both = QPushButton("CREATE & RUN SCENARIO")
        btn_layout.addWidget(self.btn_write)
        btn_layout.addWidget(self.btn_run)
        btn_layout.addWidget(self.btn_both)
        btn_layout.addStretch(1)
        main.addWidget(btn_row)
        main.addStretch(1)

        self._target_detail_widgets = [
            self.target_acid,
            self.target_type,
            self.target_lat_value,
            self.target_lat_range,
            self.target_lon_value,
            self.target_lon_range,
            self.target_hdg_value,
            self.target_hdg_range,
            self.target_alt_value,
            self.target_alt_range,
            self.target_spd_value,
            self.target_spd_range,
        ]

        self.refresh_btn.clicked.connect(self._refresh_targets)
        self.include_target_cb.toggled.connect(self._update_target_field_state)
        self.btn_write.clicked.connect(self._gc_rel_create)
        self.btn_run.clicked.connect(self._gc_rel_run_only)
        self.btn_both.clicked.connect(self._gc_rel_create_and_run)

        self._refresh_targets()
        self._update_target_field_state()

    def _emit_conf(self):
        _emit(f"SATG_GC_CONF {self._minima.hsep_value()} {self._minima.vsep_value()}")

    def _combo_target_id(self) -> str:
        data = self.target_combo.currentData()
        if isinstance(data, str) and data:
            return data
        txt = self.target_combo.currentText().strip()
        return txt.split()[0] if txt else ""

    def _current_target_id(self) -> str:
        if self.include_target_cb.isChecked():
            return self.target_acid.text().strip().upper()
        return self._combo_target_id().upper()

    def _gc_rel_create(self):
        """Create scenario file only (like CPA _gc_create)"""
        self._emit_gc_rel()

    def _gc_rel_create_and_run(self):
        """Create scenario file and run it (like CPA _gc_create_and_run)"""
        self._gc_rel_create()
        self._gc_rel_run_only()

    def _gc_rel_run_only(self):
        """Run existing scenario (like CPA _gc_run_only)"""
        name = self.scn_name.text().strip()
        if name:
            _emit("SATG_GC_RUN " + name)

    def _emit_gc_rel(self):
        """Send the SATG_GC_REL command with write mode and overwrite parameter"""
        cmd = self._build_command()
        if not cmd:
            return
        # Send aircraft types separately before the main command (like GC Absolute does)
        self._emit_gc_types()
        self._emit_conf()
        _emit(cmd)

    def _emit_gc_types(self):
        """Send SATG_GC_TYPES command with aircraft types from the intruder type field."""
        raw = self.intr_type.text().strip()
        if not raw:
            _emit("SATG_GC_TYPES")
            return
        parts = [seg.strip().upper() for seg in re.split(r"[,\s]+", raw.replace("|", " ")) if seg.strip()]
        if not parts:
            _emit("SATG_GC_TYPES")
            return
        cmd = "SATG_GC_TYPES " + " ".join(parts)
        _emit(cmd)

    def _build_command(self) -> Optional[str]:
        include_target = self.include_target_cb.isChecked()
        target_id = self._current_target_id()
        if not target_id and not include_target:
            _emit("ECHO SATGGUI: Select or define a target aircraft first.")
            return None

        # Always use write mode like CPA does
        tokens = ["SATG_GC_REL", _kv("mode", "write")]
        if target_id:
            tokens.append(_kv("target", target_id))

        intr_acid = self.intr_acid.text().strip().upper()
        if intr_acid:
            tokens.append(_kv("acid", intr_acid))

        dpsi_field = self._extract_numeric_pair(
            "Conflict angle dpsi [deg]",
            self.intr_dpsi_value,
            self.intr_dpsi_range,
            allow_float=True,
            allow_negative=True,
            min_value=-180.0,
            max_value=180.0,
        )
        if dpsi_field is None:
            return None
        tokens.append(_kv("dpsi", dpsi_field[0]))

        dcpa_field = self._extract_numeric_pair(
            "CPA distance dcpa [NM]",
            self.intr_dcpa_value,
            self.intr_dcpa_range,
            allow_float=True,
            allow_negative=False,
            min_value=0.0001,
        )
        if dcpa_field is None:
            return None
        tokens.append(_kv("dcpa", dcpa_field[0]))

        tlosh_field = self._extract_numeric_pair(
            "Horizontal TL tlosh [s]",
            self.intr_tlosh_value,
            self.intr_tlosh_range,
            allow_float=True,
            allow_negative=False,
            min_value=0.0001,
        )
        if tlosh_field is None:
            return None
        tokens.append(_kv("tlosh", tlosh_field[0]))

        dh_field = self._extract_numeric_pair(
            "Vertical offset dH [ft]",
            self.intr_dh_value,
            self.intr_dh_range,
            allow_float=False,
            allow_negative=True,
            allow_empty=True,
            default_value=0.0,
        )
        if dh_field is None:
            return None
        dh_txt, dh_is_range, dh_lo, _ = dh_field
        if dh_is_range or abs(dh_lo) >= 1.0:
            tokens.append(_kv("dh", dh_txt))

        tlosv_field = self._extract_numeric_pair(
            "Vertical TL tlosv [s]",
            self.intr_tlosv_value,
            self.intr_tlosv_range,
            allow_float=True,
            allow_negative=False,
            min_value=0.0,
            allow_empty=True,
            default_value=0.0,
        )
        if tlosv_field is None:
            return None
        tlosv_txt, tlosv_is_range, _, tlosv_hi = tlosv_field
        if tlosv_is_range or tlosv_hi > 0.0:
            tokens.append(_kv("tlosv", tlosv_txt))

        spd_value_txt = self.intr_spd_value.text().strip()
        spd_range_txt = self.intr_spd_range.text().strip()
        if spd_range_txt and not spd_value_txt:
            _emit("ECHO CAS/Mach: fill the left field before setting an upper bound.")
            return None
        if spd_value_txt:
            try:
                base_norm, base_val, base_is_mach = self._normalize_speed_text(spd_value_txt)
            except ValueError as exc:
                _emit(f"ECHO {exc}")
                return None
            if spd_range_txt:
                try:
                    _, range_val, range_is_mach = self._normalize_speed_text(spd_range_txt)
                except ValueError as exc:
                    _emit(f"ECHO {exc}")
                    return None
                if base_is_mach != range_is_mach:
                    _emit("ECHO CAS/Mach range must use the same units (both CAS or both Mach).")
                    return None
                lo_val = min(base_val, range_val)
                hi_val = max(base_val, range_val)
                if base_is_mach:
                    lo_txt = self._format_mach(lo_val)
                    hi_txt = self._format_mach(hi_val)
                else:
                    lo_txt = self._format_numeric(lo_val, allow_float=True)
                    hi_txt = self._format_numeric(hi_val, allow_float=True)
                spd_token = f"{lo_txt}:{hi_txt}" if hi_val > lo_val else lo_txt
            else:
                spd_token = base_norm
            if spd_token:
                tokens.append(_kv("spd", spd_token))

        if include_target:
            lat_field = self._extract_numeric_pair(
                "Target latitude [deg]",
                self.target_lat_value,
                self.target_lat_range,
                allow_float=True,
                allow_negative=True,
                min_value=-90.0,
                max_value=90.0,
            )
            if lat_field is None:
                return None
            lon_field = self._extract_numeric_pair(
                "Target longitude [deg]",
                self.target_lon_value,
                self.target_lon_range,
                allow_float=True,
                allow_negative=True,
                min_value=-360.0,
                max_value=360.0,
            )
            if lon_field is None:
                return None
            hdg_field = self._extract_numeric_pair(
                "Target heading [deg]",
                self.target_hdg_value,
                self.target_hdg_range,
                allow_float=True,
                allow_negative=False,
                min_value=0.0,
                max_value=360.0,
            )
            if hdg_field is None:
                return None
            alt_field = self._extract_numeric_pair(
                "Target altitude [ft]",
                self.target_alt_value,
                self.target_alt_range,
                allow_float=False,
                allow_negative=False,
                min_value=0.0,
            )
            if alt_field is None:
                return None
            spd_field = self._extract_numeric_pair(
                "Target CAS [kt]",
                self.target_spd_value,
                self.target_spd_range,
                allow_float=True,
                allow_negative=False,
                min_value=0.0,
            )
            if spd_field is None:
                return None
            tokens.append(_kv("include_target", 1))
            target_acid_txt = self.target_acid.text().strip().upper()
            target_type_txt = self.target_type.text().strip().upper()
            tokens.append(_kv("target_acid", target_acid_txt or None))
            tokens.append(_kv("target_type", target_type_txt or None))
            tokens.append(_kv("target_lat", lat_field[0]))
            tokens.append(_kv("target_lon", lon_field[0]))
            tokens.append(_kv("target_hdg", hdg_field[0]))
            tokens.append(_kv("target_alt_ft", alt_field[0]))
            tokens.append(_kv("target_spd", spd_field[0]))

        # Always include scenario name and overwrite flag (like CPA does)
        name_txt = self.scn_name.text().strip()
        if not name_txt:
            _emit("ECHO SATGGUI: Provide a scenario name before writing.")
            return None
        tokens.append(_kv("name", name_txt))
        tokens.append(_kv("overwrite", 1 if self.overwrite_cb.isChecked() else 0))
        
        # Add seed parameter like CPA does
        seed_val = int(self.gc_rel_seed.value())
        if seed_val > 0:
            tokens.append(_kv("seed", seed_val))

        return _join_tokens(*tokens)

    def _refresh_targets(self):
        self.target_combo.clear()
        traf = getattr(bs, "traf", None) if bs else None
        added = False
        try:
            if traf and getattr(traf, "ntraf", 0) > 0:
                for idx in range(traf.ntraf):
                    acid = str(traf.id[idx]).upper()
                    if not acid:
                        continue
                    self.target_combo.addItem(acid, acid)
                    added = True
        except Exception:
            added = False
        if not added:
            self.target_combo.addItem("(no active aircraft)", "")

    def _update_target_field_state(self):
        enable = self.include_target_cb.isChecked()
        for widget in self._target_detail_widgets:
            widget.setEnabled(enable)

    def _make_numeric_row(self, value_widget: QLineEdit, range_widget: QLineEdit, placeholder: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        value_widget.setMaximumWidth(90)
        value_widget.setPlaceholderText("required")
        value_widget.setToolTip("Required value (used as deterministic or lower bound).")
        range_widget.setPlaceholderText(placeholder)
        range_widget.setToolTip("Optional upper bound; leave empty for a fixed value.")
        range_widget.setMinimumWidth(140)
        layout.addWidget(value_widget)
        layout.addWidget(range_widget, 1)
        return row

    def _format_mach(self, value: float) -> str:
        txt = f"{float(value):.3f}".rstrip("0").rstrip(".")
        if not txt or txt == "0":
            txt = "0"
        if txt.startswith("."):
            txt = "0" + txt
        return f"M{txt}"

    def _normalize_speed_text(self, text: str) -> Tuple[str, float, bool]:
        raw = text.strip()
        if not raw:
            raise ValueError("CAS/Mach value is empty.")
        compact = raw.replace(" ", "")
        upper = compact.upper()
        if upper.startswith("M"):
            num_txt = upper[1:]
            if not num_txt:
                raise ValueError("Mach value must include digits after the 'M' prefix.")
            try:
                value = float(num_txt)
            except ValueError as exc:
                raise ValueError("Mach value must be numeric, e.g. M0.78.") from exc
            if value <= 0.0:
                raise ValueError("Mach value must be greater than zero.")
            return self._format_mach(value), value, True
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("CAS value must be numeric knots or use the 'M' prefix for Mach.") from exc
        if value <= 0.0:
            raise ValueError("CAS value must be greater than zero.")
        return self._format_numeric(value, allow_float=True), value, False

    def _extract_numeric_pair(
        self,
        label: str,
        primary: QLineEdit,
        secondary: QLineEdit,
        *,
        allow_float: bool,
        allow_negative: bool,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        allow_empty: bool = False,
        default_value: float = 0.0,
    ) -> Optional[Tuple[str, bool, float, float]]:
        primary_txt = primary.text().strip()
        hi_txt = secondary.text().strip()
        if not primary_txt:
            if allow_empty and not hi_txt:
                try:
                    lo_val = self._coerce_numeric(
                        str(default_value),
                        label,
                        "value",
                        allow_float,
                        allow_negative,
                        min_value,
                        max_value,
                    )
                except ValueError as exc:
                    _emit(f"ECHO {exc}")
                    return None
                normalized = self._format_numeric(lo_val, allow_float)
                return normalized, False, lo_val, lo_val
            _emit(f"ECHO SATGGUI: {label} requires a value.")
            return None
        try:
            lo_val = self._coerce_numeric(
                primary_txt,
                label,
                "value",
                allow_float,
                allow_negative,
                min_value,
                max_value,
            )
        except ValueError as exc:
            _emit(f"ECHO {exc}")
            return None
        if hi_txt:
            try:
                hi_val = self._coerce_numeric(
                    hi_txt,
                    label,
                    "upper bound",
                    allow_float,
                    allow_negative,
                    min_value,
                    max_value,
                )
            except ValueError as exc:
                _emit(f"ECHO {exc}")
                return None
            lo, hi = (lo_val, hi_val) if hi_val >= lo_val else (hi_val, lo_val)
            normalized = f"{self._format_numeric(lo, allow_float)}:{self._format_numeric(hi, allow_float)}"
            return normalized, True, lo, hi

        normalized = self._format_numeric(lo_val, allow_float)
        return normalized, False, lo_val, lo_val

    def _coerce_numeric(
        self,
        text: str,
        label: str,
        desc: str,
        allow_float: bool,
        allow_negative: bool,
        min_value: Optional[float],
        max_value: Optional[float],
    ) -> float:
        stripped = text.strip()
        if not stripped:
            raise ValueError(f"{label} {desc} is empty.")
        try:
            value = float(stripped)
        except ValueError as exc:
            raise ValueError(f"{label} {desc} must be numeric.") from exc
        if not allow_negative and value < 0.0:
            raise ValueError(f"{label} {desc} must be >= 0.")
        if min_value is not None and value < min_value:
            raise ValueError(f"{label} {desc} must be >= {min_value}.")
        if max_value is not None and value > max_value:
            raise ValueError(f"{label} {desc} must be <= {max_value}.")
        if allow_float:
            return value
        return float(int(round(value)))

    def _format_numeric(self, value: float, allow_float: bool) -> str:
        if allow_float:
            text = f"{float(value):.6f}".rstrip("0").rstrip(".")
            return text if text and text != "-0" else "0"
        return str(int(round(value)))


class GCTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(10)

        self._minima = GCMinimaPanel(self)
        main.addWidget(self._minima)

        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(12)

        abs_box = QGroupBox("CPA (legacy)", self)
        abs_layout = QVBoxLayout(abs_box)
        abs_layout.setContentsMargins(8, 8, 8, 8)
        self._absolute_page = GCAbsolutePage(self._minima, abs_box)  # Store reference
        abs_layout.addWidget(self._absolute_page)

        rel_box = QGroupBox("Relative (creconfs)", self)
        rel_layout = QVBoxLayout(rel_box)
        rel_layout.setContentsMargins(8, 8, 8, 8)
        rel_layout.addWidget(GCRelativePage(self._minima, rel_box))

        cols.addWidget(abs_box, 1)
        cols.addWidget(rel_box, 1)

        main.addLayout(cols)


# --- RC tab (Random Conflicts) ---------------------------------------------

class RCTab(QWidget):
    """Random Conflicts (RC) — Modern geometric conflicts in a circle region."""
    def __init__(self, parent=None):
        super().__init__(parent)
        
        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(10)

        # Common settings section
        common_gb = QGroupBox("1) Batch Settings")
        common_layout = QVBoxLayout(common_gb)
        
        # Create a scroll area for common settings
        common_scroll = QScrollArea()
        common_scroll.setWidgetResizable(True)
        common_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        common_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        common_scroll.setMaximumHeight(300)  # Increased height for better visibility
        
        common_form_widget = QWidget()
        common_form = QFormLayout(common_form_widget)
        common_form.setContentsMargins(5, 5, 5, 5)

        self.n = QSpinBox(); self.n.setRange(1, 100000); self.n.setValue(20)
        
        # Circle region settings
        self.c_lat = QLineEdit("52.100")
        self.c_lon = QLineEdit("4.500")
        self.c_rad = QDoubleSpinBox(); self.c_rad.setRange(0.1, 1000.0); self.c_rad.setDecimals(2); self.c_rad.setValue(25.0)
        
        # Separation minima
        self.hsep = QDoubleSpinBox(); self.hsep.setRange(0.0, 50.0); self.hsep.setDecimals(2); self.hsep.setValue(5.0)
        self.vsep = QSpinBox(); self.vsep.setRange(0, 5000); self.vsep.setValue(1000)

        common_form.addRow("Number of conflicts:", self.n)
        
        # Area type selection
        self.area_type_group = QGroupBox("Conflict Area")
        area_type_layout = QVBoxLayout(self.area_type_group)
        
        # Radio buttons for area type
        self.circle_rb = QRadioButton("Circle")
        self.polygon_rb = QRadioButton("Polygon")
        self.circle_rb.setChecked(True)  # Default to circle
        
        area_type_layout.addWidget(self.circle_rb)
        area_type_layout.addWidget(self.polygon_rb)
        
        # Connect radio buttons to update visibility
        self.circle_rb.toggled.connect(self._update_area_controls)
        self.polygon_rb.toggled.connect(self._update_area_controls)
        
        common_form.addRow(self.area_type_group)
        
        # Circle controls (initially visible)
        self.circle_controls = QWidget()
        circle_layout = QFormLayout(self.circle_controls)
        circle_layout.setContentsMargins(0, 0, 0, 0)
        circle_layout.addRow("Circle center lat [deg]:", self.c_lat)
        circle_layout.addRow("Circle center lon [deg]:", self.c_lon)
        circle_layout.addRow("Circle radius [NM]:", self.c_rad)
        common_form.addRow(self.circle_controls)
        
        # Polygon controls (initially hidden)
        self.polygon_controls = QWidget()
        polygon_layout = QFormLayout(self.polygon_controls)
        polygon_layout.setContentsMargins(0, 0, 0, 0)
        
        # Polygon name text input
        self.polygon_name_input = QLineEdit()
        self.polygon_name_input.setPlaceholderText("Enter polygon name (e.g., myarea)")
        self.polygon_name_input.setToolTip("Enter the name of the polygon you created with POLY command\nPress Enter to start creating polygon on screen")
        self.polygon_name_input.returnPressed.connect(self._start_polygon_creation)
        polygon_layout.addRow("Polygon Name:", self.polygon_name_input)
        
        common_form.addRow(self.polygon_controls)
        self.polygon_controls.hide()  # Initially hidden
        
        # Circle visualization option
        self.show_circle_cb = QCheckBox("Show circle on screen")
        self.show_circle_cb.setChecked(False)
        self.show_circle_cb.setToolTip("Draw the circle region on BlueSky screen for visual reference")
        self.show_circle_cb.toggled.connect(self._toggle_circle_display)
        common_form.addRow(self.show_circle_cb)
        
        common_form.addRow("HSEP [NM]:", self.hsep)
        common_form.addRow("VSEP [ft]:", self.vsep)

        common_scroll.setWidget(common_form_widget)
        common_layout.addWidget(common_scroll)
        main.addWidget(common_gb)

        # Two-column layout for conflict modes
        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(12)

        # Absolute conflicts column
        abs_box = QGroupBox("2) Absolute Conflicts (CPA-based)")
        abs_layout = QVBoxLayout(abs_box)
        abs_layout.setContentsMargins(8, 8, 8, 8)
        
        self.abs_enabled = QCheckBox("Enable absolute conflicts")
        self.abs_enabled.setChecked(True)
        abs_layout.addWidget(self.abs_enabled)
        
        # Absolute settings scroll area
        abs_scroll = QScrollArea()
        abs_scroll.setWidgetResizable(True)
        abs_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        abs_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        abs_scroll.setMaximumHeight(350)
        
        abs_form_widget = QWidget()
        abs_form = QFormLayout(abs_form_widget)
        abs_form.setContentsMargins(5, 5, 5, 5)

        # TCPA using two-field format like geometric conflicts
        self.abs_tcpa_value = QLineEdit("120.0")
        self.abs_tcpa_value.setClearButtonEnabled(True)
        self.abs_tcpa_range = QLineEdit()
        self.abs_tcpa_range.setClearButtonEnabled(True)
        abs_form.addRow(
            "TCPA [s]:",
            self._make_value_range_row(self.abs_tcpa_value, self.abs_tcpa_range, "upper [s] (optional)"),
        )

        # CPA angle using two-field format
        self.abs_angle_value = QLineEdit("90.0")
        self.abs_angle_value.setClearButtonEnabled(True)
        self.abs_angle_range = QLineEdit()
        self.abs_angle_range.setClearButtonEnabled(True)
        abs_form.addRow(
            "CPA angle [deg]:",
            self._make_value_range_row(self.abs_angle_value, self.abs_angle_range, "upper [deg] (optional)"),
        )

        # Altitude offset using two-field format
        self.abs_alt_offset_value = QLineEdit("0")
        self.abs_alt_offset_value.setClearButtonEnabled(True)
        self.abs_alt_offset_range = QLineEdit()
        self.abs_alt_offset_range.setClearButtonEnabled(True)
        abs_form.addRow(
            "Altitude offset dH [ft] (optional):",
            self._make_value_range_row(self.abs_alt_offset_value, self.abs_alt_offset_range, "upper [ft] (optional)"),
        )

        # Flight level using two-field format
        self.abs_fl_value = QLineEdit("310")
        self.abs_fl_value.setClearButtonEnabled(True)
        self.abs_fl_range = QLineEdit()
        self.abs_fl_range.setClearButtonEnabled(True)
        abs_form.addRow(
            "Flight level:",
            self._make_value_range_row(self.abs_fl_value, self.abs_fl_range, "upper FL (optional)"),
        )

        # CAS using two-field format
        self.abs_cas_value = QLineEdit("250")
        self.abs_cas_value.setClearButtonEnabled(True)
        self.abs_cas_range = QLineEdit()
        self.abs_cas_range.setClearButtonEnabled(True)
        abs_form.addRow(
            "CAS [kt]:",
            self._make_value_range_row(self.abs_cas_value, self.abs_cas_range, "upper [kt] (optional)"),
        )

        self.abs_actypes = QLineEdit("A320,B738,A350,B78X")

        abs_form.addRow("Aircraft types:", self.abs_actypes)

        abs_scroll.setWidget(abs_form_widget)
        abs_layout.addWidget(abs_scroll)

        # Relative conflicts column
        rel_box = QGroupBox("3) Relative Conflicts (Target-Intruder)")
        rel_layout = QVBoxLayout(rel_box)
        rel_layout.setContentsMargins(8, 8, 8, 8)
        
        self.rel_enabled = QCheckBox("Enable relative conflicts")
        self.rel_enabled.setChecked(False)
        rel_layout.addWidget(self.rel_enabled)
        
        # Relative settings scroll area
        rel_scroll = QScrollArea()
        rel_scroll.setWidgetResizable(True)
        rel_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rel_scroll.setMaximumHeight(350)
        
        rel_form_widget = QWidget()
        rel_form = QFormLayout(rel_form_widget)
        rel_form.setContentsMargins(5, 5, 5, 5)

        # Aircraft type field (copied from geometric conflicts intruder setup)
        self.rel_type = QLineEdit("A320,B738,A350,B78X")
        self.rel_type.setClearButtonEnabled(True)
        rel_form.addRow("Aircraft type:", self.rel_type)

        # Conflict angle dpsi (copied from geometric conflicts intruder setup)
        self.rel_dpsi_value = QLineEdit("90.0")
        self.rel_dpsi_value.setClearButtonEnabled(True)
        self.rel_dpsi_range = QLineEdit()
        self.rel_dpsi_range.setClearButtonEnabled(True)
        rel_form.addRow(
            "Conflict angle dpsi [deg]:",
            self._make_value_range_row(self.rel_dpsi_value, self.rel_dpsi_range, "upper [deg] (optional)"),
        )

        # CPA distance dcpa (copied from geometric conflicts intruder setup)
        self.rel_dcpa_value = QLineEdit("2.0")
        self.rel_dcpa_value.setClearButtonEnabled(True)
        self.rel_dcpa_range = QLineEdit()
        self.rel_dcpa_range.setClearButtonEnabled(True)
        rel_form.addRow(
            "CPA distance dcpa [NM]:",
            self._make_value_range_row(self.rel_dcpa_value, self.rel_dcpa_range, "upper [NM] (optional)"),
        )

        # Horizontal TL tlosh (copied from geometric conflicts intruder setup)
        self.rel_tlosh_value = QLineEdit("120.0")
        self.rel_tlosh_value.setClearButtonEnabled(True)
        self.rel_tlosh_range = QLineEdit()
        self.rel_tlosh_range.setClearButtonEnabled(True)
        rel_form.addRow(
            "Horizontal TL tlosh [s]:",
            self._make_value_range_row(self.rel_tlosh_value, self.rel_tlosh_range, "upper [s] (optional)"),
        )

        # Vertical offset dH (copied from geometric conflicts intruder setup)
        self.rel_dh_value = QLineEdit("0")
        self.rel_dh_value.setClearButtonEnabled(True)
        self.rel_dh_range = QLineEdit()
        self.rel_dh_range.setClearButtonEnabled(True)
        rel_form.addRow(
            "Vertical offset dH [ft] (optional):",
            self._make_value_range_row(self.rel_dh_value, self.rel_dh_range, "upper [ft] (optional)"),
        )

        # Vertical TL tlosv (copied from geometric conflicts intruder setup)
        self.rel_tlosv_value = QLineEdit("0")
        self.rel_tlosv_value.setClearButtonEnabled(True)
        self.rel_tlosv_range = QLineEdit()
        self.rel_tlosv_range.setClearButtonEnabled(True)
        self.rel_tlosv_value.setToolTip("0 uses horizontal TL")
        rel_form.addRow(
            "Vertical TL tlosv [s] (optional):",
            self._make_value_range_row(self.rel_tlosv_value, self.rel_tlosv_range, "upper [s] (optional)"),
        )

        # CAS/Mach speed (copied from geometric conflicts intruder setup)
        rel_spd_row = QWidget()
        rel_spd_layout = QHBoxLayout(rel_spd_row)
        rel_spd_layout.setContentsMargins(0, 0, 0, 0)
        rel_spd_layout.setSpacing(6)
        self.rel_spd_value = QLineEdit("")
        self.rel_spd_value.setClearButtonEnabled(True)
        self.rel_spd_value.setPlaceholderText("value (e.g. 250 or M0.78)")
        self.rel_spd_range = QLineEdit("")
        self.rel_spd_range.setClearButtonEnabled(True)
        self.rel_spd_range.setPlaceholderText("upper (optional)")
        rel_spd_layout.addWidget(self.rel_spd_value)
        rel_spd_layout.addWidget(self.rel_spd_range, 1)
        rel_form.addRow("CAS/Mach (optional):", rel_spd_row)

        # Flight level/altitude fields to match absolute conflicts format
        self.rel_fl_value = QLineEdit("310")
        self.rel_fl_value.setClearButtonEnabled(True)
        self.rel_fl_range = QLineEdit()
        self.rel_fl_range.setClearButtonEnabled(True)
        rel_form.addRow(
            "Flight level:",
            self._make_value_range_row(self.rel_fl_value, self.rel_fl_range, "upper FL (optional)"),
        )

        rel_scroll.setWidget(rel_form_widget)
        rel_layout.addWidget(rel_scroll)

        cols.addWidget(abs_box, 1)
        cols.addWidget(rel_box, 1)
        main.addLayout(cols)

        # Actions
        actions_gb = QGroupBox("4) Create Scenario")
        actions_main_layout = QVBoxLayout(actions_gb)
        actions_main_layout.setContentsMargins(8, 8, 8, 8)
        actions_main_layout.setSpacing(10)
        
        # Scenario controls form
        scenario_form = QFormLayout()
        scenario_form.setContentsMargins(0, 0, 0, 0)
        scenario_form.setSpacing(8)
        
        # Add the scenario controls that were moved from batch settings
        self.scn = QLineEdit("rc_circle")
        self.scn.textChanged.connect(self._update_circle_if_visible)  # Update circle name when scenario name changes
        self.seed = QSpinBox(); self.seed.setRange(0, 2**31-1); self.seed.setValue(0)
        self.gc_overwrite_cb = QCheckBox("Overwrite scenario if it exists")
        self.gc_overwrite_cb.setChecked(False)
        
        self.include_polygon_cb = QCheckBox("Include polygon in scenario file")
        self.include_polygon_cb.setChecked(True)
        self.include_polygon_cb.setToolTip("When using polygon areas, automatically include POLY command in scenario file")
        self.include_polygon_cb.hide()  # Initially hidden since circle is selected by default
        
        scenario_form.addRow("Scenario name:", self.scn)
        scenario_form.addRow("Seed (0=random):", self.seed)
        scenario_form.addRow(self.gc_overwrite_cb)
        scenario_form.addRow(self.include_polygon_cb)
        
        actions_main_layout.addLayout(scenario_form)
        
        # Action buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)
        
        self.btn_create = QPushButton("CREATE SCENARIO")
        self.btn_run = QPushButton("RUN SCENARIO")
        self.btn_both = QPushButton("CREATE & RUN SCENARIO")
        self.btn_create.clicked.connect(self._create)
        self.btn_run.clicked.connect(self._run)
        self.btn_both.clicked.connect(self._create_and_run)
        buttons_layout.addWidget(self.btn_create)
        buttons_layout.addWidget(self.btn_run)
        buttons_layout.addWidget(self.btn_both)
        buttons_layout.addStretch(1)
        
        actions_main_layout.addLayout(buttons_layout)

        main.addWidget(actions_gb)
        main.addStretch(1)
        
        # Connect parameter change signals to update circle display
        self.c_lat.textChanged.connect(self._update_circle_if_visible)
        self.c_lon.textChanged.connect(self._update_circle_if_visible)
        self.c_rad.valueChanged.connect(self._update_circle_if_visible)
        
        # Set initial visibility state for area controls (must be after all widgets are created)
        self._update_area_controls()

    def _start_polygon_creation(self):
        """Start polygon creation by pre-filling the command line with POLY command."""
        polygon_name = self.polygon_name_input.text().strip()
        if polygon_name:
            # Pre-fill the command line with "POLY polygonname " (note the trailing space)
            process_cmdline(f"POLY {polygon_name} ")
        else:
            _emit("ECHO Please enter a polygon name first")

    def _toggle_circle_display(self):
        """Show or hide the circle on the BlueSky screen."""
        if self.show_circle_cb.isChecked():
            self._show_circle()
        else:
            self._hide_circle()
            
    def _update_circle_if_visible(self):
        """Update circle display if it's currently visible."""
        if hasattr(self, 'show_circle_cb') and self.show_circle_cb.isChecked():
            self._show_circle()
            
    def _show_circle(self):
        """Draw the circle on the BlueSky screen."""
        try:
            lat = float(self.c_lat.text())
            lon = float(self.c_lon.text())
            radius = float(self.c_rad.value())
            
            # Use the CIRCLE command to draw the circle
            # Format: CIRCLE name lat lon radius_nm
            circle_name = f"RC_CIRCLE_{self.scn.text()}"
            _emit(f"CIRCLE {circle_name} {lat} {lon} {radius}")
            
        except (ValueError, AttributeError):
            # Invalid coordinates, don't draw anything
            pass
            
    def _hide_circle(self):
        """Remove the circle from the BlueSky screen."""
        # Hide the circle by drawing it with radius 0 (invisible)
        try:
            lat = float(self.c_lat.text())
            lon = float(self.c_lon.text())
            circle_name = f"RC_CIRCLE_{self.scn.text()}"
            _emit(f"CIRCLE {circle_name} {lat} {lon} 0")  # Radius 0 makes it invisible
        except (ValueError, AttributeError):
            # If we can't get coordinates, try with default coords
            circle_name = f"RC_CIRCLE_{self.scn.text()}"
            _emit(f"CIRCLE {circle_name} 0 0 0")

    def _update_area_controls(self):
        """Show/hide area controls based on selected area type."""
        if self.circle_rb.isChecked():
            self.circle_controls.show()
            self.polygon_controls.hide()
            self.show_circle_cb.show()  # Show circle visualization option for circle mode
            self.include_polygon_cb.hide()  # Hide polygon checkbox for circle mode
        else:  # polygon selected
            self.polygon_controls.show()
            self.circle_controls.hide()
            self.show_circle_cb.hide()  # Hide circle visualization option for polygon mode
            self.include_polygon_cb.show()  # Show polygon checkbox for polygon mode

    def _create(self):
        """Create random conflicts using modern geometric conflicts commands."""
        # Validate inputs
        if not self.abs_enabled.isChecked() and not self.rel_enabled.isChecked():
            _emit("ECHO SATGGUI: Enable at least one conflict mode (Absolute or Relative).")
            return
            
        if self.abs_enabled.isChecked() and not self._validate_abs_settings():
            return
            
        name = self.scn.text().strip()
        if not name:
            _emit("ECHO SATGGUI: Enter a scenario name.")
            return
            
        # Validate area parameters based on selected type
        if self.circle_rb.isChecked():
            # Circle mode - validate coordinates and radius
            try:
                center_lat = float(self.c_lat.text())
                center_lon = float(self.c_lon.text())
                radius = float(self.c_rad.value())
            except ValueError:
                _emit("ECHO SATGGUI: Invalid circle parameters.")
                return
            area_params = {"area_type": "circle", "center_lat": center_lat, "center_lon": center_lon, "radius": radius}
        else:
            # Polygon mode - validate polygon name input
            polygon_name = self.polygon_name_input.text().strip()
            if not polygon_name:
                _emit("ECHO SATGGUI: Enter a polygon name. Create polygon first with POLY command.")
                return
            area_params = {"area_type": "polygon", "polygon_name": polygon_name}

        # Set separation minima
        _emit(f"SATG_GC_CONF {self.hsep.value()} {self.vsep.value()}")
        
        # Count conflicts to generate
        total_conflicts = self.n.value()
        overwrite = 1 if self.gc_overwrite_cb.isChecked() else 0
        
        if self.abs_enabled.isChecked() and self.rel_enabled.isChecked():
            # Use random mode like backend - let SATG_RC_CIRCLE decide randomly for each conflict
            self._create_mixed_conflicts(name, total_conflicts, area_params, overwrite)
        elif self.abs_enabled.isChecked():
            self._create_absolute_conflicts(name, total_conflicts, area_params, overwrite)
        elif self.rel_enabled.isChecked():
            self._create_relative_conflicts(name, total_conflicts, area_params, overwrite)

    def _validate_abs_settings(self) -> bool:
        """Check if absolute conflict settings are valid."""
        # For modern geometric conflicts, no specific validation needed beyond enabled state
        return True

    def _create_absolute_conflicts(self, name: str, count: int, area_params: dict, overwrite: int):
        """Generate absolute (CPA-based) conflicts in the specified area."""
        
        # Extract and validate TCPA field
        tcpa_field = self._extract_numeric_field("TCPA [s]", self.abs_tcpa_value, self.abs_tcpa_range, allow_float=True, allow_negative=False)
        if tcpa_field is None:
            return
        tcpa_txt, _, tcpa_lo, tcpa_hi = tcpa_field

        # Extract and validate CPA angle field  
        angle_field = self._extract_numeric_field("CPA angle [deg]", self.abs_angle_value, self.abs_angle_range, allow_float=True, allow_negative=False)
        if angle_field is None:
            return
        angle_txt, _, angle_lo, angle_hi = angle_field

        # Extract and validate altitude offset field (optional)
        alt_offset_txt = ""
        if self.abs_alt_offset_value.text().strip() or self.abs_alt_offset_range.text().strip():
            alt_offset_field = self._extract_numeric_field("Altitude offset [ft]", self.abs_alt_offset_value, self.abs_alt_offset_range, allow_float=True, allow_negative=True)
            if alt_offset_field is None:
                return
            alt_offset_txt, _, _, _ = alt_offset_field

        # Extract and validate flight level field
        fl_field = self._extract_numeric_field("Flight level", self.abs_fl_value, self.abs_fl_range, allow_float=False, allow_negative=False)
        if fl_field is None:
            return
        fl_txt, _, _, _ = fl_field

        # Extract and validate CAS field
        cas_field = self._extract_numeric_field("CAS [kt]", self.abs_cas_value, self.abs_cas_range, allow_float=True, allow_negative=False)
        if cas_field is None:
            return
        cas_txt, _, _, _ = cas_field
        
        # Set FL and CAS ranges for backward compatibility with SATG_GC_RANGE
        _emit(f"SATG_GC_RANGE fl={fl_txt} cas={cas_txt}")
        
        # Get seed from parent widget
        seed_value = self.parent().rl_seed.value() if hasattr(self.parent(), 'rl_seed') else 0
        
        # Build command parts
        cmd_parts = [
            "SATG_RC_CIRCLE",
            f"name={name}",
            f"n={count}",
            "types=headon,cross,overtake",  # Use all types for modern geometric conflicts
            "mode=abs",
            "altmode=level",
            f"tcpa={tcpa_txt}",
            f"fl={fl_txt}",
            f"cas={cas_txt}",
            f"actypes={self.abs_actypes.text()}",
            f"overwrite={overwrite}",
            f"angle={angle_txt}",
        ]
        
        # Add seed if not 0 (0 means random)
        if seed_value != 0:
            cmd_parts.append(f"seed={seed_value}")
        
        # Add area-specific parameters
        if area_params["area_type"] == "circle":
            cmd_parts.extend([
                f"center_lat={area_params['center_lat']}",
                f"center_lon={area_params['center_lon']}",
                f"radius_nm={area_params['radius']}",
                "area_type=circle"
            ])
        else:  # polygon
            cmd_parts.extend([
                f"area_type=polygon",
                f"polygon_name={area_params['polygon_name']}",
                "center_lat=0",  # Dummy values required by command parsing
                "center_lon=0",
                "radius_nm=1",
                f"include_polygon={1 if self.include_polygon_cb.isChecked() else 0}"
            ])
        
        # Add altitude offset if specified
        if alt_offset_txt:
            cmd_parts.append(f"dh={alt_offset_txt}")
            
        # Add seed if specified
        if self.seed.value() != 0:
            cmd_parts.append(f"seed={self.seed.value()}")
            
        _emit(" ".join(cmd_parts))

    def _create_relative_conflicts(self, name: str, count: int, area_params: dict, overwrite: int):
        """Generate relative (target-intruder) conflicts in the specified area."""
        # Extract parameters using the two-field format like geometric conflicts
        
        # Extract dpsi (conflict angle)
        dpsi_field = self._extract_numeric_field("dpsi [deg]", self.rel_dpsi_value, self.rel_dpsi_range, allow_float=True, allow_negative=True)
        if not dpsi_field:
            return
        dpsi_str = f"{dpsi_field[2]:.1f}" if not dpsi_field[1] else f"{dpsi_field[2]:.1f}:{dpsi_field[3]:.1f}"
        
        # Extract dcpa (CPA distance) 
        dcpa_field = self._extract_numeric_field("dcpa [NM]", self.rel_dcpa_value, self.rel_dcpa_range, allow_float=True, allow_negative=False)
        if not dcpa_field:
            return
        dcpa_str = f"{dcpa_field[2]:.1f}" if not dcpa_field[1] else f"{dcpa_field[2]:.1f}:{dcpa_field[3]:.1f}"
        
        # Extract tlosh (horizontal time to loss)
        tlosh_field = self._extract_numeric_field("tlosh [s]", self.rel_tlosh_value, self.rel_tlosh_range, allow_float=True, allow_negative=False)
        if not tlosh_field:
            return
        tlosh_str = f"{tlosh_field[2]:.1f}" if not tlosh_field[1] else f"{tlosh_field[2]:.1f}:{tlosh_field[3]:.1f}"
        
        # Extract dh (vertical offset) - optional
        dh_str = ""
        if self.rel_dh_value.text().strip():
            dh_field = self._extract_numeric_field("dH [ft]", self.rel_dh_value, self.rel_dh_range, allow_float=False, allow_negative=True)
            if not dh_field:
                return
            dh_str = f"{int(dh_field[2])}" if not dh_field[1] else f"{int(dh_field[2])}:{int(dh_field[3])}"
        
        # Extract tlosv (vertical time to loss) - optional
        tlosv_str = ""
        if self.rel_tlosv_value.text().strip() and self.rel_tlosv_value.text().strip() != "0":
            tlosv_field = self._extract_numeric_field("tlosv [s]", self.rel_tlosv_value, self.rel_tlosv_range, allow_float=True, allow_negative=False)
            if not tlosv_field:
                return
            tlosv_str = f"{tlosv_field[2]:.1f}" if not tlosv_field[1] else f"{tlosv_field[2]:.1f}:{tlosv_field[3]:.1f}"
        
        # Extract flight level
        fl_field = self._extract_numeric_field("FL", self.rel_fl_value, self.rel_fl_range, allow_float=False, allow_negative=False)
        if not fl_field:
            return
        fl_str = f"{int(fl_field[2])}" if not fl_field[1] else f"{int(fl_field[2])}:{int(fl_field[3])}"
        
        # Extract speed - optional
        spd_str = ""
        if self.rel_spd_value.text().strip():
            # Speed can be CAS (numeric) or Mach (M0.xx), so we don't use the numeric validator
            spd_val = self.rel_spd_value.text().strip()
            spd_range = self.rel_spd_range.text().strip()
            if spd_range:
                spd_str = f"{spd_val}:{spd_range}"
            else:
                spd_str = spd_val
        
        # Get seed from parent widget
        seed_value = self.parent().rl_seed.value() if hasattr(self.parent(), 'rl_seed') else 0
        
        # Build SATG_RC_CIRCLE command
        cmd_parts = [
            "SATG_RC_CIRCLE",
            f"name={name}",
            f"n={count}",
            "types=cross",  # Relative conflicts are typically crossing encounters
            "mode=rel",
            "altmode=level",
            f"tcpa={tlosh_str}",  # tcpa parameter will be interpreted as tlosh in rel mode
            f"fl={fl_str}",
            f"actypes={self.rel_type.text()}",
            f"overwrite={overwrite}",
            f"angle={dpsi_str}",  # angle parameter will be interpreted as dpsi in rel mode
            f"dcpa={dcpa_str}",
        ]
        
        # Add area-specific parameters
        if area_params["area_type"] == "circle":
            cmd_parts.extend([
                f"center_lat={area_params['center_lat']}",
                f"center_lon={area_params['center_lon']}",
                f"radius_nm={area_params['radius']}",
                "area_type=circle"
            ])
        else:  # polygon
            cmd_parts.extend([
                f"area_type=polygon",
                f"polygon_name={area_params['polygon_name']}",
                "center_lat=0",  # Dummy values required by command parsing
                "center_lon=0",
                "radius_nm=1"
            ])
        
        # Add polygon inclusion parameter for polygon areas
        if area_params["area_type"] == "polygon":
            cmd_parts.append(f"include_polygon={1 if self.include_polygon_cb.isChecked() else 0}")
        
        # Add optional parameters
        if dh_str:
            cmd_parts.append(f"dh={dh_str}")
        if tlosv_str:
            cmd_parts.append(f"tlosv={tlosv_str}")
        if spd_str:
            cmd_parts.append(f"cas={spd_str}")
        
        # Add seed if not 0 (0 means random)
        if seed_value != 0:
            cmd_parts.append(f"seed={seed_value}")
            
        _emit(" ".join(cmd_parts))

    def _create_mixed_conflicts(self, name: str, count: int, area_params: dict, overwrite: int):
        """Generate mixed conflicts using SATG_RC_CIRCLE mode=mix (random for each conflict)."""
        
        # We need to determine which parameters to use for the mixed mode
        # We'll combine parameters from both absolute and relative settings
        
        # Use absolute settings for basic parameters, but allow both types
        tcpa_field = self._extract_numeric_field("TCPA [s]", self.abs_tcpa_value, self.abs_tcpa_range, allow_float=True, allow_negative=False)
        if tcpa_field is None:
            return
        tcpa_str = f"{tcpa_field[2]:.1f}" if not tcpa_field[1] else f"{tcpa_field[2]:.1f}:{tcpa_field[3]:.1f}"
        
        # Angle from absolute settings
        angle_field = self._extract_numeric_field("Angle [deg]", self.abs_angle_value, self.abs_angle_range, allow_float=True, allow_negative=False)
        if angle_field is None:
            return
        angle_str = f"{angle_field[2]:.1f}" if not angle_field[1] else f"{angle_field[2]:.1f}:{angle_field[3]:.1f}"
        
        # Flight level from absolute settings
        fl_field = self._extract_numeric_field("FL", self.abs_fl_value, self.abs_fl_range, allow_float=False, allow_negative=False)
        if fl_field is None:
            return
        fl_str = f"{int(fl_field[2])}" if not fl_field[1] else f"{int(fl_field[2])}:{int(fl_field[3])}"
        
        # CAS from absolute settings
        cas_field = self._extract_numeric_field("CAS [kt]", self.abs_cas_value, self.abs_cas_range, allow_float=False, allow_negative=False)
        if cas_field is None:
            return
        cas_str = f"{int(cas_field[2])}" if not cas_field[1] else f"{int(cas_field[2])}:{int(cas_field[3])}"
        
        # Aircraft types - combine both lists
        abs_types = self.abs_actypes.text().strip()
        rel_types = self.rel_type.text().strip()
        all_types = []
        if abs_types:
            all_types.extend([t.strip() for t in abs_types.split(",") if t.strip()])
        if rel_types:
            rel_types_list = [t.strip() for t in rel_types.split(",") if t.strip()]
            # Add relative types that aren't already in absolute types
            for t in rel_types_list:
                if t not in all_types:
                    all_types.append(t)
        if not all_types:
            all_types = ["A320", "B738"]  # Default fallback
        
        # Altitude offset from absolute settings (will be used for altcross mode)
        dh_str = ""
        if self.abs_alt_offset_value.text().strip():
            dh_field = self._extract_numeric_field("dH [ft]", self.abs_alt_offset_value, self.abs_alt_offset_range, allow_float=False, allow_negative=True)
            if dh_field is None:
                return
            dh_str = f"{int(dh_field[2])}" if not dh_field[1] else f"{int(dh_field[2])}:{int(dh_field[3])}"
        
        # Get seed from parent widget
        seed_value = self.parent().rl_seed.value() if hasattr(self.parent(), 'rl_seed') else 0
        
        # Build SATG_RC_CIRCLE command with mode=mix
        cmd_parts = [
            "SATG_RC_CIRCLE",
            f"name={name}",
            f"n={count}",
            "types=cross",  # Use cross as default type
            "mode=mix",     # This is the key - random mode like backend
            "altmode=level",
            f"tcpa={tcpa_str}",
            f"angle={angle_str}",
            f"fl={fl_str}",
            f"cas={cas_str}",
            f"actypes={','.join(all_types)}",
            f"overwrite={overwrite}",
        ]
        
        # Add area-specific parameters
        if area_params["area_type"] == "circle":
            cmd_parts.extend([
                f"center_lat={area_params['center_lat']}",
                f"center_lon={area_params['center_lon']}",
                f"radius_nm={area_params['radius']}",
                "area_type=circle"
            ])
        else:  # polygon
            cmd_parts.extend([
                f"area_type=polygon",
                f"polygon_name={area_params['polygon_name']}",
                "center_lat=0",  # Dummy values required by command parsing
                "center_lon=0",
                "radius_nm=1"
            ])
        
        # Add polygon inclusion parameter for polygon areas
        if area_params["area_type"] == "polygon":
            cmd_parts.append(f"include_polygon={1 if self.include_polygon_cb.isChecked() else 0}")
        
        # Add optional parameters
        if dh_str:
            cmd_parts.append(f"dh={dh_str}")
        
        # Add seed if not 0 (0 means random)
        if seed_value != 0:
            cmd_parts.append(f"seed={seed_value}")
        
        _emit(" ".join(cmd_parts))

    def _run(self):
        """Run the generated scenario."""
        name = self.scn.text().strip()
        if name:
            _emit(f"SATG_GC_RUN name={name}")
        else:
            _emit("ECHO SATGGUI: Set a scenario name before running.")

    def _create_and_run(self):
        """Create and immediately run the scenario."""
        self._create()
        self._run()

    def _make_value_range_row(self, value_widget: QLineEdit, range_widget: QLineEdit, placeholder: str) -> QWidget:
        """Create a two-field row like geometric conflicts for value + optional range."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        value_widget.setMaximumWidth(90)
        value_widget.setPlaceholderText("required")
        value_widget.setToolTip("Required value (used as deterministic or lower bound).")
        range_widget.setPlaceholderText(placeholder)
        range_widget.setToolTip("Optional upper bound; leave empty for a fixed value.")
        range_widget.setMinimumWidth(140)
        layout.addWidget(value_widget)
        layout.addWidget(range_widget, 1)
        return row

    def _extract_numeric_field(
        self,
        label: str,
        primary: QLineEdit,
        secondary: QLineEdit,
        *,
        allow_float: bool,
        allow_negative: bool,
    ) -> Optional[Tuple[str, bool, float, float]]:
        """Extract and validate a numeric field in value:range format."""
        primary_txt = primary.text().strip()
        if not primary_txt:
            _emit(f"ECHO Please fill the left {label} field.")
            return None
        try:
            primary_val = self._coerce_number(primary_txt, allow_float, allow_negative, label, "value")
        except ValueError as exc:
            _emit(f"ECHO {exc}")
            return None
        secondary_txt = secondary.text().strip()
        if secondary_txt:
            try:
                secondary_val = self._coerce_number(secondary_txt, allow_float, allow_negative, label, "upper bound")
            except ValueError as exc:
                _emit(f"ECHO {exc}")
                return None
            lo_val = float(primary_val)
            hi_val = float(secondary_val)
            if hi_val < lo_val:
                lo_val, hi_val = hi_val, lo_val
            normalized = f"{self._format_number(lo_val, allow_float)}:{self._format_number(hi_val, allow_float)}"
            return normalized, True, lo_val, hi_val

        normalized = self._format_number(primary_val, allow_float)
        return normalized, False, float(primary_val), float(primary_val)

    def _coerce_number(self, text: str, allow_float: bool, allow_negative: bool, label: str, desc: str):
        """Convert text to number with validation."""
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{label} {desc} must be numeric.")
        if not allow_negative and value < 0:
            raise ValueError(f"{label} {desc} must be >= 0.")
        if not allow_float:
            value = round(value)
        return value

    def _format_number(self, value, allow_float: bool) -> str:
        """Format number for command generation."""
        if allow_float:
            txt = f"{float(value):.6f}".rstrip("0").rstrip(".")
            return txt if txt and txt != "-0" else "0"
        else:
            return str(int(round(value)))
        
# --- Procedure Tab ----------------------------------------------------------
class ProcTab(QWidget):
    """Procedural traffic: load waypoint/procedure .scn files, then spawn flights
    that are auto-assigned a random procedure. Spawns occur in a sector around
    the first fix, toward the fix (inbound), with per-procedure min time spacing.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._wpt_files = []    # waypoint definition .scn (DEFWPT)
        self._proc_files = []   # procedure .scn (with %0 and PROCNAME)
        self._sid_rate_rows = {}
        self._sid_schedule_data: Dict[str, Dict[str, object]] = {}
        self._star_rate_rows = {}
        self._star_schedule_data: Dict[str, Dict[str, object]] = {}
        self._star_rate_values: Dict[str, Dict[str, int]] = {"initial": {}, "final": {}}
        self._star_rate_groups: Dict[str, List[str]] = {}
        self._star_basis_index: int = 0
        self._origins: Dict[str, str] = {}
        self._destinations: Dict[str, List[str]] = {}
        self._last_dest_sent: set[str] = set()
        self._proc_widgets: Dict[str, Dict[str, object]] = {}
        self._last_sid_sched_sent: set = set()
        self._last_star_sched_sent: set = set()
        main = QVBoxLayout(self)

        # 1) Files
        gb1 = QGroupBox("1) Files")
        gb1_layout = QVBoxLayout(gb1)
        
        # Create a scroll area for files section
        files_scroll = QScrollArea()
        files_scroll.setWidgetResizable(True)
        files_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        files_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        files_scroll.setMaximumHeight(300)  # Limit height to trigger scrolling
        
        # Create the form widget that will go inside the scroll area
        files_form_widget = QWidget()
        f1 = QFormLayout(files_form_widget)
        f1.setContentsMargins(5, 5, 5, 5)

        # Waypoint files
        self.lst_wpt = QListWidget()
        self.lst_wpt.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        wpt_btns = QWidget(); hbw = QHBoxLayout(wpt_btns); hbw.setContentsMargins(0,0,0,0)
        btn_wpt_add = QPushButton("Add waypoint files…")
        btn_wpt_rm  = QPushButton("Remove selected")
        btn_wpt_clr = QPushButton("Clear all")
        hbw.addWidget(btn_wpt_add); hbw.addWidget(btn_wpt_rm); hbw.addWidget(btn_wpt_clr); hbw.addStretch(1)
        f1.addRow(QLabel("Waypoint scenario files (.scn):"))
        f1.addRow(self.lst_wpt)
        f1.addRow(wpt_btns)

        # Procedure files
        self.lst_proc = QListWidget()
        self.lst_proc.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        proc_btns = QWidget(); hbp = QHBoxLayout(proc_btns); hbp.setContentsMargins(0,0,0,0)
        btn_proc_add = QPushButton("Add procedure files…")
        btn_proc_rm  = QPushButton("Remove selected")
        btn_proc_clr = QPushButton("Clear all")
        hbp.addWidget(btn_proc_add); hbp.addWidget(btn_proc_rm); hbp.addWidget(btn_proc_clr); hbp.addStretch(1)
        f1.addRow(QLabel("Procedure scenario files (.scn):"))
        f1.addRow(self.lst_proc)
        f1.addRow(proc_btns)

        # Set the form widget as the scroll area's widget
        files_scroll.setWidget(files_form_widget)
        
        # Add the scroll area to the group box
        gb1_layout.addWidget(files_scroll)
        main.addWidget(gb1)

        btn_wpt_add.clicked.connect(self._add_wpt)
        btn_wpt_rm.clicked.connect(self._rm_wpt)
        btn_wpt_clr.clicked.connect(self._clr_wpt)
        btn_proc_add.clicked.connect(self._add_proc)
        btn_proc_rm.clicked.connect(self._rm_proc)
        btn_proc_clr.clicked.connect(self._clr_proc)

        # 2) Batch options
        gb2 = QGroupBox("2) Batch options")
        gb2_main_layout = QVBoxLayout(gb2)
        
        # Create a scroll area for batch options
        batch_options_scroll = QScrollArea()
        batch_options_scroll.setWidgetResizable(True)
        batch_options_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        batch_options_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        batch_options_scroll.setMaximumHeight(400)  # Limit height to trigger scrolling
        
        # Create the widget that will hold the horizontal layout
        batch_options_widget = QWidget()
        hb2 = QHBoxLayout(batch_options_widget)
        hb2.setContentsMargins(6, 6, 6, 6)
        hb2.setSpacing(12)

        # Generic procedures column
        generic_box = QGroupBox("Generic")
        fg = QFormLayout(generic_box)
        self.gen_flights = QSpinBox(); self.gen_flights.setRange(0, 100000); self.gen_flights.setValue(20)
        fg.addRow("Flights:", self.gen_flights)
        self.gen_minsep = QSpinBox(); self.gen_minsep.setRange(0, 3600); self.gen_minsep.setValue(90)
        fg.addRow("Min separation [s]:", self.gen_minsep)
        self.spawn_r = QDoubleSpinBox(); self.spawn_r.setDecimals(1); self.spawn_r.setRange(0.1, 100.0); self.spawn_r.setValue(5.0)
        fg.addRow("Spawn radius [NM]:", self.spawn_r)
        self.spawn_env = QDoubleSpinBox(); self.spawn_env.setDecimals(0); self.spawn_env.setRange(5, 180); self.spawn_env.setValue(40)
        fg.addRow("Angular envelope [deg]:", self.spawn_env)
        hb2.addWidget(generic_box, 1)

        # SID-specific column
        sid_box = QGroupBox("SID")
        fs = QFormLayout(sid_box); self._sid_form = fs
        sid_hint = QLabel("SID procedures spawn at runway thresholds")
        sid_hint.setWordWrap(True)
        fs.addRow(sid_hint)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Scheduling mode:", sid_box))
        self.sid_mode = QComboBox(sid_box)
        self.sid_mode.addItems(["Hourly rate", "15-min schedule"])
        mode_row.addWidget(self.sid_mode)
        mode_row.addStretch(1)
        fs.addRow(mode_row)

        self.sid_flights = QSpinBox(); self.sid_flights.setRange(0, 100000); self.sid_flights.setValue(20)
        fs.addRow("Flights:", self.sid_flights)
        self.sid_alt = QSpinBox(); self.sid_alt.setRange(0, 50000); self.sid_alt.setValue(3000)
        fs.addRow("Initial ALT [ft]:", self.sid_alt)
        self.sid_spd = QSpinBox(); self.sid_spd.setRange(0, 600); self.sid_spd.setValue(210)
        fs.addRow("Initial SPD [kt]:", self.sid_spd)
        self.sid_sched_btn = QPushButton("Configure schedule…")
        self.sid_sched_btn.clicked.connect(self._configure_sid_schedule)
        fs.addRow(self.sid_sched_btn)
        self.sid_mode.currentIndexChanged.connect(self._on_sid_mode_changed)
        hb2.addWidget(sid_box, 1)

        # STAR column
        star_box = QGroupBox("STAR")
        ft = QFormLayout(star_box); self._star_form = ft
        star_hint = QLabel("STAR procedures spawn at their first waypoint.\nLoad STAR-*.scn files.")
        star_hint.setWordWrap(True)
        ft.addRow(star_hint)
        mode_row_star = QHBoxLayout()
        mode_row_star.addWidget(QLabel("Scheduling mode:", star_box))
        self.star_mode = QComboBox(star_box)
        self.star_mode.addItems(["Hourly rate", "15-min schedule"])
        mode_row_star.addWidget(self.star_mode)
        mode_row_star.addSpacing(12)
        mode_row_star.addWidget(QLabel("Rate basis:", star_box))
        self.star_rate_basis = QComboBox(star_box)
        self.star_rate_basis.addItems(["Initial waypoint", "Final waypoint"])
        mode_row_star.addWidget(self.star_rate_basis)
        mode_row_star.addStretch(1)
        ft.addRow(mode_row_star)
        self.star_flights = QSpinBox(); self.star_flights.setRange(0, 100000); self.star_flights.setValue(20)
        ft.addRow("Flights:", self.star_flights)
        alt_row = QWidget(star_box)
        alt_layout = QHBoxLayout(alt_row); alt_layout.setContentsMargins(0, 0, 0, 0)
        alt_layout.addWidget(QLabel("Initial ALT [FL]:", star_box))
        self.star_alt_fl = QSpinBox()
        self.star_alt_fl.setRange(0, 600)
        self.star_alt_fl.setSingleStep(10)
        self.star_alt_fl.setValue(360)
        alt_layout.addWidget(self.star_alt_fl)
        alt_layout.addSpacing(12)
        alt_layout.addWidget(QLabel("Final ALT [FL]:", star_box))
        self.star_final_alt_fl = QSpinBox()
        self.star_final_alt_fl.setRange(0, 600)
        self.star_final_alt_fl.setSingleStep(10)
        self.star_final_alt_fl.setValue(100)
        alt_layout.addWidget(self.star_final_alt_fl)
        alt_layout.addStretch(1)
        ft.addRow(alt_row)
        spd_row = QWidget(star_box)
        spd_layout = QHBoxLayout(spd_row); spd_layout.setContentsMargins(0, 0, 0, 0)
        spd_layout.addWidget(QLabel("Initial Mach:", star_box))
        self.star_mach = QDoubleSpinBox()
        self.star_mach.setDecimals(2)
        self.star_mach.setRange(0.40, 0.92)
        self.star_mach.setSingleStep(0.01)
        self.star_mach.setValue(0.79)
        spd_layout.addWidget(self.star_mach)
        spd_layout.addSpacing(12)
        spd_layout.addWidget(QLabel("Final SPD [kt]:", star_box))
        self.star_final_spd = QSpinBox()
        self.star_final_spd.setRange(0, 600)
        self.star_final_spd.setValue(240)
        spd_layout.addWidget(self.star_final_spd)
        spd_layout.addStretch(1)
        ft.addRow(spd_row)
        self.star_sched_btn = QPushButton("Configure schedule…")
        self.star_sched_btn.clicked.connect(self._configure_star_schedule)
        ft.addRow(self.star_sched_btn)
        self.star_mode.currentIndexChanged.connect(self._on_star_mode_changed)
        self.star_rate_basis.currentIndexChanged.connect(self._on_star_basis_changed)
        hb2.addWidget(star_box, 1)

        # Set the batch options widget as the scroll area's widget
        batch_options_scroll.setWidget(batch_options_widget)
        
        # Add the scroll area to the group box
        gb2_main_layout.addWidget(batch_options_scroll)
        main.addWidget(gb2)

        # 3) Scenario and actions
        gb3 = QGroupBox("3) Scenario and actions")
        gb3_layout = QVBoxLayout(gb3)
        
        # Create a scroll area for scenario and actions
        scenario_scroll = QScrollArea()
        scenario_scroll.setWidgetResizable(True)
        scenario_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scenario_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scenario_scroll.setMaximumHeight(300)  # Limit height to trigger scrolling
        
        # Create the form widget that will go inside the scroll area
        scenario_form_widget = QWidget()
        f3 = QFormLayout(scenario_form_widget)
        f3.setContentsMargins(5, 5, 5, 5)

        self.scn = QLineEdit("proc_scn")
        f3.addRow("Scenario name:", self.scn)

        self.seed = QSpinBox(); self.seed.setRange(0, 2_000_000_000); self.seed.setValue(0)
        f3.addRow("Seed (0 = random):", self.seed)

        self.overwrite = QCheckBox("Overwrite scenario if it exists")
        self.overwrite.setChecked(True)
        f3.addRow(self.overwrite)

        self.dest_enable = QCheckBox("Assign random destinations")
        self.dest_enable.toggled.connect(self._on_dest_toggle)
        self.dest_enable.setChecked(True)
        f3.addRow(self.dest_enable)

        row_act = QWidget(); hb3 = QHBoxLayout(row_act); hb3.setContentsMargins(0,0,0,0)
        btn_cre  = QPushButton("CREATE SCENARIO FILE")
        btn_run  = QPushButton("RUN SCENARIO")
        btn_both = QPushButton("CREATE & RUN SCENARIO")
        hb3.addWidget(btn_cre); hb3.addWidget(btn_run); hb3.addWidget(btn_both)
        f3.addRow(row_act)

        # Set the form widget as the scroll area's widget
        scenario_scroll.setWidget(scenario_form_widget)
        
        # Add the scroll area to the group box
        gb3_layout.addWidget(scenario_scroll)
        main.addWidget(gb3)

        btn_cre.clicked.connect(self._make)
        btn_run.clicked.connect(self._run_only)
        btn_both.clicked.connect(self._make_and_run)
        self._update_sid_mode_state()
        self._update_star_mode_state()
        self._update_dest_state()

    # ---- file ops ----
    def _ensure_sid_runway(self, runway: str):
        if runway in self._sid_rate_rows:
            return
        label = QLabel(f"RW{runway} rate [ac/h]:")
        spin = QSpinBox(); spin.setRange(0, 120); spin.setValue(40)
        self._sid_form.addRow(label, spin)
        self._sid_rate_rows[runway] = (label, spin)

    def _remove_sid_runway(self, runway: str):
        row = self._sid_rate_rows.pop(runway, None)
        if not row:
            return
        label, spin = row
        self._sid_form.removeRow(spin)
        if label is not None and not sip.isdeleted(label):
            label.deleteLater()
        if spin is not None and not sip.isdeleted(spin):
            spin.deleteLater()

    def _clear_sid_runway_rows(self):
        for runway in list(self._sid_rate_rows.keys()):
            self._remove_sid_runway(runway)

    def _is_sid_file(self, path: Optional[str]) -> bool:
        if not path:
            return False
        base = os.path.basename(path)
        return bool(_SID_FILE_RE.match(base)) if base else False

    def _is_star_file(self, path: Optional[str]) -> bool:
        if not path:
            return False
        base = os.path.basename(path)
        return bool(re.match(r'^STAR-[-A-Za-z0-9_]+\.scn$', base or "", re.IGNORECASE))

    def _proc_fix_sequence(self, path: str) -> List[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return []
        seq: List[str] = []
        for match in re.finditer(r"ADDWPT\s+([A-Za-z0-9_+\-/]+)", text, re.IGNORECASE):
            name = match.group(1).strip().upper()
            if not name:
                continue
            if not seq or seq[-1] != name:
                seq.append(name)
        return seq

    def _current_sid_runways(self) -> set:
        runways = set()
        for path in self._proc_files:
            m = _SID_FILE_RE.match(os.path.basename(path))
            if m:
                runways.add(m.group(1).upper())
        return runways

    def _refresh_sid_runway_rows(self):
        active = self._current_sid_runways()
        for rw in list(self._sid_rate_rows.keys()):
            if rw not in active:
                self._remove_sid_runway(rw)
        for rw in list(self._sid_schedule_data.keys()):
            if rw not in active:
                self._sid_schedule_data.pop(rw, None)
        for rw in active:
            self._ensure_sid_runway(rw)
        self._update_sid_mode_state()

    def _on_sid_mode_changed(self, idx):
        self._update_sid_mode_state()

    def _update_sid_mode_state(self):
        use_schedule = self.sid_mode.currentIndex() == 1 if hasattr(self, "sid_mode") else False
        has_runways = bool(self._current_sid_runways()) if hasattr(self, "_current_sid_runways") else False
        if hasattr(self, "sid_sched_btn"):
            self.sid_sched_btn.setEnabled(use_schedule and has_runways)
        if hasattr(self, "sid_flights"):
            self.sid_flights.blockSignals(True)
            if use_schedule:
                total = sum(sum(cfg.get("caps", [])) for cfg in self._sid_schedule_data.values())
                self.sid_flights.setValue(total)
            self.sid_flights.setEnabled(not use_schedule)
            self.sid_flights.blockSignals(False)
        for label, spin in self._sid_rate_rows.values():
            spin.setEnabled(not use_schedule)

    def _current_star_procs(self) -> List[str]:
        return [path for path, data in self._proc_widgets.items() if data.get("is_star")]

    def _basis_name(self, idx: int) -> str:
        return "final" if idx == 1 else "initial"

    def _current_star_basis(self) -> str:
        return self._basis_name(self._star_basis_index)

    def _capture_star_rates(self, basis: str):
        store = self._star_rate_values.setdefault(basis, {})
        for key, (_, spin) in self._star_rate_rows.items():
            store[key] = int(spin.value())

    def _build_star_groups(self, basis: str) -> Dict[str, List[str]]:
        groups: Dict[str, List[str]] = {}
        for path in self._current_star_procs():
            widgets = self._proc_widgets.get(path, {})
            key_raw = widgets.get("initial_fix") if basis == "initial" else widgets.get("final_fix")
            if not key_raw:
                key_raw = os.path.splitext(os.path.basename(path))[0]
            key = str(key_raw).upper()
            groups.setdefault(key, []).append(path)
        return groups

    def _ensure_star_rate_row(self, key: str, basis: str):
        store = self._star_rate_values.setdefault(basis, {})
        value = int(store.get(key, DEFAULT_STAR_RATE))
        if key in self._star_rate_rows:
            label, spin = self._star_rate_rows[key]
            label.setText(f"{key} rate [ac/h]:")
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
            return
        label = QLabel(f"{key} rate [ac/h]:")
        spin = QSpinBox()
        spin.setRange(0, 120)
        spin.setValue(value)
        spin.valueChanged.connect(lambda val, b=basis, g=key: self._on_star_rate_changed(b, g, val))
        self._star_form.addRow(label, spin)
        self._star_rate_rows[key] = (label, spin)

    def _on_star_rate_changed(self, basis: str, key: str, val: int):
        self._star_rate_values.setdefault(basis, {})[key] = int(val)

    def _remove_star_rate_row(self, key: str):
        row = self._star_rate_rows.pop(key, None)
        if not row:
            return
        label, spin = row
        basis = self._current_star_basis()
        self._star_rate_values.setdefault(basis, {})[key] = int(spin.value())
        self._star_form.removeRow(spin)
        if label is not None and not sip.isdeleted(label):
            label.deleteLater()
        if spin is not None and not sip.isdeleted(spin):
            spin.deleteLater()

    def _clear_star_rate_rows(self):
        for key in list(self._star_rate_rows.keys()):
            self._remove_star_rate_row(key)

    def _refresh_star_rate_rows(self):
        basis = self._current_star_basis()
        self._capture_star_rates(basis)
        groups = self._build_star_groups(basis)
        self._star_rate_groups = groups
        for key in list(self._star_rate_rows.keys()):
            if key not in groups:
                self._remove_star_rate_row(key)
        for key in groups.keys():
            self._ensure_star_rate_row(key, basis)
        active_paths = set(self._current_star_procs())
        for path in list(self._star_schedule_data.keys()):
            if path not in active_paths:
                self._star_schedule_data.pop(path, None)
        self._update_star_mode_state()

    def _on_star_mode_changed(self, idx):
        self._update_star_mode_state()

    def _update_star_mode_state(self):
        use_schedule = self.star_mode.currentIndex() == 1 if hasattr(self, "star_mode") else False
        has_procs = bool(self._current_star_procs())
        if hasattr(self, "star_sched_btn"):
            self.star_sched_btn.setEnabled(use_schedule and has_procs)
        if hasattr(self, "star_rate_basis"):
            self.star_rate_basis.setEnabled(not use_schedule)
        if hasattr(self, "star_flights"):
            self.star_flights.blockSignals(True)
            if use_schedule and has_procs:
                total_caps = 0
                for path in self._current_star_procs():
                    cfg = self._star_schedule_data.get(path)
                    if not cfg:
                        continue
                    caps = [int(c) for c in cfg.get("caps", [])]
                    total_caps += sum(caps)
                if total_caps > 0:
                    self.star_flights.setValue(total_caps)
                self.star_flights.setEnabled(False)
            else:
                self.star_flights.setEnabled(True)
            self.star_flights.blockSignals(False)
        for _, spin in self._star_rate_rows.values():
            spin.setEnabled(not use_schedule)

    def _on_star_basis_changed(self, idx: int):
        prev_basis = self._basis_name(self._star_basis_index)
        self._capture_star_rates(prev_basis)
        self._star_basis_index = idx
        self._refresh_star_rate_rows()

    def _configure_star_schedule(self):
        procs = [(path, os.path.splitext(os.path.basename(path))[0]) for path in self._current_star_procs()]
        if not procs:
            _emit("ECHO Load STAR procedures before configuring schedules.")
            return
        dialog = StarSchedDialog(procs, self._star_schedule_data, self)
        if dialog.exec():
            result = dialog.result_data
            cleaned: Dict[str, Dict[str, object]] = {}
            for path, cfg in result.items():
                caps = [int(c) for c in cfg.get("caps", [])]
                if sum(caps) > 0:
                    cleaned[path] = {
                        "start": float(cfg.get("start", 0.0)),
                        "end": float(cfg.get("end", 0.0)),
                        "caps": caps,
                        "slot": float(cfg.get("slot", StarSchedDialog.SLOT_MINUTES)),
                    }
            self._star_schedule_data = cleaned
            self._update_star_mode_state()

    def _update_dest_state(self):
        has_procs = bool(self._proc_files)
        if hasattr(self, "dest_enable") and not has_procs:
            self.dest_enable.blockSignals(True)
            self.dest_enable.setChecked(False)
            self.dest_enable.blockSignals(False)
        self._update_star_mode_state()

    def _configure_sid_schedule(self):
        runways = sorted(self._current_sid_runways())
        if not runways:
            _emit("ECHO Load SID procedures before configuring schedules.")
            return
        dialog = SIDSchedDialog(runways, self._sid_schedule_data, self)
        if dialog.exec():
            result = dialog.result_data
            new_data: Dict[str, Dict[str, object]] = {}
            for rw, cfg in result.items():
                caps = [int(c) for c in cfg.get("caps", [])]
                if sum(caps) > 0:
                    new_data[rw] = {
                        "start": float(cfg.get("start", 0.0)),
                        "end": float(cfg.get("end", 0.0)),
                        "caps": caps,
                    }
            self._sid_schedule_data = new_data
            self._refresh_sid_runway_rows()
            self._update_sid_mode_state()

    def _on_dest_toggle(self, checked: bool):
        self._update_dest_state()

    def _configure_destinations(self):
        if not self._proc_files:
            _emit("ECHO Load procedures before configuring destinations.")
            return
        procs = [(p, os.path.splitext(os.path.basename(p))[0]) for p in self._proc_files]
        dialog = DestDialog(procs, self._destinations, self)
        if dialog.exec():
            result = dialog.result_data
            cleaned: Dict[str, List[str]] = {}
            for path_key, codes in result.items():
                if path_key in self._proc_files:
                    cleaned[path_key] = [c.upper() for c in codes]
            self._destinations = cleaned
            self._sync_destination_edits()
            self._update_dest_state()

    def _parse_destinations(self, text: str) -> List[str]:
        return [c.strip().upper() for c in re.split(r'[;,\\s]+', text) if c.strip()]

    def _normalize_origin_code(self, text: str) -> Optional[str]:
        code = text.strip().upper()
        if not code:
            return ""
        if re.fullmatch(r"[A-Z]{3,4}", code):
            return code
        return None

    def _update_origin_entry(self, path: str, text: str):
        widgets = self._proc_widgets.get(path)
        origin_edit = widgets.get("origin") if widgets else None
        if not self._is_sid_file(path):
            if origin_edit:
                origin_edit.blockSignals(True)
                origin_edit.clear()
                origin_edit.blockSignals(False)
            return
        code = self._normalize_origin_code(text)
        label = os.path.basename(path)
        if code is None:
            _emit(f"ECHO Origin ICAO must be 3 or 4 letters for {label}.")
            if origin_edit:
                origin_edit.blockSignals(True)
                origin_edit.setText(self._origins.get(path, ""))
                origin_edit.blockSignals(False)
            return
        if not code:
            _emit(f"ECHO Set an origin ICAO for {label}.")
            if origin_edit:
                origin_edit.blockSignals(True)
                origin_edit.setText(self._origins.get(path, ""))
                origin_edit.blockSignals(False)
            return
        if origin_edit and origin_edit.text() != code:
            origin_edit.blockSignals(True)
            origin_edit.setText(code)
            origin_edit.blockSignals(False)
        self._origins[path] = code

    def _update_destination_entry(self, path: str, text: str):
        codes = self._parse_destinations(text)
        if codes:
            self._destinations[path] = codes
        else:
            self._destinations.pop(path, None)
        widgets = self._proc_widgets.get(path)
        if widgets:
            edit = widgets.get("dest")
            if edit:
                edit.blockSignals(True)
                edit.setText(", ".join(self._destinations.get(path, [])))
                edit.blockSignals(False)
        self._update_dest_state()

    def _apply_dest_to_all(self, path: str):
        widgets = self._proc_widgets.get(path, {})
        dest_edit = widgets.get("dest")
        if not dest_edit:
            return
        codes = self._parse_destinations(dest_edit.text())
        if codes:
            self._destinations[path] = list(codes)
        else:
            self._destinations.pop(path, None)
        for other in self._proc_files:
            if codes:
                self._destinations[other] = list(codes)
            else:
                self._destinations.pop(other, None)
        self._sync_destination_edits()
        self._update_dest_state()

    def _sync_destination_edits(self):
        for path, widgets in self._proc_widgets.items():
            edit = widgets.get("dest")
            if not edit:
                continue
            edit.blockSignals(True)
            edit.setText(", ".join(self._destinations.get(path, [])))
            edit.blockSignals(False)

    def _sync_origin_edits(self):
        for path, widgets in self._proc_widgets.items():
            if not widgets.get("is_sid", False):
                continue
            origin_edit = widgets.get("origin")
            if not origin_edit:
                continue
            origin_edit.blockSignals(True)
            origin_edit.setText(self._origins.get(path, ""))
            origin_edit.blockSignals(False)

    def _apply_origin_to_all(self, path: str):
        widgets = self._proc_widgets.get(path, {})
        origin_edit = widgets.get("origin")
        text = origin_edit.text() if origin_edit else self._origins.get(path, "")
        code = self._normalize_origin_code(text or "")
        label = os.path.basename(path)
        if not self._is_sid_file(path):
            return
        if code is None:
            _emit(f"ECHO Origin ICAO must be 3 or 4 letters for {label}.")
            if origin_edit:
                origin_edit.blockSignals(True)
                origin_edit.setText(self._origins.get(path, ""))
                origin_edit.blockSignals(False)
            return
        if not code:
            _emit(f"ECHO Set an origin ICAO for {label} before copying to all.")
            if origin_edit:
                origin_edit.blockSignals(True)
                origin_edit.setText(self._origins.get(path, ""))
                origin_edit.blockSignals(False)
            return
        self._origins[path] = code
        for other in self._proc_files:
            if not self._is_sid_file(other):
                continue
            self._origins[other] = code
        self._sync_origin_edits()

    def _ensure_origin_ready(self) -> bool:
        invalid: List[str] = []
        missing: List[str] = []
        focus_widget = None
        for path in self._proc_files:
            if not self._is_sid_file(path):
                continue
            widgets = self._proc_widgets.get(path, {})
            origin_edit = widgets.get("origin")
            raw = origin_edit.text() if origin_edit else self._origins.get(path, "")
            code = self._normalize_origin_code(raw)
            label = os.path.basename(path)
            if code is None:
                invalid.append(label)
                if origin_edit:
                    origin_edit.blockSignals(True)
                    origin_edit.setText(self._origins.get(path, ""))
                    origin_edit.blockSignals(False)
                    if focus_widget is None:
                        focus_widget = origin_edit
                continue
            if not code:
                missing.append(label)
                if focus_widget is None and origin_edit:
                    focus_widget = origin_edit
                continue
            if origin_edit and origin_edit.text() != code:
                origin_edit.blockSignals(True)
                origin_edit.setText(code)
                origin_edit.blockSignals(False)
            self._origins[path] = code
            base = os.path.splitext(os.path.basename(path))[0]
            _emit(_join_tokens("SATG_PROC_SET_ICAO", base, code))
        if invalid:
            _emit("ECHO Origin ICAO must be 3 or 4 letters for: " + ", ".join(invalid))
            if focus_widget:
                focus_widget.setFocus()
            return False
        if missing:
            _emit("ECHO Set an origin ICAO for: " + ", ".join(missing))
            if focus_widget:
                focus_widget.setFocus()
            return False
        return True

    def _add_wpt(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose waypoint .scn files", filter="Scenario files (*.scn);;All files (*)")
        if not files: return
        for p in files:
            _emit(_join_tokens("SATG_PROC_LOAD_WPT", _qpath(p)))
        new = [p for p in files if p not in self._wpt_files]
        self._wpt_files.extend(new)
        for p in new:
            self.lst_wpt.addItem(p)

    def _rm_wpt(self):
        sel = self.lst_wpt.selectedItems()
        for it in sel:
            p = it.text()
            _emit(_join_tokens("SATG_PROC_UNLOAD_WPT", _qpath(p)))
            self._wpt_files = [x for x in self._wpt_files if x != p]
            self.lst_wpt.takeItem(self.lst_wpt.row(it))

    def _clr_wpt(self):
        if not self._wpt_files: return
        _emit("SATG_PROC_CLEAR_WPT")
        self._wpt_files.clear(); self.lst_wpt.clear()

    def _add_proc(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose procedure .scn files", filter="Scenario files (*.scn);;All files (*)")
        if not files: return
        for p in files:
            _emit(_join_tokens("SATG_PROC_LOAD_PROC", _qpath(p)))
        new = [p for p in files if p not in self._proc_files]
        self._proc_files.extend(new)
        for p in new:
            item = QListWidgetItem(self.lst_proc)
            item.setData(Qt.ItemDataRole.UserRole, p)
            container = QWidget(self.lst_proc)
            row_layout = QHBoxLayout(container)
            row_layout.setContentsMargins(0, 0, 0, 0)
            is_sid = self._is_sid_file(p)
            is_star = self._is_star_file(p)

            origin_edit: Optional[QLineEdit] = None
            origin_all_btn: Optional[QPushButton] = None
            if is_sid:
                origin_edit = QLineEdit(container)
                origin_edit.setPlaceholderText("Origin ICAO")
                origin_edit.setMaxLength(4)
                origin_edit.setMaximumWidth(90)
                if self._origins.get(p):
                    origin_edit.setText(self._origins[p])
                origin_edit.editingFinished.connect(lambda path=p, ref=origin_edit: self._update_origin_entry(path, ref.text()))

                origin_all_btn = QPushButton("Origin -> all", container)
                origin_all_btn.setAutoDefault(False)
                origin_all_btn.clicked.connect(lambda _, path=p: self._apply_origin_to_all(path))

                row_layout.addWidget(origin_edit)
                row_layout.addWidget(origin_all_btn)

            label = QLabel(p, container)
            label.setStyleSheet("color: #555;")
            row_layout.addWidget(label, 2)

            dest_edit = QLineEdit(container)
            dest_edit.setPlaceholderText("Destinations (comma separated)")
            existing = self._destinations.get(p, [])
            if existing:
                dest_edit.setText(", ".join(existing))
            dest_edit.editingFinished.connect(lambda path=p, ref=dest_edit: self._update_destination_entry(path, ref.text()))

            dest_all_btn = QPushButton("Dest -> all", container)
            dest_all_btn.setAutoDefault(False)
            dest_all_btn.clicked.connect(lambda _, path=p: self._apply_dest_to_all(path))

            row_layout.addWidget(dest_edit, 1)
            row_layout.addWidget(dest_all_btn)
            row_layout.addStretch(1)
            item.setSizeHint(container.sizeHint())
            self.lst_proc.setItemWidget(item, container)
            self._proc_widgets[p] = {
                "item": item,
                "origin": origin_edit,
                "origin_all": origin_all_btn,
                "dest": dest_edit,
                "dest_all": dest_all_btn,
                "is_sid": is_sid,
                "is_star": is_star,
            }
            if is_star:
                fixes = self._proc_fix_sequence(p)
                initial_fix = fixes[0] if fixes else ""
                final_fix = fixes[-1] if fixes else ""
                self._proc_widgets[p]["initial_fix"] = initial_fix
                self._proc_widgets[p]["final_fix"] = final_fix
        self._refresh_sid_runway_rows()
        self._refresh_star_rate_rows()
        self._sync_destination_edits()
        self._sync_origin_edits()
        self._update_dest_state()

    def _rm_proc(self):
        sel = self.lst_proc.selectedItems()
        for item in sel:
            p = item.data(Qt.ItemDataRole.UserRole)
            if not p:
                continue
            _emit(_join_tokens("SATG_PROC_UNLOAD_PROC", _qpath(p)))
            self._proc_files = [x for x in self._proc_files if x != p]
            self._destinations.pop(p, None)
            self._origins.pop(p, None)
            self._star_schedule_data.pop(p, None)
            self._proc_widgets.pop(p, None)
            row = self.lst_proc.row(item)
            self.lst_proc.takeItem(row)
        self._refresh_sid_runway_rows()
        self._refresh_star_rate_rows()
        self._sync_destination_edits()
        self._sync_origin_edits()
        self._update_dest_state()

    def _clr_proc(self):
        if not self._proc_files: return
        _emit("SATG_PROC_CLEAR_PROC")
        self._proc_files.clear(); self.lst_proc.clear()
        self._clear_sid_runway_rows()
        self._sid_schedule_data.clear()
        self._last_sid_sched_sent.clear()
        self._clear_star_rate_rows()
        self._star_schedule_data.clear()
        self._last_star_sched_sent.clear()
        self._star_rate_values = {"initial": {}, "final": {}}
        self._star_rate_groups = {}
        if hasattr(self, "star_rate_basis"):
            self._star_basis_index = self.star_rate_basis.currentIndex()
        self._destinations.clear()
        self._last_dest_sent.clear()
        self._origins.clear()
        self._proc_widgets.clear()
        self._update_dest_state()
        self._update_star_mode_state()

    # ---- actions ----
    def _make(self):
        if not self._proc_files:
            _emit("ECHO Add at least one procedure file.")
            return False
        name = self.scn.text().strip()
        if not name:
            _emit("ECHO Provide a scenario name.")
            return False

        gen_n = int(self.gen_flights.value())
        sid_n = int(self.sid_flights.value())
        star_n = int(self.star_flights.value())
        star_mode_schedule = self.star_mode.currentIndex() == 1
        star_alt_fl = int(self.star_alt_fl.value())
        star_mach = float(self.star_mach.value())
        star_rate_basis_idx = self.star_rate_basis.currentIndex()
        star_final_alt_fl = int(self.star_final_alt_fl.value())
        star_final_spd = int(self.star_final_spd.value())
        star_minsep = 90

        star_procs = self._current_star_procs()
        if star_mode_schedule and star_n > 0 and star_procs:
            total_caps = 0
            for path in star_procs:
                cfg = self._star_schedule_data.get(path)
                if not cfg:
                    continue
                caps = [int(c) for c in cfg.get("caps", [])]
                total_caps += sum(caps)
            if total_caps <= 0:
                _emit("ECHO Configure STAR schedule slots before running.")
                return False
            star_n = total_caps
            self.star_flights.blockSignals(True)
            self.star_flights.setValue(star_n)
            self.star_flights.blockSignals(False)

        if not self._ensure_origin_ready():
            return False

        use_schedule = self.sid_mode.currentIndex() == 1
        sid_sched_total = 0
        scheduled_runways = set()
        if use_schedule and sid_n > 0:
            for rw, cfg in self._sid_schedule_data.items():
                caps = cfg.get("caps", [])
                total_caps = sum(int(c) for c in caps)
                if total_caps > 0:
                    sid_sched_total += total_caps
                    scheduled_runways.add(rw)
            if sid_sched_total > 0:
                sid_n = sid_sched_total
            self.sid_flights.blockSignals(True)
            self.sid_flights.setValue(sid_sched_total)
            self.sid_flights.blockSignals(False)

        total = gen_n + sid_n + star_n
        if total <= 0:
            _emit("ECHO Configure at least one flight in Generic, SID, or STAR sections.")
            return False

        rnm = float(self.spawn_r.value())
        env = float(self.spawn_env.value())
        gen_min = int(self.gen_minsep.value())
        overall_min = max(gen_min, star_minsep)
        s   = int(self.seed.value())
        ow  = 1 if self.overwrite.isChecked() else 0

        sid_alt = int(self.sid_alt.value())
        sid_spd = int(self.sid_spd.value())

        _emit(f"SATG_PROC_CFG_GENERIC {gen_n} {gen_min}")
        _emit(f"SATG_PROC_CFG_SID {sid_n} {sid_alt} {sid_spd}")
        _emit(f"SATG_PROC_USE_DEST {1 if self.dest_enable.isChecked() else 0}")
        active_runways = self._current_sid_runways()
        current_sched_sent = set()
        current_sched_sent = set()
        if use_schedule and sid_n > 0:
            if sid_sched_total == 0:
                _emit("ECHO No SID schedule configured; add slots or switch to hourly rate mode.")
                return False
            for rw in sorted(scheduled_runways):
                cfg = self._sid_schedule_data.get(rw, {})
                caps = cfg.get("caps", [])
                if not caps:
                    continue
                start = int(round(cfg.get("start", 0.0)))
                end = int(round(cfg.get("end", start + 15)))
                if end <= start:
                    end = start + 15 * len(caps)
                caps_str = " ".join(str(int(c)) for c in caps)
                if caps_str:
                    _emit(f"SATG_PROC_CFG_SIDSCHED RW{rw} {start} {end} {caps_str}")
                    current_sched_sent.add(rw)
        else:
            if self._last_sid_sched_sent:
                for rw in sorted(self._last_sid_sched_sent):
                    _emit(f"SATG_PROC_CLEAR_SIDSCHED RW{rw}")
                self._last_sid_sched_sent.clear()

        if use_schedule:
            to_clear = self._last_sid_sched_sent - current_sched_sent
            for rw in sorted(to_clear):
                _emit(f"SATG_PROC_CLEAR_SIDSCHED RW{rw}")
            self._last_sid_sched_sent = current_sched_sent

        if self.dest_enable.isChecked():
            current_dest_sent = set()
            for proc_path, dests in self._destinations.items():
                if proc_path not in self._proc_files:
                    continue
                if not dests:
                    continue
                label = os.path.splitext(os.path.basename(proc_path))[0]
                _emit("SATG_PROC_SET_DEST " + label + " " + " ".join(dests))
                current_dest_sent.add(label)
            to_clear_dest = self._last_dest_sent - current_dest_sent
            for label in sorted(to_clear_dest):
                _emit(f"SATG_PROC_SET_DEST {label}")
            self._last_dest_sent = current_dest_sent
        else:
            if self._last_dest_sent:
                for label in sorted(self._last_dest_sent):
                    _emit(f"SATG_PROC_SET_DEST {label}")
                self._last_dest_sent.clear()
        for rw in active_runways:
            row = self._sid_rate_rows.get(rw)
            if not row:
                continue
            _, spin = row
            rate = int(spin.value())
            _emit(f"SATG_PROC_CFG_SIDRATE RW{rw} {rate}")

        basis_name = self._basis_name(star_rate_basis_idx)
        self._capture_star_rates(basis_name)
        self._star_basis_index = star_rate_basis_idx

        _emit(
            f"SATG_PROC_CFG_STAR {star_n} {star_minsep} {star_alt_fl} {star_mach:.2f} "
            f"{1 if star_mode_schedule else 0} {star_rate_basis_idx} {star_final_alt_fl} {star_final_spd}"
        )

        rate_store = self._star_rate_values.setdefault(basis_name, {})
        for key in sorted(self._star_rate_rows.keys()):
            row = self._star_rate_rows.get(key)
            if not row:
                continue
            _, spin = row
            rate = int(spin.value())
            rate_store[key] = rate
            _emit(f"SATG_PROC_CFG_STARRATE {key} {rate}")

        current_star_sched_sent = set()
        if star_mode_schedule:
            for path in star_procs:
                cfg = self._star_schedule_data.get(path)
                if not cfg:
                    continue
                caps = [int(c) for c in cfg.get("caps", [])]
                if sum(caps) <= 0:
                    continue
                start = int(round(float(cfg.get("start", 0.0))))
                end = int(round(float(cfg.get("end", start + 15))))
                if end <= start:
                    end = start + 15 * len(caps)
                caps_str = " ".join(str(int(c)) for c in caps)
                if not caps_str:
                    continue
                label = os.path.splitext(os.path.basename(path))[0]
                _emit(f"SATG_PROC_CFG_STARSCHED {label} {start} {end} {caps_str}")
                current_star_sched_sent.add(label)
            to_clear_star = self._last_star_sched_sent - current_star_sched_sent
            for label in sorted(to_clear_star):
                _emit(f"SATG_PROC_CLEAR_STARSCHED {label}")
            self._last_star_sched_sent = current_star_sched_sent
        else:
            if self._last_star_sched_sent:
                for label in sorted(self._last_star_sched_sent):
                    _emit(f"SATG_PROC_CLEAR_STARSCHED {label}")
                self._last_star_sched_sent.clear()

        # strictly positional to satisfy BlueSky argparser
        cmd = f"SATG_PROC_MAKE {name} {total} {rnm} {env} {overall_min} {s} {ow}"
        _emit(cmd)
        return True

    def _run_only(self):
        if not self._ensure_origin_ready():
            return
        name = self.scn.text().strip()
        if not name:
            _emit("ECHO Provide a scenario name.")
            return
        _emit("SATG_PROC_RUN " + name)

    def _make_and_run(self):
        if self._make():
            self._run_only()

# --- Help tab -------------------------------------------------------------
class HelpTab(QWidget):
    """Read-only help inside the GUI with clear descriptions and examples."""
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setMinimumHeight(300)

        help_text = (
            "SATG Help\n"
            "=========\n"
            "\n"
            "Overview\n"
            "--------\n"
            "SATG is a scenario generator for BlueSky. It can create random conflicts,\n"
            "geometric conflicts, realistic replays, and procedure based traffic. You can\n"
            "use the GUI tabs or the console commands described below.\n"
            "\n"
            "Paths and scenario files\n"
            "------------------------\n"
            "- Use the Set Base button to choose where scenarios are written.\n"
            "- When loading other .scn files inside a scenario, SATG writes PCALL lines\n"
            "  with absolute paths so BlueSky can resolve them reliably.\n"
            "\n"
            "Random conflicts in a circle (GUI tab: Random Conflicts)\n"
            "--------------------------------------------------------\n"
            "The Random Conflicts tab has been modernized to use the geometric conflicts\n"
            "architecture. It provides separate configuration for absolute (CPA-based)\n"
            "and relative (target-intruder) conflict generation within a circular region.\n"
            "\n"
            "Batch Settings:\n"
            "- Scenario name, number of conflicts, seed, circle location and radius\n"
            "- Separation minima (HSEP/VSEP) that apply to all generated conflicts\n"
            "\n"
            "Absolute Conflicts Column:\n"
            "- Enable/disable absolute conflict generation\n"
            "- Encounter types: head-on, crossing, overtake\n"
            "- TCPA range [s]: time to closest point of approach\n"
            "- Cross angle range [deg]: relative bearing for crossing encounters\n"
            "- Flight level and CAS ranges for aircraft generation\n"
            "- Aircraft types list\n"
            "\n"
            "Relative Conflicts Column:\n"
            "- Enable/disable relative conflict generation\n"
            "- Time to loss of separation [s]: when conflicts will occur\n"
            "- Relative bearing change [deg]: how tracks converge\n"
            "- Altitude and speed ranges for target and intruder aircraft\n"
            "- Aircraft types list\n"
            "\n"
            "Backend Commands:\n"
            "Uses SATG_RC_CIRCLE with mode=abs|rel|mix parameter\n"
            "Command: SATG_RC_CIRCLE name N types center_lat center_lon radius_nm mode altmode tcpa fl cas actypes overwrite [angle]\n"
            "  mode: abs|rel|mix (abs=absolute CPA-based, rel=relative target+intruder, mix=random selection)\n"
            "\n"
            "Required\n"
            "- name: scenario name (file will be name.scn)\n"
            "- N: number of conflicts\n"
            "- types: comma list from headon,cross,overtake\n"
            "- center_lat, center_lon: circle center in degrees\n"
            "- radius_nm: circle radius in nautical miles\n"
            "- altmode: level, altcross, or mix\n"
            "- tcpa: seconds as lo:hi (all aircraft spawn at t=0; tcpa sets time to CPA)\n"
            "- fl: flight level range lo:hi\n"
            "- cas: calibrated airspeed range lo:hi in kt\n"
            "- actypes: comma list of aircraft types to sample uniformly (for example A320,B738,E190)\n"
            "- overwrite: 1 overwrite the scenario file, 0 append to existing\n"
            "Optional\n"
            "- angle: crossing angle range lo:hi in degrees. Only used if cross is in types.\n"
            "\n"
            "Behavior\n"
            "- All aircraft are created at t=0. Larger tcpa means farther initial distance to the CPA point.\n"
            "- HSEP and VSEP for conflicts are set through the GUI before creation.\n"
            "- When appending, aircraft callsigns are auto-renamed to avoid duplicates.\n"
            "\n"
            "Geometric conflicts (GUI tab: Geometric Conflicts)\n"
            "--------------------------------------------------\n"
            "Flow in the GUI Create button:\n"
            "- First sets separation minima: SATG_GC_CONF HSEP VSEP\n"
            "- Then sets ranges: SATG_GC_RANGE fl=lo:hi cas=lo:hi\n"
            "- Then creates encounters: SATG_GC_CRE name=<...> typ=<...> altmode=<...> lat=<...> lon=<...> tcpa=<...> [angle=<...>] overwrite=0|1\n"
            "Run with: SATG_GC_RUN name\n"
            "\n"
            "Notes\n"
            "- Types are checkboxes for headon, crossing, overtake. Angle is only used for crossing.\n"
            "- Alt mode is Level, Alt-cross, or Mix when both are selected.\n"
            "- Overwrite checkbox controls whether to overwrite or append.\n"
            "\n"
            "Realistic replay (GUI tab: Realistic Replay)\n"
            "--------------------------------------------\n"
            "Create: SATG_RL_MAKE name overwrite\n"
            "Run:    SATG_RL_RUN name overwrite\n"
            "\n"
            "Options in the GUI\n"
            "- Jitter: if enabled, applies time jitter to only a selected percentage of flights.\n"
            "- Auto-delete at last waypoint: if enabled, the aircraft is deleted when it reaches the last waypoint.\n"
            "- Overwrite checkbox: when off, new flights are appended to the same scenario; callsigns are auto-renamed.\n"
            "- The scenario file is sorted by time after writing to avoid odd spawn timing.\n"
            "\n"
            "Procedures mode (GUI tab: Procedures)\n"
            "-------------------------------------\n"
            "Files\n"
            "- Load waypoint .scn that define DEFWPT fixes (for example DEFFIX.scn).\n"
            "- Load procedure .scn that contain a procedure name with %0 placeholder (for example SID-04-GORLO.scn).\n"
            "- For SID-XX-NAME.scn files, map the airport with SATG_PROC_SET_ICAO SID-XX-NAME ICAO.\n"
            "- Waypoint files are PCALLed once at time 0; each aircraft PCALLs its procedure right after creation.\n"
            "\n"
            "Create\n"
            "Command: SATG_PROC_MAKE name N radius_nm envelope_deg minsep seed overwrite\n"
            "Meaning\n"
            "- N: number of flights to spawn.\n"
            "- radius_nm: spawn radius around the first fix of the chosen procedure (ignored for SID files).\n"
            "- envelope_deg: angular envelope around the inbound direction to the first fix (ignored for SID files).\n"
            "  For generic procedures, the program samples a bearing inside that envelope so the aircraft arrives at the first fix,\n"
            "  not past it. Distance is sampled for uniform area inside the circle.\n"
            "- minsep: minimum time separation in seconds between successive spawns on the same procedure.\n"
            "- seed: 0 means random; a positive integer will produce repeatable results.\n"
            "- overwrite: 1 overwrites the scenario; 0 appends to it.\n"
            "\n"
            "Run\n"
            "Command: SATG_PROC_RUN name\n"
            "\n"
            "General tips\n"
            "------------\n"
            "- If you append to an existing scenario, the generator avoids callsign collisions by renaming.\n"
            "- Use Create & Run in the GUI to write the scenario and immediately load it in BlueSky.\n"
            "- SID procedures spawn from runway thresholds and auto-add TAKEOFF before the procedure call.\n"
            "- If a command says an argument is lo:hi, it means two numbers separated by a colon, for example 60:240.\n"
            "- If you see path errors during PCALL, verify the base folder and that the referenced .scn files exist.\n"
        )

        txt.setPlainText(help_text)
        lay.addWidget(txt)

# --- main window ------------------------------------------------------------

class SATGWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SATG GUI")
        self.resize(980, 720)
        layout = QVBoxLayout(self)

        tabs = QTabWidget(self)
        
        # Create tab instances and store references for visual indicator management
        self.help_tab = HelpTab(self)
        self.rl_tab = RLTab(self)
        self.gc_tab = GCTab(self)  # Has CPA reference visualization
        self.rc_tab = RCTab(self)  # Has circle visualization
        self.proc_tab = ProcTab(self)
        
        tabs.addTab(self.help_tab, "Help")
        tabs.addTab(self.rl_tab, "Realistic Replay")
        tabs.addTab(self.gc_tab, "Geometric Conflicts")
        tabs.addTab(self.rc_tab, "Random Conflicts")
        tabs.addTab(self.proc_tab, "Procedures")

        # Connect tab change event to manage visual indicators
        tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs = tabs  # Store reference for later use

        self.top = TopStrip(self)

        layout.addWidget(self.top)
        layout.addWidget(tabs, 1)

    def _on_tab_changed(self, index):
        """Handle tab changes and manage visual indicators."""
        current_tab = self.tabs.widget(index)
        
        # Hide all visual indicators first
        self._hide_all_indicators()
        
        # Show indicators for the current tab if they should be visible
        if current_tab == self.gc_tab:
            # Geometric Conflicts tab - show CPA reference if enabled
            self._show_gc_indicators()
        elif current_tab == self.rc_tab:
            # Random Conflicts tab - show circle if enabled
            self._show_rc_indicators()
            
    def _hide_all_indicators(self):
        """Hide all visual indicators from all tabs."""
        # Hide Random Conflicts circle
        if hasattr(self, 'rc_tab'):
            self.rc_tab._hide_circle()
            
        # Hide Geometric Conflicts CPA reference (absolute mode)
        if hasattr(self, 'gc_tab') and hasattr(self.gc_tab, '_minima'):
            # Access the absolute page through the GCTab structure
            gc_absolute_page = getattr(self.gc_tab, '_absolute_page', None)
            if gc_absolute_page and hasattr(gc_absolute_page, '_hide_cpa_reference'):
                gc_absolute_page._hide_cpa_reference()
    
    def _show_gc_indicators(self):
        """Show Geometric Conflicts indicators if they should be visible."""
        if hasattr(self, 'gc_tab') and hasattr(self.gc_tab, '_minima'):
            gc_absolute_page = getattr(self.gc_tab, '_absolute_page', None)
            if (gc_absolute_page and 
                hasattr(gc_absolute_page, 'show_cpa_cb') and 
                gc_absolute_page.show_cpa_cb.isChecked()):
                gc_absolute_page._show_cpa_reference()
    
    def _show_rc_indicators(self):
        """Show Random Conflicts indicators if they should be visible."""
        if (hasattr(self, 'rc_tab') and 
            hasattr(self.rc_tab, 'show_circle_cb') and 
            self.rc_tab.show_circle_cb.isChecked()):
            self.rc_tab._show_circle()

# single instance + lazy creation
_window = None
def _get_window():
    from PyQt6.QtWidgets import QApplication
    global _window
    if QApplication.instance() is None:
        raise RuntimeError("SATGGUI: GUI not ready; run BlueSky with GUI and call SATGGUI after startup.")
    if _window is None:
        _window = SATGWindow()
    return _window

# --- plugin hooks -----------------------------------------------------------

def init_plugin():
    return {'plugin_name': 'SATGGUI', 'plugin_type': 'gui'}

from bluesky import stack as _stack_mod  # ensure decorator import after init
@_stack_mod.command
def SATGGUI():
    _get_window().show()
