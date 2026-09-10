"""Async MQTT client for the Bambu P1S (spec 5.1).

Pure protocol: no FastAPI, no service imports. Drives one printer's MQTT link
and hands decoded reports to callbacks. Reconnect is the caller's expectation —
:meth:`MqttClient.run` never returns until cancelled.

Spec gotchas encoded here:
  #1  subscribe BEFORE publishing ``pushall`` (else the response is missed)
  #3  printer accepts only ~3 concurrent MQTT clients — don't hammer on drop
  #5  never reconnect faster than ~1/s; exp backoff + jitter, capped 60 s
  #7  self-signed cert rotates on firmware update — never pin, CERT_NONE
  #8  P1S broker is **TLS 1.2 only** — a TLS 1.3 ClientHello draws a
      ``handshake_failure`` alert (40) and the connect hangs to the socket
      timeout. The context MUST cap at TLS 1.2 (mosquitto needs
      ``--tls-version tlsv1.2`` for the same reason). REPORT.md §1.
  #9  The broker **never sends PUBACK**, so any QoS-1 publish blocks until
      the client timeout (~10 s) and the whole session dies before it
      connects. Publish at **QoS 0** — this is why every working capture
      used ``mosquitto_pub`` (QoS 0 by default). Delivery confidence comes
      from the report stream echoing state, not from MQTT acks. Verified on
      hardware 2026-05-19: qos=1 → timeout 12.8 s; qos=0 → report in 0.88 s.
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import aiomqtt
import structlog

from bambu_bridge.protocol.models import ReportMessage, pushall_request
from bambu_bridge.protocol.tls import insecure_tls_context

log = structlog.get_logger(__name__)

ReportHandler = Callable[[ReportMessage], Awaitable[None]]
ConnectionHandler = Callable[[], Awaitable[None]]
SessionErrorHandler = Callable[[str, str], Awaitable[None]]

_BACKOFF_START = 1.0
_BACKOFF_CAP = 15.0  # LAN printer: recover fast after a blip
_KEEPALIVE = 30
_RECONNECT_FLOOR = 1.0  # gotcha #5: never faster than ~1/s
_CONNECT_TIMEOUT = 10.0  # bound the connect; a hang must fail into _backoff


# Stable session_error enum — what the API edge / session-health surface
# reports as ``last_failure_phase`` (contract §3.4 phase strings + a
# protocol-error catch-all). Keep flat; one new value here means one new
# row in the contract's translation table.
SessionErrorPhase = Literal[
    "tls_handshake",       # OSError/SSLError/TimeoutError reaching the broker
    "mqtt_connack",        # CONNACK non-zero (rc=5 etc.) — printer rejected creds
    "mqtt_protocol_error", # generic aiomqtt.MqttError that isn't a CONNACK
    "unknown",             # something else escaped the classifier
]


def classify_mqtt_error(exc: BaseException) -> tuple[SessionErrorPhase, str]:
    """Map an mqtt session exception to ``(stable_enum, short_human_text)``.

    Used by :class:`MqttClient` to report a structured ``last_failure_phase``
    instead of just stringifying the exception — the APK's session-health
    surface needs the enum to colour the dot and pick remediation copy,
    while the human text is what the dev log / `_raw` field carries.
    """
    if isinstance(exc, aiomqtt.MqttCodeError):
        rc_value = getattr(exc.rc, "value", exc.rc)
        connack = int(rc_value) if isinstance(rc_value, int) else None
        if connack is not None:
            return "mqtt_connack", f"CONNACK rc={connack} ({exc})"
        return "mqtt_connack", f"CONNACK error ({exc})"
    if isinstance(exc, ssl.SSLError | TimeoutError | OSError):
        return "tls_handshake", f"{type(exc).__name__}: {exc}"
    if isinstance(exc, aiomqtt.MqttError):
        return "mqtt_protocol_error", f"{type(exc).__name__}: {exc}"
    return "unknown", f"{type(exc).__name__}: {exc}"


class MqttClient:
    """One MQTT link to one printer.

    Usage::

        client = MqttClient(ip, serial, access_code, on_report=…, on_lost=…)
        task = asyncio.create_task(client.run())   # loops forever
        ...
        await client.publish(build_command("print", "pause"))
        task.cancel()
    """

    def __init__(
        self,
        ip: str,
        serial: str,
        access_code: str,
        *,
        on_report: ReportHandler | None = None,
        on_connected: ConnectionHandler | None = None,
        on_lost: ConnectionHandler | None = None,
        on_session_error: SessionErrorHandler | None = None,
        port: int = 8883,
    ) -> None:
        self.ip = ip
        self.serial = serial
        self._access_code = access_code
        self._on_report = on_report
        self._on_connected = on_connected
        self._on_lost = on_lost
        # PR A.2: structured classifier feeds session-health (PR B reads
        # ``last_failure_phase``). Optional so existing call sites stay green.
        self._on_session_error = on_session_error
        self.port = port

        self._report_topic = f"device/{serial}/report"
        self._request_topic = f"device/{serial}/request"
        self._identifier = f"bambu-bridge-{uuid.uuid4()}"

        self._client: aiomqtt.Client | None = None
        self._connected = asyncio.Event()
        self._log = log.bind(printer_id=serial, ip=ip)

    # ----------------------------------------------------------------- #
    # Public
    # ----------------------------------------------------------------- #

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def run(self) -> None:
        """Connect/subscribe/seed/consume forever, reconnecting on any error.

        Cancel the task to stop. Backoff resets once a connection survives long
        enough to be considered healthy.
        """
        attempt = 0
        while True:
            try:
                await self._session()
                # Clean EOF (broker closed) — treat as a disconnect.
                attempt = 0
            except asyncio.CancelledError:
                self._log.info("mqtt.run cancelled")
                raise
            except aiomqtt.MqttError as exc:
                await self._report_session_error(exc)
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                self._log.exception("mqtt.session_unexpected")
                await self._report_session_error(exc)
            finally:
                await self._mark_disconnected()

            attempt += 1
            delay = self._backoff(attempt)
            self._log.info("mqtt.reconnect_wait", attempt=attempt, delay=round(delay, 1))
            await asyncio.sleep(delay)

    async def publish(self, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
        """Publish a command envelope to ``device/<serial>/request`` (QoS 0).

        Waits up to ``timeout`` for an active connection. QoS 0 is mandatory,
        not a downgrade: the P1S broker never PUBACKs, so QoS 1 hangs the
        session (gotcha #9). Command delivery is confirmed by the printer's
        report stream reflecting the new state, not by an MQTT ack.
        """
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError as exc:
            raise ConnectionError(
                f"printer {self.serial} not connected; cannot publish command"
            ) from exc
        client = self._client
        if client is None:  # lost between the wait and here
            raise ConnectionError(f"printer {self.serial} connection dropped")
        await client.publish(self._request_topic, json.dumps(payload), qos=0)
        self._log.debug("mqtt.published", topic=self._request_topic)

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    async def _session(self) -> None:
        """One connected lifetime: subscribe → pushall → consume."""
        self._log.info("mqtt.connecting", port=self.port)
        async with aiomqtt.Client(
            hostname=self.ip,
            port=self.port,
            username="bblp",
            password=self._access_code,
            identifier=self._identifier,
            tls_context=insecure_tls_context(),
            keepalive=_KEEPALIVE,
            clean_session=True,
            timeout=_CONNECT_TIMEOUT,
        ) as client:
            self._client = client
            # Gotcha #1: subscribe before publishing pushall.
            # Gotcha #9: QoS 0 — the broker never PUBACKs; QoS 1 hangs here.
            await client.subscribe(self._report_topic, qos=0)
            await client.publish(self._request_topic, json.dumps(pushall_request()), qos=0)
            await self._mark_connected()
            self._log.info("mqtt.connected")

            async for message in client.messages:
                await self._handle_message(message.payload)

    async def _handle_message(self, payload: Any) -> None:
        if isinstance(payload, bytes | bytearray):
            raw_text = payload.decode("utf-8", errors="replace")
        else:
            raw_text = str(payload)
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            self._log.warning("mqtt.bad_json", sample=raw_text[:120])
            return
        if not isinstance(data, dict):
            return
        try:
            report = ReportMessage.parse(data)
        except Exception:  # noqa: BLE001 — extra="allow" makes this rare
            self._log.exception("mqtt.parse_failed")
            return
        if self._on_report is not None:
            await self._on_report(report)

    async def _mark_connected(self) -> None:
        self._connected.set()
        if self._on_connected is not None:
            await self._on_connected()

    async def _report_session_error(self, exc: BaseException) -> None:
        """Log + fan out a structured session_error (PR A.2).

        Structured log carries the stable phase enum so log aggregators can
        count failures by class. The ``on_session_error`` callback feeds
        :class:`PrinterService` so session-health (PR B) can surface
        ``last_failure_phase`` without re-parsing the exception.
        """
        phase, human = classify_mqtt_error(exc)
        self._log.warning("mqtt.session_error", phase=phase, error=human)
        if self._on_session_error is not None:
            try:
                await self._on_session_error(phase, human)
            except Exception:  # noqa: BLE001 — callback errors mustn't kill the loop
                self._log.exception("mqtt.session_error_callback_failed")

    async def _mark_disconnected(self) -> None:
        was_connected = self._connected.is_set()
        self._connected.clear()
        self._client = None
        if was_connected and self._on_lost is not None:
            # Spec 5.1 #4: emit connection_lost so RN clients show stale state.
            await self._on_lost()

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with jitter, floored at 1 s, capped at 15 s."""
        # cap exponent before exponentiating: a long outage drives attempt high
        # enough that 2.0 ** (attempt - 1) overflows float before min() can clamp
        # it, which crashes the reconnect loop permanently. 2**32 already dwarfs
        # _BACKOFF_CAP, so clamping the exponent changes no real-world delay.
        exp: int = min(attempt - 1, 32)
        base: float = min(_BACKOFF_CAP, _BACKOFF_START * (2.0 ** exp))
        jitter: float = random.uniform(0, base * 0.3)
        return max(_RECONNECT_FLOOR, min(_BACKOFF_CAP, base + jitter))
