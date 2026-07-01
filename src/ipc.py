import os
import sys
import threading
import xmlrpc.client
from socketserver import ThreadingMixIn
from xmlrpc.server import SimpleXMLRPCServer

try:
    from pydbus import SessionBus as _DBusSessionBus
except ImportError:
    _DBusSessionBus = None

try:
    from commons import DBUS_NAME_PLAYER, DBUS_NAME_SERVER
except (ModuleNotFoundError, ImportError):
    from wallblazer.commons import DBUS_NAME_PLAYER, DBUS_NAME_SERVER


_LOCAL_IPC_PORTS = {
    DBUS_NAME_SERVER: 38461,
    DBUS_NAME_PLAYER: 38462,
}


def _use_local_ipc():
    forced = str(os.environ.get("WALLBLAZER_LOCAL_IPC", "")).strip().lower()
    if forced in {"1", "true", "yes", "on"}:
        return True
    return sys.platform == "win32" or _DBusSessionBus is None


class _ThreadedXmlRpcServer(ThreadingMixIn, SimpleXMLRPCServer):
    allow_reuse_address = True
    daemon_threads = True


def _describe_object(obj):
    properties = set()
    for cls in type(obj).mro():
        for name, member in getattr(cls, "__dict__", {}).items():
            if name.startswith("_"):
                continue
            if isinstance(member, property):
                properties.add(name)

    methods = []
    for name in dir(obj):
        if name.startswith("_") or name in properties:
            continue
        try:
            attr = getattr(obj, name)
        except Exception:
            continue
        if callable(attr):
            methods.append(name)
        else:
            properties.add(name)

    return {
        "methods": sorted(set(methods)),
        "properties": sorted(properties),
    }


class _XmlRpcDispatcher:
    def __init__(self, obj):
        self._obj = obj
        self._description = _describe_object(obj)

    def ping(self):
        return True

    def describe(self):
        return dict(self._description)

    def call_method(self, name, args):
        method = getattr(self._obj, name)
        return method(*tuple(args or ()))

    def get_property(self, name):
        return getattr(self._obj, name)

    def set_property(self, name, value):
        setattr(self._obj, name, value)
        return True


class LocalServiceHandle:
    def __init__(self, name, obj):
        port = _LOCAL_IPC_PORTS[name]
        self._server = _ThreadedXmlRpcServer(
            ("127.0.0.1", port),
            allow_none=True,
            logRequests=False,
        )
        self._server.register_instance(_XmlRpcDispatcher(obj))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()


class LocalProxy:
    def __init__(self, name):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_client", xmlrpc.client.ServerProxy(
            f"http://127.0.0.1:{_LOCAL_IPC_PORTS[name]}",
            allow_none=True,
        ))
        object.__setattr__(self, "_description", None)

    def _get_description(self, refresh=False):
        description = object.__getattribute__(self, "_description")
        if description is not None and not refresh:
            return description
        description = object.__getattribute__(self, "_client").describe()
        object.__setattr__(self, "_description", description)
        return description

    def __getattr__(self, name):
        description = self._get_description()
        properties = set(description.get("properties", []))
        methods = set(description.get("methods", []))

        if name in properties:
            return object.__getattribute__(self, "_client").get_property(name)
        if name in methods:
            def _caller(*args):
                return object.__getattribute__(self, "_client").call_method(name, list(args))
            return _caller

        description = self._get_description(refresh=True)
        if name in set(description.get("properties", [])):
            return object.__getattribute__(self, "_client").get_property(name)
        if name in set(description.get("methods", [])):
            def _caller(*args):
                return object.__getattribute__(self, "_client").call_method(name, list(args))
            return _caller
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        description = self._get_description()
        if name not in set(description.get("properties", [])):
            raise AttributeError(name)
        object.__getattribute__(self, "_client").set_property(name, value)


def publish_service(name, obj):
    if not _use_local_ipc():
        try:
            bus = _DBusSessionBus()
            return bus.publish(name, obj)
        except Exception:
            pass
    return LocalServiceHandle(name, obj)


def get_service(name):
    if not _use_local_ipc():
        try:
            # First try DBus
            obj = _DBusSessionBus().get(name)
            if obj is not None:
                return obj
        except Exception:
            pass
    try:
        proxy = LocalProxy(name)
        proxy._client.ping()
        return proxy
    except Exception:
        return None
