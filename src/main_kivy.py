#
# FULL FILE CONTENT: main_kivy.py 
# ---------------------------------------------------------
import os
import sys
import threading
import signal
import atexit
import time
from datetime import datetime
import subprocess
import shutil

# --- 0. OS ENVIRONMENT SETUP ---
os.environ['SDL_VIDEO_X11_WMCLASS'] = "Fermentation Vault"

# --- 1. KIVY CONFIGURATION ---
from kivy.config import Config
Config.set('graphics', 'width', '800')
Config.set('graphics', 'height', '417')
Config.set('graphics', 'resizable', '0')

from kivy.app import App
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.properties import StringProperty, ListProperty, BooleanProperty
from kivy.uix.popup import Popup
from kivy.clock import Clock, mainthread

# --- 2. BACKEND IMPORTS ---
try:
    from settings_manager import SettingsManager
    from relay_control import RelayControl
    from temperature_controller import TemperatureController
    from api_manager import APIManager
    from notification_manager import NotificationManager
    from fg_calculator import FGCalculator
except ImportError as e:
    print(f"CRITICAL IMPORT ERROR: {e}")
    SettingsManager = None
    RelayControl = None

# --- HARDWARE CONFIGURATION ---
# MATCHES TKINTER CONFIGURATION EXACTLY (Source of Truth)
RELAY_PINS = {
    'Heat': 26, # Board Pin 37
    'Cool': 20, # Board Pin 38
    'Fan': 21   # Board Pin 40
}

# --- 3. ROBUST SHUTDOWN LOGIC ---
def failsafe_cleanup():
    print("\n[System] Executing Failsafe Cleanup...")
    try:
        app = App.get_running_app()
        if app and hasattr(app, 'relay_control'):
            if hasattr(app, 'temp_controller'):
                app.temp_controller.stop_monitoring()
            app.relay_control.cleanup_gpio()
            print("[System] GPIO cleaned up via App reference.")
            return
    except Exception as e:
        print(f"[System] Warning: App reference cleanup failed: {e}")

    try:
        if RelayControl and SettingsManager:
            print("[System] Attempting fallback cleanup with fresh instance...")
            sm = SettingsManager() 
            # Force 'Configured' to skip wizard during cleanup
            sm.set("relay_logic_configured", True) 
            rc = RelayControl(sm, RELAY_PINS)
            rc.cleanup_gpio()
            print("[System] GPIO cleaned up via Fresh Instance.")
    except Exception as e:
        print(f"[System] Failsafe Error: {e}")

def handle_signal(signum, frame):
    print(f"\n[System] Caught Signal {signum}. Shutting down safely...")
    failsafe_cleanup()
    os._exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# --- 4. COMPATIBILITY CLASSES ---
class TkinterRootShim:
    def after(self, delay_ms, callback, *args):
        Clock.schedule_once(lambda dt: callback(*args), delay_ms / 1000.0)

class KivyVarWrapper:
    def __init__(self, setter_callback):
        self.setter_callback = setter_callback
    def set(self, value):
        self.setter_callback(value)
    def get(self):
        return None 

class KivyUIManagerAdapter:
    def __init__(self, app):
        self.app = app
        self.root = TkinterRootShim() 
        self.monitoring_var = app.monitoring_var
        self.control_mode_var = app.control_mode_var
        self.fg_status_var = app.fg_status_var
        self.fg_value_var = app.fg_value_var
        self.og_display_var = app.og_display_var
        self.sg_display_var = app.sg_display_var
        self.og_timestamp_var = app.og_timestamp_var
        self.sg_timestamp_var = app.sg_timestamp_var

    def log_system_message(self, message):
        self.app.log_system_message(message)

    def push_data_update(self, **kwargs):
        self.app.push_data_update(**kwargs)
        
    def _update_data_display(self):
        pass

    @property
    def api_manager(self): return self.app.api_manager
    @property
    def temp_controller(self): return self.app.temp_controller
    @property
    def fg_calculator_instance(self): return self.app.fg_calculator_instance

# --- 5. SCREEN CLASSES & POPUP ---
class DashboardScreen(Screen): pass
class LogScreen(Screen): pass
class SettingsScreen(Screen): pass
class InfoScreen(Screen): pass
class DirtyPopup(Popup): pass

# --- 6. MAIN APP CLASS ---
class FermVaultApp(App):
    # --- UI Properties ---
    beer_actual = StringProperty("--.-")
    
    # --- DYNAMIC COLOR PROPERTIES ---
    # Default to White [1, 1, 1, 1]
    beer_actual_color = ListProperty([1, 1, 1, 1])
    ambient_actual_color = ListProperty([1, 1, 1, 1])
    
    # --- NEW SINGLE SOURCE OF TRUTH FOR BLUE ---
    # This replaces the hardcoded numbers [0.2, 0.8, 1, 1] used in the Header
    col_theme_blue = ListProperty([0.2, 0.8, 1, 1])
    
    beer_target = StringProperty("--.-")
    ambient_actual = StringProperty("--.-")
    ambient_target = StringProperty("--.-")
    ambient_range = StringProperty("--.-")
    heater_color = ListProperty([0.2, 0.2, 0.2, 1]) 
    cooler_color = ListProperty([0.2, 0.2, 0.2, 1])
    control_mode_display = StringProperty("Ambient")
    monitoring_state = StringProperty("OFF")
    log_text = StringProperty("[System] UI Initialized.\n")
    warning_message = StringProperty("")
    
    # --- Status Bar Properties ---
    warning_bg_color = ListProperty([0.2, 0.8, 0.2, 1]) 
    current_sensor_error = StringProperty("")
    
    # --- Settings Properties (SOURCE OF TRUTH NAMES) ---
    available_sensors = ListProperty(["unassigned"])
    
    # --- UPDATE & MAINTENANCE PROPERTIES ---
    update_log_text = StringProperty("Click CHECK to check for app updates.\n")
    is_update_available = BooleanProperty(False)
    is_install_successful = BooleanProperty(False)
    
    # SOURCE OF TRUTH: settings_manager.py -> system_settings
    ds18b20_beer_sensor = StringProperty("unassigned")
    ds18b20_ambient_sensor = StringProperty("unassigned")
    relay_active_high = BooleanProperty(False)
    pid_logging_enabled = BooleanProperty(False) # Renamed from log_csv_enabled
    
    # SOURCE OF TRUTH: settings_manager.py -> control_settings
    ambient_hold_f = StringProperty("0.0")
    beer_hold_f = StringProperty("0.0")
    ramp_up_hold_f = StringProperty("0.0")
    fast_crash_hold_f = StringProperty("0.0")
    ramp_up_duration_hours = StringProperty("0.0")
    
    # SOURCE OF TRUTH: settings_manager.py -> compressor_protection_settings
    cooling_dwell_time_s = StringProperty("0.0")
    max_cool_runtime_s = StringProperty("0.0")
    fail_safe_shutdown_time_s = StringProperty("0.0")

    # SOURCE OF TRUTH: settings_manager.py -> system_settings (PID)
    pid_kp = StringProperty("0.0")
    pid_ki = StringProperty("0.0")
    pid_kd = StringProperty("0.0")
    ambient_mode_deadband_f = StringProperty("0.0")
    pid_envelope_f = StringProperty("0.0")
    crash_mode_envelope_f = StringProperty("0.0")
    ramp_pre_ramp_tolerance_f = StringProperty("0.0")
    ramp_thermostatic_deadband_f = StringProperty("0.0")
    ramp_pid_landing_zone_f = StringProperty("0.0")
    
    # --- Dirty Tracking ---
    is_settings_dirty = BooleanProperty(False)
    staged_changes = {} 
    
    # NO PROPERTY MAP - KEYS ARE DIRECT
    
    # --- Backend References ---
    notification_manager = None
    temp_controller = None
    relay_control = None
    
    # --- THREADING CONTROL ---
    _standby_running = False
    _standby_thread = None

    @mainthread
    def log_system_message(self, message):
        timestamp = datetime.now().strftime("[%H:%M:%S]")
        self.log_text = f"{timestamp} {message}\n" + self.log_text
        if len(self.log_text) > 5000: self.log_text = self.log_text[:4000]

    def build(self):
        self.title = "Fermentation Vault"
        self.sm = ScreenManager()
        self.dashboard_screen = DashboardScreen(name='dashboard')
        self.log_screen = LogScreen(name='log')
        self.settings_screen = SettingsScreen(name='settings')
        self.info_screen = InfoScreen(name='info')
        
        self.sm.add_widget(self.dashboard_screen)
        self.sm.add_widget(self.log_screen)
        self.sm.add_widget(self.settings_screen)
        self.sm.add_widget(self.info_screen)
        self.sm.current = 'dashboard'

        Clock.schedule_once(self.start_backend, 0.2)
        return self.sm

    def start_backend(self, dt=None):
        if SettingsManager is None:
            self.log_system_message("BACKEND ERROR: Modules not found.")
            return

        try:
            self.log_system_message("Initializing Backend...")
            
            # 1. Initialize Settings
            self.settings_manager = SettingsManager() 
            
            # --- CRITICAL FIX: Force Relay Hardware Configuration ---
            if not self.settings_manager.get("relay_logic_configured"):
                self.log_system_message("Setup: Auto-enabling Relay Hardware (Active Low).")
                self.settings_manager.set("relay_logic_configured", True)
                self.settings_manager.set("relay_active_high", False) 
            # --------------------------------------------------------

            # 2. Initialize Components
            self.relay_control = RelayControl(self.settings_manager, RELAY_PINS)
            self.api_manager = APIManager(self.settings_manager)
            self.temp_controller = TemperatureController(self.settings_manager, self.relay_control)
            
            # 3. Variable Wrappers
            self.monitoring_var = KivyVarWrapper(lambda v: setattr(self, 'monitoring_state', v))
            self.control_mode_var = KivyVarWrapper(self._sync_control_mode_from_backend)
            
            # Shims for UI Adapter compatibility
            self.fg_status_var = KivyVarWrapper(lambda v: None)
            self.fg_value_var = KivyVarWrapper(lambda v: None)
            self.og_display_var = KivyVarWrapper(lambda v: None)
            self.sg_display_var = KivyVarWrapper(lambda v: None)
            self.og_timestamp_var = KivyVarWrapper(lambda v: None)
            self.sg_timestamp_var = KivyVarWrapper(lambda v: None)
            
            self.ui_adapter = KivyUIManagerAdapter(self)
            self.fg_calculator_instance = FGCalculator(self.settings_manager, self.api_manager)
            self.notification_manager = NotificationManager(self.settings_manager, self.ui_adapter)
            
            # 4. Wiring
            self.temp_controller.notification_manager = self.notification_manager
            self.notification_manager.ui = self.ui_adapter
            self.notification_manager.ui.app = self
            self.relay_control.set_logger(self.log_system_message)
            
            self.notification_manager.start_scheduler()
            
            self._refresh_all_settings_from_manager()
            Clock.schedule_interval(self.tick, 1.0)
            
            self.log_system_message("Backend initialized successfully.")
            
            # 5. STARTUP LOGIC: FORCE OFF (Safety Default)
            self.settings_manager.set("monitoring_state", "OFF")
            self.monitoring_state = "OFF"
            
            self.log_system_message("System Started. Monitoring is OFF (Safe Standby).")
            self.start_standby_loop()

        except Exception as e:
            self.log_system_message(f"CRITICAL BACKEND ERROR: {e}")
            import traceback
            traceback.print_exc()

    # --- SAFE STANDBY LOGIC (THREADED) ---
    def start_standby_loop(self):
        """Starts a background thread for safe monitoring to prevent UI blocking."""
        if self._standby_running: 
            return # Already running
            
        self._standby_running = True
        self._standby_thread = threading.Thread(target=self._standby_worker, daemon=True)
        self._standby_thread.start()
        print("[App] Safe Standby Loop STARTED (Threaded).")

    def stop_standby_loop(self):
        """Stops the background thread."""
        if self._standby_running:
            self._standby_running = False
            # Daemon thread will exit naturally
            print("[App] Safe Standby Loop STOPPED.")

    def _standby_worker(self):
        """
        Background worker that mimics the monitoring loop but forces relays OFF.
        Runs in a separate thread to ensure blocking sensor reads don't freeze the UI.
        """
        while self._standby_running:
            if self.temp_controller and self.relay_control:
                try:
                    # A. Safety Tick: Force logic to "OFF" state.
                    self.relay_control.set_desired_states(False, False, "OFF")
                    
                    # B. UI Update: Read sensors (Blocking I/O) and push data.
                    self.temp_controller.update_control_logic_and_ui_data()
                except Exception as e:
                    print(f"[Standby Thread Error] {e}")
            
            # Sleep to regulate loop speed (approx 1Hz)
            time.sleep(1.0)

    # --- MONITORING TOGGLES ---
    def toggle_monitoring(self, new_state):
        if not self.temp_controller: return

        if new_state == "ON":
            self.stop_standby_loop()
            self.temp_controller.start_monitoring()
            self.log_system_message("Monitoring STARTED (Active Control).")
        else:
            self.temp_controller.stop_monitoring()
            self.start_standby_loop()
            self.log_system_message("Monitoring STOPPED (Safe Standby).")

    def toggle_monitoring_state(self, is_active):
        state_str = "ON" if is_active else "OFF"
        self.toggle_monitoring(state_str)

    # --- SETTINGS REFRESH (Source of Truth) ---
    def _refresh_all_settings_from_manager(self):
        if not hasattr(self, 'settings_manager'): return
        self.staged_changes.clear()
        
        def s(key, default, fmt=".1f"): return f"{float(self.settings_manager.get(key, default)):{fmt}}"
        
        # Targets
        self.ambient_hold_f = s("ambient_hold_f", 37.0)
        self.beer_hold_f = s("beer_hold_f", 55.0)
        self.ramp_up_hold_f = s("ramp_up_hold_f", 68.0)
        self.fast_crash_hold_f = s("fast_crash_hold_f", 34.0)
        self.ramp_up_duration_hours = s("ramp_up_duration_hours", 30.0)
        
        # System
        self.ds18b20_beer_sensor = self.settings_manager.get("ds18b20_beer_sensor", "unassigned")
        self.ds18b20_ambient_sensor = self.settings_manager.get("ds18b20_ambient_sensor", "unassigned")
        self.relay_active_high = self.settings_manager.get("relay_active_high", False)
        self.pid_logging_enabled = self.settings_manager.get("pid_logging_enabled", False)
        
        # Compressor Protection (Source of Truth Keys)
        comp = self.settings_manager.get_all_compressor_protection_settings()
        # Convert Seconds (Backend) to Minutes (UI)
        self.cooling_dwell_time_s = f"{comp.get('cooling_dwell_time_s', 180) / 60.0:.1f}"
        self.max_cool_runtime_s = f"{comp.get('max_cool_runtime_s', 7200) / 60.0:.1f}"
        self.fail_safe_shutdown_time_s = f"{comp.get('fail_safe_shutdown_time_s', 3600) / 60.0:.1f}"

        # PID & Tuning
        self.pid_kp = s("pid_kp", 2.0)
        self.pid_ki = s("pid_ki", 0.03, ".4f")
        self.pid_kd = s("pid_kd", 20.0)
        self.ambient_mode_deadband_f = s("ambient_mode_deadband_f", 1.0)
        self.pid_envelope_f = s("pid_envelope_f", 1.0)
        self.crash_mode_envelope_f = s("crash_mode_envelope_f", 2.0)
        self.ramp_pre_ramp_tolerance_f = s("ramp_pre_ramp_tolerance_f", 0.2)
        self.ramp_thermostatic_deadband_f = s("ramp_thermostatic_deadband_f", 0.1)
        self.ramp_pid_landing_zone_f = s("ramp_pid_landing_zone_f", 0.5)
        
        self.is_settings_dirty = False

    def tick(self, dt):
        self._update_warning_status()

    def _update_warning_status(self):
        """Priority 1: Error, Priority 2: Delay, Priority 3: Healthy"""
        if self.current_sensor_error:
            self.warning_message = self.current_sensor_error
            self.warning_bg_color = [0.9, 0, 0, 1] # Red
            return

        if hasattr(self, 'relay_control'):
            restriction = self.settings_manager.get("cool_restriction_status", "")
            if restriction:
                self.warning_message = f"Protection: {restriction}"
                self.warning_bg_color = [1, 0.6, 0, 1] # Orange
                return

        self.warning_message = "System Healthy"
        self.warning_bg_color = [0.2, 0.8, 0.2, 1] # Green

    @mainthread
    def push_data_update(self, **kwargs):
        def fmt(val):
            try: return f"{float(val):.1f}"
            except (ValueError, TypeError): return "--.-"

        # 1. Update Text Values
        self.beer_actual = fmt(kwargs.get('beer_temp'))
        self.beer_target = fmt(kwargs.get('beer_setpoint'))
        self.ambient_actual = fmt(kwargs.get('amb_temp'))
        self.ambient_target = fmt(kwargs.get('amb_target'))
        
        amin = kwargs.get('amb_min')
        amax = kwargs.get('amb_max')
        if amin is not None and amax is not None:
            self.ambient_range = f"{fmt(amin)} - {fmt(amax)}"
        else:
            self.ambient_range = "--.- - --.-"
            
        h_state = str(kwargs.get('heat_state', 'OFF'))
        c_state = str(kwargs.get('cool_state', 'OFF'))
        
        # --- COLOR LOGIC UPDATE: Use self.col_theme_blue ---
        self.heater_color = [0.8, 0, 0, 1] if "HEATING" in h_state else [0.2, 0.2, 0.2, 1]
        self.cooler_color = self.col_theme_blue if "COOLING" in c_state else [0.2, 0.2, 0.2, 1]

        mode_internal = kwargs.get('current_mode', 'Ambient Hold')
        self._sync_control_mode_from_backend(mode_internal)
        
        self.current_sensor_error = kwargs.get('sensor_error_message', "")
        self._update_warning_status()

        # 2. Dynamic Hero Colors
        COL_WHITE = [1, 1, 1, 1]
        COL_LGRAY = [0.7, 0.7, 0.7, 1]
        COL_GREEN = [0, 0.8, 0, 1]
        COL_RED   = [0.8, 0, 0, 1]
        # USE THE THEME BLUE HERE
        COL_BLUE  = self.col_theme_blue

        if self.monitoring_state == "OFF":
            self.beer_actual_color = COL_WHITE
            self.ambient_actual_color = COL_WHITE
        else:
            # Ambient Logic
            try:
                a_act = float(kwargs.get('amb_temp'))
                a_max = float(amax) if amax is not None else 100.0
                a_min = float(amin) if amin is not None else 0.0
                
                if a_act > a_max: self.ambient_actual_color = COL_RED
                elif a_act < a_min: self.ambient_actual_color = COL_BLUE
                else: self.ambient_actual_color = COL_GREEN
            except (ValueError, TypeError):
                self.ambient_actual_color = COL_WHITE

            # Beer Logic
            if "Ambient" in self.control_mode_display:
                self.beer_actual_color = COL_LGRAY
            else:
                try:
                    b_act = float(kwargs.get('beer_temp'))
                    b_tgt = float(kwargs.get('beer_setpoint'))
                    delta = b_act - b_tgt
                    
                    if delta >= 0.3: self.beer_actual_color = COL_RED
                    elif delta <= -0.3: self.beer_actual_color = COL_BLUE
                    else: self.beer_actual_color = COL_GREEN
                except (ValueError, TypeError):
                    self.beer_actual_color = COL_WHITE

    # --- OTHER METHODS ---
    def scan_sensors(self):
        if not hasattr(self, 'temp_controller') or not self.temp_controller: return
        self._refresh_all_settings_from_manager()
        self.log_system_message("Scanning for sensors...")
        def _scan():
            found = self.temp_controller.detect_ds18b20_sensors()
            if "unassigned" not in found: found.insert(0, "unassigned")
            Clock.schedule_once(lambda dt: setattr(self, 'available_sensors', found))
        threading.Thread(target=_scan, daemon=True).start()

    def stage_setting_change(self, key, new_value):
        # DIRECT KEY USAGE: The key IS the property name (except for formatting).
        self.staged_changes[key] = new_value
        self.is_settings_dirty = True
        
        # Update the UI property directly using the key
        if hasattr(self, key):
            # Special handling only for float formatting
            if isinstance(new_value, (float, int)):
                fmt = ".4f" if key == "pid_ki" else ".1f"
                setattr(self, key, f"{float(new_value):{fmt}}")
            else:
                setattr(self, key, str(new_value))

    def stage_text_input(self, key, text_value):
        try:
            val = float(text_value)
            self.stage_setting_change(key, val)
        except ValueError: pass

    def adjust_target(self, key, delta):
        try:
            current = float(self.staged_changes.get(key, self.settings_manager.get(key, 0.0)))
            self.stage_setting_change(key, current + delta)
        except: pass

    def save_target_from_slider(self, key, value):
        try:
            new_val = float(value)
            if hasattr(self, key):
                current_ui = float(getattr(self, key))
                if abs(new_val - current_ui) > 0.01:
                    self.stage_setting_change(key, new_val)
        except: pass

    def save_sensor_setting(self, sensor_type, value):
        # Map simple type to TRUE Key
        key = "ds18b20_beer_sensor" if sensor_type == "beer" else "ds18b20_ambient_sensor"
        self.stage_setting_change(key, value)

    def set_relay_logic(self, active_high):
        self.stage_setting_change("relay_active_high", active_high)

    def check_unsaved_changes(self):
        if self.is_settings_dirty: DirtyPopup().open()
        else: self.go_to_screen('dashboard', 'right')

    def discard_changes(self):
        self._refresh_all_settings_from_manager()
        self.go_to_screen('dashboard', 'right')

    def save_and_exit(self):
        if not hasattr(self, 'settings_manager'): return
        self.log_system_message("Saving Settings...")
        
        # Keys requiring Minute->Second conversion (Source of Truth)
        cooling_keys = ["cooling_dwell_time_s", "max_cool_runtime_s", "fail_safe_shutdown_time_s"]
        cooling_update = {}
        
        for key, val in self.staged_changes.items():
            if key in cooling_keys:
                cooling_update[key] = float(val) * 60.0
            else:
                self.settings_manager.set(key, val)
                
        if cooling_update:
            current = self.settings_manager.get_all_compressor_protection_settings()
            current.update(cooling_update)
            self.settings_manager.save_compressor_protection_settings(current)
            
        if hasattr(self, 'temp_controller') and self.temp_controller and hasattr(self.temp_controller, 'pid'):
            self.temp_controller.pid.Kp = float(self.settings_manager.get("pid_kp", 2.0))
            self.temp_controller.pid.Ki = float(self.settings_manager.get("pid_ki", 0.03))
            self.temp_controller.pid.Kd = float(self.settings_manager.get("pid_kd", 20.0))

        if "relay_active_high" in self.staged_changes:
            self.settings_manager.set("relay_logic_configured", True)
            self.relay_control.update_relay_logic()
            
        self.staged_changes.clear()
        self.is_settings_dirty = False
        self.go_to_screen('dashboard', 'right')

    def reset_targets_to_defaults(self):
        defaults = {
            "ambient_hold_f": 68.0, "beer_hold_f": 68.0, "ramp_up_hold_f": 68.0, 
            "ramp_up_duration_hours": 24.0, "fast_crash_hold_f": 34.0,
            
            # TRUE COMPRESSOR KEYS
            "cooling_dwell_time_s": 3.0,
            "max_cool_runtime_s": 120.0,
            "fail_safe_shutdown_time_s": 60.0,
            
            "pid_kp": 2.0, "pid_ki": 0.03, "pid_kd": 20.0,
            "ambient_mode_deadband_f": 1.0, "pid_envelope_f": 1.0, "crash_mode_envelope_f": 2.0,
            "ramp_pre_ramp_tolerance_f": 0.2, "ramp_thermostatic_deadband_f": 0.1, "ramp_pid_landing_zone_f": 0.5
        }
        for key, val in defaults.items(): self.stage_setting_change(key, val)

    def set_control_mode(self, display_mode):
        if not hasattr(self, 'settings_manager'): return
        map_ui_to_internal = {
            "Ambient": "Ambient Hold", "Beer": "Beer Hold",
            "Ramp": "Ramp-Up", "Crash": "Fast Crash"
        }
        internal_mode = map_ui_to_internal.get(display_mode, "Ambient Hold")
        self.settings_manager.set("control_mode", internal_mode)
        if internal_mode != "Ramp-Up":
            self.temp_controller.reset_ramp_state()

    def _sync_control_mode_from_backend(self, internal_mode):
        map_internal_to_ui = {
            "Ambient Hold": "Ambient", "Beer Hold": "Beer",
            "Ramp-Up": "Ramp", "Fast Crash": "Crash", "OFF": "Ambient"
        }
        ui_value = map_internal_to_ui.get(internal_mode, "Ambient")
        if self.control_mode_display != ui_value:
            self.control_mode_display = ui_value

    def go_to_screen(self, screen_name, direction):
        self.sm.transition.direction = direction
        self.sm.current = screen_name

    def on_stop(self):
        print("[App] on_stop called. FORCING EXIT.")
        failsafe_cleanup()
        os._exit(0)
        
    def check_for_updates(self):
        self.update_log_text = "Checking for updates...\n"
        self.is_update_available = False
        self.is_install_successful = False
        
        def _check():
            try:
                # Fetch latest data from origin (Blocking I/O)
                subprocess.check_output(["git", "fetch"], stderr=subprocess.STDOUT)
                
                # Check status (Blocking I/O)
                status = subprocess.check_output(["git", "status", "-uno"], text=True)
                
                # Define the UI update logic to run back on the main thread
                def update_ui(dt):
                    if "behind" in status:
                        self.update_log_text += "\n[UPDATE AVAILABLE]\nA new version is available on GitHub.\nClick INSTALL to proceed."
                        self.is_update_available = True
                    elif "up to date" in status:
                        self.update_log_text += "\n[SYSTEM IS CURRENT]\nYou are running the latest version."
                    else:
                        self.update_log_text += f"\n[STATUS UNKNOWN]\nGit status returned:\n{status}"

                Clock.schedule_once(update_ui, 0)
                    
            except Exception as e:
                # FIX: Convert exception to string IMMEDIATELY
                err_msg = str(e)
                def report_error(dt):
                    self.update_log_text += f"\n[ERROR] Check failed:\n{err_msg}"
                Clock.schedule_once(report_error, 0)
        
        threading.Thread(target=_check, daemon=True).start()

    #
# Replace the existing run_update_script method

    def run_update_script(self):
        self.update_log_text += "\n\n[STARTING INSTALLATION]...\n"
        self.is_update_available = False 
        
        def _install():
            script_url = "https://github.com/keglevelmonitor/fermvault_lite/raw/main/update.sh"
            
            try:
                # 1. Determine Project Root (One level up from 'src') 
                # current file is in .../fermvault_lite/src
                src_dir = os.path.dirname(os.path.abspath(__file__))
                project_root = os.path.dirname(src_dir)
                
                local_script_path = os.path.join(project_root, "update.sh")

                # 2. Download the script to PROJECT ROOT
                msg_dl = f"Downloading update script to {local_script_path}...\n"
                Clock.schedule_once(lambda dt: self._append_update_log(msg_dl), 0)
                
                subprocess.run(["curl", "-L", "-o", local_script_path, script_url], check=True)
                subprocess.run(["chmod", "+x", local_script_path], check=True)
                
                # 3. Run the script FROM PROJECT ROOT
                Clock.schedule_once(lambda dt: self._append_update_log("Executing update.sh...\n"), 0)
                
                process = subprocess.Popen(
                    ["./update.sh"],
                    cwd=project_root, # <--- CRITICAL FIX: Run inside project root
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                
                # Read output line by line
                for line in process.stdout:
                    Clock.schedule_once(lambda dt, l=line: self._append_update_log(l), 0)
                
                process.wait()
                
                if process.returncode == 0:
                    Clock.schedule_once(lambda dt: self._append_update_log("\n[SUCCESS] Update finished successfully.\nClick RESTART APP to apply changes."), 0)
                    self.is_install_successful = True
                else:
                    code = process.returncode
                    Clock.schedule_once(lambda dt: self._append_update_log(f"\n[FAILURE] Script exited with code {code}"), 0)

            except Exception as e:
                err_msg = str(e)
                Clock.schedule_once(lambda dt: self._append_update_log(f"\n[CRITICAL ERROR] {err_msg}"), 0)

        threading.Thread(target=_install, daemon=True).start()

    def _append_update_log(self, text):
        self.update_log_text += text

    def restart_application(self):
        self.log_system_message("RESTARTING APPLICATION...")
        self.stop() # Triggers on_stop failsafe
        # Wait briefly for cleanup then restart
        time.sleep(1)
        os.execl(sys.executable, sys.executable, *sys.argv)

if __name__ == '__main__':
    FermVaultApp().run()
