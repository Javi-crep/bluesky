"""
SATG: Scenario and Traffic Generator Plugin for BlueSky ATM Simulator

This module provides comprehensive scenario generation capabilities for the BlueSky Air Traffic 
Management simulator, including realistic replay from EUROCONTROL data and geometric conflict 
generation for training and testing purposes.

Main Features:
    - Realistic Replay: Generate scenarios from historical EUROCONTROL flight data
    - Historic Sampling: Create synthetic scenarios using TraffixGen ML models  
    - Geometric Conflicts: Generate controlled conflict scenarios for testing
    - Random Conflicts: Create randomized conflict situations
    - Procedure Integration: Support for SID/STAR procedures and custom waypoints
    - Advanced Filtering: Geographic, altitude, time, and aircraft type filtering

The module integrates with the broader BlueSky ecosystem and provides both command-line
and GUI interfaces for scenario generation and management.

Classes:
    - Various dialog classes for GUI interaction
    - Data structures for flight and waypoint management
    
Functions:
    - Scenario generation and export functions
    - Data processing and filtering utilities
    - Coordinate conversion and formatting helpers
    - Flight trajectory and conflict calculation algorithms

Dependencies:
    - BlueSky core simulator
    - TraffixGen plugin for ML-based synthetic data
    - PyQt6 for GUI components
    - NumPy/Pandas for data processing
    - Optional: GeoPandas for advanced geographic operations

Usage:
    This plugin is automatically loaded by BlueSky and provides commands accessible
    via the simulator's command interface and GUI panels.

Author: BlueSky ATM Simulator Team
Version: Compatible with BlueSky 1.5+
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
    """
    Convert latitude to standardized text format with robust numeric handling.
    
    Converts a latitude value (in decimal degrees) to a formatted string representation
    using degrees, minutes, and seconds notation. Handles both regular Python floats
    and numpy scalar types safely.
    
    Args:
        lat (float): Latitude in decimal degrees. Can be numpy scalar or Python float.
                    Valid range is -90.0 to +90.0 degrees.
    
    Returns:
        str: Formatted latitude string in format "N##'##'##"" or "S##'##'##""
             where N/S indicates hemisphere, followed by degrees, minutes, seconds.
    
    Examples:
        >>> _satg_safe_lat2txt(52.3676)
        "N52'22'03""
        >>> _satg_safe_lat2txt(-34.6037)  
        "S34'36'13""
        >>> _satg_safe_lat2txt(0.0)
        "N00'00'00""
    
    Note:
        This function is designed to handle numpy scalars which can sometimes cause
        issues with standard formatting functions. All values are explicitly converted
        to Python float before processing.
    """
    latf = float(lat)
    d, m, s = _misc.float2degminsec(abs(latf))
    d = int(round(d))
    m = int(round(m))
    s = int(round(s))
    prefix = "S" if latf < 0 else "N"
    return f"{prefix}{d:02d}'{m:02d}'{s}\""


def _satg_safe_lon2txt(lon: float) -> str:
    """
    Convert longitude to standardized text format with robust numeric handling.
    
    Converts a longitude value (in decimal degrees) to a formatted string representation
    using degrees, minutes, and seconds notation. Handles both regular Python floats
    and numpy scalar types safely.
    
    Args:
        lon (float): Longitude in decimal degrees. Can be numpy scalar or Python float.
                    Valid range is -180.0 to +180.0 degrees.
    
    Returns:
        str: Formatted longitude string in format "E###'##'##"" or "W###'##'##""
             where E/W indicates hemisphere, followed by degrees, minutes, seconds.
             Note the longitude uses 3 digits for degrees (vs 2 for latitude).
    
    Examples:
        >>> _satg_safe_lon2txt(4.8925)
        "E004'53'33""
        >>> _satg_safe_lon2txt(-74.0060)
        "W074'00'22""
        >>> _satg_safe_lon2txt(180.0)
        "E180'00'00""
    
    Note:
        This function is designed to handle numpy scalars which can sometimes cause
        issues with standard formatting functions. All values are explicitly converted
        to Python float before processing.
    """
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
    """
    Convert ground speed to calibrated airspeed using atmospheric models.
    
    Performs atmospheric calculations to convert ground speed (assuming no wind) to 
    calibrated airspeed (CAS) at a given flight level. Uses International Standard
    Atmosphere (ISA) model for temperature and pressure calculations.
    
    Args:
        gs_kt (float): Ground speed in knots. Assumed to equal true airspeed when wind=0.
        flight_level (float): Flight level (e.g., 350 for FL350). Converted to altitude
                             using standard 100ft per FL conversion.
    
    Returns:
        float: Calibrated airspeed in knots. Always non-negative.
    
    Note:
        This function assumes zero wind conditions (ground speed = true airspeed).
        For low Mach numbers (M < 0.1), uses simplified density-based conversion.
        For higher speeds, uses compressible flow equations with Mach number calculations.
        
    Examples:
        >>> _gs_to_cas_kt(450.0, 350.0)  # 450kt GS at FL350
        420.5  # Approximate CAS in knots
        >>> _gs_to_cas_kt(250.0, 100.0)  # 250kt GS at FL100  
        245.2  # Approximate CAS in knots
    
    Raises:
        None: Function handles edge cases gracefully with max() operations.
    """
    tas_ms = gs_kt / MS2KT  # wind=0 => TAS approx GS
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
    # Command hints disabled - GUI provides the interface
    # if nxt: _echo_lines([f"[NEXT] {nxt}"])

def _echo_err(msg: str):
    for line in str(msg).splitlines(): _echo_lines([f"[SATG][ERR] {line}"])

def _fmt_alt_token(fl: int) -> str:
    return "0" if int(fl) <= 0 else f"FL{int(fl):03d}"

def _sanitize_name(name: str) -> str:
    s = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if not s or not s[0].isalpha(): s = "WPT_" + s
    return s[:32]

def _generate_scenario_header(scenario_type: str, **params) -> List[str]:
    """Generate commented header lines for scenario files"""
    from datetime import datetime
    
    header = [
        f"# SATG Generated Scenario - {scenario_type}",
        f"# Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Generator: BlueSky SATG Plugin",
        "#"
    ]
    
    # Add type-specific parameters
    if scenario_type == "Random Conflicts":
        header.extend([
            f"# Scenario Type: {scenario_type}",
            f"# Aircraft Count: {params.get('n', 'N/A')}",
            f"# Center: {params.get('center_lat', 'N/A')} deg, {params.get('center_lon', 'N/A')} deg",
            f"# Radius: {params.get('radius_nm', 'N/A')} NM",
            f"# Conflict Types: {params.get('types', 'N/A')}",
            f"# Altitude Mode: {params.get('altmode', 'N/A')}",
            f"# Time to CPA: {params.get('tcpa', 'N/A')} seconds",
            f"# Flight Levels: {params.get('fl_range', 'N/A')}",
            f"# CAS Range: {params.get('cas_range', 'N/A')} kt"
        ])
    elif scenario_type == "Geometric Conflicts":
        header.extend([
            f"# Scenario Type: {scenario_type}",
            f"# Position: {params.get('lat', 'N/A')} deg, {params.get('lon', 'N/A')} deg",
            f"# Time to CPA: {params.get('tcpa', 'N/A')} seconds",
            f"# Conflict Angle: {params.get('angle', 'N/A')} deg",
            f"# Aircraft Types: {params.get('actypes', 'N/A')}",
            f"# Altitude Mode: {params.get('altmode', 'N/A')}"
        ])
    elif scenario_type == "Realistic Replay":
        header.extend([
            f"# Scenario Type: {scenario_type}",
            f"# Data Source: Historical traffic data",
            f"# Jitter Applied: {params.get('jitter_enabled', 'N/A')}",
            f"# Auto-delete: {params.get('autodel_enabled', 'N/A')}"
        ])
    elif scenario_type == "Procedural Traffic":
        header.extend([
            f"# Scenario Type: {scenario_type}",
            f"# Aircraft Count: {params.get('n', 'N/A')}",
            f"# Generic Procedures: Spawn at first waypoint",
            f"# SID Procedures: {params.get('sid_enabled', 'No')}",
            f"# STAR Procedures: {params.get('star_enabled', 'No')}"
        ])
    
    header.append("#")
    return header

# ---------------- Math helpers (bearing/destination) ---------------- #
def _bearing_nm(lat1, lon1, lat2, lon2):
    """
    Calculate initial great-circle bearing between two geographic points.
    
    This function computes the initial bearing (forward azimuth) along the
    great-circle path from the first point to the second point using either
    BlueSky's optimized geo functions or a fallback mathematical implementation.
    The result represents the compass direction to follow at the starting point
    to reach the destination along the shortest spherical path.
    
    Args:
        lat1 (float): Latitude of the starting point in decimal degrees
        lon1 (float): Longitude of the starting point in decimal degrees  
        lat2 (float): Latitude of the destination point in decimal degrees
        lon2 (float): Longitude of the destination point in decimal degrees
    
    Returns:
        float: Initial bearing in degrees (0-360), where 0° is North, 90° is East
    
    Examples:
        # Calculate bearing from Amsterdam to Berlin
        bearing = _bearing_nm(52.3676, 4.9041, 52.5200, 13.4050)
        
        # Calculate bearing for navigation waypoint routing
        next_bearing = _bearing_nm(current_lat, current_lon, waypoint_lat, waypoint_lon)
    
    Note:
        This function uses BlueSky's geo.qdrdist when available for optimized
        calculations, with a mathematical fallback for environments where
        BlueSky geo functions are not accessible. The bearing is the initial
        direction and will change along the great-circle path due to convergence.
    """
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
    """
    Calculate destination point from starting coordinates along a bearing.
    
    This function computes the destination coordinates reached by traveling
    from a starting point along a specified bearing for a given distance.
    The calculation follows great-circle navigation principles, accounting
    for the spherical nature of Earth's surface.
    
    Args:
        lat (float): Starting latitude in decimal degrees
        lon (float): Starting longitude in decimal degrees
        brg_deg (float): True bearing in degrees (0-360), where 0° is North
        dist_nm (float): Distance to travel in nautical miles
    
    Returns:
        tuple: (latitude, longitude) of the destination point in decimal degrees
    
    Examples:
        # Calculate position 50 NM northeast from current location
        dest_lat, dest_lon = _dest_nm(52.0, 4.0, 45.0, 50.0)
        
        # Find waypoint coordinates for procedure design
        wp_lat, wp_lon = _dest_nm(airport_lat, airport_lon, runway_heading, 10.0)
    
    Note:
        This function uses BlueSky's geo.qdrpos when available for optimized
        spherical calculations, with a mathematical fallback implementation.
        The calculation accounts for Earth's curvature and provides accurate
        results for aviation navigation applications.
    """
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

        # Flight phase-based jitter configuration
        self.phase_jitter_enabled: bool = False
        self.phase_altitudes = {
            'takeoff': {'min_fl': 0, 'max_fl': 15},      # Ground to initial climb
            'climb': {'min_fl': 15, 'max_fl': 250},      # Climbing phase
            'cruise': {'min_fl': 250, 'max_fl': 450},    # Cruise altitude
            'descent': {'min_fl': 50, 'max_fl': 250},    # Descending from cruise  
            'approach': {'min_fl': 0, 'max_fl': 50}      # Final approach
        }
        self.phase_configs = {
            'takeoff': {'enabled': False, 'dt_max': 0.0, 'dlat_max': 0.0, 'dlon_max': 0.0, 'dfl_max': 0},
            'climb': {'enabled': False, 'dt_max': 0.0, 'dlat_max': 0.0, 'dlon_max': 0.0, 'dfl_max': 0},
            'cruise': {'enabled': False, 'dt_max': 0.0, 'dlat_max': 0.0, 'dlon_max': 0.0, 'dfl_max': 0},
            'descent': {'enabled': False, 'dt_max': 0.0, 'dlat_max': 0.0, 'dlon_max': 0.0, 'dfl_max': 0},
            'approach': {'enabled': False, 'dt_max': 0.0, 'dlat_max': 0.0, 'dlon_max': 0.0, 'dfl_max': 0}
        }
        
        # Track-specific phase configurations for per-track altitude boundaries
        self.track_phase_altitudes = {}
        
        # Mapping from aircraft ID to track name (for track-specific phase determination)
        self.aircraft_to_track = {}

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
        self.proc_generic_cfg = {
            "flights": 20,
            "override_initial_alt": False,
            "override_initial_spd": False,
            "override_final_alt": False,
            "override_final_spd": False
        }
        self.proc_generic_actypes: List[str] = ["A320", "B738", "A350"]
        self.proc_sid_cfg = {
            "flights": 0, 
            "alt_ft": 3000, 
            "spd_kt": 210,
            "override_initial_alt": False,
            "override_initial_spd": False
        }
        self.proc_sid_actypes: List[str] = ["A320", "B738", "A350"]
        self.proc_star_cfg = {
            "flights": 20,
            "minsep": 90,
            "initial_alt_fl": 360,
            "initial_mach": 0.79,
            "final_alt_fl": 100,
            "final_spd": 240,
            "use_schedule": False,
            "rate_basis": "initial",
            "override_initial_alt": False,
            "override_initial_spd": False,
            "override_final_alt": False,
            "override_final_spd": False
        }
        self.proc_star_actypes: List[str] = ["A320", "B738", "A350"]
        self.proc_sid_rates: Dict[str, float] = {}
        self.proc_star_rates: Dict[str, Dict[str, float]] = {"initial": {}, "final": {}}
        self.proc_generic_rates: Dict[str, Dict[str, float]] = {"initial": {}, "final": {}}
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

def _get_flight_phase(fl: int, waypoint_sequence: list, current_index: int, track_name: str = None) -> str:
    """Determine flight phase using simplified logic matching GUI implementation
    
    Simple approach (matches GUI _get_flight_phase_with_config exactly):
    1. Look at rate of change before and after current waypoint
    2. Both positive = climbing, both negative = descending
    3. Same before = level, mixed = momentary variation
    4. Apply altitude boundaries for final classification
    
    Args:
        fl: Current flight level
        waypoint_sequence: List of waypoints with 'fl' field
        current_index: Index of current waypoint in the sequence
        track_name: Optional track name for track-specific phase boundaries
        
    Returns:
        Flight phase: 'takeoff', 'climb', 'cruise', 'descent', or 'approach'
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
    threshold = 5  # Minimum FL change to consider significant (matches GUI)
    
    is_climbing_before = rate_before > threshold
    is_descending_before = rate_before < -threshold
    is_climbing_after = rate_after > threshold  
    is_descending_after = rate_after < -threshold
    
    # Simple logic for trend determination (matches GUI exactly)
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
    
    # Get phase altitude boundaries (track-specific or default)
    phase_altitudes = STATE.phase_altitudes
    if track_name and track_name in STATE.track_phase_altitudes:
        phase_altitudes = STATE.track_phase_altitudes[track_name]
    
    # Extract altitude boundaries for phase determination
    takeoff_max = phase_altitudes.get('takeoff', {}).get('max_fl', 15)
    climb_max = phase_altitudes.get('climb', {}).get('max_fl', 250)
    descent_max = phase_altitudes.get('descent', {}).get('max_fl', 250)  
    approach_max = phase_altitudes.get('approach', {}).get('max_fl', 50)
    
    # Phase determination based on trend + altitude boundaries (matches GUI exactly)
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
        # Level flight - determine phase based on altitude only (matches GUI exactly)
        if current_fl <= takeoff_max:
            return 'takeoff'
        elif current_fl <= approach_max:
            return 'approach'  # Low altitude level = approach
        elif current_fl <= climb_max:
            return 'cruise'   # Medium altitude level = cruise
        else:
            return 'cruise'   # High altitude level = cruise

def _get_points_for_run() -> Dict[str, List[dict]]:
    if not STATE.base_points: return {}
    pts = {acid: [dict(p) for p in plist] for acid, plist in STATE.base_points.items()}
    
    # Check if any jitter is enabled (global or phase-based)
    jitter_enabled = STATE.jitter_on or STATE.phase_jitter_enabled
    if not jitter_enabled: return pts
    
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

    def _get_phase_jitter_params(phase: str) -> dict:
        """Get jitter parameters for a specific flight phase"""
        if not STATE.phase_jitter_enabled or phase not in STATE.phase_configs:
            return {'dt_max': 0.0, 'dlat_max': 0.0, 'dlon_max': 0.0, 'dfl_max': 0}
        
        phase_config = STATE.phase_configs[phase]
        if not phase_config.get('enabled', False):
            return {'dt_max': 0.0, 'dlat_max': 0.0, 'dlon_max': 0.0, 'dfl_max': 0}
        
        return {
            'dt_max': phase_config.get('dt_max', 0.0),
            'dlat_max': phase_config.get('dlat_max', 0.0),
            'dlon_max': phase_config.get('dlon_max', 0.0),
            'dfl_max': phase_config.get('dfl_max', 0)
        }

    for acid, plist in pts.items():
        plist.sort(key=lambda r: r['seq'])

        # Check if this flight should be jittered (for global jitter)
        apply_global_jitter = _should_jitter(acid)

        last_t: Optional[float] = None
        for i, p in enumerate(plist):
            # Determine jitter parameters
            if STATE.phase_jitter_enabled:
                # Use phase-based jitter with trajectory context
                # Try to get track name for this aircraft
                track_name = STATE.aircraft_to_track.get(acid)
                phase = _get_flight_phase(p['fl'], plist, i, track_name)
                jitter_params = _get_phase_jitter_params(phase)
                apply_jitter = jitter_params['dt_max'] > 0 or jitter_params['dlat_max'] > 0 or jitter_params['dlon_max'] > 0 or jitter_params['dfl_max'] > 0
            elif apply_global_jitter:
                # Use global jitter parameters
                jitter_params = {
                    'dt_max': STATE.dt_max,
                    'dlat_max': STATE.dlat_max,
                    'dlon_max': STATE.dlon_max,
                    'dfl_max': STATE.dfl_max
                }
                apply_jitter = True
            else:
                apply_jitter = False
                jitter_params = {'dt_max': 0.0, 'dlat_max': 0.0, 'dlon_max': 0.0, 'dfl_max': 0}

            # Apply jitter if enabled
            if apply_jitter:
                p['t'] = max(0.0, p['t'] + _draw_noise(rng, jitter_params['dt_max'], dist, nsig))
                if last_t is not None:
                    p['t'] = max(p['t'], last_t)
                p['lat'] += _draw_noise(rng, jitter_params['dlat_max'], dist, nsig)
                p['lon'] += _draw_noise(rng, jitter_params['dlon_max'], dist, nsig)
                p['fl'] = max(0, int(round(p['fl'] + _draw_noise(rng, float(jitter_params['dfl_max']), dist, nsig))))
                last_t = p['t']
            else:
                # Ensure time ordering even without jitter
                if last_t is not None:
                    p['t'] = max(p['t'], last_t)
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

    # Use optimized TraffixGen loading system for large files
    try:
        from . import traffixgen
        
        # Find flights and points files
        flights_file = None
        points_files = []
        
        for p in paths:
            # Check headers to determine file type
            kind, _ = _read_csv_auto(p)
            if kind == 'flights':
                flights_file = p
            elif kind == 'points':
                points_files.append(p)
        
        if not flights_file:
            return False, "No flights CSV file found (must have ECTRL ID, ADEP, ADES, AC Type columns)."
        if not points_files:
            return False, "No flight points CSV file found (must have ECTRL ID, Time Over, Latitude, Longitude columns)."
        
        # Use TraffixGen optimized loading (supports all our optimizations)
        print("Using optimized loading system for Realistic Replay...")
        success = traffixgen.traffixgen_load_eurocontrol(
            flights_file, 
            points_files[0],  # Filed points 
            points_files[1] if len(points_files) > 1 else points_files[0],  # Actual points
            ""  # No FIR file
        )
        
        if not success:
            return False, "Failed to load data using optimized system."
        
        # Convert loaded data to SATG format  
        success_export = traffixgen.traffixgen_export_to_satg()
        if success_export:
            return True, f"Loaded data using optimized system with all performance improvements."
        else:
            # Fallback to old method if export fails
            flights_rows: List[dict] = []; points_rows: List[dict] = []
            found_flights = found_points = False
            for p in paths:
                kind, rows = _read_csv_auto(p)
                if kind == 'flights': flights_rows.extend(rows); found_flights = True
                elif kind == 'points': points_rows.extend(rows); found_points = True
            if not (found_flights and found_points):
                return False, "Missing required files: need both flights and flights_points (by headers)."
                
    except Exception as e:
        print(f"Optimized loading failed: {e}, falling back to original method...")
        # Fallback to original method
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
    Generate a unique aircraft ID by avoiding conflicts with used set.
    
    Strategy:
    1. If base not in used -> return base
    2. For airline callsigns (e.g., EZY123, BAW456) -> append _2, _3, etc.
    3. For sequential IDs (e.g., ABC01, TEST02) -> increment preserving width
    
    The distinction is made by checking if the trailing digits are likely
    a flight number (3+ digits) vs a sequential ID (1-2 digits with leading zeros).
    """
    if base not in used:
        return base
    
    # Check if it ends with digits
    m = re.match(r"^(.*?)(\d+)$", base)
    if m:
        root, num = m.group(1), m.group(2)
        
        # Heuristic: if 3+ digits or doesn't have leading zeros, treat as airline callsign
        # Examples: EZY123, BAW456, AFR789 -> append _2, _3
        # Counter-examples: ABC01, TEST02 -> increment to ABC02, TEST03
        if len(num) >= 3 or not num.startswith('0'):
            # Treat as airline callsign - append _2, _3, etc.
            n = 2
            while True:
                cand = f"{base}_{n}"
                if cand not in used:
                    return cand
                n += 1
        else:
            # Treat as sequential ID - increment preserving width
            width = len(num)
            n = int(num)
            while True:
                n += 1
                cand = f"{root}{str(n).zfill(width)}"
                if cand not in used:
                    return cand
    
    # No trailing digits: use _2, _3, ...
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
    footer_comments = []

    for idx, ln in enumerate(lines):
        # Keep scenario header comments at the top (comments that come before any timestamped commands)
        if ln.strip().startswith("#"):
            # If we haven't seen any timestamped commands yet, it's a header comment
            if not stamped:
                header.append(ln)
                continue
            else:
                # Comments after timestamped commands go to footer
                footer_comments.append((idx, ln))
                continue
        
        # Keep classic header lines (HOLD/ASAS) as-is at the top
        if ln.strip().startswith("0:") and (">HOLD" in ln or ">ASAS ON" in ln):
            header.append(ln)
            continue
            
        t = _parse_ts(ln)
        if t is None:
            footer_comments.append((idx, ln))  # blank lines / stray lines go to footer
        else:
            stamped.append((t, idx, ln))  # stable by (time, original order)

    stamped.sort(key=lambda x: (x[0], x[1]))  # time asc, stable on original index

    out = []
    out.extend(header)
    out.extend([ln for _, _, ln in stamped])
    # Keep any non-timestamp lines at the very end in their original relative order
    out.extend([ln for _, ln in footer_comments])

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
    """
    Write Realistic Replay scenario file with comprehensive flight trajectory data.
    
    This function generates BlueSky-compatible scenario files from loaded flight
    trajectory data, incorporating jitter variations, auto-delete configurations,
    and proper scenario formatting. The function handles both new scenario creation
    and appending to existing scenarios with collision detection and resolution.
    
    The scenario generation process includes:
    1. Scenario header generation with configuration metadata
    2. BlueSky simulation initialization commands
    3. Flight trajectory data processing and formatting
    4. Aircraft ID collision detection and resolution for append mode
    5. Proper timestamp formatting and command sequencing
    
    Generated scenarios include complete flight operations with:
    - Aircraft creation commands with proper positioning
    - Route assignments and waypoint sequences
    - Altitude and speed profiles throughout flight phases
    - Timing synchronization for realistic traffic patterns
    
    Args:
        out_path (str): Output file path for the generated scenario file
        append (bool, optional): If True, append to existing file with collision
                               avoidance. If False, create new file. Defaults to False
    
    Returns:
        None: Writes scenario data directly to the specified output file
    
    Raises:
        IOError: When output file cannot be created or written
        ValueError: When flight data is invalid or incomplete
        Exception: For other scenario generation errors
    
    Examples:
        # Create new realistic replay scenario
        _write_rl_scn("morning_rush.scn", append=False)
        
        # Append additional traffic to existing scenario
        _write_rl_scn("busy_airspace.scn", append=True)
    
    Note:
        The function automatically handles aircraft ID conflicts when appending
        to existing scenarios by renaming colliding callsigns with systematic
        suffixes. All scenario commands are properly timestamped and formatted
        for BlueSky simulation compatibility with realistic traffic timing.
    """
    mode = "a" if append else "w"
    with open(out_path, mode, encoding="utf-8") as f:
        if not append:
            # Write scenario header
            header = _generate_scenario_header("Realistic Replay",
                jitter_enabled="Yes" if STATE.jitter_on else "No",
                autodel_enabled="Yes" if STATE.autodel else "No"
            )
            for line in header:
                f.write(f"{line}\n")
            
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")
        points = _get_points_for_run()

        # When appending, avoid duplicate callsigns by renaming colliding ACIDs
        used = _scan_existing_acids(out_path) if append else set()
        name_map = {}  # original_acid -> new_acid

        # Compute a deterministic mapping for this batch, considering both ACIDs and callsigns
        
        for acid in STATE.flights.keys():
            meta = STATE.flights[acid]
            # Prefer callsign from metadata if available, otherwise use acid
            preferred_name = meta.get('Callsign', acid) if meta.get('Callsign') else acid
            
            # Ensure the preferred name is unique
            final_name = preferred_name
            if final_name in used or final_name in name_map.values():
                final_name = _next_unique_acid(preferred_name, used | set(name_map.values()))
            
            name_map[acid] = final_name
            used.add(final_name)

        # Generate aircraft using the pre-computed unique name mapping
        for acid, meta in STATE.flights.items():
            # Use the pre-computed unique name from name_map
            acid_out = name_map.get(acid, acid)

            if acid not in points or not points[acid]:
                continue
            
            segs = points[acid]; r0 = segs[0]; last = segs[-1]
            t0 = timedelta(seconds=r0['t']); stamp0 = _stamp(t0)
            fl0, lat0, lon0 = r0['fl'], r0['lat'], r0['lon']
            cas0 = _gs_to_cas_kt(r0['gs'], fl0)
            
            # Calculate initial heading from first to third waypoint (skip duplicate second)
            if len(segs) > 2:
                # Use third waypoint since second is often duplicate
                target_point = segs[2]
                lat1, lon1 = target_point['lat'], target_point['lon']
                # Calculate bearing from lat0,lon0 to lat1,lon1
                lat0_rad = math.radians(lat0)
                lat1_rad = math.radians(lat1)
                dlon_rad = math.radians(lon1 - lon0)
                
                y = math.sin(dlon_rad) * math.cos(lat1_rad)
                x = math.cos(lat0_rad) * math.sin(lat1_rad) - math.sin(lat0_rad) * math.cos(lat1_rad) * math.cos(dlon_rad)
                bearing_rad = math.atan2(y, x)
                bearing_deg = math.degrees(bearing_rad)
                hdg0 = int((bearing_deg + 360) % 360)  # Normalize to 0-359
            elif len(segs) > 1:
                # Fallback to second waypoint if no third available
                next_point = segs[1]
                lat1, lon1 = next_point['lat'], next_point['lon']
                # Calculate bearing from lat0,lon0 to lat1,lon1
                lat0_rad = math.radians(lat0)
                lat1_rad = math.radians(lat1)
                dlon_rad = math.radians(lon1 - lon0)
                
                y = math.sin(dlon_rad) * math.cos(lat1_rad)
                x = math.cos(lat0_rad) * math.sin(lat1_rad) - math.sin(lat0_rad) * math.cos(lat1_rad) * math.cos(dlon_rad)
                bearing_rad = math.atan2(y, x)
                bearing_deg = math.degrees(bearing_rad)
                hdg0 = int((bearing_deg + 360) % 360)  # Normalize to 0-359
            else:
                hdg0 = int(r0['hdg']) if not math.isnan(r0['hdg']) else 0
            
            actype = meta.get('AC Type',''); alt_ft0 = int(fl0) * 100

            f.write(f"{stamp0}CRE {acid_out},{actype},{lat0:.6f},{lon0:.6f},{hdg0:03d},{alt_ft0},{cas0:.1f}\n")

            last_is_landing = int(last['fl']) == 0
            # Auto-delete logic: always delete landing aircraft, or when checkbox is enabled for others
            trigger_on_last = STATE.autodel or last_is_landing  # Always delete landing aircraft

            pen_wptname = None; last_wptname = None
            if trigger_on_last:
                last_wptname = _sanitize_name(f"{acid_out}_DEST")
                f.write(f"{stamp0}DEFWPT {last_wptname},{last['lat']:.6f},{last['lon']:.6f}\n")
            if last_is_landing and len(segs) >= 2:
                pen = segs[-2]
                pen_wptname = _sanitize_name(f"{acid_out}_APP")
                f.write(f"{stamp0}DEFWPT {pen_wptname},{pen['lat']:.6f},{pen['lon']:.6f}\n")

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

            # Robust takeoff logic - find first non-zero altitude waypoint for realistic initial conditions
            if cas0 <= 0 or alt_ft0 <= 0:
                
                # Find first waypoint with non-zero altitude (handles varying numbers of ground waypoints)
                first_airborne_waypoint = None
                first_airborne_index = -1
                
                for idx, waypoint in enumerate(segs):
                    wp_fl = waypoint.get('fl', 0)
                    if wp_fl > 0:  # Found first waypoint above ground
                        first_airborne_waypoint = waypoint
                        first_airborne_index = idx
                        break
                
                if first_airborne_waypoint:
                    try:
                        # Use the first airborne waypoint for realistic initial climb conditions
                        target_gs = first_airborne_waypoint.get('gs', 0)
                        target_fl = first_airborne_waypoint.get('fl', 0)
                        target_cas = _gs_to_cas_kt(target_gs, target_fl)
                        
                        # Set realistic takeoff/climb conditions
                        if cas0 <= 0:
                            if target_cas > 0:
                                # Use actual climb speed from data
                                initial_speed = min(max(target_cas, 160), 250)  # Realistic takeoff/climb speed range
                                f.write(f"{stamp0}SPD {acid_out} {initial_speed:.0f}\n")
                            else:
                                # Use realistic takeoff speed
                                initial_speed = 180  # Typical takeoff/initial climb speed
                                f.write(f"{stamp0}SPD {acid_out} {initial_speed}\n")
                                
                        if alt_ft0 <= 0:
                            if target_fl > 0:
                                # Use the first climbing altitude as target
                                f.write(f"{stamp0}ALT {acid_out} FL{target_fl:03.0f}\n")
                            else:
                                # Use realistic initial climb altitude
                                initial_alt = "FL050"  # Typical initial climb clearance
                                f.write(f"{stamp0}ALT {acid_out} {initial_alt}\n")
                                
                    except Exception as e:
                        # Use realistic takeoff defaults as fallback
                        if cas0 <= 0:
                            takeoff_speed = 180
                            f.write(f"{stamp0}SPD {acid_out} {takeoff_speed}\n")
                        if alt_ft0 <= 0:
                            takeoff_alt = "FL050"
                            f.write(f"{stamp0}ALT {acid_out} {takeoff_alt}\n")
                else:
                    # All waypoints are at ground level - use realistic takeoff values
                    if cas0 <= 0:
                        takeoff_speed = 180  # Realistic takeoff speed
                        f.write(f"{stamp0}SPD {acid_out} {takeoff_speed}\n")
                    if alt_ft0 <= 0:
                        takeoff_alt = "FL050"  # Realistic initial climb clearance
                        f.write(f"{stamp0}ALT {acid_out} {takeoff_alt}\n")

            # Write LNAV/VNAV commands - delay only for takeoff aircraft (ground start)
            if alt_ft0 <= 0:
                # Aircraft starts on ground (takeoff) - apply 30 second delay
                t0_plus_30 = timedelta(seconds=r0['t'] + 30)
                stamp_lnav_vnav = _stamp(t0_plus_30)
            else:
                # Aircraft starts airborne - no delay needed
                stamp_lnav_vnav = stamp0
            
            f.write(f"{stamp_lnav_vnav}LNAV {acid_out} ON\n")
            f.write(f"{stamp_lnav_vnav}VNAV {acid_out} ON\n")
            
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
    """
    Generate uniformly distributed random value within specified range.
    
    This utility function provides consistent random value generation within
    a specified range using the provided random number generator. The function
    handles edge cases where minimum and maximum values are equal, returning
    the constant value directly without random generation overhead.
    
    The function supports both integer and floating-point ranges with uniform
    distribution characteristics suitable for parameter sampling in scenario
    generation, ensuring consistent statistical properties across different
    random seeds and generator instances.
    
    Args:
        rng (random.Random): Random number generator instance for consistent sampling
        lo (float): Lower bound of the range (inclusive)
        hi (float): Upper bound of the range (inclusive)
    
    Returns:
        float: Uniformly distributed random value between lo and hi (inclusive)
               or lo if lo equals hi (constant value case)
    
    Examples:
        # Generate random airspeed within operational range
        rng = random.Random(12345)
        speed = _rand_in(rng, 250.0, 450.0)  # Returns value in [250, 450]
        
        # Handle constant value case
        fixed_alt = _rand_in(rng, 35000, 35000)  # Returns 35000 directly
        
        # Generate random angle for conflict geometry
        angle = _rand_in(rng, 0.0, 180.0)  # Returns value in [0, 180]
    
    Note:
        The function uses uniform distribution which is appropriate for most
        scenario generation parameters where all values within the range are
        equally likely. For specialized distributions (normal, exponential),
        use appropriate generator methods directly on the rng instance.
    """
    return lo if lo == hi else rng.uniform(lo, hi)

def _gc_sample(seed: Optional[int]):
    """
    Generate randomized aircraft parameters for geometric conflict scenarios.
    
    This function samples realistic aircraft operational parameters using
    configurable random distributions to create diverse conflict scenarios
    for training and testing purposes. The sampling includes speed profiles,
    flight levels, initial bearings, and conflict angles based on realistic
    operational constraints and statistical distributions from real traffic data.
    
    The sampling process generates:
    1. Calibrated airspeed (CAS) values for both aircraft within operational ranges
    2. Flight level assignments considering separation requirements and airspace
    3. Initial bearing calculations for trajectory planning and conflict setup
    4. Conflict angle determination for crossing, head-on, and overtaking scenarios
    5. Consistent randomization using optional seed for reproducible scenarios
    
    Sampled parameters follow realistic distributions:
    - CAS values: Configured range based on aircraft performance and flight phase
    - Flight levels: Configured range considering typical cruise altitudes
    - Bearings: 0-360 degrees with uniform distribution for approach directions
    - Conflict angles: Configured range with emphasis on operationally significant cases
    
    Args:
        seed (Optional[int]): Random seed for reproducible parameter generation.
                            If None, uses system entropy for random sampling
    
    Returns:
        Tuple: Generated parameters including (rng, cas1, cas2, fl1, fl2, brg1, angle):
        - rng: Python random generator for additional sampling
        - cas1, cas2: Calibrated airspeed for aircraft 1 and 2 (knots)
        - fl1, fl2: Flight levels for aircraft 1 and 2 (flight level * 100 feet)
        - brg1: Initial bearing for aircraft 1 trajectory (degrees)
        - angle: Conflict crossing angle between aircraft trajectories (degrees)
    
    Examples:
        # Generate random parameters for conflict scenario
        rng, cas1, cas2, fl1, fl2, brg1, angle = _gc_sample(None)
        print(f"Aircraft 1: {cas1} knots at FL{fl1}, bearing {brg1}°")
        print(f"Aircraft 2: {cas2} knots at FL{fl2}")
        print(f"Conflict angle: {angle}°")
        
        # Generate reproducible parameters for testing
        rng, cas1, cas2, fl1, fl2, brg1, angle = _gc_sample(12345)
        # Same parameters will be generated with seed=12345
    
    Note:
        The function uses realistic operational constraints from STATE.gc_ranges
        configuration to ensure generated scenarios are achievable and representative
        of actual air traffic conflicts. Parameters are sampled from configured
        ranges to maintain training scenario authenticity and operational realism.
    """
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
    """
    Write Geometric Conflict scenario file with comprehensive conflict trajectory data.
    
    This function generates BlueSky-compatible scenario files from geometric conflict
    configurations, creating structured conflict scenarios with precise aircraft
    positioning, timing, and trajectory management. The function handles multiple
    conflict parameters including CPA positioning, timing variations, altitude
    offsets, and aircraft type selection with comprehensive geometric validation.
    
    The scenario generation process includes:
    1. Conflict geometry validation and parameter verification
    2. Aircraft trajectory calculation with CPA timing precision
    3. BlueSky simulation initialization with conflict-specific settings
    4. Multi-parameter randomization for realistic conflict variations
    5. Proper scenario timing and synchronization for training effectiveness
    
    Generated scenarios feature complete conflict operations with:
    - Precise aircraft positioning for guaranteed conflicts at specified CPA
    - Calculated trajectory intersections with configurable timing accuracy
    - Speed and altitude profiles optimized for conflict execution
    - Randomized parameters within specified ranges for training variation
    - Aircraft type selection from configured type pools
    - Polygon constraint integration for airspace boundary enforcement
    
    Args:
        out_path (str): Output file path for the generated scenario file
        append (bool): If True, append to existing file. If False, create new file
        name (str): Scenario name identifier for metadata and organization
        cpa_lat (float): Latitude coordinate of the Closest Point of Approach (degrees)
        cpa_lon (float): Longitude coordinate of the Closest Point of Approach (degrees)
        tcpa_value (Optional[float]): Fixed time to CPA in seconds, or None for range
        tcpa_range (Optional[Tuple[float, float]]): Time to CPA range (min, max) seconds
        fl_cpa (Optional[int]): Flight level at CPA, or None for default sampling
        acid1 (str): Aircraft ID for first aircraft in conflict
        acid2 (str): Aircraft ID for second aircraft in conflict
        ac1 (str): Aircraft callsign/identifier for first aircraft
        ac2 (str): Aircraft callsign/identifier for second aircraft
        ac_types (Optional[List[str]]): Aircraft type pool for selection, or None for default
        seed (Optional[int]): Random seed for reproducible parameter generation
        angle_in (Optional[float]): Fixed conflict angle in degrees, or None for range
        angle_range (Optional[Tuple[float, float]]): Conflict angle range (min, max) degrees
        alt_offset_value (Optional[float]): Fixed altitude offset in feet, or None for range
        alt_offset_range (Optional[Tuple[float, float]]): Altitude offset range (min, max) feet
        polygon_commands (Optional[List[str]]): Airspace polygon constraint commands
    
    Returns:
        None: Writes scenario data directly to the specified output file
    
    Raises:
        IOError: When output file cannot be created or written
        ValueError: When conflict configuration parameters are invalid or incomplete
        GeometryError: When conflict geometry calculations fail validation
        Exception: For other scenario generation errors
    
    Examples:
        # Create fixed-parameter head-on conflict
        _write_gc_scn("head_on.scn", append=False, name="HeadOn_Test",
                     cpa_lat=52.3, cpa_lon=4.8, tcpa_value=300.0, fl_cpa=350,
                     acid1="KLM001", acid2="UAL002", ac1="A320", ac2="B737")
        
        # Create randomized crossing conflict with variations
        _write_gc_scn("crossing_var.scn", append=True, name="Crossing_Random",
                     cpa_lat=51.5, cpa_lon=0.1, tcpa_range=(240, 360),
                     angle_range=(60, 120), alt_offset_range=(-1000, 1000),
                     acid1="BAW003", acid2="AFR004", ac1="B777", ac2="A350")
    
    Note:
        The function validates all conflict geometry before scenario generation
        to ensure realistic and achievable conflict scenarios. CPA calculations
        include safety margins and operational constraints for training realism.
        Random parameter generation uses specified seeds for reproducible scenarios
        when needed for training consistency and evaluation purposes.
    """
    
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
            # r * v_rel = 0 at CPA for minimum distance -> rotate v_rel by 90 deg
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
            # Write scenario header
            header = _generate_scenario_header("Geometric Conflicts",
                lat=cpa_lat,
                lon=cpa_lon,
                tcpa=tcpa_value or f"{tcpa_range[0]}-{tcpa_range[1]}" if tcpa_range else "N/A",
                angle=angle_in or f"{angle_range[0]}-{angle_range[1]}" if angle_range else "N/A",
                actypes=", ".join(ac_types) if ac_types else f"{ac1}, {ac2}",
                altmode="Mixed" if alt_offset_value or alt_offset_range else "Level"
            )
            for line in header:
                f.write(f"{line}\n")
            
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


def _proc_pick_actype(proc_type: str, rng: random.Random) -> str:
    """Pick a random aircraft type for the specified procedure type."""
    if proc_type == "generic":
        types = STATE.proc_generic_actypes
    elif proc_type == "sid":
        types = STATE.proc_sid_actypes
    elif proc_type == "star":
        types = STATE.proc_star_actypes
    else:
        types = ["A320"]  # Fallback
    
    # Clean and filter types
    clean_types = [str(t).strip().upper() for t in types if str(t).strip()]
    if not clean_types:
        return "A320"  # Fallback
    
    if len(clean_types) == 1:
        return clean_types[0]
    
    return rng.choice(clean_types)


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


def _gc_rel_write_scn(path: str, *, append: bool, lines: List[str], **header_params) -> None:
    stamp0 = _stamp(timedelta(seconds=0.0))
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as f:
        if not append:
            # Write scenario header
            header = _generate_scenario_header("Geometric Conflicts",
                **header_params
            )
            for line in header:
                f.write(f"{line}\n")
            
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
    
    # Handle both coordinate format and waypoint name format
    lines = txt.split('\n')
    for line_num, line in enumerate(lines):
        if 'ADDWPT' in line:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == 'ADDWPT':
                # Check if third argument (after ADDWPT) is a coordinate (contains decimal point)
                try:
                    float(parts[2])  # If this succeeds, it's a coordinate
                    # Generate a waypoint name for coordinate-based format
                    wp_name = f"WP{len(seq)+1:02d}"
                    if not seq or seq[-1] != wp_name:
                        seq.append(wp_name)
                except ValueError:
                    # Traditional waypoint name format
                    # Use original regex for waypoint names
                    match = re.search(r"ADDWPT\s+([A-Za-z0-9_+\-/]+)", line, re.IGNORECASE)
                    if match:
                        name = match.group(1).strip().upper()
                        if name and (not seq or seq[-1] != name):
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


def _proc_first_two_coordinates(proc_path: str) -> tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    """Extract the first two coordinates directly from coordinate-based procedure files."""
    coordinates = _proc_all_coordinates(proc_path)
    coord1 = coordinates[0] if len(coordinates) >= 1 else None
    coord2 = coordinates[1] if len(coordinates) >= 2 else None
    return coord1, coord2


def _proc_last_coordinate(proc_path: str) -> Optional[Tuple[float, float]]:
    """Extract the last coordinate directly from coordinate-based procedure files."""
    coordinates = _proc_all_coordinates(proc_path)
    return coordinates[-1] if coordinates else None


def _proc_extract_initial_alt_spd(proc_path: str) -> Tuple[Optional[int], Optional[float]]:
    """Extract initial altitude (feet) and speed (knots or Mach) from procedure file.
    
    Returns:
        Tuple of (altitude_feet, speed_value):
        - altitude_feet: int altitude in feet, or None if not specified
        - speed_value: float speed in knots, or Mach number if M prefix, or None if not specified
    """
    try:
        with open(proc_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return None, None
    
    lines = txt.split('\n')
    for line in lines:
        if 'ADDWPT' in line:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == 'ADDWPT':
                # First ADDWPT line found - check for altitude and speed
                # Format: timestamp>id ADDWPT lat lon [altitude] [speed]
                altitude_ft = None
                speed_val = None
                
                # Check for altitude (5th parameter, index 4)
                if len(parts) >= 5 and parts[4] not in ['', ',,']:
                    alt_str = parts[4].strip()
                    try:
                        if alt_str.upper().startswith('FL'):
                            # Flight level format: FL100 -> 10000 feet
                            fl = int(alt_str[2:])
                            altitude_ft = fl * 100
                        else:
                            # Direct feet format: 1000 -> 1000 feet
                            altitude_ft = int(alt_str)
                    except ValueError:
                        pass
                
                # Check for speed (6th parameter, index 5)
                if len(parts) >= 6 and parts[5] not in ['', ',,']:
                    spd_str = parts[5].strip()
                    try:
                        if spd_str.upper().startswith('M'):
                            # Mach format: M0.7 -> 0.7
                            speed_val = float(spd_str[1:])
                        else:
                            # Knots format: 210 -> 210
                            speed_val = float(spd_str)
                    except ValueError:
                        pass
                
                return altitude_ft, speed_val
    
    return None, None


def _proc_all_coordinates(proc_path: str) -> List[Tuple[float, float]]:
    """Extract all coordinates from coordinate-based procedure files."""
    try:
        with open(proc_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return []
    
    coordinates = []
    lines = txt.split('\n')
    for line in lines:
        if 'ADDWPT' in line:
            parts = line.strip().split()
            if len(parts) >= 4 and parts[1] == 'ADDWPT':
                # Check if third argument (after ADDWPT) is a coordinate (contains decimal point)
                try:
                    lat = float(parts[2])
                    lon = float(parts[3])
                    coordinates.append((lat, lon))
                except (ValueError, IndexError):
                    # Not a coordinate format, skip
                    continue
    return coordinates


def _proc_is_coordinate_based(proc_path: str) -> bool:
    """Check if a procedure file uses coordinate-based format (vs waypoint names)."""
    return len(_proc_all_coordinates(proc_path)) > 0


def _proc_unified_first_waypoint(proc_path: str, fix_db: Optional[Dict[str, Tuple[float, float]]] = None) -> Optional[Tuple[float, float]]:
    """Get first waypoint coordinates, handling both coordinate-based and waypoint-based procedures."""
    # Always get the FIRST waypoint from the procedure file, regardless of format
    try:
        with open(proc_path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return None
    
    # First pass: try to establish geographical context from known waypoints in the procedure
    ref_lat, ref_lon = None, None
    known_waypoints = ["EHAM", "EGLL", "LFPG", "EDDF", "LEMD", "LIRF", "LOWW"]  # Major European airports
    
    lines = txt.split('\n')
    for line in lines:
        if 'ADDWPT' in line:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == 'ADDWPT':
                waypoint_name = parts[2]
                if waypoint_name in known_waypoints:
                    # Found a known waypoint - use it for context
                    context_coord = _resolve_fix_coord(waypoint_name, fix_db, proc_path=proc_path)
                    if context_coord:
                        ref_lat, ref_lon = context_coord
                        break
    
    # Second pass: find the actual first waypoint with established context
    for line in lines:
        if 'ADDWPT' in line:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == 'ADDWPT':
                # Check if this is a coordinate-based waypoint
                try:
                    lat = float(parts[2])
                    lon = float(parts[3]) if len(parts) >= 4 else 0.0
                    return (lat, lon)
                except (ValueError, IndexError):
                    # This is a named waypoint - resolve it with context
                    waypoint_name = parts[2]
                    return _resolve_fix_coord(waypoint_name, fix_db, ref_lat, ref_lon, proc_path=proc_path)
    
    return None


def _proc_unified_last_waypoint(proc_path: str, fix_db: Optional[Dict[str, Tuple[float, float]]] = None) -> Optional[Tuple[float, float]]:
    """Get last waypoint coordinates, handling both coordinate-based and waypoint-based procedures."""
    # Try coordinate-based first
    coord = _proc_last_coordinate(proc_path)
    if coord:
        return coord
    
    # Fall back to waypoint-based resolution
    fix_names = _proc_fix_sequence(proc_path)
    if fix_names:
        return _resolve_fix_coord(fix_names[-1], fix_db, proc_path=proc_path)
    
    return None


def _proc_unified_waypoint_token(proc_path: str, is_first: bool = True) -> Optional[str]:
    """Get waypoint token for AT commands, handling both coordinate-based and waypoint-based procedures."""
    if _proc_is_coordinate_based(proc_path):
        # For coordinate-based, use coordinates directly
        if is_first:
            coord = _proc_first_two_coordinates(proc_path)[0]
        else:
            coord = _proc_last_coordinate(proc_path)
        
        if coord:
            lat, lon = coord
            return f"{lat:.6f},{lon:.6f}"
    else:
        # For waypoint-based, use waypoint names
        fix_names = _proc_fix_sequence(proc_path)
        if fix_names:
            fix_name = fix_names[0] if is_first else fix_names[-1]
            return fix_name.upper()
    
    return None


def _proc_last_fix(proc_path: str) -> Optional[str]:
    seq = _proc_fix_sequence(proc_path)
    if not seq:
        return None
    return seq[-1]

def _resolve_waypoint_pair(name1: str, name2: str, fix_db: Optional[Dict[str, Tuple[float, float]]]) -> tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    """
    Resolve a pair of waypoints trying to find geographically close matches.
    Returns (coord1, coord2) for the best pair, or (None, None) if no good pair found.
    """
    if not name1 or not name2:
        return None, None
        
    navdb = getattr(bs, "navdb", None) if bs else None
    if not navdb:
        return None, None
    
    try:
        # Get all possible coordinates for both waypoints
        coords1 = []
        coords2 = []
        
        # Check fix_db first
        key1 = name1.strip().upper()
        key2 = name2.strip().upper()
        if fix_db:
            if key1 in fix_db:
                coords1.append(fix_db[key1])
            if key2 in fix_db:
                coords2.append(fix_db[key2])
        
        # Get additional coordinates from navigation database
        wpid_list = getattr(navdb, "wpid", None)
        wplat_list = getattr(navdb, "wplat", None)
        wplon_list = getattr(navdb, "wplon", None)
        
        if wpid_list is not None and wplat_list is not None and wplon_list is not None:
            # Find all instances of name1
            for i, wpid in enumerate(wpid_list):
                if wpid == key1:
                    coords1.append((float(wplat_list[i]), float(wplon_list[i])))
                elif wpid == key2:
                    coords2.append((float(wplat_list[i]), float(wplon_list[i])))
        
        if not coords1 or not coords2:
            return None, None
        
        # Find the pair with minimum distance
        best_pair = None
        min_distance = float('inf')
        
        from bluesky.tools import geo
        if hasattr(geo, "qdrdist"):
            for coord1 in coords1:
                for coord2 in coords2:
                    _, dist_nm = geo.qdrdist(coord1[0], coord1[1], coord2[0], coord2[1])
                    if dist_nm < min_distance:
                        min_distance = dist_nm
                        best_pair = (coord1, coord2)
        
        if best_pair and min_distance < 1000:  # Only accept pairs within 1000 NM
            return best_pair
        else:
            return None, None
            
    except Exception as e:
        return None, None


def _build_proc_coord_db(proc_path: str) -> Dict[str, Tuple[float, float]]:
    """Build coordinate database from procedure file for coordinate-based ADDWPT commands."""
    coord_db = {}
    try:
        with open(proc_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        lines = content.split('\n')
        wp_index = 1
        
        for line in lines:
            if 'ADDWPT' in line:
                parts = line.strip().split()
                if len(parts) >= 4 and parts[1] == 'ADDWPT':
                    try:
                        lat = float(parts[2])
                        lon = float(parts[3])
                        wp_name = f"WP{wp_index:02d}"
                        coord_db[wp_name] = (lat, lon)
                        wp_index += 1
                    except ValueError:
                        # Not coordinate format, skip
                        pass
    except Exception:
        pass
    
    return coord_db


def _get_defwpt_coordinate(waypoint_name: str, proc_path: str) -> Optional[Tuple[float, float]]:
    """Get coordinates for a waypoint from DEFWPT definitions in procedure file."""
    try:
        with open(proc_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        lines = content.split('\n')
        target_name = waypoint_name.upper()
        
        for line in lines:
            if 'DEFWPT' in line.upper():
                parsed = _parse_defwpt_line(line)
                if parsed:
                    name, lat, lon = parsed
                    if name.upper() == target_name:
                        return (lat, lon)
    except Exception:
        pass
    
    return None


def _resolve_fix_coord(name: Optional[str],
                       fix_db: Optional[Dict[str, Tuple[float, float]]],
                       ref_lat: Optional[float] = None,
                       ref_lon: Optional[float] = None,
                       proc_path: Optional[str] = None) -> Optional[Tuple[float, float]]:
    if not name:
        return None
    key = str(name).strip().upper()
    if not key:
        return None
    
    # PRIORITY 1: Check DEFWPT definitions in procedure file first
    if proc_path:
        defwpt_coord = _get_defwpt_coordinate(key, proc_path)
        if defwpt_coord:
            if fix_db is not None:
                fix_db[key] = defwpt_coord
            return defwpt_coord
    
    # PRIORITY 2: Check legacy coordinate-based waypoint format 
    if proc_path and key.startswith("WP") and len(key) == 4:
        proc_coord_db = _build_proc_coord_db(proc_path)
        if key in proc_coord_db:
            coord = proc_coord_db[key]
            if fix_db is not None:
                fix_db[key] = coord
            return coord
    
    # PRIORITY 3: Check the provided fix database
    if fix_db is not None and key in fix_db:
        return fix_db[key]
    
    # Finally try navigation database lookup
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
    """
    Show or set the SATG base directory with automatic subdirectory creation.
    
    This command function manages the SATG working directory structure, creating
    the necessary subdirectories for data files and generated scenarios. When
    setting a new base directory, the function automatically creates 'data' and
    'scenarios' subdirectories if they don't exist.
    
    The base directory structure is essential for SATG operations:
    - <base>/data: Contains input CSV files for realistic replay
    - <base>/scenarios: Contains generated scenario files for BlueSky simulation
    
    Args:
        base (str, optional): New base directory path to set. If None, displays
                            current directory configuration without changes.
    
    Returns:
        tuple: (True, "") indicating successful execution
    
    Examples:
        # Display current directory configuration
        SATG_DIR
        
        # Set new base directory with automatic subdirectory creation
        SATG_DIR base=C:/work/satg
        
        # Set relative path base directory
        SATG_DIR base=./satg_data
    
    Note:
        Setting a new base directory automatically creates the required
        subdirectories and updates the global STATE configuration. The
        function provides helpful guidance about placing CSV files in
        the data directory for subsequent loading operations.
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
    """
    Load pre-filtered flight data for Realistic Replay scenario generation.
    
    This command function loads historical flight data that has been pre-processed
    and filtered for use in realistic replay scenarios. The function supports
    automatic file discovery in the data directory or manual specification of
    specific data files. The loaded data includes flight metadata and trajectory
    points required for scenario generation.
    
    The function expects CSV files with specific headers for flight information
    and flight points data. When using AUTO mode, it scans the configured data
    directory for appropriately formatted files and loads them automatically.
    
    Data Requirements:
    - Flights file: Contains flight metadata with proper CSV headers
    - Flights_points file: Contains trajectory data with coordinate information
    - Files must be pre-filtered and formatted according to SATG specifications
    
    Args:
        *args: Variable arguments supporting multiple input formats:
              - No args: Defaults to AUTO scanning mode
              - 'AUTO': Automatically scan <base>/data directory
              - 'files=path': Specify directory or comma-separated file paths
              - Direct file paths: Space or comma-separated file specifications
    
    Returns:
        tuple: (success, message) where success is boolean and message is string
    
    Examples:
        # Automatic scanning of data directory
        SATG_RL_LOAD
        SATG_RL_LOAD AUTO
        
        # Load from specific directory containing flights data
        SATG_RL_LOAD files=C:/data/case1
        
        # Load specific files with explicit paths
        SATG_RL_LOAD files=C:/data/flights.csv,C:/data/flights_points.csv
        
        # Direct file specification without 'files=' parameter
        SATG_RL_LOAD C:/data/flights.csv,C:/data/flights_points.csv
    
    Note:
        The function validates file headers and formats during loading to ensure
        compatibility with SATG scenario generation. Pre-filtering of data is
        expected to have been performed before loading. Files should contain
        complete flight trajectories with proper coordinate and timing information.
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
        _echo_ok(msg, nxt="Now: SATG_RL_JITTER [on|off] ... (optional), then SATG_RL_RUN [SCNNAME]")
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
    """
    Configure synthetic noise (jitter) for realistic flight trajectory variations.
    
    This command function enables and configures stochastic variations applied to
    flight trajectories during scenario generation to create realistic deviations
    from perfect flight paths. The jitter system adds controlled noise to temporal,
    spatial, and altitude parameters to simulate real-world flight operations.
    
    The jitter system supports multiple probability distributions and provides
    fine-grained control over variation parameters. This enables generation of
    realistic traffic scenarios that reflect the natural variations found in
    actual flight operations while maintaining reproducibility through seeding.
    
    Jitter Parameters:
    - Temporal: Time variations in departure and waypoint timing
    - Spatial: Latitude and longitude coordinate variations
    - Vertical: Flight level and altitude variations
    - Statistical: Distribution type and sigma clamping controls
    
    Args:
        mode (str): Enable or disable jitter ('on' or 'off')
        dist (str, optional): Probability distribution ('uniform' or 'normal').
                             Defaults to 'normal' if not specified
        seed (int, optional): Random seed for reproducible jitter patterns
        dt (float, optional): Time jitter range in seconds (+/- variance)
        dlat (float, optional): Latitude jitter range in degrees (+/- variance)
        dlon (float, optional): Longitude jitter range in degrees (+/- variance)
        dfl (int, optional): Flight level jitter range (+/- variance)
        nsig (float, optional): Sigma clamp for normal distribution (0 disables)
        pct (float, optional): Percentage of flights to apply jitter to
    
    Returns:
        tuple: (success, message) indicating configuration success
    
    Examples:
        # Enable jitter with default normal distribution
        SATG_RL_JITTER on
        
        # Configure comprehensive jitter with specific parameters
        SATG_RL_JITTER on dist=normal seed=12345 dt=30 dlat=0.01 dlon=0.01 dfl=2
        
        # Set uniform distribution with moderate variations
        SATG_RL_JITTER on dist=uniform dt=60 dlat=0.005 dlon=0.005 nsig=2.0
        
        # Disable jitter for deterministic scenarios
        SATG_RL_JITTER off
    
    Note:
        Only specified parameters are updated, allowing incremental configuration
        changes. Jitter is applied during scenario generation (MAKE/RUN) and
        affects all trajectory points. Use seeding for reproducible scenarios
        while maintaining realistic flight path variations.
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

    msg = ("Jitter ON - dist=%s: dt=%s, dlat=%s, dlon=%s, dfl=%s, pct=%.0f%%" %
        (STATE.jitter_dist, STATE.dt_max, STATE.dlat_max, STATE.dlon_max, STATE.dfl_max, STATE.jitter_pct))

    _echo_ok(msg, nxt="Now: SATG_RL_RUN [SCNNAME]")
    return True, ""

@command
def SATG_RL_AUTODEL(mode: str):
    """
    Configure automatic aircraft deletion at scenario endpoints.
    
    SATG_RL_AUTODEL mode
      mode: on|off
    Delete aircraft at last waypoint even if final FL>0 (default: ON).
    
    This command controls whether aircraft are automatically deleted when they
    reach their final waypoint in Realistic Replay scenarios, regardless of
    their final flight level. This feature ensures clean scenario termination
    and prevents aircraft from continuing indefinitely beyond their planned
    trajectory endpoints, which is essential for training scenario management.
    
    When auto-deletion is enabled:
    - Aircraft are removed upon reaching their last waypoint
    - Final flight level restrictions are ignored for deletion
    - Scenario cleanup is automated for training sessions
    - Memory usage is optimized for long-running scenarios
    
    When auto-deletion is disabled:
    - Aircraft remain active after reaching final waypoints
    - Manual deletion commands are required for cleanup
    - Useful for extended observation of aircraft behavior
    - May cause memory accumulation in long scenarios
    
    Args:
        mode (str): Configuration mode, must be "on" or "off"
                   - "on": Enable automatic deletion at last waypoint
                   - "off": Disable automatic deletion, require manual cleanup
    
    Returns:
        Tuple[bool, str]: (True, "") on success, (False, "") on invalid input
    
    Examples:
        # Enable automatic aircraft deletion (default)
        SATG_RL_AUTODEL on
        
        # Disable automatic deletion for extended observation
        SATG_RL_AUTODEL off
    
    Note:
        Auto-deletion is enabled by default to ensure proper scenario cleanup
        and optimal memory usage during training sessions. Disabling should only
        be done when extended aircraft observation is required for analysis.
    """
    m = mode.strip().lower()
    if m not in ("on","off"):
        _echo_err("Usage: SATG_RL_AUTODEL on|off"); return False, ""
    STATE.autodel = (m == "on")
    _echo_ok(f"Auto-delete at last waypoint {'ENABLED' if STATE.autodel else 'DISABLED'}",
             nxt="Now: SATG_RL_RUN [SCNNAME]")
    return True, ""

@command
def SATG_RL_PHASE_JITTER(mode: str):
    """
    Configure flight phase-based jitter system for enhanced scenario realism.
    
    SATG_RL_PHASE_JITTER mode
      mode: on|off
    Enable/disable flight phase-based jitter system.
    
    This command controls the advanced phase-based jitter system that applies
    different variation parameters depending on the current flight phase
    (initial climb, top of climb, cruise, top of descent, final approach).
    Phase-based jitter provides more realistic trajectory variations that
    reflect actual operational differences between flight phases.
    
    The phase-based system enables:
    - Flight phase-specific jitter parameters (position, timing, altitude)
    - Realistic variation patterns matching operational procedures
    - Enhanced training scenario diversity with phase-appropriate variations
    - Improved simulation fidelity for different flight segments
    - Configurable parameters per phase via SATG_RL_PHASE_CONFIG
    
    When enabled, jitter variations are applied according to:
    - Initial climb: Higher altitude and timing variations
    - Cruise: Moderate position variations with stable altitude
    - Descent: Progressive altitude changes with approach timing
    - Approach: Minimal variations for approach precision requirements
    
    Args:
        mode (str): Configuration mode, must be "on" or "off"
                   - "on": Enable phase-based jitter system
                   - "off": Disable phase-based jitter, use global settings
    
    Returns:
        Tuple[bool, str]: (True, "") on success, (False, "") on invalid input
    
    Examples:
        # Enable phase-based jitter for realistic variations
        SATG_RL_PHASE_JITTER on
        
        # Disable phase-based system, use global jitter
        SATG_RL_PHASE_JITTER off
    
    Note:
        Phase-based jitter requires proper flight phase configuration via
        SATG_RL_PHASE_CONFIG for each phase. When disabled, global jitter
        settings from SATG_RL_JITTER are used uniformly across all phases.
    """
    m = mode.strip().lower()
    if m not in ("on","off"):
        _echo_err("Usage: SATG_RL_PHASE_JITTER on|off"); return False, ""
    STATE.phase_jitter_enabled = (m == "on")
    status = "ENABLED" if STATE.phase_jitter_enabled else "DISABLED"
    _echo_ok(f"Phase-based jitter {status}",
             nxt="Use SATG_RL_PHASE_CONFIG to configure individual phases")
    return True, ""

@command  
def SATG_RL_PHASE_CONFIG(phase: str, enabled: str=None, dt: float=None, dlat: float=None, dlon: float=None, dfl: int=None):
    """
    Configure jitter parameters for specific flight phases.
    
    SATG_RL_PHASE_CONFIG phase [enabled] [dt] [dlat] [dlon] [dfl]
    Configure jitter parameters for a specific flight phase.
      phase: takeoff|climb|cruise|descent|approach
      enabled: on|off
      dt: seconds (+/- range for time)
      dlat: degrees (+/- range latitude) 
      dlon: degrees (+/- range longitude)
      dfl: flight levels (+/- range)
    
    This command enables fine-grained control over jitter parameters for
    different flight phases, allowing realistic variation patterns that
    reflect operational differences between takeoff, climb, cruise, descent,
    and approach phases. Each phase can have unique jitter characteristics
    tailored to typical operational variations in that flight segment.
    
    Phase-specific configurations enable:
    - Takeoff: Higher timing variations for departure slot flexibility
    - Climb: Altitude and position variations for traffic management
    - Cruise: Moderate position jitter with stable flight levels
    - Descent: Progressive altitude changes with approach timing coordination
    - Approach: Minimal variations to maintain approach precision
    
    Jitter parameters for each phase:
    - dt: Temporal variation range (±seconds) for waypoint timing
    - dlat/dlon: Spatial variation ranges (±degrees) for position accuracy
    - dfl: Altitude variation range (±flight levels) for level changes
    - enabled: Phase-specific enable/disable control
    
    Args:
        phase (str): Flight phase identifier (takeoff|climb|cruise|descent|approach)
        enabled (str, optional): Enable phase jitter ("on"|"off")
        dt (float, optional): Time jitter range in seconds (±)
        dlat (float, optional): Latitude jitter range in degrees (±)
        dlon (float, optional): Longitude jitter range in degrees (±)
        dfl (int, optional): Flight level jitter range (±)
    
    Returns:
        Tuple[bool, str]: (True, "") on success, (False, "") on invalid input
    
    Examples:
        # Configure climb phase with moderate variations
        SATG_RL_PHASE_CONFIG climb enabled on dt 30 dlat 0.01 dlon 0.01 dfl 2
        
        # Configure approach phase with minimal variations
        SATG_RL_PHASE_CONFIG approach enabled on dt 10 dlat 0.001 dlon 0.001 dfl 0
        
        # Disable jitter for cruise phase
        SATG_RL_PHASE_CONFIG cruise enabled off
    
    Note:
        Phase-based jitter requires SATG_RL_PHASE_JITTER to be enabled.
        Flight phase detection uses altitude thresholds and track analysis
        configured via SATG_RL_TRACK_CONFIG for accurate phase identification.
    """
    p = phase.strip().lower()
    if p not in STATE.phase_configs:
        _echo_err(f"Invalid phase '{phase}'. Valid phases: takeoff, climb, cruise, descent, approach")
        return False, ""
    
    config = STATE.phase_configs[p]
    
    if enabled is not None:
        e = enabled.strip().lower()
        if e not in ("on","off"):
            _echo_err("enabled must be 'on' or 'off'"); return False, ""
        config['enabled'] = (e == "on")
    
    if dt is not None: config['dt_max'] = float(dt)
    if dlat is not None: config['dlat_max'] = float(dlat) 
    if dlon is not None: config['dlon_max'] = float(dlon)
    if dfl is not None: config['dfl_max'] = int(dfl)
    
    status = "ENABLED" if config['enabled'] else "DISABLED"
    msg = (f"Phase '{phase}' {status} - dt={config['dt_max']}, dlat={config['dlat_max']}, "
           f"dlon={config['dlon_max']}, dfl={config['dfl_max']}")
    _echo_ok(msg)
    return True, ""

@command
def SATG_RL_PHASE_ALTITUDES(phase: str, min_fl: int=None, max_fl: int=None):
    """
    Configure altitude boundaries for flight phase detection and classification.
    
    SATG_RL_PHASE_ALTITUDES phase [min_fl] [max_fl]
    Configure altitude boundaries for flight phases.
      phase: takeoff|climb|cruise|descent|approach
      min_fl: minimum flight level
      max_fl: maximum flight level
    
    This command defines altitude boundaries used for automatic flight phase
    detection in Realistic Replay scenarios. Proper phase classification is
    essential for applying phase-specific jitter parameters and ensuring
    realistic trajectory variations that match operational flight profiles.
    
    Flight phase altitude boundaries enable:
    - Automatic phase detection based on current aircraft altitude
    - Phase-specific jitter parameter application
    - Realistic trajectory modeling for different flight segments
    - Training scenario authenticity with proper operational phases
    - Statistical analysis of phase-based performance metrics
    
    Typical altitude ranges for phase classification:
    - Takeoff: Ground level to initial climb altitude (FL000-FL100)
    - Climb: Initial climb to cruise entry (FL100-FL300)
    - Cruise: Primary cruise altitudes (FL300-FL400)
    - Descent: Cruise exit to approach entry (FL300-FL100)
    - Approach: Approach entry to landing (FL100-FL000)
    
    Args:
        phase (str): Flight phase identifier (takeoff|climb|cruise|descent|approach)
        min_fl (int, optional): Minimum flight level boundary for phase
        max_fl (int, optional): Maximum flight level boundary for phase
    
    Returns:
        Tuple[bool, str]: (True, "") on success, (False, "") on invalid input
    
    Examples:
        # Configure cruise phase altitude boundaries
        SATG_RL_PHASE_ALTITUDES cruise min_fl 300 max_fl 400
        
        # Configure approach phase for low altitudes
        SATG_RL_PHASE_ALTITUDES approach min_fl 0 max_fl 100
        
        # Set climb phase boundaries
        SATG_RL_PHASE_ALTITUDES climb min_fl 100 max_fl 300
    
    Note:
        Altitude boundaries should not overlap between phases to ensure
        unambiguous phase classification. The system uses these boundaries
        combined with track analysis for accurate flight phase detection.
    """
    p = phase.strip().lower()
    if p not in STATE.phase_altitudes:
        _echo_err(f"Invalid phase '{phase}'. Valid phases: takeoff, climb, cruise, descent, approach")
        return False, ""
    
    if min_fl is not None: STATE.phase_altitudes[p]['min_fl'] = int(min_fl)
    if max_fl is not None: STATE.phase_altitudes[p]['max_fl'] = int(max_fl)
    
    bounds = STATE.phase_altitudes[p]
    msg = f"Phase '{phase}' altitude bounds: FL{bounds['min_fl']:03d} to FL{bounds['max_fl']:03d}"
    _echo_ok(msg)
    return True, ""

@command
def SATG_RL_TRACK_CONFIG(ectrl_id: str, initial_climb: int, top_of_climb: int, top_of_descent: int, final_approach: int):
    """
    Configure track-specific altitude boundaries for flight phase detection.
    
    SATG_RL_TRACK_CONFIG ectrl_id initial_climb top_of_climb top_of_descent final_approach
    Set altitude boundaries for a specific ECTRL ID (all values in flight levels).
    
    This command enables individual flight track customization of altitude
    boundaries for flight phase detection, allowing precise phase classification
    based on each track's unique operational profile. Track-specific configuration
    overrides global phase altitude settings for enhanced accuracy in phase-based
    jitter application and realistic trajectory modeling.
    
    Track-specific configuration enables:
    - Individual flight profile optimization for phase detection
    - Accurate phase classification for diverse route types
    - Enhanced jitter parameter application based on actual flight characteristics
    - Realistic scenario generation matching specific operational procedures
    - Statistical analysis of route-specific performance patterns
    
    Flight phase boundaries define altitude transitions:
    - Takeoff phase: Ground (FL000) to initial_climb altitude
    - Climb phase: initial_climb to top_of_climb altitude
    - Cruise phase: top_of_climb to top_of_descent altitude
    - Descent phase: final_approach to top_of_descent altitude
    - Approach phase: Ground (FL000) to final_approach altitude
    
    Args:
        ectrl_id (str): EUROCONTROL track identifier for flight configuration
                       (e.g., "IBE3312", "TAP342", "KLM1234")
        initial_climb (int): Upper bound altitude for takeoff phase (flight levels)
        top_of_climb (int): Upper bound altitude for climb phase (flight levels)
        top_of_descent (int): Upper bound for cruise/start of descent (flight levels)
        final_approach (int): Upper bound altitude for approach phase (flight levels)
    
    Returns:
        Tuple[bool, str]: (True, "") on success, (False, "") on configuration error
    
    Examples:
        # Configure short-haul domestic flight profile
        SATG_RL_TRACK_CONFIG IBE3312 100 250 250 80
        
        # Configure long-haul international flight profile
        SATG_RL_TRACK_CONFIG TAP342 120 380 390 100
        
        # Configure regional jet profile with lower cruise
        SATG_RL_TRACK_CONFIG KLM1234 80 300 320 60
    
    Note:
        Track-specific configurations take precedence over global phase altitude
        settings. Altitude boundaries should reflect realistic operational profiles
        for the specific route type to ensure accurate phase detection and
        appropriate jitter parameter application during scenario generation.
    """
    try:
        # Store track-specific altitude boundaries in the expected format
        STATE.track_phase_altitudes[ectrl_id] = {
            'takeoff': {'min_fl': 0, 'max_fl': int(initial_climb)},
            'climb': {'min_fl': int(initial_climb), 'max_fl': int(top_of_climb)},
            'cruise': {'min_fl': int(top_of_climb), 'max_fl': int(top_of_descent)},
            'descent': {'min_fl': int(final_approach), 'max_fl': int(top_of_descent)},
            'approach': {'min_fl': 0, 'max_fl': int(final_approach)}
        }
        
        _echo_ok(f"Set altitude boundaries for {ectrl_id}: "
                f"Initial Climb FL{initial_climb}, Top of Climb FL{top_of_climb}, "
                f"Top of Descent FL{top_of_descent}, Final Approach FL{final_approach}")
        return True, ""
    except Exception as e:
        _echo_err(f"Error setting track configuration for {ectrl_id}: {e}")
        return False, ""

@command
def SATG_RL_MAKE(*args):
    """
    Generate Realistic Replay scenario file from loaded flight data.
    
    SATG_RL_MAKE name [overwrite] [files]
    Write <base>/scenarios/<name>.scn (scenario starts paused; ASAS ON at 0).
    If files provided, automatically load them first.
    
    This command generates complete BlueSky scenario files from previously loaded
    EUROCONTROL flight trajectory data, incorporating all configured jitter
    parameters, phase-based variations, and operational constraints. The generated
    scenarios are fully functional BlueSky simulation files ready for training
    and evaluation purposes with realistic air traffic patterns.
    
    The scenario generation process includes:
    1. Flight trajectory data processing with jitter application
    2. Aircraft creation commands with proper positioning and timing
    3. Route assignments and waypoint sequence generation
    4. Altitude and speed profile integration throughout flight phases
    5. ASAS (Airborne Separation Assurance System) initialization
    6. Proper scenario formatting for BlueSky simulation compatibility
    
    Generated scenarios feature:
    - Realistic aircraft trajectories with operational variations
    - Phase-based jitter application for enhanced training diversity
    - Proper timing synchronization for multi-aircraft scenarios
    - ASAS system activation for conflict detection training
    - Configurable aircraft deletion at trajectory endpoints
    - Complete flight operations from takeoff to landing
    
    Args:
        *args: Variable arguments containing:
               - name (str): Scenario file name (without .scn extension)
               - overwrite (int, optional): Overwrite flag (1=overwrite, 0=no overwrite)
               - files (str, optional): Flight data files to auto-load before generation
    
    Returns:
        Tuple[bool, str]: (True, "") on successful generation, (False, "") on error
    
    Examples:
        # Generate scenario from currently loaded data
        SATG_RL_MAKE morning_rush_scenario
        
        # Generate with overwrite enabled
        SATG_RL_MAKE busy_airspace 1
        
        # Auto-load data files and generate scenario
        SATG_RL_MAKE training_scenario 1 flight_data.json
    
    Note:
        Generated scenarios start paused to allow setup verification before
        simulation execution. ASAS is automatically enabled at simulation start
        for conflict detection training. The scenario file is saved to the
        configured scenarios directory with proper BlueSky formatting.
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
    """
    Generate and immediately execute Realistic Replay scenario.
    
    SATG_RL_RUN name [overwrite] [files]
    Write + immediately load <base>/scenarios/<name>.scn (paused; ASAS ON at 0).
    If files provided, automatically load them first.
    
    This command combines scenario generation and execution in a single operation,
    generating a BlueSky scenario file from loaded flight data and immediately
    loading it into the simulator for execution. This streamlined workflow is
    ideal for rapid training scenario deployment and iterative scenario testing
    with immediate validation of generated trajectories.
    
    The combined generation and execution process includes:
    1. Automatic flight data loading if files are provided
    2. Scenario generation with all configured jitter and phase parameters
    3. Immediate scenario file loading into BlueSky simulator
    4. Simulation initialization with paused state for setup review
    5. ASAS system activation for conflict detection training
    6. Ready-to-execute training scenario with realistic air traffic
    
    Execution features:
    - Immediate scenario validation through simulator loading
    - Paused start for instructor setup and briefing
    - ASAS system ready for separation assurance training
    - Real-time trajectory visualization for training effectiveness
    - Quick iteration capability for scenario refinement
    - Direct feedback on scenario quality and realism
    
    Args:
        *args: Variable arguments containing:
               - name (str): Scenario name for generation and execution
               - overwrite (int, optional): Overwrite flag (1=overwrite, 0=append)
               - files (str, optional): Flight data files to auto-load before generation
    
    Returns:
        Tuple[bool, str]: (True, "") on successful execution, (False, "") on error
    
    Examples:
        # Generate and run scenario from loaded data
        SATG_RL_RUN morning_training
        
        # Generate with overwrite and immediate execution
        SATG_RL_RUN conflict_scenario 1
        
        # Auto-load data, generate, and run in one command
        SATG_RL_RUN comprehensive_training 1 flight_data.json
    
    Note:
        The scenario starts paused to allow instructor setup and student briefing.
        ASAS is enabled automatically for conflict detection training. Use standard
        BlueSky commands (OP, HOLD) to control simulation execution after loading.
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

@command
def SATG_RL_LOAD_DATA(flights_json: str, points_json: str):
    """
    Load flight trajectory data directly from JSON strings for TraffixGen integration.
    
    SATG_RL_LOAD_DATA <flights_json> <points_json>
    Load flight data directly from JSON strings (for TraffixGen integration).
    This bypasses file loading and accepts processed data directly from TraffixGen.
    
    This command enables seamless integration between TraffixGen's machine learning
    pipeline and SATG's scenario generation capabilities by accepting pre-processed
    flight trajectory data directly in memory without intermediate file operations.
    This integration streamlines the workflow from EUROCONTROL data processing
    through ML-based traffic generation to realistic scenario creation.
    
    The direct data loading process includes:
    1. JSON string parsing and validation for both flights and points data
    2. Data structure verification for compatibility with SATG processing
    3. Flight trajectory integration with existing SATG data management
    4. Point cloud processing for spatial and temporal trajectory representation
    5. Seamless transition to SATG scenario generation pipeline
    6. Memory-efficient processing without temporary file creation
    
    Integration benefits:
    - Eliminates file I/O overhead for TraffixGen workflow integration
    - Maintains data integrity throughout the ML-to-scenario pipeline
    - Enables real-time scenario generation from ML-processed traffic data
    - Supports dynamic traffic pattern generation with immediate scenario creation
    - Preserves all trajectory metadata for enhanced scenario realism
    - Facilitates automated scenario generation workflows
    
    Args:
        flights_json (str): JSON string containing flight metadata and trajectory
                           information with structure compatible with EUROCONTROL
                           flight data format including callsigns, aircraft types,
                           routes, and operational parameters
        points_json (str): JSON string containing trajectory point data with
                          temporal and spatial coordinates, altitudes, speeds,
                          and other trajectory-specific operational parameters
    
    Returns:
        Tuple[bool, str]: (True, "") on successful data loading and integration,
                         (False, "") on JSON parsing errors or data validation failure
    
    Examples:
        # Load TraffixGen-processed flight data directly
        flights = '{"flights": [{"callsign": "KLM001", "route": "EHAM-EGLL"}]}'
        points = '{"points": [{"lat": 52.3, "lon": 4.8, "alt": 35000}]}'
        SATG_RL_LOAD_DATA flights points
        
        # Integration with TraffixGen automated workflow
        # (typically called programmatically from TraffixGen)
        result = traffixgen_export_to_satg(processed_data)
    
    Note:
        This command is primarily designed for programmatic use by TraffixGen
        and other automated traffic generation systems. The JSON data must
        conform to EUROCONTROL trajectory data structure for proper integration
        with SATG's flight processing and scenario generation capabilities.
    """
    try:
        import json
        
        # Parse JSON data
        flights_data = json.loads(flights_json)
        points_data = json.loads(points_json)
        
        # Validate data structure
        if not isinstance(flights_data, list) or not isinstance(points_data, list):
            _echo_err("SATG_RL_LOAD_DATA: Invalid data format - expected JSON arrays")
            return False, ""
        
        # Convert to expected format (same as _load_files)
        flights_rows = []
        for flight in flights_data:
            if not isinstance(flight, dict) or 'ECTRL ID' not in flight:
                continue
            flights_rows.append({
                'ECTRL ID': str(flight.get('ECTRL ID', '')),
                'Callsign': str(flight.get('Callsign', '')),  # Include callsign for aircraft creation
                'ADEP': str(flight.get('ADEP', '')),
                'ADES': str(flight.get('ADES', '')),
                'AC Type': str(flight.get('AC Type', '')),
                'AC Operator': str(flight.get('AC Operator', ''))  # Include operator for callsigns
            })
        
        points_rows = []
        for point in points_data:
            if not isinstance(point, dict) or 'ECTRL ID' not in point:
                continue
            points_rows.append({
                'ECTRL ID': str(point.get('ECTRL ID', '')),
                'Sequence Number': str(point.get('Sequence Number', 0)),
                'Time Over': str(point.get('Time Over', '')),
                'Flight Level': str(point.get('Flight Level', 0)),
                'Latitude': str(point.get('Latitude', 0.0)),
                'Longitude': str(point.get('Longitude', 0.0)),
                'ground_speed': str(point.get('ground_speed', 0.0)),
                'heading': str(point.get('heading', 0.0))
            })
        
        if not flights_rows or not points_rows:
            _echo_err("SATG_RL_LOAD_DATA: No valid flight or point data found")
            return False, ""
        
        # Use existing processing functions
        STATE.base_points = _build_base_points(points_rows)
        fl: Dict[str, Dict[str,str]] = {}
        for r in flights_rows:
            acid = r['ECTRL ID']
            fl[acid] = {
                'AC Type': r.get('AC Type',''), 
                'ADEP': r.get('ADEP',''), 
                'ADES': r.get('ADES',''),
                'Callsign': r.get('Callsign', ''),  # Include callsign in flight metadata
                'AC Operator': r.get('AC Operator', '')  # Include operator
            }
        
        STATE.flights = fl
        STATE.loaded_ok = True
        
        msg = f"Loaded {len(fl)} flights, {sum(len(v) for v in STATE.base_points.values())} points from TraffixGen"
        _echo_ok(msg, nxt="Now: SATG_RL_JITTER [on|off] ... (optional), then SATG_RL_RUN [SCNNAME]")
        return True, ""
        
    except json.JSONDecodeError as e:
        _echo_err(f"SATG_RL_LOAD_DATA: Invalid JSON format - {e}")
        return False, ""
    except Exception as e:
        _echo_err(f"SATG_RL_LOAD_DATA: Error processing data - {e}")
        return False, ""

# ---------------- GC commands (typed) ---------------- #
@command
def SATG_GC_REL(*argv):
    """
    Create relative geometric conflict scenario with precise aircraft positioning.
    
    SATG_GC_REL target=<acid> dpsi=<deg> dcpa=<NM> tlosh=<s>
    Optional:
      acid=<id> actype=<type> dh=<ft> tlosv=<s> spd=<CAS/Mach>
    include_target=1 target_acid=<id> target_type=<type> target_lat=<deg> target_lon=<deg>
               target_hdg=<deg> target_alt_ft=<ft> target_spd=<kt>
    name=<scenario> overwrite=1 seed=<int>
    
    This command generates geometric conflict scenarios by positioning aircraft
    relative to existing target aircraft in the simulation, creating precise
    conflict geometries with configurable parameters for separation distances,
    timing, and aircraft characteristics. This approach enables dynamic conflict
    generation during live simulation for advanced training scenarios.
    
    The relative positioning system calculates:
    1. Target aircraft state vector (position, heading, speed, altitude)
    2. Conflict geometry parameters (approach angle, CPA distance, timing)
    3. Intruder aircraft initial positioning for precise conflict execution
    4. Trajectory calculations ensuring specified separation parameters
    5. Optional target aircraft specification for complete conflict control
    6. Randomization with seed control for reproducible scenarios
    
    Conflict parameters enable precise control over:
    - dpsi: Relative approach angle difference (degrees) between aircraft tracks
    - dcpa: Distance at Closest Point of Approach (nautical miles)
    - tlosh: Time to Loss of Separation Horizontal (seconds) from conflict start
    - dh: Altitude separation difference (feet) for vertical conflict components
    - tlosv: Time to Loss of Separation Vertical (seconds) for climbing/descending
    
    Args:
        *argv: Variable keyword arguments containing conflict parameters:
               - target (str): Target aircraft ID for relative positioning
               - dpsi (float): Relative approach angle in degrees
               - dcpa (float): Distance at CPA in nautical miles
               - tlosh (float): Time to horizontal loss of separation in seconds
               - acid (str, optional): Intruder aircraft ID
               - actype (str, optional): Intruder aircraft type
               - dh (float, optional): Altitude difference in feet
               - tlosv (float, optional): Time to vertical loss of separation
               - spd (str, optional): Intruder speed (CAS or Mach)
               - include_target (int, optional): Include target aircraft in scenario
               - target_* (various, optional): Target aircraft override parameters
               - name (str): Scenario name for file generation
               - overwrite (int, optional): Overwrite existing scenario flag
               - seed (int, optional): Random seed for reproducible generation
    
    Returns:
        Tuple[bool, str]: (True, "") on successful generation, (False, "") on error
    
    Examples:
        # Create head-on conflict with existing aircraft
        SATG_GC_REL target=KLM001 dpsi=180 dcpa=3.0 tlosh=120 name=head_on_test
        
        # Create crossing conflict with altitude separation
        SATG_GC_REL target=BAW002 dpsi=90 dcpa=5.0 tlosh=180 dh=1000 name=crossing_alt
        
        # Create reproducible conflict with custom intruder
        SATG_GC_REL target=AFR003 dpsi=45 dcpa=4.0 tlosh=150 acid=UAL004 actype=B777 seed=12345 name=custom_conflict
    
    Note:
        The target aircraft must exist in the current simulation for relative
        positioning calculations. The command generates scenario files compatible
        with standard BlueSky scenario loading for training session integration.
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
    _gc_rel_write_scn(path, append=append, lines=lines,
        tcpa=params.get("tlosh", "N/A"),
        angle=params.get("dpsi", "N/A"),
        actypes=params.get("actype", "A320"),
        altmode="Mixed" if params.get("dh") else "Level"
    )
    act = "appended to" if append else "written to"
    _echo_ok(
        f"GC relative scenario {act} {path}",
        nxt="Load with SATG_GC_RUN <name>"
    )

    return True, ""


@command
def SATG_GC_CONF(hsep_nm: float=5.0, vsep_ft: int=1000):
    """
    Configure loss-of-separation thresholds for Geometric Conflict generation.
    
    This command function sets the horizontal and vertical separation minima used
    in geometric conflict scenario design. These thresholds define the critical
    separation distances that constitute a loss of separation event and are used
    to calculate conflict geometry parameters such as Closest Point of Approach (CPA).
    
    The configured separation minima are used throughout the geometric conflict
    generation process to ensure realistic conflict scenarios that represent
    actual air traffic management separation requirements. These values serve
    as the baseline for conflict detection and resolution algorithms.
    
    Args:
        hsep_nm (float, optional): Horizontal separation minimum in nautical miles.
                                 Defaults to 5.0 NM which is standard for enroute
                                 operations in controlled airspace.
        vsep_ft (int, optional): Vertical separation minimum in feet. Defaults to
                               1000 ft which is standard for operations below FL290.
    
    Returns:
        tuple: (True, "") indicating successful configuration
    
    Examples:
        # Use default separation standards (5 NM horizontal, 1000 ft vertical)
        SATG_GC_CONF
        
        # Set custom horizontal separation for terminal area operations
        SATG_GC_CONF hsep_nm=3.0
        
        # Set RVSM vertical separation for high-altitude operations
        SATG_GC_CONF hsep_nm=5.0 vsep_ft=1000
        
        # Configure for approach/departure operations
        SATG_GC_CONF hsep_nm=2.5 vsep_ft=500
    
    Note:
        These values are used for informational and design purposes in conflict
        generation. The actual BlueSky simulation may use different separation
        standards. Default values comply with ICAO standard separation minima
        for controlled airspace operations.
    """
    STATE.gc_hsep_nm = float(hsep_nm)
    STATE.gc_vsep_ft = int(vsep_ft)
    _echo_ok(f"GC minima set: HSEP={STATE.gc_hsep_nm} NM, VSEP={STATE.gc_vsep_ft} ft",
             nxt="Optionally set sampling ranges: SATG_GC_RANGE [cas1=..] [cas2=..] [fl1=..] [fl2=..] [brg1=..] [angle=..]")
    return True, ""

@command
def SATG_GC_TYPES(*types):
    """
    Configure aircraft types for Geometric Conflict scenario generation.
    
    SATG_GC_TYPES [TYPE1] [TYPE2] ...
    Set the candidate aircraft types used for CPA scenario generation.
    Without arguments, resets to the default list.
    
    This command defines the pool of aircraft types available for selection
    during geometric conflict scenario generation, enabling realistic aircraft
    performance characteristics and operational diversity in training scenarios.
    The aircraft type selection affects trajectory calculations, performance
    parameters, and visual representation in conflict scenarios.
    
    Aircraft type configuration influences:
    - Flight performance characteristics (climb rates, speeds, maneuverability)
    - Realistic aircraft mix for training scenario authenticity
    - Wake turbulence categories for separation requirement modeling
    - Visual identification training with diverse aircraft silhouettes
    - Operational realism matching real-world traffic compositions
    - Statistical analysis of conflict resolution performance by aircraft type
    
    The system maintains a curated list of common commercial aircraft types
    with verified performance characteristics suitable for air traffic control
    training scenarios. Custom aircraft types can be added if supported by
    the BlueSky aircraft performance database.
    
    Args:
        *types: Variable arguments containing aircraft type designators
               (e.g., "A320", "B738", "A350", "B78X"). Aircraft codes should
               follow ICAO aircraft type designator standards. Multiple types
               can be specified separated by spaces or commas.
    
    Returns:
        Tuple[bool, str]: (True, "") on successful configuration, (False, "") on error
    
    Examples:
        # Set common European aircraft types
        SATG_GC_TYPES A320 B738 A350 B77W
        
        # Set mixed fleet with regional and wide-body aircraft  
        SATG_GC_TYPES E190 A320 A330 B747 B78X
        
        # Reset to default aircraft type selection
        SATG_GC_TYPES
        
        # Single aircraft type for specialized training
        SATG_GC_TYPES A320
    
    Note:
        Aircraft types must be supported by BlueSky's aircraft performance
        database for proper scenario execution. The default list includes
        verified aircraft types commonly used in European airspace for
        comprehensive air traffic control training coverage.
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
    """
    Configure sampling ranges for geometric conflict scenario parameters.
    
    Configure sampling ranges for geometric conflicts.

    Parameters mirror the SATG_GC_CRE inputs:
      cas1/cas2 -> CAS range [kt] for aircraft 1/2
      fl1/fl2   -> Flight level range for aircraft 1/2
      brg1      -> Initial bearing range for aircraft 1 (deg)
      angle     -> CPA angle range (0=head-on, 180=overtake)
    
    This command sets the statistical sampling ranges used for randomizing
    aircraft parameters in geometric conflict scenarios, enabling diverse
    training scenarios with realistic operational parameter variations.
    The ranges define the bounds for random parameter generation, ensuring
    scenarios remain within operationally realistic constraints while
    providing sufficient diversity for comprehensive training coverage.
    
    Parameter range configuration enables:
    - Calibrated airspeed variations matching realistic cruise performance
    - Flight level distributions representative of actual traffic patterns
    - Initial bearing randomization for diverse approach geometries
    - Conflict angle variations covering head-on, crossing, and overtaking scenarios
    - Statistical scenario generation with controlled parameter bounds
    - Reproducible training scenarios with consistent parameter distributions
    
    Sampling ranges support both fixed values and intervals:
    - Single values: "350" for fixed parameters
    - Ranges: "320-450" for uniform distribution between bounds
    - Flight levels: Specified as flight level numbers (e.g., "350" = FL350)
    - Angles: Specified in degrees with 0° = head-on, 180° = overtaking
    
    Args:
        cas1 (str, optional): CAS range for aircraft 1 in knots (e.g., "250-450")
        cas2 (str, optional): CAS range for aircraft 2 in knots (e.g., "280-420")
        fl1 (str, optional): Flight level range for aircraft 1 (e.g., "300-400")
        fl2 (str, optional): Flight level range for aircraft 2 (e.g., "310-390")
        brg1 (str, optional): Initial bearing range for aircraft 1 in degrees (e.g., "0-360")
        angle (str, optional): CPA angle range in degrees (e.g., "30-150")
    
    Returns:
        Tuple[bool, str]: (True, "") on successful configuration, (False, "") on error
    
    Examples:
        # Configure realistic cruise speed and altitude ranges
        SATG_GC_RANGE cas1="320-450" cas2="300-420" fl1="300-400" fl2="310-390"
        
        # Set head-on conflict parameters
        SATG_GC_RANGE angle="0-30" brg1="0-360"
        
        # Configure crossing conflict scenarios
        SATG_GC_RANGE angle="60-120" cas1="350" cas2="380"
    
    Note:
        Parameter ranges should reflect realistic operational constraints
        for the intended training airspace and aircraft types. The system
        uses uniform distributions within specified ranges for scenario
        generation, ensuring balanced coverage of the parameter space.
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
    """
    Create geometric conflict scenarios with precise CPA (Closest Point of Approach) design.
    
    This command function generates sophisticated geometric conflict scenarios where
    two aircraft are precisely positioned to achieve a specified loss of separation
    at a defined time and location. The function uses advanced geometric algorithms
    to calculate aircraft positions and trajectories that result in controlled
    conflict situations for air traffic management research and training.
    
    The geometric conflict creation process involves:
    1. Define conflict location and timing parameters
    2. Calculate aircraft trajectories to achieve specified CPA
    3. Generate realistic aircraft parameters and routes
    4. Create BlueSky-compatible scenario files with conflict geometry
    
    Required Parameters:
    - name: Scenario filename for the generated conflict
    - Location: Either lat/lon coordinates OR waypoint identifier
    - tcpa: Time to Closest Point of Approach in seconds
    
    Optional Geometry Parameters:
    - angle: Crossing angle between aircraft tracks (degrees)
    - dh: Altitude difference at CPA (feet)
    - fl_cpa: Flight level at conflict point
    
    Args:
        *argv: Variable arguments supporting both keyword and positional formats:
              name=<scenario> - Output scenario filename (required)
              lat=<degrees> lon=<degrees> - Conflict location coordinates
              wp=<identifier> - Named waypoint for conflict location
              tcpa=<seconds> - Time to CPA from scenario start (required)
              angle=<degrees> - Aircraft crossing angle (optional)
              dh=<feet> - Vertical separation at CPA (optional)
              acid1=<callsign> - First aircraft identifier (optional)
              acid2=<callsign> - Second aircraft identifier (optional)
              ac1=<type> ac2=<type> - Aircraft type specifications (optional)
              actypes=<types> - List of candidate aircraft types (optional)
              fl_cpa=<level> - Flight level at conflict (optional)
              seed=<number> - Random seed for reproducibility (optional)
              overwrite=<bool> - Allow scenario file overwriting (optional)
    
    Returns:
        tuple: (success, message) indicating conflict creation success
    
    Examples:
        # Create head-on conflict at specific coordinates
        SATG_GC_CRE name=conflict1 lat=52.0 lon=4.0 tcpa=300 angle=180
        
        # Create crossing conflict at named waypoint
        SATG_GC_CRE name=crossing wp=SUGOL tcpa=240 angle=90 dh=500
        
        # Create conflict with specific aircraft types
        SATG_GC_CRE name=heavy_conflict lat=51.5 lon=4.5 tcpa=180 ac1=B748 ac2=A380
    
    Note:
        The function supports both modern keyword arguments and legacy positional
        arguments for backward compatibility. Geometric calculations ensure precise
        conflict timing and positioning while maintaining realistic flight parameters.
        Generated scenarios include proper aircraft initialization and routing.
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
    """
    Load and execute geometric conflict scenario for training.
    
    SATG_GC_RUN name
    Load the specified geometric-conflict scenario (paused; ASAS ON at 0 only in file header).
    
    This command loads previously generated geometric conflict scenarios into
    the BlueSky simulator for immediate execution, providing streamlined access
    to training scenarios with proper initialization for conflict detection and
    resolution training. The scenario starts paused to allow instructor setup
    and student briefing before conflict execution begins.
    
    Scenario loading process includes:
    1. Scenario file validation and path resolution
    2. BlueSky scenario loading with proper initialization
    3. ASAS system preparation for conflict detection
    4. Simulation state setup with paused start for training control
    5. Conflict timing synchronization for precise training execution
    6. Ready-to-execute state for immediate training session start
    
    Training scenario features:
    - Paused start for instructor control and student preparation
    - ASAS system enabled for conflict detection and alerting
    - Precise conflict timing for predictable training outcomes
    - Multiple conflict capability through scenario composition
    - Realistic aircraft trajectories with operational constraints
    - Integrated conflict resolution challenge progression
    
    Args:
        name (str): Scenario name (without .scn extension) to load from the
                   configured scenarios directory. The scenario must have been
                   previously generated using SATG_GC_CRE command.
    
    Returns:
        Tuple[bool, str]: (True, "") on successful loading, (False, "") on file error
    
    Examples:
        # Load head-on conflict training scenario
        SATG_GC_RUN head_on_training
        
        # Load complex multi-conflict scenario
        SATG_GC_RUN advanced_conflicts
        
        # Load specific conflict geometry scenario
        SATG_GC_RUN crossing_60deg_FL350
    
    Note:
        The scenario file must exist in the configured scenarios directory.
        Additional conflicts can be appended to the same scenario using
        SATG_GC_CRE with the same scenario name for complex training sequences.
        Use standard BlueSky commands (OP, HOLD) to control simulation execution.
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
    """
    Delete all aircraft created by geometric conflict generation.
    
    SATG_GC_DEL
    Delete all aircraft created via SATG_GC_CRE during this BlueSky session.
    
    This command provides clean-up functionality for geometric conflict scenarios
    by removing all aircraft that were created during the current session through
    SATG geometric conflict generation commands. This enables rapid scenario
    reset and preparation for new conflict scenarios without full simulation
    restart, improving training session efficiency and workflow.
    
    The deletion process includes:
    1. Identification of all SATG-generated aircraft from session records
    2. Individual aircraft deletion commands sent to BlueSky simulator
    3. Session state cleanup to reset aircraft tracking records
    4. Confirmation feedback with list of deleted aircraft identifiers
    5. Preparation for new scenario generation without aircraft ID conflicts
    6. Memory cleanup for optimal simulation performance
    
    Cleanup benefits for training sessions:
    - Rapid scenario reset without full simulation restart
    - Clean slate preparation for new conflict scenarios
    - Prevention of aircraft ID conflicts in subsequent scenarios
    - Memory optimization for extended training sessions
    - Clear visual airspace for new scenario setup
    - Streamlined workflow for iterative training scenario testing
    
    Returns:
        Tuple[bool, str]: (True, "") on successful deletion, (False, "") if no aircraft
    
    Examples:
        # Clean up after conflict resolution training
        SATG_GC_DEL
        
        # Prepare for new scenario after training session
        SATG_GC_DEL
        
        # Reset airspace between different conflict types
        SATG_GC_DEL
    
    Note:
        Only aircraft created through SATG_GC_CRE commands are deleted.
        Other aircraft in the simulation remain unaffected. The command
        maintains session records to track SATG-generated aircraft for
        accurate cleanup without affecting manually created aircraft.
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
            if not append:
                # Write scenario header
                header = _generate_scenario_header("Random Conflicts",
                    n="Mixed mode" if conflict_params.get("mode") == "mix" else "Relative conflicts",
                    center_lat=target_data.get("lat", "Variable"),
                    center_lon=target_data.get("lon", "Variable"), 
                    radius_nm="N/A (Relative mode)",
                    types="Relative conflicts",
                    altmode="Mixed" if conflict_params.get("dh", 0) != 0 else "Level",
                    tcpa=conflict_params.get("tlosh", "N/A"),
                    fl_range="Variable",
                    cas_range="Variable"
                )
                for line in header:
                    f.write(f"{line}\n")
                f.write("0:00:00.00>HOLD\n")
                f.write("0:00:00.00>ASAS ON\n")
                
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
    """
    Generate randomized conflicts within circular or polygonal airspace areas.
    
    SATG_RC_CIRCLE name n types center_lat center_lon radius_nm [mode] [altmode] [tcpa] [angle] [dh] [seed] [fl] [cas] [actypes] [overwrite] [area_type] [polygon_name]
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
    
    This command generates multiple randomized geometric conflict scenarios
    distributed within specified airspace boundaries, creating diverse training
    scenarios with realistic spatial distribution patterns. The random conflict
    placement enables comprehensive airspace coverage for training controllers
    on varied conflict geometries and locations within their operational area.
    
    The randomized conflict generation process includes:
    1. Spatial distribution calculation within circular or polygonal boundaries
    2. Conflict type randomization (head-on, crossing, overtaking scenarios)
    3. CPA timing synchronization for realistic conflict sequence presentation
    4. Aircraft parameter sampling from configured ranges for diversity
    5. Altitude mode selection for vertical conflict dimension training
    6. Reproducible scenario generation with optional seed control
    
    Spatial distribution features:
    - Uniform random CPA placement within defined airspace boundaries
    - Circular areas: Center point and radius specification in nautical miles
    - Polygonal areas: Named polygon boundaries for irregular airspace shapes
    - Geographic coordinate system integration for real-world airspace modeling
    - Boundary compliance validation for all generated conflicts
    - Scalable area coverage from approach sectors to terminal control areas
    
    Conflict diversity parameters:
    - Multiple conflict types in single scenario for comprehensive training
    - Randomized aircraft performance parameters within operational bounds
    - Altitude crossing patterns for vertical separation training challenges
    - Mixed conflict generation modes combining absolute and relative positioning
    - Aircraft type diversity from configured type pools for operational realism
    
    Args:
        *argv: Variable arguments supporting positional or key=value format:
               - name (str): Scenario name for file generation
               - n (int): Number of conflict pairs to generate
               - types (str): CSV conflict types (headon,cross,overtake)
               - center_lat (float): Center latitude for circular area (degrees)
               - center_lon (float): Center longitude for circular area (degrees)  
               - radius_nm (float): Radius in nautical miles for circular area
               - mode (str, optional): Generation mode (abs|rel|mix, default: abs)
               - altmode (str, optional): Altitude mode (level|altcross|mix)
               - tcpa (float, optional): Time to CPA in seconds
               - angle (float, optional): Conflict angle constraint in degrees
               - dh (str, optional): Altitude offset range in feet (e.g., "0:2000")
               - seed (int, optional): Random seed for reproducible generation
               - fl (str, optional): Flight level range override
               - cas (str, optional): CAS range override in knots
               - actypes (str, optional): Aircraft type list override
               - overwrite (int, optional): Overwrite existing scenario flag
               - area_type (str, optional): Area type (circle|polygon, default: circle)
               - polygon_name (str, optional): Named polygon for area_type=polygon
    
    Returns:
        Tuple[bool, str]: (True, "") on successful generation, (False, "") on error
    
    Examples:
        # Generate 5 mixed conflicts in circular area around Amsterdam
        SATG_RC_CIRCLE training_ams 5 headon,cross,overtake 52.3 4.8 25
        
        # Create reproducible scenario with specific parameters  
        SATG_RC_CIRCLE seed_test 3 cross 51.5 0.1 30 mode=abs altmode=level seed=12345
        
        # Generate conflicts within named polygon boundary
        SATG_RC_CIRCLE sector_conflicts 8 headon,cross center_lat=50.0 center_lon=3.0 area_type=polygon polygon_name=BRUSSELS_TMA
    
    Note:
        Circular areas require center coordinates and radius. Polygon areas
        require geopandas installation and named polygon definitions. All
        generated conflicts maintain operational realism within specified
        airspace boundaries for effective controller training scenarios.
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
    """
    Load waypoint definition file for procedure creation and validation.
    
    This command function loads a waypoint definition file containing named
    navigation points with their coordinates. These waypoints are used during
    procedure creation to resolve waypoint names to geographic coordinates
    and validate procedure routing.
    
    The waypoint file should contain properly formatted waypoint definitions
    with names and coordinates that can be referenced in SID/STAR procedures.
    Multiple waypoint files can be loaded to build a comprehensive navigation
    database for procedure operations.
    
    Args:
        path (str): File path to the waypoint definition file. Quotes will be
                   stripped from the path if present.
    
    Returns:
        tuple: (success, message) where success is boolean indicating if the
               file was successfully loaded
    
    Examples:
        # Load a waypoint definition file
        SATG_PROC_LOAD_WPT /path/to/waypoints.txt
        
        # Load waypoint file with quoted path
        SATG_PROC_LOAD_WPT "/path with spaces/waypoints.txt"
    
    Note:
        The waypoint file is added to the global waypoint file list and will
        be used for waypoint resolution in all subsequent procedure operations.
        If the file is already loaded, it won't be added again.
    """
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
def SATG_PROC_LOAD_CUSTOM():
    """Auto-load all custom procedure files from satg_data/procedures/ folder."""
    try:
        # Get the procedures directory path
        procedures_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'satg_data', 'procedures')
        
        if not os.path.exists(procedures_dir):
            _echo_err(f"Procedures directory not found: {procedures_dir}")
            return False, ""
        
        # Find all .scn files in the procedures directory
        proc_files = []
        for filename in os.listdir(procedures_dir):
            if filename.lower().endswith('.scn'):
                filepath = os.path.join(procedures_dir, filename)
                if os.path.isfile(filepath):
                    proc_files.append(filepath)
        
        if not proc_files:
            _echo_ok("No custom procedure files found in procedures directory")
            return True, ""
        
        # Load each procedure file
        loaded_count = 0
        for filepath in proc_files:
            try:
                success, _ = SATG_PROC_LOAD_PROC(filepath)
                if success:
                    loaded_count += 1
            except Exception as e:
                _echo_err(f"Error loading {os.path.basename(filepath)}: {e}")
        
        _echo_ok(f"Loaded {loaded_count} custom procedure files from procedures directory")
        return True, ""
        
    except Exception as e:
        _echo_err(f"Error loading custom procedures: {e}")
        return False, ""


@command
def SATG_PROC_EXPORT_POLY(poly_name: str):
    """Export polygon coordinates to a temporary file for GUI access."""
    try:
        import bluesky as bs
        
        poly_name_upper = poly_name.strip().upper()
        if not poly_name_upper:
            _echo_err("SATG_PROC_EXPORT_POLY: missing polygon name")
            return False, ""
        
        # Access BlueSky's areafilter to get polygon coordinates
        if hasattr(bs, 'sim') and hasattr(bs.sim, 'areafilter'):
            areafilter = bs.sim.areafilter
            if hasattr(areafilter, 'areas') and poly_name_upper in areafilter.areas:
                area_data = areafilter.areas[poly_name_upper]
                if hasattr(area_data, 'border') and area_data.border is not None:
                    # Extract coordinates from border
                    coords = []
                    border = area_data.border
                    if hasattr(border, 'lat') and hasattr(border, 'lon'):
                        for i in range(len(border.lat)):
                            coords.append((float(border.lat[i]), float(border.lon[i])))
                    
                    if coords:
                        # Export coordinates to temporary file
                        import tempfile
                        import json
                        from datetime import datetime
                        
                        temp_dir = tempfile.gettempdir()
                        temp_file = os.path.join(temp_dir, f"satg_poly_{poly_name_upper}.json")
                        
                        data = {
                            'polygon_name': poly_name_upper,
                            'coordinates': coords,
                            'timestamp': datetime.now().isoformat()
                        }
                        
                        with open(temp_file, 'w') as f:
                            json.dump(data, f, indent=2)
                        
                        _echo_ok(f"Exported {len(coords)} coordinates for polygon '{poly_name_upper}' to {temp_file}")
                        return True, temp_file
                    else:
                        _echo_err(f"SATG_PROC_EXPORT_POLY: no coordinates found for polygon '{poly_name_upper}'")
                        return False, ""
                else:
                    _echo_err(f"SATG_PROC_EXPORT_POLY: polygon '{poly_name_upper}' has no border data")
                    return False, ""
            else:
                _echo_err(f"SATG_PROC_EXPORT_POLY: polygon '{poly_name_upper}' not found")
                return False, ""
        else:
            _echo_err("SATG_PROC_EXPORT_POLY: BlueSky areafilter not available")
            return False, ""
            
    except Exception as e:
        _echo_err(f"SATG_PROC_EXPORT_POLY: error exporting polygon: {e}")
        return False, ""


@command
def SATG_PROC_CREATE_FROM_POLY(poly_name: str, proc_name: str = ""):
    """Create a basic procedure file directly from polygon coordinates."""
    try:
        from datetime import datetime
        
        poly_name_clean = poly_name.strip()
        if not poly_name_clean:
            _echo_err("SATG_PROC_CREATE_FROM_POLY: missing polygon name")
            return False, ""
        
        _echo_ok(f"SATG_PROC_CREATE_FROM_POLY: Attempting to find polygon '{poly_name_clean}'")
        
        # Use proc_name if provided, otherwise use poly_name
        procedure_name = proc_name.strip() if proc_name.strip() else poly_name_clean
        
        # Use the same method as other SATG polygon commands with case-insensitive search
        poly = areafilter.getArea(poly_name_clean)
        actual_poly_name = poly_name_clean
        
        # If not found, try case-insensitive search
        if poly is None and hasattr(areafilter, 'basic_shapes') and areafilter.basic_shapes:
            for area_name, shape in areafilter.basic_shapes.items():
                if area_name.lower() == poly_name_clean.lower():
                    poly = shape
                    actual_poly_name = area_name
                    _echo_ok(f"SATG_PROC_CREATE_FROM_POLY: Found polygon with case-insensitive match: '{area_name}'")
                    break
        
        if poly is None:
            _echo_err(f"SATG_PROC_CREATE_FROM_POLY: polygon '{poly_name_clean}' not found")
            # Try to list available polygons for debugging
            try:
                if hasattr(areafilter, 'areas') and areafilter.areas:
                    available = list(areafilter.areas.keys())
                    _echo_err(f"Available polygons: {available}")
                elif hasattr(areafilter, 'basic_shapes') and areafilter.basic_shapes:
                    available = list(areafilter.basic_shapes.keys())
                    _echo_err(f"Available basic shapes: {available}")
                else:
                    _echo_err("No polygons found in areafilter")
            except:
                _echo_err("Could not list available polygons")
            return False, ""
        
        _echo_ok(f"SATG_PROC_CREATE_FROM_POLY: Found polygon '{actual_poly_name}'")
        
        if not hasattr(poly, 'coordinates'):
            _echo_err(f"SATG_PROC_CREATE_FROM_POLY: '{poly_name_clean}' is not a polygon area")
            _echo_err(f"Polygon type: {type(poly)}, attributes: {dir(poly)}")
            return False, ""
        
        coordinates = poly.coordinates
        _echo_ok(f"SATG_PROC_CREATE_FROM_POLY: Found {len(coordinates)} coordinates")
        
        if len(coordinates) < 6:  # Need at least 3 points (6 coordinates)
            _echo_err(f"SATG_PROC_CREATE_FROM_POLY: polygon '{poly_name_clean}' has insufficient coordinates ({len(coordinates)})")
            return False, ""
        
        # Parse coordinates into (lat, lon) pairs
        waypoints = []
        for i in range(0, len(coordinates), 2):
            if i + 1 < len(coordinates):
                lat = coordinates[i]
                lon = coordinates[i + 1]
                waypoints.append((lat, lon))
        
        _echo_ok(f"SATG_PROC_CREATE_FROM_POLY: Parsed {len(waypoints)} waypoints")
        
        # Create procedures directory if it doesn't exist - use root satg_data folder
        procedures_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'satg_data', 'procedures')
        os.makedirs(procedures_dir, exist_ok=True)
        
        # Create filename
        filename = f"{procedure_name}.scn"
        filepath = os.path.join(procedures_dir, filename)
        
        _echo_ok(f"SATG_PROC_CREATE_FROM_POLY: Will create file at {filepath}")
        
        # Create file content
        content = []
        content.append(f"# Procedure: {procedure_name}")
        content.append(f"# Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        content.append(f"# Type: Custom Track Procedure from Polygon {actual_poly_name}")
        content.append(f"# Waypoints: {len(waypoints)}")
        content.append("#")
        
        # Add waypoints directly with coordinates (no DEFWPT needed)
        for i, (lat, lon) in enumerate(waypoints):
            wp_name = f"{procedure_name}WP{i+1:02d}"
            content.append(f"00:00:00.00>%0 ADDWPT {lat:.6f} {lon:.6f}")
        
        content.append("")  # Empty line at end
        
        # Write file
        with open(filepath, 'w') as f:
            f.write('\n'.join(content))
        
        _echo_ok(f"Created procedure file: {filepath}")
        _echo_ok(f"Found {len(waypoints)} waypoints from polygon '{actual_poly_name}'")
        _echo_ok(f"Use SATG_PROC_LOAD_FOR_EDIT {procedure_name} to edit constraints")
        return True, filepath
            
    except Exception as e:
        _echo_err(f"SATG_PROC_CREATE_FROM_POLY: error creating procedure: {e}")
        import traceback
        _echo_err(f"Traceback: {traceback.format_exc()}")
        return False, ""


@command
def SATG_PROC_LOAD_FOR_EDIT(proc_name: str):
    """Load a procedure file and export waypoints for GUI editing."""
    try:
        from datetime import datetime
        import tempfile
        import json
        import re
        
        proc_name_clean = proc_name.strip()
        if not proc_name_clean:
            _echo_err("SATG_PROC_LOAD_FOR_EDIT: missing procedure name")
            return False, ""
        
        # Find the procedure file - use root satg_data folder
        procedures_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'satg_data', 'procedures')
        filepath = os.path.join(procedures_dir, f"{proc_name_clean}.scn")
        
        if not os.path.exists(filepath):
            _echo_err(f"SATG_PROC_LOAD_FOR_EDIT: procedure file not found: {filepath}")
            return False, ""
        
        # Parse the procedure file to extract waypoints
        waypoints = []
        with open(filepath, 'r') as f:
            content = f.read()
        
        _echo_ok(f"SATG_PROC_LOAD_FOR_EDIT: Parsing procedure file")
        
        # Find ADDWPT commands line by line to avoid cross-line matching
        lines = content.split('\n')
        addwpt_lines = [line for line in lines if '00:00:00.00>%0 ADDWPT' in line]
        
        _echo_ok(f"SATG_PROC_LOAD_FOR_EDIT: Found {len(addwpt_lines)} ADDWPT lines")
        
        for i, line in enumerate(addwpt_lines):
            _echo_ok(f"Processing line: {repr(line)}")
            
            # Split the line and extract components
            parts = line.strip().split()
            if len(parts) >= 3 and parts[0] == '00:00:00.00>%0' and parts[1] == 'ADDWPT':
                try:
                    # Check if this is a coordinate-based waypoint (lat/lon numbers) or named waypoint
                    waypoint_identifier = parts[2]
                    
                    # Try to parse as coordinates first
                    try:
                        lat = float(waypoint_identifier)
                        lon = float(parts[3]) if len(parts) > 3 else 0.0
                        wp_name = f"WP{i+1:02d}"
                        is_coordinate_based = True
                    except (ValueError, IndexError):
                        # It's a named waypoint
                        wp_name = waypoint_identifier
                        lat = 0.0  # Placeholder - will be resolved by BlueSky
                        lon = 0.0  # Placeholder - will be resolved by BlueSky
                        is_coordinate_based = False
                    
                    # Handle altitude and speed parameters
                    wp_alt = ""
                    wp_spd = ""
                    
                    if is_coordinate_based and len(parts) > 4:
                        # For coordinate-based waypoints, altitude is at index 4
                        if parts[4] != ",,":  # Not a placeholder
                            wp_alt = parts[4]
                        
                        if len(parts) > 5:
                            wp_spd = parts[5]
                        elif parts[4] == ",," and len(parts) > 4:
                            # Format: ADDWPT lat lon ,, speed
                            wp_spd = parts[5] if len(parts) > 5 else ""
                    elif not is_coordinate_based and len(parts) > 3:
                        # For named waypoints, altitude might be at index 3
                        if parts[3] != ",,":
                            wp_alt = parts[3]
                        
                        if len(parts) > 4:
                            wp_spd = parts[4]
                    
                    if is_coordinate_based:
                        _echo_ok(f"Parsed coordinate waypoint: {wp_name} at {lat}, {lon}, alt='{wp_alt}', spd='{wp_spd}'")
                    else:
                        _echo_ok(f"Parsed named waypoint: {wp_name} (coordinates will be resolved by BlueSky), alt='{wp_alt}', spd='{wp_spd}'")
                    
                    waypoints.append({
                        "name": wp_name,
                        "lat": lat,
                        "lon": lon,
                        "alt": wp_alt,
                        "spd": wp_spd,
                        "is_named": not is_coordinate_based
                    })
                except (ValueError, IndexError) as e:
                    _echo_err(f"Error parsing line '{line}': {e}")
                    continue
        
        if waypoints:
            # Export waypoints to temporary file for GUI
            temp_dir = tempfile.gettempdir()
            temp_file = os.path.join(temp_dir, f"satg_proc_edit_{proc_name_clean}.json")
            
            # Remove any existing temp file to ensure fresh data
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass
            
            data = {
                'procedure_name': proc_name_clean,
                'filepath': filepath,
                'waypoints': waypoints,
                'timestamp': datetime.now().isoformat(),
                'request_id': f"{proc_name_clean}_{datetime.now().timestamp()}"  # Unique request ID
            }
            
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            _echo_ok(f"Loaded {len(waypoints)} waypoints for editing: {proc_name_clean}")
            return True, temp_file
        else:
            _echo_err(f"SATG_PROC_LOAD_FOR_EDIT: no waypoints found in procedure file")
            return False, ""
            
    except Exception as e:
        _echo_err(f"SATG_PROC_LOAD_FOR_EDIT: error loading procedure: {e}")
        return False, ""


def _rebuild_generic_rates():
    """Rebuild generic procedure rates based on currently loaded procedure files.
    
    This function scans all loaded procedure files and rebuilds the generic rates
    to match only the waypoints available in the current files. This ensures that
    when files are removed, their associated generic rate data is also cleaned up.
    """
    # Save the current generic configuration
    current_basis = str(STATE.proc_generic_cfg.get("rate_basis", "initial")).lower()
    if current_basis not in ("initial", "final"):
        current_basis = "initial"
    
    # Get all available waypoint tokens from currently loaded files
    available_tokens = set()
    for proc_path in STATE.proc_proc_files:
        if os.path.exists(proc_path):
            # Extract initial and final waypoint tokens
            initial_token = _proc_unified_waypoint_token(proc_path, is_first=True)
            final_token = _proc_unified_waypoint_token(proc_path, is_first=False)
            
            if initial_token:
                available_tokens.add(initial_token.upper())
            if final_token:
                available_tokens.add(final_token.upper())
    
    # Clear and rebuild generic rates tables, keeping only tokens from loaded files
    for basis in ["initial", "final"]:
        if basis in STATE.proc_generic_rates:
            # Keep only rates for waypoints that still exist in loaded files
            existing_rates = STATE.proc_generic_rates[basis]
            filtered_rates = {token: rate for token, rate in existing_rates.items() 
                            if token in available_tokens}
            STATE.proc_generic_rates[basis] = filtered_rates


@command
def SATG_PROC_UNLOAD_PROC(path: str):
    p = os.path.abspath(_normpath(path.strip('"').strip("'")))
    STATE.proc_proc_files = [x for x in STATE.proc_proc_files if x != p]
    _unregister_sid_proc(p)
    _unregister_star_proc(p)
    STATE.proc_destinations.pop(p, None)
    
    # Rebuild generic rates to remove data from the unloaded file
    _rebuild_generic_rates()
    
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
    STATE.proc_generic_rates = {"initial": {}, "final": {}}
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
def SATG_PROC_OVERRIDE_GENERIC(override_initial_alt: int, override_initial_spd: int, override_final_alt: int, override_final_spd: int):
    """
    Set override flags for generic procedure initial and final constraints.
    
    Args:
        override_initial_alt: 1 to override initial altitude, 0 to use procedure file values
        override_initial_spd: 1 to override initial speed, 0 to use procedure file values  
        override_final_alt: 1 to override final altitude, 0 to use procedure file values
        override_final_spd: 1 to override final speed, 0 to use procedure file values
    """
    STATE.proc_generic_cfg.update({
        "override_initial_alt": bool(override_initial_alt),
        "override_initial_spd": bool(override_initial_spd),
        "override_final_alt": bool(override_final_alt),
        "override_final_spd": bool(override_final_spd)
    })
    _echo_ok(f"Generic procedure overrides: initial_alt={bool(override_initial_alt)}, initial_spd={bool(override_initial_spd)}, final_alt={bool(override_final_alt)}, final_spd={bool(override_final_spd)}")


@command
def SATG_PROC_OVERRIDE_SID(override_initial_alt: int, override_initial_spd: int):
    """
    Set override flags for SID procedure initial constraints.
    
    Args:
        override_initial_alt: 1 to override initial altitude, 0 to use procedure file values
        override_initial_spd: 1 to override initial speed, 0 to use procedure file values
    """
    STATE.proc_sid_cfg.update({
        "override_initial_alt": bool(override_initial_alt),
        "override_initial_spd": bool(override_initial_spd)
    })
    _echo_ok(f"SID procedure overrides: initial_alt={bool(override_initial_alt)}, initial_spd={bool(override_initial_spd)}")


@command
def SATG_PROC_OVERRIDE_STAR(override_initial_alt: int, override_initial_spd: int, override_final_alt: int, override_final_spd: int):
    """
    Set override flags for STAR procedure initial and final constraints.
    
    Args:
        override_initial_alt: 1 to override initial altitude, 0 to use procedure file values
        override_initial_spd: 1 to override initial speed, 0 to use procedure file values
        override_final_alt: 1 to override final altitude, 0 to use procedure file values
        override_final_spd: 1 to override final speed, 0 to use procedure file values
    """
    STATE.proc_star_cfg.update({
        "override_initial_alt": bool(override_initial_alt),
        "override_initial_spd": bool(override_initial_spd),
        "override_final_alt": bool(override_final_alt),
        "override_final_spd": bool(override_final_spd)
    })
    _echo_ok(f"STAR procedure overrides: initial_alt={bool(override_initial_alt)}, initial_spd={bool(override_initial_spd)}, final_alt={bool(override_final_alt)}, final_spd={bool(override_final_spd)}")


@command
def SATG_PROC_CFG_GENERIC(flights: int, alt_fl: int, mach: float, schedule_mode: int, rate_basis_idx: int, final_alt_fl: int, final_spd: int):
    flights = max(0, int(flights))
    alt_fl = max(0, int(alt_fl))
    mach = max(0.40, min(0.92, float(mach)))
    schedule_mode = int(schedule_mode)
    rate_basis_idx = int(rate_basis_idx)
    final_alt_fl = max(0, int(final_alt_fl))
    final_spd = max(0, int(final_spd))
    
    STATE.proc_generic_cfg.update({
        "flights": flights,
        "alt_fl": alt_fl,
        "mach": mach,
        "schedule_mode": schedule_mode,
        "rate_basis_idx": rate_basis_idx,
        "final_alt_fl": final_alt_fl,
        "final_spd": final_spd
    })
    
    mode_str = "schedule" if schedule_mode else "hourly rate"
    basis_str = "final waypoint" if rate_basis_idx else "initial waypoint"
    _echo_ok(f"Generic procedures: flights={flights}, alt={alt_fl}FL, mach={mach:.2f}, mode={mode_str}, basis={basis_str}, final_alt={final_alt_fl}FL, final_spd={final_spd}kt")
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
def SATG_PROC_CFG_GENERICRATE(proc_id: str, rate: float):
    """Configure Generic procedure spawn rate by waypoint identifier."""
    rate_val = max(0.0, float(rate))
    pid = proc_id.strip()
    if not pid:
        _echo_err("SATG_PROC_CFG_GENERICRATE: missing identifier")
        return False, ""
    
    pid_upper = pid.upper()
    current_basis = str(STATE.proc_generic_cfg.get("rate_basis", "initial")).lower()
    if current_basis not in ("initial", "final"):
        current_basis = "initial"
    
    # Store rate in the appropriate basis table
    rate_table = STATE.proc_generic_rates.setdefault("final" if current_basis == "final" else "initial", {})
    rate_table[pid_upper] = rate_val
    
    descriptor = "final waypoint" if current_basis == "final" else "initial waypoint"
    _echo_ok(f"Generic {descriptor} {pid_upper}: rate={rate_val:.1f} ac/h")
    return True, ""


@command
def SATG_PROC_TYPES_GENERIC(*types):
    """Configure aircraft types for generic procedures."""
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
        _echo_err("SATG_PROC_TYPES_GENERIC: no valid aircraft types found")
        return False, ""
    
    STATE.proc_generic_actypes = cleaned
    _echo_ok(f"Generic aircraft types set: {', '.join(cleaned)}")
    return True, ""


@command  
def SATG_PROC_TYPES_SID(*types):
    """Configure aircraft types for SID procedures."""
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
        _echo_err("SATG_PROC_TYPES_SID: no valid aircraft types found")
        return False, ""
    
    STATE.proc_sid_actypes = cleaned
    _echo_ok(f"SID aircraft types set: {', '.join(cleaned)}")
    return True, ""


@command
def SATG_PROC_TYPES_STAR(*types):
    """Configure aircraft types for STAR procedures."""
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
        _echo_err("SATG_PROC_TYPES_STAR: no valid aircraft types found")
        return False, ""
    
    STATE.proc_star_actypes = cleaned
    _echo_ok(f"STAR aircraft types set: {', '.join(cleaned)}")
    return True, ""


def _get_available_aircraft_types():
    """Get list of available aircraft types from the current performance model."""
    try:
        # Try to get from OpenAP performance model
        if hasattr(bs.traf.perf, 'coeff') and hasattr(bs.traf.perf.coeff, 'actypes_fixwing'):
            fixwing_types = sorted(bs.traf.perf.coeff.actypes_fixwing)
            rotor_types = sorted(bs.traf.perf.coeff.actypes_rotor) if hasattr(bs.traf.perf.coeff, 'actypes_rotor') else []
            return fixwing_types + rotor_types
        
        # Try BADA performance model
        elif hasattr(bs.traf.perf, 'synonym') and hasattr(bs.traf.perf.synonym, 'ap_info'):
            # BADA has synonym info - extract aircraft types
            types = []
            for synonym in bs.traf.perf.synonym.ap_info:
                if hasattr(synonym, 'accode'):
                    types.append(synonym.accode)
            return sorted(list(set(types)))  # Remove duplicates and sort
        
        # Try legacy performance model
        elif hasattr(bs.traf.perf, 'coeff') and hasattr(bs.traf.perf.coeff, 'atype'):
            return sorted(bs.traf.perf.coeff.atype)
        
        # Fallback to common aircraft types
        else:
            return ["A320", "A321", "A330", "A340", "A350", "A380", 
                   "B737", "B738", "B744", "B747", "B777", "B787", "B78X",
                   "CRJ2", "E190", "E195", "MD11", "MD80"]
    
    except Exception as e:
        # If anything fails, return common types
        return ["A320", "A321", "A330", "A340", "A350", "A380", 
               "B737", "B738", "B744", "B747", "B777", "B787", "B78X",
               "CRJ2", "E190", "E195", "MD11", "MD80"]


@command
def SATG_AVAILABLE_ACTYPES():
    """Get list of available aircraft types from performance model."""
    try:
        actypes = _get_available_aircraft_types()
        if actypes:
            # Format for display - group in rows of 8
            grouped = []
            for i in range(0, len(actypes), 8):
                grouped.append(", ".join(actypes[i:i+8]))
            
            result = f"Available aircraft types ({len(actypes)} total):\n" + "\n".join(grouped)
            _echo_ok(result)
            return True, "|".join(actypes)  # Return pipe-separated for GUI consumption
        else:
            _echo_err("No aircraft types available from performance model")
            return False, ""
    except Exception as e:
        _echo_err(f"Error getting aircraft types: {e}")
        return False, ""


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
                   seed: int = 0,
                   overwrite: int = 0):
    """
    Generate comprehensive procedure-based traffic scenarios with SID/STAR operations.
    
    This command function creates sophisticated air traffic scenarios incorporating
    Standard Instrument Departures (SIDs), Standard Terminal Arrival Routes (STARs),
    and generic procedural operations. The function combines loaded procedures with
    configured aircraft types, scheduling parameters, and operational constraints
    to generate realistic terminal area traffic scenarios.
    
    The scenario generation process integrates multiple procedure types:
    - SID procedures: Instrument departures from airports with runway-specific routing
    - STAR procedures: Standard arrivals with approach transitions and sequencing
    - Generic procedures: Enroute and terminal area operations with custom routing
    
    All procedures are validated for completeness, waypoint resolution, and aircraft
    compatibility before scenario generation. The function ensures proper coordination
    between different procedure types and realistic operational timing.
    
    Args:
        name (str): Output scenario filename (without .scn extension)
        n (int): Scenario duration multiplier or repetition count
        seed (int, optional): Random seed for reproducible scenario generation.
                             Defaults to 0 for deterministic scenarios
        overwrite (int, optional): File overwrite behavior (0=append if exists,
                                 1=overwrite existing file). Defaults to 0
    
    Returns:
        tuple: (success, message) indicating scenario generation success
    
    Examples:
        # Generate basic procedure scenario with default settings
        SATG_PROC_MAKE terminal_ops 1
        
        # Generate repeatable scenario with specific seed
        SATG_PROC_MAKE busy_terminal 2 seed=12345
        
        # Generate scenario with file overwrite enabled
        SATG_PROC_MAKE test_scenario 1 seed=42 overwrite=1
    
    Raises:
        ValidationError: When required procedure files are not loaded
        ConfigurationError: When SID procedures lack required ICAO codes
        FileError: When output file cannot be created or written
    
    Note:
        This function requires that procedures have been loaded using SATG_PROC_LOAD_PROC
        and properly configured with aircraft types, rates, and scheduling parameters.
        All SID procedures must have associated ICAO airport codes set via
        SATG_PROC_SET_ICAO before scenario generation can proceed.
    """
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
            # Write scenario header
            header = _generate_scenario_header("Procedural Traffic",
                n=n,
                sid_enabled="Yes" if STATE.proc_sid_info else "No",
                star_enabled="Yes" if STATE.proc_star_info else "No"
            )
            for line in header:
                f.write(f"{line}\n")
            
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")
            for w in STATE.proc_wpt_files:
                f.write(f"0:00:00.00>PCALL {_cmd_path(w)}\n")

        # Generic procedures
        gen_total_requested = max(0, int(gen_cfg.get("flights", 0)))
        use_generic_schedule = bool(gen_cfg.get("schedule_mode"))
        rate_basis_raw = str(gen_cfg.get("rate_basis_idx", 0))
        generic_rate_basis = "final" if int(rate_basis_raw) == 1 else "initial"
        generic_rate_table = {}
        if isinstance(STATE.proc_generic_rates, dict):
            generic_rate_table = STATE.proc_generic_rates.get(generic_rate_basis, {})
        fallback_generic_minsep = 90
        DEFAULT_GENERIC_RATE = 20  # 20 aircraft per hour default
        
        # Group generic procedures by waypoint (like STARs)
        generic_groups: Dict[str, List[str]] = {}
        path_to_group: Dict[str, str] = {}
        for proc_path in generic_paths:
            f1_name, _ = _proc_first_two_fixes(proc_path, set())
            group_key = f1_name.upper() if f1_name else os.path.splitext(os.path.basename(proc_path))[0].upper()
            path_to_group[proc_path] = group_key
            generic_groups.setdefault(group_key, []).append(proc_path)
        
        # Calculate group rates
        group_rates = {}
        for group_key in generic_groups:
            group_rates[group_key] = generic_rate_table.get(group_key, DEFAULT_GENERIC_RATE)
        
        group_next_time: Dict[str, float] = {}
        
        for i in range(gen_total_requested):
            if not generic_groups:
                break
                
            # Select procedure based on rates (like STAR logic)
            keys = list(generic_groups.keys())
            weights = [max(group_rates.get(key, DEFAULT_GENERIC_RATE), 0.0) for key in keys]
            total_weight = sum(weights)
            if total_weight <= 0.0:
                group_key = rng.choice(keys)
            else:
                group_key = _weighted_choice(rng, keys, weights)
            
            proc_path = rng.choice(generic_groups[group_key])
            proc_name = os.path.splitext(os.path.basename(proc_path))[0]

            # Calculate timing based on rate (like STAR procedures)
            rate = group_rates.get(group_key, DEFAULT_GENERIC_RATE)
            interval = 3600.0 / rate if rate > 0.0 else fallback_generic_minsep
            t0 = group_next_time.get(group_key, 0.0)
            group_next_time[group_key] = t0 + interval
            ts = _fmt_ts(t0)

            acid = _next_pr_acid()

            # Use unified coordinate resolution for both coordinate-based and waypoint-based procedures
            coord1 = _proc_unified_first_waypoint(proc_path, fix_db)
            coord2 = None
            
            if coord1:
                # Get second coordinate for heading calculation
                if _proc_is_coordinate_based(proc_path):
                    coord2 = _proc_first_two_coordinates(proc_path)[1]
                else:
                    # For waypoint-based, try to get second waypoint
                    fix_names = _proc_fix_sequence(proc_path)
                    if len(fix_names) >= 2:
                        coord2 = _resolve_fix_coord(fix_names[1], fix_db, coord1[0], coord1[1], proc_path=proc_path)
            
            if not coord1:
                _echo_err(f"{proc_name}: could not resolve first waypoint position."); return False, ""
            
            # Calculate heading
            if coord2:
                latA, lonA = coord1
                latB, lonB = coord2
                hdg0 = _bearing_deg(latA, lonA, latB, lonB)
            else:
                hdg0 = 0.0

            # Use STAR-like spawning: spawn directly at first waypoint
            # hdg0 is already calculated above as heading toward second waypoint
            
            # Get generic procedure configuration
            gen_alt_fl = max(0, int(gen_cfg.get("alt_fl", 360)))
            gen_mach = float(gen_cfg.get("mach", 0.79))
            gen_alt_ft = gen_alt_fl * 100
            actype = _proc_pick_actype("generic", rng)

            hdg_cmd = int(round(hdg0)) % 360
            
            # Always use coordinates for spawn position in CRE command
            # This ensures coordinate-based procedures spawn at actual coordinates, not generated waypoint names
            latA, lonA = coord1
            spawn_token = f"{latA:.6f},{lonA:.6f}"
            
            # Check for initial overrides to modify CRE command
            gen_override_initial_alt = gen_cfg.get("override_initial_alt", False)
            gen_override_initial_spd = gen_cfg.get("override_initial_spd", False)
            
            # Extract initial altitude and speed from procedure file
            proc_alt_ft, proc_spd_val = _proc_extract_initial_alt_spd(proc_path)
            
            # Determine altitude and speed for CRE command
            if gen_override_initial_alt:
                cre_alt_ft = gen_alt_ft  # Use GUI altitude
            else:
                cre_alt_ft = proc_alt_ft  # Use procedure file altitude (can be None)
                
            if gen_override_initial_spd:
                cre_mach_token = f"M{gen_mach:.2f}"  # Use GUI speed
            else:
                # Use procedure file speed (can be None)
                if proc_spd_val is not None:
                    if proc_spd_val < 1.0:  # Assume Mach number if < 1.0
                        cre_mach_token = f"M{proc_spd_val:.2f}"
            if gen_override_initial_spd:
                cre_mach_token = f"M{gen_mach:.2f}"  # Use GUI speed
            else:
                # Use procedure file speed (can be None)
                if proc_spd_val is not None:
                    if proc_spd_val < 1.0:  # Assume Mach number if < 1.0
                        cre_mach_token = f"M{proc_spd_val:.2f}"
                    else:  # Assume knots if >= 1.0
                        cre_mach_token = f"{int(proc_spd_val)}"
                else:
                    cre_mach_token = None
                
            # Handle placeholder case for partial overrides
            if (gen_override_initial_spd or proc_spd_val is not None) and (not gen_override_initial_alt and cre_alt_ft is None):
                # Only speed override/available - use placeholder for altitude
                cre_command = f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d} ,, {cre_mach_token}\n"
            elif (gen_override_initial_alt or cre_alt_ft is not None) and (not gen_override_initial_spd and cre_mach_token is None):
                # Only altitude override/available - use placeholder for speed
                cre_command = f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d} {cre_alt_ft} ,,\n"
            elif (gen_override_initial_alt or cre_alt_ft is not None) and (gen_override_initial_spd or cre_mach_token is not None):
                # Both available
                cre_command = f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d} {cre_alt_ft} {cre_mach_token}\n"
            else:
                # No overrides and no procedure values - use default behavior (don't specify alt/speed in CRE)
                cre_command = f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d}\n"
            
            f.write(cre_command)
            f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
            f.write(f"{ts}LNAV {acid} ON\n")
            f.write(f"{ts}VNAV {acid} ON\n")
            
            # Add final altitude and speed override commands if enabled
            gen_final_alt_fl = max(0, int(gen_cfg.get("final_alt_fl", 100)))
            gen_final_spd = max(0, int(gen_cfg.get("final_spd", 240)))
            gen_override_final_alt = gen_cfg.get("override_final_alt", False)
            gen_override_final_spd = gen_cfg.get("override_final_spd", False)
            
            if gen_override_final_alt or gen_override_final_spd:
                final_token = _proc_unified_waypoint_token(proc_path, is_first=False)
                if final_token:
                    if gen_override_final_alt and gen_final_alt_fl > 0:
                        final_alt_tok = _fmt_alt_token(gen_final_alt_fl)
                        f.write(f"{ts}{acid} AT {final_token} ALT {final_alt_tok}\n")
                    if gen_override_final_spd and gen_final_spd > 0:
                        f.write(f"{ts}{acid} AT {final_token} SPD {gen_final_spd}\n")
            
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

            actype = _proc_pick_actype("sid", rng)
            
            # Check for initial overrides - for SIDs, apply as commands after LNAV/VNAV
            sid_override_initial_alt = sid_cfg.get("override_initial_alt", False)
            sid_override_initial_spd = sid_cfg.get("override_initial_spd", False)
            
            # Extract initial altitude and speed from procedure file for potential commands
            proc_alt_ft, proc_spd_val = _proc_extract_initial_alt_spd(proc_path)
            
            # For SIDs: Always use simple CRE command (no altitude/speed in CRE)
            # Let aircraft take off naturally, then apply overrides as commands
            f.write(f"{ts}CRE {acid} {actype} {icao} {rw_tag}\n")
                
            f.write(f"{ts}ADDWPT {acid} {icao}/{rw_tag}\n")
            f.write(f"{ts}ADDWPT {acid} TAKEOFF\n")
            f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
            f.write(f"{ts}LNAV {acid} ON\n")
            f.write(f"{ts}VNAV {acid} ON\n")
            
            # Apply overrides as separate commands after LNAV/VNAV activation
            if sid_override_initial_alt:
                override_alt = int(sid_cfg.get('alt_ft', 5000))
                f.write(f"{ts}ALT {acid} {override_alt}\n")
            elif proc_alt_ft is not None:
                # Use procedure file altitude if available and no override
                f.write(f"{ts}ALT {acid} {int(proc_alt_ft)}\n")
                
            if sid_override_initial_spd:
                override_spd = int(sid_cfg.get('spd_kt', 250))
                f.write(f"{ts}SPD {acid} {override_spd}\n")
            elif proc_spd_val is not None:
                # Use procedure file speed if available and no override
                if proc_spd_val < 1.0:  # Mach number
                    f.write(f"{ts}SPD {acid} M{proc_spd_val:.2f}\n")
                else:  # Knots
                    f.write(f"{ts}SPD {acid} {int(proc_spd_val)}\n")
            
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

                    actype = _proc_pick_actype("sid", rng)
                    
                    # For SIDs, use simple CRE command and apply overrides as separate commands after LNAV/VNAV
                    f.write(f"{ts}CRE {acid} {actype} {icao} {rw_tag}\n")
                        
                    f.write(f"{ts}ADDWPT {acid} {icao}/{rw_tag}\n")
                    f.write(f"{ts}ADDWPT {acid} TAKEOFF\n")
                    f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
                    f.write(f"{ts}LNAV {acid} ON\n")
                    f.write(f"{ts}VNAV {acid} ON\n")
                    
                    # Apply SID overrides as separate commands after LNAV/VNAV activation
                    if sid_cfg.get("override_initial_alt", False):
                        override_alt = int(sid_cfg.get('alt_ft', 5000))
                        f.write(f"{ts}ALT {acid} {override_alt}\n")
                    
                    if sid_cfg.get("override_initial_spd", False):
                        override_spd = int(sid_cfg.get('spd_kt', 250))
                        f.write(f"{ts}SPD {acid} {override_spd}\n")
                    
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
        star_override_initial_alt = star_cfg.get("override_initial_alt", False)
        star_override_initial_spd = star_cfg.get("override_initial_spd", False)
        star_override_final_alt = star_cfg.get("override_final_alt", False)
        star_override_final_spd = star_cfg.get("override_final_spd", False)
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
            
            # Use unified coordinate resolution for both coordinate-based and waypoint-based procedures
            coord1 = _proc_unified_first_waypoint(proc_path, fix_db)
            if not coord1:
                _echo_err(f"{proc_name}: could not resolve first waypoint position."); return False, ""
            
            # Get second coordinate for heading calculation
            coord2 = None
            if _proc_is_coordinate_based(proc_path):
                coord2 = _proc_first_two_coordinates(proc_path)[1]
            else:
                if f2_up:
                    coord2 = _resolve_fix_coord(f2_up, fix_db, coord1[0], coord1[1])
                    if coord2:
                        fix_keys.add(f2_up)
            
            # Calculate heading
            if coord2:
                lat0, lon0 = coord1
                lat1, lon1 = coord2
                hdg0 = _bearing_deg(lat0, lon0, lat1, lon1)
            else:
                hdg0 = 0.0
            
            fix_keys.add(final_fix_up)
            ts = _fmt_ts(t_sec)
            acid = _next_pr_acid()
            actype = _proc_pick_actype("star", rng)
            hdg_cmd = int(round(hdg0)) % 360
            
            # Extract initial altitude and speed from procedure file
            proc_alt_ft, proc_spd_val = _proc_extract_initial_alt_spd(proc_path)
            
            # Check for initial overrides to modify CRE command
            if star_override_initial_alt:
                cre_alt_ft = star_alt_ft  # Use GUI altitude
            else:
                cre_alt_ft = proc_alt_ft  # Use procedure file altitude (can be None)
                
            if star_override_initial_spd:
                cre_mach_token = f"M{star_mach_val:.2f}"  # Use GUI speed
            else:
                # Use procedure file speed (can be None)
                if proc_spd_val is not None:
                    if proc_spd_val < 1.0:  # Mach number
                        cre_mach_token = f"M{proc_spd_val:.2f}"
                    else:  # Knots
                        cre_mach_token = f"{int(proc_spd_val)}"
                else:
                    cre_mach_token = None
            
            # Use unified spawn token (coordinates for coordinate-based, waypoint name for waypoint-based)
            if _proc_is_coordinate_based(proc_path):
                lat0, lon0 = coord1
                spawn_token = f"{lat0:.6f},{lon0:.6f}"
            else:
                spawn_token = f1_up
            
            # Handle placeholder case for partial overrides
            if (star_override_initial_spd or cre_mach_token is not None) and (not star_override_initial_alt and cre_alt_ft is None):
                # Only speed override/available - use placeholder for altitude
                cre_command = f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d} ,, {cre_mach_token}\n"
            elif (star_override_initial_alt or cre_alt_ft is not None) and (not star_override_initial_spd and cre_mach_token is None):
                # Only altitude override/available - use placeholder for speed
                cre_command = f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d} {cre_alt_ft} ,,\n"
            elif (star_override_initial_alt or cre_alt_ft is not None) and (star_override_initial_spd or cre_mach_token is not None):
                # Both available
                cre_command = f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d} {cre_alt_ft} {cre_mach_token}\n"
            else:
                # No overrides and no procedure values - use default behavior (don't specify alt/speed in CRE)
                cre_command = f"{ts}CRE {acid} {actype} {spawn_token} {hdg_cmd:03d}\n"
            
            f.write(cre_command)
            f.write(f"{ts}PCALL {_cmd_path(proc_path)} {acid}\n")
            f.write(f"{ts}LNAV {acid} ON\n")
            f.write(f"{ts}VNAV {acid} ON\n")
            if STATE.proc_destinations_enabled:
                dests = STATE.proc_destinations.get(proc_path)
                if dests:
                    dest_choice = rng.choice(dests)
                    f.write(f"{ts}DEST {acid} {dest_choice}\n")
            
            # Add final altitude/speed override commands if enabled
            if star_override_final_alt or star_override_final_spd:
                final_token = _proc_unified_waypoint_token(proc_path, is_first=False)
                if final_token:
                    if star_override_final_alt and star_final_fl > 0:
                        final_alt_tok = _fmt_alt_token(star_final_fl)
                        f.write(f"{ts}{acid} AT {final_token} ALT {final_alt_tok}\n")
                    if star_override_final_spd and star_final_spd > 0:
                        f.write(f"{ts}{acid} AT {final_token} SPD {star_final_spd}\n")
            
            final_hdg = None
            if pen_final_up:
                coord_pen = _resolve_fix_coord(pen_final_up, fix_db)
                coord_fin = _resolve_fix_coord(final_fix_up, fix_db)
                if coord_pen and coord_fin:
                    final_hdg = _bearing_deg(coord_pen[0], coord_pen[1], coord_fin[0], coord_fin[1])
            if final_hdg is None:
                final_hdg = hdg0
            final_hdg_cmd = int(round(final_hdg)) % 360
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
        "SATG - Synthetic Air Traffic Generator",
        "======================================",
        "",
        "SATG generates synthetic air traffic scenarios for BlueSky simulation.",
        "Supports conflict generation, historical traffic replay, and procedural operations.",
        "",
        "Random Conflicts (in circles or custom polygons)",
        "================================================",
        "SATG_RC_CIRCLE name N types lat lon radius mode altmode tcpa fl cas actypes [overwrite] [angle] [area_type] [polygon_name]",
        "",
        "Parameters:",
        "  - name: scenario filename (creates name.scn)",
        "  - N: number of conflicts to generate", 
        "  - types: headon,cross,overtake (comma-separated encounter types)",
        "  - lat,lon: center coordinates in decimal degrees",
        "  - radius: area radius in nautical miles",
        "  - mode: abs|rel|mix (absolute CPA/relative target-intruder/mixed)",
        "  - altmode: level|altcross|mix (same level/crossing altitudes/mixed)",
        "  - tcpa: time to closest approach in seconds (lo:hi range)",
        "  - fl: flight level range (lo:hi, e.g., 100:400)",
        "  - cas: calibrated airspeed in knots (lo:hi range)",
        "  - actypes: aircraft types (A320,B738,E190 - comma-separated)",
        "  - overwrite: 1=replace existing file, 0=append to existing",
        "  - angle: crossing angle range in degrees (lo:hi, only for crossing)",
        "  - area_type: circle|polygon (default: circle)",
        "  - polygon_name: name of custom polygon (requires geopandas)",
        "",
        "Geometric Conflicts",
        "===================",
        "SATG_GC_CONF HSEP VSEP",
        "  Set horizontal separation (nm) and vertical separation (feet)",
        "",
        "SATG_GC_RANGE fl=lo:hi cas=lo:hi", 
        "  Set flight level and airspeed ranges for aircraft generation",
        "",
        "SATG_GC_CRE name=scenario typ=types altmode=mode lat=lat lon=lon tcpa=seconds [options]",
        "  Create conflicts at specific coordinates or waypoints:",
        "  - typ: headon,cross,overtake (encounter types)",
        "  - altmode: level|altcross|mix",
        "  - lat=deg lon=deg OR wp=waypoint_name (conflict location)",
        "  - tcpa: time to closest point of approach in seconds",
        "  - angle: crossing angle in degrees (for crossing conflicts)",
        "  - actypes: aircraft type list (optional)",
        "  - overwrite: 0=append, 1=replace file",
        "",
        "SATG_GC_RUN name",
        "  Load and run the generated geometric conflict scenario",
        "",
        "Realistic Replay",
        "================",
        "SATG_RL_MAKE name overwrite",
        "  Generate scenario from previously loaded flight/track data",
        "",
        "SATG_RL_RUN name",
        "  Load and execute the realistic replay scenario",
        "",
        "Notes:",
        "  - Load CSV data using the GUI (flight data + track data files)",
        "  - Jitter and auto-delete settings configured via GUI interface",
        "  - Scenarios include realistic timing and route variations",
        "",
        "Procedural Traffic",
        "==================",
        "Setup Commands:",
        "  SATG_PROC_LOAD_WPT path_to_waypoints.scn",
        "",
        "  SATG_PROC_LOAD_PROC path_to_procedure.scn", 
        "  SATG_PROC_LOAD_CUSTOM (auto-load all files from procedures/ folder)",
        "  SATG_PROC_EXPORT_POLY polygon_name (export polygon coordinates for GUI)",
        "  SATG_PROC_CREATE_FROM_POLY polygon_name [procedure_name] (create procedure from polygon)",
        "  SATG_PROC_LOAD_FOR_EDIT procedure_name (load procedure for editing constraints)",
        "",
        "  SATG_PROC_SET_ICAO SID-XX-NAME AIRPORT_ICAO",
        "",
        "Configuration Commands:",
        "  SATG_PROC_CFG_GENERIC flights alt_fl mach schedule_mode rate_basis_idx final_alt_fl final_spd",
        "  SATG_PROC_CFG_GENERICRATE proc_name rate_per_hour",
        "  SATG_PROC_CFG_SID flights altitude_ft speed_kt",
        "  SATG_PROC_CFG_SIDRATE runway rate_per_hour",
        "  SATG_PROC_CFG_STAR flights minsep init_fl mach mode ratebasis final_fl final_spd",
        "  SATG_PROC_CFG_STARRATE proc_name rate_per_hour",
        "",
        "Traffic Scheduling:",
        "  SATG_PROC_CFG_STARSCHED proc_name start_min end_min cap1 cap2 ...",
        "  SATG_PROC_CFG_SIDSCHED runway start_min end_min cap1 cap2 ...",
        "  SATG_PROC_CLEAR_STARSCHED [proc_name]",
        "  SATG_PROC_CLEAR_SIDSCHED [runway]",
        "",
        "Generation:",
        "  SATG_PROC_MAKE name N seed overwrite",
        "  SATG_PROC_RUN name",
        "",
        "Custom Polygon Areas",
        "====================",
        "SATG_POLY_CREATE name lat1 lon1 lat2 lon2 [lat3 lon3 ...]",
        "SATG_POLY_LIST",
        "SATG_POLY_INFO name",
        "SATG_POLY_COORDS name", 
        "SATG_POLY_TEST polygon_name lat lon",
        "",
        "General",
        "=======",
        "- Scenario files written with .scn extension in base directory",
        "- PCALL commands use absolute paths for reliable file resolution",
        "- Callsigns auto-renamed when appending to prevent conflicts",
        "- Scenarios automatically sorted by time for proper execution",
        "- Seed values enable reproducible random generation",
        "",
        "Use SATG_HELP [topic] for focused help on specific commands.",
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

# ============================================================================
# SYNTHETIC TRAJECTORY COMMANDS (Historic Sampling)
# ============================================================================

# Global variable to store synthetic data
_synthetic_flights = []
_synthetic_points = []

def SATG_SYNTH_LOAD_DATA(flights_json: str, points_json: str) -> Tuple[bool, str]:
    """Load synthetic trajectory data (called from TraffixGen).
    
    Args:
        flights_json: JSON string containing synthetic flight data
        points_json: JSON string containing synthetic route point data
        
    Returns:
        Tuple of (success, message)
    """
    global _synthetic_flights, _synthetic_points
    
    try:
        import json
        
        # Parse JSON data
        flights_data = json.loads(flights_json)
        points_data = json.loads(points_json)
        
        # Store synthetic data
        _synthetic_flights = flights_data
        _synthetic_points = points_data
        
        print(f"Loaded {len(flights_data)} synthetic flights and {len(points_data)} synthetic route points")
        return True, f"Loaded {len(flights_data)} synthetic flights"
        
    except Exception as e:
        error_msg = f"Error loading synthetic data: {e}"
        print(error_msg)
        return False, error_msg

@command
def SATG_SYNTH_STATUS():
    """SATG_SYNTH_STATUS
    Show status of synthetic trajectory data.
    """
    global _synthetic_flights, _synthetic_points
    
    status_lines = []
    status_lines.append("=== Synthetic Trajectory Status ===")
    status_lines.append(f"Synthetic Flights Loaded: {len(_synthetic_flights)}")
    status_lines.append(f"Synthetic Route Points: {len(_synthetic_points)}")
    
    if _synthetic_flights:
        # Show sample data info
        sample_flight = _synthetic_flights[0]
        status_lines.append(f"Sample Flight: {sample_flight.get('callsign', 'N/A')} ({sample_flight.get('origin', 'N/A')} -> {sample_flight.get('destination', 'N/A')})")
        status_lines.append(f"Aircraft Type: {sample_flight.get('aircraft_type', 'N/A')}")
        
        # Show OD pairs
        od_pairs = set(f"{flight.get('origin', 'N/A')}-{flight.get('destination', 'N/A')}" for flight in _synthetic_flights)
        status_lines.append(f"Unique OD Pairs: {len(od_pairs)}")
        
        # Show aircraft types
        ac_types = set(flight.get('aircraft_type', 'N/A') for flight in _synthetic_flights)
        status_lines.append(f"Aircraft Types: {len(ac_types)}")
    
    status_message = "\n".join(status_lines)
    stack.stack(f"ECHO {status_message}")
    return True, ""

def SATG_SYNTH_CREATE_SCENARIO(scenario_name: str) -> bool:
    """Create a scenario file from synthetic trajectory data.
    
    Args:
        scenario_name: Name for the scenario file
        
    Returns:
        bool: True if scenario created successfully
    """
    global _synthetic_flights, _synthetic_points
    
    if not _synthetic_flights or not _synthetic_points:
        print("Error: No synthetic data loaded. Use TraffixGen Historic Sampling first.")
        return False
    
    try:
        # Create scenario file path
        if not os.path.isdir(STATE.scn_dir):
            os.makedirs(STATE.scn_dir, exist_ok=True)
        
        scenario_path = os.path.join(STATE.scn_dir, f"{scenario_name}.scn")
        
        # Use the same scenario generation logic as Realistic Replay
        _write_historic_sampling_scn(scenario_path, append=False)
        print(f"Created Historic Sampling scenario: {scenario_path}")
        return True
        
    except Exception as e:
        print(f"Error creating Historic Sampling scenario: {e}")
        return False


def _write_historic_sampling_scn(out_path: str, append: bool = False):
    """
    Generate BlueSky scenario file from synthetic flight data using Historic Sampling methodology.
    
    Creates a complete BlueSky scenario (.scn) file from synthetic flight trajectories generated
    by the TraffixGen ML models. Uses the same proven logic as Realistic Replay but operates
    on ML-generated synthetic data instead of historical EUROCONTROL data.
    
    The function processes synthetic flights and flight points to create time-normalized
    aircraft with realistic:
    - Initial conditions (position, altitude, speed, heading)
    - Waypoint sequences with appropriate timing
    - Speed and altitude commands for takeoff scenarios  
    - LNAV/VNAV autopilot activation with proper delays
    - Flight plan integration and conflict potential
    
    Args:
        out_path (str): Absolute file path where the scenario file will be written.
                       Should have .scn extension for BlueSky compatibility.
        append (bool, optional): If True, appends to existing file. If False (default),
                               creates new file or overwrites existing one.
    
    Returns:
        None: Writes scenario file directly. Success/failure indicated by print statements.
    
    Raises:
        Exception: Various exceptions possible from file I/O, data processing, or
                  coordinate calculations. All exceptions caught and reported via print.
    
    Note:
        Requires synthetic data to be loaded via TraffixGen Historic Sampling first.
        The synthetic data must be exported to the Realistic Replay data pipeline
        before calling this function. Uses same aircraft generation algorithms as
        proven Realistic Replay system for maximum compatibility and reliability.
        
    Dependencies:
        - TraffixGen plugin with loaded synthetic data
        - Global _synthetic_flights and _synthetic_points data structures  
        - BlueSky coordinate and timing utilities
        
    Examples:
        >>> _write_historic_sampling_scn("/path/to/scenario.scn", append=False)
        [Output: Creates complete scenario file with synthetic traffic]
        
        >>> _write_historic_sampling_scn("/path/to/scenario.scn", append=True) 
        [Output: Adds synthetic traffic to existing scenario]
    """
    # Use the same data source as Realistic Replay (STATE.flights and STATE.base_points)
    # The synthetic data was already exported to these variables by traffixgen_export_synthetic_to_satg
    
    mode = "a" if append else "w"
    with open(out_path, mode, encoding="utf-8") as f:
        if not append:
            # Write scenario header (same format as Realistic Replay)
            header = _generate_scenario_header("Historic Sampling",
                jitter_enabled="No",  # Historic Sampling doesn't use jitter
                autodel_enabled="Yes"  # Always enable auto-delete for Historic Sampling
            )
            for line in header:
                f.write(f"{line}\n")
            
            # Use same basic commands as Realistic Replay (no extra commands)
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")
        
        # Use the same data processing pipeline as Realistic Replay
        points = _get_points_for_run()  # Use same function as Realistic Replay

        # When appending, avoid duplicate callsigns (same logic as Realistic Replay)
        used = _scan_existing_acids(out_path) if append else set()
        name_map = {}  # original_acid -> new_acid

        # Compute deterministic mapping (same logic as Realistic Replay)
        for acid in STATE.flights.keys():
            meta = STATE.flights[acid]
            # Prefer callsign from metadata if available, otherwise use acid
            preferred_name = meta.get('Callsign', acid) if meta.get('Callsign') else acid
            
            # Ensure the preferred name is unique
            final_name = preferred_name
            if final_name in used or final_name in name_map.values():
                final_name = _next_unique_acid(preferred_name, used | set(name_map.values()))
            
            name_map[acid] = final_name
            used.add(final_name)

        # Generate aircraft using the pre-computed unique name mapping
        for acid, meta in STATE.flights.items():
            # Use the pre-computed unique name from name_map
            acid_out = name_map.get(acid, acid)

            if acid not in points or not points[acid]:
                continue
            segs = points[acid]; r0 = segs[0]; last = segs[-1]
            t0 = timedelta(seconds=r0['t']); stamp0 = _stamp(t0)
            fl0, lat0, lon0 = r0['fl'], r0['lat'], r0['lon']
            cas0 = _gs_to_cas_kt(r0['gs'], fl0)
            
            # Calculate initial heading (same logic as Realistic Replay)
            if len(segs) > 2:
                # Use third waypoint since second is often duplicate
                target_point = segs[2]
                lat1, lon1 = target_point['lat'], target_point['lon']
                # Calculate bearing from lat0,lon0 to lat1,lon1 (same calculation as Realistic Replay)
                lat0_rad = math.radians(lat0)
                lat1_rad = math.radians(lat1)
                dlon_rad = math.radians(lon1 - lon0)
                
                y = math.sin(dlon_rad) * math.cos(lat1_rad)
                x = math.cos(lat0_rad) * math.sin(lat1_rad) - math.sin(lat0_rad) * math.cos(lat1_rad) * math.cos(dlon_rad)
                bearing_rad = math.atan2(y, x)
                bearing_deg = math.degrees(bearing_rad)
                hdg0 = int((bearing_deg + 360) % 360)  # Normalize to 0-359
            elif len(segs) > 1:
                # Fallback to second waypoint if no third available
                next_point = segs[1]
                lat1, lon1 = next_point['lat'], next_point['lon']
                # Calculate bearing from lat0,lon0 to lat1,lon1 (same calculation as Realistic Replay)
                lat0_rad = math.radians(lat0)
                lat1_rad = math.radians(lat1)
                dlon_rad = math.radians(lon1 - lon0)
                
                y = math.sin(dlon_rad) * math.cos(lat1_rad)
                x = math.cos(lat0_rad) * math.sin(lat1_rad) - math.sin(lat0_rad) * math.cos(lat1_rad) * math.cos(dlon_rad)
                bearing_rad = math.atan2(y, x)
                bearing_deg = math.degrees(bearing_rad)
                hdg0 = int((bearing_deg + 360) % 360)  # Normalize to 0-359
            else:
                hdg0 = int(r0['hdg']) if not math.isnan(r0['hdg']) else 0
            
            actype = meta.get('AC Type',''); alt_ft0 = int(fl0) * 100

            # Create aircraft (same format as Realistic Replay)
            f.write(f"{stamp0}CRE {acid_out},{actype},{lat0:.6f},{lon0:.6f},{hdg0:03d},{alt_ft0},{cas0:.1f}\n")

            # Auto-delete logic (same as Realistic Replay)
            last_is_landing = int(last['fl']) == 0
            # For Historic Sampling: always delete landing aircraft, or when auto-delete is enabled for others
            trigger_on_last = True or last_is_landing  # Always auto-delete for Historic Sampling

            pen_wptname = None; last_wptname = None
            if trigger_on_last:
                last_wptname = _sanitize_name(f"{acid_out}_DEST")
                f.write(f"{stamp0}DEFWPT {last_wptname},{last['lat']:.6f},{last['lon']:.6f}\n")
            if last_is_landing and len(segs) >= 2:
                pen = segs[-2]
                pen_wptname = _sanitize_name(f"{acid_out}_APP")
                f.write(f"{stamp0}DEFWPT {pen_wptname},{pen['lat']:.6f},{pen['lon']:.6f}\n")

            # Add waypoints (same logic as Realistic Replay)
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

            # Robust takeoff logic (same logic as Realistic Replay)
            if cas0 <= 0 or alt_ft0 <= 0:
                
                # Find first waypoint with non-zero altitude (same logic as Realistic Replay)
                first_airborne_waypoint = None
                first_airborne_index = -1
                
                for idx, waypoint in enumerate(segs):
                    wp_fl = waypoint.get('fl', 0)
                    if wp_fl > 0:  # Found first waypoint above ground
                        first_airborne_waypoint = waypoint
                        first_airborne_index = idx
                        break
                
                if first_airborne_waypoint:
                    try:
                        # Use the first airborne waypoint for realistic initial climb conditions
                        target_gs = first_airborne_waypoint.get('gs', 0)
                        target_fl = first_airborne_waypoint.get('fl', 0)
                        target_cas = _gs_to_cas_kt(target_gs, target_fl)
                        
                        # Set realistic takeoff/climb conditions (same logic as Realistic Replay)
                        if cas0 <= 0:
                            if target_cas > 0:
                                # Use actual climb speed from data
                                initial_speed = min(max(target_cas, 160), 250)  # Realistic takeoff/climb speed range
                                f.write(f"{stamp0}SPD {acid_out} {initial_speed:.0f}\n")
                            else:
                                # Use realistic takeoff speed
                                initial_speed = 180  # Typical takeoff/initial climb speed
                                f.write(f"{stamp0}SPD {acid_out} {initial_speed}\n")
                                
                        if alt_ft0 <= 0:
                            if target_fl > 0:
                                # Use the first climbing altitude as target
                                f.write(f"{stamp0}ALT {acid_out} FL{target_fl:03.0f}\n")
                            else:
                                # Use realistic initial climb altitude
                                initial_alt = "FL050"  # Typical initial climb clearance
                                f.write(f"{stamp0}ALT {acid_out} {initial_alt}\n")
                                
                    except Exception as e:
                        # Use realistic takeoff defaults as fallback
                        if cas0 <= 0:
                            takeoff_speed = 180
                            f.write(f"{stamp0}SPD {acid_out} {takeoff_speed}\n")
                        if alt_ft0 <= 0:
                            takeoff_alt = "FL050"
                            f.write(f"{stamp0}ALT {acid_out} {takeoff_alt}\n")
                else:
                    # All waypoints are at ground level - use realistic takeoff values
                    if cas0 <= 0:
                        takeoff_speed = 180  # Realistic takeoff speed
                        f.write(f"{stamp0}SPD {acid_out} {takeoff_speed}\n")
                    if alt_ft0 <= 0:
                        takeoff_alt = "FL050"  # Realistic initial climb clearance
                        f.write(f"{stamp0}ALT {acid_out} {takeoff_alt}\n")

            # Write LNAV/VNAV commands (same logic as Realistic Replay)
            if alt_ft0 <= 0:
                # Aircraft starts on ground (takeoff) - apply 30 second delay
                t0_plus_30 = timedelta(seconds=r0['t'] + 30)
                stamp_lnav_vnav = _stamp(t0_plus_30)
            else:
                # Aircraft starts airborne - no delay needed
                stamp_lnav_vnav = stamp0
            
            f.write(f"{stamp_lnav_vnav}LNAV {acid_out} ON\n")
            f.write(f"{stamp_lnav_vnav}VNAV {acid_out} ON\n")
            
            # Landing and deletion commands (same logic as Realistic Replay)
            if last_is_landing and pen_wptname:
                f.write(f"{stamp0}{acid_out} AT {pen_wptname} DO {acid_out} ALT 0\n")
            if trigger_on_last and last_wptname:
                f.write(f"{stamp0}{acid_out} AT {last_wptname} DO DEL {acid_out}\n")
    
    # Sort scenario file (same as Realistic Replay)
    _sort_scn_file(out_path)


def _get_historic_sampling_points_for_run():
    """Convert synthetic points data to same format as Realistic Replay points.
    
    This follows the exact same logic as _get_points_for_run() but for synthetic data.
    """
    global _synthetic_points
    
    if not _synthetic_points:
        return {}
    
    # Build points dictionary in same format as Realistic Replay
    pts = {}
    for point in _synthetic_points:
        callsign = point.get('Callsign', point.get('ECTRL ID', 'UNKNOWN'))
        if callsign not in pts:
            pts[callsign] = []
        
        # Convert to exact same format as Realistic Replay _build_base_points
        converted_point = {
            'seq': int(point.get('Sequence Number', 0)),
            't': 0,  # TODO: Will be fixed when time distribution is implemented
            'fl': float(point.get('Flight Level', 0)),
            'lat': float(point.get('Latitude', 0)),
            'lon': float(point.get('Longitude', 0)),
            'gs': float(point.get('ground_speed', 200)),  # Use correct field name
            'hdg': float(point.get('heading', 0))         # Use correct field name
        }
        pts[callsign].append(converted_point)
    
    # Sort points by sequence for each aircraft (same as Realistic Replay)
    for callsign in pts:
        pts[callsign].sort(key=lambda r: r['seq'])
    
    return pts


def _get_historic_sampling_flights_for_run():
    """Convert synthetic flights data to same format as Realistic Replay flights.
    
    This follows the exact same logic as STATE.flights structure.
    """
    global _synthetic_flights
    
    if not _synthetic_flights:
        return {}
    
    # Build flights dictionary in same format as Realistic Replay
    flights = {}
    for flight in _synthetic_flights:
        callsign = flight.get('Callsign', flight.get('ECTRL ID', 'UNKNOWN'))
        flights[callsign] = {
            'AC Type': flight.get('AC Type', flight.get('aircraft_type', 'A320')),
            'ADEP': flight.get('ADEP', flight.get('origin', '')),
            'ADES': flight.get('ADES', flight.get('destination', '')),
            'Callsign': callsign,
            'AC Operator': flight.get('AC Operator', 'SYN')
        }
    
    return flights


def _calculate_bearing(lat1, lon1, lat2, lon2):
    """Calculate bearing between two points (same as Realistic Replay logic)."""
    import math
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon_rad = math.radians(lon2 - lon1)
    
    y = math.sin(dlon_rad) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)
    bearing_rad = math.atan2(y, x)
    bearing_deg = math.degrees(bearing_rad)
    return int((bearing_deg + 360) % 360)  # Normalize to 0-359


@command  
def SATG_SYNTH_CREATE(scenario_name: str):
    """SATG_SYNTH_CREATE scenario_name
    Create a scenario file from loaded synthetic trajectory data.
    """
    if not scenario_name.strip():
        _echo_err("Usage: SATG_SYNTH_CREATE scenario_name")
        return False, ""
    
    success = SATG_SYNTH_CREATE_SCENARIO(scenario_name.strip())
    
    if success:
        stack.stack(f"ECHO Created synthetic scenario: {scenario_name}")
        return True, ""
    else:
        _echo_err(f"Failed to create synthetic scenario: {scenario_name}")
        return False, ""

@command
def SATG_SYNTH_RUN(scenario_name: str):
    """SATG_SYNTH_RUN scenario_name  
    Create and run a synthetic trajectory scenario.
    """
    if not scenario_name.strip():
        _echo_err("Usage: SATG_SYNTH_RUN scenario_name")
        return False, ""
    
    # First create the scenario
    success = SATG_SYNTH_CREATE_SCENARIO(scenario_name.strip())
    
    if success:
        # Then run it
        scenario_path = os.path.join(STATE.scn_dir, f"{scenario_name.strip()}.scn")
        stack.stack(f"IC {_cmd_path(scenario_path)}")
        stack.stack(f"ECHO Running synthetic scenario: {scenario_name}")
        return True, ""
    else:
        _echo_err(f"Failed to create/run synthetic scenario: {scenario_name}")
        return False, ""


# ================= Historic Sampling Scenario Generation =================
# These functions follow the same proven logic as Realistic Replay (_write_rl_scn)
# but are specifically designed for Historic Sampling synthetic data

def SATG_HS_MAKE(name: str) -> bool:
    """Create Historic Sampling scenario file using synthetic data from TraffixGen."""
    global _synthetic_flights, _synthetic_points
    
    # Check for synthetic data first (this is the preferred source for Historic Sampling)
    if _synthetic_flights and _synthetic_points:
        print(f"[SATG HS] Using synthetic data: {len(_synthetic_flights)} flights, {len(_synthetic_points)} points")
        flights_data = _synthetic_flights
        points_data = _synthetic_points
    elif STATE.loaded_ok and STATE.flights and STATE.base_points:
        print("[SATG HS] Using realistic replay data")
        flights_data = STATE.flights
        points_data = STATE.base_points
    else:
        print("[SATG HS] Error: No data available. Either load synthetic data via TraffixGen or historic data via Realistic Replay.")
        return False
    
    try:
        # Create scenario file path (same as Realistic Replay)
        if not os.path.isdir(STATE.scn_dir):
            os.makedirs(STATE.scn_dir, exist_ok=True)
        
        scn_path = os.path.join(STATE.scn_dir, f"{name}.scn")
        
        # Use the EXACT SAME scenario generation logic as Realistic Replay
        # This ensures identical command structure, ordering, and logic
        print("[SATG HS] Using proven Realistic Replay scenario generation logic...")
        
        # Write scenario file with Historic Sampling header but Realistic Replay logic
        mode = "w"  # Never append for Historic Sampling
        with open(scn_path, mode, encoding="utf-8") as f:
            # Write Historic Sampling header
            header = _generate_scenario_header("Historic Sampling",
                jitter_enabled="No",   # Historic Sampling doesn't use jitter
                autodel_enabled="Yes"  # Always enable auto-delete for Historic Sampling
            )
            for line in header:
                f.write(f"{line}\n")
            
            # Use same basic commands as Realistic Replay (no extra commands)
            f.write("0:00:00.00>HOLD\n")
            f.write("0:00:00.00>ASAS ON\n")
        
        # Convert synthetic data to the same format as Realistic Replay
        print("[SATG HS] Converting synthetic data to scenario format...")
        
        # Use the same _build_base_points function that Realistic Replay uses
        points = _build_base_points(points_data)
        
        # Create flights dictionary in the same format as Realistic Replay
        flights = {}
        for flight in flights_data:
            callsign = flight.get('Callsign', flight.get('ECTRL ID', 'UNKNOWN'))
            # Match the exact format used by Realistic Replay
            flights[callsign] = {
                'AC Type': flight.get('AC Type', ''),
                'ADEP': flight.get('ADEP', ''),
                'ADES': flight.get('ADES', ''),
                'Callsign': flight.get('Callsign', callsign),  # Keep original data for reference
                'Synthetic': flight.get('Synthetic', True)
            }
            
        print(f"[SATG HS] Prepared {len(flights)} flights and {len(points)} point groups")

        # When appending, avoid duplicate callsigns (same logic as Realistic Replay)
        used = _scan_existing_acids(scn_path)  # Scan the file we just created
        name_map = {}  # original_acid -> new_acid

        # Compute deterministic mapping (same logic as Realistic Replay)
        for acid in flights.keys():
            meta = flights[acid]
            # Prefer callsign from metadata if available, otherwise use acid
            preferred_name = meta.get('Callsign', acid) if meta.get('Callsign') else acid
            
            # Ensure the preferred name is unique
            final_name = preferred_name
            if final_name in used or final_name in name_map.values():
                final_name = _next_unique_acid(preferred_name, used | set(name_map.values()))
            
            name_map[acid] = final_name
            used.add(final_name)

        # Generate aircraft using the exact same logic as Realistic Replay with time diversity
        aircraft_list = list(flights.items())
        
        # Step 1: Collect all departure times for normalization
        departure_times = []
        valid_aircraft = []
        for acid, meta in aircraft_list:
            if acid in points and points[acid]:
                segs = points[acid]
                r0 = segs[0]
                departure_times.append(r0['t'])
                valid_aircraft.append((acid, meta))
        
        # Step 2: Find earliest departure time for normalization
        if departure_times:
            earliest_departure = min(departure_times)
        else:
            earliest_departure = 0
        
        with open(scn_path, "a", encoding="utf-8") as f:
            for aircraft_idx, (acid, meta) in enumerate(valid_aircraft):
                # Use the pre-computed unique name from name_map
                acid_out = name_map.get(acid, acid)

                segs = points[acid]; r0 = segs[0]; last = segs[-1]
                
                # Step 3: Use normalized departure time (preserve distribution-based timing)
                base_time = r0['t']
                adjusted_time = base_time - earliest_departure  # Normalize to start at 0
                
                t0 = timedelta(seconds=adjusted_time); stamp0 = _stamp(t0)
                fl0, lat0, lon0 = r0['fl'], r0['lat'], r0['lon']
                cas0 = _gs_to_cas_kt(r0['gs'], fl0)
                
                # Calculate initial heading (same calculation as Realistic Replay)
                if len(segs) > 2:
                    # Use third waypoint since second is often duplicate
                    target_point = segs[2]
                    lat1, lon1 = target_point['lat'], target_point['lon']
                    # Calculate bearing from lat0,lon0 to lat1,lon1 (same calculation as Realistic Replay)
                    lat0_rad = math.radians(lat0)
                    lat1_rad = math.radians(lat1)
                    dlon_rad = math.radians(lon1 - lon0)
                    
                    y = math.sin(dlon_rad) * math.cos(lat1_rad)
                    x = math.cos(lat0_rad) * math.sin(lat1_rad) - math.sin(lat0_rad) * math.cos(lat1_rad) * math.cos(dlon_rad)
                    bearing_rad = math.atan2(y, x)
                    bearing_deg = math.degrees(bearing_rad)
                    hdg0 = int((bearing_deg + 360) % 360)  # Normalize to 0-359
                elif len(segs) > 1:
                    # Fallback to second waypoint if no third available
                    next_point = segs[1]
                    lat1, lon1 = next_point['lat'], next_point['lon']
                    # Calculate bearing from lat0,lon0 to lat1,lon1 (same calculation as Realistic Replay)
                    lat0_rad = math.radians(lat0)
                    lat1_rad = math.radians(lat1)
                    dlon_rad = math.radians(lon1 - lon0)
                    
                    y = math.sin(dlon_rad) * math.cos(lat1_rad)
                    x = math.cos(lat0_rad) * math.sin(lat1_rad) - math.sin(lat0_rad) * math.cos(lat1_rad) * math.cos(dlon_rad)
                    bearing_rad = math.atan2(y, x)
                    bearing_deg = math.degrees(bearing_rad)
                    hdg0 = int((bearing_deg + 360) % 360)  # Normalize to 0-359
                else:
                    hdg0 = int(r0['hdg']) if not math.isnan(r0['hdg']) else 0
                
                actype = meta.get('AC Type',''); alt_ft0 = int(fl0) * 100

                # Create aircraft (same format as Realistic Replay)
                f.write(f"{stamp0}CRE {acid_out},{actype},{lat0:.6f},{lon0:.6f},{hdg0:03d},{alt_ft0},{cas0:.1f}\n")

                # Auto-delete logic (same as Realistic Replay)
                last_is_landing = int(last['fl']) == 0
                # For Historic Sampling: always delete landing aircraft, or when auto-delete is enabled for others
                trigger_on_last = True or last_is_landing  # Always auto-delete for Historic Sampling

                pen_wptname = None; last_wptname = None
                if trigger_on_last:
                    last_wptname = _sanitize_name(f"{acid_out}_DEST")
                    f.write(f"{stamp0}DEFWPT {last_wptname},{last['lat']:.6f},{last['lon']:.6f}\n")
                if last_is_landing and len(segs) >= 2:
                    pen = segs[-2]
                    pen_wptname = _sanitize_name(f"{acid_out}_APP")
                    f.write(f"{stamp0}DEFWPT {pen_wptname},{pen['lat']:.6f},{pen['lon']:.6f}\n")

                # Add waypoints (same logic as Realistic Replay)
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

                # Robust takeoff logic (same logic as Realistic Replay)
                # Use proper thresholds: cas < 50 knots or alt <= 0 feet (ground level)
                needs_spd = cas0 < 50  # Very low speed indicates aircraft needs initial speed
                needs_alt = alt_ft0 <= 0  # Ground level or below indicates aircraft needs initial climb
                
                if needs_spd or needs_alt:
                    
                    # Find first waypoint with non-zero altitude (same logic as Realistic Replay)
                    first_airborne_waypoint = None
                    first_airborne_index = -1
                    
                    for idx, waypoint in enumerate(segs):
                        wp_fl = waypoint.get('fl', 0)
                        if wp_fl > 0:  # Found first waypoint above ground
                            first_airborne_waypoint = waypoint
                            first_airborne_index = idx
                            break
                    
                    if first_airborne_waypoint:
                        try:
                            # Use the first airborne waypoint for realistic initial climb conditions
                            target_gs = first_airborne_waypoint.get('gs', 0)
                            target_fl = first_airborne_waypoint.get('fl', 0)
                            target_cas = _gs_to_cas_kt(target_gs, target_fl)
                            
                            # Set realistic takeoff/climb conditions (same logic as Realistic Replay)
                            if needs_spd:
                                if target_cas > 50:  # Use target speed if it's reasonable
                                    # Use actual climb speed from data
                                    initial_speed = min(max(target_cas, 160), 250)  # Realistic takeoff/climb speed range
                                    f.write(f"{stamp0}SPD {acid_out} {initial_speed:.0f}\n")
                                else:
                                    # Use realistic takeoff speed
                                    initial_speed = 180  # Typical takeoff/initial climb speed
                                    f.write(f"{stamp0}SPD {acid_out} {initial_speed}\n")
                                    
                            if needs_alt:
                                if target_fl > 0:
                                    # Use the first climbing altitude as target
                                    f.write(f"{stamp0}ALT {acid_out} FL{target_fl:03.0f}\n")
                                else:
                                    # Use realistic initial climb altitude
                                    initial_alt = "FL050"  # Typical initial climb clearance
                                    f.write(f"{stamp0}ALT {acid_out} {initial_alt}\n")
                                    
                        except Exception as e:
                            # Use realistic takeoff defaults as fallback
                            if needs_spd:
                                takeoff_speed = 180
                                f.write(f"{stamp0}SPD {acid_out} {takeoff_speed}\n")
                            if needs_alt:
                                takeoff_alt = "FL050"
                                f.write(f"{stamp0}ALT {acid_out} {takeoff_alt}\n")
                    else:
                        # All waypoints are at ground level - use realistic takeoff values
                        if needs_spd:
                            takeoff_speed = 180  # Realistic takeoff speed
                            f.write(f"{stamp0}SPD {acid_out} {takeoff_speed}\n")
                        if needs_alt:
                            takeoff_alt = "FL050"  # Realistic initial climb clearance
                            f.write(f"{stamp0}ALT {acid_out} {takeoff_alt}\n")

                # Write LNAV/VNAV commands (same logic as Realistic Replay)
                if alt_ft0 <= 0:
                    # Aircraft starts on ground (takeoff) - apply 30 second delay
                    t0_plus_30 = timedelta(seconds=adjusted_time + 30)
                    stamp_lnav_vnav = _stamp(t0_plus_30)
                else:
                    # Aircraft starts airborne - no delay needed
                    stamp_lnav_vnav = stamp0
                
                f.write(f"{stamp_lnav_vnav}LNAV {acid_out} ON\n")
                f.write(f"{stamp_lnav_vnav}VNAV {acid_out} ON\n")
                
                # Landing and deletion commands (same logic as Realistic Replay)
                if last_is_landing and pen_wptname:
                    f.write(f"{stamp0}{acid_out} AT {pen_wptname} DO {acid_out} ALT 0\n")
                if trigger_on_last and last_wptname:
                    f.write(f"{stamp0}{acid_out} AT {last_wptname} DO DEL {acid_out}\n")
        
        # Sort scenario file by timestamp (same as Realistic Replay)
        _sort_scn_file(scn_path)
        
        print(f"[SATG HS] Created scenario: {scn_path}")
        return True
        
    except Exception as e:
        print(f"[SATG HS] Error creating scenario: {e}")
        return False


def SATG_HS_RUN(name: str) -> bool:
    """Load and run Historic Sampling scenario (same pattern as Realistic Replay)."""
    try:
        # Create scenario first
        if not SATG_HS_MAKE(name):
            return False
        
        # Load scenario (same as Realistic Replay)
        STATE.ic_called = ""
        scenario_path = os.path.join(STATE.scn_dir, f"{name}.scn")
        stack.stack(f"IC {_cmd_path(scenario_path)}")
        print(f"[SATG HS] Loading scenario: {scenario_path}")
        return True
        
    except Exception as e:
        print(f"[SATG HS] Error running scenario: {e}")
        return False


# Backward compatibility wrappers
def SATG_SYNTH_MAKE(scenario_name: str) -> bool:
    """Wrapper for backward compatibility - delegates to Historic Sampling."""
    return SATG_HS_MAKE(scenario_name)


# ------------------- Plugin init -------------------- #
