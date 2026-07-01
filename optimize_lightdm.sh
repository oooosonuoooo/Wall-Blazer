#!/bin/bash
# LightDM Resource Optimization Script for Wall-Blazer users
# This script will reduce LightDM memory and CPU usage by disabling animations, VNC, and switching to a lightweight greeter.

if [ "$EUID" -ne 0 ]; then
  echo "Please run this script as root (sudo ./optimize_lightdm.sh)"
  exit 1
fi

echo "==========================================="
echo "   LightDM Optimization Script Started     "
echo "==========================================="

echo "[INFO] Installing lightweight lightdm-gtk-greeter if missing..."
if command -v apt-get &> /dev/null; then
    apt-get update -y && apt-get install -y lightdm-gtk-greeter
elif command -v pacman &> /dev/null; then
    pacman -Sy --noconfirm lightdm-gtk-greeter
elif command -v dnf &> /dev/null; then
    dnf install -y lightdm-gtk-greeter
else
    echo "[WARNING] Unsupported package manager. Please ensure 'lightdm-gtk-greeter' is installed."
fi

# Set the active greeter
echo "[INFO] Configuring LightDM to use the GTK greeter..."
LIGHTDM_CONF="/etc/lightdm/lightdm.conf"

if [ ! -f "$LIGHTDM_CONF" ]; then
    echo "[INFO] /etc/lightdm/lightdm.conf not found. Creating a baseline configuration..."
    mkdir -p /etc/lightdm/
    touch "$LIGHTDM_CONF"
fi

# Backup the configuration file
cp "$LIGHTDM_CONF" "$LIGHTDM_CONF.backup.$(date +%F_%T)"

# Function to reliably set keys in lightdm.conf INI format
set_ini_value() {
    local section=$1
    local key=$2
    local value=$3
    local file=$4
    
    # If section exists, append/replace key under it
    if grep -q "^\[$section\]" "$file"; then
        if awk -v s="[$section]" '$0==s{f=1; next} /^\[.*\]/{f=0} f' "$file" | grep -q "^[#]*$key="; then
             sed -i -e "/^\[$section\]/,/^\[.*\]/ s|^[#]*$key=.*|$key=$value|" "$file"
        else
             sed -i -e "/^\[$section\]/a $key=$value" "$file"
        fi
    else
        echo -e "\n[$section]\n$key=$value" >> "$file"
    fi
}

# Apply Resource Saving Optimizations to LightDM
echo "[INFO] Applying resource saving modifications..."

# Optimize seat defaults (Disable animations, disable guest session, use basic greeter)
set_ini_value "Seat:*" "greeter-session" "lightdm-gtk-greeter" "$LIGHTDM_CONF"
set_ini_value "Seat:*" "greeter-show-manual-login" "false" "$LIGHTDM_CONF"
set_ini_value "Seat:*" "greeter-hide-users" "false" "$LIGHTDM_CONF"
set_ini_value "Seat:*" "allow-guest" "false" "$LIGHTDM_CONF"
set_ini_value "Seat:*" "xserver-allow-tcp" "false" "$LIGHTDM_CONF"

# Disable VNC and XDMCP (Releases background socket listeners)
set_ini_value "VNCServer" "enabled" "false" "$LIGHTDM_CONF"
set_ini_value "XDMCPServer" "enabled" "false" "$LIGHTDM_CONF"

# Tweak the GTK Greeter itself to be lightweight
GTK_GREETER_CONF="/etc/lightdm/lightdm-gtk-greeter.conf"
if [ ! -f "$GTK_GREETER_CONF" ]; then
    touch "$GTK_GREETER_CONF"
fi
set_ini_value "greeter" "background" "#000000" "$GTK_GREETER_CONF"
set_ini_value "greeter" "theme-name" "Adwaita" "$GTK_GREETER_CONF"
set_ini_value "greeter" "icon-theme-name" "Adwaita" "$GTK_GREETER_CONF"
set_ini_value "greeter" "indicators" "~host;~spacer;~clock;~spacer;~language;~session;~power" "$GTK_GREETER_CONF"
set_ini_value "greeter" "clock-format" "%H:%M" "$GTK_GREETER_CONF"

echo "==========================================="
echo "   Optimization Complete!                  "
echo "   A backup of your old config is saved at "
echo "   $LIGHTDM_CONF.backup.*                  "
echo "                                           "
echo "   Please REBOOT your system to apply      "
echo "   the new Lightweight LightDM settings.   "
echo "==========================================="
