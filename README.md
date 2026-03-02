<h1>🌟 Wall Blazer</h1>

<p><strong>Wall Blazer</strong> is a powerful, highly customizable video and web wallpaper application for Linux and Windows desktops. Built with Python and GTK3, it provides an elegant native experience to bring your desktop to life.</p>

<p>Whether you want to loop local video files, stream directly from YouTube, or render interactive WebGL pages as your background, Wall Blazer handles it seamlessly.</p>

<p><a href="https://github.com/oooosonuoooo/Wall-Blazer.git"><strong>GitHub Repository</strong></a></p>

<h3>🎥 Demo</h3>
<p>
  <video src="https://github.com/user-attachments/assets/53dfd759-e18e-43d1-9762-6f8dbd808a3a" controls="controls" muted="muted" style="max-width: 100%;"></video>
</p>

<hr>

<h2>📸 Screenshots</h2>

<table>
  <tr>
    <td align="center"><b>Main UI Library</b><br><img src="https://github.com/user-attachments/assets/561dffc7-80e2-4e01-a52b-cd128c2c15ef" width="400" alt="Main UI"/></td>
    <td align="center"><b>Playlist Management</b><br><img src="https://github.com/user-attachments/assets/d9b49113-deb1-4616-b108-5c6d8da25fde" width="400" alt="Playlists"/></td>
  </tr>
  <tr>
    <td align="center"><b>YouTube Streaming</b><br><img src="https://github.com/user-attachments/assets/e979c681-efdf-41bf-957f-553f76f546c9" width="400" alt="Streaming"/></td>
    <td align="center"><b>Interactive Web Pages</b><br><img src="https://github.com/user-attachments/assets/188114af-6805-474c-9c4c-3df50271704c" width="400" alt="Webpages"/></td>
  </tr>
</table>

<hr>

<h2>🙌 Shoutouts & Credits</h2>

<p>A massive shoutout to the amazing projects and creators that made Wall Blazer possible:</p>
<ul>
  <li><strong>Hidamari:</strong> This app is a straight inspiration from <a href="https://github.com/jeffshee/hidamari.git">Hidamari</a> by jeffshee.</li>
  <li><strong>yt-dlp:</strong> YouTube streaming capabilities are powered entirely by <a href="https://github.com/yt-dlp/yt-dlp.git">yt-dlp</a>.</li>
  <li><strong>TeshiiLatte:</strong> The awesome anime videos used in the demo are created by <a href="https://www.youtube.com/@TeshiiSan">TeshiiLatte / TeshiiSan</a>.</li>
  <li><strong>AlteredQualia:</strong> Check out <a href="https://alteredqualia.com/">alteredqualia.com</a> for the amazing WebGL examples! They were used to demonstrate the website wallpaper page.</li>
</ul>

<hr>

<h2>✨ Features</h2>

<h3>📁 File Management (NEW!)</h3>
<ul>
  <li><strong>Advanced File Search:</strong> Quickly locate your favorite videos and web files with the newly integrated, lightning-fast file search capability.</li>
  <li><strong>Full File Manager Control:</strong> Browse, manage, and organize your media directly from within the app using the brand-new, fully featured built-in file manager interface.</li>
</ul>

<h3>🎬 Media Support</h3>
<ul>
  <li><strong>Video Wallpapers:</strong> Play local video files seamlessly on your desktop.</li>
  <li><strong>YouTube Streams:</strong> Stream YouTube videos directly to your desktop.</li>
  <li><strong>Interactive Web Pages:</strong> Set websites and interactive elements (like WebGL) as your wallpaper.</li>
</ul>

<h3>🖥️ Display & Playback</h3>
<ul>
  <li><strong>Multi-Monitor Support:</strong> Set completely different wallpapers and playlists for each monitor.</li>
  <li><strong>Seamless Playlist Switching:</strong> Preloads the next video in the background and switches ~0.5 seconds early to eliminate flickering and ensure instant transitions.</li>
  <li><strong>Custom Playlists:</strong> Create multiple named playlists, assign them to specific monitors, and customize intervals and shuffle modes.</li>
  <li><strong>GPU-First Playback:</strong> Automatically utilizes hardware decoding (VAAPI/VDPAU/DRM/auto) to save battery, gracefully falling back to CPU if no stable GPU path is found.</li>
  <li><strong>Broad Video Preview:</strong> Employs multiple extraction strategies to generate previews even for difficult codecs and containers.</li>
</ul>

<h3>⚙️ System Integration & Resource Management</h3>
<ul>
  <li><strong>Extreme Low-End CPU Support:</strong> Specially tuned VLC decoding threads to drop overhead, skip irrelevant B-frames, and disable IDCT scaling logic. Capable of rendering smoothly even on <strong>0.5GHz</strong> mobile CPUs!</li>
  <li><strong>Smart Pause & Mute:</strong> Automatically pauses or mutes playback when a window is maximized or goes fullscreen to conserve system resources.</li>
  <li><strong>Native Desktop Integration:</strong> Built for GNOME, Cinnamon, and other GTK-based desktops. Supports native X11 and Wayland (with Wayland-specific window detection limits).</li>
  <li><strong>Auto-Start & Tray Icon:</strong> Automatically launches on boot via system portal/<code>.desktop</code> entry and runs quietly in the system tray.</li>
  <li><strong>GTK Theme Sync:</strong> Automatically adapts to your system's Light, Dark, or System theme.</li>
  <li><strong>Self-Healing & Auto-Repair:</strong> Features runtime health checks, a player watchdog for auto-restarts, and an installer-provisioned repair command and timer.</li>
</ul>

<h3>🚀 Performance Optimization Scripts</h3>
<p>If you notice that your display manager (like <strong>LightDM</strong>) is using way too much CPU/RAM in the background, we've provided a dedicated bash script to optimize it for maximum resource efficiency.</p>
<ul>
  <li><strong>LightDM Resource Optimizer:</strong> Run <code>sudo bash optimize_lightdm.sh</code> and reboot. This script replaces Heavy greeters with <code>lightdm-gtk-greeter</code>, disables guest sessions, background animations, and strips remote VNC listeners completely to free up RAM/CPU!</li>
</ul>

<hr>

<h2>📦 Installation & Setup</h2>

<p>Wall Blazer includes a robust, automated bash script that handles all system dependencies, Python packages, and the native build process using Meson and Ninja.</p>

<h3>1. Prerequisites (Handled Automatically)</h3>
<p>The installer will automatically fetch:</p>
<blockquote>
  <p>Python 3 &amp; <code>pip</code> • GTK3 &amp; GObject Introspection • Wnck • Tray AppIndicator • VLC &amp; FFmpeg • <code>meson</code>, <code>ninja-build</code>, <code>pkg-config</code> • <code>yt-dlp</code>, <code>pydbus</code>, <code>Pillow</code>, <code>requests</code>, <code>setproctitle</code></p>
</blockquote>

<h3>2. Installing / Updating</h3>
<p>Clone the repository and run the installation script. <strong>Do not run the script directly as root</strong>; it will prompt for your <code>sudo</code> password when needed.</p>

<pre><code class="language-bash">git clone https://github.com/oooosonuoooo/Wall-Blazer.git
cd Wall-Blazer
chmod +x install.sh
./install.sh</code></pre>

<h3>3. Windows Installation (Automated)</h3>
<p>Wall Blazer now supports native Windows! You can run it seamlessly behind your desktop icons with a 1-click installer setup.</p>
<ol>
  <li>Download the repository as a ZIP or clone it via Git.</li>
  <li>Double-click the <code>install_windows.bat</code> file.</li>
  <li>It will automatically download/install <code>Python 3.11</code>, <code>VLC (64-bit)</code>, compile the source into a standalone <code>Wall-Blazer.exe</code>, and securely drop it into your startup folder!</li>
</ol>
