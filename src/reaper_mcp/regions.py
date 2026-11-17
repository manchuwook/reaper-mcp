"""Project marker and region helpers shared by the project and envelope tools.

Reading goes through ``CountProjectMarkers`` / ``EnumProjectMarkers2`` because
that pair is verified working against live REAPER. REAPER's API reference marks
both "discouraged" in favour of ``GetNumRegionsOrMarkers`` / ``GetRegionOrMarker``;
keeping every read in this one module means moving to the newer calls later is a
single-file change. Creating a region uses the non-discouraged
``AddRegionOrMarker``.

Why one shared reader: ``get_project_info`` used to walk ``project.regions`` and
swallow every exception, so it reported ``"regions": []`` for a project that
``list_project_regions`` correctly showed ten regions for.
"""

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import is_valid_pointer


def _enumerate(project) -> list:
    """Return every marker and region in project order as plain dicts.

    ``EnumProjectMarkers2``'s ``nameOut`` is a *string* out-parameter. It must be
    passed as ``""`` (a string buffer), not ``0`` -- an int makes ReaScript hand
    the 0 straight back instead of the name. The numeric out-params are fine
    as 0. The Python result tuple is
    ``(retval, proj, idx, isrgn, pos, rgnend, name, markrgnindexnumber)``.
    """
    entries = []
    with reapy.inside_reaper():
        # (retval, proj, num_markers, num_regions)
        count = RPR.CountProjectMarkers(project.id, 0, 0)
        for i in range(count[2] + count[3]):
            result = RPR.EnumProjectMarkers2(project.id, i, 0, 0, 0, "", 0)
            if not result[0]:
                continue
            entries.append({
                "is_region": bool(result[3]),
                "name": result[6],
                "start": result[4],
                "end": result[5],
                "number": result[7],
            })
    return entries


def list_regions(project) -> list:
    """All project regions as ``[{name, start, end}]``, sorted by start time."""
    regions = [
        {"name": e["name"], "start": e["start"], "end": e["end"]}
        for e in _enumerate(project)
        if e["is_region"]
    ]
    regions.sort(key=lambda r: r["start"])
    return regions


def list_markers(project) -> list:
    """All project markers (not regions) as ``[{number, name, position}]``."""
    return [
        {"number": e["number"], "name": e["name"], "position": e["start"]}
        for e in _enumerate(project)
        if not e["is_region"]
    ]


def add_region(project, name: str, start: float, end: float, color: int = 0) -> None:
    """Add a region spanning ``start``..``end`` seconds; raises if REAPER refuses.

    ``AddRegionOrMarker`` returns a ``ProjectMarker*`` pointer (a string such as
    ``"(ProjectMarker*)0x..."``); a null pointer means failure, and as with every
    ReaScript pointer a null one is still a truthy string, hence
    ``is_valid_pointer``. ``wantidx=-1`` lets REAPER pick the region number.
    ``color=0`` is the default colour.
    """
    pointer = RPR.AddRegionOrMarker(project.id, True, start, end, name, -1, color)
    if not is_valid_pointer(pointer):
        raise RuntimeError(f"REAPER refused to create region '{name}'")
