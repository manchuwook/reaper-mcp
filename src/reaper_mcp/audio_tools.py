import os
import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project

logger = logging.getLogger("reaper_mcp.audio_tools")


def register_tools(mcp):

    @mcp.tool()
    def import_audio_file(file_path: str, track_index: int, position: float = 0.0) -> dict:
        """
        Import an audio file onto a track at the given position (seconds).
        Supports all formats REAPER can read: wav, aiff, mp3, flac, ogg, etc.
        """
        try:
            if not os.path.exists(file_path):
                return {"success": False, "error": f"File not found: {file_path}"}
            project = get_project()
            track = project.tracks[track_index]
            # Select only this track, set cursor, then insert media at cursor
            RPR.SetOnlyTrackSelected(track.id)
            project.cursor_position = position
            RPR.InsertMedia(file_path, 0)
            # Retrieve the item that was just created (last item on the track)
            track_refreshed = project.tracks[track_index]
            if track_refreshed.n_items == 0:
                return {"success": False, "error": "Insert succeeded but no item found on track"}
            item = track_refreshed.items[track_refreshed.n_items - 1]
            return {
                "success": True,
                "track_index": track_index,
                "item_index": track_refreshed.n_items - 1,
                "position": item.position,
                "length": item.length,
                "file_path": file_path,
            }
        except Exception as e:
            logger.error(f"import_audio_file failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def start_recording(track_index: int) -> dict:
        """Arm a track and start recording. Call stop_transport when done."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            # reapy's Track has no `armed` attribute: assigning one silently does
            # nothing, and transport-record then records no track at all.
            RPR.SetMediaTrackInfo_Value(track.id, "I_RECARM", 1)
            armed = bool(RPR.GetMediaTrackInfo_Value(track.id, "I_RECARM"))
            if not armed:
                return {"success": False, "error": "REAPER did not arm the track"}
            RPR.Main_OnCommand(1013, 0)  # Transport: Record
            return {
                "success": True,
                "track_index": track_index,
                "armed": armed,
                "message": "Recording started. Call stop_transport to stop.",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def stop_transport() -> dict:
        """Stop playback or recording."""
        try:
            RPR.Main_OnCommand(1016, 0)  # Transport: Stop
            return {"success": True, "message": "Transport stopped"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def play_project() -> dict:
        """Start project playback from the current cursor position."""
        try:
            RPR.Main_OnCommand(1007, 0)  # Transport: Play
            return {"success": True, "message": "Playback started"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_cursor_position(position: float) -> dict:
        """Move the edit cursor to a position in seconds."""
        try:
            project = get_project()
            project.cursor_position = position
            return {"success": True, "position": project.cursor_position}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def edit_audio_item(
        track_index: int,
        item_index: int,
        start_trim: float = 0.0,
        end_trim: float = 0.0,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
    ) -> dict:
        """
        Trim an audio item and/or add fades. All values in seconds.
        start_trim: seconds to remove from the beginning.
        end_trim: seconds to remove from the end.
        fade_in/fade_out: fade length in seconds.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            item = track.items[item_index]

            if start_trim > 0:
                take = item.active_take
                if take:
                    # Source offset is measured in source time, which runs at
                    # the take's playback rate.
                    rate = RPR.GetMediaItemTakeInfo_Value(take.id, "D_PLAYRATE") or 1.0
                    offset = RPR.GetMediaItemTakeInfo_Value(take.id, "D_STARTOFFS")
                    RPR.SetMediaItemTakeInfo_Value(
                        take.id, "D_STARTOFFS", offset + start_trim * rate
                    )
                item.position += start_trim
                item.length -= start_trim

            if end_trim > 0:
                item.length -= end_trim

            # reapy's Item has no fade properties (assigning one is a silent
            # no-op), so write the item's D_FADEINLEN / D_FADEOUTLEN directly.
            if fade_in > 0:
                RPR.SetMediaItemInfo_Value(item.id, "D_FADEINLEN", fade_in)

            if fade_out > 0:
                RPR.SetMediaItemInfo_Value(item.id, "D_FADEOUTLEN", fade_out)

            return {
                "success": True,
                "track_index": track_index,
                "item_index": item_index,
                "position": item.position,
                "length": item.length,
                "fade_in": RPR.GetMediaItemInfo_Value(item.id, "D_FADEINLEN"),
                "fade_out": RPR.GetMediaItemInfo_Value(item.id, "D_FADEOUTLEN"),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def adjust_pitch(track_index: int, item_index: int, semitones: float) -> dict:
        """Adjust the pitch of an audio item by semitones (can be fractional)."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            item = track.items[item_index]
            take = item.active_take
            # reapy's Take has no `pitch` attribute; write D_PITCH directly.
            RPR.SetMediaItemTakeInfo_Value(take.id, "D_PITCH", semitones)
            return {
                "success": True,
                "track_index": track_index,
                "item_index": item_index,
                "pitch_semitones": RPR.GetMediaItemTakeInfo_Value(take.id, "D_PITCH"),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def adjust_playback_rate(
        track_index: int,
        item_index: int,
        rate: float,
        preserve_pitch: bool = True,
        adjust_length: bool = True,
    ) -> dict:
        """
        Adjust playback rate of an audio item. 1.0 = normal speed, 0.5 = half speed.
        preserve_pitch: time-stretch (True) or tape-style speed change that also
          shifts pitch (False).
        adjust_length: resize the item so it still covers the same source audio,
          as REAPER's item properties dialog does. With False the item keeps its
          length and plays a different stretch of the source.
        """
        try:
            if rate <= 0:
                return {"success": False, "error": f"rate must be > 0, got {rate}"}
            project = get_project()
            track = project.tracks[track_index]
            item = track.items[item_index]
            take = item.active_take
            old_rate = RPR.GetMediaItemTakeInfo_Value(take.id, "D_PLAYRATE") or 1.0
            RPR.SetMediaItemTakeInfo_Value(take.id, "D_PLAYRATE", rate)
            RPR.SetMediaItemTakeInfo_Value(take.id, "B_PPITCH", 1 if preserve_pitch else 0)
            if adjust_length:
                item.length = item.length * old_rate / rate
            return {
                "success": True,
                "track_index": track_index,
                "item_index": item_index,
                "playback_rate": RPR.GetMediaItemTakeInfo_Value(take.id, "D_PLAYRATE"),
                "preserve_pitch": preserve_pitch,
                "length": item.length,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
