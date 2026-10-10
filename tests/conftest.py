"""Shared fixtures: a fake ``reapy`` so tool modules can be tested without REAPER.

The tools talk to REAPER through ``reapy.reascript_api`` (``RPR``) and a few
``reapy`` objects. Both are replaced here:

* ``RPR`` is one module-wide ``MagicMock`` that every tool module imports; the
  ``rpr`` fixture resets it between tests so each test configures only what it
  needs and then asserts on the exact ReaScript calls made.
* ``make_project`` builds a fake ``reapy.Project`` with real Python lists for
  its tracks, so index-based tool code behaves as it does against REAPER.
* ``project_settings`` backs ``GetSetProjectInfo`` / ``GetSetProjectInfo_String``
  with dicts, which lets render tests check both the settings in force *while*
  a render runs and that the user's settings are restored afterwards.

Tests are written Given / When / Then; the expected ReaScript calls come from
REAPER's own API documentation.
"""

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest

_rpr = MagicMock(name="RPR")
_reapy = types.ModuleType("reapy")
_reapy.reascript_api = _rpr
_reapy.Project = MagicMock(name="Project")
_reapy.connect = lambda: None
sys.modules["reapy"] = _reapy
sys.modules["reapy.reascript_api"] = _rpr


class FakeMCP:
    """Stands in for the MCP server: just records each registered tool function."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture(autouse=True)
def rpr():
    _rpr.reset_mock(return_value=True, side_effect=True)
    return _rpr


@pytest.fixture
def register(monkeypatch):
    """register("module_name", project=None) -> {tool_name: function}."""

    def _register(module_name, project=None):
        module = importlib.import_module(f"reaper_mcp.{module_name}")
        fake = FakeMCP()
        module.register_tools(fake)
        if project is not None:
            monkeypatch.setattr(module, "get_project", lambda: project)
        return fake.tools

    return _register


@pytest.fixture
def make_project():
    def _make(n_tracks=3, **attrs):
        project = MagicMock(name="project")
        project.id = "(ReaProject*)0x1"
        tracks = []

        def new_track(name):
            track = MagicMock(name=f"track{len(tracks)}")
            track.id = f"(MediaTrack*)0x{len(tracks) + 16:x}"
            track.name = name
            tracks.append(track)
            return track

        for i in range(n_tracks):
            new_track(f"Track {i}")

        def add_track(index=0, name=""):
            new_track(name)
            project.n_tracks = len(tracks)

        project.tracks = tracks
        project.n_tracks = n_tracks
        project.add_track.side_effect = add_track
        for key, value in attrs.items():
            setattr(project, key, value)
        return project

    return _make


@pytest.fixture
def project_settings(rpr):
    """Back the project render settings with dicts; returns (numeric, strings)."""
    numeric, strings = {}, {}

    def get_set_info(proj, key, value, is_set):
        if is_set:
            numeric[key] = value
        return numeric.get(key, 0.0)

    def get_set_info_string(proj, key, value, is_set):
        if is_set:
            strings[key] = value
        return (True, proj, key, strings.get(key, ""), is_set)

    rpr.GetSetProjectInfo.side_effect = get_set_info
    rpr.GetSetProjectInfo_String.side_effect = get_set_info_string
    return numeric, strings


@pytest.fixture
def track_info(rpr):
    """Back Get/SetMediaTrackInfo_Value with a dict keyed by (track_id, key)."""
    store = {}
    rpr.GetMediaTrackInfo_Value.side_effect = lambda tid, key: store.get((tid, key), 0.0)

    def set_value(tid, key, value):
        store[(tid, key)] = value
        return True

    rpr.SetMediaTrackInfo_Value.side_effect = set_value
    return store
