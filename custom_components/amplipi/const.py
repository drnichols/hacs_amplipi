"""Constants for the AmpliPi integration."""

DOMAIN = "amplipi"
CONF_VENDOR = "vendor"
CONF_VERSION = "version"
CONF_WEBAPP = "webapp"
CONF_API_PATH = "api_path"

MINIMUM_FIRMWARE = "0.4.7"

# Options
CONF_RESERVED_RCA = "reserved_rca_inputs"
CONF_FREE_IDLE_BUSES = "free_idle_buses"
CONF_IDLE_GRACE_SECONDS = "idle_grace_seconds"
CONF_SHOW_BUS_STREAM_ENTITIES = "show_bus_stream_entities"
CONF_MUTE_ON_PAUSE = "mute_on_pause"
CONF_MUTE_DELAY_SECONDS = "mute_delay_seconds"

DEFAULT_FREE_IDLE_BUSES = True
DEFAULT_IDLE_GRACE_SECONDS = 30
DEFAULT_MUTE_ON_PAUSE = False
DEFAULT_MUTE_DELAY_SECONDS = 30
