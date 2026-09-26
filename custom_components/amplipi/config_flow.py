"""Config flow for AmpliPi."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries, exceptions
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_ID, CONF_NAME, CONF_PORT, CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pyamplipi.amplipi import AmpliPi

from .const import (
    DOMAIN, CONF_VENDOR, CONF_VERSION, CONF_WEBAPP, CONF_API_PATH,
    CONF_RESERVED_RCA, CONF_FREE_IDLE_BUSES, CONF_IDLE_GRACE_SECONDS, CONF_SHOW_BUS_STREAM_ENTITIES,
    DEFAULT_FREE_IDLE_BUSES, DEFAULT_IDLE_GRACE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 80


async def async_retrieve_info(hass, host, port):
    """Validate the user input allows us to connect."""
    client = AmpliPi(f"http://{host}:{port}/api/", 10, async_get_clientsession(hass))
    try:
        async with asyncio.timeout(10):
            return await client.get_status()
    except (ClientError, TimeoutError) as err:
        _LOGGER.debug("Error connecting to AmpliPi Controller at %s:%s: %s", host, port, err)
        raise CannotConnect from err


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AmpliPi."""

    VERSION = 1
    # Minor version 2 added options. Entries from before that are migrated in __init__.async_migrate_entry
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()

    def __init__(self):
        """Initialize flow."""
        self._controller_hostname: str | None = None
        self._controller_port: int = DEFAULT_PORT
        self._name: str = "AmpliPi"
        self._vendor: str = "Unknown"
        self._version: str = "Unknown"
        self._webapp_url: str | None = None
        self._api_path: str = "/api"

    @callback
    def _async_get_entry(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._name,
            description="AmpliPi Multizone Media Controller",
            data={
                CONF_NAME: self._name,
                CONF_HOST: self._controller_hostname,
                CONF_PORT: self._controller_port,
                CONF_ID: self.unique_id,
                CONF_VENDOR: self._vendor,
                CONF_VERSION: self._version,
                CONF_WEBAPP: self._webapp_url,
                CONF_API_PATH: self._api_path,
            },
            # New installs are zone-centric, so the bus and stream entities start hidden
            options={CONF_SHOW_BUS_STREAM_ENTITIES: False},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_user_confirm(user_input)

    async def async_step_user_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            # AmpliPi doesn't report a unique id over the API, so manual entries are matched on host
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            try:
                await async_retrieve_info(self.hass, user_input[CONF_HOST], user_input[CONF_PORT])
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self._controller_hostname = user_input[CONF_HOST]
                self._controller_port = user_input[CONF_PORT]
                self._webapp_url = f"http://{user_input[CONF_HOST]}"
                return self._async_get_entry()

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=self._controller_hostname or vol.UNDEFINED): str,
                vol.Required(CONF_PORT, default=self._controller_port): int,
            }
        )
        return self.async_show_form(step_id="user_confirm", data_schema=schema, errors=errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Handle zeroconf discovery."""
        _LOGGER.debug("Discovered %s", discovery_info)
        properties = discovery_info.properties
        self._controller_hostname = discovery_info.host
        self._controller_port = discovery_info.port or DEFAULT_PORT
        self._name = properties.get("name") or self._name
        self._vendor = properties.get("vendor") or self._vendor
        self._version = properties.get("version") or self._version
        self._webapp_url = properties.get("web_app") or f"http://{discovery_info.host}"
        self._api_path = properties.get("path") or self._api_path

        await self.async_set_unique_id(discovery_info.name)
        self._abort_if_unique_id_configured(
            updates={
                CONF_NAME: self._name,
                CONF_HOST: self._controller_hostname,
                CONF_PORT: self._controller_port,
                CONF_VENDOR: self._vendor,
                CONF_VERSION: self._version,
                CONF_WEBAPP: self._webapp_url,
                CONF_API_PATH: self._api_path,
            }
        )
        # Don't offer a controller that was already added by hand
        self._async_abort_entries_match({CONF_HOST: self._controller_hostname})
        self.context["title_placeholders"] = {CONF_NAME: self._name}

        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle user-confirmation of discovered node."""
        errors = {}
        if user_input is not None:
            try:
                await async_retrieve_info(self.hass, self._controller_hostname, self._controller_port)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self._async_get_entry()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            errors=errors,
            description_placeholders={
                CONF_NAME: self._name,
                CONF_HOST: self._controller_hostname,
                CONF_VENDOR: self._vendor,
                CONF_VERSION: self._version,
            },
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle AmpliPi options: bus allocation and which entities to create."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        # Keys are bus ids as strings; RCA input N can only ever play on bus N
        rca_inputs = {str(bus): f"Input {bus + 1}" for bus in range(4)}

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RESERVED_RCA,
                    default=options.get(CONF_RESERVED_RCA, []),
                ): cv.multi_select(rca_inputs),
                vol.Optional(
                    CONF_FREE_IDLE_BUSES,
                    default=options.get(CONF_FREE_IDLE_BUSES, DEFAULT_FREE_IDLE_BUSES),
                ): bool,
                vol.Optional(
                    CONF_IDLE_GRACE_SECONDS,
                    default=options.get(CONF_IDLE_GRACE_SECONDS, DEFAULT_IDLE_GRACE_SECONDS),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=600)),
                vol.Optional(
                    CONF_SHOW_BUS_STREAM_ENTITIES,
                    default=options.get(CONF_SHOW_BUS_STREAM_ENTITIES, False),
                ): bool,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""
