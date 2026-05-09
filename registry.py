import json
import os
import threading
from datetime import datetime

REGISTRY_FILE = "bots_registry.json"


class RegistryManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"bots": {}}
        self._load()

    def _load(self):
        if os.path.exists(REGISTRY_FILE):
            with open(REGISTRY_FILE, encoding="utf-8") as f:
                self._data = json.load(f)

    def _save(self):
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def exists(self, name: str) -> bool:
        with self._lock:
            return name in self._data["bots"]

    def add_bot(self, name: str, path: str, entry_point: str,
                owner_id: int = 0, display_name: str = "",
                source: str = "zip", git_url: str = None, worker_id: str = None):
        with self._lock:
            self._data["bots"][name] = {
                "name": name,
                "display_name": display_name or name,
                "owner_id": owner_id,
                "path": path,
                "entry_point": entry_point,
                "source": source,
                "git_url": git_url,
                "worker_id": worker_id,
                "added_at": datetime.now().isoformat(timespec="seconds"),
                "status": "stopped",
                "pid": None,
                "provisioned": False,
            }
            self._save()

    def list_bots_by_worker(self, worker_id: str) -> list[dict]:
        with self._lock:
            return [b for b in self._data["bots"].values()
                    if b.get("worker_id") == worker_id]

    def update_bot(self, name: str, **kwargs):
        with self._lock:
            if name in self._data["bots"]:
                self._data["bots"][name].update(kwargs)
                self._save()

    def remove_bot(self, name: str):
        with self._lock:
            self._data["bots"].pop(name, None)
            self._save()

    def get_bot(self, name: str) -> dict | None:
        with self._lock:
            return self._data["bots"].get(name)

    def list_bots(self) -> list[dict]:
        with self._lock:
            return list(self._data["bots"].values())

    def list_bots_by_owner(self, owner_id: int) -> list[dict]:
        with self._lock:
            return [b for b in self._data["bots"].values()
                    if b.get("owner_id") == owner_id]

    def restore_running_bots(self):
        with self._lock:
            for bot in self._data["bots"].values():
                if bot.get("status") == "running":
                    bot["status"] = "stopped"
                    bot["pid"] = None
            self._save()
