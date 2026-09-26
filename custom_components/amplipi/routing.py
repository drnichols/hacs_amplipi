"""
    Bus allocation for AmpliPi
    AmpliPi has 4 sources (buses). A stream has to be placed on a bus before any zone can hear it, and each zone listens to one bus.
    These helpers decide which bus a stream should use. They only read state and never talk to the API, so they can be tested without Home Assistant.
"""
from typing import Iterable, List, Optional, Set

from pyamplipi.models import Source, Stream, Zone, Status

RCA_STREAM_IDS = (996, 997, 998, 999)
EMPTY_INPUTS = ('', 'None', None)

# Music Assistant's AmpliPi provider plays through internetradio streams it names "Music Assistant <bus>"
MUSIC_ASSISTANT_STREAM_TYPE = "internetradio"
MUSIC_ASSISTANT_STREAM_PREFIX = "Music Assistant "


def is_music_assistant_stream(stream: Stream) -> bool:
    """Is this one of the streams Music Assistant creates and drives. Matches Music Assistant's own rule, so a user stream such as "Music Assistant Radio" isn't caught"""
    name = stream.name or ""
    if stream.type != MUSIC_ASSISTANT_STREAM_TYPE or not name.startswith(MUSIC_ASSISTANT_STREAM_PREFIX):
        return False
    return name[len(MUSIC_ASSISTANT_STREAM_PREFIX):].strip().isdigit()


def is_bus_empty(source: Source) -> bool:
    """Does the bus have no stream on it"""
    return source.input in EMPTY_INPUTS


def stream_input(stream_id: int) -> str:
    """The source input string that places a stream on a bus"""
    return f"stream={stream_id}"


def rca_bus(stream: Stream) -> Optional[int]:
    """The only bus an RCA stream can use, or None for any other stream type"""
    if stream.type == "rca" and stream.id in RCA_STREAM_IDS:
        return stream.id - RCA_STREAM_IDS[0]
    return None


def listening_zones(state: Status, bus_id: int, leaving: Iterable[int] = ()) -> List[Zone]:
    """
        Enabled zones listening to a bus. Disconnected (-1) and off (-2) zones never match a bus id\n
        leaving: ids of zones about to be pointed elsewhere, which no longer count as listening
    """
    leaving = set(leaving)
    return [z for z in state.zones if z.source_id == bus_id and not z.disabled and z.id not in leaving]


def bus_for_stream(state: Status, stream_id: int) -> Optional[int]:
    """The bus a stream is currently on, if any"""
    return next((s.id for s in state.sources if s.input == stream_input(stream_id)), None)


def stream_on_bus(state: Status, bus_id: int) -> Optional[Stream]:
    """The stream currently placed on a bus, if any"""
    source = next((s for s in state.sources if s.id == bus_id), None)
    if source is None or is_bus_empty(source):
        return None
    return next((s for s in state.streams if source.input == stream_input(s.id)), None)


def idle_buses(state: Status, leaving: Iterable[int] = ()) -> Set[int]:
    """Buses that have a stream on them but no zone listening"""
    return {s.id for s in state.sources if not is_bus_empty(s) and not listening_zones(state, s.id, leaving)}


def listened_buses(state: Status) -> Set[int]:
    """Buses that at least one enabled zone is listening to"""
    return {s.id for s in state.sources if listening_zones(state, s.id)}


def pick_bus(state: Status, stream: Optional[Stream], reserved: Iterable[int] = (), exclude: Iterable[int] = (),
             leaving: Iterable[int] = ()) -> Optional[int]:
    """
        Choose the bus a stream should play on. In order of preference:\n
        1. The bus the stream is already on\n
        2. For an RCA stream, its fixed bus (the caller must clear whatever is on it)\n
        3. An empty bus that isn't reserved for an RCA\n
        4. A bus that isn't reserved and has a stream but no listeners\n
        5. A bus reserved for an RCA, only once every other bus is in use, and only while it is empty or lent to a stream nobody is listening to\n
        Returns None rather than take a bus that zones are listening to.\n
        leaving: ids of zones that are switching to this stream, so the bus they leave can be reused
    """
    reserved = set(reserved)
    exclude = set(exclude)

    if stream is not None:
        current = bus_for_stream(state, stream.id)
        if current is not None and current not in exclude:
            return current
        fixed = rca_bus(stream)
        if fixed is not None:
            return fixed if fixed not in exclude else None

    candidates = [s for s in state.sources if s.id not in exclude]
    idle = idle_buses(state, leaving)

    for source in candidates:
        if source.id not in reserved and is_bus_empty(source):
            return source.id
    for source in candidates:
        if source.id not in reserved and source.id in idle:
            return source.id
    for source in candidates:
        if source.id not in reserved:
            continue
        if is_bus_empty(source):
            return source.id
        on_bus = stream_on_bus(state, source.id)
        # A reserved bus holding its own RCA stays with it, even with no listeners
        if source.id in idle and on_bus is not None and on_bus.type != "rca":
            return source.id
    return None
