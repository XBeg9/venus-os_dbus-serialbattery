"""
Tests for the cell over voltage protection (Cell OVP) recovery in Battery.manage_charge_voltage_limit().

After a cell over voltage lowers the charge voltage limit, the limit is held for CVL_RECOVERY_HOLD_SEC and then ramps
back up at CVL_RECOVERY_RATE_V_PER_SEC. Once it is back at max_battery_voltage the limiter must count as recovered:
plain "Bulk", no " (Cell OVP)" in the charge mode and no limit time left, or dbus-aggregate-batteries keeps treating
the pack as limiting (lowest CVL of all packs) for as long as the driver runs.
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
    monkeypatch.setattr(utils, "CVL_RECOVERY_HOLD_SEC", 60)
    monkeypatch.setattr(utils, "CVL_RECOVERY_RATE_V_PER_SEC", 0.001)
    monkeypatch.setattr(utils, "SOC_RESET_AFTER_DAYS", False)
    monkeypatch.setattr(utils, "GUI_PARAMETERS_SHOW_ADDITIONAL_INFO", False)
    return c


def make_battery(cell_count, max_battery_voltage):
    b = FakeBattery("/dev/null", 19200, "0x00")
    b.cell_count = cell_count
    b.cells = [Cell(False) for _ in range(cell_count)]
    b.max_battery_voltage = max_battery_voltage
    b.min_battery_voltage = round(2.9 * cell_count, 2)
    b.allow_max_voltage = True
    b.soc_calc = 98.0
    b.soc = 98.0
    return b


@pytest.fixture
def bat(clock):
    # depends on clock so MAX_CELL_VOLTAGE is patched before max_battery_voltage is computed
    return make_battery(CELL_COUNT, round(utils.MAX_CELL_VOLTAGE * CELL_COUNT, 2))


def set_cells(b, voltages):
    assert len(voltages) == b.cell_count
    for cell, v in zip(b.cells, voltages):
        cell.voltage = v


def run(b, clock, seconds, step=1):
    for _ in range(seconds // step):
        clock.t += step
        b.manage_charge_voltage_limit()


def limit_once(b, clock, relaxed_cells, limiting_cells):
    set_cells(b, relaxed_cells)
    run(b, clock, 5)
    set_cells(b, limiting_cells)
    run(b, clock, 1)
    assert "(Cell OVP)" in b.charge_mode
    assert b.control_voltage_last_limit_time is not None


def assert_recovered(b):
    assert b.control_voltage == pytest.approx(b.max_battery_voltage)
    assert "Cell OVP" not in b.charge_mode
    assert b.control_voltage_last_limit_time is None


# One cell at 3.470 V (20 mV over), the rest unbalanced, so the controller limits the CVL to
# sum - penalty = 14 x 3.437 + 3.412 + 3.45 = 54.98 V.
LIMITING_CELLS = [3.437] * 14 + [3.412, 3.470]
# All cells back under MAX_CELL_VOLTAGE: nothing limits any more.
RELAXED_CELLS = [3.411] * 15 + [3.370]


@pytest.mark.parametrize("step", [1, 5])
@pytest.mark.parametrize("seconds_after_limit", [400, 1000])
def test_cell_ovp_clears_once_the_limit_has_ramped_back_to_max(bat, clock, seconds_after_limit, step):
    # 54.98 V ramping at 1 mV/s passes 54.98 + 0.220 = 55.199999999999996, one float step below 55.2 V
    limit_once(bat, clock, RELAXED_CELLS, LIMITING_CELLS)
    assert bat.control_voltage == pytest.approx(54.98)

    set_cells(bat, RELAXED_CELLS)
    run(bat, clock, seconds_after_limit, step)

    assert_recovered(bat)


def test_cell_ovp_holds_then_ramps_before_it_recovers(bat, clock):
    limit_once(bat, clock, RELAXED_CELLS, LIMITING_CELLS)
    set_cells(bat, RELAXED_CELLS)

    run(bat, clock, 30)
    assert bat.control_voltage == pytest.approx(54.98)
    assert "(Cell OVP)" in bat.charge_mode

    run(bat, clock, 130)  # 60 s hold over, 100 s into the ramp
    assert bat.control_voltage == pytest.approx(54.98 + 0.100)
    assert "(Cell OVP*)" in bat.charge_mode
    assert bat.control_voltage_last_limit_time is not None


def test_cell_ovp_clears_when_max_is_not_on_the_rounding_grid(clock):
    # Drivers that do not round max_battery_voltage: 12 x 3.45 = 41.400000000000006, which rounds down to 41.4,
    # so a stored (rounded) ramp value never reaches the unrounded max
    max_battery_voltage = 12 * 3.45
    assert max_battery_voltage != round(max_battery_voltage, 6)
    b = make_battery(12, max_battery_voltage)

    limit_once(b, clock, [3.40] * 12, [3.40] * 11 + [3.47])
    set_cells(b, [3.40] * 12)
    run(b, clock, 1000)

    assert_recovered(b)


@pytest.mark.parametrize("new_max_battery_voltage", [55.2, 55.0])
def test_cell_ovp_clears_when_max_drops_to_or_below_the_stored_limit(clock, new_max_battery_voltage):
    # max_battery_voltage can drop at runtime (end of a SoC reset, BMS supplied DVCC values) while a limit is stored
    b = make_battery(CELL_COUNT, 56.0)

    # every cell clipped at 3.45 V: the limit is stored at 16 x 3.45 = 55.2 V, at or above the max it drops to
    limit_once(b, clock, RELAXED_CELLS, [3.45] * 15 + [3.47])
    assert b.control_voltage == pytest.approx(55.2)

    b.max_battery_voltage = new_max_battery_voltage
    set_cells(b, RELAXED_CELLS)
    run(b, clock, 1000)

    assert_recovered(b)
