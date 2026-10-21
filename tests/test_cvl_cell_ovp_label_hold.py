"""
The "Cell OVP" label is held for CVL_RECOVERY_HOLD_SEC after the last poll with a cell over the limit (review of
cerbo-2026-10-21.2).

Where the limiter cuts the CVL below max, the label already lasts through its hold and ramp. Where it does not (a pack
in float, or a full pack whose cells are all at or above the limit, so the clipped sum is max), the label followed the
top cell poll by poll. Seplos reports whole millivolts, so a top cell sitting at the limit reads 3.450 / 3.451 V on
alternate polls, and dbus-aggregate-batteries (KEEP_MAX_CVL: lowest pack CVL while a ChargeMode says "Cell OVP", else
highest) flipped the bank CVL between the packs' values on every poll.
"""

import pytest

import battery as battery_module
import utils
from battery import Battery, Cell

CELL_COUNT = 16
HOLD = 60
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
        "CVL_RECOVERY_HOLD_SEC": HOLD,
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


def poll(b, clock, seconds=POLL):
    for _ in range(seconds // POLL):
        clock[0] += POLL
        b.manage_charge_voltage_limit()


def labels_while_dithering(b, clock, others_mv, polls=24):
    """Top cell reads 3.451 / 3.450 V on alternate polls (2 minutes). Returns the label per poll."""
    seen = []
    for i in range(polls):
        set_mv(b, [others_mv] * 15 + [3451 if i % 2 == 0 else 3450])
        poll(b, clock)
        seen.append("Cell OVP" in b.charge_mode)
    return seen


def to_float(b, clock):
    set_mv(b, [3450] * 16)
    poll(b, clock, 1000)  # absorption past SWITCH_TO_FLOAT_WAIT_FOR_SEC
    poll(b, clock, 1300)  # transition ramp down to 54.0 V
    assert b.charge_mode.startswith("Float") and not b.charge_mode.startswith("Float Transition")


def test_float_label_does_not_flip_with_a_top_cell_dithering_at_the_limit(bat, clock):
    to_float(bat, clock)
    seen = labels_while_dithering(bat, clock, 3440)
    assert all(seen), f"float label per 5 s poll: {['OVP' if s else '-' for s in seen]}"
    assert bat.control_voltage == pytest.approx(54.0)


def test_absorption_label_at_max_does_not_flip_with_a_top_cell_dithering_at_the_limit(bat, clock):
    # every other cell exactly at the limit: the clipped sum is max, the CVL is not reduced, only the label signals
    set_mv(bat, [3450] * 16)
    poll(bat, clock, 10)
    seen = labels_while_dithering(bat, clock, 3450)
    assert all(seen), f"absorption label per 5 s poll: {['OVP' if s else '-' for s in seen]}"
    assert bat.control_voltage == pytest.approx(bat.max_battery_voltage)


@pytest.mark.parametrize("phase", ["float", "absorption"])
def test_the_label_clears_once_no_cell_was_over_for_the_hold(bat, clock, phase):
    if phase == "float":
        to_float(bat, clock)
        others = 3440
    else:
        set_mv(bat, [3450] * 16)
        poll(bat, clock, 10)
        others = 3450
    set_mv(bat, [others] * 15 + [3451])
    poll(bat, clock)
    assert "Cell OVP" in bat.charge_mode

    set_mv(bat, [others] * 16)  # nothing over the limit from now on
    poll(bat, clock, HOLD - POLL)
    assert "Cell OVP" in bat.charge_mode, f"{phase}: label dropped before the hold ({HOLD} s) was over"
    poll(bat, clock, 2 * POLL)
    assert "Cell OVP" not in bat.charge_mode, f"{phase}: label still set {HOLD + POLL} s after the last cell over the limit"
    assert bat.control_voltage == pytest.approx(54.0 if phase == "float" else bat.max_battery_voltage)
