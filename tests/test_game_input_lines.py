import importlib.util
from pathlib import Path

path = Path(__file__).parents[1] / "ci" / "game_input_lines.py"
spec = importlib.util.spec_from_file_location("game_input_lines", path)
assert spec and spec.loader
lines = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lines)

# captured from the rig game's stderr: libwayland prints '#' for ids, older builds '@'
ENTER_NEW = "[1226631.447] {Default Queue}  <- wl_keyboard#27.enter(412, wl_surface#41, array[0])"
KEY_NEW = "[1226700.100] {Default Queue}  <- wl_keyboard#27.key(413, 98313, 106, 1)"
KEY_OLD = "[1226700.200] wl_keyboard@27.key(414, 98400, 106, 0)"


def test_focus_is_seen_in_both_id_forms() -> None:
    assert lines.entered(ENTER_NEW)
    assert lines.entered(ENTER_NEW.replace("#", "@"))
    assert not lines.entered(KEY_NEW)


def test_key_events_are_read_in_both_id_forms() -> None:
    assert lines.key_event(KEY_NEW) == (106, 1)
    assert lines.key_event(KEY_OLD) == (106, 0)
    assert lines.key_event(ENTER_NEW) is None
