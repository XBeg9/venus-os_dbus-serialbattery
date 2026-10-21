"""
CVL_CONTROLLER_MODE 3 must never set a charge voltage limit above max_battery_voltage (review of cerbo-2026-10-21.2).

The clipped-sum controller adds 10 mV to the clipped cell sum "to keep charging" but, unlike modes 1 and 2, did not
clamp the result to max_battery_voltage. With every cell at or above the limit and one over it, that is
16 x 3.45 + 0.010 = 55.21 V. The "not reduced" branch used to clamp it to max by accident; since a cell over the limit
always goes through the limiter, a fresh start stored and published 55.21 V for as long as the cells stayed there.
"""

import pytest

import battery as battery_module
import utils
from battery import Battery, Cell

CELL_COUNT = 16


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
        "CVL_CONTROLLER_MODE": 3,
        "MAX_CELL_VOLTAGE": 3.45,
        "CVL_RECOVERY_HOLD_SEC": 60,
        "CVL_RECOVERY_RATE_V_PER_SEC": 0.001,
        "SOC_RESET_AFTER_DAYS": False,
        "GUI_PARAMETERS_SHOW_ADDITIONAL_INFO": False,
    }.items():
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


def poll(b, clock, n=1):
    for _ in range(n):
        clock[0] += 5
        b.manage_charge_voltage_limit()


@pytest.mark.parametrize("warm", [False, True], ids=["fresh-start", "after-a-normal-poll"])
def test_mode3_never_sets_the_limit_above_max(bat, clock, warm):
    if warm:
        set_mv(bat, [3400] * 16)
        poll(bat, clock)
    set_mv(bat, [3450] * 15 + [3460])  # clipped sum 55.2 V, + 10 mV
    limits = []
    for _ in range(6):
        poll(bat, clock)
        limits.append(round(bat.control_voltage, 3))
    assert max(limits) <= bat.max_battery_voltage, f"CVL per poll: {limits}"
    assert "Cell OVP" in bat.charge_mode


def test_mode3_still_adds_its_margin_below_max(bat, clock):
    set_mv(bat, [3400] * 16)
    poll(bat, clock)
    set_mv(bat, [3420] * 15 + [3460])  # clipped sum 15 x 3.42 + 3.45 = 54.75 V, + 10 mV = 54.76 V
    poll(bat, clock)
    assert bat.control_voltage == pytest.approx(54.76)
