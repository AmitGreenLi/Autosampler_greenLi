from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApplicationPaths:
    """All writable runtime locations, independent of the install directory/CWD."""

    data_dir: Path
    config_dir: Path
    log_dir: Path

    @classmethod
    def platform_default(cls) -> "ApplicationPaths":
        from platformdirs import user_config_path, user_data_path, user_log_path

        app = "DIYCNCAutosampler"
        author = "DIYCNCAutosampler"
        return cls(
            Path(user_data_path(app, author)),
            Path(user_config_path(app, author)),
            Path(user_log_path(app, author)),
        )

    @classmethod
    def under(cls, root: str | Path) -> "ApplicationPaths":
        root = Path(root)
        return cls(root / "data", root / "config", root / "logs")

    @property
    def session_state(self) -> Path:
        return self.data_dir / "session_state.json"

    @property
    def device_preferences(self) -> Path:
        return self.config_dir / "devices.json"

    @property
    def user_preferences(self) -> Path:
        return self.config_dir / "preferences.json"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "autosampler.log"

    def ensure(self) -> None:
        for directory in (self.data_dir, self.config_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def migrate_legacy_session(self, legacy_path: str | Path) -> bool:
        """Copy, never delete, the old source-directory session file once."""
        legacy = Path(legacy_path)
        if self.session_state.exists() or not legacy.is_file():
            return False
        self.ensure()
        shutil.copy2(legacy, self.session_state)
        return True
