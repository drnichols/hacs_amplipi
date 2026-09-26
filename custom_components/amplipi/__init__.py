"""The AmpliPi integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from packaging.version import InvalidVersion, Version

from .coordinator import AmpliPiDataClient
from .const import DOMAIN, CONF_VENDOR, CONF_VERSION, CONF_WEBAPP, CONF_SHOW_BUS_STREAM_ENTITIES, MINIMUM_FIRMWARE

PLATFORMS = ["media_player"]

_LOGGER = logging.getLogger(__name__)

type AmpliPiConfigEntry = ConfigEntry[AmpliPiDataClient]


async def async_setup_entry(hass: HomeAssistant, entry: AmpliPiConfigEntry) -> bool:
    coordinator = AmpliPiDataClient(
            hass=hass,
            config_entry=entry,
            logger=_LOGGER,
            endpoint=f'http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}/api/',
            timeout=10,
            http_session=async_get_clientsession(hass)
        )
    # Raises ConfigEntryNotReady if the controller can't be reached, so Home Assistant retries later
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    firmware = coordinator.data.info.version if coordinator.data.info else None
    _check_firmware(hass, entry, firmware)

    # The controller itself, which the zone and group devices hang off
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data[CONF_NAME],
        manufacturer=entry.data[CONF_VENDOR],
        model="AmpliPi",
        sw_version=firmware or entry.data[CONF_VERSION],
        configuration_url=entry.data[CONF_WEBAPP],
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


def _check_firmware(hass: HomeAssistant, entry: ConfigEntry, firmware: str | None) -> None:
    """Raise a repair issue if the controller's firmware is older than this integration supports"""
    issue_id = f"firmware_too_old_{entry.entry_id}"
    try:
        too_old = firmware is not None and Version(firmware) < Version(MINIMUM_FIRMWARE)
    except InvalidVersion:
        too_old = False
    if too_old:
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="firmware_too_old",
            translation_placeholders={
                "version": firmware,
                "minimum": MINIMUM_FIRMWARE,
                "update_url": f"{entry.data[CONF_WEBAPP]}/update",
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_reload_entry(hass: HomeAssistant, entry: AmpliPiConfigEntry) -> None:
    """Reload when the options change, so entities are rebuilt with the new settings."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: AmpliPiConfigEntry) -> bool:
    """Migrate entries created before options existed."""
    if entry.version == 1 and entry.minor_version < 2:
        # Existing installs keep their bus and stream entities so automations and dashboards don't break
        options = {CONF_SHOW_BUS_STREAM_ENTITIES: True, **entry.options}
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        _LOGGER.info("Migrated AmpliPi config entry to version 1.2")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AmpliPiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
