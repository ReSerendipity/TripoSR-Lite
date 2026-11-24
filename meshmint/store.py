"""In-memory mesh store shared by jobs and the demo path."""

from __future__ import annotations

import threading

from .meshes import MeshRecord


class MeshStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, MeshRecord] = {}

    def put(self, record: MeshRecord) -> MeshRecord:
        with self._lock:
            self._items[record.id] = record
        return record

    def get(self, mesh_id: str) -> MeshRecord | None:
        with self._lock:
            return self._items.get(mesh_id)

    def drop(self, mesh_id: str) -> None:
        with self._lock:
            self._items.pop(mesh_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
