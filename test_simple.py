#!/usr/bin/env python3
"""Simple test for SATG procedure extraction functionality."""

import tempfile
import os

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

def test_basic():
    """Test basic extraction functionality."""
    print("Testing basic extraction...")
    
    # Test case 1: Both altitude and speed
    content1 = "00:00:00.00>%0 ADDWPT 52.587654 3.400154 1000 210\n"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.scn', delete=False, encoding='utf-8') as f:
        f.write(content1)
        test_path1 = f.name
    
    alt, spd = _proc_extract_initial_alt_spd(test_path1)
    print(f"Test 1 - Expected: alt=1000, spd=210, Got: alt={alt}, spd={spd}")
    assert alt == 1000 and spd == 210, "Test 1 failed"
    os.unlink(test_path1)
    
    # Test case 2: Flight level and Mach
    content2 = "00:00:00.00>%0 ADDWPT 52.587654 3.400154 FL100 M0.79\n"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.scn', delete=False, encoding='utf-8') as f:
        f.write(content2)
        test_path2 = f.name
    
    alt, spd = _proc_extract_initial_alt_spd(test_path2)
    print(f"Test 2 - Expected: alt=10000, spd=0.79, Got: alt={alt}, spd={spd}")
    assert alt == 10000 and spd == 0.79, "Test 2 failed"
    os.unlink(test_path2)
    
    # Test case 3: No values
    content3 = "00:00:00.00>%0 ADDWPT SPY\n"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.scn', delete=False, encoding='utf-8') as f:
        f.write(content3)
        test_path3 = f.name
    
    alt, spd = _proc_extract_initial_alt_spd(test_path3)
    print(f"Test 3 - Expected: alt=None, spd=None, Got: alt={alt}, spd={spd}")
    assert alt is None and spd is None, "Test 3 failed"
    os.unlink(test_path3)
    
    print("All tests passed!")

if __name__ == "__main__":
    test_basic()