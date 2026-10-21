"""Shared test fixtures and module stubs for platforms without D-Bus.

dbushelper.py (and its transitive imports) depend on Linux-only C extensions
(``dbus``, ``gi``) and Victron's ``velib_python`` helpers that are only
available on Venus OS.  This conftest injects lightweight stubs *before*
any test module triggers the real import chain, so the test suite works on
macOS, Windows, and headless CI runners.
"""

import sys
import types
from unittest.mock import MagicMock


def _stub(name, **attrs):
    """Register a stub module if the real one isn't installed."""
    if name not in sys.modules:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
    return sys.modules[name]


_BusConnection = type(
    "BusConnection",
    (),
    {
        "TYPE_SYSTEM": 0,
        "TYPE_SESSION": 1,
        "__new__": lambda cls, *a, **kw: object.__new__(cls),
        # dbushelper.get_bus() checks this; stub must not raise AttributeError in tests.
        "get_is_connected": lambda self: True,
    },
)


class _DBusException(Exception):
    """dbus.exceptions.DBusException: carries the D-Bus error name that callers classify on."""

    def __init__(self, *args, name=None):
        super().__init__(*args)
        self._dbus_error_name = name

    def get_dbus_name(self):
        return self._dbus_error_name


class _Interface:
    """dbus.Interface: binds a proxy object to one interface; calls go through the proxy's get_dbus_method()."""

    def __init__(self, obj, dbus_interface):
        self.proxy_object = obj
        self.dbus_interface = dbus_interface

    def get_dbus_method(self, member, dbus_interface=None):
        return self.proxy_object.get_dbus_method(member, dbus_interface or self.dbus_interface)

    def __getattr__(self, member):
        if member.startswith("__"):
            raise AttributeError(member)
        return self.get_dbus_method(member)


_dbus_bus = _stub("dbus.bus", BusConnection=_BusConnection)
_dbus_svc = _stub("dbus.service")
_dbus_exceptions = _stub("dbus.exceptions", DBusException=_DBusException)
_dbus = _stub(
    "dbus",
    bus=_dbus_bus,
    service=_dbus_svc,
    exceptions=_dbus_exceptions,
    Interface=_Interface,
    # dbus-python's value types subclass the Python ones
    Dictionary=type("Dictionary", (dict,), {}),
    Int32=type("Int32", (int,), {}),
    Double=type("Double", (float,), {}),
)
_stub("dbus.mainloop")
_stub("dbus.mainloop.glib")

_gi_repo = _stub("gi.repository", GLib=MagicMock())
_stub("gi", repository=_gi_repo)

_stub("requests")

try:
    import serial  # noqa: F401
except ImportError:
    _stub(
        "serial",
        Serial=MagicMock,
        SerialException=type("SerialException", (IOError,), {}),
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )
    _stub("serial.tools")
    _stub("serial.tools.list_ports", comports=lambda: [])
_stub("vedbus", VeDbusService=MagicMock)
_stub("ve_utils", get_vrm_portal_id=lambda: "stub")
_stub("settingsdevice", SettingsDevice=MagicMock)
