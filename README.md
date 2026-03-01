# 🌟 Wall Blazer

**Wall Blazer** is a powerful, highly customizable video and web wallpaper application for Linux desktops. Built with Python and GTK3, it provides an elegant native experience to bring your desktop to life. 

Whether you want to loop local video files, stream directly from YouTube, or render interactive WebGL pages as your background, Wall Blazer handles it seamlessly. 

[**GitHub Repository**](https://github.com/oooosonuoooo/Wall-Blazer.git)

### 🎥 Demo

https://github.com/user-attachments/assets/53dfd759-e18e-43d1-9762-6f8dbd808a3a

---

## 📸 Screenshots

<table>
  <tr>
    <td align="center"><b>Main UI Library</b><br><img src="https://github.com/user-attachments/assets/c62a53e5-f085-48c8-a154-0f7d114cbaec" width="400" alt="Main UI"/></td>
    <td align="center"><b>Playlist Management</b><br><img src="https://github.com/user-attachments/assets/d9b49113-deb1-4616-b108-5c6d8da25fde" width="400" alt="Playlists"/></td>
  </tr>
  <tr>
    <td align="center"><b>YouTube Streaming</b><br><img src="https://github.com/user-attachments/assets/e979c681-efdf-41bf-957f-553f76f546c9" width="400" alt="Streaming"/></td>
    <td align="center"><b>Interactive Web Pages</b><br><img src="https://github.com/user-attachments/assets/188114af-6805-474c-9c4c-3df50271704c" width="400" alt="Webpages"/></td>
  </tr>
</table>

---

## 🙌 Shoutouts & Credits

A massive shoutout to the amazing projects and creators that made Wall Blazer possible:

* **Hidamari:** This app is a straight inspiration from [Hidamari](https://github.com/jeffshee/hidamari.git) by jeffshee.
* **yt-dlp:** YouTube streaming capabilities are powered entirely by [yt-dlp](https://github.com/yt-dlp/yt-dlp.git).
* **TeshiiLatte:** The awesome anime videos used in the demo are created by [TeshiiLatte / TeshiiSan](https://www.youtube.com/@TeshiiSan).
* **AlteredQualia:** Check out [alteredqualia.com](https://alteredqualia.com/) for the amazing WebGL examples! They were used to demonstrate the website wallpaper page.

---

## ✨ Features

### 🎬 Media Support
* **Video Wallpapers:** Play local video files seamlessly on your desktop.
* **YouTube Streams:** Stream YouTube videos directly to your desktop.
* **Interactive Web Pages:** Set websites and interactive elements (like WebGL) as your wallpaper.

### 🖥️ Display & Playback
* **Multi-Monitor Support:** Set completely different wallpapers and playlists for each monitor.
* **Seamless Playlist Switching:** Preloads the next video in the background and switches ~0.5 seconds early to eliminate flickering and ensure instant transitions.
* **Custom Playlists:** Create multiple named playlists, assign them to specific monitors, and customize intervals and shuffle modes.
* **GPU-First Playback:** Automatically utilizes hardware decoding (VAAPI/VDPAU/DRM/auto) to save battery, gracefully falling back to CPU if no stable GPU path is found.
* **Broad Video Preview:** Employs multiple extraction strategies to generate previews even for difficult codecs and containers.

### ⚙️ System Integration & Resource Management
* **Extreme Low-End CPU Support**: Specially tuned VLC decoding threads to drop overhead, skip irrelevant B-frames, and disable IDCT scaling logic. Capable of rendering smoothly even on **0.5GHz** mobile CPUs!
* **Smart Pause & Mute:** Automatically pauses or mutes playback when a window is maximized or goes fullscreen to conserve system resources.
* **Native Desktop Integration:** Built for GNOME, Cinnamon, and other GTK-based desktops. Supports native X11 and Wayland (with Wayland-specific window detection limits).
* **Auto-Start & Tray Icon:** Automatically launches on boot via system portal/`.desktop` entry and runs quietly in the system tray.
* **GTK Theme Sync:** Automatically adapts to your system's Light, Dark, or System theme.
* **Self-Healing & Auto-Repair:** Features runtime health checks, a player watchdog for auto-restarts, and an installer-provisioned repair command and timer.

### 🚀 Performance Optimization Scripts
If you notice that your display manager (like **LightDM**) is using way too much CPU/RAM in the background, we've provided a dedicated bash script to optimize it for maximum resource efficiency.

* **LightDM Resource Optimizer:** Run `sudo bash optimize_lightdm.sh` and reboot. This script replaces Heavy greeters with `lightdm-gtk-greeter`, disables guest sessions, background animations, and strips remote VNC listeners completely to free up RAM/CPU!

---

## 📦 Installation & Setup

Wall Blazer includes a robust, automated bash script that handles all system dependencies, Python packages, and the native build process using Meson and Ninja.

### 1. Prerequisites (Handled Automatically)
The installer will automatically fetch:
> Python 3 & `pip` • GTK3 & GObject Introspection • Wnck • Tray AppIndicator • VLC & FFmpeg • `meson`, `ninja-build`, `pkg-config` • `yt-dlp`, `pydbus`, `Pillow`, `requests`, `setproctitle`

### 2. Installing / Updating
Clone the repository and run the installation script. **Do not run the script directly as root**; it will prompt for your `sudo` password when needed.

```bash
git clone https://github.com/oooosonuoooo/Wall-Blazer.git
cd Wall-Blazer
chmod +x install.sh
./install.sh
```

### 3. Windows Installation (Automated)
Wall Blazer now supports native Windows! You can run it seamlessly behind your desktop icons with a 1-click installer setup.
1. Download the repository as a ZIP or clone it via Git.
2. Double-click the `install_windows.bat` file.
3. It will automatically download/install `Python 3.11`, `VLC (64-bit)`, compile the source into a standalone `Wall-Blazer.exe`, and securely drop it into your startup folder!
