"""Support for interfacing with the AmpliPi Multizone home audio controller's various entities."""
# pylint: disable=W1203
import logging
import operator
from functools import reduce
from typing import List, Optional

import validators
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components import persistent_notification
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState
from pyamplipi.models import ZoneUpdate, SourceUpdate, MultiZoneUpdate

from ..utils import get_fixed_source_id, has_fixed_source, extract_amplipi_id_from_unique_id
from .. import routing
from ..const import DOMAIN
from ..coordinator import AmpliPiDataClient
from ..models import Source, Group, Zone, Stream

_LOGGER = logging.getLogger(__name__)

SUPPORT_AMPLIPI_DAC = (
    MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.GROUPING
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.BROWSE_MEDIA
)

DEFAULT_SUPPORTED_COMMANDS = ( # Used to forcibly support a shortlist of commands regardless of sub-type
    MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.TURN_ON
)

PLAYBACK_STATES = {
    'playing': MediaPlayerState.PLAYING,
    'paused': MediaPlayerState.PAUSED,
}

SUPPORT_LOOKUP_DICT = {
    'play': MediaPlayerEntityFeature.PLAY,
    'pause': MediaPlayerEntityFeature.PAUSE,
    'stop': MediaPlayerEntityFeature.STOP,
    'next': MediaPlayerEntityFeature.NEXT_TRACK,
    'prev': MediaPlayerEntityFeature.PREVIOUS_TRACK,
}

class AmpliPiMediaPlayer(CoordinatorEntity[AmpliPiDataClient], MediaPlayerEntity):
    """
        Parent class of all AmpliPi MediaPlayer entities. Used to enforce common variables and provide shared functionality.
    """
    # The amplipi-side id
    _id: int

    # Home assistant side immutible id
    _unique_id: str

    # Lists of zones and groups related to the entity, either because the entity is a zone or a group or because they're connected to the entity
    _zones: List[Zone]
    _groups: List[Group]
    _stream: Optional[Stream] = None
    _source: Optional[Source] = None

    # List of all known streams
    _streams: List[Stream]

    # Home assistant particulars that are populated at entity instantiation via hass
    _vendor: str
    _version: str
    _data_client: AmpliPiDataClient
    _domain: str
    _image_base_path: str # Where the album art metadata is stored on home assistant
    _attr_has_entity_name: bool = True # Mandatory to set to True as per https://developers.home-assistant.io/docs/core/entity#has_entity_name-true-mandatory-for-new-integrations

    # Was the last polling cycle successful?
    _last_update_successful: bool = False

    # List of various arbitrary extra state attributes. Home assistant expects this list to exist, but it doesn't necessarily contain anything in most of our cases.
    _extra_attributes: dict

    # Does home assistant let you interact with the entity? False by default, made true if the entity is able to poll properly.
    _available: bool = False

    # Should the media player be set to STATE_OFF?
    _is_off: bool = False

    # The displayname of the entity. Also what is passed to async_select_source via dropdown menus.
    _name: str

    def __init__(self, coordinator: AmpliPiDataClient):
        super().__init__(coordinator)
        # Per-instance, so entities never share (and mutate) one list
        self._zones = []
        self._groups = []
        self._streams = []
        self._extra_attributes = {}

    def available_streams(self, source: Source):
        """Returns the available streams (generally all of them minus three of the four RCAs) relative to the provided source"""
        streams: List[str] = ['None']
        if self._data_client.data is not None:
            # Excludes every RCA except for the one related to the given source
            RCAs = [996, 997, 998, 999]
            rca_selectable = RCAs[source.id]
            stream_entries = self._data_client.data.streams
            if stream_entries:
                for entry in stream_entries:
                    amplipi_id = extract_amplipi_id_from_unique_id(entry.unique_id)
                    if amplipi_id == rca_selectable or amplipi_id not in RCAs:
                        streams.append(entry.friendly_name if entry.friendly_name not in [None, 'None'] else entry.original_name)
        return streams
    
    async def async_connect_stream_to_source(self, stream: Stream, source: Optional[Source] = None, leaving: Optional[List[int]] = None):
        """Connects the stream to a source. If a source is not provided, picks one with routing.pick_bus, which respects buses reserved for RCA inputs."""
        _LOGGER.debug(f"Stream {stream.name} attempting to connect to source {source}")
        source_id = None
        if has_fixed_source(stream):
            # RCAs are hardware constrained to only being able to use one specific source
            # If that source is busy, free it up without interrupting a users music
            if self._source is not None and source is not None and source.id != self._source.id:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="rca_fixed_source",
                    translation_placeholders={"stream": stream.name, "source": str(get_fixed_source_id(stream) + 1)},
                )

            state = self._data_client.data
            source = state.sources[get_fixed_source_id(stream)]
            # It would be cleaner to do the following, but pyamplipi doesn't support RCA stream's index value atm:
            # source = state.sources[self._stream.index]
            if source.input not in routing.EMPTY_INPUTS and source.input != routing.stream_input(stream.id):
                await self.reclaim_rca_source(stream, source.id)

        if source is not None:
            source_id = source.id
        else:
            source_id = routing.pick_bus(self._data_client.data, stream, self._data_client.reserved_buses, leaving=leaving or ())
            if source_id is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="no_free_source",
                    translation_placeholders={"stream": stream.name},
                )
            
        if source_id is not None:
            await self._data_client.set_source(
                source_id,
                SourceUpdate(
                    input=f'stream={stream.id}'
                )
            )
            return source_id

    async def reclaim_rca_source(self, rca: Stream, source_id: int):
        """
            Clear an RCA's fixed source so the RCA can play on it\n
            The stream already on it is moved to another source along with its zones. If no other source is available, the RCA wins:
            the zones listening to that stream are disconnected and a notification says which ones.
        """
        state = self._data_client.data
        borrower = routing.stream_on_bus(state, source_id)
        if borrower is None or borrower.type == "rca":
            return

        new_source = routing.pick_bus(state, None, self._data_client.reserved_buses, exclude={source_id})
        if new_source is not None:
            await self.swap_source(source_id, new_source)
            return

        cut_off = routing.listening_zones(state, source_id)
        _LOGGER.warning(f"{rca.name} needs source {source_id + 1}, disconnecting {borrower.name} from {[z.name for z in cut_off]}")
        if cut_off:
            await self._data_client.set_zones(
                MultiZoneUpdate(
                    zones=[z.id for z in cut_off],
                    update=ZoneUpdate(source_id=-1)
                )
            )
            persistent_notification.create(
                self.hass,
                f"{rca.name} can only play on source {source_id + 1}, which {borrower.name} was using, and no other source was free.\n\n"
                f"Disconnected: {', '.join(z.name for z in cut_off)}",
                f"{borrower.name} stopped for {rca.name}",
                f"{DOMAIN}_rca_reclaim_{source_id}",
            )
    
    async def async_connect_zones_to_source(self, source: Source, zones: Optional[List[int]], groups: Optional[List[int]]):
        """Connects zones and/or groups to the provided source"""
        if source is not None:
            await self._data_client.set_zones(
                MultiZoneUpdate(
                    zones=zones,
                    groups=groups,
                    update=ZoneUpdate(
                        source_id=source.id
                    )
                )
            )

    async def async_connect_zones_to_stream(self, stream: Stream, zones: Optional[List[int]], groups: Optional[List[int]]):
        """Connects zones and/or groups to the source of the selected stream. If stream does not have a source, select one"""
        state = self._data_client.data
        source_id = routing.bus_for_stream(state, stream.id)
        if source_id is None:
            # The zones being moved stop listening to their current source, so it can be reused if nothing else is free
            leaving = set(zones or [])
            for group in state.groups:
                if group.id in (groups or []):
                    leaving |= set(group.zones)
            source_id = await self.async_connect_stream_to_source(stream, leaving=list(leaving))
            state = self._data_client.data
        
        if source_id is not None:
            await self.async_connect_zones_to_source(state.sources[source_id], zones, groups)


    def build_url(self, img_url):
        """Returns the directory where album art metadata is kept"""
        if img_url is None:
            return None

        # if we have a full url, go ahead and return it
        if validators.url(img_url):
            return img_url

        # otherwise it might be a relative path.
        new_url = f'{self._image_base_path}/{img_url}'

        if validators.url(new_url):
            return new_url

        return None
    
    def playback_state(self) -> MediaPlayerState:
        """The state of whatever is playing on the connected source. Anything but playing or paused counts as idle"""
        return PLAYBACK_STATES.get(self._source.info.state, MediaPlayerState.IDLE)

    def get_song_info(self, source):
        """Get info relating to current song from the connected source"""
        if source is not None:
            info = source.info
            self._attr_media_album_artist = info.artist
            self._attr_media_album_name = info.album
            self._attr_media_title = info.name
            self._attr_media_track = info.track
            self._attr_media_image_url = self.build_url(info.img_url)
            self._attr_media_channel = info.station
        else:
            self._attr_media_album_artist = None
            self._attr_media_album_name = None
            self._attr_media_title = None
            self._attr_media_track = None
            self._attr_media_image_url = None
            self._attr_media_channel = None

    async def find_source(self, exclude=()) -> Optional[Source]:
        """Find an available source for a new stream and return it, skipping sources reserved for RCA inputs until every other source is in use. If no sources are available, returns None."""
        state = self._data_client.data
        if state is None:
            state = await self._data_client.get_status()
        source_id = routing.pick_bus(state, None, self._data_client.reserved_buses, exclude)
        return next((s for s in state.sources if s.id == source_id), None)
    
    async def swap_source(self, old_source: int, new_source: Optional[int] = None):
        """Moves a stream from one source to another, ensuring all zones follow. Generally only used for RCA streams, but able to be used by anyone."""
        state = self._data_client.data
        
        moved_stream: Stream = next(filter(lambda s: state.sources[old_source].input == f"stream={s.id}", state.streams), None)
        if moved_stream is not None and moved_stream.type != "rca":
            # RCA streams each have an associated source to output them due to hardware constraints
            if new_source is None:
                source = await self.find_source(exclude={old_source})
                if source:
                    new_source = source.id

            if new_source is not None:
                await self._data_client.set_source(
                    new_source,
                    SourceUpdate(
                        input=f'stream={moved_stream.id}'
                    )
                )

                moved_zones = [z.id for z in state.zones if z.source_id == old_source]
                await self._data_client.set_zones(
                    MultiZoneUpdate(
                        zones=moved_zones,
                        update=ZoneUpdate(
                            source_id=new_source
                        )
                    )
                )
    
    async def async_volume_up(self):
        """Increases volume by 1%"""
        if hasattr(self, "volume_up"):
            await self.hass.async_add_executor_job(self.volume_up)
            return

        if self.volume_level is not None and self.volume_level < 1:
            await self.async_set_volume_level(min(1, self.volume_level + 0.01))

    async def async_volume_down(self):
        """Decreases volume by 1%"""
        if hasattr(self, "volume_down"):
            await self.hass.async_add_executor_job(self.volume_down)
            return

        if self.volume_level is not None and self.volume_level > 0:
            await self.async_set_volume_level(max(0, self.volume_level - 0.01))
       
    async def async_media_play(self):
        if self._stream is not None:
            await self._data_client.play_stream(self._stream.id)

    async def async_media_stop(self):
        if self._stream is not None:
            await self._data_client.stop_stream(self._stream.id)

    async def async_media_pause(self):
        if self._stream is not None:
            await self._data_client.pause_stream(self._stream.id)

    async def async_media_previous_track(self):
        if self._stream is not None:
            await self._data_client.previous_stream(self._stream.id)

    async def async_media_next_track(self):
        if self._stream is not None:
            await self._data_client.next_stream(self._stream.id)

    def sync_state(self):
        """Refresh the entity's cached state from the coordinator's data. Overridden by each entity type"""

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.sync_state()

    def _handle_coordinator_update(self):
        self.sync_state()
        super()._handle_coordinator_update()

    @property
    def available(self):
        """Available while the controller is reachable and the AmpliPi-side object is usable"""
        return self.coordinator.last_update_success and self._available
    
    @property
    def entity_registry_enabled_default(self):
        """Return if the entity should be enabled when first added to the entity registry."""
        return True

    @property
    def unique_id(self):
        """Return unique ID for this entity."""
        return self._unique_id
    
    @property
    def source(self): # This is handled as a default as it's used by streams, groups, and zones but for sources this is overridden
        """Returns the current source playing, if this is wrong it won't show up as the selected source on HomeAssistant"""
        if self._source in [None, "None"]:
            return "None"
        return f'Source {self._source.id + 1}'
    
    @property
    def supported_features(self):
        """Return flag of media commands that are supported."""
        supported_features = SUPPORT_AMPLIPI_DAC
        if self._source is not None and self._source.info is not None:
            cmds_avail = SUPPORT_LOOKUP_DICT.keys() & self._source.info.supported_cmds
            features_avail = [SUPPORT_LOOKUP_DICT.get(cmd) for cmd in cmds_avail]
            if features_avail:
                supported_features = supported_features | reduce(operator.or_,  features_avail) # Add everything from the features_avail list to the support dict
        return supported_features | DEFAULT_SUPPORTED_COMMANDS
