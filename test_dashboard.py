"""Run with: python3 -m unittest -v test_dashboard.py"""
import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from dashboard import Handler, JSONStream, Monitor, Server, normalize_connections, number, private_ipv4


def stats():
    # Socket traffic is deliberately higher than the synthetic aggregate.
    return ["connection_stats", {"connections": [
        {"adapterID": "speedify", "receiveBps": 280_000_000, "sendBps": 65_000_000},
        {"adapterID": "eth0", "connectionID": "one", "connected": True, "receiveBps": 300_000_000,
         "sendBps": 40_000_000, "latencyMs": 50},
        {"adapterID": "wwan0", "connectionID": "two", "connected": True, "receiveBps": 100_000_000,
         "sendBps": 40_000_000, "latencyMs": 67}]}]


class ParsingTests(unittest.TestCase):
    def test_fragmented_pretty_and_single_line_json(self):
        events = [["state", {"state": "CONNECTED"}], stats()]
        stream = JSONStream()
        output = []
        for char in json.dumps(events[0], indent=2) + "\n\n" + json.dumps(events[1]):
            output.extend(stream.feed(char))
        self.assertEqual(output, events)

    def test_correct_units_and_no_transport_double_count(self):
        total, links = normalize_connections(stats()[1])
        self.assertEqual(total, {"down": 280.0, "up": 65.0})
        self.assertEqual(sum(link["down"] for link in links), 400.0)

    def test_missing_aggregate_does_not_inflate_total(self):
        data = stats()[1]
        data["connections"].pop(0)
        total, links = normalize_connections(data)
        self.assertIsNone(total)
        self.assertEqual(len(links), 2)

    def test_socket_grouping_duplicates_and_sleeping_rtt(self):
        row = {"adapterID": "eth0", "connectionID": "a", "connected": True,
               "receiveBps": 10_000_000, "sendBps": 1_000_000, "latencyMs": 60}
        data = {"connections": [row, dict(row), dict(row, connectionID="b", latencyMs=55),
                                 dict(row, connectionID="c", sleeping=True, latencyMs=1)]}
        _, links = normalize_connections(data)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["down"], 30.0)
        self.assertEqual(links[0]["ping"], 55)

    def test_non_finite_and_negative_values_are_unknown(self):
        for value in (float("nan"), float("inf"), -1, True, None, "bad"):
            self.assertIsNone(number(value))

    def test_explicit_bind_only(self):
        for address in ("192.168.1.10", "10.4.1.2", "172.20.0.5", "127.0.0.1"):
            self.assertEqual(private_ipv4(address), address)
        for address in ("0.0.0.0", "8.8.8.8", "100.64.0.1", "::", "hostname"):
            with self.assertRaises(ValueError):
                # argparse.ArgumentTypeError is not ValueError; normalize for this check below.
                try:
                    private_ipv4(address)
                except Exception as exc:
                    raise ValueError from exc


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor("unused", "1.1.1.1")
        self.monitor.handle(["state", {"state": "CONNECTED"}])
        self.monitor.handle(stats())

    def test_disconnect_and_staleness_do_not_show_cached_rates(self):
        self.assertEqual(self.monitor.current()["down"], 280)
        self.monitor.updated -= 6
        self.assertIsNone(self.monitor.current()["down"])
        self.assertFalse(self.monitor.current()["live"])
        self.monitor.handle(stats())
        self.monitor.handle(["state", {"state": "LOGGED_IN"}])
        self.assertIsNone(self.monitor.current()["down"])
        self.assertEqual(self.monitor.current()["links"], [])

    def test_unknown_rates_and_real_idle_zero(self):
        self.monitor.handle(["connection_stats", {"connections": [{"adapterID": "speedify"}]}])
        self.assertFalse(self.monitor.current()["live"])
        self.monitor.handle(["connection_stats", {"connections": [{"adapterID": "speedify", "receiveBps": 0, "sendBps": 0}]}])
        self.assertTrue(self.monitor.current()["live"])
        self.assertEqual(self.monitor.current()["down"], 0)

    def test_history_cursor_recovery_and_bounded_memory(self):
        for _ in range(950):
            self.monitor.sample()
        self.assertEqual(len(self.monitor.history), 900)
        self.assertEqual(len(self.monitor.snapshot(948)["history"]), 2)
        self.assertEqual(len(self.monitor.snapshot(9999)["history"]), 900)
        self.assertEqual(self.monitor.snapshot(950)["history"], [])

    def test_link_history_retains_busy_sample_after_idle_and_disconnect(self):
        self.monitor.sample()
        idle = stats()
        for link in idle[1]["connections"]:
            link["receiveBps"] = link["sendBps"] = 0
        self.monitor.handle(idle)
        self.monitor.sample()
        self.monitor.handle(["state", {"state": "LOGGED_IN"}])
        self.monitor.sample()
        samples = self.monitor.snapshot()["history"]
        self.assertEqual(samples[0]["links"][0]["id"], "eth0")
        self.assertEqual(samples[0]["links"][0]["down"], 300)
        self.assertEqual(samples[1]["links"][0]["down"], 0)
        self.assertEqual(samples[2]["links"], [])
        self.assertEqual(len(samples[0]["links"]), 2)  # excludes synthetic aggregate

    def test_stale_samples_do_not_claim_link_measurements(self):
        self.monitor.updated -= 6
        self.monitor.sample()
        self.assertEqual(self.monitor.snapshot()["history"][0]["links"], [])

    def test_ping_parser_and_failure(self):
        def success(*args, **kwargs):
            self.monitor.stop.set()
            return subprocess.CompletedProcess(args, 0, "64 bytes: time=50.2 ms\n", "")
        with mock.patch("dashboard.subprocess.run", side_effect=success):
            self.monitor.ping_loop()
        self.assertEqual(self.monitor.current()["ping"], 50.2)
        self.monitor.stop.clear()
        def failure(*args, **kwargs):
            self.monitor.stop.set()
            raise FileNotFoundError
        with mock.patch("dashboard.subprocess.run", side_effect=failure):
            self.monitor.ping_loop()
        self.assertIsNone(self.monitor.current()["ping"])
        self.assertIn("iputils-ping", self.monitor.current()["ping_note"])

    def test_real_cli_subprocess_feed_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            cli = Path(temp, "fake_cli")
            cli.write_text("#!/usr/bin/env python3\nimport sys,json,time\n"
                           "args=sys.argv[1:]\n"
                           "if args == ['-s','show','currentserver']:\n"
                           " print(json.dumps({'tag':'us-nyc-8','dataCenter':'linode-nyc'}))\n"
                           "elif args == ['-s','show','adapters']:\n"
                           " print(json.dumps([{'adapterID':'eth0','name':'Starlink'}]))\n"
                           "elif args == ['-s','stats']:\n"
                           " print(json.dumps(['state',{'state':'CONNECTED'}]),flush=True)\n"
                           " while True:\n"
                           "  print(" + repr(json.dumps(stats())) + ",flush=True)\n"
                           "  time.sleep(.05)\n")
            cli.chmod(0o755)
            monitor = Monitor(str(cli), "1.1.1.1")
            monitor.ping_loop = lambda: monitor.stop.wait()
            monitor.start()
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    data = monitor.current()
                    if data["live"] and data["server"].get("tag") and data["links"][0]["name"] == "Starlink":
                        break
                    time.sleep(.02)
                self.assertTrue(data["live"])
                self.assertEqual(data["down"], 280)
                self.assertEqual(data["up"], 65)
                self.assertEqual(data["server"]["tag"], "us-nyc-8")
                self.assertEqual(data["links"][0]["name"], "Starlink")
            finally:
                monitor.close()


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.monitor = Monitor("unused", "1.1.1.1")
        cls.monitor.handle(["state", {"state": "CONNECTED"}])
        cls.monitor.handle(stats())
        cls.monitor.sample()
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.server.monitor = cls.monitor
        cls.server.allowed_hosts = {"127.0.0.1"}
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_page_and_api_share_same_origin_and_no_cache(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"trafficChart", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, headers, body = self.request("/api/live")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["history"][0]["down"], 280)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_host_origin_and_read_only_boundaries(self):
        self.assertEqual(self.request("/api/live", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.request("/api/live", headers={"Origin": "http://example.com"})[0], 403)
        self.assertEqual(self.request("/api/live", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/api/live", method="POST")[0], 501)
        self.assertEqual(self.request("/api/connect?server=us-nyc-4")[0], 404)
        self.assertEqual(self.request("/../../etc/passwd")[0], 404)
        self.assertEqual(self.request("/api/live?after=invalid")[0], 400)


if __name__ == "__main__":
    unittest.main()
