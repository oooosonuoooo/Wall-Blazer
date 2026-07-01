import logging
import threading
import multiprocessing as mp
import setproctitle
import sys

AppIndicator = None
def _init_gtk():
    global AppIndicator
    import gi
    gi.require_version("Gtk", "3.0")
    import sys
    if sys.platform != "win32":
        try:
            gi.require_version('AyatanaAppIndicator3', '0.1')
            from gi.repository import AyatanaAppIndicator3 as AppIndicator
        except (ImportError, ValueError):
            AppIndicator = None

try:
    from commons import *
    from ipc import get_service
except (ModuleNotFoundError, ImportError):
    from wallblazer.commons import *
    from wallblazer.ipc import get_service

logger = logging.getLogger(LOGGER_NAME)

APP_INDICATOR_ID = PROJECT
APP_INDICATOR_ICON = "com.wallblazer.WallBlazer"

def connect():
    try:
        return get_service(DBUS_NAME_SERVER)
    except Exception:
        logger.error("[Menu] Couldn't connect to server")
    return


def on_item_show():
    server = connect()
    if server:
        server.show_gui()


def on_item_mute():
    server = connect()
    if server:
        prev_state = server.is_mute
        server.is_mute = not prev_state


def on_item_pause():
    server = connect()
    if server:
        prev_state = server.is_paused_by_user
        server.is_paused_by_user = not prev_state
        if not prev_state:
            server.pause_playback()
        else:
            server.start_playback()


def on_item_reload():
    server = connect()
    if server:
        server.reload()


def on_item_lucky():
    server = connect()
    if server:
        server.feeling_lucky()


def on_item_quit():
    server = connect()
    if server:
        server.quit()


def start_action(f: callable):
    """Use this function to execute callback (for not blocking the UI)"""
    t = threading.Thread(target=f)
    t.start()


def build_menu(mode):
    from gi.repository import Gtk
    menu = Gtk.Menu()
    #
    item_show = Gtk.MenuItem(label="Show Wall Blazer")
    item_show.connect("activate", lambda *_: start_action(on_item_show))
    #
    item_mute = Gtk.MenuItem(label="Toggle Mute Audio")
    item_mute.connect("activate", lambda *_: start_action(on_item_mute))
    #
    item_pause = Gtk.MenuItem(label="Toggle Play/Pause")
    item_pause.connect("activate", lambda *_: start_action(on_item_pause))
    #
    item_reload = Gtk.MenuItem(label="Reload")
    item_reload.connect("activate", lambda *_: start_action(on_item_reload))
    #
    item_lucky = Gtk.MenuItem(label="I'm Feeling Lucky")
    item_lucky.connect("activate", lambda *_: start_action(on_item_lucky))
    #
    item_quit = Gtk.MenuItem(label="Quit Wall Blazer")
    item_quit.connect("activate", lambda *_: start_action(on_item_quit))
    #
    # Filter out unsupported action in current mode
    if mode == MODE_WEBPAGE:
        item_list = [item_show, item_mute, item_reload, item_lucky, item_quit]
    else:
        item_list = [item_show, item_mute, item_pause, item_reload, item_lucky, item_quit]
    for item in item_list:
        menu.append(item)
    menu.show_all()
    return menu


def show_systray_icon(mode):
    _init_gtk()
    global AppIndicator
    from gi.repository import Gtk
    if AppIndicator is None:
        logger.info("[Systray] AppIndicator is unavailable on this platform; skipping tray icon")
        return

    setproctitle.setproctitle(mp.current_process().name)
    
    menu = build_menu(mode)
    indicator = AppIndicator.Indicator.new(id=APP_INDICATOR_ID, icon_name=APP_INDICATOR_ICON,
                                           category=AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
    indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
    indicator.set_menu(menu)
    logger.info("[Systray] Ready")
    Gtk.main()


if __name__ == "__main__":
    show_systray_icon(MODE_VIDEO)
