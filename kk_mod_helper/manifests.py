from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def read_zipmod_manifest(path: Path) -> dict[str, Any]:
    """Read the root manifest.xml from a local zipmod without extracting it."""
    with zipfile.ZipFile(path, "r") as archive:
        manifest_name = next(
            (name for name in archive.namelist() if name.replace("\\", "/").lower() == "manifest.xml"),
            None,
        )
        if manifest_name is None:
            raise ValueError("manifest.xml 不存在")
        raw = archive.read(manifest_name)

    root = ElementTree.fromstring(raw.decode("utf-8-sig", errors="replace"))

    def text(name: str) -> str:
        element = next((item for item in root.iter() if _tag_name(item.tag) == name.lower()), None)
        return (element.text or "").strip() if element is not None else ""

    guid = text("guid")
    if not guid:
        raise ValueError("manifest.xml 缺少 GUID")

    old_guids: set[str] = set()
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.lower() in {"guidold", "oldguid"} and value.strip():
                old_guids.add(value.strip())

    return {
        "guid": guid,
        "name": text("name"),
        "version": text("version"),
        "author": text("author"),
        "old_guids": sorted(old_guids, key=str.casefold),
    }


def _iter_zipmods(mods_dir: Path):
    for root, dirs, files in os.walk(mods_dir, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for filename in files:
            if filename.lower().endswith(".zipmod"):
                yield Path(root) / filename


def scan_installed_mods(
    mods_dir: Path,
    cache_path: Path | None = None,
    force: bool = False,
) -> tuple[dict[str, dict], list[dict]]:
    """Return installed GUIDs (case-folded) and non-fatal parse errors."""
    old_cache: dict[str, Any] = {}
    if cache_path and cache_path.exists() and not force:
        try:
            old_cache = json.loads(cache_path.read_text(encoding="utf-8")).get("files", {})
        except (OSError, ValueError, TypeError):
            old_cache = {}

    new_cache: dict[str, Any] = {}
    installed: dict[str, dict] = {}
    errors: list[dict] = []

    for path in _iter_zipmods(mods_dir):
        try:
            stat = path.stat()
            key = str(path.resolve())
            fingerprint = [stat.st_size, stat.st_mtime_ns]
            cached = old_cache.get(key)
            if cached and cached.get("fingerprint") == fingerprint:
                manifest = cached["manifest"]
            else:
                manifest = read_zipmod_manifest(path)
            new_cache[key] = {"fingerprint": fingerprint, "manifest": manifest}

            record = {**manifest, "path": str(path), "file_size": stat.st_size}
            installed[manifest["guid"].casefold()] = record
            for old_guid in manifest.get("old_guids", []):
                installed.setdefault(old_guid.casefold(), {**record, "satisfied_by_migration": True})
        except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            errors.append({"path": str(path), "error": str(exc)})

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "files": new_cache}, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, cache_path)

    return installed, errors
