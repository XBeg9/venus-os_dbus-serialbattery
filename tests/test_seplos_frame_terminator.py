"""
A Seplos reply is read up to its end-of-frame CR, not until the serial timeout (review 2026-10-21).

Seplos V2 frames end with EOI 0x0D (CR). read_serial_data_seplos read the reply with readline(), which only stops at
LF, so every command waited the full 1 s timeout although the frame was complete after a few tens of milliseconds:
with two packs (status + alarm each) about 4 s of every 5 s poll went into waiting. No fakes here: real pyserial on a
pseudo terminal, a thread playing the BMS.
"""

import os
import threading
import time

from battery import Cell
from bms.seplos import Seplos

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


def read_status_on_a_pty(reply: bytes):
    """read_status_data() against a BMS that answers the command with `reply` 20 ms after it; (result, seconds)."""
    master, slave = os.openpty()

    def bms():
        cmd = b""
        while not cmd.endswith(b"\r"):
            cmd += os.read(master, 64)
        time.sleep(0.02)
        os.write(master, reply)

    t = threading.Thread(target=bms, daemon=True)
    try:
        b = Seplos(os.ttyname(slave), 19200, b"\x00")
        b.cell_count = CELLS
        b.cells = [Cell(False) for _ in range(CELLS)]
        t.start()
        start = time.monotonic()
        ok = b.read_status_data()
        elapsed = time.monotonic() - start
    finally:
        t.join(timeout=2)
        os.close(master)
        os.close(slave)
    return ok, elapsed


def test_a_complete_frame_is_returned_without_waiting_for_the_timeout():
    ok, elapsed = read_status_on_a_pty(reply_frame(0x00, status_info([3300] * CELLS)))
    assert ok is True
    assert elapsed < 0.5, f"read_status_data took {elapsed:.2f} s for a frame that was complete after ~0.02 s"


def test_a_frame_cut_short_waits_for_the_timeout_and_is_rejected():
    # no CR arrives: the read still ends at the 1 s timeout, and what came is not taken for a frame
    ok, elapsed = read_status_on_a_pty(reply_frame(0x00, status_info([3300] * CELLS))[:90])
    assert ok is False
    assert elapsed >= 0.9, f"read_status_data gave up after {elapsed:.2f} s, before the 1 s timeout"
