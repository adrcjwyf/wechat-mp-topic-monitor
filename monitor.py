# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 adrcjwyf
"""Local launcher for the topic monitor. State belongs to this installation."""

import argparse
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
from urllib.request import ProxyHandler, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
DATA = ROOT / "data"
STATE = DATA / "service.json"
STOP = DATA / "stop.request"
HOST = "127.0.0.1"
DEFAULT_PORT = 18767
LOCAL = build_opener(ProxyHandler({}))
sys.path.insert(0, str(APP))
from __version__ import PROJECT_NAME, credit


def endpoint():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        port = int(state["port"])
        if not 0 < port < 65536:
            return None
        url = f"http://{HOST}:{port}"
        with LOCAL.open(url + "/_instance", timeout=1) as response:
            if response.read(100).decode("ascii") == state["instance"]:
                return url, state["instance"]
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return None


def worker_command():
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if os.name == "nt" and windowless.is_file():
        exe = windowless
    return [str(exe), "-B", "-X", "utf8", str(Path(__file__).resolve()), "serve"]


def register(enabled):
    if os.name != "nt":
        if enabled:
            raise ValueError("登录自启仅适用于 Windows。")
        return
    import winreg

    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    suffix = hashlib.sha256(os.path.normcase(str(ROOT)).encode()).hexdigest()[:12]
    name = f"{PROJECT_NAME}-{suffix}"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            command = subprocess.list2cmdline(worker_command())
            if len(command) > 260:
                raise ValueError("登录自启命令过长，请将项目移至较短的路径。")
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


@contextmanager
def sole_worker():
    with (DATA / "service.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            def acquire():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            yield False
        else:
            try:
                yield True
            finally:
                release()


def run_server():
    from wsgiref.simple_server import make_server

    os.environ.update(WECHAT_MP_DATA=str(DATA), WECHAT_MP_DB=str(DATA / "wechat_mp.db"))
    os.chdir(APP)
    import web_app

    app, scheduler = web_app.build_runtime()
    instance = secrets.token_hex(18)

    def local_app(environ, respond):
        if environ.get("HTTP_HOST", "").split(":")[0].lower() not in {HOST, "localhost"}:
            respond("403 Forbidden", [("Content-Type", "text/plain")])
            return [b"Local access only"]
        if environ["PATH_INFO"] == "/_instance":
            respond("200 OK", [("Content-Type", "text/plain"), ("Cache-Control", "no-store")])
            return [instance.encode("ascii")]
        return app(environ, respond)

    class LocalServer(web_app.ThreadingWSGIServer):
        allow_reuse_address = False

        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    def bind(port):
        return make_server(HOST, port, local_app, server_class=LocalServer,
                           handler_class=web_app.QuietHandler)

    try:
        server = bind(DEFAULT_PORT)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", 0) not in {10013, 10048}:
            raise
        server = bind(0)
    with server:
        server.timeout = 0.5
        STOP.unlink(missing_ok=True)
        scheduler.start()
        try:
            temporary = STATE.with_suffix(".tmp")
            temporary.write_text(json.dumps({"port": server.server_port, "instance": instance}),
                                 encoding="utf-8")
            temporary.replace(STATE)
            while True:
                if STOP.exists() and STOP.read_text(encoding="ascii") == instance:
                    break
                server.handle_request()
                if app.idle_exit():
                    break
        finally:
            STATE.unlink(missing_ok=True)
            STOP.unlink(missing_ok=True)
            scheduler.stop()
            scheduler.join()


def worker():
    with sole_worker() as acquired:
        if not acquired:
            return
        log = DATA / "monitor.log"
        if log.exists() and log.stat().st_size > 1_000_000:
            log.replace(DATA / "monitor.previous.log")
        with log.open("a", encoding="utf-8", buffering=1) as stream:
            with redirect_stdout(stream), redirect_stderr(stream):
                try:
                    run_server()
                except Exception:
                    traceback.print_exc()
                    raise SystemExit(1)


def start(args):
    active = endpoint()
    if not active:
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                   else {"start_new_session": True})
        child = subprocess.Popen(worker_command(), cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not active:
            time.sleep(.2)
            active = endpoint()
            if child.poll() not in (None, 0):
                break
    if not active:
        raise RuntimeError(f"本地服务未能启动，请查看 {DATA / 'monitor.log'}。")
    if args.autostart:
        register(True)
    config = json.loads((DATA / "config.json").read_text("utf-8-sig"))
    with closing(sqlite3.connect(DATA / "wechat_mp.db")) as db:
        has_history = bool(db.execute("SELECT 1 FROM articles LIMIT 1").fetchone())
    address = active[0] + ("/target" if config.get("accounts") or has_history else "/settings")
    print(f"本机监测页 {address}")
    if not args.no_browser:
        webbrowser.open(address)
    return 0


def stop(unregister):
    if unregister:
        register(False)
    active = endpoint()
    if active:
        STOP.write_text(active[1], encoding="ascii")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        with sole_worker() as acquired:
            if acquired:
                print("监测已停止。数据保存在 data 文件夹。")
                return
        time.sleep(.25)
    raise RuntimeError("后台仍在结束同步，请稍后再移动文件夹。")


def main():
    parser = argparse.ArgumentParser(description="本地公众号专题监测")
    parser.add_argument("--version", action="version", version=credit())
    parser.add_argument("action", nargs="?", choices=("start", "serve", "stop", "status"), default="start")
    startup = parser.add_mutually_exclusive_group()
    startup.add_argument("--autostart", action="store_true", help="开启 Windows 登录自启")
    startup.add_argument("--no-autostart", action="store_true", help="跳过对登录自启项的修改")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.action == "status":
        active = endpoint()
        print(active[0] if active else "监测程序未运行。")
        return 0 if active else 1
    DATA.mkdir(exist_ok=True)
    if args.action == "serve":
        worker()
    elif args.action == "stop":
        stop(not args.no_autostart)
    else:
        return start(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        raise SystemExit(1)

