"""
Float must not carry the Cell OVP limiter state into the next bulk phase (review of cerbo-2026-10-21.2).

In absorption with a cell over the limit, the limiter stamps control_voltage_last_limit_time every poll, also when the
CVL is not reduced (every cell at or above the limit: the clipped sum is max). Float does not run the limiter, so the
stamp and control_voltage_ramp_base survived it. On the way back to bulk the stale stamp sent the controller into its
hold/ramp branch from the float CVL: the CVL stayed below max for ~50 s and the pack said "(Cell OVP*)", although no
cell had been over the limit for minutes and the CVL had never been reduced.
"""

import pytest

import battery as battery_module
import utils
from battery import Battery, Cell

CELL_COUNT = 16
POLL = 5


class FakeBattery(Battery):
    def test_connection(self):
        return True

    def get_settings(self):
        return True

    def refresh_data(self):
        return True


@pytest.fixture
def clock(monkeypatch):
    t = [1_000_000]
    monkeypatch.setattr(battery_module, "time", lambda: t[0])
    for name, value in {
        "CVL_CONTROLLER_MODE": 1,
        "MAX_CELL_VOLTAGE": 3.45,
        "FLOAT_CELL_VOLTAGE": 3.375,
        "CVL_RECOVERY_HOLD_SEC": 60,
        "CVL_RECOVERY_RATE_V_PER_SEC": 0.001,
        "SWITCH_TO_FLOAT_WAIT_FOR_SEC": 900,
        "SWITCH_TO_FLOAT_CELL_VOLTAGE_DIFF": 0.010,
        "SWITCH_TO_FLOAT_CELL_VOLTAGE_DEVIATION": 0.003,
        "SWITCH_TO_BULK_SOC_THRESHOLD": 80,
        "SWITCH_TO_BULK_CELL_VOLTAGE_DIFF": 0.080,
        "SOC_RESET_AFTER_DAYS": False,
        "SOC_CALCULATION": False,
        "GUI_PARAMETERS_SHOW_ADDITIONAL_INFO": False,
    }.items():
        assert hasattr(utils, name), name
        monkeypatch.setattr(utils, name, value)
    return t


@pytest.fixture
def bat(clock):
    b = FakeBattery("/dev/null", 19200, "0x00")
    b.cell_count = CELL_COUNT
    b.cells = [Cell(False) for _ in range(CELL_COUNT)]
    b.max_battery_voltage = round(utils.MAX_CELL_VOLTAGE * CELL_COUNT, 2)
    b.min_battery_voltage = round(2.9 * CELL_COUNT, 2)
    b.allow_max_voltage = True
    b.soc_calc = b.soc = 98.0
    return b


def set_mv(b, mv):
    for cell, v in zip(b.cells, mv):
        cell.voltage = v / 1000


def poll(b, clock, seconds):
    for _ in range(seconds // POLL):
        clock[0] += POLL
        b.manage_charge_voltage_limit()


@pytest.mark.parametrize("float_seconds", [120, 600, 3000])
def test_bulk_after_float_starts_at_max_without_cell_ovp(bat, clock, float_seconds):
    set_mv(bat, [3451] * 16)  # balanced, every cell 1 mV over: limiter stamped each poll, CVL stays at max
    poll(bat, clock, 1000)
    assert bat.allow_max_voltage is False  # absorption done: float

    set_mv(bat, [3400] * 16)  # nothing over the limit while the pack floats
    poll(bat, clock, float_seconds)
    assert bat.charge_mode.startswith("Float")

    bat.soc_calc = bat.soc = 70.0  # below SWITCH_TO_BULK_SOC_THRESHOLD: back to bulk
    poll(bat, clock, POLL)

    assert bat.allow_max_voltage is True
    assert "Cell OVP" not in bat.charge_mode, f"charge_mode={bat.charge_mode!r} after {float_seconds} s of float"
    assert bat.control_voltage == pytest.approx(bat.max_battery_voltage), f"CVL {bat.control_voltage} V, not max, on the first bulk poll"
