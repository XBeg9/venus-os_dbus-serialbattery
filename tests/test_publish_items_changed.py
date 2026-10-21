"""
One poll reaches D-Bus as one ItemsChanged per pack, not as one PropertiesChanged per changed path (CPU review
2026-10-21).

publish_dbus wrote every path through VeDbusService.__setitem__, which emits one PropertiesChanged per changed path:
41 signals and 38 ms CPU per poll for our two packs on the Cerbo. Inside a velib ServiceContext (vedbus.py:182-217) the
same writes go out as one ItemsChanged per pack: 2 signals and 21.5 ms. Per-path signals also let a consumer act
between two paths of the same poll: dbus-aggregate-batteries was seen with /Info/MaxChargeVoltage and /Info/ChargeMode
from different polls, and it picks the bank CVL from both (KEEP_MAX_CVL: lowest pack CVL while a ChargeMode says
"Cell OVP").

The real Seplos detection and DbusHelper.setup_vedbus/publish_battery/publish_dbus run; faked are the serial transport
(Seplos.read_serial_data_seplos), the clock, the settings and the VeDbusService. The real vedbus needs dbus-python,
which conftest stubs, so FakeVeDbusService mirrors the parts of vedbus.py that decide which signals go out.
"""

from collections import Counter
from unittest.mock import MagicMock

import pytest

import battery as battery_module
import dbushelper
import utils
from bms.seplos import Seplos
from dbushelper import DbusHelper, _CachedDbusProxy

CELLS = 16
POLL = 5  # s, our poll interval


# --------------------------------------------------------------------------- velib, as far as the signals go


class FakeItem:
    """VeDbusItemExport._local_set_value (vedbus.py:538-546): a change record only when the value differs."""

    def __init__(self, value):
        self.value = value

    def _local_set_value(self, newvalue):
        if self.value == newvalue:
            return None

        self.value = newvalue
        return {"Value": newvalue, "Text": "---" if newvalue is None else str(newvalue)}


class FakeRootExport:
    """VeDbusRootExport (vedbus.py:479-483): the ItemsChanged signal on '/'."""

    def __init__(self, signals):
        self.signals = signals

    def ItemsChanged(self, changes):
        self.signals.append(("ItemsChanged", dict(changes)))


class FakeServiceContext:
    """vedbus.ServiceContext (vedbus.py:193-212): collects the changes and emits them in one ItemsChanged on flush."""

    def __init__(self, parent):
        self.parent = parent
        self.changes = {}

    def __contains__(self, path):
        return path in self.parent

    def __getitem__(self, path):
        return self.parent[path]

    def __setitem__(self, path, newvalue):
        self.parent.received.append(path)
        c = self.parent._dbusobjects[path]._local_set_value(newvalue)
        if c is not None:
            self.changes[path] = c

    def flush(self):
        if self.changes:
            self.parent.root.ItemsChanged(self.changes)
            self.changes.clear()


class FakeVeDbusService:
    """
    vedbus.VeDbusService (vedbus.py:60-80, 169-191). A write outside a context emits one PropertiesChanged if the value
    changed (local_set_value, vedbus.py:533-536); __enter__ pushes a ServiceContext, __exit__ pops it and flushes it,
    also when the with-body raised. An unregistered path raises KeyError, so setup_vedbus has to add it first.
    """

    def __init__(self):
        self._dbusobjects = {}
        self._ratelimiters = []
        self.signals = []  # ("PropertiesChanged", path, change) or ("ItemsChanged", {path: change})
        self.received = []  # every write that reached the service or its context, changed or not
        self.root = FakeRootExport(self.signals)

    def add_path(self, path, value=None, *args, **kwargs):
        self._dbusobjects[path] = FakeItem(value)

    def register(self):
        pass

    def __getitem__(self, path):
        return self._dbusobjects[path].value

    def __setitem__(self, path, newvalue):
        self.received.append(path)
        changes = self._dbusobjects[path]._local_set_value(newvalue)
        if changes is not None:
            self.signals.append(("PropertiesChanged", path, changes))

    def __contains__(self, path):
        return path in self._dbusobjects

    def __enter__(self):
        context = FakeServiceContext(self)
        self._ratelimiters.append(context)
        return context

    def __exit__(self, *exc):
        if self._ratelimiters:
            self._ratelimiters.pop().flush()


def kinds(svc):
    return dict(Counter(s[0] for s in svc.signals))


# --------------------------------------------------------------------------- a pack detected and polled as in main()


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


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


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
    "CVCM_ENABLE": True,
    "CVL_CONTROLLER_MODE": 1,
    "CVL_RECOVERY_HOLD_SEC": 60,
    "CVL_RECOVERY_RATE_V_PER_SEC": 0.001,
    "SWITCH_TO_FLOAT_WAIT_FOR_SEC": 900,
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
    "VOLTAGE_DROP": 0.0,
    "BLOCK_ON_DISCONNECT": False,
    "BLOCK_ON_DISCONNECT_TIMEOUT_MINUTES": 30,
    "BLOCK_ON_DISCONNECT_VOLTAGE_MIN": 3.225,
    "BLOCK_ON_DISCONNECT_VOLTAGE_MAX": 3.45,
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
def started(monkeypatch):
    """A pack detected as in main() and its service set up by setup_vedbus, before the first poll."""
    clock = Clock(1_000_000)
    monkeypatch.setattr(battery_module, "time", clock)
    monkeypatch.setattr(dbushelper, "time", clock)
    for name, value in OUR_CONFIG.items():
        assert hasattr(utils, name), name
        monkeypatch.setattr(utils, name, value)
    link = Link()
    monkeypatch.setattr(Seplos, "read_serial_data_seplos", link)

    bat = Seplos("/dev/ttyUSB1", 19200, b"\x00")
    link.answer([3400] * CELLS)
    assert bat.test_connection() is True  # the real detection path, as in main()

    helper = DbusHelper(bat)
    svc = FakeVeDbusService()
    helper._dbusservice = _CachedDbusProxy(svc)
    helper.settings = MagicMock()
    helper.settings.__getitem__.side_effect = lambda key: ""
    monkeypatch.setattr(helper, "setup_instance", lambda: True)
    monkeypatch.setattr(helper, "save_current_battery_state", lambda: None)
    helper.last_seen_saved_last_time = clock.t
    assert helper.setup_vedbus() is True
    bat.set_calculated_data()  # as main() does right after setup_vedbus
    assert svc.signals == []  # add_path only registers
    return clock, link, bat, helper, svc, MagicMock()


LIMITS = ("/Info/MaxChargeCurrent", "/Info/MaxDischargeCurrent", "/Io/AllowToCharge", "/Io/AllowToDischarge")


def test_one_poll_goes_out_as_one_items_changed_with_the_charge_voltage_and_its_mode(started):
    clock, link, bat, helper, svc, loop = started
    link.answer([3470] + [3440] * 15)  # one cell 20 mV over the limit: CVL 55.2 -> 55.05 V, ChargeMode None -> Cell OVP
    clock.t += POLL
    helper.publish_battery(loop)
    loop.quit.assert_not_called()

    assert kinds(svc) == {"ItemsChanged": 1}, f"one poll of one pack emitted {kinds(svc)}, so consumers can act between its paths"
    ((_, changes),) = svc.signals
    assert changes["/Info/MaxChargeVoltage"]["Value"] == pytest.approx(55.05)
    assert changes["/Info/ChargeMode"]["Value"].startswith("Bulk (Cell OVP)")
    # the signal carries the state the service ends the poll with
    assert {p: c["Value"] for p, c in changes.items()} == {p: svc[p] for p in changes}


def test_the_poll_that_blocks_a_silent_pack_goes_out_as_one_items_changed(started, monkeypatch):
    # The block from 2a9db81/11bbcbc sets CCL/DCL and AllowTo* to 0 and /Connected to 0 at the third failed poll (10 s);
    # dbus-aggregate-batteries must not see the pack offline with its old limits, or blocked but still connected.
    monkeypatch.setattr(utils, "BLOCK_ON_DISCONNECT", True)
    clock, link, bat, helper, svc, loop = started
    clock.t += POLL
    helper.publish_battery(loop)
    before = tuple(svc[p] for p in LIMITS)
    assert svc["/Connected"] == 1 and before[0] > 0 and before[1] > 0 and before[2:] == (1, 1), before

    link.silence()
    for _ in range(2):  # failed polls 0 and 5 s after the first: not offline yet
        clock.t += POLL
        helper.publish_battery(loop)
    assert tuple(svc[p] for p in LIMITS) == before
    svc.signals.clear()
    clock.t += POLL  # 10 s: marked offline and blocked
    helper.publish_battery(loop)
    loop.quit.assert_not_called()

    assert kinds(svc) == {"ItemsChanged": 1}, f"the blocking poll emitted {kinds(svc)}, so consumers can act between its paths"
    ((_, changes),) = svc.signals
    assert {p: changes[p]["Value"] for p in ("/Connected",) + LIMITS} == dict.fromkeys(("/Connected",) + LIMITS, 0)


# --------------------------------------------------------------------------- _CachedDbusProxy batches


@pytest.fixture
def svc():
    s = FakeVeDbusService()
    for path in ("/A", "/B"):
        s.add_path(path, None)
    return s


@pytest.fixture
def proxy(svc):
    return _CachedDbusProxy(svc)


def test_outside_a_batch_every_changed_path_emits_its_own_properties_changed(proxy, svc):
    proxy["/A"] = 1
    proxy["/B"] = 2
    proxy["/B"] = 2
    assert svc.signals == [
        ("PropertiesChanged", "/A", {"Value": 1, "Text": "1"}),
        ("PropertiesChanged", "/B", {"Value": 2, "Text": "2"}),
    ]


def test_a_batch_emits_one_items_changed_with_every_changed_path_at_its_end(proxy, svc):
    with proxy:
        proxy["/A"] = 1
        proxy["/B"] = 2
        assert svc.signals == []
    assert svc.signals == [("ItemsChanged", {"/A": {"Value": 1, "Text": "1"}, "/B": {"Value": 2, "Text": "2"}})]
    assert svc._ratelimiters == []


def test_a_batch_without_changes_emits_nothing(proxy, svc):
    proxy["/A"] = 1
    svc.signals.clear()
    with proxy:
        proxy["/A"] = 1
    assert svc.signals == []


def test_unchanged_writes_inside_a_batch_are_still_suppressed_by_the_cache(proxy, svc):
    proxy["/A"] = 1
    with proxy:
        proxy["/A"] = 1
        proxy["/A"] = 1
        proxy["/B"] = 2
        proxy["/B"] = 2
    assert svc.received == ["/A", "/B"]  # the repeats never reached the service
    assert svc.signals[1:] == [("ItemsChanged", {"/B": {"Value": 2, "Text": "2"}})]


def test_reads_inside_a_batch_return_the_new_values(proxy, svc):
    # publish_dbus reads /Mode back after writing it, and the JSON export reads every value through GetItems
    with proxy:
        proxy["/A"] = 5
        assert proxy["/A"] == 5
        assert svc.signals == []


def test_service_attributes_reach_the_service_during_a_batch(proxy, svc):
    with proxy:
        proxy.add_path("/C", 3)
        assert "/C" in proxy and "/D" not in proxy
        assert proxy._dbusobjects is svc._dbusobjects
        proxy["/C"] = 4
    assert svc.signals == [("ItemsChanged", {"/C": {"Value": 4, "Text": "4"}})]


def test_an_exception_flushes_what_was_written_and_ends_the_batch(proxy, svc):
    # publish_battery quits the main loop on an exception from publish_dbus, so it has to come out of the batch
    with pytest.raises(RuntimeError, match="decode failed"):
        with proxy:
            proxy["/A"] = 1
            raise RuntimeError("decode failed")
    assert svc.signals == [("ItemsChanged", {"/A": {"Value": 1, "Text": "1"}})]
    assert svc._ratelimiters == []

    proxy["/B"] = 2
    assert svc.signals[1:] == [("PropertiesChanged", "/B", {"Value": 2, "Text": "2"})]


def test_a_flush_that_raises_still_ends_the_batch(proxy, svc):
    def bus_error(changes):
        raise RuntimeError("org.freedesktop.DBus.Error.NoMemory")

    svc.root.ItemsChanged = bus_error
    with pytest.raises(RuntimeError, match="NoMemory"):
        with proxy:
            proxy["/A"] = 1

    proxy["/B"] = 2
    assert svc.signals == [("PropertiesChanged", "/B", {"Value": 2, "Text": "2"})]


def test_a_nested_batch_joins_the_outer_one(proxy, svc):
    with proxy:
        proxy["/A"] = 1
        with proxy:
            proxy["/B"] = 2
        assert svc.signals == []
        assert len(svc._ratelimiters) == 1
    assert svc.signals == [("ItemsChanged", {"/A": {"Value": 1, "Text": "1"}, "/B": {"Value": 2, "Text": "2"}})]
    assert svc._ratelimiters == []

    proxy["/A"] = 3
    assert svc.signals[1:] == [("PropertiesChanged", "/A", {"Value": 3, "Text": "3"})]


def test_a_service_without_batches_keeps_per_path_writes_in_a_batch():
    plain = {}
    proxy = _CachedDbusProxy(plain)
    with proxy:
        proxy["/A"] = 1
        assert plain == {"/A": 1}
