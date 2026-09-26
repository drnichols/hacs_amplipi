"""Tests for the AmpliPi config flow, with the controller API faked"""
from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amplipi.const import DOMAIN, CONF_WEBAPP

GET_STATUS = "custom_components.amplipi.config_flow.AmpliPi.get_status"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_setup():
    # Only the flow is under test, so don't set the integration up once the entry is created
    with patch("custom_components.amplipi.async_setup_entry", return_value=True):
        yield


def zeroconf_info(host="10.0.0.5", **properties):
    props = {"name": "AmpliPi", "vendor": "micro-nova", "version": "0.4.9",
             "web_app": f"http://{host}", "path": "/api"}
    props.update(properties)
    return ZeroconfServiceInfo(
        ip_address=ip_address(host), ip_addresses=[ip_address(host)], port=80, hostname="amplipi.local.",
        type="_amplipi._tcp.local.", name="amplipi._amplipi._tcp.local.", properties=props,
    )


async def start_user_flow(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return result


async def test_user_creates_entry(hass):
    result = await start_user_flow(hass)
    with patch(GET_STATUS, AsyncMock(return_value={})):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "10.0.0.5", CONF_PORT: 80})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "10.0.0.5"
    assert result["data"][CONF_WEBAPP] == "http://10.0.0.5"


@pytest.mark.parametrize("error", [ClientError(), TimeoutError()])
async def test_user_cannot_connect(hass, error):
    result = await start_user_flow(hass)
    with patch(GET_STATUS, AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "10.0.0.5", CONF_PORT: 80})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_aborts_when_host_already_configured(hass):
    MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "10.0.0.5", CONF_PORT: 80}).add_to_hass(hass)
    result = await start_user_flow(hass)
    with patch(GET_STATUS, AsyncMock(return_value={})):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "10.0.0.5", CONF_PORT: 80})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zeroconf_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    with patch(GET_STATUS, AsyncMock(return_value={})):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "amplipi._amplipi._tcp.local."
    assert result["data"][CONF_HOST] == "10.0.0.5"


async def test_zeroconf_aborts_when_host_added_manually(hass):
    MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "10.0.0.5", CONF_PORT: 80}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zeroconf_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_tolerates_missing_properties(hass):
    info = zeroconf_info()
    info.properties.clear()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    with patch(GET_STATUS, AsyncMock(return_value={})):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_WEBAPP] == "http://10.0.0.5"
