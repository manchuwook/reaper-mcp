import os
import time
import logging
from pathlib import Path

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project
from reaper_mcp.timing import format_time_signature, set_time_signature as _set_time_signature

logger = logging.getLogger("reaper_mcp.project_tools")

#: ``Main_SaveProjectEx`` option: "set as the new project filename".
_SAVE_AS_PROJECT_FILE = 8


def _current_project_file(project) -> str:
    """Full path of the project's .rpp, or "" if it has never been saved.

    (``reapy.Project.path`` is the project's *media* directory, not the file.)
    """
    try:
        return RPR.EnumProjects(-1, "", 2048)[2] or ""
    except Exception:
        return ""


def register_tools(mcp):

    @mcp.tool()
    def create_project(tempo: float = 120.0, time_signature: str = "4/4", name: str = "") -> dict:
        """
        Open a new project tab with the given tempo and time signature.

        The project is unsaved until save_project is called; `name` is only a
        label echoed back in the result (REAPER names a project when it is saved).
        """
        try:
            # Command 41929: new project tab, ignoring the default project template.
            # (Confirm against Actions > Show action list on your REAPER build.)
            RPR.Main_OnCommand(41929, 0)
            project = get_project()
            project.bpm = tempo
            if time_signature:
                num, denom = map(int, time_signature.split("/"))
                _set_time_signature(project, num, denom)
            return {
                "success": True,
                "name": name or f"New Project {time.strftime('%Y-%m-%d %H-%M-%S')}",
                "tempo": project.bpm,
                "time_signature": format_time_signature(project),
            }
        except Exception as e:
            logger.error(f"create_project failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def save_project(project_path: str = "") -> dict:
        """
        Save the current project.

        - With `project_path`: save there ("Save as") and make it the project's file.
        - Without it, a project that already has a file is saved in place.
        - Without it, a never-saved project goes to ~/Documents/REAPER Projects.
        """
        try:
            project = get_project()
            if not project_path:
                existing = _current_project_file(project)
                if existing:
                    RPR.Main_SaveProject(project.id, False)
                    return {"success": True, "project_path": existing}
                proj_name = project.name or f"Project {time.strftime('%Y-%m-%d %H-%M-%S')}"
                default_dir = Path.home() / "Documents" / "REAPER Projects"
                project_path = str(default_dir / f"{proj_name}.rpp")
            if not project_path.lower().endswith(".rpp"):
                project_path += ".rpp"
            project_path = os.path.abspath(project_path)
            os.makedirs(os.path.dirname(project_path), exist_ok=True)
            RPR.Main_SaveProjectEx(project.id, project_path, _SAVE_AS_PROJECT_FILE)
            return {"success": True, "project_path": project_path}
        except Exception as e:
            logger.error(f"save_project failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def load_project(project_path: str, discard_unsaved: bool = False) -> dict:
        """
        Load a REAPER project (.rpp) from the given file path.

        If the current project has unsaved changes the call is refused, because
        REAPER would otherwise block on a "Save changes?" dialog that nobody can
        answer remotely. Call save_project first, or pass discard_unsaved=True to
        throw the changes away.
        """
        try:
            if not os.path.exists(project_path):
                return {"success": False, "error": f"File not found: {project_path}"}
            if RPR.IsProjectDirty(0) and not discard_unsaved:
                return {
                    "success": False,
                    "unsaved_changes": True,
                    "error": (
                        "The current project has unsaved changes. Call save_project "
                        "first, or pass discard_unsaved=True to discard them."
                    ),
                }
            # "noprompt:" stops REAPER asking to save (which would hang this call).
            RPR.Main_openProject(f"noprompt:{project_path}")
            project = get_project()
            return {
                "success": True,
                "name": project.name,
                "tempo": project.bpm,
                "time_signature": format_time_signature(project),
                "project_path": project_path,
            }
        except Exception as e:
            logger.error(f"load_project failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def get_project_info() -> dict:
        """Get information about the current project: name, path, tempo, tracks, length."""
        try:
            project = get_project()
            markers = []
            try:
                for i in range(project.n_markers):
                    m = project.markers[i]
                    markers.append({"index": i, "name": m.name, "position": m.position})
            except Exception:
                pass

            regions = []
            try:
                for i in range(project.n_regions):
                    r = project.regions[i]
                    regions.append({"index": i, "name": r.name, "start": r.start, "end": r.end})
            except Exception:
                pass

            return {
                "success": True,
                "name": project.name,
                "path": project.path,
                "tempo": project.bpm,
                "time_signature": format_time_signature(project),
                "length": project.length,
                "track_count": project.n_tracks,
                "markers": markers,
                "regions": regions,
            }
        except Exception as e:
            logger.error(f"get_project_info failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_tempo(bpm: float) -> dict:
        """Set the project tempo in BPM."""
        try:
            project = get_project()
            project.bpm = bpm
            return {"success": True, "tempo": project.bpm}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_time_signature(numerator: int, denominator: int) -> dict:
        """
        Set the project's opening time signature, e.g. 4/4, 3/4, 6/8, keeping its tempo.

        Edits the tempo/time-signature marker at time 0 (creating it if needed).
        Denominator must be 1, 2, 4, 8, 16, 32 or 64.
        """
        try:
            project = get_project()
            _set_time_signature(project, numerator, denominator)
            return {"success": True, "time_signature": format_time_signature(project)}
        except Exception as e:
            return {"success": False, "error": str(e)}
