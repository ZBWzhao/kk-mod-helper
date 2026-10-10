from __future__ import annotations

import json
import os
import re
import struct
import time
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .reporting import save_catalog


DEFAULT_BASE_URL = "https://sideload.betterrepack.com/download/KKEC/"
USER_AGENT = "KK-Mod-Helper/0.1 (+personal interoperability tool)"
ALLOWED_HOST = "sideload.betterrepack.com"
ALLOWED_PATH_PREFIX = "/download/KKEC/"


def safe_remote_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError(f"不允许的下载主机：{url}")
    decoded_path = unquote(parsed.path)
    if not decoded_path.startswith(ALLOWED_PATH_PREFIX) or not decoded_path.lower().endswith(".zipmod"):
        raise ValueError(f"不允许的下载路径：{url}")
    return quote(url, safe=":/%?=&")


def load_url_cache(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    urls = data.get("urls")
    if not isinstance(urls, list):
        raise ValueError("URL 缓存缺少 urls 数组")
    return [item for item in urls if isinstance(item, dict) and item.get("url") and item.get("filename")]


def bootstrap_catalog_from_report(
    report_path: Path,
    url_cache_path: Path,
    catalog_path: Path,
) -> tuple[int, int]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    url_items = load_url_cache(url_cache_path)
    existing = {}
    if catalog_path.exists():
        existing = json.loads(catalog_path.read_text(encoding="utf-8")).get("entries", {})
    entries = {key.casefold(): value for key, value in existing.items()}

    by_filename: dict[str, list[dict]] = {}
    for item in url_items:
        by_filename.setdefault(item["filename"].casefold(), []).append(item)

    added = 0
    unresolved = 0
    for item in report.get("items", []):
        guid = str(item.get("guid") or "").strip()
        filename = str(item.get("filename") or "").strip()
        if not guid or not filename:
            unresolved += 1
            continue
        candidates = by_filename.get(filename.casefold(), [])
        category = str(item.get("category") or "")
        selected = next((entry for entry in candidates if entry.get("category") == category), None)
        if selected is None and len(candidates) == 1:
            selected = candidates[0]
        if selected is None:
            unresolved += 1
            continue
        key = guid.casefold()
        entries[key] = {
            "guid": guid,
            "name": item.get("name", ""),
            "author": item.get("author", ""),
            "filename": filename,
            "category": selected.get("category", category),
            "file_size": int(item.get("file_size") or 0),
            "url": selected["url"],
            "source": "report+public-url-index",
        }
        added += 1

    save_catalog(catalog_path, entries)
    return added, unresolved


def _request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, timeout: int = 8):
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if headers:
        request_headers.update(headers)
    return urlopen(Request(url, method=method, headers=request_headers), timeout=timeout)


def _range_get(url: str, start: int, end: int) -> bytes:
    with _request(url, headers={"Range": f"bytes={start}-{end}"}) as response:
        if response.status not in (200, 206):
            raise OSError(f"Range 请求失败：HTTP {response.status}")
        if response.status == 200:
            if start != 0:
                raise OSError("服务器忽略 Range 请求；为避免意外下载整个大文件，已停止远程探测")
            return response.read(end + 1)[: end + 1]
        return response.read()


def _suffix_get(url: str, length: int) -> tuple[bytes, int, int]:
    """Return suffix bytes, total file size, and suffix start from one Range request."""
    with _request(url, headers={"Range": f"bytes=-{length}"}) as response:
        if response.status != 206:
            raise OSError("服务器忽略尾部 Range 请求；为避免意外下载整个大文件，已停止远程探测")
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", content_range.strip())
        if not match:
            raise OSError("服务器返回的 Content-Range 无效")
        start, _end, total = (int(value) for value in match.groups())
        return response.read(), total, start


def read_remote_manifest(url: str) -> dict[str, Any]:
    """Fetch only ZIP metadata and manifest.xml through HTTP Range requests."""
    url = safe_remote_url(url)
    tail, size, tail_start = _suffix_get(url, 256 * 1024)
    eocd_at = tail.rfind(b"PK\x05\x06")
    if eocd_at < 0 or eocd_at + 22 > len(tail):
        raise ValueError("ZIP EOCD 不存在或为不支持的 ZIP64 文件")
    eocd = struct.unpack_from("<4s4H2LH", tail, eocd_at)
    central_size, central_offset = eocd[5], eocd[6]
    if central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        raise ValueError("暂不支持 ZIP64 中央目录")

    if central_offset >= tail_start and central_offset + central_size <= size:
        begin = central_offset - tail_start
        central = tail[begin : begin + central_size]
    else:
        central = _range_get(url, central_offset, central_offset + central_size - 1)

    position = 0
    selected = None
    while position + 46 <= len(central) and central[position : position + 4] == b"PK\x01\x02":
        values = struct.unpack_from("<4s6H3I5H2I", central, position)
        flags, method = values[3], values[4]
        compressed_size, uncompressed_size = values[8], values[9]
        name_length, extra_length, comment_length = values[10], values[11], values[12]
        local_offset = values[16]
        name_bytes = central[position + 46 : position + 46 + name_length]
        encoding = "utf-8" if flags & 0x800 else "cp437"
        name = name_bytes.decode(encoding, errors="replace").replace("\\", "/")
        if name.casefold() == "manifest.xml":
            selected = (flags, method, compressed_size, uncompressed_size, local_offset)
            break
        position += 46 + name_length + extra_length + comment_length

    if selected is None:
        raise ValueError("远程 zipmod 缺少根目录 manifest.xml")
    flags, method, compressed_size, _uncompressed_size, local_offset = selected
    if flags & 1:
        raise ValueError("不支持加密的 zipmod")

    prefix = b""
    if local_offset < 64 * 1024:
        prefix = _range_get(url, 0, min(size - 1, 64 * 1024 - 1))
        local_header = prefix[local_offset:]
    else:
        local_header = _range_get(url, local_offset, min(size - 1, local_offset + 4095))
    if local_header[:4] != b"PK\x03\x04":
        raise ValueError("ZIP 本地文件头无效")
    local_values = struct.unpack_from("<4s5H3I2H", local_header, 0)
    name_length, extra_length = local_values[9], local_values[10]
    data_offset = local_offset + 30 + name_length + extra_length
    if prefix and data_offset + compressed_size <= len(prefix):
        compressed = prefix[data_offset : data_offset + compressed_size]
    else:
        compressed = _range_get(url, data_offset, data_offset + compressed_size - 1)
    if method == 0:
        raw = compressed
    elif method == 8:
        raw = zlib.decompress(compressed, -zlib.MAX_WBITS)
    else:
        raise ValueError(f"manifest.xml 使用了不支持的压缩方法：{method}")

    root = ElementTree.fromstring(raw.decode("utf-8-sig", errors="replace"))

    def text(name: str) -> str:
        element = next(
            (item for item in root.iter() if item.tag.rsplit("}", 1)[-1].casefold() == name.casefold()),
            None,
        )
        return (element.text or "").strip() if element is not None else ""

    guid = text("guid")
    if not guid:
        raise ValueError("远程 manifest.xml 缺少 GUID")
    return {
        "guid": guid,
        "name": text("name"),
        "version": text("version"),
        "author": text("author"),
        "file_size": size,
    }


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9\u0080-\uffff]+", text.casefold()))


def _candidate_score(item: dict, url_item: dict) -> float:
    filename = _normalize(Path(url_item.get("filename", "")).stem)
    needles = [
        _normalize(str(item.get("guid") or "")),
        _normalize(str(item.get("name") or "")),
        _normalize(str(item.get("author") or "")),
    ]
    scores = [SequenceMatcher(None, needle, filename).ratio() for needle in needles if needle]
    for needle in needles:
        if needle and needle in filename:
            scores.append(0.95)
    return max(scores, default=0.0)


def resolve_report(
    report_path: Path,
    url_cache_path: Path,
    catalog_path: Path,
    output_path: Path,
    probe_limit: int = 0,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    probe_workers: int = 8,
    max_remote_probes: int = 240,
) -> dict:
    progress = progress or (lambda _message: None)
    cancelled = cancelled or (lambda: False)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    url_items = load_url_cache(url_cache_path)
    entries: dict[str, dict] = {}
    if catalog_path.exists():
        entries = {
            key.casefold(): value
            for key, value in json.loads(catalog_path.read_text(encoding="utf-8")).get("entries", {}).items()
        }

    by_filename: dict[str, list[dict]] = {}
    for url_item in url_items:
        by_filename.setdefault(url_item["filename"].casefold(), []).append(url_item)

    was_cancelled = False
    unresolved_items: list[dict] = []
    for item in report.get("items", []):
        if cancelled():
            was_cancelled = True
            progress("已收到取消请求，停止解析并保存当前结果。")
            break
        guid = str(item.get("guid") or "").strip()
        if not guid:
            continue
        key = guid.casefold()
        match = entries.get(key)

        if match is None and item.get("filename"):
            candidates = by_filename.get(str(item["filename"]).casefold(), [])
            category = item.get("category")
            selected = next((entry for entry in candidates if entry.get("category") == category), None)
            if selected is None and len(candidates) == 1:
                selected = candidates[0]
            if selected:
                match = {
                    "guid": guid,
                    "name": item.get("name", ""),
                    "author": item.get("author", ""),
                    "filename": selected["filename"],
                    "category": selected.get("category", ""),
                    "file_size": int(item.get("file_size") or 0),
                    "url": selected["url"],
                    "source": "exact-filename",
                }

        if match:
            entries[key] = match
            item.update(
                {
                    "status": "downloadable",
                    "status_key": "downloadable",
                    "reason": "",
                    "name": match.get("name") or item.get("name", ""),
                    "author": match.get("author") or item.get("author", ""),
                    "category": match.get("category", ""),
                    "file_size": int(match.get("file_size") or item.get("file_size") or 0),
                    "filename": match.get("filename", ""),
                    "url": match.get("url", ""),
                }
            )
        else:
            unresolved_items.append(item)

    if not was_cancelled and unresolved_items and probe_limit > 0 and max_remote_probes > 0:
        associations: list[tuple[float, dict]] = []
        for item in unresolved_items:
            if cancelled():
                was_cancelled = True
                break
            ranked = sorted(
                ((_candidate_score(item, candidate), candidate) for candidate in url_items),
                key=lambda pair: pair[0],
                reverse=True,
            )
            for score, candidate in ranked[:probe_limit]:
                if score < 0.32:
                    break
                associations.append((score, candidate))

        associations.sort(key=lambda pair: pair[0], reverse=True)
        unique_candidates: list[dict] = []
        seen_urls: set[str] = set()
        for _score, candidate in associations:
            url = candidate["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            unique_candidates.append(candidate)
            if len(unique_candidates) >= max_remote_probes:
                break

        unresolved_by_guid = {
            str(item.get("guid") or "").casefold(): item for item in unresolved_items if item.get("guid")
        }
        if unique_candidates and not was_cancelled:
            worker_count = max(1, min(int(probe_workers), 12))
            progress(
                f"开始并发核对远程 manifest：{len(unique_candidates)} 个去重候选，{worker_count} 路并发。"
            )

            def probe(candidate: dict):
                try:
                    return candidate, read_remote_manifest(candidate["url"]), ""
                except (OSError, ValueError, ElementTree.ParseError) as exc:
                    return candidate, None, str(exc)

            executor = ThreadPoolExecutor(max_workers=worker_count)
            futures = {executor.submit(probe, candidate): candidate for candidate in unique_candidates}
            completed = 0
            try:
                for future in as_completed(futures):
                    if cancelled():
                        was_cancelled = True
                        progress("已收到取消请求，正在停止剩余远程探测。")
                        for pending in futures:
                            pending.cancel()
                        break
                    completed += 1
                    candidate, manifest, _error = future.result()
                    if completed == 1 or completed % 10 == 0 or completed == len(unique_candidates):
                        progress(f"远程核对进度：{completed}/{len(unique_candidates)}")
                    if not manifest:
                        continue
                    key = manifest["guid"].casefold()
                    catalog_match = {
                        **manifest,
                        "filename": candidate["filename"],
                        "category": candidate.get("category", ""),
                        "url": candidate["url"],
                        "source": "remote-manifest-range-check",
                    }
                    entries.setdefault(key, catalog_match)
                    item = unresolved_by_guid.get(key)
                    if item is None:
                        continue
                    match = catalog_match
                    entries[key] = match
                    item.update(
                        {
                            "status": "downloadable",
                            "status_key": "downloadable",
                            "reason": "",
                            "name": match.get("name") or item.get("name", ""),
                            "author": match.get("author") or item.get("author", ""),
                            "category": match.get("category", ""),
                            "file_size": int(match.get("file_size") or 0),
                            "filename": match.get("filename", ""),
                            "url": match.get("url", ""),
                        }
                    )
                    unresolved_by_guid.pop(key, None)
                    progress(f"已解析：{item['guid']} → {candidate['filename']}")
                    if not unresolved_by_guid:
                        for pending in futures:
                            pending.cancel()
                        break
            finally:
                executor.shutdown(wait=True, cancel_futures=True)

    items = report.get("items", [])
    downloadable = sum(
        bool(item.get("status") == "downloadable" and item.get("url")) for item in items
    )
    report["summary"] = {
        "total": len(items),
        "downloadable": downloadable,
        "not_found": len(items) - downloadable,
        "non_downloadable": 0,
        "plugin": 0,
    }
    report["cancelled"] = was_cancelled
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, output_path)
    save_catalog(catalog_path, entries)
    return report


class _DirectoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def crawl_public_index(
    output_path: Path,
    base_url: str = DEFAULT_BASE_URL,
    progress: Callable[[str], None] | None = None,
    delay: float = 0.03,
    cancelled: Callable[[], bool] | None = None,
    workers: int = 8,
) -> list[dict]:
    """Recursively crawl the public directory with bounded concurrency."""
    progress = progress or (lambda _message: None)
    cancelled = cancelled or (lambda: False)
    parsed_base = urlparse(base_url)
    if parsed_base.scheme != "https" or parsed_base.hostname != ALLOWED_HOST:
        raise ValueError("索引基址不在允许的公开下载站")

    old_files: list[dict] = []
    if output_path.exists():
        try:
            old_files = load_url_cache(output_path)
        except (OSError, ValueError, TypeError):
            old_files = []

    pending = [base_url]
    queued = {base_url}
    visited: set[str] = set()
    files_by_url: dict[str, dict] = {}
    failures: list[dict] = []
    worker_count = max(1, min(int(workers), 12))
    progress(f"开始更新公开索引：{worker_count} 路并发。")

    def fetch_directory(current: str) -> tuple[list[str], list[dict]]:
        last_error: Exception | None = None
        for attempt in range(2):
            if cancelled():
                return [], []
            try:
                with _request(quote(current, safe=":/%?=&")) as response:
                    html = response.read().decode("utf-8", errors="replace")
                break
            except OSError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.25)
        else:
            raise OSError(f"读取目录失败：{current}：{last_error}")

        parser = _DirectoryParser()
        parser.feed(html)
        directories: list[str] = []
        found_files: list[dict] = []
        for href in parser.links:
            if href.startswith("?") or href.startswith("#") or href == "../":
                continue
            absolute = urljoin(current, href)
            parsed = urlparse(absolute)
            if parsed.hostname != ALLOWED_HOST or not unquote(parsed.path).startswith(ALLOWED_PATH_PREFIX):
                continue
            if parsed.path.endswith("/"):
                directories.append(absolute)
                continue
            if not parsed.path.casefold().endswith(".zipmod"):
                continue
            relative = unquote(parsed.path)[len(ALLOWED_PATH_PREFIX) :]
            category = relative.split("/", 1)[0] if "/" in relative else ""
            found_files.append(
                {
                    "url": absolute,
                    "filename": unquote(parsed.path.rsplit("/", 1)[-1]),
                    "category": category,
                    "game_scope": "KKEC",
                }
            )
        if delay:
            time.sleep(delay)
        return directories, found_files

    executor = ThreadPoolExecutor(max_workers=worker_count)
    futures: dict = {}
    try:
        while pending or futures:
            if cancelled():
                progress("已收到取消请求，停止提交新的目录任务。")
                pending.clear()
                for future in futures:
                    future.cancel()
                break

            while pending and len(futures) < worker_count:
                current = pending.pop()
                futures[executor.submit(fetch_directory, current)] = current

            if not futures:
                break
            done, _not_done = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                current = futures.pop(future)
                visited.add(current)
                try:
                    directories, found_files = future.result()
                except Exception as exc:
                    failures.append({"url": current, "error": str(exc)})
                    progress(f"目录读取失败，已跳过：{unquote(urlparse(current).path)}")
                    continue
                for directory in directories:
                    if directory not in queued:
                        queued.add(directory)
                        pending.append(directory)
                for item in found_files:
                    files_by_url[item["url"]] = item
                count = len(visited)
                if count == 1 or count % 25 == 0:
                    progress(
                        f"索引进度：已读取 {count} 个目录，发现 {len(files_by_url)} 个 zipmod，"
                        f"待处理 {len(pending) + len(futures)} 个目录。"
                    )
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    complete = not cancelled() and not failures and not pending and not futures
    if not complete and old_files:
        for item in old_files:
            url = str(item.get("url") or "")
            if url:
                files_by_url.setdefault(url, item)
        progress("本次更新未完整结束，已合并保留旧索引。")
    files = sorted(files_by_url.values(), key=lambda item: str(item.get("url") or "").casefold())

    payload = {
        "schema_version": 2,
        "scope": "KKEC",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "complete": complete,
        "workers": worker_count,
        "directories": len(visited),
        "failures": failures,
        "count": len(files),
        "urls": files,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, output_path)
    return files
