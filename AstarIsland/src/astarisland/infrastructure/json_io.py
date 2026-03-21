from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import orjson
except ModuleNotFoundError:  # pragma: no cover - fallback only used before deps are installed
    orjson = None


def dumps_json(payload: Any, *, indent: bool = True) -> bytes:
    if orjson is not None:
        option = orjson.OPT_INDENT_2 if indent else 0
        return orjson.dumps(payload, option=option)
    encoded = json.dumps(payload, indent=2 if indent else None, sort_keys=False).encode("utf-8")
    return encoded


def dump_json_file(path: Path, payload: Any, *, indent: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps_json(payload, indent=indent) + b"\n")


def load_json_file(path: Path) -> Any:
    data = path.read_bytes()
    if orjson is not None:
        return orjson.loads(data)
    return json.loads(data.decode("utf-8"))

