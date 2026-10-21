"""
A Seplos reply is only taken from the pack it was asked of (review 2026-10-21).

Both packs share one RS485 bus. A Seplos V2 reply carries the ADR of the pack that sent it (frames captured from our
Pack-02 echo ADR 01: "~2001 46 00 ..."), but read_serial_data_seplos never compared it with the address it asked: a
valid frame from the other pack, e.g. a late reply to an earlier request, was decoded into the asked pack and published
under its service (cells, current, SoC, FET states).
"""

from unittest.mock import MagicMock

import pytest

from battery import Cell
from bms import seplos as seplos_module
from bms.seplos import Seplos

CELLS = 16


def status_info(cells_mv):
    """150-char info of a 0x42 reply, offsets from Seplos.decode_status_data."""
    s = "0000" + "%02X" % len(cells_mv)
    s += "".join("%04X" % mv for mv in cells_mv)
    s += "06" + "".join("%04X" % 2981 for _ in range(6))
    s += "%04X" % 0 + "%04X" % round(sum(cells_mv) / 10) + "%04X" % 20000 + "0A" + "%04X" % 28000 + "%04X" % 900 + "0000" + "%04X" % 12
    return s.ljust(150, "0").encode("ascii")


def alarm_info():
    """98-char info of a 0x44 reply: no alarms, both FETs on."""
    b = bytearray(49)
    b[35] = 0x03
    return b.hex().upper().encode("ascii")


def reply_frame(address: int, info: bytes) -> bytes:
    """~ VER ADR CID1 RTN LENGTH INFO CHKSUM CR, with a valid checksum over whatever info holds."""
    body = "20{:02X}4600{:04X}".format(address, Seplos.get_info_length(info)).encode() + info
    return b"~" + body + "{:04X}".format(Seplos.get_checksum(body)).encode() + b"\r"


class OneReplySerial:
    """Stands in for serial.Serial as read_serial_data_seplos uses it: context manager, flush*, write, read_until."""

    reply = b""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def flushOutput(self):
        pass

    def flushInput(self):
        pass

    def write(self, data):
        return len(data)

    def read_until(self, expected=b"\n", size=None):
        # the whole reply is already in the input buffer: pyserial returns it up to and including `expected`
        reply = type(self).reply
        end = reply.find(expected)
        return reply if end < 0 else reply[: end + len(expected)]


@pytest.fixture
def one_reply(monkeypatch):
    monkeypatch.setattr(seplos_module.serial, "Serial", OneReplySerial)
    return OneReplySerial


def make_seplos(address):
    b = Seplos("/dev/ttyUSB1", 19200, address)
    b.cell_count = CELLS
    b.cells = [Cell(False) for _ in range(CELLS)]
    return b


def test_a_reply_from_another_pack_is_rejected(one_reply):
    # the status command goes to ADR 0x00; a frame from 0x01 is not its answer
    one_reply.reply = reply_frame(0x01, status_info([3400] * CELLS))
    b = make_seplos(b"\x00")
    assert b.read_status_data() is False
    assert all(c.voltage is None for c in b.cells)


def test_a_reply_from_the_asked_pack_is_read(one_reply):
    one_reply.reply = reply_frame(0x01, status_info([3400] * 15 + [3412]))
    b = make_seplos(b"\x01")
    assert b.read_status_data() is True
    assert b.cells[-1].voltage == pytest.approx(3.412)


def test_a_reply_from_another_pack_is_logged_once_without_the_frame(one_reply, monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(seplos_module, "logger", log)
    info = status_info([3400] * CELLS)
    one_reply.reply = reply_frame(0x01, info)
    assert make_seplos(b"\x00").read_status_data() is False
    assert log.warning.call_count == 1
    message = str(log.warning.call_args)
    assert "01" in message and "00" in message
    assert info.decode()[:40] not in message


class Bus:
    """
    One RS485 wire with both packs on it, seen through a USB adapter at 19200 baud.

    Replies are (arrival time, bytes); a frame lands whole at one instant. read_serial_data_seplos opens the port,
    flushInput() (drops what already arrived), writes, then read_until(b"\\r") with timeout=1 s. pyserial 3.5's
    read_until loops read(1): each read(1) returns as soon as a byte is in the input buffer or after the 1 s timeout,
    and the loop stops at the expected bytes, at an empty read, or once the call's own 1 s has expired after a byte.
    So the fake returns, at the arrival of the first frame that brings a CR within 1 s, everything up to and including
    that CR (the rest stays in the input buffer until the next flushInput), and otherwise everything that arrived when
    the 1 s is over. Not modelled: bytes trickling in until just before the deadline keep the real call open up to one
    more second; every frame here arrives whole.
    """

    def __init__(self, respond):
        self.now = 0.0
        self.pending = []  # (arrival, bytes)
        self.respond = respond  # (address byte, cid2) -> (delay s, frame) or None

    def serial(self):
        bus = self

        class BusSerial(OneReplySerial):
            def flushInput(self):
                bus.pending = [p for p in bus.pending if p[0] > bus.now]

            def write(self, data):
                address, cid2 = int(data[3:5], 16), int(data[7:9], 16)
                answer = bus.respond(address, cid2)
                if answer is not None:
                    delay, frame = answer
                    bus.pending.append((bus.now + delay, frame))
                return len(data)

            def read_until(self, expected=b"\n", size=None):
                end = bus.now + 1.0
                got = b""
                for arrival, chunk in sorted(p for p in bus.pending if p[0] <= end):
                    bus.pending.remove((arrival, chunk))
                    got += chunk
                    if expected in got:
                        cut = got.index(expected) + len(expected)
                        if cut < len(got):
                            bus.pending.append((arrival, got[cut:]))
                        bus.now = max(bus.now, arrival)
                        return got[:cut]
                bus.now = end
                return got

        return BusSerial


def test_a_late_reply_from_pack_0x00_is_not_published_as_pack_0x01(monkeypatch):
    # Trigger (conditional, not observed on our Cerbo): pack 0x00 answers its status request 2.2 s late and skips
    # its alarm request, pack 0x01 does not answer its status request in that poll. The late 0x00 frame lands in the
    # 0x01 status read window, has the right length (150) and a valid checksum, and nothing compared its ADR.
    p0_cells = [3300] * 15 + [3333]
    p1_cells = [3400] * CELLS

    def respond(address, cid2):
        if address == 0x00 and cid2 == 0x42:
            return 2.2, reply_frame(0x00, status_info(p0_cells))
        if address == 0x01 and cid2 == 0x44:
            return 0.1, reply_frame(0x01, alarm_info())
        return None

    bus = Bus(respond)
    monkeypatch.setattr(seplos_module.serial, "Serial", bus.serial())
    p0, p1 = make_seplos(b"\x00"), make_seplos(b"\x01")
    for c, mv in zip(p1.cells, p1_cells):  # pack 0x01's last good values
        c.voltage = mv / 1000

    p0.refresh_data()
    p1_ok = p1.refresh_data()

    assert [c.voltage for c in p1.cells] == [mv / 1000 for mv in p1_cells], f"pack 0x01 now holds pack 0x00's cells (refresh_data={p1_ok})"
    assert p1_ok is False  # its status read failed: publish_battery counts it like any other failed read
