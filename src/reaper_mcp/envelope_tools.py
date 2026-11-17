import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project, is_valid_pointer
from reaper_mcp.regions import list_regions as _list_regions

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


def _format_env_value(envelope, value):
    """The value exactly as REAPER's UI shows it for this envelope (e.g. "2"), or None."""
    try:
        r = RPR.Envelope_FormatValue(envelope, value, "", 64)
        text = r[2]
        return text if isinstance(text, str) else None
    except Exception:
        return None


def _normalize_points(track, fx_index: int, param_index: int, points: list, normalized: bool):
    """Return (points in the envelope's native units, warning-or-None), ready to write.

    FX parameter envelopes store the parameter's *native* value (VST/VST3: 0-1;
    JSFX: the slider's own min..max), so the 0-1 fractions callers give are mapped
    back onto that range before writing. With ``normalized=False``
    the caller's values are converted using the parameter's own min/max from
    ``TrackFX_GetParam``. (Do not use ``fx.params[i].range`` for this: reapy's
    normalized parameter reports a constant (0, 1).) Plugins that only expose
    normalized parameters, such as VST3, also report (0, 1), so the conversion
    is a no-op there and a warning says so. Out-of-range values raise
    ``ValueError`` rather than being silently clamped by REAPER.
    """
    warning = None
    raw = RPR.TrackFX_GetParam(track.id, fx_index, param_index, 0, 0)
    lo, hi = raw[4], raw[5]
    span = hi - lo
    if not normalized:
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
    # Envelope points hold the native value, not the 0-1 fraction: a JSFX slider with
    # range 0-15 stores 1.0 for its second step. Map back onto lo..hi (a no-op for 0-1).
    points = [{**p, "value": lo + p["value"] * span} for p in points]
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
    def add_project_markers(markers: list) -> dict:
        """
        Add project markers (not regions) at the given times.
        markers: list of {"position": seconds, "name": str, "color": [r, g, b]} where
        color is optional (0-255 each). Returns the index REAPER assigned to each marker.
        Existing markers are left alone, so calling this twice adds duplicates.
        """
        try:
            if not markers:
                return {"success": False, "error": "markers must be a non-empty list"}
            created = []
            for m in markers:
                color = 0
                rgb = m.get("color")
                if rgb:
                    try:
                        color = RPR.ColorToNative(int(rgb[0]), int(rgb[1]), int(rgb[2])) | 0x1000000
                    except Exception:
                        color = 0
                position = float(m["position"])
                name = str(m.get("name", ""))
                index = RPR.AddProjectMarker2(0, False, position, 0, name, -1, color)
                created.append({"position": position, "name": name, "index": index})
            RPR.UpdateTimeline()
            return {"success": True, "markers": created}
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
    def get_fx_param_envelope_points(
        track_index: int, fx_index: int, param_index: int, limit: int = 0
    ) -> dict:
        """
        List the points of an FX parameter envelope: position (seconds), the stored
        value, shape (1 = square), and `display`, the value as REAPER's UI shows it.
        limit: max points to return (0 = all). Use this to review what
        set_fx_param_step_automation wrote.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            envelope = _get_fx_envelope(track, fx_index, param_index, create=False)
            if not is_valid_pointer(envelope):
                return {"success": True, "exists": False}
            n_points = RPR.CountEnvelopePoints(envelope)
            count = n_points if limit <= 0 else min(n_points, limit)
            points = []
            for i in range(count):
                # returns (retval, envelope, ptidx, time, value, shape, tension, selected)
                r = RPR.GetEnvelopePoint(envelope, i, 0, 0, 0, 0, 0)
                points.append(
                    {
                        "index": i,
                        "position": r[3],
                        "value": r[4],
                        "shape": r[5],
                        "display": _format_env_value(envelope, r[4]),
                    }
                )
            return {
                "success": True,
                "exists": True,
                "n_points": n_points,
                "points": points,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def evaluate_fx_param_envelope(
        track_index: int, fx_index: int, param_index: int, times: list
    ) -> dict:
        """
        The effective envelope value at each time in `times` (seconds), with `display`
        as REAPER shows it. This is what playback would use at that moment, so it
        matches the hover tooltip in the lane.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            envelope = _get_fx_envelope(track, fx_index, param_index, create=False)
            if not is_valid_pointer(envelope):
                return {"success": True, "exists": False}
            out = []
            for t in times:
                # returns (retval, envelope, time, samplerate, samplesRequested,
                #          value, dVdS, ddVdS, dddVdS)
                r = RPR.Envelope_Evaluate(envelope, float(t), 0, 0, 0, 0, 0, 0)
                out.append(
                    {
                        "time": float(t),
                        "value": r[5],
                        "display": _format_env_value(envelope, r[5]),
                    }
                )
            return {"success": True, "exists": True, "values": out}
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
        (default), value is a 0.0-1.0 fraction of the parameter's range; it is converted
        to the native value REAPER stores (VST/VST3 params are already 0-1, JSFX sliders
        use their own min..max). With normalized=False give real-world values instead,
        e.g. 2 for the third step of a slider that runs 0-15. Review the result with
        get_fx_param_envelope_points.
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
