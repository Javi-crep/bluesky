# SATGgui.py -- BlueSky GUI plugin for SATG command front-end (no GUI echo log)
# Place in: bluesky/plugins/
#
# PyQt6; lazy window creation to avoid QApplication race.

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QCheckBox, QComboBox, QPushButton, QSpinBox,
    QDoubleSpinBox, QFileDialog, QSlider
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
    """Geometric Conflicts: minima/ranges -> create -> run/delete."""
    def __init__(self, parent=None):
        super().__init__(parent)
        main = QVBoxLayout(self)

        # 1) Separation minima (Optional)
        gb_conf = QGroupBox("1) Separation minima — Optional")
        fc = QFormLayout(gb_conf)
        desc1 = QLabel("Set horizontal/vertical minima for encounter synthesis (informational).")
        desc1.setStyleSheet("color: #666; font-style: italic;")

        self.hsep = QDoubleSpinBox(); self.hsep.setRange(0.1, 50.0); self.hsep.setValue(5.0)
        self.vsep = QSpinBox();       self.vsep.setRange(100, 5000); self.vsep.setValue(1000)
        btn_conf = QPushButton("SATG_GC_CONF")

        fc.addRow(desc1)
        fc.addRow("HSEP [NM]:", self.hsep)
        fc.addRow("VSEP [ft]:", self.vsep)
        fc.addRow(btn_conf)
        btn_conf.clicked.connect(lambda: _emit(f"SATG_GC_CONF {self.hsep.value()} {self.vsep.value()}"))

        # 2) Sampling ranges (Optional)
        gb_rng = QGroupBox("2) Sampling ranges — Optional")
        fr = QFormLayout(gb_rng)
        desc2 = QLabel("Define CAS/FL/bearing/angle ranges used when creating encounters.")
        desc2.setStyleSheet("color: #666; font-style: italic;")

        self.cas1 = QLineEdit("220:280"); self.cas2 = QLineEdit("220:280")
        self.fl1  = QLineEdit("290:370"); self.fl2  = QLineEdit("290:370")
        self.brg1 = QLineEdit("0:359");   self.ang  = QLineEdit("60:120")
        btn_rng = QPushButton("SATG_GC_RANGE")

        fr.addRow(desc2)
        fr.addRow("cas1 [kt lo:hi]:", self.cas1); fr.addRow("cas2 [kt lo:hi]:", self.cas2)
        fr.addRow("fl1 [FL lo:hi]:", self.fl1);   fr.addRow("fl2 [FL lo:hi]:", self.fl2)
        fr.addRow("brg1 [deg lo:hi]:", self.brg1); fr.addRow("angle [deg lo:hi]:", self.ang)
        fr.addRow(btn_rng)
        btn_rng.clicked.connect(self._ranges)

        # 3) Create encounters (Required)
        gb_cre = QGroupBox("3) Create encounters — Required")
        fcr = QFormLayout(gb_cre)
        desc3 = QLabel("Append one or more encounters to a scenario definition.")
        desc3.setStyleSheet("color: #666; font-style: italic;")

        self.name = QLineEdit("gc_demo")
        self.typ  = QComboBox(); self.typ.addItems(["headon", "cross", "overtake"])
        self.altm = QComboBox(); self.altm.addItems(["level", "altcross"])
        self.lat  = QLineEdit("52.10"); self.lon = QLineEdit("4.50")
        self.tcpa = QLineEdit("180")
        self.angle= QLineEdit("60:120")  # crossing/altcross only
        self.overwrite = QCheckBox("Overwrite existing file")
        btn_cre = QPushButton("SATG_GC_CRE")

        fcr.addRow(desc3)
        fcr.addRow("scenario name:", self.name)
        fcr.addRow("type:", self.typ)
        fcr.addRow("altmode:", self.altm)
        fcr.addRow("CPA lat:", self.lat)
        fcr.addRow("CPA lon:", self.lon)
        fcr.addRow("tcpa [s]:", self.tcpa)
        fcr.addRow("angle [deg, crossing only]:", self.angle)
        fcr.addRow("Overwrite (0/1):", self.overwrite)
        fcr.addRow(btn_cre)
        btn_cre.clicked.connect(self._cre)

        # 4) Run/Purge (Required)
        gb_run = QGroupBox("4) Run / Purge — Required")
        frun = QFormLayout(gb_run)
        desc4 = QLabel("Load scenario into BlueSky, press Play. Purge removes spawned aircraft.")
        desc4.setStyleSheet("color: #666; font-style: italic;")

        btn_run = QPushButton("SATG_GC_RUN")
        btn_del = QPushButton("SATG_GC_DEL")

        frun.addRow(desc4)
        frun.addRow(btn_run)
        frun.addRow(btn_del)

        btn_run.clicked.connect(lambda: _emit("SATG_GC_RUN " + self.name.text().strip()))
        btn_del.clicked.connect(lambda: _emit("SATG_GC_DEL"))

        # assemble
        main.addWidget(gb_conf)
        main.addWidget(gb_rng)
        main.addWidget(gb_cre)
        main.addWidget(gb_run)
        main.addStretch(1)

    def _ranges(self):
        toks = ["SATG_GC_RANGE",
                _kv("cas1", self.cas1.text().strip()),
                _kv("cas2", self.cas2.text().strip()),
                _kv("fl1",  self.fl1.text().strip()),
                _kv("fl2",  self.fl2.text().strip()),
                _kv("brg1", self.brg1.text().strip()),
                _kv("angle",self.ang.text().strip())]
        _emit(_join_tokens(*toks))

    def _cre(self):
        name = self.name.text().strip()
        typ  = self.typ.currentText().strip()
        altm = self.altm.currentText().strip()
        lat  = self.lat.text().strip()
        lon  = self.lon.text().strip()
        tcpa = self.tcpa.text().strip()
        angle= self.angle.text().strip()
        toks = ["SATG_GC_CRE",
                _kv("name", name),
                _kv("type", typ),
                _kv("altmode", altm),
                _kv("lat", lat), _kv("lon", lon),
                _kv("tcpa", tcpa),
                _kv("angle", angle) if angle else "",
                _kv("overwrite", 1 if self.overwrite.isChecked() else 0)]
        _emit(_join_tokens(*toks))

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
        self.tcpa = QLineEdit("60:240")     # seconds or lo:hi
        self.angle = QLineEdit("")          # optional lo:hi for cross
        self.seed = QSpinBox(); self.seed.setRange(0, 2**31-1); self.seed.setValue(0)
        self.hsep = QDoubleSpinBox(); self.hsep.setRange(0.1, 50.0); self.hsep.setDecimals(2); self.hsep.setValue(5.0)
        self.vsep = QSpinBox();       self.vsep.setRange(100, 5000);  self.vsep.setValue(1000)

        self.actypes = QLineEdit("A320,B738")
        self.overwrite = QCheckBox("Overwrite existing file")

        f1.addRow("Scenario name:", self.scn)
        f1.addRow("Number of conflicts (n):", self.n)
        f1.addRow("Types:", types_box)
        f1.addRow("Alt mode:", alt_row)
        f1.addRow("TCPA [s] (x or lo:hi):", self.tcpa)
        f1.addRow("Cross angle [deg] (lo:hi, optional):", self.angle)
        f1.addRow("Seed (0=none):", self.seed)
        f1.addRow("HSEP [NM]:", self.hsep)
        f1.addRow("VSEP [ft]:", self.vsep)
        f1.addRow("AC types (CSV):", self.actypes)
        f1.addRow("Overwrite (0/1):", self.overwrite)

        # 2) Circle region
        gb2 = QGroupBox("2) Circle region")
        f2 = QFormLayout(gb2)
        f2.addRow(QLabel("CPA uniformly sampled in a circle. All aircraft spawn at t=0; CPA time equals TCPA."))

        self.c_lat = QLineEdit("52.10")
        self.c_lon = QLineEdit("4.50")
        self.c_rad = QDoubleSpinBox(); self.c_rad.setRange(0.1, 1000.0); self.c_rad.setDecimals(2); self.c_rad.setValue(25.0)
        self.c_fl  = QLineEdit("290:370")
        self.c_cas = QLineEdit("220:280")

        f2.addRow("Center lat [deg]:", self.c_lat)
        f2.addRow("Center lon [deg]:", self.c_lon)
        f2.addRow("Radius [NM]:", self.c_rad)
        f2.addRow("FL range (lo:hi, optional):", self.c_fl)
        f2.addRow("CAS range [kt] (lo:hi, optional):", self.c_cas)

        # 3) Actions
        gb3 = QGroupBox("3) Actions")
        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0,0,0,0); h.setSpacing(8)
        self.btn_cre = QPushButton("Create — SATG_RC_CIRCLE")
        self.btn_run = QPushButton("Run scenario")
        self.btn_both= QPushButton("Create & Run")
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
        if not self._ensure_types(): return

        if self.alt_level.isChecked() and self.alt_altcross.isChecked():
            altmode_val = "mix"
        elif self.alt_level.isChecked():
            altmode_val = "level"
        elif self.alt_altcross.isChecked():
            altmode_val = "altcross"
        else:
            altmode_val = "level"

        _emit(f"SATG_GC_CONF {self.hsep.value()} {self.vsep.value()}")

        toks = [
            "SATG_RC_CIRCLE",
            _kv("name", self.scn.text().strip()),
            _kv("n", self.n.value()),
            _kv("types", self._types_csv()),
            _kv("center_lat", self.c_lat.text().strip()),
            _kv("center_lon", self.c_lon.text().strip()),
            _kv("radius_nm", self.c_rad.value()),
            _kv("altmode", altmode_val),
            _kv("tcpa", self.tcpa.text().strip()),
            _kv("actypes", self.actypes.text().strip()),  # <-- NEW
        ]
        ang = self.angle.text().strip()
        if ang: toks.append(_kv("angle", ang))
        sd = self.seed.value()
        if sd != 0: toks.append(_kv("seed", sd))
        fl = self.c_fl.text().strip()
        if fl: toks.append(_kv("fl", fl))
        cas = self.c_cas.text().strip()
        if cas: toks.append(_kv("cas", cas))
        toks.append(_kv("overwrite", 1 if self.overwrite.isChecked() else 0))

        _emit(_join_tokens(*toks))

    def _run(self):
        nm = self.scn.text().strip()
        if nm: _emit(_join_tokens("SATG_GC_RUN", _kv("name", nm)))
        else:  _emit("ECHO SATGGUI: Set a scenario name before running.")

    def _create_and_run(self):
        self._create()
        self._run()

# --- main window ------------------------------------------------------------

class SATGWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SATG GUI")
        self.resize(980, 720)
        layout = QVBoxLayout(self)

        tabs = QTabWidget(self)
        tabs.addTab(RLTab(self), "Realistic Replay")
        tabs.addTab(GCTab(self), "Geometric Conflicts")
        tabs.addTab(RCTab(self), "Random Conflicts")

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
