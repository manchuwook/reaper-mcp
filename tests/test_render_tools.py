"""Render tools.

REAPER's render settings are *project* settings, so the behaviours that matter are
(1) the right keys and values are in force while the render runs, and (2) the
user's own settings come back afterwards. ``project_settings`` backs the
``GetSetProjectInfo*`` calls with dicts so both can be asserted.
"""

import base64
import os

import pytest

WAV_24 = base64.b64encode(b"evaw" + bytes([24, 0, 1])).decode()

USER_NUMERIC = {
    "RENDER_SETTINGS": 2.0,
    "RENDER_BOUNDSFLAG": 3.0,
    "RENDER_CHANNELS": 1.0,
    "RENDER_SRATE": 44100.0,
    "RENDER_STARTPOS": 1.0,
    "RENDER_ENDPOS": 2.0,
    "RENDER_NORMALIZE": 1.0,
    "RENDER_ADDTOPROJ": 1.0,
}
USER_STRINGS = {
    "RENDER_FILE": "C:/their/dir",
    "RENDER_PATTERN": "$project",
    "RENDER_FORMAT": "their-format",
    "RENDER_FORMAT2": "l3pm",
}


@pytest.fixture
def renderer(rpr, project_settings, tmp_path):
    """Seed the user's render settings and make Main_OnCommand(41824) 'render'.

    ``renderer.during`` holds the settings seen at the moment of the render;
    ``renderer.writes`` lists the files the fake render should create, as a
    function of those settings (default: one WAV named by directory + pattern).
    """
    numeric, strings = project_settings
    numeric.update(USER_NUMERIC)
    strings.update(USER_STRINGS)

    class Renderer:
        during = None
        commands = []
        writes = staticmethod(
            lambda n, s: [os.path.join(s["RENDER_FILE"], s["RENDER_PATTERN"] + ".wav")]
        )

    def fake_command(command, flag):
        Renderer.commands.append(command)
        if command == 41824:
            Renderer.during = (dict(numeric), dict(strings))
            for path in Renderer.writes(numeric, strings):
                with open(path, "wb") as f:
                    f.write(b"RIFF")

    Renderer.commands = []
    Renderer.numeric, Renderer.strings = numeric, strings
    rpr.Main_OnCommand.side_effect = fake_command
    return Renderer


def assert_user_settings_restored(renderer):
    for key, value in USER_NUMERIC.items():
        assert renderer.numeric[key] == value, key
    for key, value in USER_STRINGS.items():
        assert renderer.strings[key] == value, key


class TestRenderProject:
    def test_splits_the_path_into_directory_and_pattern_and_renders_the_whole_project(
        self, register, renderer, tmp_path
    ):
        tools = register("render_tools")
        result = tools["render_project"](str(tmp_path / "mix.wav"))

        assert result["success"] is True
        numeric, strings = renderer.during
        assert strings["RENDER_FILE"] == str(tmp_path.resolve())
        assert strings["RENDER_PATTERN"] == "mix"
        assert strings["RENDER_FORMAT"] == WAV_24
        assert strings["RENDER_FORMAT2"] == ""  # no secondary file
        assert numeric["RENDER_BOUNDSFLAG"] == 1.0  # entire project (0 = custom range!)
        assert numeric["RENDER_SETTINGS"] == 0.0  # master mix
        assert numeric["RENDER_SRATE"] == 48000.0
        assert numeric["RENDER_CHANNELS"] == 2.0
        assert result["output_path"] == str(tmp_path.resolve() / "mix.wav")

    def test_switches_off_normalising_and_add_to_project_for_the_render(
        self, register, renderer, tmp_path
    ):
        register("render_tools")["render_project"](str(tmp_path / "mix.wav"))
        numeric, _ = renderer.during
        assert numeric["RENDER_NORMALIZE"] == 0.0
        assert numeric["RENDER_ADDTOPROJ"] == 0.0

    def test_puts_the_users_render_settings_back(self, register, renderer, tmp_path):
        register("render_tools")["render_project"](str(tmp_path / "mix.wav"))
        assert_user_settings_restored(renderer)

    def test_restores_settings_even_when_the_render_command_fails(
        self, register, renderer, rpr, tmp_path
    ):
        rpr.Main_OnCommand.side_effect = RuntimeError("REAPER went away")
        result = register("render_tools")["render_project"](str(tmp_path / "mix.wav"))
        assert result["success"] is False
        assert_user_settings_restored(renderer)

    def test_format_extension_follows_the_requested_format(self, register, renderer, tmp_path):
        renderer.writes = staticmethod(
            lambda n, s: [os.path.join(s["RENDER_FILE"], s["RENDER_PATTERN"] + ".mp3")]
        )
        result = register("render_tools")["render_project"](str(tmp_path / "mix.wav"), format="mp3")
        assert result["success"] is True
        assert result["output_path"].endswith("mix.mp3")
        assert renderer.during[1]["RENDER_FORMAT"] == "l3pm"

    def test_refuses_to_overwrite_unless_asked(self, register, renderer, tmp_path):
        existing = tmp_path / "mix.wav"
        existing.write_bytes(b"precious")
        tools = register("render_tools")

        refused = tools["render_project"](str(existing))
        assert refused["success"] is False
        assert existing.read_bytes() == b"precious"
        assert renderer.commands == []  # an overwrite prompt would hang REAPER

        allowed = tools["render_project"](str(existing), overwrite=True)
        assert allowed["success"] is True
        assert existing.read_bytes() == b"RIFF"

    def test_rejects_unsupported_formats_before_touching_reaper(self, register, renderer, tmp_path):
        result = register("render_tools")["render_project"](str(tmp_path / "x"), format="aac")
        assert result["success"] is False
        assert "aac" in result["error"]
        assert renderer.commands == []

    def test_rejects_unsupported_wav_bit_depths(self, register, renderer, tmp_path):
        result = register("render_tools")["render_project"](str(tmp_path / "x"), bit_depth=20)
        assert result["success"] is False
        assert renderer.commands == []

    def test_reports_a_render_that_produced_no_file(self, register, renderer, tmp_path):
        renderer.writes = staticmethod(lambda n, s: [])
        result = register("render_tools")["render_project"](str(tmp_path / "mix.wav"))
        assert result["success"] is False


class TestRenderTimeSelection:
    def test_uses_a_custom_range_instead_of_touching_the_time_selection(
        self, register, renderer, tmp_path
    ):
        result = register("render_tools")["render_time_selection"](
            str(tmp_path / "chorus.wav"), start=5.0, end=12.5
        )
        assert result["success"] is True
        numeric, _ = renderer.during
        assert numeric["RENDER_BOUNDSFLAG"] == 0.0
        assert numeric["RENDER_STARTPOS"] == 5.0
        assert numeric["RENDER_ENDPOS"] == 12.5
        assert_user_settings_restored(renderer)

    def test_rejects_an_empty_range(self, register, renderer, tmp_path):
        result = register("render_tools")["render_time_selection"](
            str(tmp_path / "x.wav"), start=10.0, end=10.0
        )
        assert result["success"] is False
        assert renderer.commands == []


class TestRenderStems:
    @pytest.fixture
    def setup(self, register, renderer, make_project, track_info, tmp_path):
        project = make_project(n_tracks=3)
        for track, name in zip(project.tracks, ["Kick", "Lead Guitar", "Bass"]):
            track.name = name
        track_info[(project.tracks[2].id, "I_SELECTED")] = 1.0  # user had Bass selected
        selection_during = {}

        def stems_written(numeric, strings):
            chosen = [t for t in project.tracks if track_info.get((t.id, "I_SELECTED"))]
            selection_during["names"] = [t.name for t in chosen]
            return [os.path.join(strings["RENDER_FILE"], t.name + ".wav") for t in chosen]

        renderer.writes = staticmethod(stems_written)
        tools = register("render_tools", project)
        return tools, project, track_info, selection_during, tmp_path

    def test_uses_reapers_stems_source_for_just_the_requested_tracks(self, setup, renderer):
        tools, project, _, selection_during, tmp_path = setup

        result = tools["render_stems"](str(tmp_path / "stems"), track_indices=[0, 1])

        assert result["success"] is True
        numeric, strings = renderer.during
        assert numeric["RENDER_SETTINGS"] == 2.0  # stems only
        assert strings["RENDER_PATTERN"] == "$track"
        assert selection_during["names"] == ["Kick", "Lead Guitar"]
        assert [s["track_name"] for s in result["stems"] if s["exists"]] == ["Kick", "Lead Guitar"]
        assert os.path.exists(tmp_path / "stems" / "Lead Guitar.wav")

    def test_each_stem_is_found_by_name_even_with_spaces(self, setup):
        tools, _, _, _, tmp_path = setup
        result = tools["render_stems"](str(tmp_path / "stems"), track_indices=[1])
        assert result["stems"][0]["output_path"].endswith("Lead Guitar.wav")

    def test_restores_track_selection_and_render_settings(self, setup, renderer):
        tools, project, store, _, tmp_path = setup
        tools["render_stems"](str(tmp_path / "stems"), track_indices=[0])
        selected = [t.name for t in project.tracks if store.get((t.id, "I_SELECTED"))]
        assert selected == ["Bass"]
        assert_user_settings_restored(renderer)

    def test_renders_every_track_when_no_indices_are_given(self, setup, renderer):
        tools, _, _, selection_during, tmp_path = setup
        tools["render_stems"](str(tmp_path / "stems"))
        assert selection_during["names"] == ["Kick", "Lead Guitar", "Bass"]

    def test_refuses_to_clobber_existing_stems_unless_asked(self, setup, renderer):
        tools, _, _, _, tmp_path = setup
        out = tmp_path / "stems"
        out.mkdir()
        (out / "Kick.wav").write_bytes(b"precious")

        refused = tools["render_stems"](str(out), track_indices=[0])
        assert refused["success"] is False and "Kick.wav" in refused["error"]
        assert (out / "Kick.wav").read_bytes() == b"precious"
        assert renderer.commands == []

        assert tools["render_stems"](str(out), track_indices=[0], overwrite=True)["success"] is True


class TestRenderToTempFile:
    def test_renders_to_a_unique_temp_wav_and_leaves_project_settings_alone(
        self, register, renderer, monkeypatch, tmp_path
    ):
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        from reaper_mcp import render_tools

        path = render_tools.render_to_temp_file(sample_rate=44100)

        assert os.path.dirname(path) == str(tmp_path)
        assert os.path.basename(path).startswith("reaper_mcp_") and path.endswith(".wav")
        assert os.path.exists(path)
        numeric, strings = renderer.during
        assert numeric["RENDER_BOUNDSFLAG"] == 1.0
        assert numeric["RENDER_SRATE"] == 44100.0
        assert numeric["RENDER_NORMALIZE"] == 0.0  # analysis must see the mix as it is
        assert_user_settings_restored(renderer)

    def test_raises_when_no_file_appears(self, register, renderer, monkeypatch, tmp_path):
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        renderer.writes = staticmethod(lambda n, s: [])
        from reaper_mcp import render_tools

        with pytest.raises(RuntimeError):
            render_tools.render_to_temp_file()
        assert_user_settings_restored(renderer)
