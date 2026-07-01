import sys
import logging
import pathlib

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk
try:
    gi.require_version("WebKit2", "4.1")
    from gi.repository import WebKit2
except (ImportError, ValueError):
    WebKit2 = None

try:
    import os
    sys.path.insert(1, os.path.join(sys.path[0], '..'))
    from player.base_player import BasePlayer
    from ipc import publish_service
    from menu import build_menu
    from commons import *
    from utils import ConfigUtil
except (ModuleNotFoundError, ImportError):
    from wallblazer.player.base_player import BasePlayer
    from wallblazer.ipc import publish_service
    from wallblazer.menu import build_menu
    from wallblazer.commons import *
    from wallblazer.utils import ConfigUtil

logger = logging.getLogger(LOGGER_NAME)


class WebWindow(Gtk.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super(WebWindow, self).__init__(*args, **kwargs)
        if WebKit2 is not None:
            self.__webview = WebKit2.WebView()
        else:
            self.__webview = Gtk.Label(
                label="Web wallpaper support is unavailable in this build."
            )
        self.add(self.__webview)
        self.__webview.show()

        self.menu = None
        self.__webview.connect("button-press-event", self._on_button_press_event)

    def load_uri(self, uri):
        if WebKit2 is not None:
            self.__webview.load_uri(uri)

    def set_is_mute(self, is_mute):
        if WebKit2 is not None:
            self.__webview.set_is_muted(is_mute)

    def reload(self):
        if WebKit2 is not None:
            self.__webview.reload()

    def _on_button_press_event(self, widget, event):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            if not self.menu:
                self.menu = build_menu(MODE_WEBPAGE)
            self.menu.popup_at_pointer()
            return True
        return False


class WebPlayer(BasePlayer):
    """
    <node>
    <interface name='io.github.wallblazer.wallblazer.player'>
        <property name="mode" type="s" access="read"/>
        <property name="data_source" type="a{ss}" access="readwrite"/>
        <property name="volume" type="i" access="readwrite"/>
        <property name="is_mute" type="b" access="readwrite"/>
        <property name="is_playing" type="b" access="read"/>
        <method name='reload_config'/>
        <method name='pause_playback'/>
        <method name='start_playback'/>
        <method name='quit_player'/>
    </interface>
    </node>
    """

    def __init__(self, *args, **kwargs):
        super(WebPlayer, self).__init__(*args, **kwargs)
        self.config = None
        self.reload_config()

    def new_window(self, gdk_monitor):
        return WebWindow(application=self)

    def do_activate(self):
        super().do_activate()
        self.data_source = self.config[CONFIG_KEY_DATA_SOURCE]['Default']

    @property
    def mode(self):
        return self.config[CONFIG_KEY_MODE]

    @property
    def data_source(self):
        return self.config[CONFIG_KEY_DATA_SOURCE]

    @data_source.setter
    def data_source(self, data_source: str):
        self.config[CONFIG_KEY_DATA_SOURCE]['Default'] = data_source
        if self.mode != MODE_WEBPAGE:
            raise ValueError("Invalid mode")

        # Convert to uri if necessary
        if not data_source.startswith("http://") and \
                not data_source.startswith("https://") and not data_source.startswith("file://"):
            data_source = pathlib.Path(data_source).resolve().as_uri()

        for monitor, window in self.windows.items():
            window.load_uri(data_source)
            if not monitor.is_primary():
                window.set_is_mute(True)
        self.volume = self.config[CONFIG_KEY_VOLUME]
        self.is_mute = self.config[CONFIG_KEY_MUTE]

    @property
    def volume(self):
        return self.config[CONFIG_KEY_VOLUME]

    @volume.setter
    def volume(self, volume):
        # TODO: How to set volume of webview?
        self.config[CONFIG_KEY_VOLUME] = volume

    @property
    def is_mute(self):
        return self.config[CONFIG_KEY_MUTE]

    @is_mute.setter
    def is_mute(self, is_mute):
        self.config[CONFIG_KEY_MUTE] = is_mute
        for monitor, window in self.windows.items():
            if monitor.is_primary():
                window.set_is_mute(is_mute)

    @property
    def is_playing(self):
        return True

    def pause_playback(self):
        pass

    def start_playback(self):
        pass

    def reload_config(self):
        self.config = ConfigUtil().load()


def main():
    logging.basicConfig(level=logging.INFO)
    app = WebPlayer()
    try:
        service_handle = publish_service(DBUS_NAME_PLAYER, app)
    except Exception as e:
        logger.error(e)
        service_handle = None
    app.run(sys.argv)


if __name__ == "__main__":
    main()
