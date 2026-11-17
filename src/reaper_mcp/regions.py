"""Project marker and region helpers shared by the project and envelope tools.

Why not ``EnumProjectMarkers*``
-------------------------------
``EnumProjectMarkers`` / ``EnumProjectMarkers2`` / ``EnumProjectMarkers3``
return positions and numbers correctly from Python, but their ``nameOut``
parameter is a ``const char**`` -- REAPER hands back a *pointer* to its own
string, which the Python ReaScript bridge cannot unpack. Checked against live
REAPER 7.86: every name comes back ``""`` (pass a 512-char buffer and you get the
same 512 spaces back). A project whose regions read "Intro", "Verse 1", ... in
the ruler looked unnamed to every tool, so finding a section by name never
matched and ``create_regions_from_items``' duplicate check never fired.

The per-marker API REAPER now recommends works:

* ``GetNumRegionsOrMarkers(proj)`` -- regions and markers together.
* ``GetRegionOrMarker(proj, idx, "")`` -- a ``ProjectMarker*`` (a null-pointer
  string past the end; see ``is_valid_pointer``).
* ``GetRegionOrMarkerInfo_Value(proj, m, key)`` -- ``B_ISREGION``,
  ``D_STARTPOS``, ``D_ENDPOS``, ``I_NUMBER``.
* ``GetSetRegionOrMarkerInfo_String(proj, m, "P_NAME", "", False)`` -- returns
  ``(retval, proj, marker, key, value, set)``; the name is ``[4]``.

Keep every read in this module. The tests fake exactly this API (see the
``project_markers`` fixture), so a mock cannot report a name REAPER would not.
"""

import logging

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import is_valid_pointer

logger = logging.getLogger("reaper_mcp.regions")


def _read_entry(proj, marker) -> dict:
    def value(key):
        return RPR.GetRegionOrMarkerInfo_Value(proj, marker, key)

    name = RPR.GetSetRegionOrMarkerInfo_String(proj, marker, "P_NAME", "", False)
    return {
        "is_region": bool(value("B_ISREGION")),
        "name": name[4] if name[0] else "",
        "start": float(value("D_STARTPOS")),
        "end": float(value("D_ENDPOS")),
        "number": int(value("I_NUMBER")),
    }


def _enumerate_legacy(proj) -> list:
    """Fallback for a REAPER without the per-marker API: names read back blank."""
    logger.warning(
        "REAPER lacks GetNumRegionsOrMarkers; region and marker names will be empty. "
        "Update REAPER to read names."
    )
    entries = []
    count = RPR.CountProjectMarkers(proj, 0, 0)  # (retval, proj, n_markers, n_regions)
    for i in range(count[2] + count[3]):
        # (retval, proj, idx, isrgn, pos, rgnend, name, markrgnindexnumber)
        result = RPR.EnumProjectMarkers2(proj, i, 0, 0, 0, "", 0)
        if result[0]:
            entries.append({
                "is_region": bool(result[3]),
                "name": "",
                "start": result[4],
                "end": result[5],
                "number": result[7],
            })
    return entries


def _enumerate(project) -> list:
    """Every marker and region in project order as ``{is_region, name, start, end, number}``."""
    proj = project.id
    with reapy.inside_reaper():
        try:
            count = int(RPR.GetNumRegionsOrMarkers(proj))
        except AttributeError:
            return _enumerate_legacy(proj)
        entries = []
        for i in range(count):
            marker = RPR.GetRegionOrMarker(proj, i, "")
            if is_valid_pointer(marker):
                entries.append(_read_entry(proj, marker))
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
