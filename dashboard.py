#!/usr/bin/env python3
"""Small, read-only LAN monitor for Speedify. Python 3.9+, standard library only."""
import argparse
import collections
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION = "1.1.0"
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in
                     ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))
STALE = 5.0
PAGE = Path(__file__).with_name("index.html")


def private_ipv4(value):
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("Use the Pi's numeric LAN IPv4 address.") from exc
    if not any(ip in net for net in PRIVATE_NETS):
        raise argparse.ArgumentTypeError("Bind to a private LAN address or 127.0.0.1, not a public/wildcard address.")
    return str(ip)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def mbps(value):
    value = number(value)
    return None if value is None else value / 1_000_000.0


class JSONStream:
    """Decode concatenated or pretty-printed JSON without guessing field boundaries."""
    def __init__(self):
        self.buffer = ""
        self.decoder = json.JSONDecoder()

    def feed(self, text):
        self.buffer += text
        if len(self.buffer) > 2_000_000:
            raise ValueError("CLI JSON buffer exceeded 2 MB")
        while self.buffer.strip():
            self.buffer = self.buffer.lstrip()
            try:
                value, end = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                return
            self.buffer = self.buffer[end:]
            yield value


def normalize_connections(payload):
    """Use the synthetic aggregate once; never add physical links to the total."""
    rows = payload.get("connections", [])
    if not isinstance(rows, list):
        raise ValueError("Expected a connections array")
    aggregate = None
    links = {}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        adapter = row.get("adapterID")
        if not isinstance(adapter, str) or not adapter:
            continue
        if adapter.lower() == "speedify":
            if aggregate is None:
                aggregate = {"down": mbps(row.get("receiveBps")),
                             "up": mbps(row.get("sendBps"))}
            continue
        # Some releases repeat a socket record; don't count that socket twice.
        identity = row.get("connectionUID") or row.get("connectionID")
        if identity:
            key = (adapter, str(identity))
            if key in seen:
                continue
            seen.add(key)
        link = links.setdefault(adapter, {"id": adapter, "down": 0.0, "up": 0.0,
                                          "ping": None, "connected": False, "sleeping": True})
        active = row.get("connected") is True
        sleeping = row.get("sleeping") is True
        link["connected"] = link["connected"] or active
        link["sleeping"] = link["sleeping"] and (sleeping or not active)
        if active:
            down, up = mbps(row.get("receiveBps")), mbps(row.get("sendBps"))
            link["down"] += down or 0.0
            link["up"] += up or 0.0
            latency = number(row.get("latencyMs"))
            if not sleeping and latency is not None:
                link["ping"] = latency if link["ping"] is None else min(link["ping"], latency)
    return aggregate, list(links.values())


class Monitor:
    def __init__(self, cli, ping_target, demo=False):
        self.cli, self.ping_target, self.demo = cli, ping_target, demo
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.history = collections.deque(maxlen=900)
        self.seq = 0
        self.state = "STARTING"
        self.aggregate = None
        self.links = []
        self.adapters = {}
        self.server = {}
        self.updated = 0.0
        self.message = "Waiting for the Speedify statistics feed."
        self.ping = None
        self.ping_updated = 0.0
        self.ping_error = "Waiting for a ping reply."
        self.proc = None
        self.threads = []

    def start(self):
        targets = (self.demo_loop,) if self.demo else (self.collect, self.metadata, self.ping_loop)
        for target in targets + (self.sample_loop,):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        with self.lock:
            proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        for thread in self.threads:
            thread.join(timeout=2)

    def handle(self, event):
        if not isinstance(event, list) or len(event) != 2:
            return
        kind, payload = event
        if not isinstance(payload, dict):
            return
        with self.lock:
            if kind == "state":
                self.state = str(payload.get("state", "UNKNOWN"))
                if self.state != "CONNECTED":
                    self.aggregate = None
                    self.links = []
                    self.updated = 0.0
            elif kind == "connection_stats":
                self.aggregate, self.links = normalize_connections(payload)
                self.updated = time.monotonic()
                self.message = "" if self.aggregate else "Speedify has not supplied its aggregate traffic record."

    def collect(self):
        while not self.stop.is_set():
            proc = None
            try:
                # Fixed, read-only CLI arguments; no commands are accepted from HTTP.
                proc = subprocess.Popen([self.cli, "-s", "stats"], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                        env=dict(os.environ, LC_ALL="C"))
                with self.lock:
                    self.proc = proc
                # Restart a stuck stream even if its process is still alive.
                def watchdog(child):
                    deadline = time.monotonic() + 15
                    while not self.stop.wait(2) and child.poll() is None:
                        with self.lock:
                            deadline = max(deadline, self.updated + 15)
                        if time.monotonic() > deadline:
                            child.terminate()
                            return
                threading.Thread(target=watchdog, args=(proc,), daemon=True).start()
                decoder = JSONStream()
                for line in proc.stdout:
                    if self.stop.is_set():
                        break
                    for event in decoder.feed(line):
                        self.handle(event)
                if not self.stop.is_set():
                    raise RuntimeError("Speedify statistics feed ended; retrying.")
            except (OSError, ValueError, RuntimeError) as exc:
                logging.warning("Statistics feed: %s", exc)
                with self.lock:
                    self.updated = 0.0
                    self.message = str(exc)
            finally:
                if proc:
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    if proc.stdout:
                        proc.stdout.close()
                with self.lock:
                    self.proc = None
            self.stop.wait(2)

    def metadata(self):
        while not self.stop.is_set():
            for what in ("currentserver", "adapters"):
                if self.stop.is_set():
                    return
                try:
                    result = subprocess.run([self.cli, "-s", "show", what],
                                            capture_output=True, text=True, timeout=5,
                                            env=dict(os.environ, LC_ALL="C"))
                    if result.returncode:
                        continue
                    value = json.loads(result.stdout)
                    with self.lock:
                        if what == "currentserver" and isinstance(value, dict):
                            self.server = {k: value.get(k) for k in ("tag", "friendlyName", "dataCenter")}
                        elif what == "adapters" and isinstance(value, list):
                            self.adapters = {a["adapterID"]: str(a.get("name") or a.get("description") or a["adapterID"])
                                             for a in value if isinstance(a, dict) and "adapterID" in a}
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            self.stop.wait(10)

    def ping_loop(self):
        while not self.stop.is_set():
            latency, error = None, "No ping reply (ICMP may be filtered)."
            try:
                result = subprocess.run(["ping", "-n", "-c", "1", "-W", "1", self.ping_target],
                                        capture_output=True, text=True, timeout=2,
                                        env=dict(os.environ, LC_ALL="C"))
                match = re.search(r"time\s*[=<]\s*([0-9.]+)\s*ms", result.stdout)
                if result.returncode == 0 and match:
                    latency, error = float(match.group(1)), ""
            except FileNotFoundError:
                error = "ping is not installed; install iputils-ping."
            except (OSError, ValueError, subprocess.TimeoutExpired):
                error = "Ping is unavailable; check ping permissions or routing."
            with self.lock:
                self.ping, self.ping_error = latency, error
                self.ping_updated = time.monotonic()
            self.stop.wait(2)

    def current(self):
        with self.lock:
            now = time.monotonic()
            fresh = self.updated > 0 and now - self.updated <= STALE
            valid_rates = self.aggregate is not None and all(self.aggregate.get(k) is not None for k in ("down", "up"))
            live = fresh and self.state == "CONNECTED" and valid_rates
            traffic = self.aggregate if live else {}
            ping = self.ping if now - self.ping_updated <= STALE else None
            links = [dict(link, name=self.adapters.get(link["id"], link["id"])) for link in self.links] if fresh else []
            if self.state != "CONNECTED":
                message = "Speedify is " + self.state.lower().replace("_", " ") + "."
            elif not fresh:
                message = self.message or "Live statistics are stale; waiting for Speedify."
            else:
                message = self.message
                if not valid_rates and not message:
                    message = "Speedify has not supplied valid aggregate traffic rates."
            return {"live": live, "state": self.state, "down": traffic.get("down"),
                    "up": traffic.get("up"), "ping": ping, "ping_target": self.ping_target,
                    "ping_note": self.ping_error, "server": dict(self.server), "links": links,
                    "message": message, "demo": self.demo, "version": VERSION}

    def sample(self):
        with self.lock:
            data = self.current()
            self.seq += 1
            self.history.append({"seq": self.seq, "t": time.time(), "down": data["down"],
                                 "up": data["up"], "ping": data["ping"],
                                 "links": [{k: link[k] for k in ("id", "name", "down", "up")}
                                           for link in data["links"] if link["connected"]]})

    def sample_loop(self):
        deadline = time.monotonic()
        while not self.stop.is_set():
            self.sample()
            deadline += 1
            # Avoid a burst of duplicate samples after a clock/suspend delay.
            if deadline < time.monotonic():
                deadline = time.monotonic() + 1
            self.stop.wait(max(0, deadline - time.monotonic()))

    def snapshot(self, after=0):
        with self.lock:
            data = self.current()
            if after > self.seq:
                after = 0  # A browser still has the cursor from before this process restarted.
            data.update({"seq": self.seq, "now": time.time(),
                         "history": [dict(p) for p in self.history if p["seq"] > after]})
            return data

    def demo_loop(self):
        start = time.monotonic()
        with self.lock:
            self.server = {"tag": "DEMO · us-nyc-8", "friendlyName": "Simulated New York server", "dataCenter": "linode-nyc"}
        while not self.stop.is_set():
            t = time.monotonic() - start
            down = 25 + 255 * max(0, math.sin(t / 9)) ** 2
            up = 3 + 62 * max(0, math.sin(t / 12 + 1)) ** 4
            self.handle(["state", {"state": "CONNECTED"}])
            self.handle(["connection_stats", {"connections": [
                {"adapterID": "speedify", "receiveBps": down * 1e6, "sendBps": up * 1e6},
                {"adapterID": "Starlink (demo)", "connected": True, "receiveBps": down * .8e6, "sendBps": up * .3e6, "latencyMs": 49},
                {"adapterID": "Cellular (demo)", "connected": True, "receiveBps": down * .2e6, "sendBps": up * .7e6, "latencyMs": 63}]}])
            with self.lock:
                self.ping, self.ping_updated = 50 + 5 * math.sin(t / 5), time.monotonic()
                self.ping_error = "Simulated ping; no real traffic is being measured."
            self.stop.wait(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "SpeedifyDashboard/" + VERSION

    def reply(self, code, body, content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        try:
            if not any(ipaddress.ip_address(self.client_address[0]) in n for n in PRIVATE_NETS):
                return self.reply(403, b"LAN clients only.\n")
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            if host not in self.server.allowed_hosts:
                return self.reply(403, b"Use this Pi's configured LAN address.\n")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                return self.reply(403, b"Same-origin requests only.\n")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.reply(403, b"Same-origin requests only.\n")
            request = urlsplit(self.path)
            if request.path in ("/", "/index.html"):
                return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if request.path == "/api/live":
                try:
                    after = max(0, int(parse_qs(request.query).get("after", ["0"])[0]))
                except (ValueError, IndexError):
                    return self.reply(400, b"Invalid history cursor.\n")
                data = json.dumps(self.server.monitor.snapshot(after), allow_nan=False, separators=(",", ":")).encode()
                return self.reply(200, data, "application/json; charset=utf-8")
            if request.path == "/favicon.ico":
                return self.reply(204, b"")
            self.reply(404, b"Not found.\n")
        except (OSError, ValueError):
            logging.exception("Dashboard request failed")
            self.reply(500, b"Dashboard request failed.\n")

    def log_message(self, fmt, *args):
        # Polling once per second should not write once per second to the SD card.
        if args and str(args[1] if len(args) > 1 else "") not in ("200", "204"):
            logging.info(fmt, *args)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", type=private_ipv4, default="127.0.0.1", help="Pi's LAN IPv4 address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--cli", default="/usr/share/speedify/speedify_cli")
    parser.add_argument("--ping-target", type=ipaddress.IPv4Address, default=ipaddress.IPv4Address("1.1.1.1"))
    parser.add_argument("--demo", action="store_true", help="Explicitly simulated data, for testing the page")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged port from 1024 to 65535.")
    if not args.demo and not os.access(args.cli, os.X_OK):
        parser.error("Speedify CLI not found/executable: " + args.cli)
    if not PAGE.is_file():
        parser.error("Keep index.html in the same directory as dashboard.py.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    monitor = Monitor(args.cli, str(args.ping_target), args.demo)
    with Server((args.bind, args.port), Handler) as server:
        server.monitor = monitor
        server.allowed_hosts = {args.bind, socket.gethostname().lower(), socket.gethostname().lower() + ".local"}
        if args.bind.startswith("127."):
            server.allowed_hosts.add("localhost")
        def shutdown(_sig, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        monitor.start()
        logging.info("Dashboard at http://%s:%s (read-only%s)", args.bind, args.port, ", DEMO" if args.demo else "")
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            monitor.close()


if __name__ == "__main__":
    main()
