"""Minimal `sd_notify` client so systemd can supervise a hung bridge.

`Restart=always` only recovers a process that exits. A bridge whose event loop
is wedged keeps the port open and answers nothing, which is the failure mode
that actually hurts an unattended deployment. Feeding the systemd watchdog
turns that case into an automatic restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from pathlib import Path

log = logging.getLogger("uvicorn.error.notion_bridge")


class SystemdNotifier:
    """Sends datagrams to `$NOTIFY_SOCKET`; a no-op when unset."""

    def __init__(self, address: str | None = None) -> None:
        self.address = address if address is not None else os.getenv("NOTIFY_SOCKET", "")
        self._socket: socket.socket | None = None

    @property
    def available(self) -> bool:
        return bool(self.address)

    def _target(self) -> str:
        # A leading '@' selects the Linux abstract namespace.
        return "\0" + self.address[1:] if self.address.startswith("@") else self.address

    def notify(self, message: str) -> bool:
        if not self.available:
            return False
        try:
            if self._socket is None:
                self._socket = socket.socket(
                    socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC
                )
            self._socket.sendto(message.encode("utf8"), self._target())
            return True
        except OSError as error:
            log.warning("Could not notify systemd (%s): %s", message.split("=")[0], error)
            self.close()
            return False

    def ready(self) -> bool:
        return self.notify("READY=1")

    def watchdog(self) -> bool:
        return self.notify("WATCHDOG=1")

    def stopping(self) -> bool:
        return self.notify("STOPPING=1")

    def status(self, text: str) -> bool:
        return self.notify(f"STATUS={text}")

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None


def watchdog_interval_seconds(env: dict[str, str] | None = None) -> float:
    """Half of `WatchdogSec`, the interval systemd documents for keep-alives."""
    values = os.environ if env is None else env
    raw = str(values.get("WATCHDOG_USEC", "")).strip()
    if not raw.isdigit() or int(raw) <= 0:
        return 30.0
    return max(1.0, int(raw) / 2_000_000)


class WatchdogTask:
    """Background keep-alive loop bound to the application lifespan."""

    def __init__(
        self,
        notifier: SystemdNotifier,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        self._notifier = notifier
        self._interval = interval_seconds or watchdog_interval_seconds()
        self._task: asyncio.Task[None] | None = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self._notifier.watchdog()

    def start(self) -> None:
        if not self._notifier.available or self._task is not None:
            return
        # Ping immediately so a slow first interval cannot trip the watchdog.
        self._notifier.watchdog()
        self._task = asyncio.create_task(self._loop(), name="systemd-watchdog")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def systemd_credential_home() -> Path | None:
    """Directory of credentials passed via systemd `LoadCredential=`, if any."""
    directory = os.getenv("CREDENTIALS_DIRECTORY", "")
    return Path(directory) if directory else None
