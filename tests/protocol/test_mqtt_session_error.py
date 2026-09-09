"""PR A.2 — ``classify_mqtt_error`` translation table.

Before A.2 a session error was just ``str(exc)`` in a log line — the APK
had to body-sniff text to figure out whether the bridge couldn't reach
the printer (E1) vs the printer rejected the access code (E2). This
table is the contract-stable enum that feeds ``last_failure_phase`` on
the session-health surface (PR B reads it; A.2 writes it).
"""

from __future__ import annotations

import ssl

import aiomqtt

from bambu_bridge.protocol.mqtt import classify_mqtt_error


def _connack_exc(rc_value: int) -> aiomqtt.MqttCodeError:
    """Build an ``MqttCodeError`` carrying a CONNACK rc. aiomqtt's ctor is
    ``(rc, *args)`` — the `rc` may be a bare int or a ReasonCode-with-`.value`,
    and the classifier handles both."""
    return aiomqtt.MqttCodeError(rc_value, f"CONNACK rc={rc_value}")


def test_connack_rc5_classified_as_mqtt_connack() -> None:
    """rc=5 (not authorised) is the dominant E2 case: wrong access code OR
    LAN-Only Mode off. Either way the phase is ``mqtt_connack``."""
    phase, human = classify_mqtt_error(_connack_exc(5))
    assert phase == "mqtt_connack"
    assert "rc=5" in human


def test_ssl_error_classified_as_tls_handshake() -> None:
    """A handshake failure (mismatched TLS version, refused conn) is E1."""
    phase, human = classify_mqtt_error(ssl.SSLError("alert handshake_failure"))
    assert phase == "tls_handshake"
    assert "SSLError" in human


def test_oserror_classified_as_tls_handshake() -> None:
    """Connection refused / no route to host land here too — same UX as a
    failed handshake from the APK's perspective."""
    phase, _ = classify_mqtt_error(ConnectionRefusedError("nope"))
    assert phase == "tls_handshake"


def test_timeout_classified_as_tls_handshake() -> None:
    """Connect-timeout (the 10s bound we set on the aiomqtt connect)."""
    phase, _ = classify_mqtt_error(TimeoutError("connect timed out"))
    assert phase == "tls_handshake"


def test_generic_mqtt_error_classified_as_protocol_error() -> None:
    """Other aiomqtt errors that aren't a CONNACK (e.g. broker dropped the
    socket mid-session) carry the third stable phase value."""
    phase, _ = classify_mqtt_error(aiomqtt.MqttError("connection lost"))
    assert phase == "mqtt_protocol_error"


def test_unexpected_exception_classified_as_unknown() -> None:
    """Anything escaping the catch clauses is ``unknown`` — better an
    explicit unknown than a None or a crash in the writer path."""
    phase, _ = classify_mqtt_error(RuntimeError("never seen this"))
    assert phase == "unknown"
