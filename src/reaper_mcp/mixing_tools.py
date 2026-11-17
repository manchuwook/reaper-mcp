import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project, is_valid_pointer
from reaper_mcp.units import db_to_gain as _db_to_linear, gain_to_db as _linear_to_db

logger = logging.getLogger("reaper_mcp.mixing_tools")

#: ``I_SENDMODE`` values (REAPER User Guide 17.3, "Send and Receive Types").
SEND_MODES = {"post_fader": 0, "pre_fx": 1, "post_fx": 3}
_SEND_MODE_NAMES = {v: k for k, v in SEND_MODES.items()}
_SEND_MODE_NAMES[2] = "post_fx"  # value 2 is the deprecated spelling of post-FX

#: ``I_SRCCHAN`` value meaning "this send carries no audio" (MIDI-only send).
_NO_AUDIO = -1

#: ``I_AUTOMODE`` values (REAPER User Guide 18.8, "Automation Modes").
AUTOMATION_MODES = {"trim": 0, "off": 0, "read": 1, "touch": 2, "write": 3, "latch": 4}


def _envelope_value(envelope, scaled_value: float) -> float:
    """Convert a value in the envelope's "natural" units to its raw point value.

    Volume envelopes may use *fader scaling* instead of amplitude scaling
    (User Guide 18.14); ReaScript point values are always raw, so a plain
    amplitude written to a fader-scaled envelope would sound wrong.
    """
    mode = RPR.GetEnvelopeScalingMode(envelope)
    return RPR.ScaleToEnvelopeMode(mode, scaled_value)


def register_tools(mcp):

    @mcp.tool()
    def add_volume_automation(track_index: int, position: float, value_db: float) -> dict:
        """
        Add a volume automation point on a track.
        The volume envelope must exist on the track (right-click the track >
        Show envelope for track volume); the point is converted for the
        envelope's amplitude or fader scaling automatically.
        Points only play back when the track's automation mode is read, touch
        or latch (see set_track_automation_mode); in write mode they get overwritten.
        position: time in seconds. value_db: volume level in dB.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            envelope = RPR.GetTrackEnvelopeByName(track.id, "Volume")
            if not is_valid_pointer(envelope):
                return {
                    "success": False,
                    "error": (
                        "Volume envelope not found. Show it first: right-click the track "
                        "in REAPER and choose 'Show envelope for track volume'."
                    ),
                }
            raw = _envelope_value(envelope, _db_to_linear(value_db))
            RPR.InsertEnvelopePoint(envelope, position, raw, 0, 0, False, True)
            RPR.Envelope_SortPoints(envelope)
            return {"success": True, "track_index": track_index, "position": position, "value_db": value_db}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def add_pan_automation(track_index: int, position: float, pan: float) -> dict:
        """
        Add a pan automation point on a track.
        The pan envelope must exist on the track. See add_volume_automation for
        how automation mode affects playback.
        pan: -1.0 (full left) to 1.0 (full right).
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            envelope = RPR.GetTrackEnvelopeByName(track.id, "Pan")
            if not is_valid_pointer(envelope):
                return {
                    "success": False,
                    "error": (
                        "Pan envelope not found. Show it first: right-click the track "
                        "in REAPER and choose 'Show envelope for track pan'."
                    ),
                }
            raw = _envelope_value(envelope, pan)
            RPR.InsertEnvelopePoint(envelope, position, raw, 0, 0, False, True)
            RPR.Envelope_SortPoints(envelope)
            return {"success": True, "track_index": track_index, "position": position, "pan": pan}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_track_automation_mode(track_index: int, mode: str) -> dict:
        """
        Set a track's automation mode: trim (alias off), read, touch, write or latch.
        Automation written with the envelope tools plays back in read, touch and
        latch; write mode records over it. Mode "trim" ignores envelope shape
        for playback except trim envelopes.
        """
        try:
            key = mode.strip().lower()
            if key not in AUTOMATION_MODES:
                return {
                    "success": False,
                    "error": f"Unknown mode '{mode}'. Choose from {sorted(AUTOMATION_MODES)}",
                }
            project = get_project()
            track = project.tracks[track_index]
            RPR.SetMediaTrackInfo_Value(track.id, "I_AUTOMODE", AUTOMATION_MODES[key])
            actual = int(RPR.GetMediaTrackInfo_Value(track.id, "I_AUTOMODE"))
            return {"success": True, "track_index": track_index, "mode": key, "mode_code": actual}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def create_send(
        source_track_index: int,
        dest_track_index: int,
        volume_db: float = 0.0,
        send_mode: str = "post_fader",
        midi_only: bool = False,
    ) -> dict:
        """
        Create a send from one track to another.
        send_mode: post_fader (default), pre_fx or post_fx, i.e. where in the
          source track's signal flow the send taps its audio.
        midi_only: carry only MIDI (no audio). Use it to drive an instrument on
          another track, e.g. a chord track feeding a guitar sampler.
        """
        try:
            if send_mode not in SEND_MODES:
                return {
                    "success": False,
                    "error": f"Unknown send_mode '{send_mode}'. Choose from {sorted(SEND_MODES)}",
                }
            if source_track_index == dest_track_index:
                return {"success": False, "error": "A track cannot send to itself"}
            project = get_project()
            src = project.tracks[source_track_index]
            dst = project.tracks[dest_track_index]
            send_idx = RPR.CreateTrackSend(src.id, dst.id)
            if send_idx < 0:
                return {"success": False, "error": "Failed to create send"}
            RPR.SetTrackSendInfo_Value(src.id, 0, send_idx, "D_VOL", _db_to_linear(volume_db))
            RPR.SetTrackSendInfo_Value(src.id, 0, send_idx, "I_SENDMODE", SEND_MODES[send_mode])
            if midi_only:
                RPR.SetTrackSendInfo_Value(src.id, 0, send_idx, "I_SRCCHAN", _NO_AUDIO)
            return {
                "success": True,
                "source_track_index": source_track_index,
                "dest_track_index": dest_track_index,
                "send_index": send_idx,
                "volume_db": volume_db,
                "send_mode": send_mode,
                "midi_only": midi_only,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def list_sends(track_index: int) -> dict:
        """List all sends from a track: level, pan, mute, send mode and what they carry."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            n = RPR.GetTrackNumSends(track.id, 0)
            sends = []
            for i in range(n):
                vol = RPR.GetTrackSendInfo_Value(track.id, 0, i, "D_VOL")
                pan = RPR.GetTrackSendInfo_Value(track.id, 0, i, "D_PAN")
                muted = bool(RPR.GetTrackSendInfo_Value(track.id, 0, i, "B_MUTE"))
                mode = int(RPR.GetTrackSendInfo_Value(track.id, 0, i, "I_SENDMODE"))
                src_chan = int(RPR.GetTrackSendInfo_Value(track.id, 0, i, "I_SRCCHAN"))
                midi_flags = int(RPR.GetTrackSendInfo_Value(track.id, 0, i, "I_MIDIFLAGS"))
                sends.append({
                    "send_index": i,
                    "volume_db": round(_linear_to_db(vol), 2),
                    "volume_linear": vol,
                    "pan": pan,
                    "muted": muted,
                    "send_mode": _SEND_MODE_NAMES.get(mode, f"unknown({mode})"),
                    "carries_audio": src_chan != _NO_AUDIO,
                    "carries_midi": (midi_flags & 31) != 31,
                })
            return {"success": True, "track_index": track_index, "sends": sends}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def remove_send(source_track_index: int, send_index: int) -> dict:
        """Remove a send from a track by its index."""
        try:
            project = get_project()
            track = project.tracks[source_track_index]
            RPR.RemoveTrackSend(track.id, 0, send_index)
            return {"success": True, "source_track_index": source_track_index, "send_index": send_index}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_send_volume(source_track_index: int, send_index: int, volume_db: float) -> dict:
        """Set the volume of a send in dB."""
        try:
            project = get_project()
            track = project.tracks[source_track_index]
            RPR.SetTrackSendInfo_Value(track.id, 0, send_index, "D_VOL", _db_to_linear(volume_db))
            return {
                "success": True,
                "source_track_index": source_track_index,
                "send_index": send_index,
                "volume_db": volume_db,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def create_bus(name: str, track_indices: list, submix: bool = True) -> dict:
        """
        Create a new bus track and route the given tracks to it via sends.
        track_indices: list of track indices to feed into the bus.
        submix: True (default) turns off each source track's direct send to the
          master/parent, so the bus is a true submix. Leaving it on would deliver
          the dry signal to the master twice (directly and through the bus), which
          is only wanted for an FX bus such as a shared reverb: pass submix=False
          for that. (REAPER folders are another way to build submixes.)
        """
        try:
            project = get_project()
            count = project.n_tracks
            bad = [i for i in track_indices if not (0 <= i < count)]
            if bad:  # validate first so a bad index doesn't leave a half-built bus
                return {"success": False, "error": f"Track indices out of range: {bad}"}
            bus_idx = count
            project.add_track(bus_idx, name)
            bus_track = project.tracks[bus_idx]
            sends = []
            for idx in track_indices:
                src = project.tracks[idx]
                send_i = RPR.CreateTrackSend(src.id, bus_track.id)
                if submix:
                    RPR.SetMediaTrackInfo_Value(src.id, "B_MAINSEND", 0)
                sends.append({"track_index": idx, "send_index": send_i})
            return {
                "success": True,
                "bus_index": bus_idx,
                "bus_name": name,
                "sends": sends,
                "master_send_disabled": submix,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
