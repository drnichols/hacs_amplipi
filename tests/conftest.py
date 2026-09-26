"""Shared fixtures for the AmpliPi routing tests"""
import importlib.util
from pathlib import Path

import pytest
from pyamplipi.models import Status

ROUTING_PATH = Path(__file__).parent.parent / "custom_components" / "amplipi" / "routing.py"


def _load_routing():
    # Load routing.py on its own so the tests don't need Home Assistant, which the package __init__ imports
    spec = importlib.util.spec_from_file_location("amplipi_routing", ROUTING_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def routing():
    return _load_routing()


STREAMS = [
    {"id": 996, "name": "Input 1", "type": "rca"},
    {"id": 997, "name": "Input 2", "type": "rca"},
    {"id": 998, "name": "Input 3", "type": "rca"},
    {"id": 999, "name": "Input 4", "type": "rca"},
    {"id": 1000, "name": "Spotify", "type": "spotify"},
    {"id": 1001, "name": "AirPlay", "type": "airplay"},
    {"id": 1002, "name": "Radio", "type": "internetradio"},
    {"id": 1003, "name": "Pandora", "type": "pandora"},
    {"id": 1004, "name": "DLNA", "type": "dlna"},
]


def build_status(inputs, zone_sources, disabled=()):
    """
        Build a Status from the input on each of the 4 buses and the source_id of each zone\n
        inputs: e.g. ['stream=1000', '', '', '']\n
        zone_sources: e.g. [0, 0, -1, -2]
    """
    return Status(**{
        "sources": [{"id": i, "name": f"Output {i + 1}", "input": inp} for i, inp in enumerate(inputs)],
        "zones": [
            {"id": i, "name": f"Zone {i + 1}", "source_id": src, "disabled": i in disabled,
             "mute": src < 0, "vol": -40, "vol_f": 0.5, "vol_min": -80, "vol_max": 0}
            for i, src in enumerate(zone_sources)
        ],
        "groups": [],
        "streams": STREAMS,
    })


@pytest.fixture
def status():
    return build_status


@pytest.fixture
def stream():
    def _stream(stream_id):
        return next(s for s in build_status(['', '', '', ''], []).streams if s.id == stream_id)
    return _stream
