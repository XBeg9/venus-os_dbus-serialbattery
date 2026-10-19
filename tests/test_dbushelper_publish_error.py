"""publish_battery must stop the main loop on an unexpected error, even if reporting the error fails.

Seen on a Cerbo GX (2026-10-19): the USB-RS485 adapter dropped, refresh_data raised SerialException, and while
publish_battery's except block was printing the traceback, serial-starter's SIGTERM arrived. The signal handler logged
to the same stderr, Python raised "RuntimeError: reentrant call inside <_io.BufferedWriter name='<stderr>'>" out of
traceback.print_exc(), and loop.quit() after it never ran. The exception left the GLib timeout callback, GLib dropped
the poll source, and the driver stayed alive with frozen values on D-Bus, blocking every restart.
"""

from unittest.mock import MagicMock

import dbushelper
from dbushelper import DbusHelper


def _helper_whose_refresh_fails():
    helper = DbusHelper.__new__(DbusHelper)
    helper.battery = MagicMock()
    helper.battery.refresh_data.side_effect = OSError("device reports readiness to read but returned no data")
    return helper


def test_unexpected_error_quits_the_loop():
    loop = MagicMock()
    _helper_whose_refresh_fails().publish_battery(loop)
    loop.quit.assert_called_once()


def test_loop_quits_even_if_printing_the_traceback_raises(monkeypatch):
    def interrupted_print_exc(*args, **kwargs):
        raise RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>")

    monkeypatch.setattr(dbushelper.traceback, "print_exc", interrupted_print_exc)
    loop = MagicMock()

    _helper_whose_refresh_fails().publish_battery(loop)  # must not raise into the GLib callback

    loop.quit.assert_called_once()
