"""Time-signature helpers shared by the project tools.

Why this module exists
----------------------
``reapy.Project.time_signature`` is a *read-only* property and, despite its
name, returns ``(bpm, beats_per_measure)`` -- it never reports the denominator.
Code that treated it as ``(numerator, denominator)`` printed strings such as
``"98.0/4.0"`` and any attempt to assign to it raised ``AttributeError``.

The ReaScript API offers two things we use instead:

* ``TimeMap_GetTimeSigAtTime`` reads numerator, denominator and tempo at a
  given project time.
* ``SetTempoTimeSigMarker`` writes a tempo/time-signature marker. The project's
  opening time signature is the marker at time 0, so that is what we edit
  (see the REAPER User Guide, section 10.8 "Time Signature/Tempo Changes and
  Markers").
"""

from reapy import reascript_api as RPR

#: Denominators REAPER's time-signature dialog offers.
VALID_DENOMINATORS = (1, 2, 4, 8, 16, 32, 64)


def get_time_signature(project, time: float = 0.0) -> tuple:
    """Return ``(numerator, denominator)`` in effect at ``time`` (seconds)."""
    result = RPR.TimeMap_GetTimeSigAtTime(project.id, time, 0, 0, 0)
    # (proj, time, num, denom, tempo) -- the out-params follow the inputs.
    return int(result[2]), int(result[3])


def format_time_signature(project, time: float = 0.0) -> str:
    """Return the time signature at ``time`` as e.g. ``"7/8"``."""
    numerator, denominator = get_time_signature(project, time)
    return f"{numerator}/{denominator}"


def set_time_signature(project, numerator: int, denominator: int) -> None:
    """Set the project's opening time signature, keeping its tempo.

    Edits the tempo/time-signature marker at time 0, creating it if the project
    has none yet. Raises ``ValueError`` for an impossible signature and
    ``RuntimeError`` if REAPER rejects the write.
    """
    if numerator < 1:
        raise ValueError(f"numerator must be >= 1, got {numerator}")
    if denominator not in VALID_DENOMINATORS:
        raise ValueError(
            f"denominator must be one of {VALID_DENOMINATORS}, got {denominator}"
        )

    # Tempo currently in effect at time 0; the marker we write must keep it.
    at_zero = RPR.TimeMap_GetTimeSigAtTime(project.id, 0.0, 0, 0, 0)
    tempo = float(at_zero[4])
    point_index = -1  # -1 = append a new marker
    linear_tempo = False

    existing = RPR.FindTempoTimeSigMarker(project.id, 0.0)
    if existing >= 0:
        # (retval, proj, ptidx, timepos, measurepos, beatpos, bpm, num, denom, linear)
        marker = RPR.GetTempoTimeSigMarker(project.id, existing, 0, 0, 0, 0, 0, 0, 0)
        if abs(float(marker[3])) < 1e-9:  # a marker already sits at time 0: edit it
            point_index = existing
            tempo = float(marker[6])
            linear_tempo = bool(marker[9])

    ok = RPR.SetTempoTimeSigMarker(
        project.id, point_index, 0.0, -1, -1, tempo, numerator, denominator, linear_tempo
    )
    if not ok:
        raise RuntimeError("REAPER rejected the time-signature marker")
    RPR.UpdateTimeline()
