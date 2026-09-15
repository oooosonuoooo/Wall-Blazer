import logging
import os
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


def _show_delete_message(text, message_type):
    from gi.repository import Gtk

    dialog = Gtk.MessageDialog(
        parent=None,
        modal=True,
        text="Delete This Video",
        message_type=message_type,
        secondary_text=text,
        buttons=Gtk.ButtonsType.OK,
    )
    dialog.run()
    dialog.destroy()
    return False


def _delete_confirmed(server, expected_path):
    from gi.repository import GLib, Gtk

    try:
        deleted = server.delete_current_video(expected_path)
        if isinstance(deleted, (tuple, list)):
            deleted = deleted[0] if deleted else False
    except Exception:
        deleted = False
    if not deleted:
        GLib.idle_add(
            _show_delete_message,
            "Wall Blazer could not delete the current video. It may have been removed, changed, or be inaccessible.",
            Gtk.MessageType.ERROR,
        )


def _confirm_delete_video(server, current_path):
    from gi.repository import Gtk

    if not isinstance(current_path, str) or not current_path:
        return _show_delete_message(
            "The current wallpaper is not an available local video file.",
            Gtk.MessageType.INFO,
        )

    filename = os.path.basename(os.path.normpath(current_path)) or current_path
    dialog = Gtk.MessageDialog(
        parent=None,
        modal=True,
        text="Delete this video?",
        message_type=Gtk.MessageType.QUESTION,
        secondary_text=(
            f"{filename}\n\n"
            "This will permanently delete the video file from your computer."
        ),
        buttons=Gtk.ButtonsType.NONE,
    )
    dialog.add_buttons(
        "Cancel", Gtk.ResponseType.CANCEL,
        "Delete", Gtk.ResponseType.OK,
    )
    dialog.set_default_response(Gtk.ResponseType.CANCEL)
    response = dialog.run()
    dialog.destroy()
    if response == Gtk.ResponseType.OK:
        start_action(_delete_confirmed, server, current_path)
    return False


def on_item_delete_video():
    server = connect()
    if not server:
        return
    try:
        current_path = server.current_video_path
    except Exception:
        current_path = ""

    from gi.repository import GLib
    GLib.idle_add(_confirm_delete_video, server, current_path)


def on_item_quit():
    server = connect()
    if server:
        server.quit()


def start_action(f: callable, *args):
    """Use this function to execute callback (for not blocking the UI)"""
    t = threading.Thread(target=f, args=args, daemon=True)
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
    item_delete = Gtk.MenuItem(label="Delete This Video")
    item_delete.connect("activate", lambda *_: start_action(on_item_delete_video))
    #
    item_quit = Gtk.MenuItem(label="Quit Wall Blazer")
    item_quit.connect("activate", lambda *_: start_action(on_item_quit))
    #
    # Filter out unsupported action in current mode
    if mode == MODE_WEBPAGE:
        item_list = [item_show, item_mute, item_reload, item_lucky, item_quit]
    elif mode == MODE_VIDEO:
        item_list = [
            item_show, item_mute, item_pause, item_reload, item_lucky,
            Gtk.SeparatorMenuItem(), item_delete, Gtk.SeparatorMenuItem(), item_quit,
        ]
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
