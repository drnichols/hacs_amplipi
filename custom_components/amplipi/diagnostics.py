"""Diagnostics support for AmpliPi."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import AmpliPiConfigEntry
from .const import CONF_WEBAPP

TO_REDACT = {CONF_HOST, CONF_WEBAPP, "configuration_url"}
# Stream configs hold credentials for services such as Pandora, and info holds the controller's access key and serial numbers
STATUS_TO_REDACT = {"user", "password", "token", "client_id", "config_file", "access_key", "serial", "expanders"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AmpliPiConfigEntry) -> dict[str, Any]:
    """Return the config entry and the controller's last status."""
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "last_update_success": coordinator.last_update_success,
        "status": async_redact_data(coordinator.data.model_dump(mode="json"), STATUS_TO_REDACT)
        if coordinator.data is not None else None,
    }
