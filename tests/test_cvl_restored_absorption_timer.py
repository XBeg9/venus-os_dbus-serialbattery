"""
An absorption timer restored after a driver restart must not float a pack that is not near max voltage (review
2026-10-21, #8).

DbusHelper.setup_instance restores battery.max_voltage_start_time from the saved MaxVoltageStartTime (dbushelper.py
369-373), the moment the pack last reached max voltage balanced. In manage_charge_voltage_limit the
SWITCH_TO_FLOAT_WAIT_FOR_SEC expiry ran before the "voltage_sum < max_battery_voltage - 0.5" timer reset, so with a
start time older than 900 s the first poll after the restart switched the pack to float (54.0 V) whatever its voltage,
and it then stayed in float until SoC < SWITCH_TO_BULK_SOC_THRESHOLD or an 80 mV spread.
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
        "MIN_CELL_VOLTAGE": 2.9,
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


def restarted_battery(clock, start_time_age):
    """The state setup_instance leaves after a restart: AllowMaxVoltage=1 and a MaxVoltageStartTime start_time_age ago."""
    b = FakeBattery("/dev/null", 19200, "0x00")
    b.cell_count = CELLS
    b.cells = [Cell(False) for _ in range(CELLS)]
    b.max_battery_voltage = round(utils.MAX_CELL_VOLTAGE * CELLS, 2)  # 55.2
    b.min_battery_voltage = round(utils.MIN_CELL_VOLTAGE * CELLS, 2)
    b.allow_max_voltage = True
    b.max_voltage_start_time = clock.t - start_time_age
    b.soc_calc = b.soc = 90.0
    return b


def set_cells(b, voltages):
    for cell, v in zip(b.cells, voltages):
        cell.voltage = v


def test_a_stale_restored_timer_does_not_float_a_pack_that_is_not_near_max(clock):
    b = restarted_battery(clock, 1000)  # older than SWITCH_TO_FLOAT_WAIT_FOR_SEC
    # ~52.0 V, below max (55.2 V) by more than the 0.5 V tolerance; balanced (5 mV)
    set_cells(b, [3.250] * 15 + [3.255])
    assert b.get_cell_voltage_sum() < b.max_battery_voltage - 0.5

    modes = []
    for _ in range(11):
        clock.t += 1
        b.manage_charge_voltage_limit()
        modes.append(b.charge_mode.split(" │")[0])

    assert modes[0] == "Bulk", f"first poll after the restart: {modes[0]!r} at {b.get_cell_voltage_sum():.2f} V"
    assert set(modes) == {"Bulk"}, modes
    assert b.allow_max_voltage is True
    assert b.max_voltage_start_time is None
    assert b.control_voltage == pytest.approx(55.2)


def test_a_restored_timer_keeps_counting_while_the_pack_holds_max(clock):
    # control: the restore is what lets a restart in the middle of absorption float on time instead of starting over
    b = restarted_battery(clock, 300)
    set_cells(b, [3.450] * 16)  # at max (55.2 V), balanced, no cell over the limit
    started = b.max_voltage_start_time

    for _ in range(120):  # 600 s at 5 s polls: 900 s since the restored start
        clock.t += 5
        b.manage_charge_voltage_limit()
    assert (b.charge_mode.split(" │")[0], b.max_voltage_start_time) == ("Absorption", started)

    clock.t += 5
    b.manage_charge_voltage_limit()
    assert b.allow_max_voltage is False
    assert b.charge_mode.startswith("Float")
