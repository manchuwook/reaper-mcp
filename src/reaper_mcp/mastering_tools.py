import os
import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project
from reaper_mcp.fx_tools import _param_formatted, _param_name
from reaper_mcp.units import db_to_gain, gain_to_db

logger = logging.getLogger("reaper_mcp.mastering_tools")

MASTERING_PRESETS = {
    "default": ["ReaEQ", "ReaComp", "ReaLimit"],
    "loud":    ["ReaEQ", "ReaComp", "ReaComp", "ReaLimit"],
    "gentle":  ["ReaEQ", "ReaComp", "ReaLimit"],
}


def register_tools(mcp):

    @mcp.tool()
    def add_master_fx(fx_name: str) -> dict:
        """Add an FX plugin to the master track."""
        try:
            project = get_project()
            master = project.master_track
            # add_fx returns an FX object and raises ValueError on an unknown name.
            fx = master.add_fx(fx_name)
            fx_index = fx.index
            return {"success": True, "fx_index": fx_index, "name": fx.name, "n_params": fx.n_params}
        except ValueError:
            return {"success": False, "error": f"Plugin not found: '{fx_name}'"}
        except Exception as e:
            logger.error(f"add_master_fx failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def list_master_fx() -> dict:
        """List all FX plugins on the master track."""
        try:
            project = get_project()
            master = project.master_track
            fx_list = []
            for i in range(master.n_fxs):
                fx = master.fxs[i]
                fx_list.append({"index": i, "name": fx.name, "enabled": fx.is_enabled, "n_params": fx.n_params})
            return {"success": True, "fx": fx_list}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_master_fx_parameter(fx_index: int, param_index: int, value: float) -> dict:
        """Set a normalized parameter (0.0–1.0) on a master track FX plugin."""
        try:
            project = get_project()
            master = project.master_track
            # reapy's FXParam subclasses float, so `.normalized_value = v` only sets
            # a throwaway Python attribute and never reaches REAPER; use ReaScript.
            ok = RPR.TrackFX_SetParamNormalized(master.id, fx_index, param_index, value)
            if not ok:
                return {"success": False, "error": "REAPER rejected the parameter write"}
            return {
                "success": True,
                "fx_index": fx_index,
                "param_index": param_index,
                "param_name": _param_name(master, fx_index, param_index),
                "requested_value": value,
                "value": RPR.TrackFX_GetParamNormalized(master.id, fx_index, param_index),
                "formatted_value": _param_formatted(master, fx_index, param_index),
            }
        except Exception as e:
            logger.error(f"set_master_fx_parameter failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_master_volume(volume_db: float) -> dict:
        """Set the master track output volume in dB."""
        try:
            project = get_project()
            master = project.master_track
            master.set_info_value("D_VOL", db_to_gain(volume_db))
            return {"success": True, "volume_db": round(gain_to_db(master.get_info_value("D_VOL")), 2)}
        except Exception as e:
            logger.error(f"set_master_volume failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def apply_mastering_chain(preset: str = "default") -> dict:
        """
        Add a standard mastering FX chain to the master track.
        Presets: default (EQ > Comp > Limiter), loud (EQ > Comp x2 > Limiter),
        gentle (EQ > light Comp > Limiter).
        After applying, use set_master_fx_parameter to dial in specific settings.
        Use list_master_fx + get_fx_parameters to discover parameter indices.
        """
        try:
            if preset not in MASTERING_PRESETS:
                return {
                    "success": False,
                    "error": f"Unknown preset '{preset}'. Available: {list(MASTERING_PRESETS.keys())}",
                }
            project = get_project()
            master = project.master_track
            added = []
            for fx_name in MASTERING_PRESETS[preset]:
                try:
                    fx = master.add_fx(fx_name)
                except ValueError:
                    logger.warning(f"Skipping unavailable plugin '{fx_name}'")
                    continue
                added.append({"fx_index": fx.index, "name": fx.name})
            return {"success": True, "preset": preset, "fx_chain": added}
        except Exception as e:
            logger.error(f"apply_mastering_chain failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def apply_limiter(threshold_db: float = -0.5, release_ms: float = 50.0) -> dict:
        """
        Add ReaLimit to the master track and set its threshold (dB) and release (ms).
        The parameters are matched by name and written in the plugin's own units;
        the returned `applied` list shows what ReaLimit reports back, and any that
        could not be matched appear under `unmatched` (set those with
        set_master_fx_parameter after looking them up with get_fx_parameters).
        """
        try:
            project = get_project()
            master = project.master_track
            fx = master.add_fx("ReaLimit")
            fx_index = fx.index
            wanted = {"threshold": threshold_db, "release": release_ms}
            applied, unmatched = [], dict(wanted)
            for i in range(fx.n_params):
                name = _param_name(master, fx_index, i)
                for key, value in wanted.items():
                    if key in name.lower() and key in unmatched:
                        RPR.TrackFX_SetParam(master.id, fx_index, i, value)
                        applied.append({
                            "param_index": i,
                            "param_name": name,
                            "requested": value,
                            "formatted_value": _param_formatted(master, fx_index, i),
                        })
                        unmatched.pop(key)
            return {
                "success": True,
                "fx_index": fx_index,
                "name": fx.name,
                "applied": applied,
                "unmatched": sorted(unmatched),
            }
        except ValueError:
            return {"success": False, "error": "ReaLimit not found — check REAPER installation"}
        except Exception as e:
            logger.error(f"apply_limiter failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def analyze_loudness() -> dict:
        """
        Render the project to a temp file and measure integrated loudness (LUFS)
        and true peak (dBTP) using the ITU-R BS.1770 standard.
        """
        try:
            import soundfile as sf
            import pyloudnorm as pyln
            import numpy as np
            from reaper_mcp.render_tools import render_to_temp_file

            tmp = render_to_temp_file()
            try:
                data, rate = sf.read(tmp)
                meter = pyln.Meter(rate)
                integrated = meter.integrated_loudness(data)
                peak_linear = float(np.max(np.abs(data)))
                peak_db = float(20 * np.log10(peak_linear)) if peak_linear > 0 else -120.0
                return {
                    "success": True,
                    "integrated_lufs": round(integrated, 1),
                    "true_peak_dbtp": round(peak_db, 1),
                    "sample_rate": rate,
                }
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except Exception as e:
            logger.error(f"analyze_loudness failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def normalize_project(target_lufs: float = -14.0) -> dict:
        """
        Measure the project's integrated loudness, then adjust the master volume
        so the output hits the target LUFS level.
        Common targets: -14 LUFS (streaming), -16 LUFS (podcasts), -23 LUFS (broadcast).
        """
        try:
            import soundfile as sf
            import pyloudnorm as pyln
            from reaper_mcp.render_tools import render_to_temp_file

            tmp = render_to_temp_file()
            try:
                data, rate = sf.read(tmp)
                meter = pyln.Meter(rate)
                current_lufs = meter.integrated_loudness(data)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

            if current_lufs == float("-inf"):
                return {"success": False, "error": "Project appears to be silent"}

            gain_db = target_lufs - current_lufs
            project = get_project()
            master = project.master_track
            current_vol_db = gain_to_db(master.get_info_value("D_VOL"))
            new_vol_db = current_vol_db + gain_db
            master.set_info_value("D_VOL", db_to_gain(new_vol_db))

            return {
                "success": True,
                "original_lufs": round(current_lufs, 1),
                "target_lufs": target_lufs,
                "gain_applied_db": round(gain_db, 1),
                "new_master_volume_db": round(new_vol_db, 1),
            }
        except Exception as e:
            logger.error(f"normalize_project failed: {e}")
            return {"success": False, "error": str(e)}
