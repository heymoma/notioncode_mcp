"""Single source of truth for every runtime setting of the bridge.

Everything the service reads from the environment is declared, validated and
frozen here at startup. A misconfigured deployment therefore fails with one
explicit message instead of surfacing as a confusing 502 on the first request.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL_ID = "opus-5"
SUPPORTED_MODELS: tuple[str, ...] = ("fable-5", "gpt-5.6-sol", "opus-5")
MODEL_DISPLAY_NAMES: dict[str, str] = {
    "fable-5": "Fable 5 (Notion)",
    "gpt-5.6-sol": "GPT-5.6 Sol (Notion)",
    "opus-5": "Opus 5 (Notion)",
}
# Codex ships a fixed transport ID for Fable; the bridge maps it back.
CODEX_FABLE_MODEL_ID = "gpt-5.5"
REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
MAX_SUPPORTED_ACCOUNTS = 10


class SettingsError(RuntimeError):
    """The environment describes a deployment that cannot work."""


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default)).strip()


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _text(env, name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be a boolean value, got {raw!r}")


def _number(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> float:
    raw = _text(env, name)
    if not raw:
        value = default
    else:
        try:
            value = int(raw) if integer else float(raw)
        except ValueError as error:
            kind = "an integer" if integer else "a number"
            raise SettingsError(f"{name} must be {kind}, got {raw!r}") from error
    if minimum is not None and value < minimum:
        raise SettingsError(f"{name} must be >= {minimum:g}, got {value:g}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} must be <= {maximum:g}, got {value:g}")
    return value


def _integer(env: Mapping[str, str], name: str, default: int, **bounds: float) -> int:
    return int(_number(env, name, default, integer=True, **bounds))


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a `KEY=value` file, ignoring comments and blank lines."""
    values: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf8")
    except OSError:
        return values
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _runtime_endpoint(env: Mapping[str, str], project_root: Path) -> str | None:
    """Resolve the coding-tools MCP endpoint once, at startup.

    Preference order: an explicit URL, this process' own secret, then the
    legacy env file written by older installers.
    """
    explicit = _text(env, "NOTION_MCP_RUNTIME_URL")
    if explicit:
        return explicit.rstrip("/")
    port = _text(env, "NOTION_MCP_RUNTIME_PORT") or _text(env, "PORT") or "8787"
    secret = _text(env, "MCP_PATH_SECRET")
    if not secret:
        legacy = env.get("NOTION_RUNTIME_ENV")
        candidates = (
            [Path(legacy).expanduser()]
            if legacy
            else [
                project_root / ".runtime" / "env" / "mcp-runtime.env",
                project_root / "services" / "mcp-runtime" / ".env",
            ]
        )
        for candidate in candidates:
            values = read_env_file(candidate)
            secret = values.get("MCP_PATH_SECRET", "")
            if secret:
                port = values.get("PORT", port)
                break
    if not secret:
        return None
    return f"http://127.0.0.1:{port}/mcp/{secret}"


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    account_home: Path
    code_root: Path
    conversation_state_path: Path
    pool_state_path: Path

    host: str
    port: int
    log_level: str

    default_model: str
    forced_model: str
    reasoning_effort: str
    workflow_id: str

    max_accounts: int
    inference_timeout_seconds: float
    transient_cooldown_seconds: int
    denial_cooldown_seconds: int
    circuit_failure_window_seconds: int
    circuit_failure_threshold: int

    turn_affinity_ttl_seconds: int
    turn_affinity_max_entries: int
    conversation_ttl_seconds: int
    conversation_max_entries: int

    reasoning_heartbeat_seconds: float
    planner_max_steps: int
    planner_correction_attempts: int
    runtime_tool_timeout_seconds: float
    mcp_runtime_url: str | None

    metrics_enabled: bool
    admin_enabled: bool
    watchdog_enabled: bool

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        project_root: Path | None = None,
    ) -> Settings:
        env = os.environ if env is None else env
        root = (
            project_root
            or Path(_text(env, "NOTIONCODE_ROOT") or Path(__file__).resolve().parents[2])
        ).resolve()
        account_home = Path(
            _text(env, "NOTION_AGENT_HOME") or Path.home() / ".notionagents"
        ).expanduser()

        default_model = _text(env, "NOTION_DEFAULT_MODEL", DEFAULT_MODEL_ID).lower()
        if default_model not in SUPPORTED_MODELS:
            raise SettingsError(
                f"NOTION_DEFAULT_MODEL must be one of {', '.join(SUPPORTED_MODELS)}, "
                f"got {default_model!r}"
            )
        forced_model = _text(env, "NOTION_FORCE_MODEL").lower()
        if forced_model and forced_model not in SUPPORTED_MODELS:
            raise SettingsError(
                f"NOTION_FORCE_MODEL must be one of {', '.join(SUPPORTED_MODELS)}, "
                f"got {forced_model!r}"
            )
        effort = _text(env, "NOTION_REASONING_EFFORT", "high").lower()
        if effort not in REASONING_EFFORTS:
            raise SettingsError(
                f"NOTION_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}, "
                f"got {effort!r}"
            )
        log_level = _text(env, "NOTION_LOG_LEVEL", "INFO").upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise SettingsError(f"NOTION_LOG_LEVEL is not a logging level: {log_level!r}")

        host = _text(env, "NOTION_BRIDGE_HOST", "127.0.0.1")
        return cls(
            project_root=root,
            account_home=account_home,
            code_root=Path(_text(env, "CODE_ROOT") or Path.home()).expanduser().resolve(),
            conversation_state_path=account_home / "conversation-state.json",
            pool_state_path=account_home / "pool-state.json",
            host=host,
            port=_integer(env, "NOTION_BRIDGE_PORT", 8765, minimum=1, maximum=65535),
            log_level=log_level,
            default_model=default_model,
            forced_model=forced_model,
            reasoning_effort=effort,
            workflow_id=_text(env, "NOTION_WORKFLOW_ID"),
            max_accounts=_integer(
                env, "NOTION_MAX_ACCOUNTS", MAX_SUPPORTED_ACCOUNTS,
                minimum=1, maximum=MAX_SUPPORTED_ACCOUNTS,
            ),
            inference_timeout_seconds=_number(
                env, "NOTION_INFERENCE_TIMEOUT_SECONDS", 180, minimum=1, maximum=3600,
            ),
            transient_cooldown_seconds=_integer(
                env, "NOTION_TRANSIENT_COOLDOWN_SECONDS", 30, minimum=0, maximum=86400,
            ),
            denial_cooldown_seconds=_integer(
                env, "NOTION_DENIAL_COOLDOWN_SECONDS", 300, minimum=0, maximum=86400,
            ),
            circuit_failure_window_seconds=_integer(
                env, "NOTION_CIRCUIT_WINDOW_SECONDS", 30, minimum=1, maximum=3600,
            ),
            circuit_failure_threshold=_integer(
                env, "NOTION_CIRCUIT_THRESHOLD", 3, minimum=1, maximum=100,
            ),
            turn_affinity_ttl_seconds=_integer(
                env, "NOTION_TURN_AFFINITY_TTL_SECONDS", 2 * 60 * 60, minimum=60,
            ),
            turn_affinity_max_entries=_integer(
                env, "NOTION_TURN_AFFINITY_MAX_ENTRIES", 512, minimum=1,
            ),
            conversation_ttl_seconds=_integer(
                env, "NOTION_CONVERSATION_TTL_SECONDS", 30 * 24 * 60 * 60, minimum=60,
            ),
            conversation_max_entries=_integer(
                env, "NOTION_CONVERSATION_MAX_ENTRIES", 500, minimum=1,
            ),
            reasoning_heartbeat_seconds=_number(
                env, "NOTION_REASONING_HEARTBEAT_SECONDS", 10, minimum=1, maximum=600,
            ),
            planner_max_steps=_integer(
                env, "NOTION_PLANNER_MAX_STEPS", 20, minimum=1, maximum=200,
            ),
            planner_correction_attempts=_integer(
                env, "NOTION_PLANNER_CORRECTION_ATTEMPTS", 3, minimum=1, maximum=10,
            ),
            runtime_tool_timeout_seconds=_number(
                env, "NOTION_RUNTIME_TOOL_TIMEOUT_SECONDS", 120, minimum=1, maximum=1800,
            ),
            mcp_runtime_url=_runtime_endpoint(env, root),
            metrics_enabled=_flag(env, "NOTION_METRICS_ENABLED", True),
            admin_enabled=_flag(env, "NOTION_ADMIN_ENABLED", True),
            watchdog_enabled=_flag(
                env, "NOTION_WATCHDOG_ENABLED", bool(_text(env, "NOTIFY_SOCKET"))
            ),
        )

    @property
    def publicly_bound(self) -> bool:
        return self.host not in {"127.0.0.1", "localhost", "::1"}

    def summary(self) -> dict[str, object]:
        """Non-secret configuration, safe for logs and the health endpoint."""
        return {
            "host": self.host,
            "port": self.port,
            "default_model": self.default_model,
            "forced_model": self.forced_model or None,
            "reasoning_effort": self.reasoning_effort,
            "max_accounts": self.max_accounts,
            "inference_timeout_seconds": self.inference_timeout_seconds,
            "coding_tools_configured": self.mcp_runtime_url is not None,
            "custom_agent": bool(self.workflow_id),
            "metrics_enabled": self.metrics_enabled,
            "admin_enabled": self.admin_enabled,
            "watchdog_enabled": self.watchdog_enabled,
        }
