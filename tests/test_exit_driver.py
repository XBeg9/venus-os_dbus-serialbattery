"""The SIGTERM/SIGINT handler must stop the main loop even if logging fails.

Python runs signal handlers between bytecodes, so the handler can interrupt a write to stderr. Its logger call then
raises "RuntimeError: reentrant call inside <_io.BufferedWriter name='<stderr>'>" in the interrupted frame. When the
handler logged before quitting the main loop, that error skipped mainloop.quit() and sys.exit(): the driver stayed
alive (seen on a Cerbo GX, 2026-10-19) instead of exiting so that the service restarts it.
"""

import importlib.util
import pathlib
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


def test_main_stores_the_main_loop_where_the_signal_handler_reads_it(driver):
    """exit_driver reads the loop from the module globals; main() must assign it there, not to a local."""
    assert hasattr(driver, "mainloop") and driver.mainloop is None
    assert "mainloop" in driver.main.__code__.co_names  # assigned with a `global` declaration
    assert "mainloop" not in driver.main.__code__.co_varnames
