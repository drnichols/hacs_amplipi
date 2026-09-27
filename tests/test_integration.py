"""Tests for zone-centric routing, bus freeing and RCA-reserved buses, run against a fake AmpliPi controller"""
import copy
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_ID, CONF_NAME, CONF_PORT
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from pyamplipi.amplipi import AmpliPi
from pyamplipi.models import Status
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amplipi.const import (
    DOMAIN, CONF_VENDOR, CONF_VERSION, CONF_WEBAPP, CONF_API_PATH,
    CONF_RESERVED_RCA, CONF_SHOW_BUS_STREAM_ENTITIES, CONF_IDLE_GRACE_SECONDS,
)

from .conftest import STREAMS


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeController:
    """Just enough of the AmpliPi controller rules: one stream per source (stealing), zones pick a source, groups fan out"""

    def __init__(self, zone_count=6):
        self.state = {
            "sources": [{"id": i, "name": f"Output {i + 1}", "input": "", "info": {"state": "stopped", "supported_cmds": []}} for i in range(4)],
            "zones": [
                {"id": i, "name": f"Zone {i + 1}", "source_id": -1, "mute": True, "disabled": False,
                 "vol": -40, "vol_f": 0.5, "vol_min": -80, "vol_max": 0}
                for i in range(zone_count)
            ],
            "groups": [{"id": 100, "name": "Upstairs", "zones": [4, 5], "source_id": -1, "mute": True, "vol_f": 0.5}],
            "streams": copy.deepcopy(STREAMS),
            "presets": [],
            "info": {"version": "0.4.9"},
        }
        self.source_writes = []

    def status(self):
        return Status(**copy.deepcopy(self.state))

    def zone_source(self, zone_id):
        return self.state["zones"][zone_id]["source_id"]

    def bus_input(self, bus_id):
        return self.state["sources"][bus_id]["input"]

    def put(self, bus_id, inp):
        self.state["sources"][bus_id]["input"] = inp

    def point(self, zone_ids, bus_id):
        for zone_id in zone_ids:
            self.state["zones"][zone_id]["source_id"] = bus_id

    def _apply_zone(self, zone_id, update):
        zone = self.state["zones"][zone_id]
        for field in ("source_id", "mute", "vol_f"):
            value = getattr(update, field, None)
            if value is not None:
                zone[field] = value
        if zone["source_id"] < 0:
            zone["mute"] = True

    async def get_status(self):
        return self.status()

    async def set_source(self, source_id, update):
        self.source_writes.append((source_id, update.input))
        inp = '' if update.input == 'None' else update.input
        if inp:
            for source in self.state["sources"]:
                if source["input"] == inp:
                    source["input"] = ''  # stealing
        self.put(source_id, inp)
        return self.status()

    async def set_zone(self, zone_id, update):
        self._apply_zone(zone_id, update)
        return self.status()

    async def set_zones(self, update):
        zone_ids = set(update.zones or [])
        for group in self.state["groups"]:
            if group["id"] in (update.groups or []):
                zone_ids |= set(group["zones"])
        for zone_id in zone_ids:
            self._apply_zone(zone_id, update.update)
        return self.status()


@pytest.fixture
def controller():
    return FakeController()


async def setup_amplipi(hass: HomeAssistant, controller: FakeController, options=None, minor_version=2):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=minor_version,
        data={
            CONF_NAME: "AmpliPi", CONF_HOST: "amplipi.local", CONF_PORT: 80, CONF_ID: "test",
            CONF_VENDOR: "micro-nova", CONF_VERSION: "0.4.9", CONF_WEBAPP: "http://amplipi.local", CONF_API_PATH: "/api",
        },
        options=options if options is not None else {CONF_SHOW_BUS_STREAM_ENTITIES: False},
    )
    entry.add_to_hass(hass)
    patches = [patch.object(AmpliPi, name, getattr(controller, name), create=True)
               for name in ("get_status", "set_source", "set_zone", "set_zones")]
    # The API is faked, so no real HTTP session (and its DNS resolver thread) is needed
    patches.append(patch("custom_components.amplipi.async_get_clientsession", return_value=MagicMock()))
    for p in patches:
        p.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, patches


@pytest.fixture
async def amplipi(hass, controller):
    """Set up the integration and return a helper to (re)configure it"""
    started = []

    async def _setup(options=None, minor_version=2):
        entry, patches = await setup_amplipi(hass, controller, options, minor_version)
        started.extend(patches)
        return entry

    yield _setup
    for p in started:
        p.stop()


async def select(hass, entity_id, source):
    await hass.services.async_call(
        "media_player", "select_source", {"entity_id": entity_id, "source": source}, blocking=True
    )


async def refresh(hass, entry):
    coordinator = entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    return coordinator


async def test_zone_lists_streams_and_hides_buses(hass, amplipi, controller):
    await amplipi()
    state = hass.states.get("media_player.amplipi_zone_0")
    assert state.attributes["source_list"][:2] == ["None", "Input 1"]
    assert "Spotify" in state.attributes["source_list"]
    assert "Source 1" not in state.attributes["source_list"]
    # bus and stream entities are hidden for new installs
    assert hass.states.get("media_player.amplipi_source_0") is None
    assert hass.states.get("media_player.amplipi_stream_1000") is None


async def test_existing_install_keeps_bus_and_stream_entities(hass, amplipi, controller):
    entry = await amplipi(options={}, minor_version=1)
    assert entry.minor_version == 2
    assert entry.options[CONF_SHOW_BUS_STREAM_ENTITIES] is True
    assert hass.states.get("media_player.amplipi_source_0") is not None
    assert hass.states.get("media_player.amplipi_stream_1000") is not None


async def test_select_stream_routes_to_free_bus(hass, amplipi, controller):
    await amplipi()
    await select(hass, "media_player.amplipi_zone_0", "Spotify")
    assert controller.bus_input(0) == "stream=1000"
    assert controller.zone_source(0) == 0
    assert hass.states.get("media_player.amplipi_zone_0").attributes["source"] == "Spotify"

    # A second zone picking the same stream shares the bus
    await select(hass, "media_player.amplipi_zone_1", "Spotify")
    assert controller.zone_source(1) == 0


async def test_reserved_bus_skipped_until_others_exhausted(hass, amplipi, controller):
    await amplipi(options={CONF_RESERVED_RCA: ["0"]})
    await select(hass, "media_player.amplipi_zone_0", "Spotify")
    assert controller.zone_source(0) == 1
    await select(hass, "media_player.amplipi_zone_1", "AirPlay")
    await select(hass, "media_player.amplipi_zone_2", "Radio")
    assert {controller.zone_source(z) for z in (0, 1, 2)} == {1, 2, 3}
    # Every unreserved bus is in use, so the reserved one is lent out
    await select(hass, "media_player.amplipi_zone_3", "Pandora")
    assert controller.zone_source(3) == 0
    assert controller.bus_input(0) == "stream=1003"


async def test_rca_reclaims_reserved_bus_and_cuts_off_borrower(hass, amplipi, controller):
    await amplipi(options={CONF_RESERVED_RCA: ["0"]})
    for zone, stream in ((0, "Spotify"), (1, "AirPlay"), (2, "Radio"), (3, "Pandora")):
        await select(hass, f"media_player.amplipi_zone_{zone}", stream)
    assert controller.bus_input(0) == "stream=1003"

    with patch("custom_components.amplipi.media_players.base.persistent_notification.create") as notify:
        await select(hass, "media_player.amplipi_zone_4", "Input 1")
    assert controller.bus_input(0) == "stream=996"
    assert controller.zone_source(4) == 0
    assert controller.zone_source(3) == -1  # the borrower was cut off, not left hearing the RCA
    notify.assert_called_once()
    assert "Pandora" in notify.call_args.args[2] and "Zone 4" in notify.call_args.args[1]


async def test_rca_moves_borrower_when_a_bus_is_free(hass, amplipi, controller):
    await amplipi()
    await select(hass, "media_player.amplipi_zone_0", "Spotify")
    assert controller.bus_input(0) == "stream=1000"
    await select(hass, "media_player.amplipi_zone_1", "Input 1")
    assert controller.bus_input(0) == "stream=996"
    assert controller.bus_input(1) == "stream=1000"
    assert controller.zone_source(0) == 1  # Spotify's listener moved with it


async def test_bus_freed_when_last_listener_leaves(hass, amplipi, controller):
    await amplipi()
    await select(hass, "media_player.amplipi_zone_0", "Spotify")
    await select(hass, "media_player.amplipi_zone_1", "Spotify")
    await hass.services.async_call("media_player", "turn_off", {"entity_id": "media_player.amplipi_zone_0"}, blocking=True)
    assert controller.bus_input(0) == "stream=1000"  # zone 1 still listening
    await select(hass, "media_player.amplipi_zone_1", "AirPlay")
    assert controller.bus_input(0) == ""
    assert controller.bus_input(1) == "stream=1001"


async def test_bus_not_freed_when_option_off(hass, amplipi, controller):
    await amplipi(options={"free_idle_buses": False})
    await select(hass, "media_player.amplipi_zone_0", "Spotify")
    await hass.services.async_call("media_player", "turn_off", {"entity_id": "media_player.amplipi_zone_0"}, blocking=True)
    assert controller.bus_input(0) == "stream=1000"


async def test_poll_sweep_frees_external_idle_bus_after_grace(hass, amplipi, controller):
    entry = await amplipi(options={CONF_IDLE_GRACE_SECONDS: 30})
    controller.put(2, "stream=1002")  # set up in the AmpliPi web app with no zone listening
    with patch("custom_components.amplipi.coordinator.time.monotonic", return_value=1000.0):
        await refresh(hass, entry)
    assert controller.bus_input(2) == "stream=1002"
    with patch("custom_components.amplipi.coordinator.time.monotonic", return_value=1029.0):
        await refresh(hass, entry)
    assert controller.bus_input(2) == "stream=1002"
    with patch("custom_components.amplipi.coordinator.time.monotonic", return_value=1031.0):
        await refresh(hass, entry)
    assert controller.bus_input(2) == ""


async def test_poll_sweep_resets_when_a_zone_listens(hass, amplipi, controller):
    entry = await amplipi(options={CONF_IDLE_GRACE_SECONDS: 30})
    controller.put(2, "stream=1002")
    with patch("custom_components.amplipi.coordinator.time.monotonic", return_value=1000.0):
        await refresh(hass, entry)
    controller.point([0], 2)
    with patch("custom_components.amplipi.coordinator.time.monotonic", return_value=1031.0):
        await refresh(hass, entry)
    assert controller.bus_input(2) == "stream=1002"


async def test_turn_on_resumes_last_stream(hass, amplipi, controller):
    await amplipi()
    await select(hass, "media_player.amplipi_zone_0", "AirPlay")
    await hass.services.async_call("media_player", "turn_off", {"entity_id": "media_player.amplipi_zone_0"}, blocking=True)
    assert controller.zone_source(0) == -2
    assert controller.bus_input(0) == ""  # freed while off
    await hass.services.async_call("media_player", "turn_on", {"entity_id": "media_player.amplipi_zone_0"}, blocking=True)
    assert controller.bus_input(controller.zone_source(0)) == "stream=1001"


async def test_join_and_unjoin(hass, amplipi, controller):
    entry = await amplipi()
    await select(hass, "media_player.amplipi_zone_0", "Spotify")
    await hass.services.async_call(
        "media_player", "join",
        {"entity_id": "media_player.amplipi_zone_0", "group_members": ["media_player.amplipi_zone_1", "media_player.amplipi_group_100"]},
        blocking=True,
    )
    assert [controller.zone_source(z) for z in (1, 4, 5)] == [0, 0, 0]
    await refresh(hass, entry)
    members = hass.states.get("media_player.amplipi_zone_0").attributes["group_members"]
    assert members[0] == "media_player.amplipi_zone_0"
    assert set(members) == {f"media_player.amplipi_zone_{z}" for z in (0, 1, 4, 5)}

    await hass.services.async_call("media_player", "unjoin", {"entity_id": "media_player.amplipi_zone_1"}, blocking=True)
    assert controller.zone_source(1) == -1
    assert controller.bus_input(0) == "stream=1000"


async def test_legacy_bus_name_still_selectable(hass, amplipi, controller):
    await amplipi()
    controller.put(2, "stream=1002")
    await select(hass, "media_player.amplipi_zone_0", "Source 3")
    assert controller.zone_source(0) == 2


async def test_switching_stream_reuses_own_bus_when_all_busy(hass, amplipi, controller):
    await amplipi()
    for zone, stream in ((0, "Spotify"), (1, "AirPlay"), (2, "Radio"), (3, "Pandora")):
        await select(hass, f"media_player.amplipi_zone_{zone}", stream)
    await select(hass, "media_player.amplipi_zone_2", "DLNA")
    assert controller.zone_source(2) == 2
    assert controller.bus_input(2) == "stream=1004"


async def test_next_track_calls_next_not_previous(hass, amplipi, controller):
    entry = await amplipi()
    coordinator = entry.runtime_data
    with patch.object(AmpliPi, "next_stream", create=True, return_value=controller.status()) as nxt, \
            patch.object(AmpliPi, "previous_stream", create=True, return_value=controller.status()) as prev:
        await coordinator.next_stream(1000)
    nxt.assert_called_once_with(1000)
    prev.assert_not_called()


async def test_disabled_zone_and_group_become_unavailable(hass, amplipi, controller):
    entry = await amplipi()
    assert hass.states.get("media_player.amplipi_zone_0").state != "unavailable"
    controller.state["zones"][0]["disabled"] = True
    for zone_id in (4, 5):
        controller.state["zones"][zone_id]["disabled"] = True
    await refresh(hass, entry)
    assert hass.states.get("media_player.amplipi_zone_0").state == "unavailable"
    assert hass.states.get("media_player.amplipi_group_100").state == "unavailable"
    assert hass.states.get("media_player.amplipi_zone_1").state != "unavailable"


async def test_entities_follow_coordinator_without_polling(hass, amplipi, controller):
    entry = await amplipi()
    assert hass.states.get("media_player.amplipi_zone_0").attributes["source"] == "None"
    controller.put(1, "stream=1001")
    controller.point([0], 1)
    await refresh(hass, entry)
    assert hass.states.get("media_player.amplipi_zone_0").attributes["source"] == "AirPlay"


async def test_coordinator_entities_do_not_poll(hass, amplipi, controller):
    await amplipi()
    entity_component = hass.data["entity_components"]["media_player"]
    assert all(not e.should_poll for e in entity_component.entities)


async def test_source_turn_off_and_on_updates_state(hass, amplipi, controller):
    await amplipi(options={CONF_SHOW_BUS_STREAM_ENTITIES: True})
    await hass.services.async_call("media_player", "turn_off", {"entity_id": "media_player.amplipi_source_0"}, blocking=True)
    assert hass.states.get("media_player.amplipi_source_0").state == "off"
    await hass.services.async_call("media_player", "turn_on", {"entity_id": "media_player.amplipi_source_0"}, blocking=True)
    assert hass.states.get("media_player.amplipi_source_0").state != "off"


async def test_unreachable_controller_retries_setup(hass, controller):
    async def unreachable():
        raise TimeoutError
    controller.get_status = unreachable
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2, data={
        CONF_NAME: "AmpliPi", CONF_HOST: "amplipi.local", CONF_PORT: 80, CONF_ID: "test",
        CONF_VENDOR: "micro-nova", CONF_VERSION: "0.4.9", CONF_WEBAPP: "http://amplipi.local", CONF_API_PATH: "/api",
    })
    entry.add_to_hass(hass)
    with patch.object(AmpliPi, "get_status", controller.get_status, create=True), \
            patch("custom_components.amplipi.async_get_clientsession", return_value=MagicMock()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_old_firmware_raises_one_repair_issue(hass, amplipi, controller):
    controller.state["info"]["version"] = "0.4.0"
    entry = await amplipi()
    await refresh(hass, entry)
    issues = [i for (domain, _), i in ir.async_get(hass).issues.items() if domain == DOMAIN]
    assert len(issues) == 1
    assert issues[0].translation_key == "firmware_too_old"


async def test_zones_hang_off_controller_device(hass, amplipi, controller):
    entry = await amplipi()
    await select(hass, "media_player.amplipi_zone_0", "Spotify")
    devices = dr.async_get(hass)
    controller_device = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert controller_device is not None
    zone_entity = er.async_get(hass).async_get("media_player.amplipi_zone_0")
    zone_device = devices.async_get(zone_entity.device_id)
    assert zone_device.via_device_id == controller_device.id


async def test_no_free_source_raises_translated_error(hass, amplipi, controller):
    from homeassistant.exceptions import HomeAssistantError
    await amplipi()
    for zone, stream in ((0, "Spotify"), (1, "AirPlay"), (2, "Radio"), (3, "Pandora")):
        await select(hass, f"media_player.amplipi_zone_{zone}", stream)
    with pytest.raises(HomeAssistantError) as err:
        await select(hass, "media_player.amplipi_zone_4", "DLNA")
    assert err.value.translation_key == "no_free_source"
    assert "DLNA" in str(err.value)


async def test_diagnostics_redacts_host_and_credentials(hass, amplipi, controller):
    from custom_components.amplipi.diagnostics import async_get_config_entry_diagnostics
    controller.state["streams"][7].update(user="me@example.com", password="hunter2")
    entry = await amplipi()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"][CONF_HOST] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_WEBAPP] == "**REDACTED**"
    assert len(diag["status"]["zones"]) == 6
    assert "amplipi.local" not in str(diag)
    assert "hunter2" not in str(diag) and "me@example.com" not in str(diag)


async def test_music_assistant_stream_hidden_but_shown_as_current_source(hass, amplipi, controller):
    controller.state["streams"].append({"id": 1010, "name": "Music Assistant 1", "type": "internetradio"})
    controller.put(1, "stream=1010")
    controller.point([0], 1)
    await amplipi(options={CONF_SHOW_BUS_STREAM_ENTITIES: True})
    state = hass.states.get("media_player.amplipi_zone_0")
    assert "Music Assistant 1" not in state.attributes["source_list"]
    assert state.attributes["source"] == "Music Assistant 1"
    assert hass.states.get("media_player.amplipi_stream_1010") is None
    assert hass.states.get("media_player.amplipi_stream_1000") is not None
