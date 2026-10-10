from __future__ import annotations

import base64
import json
import os
import queue
import threading
import traceback
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Button, Entry, Frame, Label, Menu, PhotoImage, StringVar, Text, Tk, Toplevel, filedialog, messagebox
from tkinter import ttk

from .catalog import crawl_public_index, resolve_report
from .cli import DATA_DIR, DEFAULT_CATALOG, DEFAULT_URL_CACHE, REPORT_DIR, perform_scan, validate_game_dir
from .downloader import install_report


DEFAULT_GAME = os.environ.get("KK_GAME_DIR", "")
SETTINGS_PATH = DATA_DIR / "settings.json"

STATUS_TEXT = {
    "downloadable": "等待下载",
    "not_found": "未找到地址",
    "unresolved": "未找到地址",
    "queued": "排队中",
    "downloading": "下载中",
    "retrying": "自动重试",
    "installed": "安装完成",
    "skipped": "已存在",
    "failed": "下载失败",
    "cancelled": "已取消",
    "satisfied": "已安装",
}


def _format_size(value: int | None) -> str:
    size = int(value or 0)
    if not size:
        return "未知"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "未知"


def _selection_marker(selected: bool, selectable: bool) -> str:
    if selected:
        return "☑"
    return "☐" if selectable else "—"


def _load_saved_game_dir() -> str:
    if DEFAULT_GAME:
        return DEFAULT_GAME
    if not SETTINGS_PATH.exists():
        return ""


def _scene_cards_from_report(data: dict) -> list[dict]:
    """Return scene-card records, deriving a missing-only index for old reports."""
    records = [
        dict(card)
        for card in data.get("cards", [])
        if str(card.get("card_type") or "").casefold() == "scene" and card.get("path")
    ]
    if records:
        return sorted(records, key=lambda card: str(card.get("path") or "").casefold())

    by_path: dict[str, dict] = {}
    for item in data.get("items", []):
        for card in item.get("cards", []):
            if str(card.get("card_type") or "").casefold() != "scene":
                continue
            path = str(card.get("path") or "")
            if not path:
                continue
            record = by_path.setdefault(
                path,
                {
                    "path": path,
                    "filename": card.get("filename") or Path(path).name,
                    "card_type": "scene",
                    "mods": [],
                },
            )
            record["mods"].append(
                {
                    "guid": item.get("guid", ""),
                    "name": item.get("name", ""),
                    "filename": item.get("filename", ""),
                    "file_size": item.get("file_size", 0),
                    "installed": False,
                }
            )
    for record in by_path.values():
        record["mod_count"] = len(record["mods"])
        record["missing_count"] = len(record["mods"])
    return sorted(by_path.values(), key=lambda card: str(card.get("path") or "").casefold())


def _items_for_selected_cards(
    all_missing_items: dict[str, dict],
    scene_cards: dict[str, dict],
    selected_paths: set[str],
) -> dict[str, dict]:
    if not selected_paths:
        return dict(all_missing_items)

    visible: dict[str, dict] = {}
    for path in sorted(selected_paths, key=str.casefold):
        card = scene_cards.get(path)
        if not card:
            continue
        for dependency in card.get("mods", []):
            guid = str(dependency.get("guid") or "").strip()
            if not guid:
                continue
            key = guid.casefold()
            if key in all_missing_items:
                item = dict(all_missing_items[key])
                item["cards"] = [
                    entry
                    for entry in item.get("cards", [])
                    if str(entry.get("path") or "") in selected_paths
                ]
            else:
                item = {
                    "guid": guid,
                    "status": "satisfied" if dependency.get("installed") else "not_found",
                    "reason": (
                        "游戏 mods 目录中已安装"
                        if dependency.get("installed")
                        else "此依赖未出现在当前缺失报告中"
                    ),
                    "name": dependency.get("name", ""),
                    "author": dependency.get("author", ""),
                    "filename": dependency.get("filename", ""),
                    "file_size": int(dependency.get("file_size") or 0),
                    "url": "",
                    "cards": [
                        {
                            "path": path,
                            "filename": card.get("filename") or Path(path).name,
                            "card_type": "scene",
                        }
                    ],
                }
            if key in visible:
                known_paths = {str(entry.get("path") or "") for entry in visible[key].get("cards", [])}
                visible[key].setdefault("cards", []).extend(
                    entry for entry in item.get("cards", []) if str(entry.get("path") or "") not in known_paths
                )
            else:
                visible[key] = item
    return visible


def _png_preview_data(path: Path) -> str:
    """Trim KK data appended after PNG IEND and return Tk-compatible base64."""
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("不是有效的 PNG 场景卡")
    cursor = 8
    while cursor + 12 <= len(data):
        length = int.from_bytes(data[cursor : cursor + 4], "big")
        chunk_type = data[cursor + 4 : cursor + 8]
        cursor += 12 + length
        if cursor > len(data):
            break
        if chunk_type == b"IEND":
            return base64.b64encode(data[:cursor]).decode("ascii")
    raise ValueError("PNG 场景卡缺少 IEND 数据块")
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return str(data.get("game_dir") or "")
    except (OSError, ValueError, TypeError):
        return ""


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("KK 缺失 MOD 批量安装器")
        self.root.geometry("1220x790")
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
        self.stop_event = threading.Event()
        self.current_report: Path | None = None
        self.report_data: dict = {}
        self.all_items_by_guid: dict[str, dict] = {}
        self.items_by_guid: dict[str, dict] = {}
        self.task_states: dict[str, dict] = {}
        self.selected_guids: set[str] = set()
        self.scene_cards: dict[str, dict] = {}
        self.selected_scene_paths: set[str] = set()
        self.scene_preview_image: PhotoImage | None = None
        self.last_checkbox_click: tuple[str, int] = ("", -1000)

        self.game_var = StringVar(value=_load_saved_game_dir())
        self.report_label_var = StringVar(value="尚未生成报告")

        path_frame = Frame(root)
        path_frame.pack(fill=X, padx=10, pady=(10, 4))
        Label(path_frame, text="游戏目录", width=10, anchor="w").pack(side=LEFT)
        Entry(path_frame, textvariable=self.game_var).pack(side=LEFT, fill=X, expand=True, padx=4)
        Button(path_frame, text="选择游戏目录", command=self.choose_game_dir).pack(side=RIGHT)

        scan_frame = Frame(root)
        scan_frame.pack(fill=X, padx=10, pady=4)
        self.action_buttons = [
            Button(scan_frame, text="追加解析", width=14, command=lambda: self.scan_and_resolve(False)),
            Button(scan_frame, text="完整解析", width=14, command=lambda: self.scan_and_resolve(True)),
            Button(scan_frame, text="更新公开索引（8 路）", width=18, command=self.crawl),
            Button(scan_frame, text="打开报告文件夹", width=14, command=self.open_report_folder),
        ]
        for button in self.action_buttons:
            button.pack(side=LEFT, padx=(0, 6))
        self.cancel_button = Button(scan_frame, text="取消当前任务", width=14, command=self.cancel)
        self.cancel_button.pack(side=RIGHT)

        Label(
            root,
            text=(
                "追加解析：复用上次扫描结果，只处理新增或变化内容；完整解析：重新读取全部角色卡和已装 MOD。"
                " 两种方式都会自动生成、解析并保存 JSON，无需手动选择文件。"
            ),
            anchor="w",
            justify=LEFT,
        ).pack(fill=X, padx=10, pady=(2, 2))
        Label(root, textvariable=self.report_label_var, anchor="w", fg="#555555").pack(fill=X, padx=10, pady=(0, 6))

        scene_filter_frame = Frame(root)
        scene_filter_frame.pack(fill=X, padx=10, pady=(0, 5))
        self.scene_filter_button = Button(
            scene_filter_frame,
            text="选择场景卡…",
            command=self.open_scene_card_selector,
        )
        self.scene_filter_button.pack(side=LEFT, padx=(0, 6))
        self.clear_scene_filter_button = Button(
            scene_filter_frame,
            text="显示全部缺失 MOD",
            command=self.clear_scene_filter,
        )
        self.clear_scene_filter_button.pack(side=LEFT, padx=(0, 8))
        self.scene_filter_var = StringVar(value="当前：全部卡片的缺失 MOD")
        Label(scene_filter_frame, textvariable=self.scene_filter_var, anchor="w", fg="#245a8d").pack(
            side=LEFT, fill=X, expand=True
        )

        selection_frame = Frame(root)
        selection_frame.pack(fill=X, padx=10, pady=(2, 5))
        self.selection_buttons = [
            Button(selection_frame, text="全选可下载", command=self.select_downloadable),
            Button(selection_frame, text="只选失败任务", command=self.select_failed),
            Button(selection_frame, text="勾选高亮项", command=self.select_highlighted),
            Button(selection_frame, text="取消高亮项", command=self.deselect_highlighted),
            Button(selection_frame, text="清除选择", command=self.clear_selection),
            Button(selection_frame, text="复制模组名称", command=self.copy_selected_mod_name),
        ]
        for button in self.selection_buttons:
            button.pack(side=LEFT, padx=(0, 6))
        self.download_button = Button(
            selection_frame,
            text="并发下载选中项（4 个同时）",
            command=self.download_selected,
        )
        self.download_button.pack(side=RIGHT)

        columns = ("selected", "status", "name", "guid", "size", "cards", "progress", "reason")
        table_frame = Frame(root)
        table_frame.pack(fill=BOTH, expand=True, padx=10, pady=4)
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "selected": "选择",
            "status": "状态",
            "name": "模组名称 / 文件名",
            "guid": "GUID",
            "size": "大小",
            "cards": "关联卡数",
            "progress": "进度",
            "reason": "说明",
        }
        widths = {"selected": 52, "status": 85, "name": 265, "guid": 210, "size": 78, "cards": 65, "progress": 92, "reason": 290}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=45, anchor="w")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        yscroll.pack(side=RIGHT, fill="y")
        self.tree.bind("<ButtonRelease-1>", self._handle_mod_table_click)
        self.tree.bind("<Double-1>", self._toggle_selected_row)
        self.tree.bind("<Button-3>", self._show_tree_menu)
        self.tree.bind("<Control-c>", self.copy_selected_mod_name)
        self.tree.bind("<space>", self._toggle_highlighted_rows)

        self.tree_menu = Menu(self.root, tearoff=False)
        self.tree_menu.add_command(label="复制模组名称", command=self.copy_selected_mod_name)
        self.tree_menu.add_command(label="复制 GUID", command=self.copy_selected_guid)
        self.tree_menu.add_command(label="复制名称 + GUID", command=self.copy_selected_mod_info)

        self.log = Text(root, height=8, wrap="word")
        self.log.pack(fill=X, padx=10, pady=(4, 10))
        self._write(
            "程序会自动管理 JSON 报告。单击第一列的 ☐ 可直接勾选；用 Ctrl/Shift+单击可高亮多行，"
            "再点击“勾选高亮项”或按空格键；Ctrl+C 可批量复制名称。\n"
        )
        self.root.after(100, self._drain_messages)
        self.root.after(200, self._startup)

    def _startup(self) -> None:
        try:
            latest = self._latest_report()
            if latest:
                self.load_report(latest)
        except Exception as exc:
            self._write(f"自动恢复上次任务失败：{exc}\n")

    @staticmethod
    def _latest_report() -> Path | None:
        preferred = list(REPORT_DIR.glob("missing_mods_*_resolved.json"))
        candidates = preferred or [
            path for path in REPORT_DIR.glob("*.json") if not path.name.endswith("_install_results.json")
        ]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None

    def _write(self, message: str) -> None:
        self.log.insert(END, message)
        self.log.see(END)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in self.action_buttons + self.selection_buttons + [
            self.download_button,
            self.scene_filter_button,
            self.clear_scene_filter_button,
        ]:
            button.configure(state=state)

    def _run(self, title: str, function) -> None:
        if self.busy:
            messagebox.showinfo("任务进行中", "请等待当前任务完成或先取消。")
            return
        self.stop_event.clear()
        self._set_busy(True)
        self._write(f"\n=== {title} ===\n")

        def worker() -> None:
            try:
                result = function()
                self.messages.put(("done", result))
            except Exception:
                self.messages.put(("error", traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()

    def _progress(self, message: str) -> None:
        self.messages.put(("log", message + "\n"))

    def _task_event(self, event: dict) -> None:
        self.messages.put(("task", event))

    def _drain_messages(self) -> None:
        try:
            while True:
                try:
                    kind, payload = self.messages.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "log":
                        self._write(str(payload))
                    elif kind == "task":
                        self._apply_task_event(dict(payload))
                    elif kind == "done":
                        if isinstance(payload, dict) and payload.get("report"):
                            self.load_report(Path(str(payload["report"])))
                            self._write(f"当前报告已自动切换：{Path(str(payload['report'])).name}\n")
                            payload = payload.get("message", "任务完成")
                        self._write(f"完成：{payload}\n")
                        self._set_busy(False)
                    elif kind == "error":
                        self._write(str(payload) + "\n")
                        self._set_busy(False)
                        messagebox.showerror("任务失败", "操作失败，详细信息已写入下方日志。")
                except Exception:
                    self._write("界面刷新失败：\n" + traceback.format_exc() + "\n")
                    self._set_busy(False)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain_messages)

    def scan_and_resolve(self, full: bool) -> None:
        game = Path(self.game_var.get().strip())
        title = "完整解析" if full else "追加解析"

        def action() -> str:
            validate_game_dir(game)
            self._remember_game_dir(game)
            self._progress("正在重新读取全部文件……" if full else "正在读取新增和变化的文件……")
            scan_path = perform_scan(game, DEFAULT_CATALOG, full=full)
            self._progress(f"缺失清单已生成，正在自动解析下载地址：{scan_path.name}")
            url_cache = self._ensure_url_cache(game)
            resolved_path = scan_path.with_name(scan_path.stem + "_resolved.json")
            result = resolve_report(
                scan_path,
                url_cache,
                DEFAULT_CATALOG,
                resolved_path,
                probe_limit=5,
                progress=self._progress,
                cancelled=self.stop_event.is_set,
                probe_workers=8,
                max_remote_probes=40,
            )
            summary = result.get("summary", {})
            return {
                "report": str(resolved_path),
                "message": (
                    f"JSON 已自动保存：{resolved_path}；"
                    f"可下载 {summary.get('downloadable', 0)}，未解析 {summary.get('not_found', 0)}。"
                ),
            }

        self._run(title, action)

    def _ensure_url_cache(self, game: Path) -> Path:
        if DEFAULT_URL_CACHE.exists():
            return DEFAULT_URL_CACHE

        # ModResolver commonly lives beside the game directory. Reuse its public
        # URL index when present, without asking non-technical users to locate it.
        resolver_cache = game.parent / "SD" / ".modresolver" / "crawl_cache" / "KKEC_urls.json"
        if resolver_cache.exists():
            self._progress(f"已自动找到现有公开索引：{resolver_cache}")
            return resolver_cache

        self._progress("首次运行未找到公开 URL 索引，正在自动建立索引；这可能需要几分钟……")
        crawl_public_index(
            DEFAULT_URL_CACHE,
            progress=self._progress,
            cancelled=self.stop_event.is_set,
            workers=8,
        )
        if not DEFAULT_URL_CACHE.exists():
            raise FileNotFoundError("公开 URL 索引尚未建立完成，请重试或点击“更新公开索引”。")
        return DEFAULT_URL_CACHE

    def choose_game_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.game_var.get() or None)
        if selected:
            self.game_var.set(selected)
            self._remember_game_dir(Path(selected))

    @staticmethod
    def _remember_game_dir(game: Path) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "game_dir": str(game.resolve())}
        temporary = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, SETTINGS_PATH)

    def load_report(self, report_path: Path) -> None:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        self.current_report = report_path
        self.report_data = data
        self.all_items_by_guid.clear()
        self.items_by_guid.clear()
        self.task_states.clear()
        self.selected_guids.clear()
        self.selected_scene_paths.clear()
        scene_records = _scene_cards_from_report(data)
        self.scene_cards = {str(card["path"]): card for card in scene_records}
        self.tree.delete(*self.tree.get_children())

        for item in data.get("items", []):
            guid = str(item.get("guid") or "").strip()
            if not guid:
                continue
            key = guid.casefold()
            self.all_items_by_guid[key] = item
            status = str(item.get("status") or "unresolved")
            state = {
                "guid": guid,
                "status": status,
                "reason": str(item.get("reason") or ""),
                "downloaded": 0,
                "total": int(item.get("file_size") or 0),
            }
            self.task_states[key] = state

        result_path = report_path.with_name(report_path.stem + "_install_results.json")
        if result_path.exists():
            try:
                previous = json.loads(result_path.read_text(encoding="utf-8"))
                for event in previous.get("results", []):
                    key = str(event.get("guid") or "").casefold()
                    if key in self.task_states:
                        self.task_states[key].update(event)
                        if event.get("status") in {"installed", "skipped"}:
                            self.selected_guids.discard(key)
            except Exception as exc:
                self._write(f"读取上次下载结果失败：{exc}\n")

        self._apply_scene_filter()
        summary = data.get("summary", {})
        self.report_label_var.set(
            f"当前报告：{report_path.name}｜缺失 {summary.get('total', len(self.items_by_guid))}｜"
            f"可下载 {summary.get('downloadable', 0)}｜未解析 {summary.get('not_found', 0)}"
        )
        self.root.title(f"KK 缺失 MOD 批量安装器 - {report_path.name}")
        failed = sum(state.get("status") == "failed" for state in self.task_states.values())
        self._write(
            f"已自动载入报告：{report_path.name}。识别到 {len(self.scene_cards)} 张场景卡；"
            f"上次失败任务：{failed}。\n"
        )

    def _apply_scene_filter(self) -> None:
        self.items_by_guid = _items_for_selected_cards(
            self.all_items_by_guid,
            self.scene_cards,
            self.selected_scene_paths,
        )
        selectable = {
            key for key, item in self.items_by_guid.items() if item.get("status") == "downloadable"
        }
        self.selected_guids.intersection_update(selectable)
        self.tree.delete(*self.tree.get_children())
        for key in self.items_by_guid:
            self._refresh_row(key)

        if self.selected_scene_paths:
            missing_count = sum(item.get("status") != "satisfied" for item in self.items_by_guid.values())
            installed_count = len(self.items_by_guid) - missing_count
            self.scene_filter_var.set(
                f"当前：已选择 {len(self.selected_scene_paths)} 张场景卡｜"
                f"依赖 {len(self.items_by_guid)}｜缺失 {missing_count}｜已安装 {installed_count}"
            )
        else:
            self.scene_filter_var.set(
                f"当前：全部卡片的缺失 MOD（{len(self.all_items_by_guid)} 项）"
            )

    def clear_scene_filter(self) -> None:
        if not self.selected_scene_paths:
            return
        self.selected_scene_paths.clear()
        self._apply_scene_filter()
        self._write("已取消场景卡筛选，恢复显示全部卡片的缺失 MOD。\n")

    def open_scene_card_selector(self) -> None:
        if not self.current_report:
            messagebox.showinfo("尚无报告", "请先点击“追加解析”或“完整解析”。")
            return
        if not self.scene_cards:
            messagebox.showinfo(
                "没有场景卡",
                "当前报告没有场景卡信息。若这是旧报告，请重新执行一次追加解析或完整解析。",
            )
            return

        window = Toplevel(self.root)
        window.title("选择场景卡（支持多选）")
        window.geometry("1080x650")
        window.minsize(900, 560)
        window.transient(self.root)
        window.grab_set()
        draft_selected = set(self.selected_scene_paths)

        body = Frame(window)
        body.pack(fill=BOTH, expand=True, padx=10, pady=10)
        list_frame = Frame(body)
        list_frame.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 10))
        preview_frame = Frame(body, width=440)
        preview_frame.pack(side=RIGHT, fill=BOTH)
        preview_frame.pack_propagate(False)

        columns = ("selected", "filename", "mods", "missing", "path")
        card_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="extended")
        for column, title, width in (
            ("selected", "选择", 48),
            ("filename", "场景卡", 210),
            ("mods", "全部 zipmod", 82),
            ("missing", "缺失", 58),
            ("path", "位置", 310),
        ):
            card_tree.heading(column, text=title)
            card_tree.column(column, width=width, minwidth=45, anchor="w")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=card_tree.yview)
        card_tree.configure(yscrollcommand=scrollbar.set)
        card_tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill="y")

        iid_to_path: dict[str, str] = {}
        path_to_iid: dict[str, str] = {}
        for index, (path, card) in enumerate(self.scene_cards.items()):
            iid = f"scene_{index}"
            iid_to_path[iid] = path
            path_to_iid[path] = iid
            card_tree.insert(
                "",
                END,
                iid=iid,
                values=(
                    "☑" if path in draft_selected else "☐",
                    card.get("filename") or Path(path).name,
                    int(card.get("mod_count") or len(card.get("mods", []))),
                    int(
                        card.get("missing_count")
                        if card.get("missing_count") is not None
                        else sum(not mod.get("installed") for mod in card.get("mods", []))
                    ),
                    path,
                ),
            )

        Label(preview_frame, text="场景卡预览", font=("Microsoft YaHei UI", 11, "bold")).pack(
            fill=X, pady=(4, 8)
        )
        preview_label = Label(
            preview_frame,
            text="单击左侧场景卡查看图片",
            anchor="center",
            justify="center",
            bg="#202020",
            fg="#eeeeee",
        )
        preview_label.pack(fill=BOTH, expand=True)
        preview_info = StringVar(value="")
        Label(
            preview_frame,
            textvariable=preview_info,
            anchor="w",
            justify=LEFT,
            wraplength=420,
        ).pack(fill=X, pady=(8, 0))

        def refresh_card_row(path: str) -> None:
            iid = path_to_iid[path]
            values = list(card_tree.item(iid, "values"))
            values[0] = "☑" if path in draft_selected else "☐"
            card_tree.item(iid, values=values)

        def show_preview(_event=None) -> None:
            highlighted = card_tree.selection()
            if not highlighted:
                return
            path = iid_to_path.get(str(highlighted[0]))
            if not path:
                return
            card = self.scene_cards[path]
            try:
                image = PhotoImage(data=_png_preview_data(Path(path)), format="png")
                factor = max(1, (image.width() + 419) // 420, (image.height() + 359) // 360)
                if factor > 1:
                    image = image.subsample(factor, factor)
                self.scene_preview_image = image
                preview_label.configure(image=image, text="")
            except Exception as exc:
                self.scene_preview_image = None
                preview_label.configure(image="", text=f"无法读取预览\n{exc}")
            preview_info.set(
                f"{card.get('filename') or Path(path).name}\n"
                f"全部 zipmod：{int(card.get('mod_count') or len(card.get('mods', [])))}　"
                f"缺失：{int(card.get('missing_count') or 0)}\n{path}"
            )

        def toggle_highlighted(_event=None) -> None:
            paths = [iid_to_path[str(iid)] for iid in card_tree.selection()]
            if not paths:
                messagebox.showinfo("请先选择", "请先单击或用 Ctrl/Shift 选择场景卡。", parent=window)
                return
            if all(path in draft_selected for path in paths):
                draft_selected.difference_update(paths)
            else:
                draft_selected.update(paths)
            for path in paths:
                refresh_card_row(path)

        def toggle_clicked(event) -> None:
            iid = card_tree.identify_row(event.y)
            if not iid:
                return
            card_tree.selection_set(iid)
            show_preview()
            toggle_highlighted()

        def select_all_cards() -> None:
            draft_selected.update(self.scene_cards)
            for path in self.scene_cards:
                refresh_card_row(path)

        def clear_cards() -> None:
            draft_selected.clear()
            for path in self.scene_cards:
                refresh_card_row(path)

        def apply_cards() -> None:
            self.selected_scene_paths = set(draft_selected)
            self._apply_scene_filter()
            if self.selected_scene_paths:
                self._write(
                    f"已按 {len(self.selected_scene_paths)} 张场景卡筛选。"
                    "点击“全选可下载”即可只勾选这些场景卡的缺失 MOD。\n"
                )
            window.destroy()

        card_tree.bind("<<TreeviewSelect>>", show_preview)
        card_tree.bind("<Double-1>", toggle_clicked)
        instruction = Label(
            window,
            text="单击查看图片；Ctrl/Shift 可高亮多张，双击可直接勾选或取消。应用后主列表会显示所选卡的全部依赖。",
            anchor="w",
        )
        instruction.pack(fill=X, padx=10, pady=(0, 6))
        buttons = Frame(window)
        buttons.pack(fill=X, padx=10, pady=(0, 10))
        Button(buttons, text="勾选/取消高亮卡", command=toggle_highlighted).pack(side=LEFT, padx=(0, 6))
        Button(buttons, text="全选场景卡", command=select_all_cards).pack(side=LEFT, padx=(0, 6))
        Button(buttons, text="清空选择", command=clear_cards).pack(side=LEFT, padx=(0, 6))
        Button(buttons, text="应用筛选", command=apply_cards, width=14).pack(side=RIGHT)
        Button(buttons, text="取消", command=window.destroy, width=10).pack(side=RIGHT, padx=(0, 6))
        if card_tree.get_children():
            first = card_tree.get_children()[0]
            card_tree.selection_set(first)
            card_tree.focus(first)
            show_preview()

    def _refresh_row(self, key: str) -> None:
        item = self.items_by_guid[key]
        state = self.task_states.get(
            key,
            {
                "status": item.get("status") or "unresolved",
                "reason": item.get("reason") or "",
                "downloaded": 0,
                "total": int(item.get("file_size") or 0),
            },
        )
        status = str(state.get("status") or item.get("status") or "unresolved")
        downloaded = int(state.get("downloaded") or state.get("bytes") or 0)
        total = int(state.get("total") or item.get("file_size") or 0)
        if status == "installed":
            progress_text = "100%"
        elif total and downloaded:
            progress_text = f"{min(downloaded / total * 100, 100):.1f}%"
        else:
            progress_text = "—"
        reason = str(state.get("reason") or item.get("reason") or "")
        if status == "retrying" and state.get("attempt"):
            reason = f"第 {state['attempt']} 次重试；{reason}".strip("；")
        values = (
            _selection_marker(key in self.selected_guids, self._is_downloadable_key(key)),
            STATUS_TEXT.get(status, status),
            item.get("name") or item.get("filename") or "未知模组",
            item.get("guid", ""),
            _format_size(item.get("file_size")),
            len(item.get("cards") or []),
            progress_text,
            reason,
        )
        if self.tree.exists(key):
            self.tree.item(key, values=values)
        else:
            self.tree.insert("", END, iid=key, values=values)

    def _apply_task_event(self, event: dict) -> None:
        key = str(event.get("guid") or "").casefold()
        if key not in self.task_states:
            return
        self.task_states[key].update(event)
        if event.get("status") in {"installed", "skipped"}:
            self.selected_guids.discard(key)
        self._refresh_row(key)

    def _is_downloadable_key(self, key: str) -> bool:
        item = self.items_by_guid.get(key, {})
        state = self.task_states.get(key, {})
        return item.get("status") == "downloadable" and state.get("status") not in {
            "installed",
            "skipped",
        }

    def _toggle_selected_row(self, event) -> None:
        key = self.tree.identify_row(event.y)
        if not key or key not in self.items_by_guid:
            return
        if self.tree.identify_column(event.x) == "#1":
            return "break"
        self._toggle_mod_key(key)

    def _handle_mod_table_click(self, event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        key = self.tree.identify_row(event.y)
        if not key or key not in self.items_by_guid:
            return None
        last_key, last_time = self.last_checkbox_click
        if key == last_key and int(event.time) - last_time < 350:
            return "break"
        self.last_checkbox_click = (key, int(event.time))
        self.tree.selection_set(key)
        self.tree.focus(key)
        self._toggle_mod_key(key)
        return "break"

    def _toggle_mod_key(self, key: str) -> None:
        if not self._is_downloadable_key(key):
            status = self.task_states.get(key, {}).get("status") or self.items_by_guid[key].get("status")
            message = (
                "这个依赖已经安装，无需重复下载。"
                if status in {"satisfied", "installed", "skipped"}
                else "这个缺失 MOD 还没有解析到下载地址，暂时无法加入下载任务。"
            )
            messagebox.showinfo("不可下载", message)
            return
        if key in self.selected_guids:
            self.selected_guids.remove(key)
        else:
            self.selected_guids.add(key)
        self._refresh_row(key)

    def _selected_tree_keys(self) -> list[str]:
        return [str(key) for key in self.tree.selection() if str(key) in self.items_by_guid]

    def _toggle_highlighted_rows(self, _event=None) -> str:
        keys = [key for key in self._selected_tree_keys() if self._is_downloadable_key(key)]
        if not keys:
            messagebox.showinfo("无可下载项", "请先高亮至少一个状态为“等待下载”的模组。")
            return "break"
        if all(key in self.selected_guids for key in keys):
            self.selected_guids.difference_update(keys)
        else:
            self.selected_guids.update(keys)
        for key in keys:
            self._refresh_row(key)
        return "break"

    def _copy_text(self, text: str, description: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self._write(f"已复制{description}：{text}\n")

    def copy_selected_mod_name(self, _event=None) -> str:
        keys = self._selected_tree_keys()
        if not keys:
            messagebox.showinfo("请先选择", "请先单击选中至少一行模组。")
            return "break"
        names = [
            str(self.items_by_guid[key].get("name") or self.items_by_guid[key].get("filename") or "未知模组")
            for key in keys
        ]
        self._copy_text("\n".join(names), f" {len(names)} 个模组名称")
        return "break"

    def copy_selected_guid(self) -> None:
        keys = self._selected_tree_keys()
        if not keys:
            messagebox.showinfo("请先选择", "请先单击选中至少一行模组。")
            return
        guids = [str(self.items_by_guid[key].get("guid") or "") for key in keys]
        self._copy_text("\n".join(guids), f" {len(guids)} 个 GUID")

    def copy_selected_mod_info(self) -> None:
        keys = self._selected_tree_keys()
        if not keys:
            messagebox.showinfo("请先选择", "请先单击选中至少一行模组。")
            return
        lines = []
        for key in keys:
            item = self.items_by_guid[key]
            name = str(item.get("name") or item.get("filename") or "未知模组")
            lines.append(f"{name}\t{str(item.get('guid') or '')}")
        self._copy_text("\n".join(lines), f" {len(lines)} 个模组的名称和 GUID")

    def _show_tree_menu(self, event) -> None:
        key = self.tree.identify_row(event.y)
        if not key or key not in self.items_by_guid:
            return
        if key not in self.tree.selection():
            self.tree.selection_set(key)
        self.tree.focus(key)
        try:
            self.tree_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.tree_menu.grab_release()

    def select_highlighted(self) -> None:
        highlighted = self._selected_tree_keys()
        if not highlighted:
            messagebox.showinfo("请先选择", "请先用 Ctrl/Shift+单击高亮要下载的模组。")
            return
        downloadable = [key for key in highlighted if self._is_downloadable_key(key)]
        self.selected_guids.update(downloadable)
        for key in downloadable:
            self._refresh_row(key)
        skipped = len(highlighted) - len(downloadable)
        self._write(f"已勾选 {len(downloadable)} 个高亮模组；跳过 {skipped} 个不可下载项。\n")

    def deselect_highlighted(self) -> None:
        highlighted = self._selected_tree_keys()
        if not highlighted:
            messagebox.showinfo("请先选择", "请先用 Ctrl/Shift+单击高亮要取消的模组。")
            return
        self.selected_guids.difference_update(highlighted)
        for key in highlighted:
            self._refresh_row(key)
        self._write(f"已取消勾选 {len(highlighted)} 个高亮项。\n")

    def select_downloadable(self) -> None:
        self.selected_guids = {
            key for key in self.items_by_guid if self._is_downloadable_key(key)
        }
        for key in self.items_by_guid:
            self._refresh_row(key)
        if not self.selected_guids:
            messagebox.showinfo(
                "没有可下载项",
                "当前列表中的缺失 MOD 都还没有解析到下载地址。请先更新公开索引，再重新执行追加解析。",
            )

    def select_failed(self) -> None:
        self.selected_guids = {
            key
            for key, state in self.task_states.items()
            if key in self.items_by_guid
            and state.get("status") == "failed"
            and self.items_by_guid[key].get("status") == "downloadable"
        }
        for key in self.items_by_guid:
            self._refresh_row(key)
        if not self.selected_guids:
            messagebox.showinfo("没有失败任务", "当前报告中没有可重试的失败下载任务。")

    def clear_selection(self) -> None:
        self.selected_guids.clear()
        for key in self.items_by_guid:
            self._refresh_row(key)

    def download_selected(self) -> None:
        if not self.current_report:
            messagebox.showinfo("尚无报告", "请先点击“追加解析”或“完整解析”。")
            return
        selected = {
            key for key in self.selected_guids if self._is_downloadable_key(key)
        }
        if not selected:
            messagebox.showinfo("没有任务", "请先勾选要下载的项目。双击任务行可以切换选择。")
            return
        game = Path(self.game_var.get().strip())
        try:
            validate_game_dir(game)
            self._remember_game_dir(game)
        except Exception as exc:
            messagebox.showerror("游戏目录无效", str(exc))
            return
        total = sum(int(self.items_by_guid[key].get("file_size") or 0) for key in selected)
        if not messagebox.askyesno(
            "确认并发下载",
            f"将同时下载最多 4 个任务，共 {len(selected)} 项，已知大小约 {total / 1024**3:.2f} GB。\n\n"
            "下载完成后会校验 GUID，再放入 mods\\ModResolver_Downloads。是否继续？",
        ):
            return

        def action() -> str:
            result = install_report(
                self.current_report,
                game,
                workers=4,
                progress=self._progress,
                stop_event=self.stop_event,
                selected_guids=selected,
                task_callback=self._task_event,
            )
            return f"下载结果已保存；{json.dumps(result['summary'], ensure_ascii=False)}"

        self._run("并发下载并安装", action)

    def crawl(self) -> None:
        if not messagebox.askyesno("更新公开索引", "这会读取公开模组目录，可能需要几分钟。是否继续？"):
            return

        def action() -> str:
            files = crawl_public_index(
                DEFAULT_URL_CACHE,
                progress=self._progress,
                cancelled=self.stop_event.is_set,
                workers=8,
            )
            return f"公开索引已更新，共记录 {len(files)} 个 zipmod。"

        self._run("更新公开文件索引", action)

    @staticmethod
    def open_report_folder() -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(REPORT_DIR)

    def cancel(self) -> None:
        self.stop_event.set()
        self._write("已请求取消；正在进行的网络读取结束后会停止。\n")


def main() -> None:
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
