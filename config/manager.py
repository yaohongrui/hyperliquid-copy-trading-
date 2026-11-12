import os
import tempfile
from pathlib import Path
import yaml


class ConfigManager:
    def __init__(self, path):
        self.path = Path(path)

    def update_leader(self, name, changes):
        data = yaml.safe_load(self.path.read_text()) or {}
        for leader in data.get("leaders", []):
            if leader.get("name") == name:
                leader.update(changes)
                break
        else:
            raise KeyError(name)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", text=True)
        try:
            with os.fdopen(fd, "w") as handle:
                yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
