SATG Configuration Saves
========================

This directory contains saved SATG tab configurations.

File naming convention:
  {user_name}_{tab_type}.json

Supported tab types:
  - realistic_replay  (Realistic Replay tab)
  - geometric_conflicts  (Geometric Conflicts tab)  
  - random_conflicts  (Random Conflicts tab)
  - procedures  (Procedures tab)

Each configuration file contains:
  - All parameter values from the respective tab
  - File lists (automatically reloaded and displayed in GUI when configuration is loaded)
  - Origin ICAO codes for SID procedures
  - Destination airport codes for procedures
  - Timestamp of when the configuration was saved
  - Tab type for validation

Usage:
  1. Configure your tab parameters as desired
  2. Load your files (waypoint and procedure files for Procedures tab)
  3. Set origin ICAO codes for SID procedures
  4. Set destination airport codes for procedures 
  5. Click "Save Config" in the top toolbar
  6. Enter a name for your configuration
  7. To reuse: Click "Load Config" and select from saved configurations
  8. To manage: Click "Edit Configs" to rename, duplicate, or delete saved configurations

Features:
  All parameters are restored automatically
  Files are automatically reloaded and displayed in GUI lists
  Origin ICAO codes are restored in SID procedure fields
  Destination airport codes are restored in procedure fields
  Complex GUI widgets are properly recreated with working edit functionality
  Clear feedback on file loading success/failure
  Configuration management: rename, duplicate, delete saved configs
  Works with both waypoint files and procedure files