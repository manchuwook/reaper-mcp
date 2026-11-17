import base64
import binascii
import hashlib
import logging
import os
import re

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project, is_valid_pointer
from reaper_mcp.timing import qn_at, seconds_at_qn

logger = logging.getLogger("reaper_mcp.midi_tools")

#: Folder (under the project's media path) that decoded base64 MIDI is written to.
MIDI_IMPORT_SUBDIR = "MIDI Imports"

#: Action "Item: Convert active take MIDI to in-project MIDI source data".
_CMD_MIDI_TO_IN_PROJECT = 40684

_IGNTEMPO_RE = re.compile(r"^([ \t]*IGNTEMPO[ \t]+)1\b", re.MULTILINE)
_MIDI_FILE_REF_RE = re.compile(r"<SOURCE MIDI[ \t]*\r?\n[ \t]*FILE ")

# GM standard drum MIDI notes
DRUM_MAPPINGS = {
    "k": 36,  # kick  - C1
    "s": 38,  # snare - D1
    "h": 42,  # hihat closed - F#1
    "o": 46,  # hihat open   - A#1
    "t": 41,  # tom low  - F1
    "m": 45,  # tom mid  - A1
    "f": 48,  # tom high - C2
    "c": 49,  # crash - C#2
    "r": 51,  # ride  - D#2
}

CHORD_TYPES = {
    "maj":   [0, 4, 7],
    "min":   [0, 3, 7],
    "m":     [0, 3, 7],
    "dim":   [0, 3, 6],
    "aug":   [0, 4, 8],
    "maj7":  [0, 4, 7, 11],
    "min7":  [0, 3, 7, 10],
    "m7":    [0, 3, 7, 10],
    "7":     [0, 4, 7, 10],
    "dom7":  [0, 4, 7, 10],
    "dim7":  [0, 3, 6, 9],
    "hdim7": [0, 3, 6, 10],
    "sus2":  [0, 2, 7],
    "sus4":  [0, 5, 7],
}

NOTE_TO_NUMBER = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8,
    "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11,
}


def _parse_chord(chord_str: str):
    """Return (intervals_list, root_semitone) for a chord string like 'Cm7', 'G', 'F#maj7'."""
    chord_str = chord_str.strip()
    if len(chord_str) >= 2 and chord_str[1] in ("#", "b"):
        root = chord_str[:2]
        chord_type = chord_str[2:] or "maj"
    else:
        root = chord_str[:1]
        chord_type = chord_str[1:] or "maj"
    intervals = CHORD_TYPES.get(chord_type, CHORD_TYPES["maj"])
    root_num = NOTE_TO_NUMBER.get(root, 0)
    return intervals, root_num


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:120]


def _write_midi_bytes(data: bytes, name: str) -> str:
    """Write decoded MIDI into the project's media folder; returns the path.

    The file must outlive this call: if REAPER is set to import MIDI as a file
    reference (rather than in-project data) the item keeps pointing at it.
    The content hash in the filename makes re-inserting the same groove reuse
    one file instead of piling up copies.
    """
    media_dir = RPR.GetProjectPath("", 4096)[0]
    folder = os.path.join(media_dir, MIDI_IMPORT_SUBDIR)
    os.makedirs(folder, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:12]
    stem = _safe_filename(name) if name else "groove"
    path = os.path.join(folder, f"{stem} [{digest}].mid")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return path


def _track_item_ids(track_id) -> list:
    return [RPR.GetTrackMediaItem(track_id, i) for i in range(RPR.CountTrackMediaItems(track_id))]


def _make_in_project(item_id) -> bool:
    """Store the take's MIDI inside the project rather than referencing the file.

    Whether a file loads as in-project data or as a ``FILE "..."`` reference
    depends on the user's import preference. A reference would make edits in the
    MIDI editor write back into the groove library's .mid file, so convert it.
    The conversion action works on selected items, so the selection is saved
    and restored around it. Returns True if a conversion was needed.
    """
    chunk = RPR.GetItemStateChunk(item_id, "", 1 << 22, False)[2]
    if not _MIDI_FILE_REF_RE.search(chunk):
        return False
    selected = [RPR.GetSelectedMediaItem(0, i) for i in range(RPR.CountSelectedMediaItems(0))]
    RPR.SelectAllMediaItems(0, False)
    RPR.SetMediaItemSelected(item_id, True)
    RPR.Main_OnCommand(_CMD_MIDI_TO_IN_PROJECT, 0)
    RPR.SetMediaItemSelected(item_id, False)
    for other in selected:
        RPR.SetMediaItemSelected(other, True)
    return True


def _follow_project_tempo(item_id) -> bool:
    """Make the item's MIDI source follow the project tempo map.

    REAPER's MIDI import can (depending on preferences) mark the source
    "ignore project tempo, use <file bpm>", which shows up in the item chunk
    as ``IGNTEMPO 1 <bpm> <num> <denom>``. A 150 BPM groove would then play at
    150 inside a 120 BPM project. Flipping the flag to 0 makes REAPER read the
    notes in quarter notes against the project tempo. Returns True if the
    chunk had to be changed.
    """
    chunk = RPR.GetItemStateChunk(item_id, "", 1 << 22, False)[2]
    fixed, n = _IGNTEMPO_RE.subn(r"\g<1>0", chunk)
    if n:
        RPR.SetItemStateChunk(item_id, fixed, False)
    return bool(n)


def register_tools(mcp):

    @mcp.tool()
    def insert_midi_file(
        track_index: int,
        file_path: str = "",
        midi_base64: str = "",
        bar: int = 0,
        position: float = -1.0,
        repeats: int = 1,
        name: str = "",
    ) -> dict:
        """
        Insert a whole .mid file onto a track as one MIDI item, the same way
        dragging the file into REAPER does. Use this for drummer-library grooves
        instead of rebuilding them note by note.

        Source (exactly one):
          file_path:   path to a .mid file on this machine.
          midi_base64: the midiBase64 field from drummer-library's get_groove (or
                       apply_drummer_style / generate_groove). It is written to
                       "<project media folder>/MIDI Imports/" and imported from there.
        Placement (first one given wins):
          bar:      1-based measure number (bar=9 -> 9.1.00), following the project's
                    tempo and time-signature map.
          position: seconds on the timeline.
          neither:  directly after the last item already on the track, so calling
                    this repeatedly lays sections end to end.
        repeats: loop the file this many times within the one item.
        name:    take name (defaults to the file name).

        The notes keep their musical (quarter-note) positions and follow the project
        tempo, whatever tempo the file was written at; the item length is the file's
        length in quarter notes times repeats.
        """
        try:
            if bool(file_path) == bool(midi_base64):
                return {"success": False, "error": "Give exactly one of file_path or midi_base64"}
            if repeats < 1:
                return {"success": False, "error": "repeats must be at least 1"}
            if bar < 0:
                return {"success": False, "error": "bar is 1-based; use 0 (or omit) for 'not set'"}

            if midi_base64:
                try:
                    data = base64.b64decode(midi_base64, validate=True)
                except (binascii.Error, ValueError) as e:
                    return {"success": False, "error": f"midi_base64 is not valid base64: {e}"}
            else:
                if not os.path.isfile(file_path):
                    return {"success": False, "error": f"File not found: {file_path}"}
                with open(file_path, "rb") as f:
                    data = f.read(4)
            if not data.startswith(b"MThd"):
                return {"success": False, "error": "Not a standard MIDI file (missing MThd header)"}

            project = get_project()
            if not 0 <= track_index < project.n_tracks:
                return {"success": False, "error": f"No track at index {track_index}"}
            track_id = project.tracks[track_index].id

            with reapy.inside_reaper():
                if midi_base64:
                    file_path = _write_midi_bytes(data, name)

                if bar:
                    start = float(RPR.TimeMap2_beatsToTime(project.id, 0.0, bar - 1)[0])
                elif position >= 0:
                    start = float(position)
                else:
                    start = 0.0
                    for existing in _track_item_ids(track_id):
                        end = RPR.GetMediaItemInfo_Value(existing, "D_POSITION") + \
                            RPR.GetMediaItemInfo_Value(existing, "D_LENGTH")
                        start = max(start, end)

                RPR.Undo_BeginBlock2(project.id)
                RPR.PreventUIRefresh(1)
                try:
                    # Not InsertMedia: that is the GUI import path, and for a MIDI file
                    # carrying a tempo it can pop a modal "import tempo map?" prompt,
                    # which blocks this call and, if accepted, rewrites the project's
                    # tempo map. Building the item from a source never prompts.
                    source = RPR.PCM_Source_CreateFromFile(file_path)
                    if not is_valid_pointer(source):
                        raise RuntimeError(f"REAPER could not read {file_path}")
                    item_id = RPR.AddMediaItemToTrack(track_id)
                    take = RPR.AddTakeToMediaItem(item_id)
                    RPR.SetMediaItemTake_Source(take, source)
                    RPR.SetMediaItemInfo_Value(item_id, "D_POSITION", start)
                    if not RPR.TakeIsMIDI(take):
                        RPR.DeleteTrackMediaItem(track_id, item_id)
                        raise RuntimeError("REAPER did not load the file as MIDI")

                    in_project = _make_in_project(item_id)
                    tempo_fixed = _follow_project_tempo(item_id)
                    take = RPR.GetActiveTake(item_id)  # chunk rewrites can replace the take
                    RPR.GetSetMediaItemTakeInfo_String(
                        take, "P_NAME", name or os.path.splitext(os.path.basename(file_path))[0], True
                    )

                    source = RPR.GetMediaItemTake_Source(take)
                    src = RPR.GetMediaSourceLength(source, False)
                    src_len, is_qn = float(src[0]), bool(src[2])
                    start_qn = qn_at(project, start)
                    if is_qn:
                        length_qn = src_len * repeats
                        end = seconds_at_qn(project, start_qn + length_qn)
                    else:
                        end = start + src_len * repeats
                        length_qn = qn_at(project, end) - start_qn

                    RPR.SetMediaItemInfo_Value(item_id, "D_POSITION", start)
                    RPR.SetMediaItemInfo_Value(item_id, "D_LENGTH", end - start)
                    RPR.SetMediaItemInfo_Value(item_id, "B_LOOPSRC", 1 if repeats > 1 else 0)
                    RPR.UpdateItemInProject(item_id)
                    take_name = RPR.GetTakeName(take)
                    item_index = int(RPR.GetMediaItemInfo_Value(item_id, "IP_ITEMNUMBER"))
                finally:
                    RPR.PreventUIRefresh(-1)
                    RPR.Undo_EndBlock2(project.id, "Insert MIDI file", -1)
                    RPR.UpdateArrange()

                measure_info = RPR.TimeMap2_timeToBeats(project.id, start, 0, 0, 0, 0)

            return {
                "success": True,
                "track_index": track_index,
                "item_index": item_index,
                "name": take_name,
                "file_path": file_path,
                "position": start,
                "length": end - start,
                "end": end,
                "start_bar": int(measure_info[3]) + 1,
                "length_qn": length_qn,
                "source_length_qn": src_len if is_qn else None,
                "repeats": repeats,
                "converted_to_in_project": in_project,
                "tempo_flag_fixed": tempo_fixed,
            }
        except Exception as e:
            logger.error(f"insert_midi_file failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def create_midi_item(track_index: int, start_position: float, length: float) -> dict:
        """Create an empty MIDI item on a track. Returns item_id for use with add_midi_note."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            item = track.add_midi_item(start_position, start_position + length)
            return {
                "success": True,
                "item_id": item.id,
                "item_index": track.n_items - 1,
                "position": item.position,
                "length": item.length,
                "track_index": track_index,
            }
        except Exception as e:
            logger.error(f"create_midi_item failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def add_midi_note(
        track_index: int,
        item_index: int,
        pitch: int,
        start: float,
        length: float,
        velocity: int = 100,
        channel: int = 0,
    ) -> dict:
        """
        Add a MIDI note to an existing MIDI item.
        pitch: MIDI note number 0–127 (60 = middle C, 69 = A4).
        start/length: seconds, relative to the item's start.
        channel: MIDI channel 0–15 (use 9 for drums).
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            item = track.items[item_index]
            take = item.active_take
            if not take.is_midi:
                return {"success": False, "error": "Item is not a MIDI item"}
            # reapy's Take.add_note(unit="seconds") takes *item-relative* time:
            # _resolve_midi_unit adds the item's position itself. Passing absolute
            # project time would add the item position twice.
            take.add_note(
                start=start,
                end=start + length,
                pitch=pitch,
                velocity=velocity,
                channel=channel,
            )
            return {
                "success": True,
                "track_index": track_index,
                "item_index": item_index,
                "pitch": pitch,
                "start": start,
                "length": length,
                "velocity": velocity,
                "channel": channel,
            }
        except Exception as e:
            logger.error(f"add_midi_note failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def create_chord_progression(
        track_index: int,
        chords: str,
        start_position: float,
        beats_per_chord: int = 4,
    ) -> dict:
        """
        Create a chord progression on a track as a single MIDI item.
        chords: comma-separated chord names, e.g. "C,G,Am,F" or "Cm7,Fm7,Bb7,Ebmaj7".
        Supports: maj, min/m, dim, aug, maj7, min7/m7, dom7/7, dim7, hdim7, sus2, sus4.
        All chords are voiced around middle C (MIDI 60).
        beats_per_chord counts quarter notes. Lengths follow the project's tempo map
        (TimeMap2_QNToTime), so tempo changes after start_position are honoured.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            if beats_per_chord <= 0:
                return {"success": False, "error": "beats_per_chord must be positive"}
            chord_list = [c.strip() for c in chords.split(",")]

            # One batched session: every note is a socket round trip otherwise.
            with reapy.inside_reaper():
                first_qn = qn_at(project, start_position)
                end_position = seconds_at_qn(project, first_qn + beats_per_chord * len(chord_list))
                total_length = end_position - start_position

                item = track.add_midi_item(start_position, end_position)
                take = item.active_take
                added_chords = []

                for i, chord_str in enumerate(chord_list):
                    try:
                        intervals, root_num = _parse_chord(chord_str)
                        chord_qn = first_qn + i * beats_per_chord
                        chord_start = seconds_at_qn(project, chord_qn)
                        chord_end = seconds_at_qn(project, chord_qn + beats_per_chord)
                        note_end = seconds_at_qn(project, chord_qn + beats_per_chord * 0.95)
                        # chord_start is absolute project time (reported to the caller);
                        # add_note wants item-relative time.
                        for interval in intervals:
                            take.add_note(
                                start=chord_start - start_position,
                                end=note_end - start_position,
                                pitch=60 + root_num + interval,
                                velocity=80,
                                channel=0,
                                sort=False,
                            )
                        added_chords.append({
                            "chord": chord_str,
                            "position": chord_start,
                            "length": chord_end - chord_start,
                        })
                    except Exception as e:
                        logger.warning(f"Skipping chord '{chord_str}': {e}")
                take.sort_events()  # sorted once, not after every note

            return {
                "success": True,
                "item_id": item.id,
                "chords": added_chords,
                "start_position": start_position,
                "total_length": total_length,
            }
        except Exception as e:
            logger.error(f"create_chord_progression failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def create_drum_pattern(
        track_index: int,
        pattern: str,
        start_position: float,
        beats: int = 4,
        repeats: int = 1,
    ) -> dict:
        """
        Create a drum pattern on a track using a step-sequencer string.
        Each character = one step. Characters: k=kick, s=snare, h=hihat(closed),
        o=hihat(open), t=tom(low), m=tom(mid), f=tom(high), c=crash, r=ride, .=rest.
        Example 4/4 rock beat (16 steps): "k...h...s...h..."
        All drum notes are placed on MIDI channel 9 (GM standard).
        beats counts quarter notes per repeat; timing follows the project's tempo map.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            if not pattern:
                return {"success": False, "error": "pattern must not be empty"}
            if beats <= 0 or repeats < 1:
                return {"success": False, "error": "beats must be positive and repeats at least 1"}

            with reapy.inside_reaper():
                first_qn = qn_at(project, start_position)
                end_position = seconds_at_qn(project, first_qn + beats * repeats)
                total_length = end_position - start_position

                item = track.add_midi_item(start_position, end_position)
                take = item.active_take
                step_qn = beats / len(pattern)

                for repeat in range(repeats):
                    for i, char in enumerate(pattern):
                        if char in DRUM_MAPPINGS:
                            step = first_qn + repeat * beats + i * step_qn
                            # item-relative time: reapy's add_note adds the item position itself
                            take.add_note(
                                start=seconds_at_qn(project, step) - start_position,
                                end=seconds_at_qn(project, step + step_qn * 0.5) - start_position,
                                pitch=DRUM_MAPPINGS[char],
                                velocity=100,
                                channel=9,
                                sort=False,
                            )
                take.sort_events()

            return {
                "success": True,
                "item_id": item.id,
                "pattern": pattern,
                "repeats": repeats,
                "start_position": start_position,
                "total_length": total_length,
            }
        except Exception as e:
            logger.error(f"create_drum_pattern failed: {e}")
            return {"success": False, "error": str(e)}
