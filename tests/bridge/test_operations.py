"""Tests for the operational surface an unattended deployment relies on."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path

from notion_bridge import metrics
from notion_bridge.app import create_app
from notion_bridge.sd_notify import (
    SystemdNotifier,
    WatchdogTask,
    watchdog_interval_seconds,
)
from notion_bridge.service import AccountReloadBusy, BridgeService
from tests.bridge.support import FakePool, build_settings


class MetricsRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = metrics.MetricsRegistry()

    def test_counters_gauges_and_histograms_render_prometheus_text(self) -> None:
        self.registry.increment("requests_total", labels={"endpoint": "/v1/responses"})
        self.registry.increment("requests_total", labels={"endpoint": "/v1/responses"})
        self.registry.set_gauge("accounts", 3, labels={"state": "ready"})
        self.registry.observe("duration_seconds", 1.5, labels={"model": "opus-5"})
        text = self.registry.render()
        self.assertIn('requests_total{endpoint="/v1/responses"} 2', text)
        self.assertIn('accounts{state="ready"} 3', text)
        self.assertIn('duration_seconds_bucket{model="opus-5",le="2.5"} 1', text)
        self.assertIn('duration_seconds_bucket{model="opus-5",le="1"} 0', text)
        self.assertIn('duration_seconds_count{model="opus-5"} 1', text)
        self.assertTrue(text.endswith("\n"))

    def test_label_values_are_escaped(self) -> None:
        self.registry.increment("events_total", labels={"reason": 'a"b\\c'})
        self.assertIn('reason="a\\"b\\\\c"', self.registry.render())

    def test_none_labels_are_dropped(self) -> None:
        self.registry.increment("events_total", labels={"kept": "yes", "dropped": None})
        self.assertIn('events_total{kept="yes"} 1', self.registry.render())

    def test_a_negative_increment_decrements_an_in_flight_counter(self) -> None:
        self.registry.increment("in_flight", labels={"endpoint": "/x"})
        self.registry.increment("in_flight", amount=-1, labels={"endpoint": "/x"})
        self.assertEqual(
            self.registry.snapshot()["counters"]['in_flight{endpoint="/x"}'], 0
        )


class SystemdNotifierTests(unittest.TestCase):
    def test_notifications_reach_a_unix_datagram_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            address = os.path.join(directory, "notify")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            listener.bind(address)
            listener.settimeout(2)
            try:
                notifier = SystemdNotifier(address)
                self.assertTrue(notifier.available)
                self.assertTrue(notifier.ready())
                self.assertTrue(notifier.watchdog())
                notifier.close()
                self.assertEqual(listener.recv(64), b"READY=1")
                self.assertEqual(listener.recv(64), b"WATCHDOG=1")
            finally:
                listener.close()

    def test_an_absent_notify_socket_is_a_no_op(self) -> None:
        notifier = SystemdNotifier("")
        self.assertFalse(notifier.available)
        self.assertFalse(notifier.ready())

    def test_the_watchdog_interval_is_half_of_watchdog_sec(self) -> None:
        self.assertEqual(watchdog_interval_seconds({"WATCHDOG_USEC": "120000000"}), 60)
        self.assertEqual(watchdog_interval_seconds({}), 30)
        self.assertEqual(watchdog_interval_seconds({"WATCHDOG_USEC": "nope"}), 30)


class WatchdogTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_pings_immediately_and_then_on_an_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            address = os.path.join(directory, "notify")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            listener.bind(address)
            listener.setblocking(False)
            try:
                task = WatchdogTask(SystemdNotifier(address), interval_seconds=0.01)
                task.start()
                await asyncio.sleep(0.05)
                await task.stop()
                pings = 0
                while True:
                    try:
                        listener.recv(64)
                    except BlockingIOError:
                        break
                    pings += 1
                # One immediate ping plus at least one interval ping.
                self.assertGreaterEqual(pings, 2)
            finally:
                listener.close()

    async def test_stopping_without_a_socket_is_safe(self) -> None:
        task = WatchdogTask(SystemdNotifier(""), interval_seconds=0.01)
        task.start()
        await task.stop()


class ServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_refuses_while_an_account_is_in_use(self) -> None:
        settings = build_settings()
        service = BridgeService(settings)

        class BusyPool(FakePool):
            async def busy_count(self) -> int:
                return 2

        service._pool = BusyPool(lambda _account_id: None)
        with self.assertRaises(AccountReloadBusy) as raised:
            await service.reload_accounts()
        self.assertEqual(raised.exception.busy, 2)

    async def test_reload_rebuilds_the_pool_from_disk(self) -> None:
        settings = build_settings()
        service = BridgeService(settings)
        service._pool = FakePool(lambda _account_id: None)
        status = await service.reload_accounts()
        # No credential files exist in the temporary account home.
        self.assertEqual(status["configured"], 0)
        self.assertFalse(service.has_accounts)

    async def test_account_status_is_shaped_the_same_without_a_pool(self) -> None:
        service = BridgeService(build_settings())
        empty = await service.account_status()
        service._pool = FakePool(lambda _account_id: None)
        populated = await service.account_status()
        self.assertEqual(set(empty), set(populated))


class HttpSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self.settings = build_settings()
        self.client_factory = lambda: TestClient(create_app(self.settings))

    def test_livez_readyz_and_healthz_report_consistent_state(self) -> None:
        with self.client_factory() as client:
            self.assertEqual(client.get("/livez").status_code, 200)
            ready = client.get("/readyz")
            self.assertEqual(ready.status_code, 503)
            self.assertIn("no accounts", ready.json()["reason"])
            health = client.get("/healthz").json()
            self.assertFalse(health["ok"])
            self.assertFalse(health["ready"])
            self.assertEqual(health["account_pool"]["configured"], 0)
            self.assertIn("settings", health)

    def test_metrics_expose_prometheus_text(self) -> None:
        with self.client_factory() as client:
            response = client.get("/metrics")
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/plain", response.headers["content-type"])
            self.assertIn("notion_bridge_accounts", response.text)

    def test_metrics_can_be_switched_off(self) -> None:
        from fastapi.testclient import TestClient

        disabled = build_settings(NOTION_METRICS_ENABLED="0")
        with TestClient(create_app(disabled)) as client:
            self.assertEqual(client.get("/metrics").status_code, 404)

    def test_admin_reload_can_be_switched_off(self) -> None:
        from fastapi.testclient import TestClient

        disabled = build_settings(NOTION_ADMIN_ENABLED="0")
        with TestClient(create_app(disabled)) as client:
            self.assertEqual(client.post("/admin/accounts/reload").status_code, 404)

    def test_every_endpoint_reports_no_accounts_the_same_way(self) -> None:
        with self.client_factory() as client:
            openai = client.post("/v1/responses", json={"input": "hi"})
            anthropic = client.post(
                "/v1/messages",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            chat = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            for response in (openai, anthropic, chat):
                self.assertEqual(response.status_code, 503)
            self.assertIn("error", openai.json())
            self.assertEqual(anthropic.json()["type"], "error")

    def test_models_are_listed_without_credentials(self) -> None:
        with self.client_factory() as client:
            data = client.get("/v1/models").json()["data"]
            self.assertEqual(
                [model["id"] for model in data],
                ["fable-5", "gpt-5.6-sol", "opus-5"],
            )

    def test_request_metrics_record_the_endpoint_and_status_class(self) -> None:
        metrics.registry.reset()
        with self.client_factory() as client:
            client.post("/v1/responses", json={"input": "hi"})
        counters = metrics.registry.snapshot()["counters"]
        self.assertIn(
            'notion_bridge_requests_total{endpoint="/v1/responses",status="5xx"}',
            counters,
        )

    def test_unknown_paths_are_not_served(self) -> None:
        with self.client_factory() as client:
            self.assertEqual(client.get("/").status_code, 404)
            # Interactive docs would expose the local surface for no benefit.
            self.assertEqual(client.get("/docs").status_code, 404)
            self.assertEqual(client.get("/openapi.json").status_code, 404)


class ProjectLayoutTests(unittest.TestCase):
    def test_the_package_lives_under_src(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertTrue((root / "src" / "notion_bridge" / "app.py").is_file())
        self.assertFalse((root / "bridge").exists())


if __name__ == "__main__":
    unittest.main()
