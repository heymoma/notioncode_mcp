"""Process entry point: `python -m notion_bridge`.

Configuration is read and validated before uvicorn binds anything, so a broken
environment produces one clear message instead of a service that starts and
then fails every request.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .logging_config import configure_logging
from .settings import Settings, SettingsError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="notion-bridge",
        description="Run the Notion AI compatibility bridge.",
    )
    parser.add_argument("--host", help="Override NOTION_BRIDGE_HOST.")
    parser.add_argument("--port", type=int, help="Override NOTION_BRIDGE_PORT.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and exit without binding a port.",
    )
    arguments = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except SettingsError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    host = arguments.host or settings.host
    port = arguments.port or settings.port
    configure_logging(settings.log_level)

    if arguments.check:
        for key, value in settings.summary().items():
            print(f"{key}={value}")
        print(f"bind={host}:{port}")
        print("configuration is valid")
        return 0

    if host not in {"127.0.0.1", "localhost", "::1"}:
        logging.getLogger("uvicorn.error.notion_bridge").warning(
            "Binding to %s exposes Notion inference beyond this machine; "
            "keep 127.0.0.1 unless a trusted reverse proxy terminates access.",
            host,
        )

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level=settings.log_level.lower(),
        access_log=False,
        # Long Notion turns must not be cut off by a proxy-style idle timeout.
        timeout_keep_alive=75,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
