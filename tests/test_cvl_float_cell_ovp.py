"""
A cell above MAX_CELL_VOLTAGE while the pack is in float must be signalled as " (Cell OVP)" in the charge mode.

In float the charge voltage limit is already the float voltage, so there is nothing to clip, but the label is what
dbus-aggregate-batteries acts on: with KEEP_MAX_CVL it takes the highest pack CVL unless a pack's ChargeMode contains
"Cell OVP". Without the label, a pack in float is charged at the other pack's bulk voltage (55.2 V instead of 54.0 V)
while one of its cells is over the limit.
"""

import pytest

import battery as battery_module
import utils
from battery import Battery, Cell

CELL_COUNT = 16
FLOAT_VOLTAGE = 54.0


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
    monkeypatch.setattr(utils, "CVL_CONTROLLER_MODE", 1)
    monkeypatch.setattr(utils, "MAX_CELL_VOLTAGE", 3.45)
    monkeypatch.setattr(utils, "FLOAT_CELL_VOLTAGE", 3.375)
    monkeypatch.setattr(utils, "CVL_RECOVERY_HOLD_SEC", 60)
    monkeypatch.setattr(utils, "CVL_RECOVERY_RATE_V_PER_SEC", 0.001)
    monkeypatch.setattr(utils, "SWITCH_TO_FLOAT_WAIT_FOR_SEC", 900)
    monkeypatch.setattr(utils, "SWITCH_TO_FLOAT_CELL_VOLTAGE_DIFF", 0.010)
    monkeypatch.setattr(utils, "SWITCH_TO_FLOAT_CELL_VOLTAGE_DEVIATION", 0.003)
    monkeypatch.setattr(utils, "SWITCH_TO_BULK_SOC_THRESHOLD", 80)
    monkeypatch.setattr(utils, "SWITCH_TO_BULK_CELL_VOLTAGE_DIFF", 0.080)
    monkeypatch.setattr(utils, "SOC_RESET_AFTER_DAYS", False)
    monkeypatch.setattr(utils, "SOC_CALCULATION", False)
    monkeypatch.setattr(utils, "GUI_PARAMETERS_SHOW_ADDITIONAL_INFO", False)
    return c


@pytest.fixture
def bat(clock):
    b = FakeBattery("/dev/null", 19200, "0x00")
    b.cell_count = CELL_COUNT
    b.cells = [Cell(False) for _ in range(CELL_COUNT)]
    b.max_battery_voltage = round(utils.MAX_CELL_VOLTAGE * CELL_COUNT, 2)
    b.min_battery_voltage = round(2.9 * CELL_COUNT, 2)
    b.allow_max_voltage = True
    b.soc_calc = 98.0
    b.soc = 98.0
    return b


def set_cells(b, voltages):
    assert len(voltages) == b.cell_count
    for cell, v in zip(b.cells, voltages):
        cell.voltage = v


def run(b, clock, seconds, step=1):
    for _ in range(seconds // step):
        clock.t += step
        b.manage_charge_voltage_limit()


# balanced pack at max: absorption timer runs, no cell over the limit
BALANCED_FULL = [3.450] * 16
# one cell runs away while the pack floats; spread 53 mV < SWITCH_TO_BULK_CELL_VOLTAGE_DIFF, so it stays in float
HIGH_CELL = [3.447] * 15 + [3.50]
RELAXED = [3.370] * 16


def reach_float(b, clock):
    set_cells(b, BALANCED_FULL)
    run(b, clock, 905)
    assert b.allow_max_voltage is False
    run(b, clock, 1300)  # the float transition ramps 1 mV/s down to 54.0 V
    assert b.charge_mode.startswith("Float") and not b.charge_mode.startswith("Float Transition")
    assert b.control_voltage == pytest.approx(FLOAT_VOLTAGE)


def test_bulk_labels_a_cell_over_max(bat, clock):
    set_cells(bat, HIGH_CELL)
    run(bat, clock, 1)
    assert bat.allow_max_voltage is True
    assert "Cell OVP" in bat.charge_mode


def test_float_labels_a_cell_over_max(bat, clock):
    reach_float(bat, clock)

    set_cells(bat, HIGH_CELL)
    run(bat, clock, 10)

    assert bat.allow_max_voltage is False  # still in float
    assert bat.charge_mode.startswith("Float")
    assert "Cell OVP" in bat.charge_mode, f"charge_mode={bat.charge_mode!r}"
    assert bat.control_voltage == pytest.approx(FLOAT_VOLTAGE)


def test_float_transition_labels_a_cell_over_max_and_keeps_ramping(bat, clock):
    set_cells(bat, BALANCED_FULL)
    run(bat, clock, 905)
    run(bat, clock, 100)  # 100 s into the transition ramp
    assert bat.charge_mode.startswith("Float Transition")
    before = bat.control_voltage

    set_cells(bat, HIGH_CELL)
    run(bat, clock, 10)

    assert bat.charge_mode.startswith("Float Transition")
    assert "Cell OVP" in bat.charge_mode
    assert bat.control_voltage == pytest.approx(before - 0.010)  # the ramp is not disturbed by the label


def test_float_label_clears_when_the_cell_is_back_under_max(bat, clock):
    reach_float(bat, clock)
    set_cells(bat, HIGH_CELL)
    run(bat, clock, 10)
    assert "Cell OVP" in bat.charge_mode

    set_cells(bat, RELAXED)
    run(bat, clock, 5)

    assert bat.charge_mode.startswith("Float")
    assert "Cell OVP" not in bat.charge_mode
    assert bat.control_voltage == pytest.approx(FLOAT_VOLTAGE)
