import copy
import json
import os
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def read(self, default: Any) -> Any:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (OSError, TypeError, json.JSONDecodeError):
            return copy.deepcopy(default)

    def write(self, value: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, self.path)
