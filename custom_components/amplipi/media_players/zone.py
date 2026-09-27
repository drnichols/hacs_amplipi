"""Support for interfacing with the AmpliPi Multizone home audio controller's audio outputs (Zones, Groups)."""
# pylint: disable=W1203
import logging
from typing import List, Optional

from homeassistant.components import media_source
from homeassistant.components.media_player import MediaPlayerDeviceClass, MediaPlayerState, MediaType
from homeassistant.components.media_player.browse_media import (
    async_process_play_media_url,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from pyamplipi.models import ZoneUpdate, MultiZoneUpdate, PlayMedia

from .base import AmpliPiMediaPlayer
from ..coordinator import AmpliPiDataClient
from ..const import DOMAIN
from ..models import Source, Group, Zone, Stream
from .. import routing

_LOGGER = logging.getLogger(__name__)

class AmpliPiZone(AmpliPiMediaPlayer, RestoreEntity):
    """Representation of an AmpliPi Zone and/or Group. Supports Audio volume
        and mute controls and the ability to pick the stream a zone plays.
        The source (bus) carrying the stream is chosen automatically"""

    def __init__(self, namespace: str, zone: Zone, group: Group,
                 streams: List[Stream], sources: List[Source],
                 vendor: str, version: str, image_base_path: str,
                 client: AmpliPiDataClient):
        super().__init__(client)
        self._sources = sources
        self._split_group: bool = False
        self._domain = namespace
        self._zone = zone
        self._group = group

        if group is not None:
            self._id = group.id
            self._unique_id = group.unique_id
            self.entity_id = group.entity_id
            
        else:
            self._id = zone.id
            self._unique_id = zone.unique_id
            self.entity_id = zone.entity_id

        self.entity_id = f"media_player.{self._unique_id}"
        self._attr_name = None
        self._streams = streams
        self._image_base_path = image_base_path
        self._vendor = vendor
        self._version = version
        self._enabled = False
        self._data_client = client
        # The stream to resume when turned back on, kept across restarts via RestoreEntity
        self._last_stream_id: Optional[int] = None
        self._attr_device_class = MediaPlayerDeviceClass.SPEAKER

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._last_stream_id = last_state.attributes.get("last_stream_id")

    def get_original_name(self):
        """
            Stores the f-string of the default entity name schema\n
            For use when naming the entity during __init__ and when populating _amplipi_state.update_state_entry()
        """
        return self._group.name if self._group else self._zone.name

    async def async_toggle(self):
        if self._is_off:
            await self.async_turn_on()
        else:
            await self.async_turn_off()

    def _zone_args(self):
        """The (zones, groups) arguments that address this entity in a MultiZoneUpdate"""
        return (None, [self._id]) if self._group is not None else ([self._id], None)

    def _find_stream(self, stream_id: Optional[int]) -> Optional[Stream]:
        state = self._data_client.data
        if state is None or stream_id is None:
            return None
        return next((s for s in state.streams if s.id == stream_id), None)

    async def async_turn_on(self):
        # Resume the stream that was playing when the zone/group was turned off, if it still exists
        last_stream = self._find_stream(self._last_stream_id)
        if last_stream is not None and not getattr(last_stream, "disabled", False):
            _LOGGER.debug(f"Turning {self.name} on and resuming {last_stream.name}")
            try:
                await self.async_connect_zones_to_stream(last_stream, *self._zone_args())
                self._is_off = False
                return
            except HomeAssistantError as e:
                # No source was free for it, so fall back to turning on without a stream
                _LOGGER.warning(f"Could not resume {last_stream.name} on {self.name}: {e}")

        # update zone/group to a disconnected but not off state
        # this allows it to be configured from HA without forcing a specific connection 
        no_source_update = ZoneUpdate(source_id=-1)
        if self._group is not None:
            _LOGGER.debug(f"Turning group {self.name} on")
            await self._update_group(
                MultiZoneUpdate(
                    groups=[self._group.id],
                    update=no_source_update
                )
            )
        else:
            _LOGGER.debug(f"Turning zone {self.name} on")
            await self._update_zone(no_source_update)
        self._is_off = False

    async def async_turn_off(self):
        # update zone/group to have a disconnected source state that indicates to HA that the zone/group is off
        if self._stream is not None:
            self._last_stream_id = self._stream.id
        source_off_update = ZoneUpdate(source_id=-2)
        if self._group is not None:
            _LOGGER.debug(f"Turning group {self.name} off")
            await self._update_group(
                MultiZoneUpdate(
                    groups=[self._group.id],
                    update=source_off_update
                )
            )
        else:
            _LOGGER.debug(f"Turning zone {self.name} off")
            await self._update_zone(source_off_update)
        self._is_off = True

    async def async_mute_volume(self, mute):
        if mute is None:
            return
        _LOGGER.debug(f"setting mute to {mute}")
        if self._group is not None:
            await self._update_group(
                MultiZoneUpdate(
                    groups=[self._group.id],
                    update=ZoneUpdate(
                        mute=mute,
                    )
                )
            )
        else:
            await self._update_zone(ZoneUpdate(
                mute=mute
            ))

    async def async_set_volume_level(self, volume):
        if volume is None:
            return
        
        if self._group is not None:
            self._group.vol_f = volume
        elif self._zone is not None:
            self._zone.vol_f = volume
    
        _LOGGER.debug(f"setting volume to {volume}")
        if self._group is not None:
            await self._update_group(
                MultiZoneUpdate(
                    groups=[self._group.id],
                    update=ZoneUpdate(
                        vol_f=volume
                    )
                )
            )
        else:
            await self._update_zone(ZoneUpdate(
                vol_f=volume
            ))

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        return MediaType.MUSIC

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this device."""
        if self._group is not None:
            name = self._group.original_name
            model = "AmpliPi Group"
        else:
            name = self._zone.original_name
            model = "AmpliPi Zone"

        return DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            model=model,
            name=name,
            manufacturer=self._vendor,
            sw_version=self._version,
            configuration_url=self._image_base_path,
            via_device=(DOMAIN, self.coordinator.config_entry.entry_id),
        )

    def sync_state(self):
        """Retrieve latest state."""
        _LOGGER.debug(f'Retrieving state for source {self._id}')
        state = self._data_client.data
        if state is not None:
            zone = None
            group = None
            enabled = False

            try:
                if self._group is not None:
                    group: Group = next(filter(lambda z: z.id == self._id, state.groups), None)
                    if not group:
                        self._last_update_successful = False
                        return
                    any_enabled_zone = next(filter(lambda z: z.id in group.zones, state.zones), None)

                    if any_enabled_zone is not None:
                        enabled = True

                    connected_sources = [state.zones[zone_index].source_id for zone_index in group.zones]
                    # Is every zone connected to the same source?
                    self._split_group = len(set(connected_sources)) != 1
                else:
                    zone = next(filter(lambda z: z.id == self._id, state.zones), None)
                    if not zone:
                        self._last_update_successful = False
                        return
                    enabled = not zone.disabled
            except Exception:
                self._last_update_successful = False
                _LOGGER.error(f'Could not update {"group" if self._group is not None else "zone"} {self._id}')
                return


            if group is not None:
                self._available = any(z.id in group.zones and not z.disabled for z in state.zones)
            else:
                self._available = not zone.disabled

            self._zone = zone
            self._group = group
            self._streams = state.streams
            self._sources = state.sources
            self._last_update_successful = True
            self._enabled = enabled
            self._source = None
            self._stream = None

            # When a zone is off it connects to source_id -2, groups also yield the source_id that all requisite zones are already connected to
            if self._group is not None:
                self._source = next(filter(lambda s: self._group.source_id == s.id, state.sources), None)
                self._is_off = self._group.source_id == -2

            elif self._zone.source_id is not None:
                self._source = next(filter(lambda s: self._zone.source_id == s.id, state.sources), None)
                self._is_off = self._zone.source_id == -2

            if self._source is not None and 'stream=' in self._source.input and 'stream=local' not in self._source.input:
                stream_id = int(self._source.input.split('=')[1])
                self._stream = next(filter(lambda z: z.id == stream_id, self._streams), None)

            self.get_song_info(self._source)
            self._last_update_successful = True

    @property
    def state(self):
        """Media player state of the zone or group."""
        
        if self._is_off and self._source is None:
            return MediaPlayerState.OFF
        elif self._last_update_successful is False or self._split_group:
            return None
        elif self._source is None or self._source == -1 or self._source.info is None or self._source.info.state is None:
            return MediaPlayerState.IDLE
        return self.playback_state()

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        if self._group is not None:
            return self._group.vol_f
        elif self._zone is not None:
            return self._zone.vol_f
        return None

    @property
    def is_volume_muted(self) -> bool:
        """Boolean if volume is currently muted."""
        if self._group is not None:
            return self._group.mute
        else:
            return self._zone.mute
 
    async def async_select_source(self, source: str):
        # This is a home assistant MediaPlayer built-in function, so the source being passed in isn't the same as an amplipi source
        # the argument "source" can either be the name or entity_id of a stream or amplipi source, or the string "None" to signify being disconnected
        # As such, this info must be sorted and then sent down the proper logical path
        if source == "None":
            disconnect_update = ZoneUpdate(source_id=-1)
            if self._group is not None:
                await self._update_group(
                    MultiZoneUpdate(
                        groups=[self._group.id],
                        update=disconnect_update
                    )
                )
            else:
                await self._update_zone(disconnect_update)
        else:
            entity = self._data_client.get_entry_by_value(source)
            args = (entity, None, [self._id]) if self._group is not None else (entity, [self._id], None)
            if isinstance(entity, Stream):
                await self.async_connect_zones_to_stream(*args)
            elif isinstance(entity, Source):
                await self.async_connect_zones_to_source(*args)

    async def _update_zone(self, update: ZoneUpdate):
        await self._data_client.set_zone(self._id, update)

    async def _update_group(self, update: MultiZoneUpdate):
        await self._data_client.set_zones(update)

    @staticmethod
    def _stream_label(stream: Stream) -> str:
        return stream.friendly_name if stream.friendly_name not in [None, 'None'] else stream.original_name

    @property
    def source_list(self):
        """The streams a zone can play. Picking one routes it to a source automatically. Music Assistant's streams are left out, since Music Assistant drives them"""
        streams = self._streams or []
        return ['None'] + [
            self._stream_label(s) for s in streams
            if not getattr(s, "disabled", False) and not routing.is_music_assistant_stream(s)
        ]

    @property
    def source(self):
        """Returns the stream playing, if this is wrong it won't show up as the selected source on HomeAssistant"""
        if self._stream is not None:
            return self._stream_label(self._stream)
        return "None"

    @property
    def group_members(self) -> Optional[List[str]]:
        """Zones sharing this zone's source, and so hearing the same stream. Leader (this zone) first"""
        state = self._data_client.data
        if self._group is not None or self._zone is None or state is None:
            return None
        if self._zone.source_id is None or self._zone.source_id < 0:
            return [self.entity_id]
        others = [z.entity_id for z in routing.listening_zones(state, self._zone.source_id) if z.id != self._zone.id]
        return [self.entity_id] + others

    async def async_join_players(self, group_members: List[str]):
        """Point the given zones and groups at this entity's source so they hear the same stream"""
        state = self._data_client.data
        bus_id = self._group.source_id if self._group is not None else self._zone.source_id
        if bus_id is None or bus_id < 0:
            raise HomeAssistantError(f"{self.name} isn't playing anything to join")

        zones = [z.id for z in state.zones if z.entity_id in group_members]
        groups = [g.id for g in state.groups if g.entity_id in group_members]
        if zones or groups:
            await self._data_client.set_zones(
                MultiZoneUpdate(
                    zones=zones or None,
                    groups=groups or None,
                    update=ZoneUpdate(source_id=bus_id)
                )
            )

    async def async_unjoin_player(self):
        """Disconnect from the shared source. The source is freed if nothing else listens to it"""
        zones, groups = self._zone_args()
        await self._data_client.set_zones(
            MultiZoneUpdate(
                zones=zones,
                groups=groups,
                update=ZoneUpdate(source_id=-1)
            )
        )

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Implement the websocket media browsing helper."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )

    async def async_play_media(self, media_type, media_id, **kwargs):
        _LOGGER.debug(f'Play Media {media_type} {media_id} {kwargs}')

        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(self.hass, media_id)
            media_id = play_item.url
            _LOGGER.debug(f'Playing media source: {play_item} {media_id}')

        # No source, see if we can find an empty one and point this zone at it
        if self._source is None:
            self._source = await self.find_source()
            
            if self._source is None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_free_source_for_media")

            await self.async_connect_zones_to_source(self._source, *self._zone_args())
                

        media_id = async_process_play_media_url(self.hass, media_id)
        await self._data_client.play_media(
            PlayMedia(
                source_id=self._source.id,
                media=media_id,
            )
        )
        pass

    @property
    def extra_state_attributes(self):
        # amplipi_zones and amplipi_zone_id are used by the group card of AmpliPi-HomeAssistant-Card to select related zone entities so they can be listed individually on the card
        # amplipi_zone_id is used in a similar way on the source cards as well
        if self._group is not None:
            return {
                "amplipi_zones": self._get_zone_ids(),
                "is_group": True,
                "stream_connected": self._stream is not None,
                "last_stream_id": self._last_stream_id,
            }
        else:
            return {
                "stream_connected": self._stream is not None,
                "is_group": False,
                "amplipi_zone_id": self._zone.id,
                "last_stream_id": self._last_stream_id,
            }

    def _get_zone_ids(self) -> List[int]:
        if self._group is not None:
            state = self._data_client.data
            zone_ids = []

            for zone_id in self._group.zones:
                for state_zone in state.zones:
                    if state_zone.id == zone_id and not state_zone.disabled:
                        zone_ids.append(zone_id)
            return zone_ids
        else:
            return self._zone.id
