"""
A battery address that does not answer during start-up must be tried again (review 2026-10-21).

Runs the real main() of dbus-serialbattery.py for a serial port with BATTERY_ADDRESSES = 0x00, 0x01, under a simulated
GLib main loop: every GLib.timeout_add / timeout_add_seconds that main() registers fires on a simulated clock until the
loop is quit or an hour has passed. Faked: the detection result per address (Seplos.test_connection), sleep(), the port
checks, the BMS detection cache file, and DbusHelper (records which addresses get a D-Bus service and are polled).

Before the fix the missing address was never probed again and the driver never exited, so the bank ran on one pack's
service until a manual restart (dbus-aggregate-batteries needs both and keeps exiting meanwhile).
"""

import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

from bms.seplos import Seplos


@pytest.fixture
def driver():
    glib = sys.modules["dbus.mainloop.glib"]
    if not hasattr(glib, "DBusGMainLoop"):
        glib.DBusGMainLoop = MagicMock()
    path = pathlib.Path(__file__).resolve().parent.parent / "dbus-serialbattery" / "dbus-serialbattery.py"
    spec = importlib.util.spec_from_file_location("dbus_serialbattery_main_startup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SimulatedGLib:
    """timeout_add(ms, cb) / timeout_add_seconds(s, cb) on a simulated clock; MainLoop().run() advances it."""

    def __init__(self, seconds):
        self.now = 0.0
        self.end = seconds
        self.timers = []  # [next_due, interval_s, callback]
        self.quit_at = None
        self.on_tick = []

    def timeout_add(self, ms, callback, *args):
        self.timers.append([self.now + ms / 1000, ms / 1000, lambda: callback(*args)])
        return len(self.timers)

    def timeout_add_seconds(self, seconds, callback, *args):
        return self.timeout_add(seconds * 1000, callback, *args)

    def MainLoop(self):
        sim = self

        class Loop:
            def run(self):
                while sim.quit_at is None and sim.timers:
                    timer = min(sim.timers, key=lambda t: t[0])
                    if timer[0] > sim.end:
                        break
                    sim.now = timer[0]
                    for hook in sim.on_tick:
                        hook(sim.now)
                    if timer[2]():
                        timer[0] += timer[1]
                    else:
                        sim.timers.remove(timer)

            def quit(self):
                sim.quit_at = sim.now

        return Loop()


def test_an_address_missing_at_start_up_is_retried_and_picked_up(driver, monkeypatch):
    answering = {b"\x00"}  # pack 0x01 is silent during start-up and for the first 10 minutes
    probes = []  # (simulated time, address)
    sim = SimulatedGLib(3600)

    def test_connection(self):
        probes.append((sim.now, self.address))
        return self.address in answering

    monkeypatch.setattr(Seplos, "test_connection", test_connection)
    monkeypatch.setattr(Seplos, "validate_data", lambda self: True)
    monkeypatch.setattr(Seplos, "log_settings", lambda self: None)
    monkeypatch.setattr(Seplos, "set_calculated_data", lambda self: None)
    sim.on_tick.append(lambda now: answering.add(b"\x01") if now >= 600 else None)

    served, polled = [], []

    class FakeDbusHelper:
        def __init__(self, battery, address=None):
            self.battery, self.address = battery, address
            served.append(address)

        def setup_vedbus(self):
            return True

        def publish_battery(self, loop):
            polled.append((sim.now, self.address))
            self.battery.last_refresh_duration = 0.1

    monkeypatch.setattr(driver, "DbusHelper", FakeDbusHelper)
    monkeypatch.setattr(driver, "BATTERY_ADDRESSES", ["0x00", "0x01"])
    monkeypatch.setattr(driver, "BMS_TYPE", ["Seplos"])
    monkeypatch.setattr(driver, "expected_bms_types", [{"bms": Seplos, "baud": 19200}])
    monkeypatch.setattr(driver, "POLL_INTERVAL", None)
    monkeypatch.setattr(driver, "sleep", lambda s: None)
    monkeypatch.setattr(driver, "load_bms_detection_cache", lambda: {})
    monkeypatch.setattr(driver, "save_bms_detection_cache", lambda cache: None)
    monkeypatch.setattr(driver, "validate_config_values", lambda: True)
    monkeypatch.setattr(driver, "get_venus_os_version", lambda: "v3.81")
    monkeypatch.setattr(driver, "get_venus_os_image_type", lambda: "normal")
    monkeypatch.setattr(driver, "get_venus_os_device_type", lambda: "Cerbo GX")
    monkeypatch.setattr(driver.os.path, "exists", lambda p: True)
    monkeypatch.setattr(driver.os, "access", lambda p, mode: True)
    monkeypatch.setattr(driver.signal, "signal", lambda *a: None)
    monkeypatch.setattr(driver.sys, "argv", ["dbus-serialbattery.py", "/dev/ttyUSB1"])
    monkeypatch.setattr(driver.GLib, "timeout_add", sim.timeout_add)
    monkeypatch.setattr(driver.GLib, "timeout_add_seconds", sim.timeout_add_seconds)
    monkeypatch.setattr(driver.GLib, "MainLoop", sim.MainLoop)

    driver.main()

    assert served == ["0x00"], served  # start-up found only pack 0x00, as in the trigger
    retries = [t for t, address in probes if address == b"\x01" and t > 0]
    assert sim.quit_at is not None, (
        f"0x01 answered again at 600 s but after an hour only {sorted({a for _, a in polled})} is served "
        f"({len(retries)} retries of 0x01) and the driver did not exit to restart with both"
    )
    # picked up after three retries in a row answered (test_startup_flaky_battery.py), by quitting the loop so the
    # service restarts the driver
    assert 600 <= sim.quit_at <= 600 + 3 * 60 + 5
    # the healthy pack kept being polled meanwhile, and the retries were single attempts, not a full re-detection
    assert len([t for t, a in polled if a == "0x00" and 0 < t < 600]) >= 100
    assert len([t for t in retries if t < 600]) <= 600 / 60 + 1
