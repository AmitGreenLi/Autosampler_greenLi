from pathlib import Path

from autosampler.app_paths import ApplicationPaths


def test_application_paths_are_centralized_under_requested_root(tmp_path):
    paths = ApplicationPaths.under(tmp_path)
    paths.ensure()
    assert paths.session_state.parent == tmp_path / "data"
    assert paths.device_preferences.parent == tmp_path / "config"
    assert paths.log_file.parent == tmp_path / "logs"
    assert all(path.is_dir() for path in (paths.data_dir, paths.config_dir, paths.log_dir))


def test_legacy_session_is_copied_not_discarded(tmp_path):
    paths = ApplicationPaths.under(tmp_path / "new")
    legacy = tmp_path / "autosampler_state.json"
    legacy.write_text('{"version": 1, "vessels": []}', encoding="utf-8")
    assert paths.migrate_legacy_session(legacy)
    assert paths.session_state.read_text(encoding="utf-8") == legacy.read_text(encoding="utf-8")
    assert legacy.exists()
    assert not paths.migrate_legacy_session(legacy)


def test_core_source_contains_no_tkinter_or_windows_absolute_path():
    source_root = Path(__file__).resolve().parents[1] / "autosampler"
    contents = "\n".join(path.read_text(encoding="utf-8") for path in source_root.rglob("*.py"))
    assert "tkinter" not in contents.lower()
    assert "C:\\" not in contents

