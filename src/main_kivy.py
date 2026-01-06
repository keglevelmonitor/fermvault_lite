# fermvault app
# main_kivy.py
import os
import sys
import threading
import signal
import atexit
import time
from datetime import datetime

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
RELAY_PINS = {'Heat': 17, 'Cool': 22, 'Fan': 27}

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
    # UI Properties
    beer_actual = StringProperty("--.-")
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
    
    # Settings Properties
    available_sensors = ListProperty(["unassigned"])
    beer_sensor_setting = StringProperty("unassigned")
    ambient_sensor_setting = StringProperty("unassigned")
    relay_active_high = BooleanProperty(False)
    log_csv_enabled = BooleanProperty(False)
    
    # Target Properties
    setting_ambient_hold = StringProperty("0.0")
    setting_beer_hold = StringProperty("0.0")
    setting_ramp_hold = StringProperty("0.0")
    setting_crash_hold = StringProperty("0.0")
    setting_ramp_hours = StringProperty("0.0")
    
    # SYSTEM: Cooling Operations (Minutes)
    setting_cool_dwell = StringProperty("0.0")
    setting_cool_max_run = StringProperty("0.0")
    setting_cool_failsafe = StringProperty("0.0")

    # PID & TUNING Properties
    setting_pid_kp = StringProperty("0.0")
    setting_pid_ki = StringProperty("0.0")
    setting_pid_kd = StringProperty("0.0")
    setting_amb_deadband = StringProperty("0.0")
    setting_pid_envelope = StringProperty("0.0")
    setting_crash_envelope = StringProperty("0.0")
    setting_ramp_tol = StringProperty("0.0")
    setting_ramp_deadband = StringProperty("0.0")
    setting_ramp_landing = StringProperty("0.0")
    
    # Dirty Tracking & Staging
    is_settings_dirty = BooleanProperty(False)
    staged_changes = {} 
    
    # Key Mapping: Settings Key -> UI Property Name
    property_map = {
        # Targets
        "ambient_hold_f": "setting_ambient_hold",
        "beer_hold_f": "setting_beer_hold",
        "ramp_up_hold_f": "setting_ramp_hold",
        "fast_crash_hold_f": "setting_crash_hold",
        "ramp_up_duration_hours": "setting_ramp_hours",
        
        # System
        "ds18b20_beer_sensor": "beer_sensor_setting",
        "ds18b20_ambient_sensor": "ambient_sensor_setting",
        "relay_active_high": "relay_active_high",
        "pid_logging_enabled": "log_csv_enabled",
        
        # Cooling (Seconds in Backend -> Minutes in UI)
        "cooling_dwell_time_s": "setting_cool_dwell",
        "cooling_max_run_time_s": "setting_cool_max_run",
        "min_off_time_s": "setting_cool_failsafe",
        
        # PID & Tuning
        "pid_kp": "setting_pid_kp",
        "pid_ki": "setting_pid_ki",
        "pid_kd": "setting_pid_kd",
        "ambient_mode_deadband_f": "setting_amb_deadband",
        "pid_envelope_f": "setting_pid_envelope",
        "crash_mode_envelope_f": "setting_crash_envelope",
        "ramp_pre_ramp_tolerance_f": "setting_ramp_tol",
        "ramp_thermostatic_deadband_f": "setting_ramp_deadband",
        "ramp_pid_landing_zone_f": "setting_ramp_landing"
    }

    # --- LOGGING METHOD (Moved to top to prevent AttributeErrors) ---
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
            self.settings_manager = SettingsManager()
            self.relay_control = RelayControl(self.settings_manager, RELAY_PINS)
            self.api_manager = APIManager(self.settings_manager)
            self.temp_controller = TemperatureController(self.settings_manager, self.relay_control)
            
            self.monitoring_var = KivyVarWrapper(lambda v: setattr(self, 'monitoring_state', v))
            self.control_mode_var = KivyVarWrapper(self._sync_control_mode_from_backend)
            
            self.fg_status_var = KivyVarWrapper(lambda v: None)
            self.fg_value_var = KivyVarWrapper(lambda v: None)
            self.og_display_var = KivyVarWrapper(lambda v: None)
            self.sg_display_var = KivyVarWrapper(lambda v: None)
            self.og_timestamp_var = KivyVarWrapper(lambda v: None)
            self.sg_timestamp_var = KivyVarWrapper(lambda v: None)
            
            self.ui_adapter = KivyUIManagerAdapter(self)
            self.fg_calculator_instance = FGCalculator(self.settings_manager, self.api_manager)
            self.notification_manager = NotificationManager(self.settings_manager, self.ui_adapter)
            self.temp_controller.notification_manager = self.notification_manager
            
            self.notification_manager.start_scheduler()
            
            # Load initial settings into UI properties
            self._refresh_all_settings_from_manager()
            
            Clock.schedule_interval(self.tick, 1.0)
            
            if self.settings_manager.get("monitoring_state") == "ON":
                self.temp_controller.start_monitoring()
                self.monitoring_state = "ON"
            else:
                self.log_system_message("Monitoring is OFF. Passive Mode Active.")
                threading.Thread(target=self.temp_controller.update_control_logic_and_ui_data, daemon=True).start()
            
            self.log_system_message("Backend initialized successfully.")

        except Exception as e:
            self.log_system_message(f"CRITICAL BACKEND ERROR: {e}")
            import traceback
            traceback.print_exc()

    def _refresh_all_settings_from_manager(self):
        if not hasattr(self, 'settings_manager'): return
        
        self.staged_changes.clear()
        
        def s(key, default, fmt=".1f"): return f"{float(self.settings_manager.get(key, default)):{fmt}}"
        
        # Targets
        self.setting_ambient_hold = s("ambient_hold_f", 37.0)
        self.setting_beer_hold = s("beer_hold_f", 55.0)
        self.setting_ramp_hold = s("ramp_up_hold_f", 68.0)
        self.setting_crash_hold = s("fast_crash_hold_f", 34.0)
        self.setting_ramp_hours = s("ramp_up_duration_hours", 30.0)
        
        # System
        self.beer_sensor_setting = self.settings_manager.get("ds18b20_beer_sensor", "unassigned")
        self.ambient_sensor_setting = self.settings_manager.get("ds18b20_ambient_sensor", "unassigned")
        self.relay_active_high = self.settings_manager.get("relay_active_high", False)
        self.log_csv_enabled = self.settings_manager.get("pid_logging_enabled", False)
        
        # Cooling (Convert Seconds -> Minutes)
        comp_settings = self.settings_manager.get_all_compressor_protection_settings()
        self.setting_cool_dwell = f"{comp_settings.get('cooling_dwell_time_s', 180) / 60.0:.1f}"
        self.setting_cool_max_run = f"{comp_settings.get('cooling_max_run_time_s', 7200) / 60.0:.1f}"
        self.setting_cool_failsafe = f"{comp_settings.get('min_off_time_s', 3600) / 60.0:.1f}"

        # PID & Tuning
        self.setting_pid_kp = s("pid_kp", 2.0)
        self.setting_pid_ki = s("pid_ki", 0.03, ".4f")
        self.setting_pid_kd = s("pid_kd", 20.0)
        self.setting_amb_deadband = s("ambient_mode_deadband_f", 1.0)
        self.setting_pid_envelope = s("pid_envelope_f", 1.0)
        self.setting_crash_envelope = s("crash_mode_envelope_f", 2.0)
        self.setting_ramp_tol = s("ramp_pre_ramp_tolerance_f", 0.2)
        self.setting_ramp_deadband = s("ramp_thermostatic_deadband_f", 0.1)
        self.setting_ramp_landing = s("ramp_pid_landing_zone_f", 0.5)
        
        self.is_settings_dirty = False

    def tick(self, dt):
        if hasattr(self, 'temp_controller'):
            if self.monitoring_state == "OFF":
                threading.Thread(target=self.temp_controller.update_control_logic_and_ui_data, daemon=True).start()
            self._update_warning_status()

    def _update_warning_status(self):
        if not hasattr(self, 'relay_control'): return
        now = time.time()
        disabled_until = getattr(self.relay_control, 'cool_disabled_until', 0)
        
        if disabled_until > now:
            remaining = int(disabled_until - now)
            mins = remaining // 60
            secs = remaining % 60
            self.warning_message = f"Compressor Delay: {mins:02}:{secs:02} remaining"
        else:
            self.warning_message = ""

    # --- SETTINGS LOGIC ---
    def scan_sensors(self):
        if not hasattr(self, 'temp_controller'): return
        self._refresh_all_settings_from_manager()
        self.log_system_message("Scanning for sensors...")
        def _scan():
            found = self.temp_controller.detect_ds18b20_sensors()
            if "unassigned" not in found: found.insert(0, "unassigned")
            Clock.schedule_once(lambda dt: setattr(self, 'available_sensors', found))
        threading.Thread(target=_scan, daemon=True).start()

    # --- STAGING LOGIC ---
    def stage_setting_change(self, key, new_value):
        self.staged_changes[key] = new_value
        self.is_settings_dirty = True
        
        prop_name = self.property_map.get(key)
        if prop_name:
            # Update UI string immediately for feedback
            if isinstance(new_value, (float, int)) and "setting_" in prop_name:
                fmt = ".4f" if key == "pid_ki" else ".1f"
                setattr(self, prop_name, f"{float(new_value):{fmt}}")
            else:
                setattr(self, prop_name, str(new_value))

    def stage_text_input(self, key, text_value):
        """Called by TextInputs. Converts string to float safely."""
        try:
            val = float(text_value)
            self.stage_setting_change(key, val)
        except ValueError:
            # Ignore invalid typing (e.g. "2.") until it's valid
            pass

    # --- TARGET TAB (Slider Logic) ---
    def adjust_target(self, key, delta):
        try:
            if key in self.staged_changes:
                current = float(self.staged_changes[key])
            else:
                current = float(self.settings_manager.get(key, 0.0))
            self.stage_setting_change(key, current + delta)
        except Exception as e:
            print(f"Error adjust: {e}")

    def save_target_from_slider(self, key, value):
        try:
            new_val = float(value)
            prop_name = self.property_map.get(key)
            if prop_name:
                current_ui = float(getattr(self, prop_name))
                if abs(new_val - current_ui) > 0.01:
                    self.stage_setting_change(key, new_val)
        except: pass

    # --- SYSTEM & OTHERS ---
    def save_sensor_setting(self, sensor_type, value):
        key = "ds18b20_beer_sensor" if sensor_type == "beer" else "ds18b20_ambient_sensor"
        current_saved = self.settings_manager.get(key)
        if value != current_saved or key in self.staged_changes:
            self.stage_setting_change(key, value)

    def set_relay_logic(self, active_high):
        self.stage_setting_change("relay_active_high", active_high)

    # --- GLOBAL ACTIONS ---
    def check_unsaved_changes(self):
        if self.is_settings_dirty:
            DirtyPopup().open()
        else:
            self.go_to_screen('dashboard', 'right')

    def discard_changes(self):
        self._refresh_all_settings_from_manager()
        self.go_to_screen('dashboard', 'right')

    def save_and_exit(self):
        if not hasattr(self, 'settings_manager'): return
        self.log_system_message("Saving Settings...")
        
        # Separate Cooling keys for conversion
        cooling_keys = ["cooling_dwell_time_s", "cooling_max_run_time_s", "min_off_time_s"]
        cooling_update = {}
        
        for key, val in self.staged_changes.items():
            if key in cooling_keys:
                # Convert Minutes (UI) -> Seconds (Backend)
                cooling_update[key] = float(val) * 60.0
            else:
                self.settings_manager.set(key, val)
                
        if cooling_update:
            current = self.settings_manager.get_all_compressor_protection_settings()
            current.update(cooling_update)
            self.settings_manager.save_compressor_protection_settings(current)
            
        if hasattr(self, 'temp_controller') and hasattr(self.temp_controller, 'pid'):
            # Update Live PID
            self.temp_controller.pid.Kp = float(self.settings_manager.get("pid_kp", 2.0))
            self.temp_controller.pid.Ki = float(self.settings_manager.get("pid_ki", 0.03))
            self.temp_controller.pid.Kd = float(self.settings_manager.get("pid_kd", 20.0))

        if "relay_active_high" in self.staged_changes:
            self.settings_manager.set("relay_logic_configured", True)
            self.relay_control.update_relay_logic()
            
        self.staged_changes.clear()
        self.is_settings_dirty = False
        threading.Thread(target=self.temp_controller.update_control_logic_and_ui_data, daemon=True).start()
        self.go_to_screen('dashboard', 'right')

    def reset_targets_to_defaults(self):
        self.log_system_message("Resetting to Defaults (Unsaved).")
        
        # 1. Targets
        defaults = {
            "ambient_hold_f": 68.0, "beer_hold_f": 68.0,
            "ramp_up_hold_f": 68.0, "ramp_up_duration_hours": 24.0,
            "fast_crash_hold_f": 34.0,
            # 2. Cooling (Minutes)
            "cooling_dwell_time_s": 3.0,
            "cooling_max_run_time_s": 120.0,
            "min_off_time_s": 60.0,
            # 3. PID & Tuning
            "pid_kp": 2.0, "pid_ki": 0.03, "pid_kd": 20.0,
            "ambient_mode_deadband_f": 1.0,
            "pid_envelope_f": 1.0,
            "crash_mode_envelope_f": 2.0,
            "ramp_pre_ramp_tolerance_f": 0.2,
            "ramp_thermostatic_deadband_f": 0.1,
            "ramp_pid_landing_zone_f": 0.5
        }
        
        for key, val in defaults.items():
            self.stage_setting_change(key, val)

    # --- NAVIGATION & CLEANUP ---
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
        threading.Thread(target=self.temp_controller.update_control_logic_and_ui_data, daemon=True).start()

    def toggle_monitoring(self, new_state):
        if not hasattr(self, 'temp_controller'): return
        if new_state == "ON":
            self.temp_controller.start_monitoring()
        else:
            self.temp_controller.stop_monitoring()

    @mainthread
    def push_data_update(self, **kwargs):
        def fmt(val):
            try: return f"{float(val):.1f}"
            except (ValueError, TypeError): return "--.-"

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
        self.heater_color = [0.8, 0, 0, 1] if "HEATING" in h_state else [0.2, 0.2, 0.2, 1]
        self.cooler_color = [0.2, 0.2, 0.8, 1] if "COOLING" in c_state else [0.2, 0.2, 0.2, 1]

        mode_internal = kwargs.get('current_mode', 'Ambient Hold')
        self._sync_control_mode_from_backend(mode_internal)

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

if __name__ == '__main__':
    FermVaultApp().run()
