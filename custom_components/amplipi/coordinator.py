"""
    AmpliPi API data coordinator
    Used to synchronize the current AmpliPi state with all of the corresponding HA Entities
"""
import time
from datetime import timedelta
from typing import Optional, Union, Callable, Iterable, Set

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from pyamplipi.amplipi import AmpliPi
from pyamplipi.models import SourceUpdate, ZoneUpdate, MultiZoneUpdate, GroupUpdate, PlayMedia, Announcement, Source as PySource, Stream as PyStream, Group as PyGroup, Zone as PyZone

from .models import Status, Source, Zone, Group, Stream
from .const import (
    DOMAIN, CONF_RESERVED_RCA, CONF_FREE_IDLE_BUSES, CONF_IDLE_GRACE_SECONDS, CONF_MUTE_ON_PAUSE, CONF_MUTE_DELAY_SECONDS,
    DEFAULT_FREE_IDLE_BUSES, DEFAULT_IDLE_GRACE_SECONDS, DEFAULT_MUTE_ON_PAUSE, DEFAULT_MUTE_DELAY_SECONDS,
)
from . import routing

class AmpliPiDataClient(DataUpdateCoordinator, AmpliPi):
    def __init__(self, hass, logger, config_entry, endpoint, timeout, http_session):
        super().__init__(
            hass,
            logger,
            config_entry=config_entry,
            name="hacs_amplipi",
            update_interval=timedelta(seconds=30),
            always_update=False
        )

        AmpliPi.__init__(
            self,
            endpoint=endpoint,
            timeout=timeout,
            http_session=http_session
        )

        # When each bus was first seen with a stream on it but no zone listening, for the poll sweep
        self._idle_since: dict[int, float] = {}
        self._sweep_task = None
        # /api/announce borrows a bus while it plays, so buses are left alone until it finishes
        self._announcing = False
        # When each listened bus was first seen paused, and the zones muted because of it (zone id -> bus id)
        self._paused_since: dict[int, float] = {}
        self._auto_muted: dict[int, int] = {}
        self._mute_exempt: dict[int, int] = {}
        self._mute_task = None

    @property
    def reserved_buses(self) -> Set[int]:
        """Buses kept for their RCA input, only lent to other streams once every other bus is in use"""
        return {int(bus) for bus in self.config_entry.options.get(CONF_RESERVED_RCA, [])}

    @property
    def free_idle_buses(self) -> bool:
        """Should a bus be cleared once no zone listens to it"""
        return self.config_entry.options.get(CONF_FREE_IDLE_BUSES, DEFAULT_FREE_IDLE_BUSES)

    @property
    def idle_grace_seconds(self) -> int:
        """How long a bus can sit unlistened before the poll sweep clears it"""
        return self.config_entry.options.get(CONF_IDLE_GRACE_SECONDS, DEFAULT_IDLE_GRACE_SECONDS)

    async def release_buses(self, bus_ids: Iterable[int]):
        """Clear the input of each bus that still has a stream but no zone listening, so it counts as available"""
        if not self.free_idle_buses or self._announcing or self.data is None:
            return
        idle = routing.idle_buses(self.data)
        for bus_id in sorted(set(bus_ids) & idle):
            self.logger.info(f"Freeing source {bus_id + 1}, no zone is listening to it")
            self._idle_since.pop(bus_id, None)
            await self.set_source(bus_id, SourceUpdate(input='None'))

    def _sweep_idle_buses(self, status: Status):
        """
            Track buses that have a stream but no listener, and free any that stay that way for the grace period\n
            This catches changes made outside Home Assistant, such as in the AmpliPi web app, without undoing them straight away
        """
        if not self.free_idle_buses or self._announcing:
            self._idle_since.clear()
            return
        now = time.monotonic()
        idle = routing.idle_buses(status)
        self._idle_since = {bus: self._idle_since.get(bus, now) for bus in idle}
        expired = [bus for bus, since in self._idle_since.items() if now - since >= self.idle_grace_seconds]
        if expired and (self._sweep_task is None or self._sweep_task.done()):
            self._sweep_task = self.hass.async_create_task(self.release_buses(expired))

    @property
    def mute_on_pause(self) -> bool:
        """Should zones be muted while their stream is paused, so the amps can drop into standby"""
        return self.config_entry.options.get(CONF_MUTE_ON_PAUSE, DEFAULT_MUTE_ON_PAUSE)

    @property
    def mute_delay_seconds(self) -> int:
        """How long a stream can sit paused before its zones are muted"""
        return self.config_entry.options.get(CONF_MUTE_DELAY_SECONDS, DEFAULT_MUTE_DELAY_SECONDS)

    def _sweep_paused_buses(self, status: Status):
        """
            Mute the zones on a bus whose stream has been paused for the mute delay, and unmute them once it plays again or they move to another stream\n
            Only zones muted here are ever unmuted, so a zone the user muted stays muted. A zone the user unmutes, turns off or disconnects is forgotten
        """
        if not self.mute_on_pause or self._announcing:
            return
        now = time.monotonic()
        paused = routing.paused_buses(status)
        self._paused_since = {bus: self._paused_since.get(bus, now) for bus in paused}

        zones = {z.id: z for z in status.zones}
        # A zone the user unmutes during a pause is left alone until that pause ends or the zone moves
        self._mute_exempt = {
            zone_id: bus for zone_id, bus in self._mute_exempt.items()
            if bus in paused and zone_id in zones and zones[zone_id].source_id == bus
        }
        unmute = {}
        for zone_id, bus in list(self._auto_muted.items()):
            zone = zones.get(zone_id)
            if zone is None or zone.disabled or zone.source_id < 0:
                del self._auto_muted[zone_id]
            elif not zone.mute:
                del self._auto_muted[zone_id]
                if zone.source_id == bus and bus in paused:
                    self._mute_exempt[zone_id] = bus
            elif zone.source_id != bus or bus not in paused:
                unmute[zone_id] = bus

        mute = {}
        for bus, since in self._paused_since.items():
            if now - since < self.mute_delay_seconds:
                continue
            for zone in routing.listening_zones(status, bus):
                if not zone.mute and zone.id not in self._auto_muted and zone.id not in self._mute_exempt:
                    mute[zone.id] = bus

        # While a previous change is still being sent, leave everything for the next sweep
        if not (mute or unmute) or (self._mute_task is not None and not self._mute_task.done()):
            return
        for zone_id in unmute:
            del self._auto_muted[zone_id]
        self._auto_muted.update(mute)
        self._mute_task = self.hass.async_create_task(self._apply_pause_mutes(mute, unmute))

    async def _apply_pause_mutes(self, mute: dict[int, int], unmute: dict[int, int]):
        """
            Send the mutes and unmutes the sweep decided on. mute and unmute map zone id -> bus id\n
            Tracking was updated before sending, so the sweeps these calls trigger see the change. It's undone if a call fails, so the next sweep retries
        """
        try:
            if unmute:
                self.logger.info(f"Unmuting zones {list(unmute)}, their stream is playing again")
                await self.set_zones(MultiZoneUpdate(zones=list(unmute), update=ZoneUpdate(mute=False)))
            if mute:
                self.logger.info(f"Muting zones {list(mute)}, their stream has been paused for {self.mute_delay_seconds}s")
                await self.set_zones(MultiZoneUpdate(zones=list(mute), update=ZoneUpdate(mute=True)))
        except Exception as e:  # pylint: disable=broad-except
            self.logger.warning(f"Couldn't change mute for zones on a paused stream, will retry: {e}")
            for zone_id in mute:
                self._auto_muted.pop(zone_id, None)
            self._auto_muted.update(unmute)

    async def _unmute_bus(self, bus_id: Optional[int]):
        """Unmute the zones muted while this bus was paused, straight away rather than on the next poll"""
        self._paused_since.pop(bus_id, None)
        self._mute_exempt = {zone_id: bus for zone_id, bus in self._mute_exempt.items() if bus != bus_id}
        zones = [zone_id for zone_id, bus in self._auto_muted.items() if bus == bus_id]
        for zone_id in zones:
            del self._auto_muted[zone_id]
        if zones:
            await self.set_zones(MultiZoneUpdate(zones=zones, update=ZoneUpdate(mute=False)))

    def get_entry_by_value(self, value: str) -> Union[Source, Zone, Group, Stream, None]:
        """Find what dict within the state array has a given value and return said dict"""
        if self.data is not None:
            for category in (self.data.sources, self.data.zones, self.data.groups, self.data.streams):
                for entry in category:
                    if value in entry.model_dump().values():
                        return entry
        return None

    async def get_friendly_name(self, entity_id):
        """Look up entity in hass.states and get the friendly name"""
        state = self.hass.states.get(entity_id)
        if state:
            return state.attributes.get("friendly_name")
        
    async def get_entity_id_from_unique_id(self, unique_id: str):
        """Gets entity_id from the entity registry using the unique_id"""
        return async_get_entity_registry(self.hass).async_get_entity_id("media_player", DOMAIN, unique_id)
    
    async def _async_update_data(self) -> Status:
        """Fetch data from API endpoint and pre-process into lookup tables."""
        try:
            resp = await super().get_status()
        except Exception as e:
            raise UpdateFailed(f"Error fetching data: {e}") from e
        status = await self.build_status(resp.model_dump())
        self._sweep_idle_buses(status)
        self._sweep_paused_buses(status)
        return status

    async def set_data(self, state: dict) -> Status:
        """Publish the Status returned by a command, so entities update without waiting for the next poll"""
        status = await self.build_status(state)
        self.async_set_updated_data(status)
        self._sweep_idle_buses(status)
        self._sweep_paused_buses(status)
        return status

    async def build_status(self, state: dict) -> Status:
        """Take in a Status from the AmpliPi API and add home assistant specific encoding to it"""
        async def build_entity(entity: Union[PySource, PyZone, PyGroup, PyStream], kind: str, cls, original_name: str):
            try:
                unique_id = f"{DOMAIN}_{kind}_{entity['id']}"
                entity_id = await self.get_entity_id_from_unique_id(unique_id) or f"media_player.{unique_id}"
                friendly_name = await self.get_friendly_name(entity_id) or original_name
                return cls(
                    **entity,
                    original_name=original_name,
                    unique_id=unique_id,
                    entity_id=entity_id,
                    friendly_name=friendly_name,
                )
            except TypeError as e:
                self.logger.error(f"Original name = {original_name}, entity = {entity}")
                raise TypeError(e) from e

        try:
            state["sources"] = [
                await build_entity(entity, "source", Source, f"Source {entity['id'] + 1}")
                for entity in state["sources"]
            ]

            state["zones"] = [
                await build_entity(entity, "zone", Zone, entity["name"])
                for entity in state["zones"]
            ]

            state["groups"] = [
                await build_entity(entity, "group", Group, entity["name"])
                for entity in state["groups"]
            ]

            state["streams"] = [
                await build_entity(entity, "stream", Stream, entity["name"])
                for entity in state["streams"]
            ]

            return Status(**state)

        except Exception as e:
            raise UpdateFailed(f"Error fetching data: {e}") from e
        
    # TODO: Find a better way to do the following without all the repeated boilerplate code
        
    def intercept_and_consume(func: Callable):
        """Intercept the return of a function and consume the data into the data coordinator"""
        async def wrapper(self, *args, **kwargs):
            resp = await func(self, *args, **kwargs)
            return await self.set_data(resp.model_dump())
        return wrapper

    def release_abandoned_buses(func: Callable):
        """Free any bus that had a zone listening before the call and has none after it"""
        async def wrapper(self, *args, **kwargs):
            before = routing.listened_buses(self.data) if self.data is not None else set()
            status = await func(self, *args, **kwargs)
            abandoned = before - routing.listened_buses(status)
            if abandoned:
                await self.release_buses(abandoned)
            return self.data
        return wrapper

    @intercept_and_consume
    async def get_status(self) -> Status:
        return await super().get_status()

    @intercept_and_consume
    async def set_source(self, source_id: int, source_update: SourceUpdate) -> Status:
        return await super().set_source(source_id, source_update)
        
    @release_abandoned_buses
    @intercept_and_consume
    async def set_zone(self, zone_id: int, zone_update: ZoneUpdate) -> Status:
        return await super().set_zone(zone_id, zone_update)

    @release_abandoned_buses
    @intercept_and_consume
    async def set_zones(self, zone_update: MultiZoneUpdate) -> Status:
        return await super().set_zones(zone_update)
        
    @intercept_and_consume
    async def play_media(self, media: PlayMedia) -> Status:
        return await super().play_media(media)

    @release_abandoned_buses
    @intercept_and_consume
    async def set_group(self, group_id, update: GroupUpdate) -> Status:
        return await super().set_group(group_id, update)

    async def announce(self, announcement: Announcement, timeout: Optional[int] = None) -> Status:
        self._announcing = True
        try:
            return await self._announce(announcement, timeout)
        finally:
            self._announcing = False
            self._idle_since.clear()

    @intercept_and_consume
    async def _announce(self, announcement: Announcement, timeout: Optional[int] = None) -> Status:
        return await super().announce(announcement, timeout)

    async def play_stream(self, stream_id: int) -> Status:
        status = await self._play_stream(stream_id)
        await self._unmute_bus(routing.bus_for_stream(status, stream_id))
        return self.data

    @intercept_and_consume
    async def _play_stream(self, stream_id: int) -> Status:
        return await super().play_stream(stream_id)

    @intercept_and_consume
    async def pause_stream(self, stream_id: int) -> Status:
        return await super().pause_stream(stream_id)

    @intercept_and_consume
    async def previous_stream(self, stream_id: int) -> Status:
        return await super().previous_stream(stream_id)

    @intercept_and_consume
    async def next_stream(self, stream_id: int) -> Status:
        return await super().next_stream(stream_id)

    @intercept_and_consume
    async def stop_stream(self, stream_id: int) -> Status:
        return await super().stop_stream(stream_id)
        