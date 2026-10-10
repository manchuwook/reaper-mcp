"""MCP tools for creating and manipulating REAPER tracks.

Notes on the reapy API
----------------------
``reapy.core.Track`` does *not* expose ``volume`` or ``pan`` attributes, and
``mute`` / ``solo`` are **methods**, not settable properties. Assigning to them
(``track.mute = True``) silently sets a Python instance attribute and never
reaches REAPER, so this module deliberately avoids that pattern.

Instead:

* volume / pan go through ``get_info_value`` / ``set_info_value`` on the
  underlying ``MediaTrack`` parameters ``D_VOL`` and ``D_PAN``;
* mute / solo use the ``is_muted`` and ``is_solo`` properties.

``D_VOL`` is a *linear amplitude* factor, not decibels: 1.0 is unity gain,
0.0 is silence, 2.0 is +6 dB. The public tool surface here speaks dB because
that is what the REAPER UI shows, so every read is converted out of linear and
every write is converted into it.
"""

import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project
from reaper_mcp.units import db_to_gain as _db_to_gain, gain_to_db as _gain_to_db

logger = logging.getLogger("reaper_mcp.track_tools")

#: ``I_RECINPUT`` for "MIDI, all devices, all channels": bit 4096 marks MIDI input,
#: bits 5-10 hold the physical device (63 = all) and the low 5 bits the channel
#: (0 = all). A bare 4096 would mean device 0 only.
MIDI_ALL_INPUTS = 4096 | (63 << 5)


def _track_summary(track, index: int) -> dict:
    """Build the common summary payload shared by list_tracks and get_track_info.

    Kept in one place so the two tools can never drift apart in field names or
    units.
    """
    return {
        "index": index,
        "name": track.name,
        "volume_db": round(_gain_to_db(track.get_info_value("D_VOL")), 2),
        "pan": round(track.get_info_value("D_PAN"), 4),
        "muted": track.is_muted,
        "soloed": track.is_solo,
        "fx_count": track.n_fxs,
        "item_count": track.n_items,
    }


def register_tools(mcp):

    @mcp.tool()
    def create_track(name: str, track_type: str = "audio") -> dict:
        """
        Create a new track at the end of the project.
        track_type: audio, midi, instrument, folder
        midi and instrument tracks record from all MIDI inputs on all channels.
        A folder track opens a folder that swallows every track created after it
        until one is closed with set_track_folder_depth(index, -1).
        """
        try:
            project = get_project()
            idx = project.n_tracks
            project.add_track(idx, name)
            track = project.tracks[idx]

            if track_type in ("midi", "instrument"):
                RPR.SetMediaTrackInfo_Value(track.id, "I_RECINPUT", MIDI_ALL_INPUTS)
            elif track_type == "folder":
                RPR.SetMediaTrackInfo_Value(track.id, "I_FOLDERDEPTH", 1)

            return {
                "success": True,
                "track_index": idx,
                "name": track.name,
                "type": track_type,
            }
        except Exception as e:
            logger.error(f"create_track failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_track_folder_depth(track_index: int, depth: int) -> dict:
        """
        Set a track's folder depth change (REAPER's I_FOLDERDEPTH).
        1 = this track starts a folder; 0 = normal track; -1 = this is the last
        child, closing one folder level (-2 closes two, and so on).
        Child tracks inside a folder are submixed by it, so don't disable their
        parent send by hand.
        """
        try:
            if depth > 1:
                return {"success": False, "error": "depth must be 1, 0 or a negative number"}
            project = get_project()
            track = project.tracks[track_index]
            RPR.SetMediaTrackInfo_Value(track.id, "I_FOLDERDEPTH", depth)
            actual = int(RPR.GetMediaTrackInfo_Value(track.id, "I_FOLDERDEPTH"))
            return {"success": True, "track_index": track_index, "folder_depth": actual}
        except Exception as e:
            logger.error(f"set_track_folder_depth failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def delete_track(track_index: int) -> dict:
        """Delete a track by its index."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            track.delete()
            return {"success": True, "deleted_index": track_index}
        except Exception as e:
            logger.error(f"delete_track failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def rename_track(track_index: int, name: str) -> dict:
        """Rename a track."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            track.name = name
            return {"success": True, "track_index": track_index, "name": track.name}
        except Exception as e:
            logger.error(f"rename_track failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_track_volume(track_index: int, volume_db: float) -> dict:
        """Set track volume in dB. Range: roughly -150 to +12 dB."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            track.set_info_value("D_VOL", _db_to_gain(volume_db))
            actual = _gain_to_db(track.get_info_value("D_VOL"))
            return {
                "success": True,
                "track_index": track_index,
                "volume_db": round(actual, 2),
            }
        except Exception as e:
            logger.error(f"set_track_volume failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_track_pan(track_index: int, pan: float) -> dict:
        """Set track pan. -1.0 = full left, 0.0 = center, 1.0 = full right."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            pan = max(-1.0, min(1.0, pan))
            track.set_info_value("D_PAN", pan)
            return {
                "success": True,
                "track_index": track_index,
                "pan": round(track.get_info_value("D_PAN"), 4),
            }
        except Exception as e:
            logger.error(f"set_track_pan failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_track_mute(track_index: int, muted: bool) -> dict:
        """Mute or unmute a track."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            track.is_muted = muted
            return {"success": True, "track_index": track_index, "muted": track.is_muted}
        except Exception as e:
            logger.error(f"set_track_mute failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_track_solo(track_index: int, soloed: bool) -> dict:
        """Solo or unsolo a track."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            track.is_solo = soloed
            return {"success": True, "track_index": track_index, "soloed": track.is_solo}
        except Exception as e:
            logger.error(f"set_track_solo failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def get_track_info(track_index: int) -> dict:
        """Get detailed information about a track including FX and items."""
        try:
            project = get_project()
            track = project.tracks[track_index]

            fx_list = []
            for i in range(track.n_fxs):
                fx = track.fxs[i]
                fx_list.append({"index": i, "name": fx.name, "enabled": fx.is_enabled})

            items = []
            for i in range(track.n_items):
                item = track.items[i]
                items.append({
                    "index": i,
                    "position": item.position,
                    "length": item.length,
                    "name": item.name,
                })

            info = _track_summary(track, track_index)
            info.update({
                "success": True,
                "track_index": track_index,
                "fx": fx_list,
                "items": items,
            })
            return info
        except Exception as e:
            logger.error(f"get_track_info failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def list_tracks() -> dict:
        """List all tracks in the current project with their basic parameters."""
        try:
            project = get_project()
            tracks = [
                _track_summary(project.tracks[i], i)
                for i in range(project.n_tracks)
            ]
            return {"success": True, "count": len(tracks), "tracks": tracks}
        except Exception as e:
            logger.error(f"list_tracks failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_track_color(track_index: int, r: int, g: int, b: int) -> dict:
        """Set track color using RGB values (0–255 each)."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            track.color = (r, g, b)
            return {"success": True, "track_index": track_index, "r": r, "g": g, "b": b}
        except Exception as e:
            logger.error(f"set_track_color failed: {e}")
            return {"success": False, "error": str(e)}
