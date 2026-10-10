import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project, is_valid_pointer

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
    """Return all project regions as [{name, start, end}], in project order.

    NOTE: EnumProjectMarkers2's nameOut is a *string* out-parameter. It must be
    passed as "" (a string buffer), not 0 -- passing an int makes ReaScript hand
    the 0 straight back instead of the region name, so every region reads as 0.
    The numeric out-params (isrgn/pos/rgnend) are fine as 0.
    """
    # CountProjectMarkers returns (retval, proj, n_markers, n_regions) -- one
    # call gives both counts.
    count_result = RPR.CountProjectMarkers(project.id, 0, 0)
    n_markers, n_regions = count_result[2], count_result[3]
    total = n_markers + n_regions
    regions = []
    for i in range(total):
        result = RPR.EnumProjectMarkers2(project.id, i, 0, 0, 0, "", 0)
        retval, isrgn, pos, rgnend, name = result[0], result[3], result[4], result[5], result[6]
        if retval and isrgn:
            regions.append({"name": name, "start": pos, "end": rgnend})
    regions.sort(key=lambda r: r["start"])
    return regions


def _normalize_points(track, fx_index: int, param_index: int, points: list, normalized: bool):
    """Return (points in 0-1 units, warning-or-None) ready to write to an FX envelope.

    FX parameter envelopes store *normalized* 0-1 values. With ``normalized=False``
    the caller's values are converted using the parameter's own min/max from
    ``TrackFX_GetParam``. (Do not use ``fx.params[i].range`` for this: reapy's
    normalized parameter reports a constant (0, 1).) Plugins that only expose
    normalized parameters, such as VST3, also report (0, 1), so the conversion
    is a no-op there and a warning says so. Out-of-range values raise
    ``ValueError`` rather than being silently clamped by REAPER.
    """
    warning = None
    if not normalized:
        raw = RPR.TrackFX_GetParam(track.id, fx_index, param_index, 0, 0)
        lo, hi = raw[4], raw[5]
        span = hi - lo
        if (lo, hi) == (0.0, 1.0):
            warning = (
                "This parameter reports a 0-1 range, so normalized=False changed "
                "nothing. Plugins that only expose normalized values (e.g. VST3) "
                "need normalized values; check formatted_value in get_fx_parameters."
            )
        points = [
            {"position": p["position"], "value": (p["value"] - lo) / span if span else 0.0}
            for p in points
        ]
    eps = 1e-9  # tolerate float rounding from the min/max conversion
    bad = [p["value"] for p in points if not -eps <= p["value"] <= 1.0 + eps]
    if bad:
        raise ValueError(
            f"Values {bad} fall outside the parameter's range once normalized (must be 0.0-1.0)"
        )
    points = [{**p, "value": min(1.0, max(0.0, p["value"]))} for p in points]
    return points, warning


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
            if not is_valid_pointer(envelope):
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
            if not is_valid_pointer(envelope):
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
            # Validate before touching REAPER: creating the envelope is a side effect.
            points, warning = _normalize_points(track, fx_index, param_index, points, normalized)
            envelope = _get_fx_envelope(track, fx_index, param_index, create=True)
            if not is_valid_pointer(envelope):
                return {"success": False, "error": "Could not get or create envelope for this FX parameter"}
            applied = _insert_step_points(envelope, points, SHAPE_SQUARE, clear_existing)
            result = {
                "success": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "param_index": param_index,
                "points_written": applied,
            }
            if warning:
                result["warning"] = warning
            return result
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
            points, warning = _normalize_points(track, fx_index, param_index, points, normalized)
            envelope = _get_fx_envelope(track, fx_index, param_index, create=True)
            if not is_valid_pointer(envelope):
                return {"success": False, "error": "Could not get or create envelope for this FX parameter"}
            applied = _insert_step_points(envelope, points, SHAPE_SQUARE, clear_existing)
            result = {
                "success": True,
                "track_index": track_index,
                "fx_index": fx_index,
                "param_index": param_index,
                "points_written": applied,
                "matched_regions": matched,
                "unmatched_names": sorted(unmatched_names),
            }
            if warning:
                result["warning"] = warning
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}
