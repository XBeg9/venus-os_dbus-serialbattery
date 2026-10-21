"""Which SoC the TimeToGo recalculation in publish_dbus counts down to, and what it costs on D-Bus.

The SoC comes from four settings of com.victronenergy.settings (localsettings): /Settings/CGwacs/Hub4Mode and
/Settings/CGwacs/BatteryLife/{State,SocLimit,MinimumSocLimit}. Measured on the Cerbo GX (2026-10-21): walking
/Settings/CGwacs/BatteryLife and /Settings/CGwacs/Hub4Mode with get_settings_with_values() introspects every node below
them, 271 ms CPU and about 1 s wall per pack once a minute; four GetValue calls without introspection take 7.6 ms.

The real DbusHelper.publish_dbus runs on a real (fresh) Seplos battery; faked are the settings service (LocalSettings,
recording every call) and the VeDbusService (a dict). The behaviour tests pass on the introspecting walk and on the
direct reads alike.
"""

from unittest.mock import MagicMock

import dbus
import pytest

import dbushelper
import utils
from battery import Cell
from bms.seplos import Seplos
from dbushelper import VICTRON_SETTINGS_DBUS_NAME, DbusHelper, _CachedDbusProxy

HUB4MODE = "/Settings/CGwacs/Hub4Mode"
STATE = "/Settings/CGwacs/BatteryLife/State"
SOC_LIMIT = "/Settings/CGwacs/BatteryLife/SocLimit"
MINIMUM_SOC_LIMIT = "/Settings/CGwacs/BatteryLife/MinimumSocLimit"
LEAVES = (HUB4MODE, STATE, SOC_LIMIT, MINIMUM_SOC_LIMIT)

UNKNOWN_OBJECT = "org.freedesktop.DBus.Error.UnknownObject"
UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
BUS_ITEM = "com.victronenergy.BusItem"
INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"
SOC_LOW_WARNING = 20.0

# the battery under test: 280 Ah at 60 % SoC, discharging at 28 A
SOC = 60.0
PERCENT_PER_SECOND = abs(-28.0 / (280.0 / 100)) / 3600


def ess(hub4mode=1, state=10, soc_limit=35.0, minimum_soc_limit=10.0):
    """The settings localsettings exports with ESS configured, typed as it sends them."""
    return {
        HUB4MODE: dbus.Int32(hub4mode),
        STATE: dbus.Int32(state),
        SOC_LIMIT: dbus.Double(soc_limit),
        MINIMUM_SOC_LIMIT: dbus.Double(minimum_soc_limit),
        # other settings below /Settings/CGwacs/BatteryLife, which TimeToGo does not use
        "/Settings/CGwacs/BatteryLife/DischargedTime": dbus.Int32(0),
        "/Settings/CGwacs/BatteryLife/Flags": dbus.Int32(0),
        "/Settings/CGwacs/BatteryLife/Schedule/Charge/0/Day": dbus.Int32(-7),
        "/Settings/CGwacs/BatteryLife/Schedule/Charge/0/Soc": dbus.Int32(100),
    }


class LocalSettings:
    """com.victronenergy.settings as localsettings exports it, recording every D-Bus call made to it.

    Every setting and every group is an object with GetValue on com.victronenergy.BusItem (a group answers with a
    dictionary) and with the com.victronenergy.Settings interface. Introspect lists those interfaces and the child
    nodes. A path nothing is exported at introspects as a node without them, and GetValue there fails with
    `missing_error`, as libdbus answers (UnknownObject; UnknownMethod from older libdbus).

    On an exported object a method is looked up by interface and name, as dbus-python's service does: GetValue exists
    only on com.victronenergy.BusItem and Introspect only on org.freedesktop.DBus.Introspectable; anything else fails
    with UnknownMethod. Stricter than dbus-python, a call without an interface fails too: neither the walk nor the
    direct read sends one.
    """

    def __init__(self, values, failing=None, missing_error=UNKNOWN_OBJECT, unreachable=None):
        self.values = values  # path -> D-Bus value
        self.failing = failing or {}  # path -> error name GetValue raises on that exported setting
        self.missing_error = missing_error
        self.unreachable = unreachable  # (where, error name): the service is gone ("get_object") or does not answer ("call")
        self.get_objects = []  # (path, introspect)
        self.calls = []  # (member, interface, path)

    def exported(self, path):
        return path in self.values or path in self.failing or any(p.startswith(path + "/") for p in (*self.values, *self.failing))

    def get_object(self, service, object_path, introspect=True, follow_name_owner_changes=False):
        assert service == VICTRON_SETTINGS_DBUS_NAME
        self.get_objects.append((object_path, introspect))
        if self.unreachable and self.unreachable[0] == "get_object":
            # dbus-python resolves the owner of the well-known name here
            raise dbus.exceptions.DBusException(f"The name {service} was not provided by any .service files", name=self.unreachable[1])
        if introspect:
            # dbus-python's ProxyObject introspects the object itself unless told not to
            self.calls.append(("Introspect", INTROSPECTABLE, object_path))
        return Proxy(self, object_path)

    def call(self, member, interface, path):
        self.calls.append((member, interface, path))
        if self.unreachable and self.unreachable[0] == "call":
            raise dbus.exceptions.DBusException("Did not receive a reply", name=self.unreachable[1])
        if not self.exported(path):
            # libdbus answers for a path without an object, whatever the interface
            if (member, interface) == ("Introspect", INTROSPECTABLE):
                return self.introspect(path)
            raise dbus.exceptions.DBusException(f"No such object path '{path}'", name=self.missing_error)
        if (member, interface) == ("Introspect", INTROSPECTABLE):
            return self.introspect(path)
        if (member, interface) == ("GetValue", BUS_ITEM):
            if path in self.failing:
                raise dbus.exceptions.DBusException(f"GetValue failed on {path}", name=self.failing[path])
            if path in self.values:
                return self.values[path]
            prefix = path + "/"
            return dbus.Dictionary({p[len(prefix) :]: v for p, v in self.values.items() if p.startswith(prefix)})
        raise dbus.exceptions.DBusException(f"Unknown method: {member} is not a valid method of interface {interface}", name=UNKNOWN_METHOD)

    def introspect(self, path):
        prefix = path + "/"
        children = sorted({p[len(prefix) :].split("/")[0] for p in (*self.values, *self.failing) if p.startswith(prefix)})
        xml = '<node><interface name="org.freedesktop.DBus.Introspectable"/>'
        if self.exported(path):
            xml += '<interface name="com.victronenergy.BusItem"/><interface name="com.victronenergy.Settings"/>'
        return xml + "".join(f'<node name="{child}"/>' for child in children) + "</node>"


class Proxy:
    def __init__(self, bus, path):
        self.bus = bus
        self.path = path

    def get_dbus_method(self, member, dbus_interface=None):
        return lambda *args: self.bus.call(member, dbus_interface, self.path)


@pytest.fixture
def publish(monkeypatch):
    """Runs one publish_dbus that recalculates TimeToGo against `settings`; returns battery, helper and published values."""
    for name, value in {
        "TIME_TO_GO_ENABLE": True,
        "TIME_TO_SOC_POINTS": [],
        "TIME_TO_SOC_RECALCULATE_EVERY": 60,
        "SOC_LOW_WARNING": SOC_LOW_WARNING,
        "HISTORY_ENABLE": False,
        "PUBLISH_BATTERY_DATA_AS_JSON": False,
    }.items():
        assert hasattr(utils, name), name
        monkeypatch.setattr(utils, name, value)
    logger = MagicMock()
    monkeypatch.setattr(dbushelper, "logger", logger)

    def run(settings):
        monkeypatch.setattr(dbushelper, "get_bus", lambda name: settings if name == VICTRON_SETTINGS_DBUS_NAME else MagicMock())
        bat = Seplos("/dev/ttyUSB1", 19200, b"\x00")
        bat.cell_count = 16
        bat.cells = [Cell(False) for _ in range(16)]
        bat.capacity = 280.0
        bat.soc_calc = SOC
        bat.current_calc = -28.0  # discharging, so TimeToGo counts down to the SoC under test
        monkeypatch.setattr(bat, "get_time_to_soc", MagicMock(wraps=bat.get_time_to_soc))
        monkeypatch.setattr(bat, "manage_error_code", MagicMock(wraps=bat.manage_error_code))
        helper = DbusHelper(bat)
        svc = {"/Connected": 1, "/Mode": 3, "/TimeToGo": 4321, "/TimeToSoC/10": "1234"}
        helper._dbusservice = _CachedDbusProxy(svc)
        monkeypatch.setattr(helper, "save_current_battery_state", lambda: None)
        helper.last_seen_saved_last_time = 2**40  # no LastSeen refresh in this poll
        assert bat.time_to_soc_update == 0  # so this poll recalculates

        helper.publish_dbus()

        return bat, logger, svc

    return run


def time_to_go_targets(bat):
    """The SoC each TimeToGo calculation of the poll counted down to (TimeToSoC points call without only_number)."""
    return [c.args[0] for c in bat.get_time_to_soc.call_args_list if c.args[2:] == (True,)]


# --------------------------------------------------------------------------- cost


def test_time_to_go_reads_its_four_settings_without_introspection(publish):
    settings = LocalSettings(ess(hub4mode=1, state=10, minimum_soc_limit=10.0))

    bat, logger, svc = publish(settings)

    introspects = [path for member, interface, path in settings.calls if member == "Introspect"]
    assert introspects == [], f"one TimeToGo recalculation introspected {len(introspects)} settings objects: {introspects}"
    assert sorted(settings.get_objects) == sorted((leaf, False) for leaf in LEAVES)
    # on another interface localsettings answers UnknownMethod, which reads as a missing setting
    assert sorted(settings.calls) == sorted(("GetValue", BUS_ITEM, leaf) for leaf in LEAVES)
    assert time_to_go_targets(bat) == [10], "the poll must have recalculated TimeToGo"


@pytest.mark.parametrize("value,text", [(dbus.Int32(10), "10"), (dbus.Double(15.0), "15.0")], ids=["Int32", "Double"])
def test_a_setting_is_read_as_the_text_the_walk_stored(value, text):
    # get_settings_with_values() stored str(value) and the TimeToGo block parses that text (int("10"), int(float("15.0")));
    # on the Cerbo, str() of python-dbus's Int32/Double gives "10" and "80.0"
    settings = LocalSettings({STATE: value})
    helper = DbusHelper(Seplos("/dev/ttyUSB1", 19200, b"\x00"))

    assert helper.get_setting_value(settings, VICTRON_SETTINGS_DBUS_NAME, STATE) == text


# --------------------------------------------------------------------------- which SoC TimeToGo counts down to


@pytest.mark.parametrize(
    "hub4mode,state,expected",
    [
        pytest.param(1, 10, 10, id="optimized without BatteryLife (state 10): MinimumSocLimit"),
        pytest.param(1, 11, 10, id="optimized without BatteryLife (state 11): MinimumSocLimit"),
        pytest.param(1, 12, 10, id="optimized without BatteryLife (state 12): MinimumSocLimit"),
        pytest.param(1, 2, 35, id="optimized with BatteryLife (state 2): SocLimit"),
        pytest.param(1, 13, 35, id="state 13 is outside 10..12: SocLimit"),
        pytest.param(1, 9, SOC_LOW_WARNING, id="keep batteries charged (state 9): SOC_LOW_WARNING"),
        pytest.param(2, 10, SOC_LOW_WARNING, id="external control (Hub4Mode 2): SOC_LOW_WARNING"),
        pytest.param(3, 2, SOC_LOW_WARNING, id="ESS off (Hub4Mode 3): SOC_LOW_WARNING"),
    ],
)
def test_time_to_go_counts_down_to_the_soc_of_the_ess_mode(publish, hub4mode, state, expected):
    bat, logger, svc = publish(LocalSettings(ess(hub4mode=hub4mode, state=state, soc_limit=35.0, minimum_soc_limit=10.0)))

    assert time_to_go_targets(bat) == [expected]
    assert svc["/TimeToGo"] == int((SOC - expected) / PERCENT_PER_SECOND)
    bat.manage_error_code.assert_not_called()
    logger.error.assert_not_called()


@pytest.mark.parametrize(
    "soc_limit,minimum_soc_limit,state,expected",
    [
        pytest.param(dbus.Double(35.0), dbus.Double(15.5), 10, 15, id="MinimumSocLimit 15.5 -> 15"),
        pytest.param(dbus.Double(35.9), dbus.Double(10.0), 2, 35, id="SocLimit 35.9 -> 35"),
        pytest.param(dbus.Int32(35), dbus.Double(10.0), 2, 35, id="SocLimit as Int32"),
    ],
)
def test_soc_limits_are_truncated_to_whole_percent(publish, soc_limit, minimum_soc_limit, state, expected):
    values = ess(hub4mode=1, state=state)
    values[SOC_LIMIT] = soc_limit
    values[MINIMUM_SOC_LIMIT] = minimum_soc_limit

    bat, logger, svc = publish(LocalSettings(values))

    assert time_to_go_targets(bat) == [expected]
    bat.manage_error_code.assert_not_called()


# --------------------------------------------------------------------------- settings that do not exist


@pytest.mark.parametrize("missing_error", [UNKNOWN_OBJECT, UNKNOWN_METHOD, "org.freedesktop.DBus.Error.UnknownInterface"])
@pytest.mark.parametrize(
    "absent",
    [
        pytest.param(LEAVES, id="no ESS settings at all"),
        pytest.param((MINIMUM_SOC_LIMIT,), id="MinimumSocLimit missing"),
        pytest.param((HUB4MODE,), id="Hub4Mode missing"),
    ],
)
def test_a_missing_setting_falls_back_to_soc_low_warning_without_an_error(publish, absent, missing_error):
    values = {path: value for path, value in ess(hub4mode=1, state=10).items() if path not in absent}

    bat, logger, svc = publish(LocalSettings(values, missing_error=missing_error))

    assert time_to_go_targets(bat) == [SOC_LOW_WARNING]
    assert svc["/TimeToGo"] is not None
    bat.manage_error_code.assert_not_called()
    logger.error.assert_not_called()


# --------------------------------------------------------------------------- errors


@pytest.mark.parametrize("error", ["org.freedesktop.DBus.Python.KeyError", "org.freedesktop.DBus.Error.AccessDenied"])
def test_get_value_failing_on_an_existing_setting_sets_error_8_and_falls_back(publish, error):
    settings = LocalSettings(ess(hub4mode=1, state=10), failing={HUB4MODE: error})

    bat, logger, svc = publish(settings)

    bat.manage_error_code.assert_called_once_with(8)
    assert logger.error.call_count == 1
    assert "Non blocking exception" not in logger.error.call_args.args[0]
    # the poll carries on with the other settings: TimeToGo is recalculated with the fallback
    assert time_to_go_targets(bat) == [SOC_LOW_WARNING]
    assert svc["/TimeToGo"] != 4321


@pytest.mark.parametrize(
    "unreachable",
    [
        pytest.param(("get_object", "org.freedesktop.DBus.Error.ServiceUnknown"), id="settings service gone"),
        # every name listed here, not read from dbushelper.DBUS_ERRORS_SERVICE_UNREACHABLE: a name dropped from or
        # misspelled in that tuple must fail here
        pytest.param(("call", "org.freedesktop.DBus.Error.ServiceUnknown"), id="settings service restarted"),
        pytest.param(("call", "org.freedesktop.DBus.Error.NameHasNoOwner"), id="settings service has no owner"),
        pytest.param(("call", "org.freedesktop.DBus.Error.NoReply"), id="settings service does not answer"),
        pytest.param(("call", "org.freedesktop.DBus.Error.Timeout"), id="call timed out (Timeout)"),
        pytest.param(("call", "org.freedesktop.DBus.Error.TimedOut"), id="call timed out (TimedOut)"),
        pytest.param(("call", "org.freedesktop.DBus.Error.Disconnected"), id="bus connection lost"),
        pytest.param(("call", "org.freedesktop.DBus.Error.NoServer"), id="no bus server"),
    ],
)
def test_an_unreachable_settings_service_skips_this_recalculation(publish, monkeypatch, unreachable):
    # Today Introspect raises these, before any GetValue, and the except around the TimeToGo/TimeToSoC update handles
    # them: error code 8 once, "Non blocking exception" logged, TimeToGo and TimeToSoC keep their values until the next
    # recalculation a minute later.
    monkeypatch.setattr(utils, "TIME_TO_SOC_POINTS", [10])

    bat, logger, svc = publish(LocalSettings(ess(hub4mode=1, state=10), unreachable=unreachable))

    bat.manage_error_code.assert_called_once_with(8)
    assert logger.error.call_count == 1
    assert logger.error.call_args.args[0].startswith("Non blocking exception occurred: ")
    assert time_to_go_targets(bat) == []
    assert (svc["/TimeToGo"], svc["/TimeToSoC/10"]) == (4321, "1234")
    assert bat.time_to_soc_update != 0  # next attempt after TIME_TO_SOC_RECALCULATE_EVERY, as before
