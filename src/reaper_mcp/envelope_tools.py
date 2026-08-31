import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project

logger = logging.getLogger("reaper_mcp.envelope_tools")

# REAPER envelope point shapes (used by InsertEnvelopePoint / InsertEnvelopePoint2)
SHAPE_LINEAR = 0
SHAPE_SQUARE = 1
SHAPE_SLOW_START_END = 2
SHAPE_FAST_START = 3
SHAPE_FAST_END = 4
SHAPE_BEZIER = 5


def _get_fx_envelope(track, fx_index: int, param_index: int, create: bool):
    return RPR.GetFXEnvelope(track.id, fx_index, param_index, create)


def _list_regions(project):
    """Return all project regions as [{name, start, end}], in project order."""
    n_markers = RPR.CountProjectMarkers(project.id, 0, 0)[2]
    n_regions = RPR.CountProjectMarkers(project.id, 0, 0)[3]
    total = n_markers + n_regions
    regions = []
    for i in range(total):
        result = RPR.EnumProjectMarkers2(project.id, i, 0, 0, 0, 0, 0)
        retval, isrgn, pos, rgnend, name = result[0], result[3], result[4], result[5], result[6]
        if retval and isrgn:
            regions.append({"name": name, "start": pos, "end": rgnend})
    regions.sort(key=lambda r: r["start"])
    return regions


def _insert_step_points(envelope, points: list, shape: int, clear_existing: bool):
    """points: list of {"position": float, "value": float}, sorted by position."""
    sorted_points = sorted(points, key=lambda p: p["position"])
    if clear_existing and sorted_points:
        RPR.DeleteEnvelopePointRange(envelope, -1.0, sorted_points[-1]["position"] + 1.0)
    for p in sorted_points:
        RPR.InsertEnvelopePoint(
            envelope, p["position"], p["value"], shape, 0, False, True
        )
    RPR.Envelope_SortPoints(envelope)
    return sorted_points


def register_tools(mcp):

    @mcp.tool()
    def list_project_regions() -> dict:
        """
        List all regions defined in the REAPER project (name, start/end in seconds).
        Useful for discovering the region names to target with
        set_fx_param_step_automation_by_region (e.g. regions imported from song parts).
        """
        try:
            project = get_project()
            return {"success": True, "regions": _list_regions(project)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def get_fx_param_envelope_info(track_index: int, fx_index: int, param_index: int) -> dict:
        """
        Check whether an automation envelope exists for a given FX parameter,
        and report how many points it has.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            envelope = _get_fx_envelope(track, fx_index, param_index, create=False)
            if not envelope:
                return {
                    "success": True,
                    "exists": False,
                    "track_index": track_index,
                    "fx_index": fx_index,
                    "param_index": param_index,
                }
            n_points = RPR.CountEnvelopePoints(envelope)
            return {
                "success": True,
                "exists": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "param_index": param_index,
                "n_points": n_points,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def clear_fx_param_envelope(track_index: int, fx_index: int, param_index: int) -> dict:
        """Remove all automation points from an FX parameter's envelope, if it exists."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            envelope = _get_fx_envelope(track, fx_index, param_index, create=False)
            if not envelope:
                return {"success": True, "cleared": False, "reason": "no envelope exists"}
            RPR.DeleteEnvelopePointRange(envelope, -1e9, 1e9)
            return {"success": True, "cleared": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_fx_param_step_automation(
        track_index: int,
        fx_index: int,
        param_index: int,
        points: list,
        normalized: bool = True,
        clear_existing: bool = True,
    ) -> dict:
        """
        Write step (square-shaped) automation onto an FX parameter: the value jumps
        at each point's position and holds constant until the next point. Good for
        things like switching an arpeggiator/synth "program" parameter per song section.

        points: list of {"position": seconds, "value": float}. If normalized is True
        (default), value must be 0.0-1.0 — this is how REAPER stores FX param envelope
        values regardless of the parameter's real-world range. Use get_fx_parameters
        to see a param's current normalized_value for reference.
        clear_existing: if True, existing points from the envelope's start up to the
        last given position are removed first, so re-running this call replaces
        the automation cleanly instead of layering points.
        """
        try:
            if not points:
                return {"success": False, "error": "points must be a non-empty list"}
            project = get_project()
            track = project.tracks[track_index]
            envelope = _get_fx_envelope(track, fx_index, param_index, create=True)
            if not envelope:
                return {"success": False, "error": "Could not get or create envelope for this FX parameter"}
            if not normalized:
                fx = track.fxs[fx_index]
                lo, hi = fx.params[param_index].range
                span = hi - lo
                points = [
                    {"position": p["position"], "value": (p["value"] - lo) / span if span else 0.0}
                    for p in points
                ]
            applied = _insert_step_points(envelope, points, SHAPE_SQUARE, clear_existing)
            return {
                "success": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "param_index": param_index,
                "points_written": applied,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_fx_param_step_automation_by_region(
        track_index: int,
        fx_index: int,
        param_index: int,
        values_by_region: dict,
        normalized: bool = True,
        clear_existing: bool = True,
    ) -> dict:
        """
        Write step (square-shaped) automation onto an FX parameter, one value per
        project region, using each region's start time as the automation point.
        Ideal for setting a per-song-part value (e.g. an arpeggiator/synth "program"
        parameter) once song part regions are in the REAPER project (see
        list_project_regions to check names, or import them from song structure first).

        values_by_region: dict mapping region name -> value. If normalized is True
        (default), values must be 0.0-1.0. Regions with no matching key are left
        untouched; keys with no matching region are reported back as unmatched.
        """
        try:
            if not values_by_region:
                return {"success": False, "error": "values_by_region must be a non-empty dict"}
            project = get_project()
            track = project.tracks[track_index]
            regions = _list_regions(project)
            matched = []
            unmatched_names = set(values_by_region.keys())
            points = []
            for r in regions:
                if r["name"] in values_by_region:
                    points.append({"position": r["start"], "value": values_by_region[r["name"]]})
                    matched.append({"region": r["name"], "position": r["start"]})
                    unmatched_names.discard(r["name"])
            if not points:
                return {
                    "success": False,
                    "error": "No project regions matched the given names",
                    "available_regions": [r["name"] for r in regions],
                }
            envelope = _get_fx_envelope(track, fx_index, param_index, create=True)
            if not envelope:
                return {"success": False, "error": "Could not get or create envelope for this FX parameter"}
            if not normalized:
                fx = track.fxs[fx_index]
                lo, hi = fx.params[param_index].range
                span = hi - lo
                points = [
                    {"position": p["position"], "value": (p["value"] - lo) / span if span else 0.0}
                    for p in points
                ]
            applied = _insert_step_points(envelope, points, SHAPE_SQUARE, clear_existing)
            return {
                "success": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "param_index": param_index,
                "points_written": applied,
                "matched_regions": matched,
                "unmatched_names": sorted(unmatched_names),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
