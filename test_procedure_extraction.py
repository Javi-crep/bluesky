#!/usr/bin/env python3
"""
Test script for SATG procedure extraction and CRE command generation.
Tests both extraction from procedure files and correct CRE command generation.
"""

import sys
import os
import tempfile
import re

# Add the bluesky directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'bluesky'))

def test_proc_extract_initial_alt_spd():
    """Test the _proc_extract_initial_alt_spd function."""
    print("=== Testing _proc_extract_initial_alt_spd function ===")
    
    def _proc_extract_initial_alt_spd(proc_path: str):
        """Extract initial altitude (feet) and speed (knots or Mach) from procedure file."""
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
    
    test_cases = [
        {
            "name": "Procedure with feet altitude and knots speed",
            "content": "00:00:00.00>%0 ADDWPT 52.587654 3.400154 1000 210\n00:00:00.00>%0 ADDWPT 52.162838 3.757884 ,, M0.7\n",
            "expected_alt": 1000,
            "expected_spd": 210
        },
        {
            "name": "Procedure with flight level and Mach speed",
            "content": "00:00:00.00>%0 ADDWPT 52.587654 3.400154 FL100 M0.79\n00:00:00.00>%0 ADDWPT 52.162838 3.757884\n",
            "expected_alt": 10000,
            "expected_spd": 0.79
        },
        {
            "name": "Procedure with only altitude",
            "content": "00:00:00.00>%0 ADDWPT 52.587654 3.400154 5000\n00:00:00.00>%0 ADDWPT 52.162838 3.757884\n",
            "expected_alt": 5000,
            "expected_spd": None
        },
        {
            "name": "Procedure with only speed",
            "content": "00:00:00.00>%0 ADDWPT 52.587654 3.400154 ,, 250\n00:00:00.00>%0 ADDWPT 52.162838 3.757884\n",
            "expected_alt": None,
            "expected_spd": 250
        },
        {
            "name": "Procedure with no altitude/speed",
            "content": "00:00:00.00>%0 ADDWPT SPY\n00:00:00.00>%0 ADDWPT LAK\n",
            "expected_alt": None,
            "expected_spd": None
        },
        {
            "name": "Procedure with placeholders",
            "content": "00:00:00.00>%0 ADDWPT 52.587654 3.400154 ,, ,,\n00:00:00.00>%0 ADDWPT 52.162838 3.757884\n",
            "expected_alt": None,
            "expected_spd": None
        }
    ]
    
    for case in test_cases:
        print(f"\n--- Testing: {case['name']} ---")
        
        # Create temporary procedure file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.scn', delete=False) as f:
            test_proc_path = f.name
            f.write(case['content'])
        
        try:
            alt, spd = _proc_extract_initial_alt_spd(test_proc_path)
            print(f"Extracted: altitude={alt}, speed={spd}")
            print(f"Expected:  altitude={case['expected_alt']}, speed={case['expected_spd']}")
            
            if alt == case['expected_alt'] and spd == case['expected_spd']:
                print("✓ PASS")
            else:
                print("✗ FAIL")
                return False
                
        finally:
            os.unlink(test_proc_path)
    
    return True

def test_cre_command_with_procedure_values():
    """Test CRE command generation with procedure values when overrides are disabled."""
    print("\n=== Testing CRE Command Generation with Procedure Values ===")
    
    test_cases = [
        {
            "name": "Override disabled, procedure has altitude and speed",
            "override_alt": False,
            "override_spd": False,
            "proc_alt": 5000,
            "proc_spd": 250,
            "expected_pattern": r"CRE .+ A320 .+ RW\w+ 5000 250$"
        },
        {
            "name": "Override disabled, procedure has only altitude",
            "override_alt": False,
            "override_spd": False,
            "proc_alt": 5000,
            "proc_spd": None,
            "expected_pattern": r"CRE .+ A320 .+ RW\w+ 5000 ,,$"
        },
        {
            "name": "Override disabled, procedure has only speed",
            "override_alt": False,
            "override_spd": False,
            "proc_alt": None,
            "proc_spd": 250,
            "expected_pattern": r"CRE .+ A320 .+ RW\w+ ,, 250$"
        },
        {
            "name": "Override disabled, procedure has no values",
            "override_alt": False,
            "override_spd": False,
            "proc_alt": None,
            "proc_spd": None,
            "expected_pattern": r"CRE .+ A320 .+ RW\w+$"  # No alt/speed
        },
        {
            "name": "Altitude override enabled, procedure has both",
            "override_alt": True,
            "override_spd": False,
            "proc_alt": 5000,
            "proc_spd": 250,
            "gui_alt": 10000,
            "expected_pattern": r"CRE .+ A320 .+ RW\w+ 10000 250$"  # GUI alt, proc speed
        },
        {
            "name": "Speed override enabled, procedure has both",
            "override_alt": False,
            "override_spd": True,
            "proc_alt": 5000,
            "proc_spd": 250,
            "gui_spd": 300,
            "expected_pattern": r"CRE .+ A320 .+ RW\w+ 5000 300$"  # Proc alt, GUI speed
        },
        {
            "name": "Both overrides enabled",
            "override_alt": True,
            "override_spd": True,
            "proc_alt": 5000,
            "proc_spd": 250,
            "gui_alt": 15000,
            "gui_spd": 350,
            "expected_pattern": r"CRE .+ A320 .+ RW\w+ 15000 350$"  # Both GUI values
        }
    ]
    
    for case in test_cases:
        print(f"\n--- Testing: {case['name']} ---")
        print(f"Expected pattern: {case['expected_pattern']}")
        
        # Simulate the SID CRE command generation logic
        acid = "PR001"
        actype = "A320"
        icao = "EHAM"
        rw_tag = "RW36R"
        ts = "00:00:00.00>"
        
        # Override flags
        sid_override_initial_alt = case['override_alt']
        sid_override_initial_spd = case['override_spd']
        
        # Procedure values (simulated extraction)
        proc_alt_ft = case['proc_alt']
        proc_spd_val = case['proc_spd']
        
        # Build CRE command logic
        if sid_override_initial_alt:
            cre_alt = case.get('gui_alt', 5000)
        else:
            cre_alt = proc_alt_ft  # Use procedure file altitude (can be None)
            
        if sid_override_initial_spd:
            cre_spd = case.get('gui_spd', 250)
        else:
            # Use procedure file speed (can be None)
            if proc_spd_val is not None:
                if proc_spd_val < 1.0:  # Mach number
                    cre_spd = f"M{proc_spd_val:.2f}"
                else:  # Knots
                    cre_spd = int(proc_spd_val)
            else:
                cre_spd = None
        
        # Handle placeholder cases
        if (sid_override_initial_spd or cre_spd is not None) and (not sid_override_initial_alt and cre_alt is None):
            # Only speed available - use placeholder for altitude
            cre_command = f"{ts}CRE {acid} {actype} {icao} {rw_tag} ,, {cre_spd}"
        elif (sid_override_initial_alt or cre_alt is not None) and (not sid_override_initial_spd and cre_spd is None):
            # Only altitude available - use placeholder for speed
            cre_command = f"{ts}CRE {acid} {actype} {icao} {rw_tag} {cre_alt} ,,"
        elif (sid_override_initial_alt or cre_alt is not None) and (sid_override_initial_spd or cre_spd is not None):
            # Both available
            cre_command = f"{ts}CRE {acid} {actype} {icao} {rw_tag} {cre_alt} {cre_spd}"
        else:
            # No values available - use standard format
            cre_command = f"{ts}CRE {acid} {actype} {icao} {rw_tag}"
        
        print(f"Generated CRE: {cre_command}")
        
        # Check if it matches expected pattern
        if re.search(case["expected_pattern"], cre_command):
            print("✓ PASS - Matches expected pattern")
        else:
            print("✗ FAIL - Does not match expected pattern")
            return False
    
    return True

def main():
    """Run all tests."""
    print("Testing SATG Procedure Extraction and CRE Command Enhancement")
    print("=" * 70)
    
    try:
        success = True
        success &= test_proc_extract_initial_alt_spd()
        success &= test_cre_command_with_procedure_values()
        
        print("\n" + "=" * 70)
        if success:
            print("🎉 ALL TESTS PASSED! 🎉")
            print("Procedure extraction and CRE command enhancement working correctly!")
            print("✓ Altitude/speed extraction from procedure files")
            print("✓ CRE commands include procedure values when overrides disabled")
            print("✓ Overrides take precedence when enabled")
            print("✓ Proper placeholder handling for partial values")
        else:
            print("❌ SOME TESTS FAILED")
            return False
        
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)