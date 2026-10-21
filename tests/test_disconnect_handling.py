"""
What publish_battery does while a Seplos pack stops answering on RS485 and the port stays (review 2026-10-21).

config.default.ini, "BMS Disconnect Behavior", with BLOCK_ON_DISCONNECT = False: cells between
BLOCK_ON_DISCONNECT_VOLTAGE_MIN and BLOCK_ON_DISCONNECT_VOLTAGE_MAX ride through for BLOCK_ON_DISCONNECT_TIMEOUT_MINUTES,
cells outside that band are blocked and the driver exits after 60 s.

The real DbusHelper.publish_battery/publish_dbus and the real Seplos decoder run; faked are the serial transport
(Seplos.read_serial_data_seplos), the clock and the VeDbusService (a dict).
"""

from unittest.mock import MagicMock

import pytest

import battery as battery_module
import dbushelper
import utils
from battery import Cell
from bms.seplos import Seplos
from dbushelper import DbusHelper, _CachedDbusProxy

CELLS = 16
POLL = 5  # s, our poll interval


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def status_info(cells_mv, current_cA=0, soc_dpct=900):
    """150-char info of a 0x42 reply, offsets from Seplos.decode_status_data."""
    s = "0000" + "%02X" % len(cells_mv)
    s += "".join("%04X" % mv for mv in cells_mv)
    s += "06" + "".join("%04X" % 2981 for _ in range(6))
    s += "%04X" % (current_cA & 0xFFFF) + "%04X" % round(sum(cells_mv) / 10) + "%04X" % 20000 + "0A" + "%04X" % 28000
    s += "%04X" % soc_dpct + "0000" + "%04X" % 12
    return s.ljust(150, "0").encode("ascii")


def alarm_info():
    """98-char info of a 0x44 reply; byte 35 = switch: charge and discharge FET on."""
    b = bytearray(49)
    b[35] = 0x03
    return b.hex().upper().encode("ascii")


class Link:
    """Replaces Seplos.read_serial_data_seplos: the info bytes of a reply, or False like a timed-out read."""

    def __init__(self):
        self.status = None
        self.alarm = None

    def answer(self, cells_mv):
        self.status = status_info(cells_mv)
        self.alarm = alarm_info()

    def silence(self):
        self.status = self.alarm = False

    def __call__(self, command):
        return self.status if command[7:9] == b"42" else self.alarm


OUR_CONFIG = {
    "MAX_CELL_VOLTAGE": 3.45,
    "FLOAT_CELL_VOLTAGE": 3.375,
    "MIN_CELL_VOLTAGE": 2.9,
    "MAX_BATTERY_CHARGE_CURRENT": 70.0,
    "MAX_BATTERY_DISCHARGE_CURRENT": 140.0,
    "CVL_CONTROLLER_MODE": 1,
    "CVL_RECOVERY_HOLD_SEC": 60,
    "CVL_RECOVERY_RATE_V_PER_SEC": 0.001,
    "SWITCH_TO_FLOAT_WAIT_FOR_SEC": 900,
    "SWITCH_TO_FLOAT_CELL_VOLTAGE_DIFF": 0.010,
    "SWITCH_TO_BULK_SOC_THRESHOLD": 80,
    "SWITCH_TO_BULK_CELL_VOLTAGE_DIFF": 0.080,
    "SOC_RESET_AFTER_DAYS": False,
    "SOC_CALCULATION": False,
    "CCCM_CV_ENABLE": True,
    "DCCM_CV_ENABLE": True,
    "CELL_VOLTAGES_WHILE_CHARGING": [3.55, 3.45, 3.44, 3.38],
    "MAX_CHARGE_CURRENT_CV": [0.0, 10.5, 35.0, 70.0],
    "CELL_VOLTAGES_WHILE_DISCHARGING": [2.8, 2.9, 3.0, 3.05, 3.1],
    "MAX_DISCHARGE_CURRENT_CV": [0.0, 7.0, 35.0, 70.0, 140.0],
    "CCCM_T_ENABLE": False,
    "DCCM_T_ENABLE": False,
    "CCCM_T_MOSFET_ENABLE": False,
    "DCCM_T_MOSFET_ENABLE": False,
    "CCCM_SOC_ENABLE": False,
    "DCCM_SOC_ENABLE": False,
    "CHARGE_CURRENT_RECOVERY_THRESHOLD_PERCENT": 0.015,
    "DISCHARGE_CURRENT_RECOVERY_THRESHOLD_PERCENT": 0.015,
    "CCL_RECOVERY_HOLD_SEC": 120,
    "CCL_RECOVERY_RATE_A_PER_SEC": 0.10,
    "DCL_RECOVERY_HOLD_SEC": 30,
    "DCL_RECOVERY_RATE_A_PER_SEC": 5,
    "CVL_RECALCULATION_EVERY": 10,
    "BLOCK_ON_DISCONNECT": False,
    "BLOCK_ON_DISCONNECT_TIMEOUT_MINUTES": 30,
    "BLOCK_ON_DISCONNECT_VOLTAGE_MIN": 3.225,
    # our config.ini sets 3.450; utils.py clamps any higher value to MAX_CELL_VOLTAGE (3.450)
    "BLOCK_ON_DISCONNECT_VOLTAGE_MAX": 3.45,
    "VOLTAGE_DROP": 0.0,
    "GUI_PARAMETERS_SHOW_ADDITIONAL_INFO": False,
    "TELEMETRY": False,
    "TIME_TO_GO_ENABLE": False,
    "TIME_TO_SOC_POINTS": [],
    "PUBLISH_BATTERY_DATA_AS_JSON": False,
    "HISTORY_ENABLE": False,
    "CAPTURE_RAW_DATA": False,
    "EXTERNAL_SENSOR_DBUS_DEVICE": None,
    "EXTERNAL_SENSOR_DBUS_PATH_CURRENT": None,
    "EXTERNAL_SENSOR_DBUS_PATH_SOC": None,
    "BMS_CABLE_ALARM": True,
}


@pytest.fixture
def env(monkeypatch):
    clock = Clock(1_000_000)
    monkeypatch.setattr(battery_module, "time", clock)
    monkeypatch.setattr(dbushelper, "time", clock)
    for name, value in OUR_CONFIG.items():
        assert hasattr(utils, name), name
        monkeypatch.setattr(utils, name, value)
    link = Link()
    monkeypatch.setattr(Seplos, "read_serial_data_seplos", link)
    bat = Seplos("/dev/ttyUSB1", 19200, b"\x00")
    bat.cell_count = CELLS
    bat.cells = [Cell(False) for _ in range(CELLS)]  # what Seplos.get_settings() does after test_connection
    helper = DbusHelper(bat)
    svc = {"/Connected": 1, "/Mode": 3}  # /Connected: setup_vedbus add_path("/Connected", 1)
    helper._dbusservice = _CachedDbusProxy(svc)
    monkeypatch.setattr(helper, "save_current_battery_state", lambda: None)
    helper.last_seen_saved_last_time = clock.t
    return clock, link, bat, helper, svc, MagicMock()


LIMITS = ("/Info/MaxChargeCurrent", "/Info/MaxDischargeCurrent", "/Io/AllowToCharge", "/Io/AllowToDischarge")


def limits(svc):
    return tuple(svc[p] for p in LIMITS)


def go_silent(clock, link, helper, loop, seconds):
    """The pack stops answering for `seconds` of 5 s polls; returns (seconds since the first failed poll, limits) per poll."""
    link.silence()
    t_first = clock.t + POLL
    rows = []
    for _ in range(seconds // POLL):
        clock.t += POLL
        helper.publish_battery(loop)
        rows.append((clock.t - t_first, limits(helper._dbusservice)))
    return rows


# --------------------------------------------------------------------------- the safe band includes its bounds


@pytest.mark.parametrize(
    "cells_mv",
    [
        pytest.param([3400] * 15 + [3450], id="top cell at BLOCK_ON_DISCONNECT_VOLTAGE_MAX (top hold at 55.2 V)"),
        pytest.param([3225] + [3300] * 15, id="bottom cell at BLOCK_ON_DISCONNECT_VOLTAGE_MIN"),
    ],
)
def test_a_cell_exactly_at_a_configured_bound_is_inside_the_safe_band(env, cells_mv):
    # The bound cannot be set above MAX_CELL_VOLTAGE (utils.py clamps it), so with an exclusive '<' a pack whose top
    # cell sits at MAX_CELL_VOLTAGE, normal during every top hold at CVL 55.2 V, can never count as safe: the driver
    # exits after 60 s of RS485 silence instead of riding through 30 min, and both pack services leave D-Bus.
    clock, link, bat, helper, svc, loop = env
    link.answer(cells_mv)
    helper.publish_battery(loop)
    assert bat.online is True
    before = limits(svc)
    assert before[0] > 0 and before[1] > 0 and before[2:] == (1, 1)

    rows = go_silent(clock, link, helper, loop, 70)

    assert (helper.cell_voltages_good, loop.quit.called) == (True, False), (
        f"cells {min(cells_mv)}..{max(cells_mv)} mV with band 3.225..3.450 V: judged safe={helper.cell_voltages_good}, "
        f"driver quit after 70 s of silence={loop.quit.called} (documented: continue for 30 min)"
    )
    # documented ride-through: the last limits stay published
    assert [r for r in rows if r[1] != before] == []


@pytest.mark.parametrize(
    "cells_mv",
    [
        pytest.param([3400] * 15 + [3451], id="top cell 1 mV above the band"),
        pytest.param([3224] + [3300] * 15, id="bottom cell 1 mV below the band"),
    ],
)
def test_a_cell_outside_the_band_still_ends_in_an_exit_after_60_s(env, cells_mv):
    clock, link, bat, helper, svc, loop = env
    link.answer(cells_mv)
    helper.publish_battery(loop)

    go_silent(clock, link, helper, loop, 60)  # failed polls 0 .. 55 s after the first
    assert helper.cell_voltages_good is False
    loop.quit.assert_not_called()

    go_silent(clock, link, helper, loop, POLL)  # 60 s after the first failed poll
    loop.quit.assert_called_once()


def test_the_logged_band_check_matches_the_decision(env, monkeypatch):
    # dbushelper logs the comparison when the pack goes offline; it must print the comparison the decision used
    clock, link, bat, helper, svc, loop = env
    log = MagicMock()
    monkeypatch.setattr(dbushelper, "logger", log)
    link.answer([3225] + [3300] * 14 + [3450])
    helper.publish_battery(loop)

    go_silent(clock, link, helper, loop, 15)

    lines = [c.args[0] for c in log.error.call_args_list]
    assert any("Cell voltages are in a safe threshold" in line for line in lines), lines
    assert any("Min cell voltage: 3.225 >= Min Threshold: 3.225 --> OK" in line for line in lines), lines
    assert any("Max cell voltage: 3.450 <= Max threshold: 3.450 --> OK" in line for line in lines), lines


# --------------------------------------------------------------------------- blocking while the pack is silent

BLOCKED = (0, 0, 0, 0)
OFFLINE_AFTER = 10  # s after the first failed poll publish_battery marks the battery offline (RETRY_CYCLE_SHORT_COUNT)


def connect(clock, link, helper, loop, cells_mv):
    link.answer(cells_mv)
    helper.publish_battery(loop)
    svc = helper._dbusservice
    assert helper.battery.online is True and svc["/Connected"] == 1
    before = limits(svc)
    assert before[0] > 0 and before[1] > 0 and before[2:] == (1, 1), before
    return before


@pytest.mark.parametrize(
    "cells_mv",
    [
        pytest.param([3400] * 15 + [3460], id="top cell above the band"),
        pytest.param([3200] + [3300] * 15, id="bottom cell below the band"),
    ],
)
def test_offline_with_cells_outside_the_safe_band_blocks_charge_and_discharge(env, cells_mv):
    # config.default.ini: "If cell voltages are outside that range, the driver blocks charge and discharge and exits
    # after 60 seconds." manage_charge_and_discharge_current() only runs on a good read, so the last limits and
    # AllowTo* = 1 stayed on D-Bus for those 60 s, and dbus-aggregate-batteries (Controlling BMS) kept using them.
    clock, link, bat, helper, svc, loop = env
    before = connect(clock, link, helper, loop, cells_mv)

    rows = go_silent(clock, link, helper, loop, 60)  # failed polls 0 .. 55 s after the first

    assert helper.cell_voltages_good is False and bat.online is False
    loop.quit.assert_not_called()
    assert [r for r in rows if r[0] < OFFLINE_AFTER] == [(t, before) for t in (0, 5)]
    assert [r for r in rows if r[0] >= OFFLINE_AFTER] == [(t, BLOCKED) for t in range(OFFLINE_AFTER, 60, POLL)]
    assert svc["/Connected"] == 0

    go_silent(clock, link, helper, loop, POLL)  # 60 s: the documented exit, still blocked
    loop.quit.assert_called_once()
    assert limits(svc) == BLOCKED


def test_block_on_disconnect_blocks_charge_and_discharge_whatever_the_cells(env, monkeypatch):
    # config.default.ini, BLOCK_ON_DISCONNECT = True: "Charge and discharge are blocked immediately on BMS communication
    # loss." The driver can only tell a lost link from a missed reply after two failed polls, at the offline mark.
    monkeypatch.setattr(utils, "BLOCK_ON_DISCONNECT", True)
    clock, link, bat, helper, svc, loop = env
    before = connect(clock, link, helper, loop, [3300] * CELLS)  # well inside the band

    rows = go_silent(clock, link, helper, loop, 60)

    loop.quit.assert_not_called()
    assert [r for r in rows if r[0] < OFFLINE_AFTER] == [(t, before) for t in (0, 5)]
    assert [r for r in rows if r[0] >= OFFLINE_AFTER] == [(t, BLOCKED) for t in range(OFFLINE_AFTER, 60, POLL)]
    assert svc["/Connected"] == 0

    go_silent(clock, link, helper, loop, POLL)
    loop.quit.assert_called_once()


def test_offline_inside_the_safe_band_rides_through_until_the_timeout(env):
    # BLOCK_ON_DISCONNECT = False and cells in the band: "charge and discharge continue for
    # BLOCK_ON_DISCONNECT_TIMEOUT_MINUTES minutes (default: 30) before the driver exits"
    clock, link, bat, helper, svc, loop = env
    before = connect(clock, link, helper, loop, [3300] * CELLS)

    rows = go_silent(clock, link, helper, loop, 30 * 60)

    loop.quit.assert_not_called()
    assert [r for r in rows if r[1] != before] == []
    assert bat.online is False and svc["/Connected"] == 0  # the GUI shows the lost link even while riding through

    go_silent(clock, link, helper, loop, POLL)
    loop.quit.assert_called_once()


def test_limits_recover_from_zero_through_hold_and_ramp_after_a_blocked_outage(env, monkeypatch):
    # After the block the limits restart from 0 like after any other hard stop: CCL holds CCL_RECOVERY_HOLD_SEC
    # (120 s) and ramps at CCL_RECOVERY_RATE_A_PER_SEC (0.10 A/s), DCL holds DCL_RECOVERY_HOLD_SEC (30 s) and ramps at
    # DCL_RECOVERY_RATE_A_PER_SEC (5 A/s), so a link that is not stable again does not toggle the bank on and off.
    monkeypatch.setattr(utils, "BLOCK_ON_DISCONNECT", True)
    clock, link, bat, helper, svc, loop = env
    cells = [3300] * CELLS  # CCL 70 A, DCL 140 A from the cell-voltage tables
    assert connect(clock, link, helper, loop, cells) == (70, 140, 1, 1)
    go_silent(clock, link, helper, loop, 30)
    assert limits(svc) == BLOCKED

    link.answer(cells)
    t_back = clock.t + POLL
    seen = {}
    for _ in range(900 // POLL):
        clock.t += POLL
        helper.publish_battery(loop)
        seen[clock.t - t_back] = (bat.online, svc["/Connected"]) + limits(svc)
    loop.quit.assert_not_called()

    assert seen[0] == (True, 1) + BLOCKED  # /Connected follows online at once, the limits do not
    assert all(seen[t][2] == 0 and seen[t][4] == 0 for t in seen if t < 120)  # CCL hold
    assert all(seen[t][3] == 0 and seen[t][5] == 0 for t in seen if t < 30)  # DCL hold
    assert seen[60][3:] == (140, 0, 1)  # DCL back in full 28 s after its hold
    assert seen[200][2] == pytest.approx(8.0) and seen[200][4] == 1  # CCL ramping: 80 s at 0.10 A/s
    assert seen[895][2:] == (70, 140, 1, 1)  # CCL back in full 700 s after its hold


def test_a_pack_that_answers_briefly_between_silences_is_blocked_again_at_once(env):
    # The offline mark is set once per error episode (error["cleared"] only returns after 60 s without errors), so a
    # pack that answered for a while in between is not marked offline again; its next failed poll must still block.
    clock, link, bat, helper, svc, loop = env
    cells = [3400] * 15 + [3460]  # top cell above the band
    connect(clock, link, helper, loop, cells)
    go_silent(clock, link, helper, loop, 15)  # failed polls 0, 5, 10 s: offline and blocked
    assert limits(svc) == BLOCKED

    link.answer(cells)
    for _ in range(8):  # good polls 15 .. 50 s: DCL holds 30 s, then ramps
        clock.t += POLL
        helper.publish_battery(loop)
    assert svc["/Info/MaxDischargeCurrent"] > 0 and svc["/Io/AllowToDischarge"] == 1

    go_silent(clock, link, helper, loop, POLL)  # 55 s after the first failed poll of this episode
    loop.quit.assert_not_called()
    assert limits(svc) == BLOCKED
