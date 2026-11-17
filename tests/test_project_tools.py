"""Project tools: time signature, save and load.

Regression tests for bugs found by comparing the tools against the REAPER User
Guide, the ReaScript API reference and the installed ``reapy`` source.
"""

import os

import pytest


@pytest.fixture
def project(make_project):
    return make_project(
        n_tracks=0,
        name="song.rpp",
        path="C:/media",
        bpm=98.0,
        length=60.0,
        n_markers=0,
        n_regions=0,
    )


class TestTimeSignatureRead:
    def test_reports_numerator_and_denominator_not_bpm(self, register, rpr, project):
        # Given reapy's time_signature would have returned (98.0, 4.0) = (bpm, beats)
        rpr.TimeMap_GetTimeSigAtTime.return_value = ("p", 0.0, 3, 4, 98.0)
        tools = register("project_tools", project)
        # When
        info = tools["get_project_info"]()
        # Then the old bug ("98.0/4.0") is gone
        assert info["time_signature"] == "3/4"
        rpr.TimeMap_GetTimeSigAtTime.assert_called_with(project.id, 0.0, 0, 0, 0)


class TestTimeSignatureWrite:
    def test_appends_a_marker_at_time_zero_keeping_the_tempo(self, register, rpr, project):
        rpr.TimeMap_GetTimeSigAtTime.return_value = ("p", 0.0, 4, 4, 120.0)
        rpr.FindTempoTimeSigMarker.return_value = -1
        rpr.SetTempoTimeSigMarker.return_value = True
        tools = register("project_tools", project)

        result = tools["set_time_signature"](7, 8)

        assert result["success"] is True
        rpr.SetTempoTimeSigMarker.assert_called_once_with(
            project.id, -1, 0.0, -1, -1, 120.0, 7, 8, False
        )

    def test_edits_the_existing_marker_at_time_zero(self, register, rpr, project):
        rpr.TimeMap_GetTimeSigAtTime.return_value = ("p", 0.0, 4, 4, 110.0)
        rpr.FindTempoTimeSigMarker.return_value = 0
        rpr.GetTempoTimeSigMarker.return_value = (True, "p", 0, 0.0, 0, 0.0, 110.0, 4, 4, True)
        rpr.SetTempoTimeSigMarker.return_value = True
        tools = register("project_tools", project)

        tools["set_time_signature"](3, 4)

        rpr.SetTempoTimeSigMarker.assert_called_once_with(
            project.id, 0, 0.0, -1, -1, 110.0, 3, 4, True
        )

    def test_does_not_edit_a_marker_that_sits_later_in_the_song(self, register, rpr, project):
        rpr.TimeMap_GetTimeSigAtTime.return_value = ("p", 0.0, 4, 4, 120.0)
        rpr.FindTempoTimeSigMarker.return_value = 2
        rpr.GetTempoTimeSigMarker.return_value = (True, "p", 2, 31.5, 0, 0.0, 90.0, 4, 4, False)
        rpr.SetTempoTimeSigMarker.return_value = True
        tools = register("project_tools", project)

        tools["set_time_signature"](5, 4)

        assert rpr.SetTempoTimeSigMarker.call_args.args[1] == -1

    @pytest.mark.parametrize("num, denom", [(0, 4), (4, 3), (4, 0)])
    def test_rejects_impossible_signatures(self, register, rpr, project, num, denom):
        tools = register("project_tools", project)
        result = tools["set_time_signature"](num, denom)
        assert result["success"] is False
        rpr.SetTempoTimeSigMarker.assert_not_called()

    def test_reports_failure_when_reaper_rejects_the_marker(self, register, rpr, project):
        rpr.TimeMap_GetTimeSigAtTime.return_value = ("p", 0.0, 4, 4, 120.0)
        rpr.FindTempoTimeSigMarker.return_value = -1
        rpr.SetTempoTimeSigMarker.return_value = False
        tools = register("project_tools", project)
        assert tools["set_time_signature"](3, 4)["success"] is False


class TestSaveProject:
    def test_saves_to_the_requested_path_with_save_as(self, register, rpr, project, tmp_path):
        tools = register("project_tools", project)
        target = tmp_path / "out" / "mysong"

        result = tools["save_project"](str(target))

        expected = os.path.abspath(str(target) + ".rpp")
        assert result == {"success": True, "project_path": expected}
        # reapy's Project.save() takes a bool, not a path; the path goes via SaveProjectEx
        rpr.Main_SaveProjectEx.assert_called_once_with(project.id, expected, 8)
        rpr.Main_SaveProject.assert_not_called()
        assert (tmp_path / "out").is_dir()

    def test_saves_an_already_saved_project_in_place(self, register, rpr, project):
        rpr.EnumProjects.return_value = ("p", -1, "C:/music/song.rpp", 2048)
        tools = register("project_tools", project)

        result = tools["save_project"]()

        assert result == {"success": True, "project_path": "C:/music/song.rpp"}
        rpr.Main_SaveProject.assert_called_once_with(project.id, False)
        rpr.Main_SaveProjectEx.assert_not_called()

    def test_saves_a_new_project_into_the_default_folder(self, register, rpr, project, monkeypatch, tmp_path):
        rpr.EnumProjects.return_value = ("p", -1, "", 2048)
        project.name = ""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        tools = register("project_tools", project)

        result = tools["save_project"]()

        assert result["success"] is True
        assert result["project_path"].startswith(str(tmp_path / "Documents" / "REAPER Projects"))
        assert result["project_path"].endswith(".rpp")


class TestLoadProject:
    def test_refuses_to_hang_on_a_save_prompt_when_there_are_unsaved_changes(
        self, register, rpr, project, tmp_path
    ):
        rpr.IsProjectDirty.return_value = 1
        rpp = tmp_path / "other.rpp"
        rpp.write_text("")
        tools = register("project_tools", project)

        result = tools["load_project"](str(rpp))

        assert result["success"] is False
        assert result["unsaved_changes"] is True
        rpr.Main_openProject.assert_not_called()

    def test_opens_without_a_prompt_when_the_project_is_clean(self, register, rpr, project, tmp_path):
        rpr.IsProjectDirty.return_value = 0
        rpr.TimeMap_GetTimeSigAtTime.return_value = ("p", 0.0, 6, 8, 98.0)
        rpp = tmp_path / "other.rpp"
        rpp.write_text("")
        tools = register("project_tools", project)

        result = tools["load_project"](str(rpp))

        assert result["success"] is True
        assert result["time_signature"] == "6/8"
        rpr.Main_openProject.assert_called_once_with(f"noprompt:{rpp}")

    def test_discard_unsaved_opens_anyway(self, register, rpr, project, tmp_path):
        rpr.IsProjectDirty.return_value = 1
        rpr.TimeMap_GetTimeSigAtTime.return_value = ("p", 0.0, 4, 4, 98.0)
        rpp = tmp_path / "other.rpp"
        rpp.write_text("")
        tools = register("project_tools", project)

        assert tools["load_project"](str(rpp), discard_unsaved=True)["success"] is True
        rpr.Main_openProject.assert_called_once()

    def test_missing_file_is_reported(self, register, rpr, project, tmp_path):
        tools = register("project_tools", project)
        result = tools["load_project"](str(tmp_path / "nope.rpp"))
        assert result["success"] is False
        rpr.Main_openProject.assert_not_called()


# ---------------------------------------------------------------------------
# Markers and regions
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402


class TestProjectInfoMarkersAndRegions:
    def test_reports_the_regions_and_markers_that_exist(
        self, register, rpr, project, project_markers
    ):
        # Regression: get_project_info used to return "regions": [] for a project that
        # list_project_regions correctly showed regions for.
        project_markers([
            (False, 4.0, 4.0, "Drop", 1),
            (True, 8.0, 40.0, "Verse 1", 2),
            (True, 0.0, 8.0, "Intro", 1),
        ])
        info = register("project_tools", project)["get_project_info"]()
        assert info["regions"] == [
            {"index": 0, "name": "Intro", "start": 0.0, "end": 8.0},
            {"index": 1, "name": "Verse 1", "start": 8.0, "end": 40.0},
        ]
        assert info["markers"] == [{"index": 0, "number": 1, "name": "Drop", "position": 4.0}]

    def test_a_failing_read_is_reported_not_swallowed(self, register, rpr, project):
        rpr.GetNumRegionsOrMarkers.side_effect = RuntimeError("boom")
        info = register("project_tools", project)["get_project_info"]()
        assert info["success"] is False
        assert "boom" in info["error"]

    def test_names_come_from_the_per_marker_api_not_enum_project_markers(
        self, register, rpr, project, project_markers
    ):
        # Live REAPER 7.86 returns "" for every name from EnumProjectMarkers*; a
        # project whose ruler shows "Intro", "Verse 1" read as unnamed.
        project_markers([(True, 0.0, 16.0, "Intro", 1), (False, 0.0, 0.0, "Intro", 1)])
        info = register("project_tools", project)["get_project_info"]()
        assert info["regions"][0]["name"] == "Intro"
        assert info["markers"][0]["name"] == "Intro"
        rpr.GetSetRegionOrMarkerInfo_String.assert_any_call(
            project.id, "(ProjectMarker*)0x1", "P_NAME", "", False
        )
        rpr.EnumProjectMarkers2.assert_not_called()

    def test_without_the_per_marker_api_positions_still_read(
        self, register, rpr, project, project_markers
    ):
        project_markers([(True, 0.0, 16.0, "Intro", 1)])
        rpr.GetNumRegionsOrMarkers.side_effect = AttributeError("GetNumRegionsOrMarkers")
        info = register("project_tools", project)["get_project_info"]()
        assert info["regions"] == [{"index": 0, "name": "", "start": 0.0, "end": 16.0}]


def _item(position, length, take_name):
    item = MagicMock(name=f"item {take_name}")
    item.position, item.length = position, length
    item.active_take = MagicMock()
    item.active_take.name = take_name
    return item


@pytest.fixture
def song_parts(make_project):
    project = make_project(n_tracks=1)
    track = project.tracks[0]
    track.items = [_item(0.0, 8.0, "Intro"), _item(8.0, 32.0, "Verse 1")]
    track.n_items = 2
    return project


class TestCreateRegionsFromItems:
    def test_creates_one_named_region_per_item(self, register, rpr, song_parts, project_markers):
        project_markers([])
        rpr.AddRegionOrMarker.return_value = "(ProjectMarker*)0x2a"
        result = register("project_tools", song_parts)["create_regions_from_items"](0)
        assert [c["name"] for c in result["created"]] == ["Intro", "Verse 1"]
        pid = song_parts.id
        rpr.AddRegionOrMarker.assert_any_call(pid, True, 0.0, 8.0, "Intro", -1, 0)
        rpr.AddRegionOrMarker.assert_any_call(pid, True, 8.0, 40.0, "Verse 1", -1, 0)

    def test_name_prefix_is_applied(self, register, rpr, song_parts, project_markers):
        project_markers([])
        rpr.AddRegionOrMarker.return_value = "(ProjectMarker*)0x2a"
        result = register("project_tools", song_parts)["create_regions_from_items"](0, name_prefix="Song A - ")
        assert result["created"][0]["name"] == "Song A - Intro"

    def test_running_twice_does_not_stack_duplicates(self, register, rpr, song_parts, project_markers):
        project_markers([(True, 0.0, 8.0, "Intro", 1)])
        rpr.AddRegionOrMarker.return_value = "(ProjectMarker*)0x2a"
        result = register("project_tools", song_parts)["create_regions_from_items"](0)
        assert result["skipped_existing"] == ["Intro"]
        assert [c["name"] for c in result["created"]] == ["Verse 1"]
        assert rpr.AddRegionOrMarker.call_count == 1

    def test_a_null_pointer_means_reaper_refused(self, register, rpr, song_parts, project_markers):
        project_markers([])
        rpr.AddRegionOrMarker.return_value = "(ProjectMarker*)0x0000000000000000"
        result = register("project_tools", song_parts)["create_regions_from_items"](0)
        assert result["success"] is False
