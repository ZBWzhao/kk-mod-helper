from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def load_catalog(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", {})
    return {key.casefold(): value for key, value in entries.items()}


def save_catalog(path: Path, entries: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now().isoformat(),
        "entries": dict(sorted(entries.items())),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)

def build_missing_report(
    game_dir: Path,
    requirements: dict[str, dict],
    installed: dict[str, dict],
    catalog: dict[str, dict],
    scan_errors: list[dict] | None = None,
) -> dict[str, Any]:
    items: list[dict] = []
    for key, requirement in sorted(requirements.items(), key=lambda pair: pair[1]["guid"].casefold()):
        if key in installed:
            continue
        match = catalog.get(key, {})
        downloadable = bool(match.get("url"))
        items.append(
            {
                "guid": requirement["guid"],
                "guid_type": "mod",
                "status": "downloadable" if downloadable else "not_found",
                "status_key": "downloadable" if downloadable else "not_found",
                "reason": "" if downloadable else "下载目录索引中尚未解析到该 GUID",
                "card_count": len(requirement["cards"]),
                "cards": requirement["cards"],
                "name": match.get("name") or requirement.get("name", ""),
                "author": match.get("author") or requirement.get("author", ""),
                "category": match.get("category", ""),
                "file_size": int(match.get("file_size") or 0),
                "filename": match.get("filename", ""),
                "url": match.get("url", ""),
            }
        )

    downloadable = sum(item["status"] == "downloadable" for item in items)
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "game_dir": str(game_dir),
        "summary": {
            "total": len(items),
            "downloadable": downloadable,
            "not_found": len(items) - downloadable,
            "non_downloadable": 0,
            "plugin": 0,
        },
        "items": items,
        "scan_errors": scan_errors or [],
    }


def save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
