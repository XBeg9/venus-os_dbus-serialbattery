"""
A Seplos reply is read from the port in chunks, not one byte per system call (CPU review 2026-10-21).

pyserial's read_until loops read(1): a select, an os.read and the timeout bookkeeping for every byte of the reply. On
the Cerbo that cost 28-33 ms CPU per command, 4 commands per 5 s poll for two packs. The reply now comes with one
os.read per wake-up (6-10 ms per command), up to the frame's first CR or a 1 s deadline from the write, and a port that
hangs up or fails raises pyserial's own SerialException as before. Real pyserial on a pseudo terminal, a thread playing
the BMS; the hang-up case uses a pipe whose write end is closed.
"""

import errno
import os
import threading
import time

import pytest
import serial

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


def reply_frame(address: int, info: bytes) -> bytes:
    """~ VER ADR CID1 RTN LENGTH INFO CHKSUM CR, with a valid checksum over whatever info holds."""
    body = "20{:02X}4600{:04X}".format(address, Seplos.get_info_length(info)).encode() + info
    return b"~" + body + "{:04X}".format(Seplos.get_checksum(body)).encode() + b"\r"


FRAME = reply_frame(0x00, status_info([3300] * 15 + [3333]))  # 168 bytes, a status reply as Pack-02 sends it


def pieces(data: bytes, n: int):
    """data in n pieces of about equal size."""
    size = -(-len(data) // n)
    return [data[i : i + size] for i in range(0, len(data), size)]


def make_seplos(port):
    b = Seplos(port, 19200, b"\x00")
    b.cell_count = CELLS
    b.cells = [Cell(False) for _ in range(CELLS)]
    return b


def timed(call):
    start = time.monotonic()
    result = call()
    return result, time.monotonic() - start


class PtyBms:
    """
    A BMS on the master side of a pseudo terminal: once the whole command is in, it writes `chunks`, each `gap`
    seconds after the one before. Every os.read on the slave tty (the port, which pyserial opens by name, so on a
    descriptor of its own) is counted in port_reads, and raises the next exception in `fail` instead, if any.
    """

    def __init__(self, monkeypatch, chunks, gap):
        self.master, self.slave = os.openpty()
        self.port = os.ttyname(self.slave)
        self.chunks, self.gap = chunks, gap
        self.port_reads = 0
        self.fail = []
        tty = os.fstat(self.slave)
        real_read = os.read

        def read(fd, n):
            if fd != self.master and os.path.samestat(os.fstat(fd), tty):
                self.port_reads += 1
                if self.fail:
                    raise self.fail.pop(0)
            return real_read(fd, n)

        monkeypatch.setattr(os, "read", read)
        self.thread = threading.Thread(target=self.answer, daemon=True)
        self.thread.start()

    def answer(self):
        try:
            cmd = b""
            while not cmd.endswith(b"\r"):
                cmd += os.read(self.master, 64)
            for chunk in self.chunks:
                time.sleep(self.gap)
                os.write(self.master, chunk)
        except OSError:
            pass  # the test is over and closed the pty

    def close(self):
        self.thread.join(timeout=2)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def pty_bms(monkeypatch):
    started = []

    def start(chunks, gap=0.02):
        bms = PtyBms(monkeypatch, chunks, gap)
        started.append(bms)
        return bms

    yield start
    for bms in started:
        bms.close()


def test_a_reply_is_read_with_one_os_read_per_chunk_not_one_per_byte(pty_bms):
    assert len(FRAME) == 168
    chunks = pieces(FRAME, 6)
    bms = pty_bms(chunks)
    assert make_seplos(bms.port).read_status_data() is True
    assert bms.port_reads <= len(chunks) + 2, (
        f"{bms.port_reads} os.read calls on the port for a {len(FRAME)}-byte reply that arrived in {len(chunks)} chunks:"
        " one per byte, as pyserial's read_until(b'\\r') reads"
    )


def test_a_reply_split_over_several_chunks_is_assembled_and_decoded(pty_bms):
    # the CR comes alone, last: the read must not stop at a chunk boundary
    bms = pty_bms([FRAME[:1], FRAME[1:70], FRAME[70:-1], FRAME[-1:]])
    b = make_seplos(bms.port)
    assert b.read_status_data() is True
    assert b.cells[-1].voltage == pytest.approx(3.333)


def test_bytes_after_the_first_cr_are_not_part_of_the_reply(pty_bms, monkeypatch):
    # a chunk carries the end of the frame and the start of whatever follows on the bus, another CR included
    rx = []
    monkeypatch.setattr(seplos_module, "capture_raw_data", lambda port, direction, data: rx.append(data) if direction == "rx" else None)
    bms = pty_bms([FRAME[:100], FRAME[100:] + b"~20014600\r~2001"])
    b = make_seplos(bms.port)
    assert b.read_status_data() is True
    assert b.cells[-1].voltage == pytest.approx(3.333)
    assert rx == [FRAME]


def test_a_reply_without_cr_is_read_until_the_deadline_and_rejected(pty_bms, monkeypatch):
    monkeypatch.setattr(seplos_module, "REPLY_TIMEOUT", 0.3)
    bms = pty_bms(pieces(FRAME[:-1], 3))
    ok, elapsed = timed(make_seplos(bms.port).read_status_data)
    assert ok is False
    assert 0.28 <= elapsed < 0.6, f"read_status_data took {elapsed:.2f} s for a 0.3 s deadline"


def test_bytes_just_before_the_deadline_do_not_extend_the_read(pty_bms, monkeypatch):
    # pyserial's read_until gave each read(1) its own timeout on top of the call's: a byte that came just before the
    # deadline kept the read open for up to one more timeout (about 2 s with timeout=1)
    monkeypatch.setattr(seplos_module, "REPLY_TIMEOUT", 0.3)
    bms = pty_bms(pieces(FRAME[:-1], 2), gap=0.12)  # the second piece comes about 0.24 s after the command
    ok, elapsed = timed(make_seplos(bms.port).read_status_data)
    assert ok is False
    assert elapsed < 0.45, f"read_status_data took {elapsed:.2f} s for a 0.3 s deadline"


def hung_up_serial(reply: bytes):
    """serial.Serial as read_serial_data_seplos uses it, on a pipe that holds `reply` and whose write end is closed."""

    class HungUpSerial:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            self.r, w = os.pipe()
            os.write(w, reply)
            os.close(w)  # an unplugged adapter: always readable, and a read returns no data
            return self

        def __exit__(self, *exc):
            os.close(self.r)
            return False

        def flushOutput(self):
            pass

        def flushInput(self):
            pass

        def write(self, data):
            return len(data)

        def fileno(self):
            return self.r

    return HungUpSerial


def test_a_port_that_hangs_up_raises_at_once_instead_of_spinning_to_the_deadline(monkeypatch):
    # without the check, select keeps saying readable and os.read keeps returning b"": a busy loop until the deadline
    monkeypatch.setattr(seplos_module.serial, "Serial", hung_up_serial(FRAME[:90]))
    start = time.monotonic()
    with pytest.raises(serial.SerialException, match="device reports readiness to read but returned no data"):
        make_seplos("/dev/ttyUSB1").read_status_data()
    assert time.monotonic() - start < 0.2


def test_a_read_error_on_the_port_raises_serial_exception_at_once(pty_bms):
    bms = pty_bms(pieces(FRAME, 3))
    bms.fail.append(OSError(errno.EIO, "Input/output error"))
    start = time.monotonic()
    with pytest.raises(serial.SerialException, match="read failed"):
        make_seplos(bms.port).read_status_data()
    assert time.monotonic() - start < 0.5


def test_a_read_that_would_block_is_retried(pty_bms):
    # pyserial opens the port O_NONBLOCK and ignores EAGAIN after select, so a spurious wake-up is not an error
    bms = pty_bms(pieces(FRAME, 3))
    bms.fail.append(OSError(errno.EAGAIN, "Resource temporarily unavailable"))
    b = make_seplos(bms.port)
    assert b.read_status_data() is True
    assert b.cells[-1].voltage == pytest.approx(3.333)
