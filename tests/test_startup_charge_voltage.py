"""
/Info/MaxChargeVoltage must be a number from the moment the service is on D-Bus (review 2026-10-21, #11).

main() runs get_battery() (test_connection: get_settings + refresh_data), then setup_vedbus(), which registers the
service with /Connected = 1, and the first poll comes poll_interval (5 s) later. setup_vedbus registered
/Info/MaxChargeVoltage with battery.max_battery_voltage, which only manage_charge_voltage sets, on the first good poll,
and publish_dbus wrote None while battery.control_voltage was None, so a failed first poll published None as well.
dbus-aggregate-batteries reads every pack's /Info/MaxChargeVoltage and cannot form the bank CVL while one is None.

/Info/ChargeMode stays None until the first good poll on purpose: dbus-aggregate-batteries (KEEP_MAX_CVL) counts a
charge mode that is not a string like "Cell OVP" and takes the lowest pack CVL, the safe choice while no cell voltage is
known. A placeholder string would let it take the highest CVL before the driver has seen a single cell.

The real Seplos detection, DbusHelper.setup_vedbus/publish_battery/publish_dbus run; faked are the serial transport
(Seplos.read_serial_data_seplos), the clock, the settings and the VeDbusService (records the registered values).
"""

from unittest.mock import MagicMock

import pytest

import battery as battery_module
import dbushelper
import utils
from bms.seplos import Seplos
from dbushelper import DbusHelper, _CachedDbusProxy

CELLS = 16


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def status_info(cells_mv):
    """150-char info of a 0x42 reply, offsets from Seplos.decode_status_data."""
    s = "0000" + "%02X" % len(cells_mv)
    s += "".join("%04X" % mv for mv in cells_mv)
    s += "06" + "".join("%04X" % 2981 for _ in range(6))
    s += "%04X" % 0 + "%04X" % round(sum(cells_mv) / 10) + "%04X" % 20000 + "0A" + "%04X" % 28000 + "%04X" % 900 + "0000" + "%04X" % 12
    return s.ljust(150, "0").encode("ascii")


def alarm_info():
    b = bytearray(49)
    b[35] = 0x03  # charge and discharge switches on
    return b.hex().upper().encode("ascii")


class Link:
    """Replaces Seplos.read_serial_data_seplos: the info bytes of a reply, or False like a timed-out read."""

    def __init__(self):
        self.status = None
        self.alarm = None

    def __call__(self, command):
        return self.status if command[7:9] == b"42" else self.alarm


class RecordingService:
    """Stands in for VeDbusService: add_path records the registered value, item writes replace it."""

    def __init__(self):
        self.values = {}

    def add_path(self, path, value=None, **kwargs):
        self.values[path] = value

    def __setitem__(self, path, value):
        self.values[path] = value

    def __getitem__(self, path):
        return self.values[path]

    def register(self):
        pass


OUR_CONFIG = {
    "MAX_CELL_VOLTAGE": 3.45,
    "FLOAT_CELL_VOLTAGE": 3.375,
    "MIN_CELL_VOLTAGE": 2.9,
    "MAX_BATTERY_CHARGE_CURRENT": 70.0,
    "MAX_BATTERY_DISCHARGE_CURRENT": 140.0,
    "CVCM_ENABLE": True,
    "CVL_CONTROLLER_MODE": 1,
    "SWITCH_TO_FLOAT_WAIT_FOR_SEC": 900,
    "SOC_RESET_AFTER_DAYS": False,
    "SOC_CALCULATION": False,
    "VOLTAGE_DROP": 0.0,
    "BLOCK_ON_DISCONNECT": False,
    "BLOCK_ON_DISCONNECT_TIMEOUT_MINUTES": 30,
    "GUI_PARAMETERS_SHOW_ADDITIONAL_INFO": False,
    "TELEMETRY": False,
    "TIME_TO_GO_ENABLE": False,
    "PUBLISH_BATTERY_DATA_AS_JSON": False,
    "HISTORY_ENABLE": False,
    "CAPTURE_RAW_DATA": False,
    "EXTERNAL_SENSOR_DBUS_DEVICE": None,
    "EXTERNAL_SENSOR_DBUS_PATH_CURRENT": None,
    "EXTERNAL_SENSOR_DBUS_PATH_SOC": None,
}


@pytest.fixture
def started(monkeypatch):
    """A pack detected as in main() and its service registered by setup_vedbus, before the first poll."""
    clock = Clock(1_000_000)
    monkeypatch.setattr(battery_module, "time", clock)
    monkeypatch.setattr(dbushelper, "time", clock)
    for name, value in OUR_CONFIG.items():
        assert hasattr(utils, name), name
        monkeypatch.setattr(utils, name, value)
    link = Link()
    monkeypatch.setattr(Seplos, "read_serial_data_seplos", link)

    bat = Seplos("/dev/ttyUSB1", 19200, b"\x00")
    link.status = status_info([3400] * CELLS)
    link.alarm = alarm_info()
    assert bat.test_connection() is True  # the real detection path, as in main()

    helper = DbusHelper(bat)
    rec = RecordingService()
    helper._dbusservice = _CachedDbusProxy(rec)
    helper.settings = MagicMock()
    helper.settings.__getitem__.side_effect = lambda key: ""
    monkeypatch.setattr(helper, "setup_instance", lambda: True)
    monkeypatch.setattr(helper, "save_current_battery_state", lambda: None)
    helper.last_seen_saved_last_time = clock.t
    assert helper.setup_vedbus() is True
    bat.set_calculated_data()  # as main() does right after setup_vedbus
    return clock, link, bat, helper, rec.values


def test_the_service_is_registered_with_the_configured_max_charge_voltage(started):
    clock, link, bat, helper, values = started

    assert values["/Connected"] == 1
    assert values["/Info/MaxChargeVoltage"] == pytest.approx(55.2), "registered with /Info/MaxChargeVoltage = None"
    assert values["/Info/MaxChargeCellVoltage"] == pytest.approx(3.45), "registered with /Info/MaxChargeCellVoltage = None"
    # unknown until the first good poll, so dbus-aggregate-batteries takes the lowest pack CVL (see module docstring)
    assert values["/Info/ChargeMode"] is None


def test_a_failed_first_poll_keeps_the_registered_charge_voltage(started):
    # detection succeeded seconds ago; the first poll times out
    clock, link, bat, helper, values = started
    link.status = link.alarm = False
    loop = MagicMock()
    clock.t += 5
    helper.publish_battery(loop)
    loop.quit.assert_not_called()

    assert values["/Connected"] == 1
    assert values["/Info/MaxChargeVoltage"] == pytest.approx(55.2), "a failed first poll published /Info/MaxChargeVoltage = None"
    assert values["/Info/MaxChargeCellVoltage"] == pytest.approx(3.45), "a failed first poll published /Info/MaxChargeCellVoltage = None"
    # unknown until the first good poll, so dbus-aggregate-batteries takes the lowest pack CVL (see module docstring)
    assert values["/Info/ChargeMode"] is None


def test_the_first_good_poll_publishes_the_computed_charge_voltage_and_mode(started):
    # control: once a poll has the cells, the CVL and the charge mode come from manage_charge_voltage
    clock, link, bat, helper, values = started
    link.status = status_info([3470] + [3440] * 15)  # one cell 20 mV over the limit: P-controller CVL 55.07 - 0.02
    loop = MagicMock()
    clock.t += 5
    helper.publish_battery(loop)
    loop.quit.assert_not_called()

    assert bat.control_voltage == pytest.approx(55.05)
    assert values["/Info/MaxChargeVoltage"] == pytest.approx(55.05)
    assert values["/Info/MaxChargeCellVoltage"] == pytest.approx(3.45)
    assert values["/Info/ChargeMode"].startswith("Bulk (Cell OVP)")
