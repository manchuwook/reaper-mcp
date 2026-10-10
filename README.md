# REAPER MCP Server

A Model Context Protocol (MCP) server that enables AI agents to control REAPER DAW — 65 tools covering project management, tracks, MIDI, FX, mixing, automation, mastering, rendering, and audio analysis.

## Requirements

- [REAPER](https://www.reaper.fm/) DAW installed and running
- Python 3.10+
- REAPER's distant API enabled (see [Setup](#setting-up-reaper))

## Installation

```bash
pip install reaper-mcp-server
```

Or install from source:

```bash
git clone https://github.com/bonfire-systems/reaper-mcp.git
cd reaper-mcp
pip install -e .
```

## Setting Up REAPER

The server communicates with REAPER via [python-reapy](https://github.com/RomeoDespres/reapy), which requires REAPER's distant API to be enabled.

1. Open REAPER
2. Go to Actions > Run ReaScript
3. Select `scripts/enable_reapy.py` from this repo (or create a new script with the contents below)
   ```python
   import reapy
   reapy.config.enable_dist_api()
   ```
4. Restart REAPER

## Usage

### With Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "reaper": {
      "command": "reaper-mcp-server",
      "args": []
    }
  }
}
```

### With Claude Code

```bash
claude mcp add reaper -- reaper-mcp-server
```

### Standalone

```bash
reaper-mcp-server          # start the server
reaper-mcp-server --debug  # with debug logging
```

## Tools (65)

### Project Management
`create_project` `load_project` `save_project` `get_project_info` `set_tempo` `set_time_signature` `set_cursor_position` `play_project` `stop_transport`

### Tracks
`create_track` `delete_track` `rename_track` `list_tracks` `get_track_info` `set_track_color` `set_track_folder_depth`

### Routing
`create_bus` `create_send` `remove_send` `list_sends` `set_send_volume`

### MIDI
`create_midi_item` `add_midi_note` `create_chord_progression` `create_drum_pattern`

### FX & Instruments
`add_fx` `remove_fx` `bypass_fx` `list_track_fx` `get_fx_parameters` `set_fx_parameter` `load_fx_preset` `add_master_fx` `list_master_fx` `set_master_fx_parameter`

### Audio
`import_audio_file` `edit_audio_item` `start_recording` `adjust_pitch` `adjust_playback_rate`

### Mixing
`set_track_volume` `set_track_pan` `set_track_mute` `set_track_solo` `set_master_volume`

### Automation
`add_volume_automation` `add_pan_automation` `set_track_automation_mode` `list_project_regions` `get_fx_param_envelope_info` `clear_fx_param_envelope` `set_fx_param_step_automation` `set_fx_param_step_automation_by_region`

### Rendering
`render_project` `render_stems` `render_time_selection`

### Mastering
`apply_mastering_chain` `apply_limiter` `normalize_project`

### Analysis
`analyze_loudness` `analyze_dynamics` `analyze_frequency_spectrum` `analyze_stereo_field` `analyze_transients` `detect_clipping`

## Behaviour worth knowing

- **Render settings are restored.** REAPER stores render settings in the project, so every render tool
  snapshots them, renders, and puts them back. During a render, normalisation and "add rendered files to
  project" are switched off. Existing output files are never overwritten unless you pass `overwrite=True`.
- **Stems use REAPER's own "Stems (selected tracks)" source**, so folders, sends and FX behave as they do in
  the Render dialog (User Guide 21.5). Track selection is restored afterwards.
- **`load_project` won't hang on a "Save changes?" prompt.** It refuses if the current project has unsaved
  changes, unless you call `save_project` first or pass `discard_unsaved=True`.
- **`create_bus` builds a submix by default** (the sources' direct master sends are turned off, User Guide
  5.12). Pass `submix=False` for an FX bus such as a shared reverb.
- **`create_send`** supports `send_mode` (`post_fader`, `pre_fx`, `post_fx`) and `midi_only=True` for sends
  that drive an instrument on another track.
- **Automation points need a playing automation mode.** Use `set_track_automation_mode` (`read`, `touch` or
  `latch`); in `write` mode REAPER records over the points the tools insert.
- **MIDI positions** (`start`, `start_position`) are seconds. `add_midi_note` start is relative to the item;
  chord and drum tools use absolute project time and compute beat lengths from the project tempo, so they
  assume a constant tempo.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests run without REAPER: `tests/conftest.py` swaps in a fake `reapy`, and each test asserts the exact
ReaScript calls a tool makes (the expected keys and values come from REAPER's API reference). When adding a
tool, prefer ReaScript calls over assigning to `reapy` objects: assignments to attributes that do not exist
(`track.armed`, `take.pitch`, ...) are silently accepted by Python and never reach REAPER.

## Configuration

The server stores its configuration in your platform's config directory:

- macOS: `~/Library/Application Support/reaper-mcp/config.json`
- Linux: `~/.config/reaper-mcp/config.json`
- Windows: `%APPDATA%\reaper-mcp\config.json`

## License

MIT
