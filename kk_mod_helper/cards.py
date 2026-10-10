from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import msgpack


UAR_GUID = "com.bepis.sideloader.universalautoresolver"
UAR_MARKER = UAR_GUID.encode("utf-8")
UNCENSOR_GUID = "com.deathweasel.bepinex.uncensorselector"
UNCENSOR_MARKER = UNCENSOR_GUID.encode("utf-8")


def _decode_nested(value: Any, found: dict[str, dict], depth: int = 0) -> None:
    if depth > 8:
        return

    if isinstance(value, bytes):
        if len(value) > 16 * 1024 * 1024:
            return
        try:
            decoded = msgpack.unpackb(value, raw=False, strict_map_key=False)
        except (ValueError, TypeError, msgpack.ExtraData, msgpack.FormatError, msgpack.StackError):
            return
        _decode_nested(decoded, found, depth + 1)
        return

    if isinstance(value, dict):
        normalized = {
            (key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)): item
            for key, item in value.items()
        }
        mod_id = normalized.get("ModID") or normalized.get("modID") or normalized.get("modId")
        if isinstance(mod_id, bytes):
            mod_id = mod_id.decode("utf-8", errors="replace")
        if isinstance(mod_id, str) and mod_id.strip():
            guid = mod_id.strip()
            record = found.setdefault(guid.casefold(), {"guid": guid, "name": "", "author": "", "website": ""})
            for source_key, target_key in (("Name", "name"), ("Author", "author"), ("Website", "website")):
                item = normalized.get(source_key)
                if isinstance(item, bytes):
                    item = item.decode("utf-8", errors="replace")
                if isinstance(item, str) and item.strip() and not record[target_key]:
                    record[target_key] = item.strip()
        for item in normalized.values():
            _decode_nested(item, found, depth + 1)
        return

    if isinstance(value, (list, tuple)):
        for item in value:
            _decode_nested(item, found, depth + 1)


def _decode_uncensor(value: Any, found: dict[str, dict], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, bytes):
        try:
            _decode_uncensor(msgpack.unpackb(value, raw=False, strict_map_key=False), found, depth + 1)
        except (ValueError, TypeError, msgpack.ExtraData, msgpack.FormatError, msgpack.StackError):
            pass
        return
    if isinstance(value, dict):
        for key, item in value.items():
            name = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
            if name.casefold() in {"bodyguid", "penisguid", "ballsguid"}:
                if isinstance(item, bytes):
                    item = item.decode("utf-8", errors="replace")
                if isinstance(item, str) and item.strip():
                    guid = item.strip()
                    found.setdefault(guid.casefold(), {"guid": guid, "name": "", "author": "", "website": ""})
            _decode_uncensor(item, found, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _decode_uncensor(item, found, depth + 1)


def _unpack_values_after_marker(data: bytes, marker: bytes):
    start = 0
    while True:
        marker_at = data.find(marker, start)
        if marker_at < 0:
            return
        value_at = marker_at + len(marker)
        start = value_at
        try:
            unpacker = msgpack.Unpacker(raw=False, strict_map_key=False, max_buffer_size=64 * 1024 * 1024)
            limit = min(len(data), value_at + 64 * 1024 * 1024)
            cursor = value_at
            while cursor < limit:
                end = min(limit, cursor + 1024 * 1024)
                unpacker.feed(memoryview(data)[cursor:end])
                cursor = end
                try:
                    yield unpacker.unpack()
                    break
                except msgpack.OutOfData:
                    continue
        except (
            ValueError,
            TypeError,
            msgpack.OutOfData,
            msgpack.BufferFull,
            msgpack.FormatError,
            msgpack.StackError,
        ):
            continue


def extract_zipmod_requirements(path: Path) -> dict[str, dict]:
    """Extract UAR ModID records from a KK/KKS card, coordinate, or scene PNG."""
    data = path.read_bytes()
    found: dict[str, dict] = {}

    for value in _unpack_values_after_marker(data, UAR_MARKER):
        _decode_nested(value, found)
    for value in _unpack_values_after_marker(data, UNCENSOR_MARKER):
        _decode_uncensor(value, found)

    return found


def _card_type(path: Path, user_data: Path) -> str:
    try:
        relative = path.relative_to(user_data)
    except ValueError:
        return "unknown"
    parts = [part.casefold() for part in relative.parts]
    if "scene" in parts or "studio" in parts:
        return "scene"
    if "coordinate" in parts:
        return "coordinate"
    if "chara" in parts:
        return "chara"
    return "other"


def scan_user_data(
    user_data: Path,
    cache_path: Path | None = None,
    force: bool = False,
) -> tuple[dict[str, dict], list[dict]]:
    requirements: dict[str, dict] = {}
    errors: list[dict] = []
    old_cache: dict[str, Any] = {}
    if cache_path and cache_path.exists() and not force:
        try:
            old_cache = json.loads(cache_path.read_text(encoding="utf-8")).get("files", {})
        except (OSError, ValueError, TypeError):
            old_cache = {}
    new_cache: dict[str, Any] = {}

    for root, dirs, files in os.walk(user_data, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for filename in files:
            if not filename.lower().endswith(".png"):
                continue
            path = Path(root) / filename
            try:
                stat = path.stat()
                key_path = str(path.resolve())
                fingerprint = [stat.st_size, stat.st_mtime_ns]
                cached = old_cache.get(key_path)
                if cached and cached.get("fingerprint") == fingerprint:
                    mods = {
                        key: value for key, value in cached.get("mods", {}).items() if isinstance(value, dict)
                    }
                else:
                    mods = extract_zipmod_requirements(path)
                new_cache[key_path] = {"fingerprint": fingerprint, "mods": mods}
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue

            for key, metadata in mods.items():
                record = requirements.setdefault(
                    key,
                    {
                        "guid": metadata["guid"],
                        "name": metadata.get("name", ""),
                        "author": metadata.get("author", ""),
                        "website": metadata.get("website", ""),
                        "cards": [],
                    },
                )
                for field in ("name", "author", "website"):
                    if not record[field] and metadata.get(field):
                        record[field] = metadata[field]
                record["cards"].append(
                    {
                        "path": str(path),
                        "filename": filename,
                        "card_type": _card_type(path, user_data),
                    }
                )

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "files": new_cache}, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, cache_path)

    return requirements, errors
