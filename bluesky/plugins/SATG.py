"""
SATG: Scenario generator (Realistic Replay + Geometric Conflicts)

- RL mode (SATG_RL_*): read pre-filtered CSV (by headers), convert GS->CAS (ISA, wind=0),
  build scenarios where each aircraft is fully routed at t0, LNAV/VNAV ON.
- GC mode (SATG_GC_*): synthesize 2-aircraft encounters (head-on / crossing / overtake),
  CPA at given lat/lon and time-to-CPA; initial CAS/FL/heading sampled from ranges.
  * SATG_GC_CRE appends conflicts to the same .scn if it already exists.
    Only the first creation writes the file header (HOLD + ASAS ON).

Folders (idempotent):
  ./satg_data/data/       <-- put CSVs here (RL AUTO)
  ./satg_data/scenarios/  <-- .scn outputs
"""

import os, math, csv, re, random
from datetime import timedelta
from typing import Dict, List, Optional, Tuple
import re

from bluesky import stack
try:
    import bluesky as bs
except Exception:
    bs = None
from bluesky.stack import command
from bluesky.tools import geo
from bluesky.tools import misc as _misc
import bluesky.traffic.traffic as _traf_mod


def _satg_safe_lat2txt(lat: float) -> str:
    """Robust lat formatter that tolerates numpy scalars."""
    latf = float(lat)
    d, m, s = _misc.float2degminsec(abs(latf))
    d = int(round(d))
    m = int(round(m))
    s = int(round(s))
    prefix = "S" if latf < 0 else "N"
    return f"{prefix}{d:02d}'{m:02d}'{s}\""


def _satg_safe_lon2txt(lon: float) -> str:
    """Robust lon formatter that tolerates numpy scalars."""
    lonf = float(lon)
    d, m, s = _misc.float2degminsec(abs(lonf))
    d = int(round(d))
    m = int(round(m))
    s = int(round(s))
    prefix = "W" if lonf < 0 else "E"
    return f"{prefix}{d:03d}'{m:02d}'{s}\""


def _satg_safe_latlon2txt(lat: float, lon: float) -> str:
    return f"{_satg_safe_lat2txt(lat)}  {_satg_safe_lon2txt(lon)}"


if getattr(_misc.lat2txt, "__name__", "") != "_satg_safe_lat2txt":
    _misc.lat2txt = _satg_safe_lat2txt
    _misc.lon2txt = _satg_safe_lon2txt
    _misc.latlon2txt = _satg_safe_latlon2txt
    _traf_mod.latlon2txt = _satg_safe_latlon2txt

_SID_FILE_RE = re.compile(r'^SID-([0-9]{2,3}[LRC]?)-([-A-Za-z0-9_]+)\.scn$', re.IGNORECASE)
_STAR_FILE_RE = re.compile(r'^STAR-([-A-Za-z0-9_]+)\.scn$', re.IGNORECASE)
DEFAULT_SID_RATE = 40.0  # aircraft per hour
DEFAULT_STAR_RATE = 20.0  # aircraft per hour

# ---------------- ISA + GS->CAS (wind=0) ---------------- #
GAMMA = 1.4; R = 287.05287; G0 = 9.80665
T0 = 288.15; P0 = 101325.0; L = 0.0065
T_TROP = 216.65; H_TROP = 11000.0
A0 = math.sqrt(GAMMA*R*T0)
FT2M = 0.3048; MS2KT = 1.943844492

def _isa_tp(h_m: float) -> Tuple[float, float]:
    if h_m < 0: h_m = 0.0
    if h_m <= H_TROP:
        T = T0 - L*h_m
        p = P0 * (T/T0)**(G0/(R*L))
    else:
        T = T_TROP
        p_trop = P0 * (T_TROP/T0)**(G0/(R*L))
        p = p_trop * math.exp(-G0*(h_m - H_TROP)/(R*T))
    return T, p

def _gs_to_cas_kt(gs_kt: float, flight_level: float) -> float:
    tas_ms = gs_kt / MS2KT  # wind=0 => TAS≈GS
    h_m = float(flight_level) * 100.0 * FT2M
    T, p = _isa_tp(h_m)
    a = math.sqrt(GAMMA*R*T)
    M = max(tas_ms / a, 0.0)
    qc = p * ((1 + 0.2*M*M)**3.5 - 1.0)
    qcp = max(qc / P0 + 1.0, 1.0)
    cas_ms = A0 * math.sqrt(5.0 * (qcp**(2.0/7.0) - 1.0))
    if M < 0.1:
        rho  = p/(R*T); rho0 = P0/(R*T0)
        cas_ms = tas_ms * math.sqrt(rho/rho0)
    return cas_ms * MS2KT

# ---------------- Helpers ---------------- #
def _to_float(s, default=0.0):
    try: return float(s)
    except Exception: return default

def _to_int(s, default=0):
    try: return int(float(s))
    except Exception: return default

def _to_td(tval) -> timedelta:
    s = str(tval).strip()
    if ":" in s:
        h, m, sec = s.split(":")
        return timedelta(hours=int(h), minutes=int(m), seconds=float(sec))
    return timedelta(seconds=float(s))

def _stamp(td: timedelta) -> str:
    total = td.total_seconds()
    h = int(total//3600); m = int((total%3600)//60); s = total - 60*m - 3600*h
    return f"{h}:{m:02d}:{s:05.2f}>"

def _echo_lines(lines: List[str]):
    for line in lines: stack.stack(f"ECHO {line}")

def _echo_ok(msg: str, nxt: Optional[str]=None):
    for line in str(msg).splitlines(): _echo_lines([f"[SATG] {line}"])
    if nxt: _echo_lines([f"[NEXT] {nxt}"])

def _echo_err(msg: str):
    for line in str(msg).splitlines(): _echo_lines([f"[SATG][ERR] {line}"])

def _fmt_alt_token(fl: int) -> str:
    return "0" if int(fl) <= 0 else f"FL{int(fl)}"

def _sanitize_name(name: str) -> str:
    s = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if not s or not s[0].isalpha(): s = "WPT_" + s
    return s[:32]

# ---------------- Math helpers (bearing/destination) ---------------- #
def _bearing_nm(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing (deg) from (lat1,lon1) to (lat2,lon2)."""
    if hasattr(geo, "qdrdist"):
        qdr, _ = geo.qdrdist(lat1, lon1, lat2, lon2)  # dist in NM
        return qdr
    # Fallback (ASCII-only math)
    import math
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return brg

def _dest_nm(lat, lon, brg_deg, dist_nm):
    """Destination from (lat,lon) along bearing brg_deg for dist_nm nautical miles."""
    if hasattr(geo, "qdrpos"):
        lat2, lon2 = geo.qdrpos(lat, lon, brg_deg, dist_nm)  # deg, deg
        return (lat2, lon2)
    # Fallback (ASCII-only math)
    import math
    R_nm = 3440.065  # Earth radius in nautical miles
    delta = float(dist_nm) / R_nm
    theta = math.radians(brg_deg)
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)

    sin_phi1 = math.sin(phi1)
    cos_phi1 = math.cos(phi1)
    sin_delta = math.sin(delta)
    cos_delta = math.cos(delta)

    sin_phi2 = sin_phi1 * cos_delta + cos_phi1 * sin_delta * math.cos(theta)
    # clamp to [-1, 1] to avoid numerical issues
    sin_phi2 = max(-1.0, min(1.0, sin_phi2))
    phi2 = math.asin(sin_phi2)

    y = math.sin(theta) * sin_delta * cos_phi1
    x = cos_delta - sin_phi1 * math.sin(phi2)
    lam2 = lam1 + math.atan2(y, x)

    lat2 = math.degrees(phi2)
    lon2 = (math.degrees(lam2) + 540.0) % 360.0 - 180.0  # wrap to [-180, 180)
    return (lat2, lon2)

# ---------------- State ---------------- #
class _SATGState:
    def __init__(self):
        # RL data
        self.flights: Dict[str, Dict[str, str]] = {}
        self.base_points: Dict[str, List[dict]] = {}
        self.loaded_ok: bool = False
        # RL jitter (default OFF; params 0 => no noise)
        self.jitter_on: bool = False
        self.j_seed: Optional[int] = None
        # Jitter coverage (percentage of flights to jitter) 
        self.jitter_pct: float = 100.0
        self.jitter_subset: Optional[set] = None  # set of ACIDs to jitter (None => compute on the fly)

        self.dt_max: float = 0.0
        self.dlat_max: float = 0.0
        self.dlon_max: float = 0.0
        self.dfl_max: int = 0
        self.jitter_dist: str = "normal"
        self.nsig: float = 0.0
        # RL autodel
        self.autodel: bool = True
        # Dirs
        self.base_dir: str = ""; self.data_dir: str = ""; self.scn_dir: str = ""
        # GC defaults (apply even if GC_CONF/GC_RANGE never called)
        self.gc_hsep_nm: float = 5.0
        self.gc_vsep_ft: int   = 1000
        self.gc_ranges = {
            "cas1": (220.0, 280.0),
            "cas2": (220.0, 280.0),
            "fl1":  (290,  370),
            "fl2":  (290,  370),
            "brg1": (0.0, 359.0),
            "angle":(90.0, 90.0),
        }
        # Last geometric-conflict aircraft (for quick delete)
        self.gc_last_acids: List[str] = []

        self.proc_wpt_files = []   
        self.proc_proc_files = []  
        self.proc_sid_info: Dict[str, Dict[str, str]] = {}
        self.proc_sid_lookup: Dict[str, str] = {}
        self.proc_sid_schedules: Dict[str, Dict[str, object]] = {}
        self.proc_star_info: Dict[str, Dict[str, str]] = {}
        self.proc_generic_cfg = {"flights": 20, "minsep": 90}
        self.proc_sid_cfg = {"flights": 0, "alt_ft": 3000, "spd_kt": 210}
        self.proc_star_cfg = {"flights": 20, "minsep": 90, "alt_ft": 36000, "mach": 0.79, "use_schedule": False}
        self.proc_sid_rates: Dict[str, float] = {}
        self.proc_star_rates: Dict[str, float] = {}
        self.proc_sid_usage: Dict[str, set] = {}
        self.proc_star_schedules: Dict[str, Dict[str, object]] = {}
        self.proc_destinations_enabled: bool = False
        self.proc_destinations: Dict[str, List[str]] = {}


STATE = _SATGState()
DEFAULT_BASE_DIR = os.path.abspath(os.path.join(os.getcwd(), "satg_data"))

def _init_dirs(base_dir: Optional[str]=None):
    base = os.path.abspath(base_dir or DEFAULT_BASE_DIR)
    data = os.path.join(base, "data"); scns = os.path.join(base, "scenarios")
    if STATE.base_dir == base and os.path.isdir(data) and os.path.isdir(scns):
        STATE.data_dir = data; STATE.scn_dir = scns; return
    if not os.path.isdir(data): os.makedirs(data, exist_ok=True)
    if not os.path.isdir(scns): os.makedirs(scns, exist_ok=True)
    STATE.base_dir = base; STATE.data_dir = data; STATE.scn_dir = scns

_init_dirs()

def _register_sid_proc(path: str):
    """Register SID metadata for the given procedure file path if applicable."""
    path = os.path.abspath(path)
    base = os.path.basename(path)
    match = _SID_FILE_RE.match(base)
    prev = STATE.proc_sid_info.get(path, {})
    if match:
        runway = match.group(1).upper()
        basename_no_ext = os.path.splitext(base)[0]
        key = basename_no_ext.upper()
        info = {
            "path": path,
            "basename": basename_no_ext,
            "runway": runway,
            "icao": prev.get("icao", "")
        }
        STATE.proc_sid_info[path] = info
        STATE.proc_sid_lookup[key] = path
        usage = STATE.proc_sid_usage.setdefault(runway, set())
        usage.add(path)
        STATE.proc_sid_rates.setdefault(runway, DEFAULT_SID_RATE)
        return info
    # Not a SID file: ensure clean-up
    _unregister_sid_proc(path)
    return None


def _unregister_sid_proc(path: str):
    """Remove SID metadata for the given procedure file path."""
    path = os.path.abspath(path)
    info = STATE.proc_sid_info.pop(path, None)
    if info:
        key = info.get("basename", "").upper()
        if STATE.proc_sid_lookup.get(key) == path:
            STATE.proc_sid_lookup.pop(key, None)
        runway = info.get("runway")
        if runway:
            usage = STATE.proc_sid_usage.get(runway)
            if usage and path in usage:
                usage.remove(path)
            if usage and len(usage) == 0:
                STATE.proc_sid_usage.pop(runway, None)
                STATE.proc_sid_rates.pop(runway, None)
                STATE.proc_sid_schedules.pop(runway, None)
    else:
        # Ensure lookup clean even if info not stored
        for key, val in list(STATE.proc_sid_lookup.items()):
            if val == path:
                STATE.proc_sid_lookup.pop(key, None)

def _register_star_proc(path: str):
    """Register STAR metadata for the given procedure file path if applicable."""
    path = os.path.abspath(path)
    base = os.path.basename(path)
    match = _STAR_FILE_RE.match(base)
    if match:
        basename_no_ext = os.path.splitext(base)[0]
        info = STATE.proc_star_info.get(path, {})
        fix, _ = _proc_first_two_fixes(path, set())
        fix_up = fix.upper() if fix else ""
        info.update({
            "path": path,
            "basename": basename_no_ext,
            "fix": fix_up,
        })
        STATE.proc_star_info[path] = info
        if fix_up:
            STATE.proc_star_rates.setdefault(fix_up, DEFAULT_STAR_RATE)
            STATE.proc_star_schedules.setdefault(fix_up, {})
        return info
    _unregister_star_proc(path)
    return None

def _unregister_star_proc(path: str):
    """Remove STAR metadata for the given procedure file path."""
    path = os.path.abspath(path)
    info = STATE.proc_star_info.pop(path, None)
    fix = info.get("fix") if info else ""
    if fix:
        still_used = any(other.get("fix") == fix for other in STATE.proc_star_info.values())
        if not still_used:
            STATE.proc_star_rates.pop(fix, None)
            STATE.proc_star_schedules.pop(fix, None)


# ---------------- RL I/O ---------------- #
_EXPECT_FLIGHTS = {'ECTRL ID','ADEP','ADES','AC Type'}
_EXPECT_POINTS  = {'ECTRL ID','Sequence Number','Time Over','Flight Level','Latitude','Longitude',
                   'Delay Time Over','Dev Latitude','Dev Longitude','Dev Flight Level',
                   'ground_speed','vertical_speed','heading','pitch'}

def _read_csv_auto(path: str) -> Tuple[str, List[dict]]:
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        hdrs = set([h.strip() for h in (reader.fieldnames or [])])
        rows = [{k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in reader]
    if _EXPECT_FLIGHTS.issubset(hdrs): return 'flights', rows
    if _EXPECT_POINTS.issubset(hdrs):  return 'points', rows
    if {'Airspace ID','Min Flight Level','Max Flight Level','Sequence Number','Latitude','Longitude'}.issubset(hdrs):
        return 'FIR', rows
    return '', rows

def _build_base_points(points_rows: List[dict]) -> Dict[str, List[dict]]:
    pts: Dict[str, List[dict]] = {}
    for r in points_rows:
        acid = r['ECTRL ID']
        pts.setdefault(acid, []).append({
            'seq': _to_int(r['Sequence Number']),
            't':   _to_td(r['Time Over']).total_seconds(),
            'fl':  max(0, _to_int(r['Flight Level'])),
            'lat': _to_float(r['Latitude']),
            'lon': _to_float(r['Longitude']),
            'gs':  _to_float(r.get('ground_speed', 0.0)),
            'hdg': _to_float(r.get('heading', float('nan'))),
        })
    for acid in pts:
        pts[acid].sort(key=lambda r: r['seq'])
    return pts

def _draw_noise(rng: random.Random, delta: float, dist: str, nsig: float) -> float:
    if delta <= 0: return 0.0
    if dist == "uniform": return rng.uniform(-delta, +delta)
    x = rng.gauss(0.0, delta)
    lim = nsig * delta
    if lim > 0:
        if x >  lim: x =  lim
        if x < -lim: x = -lim
    return x

def _get_points_for_run() -> Dict[str, List[dict]]:
    if not STATE.base_points: return {}
    pts = {acid: [dict(p) for p in plist] for acid, plist in STATE.base_points.items()}
    if not STATE.jitter_on: return pts
    rng = random.Random(STATE.j_seed) if STATE.j_seed is not None else random.Random()
    dist = STATE.jitter_dist.lower(); nsig = STATE.nsig

    # If a subset hasn't been computed yet and pct < 100, compute a deterministic one now
    if STATE.jitter_on and STATE.jitter_subset is None and float(STATE.jitter_pct) < 100.0:
        acids_all = list(pts.keys())
        k = int(round((float(STATE.jitter_pct) / 100.0) * len(acids_all)))
        rng_sel = random.Random(STATE.j_seed) if STATE.j_seed is not None else random.Random()
        STATE.jitter_subset = set(rng_sel.sample(acids_all, k)) if k > 0 else set()

    def _should_jitter(acid: str) -> bool:
        if not STATE.jitter_on:
            return False
        p = float(STATE.jitter_pct)
        if p <= 0.0:
            return False
        if p >= 100.0:
            return True
        if STATE.jitter_subset is not None:
            return acid in STATE.jitter_subset
        # Fallback: deterministic per-ACID decision using seed + hash
        seed_base = STATE.j_seed if STATE.j_seed is not None else 0
        # Combine seed and per-run salted hash for stable behavior within this session
        rng_local = random.Random((seed_base << 32) ^ (hash(acid) & 0xffffffff))
        return (rng_local.random() * 100.0) < p

    for acid, plist in pts.items():
        plist.sort(key=lambda r: r['seq'])

        # NEW: only jitter this flight if selected
        if not _should_jitter(acid):
            continue

        last_t: Optional[float] = None
        for p in plist:
            p['t']  = max(0.0, p['t'] + _draw_noise(rng, STATE.dt_max,   dist, nsig))
            if last_t is not None:
                p['t'] = max(p['t'], last_t)
            p['lat'] += _draw_noise(rng, STATE.dlat_max, dist, nsig)
            p['lon'] += _draw_noise(rng, STATE.dlon_max, dist, nsig)
            p['fl']   = max(0, int(round(p['fl'] + _draw_noise(rng, float(STATE.dfl_max), dist, nsig))))
            last_t = p['t']

    return pts

def _load_files(files_arg: str) -> Tuple[bool, str]:
    paths: List[str] = []
    arg = (files_arg or "").strip()
    if not arg or arg.upper() == "AUTO":
        if not os.path.isdir(STATE.data_dir): return False, f"Data dir not found: {STATE.data_dir}"
        paths = [os.path.join(STATE.data_dir, fn) for fn in os.listdir(STATE.data_dir) if fn.lower().endswith(".csv")]
    else:
        parts = [p.strip() for p in arg.split(",") if p.strip()]
        if len(parts) == 1 and os.path.isdir(parts[0]):
            paths = [os.path.join(parts[0], fn) for fn in os.listdir(parts[0]) if fn.lower().endswith(".csv")]
        else:
            paths = parts
    if not paths: return False, "No CSV files found."

    flights_rows: List[dict] = []; points_rows: List[dict] = []
    found_flights = found_points = False
    for p in paths:
        kind, rows = _read_csv_auto(p)
        if kind == 'flights': flights_rows.extend(rows); found_flights = True
        elif kind == 'points': points_rows.extend(rows); found_points = True
    if not (found_flights and found_points):
        return False, "Missing required files: need both flights and flights_points (by headers)."

    STATE.base_points = _build_base_points(points_rows)
    fl: Dict[str, Dict[str,str]] = {}
    for r in flights_rows:
        acid = r['ECTRL ID']
        fl[acid] = {'AC Type': r.get('AC Type',''), 'ADEP': r.get('ADEP',''), 'ADES': r.get('ADES','')}
    STATE.flights = fl; STATE.loaded_ok = True
    return True, f"Loaded {len(fl)} flights, {sum(len(v) for v in STATE.base_points.values())} points."

def _scan_existing_acids(path: str) -> set:
    """Return a set of ACIDs already present in an .scn (by CRE lines)."""
    used = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return used
    # Match lines like: ...>CRE ACID,TYPE,lat,lon,...
    for m in re.finditer(r">\s*CRE\s+([A-Za-z0-9_-]+)\s*,", txt):
        used.add(m.group(1))
    return used

def _next_unique_acid(base: str, used: set) -> str:
    """
    If base not in used -> return base.
    If base ends with digits, increment preserving width (e.g., ABC01 -> ABC02).
    Else, append _2, _3, ... until unique.
    """
    if base not in used:
        return base
    m = re.match(r"^(.*?)(\d+)$", base)
    if m:
        root, num = m.group(1), m.group(2)
        width = len(num)
        n = int(num)
        while True:
            n += 1
            cand = f"{root}{str(n).zfill(width)}"
            if cand not in used:
                return cand
    # no trailing digits: use _2, _3, ...
    n = 2
    while True:
        cand = f"{base}_{n}"
        if cand not in used:
            return cand
        n += 1

_TS_RE = re.compile(r"^\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)>")

def _parse_ts(line: str):
    """Return total seconds (float) if line starts with H:MM:SS(.ss)>, else None."""
    m = _TS_RE.match(line)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2))
    s = float(m.group(3))
    return h*3600.0 + mnt*60.0 + s

def _sort_scn_file(path: str):
    """Stable sort all timestamped lines by time; keep header lines at top in original order."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return  # if we cannot read, do nothing

    header = []
    stamped = []
    nonstamped = []

    for idx, ln in enumerate(lines):
        # Keep classic header lines (HOLD/ASAS) as-is at the top
        if ln.strip().startswith("0:") and (">HOLD" in ln or ">ASAS ON" in ln):
            header.append(ln)
            continue
        t = _parse_ts(ln)
        if t is None:
            nonstamped.append((idx, ln))  # comments / blanks / stray lines
        else:
            stamped.append((t, idx, ln))  # stable by (time, original order)

    stamped.sort(key=lambda x: (x[0], x[1]))  # time asc, stable on original index

    out = []
    out.extend(header)
    out.extend([ln for _, _, ln in stamped])
    # Keep any non-timestamp lines at the very end in their original relative order
    out.extend([ln for _, ln in nonstamped])

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(out)
    except Exception:
        pass

# ---------------- RL scenario writing ---------------- #
def _write_rl_scn(out_path: str, append: bool = False):
    mode = "a" if append else "w"
    with open(out_path, mode, encoding="utf-8") as f:
        if not append:
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")
        points = _get_points_for_run()

        # When appending, avoid duplicate callsigns by renaming colliding ACIDs
        used = _scan_existing_acids(out_path) if append else set()
        name_map = {}  # original_acid -> new_acid

        # Compute a deterministic mapping for this batch
        for acid in STATE.flights.keys():
            new_acid = acid
            if new_acid in used or new_acid in name_map.values():
                new_acid = _next_unique_acid(new_acid, used | set(name_map.values()))
            name_map[acid] = new_acid
            used.add(new_acid)

        for acid, meta in STATE.flights.items():
            acid_out = name_map.get(acid, acid)

            if acid not in points or not points[acid]: continue
            segs = points[acid]; r0 = segs[0]; last = segs[-1]
            t0 = timedelta(seconds=r0['t']); stamp0 = _stamp(t0)
            fl0, lat0, lon0 = r0['fl'], r0['lat'], r0['lon']
            cas0 = _gs_to_cas_kt(r0['gs'], fl0)
            hdg0 = int(r0['hdg']) if not math.isnan(r0['hdg']) else 0
            actype = meta.get('AC Type',''); alt_ft0 = int(fl0) * 100

            f.write(f"{stamp0}CRE {acid_out},{actype},{lat0:.6f},{lon0:.6f},{hdg0:03d},{alt_ft0},{cas0:.1f}\n")

            last_is_landing = int(last['fl']) == 0
            trigger_on_last = last_is_landing or STATE.autodel

            pen_wptname = None; last_wptname = None
            if trigger_on_last:
                last_wptname = _sanitize_name(f"{acid_out}_DEST")
                f.write(f"{stamp0}DEFWPT {last_wptname},{last['lat']:.6f},{last['lon']:.6f},FIX\n")
            if last_is_landing and len(segs) >= 2:
                pen = segs[-2]
                pen_wptname = _sanitize_name(f"{acid_out}_APP")
                f.write(f"{stamp0}DEFWPT {pen_wptname},{pen['lat']:.6f},{pen['lon']:.6f},FIX\n")

            for idx, r in enumerate(segs[1:], start=2):
                cas_i = _gs_to_cas_kt(r['gs'], r['fl'])
                is_pen = (idx == len(segs)-1); is_last = (r is last)
                if is_last and trigger_on_last and last_wptname:
                    alt_tok = "0" if int(r['fl']) <= 0 else _fmt_alt_token(r['fl'])
                    f.write(f"{stamp0}ADDWPT {acid_out} {last_wptname},{alt_tok},{cas_i:.1f}\n")
                elif is_pen and last_is_landing and pen_wptname:
                    f.write(f"{stamp0}ADDWPT {acid_out} {pen_wptname},{_fmt_alt_token(r['fl'])},{cas_i:.1f}\n")
                else:
                    f.write(f"{stamp0}ADDWPT {acid_out} {r['lat']:.6f},{r['lon']:.6f},{_fmt_alt_token(r['fl'])},{cas_i:.1f}\n")

            f.write(f"{stamp0}LNAV {acid_out} ON\n")
            f.write(f"{stamp0}VNAV {acid_out} ON\n")
            if last_is_landing and pen_wptname:
                f.write(f"{stamp0}{acid_out} AT {pen_wptname} DO {acid_out} ALT 0\n")
            if trigger_on_last and last_wptname:
                f.write(f"{stamp0}{acid_out} AT {last_wptname} DO DEL {acid_out}\n")
    _sort_scn_file(out_path)

# ---------------- GC utilities ---------------- #
def _parse_range(text: Optional[str], cur: Tuple[float, float]) -> Tuple[float, float]:
    if not text: return cur
    s = str(text).strip()
    if ":" not in s:
        try:
            v = float(s); return (v, v)
        except: return cur
    a, b = s.split(":", 1)
    try:
        lo = float(a); hi = float(b)
        if lo > hi: lo, hi = hi, lo
        return (lo, hi)
    except:
        return cur

def _rand_in(rng: random.Random, lo: float, hi: float) -> float:
    return lo if lo == hi else rng.uniform(lo, hi)

def _gc_sample(seed: Optional[int]):
    rng = random.Random(seed) if seed is not None else random.Random()
    r = STATE.gc_ranges
    cas1 = _rand_in(rng, *r["cas1"]); cas2 = _rand_in(rng, *r["cas2"])
    fl1  = int(round(_rand_in(rng, *r["fl1"]))); fl2 = int(round(_rand_in(rng, *r["fl2"])))
    brg1 = _rand_in(rng, *r["brg1"]) % 360.0
    angle= _rand_in(rng, *r["angle"])
    return rng, cas1, cas2, fl1, fl2, brg1, angle

def _scan_max_sc_index(path: str) -> int:
    """Return the highest SC<number> found in an existing .scn (0 if none)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return 0
    maxn = 0
    # Look for CRE lines: "...>CRE ACID,TYPE,..."
    for m in re.finditer(r">\s*CRE\s+([A-Za-z0-9_-]+)\s*,", txt):
        acid = m.group(1)
        m2 = re.fullmatch(r"SC(\d+)", acid)
        if m2:
            n = int(m2.group(1))
            if n > maxn: maxn = n
    return maxn

# ---------------- GC scenario writer (append-aware) ---------------- #
def _write_gc_scn(out_path: str, *,
                  append: bool,
                  name: str, cpa_lat: float, cpa_lon: float, tcpa: float,
                  typ: str, altmode: str, fl_cpa: Optional[int],
                  acid1: str, acid2: str, ac1: str, ac2: str,
                  seed: Optional[int], angle_in: Optional[float]):
    
    # Sample speeds/levels/initial bearing and default crossing angle
    rng, cas1, cas2, fl1, fl2, brg1, angle = _gc_sample(seed)

    # If user specified crossing angle and type=cross, override the sampled angle
    if angle_in is not None and typ == "cross":
        angle = float(angle_in)

    # Ensure overtake has v2 > v1
    if typ == "overtake" and cas2 <= cas1:
        cas1, cas2 = sorted([cas1, cas2])
        cas2 += max(5.0, 0.05*cas2)  # bump

    # Headings
    if typ == "headon":
        brg2 = (brg1 + 180.0) % 360.0
    elif typ == "cross":
        brg2 = (brg1 + angle) % 360.0
    elif typ == "overtake":
        brg2 = brg1
    else:
        typ = "headon"; brg2 = (brg1 + 180.0) % 360.0

    # Distances (NM) from start to CPA using CAS as GS approx
    d1_nm = (cas1 / 3600.0) * float(tcpa)
    d2_nm = (cas2 / 3600.0) * float(tcpa)

    # Start positions: back-project from CPA along opposite course
    lat1, lon1 = _dest_nm(cpa_lat, cpa_lon, (brg1 + 180.0), d1_nm)
    lat2, lon2 = _dest_nm(cpa_lat, cpa_lon, (brg2 + 180.0), d2_nm)

    # Altitudes
    if altmode.lower() == "level":
        flc = int(fl_cpa) if fl_cpa is not None else int(round((fl1 + fl2)/2))
        fl1_start = flc; fl2_start = flc
        fl_cpa1 = flc; fl_cpa2 = flc
    else:  # altcross
        flc = int(fl_cpa) if fl_cpa is not None else int(round((fl1 + fl2)/2))
        if fl1 == flc: fl1 += 10
        if fl2 == flc: fl2 -= 10
        if not ((fl1 > flc and fl2 < flc) or (fl2 > flc and fl1 < flc)):
            if fl1 <= flc: fl1 = flc + 10
            if fl2 >= flc: fl2 = flc - 10
        fl1_start, fl2_start = fl1, fl2
        fl_cpa1 = flc; fl_cpa2 = flc

    hdg1 = int(round(brg1)) % 360
    hdg2 = int(round(brg2)) % 360

    # Always spawn at time 0
    tzero = timedelta(seconds=0.0)
    stamp0 = _stamp(tzero)

    # Write / append
    mode = "a" if append else "w"
    with open(out_path, mode, encoding="utf-8") as f:
        if not append:
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")

        # AC1
        f.write(f"{stamp0}CRE {acid1},{ac1},{lat1:.6f},{lon1:.6f},{hdg1:03d},{fl1_start*100},{cas1:.1f}\n")
        f.write(f"{stamp0}ADDWPT {acid1} {cpa_lat:.6f},{cpa_lon:.6f},{_fmt_alt_token(fl_cpa1)},{cas1:.1f}\n")
        f.write(f"{stamp0}LNAV {acid1} ON\n{stamp0}VNAV {acid1} ON\n")

        # AC2
        f.write(f"{stamp0}CRE {acid2},{ac2},{lat2:.6f},{lon2:.6f},{hdg2:03d},{fl2_start*100},{cas2:.1f}\n")
        f.write(f"{stamp0}ADDWPT {acid2} {cpa_lat:.6f},{cpa_lon:.6f},{_fmt_alt_token(fl_cpa2)},{cas2:.1f}\n")
        f.write(f"{stamp0}LNAV {acid2} ON\n{stamp0}VNAV {acid2} ON\n")

    # Track all aircraft created in this session (for GC_DEL)
    STATE.gc_last_acids.extend([acid1, acid2])

    # Echo summary (ASCII only)
    r = STATE.gc_ranges
    ang_txt = f"{angle:.1f} deg" if typ == "cross" else "-"
    act = "appended to" if append else "written"
    _echo_ok(
        (f"GC {act}: {out_path}\n"
         f" type={typ} altmode={altmode} CPA=({cpa_lat:.4f},{cpa_lon:.4f}) tcpa={tcpa}s angle={ang_txt}\n"
         f" Minima: HSEP={STATE.gc_hsep_nm} NM, VSEP={STATE.gc_vsep_ft} ft\n"
         f" Ranges: cas1={r['cas1'][0]}:{r['cas1'][1]} kt  cas2={r['cas2'][0]}:{r['cas2'][1]} kt\n"
         f"         fl1={r['fl1'][0]}:{r['fl1'][1]}       fl2={r['fl2'][0]}:{r['fl2'][1]}\n"
         f"         brg1={r['brg1'][0]}:{r['brg1'][1]} deg   angle={r['angle'][0]}:{r['angle'][1]} deg\n"
         f" AC1={acid1} {ac1} brg={hdg1} cas={cas1:.1f} fl0={fl1_start}->CPA{fl_cpa1}\n"
         f" AC2={acid2} {ac2} brg={hdg2} cas={cas2:.1f} fl0={fl2_start}->CPA{fl_cpa2}"),
        nxt="Load: SATG_GC_RUN [SCNNAME]  |  Add more: SATG_GC_CRE name=<sameSCN> ...  |  Clean: SATG_GC_DEL"
    )

# ------- Procedures helpers ----------------- #
def _dms_to_deg(sign, d, m, s):
    deg = float(d) + float(m)/60.0 + float(s)/3600.0
    if sign in ("S","W"): deg = -deg
    return deg

def _parse_defwpt_line(line: str):
    L = line.strip()
    if not L or "DEFWPT" not in L.upper():
        return None
    # Numeric: >DEFWPT NAME lat lon
    m = re.search(r">\s*DEFWPT\s+([A-Za-z0-9_+-]+)\s+([\-0-9\.]+)\s+([\-0-9\.]+)", L, re.IGNORECASE)
    if m:
        return m.group(1).strip(), float(m.group(2)), float(m.group(3))
    # DMS: >DEFWPT NAME Ndd'mm'ss.ss" Edd'mm'ss.ss"
    m2 = re.search(r">\s*DEFWPT\s+([A-Za-z0-9_+-]+)\s+N(\d+)'(\d+)'(\d+(?:\.\d+)?)\"\s+E(\d+)'(\d+)'(\d+(?:\.\d+)?)\"", L, re.IGNORECASE)
    if m2:
        name = m2.group(1).strip()
        lat  = _dms_to_deg("N", m2.group(2), m2.group(3), m2.group(4))
        lon  = _dms_to_deg("E", m2.group(5), m2.group(6), m2.group(7))
        return name, lat, lon
    return None

def _build_fix_db(wpt_files: list[str]) -> dict:
    db = {}
    for p in wpt_files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                for ln in f:
                    hit = _parse_defwpt_line(ln)
                    if hit:
                        name, lat, lon = hit
                        db[name.upper()] = (lat, lon)
        except Exception:
            continue
    return db

def _proc_first_two_fixes(proc_path: str, fix_keys: set) -> tuple[str|None, str|None]:
    """Heuristic: first two tokens that match known DEFWPT names."""
    try:
        with open(proc_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return None, None
    cand = []
    for match in re.finditer(r"ADDWPT\s+([A-Za-z0-9_+-]+)", txt, re.IGNORECASE):
        name = match.group(1).upper()
        if not cand or cand[-1] != name:
            cand.append(name)
        if len(cand) >= 2:
            break
    if len(cand) >= 2: return cand[0], cand[1]
    if len(cand) == 1: return cand[0], None
    return None, None

def _resolve_fix_coord(name: Optional[str],
                       fix_db: Optional[Dict[str, Tuple[float, float]]],
                       ref_lat: Optional[float] = None,
                       ref_lon: Optional[float] = None) -> Optional[Tuple[float, float]]:
    if not name:
        return None
    key = str(name).strip().upper()
    if not key:
        return None
    if fix_db is not None and key in fix_db:
        return fix_db[key]
    navdb = getattr(bs, "navdb", None) if bs else None
    if navdb:
        try:
            wpid_list = getattr(navdb, "wpid", None)
            if wpid_list is not None and len(wpid_list) == 0 and hasattr(navdb, "reset"):
                navdb.reset()
        except Exception:
            pass
        try:
            idx = navdb.getwpidx(key, ref_lat, ref_lon)
            if idx >= 0:
                lat = float(navdb.wplat[idx])
                lon = float(navdb.wplon[idx])
                if fix_db is not None:
                    fix_db[key] = (lat, lon)
                return lat, lon
        except Exception:
            pass
        if len(key) == 4 and hasattr(navdb, "getaptidx"):
            try:
                idx = navdb.getaptidx(key)
                if idx >= 0:
                    lat = float(navdb.aptlat[idx])
                    lon = float(navdb.aptlon[idx])
                    if fix_db is not None:
                        fix_db[key] = (lat, lon)
                    return lat, lon
            except Exception:
                pass
    return None

def _bearing_deg(lat1, lon1, lat2, lon2):
    try:
        from bluesky.tools import geo
        if hasattr(geo, "qdrdist"):
            brg, _ = geo.qdrdist(lat1, lon1, lat2, lon2)
            return float(brg) % 360.0
    except Exception:
        pass
    # fallback
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1)*math.sin(phi2) - math.sin(phi1)*math.cos(phi2)*math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

def _dest_nm(lat, lon, brg_deg, dist_nm):
    try:
        from bluesky.tools import geo
        if hasattr(geo, "qdrpos"):
            lat2, lon2 = geo.qdrpos(lat, lon, brg_deg, dist_nm)
            return float(lat2), float(lon2)
    except Exception:
        pass
    # fallback
    Rnm = 3440.065
    d = float(dist_nm) / Rnm
    th = math.radians(brg_deg)
    phi1 = math.radians(lat); lam1 = math.radians(lon)
    phi2 = math.asin(math.sin(phi1)*math.cos(d) + math.cos(phi1)*math.sin(d)*math.cos(th))
    lam2 = lam1 + math.atan2(math.sin(th)*math.sin(d)*math.cos(phi1), math.cos(d) - math.sin(phi1)*math.sin(phi2))
    return math.degrees(phi2), ((math.degrees(lam2)+540)%360)-180

def _fmt_ts(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600); t -= 3600*h
    m = int(t // 60); t -= 60*m
    s = t
    return f"{h}:{m:02d}:{s:05.2f}>"

def _scn_path(name: str) -> str:
    name = name.strip()
    if not name.lower().endswith(".scn"):
        name += ".scn"
    base = getattr(STATE, "base_dir", "")
    return os.path.join(base, name) if base else name

def _normpath(p: str) -> str:
    """Normalize a user-supplied path. Strips quotes, expands ~, makes absolute.
       If path is relative and a base_dir is set, resolve relative to base_dir.
    """
    if not p:
        return ""
    s = p.strip().strip('"').strip("'")
    # If it's already absolute, keep it; otherwise resolve against base_dir if available
    if os.path.isabs(s):
        return os.path.abspath(os.path.expanduser(s))
    base = getattr(STATE, "base_dir", "")
    root = base if base else os.getcwd()
    return os.path.abspath(os.path.join(root, os.path.expanduser(s)))

def _cmd_path(target: str) -> str:
    """Return an absolute path for BlueSky commands, quoting when spaces are present."""
    abs_path = _normpath(target)
    clean = abs_path.replace("\\", "/")
    return f"\"{clean}\"" if " " in clean else clean

# ---------------- Stack commands (typed for console hints) ---------------- #
@command
def SATG_DIR(base: str=None):
    """SATG_DIR [base]
    Show or set the base directory. Creates <base>/data and <base>/scenarios if missing.
    Example:
      SATG_DIR
      SATG_DIR base=C:/work/satg
    """
    if base:
        _init_dirs(base)
        _echo_ok("Base directory set to: " + STATE.base_dir,
                 nxt="Put CSVs in <base>/data and run: SATG_RL_LOAD [AUTO]")
        return True, ""
    _echo_ok(f"Base: {STATE.base_dir}")
    _echo_ok(f"Data: {STATE.data_dir}")
    _echo_ok(f"Scenarios: {STATE.scn_dir}", nxt="To change: SATG_DIR [base]")
    return True, ""

@command
def SATG_RL_LOAD(files: str="AUTO"):
    """SATG_RL_LOAD [files]
    Load pre-filtered flights + flights_points by headers. Use 'AUTO' to scan <base>/data.
    Examples:
      SATG_RL_LOAD
      SATG_RL_LOAD AUTO
      SATG_RL_LOAD files=C:/data/case1
      SATG_RL_LOAD files=C:/data/flights.csv,C:/data/flights_points.csv
    """
    arg = files.strip()
    if "=" in arg:
        k, v = arg.split("=", 1)
        if k.strip().lower() in ("files",): arg = v.strip().strip('"').strip("'")
    ok, msg = _load_files(arg)
    if ok:
        _echo_ok(msg, nxt="Now: SATG_RL_JITTER [on|off] … (optional), then SATG_RL_RUN [SCNNAME]")
    else:
        _echo_err(msg)
    return ok, ""

@command
def SATG_RL_JITTER(mode: str,
                   dist: str=None,
                   seed: int=None,
                   dt: float=None,
                   dlat: float=None,
                   dlon: float=None,
                   dfl: int=None,
                   nsig: float=None,
                   pct: float=None):
    """SATG_RL_JITTER mode [dist] [seed] [dt] [dlat] [dlon] [dfl] [nsig]
    Synthetic noise applied at MAKE/RUN to baseline points.
      mode: on|off
      dist: uniform|normal     (default keeps last; initial 'normal')
      seed: integer            (repeatable)
      dt:   seconds            (± range for time)
      dlat: degrees            (± range latitude)
      dlon: degrees            (± range longitude)
      dfl:  flight levels      (± range)
      nsig: sigma clamp for normal (±nsig·σ); 0 disables clamp
    Only params you pass are changed; others keep last values (defaults are 0 => no effect).
    """
    m = (mode or "").strip().lower()
    if m not in ("on","off"):
        _echo_err("Usage: SATG_RL_JITTER on|off [dist=uniform|normal] [seed=int] [dt=s] [dlat=deg] [dlon=deg] [dfl=FL] [nsig=sig]")
        return False, ""
    if m == "off":
        STATE.jitter_on = False
        if seed is not None: STATE.j_seed = int(seed)
        _echo_ok("Jitter OFF", nxt="Now: SATG_RL_RUN [SCNNAME]")
        return True, ""
    STATE.jitter_on = True
    if dist is not None:
        d = dist.strip().lower()
        if d not in ("uniform","normal"):
            _echo_err("SATG_RL_JITTER: dist must be 'uniform' or 'normal'"); return False, ""
        STATE.jitter_dist = d
    if seed is not None: STATE.j_seed   = int(seed)
    if dt   is not None: STATE.dt_max   = float(dt)
    if dlat is not None: STATE.dlat_max = float(dlat)
    if dlon is not None: STATE.dlon_max = float(dlon)
    if dfl  is not None: STATE.dfl_max  = int(dfl)
    if nsig is not None: STATE.nsig     = float(nsig)

    # Percentage of flights to jitter (0..100)
    if pct is not None:
        p = max(0.0, min(100.0, float(pct)))
        STATE.jitter_pct = p

    # If we already have flights loaded, precompute a deterministic subset now
    # so selection is stable across runs given the same seed + percentage.
    if STATE.base_points:
        acids = list(STATE.base_points.keys())
        k = int(round((STATE.jitter_pct / 100.0) * len(acids)))
        rng_sel = random.Random(STATE.j_seed) if STATE.j_seed is not None else random.Random()
        STATE.jitter_subset = set(rng_sel.sample(acids, k)) if k > 0 else set()
    else:
        STATE.jitter_subset = None  # compute later once data is loaded

    msg = ("Jitter ON — dist=%s: dt=%s, dlat=%s, dlon=%s, dfl=%s, pct=%.0f%%" %
        (STATE.jitter_dist, STATE.dt_max, STATE.dlat_max, STATE.dlon_max, STATE.dfl_max, STATE.jitter_pct))

    _echo_ok(msg, nxt="Now: SATG_RL_RUN [SCNNAME]")
    return True, ""

@command
def SATG_RL_AUTODEL(mode: str):
    """SATG_RL_AUTODEL mode
      mode: on|off
    Delete aircraft at last waypoint even if final FL>0 (default: ON).
    """
    m = mode.strip().lower()
    if m not in ("on","off"):
        _echo_err("Usage: SATG_RL_AUTODEL on|off"); return False, ""
    STATE.autodel = (m == "on")
    _echo_ok(f"Auto-delete at last waypoint {'ENABLED' if STATE.autodel else 'DISABLED'}",
             nxt="Now: SATG_RL_RUN [SCNNAME]")
    return True, ""

@command
def SATG_RL_MAKE(name: str, overwrite: int = 0):
    """SATG_RL_MAKE name
    Write <base>/scenarios/<name>.scn (scenario starts paused; ASAS ON at 0).
    """
    if not STATE.loaded_ok:
        _echo_err("No data loaded. Run SATG_RL_LOAD first."); return False, ""
    if not os.path.isdir(STATE.scn_dir): os.makedirs(STATE.scn_dir, exist_ok=True)
    nm = name.strip()
    if "=" in nm and nm.lower().startswith("name="): nm = nm.split("=",1)[1].strip()
    out_path = os.path.join(STATE.scn_dir, f"{nm}.scn")
    ow = int(overwrite)
    exists = os.path.isfile(out_path)
    append = (ow == 0) and exists
    _write_rl_scn(out_path, append=append)
    _echo_ok(f"Wrote scenario: {out_path}", nxt="Load it: SATG_RL_RUN [SCNNAME]")
    return True, ""

@command
def SATG_RL_RUN(name: str, overwrite: int = 0):
    """SATG_RL_RUN name
    Write + immediately load <base>/scenarios/<name>.scn (paused; ASAS ON at 0).
    """
    if not STATE.loaded_ok:
        _echo_err("No data loaded. Run SATG_RL_LOAD first."); return False, ""
    if not os.path.isdir(STATE.scn_dir): os.makedirs(STATE.scn_dir, exist_ok=True)
    nm = name.strip()
    if "=" in nm and nm.lower().startswith("name="): nm = nm.split("=",1)[1].strip()
    out_path = os.path.abspath(os.path.join(STATE.scn_dir, f"{nm}.scn"))

    ow = int(overwrite)
    exists = os.path.isfile(out_path)
    append = (ow == 0) and exists
    _write_rl_scn(out_path, append=append)
    stack.stack(f"IC {out_path}")
    _echo_ok(f"Scenario written and loaded: {out_path}",
             nxt="Press Play to run. For geometric conflicts: SATG_GC_HELP")
    return True, ""

# ---------------- GC commands (typed) ---------------- #
@command
def SATG_GC_CONF(hsep_nm: float=5.0, vsep_ft: int=1000):
    """SATG_GC_CONF [hsep_nm] [vsep_ft]
    Set loss-of-separation thresholds used for GC design (informational).
    Defaults apply even if you never call this.
    """
    STATE.gc_hsep_nm = float(hsep_nm)
    STATE.gc_vsep_ft = int(vsep_ft)
    _echo_ok(f"GC minima set: HSEP={STATE.gc_hsep_nm} NM, VSEP={STATE.gc_vsep_ft} ft",
             nxt="Optionally set sampling ranges: SATG_GC_RANGE [cas1=..] [cas2=..] [fl1=..] [fl2=..] [brg1=..] [angle=..]")
    return True, ""

@command
def SATG_GC_RANGE(cas1: str=None, cas2: str=None, fl1: str=None, fl2: str=None,
                  brg1: str=None, angle: str=None):
    """SATG_GC_RANGE [cas1=lo:hi] [cas2=lo:hi] [fl1=lo:hi] [fl2=lo:hi] [brg1=lo:hi] [angle=lo:hi]
    Define sampling ranges for initial CAS/FL/heading and crossing angle.
    Examples:
      SATG_GC_RANGE cas1=230:260 fl1=300:360 brg1=0:359 angle=80:100
    Defaults apply even if you never call this.
    """
    r = STATE.gc_ranges
    if cas1 is not None: r["cas1"] = _parse_range(cas1, r["cas1"])
    if cas2 is not None: r["cas2"] = _parse_range(cas2, r["cas2"])
    if fl1  is not None: r["fl1"]  = tuple(int(x) for x in _parse_range(fl1,  r["fl1"]))
    if fl2  is not None: r["fl2"]  = tuple(int(x) for x in _parse_range(fl2,  r["fl2"]))
    if brg1 is not None: r["brg1"] = _parse_range(brg1, r["brg1"])
    if angle is not None: r["angle"] = _parse_range(angle, r["angle"])
    _echo_ok(
        "GC ranges set:\n"
        f" cas1={r['cas1'][0]}:{r['cas1'][1]} kt   cas2={r['cas2'][0]}:{r['cas2'][1]} kt\n"
        f" fl1={r['fl1'][0]}:{r['fl1'][1]}        fl2={r['fl2'][0]}:{r['fl2'][1]}\n"
        f" brg1={r['brg1'][0]}:{r['brg1'][1]} deg angle={r['angle'][0]}:{r['angle'][1]} deg",
        nxt="Build: SATG_GC_CRE name=<SCN> type=headon|cross|overtake altmode=level|altcross lat=<..> lon=<..> tcpa=<sec> [angle=<deg>]"
    )
    return True, ""

@command
def SATG_GC_CRE(name: str, type: str, altmode: str, lat: float, lon: float, tcpa: float,
                angle: float=None,
                acid1: str="SC1", acid2: str="SC2",
                ac1: str="A320", ac2: str="B738",
                fl_cpa: int=None, seed: int=None, overwrite: str="0"):
    """SATG_GC_CRE name type altmode lat lon tcpa [angle] [acid1] [acid2] [ac1] [ac2] [fl_cpa] [seed] [overwrite (0=append,1=overwrite)]
    Create (or append) a 2-aircraft geometric conflict in <base>/scenarios/<name>.scn.
      type: headon|cross|overtake
      altmode: level|altcross          (altcross forces both to meet at FL_cpa at CPA)
      lat,lon: CPA coordinates (deg)
      tcpa: seconds to CPA from start (t=0)
      angle: crossing angle in degrees (only used when type=cross; overrides random range)
      acid1/2: callsigns (default SC1/SC2; will auto-increment on append if left as default)
      ac1/2: BlueSky AC types (default A320/B738)
      fl_cpa: flight level at CPA (used by altcross; default mid of sampled FLs)
      seed: integer for repeatable sampling
    Behavior:
      - If <name>.scn doesn't exist, it's created with header (HOLD + ASAS ON).
      - If it exists, new conflict lines are appended (no duplicate header).
      - If appending and you keep default callsigns (SC1/SC2), they are auto-bumped to next SC# pair.
    """
    typ = type.strip().lower()
    if typ not in ("headon","cross","overtake"):
        _echo_err("SATG_GC_CRE: type must be headon|cross|overtake"); return False, ""
    am = altmode.strip().lower()
    if am not in ("level","altcross"):
        _echo_err("SATG_GC_CRE: altmode must be level|altcross"); return False, ""

    if not os.path.isdir(STATE.scn_dir): os.makedirs(STATE.scn_dir, exist_ok=True)
    nm = name.strip()
    if "=" in nm and nm.lower().startswith("name="): nm = nm.split("=",1)[1].strip()
    out_path = os.path.join(STATE.scn_dir, f"{nm}.scn")

    # Overwrite handling
    ow_raw = str(overwrite).strip()
    if ow_raw not in ("0","1"):
        _echo_err("SATG_GC_CRE: overwrite must be 0 or 1"); return False, ""
    ow_true = (ow_raw == "1")
    exists = os.path.isfile(out_path)
    append = False if ow_true else exists
    if ow_true and exists:
        try:
            os.remove(out_path)
        except Exception:
            pass

    # Default ACIDs auto-increment when appending
    ac1_final, ac2_final = acid1, acid2
    if append and acid1 == "SC1" and acid2 == "SC2":
        nmax = _scan_max_sc_index(out_path)  # 0 if none found
        ac1_final = f"SC{nmax + 1}"
        ac2_final = f"SC{nmax + 2}"

    _write_gc_scn(out_path, append=append, name=nm,
              cpa_lat=float(lat), cpa_lon=float(lon), tcpa=float(tcpa),
              typ=typ, altmode=am, fl_cpa=fl_cpa,
              acid1=ac1_final, acid2=ac2_final, ac1=ac1, ac2=ac2,
              seed=seed, angle_in=angle)

    return True, ""

@command
def SATG_GC_RUN(name: str):
    """SATG_GC_RUN name
    Load the specified geometric-conflict scenario (paused; ASAS ON at 0 only in file header).
    """
    nm = name.strip()
    if "=" in nm and nm.lower().startswith("name="): nm = nm.split("=",1)[1].strip()
    out_path = os.path.abspath(os.path.join(STATE.scn_dir, f"{nm}.scn"))
    if not os.path.isfile(out_path):
        _echo_err(f"Scenario not found: {out_path}. Run SATG_GC_CRE name={nm} ... first."); return False, ""
    stack.stack(f"IC {out_path}")
    _echo_ok(f"Geometric-conflict scenario loaded: {out_path}",
             nxt="Press Play to run. Add more conflicts with SATG_GC_CRE name=<sameSCN> ...")
    return True, ""

@command
def SATG_GC_DEL():
    """SATG_GC_DEL
    Delete all aircraft created via SATG_GC_CRE during this BlueSky session.
    """
    if not STATE.gc_last_acids:
        _echo_err("No geometric-conflict aircraft recorded to delete."); return False, ""
    for acid in STATE.gc_last_acids:
        stack.stack(f"DEL {acid}")
    _echo_ok(f"Deleted aircraft: {', '.join(STATE.gc_last_acids)}")
    STATE.gc_last_acids = []
    return True, ""

@command
def SATG_RC_CIRCLE(*argv):
    """SATG_RC_CIRCLE name n types center_lat center_lon radius_nm [altmode] [tcpa] [angle] [seed] [fl] [cas] [ac1] [ac2]
    Append n randomized 2-AC conflicts with CPA uniformly inside a circle.
    - Args can be positional (in that order) or key=value (mix ok).
    - All aircraft spawn at t=0; CPA time equals tcpa (no tspan).

    types: CSV from {headon,cross,overtake}
    altmode: level | altcross | mix
    """
    # parse argv
    order = ["name","n","types","center_lat","center_lon","radius_nm",
         "altmode","tcpa","angle","seed","fl","cas","actypes","overwrite"]
    kv, pos = {}, []
    for tok in argv:
        s = str(tok).strip()
        if not s: continue
        if "=" in s:
            k, v = s.split("=",1); kv[k.strip().lower()] = v.strip()
        else:
            pos.append(s)
    for i,p in enumerate(pos):
        if i < len(order) and order[i] not in kv:
            kv[order[i]] = p

    def _get(k, default=None):
        v = kv.get(k, None)
        return default if v is None or v == "" else v
    def _get_int(k):
        return int(float(_get(k)))
    def _get_float(k):
        return float(_get(k))
    def _rng(k, default_tuple):
        v = _get(k, None)
        return _parse_range(v, default_tuple) if v is not None else default_tuple

    # required
    name = str(_get("name","rc_circle")).strip()
    try:
        n = _get_int("n")
        center_lat = _get_float("center_lat")
        center_lon = _get_float("center_lon")
        radius_nm  = _get_float("radius_nm")
    except Exception:
        _echo_err("SATG_RC_CIRCLE: need name, n, center_lat, center_lon, radius_nm."); return False, ""
    if n <= 0 or radius_nm <= 0:
        _echo_err("SATG_RC_CIRCLE: n>0 and radius_nm>0 required"); return False, ""

    types = [t.strip().lower() for t in str(_get("types","headon,cross,overtake")).split(",") if t.strip()]
    if not types or any(t not in {"headon","cross","overtake"} for t in types):
        _echo_err("SATG_RC_CIRCLE: types must be CSV of headon,cross,overtake"); return False, ""

    altmode = str(_get("altmode","level")).lower()
    if altmode not in ("level","altcross","mix"):
        _echo_err("SATG_RC_CIRCLE: altmode must be level|altcross|mix"); return False, ""

    tcpa_rng = _rng("tcpa", (60.0,240.0))
    angle_str = _get("angle", None)
    angle_rng = _parse_range(angle_str, STATE.gc_ranges["angle"]) if angle_str is not None else STATE.gc_ranges["angle"]
    cas_rng   = _rng("cas", STATE.gc_ranges["cas1"])
    fl_rng    = tuple(int(x) for x in _rng("fl", STATE.gc_ranges["fl1"]))

    seed = _get("seed", None)
    rng  = random.Random(int(seed)) if seed is not None else random.Random()

    actypes_str = str(_get("actypes", "")).strip()
    types_list = [t.strip() for t in actypes_str.split(",") if t.strip()]
    if not types_list:
        # Fallbacks for backward compatibility
        ac1_default = str(_get("ac1", "A320"))
        ac2_default = str(_get("ac2", "B738"))
        types_list = [ac1_default, ac2_default]  # if the user provided only ac1/ac2


    # target filepath
    if not os.path.isdir(STATE.scn_dir): os.makedirs(STATE.scn_dir, exist_ok=True)
    nm = name if not name.lower().startswith("name=") else name.split("=",1)[1].strip()
    out_path = os.path.join(STATE.scn_dir, f"{nm}.scn")
    
    ow_raw = str(_get("overwrite","0")).strip()
    if ow_raw not in ("0","1"):
        _echo_err("SATG_RC_CIRCLE: overwrite must be 0 or 1"); return False, ""
    ow_true = (ow_raw == "1")
    exists = os.path.isfile(out_path)
    append = False if ow_true else exists
    if ow_true and exists:
        try:
            os.remove(out_path)
        except Exception:
            pass

    # temp override ranges
    old = dict(STATE.gc_ranges)
    try:
        STATE.gc_ranges["cas1"] = cas_rng
        STATE.gc_ranges["cas2"] = cas_rng
        STATE.gc_ranges["fl1"]  = fl_rng
        STATE.gc_ranges["fl2"]  = fl_rng
        if angle_str is not None:
            STATE.gc_ranges["angle"] = angle_rng

        for _ in range(n):
            typ = rng.choice(types)
            am_i = rng.choice(["level","altcross"]) if altmode == "mix" else altmode
            tcpa_i = _rand_in(rng, tcpa_rng[0], tcpa_rng[1])

            # CPA uniformly by area: r = R*sqrt(u), theta ~ U(0,360)
            r = radius_nm * math.sqrt(rng.random())
            theta = rng.uniform(0.0, 360.0)
            cpa_lat, cpa_lon = _dest_nm(center_lat, center_lon, theta, r)

            angle_i = None
            if typ == "cross":
                lo, hi = STATE.gc_ranges["angle"]
                angle_i = _rand_in(rng, lo, hi)

            if append:
                nmax = _scan_max_sc_index(out_path)
                acid1 = f"SC{nmax+1}"; acid2 = f"SC{nmax+2}"
            else:
                acid1, acid2 = "SC1","SC2"
            
            # Sample AC types uniformly for each aircraft
            ac1 = rng.choice(types_list)
            ac2 = rng.choice(types_list)

            _write_gc_scn(out_path, append=append, name=nm,
                          cpa_lat=cpa_lat, cpa_lon=cpa_lon, tcpa=float(tcpa_i),
                          typ=typ, altmode=am_i, fl_cpa=None,
                          acid1=acid1, acid2=acid2, ac1=ac1, ac2=ac2,
                          seed=None, angle_in=angle_i)
            append = True

        _echo_ok(
            f"RC-CIRCLE wrote {n} conflicts to {out_path}\n"
            f" center=({center_lat:.4f},{center_lon:.4f}) R={radius_nm:.2f}NM altmode={altmode} types={','.join(types)}\n"
            f" tcpa={tcpa_rng[0]:.0f}:{tcpa_rng[1]:.0f}s  spawn_t0=0  seed={seed}\n"
            f" FL={fl_rng[0]}:{fl_rng[1]}  CAS={cas_rng[0]:.0f}:{cas_rng[1]:.0f} kt",
            nxt="Load: SATG_GC_RUN [SCNNAME]"
        )
    finally:
        STATE.gc_ranges = old
    return True, ""

# -------------------- Stack commands procedures -------------------- #
@command
def SATG_PROC_LOAD_WPT(path: str):
    p = _normpath(path.strip('"').strip("'"))
    if not os.path.isfile(p): _echo_err(f"File not found: {p}"); return False, ""
    if p not in STATE.proc_wpt_files: STATE.proc_wpt_files.append(p)
    _echo_ok(f"Loaded waypoint file: {p}"); return True, ""

@command
def SATG_PROC_UNLOAD_WPT(path: str):
    p = _normpath(path.strip('"').strip("'"))
    STATE.proc_wpt_files = [x for x in STATE.proc_wpt_files if x != p]
    _echo_ok(f"Unloaded waypoint file: {p}"); return True, ""

@command
def SATG_PROC_CLEAR_WPT():
    STATE.proc_wpt_files.clear(); _echo_ok("Cleared waypoint files"); return True, ""

@command
def SATG_PROC_LOAD_PROC(path: str):
    p = os.path.abspath(_normpath(path.strip('"').strip("'")))
    if not os.path.isfile(p): _echo_err(f"File not found: {p}"); return False, ""
    if p not in STATE.proc_proc_files: STATE.proc_proc_files.append(p)
    sid_info = _register_sid_proc(p)
    if sid_info:
        _echo_ok(f"Loaded SID procedure file: {p}")
        if not sid_info.get("icao"):
            _echo_lines([
                "[SATG] Provide airport ICAO for SID:",
                f"[SATG]   SATG_PROC_SET_ICAO {sid_info['basename']} <ICAO>"
            ])
    else:
        star_info = _register_star_proc(p)
        if star_info:
            _echo_ok(f"Loaded STAR procedure file: {p}")
        else:
            _echo_ok(f"Loaded procedure file: {p}")
    return True, ""

@command
def SATG_PROC_UNLOAD_PROC(path: str):
    p = os.path.abspath(_normpath(path.strip('"').strip("'")))
    STATE.proc_proc_files = [x for x in STATE.proc_proc_files if x != p]
    _unregister_sid_proc(p)
    _unregister_star_proc(p)
    STATE.proc_destinations.pop(p, None)
    _echo_ok(f"Unloaded procedure file: {p}"); return True, ""

@command
def SATG_PROC_CLEAR_PROC():
    STATE.proc_proc_files.clear()
    STATE.proc_sid_info.clear()
    STATE.proc_sid_lookup.clear()
    STATE.proc_sid_rates.clear()
    STATE.proc_sid_usage.clear()
    STATE.proc_sid_schedules.clear()
    STATE.proc_star_info.clear()
    STATE.proc_star_rates.clear()
    STATE.proc_star_schedules.clear()
    STATE.proc_destinations.clear()
    STATE.proc_destinations_enabled = False
    _echo_ok("Cleared procedure files"); return True, ""

@command
def SATG_PROC_SET_ICAO(proc_id: str, icao: str):
    pid = proc_id.strip().upper()
    if not pid:
        _echo_err("SATG_PROC_SET_ICAO requires a procedure identifier."); return False, ""
    if not icao:
        _echo_err("SATG_PROC_SET_ICAO requires an ICAO code."); return False, ""
    icao_up = icao.strip().upper()
    if len(icao_up) not in (3, 4):
        _echo_err("ICAO codes should be 3 or 4 letters."); return False, ""

    # Try direct lookup by basename
    path = STATE.proc_sid_lookup.get(pid)
    if not path and pid.endswith(".SCN"):
        path = STATE.proc_sid_lookup.get(os.path.splitext(pid)[0])
    # Try to resolve as path
    if not path:
        path_candidate = os.path.abspath(_normpath(proc_id.strip('"').strip("'")))
        path = path_candidate if path_candidate in STATE.proc_sid_info else None

    if not path:
        _echo_err(f"No SID procedure loaded matching '{proc_id}'."); return False, ""

    info = STATE.proc_sid_info.get(path)
    if not info:
        _echo_err(f"Procedure '{proc_id}' is not recognised as a SID."); return False, ""

    info["icao"] = icao_up
    STATE.proc_sid_lookup[info["basename"].upper()] = path  # ensure mapping
    _echo_ok(f"ICAO set for {info['basename']}: {icao_up}")
    return True, ""


@command
def SATG_PROC_CFG_GENERIC(flights: int, minsep: int):
    flights = max(0, int(flights))
    minsep = max(0, int(minsep))
    STATE.proc_generic_cfg["flights"] = flights
    STATE.proc_generic_cfg["minsep"] = minsep
    _echo_ok(f"Generic procedures: flights={flights}, minsep={minsep}s")
    return True, ""


@command
def SATG_PROC_CFG_SID(flights: int, alt_ft: int, spd_kt: float):
    flights = max(0, int(flights))
    alt_ft = max(0, int(alt_ft))
    spd_kt = max(0, int(float(spd_kt)))
    STATE.proc_sid_cfg.update({
        "flights": flights,
        "alt_ft": alt_ft,
        "spd_kt": spd_kt
    })
    _echo_ok(f"SID procedures: flights={flights}, alt={alt_ft}ft, spd={spd_kt}kt")
    return True, ""


@command
def SATG_PROC_CFG_STAR(flights: int, minsep: int, alt_fl: int = 360, mach: float = 0.79, mode: int = 0):
    flights = max(0, int(flights))
    minsep = max(0, int(minsep))
    alt_fl_val = max(0, int(_to_int(alt_fl, 360)))
    alt_ft = alt_fl_val * 100
    mach_val = max(0.0, float(mach))
    use_schedule = bool(int(mode))
    STATE.proc_star_cfg.update({
        "flights": flights,
        "minsep": minsep,
        "alt_ft": alt_ft,
        "mach": mach_val,
        "use_schedule": use_schedule,
    })
    mode_txt = "schedule" if use_schedule else "rate"
    _echo_ok(f"STAR procedures: flights={flights}, mode={mode_txt}, minsep={minsep}s, alt=FL{alt_fl_val}, mach={mach_val:.2f}")
    return True, ""

@command
def SATG_PROC_CFG_STARRATE(proc_id: str, rate: float):
    path = _resolve_proc_path(proc_id)
    if not path:
        _echo_err(f"SATG_PROC_CFG_STARRATE: unknown procedure '{proc_id}'"); return False, ""
    rate_val = max(0.0, float(rate))
    STATE.proc_star_rates[path] = rate_val
    label = os.path.basename(path)
    _echo_ok(f"STAR rate set for {label}: {rate_val:.1f} ac/h")
    return True, ""


@command
def SATG_PROC_CFG_STARSCHED(proc_id: str, start_min: float, end_min: float, *caps):
    path = _resolve_proc_path(proc_id)
    if not path:
        _echo_err(f"SATG_PROC_CFG_STARSCHED: unknown procedure '{proc_id}'"); return False, ""
    caps_int = [max(0, int(round(_to_float(c, 0)))) for c in caps]
    if not caps_int or sum(caps_int) <= 0:
        STATE.proc_star_schedules.pop(path, None)
        label = os.path.basename(path)
        _echo_ok(f"STAR schedule cleared for {label}")
        return True, ""
    slot_minutes = 15.0
    start = max(0.0, float(start_min))
    end = max(0.0, float(end_min))
    if end <= start:
        end = start + slot_minutes * len(caps_int)
    expected_slots = max(1, int(round((end - start) / slot_minutes)))
    if expected_slots != len(caps_int):
        end = start + slot_minutes * len(caps_int)
    STATE.proc_star_schedules[path] = {"start": start, "end": end, "caps": caps_int, "slot": slot_minutes}
    label = os.path.basename(path)
    total_caps = sum(caps_int)
    _echo_ok(f"STAR schedule set for {label}: total={total_caps} flights, window {start:.0f}-{end:.0f} min")
    return True, ""


@command
def SATG_PROC_CLEAR_STARSCHED(proc_id: str = ''):
    if proc_id:
        path = _resolve_proc_path(proc_id)
        if not path:
            _echo_err(f"SATG_PROC_CLEAR_STARSCHED: unknown procedure '{proc_id}'"); return False, ""
        STATE.proc_star_schedules.pop(path, None)
        label = os.path.basename(path)
        _echo_ok(f"Cleared STAR schedule for {label}")
        return True, ""
    STATE.proc_star_schedules.clear()
    _echo_ok('Cleared all STAR schedules')
    return True, ""


@command
def SATG_PROC_CFG_SIDRATE(runway: str, rate: float):
    rw = runway.strip().upper()
    if rw.startswith("RW"):
        rw = rw[2:]
    if not rw:
        _echo_err("SATG_PROC_CFG_SIDRATE requires a runway identifier (e.g., RW18L)."); return False, ""
    rate = max(0.0, float(rate))
    STATE.proc_sid_rates[rw] = rate
    _echo_ok(f"SID runway RW{rw}: rate set to {rate:.1f} ac/h")
    return True, ""


@command
def SATG_PROC_CFG_SIDSCHED(runway: str, start_min: float, end_min: float, *caps):
    rw = runway.strip().upper()
    if rw.startswith("RW"):
        rw = rw[2:]
    if not rw:
        _echo_err("SATG_PROC_CFG_SIDSCHED requires a runway identifier (e.g., RW18L)."); return False, ""

    if not caps:
        STATE.proc_sid_schedules.pop(rw, None)
        _echo_ok(f"SID runway RW{rw}: schedule cleared")
        return True, ""

    start = max(0.0, float(start_min))
    end = max(0.0, float(end_min))
    caps_int = [max(0, int(round(_to_float(c, 0)))) for c in caps]

    slot_minutes = 15.0
    if end <= start:
        end = start + slot_minutes * len(caps_int)
    expected_slots = max(1, int(round((end - start) / slot_minutes)))
    if expected_slots != len(caps_int):
        end = start + slot_minutes * len(caps_int)

    STATE.proc_sid_schedules[rw] = {
        "start": start,
        "end": end,
        "caps": caps_int,
        "slot": slot_minutes,
    }
    total = sum(caps_int)
    _echo_ok(f"SID runway RW{rw}: schedule set ({total} departures planned)")
    return True, ""


@command
def SATG_PROC_CLEAR_SIDSCHED(runway: str = ""):
    rw = runway.strip().upper()
    if not rw:
        STATE.proc_sid_schedules.clear()
        _echo_ok("Cleared all SID schedules")
        return True, ""
    if rw.startswith("RW"):
        rw = rw[2:]
    STATE.proc_sid_schedules.pop(rw, None)
    _echo_ok(f"SID runway RW{rw}: schedule cleared")
    return True, ""


def _resolve_proc_path(proc_id: str) -> Optional[str]:
    pid = proc_id.strip().strip('"').strip("'")
    if not pid:
        return None
    pid_upper = pid.upper()
    norm = _normpath(pid)
    if norm in STATE.proc_proc_files:
        return norm
    if os.path.isabs(pid) and pid in STATE.proc_proc_files:
        return pid
    base = os.path.splitext(pid_upper)[0]
    for path in STATE.proc_proc_files:
        if os.path.splitext(os.path.basename(path))[0].upper() == base:
            return path
    return None


def _normalize_star_fix(proc_id: str) -> str:
    proc_id = str(proc_id).strip()
    if not proc_id:
        return ""
    path = _resolve_proc_path(proc_id)
    if path:
        fix, _ = _proc_first_two_fixes(path, set())
        if fix:
            return fix.upper()
        return os.path.splitext(os.path.basename(path))[0].upper()
    return proc_id.strip().upper()


@command
def SATG_PROC_USE_DEST(flag: int):
    STATE.proc_destinations_enabled = bool(int(flag))
    state_txt = "ON" if STATE.proc_destinations_enabled else "OFF"
    _echo_ok(f"SATG procedure destinations {state_txt}")
    return True, ""


@command
def SATG_PROC_SET_DEST(proc_id: str, *airports: str):
    path = _resolve_proc_path(proc_id)
    if not path:
        _echo_err(f"SATG_PROC_SET_DEST: unknown procedure '{proc_id}'"); return False, ""
    dests = [a.strip().upper() for a in airports if a.strip()]
    if dests:
        STATE.proc_destinations[path] = dests
        _echo_ok(f"Destinations set for {os.path.basename(path)}: {', '.join(dests)}")
    else:
        STATE.proc_destinations.pop(path, None)
        _echo_ok(f"Destinations cleared for {os.path.basename(path)}")
    return True, ""


@command
def SATG_PROC_MAKE(name: str,
                   n: int,
                   radius_nm: float,
                   envelope_deg: float,
                   minsep: int,
                   seed: int = 0,
                   overwrite: int = 0):
    if not STATE.proc_proc_files:
        _echo_err("No procedure files loaded (SATG_PROC_LOAD_PROC)."); return False, ""
    out_path = _scn_path(name)
    ow = int(overwrite)
    exists = os.path.isfile(out_path)
    append = (ow == 0) and exists

    # Write header and IC's (fresh file only)
    if not append:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")
            for w in STATE.proc_wpt_files:
                f.write(f"0:00:00.00>PCALL {_cmd_path(w)}\n")


    procs = [os.path.abspath(p) for p in STATE.proc_proc_files]
    sid_missing = [
        info["basename"] for path, info in STATE.proc_sid_info.items()
        if path in procs and not info.get("icao")
    ]
    if sid_missing:
        _echo_err("SID procedures missing ICAO codes: " + ", ".join(sorted(sid_missing)) +
                  ". Use SATG_PROC_SET_ICAO <SID-NAME> <ICAO>."); return False, ""

    gen_cfg = dict(STATE.proc_generic_cfg)
    sid_cfg = dict(STATE.proc_sid_cfg)
    star_cfg = dict(STATE.proc_star_cfg)

    total_cfg = gen_cfg["flights"] + sid_cfg["flights"] + star_cfg["flights"]
    if total_cfg <= 0:
        gen_cfg["flights"] = int(n)
        gen_cfg["minsep"] = max(0, int(minsep))
        sid_cfg["flights"] = 0
        total_cfg = gen_cfg["flights"]

    sid_paths = [p for p in procs if p in STATE.proc_sid_info]
    star_paths = [p for p in procs if p in STATE.proc_star_info]
    generic_paths = [p for p in procs if p not in STATE.proc_sid_info and p not in STATE.proc_star_info]

    if not sid_paths:
        sid_cfg["flights"] = 0
    if not generic_paths:
        gen_cfg["flights"] = 0
    if not star_paths:
        star_cfg["flights"] = 0

    total_cfg = gen_cfg["flights"] + sid_cfg["flights"] + star_cfg["flights"]
    if total_cfg <= 0:
        base_n = int(n)
        if generic_paths:
            gen_cfg["flights"] = base_n
            gen_cfg["minsep"] = max(0, int(minsep))
        elif sid_paths:
            sid_cfg["flights"] = base_n
        elif star_paths:
            star_cfg["flights"] = base_n
        total_cfg = gen_cfg["flights"] + sid_cfg["flights"] + star_cfg["flights"]

    if total_cfg <= 0:
        _echo_err("No usable procedure flights configured."); return False, ""

    n = total_cfg

    needs_fix_db = (gen_cfg["flights"] > 0) or (star_cfg["flights"] > 0)
    fix_db: Dict[str, Tuple[float, float]] = {}
    navdb_available = bool(bs and getattr(bs, "navdb", None))
    if needs_fix_db:
        fix_db = _build_fix_db(STATE.proc_wpt_files)
        if not fix_db and not navdb_available:
            _echo_err("No waypoints parsed from DEFWPT files and no global nav database available. Load waypoint .scn first."); return False, ""
    fix_keys = set(fix_db.keys())

    rng = random.Random(int(seed)) if int(seed) != 0 else random.Random()
    R = max(0.01, float(radius_nm))
    env = max(1.0, min(180.0, float(envelope_deg)))

    next_generic = {p: 0.0 for p in generic_paths}
    sid_by_runway: Dict[str, List[str]] = {}
    for path in sid_paths:
        info = STATE.proc_sid_info.get(path)
        if not info:
            continue
        rw = info.get("runway", "RW")
        sid_by_runway.setdefault(rw, []).append(path)
    next_sid = {rw: 0.0 for rw in sid_by_runway}
    used = _scan_existing_acids(out_path) if append else set()
    star_minsep = max(0.0, float(star_cfg.get("minsep", 0)))
    star_alt_ft = max(0, int(star_cfg.get("alt_ft", 0)))
    star_spd = max(0.0, float(star_cfg.get("spd_kt", 0)))

    def _weighted_choice(rng_obj: random.Random, items: List[str], weights: List[float]) -> str:
        total = sum(weights)
        if total <= 0.0:
            return rng_obj.choice(items)
        r = rng_obj.random() * total
        upto = 0.0
        for item, w in zip(items, weights):
            upto += w
            if r <= upto:
                return item
        return items[-1]

    with open(out_path, "a", encoding="utf-8") as f:
        flight_idx = 0

        # Generic procedures
        for _ in range(int(gen_cfg["flights"])):
            proc_path = rng.choice(generic_paths)
            proc_name = os.path.splitext(os.path.basename(proc_path))[0]

            t0 = next_generic[proc_path]; next_generic[proc_path] = t0 + gen_cfg["minsep"]
            ts = _fmt_ts(t0)

            base = f"PR{len(used)+1:03d}"
            acid = _next_unique_acid(base, used); used.add(acid)
            flight_idx = len(used)

            f1_name, f2_name = _proc_first_two_fixes(proc_path, fix_keys)
            coord1 = _resolve_fix_coord(f1_name, fix_db)
            if not coord1:
                _echo_err(f"{proc_name}: could not resolve first fix position."); return False, ""
            latA, lonA = coord1
            if f1_name:
                fix_keys.add(f1_name.upper())
            coord2 = _resolve_fix_coord(f2_name, fix_db, latA, lonA) if f2_name else None
            if coord2:
                latB, lonB = coord2
                if f2_name:
                    fix_keys.add(f2_name.upper())
                brg_AB = _bearing_deg(latA, lonA, latB, lonB)
            else:
                brg_AB = 0.0
            brg_BA = (brg_AB + 180.0) % 360.0

            half = env / 2.0
            theta = (brg_BA - half) + rng.random() * (2.0 * half)
            r = R * math.sqrt(rng.random())
            lat0, lon0 = _dest_nm(latA, lonA, theta, r)
            hdg0 = _bearing_deg(lat0, lon0, latA, lonA)

            alt_ft0 = 3000
            cas0 = 180.0
            actype = "A320"

            hdg_cmd = int(round(hdg0)) % 360
            f.write(f"{ts}CRE {acid} {actype} {lat0:.6f} {lon0:.6f} {hdg_cmd:03d} {int(alt_ft0)} {float(cas0):.1f}\n")
            f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
            f.write(f"{ts}LNAV {acid} ON\n")
            f.write(f"{ts}VNAV {acid} ON\n")
            if STATE.proc_destinations_enabled:
                dests = STATE.proc_destinations.get(proc_path)
                if dests:
                    dest_choice = rng.choice(dests)
                    f.write(f"{ts}DEST {acid} {dest_choice}\n")

        # SID procedures
        sid_total_requested = max(0, int(sid_cfg["flights"]))
        sid_remaining = sid_total_requested
        scheduled_events: List[Tuple[float, str, str]] = []
        schedule_runways: set = set()

        for runway, proc_candidates in sid_by_runway.items():
            schedule = STATE.proc_sid_schedules.get(runway)
            if not schedule:
                continue
            caps = list(schedule.get("caps", []))
            if not caps:
                continue
            slot_minutes = float(schedule.get("slot", 15.0)) or 15.0
            start_minutes = float(schedule.get("start", 0.0))
            slot_seconds = slot_minutes * 60.0
            schedule_runways.add(runway)
            for idx, cap in enumerate(caps):
                cap = int(cap)
                if cap <= 0:
                    continue
                slot_start_sec = (start_minutes + idx * slot_minutes) * 60.0
                if cap == 1:
                    times = [slot_start_sec]
                else:
                    step = slot_seconds / cap
                    times = [slot_start_sec + j * step for j in range(cap)]
                for t_sec in times:
                    proc_path = rng.choice(proc_candidates)
                    scheduled_events.append((t_sec, runway, proc_path))

        scheduled_events.sort(key=lambda x: x[0])
        if sid_total_requested > 0 and len(scheduled_events) > sid_total_requested:
            scheduled_events = scheduled_events[:sid_total_requested]
        sid_remaining = max(0, sid_total_requested - len(scheduled_events))

        for t_sec, runway, proc_path in scheduled_events:
            proc_name = os.path.splitext(os.path.basename(proc_path))[0]
            sid_info = STATE.proc_sid_info.get(proc_path, {})
            icao = sid_info.get("icao", "").upper()
            if not icao:
                _echo_err(f"{proc_name}: ICAO code not set. Use SATG_PROC_SET_ICAO {proc_name} <ICAO>."); return False, ""
            rw_tag = f"RW{runway}"
            ts = _fmt_ts(t_sec)
            next_sid[runway] = max(next_sid.get(runway, 0.0), t_sec)

            base = f"PR{len(used)+1:03d}"
            acid = _next_unique_acid(base, used); used.add(acid)
            flight_idx = len(used)

            actype = "A320"
            f.write(f"{ts}CRE {acid} {actype} {icao} {rw_tag}\n")
            f.write(f"{ts}ADDWPT {acid} {icao}/{rw_tag}\n")
            f.write(f"{ts}ADDWPT {acid} TAKEOFF\n")
            f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
            f.write(f"{ts}LNAV {acid} ON\n")
            f.write(f"{ts}VNAV {acid} ON\n")
            f.write(f"{ts}SPD {acid} {int(sid_cfg['spd_kt'])}\n")
            f.write(f"{ts}ALT {acid} {int(sid_cfg['alt_ft'])}\n")
            if STATE.proc_destinations_enabled:
                dests = STATE.proc_destinations.get(proc_path)
                if dests:
                    dest_choice = rng.choice(dests)
                    f.write(f"{ts}DEST {acid} {dest_choice}\n")

        if sid_remaining > 0:
            rate_runways = [rw for rw in sid_by_runway if rw not in schedule_runways]
            if not rate_runways:
                rate_runways = list(sid_by_runway.keys())
            if rate_runways:
                weights = [max(STATE.proc_sid_rates.get(rw, DEFAULT_SID_RATE), 0.0) for rw in rate_runways]
                for _ in range(sid_remaining):
                    runway = _weighted_choice(rng, rate_runways, weights)
                    proc_candidates = sid_by_runway.get(runway)
                    if not proc_candidates:
                        continue
                    proc_path = rng.choice(proc_candidates)
                    proc_name = os.path.splitext(os.path.basename(proc_path))[0]
                    sid_info = STATE.proc_sid_info.get(proc_path, {})
                    icao = sid_info.get("icao", "").upper()
                    if not icao:
                        _echo_err(f"{proc_name}: ICAO code not set. Use SATG_PROC_SET_ICAO {proc_name} <ICAO>."); return False, ""
                    rw_tag = f"RW{runway}"

                    rate = STATE.proc_sid_rates.get(runway, DEFAULT_SID_RATE)
                    if rate <= 0.0:
                        rate = DEFAULT_SID_RATE
                    interval = 3600.0 / rate

                    t0 = next_sid[runway]
                    next_sid[runway] = t0 + interval
                    ts = _fmt_ts(t0)

                    base = f"PR{len(used)+1:03d}"
                    acid = _next_unique_acid(base, used); used.add(acid)
                    flight_idx = len(used)

                    actype = "A320"
                    f.write(f"{ts}CRE {acid} {actype} {icao} {rw_tag}\n")
                    f.write(f"{ts}ADDWPT {acid} {icao}/{rw_tag}\n")
                    f.write(f"{ts}ADDWPT {acid} TAKEOFF\n")
                    f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
                    f.write(f"{ts}LNAV {acid} ON\n")
                    f.write(f"{ts}VNAV {acid} ON\n")
                    f.write(f"{ts}SPD {acid} {int(sid_cfg['spd_kt'])}\n")
                    f.write(f"{ts}ALT {acid} {int(sid_cfg['alt_ft'])}\n")
                    if STATE.proc_destinations_enabled:
                        dests = STATE.proc_destinations.get(proc_path)
                        if dests:
                            dest_choice = rng.choice(dests)
                            f.write(f"{ts}DEST {acid} {dest_choice}\n")

        # STAR procedures
        star_total_requested = max(0, int(star_cfg.get("flights", 0)))
        use_star_schedule = bool(star_cfg.get("use_schedule"))
        star_rate_map = {path: max(0.0, float(STATE.proc_star_rates.get(path, DEFAULT_STAR_RATE))) for path in star_paths}
        star_schedule_data = STATE.proc_star_schedules if use_star_schedule else {}
        fallback_star_minsep = max(1.0, float(star_cfg.get("minsep", 90)))
        star_alt_ft = int(star_cfg.get("alt_ft", 36000))
        star_mach_val = float(star_cfg.get("mach", 0.79))

        def _write_star_entry(t_sec: float, proc_path: str) -> bool:
            nonlocal flight_idx
            proc_name = os.path.splitext(os.path.basename(proc_path))[0]
            f1_name, f2_name = _proc_first_two_fixes(proc_path, fix_keys)
            if not f1_name:
                _echo_err(f"{proc_name}: procedure missing initial waypoint."); return False
            fix_keys.add(f1_name.upper())
            coord1 = _resolve_fix_coord(f1_name, fix_db)
            lat0 = lon0 = None
            if coord1:
                lat0, lon0 = coord1
            coord2 = _resolve_fix_coord(f2_name, fix_db, lat0, lon0) if (f2_name and lat0 is not None and lon0 is not None) else None
            if coord2:
                lat1, lon1 = coord2
                fix_keys.add(f2_name.upper())
                hdg0 = _bearing_deg(lat0, lon0, lat1, lon1)
            else:
                hdg0 = 0.0
            ts = _fmt_ts(t_sec)
            base = f"PR{len(used)+1:03d}"
            acid = _next_unique_acid(base, used); used.add(acid)
            flight_idx = len(used)
            actype = "A320"
            hdg_cmd = int(round(hdg0)) % 360
            spawn_token = f1_name.upper()
            f.write(f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d} {star_alt_ft} {star_mach_val:.2f}\n")
            f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
            f.write(f"{ts}LNAV {acid} ON\n")
            f.write(f"{ts}VNAV {acid} ON\n")
            if STATE.proc_destinations_enabled:
                dests = STATE.proc_destinations.get(proc_path)
                if dests:
                    dest_choice = rng.choice(dests)
                    f.write(f"{ts}DEST {acid} {dest_choice}\n")
            return True

        if star_paths:
            scheduled_star_events: List[Tuple[float, str]] = []
            if use_star_schedule:
                for proc_path, sched in star_schedule_data.items():
                    if proc_path not in star_paths:
                        continue
                    caps = list(sched.get("caps", []))
                    if not caps:
                        continue
                    slot_minutes = float(sched.get("slot", 15.0)) or 15.0
                    start_minutes = float(sched.get("start", 0.0))
                    slot_seconds = slot_minutes * 60.0
                    for idx, cap in enumerate(caps):
                        cap = int(cap)
                        if cap <= 0:
                            continue
                        slot_start_sec = (start_minutes + idx * slot_minutes) * 60.0
                        if cap == 1:
                            times = [slot_start_sec]
                        else:
                            step = slot_seconds / cap
                            times = [slot_start_sec + j * step for j in range(cap)]
                        for t_sec in times:
                            scheduled_star_events.append((t_sec, proc_path))
                scheduled_star_events.sort(key=lambda x: x[0])
                if star_total_requested <= 0:
                    star_total_requested = len(scheduled_star_events)
                if star_total_requested > 0 and len(scheduled_star_events) > star_total_requested:
                    scheduled_star_events = scheduled_star_events[:star_total_requested]
            star_remaining = max(0, star_total_requested - len(scheduled_star_events))
            next_star_time: Dict[str, float] = {path: 0.0 for path in star_paths}
            for t_sec, proc_path in scheduled_star_events:
                if not _write_star_entry(t_sec, proc_path):
                    return False, ""
                rate = star_rate_map.get(proc_path, DEFAULT_STAR_RATE)
                interval = 3600.0 / rate if rate > 0.0 else fallback_star_minsep
                next_star_time[proc_path] = max(next_star_time.get(proc_path, 0.0), t_sec + interval)

            if star_remaining > 0:
                choices = list(star_paths)
                if choices:
                    weights = [max(star_rate_map.get(path, DEFAULT_STAR_RATE), 0.0) for path in choices]
                    total_weight = sum(weights)
                    for _ in range(star_remaining):
                        if total_weight <= 0.0:
                            proc_path = rng.choice(choices)
                        else:
                            proc_path = _weighted_choice(rng, choices, weights)
                        rate = star_rate_map.get(proc_path, DEFAULT_STAR_RATE)
                        interval = 3600.0 / rate if rate > 0.0 else fallback_star_minsep
                        t0 = next_star_time.get(proc_path, 0.0)
                        next_star_time[proc_path] = t0 + interval
                        if not _write_star_entry(t0, proc_path):
                            return False, ""

    _sort_scn_file(out_path)
    _echo_ok(f"Scenario written: {out_path}")
    return True, ""

@command
def SATG_PROC_RUN(name: str):
    out_path = _scn_path(name)
    if not os.path.isfile(out_path):
        _echo_err(f"Scenario not found: {out_path}"); return False, ""
    stack.stack(f"IC {_cmd_path(out_path)}")
    _echo_ok(f"Loaded scenario: {out_path}")
    return True, ""

@command
def SATG_HELP(topic: str = ""):
    """
    Print SATG help in the console. Optional topic filter prints only matching lines.
    Usage:
      SATG_HELP
      SATG_HELP random
      SATG_HELP proc
    """
    lines = [
        "SATG Help",
        "=========",
        "",
        "Random conflicts in a circle:",
        "  SATG_RC_CIRCLE name N types center_lat center_lon radius_nm altmode tcpa fl cas actypes overwrite [angle]",
        "  - types: headon,cross,overtake (comma separated)",
        "  - altmode: level | altcross | mix",
        "  - tcpa: seconds lo:hi, all aircraft spawn at t=0 (tcpa is time to CPA)",
        "  - fl: flight level lo:hi",
        "  - cas: kt lo:hi",
        "  - angle: lo:hi degrees, only for crossing",
        "  - overwrite: 1 overwrite file, 0 append",
        "",
        "Geometric conflicts:",
        "  SATG_GC_CONF HSEP VSEP",
        "  SATG_GC_RANGE fl=lo:hi cas=lo:hi",
        "  SATG_GC_CRE name=<...> typ=<headon,cross,overtake> altmode=<level|altcross|mix> lat=<deg> lon=<deg> tcpa=<s> [angle=<deg>] overwrite=<0|1>",
        "  SATG_GC_RUN name",
        "",
        "Realistic replay:",
        "  SATG_RL_MAKE name overwrite",
        "  SATG_RL_RUN  name overwrite",
        "  Notes: jitter and auto-delete are set via the GUI and applied just-in-time.",
        "",
        "Procedures mode:",
        "  Load waypoint files (DEFWPT) and procedure files (%0) first:",
        "    SATG_PROC_LOAD_WPT path_to_fix_file.scn",
        "    SATG_PROC_LOAD_PROC path_to_proc_file.scn",
        "    SATG_PROC_SET_ICAO SID-XX-NAME ICAO",
        "    SATG_PROC_CFG_GENERIC flights minsep",
        "    SATG_PROC_CFG_SID flights alt_ft spd_kt",
        "    SATG_PROC_CFG_SIDRATE runway rate_per_hour",
        "    SATG_PROC_CFG_STAR flights minsep alt_fl spd_kt mode",
        "    SATG_PROC_CFG_STARRATE proc_name rate_per_hour",
        "    SATG_PROC_CFG_STARSCHED proc_name start_min end_min cap1 cap2 ...",
        "    SATG_PROC_CLEAR_STARSCHED [proc_name]",
        "    SATG_PROC_USE_DEST 0|1",
        "    SATG_PROC_SET_DEST proc_name ICAO1 ICAO2 ...",
        "    SATG_PROC_CFG_SIDSCHED runway start_min end_min cap1 cap2 ...",
        "    SATG_PROC_CLEAR_SIDSCHED [runway]",
        "    SATG_PROC_MAKE name N radius_nm envelope_deg minsep seed overwrite",
        "    SATG_PROC_RUN  name",
        "  Behavior: generic procedures spawn near the first fix inside a sector pointing inbound to it,",
        "            SID-*-*.scn spawn from runway thresholds once mapped to an ICAO and follow configured runway rates or schedules.",
        "            Min separation applies to generic procedures only.",
        "",
        "General:",
        "  - Use the GUI to set a base folder. Scenario files are written there.",
        "  - When appending to an existing scenario, callsigns are auto-renamed to avoid duplicates.",
        "  - Scenario files are sorted by time after writing.",
        "  - If destination assignment is ON, DEST commands are sent with random airports from configured lists.",
    ]

    t = topic.strip().lower()
    if not t:
        return True, "\n".join(lines)

    # Simple filter: print only lines containing the topic
    filtered = [ln for ln in lines if t in ln.lower()]
    if not filtered:
        return True, "No matching help entries."
    return True, "\n".join(filtered)

# ------------------- Plugin init -------------------- #
def init_plugin():
    return {'plugin_name': 'SATG', 'plugin_type': 'sim'}
