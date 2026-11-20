# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 adrcjwyf
"""Export the approved source files, optionally adding a live SQLite snapshot."""

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from __version__ import AUTHOR, LICENSE_ID, PROJECT_NAME, REPO_URL, SCHEMA_VERSION, VERSION

FOLDER = PROJECT_NAME.lower()
FILES = (
    ".gitignore", "LICENSE", "NOTICE", "README.md", "使用说明.txt", "requirements.txt",
    "monitor.py", "启动自动监测.cmd", "停止并取消自启.cmd", "导出分发包.cmd",
    "config.example.json", "tools/build_release.py", "tools/windows-runtime.lock",
    "presets/general_blank.json", "presets/example_policy_research.json",
    "app/LICENSE", "app/__version__.py", "app/articles.sql", "app/billing.py","app/collection.py",
    "app/configuration.py", "app/export_package.py", "app/history_coverage.py",
    "app/history-ui.js", "app/tags-ui.js", "app/monetary_topic.py", "app/platform_source.py",
    "app/platform_store.py", "app/source_adapters.py", "app/review.py", "app/topic_engine.py",
    "app/web_app.py", "app/templates/all.html", "app/templates/article.html",
    "app/templates/base.html", "app/templates/rules_editor.html", "app/templates/filter.html", "app/templates/history_preview.html",
    "app/templates/notice.html", "app/templates/settings.html", "app/templates/topic.html",
)
MODES = {1: "纯程序", 2: "历史库与无凭据配置", 3: "历史库与私人配置"}
MANIFEST = "runtime-manifest.json"


def config_bytes(values):
    return (json.dumps(values, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def metadata():
    return (f"生成工具：{PROJECT_NAME} v{VERSION}\n作者：{AUTHOR}\n仓库：{REPO_URL}\n"
            f"数据库结构版本：{SCHEMA_VERSION}\n项目许可证：{LICENSE_ID}\n")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_runtime(archive, directory):
    manifest = json.loads((directory.parent / MANIFEST).read_text("utf-8"))
    if "python.exe" not in manifest or "pythonw.exe" not in manifest:
        raise ValueError("便携运行库清单不完整。")
    for name, digest in sorted(manifest.items()):
        parts = PurePosixPath(name).parts
        if not parts or "\\" in name or ":" in name or ".." in parts or parts[0] == "/":
            raise ValueError("运行库清单包含无效路径。")
        file = (directory / name).resolve()
        if not file.is_relative_to(directory.resolve()) or sha256(file) != digest:
            raise ValueError(f"运行库校验失败：{name}")
        archive.write(file, f"{FOLDER}/runtime/{name}")
    archive.writestr(f"{FOLDER}/{MANIFEST}", config_bytes(manifest))


def add_snapshot(archive, root):
    from review import stream_rows

    source = root / "data" / "wechat_mp.db"
    if not source.is_file():
        raise ValueError("请先启动程序以创建本地数据库。")
    with tempfile.TemporaryDirectory() as work:
        snapshot = Path(work) / "wechat_mp.db"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(snapshot)) as db:
                live.backup(db, pages=256, sleep=.05)
                db.execute("PRAGMA journal_mode=DELETE")
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("数据库快照未通过完整性检查。")
                db.row_factory = sqlite3.Row
                with archive.open(f"{FOLDER}/数据快照/全量文章.csv", "w", force_zip64=True) as output:
                    for chunk in stream_rows(db, True, {}):
                        output.write(chunk)
        archive.write(snapshot, f"{FOLDER}/data/wechat_mp.db")
    guide = (
        "# 数据快照\n\n" + metadata() + "\n全量文章.csv 与随包 SQLite 数据库来自同一次在线备份。"
        "在表格软件中以 UTF-8 打开 CSV，按 publish_date 查看北京时间的发布日期。"
        "CSV 对可能被识别为公式的单元格加了文本前缀。\n\n"
        "articles 保存账号、标题、摘要、正文、原文链接和专题判定，coverage 标明正文获取情况。"
        "account_state 保存账号与断点。history_pages 保存历史列表响应。"
        "history_coverage 记录已覆盖的历史范围。app_meta 保存生成工具信息和已保存的专题规则。\n"
    )
    archive.writestr(f"{FOLDER}/数据快照/数据库字段说明.md", guide)


def build(root, target, mode=1, *, runtime_dir=None):
    root, target = Path(root).resolve(), Path(target).resolve()
    if mode not in MODES:
        raise ValueError("请选择模式 1、2 或 3。")
    if target.exists():
        raise FileExistsError(f"目标已存在：{target.name}")
    if runtime_dir is None and (root / "runtime").is_dir():
        runtime_dir = root / "runtime"
    config = None
    if mode in (2, 3):
        from configuration import SECRETS, validate

        config = validate(json.loads((root / "data/config.json").read_text("utf-8-sig")))
        if mode == 2:
            config.update(dict.fromkeys(SECRETS, ""))
            config["interval_hours"] = 0
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as out:
        temporary = Path(out.name)
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED, compresslevel=6) as archive:
            for name in FILES:
                file = (root / name).resolve()
                if not file.is_relative_to(root):
                    raise ValueError(f"源码文件位于项目目录之外：{name}")
                if name == "使用说明.txt":
                    _, divider, body = file.read_text("utf-8-sig").partition("\n\n")
                    if not divider:
                        raise ValueError("使用说明缺少标题与正文之间的空行。")
                    archive.writestr(f"{FOLDER}/{name}", metadata() + "\n" + body)
                else:
                    archive.write(file, f"{FOLDER}/{name}")
            if runtime_dir is not None:
                add_runtime(archive, Path(runtime_dir))
            if config is not None:
                add_snapshot(archive, root)
                archive.writestr(f"{FOLDER}/data/config.json", config_bytes(config))
            kind = "Windows x64 便携版" if runtime_dir else "Python 3.10+ 源码版"
            archive.writestr(f"{FOLDER}/发行信息.txt",
                             f"{metadata()}分发形式：{kind}\n导出内容：{MODES[mode]}\n")
        with ZipFile(temporary) as archive:
            if archive.testzip():
                raise ValueError("ZIP 校验失败。")
        # Reserve the destination exclusively. A competing export is never overwritten.
        with target.open("xb"):
            pass
        try:
            os.replace(temporary, target)
        except OSError:
            target.unlink(missing_ok=True)
            raise
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="导出专题监测程序与历史快照")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--mode", type=int, choices=MODES)
    modes.add_argument("--clean", action="store_true", help="等同于 --mode 1")
    parser.add_argument("--output", type=Path, help="目标 ZIP 文件")
    parser.add_argument("--include-private-config", action="store_true", help="确认模式 3 包含私人凭据")
    args = parser.parse_args()
    mode = 1 if args.clean else args.mode
    if mode is None:
        for index, title in MODES.items():
            print(f"{index}. {title}")
        answer = input("输入导出模式（回车取消）：").strip()
        if answer not in {"1", "2", "3"}:
            return
        mode = int(answer)
    if mode == 3 and not args.include_private_config:
        if input("收件人可使用包内的 API Key 和机器人地址。输入 YES 确认：").strip() != "YES":
            return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = args.output or ROOT.parent / f"{FOLDER}-{VERSION}-{MODES[mode]}-{stamp}.zip"
    build(ROOT, target, mode)
    print(f"已导出 {target}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(f"导出失败：{exc}") from None

