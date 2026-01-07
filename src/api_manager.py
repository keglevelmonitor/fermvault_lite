import os
import sys
import importlib.util
import inspect
import threading

class APIManager:
    
    def __init__(self, settings_manager, scan_directory=None):
        self.settings_manager = settings_manager
        self.available_services = {"OFF": None}
        self.active_service_instance = None
        self.session_map = {} 
        
        # If no directory provided, default to the folder containing this script
        if scan_directory is None:
            scan_directory = os.path.dirname(os.path.abspath(__file__))
            
        self._discover_services(scan_directory)

    def _discover_services(self, directory):
        """Scans directory for *.api.py, loads them, and finds the API class inside."""
        print(f"APIManager: Scanning for services in: {directory}")
        
        if not os.path.exists(directory):
            print(f"[ERROR] APIManager: Directory not found: {directory}")
            return

        for filename in os.listdir(directory):
            if filename.endswith(".api.py"):
                module_name = filename[:-7] # remove .api.py
                file_path = os.path.join(directory, filename)
                
                try:
                    # 1. Load the module dynamically
                    spec = importlib.util.spec_from_file_location(f"api_modules.{module_name}", file_path)
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        sys.modules[spec.name] = module
                        spec.loader.exec_module(module)
                        
                        # 2. Inspect module for a compatible API class
                        # Look for any class that ends with 'API' and has a 'get_data' method
                        found_class = None
                        for name, obj in inspect.getmembers(module, inspect.isclass):
                            # Ensure it's defined in this module (not imported) and looks like an API
                            if obj.__module__ == spec.name and name.endswith("API"):
                                found_class = obj
                                break
                        
                        if found_class:
                            # Use the filename prefix as the service key (e.g., "brewersfriend")
                            self.available_services[module_name] = found_class
                            print(f"APIManager: Successfully loaded '{module_name}' (Class: {found_class.__name__})")
                        else:
                            print(f"APIManager: Skipped {filename} - No class ending in 'API' found.")

                except ImportError as e:
                    print(f"[ERROR] APIManager: Could not load {filename}. Missing dependency? Error: {e}")
                except Exception as e:
                    print(f"[ERROR] APIManager: Failed to load {filename}: {e}")

        # Initialize the active service if one was saved
        saved_service = self.settings_manager.get("active_api_service", "OFF")
        self.set_active_service(saved_service)

    def get_service_list(self):
        """Returns sorted list of available services (OFF is always first)."""
        keys = list(self.available_services.keys())
        if "OFF" in keys: keys.remove("OFF")
        keys.sort()
        return ["OFF"] + keys

    def set_active_service(self, service_name):
        """Instantiates the selected API service class."""
        if service_name not in self.available_services:
            if service_name != "OFF":
                print(f"[ERROR] APIManager: Service '{service_name}' not available. Defaulting to OFF.")
            service_name = "OFF"
            
        if service_name == "OFF":
            self.active_service_instance = None
            self.settings_manager.set("active_api_service", "OFF")
            self.session_map = {}
            return

        try:
            ServiceClass = self.available_services[service_name]
            self.active_service_instance = ServiceClass(self.settings_manager)
            self.settings_manager.set("active_api_service", service_name)
            print(f"APIManager: Active service set to {service_name}.")
        except Exception as e:
            print(f"[ERROR] APIManager: Failed to instantiate {service_name}: {e}")
            self.active_service_instance = None
            self.settings_manager.set("active_api_service", "OFF")

    def fetch_sessions_threaded(self, on_success, on_error):
        """Fetches brew sessions in a background thread."""
        if not self.active_service_instance:
            on_error("API is OFF")
            return

        def _worker():
            try:
                sessions_data = self.active_service_instance.get_data("list_sessions")
                
                if sessions_data is None:
                    on_error("No Data / API Error")
                    return
                
                if isinstance(sessions_data, dict) and "error" in sessions_data:
                    on_error(sessions_data["error"])
                    return

                self.session_map = {}
                display_titles = []
                
                for session in sessions_data:
                    title = session.get('recipe_title', session.get('name', 'Unknown'))
                    sid = str(session.get('id', ''))
                    if title and sid:
                        self.session_map[title] = sid
                        display_titles.append(title)
                
                if not display_titles:
                    on_error("No Sessions Found")
                else:
                    on_success(display_titles)

            except Exception as e:
                print(f"APIManager Error: {e}")
                on_error(str(e))

        threading.Thread(target=_worker, daemon=True).start()

    def get_session_id_by_title(self, title):
        return self.session_map.get(title)

    def get_api_data(self, data_type, session_id=None):
        if self.active_service_instance:
            try:
                return self.active_service_instance.get_data(data_type, session_id)
            except Exception as e:
                print(f"[ERROR] APIManager call failed: {e}")
        return None
