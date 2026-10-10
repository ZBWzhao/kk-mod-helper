from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Button, Entry, Frame, Label, StringVar, Text, Tk, filedialog, messagebox
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


def _load_saved_game_dir() -> str:
    if DEFAULT_GAME:
        return DEFAULT_GAME
    if not SETTINGS_PATH.exists():
        return ""
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
        self.items_by_guid: dict[str, dict] = {}
        self.task_states: dict[str, dict] = {}
        self.selected_guids: set[str] = set()

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

        selection_frame = Frame(root)
        selection_frame.pack(fill=X, padx=10, pady=(2, 5))
        self.selection_buttons = [
            Button(selection_frame, text="全选可下载", command=self.select_downloadable),
            Button(selection_frame, text="只选失败任务", command=self.select_failed),
            Button(selection_frame, text="清除选择", command=self.clear_selection),
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
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
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
        self.tree.bind("<Double-1>", self._toggle_selected_row)

        self.log = Text(root, height=8, wrap="word")
        self.log.pack(fill=X, padx=10, pady=(4, 10))
        self._write("程序会自动管理 JSON 报告。双击任务行可勾选或取消，下载失败项会保留供再次重试。\n")
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
        for button in self.action_buttons + self.selection_buttons + [self.download_button]:
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
        self.items_by_guid.clear()
        self.task_states.clear()
        self.selected_guids.clear()
        self.tree.delete(*self.tree.get_children())

        for item in data.get("items", []):
            guid = str(item.get("guid") or "").strip()
            if not guid:
                continue
            key = guid.casefold()
            self.items_by_guid[key] = item
            status = str(item.get("status") or "unresolved")
            state = {
                "guid": guid,
                "status": status,
                "reason": str(item.get("reason") or ""),
                "downloaded": 0,
                "total": int(item.get("file_size") or 0),
            }
            self.task_states[key] = state
            if status == "downloadable":
                self.selected_guids.add(key)

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

        for key in self.items_by_guid:
            self._refresh_row(key)
        summary = data.get("summary", {})
        self.report_label_var.set(
            f"当前报告：{report_path.name}｜缺失 {summary.get('total', len(self.items_by_guid))}｜"
            f"可下载 {summary.get('downloadable', 0)}｜未解析 {summary.get('not_found', 0)}"
        )
        self.root.title(f"KK 缺失 MOD 批量安装器 - {report_path.name}")
        failed = sum(state.get("status") == "failed" for state in self.task_states.values())
        self._write(f"已自动载入报告：{report_path.name}。上次失败任务：{failed}。\n")

    def _refresh_row(self, key: str) -> None:
        item = self.items_by_guid[key]
        state = self.task_states[key]
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
            "☑" if key in self.selected_guids else "☐",
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

    def _toggle_selected_row(self, event) -> None:
        key = self.tree.identify_row(event.y)
        if not key or key not in self.items_by_guid:
            return
        item = self.items_by_guid[key]
        if item.get("status") != "downloadable":
            messagebox.showinfo("不可下载", "这个项目还没有解析到下载地址。")
            return
        if key in self.selected_guids:
            self.selected_guids.remove(key)
        else:
            self.selected_guids.add(key)
        self._refresh_row(key)

    def select_downloadable(self) -> None:
        self.selected_guids = {
            key for key, item in self.items_by_guid.items() if item.get("status") == "downloadable"
        }
        for key in self.items_by_guid:
            self._refresh_row(key)

    def select_failed(self) -> None:
        self.selected_guids = {
            key
            for key, state in self.task_states.items()
            if state.get("status") == "failed" and self.items_by_guid[key].get("status") == "downloadable"
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
            key for key in self.selected_guids if self.items_by_guid[key].get("status") == "downloadable"
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
