"""重启助手：等旧 bridge 彻底退出、端口让出来，再开一个新控制台跑 start-bridge.cmd。

由 /restart 以分离进程启动，所以旧 bridge 退出后它还在。
"""

import argparse
import ctypes
import os
import socket
import sys
import time

STILL_ACTIVE = 259
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "relaunch.log")


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def pid_alive(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def port_open(port, host="127.0.0.1", timeout=0.3):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        sock.close()


def wait_gone(pid, port, pid_timeout=120.0, port_timeout=30.0, step=0.4):
    """旧进程退出（含 worker 收尾）且端口空出来才算可以重启。"""
    deadline = time.time() + pid_timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(step)
    else:
        log("pid %s still alive after %.0fs; not relaunching" % (pid, pid_timeout))
        return False
    deadline = time.time() + port_timeout
    while time.time() < deadline:
        if not port_open(port):
            break
        time.sleep(step)
    else:
        log("port %s still busy after %.0fs; not relaunching" % (port, port_timeout))
        return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--settle", type=float, default=1.0)
    args = ap.parse_args(argv)
    log("waiting for pid %s to exit" % args.pid)
    if not wait_gone(args.pid, args.port):
        return 1
    time.sleep(max(0.0, args.settle))
    try:
        os.startfile(args.cmd)
    except OSError as exc:
        log("startfile failed: %s" % exc)
        return 1
    log("relaunched %s" % args.cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
