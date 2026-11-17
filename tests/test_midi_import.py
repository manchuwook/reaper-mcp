"""insert_midi_file: drop a whole .mid (e.g. a drummer-library groove) onto a track.

The behaviour these tests pin down was checked against live REAPER 7.86:

* The item is built from ``PCM_Source_CreateFromFile``, never ``InsertMedia``.
  ``InsertMedia`` is the GUI import path; for a MIDI file with a tempo it pops a
  modal "import tempo map?" prompt that blocks the tool call and, if accepted,
  writes the file's tempo into the project (a 150 BPM groove re-timed a whole
  120 BPM session).
* Length is the source's length in quarter notes, placed on the project tempo
  map, so a groove written at 150 BPM fills exactly its bars in a 120 BPM song.
"""

import base64
import os

import pytest

MIDI_BYTES = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\xf0MTrk\x00\x00\x00\x04\x00\xff\x2f\x00"
IN_PROJECT_CHUNK = "<ITEM\n<SOURCE MIDI\nHASDATA 1 240 QN\nIGNTEMPO 0 120 4 4\n>\n>"

ITEM, TAKE, SOURCE = "(MediaItem*)0xa", "(MediaItem_Take*)0xb", "(PCM_source*)0xc"


@pytest.fixture
def midi_file(tmp_path):
    path = tmp_path / "150 DBeat 1 OH A.mid"
    path.write_bytes(MIDI_BYTES)
    return str(path)


@pytest.fixture
def reaper(rpr, make_project, tmp_path):
    """A 120 BPM 4/4 project whose new items load a 32-quarter-note MIDI source."""
    project = make_project(n_tracks=3)
    state = {
        "chunk": IN_PROJECT_CHUNK,
        "existing": [],  # (item_id, position, length) already on the track
        "values": {},  # item info written by the tool
        "source_qn": 32.0,
    }

    # 120 BPM: 2 quarter notes per second, 4/4 bars of 2 s.
    rpr.TimeMap2_timeToQN.side_effect = lambda p, t: t * 2.0
    rpr.TimeMap2_QNToTime.side_effect = lambda p, qn: qn / 2.0
    rpr.TimeMap2_beatsToTime.side_effect = lambda p, tpos, measure: [measure * 2.0, p, tpos, measure]
    rpr.TimeMap2_timeToBeats.side_effect = lambda p, t, *_: [0.0, p, t, int(t // 2.0), 4, t * 2.0, 4]

    rpr.GetProjectPath.return_value = [str(tmp_path / "media"), 4096]
    rpr.CountTrackMediaItems.side_effect = lambda tid: len(state["existing"])
    rpr.GetTrackMediaItem.side_effect = lambda tid, i: state["existing"][i][0]

    def item_value(item_id, key):
        for existing, pos, length in state["existing"]:
            if existing == item_id:
                return {"D_POSITION": pos, "D_LENGTH": length}.get(key, 0.0)
        return state["values"].get(key, 0.0)

    def set_item_value(item_id, key, value):
        state["values"][key] = value
        return True

    rpr.GetMediaItemInfo_Value.side_effect = item_value
    rpr.SetMediaItemInfo_Value.side_effect = set_item_value

    rpr.PCM_Source_CreateFromFile.return_value = SOURCE
    rpr.AddMediaItemToTrack.return_value = ITEM
    rpr.AddTakeToMediaItem.return_value = TAKE
    rpr.GetActiveTake.return_value = TAKE
    rpr.TakeIsMIDI.return_value = True
    rpr.GetMediaItemTake_Source.return_value = SOURCE
    rpr.GetMediaSourceLength.side_effect = lambda src, _: [state["source_qn"], src, True]
    rpr.GetItemStateChunk.side_effect = lambda item, *_: [True, item, state["chunk"], 1 << 22, False]
    rpr.GetTakeName.return_value = "150 DBeat 1 OH A"
    rpr.CountSelectedMediaItems.return_value = 0
    return project, state


class TestInsertMidiFile:
    def test_a_groove_lands_on_its_bar_and_fills_its_bars_at_project_tempo(
        self, register, rpr, reaper, midi_file
    ):
        project, state = reaper
        result = register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file, bar=9)

        assert result["success"] is True
        # bar 9 of a 120 BPM 4/4 song starts at 16 s; 32 quarter notes are 16 s,
        # whatever tempo the file itself was written at.
        assert state["values"]["D_POSITION"] == 16.0
        assert state["values"]["D_LENGTH"] == 16.0
        assert result["start_bar"] == 9
        assert result["length_qn"] == 32.0
        rpr.PCM_Source_CreateFromFile.assert_called_once_with(midi_file)
        rpr.SetMediaItemTake_Source.assert_called_once_with(TAKE, SOURCE)

    def test_never_uses_insert_media_which_can_prompt_and_import_a_tempo_map(
        self, register, rpr, reaper, midi_file
    ):
        project, _ = reaper
        register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file, bar=1)
        rpr.InsertMedia.assert_not_called()

    def test_length_follows_a_tempo_change_inside_the_groove(
        self, register, rpr, reaper, midi_file
    ):
        project, state = reaper
        # 120 BPM up to quarter note 8 (4 s), then 60 BPM.
        rpr.TimeMap2_timeToQN.side_effect = lambda p, t: t * 2.0 if t <= 4.0 else 8.0 + (t - 4.0)
        rpr.TimeMap2_QNToTime.side_effect = lambda p, qn: qn / 2.0 if qn <= 8.0 else 4.0 + (qn - 8.0)
        state["source_qn"] = 16.0
        register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file, position=0.0)
        # 8 quarter notes at 120 (4 s) + 8 at 60 (8 s)
        assert state["values"]["D_LENGTH"] == 12.0

    def test_repeats_loop_the_file_within_one_item(self, register, rpr, reaper, midi_file):
        project, state = reaper
        state["source_qn"] = 4.0  # a one-bar fill
        result = register("midi_tools", project)["insert_midi_file"](
            2, file_path=midi_file, bar=1, repeats=4
        )
        assert state["values"]["D_LENGTH"] == 8.0  # four 2 s bars
        assert state["values"]["B_LOOPSRC"] == 1
        assert result["length_qn"] == 16.0

    def test_without_a_position_it_appends_after_the_last_item(
        self, register, rpr, reaper, midi_file
    ):
        project, state = reaper
        state["existing"] = [("(MediaItem*)0x1", 0.0, 16.0), ("(MediaItem*)0x2", 16.0, 2.0)]
        register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file)
        assert state["values"]["D_POSITION"] == 18.0

    def test_a_source_ignoring_project_tempo_is_made_to_follow_it(
        self, register, rpr, reaper, midi_file
    ):
        project, state = reaper
        state["chunk"] = IN_PROJECT_CHUNK.replace("IGNTEMPO 0 120", "IGNTEMPO 1 150")
        result = register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file, bar=1)
        written = rpr.SetItemStateChunk.call_args.args[1]
        assert "IGNTEMPO 0 150 4 4" in written
        assert result["tempo_flag_fixed"] is True

    def test_a_file_reference_is_converted_to_in_project_midi(
        self, register, rpr, reaper, midi_file
    ):
        # Otherwise editing the item in the MIDI editor would rewrite the library file.
        project, state = reaper
        state["chunk"] = f'<ITEM\n<SOURCE MIDI\nFILE "{midi_file}"\nIGNTEMPO 0 120 4 4\n>\n>'
        rpr.CountSelectedMediaItems.return_value = 1
        rpr.GetSelectedMediaItem.return_value = "(MediaItem*)0x99"

        result = register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file, bar=1)

        rpr.Main_OnCommand.assert_called_once_with(40684, 0)
        assert result["converted_to_in_project"] is True
        # the user's own selection is put back afterwards
        assert rpr.SetMediaItemSelected.call_args_list[-1].args == ("(MediaItem*)0x99", True)

    def test_in_project_midi_is_left_alone(self, register, rpr, reaper, midi_file):
        project, _ = reaper
        result = register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file, bar=1)
        rpr.Main_OnCommand.assert_not_called()
        rpr.SetItemStateChunk.assert_not_called()
        assert result["converted_to_in_project"] is False

    def test_drummer_library_base64_is_written_once_to_the_project_media_folder(
        self, register, rpr, reaper, tmp_path
    ):
        project, _ = reaper
        tool = register("midi_tools", project)["insert_midi_file"]
        b64 = base64.b64encode(MIDI_BYTES).decode()

        first = tool(2, midi_base64=b64, name="150 DBeat 1 OH A", bar=1)
        second = tool(2, midi_base64=b64, name="150 DBeat 1 OH A", bar=9)

        path = first["file_path"]
        assert path == second["file_path"]
        assert os.path.dirname(path) == str(tmp_path / "media" / "MIDI Imports")
        assert os.path.basename(path).startswith("150 DBeat 1 OH A [")
        with open(path, "rb") as f:
            assert f.read() == MIDI_BYTES
        rpr.GetSetMediaItemTakeInfo_String.assert_called_with(TAKE, "P_NAME", "150 DBeat 1 OH A", True)

    def test_the_change_is_one_undo_step(self, register, rpr, reaper, midi_file):
        project, _ = reaper
        register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file, bar=1)
        rpr.Undo_BeginBlock2.assert_called_once()
        rpr.Undo_EndBlock2.assert_called_once_with(project.id, "Insert MIDI file", -1)

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({}, "exactly one"),
            ({"file_path": "x.mid", "midi_base64": "TVRoZA=="}, "exactly one"),
            ({"midi_base64": "not base64!"}, "not valid base64"),
            ({"midi_base64": base64.b64encode(b"RIFF....").decode()}, "MThd"),
            ({"file_path": "Z:/does/not/exist.mid"}, "File not found"),
            ({"midi_base64": base64.b64encode(MIDI_BYTES).decode(), "repeats": 0}, "repeats"),
        ],
    )
    def test_bad_input_is_rejected_before_touching_reaper(
        self, register, rpr, reaper, kwargs, message
    ):
        project, _ = reaper
        result = register("midi_tools", project)["insert_midi_file"](2, **kwargs)
        assert result["success"] is False
        assert message in result["error"]
        rpr.AddMediaItemToTrack.assert_not_called()

    def test_an_unknown_track_is_rejected(self, register, rpr, reaper, midi_file):
        project, _ = reaper
        result = register("midi_tools", project)["insert_midi_file"](7, file_path=midi_file)
        assert result["success"] is False
        rpr.AddMediaItemToTrack.assert_not_called()

    def test_a_file_reaper_cannot_load_as_midi_leaves_no_item_behind(
        self, register, rpr, reaper, midi_file
    ):
        project, _ = reaper
        rpr.TakeIsMIDI.return_value = False
        result = register("midi_tools", project)["insert_midi_file"](2, file_path=midi_file)
        assert result["success"] is False
        rpr.DeleteTrackMediaItem.assert_called_once_with(project.tracks[2].id, ITEM)
