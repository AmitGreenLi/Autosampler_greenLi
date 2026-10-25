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
        payload = {"version": self.VERSION, "vessels": inventory.to_list(),
                   "unverified_sources": sorted(inventory.unverified_sources),
                   "dump_required": sorted(inventory.dump_required),
                   "pipette_contents_ul": inventory.pipette_contents_ul}
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
            inventory = Inventory.from_list(payload["vessels"])
            inventory.unverified_sources = set(payload.get("unverified_sources", []))
            inventory.dump_required = set(payload.get("dump_required", []))
            contents = payload.get("pipette_contents_ul", {"P1": 0.0, "P2": 0.0})
            import math
            if set(contents) != {"P1", "P2"} or any(not math.isfinite(v) or v < 0 for v in contents.values()):
                raise ValueError("invalid pipette contents")
            inventory.pipette_contents_ul = contents
            return inventory
        except Exception as exc:
            raise AutosamplerError(f"Could not load state file {self.path}: {exc}") from exc

    def reset(self) -> Inventory:
        # Reset is explicit and caller-confirmed. Preserve a recoverable backup.
        if self.path.exists():
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            os.replace(self.path, backup)
        return Inventory()

