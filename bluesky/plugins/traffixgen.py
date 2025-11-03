"""
TraffixGen Plugin for Bluesky
============================

A Bluesky plugin that integrates TraffixGen for realistic traffic generation.
This plugin generates realistic flight trajectories that can be exported as JSON
and used with the existing SATG plugin for scenario generation.

Main Features:
- Load historical flight data
- Train ML models for trajectory generation  
- Generate realistic flight trajectories
- Export trajectories as JSON for SATG integration
- Bluesky command interface

Commands:
- TRAFFIXGEN LOAD <flights_file> <routes_file>  : Load historical data
- TRAFFIXGEN TRAIN                              : Train trajectory models
- TRAFFIXGEN GENERATE <n_flights> [bounds]      : Generate trajectories  
- TRAFFIXGEN EXPORT <filename>                  : Export to JSON for SATG
- TRAFFIXGEN STATUS                             : Show plugin status
- TRAFFIXGEN CONFIG <param> <value>             : Configure parameters

Dependencies: numpy, pandas, scikit-learn, xgboost
"""

import os
import json
import numpy as np
import pandas as pd
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path
import pickle
from datetime import datetime

# Bluesky imports
import bluesky as bs
from bluesky import stack
from bluesky.stack import command
from bluesky.tools import geo

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def _generate_callsign(ac_operator, ectrl_id):
    """Generate callsign using the same logic as SATG export"""
    ectrl_id_str = str(ectrl_id)
    ac_operator_str = str(ac_operator)
    
    if ectrl_id_str.isdigit():
        # Use operator code if available, otherwise use generic TFC
        if ac_operator_str and ac_operator_str != '' and ac_operator_str != 'nan':
            return f"{ac_operator_str}{int(ectrl_id) % 9999:04d}"
        else:
            return f"TFC{int(ectrl_id) % 9999:04d}"  # Traffic (generic)
    else:
        # Use existing callsign if already proper format
        return ectrl_id_str

# ============================================================================
# MINIMAL TRAFFIXGEN CORE (Embedded)
# ============================================================================

def exponential_average(x: np.ndarray, alpha: float = 0.3):
    """Simple exponential smoothing for trajectory data."""
    y_hat = np.zeros_like(x, dtype=float)
    y_hat[0] = x[0]
    for t in range(1, len(x)):
        y_hat[t] = alpha * x[t] + (1 - alpha) * y_hat[t - 1]
    return y_hat

def compute_elapsed_time_per_flight(df: pd.DataFrame) -> pd.DataFrame:
    """Compute elapsed time for each flight starting from 0."""
    df = df.copy()
    df["elapsed_time"] = df.groupby("ECTRL ID")["Time Over"].transform(lambda x: (x - x.min()))
    return df

class FlightTrajectory:
    """Container for flight trajectory data with easy access methods."""
    
    def __init__(self, data: np.ndarray, columns: List[str]):
        self.data = data
        self.columns = columns
        
    def __len__(self):
        return len(self.data)
        
    def __getitem__(self, key):
        if isinstance(key, str):
            if key in self.columns:
                idx = self.columns.index(key)
                return self.data[:, idx]
            else:
                raise KeyError(f"Column '{key}' not found")
        return self.data[key]

def fit_simple_distribution(data):
    """Simplified distribution fitting for OD pairs and aircraft types."""
    from scipy import stats
    
    # For discrete data (OD pairs, aircraft types), use empirical distribution
    unique_vals, counts = np.unique(data, return_counts=True)
    probs = counts / counts.sum()
    
    class EmpiricalDistribution:
        def __init__(self, values, probabilities):
            self.values = values
            self.probs = probabilities
            
        def sample(self, size=1):
            return np.random.choice(self.values, size=size, p=self.probs)
    
    return EmpiricalDistribution(unique_vals, probs)

class FlightStateSpaceTreesPhased:
    """Simplified tree-based trajectory generator for Bluesky integration."""
    
    def __init__(self, flights_df: pd.DataFrame, route_df: pd.DataFrame, 
                 model_cls, model_kwargs: Optional[Dict] = None):
        
        self.model_cls = model_cls
        self.model_kwargs = model_kwargs or {}
        
        # Create OD column
        flights_df = flights_df.copy()
        flights_df["OD"] = flights_df["ADEP"] + "-" + flights_df["ADES"]
        
        # Merge data
        merged_df = route_df.merge(
            flights_df[["ECTRL ID", "OD", "AC Type"]], 
            on="ECTRL ID", how="left"
        )
        
        # Add previous features
        self._add_previous_features(merged_df)
        
        # Compute elapsed time
        merged_df = compute_elapsed_time_per_flight(merged_df)
        
        # Define features and targets
        self.features = [
            "elapsed_time", "prev_latitude", "prev_longitude",
            "prev_flight_level", "prev_ground_speed", "prev_heading"
        ]
        self.target_cols = [
            "Latitude", "Longitude", "Flight Level",
            "ground_speed", "heading"
        ]
        
        # Clean data
        merged_df = merged_df.dropna(subset=self.features + self.target_cols)
        
        # Store initial states and max times
        self._compute_initial_states(merged_df)
        
        # Fit models
        self._fit_models(merged_df)
        
    def _add_previous_features(self, df):
        """Add previous timestep features."""
        prev_mapping = {
            "prev_latitude": "Latitude",
            "prev_longitude": "Longitude", 
            "prev_flight_level": "Flight Level",
            "prev_ground_speed": "ground_speed",
            "prev_heading": "heading"
        }
        
        for prev_col, orig_col in prev_mapping.items():
            if orig_col in df.columns:
                df[prev_col] = df.groupby("ECTRL ID")[orig_col].shift(1)
                
    def _compute_initial_states(self, df):
        """Compute initial states and max times for each OD/AC combination."""
        self.initial_states = {}
        self.max_times = {}
        
        for (od, ac), group in df.groupby(["OD", "AC Type"]):
            if len(group) > 0:
                self.initial_states[(od, ac)] = group[self.features].iloc[0].to_numpy()
                self.max_times[(od, ac)] = group["elapsed_time"].max()
                
    def _fit_models(self, df):
        """Fit tree-based models for each OD/AC combination."""
        self.models = {}
        
        for (od, ac), group in df.groupby(["OD", "AC Type"]):
            if len(group) < 10:  # Skip if insufficient data
                continue
                
            X = group[self.features]
            models = {}
            
            for target in self.target_cols:
                y = group[target]
                model = self.model_cls(**self.model_kwargs)
                model.fit(X, y)
                models[target] = model
                
            self.models[(od, ac)] = models
            
    def sample_state(self, od: str, ac_type: str, n_points: int = 200) -> np.ndarray:
        """Sample a trajectory for given OD and aircraft type."""
        key = (od, ac_type)
        
        if key not in self.models:
            raise ValueError(f"No model for OD={od}, AC={ac_type}")
            
        models = self.models[key]
        features = dict(zip(self.features, self.initial_states[key]))
        dt = self.max_times[key] / n_points
        
        # Initialize trajectory array
        traj = np.zeros((n_points, 1 + len(self.target_cols)))
        
        for t in range(n_points):
            # Build feature vector
            X = np.array([features[f] for f in self.features]).reshape(1, -1)
            
            # Predict targets
            preds = {}
            for target in self.target_cols:
                preds[target] = models[target].predict(X)[0]
                
            # Store results
            traj[t, 0] = features["elapsed_time"]
            for i, target in enumerate(self.target_cols):
                traj[t, i + 1] = preds[target]
                
            # Update features for next timestep
            features["elapsed_time"] += dt
            for target in self.target_cols:
                prev_name = f"prev_{target.lower().replace(' ', '_')}"
                if prev_name in features:
                    features[prev_name] = preds[target]
                    
        # Apply smoothing
        for i in range(1, len(self.target_cols) + 1):
            traj[:, i] = exponential_average(traj[:, i], alpha=0.3)
            
        return traj

class FlightTrajectorySampler:
    """Simplified trajectory sampler for Bluesky integration."""
    
    def __init__(self, flights_df: pd.DataFrame, route_df: pd.DataFrame):
        self.flights_df = flights_df.copy()
        self.route_df = route_df.copy()
        self.state_space = None
        self.od_dist = None
        self.ac_type_dists = None
        self.preprocessed = False
        
    def preprocess(self):
        """Preprocess data and fit distributions."""
        # Create OD column
        self.flights_df["OD"] = self.flights_df["ADEP"] + "-" + self.flights_df["ADES"]
        
        # Convert time to seconds if needed
        if not pd.api.types.is_numeric_dtype(self.route_df["Time Over"]):
            self.route_df["Time Over"] = pd.to_datetime(self.route_df["Time Over"])
            self.route_df["Time Over"] = (
                self.route_df["Time Over"].dt.hour * 3600 +
                self.route_df["Time Over"].dt.minute * 60 +
                self.route_df["Time Over"].dt.second
            )
            
        # Fit distributions
        self._create_distributions()
        self.preprocessed = True
        
    def _create_distributions(self):
        """Create distributions for OD pairs and aircraft types."""
        # OD distribution
        od_series = self.flights_df["OD"]
        od_codes, self.od_categories = pd.factorize(od_series)
        self.od_dist = fit_simple_distribution(od_codes)
        
        # AC type distributions per OD
        self.ac_type_dists = {}
        for od_label in pd.unique(od_series):
            mask = od_series == od_label
            ac_types = self.flights_df.loc[mask, "AC Type"]
            ac_codes, ac_categories = pd.factorize(ac_types)
            ac_dist = fit_simple_distribution(ac_codes)
            self.ac_type_dists[od_label] = (ac_dist, ac_categories)
            
    def initialize_state_space(self, state_space_cls, model_cls, 
                             model_kwargs: Optional[Dict] = None):
        """Initialize the state space model."""
        if not self.preprocessed:
            self.preprocess()
            
        self.state_space = state_space_cls(
            self.flights_df, self.route_df, model_cls, model_kwargs
        )
        
    def sample_od_ac(self, n_samples: int = 1) -> Tuple[List[str], List[str]]:
        """Sample OD pairs and aircraft types."""
        # Sample ODs
        od_indices = self.od_dist.sample(n_samples).astype(int)
        ods_sampled = [self.od_categories[i] for i in od_indices]
        
        # Sample aircraft types
        acs_sampled = []
        for od in ods_sampled:
            dist, categories = self.ac_type_dists[od]
            ac_index = int(dist.sample(1)[0])
            acs_sampled.append(categories[ac_index])
            
        return ods_sampled, acs_sampled
        
    def sample_trajectories(self, n_samples: int = 1, n_points: int = 200) -> List[Tuple[str, str, FlightTrajectory]]:
        """Sample flight trajectories."""
        if self.state_space is None:
            raise RuntimeError("State space not initialized")
            
        ods, ac_types = self.sample_od_ac(n_samples)
        
        trajectories = []
        for od, ac_type in zip(ods, ac_types):
            try:
                traj_array = self.state_space.sample_state(od, ac_type, n_points)
                traj = FlightTrajectory(traj_array, ["elapsed_time"] + self.state_space.target_cols)
                trajectories.append((od, ac_type, traj))
            except ValueError as e:
                print(f"Warning: Could not generate trajectory for {od} {ac_type}: {e}")
                continue
                
        return trajectories

# ============================================================================
# BLUESKY PLUGIN CLASS
# ============================================================================

class TraffixGenPlugin(bs.core.Entity):
    """Main TraffixGen plugin for Bluesky."""
    
    # Class variable to store data that can be accessed from other plugins
    _shared_data = {
        'last_summary': None,
        'last_tracks': None,
        'dataset_loaded': False
    }
    
    def __init__(self):
        super().__init__()
        self.sampler = None
        self.is_trained = False
        self.last_trajectories = []
        self.config = {
            'model_type': 'xgboost',
            'n_estimators': 50,
            'max_depth': 6,
            'default_n_points': 200,
            'random_state': 42
        }
        
        # Get data directory (same as SATG)
        self.data_dir = None
        self.scenarios_dir = None
        self._setup_directories()
        
    def _setup_directories(self):
        """Setup data directories compatible with SATG."""
        try:
            # Try to get SATG base directory if available
            if hasattr(bs, 'plugins') and hasattr(bs.plugins, 'satg') and hasattr(bs.plugins.satg, 'STATE'):
                satg_base = getattr(bs.plugins.satg.STATE, 'base_dir', None)
                if satg_base:
                    self.data_dir = os.path.join(satg_base, 'data')
                    self.scenarios_dir = os.path.join(satg_base, 'scenarios')
        except:
            pass
            
        # Fallback to default locations
        if not self.data_dir:
            self.data_dir = os.path.join(os.path.dirname(bs.__file__), '..', 'satg_data', 'data')
            self.scenarios_dir = os.path.join(os.path.dirname(bs.__file__), '..', 'scenario')
            
        # Create directories if they don't exist
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.scenarios_dir, exist_ok=True)
        
    def load_historical_data(self, flights_file: str, routes_file: str) -> bool:
        """Load historical flight data for training."""
        try:
            # Handle relative paths
            if not os.path.isabs(flights_file):
                flights_file = os.path.join(self.data_dir, flights_file)
            if not os.path.isabs(routes_file):
                routes_file = os.path.join(self.data_dir, routes_file)
                
            print(f"Loading historical data from {flights_file} and {routes_file}")
            
            # Load data
            flights_df = pd.read_csv(flights_file)
            routes_df = pd.read_csv(routes_file)
            
            # Validate required columns
            required_flight_cols = ['ECTRL ID', 'ADEP', 'ADES', 'AC Type']
            required_route_cols = ['ECTRL ID', 'Time Over', 'Latitude', 'Longitude', 'Flight Level']
            
            missing_flight = [col for col in required_flight_cols if col not in flights_df.columns]
            missing_route = [col for col in required_route_cols if col not in routes_df.columns]
            
            if missing_flight:
                raise ValueError(f"Missing flight columns: {missing_flight}")
            if missing_route:
                raise ValueError(f"Missing route columns: {missing_route}")
            
            # Add missing optional columns
            if 'ground_speed' not in routes_df.columns:
                routes_df['ground_speed'] = 450  # Default ground speed
            if 'heading' not in routes_df.columns:
                routes_df['heading'] = 0  # Will be computed from trajectory
                
            print(f"Loaded {len(flights_df)} flights and {len(routes_df)} route points")
            
            # Create sampler
            self.sampler = FlightTrajectorySampler(flights_df, routes_df)
            return True
            
        except Exception as e:
            print(f"Error loading data: {e}")
            return False
    
    def train_models(self) -> bool:
        """Train trajectory generation models."""
        if self.sampler is None:
            print("Error: No data loaded. Use TRAFFIXGEN LOAD first.")
            return False
            
        try:
            print("Training trajectory models...")
            
            # Import XGBoost
            try:
                from xgboost import XGBRegressor
            except ImportError:
                print("Error: XGBoost not available. Install with: pip install xgboost")
                return False
            
            # Initialize state space
            self.sampler.initialize_state_space(
                FlightStateSpaceTreesPhased,
                model_cls=XGBRegressor,
                model_kwargs={
                    'n_estimators': self.config['n_estimators'],
                    'max_depth': self.config['max_depth'],
                    'random_state': self.config['random_state']
                }
            )
            
            self.is_trained = True
            print("Model training completed successfully!")
            return True
            
        except Exception as e:
            print(f"Error during training: {e}")
            return False
    
    def generate_trajectories(self, n_flights: int = 20, bounds: Optional[str] = None) -> bool:
        """Generate flight trajectories."""
        if not self.is_trained:
            print("Error: Models not trained. Use TRAFFIXGEN TRAIN first.")
            return False
            
        try:
            print(f"Generating {n_flights} flight trajectories...")
            
            # Generate trajectories
            trajectories = self.sampler.sample_trajectories(
                n_samples=n_flights, 
                n_points=self.config['default_n_points']
            )
            
            if not trajectories:
                print("Warning: No valid trajectories generated")
                return False
            
            # Convert to JSON-friendly format for SATG integration
            flight_data = []
            for i, (od, ac_type, traj) in enumerate(trajectories):
                origin, dest = od.split('-') if '-' in od else (od[:4], od[4:])
                
                waypoints = []
                for j in range(len(traj)):
                    waypoint = {
                        'time': float(traj['elapsed_time'][j]),
                        'latitude': float(traj['Latitude'][j]),
                        'longitude': float(traj['Longitude'][j]),
                        'altitude': float(traj['Flight Level'][j]) * 100,  # Convert to feet
                        'ground_speed': float(traj['ground_speed'][j]),
                        'heading': float(traj['heading'][j])
                    }
                    waypoints.append(waypoint)
                
                flight_info = {
                    'id': i + 1,
                    'callsign': f"{origin}{dest}{i+1:03d}",
                    'aircraft_type': ac_type,
                    'origin': origin,
                    'destination': dest,
                    'od_pair': od,
                    'waypoints': waypoints
                }
                flight_data.append(flight_info)
            
            self.last_trajectories = flight_data
            print(f"Generated {len(flight_data)} valid trajectories")
            return True
            
        except Exception as e:
            print(f"Error generating trajectories: {e}")
            return False
    
    def export_trajectories(self, filename: str) -> bool:
        """Export generated trajectories to JSON for SATG integration."""
        if not self.last_trajectories:
            print("Error: No trajectories to export. Use TRAFFIXGEN GENERATE first.")
            return False
            
        try:
            # Handle relative paths
            if not os.path.isabs(filename):
                export_path = os.path.join(self.data_dir, filename)
            else:
                export_path = filename
                
            # Ensure .json extension
            if not export_path.endswith('.json'):
                export_path += '.json'
            
            # Create export data with metadata
            export_data = {
                'metadata': {
                    'generated_at': datetime.now().isoformat(),
                    'plugin': 'TraffixGen',
                    'version': '1.0',
                    'n_flights': len(self.last_trajectories),
                    'config': self.config
                },
                'flights': self.last_trajectories
            }
            
            # Write JSON file
            with open(export_path, 'w') as f:
                json.dump(export_data, f, indent=2)
                
            print(f"Exported {len(self.last_trajectories)} trajectories to {export_path}")
            print(f"File ready for SATG integration!")
            return True
            
        except Exception as e:
            print(f"Error exporting trajectories: {e}")
            return False
    
    def get_status(self) -> str:
        """Get plugin status."""
        status = []
        status.append("=== TraffixGen Plugin Status ===")
        status.append(f"Data loaded: {'Yes' if self.sampler else 'No'}")
        status.append(f"Models trained: {'Yes' if self.is_trained else 'No'}")
        status.append(f"Last generation: {len(self.last_trajectories)} trajectories")
        status.append(f"Data directory: {self.data_dir}")
        status.append(f"Scenarios directory: {self.scenarios_dir}")
        status.append("\nConfiguration:")
        for key, value in self.config.items():
            status.append(f"  {key}: {value}")
        return "\n".join(status)

    def load_eurocontrol_data(self, flights_file: str, filed_points_file: str, actual_points_file: str, fir_file: str = "") -> bool:
        """Load Eurocontrol CSV files for processing."""
        try:
            print("Loading Eurocontrol data files...")
            
            # Initialize dataset collection
            self.dataset_collection = DatasetCollection()
            
            # Load all files
            self.dataset_collection.load_data(
                flights_file=flights_file,
                filed_points_file=filed_points_file, 
                actual_points_file=actual_points_file,
                fir_file=fir_file if fir_file else None
            )
            
            print("Eurocontrol data loaded successfully!")
            return True
            
        except Exception as e:
            print(f"Error loading Eurocontrol data: {e}")
            return False
    
    def apply_eurocontrol_filters(self, filters: dict) -> bool:
        """Apply filters to loaded Eurocontrol data."""
        try:
            if not hasattr(self, 'dataset_collection') or self.dataset_collection is None:
                print("Error: No Eurocontrol data loaded. Use TRAFFIXGEN LOAD_EUROCONTROL first.")
                return False
            
            # Apply geographic filter
            if 'lat_min' in filters and 'lat_max' in filters:
                self.dataset_collection.apply_geographic_filter(
                    filters['lat_min'], filters['lat_max'],
                    filters['lon_min'], filters['lon_max']
                )
            
            # Apply flight level filter
            if 'fl_min' in filters and 'fl_max' in filters:
                self.dataset_collection.apply_flight_level_filter(
                    filters['fl_min'], filters['fl_max']
                )
            
            # Apply aircraft type filter
            if 'aircraft_types' in filters and filters['aircraft_types']:
                self.dataset_collection.apply_aircraft_filter(filters['aircraft_types'])
            
            # Apply time filter (if implemented)
            if 'time_start' in filters and 'time_end' in filters:
                if filters['time_start'] and filters['time_end']:
                    self.dataset_collection.apply_time_filter(filters['time_start'], filters['time_end'])
            
            # Apply airspace exclusion (if implemented)
            if 'exclude_airspace' in filters and filters['exclude_airspace']:
                self.dataset_collection.apply_airspace_exclusion(filters['exclude_airspace'])
            
            print("Filters applied successfully!")
            return True
            
        except Exception as e:
            print(f"Error applying filters: {e}")
            return False
    
    def export_to_satg(self) -> bool:
        """Export processed Eurocontrol data directly to SATG via stack commands."""
        try:
            if not hasattr(self, 'dataset_collection') or self.dataset_collection is None:
                print("Error: No Eurocontrol data loaded. Use TRAFFIXGEN LOAD_EUROCONTROL first.")
                return False
            
            # Get processed data
            flights_df = self.dataset_collection.get_flights_dataframe()
            points_df = self.dataset_collection.get_points_dataframe()
            
            if flights_df.empty or points_df.empty:
                print("Error: No valid data after filtering.")
                return False
            
            # Convert to SATG format
            flights_data = []
            for _, row in flights_df.iterrows():
                flight = {
                    'ECTRL ID': str(row.get('ECTRL ID', '')),
                    'ADEP': str(row.get('ADEP', '')), 
                    'ADES': str(row.get('ADES', '')),
                    'AC Type': str(row.get('AC Type', ''))
                }
                flights_data.append(flight)
            
            points_data = []
            for _, row in points_df.iterrows():
                point = {
                    'ECTRL ID': str(row.get('ECTRL ID', '')),
                    'Sequence Number': int(row.get('Sequence Number', 0)),
                    'Time Over': str(row.get('Time Over', '')),
                    'Flight Level': float(row.get('Flight Level', 0)),
                    'Latitude': float(row.get('Latitude', 0.0)),
                    'Longitude': float(row.get('Longitude', 0.0)),
                    'ground_speed': float(row.get('ground_speed', 450.0)),  # Default if missing
                    'heading': float(row.get('heading', 0.0))  # Default if missing
                }
                points_data.append(point)
            
            # Convert to JSON strings
            import json
            flights_json = json.dumps(flights_data)
            points_json = json.dumps(points_data)
            
            # Call SATG command to load data directly
            from bluesky import stack
            result = stack.stack(f"SATG_RL_LOAD_DATA {flights_json} {points_json}")
            
            if result:
                print(f"Successfully exported {len(flights_data)} flights and {len(points_data)} points to SATG!")
                print("Data is now ready for realistic replay in SATG.")
                return True
            else:
                print("Error: Failed to transfer data to SATG")
                return False
                
        except Exception as e:
            print(f"Error exporting to SATG: {e}")
            return False

    def get_flight_summary(self) -> dict:
        """Get summary of loaded flight data for GUI display and filtering."""
        try:
            if not hasattr(self, 'dataset_collection') or self.dataset_collection is None:
                summary = {'error': 'No data loaded'}
            else:
                flights_df = self.dataset_collection.get_flights_dataframe()
                points_df = self.dataset_collection.get_points_dataframe()
                
                if flights_df.empty:
                    summary = {'error': 'No flight data available'}
                else:
                    # Calculate summary statistics
                    summary = {
                        'total_flights': len(flights_df),
                        'total_points': len(points_df),
                        'aircraft_types': sorted(flights_df['AC Type'].unique().tolist()),
                        'airports_origin': sorted(flights_df['ADEP'].unique().tolist()),
                        'airports_dest': sorted(flights_df['ADES'].unique().tolist()),
                        'lat_bounds': {
                            'min': float(points_df['Latitude'].min()) if not points_df.empty else 0,
                            'max': float(points_df['Latitude'].max()) if not points_df.empty else 0
                        },
                        'lon_bounds': {
                            'min': float(points_df['Longitude'].min()) if not points_df.empty else 0,
                            'max': float(points_df['Longitude'].max()) if not points_df.empty else 0
                        },
                        'fl_bounds': {
                            'min': float(points_df['Flight Level'].min()) if not points_df.empty else 0,
                            'max': float(points_df['Flight Level'].max()) if not points_df.empty else 0
                        }
                    }
            
            # Store in shared data for access from other plugins
            TraffixGenPlugin._shared_data['last_summary'] = summary
            return summary
            
        except Exception as e:
            error_summary = {'error': f'Error getting flight summary: {e}'}
            TraffixGenPlugin._shared_data['last_summary'] = error_summary
            return error_summary

    def _generate_callsign(self, ectrl_id, ac_operator):
        """Generate callsign using the same logic as SATG export"""
        ectrl_id_str = str(ectrl_id)
        ac_operator_str = str(ac_operator)
        
        if ectrl_id_str.isdigit():
            # Use operator code if available, otherwise use generic TFC
            if ac_operator_str and ac_operator_str != '' and ac_operator_str != 'nan':
                return f"{ac_operator_str}{int(ectrl_id) % 9999:04d}"
            else:
                return f"TFC{int(ectrl_id) % 9999:04d}"  # Traffic (generic)
        else:
            # Use existing callsign if already proper format
            return ectrl_id_str

    def get_filtered_tracks(self) -> dict:
        """Get individual track data for phase altitude configuration."""
        try:
            if not hasattr(self, 'dataset_collection') or self.dataset_collection is None:
                tracks_data = {'error': 'No data loaded'}
            else:
                flights_df = self.dataset_collection.get_flights_dataframe()
                points_df = self.dataset_collection.get_points_dataframe()
                
                if flights_df.empty or points_df.empty:
                    tracks_data = {'error': 'No data available after filtering'}
                else:
                    # Create track data grouped by flight ID with callsigns
                    tracks = {}
                    
                    # Debug: Print available columns to understand data structure (limited output)
                    print(f"Available columns in flights_df: {list(flights_df.columns)}")
                    
                    for _, flight in flights_df.iterrows():
                        flight_id = flight['ECTRL ID']
                        flight_points = points_df[points_df['ECTRL ID'] == flight_id]
                        
                        # Try multiple possible column names for airline operator
                        ac_operator = ''
                        possible_operator_columns = [
                            'AC Operator', 'Operator', 'OPERATOR', 'Airline', 'AIRLINE',
                            'AC_Operator', 'Aircraft Operator', 'AIRCRAFT_OPERATOR'
                        ]
                        
                        for col_name in possible_operator_columns:
                            if col_name in flight.index and pd.notna(flight[col_name]) and flight[col_name] != '':
                                ac_operator = str(flight[col_name])
                                break
                        
                        # If no operator found, try to extract from ECTRL ID if it looks like a callsign
                        if not ac_operator:
                            ectrl_id_str = str(flight_id)
                            # Check if ECTRL ID starts with letters (likely a callsign)
                            # Accept 2 or 3 letter airline codes (e.g., VG, IBE, KLM)
                            if len(ectrl_id_str) >= 2:
                                # Find the longest alphabetic prefix (2-3 chars typically)
                                alpha_prefix = ''
                                for i, char in enumerate(ectrl_id_str):
                                    if char.isalpha():
                                        alpha_prefix += char
                                    else:
                                        break
                                    # Stop at 3 chars max for airline codes
                                    if len(alpha_prefix) >= 3:
                                        break
                                
                                if len(alpha_prefix) >= 2:  # Accept 2+ letter codes
                                    ac_operator = alpha_prefix
                        
                        # If still no operator and there's a Callsign field, try that
                        if not ac_operator and 'Callsign' in flight.index and pd.notna(flight['Callsign']):
                            callsign_field = str(flight['Callsign'])
                            # Extract first 3 characters if it looks like airline code + number
                            if len(callsign_field) >= 3 and callsign_field[:3].isalpha():
                                ac_operator = callsign_field[:3]
                        
                        # Generate callsign using the same logic as SATG export
                        callsign = self._generate_callsign(flight_id, ac_operator)
                        
                        if len(flight_points) > 0:
                            tracks[flight_id] = {
                                'flight_id': flight_id,
                                'callsign': callsign,
                                'ac_operator': ac_operator,
                                'origin': flight['ADEP'],
                                'destination': flight['ADES'],
                                'aircraft_type': flight['AC Type'],
                                'points_count': len(flight_points),
                                'max_fl': float(flight_points['Flight Level'].max()),
                                'min_fl': float(flight_points['Flight Level'].min())
                            }
                    
                    tracks_data = {
                        'tracks': tracks,
                        'total_tracks': len(tracks)
                    }
            
            # Store in shared data for access from other plugins
            TraffixGenPlugin._shared_data['last_tracks'] = tracks_data
            return tracks_data
            
        except Exception as e:
            error_tracks = {'error': f'Error getting track data: {e}'}
            TraffixGenPlugin._shared_data['last_tracks'] = error_tracks
            return error_tracks

    @stack.command
    def traffixgen(self, command_part: str = "", *args) -> bool:
        """
        TraffixGen Plugin Commands
        
        TRAFFIXGEN LOAD <flights_file> <routes_file>          : Load historical data (legacy)
        TRAFFIXGEN LOAD_EUROCONTROL <flights> <filed> <actual> [fir] : Load Eurocontrol files
        TRAFFIXGEN GET_SUMMARY                               : Get summary of loaded data for filters
        TRAFFIXGEN FILTER <filters_json>                     : Apply filters to loaded data
        TRAFFIXGEN GET_TRACKS                                : Get track data for phase altitude config
        TRAFFIXGEN EXPORT_TO_SATG                            : Export processed data to SATG
        TRAFFIXGEN TRAIN                                      : Train trajectory models
        TRAFFIXGEN GENERATE <n_flights> [bounds]              : Generate trajectories  
        TRAFFIXGEN EXPORT <filename>                          : Export to JSON file
        TRAFFIXGEN STATUS                                     : Show plugin status
        TRAFFIXGEN CONFIG <param> <value>                     : Configure parameters
        """
        
        if not command_part:
            print(self.traffixgen.__doc__)
            return True
            
        cmd = command_part.upper()
        
        if cmd == "LOAD":
            if len(args) < 2:
                print("Usage: TRAFFIXGEN LOAD <flights_file> <routes_file>")
                return False
            return self.load_historical_data(args[0], args[1])
            
        elif cmd == "LOAD_EUROCONTROL":
            if len(args) < 3:
                print("Usage: TRAFFIXGEN LOAD_EUROCONTROL <flights_file> <filed_points_file> <actual_points_file> [fir_file]")
                return False
            fir_file = args[3] if len(args) > 3 else ""
            # Use the standalone function that works with global _dataset_collection
            return traffixgen_load_eurocontrol(args[0], args[1], args[2], fir_file)
            
        elif cmd == "FILTER":
            if len(args) < 1:
                print("Usage: TRAFFIXGEN FILTER <filters_json>")
                print("Example: TRAFFIXGEN FILTER {\"lat_min\":50,\"lat_max\":55,\"lon_min\":3,\"lon_max\":7}")
                return False
            try:
                import json
                filters = json.loads(args[0])
                # Use the standalone function that works with global _dataset_collection
                global _dataset_collection
                if _dataset_collection is None:
                    print("Error: No Eurocontrol data loaded. Use TRAFFIXGEN LOAD_EUROCONTROL first.")
                    return False
                return traffixgen_apply_filters(filters)
            except json.JSONDecodeError:
                print("Error: Invalid JSON format for filters")
                return False
                
        elif cmd == "EXPORT_TO_SATG":
            # Use the standalone function that works with global _dataset_collection
            return traffixgen_export_to_satg()
            
        elif cmd == "GET_SUMMARY":
            print("DEBUG: GET_SUMMARY command called")
            summary = self.get_flight_summary()
            if 'error' in summary:
                print(f"Error: {summary['error']}")
                return False
            else:
                import json
                print("Flight Data Summary:")
                print(json.dumps(summary, indent=2))
                return True
                
        elif cmd == "GET_TRACKS":
            print("DEBUG: GET_TRACKS command called")
            tracks = self.get_filtered_tracks()
            if 'error' in tracks:
                print(f"Error: {tracks['error']}")
                return False
            else:
                import json
                print("Track Data:")
                print(json.dumps(tracks, indent=2))
                return True
            
        elif cmd == "TRAIN":
            return self.train_models()
            
        elif cmd == "GENERATE":
            if len(args) < 1:
                print("Usage: TRAFFIXGEN GENERATE <n_flights> [bounds]")
                return False
            try:
                n_flights = int(args[0])
                bounds = args[1] if len(args) > 1 else None
                return self.generate_trajectories(n_flights, bounds)
            except ValueError:
                print("Error: n_flights must be an integer")
                return False
                
        elif cmd == "EXPORT":
            if len(args) < 1:
                print("Usage: TRAFFIXGEN EXPORT <filename>")
                return False
            return self.export_trajectories(args[0])
            
        elif cmd == "STATUS":
            print(self.get_status())
            return True
            
        elif cmd == "CONFIG":
            if len(args) < 2:
                print("Usage: TRAFFIXGEN CONFIG <param> <value>")
                print("Available parameters: n_estimators, max_depth, default_n_points")
                return False
            param, value = args[0], args[1]
            if param in self.config:
                try:
                    self.config[param] = int(value) if value.isdigit() else value
                    print(f"Set {param} = {self.config[param]}")
                    return True
                except:
                    print(f"Error setting {param} = {value}")
                    return False
            else:
                print(f"Unknown parameter: {param}")
                return False
        else:
            print(f"Unknown command: {cmd}")
            print(self.traffixgen.__doc__)
            return False

# ============================================================================
# EUROCONTROL DATA PROCESSING (For SATGgui integration)
# ============================================================================

class DatasetCollection:
    """Simple dataset collection for Eurocontrol file processing."""
    
    def __init__(self):
        self.flights_df = None
        self.filed_points_df = None
        self.actual_points_df = None
        self.fir_df = None
        
    def load_data(self, flights_file=None, filed_points_file=None, actual_points_file=None, fir_file=None):
        """Load Eurocontrol CSV files."""
        try:
            if flights_file and os.path.exists(flights_file):
                self.flights_df = pd.read_csv(flights_file)
                print(f"Loaded flights data: {len(self.flights_df)} records")
                
            if filed_points_file and os.path.exists(filed_points_file):
                self.filed_points_df = pd.read_csv(filed_points_file)
                print(f"Loaded filed points data: {len(self.filed_points_df)} records")
                
            if actual_points_file and os.path.exists(actual_points_file):
                self.actual_points_df = pd.read_csv(actual_points_file)
                print(f"Loaded actual points data: {len(self.actual_points_df)} records")
                
            if fir_file and os.path.exists(fir_file):
                self.fir_df = pd.read_csv(fir_file)
                print(f"Loaded FIR data: {len(self.fir_df)} records")
                
        except Exception as e:
            print(f"Error loading data: {e}")
            
    def apply_geographic_filter(self, min_lat, max_lat, min_lon, max_lon):
        """Apply geographic bounds filter."""
        print(f"Applying geographic filter: lat [{min_lat}, {max_lat}], lon [{min_lon}, {max_lon}]")
        # Apply to actual points data if available
        if self.actual_points_df is not None:
            mask = (
                (self.actual_points_df['Latitude'] >= min_lat) &
                (self.actual_points_df['Latitude'] <= max_lat) &
                (self.actual_points_df['Longitude'] >= min_lon) &
                (self.actual_points_df['Longitude'] <= max_lon)
            )
            self.actual_points_df = self.actual_points_df[mask]
            print(f"Filtered actual points: {len(self.actual_points_df)} records remaining")
            
    def apply_flight_level_filter(self, min_fl, max_fl):
        """Apply flight level filter."""
        print(f"Applying flight level filter: FL [{min_fl}, {max_fl}]")
        if self.actual_points_df is not None and 'Flight Level' in self.actual_points_df.columns:
            mask = (
                (self.actual_points_df['Flight Level'] >= min_fl) &
                (self.actual_points_df['Flight Level'] <= max_fl)
            )
            self.actual_points_df = self.actual_points_df[mask]
            print(f"Filtered by flight level: {len(self.actual_points_df)} records remaining")
            
    def apply_airspace_exclusion(self, exclude_list):
        """Apply airspace exclusion filter."""
        print(f"Applying airspace exclusion: {exclude_list}")
        
        if not exclude_list:
            print("No airspaces to exclude")
            return
            
        if self.fir_df is None or 'Airspace ID' not in self.fir_df.columns:
            print("Warning: No FIR data or Airspace ID column not found")
            return
            
        # Filter FIR data to exclude specified airspaces
        original_fir_count = len(self.fir_df)
        self.fir_df = self.fir_df[~self.fir_df['Airspace ID'].isin(exclude_list)]
        
        print(f"Airspace exclusion applied: {original_fir_count} -> {len(self.fir_df)} FIR points remaining")
        print(f"Excluded airspaces: {exclude_list}")
        
        # Note: This filters the airspace boundary data itself
        # For flight filtering by airspace, we'd need point-in-polygon logic
        # which would require the flights_points_df to be filtered based on 
        # whether they fall within the remaining (non-excluded) airspace boundaries
        
    def apply_time_filter(self, start_time, end_time):
        """Apply time range filter."""
        print(f"Applying time filter: {start_time} to {end_time}")
        
        if self.flights_points_df is None or 'Time Over' not in self.flights_points_df.columns:
            print("Warning: No flight points data or Time Over column not found")
            return
            
        # Convert GUI time format (HH:MM:SS) to seconds for comparison
        def time_to_seconds(time_str):
            if isinstance(time_str, str) and ':' in time_str:
                parts = time_str.split(':')
                if len(parts) >= 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return 0
            
        start_seconds = time_to_seconds(start_time)
        end_seconds = time_to_seconds(end_time)
        
        # Filter flight points based on time
        original_count = len(self.flights_points_df)
        
        # Convert Time Over column to seconds for comparison
        def extract_time_seconds(time_str):
            if isinstance(time_str, str):
                # Handle full datetime format like "01-03-2015 05:55:00"
                if ' ' in time_str and ':' in time_str:
                    time_part = time_str.split(' ')[1]  # Get "05:55:00"
                    return time_to_seconds(time_part)
                # Handle simple time format like "05:55:00"
                elif ':' in time_str:
                    return time_to_seconds(time_str)
            return 0
            
        # Apply time filter
        time_mask = self.flights_points_df['Time Over'].apply(
            lambda x: start_seconds <= extract_time_seconds(x) <= end_seconds
        )
        
        self.flights_points_df = self.flights_points_df[time_mask]
        
        # Also filter flights table to only include flights that have remaining points
        if self.flights_df is not None:
            remaining_flight_ids = self.flights_points_df['ECTRL ID'].unique()
            self.flights_df = self.flights_df[self.flights_df['ECTRL ID'].isin(remaining_flight_ids)]
            
        print(f"Time filter applied: {original_count} -> {len(self.flights_points_df)} points remaining")
        print(f"Flights remaining: {len(self.flights_df) if self.flights_df is not None else 0}")
        
    def apply_aircraft_filter(self, include_types):
        """Apply aircraft type filter."""
        print(f"Applying aircraft filter: {include_types}")
        
        # If no aircraft types selected, include all (don't filter)
        if not include_types:
            print("No aircraft types selected - including all aircraft")
            return
            
        if self.flights_df is None or 'AC Type' not in self.flights_df.columns:
            print("Warning: No flights data or AC Type column not found")
            return
            
        # Filter flights by selected aircraft types
        original_flights_count = len(self.flights_df)
        self.flights_df = self.flights_df[self.flights_df['AC Type'].isin(include_types)]
        
        # Also filter flight points to only include points from remaining flights
        if self.flights_points_df is not None and len(self.flights_df) > 0:
            remaining_flight_ids = self.flights_df['ECTRL ID'].unique()
            original_points_count = len(self.flights_points_df)
            self.flights_points_df = self.flights_points_df[
                self.flights_points_df['ECTRL ID'].isin(remaining_flight_ids)
            ]
            print(f"Aircraft filter applied: {original_flights_count} -> {len(self.flights_df)} flights remaining")
            print(f"Flight points: {original_points_count} -> {len(self.flights_points_df)} points remaining")
        elif len(self.flights_df) == 0:
            # No flights match the criteria, clear points as well
            if self.flights_points_df is not None:
                original_points_count = len(self.flights_points_df)
                self.flights_points_df = self.flights_points_df.iloc[0:0]  # Empty dataframe
                print(f"No flights match aircraft filter criteria")
                print(f"Flight points: {original_points_count} -> 0 points remaining")
        else:
            print(f"Aircraft filter applied: {original_flights_count} -> {len(self.flights_df)} flights remaining")
            
    def get_processed_data(self):
        """Return processed data."""
        return {
            'flights': self.flights_df,
            'filed_points': self.filed_points_df,
            'actual_points': self.actual_points_df,
            'fir': self.fir_df
        }
    
    def get_flights_dataframe(self):
        """Return the flights dataframe."""
        return self.flights_df if self.flights_df is not None else pd.DataFrame()
    
    def get_points_dataframe(self):
        """Return the actual points dataframe (main flight points data)."""
        return self.actual_points_df if self.actual_points_df is not None else pd.DataFrame()

# ============================================================================
# DATASET COLLECTION (Based on Original TraffixGen Logic)
# ============================================================================

import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict

class Dataset:
    """Simple Dataset class following original TraffixGen pattern."""
    def __init__(self, data: pd.DataFrame = None):
        self.data = data if data is not None else pd.DataFrame()
    
    def get_column_names(self):
        return list(self.data.columns)

class DatasetCollection:
    """DatasetCollection following original TraffixGen logic but adapted for BlueSky."""
    
    def __init__(self):
        """Initialize following original TraffixGen pattern."""
        self._flights: Optional[Dataset] = None
        self._flights_points: Optional[Dataset] = None
        self._FIR: Optional[Dataset] = None
        
    @property
    def flights(self) -> Dataset:
        """Flights data property following original pattern."""
        if self._flights is None:
            raise ValueError("flights data is not loaded")
        return self._flights
        
    @property
    def flights_points(self) -> Dataset:
        """Flights points data property following original pattern."""
        if self._flights_points is None:
            raise ValueError("flights_points data is not loaded")
        return self._flights_points
        
    @property
    def FIR(self) -> Dataset:
        """FIR data property following original pattern."""
        if self._FIR is None:
            raise ValueError("FIR data is not loaded")
        return self._FIR
    
    def set_flight_data(self, filepath: str):
        """Load flight data following original TraffixGen logic."""
        try:
            # Load with original column filtering - include AC Operator for callsign generation
            required_cols = ["ECTRL ID", "ADEP", "ADES", "AC Type", "AC Operator"]
            df = pd.read_csv(filepath)
            
            # Filter to only required columns that exist
            available_cols = [col for col in required_cols if col in df.columns]
            if not available_cols:
                raise ValueError(f"No required columns found in {filepath}")
                
            filtered_df = df[available_cols].copy()
            self._flights = Dataset(filtered_df)
            print(f"Loaded flights data: {len(filtered_df)} flights with columns {available_cols}")
            
        except Exception as e:
            raise ValueError(f"Error loading flight data from {filepath}: {e}")
    
    def set_flights_points_data(self, filepaths: Tuple[str, str]):
        """Load flight points data following original TraffixGen logic."""
        try:
            filed_path, actual_path = filepaths
            
            # Load filed points (original columns)
            required_cols = ["ECTRL ID", "Sequence Number", "Time Over", "Flight Level", "Latitude", "Longitude"]
            filed_df = pd.read_csv(filed_path)
            actual_df = pd.read_csv(actual_path)
            
            # Filter to required columns
            filed_cols = [col for col in required_cols if col in filed_df.columns]
            actual_cols = [col for col in required_cols if col in actual_df.columns]
            
            filed_filtered = filed_df[filed_cols].copy()
            actual_filtered = actual_df[actual_cols].copy()
            
            # Calculate delays and deviations (original TraffixGen postprocessing)
            processed_df = self._calculate_deviations_and_delays(filed_filtered, actual_filtered)
            
            # Compute motion features (original TraffixGen postprocessing)
            processed_df = self._compute_motion_features(processed_df)
            
            self._flights_points = Dataset(processed_df)
            print(f"Loaded flight points data: {len(processed_df)} points with calculated deviations and motion features")
            
        except Exception as e:
            raise ValueError(f"Error loading flight points data: {e}")
    
    def set_FIR_data(self, filepath: str):
        """Load FIR data following original TraffixGen logic."""
        try:
            # Load with original column filtering
            required_cols = ["Airspace ID", "Min Flight Level", "Max Flight Level", "Sequence Number", "Latitude", "Longitude"]
            df = pd.read_csv(filepath)
            
            # Filter to only required columns that exist
            available_cols = [col for col in required_cols if col in df.columns]
            if available_cols:
                filtered_df = df[available_cols].copy()
                self._FIR = Dataset(filtered_df)
                print(f"Loaded FIR data: {len(filtered_df)} points with columns {available_cols}")
            
        except Exception as e:
            print(f"Warning: Could not load FIR data from {filepath}: {e}")
    
    def _calculate_deviations_and_delays(self, filed_df: pd.DataFrame, actual_df: pd.DataFrame) -> pd.DataFrame:
        """Calculate delays and deviations following original TraffixGen logic."""
        # Use filed as base and add actual data for comparison
        result_df = filed_df.copy()
        
        # Add delay and deviation columns with default values
        result_df['Delay Time Over'] = 0.0
        result_df['Dev Latitude'] = 0.0
        result_df['Dev Longitude'] = 0.0  
        result_df['Dev Flight Level'] = 0.0
        
        # Try to calculate actual deviations if both datasets have matching flights
        try:
            for flight_id in filed_df['ECTRL ID'].unique():
                filed_flight = filed_df[filed_df['ECTRL ID'] == flight_id]
                actual_flight = actual_df[actual_df['ECTRL ID'] == flight_id]
                
                if not actual_flight.empty and len(filed_flight) == len(actual_flight):
                    # Calculate deviations for matching sequences
                    for idx, filed_row in filed_flight.iterrows():
                        seq_num = filed_row['Sequence Number']
                        actual_row = actual_flight[actual_flight['Sequence Number'] == seq_num]
                        
                        if not actual_row.empty:
                            actual_row = actual_row.iloc[0]
                            
                            # Update the result dataframe with deviations
                            result_idx = result_df[(result_df['ECTRL ID'] == flight_id) & 
                                                 (result_df['Sequence Number'] == seq_num)].index
                            
                            if len(result_idx) > 0:
                                idx = result_idx[0]
                                result_df.loc[idx, 'Dev Latitude'] = actual_row['Latitude'] - filed_row['Latitude']
                                result_df.loc[idx, 'Dev Longitude'] = actual_row['Longitude'] - filed_row['Longitude']
                                result_df.loc[idx, 'Dev Flight Level'] = actual_row['Flight Level'] - filed_row['Flight Level']
        except Exception as e:
            print(f"Warning: Could not calculate all deviations: {e}")
        
        return result_df
    
    def _compute_motion_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute motion features using realistic aircraft performance and actual time differences."""
        # Add motion feature columns with default values
        df['ground_speed'] = 0.0  # Will be calculated per point
        df['vertical_speed'] = 0.0
        df['heading'] = 0.0
        df['pitch'] = 0.0
        
        # Calculate motion features for each flight using actual time data
        try:
            import math
            from datetime import datetime, timedelta
            import pandas as pd
            
            for flight_id in df['ECTRL ID'].unique():
                flight_points = df[df['ECTRL ID'] == flight_id].sort_values('Sequence Number')
                
                if len(flight_points) > 1:
                    for i in range(1, len(flight_points)):
                        prev_idx = flight_points.index[i-1]
                        curr_idx = flight_points.index[i]
                        
                        prev_point = flight_points.loc[prev_idx]
                        curr_point = flight_points.loc[curr_idx]
                        
                        # Calculate actual time difference from Eurocontrol data
                        try:
                            prev_time_str = str(prev_point.get('Time Over', ''))
                            curr_time_str = str(curr_point.get('Time Over', ''))
                            
                            if prev_time_str and curr_time_str and prev_time_str != 'nan' and curr_time_str != 'nan':
                                # Parse time strings (handle different formats)
                                if ' ' in prev_time_str:
                                    prev_dt = pd.to_datetime(prev_time_str)
                                    curr_dt = pd.to_datetime(curr_time_str)
                                else:
                                    # Time only format
                                    prev_dt = pd.to_datetime(f"2000-01-01 {prev_time_str}")
                                    curr_dt = pd.to_datetime(f"2000-01-01 {curr_time_str}")
                                
                                # Calculate time difference in minutes
                                time_diff = curr_dt - prev_dt
                                dt_minutes = time_diff.total_seconds() / 60.0
                                
                                # Use realistic minimum time difference (avoid division by zero)
                                dt_minutes = max(dt_minutes, 1.0)  # At least 1 minute
                            else:
                                dt_minutes = 5.0  # Default 5 minutes between points
                                
                        except Exception as e:
                            dt_minutes = 5.0  # Fallback to 5 minutes
                        
                        # Calculate great circle distance (more accurate than simple lat/lon difference)
                        lat1 = math.radians(prev_point['Latitude'])
                        lon1 = math.radians(prev_point['Longitude'])
                        lat2 = math.radians(curr_point['Latitude'])
                        lon2 = math.radians(curr_point['Longitude'])
                        
                        # Haversine formula for great circle distance
                        dlat = lat2 - lat1
                        dlon = lon2 - lon1
                        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
                        c = 2 * math.asin(math.sqrt(a))
                        distance_km = 6371.0 * c  # Earth radius in km
                        
                        # Calculate ground speed in knots from actual data
                        distance_nm = distance_km * 0.539957  # Convert km to nautical miles
                        ground_speed_knots = distance_nm / (dt_minutes / 60.0) if dt_minutes > 0 else 0
                        
                        # Calculate heading (bearing from prev to current point)
                        if distance_km > 0.1:  # Only calculate if points are far enough apart
                            bearing = math.atan2(
                                math.sin(dlon) * math.cos(lat2),
                                math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
                            )
                            heading = (math.degrees(bearing) + 360) % 360
                        else:
                            heading = prev_point.get('heading', 0)  # Keep previous heading if stationary
                        
                        # Calculate vertical speed (feet per minute)
                        dfl = curr_point['Flight Level'] - prev_point['Flight Level']
                        vertical_speed = (dfl * 100) / dt_minutes if dt_minutes > 0 else 0  # ft/min
                        
                        # Update dataframe with calculated values
                        df.loc[curr_idx, 'ground_speed'] = ground_speed_knots
                        df.loc[curr_idx, 'heading'] = heading
                        df.loc[curr_idx, 'vertical_speed'] = vertical_speed
                        df.loc[curr_idx, 'pitch'] = 0.0  # Simplified
                        
            # Set reasonable speeds for first waypoints (no previous point to calculate from)
            for flight_id in df['ECTRL ID'].unique():
                flight_points = df[df['ECTRL ID'] == flight_id].sort_values('Sequence Number')
                if len(flight_points) > 0:
                    first_idx = flight_points.index[0]
                    # Set first waypoint speed to 0 to trigger initial SPD/ALT commands in SATG
                    df.loc[first_idx, 'ground_speed'] = 0.0  # Changed from 200.0 to 0.0
                    
                    # Calculate initial heading from first to second waypoint
                    if len(flight_points) > 1:
                        second_idx = flight_points.index[1]
                        first_point = flight_points.loc[first_idx]
                        second_point = flight_points.loc[second_idx]
                        
                        # Calculate initial heading using same formula
                        lat1 = math.radians(first_point['Latitude'])
                        lon1 = math.radians(first_point['Longitude'])
                        lat2 = math.radians(second_point['Latitude'])
                        lon2 = math.radians(second_point['Longitude'])
                        
                        dlon = lon2 - lon1
                        bearing = math.atan2(
                            math.sin(dlon) * math.cos(lat2),
                            math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
                        )
                        initial_heading = (math.degrees(bearing) + 360) % 360
                        df.loc[first_idx, 'heading'] = initial_heading
                        
        except Exception as e:
            print(f"Warning: Could not calculate all motion features: {e}")
            print("Using default realistic speeds for aircraft performance")
            
        return df
    
    def set_bbox(self, lat: Tuple[float, float], lon: Tuple[float, float]):
        """Apply geographic bounding box filter following original TraffixGen logic."""
        lat_min, lat_max = lat
        lon_min, lon_max = lon
        
        # Apply to all datasets that have Latitude/Longitude columns
        for dataset_attr in ['_flights_points', '_FIR']:
            dataset = getattr(self, dataset_attr)
            if dataset is not None and 'Latitude' in dataset.data.columns and 'Longitude' in dataset.data.columns:
                df = dataset.data
                filtered = df[
                    (df['Latitude'] >= lat_min) & (df['Latitude'] <= lat_max) &
                    (df['Longitude'] >= lon_min) & (df['Longitude'] <= lon_max)
                ]
                dataset.data = filtered
                print(f"Applied geographic filter to {dataset_attr}: {len(filtered)} points remaining")
    
    def set_fl_bounds(self, fl_min: float = 0, fl_max: float = 10000):
        """Apply flight level filter following original TraffixGen logic."""
        # Apply to datasets with Flight Level columns
        for dataset_attr in ['_flights_points', '_FIR']:
            dataset = getattr(self, dataset_attr)
            if dataset is not None and 'Flight Level' in dataset.data.columns:
                df = dataset.data
                filtered = df[(df['Flight Level'] >= fl_min) & (df['Flight Level'] <= fl_max)]
                dataset.data = filtered
                print(f"Applied flight level filter to {dataset_attr}: {len(filtered)} points remaining")
    
    def exclude_airspace(self, airspace_ids: List[str]):
        """Exclude airspaces following original TraffixGen logic."""
        if self._FIR is not None and 'Airspace ID' in self._FIR.data.columns:
            df = self._FIR.data
            filtered = df[~df['Airspace ID'].isin(airspace_ids)]
            self._FIR.data = filtered
            print(f"Excluded airspaces {airspace_ids}: {len(filtered)} FIR points remaining")

# Global instance for data storage
_dataset_collection = None
_shared_data = {
    'last_summary': None,
    'last_tracks': None,
    'dataset_loaded': False
}

def init_plugin():
    """Initialize the TraffixGen plugin."""
    # Configuration parameters
    config = {
        'plugin_name': 'TRAFFIXGEN',
        'plugin_type': 'sim',
    }
    
    print("TraffixGen Plugin loaded successfully!")
    print("Use 'TRAFFIXGEN' to see available commands")
    print("Workflow: LOAD → TRAIN → GENERATE → EXPORT → Use with SATG")
    
    return config

def get_flight_summary():
    """Get summary of loaded flight data for GUI display and filtering."""
    global _dataset_collection, _shared_data
    
    try:
        if _dataset_collection is None:
            summary = {'error': 'No data loaded'}
        else:
            # Use original TraffixGen property access (flights.data, flights_points.data)
            flights_df = _dataset_collection.flights.data
            points_df = _dataset_collection.flights_points.data
            
            # Also get FIR data if available
            fir_df = None
            try:
                fir_df = _dataset_collection.FIR.data
            except (ValueError, AttributeError):
                # FIR data not loaded or not available
                pass
            
            if flights_df.empty:
                summary = {'error': 'No flight data available'}
            else:
                # Calculate summary statistics using original column names
                summary = {
                    'total_flights': len(flights_df),
                    'total_points': len(points_df),
                    'aircraft_types': sorted(flights_df['AC Type'].unique().tolist()),
                    'airports_origin': sorted(flights_df['ADEP'].unique().tolist()),
                    'airports_dest': sorted(flights_df['ADES'].unique().tolist()),
                    'lat_bounds': {
                        'min': float(points_df['Latitude'].min()) if not points_df.empty else 0,
                        'max': float(points_df['Latitude'].max()) if not points_df.empty else 0
                    },
                    'lon_bounds': {
                        'min': float(points_df['Longitude'].min()) if not points_df.empty else 0,
                        'max': float(points_df['Longitude'].max()) if not points_df.empty else 0
                    },
                    'fl_bounds': {
                        'min': float(points_df['Flight Level'].min()) if not points_df.empty else 0,
                        'max': float(points_df['Flight Level'].max()) if not points_df.empty else 0
                    }
                }
                
                # Add FIR information if available
                if fir_df is not None and not fir_df.empty:
                    summary['total_fir_points'] = len(fir_df)
                    if 'Airspace ID' in fir_df.columns:
                        summary['airspace_ids'] = sorted(fir_df['Airspace ID'].unique().tolist())
                else:
                    summary['total_fir_points'] = 0
                    summary['airspace_ids'] = []
                
                # Add time bounds if Time Over column exists
                if 'Time Over' in points_df.columns and not points_df.empty:
                    # Convert time data to consistent format for bounds calculation
                    time_data = points_df['Time Over'].dropna()
                    if len(time_data) > 0:
                        try:
                            # Parse time strings - handle both "H:MM:SS" and full datetime formats
                            time_seconds = []
                            for time_str in time_data:
                                if isinstance(time_str, str):
                                    # Handle full datetime format like "01-03-2015 05:55:00"
                                    if ' ' in time_str and ':' in time_str:
                                        # Extract just the time part after the space
                                        time_part = time_str.split(' ')[1]  # Get "05:55:00"
                                        parts = time_part.split(':')
                                        if len(parts) >= 3:
                                            hours = int(parts[0])
                                            minutes = int(parts[1])
                                            seconds = int(parts[2])
                                            total_seconds = hours * 3600 + minutes * 60 + seconds
                                            time_seconds.append(total_seconds)
                                    # Handle simple time format like "0:55:00"
                                    elif ':' in time_str:
                                        parts = time_str.split(':')
                                        if len(parts) >= 3:
                                            hours = int(parts[0])
                                            minutes = int(parts[1])
                                            seconds = int(parts[2])
                                            total_seconds = hours * 3600 + minutes * 60 + seconds
                                            time_seconds.append(total_seconds)
                            
                            if time_seconds:
                                min_seconds = min(time_seconds)
                                max_seconds = max(time_seconds)
                                
                                # Convert back to HH:MM:SS format
                                min_time = f"{min_seconds//3600:02d}:{(min_seconds%3600)//60:02d}:{min_seconds%60:02d}"
                                max_time = f"{max_seconds//3600:02d}:{(max_seconds%3600)//60:02d}:{max_seconds%60:02d}"
                                
                                summary['time_bounds'] = {
                                    'min': min_time,
                                    'max': max_time
                                }
                        except Exception as e:
                            print(f"Warning: Could not calculate time bounds: {e}")
                            # Don't add time_bounds if calculation fails
        
        # Store in shared data for access from other plugins
        _shared_data['last_summary'] = summary
        return summary
        
    except Exception as e:
        error_summary = {'error': f'Error getting flight summary: {e}'}
        _shared_data['last_summary'] = error_summary
        return error_summary

def get_filtered_tracks():
    """Get individual track data for phase altitude configuration."""
    global _dataset_collection, _shared_data
    
    try:
        if _dataset_collection is None:
            tracks_data = {'error': 'No data loaded'}
        else:
            # Use original TraffixGen property access (flights.data, flights_points.data)
            flights_df = _dataset_collection.flights.data
            points_df = _dataset_collection.flights_points.data
            
            if flights_df.empty or points_df.empty:
                tracks_data = {'error': 'No data available after filtering'}
            else:
                # Create track data grouped by flight ID with full trajectory points
                tracks = {}
                
                # Debug: Print available columns to understand data structure (limited output)
                print(f"Available columns in flights_df: {list(flights_df.columns)}")
                
                for _, flight in flights_df.iterrows():
                    flight_id = flight['ECTRL ID']
                    flight_points = points_df[points_df['ECTRL ID'] == flight_id].copy()
                    
                    # Extract AC operator using the same logic as the class method
                    ac_operator = ''
                    possible_operator_columns = [
                        'AC Operator', 'Operator', 'OPERATOR', 'Airline', 'AIRLINE',
                        'AC_Operator', 'Aircraft Operator', 'AIRCRAFT_OPERATOR'
                    ]
                    
                    for col_name in possible_operator_columns:
                        if col_name in flight.index and pd.notna(flight[col_name]) and flight[col_name] != '':
                            ac_operator = str(flight[col_name])
                            break
                    
                    # If no operator found, try to extract from ECTRL ID if it looks like a callsign
                    if not ac_operator:
                        ectrl_id_str = str(flight_id)
                        # Check if ECTRL ID starts with letters (likely a callsign)
                        # Accept 2 or 3 letter airline codes (e.g., VG, IBE, KLM)
                        if len(ectrl_id_str) >= 2:
                            # Find the longest alphabetic prefix (2-3 chars typically)
                            alpha_prefix = ''
                            for i, char in enumerate(ectrl_id_str):
                                if char.isalpha():
                                    alpha_prefix += char
                                else:
                                    break
                                # Stop at 3 chars max for airline codes
                                if len(alpha_prefix) >= 3:
                                    break
                            
                            if len(alpha_prefix) >= 2:  # Accept 2+ letter codes
                                ac_operator = alpha_prefix
                    
                    # If still no operator and there's a Callsign field, try that
                    if not ac_operator and 'Callsign' in flight.index and pd.notna(flight['Callsign']):
                        callsign_field = str(flight['Callsign'])
                        # Extract first 3 characters if it looks like airline code + number
                        if len(callsign_field) >= 3 and callsign_field[:3].isalpha():
                            ac_operator = callsign_field[:3]
                    
                    # Generate callsign using the same logic as SATG export
                    callsign = _generate_callsign(ac_operator, flight_id)
                    
                    if len(flight_points) > 0:
                        tracks[flight_id] = {
                            'flight_id': flight_id,
                            'callsign': callsign,
                            'ac_operator': ac_operator,
                            'origin': flight['ADEP'],
                            'destination': flight['ADES'],
                            'aircraft_type': flight['AC Type'],
                            'points_count': len(flight_points),
                            'max_fl': float(flight_points['Flight Level'].max()),
                            'min_fl': float(flight_points['Flight Level'].min()),
                            'points': flight_points  # Include the actual trajectory points DataFrame
                        }
                
                tracks_data = {
                    'tracks': tracks,
                    'total_tracks': len(tracks)
                }
        
        # Store in shared data for access from other plugins
        _shared_data['last_tracks'] = tracks_data
        return tracks_data
        
    except Exception as e:
        error_tracks = {'error': f'Error getting track data: {e}'}
        _shared_data['last_tracks'] = error_tracks
        return error_tracks

@stack.command
def traffixgen_load_eurocontrol(flights_file: str, filed_file: str, actual_file: str, fir_file: str = ""):
    """Load Eurocontrol CSV files for processing using original TraffixGen logic."""
    global _dataset_collection
    
    try:
        print("Loading Eurocontrol data files...")
        
        # Import TraffixGen components following original structure
        # Initialize dataset collection using internal implementation
        _dataset_collection = DatasetCollection()
        
        # Load flights data (follows original set_flight_data method)
        print(f"Loading flights data from: {flights_file}")
        _dataset_collection.set_flight_data(filepath=flights_file)
        
        # Load flight points data (follows original set_flights_points_data method)
        # This takes both filed and actual points files as a tuple
        print(f"Loading flight points from: {filed_file}, {actual_file}")
        _dataset_collection.set_flights_points_data(filepaths=(filed_file, actual_file))
        
        # Load FIR data if provided (follows original set_FIR_data method)
        if fir_file and os.path.exists(fir_file):
            print(f"Loading FIR data from: {fir_file}")
            _dataset_collection.set_FIR_data(filepath=fir_file)
        
        print("Eurocontrol data loaded successfully using original TraffixGen logic!")
        return True
        
    except ImportError as e:
        print(f"Error importing TraffixGen: {e}")
        print("Please ensure TraffixGen_Complete_Package is available in the parent directory.")
        return False
    except Exception as e:
        print(f"Error loading Eurocontrol data: {e}")
        return False

@stack.command 
def traffixgen_apply_filters(filters_dict):
    """Apply filters to loaded Eurocontrol data using original TraffixGen methods."""
    global _dataset_collection
    
    try:
        if _dataset_collection is None:
            print("Error: No Eurocontrol data loaded. Use TRAFFIXGEN LOAD_EUROCONTROL first.")
            return False
        
        print(f"Applying filters using original TraffixGen logic: {filters_dict}")
        
        # Apply geographic bounds using original set_bbox method
        if 'lat_min' in filters_dict and 'lat_max' in filters_dict:
            lat_bounds = (filters_dict['lat_min'], filters_dict['lat_max'])
            lon_bounds = (filters_dict.get('lon_min', -180), filters_dict.get('lon_max', 180))
            print(f"Applying geographic bounds: lat={lat_bounds}, lon={lon_bounds}")
            _dataset_collection.set_bbox(lat=lat_bounds, lon=lon_bounds)
        
        # Apply flight level filter using original set_fl_bounds method
        if 'fl_min' in filters_dict and 'fl_max' in filters_dict:
            print(f"Applying flight level bounds: FL{filters_dict['fl_min']} to FL{filters_dict['fl_max']}")
            _dataset_collection.set_fl_bounds(
                fl_min=filters_dict['fl_min'],
                fl_max=filters_dict['fl_max']
            )
        
        # Apply airspace exclusion using original exclude_airspace method
        if 'exclude_airspace' in filters_dict and filters_dict['exclude_airspace']:
            print(f"Excluding airspaces: {filters_dict['exclude_airspace']}")
            
            # First, get the excluded airspace boundaries for point-in-polygon checking
            excluded_boundaries = {}
            if _dataset_collection._FIR is not None and 'Airspace ID' in _dataset_collection._FIR.data.columns:
                fir_df = _dataset_collection._FIR.data
                for airspace_id in filters_dict['exclude_airspace']:
                    airspace_points = fir_df[fir_df['Airspace ID'] == airspace_id]
                    if not airspace_points.empty:
                        # Store boundary points for this airspace
                        excluded_boundaries[airspace_id] = airspace_points[['Latitude', 'Longitude']].values
            
            # Filter flight points that fall within excluded airspaces
            if _dataset_collection.flights_points is not None and excluded_boundaries:
                original_points = len(_dataset_collection.flights_points.data)
                points_to_keep = []
                
                for idx, point in _dataset_collection.flights_points.data.iterrows():
                    point_lat, point_lon = point['Latitude'], point['Longitude']
                    keep_point = True
                    
                    # Check if point falls within any excluded airspace
                    for airspace_id, boundary_points in excluded_boundaries.items():
                        if len(boundary_points) >= 3:  # Need at least 3 points for a polygon
                            # Use BlueSky's existing polygon check approach (same as used in ASAS/SSD)
                            try:
                                import pyclipper
                                # Convert boundary points to pyclipper format
                                polygon = [(float(pt[1]), float(pt[0])) for pt in boundary_points]  # lon, lat for pyclipper
                                point = (float(point_lon), float(point_lat))  # lon, lat
                                
                                if pyclipper.PointInPolygon(pyclipper.scale_to_clipper(point), 
                                                          pyclipper.scale_to_clipper(polygon)):
                                    keep_point = False
                                    break
                            except ImportError:
                                # Fallback to custom ray casting if pyclipper not available
                                if _point_in_polygon(point_lat, point_lon, boundary_points):
                                    keep_point = False
                                    break
                    
                    if keep_point:
                        points_to_keep.append(idx)
                
                # Filter the flight points data
                _dataset_collection.flights_points.data = _dataset_collection.flights_points.data.loc[points_to_keep]
                
                # Filter flights to only include those with remaining points
                if len(_dataset_collection.flights_points.data) > 0:
                    remaining_flight_ids = _dataset_collection.flights_points.data['ECTRL ID'].unique()
                    if _dataset_collection.flights is not None:
                        original_flights = len(_dataset_collection.flights.data)
                        _dataset_collection.flights.data = _dataset_collection.flights.data[
                            _dataset_collection.flights.data['ECTRL ID'].isin(remaining_flight_ids)
                        ]
                        print(f"Airspace exclusion: {original_points} -> {len(_dataset_collection.flights_points.data)} points, "
                              f"{original_flights} -> {len(_dataset_collection.flights.data)} flights remaining")
                else:
                    # No points remain, clear flights too
                    if _dataset_collection.flights is not None:
                        _dataset_collection.flights.data = _dataset_collection.flights.data.iloc[0:0]
                    print(f"Airspace exclusion: All flight points removed")
            
            # Finally, exclude the airspace boundaries themselves
            _dataset_collection.exclude_airspace(filters_dict['exclude_airspace'])
        
        # Apply aircraft type filtering (custom implementation)
        if 'aircraft_types' in filters_dict and filters_dict['aircraft_types']:
            aircraft_types = filters_dict['aircraft_types']
            print(f"Filtering aircraft types: {aircraft_types}")
            
            # Filter flights data
            if _dataset_collection.flights is not None and 'AC Type' in _dataset_collection.flights.data.columns:
                original_count = len(_dataset_collection.flights.data)
                filtered_flights = _dataset_collection.flights.data[
                    _dataset_collection.flights.data['AC Type'].isin(aircraft_types)
                ]
                _dataset_collection.flights.data = filtered_flights
                
                # Filter flight points to only include remaining flights
                if len(filtered_flights) > 0:
                    remaining_flight_ids = filtered_flights['ECTRL ID'].unique()
                    
                    # Filter flights_points data
                    if _dataset_collection.flights_points is not None:
                        original_points = len(_dataset_collection.flights_points.data)
                        _dataset_collection.flights_points.data = _dataset_collection.flights_points.data[
                            _dataset_collection.flights_points.data['ECTRL ID'].isin(remaining_flight_ids)
                        ]
                        print(f"Aircraft filter: {original_count} -> {len(filtered_flights)} flights, "
                              f"{original_points} -> {len(_dataset_collection.flights_points.data)} points")
                else:
                    # No flights match criteria, clear all data
                    if _dataset_collection.flights_points is not None:
                        _dataset_collection.flights_points.data = _dataset_collection.flights_points.data.iloc[0:0]
                    print(f"Aircraft filter: No flights match criteria")
        
        # Apply time filtering (custom implementation)
        if 'time_start' in filters_dict and 'time_end' in filters_dict:
            if filters_dict['time_start'] and filters_dict['time_end']:
                time_start = filters_dict['time_start']
                time_end = filters_dict['time_end']
                print(f"Filtering time range: {time_start} to {time_end}")
                
                # Convert GUI time format (HH:MM:SS) to seconds for comparison
                def time_to_seconds(time_str):
                    if isinstance(time_str, str) and ':' in time_str:
                        parts = time_str.split(':')
                        if len(parts) >= 3:
                            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    return 0
                    
                start_seconds = time_to_seconds(time_start)
                end_seconds = time_to_seconds(time_end)
                
                # Filter flight points based on time
                if _dataset_collection.flights_points is not None and 'Time Over' in _dataset_collection.flights_points.data.columns:
                    original_points = len(_dataset_collection.flights_points.data)
                    
                    def extract_time_seconds(time_str):
                        if isinstance(time_str, str):
                            # Handle full datetime format like "01-03-2015 05:55:00"
                            if ' ' in time_str and ':' in time_str:
                                time_part = time_str.split(' ')[1]  # Get "05:55:00"
                                return time_to_seconds(time_part)
                            # Handle simple time format like "05:55:00"
                            elif ':' in time_str:
                                return time_to_seconds(time_str)
                        return 0
                    
                    # Apply time filter
                    time_mask = _dataset_collection.flights_points.data['Time Over'].apply(
                        lambda x: start_seconds <= extract_time_seconds(x) <= end_seconds
                    )
                    
                    _dataset_collection.flights_points.data = _dataset_collection.flights_points.data[time_mask]
                    
                    # Filter flights to only include those with remaining points
                    if len(_dataset_collection.flights_points.data) > 0:
                        remaining_flight_ids = _dataset_collection.flights_points.data['ECTRL ID'].unique()
                        if _dataset_collection.flights is not None:
                            original_flights = len(_dataset_collection.flights.data)
                            _dataset_collection.flights.data = _dataset_collection.flights.data[
                                _dataset_collection.flights.data['ECTRL ID'].isin(remaining_flight_ids)
                            ]
                            print(f"Time filter: {original_points} -> {len(_dataset_collection.flights_points.data)} points, "
                                  f"{original_flights} -> {len(_dataset_collection.flights.data)} flights")
                    else:
                        # No points match time criteria, clear flights too
                        if _dataset_collection.flights is not None:
                            _dataset_collection.flights.data = _dataset_collection.flights.data.iloc[0:0]
                        print(f"Time filter: No points match time criteria")
        
        # Apply polygon filter (inclusion filter - keep only points inside polygon)
        if 'polygon_filter' in filters_dict and filters_dict['polygon_filter']:
            polygon_filter = filters_dict['polygon_filter']
            if polygon_filter.get('enabled', False) and polygon_filter.get('polygon_name'):
                polygon_name = polygon_filter['polygon_name']
                print(f"Applying polygon filter: {polygon_name}")
                
                # Get polygon coordinates using same method as random conflicts
                try:
                    import tempfile
                    import json
                    import time
                    import os
                    
                    print(f"DEBUG: Attempting to get polygon coordinates for '{polygon_name}' using SATG_PROC_EXPORT_POLY")
                    
                    # Send command to backend to export polygon coordinates (same as random conflicts)
                    from bluesky.ui.qtgl.console import process_cmdline
                    process_cmdline(f"SATG_PROC_EXPORT_POLY {polygon_name}")
                    
                    # Wait a moment for the command to process
                    time.sleep(0.5)
                    
                    # Read the exported coordinates from temp file
                    temp_dir = tempfile.gettempdir()
                    temp_file = os.path.join(temp_dir, f"satg_poly_{polygon_name.upper()}.json")
                    
                    polygon_coords = None
                    if os.path.exists(temp_file):
                        with open(temp_file, 'r') as f:
                            data = json.load(f)
                        
                        polygon_coords = data.get('coordinates', [])
                        
                        # Clean up temp file
                        try:
                            os.remove(temp_file)
                        except:
                            pass
                    
                    print(f"DEBUG: Retrieved coordinates: {polygon_coords}")
                    
                    if polygon_coords and len(polygon_coords) >= 3:  # Need at least 3 points for a polygon
                        if _dataset_collection.flights_points is not None:
                            original_points = len(_dataset_collection.flights_points.data)
                            points_to_keep = []
                            
                            for idx, point in _dataset_collection.flights_points.data.iterrows():
                                point_lat, point_lon = point['Latitude'], point['Longitude']
                                
                                # Check if point is inside the polygon (inclusion filter)
                                point_inside = False
                                try:
                                    import pyclipper
                                    # Convert polygon coordinates to pyclipper format
                                    polygon = [(float(lon), float(lat)) for lat, lon in polygon_coords]  # lon, lat for pyclipper
                                    point_xy = (float(point_lon), float(point_lat))  # lon, lat
                                    
                                    if pyclipper.PointInPolygon(pyclipper.scale_to_clipper(point_xy), 
                                                              pyclipper.scale_to_clipper(polygon)):
                                        point_inside = True
                                except ImportError:
                                    # No fallback needed as per user request
                                    print("Warning: pyclipper not available for polygon filtering")
                                    point_inside = True  # Skip filtering if pyclipper unavailable
                                
                                if point_inside:
                                    points_to_keep.append(idx)
                            
                            # Filter the flight points data to keep only points inside polygon
                            _dataset_collection.flights_points.data = _dataset_collection.flights_points.data.loc[points_to_keep]
                            
                            # Filter flights to only include those with remaining points
                            if len(_dataset_collection.flights_points.data) > 0:
                                remaining_flight_ids = _dataset_collection.flights_points.data['ECTRL ID'].unique()
                                if _dataset_collection.flights is not None:
                                    original_flights = len(_dataset_collection.flights.data)
                                    _dataset_collection.flights.data = _dataset_collection.flights.data[
                                        _dataset_collection.flights.data['ECTRL ID'].isin(remaining_flight_ids)
                                    ]
                                    print(f"Polygon filter: {original_points} -> {len(_dataset_collection.flights_points.data)} points, "
                                          f"{original_flights} -> {len(_dataset_collection.flights.data)} flights remaining")
                            else:
                                # No points remain inside polygon, clear flights too
                                if _dataset_collection.flights is not None:
                                    _dataset_collection.flights.data = _dataset_collection.flights.data.iloc[0:0]
                                print(f"Polygon filter: No flight points inside polygon")
                    else:
                        print(f"Warning: Polygon '{polygon_name}' not found or has fewer than 3 points, skipping polygon filter")
                except Exception as e:
                    print(f"Error loading polygon '{polygon_name}': {e}")
                    print("Skipping polygon filter")
        
        print("Filters applied successfully using original TraffixGen methods!")
        return True
        
    except Exception as e:
        print(f"Error applying filters: {e}")
        return False

@stack.command
def traffixgen_export_to_satg():
    """Export processed Eurocontrol data directly to SATG using original TraffixGen data access."""
    global _dataset_collection
    
    try:
        if _dataset_collection is None:
            print("Error: No Eurocontrol data loaded. Use TRAFFIXGEN LOAD_EUROCONTROL first.")
            return False
        
        # Get processed data using original TraffixGen property access
        flights_df = _dataset_collection.flights.data
        points_df = _dataset_collection.flights_points.data
        
        # Debug: Print data sizes at export time
        print(f"Export debug: flights_df has {len(flights_df)} flights")
        print(f"Export debug: points_df has {len(points_df)} points")
        if len(flights_df) > 0:
            print(f"Export debug: flight IDs = {flights_df['ECTRL ID'].tolist()}")
        if len(points_df) > 0:
            print(f"Export debug: point flight IDs = {sorted(points_df['ECTRL ID'].unique())}")
        
        if flights_df.empty or points_df.empty:
            print("Error: No valid data after filtering.")
            return False
        
        # Convert to SATG format with proper callsigns
        flights_data = []
        for _, row in flights_df.iterrows():
            ectrl_id = str(row.get('ECTRL ID', ''))
            ac_operator = str(row.get('AC Operator', ''))  # Try to get operator info
            
            # Generate proper callsign based on available data
            if ectrl_id.isdigit():
                # Use operator code if available, otherwise use generic TFC
                if ac_operator and ac_operator != '' and ac_operator != 'nan':
                    callsign = f"{ac_operator}{int(ectrl_id) % 9999:04d}"
                else:
                    callsign = f"TFC{int(ectrl_id) % 9999:04d}"  # Traffic (generic)
            else:
                # Use existing callsign if already proper format
                callsign = ectrl_id
            
            flight = {
                'ECTRL ID': ectrl_id,  # Keep original ID for data matching
                'Callsign': callsign,  # Add callsign as separate field
                'ADEP': str(row.get('ADEP', '')), 
                'ADES': str(row.get('ADES', '')),
                'AC Type': str(row.get('AC Type', '')),
                'AC Operator': ac_operator
            }
            flights_data.append(flight)
            print(f"[DEBUG] Exported flight: ECTRL_ID={ectrl_id}, Callsign={callsign}, AC_Operator={ac_operator}")
        
        # NORMALIZE TIMES: Find earliest time and make it time 0 for scenario
        from datetime import datetime, timedelta
        import pandas as pd
        
        # Convert all Time Over values to datetime objects for comparison
        valid_times = []
        for _, row in points_df.iterrows():
            time_over_str = str(row.get('Time Over', ''))
            try:
                if time_over_str and time_over_str != 'nan':
                    # Parse time - handle different formats
                    if ' ' in time_over_str:
                        # Format like "01-03-2015 05:30:00"
                        dt = pd.to_datetime(time_over_str)
                    else:
                        # Format like "05:30:00" - treat as time only
                        if ':' in time_over_str:
                            dt = pd.to_datetime(f"2000-01-01 {time_over_str}")
                        else:
                            # Just hour number
                            dt = pd.to_datetime(f"2000-01-01 {time_over_str}:00:00")
                    valid_times.append(dt)
            except:
                continue
        
        if not valid_times:
            print("Warning: No valid times found in data")
            earliest_time = pd.to_datetime("2000-01-01 00:00:00")
        else:
            earliest_time = min(valid_times)
        
        print(f"Normalizing times: earliest time {earliest_time.strftime('%H:%M:%S')} becomes 00:00:00 in scenario")
        
        points_data = []
        for _, row in points_df.iterrows():
            ectrl_id = str(row.get('ECTRL ID', ''))
            
            # Generate same callsign mapping as used for flights
            # First check if we have operator info in flights data
            matching_flight = flights_df[flights_df['ECTRL ID'] == ectrl_id]
            ac_operator = ''
            adep = ''
            if not matching_flight.empty:
                ac_operator = str(matching_flight.iloc[0].get('AC Operator', ''))
                adep = str(matching_flight.iloc[0].get('ADEP', ''))
            
            if ectrl_id.isdigit():
                # Use same logic as flights data
                if ac_operator and ac_operator != '' and ac_operator != 'nan':
                    callsign = f"{ac_operator}{int(ectrl_id) % 9999:04d}"
                else:
                    callsign = f"TFC{int(ectrl_id) % 9999:04d}"
            else:
                callsign = ectrl_id
            
            # Convert Time Over to normalized time relative to earliest
            time_over_str = str(row.get('Time Over', ''))
            
            try:
                if time_over_str and time_over_str != 'nan':
                    # Parse the time
                    if ' ' in time_over_str:
                        # Format like "01-03-2015 05:30:00"
                        dt = pd.to_datetime(time_over_str)
                    else:
                        # Format like "05:30:00" - treat as time only
                        if ':' in time_over_str:
                            dt = pd.to_datetime(f"2000-01-01 {time_over_str}")
                        else:
                            # Just hour number
                            dt = pd.to_datetime(f"2000-01-01 {time_over_str}:00:00")
                    
                    # Calculate time difference from earliest time
                    time_diff = dt - earliest_time
                    total_seconds = int(time_diff.total_seconds())
                    
                    # Convert to HH:MM:SS format starting from 00:00:00
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60
                    
                    time_over_formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                else:
                    time_over_formatted = "00:00:00"
            except Exception as e:
                print(f"Warning: Could not parse time '{time_over_str}': {e}")
                time_over_formatted = "00:00:00"
            
            point = {
                'ECTRL ID': ectrl_id,  # Keep original ID for data matching
                'Callsign': callsign,  # Add callsign as separate field
                'Sequence Number': int(row.get('Sequence Number', 0)),
                'Time Over': time_over_formatted,  # Use converted time format
                'Flight Level': float(row.get('Flight Level', 0)),
                'Latitude': float(row.get('Latitude', 0)),
                'Longitude': float(row.get('Longitude', 0)),
                'Delay Time Over': float(row.get('Delay Time Over', 0)),
                'Dev Latitude': float(row.get('Dev Latitude', 0)),
                'Dev Longitude': float(row.get('Dev Longitude', 0)),
                'Dev Flight Level': float(row.get('Dev Flight Level', 0)),
                'ground_speed': float(row.get('ground_speed', 0)),
                'vertical_speed': float(row.get('vertical_speed', 0)),
                'heading': float(row.get('heading', 0)),
                'pitch': float(row.get('pitch', 0))
            }
            points_data.append(point)
        
        # Convert to JSON strings
        import json
        flights_json = json.dumps(flights_data)
        points_json = json.dumps(points_data)
        
        # Call SATG function directly (like how SATGgui calls SATG functions)
        from . import SATG
        success, message = SATG.SATG_RL_LOAD_DATA(flights_json, points_json)
        
        if success:
            print(f"Successfully exported {len(flights_data)} flights and {len(points_data)} points to SATG!")
            print("Data is now ready for realistic replay in SATG.")
            return True
        else:
            print(f"Error: Failed to transfer data to SATG - {message}")
            return False
            
    except Exception as e:
        print(f"Error exporting to SATG: {e}")
        return False

def _point_in_polygon(lat, lon, polygon_points):
    """
    Check if a point (lat, lon) is inside a polygon defined by polygon_points.
    Uses the ray casting algorithm.
    
    Args:
        lat, lon: Point coordinates to check
        polygon_points: Array of [lat, lon] pairs defining the polygon boundary
    
    Returns:
        bool: True if point is inside polygon, False otherwise
    """
    if len(polygon_points) < 3:
        return False
    
    x, y = lat, lon
    n = len(polygon_points)
    inside = False
    
    p1x, p1y = polygon_points[0]
    for i in range(1, n + 1):
        p2x, p2y = polygon_points[i % n]
        
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    
    return inside