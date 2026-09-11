import pytest

from bambu_bridge.db.starts import StartConflict
from bambu_bridge.service.command_guard import guard_passthrough


@pytest.mark.parametrize(
    "line",
    [
        "M23 part.gcode\nM24",
        "M32 part.gcode",
        "M98 P1",
        "G1 X1 M24",
        "G1 X1\nM24",
        "G1X1M24",
        "N1 M24",
        "M1002 foo",
        "G1 Xnan",
    ],
)
def test_sd_start_and_macro_escape_paths_rejected(line):
    with pytest.raises(StartConflict):
        guard_passthrough({"print": {"command": "gcode_line", "param": line}})


@pytest.mark.parametrize(
    "line",
    [
        "G91\nG1 Z10 F600\nG90",
        "G28 X Y",
        "M104 S220",
        "M106 P1 S128",
        "M83\nG1 E-5 F300",
        "M84",
        "G1 X1 ; M24 is a comment",
    ],
)
def test_typed_control_grammar_remains_available(line):
    guard_passthrough({"print": {"command": "gcode_line", "param": line}})


@pytest.mark.parametrize("command", ["gcode_file", "start", "unknown_firmware_command"])
def test_unmanaged_raw_print_commands_rejected(command):
    with pytest.raises(StartConflict):
        guard_passthrough({"print": {"command": command}})
