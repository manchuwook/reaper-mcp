import logging
import reapy
from reapy import reascript_api as RPR

logger = logging.getLogger("reaper_mcp.connection")

_connected = False


def _is_alive() -> bool:
    """Cheap round trip that tells a live socket from one REAPER dropped.

    reapy holds one socket for the life of the process. If REAPER is restarted
    (or the distant API server stops) that socket is dead, and every later call
    fails until the MCP server itself is restarted -- unless we notice and
    reconnect.
    """
    try:
        RPR.GetAppVersion()
        return True
    except Exception:
        return False


def ensure_connected() -> None:
    """Connect to REAPER, reconnecting if an earlier connection has gone away."""
    global _connected
    reconnecting = _connected
    if _connected:
        if _is_alive():
            return
        logger.warning("Lost connection to REAPER; reconnecting")
        _connected = False
    try:
        # reapy.reconnect() discards the dead client and regenerates the API
        # proxy functions; plain connect() is only right for the first attempt.
        if reconnecting and hasattr(reapy, "reconnect"):
            reapy.reconnect()
        else:
            reapy.connect()
        if reconnecting and not _is_alive():
            raise RuntimeError("REAPER did not answer after reconnecting")
        _connected = True
        logger.info("Connected to REAPER")
    except Exception as e:
        raise RuntimeError(
            f"Cannot connect to REAPER: {e}. "
            "Make sure REAPER is running and the distant API is enabled. "
            "To enable it: run the setup script (scripts/enable_reapy.py) or "
            "in REAPER go to Actions > Run ReaScript, then run: "
            "import reapy; reapy.config.enable_dist_api()"
        ) from e


def get_project() -> reapy.Project:
    ensure_connected()
    return reapy.Project()


def is_valid_pointer(pointer) -> bool:
    """Return True if a ReaScript pointer refers to a real object.

    ReaScript returns object pointers as *strings* shaped like
    ``"(TrackEnvelope*)0x0000000000000000"``, and a null pointer is that same
    string with an all-zero address. Because a non-empty string is always
    truthy in Python, the natural-looking ``if not envelope:`` guard never
    fires and callers happily go on to use the null pointer -- which surfaces
    as nonsense return values such as ``CountEnvelopePoints() == -1`` rather
    than as an exception.

    Handles the other shapes ReaScript can hand back too: ``None``, and the
    integer ``0`` used by some functions.
    """
    if not pointer:
        return False
    if isinstance(pointer, str):
        address = pointer.rpartition(")")[2].strip()
        try:
            return int(address, 16) != 0
        except ValueError:
            # Unrecognised shape -- treat a non-empty string as valid rather
            # than silently discarding a pointer we simply failed to parse.
            return True
    return True
