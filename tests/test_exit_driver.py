"""The SIGTERM/SIGINT handler must stop the main loop even if logging fails.

Python runs signal handlers between bytecodes, so the handler can interrupt a write to stderr. Its logger call then
raises "RuntimeError: reentrant call inside <_io.BufferedWriter name='<stderr>'>" in the interrupted frame. When the
handler logged before quitting the main loop, that error skipped mainloop.quit() and sys.exit(): the driver stayed
alive (seen on a Cerbo GX, 2026-10-19) instead of exiting so that the service restarts it.
"""

import importlib.util
import pathlib
import signal
import sys
from unittest.mock import MagicMock

import pytest

REENTRANT = "reentrant call inside <_io.BufferedWriter name='<stderr>'>"


@pytest.fixture(scope="module")
def driver():
    glib = sys.modules["dbus.mainloop.glib"]
    if not hasattr(glib, "DBusGMainLoop"):
        glib.DBusGMainLoop = MagicMock()
    path = pathlib.Path(__file__).resolve().parent.parent / "dbus-serialbattery" / "dbus-serialbattery.py"
    spec = importlib.util.spec_from_file_location("dbus_serialbattery_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loop_is_quit_even_if_logging_raises(driver):
    def interrupted_log(message):
        raise RuntimeError(REENTRANT)

    loop = MagicMock()
    driver.quit_mainloop_then_log(loop, interrupted_log, "Exit signal received, exiting gracefully...")
    loop.quit.assert_called_once()


def test_loop_is_quit_before_logging(driver):
    order = []
    loop = MagicMock()
    loop.quit.side_effect = lambda: order.append("quit")
    driver.quit_mainloop_then_log(loop, order.append, "Exit signal received, exiting gracefully...")
    assert order == ["quit", "Exit signal received, exiting gracefully..."]


def test_without_a_main_loop_it_still_logs(driver):
    messages = []
    driver.quit_mainloop_then_log(None, messages.append, "Exit signal received, exiting gracefully...")
    assert messages == ["Exit signal received, exiting gracefully..."]


class _StopMain(Exception):
    pass


@pytest.fixture
def exit_driver(driver, monkeypatch):
    """The real SIGTERM handler. main() registers it, then defines the helpers it uses (get_port), then logs the
    Venus OS version: stop main() there and keep the handler."""
    handlers = {}

    def stop_main():
        raise _StopMain

    monkeypatch.setattr(driver.signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(driver, "get_venus_os_version", stop_main)
    with pytest.raises(_StopMain):
        driver.main()
    return handlers[signal.SIGTERM]


def test_sigterm_quits_the_loop_and_exits_even_if_logging_raises(driver, exit_driver, monkeypatch):
    """The 2026-10-19 incident: SIGTERM arrives while stderr is being written, so every log call raises."""

    def interrupted_log(message):
        raise RuntimeError(REENTRANT)

    loop = MagicMock()
    monkeypatch.setattr(driver, "mainloop", loop)
    monkeypatch.setattr(driver.sys, "argv", ["dbus-serialbattery.py", "ttyUSB1"])
    monkeypatch.setattr(driver.logger, "info", interrupted_log)
    with pytest.raises(SystemExit) as exited:
        exit_driver(signal.SIGTERM, None)
    assert exited.value.code == 0
    loop.quit.assert_called_once()


def test_sigterm_stops_the_can_receiver_thread(driver, exit_driver, monkeypatch):
    can_thread = MagicMock()
    monkeypatch.setattr(driver, "can_thread", can_thread, raising=False)
    monkeypatch.setattr(driver.sys, "argv", ["dbus-serialbattery.py", "can0"])
    with pytest.raises(SystemExit):
        exit_driver(signal.SIGTERM, None)
    can_thread.stop.assert_called_once()


@pytest.mark.parametrize("name", ["mainloop", "can_thread"])
def test_main_stores_what_the_signal_handler_reads_in_the_module_globals(driver, name):
    """exit_driver reads these from the module globals; main() must assign them there, not to a local."""
    assert hasattr(driver, name) and getattr(driver, name) is None
    code = driver.main.__code__
    assert name in code.co_names  # assigned with a `global` declaration
    assert name not in code.co_varnames
    assert name not in code.co_cellvars  # a local captured by the nested handler and callbacks
