"""Track, audio, MIDI, FX, mixing and envelope tools.

Most of these guard against one failure mode: reapy objects silently accepting an
assignment (``track.armed = True``, ``take.pitch = 3``) that never reaches REAPER,
so the tool reports success while nothing happened. Each test checks that the real
ReaScript call is made, with the value the API documentation says it takes.
"""

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------


class TestCreateTrack:
    @pytest.mark.parametrize("track_type", ["midi", "instrument"])
    def test_midi_tracks_record_from_all_devices_on_all_channels(
        self, register, rpr, make_project, track_type
    ):
        project = make_project(n_tracks=0)
        tools = register("track_tools", project)

        tools["create_track"]("Keys", track_type)

        # 4096 = MIDI input; (63 << 5) = all devices; low 5 bits 0 = all channels.
        # A bare 4096 would mean "device 0 only".
        rpr.SetMediaTrackInfo_Value.assert_called_once_with(project.tracks[0].id, "I_RECINPUT", 6112)

    def test_folder_tracks_start_a_folder(self, register, rpr, make_project):
        project = make_project(n_tracks=0)
        register("track_tools", project)["create_track"]("Drums", "folder")
        rpr.SetMediaTrackInfo_Value.assert_called_once_with(project.tracks[0].id, "I_FOLDERDEPTH", 1)

    def test_a_folder_can_be_closed_by_setting_the_last_childs_depth(
        self, register, rpr, make_project, track_info
    ):
        project = make_project(n_tracks=3)
        tools = register("track_tools", project)

        result = tools["set_track_folder_depth"](2, -1)

        assert result == {"success": True, "track_index": 2, "folder_depth": -1}
        assert register("track_tools", project)["set_track_folder_depth"](0, 2)["success"] is False


# ---------------------------------------------------------------------------
# Audio items and recording
# ---------------------------------------------------------------------------


class TestRecording:
    def test_arms_the_track_through_reascript_before_recording(
        self, register, rpr, make_project, track_info
    ):
        project = make_project()
        tools = register("audio_tools", project)

        result = tools["start_recording"](1)

        assert result["success"] is True and result["armed"] is True
        assert track_info[(project.tracks[1].id, "I_RECARM")] == 1
        rpr.Main_OnCommand.assert_called_once_with(1013, 0)

    def test_does_not_record_when_reaper_refuses_to_arm(self, register, rpr, make_project):
        project = make_project()
        rpr.GetMediaTrackInfo_Value.return_value = 0.0  # arm did not stick
        result = register("audio_tools", project)["start_recording"](0)
        assert result["success"] is False
        rpr.Main_OnCommand.assert_not_called()


@pytest.fixture
def audio_item(make_project, rpr):
    """A project whose track 0 has one audio item with one take."""
    project = make_project(n_tracks=1)
    item, take = MagicMock(name="item"), MagicMock(name="take")
    item.id, take.id = "(MediaItem*)0x1", "(MediaItemTake*)0x2"
    item.position, item.length, item.active_take = 4.0, 10.0, take
    project.tracks[0].items = [item]
    take_info = {"D_PLAYRATE": 1.0, "D_STARTOFFS": 0.0, "D_PITCH": 0.0}
    item_info = {"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 0.0}
    rpr.GetMediaItemTakeInfo_Value.side_effect = lambda tk, key: take_info[key]
    rpr.GetMediaItemInfo_Value.side_effect = lambda it, key: item_info[key]
    rpr.SetMediaItemTakeInfo_Value.side_effect = lambda tk, key, v: take_info.__setitem__(key, v) or True
    rpr.SetMediaItemInfo_Value.side_effect = lambda it, key, v: item_info.__setitem__(key, v) or True
    return project, item, take, take_info, item_info


class TestAudioItems:
    def test_pitch_is_written_to_the_takes_d_pitch(self, register, rpr, audio_item):
        project, _, take, take_info, _ = audio_item
        result = register("audio_tools", project)["adjust_pitch"](0, 0, -3.5)
        rpr.SetMediaItemTakeInfo_Value.assert_called_once_with(take.id, "D_PITCH", -3.5)
        assert result["pitch_semitones"] == -3.5

    def test_playback_rate_stretches_the_item_and_preserves_pitch_by_default(
        self, register, rpr, audio_item
    ):
        project, item, take, take_info, _ = audio_item
        result = register("audio_tools", project)["adjust_playback_rate"](0, 0, 2.0)
        assert take_info["D_PLAYRATE"] == 2.0
        rpr.SetMediaItemTakeInfo_Value.assert_any_call(take.id, "B_PPITCH", 1)
        assert item.length == 5.0  # double speed -> same source audio in half the time
        assert result["playback_rate"] == 2.0

    def test_playback_rate_can_leave_the_length_and_shift_pitch(self, register, rpr, audio_item):
        project, item, take, _, _ = audio_item
        register("audio_tools", project)["adjust_playback_rate"](
            0, 0, 0.5, preserve_pitch=False, adjust_length=False
        )
        rpr.SetMediaItemTakeInfo_Value.assert_any_call(take.id, "B_PPITCH", 0)
        assert item.length == 10.0

    @pytest.mark.parametrize("rate", [0, -1.0])
    def test_playback_rate_must_be_positive(self, register, rpr, audio_item, rate):
        project = audio_item[0]
        assert register("audio_tools", project)["adjust_playback_rate"](0, 0, rate)["success"] is False
        rpr.SetMediaItemTakeInfo_Value.assert_not_called()

    def test_fades_are_written_to_the_item(self, register, rpr, audio_item):
        project, item, _, _, item_info = audio_item
        result = register("audio_tools", project)["edit_audio_item"](0, 0, fade_in=0.25, fade_out=1.5)
        rpr.SetMediaItemInfo_Value.assert_any_call(item.id, "D_FADEINLEN", 0.25)
        rpr.SetMediaItemInfo_Value.assert_any_call(item.id, "D_FADEOUTLEN", 1.5)
        assert (result["fade_in"], result["fade_out"]) == (0.25, 1.5)

    def test_trimming_the_start_moves_the_source_offset_at_the_takes_rate(
        self, register, rpr, audio_item
    ):
        project, item, take, take_info, _ = audio_item
        take_info["D_PLAYRATE"] = 2.0
        register("audio_tools", project)["edit_audio_item"](0, 0, start_trim=1.0, end_trim=0.5)
        assert take_info["D_STARTOFFS"] == 2.0  # 1 s of project time = 2 s of source
        assert item.position == 5.0
        assert item.length == 8.5  # 10 - 1 - 0.5


# ---------------------------------------------------------------------------
# MIDI
# ---------------------------------------------------------------------------


@pytest.fixture
def midi_item(make_project):
    project = make_project(n_tracks=1, bpm=120.0)
    item, take = MagicMock(name="item"), MagicMock(name="take")
    item.id, item.position, item.active_take = "(MediaItem*)0x1", 10.0, take
    take.is_midi = True
    track = project.tracks[0]
    track.items = [item]
    track.add_midi_item.return_value = item
    return project, item, take


class TestMidiTiming:
    def test_note_start_is_shifted_by_the_items_position(self, register, midi_item):
        project, item, take = midi_item
        register("midi_tools", project)["add_midi_note"](0, 0, pitch=60, start=1.0, length=0.5)
        # Take.add_note takes *project* time; the item sits at 10 s.
        take.add_note.assert_called_once_with(start=11.0, end=11.5, pitch=60, velocity=100, channel=0)

    def test_chords_land_at_the_requested_start_position(self, register, midi_item):
        project, item, take = midi_item
        result = register("midi_tools", project)["create_chord_progression"](
            0, "C,G", start_position=8.0, beats_per_chord=4
        )
        starts = sorted({c.kwargs["start"] for c in take.add_note.call_args_list})
        assert starts == [8.0, 10.0]  # 120 bpm -> a 4-beat chord is 2 s
        assert [c["position"] for c in result["chords"]] == [8.0, 10.0]

    def test_drum_steps_land_at_the_requested_start_position(self, register, midi_item):
        project, item, take = midi_item
        register("midi_tools", project)["create_drum_pattern"](
            0, "k...", start_position=6.0, beats=4, repeats=2
        )
        starts = [c.kwargs["start"] for c in take.add_note.call_args_list]
        assert starts == [6.0, 8.0]  # one kick per 2 s bar, offset by the 6 s start

    def test_an_empty_drum_pattern_is_rejected(self, register, midi_item):
        project, _, take = midi_item
        result = register("midi_tools", project)["create_drum_pattern"](0, "", start_position=0.0)
        assert result["success"] is False
        take.add_note.assert_not_called()


# ---------------------------------------------------------------------------
# FX parameters and envelopes
# ---------------------------------------------------------------------------


class TestMasterFx:
    def test_parameter_write_goes_through_reascript(self, register, rpr, make_project):
        project = make_project()
        master = MagicMock(name="master")
        master.id = "(MediaTrack*)0xM"
        project.master_track = master
        rpr.TrackFX_SetParamNormalized.return_value = True
        rpr.TrackFX_GetParamNormalized.return_value = 0.75
        rpr.TrackFX_GetParamName.return_value = (True, "t", 0, 3, "Threshold", 512)
        rpr.TrackFX_GetFormattedParamValue.return_value = (True, "t", 0, 3, "0.75", 512)
        tools = register("mastering_tools", project)

        result = tools["set_master_fx_parameter"](0, 3, 0.75)

        rpr.TrackFX_SetParamNormalized.assert_called_once_with(master.id, 0, 3, 0.75)
        assert result["success"] is True
        assert result["value"] == 0.75 and result["param_name"] == "Threshold"

    def test_a_rejected_write_is_reported(self, register, rpr, make_project):
        project = make_project()
        project.master_track = MagicMock()
        rpr.TrackFX_SetParamNormalized.return_value = False
        assert register("mastering_tools", project)["set_master_fx_parameter"](0, 3, 2.0)["success"] is False

    def test_limiter_threshold_and_release_are_actually_applied(self, register, rpr, make_project):
        project = make_project()
        master = MagicMock(name="master")
        master.id = "(MediaTrack*)0xM"
        fx = MagicMock(n_params=3, index=0)
        fx.name = "ReaLimit"
        master.add_fx.return_value = fx
        project.master_track = master
        names = {0: "Threshold", 1: "Ceiling", 2: "Release"}
        rpr.TrackFX_GetParamName.side_effect = lambda t, f, i, buf, n: (True, t, f, i, names[i], n)
        rpr.TrackFX_GetFormattedParamValue.return_value = (True, "t", 0, 0, "x", 512)

        result = register("mastering_tools", project)["apply_limiter"](-1.5, 80.0)

        rpr.TrackFX_SetParam.assert_any_call(master.id, 0, 0, -1.5)
        rpr.TrackFX_SetParam.assert_any_call(master.id, 0, 2, 80.0)
        assert rpr.TrackFX_SetParam.call_count == 2
        assert result["unmatched"] == []


class TestFxStepAutomation:
    ENVELOPE = "(TrackEnvelope*)0x1234"

    @pytest.fixture
    def setup(self, register, rpr, make_project):
        project = make_project(n_tracks=1)
        rpr.GetFXEnvelope.return_value = self.ENVELOPE
        return register("envelope_tools", project), rpr

    def written_values(self, rpr):
        return [c.args[2] for c in rpr.InsertEnvelopePoint.call_args_list]

    def test_real_world_values_are_converted_with_the_parameters_own_range(self, setup):
        tools, rpr = setup
        rpr.TrackFX_GetParam.return_value = (5.0, "t", 0, 0, 0.0, 10.0)

        result = tools["set_fx_param_step_automation"](
            0, 0, 4, [{"position": 0.0, "value": 5.0}, {"position": 8.0, "value": 10.0}], normalized=False
        )

        assert result["success"] is True and "warning" not in result
        assert self.written_values(rpr) == [0.5, 1.0]

    def test_a_zero_to_one_range_triggers_a_warning_instead_of_silent_nonsense(self, setup):
        tools, rpr = setup
        rpr.TrackFX_GetParam.return_value = (0.5, "t", 0, 0, 0.0, 1.0)
        result = tools["set_fx_param_step_automation"](
            0, 0, 4, [{"position": 0.0, "value": 0.5}], normalized=False
        )
        assert result["success"] is True
        assert "0-1 range" in result["warning"]

    def test_out_of_range_values_are_rejected_before_the_envelope_is_created(self, setup):
        tools, rpr = setup
        result = tools["set_fx_param_step_automation"](
            0, 0, 4, [{"position": 0.0, "value": 1.5}]  # normalized=True by default
        )
        assert result["success"] is False
        rpr.GetFXEnvelope.assert_not_called()
        rpr.InsertEnvelopePoint.assert_not_called()

    def test_region_automation_validates_and_converts_the_same_way(self, setup):
        tools, rpr = setup
        rpr.CountProjectMarkers.return_value = (2, "p", 0, 2)
        regions = [("Verse", 0.0, 16.0), ("Chorus", 16.0, 32.0)]
        rpr.EnumProjectMarkers2.side_effect = lambda proj, i, a, b, c, name, d: (
            1, proj, i, 1, regions[i][1], regions[i][2], regions[i][0], i
        )
        rpr.TrackFX_GetParam.return_value = (0, "t", 0, 0, 0.0, 4.0)

        result = tools["set_fx_param_step_automation_by_region"](
            0, 0, 4, {"Verse": 1.0, "Chorus": 3.0, "Outro": 2.0}, normalized=False
        )

        assert self.written_values(rpr) == [0.25, 0.75]
        assert result["unmatched_names"] == ["Outro"]
        assert [m["region"] for m in result["matched_regions"]] == ["Verse", "Chorus"]


# ---------------------------------------------------------------------------
# Mixing, routing and automation
# ---------------------------------------------------------------------------


class TestBuses:
    def test_a_submix_bus_turns_off_each_sources_direct_master_send(
        self, register, rpr, make_project
    ):
        project = make_project(n_tracks=3)
        rpr.CreateTrackSend.side_effect = [0, 0]
        tools = register("mixing_tools", project)

        result = tools["create_bus"]("Drums", [0, 1])

        assert result["bus_index"] == 3 and result["master_send_disabled"] is True
        rpr.SetMediaTrackInfo_Value.assert_any_call(project.tracks[0].id, "B_MAINSEND", 0)
        rpr.SetMediaTrackInfo_Value.assert_any_call(project.tracks[1].id, "B_MAINSEND", 0)
        assert rpr.SetMediaTrackInfo_Value.call_count == 2  # the untouched track keeps its send

    def test_an_fx_bus_keeps_the_direct_sends(self, register, rpr, make_project):
        project = make_project(n_tracks=2)
        rpr.CreateTrackSend.return_value = 0
        result = register("mixing_tools", project)["create_bus"]("Reverb", [0, 1], submix=False)
        assert result["master_send_disabled"] is False
        rpr.SetMediaTrackInfo_Value.assert_not_called()

    def test_bad_indices_do_not_leave_a_half_built_bus(self, register, rpr, make_project):
        project = make_project(n_tracks=2)
        result = register("mixing_tools", project)["create_bus"]("Oops", [0, 7])
        assert result["success"] is False
        project.add_track.assert_not_called()


class TestSends:
    def test_a_midi_only_send_has_no_audio_channels(self, register, rpr, make_project):
        project = make_project(n_tracks=2)
        rpr.CreateTrackSend.return_value = 0
        tools = register("mixing_tools", project)

        result = tools["create_send"](0, 1, volume_db=-6.0, send_mode="pre_fx", midi_only=True)

        src = project.tracks[0].id
        rpr.SetTrackSendInfo_Value.assert_any_call(src, 0, 0, "I_SENDMODE", 1)
        rpr.SetTrackSendInfo_Value.assert_any_call(src, 0, 0, "I_SRCCHAN", -1)
        assert result["midi_only"] is True and result["send_mode"] == "pre_fx"

    def test_an_audio_send_leaves_the_source_channels_alone(self, register, rpr, make_project):
        project = make_project(n_tracks=2)
        rpr.CreateTrackSend.return_value = 0
        register("mixing_tools", project)["create_send"](0, 1)
        keys = [c.args[3] for c in rpr.SetTrackSendInfo_Value.call_args_list]
        assert "I_SRCCHAN" not in keys
        rpr.SetTrackSendInfo_Value.assert_any_call(project.tracks[0].id, 0, 0, "I_SENDMODE", 0)

    def test_unknown_send_modes_and_self_sends_are_rejected(self, register, rpr, make_project):
        project = make_project(n_tracks=2)
        tools = register("mixing_tools", project)
        assert tools["create_send"](0, 1, send_mode="sideways")["success"] is False
        assert tools["create_send"](1, 1)["success"] is False
        rpr.CreateTrackSend.assert_not_called()

    def test_listing_shows_db_mode_and_what_each_send_carries(self, register, rpr, make_project):
        project = make_project(n_tracks=2)
        rpr.GetTrackNumSends.return_value = 1
        values = {"D_VOL": 0.5, "D_PAN": 0.0, "B_MUTE": 0, "I_SENDMODE": 1,
                  "I_SRCCHAN": -1, "I_MIDIFLAGS": 0}
        rpr.GetTrackSendInfo_Value.side_effect = lambda tid, cat, i, key: values[key]

        send = register("mixing_tools", project)["list_sends"](0)["sends"][0]

        assert send["volume_db"] == pytest.approx(-6.02, abs=0.01)
        assert send["send_mode"] == "pre_fx"
        assert send["carries_audio"] is False and send["carries_midi"] is True


class TestAutomation:
    ENVELOPE = "(TrackEnvelope*)0x1234"

    def test_volume_points_respect_the_envelopes_fader_scaling(self, register, rpr, make_project):
        project = make_project(n_tracks=1)
        rpr.GetTrackEnvelopeByName.return_value = self.ENVELOPE
        rpr.GetEnvelopeScalingMode.return_value = 1  # fader scaling
        rpr.ScaleToEnvelopeMode.return_value = 0.7

        result = register("mixing_tools", project)["add_volume_automation"](0, 12.0, -6.0)

        assert result["success"] is True
        mode, linear = rpr.ScaleToEnvelopeMode.call_args.args
        assert mode == 1 and linear == pytest.approx(0.501, abs=0.001)
        assert rpr.InsertEnvelopePoint.call_args.args[:3] == (self.ENVELOPE, 12.0, 0.7)

    def test_a_missing_envelope_is_explained(self, register, rpr, make_project):
        project = make_project(n_tracks=1)
        rpr.GetTrackEnvelopeByName.return_value = "(TrackEnvelope*)0x0000000000000000"
        result = register("mixing_tools", project)["add_volume_automation"](0, 1.0, 0.0)
        assert result["success"] is False
        rpr.InsertEnvelopePoint.assert_not_called()

    @pytest.mark.parametrize("mode, code", [("trim", 0), ("read", 1), ("Touch", 2), ("write", 3), ("latch", 4)])
    def test_automation_mode_is_set_on_the_track(self, register, rpr, make_project, track_info, mode, code):
        project = make_project(n_tracks=1)
        result = register("mixing_tools", project)["set_track_automation_mode"](0, mode)
        assert result["success"] is True and result["mode_code"] == code
        assert track_info[(project.tracks[0].id, "I_AUTOMODE")] == code

    def test_unknown_automation_modes_are_rejected(self, register, rpr, make_project):
        project = make_project(n_tracks=1)
        assert register("mixing_tools", project)["set_track_automation_mode"](0, "sticky")["success"] is False
        rpr.SetMediaTrackInfo_Value.assert_not_called()


# ---------------------------------------------------------------------------
# Whole-server sanity
# ---------------------------------------------------------------------------


def test_every_tool_module_registers_unique_tool_names(register):
    modules = [
        "project_tools", "track_tools", "midi_tools", "fx_tools", "audio_tools",
        "mixing_tools", "render_tools", "mastering_tools", "analysis_tools", "envelope_tools",
    ]
    names = []
    for module in modules:
        names.extend(register(module))
    assert len(names) == len(set(names)), "a tool name is registered twice"
    assert {"set_track_automation_mode", "set_track_folder_depth"} <= set(names)
