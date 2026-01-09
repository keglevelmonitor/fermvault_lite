# 
# fermvault_lite app 
# FULL FILE CONTENT: main_kivy.py 
# ---------------------------------------------------------
import os
import sys
import threading
import signal
import atexit
import time
# --- NEW IMPORT ---
import multiprocessing
# ------------------
from datetime import datetime
import subprocess
import shutil

# --- ADD THESE LINES TO SILENCE DEBUG LOGS ---
import logging
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
# ---------------------------------------------

# --- 0. OS ENVIRONMENT & ICON SETUP ---
# CRITICAL: This string must match the 'Name' in your .desktop file exactly.
os.environ['SDL_VIDEO_X11_WMCLASS'] = "FermVault Lite"

from kivy.config import Config

# Calculate path to icon immediately
current_dir = os.path.dirname(os.path.abspath(__file__))
icon_path = os.path.join(current_dir, 'assets', 'fermenter.png')

# Set the icon globally for the window
Config.set('kivy', 'window_icon', icon_path)

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
    """
    Executes emergency hardware cleanup.
    Prioritizes GPIO safety over logging or graceful state saving.
    """
    # 1. Hardware Cleanup (Priority #1)
    try:
        app = App.get_running_app()
        if app and hasattr(app, 'relay_control') and app.relay_control:
            app.relay_control.cleanup_gpio()
            # Try to log, but don't let it crash the cleanup
            try: print("[System] GPIO cleaned up via App reference.")
            except: pass
            return
    except Exception:
        pass # App reference failed, try fallback

    # 2. Fallback Cleanup (New Instance)
    try:
        # Check if classes were successfully imported before trying to use them
        if 'RelayControl' in globals() and 'SettingsManager' in globals():
            if RelayControl and SettingsManager:
                sm = SettingsManager() 
                # Force 'Configured' to skip wizard during cleanup
                sm.set("relay_logic_configured", True) 
                rc = RelayControl(sm, RELAY_PINS)
                rc.cleanup_gpio()
                try: print("[System] GPIO cleaned up via Fresh Instance.")
                except: pass
    except Exception:
        pass # Fallback failed, nothing else we can do

def handle_signal(signum, frame):
    """
    Handles external kill signals (SIGHUP/SIGTERM).
    Forces immediate hardware cleanup and process exit.
    """
    signal_name = "SIGHUP" if signum == getattr(signal, 'SIGHUP', 1) else "SIGTERM"
    
    # 1. Try to log receipt (might fail if terminal closed)
    try:
        print(f"\n[System] Caught Signal {signal_name} ({signum}). Shutting down safely...")
    except: 
        pass

    # 2. Execute Cleanup
    failsafe_cleanup()
    
    # 3. Force Exit (Prevents Kivy/Python from hanging)
    os._exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)
if hasattr(signal, 'SIGHUP'):
    signal.signal(signal.SIGHUP, handle_signal)

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
class DirtyPopup(Popup): pass
class PIDWarningPopup(Popup): pass  # <--- NEW

# --- 6. MAIN APP CLASS ---
class FermVaultApp(App):
    # --- UI Properties ---
    beer_actual = StringProperty("--.-")
    
    # --- Aux Relay Property ---
    aux_mode_display = StringProperty("MONITORING")
    
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
    control_mode_display = StringProperty("AMBIENT")
    monitoring_state = StringProperty("OFF")
    log_text = StringProperty("[System] UI Initialized.\n")
    warning_message = StringProperty("")
    
    # --- Functional Display Colors (Target/Range) ---
    beer_target_color = ListProperty([0.7, 0.7, 0.7, 1])
    ambient_target_color = ListProperty([0.7, 0.7, 0.7, 1])
    range_text_color = ListProperty([0.7, 0.7, 0.7, 1])
    
    # --- Status Bar Properties ---
    warning_bg_color = ListProperty([0.2, 0.8, 0.2, 1]) 
    current_sensor_error = StringProperty("")
    
    # --- Settings Properties (SOURCE OF TRUTH NAMES) ---
    available_sensors = ListProperty(["unassigned"])
    
    # --- UPDATE & MAINTENANCE PROPERTIES ---
    update_log_text = StringProperty("Click CHECK to check for app updates.\n")
    is_update_available = BooleanProperty(False)
    is_install_successful = BooleanProperty(False)
    
    # --- GRAVITY DISPLAY PROPERTIES ---
    # Added explicit FG Color property for the "Stable" green highlight
    fg_text_color = ListProperty([1, 1, 1, 1]) # Default White
    
    # These combine Value + Date for the UI Button text
    og_full_text = StringProperty("OG: -.---\n\n--:--:--")
    sg_full_text = StringProperty("SG: -.---\n\n--:--:--")
    fg_full_text = StringProperty("FG: -.---\n\n--")
    
    # --- API PROPERTIES ---
    api_service_list = ListProperty(["OFF"])
    brew_session_list = ListProperty([])
    current_api_service = StringProperty("OFF")
    current_brew_session = StringProperty("Select Recipe...")

    # --- API SETTINGS PROPERTIES (Mapped to SettingsManager) ---
    api_key = StringProperty("")
    # NOTE: Frequency stored as seconds in backend, displayed as minutes in UI
    api_call_frequency_m = StringProperty("20.0") 
    fg_check_frequency_h = StringProperty("24.0")
    tolerance = StringProperty("0.005")
    window_size = StringProperty("500.0")
    max_outliers = StringProperty("4.0")
    
    # SOURCE OF TRUTH: settings_manager.py -> system_settings
    ds18b20_beer_sensor = StringProperty("unassigned")
    ds18b20_ambient_sensor = StringProperty("unassigned")
    relay_active_high = BooleanProperty(False)
    pid_logging_enabled = BooleanProperty(False) # Renamed from log_csv_enabled
    system_logging_enabled = BooleanProperty(False) # <--- NEW
    
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
    
    # --- NEW: NOTIFICATION / ALERTS PROPERTIES ---
    notif_frequency_hours = StringProperty("0.0")
    smtp_recipient = StringProperty("")
    smtp_sender = StringProperty("")
    smtp_password = StringProperty("")
    smtp_server = StringProperty("")
    smtp_port = StringProperty("587")
    
    conditional_enabled = BooleanProperty(False)
    cond_amb_min = StringProperty("32.0")
    cond_amb_max = StringProperty("85.0")
    cond_beer_min = StringProperty("32.0")
    cond_beer_max = StringProperty("75.0")
    # ---------------------------------------------
    
    # --- NEW: Temperature Units Property ---
    temp_units = StringProperty("F")
    
    # Keys that represent Absolute Temperatures (Convert: (F-32)*5/9)
    TEMP_KEYS_ABS = [
        "ambient_hold_f", "beer_hold_f", "ramp_up_hold_f", "fast_crash_hold_f",
        "cond_amb_min", "cond_amb_max", "cond_beer_min", "cond_beer_max"
    ]
    
    # Keys that represent Temperature Deltas/Ranges (Convert: F * 5/9)
    TEMP_KEYS_DELTA = [
        "ambient_mode_deadband_f", "pid_envelope_f", "crash_mode_envelope_f",
        "ramp_pre_ramp_tolerance_f", "ramp_thermostatic_deadband_f", "ramp_pid_landing_zone_f"
    ]
    
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
    
    def dismiss_splash(self, dt=None):
        """
        Kills the splash screen after the UI is fully rendered.
        """
        if hasattr(self, 'splash_queue') and self.splash_queue:
            self.splash_queue.put("STOP")

    @mainthread
    def log_system_message(self, message):
        # 1. UI Update
        # MODIFIED: Added date to timestamp [%Y-%m-%d %H:%M:%S]
        timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        self.log_text += f"{timestamp} {message}\n"
        if len(self.log_text) > 5000: self.log_text = self.log_text[-4000:]
        
        # 2. File Write (If Enabled)
        if self.system_logging_enabled and hasattr(self, 'settings_manager'):
            try:
                # Use same data_dir as SettingsManager
                data_dir = self.settings_manager.data_dir
                log_path = os.path.join(data_dir, "system_log.csv")
                
                # Full Date+Time for CSV (Verified: Already includes Date)
                csv_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                file_exists = os.path.isfile(log_path)
                
                with open(log_path, 'a', newline='', encoding='utf-8') as f:
                    # Simple manual CSV write to avoid overhead
                    if not file_exists:
                        f.write("Timestamp,Action\n")
                    
                    # Escape quotes in message just in case
                    clean_msg = message.replace('"', '""')
                    f.write(f'"{csv_timestamp}","{clean_msg}"\n')
                    
            except Exception as e:
                print(f"Error writing to system log: {e}")

    def build(self):
        self.title = "FermVault Lite"
        self.sm = ScreenManager()
        self.dashboard_screen = DashboardScreen(name='dashboard')
        self.log_screen = LogScreen(name='log')
        self.settings_screen = SettingsScreen(name='settings')
        # self.info_screen = InfoScreen(name='info')
        
        self.sm.add_widget(self.dashboard_screen)
        self.sm.add_widget(self.log_screen)
        self.sm.add_widget(self.settings_screen)
        # self.sm.add_widget(self.info_screen)
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
            
            # Auto-enable Relay Hardware if not configured
            if not self.settings_manager.get("relay_logic_configured"):
                self.log_system_message("Setup: Auto-enabling Relay Hardware (Active Low).")
                self.settings_manager.set("relay_logic_configured", True)
                self.settings_manager.set("relay_active_high", False) 
            
            # 2. Initialize Components
            
            # Explicitly pass the current directory to APIManager
            app_dir = os.path.dirname(os.path.abspath(__file__))
            self.api_manager = APIManager(self.settings_manager, scan_directory=app_dir)
            
            self.relay_control = RelayControl(self.settings_manager, RELAY_PINS)
            self.temp_controller = TemperatureController(self.settings_manager, self.relay_control)
            
            # 3. Variable Wrappers
            self.monitoring_var = KivyVarWrapper(lambda v: setattr(self, 'monitoring_state', v))
            self.control_mode_var = KivyVarWrapper(self._sync_control_mode_from_backend)
            
            # Shims
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
            
            # Populate API list from the manager
            self.api_service_list = self.api_manager.get_service_list()

            self.notification_manager.start_scheduler()
            
            self._refresh_all_settings_from_manager()
            Clock.schedule_interval(self.tick, 1.0)
            
            self.log_system_message("Backend initialized successfully.")
            
            # 5. Startup Logic
            self.settings_manager.set("monitoring_state", "OFF")
            self.monitoring_state = "OFF"
            
            # --- FIX: Explicitly Reset FG Data on Startup ---
            self.settings_manager.set("fg_value_var", "-.---")
            self.settings_manager.set("fg_status_var", "")
            # ------------------------------------------------
            
            self.log_system_message("System Started. Monitoring is OFF (Safe Standby).")
            self.start_standby_loop()
            
            # --- NEW: Dismiss Splash Screen ---
            Clock.schedule_once(self.dismiss_splash, 0.5)
            # ----------------------------------

        except Exception as e:
            self.log_system_message(f"CRITICAL BACKEND ERROR: {e}")
            import traceback
            traceback.print_exc()
            # Ensure splash dies even on error so we can see the app
            self.dismiss_splash()

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

    def set_temp_units(self, unit_str):
        """
        Sets the global temperature unit (F/C) and triggers a full refresh.
        This is an immediate action to ensure display consistency.
        """
        if unit_str not in ["F", "C"]: return
        
        self.log_system_message(f"Changing temperature units to {unit_str}...")
        
        # 1. Update Backend Immediately
        if hasattr(self, 'settings_manager'):
            self.settings_manager.set("temp_units", unit_str)
            
        # 2. Update UI Property
        self.temp_units = unit_str
        
        # 3. Force Refresh of all displayed values (triggers conversion)
        self._refresh_all_settings_from_manager()
        
        # 4. Force Logic Update (Optional, just to be safe)
        if hasattr(self, 'temp_controller') and self.temp_controller:
             self.temp_controller.update_control_logic_and_ui_data()

    def _refresh_all_settings_from_manager(self):
        if not hasattr(self, 'settings_manager'): return
        self.staged_changes.clear()
        
        # --- UNIT CONVERSION HELPERS ---
        self.temp_units = self.settings_manager.get("temp_units", "F")
        is_c = (self.temp_units == "C")
        
        def to_ui_abs(val_f):
            """Convert Absolute Temp F -> UI (F or C)"""
            if not is_c: return float(val_f)
            return (float(val_f) - 32.0) * 5.0 / 9.0

        def to_ui_delta(val_f):
            """Convert Delta Temp F -> UI (F or C)"""
            if not is_c: return float(val_f)
            return float(val_f) * 5.0 / 9.0

        def s(key, default, fmt=".1f", conv_type=None):
            val = float(self.settings_manager.get(key, default))
            
            if conv_type == 'abs':
                val = to_ui_abs(val)
            elif conv_type == 'delta':
                val = to_ui_delta(val)
                
            return f"{val:{fmt}}"
            
        def s_str(key, default): return str(self.settings_manager.get(key, default))
        
        # --- NEW: Load Aux Relay Mode ---
        self.aux_mode_display = self.settings_manager.get("aux_relay_mode", "MONITORING")
        # --------------------------------
        
        # Targets (ABSOLUTE TEMPS)
        self.ambient_hold_f = s("ambient_hold_f", 68.0, conv_type='abs')
        self.beer_hold_f = s("beer_hold_f", 55.0, conv_type='abs')
        self.ramp_up_hold_f = s("ramp_up_hold_f", 68.0, conv_type='abs')
        self.fast_crash_hold_f = s("fast_crash_hold_f", 34.0, conv_type='abs')
        
        # Duration is time, no conversion
        self.ramp_up_duration_hours = s("ramp_up_duration_hours", 30.0)
        
        # System
        self.ds18b20_beer_sensor = self.settings_manager.get("ds18b20_beer_sensor", "unassigned")
        self.ds18b20_ambient_sensor = self.settings_manager.get("ds18b20_ambient_sensor", "unassigned")
        self.relay_active_high = self.settings_manager.get("relay_active_high", False)
        
        # LOGGING (Separated)
        self.pid_logging_enabled = self.settings_manager.get("pid_logging_enabled", False)
        self.system_logging_enabled = self.settings_manager.get("system_logging_enabled", False)
        
        # Compressor Protection (Source of Truth Keys)
        comp = self.settings_manager.get_all_compressor_protection_settings()
        self.cooling_dwell_time_s = f"{comp.get('cooling_dwell_time_s', 180) / 60.0:.1f}"
        self.max_cool_runtime_s = f"{comp.get('max_cool_runtime_s', 7200) / 60.0:.1f}"
        self.fail_safe_shutdown_time_s = f"{comp.get('fail_safe_shutdown_time_s', 3600) / 60.0:.1f}"

        # PID & Tuning (DELTAS vs RAW)
        self.pid_kp = s("pid_kp", 2.0) # Kp unit is 1/Temp, effectively a delta related term but usually kept raw or treated as delta. For now keeping raw as tuning is complex.
        self.pid_ki = s("pid_ki", 0.03, ".4f")
        self.pid_kd = s("pid_kd", 20.0)
        
        # Deadbands are DELTAS
        self.ambient_mode_deadband_f = s("ambient_deadband", 1.0, conv_type='delta') # Note: Key mapping 'ambient_deadband'
        self.pid_envelope_f = s("beer_pid_envelope_width", 1.0, conv_type='delta')   # Note: Key mapping
        self.crash_mode_envelope_f = s("crash_pid_envelope_width", 2.0, conv_type='delta')
        self.ramp_pre_ramp_tolerance_f = s("ramp_pre_ramp_tolerance", 0.2, conv_type='delta')
        self.ramp_thermostatic_deadband_f = s("ramp_thermo_deadband", 0.1, conv_type='delta')
        self.ramp_pid_landing_zone_f = s("ramp_pid_landing_zone", 0.5, conv_type='delta')
        
        # API Settings
        self.api_key = self.settings_manager.get("api_key", "")
        freq_s = self.settings_manager.get("api_call_frequency_s", 1200)
        self.api_call_frequency_m = f"{freq_s / 60.0:.1f}"
        self.fg_check_frequency_h = s("fg_check_frequency_h", 24.0)
        self.tolerance = f"{float(self.settings_manager.get('tolerance', 0.005)):.4f}"
        self.window_size = s("window_size", 500.0)
        self.max_outliers = s("max_outliers", 4.0)
        
        # --- NOTIFICATION / ALERTS REFRESH ---
        smtp = self.settings_manager.get_all_smtp_settings()
        notif = self.settings_manager.settings.get('notification_settings', {})
        
        self.notif_frequency_hours = s("frequency_hours", 0.0)
        self.smtp_recipient = smtp.get("email_recipient", "")
        self.smtp_sender = smtp.get("server_email", "")
        self.smtp_password = smtp.get("server_password", "")
        self.smtp_server = smtp.get("smtp_server", "")
        self.smtp_port = str(smtp.get("smtp_port", 587))
        
        self.conditional_enabled = notif.get("conditional_enabled", False)
        
        # Conditional Limits are ABSOLUTE
        self.cond_amb_min = s("conditional_amb_min", 32.0, conv_type='abs')
        self.cond_amb_max = s("conditional_amb_max", 85.0, conv_type='abs')
        self.cond_beer_min = s("conditional_beer_min", 32.0, conv_type='abs')
        self.cond_beer_max = s("conditional_beer_max", 75.0, conv_type='abs')
        # -------------------------------------

        # Sync Dashboard State
        self.current_api_service = self.settings_manager.get("active_api_service", "OFF")
        self.current_brew_session = self.settings_manager.get("brew_session_title", "Select Recipe...")
        
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

    def toggle_setting_immediate(self, key, value):
        """
        Updates a setting immediately to the backend and UI.
        Bypasses 'staged_changes' and 'dirty' flags.
        Used for logging toggles to avoid excessive 'Unsaved Changes' warnings.
        """
        # 1. Update Backend (Source of Truth) & Save to Disk immediately
        if hasattr(self, 'settings_manager'):
             self.settings_manager.set(key, value)
        
        # 2. Update UI Property
        if hasattr(self, key):
             setattr(self, key, value)
             
        # 3. Log it
        # self.log_system_message(f"Setting '{key}' saved immediately.")
    
    @mainthread
    def push_data_update(self, **kwargs):
        def fmt(val):
            try: return f"{float(val):.1f}"
            except (ValueError, TypeError): return "--.-"
            
        # --- UNIT CONVERSION LOGIC ---
        is_c = (self.temp_units == "C")
        
        def convert_val(val):
            if val is None: return None
            try:
                v = float(val)
                if is_c: return (v - 32.0) * 5.0 / 9.0
                return v
            except: return None

        # 1. Update Text Values
        b_act = convert_val(kwargs.get('beer_temp'))
        b_tgt = convert_val(kwargs.get('beer_setpoint'))
        a_act = convert_val(kwargs.get('amb_temp'))
        a_tgt = convert_val(kwargs.get('amb_target'))
        
        self.beer_actual = fmt(b_act) if b_act is not None else "--.-"
        self.beer_target = fmt(b_tgt) if b_tgt is not None else "--.-"
        self.ambient_actual = fmt(a_act) if a_act is not None else "--.-"
        self.ambient_target = fmt(a_tgt) if a_tgt is not None else "--.-"
        
        amin = convert_val(kwargs.get('amb_min'))
        amax = convert_val(kwargs.get('amb_max'))
        
        if amin is not None and amax is not None:
            self.ambient_range = f"{fmt(amin)} - {fmt(amax)}"
        else:
            self.ambient_range = "--.- - --.-"
            
        h_state = str(kwargs.get('heat_state', 'OFF'))
        c_state = str(kwargs.get('cool_state', 'OFF'))
        
        # --- COLOR LOGIC UPDATE ---
        self.heater_color = [0.8, 0, 0, 1] if "HEATING" in h_state else [0.2, 0.2, 0.2, 1]
        self.cooler_color = self.col_theme_blue if "COOLING" in c_state else [0.2, 0.2, 0.2, 1]

        mode_internal = kwargs.get('current_mode', 'Ambient Hold')
        self._sync_control_mode_from_backend(mode_internal)
        
        self.current_sensor_error = kwargs.get('sensor_error_message', "")
        self._update_warning_status()

        # 2. Dynamic Hero Colors (ACTUALS)
        COL_WHITE = [1, 1, 1, 1]
        COL_LGRAY = [0.7, 0.7, 0.7, 1]
        COL_GREEN = [0, 0.8, 0, 1]
        COL_RED   = [0.8, 0, 0, 1]
        COL_BLUE  = self.col_theme_blue
        
        # New Colors for Functional Display
        COL_AMBER = [1, 0.6, 0, 1]

        if self.monitoring_state == "OFF":
            self.beer_actual_color = COL_WHITE
            self.ambient_actual_color = COL_WHITE
            
            # FUNCTIONAL DISPLAY: ALL GRAY WHEN OFF
            self.beer_target_color = COL_LGRAY
            self.ambient_target_color = COL_LGRAY
            self.range_text_color = COL_LGRAY
        else:
            # --- 2a. Actuals Logic (Using Converted Values) ---
            # Tolerance is Delta: 0.3F is roughly 0.17C. 
            # We can use a simpler approach: Calculate delta in the current unit.
            # If C: 0.2 deg C. If F: 0.3 deg F.
            tolerance = 0.2 if is_c else 0.3
            
            # Ambient Logic
            if a_act is not None and amax is not None and amin is not None:
                if a_act > amax: self.ambient_actual_color = COL_RED
                elif a_act < amin: self.ambient_actual_color = COL_BLUE
                else: self.ambient_actual_color = COL_GREEN
            else:
                self.ambient_actual_color = COL_WHITE

            # Beer Logic
            if "AMBIENT" in self.control_mode_display:
                self.beer_actual_color = COL_LGRAY
            else:
                if b_act is not None and b_tgt is not None:
                    delta = b_act - b_tgt
                    if delta >= tolerance: self.beer_actual_color = COL_RED
                    elif delta <= -tolerance: self.beer_actual_color = COL_BLUE
                    else: self.beer_actual_color = COL_GREEN
                else:
                    self.beer_actual_color = COL_WHITE

            # --- 2b. Functional Display Logic (Targets/Range) ---
            self.range_text_color = COL_AMBER
            
            if "AMBIENT" in self.control_mode_display:
                self.ambient_target_color = COL_AMBER
                self.beer_target_color = COL_LGRAY
            else:
                # BEER, RAMP, CRASH modes
                self.ambient_target_color = COL_LGRAY
                self.beer_target_color = COL_AMBER

        # --- 3. UPDATE GRAVITY WIDGETS (Unchanged) ---
        if hasattr(self, 'settings_manager'):
            og_val = self.settings_manager.get("og_display_var", "-.---")
            og_time = self.settings_manager.get("og_timestamp_var", "--:--:--")
            if og_time and isinstance(og_time, str) and " " in og_time: og_time = og_time.replace(" ", "\n")
            self.og_full_text = f"OG: {og_val}\n\n{og_time}"

            sg_val = self.settings_manager.get("sg_display_var", "-.---")
            sg_time = self.settings_manager.get("sg_timestamp_var", "--:--:--")
            if sg_time and isinstance(sg_time, str) and " " in sg_time: sg_time = sg_time.replace(" ", "\n")
            self.sg_full_text = f"SG: {sg_val}\n\n{sg_time}"

            fg_val = self.settings_manager.get("fg_value_var", "-.---")
            fg_msg = self.settings_manager.get("fg_status_var", "--")
            if not fg_msg: fg_msg = "--"
            has_valid_value = (fg_val != "-.---")
            if not has_valid_value and fg_msg not in ["--", ""]: self.fg_full_text = f"FG: {fg_msg}"
            else: self.fg_full_text = f"FG: {fg_val}\n\n{fg_msg}"
            
            if fg_msg == "Stable": self.fg_text_color = [0.2, 0.8, 0.2, 1] 
            else: self.fg_text_color = [1, 1, 1, 1]
            
    # --- OTHER METHODS ---
    def set_aux_mode(self, mode_value):
        """Called when Aux Relay Spinner is changed on Dashboard."""
        self.aux_mode_display = mode_value # Keep UI in sync
        if hasattr(self, 'settings_manager'):
            self.settings_manager.set("aux_relay_mode", mode_value)
            self.log_system_message(f"Aux Relay Mode set to: {mode_value}")
    
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
        # DIRECT KEY USAGE: The key IS the property name.
        
        # 1. Update the UI property directly using the key (for immediate visual feedback)
        #    This stores the 'new_value' (which might be in C) in the UI property.
        if hasattr(self, key):
            if isinstance(new_value, bool):
                 setattr(self, key, new_value)
            elif isinstance(new_value, (float, int)):
                high_precision_keys = ["pid_ki", "tolerance"]
                fmt = ".4f" if key in high_precision_keys else ".1f"
                setattr(self, key, f"{float(new_value):{fmt}}")
            else:
                setattr(self, key, str(new_value))

        # 2. Determine value to Stage for Backend (Convert C -> F if needed)
        value_to_stage = new_value
        
        if self.temp_units == "C" and isinstance(new_value, (int, float)):
            if key in self.TEMP_KEYS_ABS:
                # C -> F Absolute: (C * 9/5) + 32
                value_to_stage = (float(new_value) * 9.0 / 5.0) + 32.0
            elif key in self.TEMP_KEYS_DELTA:
                # C -> F Delta: C * 9/5
                value_to_stage = float(new_value) * 9.0 / 5.0

        # 3. Stage the change
        self.staged_changes[key] = value_to_stage
        self.is_settings_dirty = True

    def stage_text_input(self, key, text_value):
        # API key is string, allow it
        str_keys = ["api_key", "smtp_recipient", "smtp_sender", "smtp_password", "smtp_server", "smtp_port"]
        if key in str_keys:
             self.stage_setting_change(key, text_value)
             return
             
        try:
            val = float(text_value)
            self.stage_setting_change(key, val)
        except ValueError: pass

    def adjust_target(self, key, delta, min_val=None, max_val=None):
        try:
            # Get current value (check staged first, then backend)
            current = float(self.staged_changes.get(key, self.settings_manager.get(key, 0.0)))
            new_val = current + delta
            
            # Enforce Limits if provided
            if min_val is not None:
                new_val = max(float(min_val), new_val)
            if max_val is not None:
                new_val = min(float(max_val), new_val)
                
            self.stage_setting_change(key, new_val)
        except Exception as e:
            print(f"Error adjusting target: {e}")

    def save_target_from_slider(self, key, value):
        try:
            new_val = float(value)
            if hasattr(self, key):
                current_ui = float(getattr(self, key))
                # For tolerance, we need finer sensitivity
                threshold = 0.00001 if key == "tolerance" else 0.01
                if abs(new_val - current_ui) > threshold:
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

    # --- 7. REFACTORED SETTINGS MANAGEMENT (ISOLATED TABS) ---
    
    def _commit_staged_changes(self):
        """
        Internal helper: Commits all staged changes to the SettingsManager and Backend.
        Does NOT handle navigation.
        """
        if not hasattr(self, 'settings_manager'): return
        self.log_system_message("Saving Settings...")
        
        # Keys requiring Minute->Second conversion (Source of Truth)
        cooling_keys = ["cooling_dwell_time_s", "max_cool_runtime_s", "fail_safe_shutdown_time_s"]
        
        # Keys requiring special handling
        api_keys = ["api_key", "api_call_frequency_m", "fg_check_frequency_h", "tolerance", "window_size", "max_outliers"]
        
        # --- NEW: Notification Keys ---
        smtp_keys = ["smtp_recipient", "smtp_sender", "smtp_password", "smtp_server", "smtp_port"]
        cond_keys = ["conditional_enabled", "cond_amb_min", "cond_amb_max", "cond_beer_min", "cond_beer_max"]
        # ------------------------------
        
        cooling_update = {}
        api_update = {}
        
        # Dictionaries for complex updates
        smtp_update = {}
        cond_update = {}
        notif_update_freq = None # Stores frequency change
        
        # Capture old frequency for rescheduling
        old_freq = int(self.settings_manager.get("frequency_hours", 0))

        for key, val in self.staged_changes.items():
            if key in cooling_keys:
                cooling_update[key] = float(val) * 60.0
            
            elif key == "api_call_frequency_m":
                api_update["api_call_frequency_s"] = int(float(val) * 60)
            
            elif key in ["window_size", "max_outliers", "fg_check_frequency_h"]:
                 api_update[key] = int(float(val))
            
            elif key == "tolerance":
                 api_update[key] = float(val)
                 
            elif key == "api_key":
                 api_update[key] = str(val)

            # --- NOTIFICATIONS HANDLING ---
            elif key == "notif_frequency_hours":
                new_freq = int(float(val))
                self.settings_manager.set("frequency_hours", new_freq)
                notif_update_freq = new_freq
            
            elif key in smtp_keys:
                # Map Property Key -> Backend Key
                if key == "smtp_recipient": smtp_update["email_recipient"] = str(val)
                elif key == "smtp_sender": smtp_update["server_email"] = str(val)
                elif key == "smtp_password": smtp_update["server_password"] = str(val)
                elif key == "smtp_server": smtp_update["smtp_server"] = str(val)
                elif key == "smtp_port": smtp_update["smtp_port"] = int(val)
            
            elif key in cond_keys:
                # Map properties to backend keys
                if key == "conditional_enabled": cond_update["conditional_enabled"] = bool(val)
                elif key == "cond_amb_min": cond_update["conditional_amb_min"] = float(val)
                elif key == "cond_amb_max": cond_update["conditional_amb_max"] = float(val)
                elif key == "cond_beer_min": cond_update["conditional_beer_min"] = float(val)
                elif key == "cond_beer_max": cond_update["conditional_beer_max"] = float(val)

            else:
                self.settings_manager.set(key, val)
                
        if cooling_update:
            current = self.settings_manager.get_all_compressor_protection_settings()
            current.update(cooling_update)
            self.settings_manager.save_compressor_protection_settings(current)

        if api_update:
             current_api = self.settings_manager.get_all_api_settings()
             current_api.update(api_update)
             self.settings_manager.save_api_settings(current_api)
        
        # --- SAVE SMTP ---
        if smtp_update:
            current_smtp = self.settings_manager.get_all_smtp_settings()
            current_smtp.update(smtp_update)
            # We must set this directly back to the main settings dict
            self.settings_manager.settings['smtp_settings'] = current_smtp
            self.settings_manager._save_all_settings() # Force save
            
        # --- SAVE CONDITIONAL ---
        if cond_update:
            current_notif = self.settings_manager.settings.get('notification_settings', {})
            current_notif.update(cond_update)
            self.settings_manager.settings['notification_settings'] = current_notif
            self.settings_manager._save_all_settings()

        # --- RESCHEDULE IF FREQ CHANGED ---
        if notif_update_freq is not None:
             if self.notification_manager:
                 self.notification_manager.force_reschedule(old_freq, notif_update_freq)

        if hasattr(self, 'temp_controller') and self.temp_controller and hasattr(self.temp_controller, 'pid'):
            self.temp_controller.pid.Kp = float(self.settings_manager.get("pid_kp", 2.0))
            self.temp_controller.pid.Ki = float(self.settings_manager.get("pid_ki", 0.03))
            self.temp_controller.pid.Kd = float(self.settings_manager.get("pid_kd", 20.0))

        if "relay_active_high" in self.staged_changes:
            self.settings_manager.set("relay_logic_configured", True)
            self.relay_control.update_relay_logic()
            
        self.staged_changes.clear()
        self.is_settings_dirty = False

    def save_and_continue(self):
        """
        Saves changes and exits to the Dashboard.
        Renamed from save_and_exit to reflect 'Continue' workflow from Popup.
        """
        self._commit_staged_changes()
        self.go_to_screen('dashboard', 'right')

    def save_current_tab(self, tab_name=None):
        """
        Saves changes. 
        For PID tab: Intercepts to show Safety Warning.
        For others: Saves and stays on the tab.
        """
        if tab_name == 'pid' and self.is_settings_dirty:
            # PID Safety Interception
            self.show_pid_warning()
        else:
            # Standard Behavior
            self._commit_staged_changes()

    def attempt_exit_settings(self, tab_name=None):
        """
        Called by CANCEL/EXIT footer button.
        Checks for dirty state before allowing exit.
        For PID tab: Intercepts to show Safety Warning if dirty.
        """
        if not self.is_settings_dirty:
            self.go_to_screen('dashboard', 'right')
            return

        if tab_name == 'pid':
            # PID Safety Interception
            self.show_pid_warning()
        else:
            # Standard Dirty Popup
            DirtyPopup().open()

    def show_pid_warning(self):
        """Triggers the specific PID Safety Popup."""
        PIDWarningPopup().open()

    def discard_changes(self):
        """
        Reverts changes and returns to Dashboard.
        Called by DISCARD on Popup.
        """
        self._refresh_all_settings_from_manager()
        self.go_to_screen('dashboard', 'right')

    def reset_targets_to_defaults(self, tab_name=None):
        """
        Resets settings to default values ONLY for the specified tab context.
        Fetches official defaults from SettingsManager (SSOT).
        """
        if not hasattr(self, 'settings_manager'): return
        
        self.log_system_message(f"Resetting defaults for: {tab_name}")
        
        if tab_name == 'targets':
            # SSOT: control_settings
            defs = self.settings_manager.get_defaults_for_category("control_settings")
            self.stage_setting_change("ambient_hold_f", defs.get("ambient_hold_f", 68.0))
            self.stage_setting_change("beer_hold_f", defs.get("beer_hold_f", 68.0))
            self.stage_setting_change("ramp_up_hold_f", defs.get("ramp_up_hold_f", 68.0))
            self.stage_setting_change("ramp_up_duration_hours", defs.get("ramp_up_duration_hours", 30.0))
            self.stage_setting_change("fast_crash_hold_f", defs.get("fast_crash_hold_f", 34.0))

        elif tab_name == 'system':
            # SSOT: compressor_protection_settings (Converted to Minutes for UI)
            comp_defs = self.settings_manager.get_defaults_for_category("compressor_protection_settings")
            self.stage_setting_change("cooling_dwell_time_s", comp_defs.get("cooling_dwell_time_s", 180) / 60.0)
            self.stage_setting_change("max_cool_runtime_s", comp_defs.get("max_cool_runtime_s", 7200) / 60.0)
            self.stage_setting_change("fail_safe_shutdown_time_s", comp_defs.get("fail_safe_shutdown_time_s", 3600) / 60.0)
            
            # SSOT: system_settings (Relay Logic)
            sys_defs = self.settings_manager.get_defaults_for_category("system_settings")
            # Default is False (Active Low)
            default_active_high = sys_defs.get("relay_active_high", False)
            self.stage_setting_change("relay_active_high", default_active_high)

        elif tab_name == 'api':
            # SSOT: api_settings
            api_defs = self.settings_manager.get_defaults_for_category("api_settings")
            
            # Convert Seconds -> Minutes
            freq_s = api_defs.get("api_call_frequency_s", 1200)
            self.stage_setting_change("api_call_frequency_m", freq_s / 60.0)
            
            self.stage_setting_change("fg_check_frequency_h", api_defs.get("fg_check_frequency_h", 24))
            self.stage_setting_change("tolerance", api_defs.get("tolerance", 0.0005))
            self.stage_setting_change("window_size", api_defs.get("window_size", 450))
            self.stage_setting_change("max_outliers", api_defs.get("max_outliers", 4))
            
        # --- NEW: ALERTS DEFAULTS ---
        # --- NEW: ALERTS DEFAULTS ---
        elif tab_name == 'alerts':
            notif_defs = self.settings_manager.get_defaults_for_category("notification_settings")
            
            # NOTE: We specifically DO NOT reset SMTP/Email settings here 
            # to preserve user data (Server, Port, User, Pass, Recipient).
            
            # 1. Reset Push Frequency
            self.stage_setting_change("notif_frequency_hours", notif_defs.get("frequency_hours", 0.0))
            
            # 2. Reset Conditional Logic & Thresholds
            self.stage_setting_change("conditional_enabled", notif_defs.get("conditional_enabled", False))
            self.stage_setting_change("cond_amb_min", notif_defs.get("conditional_amb_min", 32.0))
            self.stage_setting_change("cond_amb_max", notif_defs.get("conditional_amb_max", 85.0))
            self.stage_setting_change("cond_beer_min", notif_defs.get("conditional_beer_min", 32.0))
            self.stage_setting_change("cond_beer_max", notif_defs.get("conditional_beer_max", 75.0))
        # ----------------------------
        # ----------------------------

        elif tab_name == 'pid':
            # SSOT: system_settings (PID params are stored here)
            sys_defs = self.settings_manager.get_defaults_for_category("system_settings")
            
            self.stage_setting_change("pid_kp", sys_defs.get("pid_kp", 2.0))
            self.stage_setting_change("pid_ki", sys_defs.get("pid_ki", 0.03))
            self.stage_setting_change("pid_kd", sys_defs.get("pid_kd", 20.0))
            
            # Reset PID Logging default too
            self.stage_setting_change("pid_logging_enabled", sys_defs.get("pid_logging_enabled", False))
            
            self.stage_setting_change("ambient_mode_deadband_f", sys_defs.get("ambient_deadband", 1.0))
            self.stage_setting_change("pid_envelope_f", sys_defs.get("beer_pid_envelope_width", 1.0))
            self.stage_setting_change("crash_mode_envelope_f", sys_defs.get("crash_pid_envelope_width", 2.0))
            
            self.stage_setting_change("ramp_pre_ramp_tolerance_f", sys_defs.get("ramp_pre_ramp_tolerance", 0.2))
            self.stage_setting_change("ramp_thermostatic_deadband_f", sys_defs.get("ramp_thermo_deadband", 0.1))
            self.stage_setting_change("ramp_pid_landing_zone_f", sys_defs.get("ramp_pid_landing_zone", 0.5))

        else:
            self.log_system_message(f"No defaults logic defined for tab: {tab_name}")

    def set_control_mode(self, display_mode):
        if not hasattr(self, 'settings_manager'): return
        map_ui_to_internal = {
            "AMBIENT": "Ambient Hold", "BEER": "Beer Hold",
            "RAMP": "Ramp-Up", "CRASH": "Fast Crash"
        }
        internal_mode = map_ui_to_internal.get(display_mode, "Ambient Hold")
        self.settings_manager.set("control_mode", internal_mode)
        if internal_mode != "Ramp-Up":
            self.temp_controller.reset_ramp_state()

    def _sync_control_mode_from_backend(self, internal_mode):
        map_internal_to_ui = {
            "Ambient Hold": "AMBIENT", "Beer Hold": "BEER",
            "Ramp-Up": "RAMP", "Fast Crash": "CRASH", "OFF": "AMBIENT"
        }
        ui_value = map_internal_to_ui.get(internal_mode, "AMBIENT")
        if self.control_mode_display != ui_value:
            self.control_mode_display = ui_value

    def go_to_screen(self, screen_name, direction):
        self.sm.transition.direction = direction
        self.sm.current = screen_name

    def on_stop(self):
        """
        Called by Kivy when the window is closed (Controlled Shutdown).
        Mirrors 'shutdown_application' from the legacy Tkinter app.
        """
        print("[App] Controlled shutdown initiated (on_stop)...")
        
        try:
            # 1. Stop Notification Scheduler
            if self.notification_manager:
                self.notification_manager.stop_scheduler()

            # 2. Stop Monitoring Thread (Logic)
            if self.temp_controller:
                self.temp_controller.stop_monitoring()
            
            # 3. Stop Standby Thread
            self.stop_standby_loop()

            # 4. Hardware Safety (Relays OFF)
            if self.relay_control:
                self.relay_control.turn_off_all_relays()

            # 5. Flag as Controlled Shutdown
            if hasattr(self, 'settings_manager') and self.settings_manager:
                self.settings_manager.set_controlled_shutdown(True)
                
            print("[App] Application closed gracefully.")

        except Exception as e:
            print(f"[App] Error during controlled shutdown: {e}")
            # Fallback to hard cleanup if graceful steps fail
            failsafe_cleanup()

        # Final Safety: Ensure process dies (Kivy sometimes hangs on thread join)
        time.sleep(0.5)
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
        
        # --- PORTED FROM KETTLEBRAIN: MANUAL CLEANUP ---
        # We do NOT call self.stop() here because that triggers on_stop -> os._exit(0)
        # which kills the process immediately, preventing the restart.
        
        try:
            if hasattr(self, 'temp_controller') and self.temp_controller:
                self.temp_controller.stop_monitoring()
                
            if hasattr(self, 'relay_control') and self.relay_control:
                self.relay_control.cleanup_gpio()
        except Exception as e:
            print(f"[System] Restart cleanup warning: {e}")

        # --- PORTED FROM KETTLEBRAIN: ABSOLUTE PATH RESTART ---
        import sys
        import os
        
        # 1. Resolve Absolute Paths to ensure reliability
        python = sys.executable
        script = os.path.abspath(sys.argv[0])
        args = sys.argv[1:]
        
        # 2. Construct the Command
        cmd_args = [python, script] + args
        
        print(f"[System] Executing Restart: {python} {script} {args}")
        
        # 3. Replace Process (Nuclear Option)
        # This replaces the current process memory with the new one.
        os.execv(python, cmd_args)

    def select_api_service(self, service_name):
        """Called when API Service Spinner is changed."""
        self.current_api_service = service_name
        self.api_manager.set_active_service(service_name)
        
        if service_name == "OFF":
            self.brew_session_list = []
            # MODIFIED: Blank text as requested
            self.current_brew_session = ""
            
            # MODIFIED: Reset all Gravity Data to dashes (Backend + UI)
            if hasattr(self, 'settings_manager'):
                self.settings_manager.set("og_display_var", "-.---")
                self.settings_manager.set("og_timestamp_var", "--:--:--")
                self.settings_manager.set("sg_display_var", "-.---")
                self.settings_manager.set("sg_timestamp_var", "--:--:--")
                self.settings_manager.set("fg_value_var", "-.---")
                self.settings_manager.set("fg_status_var", "")

            # Update UI Properties immediately
            self.og_full_text = "OG: -.---\n\n--:--:--"
            self.sg_full_text = "SG: -.---\n\n--:--:--"
            self.fg_full_text = "FG: -.---\n\n--"
            self.fg_text_color = [1, 1, 1, 1]

        else:
            self.brew_session_list = ["Loading..."]
            self.current_brew_session = "Loading..."
            
            # Fetch sessions in background
            self.api_manager.fetch_sessions_threaded(
                on_success=self._update_session_list_success,
                on_error=self._update_session_list_error
            )

    def refresh_api_data(self):
        """
        Manually triggers an API data fetch for the current brew session.
        Ported from ui_manager_base.py
        """
        # 1. Get the current session ID
        current_id = self.settings_manager.get("current_brew_session_id")
        
        # 2. Trigger the fetch if the manager exists
        if self.notification_manager:
            print(f"[App] Manual API refresh triggered for session ID: {current_id}")
            self.notification_manager.fetch_api_data_now(current_id, is_scheduled=False)
    
    def run_fg_calculator(self):
        """
        Manually triggers the FG Calculator logic.
        Directly calls the notification manager, bypassing ui_manager_base.
        """
        if self.notification_manager:
            print("[App] Manual FG Calculation triggered.")
            self.notification_manager.run_fg_calc_and_update_ui()
    
    @mainthread
    def _update_session_list_success(self, titles):
        self.brew_session_list = titles
        
        # Restore previous selection if valid, else pick first
        saved_title = self.settings_manager.get("brew_session_title", "")
        if saved_title in titles:
            self.current_brew_session = saved_title
            
            # Ensure ID is set
            sid = self.api_manager.get_session_id_by_title(saved_title)
            self.settings_manager.set("current_brew_session_id", sid)
            
            # --- FIX: Force immediate data fetch for the RESTORED session ---
            if self.notification_manager and sid:
                print(f"[App] Restoring Session '{saved_title}' (ID: {sid}). Fetching data...")
                threading.Thread(target=self.notification_manager.fetch_api_data_now, args=(sid,), daemon=True).start()
            # ---------------------------------------------------------------
            
        else:
            self.current_brew_session = titles[0]
            self.select_brew_session(titles[0]) # Trigger ID save AND fetch

    @mainthread
    def _update_session_list_error(self, error_msg):
        self.brew_session_list = ["Error"]
        self.current_brew_session = "Error"
        self.log_system_message(f"API Fetch Error: {error_msg}")

    def select_brew_session(self, title):
        """Called when Brew Session Spinner is changed."""
        if title in ["Loading...", "Error", "Select Recipe..."]: return
        
        self.current_brew_session = title
        self.settings_manager.set("brew_session_title", title)
        
        # Get ID from API Manager map
        sid = self.api_manager.get_session_id_by_title(title)
        if sid:
            self.settings_manager.set("current_brew_session_id", sid)
            self.log_system_message(f"Session selected: {title} ({sid})")
            
            # Trigger immediate data update via Notification Manager (which handles API calls)
            if self.notification_manager:
                # Run in thread to avoid blocking
                threading.Thread(target=self.notification_manager.fetch_api_data_now, args=(sid,), daemon=True).start()


def run_splash_screen(queue):
    """
    Runs a standalone Tkinter loading dialog in a separate process.
    This appears immediately, independent of Kivy's loading time.
    """
    import tkinter as tk
    
    try:
        root = tk.Tk()
        # Remove window decorations (frameless)
        root.overrideredirect(True)
        # Keep on top of the launching Kivy window
        root.attributes('-topmost', True)
        
        # Calculate center position
        width = 320
        height = 80
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        root.geometry(f'{width}x{height}+{x}+{y}')
        root.configure(bg='#222222')
        
        # Add a simple styled frame
        frame = tk.Frame(root, bg='#222222', highlightbackground='#33CCFF', highlightthickness=2)
        frame.pack(fill='both', expand=True)
        
        # Add Text
        # Using Blue/Cyan (#33CCFF) to match FermVault Theme
        lbl = tk.Label(frame, text="FermVault Lite App Loading...", font=("Arial", 16, "bold"), fg="#33CCFF", bg="#222222")
        lbl.pack(expand=True)
        
        # Force a draw immediately
        root.update()
        
        # Check for kill signal every 100ms
        def check_kill():
            if not queue.empty():
                root.destroy()
            else:
                root.after(100, check_kill)
                
        root.after(100, check_kill)
        root.mainloop()
    except Exception as e:
        print(f"Splash screen error: {e}")

if __name__ == '__main__':
    # 1. Start the Splash Screen immediately in a separate process
    # We use multiprocessing so it doesn't block the main thread imports
    splash_queue = multiprocessing.Queue()
    splash_process = multiprocessing.Process(target=run_splash_screen, args=(splash_queue,))
    splash_process.start()
    
    try:
        # 2. Initialize and Run the App
        app = FermVaultApp()
        # Pass the queue so the App can kill the splash when ready
        app.splash_queue = splash_queue 
        app.run()
        
    except KeyboardInterrupt:
        failsafe_cleanup()
        print("\nFermVault App interrupted by user.")
        
    finally:
        # Ensure splash process is definitely dead on exit
        if splash_process.is_alive():
            splash_process.terminate()
