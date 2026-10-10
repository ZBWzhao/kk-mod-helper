from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .cards import scan_user_data
from .catalog import bootstrap_catalog_from_report, crawl_public_index, resolve_report
from .downloader import install_report
from .manifests import scan_installed_mods
from .reporting import build_missing_report, load_catalog, save_report


if getattr(sys, "frozen", False):
    # A one-file PyInstaller build runs its modules from a temporary directory.
    # Keep user data beside the portable executable so it survives restarts.
    PROJECT_DIR = Path(sys.executable).resolve().parent
else:
    PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
REPORT_DIR = PROJECT_DIR / "reports"
DEFAULT_CATALOG = DATA_DIR / "catalog.json"
DEFAULT_URL_CACHE = DATA_DIR / "KKEC_urls.json"


def validate_game_dir(game_dir: Path) -> None:
    required = [game_dir / "Koikatu.exe", game_dir / "mods", game_dir / "UserData"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError("游戏目录不完整，缺少：" + "、".join(missing))


def perform_scan(
    game_dir: Path,
    catalog_path: Path,
    output_path: Path | None = None,
    full: bool = False,
) -> Path:
    validate_game_dir(game_dir)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    print("正在扫描人物卡、坐标卡和场景……", flush=True)
    requirements, card_errors = scan_user_data(
        game_dir / "UserData",
        DATA_DIR / "cards_cache.json",
        force=full,
    )
    print(f"读取到 {len(requirements)} 个不同的 zipmod GUID。", flush=True)
    print("正在扫描已安装 zipmod 的 manifest.xml……", flush=True)
    installed, mod_errors = scan_installed_mods(
        game_dir / "mods",
        DATA_DIR / "installed_mods_cache.json",
        force=full,
    )
    print(f"已识别 {len(installed)} 个已满足 GUID（包含迁移 GUID）。", flush=True)
    catalog = load_catalog(catalog_path)
    report = build_missing_report(
        game_dir,
        requirements,
        installed,
        catalog,
        scan_errors=[*card_errors, *mod_errors],
    )
    if output_path is None:
        output_path = REPORT_DIR / f"missing_mods_{datetime.now():%Y%m%d_%H%M%S}.json"
    save_report(output_path, report)
    print(
        f"报告已生成：{output_path}\n"
        f"缺失 {report['summary']['total']}，可下载 {report['summary']['downloadable']}，"
        f"待解析 {report['summary']['not_found']}。",
        flush=True,
    )
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Koikatu 缺失 zipmod 扫描与安装工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="扫描缺失模组并导出 JSON")
    scan.add_argument("--game", required=True, type=Path)
    scan.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    scan.add_argument("--output", type=Path)
    scan.add_argument("--full", action="store_true", help="忽略缓存并完整重扫")

    bootstrap = subparsers.add_parser("bootstrap", help="用现有报告和公开 URL 缓存初始化目录")
    bootstrap.add_argument("--report", required=True, type=Path)
    bootstrap.add_argument("--url-cache", required=True, type=Path)
    bootstrap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)

    resolve = subparsers.add_parser("resolve", help="为报告解析公开下载地址")
    resolve.add_argument("--report", required=True, type=Path)
    resolve.add_argument("--url-cache", type=Path, default=DEFAULT_URL_CACHE)
    resolve.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    resolve.add_argument("--output", type=Path)
    resolve.add_argument("--probe-limit", type=int, default=5)
    resolve.add_argument("--probe-workers", type=int, default=8)
    resolve.add_argument("--max-probes", type=int, default=40)

    install = subparsers.add_parser("install", help="下载、校验并安装报告中的 zipmod")
    install.add_argument("--game", required=True, type=Path)
    install.add_argument("--report", required=True, type=Path)
    install.add_argument("--workers", type=int, default=3)
    install.add_argument("--confirm", help="必须明确填写 INSTALL")

    crawl = subparsers.add_parser("crawl", help="更新 BetterRepack 公开文件目录")
    crawl.add_argument("--output", type=Path, default=DEFAULT_URL_CACHE)
    crawl.add_argument("--workers", type=int, default=8)

    run_all = subparsers.add_parser("run-all", help="扫描、解析并按确认安装")
    run_all.add_argument("--game", required=True, type=Path)
    run_all.add_argument("--url-cache", type=Path, default=DEFAULT_URL_CACHE)
    run_all.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    run_all.add_argument("--probe-limit", type=int, default=5)
    run_all.add_argument("--probe-workers", type=int, default=8)
    run_all.add_argument("--max-probes", type=int, default=40)
    run_all.add_argument("--workers", type=int, default=3)
    run_all.add_argument("--confirm", help="必须明确填写 INSTALL")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            perform_scan(args.game, args.catalog, args.output, full=args.full)
            return 0

        if args.command == "bootstrap":
            added, unresolved = bootstrap_catalog_from_report(args.report, args.url_cache, args.catalog)
            print(f"目录已更新：写入 {added} 项，报告中仍有 {unresolved} 项没有文件名。")
            return 0

        if args.command == "resolve":
            output = args.output or args.report.with_name(args.report.stem + "_resolved.json")
            report = resolve_report(
                args.report,
                args.url_cache,
                args.catalog,
                output,
                max(0, args.probe_limit),
                print,
                probe_workers=args.probe_workers,
                max_remote_probes=args.max_probes,
            )
            print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
            print(f"已保存：{output}")
            return 0

        if args.command == "install":
            validate_game_dir(args.game)
            if args.confirm != "INSTALL":
                raise ValueError("未明确确认安装；请添加 --confirm INSTALL")
            result = install_report(args.report, args.game, args.workers, print)
            print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
            return 0 if result["summary"]["failed"] == 0 else 2

        if args.command == "crawl":
            files = crawl_public_index(args.output, progress=print, workers=args.workers)
            print(f"公开目录索引完成，共 {len(files)} 个 zipmod：{args.output}")
            return 0

        if args.command == "run-all":
            report = perform_scan(args.game, args.catalog)
            resolved = report.with_name(report.stem + "_resolved.json")
            result = resolve_report(
                report,
                args.url_cache,
                args.catalog,
                resolved,
                max(0, args.probe_limit),
                print,
                probe_workers=args.probe_workers,
                max_remote_probes=args.max_probes,
            )
            print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
            if args.confirm != "INSTALL":
                print(f"未执行下载。检查报告后运行 install：{resolved}")
                return 0
            installed = install_report(resolved, args.game, args.workers, print)
            print(json.dumps(installed["summary"], ensure_ascii=False, indent=2))
            return 0 if installed["summary"]["failed"] == 0 else 2
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
