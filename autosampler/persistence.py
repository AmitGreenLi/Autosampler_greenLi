from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from autosampler.errors import AutosamplerError
from autosampler.vessels.inventory import Inventory


class StateStore:
    VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, inventory: Inventory) -> None:
        payload = {"version": self.VERSION, "vessels": inventory.to_list()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise AutosamplerError(f"Could not save state: {exc}") from exc

    def load(self) -> Inventory:
        if not self.path.exists():
            return Inventory()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != self.VERSION:
                raise ValueError("unsupported state version")
            return Inventory.from_list(payload["vessels"])
        except Exception as exc:
            raise AutosamplerError(f"Could not load state file {self.path}: {exc}") from exc

    def reset(self) -> Inventory:
        # Reset is explicit and caller-confirmed. Preserve a recoverable backup.
        if self.path.exists():
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            os.replace(self.path, backup)
        return Inventory()

