# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 adrcjwyf
"""Build and launch-test a clean Windows x64 release entirely in a temporary tree."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import ProxyHandler, build_opener, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from __version__ import VERSION
from export_package import FOLDER, MANIFEST, build, config_bytes, sha256

EMBED_NAME = "python-3.12.10-embed-amd64.zip"
EMBED_URL = f"https://www.python.org/ftp/python/3.12.10/{EMBED_NAME}"
EMBED_HASH = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"


def stage_runtime(work, python_zip, wheelhouse):
    archive = python_zip or work / EMBED_NAME
    if python_zip is None:
        with urlopen(EMBED_URL, timeout=60) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
    if sha256(archive) != EMBED_HASH:
        raise ValueError(f"{EMBED_NAME} 的 SHA-256 与锁定值不一致。")
    runtime = work / "runtime"
    with ZipFile(archive) as z:
        z.extractall(runtime)
    (runtime / "python312._pth").write_text("python312.zip\n.\nLib/site-packages\n", encoding="ascii")

    packages = runtime / "Lib" / "site-packages"
    command = [
        sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
        "--require-hashes", "--only-binary=:all:", "--no-compile", "--no-warn-script-location",
        "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp",
        "--abi", "cp312", "--target", str(packages), "-r", str(ROOT / "tools/windows-runtime.lock"),
    ]
    if wheelhouse:
        command.extend(["--no-index", "--find-links", str(wheelhouse)])
    subprocess.run(command, check=True)
    # pip-generated console launchers can contain the build host's path.
    for directory in ("bin", "Scripts"):
        launchers = packages / directory
        if launchers.is_dir() and launchers.resolve().is_relative_to(work.resolve()):
            shutil.rmtree(launchers)
    manifest = {file.relative_to(runtime).as_posix(): sha256(file)
                for file in sorted(runtime.rglob("*")) if file.is_file()}
    (work / MANIFEST).write_bytes(config_bytes(manifest))
    return runtime


def smoke_test(path, work):
    with ZipFile(path) as archive:
        archive.extractall(work / "smoke")
    folder = work / "smoke" / FOLDER
    args = [str(folder / "runtime/python.exe"), "-B", "-X", "utf8", str(folder / "monitor.py")]
    opener = build_opener(ProxyHandler({}))
    try:
        subprocess.run([*args, "start", "--no-autostart", "--no-browser"], check=True, cwd=folder)
        status = subprocess.check_output([*args, "status"], cwd=folder, encoding="utf-8").strip()
        for route in ("/target", "/all", "/rules", "/settings", "/history-ui.js", "/tags-ui.js"):
            with opener.open(status + route, timeout=10) as response:
                if response.status != 200 or not response.read(100):
                    raise ValueError(f"启动检查失败：{route}")
        config = json.loads((folder / "data/config.json").read_text("utf-8"))
        if config["accounts"] or config["key"] or config["interval_hours"]:
            raise ValueError("首启配置应无账号、无 Key，且为手动模式。")
    finally:
        subprocess.run([*args, "stop", "--no-autostart"], check=True, cwd=folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / f"{FOLDER}-{VERSION}-windows-x64.zip")
    parser.add_argument("--python-zip", type=Path, help=f"本地官方 {EMBED_NAME}，省略时自动下载")
    parser.add_argument("--wheelhouse", type=Path, help="包含锁定依赖 wheel 的目录，用于离线构建")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("请在 Windows x64 上构建，以便运行包内程序完成启动验收。")
    target = args.output.resolve()
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mp-build-") as temporary:
        work = Path(temporary)
        runtime = stage_runtime(work, args.python_zip, args.wheelhouse)
        artifact = work / "release.zip"
        build(ROOT, artifact, runtime_dir=runtime)
        smoke_test(artifact, work)
        # Target may be on a different volume. Stage next to it before publishing.
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".partial", delete=False) as f:
            staged = Path(f.name)
        try:
            shutil.copyfile(artifact, staged)
            staged.rename(target)  # Windows rename rejects an existing target.
        finally:
            staged.unlink(missing_ok=True)
    print(f"已通过便携版首启检查：{target}\nSHA-256：{sha256(target)}")


if __name__ == "__main__":
    main()

