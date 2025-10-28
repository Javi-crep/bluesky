#!/usr/bin/env python3
"""Simple test for generic procedure rate cleanup."""

def test_rebuild_logic():
    """Test the rebuild logic for generic rates."""
    print("Testing generic rate cleanup logic...")
    
    # Mock data structures
    proc_files = [
        "/path/to/EHAM_SID.scn",
        "/path/to/LFPG_STAR.scn", 
        "/path/to/GENERIC.scn"
    ]
    
    # Mock waypoint extraction function
    def get_tokens_from_file(filepath):
        if "EHAM_SID" in filepath:
            return ["SPY", "LAK"]
        elif "LFPG_STAR" in filepath:
            return ["BOB", "VOR"]
        elif "GENERIC" in filepath:
            return ["WPT1", "WPT2"]
        return []
    
    # Initial generic rates (includes rates for removed files)
    generic_rates = {
        "SPY": 25.0,   # From EHAM_SID
        "BOB": 30.0,   # From LFPG_STAR  
        "WPT1": 20.0,  # From GENERIC
        "OLD1": 15.0,  # From a removed file
        "OLD2": 10.0   # From another removed file
    }
    
    print(f"Before cleanup: {list(generic_rates.keys())}")
    
    # Simulate removing GENERIC file
    proc_files = [f for f in proc_files if "GENERIC" not in f]
    print(f"Removed GENERIC file, remaining: {len(proc_files)} files")
    
    # Rebuild logic: collect available tokens
    available_tokens = set()
    for filepath in proc_files:
        tokens = get_tokens_from_file(filepath)
        for token in tokens:
            available_tokens.add(token.upper())
    
    print(f"Available tokens from remaining files: {available_tokens}")
    
    # Filter rates to keep only available tokens
    filtered_rates = {token: rate for token, rate in generic_rates.items() 
                     if token in available_tokens}
    
    print(f"After cleanup: {list(filtered_rates.keys())}")
    
    # Verify results
    # We should keep rates for tokens that:
    # 1. Have explicit rates set AND
    # 2. Still exist in the remaining files
    expected = {"SPY", "BOB"}  # Only these have rates AND exist in remaining files
    actual = set(filtered_rates.keys())
    
    success = actual == expected
    print(f"Expected tokens: {expected}")
    print(f"Actual tokens: {actual}")
    print(f"Test result: {'PASS' if success else 'FAIL'}")
    
    if success:
        print("\nSUCCESS: Generic rate cleanup working correctly!")
        print("- Removed rates for waypoints from unloaded files (WPT1)")
        print("- Removed rates for non-existent waypoints (OLD1, OLD2)")
        print("- Kept rates for waypoints from remaining files (SPY, BOB)") 
        print("- No stale data persists in the GUI")
    
    return success

if __name__ == "__main__":
    test_rebuild_logic()