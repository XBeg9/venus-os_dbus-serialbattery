"""
Garbled bytes on the RS485 bus must be a failed Seplos read, not an exception (review 2026-10-21).

The serial read returns whatever bytes arrived: line noise, a frame cut by a collision, non-ASCII. The frame check
decoded the checksum field with bytes.fromhex(...decode("ascii")) unguarded, so such a line raised UnicodeDecodeError or
ValueError out of read_status_data, through refresh_data, into publish_battery's catch-all, which quits the main loop:
the driver exited and both pack services left D-Bus until serial-starter restarted it. read_alarm_data already treated
undecodable data as a failed read; now every read does, and publish_battery counts it like any other failed read.
"""

import os
from unittest.mock import MagicMock

import pytest

import battery as battery_module
import dbushelper
import utils
from battery import Cell
from bms import seplos as seplos_module
from bms.seplos import Seplos
from dbushelper import DbusHelper, _CachedDbusProxy

CELLS = 16


def status_info(cells_mv):
    """150-char info of a 0x42 reply, offsets from Seplos.decode_status_data."""
    s = "0000" + "%02X" % len(cells_mv)
    s += "".join("%04X" % mv for mv in cells_mv)
    s += "06" + "".join("%04X" % 2981 for _ in range(6))
    s += "%04X" % 0 + "%04X" % round(sum(cells_mv) / 10) + "%04X" % 20000 + "0A" + "%04X" % 28000 + "%04X" % 900 + "0000" + "%04X" % 12
    return s.ljust(150, "0").encode("ascii")


def reply_frame(address: int, info: bytes) -> bytes:
    """~ VER ADR CID1 RTN LENGTH INFO CHKSUM CR, with a valid checksum over whatever info holds."""
    body = "20{:02X}4600{:04X}".format(address, Seplos.get_info_length(info)).encode() + info
    return b"~" + body + "{:04X}".format(Seplos.get_checksum(body)).encode() + b"\r"


class OneReplySerial:
    """
    Stands in for serial.Serial as read_serial_data_seplos uses it: context manager, flush*, write, fileno.

    The port is a pipe, and the whole reply is in it once the command is written. The write end stays open until the
    port is closed, so a reply without CR waits out the read deadline like a quiet port instead of reading EOF.
    """

    reply = b""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        self.r, self.w = os.pipe()
        return self

    def __exit__(self, *exc):
        os.close(self.r)
        os.close(self.w)
        return False

    def flushOutput(self):
        pass

    def flushInput(self):
        pass

    def write(self, data):
        os.write(self.w, type(self).reply)
        return len(data)

    def fileno(self):
        return self.r


@pytest.fixture
def one_reply(monkeypatch):
    monkeypatch.setattr(seplos_module.serial, "Serial", OneReplySerial)
    return OneReplySerial


def make_seplos():
    b = Seplos("/dev/ttyUSB1", 19200, b"\x00")
    b.cell_count = CELLS
    b.cells = [Cell(False) for _ in range(CELLS)]
    return b


NOISE = b"\xff\xfe" * 12 + b"\r"


def test_a_valid_frame_is_still_read(one_reply):
    one_reply.reply = reply_frame(0x00, status_info([3300] * 15 + [3333]))
    b = make_seplos()
    assert b.read_status_data() is True
    assert b.cells[-1].voltage == pytest.approx(3.333)


def test_noise_on_the_bus_is_a_failed_read(one_reply):
    one_reply.reply = NOISE
    assert make_seplos().read_status_data() is False


def test_a_non_hex_checksum_is_a_failed_read(one_reply):
    one_reply.reply = reply_frame(0x00, status_info([3300] * CELLS))[:-5] + b"ZZZZ\r"
    assert make_seplos().read_status_data() is False


def test_a_non_hex_payload_with_a_valid_checksum_is_a_failed_read(one_reply):
    info = bytearray(status_info([3300] * CELLS))
    info[6:10] = b"ZZZZ"  # cell 1 voltage
    one_reply.reply = reply_frame(0x00, bytes(info))
    b = make_seplos()
    assert b.read_status_data() is False
    assert all(c.voltage is None for c in b.cells)  # nothing half-decoded was kept


@pytest.fixture
def helper_env(monkeypatch):
    t = [1_000_000]
    monkeypatch.setattr(battery_module, "time", lambda: t[0])
    monkeypatch.setattr(dbushelper, "time", lambda: t[0])
    for name, value in {
        "MAX_CELL_VOLTAGE": 3.45,
        "SOC_RESET_AFTER_DAYS": False,
        "SOC_CALCULATION": False,
        "GUI_PARAMETERS_SHOW_ADDITIONAL_INFO": False,
        "TELEMETRY": False,
        "PUBLISH_BATTERY_DATA_AS_JSON": False,
        "HISTORY_ENABLE": False,
        "CAPTURE_RAW_DATA": False,
        "EXTERNAL_SENSOR_DBUS_DEVICE": None,
    }.items():
        assert hasattr(utils, name), name
        monkeypatch.setattr(utils, name, value)
    helper = DbusHelper(make_seplos())
    helper._dbusservice = _CachedDbusProxy({"/Connected": 1, "/Mode": 3})
    monkeypatch.setattr(helper, "save_current_battery_state", lambda: None)
    helper.last_seen_saved_last_time = t[0]
    return helper, MagicMock()


def test_one_garbled_line_is_counted_as_a_failed_read_and_the_driver_keeps_running(one_reply, helper_env):
    helper, loop = helper_env
    one_reply.reply = NOISE
    helper.publish_battery(loop)
    loop.quit.assert_not_called()
    assert helper.error["count"] == 1
