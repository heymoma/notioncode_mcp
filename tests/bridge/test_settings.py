from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from notion_bridge.settings import Settings, SettingsError, read_env_file


def settings(**environment: str) -> Settings:
    return Settings.from_env(
        {"NOTION_AGENT_HOME": tempfile.mkdtemp(prefix="notion-settings-"), **environment},
        project_root=Path("/opt/notioncode"),
    )


class SettingsValidationTests(unittest.TestCase):
    def test_defaults_are_loopback_and_opus(self) -> None:
        configured = settings()
        self.assertEqual(configured.host, "127.0.0.1")
        self.assertEqual(configured.port, 8765)
        self.assertEqual(configured.default_model, "opus-5")
        self.assertEqual(configured.reasoning_effort, "high")
        self.assertFalse(configured.publicly_bound)

    def test_an_unsupported_forced_model_fails_fast(self) -> None:
        with self.assertRaisesRegex(SettingsError, "NOTION_FORCE_MODEL"):
            settings(NOTION_FORCE_MODEL="gpt-4o")

    def test_a_non_numeric_timeout_fails_fast(self) -> None:
        with self.assertRaisesRegex(SettingsError, "must be a number"):
            settings(NOTION_INFERENCE_TIMEOUT_SECONDS="soon")

    def test_out_of_range_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "must be <= 65535"):
            settings(NOTION_BRIDGE_PORT="70000")
        with self.assertRaisesRegex(SettingsError, "must be <= 10"):
            settings(NOTION_MAX_ACCOUNTS="25")

    def test_an_unparseable_boolean_is_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "must be a boolean"):
            settings(NOTION_METRICS_ENABLED="maybe")

    def test_a_non_loopback_host_is_reported_as_public(self) -> None:
        self.assertTrue(settings(NOTION_BRIDGE_HOST="0.0.0.0").publicly_bound)

    def test_the_runtime_endpoint_is_built_from_the_secret(self) -> None:
        configured = settings(MCP_PATH_SECRET="abc123", NOTION_MCP_RUNTIME_PORT="9001")
        self.assertEqual(configured.mcp_runtime_url, "http://127.0.0.1:9001/mcp/abc123")

    def test_an_explicit_runtime_url_wins(self) -> None:
        configured = settings(
            MCP_PATH_SECRET="ignored",
            NOTION_MCP_RUNTIME_URL="http://127.0.0.1:1234/mcp/other/",
        )
        self.assertEqual(configured.mcp_runtime_url, "http://127.0.0.1:1234/mcp/other")

    def test_a_legacy_env_file_still_provides_the_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "mcp-runtime.env"
            legacy.write_text("# comment\nPORT=8999\nMCP_PATH_SECRET=from-file\n")
            configured = settings(NOTION_RUNTIME_ENV=str(legacy))
            self.assertEqual(
                configured.mcp_runtime_url, "http://127.0.0.1:8999/mcp/from-file"
            )

    def test_a_missing_runtime_endpoint_is_not_fatal(self) -> None:
        # The bridge still serves plain chat turns without coding tools, so an
        # unconfigured runtime must not stop the process from starting.
        self.assertIsNone(settings(NOTION_RUNTIME_ENV="/nonexistent").mcp_runtime_url)

    def test_the_summary_contains_no_secret(self) -> None:
        summary = settings(MCP_PATH_SECRET="super-secret-value").summary()
        self.assertNotIn("super-secret-value", str(summary))
        self.assertTrue(summary["coding_tools_configured"])


class EnvFileTests(unittest.TestCase):
    def test_quotes_comments_and_blank_lines_are_handled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.env"
            path.write_text(
                '\n# a comment\nA=1\nB="two"\nC=\'three\'\nnot-an-assignment\n'
            )
            self.assertEqual(
                read_env_file(path), {"A": "1", "B": "two", "C": "three"}
            )

    def test_a_missing_file_is_empty_rather_than_an_error(self) -> None:
        self.assertEqual(read_env_file(Path("/nonexistent/service.env")), {})


if __name__ == "__main__":
    unittest.main()
