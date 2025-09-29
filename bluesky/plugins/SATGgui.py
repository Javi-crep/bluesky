# SATGgui.py -- BlueSky GUI plugin for SATG command front-end (no GUI echo log)
# Place in: bluesky/plugins/
#
# PyQt6; lazy window creation to avoid QApplication race.

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QCheckBox, QComboBox, QPushButton, QSpinBox,
    QDoubleSpinBox, QFileDialog, QSlider, QListWidget, QFileDialog, QTextEdit
)
from bluesky import stack

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

# --- top strip -------------------------------------------------------------

class TopStrip(QWidget):
    """Top strip with base dir controls and a single RESET button."""
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)

        btn_browse = QPushButton("Browse Base Folder", self)
        btn_show = QPushButton("Show Paths", self)
        btn_help = QPushButton("SATG_HELP", self)
        btn_reset = QPushButton("Reset", self)
        btn_reset.setToolTip("Full BlueSky reset")

        lay.addWidget(btn_browse)
        lay.addWidget(btn_show)
        lay.addWidget(btn_help)
        lay.addStretch(1)
        lay.addWidget(btn_reset)

        btn_browse.clicked.connect(self._choose_base)
        btn_show.clicked.connect(lambda: _emit("SATG_DIR"))
        btn_help.clicked.connect(lambda: _emit("SATG_HELP"))
        btn_reset.clicked.connect(lambda: _emit("RESET"))

    def _choose_base(self):
        path = QFileDialog.getExistingDirectory(self, "Choose SATG base directory")
        if path:
            _emit(_join_tokens("SATG_DIR", _qpath(path)))

# --- RL tab (Realistic Replay) --------------------------------------------

class RLTab(QWidget):
    """Realistic Replay: load -> jitter -> autodel/make/run."""
    def __init__(self, parent=None):
        super().__init__(parent)
        main = QVBoxLayout(self)

        # 1) Load (Required)
        gb_load = QGroupBox("1) Load data - Required")
        fl = QFormLayout(gb_load)
        desc1 = QLabel("Load aircraft tracks from selected path")
        desc1.setStyleSheet("color: #666; font-style: italic;")
        self.opt_auto = QCheckBox("Use AUTO folder (./satg_data/data)")
        self.opt_auto.setChecked(True)

        btn_file = QPushButton("(Optionally) Add files manually")
        self._chosen_files = []  # internal list of selected CSV files
        btn_file.clicked.connect(self._pick_files)

        btn_load = QPushButton("LOAD FILES")

        fl.addRow(desc1)
        fl.addRow(self.opt_auto)
        fl.addRow("Files:", btn_file)
        fl.addRow(btn_load)

        btn_load.clicked.connect(self._load)

        # 2) Jitter (Optional)
        gb_j = QGroupBox("2) Jitter - Optional")
        fj = QFormLayout(gb_j)
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

        # 3) Run (Required)
        gb_run = QGroupBox("3) Run - Required")
        fr = QFormLayout(gb_run)
        desc3 = QLabel("Select overwrite or not, Set auto-deletion, create scenario file or run directly; press Play in BlueSky.")
        desc3.setStyleSheet("color: #666; font-style: italic;")

        self.autodel_chk = QCheckBox("Auto-delete at last waypoint"); self.autodel_chk.setChecked(True)

        self.scn_name = QLineEdit(); self.scn_name.setPlaceholderText("Scenario name, e.g. replay_01")
        self.rl_overwrite = QCheckBox("Overwrite scenario if it exists")

        btn_make = QPushButton("CREATE SCENARIO")
        btn_run  = QPushButton("CREATE & RUN SCENARIO")

        hb_make = QHBoxLayout(); hb_make.addWidget(self.scn_name, 1); hb_make.addWidget(btn_make); hb_make.addWidget(btn_run)

        fr.addRow(desc3)
        fr.addRow(self.rl_overwrite)
        fr.addRow(self.autodel_chk)
        fr.addRow(hb_make)

        btn_make.clicked.connect(self._make)
        btn_run.clicked.connect(self._run)

        # assemble
        main.addWidget(gb_load)
        main.addWidget(gb_j)
        main.addWidget(gb_run)
        main.addStretch(1)

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose CSV files", filter="CSV files (*.csv);All files (*)"
        )
        if files:
            self._chosen_files = files[:]     # store internally
            self.opt_auto.setChecked(False)   # switch off AUTO if user picked files


    def _load(self):
        if self.opt_auto.isChecked() or not self._chosen_files:
            _emit("SATG_RL_LOAD AUTO")
            return
        # Join full paths with commas (no quotes needed; BlueSky supports raw CSV list)
        _emit("SATG_RL_LOAD " + ",".join(self._chosen_files))

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
        seed = int(self.j_seed.value()) if hasattr(self, "j_seed") else 0
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

    def _make(self):
        name = self.scn_name.text().strip()
        if not name:
            return
        self._emit_autodel_from_toggle()
        self._emit_jitter_if_needed()
        ow = 1 if self.rl_overwrite.isChecked() else 0
        _emit(f"SATG_RL_MAKE {name} {ow}")   # positional overwrite flag

    def _run(self):
        name = self.scn_name.text().strip()
        if not name:
            return
        self._emit_autodel_from_toggle()
        self._emit_jitter_if_needed()
        ow = 1 if self.rl_overwrite.isChecked() else 0
        _emit(f"SATG_RL_RUN {name} {ow}")    # positional overwrite flag


# --- GC tab (Geometric Conflicts) ------------------------------------------

class GCTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        main = QVBoxLayout(self)

        # -------- 1) Batch options (match RC order) --------
        gb1 = QGroupBox("1) Batch options")
        f1 = QFormLayout(gb1)

        # Scenario name
        self.gc_name = QLineEdit("gc_scn")
        f1.addRow("Scenario name:", self.gc_name)

        # Types (checkboxes)
        types_box = QWidget(); hb_types = QHBoxLayout(types_box); hb_types.setContentsMargins(0,0,0,0)
        self.gc_headon = QCheckBox("Head-on");  self.gc_headon.setChecked(True)
        self.gc_cross  = QCheckBox("Crossing"); self.gc_cross.setChecked(False)
        self.gc_overtk = QCheckBox("Overtake"); self.gc_overtk.setChecked(False)
        hb_types.addWidget(self.gc_headon); hb_types.addWidget(self.gc_cross); hb_types.addWidget(self.gc_overtk); hb_types.addStretch(1)
        f1.addRow("Types:", types_box)

        # Alt mode (checkboxes)
        alt_row = QWidget(); alt_hb = QHBoxLayout(alt_row); alt_hb.setContentsMargins(0,0,0,0)
        self.gc_alt_level    = QCheckBox("Level");     self.gc_alt_level.setChecked(True)
        self.gc_alt_altcross = QCheckBox("Alt-cross"); self.gc_alt_altcross.setChecked(False)
        alt_hb.addWidget(self.gc_alt_level); alt_hb.addWidget(self.gc_alt_altcross); alt_hb.addStretch(1)
        f1.addRow("Alt mode:", alt_row)

        # TCPA [s]
        self.gc_tcpa = QDoubleSpinBox(); self.gc_tcpa.setDecimals(0); self.gc_tcpa.setRange(10, 3600); self.gc_tcpa.setValue(120)
        f1.addRow("TCPA [s]:", self.gc_tcpa)

        # Angle [deg] (cross only)
        self.gc_angle = QDoubleSpinBox(); self.gc_angle.setDecimals(0); self.gc_angle.setRange(0, 180); self.gc_angle.setValue(90)
        f1.addRow("Cross angle [deg] (cross only):", self.gc_angle)

        # Separation minima (HSEP/VSEP)
        self.gc_hsep = QDoubleSpinBox(); self.gc_hsep.setRange(0.1, 50.0); self.gc_hsep.setDecimals(2); self.gc_hsep.setValue(5.0)
        self.gc_vsep = QSpinBox();       self.gc_vsep.setRange(100, 5000);  self.gc_vsep.setValue(1000)
        f1.addRow("HSEP [NM]:", self.gc_hsep)
        f1.addRow("VSEP [ft]:", self.gc_vsep)

        # Overwrite toggle (checkbox -> 0/1 when emitting)
        self.gc_overwrite_cb = QCheckBox("Overwrite scenario if it exists")
        self.gc_overwrite_cb.setChecked(False)
        f1.addRow(self.gc_overwrite_cb)

        main.addWidget(gb1)

        # enable angle only when Crossing is selected
        def _upd_angle_enabled():
            self.gc_angle.setEnabled(self.gc_cross.isChecked())
        self.gc_cross.toggled.connect(lambda _: _upd_angle_enabled())
        _upd_angle_enabled()

        # -------- 2) CPA & ranges (match RC "region" section) --------
        gb2 = QGroupBox("2) CPA & ranges")
        f2 = QFormLayout(gb2)

        # CPA lat/lon
        self.gc_lat = QLineEdit("52.100000")
        self.gc_lon = QLineEdit("4.500000")
        f2.addRow("CPA lat [deg]:", self.gc_lat)
        f2.addRow("CPA lon [deg]:", self.gc_lon)

        # FL range
        self.gc_fl_lo = QDoubleSpinBox(); self.gc_fl_lo.setDecimals(0); self.gc_fl_lo.setRange(0, 450); self.gc_fl_lo.setValue(290)
        self.gc_fl_hi = QDoubleSpinBox(); self.gc_fl_hi.setDecimals(0); self.gc_fl_hi.setRange(0, 450); self.gc_fl_hi.setValue(370)
        self.gc_fl_lo.valueChanged.connect(lambda v: self.gc_fl_hi.setMinimum(v))
        self.gc_fl_hi.valueChanged.connect(lambda v: self.gc_fl_lo.setMaximum(v))
        row_fl = QWidget(); hb_fl = QHBoxLayout(row_fl); hb_fl.setContentsMargins(0,0,0,0)
        hb_fl.addWidget(self.gc_fl_lo); hb_fl.addWidget(QLabel(" to ")); hb_fl.addWidget(self.gc_fl_hi)
        f2.addRow("FL range (lo:hi):", row_fl)

        # CAS range
        self.gc_cas_lo = QDoubleSpinBox(); self.gc_cas_lo.setDecimals(0); self.gc_cas_lo.setRange(100, 600); self.gc_cas_lo.setValue(220)
        self.gc_cas_hi = QDoubleSpinBox(); self.gc_cas_hi.setDecimals(0); self.gc_cas_hi.setRange(100, 600); self.gc_cas_hi.setValue(280)
        self.gc_cas_lo.valueChanged.connect(lambda v: self.gc_cas_hi.setMinimum(v))
        self.gc_cas_hi.valueChanged.connect(lambda v: self.gc_cas_lo.setMaximum(v))
        row_cas = QWidget(); hb_cas = QHBoxLayout(row_cas); hb_cas.setContentsMargins(0,0,0,0)
        hb_cas.addWidget(self.gc_cas_lo); hb_cas.addWidget(QLabel(" to ")); hb_cas.addWidget(self.gc_cas_hi)
        f2.addRow("CAS range [kt] (lo:hi):", row_cas)

        main.addWidget(gb2)

        # -------- 3) Actions --------
        gb3 = QGroupBox("3) Actions")
        f3 = QFormLayout(gb3)

        btn_cre  = QPushButton("CREATE SCENARIO")
        btn_run  = QPushButton("RUN SCENARIO")
        btn_both = QPushButton("CREATE & RUN SCENARIO")
        row_act = QWidget(); hb_act = QHBoxLayout(row_act); hb_act.setContentsMargins(0,0,0,0)
        hb_act.addWidget(btn_cre); hb_act.addWidget(btn_run); hb_act.addWidget(btn_both)
        f3.addRow(row_act)

        main.addWidget(gb3)

        # wire actions
        btn_cre.clicked.connect(self._gc_create)
        btn_run.clicked.connect(self._gc_run_only)
        btn_both.clicked.connect(self._gc_create_and_run)

        

    # ---------- helpers ----------
    def _gc_types_csv(self) -> str:
        sel = []
        if self.gc_headon.isChecked(): sel.append("headon")
        if self.gc_cross.isChecked():  sel.append("cross")
        if self.gc_overtk.isChecked(): sel.append("overtake")
        return ",".join(sel)

    def _gc_ensure_types(self) -> bool:
        if self.gc_headon.isChecked() or self.gc_cross.isChecked() or self.gc_overtk.isChecked():
            return True
        _emit("ECHO Please select at least one conflict type.")
        return False

    def _gc_altmode(self) -> str:
        a = self.gc_alt_level.isChecked()
        b = self.gc_alt_altcross.isChecked()
        if a and b: return "mix"
        if a: return "level"
        if b: return "altcross"
        return "level"

    # ---------- emitters ----------
    def _emit_gc_conf(self):
        _emit(f"SATG_GC_CONF {self.gc_hsep.value()} {self.gc_vsep.value()}")

    def _emit_gc_range(self):
        fl_lo, fl_hi   = int(self.gc_fl_lo.value()),  int(self.gc_fl_hi.value())
        cas_lo, cas_hi = int(self.gc_cas_lo.value()), int(self.gc_cas_hi.value())
        toks = ["SATG_GC_RANGE", _kv("fl", f"{fl_lo}:{fl_hi}"), _kv("cas", f"{cas_lo}:{cas_hi}")]
        _emit(_join_tokens(*toks))

    def _emit_gc_cre(self):
        if not self._gc_ensure_types():
            return
        name = self.gc_name.text().strip()
        if not name:
            _emit("ECHO Please provide a scenario name.")
            return
        types_csv = self._gc_types_csv()
        altmode   = self._gc_altmode()
        ow        = 1 if self.gc_overwrite_cb.isChecked() else 0

        toks = [
            "SATG_GC_CRE",
            _kv("name",   name),
            _kv("typ",    types_csv),
            _kv("altmode", altmode),
            _kv("lat",    self.gc_lat.text().strip()),
            _kv("lon",    self.gc_lon.text().strip()),
            _kv("tcpa",   int(self.gc_tcpa.value())),
            _kv("overwrite", ow),
        ]
        if "cross" in {t.strip() for t in types_csv.split(",") if t.strip()}:
            toks.append(_kv("angle", int(self.gc_angle.value())))
        _emit(_join_tokens(*toks))

    # ---------- actions ----------
    def _gc_create(self):
        # Push minima and ranges first, then create (like RC)
        self._emit_gc_conf()
        self._emit_gc_range()
        self._emit_gc_cre()

    def _gc_run_only(self):
        name = self.gc_name.text().strip()
        if name:
            _emit("SATG_GC_RUN " + name)

    def _gc_create_and_run(self):
        self._gc_create()
        self._gc_run_only()


# --- RC tab (Random Conflicts) ---------------------------------------------

class RCTab(QWidget):
    """Random Conflicts (RC) — Circle-only region, types via checkboxes, run buttons."""
    def __init__(self, parent=None):
        super().__init__(parent)

        main = QVBoxLayout(self)
        main.setContentsMargins(10,10,10,10)
        main.setSpacing(10)

        # 1) Batch options
        gb1 = QGroupBox("1) Batch options")
        f1 = QFormLayout(gb1)

        self.scn = QLineEdit("rc_circle")
        self.n = QSpinBox(); self.n.setRange(1, 100000); self.n.setValue(20)

        # Types as checkboxes
        types_box = QWidget(); hb = QHBoxLayout(types_box); hb.setContentsMargins(0,0,0,0)
        self.cb_headon   = QCheckBox("Head-on");  self.cb_headon.setChecked(True)
        self.cb_cross    = QCheckBox("Crossing"); self.cb_cross.setChecked(True)
        self.cb_overtake = QCheckBox("Overtake"); self.cb_overtake.setChecked(True)
        hb.addWidget(self.cb_headon); hb.addWidget(self.cb_cross); hb.addWidget(self.cb_overtake); hb.addStretch(1)

        alt_row = QWidget(); alt_hb = QHBoxLayout(alt_row); alt_hb.setContentsMargins(0,0,0,0)
        self.alt_level   = QCheckBox("Level");    self.alt_level.setChecked(True)
        self.alt_altcross= QCheckBox("Alt-cross"); # unchecked default
        alt_hb.addWidget(self.alt_level); alt_hb.addWidget(self.alt_altcross); alt_hb.addStretch(1)
        self.seed = QSpinBox(); self.seed.setRange(0, 2**31-1); self.seed.setValue(0)
        self.hsep = QDoubleSpinBox(); self.hsep.setRange(0.1, 50.0); self.hsep.setDecimals(2); self.hsep.setValue(5.0)
        self.vsep = QSpinBox();       self.vsep.setRange(100, 5000);  self.vsep.setValue(1000)

        self.actypes = QLineEdit("A320,B738,A350,B78X")
        # Overwrite toggle (checkbox -> 0/1 when emitting)
        self.gc_overwrite_cb = QCheckBox("Overwrite scenario if it exists")
        self.gc_overwrite_cb.setChecked(False)

        # TCPA lo/hi (seconds)
        self.tcpa_lo = QDoubleSpinBox(); self.tcpa_lo.setDecimals(0); self.tcpa_lo.setRange(0, 3600); self.tcpa_lo.setValue(60)
        self.tcpa_hi = QDoubleSpinBox(); self.tcpa_hi.setDecimals(0); self.tcpa_hi.setRange(0, 3600); self.tcpa_hi.setValue(240)
        # keep lo <= hi
        self.tcpa_lo.valueChanged.connect(lambda v: self.tcpa_hi.setMinimum(v))
        self.tcpa_hi.valueChanged.connect(lambda v: self.tcpa_lo.setMaximum(v))
        row_tcpa = QWidget(); hb_tcpa = QHBoxLayout(row_tcpa); hb_tcpa.setContentsMargins(0,0,0,0)
        hb_tcpa.addWidget(self.tcpa_lo); hb_tcpa.addWidget(QLabel(" to ")); hb_tcpa.addWidget(self.tcpa_hi)

        # Cross angle lo/hi (deg)
        self.ang_lo = QDoubleSpinBox(); self.ang_lo.setDecimals(0); self.ang_lo.setRange(0, 180); self.ang_lo.setValue(60)
        self.ang_hi = QDoubleSpinBox(); self.ang_hi.setDecimals(0); self.ang_hi.setRange(0, 180); self.ang_hi.setValue(120)
        self.ang_lo.valueChanged.connect(lambda v: self.ang_hi.setMinimum(v))
        self.ang_hi.valueChanged.connect(lambda v: self.ang_lo.setMaximum(v))
        row_ang = QWidget(); hb_ang = QHBoxLayout(row_ang); hb_ang.setContentsMargins(0,0,0,0)
        hb_ang.addWidget(self.ang_lo); hb_ang.addWidget(QLabel(" to ")); hb_ang.addWidget(self.ang_hi)
        
        f1.addRow("Scenario name:", self.scn)
        f1.addRow("Number of conflicts (n):", self.n)
        f1.addRow("Types:", types_box)
        f1.addRow("Alt mode:", alt_row)
        f1.addRow("TCPA [s] (lo:hi):", row_tcpa)
        f1.addRow("Cross angle [deg] (lo:hi):", row_ang)
        f1.addRow("Seed (0=none):", self.seed)
        f1.addRow("HSEP [NM]:", self.hsep)
        f1.addRow("VSEP [ft]:", self.vsep)
        f1.addRow("AC types:", self.actypes)
        f1.addRow(self.gc_overwrite_cb)

        def _upd_angle_enabled():
            self.ang_lo.setEnabled(self.cb_cross.isChecked())
            self.ang_hi.setEnabled(self.cb_cross.isChecked())

        self.cb_cross.toggled.connect(lambda _: _upd_angle_enabled())
        _upd_angle_enabled()

        # 2) Circle region
        gb2 = QGroupBox("2) Circle region")
        f2 = QFormLayout(gb2)

        # Make the note a widget, style the widget (not the layout), then add it
        desc = QLabel("CPA uniformly sampled in a circle. All aircraft spawn at t=0; CPA time equals TCPA.")
        desc.setStyleSheet("color: #666; font-style: italic;")
        # Add as a full-width row in the form
        f2.addRow(desc)


        self.c_lat = QLineEdit("52.10")
        self.c_lon = QLineEdit("4.50")
        self.c_rad = QDoubleSpinBox(); self.c_rad.setRange(0.1, 1000.0); self.c_rad.setDecimals(2); self.c_rad.setValue(25.0)

        # FL lo/hi (flight levels)
        self.fl_lo = QDoubleSpinBox(); self.fl_lo.setDecimals(0); self.fl_lo.setRange(0, 500); self.fl_lo.setValue(290)
        self.fl_hi = QDoubleSpinBox(); self.fl_hi.setDecimals(0); self.fl_hi.setRange(0, 500); self.fl_hi.setValue(370)
        self.fl_lo.valueChanged.connect(lambda v: self.fl_hi.setMinimum(v))
        self.fl_hi.valueChanged.connect(lambda v: self.fl_lo.setMaximum(v))
        row_fl = QWidget(); hb_fl = QHBoxLayout(row_fl); hb_fl.setContentsMargins(0,0,0,0)
        hb_fl.addWidget(self.fl_lo); hb_fl.addWidget(QLabel(" to ")); hb_fl.addWidget(self.fl_hi)

        # CAS lo/hi (kt)
        self.cas_lo = QDoubleSpinBox(); self.cas_lo.setDecimals(0); self.cas_lo.setRange(100, 600); self.cas_lo.setValue(220)
        self.cas_hi = QDoubleSpinBox(); self.cas_hi.setDecimals(0); self.cas_hi.setRange(100, 600); self.cas_hi.setValue(280)
        self.cas_lo.valueChanged.connect(lambda v: self.cas_hi.setMinimum(v))
        self.cas_hi.valueChanged.connect(lambda v: self.cas_lo.setMaximum(v))
        row_cas = QWidget(); hb_cas = QHBoxLayout(row_cas); hb_cas.setContentsMargins(0,0,0,0)
        hb_cas.addWidget(self.cas_lo); hb_cas.addWidget(QLabel(" to ")); hb_cas.addWidget(self.cas_hi)
        
        f2.addRow("Center lat [deg]:", self.c_lat)
        f2.addRow("Center lon [deg]:", self.c_lon)
        f2.addRow("Radius [NM]:", self.c_rad)
        f2.addRow("FL range (lo:hi):", row_fl)
        f2.addRow("CAS range [kt] (lo:hi):", row_cas)

        # 3) Actions
        gb3 = QGroupBox("3) Actions")
        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0,0,0,0); h.setSpacing(8)
        self.btn_cre = QPushButton("CREATE SCENARIO")
        self.btn_run = QPushButton("RUN SCENARIO")
        self.btn_both= QPushButton("CREATE & RUN SCENARIO")
        self.btn_cre.clicked.connect(self._create)
        self.btn_run.clicked.connect(self._run)
        self.btn_both.clicked.connect(self._create_and_run)
        h.addWidget(self.btn_cre); h.addWidget(self.btn_run); h.addWidget(self.btn_both); h.addStretch(1)
        lay3 = QVBoxLayout(gb3); lay3.addWidget(row)

        main.addWidget(gb1); main.addWidget(gb2); main.addWidget(gb3); main.addStretch(1)

    def _types_csv(self) -> str:
        t = []
        if self.cb_headon.isChecked():   t.append("headon")
        if self.cb_cross.isChecked():    t.append("cross")
        if self.cb_overtake.isChecked(): t.append("overtake")
        return ",".join(t)

    def _ensure_types(self) -> bool:
        if self._types_csv(): return True
        _emit("ECHO SATGGUI: Select at least one type.")
        return False

    def _create(self):
        # 0) Must have at least one conflict type selected
        if not self._ensure_types():
            return

        # 1) Alt mode from checkboxes
        if self.alt_level.isChecked() and self.alt_altcross.isChecked():
            altmode_val = "mix"
        elif self.alt_level.isChecked():
            altmode_val = "level"
        elif self.alt_altcross.isChecked():
            altmode_val = "altcross"
        else:
            altmode_val = "level"  # fallback

        # 2) Push HSEP/VSEP first so backend minima are in sync
        _emit(f"SATG_GC_CONF {self.hsep.value()} {self.vsep.value()}")

        # 3) Gather inputs
        name_val   = self.scn.text().strip()
        types_csv  = self._types_csv()  # from head-on / crossing / overtake checkboxes
        center_lat = self.c_lat.text().strip()
        center_lon = self.c_lon.text().strip()
        radius_nm  = float(self.c_rad.value())
        seed_val   = int(self.seed.value())
        actypes_val = self.actypes.text().strip()
        overwrite_val = 1 if self.gc_overwrite_cb.isChecked() else 0

        # Ranges from spin boxes -> "lo:hi"
        tcpa_lo, tcpa_hi = int(self.tcpa_lo.value()), int(self.tcpa_hi.value())   # seconds
        fl_lo,   fl_hi   = int(self.fl_lo.value()),   int(self.fl_hi.value())     # flight levels
        cas_lo,  cas_hi  = int(self.cas_lo.value()),  int(self.cas_hi.value())    # knots

        # 4) Build command tokens
        toks = [
            "SATG_RC_CIRCLE",
            _kv("name", name_val),
            _kv("n", self.n.value()),
            _kv("types", types_csv),
            _kv("center_lat", center_lat),
            _kv("center_lon", center_lon),
            _kv("radius_nm", radius_nm),
            _kv("altmode", altmode_val),
            _kv("tcpa", f"{tcpa_lo}:{tcpa_hi}"),
            _kv("fl",   f"{fl_lo}:{fl_hi}"),
            _kv("cas",  f"{cas_lo}:{cas_hi}"),
            _kv("actypes", actypes_val),
            _kv("overwrite", overwrite_val),
        ]

        # Angle only matters if 'cross' is selected
        type_set = {t.strip() for t in types_csv.split(",") if t.strip()}
        if "cross" in type_set:
            ang_lo, ang_hi = int(self.ang_lo.value()), int(self.ang_hi.value())
            toks.append(_kv("angle", f"{ang_lo}:{ang_hi}"))

        # Seed is optional; omit if 0
        if seed_val != 0:
            toks.append(_kv("seed", seed_val))

        # 5) Emit
        _emit(_join_tokens(*toks))

    def _run(self):
        nm = self.scn.text().strip()
        if nm: _emit(_join_tokens("SATG_GC_RUN", _kv("name", nm)))
        else:  _emit("ECHO SATGGUI: Set a scenario name before running.")

    def _create_and_run(self):
        self._create()
        self._run()
        
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
        main = QVBoxLayout(self)

        # 1) Files
        gb1 = QGroupBox("1) Files")
        f1 = QFormLayout(gb1)

        # Waypoint files
        self.lst_wpt = QListWidget()
        self.lst_wpt.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        wpt_btns = QWidget(); hbw = QHBoxLayout(wpt_btns); hbw.setContentsMargins(0,0,0,0)
        btn_wpt_add = QPushButton("Add waypoint files…")
        btn_wpt_rm  = QPushButton("Remove selected")
        btn_wpt_clr = QPushButton("Clear all")
        hbw.addWidget(btn_wpt_add); hbw.addWidget(btn_wpt_rm); hbw.addWidget(btn_wpt_clr); hbw.addStretch(1)
        f1.addRow(QLabel("Waypoint .scn (DEFWPT):"))
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
        f1.addRow(QLabel("Procedure .scn (with %0, e.g., SID-04-GORLO.scn):"))
        f1.addRow(self.lst_proc)
        f1.addRow(proc_btns)

        main.addWidget(gb1)

        btn_wpt_add.clicked.connect(self._add_wpt)
        btn_wpt_rm.clicked.connect(self._rm_wpt)
        btn_wpt_clr.clicked.connect(self._clr_wpt)
        btn_proc_add.clicked.connect(self._add_proc)
        btn_proc_rm.clicked.connect(self._rm_proc)
        btn_proc_clr.clicked.connect(self._clr_proc)

        # 2) Batch options
        gb2 = QGroupBox("2) Batch options")
        f2 = QFormLayout(gb2)

        self.nflights = QSpinBox(); self.nflights.setRange(1, 100000); self.nflights.setValue(20)
        f2.addRow("Number of flights:", self.nflights)

        self.minsep = QDoubleSpinBox(); self.minsep.setDecimals(0); self.minsep.setRange(0, 3600); self.minsep.setValue(30)
        f2.addRow("Min separation on same procedure [s]:", self.minsep)

        # Spawn geometry
        self.spawn_r = QDoubleSpinBox(); self.spawn_r.setDecimals(1); self.spawn_r.setRange(0.1, 100.0); self.spawn_r.setValue(5.0)
        f2.addRow("Spawn radius around first fix [NM]:", self.spawn_r)

        self.spawn_env = QDoubleSpinBox(); self.spawn_env.setDecimals(0); self.spawn_env.setRange(5, 180); self.spawn_env.setValue(40)
        f2.addRow("Angular envelope around inbound bearing [deg]:", self.spawn_env)

        self.seed = QSpinBox(); self.seed.setRange(0, 2_000_000_000); self.seed.setValue(0)
        f2.addRow("Seed (0 = random):", self.seed)

        main.addWidget(gb2)

        # 3) Scenario and actions
        gb3 = QGroupBox("3) Scenario and actions")
        f3 = QFormLayout(gb3)

        self.scn = QLineEdit("proc_scn")
        f3.addRow("Scenario name:", self.scn)

        self.overwrite = QCheckBox("Overwrite scenario if it exists")
        self.overwrite.setChecked(False)
        f3.addRow(self.overwrite)

        row_act = QWidget(); hb3 = QHBoxLayout(row_act); hb3.setContentsMargins(0,0,0,0)
        btn_cre  = QPushButton("CREATE SCENARIO FILE")
        btn_run  = QPushButton("RUN SCENARIO")
        btn_both = QPushButton("CREATE & RUN SCENARIO")
        hb3.addWidget(btn_cre); hb3.addWidget(btn_run); hb3.addWidget(btn_both)
        f3.addRow(row_act)

        main.addWidget(gb3)

        btn_cre.clicked.connect(self._make)
        btn_run.clicked.connect(self._run_only)
        btn_both.clicked.connect(self._make_and_run)

    # ---- file ops ----
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
            self.lst_proc.addItem(p)

    def _rm_proc(self):
        sel = self.lst_proc.selectedItems()
        for it in sel:
            p = it.text()
            _emit(_join_tokens("SATG_PROC_UNLOAD_PROC", _qpath(p)))
            self._proc_files = [x for x in self._proc_files if x != p]
            self.lst_proc.takeItem(self.lst_proc.row(it))

    def _clr_proc(self):
        if not self._proc_files: return
        _emit("SATG_PROC_CLEAR_PROC")
        self._proc_files.clear(); self.lst_proc.clear()

    # ---- actions ----
    def _make(self):
        if not self._proc_files:
            _emit("ECHO Add at least one procedure file.")
            return
        name = self.scn.text().strip()
        if not name:
            _emit("ECHO Provide a scenario name.")
            return

        n   = int(self.nflights.value())
        rnm = float(self.spawn_r.value())
        env = float(self.spawn_env.value())
        msp = int(self.minsep.value())
        s   = int(self.seed.value())
        ow  = 1 if self.overwrite.isChecked() else 0

        # strictly positional to satisfy BlueSky argparser
        cmd = f"SATG_PROC_MAKE {name} {n} {rnm} {env} {msp} {s} {ow}"
        _emit(cmd)

    def _run_only(self):
        name = self.scn.text().strip()
        if not name:
            _emit("ECHO Provide a scenario name.")
            return
        _emit("SATG_PROC_RUN " + name)

    def _make_and_run(self):
        self._make()
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
            "- When loading other .scn files inside a scenario, SATG writes IC lines using\n"
            "  relative paths so the scenario stays portable.\n"
            "\n"
            "Random conflicts in a circle (GUI tab: Random Conflicts)\n"
            "--------------------------------------------------------\n"
            "Command: SATG_RC_CIRCLE name N types center_lat center_lon radius_nm altmode tcpa fl cas actypes overwrite [angle]\n"
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
            "- The scenario will IC the waypoint files first, then the procedure files, at time 0.\n"
            "\n"
            "Create\n"
            "Command: SATG_PROC_MAKE name N radius_nm envelope_deg minsep seed overwrite\n"
            "Meaning\n"
            "- N: number of flights to spawn.\n"
            "- radius_nm: spawn radius around the first fix of the chosen procedure.\n"
            "- envelope_deg: angular envelope around the inbound direction to the first fix.\n"
            "  The program samples a bearing inside that envelope so the aircraft arrives at the first fix,\n"
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
            "- If a command says an argument is lo:hi, it means two numbers separated by a colon, for example 60:240.\n"
            "- If you see path errors on IC, check that your base folder is correct and that the included .scn files exist.\n"
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
        
        tabs.addTab(HelpTab(self), "Help")
        tabs.addTab(RLTab(self), "Realistic Replay")
        tabs.addTab(GCTab(self), "Geometric Conflicts")
        tabs.addTab(RCTab(self), "Random Conflicts")
        tabs.addTab(ProcTab(self), "Procedures")

        self.top = TopStrip(self)

        layout.addWidget(self.top)
        layout.addWidget(tabs, 1)

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
