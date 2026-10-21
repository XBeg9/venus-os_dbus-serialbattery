"""
A battery that answers only now and then must not make the driver restart over and over (review of cerbo-2026-10-21.2).

retry_missing_batteries() quit the main loop after one good connection test of a missing address, so the service
restarts the driver to set up all batteries. Each restart takes every pack service off D-Bus (start-up sleeps 16 s and
probes each address again). A pack on a marginal link that answers one probe in ten is usually missed again at
start-up, answers a retry a few minutes later, and the cycle repeats: the healthy pack's service drops out every few
minutes, while before the retry it never did.

Runs the real main() of dbus-serialbattery.py again after each exit, as daemontools supervise does, on one simulated
clock: start-up sleep 16 s, each connection test 1 s (a Seplos read timeout), supervise's 1 s restart pause.
"""

import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

from bms.seplos import Seplos

POLL = 5


@pytest.fixture
def driver():
    glib = sys.modules["dbus.mainloop.glib"]
    if not hasattr(glib, "DBusGMainLoop"):
        glib.DBusGMainLoop = MagicMock()
    path = pathlib.Path(__file__).resolve().parent.parent / "dbus-serialbattery" / "dbus-serialbattery.py"
    spec = importlib.util.spec_from_file_location("dbus_serialbattery_main_flaky", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Sim:
    """One clock across driver runs; per run, GLib timeouts fire on it until the loop is quit or time is up."""

    def __init__(self, end):
        self.now = 0.0
        self.end = end
        self.timers = []
        self.quit = False

    def timeout_add(self, ms, callback, *args):
        self.timers.append([self.now + ms / 1000, ms / 1000, lambda: callback(*args)])
        return len(self.timers)

    def timeout_add_seconds(self, seconds, callback, *args):
        return self.timeout_add(seconds * 1000, callback, *args)

    def MainLoop(self):
        sim = self

        class Loop:
            def run(self):
                while not sim.quit and sim.timers:
                    timer = min(sim.timers, key=lambda t: t[0])
                    if timer[0] > sim.end:
                        sim.now = sim.end
                        break
                    sim.now = max(sim.now, timer[0])
                    if timer[2]():
                        timer[0] = sim.now + timer[1]
                    else:
                        sim.timers.remove(timer)

            def quit(self):
                sim.quit = True

        return Loop()


def run_driver_for(driver, monkeypatch, seconds, answers):
    """Run main() again after each exit until `seconds` of simulated time; answers(address, n) decides each probe."""
    sim = Sim(seconds)
    probes = {}
    polls = []  # (time, address)

    def test_connection(self):
        n = probes.get(self.address, 0)
        probes[self.address] = n + 1
        sim.now += 1  # one serial read, or its 1 s timeout
        return answers(self.address, n)

    class FakeDbusHelper:
        def __init__(self, battery, address=None):
            self.battery, self.address = battery, address

        def setup_vedbus(self):
            return True

        def publish_battery(self, loop):
            polls.append((sim.now, self.address))
            self.battery.last_refresh_duration = 0.1

    def sleep(s):
        sim.now += s

    monkeypatch.setattr(Seplos, "test_connection", test_connection)
    monkeypatch.setattr(Seplos, "validate_data", lambda self: True)
    monkeypatch.setattr(Seplos, "log_settings", lambda self: None)
    monkeypatch.setattr(Seplos, "set_calculated_data", lambda self: None)
    monkeypatch.setattr(driver, "DbusHelper", FakeDbusHelper)
    monkeypatch.setattr(driver, "BATTERY_ADDRESSES", ["0x00", "0x01"])
    monkeypatch.setattr(driver, "BMS_TYPE", ["Seplos"])
    monkeypatch.setattr(driver, "expected_bms_types", [{"bms": Seplos, "baud": 19200}])
    monkeypatch.setattr(driver, "POLL_INTERVAL", None)
    monkeypatch.setattr(driver, "sleep", sleep)
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

    starts = 0
    while sim.now < seconds:
        starts += 1
        sim.timers, sim.quit = [], False
        driver.main()
        sim.now += 1  # supervise pauses 1 s before starting ./run again
    return starts, polls, probes


def longest_gap(polls, address, seconds):
    """Longest time without a poll of address, from its first poll (the first start-up itself is not an outage)."""
    times = [t for t, a in polls if a == address] + [seconds]
    return max(b - a for a, b in zip(times, times[1:]))


def test_a_pack_answering_one_probe_in_ten_does_not_restart_the_driver(driver, monkeypatch):
    def answers(address, n):
        return address == b"\x00" or n % 10 == 9

    starts, polls, probes = run_driver_for(driver, monkeypatch, 3600, answers)

    gap = longest_gap(polls, "0x00", 3600)
    probes_0x01 = probes.get(b"\x01", 0)
    assert starts == 1, (
        f"{starts - 1} restarts in an hour for a pack answering 1 probe in 10 "
        f"({probes_0x01} probes of 0x01); the healthy pack 0x00 went {gap:.0f} s without a poll"
    )
    assert gap <= POLL + 1


def test_a_pack_that_comes_back_for_good_is_still_picked_up(driver, monkeypatch):
    # silent for its first 13 probes (start-up and ~10 minutes of retries), answers every probe after that
    def answers(address, n):
        return address == b"\x00" or n >= 13

    starts, polls, probes = run_driver_for(driver, monkeypatch, 1800, answers)

    assert starts == 2, f"{starts} driver starts"
    assert "0x01" in {a for _, a in polls}
    first_0x01 = min(t for t, a in polls if a == "0x01")
    assert first_0x01 < 16 + 13 * 60 + 5 * 60, f"0x01 first polled at {first_0x01:.0f} s"
