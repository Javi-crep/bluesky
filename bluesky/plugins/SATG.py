"""
SATG: Scenario generator (Realistic Replay + Geometric Conflicts)

"""
import os, math, csv, re, random
from datetime import timedelta
from typing import Dict, List, Optional, Tuple
import re

# Import geopandas for polygon sampling
try:
    import geopandas as gpd
    from shapely.geometry import Point, Polygon
    HAS_GEOPANDAS = True
except ImportError:
    HAS_GEOPANDAS = False
    gpd = None

from bluesky import stack
try:
    import bluesky as bs
except Exception:
    bs = None
from bluesky.stack import command
from bluesky.tools import geo, areafilter
from bluesky.tools import misc as _misc
from bluesky.tools.aero import ft, kts, casormach2tas, tas2cas, nm
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

def _sample_point_in_polygon(polygon_name: str, rng: random.Random) -> Optional[Tuple[float, float]]:
    """Sample a random point within the specified polygon using geopandas.
    
    Args:
        polygon_name: Name of the polygon in BlueSky's basic_shapes
        rng: Random number generator for reproducible sampling
        
    Returns:
        (lat, lon) tuple if successful, None if polygon not found or geopandas unavailable
    """
    if not HAS_GEOPANDAS:
        _echo_err("geopandas not available for polygon sampling. Install with: pip install geopandas")
        return None
        
    # Get polygon coordinates from BlueSky's areafilter
    coords = get_polygon_coordinates(polygon_name)
    if not coords:
        _echo_err(f"Polygon '{polygon_name}' not found. Use SATG_POLY_LIST to see available polygons.")
        return None
    
    try:
        # Convert coordinates to Shapely polygon
        # Ensure polygon is closed
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        
        polygon = Polygon([(lon, lat) for lat, lon in coords])  # Shapely uses (x,y) = (lon,lat)
        
        # Create GeoDataFrame
        gdf = gpd.GeoDataFrame([1], geometry=[polygon])
        
        # Sample one point using the random number generator
        # Note: Use numpy RandomState for geopandas compatibility
        import numpy as np
        np_rng = np.random.RandomState(rng.randint(0, 2**31 - 1))
        
        points = gdf.sample_points(1, rng=np_rng)
        
        if len(points) > 0:
            point = points.iloc[0]
            # Extract coordinates (Shapely Point has x=lon, y=lat)
            return (point.y, point.x)  # Return as (lat, lon)
        else:
            _echo_err(f"Failed to sample point in polygon '{polygon_name}'")
            return None
            
    except Exception as e:
        _echo_err(f"Error sampling point in polygon '{polygon_name}': {e}")
        return None

def _notify_gui_polygon_update():
    """Notify that polygons have been updated."""
    print("[SATG] Polygon created - ready for use in GUI")

def _get_polygon_creation_command(polygon_name: str) -> Optional[str]:
    """Get the POLY command to recreate a polygon.
    
    Args:
        polygon_name: Name of the polygon to get the creation command for
        
    Returns:
        POLY command string if polygon exists, None otherwise
    """
    coords = get_polygon_coordinates(polygon_name)
    if not coords:
        return None
        
    # Format coordinates as lat,lon pairs for POLY command
    coord_pairs = []
    for lat, lon in coords:
        coord_pairs.append(f"{lat:.6f},{lon:.6f}")
    
    # Create POLY command: POLY polygon_name lat1,lon1 lat2,lon2 ... 
    return f"POLY {polygon_name} {' '.join(coord_pairs)}"

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
        # Relative conflict default sequence counter
        self.gc_rel_seq: int = 1
        self.gc_ac_types: List[str] = ["A320", "B738", "A350", "B78X"]
        # Cache nav lookups for CPA waypoint resolution
        self.gc_fix_cache: Dict[str, Tuple[float, float]] = {}

        self.proc_wpt_files = []   
        self.proc_proc_files = []  
        self.proc_sid_info: Dict[str, Dict[str, str]] = {}
        self.proc_sid_lookup: Dict[str, str] = {}
        self.proc_sid_schedules: Dict[str, Dict[str, object]] = {}
        self.proc_star_info: Dict[str, Dict[str, str]] = {}
        self.proc_generic_cfg = {"flights": 20, "minsep": 90}
        self.proc_sid_cfg = {"flights": 0, "alt_ft": 3000, "spd_kt": 210}
        self.proc_star_cfg = {
            "flights": 20,
            "minsep": 90,
            "initial_alt_fl": 360,
            "initial_mach": 0.79,
            "final_alt_fl": 100,
            "final_spd": 240,
            "use_schedule": False,
            "rate_basis": "initial",
        }
        self.proc_sid_rates: Dict[str, float] = {}
        self.proc_star_rates: Dict[str, Dict[str, float]] = {"initial": {}, "final": {}}
        self.proc_sid_usage: Dict[str, set] = {}
        self.proc_star_schedules: Dict[str, Dict[str, object]] = {}
        self.proc_star_initial_groups: Dict[str, set] = {}
        self.proc_star_final_groups: Dict[str, set] = {}
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
        fixes = _proc_fix_sequence(path)
        initial_fix = fixes[0] if fixes else ""
        second_fix = fixes[1] if len(fixes) > 1 else None
        final_fix = fixes[-1] if fixes else ""
        penultimate_fix = fixes[-2] if len(fixes) > 1 else None
        initial_fix_up = initial_fix.upper() if initial_fix else ""
        final_fix_up = final_fix.upper() if final_fix else ""
        info.update({
            "path": path,
            "basename": basename_no_ext,
            "fix": initial_fix_up,
            "initial_fix": initial_fix_up,
            "second_fix": second_fix.upper() if second_fix else None,
            "final_fix": final_fix_up,
            "penultimate_fix": penultimate_fix.upper() if penultimate_fix else None,
        })
        STATE.proc_star_info[path] = info
        if initial_fix_up:
            STATE.proc_star_initial_groups.setdefault(initial_fix_up, set()).add(path)
            STATE.proc_star_rates.setdefault("initial", {}).setdefault(initial_fix_up, DEFAULT_STAR_RATE)
        if final_fix_up:
            STATE.proc_star_final_groups.setdefault(final_fix_up, set()).add(path)
            STATE.proc_star_rates.setdefault("final", {}).setdefault(final_fix_up, DEFAULT_STAR_RATE)
        return info
    _unregister_star_proc(path)
    return None

def _unregister_star_proc(path: str):
    """Remove STAR metadata for the given procedure file path."""
    path = os.path.abspath(path)
    info = STATE.proc_star_info.pop(path, None)
    initial_fix = info.get("initial_fix") if info else ""
    final_fix = info.get("final_fix") if info else ""
    if initial_fix:
        group = STATE.proc_star_initial_groups.get(initial_fix)
        if group:
            group.discard(path)
            if not group:
                STATE.proc_star_initial_groups.pop(initial_fix, None)
                STATE.proc_star_rates.setdefault("initial", {}).pop(initial_fix, None)
    if final_fix:
        group = STATE.proc_star_final_groups.get(final_fix)
        if group:
            group.discard(path)
            if not group:
                STATE.proc_star_final_groups.pop(final_fix, None)
                STATE.proc_star_rates.setdefault("final", {}).pop(final_fix, None)
    STATE.proc_star_schedules.pop(path, None)


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
_PR_TOKEN_RE = re.compile(r"\bPR\d+\b")

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

def _renumber_pr_acids(path: str, start_index: int = 0):
    """Renumber PR*** callsigns chronologically so they are sequential."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    mapping: Dict[str, str] = {}
    counter = int(start_index)

    def _map_acid(old: str) -> str:
        nonlocal counter
        if old not in mapping:
            counter += 1
            mapping[old] = f"PR{counter:03d}"
        return mapping[old]

    new_lines: List[str] = []
    cre_re = re.compile(r"(>CRE\s+)(PR\d+)(\b)")

    for line in lines:
        match = cre_re.search(line)
        if match:
            old_acid = match.group(2)
            new_acid = _map_acid(old_acid)
            line = line[:match.start(2)] + new_acid + line[match.end(2):]

        def _replace(match_obj):
            tok = match_obj.group(0)
            return mapping.get(tok, tok)

        if mapping:
            line = _PR_TOKEN_RE.sub(_replace, line)
        new_lines.append(line)

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

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


def _parse_value_range(raw: Optional[str], *, context: str, label: str,
                       allow_negative: bool=False, required: bool=True) -> Optional[Tuple[float, float, bool]]:
    txt = (raw or "").strip()
    if not txt:
        if required:
            _echo_err(f"{context}: {label} is required")
        return None
    if ":" in txt:
        left, right = txt.split(":", 1)
        try:
            lo = float(left.strip()); hi = float(right.strip())
        except Exception:
            _echo_err(f"{context}: {label} range must be numeric")
            return None
        if not allow_negative and (lo < 0.0 or hi < 0.0):
            _echo_err(f"{context}: {label} must be >= 0")
            return None
        if lo > hi:
            lo, hi = hi, lo
        return float(lo), float(hi), True
    try:
        val = float(txt)
    except Exception:
        _echo_err(f"{context}: {label} must be numeric")
        return None
    if not allow_negative and val < 0.0:
        _echo_err(f"{context}: {label} must be >= 0")
        return None
    return float(val), float(val), False


def _format_numeric(val: float, *, as_int: bool=False) -> str:
    if as_int:
        return str(int(round(val)))
    txt = f"{float(val):.6f}".rstrip("0").rstrip(".")
    return txt if txt and txt != "-0" else "0"

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


def _gc_velocity_components(cas_kt: float, heading_deg: float) -> Tuple[float, float]:
    """Return north/east ground-speed components (NM/s) for CAS/heading.

    Follows the same velocity decomposition used by creconfs: speed projected onto
    the local-tangent plane, assuming wind=0 for scenario backtracking.
    """
    spd_nmps = float(cas_kt) / 3600.0  # knots -> NM/s
    rad = math.radians(heading_deg)
    vn = spd_nmps * math.cos(rad)
    ve = spd_nmps * math.sin(rad)
    return vn, ve


def _gc_offset_from_cpa(lat_cpa: float, lon_cpa: float,
                        dn_nm: float, de_nm: float) -> Tuple[float, float]:
    """Translate an offset (north/east in NM) from CPA into lat/lon.

    Mirrors the vector backtracking used in creconfs: we work in the local tangent
    plane, then project the resulting distance/bearing back onto the sphere.
    """
    dist_nm = math.hypot(dn_nm, de_nm)
    if dist_nm <= 1e-9:
        return lat_cpa, lon_cpa
    bearing = (math.degrees(math.atan2(de_nm, dn_nm)) + 360.0) % 360.0
    return _dest_nm(lat_cpa, lon_cpa, bearing, dist_nm)

def _scan_max_sc_index(path: str) -> int:
    """Return the highest GCA<number> found in an existing .scn (0 if none)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return 0
    maxn = 0
    # Look for CRE lines: "...>CRE ACID,TYPE,..."
    for m in re.finditer(r">\s*CRE\s+([A-Za-z0-9_-]+)\s*,", txt):
        acid = m.group(1)
        m2 = re.fullmatch(r"GCA(\d+)", acid)
        if m2:
            n = int(m2.group(1))
            if n > maxn: maxn = n
    return maxn

def _scan_max_gcr_index(path: str) -> int:
    """Return the highest GCR<number> found in an existing .scn (0 if none)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return 0
    maxn = 0
    # Look for CRE lines, SATG_GC_REL commands, and CRECONFS commands
    # Pattern matches:
    # - CRE commands: >CRE GCR123,
    # - SATG_GC_REL: acid=GCR123 or target=GCR123
    # - CRECONFS: CRECONFS,GCR123, (intruder ID in second field)
    pattern = r"(>\s*CRE\s+([A-Za-z0-9_-]+)\s*,|acid=([A-Za-z0-9_-]+)|target=([A-Za-z0-9_-]+)|CRECONFS\s*,\s*([A-Za-z0-9_-]+)\s*,)"
    
    for m in re.finditer(pattern, txt):
        for i in range(2, 6):  # Check groups 2, 3, 4, 5
            if m.group(i):
                acid = m.group(i)
                m2 = re.fullmatch(r"GCR(\d+)", acid)
                if m2:
                    n = int(m2.group(1))
                    if n > maxn: maxn = n
    return maxn

# ---------------- GC scenario writer (append-aware) ---------------- #
def _write_gc_scn(out_path: str, *,
                  append: bool,
                  name: str,
                  cpa_lat: float,
                  cpa_lon: float,
                  tcpa_value: Optional[float],
                  tcpa_range: Optional[Tuple[float, float]],
                  fl_cpa: Optional[int],
                  acid1: str,
                  acid2: str,
                  ac1: str,
                  ac2: str,
                  ac_types: Optional[List[str]] = None,
                  seed: Optional[int] = None,
                  angle_in: Optional[float] = None,
                  angle_range: Optional[Tuple[float, float]] = None,
                  alt_offset_value: Optional[float] = None,
                  alt_offset_range: Optional[Tuple[float, float]] = None,
                  polygon_commands: Optional[List[str]] = None):
    
    # Sample speeds/levels/initial bearing and default crossing angle
    rng, cas1, cas2, fl1, fl2, brg1, angle = _gc_sample(seed)

    choices = [str(t).strip().upper() for t in (ac_types if ac_types else STATE.gc_ac_types) if str(t).strip()]
    if choices:
        ac1 = rng.choice(choices)
        ac2 = rng.choice(choices)

    # Determine encounter parameters possibly overridden by user ranges/values.
    if angle_range is not None:
        angle = _rand_in(rng, float(angle_range[0]), float(angle_range[1]))
    elif angle_in is not None:
        angle = float(angle_in)

    if tcpa_range is not None:
        tcpa = _rand_in(rng, float(tcpa_range[0]), float(tcpa_range[1]))
    else:
        fallback_tcpa = tcpa_value if tcpa_value is not None else 120.0
        tcpa = float(fallback_tcpa)

    # Clamp angle (0=head-on, 180=overtake, intermediate=crossing)
    angle = max(0.0, min(180.0, float(angle)))
    delta_hdg = 180.0 - angle
    brg2 = (brg1 + delta_hdg) % 360.0

    # Identify encounter style for reporting / tuning
    if abs(delta_hdg) < 1e-3:
        encounter = "overtake"
    elif abs(abs(delta_hdg) - 180.0) < 1e-3:
        encounter = "headon"
    else:
        encounter = "cross"

    # Ensure overtakes have v2 > v1 for meaningful closure
    if encounter == "overtake" and cas2 <= cas1:
        cas1, cas2 = sorted([cas1, cas2])
        cas2 += max(5.0, 0.05 * cas2)

    # Use creconfs-style velocity backtracking to derive spawn points
    tcpa_s = float(tcpa)
    vn1, ve1 = _gc_velocity_components(cas1, brg1)
    vn2, ve2 = _gc_velocity_components(cas2, brg2)

    # Compute CPA separation: AC1 at requested CPA, AC2 offset to satisfy HSEP
    hsep_nm = max(0.0, float(STATE.gc_hsep_nm))
    sep_n = 0.0
    sep_e = 0.0
    if hsep_nm > 1e-6:
        vrel_n = vn2 - vn1
        vrel_e = ve2 - ve1
        rel_mag = math.hypot(vrel_n, vrel_e)
        if rel_mag < 1e-6:
            # Degenerate relative velocity (nearly same track/speed): rotate aircraft 1 heading by 90 deg
            rad = math.radians((brg1 + 90.0) % 360.0)
            sep_n = math.cos(rad) * hsep_nm
            sep_e = math.sin(rad) * hsep_nm
        else:
            # r · v_rel = 0 at CPA for minimum distance -> rotate v_rel by 90 deg
            sep_n = (-vrel_e / rel_mag) * hsep_nm
            sep_e = ( vrel_n / rel_mag) * hsep_nm

    cpa1_lat = cpa_lat
    cpa1_lon = cpa_lon
    cpa2_lat, cpa2_lon = _gc_offset_from_cpa(cpa_lat, cpa_lon, sep_n, sep_e)

    # Offsets from each aircraft's CPA expressed in north/east nautical miles
    off1_n = -vn1 * tcpa_s
    off1_e = -ve1 * tcpa_s
    off2_n = -vn2 * tcpa_s
    off2_e = -ve2 * tcpa_s
    lat1, lon1 = _gc_offset_from_cpa(cpa1_lat, cpa1_lon, off1_n, off1_e)
    lat2, lon2 = _gc_offset_from_cpa(cpa2_lat, cpa2_lon, off2_n, off2_e)

    # Altitudes: VSEP defines CPA separation; optional offset applies to initial difference
    base_alt_ft = (int(fl_cpa) * 100.0) if fl_cpa is not None else float(int(round((fl1 + fl2) / 2)) * 100)
    vsep_ft = float(STATE.gc_vsep_ft)
    if alt_offset_range is not None:
        offset_ft = _rand_in(rng, float(alt_offset_range[0]), float(alt_offset_range[1]))
    else:
        offset_ft = float(alt_offset_value) if alt_offset_value is not None else 0.0
    alt1_cpa_ft = base_alt_ft
    alt2_cpa_ft = base_alt_ft + vsep_ft
    alt1_spawn_ft = alt1_cpa_ft
    alt2_spawn_ft = alt2_cpa_ft + offset_ft
    fl_cpa1 = int(round(alt1_cpa_ft / 100.0))
    fl_cpa2 = int(round(alt2_cpa_ft / 100.0))
    fl1_start = int(round(alt1_spawn_ft / 100.0))
    fl2_start = int(round(alt2_spawn_ft / 100.0))

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
            
            # Add polygon creation commands if provided
            if polygon_commands:
                for poly_cmd in polygon_commands:
                    f.write(f"0:00:00.00>{poly_cmd}\n")

        # AC1
        f.write(f"{stamp0}CRE {acid1},{ac1},{lat1:.6f},{lon1:.6f},{hdg1:03d},{int(round(alt1_spawn_ft))},{cas1:.1f}\n")
        f.write(f"{stamp0}ADDWPT {acid1} {cpa1_lat:.6f},{cpa1_lon:.6f},{_fmt_alt_token(fl_cpa1)},{cas1:.1f}\n")
        f.write(f"{stamp0}LNAV {acid1} ON\n{stamp0}VNAV {acid1} ON\n")

        # AC2
        f.write(f"{stamp0}CRE {acid2},{ac2},{lat2:.6f},{lon2:.6f},{hdg2:03d},{int(round(alt2_spawn_ft))},{cas2:.1f}\n")
        f.write(f"{stamp0}ADDWPT {acid2} {cpa2_lat:.6f},{cpa2_lon:.6f},{_fmt_alt_token(fl_cpa2)},{cas2:.1f}\n")
        f.write(f"{stamp0}LNAV {acid2} ON\n{stamp0}VNAV {acid2} ON\n")

    # Track all aircraft created in this session (for GC_DEL)
    STATE.gc_last_acids.extend([acid1, acid2])

    # Echo summary (ASCII only)
    r = STATE.gc_ranges
    ang_txt = f"{angle:.1f} deg"
    if abs(offset_ft) >= 1.0:
        alt_dir = "above" if offset_ft >= 0.0 else "below"
        alt_txt = f"dH0={abs(offset_ft):.0f} ft ({alt_dir})"
    else:
        alt_txt = "dH0=0 ft"
    sep_txt = f"HSEP={hsep_nm:.2f} NM VSEP={vsep_ft:.0f} ft"
    act = "appended to" if append else "written"
    _echo_ok(
    (f"GC {act}: {out_path}\n"
     f" encounter={encounter} CPA1=({cpa1_lat:.4f},{cpa1_lon:.4f}) CPA2=({cpa2_lat:.4f},{cpa2_lon:.4f}) tcpa={tcpa}s angle={ang_txt} {alt_txt} {sep_txt}\n"
         f" Minima: HSEP={STATE.gc_hsep_nm} NM, VSEP={STATE.gc_vsep_ft} ft\n"
         f" Ranges: cas1={r['cas1'][0]}:{r['cas1'][1]} kt  cas2={r['cas2'][0]}:{r['cas2'][1]} kt\n"
         f"         fl1={r['fl1'][0]}:{r['fl1'][1]}       fl2={r['fl2'][0]}:{r['fl2'][1]}\n"
         f"         brg1={r['brg1'][0]}:{r['brg1'][1]} deg   angle={r['angle'][0]}:{r['angle'][1]} deg\n"
         f" AC1={acid1} {ac1} brg={hdg1} cas={cas1:.1f} alt={alt1_spawn_ft:.0f}ft->CPA{fl_cpa1}\n"
         f" AC2={acid2} {ac2} brg={hdg2} cas={cas2:.1f} alt={alt2_spawn_ft:.0f}ft->CPA{fl_cpa2}"),
        nxt="Load: SATG_GC_RUN [SCNNAME]  |  Add more: SATG_GC_CRE name=<sameSCN> ...  |  Clean: SATG_GC_DEL"
    )


# ---------------- Relative conflict helpers ---------------- #
def _gc_rel_parse(argv: Tuple[str, ...]) -> Dict[str, str]:
    """Parse key=value tokens from the command call."""
    params: Dict[str, str] = {}
    for raw in argv:
        token = str(raw).strip()
        if not token:
            continue
        if "=" in token:
            key, val = token.split("=", 1)
            params[key.strip().lower()] = val.strip()
        else:
            key = "mode" if "mode" not in params else token.lower()
            params[key.strip().lower()] = "1"
    return params


def _gc_rel_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _gc_rel_next_acid(explicit: Optional[str] = None) -> str:
    if explicit:
        return str(explicit).strip().upper()
    while True:
        acid = f"GCR{STATE.gc_rel_seq}"
        STATE.gc_rel_seq += 1
        if acid not in STATE.gc_last_acids:
            return acid


def _gc_rel_extract_target(params: Dict[str, str]) -> Optional[Dict[str, float]]:
    """Extract target creation data from parameters."""
    acid = params.get("target_acid") or params.get("target")
    lat = params.get("target_lat")
    lon = params.get("target_lon")
    hdg = params.get("target_hdg")
    alt = params.get("target_alt_ft")
    spd = params.get("target_spd")
    # acid is optional - will be auto-generated if not provided
    if not all([lat, lon, hdg, alt, spd]):
        return None
    return {
        "acid": str(acid).strip().upper() if acid else "",  # Will be set later if empty
        "type": str(params.get("target_type", "A320")),  # This will be processed later
        "lat": float(lat),
        "lon": float(lon),
        "hdg": float(hdg),
        "alt_ft": float(alt),
        "cas_kt": float(spd),
    }


def _gc_rel_format_mach(value: float) -> str:
    txt = f"{float(value):.3f}".rstrip("0").rstrip(".")
    if not txt or txt == "0":
        txt = "0"
    if txt.startswith("."):
        txt = "0" + txt
    return f"M{txt}"


def _gc_rel_normalize_speed_token(token: str) -> Tuple[str, float, bool]:
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("CAS/Mach value is empty.")
    compact = cleaned.replace(" ", "")
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
        return _gc_rel_format_mach(value), value, True
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise ValueError("CAS value must be numeric knots or use the 'M' prefix for Mach.") from exc
    if value <= 0.0:
        raise ValueError("CAS value must be greater than zero.")
    return _format_numeric(value, as_int=False), value, False


def _gc_rel_pick_speed_value(raw: str, rng: random.Random) -> str:
    token = raw.strip()
    if not token:
        raise ValueError("CAS/Mach value is empty.")
    if ":" not in token:
        normalized, _, _ = _gc_rel_normalize_speed_token(token)
        return normalized
    left_txt, right_txt = token.split(":", 1)
    _, left_val, left_is_mach = _gc_rel_normalize_speed_token(left_txt)
    _, right_val, right_is_mach = _gc_rel_normalize_speed_token(right_txt)
    if left_is_mach != right_is_mach:
        raise ValueError("CAS/Mach range must use consistent units (both CAS or both Mach).")
    lo_val = min(left_val, right_val)
    hi_val = max(left_val, right_val)
    value = lo_val if hi_val <= lo_val else _rand_in(rng, lo_val, hi_val)
    if left_is_mach:
        return _gc_rel_format_mach(value)
    return _format_numeric(value, as_int=False)


def _gc_rel_normalize_speed_param(params: Dict[str, str], rng: random.Random) -> bool:
    raw = params.get("spd")
    if raw is None:
        return True
    text = str(raw).strip()
    if not text:
        params.pop("spd", None)
        return True
    try:
        params["spd"] = _gc_rel_pick_speed_value(text, rng)
    except ValueError as exc:
        _echo_err(f"SATG_GC_REL: {exc}")
        return False
    return True


def _gc_rel_pick_actype(raw: Optional[str], rng: random.Random) -> str:
    cleaned = str(raw).strip() if raw is not None else ""
    if cleaned:
        # Strip surrounding quotes if present
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        # Handle both pipe and comma separators (like GC CRE does)
        cleaned = cleaned.replace("|", " ")
        parts = [seg.strip().upper() for seg in re.split(r"[,\s]+", cleaned) if seg.strip()]
        if len(parts) == 1:
            return parts[0]
        if parts:
            return rng.choice(parts)
    defaults = [str(typ).strip().upper() for typ in STATE.gc_ac_types if str(typ).strip()]
    if len(defaults) == 1:
        return defaults[0]
    if defaults:
        return rng.choice(defaults)
    return "A320"


def _gc_rel_cre_block(state: Dict[str, float]) -> List[str]:
    lat = float(state["lat"])
    lon = float(state["lon"])
    hdg = int(round(float(state["hdg"]))) % 360
    alt_ft = float(state["alt_ft"])
    cas_kt = float(state["cas_kt"])
    acid = state["acid"]
    actype = state.get("type", "A320")
    return [
        f"CRE {acid},{actype},{lat:.6f},{lon:.6f},{hdg:03d},{alt_ft:.0f},{cas_kt:.1f}",
        # Remove LNAV/VNAV commands - not needed for conflict scenarios and cause errors
        # f"LNAV {acid} ON",
        # f"VNAV {acid} ON",
    ]


def _gc_rel_write_scn(path: str, *, append: bool, lines: List[str]) -> None:
    stamp0 = _stamp(timedelta(seconds=0.0))
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as f:
        if not append:
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")
        for line in lines:
            f.write(f"{stamp0}{line}\n")

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

def _proc_fix_sequence(proc_path: str) -> List[str]:
    """Return ordered list of waypoint names (uppercased) referenced by ADDWPT commands."""
    try:
        with open(proc_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return []
    seq: List[str] = []
    for match in re.finditer(r"ADDWPT\s+([A-Za-z0-9_+\-/]+)", txt, re.IGNORECASE):
        name = match.group(1).strip().upper()
        if not name:
            continue
        if not seq or seq[-1] != name:
            seq.append(name)
    return seq


def _proc_first_two_fixes(proc_path: str, fix_keys: set) -> tuple[str|None, str|None]:
    """Heuristic: first two tokens that match known DEFWPT names."""
    seq = _proc_fix_sequence(proc_path)
    if not seq:
        return None, None
    if len(seq) == 1:
        return seq[0], None
    return seq[0], seq[1]


def _proc_last_fix(proc_path: str) -> Optional[str]:
    seq = _proc_fix_sequence(proc_path)
    if not seq:
        return None
    return seq[-1]

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
    # Absolute path: honor it directly
    if os.path.isabs(name):
        return os.path.normpath(name)

    base = getattr(STATE, "base_dir", "") or ""
    scn_dir = getattr(STATE, "scn_dir", "") or ""

    # If the provided name already navigates directories, treat it as
    # relative to the base directory (matches longstanding SATG_DIR docs).
    if any(sep in name for sep in ("/", "\\")):
        root = base if base else os.getcwd()
        return os.path.normpath(os.path.join(root, name))

    # Default: place scenarios in the configured scenario folder when
    # available (same behavior as CPA writers). Fallback to base, then CWD.
    root = scn_dir or base or os.getcwd()
    return os.path.normpath(os.path.join(root, name))

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
def SATG_RL_LOAD(*args):
    """SATG_RL_LOAD [files]
    Load pre-filtered flights + flights_points by headers. Use 'AUTO' to scan <base>/data.
    Examples:
      SATG_RL_LOAD
      SATG_RL_LOAD AUTO
      SATG_RL_LOAD files=C:/data/case1
      SATG_RL_LOAD files=C:/data/flights.csv,C:/data/flights_points.csv
      SATG_RL_LOAD C:/data/flights.csv,C:/data/flights_points.csv
    """
    if len(args) == 0:
        files = "AUTO"
    else:
        # Join all arguments back together to reconstruct the original parameter
        files = " ".join(str(arg) for arg in args)
    
    # Process the files parameter
    arg = files.strip()
    if "=" in arg:
        k, v = arg.split("=", 1)
        if k.strip().lower() in ("files",): 
            arg = v.strip().strip('"').strip("'")
    
    # Handle pipe-separated files (from GUI) or comma-separated files
    if "|" in arg:
        # Convert pipe-separated to comma-separated for _load_files
        arg = arg.replace("|", ",")
    
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
def SATG_RL_MAKE(*args):
    """SATG_RL_MAKE name [overwrite] [files]
    Write <base>/scenarios/<name>.scn (scenario starts paused; ASAS ON at 0).
    If files provided, automatically load them first.
    """
    if len(args) < 1:
        _echo_err("Usage: SATG_RL_MAKE name [overwrite] [files]")
        return False, ""
    
    name = str(args[0]).strip()
    overwrite = int(args[1]) if len(args) > 1 else 0
    
    # Handle files parameter (all remaining arguments joined)
    files = ""
    if len(args) > 2:
        files = " ".join(str(arg) for arg in args[2:])
        # Handle pipe-separated files (from GUI)
        if "|" in files:
            files = files.replace("|", ",")
    
    # Auto-load files if provided and not already loaded
    if files and not STATE.loaded_ok:
        result, _ = SATG_RL_LOAD(files)
        if not result:
            return False, ""
    
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
def SATG_RL_RUN(*args):
    """SATG_RL_RUN name [overwrite] [files]
    Write + immediately load <base>/scenarios/<name>.scn (paused; ASAS ON at 0).
    If files provided, automatically load them first.
    """
    if len(args) < 1:
        _echo_err("Usage: SATG_RL_RUN name [overwrite] [files]")
        return False, ""
    
    name = str(args[0]).strip()
    overwrite = int(args[1]) if len(args) > 1 else 0
    
    # Handle files parameter (all remaining arguments joined)
    files = ""
    if len(args) > 2:
        files = " ".join(str(arg) for arg in args[2:])
        # Handle pipe-separated files (from GUI)
        if "|" in files:
            files = files.replace("|", ",")
    
    # Auto-load files if provided and not already loaded
    if files and not STATE.loaded_ok:
        result, _ = SATG_RL_LOAD(files)
        if not result:
            return False, ""
    
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
def SATG_GC_REL(*argv):
    """SATG_GC_REL target=<acid> dpsi=<deg> dcpa=<NM> tlosh=<s>
    Optional:
      acid=<id> actype=<type> dh=<ft> tlosv=<s> spd=<CAS/Mach>
    include_target=1 target_acid=<id> target_type=<type> target_lat=<deg> target_lon=<deg>
               target_hdg=<deg> target_alt_ft=<ft> target_spd=<kt>
    name=<scenario> overwrite=1 seed=<int>
    """

    params = _gc_rel_parse(argv)
    
    # Always use write mode (scenario generation)
    name = params.get("name")
    if not name:
        _echo_err("SATG_GC_REL: name=<scenario> required")
        return False, ""
    path = _scn_path(name)
    overwrite = _gc_rel_bool(params.get("overwrite"))
    file_exists = os.path.isfile(path)
    
    # Reset sequence when overwriting or creating new file
    if (overwrite and file_exists) or not file_exists:
        STATE.gc_rel_seq = 1
        
        # Also reset simulation when overwriting to clear existing aircraft
        if overwrite and file_exists:
            bs.sim.reset()
            STATE.gc_last_acids.clear()

    include_target = _gc_rel_bool(params.get("include_target"))
    
    # Parse seed parameter like CPA does
    seed_txt = params.get("seed", "").strip()
    seed = None
    if seed_txt:
        try:
            seed = int(float(seed_txt))
        except Exception:
            _echo_err("SATG_GC_REL: seed must be an integer")
            return False, ""
    
    rng = random.Random(seed) if seed is not None else random.Random()

    # Handle target ID determination (but don't generate auto IDs yet)
    raw_target = str(params.get("target") or "").strip()
    raw_target_acid = str(params.get("target_acid") or "").strip()
    target_id = None
    if raw_target:
        target_id = raw_target.upper()
    elif raw_target_acid:
        target_id = raw_target_acid.upper()
    elif not include_target:
        _echo_err("SATG_GC_REL: target=<acid> required")
        return False, ""
    # target_id will be generated later if needed

    # Handle intruder ID (also defer auto-generation)
    raw_intruder_acid = params.get("acid")
    intruder_acid = None
    if raw_intruder_acid:
        intruder_acid = str(raw_intruder_acid).strip().upper()
    # intruder_acid will be generated later if needed

    actype_raw = params.get("actype")
    actype = _gc_rel_pick_actype(actype_raw, rng)
    params["actype"] = actype

    if include_target:
        # Target aircraft should sample from the same aircraft types as intruder
        # Both should use STATE.gc_ac_types (set by SATG_GC_TYPES command)
        params["target_type"] = _gc_rel_pick_actype(None, rng)

    target_info: Optional[Dict[str, float]] = None

    def _sample_param(key: str, label: str, *, allow_negative: bool,
                      required: bool, as_int: bool=False,
                      min_value: Optional[float]=None,
                      max_value: Optional[float]=None) -> Tuple[Optional[float], bool]:
        if key not in params:
            if required:
                _echo_err(f"SATG_GC_REL: {label} is required")
            return None, False
        raw = params.get(key)
        if raw is None or str(raw).strip() == "":
            if required:
                _echo_err(f"SATG_GC_REL: {label} is required")
            return None, False
        parsed = _parse_value_range(raw, context="SATG_GC_REL", label=label,
                                    allow_negative=allow_negative, required=True)
        if parsed is None:
            return None, False
        lo, hi, is_range = parsed
        if min_value is not None and (lo < min_value or hi < min_value):
            _echo_err(f"SATG_GC_REL: {label} must be >= {min_value}")
            return None, False
        if max_value is not None and (lo > max_value or hi > max_value):
            _echo_err(f"SATG_GC_REL: {label} must be <= {max_value}")
            return None, False
        value = lo if not is_range else _rand_in(rng, lo, hi)
        params[key] = _format_numeric(value, as_int=as_int)
        return float(value), is_range

    dpsi_val, _ = _sample_param("dpsi", "dpsi", allow_negative=True, required=True,
                                 min_value=-180.0, max_value=180.0)
    if dpsi_val is None:
        return False, ""
    dcpa_val, _ = _sample_param("dcpa", "dcpa", allow_negative=False, required=True,
                                 min_value=0.0)
    if dcpa_val is None or dcpa_val <= 0.0:
        _echo_err("SATG_GC_REL: dcpa must be greater than zero")
        return False, ""
    tlosh_val, _ = _sample_param("tlosh", "tlosh", allow_negative=False, required=True,
                                  min_value=0.0)
    if tlosh_val is None or tlosh_val <= 0.0:
        _echo_err("SATG_GC_REL: tlosh must be greater than zero")
        return False, ""

    dh_val: Optional[float] = None
    dh_is_range = False
    if "dh" in params:
        dh_val, dh_is_range = _sample_param("dh", "dh", allow_negative=True, required=True)
        if dh_val is None:
            return False, ""
        if not dh_is_range and abs(dh_val) < 1e-6:
            params.pop("dh", None)
            dh_val = None
    tlosv_val: Optional[float] = None
    tlosv_is_range = False
    if "tlosv" in params:
        tlosv_val, tlosv_is_range = _sample_param("tlosv", "tlosv", allow_negative=False, required=True,
                                                  min_value=0.0)
        if tlosv_val is None:
            return False, ""
        if not tlosv_is_range and tlosv_val <= 1e-6:
            params.pop("tlosv", None)
            tlosv_val = None

    if include_target:
        lat_val, _ = _sample_param("target_lat", "target_lat", allow_negative=True, required=True)
        if lat_val is None:
            return False, ""
        lon_val, _ = _sample_param("target_lon", "target_lon", allow_negative=True, required=True)
        if lon_val is None:
            return False, ""
        hdg_val, _ = _sample_param("target_hdg", "target_hdg", allow_negative=False, required=True,
                                   min_value=0.0, max_value=360.0)
        if hdg_val is None:
            return False, ""
        # Normalize heading into [0, 360)
        hdg_val = hdg_val % 360.0
        params["target_hdg"] = _format_numeric(hdg_val, as_int=False)
        alt_val, _ = _sample_param("target_alt_ft", "target_alt_ft", allow_negative=False,
                                   required=True, as_int=True, min_value=0.0)
        if alt_val is None:
            return False, ""
        spd_val, _ = _sample_param("target_spd", "target_spd", allow_negative=False,
                                   required=True, min_value=0.0)
        if spd_val is None:
            return False, ""
        target_info = _gc_rel_extract_target(params)
        if target_info is None:
            _echo_err("SATG_GC_REL: include_target=1 requires target_acid, target_lat, target_lon, target_hdg, target_alt_ft, target_spd")
            return False, ""
        # Note: target_info["acid"] will be set later after ID generation
        # Process target type through the aircraft type picker to handle quoted comma-separated values
        target_info["type"] = _gc_rel_pick_actype(target_info["type"], rng)

    if target_info:
        params.setdefault("target_acid", target_id)

    if not _gc_rel_normalize_speed_param(params, rng):
        return False, ""

    # Generate scenario file
    # name and path already validated earlier
    file_exists = os.path.isfile(path)
    append = file_exists and not overwrite
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    if overwrite and file_exists:
        try:
            os.remove(path)
            append = False
        except Exception as exc:
            _echo_err(f"SATG_GC_REL: failed to remove existing scenario {path}: {exc}")
            append = False
    
    # Smart numbering: when appending to existing scenario, adjust sequence first
    if append:
        max_existing = _scan_max_gcr_index(path)
        if max_existing > 0:
            # Adjust our sequence to continue from where the file left off
            STATE.gc_rel_seq = max(STATE.gc_rel_seq, max_existing + 1)
    
    # Now generate IDs (either auto-generated or explicit)
    if target_id is None:
        target_id = _gc_rel_next_acid()
    if intruder_acid is None:
        intruder_acid = _gc_rel_next_acid()
    
    # Update params with final IDs
    params["target"] = target_id
    params["acid"] = intruder_acid
    if include_target:
        params["target_acid"] = target_id
        # IMPORTANT: Now update target_info with the final generated target_id
        if target_info is not None:
            target_info["acid"] = target_id
    lines: List[str] = []
    if include_target and target_info is not None:
        lines.extend(_gc_rel_cre_block(target_info))
    
    # Generate CRECONFS command instead of SATG_GC_REL command
    # CRECONFS syntax: id, type, targetid, dpsi, cpa, tlos_hor, dH, tlos_ver, spd
    creconfs_parts = [
        "CRECONFS",
        intruder_acid,
        actype,
        target_id,
        params["dpsi"],
        params["dcpa"], 
        params["tlosh"]
    ]
    
    # Add optional parameters
    dh_val = params.get("dh")
    if dh_val not in (None, ""):
        creconfs_parts.append(str(dh_val))
    else:
        creconfs_parts.append("")  # Empty placeholder for dH
        
    tlosv_val = params.get("tlosv") 
    if tlosv_val not in (None, ""):
        creconfs_parts.append(str(tlosv_val))
    else:
        creconfs_parts.append("")  # Empty placeholder for tlos_ver
        
    spd_val = params.get("spd")
    if spd_val not in (None, ""):
        creconfs_parts.append(str(spd_val))
    
    # Join with commas as CRECONFS expects comma-separated parameters
    lines.append(",".join(creconfs_parts))
    _gc_rel_write_scn(path, append=append, lines=lines)
    act = "appended to" if append else "written to"
    _echo_ok(
        f"GC relative scenario {act} {path}",
        nxt="Load with SATG_GC_RUN <name>"
    )

    return True, ""


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
def SATG_GC_TYPES(*types):
    """SATG_GC_TYPES [TYPE1] [TYPE2] ...
    Set the candidate aircraft types used for CPA scenario generation.
    Without arguments, resets to the default list.
    """
    cleaned: List[str] = []
    for tok in types:
        s = str(tok).strip().upper()
        if not s:
            continue
        for part in re.split(r"[,\s]+", s):
            part = part.strip().upper()
            if part:
                cleaned.append(part)
    if not cleaned:
        STATE.gc_ac_types = ["A320", "B738", "A350", "B78X"]
        _echo_ok("GC aircraft types reset to defaults",
                 nxt="Set ranges with SATG_GC_RANGE or build scenarios with SATG_GC_CRE")
    else:
        dedup: List[str] = []
        for typ in cleaned:
            if typ and typ not in dedup:
                dedup.append(typ)
        STATE.gc_ac_types = dedup
        _echo_ok("GC aircraft types set: " + ", ".join(STATE.gc_ac_types),
                 nxt="Run SATG_GC_CRE to write a scenario")
    return True, ""

@command
def SATG_GC_RANGE(cas1: str=None, cas2: str=None, fl1: str=None, fl2: str=None,
                  brg1: str=None, angle: str=None):
    """Configure sampling ranges for geometric conflicts.

    Parameters mirror the SATG_GC_CRE inputs:
      cas1/cas2 -> CAS range [kt] for aircraft 1/2
      fl1/fl2   -> Flight level range for aircraft 1/2
      brg1      -> Initial bearing range for aircraft 1 (deg)
      angle     -> CPA angle range (0=head-on, 180=overtake)
    """
    r = STATE.gc_ranges
    if cas1 is not None:
        r["cas1"] = _parse_range(cas1, r["cas1"])
    if cas2 is not None:
        r["cas2"] = _parse_range(cas2, r["cas2"])
    if fl1 is not None:
        r["fl1"] = tuple(int(x) for x in _parse_range(fl1, r["fl1"]))
    if fl2 is not None:
        r["fl2"] = tuple(int(x) for x in _parse_range(fl2, r["fl2"]))
    if brg1 is not None:
        r["brg1"] = _parse_range(brg1, r["brg1"])
    if angle is not None:
        r["angle"] = _parse_range(angle, r["angle"])
    _echo_ok(
        "GC ranges set:\n"
        f" cas1={r['cas1'][0]}:{r['cas1'][1]} kt   cas2={r['cas2'][0]}:{r['cas2'][1]} kt\n"
        f" fl1={r['fl1'][0]}:{r['fl1'][1]}        fl2={r['fl2'][0]}:{r['fl2'][1]}\n"
        f" brg1={r['brg1'][0]}:{r['brg1'][1]} deg angle={r['angle'][0]}:{r['angle'][1]} deg",
    nxt="Build: SATG_GC_CRE name=<SCN> (lat=<..> lon=<..> | wp=<ident>) tcpa=<sec> angle=<deg> [dh=<ft>]"
    )
    return True, ""

@command
def SATG_GC_CRE(*argv):
    """SATG_GC_CRE name=<scn> (lat=<deg> lon=<deg> | wp=<ident>) tcpa=<s> [angle=<deg>] [dh=<ft>]
    Optional: acid1, acid2, ac1, ac2, actypes, fl_cpa, seed, overwrite.

    Legacy positional arguments (type/head-on, altmode) are still accepted but ignored.
    """

    order = [
        "name", "type", "altmode", "lat", "lon", "tcpa", "angle",
        "acid1", "acid2", "ac1", "ac2", "fl_cpa", "seed", "overwrite", "actypes"
    ]
    kv: Dict[str, str] = {}
    pos_idx = 0
    for tok in argv:
        s = str(tok).strip()
        if not s:
            continue
        if "=" in s:
            key, val = s.split("=", 1)
            kv.setdefault(key.strip().lower(), val.strip())
        else:
            if pos_idx < len(order):
                kv.setdefault(order[pos_idx], s)
            pos_idx += 1

    name = kv.get("name")
    lat_txt = kv.get("lat")
    lon_txt = kv.get("lon")
    wp_txt = kv.get("wp") or kv.get("wpt") or kv.get("fix") or kv.get("waypoint")
    tcpa_txt = kv.get("tcpa")
    if not (name and tcpa_txt and ((lat_txt and lon_txt) or wp_txt)):
        _echo_err("SATG_GC_CRE: provide tcpa and either lat/lon or wp=<ident>")
        return False, ""

    nm = name.strip()
    if "=" in nm and nm.lower().startswith("name="):
        nm = nm.split("=", 1)[1].strip()

    parsed_tcpa = _parse_value_range(tcpa_txt, context="SATG_GC_CRE", label="tcpa")
    if parsed_tcpa is None:
        return False, ""
    tcpa_lo, tcpa_hi, tcpa_is_range = parsed_tcpa
    if tcpa_lo <= 0.0 or tcpa_hi <= 0.0:
        _echo_err("SATG_GC_CRE: tcpa must be greater than zero")
        return False, ""
    tcpa_value = tcpa_lo
    tcpa_range = (tcpa_lo, tcpa_hi) if tcpa_is_range or tcpa_lo != tcpa_hi else None

    lat = lon = None
    if lat_txt and lon_txt:
        try:
            lat = float(lat_txt)
            lon = float(lon_txt)
        except Exception:
            _echo_err("SATG_GC_CRE: lat/lon must be numeric degrees")
            return False, ""
    else:
        wp_key = (wp_txt or "").strip()
        coord = _resolve_fix_coord(wp_key, STATE.gc_fix_cache)
        if coord is None:
            label = wp_key.upper() if wp_key else "(blank)"
            _echo_err(f"SATG_GC_CRE: waypoint '{label}' not found")
            return False, ""
        lat, lon = coord

    angle_txt = kv.get("angle")
    angle_value: Optional[float] = None
    angle_range: Optional[Tuple[float, float]] = None
    if angle_txt is not None:
        parsed_angle = _parse_value_range(angle_txt, context="SATG_GC_CRE", label="angle")
        if parsed_angle is None:
            return False, ""
        ang_lo, ang_hi, ang_is_range = parsed_angle
        if ang_lo < 0.0 or ang_hi > 180.0:
            _echo_err("SATG_GC_CRE: angle must stay within 0-180 degrees")
            return False, ""
        if ang_is_range or ang_lo != ang_hi:
            angle_range = (ang_lo, ang_hi)
        else:
            angle_value = ang_lo
    else:
        typ = (kv.get("type") or "").strip().lower()
        if typ == "headon":
            angle_value = 0.0
        elif typ == "overtake":
            angle_value = 180.0
        elif typ == "cross":
            angle_value = 90.0

    dh_txt = kv.get("dh") or kv.get("dalt") or kv.get("alt_offset")
    alt_offset_value: Optional[float] = None
    alt_offset_range: Optional[Tuple[float, float]] = None
    if dh_txt is not None:
        parsed_dh = _parse_value_range(dh_txt, context="SATG_GC_CRE", label="alt_offset", allow_negative=True)
        if parsed_dh is None:
            return False, ""
        dh_lo, dh_hi, dh_is_range = parsed_dh
        if dh_is_range or dh_lo != dh_hi:
            alt_offset_range = (dh_lo, dh_hi)
        else:
            alt_offset_value = dh_lo

    actypes_txt = kv.get("actypes") or kv.get("types")
    ac_types: Optional[List[str]] = None
    if actypes_txt:
        cleaned = actypes_txt.strip().strip('"').strip("'")
        cleaned = cleaned.replace("|", " ")
        raw = [p.strip().upper() for p in re.split(r"[,\s]+", cleaned) if p.strip()]
        if raw:
            seen: List[str] = []
            for typ in raw:
                if typ not in seen:
                    seen.append(typ)
            ac_types = seen

    ac1 = kv.get("ac1", "A320")
    ac2 = kv.get("ac2", "B738")
    ac_types: Optional[List[str]] = None
    actypes_txt = kv.get("actypes") or kv.get("types")
    if actypes_txt:
        raw_parts = [p.strip().upper() for p in re.split(r"[\,\s]+", actypes_txt) if p.strip()]
        if raw_parts:
            ac_types = raw_parts
    acid1 = kv.get("acid1", "GCA1").upper()
    acid2 = kv.get("acid2", "GCA2").upper()

    fl_cpa_txt = kv.get("fl_cpa")
    fl_cpa = None
    if fl_cpa_txt:
        try:
            fl_cpa = int(float(fl_cpa_txt))
        except Exception:
            _echo_err("SATG_GC_CRE: fl_cpa must be an integer flight level")
            return False, ""

    seed_txt = kv.get("seed")
    seed = None
    if seed_txt:
        try:
            seed = int(float(seed_txt))
        except Exception:
            _echo_err("SATG_GC_CRE: seed must be an integer")
            return False, ""

    ow_txt = kv.get("overwrite", "0").strip()
    if ow_txt not in ("0", "1"):
        _echo_err("SATG_GC_CRE: overwrite must be 0 or 1")
        return False, ""
    overwrite = ow_txt == "1"

    if not os.path.isdir(STATE.scn_dir):
        os.makedirs(STATE.scn_dir, exist_ok=True)
    out_path = os.path.join(STATE.scn_dir, f"{nm}.scn")

    append = os.path.isfile(out_path) and not overwrite
    if overwrite and os.path.isfile(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass

    if append and acid1 == "GCA1" and acid2 == "GCA2":
        nmax = _scan_max_sc_index(out_path)
        acid1 = f"GCA{nmax + 1}"
        acid2 = f"GCA{nmax + 2}"

    _write_gc_scn(
        out_path,
        append=append,
        name=nm,
        cpa_lat=lat,
        cpa_lon=lon,
        tcpa_value=tcpa_value,
        tcpa_range=tcpa_range,
        fl_cpa=fl_cpa,
        acid1=acid1,
        acid2=acid2,
        ac1=ac1,
        ac2=ac2,
        ac_types=ac_types,
        seed=seed,
        angle_in=angle_value,
        angle_range=angle_range,
        alt_offset_value=alt_offset_value,
        alt_offset_range=alt_offset_range,
        polygon_commands=None,
    )

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


def _write_gc_rel_scn(out_path: str, *,
                      append: bool,
                      name: str,
                      target_data: Dict,
                      conflict_params: Dict,
                      intruder_types: List[str],
                      seed: Optional[int] = None):
    """Write relative geometric conflict scenario using CRECONFS approach
    
    target_data: Dict with target aircraft parameters (lat, lon, hdg, alt_ft, spd, acid, ac_type)
    conflict_params: Dict with dpsi, dcpa, tlosh, dh, tlosv, spd values/ranges, intruder_acid, intruder_ac_type
    """
    rng = random.Random(seed) if seed is not None else random.Random()
    
    # Use pre-generated intruder aircraft ID and type
    intruder_acid = conflict_params.get("intruder_acid", _gc_rel_next_acid())
    intruder_ac_type = conflict_params.get("intruder_ac_type", rng.choice(intruder_types) if intruder_types else "A320")
    
    # Sample intruder speed if provided as range
    intruder_spd = conflict_params.get("spd")
    if isinstance(intruder_spd, tuple):
        intruder_spd = _rand_in(rng, float(intruder_spd[0]), float(intruder_spd[1]))
    elif intruder_spd is None:
        intruder_spd = _rand_in(rng, 200.0, 300.0)  # Default speed range
    else:
        intruder_spd = float(intruder_spd)
    
    # Create scenario content
    content_lines = []
    stamp0 = _stamp(timedelta(seconds=0.0))  # Get the 0:00:00.00> timestamp
    
    if not append:
        content_lines.append(f"# Random Conflicts Scenario: {name}")
        content_lines.append(f"# Generated by SATG_RC_CIRCLE in relative mode")
        content_lines.append("")
        content_lines.append("0:00:00.00>HOLD")
        content_lines.append("0:00:00.00>ASAS ON")
    
    # Add target aircraft if needed
    if target_data.get("create_target", False):
        target_acid = target_data["acid"]
        target_lat = target_data["lat"]
        target_lon = target_data["lon"] 
        target_hdg = target_data["hdg"]
        target_alt_ft = target_data["alt_ft"]
        target_spd = target_data["spd"]
        target_ac_type = target_data["ac_type"]
        
        # Use decimal degrees format for BlueSky CRE command compatibility
        content_lines.append(f"{stamp0}CRE {target_acid},{target_ac_type},{target_lat:.6f},{target_lon:.6f},{target_hdg},{target_alt_ft},{target_spd}")
    
    # Build CRECONFS command for intruder
    target_acid = target_data["acid"]
    dpsi = conflict_params["dpsi"]
    dcpa = conflict_params["dcpa"] 
    tlosh = conflict_params["tlosh"]
    dh = conflict_params.get("dh", 0)
    tlosv = conflict_params.get("tlosv", 0)
    
    # Handle ranges for conflict parameters
    if isinstance(dpsi, tuple):
        dpsi = _rand_in(rng, float(dpsi[0]), float(dpsi[1]))
    if isinstance(dcpa, tuple):
        dcpa = _rand_in(rng, float(dcpa[0]), float(dcpa[1]))
    if isinstance(tlosh, tuple):
        tlosh = _rand_in(rng, float(tlosh[0]), float(tlosh[1]))
    if isinstance(dh, tuple):
        dh = _rand_in(rng, float(dh[0]), float(dh[1]))
    if isinstance(tlosv, tuple):
        tlosv = _rand_in(rng, float(tlosv[0]), float(tlosv[1]))
    
    # Generate CRECONFS command with correct comma-separated format
    # CRECONFS syntax: id, type, targetid, dpsi, cpa, tlos_hor, dH, tlos_ver, spd
    creconfs_parts = [
        "CRECONFS",
        intruder_acid,
        intruder_ac_type,
        target_acid,
        str(dpsi),
        str(dcpa),
        str(tlosh)
    ]
    
    # Add optional parameters - use empty string if not provided
    if dh != 0:
        creconfs_parts.append(str(dh))
    else:
        creconfs_parts.append("")  # Empty placeholder for dH
        
    if tlosv != 0:
        creconfs_parts.append(str(tlosv))
    else:
        creconfs_parts.append("")  # Empty placeholder for tlos_ver
        
    if intruder_spd:
        creconfs_parts.append(str(intruder_spd))
    
    # Join with commas as CRECONFS expects comma-separated parameters
    content_lines.append(f"{stamp0}{','.join(creconfs_parts)}")
    content_lines.append("")
    
    # Write to file
    mode = "a" if append else "w"
    try:
        with open(out_path, mode, encoding="utf-8") as f:
            f.write("\n".join(content_lines))
            if not content_lines[-1]:  # If last line is empty, don't add extra newline
                pass
            else:
                f.write("\n")
    except Exception as exc:
        _echo_err(f"Failed to write relative conflict scenario to {out_path}: {exc}")
        return False
    
    return True


@command
def SATG_RC_CIRCLE(*argv):
    """SATG_RC_CIRCLE name n types center_lat center_lon radius_nm [mode] [altmode] [tcpa] [angle] [dh] [seed] [fl] [cas] [actypes] [overwrite] [area_type] [polygon_name]
    Append n randomized 2-AC conflicts with CPA uniformly inside a circle or polygon.
    - Args can be positional (in that order) or key=value (mix ok).
    - All aircraft spawn at t=0; CPA time equals tcpa (no tspan).

    types: CSV from {headon,cross,overtake}
    mode: abs | rel | mix (default: abs)
        - abs: Generate absolute geometric conflicts (CPA-based)
        - rel: Generate relative geometric conflicts (target + intruder)
        - mix: Randomly alternate between abs and rel
    altmode: level | altcross | mix
    dh: altitude offset in feet (can be value or range like "0:2000")
    area_type: circle | polygon (default: circle)
        - circle: Use center_lat, center_lon, radius_nm for circular area
        - polygon: Use polygon_name for polygon area (requires geopandas)
    polygon_name: Name of polygon when area_type=polygon
    """
    # parse argv
    order = ["name","n","types","center_lat","center_lon","radius_nm",
         "mode","altmode","tcpa","angle","seed","fl","cas","actypes","overwrite","area_type","polygon_name","include_polygon"]
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

    mode = str(_get("mode","abs")).lower()
    if mode not in ("abs","rel","mix"):
        _echo_err("SATG_RC_CIRCLE: mode must be abs|rel|mix"); return False, ""

    altmode = str(_get("altmode","level")).lower()
    if altmode not in ("level","altcross","mix"):
        _echo_err("SATG_RC_CIRCLE: altmode must be level|altcross|mix"); return False, ""

    # New area type parameter for circle vs polygon
    area_type = str(_get("area_type","circle")).lower()
    if area_type not in ("circle","polygon"):
        _echo_err("SATG_RC_CIRCLE: area_type must be circle|polygon"); return False, ""
    
    polygon_name = str(_get("polygon_name","")).strip() if area_type == "polygon" else ""
    
    # Validate polygon requirements
    if area_type == "polygon":
        if not polygon_name:
            _echo_err("SATG_RC_CIRCLE: polygon_name required when area_type=polygon"); return False, ""
        if not HAS_GEOPANDAS:
            _echo_err("SATG_RC_CIRCLE: geopandas required for polygon areas. Install with: pip install geopandas"); return False, ""
        # Check if polygon exists
        coords = get_polygon_coordinates(polygon_name)
        if not coords:
            _echo_err(f"SATG_RC_CIRCLE: polygon '{polygon_name}' not found. Use SATG_POLY_LIST to see available polygons."); return False, ""

    # Parse include_polygon parameter (default True for backward compatibility)
    include_polygon_str = str(_get("include_polygon", "1")).strip()
    include_polygon = include_polygon_str in ("1", "true", "yes", "on")

    tcpa_rng = _rng("tcpa", (60.0,240.0))
    angle_str = _get("angle", None)
    angle_rng = _parse_range(angle_str, STATE.gc_ranges["angle"]) if angle_str is not None else STATE.gc_ranges["angle"]
    cas_rng   = _rng("cas", STATE.gc_ranges["cas1"])
    fl_rng    = tuple(int(x) for x in _rng("fl", STATE.gc_ranges["fl1"]))
    
    # Parse optional dh (altitude offset) parameter  
    dh_str = _get("dh", None)
    dh_rng = _parse_range(dh_str, (0.0, 0.0)) if dh_str is not None else None

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
    
    # Track aircraft numbering for both absolute and relative conflicts
    aircraft_counter = 0
    gca_counter = 0
    if append:
        # If appending, start from existing maximum
        aircraft_counter = _scan_max_gcr_index(out_path)
        gca_counter = _scan_max_sc_index(out_path)
    
    # Prepare polygon commands for scenario file if using polygon area and include_polygon is enabled
    polygon_commands = []
    if area_type == "polygon" and include_polygon:
        poly_cmd = _get_polygon_creation_command(polygon_name)
        if poly_cmd:
            polygon_commands.append(poly_cmd)
        else:
            _echo_err(f"SATG_RC_CIRCLE: Cannot get creation command for polygon '{polygon_name}'"); return False, ""
    
    try:
        STATE.gc_ranges["cas1"] = cas_rng
        STATE.gc_ranges["cas2"] = cas_rng
        STATE.gc_ranges["fl1"]  = fl_rng
        STATE.gc_ranges["fl2"]  = fl_rng
        if angle_str is not None:
            STATE.gc_ranges["angle"] = angle_rng

        for i in range(n):
            # Determine mode for this conflict
            if mode == "mix":
                current_mode = rng.choice(["abs", "rel"])
            else:
                current_mode = mode
            
            typ = rng.choice(types)
            am_i = rng.choice(["level","altcross"]) if altmode == "mix" else altmode
            tcpa_i = _rand_in(rng, tcpa_rng[0], tcpa_rng[1])

            # Sample CPA position based on area type
            if area_type == "circle":
                # CPA uniformly by area: r = R*sqrt(u), theta ~ U(0,360)
                r = radius_nm * math.sqrt(rng.random())
                theta = rng.uniform(0.0, 360.0)
                cpa_lat, cpa_lon = _dest_nm(center_lat, center_lon, theta, r)
            else:  # area_type == "polygon"
                # Sample point within polygon using geopandas
                sampled_point = _sample_point_in_polygon(polygon_name, rng)
                if sampled_point is None:
                    _echo_err(f"Failed to sample point in polygon '{polygon_name}'"); return False, ""
                cpa_lat, cpa_lon = sampled_point

            if current_mode == "abs":
                # ABSOLUTE MODE: Use existing absolute geometric conflicts logic
                # Sample angle from range for all encounter types
                lo, hi = STATE.gc_ranges["angle"]
                if typ == "headon":
                    # Head-on: sample around 0 degrees (opposite directions)
                    angle_i = _rand_in(rng, max(0.0, lo), min(20.0, hi))
                elif typ == "overtake":
                    # Overtake: sample around 180 degrees (same direction, different speeds)  
                    angle_i = _rand_in(rng, max(160.0, lo), min(180.0, hi))
                else:
                    # Crossing: sample from the full range
                    angle_i = _rand_in(rng, lo, hi)

                # Determine altitude offset
                if dh_rng is not None:
                    # Use explicit dh parameter from GUI
                    dh_ft = _rand_in(rng, dh_rng[0], dh_rng[1])
                elif am_i == "altcross":
                    # Use default VSEP for altcross mode
                    dh_ft = float(STATE.gc_vsep_ft)
                    if rng.random() < 0.5:
                        dh_ft = -dh_ft
                else:
                    # Level encounters - no altitude offset
                    dh_ft = 0.0

                # Generate aircraft IDs using coordinated counter
                if append:
                    gca_counter += 2  # Reserve 2 IDs for this conflict pair
                    acid1 = f"GCA{gca_counter-1}"
                    acid2 = f"GCA{gca_counter}"
                else:
                    gca_counter += 2
                    acid1 = f"GCA{gca_counter-1}"
                    acid2 = f"GCA{gca_counter}"
                
                # Sample AC types uniformly for each aircraft
                ac1 = rng.choice(types_list)
                ac2 = rng.choice(types_list)

                _write_gc_scn(
                    out_path,
                    append=append,
                    name=nm,
                    cpa_lat=cpa_lat,
                    cpa_lon=cpa_lon,
                    tcpa_value=float(tcpa_i),
                    tcpa_range=None,
                    fl_cpa=None,
                    acid1=acid1,
                    acid2=acid2,
                    ac1=ac1,
                    ac2=ac2,
                    ac_types=None,
                    seed=rng.randint(0, 2**31-1),
                    angle_in=angle_i,
                    angle_range=None,
                    alt_offset_value=dh_ft,
                    alt_offset_range=None,
                    polygon_commands=polygon_commands if not append else None,
                )
                
            else:
                # RELATIVE MODE: Generate target + intruder relative conflict
                # Sample target location within circle
                target_lat, target_lon = cpa_lat, cpa_lon
                target_hdg = rng.uniform(0.0, 360.0)
                target_fl = rng.randint(fl_rng[0], fl_rng[1])
                target_alt_ft = target_fl * 100.0
                target_spd = _rand_in(rng, cas_rng[0], cas_rng[1])
                target_ac_type = rng.choice(types_list)
                
                # Generate sequential aircraft IDs for this conflict pair
                aircraft_counter += 1
                target_acid = f"GCR{aircraft_counter}"
                aircraft_counter += 1  
                intruder_acid = f"GCR{aircraft_counter}"
                
                # Map conflict type to relative parameters
                if typ == "headon":
                    dpsi = _rand_in(rng, 170.0, 190.0)  # Nearly opposite direction
                elif typ == "overtake":
                    dpsi = _rand_in(rng, 0.0, 20.0)     # Same direction
                else:  # crossing
                    dpsi = _rand_in(rng, 60.0, 120.0)   # Crossing angle
                
                # Convert TCPA to TLOSH (approximately the same for conflicts)
                tlosh = tcpa_i
                
                # Use HSEP as DCPA
                dcpa = float(STATE.gc_hsep_nm)
                
                # Determine altitude offset
                if dh_rng is not None:
                    # Use explicit dh parameter from GUI
                    dh_ft = _rand_in(rng, dh_rng[0], dh_rng[1])
                elif am_i == "altcross":
                    # Use default VSEP for altcross mode
                    dh_ft = float(STATE.gc_vsep_ft)
                    if rng.random() < 0.5:
                        dh_ft = -dh_ft
                else:
                    # Level encounters - no altitude offset
                    dh_ft = 0.0
                
                # Target data
                target_data = {
                    "create_target": True,
                    "acid": target_acid,
                    "lat": target_lat,
                    "lon": target_lon,
                    "hdg": target_hdg,
                    "alt_ft": target_alt_ft,
                    "spd": target_spd,
                    "ac_type": target_ac_type
                }
                
                # Conflict parameters
                conflict_params = {
                    "dpsi": dpsi,
                    "dcpa": dcpa,
                    "tlosh": tlosh,
                    "dh": dh_ft,
                    "tlosv": 0,  # Use horizontal TL
                    "spd": _rand_in(rng, cas_rng[0], cas_rng[1]),
                    "intruder_acid": intruder_acid,
                    "intruder_ac_type": rng.choice(types_list)
                }
                
                _write_gc_rel_scn(
                    out_path,
                    append=append,
                    name=nm,
                    target_data=target_data,
                    conflict_params=conflict_params,
                    intruder_types=types_list,
                    seed=rng.randint(0, 2**31-1)
                )
            
            append = True

        if area_type == "circle":
            area_info = f"center=({center_lat:.4f},{center_lon:.4f}) R={radius_nm:.2f}NM"
        else:  # polygon
            area_info = f"polygon='{polygon_name}'"
            
        _echo_ok(
            f"RC-CIRCLE wrote {n} conflicts to {out_path}\n"
            f" {area_info} mode={mode} altmode={altmode} types={','.join(types)}\n"
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
    STATE.proc_star_rates = {"initial": {}, "final": {}}
    STATE.proc_star_initial_groups.clear()
    STATE.proc_star_final_groups.clear()
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
def SATG_PROC_CFG_STAR(flights: int,
                       minsep: int,
                       initial_alt_fl: int = 360,
                       mach: float = 0.79,
                       mode: int = 0,
                       rate_basis: int = 0,
                       final_alt_fl: int = 100,
                       final_spd: int = 240):
    flights = max(0, int(flights))
    minsep = max(0, int(minsep))
    init_alt_fl_val = max(0, int(_to_int(initial_alt_fl, 360)))
    init_alt_ft = init_alt_fl_val * 100
    mach_val = max(0.0, float(mach))
    use_schedule = bool(int(mode))
    basis_idx = int(rate_basis)
    basis = "final" if basis_idx == 1 else "initial"
    final_alt_fl_val = max(0, int(_to_int(final_alt_fl, 100)))
    final_spd_val = max(0, int(_to_int(final_spd, 240)))
    STATE.proc_star_cfg.update({
        "flights": flights,
        "minsep": minsep,
        "initial_alt_fl": init_alt_fl_val,
        "initial_mach": mach_val,
        "final_alt_fl": final_alt_fl_val,
        "final_spd": final_spd_val,
        "use_schedule": use_schedule,
        "rate_basis": basis,
        # legacy keys retained for backward compatibility
        "alt_ft": init_alt_ft,
        "mach": mach_val,
    })
    mode_txt = "schedule" if use_schedule else "rate"
    basis_txt = "initial" if basis == "initial" else "final"
    _echo_ok(
        f"STAR procedures: flights={flights}, mode={mode_txt}, minsep={minsep}s, "
        f"initial=FL{init_alt_fl_val} M{mach_val:.2f}, final=FL{final_alt_fl_val} SPD {final_spd_val}, "
        f"rates by {basis_txt} waypoint"
    )
    return True, ""

@command
def SATG_PROC_CFG_STARRATE(proc_id: str, rate: float):
    rate_val = max(0.0, float(rate))
    pid = proc_id.strip()
    if not pid:
        _echo_err("SATG_PROC_CFG_STARRATE: missing identifier"); return False, ""
    pid_upper = pid.upper()
    current_basis = str(STATE.proc_star_cfg.get("rate_basis", "initial")).lower()
    if current_basis not in ("initial", "final"):
        current_basis = "initial"
    candidate_basis: Optional[str] = None
    key = ""
    label = pid_upper
    path = _resolve_proc_path(proc_id)
    if path:
        info = STATE.proc_star_info.get(path, {})
        initial_key = (info.get("initial_fix") or info.get("fix") or "").upper()
        final_key = (info.get("final_fix") or "").upper()
        basis_order = [current_basis, "final" if current_basis == "initial" else "initial"]
        basis_order = [b for b in basis_order if b in ("initial", "final")]
        for basis_opt in basis_order:
            if basis_opt == "final" and final_key:
                candidate_basis = "final"; key = final_key; break
            if basis_opt == "initial" and initial_key:
                candidate_basis = "initial"; key = initial_key; break
        if not key:
            if final_key:
                candidate_basis = "final"; key = final_key
            elif initial_key:
                candidate_basis = "initial"; key = initial_key
        if not key:
            short = os.path.basename(path)
            _echo_err(f"SATG_PROC_CFG_STARRATE: procedure '{short}' missing waypoint information."); return False, ""
        label = key if candidate_basis == "final" else os.path.basename(path)
    else:
        groups_map = {
            "initial": STATE.proc_star_initial_groups,
            "final": STATE.proc_star_final_groups,
        }
        for basis_opt in (current_basis, "final", "initial"):
            grp = groups_map.get(basis_opt, {})
            if pid_upper in grp:
                candidate_basis = basis_opt
                key = pid_upper
                break
        if not key:
            _echo_err(f"SATG_PROC_CFG_STARRATE: unknown waypoint or procedure '{proc_id}'"); return False, ""
    if candidate_basis not in ("initial", "final"):
        candidate_basis = current_basis
    rate_table = STATE.proc_star_rates.setdefault("final" if candidate_basis == "final" else "initial", {})
    rate_table[key] = rate_val
    STATE.proc_star_cfg["rate_basis"] = candidate_basis
    descriptor = "final waypoint" if candidate_basis == "final" else "initial waypoint"
    _echo_ok(f"STAR {descriptor} {label}: rate={rate_val:.1f} ac/h")
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

    def _max_pr_index(acids: set) -> int:
        max_idx = 0
        for acid in acids:
            m = re.search(r"(\d+)$", acid)
            if m:
                max_idx = max(max_idx, int(m.group(1)))
        return max_idx

    next_pr_index = _max_pr_index(used)

    def _next_pr_acid() -> str:
        nonlocal next_pr_index
        while True:
            next_pr_index += 1
            acid = f"PRC{next_pr_index:03d}"
            if acid not in used:
                used.add(acid)
                return acid

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

    mode = "a" if append else "w"
    with open(out_path, mode, encoding="utf-8") as f:
        if not append:
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")
            for w in STATE.proc_wpt_files:
                f.write(f"0:00:00.00>PCALL {_cmd_path(w)}\n")

        # Generic procedures
        for _ in range(int(gen_cfg["flights"])):
            proc_path = rng.choice(generic_paths)
            proc_name = os.path.splitext(os.path.basename(proc_path))[0]

            t0 = next_generic[proc_path]; next_generic[proc_path] = t0 + gen_cfg["minsep"]
            ts = _fmt_ts(t0)

            acid = _next_pr_acid()

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

            acid = _next_pr_acid()

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

                    acid = _next_pr_acid()

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
        rate_basis_raw = str(star_cfg.get("rate_basis", "initial")).lower()
        star_rate_basis = "final" if rate_basis_raw.startswith("final") else "initial"
        star_rate_table = {}
        if isinstance(STATE.proc_star_rates, dict):
            star_rate_table = STATE.proc_star_rates.get(star_rate_basis, {})
        fallback_star_minsep = max(1.0, float(star_cfg.get("minsep", 90)))
        initial_alt_fl = max(0, int(star_cfg.get("initial_alt_fl", max(0, int(round(star_cfg.get("alt_ft", 36000) / 100))))))
        star_alt_ft = initial_alt_fl * 100
        star_mach_val = float(star_cfg.get("initial_mach", star_cfg.get("mach", 0.79)))
        star_final_fl = max(0, int(star_cfg.get("final_alt_fl", 100)))
        star_final_spd = max(0, int(star_cfg.get("final_spd", 240)))
        star_schedule_data = STATE.proc_star_schedules if use_star_schedule else {}

        star_groups: Dict[str, List[str]] = {}
        path_to_group: Dict[str, str] = {}
        for proc_path in star_paths:
            info = STATE.proc_star_info.get(proc_path, {})
            if star_rate_basis == "final":
                group_key = info.get("final_fix") or ""
            else:
                group_key = info.get("initial_fix") or info.get("fix") or ""
            if not group_key:
                group_key = os.path.splitext(os.path.basename(proc_path))[0]
            group_key = group_key.upper()
            path_to_group[proc_path] = group_key
            star_groups.setdefault(group_key, []).append(proc_path)

        group_rates = {key: max(float(star_rate_table.get(key, DEFAULT_STAR_RATE)), 0.0) for key in star_groups}
        group_next_time: Dict[str, float] = {key: 0.0 for key in star_groups}

        def _write_star_entry(t_sec: float, proc_path: str) -> bool:
            proc_name = os.path.splitext(os.path.basename(proc_path))[0]
            info = STATE.proc_star_info.get(proc_path, {})
            f1_name = info.get("initial_fix")
            f2_name = info.get("second_fix")
            final_fix = info.get("final_fix")
            pen_final = info.get("penultimate_fix")
            if not f1_name or final_fix is None or pen_final is None or (f2_name is None and f1_name is None):
                seq = _proc_fix_sequence(proc_path)
                if seq:
                    if not f1_name and seq:
                        f1_name = seq[0]
                    if f2_name is None and len(seq) > 1:
                        f2_name = seq[1]
                    if not final_fix and seq:
                        final_fix = seq[-1]
                    if not pen_final and len(seq) > 1:
                        pen_final = seq[-2]
                    STATE.proc_star_info[proc_path] = {
                        **info,
                        "initial_fix": f1_name.upper() if f1_name else None,
                        "second_fix": f2_name.upper() if f2_name else None,
                        "final_fix": final_fix.upper() if final_fix else None,
                        "penultimate_fix": pen_final.upper() if pen_final else None,
                        "fix": (f1_name.upper() if f1_name else info.get("fix")),
                        "basename": info.get("basename", proc_name),
                        "path": proc_path,
                    }
                    info = STATE.proc_star_info[proc_path]
                    initial_up = (info.get("initial_fix") or "").upper()
                    final_up = (info.get("final_fix") or "").upper()
                    pen_up = (info.get("penultimate_fix") or "").upper()
                    if initial_up:
                        STATE.proc_star_initial_groups.setdefault(initial_up, set()).add(proc_path)
                        STATE.proc_star_rates.setdefault("initial", {}).setdefault(initial_up, DEFAULT_STAR_RATE)
                    if final_up:
                        STATE.proc_star_final_groups.setdefault(final_up, set()).add(proc_path)
                        STATE.proc_star_rates.setdefault("final", {}).setdefault(final_up, DEFAULT_STAR_RATE)
                    if pen_up:
                        STATE.proc_star_info[proc_path]["penultimate_fix"] = pen_up
                    pen_final = info.get("penultimate_fix")
            if not f1_name:
                _echo_err(f"{proc_name}: procedure missing initial waypoint."); return False
            if not final_fix:
                _echo_err(f"{proc_name}: procedure missing final waypoint."); return False
            f1_up = f1_name.upper()
            final_fix_up = final_fix.upper()
            f2_up = f2_name.upper() if isinstance(f2_name, str) else None
            pen_final_up = pen_final.upper() if isinstance(pen_final, str) else None
            fix_keys.add(f1_up)
            coord1 = _resolve_fix_coord(f1_up, fix_db)
            lat0 = lon0 = None
            if coord1:
                lat0, lon0 = coord1
            coord2 = _resolve_fix_coord(f2_up, fix_db, lat0, lon0) if (f2_up and lat0 is not None and lon0 is not None) else None
            if coord2:
                lat1, lon1 = coord2
                fix_keys.add(f2_up)
                hdg0 = _bearing_deg(lat0, lon0, lat1, lon1)
            else:
                hdg0 = 0.0
            fix_keys.add(final_fix_up)
            ts = _fmt_ts(t_sec)
            acid = _next_pr_acid()
            actype = "A320"
            hdg_cmd = int(round(hdg0)) % 360
            mach_token = f"M{star_mach_val:.2f}"
            spawn_token = f1_up
            f.write(f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d} {star_alt_ft} {mach_token}\n")
            f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
            f.write(f"{ts}LNAV {acid} ON\n")
            f.write(f"{ts}VNAV {acid} ON\n")
            if STATE.proc_destinations_enabled:
                dests = STATE.proc_destinations.get(proc_path)
                if dests:
                    dest_choice = rng.choice(dests)
                    f.write(f"{ts}DEST {acid} {dest_choice}\n")
            final_hdg = None
            if pen_final_up:
                coord_pen = _resolve_fix_coord(pen_final_up, fix_db)
                coord_fin = _resolve_fix_coord(final_fix_up, fix_db)
                if coord_pen and coord_fin:
                    final_hdg = _bearing_deg(coord_pen[0], coord_pen[1], coord_fin[0], coord_fin[1])
            if final_hdg is None:
                final_hdg = hdg0
            final_hdg_cmd = int(round(final_hdg)) % 360
            final_alt_tok = _fmt_alt_token(star_final_fl)
            f.write(f"{ts}{acid} AT {final_fix_up} ALT {final_alt_tok}\n")
            f.write(f"{ts}{acid} AT {final_fix_up} SPD {star_final_spd}\n")
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
            for t_sec, proc_path in scheduled_star_events:
                if not _write_star_entry(t_sec, proc_path):
                    return False, ""
                group_key = path_to_group.get(proc_path)
                rate = group_rates.get(group_key, DEFAULT_STAR_RATE)
                interval = 3600.0 / rate if rate > 0.0 else fallback_star_minsep
                if group_key is not None:
                    group_next_time[group_key] = max(group_next_time.get(group_key, 0.0), t_sec + interval)

            if star_remaining > 0 and star_groups:
                keys = list(star_groups.keys())
                weights = [max(group_rates.get(key, DEFAULT_STAR_RATE), 0.0) for key in keys]
                total_weight = sum(weights)
                for _ in range(star_remaining):
                    if total_weight <= 0.0:
                        group_key = rng.choice(keys)
                    else:
                        group_key = _weighted_choice(rng, keys, weights)
                    rate = group_rates.get(group_key, DEFAULT_STAR_RATE)
                    interval = 3600.0 / rate if rate > 0.0 else fallback_star_minsep
                    t0 = group_next_time.get(group_key, 0.0)
                    group_next_time[group_key] = t0 + interval
                    proc_choices = star_groups.get(group_key, [])
                    if not proc_choices:
                        continue
                    proc_path = rng.choice(proc_choices)
                    if not _write_star_entry(t0, proc_path):
                        return False, ""

    _sort_scn_file(out_path)
    if not append:
        _renumber_pr_acids(out_path)
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
        "Random conflicts in a circle or polygon:",
        "  SATG_RC_CIRCLE name N types center_lat center_lon radius_nm mode altmode tcpa fl cas actypes overwrite [angle] [area_type] [polygon_name]",
        "  - types: headon,cross,overtake (comma separated)",
        "  - altmode: level | altcross | mix",
        "  - tcpa: seconds lo:hi, all aircraft spawn at t=0 (tcpa is time to CPA)",
        "  - fl: flight level lo:hi",
        "  - cas: kt lo:hi",
        "  - angle: lo:hi degrees, only for crossing",
        "  - overwrite: 1 overwrite file, 0 append",
        "  - area_type: circle | polygon (default: circle)",
        "  - polygon_name: name of polygon when area_type=polygon (requires geopandas)",
        "",
        "Geometric conflicts:",
        "  SATG_GC_CONF HSEP VSEP",
        "  SATG_GC_RANGE fl=lo:hi cas=lo:hi",
    "  SATG_GC_CRE name=<...> typ=<headon,cross,overtake> altmode=<level|altcross|mix> (lat=<deg> lon=<deg> | wp=<ident>) tcpa=<s> [angle=<deg>] [actypes=<csv>] overwrite=<0|1>",
        "  SATG_GC_RUN name",
        "",
        "Realistic replay:",
        "  SATG_RL_MAKE name overwrite",
        "  SATG_RL_RUN  name overwrite",
        "  Notes: jitter and auto-delete are set via the GUI and applied just-in-time.",
        "",
        "Polygon management:",
        "  SATG_POLY_CREATE name lat1 lon1 lat2 lon2 [lat3 lon3 ...]",
        "  SATG_POLY_LIST",
        "  SATG_POLY_INFO name",
        "  SATG_POLY_COORDS name",
        "  SATG_POLY_TEST polygon_name lat lon",
        "  Create custom polygon areas and use them in random conflicts",
        "  💡 TIP: Create polygons with native POLY command, then enter name in GUI",
        "",
        "Procedures mode:",
        "  Load waypoint files (DEFWPT) and procedure files (%0) first:",
        "    SATG_PROC_LOAD_WPT path_to_fix_file.scn",
        "    SATG_PROC_LOAD_PROC path_to_proc_file.scn",
        "    SATG_PROC_SET_ICAO SID-XX-NAME ICAO",
        "    SATG_PROC_CFG_GENERIC flights minsep",
        "    SATG_PROC_CFG_SID flights alt_ft spd_kt",
        "    SATG_PROC_CFG_SIDRATE runway rate_per_hour",
        "    SATG_PROC_CFG_STAR flights minsep init_fl mach mode ratebasis final_fl final_spd",
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
        "Polygon management:",
        "    SATG_POLY_CREATE name lat1 lon1 lat2 lon2 [lat3 lon3 ...]",
        "    SATG_POLY_LIST",
        "    SATG_POLY_INFO name",
        "    SATG_POLY_COORDS name",
        "    SATG_POLY_TEST polygon_name lat lon",
        "  Create custom polygon areas and use them in random conflicts",
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

def reset():
    """Reset SATG plugin state when BlueSky simulation resets (e.g., when loading a new scenario)."""
    # Clear tracked aircraft lists so they don't carry over to the new scenario
    STATE.gc_last_acids.clear()
    STATE.gc_rel_seq = 1


# ================= POLYGON INTEGRATION COMMANDS ================= #

@command
def SATG_POLY_CREATE(name: str, *coordinates):
    """SATG_POLY_CREATE name lat1 lon1 lat2 lon2 [lat3 lon3 ...]
    Create a polygon area and make it available for SATG operations.
    This is a convenience wrapper around BlueSky's POLY command.
    """
    if len(coordinates) < 6:  # Need at least 3 points (6 coordinates)
        _echo_err("Usage: SATG_POLY_CREATE name lat1 lon1 lat2 lon2 lat3 lon3 [...]")
        return False, ""
    
    # Convert coordinates to the format expected by BlueSky's POLY command
    coord_list = [float(c) for c in coordinates]
    
    # Call BlueSky's native POLY command
    result = areafilter.defineArea(name, "POLY", coord_list)
    
    if result:
        _echo_ok(f"Created polygon '{name}' with {len(coord_list)//2} vertices")
        _echo_ok(f"Polygon '{name}' is now available for Random Conflicts")
        # Trigger GUI refresh if possible
        _notify_gui_polygon_update()
        return True, ""
    else:
        _echo_err(f"Failed to create polygon '{name}'")
        return False, ""

@command
def SATG_POLY_LIST():
    """SATG_POLY_LIST
    List all available polygon areas.
    """
    if not areafilter.basic_shapes:
        stack.stack("ECHO No polygons currently defined.")
        return True, ""
    
    poly_info = []
    for name, shape in areafilter.basic_shapes.items():
        if hasattr(shape, 'coordinates'):
            num_vertices = len(shape.coordinates) // 2
            poly_info.append(f"{name}: {num_vertices} vertices")
    
    if poly_info:
        stack.stack("ECHO Available polygons:")
        for info in poly_info:
            stack.stack(f"ECHO {info}")
    else:
        stack.stack("ECHO No polygon areas found.")
    
    return True, ""

@command 
def SATG_POLY_INFO(name: str):
    """SATG_POLY_INFO name
    Display detailed information about a specific polygon.
    """
    poly = areafilter.getArea(name)
    if poly is None:
        _echo_err(f"Polygon '{name}' not found. Use SATG_POLY_LIST to see available polygons.")
        return False, ""
    
    if not hasattr(poly, 'coordinates'):
        _echo_err(f"'{name}' is not a polygon area.")
        return False, ""
    
    coords = poly.coordinates
    num_vertices = len(coords) // 2
    
    # Format coordinate pairs
    vertex_list = []
    for i in range(0, len(coords), 2):
        lat, lon = coords[i], coords[i+1]
        vertex_list.append(f"  {i//2 + 1}: {lat:.6f}, {lon:.6f}")
    
    info = [
        f"Polygon '{name}':",
        f"  Vertices: {num_vertices}",
        f"  Altitude range: {poly.bottom:.0f} - {poly.top:.0f} ft",
        "  Coordinates:"
    ] + vertex_list
    
    for line in info:
        stack.stack(f"ECHO {line}")
    
    return True, ""

@command
def SATG_POLY_COORDS(name: str):
    """SATG_POLY_COORDS name
    Get the coordinates of a polygon as a comma-separated list.
    """
    poly = areafilter.getArea(name)
    if poly is None:
        _echo_err(f"Polygon '{name}' not found.")
        return False, ""
    
    if not hasattr(poly, 'coordinates'):
        _echo_err(f"'{name}' is not a polygon area.")
        return False, ""
    
    # Format coordinates as comma-separated string
    coord_str = ",".join(f"{c:.6f}" for c in poly.coordinates)
    stack.stack(f"ECHO Coordinates for '{name}': {coord_str}")
    return True, coord_str

def get_polygon_coordinates(name: str) -> Optional[List[Tuple[float, float]]]:
    """Helper function to get polygon coordinates as list of (lat, lon) tuples.
    
    Returns:
        List of (lat, lon) tuples if polygon exists, None otherwise
    """
    # Try exact name first
    poly = areafilter.getArea(name)
    
    # If not found, try case-insensitive search
    if poly is None and areafilter.basic_shapes:
        for area_name, shape in areafilter.basic_shapes.items():
            if area_name.lower() == name.lower():
                poly = shape
                break
    
    if poly is None or not hasattr(poly, 'coordinates'):
        return None
    
    coords = poly.coordinates
    return [(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]

def is_point_in_polygon(lat: float, lon: float, polygon_name: str) -> bool:
    """Check if a point is inside a named polygon.
    
    Args:
        lat: Latitude of the point
        lon: Longitude of the point  
        polygon_name: Name of the polygon area
        
    Returns:
        True if point is inside polygon, False otherwise
    """
    # Try exact name first
    poly = areafilter.getArea(polygon_name)
    actual_name = polygon_name
    
    # If not found, try case-insensitive search
    if poly is None and areafilter.basic_shapes:
        for area_name, shape in areafilter.basic_shapes.items():
            if area_name.lower() == polygon_name.lower():
                poly = shape
                actual_name = area_name
                break
    
    if poly is None:
        return False
    
    # Use BlueSky's built-in point-in-polygon check with the actual name
    result = areafilter.checkInside(actual_name, [lat], [lon], [0])
    return bool(result[0]) if len(result) > 0 else False

@command
def SATG_POLY_TEST(polygon_name: str, lat: float, lon: float):
    """SATG_POLY_TEST polygon_name lat lon
    Test if a specific point is inside the named polygon.
    """
    if not areafilter.getArea(polygon_name):
        _echo_err(f"Polygon '{polygon_name}' not found.")
        return False, ""
    
    inside = is_point_in_polygon(lat, lon, polygon_name)
    status = "INSIDE" if inside else "OUTSIDE"
    stack.stack(f"ECHO Point ({lat:.6f}, {lon:.6f}) is {status} polygon '{polygon_name}'")
    return True, ""

# ------------------- Plugin init -------------------- #
