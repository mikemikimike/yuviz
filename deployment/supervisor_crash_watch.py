"""Stops a service that keeps crashing after startup. stdout is the supervisord protocol; log to stderr."""
import argparse
import http.client
import os
import socket
import subprocess
import sys
import time
import xmlrpc.client
from collections import defaultdict, deque

RUNNING_STATES = {"STARTING", "RUNNING", "BACKOFF"}


class CrashCounter:
    def __init__(self, max_crashes: int, window_s: float):
        self.max_crashes = max_crashes
        self.window_s = window_s
        self._exits: dict[str, deque[float]] = defaultdict(deque)

    def record(self, name: str, now: float) -> int:
        exits = self._exits[name]
        exits.append(now)
        while exits and now - exits[0] > self.window_s:
            exits.popleft()
        return len(exits)

    def reset(self, name: str) -> None:
        self._exits[name].clear()


def parse_tokens(line: str) -> dict[str, str]:
    return dict(token.split(":", 1) for token in line.split())


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


class _UnixTransport(xmlrpc.client.Transport):
    def __init__(self, path: str):
        super().__init__()
        self._path = path

    def make_connection(self, host):
        return _UnixHTTPConnection(self._path)


def rpc_proxy(server_url: str):
    if server_url.startswith("unix://"):
        return xmlrpc.client.ServerProxy("http://localhost", transport=_UnixTransport(server_url[len("unix://"):]))
    return xmlrpc.client.ServerProxy(server_url.rstrip("/") + "/RPC2")


def log(msg: str) -> None:
    sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    sys.stderr.flush()


def notify(msg: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "yuviz crash-watch"'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)


def stop_service(proxy, name: str, settle_s: float = 15.0) -> bool:
    # supervisord is mid-restart; wait for a stoppable state.
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        state = proxy.supervisor.getProcessInfo(name)["statename"]
        if state in RUNNING_STATES:
            try:
                proxy.supervisor.stopProcess(name, False)
                return True
            except xmlrpc.client.Fault:
                pass
        elif state in {"STOPPED", "FATAL"}:
            return True
        time.sleep(0.2)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-crashes", type=int, default=3)
    parser.add_argument("--window", type=float, default=600, help="seconds")
    args = parser.parse_args()

    counter = CrashCounter(args.max_crashes, args.window)
    proxy = rpc_proxy(os.environ["SUPERVISOR_SERVER_URL"])
    log(f"watching: stop a service after {args.max_crashes} unexpected exits within {args.window:.0f}s")
    while True:
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return
        header = parse_tokens(line)
        payload = sys.stdin.read(int(header.get("len", 0)))
        first_line = payload.split("\n", 1)[0]
        event = parse_tokens(first_line) if first_line else {}
        name = event.get("processname", "")
        if header.get("eventname") == "PROCESS_STATE_EXITED" and event.get("expected") == "0":
            n = counter.record(name, time.monotonic())
            log(f"{name} exited unexpectedly ({n} within {args.window:.0f}s)")
            if n >= args.max_crashes:
                counter.reset(name)
                stopped = stop_service(proxy, name)
                msg = (f"{name} crashed {args.max_crashes} times within {args.window:.0f}s; "
                       + ("stopped it. Fix the cause, then: supervisorctl start " + name if stopped
                          else "could not stop it, check supervisorctl status"))
                log(msg)
                notify(msg)
        sys.stdout.write("RESULT 2\nOK")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
