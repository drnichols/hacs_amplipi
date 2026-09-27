"""Support for interfacing with the AmpliPi Multizone home audio controller."""
# pylint: disable=W1203
from homeassistant.components.media_player import MediaPlayerEntity

from . import routing
from .coordinator import AmpliPiDataClient
from .const import DOMAIN, CONF_VENDOR, CONF_VERSION, CONF_WEBAPP, CONF_SHOW_BUS_STREAM_ENTITIES

from .media_players.base import AmpliPiMediaPlayer
from .media_players.source import AmpliPiSource
from .media_players.stream import AmpliPiStream
from .media_players.zone import AmpliPiZone
from .media_players.announce import AmpliPiAnnouncer

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the AmpliPi MultiZone Audio Controller"""
    amplipi_coordinator: AmpliPiDataClient = config_entry.runtime_data
    vendor = config_entry.data[CONF_VENDOR]
    version = config_entry.data[CONF_VERSION]
    image_base_path = config_entry.data[CONF_WEBAPP]

    status = amplipi_coordinator.data
    sources: list[AmpliPiMediaPlayer] = [
        AmpliPiSource(DOMAIN, source, status.streams, vendor, version, image_base_path, amplipi_coordinator)
        for source in status.sources]

    zones: list[AmpliPiMediaPlayer] = [
        AmpliPiZone(DOMAIN, zone, None, status.streams, status.sources, vendor, version, image_base_path, amplipi_coordinator)
        for zone in status.zones]

    groups: list[AmpliPiMediaPlayer] = [
        AmpliPiZone(DOMAIN, None, group, status.streams, status.sources, vendor, version, image_base_path, amplipi_coordinator)
        for group in status.groups]
    
    # Music Assistant's streams are driven by Music Assistant, so they don't get a stream player here
    streams: list[AmpliPiMediaPlayer] = [
        AmpliPiStream(DOMAIN, stream, status.sources, vendor, version, image_base_path, amplipi_coordinator)
        for stream in status.streams
        if not routing.is_music_assistant_stream(stream)
    ]

    announcer: list[MediaPlayerEntity] = [
        AmpliPiAnnouncer(DOMAIN, vendor, version, image_base_path, amplipi_coordinator)
    ]

    # Zones and groups are the main entities. The bus (source) and stream players are optional, for existing automations and the start_streaming blueprint
    if not config_entry.options.get(CONF_SHOW_BUS_STREAM_ENTITIES, False):
        sources = []
        streams = []

    async_add_entities(sources + zones + groups + streams + announcer)
