"""MCP tools that render the project (or stems) to audio files.

How REAPER's render settings work (ReaScript ``GetSetProjectInfo*``)
---------------------------------------------------------------------
* ``RENDER_FILE`` is the output **directory**; ``RENDER_PATTERN`` is the file
  name **without extension** (wildcards such as ``$track`` allowed). REAPER adds
  the extension for the chosen format.
* ``RENDER_FORMAT`` / ``RENDER_FORMAT2`` are *string* keys (``GetSetProjectInfo_String``)
  holding a base64 sink configuration, or a bare four-character sink code
  (``"evaw"`` = WAV, ``"l3pm"`` = MP3) to use that sink's default settings.
  They are not numeric keys, so a number written with ``GetSetProjectInfo`` is
  silently ignored.
* ``RENDER_BOUNDSFLAG``: 0 = custom range (``RENDER_STARTPOS``/``RENDER_ENDPOS``),
  1 = entire project, 2 = time selection, 3 = all regions, ...
* ``RENDER_SETTINGS``: bits select the *source*. 0 = master mix, 2 = stems only
  (one file per **selected** track). See the REAPER User Guide, section 21.5
  "Render to File: Source Options".

Render settings live in the project file, so every render here snapshots the
settings it touches and restores them afterwards: a tool call must not leave the
user's own render dialog configured differently. Normalisation, fades, trimming
and "add rendered files to project" are switched off for the duration, because
an analysis render must measure the mix as it is, and must not litter the
project with temporary tracks.

The sink codes for FLAC and Ogg Vorbis and the WAV bit-depth config bytes follow
REAPER's project-file encoding; only ``evaw`` and ``l3pm`` appear in the API
documentation. If a format misbehaves, render once by hand and compare the
``RENDER_FORMAT`` string REAPER stores.
"""

import base64
import logging
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project

logger = logging.getLogger("reaper_mcp.render_tools")

# --- RENDER_BOUNDSFLAG values ------------------------------------------------
BOUNDS_CUSTOM = 0
BOUNDS_ENTIRE_PROJECT = 1
BOUNDS_TIME_SELECTION = 2

# --- RENDER_SETTINGS values (source) ----------------------------------------
SOURCE_MASTER_MIX = 0
SOURCE_STEMS_ONLY = 2  # one file per selected track

#: Four-character sink codes for ``RENDER_FORMAT``.
SINK_CODES = {
    "wav": "evaw",
    "flac": "calf",
    "mp3": "l3pm",
    "ogg": "vggo",
}
EXTENSIONS = {"wav": ".wav", "flac": ".flac", "mp3": ".mp3", "ogg": ".ogg"}
WAV_BIT_DEPTHS = (16, 24, 32)

#: Project render keys we change, so we can restore them afterwards.
_NUMERIC_KEYS = (
    "RENDER_SETTINGS",
    "RENDER_BOUNDSFLAG",
    "RENDER_CHANNELS",
    "RENDER_SRATE",
    "RENDER_STARTPOS",
    "RENDER_ENDPOS",
    "RENDER_NORMALIZE",  # also holds the fade/trim/pad enable bits
    "RENDER_ADDTOPROJ",
)
_STRING_KEYS = ("RENDER_FILE", "RENDER_PATTERN", "RENDER_FORMAT", "RENDER_FORMAT2")


def _sink_config(fmt: str, bit_depth: int) -> str:
    """Value for ``RENDER_FORMAT``: a bare sink code, or a WAV config with bit depth."""
    key = fmt.lower()
    if key not in SINK_CODES:
        raise ValueError(f"Unsupported format '{fmt}'. Choose from {sorted(SINK_CODES)}")
    if key == "wav":
        if bit_depth not in WAV_BIT_DEPTHS:
            raise ValueError(f"WAV bit_depth must be one of {WAV_BIT_DEPTHS}, got {bit_depth}")
        return base64.b64encode(b"evaw" + bytes([bit_depth, 0, 1])).decode("ascii")
    return SINK_CODES[key]


@contextmanager
def preserved_render_settings():
    """Restore the project's render settings when the block exits (even on error)."""
    saved_numeric = {k: RPR.GetSetProjectInfo(0, k, 0.0, False) for k in _NUMERIC_KEYS}
    saved_strings = {k: RPR.GetSetProjectInfo_String(0, k, "", False)[3] for k in _STRING_KEYS}
    try:
        yield
    finally:
        for key, value in saved_strings.items():
            RPR.GetSetProjectInfo_String(0, key, value, True)
        for key, value in saved_numeric.items():
            RPR.GetSetProjectInfo(0, key, value, True)


def _configure(
    directory: str,
    pattern: str,
    fmt: str,
    sample_rate: int,
    bit_depth: int,
    channels: int,
    bounds: int,
    source: int = SOURCE_MASTER_MIX,
    start: float = 0.0,
    end: float = 0.0,
) -> str:
    """Write the render settings for one render. Returns the RENDER_FORMAT value used."""
    sink = _sink_config(fmt, bit_depth)
    RPR.GetSetProjectInfo_String(0, "RENDER_FILE", directory, True)
    RPR.GetSetProjectInfo_String(0, "RENDER_PATTERN", pattern, True)
    RPR.GetSetProjectInfo_String(0, "RENDER_FORMAT", sink, True)
    RPR.GetSetProjectInfo_String(0, "RENDER_FORMAT2", "", True)  # no secondary file
    RPR.GetSetProjectInfo(0, "RENDER_SETTINGS", float(source), True)
    RPR.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", float(bounds), True)
    RPR.GetSetProjectInfo(0, "RENDER_STARTPOS", float(start), True)
    RPR.GetSetProjectInfo(0, "RENDER_ENDPOS", float(end), True)
    RPR.GetSetProjectInfo(0, "RENDER_SRATE", float(sample_rate), True)
    RPR.GetSetProjectInfo(0, "RENDER_CHANNELS", float(channels), True)
    RPR.GetSetProjectInfo(0, "RENDER_NORMALIZE", 0.0, True)
    RPR.GetSetProjectInfo(0, "RENDER_ADDTOPROJ", 0.0, True)
    return sink


def _run_render() -> None:
    """File: Render project, using the most recent render settings (no dialog)."""
    RPR.Main_OnCommand(41824, 0)


def _split_target(output_path: str, fmt: str):
    """Split a user path into (directory, pattern, expected_final_path)."""
    if fmt.lower() not in EXTENSIONS:
        raise ValueError(f"Unsupported format '{fmt}'. Choose from {sorted(EXTENSIONS)}")
    path = Path(output_path).expanduser().resolve()
    pattern = path.stem
    directory = str(path.parent)
    expected = str(path.parent / (pattern + EXTENSIONS[fmt.lower()]))
    return directory, pattern, expected


def _prepare_target(expected: str, overwrite: bool):
    """Return an error dict if `expected` exists and may not be replaced, else None."""
    if os.path.exists(expected):
        if not overwrite:
            return {
                "success": False,
                "error": f"{expected} already exists. Pass overwrite=True to replace it.",
            }
        os.remove(expected)  # REAPER would otherwise block on an overwrite prompt
    return None


def render_to_temp_file(sample_rate: int = 48000) -> str:
    """
    Render the whole project to a temporary 24-bit stereo WAV and return its path.

    Used by the analysis and mastering tools. The project's own render settings
    are left exactly as they were. The caller must delete the returned file.
    """
    directory = tempfile.gettempdir()
    pattern = f"reaper_mcp_{uuid.uuid4().hex}"
    expected = os.path.join(directory, pattern + ".wav")
    with preserved_render_settings():
        _configure(directory, pattern, "wav", sample_rate, 24, 2, BOUNDS_ENTIRE_PROJECT)
        _run_render()
    if not os.path.exists(expected):
        raise RuntimeError("REAPER finished rendering but the temporary file was not created")
    return expected


def register_tools(mcp):

    @mcp.tool()
    def render_project(
        output_path: str,
        format: str = "wav",
        sample_rate: int = 48000,
        bit_depth: int = 24,
        channels: int = 2,
        overwrite: bool = False,
    ) -> dict:
        """
        Render the entire project (master mix) to a file.
        output_path: target file; REAPER supplies the extension for `format`.
        format: wav, flac, mp3 (requires LAME), ogg.
        sample_rate: e.g. 44100, 48000, 96000.
        bit_depth: 16, 24 or 32 (WAV only; ignored for other formats).
        channels: 1 (mono) or 2 (stereo).
        overwrite: replace an existing file (otherwise the call is refused).
        """
        try:
            directory, pattern, expected = _split_target(output_path, format)
            os.makedirs(directory, exist_ok=True)
            refused = _prepare_target(expected, overwrite)
            if refused:
                return refused
            with preserved_render_settings():
                sink = _configure(
                    directory, pattern, format, sample_rate, bit_depth, channels,
                    BOUNDS_ENTIRE_PROJECT,
                )
                _run_render()
            if not os.path.exists(expected):
                return {"success": False, "error": "Render command completed but output file not found"}
            return {
                "success": True,
                "output_path": expected,
                "format": format,
                "render_format_config": sink,
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
                "channels": channels,
                "file_size_bytes": os.path.getsize(expected),
            }
        except Exception as e:
            logger.error(f"render_project failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def render_time_selection(
        output_path: str,
        start: float,
        end: float,
        format: str = "wav",
        sample_rate: int = 48000,
        bit_depth: int = 24,
        channels: int = 2,
        overwrite: bool = False,
    ) -> dict:
        """
        Render a specific time range (seconds) of the project to a file.

        Uses a custom render range, so the project's own time selection is left alone.
        """
        try:
            if end <= start:
                return {"success": False, "error": f"end ({end}) must be greater than start ({start})"}
            directory, pattern, expected = _split_target(output_path, format)
            os.makedirs(directory, exist_ok=True)
            refused = _prepare_target(expected, overwrite)
            if refused:
                return refused
            with preserved_render_settings():
                _configure(
                    directory, pattern, format, sample_rate, bit_depth, channels,
                    BOUNDS_CUSTOM, start=start, end=end,
                )
                _run_render()
            if not os.path.exists(expected):
                return {"success": False, "error": "Render completed but output file not found"}
            return {
                "success": True,
                "output_path": expected,
                "start": start,
                "end": end,
                "format": format,
                "file_size_bytes": os.path.getsize(expected),
            }
        except Exception as e:
            logger.error(f"render_time_selection failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def render_stems(
        output_directory: str,
        track_indices: list = None,
        format: str = "wav",
        sample_rate: int = 48000,
        bit_depth: int = 24,
        overwrite: bool = False,
        source_override: int = None,
    ) -> dict:
        """
        Render each track as a separate stem file, named after the track.

        Uses REAPER's own "Stems (selected tracks)" render source (post-FX,
        post-fader, with anything routed into the track included), so folders and
        sends behave exactly as in the Render dialog.
        track_indices: tracks to render, or null for all tracks.
        The tracks' selection and the project's render settings are restored afterwards.
        overwrite: replace stem files that already exist in output_directory.
        source_override: raw RENDER_SETTINGS value (diagnostic; default stems-only).
        """
        project = None
        previously_selected = []
        try:
            output_directory = str(Path(output_directory).expanduser().resolve())
            os.makedirs(output_directory, exist_ok=True)
            project = get_project()
            indices = track_indices if track_indices is not None else list(range(project.n_tracks))
            if not indices:
                return {"success": False, "error": "No tracks to render"}
            tracks = [project.tracks[i] for i in indices]

            ext = EXTENSIONS.get(format.lower())
            if ext is None:
                raise ValueError(f"Unsupported format '{format}'. Choose from {sorted(EXTENSIONS)}")

            all_tracks = [project.tracks[j] for j in range(project.n_tracks)]
            previously_selected = [
                t for t in all_tracks if RPR.GetMediaTrackInfo_Value(t.id, "I_SELECTED")
            ]
            started = time.time() - 1.0
            existing = {f for f in os.listdir(output_directory)}

            for t in all_tracks:
                RPR.SetMediaTrackInfo_Value(t.id, "I_SELECTED", 0)
            for t in tracks:
                RPR.SetMediaTrackInfo_Value(t.id, "I_SELECTED", 1)

            collisions = sorted(
                f for f in existing
                if f.lower().endswith(ext) and any(
                    _comparable(os.path.splitext(f)[0]) == _comparable(t.name or f"Track_{i}")
                    for t, i in zip(tracks, indices)
                )
            )
            if collisions:
                if not overwrite:
                    return {
                        "success": False,
                        "error": "Stem files already exist: "
                        + ", ".join(collisions)
                        + ". Pass overwrite=True to replace them.",
                    }
                for name in collisions:
                    os.remove(os.path.join(output_directory, name))

            with preserved_render_settings():
                _configure(
                    output_directory, "$track", format, sample_rate, bit_depth, 2,
                    BOUNDS_ENTIRE_PROJECT, source=SOURCE_STEMS_ONLY if source_override is None else int(source_override),
                )
                debug = {
                    "render_settings": RPR.GetSetProjectInfo(0, "RENDER_SETTINGS", 0.0, False),
                    "selected_tracks": [
                        j for j in range(project.n_tracks)
                        if RPR.GetMediaTrackInfo_Value(project.tracks[j].id, "I_SELECTED")
                    ],
                    "count_selected": RPR.CountSelectedTracks(0),
                }
                _run_render()

            produced = sorted(
                f for f in os.listdir(output_directory)
                if f.lower().endswith(ext)
                and os.path.getmtime(os.path.join(output_directory, f)) >= started
            )
            stems = []
            for t, i in zip(tracks, indices):
                name = t.name or f"Track_{i}"
                match = next(
                    (f for f in produced if _comparable(os.path.splitext(f)[0]) == _comparable(name)),
                    None,
                )
                stems.append({
                    "track_index": i,
                    "track_name": name,
                    "output_path": os.path.join(output_directory, match) if match else None,
                    "exists": match is not None,
                })
            return {
                "success": True,
                "output_directory": output_directory,
                "stems": stems,
                "files": produced,
                "debug": debug,
            }
        except Exception as e:
            logger.error(f"render_stems failed: {e}")
            return {"success": False, "error": str(e)}
        finally:
            if project is not None:
                try:
                    for j in range(project.n_tracks):
                        RPR.SetMediaTrackInfo_Value(project.tracks[j].id, "I_SELECTED", 0)
                    for t in previously_selected:
                        RPR.SetMediaTrackInfo_Value(t.id, "I_SELECTED", 1)
                except Exception:
                    logger.warning("could not restore track selection after render_stems")


def _comparable(name: str) -> str:
    """Normalise a track/file name so REAPER's filename sanitising doesn't break matching."""
    return "".join(c for c in name.lower() if c.isalnum())
