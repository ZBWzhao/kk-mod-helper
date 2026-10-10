from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from .catalog import USER_AGENT, safe_remote_url
from .manifests import read_zipmod_manifest, scan_installed_mods


def _safe_component(value: str, fallback: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value).strip(" .")
    if cleaned in {"", ".", ".."}:
        cleaned = fallback
    return cleaned[:180]


def _download_one(
    item: dict,
    target_root: Path,
    progress: Callable[[str], None],
    stop_event: threading.Event,
    task_callback: Callable[[dict], None],
    retries: int = 3,
) -> dict:
    guid = str(item.get("guid") or "").strip()
    url = safe_remote_url(str(item.get("url") or ""))
    filename = _safe_component(str(item.get("filename") or Path(url).name), "unknown.zipmod")
    if not filename.casefold().endswith(".zipmod"):
        raise ValueError("目标文件不是 .zipmod")
    category = _safe_component(str(item.get("category") or "未分类"), "未分类")
    target_dir = target_root / category
    target = target_dir / filename
    partial = target.with_suffix(target.suffix + ".partial")

    if target.exists():
        try:
            manifest = read_zipmod_manifest(target)
            if manifest["guid"].casefold() == guid.casefold():
                task_callback({"guid": guid, "status": "skipped", "reason": "已安装", "path": str(target)})
                return {"guid": guid, "status": "skipped", "path": str(target), "reason": "已安装"}
        except Exception:
            task_callback(
                {"guid": guid, "status": "skipped", "reason": "同名文件已存在，未覆盖", "path": str(target)}
            )
            return {"guid": guid, "status": "skipped", "path": str(target), "reason": "同名文件已存在，未覆盖"}

    target_dir.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        if stop_event.is_set():
            task_callback({"guid": guid, "status": "cancelled", "reason": "用户取消"})
            return {"guid": guid, "status": "cancelled", "path": str(target)}
        try:
            existing = partial.stat().st_size if partial.exists() else 0
            task_callback(
                {
                    "guid": guid,
                    "status": "downloading" if attempt == 1 else "retrying",
                    "attempt": attempt,
                    "downloaded": existing,
                    "total": int(item.get("file_size") or 0),
                }
            )
            headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            with urlopen(Request(url, headers=headers), timeout=60) as response:
                append = existing > 0 and response.status == 206
                if existing and not append:
                    existing = 0
                mode = "ab" if append else "wb"
                with partial.open(mode) as output:
                    downloaded = existing
                    last_update = time.monotonic()
                    while True:
                        if stop_event.is_set():
                            task_callback({"guid": guid, "status": "cancelled", "reason": "用户取消"})
                            return {"guid": guid, "status": "cancelled", "path": str(target)}
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_update >= 0.5:
                            task_callback(
                                {
                                    "guid": guid,
                                    "status": "downloading",
                                    "attempt": attempt,
                                    "downloaded": downloaded,
                                    "total": int(item.get("file_size") or 0),
                                }
                            )
                            last_update = now
            expected_size = int(item.get("file_size") or 0)
            actual_size = partial.stat().st_size
            if expected_size and actual_size != expected_size:
                raise OSError(f"文件大小不匹配：期望 {expected_size}，实际 {actual_size}")
            manifest = read_zipmod_manifest(partial)
            if manifest["guid"].casefold() != guid.casefold():
                raise ValueError(f"GUID 校验失败：下载到 {manifest['guid']}，期望 {guid}")
            os.replace(partial, target)
            progress(f"安装完成：{filename}")
            result = {
                "guid": guid,
                "status": "installed",
                "path": str(target),
                "bytes": actual_size,
                "version": manifest.get("version", ""),
            }
            task_callback(result)
            return result
        except Exception as exc:
            last_error = exc
            progress(f"第 {attempt}/{retries} 次失败：{filename}：{exc}")
            task_callback(
                {
                    "guid": guid,
                    "status": "retrying" if attempt < retries else "failed",
                    "attempt": attempt,
                    "reason": str(exc),
                }
            )
            if attempt < retries:
                stop_event.wait(min(2**attempt, 8))

    return {"guid": guid, "status": "failed", "path": str(target), "reason": str(last_error)}


def install_report(
    report_path: Path,
    game_dir: Path,
    workers: int = 3,
    progress: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    selected_guids: set[str] | None = None,
    task_callback: Callable[[dict], None] | None = None,
) -> dict:
    progress = progress or (lambda _message: None)
    stop_event = stop_event or threading.Event()
    task_callback = task_callback or (lambda _event: None)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    items = [
        item
        for item in report.get("items", [])
        if item.get("status") == "downloadable" and item.get("url") and item.get("filename")
    ]
    if selected_guids is not None:
        selected_keys = {guid.casefold() for guid in selected_guids}
        items = [item for item in items if str(item.get("guid") or "").casefold() in selected_keys]
    if not items:
        raise ValueError("报告中没有带下载地址的可下载项目")

    installed, _manifest_errors = scan_installed_mods(game_dir / "mods")
    results: list[dict] = []
    pending: list[dict] = []
    for item in items:
        key = str(item.get("guid") or "").casefold()
        if key in installed:
            result = {
                "guid": item.get("guid", ""),
                "status": "skipped",
                "path": installed[key].get("path", ""),
                "reason": "当前 mods 目录已经满足该 GUID",
            }
            results.append(result)
            task_callback(result)
        else:
            pending.append(item)
            task_callback(
                {
                    "guid": item.get("guid", ""),
                    "status": "queued",
                    "downloaded": 0,
                    "total": int(item.get("file_size") or 0),
                }
            )

    target_root = game_dir / "mods" / "ModResolver_Downloads"
    total_size = sum(int(item.get("file_size") or 0) for item in pending)
    free_size = shutil.disk_usage(game_dir).free
    if total_size and free_size < total_size + 1024**3:
        raise OSError("目标磁盘可用空间不足：需要下载总量外加至少 1 GB 安全余量")

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as executor:
        futures = {
            executor.submit(
                _download_one,
                item,
                target_root,
                progress,
                stop_event,
                task_callback,
            ): item
            for item in pending
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                item = futures[future]
                result = {"guid": item.get("guid", ""), "status": "failed", "reason": str(exc)}
                results.append(result)
                task_callback(result)

    output = report_path.with_name(report_path.stem + "_install_results.json")
    result_map: dict[str, dict] = {}
    if output.exists():
        try:
            previous = json.loads(output.read_text(encoding="utf-8"))
            for previous_result in previous.get("results", []):
                key = str(previous_result.get("guid") or "").casefold()
                if key:
                    result_map[key] = previous_result
        except (OSError, ValueError, TypeError):
            pass
    for result in results:
        key = str(result.get("guid") or "").casefold()
        if key:
            result_map[key] = result
    merged_results = list(result_map.values())
    summary = {
        "installed": sum(result["status"] == "installed" for result in merged_results),
        "skipped": sum(result["status"] == "skipped" for result in merged_results),
        "failed": sum(result["status"] == "failed" for result in merged_results),
        "cancelled": sum(result["status"] == "cancelled" for result in merged_results),
    }
    install_report_data = {
        "generated_at": datetime.now().isoformat(),
        "source_report": str(report_path),
        "target_root": str(target_root),
        "summary": summary,
        "results": merged_results,
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(install_report_data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, output)
    return install_report_data
