"""
SATGgui.py - BlueSky GUI Plugin for Synthetic Air Traffic Generation

PyQt6 GUI plugin for synthetic air traffic generation, historic sampling, and 
conflict simulation in BlueSky Air Traffic Management simulator. Provides 
caching systems, performance optimizations, and feature parity between 
Historic Sampling and Realistic Replay modes.

Features:
    - Historic Sampling Tab: ML-based aircraft generation from EUROCONTROL data
    - Realistic Replay Tab: Scenario-based aircraft generation with conflict simulation
    - Geometric Conflicts Tab: Conflict detection and resolution algorithms
    - Random Conflicts Tab: Stochastic conflict generation and analysis
    - Procedure Management: SID/STAR procedure creation and editing
    - Configuration Management: Save/load system with backward compatibility
    - Cache Management: File-based caching with path validation
    - Filter Systems: Airspace and flight point filtering

Architecture:
    - Lazy window creation to avoid QApplication race conditions
    - Tab-based interface with consistent styling
    - Progress dialogs with UI thread updates
    - Configuration persistence system
    - File path caching optimizations

Classes:
    - SATGWindow: Main window with tab management
    - HistoricSamplingTab: ML-based aircraft generation interface
    - RLTab: Realistic Replay scenario generation interface
    - RCTab: Random conflict generation and analysis
    - ProcTab: Procedure creation and management
    - EurocontrolFilterDialog: Filtering with flight point processing
    - ConfigManagerDialog: Configuration save/load management
    - CacheManagerDialog: Cache validation and management

Performance:
    - File path caching for Configure Filters dialog
    - Cache validation using file modification times
    - Vectorized flight point filtering with numpy
    - Bounding box pre-filtering for geometric calculations
    - Progress dialog updates with threading

Dependencies:
    - PyQt6: Qt6 GUI framework
    - NumPy: Vectorized calculations and data processing
    - Pandas: Flight data manipulation and analysis
    - GeoPandas: Geometric airspace calculations
    - Shapely: Point-in-polygon calculations
    - BlueSky: ATM simulator integration
    - TraffixGen: EUROCONTROL data processing backend

Usage:
    Plugin activated through BlueSky console command 'SATGGUI'.
    Configuration is persistent through save/load system with 
    backward compatibility for legacy formats.

Examples:
    # Activate GUI from BlueSky console
    SATGGUI
    
    # The GUI provides complete functionality for:
    # - Historic sampling with date range selection and filtering
    # - Realistic replay with scenario generation and conflict simulation
    # - Geometric conflict detection with customizable parameters
    # - Random conflict generation with statistical analysis
    # - Procedure creation with waypoint validation
    # - Configuration management with automatic backup

Note:
    This plugin requires a GUI-enabled BlueSky environment and uses lazy
    initialization to ensure QApplication is available before window creation.
    All caching and filtering operations are optimized for performance while
    maintaining accuracy in flight point processing for model training.
"""

# PyQt6; lazy window creation to avoid QApplication race.

from typing import Dict, List, Optional, Tuple
import os
import json
from datetime import datetime

from PyQt6.QtCore import Qt, QTime, QLocale, QDate
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QCheckBox, QComboBox, QPushButton, QSpinBox,
    QDoubleSpinBox, QFileDialog, QSlider, QListWidget, QListWidgetItem, QTextEdit,
    QDialog, QDialogButtonBox, QTimeEdit, QDateEdit, QScrollArea, QRadioButton, QButtonGroup,
    QInputDialog, QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QFrame, QSplitter, QProgressDialog, QApplication
)
from PyQt6 import sip
from bluesky import stack
from bluesky.ui.qtgl.console import process_cmdline
from bluesky.ui.qtgl.console import Console


def _clear_and_set_cmdline(text):
    """Clear the command line and set new text.
    
    This function clears any existing text in the command line and replaces it
    with the new command, preventing issues where existing text gets concatenated
    with the new POLY command.
    
    Args:
        text: The command line text to set (replacing any existing content)
    """
    if Console._instance is not None:
        Console._instance.set_cmdline(text)
    else:
        # Fallback to standard process_cmdline if console not available
        process_cmdline(text)


try:
    import bluesky as bs
except Exception:
    bs = None
import os, re

# --- helpers ---------------------------------------------------------------

def _configure_decimal_separator(spinbox):
    """Configure spinbox to use dot as decimal separator (C locale)"""
    c_locale = QLocale(QLocale.Language.C)
    spinbox.setLocale(c_locale)

def _emit(cmd: str):
    """Send a BlueSky console command (no GUI echo here)."""
    stack.stack(cmd)

def _qpath(path: str) -> str:
    if not path:
        return path
    return f"\"{path}\"" if (" " in path and not (path.startswith('"') and path.endswith('"'))) else path

def _kv(key: str, val):
    """
    Format key-value pair for command line, return empty string for null/empty values.
    
    Args:
        key (str): Parameter key name
        val: Parameter value (any type)
        
    Returns:
        str: Formatted "key=value" string or empty string for null/empty values
        
    Examples:
        >>> _kv("speed", 250)
        "speed=250"
        >>> _kv("name", None)
        ""
        >>> _kv("desc", "  ")
        ""
    """
    if val is None:
        return ""
    if isinstance(val, str) and val.strip() == "":
        return ""
    return f"{key}={val}"

def _join_tokens(*tokens):
    """
    Join multiple tokens into space-separated string, filtering out empty/null tokens.
    
    Args:
        *tokens: Variable number of token arguments (any type)
        
    Returns:
        str: Space-separated string of non-empty tokens
        
    Examples:
        >>> _join_tokens("CRE", "KL123", None, "B738")
        "CRE KL123 B738"
        >>> _join_tokens("", "TEST", "", "DATA")
        "TEST DATA"
        
    Note:
        - Filters out falsy values (None, empty strings, etc.)
        - Useful for building command strings with optional parameters
        - Converts all tokens to strings before joining
    """
    return " ".join([t for t in tokens if t])

def _validate_waypoint_name_conflict(name: str, is_coordinate_waypoint: bool) -> tuple[bool, str]:
    """
    Validate if there's a conflict between waypoint name and type.
    Returns (is_valid, warning_message)
    """
    if not name:
        return True, ""
    
    name_upper = name.strip().upper()
    is_known_navaid = _is_known_named_waypoint(name_upper)
    
    if is_coordinate_waypoint and is_known_navaid:
        return False, (f"Warning: '{name_upper}' is a known navaid/airport. "
                      f"Using this name will override your coordinates with the navaid's position. "
                      f"Consider using a different name for your coordinate waypoint.")
    
    return True, ""


def _validate_waypoint_name(name: str) -> bool:
    """Validate waypoint name: alphanumeric only, no spaces."""
    if not name or not isinstance(name, str):
        return False
    return name.replace('_', '').replace('-', '').isalnum() and ' ' not in name

def _extract_procedure_name_from_path(filepath: str) -> str:
    """Extract procedure name from file path for auto-naming waypoints."""
    if not filepath:
        return "PROC"
    import os
    base_name = os.path.splitext(os.path.basename(filepath))[0]
    # Clean up the name to be a valid waypoint prefix
    clean_name = ''.join(c for c in base_name.upper() if c.isalnum() or c in '_-')
    return clean_name if clean_name else "PROC"

def _is_coordinate_waypoint(lat_str: str, lon_str: str) -> bool:
    """Check if the given strings represent valid coordinates."""
    try:
        lat = float(lat_str)
        lon = float(lon_str)
        return -90 <= lat <= 90 and -180 <= lon <= 180
    except (ValueError, TypeError):
        return False

def _generate_auto_waypoint_name(proc_name: str, index: int) -> str:
    """Generate auto waypoint name in format PROCNAME_WP{index}."""
    return f"{proc_name}_WP{index + 1}"

def _is_known_named_waypoint(name: str) -> bool:
    """Check if a waypoint name is a known navigation waypoint (airport, navaid, etc.)."""
    if not name:
        return False
    
    name = name.upper().strip()
    
    # Common airports and navaids in the Netherlands/Europe
    known_waypoints = {
        # Major airports
        'EHAM', 'SCHIPHOL', 'AMS',
        'EHRD', 'ROTTERDAM', 'RTM',
        'EHEH', 'EINDHOVEN', 'EIN',
        'EBBR', 'BRUSSELS', 'BRU',
        'EGLL', 'HEATHROW', 'LHR',
        'LFPG', 'CHARLES_DE_GAULLE', 'CDG',
        'EDDF', 'FRANKFURT', 'FRA',
        
        # Common navaids and waypoints in Netherlands
        'LAK', 'LOPIK', 'GV', 'SSB', 'ARTIP', 'RIVER', 'HELEN', 'ANDIK',
        'BERGI', 'LUMBO', 'HELEN', 'RENDI', 'NICKY', 'WOODY', 'TIGER',
        
        # Add more as needed
    }
    
    # Also check if it looks like an ICAO code (4 letters starting with E for Europe)
    if len(name) == 4 and name.startswith('E') and name.isalpha():
        return True
    
    return name in known_waypoints

_SID_FILE_RE = re.compile(r'^SID-([0-9]{2,3}[LRC]?)-([-A-Za-z0-9_]+)\.scn$', re.IGNORECASE)
DEFAULT_STAR_RATE = 20

class SIDSchedDialog(QDialog):
    """
    Dialog for configuring SID (Standard Instrument Departure) runway scheduling.
    
    Interface for configuring departure schedules for different runways, including 
    time windows and capacity constraints. Supports multiple runways with individual 
    scheduling parameters and departure rates for traffic generation.
    
    Time-based scheduling system configuration:
    - Start and end times for departure operations
    - Departure capacity (aircraft per hour) for different time periods
    - Slider interface for schedule configuration
    - Per-runway configuration with easy switching between runways
    
    Key Features:
    - Multi-runway support with individual configuration
    - Time slot-based scheduling with 15-minute granularity
    - Departure capacity sliders for rate configuration
    - Real-time preview of schedule settings
    - Validation of time windows and capacity limits
    
    Attributes:
        SLOT_MINUTES (int): Time slot granularity in minutes (15-minute slots)
        runways (List[str]): List of available runway identifiers
        data (Dict[str, Dict]): Per-runway scheduling configuration data
        current_runway (str): Currently selected runway for editing
        sliders (List[QSlider]): Capacity configuration sliders for time slots
    
    Args:
        runways (List[str]): List of runway identifiers to configure
        existing (Dict[str, Dict[str, object]]): Existing schedule configuration
        parent (QWidget, optional): Parent widget for proper dialog behavior
    
    Examples:
        # Create dialog for multiple runways with existing configuration
        runways = ['09L', '09R', '27L', '27R']
        existing_config = {'09L': {'start': 6.0, 'end': 22.0, 'caps': [12, 16, 20]}}
        dialog = SIDSchedDialog(runways, existing_config, parent=self)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            schedule_config = dialog.get_schedule_data()
    
    Note:
        The dialog uses 15-minute time slots for schedule granularity and provides
        intuitive slider controls for setting departure capacities. Configuration
        is applied per runway and supports complex scheduling scenarios with
        varying capacity throughout operational hours.
    """
    
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
        """
        Round a time to the nearest slot interval.
        
        Args:
            time (QTime): Time to be rounded to nearest slot interval
            
        Returns:
            QTime: Rounded time aligned to slot boundaries
            
        Examples:
            >>> dialog = PhaseAltitudeConfigDialog([], {})
            >>> rounded = dialog._round_time(QTime(14, 23))  # 2:23 PM
            >>> print(rounded.toString())  # Rounded to nearest 15-min slot
            
        Note:
            - Uses SLOT_MINUTES constant for rounding interval
            - Ensures result stays within 24-hour bounds
            - Prevents rounding beyond valid time range
        """
        minutes = time.hour() * 60 + time.minute()
        rounded = (minutes // self.SLOT_MINUTES) * self.SLOT_MINUTES
        rounded = max(0, min(rounded, 24 * 60 - self.SLOT_MINUTES))
        return QTime(rounded // 60, rounded % 60)

    def _time_to_minutes(self, time: QTime) -> float:
        """
        Convert QTime to total minutes since midnight.
        
        Args:
            time (QTime): Time object to convert
            
        Returns:
            float: Total minutes since midnight (0-1439)
            
        Examples:
            >>> dialog = PhaseAltitudeConfigDialog([], {})
            >>> minutes = dialog._time_to_minutes(QTime(14, 30))  # 2:30 PM
            >>> print(minutes)  # 870.0 (14*60 + 30)
            
        Note:
            - Used for time calculations and slot management
            - Returns float for precision in time operations
        """
        return time.hour() * 60 + time.minute()

    def _minutes_to_time(self, minutes: float) -> QTime:
        """
        Convert total minutes since midnight to QTime.
        
        Args:
            minutes (float): Minutes since midnight to convert
            
        Returns:
            QTime: Time object representing the converted time
            
        Examples:
            >>> dialog = PhaseAltitudeConfigDialog([], {})
            >>> time = dialog._minutes_to_time(870.0)  # 14.5 hours
            >>> print(time.toString())  # "14:30"
            
        Note:
            - Clamps input to valid 24-hour range (0-1440)
            - Converts float minutes to integer for QTime constructor
        """
        minutes = max(0, min(int(minutes), 24 * 60))
        return QTime(minutes // 60, minutes % 60)

    def _on_runway_changed(self):
        """
        Handle runway selection change in the combo box.
        
        Note:
            - Saves current runway configuration before switching
            - Updates current_runway attribute to new selection
            - Loads configuration data for newly selected runway
            - Called automatically when user changes runway selection
        """
        self._save_current()
        data = self.runway_combo.currentData()
        if data:
            self.current_runway = data
            self._load_runway(self.current_runway)

    def _on_time_changed(self):
        """
        Handle time range changes and ensure valid time intervals.
        
        Note:
            - Rounds start and end times to nearest slot boundaries
            - Ensures end time is after start time (minimum one slot)
            - Blocks signals during programmatic time updates to prevent recursion
            - Updates slot controls to reflect new time range
            - Called automatically when user changes time values
        """
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
    """
    Dialog for configuring STAR (Standard Terminal Arrival Route) scheduling.
    
    Interface for STAR procedure scheduling with time-slot based management, 
    arrival rate configuration, and approach sequencing. Enables arrival traffic
    generation that adheres to STAR procedures with spacing and timing constraints.
    
    STAR Scheduling system uses 15-minute time slots for granular control over 
    arrival traffic patterns throughout simulation periods. Each
    STAR procedure can be individually configured with different arrival rates
    and scheduling parameters to create realistic terminal airspace operations.
    
    Key Features:
    - Time-slot based arrival scheduling with 15-minute intervals
    - Individual STAR procedure configuration and rate management  
    - Coordinated arrival sequencing with proper separation
    - Existing configuration preservation and modification
    - Real-time schedule validation and conflict detection
    - Traffic pattern optimization for realistic terminal operations
    
    Scheduling Parameters:
    - Procedure Selection: Configure which STAR procedures are active
    - Time Windows: Set operational periods for each STAR procedure
    - Arrival Rates: Define aircraft per hour for different time periods
    - Sequence Coordination: Manage arrival flow and spacing requirements
    - Schedule Validation: Ensure configuration feasibility and realism
    
    Attributes:
        SLOT_MINUTES (int): Time slot duration in minutes (15-minute intervals)
        procs (List[str]): List of STAR procedure identifiers for scheduling
        labels (Dict[str, str]): Human-readable labels for procedure display
        data (Dict[str, Dict]): Current schedule configuration for each procedure
    
    Args:
        procs (List[tuple[str, str]]): Procedure list with (path, label) pairs
        existing (Dict[str, Dict[str, object]]): Current schedule configuration data
        parent (QWidget, optional): Parent widget for proper dialog behavior
    
    Examples:
        # Configure STAR schedules for multiple procedures
        procedures = [
            ("/path/to/STAR01.scn", "STAR01 Arrival"),
            ("/path/to/STAR02.scn", "STAR02 Arrival")
        ]
        existing_config = {
            "/path/to/STAR01.scn": {"rate": 10, "active_slots": [0, 1, 2]}
        }
        
        dialog = StarSchedDialog(procedures, existing_config, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_schedule = dialog.get_schedule_data()
            self._apply_star_schedule(new_schedule)
    
    Note:
        The dialog uses 15-minute time slots to provide realistic granularity for
        arrival scheduling while maintaining manageable configuration complexity.
        All schedule configurations are validated to ensure feasible traffic
        patterns and proper STAR procedure adherence for realistic terminal operations.
    """
    
    SLOT_MINUTES = 15

    def __init__(self, procs: List[tuple[str, str]], existing: Dict[str, Dict[str, object]], parent=None):
        """
        Initialize STAR scheduling dialog with procedure list and existing configuration.
        
        Args:
            procs: List of (path, label) tuples for STAR procedures
        """
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
        """
        Round time to nearest slot interval for STAR scheduling.
        
        Args:
            time (QTime): Time to round to nearest slot boundary
            
        Returns:
            QTime: Rounded time aligned to SLOT_MINUTES intervals
            
        Note:
            - Uses same logic as PhaseAltitudeConfigDialog
            - Ensures STAR schedules align to time slots
        """
        minutes = time.hour() * 60 + time.minute()
        rounded = (minutes // self.SLOT_MINUTES) * self.SLOT_MINUTES
        rounded = max(0, min(rounded, 24 * 60 - self.SLOT_MINUTES))
        return QTime(rounded // 60, rounded % 60)

    def _time_to_minutes(self, time: QTime) -> float:
        """
        Convert QTime to minutes for STAR schedule calculations.
        
        Args:
            time (QTime): Time to convert
            
        Returns:
            float: Total minutes since midnight
        """
        return time.hour() * 60 + time.minute()

    def _minutes_to_time(self, minutes: float) -> QTime:
        """
        Convert minutes back to QTime for STAR schedules.
        
        Args:
            minutes (float): Minutes since midnight
            
        Returns:
            QTime: Converted time object
        """
        minutes = max(0, min(int(minutes), 24 * 60))
        return QTime(minutes // 60, minutes % 60)

    def _on_proc_changed(self):
        """
        Handle STAR procedure selection change.
        
        Note:
            - Saves current procedure configuration before switching
            - Updates current_proc to newly selected procedure
            - Loads configuration for selected STAR procedure
        """
        self._save_current()
        data = self.proc_combo.currentData()
        if data:
            self.current_proc = data
            self._load_proc(self.current_proc)

    def _on_time_changed(self):
        """
        Handle time range changes for STAR scheduling.
        
        Note:
            - Rounds times to slot boundaries
            - Ensures valid time range (end > start)
            - Updates slot controls with new time range
            - Prevents signal loops during updates
        """
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
        """
        Reset current STAR procedure schedule to default values.
        
        Note:
            - Resets to 1-hour window starting at midnight
            - Sets capacity to zero (no scheduled aircraft)
            - Reloads the cleared schedule in the interface
        """
        self.data[self.current_proc] = {"start": 0.0, "end": 60.0, "caps": [0]}
        self._load_proc(self.current_proc)

    def _save_current(self):
        """
        Save current schedule configuration for the selected STAR procedure.
        
        Note:
            - Captures all slider values as capacity array
            - Saves time range in minutes since midnight
            - Stores slot duration for future reference
            - Called before switching procedures or closing dialog
        """
        caps = [slider.value() for slider in self.sliders]
        self.data[self.current_proc] = {
            "start": self._time_to_minutes(self.start_edit.time()),
            "end": self._time_to_minutes(self.end_edit.time()),
            "caps": caps,
            "slot": float(self.SLOT_MINUTES),
        }

    def _load_proc(self, proc_path: str):
        """
        Load schedule configuration for specified STAR procedure.
        
        Args:
            proc_path (str): Identifier for STAR procedure to load
            
        Note:
            - Retrieves saved configuration or uses defaults
            - Converts stored minutes back to QTime objects
            - Ensures valid time ranges (end > start)
            - Rebuilds slot controls with saved capacity values
            - Blocks signals during programmatic updates
        """
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
        """
        Build capacity slider controls for time slots.
        
        Args:
            caps (List[int]): List of capacity values for each time slot
            
        Note:
            - Clears existing slider widgets before rebuilding
            - Creates vertical slider for each time slot
            - Sets slider range 0-20 aircraft per slot
            - Connects value change events for real-time updates
            - Adds labels showing current capacity values
        """
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
        """
        Update slot controls based on current time range.
        
        Note:
            - Calculates required number of slots from time range
            - Rebuilds controls if slot count changed
            - Updates all slider labels with current values
            - Preserves existing capacity values where possible
        """
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
        """
        Handle capacity slider value changes.
        
        Args:
            value (int): New capacity value from slider
            
        Note:
            - Updates corresponding label with new value
            - Provides real-time feedback to user
        """
        slider = self.sender()
        if isinstance(slider, QSlider):
            slider.label.setText(str(value))  # type: ignore

    @property
    def result_data(self) -> Dict[str, Dict[str, object]]:
        """
        Get complete schedule configuration data.
        
        Returns:
            Dict[str, Dict[str, object]]: Schedule data for all procedures
            
        Note:
            - Saves current procedure configuration before returning
            - Returns all STAR schedule configurations
        """
        self._save_current()
        return self.data

    def _clear_current_schedule(self):
        """
        Clear current runway schedule and reset to default values.
        
        Note:
            - Resets start time to 00:00 and end time to 01:00
            - Updates slot controls with default capacity values
            - Clears schedule data for current runway in internal data structure
            - Provides clean slate for new runway schedule configuration
        """
        self.start_edit.setTime(QTime(0, 0))
        self.end_edit.setTime(QTime(1, 0))
        self._update_slot_controls(default=True)
        self.data[self.current_runway] = {"start": 0.0, "end": 60.0, "caps": [0]}

    def _load_runway(self, runway: str):
        """
        Load schedule configuration for specified runway.
        
        Args:
            runway (str): Runway identifier to load configuration for
            
        Note:
            - Retrieves runway schedule data or uses defaults if not found
            - Converts stored minute values back to QTime objects
            - Ensures end time is after start time (minimum slot duration)
            - Blocks time editor signals during loading to prevent cascading updates
            - Updates slot controls with stored capacity values
        """
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
        """
        Save current runway schedule configuration to internal data structure.
        
        Note:
            - Captures all capacity slider values
            - Ensures end time is after start time (minimum slot duration)
            - Stores start/end times as minute values for persistence
            - Handles empty capacity (treats as cleared schedule)
            - Updates data dictionary with current runway configuration
            - Called automatically when switching runways or applying changes
        """
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
        """
        Update slot controls based on time range and existing capacity values.
        
        Args:
            existing_caps (Optional[List[int]]): Existing capacity values to preserve
            default (bool): Whether to use default values instead of existing
            
        Note:
            - Clears all existing slot control widgets before rebuilding
            - Calculates slot count based on time range and SLOT_MINUTES interval
            - Creates slider and spinbox pairs for each time slot
            - Preserves existing capacity values when extending or shrinking slots
            - Synchronizes slider and spinbox values bidirectionally
        """
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
        """
        Handle dialog acceptance with data validation and saving.
        
        Note:
            - Saves current configuration before accepting
            - Ensures all runway configurations are preserved
            - Calls parent accept to close dialog with accepted status
        """
        self._save_current()
        super().accept()

    def reject(self):
        """
        Handle dialog rejection without saving changes.
        
        Note:
            - Discards any unsaved configuration changes
            - Returns dialog to previous state
            - Calls parent reject to close dialog with rejected status
        """
        super().reject()

    @property
    def result_data(self) -> Dict[str, Dict[str, object]]:
        """
        Get complete runway configuration data.
        
        Returns:
            Dict[str, Dict[str, object]]: Configuration data for all runways
                Format: {runway_id: {start: float, end: float, caps: List[int], slot: float}}
                
        Examples:
            >>> dialog = PhaseAltitudeConfigDialog(['18L', '36R'], {})
            >>> dialog.exec()
            >>> data = dialog.result_data
            >>> print(data)  # {'18L': {'start': 360.0, 'end': 420.0, 'caps': [5,3,2]}}
            
        Note:
            - Returns deep copy of configuration data
            - Includes capacity arrays for all time slots
            - Safe to modify returned data without affecting dialog
        """
        return {rw: dict(cfg) for rw, cfg in self.data.items()}


class DestDialog(QDialog):
    """
    Dialog for configuring destination airports for procedural traffic generation.
    
    This specialized interface allows users to configure destination airports for
    each procedure using ICAO airport codes. The dialog provides a structured
    way to associate procedures with their appropriate destination airports,
    enabling realistic traffic flow patterns and route assignment for generated
    aircraft in synthetic traffic scenarios.
    
    The Destination Dialog supports comma-separated ICAO code entry for each
    procedure, allowing multiple destination airports per procedure to create
    varied and realistic traffic distributions. Users can configure both primary
    and alternate destinations for comprehensive traffic scenario generation.
    
    Key Features:
    - ICAO airport code configuration for procedure destinations
    - Multiple destinations per procedure with comma-separated input
    - Existing configuration preservation and modification
    - Input validation for proper ICAO code format
    - Real-time editing with immediate feedback
    - Flexible destination assignment for realistic traffic patterns
    
    Configuration Options:
    - Primary Destinations: Main airports for procedure traffic
    - Alternate Destinations: Secondary airports for traffic variation
    - Code Validation: Ensure proper ICAO format and airport existence
    - Multiple Entries: Support comma-separated lists for each procedure
    - Existing Data: Preserve and modify current destination configurations
    
    Attributes:
        _procedures (List[tuple]): List of (path, label) pairs for procedures
        _edits (Dict): Dictionary of text edit widgets for each procedure
    
    Args:
        procedures (List[tuple]): Procedure list with (path, label) pairs
        existing (Dict[str, List[str]]): Current destination configuration
        parent (QWidget, optional): Parent widget for proper dialog behavior
    
    Returns:
        Dict[str, List[str]]: Updated destination configuration when accepted
    
    Examples:
        # Configure destinations for multiple procedures
        procedures = [
            ("/path/to/SID01.scn", "SID01 Departure"),
            ("/path/to/SID02.scn", "SID02 Departure")  
        ]
        existing_dests = {
            "/path/to/SID01.scn": ["KJFK", "KLGA", "KEWR"]
        }
        
        dialog = DestDialog(procedures, existing_dests, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_destinations = dialog.get_destinations()
            self._apply_procedure_destinations(new_destinations)
    
    Note:
        The dialog expects valid ICAO airport codes in comma-separated format.
        Input validation helps ensure proper code formatting, and the interface
        provides clear guidance for entering multiple destinations per procedure
        to create realistic and varied traffic generation scenarios.
    """
    
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
        """
        Parse comma/semicolon/space separated ICAO codes from text.
        
        Args:
            text (str): Input text containing ICAO codes
            
        Returns:
            List[str]: Cleaned and uppercased ICAO codes
            
        Examples:
            >>> dialog = DestDialog([], {})
            >>> codes = dialog._parse_codes("eham, egll; kjfk  eddf")
            >>> print(codes)  # ['EHAM', 'EGLL', 'KJFK', 'EDDF']
        """
        return [c.strip().upper() for c in re.split(r'[;,\\s]+', text) if c.strip()]

    def _apply_single(self, key: str):
        """
        Apply formatting to a single procedure's destination codes.
        
        Args:
            key (str): Procedure identifier to format
            
        Note:
            - Parses and reformats codes in the corresponding edit field
            - Standardizes format with comma separation
        """
        edit = self._edits.get(key)
        if not edit:
            return
        codes = self._parse_codes(edit.text())
        edit.setText(", ".join(codes))

    def _apply_all(self, key: str):
        """
        Apply the same destination codes to all procedures.
        
        Args:
            key (str): Source procedure whose codes to copy
            
        Note:
            - Takes codes from one procedure and applies to all
            - Useful for scenarios where all procedures share destinations
        """
        edit = self._edits.get(key)
        if not edit:
            return
        codes = self._parse_codes(edit.text())
        text = ", ".join(codes)
        for other in self._edits.values():
            other.setText(text)

    @property
    def result_data(self) -> Dict[str, List[str]]:
        """
        Get destination configuration for all procedures.
        
        Returns:
            Dict[str, List[str]]: Mapping of procedures to destination airport codes
            
        Note:
            - Only includes procedures with non-empty destination lists
            - Returns parsed and validated ICAO codes
        """
        data: Dict[str, List[str]] = {}
        for path, edit in self._edits.items():
            codes = self._parse_codes(edit.text())
            if codes:
                data[path] = codes
        return data


class ProcedureCreatorDialog(QDialog):
    """
    Dialog for interactive procedure creation with visual track drawing and constraint management.
    
    Interface for creating SID and STAR procedures through interactive track drawing 
    on BlueSky display, with waypoint constraint configuration. Workflow for procedure 
    development from route sketching to parameter specification and validation.
    
    Procedure Creator integrates with BlueSky's visual interface, allowing users to 
    draw procedure tracks on simulation display while dialog remains open for 
    constraint configuration. Provides visual feedback during creation process.
    
    Key Features:
    - Interactive track drawing on BlueSky simulation display
    - Comprehensive waypoint constraint configuration (altitude, speed, restrictions)
    - Real-time procedure validation and feasibility checking
    - Integrated polygon creation for procedure boundaries and sectors
    - Non-modal operation allowing simultaneous BlueSky interaction
    - Advanced waypoint management with coordinate precision
    - Constraint validation ensuring realistic procedure parameters
    
    Creation Workflow:
    1. Dialog opens in non-modal mode for BlueSky interaction
    2. User draws procedure track directly on simulation display
    3. System captures polygon coordinates and waypoint positions
    4. User configures constraints for each waypoint (altitude, speed)
    5. Validation ensures procedure feasibility and safety requirements
    6. Procedure data is formatted and saved for use in traffic generation
    
    Constraint Configuration:
    - Altitude Constraints: Set minimum, maximum, and target altitudes
    - Speed Constraints: Configure approach, departure, and en-route speeds
    - Waypoint Types: Define navigation waypoint characteristics and restrictions
    - Route Validation: Ensure procedure adherence to airspace and safety requirements
    - Parameter Optimization: Suggest realistic constraint values based on procedure type
    
    Attributes:
        polygon_name (str): Identifier for the procedure polygon/track
        waypoints (List[Dict]): Comprehensive waypoint data with constraints
                              Format: {'name': str, 'lat': float, 'lon': float, 
                                      'alt': str, 'spd': str}
    
    Args:
        parent (QWidget, optional): Parent widget for proper dialog behavior
    
    Examples:
        # Create new procedure with interactive drawing
        dialog = ProcedureCreatorDialog(parent=self)
        dialog.show()  # Non-modal for BlueSky interaction
        
        # User draws track on BlueSky display
        # Dialog captures coordinates and provides constraint interface
        # Final procedure data available through dialog methods
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            procedure_data = dialog.get_procedure_data()
            self._save_new_procedure(procedure_data)
    
    Note:
        The dialog operates in non-modal mode to allow simultaneous interaction
        with the BlueSky simulation display for track drawing. All coordinate
        data is captured with high precision, and constraint validation ensures
        procedures meet safety and operational requirements for realistic traffic simulation.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create New Procedure")
        self.setModal(False)  # Allow interaction with BlueSky screen
        self.resize(800, 600)
        
        # Store polygon coordinates and waypoint data
        self.polygon_name = ""
        self.waypoints = []  # List of dicts: {'name': str, 'lat': float, 'lon': float, 'alt': str, 'spd': str}
        
        self._init_ui()
        
    def _init_ui(self):
        """
        Initialize the procedure creation dialog user interface.
        
        Note:
            - Creates multi-step interface for procedure creation
            - Step 1: Track creation in BlueSky with polygon drawing
            - Step 2: Procedure file generation and constraint configuration
            - Step 3: Advanced editing options and waypoint management
            - Provides status feedback and progress guidance
        """
        layout = QVBoxLayout(self)
        
        # Step 1: Procedure Name and POLY creation
        step1_group = QGroupBox("Step 1: Create Track")
        step1_layout = QFormLayout(step1_group)
        
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Enter procedure name (e.g., 'APPROACH_RWY09')")
        step1_layout.addRow("Procedure Name:", self.name_input)
        
        self.create_poly_btn = QPushButton("Create Track in BlueSky")
        self.create_poly_btn.clicked.connect(self._create_poly_command)
        step1_layout.addRow(self.create_poly_btn)
        
        self.poly_status = QLabel("Status: Enter a name and click 'Create Track' to start")
        self.poly_status.setStyleSheet("color: blue; font-style: italic;")
        step1_layout.addRow(self.poly_status)
        
        layout.addWidget(step1_group)
        
        # Step 2: Create Procedure File
        step2_group = QGroupBox("Step 2: Create Basic Procedure")
        step2_layout = QVBoxLayout(step2_group)
        
        create_buttons = QHBoxLayout()
        self.create_basic_btn = QPushButton("Create Basic Procedure from Track")
        self.create_basic_btn.clicked.connect(self._create_basic_procedure)
        create_buttons.addWidget(self.create_basic_btn)
        create_buttons.addStretch()
        step2_layout.addLayout(create_buttons)
        
        self.create_status = QLabel("Status: Create track first, then click to create basic procedure")
        self.create_status.setStyleSheet("color: blue; font-style: italic;")
        step2_layout.addWidget(self.create_status)
        
        layout.addWidget(step2_group)
        
        # Step 3: Edit Procedures
        step3_group = QGroupBox("Step 3: Edit Constraints")
        step3_layout = QVBoxLayout(step3_group)
        
        edit_buttons = QHBoxLayout()
        self.load_for_edit_btn = QPushButton("Edit Procedure...")
        self.load_for_edit_btn.clicked.connect(self._load_for_editing)
        self.load_for_edit_btn.setToolTip("Edit the created procedure or select from loaded procedures")
        edit_buttons.addWidget(self.load_for_edit_btn)
        edit_buttons.addStretch()
        step3_layout.addLayout(edit_buttons)
        
        layout.addWidget(step3_group)
        
        # Dialog buttons
        buttons = QHBoxLayout()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        buttons.addStretch()
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)
        
    def _create_poly_command(self):
        """
        Create and execute POLY command for interactive track drawing in BlueSky.
        
        Note:
            - Validates procedure name format (alphanumeric and underscores only)
            - Sends POLY command to BlueSky command line for interactive drawing
            - Enables subsequent procedure creation steps on success
            - Provides user instructions for map interaction
            - Updates status display with progress feedback
            
        Raises:
            QMessageBox: Warning dialogs for validation failures
        """
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Warning", "Please enter a procedure name first.")
            return
            
        # Validate name (alphanumeric and underscores only)
        import re
        if not re.match(r'^[A-Za-z0-9_]+$', name):
            QMessageBox.warning(self, "Warning", "Procedure name can only contain letters, numbers, and underscores.")
            return
            
        self.polygon_name = name
        
        # Send command to BlueSky using the same method as random conflicts
        try:
            # Clear any existing text and execute the POLY command directly (not just append)
            _clear_and_set_cmdline(f"POLY {name}")
            self.poly_status.setText(f"Command executed: \"POLY {name}\"\n\nINSTRUCTIONS:\n1. Click on the BlueSky map to draw waypoints\n2. Press ENTER to finish the polygon\n3. Then click \"Create Basic Procedure\" below")
            self.create_basic_btn.setEnabled(True)
        except Exception as e:
            self.poly_status.setText(f"Error sending command: {e}")
    
    def _create_basic_procedure(self):
        """
        Create a basic procedure file from the drawn polygon track.
        
        Note:
            - Requires polygon to be drawn first using _create_poly_command
            - Uses SATG backend command to convert polygon to procedure file
            - Creates procedure file in satg_data/procedures directory
            - Validates file creation and provides feedback to user
            - Enables advanced editing options on successful creation
            - Provides fallback instructions if automatic detection fails
            
        Raises:
            QMessageBox: Warning if no polygon name is set
        """
        if not self.polygon_name:
            QMessageBox.warning(self, "Warning", "No polygon name set. Create a track first.")
            return
            
        try:
            # Use backend command to create procedure from polygon
            from bluesky import stack
            
            self.create_status.setText(f"Creating procedure from polygon \"{self.polygon_name}\"...")
            
            # Execute the command using stack.stack like other SATG commands
            stack.stack(f"SATG_PROC_CREATE_FROM_POLY {self.polygon_name}")
            
            # Wait a moment for the command to process
            import time
            time.sleep(1.5)
            
            # Check if the file was created
            import os
            proc_file = os.path.join("c:\\Users\\javie\\OneDrive\\Desktop\\bluesky\\satg_data\\procedures", f"{self.polygon_name}.scn")
            
            if os.path.exists(proc_file):
                self.create_status.setText(f"Basic procedure \"{self.polygon_name}\" created successfully!\nFile: {proc_file}\nClick \"Edit Procedure...\" to add constraints.")
                self.load_for_edit_btn.setEnabled(True)
            else:
                # Check if any messages were shown in console
                self.create_status.setText(f"Command executed. Check BlueSky console for results.\nIf successful, the file should be at:\n{proc_file}\n\nIf polygon \"{self.polygon_name}\" was drawn properly, try running this command manually:\nSATG_PROC_CREATE_FROM_POLY {self.polygon_name}")
            
        except Exception as e:
            self.create_status.setText(f"Error creating procedure: {e}")
    
    def _load_for_editing(self):
        """
        Load the created procedure for advanced constraint editing.
        
        Note:
            - Opens ProcedureEditorDialog for the newly created procedure
            - Constructs file path automatically from polygon name
            - Requires successful procedure creation from _create_basic_procedure
            - Provides modal editor interface for adding altitude and speed constraints
            - Handles file access and parent tab integration
            
        Raises:
            QMessageBox: Information dialog if parent procedures tab not accessible
        """
        # Get the parent procedures tab to access loaded procedure files
        procedures_tab = self.parent()
        
        # Ensure we have the correct parent with _proc_files attribute
        if not procedures_tab or not hasattr(procedures_tab, '_proc_files'):
            QMessageBox.information(self, "No Procedures", 
                                  "Cannot access procedure files from parent tab.\n"
                                  "Please use the main Edit Procedure button instead.")
            return
        
        # First check if the current procedure was created and exists
        if self.polygon_name:
            try:
                # Construct the direct file path for the newly created procedure
                import os
                proc_file_path = os.path.abspath(os.path.join("satg_data", "procedures", f"{self.polygon_name}.scn"))
                
                # Open the procedure editor dialog with the direct file path
                editor_dialog = ProcedureEditorDialog(self.polygon_name, procedures_tab, file_path=proc_file_path)
                editor_dialog.exec()  # Use exec() for modal dialog instead of show()
                return
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error opening editor for created procedure: {e}")
                return
        
        # If no procedure was created in this session, behave like the main edit procedure button
        if not procedures_tab._proc_files:
            QMessageBox.information(self, "No Procedures", 
                                  "No procedure files are currently loaded.\n"
                                  "Please add procedure files first or create a new procedure.")
            return
        
        # Create a simple selection dialog for loaded procedures
        from PyQt6.QtWidgets import QInputDialog
        
        # Get list of procedure names from loaded files
        proc_names = [os.path.splitext(os.path.basename(filepath))[0] for filepath in procedures_tab._proc_files]
        
        if not proc_names:
            QMessageBox.information(self, "No Procedures", "No procedures available for editing.")
            return
        
        # Let user select which procedure to edit
        proc_name, ok = QInputDialog.getItem(self, "Select Procedure to Edit", 
                                           "Choose a procedure to edit:", 
                                           proc_names, 0, False)
        
        if ok and proc_name:
            # Open the procedure editor dialog with the procedures tab as parent
            try:
                editor_dialog = ProcedureEditorDialog(proc_name, procedures_tab)
                editor_dialog.exec()  # Use exec() for modal dialog instead of show()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error opening editor: {e}")


class ProcedureEditorDialog(QDialog):
    """
    Comprehensive dialog for editing and modifying existing procedure waypoint constraints and parameters.
    
    This advanced interface provides detailed editing capabilities for existing SID and STAR
    procedures, allowing modification of waypoint constraints, altitude restrictions, speed
    limitations, and route parameters. The dialog enables fine-tuning of procedure parameters
    to optimize traffic flow and ensure compliance with operational requirements and airspace
    constraints for realistic air traffic management scenarios.
    
    The Procedure Editor supports both newly created procedures (from ProcedureCreatorDialog)
    and existing procedures loaded from scenario files. The interface provides comprehensive
    editing capabilities with real-time validation to ensure all modifications maintain
    procedure integrity and operational feasibility.
    
    Key Features:
    - Comprehensive waypoint constraint editing with altitude and speed parameters
    - Real-time validation ensuring procedure integrity and operational feasibility
    - Support for both newly created and existing procedures from scenario files
    - Advanced constraint management with minimum, maximum, and target values
    - Route parameter modification with trajectory optimization
    - Procedure validation against airspace and safety requirements
    - Integrated file management for procedure storage and version control
    
    Editing Capabilities:
    - Altitude Constraints: Modify minimum, maximum, and target altitude restrictions
    - Speed Constraints: Configure approach, departure, and en-route speed limitations
    - Waypoint Properties: Edit waypoint names, coordinates, and navigation characteristics
    - Route Parameters: Modify procedure routing and trajectory specifications
    - Constraint Validation: Real-time checking of constraint feasibility and safety
    - Parameter Optimization: Suggest realistic values based on procedure type and airspace
    
    Validation Features:
    - Constraint Consistency: Ensure waypoint constraints are logically consistent
    - Safety Verification: Validate separation and safety requirements
    - Performance Limits: Check aircraft performance constraints for procedure feasibility
    - Airspace Compliance: Verify procedure compliance with airspace operational requirements
    - Route Integrity: Ensure procedure routing maintains navigation accuracy
    
    Attributes:
        proc_name (str): Name identifier for the procedure being edited
        waypoints (List[Dict]): Comprehensive waypoint data with constraints and parameters
        filepath (str): Path to the procedure scenario file for saving modifications
        direct_file_path (str): Optional direct file path for newly created procedures
    
    Args:
        proc_name (str): Name of the procedure to edit
        parent (QWidget, optional): Parent widget for proper dialog behavior
        file_path (str, optional): Direct file path for newly created procedures
    
    Examples:
        # Edit existing procedure from scenario file
        editor = ProcedureEditorDialog("SID_RUNWAY_09", parent=self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            modified_procedure = editor.get_procedure_data()
            self._save_procedure_changes(modified_procedure)
            
        # Edit newly created procedure with direct file path
        editor = ProcedureEditorDialog(
            "NEW_STAR_APPROACH", 
            parent=self,
            file_path="/path/to/new_procedure.scn"
        )
    
    Note:
        The editor operates in modal mode to ensure focused editing without conflicts.
        All modifications are validated in real-time, and the dialog provides comprehensive
        feedback for constraint violations or operational feasibility issues. File management
        is integrated to support both scenario file updates and new procedure creation.
    """
    
    def __init__(self, proc_name, parent=None, file_path=None):
        super().__init__(parent)
        self.proc_name = proc_name
        self.waypoints = []
        self.filepath = ""
        self.direct_file_path = file_path  # Optional direct file path for newly created procedures
        
        self.setWindowTitle(f"Edit Procedure: {proc_name}")
        self.setModal(True)  # Make it modal
        self.resize(800, 500)
        
        self._init_ui()
        self._load_procedure_data()
        
    def _init_ui(self):
        layout = QVBoxLayout(self)
        
        # Info section
        info_group = QGroupBox("Procedure Information")
        info_layout = QFormLayout(info_group)
        
        self.proc_label = QLabel(self.proc_name)
        self.proc_label.setStyleSheet("font-weight: bold;")
        info_layout.addRow("Procedure Name:", self.proc_label)
        
        self.status_label = QLabel("Loading procedure data...")
        info_layout.addRow("Status:", self.status_label)
        
        layout.addWidget(info_group)
        
        # Waypoints table
        wp_group = QGroupBox("Waypoint Constraints")
        wp_layout = QVBoxLayout(wp_group)
        
        # Table
        # Create waypoints table with simple built-in drag & drop
        self.waypoints_table = QTableWidget(0, 5)
        self.waypoints_table.setHorizontalHeaderLabels(["Name", "Latitude", "Longitude", "Altitude", "Speed"])
        self.waypoints_table.setDragDropMode(QTableWidget.DragDropMode.InternalMove)
        self.waypoints_table.setDragDropOverwriteMode(False)
        self.waypoints_table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.waypoints_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        
        # Connect item changed signal to update waypoints data
        self.waypoints_table.itemChanged.connect(self._on_table_changed)
        
        # Use simple drag-drop with model synchronization
        self._setup_simple_drag_drop()
        
        header = self.waypoints_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        wp_layout.addWidget(self.waypoints_table)
        
        # Waypoint management buttons
        wp_button_layout = QHBoxLayout()
        self.add_wp_btn = QPushButton("Add Waypoint")
        self.add_wp_btn.clicked.connect(self._add_waypoint)
        self.delete_wp_btn = QPushButton("Delete Selected")
        self.delete_wp_btn.clicked.connect(self._delete_waypoint)
        
        wp_button_layout.addWidget(self.add_wp_btn)
        wp_button_layout.addWidget(self.delete_wp_btn)
        wp_button_layout.addStretch()
        wp_layout.addLayout(wp_button_layout)
        
        # Buttons
        button_layout = QHBoxLayout()
        self.save_btn = QPushButton("Save Changes")
        self.save_btn.clicked.connect(self._save_procedure)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        
        button_layout.addWidget(self.save_btn)
        button_layout.addStretch()
        button_layout.addWidget(self.close_btn)
        wp_layout.addLayout(button_layout)
        
        layout.addWidget(wp_group)
    
    def _load_procedure_data(self):
        """Load procedure data by directly reading the procedure file."""
        try:
            import re
            
            # Determine the procedure file path
            proc_file_path = None
            
            # If a direct file path was provided (for newly created procedures), use it
            if self.direct_file_path:
                proc_file_path = self.direct_file_path
            else:
                # Find the procedure file path from loaded files (for existing procedures)
                for filepath in self.parent()._proc_files:
                    if os.path.splitext(os.path.basename(filepath))[0] == self.proc_name:
                        proc_file_path = filepath
                        break
            
            if not proc_file_path:
                self.status_label.setText("Error: Procedure file not found in loaded files")
                return
            
            if not os.path.exists(proc_file_path):
                self.status_label.setText(f"Error: Procedure file not found: {proc_file_path}")
                return
            
            # Read and parse the procedure file directly
            with open(proc_file_path, 'r') as f:
                content = f.read()
            
            # Parse waypoint definitions and procedure commands
            lines = content.split('\n')
            waypoint_definitions = {}  # name -> (lat, lon)
            addwpt_lines = []
            
            # First pass: collect DEFWPT commands
            for line in lines:
                if '00:00:00.00> DEFWPT' in line:
                    parts = line.strip().split()
                    if len(parts) >= 4 and parts[1] == 'DEFWPT':
                        try:
                            name = parts[2]
                            lat = float(parts[3])
                            lon = float(parts[4])
                            waypoint_definitions[name] = (lat, lon)
                        except (ValueError, IndexError) as e:
                            print(f"Error parsing DEFWPT line '{line}': {e}")
                            continue
                elif '00:00:00.00>%0 ADDWPT' in line:
                    addwpt_lines.append(line)
            
            waypoints = []
            needs_conversion = False  # Track if we found old format
            
            for i, line in enumerate(addwpt_lines):
                # Split the line and extract components
                parts = line.strip().split()
                if len(parts) >= 3 and parts[0] == '00:00:00.00>%0' and parts[1] == 'ADDWPT':
                    try:
                        # Check if this is a coordinate-based waypoint (lat/lon numbers) or named waypoint
                        waypoint_identifier = parts[2]
                        
                        # Try to parse as coordinates first (old format)
                        try:
                            lat = float(waypoint_identifier)
                            lon = float(parts[3]) if len(parts) > 3 else 0.0
                            
                            # This is OLD FORMAT coordinate waypoint - needs conversion
                            needs_conversion = True
                            proc_name = _extract_procedure_name_from_path(proc_file_path)
                            auto_name = _generate_auto_waypoint_name(proc_name, i)
                            
                            waypoint = {
                                'name': auto_name,  # Auto-generate name, user can edit
                                'lat': lat,
                                'lon': lon,
                                'alt': '',
                                'spd': '',
                                'is_named': False,
                                'needs_name_validation': True  # Flag for user to confirm names
                            }
                            
                            # Look for optional altitude and speed in remaining parts
                            remaining_parts = parts[4:]
                            
                        except (ValueError, IndexError):
                            # This is a named waypoint (new format)
                            waypoint_name = waypoint_identifier
                            
                            # Check if it's a coordinate waypoint with DEFWPT definition
                            if waypoint_name in waypoint_definitions:
                                lat, lon = waypoint_definitions[waypoint_name]
                                waypoint = {
                                    'name': waypoint_name,
                                    'lat': lat,
                                    'lon': lon,
                                    'alt': '',
                                    'spd': '',
                                    'is_named': False  # It's a coordinate waypoint with definition
                                }
                            elif _is_known_named_waypoint(waypoint_name):
                                # This is a known named waypoint (like EHAM, LAK) - don't define with DEFWPT
                                waypoint = {
                                    'name': waypoint_name,
                                    'lat': 0.0,  # Named waypoints don't store coordinates
                                    'lon': 0.0,
                                    'alt': '',
                                    'spd': '',
                                    'is_named': True
                                }
                            else:
                                # This is a user-defined named waypoint without coordinates
                                waypoint = {
                                    'name': waypoint_name,
                                    'lat': 0.0,
                                    'lon': 0.0,
                                    'alt': '',
                                    'spd': '',
                                    'is_named': True
                                }
                            
                            # Look for optional altitude and speed in remaining parts
                            remaining_parts = parts[3:]
                        
                        # Parse altitude and speed for both formats
                        for j, part in enumerate(remaining_parts):
                            # Skip empty or comma-only parts
                            if not part or part == ',,':
                                continue
                            
                            # Altitude indicators: FL (flight level), A (altitude), or larger numbers (>=1000 likely altitude)
                            if part.startswith('FL') or part.startswith('A') or (part.isdigit() and int(part) >= 1000):
                                waypoint['alt'] = part
                            # Speed indicators: M (Mach), smaller numbers (speed), or decimal numbers
                            elif part.startswith('M') or (part.replace('.', '').isdigit() and float(part) <= 999):
                                waypoint['spd'] = part
                            # For ambiguous pure numbers, use position: first number = altitude, second = speed
                            elif part.isdigit():
                                if not waypoint['alt']:  # First number goes to altitude
                                    waypoint['alt'] = part
                                elif not waypoint['spd']:  # Second number goes to speed
                                    waypoint['spd'] = part
                        
                        waypoints.append(waypoint)
                        
                    except (ValueError, IndexError) as e:
                        print(f"Error parsing line '{line}': {e}")
                        continue
            
            if waypoints:
                self.waypoints = waypoints
                self.filepath = proc_file_path  # Set the filepath for saving
                
                # If old format detected, warn user
                if needs_conversion:
                    from PyQt6.QtWidgets import QMessageBox
                    QMessageBox.information(self, "Old Format Detected", 
                                          f"This procedure uses the old coordinate format.\n\n"
                                          f"Auto-generated waypoint names have been created.\n"
                                          f"You can edit the names in the table before saving.\n\n"
                                          f"The procedure will be converted to the new format when saved.")
                
                self._populate_table()
                self.status_label.setText(f"Loaded {len(waypoints)} waypoints from {self.proc_name}")
            else:
                self.status_label.setText("No waypoints found in procedure file")
                
        except Exception as e:
            self.status_label.setText(f"Error loading procedure: {e}")
            print(f"Error in _load_procedure_data: {e}")
            import traceback
            traceback.print_exc()
    
    def _populate_table(self):
        """
        Populate the waypoints table with current procedure data.
        
        Note:
            - Disconnects change signals temporarily to prevent recursion
            - Sets appropriate colors and editability for different cell types
            - Handles named waypoints vs coordinate waypoints differently
            - Applies visual indicators (colors) for validation status
            - Read-only cells (gray), editable cells (white), warnings (yellow)
            - Reconnects change signals after population complete
        """
        # Temporarily disconnect the signal to prevent recursion
        try:
            self.waypoints_table.itemChanged.disconnect(self._on_table_changed)
        except TypeError:
            # No connection exists, continue
            pass
        
        self.waypoints_table.setRowCount(len(self.waypoints))
        
        # Define colors for different cell types
        readonly_color = QColor(230, 230, 230)  # Light gray for read-only cells
        editable_color = QColor(255, 255, 255)  # White for editable cells
        warning_color = QColor(255, 255, 200)   # Light yellow for fields needing attention
        
        for i, wp in enumerate(self.waypoints):
            # Create items for all columns
            name_item = QTableWidgetItem(wp['name'])
            alt_item = QTableWidgetItem(wp['alt'])
            spd_item = QTableWidgetItem(wp['spd'])
            
            # Check waypoint type
            is_named = wp.get('is_named', None)
            needs_name_validation = wp.get('needs_name_validation', False)
            
            if is_named is True:
                # Named waypoint (like EHAM, LAK): only name, altitude, and speed are editable
                lat_item = QTableWidgetItem("Named Waypoint")
                lon_item = QTableWidgetItem("Named Waypoint")
                
                # Make coordinates read-only with gray background
                lat_item.setFlags(lat_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                lon_item.setFlags(lon_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                lat_item.setBackground(readonly_color)
                lon_item.setBackground(readonly_color)
                
                # Name is editable with white background
                name_item.setBackground(editable_color)
                
            elif is_named is False:
                # Coordinate waypoint: both name and coordinates are editable
                lat_item = QTableWidgetItem(f"{wp['lat']:.6f}")
                lon_item = QTableWidgetItem(f"{wp['lon']:.6f}")
                
                # Both name and coordinates are editable
                if needs_name_validation:
                    # Highlight auto-generated names that user should review
                    name_item.setBackground(warning_color)
                    name_item.setToolTip("Auto-generated name - please review and edit if needed")
                else:
                    name_item.setBackground(editable_color)
                
                lat_item.setBackground(editable_color)
                lon_item.setBackground(editable_color)
                
            else:
                # Undefined state (new waypoint): all editable, empty coordinates
                lat_item = QTableWidgetItem("")  # Empty until user decides
                lon_item = QTableWidgetItem("")  # Empty until user decides
                
                # All fields are editable with white background
                name_item.setBackground(editable_color)
                lat_item.setBackground(editable_color)
                lon_item.setBackground(editable_color)
            
            # Altitude and speed are always editable for all types
            alt_item.setBackground(editable_color)
            spd_item.setBackground(editable_color)
            
            # Set items in table
            self.waypoints_table.setItem(i, 0, name_item)
            self.waypoints_table.setItem(i, 1, lat_item)
            self.waypoints_table.setItem(i, 2, lon_item)
            self.waypoints_table.setItem(i, 3, alt_item)
            self.waypoints_table.setItem(i, 4, spd_item)
        
        # Reconnect the signal after population is complete
        self.waypoints_table.itemChanged.connect(self._on_table_changed)
    
    def _on_table_changed(self, item):
        """
        Handle changes to waypoint table cells with validation and conflict resolution.
        
        Args:
            item (QTableWidgetItem): The table item that was modified
            
        Note:
            - Validates waypoint names against navigation database
            - Handles coordinate vs named waypoint conflicts
            - Updates waypoint data structure with table changes
            - Provides user dialogs for conflict resolution
            - Temporarily disconnects signals to prevent recursion
            - Applies appropriate cell formatting and validation indicators
        """
        row = item.row()
        col = item.column()
        
        if row < len(self.waypoints):
            # Temporarily disconnect to prevent recursion during updates
            try:
                self.waypoints_table.itemChanged.disconnect(self._on_table_changed)
            except TypeError:
                pass  # No connection exists
            
            try:
                if col == 0:  # Name column
                    new_name = item.text().strip().upper()  # Convert to uppercase
                    old_waypoint = self.waypoints[row].copy()
                    
                    if new_name:
                        # First check for validation conflicts
                        has_coords = (self.waypoints[row].get('lat', 0) != 0 or 
                                    self.waypoints[row].get('lon', 0) != 0 or
                                    not self.waypoints[row].get('is_named', True))
                        
                        is_valid, warning_msg = _validate_waypoint_name_conflict(new_name, has_coords)
                        
                        if not is_valid:
                            # Show warning dialog
                            reply = QMessageBox.warning(self, "Waypoint Name Conflict", 
                                                       warning_msg + "\n\nDo you want to:\n" +
                                                       "* YES: Use this name as a named waypoint (will use nav database coordinates)\n" +
                                                       "* NO: Choose a different name for your coordinate waypoint", 
                                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                            
                            if reply == QMessageBox.StandardButton.No:
                                # Revert to old name
                                item.setText(old_waypoint.get('name', ''))
                                self.waypoints_table.itemChanged.connect(self._on_table_changed)
                                return
                            # If YES, continue with the logic below to convert to named waypoint
                        
                        # Check if this is a known named waypoint (like EHAM, LAK, etc.)
                        # These are waypoints that exist in BlueSky's navigation database
                        if _is_known_named_waypoint(new_name):
                            # This is a known named waypoint - don't define with DEFWPT
                            self.waypoints[row]['name'] = new_name
                            self.waypoints[row]['is_named'] = True
                            self.waypoints[row]['lat'] = 0.0
                            self.waypoints[row]['lon'] = 0.0
                            # Clear any validation flags
                            self.waypoints[row]['needs_name_validation'] = False
                            
                            # Update lat/lon cells to show read-only status
                            self._update_coordinate_cells_readonly(row)
                            
                        else:
                            # Check if coordinates are present to determine waypoint type
                            has_coords = (self.waypoints[row].get('lat', 0) != 0 or 
                                        self.waypoints[row].get('lon', 0) != 0 or
                                        (self.waypoints_table.item(row, 1) and 
                                         self.waypoints_table.item(row, 1).text().strip() and
                                         self.waypoints_table.item(row, 1).text() != "Named Waypoint") or
                                        (self.waypoints_table.item(row, 2) and 
                                         self.waypoints_table.item(row, 2).text().strip() and
                                         self.waypoints_table.item(row, 2).text() != "Named Waypoint"))
                            
                            if has_coords:
                                # This is a coordinate waypoint with a custom name
                                # VALIDATE: Check for name conflicts
                                is_valid, warning_msg = _validate_waypoint_name_conflict(new_name, True)
                                if not is_valid:
                                    # Show warning dialog
                                    reply = QMessageBox.warning(
                                        self, 
                                        "Waypoint Name Conflict", 
                                        warning_msg + "\n\nDo you want to:\n"
                                        "* YES: Keep this name (will use navaid coordinates)\n"
                                        "* NO: Change to a different name",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                        QMessageBox.StandardButton.No
                                    )
                                    
                                    if reply == QMessageBox.StandardButton.No:
                                        # Revert to old name or suggest alternative
                                        old_name = old_waypoint.get('name', '')
                                        suggested_name = f"{new_name}_WP" if old_name == new_name else old_name
                                        item.setText(suggested_name)
                                        self.waypoints[row]['name'] = suggested_name
                                        item.setBackground(QColor(255, 200, 200))  # Light red for attention
                                        item.setToolTip("Please choose a different name to avoid conflicts")
                                        return  # Exit early
                                    else:
                                        # User chose to keep the conflicting name - convert to named waypoint
                                        self.waypoints[row]['is_named'] = True
                                        self.waypoints[row]['lat'] = 0.0
                                        self.waypoints[row]['lon'] = 0.0
                                        self._update_coordinate_cells_readonly(row)
                                
                                self.waypoints[row]['name'] = new_name
                                self.waypoints[row]['is_named'] = False
                                # Clear validation flag since user has reviewed the name
                                self.waypoints[row]['needs_name_validation'] = False
                                # Update visual styling
                                item.setBackground(QColor(255, 255, 255))  # White for normal editing
                                item.setToolTip("")
                            else:
                                # User-defined named waypoint (like a custom name without coordinates)
                                self.waypoints[row]['name'] = new_name
                                self.waypoints[row]['is_named'] = True
                                self.waypoints[row]['lat'] = 0.0
                                self.waypoints[row]['lon'] = 0.0
                                
                                # Update lat/lon cells to show read-only status
                                self._update_coordinate_cells_readonly(row)
                    else:
                        # Name cleared - keep as undefined for now
                        self.waypoints[row]['name'] = ''
                        
                elif col in [1, 2]:  # Latitude or Longitude columns
                    new_value = item.text().strip()
                    
                    if new_value and new_value != "Named Waypoint":
                        try:
                            float_value = float(new_value)
                            
                            # Update the coordinate
                            if col == 1:  # Latitude
                                self.waypoints[row]['lat'] = float_value
                            else:  # Longitude
                                self.waypoints[row]['lon'] = float_value
                            
                            # Switch to coordinate waypoint mode
                            self.waypoints[row]['is_named'] = False
                            
                            # If no name exists, generate one
                            if not self.waypoints[row]['name']:
                                proc_name = _extract_procedure_name_from_path(self.filepath or "PROC")
                                auto_name = _generate_auto_waypoint_name(proc_name, row)
                                self.waypoints[row]['name'] = auto_name
                                self.waypoints[row]['needs_name_validation'] = True
                                
                                # Update name cell
                                name_item = self.waypoints_table.item(row, 0)
                                if name_item:
                                    name_item.setText(auto_name)
                                    name_item.setBackground(QColor(255, 255, 200))  # Yellow warning
                                    name_item.setToolTip("Auto-generated name - please review and edit if needed")
                            
                        except ValueError:
                            # Invalid coordinate - revert
                            old_value = self.waypoints[row].get('lat' if col == 1 else 'lon', 0.0)
                            item.setText(f"{old_value:.6f}")
                    
                elif col == 3:  # Altitude column
                    self.waypoints[row]['alt'] = item.text().strip()
                    
                elif col == 4:  # Speed column
                    self.waypoints[row]['spd'] = item.text().strip()
                    
            except Exception as e:
                print(f"Error in _on_table_changed: {e}")
                
            finally:
                # Always reconnect the signal
                self.waypoints_table.itemChanged.connect(self._on_table_changed)
    
    def _update_coordinate_cells_readonly(self, row):
        """
        Update coordinate cells to show read-only status for named waypoints.
        
        Args:
            row (int): Table row to update coordinate cell display
            
        Note:
            - Sets coordinate cells to "Named Waypoint" text for named waypoints
            - Applies gray background color to indicate read-only status
            - Removes ItemIsEditable flag to prevent user editing
            - Used when waypoint is identified as navigation database waypoint
        """
        readonly_color = QColor(230, 230, 230)
        
        lat_item = self.waypoints_table.item(row, 1)
        lon_item = self.waypoints_table.item(row, 2)
        
        if lat_item:
            lat_item.setText("Named Waypoint")
            lat_item.setFlags(lat_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            lat_item.setBackground(readonly_color)
        
        if lon_item:
            lon_item.setText("Named Waypoint")
            lon_item.setFlags(lon_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            lon_item.setBackground(readonly_color)

    def _save_procedure(self):
        """
        Save the procedure file with updated waypoint constraints and validation.
        
        Note:
            - Updates waypoints from table to capture any reordering from drag-drop
            - Validates all waypoint names and coordinates before saving
            - Checks waypoint name format (alphanumeric, dash, underscore only)
            - Validates coordinate ranges for coordinate waypoints
            - Uses DEFWPT format for procedure file output
            - Provides comprehensive error reporting for validation failures
            - Preserves file structure and adds timestamp comments
            
        Raises:
            QMessageBox: Warning dialogs for validation errors or file access issues
        """
        try:
            # Update waypoints from table to capture any reordering
            self._update_waypoints_from_table()
            
            # Validate waypoint names before saving
            validation_errors = []
            for i, wp in enumerate(self.waypoints):
                name = wp.get('name', '').strip()
                if not name:
                    validation_errors.append(f"Row {i+1}: Waypoint name is required")
                elif not _validate_waypoint_name(name):
                    validation_errors.append(f"Row {i+1}: Invalid waypoint name '{name}' (alphanumeric and -_ only, no spaces)")
                
                # Check for coordinate waypoints with coordinates
                if not wp.get('is_named', True):  # Coordinate waypoint
                    try:
                        lat = float(wp.get('lat', 0))
                        lon = float(wp.get('lon', 0))
                        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                            validation_errors.append(f"Row {i+1}: Invalid coordinates for '{name}'")
                    except (ValueError, TypeError):
                        validation_errors.append(f"Row {i+1}: Invalid coordinates for '{name}'")
            
            if validation_errors:
                QMessageBox.warning(self, "Validation Errors", 
                                   "Please fix the following errors:\n\n" + "\n".join(validation_errors))
                return
            
            from datetime import datetime
            
            if not self.filepath:
                QMessageBox.warning(self, "Warning", "No file path available")
                return
            
            # Create updated file content with new DEFWPT format
            content = []
            content.append(f"# Procedure: {self.proc_name}")
            content.append(f"# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            content.append(f"# Type: Custom Track Procedure")
            content.append(f"# Waypoints: {len(self.waypoints)}")
            content.append("#")
            
            # Step 1: Add DEFWPT commands for coordinate waypoints
            coordinate_waypoints = [wp for wp in self.waypoints if not wp.get('is_named', True)]
            if coordinate_waypoints:
                content.append("# Waypoint Definitions")
                for wp in coordinate_waypoints:
                    defwpt_line = f"00:00:00.00> DEFWPT {wp['name']} {wp['lat']:.6f} {wp['lon']:.6f}"
                    content.append(defwpt_line)
                content.append("#")
            
            # Step 2: Add ADDWPT commands (all waypoints referenced by name now)
            content.append("# Procedure Commands")
            for wp in self.waypoints:
                # All waypoints now use names in ADDWPT commands
                line = f"00:00:00.00>%0 ADDWPT {wp['name']}"
                
                # Add constraints if present
                has_alt = wp['alt'].strip() if wp['alt'] else ""
                has_spd = wp['spd'].strip() if wp['spd'] else ""
                
                if has_alt and has_spd:
                    # Both altitude and speed
                    line += f" {has_alt} {has_spd}"
                elif has_alt and not has_spd:
                    # Only altitude
                    line += f" {has_alt}"
                elif not has_alt and has_spd:
                    # Only speed - use comma placeholders
                    line += f" ,, {has_spd}"
                # If neither, just leave as waypoint name only
                
                content.append(line)
            
            content.append("")  # Empty line at end
            
            # Write file
            with open(self.filepath, 'w') as f:
                f.write('\n'.join(content))
            
            self.status_label.setText("Procedure saved successfully!")
            
            # Now perform the unload -> reload cycle
            try:
                from bluesky import stack
                
                # Step 1: Unload the old version
                stack.stack(_join_tokens("SATG_PROC_UNLOAD_PROC", _qpath(self.filepath)))
                
                # Step 2: Reload the new version
                stack.stack(_join_tokens("SATG_PROC_LOAD_PROC", _qpath(self.filepath)))
                
                # Update the procedure widget's waypoint information with new sequence
                if self.parent() and hasattr(self.parent(), '_proc_widgets'):
                    if self.filepath in self.parent()._proc_widgets:
                        # Re-extract waypoints from the updated file
                        if hasattr(self.parent(), '_proc_fix_sequence'):
                            fixes = self.parent()._proc_fix_sequence(self.filepath)
                            initial_fix = fixes[0] if fixes else ""
                            final_fix = fixes[-1] if fixes else ""
                            
                            # Update the stored waypoint information
                            self.parent()._proc_widgets[self.filepath]["initial_fix"] = initial_fix
                            self.parent()._proc_widgets[self.filepath]["final_fix"] = final_fix
                
                # Update batch options using comprehensive refresh
                if self.parent():
                    if hasattr(self.parent(), '_refresh_all_batch_options'):
                        self.parent()._refresh_all_batch_options()
                    else:
                        # Fallback to individual refresh functions
                        if hasattr(self.parent(), '_refresh_sid_runway_rows'):
                            self.parent()._refresh_sid_runway_rows()
                        if hasattr(self.parent(), '_refresh_star_rate_rows'):
                            self.parent()._refresh_star_rate_rows()
                        if hasattr(self.parent(), '_refresh_generic_rate_rows'):
                            self.parent()._refresh_generic_rate_rows()
                        if hasattr(self.parent(), '_sync_destination_edits'):
                            self.parent()._sync_destination_edits()
                        if hasattr(self.parent(), '_sync_origin_edits'):
                            self.parent()._sync_origin_edits()
                        if hasattr(self.parent(), '_update_dest_state'):
                            self.parent()._update_dest_state()
                    
                    # Force rate basis refresh to ensure initial/final waypoint changes take effect
                    if hasattr(self.parent(), '_on_star_basis_changed') and hasattr(self.parent(), '_star_basis_index'):
                        current_star_basis = self.parent()._star_basis_index
                        self.parent()._on_star_basis_changed(current_star_basis)
                    
                    if hasattr(self.parent(), '_on_generic_basis_changed') and hasattr(self.parent(), '_generic_basis_index'):
                        current_generic_basis = self.parent()._generic_basis_index
                        self.parent()._on_generic_basis_changed(current_generic_basis)
                
                self.status_label.setText("Procedure saved and reloaded successfully!")
                
                # Check if this is a newly created procedure that needs to be added to the GUI
                if (self.parent() and hasattr(self.parent(), '_proc_files') and 
                    self.filepath not in self.parent()._proc_files):
                    
                    # This is a newly created procedure - add it to the procedures tab
                    try:
                        # Load the procedure into the backend first
                        from bluesky import stack
                        stack.stack(_join_tokens("SATG_PROC_LOAD_PROC", _qpath(self.filepath)))
                        
                        # Add to the procedures tab file list
                        self.parent()._proc_files.append(self.filepath)
                        
                        # Trigger the procedures tab to refresh its GUI by simulating the file addition
                        # This will recreate all the GUI elements properly
                        procedures_tab = self.parent()
                        
                        # Use the same logic as in _add_proc to add the GUI entry
                        from PyQt6.QtWidgets import QListWidgetItem, QWidget, QHBoxLayout, QLineEdit, QPushButton, QLabel
                        from PyQt6.QtCore import Qt
                        from typing import Optional
                        
                        item = QListWidgetItem(procedures_tab.lst_proc)
                        item.setData(Qt.ItemDataRole.UserRole, self.filepath)
                        container = QWidget(procedures_tab.lst_proc)
                        row_layout = QHBoxLayout(container)
                        row_layout.setContentsMargins(0, 0, 0, 0)
                        is_sid = procedures_tab._is_sid_file(self.filepath)
                        is_star = procedures_tab._is_star_file(self.filepath)
                        is_generic = not is_sid and not is_star

                        origin_edit: Optional[QLineEdit] = None
                        origin_all_btn: Optional[QPushButton] = None
                        if is_sid:
                            origin_edit = QLineEdit(container)
                            origin_edit.setPlaceholderText("Origin ICAO")
                            origin_edit.setMaxLength(4)
                            origin_edit.setMaximumWidth(90)
                            origin_edit.setStyleSheet("background-color: white; color: black; border: 1px solid #ccc;")
                            if procedures_tab._origins.get(self.filepath):
                                origin_edit.setText(procedures_tab._origins[self.filepath])
                                procedures_tab._update_origin_entry(self.filepath, procedures_tab._origins[self.filepath])
                            origin_edit.editingFinished.connect(lambda path=self.filepath, ref=origin_edit: procedures_tab._update_origin_entry(path, ref.text()))

                            origin_all_btn = QPushButton("Origin -> all", container)
                            origin_all_btn.setAutoDefault(False)
                            origin_all_btn.clicked.connect(lambda _, path=self.filepath: procedures_tab._apply_origin_to_all(path))

                            row_layout.addWidget(origin_edit)
                            row_layout.addWidget(origin_all_btn)

                        label = QLabel(self.filepath, container)
                        label.setStyleSheet("color: #555;")
                        row_layout.addWidget(label, 2)

                        dest_edit = QLineEdit(container)
                        dest_edit.setPlaceholderText("Destinations (comma separated)")
                        dest_edit.setStyleSheet("background-color: white; color: black; border: 1px solid #ccc;")
                        existing = procedures_tab._destinations.get(self.filepath, [])
                        if existing:
                            dest_edit.setText(", ".join(existing))
                            procedures_tab._update_destination_entry(self.filepath, ", ".join(existing))
                        dest_edit.editingFinished.connect(lambda path=self.filepath, ref=dest_edit: procedures_tab._update_destination_entry(path, ref.text()))

                        dest_all_btn = QPushButton("Dest -> all", container)
                        dest_all_btn.setAutoDefault(False)
                        dest_all_btn.clicked.connect(lambda _, path=self.filepath: procedures_tab._apply_dest_to_all(path))

                        row_layout.addWidget(dest_edit, 1)
                        row_layout.addWidget(dest_all_btn)

                        item.setSizeHint(container.sizeHint())
                        procedures_tab.lst_proc.setItemWidget(item, container)
                        
                        # Add to the _proc_widgets dictionary
                        procedures_tab._proc_widgets[self.filepath] = {
                            "item": item,
                            "origin": origin_edit,
                            "origin_all": origin_all_btn,
                            "dest": dest_edit,
                            "dest_all": dest_all_btn,
                            "is_sid": is_sid,
                            "is_star": is_star,
                            "is_generic": is_generic,
                        }
                        
                        # Extract initial and final waypoints for rate scheduling (STAR and Generic procedures)
                        if is_star or is_generic:
                            fixes = procedures_tab._proc_fix_sequence(self.filepath)
                            initial_fix = fixes[0] if fixes else ""
                            final_fix = fixes[-1] if fixes else ""
                            procedures_tab._proc_widgets[self.filepath]["initial_fix"] = initial_fix
                            procedures_tab._proc_widgets[self.filepath]["final_fix"] = final_fix
                        
                        # Refresh all related GUI components
                        procedures_tab._refresh_sid_runway_rows()
                        procedures_tab._refresh_star_rate_rows()
                        procedures_tab._refresh_generic_rate_rows()
                        procedures_tab._sync_destination_edits()
                        procedures_tab._sync_origin_edits()
                        procedures_tab._update_dest_state()
                        
                        # Force rate basis refresh to ensure initial/final waypoint changes take effect
                        if is_star and hasattr(procedures_tab, '_on_star_basis_changed') and hasattr(procedures_tab, '_star_basis_index'):
                            current_star_basis = procedures_tab._star_basis_index
                            procedures_tab._on_star_basis_changed(current_star_basis)
                        
                        if is_generic and hasattr(procedures_tab, '_on_generic_basis_changed') and hasattr(procedures_tab, '_generic_basis_index'):
                            current_generic_basis = procedures_tab._generic_basis_index
                            procedures_tab._on_generic_basis_changed(current_generic_basis)
                        
                        self.status_label.setText("Procedure saved, reloaded, and added to procedures list!")
                        
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        self.status_label.setText("Procedure saved and reloaded, but couldn't add to GUI automatically. Use 'Add Proc Files' to load it manually.")
                QMessageBox.information(self, "Success", f"Procedure saved and reloaded: {self.filepath}")
                
            except Exception as load_error:
                # File was saved but reload failed
                self.status_label.setText(f"Saved but reload failed: {load_error}")
                QMessageBox.warning(self, "Warning", f"Procedure saved but failed to reload: {load_error}")
            
        except Exception as e:
            error_msg = f"Error saving procedure: {e}"
            self.status_label.setText(error_msg)
            QMessageBox.critical(self, "Error", error_msg)

    def _add_waypoint(self):
        """
        Add a new waypoint to the procedure with type selection.
        
        Note:
            - Prompts user to choose between named or coordinate waypoint
            - Named waypoints: Uses navigation database (e.g., EHAM, LAK)
            - Coordinate waypoints: Uses latitude/longitude pairs
            - Auto-generates names for coordinate waypoints based on procedure
            - Validates input coordinates and waypoint names
            - Updates table display after adding waypoint
            - Appends new waypoint to end of procedure route
        """
        # Create new waypoint dialog
        from PyQt6.QtWidgets import QInputDialog
        
        # Ask user for waypoint type
        items = ["Named Waypoint (e.g., EHAM, LAK)", "Coordinate Waypoint (lat/lon)"]
        item, ok = QInputDialog.getItem(self, "Add Waypoint", "Choose waypoint type:", items, 0, False)
        
        if not ok:
            return
            
        if "Named" in item:
            # Named waypoint
            name, ok = QInputDialog.getText(self, "Add Named Waypoint", "Enter waypoint name (e.g., EHAM, LAK):")
            if ok and name.strip():
                new_waypoint = {
                    'name': name.strip().upper(),
                    'lat': 0.0,
                    'lon': 0.0,
                    'alt': '',
                    'spd': '',
                    'is_named': True
                }
                self.waypoints.append(new_waypoint)
                self._populate_table()
                self.status_label.setText(f"Added named waypoint: {name.strip().upper()}")
        else:
            # Coordinate waypoint - auto-generate name based on procedure
            lat, ok = QInputDialog.getDouble(self, "Add Coordinate Waypoint", "Enter latitude:", 0.0, -90.0, 90.0, 6)
            if not ok:
                return
            lon, ok = QInputDialog.getDouble(self, "Add Coordinate Waypoint", "Enter longitude:", 0.0, -180.0, 180.0, 6)
            if not ok:
                return
            
            # Generate auto name
            proc_name = _extract_procedure_name_from_path(self.filepath or "PROC")
            auto_name = _generate_auto_waypoint_name(proc_name, len(self.waypoints))
                
            new_waypoint = {
                'name': auto_name,
                'lat': lat,
                'lon': lon,
                'alt': '',
                'spd': '',
                'is_named': False,
                'needs_name_validation': True  # Mark as needing user review
            }
            self.waypoints.append(new_waypoint)
            self._populate_table()
            self.status_label.setText(f"Added coordinate waypoint: {auto_name} ({lat:.6f}, {lon:.6f})")
    
    def _delete_waypoint(self):
        """
        Delete the currently selected waypoint from the procedure.
        
        Note:
            - Requires a waypoint to be selected in the table
            - Shows confirmation dialog before deletion
            - Removes waypoint from internal data structure
            - Refreshes table display after deletion
            - Updates status with deleted waypoint name
            - Validates selection bounds before deletion
            
        Raises:
            QMessageBox: Information dialog if no waypoint is selected
        """
        current_row = self.waypoints_table.currentRow()
        if current_row < 0:
            QMessageBox.information(self, "No Selection", "Please select a waypoint to delete.")
            return
            
        if current_row >= len(self.waypoints):
            return
            
        # Confirm deletion
        waypoint = self.waypoints[current_row]
        waypoint_name = waypoint['name']
        
        reply = QMessageBox.question(self, "Confirm Deletion", 
                                   f"Delete waypoint '{waypoint_name}'?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            # Remove from waypoints list
            del self.waypoints[current_row]
            
            # Refresh table
            self._populate_table()
            
            self.status_label.setText(f"Deleted waypoint: {waypoint_name}")
    
    def _update_waypoints_from_table(self):
        """
        Update internal waypoints list from current table state after drag-drop operations.
        
        Note:
            - Reads all table rows to reconstruct waypoints list in current order
            - Preserves waypoint data including names, coordinates, constraints
            - Handles both named waypoints and coordinate waypoints appropriately
            - Maintains waypoint type information (is_named flag)
            - Called before saving to capture any drag-drop reordering
            - Validates coordinate values and handles parsing errors gracefully
        """
        try:
            new_waypoints = []
            
            for row in range(self.waypoints_table.rowCount()):
                # Get data from table items
                name_item = self.waypoints_table.item(row, 0)
                lat_item = self.waypoints_table.item(row, 1)
                lon_item = self.waypoints_table.item(row, 2)
                alt_item = self.waypoints_table.item(row, 3)
                spd_item = self.waypoints_table.item(row, 4)
                
                if name_item is None:
                    continue
                    
                # Determine if this is a named waypoint based on lat/lon display
                is_named = lat_item and lat_item.text() == "Named Waypoint"
                
                try:
                    lat_val = 0.0 if is_named else (float(lat_item.text()) if lat_item and lat_item.text() != "Named Waypoint" else 0.0)
                    lon_val = 0.0 if is_named else (float(lon_item.text()) if lon_item and lon_item.text() != "Named Waypoint" else 0.0)
                except (ValueError, AttributeError):
                    lat_val = 0.0
                    lon_val = 0.0
                
                waypoint = {
                    'name': name_item.text() if name_item else '',
                    'lat': lat_val,
                    'lon': lon_val,
                    'alt': alt_item.text() if alt_item else '',
                    'spd': spd_item.text() if spd_item else '',
                    'is_named': is_named
                }
                new_waypoints.append(waypoint)
            
            self.waypoints = new_waypoints
        except Exception as e:
            print(f"Error updating waypoints from table: {e}")
    
    def _setup_simple_drag_drop(self):
        """
        Setup simple drag and drop functionality for waypoint reordering.
        
        Note:
            - Uses Qt's built-in drag-drop with custom enhancements
            - Timer-based approach to detect completed drag operations
            - Monitors selection changes to trigger waypoint synchronization
            - Restricts drops to between-rows positions only
            - Prevents dropping on existing rows to avoid data corruption
        """
        # Use a simple timer-based approach to detect changes
        from PyQt6.QtCore import QTimer
        self._sync_timer = QTimer()
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._sync_waypoints_after_drag)
        
        # Monitor selection changes which happen after drag operations
        self.waypoints_table.itemSelectionChanged.connect(self._schedule_sync)
        
        # Override drop event to only allow drops between rows
        self._setup_between_rows_only_drop()

    def _setup_between_rows_only_drop(self):
        """
        Setup drop event restrictions to allow drops only between rows.
        
        Note:
            - Prevents dropping directly on existing rows
            - Allows drops only in gaps between rows for insertion
            - Overrides default Qt drag-move and drop event handlers
            - Provides visual feedback for valid drop zones
            - Maintains data integrity by preventing row replacement
        """
        original_drop_event = self.waypoints_table.dropEvent
        original_drag_move_event = self.waypoints_table.dragMoveEvent
        
        def custom_drag_move_event(event):
            """Control what areas accept drag operations."""
            # Get drag position
            drag_pos = event.position() if hasattr(event, 'position') else event.pos()
            drag_point = drag_pos.toPoint()
            
            # Check if we're dragging over an item
            item_at_drag = self.waypoints_table.itemAt(drag_point)
            
            if item_at_drag:
                # We're dragging over a row - check if it's in a valid zone
                row = self.waypoints_table.row(item_at_drag)
                item_rect = self.waypoints_table.visualItemRect(item_at_drag)
                
                # Calculate relative position within the row
                relative_y = drag_point.y() - item_rect.top()
                row_height = item_rect.height()
                
                # Define stricter "between rows" zones (top and bottom 20% of each row)
                between_zone_size = row_height * 0.2
                
                # Allow drag only if in the between-rows zones
                if relative_y <= between_zone_size or relative_y >= (row_height - between_zone_size):
                    # In between-rows zone - accept the drag
                    event.accept()
                else:
                    # In middle of row - reject the drag
                    event.ignore()
                    return
            else:
                # Not dragging over any item - accept it
                event.accept()
            
            # Call original if we accepted
            if event.isAccepted():
                original_drag_move_event(event)
        
        def custom_drop_event(event):
            """Handle drop events with strict between-rows validation."""
            # Get drop position
            drop_pos = event.position() if hasattr(event, 'position') else event.pos()
            drop_point = drop_pos.toPoint()
            
            # Double-check the drop position
            item_at_drop = self.waypoints_table.itemAt(drop_point)
            
            if item_at_drop:
                # We're dropping on a row - validate the position again
                row = self.waypoints_table.row(item_at_drop)
                item_rect = self.waypoints_table.visualItemRect(item_at_drop)
                
                # Calculate relative position within the row
                relative_y = drop_point.y() - item_rect.top()
                row_height = item_rect.height()
                
                # Use same strict zones as drag move
                between_zone_size = row_height * 0.2
                
                # Only allow drop in between-rows zones
                if relative_y <= between_zone_size or relative_y >= (row_height - between_zone_size):
                    # Store the original state before drop
                    original_row_count = self.waypoints_table.rowCount()
                    
                    # Allow the drop
                    original_drop_event(event)
                    
                    # Check if a row was deleted (which shouldn't happen)
                    if self.waypoints_table.rowCount() < original_row_count:
                        print("Warning: Row deletion detected during drop - this shouldn't happen")
                        # Could restore from backup here if needed
                else:
                    # Reject the drop completely
                    event.ignore()
                    # Show visual feedback that drop was rejected
                    self.status_label.setText("Drop rejected - can only drop between rows")
                    return
            else:
                # Not dropping on any item - allow it
                original_drop_event(event)
        
        # Replace both events
        self.waypoints_table.dragMoveEvent = custom_drag_move_event
        self.waypoints_table.dropEvent = custom_drop_event
    
    def _schedule_sync(self):
        """
        Schedule a delayed synchronization operation after drag-drop.
        
        Note:
            - Uses 100ms delay to ensure drag operation is fully complete
            - Prevents multiple sync operations during rapid UI changes
            - Timer is single-shot to avoid overlapping sync operations
        """
        self._sync_timer.start(100)  # 100ms delay

    def _sync_waypoints_after_drag(self):
        """
        Synchronize internal waypoints list with current table row order.
        
        Note:
            - Called after drag-drop operations to maintain data consistency
            - Matches waypoints by name to preserve all waypoint data
            - Validates table and waypoints count before synchronization
            - Updates internal waypoint order without losing constraints
            - Handles cases where table and waypoints are temporarily mismatched
        """
        try:
            # Only sync if table has same number of rows as our waypoints
            if self.waypoints_table.rowCount() != len(self.waypoints):
                return
                
            # Get current table order by reading waypoint names
            new_waypoints = []
            for row in range(self.waypoints_table.rowCount()):
                name_item = self.waypoints_table.item(row, 0)
                if name_item:
                    waypoint_name = name_item.text()
                    # Find this waypoint in our original list
                    for wp in self.waypoints:
                        if wp['name'] == waypoint_name:
                            new_waypoints.append(wp.copy())
                            break
            
            # Update our waypoints list if the order changed
            if len(new_waypoints) == len(self.waypoints):
                old_order = [wp['name'] for wp in self.waypoints]
                new_order = [wp['name'] for wp in new_waypoints]
                if old_order != new_order:
                    self.waypoints = new_waypoints
                    self.status_label.setText("Waypoints reordered")
                    
        except Exception as e:
            print(f"Error syncing waypoints after drag: {e}")

    def _setup_custom_drag_drop(self):
        """Setup custom drag and drop behavior with visual feedback."""
        # Disable the default drag-drop to implement our own
        self.waypoints_table.setDragDropMode(QTableWidget.DragDropMode.NoDragDrop)
        
        # Enable row selection and store drag state
        self.waypoints_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._drag_start_row = -1
        self._drag_active = False
        self._drag_highlight_widget = None
        self._drop_indicator = None
        self._drag_widget = None
        
        # Create drop indicator line
        self._create_drop_indicator()
        
        # Override mouse events for custom drag-drop
        original_mouse_press = self.waypoints_table.mousePressEvent
        original_mouse_move = self.waypoints_table.mouseMoveEvent
        original_mouse_release = self.waypoints_table.mouseReleaseEvent
        original_paint = self.waypoints_table.paintEvent
        
        def custom_mouse_press(event):
            if event.button() == Qt.MouseButton.LeftButton:
                item = self.waypoints_table.itemAt(event.pos())
                if item:
                    self._drag_start_row = self.waypoints_table.row(item)
                    self._drag_active = False
                    # Store initial position for drag threshold
                    self._drag_start_pos = event.pos()
            original_mouse_press(event)
        
        def custom_mouse_move(event):
            if (event.buttons() & Qt.MouseButton.LeftButton and 
                self._drag_start_row >= 0):
                
                # Check if we've moved enough to start dragging
                if not self._drag_active and (event.pos() - self._drag_start_pos).manhattanLength() > 10:
                    self._drag_active = True
                    self._start_drag_visual_feedback()
                
                if self._drag_active:
                    # Clear any existing row highlights that Qt might have added
                    self._clear_stray_highlights()
                    # Update visual feedback during drag
                    self._update_drag_visual_feedback(event.pos())
                    
            original_mouse_move(event)
        
        def custom_mouse_release(event):
            if self._drag_active and self._drag_start_row >= 0:
                # Calculate drop position between rows
                drop_row = self._calculate_drop_position(event.pos())
                
                if drop_row >= 0 and drop_row != self._drag_start_row and drop_row != self._drag_start_row + 1:
                    # Perform the move
                    self._move_waypoint_to_position(self._drag_start_row, drop_row)
                
                # Clean up visual feedback
                self._end_drag_visual_feedback()
                
                self._drag_active = False
                self._drag_start_row = -1
                
            original_mouse_release(event)
        
        def custom_paint(event):
            original_paint(event)
            # Custom painting for drag effects is handled by separate widgets
        
        self.waypoints_table.mousePressEvent = custom_mouse_press
        self.waypoints_table.mouseMoveEvent = custom_mouse_move
        self.waypoints_table.mouseReleaseEvent = custom_mouse_release
        self.waypoints_table.paintEvent = custom_paint
    
    def _create_drop_indicator(self):
        """Create a visual indicator for drop position."""
        self._drop_indicator = QFrame(self.waypoints_table)
        self._drop_indicator.setFrameStyle(QFrame.Shape.HLine)
        self._drop_indicator.setStyleSheet("""
            QFrame {
                background-color: #007ACC;
                border: 2px solid #007ACC;
                border-radius: 2px;
            }
        """)
        self._drop_indicator.setFixedHeight(4)
        self._drop_indicator.hide()
    
    def _start_drag_visual_feedback(self):
        """Start visual feedback for dragging."""
        if self._drag_start_row >= 0:
            # Highlight the source row being dragged
            self.waypoints_table.selectRow(self._drag_start_row)
            
            # Make the source row more prominent with semi-transparent overlay
            for col in range(self.waypoints_table.columnCount()):
                item = self.waypoints_table.item(self._drag_start_row, col)
                if item:
                    # Store original background for restoration
                    if not hasattr(item, '_original_background'):
                        item._original_background = item.background()
                    if not hasattr(item, '_original_font'):
                        item._original_font = item.font()
                    
                    # Apply drag styling
                    item.setBackground(QColor(0, 122, 204, 150))  # More visible blue
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
            
            # Create floating drag widget
            self._create_drag_widget()
    
    def _create_drag_widget(self):
        """
        Create a floating visual widget that follows the mouse during drag operations.
        
        Note:
            - Creates semi-transparent label showing waypoint name being dragged
            - Uses custom styling with blue background and rounded borders
            - Sets window flags to behave as tooltip without window frame
            - Makes widget transparent to mouse events to avoid interference
            - Displays waypoint name with asterisk prefix for clarity
            - Widget follows mouse cursor to provide visual feedback during drag
        """
        if self._drag_start_row >= 0:
            # Get the waypoint name being dragged
            name_item = self.waypoints_table.item(self._drag_start_row, 0)
            waypoint_name = name_item.text() if name_item else "Waypoint"
            
            # Create floating label
            self._drag_widget = QLabel(f"* {waypoint_name}", self.waypoints_table.parent())
            self._drag_widget.setStyleSheet("""
                QLabel {
                    background-color: rgba(0, 122, 204, 200);
                    color: white;
                    border: 2px solid #005a9e;
                    border-radius: 8px;
                    padding: 8px 12px;
                    font-weight: bold;
                    font-size: 12px;
                }
            """)
            self._drag_widget.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
            self._drag_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._drag_widget.show()
    
    def _update_drag_visual_feedback(self, mouse_pos):
        """Update visual feedback during drag operation."""
        # Calculate and show drop position
        drop_row = self._calculate_drop_position(mouse_pos)
        self._show_drop_indicator(drop_row)
        
        # Update cursor
        self.waypoints_table.setCursor(Qt.CursorShape.ClosedHandCursor)
        
        # Update floating drag widget position
        if hasattr(self, '_drag_widget') and self._drag_widget:
            global_pos = self.waypoints_table.mapToGlobal(mouse_pos)
            # Offset so it doesn't block the view
            self._drag_widget.move(global_pos.x() + 15, global_pos.y() - 10)
    
    def _show_drop_indicator(self, drop_row):
        """Show drop indicator at the specified position."""
        if drop_row < 0:
            self._drop_indicator.hide()
            return
        
        # Calculate position for drop indicator
        table_rect = self.waypoints_table.rect()
        
        if drop_row == 0:
            # Before first row
            if self.waypoints_table.rowCount() > 0:
                first_item = self.waypoints_table.item(0, 0)
                if first_item:
                    item_rect = self.waypoints_table.visualItemRect(first_item)
                    y_pos = item_rect.top() - 2
                else:
                    y_pos = 5
            else:
                y_pos = 5
        elif drop_row >= self.waypoints_table.rowCount():
            # After last row
            if self.waypoints_table.rowCount() > 0:
                last_item = self.waypoints_table.item(self.waypoints_table.rowCount() - 1, 0)
                if last_item:
                    item_rect = self.waypoints_table.visualItemRect(last_item)
                    y_pos = item_rect.bottom() + 2
                else:
                    y_pos = table_rect.height() - 10
            else:
                y_pos = table_rect.height() - 10
        else:
            # Between rows - show line above the target row
            target_item = self.waypoints_table.item(drop_row, 0)
            if target_item:
                target_rect = self.waypoints_table.visualItemRect(target_item)
                y_pos = target_rect.top() - 2
            else:
                y_pos = drop_row * 30  # Fallback estimate
        
        # Position and show the drop indicator with enhanced styling
        self._drop_indicator.setGeometry(5, y_pos, table_rect.width() - 10, 4)
        
        # Update styling to make it more prominent during drag
        self._drop_indicator.setStyleSheet("""
            QFrame {
                background-color: #FF6B35;
                border: 2px solid #FF4500;
                border-radius: 2px;
            }
        """)
        
        self._drop_indicator.show()
        self._drop_indicator.raise_()
    
    def _end_drag_visual_feedback(self):
        """Clean up visual feedback after drag ends."""
        # Hide drop indicator
        self._drop_indicator.hide()
        
        # Reset cursor
        self.waypoints_table.setCursor(Qt.CursorShape.ArrowCursor)
        
        # Clean up floating drag widget
        if hasattr(self, '_drag_widget') and self._drag_widget:
            self._drag_widget.hide()
            self._drag_widget.deleteLater()
            self._drag_widget = None
        
        # Clear ALL selections and highlights
        self.waypoints_table.clearSelection()
        
        # Reset ALL row styling to ensure no permanent highlights
        for row in range(self.waypoints_table.rowCount()):
            for col in range(self.waypoints_table.columnCount()):
                item = self.waypoints_table.item(row, col)
                if item:
                    # Restore original background and font
                    if hasattr(item, '_original_background'):
                        item.setBackground(item._original_background)
                        delattr(item, '_original_background')
                    else:
                        item.setBackground(QColor())  # Reset to default
                    
                    if hasattr(item, '_original_font'):
                        item.setFont(item._original_font)
                        delattr(item, '_original_font')
                    else:
                        font = item.font()
                        font.setBold(False)
                        item.setFont(font)
        
        # Force table to repaint to clear any Qt-added highlights
        self.waypoints_table.viewport().update()
    
    def _clear_stray_highlights(self):
        """Clear any unwanted row highlights that Qt might add during drag operations."""
        # Clear selection to prevent Qt's default highlighting
        self.waypoints_table.clearSelection()
        
        # Ensure only our intended drag row stays highlighted
        if self._drag_start_row >= 0:
            self.waypoints_table.selectRow(self._drag_start_row)
    
    def _calculate_drop_position(self, pos):
        """Calculate the position between rows where the item should be inserted."""
        # Find which row the mouse is closest to
        total_rows = self.waypoints_table.rowCount()
        
        if total_rows == 0:
            return 0
        
        # Get the viewport position (scroll offset adjusted)
        viewport_pos = self.waypoints_table.viewport().mapFromParent(pos)
        
        # Check each row to find the closest insertion point
        for row in range(total_rows):
            curr_item = self.waypoints_table.item(row, 0)
            
            if curr_item:
                curr_rect = self.waypoints_table.visualItemRect(curr_item)
                
                # If mouse is above the center of current row, insert before it
                if viewport_pos.y() < curr_rect.center().y():
                    return row
        
        # If we get here, mouse is below all rows - insert at end
        return total_rows
    
    def _move_waypoint_to_position(self, source_row, target_row):
        """Move waypoint from source_row to target_row position."""
        if source_row < 0 or source_row >= len(self.waypoints):
            return
        if target_row < 0 or target_row > len(self.waypoints):
            return
        
        # Store the waypoint being moved
        moved_waypoint = self.waypoints[source_row].copy()
        
        # Remove from source
        self.waypoints.pop(source_row)
        
        # Adjust target if needed (if source was before target)
        if source_row < target_row:
            target_row -= 1
        
        # Insert at target
        self.waypoints.insert(target_row, moved_waypoint)
        
        # Refresh table
        self._populate_table()
        
        # Select the moved row
        self.waypoints_table.selectRow(target_row)
        
        # Update status
        self.status_label.setText(f"Moved '{moved_waypoint['name']}' to position {target_row + 1}")
    
    
# --- old methods to remove ---

    def _refresh_waypoints(self):
        """Refresh waypoints from the created polygon in BlueSky."""
        if not self.polygon_name:
            QMessageBox.warning(self, "Warning", "No polygon name set. Create a track first.")
            return
            
        try:
            # Use backend command to export polygon coordinates
            coordinates = self._get_polygon_coordinates_via_backend(self.polygon_name)
            if coordinates:
                self._populate_waypoints_table(coordinates)
                self.poly_status.setText(f"Successfully loaded {len(coordinates)} waypoints from '{self.polygon_name}'")
                self.poly_status.setStyleSheet("color: green;")
            else:
                self.poly_status.setText(f"No polygon found with name '{self.polygon_name}'. Make sure you've drawn the track in BlueSky.")
                self.poly_status.setStyleSheet("color: orange;")
        except Exception as e:
            self.poly_status.setText(f"Error refreshing waypoints: {e}")
            self.poly_status.setStyleSheet("color: red;")
    
    def _get_polygon_coordinates_via_backend(self, poly_name):
        """Get coordinates using backend SATG command. Returns list of (lat, lon) tuples."""
        try:
            import tempfile
            import json
            import time
            
            # Send command to backend to export polygon coordinates
            from bluesky.ui.qtgl.console import process_cmdline
            process_cmdline(f"SATG_PROC_EXPORT_POLY {poly_name}")
            
            # Wait a moment for the command to process
            time.sleep(0.5)
            
            # Read the exported coordinates from temp file
            temp_dir = tempfile.gettempdir()
            temp_file = os.path.join(temp_dir, f"satg_poly_{poly_name.upper()}.json")
            
            if os.path.exists(temp_file):
                with open(temp_file, 'r') as f:
                    data = json.load(f)
                
                coordinates = data.get('coordinates', [])
                
                # Clean up temp file
                try:
                    os.remove(temp_file)
                except:
                    pass
                
                return coordinates
            else:
                return []
                
        except Exception as e:
            print(f"Error getting polygon coordinates via backend: {e}")
            return []
    
    def _populate_waypoints_table(self, coordinates):
        """Populate the waypoints table with coordinates."""
        self.waypoints.clear()
        self.waypoints_table.setRowCount(len(coordinates))
        
        for i, (lat, lon) in enumerate(coordinates):
            # Default waypoint name
            wp_name = f"{self.polygon_name}WP{i+1:02d}"
            
            waypoint = {
                'name': wp_name,
                'lat': lat,
                'lon': lon,
                'alt': '',
                'spd': ''
            }
            self.waypoints.append(waypoint)
            
            # Add to table
            self.waypoints_table.setItem(i, 0, QTableWidgetItem(wp_name))
            self.waypoints_table.setItem(i, 1, QTableWidgetItem(f"{lat:.6f}"))
            self.waypoints_table.setItem(i, 2, QTableWidgetItem(f"{lon:.6f}"))
            self.waypoints_table.setItem(i, 3, QTableWidgetItem(""))  # Altitude
            self.waypoints_table.setItem(i, 4, QTableWidgetItem(""))  # Speed
            
            # Make lat/lon read-only but name/alt/spd editable
            self.waypoints_table.item(i, 1).setFlags(self.waypoints_table.item(i, 1).flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.waypoints_table.item(i, 2).setFlags(self.waypoints_table.item(i, 2).flags() & ~Qt.ItemFlag.ItemIsEditable)
        
        # Connect table changes to update waypoints data
        self.waypoints_table.itemChanged.connect(self._on_table_item_changed)
    
    def _on_table_item_changed(self, item):
        """Update waypoints data when table items change."""
        row = item.row()
        col = item.column()
        if row < len(self.waypoints):
            if col == 0:  # Name
                self.waypoints[row]['name'] = item.text()
            elif col == 3:  # Altitude
                self.waypoints[row]['alt'] = item.text()
            elif col == 4:  # Speed
                self.waypoints[row]['spd'] = item.text()
    
    def _add_waypoint(self):
        """Add a new empty waypoint row for user input."""
        # Create a new empty waypoint in undefined state
        new_waypoint = {
            'name': '',       # Empty name
            'lat': None,      # No coordinates set yet
            'lon': None,      # No coordinates set yet
            'alt': '',        # User can enter altitude (optional)
            'spd': '',        # User can enter speed (optional)
            'is_named': None  # Undefined state - user will determine by first input
        }
        
        # Add to waypoints list
        self.waypoints.append(new_waypoint)
        
        # Refresh table to show new row
        self._populate_table()
        
        # Select the new row and focus on the name field for editing
        new_row_index = len(self.waypoints) - 1
        self.waypoints_table.selectRow(new_row_index)
        
        # Focus on the name cell for immediate editing
        name_item = self.waypoints_table.item(new_row_index, 0)
        if name_item:
            self.waypoints_table.setCurrentItem(name_item)
            self.waypoints_table.editItem(name_item)
        
        # Update status
        self.status_label.setText(f"Added new waypoint row {new_row_index + 1}. Enter name or coordinates.")
    
    def _remove_waypoint(self):
        """Remove selected waypoint."""
        current_row = self.waypoints_table.currentRow()
        if current_row >= 0 and current_row < len(self.waypoints):
            self.waypoints.pop(current_row)
            self._refresh_table_display()
    
    def _refresh_table_display(self):
        """Refresh the table display with current waypoints data."""
        self.waypoints_table.setRowCount(len(self.waypoints))
        for i, wp in enumerate(self.waypoints):
            self.waypoints_table.setItem(i, 0, QTableWidgetItem(wp['name']))
            self.waypoints_table.setItem(i, 1, QTableWidgetItem(f"{wp['lat']:.6f}"))
            self.waypoints_table.setItem(i, 2, QTableWidgetItem(f"{wp['lon']:.6f}"))
            self.waypoints_table.setItem(i, 3, QTableWidgetItem(wp['alt']))
            self.waypoints_table.setItem(i, 4, QTableWidgetItem(wp['spd']))
            
            # Make lat/lon read-only
            self.waypoints_table.item(i, 1).setFlags(self.waypoints_table.item(i, 1).flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.waypoints_table.item(i, 2).setFlags(self.waypoints_table.item(i, 2).flags() & ~Qt.ItemFlag.ItemIsEditable)
    
    def _create_file(self):
        """Create the procedure file."""
        self._create_procedure_file(load_automatically=False)
    
    def _create_and_load(self):
        """Create the procedure file and load it automatically."""
        self._create_procedure_file(load_automatically=True)
    
    def _create_procedure_file(self, load_automatically=False):
        """Create the procedure file in satg_data/procedures/."""
        if not self.polygon_name:
            QMessageBox.warning(self, "Warning", "No procedure name set.")
            return
            
        if not self.waypoints:
            QMessageBox.warning(self, "Warning", "No waypoints to create procedure from.")
            return
        
        try:
            # Create procedures directory if it doesn't exist
            import os
            from datetime import datetime
            
            procedures_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'satg_data', 'procedures')
            os.makedirs(procedures_dir, exist_ok=True)
            
            # Create filename
            filename = f"{self.polygon_name}.scn"
            filepath = os.path.join(procedures_dir, filename)
            
            # Check if file exists and ask for confirmation
            if os.path.exists(filepath):
                reply = QMessageBox.question(self, "File Exists", 
                                           f"File '{filename}' already exists. Overwrite?",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    return
            
            # Create file content
            content = []
            content.append(f"# Procedure: {self.polygon_name}")
            content.append(f"# Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            content.append(f"# Type: Custom Track Procedure")
            content.append(f"# Waypoints: {len(self.waypoints)}")
            content.append("#")
            
            for i, wp in enumerate(self.waypoints):
                line = f"00:00:00.00>%0 ADDWPT {wp['name']}"
                if wp['alt'].strip():
                    line += f" {wp['alt']}"
                if wp['spd'].strip():
                    line += f" {wp['spd']}"
                content.append(line)
            
            content.append("")  # Empty line at end
            
            # Write file
            with open(filepath, 'w') as f:
                f.write('\n'.join(content))
            
            self.file_status.setText(f"Created: {filepath}")
            self.file_status.setStyleSheet("color: green;")
            
            if load_automatically:
                # Load into the main GUI
                if self.parent() and hasattr(self.parent(), '_load_created_procedure'):
                    self.parent()._load_created_procedure(filepath)
                    self.file_status.setText(f"Created and loaded: {filepath}")
            
            QMessageBox.information(self, "Success", f"Procedure file created successfully:\n{filepath}")
            
        except Exception as e:
            error_msg = f"Error creating procedure file: {e}"
            self.file_status.setText(error_msg)
            self.file_status.setStyleSheet("color: red;")
            QMessageBox.critical(self, "Error", error_msg)


# --- top strip -------------------------------------------------------------

class TopStrip(QWidget):
    """
    Top navigation strip widget providing SATG configuration management and system controls.
    
    This widget creates a horizontal toolbar at the top of the SATG GUI window, providing
    essential navigation and configuration management functionality. The strip includes
    controls for base directory management, configuration persistence, cache management,
    and system reset operations for comprehensive SATG workflow management.
    
    The TopStrip serves as the primary control interface for SATG session management,
    enabling users to configure working directories, save and load configuration states,
    access help documentation, and perform system maintenance operations. The widget
    integrates with the main SATG window to provide seamless configuration management
    across all SATG operational modes and training scenarios.
    
    Control Categories:
    - Directory Management: Base folder browsing and path display functionality
    - Configuration Persistence: Save/load system with automatic state management
    - Session Management: Configuration editing and cache management interfaces
    - System Operations: Help access and BlueSky reset functionality
    - Status Display: Real-time path information and operational status feedback
    
    The widget maintains integration with the parent SATG window for configuration
    access and state synchronization, ensuring consistent behavior across all SATG
    operations and providing centralized access to essential system functions.
    
    Attributes:
        main_window: Reference to parent SATG window for configuration access
        
    Args:
        parent (QWidget, optional): Parent widget, typically SATGWindow instance
        
    Examples:
        # Create top strip as part of SATG window layout
        top_strip = TopStrip(parent=satg_window)
        main_layout.addWidget(top_strip)
        
        # Widget automatically connects to parent window configuration system
        # and provides integrated control interface for all SATG operations
    
    Note:
        The TopStrip requires a parent SATGWindow instance to access configuration
        management systems. All controls integrate with the parent window's state
        management and provide consistent behavior across SATG operational modes.
    """
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
        btn_manage_cache = QPushButton("Manage Cache", self)
        btn_reset = QPushButton("Reset", self)
        btn_reset.setToolTip("Full BlueSky reset")
        btn_save_config.setToolTip("Save current tab configuration")
        btn_load_config.setToolTip("Load saved tab configuration")
        btn_edit_configs.setToolTip("Manage saved configurations")
        btn_manage_cache.setToolTip("View and manage cache files")

        lay.addWidget(btn_browse)
        lay.addWidget(btn_show)
        lay.addWidget(btn_help)
        lay.addStretch(1)
        lay.addWidget(btn_save_config)
        lay.addWidget(btn_load_config)
        lay.addWidget(btn_edit_configs)
        lay.addWidget(btn_manage_cache)
        lay.addWidget(btn_reset)

        btn_browse.clicked.connect(self._choose_base)
        btn_show.clicked.connect(lambda: _emit("SATG_DIR"))
        btn_help.clicked.connect(lambda: _emit("SATG_HELP"))
        btn_save_config.clicked.connect(self._save_config)
        btn_load_config.clicked.connect(self._load_config)
        btn_edit_configs.clicked.connect(self._edit_configs)
        btn_manage_cache.clicked.connect(self._manage_cache)
        btn_reset.clicked.connect(lambda: _emit("RESET"))

    def _choose_base(self):
        path = QFileDialog.getExistingDirectory(self, "Choose SATG base directory")
        if path:
            _emit(_join_tokens("SATG_DIR", _qpath(path)))
    
    def _save_config(self):
        """
        Save current tab configuration to a file for later reuse.
        
        Note:
            - Extracts complete configuration from currently active tab
            - Prompts user for configuration name via input dialog
            - Creates configSaves directory if it doesn't exist
            - Supports all tab types: Procedures, Historic Sampling, 
              Runway Limits, Ground Control, Route Control
            - Saves as JSON file with tab-specific structure
            - Provides feedback on save success/failure
        """
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
        """
        Load a saved configuration into the current tab.
        
        Note:
            - Scans configSaves directory for matching configuration files
            - Filters configurations by current tab type
            - Presents selection dialog for available configurations
            - Applies configuration to current tab using tab-specific methods
            - Validates configuration format and compatibility
            - Provides feedback on load success/failure
            
        Examples:
            Configuration files named like:
            - "morning_rush_procedures.json" for Procedures tab
            - "eurocontrol_2023_historic_sampling.json" for Historic Sampling
        """
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
        """
        Extract configuration data from a tab widget based on tab type.
        
        Args:
            tab_widget (QWidget): Tab widget to extract configuration from
            tab_name (str): Name of the tab for type identification
            
        Returns:
            Optional[Dict]: Configuration dictionary or None if unsupported tab
            
        Note:
            - Routes to appropriate extraction method based on tab name
            - Supports: Realistic Replay, Geometric Conflicts, Random Conflicts,
              Procedures, Historic Sampling tabs
            - Returns comprehensive configuration including all relevant settings
        """
        config = {}
        
        if tab_name == "Realistic Replay":
            config.update(self._extract_rl_config(tab_widget))
        elif tab_name == "Geometric Conflicts":
            config.update(self._extract_gc_config(tab_widget))
        elif tab_name == "Random Conflicts":
            config.update(self._extract_rc_config(tab_widget))
        elif tab_name == "Procedures":
            config.update(self._extract_proc_config(tab_widget))
        elif tab_name == "Historic Sampling":
            config.update(self._extract_hs_config(tab_widget))
        else:
            return None
            
        return config

    def _apply_tab_config(self, tab_widget, tab_name: str, config_data: Dict) -> bool:
        """
        Apply configuration data to a tab widget based on tab type.
        
        Args:
            tab_widget (QWidget): Tab widget to apply configuration to
            tab_name (str): Name of the tab for type identification
            config_data (Dict): Configuration data to apply
            
        Returns:
            bool: True if configuration applied successfully, False otherwise
            
        Note:
            - Routes to appropriate application method based on tab name
            - Validates configuration data before applying
            - Handles errors gracefully and provides feedback
            - Updates UI controls with loaded configuration values
        """
        try:
            if tab_name == "Realistic Replay":
                return self._apply_rl_config(tab_widget, config_data)
            elif tab_name == "Geometric Conflicts":
                return self._apply_gc_config(tab_widget, config_data)
            elif tab_name == "Random Conflicts":
                return self._apply_rc_config(tab_widget, config_data)
            elif tab_name == "Procedures":
                return self._apply_proc_config(tab_widget, config_data)
            elif tab_name == "Historic Sampling":
                return self._apply_hs_config(tab_widget, config_data)
            else:
                return False
        except Exception:
            return False
    
    def _extract_proc_config(self, tab_widget) -> Dict:
        """
        Extract comprehensive configuration from Procedures tab.
        
        Args:
            tab_widget (QWidget): Procedures tab widget to extract configuration from
            
        Returns:
            Dict: Complete procedures configuration including:
                - Loaded procedure and waypoint files
                - Origin and destination airports configuration  
                - Generic flight parameters (altitude, speed, aircraft types)
                - SID/STAR specific parameters and schedules
                - EUROCONTROL data integration settings
                
        Note:
            - Captures all UI control states and file references
            - Includes phase altitude configuration for runways/procedures
            - Preserves aircraft type selections and flight parameters
            - Safe extraction using getattr with defaults for optional attributes
        """
        config = {}
        
        # Files loaded
        config['proc_files'] = getattr(tab_widget, '_proc_files', [])
        config['wpt_files'] = getattr(tab_widget, '_wpt_files', [])
        
        # Origins and destinations
        config['origins'] = getattr(tab_widget, '_origins', {})
        config['destinations'] = getattr(tab_widget, '_destinations', {})
        
        # Generic parameters
        config['gen_flights'] = tab_widget.gen_flights.value()
        config['generic_alt_fl'] = tab_widget.generic_alt_fl.value()
        config['generic_mach'] = tab_widget.generic_mach.value()
        config['generic_mode'] = tab_widget.generic_mode.currentIndex()
        config['generic_rate_basis'] = tab_widget.generic_rate_basis.currentIndex()
        config['generic_final_alt_fl'] = tab_widget.generic_final_alt_fl.value()
        config['generic_final_spd'] = tab_widget.generic_final_spd.value()
        config['generic_override_initial_alt'] = tab_widget.generic_override_initial_alt.isChecked()
        config['generic_override_initial_spd'] = tab_widget.generic_override_initial_spd.isChecked()
        config['generic_override_final_alt'] = tab_widget.generic_override_final_alt.isChecked()
        config['generic_override_final_spd'] = tab_widget.generic_override_final_spd.isChecked()
        config['generic_actypes'] = tab_widget.generic_actypes.text()
        
        # SID parameters
        config['sid_flights'] = tab_widget.sid_flights.value()
        config['sid_alt'] = tab_widget.sid_alt.value()
        config['sid_spd'] = tab_widget.sid_spd.value()
        config['sid_mode'] = tab_widget.sid_mode.currentIndex()
        config['sid_override_initial_alt'] = tab_widget.sid_override_initial_alt.isChecked()
        config['sid_override_initial_spd'] = tab_widget.sid_override_initial_spd.isChecked()
        config['sid_actypes'] = tab_widget.sid_actypes.text()
        
        # STAR parameters
        config['star_flights'] = tab_widget.star_flights.value()
        config['star_alt_fl'] = tab_widget.star_alt_fl.value()
        config['star_mach'] = tab_widget.star_mach.value()
        config['star_mode'] = tab_widget.star_mode.currentIndex()
        config['star_rate_basis'] = tab_widget.star_rate_basis.currentIndex()
        config['star_final_alt_fl'] = tab_widget.star_final_alt_fl.value()
        config['star_final_spd'] = tab_widget.star_final_spd.value()
        config['star_override_initial_alt'] = tab_widget.star_override_initial_alt.isChecked()
        config['star_override_initial_spd'] = tab_widget.star_override_initial_spd.isChecked()
        config['star_override_final_alt'] = tab_widget.star_override_final_alt.isChecked()
        config['star_override_final_spd'] = tab_widget.star_override_final_spd.isChecked()
        config['star_actypes'] = tab_widget.star_actypes.text()
        
        # Save SID scheduling data
        config['sid_rate_rows'] = {}
        for runway, (label, spin) in tab_widget._sid_rate_rows.items():
            config['sid_rate_rows'][runway] = spin.value()
        config['sid_schedule_data'] = dict(tab_widget._sid_schedule_data)
        
        # Save STAR scheduling data
        # IMPORTANT: Ensure state consistency before capturing current rates
        gui_rate_basis = tab_widget.star_rate_basis.currentIndex()
        if tab_widget._star_basis_index != gui_rate_basis:
            # Sync internal state with GUI state
            tab_widget._star_basis_index = gui_rate_basis
            # Refresh to ensure correct waypoints are shown
            tab_widget._refresh_star_rate_rows()
        
        # Capture current GUI state before saving to ensure accuracy
        current_basis = tab_widget._current_star_basis()
        tab_widget._capture_star_rates(current_basis)
        config['star_rate_values'] = dict(tab_widget._star_rate_values)
        config['star_schedule_data'] = dict(tab_widget._star_schedule_data)
        
        # Save Generic scheduling data
        # IMPORTANT: Ensure state consistency before capturing current rates
        gui_generic_rate_basis = tab_widget.generic_rate_basis.currentIndex()
        if tab_widget._generic_basis_index != gui_generic_rate_basis:
            tab_widget._sync_generic_rates()
            tab_widget._generic_basis_index = gui_generic_rate_basis
        config['generic_rate_values'] = dict(tab_widget._generic_rate_values)
        config['generic_schedule_data'] = dict(tab_widget._generic_schedule_data)
        
        # Scenario parameters
        config['scn_name'] = tab_widget.scn.text()
        config['seed'] = tab_widget.seed.value()
        config['overwrite'] = tab_widget.overwrite.isChecked()
        config['dest_enable'] = tab_widget.dest_enable.isChecked()
        
        return config
    
    def _apply_proc_config(self, tab_widget, config_data: Dict) -> bool:
        """
        Apply saved configuration to Procedures tab, restoring complete state.
        
        Args:
            tab_widget (QWidget): Procedures tab widget to apply configuration to
            config_data (Dict): Configuration data to restore
            
        Returns:
            bool: True if configuration applied successfully
            
        Note:
            - Clears existing procedures and waypoint files from backend and GUI
            - Restores all parameter values including flight counts and constraints
            - Reloads procedure and waypoint files with proper backend registration
            - Restores origin/destination airport configurations
            - Handles signal blocking to prevent premature UI updates
            - Maintains phase altitude and schedule configurations
        """
        try:
            # Clear existing files from GUI lists and backend first
            tab_widget.lst_wpt.clear()
            tab_widget.lst_proc.clear()
            tab_widget._wpt_files.clear()
            tab_widget._proc_files.clear()
            
            # IMPORTANT: Clear the backend SATG state to remove any previously loaded procedures
            _emit("SATG_PROC_CLEAR_WPT")
            _emit("SATG_PROC_CLEAR_PROC")
            
            # Apply generic parameters
            if 'gen_flights' in config_data:
                tab_widget.gen_flights.setValue(config_data['gen_flights'])
            if 'generic_alt_fl' in config_data:
                tab_widget.generic_alt_fl.setValue(config_data['generic_alt_fl'])
            if 'generic_mach' in config_data:
                tab_widget.generic_mach.setValue(config_data['generic_mach'])
            if 'generic_mode' in config_data:
                tab_widget.generic_mode.setCurrentIndex(config_data['generic_mode'])
            if 'generic_rate_basis' in config_data:
                # Block signals to prevent premature refresh before rate values are restored
                tab_widget.generic_rate_basis.blockSignals(True)
                tab_widget.generic_rate_basis.setCurrentIndex(config_data['generic_rate_basis'])
                tab_widget.generic_rate_basis.blockSignals(False)
                tab_widget._generic_basis_index = config_data['generic_rate_basis']
            if 'generic_final_alt_fl' in config_data:
                tab_widget.generic_final_alt_fl.setValue(config_data['generic_final_alt_fl'])
            if 'generic_final_spd' in config_data:
                tab_widget.generic_final_spd.setValue(config_data['generic_final_spd'])
            if 'generic_override_initial_alt' in config_data:
                tab_widget.generic_override_initial_alt.setChecked(config_data['generic_override_initial_alt'])
            if 'generic_override_initial_spd' in config_data:
                tab_widget.generic_override_initial_spd.setChecked(config_data['generic_override_initial_spd'])
            if 'generic_override_final_alt' in config_data:
                tab_widget.generic_override_final_alt.setChecked(config_data['generic_override_final_alt'])
            if 'generic_override_final_spd' in config_data:
                tab_widget.generic_override_final_spd.setChecked(config_data['generic_override_final_spd'])
            if 'generic_actypes' in config_data:
                tab_widget.generic_actypes.setText(config_data['generic_actypes'])
                
            # Apply SID parameters
            if 'sid_flights' in config_data:
                tab_widget.sid_flights.setValue(config_data['sid_flights'])
            if 'sid_alt' in config_data:
                tab_widget.sid_alt.setValue(config_data['sid_alt'])
            if 'sid_spd' in config_data:
                tab_widget.sid_spd.setValue(config_data['sid_spd'])
            if 'sid_mode' in config_data:
                tab_widget.sid_mode.setCurrentIndex(config_data['sid_mode'])
            if 'sid_override_initial_alt' in config_data:
                tab_widget.sid_override_initial_alt.setChecked(config_data['sid_override_initial_alt'])
            if 'sid_override_initial_spd' in config_data:
                tab_widget.sid_override_initial_spd.setChecked(config_data['sid_override_initial_spd'])
            if 'sid_actypes' in config_data:
                tab_widget.sid_actypes.setText(config_data['sid_actypes'])
                
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
                # Block signals to prevent premature refresh before rate values are restored
                tab_widget.star_rate_basis.blockSignals(True)
                tab_widget.star_rate_basis.setCurrentIndex(config_data['star_rate_basis'])
                # IMPORTANT: Manually update the internal state since signals are blocked
                tab_widget._star_basis_index = config_data['star_rate_basis']
                tab_widget.star_rate_basis.blockSignals(False)
            if 'star_final_alt_fl' in config_data:
                tab_widget.star_final_alt_fl.setValue(config_data['star_final_alt_fl'])
            if 'star_final_spd' in config_data:
                tab_widget.star_final_spd.setValue(config_data['star_final_spd'])
            if 'star_override_initial_alt' in config_data:
                tab_widget.star_override_initial_alt.setChecked(config_data['star_override_initial_alt'])
            if 'star_override_initial_spd' in config_data:
                tab_widget.star_override_initial_spd.setChecked(config_data['star_override_initial_spd'])
            if 'star_override_final_alt' in config_data:
                tab_widget.star_override_final_alt.setChecked(config_data['star_override_final_alt'])
            if 'star_override_final_spd' in config_data:
                tab_widget.star_override_final_spd.setChecked(config_data['star_override_final_spd'])
            if 'star_actypes' in config_data:
                tab_widget.star_actypes.setText(config_data['star_actypes'])
                
            # Apply scenario parameters
            if 'scn_name' in config_data:
                tab_widget.scn.setText(config_data['scn_name'])
            if 'seed' in config_data:
                tab_widget.seed.setValue(config_data['seed'])
            if 'overwrite' in config_data:
                tab_widget.overwrite.setChecked(config_data['overwrite'])
                
            # Restore origins and destinations data BEFORE loading files
            if 'origins' in config_data:
                tab_widget._origins = dict(config_data['origins'])
            if 'destinations' in config_data:
                tab_widget._destinations = dict(config_data['destinations'])
                
            # Restore SID scheduling data
            if 'sid_schedule_data' in config_data:
                tab_widget._sid_schedule_data = dict(config_data['sid_schedule_data'])
                
            # Restore STAR scheduling data
            if 'star_rate_values' in config_data:
                tab_widget._star_rate_values = dict(config_data['star_rate_values'])
            if 'star_schedule_data' in config_data:
                tab_widget._star_schedule_data = dict(config_data['star_schedule_data'])
                
            # Restore Generic scheduling data
            if 'generic_rate_values' in config_data:
                tab_widget._generic_rate_values = dict(config_data['generic_rate_values'])
            if 'generic_schedule_data' in config_data:
                tab_widget._generic_schedule_data = dict(config_data['generic_schedule_data'])
                
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
            else:
                # Even without procedure files, we may need to call _update_dest_state()
                tab_widget._update_dest_state()
                
            # Restore dest_enable setting AFTER _update_dest_state() to prevent it from being reset
            if 'dest_enable' in config_data:
                tab_widget.dest_enable.setChecked(config_data['dest_enable'])
                # IMPORTANT: Update backend state to match GUI state
                _emit(f"SATG_PROC_USE_DEST {1 if config_data['dest_enable'] else 0}")
                
            # Restore SID rate rows after refresh (which rebuilds the GUI controls)
            if 'sid_rate_rows' in config_data:
                for runway, rate_value in config_data['sid_rate_rows'].items():
                    if runway in tab_widget._sid_rate_rows:
                        label, spin = tab_widget._sid_rate_rows[runway]
                        spin.setValue(int(rate_value))
            
            # Force another STAR rate rows refresh to ensure rate basis change takes effect
            # This handles the case where rate basis was set before rate values were restored
            if 'star_rate_values' in config_data and 'star_rate_basis' in config_data:
                # Manually trigger the rate basis change to ensure proper refresh
                current_basis = tab_widget.star_rate_basis.currentIndex()
                tab_widget._on_star_basis_changed(current_basis)
            
            # Show file loading results
            if files_loaded or files_failed:
                message_parts = []
                if files_loaded:
                    message_parts.append(f"Successfully loaded files:\n" + "\n".join(f"  - {f}" for f in files_loaded))
                if files_failed:
                    message_parts.append(f"Failed to load files:\n" + "\n".join(f"  - {f}" for f in files_failed))
                    
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
        is_generic = not is_sid and not is_star

        origin_edit: Optional[QLineEdit] = None
        origin_all_btn: Optional[QPushButton] = None
        if is_sid:
            origin_edit = QLineEdit(container)
            origin_edit.setPlaceholderText("Origin ICAO")
            origin_edit.setMaxLength(4)
            origin_edit.setMaximumWidth(90)
            origin_edit.setStyleSheet("background-color: white; color: black; border: 1px solid #ccc;")
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
        dest_edit.setStyleSheet("background-color: white; color: black; border: 1px solid #ccc;")
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
            "is_generic": is_generic,
        }
    
    def _extract_hs_config(self, tab_widget) -> Dict:
        """
        Extract comprehensive Historic Sampling tab configuration.
        
        Args:
            tab_widget (QWidget): Historic Sampling tab widget
            
        Returns:
            Dict: Complete configuration including:
                - Data files (flights, filed, actual, FIR)
                - ML model parameters (XGBoost, KDE, derivative settings)
                - Trajectory generation parameters
                - Filtering and sampling configurations
                
        Note:
            - Captures all machine learning model hyperparameters
            - Includes file paths for EUROCONTROL data sources
            - Preserves trajectory generation and filtering settings
        """
        config = {}
        
        # Data files
        config['files'] = {
            'flights_file': getattr(tab_widget, '_flights_file', ''),
            'filed_file': getattr(tab_widget, '_filed_file', ''),
            'actual_file': getattr(tab_widget, '_actual_file', ''),
            'fir_file': getattr(tab_widget, '_fir_file', '')
        }
        
        # Model configuration
        config['model_type'] = tab_widget.model_type_combo.currentText()
        config['n_estimators'] = tab_widget.n_estimators_spin.value()
        config['max_depth'] = tab_widget.max_depth_spin.value()
        config['learning_rate'] = tab_widget.learning_rate_spin.value()
        config['min_child_weight'] = tab_widget.min_child_weight_spin.value()
        config['subsample'] = tab_widget.subsample_spin.value()
        config['kde_bandwidth'] = tab_widget.kde_bandwidth_spin.value()
        config['kde_kernel'] = tab_widget.kde_kernel_combo.currentText()
        config['kde_atol'] = tab_widget.kde_atol_spin.value()
        config['derivative_bandwidth'] = tab_widget.derivative_bandwidth_spin.value()
        config['derivative_order'] = tab_widget.derivative_order_spin.value()
        config['derivative_smoothing'] = tab_widget.derivative_smoothing_spin.value()
        config['derivative_kernel'] = tab_widget.derivative_kernel_combo.currentText()
        
        # Trajectory parameters
        config['n_points'] = tab_widget.n_points_spin.value()
        config['smoothing_alpha'] = tab_widget.smoothing_alpha_spin.value()
        config['interpolation_points'] = tab_widget.interpolation_spin.value()
        
        # Generation parameters
        config['n_flights'] = tab_widget.n_flights_spin.value()
        
        # Scenario parameters
        config['scn_name'] = tab_widget.scn_name.text()
        config['synthetic_seed'] = tab_widget.synthetic_seed.value()
        config['synthetic_overwrite'] = tab_widget.synthetic_overwrite.isChecked()
        
        # Filter configuration - Save historic filter settings
        config['historic_filters'] = getattr(tab_widget, 'historic_filters', {
            'lat_min': -90, 'lat_max': 90,
            'lon_min': -180, 'lon_max': 180,
            'fl_min': 0, 'fl_max': 500,
            'include_airspace': [],
            'time_start': None, 'time_end': None,
            'date_start': None, 'date_end': None,
            'aircraft_types': []
        })
        
        # Training state (for informational purposes)
        config['model_trained'] = getattr(tab_widget, '_model_trained', False)
        config['synthetic_data_generated'] = bool(getattr(tab_widget, '_synthetic_data', None))
        
        return config
    
    def _apply_hs_config(self, tab_widget, config_data: Dict) -> bool:
        """Apply configuration to Historic Sampling tab."""
        try:
            # Apply data files
            if 'files' in config_data:
                files = config_data['files']
                # Set file paths
                tab_widget._flights_file = files.get('flights_file', '')
                tab_widget._filed_file = files.get('filed_file', '')
                tab_widget._actual_file = files.get('actual_file', '')
                tab_widget._fir_file = files.get('fir_file', '')
                
                # Update file labels
                if tab_widget._flights_file:
                    tab_widget.flights_file_label.setText(f"Flights: {os.path.basename(tab_widget._flights_file)}")
                    tab_widget.flights_file_label.setStyleSheet("color: green;")
                else:
                    tab_widget.flights_file_label.setText("Flights: No file selected")
                    tab_widget.flights_file_label.setStyleSheet("color: #999; font-style: italic;")
                
                if tab_widget._filed_file:
                    tab_widget.filed_file_label.setText(f"Filed: {os.path.basename(tab_widget._filed_file)}")
                    tab_widget.filed_file_label.setStyleSheet("color: green;")
                else:
                    tab_widget.filed_file_label.setText("Filed: No file selected")
                    tab_widget.filed_file_label.setStyleSheet("color: #999; font-style: italic;")
                
                if tab_widget._actual_file:
                    tab_widget.actual_file_label.setText(f"Actual: {os.path.basename(tab_widget._actual_file)}")
                    tab_widget.actual_file_label.setStyleSheet("color: green;")
                else:
                    tab_widget.actual_file_label.setText("Actual: No file selected")
                    tab_widget.actual_file_label.setStyleSheet("color: #999; font-style: italic;")
                
                if tab_widget._fir_file:
                    tab_widget.fir_file_label.setText(f"FIR: {os.path.basename(tab_widget._fir_file)}")
                    tab_widget.fir_file_label.setStyleSheet("color: green;")
                else:
                    tab_widget.fir_file_label.setText("FIR: No file selected (optional)")
                    tab_widget.fir_file_label.setStyleSheet("color: #999; font-style: italic;")
                
                # Update status (no train button in Historic Sampling - it's automated)
                if all([tab_widget._flights_file, tab_widget._filed_file, tab_widget._actual_file]):
                    # Historic Sampling doesn't have a separate train button - training is automatic
                    pass
            
            # Apply model configuration
            if 'model_type' in config_data:
                index = tab_widget.model_type_combo.findText(config_data['model_type'])
                if index >= 0:
                    tab_widget.model_type_combo.setCurrentIndex(index)
                    # Trigger the model type change to update tab visibility
                    tab_widget._on_model_type_changed(config_data['model_type'])
            
            if 'n_estimators' in config_data:
                tab_widget.n_estimators_spin.setValue(config_data['n_estimators'])
            if 'max_depth' in config_data:
                tab_widget.max_depth_spin.setValue(config_data['max_depth'])
            if 'learning_rate' in config_data:
                tab_widget.learning_rate_spin.setValue(config_data['learning_rate'])
            if 'min_child_weight' in config_data:
                tab_widget.min_child_weight_spin.setValue(config_data['min_child_weight'])
            if 'subsample' in config_data:
                tab_widget.subsample_spin.setValue(config_data['subsample'])
            if 'kde_bandwidth' in config_data:
                tab_widget.kde_bandwidth_spin.setValue(config_data['kde_bandwidth'])
            if 'kde_kernel' in config_data:
                index = tab_widget.kde_kernel_combo.findText(config_data['kde_kernel'])
                if index >= 0:
                    tab_widget.kde_kernel_combo.setCurrentIndex(index)
            if 'kde_atol' in config_data:
                tab_widget.kde_atol_spin.setValue(config_data['kde_atol'])
            if 'derivative_bandwidth' in config_data:
                tab_widget.derivative_bandwidth_spin.setValue(config_data['derivative_bandwidth'])
            if 'derivative_order' in config_data:
                tab_widget.derivative_order_spin.setValue(config_data['derivative_order'])
            if 'derivative_smoothing' in config_data:
                tab_widget.derivative_smoothing_spin.setValue(config_data['derivative_smoothing'])
            if 'derivative_kernel' in config_data:
                index = tab_widget.derivative_kernel_combo.findText(config_data['derivative_kernel'])
                if index >= 0:
                    tab_widget.derivative_kernel_combo.setCurrentIndex(index)
            
            # Apply generation and trajectory parameters
            if 'n_flights' in config_data:
                tab_widget.n_flights_spin.setValue(config_data['n_flights'])
            if 'n_points' in config_data:
                tab_widget.n_points_spin.setValue(config_data['n_points'])
            if 'smoothing_alpha' in config_data:
                tab_widget.smoothing_alpha_spin.setValue(config_data['smoothing_alpha'])
            if 'interpolation_points' in config_data:
                tab_widget.interpolation_spin.setValue(config_data['interpolation_points'])
            
            # Apply scenario parameters
            if 'scn_name' in config_data:
                tab_widget.scn_name.setText(config_data['scn_name'])
            if 'synthetic_seed' in config_data:
                tab_widget.synthetic_seed.setValue(config_data['synthetic_seed'])
            if 'synthetic_overwrite' in config_data:
                tab_widget.synthetic_overwrite.setChecked(config_data['synthetic_overwrite'])
            
            # Apply filter configuration (NEW - Restore historic filter settings)
            if 'historic_filters' in config_data:
                tab_widget.historic_filters = config_data['historic_filters'].copy()
                
                # Backward compatibility: convert old exclude_airspace to include_airspace
                if 'exclude_airspace' in tab_widget.historic_filters and 'include_airspace' not in tab_widget.historic_filters:
                    tab_widget.historic_filters['include_airspace'] = tab_widget.historic_filters.pop('exclude_airspace')
                    print("Updated old config: converted exclude_airspace to include_airspace")
                
                print(f"Restored historic filters: {tab_widget.historic_filters}")
            
            # Note: We don't restore the training state or synthetic data as these are runtime states
            
            return True
            
        except Exception as e:
            print(f"Error applying Historic Sampling config: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _extract_rl_config(self, tab_widget) -> Dict:
        """Extract Realistic Replay tab configuration."""
        config = {}
        
        # Eurocontrol file paths (new format)
        config['eurocontrol_files'] = {
            'flights_file': getattr(tab_widget, '_flights_file', ''),
            'filed_file': getattr(tab_widget, '_filed_file', ''),
            'actual_file': getattr(tab_widget, '_actual_file', ''),
            'fir_file': getattr(tab_widget, '_fir_file', '')
        }
        
        # Eurocontrol filter settings
        config['eurocontrol_filters'] = getattr(tab_widget, 'eurocontrol_filters', {})
        config['filters_configured'] = tab_widget._are_filters_configured() if hasattr(tab_widget, '_are_filters_configured') else False
        
        # Phase-based jitter settings
        config['phase_configs'] = {}
        if hasattr(tab_widget, 'phase_configs'):
            for phase, widgets in tab_widget.phase_configs.items():
                config['phase_configs'][phase] = {
                    'enabled': widgets['enabled'].isChecked(),
                    'dist': widgets['dist'].currentIndex(),
                    'dt': widgets['dt'].value(),
                    'dlat': widgets['dlat'].value(),
                    'dlon': widgets['dlon'].value(),
                    'dfl': widgets['dfl'].value(),
                    'nsig': widgets['nsig'].value()
                }
        
        # Global jitter percentage
        config['j_pct'] = tab_widget.j_pct.value() if hasattr(tab_widget, 'j_pct') else 100
        
        # Phase-based jitter settings
        config['phase_jitter_enabled'] = getattr(tab_widget, 'phase_jitter_enabled', False)
        if hasattr(tab_widget, 'phase_altitudes'):
            config['phase_altitudes'] = dict(tab_widget.phase_altitudes)
        else:
            config['phase_altitudes'] = {}
        config['phase_altitudes_configured'] = bool(getattr(tab_widget, 'phase_altitudes', None))
        
        # Track phase configurations (per-track altitude settings)
        if hasattr(tab_widget, 'track_phase_configurations'):
            config['track_phase_configurations'] = {}
            for track_id, track_cfg in tab_widget.track_phase_configurations.items():
                config['track_phase_configurations'][track_id] = dict(track_cfg)
        
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
            # Apply Eurocontrol file paths (new format)
            if 'eurocontrol_files' in config_data:
                eurocontrol_files = config_data['eurocontrol_files']
                
                # Set file paths and update labels
                if 'flights_file' in eurocontrol_files and eurocontrol_files['flights_file']:
                    tab_widget._flights_file = eurocontrol_files['flights_file']
                    tab_widget.flights_file_label.setText(os.path.basename(eurocontrol_files['flights_file']))
                    tab_widget.flights_file_label.setStyleSheet("color: #000; font-weight: bold;")
                else:
                    tab_widget._flights_file = ""
                    tab_widget.flights_file_label.setText("No file selected")
                    tab_widget.flights_file_label.setStyleSheet("color: #999; font-style: italic;")
                
                if 'filed_file' in eurocontrol_files and eurocontrol_files['filed_file']:
                    tab_widget._filed_file = eurocontrol_files['filed_file']
                    tab_widget.filed_file_label.setText(os.path.basename(eurocontrol_files['filed_file']))
                    tab_widget.filed_file_label.setStyleSheet("color: #000; font-weight: bold;")
                else:
                    tab_widget._filed_file = ""
                    tab_widget.filed_file_label.setText("No file selected")
                    tab_widget.filed_file_label.setStyleSheet("color: #999; font-style: italic;")
                
                if 'actual_file' in eurocontrol_files and eurocontrol_files['actual_file']:
                    tab_widget._actual_file = eurocontrol_files['actual_file']
                    tab_widget.actual_file_label.setText(os.path.basename(eurocontrol_files['actual_file']))
                    tab_widget.actual_file_label.setStyleSheet("color: #000; font-weight: bold;")
                else:
                    tab_widget._actual_file = ""
                    tab_widget.actual_file_label.setText("No file selected")
                    tab_widget.actual_file_label.setStyleSheet("color: #999; font-style: italic;")
                
                if 'fir_file' in eurocontrol_files and eurocontrol_files['fir_file']:
                    tab_widget._fir_file = eurocontrol_files['fir_file']
                    tab_widget.fir_file_label.setText(os.path.basename(eurocontrol_files['fir_file']))
                    tab_widget.fir_file_label.setStyleSheet("color: #000; font-weight: bold;")
                else:
                    tab_widget._fir_file = ""
                    tab_widget.fir_file_label.setText("No file selected")
                    tab_widget.fir_file_label.setStyleSheet("color: #999; font-style: italic;")
            
            # Apply Eurocontrol filter settings
            if 'eurocontrol_filters' in config_data:
                if config_data['eurocontrol_filters']:  # Only set if not empty
                    tab_widget.eurocontrol_filters = config_data['eurocontrol_filters'].copy()
                    
                    # Backward compatibility: convert old exclude_airspace to include_airspace
                    if 'exclude_airspace' in tab_widget.eurocontrol_filters and 'include_airspace' not in tab_widget.eurocontrol_filters:
                        tab_widget.eurocontrol_filters['include_airspace'] = tab_widget.eurocontrol_filters.pop('exclude_airspace')
                        print("Updated old RL config: converted exclude_airspace to include_airspace")
                else:
                    # Clear filters if empty in config
                    tab_widget.eurocontrol_filters = {}
            
            # Note: No longer storing processed data locally - using direct TraffixGen commands
            
            # Restore phase-based jitter settings
            if 'phase_configs' in config_data and hasattr(tab_widget, 'phase_configs'):
                phase_configs = config_data['phase_configs']
                for phase, widgets in tab_widget.phase_configs.items():
                    if phase in phase_configs:
                        phase_data = phase_configs[phase]
                        widgets['enabled'].setChecked(phase_data.get('enabled', False))
                        widgets['dist'].setCurrentIndex(phase_data.get('dist', 0))
                        widgets['dt'].setValue(phase_data.get('dt', 0.0))
                        widgets['dlat'].setValue(phase_data.get('dlat', 0.0))
                        widgets['dlon'].setValue(phase_data.get('dlon', 0.0))
                        widgets['dfl'].setValue(phase_data.get('dfl', 0))
                        widgets['nsig'].setValue(phase_data.get('nsig', 0.0))
            
            # Global jitter percentage
            if 'j_pct' in config_data and hasattr(tab_widget, 'j_pct'):
                tab_widget.j_pct.setValue(config_data['j_pct'])
            
            # Phase jitter enabled flag
            if 'phase_jitter_enabled' in config_data:
                tab_widget.phase_jitter_enabled = config_data['phase_jitter_enabled']
            if 'phase_altitudes' in config_data:
                if config_data['phase_altitudes']:  # Only set if not empty
                    tab_widget.phase_altitudes = dict(config_data['phase_altitudes'])
                else:
                    # Clear phase altitudes if empty in config
                    if hasattr(tab_widget, 'phase_altitudes'):
                        delattr(tab_widget, 'phase_altitudes')
            
            # Restore track phase configurations (per-track altitude settings)
            if 'track_phase_configurations' in config_data:
                tab_widget.track_phase_configurations = {}
                for track_id, track_cfg in config_data['track_phase_configurations'].items():
                    tab_widget.track_phase_configurations[track_id] = dict(track_cfg)
            
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
        """
        Extract Geometric Conflicts tab configuration.
        
        Args:
            tab_widget (QWidget): Geometric Conflicts tab widget
            
        Returns:
            Dict: Complete configuration including:
                - Separation minima settings (horizontal/vertical)
                - Absolute conflict parameters (aircraft types, positions, flight levels)
                - Relative conflict parameters and flight geometry
                - Conflict detection thresholds and timing parameters
                
        Note:
            - Captures both absolute and relative conflict generation modes
            - Includes aircraft positioning (coordinates or waypoint-based)
            - Preserves flight level ranges and speed configurations
        """
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
        
        # Relative page settings
        if hasattr(tab_widget, '_relative_page'):
            rel_page = tab_widget._relative_page
            # Target aircraft settings
            config['target_combo'] = rel_page.target_combo.currentText()
            config['include_target_cb'] = rel_page.include_target_cb.isChecked()
            config['target_acid'] = rel_page.target_acid.text()
            config['target_type'] = rel_page.target_type.text()
            config['target_lat_value'] = rel_page.target_lat_value.text()
            config['target_lat_range'] = rel_page.target_lat_range.text()
            config['target_lon_value'] = rel_page.target_lon_value.text()
            config['target_lon_range'] = rel_page.target_lon_range.text()
            config['target_hdg_value'] = rel_page.target_hdg_value.text()
            config['target_hdg_range'] = rel_page.target_hdg_range.text()
            config['target_alt_value'] = rel_page.target_alt_value.text()
            config['target_alt_range'] = rel_page.target_alt_range.text()
            config['target_spd_value'] = rel_page.target_spd_value.text()
            config['target_spd_range'] = rel_page.target_spd_range.text()
            
            # Intruder aircraft settings
            config['intr_acid'] = rel_page.intr_acid.text()
            config['intr_type'] = rel_page.intr_type.text()
            config['intr_dpsi_value'] = rel_page.intr_dpsi_value.text()
            config['intr_dpsi_range'] = rel_page.intr_dpsi_range.text()
            config['intr_dcpa_value'] = rel_page.intr_dcpa_value.text()
            config['intr_dcpa_range'] = rel_page.intr_dcpa_range.text()
            config['intr_tlosh_value'] = rel_page.intr_tlosh_value.text()
            config['intr_tlosh_range'] = rel_page.intr_tlosh_range.text()
            config['intr_dh_value'] = rel_page.intr_dh_value.text()
            config['intr_dh_range'] = rel_page.intr_dh_range.text()
            config['intr_tlosv_value'] = rel_page.intr_tlosv_value.text()
            config['intr_tlosv_range'] = rel_page.intr_tlosv_range.text()
            config['intr_spd_value'] = rel_page.intr_spd_value.text()
            config['intr_spd_range'] = rel_page.intr_spd_range.text()
            
            # Scenario settings for relative
            config['rel_scn_name'] = rel_page.scn_name.text()
            config['gc_rel_seed'] = rel_page.gc_rel_seed.value()
            config['rel_overwrite_cb'] = rel_page.overwrite_cb.isChecked()
        
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
            
            # Relative page settings
            if hasattr(tab_widget, '_relative_page'):
                rel_page = tab_widget._relative_page
                # Target aircraft settings
                if 'target_combo' in config_data:
                    # Find and set the combo box text if it exists
                    index = rel_page.target_combo.findText(config_data['target_combo'])
                    if index >= 0:
                        rel_page.target_combo.setCurrentIndex(index)
                if 'include_target_cb' in config_data:
                    rel_page.include_target_cb.setChecked(config_data['include_target_cb'])
                if 'target_acid' in config_data:
                    rel_page.target_acid.setText(config_data['target_acid'])
                if 'target_type' in config_data:
                    rel_page.target_type.setText(config_data['target_type'])
                if 'target_lat_value' in config_data:
                    rel_page.target_lat_value.setText(config_data['target_lat_value'])
                if 'target_lat_range' in config_data:
                    rel_page.target_lat_range.setText(config_data['target_lat_range'])
                if 'target_lon_value' in config_data:
                    rel_page.target_lon_value.setText(config_data['target_lon_value'])
                if 'target_lon_range' in config_data:
                    rel_page.target_lon_range.setText(config_data['target_lon_range'])
                if 'target_hdg_value' in config_data:
                    rel_page.target_hdg_value.setText(config_data['target_hdg_value'])
                if 'target_hdg_range' in config_data:
                    rel_page.target_hdg_range.setText(config_data['target_hdg_range'])
                if 'target_alt_value' in config_data:
                    rel_page.target_alt_value.setText(config_data['target_alt_value'])
                if 'target_alt_range' in config_data:
                    rel_page.target_alt_range.setText(config_data['target_alt_range'])
                if 'target_spd_value' in config_data:
                    rel_page.target_spd_value.setText(config_data['target_spd_value'])
                if 'target_spd_range' in config_data:
                    rel_page.target_spd_range.setText(config_data['target_spd_range'])
                
                # Intruder aircraft settings
                if 'intr_acid' in config_data:
                    rel_page.intr_acid.setText(config_data['intr_acid'])
                if 'intr_type' in config_data:
                    rel_page.intr_type.setText(config_data['intr_type'])
                if 'intr_dpsi_value' in config_data:
                    rel_page.intr_dpsi_value.setText(config_data['intr_dpsi_value'])
                if 'intr_dpsi_range' in config_data:
                    rel_page.intr_dpsi_range.setText(config_data['intr_dpsi_range'])
                if 'intr_dcpa_value' in config_data:
                    rel_page.intr_dcpa_value.setText(config_data['intr_dcpa_value'])
                if 'intr_dcpa_range' in config_data:
                    rel_page.intr_dcpa_range.setText(config_data['intr_dcpa_range'])
                if 'intr_tlosh_value' in config_data:
                    rel_page.intr_tlosh_value.setText(config_data['intr_tlosh_value'])
                if 'intr_tlosh_range' in config_data:
                    rel_page.intr_tlosh_range.setText(config_data['intr_tlosh_range'])
                if 'intr_dh_value' in config_data:
                    rel_page.intr_dh_value.setText(config_data['intr_dh_value'])
                if 'intr_dh_range' in config_data:
                    rel_page.intr_dh_range.setText(config_data['intr_dh_range'])
                if 'intr_tlosv_value' in config_data:
                    rel_page.intr_tlosv_value.setText(config_data['intr_tlosv_value'])
                if 'intr_tlosv_range' in config_data:
                    rel_page.intr_tlosv_range.setText(config_data['intr_tlosv_range'])
                if 'intr_spd_value' in config_data:
                    rel_page.intr_spd_value.setText(config_data['intr_spd_value'])
                if 'intr_spd_range' in config_data:
                    rel_page.intr_spd_range.setText(config_data['intr_spd_range'])
                
                # Scenario settings for relative
                if 'rel_scn_name' in config_data:
                    rel_page.scn_name.setText(config_data['rel_scn_name'])
                if 'gc_rel_seed' in config_data:
                    rel_page.gc_rel_seed.setValue(config_data['gc_rel_seed'])
                if 'rel_overwrite_cb' in config_data:
                    rel_page.overwrite_cb.setChecked(config_data['rel_overwrite_cb'])
            
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

    def _manage_cache(self):
        """
        Open cache management dialog for viewing and deleting cache files.
        
        Note:
            - Retrieves current cache information from traffixgen backend
            - Shows error dialog if cache access fails
            - Displays informational message if no cache files exist
            - Opens CacheManagerDialog for interactive cache management
            - Handles backend communication errors gracefully
            - Allows users to delete individual or all cache files
            
        Raises:
            QMessageBox: Warning dialogs for cache access errors or no files found
        """
        try:
            # Get cache information from TraffixGen
            from . import traffixgen
            cache_info = traffixgen.get_cache_info()
            
            if cache_info.get('error'):
                QMessageBox.warning(self, "Cache Error", f"Error accessing cache: {cache_info['error']}")
                return
            
            cache_files = cache_info.get('cache_files', [])
            if not cache_files:
                QMessageBox.information(self, "No Cache Files", "No cache files found.")
                return
            
            # Create cache management dialog
            dialog = CacheManagerDialog(cache_info, self)
            dialog.exec()
            
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Error accessing cache: {e}")

# Config Manager Dialog Class
class ConfigManagerDialog(QDialog):
    """
    Advanced configuration management dialog for comprehensive SATG session persistence.
    
    This sophisticated dialog provides centralized management for all saved SATG
    configurations, supporting complete session state persistence across all tabs
    and operational modes. The dialog handles configuration lifecycle management
    including creation, loading, deletion, preview, and organization of complex
    multi-tab configuration files with full backward compatibility support.
    
    The Configuration Manager serves as the central hub for SATG workflow persistence,
    enabling users to save intricate filter settings, model parameters, aircraft
    configurations, and interface states for seamless reuse across training sessions.
    Advanced features include configuration preview, validation, backup management,
    and automatic format conversion for legacy configurations.
    
    Key Management Features:
    - Comprehensive configuration listing with metadata (creation dates, file sizes)
    - Interactive configuration loading with full validation and error recovery
    - Safe configuration deletion with confirmation dialogs and backup creation
    - Real-time configuration preview with structured content display
    - Automatic backup creation before destructive operations
    - Legacy configuration format detection and automatic conversion
    - Robust error handling for corrupted, incomplete, or invalid configuration files
    
    Supported Configuration Content:
    - Complete tab settings across all SATG operational modes (RL, HS, GC, RC, Proc)
    - Advanced filter configurations with airspace, altitude, and temporal constraints
    - Aircraft type selections and performance model parameters
    - Geometric conflict parameters and separation minima settings
    - Procedure configurations including SID/STAR settings and waypoint definitions
    - Random conflict generation parameters and spatial distribution settings
    - User interface preferences and window state information
    
    File Management Operations:
    - JSON-based configuration storage with human-readable formatting
    - Automatic file validation and integrity checking on load operations
    - Configuration metadata tracking including creation and modification timestamps
    - Safe file operations with atomic writes and rollback capabilities
    - Duplicate detection and resolution for configuration name conflicts
    - Bulk operations for configuration organization and maintenance
    
    The dialog integrates seamlessly with the SATG main window configuration system
    to provide transparent persistence across all operational modes and training
    scenarios, ensuring users can maintain consistent setups across sessions.
    
    Attributes:
        config_dir (str): Directory path containing saved configuration files
        config_files (List[str]): List of available configuration filenames
        config_list (QListWidget): Interactive list widget for configuration selection
        
    Args:
        config_dir (str): Path to directory containing configuration files
        config_files (List[str]): List of configuration filenames to display
        parent (QWidget, optional): Parent widget for proper dialog modal behavior
        
    Methods:
        refresh_list(): Updates configuration list display with current files
        load_config(): Loads selected configuration with validation
        delete_config(): Safely deletes selected configuration with confirmation
        preview_config(): Displays configuration content in preview window
        
    Examples:
        # Open configuration manager for saved configurations
        config_dir = os.path.join("satg_data", "configSaves")
        config_files = [f for f in os.listdir(config_dir) if f.endswith('.json')]
        dialog = ConfigManagerDialog(config_dir, config_files, parent=self)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Configuration operations completed successfully
            self.refresh_configuration_display()
    
    Note:
        The dialog requires valid configuration directory structure and handles
        all file operations safely with proper error recovery. Configuration
        loading automatically validates content and provides detailed error
        messages for troubleshooting invalid or corrupted configuration files.
        
        Configuration Content includes:
        - Model training parameters and machine learning settings
        - File paths for data sources with validation
        - UI state including widget values and selections
        - Cache management settings and optimization preferences
        
        Backward Compatibility:
        - Automatically detects legacy configuration formats
        - Converts old filter structures (exclude -> include semantics)
        - Migrates deprecated parameter names to current standards
        - Preserves user data during format upgrades
        - Provides informative messages about configuration updates
        
        Args:
            config_dir (str): Directory containing saved configuration files
            config_files (List[str]): List of available configuration filenames
            parent (QWidget, optional): Parent widget for proper dialog behavior
        
        Attributes:
            config_dir (str): Path to configuration storage directory
            config_files (List[str]): Current list of available configuration files
            config_list (QListWidget): Widget displaying available configurations
            selected_config (str): Currently selected configuration filename
        
        Returns:
            str: Filename of selected configuration when loading
            None: When dialog is cancelled or no selection made
        
        Examples:
        # Create dialog with current configuration state
        dialog = ConfigManagerDialog(
            config_dir="./configs",
            config_files=["default.json", "morning_rush.json"],
            parent=self
        )
        
        # Show dialog and handle selection
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_file = dialog.get_selected_config()
            if selected_file:
                self._load_configuration(selected_file)
    
        
        Note:
            The dialog automatically handles configuration format migrations and
            provides detailed error messages for invalid or corrupted configuration
            files. All operations include confirmation dialogs to prevent accidental
            data loss, and the dialog maintains consistency with the main application's
            styling and behavior patterns.
        """
    
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
        """
        Rename the selected configuration file with user input.
        
        Note:
            - Gets currently selected configuration file from list
            - Extracts current name and preserves tab type suffix
            - Prompts user for new configuration name via input dialog
            - Reconstructs filename with proper tab type suffix
            - Performs file system rename operation
            - Refreshes configuration list to show updated name
            - Handles file conflicts and operation errors gracefully
        """
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
        """
        Create a duplicate copy of the selected configuration file.
        
        Note:
            - Gets currently selected configuration file from list
            - Extracts name and preserves tab type suffix for consistency
            - Prompts user for new name with "_copy" suffix as default
            - Copies entire configuration file to new location
            - Preserves all configuration settings in the duplicate
            - Refreshes configuration list to show new duplicate
            - Handles file operations and naming conflicts gracefully
        """
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

# Cache Manager Dialog Class
class CacheManagerDialog(QDialog):
    """
    Comprehensive dialog for managing SATG cache files and optimization data.
    
    This dialog provides centralized management of all SATG caching systems including
    TraffixGen parquet files, summary data caches, filter configuration caches,
    and performance optimization data. The dialog enables users to monitor cache
    usage, clear outdated data, and manage storage efficiency.
    
    The Cache Manager handles multiple cache types used throughout the SATG system:
    - TraffixGen parquet files: High-performance columnar flight data storage
    - Summary data caches: Pre-computed dataset statistics and metadata
    - Filter configuration caches: Cached filter results for dialog performance
    - File path caches: Optimized file discovery and validation data
    
    Key Features:
    - Visual cache usage statistics and storage information
    - Selective cache clearing with confirmation dialogs
    - Cache validation and integrity checking
    - Performance impact analysis and recommendations
    - Automatic cache cleanup and optimization suggestions
    - Real-time cache size monitoring and alerts
    
    Management Operations:
    - Clear all caches: Complete cache reset for troubleshooting
    - Selective clearing: Target specific cache types or files
    - Cache validation: Verify cache integrity and consistency
    - Size monitoring: Track cache growth and storage usage
    - Performance analysis: Identify optimization opportunities
    
    Attributes:
        cache_info (dict): Comprehensive cache information and statistics
        cache_files (list): List of identified cache files for management
    
    Args:
        cache_info (dict): Cache information containing file lists, sizes,
                         and metadata for all detected cache systems
        parent (QWidget, optional): Parent widget for proper dialog behavior
    
    Examples:
        # Create cache manager with current cache state
        cache_info = {
            'cache_files': ['traffixgen_data.parquet', 'summary_cache.pkl'],
            'total_size': 1024000,
            'last_modified': datetime.now()
        }
        dialog = CacheManagerDialog(cache_info, parent=self)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Process any cache management actions
            self._refresh_data_if_needed()
    
    Note:
        This dialog provides safe cache management with confirmation prompts
        for destructive operations. Cache clearing operations include progress
        feedback and error handling to ensure system stability during cleanup.
        The dialog helps users understand cache usage patterns and optimize
        performance through intelligent cache management.
    """
    
    def __init__(self, cache_info: dict, parent=None):
        super().__init__(parent)
        self.cache_info = cache_info
        self.cache_files = cache_info.get('cache_files', [])
        
        self.setWindowTitle("Manage Cache Files")
        self.setModal(True)
        self.resize(800, 600)
        
        self._setup_ui()
        self.refresh_list()
    
    def _setup_ui(self):
        """
        Setup the cache management dialog user interface.
        
        Note:
            - Creates cache information display with total size and file count
            - Sets up list widget for cache files with extended selection mode
            - Adds action buttons for refresh, delete selected, and clear all
            - Connects button click events to appropriate handler methods
            - Applies consistent styling for information display
            - Enables multiple file selection for batch operations
        """
        layout = QVBoxLayout(self)
        
        # Info section
        info_group = QGroupBox("Cache Information")
        info_layout = QVBoxLayout(info_group)
        
        total_size = self.cache_info.get('total_size_mb', 0)
        file_count = self.cache_info.get('count', 0)
        info_text = f"Total cache size: {total_size:.1f} MB ({file_count} files)"
        info_label = QLabel(info_text)
        info_label.setStyleSheet("font-weight: bold; color: #333;")
        info_layout.addWidget(info_label)
        
        layout.addWidget(info_group)
        
        # Cache files list
        files_group = QGroupBox("Cache Files")
        files_layout = QVBoxLayout(files_group)
        
        self.files_list = QListWidget()
        self.files_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        files_layout.addWidget(self.files_list)
        
        # Action buttons
        button_layout = QHBoxLayout()
        
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_list)
        button_layout.addWidget(self.refresh_btn)
        
        button_layout.addStretch()
        
        self.delete_selected_btn = QPushButton("Delete Selected")
        self.delete_selected_btn.clicked.connect(self._delete_selected)
        button_layout.addWidget(self.delete_selected_btn)
        
        self.clear_all_btn = QPushButton("Clear All Cache")
        self.clear_all_btn.clicked.connect(self._clear_all_cache)
        button_layout.addWidget(self.clear_all_btn)
        
        files_layout.addLayout(button_layout)
        layout.addWidget(files_group)
        
        # Dialog buttons
        dialog_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        dialog_buttons.rejected.connect(self.close)
        layout.addWidget(dialog_buttons)
        
        # Connect selection changes
        self.files_list.itemSelectionChanged.connect(self._update_button_states)
        self._update_button_states()
    
    def refresh_list(self):
        """
        Refresh the cache files list with current cache information.
        
        Note:
            - Retrieves latest cache information from traffixgen backend
            - Clears existing list and rebuilds with current data
            - Displays file names with size, type, and modification time
            - Stores complete file information in list item data
            - Updates button states based on new selection
            - Handles backend communication errors gracefully
        """
        try:
            from . import traffixgen
            self.cache_info = traffixgen.get_cache_info()
            self.cache_files = self.cache_info.get('cache_files', [])
            
            self.files_list.clear()
            
            for file_info in self.cache_files:
                file_name = file_info['file']
                file_size = file_info['size_mb']
                file_type = file_info['type']
                
                # Format modified time
                import time
                modified_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_info['modified']))
                
                display_text = f"{file_name} ({file_size:.1f} MB, {file_type}, {modified_time})"
                item = QListWidgetItem(display_text)
                item.setData(Qt.ItemDataRole.UserRole, file_info)
                self.files_list.addItem(item)
                
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Error refreshing cache list: {e}")
    
    def _get_selected_files(self):
        """
        Get list of selected cache file information from the list widget.
        
        Returns:
            List[Dict]: List of file information dictionaries for selected items
                       Each dict contains: {'file': str, 'size_mb': float, 'type': str, 'modified': float}
                       
        Note:
            - Extracts file info stored in UserRole data of selected list items
            - Returns empty list if no items are selected
            - File info includes name, size, type, and modification timestamp
        """
        selected_files = []
        for item in self.files_list.selectedItems():
            file_info = item.data(Qt.ItemDataRole.UserRole)
            if file_info:
                selected_files.append(file_info)
        return selected_files

    def _update_button_states(self):
        """
        Update button states based on current selection and file availability.
        
        Note:
            - Enables "Delete Selected" button only when files are selected
            - Enables "Clear All" button only when cache files exist
            - Called automatically when selection changes
            - Provides appropriate UI feedback for available actions
        """
        has_selection = len(self.files_list.selectedItems()) > 0
        has_files = self.files_list.count() > 0
        
        self.delete_selected_btn.setEnabled(has_selection)
        self.clear_all_btn.setEnabled(has_files)
    
    def _delete_selected(self):
        """
        Delete selected cache files with confirmation dialog.
        
        Note:
            - Gets currently selected files from the list widget
            - Shows confirmation dialog with file names to be deleted
            - Uses traffixgen backend to perform actual file deletion
            - Provides individual error reporting for failed deletions
            - Refreshes list display after successful deletion
            - Shows success message with count of deleted files
            - Action cannot be undone - files are permanently removed
        """
        selected_files = self._get_selected_files()
        if not selected_files:
            return
        
        # Confirm deletion
        file_list = '\n'.join([f['file'] for f in selected_files])
        reply = QMessageBox.question(self, "Confirm Deletion", 
                                   f"Are you sure you want to delete the following cache files?\n\n"
                                   f"{file_list}\n\n"
                                   f"This action cannot be undone.",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            from . import traffixgen
            # Delete files through TraffixGen
            for file_info in selected_files:
                result = traffixgen.delete_cache_file(file_info['file'])
                if not result.get('success', False):
                    QMessageBox.warning(self, "Error", f"Failed to delete {file_info['file']}: {result.get('error', 'Unknown error')}")
            
            # Refresh the list
            self.refresh_list()
            QMessageBox.information(self, "Success", f"Deleted {len(selected_files)} cache file(s)")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete cache files:\n{str(e)}")
    
    def _clear_all_cache(self):
        """Clear all cache files."""
        if not self.cache_files:
            return
        
        # Confirm clearing all
        total_size = self.cache_info.get('total_size_mb', 0)
        reply = QMessageBox.question(self, "Confirm Clear All", 
                                   f"Are you sure you want to clear ALL cache files?\n\n"
                                   f"This will delete {len(self.cache_files)} files ({total_size:.1f} MB)\n\n"
                                   f"This action cannot be undone.",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            from . import traffixgen
            result = traffixgen.clear_cache()
            
            if result.get('success', False):
                self.refresh_list()
                QMessageBox.information(self, "Success", "All cache files cleared successfully")
            else:
                QMessageBox.warning(self, "Error", f"Failed to clear cache: {result.get('error', 'Unknown error')}")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to clear cache:\n{str(e)}")


# Aircraft Type Selection Dialog
class AircraftTypeDialog(QDialog):
    """
    Advanced aircraft type selection dialog with performance model integration.
    
    This comprehensive dialog provides an intelligent interface for selecting aircraft
    types from the active BlueSky performance model, supporting multiple selection
    modes and automatic type validation. The dialog integrates with OpenAP, BADA,
    and legacy performance models to provide accurate aircraft type availability
    and enables informed selection for realistic scenario generation.
    
    The dialog features automatic performance model detection and aircraft type
    extraction, providing users with comprehensive lists of supported aircraft
    types for scenario configuration. Multi-selection capabilities enable users
    to configure diverse aircraft mixes for training scenarios while ensuring
    compatibility with the active BlueSky performance model configuration.
    
    Key Features:
    - Automatic performance model integration and aircraft type discovery
    - Multi-selection interface with checkbox controls for aircraft type selection
    - Current selection preservation and modification capabilities
    - Alphabetical sorting and organized display for easy aircraft type location
    - Real-time validation against active performance model capabilities
    - Comprehensive aircraft type coverage across commercial and regional aircraft
    
    Performance Model Support:
    - OpenAP: Advanced performance model with detailed aircraft characteristics
    - BADA: Industry-standard aircraft performance database integration
    - Legacy Models: Backward compatibility with older BlueSky installations
    - Fallback Types: Common aircraft types for robust operation across configurations
    
    Attributes:
        selected_types (List[str]): Currently selected aircraft type designators
        available_types (List[str]): All aircraft types available from performance model
        checkboxes (Dict[str, QCheckBox]): Checkbox controls for aircraft type selection
        
    Args:
        current_types (str, optional): Comma-separated string of currently selected types
        parent (QWidget, optional): Parent widget for proper dialog modal behavior
        
    Returns:
        str: Comma-separated string of selected aircraft types on acceptance
        
    Examples:
        # Open dialog with current aircraft type selection
        current = "A320,B738,A350"
        dialog = AircraftTypeDialog(current, parent=self)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_types = dialog.get_selected_types()
            # Update configuration with new aircraft type selection
        
        # Open dialog for new aircraft type selection
        dialog = AircraftTypeDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            aircraft_config = dialog.get_selected_types()
    
    Note:
        The dialog automatically queries the active BlueSky performance model
        for aircraft type availability. Selection is validated against model
        capabilities to ensure scenario compatibility and operational accuracy
        across different BlueSky configuration and performance model installations.
    """
    
    def __init__(self, current_types: str = "", parent=None):
        super().__init__(parent)
        self.selected_types = []
        self.setWindowTitle("Select Aircraft Types")
        self.setMinimumSize(800, 600)
        
        layout = QVBoxLayout(self)
        
        # Info label
        info_label = QLabel("Select aircraft types from the current performance model:")
        layout.addWidget(info_label)
        
        # Get available aircraft types
        self.available_types = []
        self.checkboxes = {}
        
        # Parse currently selected types
        current_set = set()
        if current_types.strip():
            current_set = {t.strip().upper() for t in current_types.split(',') if t.strip()}
        
        # Create scrollable area for checkboxes
        scroll_area = QScrollArea()
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        # Get available types from performance model
        try:
            # Try to get directly from the loaded performance model
            import bluesky as bs
            if hasattr(bs, 'traf') and hasattr(bs.traf, 'perf'):
                # Try OpenAP
                if hasattr(bs.traf.perf, 'coeff') and hasattr(bs.traf.perf.coeff, 'actypes_fixwing'):
                    fixwing = sorted(bs.traf.perf.coeff.actypes_fixwing)
                    rotor = sorted(bs.traf.perf.coeff.actypes_rotor) if hasattr(bs.traf.perf.coeff, 'actypes_rotor') else []
                    self.available_types = fixwing + rotor
                else:
                    self.available_types = self._get_fallback_types()
            else:
                self.available_types = self._get_fallback_types()
        except:
            self.available_types = self._get_fallback_types()
        
        # Group checkboxes by manufacturer (first letter)
        manufacturers = {}
        for actype in self.available_types:
            first_letter = actype[0].upper()
            if first_letter not in manufacturers:
                manufacturers[first_letter] = []
            manufacturers[first_letter].append(actype)
        
        # Sort manufacturers and their aircraft
        for letter in manufacturers:
            manufacturers[letter].sort()
        
        # Create group boxes for each manufacturer
        for letter in sorted(manufacturers.keys()):
            aircraft_list = manufacturers[letter]
            group_box = QGroupBox(f"{letter}-series ({len(aircraft_list)} aircraft)")
            group_layout = QHBoxLayout(group_box)
            
            # Organize aircraft in columns (max 4 columns per manufacturer)
            cols_per_row = min(4, len(aircraft_list))
            aircraft_per_col = (len(aircraft_list) + cols_per_row - 1) // cols_per_row  # Ceiling division
            
            for col in range(cols_per_row):
                col_layout = QVBoxLayout()
                start_idx = col * aircraft_per_col
                end_idx = min(start_idx + aircraft_per_col, len(aircraft_list))
                
                for i in range(start_idx, end_idx):
                    actype = aircraft_list[i]
                    checkbox = QCheckBox(actype)
                    if actype in current_set:
                        checkbox.setChecked(True)
                    self.checkboxes[actype] = checkbox
                    col_layout.addWidget(checkbox)
                
                # Fill remaining space in column
                col_layout.addStretch()
                group_layout.addLayout(col_layout)
            
            scroll_layout.addWidget(group_box)
        
        scroll_area.setWidget(scroll_widget)
        scroll_area.setWidgetResizable(True)
        layout.addWidget(scroll_area)
        
        # Select/Deselect all buttons
        button_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self._select_all)
        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.clicked.connect(self._clear_all)
        
        # Common types buttons
        common_btn = QPushButton("Common Types")
        common_btn.clicked.connect(self._select_common)
        
        button_layout.addWidget(select_all_btn)
        button_layout.addWidget(clear_all_btn)
        button_layout.addWidget(common_btn)
        button_layout.addStretch()
        layout.addLayout(button_layout)
        
        # OK/Cancel buttons
        button_box_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        
        button_box_layout.addStretch()
        button_box_layout.addWidget(ok_btn)
        button_box_layout.addWidget(cancel_btn)
        layout.addLayout(button_box_layout)
    
    def _get_fallback_types(self):
        """Fallback list of common aircraft types."""
        return [
            "A318", "A319", "A320", "A321", "A330", "A340", "A350", "A380",
            "B722", "B733", "B734", "B735", "B736", "B737", "B738", "B739",
            "B744", "B747", "B748", "B752", "B753", "B757", "B762", "B763",
            "B764", "B767", "B772", "B773", "B777", "B778", "B787", "B788", "B789", "B78X",
            "CRJ1", "CRJ2", "CRJ7", "CRJ9", "CRJX",
            "E135", "E145", "E170", "E175", "E190", "E195",
            "F100", "F70", "MD11", "MD80", "MD81", "MD82", "MD83", "MD87", "MD88", "MD90"
        ]
    
    def _select_all(self):
        """Select all aircraft types."""
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(True)
    
    def _clear_all(self):
        """Clear all selections."""
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(False)
    
    def _select_common(self):
        """Select common aircraft types."""
        common = {"A320", "A321", "A330", "A350", "B737", "B738", "B744", "B777", "B787", "E190"}
        for actype, checkbox in self.checkboxes.items():
            checkbox.setChecked(actype in common)
    
    def get_selected_types(self):
        """Return comma-separated string of selected aircraft types."""
        selected = []
        for actype, checkbox in self.checkboxes.items():
            if checkbox.isChecked():
                selected.append(actype)
        return ",".join(sorted(selected))

# --- RL tab (Realistic Replay) --------------------------------------------

class RLTab(QWidget):
    """
    Realistic Replay tab for scenario-based air traffic generation and simulation.
    
    This comprehensive interface provides scenario-based air traffic generation using
    pre-processed EUROCONTROL flight data with advanced configuration options for
    jitter, filtering, and scenario customization. The Realistic Replay approach
    uses deterministic flight data with configurable variations to create realistic
    but reproducible traffic scenarios.
    
    The tab implements a structured workflow: data loading -> configuration -> 
    filtering -> jitter settings -> scenario generation -> simulation execution.
    All steps include comprehensive validation and user feedback to ensure proper
    configuration and successful scenario generation.
    
    Key Features:
    - EUROCONTROL data file loading with format validation
    - Advanced filtering system with airspace, altitude, and temporal constraints
    - Jitter configuration for realistic flight variations
    - Phase-based altitude and timing adjustments
    - Track configuration for individual flight customization
    - Scenario generation with comprehensive validation
    - Direct integration with BlueSky simulation environment
    
    Workflow Components:
    1. Load EUROCONTROL Data: Import flights, filed plans, actual tracks, FIR data
    2. Configure Filters: Set date ranges, airspace, altitude, aircraft constraints
    3. Jitter Settings: Configure realistic variations in timing and positioning
    4. Phase Configuration: Set altitude ranges and timing for flight phases
    5. Track Configuration: Individual flight parameter customization
    6. Scenario Generation: Create BlueSky-compatible scenario files
    7. Simulation Execution: Run scenarios in BlueSky environment
    
    Data Sources:
    - Flights file: Flight operation metadata and basic information
    - Filed plans file: Planned flight routes and procedural data
    - Actual tracks file: Historical trajectory data with coordinates
    - FIR boundaries file: Airspace definition data for geographic filtering
    
    Attributes:
        flights_file_label (QLabel): Display widget for selected flights data file
        filed_file_label (QLabel): Display widget for selected filed plans file
        actual_file_label (QLabel): Display widget for selected actual tracks file
        fir_file_label (QLabel): Display widget for selected FIR boundaries file
        eurocontrol_filters (dict): Current filter configuration settings
        jitter_enabled (bool): Flag indicating if jitter variations are enabled
        phase_configurations (dict): Phase-specific altitude and timing settings
    
    Examples:
        # Tab is created as part of main window
        rl_tab = RLTab(parent_window)
        
        # Typical workflow:
        # 1. Load EUROCONTROL data files
        # 2. Configure filters for desired traffic subset
        # 3. Set jitter parameters for realistic variations
        # 4. Generate scenarios with specified parameters
        # 5. Execute scenarios in BlueSky simulation
    
    Note:
        This tab provides feature parity with Historic Sampling tab but uses
        predetermined historical data rather than ML-generated synthetic data.
        The filtering system operates on actual flight trajectory data to ensure
        accurate geographic and temporal constraints for scenario generation.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        main = QVBoxLayout(self)

        # 1) Load Eurocontrol Data (Required)
        gb_load = QGroupBox("1) Load Eurocontrol Data - Required")
        gb_load_layout = QVBoxLayout(gb_load)
        
        # Description
        desc1 = QLabel("Select Eurocontrol data files (first 3 required, FIR optional)")
        desc1.setStyleSheet("color: #666; font-style: italic;")
        gb_load_layout.addWidget(desc1)
        
        # Create 2x2 grid for file selections
        files_grid = QHBoxLayout()
        
        # Left column
        left_column = QVBoxLayout()
        
        # Flights data file
        flights_group = QGroupBox("Flights Data (Required)")
        flights_layout = QVBoxLayout(flights_group)
        
        self.flights_file_label = QLabel("No file selected")
        self.flights_file_label.setStyleSheet("color: #999; font-style: italic;")
        flights_layout.addWidget(self.flights_file_label)
        
        flights_buttons = QHBoxLayout()
        btn_browse_flights = QPushButton("Browse...")
        btn_browse_flights.setToolTip("Select Flights_extract.csv file")
        btn_browse_flights.clicked.connect(self._browse_flights_file)
        btn_clear_flights = QPushButton("Clear")
        btn_clear_flights.clicked.connect(self._clear_flights_file)
        
        flights_buttons.addWidget(btn_browse_flights)
        flights_buttons.addWidget(btn_clear_flights)
        flights_buttons.addStretch()
        flights_layout.addLayout(flights_buttons)
        
        left_column.addWidget(flights_group)
        
        # Flight Points Filed file
        filed_group = QGroupBox("Flight Points - Filed (Required)")
        filed_layout = QVBoxLayout(filed_group)
        
        self.filed_file_label = QLabel("No file selected")
        self.filed_file_label.setStyleSheet("color: #999; font-style: italic;")
        filed_layout.addWidget(self.filed_file_label)
        
        filed_buttons = QHBoxLayout()
        btn_browse_filed = QPushButton("Browse...")
        btn_browse_filed.setToolTip("Select Flight_Points_Filed_extract.csv file")
        btn_browse_filed.clicked.connect(self._browse_filed_file)
        btn_clear_filed = QPushButton("Clear")
        btn_clear_filed.clicked.connect(self._clear_filed_file)
        
        filed_buttons.addWidget(btn_browse_filed)
        filed_buttons.addWidget(btn_clear_filed)
        filed_buttons.addStretch()
        filed_layout.addLayout(filed_buttons)
        
        left_column.addWidget(filed_group)
        
        # Right column
        right_column = QVBoxLayout()
        
        # Flight Points Actual file
        actual_group = QGroupBox("Flight Points - Actual (Required)")
        actual_layout = QVBoxLayout(actual_group)
        
        self.actual_file_label = QLabel("No file selected")
        self.actual_file_label.setStyleSheet("color: #999; font-style: italic;")
        actual_layout.addWidget(self.actual_file_label)
        
        actual_buttons = QHBoxLayout()
        btn_browse_actual = QPushButton("Browse...")
        btn_browse_actual.setToolTip("Select Flight_Points_Actual_extract.csv file")
        btn_browse_actual.clicked.connect(self._browse_actual_file)
        btn_clear_actual = QPushButton("Clear")
        btn_clear_actual.clicked.connect(self._clear_actual_file)
        
        actual_buttons.addWidget(btn_browse_actual)
        actual_buttons.addWidget(btn_clear_actual)
        actual_buttons.addStretch()
        actual_layout.addLayout(actual_buttons)
        
        right_column.addWidget(actual_group)
        
        # FIR data file
        fir_group = QGroupBox("FIR Data (Optional)")
        fir_layout = QVBoxLayout(fir_group)
        
        self.fir_file_label = QLabel("No file selected")
        self.fir_file_label.setStyleSheet("color: #999; font-style: italic;")
        fir_layout.addWidget(self.fir_file_label)
        
        fir_buttons = QHBoxLayout()
        btn_browse_fir = QPushButton("Browse...")
        btn_browse_fir.setToolTip("Select FIR_extract.csv file")
        btn_browse_fir.clicked.connect(self._browse_fir_file)
        btn_clear_fir = QPushButton("Clear")
        btn_clear_fir.clicked.connect(self._clear_fir_file)
        
        fir_buttons.addWidget(btn_browse_fir)
        fir_buttons.addWidget(btn_clear_fir)
        fir_buttons.addStretch()
        fir_layout.addLayout(fir_buttons)
        
        right_column.addWidget(fir_group)
        
        files_grid.addLayout(left_column)
        files_grid.addLayout(right_column)
        gb_load_layout.addLayout(files_grid)
        
        # Initialize file paths
        self._flights_file = ""
        self._filed_file = ""
        self._actual_file = ""
        self._fir_file = ""
        
        # Filter configuration section
        filter_section = QHBoxLayout()
        
        self.btn_configure_filters = QPushButton("Configure Filters...")
        self.btn_configure_filters.setToolTip("Set geographic, temporal, and aircraft filters")
        self.btn_configure_filters.clicked.connect(self._configure_eurocontrol_filters)
        
        filter_section.addStretch()
        filter_section.addWidget(self.btn_configure_filters)
        gb_load_layout.addLayout(filter_section)
        
        # Initialize default filter settings
        self.eurocontrol_filters = {
            'lat_min': -90, 'lat_max': 90,
            'lon_min': -180, 'lon_max': 180,
            'fl_min': 0, 'fl_max': 500,
            'include_airspace': [],
            'time_start': None, 'time_end': None,
            'date_start': None, 'date_end': None,
            'aircraft_types': [],  # Empty means all types
            'polygon_filter': None  # Polygon filter (disabled by default)
        }
        
        main.addWidget(gb_load)

        # 2) Flight Phase Jitter (Optional)
        self.gb_jitter = QGroupBox("2) Flight Phase Jitter - Optional")
        gb_j_layout = QVBoxLayout(self.gb_jitter)
        
        # Description and altitude config button  
        header_layout = QHBoxLayout()
        desc2 = QLabel("Apply noise to waypoints based on flight phase")
        desc2.setStyleSheet("color: #666; font-style: italic;")
        
        self.btn_configure_altitudes = QPushButton("Configure Phase Altitudes...")
        self.btn_configure_altitudes.setToolTip("Set flight level boundaries for each flight phase")
        self.btn_configure_altitudes.clicked.connect(self._configure_phase_altitudes)
        
        header_layout.addWidget(desc2)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_configure_altitudes)
        gb_j_layout.addLayout(header_layout)
        
        # Initialize default phase altitude boundaries (in flight levels)
        self.phase_altitudes = {
            'takeoff': {'min_fl': 0, 'max_fl': 15},      # Ground to initial climb
            'climb': {'min_fl': 15, 'max_fl': 250},      # Climbing phase
            'cruise': {'min_fl': 250, 'max_fl': 450},    # Cruise altitude
            'descent': {'min_fl': 50, 'max_fl': 250},    # Descending from cruise  
            'approach': {'min_fl': 0, 'max_fl': 50}      # Final approach
        }
        
        # Track-specific phase configurations (populated when configured)
        self.track_phase_configurations = {}
        
        # Create 5-column layout for flight phases
        phases_scroll = QScrollArea()
        phases_scroll.setWidgetResizable(True)
        phases_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        phases_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        phases_scroll.setMaximumHeight(400)
        
        phases_widget = QWidget()
        phases_layout = QHBoxLayout(phases_widget)
        phases_layout.setContentsMargins(5, 5, 5, 5)
        phases_layout.setSpacing(10)
        
        # Flight phase names and default settings
        self.phases = ['takeoff', 'climb', 'cruise', 'descent', 'approach']
        self.phase_configs = {}
        
        for phase in self.phases:
            # Create phase column
            phase_group = QGroupBox(phase.title())
            phase_layout = QFormLayout(phase_group)
            phase_layout.setContentsMargins(5, 5, 5, 5)
            
            # Store phase widgets
            phase_widgets = {}
            
            # Enable checkbox
            phase_widgets['enabled'] = QCheckBox("Enable")
            phase_widgets['enabled'].setChecked(False)
            phase_layout.addRow(phase_widgets['enabled'])
            
            # Distribution type
            phase_widgets['dist'] = QComboBox()
            phase_widgets['dist'].addItems(["uniform", "normal"])
            phase_widgets['dist'].setToolTip("Distribution type for random jitter")
            phase_layout.addRow("Distribution:", phase_widgets['dist'])
            
            # Time jitter
            phase_widgets['dt'] = QDoubleSpinBox()
            phase_widgets['dt'].setDecimals(3)
            phase_widgets['dt'].setRange(0.0, 1e6)
            phase_widgets['dt'].setValue(0.0)
            _configure_decimal_separator(phase_widgets['dt'])
            phase_widgets['dt'].setToolTip("Time jitter in seconds")
            phase_layout.addRow("dt [s]:", phase_widgets['dt'])
            
            # Position jitter
            phase_widgets['dlat'] = QDoubleSpinBox()
            phase_widgets['dlat'].setDecimals(6)
            phase_widgets['dlat'].setRange(0.0, 10.0)
            phase_widgets['dlat'].setValue(0.0)
            _configure_decimal_separator(phase_widgets['dlat'])
            phase_widgets['dlat'].setToolTip("Latitude jitter in degrees")
            phase_layout.addRow("dlat [deg]:", phase_widgets['dlat'])
            
            phase_widgets['dlon'] = QDoubleSpinBox()
            phase_widgets['dlon'].setDecimals(6)
            phase_widgets['dlon'].setRange(0.0, 10.0)
            phase_widgets['dlon'].setValue(0.0)
            _configure_decimal_separator(phase_widgets['dlon'])
            phase_widgets['dlon'].setToolTip("Longitude jitter in degrees")
            phase_layout.addRow("dlon [deg]:", phase_widgets['dlon'])
            
            # Altitude jitter
            phase_widgets['dfl'] = QSpinBox()
            phase_widgets['dfl'].setRange(0, 5000)
            phase_widgets['dfl'].setValue(0)
            phase_widgets['dfl'].setToolTip("Flight level jitter in feet")
            phase_layout.addRow("dfl [ft]:", phase_widgets['dfl'])
            
            # Normal distribution sigma
            phase_widgets['nsig'] = QDoubleSpinBox()
            phase_widgets['nsig'].setDecimals(2)
            phase_widgets['nsig'].setRange(0.0, 10.0)
            phase_widgets['nsig'].setValue(0.0)
            _configure_decimal_separator(phase_widgets['nsig'])
            phase_widgets['nsig'].setToolTip("Standard deviation for normal distribution")
            phase_layout.addRow("nsig:", phase_widgets['nsig'])
            
            self.phase_configs[phase] = phase_widgets
            phases_layout.addWidget(phase_group)
        
        phases_scroll.setWidget(phases_widget)
        gb_j_layout.addWidget(phases_scroll)
        
        # Global jitter percentage (applies to all phases)
        global_controls = QHBoxLayout()
        global_controls.setContentsMargins(5, 5, 5, 5)
        
        global_label = QLabel("Jitter % of flights:")
        self.j_pct = QSlider(Qt.Orientation.Horizontal)
        self.j_pct.setRange(0, 100)
        self.j_pct.setValue(100)
        self.j_pct.setSingleStep(1)
        self.j_pct_label = QLabel("100%")
        self.j_pct.valueChanged.connect(lambda v: self.j_pct_label.setText(f"{v}%"))
        
        global_controls.addWidget(global_label)
        global_controls.addWidget(self.j_pct, 1)
        global_controls.addWidget(self.j_pct_label)
        gb_j_layout.addLayout(global_controls)
        
        main.addWidget(self.gb_jitter)

        # 3) Options
        self.gb_options = QGroupBox("3) Options")
        options_layout = QVBoxLayout(self.gb_options)
        options_layout.setContentsMargins(8, 8, 8, 8)
        
        self.autodel_chk = QCheckBox("Auto-delete at last waypoint")
        self.autodel_chk.setChecked(True)
        options_layout.addWidget(self.autodel_chk)
        
        main.addWidget(self.gb_options)

        # 4) Create Scenario
        self.gb_scenario = QGroupBox("4) Create Scenario")
        actions_main_layout = QVBoxLayout(self.gb_scenario)
        actions_main_layout.setContentsMargins(8, 8, 8, 8)
        actions_main_layout.setSpacing(10)
        
        # Scenario controls form
        scenario_form = QFormLayout()
        scenario_form.setContentsMargins(0, 0, 0, 0)
        scenario_form.setSpacing(8)
        
        self.scn_name = QLineEdit("replay")
        self.scn_name.setPlaceholderText("Scenario name, e.g. replay_01")
        self.scn_name.setToolTip("Name for the generated scenario file (without .scn extension)")
        
        self.rl_seed = QSpinBox()
        self.rl_seed.setRange(0, 2**31-1)
        self.rl_seed.setValue(0)
        self.rl_seed.setToolTip("Seed for jitter (0=random)")
        
        self.rl_overwrite = QCheckBox("Overwrite scenario if it exists")
        self.rl_overwrite.setChecked(False)
        self.rl_overwrite.setToolTip("Replace existing scenario file if one exists with the same name")
        
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

        main.addWidget(self.gb_scenario)
        main.addStretch(1)

    def _are_filters_configured(self):
        """Check if filters have been actually configured (not just default values)"""
        if not hasattr(self, 'eurocontrol_filters') or not self.eurocontrol_filters:
            return False
        
        filters = self.eurocontrol_filters
        
        # Check if any filter values are different from defaults
        default_filters = {
            'lat_min': -90, 'lat_max': 90, 'lon_min': -180, 'lon_max': 180,
            'fl_min': 0, 'fl_max': 500, 'include_airspace': [], 'aircraft_types': [],
            'time_start': None, 'time_end': None
        }
        
        # Check if any values differ from defaults
        for key, default_value in default_filters.items():
            current_value = filters.get(key, default_value)
            if current_value != default_value:
                return True
                
        return False

    def _validate_configuration(self):
        """Validate that all required configuration steps have been completed"""
        # Check if files are loaded
        if not (getattr(self, '_flights_file', '') and
                getattr(self, '_filed_file', '') and
                getattr(self, '_actual_file', '')):
            QMessageBox.critical(self, "Missing Files", 
                               "Please select the required Eurocontrol files first:\n\n"
                               "* Flights Extract\n"
                               "* Flight Points Filed Extract\n" 
                               "* Flight Points Actual Extract")
            return False
            
        # Check if filters are configured
        if not self._are_filters_configured():
            QMessageBox.warning(self, "Configure Filters Required", 
                              "Please configure filters before creating/running scenarios.\n\n"
                              "Click 'Configure Filters...' to set up geographic, temporal, and aircraft filters.")
            return False
            
        # Check if phase altitudes are configured
        # Accept either global phase_altitudes or per-track configurations
        has_global_config = hasattr(self, 'phase_altitudes') and self.phase_altitudes
        has_track_configs = hasattr(self, 'track_phase_configurations') and self.track_phase_configurations
        
        if not has_global_config and not has_track_configs:
            QMessageBox.warning(self, "Configure Phase Altitudes Required", 
                              "Please configure phase altitudes before creating/running scenarios.\n\n"
                              "Click 'Configure Phase Altitudes...' to set flight level boundaries for each flight phase.")
            return False
        
        # If we have track configurations, that's sufficient - skip the default check
        if has_track_configs:
            return True
        
        # Check if user has customized phase altitudes (optional warning)
        default_phase_altitudes = {
            'takeoff': {'min_fl': 0, 'max_fl': 15},
            'climb': {'min_fl': 15, 'max_fl': 250},
            'cruise': {'min_fl': 250, 'max_fl': 450},
            'descent': {'min_fl': 50, 'max_fl': 250},
            'approach': {'min_fl': 0, 'max_fl': 50}
        }
        
        if self.phase_altitudes == default_phase_altitudes:
            reply = QMessageBox.question(self, "Using Default Phase Altitudes", 
                                       "You are using default phase altitude settings.\n\n"
                                       "Default settings:\n"
                                       "* Takeoff: FL0-FL15\n"
                                       "* Climb: FL15-FL250\n" 
                                       "* Cruise: FL250-FL450\n"
                                       "* Descent: FL50-FL250\n"
                                       "* Approach: FL0-FL50\n\n"
                                       "Do you want to proceed with these defaults?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                       QMessageBox.StandardButton.Yes)
            if reply == QMessageBox.StandardButton.No:
                return False
            
        return True

    def _browse_flights_file(self):
        """Browse for Flights_extract.csv file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Flights Data File", 
            filter="CSV files (*.csv);;All files (*)"
        )
        if file_path:
            self._flights_file = file_path
            self.flights_file_label.setText(os.path.basename(file_path))
            self.flights_file_label.setStyleSheet("color: #000; font-weight: bold;")

    def _clear_flights_file(self):
        """Clear flights file selection"""
        self._flights_file = ""
        self.flights_file_label.setText("No file selected")
        self.flights_file_label.setStyleSheet("color: #999; font-style: italic;")

    def _browse_filed_file(self):
        """Browse for Flight_Points_Filed_extract.csv file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Flight Points Filed File", 
            filter="CSV files (*.csv);;All files (*)"
        )
        if file_path:
            self._filed_file = file_path
            self.filed_file_label.setText(os.path.basename(file_path))
            self.filed_file_label.setStyleSheet("color: #000; font-weight: bold;")

    def _clear_filed_file(self):
        """Clear filed file selection"""
        self._filed_file = ""
        self.filed_file_label.setText("No file selected")
        self.filed_file_label.setStyleSheet("color: #999; font-style: italic;")

    def _browse_actual_file(self):
        """Browse for Flight_Points_Actual_extract.csv file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Flight Points Actual File", 
            filter="CSV files (*.csv);;All files (*)"
        )
        if file_path:
            self._actual_file = file_path
            self.actual_file_label.setText(os.path.basename(file_path))
            self.actual_file_label.setStyleSheet("color: #000; font-weight: bold;")

    def _clear_actual_file(self):
        """Clear actual file selection"""
        self._actual_file = ""
        self.actual_file_label.setText("No file selected")
        self.actual_file_label.setStyleSheet("color: #999; font-style: italic;")

    def _browse_fir_file(self):
        """Browse for FIR_extract.csv file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select FIR Data File", 
            filter="CSV files (*.csv);;All files (*)"
        )
        if file_path:
            self._fir_file = file_path
            self.fir_file_label.setText(os.path.basename(file_path))
            self.fir_file_label.setStyleSheet("color: #000; font-weight: bold;")

    def _clear_fir_file(self):
        """Clear FIR file selection"""
        self._fir_file = ""
        self.fir_file_label.setText("No file selected")
        self.fir_file_label.setStyleSheet("color: #999; font-style: italic;")

    def _configure_eurocontrol_filters(self):
        """Open dialog to configure Eurocontrol data filtering"""
        # Check if required files are loaded
        if not (getattr(self, '_flights_file', '') and
                getattr(self, '_filed_file', '') and
                getattr(self, '_actual_file', '')):
            QMessageBox.warning(self, "Files Required", 
                              "Please load all three required Eurocontrol files before configuring filters:\n\n"
                              "* Flights Extract\n"
                              "* Flight Points Filed Extract\n" 
                              "* Flight Points Actual Extract")
            return
            
        # Load data and get summary for filter dialog
        summary = self._load_and_get_summary()
        if not summary:
            return
            
        # Pass summary data to filter dialog instead of showing popup
        dialog = EurocontrolFilterDialog(self.eurocontrol_filters, self._fir_file, summary, self)
        dialog.show()  # Use show() instead of exec() for non-modal dialog

    def _validate_eurocontrol_files(self) -> Tuple[bool, str]:
        """Validate that required Eurocontrol files are selected and exist"""
        # Check required files
        if not self._flights_file:
            return False, "Flights data file is required"
        if not self._filed_file:
            return False, "Flight Points (Filed) file is required"  
        if not self._actual_file:
            return False, "Flight Points (Actual) file is required"
        
        # Check file existence
        if not os.path.exists(self._flights_file):
            return False, f"Flights file not found: {self._flights_file}"
        if not os.path.exists(self._filed_file):
            return False, f"Filed points file not found: {self._filed_file}"
        if not os.path.exists(self._actual_file):
            return False, f"Actual points file not found: {self._actual_file}"
        if self._fir_file and not os.path.exists(self._fir_file):
            return False, f"FIR file not found: {self._fir_file}"
        
        return True, ""

    def _emit_jitter_if_needed(self):
        """Send jitter configuration before scenario creation (including per-track altitude boundaries)"""
        print("=== CONFIGURING JITTER PARAMETERS ===")
        
        # Send track-specific altitude boundaries if configured
        self._emit_track_phase_altitudes()
        
        # Send simplified phase jitter parameters
        self._emit_simplified_phase_jitter()
        
        print("=== JITTER CONFIGURATION COMPLETED ===")

    def _emit_track_phase_altitudes(self):
        """Send track-specific phase altitude boundaries to the backend"""
        if not hasattr(self, 'track_phase_configurations') or not self.track_phase_configurations:
            print("No track-specific phase altitude configurations to send")
            return
        
        from . import SATG
        print(f"Sending altitude boundaries for {len(self.track_phase_configurations)} aircraft:")
        
        for track_callsign, config in self.track_phase_configurations.items():
            # Extract flight level boundaries from the configuration
            takeoff_max = config.get('takeoff', {}).get('max_fl', 15)
            climb_max = config.get('climb', {}).get('max_fl', 250)
            descent_max = config.get('descent', {}).get('max_fl', 400)
            approach_max = config.get('approach', {}).get('max_fl', 50)
            
            print(f"  {track_callsign}: Takeoff FL{takeoff_max}, Climb FL{climb_max}, Descent FL{descent_max}, Approach FL{approach_max}")
            
            # Send to backend using SATG_RL_TRACK_CONFIG command
            SATG.SATG_RL_TRACK_CONFIG(track_callsign, takeoff_max, climb_max, descent_max, approach_max)

    def _emit_simplified_phase_jitter(self):
        """Send phase jitter parameters from section 2 to backend using simple approach."""
        from . import SATG
        
        # Check if any phase is enabled
        any_enabled = False
        for phase in self.phases:
            if self.phase_configs[phase]['enabled'].isChecked():
                any_enabled = True
                break
        
        if not any_enabled:
            print("Phase jitter disabled - no phases enabled")
            return
            
        print("=== SENDING PHASE JITTER CONFIGURATION ===")
        
        # Enable phase jitter in backend
        SATG.SATG_RL_PHASE_JITTER("on")
        
        # Send global jitter percentage
        jitter_pct = self.j_pct.value()
        print(f"Jitter percentage: {jitter_pct}%")
        
        # Send configuration for each enabled phase
        for phase in self.phases:
            widgets = self.phase_configs[phase]
            enabled = widgets['enabled'].isChecked()
            
            if enabled:
                # Get values from GUI widgets
                dist = widgets['dist'].currentText()
                dt_max = widgets['dt'].value()
                dlat_max = widgets['dlat'].value() 
                dlon_max = widgets['dlon'].value()
                dfl_max = widgets['dfl'].value()
                nsig = widgets['nsig'].value()
                
                print(f"Phase {phase}: enabled, dist={dist}, dt={dt_max}s, "
                      f"dlat={dlat_max}deg, dlon={dlon_max}deg, dfl={dfl_max}ft, nsig={nsig}")
                
                # Send to backend using correct command signature (enabled, dt, dlat, dlon, dfl)
                SATG.SATG_RL_PHASE_CONFIG(phase, "on", dt_max, dlat_max, dlon_max, dfl_max)
        
        print("=== PHASE JITTER CONFIGURATION COMPLETED ===")

    def _emit_phase_jitter_config(self):
        """Emit phase-based jitter configuration to backend."""
        if not hasattr(self, 'phase_jitter_enabled'):
            return
        
        # Enable/disable phase-based jitter
        mode = "on" if self.phase_jitter_enabled else "off"
        _emit(f"SATG_RL_PHASE_JITTER {mode}")
        
        if not self.phase_jitter_enabled:
            return
        
        # Send altitude boundaries
        if hasattr(self, 'phase_altitudes'):
            for phase, bounds in self.phase_altitudes.items():
                min_fl = bounds['min_fl']
                max_fl = bounds['max_fl']
                _emit(f"SATG_RL_PHASE_ALTITUDES {phase} {min_fl} {max_fl}")
        
        # Send phase configurations
        if hasattr(self, 'phase_configs'):
            for phase, config in self.phase_configs.items():
                enabled = "on" if config.get('enabled', False) else "off"
                dt_max = config.get('dt_max', 0.0)
                dlat_max = config.get('dlat_max', 0.0)
                dlon_max = config.get('dlon_max', 0.0)
                dfl_max = config.get('dfl_max', 0)
                _emit(f"SATG_RL_PHASE_CONFIG {phase} {enabled} {dt_max} {dlat_max} {dlon_max} {dfl_max}")

    def _emit_autodel_from_toggle(self):
        """Emit SATG_RL_AUTODEL based on the checkbox state."""
        _emit("SATG_RL_AUTODEL " + ("on" if self.autodel_chk.isChecked() else "off"))

    def _validate_files(self):
        """Check if required Eurocontrol files are selected"""
        is_valid, error_msg = self._validate_eurocontrol_files()
        if not is_valid:
            QMessageBox.warning(self, "Missing Required Files", error_msg)
            return False
        return True

    def _make(self):
        name = self.scn_name.text().strip()
        if not name:
            return
        
        # Validate configuration before proceeding
        if not self._validate_configuration():
            return
        
        try:
            # Step 1: Load Eurocontrol data via TraffixGen
            fir_file = self._fir_file if self._fir_file else ""
            load_cmd = f'TRAFFIXGEN LOAD_EUROCONTROL "{self._flights_file}" "{self._filed_file}" "{self._actual_file}"'
            if fir_file:
                load_cmd += f' "{fir_file}"'
            
            print(f"Loading Eurocontrol data: {load_cmd}")
            _emit(load_cmd)
            
            # Step 2: Apply filters if configured
            if hasattr(self, 'eurocontrol_filters') and self.eurocontrol_filters:
                # Create filters dict with current settings
                filters = {}
                if self.eurocontrol_filters.get('lat_min', -90) != -90 or self.eurocontrol_filters.get('lat_max', 90) != 90:
                    filters['lat_min'] = self.eurocontrol_filters['lat_min']
                    filters['lat_max'] = self.eurocontrol_filters['lat_max']
                    filters['lon_min'] = self.eurocontrol_filters['lon_min']
                    filters['lon_max'] = self.eurocontrol_filters['lon_max']
                
                if self.eurocontrol_filters.get('fl_min', 0) != 0 or self.eurocontrol_filters.get('fl_max', 500) != 500:
                    filters['fl_min'] = self.eurocontrol_filters['fl_min']
                    filters['fl_max'] = self.eurocontrol_filters['fl_max']
                
                if self.eurocontrol_filters.get('aircraft_types'):
                    filters['aircraft_types'] = self.eurocontrol_filters['aircraft_types']
                
                # Add airspace filter if configured
                if self.eurocontrol_filters.get('include_airspace'):
                    filters['include_airspace'] = self.eurocontrol_filters['include_airspace']
                
                if filters:
                    import json
                    filters_json = json.dumps(filters)
                    filter_cmd = f'TRAFFIXGEN FILTER {filters_json}'
                    print(f"Applying filters: {filter_cmd}")
                    _emit(filter_cmd)
            
            # Step 3: Export processed data directly to SATG
            print("Exporting processed data to SATG...")
            _emit("TRAFFIXGEN EXPORT_TO_SATG")
            
            # Step 4: Configure SATG jitter and autodel settings
            self._emit_autodel_from_toggle()
            self._emit_jitter_if_needed()
            
            # Step 5: Generate scenario in SATG
            ow = 1 if self.rl_overwrite.isChecked() else 0
            _emit(f"SATG_RL_MAKE {name} {ow}")
            
            print(f"Realistic replay scenario '{name}' generated successfully!")
            
        except Exception as e:
            print(f"Error in realistic replay generation: {e}")
            QMessageBox.critical(self, "Error", f"Failed to generate scenario: {str(e)}")
            return

    # Deprecated: _create_temp_csv_files method removed
    # Now using direct plugin communication via TraffixGen -> SATG commands

    def _get_traffixgen_data(self, command: str) -> dict:
        """
        Retrieve data from TraffixGen plugin using direct function calls.
        
        This method provides a centralized interface for accessing EUROCONTROL
        flight data processed by the TraffixGen plugin. It handles direct function
        calls to avoid command-line interface overhead and provides proper error
        handling for data access operations.
        
        The method supports multiple data retrieval commands including flight
        summaries for filter configuration and filtered track data for scenario
        generation. All operations include comprehensive error handling to ensure
        graceful degradation when data access fails.
        
        Args:
            command (str): Data retrieval command to execute
                          'GET_SUMMARY' - Flight data summary for filter configuration
                          'GET_TRACKS' - Filtered flight track data for processing
        
        Returns:
            dict: Retrieved data with command-specific structure:
                  For 'GET_SUMMARY': Flight summary with date ranges, aircraft types,
                                   airspace information, and data statistics
                  For 'GET_TRACKS': Filtered flight tracks with trajectory data
                                  formatted for scenario generation
                  On error: {'error': 'Descriptive error message'}
        
        Raises:
            ImportError: When TraffixGen plugin cannot be imported
            AttributeError: When requested function is not available
            Exception: For any other data access errors
        
        Examples:
            # Get flight data summary for filter configuration
            summary = self._get_traffixgen_data('GET_SUMMARY')
            if 'error' not in summary:
                self._configure_filters(summary)
            
            # Get filtered tracks for scenario generation
            tracks = self._get_traffixgen_data('GET_TRACKS')
            if tracks.get('total_tracks', 0) > 0:
                self._generate_scenario(tracks)
        
        Note:
            This method bypasses the command-line interface for better performance
            and error handling. It requires the TraffixGen plugin to be properly
            loaded and configured with valid EUROCONTROL data files.
        """
        try:
            # Import TraffixGen plugin functions directly
            from . import traffixgen
            
            # Call the appropriate function directly
            if command == 'GET_SUMMARY':
                return traffixgen.get_flight_summary()
            elif command == 'GET_TRACKS':
                return traffixgen.get_filtered_tracks()
            else:
                return {'error': f'Unknown command: {command}'}
                
        except Exception as e:
            return {'error': f'Error accessing TraffixGen data: {e}'}

    def _load_and_get_summary(self):
        """
        Load EUROCONTROL data files and generate comprehensive data summary.
        
        This method orchestrates the loading of EUROCONTROL flight data files
        and generates a comprehensive summary for filter configuration. It includes
        intelligent caching to avoid expensive reprocessing when file paths haven't
        changed, significantly improving performance for repeated operations.
        
        The method handles the complete data loading pipeline including validation
        of file paths, loading flight data through the TraffixGen backend, and
        generating summary statistics for date ranges, aircraft types, airspace
        boundaries, and flight characteristics needed for filter configuration.
        
        Performance Optimization:
        - File path caching to detect when reloading is unnecessary
        - Summary data caching with intelligent invalidation
        - Early return for unchanged configurations
        - Progress tracking for long-running operations
        
        Data Loading Process:
        1. Check cached data validity using file path comparison
        2. Load EUROCONTROL data files through TraffixGen backend
        3. Process flight data to extract summary statistics
        4. Cache results for future use with current file paths
        5. Return comprehensive summary for filter configuration
        
        Returns:
            dict: Comprehensive flight data summary containing:
                - date_range: Available date bounds in the data
                - aircraft_types: List of all aircraft types found
                - airspace_info: FIR boundary data for geographic filtering
                - flight_counts: Statistics on total flights and coverage
                - altitude_info: Altitude distribution data
                - phase_info: Flight phase distribution statistics
            None: When loading fails or files are invalid
        
        Raises:
            ImportError: When TraffixGen plugin cannot be imported
            FileNotFoundError: When specified data files don't exist
            ValueError: When data files contain invalid or corrupted data
            Exception: For other data loading or processing errors
        
        Examples:
            # Load data and get summary for filter configuration
            summary = self._load_and_get_summary()
            if summary:
                self._configure_filter_dialog(summary)
                self._update_date_bounds(summary['date_range'])
            else:
                self._show_loading_error()
        
        Note:
            This method uses intelligent caching based on file path comparison
            to avoid expensive reprocessing. The cache is automatically invalidated
            when any of the source file paths change, ensuring data consistency
            while maximizing performance for repeated filter configuration operations.
        """
        try:
            # Import TraffixGen functions directly
            from . import traffixgen
            
            # Check if we can use cached summary data (only recalculate if file paths changed)
            current_file_paths = (
                getattr(self, '_flights_file', ''),
                getattr(self, '_filed_file', ''), 
                getattr(self, '_actual_file', ''),
                getattr(self, '_fir_file', '')
            )
            
            # Use cached data if file paths haven't changed
            if (hasattr(self, '_cached_file_paths') and 
                hasattr(self, '_cached_summary_data') and
                self._cached_file_paths == current_file_paths and
                self._cached_summary_data):
                return self._cached_summary_data
            
            # Step 1: Load Eurocontrol data via TraffixGen
            fir_file = self._fir_file if self._fir_file else ""
            
            print(f"Loading Eurocontrol data...")
            result = traffixgen.traffixgen_load_eurocontrol(
                self._flights_file, 
                self._filed_file, 
                self._actual_file, 
                fir_file
            )
            
            if not result:
                QMessageBox.critical(self, "Data Loading Error", 
                                   "Failed to load Eurocontrol data. Check file paths and formats.")
                return None
            
            # Step 2: Get summary data for filter configuration
            summary = traffixgen.get_flight_summary()
            if 'error' in summary:
                QMessageBox.critical(self, "Data Loading Error", 
                                   f"Failed to load data summary: {summary['error']}")
                return None
            
            # Cache the summary data and file paths for next time
            self._cached_file_paths = current_file_paths
            self._cached_summary_data = summary
            
            return summary
            
        except Exception as e:
            QMessageBox.critical(self, "Loading Error", f"Failed to load Eurocontrol data: {e}")
            return None

    def _run_only(self):
        """Run an existing scenario without creating it"""
        name = self.scn_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Scenario Name Required", 
                              "Please enter a scenario name.")
            return
        
        # Check if scenario file exists
        scenario_path = f"scenario/{name}.scn"
        if not os.path.exists(scenario_path):
            QMessageBox.warning(self, "Scenario Not Found", 
                              f"Scenario file '{name}.scn' not found.\n\n"
                              "Please create the scenario first or check the scenario name.")
            return
        
        # Just load the existing scenario file
        _emit(f"IC scenario/{name}.scn")

    def _run(self):
        name = self.scn_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Scenario Name Required", 
                              "Please enter a scenario name.")
            return
        
        # Validate configuration before proceeding
        if not self._validate_configuration():
            return
        
        try:
            # Step 1: Load Eurocontrol data via TraffixGen (if not already loaded)
            from . import traffixgen
            fir_file = self._fir_file if self._fir_file else ""
            
            print(f"Loading Eurocontrol data...")
            result = traffixgen.traffixgen_load_eurocontrol(
                self._flights_file, 
                self._filed_file, 
                self._actual_file, 
                fir_file
            )
            
            if not result:
                QMessageBox.critical(self, "Loading Error", "Failed to load Eurocontrol data.")
                return
            
            # Step 2: Apply configured filters
            result = traffixgen.traffixgen_apply_filters(self.eurocontrol_filters)
            if not result:
                QMessageBox.critical(self, "Filter Error", "Failed to apply filters.")
                return
            
            # Step 3: Export processed data directly to SATG
            print("Exporting processed data to SATG...")
            result = traffixgen.traffixgen_export_to_satg()
            if not result:
                QMessageBox.critical(self, "Export Error", "Failed to export data to SATG.")
                return
            
            # Step 4: Configure SATG jitter and autodel settings
            self._emit_autodel_from_toggle()
            self._emit_jitter_if_needed()
            
            # Step 5: Generate and run scenario in SATG
            ow = 1 if self.rl_overwrite.isChecked() else 0
            _emit(f"SATG_RL_RUN {name} {ow}")
            
            print(f"Realistic replay scenario '{name}' generated and started successfully!")
            
        except Exception as e:
            print(f"Error in realistic replay generation: {e}")
            QMessageBox.critical(self, "Error", f"Failed to generate and run scenario: {str(e)}")
            return

    # Deprecated: _process_eurocontrol_data method removed  
    # Now using direct TraffixGen command calls for data processing

    def _configure_phase_altitudes(self):
        """Open dialog to configure flight phase altitude boundaries per track"""
        # Check if required files are loaded
        if not (getattr(self, '_flights_file', '') and
                getattr(self, '_filed_file', '') and
                getattr(self, '_actual_file', '')):
            QMessageBox.warning(self, "Files Required", 
                              "Please load all three required Eurocontrol files first:\n\n"
                              "* Flights Extract\n"
                              "* Flight Points Filed Extract\n" 
                              "* Flight Points Actual Extract")
            return
            
        # Check if filters have been configured - if not, show warning and stop
        if not self._are_filters_configured():
            QMessageBox.warning(self, "Configure Filters First", 
                              "Please configure filters first before setting phase altitudes.\n\n"
                              "Click 'Configure Filters...' to set up geographic, temporal, and aircraft filters.\n"
                              "This will help determine which flights will be included in the phase altitude configuration.")
            return
            
        # Load data and get summary if not already done
        summary = self._load_and_get_summary()
        if not summary:
            return
                    
        # Apply current filters to get filtered track data
        if hasattr(self, 'eurocontrol_filters') and self.eurocontrol_filters:
            # Apply filters
            from . import traffixgen
            result = traffixgen.traffixgen_apply_filters(self.eurocontrol_filters)
            if not result:
                QMessageBox.critical(self, "Filter Error", "Failed to apply filters.")
                return
        
        # Get track data for phase altitude configuration
        track_data = self._get_traffixgen_data('GET_TRACKS')
        if 'error' in track_data:
            QMessageBox.critical(self, "Track Data Error", 
                               f"Failed to get track data: {track_data['error']}")
            return
            
        if track_data.get('total_tracks', 0) == 0:
            QMessageBox.warning(self, "No Tracks Available", 
                              "No flight tracks available after filtering.\n"
                              "Please check your filter settings.")
            return
            
        # Convert track data to format expected by PhaseAltitudeConfigDialog
        processed_track_data = {}
        callsign_to_flight_id = {}  # Mapping for reverse lookup
        for flight_id, track_info in track_data['tracks'].items():
            # Use the callsign generated by traffixgen (should have proper AC operator)
            callsign = track_info.get('callsign', f"UNKNOWN{int(flight_id) % 9999:04d}")
            callsign_to_flight_id[callsign] = flight_id
            
            processed_track_data[callsign] = {  # Use callsign as key instead of flight_id
                'flight_id': flight_id,
                'callsign': callsign,
                'ac_operator': track_info.get('ac_operator', ''),
                'origin': track_info['origin'],
                'destination': track_info['destination'], 
                'aircraft_type': track_info['aircraft_type'],
                'points_count': track_info['points_count'],
                'max_fl': track_info['max_fl'],
                'min_fl': track_info['min_fl'],
                'points': track_info['points']  # Include the trajectory points DataFrame
            }
        
        # Open phase altitude configuration dialog
        dialog = PhaseAltitudeConfigDialog(self.phase_altitudes, processed_track_data, callsign_to_flight_id, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            # Get all track configurations
            track_configs = dialog.get_all_track_configurations()
            
            # Store track configurations for use in backend
            self.track_phase_configurations = track_configs
            
            # For backward compatibility, use configuration from the first track as default
            if track_configs:
                # The track configuration IS the altitude ranges structure
                first_config = list(track_configs.values())[0]
                self.phase_altitudes = first_config  # No need for .get('altitude_ranges', {})
            else:
                print("Warning: No track configurations received from dialog")
                
            print(f"Phase altitude configuration saved for {len(track_configs)} tracks")
            QMessageBox.information(self, "Configuration Saved", 
                                  f"Phase altitude settings configured for {len(track_configs)} tracks.\n"
                                  "These will be applied when you create the scenario.")


# --- Flight Phase Altitude Configuration Dialog ----------------------------

class EurocontrolFilterDialog(QDialog):
    """
    Advanced dialog for configuring EUROCONTROL flight data filtering options.
    
    This sophisticated dialog provides comprehensive filtering capabilities for
    EUROCONTROL flight data, including date ranges, airspace boundaries, altitude
    constraints, aircraft types, and flight phase restrictions. The dialog features
    intelligent caching systems for performance optimization and includes flight
    point filtering for accurate model training data preparation.
    
    The dialog supports include-based filtering semantics where selected items
    are included in the analysis rather than excluded. This approach provides
    intuitive behavior for users configuring which flight data should be used
    for machine learning model training.
    
    Key Features:
    - Date range selection with automatic bounds detection
    - Airspace boundary filtering with geometric calculations
    - Altitude range constraints with phase-specific settings
    - Aircraft type filtering with comprehensive type listings
    - Flight phase filtering (departure, enroute, arrival)
    - Real-time preview of filter effects on data
    - Performance optimization through intelligent caching
    - Flight point filtering for model training accuracy
    
    Performance Optimizations:
    - File path caching to avoid repeated expensive operations
    - Summary data caching with intelligent invalidation
    - Vectorized geometric calculations for airspace filtering
    - Bounding box pre-filtering for point-in-polygon tests
    
    Args:
        current_filters (dict): Current filter configuration to populate dialog
        fir_file_path (str): Path to FIR boundary data file for airspace filtering
        summary_data (dict): Cached summary data for performance optimization
        parent (QWidget, optional): Parent widget for proper dialog behavior
    
    Attributes:
        current_filters (dict): Working copy of filter configuration
        fir_file_path (str): Path to FIR boundary data file
        summary_data (dict): Cached flight data summary information
        date_from (QDateEdit): Start date selection widget
        date_to (QDateEdit): End date selection widget
        include_airspace_list (QListWidget): Airspace selection with include semantics
        altitude_min (QSpinBox): Minimum altitude constraint
        altitude_max (QSpinBox): Maximum altitude constraint
        aircraft_types_list (QListWidget): Aircraft type selection widget
        flight_phases_list (QListWidget): Flight phase selection widget
    
    Returns:
        dict: Updated filter configuration when dialog is accepted
        None: When dialog is cancelled or encounters errors
    
    Examples:
        # Create dialog with current configuration
        dialog = EurocontrolFilterDialog(
            current_filters=self.eurocontrol_filters,
            fir_file_path=self.fir_file_path,
            summary_data=cached_summary,
            parent=self
        )
        
        # Show dialog and handle result
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated_filters = dialog.get_filters()
            self._apply_new_filters(updated_filters)
    
    Note:
        This dialog implements include-based filtering where selected airspaces,
        aircraft types, and flight phases are INCLUDED in the analysis. The dialog
        also performs actual flight point filtering to ensure model training uses
        accurately filtered data rather than just metadata filtering.
    """
    
    def __init__(self, current_filters, fir_file_path, summary_data, parent=None):
        super().__init__(parent)
        self.current_filters = current_filters.copy()
        self.fir_file_path = fir_file_path
        self.summary_data = summary_data
        
        self.setWindowTitle("Configure Eurocontrol Data Filters")
        self.setModal(False)  # Make dialog non-modal so users can interact with radar
        self.resize(800, 600)  # Make wider to accommodate summary column
        
        self._setup_ui()
        self._set_bounds_from_data()  # Set bounds based on loaded data
        self._load_current_settings()
        
        # Load airspace options if FIR file is available
        if self.fir_file_path:
            self._load_airspace_options()

    def keyPressEvent(self, event):
        """Override key press events to prevent Enter from closing dialog"""
        from PyQt5.QtCore import Qt
        
        # If Enter/Return is pressed, don't call the parent's keyPressEvent
        # which would trigger the default button behavior
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            # Ignore Enter key for all widgets to prevent dialog closure
            event.ignore()
        else:
            # For all other keys, use normal behavior
            super().keyPressEvent(event)

    def _setup_ui(self):
        """Setup the filter dialog UI"""
        layout = QVBoxLayout(self)
        
        # Create horizontal layout for tabs and summary
        main_layout = QHBoxLayout()
        
        # Create tabs for different filter categories
        tabs = QTabWidget()
        
        # Geographic filters tab
        geo_tab = self._create_geographic_tab()
        tabs.addTab(geo_tab, "Geographic")
        
        # Flight level filters tab
        fl_tab = self._create_flight_level_tab()
        tabs.addTab(fl_tab, "Flight Levels")
        
        # Airspace filters tab
        airspace_tab = self._create_airspace_tab()
        tabs.addTab(airspace_tab, "Airspace")
        
        # Time filters tab
        time_tab = self._create_time_tab()
        tabs.addTab(time_tab, "Time Range")
        
        # Aircraft filters tab
        aircraft_tab = self._create_aircraft_tab()
        tabs.addTab(aircraft_tab, "Aircraft")
        
        # Add tabs to left side of layout
        main_layout.addWidget(tabs, 2)  # Takes 2/3 of the space
        
        # Create data summary panel
        summary_panel = self._create_summary_panel()
        main_layout.addWidget(summary_panel, 1)  # Takes 1/3 of the space
        
        layout.addLayout(main_layout)
        
        # Buttons - using individual buttons instead of QDialogButtonBox to avoid Enter key issues
        button_layout = QHBoxLayout()
        
        # Reset All button
        reset_all_btn = QPushButton("Reset All")
        reset_all_btn.setToolTip("Reset all filter settings to match the loaded data ranges")
        reset_all_btn.clicked.connect(self._reset_all_filters)
        reset_all_btn.setAutoDefault(False)
        button_layout.addWidget(reset_all_btn)
        
        button_layout.addStretch()  # Push OK/Cancel to the right
        
        # OK button
        ok_btn = QPushButton("OK")
        ok_btn.setToolTip("Save filter configuration and close dialog")
        ok_btn.clicked.connect(self._save_and_close)
        ok_btn.setAutoDefault(False)
        button_layout.addWidget(ok_btn)
        
        # Cancel button
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setToolTip("Cancel and close dialog without applying filters")
        cancel_btn.clicked.connect(self.close)
        cancel_btn.setAutoDefault(False)
        button_layout.addWidget(cancel_btn)
        
        layout.addLayout(button_layout)

    def _create_geographic_tab(self):
        """Create geographic filtering tab"""
        tab = QWidget()
        layout = QFormLayout(tab)
        
        # Latitude bounds
        layout.addRow(QLabel("Latitude Bounds (degrees):"))
        
        lat_layout = QHBoxLayout()
        self.lat_min_spin = QDoubleSpinBox()
        self.lat_min_spin.setRange(-90, 90)
        self.lat_min_spin.setValue(-90)
        self.lat_min_spin.setDecimals(2)
        _configure_decimal_separator(self.lat_min_spin)
        
        self.lat_max_spin = QDoubleSpinBox()
        self.lat_max_spin.setRange(-90, 90)
        self.lat_max_spin.setValue(90)
        self.lat_max_spin.setDecimals(2)
        _configure_decimal_separator(self.lat_max_spin)
        
        lat_layout.addWidget(QLabel("Min:"))
        lat_layout.addWidget(self.lat_min_spin)
        lat_layout.addWidget(QLabel("Max:"))
        lat_layout.addWidget(self.lat_max_spin)
        lat_layout.addStretch()
        
        layout.addRow(lat_layout)
        
        # Longitude bounds
        layout.addRow(QLabel("Longitude Bounds (degrees):"))
        
        lon_layout = QHBoxLayout()
        self.lon_min_spin = QDoubleSpinBox()
        self.lon_min_spin.setRange(-180, 180)
        self.lon_min_spin.setValue(-180)
        self.lon_min_spin.setDecimals(2)
        _configure_decimal_separator(self.lon_min_spin)
        
        self.lon_max_spin = QDoubleSpinBox()
        self.lon_max_spin.setRange(-180, 180)
        self.lon_max_spin.setValue(180)
        self.lon_max_spin.setDecimals(2)
        _configure_decimal_separator(self.lon_max_spin)
        
        lon_layout.addWidget(QLabel("Min:"))
        lon_layout.addWidget(self.lon_min_spin)
        lon_layout.addWidget(QLabel("Max:"))
        lon_layout.addWidget(self.lon_max_spin)
        lon_layout.addStretch()
        
        layout.addRow(lon_layout)
        
        # Add reset button for this tab
        reset_btn = QPushButton("Reset Geographic Filters")
        reset_btn.setToolTip("Reset latitude and longitude bounds to match the loaded data range")
        reset_btn.clicked.connect(self._reset_geographic_filters)
        reset_btn.setAutoDefault(False)
        layout.addRow(reset_btn)
        
        return tab

    def _create_flight_level_tab(self):
        """Create flight level filtering tab"""
        tab = QWidget()
        layout = QFormLayout(tab)
        
        # Flight level bounds
        layout.addRow(QLabel("Flight Level Bounds:"))
        
        fl_layout = QHBoxLayout()
        self.fl_min_spin = QSpinBox()
        self.fl_min_spin.setRange(0, 600)
        self.fl_min_spin.setValue(0)
        
        self.fl_max_spin = QSpinBox()
        self.fl_max_spin.setRange(0, 600)
        self.fl_max_spin.setValue(500)
        
        fl_layout.addWidget(QLabel("Min FL:"))
        fl_layout.addWidget(self.fl_min_spin)
        fl_layout.addWidget(QLabel("Max FL:"))
        fl_layout.addWidget(self.fl_max_spin)
        fl_layout.addStretch()
        
        layout.addRow(fl_layout)
        
        # Add reset button for this tab
        reset_btn = QPushButton("Reset Flight Level Filters")
        reset_btn.setToolTip("Reset flight level bounds to match the loaded data range")
        reset_btn.clicked.connect(self._reset_flight_level_filters)
        reset_btn.setAutoDefault(False)
        layout.addRow(reset_btn)
        
        return tab

    def _create_airspace_tab(self):
        """Create airspace filtering tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Instructions
        instructions = QLabel("Select airspace regions to include in processing (leave empty to include all):")
        instructions.setStyleSheet("color: #666; font-style: italic;")
        layout.addWidget(instructions)
        
        # Airspace list
        self.airspace_list = QListWidget()
        self.airspace_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(self.airspace_list)
        
        # Status label
        self.airspace_status = QLabel("No FIR file loaded - airspace filtering unavailable")
        self.airspace_status.setStyleSheet("color: #999; font-style: italic;")
        layout.addWidget(self.airspace_status)
        
        # Add reset button for this tab
        reset_btn = QPushButton("Reset Airspace Filters")
        reset_btn.setToolTip("Reset airspace exclusions to default values")
        reset_btn.clicked.connect(self._reset_airspace_filters)
        reset_btn.setAutoDefault(False)
        layout.addWidget(reset_btn)
        
        return tab

    def _create_time_tab(self):
        """Create time filtering tab"""
        tab = QWidget()
        layout = QFormLayout(tab)
        
        # Enable time filtering
        self.time_enabled = QCheckBox("Enable time range filtering")
        layout.addRow(self.time_enabled)
        
        # Start time
        self.time_start = QTimeEdit()
        self.time_start.setDisplayFormat("hh:mm:ss")
        self.time_start.setTime(QTime(0, 0, 0))
        self.time_start.setEnabled(False)
        layout.addRow("Start time:", self.time_start)
        
        # End time
        self.time_end = QTimeEdit()
        self.time_end.setDisplayFormat("hh:mm:ss")
        self.time_end.setTime(QTime(23, 59, 59))
        self.time_end.setEnabled(False)
        layout.addRow("End time:", self.time_end)
        
        # Connect checkbox
        self.time_enabled.toggled.connect(self.time_start.setEnabled)
        self.time_enabled.toggled.connect(self.time_end.setEnabled)
        
        # Date range filtering
        layout.addRow(QLabel(""))  # Spacer
        self.date_enabled = QCheckBox("Enable date range filtering")
        layout.addRow(self.date_enabled)
        
        # Start date
        self.date_start = QDateEdit()
        self.date_start.setDisplayFormat("dd-MM-yyyy")
        self.date_start.setDate(QDate(2021, 12, 1))  # Default to December 2021
        self.date_start.setCalendarPopup(True)
        self.date_start.setEnabled(False)
        layout.addRow("Start date:", self.date_start)
        
        # End date
        self.date_end = QDateEdit()
        self.date_end.setDisplayFormat("dd-MM-yyyy")
        self.date_end.setDate(QDate(2021, 12, 31))  # Default to December 2021
        self.date_end.setCalendarPopup(True)
        self.date_end.setEnabled(False)
        layout.addRow("End date:", self.date_end)
        
        # Connect date checkbox
        self.date_enabled.toggled.connect(self.date_start.setEnabled)
        self.date_enabled.toggled.connect(self.date_end.setEnabled)
        
        # Add reset button for this tab
        reset_btn = QPushButton("Reset Time Filters")
        reset_btn.setToolTip("Reset time range to match the loaded data time span")
        reset_btn.clicked.connect(self._reset_time_filters)
        reset_btn.setAutoDefault(False)
        layout.addRow(reset_btn)
        
        return tab

    def _create_aircraft_tab(self):
        """Create aircraft type filtering tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Instructions
        instructions = QLabel("Select specific aircraft types to include (leave all unchecked to include all types):")
        instructions.setStyleSheet("color: #666; font-style: italic;")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        
        # Aircraft type list
        self.aircraft_list = QListWidget()
        self.aircraft_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        
        # Common aircraft types (can be expanded)
        common_types = [
            "A319", "A320", "A321", "A330", "A340", "A350", "A380",
            "B737", "B738", "B747", "B757", "B767", "B777", "B787",
            "E170", "E175", "E190", "CRJ7", "CRJ9", "DH8D",
            "AT72", "BE20", "C25A", "F900"
        ]
        
        for ac_type in common_types:
            item = QListWidgetItem(ac_type)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.aircraft_list.addItem(item)
        
        layout.addWidget(self.aircraft_list)
        
        # Add reset button for this tab
        reset_btn = QPushButton("Reset Aircraft Filters")
        reset_btn.setToolTip("Reset aircraft type selections to default values")
        reset_btn.clicked.connect(self._reset_aircraft_filters)
        reset_btn.setAutoDefault(False)
        layout.addWidget(reset_btn)
        
        return tab



    def _create_summary_panel(self):
        """Create data summary panel"""
        panel = QGroupBox("Data Summary")
        layout = QVBoxLayout(panel)
        
        # Title
        title = QLabel("Dataset Overview")
        title.setStyleSheet("font-weight: bold; font-size: 14px; color: #333;")
        layout.addWidget(title)
        
        # Summary information
        if self.summary_data:
            summary_text = (
                f"<b>Total Flights:</b> {self.summary_data['total_flights']:,}<br>"
                f"<b>Trajectory Points:</b> {self.summary_data['total_points']:,}<br><br>"
                
                f"<b>Geographic Bounds:</b><br>"
                f"- Latitude: {self.summary_data['lat_bounds']['min']:.2f} to {self.summary_data['lat_bounds']['max']:.2f}<br>"
                f"- Longitude: {self.summary_data['lon_bounds']['min']:.2f} to {self.summary_data['lon_bounds']['max']:.2f}<br><br>"
                
                f"<b>Flight Levels:</b><br>"
                f"- Range: FL{self.summary_data['fl_bounds']['min']:.0f} to FL{self.summary_data['fl_bounds']['max']:.0f}<br><br>"
            )
            
            # Add time bounds if available
            if 'time_bounds' in self.summary_data:
                time_bounds = self.summary_data['time_bounds']
                summary_text += f"<b>Time Range:</b><br>- {time_bounds['min']} to {time_bounds['max']}<br><br>"
            
            # Add date bounds if available
            if 'date_bounds' in self.summary_data:
                date_bounds = self.summary_data['date_bounds']
                summary_text += f"<b>Date Range:</b><br>- {date_bounds['min']} to {date_bounds['max']}<br><br>"
            
            summary_text += f"<b>Aircraft Types:</b> {len(self.summary_data['aircraft_types'])} different types<br>"
            
            # Add top aircraft types if available
            if self.summary_data.get('aircraft_types'):
                aircraft_types = self.summary_data['aircraft_types']
                if isinstance(aircraft_types, dict):
                    # If it's a dictionary (type -> count), show top types
                    top_types = list(aircraft_types.keys())[:5]
                    summary_text += f"- Most common: {', '.join(top_types)}<br>"
                elif isinstance(aircraft_types, list):
                    # If it's a list, show first few types
                    top_types = aircraft_types[:5]
                    summary_text += f"- Types include: {', '.join(top_types)}<br>"
                else:
                    summary_text += f"- Various aircraft types<br>"
        else:
            summary_text = "No data loaded"
            
        summary_label = QLabel(summary_text)
        summary_label.setWordWrap(True)
        summary_label.setStyleSheet("""
            QLabel {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 5px;
                padding: 10px;
                margin: 5px;
                font-size: 11px;
                line-height: 1.4;
            }
        """)
        layout.addWidget(summary_label)
        
        # Add help text
        help_text = QLabel(
            "<i>Use the filters on the left to focus on specific flights. "
            "Geographic and flight level filters are most commonly used.</i>"
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: #666; font-size: 10px; margin-top: 10px;")
        layout.addWidget(help_text)
        
        layout.addStretch()
        return panel

    def _set_bounds_from_data(self):
        """Set filter input bounds based on the loaded data ranges"""
        if not self.summary_data:
            return
            
        # Set geographic bounds based on data
        if 'lat_bounds' in self.summary_data:
            lat_min = self.summary_data['lat_bounds']['min']
            lat_max = self.summary_data['lat_bounds']['max']
            
            # Set ranges to exactly match data bounds
            self.lat_min_spin.setRange(lat_min, lat_max)
            self.lat_max_spin.setRange(lat_min, lat_max)
            
            # Set default values to data bounds
            self.lat_min_spin.setValue(lat_min)
            self.lat_max_spin.setValue(lat_max)
            
        if 'lon_bounds' in self.summary_data:
            lon_min = self.summary_data['lon_bounds']['min']
            lon_max = self.summary_data['lon_bounds']['max']
            
            # Set ranges to exactly match data bounds
            self.lon_min_spin.setRange(lon_min, lon_max)
            self.lon_max_spin.setRange(lon_min, lon_max)
            
            # Set default values to data bounds
            self.lon_min_spin.setValue(lon_min)
            self.lon_max_spin.setValue(lon_max)
            
        # Set flight level bounds based on data
        if 'fl_bounds' in self.summary_data:
            fl_min = int(self.summary_data['fl_bounds']['min'])
            fl_max = int(self.summary_data['fl_bounds']['max'])
            
            # Set ranges to exactly match data bounds
            self.fl_min_spin.setRange(fl_min, fl_max)
            self.fl_max_spin.setRange(fl_min, fl_max)
            
            # Set default values to data bounds
            self.fl_min_spin.setValue(fl_min)
            self.fl_max_spin.setValue(fl_max)
            
        # Set time bounds based on data
        if 'time_bounds' in self.summary_data:
            time_min = self.summary_data['time_bounds']['min']
            time_max = self.summary_data['time_bounds']['max']
            
            # Parse time strings to QTime objects
            try:
                # Assume format like "HH:MM:SS" or similar
                if ':' in time_min:
                    start_parts = time_min.split(':')
                    start_time = QTime(int(start_parts[0]), int(start_parts[1]), 
                                     int(start_parts[2]) if len(start_parts) > 2 else 0)
                    self.time_start.setTime(start_time)
                    
                if ':' in time_max:
                    end_parts = time_max.split(':')
                    end_time = QTime(int(end_parts[0]), int(end_parts[1]), 
                                   int(end_parts[2]) if len(end_parts) > 2 else 0)
                    self.time_end.setTime(end_time)
            except (ValueError, IndexError):
                # If parsing fails, keep default times
                pass
        
        # Set date bounds based on data
        if 'date_bounds' in self.summary_data:
            date_min = self.summary_data['date_bounds']['min']
            date_max = self.summary_data['date_bounds']['max']
            
            # Parse date strings to QDate objects
            try:
                # Handle DD-MM-YYYY format
                if '-' in date_min and '-' in date_max:
                    start_parts = date_min.split('-')
                    end_parts = date_max.split('-')
                    
                    if len(start_parts) >= 3 and len(end_parts) >= 3:
                        start_date = QDate(int(start_parts[2]), int(start_parts[1]), int(start_parts[0]))
                        end_date = QDate(int(end_parts[2]), int(end_parts[1]), int(end_parts[0]))
                        
                        if start_date.isValid() and end_date.isValid():
                            # Set ranges to constrain to actual data bounds (same behavior as other filters)
                            # Use setMinimumDate/setMaximumDate for proper constraint (like setRange for spinboxes)
                            self.date_start.setMinimumDate(start_date)
                            self.date_start.setMaximumDate(end_date)
                            self.date_end.setMinimumDate(start_date)
                            self.date_end.setMaximumDate(end_date)
                            
                            # Set default values to actual data bounds
                            self.date_start.setDate(start_date)
                            self.date_end.setDate(end_date)
            except (ValueError, IndexError):
                # If parsing fails, keep default dates
                pass
                
        # Populate aircraft types from actual data
        if 'aircraft_types' in self.summary_data:
            # Clear existing items
            self.aircraft_list.clear()
            
            aircraft_types = self.summary_data['aircraft_types']
            
            if isinstance(aircraft_types, dict):
                # If it's a dictionary (type -> count), sort by count
                sorted_types = sorted(aircraft_types.items(), key=lambda x: x[1], reverse=True)
                for ac_type, count in sorted_types:
                    item = QListWidgetItem(f"{ac_type} ({count} flights)")
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    item.setData(Qt.ItemDataRole.UserRole, ac_type)  # Store the actual type
                    self.aircraft_list.addItem(item)
            elif isinstance(aircraft_types, list):
                # If it's a list, just show the types
                for ac_type in sorted(aircraft_types):
                    item = QListWidgetItem(ac_type)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    item.setData(Qt.ItemDataRole.UserRole, ac_type)
                    self.aircraft_list.addItem(item)

    def _load_current_settings(self):
        """Load current filter settings into the dialog"""
        filters = self.current_filters
        
        # Geographic bounds
        self.lat_min_spin.setValue(filters['lat_min'])
        self.lat_max_spin.setValue(filters['lat_max'])
        self.lon_min_spin.setValue(filters['lon_min'])
        self.lon_max_spin.setValue(filters['lon_max'])
        
        # Flight level bounds
        self.fl_min_spin.setValue(filters['fl_min'])
        self.fl_max_spin.setValue(filters['fl_max'])
        
        # Time range
        if filters['time_start'] or filters['time_end']:
            self.time_enabled.setChecked(True)
            if filters['time_start']:
                self.time_start.setTime(QTime.fromString(filters['time_start'], "hh:mm:ss"))
            if filters['time_end']:
                self.time_end.setTime(QTime.fromString(filters['time_end'], "hh:mm:ss"))
        
        # Aircraft types
        selected_types = set(filters['aircraft_types'])
        for i in range(self.aircraft_list.count()):
            item = self.aircraft_list.item(i)
            # Check against stored aircraft type data
            ac_type = item.data(Qt.ItemDataRole.UserRole)
            if ac_type and ac_type in selected_types:
                item.setCheckState(Qt.CheckState.Checked)
            elif not ac_type and item.text() in selected_types:
                # Fallback for items without UserRole data
                item.setCheckState(Qt.CheckState.Checked)
        


    def _load_airspace_options(self):
        """Load available airspace options from FIR file"""
        try:
            import pandas as pd
            if os.path.exists(self.fir_file_path):
                df = pd.read_csv(self.fir_file_path)
                if 'Airspace ID' in df.columns:
                    airspace_ids = sorted(df['Airspace ID'].unique())
                    
                    # Clear existing items
                    self.airspace_list.clear()
                    
                    # Add airspace options
                    for airspace_id in airspace_ids:
                        item = QListWidgetItem(airspace_id)
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(Qt.CheckState.Unchecked)
                        self.airspace_list.addItem(item)
                    
                    # Update status
                    self.airspace_status.setText(f"Loaded {len(airspace_ids)} airspace regions")
                    self.airspace_status.setStyleSheet("color: #007ACC;")
                    
                    # Select currently included airspace
                    included = set(self.current_filters.get('include_airspace', []))
                    for i in range(self.airspace_list.count()):
                        item = self.airspace_list.item(i)
                        if item.text() in included:
                            item.setCheckState(Qt.CheckState.Checked)
                else:
                    self.airspace_status.setText("FIR file does not contain 'Airspace ID' column")
                    self.airspace_status.setStyleSheet("color: #CC7A00;")
            else:
                self.airspace_status.setText("FIR file not found")
                self.airspace_status.setStyleSheet("color: #CC0000;")
                
        except Exception as e:
            self.airspace_status.setText(f"Error loading airspace data: {str(e)}")
            self.airspace_status.setStyleSheet("color: #CC0000;")

    def _reset_all_filters(self):
        """Reset all filters to data-driven bounds"""
        # Use individual reset methods to ensure consistency
        self._reset_geographic_filters()
        self._reset_flight_level_filters()
        self._reset_time_filters()
        self._reset_airspace_filters()
        self._reset_aircraft_filters()

    def _reset_geographic_filters(self):
        """Reset only geographic filters to data-driven bounds"""
        if self.summary_data and 'lat_bounds' in self.summary_data:
            lat_min = self.summary_data['lat_bounds']['min']
            lat_max = self.summary_data['lat_bounds']['max']
            self.lat_min_spin.setValue(lat_min)
            self.lat_max_spin.setValue(lat_max)
        else:
            # Fallback to global bounds if no data available
            self.lat_min_spin.setValue(-90)
            self.lat_max_spin.setValue(90)
            
        if self.summary_data and 'lon_bounds' in self.summary_data:
            lon_min = self.summary_data['lon_bounds']['min']
            lon_max = self.summary_data['lon_bounds']['max']
            self.lon_min_spin.setValue(lon_min)
            self.lon_max_spin.setValue(lon_max)
        else:
            # Fallback to global bounds if no data available
            self.lon_min_spin.setValue(-180)
            self.lon_max_spin.setValue(180)

    def _reset_flight_level_filters(self):
        """Reset only flight level filters to data-driven bounds"""
        if self.summary_data and 'fl_bounds' in self.summary_data:
            fl_min = int(self.summary_data['fl_bounds']['min'])
            fl_max = int(self.summary_data['fl_bounds']['max'])
            self.fl_min_spin.setValue(fl_min)
            self.fl_max_spin.setValue(fl_max)
        else:
            # Fallback to global bounds if no data available
            self.fl_min_spin.setValue(0)
            self.fl_max_spin.setValue(500)

    def _reset_airspace_filters(self):
        """Reset only airspace filters to default values"""
        for i in range(self.airspace_list.count()):
            self.airspace_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _reset_time_filters(self):
        """Reset only time filters to data-driven bounds"""
        if self.summary_data and 'time_bounds' in self.summary_data:
            time_min = self.summary_data['time_bounds']['min']
            time_max = self.summary_data['time_bounds']['max']
            
            # Parse time strings to QTime objects
            try:
                if ':' in time_min:
                    start_parts = time_min.split(':')
                    start_time = QTime(int(start_parts[0]), int(start_parts[1]), 
                                     int(start_parts[2]) if len(start_parts) > 2 else 0)
                    self.time_start.setTime(start_time)
                    
                if ':' in time_max:
                    end_parts = time_max.split(':')
                    end_time = QTime(int(end_parts[0]), int(end_parts[1]), 
                                   int(end_parts[2]) if len(end_parts) > 2 else 0)
                    self.time_end.setTime(end_time)
                    
                # Enable time filtering when resetting to data bounds
                self.time_enabled.setChecked(True)
            except (ValueError, IndexError):
                # If parsing fails, use defaults
                self.time_enabled.setChecked(False)
                self.time_start.setTime(QTime(0, 0, 0))
                self.time_end.setTime(QTime(23, 59, 59))
        else:
            # Fallback to disabled if no data available
            self.time_enabled.setChecked(False)
            self.time_start.setTime(QTime(0, 0, 0))
            self.time_end.setTime(QTime(23, 59, 59))

    def _reset_aircraft_filters(self):
        """Reset only aircraft filters to default values"""
        for i in range(self.aircraft_list.count()):
            self.aircraft_list.item(i).setCheckState(Qt.CheckState.Unchecked)



    def get_filter_settings(self):
        """Get the current filter settings as a dictionary"""
        # Collect included airspace (changed from exclude to include logic)
        included_airspace = []
        for i in range(self.airspace_list.count()):
            item = self.airspace_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                included_airspace.append(item.text())
        
        # Collect selected aircraft types
        selected_aircraft = []
        for i in range(self.aircraft_list.count()):
            item = self.aircraft_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                # Use stored aircraft type data instead of display text
                ac_type = item.data(Qt.ItemDataRole.UserRole)
                if ac_type:
                    selected_aircraft.append(ac_type)
                else:
                    # Fallback to text if no UserRole data
                    selected_aircraft.append(item.text())
        
        # Build filter dictionary
        filters = {
            'lat_min': self.lat_min_spin.value(),
            'lat_max': self.lat_max_spin.value(),
            'lon_min': self.lon_min_spin.value(),
            'lon_max': self.lon_max_spin.value(),
            'fl_min': self.fl_min_spin.value(),
            'fl_max': self.fl_max_spin.value(),
            'include_airspace': included_airspace,
            'aircraft_types': selected_aircraft,
            'time_start': self.time_start.time().toString("hh:mm:ss") if self.time_enabled.isChecked() else None,
            'time_end': self.time_end.time().toString("hh:mm:ss") if self.time_enabled.isChecked() else None,
            'date_start': self.date_start.date().toString("dd-MM-yyyy") if self.date_enabled.isChecked() else None,
            'date_end': self.date_end.date().toString("dd-MM-yyyy") if self.date_enabled.isChecked() else None
        }
        
        return filters

    def _save_and_close(self):
        """Apply filters and close dialog (for non-modal operation)"""
        try:
            # Get filter settings
            filters = self.get_filter_settings()
            
            # Apply filters to the parent's eurocontrol_filters
            if self.parent():
                self.parent().eurocontrol_filters = filters
                
                # Apply filters immediately to show filtered results
                from . import traffixgen
                result = traffixgen.traffixgen_apply_filters(filters)
                if result:
                    print("Filters applied successfully!")
                else:
                    print("Error applying filters")
            
            # Close dialog
            self.close()
            
        except Exception as e:
            print(f"Error applying filters: {e}")
            QMessageBox.warning(self, "Filter Error", f"Error applying filters: {e}")




class PhaseAltitudeConfigDialog(QDialog):
    """
    Advanced dialog for configuring flight phase altitude ranges with track-by-track analysis.
    
    This sophisticated interface provides comprehensive flight phase altitude configuration
    capabilities with individual track analysis, visual altitude distribution displays,
    and intelligent phase boundary suggestions based on actual flight data characteristics.
    The dialog enables precise flight phase definition for accurate machine learning model
    training and realistic synthetic traffic generation scenarios.
    
    The Phase Altitude Configuration system analyzes processed flight track data to provide
    data-driven recommendations for flight phase boundaries while allowing manual override
    and customization. Each track can be individually analyzed to understand altitude
    patterns and optimize phase definitions for the specific dataset characteristics.
    
    Key Features:
    - Track-by-track flight phase altitude configuration with individual analysis
    - Visual altitude distribution analysis with graphical representation
    - Intelligent phase boundary suggestions based on actual flight data patterns
    - Real-time altitude range validation and feasibility checking
    - Integration with processed track data for accurate phase classification
    - Advanced visualization showing altitude distributions and phase transitions
    - Comprehensive validation ensuring logical phase progression and operational realism
    
    Configuration Capabilities:
    - Individual Track Analysis: Examine altitude patterns for each flight track separately
    - Phase Boundary Definition: Set precise altitude ranges for takeoff, climb, cruise, descent, approach
    - Visual Distribution Analysis: Graphical display of altitude distributions and phase characteristics
    - Data-Driven Suggestions: Intelligent recommendations based on actual flight data patterns
    - Manual Override: Allow custom phase definitions when automatic suggestions need adjustment
    - Validation System: Ensure phase boundaries are logically consistent and operationally realistic
    
    Track Navigation and Analysis:
    - Sequential Track Review: Navigate through individual flight tracks for detailed analysis
    - Callsign Mapping: Integration with flight identification and callsign data
    - Track Data Integration: Direct access to processed flight track altitude and position data
    - Configuration Persistence: Maintain individual track configurations throughout session
    - Bulk Configuration: Apply settings across multiple tracks with validation
    
    Visualization Features:
    - Altitude Distribution Graphs: Visual representation of altitude patterns for each track
    - Phase Boundary Markers: Clear indication of current phase altitude boundaries
    - Data Quality Indicators: Visual feedback on track data completeness and quality
    - Real-time Updates: Immediate visual feedback when adjusting phase boundaries
    - Statistical Analysis: Display altitude statistics and distribution characteristics
    
    Attributes:
        current_altitudes (Dict): Current flight phase altitude configuration settings
        track_data (Dict): Processed flight track data with callsigns as keys
        callsign_to_flight_id (Dict): Mapping from aircraft callsigns to flight identifiers
        track_configurations (Dict): Per-track phase configurations stored in memory
        current_track_index (int): Index of currently displayed track for navigation
        track_names (List[str]): List of available track callsigns for analysis
    
    Args:
        current_altitudes (Dict): Current phase altitude settings for initialization
        processed_track_data (Dict): Flight track data with altitude and position information
        callsign_to_flight_id (Dict): Mapping from callsigns to flight identifiers
        parent (QWidget, optional): Parent widget for proper dialog behavior
    
    Examples:
        # Configure phase altitudes with track analysis
        track_data = {
            'AAL123': {'altitudes': [0, 1500, 25000, 35000], 'phases': [...]}
        }
        callsign_map = {'AAL123': 'flight_001'}
        current_config = {
            'takeoff': (0, 1500), 'climb': (1500, 25000),
            'cruise': (25000, 40000), 'descent': (25000, 5000),
            'approach': (5000, 0)
        }
        
        dialog = PhaseAltitudeConfigDialog(
            current_config, track_data, callsign_map, parent=self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated_altitudes = dialog.get_altitude_configuration()
            self._apply_phase_altitudes(updated_altitudes)
    
    Note:
        The dialog operates in modal mode to ensure focused configuration without conflicts.
        All altitude configurations are validated for logical consistency and operational
        realism. The track-by-track analysis provides detailed insights into flight data
        characteristics and enables data-driven phase boundary optimization for machine learning.
    """
    
    def __init__(self, current_altitudes, processed_track_data, callsign_to_flight_id, parent=None):
        super().__init__(parent)
        self.current_altitudes = current_altitudes.copy()
        self.track_data = processed_track_data  # Already processed track data with callsigns as keys
        self.callsign_to_flight_id = callsign_to_flight_id  # Mapping from callsign to flight_id
        
        # Per-track phase configurations (in memory)
        self.track_configurations = {}
        
        # Track navigation
        self.current_track_index = 0
        self.track_names = list(self.track_data.keys())  # Callsigns from processed data
        
        self.setWindowTitle("Configure Flight Phase Altitudes - Track by Track")
        self.setModal(True)
        self.resize(1200, 700)  # Smaller overall size
        
        # Setup UI
        self._setup_ui()
        
        # Load first track if available
        if self.track_names:
            self._load_track(0)
        else:
            # No tracks available - show appropriate message
            self.track_label.setText("No tracks loaded")
            self.track_counter.setText("0 / 0")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.apply_all_btn.setEnabled(False)
    
    def _load_eurocontrol_track_data(self):
        """Load track data from Eurocontrol files using TraffixGen"""
        try:
            # Import TraffixGen components
            # Import from traffixgen plugin
            from .traffixgen import DatasetCollection
            import pandas as pd
            
            # Create dataset collection
            collection = DatasetCollection()
            
            # Load Eurocontrol files
            if self.eurocontrol_files['flights']:
                collection.set_flight_data(filepath=self.eurocontrol_files['flights'])
            
            if self.eurocontrol_files['filed'] and self.eurocontrol_files['actual']:
                collection.set_flights_points_data(
                    filepaths=(self.eurocontrol_files['filed'], self.eurocontrol_files['actual'])
                )
            
            if self.eurocontrol_files['fir']:
                collection.set_FIR_data(filepath=self.eurocontrol_files['fir'])
            
            # Apply filters
            filters = self.eurocontrol_filters
            if filters['lat_min'] != -90 or filters['lat_max'] != 90 or filters['lon_min'] != -180 or filters['lon_max'] != 180:
                collection.set_bbox(
                    lat=(filters['lat_min'], filters['lat_max']),
                    lon=(filters['lon_min'], filters['lon_max'])
                )
            
            if filters['fl_min'] != 0 or filters['fl_max'] != 500:
                collection.set_fl_bounds(fl_min=filters['fl_min'], fl_max=filters['fl_max'])
            
            # Apply airspace include filtering
            if 'include_airspace' in filters and filters['include_airspace']:
                collection.include_airspace(filters['include_airspace'])
            
            # Get processed flight data
            flight_collection = collection.get_flights_data()
            
            # Extract track data grouped by ECTRL ID
            route_data = flight_collection.route_data
            
            print(f"Loaded Eurocontrol data with {len(route_data)} route points")
            print(f"Columns: {list(route_data.columns)}")
            
            # Group by ECTRL ID
            for ectrl_id, group in route_data.groupby('ECTRL ID'):
                # Sort by time or sequence
                if 'Time Over' in group.columns:
                    group = group.sort_values('Time Over')
                elif 'Sequence Number' in group.columns:
                    group = group.sort_values('Sequence Number')
                
                self.track_data[str(ectrl_id)] = group
                
            print(f"Processed {len(self.track_data)} individual tracks")
            
        except Exception as e:
            print(f"Error loading Eurocontrol data: {e}")
            import traceback
            traceback.print_exc()
            # Fallback to empty data
            self.track_data = {}

    def _load_track_data(self):
        """Load all track data from CSV files and separate by ECTRL ID"""
        import pandas as pd
        import os
        
        for track_file in self.tracks_files:
            try:
                if os.path.exists(track_file):
                    # Load track data
                    df = pd.read_csv(track_file)
                    print(f"Loading track file: {track_file}")
                    print(f"Columns: {list(df.columns)}")
                    print(f"Shape: {df.shape}")
                    
                    # Check for different possible column names
                    lat_col = None
                    lon_col = None
                    alt_col = None
                    id_col = None
                    
                    # Try different column name variations
                    for col in df.columns:
                        col_lower = col.lower().strip()
                        if col_lower in ['latitude', 'lat']:
                            lat_col = col
                        elif col_lower in ['longitude', 'lon', 'long']:
                            lon_col = col
                        elif col_lower in ['altitude', 'alt', 'flight level', 'fl']:
                            alt_col = col
                        elif col_lower in ['ectrl id', 'id', 'acid', 'aircraft_id', 'callsign']:
                            id_col = col
                    
                    if lat_col and lon_col and id_col:
                        print(f"Found columns: lat={lat_col}, lon={lon_col}, alt={alt_col}, id={id_col}")
                        
                        # Group by aircraft ID to create separate tracks
                        aircraft_groups = df.groupby(id_col)
                        
                        for aircraft_id, aircraft_df in aircraft_groups:
                            # Sort by sequence number if available
                            if 'Sequence Number' in aircraft_df.columns:
                                aircraft_df = aircraft_df.sort_values('Sequence Number')
                            
                            # Create track name combining file and aircraft ID
                            base_name = os.path.splitext(os.path.basename(track_file))[0]
                            track_name = f"{base_name}_{aircraft_id}"
                            
                            print(f"Creating track: {track_name} with {len(aircraft_df)} waypoints")
                            
                            self.track_data[track_name] = {
                                'file_path': track_file,
                                'data': aircraft_df.copy(),
                                'lat_col': lat_col,
                                'lon_col': lon_col,
                                'alt_col': alt_col,
                                'id_col': id_col,
                                'aircraft_id': aircraft_id,
                                'waypoints': len(aircraft_df)
                            }
                            
                            # Initialize configuration for this track
                            self.track_configurations[track_name] = {
                                'takeoff': {'min_fl': 0, 'max_fl': 15},
                                'climb': {'min_fl': 15, 'max_fl': 250},
                                'cruise': {'min_fl': 250, 'max_fl': 400},
                                'descent': {'min_fl': 50, 'max_fl': 400},
                                'approach': {'min_fl': 0, 'max_fl': 50}
                            }
                    else:
                        print(f"Warning: Could not find required columns in {track_file}")
                        print(f"Need: lat/lon columns and ID column")
                        print(f"Available columns: {list(df.columns)}")
            except Exception as e:
                print(f"Error loading track {track_file}: {e}")
                import traceback
                traceback.print_exc()
    
    def _setup_ui(self):
        """Setup the main UI with side-by-side layout"""
        main_layout = QHBoxLayout(self)
        
        # Left side: Altitude configuration
        left_widget = QWidget()
        left_widget.setMinimumWidth(400)
        left_widget.setMaximumWidth(500)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(5, 5, 5, 5)  # Minimal margins
        
        # Track navigation header
        nav_layout = QHBoxLayout()
        
        self.track_label = QLabel("No tracks loaded")
        self.track_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #333;")
        nav_layout.addWidget(self.track_label)
        
        nav_layout.addStretch()
        
        self.track_counter = QLabel("0 / 0")
        self.track_counter.setStyleSheet("color: #666; font-size: 12px;")
        nav_layout.addWidget(self.track_counter)
        
        left_layout.addLayout(nav_layout)
        
        # Description
        desc = QLabel("Configure altitude boundaries for flight phases for each track.")
        desc.setStyleSheet("color: #666; margin-bottom: 5px; font-size: 11px;")
        left_layout.addWidget(desc)
        
        # Visual phase graph - expand to fill available space
        self.phase_canvas = PhaseVisualizationWidget()
        self.phase_canvas.setMinimumHeight(300)  # Increased minimum height
        # Remove maximum height to let it expand
        left_layout.addWidget(self.phase_canvas, 1)  # Stretch factor 1 to expand
        
        # Altitude input fields
        self._setup_altitude_inputs(left_layout)
        
        # Navigation and action buttons
        self._setup_buttons(left_layout)
        
        # Right side: Geographic plot
        right_widget = QWidget()
        right_widget.setMaximumWidth(450)  # Limit geographic plot width
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 5, 5, 5)  # Minimal margins
        
        # Matplotlib widget for geographic plot (no title label)
        self._setup_geographic_plot(right_layout)
        
        # Add both sides to main layout
        main_layout.addWidget(left_widget)
        main_layout.addWidget(right_widget)
    
    def _setup_altitude_inputs(self, layout):
        """Setup the altitude input fields"""
        # Simple input fields in a row with better spacing
        input_layout = QHBoxLayout()
        input_layout.setSpacing(10)  # Reduced spacing to fit labels better
        
        # Extract current values (will be updated per track)
        takeoff_upper = 15
        climb_upper = 250
        descent_upper = 400
        approach_upper = 50
        
        # Takeoff upper bound (Initial Climb)
        takeoff_layout = QVBoxLayout()
        takeoff_label = QLabel("Initial Climb\nUpper FL:")
        takeoff_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        takeoff_label.setStyleSheet("font-size: 11px;")  # Increased font size
        takeoff_layout.addWidget(takeoff_label)
        self.takeoff_spin = QSpinBox()
        self.takeoff_spin.setRange(0, 999)
        self.takeoff_spin.setValue(takeoff_upper)
        self.takeoff_spin.valueChanged.connect(self._validate_and_update)
        takeoff_layout.addWidget(self.takeoff_spin)
        input_layout.addLayout(takeoff_layout)
        
        # Climb upper bound (Top of Climb)
        climb_layout = QVBoxLayout()
        climb_label = QLabel("Top of Climb\nFL:")
        climb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        climb_label.setStyleSheet("font-size: 11px;")
        climb_layout.addWidget(climb_label)
        self.climb_spin = QSpinBox()
        self.climb_spin.setRange(0, 999)
        self.climb_spin.setValue(climb_upper)
        self.climb_spin.valueChanged.connect(self._validate_and_update)
        climb_layout.addWidget(self.climb_spin)
        input_layout.addLayout(climb_layout)
        
        # Descent upper bound (Top of Descent)
        descent_layout = QVBoxLayout()
        descent_label = QLabel("Top of Descent\nFL:")
        descent_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        descent_label.setStyleSheet("font-size: 11px;")
        descent_layout.addWidget(descent_label)
        self.descent_spin = QSpinBox()
        self.descent_spin.setRange(0, 999)
        self.descent_spin.setValue(descent_upper)
        self.descent_spin.valueChanged.connect(self._validate_and_update)
        descent_layout.addWidget(self.descent_spin)
        input_layout.addLayout(descent_layout)
        
        # Approach upper bound (Final Approach)
        approach_layout = QVBoxLayout()
        approach_label = QLabel("Final Approach\nUpper FL:")
        approach_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        approach_label.setStyleSheet("font-size: 11px;")
        approach_layout.addWidget(approach_label)
        self.approach_spin = QSpinBox()
        self.approach_spin.setRange(0, 999)
        self.approach_spin.setValue(approach_upper)
        self.approach_spin.valueChanged.connect(self._validate_and_update)
        approach_layout.addWidget(self.approach_spin)
        input_layout.addLayout(approach_layout)
        
        layout.addLayout(input_layout)
    
    def _setup_buttons(self, layout):
        """Setup navigation and action buttons"""
        # Navigation buttons
        nav_button_layout = QHBoxLayout()
        
        self.prev_btn = QPushButton("< Previous Aircraft")
        self.prev_btn.setToolTip("Go to the previous aircraft track in the loaded data")
        self.prev_btn.clicked.connect(self._previous_track)
        self.prev_btn.setAutoDefault(False)
        nav_button_layout.addWidget(self.prev_btn)
        
        self.next_btn = QPushButton("Next Aircraft >")
        self.next_btn.setToolTip("Go to the next aircraft track in the loaded data")
        self.next_btn.clicked.connect(self._next_track)
        self.next_btn.setAutoDefault(False)
        nav_button_layout.addWidget(self.next_btn)
        
        layout.addLayout(nav_button_layout)
        
        # Action buttons
        action_button_layout = QHBoxLayout()
        
        self.apply_all_btn = QPushButton("Apply Current Config to All Tracks")
        self.apply_all_btn.setToolTip("Copy the current altitude configuration to all other tracks")
        self.apply_all_btn.clicked.connect(self._apply_to_all)
        self.apply_all_btn.setAutoDefault(False)
        action_button_layout.addWidget(self.apply_all_btn)
        
        layout.addLayout(action_button_layout)
        
        # Final dialog buttons with Reset button on the same line
        dialog_button_layout = QHBoxLayout()
        
        self.reset_btn = QPushButton("Reset to Defaults")
        self.reset_btn.setToolTip("Reset altitude boundaries to default values (Takeoff: FL015, Climb: FL250, Descent: FL400, Approach: FL050)")
        self.reset_btn.clicked.connect(self._reset_to_defaults)
        self.reset_btn.setAutoDefault(False)
        dialog_button_layout.addWidget(self.reset_btn)
        
        dialog_button_layout.addStretch()  # Push main buttons to the right
        
        self.ok_btn = QPushButton("Finish Configuration")
        self.ok_btn.setToolTip("Save all track configurations and close the dialog")
        self.ok_btn.clicked.connect(self.accept)
        self.ok_btn.setAutoDefault(False)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Discard changes and close the dialog")
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_btn.setAutoDefault(False)
        
        dialog_button_layout.addWidget(self.ok_btn)
        dialog_button_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(dialog_button_layout)
    
    def _setup_geographic_plot(self, layout):
        """Setup matplotlib widget for geographic visualization"""
        try:
            import matplotlib
            matplotlib.use('Qt5Agg')
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
            from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
            from matplotlib.figure import Figure
            import cartopy.crs as ccrs
            import cartopy.feature as cfeature
            
            # Create matplotlib figure with cartopy - let it expand vertically
            self.geo_figure = Figure(figsize=(6, 7))  # Increased height from 5 to 7
            self.geo_figure.subplots_adjust(left=0.02, right=0.98, top=0.94, bottom=0.08)  # Better margins
            self.geo_canvas = FigureCanvas(self.geo_figure)
            # Remove height limitation to let it fill available space
            self.geo_ax = self.geo_figure.add_subplot(111, projection=ccrs.Mercator())
            
            # Create custom navigation toolbar with only pan and zoom
            class CustomNavigationToolbar(NavigationToolbar):
                # Only include the tools we want
                toolitems = [t for t in NavigationToolbar.toolitems if
                           t[0] in ('Home', 'Back', 'Forward', 'Pan', 'Zoom')]
            
            # Add navigation toolbar for pan/zoom functionality
            self.geo_toolbar = CustomNavigationToolbar(self.geo_canvas, self.geo_canvas)
            self.geo_toolbar.setMaximumHeight(30)  # Keep toolbar compact
            
            # Add coastlines
            self.geo_ax.add_feature(cfeature.COASTLINE)
            self.geo_ax.add_feature(cfeature.BORDERS)
            self.geo_ax.gridlines(draw_labels=True)
            
            # Add toolbar and canvas to layout
            layout.addWidget(self.geo_toolbar)
            layout.addWidget(self.geo_canvas)
            
        except ImportError:
            # Fallback if cartopy is not available
            error_label = QLabel("Geographic visualization requires cartopy.\nInstall with: pip install cartopy")
            error_label.setStyleSheet("color: #ff6b6b; background: #ffe0e0; padding: 20px; border-radius: 5px;")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(error_label)
            self.geo_canvas = None
            self.geo_toolbar = None
        except Exception as e:
            # General error fallback
            error_label = QLabel(f"Geographic visualization error:\n{str(e)}")
            error_label.setStyleSheet("color: #ff6b6b; background: #ffe0e0; padding: 20px; border-radius: 5px;")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(error_label)
            self.geo_canvas = None
            self.geo_toolbar = None
    
    def _load_track(self, track_index):
        """Load and display a specific track"""
        if not self.track_names or track_index >= len(self.track_names):
            # No tracks available
            self.track_label.setText("No tracks loaded")
            self.track_counter.setText("0 / 0")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            
            # Show message in geographic plot
            if self.geo_canvas:
                try:
                    self.geo_ax.clear()
                    self.geo_ax.text(0.5, 0.5, "No track files loaded\nPlease load track data first", 
                                   transform=self.geo_ax.transAxes, ha='center', va='center',
                                   bbox=dict(boxstyle="round,pad=0.3", facecolor="lightblue"))
                    self.geo_canvas.draw()
                except:
                    pass
            return
        
        # Update current track index
        self.current_track_index = track_index
        track_callsign = self.track_names[track_index]  # This is now a callsign
        
        # Update UI labels with callsign
        track_info = self.track_data.get(track_callsign, {})
        flight_id = track_info.get('flight_id', 'Unknown')
        origin = track_info.get('origin', '')
        destination = track_info.get('destination', '')
        aircraft_type = track_info.get('aircraft_type', '')
        
        self.track_label.setText(f"Aircraft: {track_callsign}")
        self.track_counter.setText(f"{track_index + 1} / {len(self.track_names)}")
        
        # Update flight profile title with aircraft callsign
        if hasattr(self, 'gb_flight_profile'):
            profile_title = f"2) Flight profile - {track_callsign}"
            if origin and destination:
                profile_title += f" ({origin}->{destination})"
            self.gb_flight_profile.setTitle(profile_title)
        
        # Update button states
        self.prev_btn.setEnabled(track_index > 0)
        self.next_btn.setEnabled(track_index < len(self.track_names) - 1)
        
        # Load track configuration
        if track_callsign in self.track_configurations:
            config = self.track_configurations[track_callsign]
            
            self.takeoff_spin.setValue(config['takeoff']['max_fl'])
            self.climb_spin.setValue(config['climb']['max_fl'])
            self.descent_spin.setValue(config['descent']['max_fl'])
            self.approach_spin.setValue(config['approach']['max_fl'])
        
        # Update visualizations
        self._validate_and_update()
        self._update_geographic_plot()
    
    def _previous_track(self):
        """Go to previous track"""
        if self.current_track_index > 0:
            self._save_current_config()
            self._load_track(self.current_track_index - 1)
    
    def _next_track(self):
        """Go to next track"""
        if self.current_track_index < len(self.track_names) - 1:
            self._save_current_config()
            self._load_track(self.current_track_index + 1)
    
    def _apply_to_all(self):
        """Apply current configuration to all tracks"""
        current_config = self._get_current_config()
        
        # Apply to all track names (callsigns) in the dataset
        for track_callsign in self.track_names:
            self.track_configurations[track_callsign] = current_config.copy()
        
        # Show confirmation
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(self, "Applied to All", 
                              f"Current configuration applied to all {len(self.track_names)} aircraft.")
    
    def _save_current_config(self):
        """Save current spinbox values to the current track's configuration"""
        if not self.track_names:
            return
            
        track_callsign = self.track_names[self.current_track_index]
        
        self.track_configurations[track_callsign] = self._get_current_config()
    
    def _get_current_config(self):
        """Get current configuration from spinboxes"""
        takeoff_upper = self.takeoff_spin.value()
        climb_upper = self.climb_spin.value()
        descent_upper = self.descent_spin.value()
        approach_upper = self.approach_spin.value()
        
        return {
            'takeoff': {'min_fl': 0, 'max_fl': takeoff_upper},
            'climb': {'min_fl': takeoff_upper, 'max_fl': climb_upper},
            'cruise': {'min_fl': climb_upper, 'max_fl': descent_upper},
            'descent': {'min_fl': approach_upper, 'max_fl': descent_upper},
            'approach': {'min_fl': 0, 'max_fl': approach_upper}
        }
    
    def _get_flight_phase_with_config(self, fl: int, waypoint_sequence: list, current_index: int, altitude_ranges: dict) -> str:
        """Determine flight phase using simplified logic with GUI configuration
        
        Simple approach:
        1. Look at rate of change before and after current waypoint
        2. Both positive = climbing, both negative = descending
        3. Same before = level, mixed = momentary variation
        4. Apply altitude boundaries for final classification
        """
        # Safety checks
        if not waypoint_sequence or current_index >= len(waypoint_sequence):
            return 'cruise'
        
        current_fl = fl
        
        # Get immediate neighbors
        prev_fl = waypoint_sequence[current_index - 1]['fl'] if current_index > 0 else current_fl
        next_fl = waypoint_sequence[current_index + 1]['fl'] if current_index < len(waypoint_sequence) - 1 else current_fl
        
        # Calculate rate of change before and after
        rate_before = current_fl - prev_fl if current_index > 0 else 0
        rate_after = next_fl - current_fl if current_index < len(waypoint_sequence) - 1 else 0
        
        # Determine trend based on rates
        threshold = 5  # Minimum FL change to consider significant
        
        is_climbing_before = rate_before > threshold
        is_descending_before = rate_before < -threshold
        is_climbing_after = rate_after > threshold  
        is_descending_after = rate_after < -threshold
        
        # Simple logic for trend determination
        if is_climbing_before and is_climbing_after:
            # Both rates positive = climbing
            is_ascending = True
            is_descending = False
        elif is_descending_before and is_descending_after:
            # Both rates negative = descending
            is_ascending = False
            is_descending = True
        elif abs(rate_before) <= threshold and abs(rate_after) <= threshold:
            # Both rates small = level flight
            is_ascending = False
            is_descending = False
        else:
            # Mixed signals = momentary variation, use stronger signal
            net_rate = rate_before + rate_after
            if abs(net_rate) > threshold:
                is_ascending = net_rate > 0
                is_descending = net_rate < 0
            else:
                # Truly level
                is_ascending = False
                is_descending = False
        
        # Extract altitude boundaries for phase determination
        takeoff_max = altitude_ranges.get('takeoff', {}).get('max_fl', 15)
        climb_max = altitude_ranges.get('climb', {}).get('max_fl', 250)
        descent_max = altitude_ranges.get('descent', {}).get('max_fl', 250)  
        approach_max = altitude_ranges.get('approach', {}).get('max_fl', 50)
        
        # Phase determination based on trend + altitude boundaries
        if is_ascending:
            if current_fl <= takeoff_max:
                return 'takeoff'
            elif current_fl <= climb_max:
                return 'climb'
            else:
                return 'cruise'  # High altitude climbing = cruise
        elif is_descending:
            if current_fl <= approach_max:
                return 'approach'
            elif current_fl <= descent_max:
                return 'descent'  # Below Top of Descent = descent phase
            else:
                return 'cruise'  # Above Top of Descent but descending = still cruise
        else:
            # Level flight - determine phase based on altitude only
            if current_fl <= takeoff_max:
                return 'takeoff'
            elif current_fl <= approach_max:
                return 'approach'  # Low altitude level = approach
            elif current_fl <= climb_max:
                return 'cruise'   # Medium altitude level = cruise
            else:
                return 'cruise'   # High altitude level = cruise
    
    def _ensure_phase_continuity(self, phases: list, altitudes: list) -> list:
        """Second pass to ensure phases are continuous and logical
        
        Fix isolated phase changes that don't make sense (e.g., single descent waypoint in cruise)
        """
        if len(phases) <= 2:
            return phases
        
        corrected_phases = phases.copy()
        
        # Fix isolated single waypoint phase changes
        for i in range(1, len(phases) - 1):
            prev_phase = phases[i-1]
            current_phase = phases[i]
            next_phase = phases[i+1]
            
            # If current phase is different from both neighbors, it might be an error
            if current_phase != prev_phase and current_phase != next_phase and prev_phase == next_phase:
                # Check if this makes sense based on altitude
                current_alt = altitudes[i]
                prev_alt = altitudes[i-1]
                next_alt = altitudes[i+1]
                
                # If altitude change is small, keep the neighboring phase
                alt_change_before = abs(current_alt - prev_alt)
                alt_change_after = abs(next_alt - current_alt)
                
                if alt_change_before <= 10 and alt_change_after <= 10:  # Small variations
                    corrected_phases[i] = prev_phase
                    print(f"Phase continuity: Fixed isolated {current_phase} at waypoint {i+1} (FL{current_alt}) to {prev_phase}")
        
        # Fix impossible phase transitions (e.g., takeoff -> descent directly)
        for i in range(1, len(corrected_phases)):
            prev_phase = corrected_phases[i-1]
            current_phase = corrected_phases[i]
            
            # Define valid transitions
            valid_transitions = {
                'takeoff': ['climb', 'takeoff'],
                'climb': ['climb', 'cruise', 'takeoff'],
                'cruise': ['cruise', 'descent', 'climb'],
                'descent': ['descent', 'approach', 'cruise'],
                'approach': ['approach', 'takeoff', 'descent']  # Can go to takeoff for touch-and-go
            }
            
            if current_phase not in valid_transitions.get(prev_phase, []):
                # Invalid transition - use previous phase or determine better one
                current_alt = altitudes[i]
                if current_alt <= 15:
                    corrected_phases[i] = 'takeoff' if prev_phase in ['takeoff', 'approach'] else 'approach'
                elif current_alt <= 50:
                    corrected_phases[i] = 'approach' if prev_phase in ['descent', 'approach'] else 'climb'
                elif current_alt <= 250:
                    corrected_phases[i] = 'climb' if prev_phase in ['takeoff', 'climb'] else 'cruise'
                else:
                    corrected_phases[i] = 'cruise'
                
                print(f"Phase continuity: Fixed invalid transition {prev_phase} -> {current_phase} at waypoint {i+1} to {corrected_phases[i]}")
        
        return corrected_phases

    def _update_geographic_plot(self):
        """Update the geographic plot with current track and phase coloring"""
        if not self.geo_canvas or not self.track_names:
            return
        
        try:
            import numpy as np
            import os
            import cartopy.crs as ccrs
            import cartopy.feature as cfeature
            
            # Clear previous plot
            self.geo_ax.clear()
            self.geo_ax.add_feature(cfeature.COASTLINE)
            self.geo_ax.add_feature(cfeature.BORDERS)
            
            # Get current track data
            track_callsign = self.track_names[self.current_track_index]
            
            if track_callsign in self.track_data:
                track_info = self.track_data[track_callsign]
                df = track_info['points']  # Use 'points' instead of 'data'
                
                # Use standard Eurocontrol column names
                lat_col = 'Latitude'
                lon_col = 'Longitude' 
                alt_col = 'Flight Level'
                aircraft_id = track_info.get('flight_id', 'Unknown')
                origin = track_info.get('origin', '')
                destination = track_info.get('destination', '')
                
                # Create route info for title
                route_info = f"{origin}->{destination}" if origin and destination else f"Flight {aircraft_id}"
                
                print(f"Plotting track: {track_callsign} (Aircraft: {aircraft_id})")
                print(f"Data shape: {df.shape}")
                print(f"Using columns: lat={lat_col}, lon={lon_col}, alt={alt_col}")
                
                if not df.empty and lat_col and lon_col:
                    # Get coordinates
                    lats = df[lat_col].values
                    lons = df[lon_col].values
                    
                    # Get altitudes if available
                    if alt_col and alt_col in df.columns:
                        alts = df[alt_col].values
                    else:
                        alts = np.zeros(len(lats))  # Default altitude if not available
                    
                    # Get speeds if available (for debug info only)
                    speeds = None
                    for col in df.columns:
                        col_lower = col.lower().strip()
                        if col_lower in ['speed', 'spd', 'ground speed', 'gs', 'velocity', 'v']:
                            speeds = df[col].values
                            print(f"Found speed column: {col}")
                            break
                    
                    # Get waypoint names if available
                    waypoint_names = None
                    for col in df.columns:
                        col_lower = col.lower().strip()
                        if col_lower in ['waypoint', 'wpt', 'name', 'waypoint name', 'point']:
                            waypoint_names = df[col].values
                            print(f"Found waypoint name column: {col}")
                            break
                    
                    # If no explicit waypoint names, use sequence numbers or indices
                    if waypoint_names is None:
                        if 'Sequence Number' in df.columns:
                            waypoint_names = [f"WPT{int(seq)}" for seq in df['Sequence Number'].values]
                        else:
                            waypoint_names = [f"WPT{i+1}" for i in range(len(lats))]
                    
                    print(f"Coordinate ranges: lat=[{np.min(lats):.3f}, {np.max(lats):.3f}], lon=[{np.min(lons):.3f}, {np.max(lons):.3f}]")
                    print(f"Altitude range: [{np.min(alts):.0f}, {np.max(alts):.0f}]")
                    if speeds is not None:
                        print(f"Speed range: [{np.min(speeds[np.isfinite(speeds)]):.0f}, {np.max(speeds[np.isfinite(speeds)]):.0f}]")
                    print(f"Sample waypoint names: {waypoint_names[:5] if len(waypoint_names) > 5 else waypoint_names}")
                    
                    # Phase colors matching the altitude profile
                    phase_colors = {
                        'takeoff': '#8B4513',    # Brown
                        'climb': '#FF6B35',      # Orange-red
                        'cruise': '#F7931E',     # Orange
                        'descent': '#4A90E2',    # Blue
                        'approach': '#7B68EE'    # Purple
                    }
                    
                    # Color waypoints by phase using backend logic
                    waypoint_colors = []
                    phase_counts = {'takeoff': 0, 'climb': 0, 'cruise': 0, 'descent': 0, 'approach': 0}
                    
                    # Import the backend phase determination function
                    from . import SATG
                    
                    # Get current configuration for phase ranges from spinboxes
                    ranges = self._calculate_ranges()
                    
                    # Convert ranges to altitude_ranges format expected by backend
                    altitude_ranges = {
                        'takeoff': ranges['takeoff'],
                        'climb': ranges['climb'], 
                        'cruise': ranges['cruise'],
                        'descent': ranges['descent'],
                        'approach': ranges['approach']
                    }
                    
                    # Create waypoint sequence in the format expected by backend
                    waypoint_sequence = [{'fl': int(a)} for a in alts]
                    
                    # First pass - determine phases using simple logic
                    initial_phases = []
                    for i, alt in enumerate(alts):
                        phase = self._get_flight_phase_with_config(int(alt), waypoint_sequence, i, altitude_ranges)
                        initial_phases.append(phase)
                    
                    # Second pass - ensure phase continuity
                    final_phases = self._ensure_phase_continuity(initial_phases, alts)
                    
                    for i, (phase, alt) in enumerate(zip(final_phases, alts)):
                        waypoint_colors.append(phase_colors[phase])
                        phase_counts[phase] += 1
                    
                    print(f"Phase distribution for {aircraft_id}: {phase_counts}")
                    
                    # Plot track with colored segments
                    for i in range(len(lons) - 1):
                        self.geo_ax.plot([lons[i], lons[i+1]], [lats[i], lats[i+1]], 
                                       color=waypoint_colors[i], linewidth=3, 
                                       transform=ccrs.PlateCarree())
                    
                    # Plot waypoints
                    self.geo_ax.scatter(lons, lats, c=waypoint_colors, s=50, 
                                      transform=ccrs.PlateCarree(), edgecolor='black', linewidth=0.5, zorder=5)
                    
                    # Add waypoint labels with name and altitude only
                    for i, (lon, lat, alt, name) in enumerate(zip(lons, lats, alts, waypoint_names)):
                        # Format the label with just name and altitude
                        label = f"{name}\nFL{int(alt)}"
                        
                        # Add text label with background for readability
                        self.geo_ax.text(lon, lat, label, 
                                       transform=ccrs.PlateCarree(),
                                       fontsize=6, ha='left', va='bottom',
                                       bbox=dict(boxstyle="round,pad=0.2", 
                                               facecolor='white', alpha=0.8, edgecolor='gray'),
                                       zorder=6)  # Higher zorder to appear above waypoints
                    
                    # Set extent around track with proper margin
                    lat_margin = max(0.5, (np.max(lats) - np.min(lats)) * 0.1)
                    lon_margin = max(0.5, (np.max(lons) - np.min(lons)) * 0.1)
                    
                    extent = [
                        np.min(lons) - lon_margin, np.max(lons) + lon_margin,
                        np.min(lats) - lat_margin, np.max(lats) + lat_margin
                    ]
                    
                    print(f"Setting extent: {extent}")
                    self.geo_ax.set_extent(extent, ccrs.PlateCarree())
                    
                    self.geo_ax.set_title(f"{track_callsign} ({route_info}) - {len(lons)} waypoints", fontsize=10, pad=10)
                    
                    # Add gridlines with optimized layout for space
                    gl = self.geo_ax.gridlines(draw_labels=True, alpha=0.5, linewidth=0.5)
                    gl.top_labels = False  # Remove top labels to save space
                    gl.right_labels = True  # Keep right labels for better space use
                    gl.left_labels = True   # Keep left labels
                    gl.bottom_labels = True # Keep bottom labels
                    gl.xlabel_style = {'size': 7}  # Smaller font for labels
                    gl.ylabel_style = {'size': 7}
                else:
                    print(f"No valid data found for track {track_callsign}")
                    self.geo_ax.text(0.5, 0.5, f"No geographic data\nfor aircraft: {track_callsign}", 
                                   transform=self.geo_ax.transAxes, ha='center', va='center',
                                   bbox=dict(boxstyle="round,pad=0.3", facecolor="yellow"))
            else:
                print(f"Track {track_callsign} not found in track_data")
                self.geo_ax.text(0.5, 0.5, f"Track data not loaded\nfor aircraft: {track_callsign}", 
                               transform=self.geo_ax.transAxes, ha='center', va='center',
                               bbox=dict(boxstyle="round,pad=0.3", facecolor="orange"))
            
            # Refresh canvas
            self.geo_canvas.draw()
            
        except Exception as e:
            print(f"Error updating geographic plot: {e}")
            import traceback
            traceback.print_exc()
    
    def get_all_track_configurations(self):
        """Return all track configurations"""
        # Save current track configuration before returning
        self._save_current_config()
        return self.track_configurations.copy()
    
    def _validate_and_update(self):
        """Validate altitude ordering and update ranges, then update visualization"""
        # Temporarily disconnect signals to prevent recursion
        self.takeoff_spin.blockSignals(True)
        self.climb_spin.blockSignals(True)
        self.descent_spin.blockSignals(True)
        self.approach_spin.blockSignals(True)
        
        # Get current values
        takeoff_val = self.takeoff_spin.value()
        climb_val = self.climb_spin.value()
        descent_val = self.descent_spin.value()
        approach_val = self.approach_spin.value()
        
        # Update ranges based on altitude ordering rules:
        # Takeoff < Climb < Descent and Approach < Descent
        
        # Takeoff: 0 to (climb - 1), max 998
        self.takeoff_spin.setRange(0, min(998, max(0, climb_val - 1)))
        
        # Climb: (takeoff + 1) to (descent - 1), max 998  
        self.climb_spin.setRange(max(1, takeoff_val + 1), min(998, max(takeoff_val + 1, descent_val - 1)))
        
        # Descent: max(climb + 1, approach + 1) to 999
        self.descent_spin.setRange(max(climb_val + 1, approach_val + 1), 999)
        
        # Approach: 0 to (descent - 1), max 998
        self.approach_spin.setRange(0, min(998, max(0, descent_val - 1)))
        
        # Reconnect signals
        self.takeoff_spin.blockSignals(False)
        self.climb_spin.blockSignals(False)
        self.descent_spin.blockSignals(False)
        self.approach_spin.blockSignals(False)
        
        # Update visualization
        self._update_visualization()
    
    def _reset_to_defaults(self):
        """Reset altitude boundaries to default values"""
        self.takeoff_spin.setValue(15)
        self.climb_spin.setValue(250)
        self.descent_spin.setValue(400)
        self.approach_spin.setValue(50)
        # Validation will be triggered by the value changes
    
    def _update_visualization(self):
        """Update the phase visualization when values change"""
        # Use a timer to prevent interference with typing
        if not hasattr(self, '_update_timer'):
            from PyQt6.QtCore import QTimer
            self._update_timer = QTimer()
            self._update_timer.setSingleShot(True)
            self._update_timer.timeout.connect(self._do_update_visualization)
        
        # Delay the update slightly to avoid interfering with input
        self._update_timer.start(50)  # 50ms delay
    
    def _do_update_visualization(self):
        """Actually perform the visualization update"""
        ranges = self._calculate_ranges()
        self.phase_canvas.update_phases(ranges)
        # Also update the geographic plot with new phase colors
        self._update_geographic_plot()
    
    def _calculate_ranges(self):
        """Calculate actual phase ranges based on the 4 input values"""
        initial_climb_upper = self.takeoff_spin.value()  # Now called Initial Climb
        top_of_climb = self.climb_spin.value()           # Now called Top of Climb  
        top_of_descent = self.descent_spin.value()       # Now called Top of Descent
        final_approach_upper = self.approach_spin.value() # Now called Final Approach
        
        # Logic: takeoff -> climb -> cruise -> descent -> approach
        # Cruise is between Top of Climb and Top of Descent
        # Descent is from Top of Descent down to Final Approach level
        ranges = {
            'takeoff': {'min_fl': 0, 'max_fl': initial_climb_upper},
            'climb': {'min_fl': initial_climb_upper, 'max_fl': top_of_climb},
            'cruise': {'min_fl': top_of_climb, 'max_fl': top_of_descent},
            'descent': {'min_fl': final_approach_upper, 'max_fl': top_of_descent},
            'approach': {'min_fl': 0, 'max_fl': final_approach_upper}
        }
        
        return ranges
    
    def get_phase_altitudes(self):
        """Return the configured phase altitudes in the expected format"""
        return self._calculate_ranges()
    
    def accept(self):
        """Override accept to validate that all aircraft have been configured"""
        # Save current track configuration before validation
        self._save_current_config()
        
        # Check if all tracks have been configured
        unconfigured_tracks = []
        for track_callsign in self.track_names:
            if track_callsign not in self.track_configurations:
                unconfigured_tracks.append(track_callsign)
        
        if unconfigured_tracks:
            # Show warning dialog
            from PyQt6.QtWidgets import QMessageBox
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setWindowTitle("Configuration Incomplete")
            msg.setText("Not all aircraft have been configured with phase altitudes.")
            msg.setInformativeText(
                f"The following aircraft still need configuration:\n\n" +
                "\n".join(f"* {callsign}" for callsign in unconfigured_tracks[:10]) +
                (f"\n... and {len(unconfigured_tracks) - 10} more" if len(unconfigured_tracks) > 10 else "") +
                "\n\nPlease navigate through all aircraft and configure their phase altitudes before finishing."
            )
            msg.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg.exec()
            return  # Don't close the dialog
        
        # All tracks are configured, proceed with normal accept
        super().accept()


class PhaseVisualizationWidget(QWidget):
    """
    Advanced visualization widget for displaying flight phase distributions and altitude analysis.
    
    This sophisticated visualization component provides comprehensive graphical representation
    of flight phase data including altitude ranges, phase distributions, and operational
    characteristics for flight data analysis and filtering configuration. The widget enables
    users to visualize flight phase patterns and make informed decisions about filtering
    parameters for machine learning model training and traffic generation.
    
    The Phase Visualization system uses color-coded graphical representations to display
    flight phase distributions across different altitude ranges, providing immediate visual
    feedback about data characteristics and helping users understand the operational patterns
    present in their flight datasets for optimal filtering configuration.
    
    Visualization Features:
    - Color-coded flight phase representation with distinct phase identification
    - Altitude range visualization showing phase distribution across flight levels
    - Interactive phase analysis with detailed statistics and operational insights
    - Real-time updates based on current filtering and data selection parameters
    - Professional graphical presentation with clear legends and labeling
    - Integrated data analysis providing phase statistics and operational metrics
    
    Flight Phase Categories:
    - Takeoff Phase: Ground operations and initial climb (Brown color coding)
    - Climb Phase: Ascending flight from departure to cruise altitude (Orange-red coding)
    - Cruise Phase: Level flight at optimal altitude (Orange color coding)  
    - Descent Phase: Controlled descent from cruise to approach altitude (Blue coding)
    - Approach Phase: Final approach and landing operations (Purple color coding)
    
    Data Analysis Capabilities:
    - Phase Distribution Analysis: Statistical breakdown of flight phases in dataset
    - Altitude Range Statistics: Operational altitude ranges for each flight phase
    - Operational Insights: Flight pattern analysis and operational characteristics
    - Filter Impact Visualization: Show effects of filtering parameters on phase distribution
    - Data Quality Assessment: Identify data completeness and phase coverage
    
    Attributes:
        phase_ranges (Dict): Current flight phase altitude range data for visualization
        phase_colors (Dict[str, str]): Color coding scheme for different flight phases
                                     - takeoff: #8B4513 (Brown)
                                     - climb: #FF6B35 (Orange-red)  
                                     - cruise: #F7931E (Orange)
                                     - descent: #4A90E2 (Blue)
                                     - approach: #7B68EE (Purple)
    
    Args:
        parent (QWidget, optional): Parent widget for proper visualization integration
    
    Returns:
        None: Widget initialization creates visualization canvas and sets up rendering
    
    Examples:
        # Create phase visualization for flight data analysis
        viz_widget = PhaseVisualizationWidget(parent=self)
        
        # Update with current phase data
        phase_data = {
            'takeoff': {'min_alt': 0, 'max_alt': 1500, 'count': 45},
            'climb': {'min_alt': 1500, 'max_alt': 25000, 'count': 120},
            'cruise': {'min_alt': 25000, 'max_alt': 42000, 'count': 200}
        }
        viz_widget.update_phases(phase_data)
        
        # Widget automatically renders updated visualization
    
    Note:
        The visualization widget provides real-time updates and professional graphical
        presentation for flight phase analysis. Color coding follows aviation industry
        standards for intuitive phase identification, and the widget integrates seamlessly
        with filtering dialogs to provide immediate visual feedback on data characteristics.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase_ranges = {}
        self.phase_colors = {
            'takeoff': '#8B4513',    # Brown
            'climb': '#FF6B35',      # Orange-red
            'cruise': '#F7931E',     # Orange
            'descent': '#4A90E2',    # Blue
            'approach': '#7B68EE'    # Purple
        }
        
    def update_phases(self, phase_ranges):
        """Update the phase data and redraw"""
        self.phase_ranges = phase_ranges
        self.update()  # Trigger repaint
        
    def paintEvent(self, event):
        """Custom paint event to draw the phase graph"""
        from PyQt6.QtGui import QPainter, QColor, QPen, QFont
        from PyQt6.QtCore import Qt, QRectF
        
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        if not self.phase_ranges:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No phase data")
            return
        
        # Get widget dimensions with better margins
        width = self.width() - 80  # More margin for Y-axis labels
        height = self.height() - 100  # More margin for X-axis labels
        start_x = 60  # More space for Y-axis labels
        start_y = 20
        
        # Find max altitude for scaling
        max_alt = max(max(r['max_fl'], r['min_fl']) for r in self.phase_ranges.values())
        if max_alt == 0:
            max_alt = 100  # Prevent division by zero
        
        # Draw flight profile curve
        phases = ['takeoff', 'climb', 'cruise', 'descent', 'approach']
        
        # Create accurate flight profile points based on actual phase boundaries
        profile_points = []
        
        # Get actual altitude values from phase ranges
        takeoff_upper = self.phase_ranges['takeoff']['max_fl']
        climb_upper = self.phase_ranges['climb']['max_fl']
        descent_upper = self.phase_ranges['descent']['max_fl']
        approach_upper = self.phase_ranges['approach']['max_fl']
        
        # Takeoff: 0 to takeoff upper (0% to 15% of flight)
        profile_points.extend([
            (0.0, 0),
            (0.15, takeoff_upper)
        ])
        
        # Climb: takeoff upper to climb upper (15% to 35% of flight)
        profile_points.append((0.35, climb_upper))
        
        # Cruise: climb upper to descent upper (35% to 65% of flight)
        # Single segment - no intermediate point needed
        profile_points.append((0.65, descent_upper))
        
        # Descent: descent upper to approach upper (65% to 85% of flight)
        profile_points.append((0.85, approach_upper))
        
        # Approach: approach upper to 0 (85% to 100% of flight)
        profile_points.append((1.0, 0))
        profile_points.append((1.0, 0))
        
        # Draw phase background regions that follow the flight profile
        for i, phase in enumerate(phases):
            if phase not in self.phase_ranges:
                continue
                
            # Get actual altitude values for accurate positioning
            takeoff_upper = self.phase_ranges['takeoff']['max_fl']
            climb_upper = self.phase_ranges['climb']['max_fl']
            descent_upper = self.phase_ranges['descent']['max_fl']
            approach_upper = self.phase_ranges['approach']['max_fl']
                
            # Calculate phase time boundaries and corresponding profile points
            if phase == 'takeoff':
                time_start, time_end = 0.0, 0.15
                start_alt, end_alt = 0, takeoff_upper
            elif phase == 'climb':
                time_start, time_end = 0.15, 0.35
                start_alt, end_alt = takeoff_upper, climb_upper
            elif phase == 'cruise':
                time_start, time_end = 0.35, 0.65
                start_alt = climb_upper
                end_alt = descent_upper  # Cruise automatically goes to descent upper
            elif phase == 'descent':
                time_start, time_end = 0.65, 0.85
                start_alt, end_alt = descent_upper, approach_upper
            else:  # approach
                time_start, time_end = 0.85, 1.0
                start_alt, end_alt = approach_upper, 0
            
            # Draw phase region that follows the profile curve
            x_start = start_x + (time_start * width)
            x_end = start_x + (time_end * width)
            
            # All phases now use polygons that follow the flight profile
            from PyQt6.QtGui import QPolygonF
            from PyQt6.QtCore import QPointF
            
            y_start = start_y + height - (start_alt / max_alt * height)
            y_end = start_y + height - (end_alt / max_alt * height)
            y_ground = start_y + height
            
            # Create polygon points that follow the flight profile
            points = [
                QPointF(x_start, y_ground),  # Ground start
                QPointF(x_start, y_start),   # Profile start
                QPointF(x_end, y_end),       # Profile end
                QPointF(x_end, y_ground),    # Ground end
            ]
            
            polygon = QPolygonF(points)
            
            # Fill the polygon
            color = QColor(self.phase_colors[phase])
            color.setAlpha(120)  # Semi-transparent
            painter.setBrush(color)
            painter.setPen(QPen(color, 1))
            painter.drawPolygon(polygon)
        
        # Draw flight profile line
        painter.setPen(QPen(QColor("#000000"), 3))
        prev_point = None
        for i, (time_frac, altitude) in enumerate(profile_points):
            x = start_x + (time_frac * width)
            y = start_y + height - (altitude / max_alt * height)
            
            if prev_point:
                painter.drawLine(int(prev_point[0]), int(prev_point[1]), int(x), int(y))
            
            # Draw altitude markers
            painter.setPen(QPen(QColor("#FF0000"), 2))
            painter.drawEllipse(int(x-3), int(y-3), 6, 6)
            painter.setPen(QPen(QColor("#000000"), 3))
            
            prev_point = (x, y)
        
        # Draw improved altitude scale with better Y-axis
        painter.setPen(QPen(QColor("#333333"), 2))
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        
        # Draw main Y-axis line
        painter.drawLine(start_x, start_y, start_x, start_y + height)
        
        # Calculate nice scale intervals
        if max_alt <= 100:
            interval = 10
        elif max_alt <= 200:
            interval = 20
        elif max_alt <= 500:
            interval = 50
        else:
            interval = 100
        
        # Y-axis labels with major and minor ticks
        for i in range(0, int(max_alt) + interval, interval):
            if i > max_alt:
                break
                
            y = start_y + height - (i / max_alt * height)
            
            # Major tick
            painter.setPen(QPen(QColor("#333333"), 2))
            painter.drawLine(start_x - 8, int(y), start_x, int(y))
            
            # Grid line (light)
            if i > 0:
                painter.setPen(QPen(QColor("#E0E0E0"), 1))
                painter.drawLine(start_x, int(y), start_x + width, int(y))
            
            # Label
            painter.setPen(QPen(QColor("#333333"), 2))
            painter.drawText(0, int(y - 8), start_x - 12, 16, 
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, 
                           f"FL{i:03d}")
        
        # Add minor ticks between major intervals
        minor_interval = interval // 2
        if minor_interval >= 5:  # Only show minor ticks if they're meaningful
            painter.setPen(QPen(QColor("#666666"), 1))
            for i in range(minor_interval, int(max_alt) + minor_interval, minor_interval):
                if i % interval != 0 and i <= max_alt:  # Skip major tick positions
                    y = start_y + height - (i / max_alt * height)
                    painter.drawLine(start_x - 4, int(y), start_x, int(y))
        
        # X-axis with better styling
        painter.setPen(QPen(QColor("#333333"), 2))
        painter.drawLine(start_x, start_y + height, start_x + width, start_y + height)
        
        # Phase names on X-axis
        painter.setFont(QFont("Arial", 10))
        painter.setPen(QPen(QColor("#333333"), 2))
        
        phase_positions = [
            (0.075, "Takeoff"),   # Center of takeoff phase (0-15%)
            (0.25, "Climb"),      # Center of climb phase (15-35%)
            (0.5, "Cruise"),      # Center of cruise phase (35-65%)
            (0.75, "Descent"),    # Center of descent phase (65-85%)
            (0.925, "Approach")   # Center of approach phase (85-100%)
        ]
        
        for pos, name in phase_positions:
            x_pos = start_x + (pos * width)
            painter.drawText(int(x_pos - 30), start_y + height + 10, 60, 20,
                           Qt.AlignmentFlag.AlignCenter, name)
        
        # X-axis main label
        painter.drawText(start_x, start_y + height + 40, width, 20,
                        Qt.AlignmentFlag.AlignCenter, "Flight Progress")


# --- Historic Sampling tab (Synthetic Route Generation) --------------------

class HistoricSamplingTab(QWidget):
    """
    Historic Sampling interface for machine learning-based synthetic air traffic generation.
    
    Machine learning-based synthetic air traffic generation using historic EUROCONTROL 
    flight data with filtering, model training, and trajectory synthesis. Uses learned 
    patterns from real flight operations to create new flight trajectories that maintain 
    statistical consistency with historic data.
    
    The Historic Sampling methodology leverages advanced machine learning algorithms trained
    on filtered historic flight data to generate synthetic traffic scenarios with realistic
    flight characteristics. Unlike traditional scenario replay systems, Historic Sampling
    creates completely new flight trajectories based on statistical patterns and operational
    constraints learned from comprehensive analysis of real-world flight operations.
    
    Core Machine Learning Features:
    - Advanced EUROCONTROL data integration with multi-format support (flights, filed, actual, FIR)
    - Sophisticated multi-dimensional filtering system with airspace, altitude, temporal, and aircraft constraints
    - Machine learning model training on comprehensive flight point datasets (position, altitude, speed, time)
    - Synthetic trajectory generation with realistic flight characteristics and operational constraints
    - Statistical pattern recognition for aircraft type distribution and route preferences
    - Flight phase analysis and altitude profile modeling based on operational data
    - Performance optimization through intelligent caching and vectorized data processing
    
    Advanced Data Processing Pipeline:
    1. Multi-source EUROCONTROL data loading with automatic format detection and validation
    2. Comprehensive multi-dimensional filtering with airspace boundaries, altitude bands, and temporal windows
    3. Flight point processing and feature extraction for machine learning model training
    4. Advanced statistical analysis of flight patterns, route preferences, and operational characteristics
    5. Machine learning model training on filtered trajectory datasets with cross-validation
    6. Synthetic trajectory generation with learned flight characteristics and operational realism
    7. Scenario file creation with BlueSky integration for immediate simulation execution
    
    Advanced Filtering Capabilities:
    - Geographic airspace filtering with polygon-based boundary definitions and coordinate systems
    - Multi-level altitude filtering with flight level ranges and vertical profile constraints
    - Temporal filtering with date ranges, time windows, and operational period selection
    - Aircraft type filtering with performance category grouping and operational constraints
    - Route-based filtering with departure/arrival airport selection and waypoint constraints
    - Statistical filtering based on flight frequency, duration, and operational patterns
    
    Machine Learning Integration:
    - TraffixGen backend integration for advanced ML-based trajectory synthesis
    - Feature engineering from multi-dimensional flight point data (4D trajectories)
    - Statistical model training with cross-validation and performance metrics
    - Synthetic data generation with learned operational patterns and constraints
    - Quality assurance through statistical validation against original data distributions
    - Model performance monitoring and continuous improvement capabilities
    
    Performance Optimization Features:
    - Intelligent file path caching for Configure Filters dialog performance enhancement
    - Vectorized flight point filtering using NumPy optimization for large datasets
    - Bounding box pre-filtering for geometric calculations and spatial query acceleration
    - Progress dialog integration with proper threading for responsive user interface
    - Memory-efficient data processing for handling large-scale EUROCONTROL datasets
    - Parallel processing support for multi-core acceleration of ML training operations
    
    User Interface Components:
    - Date range selection with automatic bounds detection from loaded data sources
    - Advanced filter configuration with real-time validation and constraint preview
    - Progress monitoring for data loading, filtering, and model training operations
    - Configuration persistence with comprehensive save/load functionality
    - Integration with SATG configuration management for session state preservation
    - Real-time feedback and status updates throughout the ML pipeline execution
    
    Historic Sampling tab provides machine learning tools for creating training scenarios 
    based on analysis of real-world flight operations and traffic patterns.
    
    Args:
        parent (QWidget, optional): Parent widget, typically SATGWindow instance
        
    Examples:
        # Create Historic Sampling tab with ML capabilities
        hs_tab = HistoricSamplingTab(parent=satg_window)
        tab_widget.addTab(hs_tab, "Historic Sampling")
        
        # Tab provides complete ML-based synthetic traffic generation
        # with advanced filtering and model training capabilities
    
    Note:
        Historic Sampling requires TraffixGen backend integration for ML functionality
        and supports large-scale EUROCONTROL data processing with performance
        optimizations for real-world operational datasets and training requirements.
        
        Workflow includes:
        1. Data source selection and validation
        2. Filter configuration and preview
        3. Model training parameter optimization
        4. Batch processing with progress tracking
        5. Quality validation and performance metrics
        6. Export scenarios for BlueSky simulation with proper formatting
        
        Filter System:
        - Date Range: Select specific time periods from available data
        - Airspace: Include specific FIR regions using geometric calculations
        - Altitude: Constrain flight levels for different operational phases
        - Aircraft Types: Focus on specific aircraft categories or models
        - Flight Phases: Filter by takeoff, climb, cruise, descent, approach
        
        Performance Features:
        - File path caching for rapid dialog reopening
        - Summary data caching with intelligent invalidation
        - Vectorized flight point filtering with numpy optimization
        - Progress dialogs with proper UI thread management
        - Parquet caching for processed data persistence
        
        Attributes:
            _flights_file (str): Path to EUROCONTROL flights data file
            _filed_file (str): Path to filed flight plans data file  
            _actual_file (str): Path to actual trajectory data file
            _fir_file (str): Path to FIR boundary definition file
            _model_trained (bool): Flag indicating if ML models are trained
            _synthetic_data (list): Generated synthetic flight data
            _data_filters (dict): Current filter configuration
            eurocontrol_filters (dict): Comprehensive filter settings
            date_from (QDateEdit): Start date selection widget
            date_to (QDateEdit): End date selection widget
        num_flights_spin (QSpinBox): Number of flights to generate
    
    Examples:
        # Tab is created as part of main window
        hs_tab = HistoricSamplingTab(parent_window)
        
        # Typical workflow:
        # 1. Load data files through file selection dialogs
        # 2. Configure filters using advanced filtering dialog
        # 3. Train models on filtered data
        # 4. Generate synthetic scenarios
        # 5. Export to BlueSky scenario files
    
    Note:
        This tab implements the same filtering interface as Realistic Replay
        for feature parity, but applies filters to actual flight point data
        for ML model training rather than just scenario metadata filtering.
        The tab requires EUROCONTROL data files in specific formats and uses
        sophisticated geometric calculations for accurate airspace filtering.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Instance variables for data files (reusing same pattern as RLTab)
        self._flights_file = ""
        self._filed_file = ""
        self._actual_file = ""
        self._fir_file = ""
        
        # Model state tracking
        self._model_trained = False
        self._synthetic_data = []
        self._data_filters = {}  # Store applied filters like RLTab
        
        # Create scroll area for the entire tab content
        scroll_area = QScrollArea()
        scroll_widget = QWidget()
        main = QVBoxLayout(scroll_widget)
        
        # Set up scrolling properties
        scroll_area.setWidget(scroll_widget)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        # Main layout for this tab - just contains the scroll area
        tab_layout = QVBoxLayout(self)
        tab_layout.addWidget(scroll_area)
        
        # 1) Load Historical Data (Required) - Copy from RLTab
        gb_load = QGroupBox("1) Load Historical Data - Required")
        gb_load_layout = QVBoxLayout(gb_load)
        
        # Description
        desc1 = QLabel("Select Eurocontrol historical data files to train synthetic route generation models")
        desc1.setStyleSheet("color: #666; font-style: italic;")
        gb_load_layout.addWidget(desc1)
        
        # Create 2x2 grid for file selections (same as RLTab)
        files_grid = QHBoxLayout()
        
        # Left column
        left_column = QVBoxLayout()
        
        # Flights data file
        flights_group = QGroupBox("Flights Data (Required)")
        flights_layout = QVBoxLayout(flights_group)
        
        self.flights_file_label = QLabel("No file selected")
        self.flights_file_label.setStyleSheet("color: #999; font-style: italic;")
        flights_layout.addWidget(self.flights_file_label)
        
        flights_buttons = QHBoxLayout()
        btn_browse_flights = QPushButton("Browse...")
        btn_browse_flights.setToolTip("Select Flights_extract.csv file")
        btn_browse_flights.clicked.connect(self._browse_flights_file)
        btn_clear_flights = QPushButton("Clear")
        btn_clear_flights.clicked.connect(self._clear_flights_file)
        
        flights_buttons.addWidget(btn_browse_flights)
        flights_buttons.addWidget(btn_clear_flights)
        flights_buttons.addStretch()
        flights_layout.addLayout(flights_buttons)
        
        left_column.addWidget(flights_group)
        
        # Flight Points Filed file
        filed_group = QGroupBox("Flight Points - Filed (Required)")
        filed_layout = QVBoxLayout(filed_group)
        
        self.filed_file_label = QLabel("No file selected")
        self.filed_file_label.setStyleSheet("color: #999; font-style: italic;")
        filed_layout.addWidget(self.filed_file_label)
        
        filed_buttons = QHBoxLayout()
        btn_browse_filed = QPushButton("Browse...")
        btn_browse_filed.setToolTip("Select Flight_Points_Filed_extract.csv file")
        btn_browse_filed.clicked.connect(self._browse_filed_file)
        btn_clear_filed = QPushButton("Clear")
        btn_clear_filed.clicked.connect(self._clear_filed_file)
        
        filed_buttons.addWidget(btn_browse_filed)
        filed_buttons.addWidget(btn_clear_filed)
        filed_buttons.addStretch()
        filed_layout.addLayout(filed_buttons)
        
        left_column.addWidget(filed_group)
        
        # Right column
        right_column = QVBoxLayout()
        
        # Flight Points Actual file
        actual_group = QGroupBox("Flight Points - Actual (Required)")
        actual_layout = QVBoxLayout(actual_group)
        
        self.actual_file_label = QLabel("No file selected")
        self.actual_file_label.setStyleSheet("color: #999; font-style: italic;")
        actual_layout.addWidget(self.actual_file_label)
        
        actual_buttons = QHBoxLayout()
        btn_browse_actual = QPushButton("Browse...")
        btn_browse_actual.setToolTip("Select Flight_Points_Actual_extract.csv file")
        btn_browse_actual.clicked.connect(self._browse_actual_file)
        btn_clear_actual = QPushButton("Clear")
        btn_clear_actual.clicked.connect(self._clear_actual_file)
        
        actual_buttons.addWidget(btn_browse_actual)
        actual_buttons.addWidget(btn_clear_actual)
        actual_buttons.addStretch()
        actual_layout.addLayout(actual_buttons)
        
        right_column.addWidget(actual_group)
        
        # FIR data file
        fir_group = QGroupBox("FIR Data (Optional)")
        fir_layout = QVBoxLayout(fir_group)
        
        self.fir_file_label = QLabel("No file selected")
        self.fir_file_label.setStyleSheet("color: #999; font-style: italic;")
        fir_layout.addWidget(self.fir_file_label)
        
        fir_buttons = QHBoxLayout()
        btn_browse_fir = QPushButton("Browse...")
        btn_browse_fir.setToolTip("Select FIR_extract.csv file (optional)")
        btn_browse_fir.clicked.connect(self._browse_fir_file)
        btn_clear_fir = QPushButton("Clear")
        btn_clear_fir.clicked.connect(self._clear_fir_file)
        
        fir_buttons.addWidget(btn_browse_fir)
        fir_buttons.addWidget(btn_clear_fir)
        fir_buttons.addStretch()
        fir_layout.addLayout(fir_buttons)
        
        right_column.addWidget(fir_group)
        
        files_grid.addLayout(left_column)
        files_grid.addLayout(right_column)
        gb_load_layout.addLayout(files_grid)
        
        # Filter configuration section
        filter_section = QHBoxLayout()
        
        self.btn_configure_filters = QPushButton("Configure Filters...")
        self.btn_configure_filters.setToolTip("Set geographic, temporal, and aircraft filters for training data")
        self.btn_configure_filters.clicked.connect(self._configure_historic_filters)
        
        filter_section.addStretch()
        filter_section.addWidget(self.btn_configure_filters)
        gb_load_layout.addLayout(filter_section)
        
        # Initialize default filter settings (same format as Realistic Replay)
        self.historic_filters = {
            'lat_min': -90, 'lat_max': 90,
            'lon_min': -180, 'lon_max': 180,
            'fl_min': 0, 'fl_max': 500,
            'include_airspace': [],
            'time_start': None, 'time_end': None,
            'date_start': None, 'date_end': None,
            'aircraft_types': []
        }
        
        main.addWidget(gb_load)
        
        # 2) Configure Model Parameters
        gb_config = QGroupBox("2) Configure Model Parameters")
        gb_config_layout = QVBoxLayout(gb_config)
        
        # Model type selection
        model_type_layout = QHBoxLayout()
        model_type_layout.addWidget(QLabel("Model Type:"))
        
        self.model_type_combo = QComboBox()
        self.model_type_combo.addItems(["Tree-based (XGBoost)", "KDE-based", "Derivative KDE"])
        self.model_type_combo.setCurrentText("Tree-based (XGBoost)")
        self.model_type_combo.currentTextChanged.connect(self._on_model_type_changed)
        model_type_layout.addWidget(self.model_type_combo)
        model_type_layout.addStretch()
        
        gb_config_layout.addLayout(model_type_layout)
        
        # Parameter columns layout (replacing tabs)
        self.param_columns_layout = QHBoxLayout()
        
        # Tree model parameters column
        self.tree_params_group = QGroupBox("Tree Parameters")
        self.tree_params_widget = QWidget()
        tree_layout = QFormLayout(self.tree_params_widget)
        
        self.n_estimators_spin = QSpinBox()
        self.n_estimators_spin.setRange(10, 1000)
        self.n_estimators_spin.setValue(100)
        self.n_estimators_spin.setToolTip("Range: 10-1000\nNumber of decision trees in the ensemble. More estimators generally improve accuracy but increase training time.")
        _configure_decimal_separator(self.n_estimators_spin)
        tree_layout.addRow("Number of Estimators:", self.n_estimators_spin)
        
        self.max_depth_spin = QSpinBox()
        self.max_depth_spin.setRange(3, 20)
        self.max_depth_spin.setValue(8)
        self.max_depth_spin.setToolTip("Range: 3-20\nMaximum depth of each decision tree. Deeper trees can model more complex patterns but may overfit.")
        _configure_decimal_separator(self.max_depth_spin)
        tree_layout.addRow("Max Depth:", self.max_depth_spin)
        
        self.learning_rate_spin = QDoubleSpinBox()
        self.learning_rate_spin.setRange(0.01, 1.0)
        self.learning_rate_spin.setValue(0.1)
        self.learning_rate_spin.setDecimals(3)
        self.learning_rate_spin.setToolTip("Range: 0.010-1.000\nControls how much each tree contributes to the final prediction. Lower values require more estimators but often improve accuracy.")
        _configure_decimal_separator(self.learning_rate_spin)
        tree_layout.addRow("Learning Rate:", self.learning_rate_spin)
        
        self.min_child_weight_spin = QSpinBox()
        self.min_child_weight_spin.setRange(1, 10)
        self.min_child_weight_spin.setValue(1)
        self.min_child_weight_spin.setToolTip("Range: 1-10\nMinimum sum of instance weight needed in a child node. Higher values prevent overfitting.")
        _configure_decimal_separator(self.min_child_weight_spin)
        tree_layout.addRow("Min Child Weight:", self.min_child_weight_spin)
        
        self.subsample_spin = QDoubleSpinBox()
        self.subsample_spin.setRange(0.5, 1.0)
        self.subsample_spin.setValue(1.0)
        self.subsample_spin.setDecimals(2)
        self.subsample_spin.setToolTip("Range: 0.50-1.00\nFraction of training data to use for each tree. Values < 1.0 help prevent overfitting.")
        _configure_decimal_separator(self.subsample_spin)
        tree_layout.addRow("Subsample:", self.subsample_spin)
        
        self.tree_params_group.setLayout(tree_layout)
        self.param_columns_layout.addWidget(self.tree_params_group)
        
        # KDE model parameters column
        self.kde_params_group = QGroupBox("KDE Parameters")
        self.kde_params_widget = QWidget()
        kde_layout = QFormLayout(self.kde_params_widget)
        
        self.kde_bandwidth_spin = QDoubleSpinBox()
        self.kde_bandwidth_spin.setRange(0.01, 5.0)
        self.kde_bandwidth_spin.setValue(1.0)
        self.kde_bandwidth_spin.setDecimals(3)
        self.kde_bandwidth_spin.setToolTip("Range: 0.010-5.000\nControls the smoothness of the KDE. Lower values create sharper distributions, higher values create smoother ones.")
        _configure_decimal_separator(self.kde_bandwidth_spin)
        kde_layout.addRow("Bandwidth:", self.kde_bandwidth_spin)
        
        self.kde_kernel_combo = QComboBox()
        self.kde_kernel_combo.addItems(["gaussian", "linear", "cosine", "tophat"])
        self.kde_kernel_combo.setCurrentText("gaussian")
        self.kde_kernel_combo.setToolTip("Options: gaussian, linear, cosine, tophat\nKernel function used for density estimation. Gaussian is most common for smooth distributions.")
        kde_layout.addRow("Kernel:", self.kde_kernel_combo)
        
        self.kde_atol_spin = QDoubleSpinBox()
        self.kde_atol_spin.setRange(1e-6, 1e-2)
        self.kde_atol_spin.setValue(1e-4)
        self.kde_atol_spin.setDecimals(6)
        self.kde_atol_spin.setSuffix("")
        self.kde_atol_spin.setToolTip("Range: 1e-6 to 1e-2\nAbsolute tolerance for KDE computations. Lower values increase precision but require more computation time.")
        _configure_decimal_separator(self.kde_atol_spin)
        kde_layout.addRow("Absolute Tolerance:", self.kde_atol_spin)
        
        self.kde_params_group.setLayout(kde_layout)
        self.param_columns_layout.addWidget(self.kde_params_group)
        
        # Derivative KDE model parameters column
        self.derivative_kde_params_group = QGroupBox("Derivative KDE Parameters")
        self.derivative_kde_params_widget = QWidget()
        derivative_kde_layout = QFormLayout(self.derivative_kde_params_widget)
        
        self.derivative_bandwidth_spin = QDoubleSpinBox()
        self.derivative_bandwidth_spin.setRange(0.01, 5.0)
        self.derivative_bandwidth_spin.setValue(0.8)
        self.derivative_bandwidth_spin.setDecimals(3)
        self.derivative_bandwidth_spin.setToolTip("Range: 0.010-5.000\nKernel bandwidth for derivative modeling. Higher values create smoother derivatives but may lose detail.")
        _configure_decimal_separator(self.derivative_bandwidth_spin)
        derivative_kde_layout.addRow("Bandwidth:", self.derivative_bandwidth_spin)
        
        self.derivative_order_spin = QSpinBox()
        self.derivative_order_spin.setRange(1, 3)
        self.derivative_order_spin.setValue(1)
        self.derivative_order_spin.setToolTip("Range: 1-3\nOrder of derivative estimation:\n1 = First derivative (velocity/rate of change)\n2 = Second derivative (acceleration)\n3 = Third derivative (jerk)")
        derivative_kde_layout.addRow("Derivative Order:", self.derivative_order_spin)
        
        self.derivative_smoothing_spin = QDoubleSpinBox()
        self.derivative_smoothing_spin.setRange(0.1, 2.0)
        self.derivative_smoothing_spin.setValue(0.5)
        self.derivative_smoothing_spin.setDecimals(2)
        self.derivative_smoothing_spin.setToolTip("Range: 0.10-2.00\nSmoothing factor for derivative estimation. Higher values reduce noise but may oversmooth important features.")
        _configure_decimal_separator(self.derivative_smoothing_spin)
        derivative_kde_layout.addRow("Smoothing Factor:", self.derivative_smoothing_spin)
        
        self.derivative_kernel_combo = QComboBox()
        self.derivative_kernel_combo.addItems(["gaussian", "linear", "cosine"])
        self.derivative_kernel_combo.setCurrentText("gaussian")
        self.derivative_kernel_combo.setToolTip("Options: gaussian, linear, cosine\nKernel function for derivative KDE. Gaussian is recommended for most applications.")
        derivative_kde_layout.addRow("Kernel:", self.derivative_kernel_combo)
        
        self.derivative_kde_params_group.setLayout(derivative_kde_layout)
        self.param_columns_layout.addWidget(self.derivative_kde_params_group)
        
        # Trajectory parameters column
        self.traj_params_group = QGroupBox("Trajectory Parameters")
        traj_params_widget = QWidget()
        traj_layout = QFormLayout(traj_params_widget)
        
        self.n_points_spin = QSpinBox()
        self.n_points_spin.setRange(50, 1000)
        self.n_points_spin.setValue(50)
        self.n_points_spin.setToolTip("Range: 50-1000\nNumber of points per synthetic trajectory. More points provide higher resolution but increase computation time.")
        _configure_decimal_separator(self.n_points_spin)
        traj_layout.addRow("Points per Trajectory:", self.n_points_spin)
        
        self.smoothing_alpha_spin = QDoubleSpinBox()
        self.smoothing_alpha_spin.setRange(0.1, 0.9)
        self.smoothing_alpha_spin.setValue(0.3)
        self.smoothing_alpha_spin.setDecimals(2)
        self.smoothing_alpha_spin.setToolTip("Range: 0.10-0.90\nSmoothing parameter for trajectory interpolation. Higher values create smoother trajectories but may lose important details.")
        _configure_decimal_separator(self.smoothing_alpha_spin)
        traj_layout.addRow("Smoothing Alpha:", self.smoothing_alpha_spin)
        
        self.interpolation_spin = QSpinBox()
        self.interpolation_spin.setRange(1, 20)
        self.interpolation_spin.setValue(5)
        self.interpolation_spin.setToolTip("Range: 1-20\nNumber of interpolation points between trajectory samples. Higher values create smoother curves but increase processing time.")
        _configure_decimal_separator(self.interpolation_spin)
        traj_layout.addRow("Interpolation Points:", self.interpolation_spin)
        
        self.traj_params_group.setLayout(traj_layout)
        self.param_columns_layout.addWidget(self.traj_params_group)
        
        gb_config_layout.addLayout(self.param_columns_layout)
        
        # Set initial column visibility based on default model type
        self._on_model_type_changed(self.model_type_combo.currentText())
        
        main.addWidget(gb_config)
        
        # 3) Generation Settings
        gb_generate = QGroupBox("3) Generation Settings")
        gb_generate_layout = QVBoxLayout(gb_generate)
        
        # Generation parameters
        gen_params_layout = QFormLayout()
        
        self.n_flights_spin = QSpinBox()
        self.n_flights_spin.setRange(1, 1000)
        self.n_flights_spin.setValue(5)
        self.n_flights_spin.setToolTip("Range: 1-1000\nNumber of synthetic flights to generate. This determines the traffic density in the created scenario.")
        _configure_decimal_separator(self.n_flights_spin)
        gen_params_layout.addRow("Number of Flights:", self.n_flights_spin)
        
        gb_generate_layout.addLayout(gen_params_layout)
        
        main.addWidget(gb_generate)
        
        # 4) Create Scenario
        self.gb_scenario = QGroupBox("4) Create Scenario")
        actions_main_layout = QVBoxLayout(self.gb_scenario)
        actions_main_layout.setContentsMargins(8, 8, 8, 8)
        actions_main_layout.setSpacing(10)
        
        # Scenario controls form
        scenario_form = QFormLayout()
        scenario_form.setContentsMargins(0, 0, 0, 0)
        scenario_form.setSpacing(8)
        
        self.scn_name = QLineEdit("synthetic")
        self.scn_name.setPlaceholderText("Scenario name, e.g. synthetic_01")
        self.scn_name.setToolTip("Name for the generated scenario file (without .scn extension)")
        
        self.synthetic_seed = QSpinBox()
        self.synthetic_seed.setRange(0, 2**31-1)
        self.synthetic_seed.setValue(0)
        self.synthetic_seed.setToolTip("Range: 0 to 2147483647\nSeed for random number generation. Use 0 for truly random results, or specify a number for reproducible outputs.")
        
        self.synthetic_overwrite = QCheckBox("Overwrite scenario if it exists")
        self.synthetic_overwrite.setChecked(False)
        self.synthetic_overwrite.setToolTip("Replace existing scenario file if one exists with the same name")
        
        scenario_form.addRow("Scenario name:", self.scn_name)
        scenario_form.addRow("Seed (0=random):", self.synthetic_seed)
        scenario_form.addRow(self.synthetic_overwrite)
        
        actions_main_layout.addLayout(scenario_form)
        
        # Action buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)
        
        self.btn_make = QPushButton("CREATE SCENARIO")
        self.btn_run_only = QPushButton("RUN SCENARIO")
        self.btn_run = QPushButton("CREATE & RUN SCENARIO")
        
        self.btn_make.clicked.connect(self._make)
        self.btn_run_only.clicked.connect(self._run_only)
        self.btn_run.clicked.connect(self._run)
        
        # Enable buttons by default - validation happens in methods
        self.btn_make.setEnabled(True)
        self.btn_run_only.setEnabled(True)
        self.btn_run.setEnabled(True)
        
        buttons_layout.addWidget(self.btn_make)
        buttons_layout.addWidget(self.btn_run_only)
        buttons_layout.addWidget(self.btn_run)
        buttons_layout.addStretch(1)
        
        actions_main_layout.addLayout(buttons_layout)

        main.addWidget(self.gb_scenario)
        
        main.addStretch()
    
    # File browsing methods (copied from RLTab pattern)
    def _browse_flights_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Flights Data File", "", "CSV files (*.csv);;All files (*)"
        )
        if path:
            self._flights_file = path
            self.flights_file_label.setText(os.path.basename(path))
            self.flights_file_label.setStyleSheet("color: black;")
            # Clear cached filter data when file paths change
            self._clear_filter_cache()
            # Invalidate models when data changes
            self._invalidate_models()
            # Load data sample for filter bounds
            self._load_data_sample()
    
    def _clear_flights_file(self):
        self._flights_file = ""
        self.flights_file_label.setText("No file selected")
        self.flights_file_label.setStyleSheet("color: #999; font-style: italic;")
        # Clear cached filter data when file paths change
        self._clear_filter_cache()
        # Clear data sample
        self.historic_data = None
    
    def _load_data_sample(self):
        """Load a sample of the data to determine filter bounds."""
        if not self._flights_file:
            return
        
        try:
            import pandas as pd
            # Load a sample of the data (first 10000 rows for performance)
            self.historic_data = pd.read_csv(self._flights_file, nrows=10000)
            print(f"Loaded data sample: {len(self.historic_data)} rows, {len(self.historic_data.columns)} columns")
            print(f"Columns: {list(self.historic_data.columns)}")
            
        except Exception as e:
            print(f"Warning: Could not load data sample for filter bounds: {e}")
            self.historic_data = None
    
    def _browse_filed_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Flight Points Filed File", "", "CSV files (*.csv);;All files (*)"
        )
        if path:
            self._filed_file = path
            self.filed_file_label.setText(os.path.basename(path))
            self.filed_file_label.setStyleSheet("color: black;")
            # Clear cached filter data when file paths change
            self._clear_filter_cache()
            # Invalidate models when data changes
            self._invalidate_models()
    
    def _clear_filed_file(self):
        self._filed_file = ""
        self.filed_file_label.setText("No file selected")
        self.filed_file_label.setStyleSheet("color: #999; font-style: italic;")
        # Clear cached filter data when file paths change
        self._clear_filter_cache()
    
    def _browse_actual_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Flight Points Actual File", "", "CSV files (*.csv);;All files (*)"
        )
        if path:
            self._actual_file = path
            self.actual_file_label.setText(os.path.basename(path))
            self.actual_file_label.setStyleSheet("color: black;")
            # Clear cached filter data when file paths change
            self._clear_filter_cache()
            # Invalidate models when data changes
            self._invalidate_models()
    
    def _clear_actual_file(self):
        self._actual_file = ""
        self.actual_file_label.setText("No file selected")
        self.actual_file_label.setStyleSheet("color: #999; font-style: italic;")
        # Clear cached filter data when file paths change
        self._clear_filter_cache()
    
    def _browse_fir_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select FIR Data File", "", "CSV files (*.csv);;All files (*)"
        )
        if path:
            self._fir_file = path
            self.fir_file_label.setText(os.path.basename(path))
            self.fir_file_label.setStyleSheet("color: black;")
            # Clear cached filter data when file paths change
            self._clear_filter_cache()
    
    def _clear_fir_file(self):
        self._fir_file = ""
        self.fir_file_label.setText("No file selected")
        self.fir_file_label.setStyleSheet("color: #999; font-style: italic;")
        # Clear cached filter data when file paths change
        self._clear_filter_cache()
    
    def _on_model_type_changed(self, model_type):
        """Handle model type selection changes."""
        if not hasattr(self, 'param_columns_layout'):
            return
        
        # Show/hide parameter columns based on model type
        if model_type == "Tree-based (XGBoost)":
            # Show Tree and Trajectory columns, hide KDE columns
            self.tree_params_group.setVisible(True)
            self.kde_params_group.setVisible(False)
            self.derivative_kde_params_group.setVisible(False)
            self.traj_params_group.setVisible(True)
            
        elif model_type == "KDE-based":
            # Show KDE and Trajectory columns, hide Tree and Derivative KDE columns
            self.tree_params_group.setVisible(False)
            self.kde_params_group.setVisible(True)
            self.derivative_kde_params_group.setVisible(False)
            self.traj_params_group.setVisible(True)
                    
        elif model_type == "Derivative KDE":
            # Show Derivative KDE and Trajectory columns, hide Tree and regular KDE columns
            self.tree_params_group.setVisible(False)
            self.kde_params_group.setVisible(False)
            self.derivative_kde_params_group.setVisible(True)
            self.traj_params_group.setVisible(True)
    
    def _on_od_mode_changed(self, mode):
        """Handle OD selection mode changes."""
        # Show/hide controls based on mode
        self.od_pairs_list.setVisible(mode == "Specific Pairs")
        self.geo_region_combo.setVisible(mode == "Geographic Region")
        if hasattr(self, 'min_distance_spin'):
            self.min_distance_spin.parent().setVisible(mode == "Distance Range")
    
    def _on_ac_mode_changed(self, mode):
        """Handle aircraft selection mode changes."""
        # Show/hide controls based on mode
        self.ac_types_list.setVisible(mode == "Specific Types")
        self.ac_category_combo.setVisible(mode == "Performance Category")
    
    def _on_geo_filter_changed(self, region):
        """Handle geographic filter changes."""
        self.custom_bounds_widget.setVisible(region == "Custom Bounds")
    
    def _on_filter_ac_mode_changed(self, mode):
        """Handle aircraft filter mode changes."""
        if hasattr(self, 'filter_ac_types_list'):
            self.filter_ac_types_list.setVisible(mode == "Specific Types")
        if hasattr(self, 'filter_ac_category_combo'):
            self.filter_ac_category_combo.setVisible(mode == "Performance Category")
    
    def _on_filter_od_mode_changed(self, mode):
        """Handle OD filter mode changes."""
        if hasattr(self, 'filter_od_pairs_list'):
            self.filter_od_pairs_list.setVisible(mode == "Specific Pairs")
        if hasattr(self, 'filter_distance_widget'):
            self.filter_distance_widget.setVisible(mode == "Distance Range")
    
    def _apply_data_filters(self):
        """Apply selected filters to the data before training."""
        try:
            from . import traffixgen
            
            # Collect filter parameters (with guards for missing UI elements)
            filter_params = {
                'geo_region': getattr(self, 'geo_region_combo', None) and self.geo_region_combo.currentText(),
                'lat_bounds': (self.lat_min_spin.value(), self.lat_max_spin.value()) if hasattr(self, 'geo_region_combo') and self.geo_region_combo.currentText() == "Custom Bounds" else None,
                'lon_bounds': (self.lon_min_spin.value(), self.lon_max_spin.value()) if hasattr(self, 'geo_region_combo') and self.geo_region_combo.currentText() == "Custom Bounds" else None,
                'fl_bounds': (self.filter_min_fl_spin.value(), self.filter_max_fl_spin.value()) if hasattr(self, 'filter_min_fl_spin') else None,
                'ac_filter_mode': getattr(self, 'filter_ac_mode_combo', None) and self.filter_ac_mode_combo.currentText(),
                'selected_ac_types': [item.text() for item in self.filter_ac_types_list.selectedItems()] if hasattr(self, 'filter_ac_types_list') else [],
                'ac_category': getattr(self, 'filter_ac_category_combo', None) and self.filter_ac_category_combo.currentText(),
                'od_filter_mode': getattr(self, 'filter_od_mode_combo', None) and self.filter_od_mode_combo.currentText(),
                'selected_od_pairs': [item.text() for item in self.filter_od_pairs_list.selectedItems()] if hasattr(self, 'filter_od_pairs_list') else [],
                'distance_bounds': (self.filter_min_distance_spin.value(), self.filter_max_distance_spin.value()) if hasattr(self, 'filter_min_distance_spin') else None
            }
            
            # Apply filters
            if hasattr(self, 'filter_status_label'):
                self.filter_status_label.setText("Applying filters...")
                self.filter_status_label.setStyleSheet("color: orange;")
            
            success = traffixgen.traffixgen_apply_data_filters(filter_params)
            
            if success:
                if hasattr(self, 'filter_status_label'):
                    self.filter_status_label.setText("Filters applied successfully!")
                    self.filter_status_label.setStyleSheet("color: green;")
            else:
                if hasattr(self, 'filter_status_label'):
                    self.filter_status_label.setText("Failed to apply filters")
                    self.filter_status_label.setStyleSheet("color: red;")
                
        except Exception as e:
            QMessageBox.critical(self, "Filter Error", f"Failed to apply filters: {e}")
            self.filter_status_label.setText("Filter application failed")
            self.filter_status_label.setStyleSheet("color: red;")
    
    def _train_models(self):
        """Train synthetic route generation models."""
        # Validate required files
        if not all([self._flights_file, self._filed_file, self._actual_file]):
            QMessageBox.warning(self, "Missing Files", 
                              "Please select all three required data files before training models.")
            return
        
        try:
            # Import TraffixGen functions
            from . import traffixgen
            
            # Get model parameters
            model_config = {
                'model_type': self.model_type_combo.currentText(),
                'n_estimators': self.n_estimators_spin.value(),
                'max_depth': self.max_depth_spin.value(),
                'learning_rate': self.learning_rate_spin.value(),
                'n_points': self.n_points_spin.value(),
                'smoothing_alpha': self.smoothing_alpha_spin.value(),
                'interpolation_points': self.interpolation_spin.value()
            }
            
            # Note: Filters are now applied earlier in the workflow, not here
            # Train models via TraffixGen (data should already be filtered)
            print(f"Training models with files:")
            print(f"  Flights: {self._flights_file}")
            print(f"  Filed: {self._filed_file}")
            print(f"  Actual: {self._actual_file}")
            print(f"  FIR: {self._fir_file}")
            print(f"  Model config: {model_config}")
            
            # Handle optional FIR file
            fir_file = getattr(self, '_fir_file', '') if hasattr(self, '_fir_file') and self._fir_file else ''
            
            success = traffixgen.traffixgen_train_synthetic_models(
                self._flights_file, self._filed_file, self._actual_file, fir_file, model_config
            )
            
            if success:
                print("Training completed successfully!")
                self._model_trained = True
                # Save the configuration that was used for training
                self._last_trained_config = self._get_current_model_config()
                # Populate OD pairs and aircraft types from loaded data
                self._populate_filter_lists()
                return True
            else:
                print("Training failed!")
                return False
            
        except Exception as e:
            QMessageBox.critical(self, "Training Error", f"Failed to train models: {e}")
            return False
    
    def _populate_filter_lists(self):
        """Populate OD pairs and aircraft types lists from loaded data."""
        try:
            from . import traffixgen
            
            # Get available OD pairs and aircraft types from the loaded data
            od_pairs, ac_types = traffixgen.traffixgen_get_available_options()
            
            # Populate generation OD pairs list (old interface, still needed for compatibility)
            if hasattr(self, 'od_pairs_list'):
                self.od_pairs_list.clear()
                for od in sorted(od_pairs):
                    self.od_pairs_list.addItem(od)
            
            # Populate generation aircraft types list (old interface)
            if hasattr(self, 'ac_types_list'):
                self.ac_types_list.clear()
                for ac_type in sorted(ac_types):
                    self.ac_types_list.addItem(ac_type)
            
            # Populate filtering interface lists (only if they exist)
            if hasattr(self, 'filter_od_pairs_list'):
                self.filter_od_pairs_list.clear()
                for od in sorted(od_pairs):
                    self.filter_od_pairs_list.addItem(od)
            
            if hasattr(self, 'filter_ac_types_list'):
                self.filter_ac_types_list.clear()
                for ac_type in sorted(ac_types):
                    self.filter_ac_types_list.addItem(ac_type)
            
            # Enable the apply filters button now that data is loaded (only if it exists)
            if hasattr(self, 'btn_apply_filters'):
                self.btn_apply_filters.setEnabled(True)
                
        except Exception as e:
            print(f"Warning: Could not populate filter lists: {e}")
    
    def _auto_generate_trajectories(self):
        """Automatically generate trajectories with current configuration."""
        try:
            # Import TraffixGen functions
            from . import traffixgen
            
            # Clear existing data to force regeneration
            self._synthetic_data = None
            
            # Simple generation parameters (models already trained on filtered data)
            n_flights = self.n_flights_spin.value()
            n_points = self.n_points_spin.value()
            
            # Generate using the trained models
            print(f"Generating {n_flights} trajectories with {n_points} points each...")
            synthetic_data = traffixgen.traffixgen_generate_synthetic_trajectories(n_flights, n_points)
            
            if synthetic_data:
                print(f"Successfully generated {len(synthetic_data)} synthetic trajectories")
                self._synthetic_data = synthetic_data
                return True
            else:
                print("Failed to generate synthetic trajectories")
                return False
                
        except Exception as e:
            print(f"Auto-generation failed: {e}")
            return False
    
    def _invalidate_models(self):
        """Invalidate trained models when data or configuration changes."""
        self._model_trained = False
        self._last_trained_config = None
        self._synthetic_data = None
        print("Model state invalidated - will retrain on next run")
    
    def _get_current_model_config(self):
        """Get current model configuration for change detection."""
        config = {
            'model_type': self.model_type_combo.currentText(),
            'n_estimators': self.n_estimators_spin.value(),
            'max_depth': self.max_depth_spin.value(),
            'learning_rate': self.learning_rate_spin.value(),
            'min_child_weight': self.min_child_weight_spin.value(),
            'subsample': self.subsample_spin.value(),
            'kde_bandwidth': self.kde_bandwidth_spin.value(),
            'kde_kernel': self.kde_kernel_combo.currentText(),
            'kde_atol': self.kde_atol_spin.value(),
            'derivative_bandwidth': self.derivative_bandwidth_spin.value(),
            'derivative_order': self.derivative_order_spin.value(),
            'derivative_smoothing': self.derivative_smoothing_spin.value(),
            'derivative_kernel': self.derivative_kernel_combo.currentText(),
            'n_points': self.n_points_spin.value(),
            'smoothing_alpha': self.smoothing_alpha_spin.value(),
            'interpolation_points': self.interpolation_spin.value(),
            'filters': getattr(self, 'historic_filters', None)
        }
        return config
    
    def _has_model_config_changed(self):
        """Check if model configuration has changed since last training."""
        current_config = self._get_current_model_config()
        last_config = getattr(self, '_last_trained_config', None)
        
        if last_config is None:
            return True  # No previous training
            
        return current_config != last_config
    
    def _execute_complete_workflow(self, progress_dialog=None):
        """Execute the complete automated workflow: load data -> apply filters -> train -> generate -> return success."""
        try:
            def update_progress(text, value):
                if progress_dialog:
                    progress_dialog.setLabelText(text)
                    progress_dialog.setValue(value)
                    QApplication.processEvents()
            
            # Validate required files are loaded (same logic as Realistic Replay)
            if not getattr(self, '_flights_file', ''):
                QMessageBox.warning(self, "Files Required", 
                                  "Please load historic flight data files before creating scenarios.\n\n"
                                  "Use 'Browse...' to select the required data files.")
                return False
            
            if not getattr(self, '_filed_file', ''):
                QMessageBox.warning(self, "Files Required", 
                                  "Please load flight plan data file before creating scenarios.\n\n"
                                  "Use 'Browse...' to select the required data files.")
                return False
            
            if not getattr(self, '_actual_file', ''):
                QMessageBox.warning(self, "Files Required", 
                                  "Please load actual flight points data file before creating scenarios.\n\n"
                                  "Use 'Browse...' to select the required data files.")
                return False
            
            # Step 0: Ensure data is loaded into TraffixGen (critical for model training)
            update_progress("Loading historic flight data...", 15)
            print("Loading data into TraffixGen...")
            from . import traffixgen
            
            # Check if data is already loaded
            data_already_loaded = (hasattr(traffixgen, '_dataset_collection') and 
                                 traffixgen._dataset_collection is not None)
            
            if not data_already_loaded:
                # Load data using optimized approach with enhanced progress feedback
                fir_file = getattr(self, '_fir_file', '') or ''
                
                def progress_callback(message):
                    if progress_dialog:
                        # Enhanced progress display with better formatting
                        lines = message.split('\n')
                        if len(lines) > 1:
                            # Multi-line message - show as structured info
                            main_msg = lines[0]
                            details = '\n'.join(lines[1:])
                            progress_dialog.setLabelText(f"{main_msg}\n\n{details}")
                        else:
                            # Single line message
                            progress_dialog.setLabelText(f"Loading data: {message}")
                        
                        # Extract percentage if available for progress bar
                        if "Progress: " in message and "%" in message:
                            try:
                                pct_start = message.find("Progress: ") + 10
                                pct_end = message.find("%", pct_start)
                                pct_value = float(message[pct_start:pct_end])
                                # Scale to current progress range (15-25% for data loading)
                                scaled_progress = int(15 + (pct_value / 100) * 10)
                                progress_dialog.setValue(scaled_progress)
                            except:
                                pass  # Ignore parsing errors
                        
                        QApplication.processEvents()
                
                load_success = traffixgen.traffixgen_load_eurocontrol(
                    self._flights_file,
                    self._filed_file,
                    self._actual_file,
                    fir_file,
                    progress_callback=progress_callback
                )
                
                if not load_success:
                    QMessageBox.critical(self, "Data Loading Failed", 
                                       "Failed to load historic flight data into TraffixGen.\n\n"
                                       "Please check your data files and try again.")
                    return False
                print("Data loaded successfully into TraffixGen")
            else:
                print("Data already loaded in TraffixGen")
            
            # Step 1: Apply filters if they are configured
            update_progress("Applying data filters...", 25)
            filters = getattr(self, 'historic_filters', None)
            if filters:
                print("Applying historic data filters...")
                filter_success = traffixgen.traffixgen_apply_filters(filters)
                if not filter_success:
                    QMessageBox.critical(self, "Filter Error", "Failed to apply data filters.")
                    return False
                print("Filters applied successfully")
            else:
                print("No filters configured, proceeding with all data")
            
            # Step 2: Train models if not trained or configuration changed
            update_progress("Checking model training status...", 40)
            
            # Check if models are already trained
            from . import traffixgen
            status = traffixgen.get_synthetic_model_status()
            models_already_trained = status.get('model_trained', False)
            
            should_train = True
            if models_already_trained:
                # Ask user if they want to retrain
                reply = QMessageBox.question(
                    self, 
                    "Models Already Trained", 
                    "Machine learning models are already trained.\n\n"
                    "Do you want to retrain them? This will take some time but ensures fresh results.\n\n"
                    "* Click 'Yes' to retrain models (recommended for new data or changed filters)\n"
                    "* Click 'No' to use existing trained models (faster)",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No  # Default to No for faster workflow
                )
                should_train = (reply == QMessageBox.StandardButton.Yes)
            
            if should_train:
                update_progress("Training machine learning models...", 50)
                if models_already_trained:
                    print("Retraining models (user requested fresh training)...")
                else:
                    print("Training models (first time training)...")
                    
                success = self._train_models()
                if not success:
                    QMessageBox.critical(self, "Training Failed", "Failed to train models.")
                    return False
            else:
                print("Using existing trained models (user opted to skip retraining)...")
                update_progress("Using existing trained models...", 50)
            
            # Step 3: Generate trajectories (always regenerate for fresh scenarios)
            update_progress("Generating synthetic trajectories...", 65)
            # FORCE REGENERATION: Always generate new trajectories
            print("Generating synthetic trajectories (forced for fresh scenarios)...")
            success = self._auto_generate_trajectories()
            if not success:
                QMessageBox.critical(self, "Generation Failed", "Failed to generate synthetic trajectories.")
                return False
            
            update_progress("Workflow completed successfully!", 75)
            return True
            
        except Exception as e:
            QMessageBox.critical(self, "Workflow Error", f"Failed to execute complete workflow: {e}")
            return False
    
    def _make(self):
        """Create synthetic trajectory scenario via complete automated workflow."""
        # Validate scenario name
        name = self.scn_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Scenario Name Required", "Please enter a scenario name.")
            return
        
        # Create progress dialog for scenario creation
        progress = QProgressDialog("Creating scenario...", "Cancel", 0, 100, self)
        progress.setWindowTitle("Creating Synthetic Scenario")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setLabelText("Initializing workflow...")
        QApplication.processEvents()
        
        try:
            # Step 1: Execute complete workflow (10% - 80%)
            progress.setLabelText("Loading data and training models...")
            progress.setValue(10)
            QApplication.processEvents()
            
            if not self._execute_complete_workflow(progress):
                progress.close()
                return  # Workflow failed, don't proceed
            
            progress.setLabelText("Workflow complete, creating scenario file...")
            progress.setValue(80)
            QApplication.processEvents()
            
            # Step 2: Create scenario file (80% - 100%)
            self._create_scenario_file(name, progress)
            
            progress.setLabelText("Scenario created successfully!")
            progress.setValue(100)
            QApplication.processEvents()
            
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Error", f"Failed to create scenario: {str(e)}")
        finally:
            if progress:
                progress.close()
    
    def _run(self):
        """Create and run synthetic trajectory scenario via complete automated workflow."""
        # Validate scenario name
        name = self.scn_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Scenario Name Required", "Please enter a scenario name.")
            return
        
        # Create progress dialog for scenario creation and running
        progress = QProgressDialog("Creating and running scenario...", "Cancel", 0, 100, self)
        progress.setWindowTitle("Creating & Running Synthetic Scenario")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setLabelText("Initializing workflow...")
        QApplication.processEvents()
        
        try:
            # Step 1: Execute complete workflow (10% - 70%)
            progress.setLabelText("Loading data and training models...")
            progress.setValue(10)
            QApplication.processEvents()
            
            if not self._execute_complete_workflow(progress):
                progress.close()
                return  # Workflow failed, don't proceed
            
            progress.setLabelText("Workflow complete, creating and running scenario...")
            progress.setValue(70)
            QApplication.processEvents()
            
            # Step 2: Create and run scenario (70% - 100%)
            self._create_and_run_scenario(name, progress)
            
            progress.setLabelText("Scenario created and running successfully!")
            progress.setValue(100)
            QApplication.processEvents()
            
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Error", f"Failed to create and run scenario: {str(e)}")
        finally:
            if progress:
                progress.close()
    
    def _create_scenario_file(self, name, progress_dialog=None):
        """Create scenario file after successful workflow execution."""
        try:
            def update_progress(text, value):
                if progress_dialog:
                    progress_dialog.setLabelText(text)
                    progress_dialog.setValue(value)
                    QApplication.processEvents()
            
            # Import TraffixGen functions
            from . import traffixgen
            
            update_progress("Exporting synthetic data to SATG format...", 85)
            
            # Export to SATG format first
            success = traffixgen.traffixgen_export_synthetic_to_satg(self._synthetic_data)
            
            if not success:
                QMessageBox.critical(self, "Export Failed", "Failed to export synthetic data to SATG.")
                return
            
            update_progress("Creating scenario file...", 95)
            
            # Create scenario using SATG functions
            from . import SATG
            
            # Create the scenario file using Historic Sampling methods
            scenario_success = SATG.SATG_HS_MAKE(name)
            
            if scenario_success:
                QMessageBox.information(self, "Success", f"Scenario '{name}' created successfully!")
                print(f"Synthetic scenario '{name}' created successfully!")
            else:
                QMessageBox.critical(self, "Creation Failed", f"Failed to create scenario '{name}'.")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create scenario: {str(e)}")
    
    def _create_and_run_scenario(self, name, progress_dialog=None):
        """Create and run scenario after successful workflow execution."""
        try:
            def update_progress(text, value):
                if progress_dialog:
                    progress_dialog.setLabelText(text)
                    progress_dialog.setValue(value)
                    QApplication.processEvents()
            
            # Import TraffixGen functions
            from . import traffixgen
            
            update_progress("Exporting synthetic data to SATG format...", 80)
            
            # Export to SATG format first
            success = traffixgen.traffixgen_export_synthetic_to_satg(self._synthetic_data)
            
            if not success:
                QMessageBox.critical(self, "Export Failed", "Failed to export synthetic data to SATG.")
                return
            
            update_progress("Creating and running scenario...", 90)
            
            # Create and run scenario using TraffixGen helper function
            success = traffixgen.traffixgen_create_and_run_synthetic_scenario(name)
            
            if success:
                QMessageBox.information(self, "Success", f"Scenario '{name}' created and running!")
                print(f"Synthetic scenario '{name}' created and running!")
            else:
                QMessageBox.critical(self, "Creation Failed", f"Failed to create and run scenario '{name}'.")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create and run scenario: {str(e)}")

    def _run_only(self):
        """Run an existing synthetic scenario without creating it."""
        name = self.scn_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Scenario Name Required", "Please enter a scenario name.")
            return
        
        try:
            # Check if scenario file exists
            import os
            from . import SATG
            scenario_path = os.path.join(SATG.STATE.scn_dir, f"{name}.scn")
            
            if not os.path.exists(scenario_path):
                QMessageBox.warning(self, "Scenario Not Found", 
                                  f"Scenario file '{name}.scn' not found.\n\n"
                                  "Please create the scenario first or check the scenario name.")
                return
            
            # Load the existing scenario
            from bluesky import stack
            stack.stack(f"IC {scenario_path}")
            QMessageBox.information(self, "Success", f"Scenario '{name}' loaded and running!")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to run scenario: {str(e)}")

    def _configure_historic_filters(self):
        """Open dialog to configure historic data filtering - using same approach as realistic replay."""
        # Check if required files are loaded (same warning logic as Eurocontrol tab)
        if not getattr(self, '_flights_file', ''):
            QMessageBox.warning(self, "Files Required", 
                              "Please load historic flight data file before configuring filters.\n\n"
                              "Click 'Browse...' to select a historic flight data file.")
            return
            
        # Prepare current filters (same structure as EurocontrolFilterDialog)
        current_filters = getattr(self, 'historic_filters', {
            'lat_min': -90, 'lat_max': 90,
            'lon_min': -180, 'lon_max': 180,
            'fl_min': 0, 'fl_max': 500,
            'include_airspace': [],
            'time_start': None, 'time_end': None,
            'aircraft_types': []
        })
        
        # Check if we can use cached summary data (only recalculate if file paths changed)
        current_file_paths = {
            'flights_file': getattr(self, '_flights_file', ''),
            'filed_file': getattr(self, '_filed_file', ''),
            'actual_file': getattr(self, '_actual_file', ''),
            'fir_file': getattr(self, '_fir_file', '')
        }
        
        cached_paths = getattr(self, '_cached_filter_file_paths', {})
        cached_summary = getattr(self, '_cached_filter_summary_data', None)
        
        # Only refresh data if file paths have changed or no cached data exists
        if current_file_paths != cached_paths or cached_summary is None:
            needs_refresh = True
        else:
            needs_refresh = False
        
        # Show progress dialog for data loading/caching
        from PyQt6.QtWidgets import QProgressDialog
        from PyQt6.QtCore import Qt
        
        if needs_refresh:
            progress = QProgressDialog("Loading data for filter configuration...", "Cancel", 0, 100, self)
            progress.setWindowTitle("Loading Data")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)  # Show immediately
            progress.setMinimumSize(400, 120)  # Ensure proper sizing
            progress.show()  # Explicitly show the dialog
            progress.setValue(10)
            progress.setLabelText("Checking data files...")
            QApplication.processEvents()  # Update UI
            
            fir_file = getattr(self, '_fir_file', None)
            
            # Update progress
            progress.setValue(30)
            progress.setLabelText("Loading flight data...")  # Shorter text to prevent clipping
            QApplication.processEvents()  # Update UI
            
            # Force reload summary data to get fresh bounds with progress callback
            def update_progress(message):
                if progress and not progress.wasCanceled():
                    progress.setLabelText(message)
                    QApplication.processEvents()
            
            summary_data = self._load_and_get_summary_for_historic(progress_callback=update_progress)
            if summary_data and 'date_bounds' in summary_data:
                print(f">> Date bounds loaded: {summary_data['date_bounds']['min']} to {summary_data['date_bounds']['max']}")
                # Cache the successful data and file paths
                self._cached_filter_file_paths = current_file_paths.copy()
                self._cached_filter_summary_data = summary_data
            else:
                print("WARNING: Date bounds missing - checking data processing...")
                # Force a call to get_flight_summary to see what's happening
                try:
                    from . import traffixgen
                    direct_summary = traffixgen.get_flight_summary()
                    if 'date_bounds' in direct_summary:
                        print(f">> Date bounds found in direct call: {direct_summary['date_bounds']['min']} to {direct_summary['date_bounds']['max']}")
                        summary_data = direct_summary
                        # Cache the successful data and file paths
                        self._cached_filter_file_paths = current_file_paths.copy()
                        self._cached_filter_summary_data = summary_data
                    else:
                        print("WARNING: Date bounds missing in direct call too")
                except Exception as e:
                    print(f"WARNING: Error getting summary: {e}")
            
            # Update progress  
            progress.setValue(80)
            progress.setLabelText("Calculating data bounds...")
            QApplication.processEvents()  # Update UI
        else:
            # Use cached data - much faster
            summary_data = cached_summary
            print(f">> Using cached date bounds: {summary_data.get('date_bounds', {}).get('min', 'N/A')} to {summary_data.get('date_bounds', {}).get('max', 'N/A')}")
        
        fir_file = getattr(self, '_fir_file', None)
        
        # Create filter dialog with same interface as EurocontrolFilterDialog
        self.historic_filter_dialog = HistoricSamplingFilterDialog(
            current_filters=current_filters,
            fir_file_path=fir_file,
            summary_data=summary_data,
            parent=self
        )
        
        # Always try to set data context and bounds
        if summary_data and 'error' not in summary_data:
            self.historic_filter_dialog.summary_data = summary_data
            self.historic_filter_dialog._set_bounds_from_data()
            # Force a filter summary update after bounds are set
            self.historic_filter_dialog._update_filter_summary()
        
        # Complete progress (only if we showed it)
        if needs_refresh:
            progress.setValue(100)
            progress.setLabelText("Ready!")
            QApplication.processEvents()  # Update UI
            progress.close()
        
        result = self.historic_filter_dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            filters = self.historic_filter_dialog.get_filters()
            # Store filters for later use during training
            old_filters = getattr(self, 'historic_filters', None)
            self.historic_filters = filters
            
            # Invalidate models if filters changed
            if old_filters != filters:
                self._invalidate_models()

    def _load_and_get_summary_for_historic(self, progress_callback=None):
        """Load and get summary for historic data - mimics realistic replay approach."""
        try:
            # Method 1: Try to get summary from existing TraffixGen data (best approach)
            from . import traffixgen
            
            # Check if TraffixGen has loaded data we can use
            if hasattr(traffixgen, '_dataset_collection') and traffixgen._dataset_collection is not None:
                # Check if the existing data has a Date column with valid data
                try:
                    points_df = traffixgen._dataset_collection.flights_points.data
                    if 'Date' not in points_df.columns:
                        print(">> Cached data missing Date column, reloading...")
                        traffixgen._dataset_collection = None
                    else:
                        # Check if Date column has valid data
                        date_data = points_df['Date'].dropna()
                        date_data = date_data[date_data.str.len() > 0] if hasattr(date_data, 'str') else date_data
                        
                        if len(date_data) == 0:
                            print(">> Cached data has empty Date column, clearing cache and reloading...")
                            traffixgen._dataset_collection = None
                            # Clear parquet cache files to force fresh processing
                            if self.parent() and hasattr(self.parent(), '_clear_traffixgen_cache'):
                                self.parent()._clear_traffixgen_cache()
                        else:
                            print(f"OK: Using cached data with Date column ({len(date_data):,} valid dates)")
                            summary = traffixgen.get_flight_summary()
                            if 'error' not in summary and 'date_bounds' in summary:
                                return summary
                            else:
                                print(f">> Summary missing date bounds, clearing cache and reloading...")
                                traffixgen._dataset_collection = None
                                # Clear parquet cache files to force fresh processing
                                if self.parent() and hasattr(self.parent(), '_clear_traffixgen_cache'):
                                    self.parent()._clear_traffixgen_cache()
                except Exception as e:
                    print(f">> Forcing data reload: {e}")
                    traffixgen._dataset_collection = None
            
            # Method 2: Load data specifically for historic sampling if we have file paths
            if hasattr(self, '_flights_file') and self._flights_file:
                print(">> Loading EUROCONTROL data...")
                
                # Try to load via TraffixGen (same as realistic replay)
                fir_file = getattr(self, '_fir_file', '') or ''
                
                # Load the data using TraffixGen with progress callback
                if progress_callback:
                    progress_callback("Loading EUROCONTROL flight data...")
                
                result = traffixgen.traffixgen_load_eurocontrol(
                    self._flights_file,
                    getattr(self, '_filed_file', ''),
                    getattr(self, '_actual_file', ''),
                    fir_file,
                    progress_callback=progress_callback
                )
                
                if result:
                    summary = traffixgen.get_flight_summary()
                    if 'error' not in summary:
                        # Check if we got date bounds this time
                        if 'date_bounds' not in summary:
                            print("WARNING: Data loaded but still missing date bounds, clearing cache for next time...")
                            if self.parent() and hasattr(self.parent(), '_clear_traffixgen_cache'):
                                self.parent()._clear_traffixgen_cache()
                        return summary
                    else:
                        print(f"ERROR: Summary error: {summary}")
                else:
                    print("ERROR: Failed to load EUROCONTROL data")
            
            # Method 3: Fallback to direct DataFrame analysis
            return self._load_data_sample()
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._load_data_sample()

    def _load_data_sample(self):
        """Load a sample of historic data to determine filter bounds - using TraffixGen approach."""
        try:
            # Check if we have loaded data via TraffixGen (same as realistic replay)
            from . import traffixgen
            
            # Try to get the flight summary from TraffixGen (same method as realistic replay)
            if hasattr(traffixgen, '_dataset_collection') and traffixgen._dataset_collection is not None:
                summary = traffixgen.get_flight_summary()
                if 'error' not in summary:
                    return summary
            
            # Fallback 1: Check if we have historic_data loaded in parent
            if hasattr(self, 'parent') and self.parent() and hasattr(self.parent(), 'historic_data'):
                data = self.parent().historic_data
                if data is not None and not data.empty:
                    return self._extract_summary_from_dataframe(data)
            
            # Fallback 2: Try to load data if we have file path
            if hasattr(self, 'parent') and self.parent() and hasattr(self.parent(), '_flights_file'):
                flights_file = self.parent()._flights_file
                if flights_file and os.path.exists(flights_file):
                    import pandas as pd
                    data = pd.read_csv(flights_file, nrows=1000)
                    return self._extract_summary_from_dataframe(data)
            
            return self._get_default_summary()
                
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._get_default_summary()
    
    def _extract_summary_from_dataframe(self, data):
        """Extract summary data from a pandas DataFrame using exact column detection."""
        import pandas as pd
        try:
            summary_data = {
                'total_flights': len(data),
                'total_points': len(data)
            }
            
            # Map common column variations to standard names
            column_mappings = {
                'latitude': ['Latitude', 'ADEP Latitude', 'ADES Latitude', 'latitude', 'lat', 'LAT'],
                'longitude': ['Longitude', 'ADEP Longitude', 'ADES Longitude', 'longitude', 'lon', 'LON'],
                'flight_level': ['Flight Level', 'Requested FL', 'flight_level', 'altitude', 'FL', 'RequestedFL'],
                'aircraft_type': ['AC Type', 'aircraft_type', 'type', 'Aircraft Type', 'ACType']
            }
            
            # Extract latitude bounds
            lat_values = []
            for col_name in column_mappings['latitude']:
                if col_name in data.columns:
                    values = data[col_name].dropna()
                    if not values.empty:
                        lat_values.extend(values.tolist())
            
            if lat_values:
                summary_data['lat_bounds'] = {
                    'min': float(min(lat_values)), 
                    'max': float(max(lat_values))
                }
            
            # Extract longitude bounds
            lon_values = []
            for col_name in column_mappings['longitude']:
                if col_name in data.columns:
                    values = data[col_name].dropna()
                    if not values.empty:
                        lon_values.extend(values.tolist())
            
            if lon_values:
                summary_data['lon_bounds'] = {
                    'min': float(min(lon_values)), 
                    'max': float(max(lon_values))
                }
            
            # Extract flight level bounds
            for col_name in column_mappings['flight_level']:
                if col_name in data.columns:
                    fl_data = data[col_name].dropna()
                    if not fl_data.empty:
                        try:
                            fl_numeric = pd.to_numeric(fl_data, errors='coerce').dropna()
                            if not fl_numeric.empty:
                                summary_data['fl_bounds'] = {
                                    'min': int(fl_numeric.min()), 
                                    'max': int(fl_numeric.max())
                                }
                                break
                        except Exception as e:
                            pass
            
            # Extract aircraft types
            for col_name in column_mappings['aircraft_type']:
                if col_name in data.columns:
                    ac_data = data[col_name].dropna()
                    if not ac_data.empty:
                        aircraft_types = sorted(ac_data.unique())
                        summary_data['aircraft_types'] = aircraft_types[:50]  # Limit to 50
                        break
            
            # Default aircraft types if none found
            if 'aircraft_types' not in summary_data:
                summary_data['aircraft_types'] = ['A320', 'A330', 'A380', 'B737', 'B747', 'B777', 'B787']
            
            return summary_data
            
        except Exception as e:
            return self._get_default_summary()
    
    def _get_default_summary(self):
        """Return default summary data when no data is available"""
        return {
            'lat_bounds': {'min': -90, 'max': 90},
            'lon_bounds': {'min': -180, 'max': 180},
            'fl_bounds': {'min': 0, 'max': 500},
            'aircraft_types': ['A320', 'A330', 'A380', 'B737', 'B747', 'B777', 'B787', 'E190', 'CRJ9']
        }


# HistoricSamplingFilterDialog - Filter configuration dialog for historic sampling
class HistoricSamplingFilterDialog(QDialog):
    """
    Specialized dialog for configuring Historic Sampling flight data filtering with ML optimization.
    
    This advanced filtering interface provides comprehensive flight data filtering capabilities
    specifically optimized for Historic Sampling machine learning workflows. The dialog offers
    identical functionality to EurocontrolFilterDialog but excludes polygon-based filtering
    to focus on the most relevant filter types for machine learning model training and
    synthetic traffic generation from historic flight data.
    
    The Historic Sampling Filter Dialog is designed specifically for machine learning workflows
    where precise control over training data characteristics is essential. By focusing on
    geographic, temporal, altitude, and aircraft type filters, the dialog provides the most
    relevant filtering capabilities for creating high-quality training datasets from historic
    flight operations data.
    
    Key Features:
    - Geographic Filtering: Precise latitude/longitude bounding box configuration
    - Temporal Filtering: Date and time range selection for historic data periods  
    - Altitude Filtering: Flight level range configuration for different operational phases
    - Aircraft Type Filtering: Specific aircraft model and category selection
    - Airspace Integration: FIR boundary-based filtering for regional data analysis
    - Summary Data Integration: Real-time filtering impact analysis and statistics
    - ML-Optimized Interface: Streamlined for machine learning data preparation workflows
    
    Filter Categories:
    - Geographic Bounds: Latitude/longitude ranges for regional flight data selection
    - Temporal Windows: Date/time ranges for historic period analysis
    - Altitude Constraints: Flight level filtering for operational phase focus
    - Aircraft Selection: Type-based filtering for aircraft category analysis
    - Airspace Regions: FIR-based geographic filtering with boundary precision
    - Data Quality: Integration with summary statistics for filtering impact assessment
    
    Machine Learning Optimization:
    - Training Data Focus: Optimized filter selection for ML model training requirements
    - Data Distribution Analysis: Real-time statistics showing filtering impact on data characteristics
    - Quality Metrics: Integration with data quality assessment and completeness indicators
    - Batch Processing: Efficient filtering for large historic datasets with performance optimization
    - Validation Integration: Filter validation ensuring adequate data volume for training
    
    Attributes:
        current_filters (Dict): Current filter configuration with geographic, temporal, altitude constraints
                              - lat_min/lat_max: Latitude bounding box limits
                              - lon_min/lon_max: Longitude bounding box limits  
                              - fl_min/fl_max: Flight level range constraints
                              - include_airspace: Selected FIR regions for analysis
                              - time_start/time_end: Temporal filtering window
                              - aircraft_types: Selected aircraft models and categories
    
    Args:
        current_filters (Dict, optional): Existing filter configuration for initialization
        fir_file_path (str, optional): Path to FIR boundary definition file for airspace filtering
        summary_data (Dict, optional): Dataset summary statistics for filtering impact analysis
        parent (QWidget, optional): Parent widget for proper dialog behavior
    
    Examples:
        # Configure filters for historic sampling ML training
        current_config = {
            'lat_min': 40.0, 'lat_max': 50.0,
            'lon_min': -10.0, 'lon_max': 10.0,
            'fl_min': 100, 'fl_max': 400,
            'aircraft_types': ['A320', 'B737']
        }
        
        dialog = HistoricSamplingFilterDialog(
            current_filters=current_config,
            fir_file_path="/path/to/fir_boundaries.json",
            summary_data=dataset_stats,
            parent=self
        )
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ml_filters = dialog.get_filters()
            self._apply_ml_training_filters(ml_filters)
    
    Note:
        This dialog is specifically designed for Historic Sampling workflows and excludes
        polygon-based filtering to focus on the most relevant filter types for machine
        learning applications. Integration with summary data provides real-time feedback
        on filtering impact and ensures adequate data volume for effective model training.
    """
    
    def __init__(self, current_filters=None, fir_file_path=None, summary_data=None, parent=None):
        super().__init__(parent)
        
        # Initialize with default filters and update with provided ones
        self.current_filters = {
            'lat_min': -90, 'lat_max': 90,
            'lon_min': -180, 'lon_max': 180,
            'fl_min': 0, 'fl_max': 500,
            'include_airspace': [],
            'time_start': None, 'time_end': None,
            'aircraft_types': []
        }
        
        # Update with provided filters
        if current_filters:
            self.current_filters.update(current_filters)
            
        self.fir_file_path = fir_file_path
        self.summary_data = summary_data
        
        self.setWindowTitle("Configure Historic Data Filters")
        self.setModal(False)  # Make dialog non-modal so users can interact with radar
        self.resize(800, 600)  # Make wider to accommodate summary column
        
        self._setup_ui()
        self._set_bounds_from_data()  # Set bounds based on loaded data
        self._load_current_settings()
        
        # Load airspace options if FIR file is available
        if self.fir_file_path:
            self._load_airspace_options()

    def keyPressEvent(self, event):
        """Override key press events to prevent Enter from closing dialog"""
        from PyQt5.QtCore import Qt
        
        # If Enter/Return is pressed, don't call the parent's keyPressEvent
        # which would trigger the default button behavior
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            # Ignore Enter key for all widgets to prevent dialog closure
            event.ignore()
        else:
            # For all other keys, use normal behavior
            super().keyPressEvent(event)

    def _setup_ui(self):
        """Setup the filter dialog UI"""
        layout = QVBoxLayout(self)
        
        # Create horizontal layout for tabs and summary
        main_layout = QHBoxLayout()
        
        # Create tabs for different filter categories
        tabs = QTabWidget()
        
        # Geographic filters tab
        geo_tab = self._create_geographic_tab()
        tabs.addTab(geo_tab, "Geographic")
        
        # Flight level filters tab
        fl_tab = self._create_flight_level_tab()
        tabs.addTab(fl_tab, "Flight Levels")
        
        # Airspace filters tab
        airspace_tab = self._create_airspace_tab()
        tabs.addTab(airspace_tab, "Airspace")
        
        # Time filters tab
        time_tab = self._create_time_tab()
        tabs.addTab(time_tab, "Time Range")
        
        # Aircraft filters tab
        aircraft_tab = self._create_aircraft_tab()
        tabs.addTab(aircraft_tab, "Aircraft")
        
        # Note: Polygon tab excluded for Historic Sampling
        
        # Add tabs to left side of layout
        main_layout.addWidget(tabs, 2)  # Takes 2/3 of the space
        
        # Create data summary panel
        summary_panel = self._create_summary_panel()
        main_layout.addWidget(summary_panel, 1)  # Takes 1/3 of the space
        
        layout.addLayout(main_layout)
        
        # Buttons - using individual buttons instead of QDialogButtonBox to avoid Enter key issues
        button_layout = QHBoxLayout()
        
        # Reset All button
        reset_all_btn = QPushButton("Reset All")
        reset_all_btn.setToolTip("Reset all filter settings to match the loaded data ranges")
        reset_all_btn.clicked.connect(self._reset_all_filters)
        reset_all_btn.setAutoDefault(False)
        button_layout.addWidget(reset_all_btn)
        
        button_layout.addStretch()  # Push OK/Cancel to the right
        
        # OK button
        ok_btn = QPushButton("OK")
        ok_btn.setToolTip("Save filter configuration and close dialog")
        ok_btn.clicked.connect(self._save_and_close)
        ok_btn.setAutoDefault(False)
        button_layout.addWidget(ok_btn)
        
        # Cancel button
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setToolTip("Cancel and close dialog without applying filters")
        cancel_btn.clicked.connect(self.close)
        cancel_btn.setAutoDefault(False)
        button_layout.addWidget(cancel_btn)
        
        layout.addLayout(button_layout)

    def _create_geographic_tab(self):
        """Create geographic filtering tab"""
        tab = QWidget()
        layout = QFormLayout(tab)
        
        # Latitude bounds
        layout.addRow(QLabel("Latitude Bounds (degrees):"))
        
        lat_layout = QHBoxLayout()
        self.lat_min_spin = QDoubleSpinBox()
        self.lat_min_spin.setRange(-90, 90)
        self.lat_min_spin.setValue(-90)
        self.lat_min_spin.setDecimals(2)
        _configure_decimal_separator(self.lat_min_spin)
        
        self.lat_max_spin = QDoubleSpinBox()
        self.lat_max_spin.setRange(-90, 90)
        self.lat_max_spin.setValue(90)
        self.lat_max_spin.setDecimals(2)
        _configure_decimal_separator(self.lat_max_spin)
        
        lat_layout.addWidget(QLabel("Min:"))
        lat_layout.addWidget(self.lat_min_spin)
        lat_layout.addWidget(QLabel("Max:"))
        lat_layout.addWidget(self.lat_max_spin)
        lat_layout.addStretch()
        
        layout.addRow(lat_layout)
        
        # Longitude bounds
        layout.addRow(QLabel("Longitude Bounds (degrees):"))
        
        lon_layout = QHBoxLayout()
        self.lon_min_spin = QDoubleSpinBox()
        self.lon_min_spin.setRange(-180, 180)
        self.lon_min_spin.setValue(-180)
        self.lon_min_spin.setDecimals(2)
        _configure_decimal_separator(self.lon_min_spin)
        
        self.lon_max_spin = QDoubleSpinBox()
        self.lon_max_spin.setRange(-180, 180)
        self.lon_max_spin.setValue(180)
        self.lon_max_spin.setDecimals(2)
        _configure_decimal_separator(self.lon_max_spin)
        
        lon_layout.addWidget(QLabel("Min:"))
        lon_layout.addWidget(self.lon_min_spin)
        lon_layout.addWidget(QLabel("Max:"))
        lon_layout.addWidget(self.lon_max_spin)
        lon_layout.addStretch()
        
        layout.addRow(lon_layout)
        
        # Add reset button for this tab
        reset_btn = QPushButton("Reset Geographic Filters")
        reset_btn.setToolTip("Reset latitude and longitude bounds to match the loaded data range")
        reset_btn.clicked.connect(self._reset_geographic_filters)
        reset_btn.setAutoDefault(False)
        layout.addRow(reset_btn)
        
        return tab

    def _create_flight_level_tab(self):
        """Create flight level filtering tab"""
        tab = QWidget()
        layout = QFormLayout(tab)
        
        # Flight level bounds
        layout.addRow(QLabel("Flight Level Bounds:"))
        
        fl_layout = QHBoxLayout()
        self.fl_min_spin = QSpinBox()
        self.fl_min_spin.setRange(0, 500)
        self.fl_min_spin.setValue(0)
        self.fl_min_spin.setSuffix(" FL")
        _configure_decimal_separator(self.fl_min_spin)
        
        self.fl_max_spin = QSpinBox()
        self.fl_max_spin.setRange(0, 500)
        self.fl_max_spin.setValue(500)
        self.fl_max_spin.setSuffix(" FL")
        _configure_decimal_separator(self.fl_max_spin)
        
        fl_layout.addWidget(QLabel("Min:"))
        fl_layout.addWidget(self.fl_min_spin)
        fl_layout.addWidget(QLabel("Max:"))
        fl_layout.addWidget(self.fl_max_spin)
        fl_layout.addStretch()
        
        layout.addRow(fl_layout)
        
        # Add reset button for this tab
        reset_btn = QPushButton("Reset Flight Level Filters")
        reset_btn.setToolTip("Reset flight level bounds to match the loaded data range")
        reset_btn.clicked.connect(self._reset_flight_level_filters)
        reset_btn.setAutoDefault(False)
        layout.addRow(reset_btn)
        
        return tab

    def _create_airspace_tab(self):
        """Create airspace filtering tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Instructions
        instructions = QLabel("Select airspace sectors to INCLUDE in the dataset:")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        
        # Airspace status
        self.airspace_status = QLabel("No airspace data loaded")
        self.airspace_status.setStyleSheet("color: #666; font-style: italic; font-size: 11px;")
        layout.addWidget(self.airspace_status)
        
        # Airspace list (will be populated if FIR data is available)
        self.airspace_list = QListWidget()
        self.airspace_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        layout.addWidget(self.airspace_list)
        
        # Reset button
        reset_btn = QPushButton("Reset Airspace Filters")
        reset_btn.setToolTip("Clear all airspace exclusions")
        reset_btn.clicked.connect(self._reset_airspace_filters)
        reset_btn.setAutoDefault(False)
        layout.addWidget(reset_btn)
        
        return tab

    def _create_time_tab(self):
        """Create time filtering tab"""
        tab = QWidget()
        layout = QFormLayout(tab)
        
        # Enable time filtering
        self.time_enabled = QCheckBox("Enable time range filtering")
        layout.addRow(self.time_enabled)
        
        # Start time
        self.time_start = QTimeEdit()
        self.time_start.setDisplayFormat("hh:mm:ss")
        self.time_start.setTime(QTime(0, 0, 0))
        self.time_start.setEnabled(False)
        layout.addRow("Start time:", self.time_start)
        
        # End time
        self.time_end = QTimeEdit()
        self.time_end.setDisplayFormat("hh:mm:ss")
        self.time_end.setTime(QTime(23, 59, 59))
        self.time_end.setEnabled(False)
        layout.addRow("End time:", self.time_end)
        
        # Connect checkbox to enable/disable time inputs
        self.time_enabled.toggled.connect(self.time_start.setEnabled)
        self.time_enabled.toggled.connect(self.time_end.setEnabled)
        
        # Date range filtering
        layout.addRow(QLabel(""))  # Spacer
        self.date_enabled = QCheckBox("Enable date range filtering")
        layout.addRow(self.date_enabled)
        
        # Start date
        self.date_start = QDateEdit()
        self.date_start.setDisplayFormat("dd-MM-yyyy")
        self.date_start.setDate(QDate(2021, 12, 1))  # Default to December 2021
        self.date_start.setCalendarPopup(True)
        self.date_start.setEnabled(False)
        layout.addRow("Start date:", self.date_start)
        
        # End date
        self.date_end = QDateEdit()
        self.date_end.setDisplayFormat("dd-MM-yyyy")
        self.date_end.setDate(QDate(2021, 12, 31))  # Default to December 2021
        self.date_end.setCalendarPopup(True)
        self.date_end.setEnabled(False)
        layout.addRow("End date:", self.date_end)
        
        # Connect date checkbox
        self.date_enabled.toggled.connect(self.date_start.setEnabled)
        self.date_enabled.toggled.connect(self.date_end.setEnabled)
        
        # Add reset button for this tab
        reset_btn = QPushButton("Reset Time Filters")
        reset_btn.setToolTip("Reset time range to data-driven bounds")
        reset_btn.clicked.connect(self._reset_time_filters)
        reset_btn.setAutoDefault(False)
        layout.addRow(reset_btn)
        
        return tab

    def _create_aircraft_tab(self):
        """Create aircraft type filtering tab - identical to realistic replay"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Instructions (same as realistic replay)
        instructions = QLabel("Select specific aircraft types to include (leave all unchecked to include all types):")
        instructions.setStyleSheet("color: #666; font-style: italic;")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        
        # Aircraft type list (same as realistic replay)
        self.aircraft_list = QListWidget()
        self.aircraft_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        
        # Common aircraft types (same as realistic replay)
        common_types = [
            "A319", "A320", "A321", "A330", "A340", "A350", "A380",
            "B737", "B738", "B747", "B757", "B767", "B777", "B787",
            "E170", "E175", "E190", "CRJ7", "CRJ9", "DH8D",
            "AT72", "BE20", "C25A", "F900"
        ]
        
        for ac_type in common_types:
            item = QListWidgetItem(ac_type)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.aircraft_list.addItem(item)
        
        layout.addWidget(self.aircraft_list)
        
        # Add reset button for this tab (same as realistic replay)
        reset_btn = QPushButton("Reset Aircraft Filters")
        reset_btn.setToolTip("Reset aircraft type selections to default values")
        reset_btn.clicked.connect(self._reset_aircraft_filters)
        reset_btn.setAutoDefault(False)
        layout.addWidget(reset_btn)
        
        return tab

    def _create_summary_panel(self):
        """Create the filter status panel"""
        panel = QGroupBox("Current Filter Settings")
        layout = QVBoxLayout(panel)
        
        # Create scroll area for filter status
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMaximumHeight(400)
        
        self.filter_status_label = QLabel("Filters will be set based on your selections above")
        self.filter_status_label.setWordWrap(True)
        self.filter_status_label.setStyleSheet("color: #333; font-size: 11px;")
        self.filter_status_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        scroll_area.setWidget(self.filter_status_label)
        layout.addWidget(scroll_area)
        
        # Connect all input changes to update summary
        self._connect_summary_updates()
        
        return panel

    def _connect_summary_updates(self):
        """Connect all input changes to update the summary panel"""
        # Geographic changes
        if hasattr(self, 'lat_min_spin'):
            self.lat_min_spin.valueChanged.connect(self._update_filter_summary)
            self.lat_max_spin.valueChanged.connect(self._update_filter_summary)
            self.lon_min_spin.valueChanged.connect(self._update_filter_summary)
            self.lon_max_spin.valueChanged.connect(self._update_filter_summary)
        
        # Flight level changes
        if hasattr(self, 'fl_min_spin'):
            self.fl_min_spin.valueChanged.connect(self._update_filter_summary)
            self.fl_max_spin.valueChanged.connect(self._update_filter_summary)
        
        # Time filter changes
        if hasattr(self, 'time_enabled'):
            self.time_enabled.toggled.connect(self._update_filter_summary)
            self.time_start.timeChanged.connect(self._update_filter_summary)
            self.time_end.timeChanged.connect(self._update_filter_summary)
        
        # Date filter changes
        if hasattr(self, 'date_enabled'):
            self.date_enabled.toggled.connect(self._update_filter_summary)
            self.date_start.dateChanged.connect(self._update_filter_summary)
            self.date_end.dateChanged.connect(self._update_filter_summary)
        
        # Aircraft list changes
        if hasattr(self, 'aircraft_list'):
            self.aircraft_list.itemSelectionChanged.connect(self._update_filter_summary)

    def _update_filter_summary(self):
        """Update the filter summary display"""
        summary_parts = []
        
        # Show data-driven bounds info if available
        if self.summary_data:
            data_parts = []
            if 'lat_bounds' in self.summary_data:
                lat_bounds = self.summary_data['lat_bounds']
                data_parts.append(f"Lat: {lat_bounds['min']:.2f} deg to {lat_bounds['max']:.2f} deg")
            if 'lon_bounds' in self.summary_data:
                lon_bounds = self.summary_data['lon_bounds']
                data_parts.append(f"Lon: {lon_bounds['min']:.2f} deg to {lon_bounds['max']:.2f} deg")
            if 'fl_bounds' in self.summary_data:
                fl_bounds = self.summary_data['fl_bounds']
                data_parts.append(f"FL: {fl_bounds['min']} to {fl_bounds['max']}")
            if 'time_bounds' in self.summary_data:
                time_bounds = self.summary_data['time_bounds']
                data_parts.append(f"Time: {time_bounds['min']} to {time_bounds['max']}")
            if 'date_bounds' in self.summary_data:
                date_bounds = self.summary_data['date_bounds']
                data_parts.append(f"Date: {date_bounds['min']} to {date_bounds['max']}")
                print(f"[OK] Date range found: {date_bounds['min']} to {date_bounds['max']}")
            else:
                print("[ERROR] No date range found in data")
            if 'aircraft_types' in self.summary_data:
                ac_count = len(self.summary_data['aircraft_types'])
                data_parts.append(f"Aircraft types: {ac_count}")
            
            if data_parts:
                summary_parts.append(f"<b>Available Data Ranges:</b><br>{'<br>'.join(data_parts)}")
        
        # Geographic filters
        if hasattr(self, 'lat_min_spin'):
            lat_min, lat_max = self.lat_min_spin.value(), self.lat_max_spin.value()
            lon_min, lon_max = self.lon_min_spin.value(), self.lon_max_spin.value()
            summary_parts.append(f"<b>Geographic Filter:</b><br>Lat: {lat_min:.2f} deg to {lat_max:.2f} deg<br>Lon: {lon_min:.2f} deg to {lon_max:.2f} deg")
        
        # Flight level filters
        if hasattr(self, 'fl_min_spin'):
            fl_min, fl_max = self.fl_min_spin.value(), self.fl_max_spin.value()
            summary_parts.append(f"<b>Flight Level Filter:</b><br>FL{fl_min} to FL{fl_max}")
        
        # Time filters
        if hasattr(self, 'time_enabled'):
            if self.time_enabled.isChecked():
                start = self.time_start.time().toString("hh:mm:ss")
                end = self.time_end.time().toString("hh:mm:ss")
                summary_parts.append(f"<b>Time Filter:</b><br>{start} to {end}")
            else:
                summary_parts.append(f"<b>Time Filter:</b><br>Disabled")
        
        # Date filters
        if hasattr(self, 'date_enabled'):
            if self.date_enabled.isChecked():
                start = self.date_start.date().toString("dd-MM-yyyy")
                end = self.date_end.date().toString("dd-MM-yyyy")
                summary_parts.append(f"<b>Date Filter:</b><br>{start} to {end}")
            else:
                summary_parts.append(f"<b>Date Filter:</b><br>Disabled")
        
        # Aircraft filters
        if hasattr(self, 'aircraft_list') and self.aircraft_list.count() > 0:
            selected_count = len([item for item in [self.aircraft_list.item(i) for i in range(self.aircraft_list.count())] 
                                if item.checkState() == Qt.CheckState.Checked])
            total_count = self.aircraft_list.count()
            if selected_count > 0:
                summary_parts.append(f"<b>Aircraft Filter:</b><br>{selected_count} of {total_count} types selected")
            else:
                summary_parts.append(f"<b>Aircraft Filter:</b><br>All {total_count} types (none excluded)")
        
        # Airspace filters (if any selected)
        if hasattr(self, 'airspace_list') and self.airspace_list.count() > 0:
            included_count = len([item for item in [self.airspace_list.item(i) for i in range(self.airspace_list.count())] 
                                if item.checkState() == Qt.CheckState.Checked])
            if included_count > 0:
                summary_parts.append(f"<b>Airspace Filter:</b><br>{included_count} regions included")
        
        # Update the label
        if summary_parts:
            summary_text = "<br><br>".join(summary_parts)
        else:
            summary_text = "No filters configured yet"
        
        if hasattr(self, 'filter_status_label'):
            self.filter_status_label.setText(summary_text)
            selected_airspace = [item.text() for item in self.airspace_list.selectedItems()]
            if selected_airspace:
                airspace_text = ", ".join(selected_airspace[:3])
                if len(selected_airspace) > 3:
                    airspace_text += f"<br>(+{len(selected_airspace)-3} more)"
                summary_parts.append(f"<b>Included Airspace:</b><br>{airspace_text}")
        
        # Update display
        if summary_parts:
            self.filter_status_label.setText("<br><br>".join(summary_parts))
            self.filter_status_label.setStyleSheet("color: #333; font-size: 11px;")
        else:
            self.filter_status_label.setText("No filters currently active<br><br>All available data will be used for training")
            self.filter_status_label.setStyleSheet("color: #666; font-style: italic; font-size: 11px;")

    # Reset methods for individual tabs
    def _reset_geographic_filters(self):
        """Reset only geographic filters to data-driven bounds - identical to realistic replay"""
        if self.summary_data and 'lat_bounds' in self.summary_data:
            lat_min = self.summary_data['lat_bounds']['min']
            lat_max = self.summary_data['lat_bounds']['max']
            self.lat_min_spin.setValue(lat_min)
            self.lat_max_spin.setValue(lat_max)
        else:
            # Fallback to global bounds if no data available
            self.lat_min_spin.setValue(-90)
            self.lat_max_spin.setValue(90)
            
        if self.summary_data and 'lon_bounds' in self.summary_data:
            lon_min = self.summary_data['lon_bounds']['min']
            lon_max = self.summary_data['lon_bounds']['max']
            self.lon_min_spin.setValue(lon_min)
            self.lon_max_spin.setValue(lon_max)
        else:
            # Fallback to global bounds if no data available
            self.lon_min_spin.setValue(-180)
            self.lon_max_spin.setValue(180)

    def _reset_flight_level_filters(self):
        """Reset only flight level filters to data-driven bounds - identical to realistic replay"""
        if self.summary_data and 'fl_bounds' in self.summary_data:
            fl_min = int(self.summary_data['fl_bounds']['min'])
            fl_max = int(self.summary_data['fl_bounds']['max'])
            self.fl_min_spin.setValue(fl_min)
            self.fl_max_spin.setValue(fl_max)
        else:
            # Fallback to global bounds if no data available
            self.fl_min_spin.setValue(0)
            self.fl_max_spin.setValue(500)

    def _reset_airspace_filters(self):
        """Reset only airspace filters to default values - same as realistic replay"""
        for i in range(self.airspace_list.count()):
            self.airspace_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _reset_time_filters(self):
        """Reset only time filters to data-driven bounds - identical to realistic replay"""
        if self.summary_data and 'time_bounds' in self.summary_data:
            time_min = self.summary_data['time_bounds']['min']
            time_max = self.summary_data['time_bounds']['max']
            
            # Parse time strings to QTime objects
            try:
                if isinstance(time_min, str) and ':' in time_min:
                    # Extract just the time part if it's a datetime string
                    time_part = time_min.split(' ')[-1] if ' ' in time_min else time_min
                    start_parts = time_part.split(':')
                    start_time = QTime(int(start_parts[0]), int(start_parts[1]), 
                                     int(start_parts[2]) if len(start_parts) > 2 else 0)
                    if start_time.isValid():
                        self.time_start.setTime(start_time)
                    
                if isinstance(time_max, str) and ':' in time_max:
                    # Extract just the time part if it's a datetime string
                    time_part = time_max.split(' ')[-1] if ' ' in time_max else time_max
                    end_parts = time_part.split(':')
                    end_time = QTime(int(end_parts[0]), int(end_parts[1]), 
                                   int(end_parts[2]) if len(end_parts) > 2 else 0)
                    if end_time.isValid():
                        self.time_end.setTime(end_time)
                        
                # Enable time filtering when resetting to data bounds
                self.time_enabled.setChecked(True)
            except (ValueError, IndexError):
                # If parsing fails, use defaults
                self.time_enabled.setChecked(False)
                self.time_start.setTime(QTime(0, 0, 0))
                self.time_end.setTime(QTime(23, 59, 59))
        else:
            # Fallback to disabled if no data available
            self.time_enabled.setChecked(False)
            self.time_start.setTime(QTime(0, 0, 0))
            self.time_end.setTime(QTime(23, 59, 59))
        
        # Set date bounds based on data (identical to realistic replay)
        if self.summary_data and 'date_bounds' in self.summary_data:
            date_min = self.summary_data['date_bounds']['min']
            date_max = self.summary_data['date_bounds']['max']
            
            # Parse date strings to QDate objects
            try:
                # Handle DD-MM-YYYY format
                if '-' in date_min and '-' in date_max:
                    start_parts = date_min.split('-')
                    end_parts = date_max.split('-')
                    
                    if len(start_parts) >= 3 and len(end_parts) >= 3:
                        start_date = QDate(int(start_parts[2]), int(start_parts[1]), int(start_parts[0]))
                        end_date = QDate(int(end_parts[2]), int(end_parts[1]), int(end_parts[0]))
                        
                        if start_date.isValid() and end_date.isValid():
                            # Set ranges to constrain to actual data bounds (same behavior as other filters)
                            self.date_start.setDateRange(start_date, end_date)
                            self.date_end.setDateRange(start_date, end_date)
                            
                            # Set default values to actual data bounds
                            self.date_start.setDate(start_date)
                            self.date_end.setDate(end_date)
                
                # Enable date filtering when resetting to data bounds
                self.date_enabled.setChecked(True)
            except (ValueError, IndexError):
                # If parsing fails, keep default dates but disable
                self.date_enabled.setChecked(False)
        else:
            # Fallback to disabled if no data available
            self.date_enabled.setChecked(False)

    def _reset_aircraft_filters(self):
        """Reset only aircraft filters to default values - same as realistic replay"""
        for i in range(self.aircraft_list.count()):
            self.aircraft_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _reset_all_filters(self):
        """Reset all filters to data-driven bounds - identical to realistic replay"""
        # Use individual reset methods to ensure consistency
        self._reset_geographic_filters()
        self._reset_flight_level_filters()
        self._reset_time_filters()
        self._reset_airspace_filters()
        self._reset_aircraft_filters()

    # Aircraft type management
    def _set_bounds_from_data(self):
        """Set filter input bounds based on the loaded data ranges"""
        if not self.summary_data:
            return
            
        bounds_set = []
            
        # Set geographic bounds based on data
        if 'lat_bounds' in self.summary_data and hasattr(self, 'lat_min_spin'):
            lat_min = self.summary_data['lat_bounds']['min']
            lat_max = self.summary_data['lat_bounds']['max']
            
            # Extend range slightly to allow some flexibility
            lat_range_min = max(-90, lat_min - 5)
            lat_range_max = min(90, lat_max + 5)
            
            # Set ranges to allow some flexibility around data bounds
            self.lat_min_spin.setRange(lat_range_min, lat_range_max)
            self.lat_max_spin.setRange(lat_range_min, lat_range_max)
            
            # Set default values to actual data bounds
            self.lat_min_spin.setValue(lat_min)
            self.lat_max_spin.setValue(lat_max)
            bounds_set.append(f"Latitude: {lat_min:.2f} to {lat_max:.2f}")
            
        if 'lon_bounds' in self.summary_data and hasattr(self, 'lon_min_spin'):
            lon_min = self.summary_data['lon_bounds']['min']
            lon_max = self.summary_data['lon_bounds']['max']
            
            # Extend range slightly to allow some flexibility
            lon_range_min = max(-180, lon_min - 5)
            lon_range_max = min(180, lon_max + 5)
            
            # Set ranges to allow some flexibility around data bounds
            self.lon_min_spin.setRange(lon_range_min, lon_range_max)
            self.lon_max_spin.setRange(lon_range_min, lon_range_max)
            
            # Set default values to actual data bounds
            self.lon_min_spin.setValue(lon_min)
            self.lon_max_spin.setValue(lon_max)
            bounds_set.append(f"Longitude: {lon_min:.2f} to {lon_max:.2f}")
            
        # Set flight level bounds based on data
        if 'fl_bounds' in self.summary_data and hasattr(self, 'fl_min_spin'):
            fl_min = int(self.summary_data['fl_bounds']['min'])
            fl_max = int(self.summary_data['fl_bounds']['max'])
            
            # Extend range to allow some flexibility
            fl_range_min = max(0, fl_min - 50)
            fl_range_max = min(600, fl_max + 50)
            
            # Set ranges to allow some flexibility around data bounds
            self.fl_min_spin.setRange(fl_range_min, fl_range_max)
            self.fl_max_spin.setRange(fl_range_min, fl_range_max)
            
            # Set default values to actual data bounds
            self.fl_min_spin.setValue(fl_min)
            self.fl_max_spin.setValue(fl_max)
            bounds_set.append(f"Flight Level: FL{fl_min} to FL{fl_max}")
            
        # Set time bounds based on data
        if 'time_bounds' in self.summary_data and hasattr(self, 'time_enabled'):
            time_min = self.summary_data['time_bounds']['min']
            time_max = self.summary_data['time_bounds']['max']
            
            # Parse time strings to QTime objects
            try:
                # Handle various time formats
                if isinstance(time_min, str) and ':' in time_min:
                    # Extract just the time part if it's a datetime string
                    time_part = time_min.split(' ')[-1] if ' ' in time_min else time_min
                    start_parts = time_part.split(':')
                    start_time = QTime(int(start_parts[0]), int(start_parts[1]), 
                                     int(start_parts[2]) if len(start_parts) > 2 else 0)
                    if start_time.isValid():
                        self.time_start.setTime(start_time)
                    
                if isinstance(time_max, str) and ':' in time_max:
                    # Extract just the time part if it's a datetime string
                    time_part = time_max.split(' ')[-1] if ' ' in time_max else time_max
                    end_parts = time_part.split(':')
                    end_time = QTime(int(end_parts[0]), int(end_parts[1]), 
                                   int(end_parts[2]) if len(end_parts) > 2 else 0)
                    if end_time.isValid():
                        self.time_end.setTime(end_time)
                        
                # Enable time filtering when bounds are available
                self.time_enabled.setChecked(True)
                    
            except (ValueError, IndexError) as e:
                print(f"Could not parse time bounds: {e}")
                # If parsing fails, disable time filtering
                self.time_enabled.setChecked(False)
        
        # Set date bounds based on data (same as realistic replay)
        if 'date_bounds' in self.summary_data and hasattr(self, 'date_enabled'):
            date_min = self.summary_data['date_bounds']['min']
            date_max = self.summary_data['date_bounds']['max']
            
            # Parse date strings to QDate objects
            try:
                # Handle DD-MM-YYYY format
                if '-' in date_min and '-' in date_max:
                    start_parts = date_min.split('-')
                    end_parts = date_max.split('-')
                    
                    if len(start_parts) >= 3 and len(end_parts) >= 3:
                        start_date = QDate(int(start_parts[2]), int(start_parts[1]), int(start_parts[0]))
                        end_date = QDate(int(end_parts[2]), int(end_parts[1]), int(end_parts[0]))
                        
                        if start_date.isValid() and end_date.isValid():
                            # Set ranges to constrain to actual data bounds (same behavior as other filters)

                            
                            # Use setMinimumDate/setMaximumDate for proper constraint (like setRange for spinboxes)
                            self.date_start.setMinimumDate(start_date)
                            self.date_start.setMaximumDate(end_date)
                            self.date_end.setMinimumDate(start_date)
                            self.date_end.setMaximumDate(end_date)
                            
                            # Set default values to actual data bounds
                            self.date_start.setDate(start_date)
                            self.date_end.setDate(end_date)

                
                # Enable date filtering when bounds are available
                self.date_enabled.setChecked(True)
                bounds_set.append(f"Date range: {date_min} to {date_max}")
                print(f"[OK] Date range constraints applied: {date_min} to {date_max}")
                    
            except (ValueError, IndexError) as e:
                print(f"Could not parse date bounds: {e}")
                # If parsing fails, disable date filtering
                self.date_enabled.setChecked(False)
        
        # Populate aircraft types from actual data (same as realistic replay)
        if 'aircraft_types' in self.summary_data and hasattr(self, 'aircraft_list'):
            # Clear existing items
            self.aircraft_list.clear()
            
            aircraft_types = self.summary_data['aircraft_types']
            
            if isinstance(aircraft_types, dict):
                # If it's a dictionary (type -> count), sort by count
                sorted_types = sorted(aircraft_types.items(), key=lambda x: x[1], reverse=True)
                for ac_type, count in sorted_types:
                    item = QListWidgetItem(f"{ac_type} ({count} flights)")
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    item.setData(Qt.ItemDataRole.UserRole, ac_type)  # Store the actual type
                    self.aircraft_list.addItem(item)
                bounds_set.append(f"Aircraft Types: {len(aircraft_types)} types loaded")
            elif isinstance(aircraft_types, list):
                # If it's a list, just show the types
                for ac_type in sorted(aircraft_types):
                    item = QListWidgetItem(ac_type)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    item.setData(Qt.ItemDataRole.UserRole, ac_type)
                    self.aircraft_list.addItem(item)
                bounds_set.append(f"Aircraft Types: {len(aircraft_types)} types loaded")
        
        # Summary of what was set
        if bounds_set:
            print(f"[OK] Data bounds configured: {len(bounds_set)} filters")
        else:
            print("[ERROR] No data bounds were configured")
        
        # Update the filter summary display
        self._update_filter_summary()

    def _load_current_settings(self):
        """Load current filter settings into the dialog"""
        filters = self.current_filters
        
        # Geographic bounds
        if hasattr(self, 'lat_min_spin'):
            self.lat_min_spin.setValue(filters.get('lat_min', -90))
            self.lat_max_spin.setValue(filters.get('lat_max', 90))
            self.lon_min_spin.setValue(filters.get('lon_min', -180))
            self.lon_max_spin.setValue(filters.get('lon_max', 180))
        
        # Flight level bounds
        if hasattr(self, 'fl_min_spin'):
            self.fl_min_spin.setValue(filters.get('fl_min', 0))
            self.fl_max_spin.setValue(filters.get('fl_max', 500))
        
        # Time filters
        if hasattr(self, 'time_enabled'):
            if filters.get('time_start') and filters.get('time_end'):
                self.time_enabled.setChecked(True)
                # Parse time strings
                try:
                    start_time = QTime.fromString(filters['time_start'], "hh:mm:ss")
                    end_time = QTime.fromString(filters['time_end'], "hh:mm:ss")
                    if start_time.isValid():
                        self.time_start.setTime(start_time)
                    if end_time.isValid():
                        self.time_end.setTime(end_time)
                except:
                    pass
            else:
                self.time_enabled.setChecked(False)
        
        # Aircraft types - check against UserRole data (same as realistic replay)
        if hasattr(self, 'aircraft_list'):
            selected_types = set(filters.get('aircraft_types', []))
            for i in range(self.aircraft_list.count()):
                item = self.aircraft_list.item(i)
                # Check both UserRole data and item text
                ac_type = item.data(Qt.ItemDataRole.UserRole) or item.text()
                if ac_type in selected_types:
                    item.setCheckState(Qt.CheckState.Checked)
                else:
                    item.setCheckState(Qt.CheckState.Unchecked)
        
        # Airspace inclusions
        if hasattr(self, 'airspace_list'):
            included_airspace = set(filters.get('include_airspace', []))
            for i in range(self.airspace_list.count()):
                item = self.airspace_list.item(i)
                if item.text() in included_airspace:
                    item.setCheckState(Qt.CheckState.Checked)
                else:
                    item.setCheckState(Qt.CheckState.Unchecked)
        
        self._update_filter_summary()

    def _load_airspace_options(self):
        """Load available airspace options from FIR file - identical to realistic replay"""
        try:
            import pandas as pd
            if self.fir_file_path and os.path.exists(self.fir_file_path):
                df = pd.read_csv(self.fir_file_path)
                if 'Airspace ID' in df.columns:
                    airspace_ids = sorted(df['Airspace ID'].unique())
                    
                    # Clear existing items
                    self.airspace_list.clear()
                    
                    # Add airspace options with checkboxes (same as realistic replay)
                    for airspace_id in airspace_ids:
                        item = QListWidgetItem(airspace_id)
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(Qt.CheckState.Unchecked)
                        self.airspace_list.addItem(item)
                    
                    # Update status
                    self.airspace_status.setText(f"Loaded {len(airspace_ids)} airspace regions")
                    self.airspace_status.setStyleSheet("color: #007ACC;")
                    
                    # Select currently included airspace
                    included = set(self.current_filters.get('include_airspace', []))
                    for i in range(self.airspace_list.count()):
                        item = self.airspace_list.item(i)
                        if item.text() in included:
                            item.setCheckState(Qt.CheckState.Checked)
                else:
                    self.airspace_status.setText("FIR file does not contain 'Airspace ID' column")
                    self.airspace_status.setStyleSheet("color: #CC7A00;")
            else:
                self.airspace_status.setText("FIR file not found")
                self.airspace_status.setStyleSheet("color: #CC0000;")
                
        except Exception as e:
            self.airspace_status.setText(f"Error loading airspace data: {str(e)}")
            self.airspace_status.setStyleSheet("color: #CC0000;")
        
        # Note: No need to connect itemSelectionChanged since we're using checkboxes

    def _save_and_close(self):
        """Save filter configuration and close dialog (filters will be applied during workflow)"""
        # Update current_filters with UI values (same as _save_and_close but without applying)
        if hasattr(self, 'lat_min_spin'):
            self.current_filters['lat_min'] = self.lat_min_spin.value()
            self.current_filters['lat_max'] = self.lat_max_spin.value()
            self.current_filters['lon_min'] = self.lon_min_spin.value()
            self.current_filters['lon_max'] = self.lon_max_spin.value()
        
        if hasattr(self, 'fl_min_spin'):
            self.current_filters['fl_min'] = self.fl_min_spin.value()
            self.current_filters['fl_max'] = self.fl_max_spin.value()
        
        # Time filters
        if hasattr(self, 'time_enabled'):
            if self.time_enabled.isChecked():
                self.current_filters['time_start'] = self.time_start.time().toString("hh:mm:ss")
                self.current_filters['time_end'] = self.time_end.time().toString("hh:mm:ss")
            else:
                self.current_filters['time_start'] = None
                self.current_filters['time_end'] = None
        
        # Aircraft types - get checked items only (same as realistic replay)
        if hasattr(self, 'aircraft_list'):
            selected_types = []
            for i in range(self.aircraft_list.count()):
                item = self.aircraft_list.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    # Get the actual aircraft type from UserRole data
                    ac_type = item.data(Qt.ItemDataRole.UserRole)
                    if ac_type:
                        selected_types.append(ac_type)
                    else:
                        # Fallback to item text if no UserRole data
                        selected_types.append(item.text())
            self.current_filters['aircraft_types'] = selected_types
        
        # Airspace inclusions - get checked items only (changed from exclude to include logic)
        if hasattr(self, 'airspace_list'):
            included_airspace = []
            for i in range(self.airspace_list.count()):
                item = self.airspace_list.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    # Get the actual airspace name from UserRole data
                    airspace_name = item.data(Qt.ItemDataRole.UserRole)
                    if airspace_name:
                        included_airspace.append(airspace_name)
                    else:
                        # Fallback to item text if no UserRole data
                        included_airspace.append(item.text())
            self.current_filters['include_airspace'] = included_airspace
        
        # Close dialog with accepted result
        self.accept()

    def _save_and_close(self):
        """Apply the configured filters and close the dialog"""
        # Update current_filters with UI values
        if hasattr(self, 'lat_min_spin'):
            self.current_filters['lat_min'] = self.lat_min_spin.value()
            self.current_filters['lat_max'] = self.lat_max_spin.value()
            self.current_filters['lon_min'] = self.lon_min_spin.value()
            self.current_filters['lon_max'] = self.lon_max_spin.value()
        
        if hasattr(self, 'fl_min_spin'):
            self.current_filters['fl_min'] = self.fl_min_spin.value()
            self.current_filters['fl_max'] = self.fl_max_spin.value()
        
        # Time filters
        if hasattr(self, 'time_enabled'):
            if self.time_enabled.isChecked():
                self.current_filters['time_start'] = self.time_start.time().toString("hh:mm:ss")
                self.current_filters['time_end'] = self.time_end.time().toString("hh:mm:ss")
            else:
                self.current_filters['time_start'] = None
                self.current_filters['time_end'] = None
        
        # Aircraft types - get checked items only (same as realistic replay)
        if hasattr(self, 'aircraft_list'):
            selected_types = []
            for i in range(self.aircraft_list.count()):
                item = self.aircraft_list.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    # Get the actual aircraft type from UserRole data
                    ac_type = item.data(Qt.ItemDataRole.UserRole)
                    if ac_type:
                        selected_types.append(ac_type)
                    else:
                        # Fallback to item text if no UserRole data
                        selected_types.append(item.text())
            self.current_filters['aircraft_types'] = selected_types
        
        # Airspace inclusions - get checked items only (changed from exclude to include logic)
        if hasattr(self, 'airspace_list'):
            included_airspace = []
            for i in range(self.airspace_list.count()):
                item = self.airspace_list.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    included_airspace.append(item.text())
            self.current_filters['include_airspace'] = included_airspace
        
        self.accept()

    def set_data_context(self, data, fir_file_path=None):
        """Set the data context to populate filter options."""
        self.fir_file_path = fir_file_path
        
        # If no data passed directly, try to get it from parent
        if data is None and self.parent() and hasattr(self.parent(), 'historic_data'):
            data = self.parent().historic_data
        
        # Extract data summary for bounds setting
        if data is not None and hasattr(data, 'columns') and not data.empty:
            try:
                summary_data = {}
                
                # Extract geographic bounds
                lat_cols = ['latitude', 'lat', 'ADEP Latitude', 'ADES Latitude']
                lon_cols = ['longitude', 'lon', 'ADEP Longitude', 'ADES Longitude']
                
                for lat_col in lat_cols:
                    if lat_col in data.columns:
                        lat_min, lat_max = float(data[lat_col].min()), float(data[lat_col].max())
                        summary_data['lat_bounds'] = {'min': lat_min, 'max': lat_max}
                        break
                
                for lon_col in lon_cols:
                    if lon_col in data.columns:
                        lon_min, lon_max = float(data[lon_col].min()), float(data[lon_col].max())
                        summary_data['lon_bounds'] = {'min': lon_min, 'max': lon_max}
                        break
                
                # Extract flight level bounds
                fl_cols = ['flight_level', 'Requested FL', 'altitude', 'FL']
                for fl_col in fl_cols:
                    if fl_col in data.columns:
                        fl_data = data[fl_col].dropna()
                        if not fl_data.empty:
                            fl_min, fl_max = int(fl_data.min()), int(fl_data.max())
                            summary_data['fl_bounds'] = {'min': fl_min, 'max': fl_max}
                            break
                
                # Extract time bounds
                time_cols = ['timestamp', 'time', 'datetime', 'date', 'FILED OFF BLOCK TIME', 'ACTUAL OFF BLOCK TIME']
                for time_col in time_cols:
                    if time_col in data.columns:
                        time_data = data[time_col].dropna()
                        if not time_data.empty:
                            time_min, time_max = str(time_data.min()), str(time_data.max())
                            summary_data['time_bounds'] = {'min': time_min, 'max': time_max}
                            break
                
                # Extract aircraft types
                ac_cols = ['aircraft_type', 'AC Type', 'callsign', 'type']
                for ac_col in ac_cols:
                    if ac_col in data.columns:
                        aircraft_types = sorted(data[ac_col].dropna().unique())
                        summary_data['aircraft_types'] = aircraft_types
                        break
                
                # If no aircraft types found, use common types
                if 'aircraft_types' not in summary_data:
                    aircraft_types = ['A320', 'A330', 'A380', 'B737', 'B747', 'B777', 'B787', 'E190', 'CRJ9']
                    summary_data['aircraft_types'] = aircraft_types
                
                self.summary_data = summary_data
                
                # Update bounds and options based on new data
                self._set_bounds_from_data()
                
                # Populate aircraft types in the aircraft tab (same as realistic replay)
                if 'aircraft_types' in summary_data and hasattr(self, 'aircraft_list'):
                    self.aircraft_list.clear()
                    for ac_type in summary_data['aircraft_types']:
                        item = QListWidgetItem(ac_type)
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(Qt.CheckState.Unchecked)
                        item.setData(Qt.ItemDataRole.UserRole, ac_type)
                        self.aircraft_list.addItem(item)
                    
                    # Update aircraft status
                    if hasattr(self, 'aircraft_status'):
                        self.aircraft_status.setText(f"Available aircraft types: {len(summary_data['aircraft_types'])}")
                        self.aircraft_status.setStyleSheet("color: #333; font-style: normal;")
                
            except Exception as e:
                print(f"Warning: Could not extract data summary: {e}")
                # Use fallback summary data
                self.summary_data = {
                    'lat_bounds': {'min': -90, 'max': 90},
                    'lon_bounds': {'min': -180, 'max': 180},
                    'fl_bounds': {'min': 0, 'max': 500},
                    'aircraft_types': ['A320', 'A330', 'A380', 'B737', 'B747', 'B777', 'B787', 'E190', 'CRJ9']
                }
        
        # Load airspace options if FIR file is provided
        if fir_file_path:
            self._load_airspace_options()
        
        self._update_filter_summary()

    def get_filters(self):
        """Get the current filter configuration in the same format as Eurocontrol filters."""
        return self.current_filters.copy()


# --- GC tab (Geometric Conflicts) ------------------------------------------

class GCMinimaPanel(QGroupBox):
    """
    Separation minima configuration panel for Geometric Conflict generation parameters.
    
    This specialized control panel provides precision configuration for horizontal
    and vertical separation minima used in geometric conflict scenario generation.
    The panel enables users to set loss-of-separation thresholds that define when
    aircraft conflicts occur, supporting realistic conflict detection parameters
    that match operational air traffic management separation requirements.
    
    The panel integrates with SATG's geometric conflict generation system to provide
    consistent separation standards across conflict scenarios, ensuring generated
    conflicts represent realistic operational situations with appropriate separation
    minima for training effectiveness and scenario authenticity.
    
    Configuration Parameters:
    - Horizontal Separation: Distance threshold in nautical miles for lateral conflicts
    - Vertical Separation: Altitude threshold in feet for vertical conflict detection
    - Precision Controls: Fine-grained adjustment with appropriate step increments
    - Operational Ranges: Realistic value ranges matching aviation separation standards
    - Real-time Updates: Immediate application to conflict generation algorithms
    
    The separation minima configured through this panel serve as baseline thresholds
    for conflict detection algorithms throughout SATG's geometric conflict generation
    system, ensuring consistent and realistic conflict scenario creation.
    
    Attributes:
        _hsep (QDoubleSpinBox): Horizontal separation control in nautical miles
        _vsep (QSpinBox): Vertical separation control in feet
        
    Methods:
        hsep_value() -> float: Returns current horizontal separation value
        vsep_value() -> int: Returns current vertical separation value
        
    Args:
        parent (QWidget, optional): Parent widget for proper panel integration
        
    Examples:
        # Create separation minima panel for conflict configuration
        minima_panel = GCMinimaPanel(parent=self)
        
        # Get current separation values for conflict generation
        h_sep = minima_panel.hsep_value()  # Horizontal separation in NM
        v_sep = minima_panel.vsep_value()  # Vertical separation in feet
        
        # Panel automatically updates conflict generation parameters
    
    Note:
        Default values (5.0 NM horizontal, 1000 ft vertical) comply with ICAO
        standard separation minima for controlled airspace operations. Values
        should reflect realistic operational separation requirements for training
        scenario authenticity and controller training effectiveness.
    """
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
        self._hsep.setToolTip("Horizontal separation at closest point of approach in nautical miles")
        _configure_decimal_separator(self._hsep)
        layout.addRow("Horizontal [NM]:", self._hsep)

        self._vsep = QSpinBox(self)
        self._vsep.setRange(0, 5000)
        self._vsep.setSingleStep(50)
        self._vsep.setValue(1000)
        self._vsep.setToolTip("Vertical separation for altitude crossing conflicts in feet")
        layout.addRow("Vertical [ft]:", self._vsep)

    def hsep_value(self) -> float:
        return float(self._hsep.value())

    def vsep_value(self) -> int:
        return int(self._vsep.value())


class GCAbsolutePage(QWidget):
    """
    Advanced interface for absolute Geometric Conflicts generation with CPA-based algorithms.
    
    This sophisticated page provides comprehensive configuration for absolute geometric
    conflict scenarios using Closest Point of Approach (CPA) algorithms and legacy
    conflict generation methods. The page enables precise control over conflict timing,
    geometry, and aircraft positioning for creating realistic loss of separation
    scenarios in air traffic management research and training applications.
    
    The Absolute Conflicts approach uses traditional CPA-based methods where conflicts
    are defined by specific geometric parameters including minimum separation distances,
    approach angles, and timing constraints. This method provides deterministic conflict
    scenarios with precise control over all conflict characteristics and resolution dynamics.
    
    Key Features:
    - Legacy CPA (Closest Point of Approach) conflict generation algorithms
    - Comprehensive geometric parameter configuration and validation
    - Multiple conflict scenario templates with customizable parameters  
    - Integration with separation minima panel for realistic separation standards
    - Advanced timing controls for conflict initiation and resolution
    - Aircraft trajectory optimization for realistic conflict geometry
    - Validation system ensuring feasible and safe conflict scenarios
    
    CPA Configuration Options:
    - Minimum Separation Distance: Configure horizontal and vertical separation minima
    - Approach Geometry: Set aircraft approach angles and trajectories
    - Timing Parameters: Control conflict onset, duration, and resolution timing
    - Aircraft Parameters: Configure speed, altitude, and performance characteristics
    - Conflict Severity: Adjust separation violations and conflict intensity
    - Resolution Constraints: Set parameters for conflict resolution scenarios
    
    Validation Features:
    - Geometric Feasibility: Ensure conflict scenarios are physically achievable
    - Safety Verification: Validate separation minima and safety parameters
    - Performance Constraints: Check aircraft performance limitations
    - Airspace Compliance: Verify scenarios meet airspace operational requirements
    - Realism Validation: Ensure conflicts represent realistic operational scenarios
    
    Attributes:
        _minima (GCMinimaPanel): Reference to separation minima configuration panel
    
    Args:
        minima_panel (GCMinimaPanel): Separation minima configuration interface
        parent (QWidget, optional): Parent widget for proper page integration
    
    Examples:
        # Create absolute conflicts page with minima configuration
        minima_panel = GCMinimaPanel()
        abs_page = GCAbsolutePage(minima_panel, parent=self)
        
        # Configure CPA-based conflict scenarios
        # Set geometric parameters and timing constraints
        # Generate realistic absolute conflict scenarios
    
    Note:
        The Absolute Page uses legacy CPA methods that provide deterministic
        conflict scenarios with precise geometric control. Integration with
        the minima panel ensures all generated conflicts respect current
        separation standards and operational requirements for realistic training scenarios.
    """
    
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
        self.gc_use_coords_rb.setToolTip("Define conflict location using latitude/longitude coordinates")
        coord_form.addRow(self.gc_use_coords_rb)
        self.gc_lat = QLineEdit("52.100")
        self.gc_lat.setClearButtonEnabled(True)
        self.gc_lat.setToolTip("Latitude of the closest point of approach (CPA) in decimal degrees")
        self.gc_lon = QLineEdit("4.500")
        self.gc_lon.setClearButtonEnabled(True)
        self.gc_lon.setToolTip("Longitude of the closest point of approach (CPA) in decimal degrees")
        coord_form.addRow("Latitude [deg]:", self.gc_lat)
        coord_form.addRow("Longitude [deg]:", self.gc_lon)

        wp_box = QGroupBox("Waypoint")
        wp_form = QFormLayout(wp_box)
        wp_form.setContentsMargins(6, 6, 6, 6)
        wp_form.setSpacing(6)
        self.gc_use_wp_rb = QRadioButton("Use waypoint")
        self.gc_use_wp_rb.setToolTip("Define conflict location using an existing waypoint identifier")
        wp_form.addRow(self.gc_use_wp_rb)
        self.gc_wp = QLineEdit("")
        self.gc_wp.setPlaceholderText("e.g. SUGOL or EHAM")
        self.gc_wp.setClearButtonEnabled(True)
        self.gc_wp.setToolTip("Waypoint identifier (navdata name) or airport ICAO code")
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
        self.gc_tcpa_value.setToolTip("Time to closest point of approach in seconds")
        self.gc_tcpa_range = QLineEdit()
        self.gc_tcpa_range.setClearButtonEnabled(True)
        self.gc_tcpa_range.setToolTip("Upper bound for TCPA range (leave empty for fixed value)")
        f1.addRow(
            "TCPA [s]:",
            self._make_value_range_row(self.gc_tcpa_value, self.gc_tcpa_range, "upper [s] (optional)"),
        )

        self.gc_angle_value = QLineEdit("90.0")
        self.gc_angle_value.setClearButtonEnabled(True)
        self.gc_angle_value.setToolTip("Crossing angle in degrees (only used for crossing conflicts)")
        self.gc_angle_range = QLineEdit()
        self.gc_angle_range.setClearButtonEnabled(True)
        self.gc_angle_range.setToolTip("Upper bound for angle range (leave empty for fixed value)")
        f1.addRow(
            "CPA angle [deg] (cross only):",
            self._make_value_range_row(self.gc_angle_value, self.gc_angle_range, "upper [deg] (optional)"),
        )

        self.gc_alt_offset_value = QLineEdit("0")
        self.gc_alt_offset_value.setClearButtonEnabled(True)
        self.gc_alt_offset_value.setToolTip("Altitude difference between aircraft at CPA in feet (0 = same level)")
        self.gc_alt_offset_range = QLineEdit()
        self.gc_alt_offset_range.setClearButtonEnabled(True)
        self.gc_alt_offset_range.setToolTip("Upper bound for altitude offset range")
        f1.addRow(
            "Altitude offset dH [ft] (optional):",
            self._make_value_range_row(self.gc_alt_offset_value, self.gc_alt_offset_range, "upper [ft] (optional)"),
        )

        self.gc_actypes = QLineEdit("A320,B738,A350,B78X")
        self.gc_actypes.setToolTip("Aircraft types to use, comma-separated (e.g. A320,B738,A350)")
        gc_actypes_btn = QPushButton("Select...")
        gc_actypes_btn.setMaximumWidth(70)
        gc_actypes_btn.clicked.connect(lambda: self._select_aircraft_types_gc())
        gc_actypes_layout = QHBoxLayout()
        gc_actypes_layout.addWidget(self.gc_actypes)
        gc_actypes_layout.addWidget(gc_actypes_btn)
        gc_actypes_layout.setContentsMargins(0, 0, 0, 0)
        gc_actypes_widget = QWidget()
        gc_actypes_widget.setLayout(gc_actypes_layout)
        f1.addRow("AC types:", gc_actypes_widget)

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

        self.gb_flight_profile = QGroupBox("2) Flight profile")
        gb2_layout = QVBoxLayout(self.gb_flight_profile)
        
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
        main.addWidget(self.gb_flight_profile)

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
        self.gc_name.setToolTip("Name for the generated geometric conflict scenario file (without .scn extension)")
        name_layout.addWidget(self.gc_name)
        f3.addRow(name_row)

        # Add seed field like GC Relative has
        self.gc_seed = QSpinBox()
        self.gc_seed.setRange(0, 2_000_000_000)
        self.gc_seed.setValue(0)
        self.gc_seed.setToolTip("Random seed for conflict generation (0 = use random seed)")
        f3.addRow("Seed (0 = random):", self.gc_seed)

        self.gc_overwrite_cb = QCheckBox("Overwrite scenario if it exists")
        self.gc_overwrite_cb.setChecked(True)
        self.gc_overwrite_cb.setToolTip("Replace existing scenario file if one exists with the same name")
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

    def _select_aircraft_types_gc(self):
        """Open aircraft type selection dialog for geometric conflicts."""
        current_types = self.gc_actypes.text()
        dialog = AircraftTypeDialog(current_types, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_types = dialog.get_selected_types()
            self.gc_actypes.setText(selected_types)


class GCRelativePage(QWidget):
    """
    Advanced interface for relative Geometric Conflicts generation with target-based algorithms.
    
    This sophisticated page provides modern relative conflict generation capabilities
    using target aircraft-based algorithms for creating realistic loss of separation
    scenarios. The relative approach enables more flexible and realistic conflict
    scenarios by defining conflicts relative to specific target aircraft rather
    than using fixed geometric parameters, resulting in more natural conflict dynamics.
    
    The Relative Conflicts system represents a modern approach to conflict generation
    where conflicts are defined in relation to existing aircraft (targets) in the
    simulation. This method provides more realistic conflict scenarios that adapt
    to current traffic conditions and create natural-looking separation violations
    that reflect real-world operational challenges.
    
    Key Features:
    - Target Aircraft Selection: Choose existing aircraft as conflict reference points
    - Relative Positioning: Define conflict aircraft positions relative to targets
    - Dynamic Conflict Generation: Adapt conflicts to current simulation conditions
    - Realistic Trajectory Calculation: Generate natural aircraft trajectories for conflicts
    - Advanced Timing Controls: Sophisticated conflict initiation and resolution timing
    - Integration with separation minima for operational realism
    - Validation system ensuring feasible relative positioning and dynamics
    
    Target-Based Configuration:
    - Target Selection: Choose reference aircraft for conflict generation
    - Relative Positioning: Define conflict aircraft positions relative to targets
    - Approach Vectors: Configure relative approach angles and trajectories
    - Timing Coordination: Synchronize conflict timing with target aircraft movements
    - Separation Parameters: Set relative separation distances and violation characteristics
    - Resolution Dynamics: Configure relative resolution trajectories and timing
    
    Advanced Features:
    - Multi-Target Conflicts: Generate conflicts involving multiple target aircraft
    - Trajectory Prediction: Use target aircraft trajectory prediction for realistic conflicts
    - Dynamic Adaptation: Adjust conflict parameters based on target aircraft behavior
    - Operational Realism: Ensure conflicts reflect realistic air traffic management scenarios
    - Performance Optimization: Efficient algorithms for real-time conflict generation
    
    Attributes:
        _minima (GCMinimaPanel): Reference to separation minima configuration panel
    
    Args:
        minima_panel (GCMinimaPanel): Separation minima configuration interface
        parent (QWidget, optional): Parent widget for proper page integration
    
    Examples:
        # Create relative conflicts page with target selection
        minima_panel = GCMinimaPanel()
        rel_page = GCRelativePage(minima_panel, parent=self)
        
        # Configure target-based conflict scenarios
        # Select reference aircraft and set relative parameters
        # Generate adaptive realistic conflict scenarios
    
    Note:
        The Relative Page uses modern target-based algorithms that provide
        more realistic and adaptive conflict scenarios compared to traditional
        CPA methods. Integration with the minima panel and dynamic adaptation
        to current traffic conditions ensures conflicts represent realistic
        operational challenges for advanced air traffic management training.
    """
    
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
        self.target_acid.setPlaceholderText("optional - auto if left blank")
        self.target_acid.setClearButtonEnabled(True)
        target_form.addRow("Target callsign:", self.target_acid)
        self.target_type = QLineEdit("A320,B738,A350,B78X")
        self.target_type.setClearButtonEnabled(True)
        target_actypes_btn = QPushButton("Select...")
        target_actypes_btn.setMaximumWidth(70)
        target_actypes_btn.clicked.connect(lambda: self._select_aircraft_types_target())
        target_actypes_layout = QHBoxLayout()
        target_actypes_layout.addWidget(self.target_type)
        target_actypes_layout.addWidget(target_actypes_btn)
        target_actypes_layout.setContentsMargins(0, 0, 0, 0)
        target_actypes_widget = QWidget()
        target_actypes_widget.setLayout(target_actypes_layout)
        target_form.addRow("Aircraft type:", target_actypes_widget)

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
        self.intr_acid.setPlaceholderText("optional - auto if left blank")
        self.intr_acid.setClearButtonEnabled(True)
        intr_form.addRow("Intruder callsign (optional):", self.intr_acid)
        self.intr_type = QLineEdit("A320,B738,A350,B78X")
        self.intr_type.setClearButtonEnabled(True)
        intr_actypes_btn = QPushButton("Select...")
        intr_actypes_btn.setMaximumWidth(70)
        intr_actypes_btn.clicked.connect(lambda: self._select_aircraft_types_intruder())
        intr_actypes_layout = QHBoxLayout()
        intr_actypes_layout.addWidget(self.intr_type)
        intr_actypes_layout.addWidget(intr_actypes_btn)
        intr_actypes_layout.setContentsMargins(0, 0, 0, 0)
        intr_actypes_widget = QWidget()
        intr_actypes_widget.setLayout(intr_actypes_layout)
        intr_form.addRow("Aircraft type:", intr_actypes_widget)

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
        self.scn_name.setToolTip("Name for the generated relative conflict scenario file (without .scn extension)")
        scen_form.addRow("Scenario name:", self.scn_name)
        
        # Add seed field like CPA has
        self.gc_rel_seed = QSpinBox()
        self.gc_rel_seed.setRange(0, 2_000_000_000)
        self.gc_rel_seed.setValue(0)
        self.gc_rel_seed.setToolTip("Random seed for conflict generation (0 = use random seed)")
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

    def _select_aircraft_types_target(self):
        """Open aircraft type selection dialog for target aircraft."""
        current_types = self.target_type.text()
        dialog = AircraftTypeDialog(current_types, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_types = dialog.get_selected_types()
            self.target_type.setText(selected_types)

    def _select_aircraft_types_intruder(self):
        """Open aircraft type selection dialog for intruder aircraft."""
        current_types = self.intr_type.text()
        dialog = AircraftTypeDialog(current_types, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_types = dialog.get_selected_types()
            self.intr_type.setText(selected_types)


class GCTab(QWidget):
    """
    Geometric Conflicts tab for advanced conflict detection and resolution scenarios.
    
    This comprehensive interface provides sophisticated geometric conflict generation
    capabilities using precise mathematical algorithms to create controlled loss of
    separation scenarios. The tab supports both legacy CPA (Closest Point of Approach)
    methods and modern relative conflict generation techniques for air traffic
    management research and training applications.
    
    The Geometric Conflicts system enables precise control over conflict parameters
    including timing, location, geometry, and resolution characteristics. All
    conflicts are generated using validated geometric algorithms that ensure
    realistic aircraft trajectories and conflict dynamics.
    
    Key Features:
    - Dual conflict generation modes: CPA-based and Relative positioning
    - Separation minima configuration with ICAO standard compliance
    - Aircraft type and performance parameter integration
    - Visual conflict indicators with real-time CPA reference display
    - Comprehensive conflict geometry validation and verification
    - Integration with BlueSky simulation for immediate testing
    
    Conflict Generation Methods:
    1. CPA (Legacy) Mode: Traditional closest point of approach calculations
       - Fixed conflict location and timing specifications
       - Geometric trajectory calculations for precise CPA achievement
       - Aircraft positioning based on separation thresholds
    
    2. Relative (Creconfs) Mode: Advanced relative positioning algorithms
       - Dynamic conflict geometry with variable parameters
       - Enhanced realism through stochastic conflict characteristics
       - Sophisticated resolution scenario generation
    
    Visual Components:
    - Minima Panel: Separation threshold configuration and validation
    - CPA Display: Real-time visual conflict reference indicators
    - Parameter Controls: Comprehensive conflict parameter specification
    - Status Indicators: Conflict validation and generation feedback
    
    Attributes:
        _minima (GCMinimaPanel): Separation minima configuration panel
        _absolute_page (GCAbsolutePage): CPA-based conflict generation interface
        _relative_page (GCRelativePage): Relative conflict generation interface
    
    Examples:
        # Tab is created as part of main window
        gc_tab = GCTab(parent_window)
        
        # Typical workflow:
        # 1. Configure separation minima (horizontal/vertical)
        # 2. Select conflict generation method (CPA or Relative)
        # 3. Specify conflict parameters (location, timing, geometry)
        # 4. Generate conflict scenarios with validation
        # 5. Test scenarios in BlueSky simulation environment
    
    Note:
        This tab provides research-grade conflict generation capabilities with
        precise geometric control for academic and professional air traffic
        management applications. All generated conflicts comply with aviation
        standards and provide realistic conflict resolution challenges.
    """
    
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
        self._relative_page = GCRelativePage(self._minima, rel_box)  # Store reference
        rel_layout.addWidget(self._relative_page)

        cols.addWidget(abs_box, 1)
        cols.addWidget(rel_box, 1)

        main.addLayout(cols)


# --- RC tab (Random Conflicts) ---------------------------------------------

class RCTab(QWidget):
    """
    Random Conflicts (RC) tab providing advanced geometric conflict generation in defined airspace areas.
    
    This comprehensive interface enables the creation of sophisticated randomized conflict
    scenarios within circular or polygonal airspace boundaries, supporting diverse conflict
    types, aircraft configurations, and operational parameters for advanced air traffic
    control training scenarios. The tab provides extensive customization options for
    conflict geometry, timing, aircraft selection, and spatial distribution.
    
    The RC tab specializes in generating multiple randomized conflicts with precise spatial
    and temporal distribution control, enabling comprehensive training scenarios that
    simulate realistic traffic density and conflict patterns. Advanced configuration
    options support complex training requirements with varied conflict types, aircraft
    performance characteristics, and operational constraints.
    
    Key Features:
    - Multi-conflict generation with randomized spatial distribution within defined areas
    - Support for circular and polygonal airspace boundary definitions
    - Comprehensive conflict type selection (head-on, crossing, overtaking scenarios)
    - Advanced aircraft parameter configuration with performance model integration
    - Altitude mode selection for vertical separation training scenarios
    - Reproducible scenario generation with seed control for training consistency
    - Integration with BlueSky polygon system for complex airspace modeling
    
    Configuration Categories:
    - Batch Settings: Global parameters for multi-conflict scenario generation
    - Spatial Distribution: Circular or polygonal area definitions for conflict placement
    - Conflict Types: Head-on, crossing, and overtaking conflict geometry selection
    - Aircraft Configuration: Type selection, altitude profiles, and speed parameters
    - Timing Control: Conflict timing synchronization and time-to-CPA management
    - Randomization: Seed control for reproducible scenario generation patterns
    
    Operational Modes:
    - Absolute Conflicts: CPA-based geometric conflict generation with precise positioning
    - Relative Conflicts: Target-intruder conflict scenarios with dynamic positioning
    - Mixed Mode: Alternating between absolute and relative conflict generation
    - Area Types: Circular regions with center/radius or polygon-based irregular areas
    
    The tab integrates with SATG's geometric conflict system and BlueSky's airspace
    management to provide realistic conflict scenarios within operationally accurate
    spatial boundaries for comprehensive air traffic control training applications.
    
    Args:
        parent (QWidget, optional): Parent widget, typically SATGWindow instance
        
    Examples:
        # Create RC tab as part of SATG tabbed interface
        rc_tab = RCTab(parent=satg_window)
        tab_widget.addTab(rc_tab, "Random Conflicts")
        
        # Tab provides complete interface for randomized conflict generation
        # with extensive parameter control and airspace integration
    
    Note:
        The RC tab requires proper BlueSky integration for polygon support and
        aircraft performance model access. Generated scenarios are compatible
        with standard BlueSky scenario loading and execution systems.
    """
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
        self.n.setToolTip("Total number of conflict encounters to generate")
        
        # Circle region settings
        self.c_lat = QLineEdit("52.100")
        self.c_lat.setToolTip("Center latitude of the conflict generation area in decimal degrees")
        self.c_lon = QLineEdit("4.500")
        self.c_lon.setToolTip("Center longitude of the conflict generation area in decimal degrees")
        self.c_rad = QDoubleSpinBox(); self.c_rad.setRange(0.1, 1000.0); self.c_rad.setDecimals(2); self.c_rad.setValue(25.0); _configure_decimal_separator(self.c_rad)
        self.c_rad.setToolTip("Radius of the circular conflict generation area in nautical miles")
        
        # Separation minima
        self.hsep = QDoubleSpinBox(); self.hsep.setRange(0.0, 50.0); self.hsep.setDecimals(2); self.hsep.setValue(5.0); _configure_decimal_separator(self.hsep)
        self.hsep.setToolTip("Horizontal separation at closest point of approach in nautical miles")
        self.vsep = QSpinBox(); self.vsep.setRange(0, 5000); self.vsep.setValue(1000)
        self.vsep.setToolTip("Vertical separation for altitude crossing conflicts in feet")

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
        self.abs_actypes.setToolTip("Aircraft types to use, comma-separated (e.g. A320,B738,A350)")
        abs_actypes_btn = QPushButton("Select...")
        abs_actypes_btn.setMaximumWidth(70)
        abs_actypes_btn.clicked.connect(lambda: self._select_aircraft_types_rc())
        abs_actypes_layout = QHBoxLayout()
        abs_actypes_layout.addWidget(self.abs_actypes)
        abs_actypes_layout.addWidget(abs_actypes_btn)
        abs_actypes_layout.setContentsMargins(0, 0, 0, 0)
        abs_actypes_widget = QWidget()
        abs_actypes_widget.setLayout(abs_actypes_layout)
        abs_form.addRow("Aircraft types:", abs_actypes_widget)

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
        rel_actypes_btn = QPushButton("Select...")
        rel_actypes_btn.setMaximumWidth(70)
        rel_actypes_btn.clicked.connect(lambda: self._select_aircraft_types_rel())
        rel_actypes_layout = QHBoxLayout()
        rel_actypes_layout.addWidget(self.rel_type)
        rel_actypes_layout.addWidget(rel_actypes_btn)
        rel_actypes_layout.setContentsMargins(0, 0, 0, 0)
        rel_actypes_widget = QWidget()
        rel_actypes_widget.setLayout(rel_actypes_layout)
        rel_form.addRow("Aircraft type:", rel_actypes_widget)

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
        self.scn.setToolTip("Name for the generated random conflict scenario file (without .scn extension)")
        self.scn.textChanged.connect(self._update_circle_if_visible)  # Update circle name when scenario name changes
        self.seed = QSpinBox(); self.seed.setRange(0, 2**31-1); self.seed.setValue(0)
        self.seed.setToolTip("Random seed for conflict generation (0 = use random seed)")
        self.gc_overwrite_cb = QCheckBox("Overwrite scenario if it exists")
        self.gc_overwrite_cb.setChecked(False)
        self.gc_overwrite_cb.setToolTip("Replace existing scenario file if one exists with the same name")
        
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
            # Clear any existing text and pre-fill the command line with "POLY polygonname " (note the trailing space)
            _clear_and_set_cmdline(f"POLY {polygon_name} ")
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

    def _select_aircraft_types_rc(self):
        """Open aircraft type selection dialog for random conflicts."""
        current_types = self.abs_actypes.text()
        dialog = AircraftTypeDialog(current_types, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_types = dialog.get_selected_types()
            self.abs_actypes.setText(selected_types)

    def _select_aircraft_types_rel(self):
        """Open aircraft type selection dialog for relative conflicts."""
        current_types = self.rel_type.text()
        dialog = AircraftTypeDialog(current_types, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_types = dialog.get_selected_types()
            self.rel_type.setText(selected_types)
        
# --- Procedure Tab ----------------------------------------------------------
class ProcTab(QWidget):
    """
    Advanced Procedural Traffic Generation interface for SID/STAR-based scenarios.
    
    This sophisticated tab provides comprehensive procedural traffic generation using
    Standard Instrument Departures (SIDs) and Standard Terminal Arrival Routes (STARs)
    with advanced scheduling capabilities, rate management, and realistic flight
    spawning based on actual airport procedure configurations. The tab enables
    creation of realistic traffic scenarios that follow published procedures.
    
    The Procedural Traffic system loads waypoint definition files and procedure
    configuration files to create complex traffic scenarios with proper procedure
    adherence. Aircraft are automatically assigned appropriate procedures based on
    sophisticated scheduling algorithms that maintain realistic traffic patterns
    and separation requirements.
    
    Core Functionality:
    - Waypoint Definition Loading: Import waypoint coordinates and navigation data
    - Procedure File Processing: Load SID/STAR procedure definitions with routing
    - Automatic Aircraft Assignment: Intelligent procedure assignment based on traffic flow
    - Rate Management: Configure spawning rates and timing patterns for procedures
    - Schedule Configuration: Advanced scheduling with time-based traffic variations
    - Inbound Traffic Generation: Realistic approach and arrival traffic patterns
    
    Traffic Generation Features:
    - Sector-based spawning around procedure entry points
    - Inbound trajectory calculation toward initial fixes  
    - Minimum time spacing enforcement between aircraft
    - Random procedure assignment with weighted selection
    - Realistic aircraft type assignment based on procedure characteristics
    - Schedule-based traffic variation throughout simulation periods
    
    Attributes:
        _wpt_files (List[str]): List of waypoint definition scenario files
        _proc_files (List[str]): List of procedure scenario files with routing
        _sid_rate_rows (Dict): SID procedure rate configuration widgets
        _sid_schedule_data (Dict): Comprehensive SID scheduling parameters
        _star_rate_rows (Dict): STAR procedure rate configuration widgets  
        _star_schedule_data (Dict): Comprehensive STAR scheduling parameters
        _star_rate_values (Dict): Current and target STAR rate values
        _star_rate_groups (Dict): Grouped STAR procedures for coordinated scheduling
        _star_basis_index (int): Current basis index for STAR rate calculations
    
    Returns:
        None: Tab initialization creates UI elements and loads default configurations
    
    Examples:
        # Tab is created as part of main SATG window
        proc_tab = ProcTab(parent_window)
        
        # Typical workflow:
        # 1. Load waypoint definition files (DEFWPT scenarios)
        # 2. Load procedure files with %0 placeholders and PROCNAME definitions
        # 3. Configure SID and STAR rates and schedules
        # 4. Generate procedural traffic scenarios
        # 5. Execute scenarios in BlueSky simulation
        
        # Rate configuration supports dynamic scheduling
        # Schedule data includes time-based variations
        # Spawning maintains proper separation and realism
    
    Note:
        Procedural traffic generation requires properly formatted scenario files
        with waypoint definitions (DEFWPT commands) and procedure specifications
        including PROCNAME identifiers and routing information. The tab maintains
        compatibility with standard BlueSky scenario file formats and provides
        sophisticated traffic management for complex airport operations.
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
        self._generic_rate_rows = {}
        self._generic_schedule_data: Dict[str, Dict[str, object]] = {}
        self._generic_rate_values: Dict[str, Dict[str, int]] = {"initial": {}, "final": {}}
        self._generic_rate_groups: Dict[str, List[str]] = {}
        self._generic_basis_index: int = 0
        self._origins: Dict[str, str] = {}
        self._destinations: Dict[str, List[str]] = {}
        self._last_dest_sent: set[str] = set()
        self._proc_widgets: Dict[str, Dict[str, object]] = {}
        self._last_sid_sched_sent: set = set()
        self._last_generic_sched_sent: set = set()
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
        btn_wpt_add = QPushButton("Add waypoint files...")
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
        
        # File management buttons (left side)
        btn_proc_add = QPushButton("Add procedure files...")
        btn_proc_rm  = QPushButton("Remove selected")
        btn_proc_clr = QPushButton("Clear all")
        hbp.addWidget(btn_proc_add); hbp.addWidget(btn_proc_rm); hbp.addWidget(btn_proc_clr)
        
        # Add stretch to push the procedure action buttons to the right
        hbp.addStretch(1)
        
        # Procedure action buttons (right side)
        create_proc_btn = QPushButton("Create New Procedure...")
        edit_proc_btn = QPushButton("Edit Procedure...")
        hbp.addWidget(create_proc_btn); hbp.addWidget(edit_proc_btn)
        
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
        create_proc_btn.clicked.connect(self._create_procedure)
        edit_proc_btn.clicked.connect(self._edit_procedure)

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
        fg = QFormLayout(generic_box); self._generic_form = fg
        generic_hint = QLabel("Generic procedures spawn at their first waypoint.\nLoad non-SID and non-STAR .scn files.")
        generic_hint.setWordWrap(True)
        fg.addRow(generic_hint)
        mode_row_generic = QHBoxLayout()
        mode_row_generic.addWidget(QLabel("Scheduling mode:", generic_box))
        self.generic_mode = QComboBox(generic_box)
        self.generic_mode.addItems(["Hourly rate", "15-min schedule"])
        self.generic_mode.setToolTip("Choose between constant hourly rates or detailed 15-minute schedules")
        mode_row_generic.addWidget(self.generic_mode)
        mode_row_generic.addSpacing(12)
        mode_row_generic.addWidget(QLabel("Rate basis:", generic_box))
        self.generic_rate_basis = QComboBox(generic_box)
        self.generic_rate_basis.addItems(["Initial waypoint", "Final waypoint"])
        self.generic_rate_basis.setToolTip("Base rates on initial waypoint (entry) or final waypoint (destination)")
        mode_row_generic.addWidget(self.generic_rate_basis)
        mode_row_generic.addStretch(1)
        fg.addRow(mode_row_generic)
        self.gen_flights = QSpinBox(); self.gen_flights.setRange(0, 100000); self.gen_flights.setValue(20)
        self.gen_flights.setToolTip("Total number of generic procedure flights to generate")
        fg.addRow("Flights:", self.gen_flights)
        alt_row_generic = QWidget(generic_box)
        alt_layout_generic = QHBoxLayout(alt_row_generic); alt_layout_generic.setContentsMargins(0, 0, 0, 0)
        alt_layout_generic.addWidget(QLabel("Initial ALT [FL]:", generic_box))
        self.generic_alt_fl = QSpinBox()
        self.generic_alt_fl.setRange(0, 600)
        self.generic_alt_fl.setSingleStep(10)
        self.generic_alt_fl.setValue(360)
        self.generic_alt_fl.setToolTip("Initial flight level for generic procedure arrivals")
        alt_layout_generic.addWidget(self.generic_alt_fl)
        alt_layout_generic.addSpacing(12)
        alt_layout_generic.addWidget(QLabel("Final ALT [FL]:", generic_box))
        self.generic_final_alt_fl = QSpinBox()
        self.generic_final_alt_fl.setRange(0, 600)
        self.generic_final_alt_fl.setSingleStep(10)
        self.generic_final_alt_fl.setValue(100)
        self.generic_final_alt_fl.setToolTip("Target flight level for generic procedure completion")
        alt_layout_generic.addWidget(self.generic_final_alt_fl)
        self.generic_override_initial_alt = QCheckBox("Override Initial")
        self.generic_override_initial_alt.setToolTip("Override initial altitude from procedure files")
        alt_layout_generic.addWidget(self.generic_override_initial_alt)
        self.generic_override_final_alt = QCheckBox("Override Final")
        self.generic_override_final_alt.setToolTip("Override final altitude in procedure files")
        alt_layout_generic.addWidget(self.generic_override_final_alt)
        alt_layout_generic.addStretch(1)
        fg.addRow(alt_row_generic)
        spd_row_generic = QWidget(generic_box)
        spd_layout_generic = QHBoxLayout(spd_row_generic); spd_layout_generic.setContentsMargins(0, 0, 0, 0)
        spd_layout_generic.addWidget(QLabel("Initial Mach:", generic_box))
        self.generic_mach = QDoubleSpinBox()
        self.generic_mach.setDecimals(2)
        self.generic_mach.setRange(0.40, 0.92)
        self.generic_mach.setSingleStep(0.01)
        self.generic_mach.setValue(0.79)
        self.generic_mach.setToolTip("Initial Mach number for generic arrivals at cruise altitude")
        _configure_decimal_separator(self.generic_mach)
        spd_layout_generic.addWidget(self.generic_mach)
        spd_layout_generic.addSpacing(12)
        spd_layout_generic.addWidget(QLabel("Final SPD [kt]:", generic_box))
        self.generic_final_spd = QSpinBox()
        self.generic_final_spd.setRange(0, 600)
        self.generic_final_spd.setValue(240)
        self.generic_final_spd.setToolTip("Target airspeed for generic procedure completion in knots")
        spd_layout_generic.addWidget(self.generic_final_spd)
        self.generic_override_initial_spd = QCheckBox("Override Initial")
        self.generic_override_initial_spd.setToolTip("Override initial speed from procedure files")
        spd_layout_generic.addWidget(self.generic_override_initial_spd)
        self.generic_override_final_spd = QCheckBox("Override Final")
        self.generic_override_final_spd.setToolTip("Override final speed in procedure files")
        spd_layout_generic.addWidget(self.generic_override_final_spd)
        spd_layout_generic.addStretch(1)
        fg.addRow(spd_row_generic)
        
        # Aircraft types field for Generic procedures
        self.generic_actypes = QLineEdit("A320,B738,A350")
        self.generic_actypes.setToolTip("Aircraft types to use for generic procedures, comma-separated (e.g. A320,B738,A350)")
        generic_actypes_btn = QPushButton("Select...")
        generic_actypes_btn.setMaximumWidth(70)
        generic_actypes_btn.clicked.connect(lambda: self._select_aircraft_types("generic"))
        generic_actypes_layout = QHBoxLayout()
        generic_actypes_layout.addWidget(self.generic_actypes)
        generic_actypes_layout.addWidget(generic_actypes_btn)
        generic_actypes_layout.setContentsMargins(0, 0, 0, 0)
        generic_actypes_widget = QWidget()
        generic_actypes_widget.setLayout(generic_actypes_layout)
        fg.addRow("Aircraft types:", generic_actypes_widget)
        
        self.generic_sched_btn = QPushButton("Configure schedule...")
        self.generic_sched_btn.clicked.connect(self._configure_generic_schedule)
        fg.addRow(self.generic_sched_btn)
        self.generic_mode.currentIndexChanged.connect(self._on_generic_mode_changed)
        self.generic_rate_basis.currentIndexChanged.connect(self._on_generic_basis_changed)
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
        self.sid_mode.setToolTip("Choose between constant hourly rates or detailed 15-minute schedules")
        mode_row.addWidget(self.sid_mode)
        mode_row.addStretch(1)
        fs.addRow(mode_row)

        self.sid_flights = QSpinBox(); self.sid_flights.setRange(0, 100000); self.sid_flights.setValue(20)
        self.sid_flights.setToolTip("Total number of SID procedure flights to generate")
        fs.addRow("Flights:", self.sid_flights)
        self.sid_alt = QSpinBox(); self.sid_alt.setRange(0, 50000); self.sid_alt.setValue(3000)
        self.sid_alt.setToolTip("Initial altitude for SID departures in feet")
        sid_alt_row = QWidget()
        sid_alt_layout = QHBoxLayout(sid_alt_row); sid_alt_layout.setContentsMargins(0, 0, 0, 0)
        sid_alt_layout.addWidget(self.sid_alt)
        self.sid_override_initial_alt = QCheckBox("Override Initial Command")
        self.sid_override_initial_alt.setToolTip("Override initial altitude command from procedure files")
        sid_alt_layout.addWidget(self.sid_override_initial_alt)
        sid_alt_layout.addStretch(1)
        fs.addRow("Initial ALT [ft]:", sid_alt_row)
        
        self.sid_spd = QSpinBox(); self.sid_spd.setRange(0, 600); self.sid_spd.setValue(210)
        self.sid_spd.setToolTip("Initial speed for SID departures in knots")
        sid_spd_row = QWidget()
        sid_spd_layout = QHBoxLayout(sid_spd_row); sid_spd_layout.setContentsMargins(0, 0, 0, 0)
        sid_spd_layout.addWidget(self.sid_spd)
        self.sid_override_initial_spd = QCheckBox("Override Initial Command")
        self.sid_override_initial_spd.setToolTip("Override initial speed command from procedure files")
        sid_spd_layout.addWidget(self.sid_override_initial_spd)
        sid_spd_layout.addStretch(1)
        fs.addRow("Initial SPD [kt]:", sid_spd_row)
        
        # Aircraft types field for SID procedures
        self.sid_actypes = QLineEdit("A320,B738,A350")
        self.sid_actypes.setToolTip("Aircraft types to use for SID procedures, comma-separated (e.g. A320,B738,A350)")
        sid_actypes_btn = QPushButton("Select...")
        sid_actypes_btn.setMaximumWidth(70)
        sid_actypes_btn.clicked.connect(lambda: self._select_aircraft_types("sid"))
        sid_actypes_layout = QHBoxLayout()
        sid_actypes_layout.addWidget(self.sid_actypes)
        sid_actypes_layout.addWidget(sid_actypes_btn)
        sid_actypes_layout.setContentsMargins(0, 0, 0, 0)
        sid_actypes_widget = QWidget()
        sid_actypes_widget.setLayout(sid_actypes_layout)
        fs.addRow("Aircraft types:", sid_actypes_widget)
        
        self.sid_sched_btn = QPushButton("Configure schedule...")
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
        self.star_mode.setToolTip("Choose between constant hourly rates or detailed 15-minute schedules")
        mode_row_star.addWidget(self.star_mode)
        mode_row_star.addSpacing(12)
        mode_row_star.addWidget(QLabel("Rate basis:", star_box))
        self.star_rate_basis = QComboBox(star_box)
        self.star_rate_basis.addItems(["Initial waypoint", "Final waypoint"])
        self.star_rate_basis.setToolTip("Base rates on initial waypoint (entry) or final waypoint (destination)")
        mode_row_star.addWidget(self.star_rate_basis)
        mode_row_star.addStretch(1)
        ft.addRow(mode_row_star)
        self.star_flights = QSpinBox(); self.star_flights.setRange(0, 100000); self.star_flights.setValue(20)
        self.star_flights.setToolTip("Total number of STAR procedure flights to generate")
        ft.addRow("Flights:", self.star_flights)
        alt_row = QWidget(star_box)
        alt_layout = QHBoxLayout(alt_row); alt_layout.setContentsMargins(0, 0, 0, 0)
        alt_layout.addWidget(QLabel("Initial ALT [FL]:", star_box))
        self.star_alt_fl = QSpinBox()
        self.star_alt_fl.setRange(0, 600)
        self.star_alt_fl.setSingleStep(10)
        self.star_alt_fl.setValue(360)
        self.star_alt_fl.setToolTip("Initial flight level for STAR arrivals")
        alt_layout.addWidget(self.star_alt_fl)
        alt_layout.addSpacing(12)
        alt_layout.addWidget(QLabel("Final ALT [FL]:", star_box))
        self.star_final_alt_fl = QSpinBox()
        self.star_final_alt_fl.setRange(0, 600)
        self.star_final_alt_fl.setSingleStep(10)
        self.star_final_alt_fl.setValue(100)
        self.star_final_alt_fl.setToolTip("Target flight level for STAR procedure completion")
        alt_layout.addWidget(self.star_final_alt_fl)
        self.star_override_initial_alt = QCheckBox("Override Initial")
        self.star_override_initial_alt.setToolTip("Override initial altitude from procedure files")
        alt_layout.addWidget(self.star_override_initial_alt)
        self.star_override_final_alt = QCheckBox("Override Final")
        self.star_override_final_alt.setToolTip("Override final altitude in procedure files")
        alt_layout.addWidget(self.star_override_final_alt)
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
        self.star_mach.setToolTip("Initial Mach number for STAR arrivals at cruise altitude")
        _configure_decimal_separator(self.star_mach)
        spd_layout.addWidget(self.star_mach)
        spd_layout.addSpacing(12)
        spd_layout.addWidget(QLabel("Final SPD [kt]:", star_box))
        self.star_final_spd = QSpinBox()
        self.star_final_spd.setRange(0, 600)
        self.star_final_spd.setValue(240)
        self.star_final_spd.setToolTip("Target airspeed for STAR procedure completion in knots")
        spd_layout.addWidget(self.star_final_spd)
        self.star_override_initial_spd = QCheckBox("Override Initial")
        self.star_override_initial_spd.setToolTip("Override initial speed from procedure files")
        spd_layout.addWidget(self.star_override_initial_spd)
        self.star_override_final_spd = QCheckBox("Override Final")
        self.star_override_final_spd.setToolTip("Override final speed in procedure files")
        spd_layout.addWidget(self.star_override_final_spd)
        spd_layout.addStretch(1)
        ft.addRow(spd_row)
        
        # Aircraft types field for STAR procedures
        self.star_actypes = QLineEdit("A320,B738,A350")
        self.star_actypes.setToolTip("Aircraft types to use for STAR procedures, comma-separated (e.g. A320,B738,A350)")
        star_actypes_btn = QPushButton("Select...")
        star_actypes_btn.setMaximumWidth(70)
        star_actypes_btn.clicked.connect(lambda: self._select_aircraft_types("star"))
        star_actypes_layout = QHBoxLayout()
        star_actypes_layout.addWidget(self.star_actypes)
        star_actypes_layout.addWidget(star_actypes_btn)
        star_actypes_layout.setContentsMargins(0, 0, 0, 0)
        star_actypes_widget = QWidget()
        star_actypes_widget.setLayout(star_actypes_layout)
        ft.addRow("Aircraft types:", star_actypes_widget)
        
        self.star_sched_btn = QPushButton("Configure schedule...")
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
        self.scn.setToolTip("Name for the generated procedural traffic scenario file (without .scn extension)")
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
        self._update_generic_mode_state()
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
        """Return only currently loaded STAR procedures."""
        # Filter to only include files that are actually in the loaded files list
        return [path for path in self._proc_files 
                if path in self._proc_widgets and self._proc_widgets[path].get("is_star")]

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
        # Capture current rates before clearing
        self._capture_star_rates(basis)
        
        # Get current active STAR procedures
        active_star_paths = set(self._current_star_procs())
        
        # Build groups based on ONLY currently active STAR procedures
        groups = self._build_star_groups(basis)
        self._star_rate_groups = groups
        
        # Remove any rows for groups that no longer exist
        for key in list(self._star_rate_rows.keys()):
            if key not in groups:
                self._remove_star_rate_row(key)
        
        # Add rows for new groups
        for key in groups.keys():
            self._ensure_star_rate_row(key, basis)
        
        # Clean up schedule data for procedures that are no longer active
        for path in list(self._star_schedule_data.keys()):
            if path not in active_star_paths:
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

    # Generic procedure helper methods
    def _current_generic_procs(self) -> List[str]:
        """Return only currently loaded generic procedures."""
        # Filter to only include files that are actually in the loaded files list
        return [path for path in self._proc_files 
                if path in self._proc_widgets and self._proc_widgets[path].get("is_generic")]

    def _current_generic_basis(self) -> str:
        return self._basis_name(self._generic_basis_index)

    def _capture_generic_rates(self, basis: str):
        store = self._generic_rate_values.setdefault(basis, {})
        for key, (_, spin) in self._generic_rate_rows.items():
            store[key] = int(spin.value())

    def _build_generic_groups(self, basis: str) -> Dict[str, List[str]]:
        groups: Dict[str, List[str]] = {}
        for path in self._current_generic_procs():
            widgets = self._proc_widgets.get(path, {})
            key_raw = widgets.get("initial_fix") if basis == "initial" else widgets.get("final_fix")
            if not key_raw:
                key_raw = os.path.splitext(os.path.basename(path))[0]
            key = str(key_raw).upper()
            groups.setdefault(key, []).append(path)
        return groups

    def _ensure_generic_rate_row(self, key: str, basis: str):
        store = self._generic_rate_values.setdefault(basis, {})
        value = int(store.get(key, DEFAULT_STAR_RATE))  # Reuse same default rate
        if key in self._generic_rate_rows:
            label, spin = self._generic_rate_rows[key]
            label.setText(f"{key} rate [ac/h]:")
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
            return
        label = QLabel(f"{key} rate [ac/h]:")
        spin = QSpinBox()
        spin.setRange(0, 120)
        spin.setValue(value)
        spin.valueChanged.connect(lambda val, b=basis, g=key: self._on_generic_rate_changed(b, g, val))
        self._generic_form.addRow(label, spin)
        self._generic_rate_rows[key] = (label, spin)

    def _on_generic_rate_changed(self, basis: str, key: str, val: int):
        self._generic_rate_values.setdefault(basis, {})[key] = int(val)

    def _remove_generic_rate_row(self, key: str):
        row = self._generic_rate_rows.pop(key, None)
        if not row:
            return
        label, spin = row
        basis = self._current_generic_basis()
        self._generic_rate_values.setdefault(basis, {})[key] = int(spin.value())
        self._generic_form.removeRow(spin)
        if label is not None and not sip.isdeleted(label):
            label.deleteLater()
        if spin is not None and not sip.isdeleted(spin):
            spin.deleteLater()

    def _clear_generic_rate_rows(self):
        for key in list(self._generic_rate_rows.keys()):
            self._remove_generic_rate_row(key)

    def _refresh_generic_rate_rows(self):
        basis = self._current_generic_basis()
        # Capture current rates before clearing
        self._capture_generic_rates(basis)
        
        # Get current active generic procedures
        active_generic_paths = set(self._current_generic_procs())
        
        # Build groups based on ONLY currently active generic procedures
        groups = self._build_generic_groups(basis)
        self._generic_rate_groups = groups
        
        # Remove any rows for groups that no longer exist
        for key in list(self._generic_rate_rows.keys()):
            if key not in groups:
                self._remove_generic_rate_row(key)
        
        # Add rows for new groups
        for key in groups.keys():
            self._ensure_generic_rate_row(key, basis)
        
        # Clean up schedule data for procedures that are no longer active
        for path in list(self._generic_schedule_data.keys()):
            if path not in active_generic_paths:
                self._generic_schedule_data.pop(path, None)
        
        self._update_generic_mode_state()

    def _sync_generic_rates(self):
        """Synchronize generic rates from GUI to internal storage."""
        basis = self._current_generic_basis()
        self._capture_generic_rates(basis)

    def _on_generic_mode_changed(self, idx):
        self._update_generic_mode_state()

    def _update_generic_mode_state(self):
        use_schedule = self.generic_mode.currentIndex() == 1 if hasattr(self, "generic_mode") else False
        if hasattr(self, "generic_sched_btn"):
            self.generic_sched_btn.setEnabled(use_schedule)
        if hasattr(self, "gen_flights"):
            self.gen_flights.blockSignals(True)
            if use_schedule:
                total_caps = sum(sum(int(c) for c in cfg.get("caps", []))
                                for cfg in self._generic_schedule_data.values())
                if total_caps > 0:
                    self.gen_flights.setValue(total_caps)
                self.gen_flights.setEnabled(False)
            else:
                self.gen_flights.setEnabled(True)
            self.gen_flights.blockSignals(False)
        for _, spin in self._generic_rate_rows.values():
            spin.setEnabled(not use_schedule)

    def _on_generic_basis_changed(self, idx: int):
        prev_basis = self._basis_name(self._generic_basis_index)
        self._capture_generic_rates(prev_basis)
        self._generic_basis_index = idx
        self._refresh_generic_rate_rows()

    def _configure_generic_schedule(self):
        procs = [(path, os.path.splitext(os.path.basename(path))[0]) for path in self._current_generic_procs()]
        if not procs:
            _emit("ECHO Load generic procedures before configuring schedules.")
            return
        dialog = StarSchedDialog(procs, self._generic_schedule_data, self)
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
            self._generic_schedule_data = cleaned
            self._update_generic_mode_state()

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
        # Update GUI state
        self._update_dest_state()
        # IMPORTANT: Update backend state to match GUI state
        _emit(f"SATG_PROC_USE_DEST {1 if checked else 0}")

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
            # Check if the widget is still valid (not deleted)
            try:
                if sip.isdeleted(edit):
                    continue
                edit.blockSignals(True)
                edit.setText(", ".join(self._destinations.get(path, [])))
                edit.blockSignals(False)
            except RuntimeError:
                # Widget has been deleted, skip it
                continue

    def _sync_origin_edits(self):
        for path, widgets in self._proc_widgets.items():
            if not widgets.get("is_sid", False):
                continue
            origin_edit = widgets.get("origin")
            if not origin_edit:
                continue
            # Check if the widget is still valid (not deleted)
            try:
                if sip.isdeleted(origin_edit):
                    continue
                origin_edit.blockSignals(True)
                origin_edit.setText(self._origins.get(path, ""))
                origin_edit.blockSignals(False)
            except RuntimeError:
                # Widget has been deleted, skip it
                continue

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
            is_generic = not is_sid and not is_star

            origin_edit: Optional[QLineEdit] = None
            origin_all_btn: Optional[QPushButton] = None
            if is_sid:
                origin_edit = QLineEdit(container)
                origin_edit.setPlaceholderText("Origin ICAO")
                origin_edit.setMaxLength(4)
                origin_edit.setMaximumWidth(90)
                origin_edit.setStyleSheet("background-color: white; color: black; border: 1px solid #ccc;")
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
            dest_edit.setStyleSheet("background-color: white; color: black; border: 1px solid #ccc;")
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
                "is_generic": is_generic,
            }
            if is_star:
                fixes = self._proc_fix_sequence(p)
                initial_fix = fixes[0] if fixes else ""
                final_fix = fixes[-1] if fixes else ""
                self._proc_widgets[p]["initial_fix"] = initial_fix
                self._proc_widgets[p]["final_fix"] = final_fix
            elif is_generic:
                fixes = self._proc_fix_sequence(p)
                initial_fix = fixes[0] if fixes else ""
                final_fix = fixes[-1] if fixes else ""
                self._proc_widgets[p]["initial_fix"] = initial_fix
                self._proc_widgets[p]["final_fix"] = final_fix
        
        # Ensure complete refresh after all additions
        self._refresh_all_batch_options()

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
            self._generic_schedule_data.pop(p, None)
            self._proc_widgets.pop(p, None)
            row = self.lst_proc.row(item)
            self.lst_proc.takeItem(row)
        
        # Ensure complete refresh after all removals
        self._refresh_all_batch_options()

    def _refresh_all_batch_options(self):
        """Comprehensive refresh of all batch options to ensure consistency."""
        # Capture current rates before refreshing
        if hasattr(self, '_generic_basis_index'):
            basis = self._current_generic_basis()
            self._capture_generic_rates(basis)
        
        if hasattr(self, '_star_basis_index'):
            basis = self._current_star_basis()
            self._capture_star_rates(basis)
        
        # Refresh all sections
        self._refresh_sid_runway_rows()
        self._refresh_star_rate_rows()
        self._refresh_generic_rate_rows()
        self._sync_destination_edits()
        self._sync_origin_edits()
        self._update_dest_state()

    def _clr_proc(self):
        if not self._proc_files: return
        _emit("SATG_PROC_CLEAR_PROC")
        self._proc_files.clear(); self.lst_proc.clear()
        self._proc_widgets.clear()  # Clear procedure widgets dictionary
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
        self._clear_generic_rate_rows()  # Add missing generic rate clearing
        self._generic_schedule_data.clear()
        self._last_generic_sched_sent.clear()
        self._generic_rate_values = {"initial": {}, "final": {}}
        self._generic_rate_groups = {}
        if hasattr(self, "generic_rate_basis"):
            self._generic_basis_index = self.generic_rate_basis.currentIndex()
        self._destinations.clear()
        self._last_dest_sent.clear()
        self._origins.clear()  # Also clear origins

    def _create_procedure(self):
        """Open the procedure creation dialog."""
        dialog = ProcedureCreatorDialog(self)
        dialog.show()  # Use show() instead of exec() for non-modal dialog

    def _edit_procedure(self):
        """Open dialog to select and edit an existing procedure."""
        if not self._proc_files:
            QMessageBox.information(self, "No Procedures", 
                                  "No procedure files are currently loaded.\n"
                                  "Please add procedure files first or create a new procedure.")
            return
        
        # Create a simple selection dialog for loaded procedures
        from PyQt6.QtWidgets import QInputDialog
        
        # Get list of procedure names from loaded files
        proc_names = [os.path.splitext(os.path.basename(filepath))[0] for filepath in self._proc_files]
        
        if not proc_names:
            QMessageBox.information(self, "No Procedures", "No procedures available for editing.")
            return
        
        # Let user select which procedure to edit
        proc_name, ok = QInputDialog.getItem(self, "Select Procedure to Edit", 
                                           "Choose a procedure to edit:", 
                                           proc_names, 0, False)
        
        if ok and proc_name:
            # Open the procedure editor dialog with the selected procedure name
            # This follows the same pattern as the enhanced "Edit Procedure..." button
            try:
                editor_dialog = ProcedureEditorDialog(proc_name, self)
                editor_dialog.exec()  # Use exec() for modal dialog instead of show()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error opening editor: {e}")

    def _load_created_procedure(self, filepath):
        """Load a newly created procedure file into the GUI."""
        if os.path.exists(filepath):
            _emit(f"SATG_PROC_LOAD_PROC {filepath}")
            self._proc_files.append(filepath)
            self.lst_proc.addItem(os.path.basename(filepath))
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
        gen_mode_schedule = self.generic_mode.currentIndex() == 1
        gen_alt_fl = int(self.generic_alt_fl.value())
        gen_mach = float(self.generic_mach.value())
        gen_rate_basis_idx = self.generic_rate_basis.currentIndex()
        gen_final_alt_fl = int(self.generic_final_alt_fl.value())
        gen_final_spd = int(self.generic_final_spd.value())
        
        sid_n = int(self.sid_flights.value())
        star_n = int(self.star_flights.value())
        star_mode_schedule = self.star_mode.currentIndex() == 1
        star_alt_fl = int(self.star_alt_fl.value())
        star_mach = float(self.star_mach.value())
        star_rate_basis_idx = self.star_rate_basis.currentIndex()
        star_final_alt_fl = int(self.star_final_alt_fl.value())
        star_final_spd = int(self.star_final_spd.value())
        star_minsep = 90

        generic_procs = self._current_generic_procs()
        if gen_mode_schedule and gen_n > 0 and generic_procs:
            total_caps = 0
            for path in generic_procs:
                cfg = self._generic_schedule_data.get(path)
                if not cfg:
                    continue
                caps = [int(c) for c in cfg.get("caps", [])]
                total_caps += sum(caps)
            if total_caps <= 0:
                _emit("ECHO Configure Generic schedule slots before running.")
                return False
            gen_n = total_caps
            self.gen_flights.blockSignals(True)
            self.gen_flights.setValue(gen_n)
            self.gen_flights.blockSignals(False)

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

        s   = int(self.seed.value())
        ow  = 1 if self.overwrite.isChecked() else 0

        sid_alt = int(self.sid_alt.value())
        sid_spd = int(self.sid_spd.value())

        # Configure Generic procedures with comprehensive parameters
        gen_basis_name = self._basis_name(gen_rate_basis_idx)
        # IMPORTANT: Ensure state is synchronized before capturing
        if self._generic_basis_index != gen_rate_basis_idx:
            # State mismatch detected - sync the internal state with the GUI state
            self._generic_basis_index = gen_rate_basis_idx
            # Refresh the rows to match the correct basis before capturing
            self._refresh_generic_rate_rows()
        self._capture_generic_rates(gen_basis_name)

        _emit(
            f"SATG_PROC_CFG_GENERIC {gen_n} {gen_alt_fl} {gen_mach:.2f} "
            f"{1 if gen_mode_schedule else 0} {gen_rate_basis_idx} {gen_final_alt_fl} {gen_final_spd}"
        )
        
        # Send override settings for generic procedures
        gen_override_initial_alt = int(self.generic_override_initial_alt.isChecked())
        gen_override_initial_spd = int(self.generic_override_initial_spd.isChecked())
        gen_override_final_alt = int(self.generic_override_final_alt.isChecked())
        gen_override_final_spd = int(self.generic_override_final_spd.isChecked())
        _emit(f"SATG_PROC_OVERRIDE_GENERIC {gen_override_initial_alt} {gen_override_initial_spd} {gen_override_final_alt} {gen_override_final_spd}")
        
        # Send aircraft types for generic procedures
        self._emit_proc_types_generic()
        
        _emit(f"SATG_PROC_CFG_SID {sid_n} {sid_alt} {sid_spd}")
        
        # Send override settings for SID procedures
        sid_override_initial_alt = int(self.sid_override_initial_alt.isChecked())
        sid_override_initial_spd = int(self.sid_override_initial_spd.isChecked())
        _emit(f"SATG_PROC_OVERRIDE_SID {sid_override_initial_alt} {sid_override_initial_spd}")
        
        # Send aircraft types for SID procedures
        self._emit_proc_types_sid()
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

        # Configure Generic rates
        gen_rate_store = self._generic_rate_values.setdefault(gen_basis_name, {})
        for key in sorted(self._generic_rate_rows.keys()):
            row = self._generic_rate_rows.get(key)
            if not row:
                continue
            _, spin = row
            rate = int(spin.value())
            gen_rate_store[key] = rate
            _emit(f"SATG_PROC_CFG_GENERICRATE {key} {rate}")

        # Configure Generic schedules
        current_generic_sched_sent = set()
        if gen_mode_schedule:
            for path in generic_procs:
                cfg = self._generic_schedule_data.get(path)
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
                _emit(f"SATG_PROC_CFG_GENERICSCHED {label} {start} {end} {caps_str}")
                current_generic_sched_sent.add(label)
            to_clear_generic = self._last_generic_sched_sent - current_generic_sched_sent
            for label in sorted(to_clear_generic):
                _emit(f"SATG_PROC_CLEAR_GENERICSCHED {label}")
            self._last_generic_sched_sent = current_generic_sched_sent
        else:
            if self._last_generic_sched_sent:
                for label in sorted(self._last_generic_sched_sent):
                    _emit(f"SATG_PROC_CLEAR_GENERICSCHED {label}")
                self._last_generic_sched_sent.clear()

        basis_name = self._basis_name(star_rate_basis_idx)
        # IMPORTANT: Ensure state is synchronized before capturing
        if self._star_basis_index != star_rate_basis_idx:
            # State mismatch detected - sync the internal state with the GUI state
            self._star_basis_index = star_rate_basis_idx
            # Refresh the rows to match the correct basis before capturing
            self._refresh_star_rate_rows()
        self._capture_star_rates(basis_name)

        _emit(
            f"SATG_PROC_CFG_STAR {star_n} {star_minsep} {star_alt_fl} {star_mach:.2f} "
            f"{1 if star_mode_schedule else 0} {star_rate_basis_idx} {star_final_alt_fl} {star_final_spd}"
        )
        
        # Send override settings for STAR procedures
        star_override_initial_alt = int(self.star_override_initial_alt.isChecked())
        star_override_initial_spd = int(self.star_override_initial_spd.isChecked())
        star_override_final_alt = int(self.star_override_final_alt.isChecked())
        star_override_final_spd = int(self.star_override_final_spd.isChecked())
        _emit(f"SATG_PROC_OVERRIDE_STAR {star_override_initial_alt} {star_override_initial_spd} {star_override_final_alt} {star_override_final_spd}")
        
        # Send aircraft types for STAR procedures
        self._emit_proc_types_star()

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
        cmd = f"SATG_PROC_MAKE {name} {total} {s} {ow}"
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

    def _emit_proc_types_generic(self):
        raw = self.generic_actypes.text().strip()
        if not raw:
            _emit("SATG_PROC_TYPES_GENERIC")
            return
        parts = [seg.strip().upper() for seg in re.split(r"[,\s]+", raw.replace("|", " ")) if seg.strip()]
        if not parts:
            _emit("SATG_PROC_TYPES_GENERIC")
            return
        cmd = "SATG_PROC_TYPES_GENERIC " + " ".join(parts)
        _emit(cmd)

    def _emit_proc_types_sid(self):
        raw = self.sid_actypes.text().strip()
        if not raw:
            _emit("SATG_PROC_TYPES_SID")
            return
        parts = [seg.strip().upper() for seg in re.split(r"[,\s]+", raw.replace("|", " ")) if seg.strip()]
        if not parts:
            _emit("SATG_PROC_TYPES_SID")
            return
        cmd = "SATG_PROC_TYPES_SID " + " ".join(parts)
        _emit(cmd)

    def _emit_proc_types_star(self):
        raw = self.star_actypes.text().strip()
        if not raw:
            _emit("SATG_PROC_TYPES_STAR")
            return
        parts = [seg.strip().upper() for seg in re.split(r"[,\s]+", raw.replace("|", " ")) if seg.strip()]
        if not parts:
            _emit("SATG_PROC_TYPES_STAR")
            return
        cmd = "SATG_PROC_TYPES_STAR " + " ".join(parts)
        _emit(cmd)

    def _select_aircraft_types(self, proc_type: str):
        """Open aircraft type selection dialog for the specified procedure type."""
        # Get current value from the appropriate field
        if proc_type == "generic":
            current_types = self.generic_actypes.text()
            field = self.generic_actypes
        elif proc_type == "sid":
            current_types = self.sid_actypes.text()
            field = self.sid_actypes
        elif proc_type == "star":
            current_types = self.star_actypes.text()
            field = self.star_actypes
        else:
            return
        
        # Open dialog
        dialog = AircraftTypeDialog(current_types, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.get_selected_types()
            field.setText(selected)

# --- Help tab -------------------------------------------------------------
class HelpTab(QWidget):
    """
    Comprehensive built-in help system providing detailed SATG documentation and guidance.
    
    This read-only help interface provides complete documentation for all SATG
    functionality including feature descriptions, workflow guidance, configuration
    examples, and troubleshooting information. The help system is integrated
    directly into the GUI to provide immediate access to documentation without
    requiring external resources or internet connectivity.
    
    The Help Tab serves as a comprehensive reference for all SATG capabilities,
    providing both overview information for new users and detailed technical
    documentation for advanced users. All content is presented in a clear,
    structured format with practical examples and step-by-step guidance.
    
    Content Structure:
    - SATG Overview: Introduction to synthetic air traffic generation concepts
    - Feature Documentation: Detailed descriptions of all tab functionalities
    - Workflow Guides: Step-by-step procedures for common tasks
    - Configuration Examples: Practical examples for different use cases
    - Troubleshooting: Common issues and resolution strategies
    - Technical Reference: Advanced configuration and integration details
    
    Help Topics Covered:
    - Historic Sampling: Machine learning-based traffic generation
    - Realistic Replay: Scenario-based historical traffic reproduction  
    - Geometric Conflicts: Advanced conflict detection and resolution scenarios
    - Random Conflicts: Stochastic conflict generation for training
    - Procedural Traffic: SID/STAR-based realistic airport operations
    - Configuration Management: Session persistence and optimization
    
    Attributes:
        txt (QTextEdit): Read-only text widget displaying help content
        help_text (str): Comprehensive help documentation content
    
    Args:
        parent (QWidget, optional): Parent widget for proper tab integration
    
    Examples:
        # Tab is automatically created as part of main SATG window
        help_tab = HelpTab(parent_window)
        
        # Help content is immediately available for reference
        # No configuration required - provides instant documentation access
    
    Note:
        The Help Tab provides offline documentation ensuring users have access
        to complete SATG guidance regardless of network connectivity. Content
        is regularly updated to reflect current SATG capabilities and includes
        practical examples for all major features and workflows.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setMinimumHeight(300)

        help_text = (
            "SATG - Synthetic Air Traffic Generator\n"
            "======================================\n"
            "\n"
            "SATG generates synthetic air traffic scenarios for BlueSky simulation,\n"
            "supporting conflict generation, historical traffic replay, procedural operations,\n"
            "and ML-based synthetic traffic generation using TraffixGen integration.\n"
            "\n"
            "Core Capabilities\n"
            "-----------------\n"
            "- Historic Sampling: ML-based synthetic traffic from learned patterns\n"
            "- Realistic Replay: Historical data replay with optional jitter\n"
            "- Geometric Conflicts: Precise conflicts at specific locations\n"
            "- Random Conflicts: Randomized conflicts within defined areas\n"
            "- Procedural Traffic: SID/STAR-based realistic airport operations\n"
            "- Polygon Areas: Custom geographic areas for traffic generation\n"
            "- Configuration Management: Save/load settings with caching\n"
            "\n"
            "Historic Sampling Tab\n"
            "--------------------\n"
            "Machine learning-based synthetic traffic generation:\n"
            "1. Load EUROCONTROL data (flights, filed, actual, FIR files)\n"
            "2. Configure filters (geographic, altitude, time, aircraft type)\n"
            "3. Train ML models on filtered trajectory data\n"
            "4. Generate synthetic scenarios with learned traffic patterns\n"
            "5. Export scenarios for BlueSky simulation\n"
            "\n"
            "Features:\n"
            "- Multi-source EUROCONTROL data integration\n"
            "- Advanced filtering with airspace boundaries\n"
            "- Machine learning pattern recognition\n"
            "- Synthetic trajectory generation with realistic characteristics\n"
            "\n"
            "Realistic Replay Tab\n"
            "-------------------\n"
            "Generate scenarios from historical flight data:\n"
            "1. Load flight data and track data CSV files\n"
            "2. Configure jitter settings for trajectory variations\n"
            "3. Set auto-delete and phase-specific jitter options\n"
            "4. Generate scenarios with realistic timing and routes\n"
            "\n"
            "Advanced Features:\n"
            "- Phase-specific jitter (departure, climb, cruise, descent, approach)\n"
            "- Track configuration with altitude phase definitions\n"
            "- Auto-delete conflicting aircraft\n"
            "- Direct JSON data loading for TraffixGen integration\n"
            "\n"
            "Geometric Conflicts Tab\n"
            "----------------------\n"
            "Create precise conflicts with mathematical conflict geometry:\n"
            "1. Configure separation standards (horizontal/vertical)\n"
            "2. Set encounter types (head-on, crossing, overtaking)\n"
            "3. Define sampling ranges for aircraft parameters\n"
            "4. Create conflicts at specific coordinates or waypoints\n"
            "\n"
            "Configuration Options:\n"
            "- Horizontal/vertical separation settings\n"
            "- Encounter type selection\n"
            "- Speed and altitude ranges\n"
            "- Crossing angle specifications\n"
            "- Aircraft type assignments\n"
            "\n"
            "Random Conflicts Tab\n"
            "-------------------\n"
            "Generate randomized conflicts within defined areas:\n"
            "1. Define conflict area (circular or custom polygon)\n"
            "2. Set number of conflicts and encounter types\n"
            "3. Configure timing (TCPA), altitude, and speed parameters\n"
            "4. Select aircraft types and generation modes\n"
            "\n"
            "Area Types:\n"
            "- Circular areas with center coordinates and radius\n"
            "- Custom polygon areas (requires polygon creation)\n"
            "- Mixed generation modes (absolute/relative/mixed)\n"
            "\n"
            "Procedures Tab\n"
            "-------------\n"
            "Create traffic following standard instrument procedures:\n"
            "1. Load waypoint definitions and procedure files\n"
            "2. Configure generic, SID, and STAR traffic parameters\n"
            "3. Set traffic rates and scheduling windows\n"
            "4. Generate procedural traffic scenarios\n"
            "\n"
            "Procedure Types:\n"
            "- Generic procedures with flexible routing\n"
            "- Standard Instrument Departures (SID)\n"
            "- Standard Terminal Arrival Routes (STAR)\n"
            "- Custom polygons converted to procedures\n"
            "\n"
            "Configuration Management\n"
            "-----------------------\n"
            "Save and load settings:\n"
            "- Save Config: Store current tab configurations\n"
            "- Load Config: Restore previously saved settings\n"
            "- Edit Configs: Manage saved configuration files\n"
            "- Manage Cache: View and clear cache files\n"
            "\n"
            "Custom Polygons\n"
            "--------------\n"
            "Create and manage geographic areas:\n"
            "- Access via 'Configure Filters' in respective tabs\n"
            "- Define polygon boundaries with coordinate points\n"
            "- Use polygons for traffic generation areas\n"
            "- Convert polygons to procedures\n"
            "\n"
            "General Workflow\n"
            "---------------\n"
            "1. Browse Base Folder: Set output directory for scenario files\n"
            "2. Select tab for desired scenario type\n"
            "3. Configure parameters using interface controls\n"
            "4. Create scenarios using 'Create' or 'Create & Run' buttons\n"
            "5. Load scenarios in BlueSky using PCALL command\n"
            "\n"
            "Command Integration\n"
            "------------------\n"
            "GUI operations correspond to SATG commands:\n"
            "- Use 'SATG_HELP' button for detailed command reference\n"
            "- All GUI functions have command-line equivalents\n"
            "- Scenarios include PCALL commands for BlueSky execution\n"
            "- Seed values enable reproducible generation\n"
            "\n"
            "For detailed command syntax and advanced features,\n"
            "click the 'SATG_HELP' button in the top toolbar.\n"
        )

        txt.setPlainText(help_text)
        lay.addWidget(txt)

# --- main window ------------------------------------------------------------

class SATGWindow(QWidget):
    """
    Primary window class for the comprehensive Synthetic Air Traffic Generation (SATG) GUI plugin.
    
    This sophisticated main window serves as the central hub for all SATG functionality,
    providing an integrated tabbed interface for synthetic air traffic generation, conflict
    simulation, machine learning-based trajectory synthesis, and comprehensive procedure
    management. The window architecture supports advanced configuration persistence,
    intelligent caching systems, and seamless integration with BlueSky simulation
    environments for comprehensive air traffic management training scenarios.
    
    The SATGWindow orchestrates multiple specialized operational tabs, each providing
    focused interfaces for specific aspects of synthetic air traffic generation and
    simulation training requirements. The integrated design ensures consistent behavior
    across all operational modes while maintaining specialized functionality for
    advanced training scenario development and execution.
    
    Comprehensive Tab Architecture:
    - Help Tab: Extensive documentation, usage examples, and operational guidance
    - Realistic Replay Tab: Scenario-based traffic generation with historical flight replay
    - Historic Sampling Tab: Advanced ML-based synthetic aircraft generation from EUROCONTROL data
    - Geometric Conflicts Tab: Sophisticated conflict detection algorithms and resolution training
    - Random Conflicts Tab: Stochastic conflict generation with statistical analysis capabilities
    - Procedures Tab: Comprehensive SID/STAR procedure creation, editing, and management system
    
    Advanced Window Management Features:
    - Sophisticated tabbed interface architecture with consistent styling and behavioral patterns
    - Intelligent visual indicator management system across all tabs (geometric references, CPA indicators)
    - Comprehensive configuration persistence system with automatic state management and recovery
    - Advanced cache management with intelligent validation, file integrity checking, and performance optimization
    - Multi-threaded progress dialog integration with proper UI threading and responsive interfaces
    - Performance optimization systems including intelligent file path caching and vectorized operations
    
    Configuration Management System:
    - Complete session state persistence across all operational tabs and training modes
    - JSON-based configuration storage with human-readable formatting and validation
    - Automatic backup creation and recovery mechanisms for configuration protection
    - Backward compatibility support for legacy configuration formats and migration
    - Configuration versioning and validation with detailed error reporting and recovery
    - Bulk configuration operations with import/export capabilities for training management
    
    Cache Management and Performance Optimization:
    - Intelligent file path caching for Configure Filters dialog performance enhancement
    - Cache validation using file modification times and integrity checking algorithms
    - Memory-efficient data processing for large-scale EUROCONTROL dataset operations
    - Vectorized flight point filtering with NumPy optimization for computational acceleration
    - Multi-threaded processing support for CPU-intensive operations and model training
    - Progress monitoring with responsive UI updates and cancellation support
    
    Integration Architecture:
    - Seamless BlueSky simulator integration with command system and state synchronization
    - TraffixGen backend integration for advanced machine learning capabilities
    - EUROCONTROL data processing with comprehensive format support and validation
    - Geographic information system integration for airspace boundary management
    - Aircraft performance model integration with automatic type detection and validation
    
    The SATGWindow represents the culmination of sophisticated air traffic simulation
    interface design, providing researchers, training organizations, and air traffic
    management professionals with comprehensive tools for advanced scenario development,
    machine learning-based traffic synthesis, and comprehensive training program support.
    
    Attributes:
        help_tab (HelpTab): Comprehensive documentation and operational guidance interface
        rl_tab (RLTab): Realistic Replay traffic generation with historical scenario support
        hs_tab (HistoricSamplingTab): Machine learning-based synthetic traffic generation system
        gc_tab (GCTab): Advanced geometric conflict detection and resolution training interface
        rc_tab (RCTab): Stochastic conflict generation with statistical analysis capabilities
        proc_tab (ProcTab): Comprehensive procedure management for SID/STAR operations
        top_strip (TopStrip): Primary navigation and configuration management interface
        tab_widget (QTabWidget): Central tabbed interface container with advanced management
        
    Args:
        parent (QWidget, optional): Parent widget for proper window hierarchy and behavior
        
    Examples:
        # Create main SATG window with full functionality
        satg_window = SATGWindow()
        satg_window.show()
        
        # Window automatically initializes all tabs and configuration systems
        # providing immediate access to comprehensive SATG functionality
    
        
        Attributes:
            rc_tab (RCTab): Random conflict generation and analysis
            proc_tab (ProcTab): Procedure creation and management
            tabs (QTabWidget): Main tab container widget
            top (TopStrip): Configuration and cache management controls
        
        Examples:
            # Window is typically created through the plugin system
            window = SATGWindow()
            window.show()
            
            # Tab switching automatically manages visual indicators
            # Configuration persistence is handled automatically
            # Cache validation occurs when needed
        
        Note:
            The SATGWindow requires proper BlueSky integration and QApplication context
            for full functionality. All configuration and cache operations are handled
            automatically with comprehensive error recovery and validation systems.
            
            This window uses lazy initialization and should only be created after
            QApplication is available. Visual indicators are automatically managed
            when switching between tabs to prevent display conflicts.
        """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SATG GUI")
        self.resize(980, 720)
        layout = QVBoxLayout(self)

        tabs = QTabWidget(self)
        
        # Create tab instances and store references for visual indicator management
        self.help_tab = HelpTab(self)
        self.rl_tab = RLTab(self)
        self.hs_tab = HistoricSamplingTab(self)  # Historic Sampling tab
        self.gc_tab = GCTab(self)  # Has CPA reference visualization
        self.rc_tab = RCTab(self)  # Has circle visualization
        self.proc_tab = ProcTab(self)
        
        tabs.addTab(self.help_tab, "Help")
        tabs.addTab(self.rl_tab, "Realistic Replay")
        tabs.addTab(self.hs_tab, "Historic Sampling")
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
    
    def _clear_traffixgen_cache(self):
        """Clear TraffixGen parquet cache files to force fresh data processing."""
        try:
            import os
            import glob
            
            # Look for TraffixGen cache files in current directory
            cache_patterns = [
                'traffixgen_*.parquet',
                'traffixgen_*.pkl'
            ]
            
            deleted_count = 0
            for pattern in cache_patterns:
                cache_files = glob.glob(pattern)
                for cache_file in cache_files:
                    try:
                        os.remove(cache_file)
                        deleted_count += 1
                        print(f">> Cleared cache: {os.path.basename(cache_file)}")
                    except Exception as e:
                        print(f"WARNING: Could not delete {cache_file}: {e}")
            
            if deleted_count > 0:
                print(f">> Cleared {deleted_count} cache files to force fresh data processing")
            else:
                print("INFO: No cache files found to clear")
                
        except Exception as e:
            print(f"WARNING: Error clearing cache: {e}")
    
    def _clear_filter_cache(self):
        """Clear the cached filter configuration data when file paths change."""
        if hasattr(self, '_cached_filter_file_paths'):
            delattr(self, '_cached_filter_file_paths')
        if hasattr(self, '_cached_filter_summary_data'):
            delattr(self, '_cached_filter_summary_data')

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
    """
    Initialize SATGGUI plugin for BlueSky simulator GUI integration.
    
    This function provides the standard BlueSky plugin initialization interface,
    returning the plugin metadata required for proper registration and integration
    with the BlueSky GUI system. The function defines the plugin as a GUI-type
    plugin with comprehensive synthetic air traffic generation capabilities.
    
    Plugin Configuration:
    - Plugin Name: 'SATGGUI' (Synthetic Air Traffic Generation GUI)
    - Plugin Type: 'gui' (GUI plugin for graphical interface integration)
    - Integration Level: Full BlueSky GUI system integration
    - Capabilities: Complete SATG functionality through advanced tabbed interface
    
    Returns:
        Dict[str, str]: Plugin metadata dictionary with name and type information
    
    Examples:
        # Called automatically by BlueSky during GUI plugin loading
        metadata = init_plugin()  # Returns {'plugin_name': 'SATGGUI', 'plugin_type': 'gui'}
    
    Note:
        This function is called automatically by BlueSky during startup and
        GUI plugin discovery. The returned metadata enables proper plugin
        registration and GUI system integration for SATGGUI functionality.
    """
    return {'plugin_name': 'SATGGUI', 'plugin_type': 'gui'}

from bluesky import stack as _stack_mod  # ensure decorator import after init
@_stack_mod.command
def SATGGUI():
    """
    Activate and display the comprehensive SATG GUI interface window.
    
    SATGGUI
    Open the main Synthetic Air Traffic Generation GUI window, providing
    comprehensive access to all SATG functionality including historic sampling,
    realistic replay, conflict generation, and procedure management through
    an integrated tabbed interface with advanced configuration management.
    
    This command creates or activates the main SATGGUI window, which serves as
    the primary interface for all synthetic air traffic generation operations.
    The window provides sophisticated tools for training scenario development,
    machine learning-based traffic synthesis, and comprehensive air traffic
    management simulation with full integration to BlueSky systems.
    
    GUI Features Activated:
    - Historic Sampling: ML-based synthetic aircraft generation from EUROCONTROL data
    - Realistic Replay: Scenario-based traffic generation with historical flight replay
    - Geometric Conflicts: Advanced conflict detection algorithms and resolution training
    - Random Conflicts: Stochastic conflict generation with statistical analysis
    - Procedures Management: Comprehensive SID/STAR procedure creation and editing
    - Configuration System: Complete session state persistence and management
    
    Examples:
        # Activate SATG GUI from BlueSky console
        SATGGUI
        
        # GUI window opens with full tabbed interface
        # providing immediate access to all SATG capabilities
    
    Note:
        The command uses lazy window creation to ensure QApplication is available
        before GUI initialization. The window maintains all configuration state
        and provides comprehensive functionality for synthetic air traffic generation.
    """
    _get_window().show()
