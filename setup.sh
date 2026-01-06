#!/bin/bash
# setup.sh
# Single-line installer wrapper for FermVault_lite

# 1. Define the Install Directories
INSTALL_DIR="$HOME/fermvault_lite"

# --- CRITICAL FIX: Export variables so install.sh can see them ---
export DATA_DIR="$HOME/fermvault_lite-data"
export DESKTOP_FILENAME="fermvault_lite.desktop"
export APP_TITLE="FermVault Lite"
# ---------------------------------------------------------------

WHAT_TO_INSTALL="FermVault Lite Application and Data Directory"
CLEANUP_MODE="NONE"

echo "========================================"
echo "    FermVault Lite Auto-Installer (LITE)"
echo "========================================"

# 2. Logic to handle existing installs
if [ -d "$INSTALL_DIR" ] || [ -d "$DATA_DIR" ]; then
    echo ""
    echo "Existing installation detected:"
    [ -d "$INSTALL_DIR" ] && echo " - App Folder: $INSTALL_DIR"
    [ -d "$DATA_DIR" ]    && echo " - Data Folder: $DATA_DIR"
    echo ""
    echo "How would you like to proceed? (Case Sensitive)"
    echo "  APP  - Reinstall App only (Keeps your existing data/settings)"
    echo "  ALL  - Reinstall App AND reset data (Fresh Install)"
    echo "  EXIT - Cancel installation"
    echo ""
    read -p "Enter selection: " choice
    
    if [ "$choice" == "APP" ]; then
        WHAT_TO_INSTALL="FermVault LIte Application"
        CLEANUP_MODE="APP"
    elif [ "$choice" == "ALL" ]; then
        WHAT_TO_INSTALL="FermVault Lite Application and Data Directory"
        CLEANUP_MODE="ALL"
    else
        echo "Cancelled."
        exit 0
    fi
fi

# 3. Size Warning / Confirmation
echo ""
echo "------------------------------------------------------------"
echo "This script will install the $WHAT_TO_INSTALL"
echo "and will use about 20 MB of storage space on the Pi's SD card."
echo ""
echo "Basic installed file structure:"
echo ""
echo "  $INSTALL_DIR/"
echo "  ├── utility files..."
echo "  ├── src/"
echo "  │ ├── application files..."
echo "  │ └── assets/"
echo "  │    └── supporting files..."
echo "  ├── venv/"
echo "  │ └── dependencies..."
echo "  $DATA_DIR/"
echo "  └── user data..."
echo ""
echo "------------------------------------------------------------"
echo ""

read -p "Press Y to proceed, or any other key to cancel: " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Installation cancelled."
    exit 1
fi

# 4. Perform Cleanup (Delayed until AFTER confirmation)
if [ "$CLEANUP_MODE" == "APP" ]; then
    echo "Removing existing application..."
    rm -rf "$INSTALL_DIR"
elif [ "$CLEANUP_MODE" == "ALL" ]; then
    echo "Removing application and data..."
    rm -rf "$INSTALL_DIR"
    rm -rf "$DATA_DIR"
fi

# 5. Check/Install Git
if ! command -v git &> /dev/null; then
    echo "Git not found. Installing..."
    sudo apt-get update && sudo apt-get install -y git
fi

# 6. Clone Repo
# Since we deleted the directory in the logic above (if it existed), 
# we can simply clone.
if [ -d "$INSTALL_DIR" ]; then
    echo "Directory exists (Update mode)..."
    cd "$INSTALL_DIR" || exit 1
    git pull
else
    echo "Cloning repository to $INSTALL_DIR..."
    git clone https://github.com/keglevelmonitor/fermvault_lite.git "$INSTALL_DIR"
    cd "$INSTALL_DIR" || exit 1
fi

# 7. Run the Main Installer
echo "Launching main installer..."
chmod +x install.sh

# The exported variables (DATA_DIR, DESKTOP_FILENAME) will now be passed automatically
./install.sh
