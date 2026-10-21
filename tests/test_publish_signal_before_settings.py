"""
A poll's charge-control paths reach D-Bus before publish_dbus makes a blocking call to com.victronenergy.settings
(review of a67f667..4b9283c, 2026-10-21).

publish_dbus batched the whole body, so the ItemsChanged carrying /Info/MaxChargeVoltage, the limits, AllowTo* and
/Connected went out only after the tail's synchronous localsettings calls: the TimeToGo GetValue reads (once a minute),
save_current_battery_state() (SetValue every 15 s) and the daily LastSeen SetValue. On base a67f667 each write emitted
its PropertiesChanged at once (vedbus.py:172-173 -> local_set_value :533-536), i.e. before those calls. A dbus-python
method call blocks the calling thread until the reply or the libdbus default timeout (25 s), so a slow localsettings held
back the safety values. With JK BLE (BLUETOOTH_USE_POLLING = False) the proxy's lock was held for the same calls, so the
GLib loop's health check waited for them too.

The real Seplos detection and DbusHelper.setup_vedbus/publish_battery/publish_dbus/save_current_battery_state/
set_settings/get_setting_value run. Faked: serial transport, clock, VeDbusService (FakeVeDbusService, which mirrors
vedbus.py:172-212) and the settings bus, whose every method call records the signals emitted so far and whether another
thread could take the proxy's lock at that moment.
"""

import threading
from unittest.mock import MagicMock

import pytest

import battery as battery_module
import dbus
import dbushelper
import utils
from bms.seplos import Seplos
from dbushelper import VICTRON_SETTINGS_DBUS_NAME, DbusHelper, _CachedDbusProxy
from test_publish_items_changed import CELLS, OUR_CONFIG, POLL, Clock, FakeVeDbusService, Link

CVL = "/Info/MaxChargeVoltage"
TRIGGERS = ["save_current_battery_state", "TimeToGo", "LastSeen"]


def lock_is_free(lock):
    """Whether another thread can take `lock` right now (the GLib loop's health check would wait otherwise)."""
    got = []

    def take():
        got.append(lock.acquire(blocking=False))
        if got[0]:
            lock.release()

    t = threading.Thread(target=take)
    t.start()
    t.join()
    return got[0]


class SettingsBus:
    """com.victronenergy.settings: every method call (a blocking python-dbus call in the driver) records what was signalled
    before it and whether the proxy's lock was free."""

    def __init__(self, svc, proxy):
        self.svc = svc
        self.proxy = proxy
        self.calls = []  # (member, path, signals emitted before the call, proxy lock free)

    def get_object(self, service, object_path, introspect=True, follow_name_owner_changes=False):
        assert service == VICTRON_SETTINGS_DBUS_NAME
        bus = self

        class Proxy:
            def get_dbus_method(self, member, dbus_interface=None):
                def call(*args):
                    bus.calls.append((member, object_path, list(bus.svc.signals), lock_is_free(bus.proxy._lock)))
                    return dbus.Int32(1) if member == "GetValue" else 0

                return call

        return Proxy()


def signalled_values(signals, path):
    out = []
    for s in signals:
        if s[0] == "PropertiesChanged" and s[1] == path:
            out.append(s[2]["Value"])
        elif s[0] == "ItemsChanged" and path in s[1]:
            out.append(s[1][path]["Value"])
    return out


@pytest.fixture
def poll(monkeypatch):
    """A pack set up as in main(); run(trigger) does one poll that changes the CVL 55.2 -> 55.05 V and reaches one
    settings call of the publish_dbus tail."""
    clock = Clock(1_000_000)
    monkeypatch.setattr(battery_module, "time", clock)
    monkeypatch.setattr(dbushelper, "time", clock)
    for name, value in OUR_CONFIG.items():
        assert hasattr(utils, name), name
        monkeypatch.setattr(utils, name, value)
    link = Link()
    monkeypatch.setattr(Seplos, "read_serial_data_seplos", link)

    bat = Seplos("/dev/ttyUSB1", 19200, b"\x00")
    link.answer([3400] * CELLS)
    assert bat.test_connection() is True

    helper = DbusHelper(bat)
    svc = FakeVeDbusService()
    helper._dbusservice = _CachedDbusProxy(svc)
    helper.settings = MagicMock()
    helper.settings.__getitem__.side_effect = lambda key: ""
    monkeypatch.setattr(helper, "setup_instance", lambda: True)
    helper.path_battery = "/Settings/Devices/serialbattery_TEST"  # setup_instance (stubbed) sets it
    assert helper.setup_vedbus() is True
    bat.set_calculated_data()
    assert svc.signals == []

    settings = SettingsBus(svc, helper._dbusservice)
    monkeypatch.setattr(dbushelper, "get_bus", lambda name: settings if name == VICTRON_SETTINGS_DBUS_NAME else MagicMock())

    def run(trigger):
        # exactly one settings tail path is due in this poll
        helper.settings_saved_last_time = 0 if trigger == "save_current_battery_state" else clock.t
        helper.last_seen_saved_last_time = 0 if trigger == "LastSeen" else clock.t
        monkeypatch.setattr(utils, "TIME_TO_GO_ENABLE", trigger == "TimeToGo")
        link.answer([3470] + [3440] * 15)  # one cell 20 mV over: CVL 55.2 -> 55.05 V
        clock.t += POLL
        loop = MagicMock()
        helper.publish_battery(loop)
        loop.quit.assert_not_called()
        assert svc[CVL] == pytest.approx(55.05)
        assert pytest.approx(55.05) in signalled_values(svc.signals, CVL), "the poll never signalled its CVL"
        assert settings.calls, "the poll made no settings call"
        return settings.calls[0]

    return run


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_charge_voltage_is_signalled_before_the_blocking_settings_call(poll, trigger):
    member, path, before, _ = poll(trigger)

    assert pytest.approx(55.05) in signalled_values(before, CVL), (
        f"publish_dbus called {member} on {path} (blocking python-dbus call to localsettings) before signalling this "
        f"poll's {CVL}=55.05; signals emitted by then: {[s[0] for s in before]}"
    )


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_the_proxy_lock_is_free_during_the_blocking_settings_call(poll, trigger):
    member, path, _, lock_free = poll(trigger)

    assert lock_free, f"publish_dbus held the proxy's lock across {member} on {path}: another publisher waits for localsettings"
