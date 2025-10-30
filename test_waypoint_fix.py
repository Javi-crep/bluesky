# Test the corrected test2.scn file parsing
def test_corrected_procedure():
    content = '''# Procedure: test2
# Updated: 2025-10-30 21:25:53
# Type: Custom Track Procedure
# Waypoints: 9
#
# Waypoint Definitions
00:00:00.00> DEFWPT INIT 52.606581 3.365757
00:00:00.00> DEFWPT TEST2_WP2 52.053479 3.386395
00:00:00.00> DEFWPT TEST2_WP3 51.981976 3.826678
00:00:00.00> DEFWPT TEST2_WP4 52.654952 3.826678
00:00:00.00> DEFWPT TEST2_WP5 52.642333 4.273841
00:00:00.00> DEFWPT TEST2_WP6 51.967254 4.342635
00:00:00.00> DEFWPT TEST2_WP8 52.659158 4.882670
00:00:00.00> DEFWPT TEST2_WP7 51.965151 4.858592
#
# Procedure Commands
00:00:00.00>%0 ADDWPT INIT F100 300
00:00:00.00>%0 ADDWPT TEST2_WP2
00:00:00.00>%0 ADDWPT TEST2_WP3
00:00:00.00>%0 ADDWPT TEST2_WP4 1000 M0.5
00:00:00.00>%0 ADDWPT TEST2_WP5
00:00:00.00>%0 ADDWPT TEST2_WP6
00:00:00.00>%0 ADDWPT TEST2_WP8
00:00:00.00>%0 ADDWPT TEST2_WP7
00:00:00.00>%0 ADDWPT EHAM 10 10
'''
    
    # Parse waypoint definitions
    import re
    lines = content.split('\n')
    waypoint_definitions = {}
    
    for line in lines:
        if '00:00:00.00> DEFWPT' in line:
            parts = line.strip().split()
            if len(parts) >= 4:
                name = parts[2]
                lat = float(parts[3])
                lon = float(parts[4])
                waypoint_definitions[name] = (lat, lon)
                print(f'DEFWPT: {name} -> ({lat}, {lon})')
    
    # Parse ADDWPT commands
    print('\nADDWPT commands:')
    for line in lines:
        if '00:00:00.00>%0 ADDWPT' in line:
            parts = line.strip().split()
            if len(parts) >= 3:
                waypoint_name = parts[2]
                constraints = ' '.join(parts[3:]) if len(parts) > 3 else 'none'
                
                if waypoint_name in waypoint_definitions:
                    lat, lon = waypoint_definitions[waypoint_name]
                    print(f'  {waypoint_name} (DEFWPT: {lat}, {lon}) - constraints: {constraints}')
                elif waypoint_name == 'EHAM':
                    print(f'  {waypoint_name} (KNOWN WAYPOINT) - constraints: {constraints}')
                else:
                    print(f'  {waypoint_name} (UNKNOWN) - constraints: {constraints}')
    
    print(f'\nKey fixes:')
    print('1. EHAM removed from DEFWPT (it is a known airport)')
    print('2. EHAM appears only in ADDWPT commands as a named waypoint')
    print('3. All custom waypoints (INIT, TEST2_WPx) properly defined with DEFWPT')

if __name__ == "__main__":
    test_corrected_procedure()