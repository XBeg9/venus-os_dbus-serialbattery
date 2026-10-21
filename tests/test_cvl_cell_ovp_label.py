"""
Whether the P-controller labels a pack "(Cell OVP)" must not depend on float rounding (review 2026-10-21).

With every cell at or above MAX_CELL_VOLTAGE and some above it, the clipped sum voltage_sum - penalty_sum is 16 x 3.45
= 55.2 V in exact arithmetic. In floats it lands a few ulp above or below max_battery_voltage, and that alone decides
the branch in manage_charge_voltage_limit (battery.py:849-859): >= max -> plain "Absorption"; < max -> "(Cell OVP)".
dbus-aggregate-batteries with KEEP_MAX_CVL takes the MAX of the pack CVLs unless a ChargeMode contains "Cell OVP", so
the bank CVL follows the rounding noise.
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


@pytest.fixture
def clock(monkeypatch):
    t = [1_000_000]
    monkeypatch.setattr(battery_module, "time", lambda: t[0])
    for name, value in {
        "CVL_CONTROLLER_MODE": 1,
        "MAX_CELL_VOLTAGE": 3.45,
        "CVL_RECOVERY_HOLD_SEC": 60,
        "CVL_RECOVERY_RATE_V_PER_SEC": 0.001,
        "SOC_RESET_AFTER_DAYS": False,
        "GUI_PARAMETERS_SHOW_ADDITIONAL_INFO": False,
    }.items():
        monkeypatch.setattr(utils, name, value)
    return t


def label_after(clock, cells):
    b = FakeBattery("/dev/null", 19200, "0x00")
    b.cell_count = CELLS
    b.cells = [Cell(False) for _ in range(CELLS)]
    b.max_battery_voltage = round(utils.MAX_CELL_VOLTAGE * CELLS, 2)
    b.min_battery_voltage = round(2.9 * CELLS, 2)
    b.allow_max_voltage = True
    b.soc_calc = b.soc = 98.0
    for c in b.cells:
        c.voltage = 3.40
    clock[0] += 5
    b.manage_charge_voltage_limit()
    for c, v in zip(b.cells, cells):
        c.voltage = v
    clock[0] += 5
    b.manage_charge_voltage_limit()
    return b


# n cells at 3.47 V (20 mV over), the rest exactly at 3.45 V: the same physical condition for every n
@pytest.mark.parametrize("n_over", [1, 13, 14])
def test_a_cell_over_max_is_labelled_cell_ovp_whatever_the_rounding(clock, n_over):
    cells = [3.47] * n_over + [3.45] * (CELLS - n_over)
    b = label_after(clock, cells)
    clipped = b.get_cell_voltage_sum() - sum(v - 3.45 for v in cells if v > 3.45)
    assert b.get_max_cell_voltage() > utils.MAX_CELL_VOLTAGE
    assert (
        "Cell OVP" in b.charge_mode
    ), f"{n_over} cell(s) at 3.47 V: charge_mode={b.charge_mode.split(' │')[0]!r}, clipped sum {clipped!r} vs max {b.max_battery_voltage!r}"


def test_a_full_pack_with_no_cell_over_max_is_not_labelled(clock):
    b = label_after(clock, [3.45] * CELLS)
    assert "Cell OVP" not in b.charge_mode
    assert b.control_voltage == pytest.approx(b.max_battery_voltage)


def test_the_label_clears_at_once_when_the_cells_drop_back_to_max(clock):
    # the CVL never went below max (the clipped sum was max), so there is no hold or ramp to wait for
    b = label_after(clock, [3.47] * 13 + [3.45] * 3)
    for c in b.cells:
        c.voltage = 3.45
    clock[0] += 5
    b.manage_charge_voltage_limit()
    assert "Cell OVP" not in b.charge_mode
    assert b.control_voltage == pytest.approx(b.max_battery_voltage)
    assert b.control_voltage_last_limit_time is None
