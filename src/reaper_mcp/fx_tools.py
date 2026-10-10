import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project

logger = logging.getLogger("reaper_mcp.fx_tools")

# Buffer size for ReaScript string out-parameters.
_STR_BUF = 512


def _param_name(track, fx_index: int, param_index: int) -> str:
    """Parameter name via ReaScript.

    String out-params must be passed as a string buffer plus its size; the name
    comes back in the last slot of the returned tuple.
    """
    return RPR.TrackFX_GetParamName(
        track.id, fx_index, param_index, "", _STR_BUF
    )[4]


def _param_formatted(track, fx_index: int, param_index: int) -> str:
    """Parameter value as the plugin formats it for display (e.g. "70", "-6.0 dB")."""
    return RPR.TrackFX_GetFormattedParamValue(
        track.id, fx_index, param_index, "", _STR_BUF
    )[4]


def register_tools(mcp):

    @mcp.tool()
    def add_fx(track_index: int, fx_name: str) -> dict:
        """
        Add an FX plugin to a track. Works for both instruments (VSTi) and effects (VST/AU).
        Use the exact plugin name as shown in REAPER's FX browser.
        Built-in Cockos plugins: ReaEQ, ReaComp, ReaDelay, ReaVerb, ReaLimit, ReaSynth,
        ReaSamplOmatic5000, ReaTune, ReaGate, ReaFIR, ReaXcomp.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            # reapy's Track.add_fx returns an FX object (raising ValueError when the
            # plugin name is unknown), NOT an integer index.
            fx = track.add_fx(fx_name)
            fx_index = fx.index
            return {
                "success": True,
                "fx_index": fx_index,
                "name": fx.name,
                "n_params": fx.n_params,
                "track_index": track_index,
            }
        except ValueError:
            return {"success": False, "error": f"Plugin not found: '{fx_name}'"}
        except Exception as e:
            logger.error(f"add_fx failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def remove_fx(track_index: int, fx_index: int) -> dict:
        """Remove an FX plugin from a track by its index."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            fx_name = track.fxs[fx_index].name
            RPR.TrackFX_Delete(track.id, fx_index)
            return {"success": True, "track_index": track_index, "removed": fx_name}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_fx_parameter(
        track_index: int, fx_index: int, param_index: int, value: float
    ) -> dict:
        """
        Set a normalized parameter value (0.0–1.0) on an FX plugin.
        Use get_fx_parameters to discover available parameters and their indices.

        Returns the value actually read back from REAPER after the write, so a
        rejected or clamped write is visible rather than silently reported as OK.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            # reapy's FXParam subclasses float, so `fx.params[i].normalized_value = v`
            # just sets a throwaway Python attribute on a temporary object and never
            # reaches REAPER -- the same silent-no-op trap noted in load_fx_preset.
            # Go through the ReaScript API instead.
            ok = RPR.TrackFX_SetParamNormalized(track.id, fx_index, param_index, value)
            if not ok:
                return {
                    "success": False,
                    "error": "REAPER rejected the parameter write",
                    "track_index": track_index,
                    "fx_index": fx_index,
                    "param_index": param_index,
                }
            actual = RPR.TrackFX_GetParamNormalized(track.id, fx_index, param_index)
            return {
                "success": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "param_index": param_index,
                "param_name": _param_name(track, fx_index, param_index),
                "requested_value": value,
                "value": actual,
                "formatted_value": _param_formatted(track, fx_index, param_index),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def get_fx_parameters(
        track_index: int,
        fx_index: int,
        name_filter: str = "",
        limit: int = 0,
        offset: int = 0,
    ) -> dict:
        """
        Get parameters for an FX plugin: names, indices, current values, and the
        parameter's own min/max range.

        Large instruments expose thousands of parameters (Kontakt reports ~2600),
        so dumping them all is rarely useful:
        name_filter: case-insensitive substring match on the parameter name
                     (e.g. "program" to find an arpeggiator's program selector).
        limit: max parameters to return; 0 means no limit.
        offset: skip this many matches first, for paging.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            fx = track.fxs[fx_index]
            n_params = fx.n_params
            needle = name_filter.lower()

            params = []
            matched = 0
            for i in range(n_params):
                name = _param_name(track, fx_index, param_index=i)
                if needle and needle not in name.lower():
                    continue
                matched += 1
                if matched <= offset:
                    continue
                # TrackFX_GetParam returns (value, proj, fx, param, min, max) --
                # the raw value in the parameter's own units plus its range.
                raw_result = RPR.TrackFX_GetParam(track.id, fx_index, i, 0, 0)
                raw_value, param_min, param_max = (
                    raw_result[0],
                    raw_result[4],
                    raw_result[5],
                )
                params.append({
                    "index": i,
                    "name": name,
                    "normalized_value": RPR.TrackFX_GetParamNormalized(
                        track.id, fx_index, i
                    ),
                    "raw_value": raw_value,
                    "min": param_min,
                    "max": param_max,
                    "formatted_value": _param_formatted(track, fx_index, i),
                })
                if limit and len(params) >= limit:
                    break

            return {
                "success": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "fx_name": fx.name,
                "n_params": n_params,
                "n_matched": matched,
                "returned": len(params),
                "parameters": params,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def list_track_fx(track_index: int) -> dict:
        """List all FX plugins on a track."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            fx_list = []
            for i in range(track.n_fxs):
                fx = track.fxs[i]
                fx_list.append({
                    "index": i,
                    "name": fx.name,
                    "enabled": fx.is_enabled,
                    "n_params": fx.n_params,
                })
            return {"success": True, "track_index": track_index, "fx": fx_list}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def bypass_fx(track_index: int, fx_index: int, bypassed: bool) -> dict:
        """Enable or bypass (disable) an FX plugin on a track."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            fx = track.fxs[fx_index]
            fx.is_enabled = not bypassed
            return {
                "success": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "fx_name": fx.name,
                "bypassed": bypassed,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def load_fx_preset(track_index: int, fx_index: int, preset_name: str) -> dict:
        """Load a saved preset by name for an FX plugin."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            fx = track.fxs[fx_index]
            # FX exposes the preset as `preset`; `preset_name` does not exist and
            # assigning to it would silently create a Python attribute instead.
            fx.preset = preset_name
            return {
                "success": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "fx_name": fx.name,
                "preset": fx.preset,
            }
        except Exception as e:
            logger.error(f"load_fx_preset failed: {e}")
            return {"success": False, "error": str(e)}
