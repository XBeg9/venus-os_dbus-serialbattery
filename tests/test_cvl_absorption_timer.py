"""
The absorption timer and the float/bulk switch must not depend on float rounding of the cell values (review 2026-10-21).

config.default.ini:292 "CVL is maintained until the difference is equal or lower" (SWITCH_TO_FLOAT_CELL_VOLTAGE_DIFF
0.010) and :295-298 "the timer will restart if the cell voltage difference exceeds 0.013 V (0.010 + 0.003)". Seplos
reports whole millivolts (decode_status_data divides by 1000), so voltage_cell_diff = max - min (battery.py:713) is a
float like 0.010000000000000231 for a 10 mV spread. battery.py:723 (<= 0.010) and :744 (> 0.010 + 0.003) then land on
either side of the documented boundary depending on which two mV values the cells have. The same goes for the cell sum
against max_battery_voltage (:721) and the spread against SWITCH_TO_BULK_CELL_VOLTAGE_DIFF (:733, documented "greater or
equal" at config.default.ini:270). Each case has a control with the same values in mV that already behaves as documented.
"""

import pytest

import battery as battery_module
import utils
from battery import Battery, Cell

CELLS = 16


class FakeBattery(Battery):
    def test_connection(self):
        return True

    def get_settings(self):
        return True

    def refresh_data(self):
        return True


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock(1_000_000)
    monkeypatch.setattr(battery_module, "time", c)
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
    return c


@pytest.fixture
def bat(clock):
    b = FakeBattery("/dev/null", 19200, "0x00")
    b.cell_count = CELLS
    b.cells = [Cell(False) for _ in range(CELLS)]
    b.max_battery_voltage = round(utils.MAX_CELL_VOLTAGE * CELLS, 2)  # 55.2
    b.min_battery_voltage = round(2.9 * CELLS, 2)
    b.allow_max_voltage = True
    b.soc_calc = b.soc = 98.0
    return b


def mv(values):
    """Cell voltages as decode_status_data produces them from the BMS's integer millivolts."""
    return [v / 1000 for v in values]


def set_cells(b, voltages):
    for cell, v in zip(b.cells, voltages):
        cell.voltage = v


def run(b, clock, seconds, step=5):
    for _ in range(seconds // step):
        clock.t += step
        b.manage_charge_voltage_limit()


# 15 cells hi, 1 cell lo, exactly 10 mV apart, cell sum >= 55.2 V (the pack has reached max voltage)
@pytest.mark.parametrize("hi, lo", [(3452, 3442), (3451, 3441)], ids=["control-0.009999999999999787", "red-0.010000000000000231"])
def test_a_10_mv_spread_at_max_voltage_goes_to_float(bat, clock, hi, lo):
    set_cells(bat, mv([hi] * 15 + [lo]))
    assert round((bat.get_max_cell_voltage() - bat.get_min_cell_voltage()) * 1000) == 10
    assert bat.get_cell_voltage_sum() >= bat.max_battery_voltage

    run(bat, clock, 1000)  # > SWITCH_TO_FLOAT_WAIT_FOR_SEC

    spread = bat.get_max_cell_voltage() - bat.get_min_cell_voltage()
    assert bat.allow_max_voltage is False, (
        f"10 mV spread ({hi}/{lo} mV -> {spread!r}) never started the absorption timer: still "
        f"{bat.charge_mode.split(' │')[0]!r} after 1000 s at max voltage"
    )


# timer running, then one poll with exactly 13 mV spread: documented "restart if it exceeds 0.013 V" -> no restart
@pytest.mark.parametrize("hi, lo", [(3449, 3436), (3450, 3437)], ids=["control-0.0129999999999999", "red-0.013000000000000345"])
def test_a_13_mv_spread_does_not_restart_the_absorption_timer(bat, clock, hi, lo):
    set_cells(bat, mv([3450] * 16))
    run(bat, clock, 5)
    started = bat.max_voltage_start_time
    assert started is not None

    run(bat, clock, 600)
    set_cells(bat, mv([hi] * 15 + [lo]))
    assert round((bat.get_max_cell_voltage() - bat.get_min_cell_voltage()) * 1000) == 13
    run(bat, clock, 5)

    spread = bat.get_max_cell_voltage() - bat.get_min_cell_voltage()
    assert bat.max_voltage_start_time == started, (
        f"13 mV spread ({hi}/{lo} mV -> {spread!r}) restarted the timer after 605 s: " f"{bat.max_voltage_start_time - started} s of absorption lost"
    )


# 16 cells totalling exactly 55200 mV (= max_battery_voltage), spread 8 mV: the pack has reached max voltage, balanced
BALANCED_55200_LOW = [3454, 3454, 3452, 3450, 3449, 3453, 3454, 3451, 3447, 3451, 3446, 3449, 3447, 3446, 3446, 3451]  # sums to 55.199999999999996


@pytest.mark.parametrize("cells_mv", [[3450] * 16, BALANCED_55200_LOW], ids=["control-55.20000000000002", "red-55.199999999999996"])
def test_a_pack_at_exactly_max_voltage_starts_the_absorption_timer(bat, clock, cells_mv):
    assert sum(cells_mv) == 55200 and max(cells_mv) - min(cells_mv) <= 10
    set_cells(bat, mv(cells_mv))
    run(bat, clock, 5)
    assert bat.max_voltage_start_time is not None, f"cells total 55200 mV but sum to {bat.get_cell_voltage_sum()!r} < 55.2: timer not started"


# pack in float, one cell drifts exactly 80 mV below the others: config.default.ini:270 "greater or equal" -> back to bulk
@pytest.mark.parametrize("hi, lo", [(3440, 3360), (3441, 3361)], ids=["control", "red-0.07999999999999963"])
def test_an_80_mv_spread_in_float_switches_back_to_bulk(bat, clock, hi, lo):
    set_cells(bat, mv([3450] * 16))
    run(bat, clock, 1000)
    assert bat.allow_max_voltage is False  # float

    set_cells(bat, mv([hi] * 15 + [lo]))
    assert round((bat.get_max_cell_voltage() - bat.get_min_cell_voltage()) * 1000) == 80
    run(bat, clock, 5)

    spread = bat.get_max_cell_voltage() - bat.get_min_cell_voltage()
    assert bat.allow_max_voltage is True, f"80 mV spread ({hi}/{lo} mV -> {spread!r}) did not switch back to bulk"
