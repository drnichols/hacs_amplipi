"""Tests for bus allocation"""
from pyamplipi.models import Stream


def test_reuses_bus_already_carrying_stream(routing, status, stream):
    state = status(['', 'stream=1000', '', ''], [1])
    assert routing.pick_bus(state, stream(1000)) == 1


def test_rca_gets_its_fixed_bus_even_when_busy(routing, status, stream):
    state = status(['', '', 'stream=1000', ''], [2])
    assert routing.pick_bus(state, stream(998)) == 2


def test_prefers_empty_unreserved_bus(routing, status, stream):
    state = status(['stream=1000', '', '', ''], [0])
    assert routing.pick_bus(state, stream(1001), reserved={1}) == 2


def test_prefers_idle_unreserved_bus_over_reserved(routing, status, stream):
    # bus 0 and 1 listened, bus 2 idle (no listeners), bus 3 empty but reserved
    state = status(['stream=1000', 'stream=1001', 'stream=1002', ''], [0, 1])
    assert routing.pick_bus(state, stream(1003), reserved={3}) == 2


def test_uses_reserved_bus_only_when_others_exhausted(routing, status, stream):
    state = status(['stream=1000', 'stream=1001', 'stream=1002', ''], [0, 1, 2])
    assert routing.pick_bus(state, stream(1003), reserved={3}) == 3


def test_reserved_bus_in_use_is_not_taken(routing, status, stream):
    state = status(['stream=1000', 'stream=1001', 'stream=1002', 'stream=999'], [0, 1, 2])
    # bus 3 carries its RCA with no listeners, but reserved buses are only borrowed while empty
    assert routing.pick_bus(state, stream(1003), reserved={3}) is None


def test_never_steals_a_listened_bus(routing, status, stream):
    state = status(['stream=1000', 'stream=1001', 'stream=1002', 'stream=1003'], [0, 1, 2, 3])
    assert routing.pick_bus(state, stream(1004)) is None


def test_exclude_skips_current_bus(routing, status, stream):
    state = status(['stream=1000', '', '', ''], [0])
    assert routing.pick_bus(state, stream(1000), exclude={0}) == 1


def test_no_stream_finds_free_bus(routing, status):
    state = status(['stream=1000', '', '', ''], [0], )
    assert routing.pick_bus(state, None, reserved={1}) == 2


def test_none_input_counts_as_empty(routing, status, stream):
    state = status(['None', 'stream=1000', '', ''], [1])
    assert routing.pick_bus(state, stream(1001)) == 0


def test_listening_zones_ignores_disabled_off_and_disconnected(routing, status):
    state = status(['stream=1000', '', '', ''], [0, 0, -1, -2], disabled={1})
    assert [z.id for z in routing.listening_zones(state, 0)] == [0]


def test_idle_buses(routing, status):
    state = status(['stream=1000', 'stream=1001', '', 'stream=1002'], [0, -2], )
    assert routing.idle_buses(state) == {1, 3}


def test_idle_bus_with_only_disabled_listener(routing, status):
    state = status(['stream=1000', '', '', ''], [0], disabled={0})
    assert routing.idle_buses(state) == {0}


def test_stream_on_bus_and_rca_bus(routing, status, stream):
    state = status(['', 'stream=1001', '', ''], [])
    assert routing.stream_on_bus(state, 1).id == 1001
    assert routing.stream_on_bus(state, 0) is None
    assert routing.rca_bus(stream(997)) == 1
    assert routing.rca_bus(stream(1000)) is None


def test_leaving_zone_frees_its_bus_for_reuse(routing, status, stream):
    state = status(['stream=1000', 'stream=1001', 'stream=1002', 'stream=1003'], [0, 1, 2, 3])
    assert routing.pick_bus(state, stream(1004)) is None
    assert routing.pick_bus(state, stream(1004), leaving={2}) == 2


def test_reserved_bus_lent_to_idle_borrower_can_be_reused(routing, status, stream):
    state = status(['stream=1000', 'stream=1001', 'stream=1002', 'stream=1003'], [0, 1, 2])
    assert routing.pick_bus(state, stream(1004), reserved={3}) == 3


def test_music_assistant_streams_recognised(routing):
    assert routing.is_music_assistant_stream(Stream(id=1010, name="Music Assistant 2", type="internetradio"))
    # a user's own radio stream with a similar name, and the right name on another stream type
    assert not routing.is_music_assistant_stream(Stream(id=1011, name="Music Assistant Radio", type="internetradio"))
    assert not routing.is_music_assistant_stream(Stream(id=1012, name="Music Assistant 1", type="spotify"))
    assert not routing.is_music_assistant_stream(Stream(id=1002, name="Radio", type="internetradio"))


def test_only_streams_with_transport_mute_on_pause(routing, stream):
    assert routing.mutes_on_pause(stream(1000))
    assert routing.mutes_on_pause(stream(1002))
    assert not routing.mutes_on_pause(stream(996))
    assert not routing.mutes_on_pause(Stream(id=1010, name="Music Assistant 1", type="internetradio"))
    assert not routing.mutes_on_pause(Stream(id=1020, name="Aux", type="aux"))
    assert not routing.mutes_on_pause(None)


def test_rca_bus_comes_from_its_index(routing):
    assert routing.rca_bus(Stream(id=997, name="Input 2", type="rca", index=1)) == 1


def test_rca_bus_falls_back_to_stream_id_without_index(routing):
    # Older firmware doesn't report index
    assert routing.rca_bus(Stream(id=999, name="Input 4", type="rca")) == 3
    assert routing.rca_bus(Stream(id=1000, name="Spotify", type="spotify", index=0)) is None
