"""Env-driven configuration and logging setup (spec 11 & 12).

Field names are the env var names lowercased — pydantic-settings matches
case-insensitively, so ``BRIDGE_API_KEY`` -> ``bridge_api_key``,
``NTFY_TOPIC`` -> ``ntfy_topic``. A local ``.env`` is read if present.

Printer config deliberately lives in the DB, not here: adding a printer must
not require a restart (spec 11).
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bambu_bridge.log_redaction import install, redact_event


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Server
    bridge_host: str = "0.0.0.0"  # noqa: S104 - bind-all is intended (Tailscale-fronted)
    bridge_port: int = 8080
    bridge_api_key: str = ""  # empty => fail closed (see api/auth.py)

    # Optional read-only viewer token (env BRIDGE_VIZ_TOKEN).
    # When set, allows access to the snapshot, viz page, and mesh routes
    # without the master API key — designed for HA dashboard iframe embedding.
    # Unset (None / empty string) => feature off, only master key is accepted.
    bridge_viz_token: str | None = None

    # When true, POST /printers accepts loopback host IPs (127.x.x.x).
    # Off in production — Aragorn war-council SSRF policy. Tests flip on
    # so the in-process MQTT broker (127.0.0.1) is registerable. Also useful
    # for a developer running the printer simulator on the same box.
    bridge_allow_loopback_host: bool = False

    # Storage
    bridge_db_path: str = "/var/lib/bambu-bridge/jobs.db"
    bridge_files_dir: str = "/var/lib/bambu-bridge/files"
    bridge_max_transfer_bytes: int = Field(default=64 * 1024 * 1024, gt=0)

    # Camera idle-linger: hold the upstream camera connection for this many
    # seconds after the last subscriber leaves, so a subsequent snapshot
    # request reuses the live stream instead of opening a new TCP+TLS
    # connection. 0 = immediate teardown (original behaviour). Default 10 s.
    bridge_camera_linger_s: float = 10.0

    # Print-failure detection. Phase 1 spaghetti detection is a coarse,
    # zero-dependency heuristic (vision/) — default OFF until validated
    # against real chamber frames. It never weakens the telemetry air
    # watchdog; it only adds an extra debounced abort path while PRINTING.
    bridge_spaghetti_detection: bool = False

    # Logging
    bridge_log_level: str = "info"
    bridge_log_format: Literal["json", "console"] = "json"

    # Push (optional)
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""


def configure_logging(level: str = "info", fmt: str = "json",
                      *, secrets: tuple[str | None, ...] = ()) -> None:
    """structlog -> stdout (spec 12). systemd captures stdout to the journal."""
    install(*secrets)
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    # JSON: serialise tracebacks into the record. Console (dev): let
    # ConsoleRenderer pretty-print exc_info itself (format_exc_info would
    # consume it first and warn).
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    if fmt == "json":
        processors += [
            structlog.processors.dict_tracebacks,
            redact_event,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors.extend([redact_event, structlog.dev.ConsoleRenderer()])

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
