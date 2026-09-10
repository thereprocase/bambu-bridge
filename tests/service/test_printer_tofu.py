"""PR A.2 — PrinterService TOFU compare-on-connect (contract §4.5).

Six cases the service has to get right:

1. Pre-TOFU rows (``expected_fingerprint=None``) stay ``"unknown"``; the
   API edge lets them through. This avoids locking operators out of every
   printer registered before the cert_fingerprint column existed.
2. Matching fingerprint → ``"trusted"`` (the happy path).
3. Mismatching fingerprint → ``"changed"`` AND a single
   ``cert_changed`` event on the bus (the APK's in-app TOFU prompt
   hooks this).
4. Repeated mismatched reconnect doesn't re-fire ``cert_changed`` —
   transition-only semantics so the APK doesn't get spammed every time
   MQTT bounces.
5. mismatch → operator re-pins → next reconnect fires ``cert_trusted``
   (so the APK can dismiss the prompt without polling).
6. Probe failure during reconnect leaves the status untouched (best-effort
   compare; a transient handshake hiccup must not flip a previously
   "trusted" printer to "unknown").

Tests drive ``PrinterService._tofu_compare`` directly with the
``tls_probe`` binding monkeypatched — no real broker, no async timing
games.
"""

from __future__ import annotations

from typing import Any

import pytest

from bambu_bridge.service import printer as svc_mod
from bambu_bridge.service.events import Event  # noqa: F401  (used in annotations)


def _make_service(expected: str | None) -> svc_mod.PrinterService:
    return svc_mod.PrinterService(
        "00M00A000000000",
        "192.168.0.42",
        "12345678",
        friendly_name="Test",
        expected_fingerprint=expected,
    )


def _stub_tls(monkeypatch: pytest.MonkeyPatch, fingerprint: str | None) -> None:
    """Replace the leaf-cert probe. ``None`` makes it raise — simulates a
    transient handshake hiccup during reconnect."""

    async def _probe(host: str, port: int, **_kw: Any) -> Any:  # noqa: ARG001
        if fingerprint is None:
            raise ConnectionError("tls_handshake: synthetic")
        return svc_mod.tls_probe.LeafCert(
            der=b"\x00",
            fingerprint_sha256=fingerprint,
            common_name="test",
            subject_rfc4514="CN=test",
        )

    monkeypatch.setattr(svc_mod.tls_probe, "leaf_cert_fingerprint", _probe)


@pytest.mark.asyncio
async def test_legacy_row_with_no_pin_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-A.2 printers have ``cert_fingerprint=NULL``. The compare must not
    flip them to ``changed`` just because they happen to have a real cert
    on the wire — that would 403 every legacy row."""
    svc = _make_service(expected=None)
    _stub_tls(monkeypatch, fingerprint="anything-real")
    await svc._tofu_compare()
    assert svc.cert_status == "unknown"
    assert svc.current_fingerprint == "anything-real"


@pytest.mark.asyncio
async def test_matching_pin_marks_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _make_service(expected="pin-A")
    _stub_tls(monkeypatch, fingerprint="pin-A")
    await svc._tofu_compare()
    assert svc.cert_status == "trusted"
    assert svc.current_fingerprint == "pin-A"


@pytest.mark.asyncio
async def test_mismatched_pin_marks_changed_and_fires_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _make_service(expected="pin-A")
    _stub_tls(monkeypatch, fingerprint="pin-B")

    captured: list[tuple[str, dict]] = []

    async def _sub() -> None:
        async with svc.bus.subscribe() as q:
            ev = await q.get()
            captured.append((ev.name or "", dict(ev.data)))

    import asyncio
    task = asyncio.create_task(_sub())
    await asyncio.sleep(0)
    await svc._tofu_compare()
    await asyncio.wait_for(task, timeout=1.0)

    assert svc.cert_status == "changed"
    assert captured == [
        (
            "cert_changed",
            {"previous_fingerprint": "pin-A", "current_fingerprint": "pin-B"},
        )
    ]


@pytest.mark.asyncio
async def test_repeated_mismatch_does_not_refire_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cert_changed event is a transition signal. Reconnect twice with
    the same mismatch should fire once, not twice — otherwise every MQTT
    bounce repaints the APK's prompt."""
    svc = _make_service(expected="pin-A")
    _stub_tls(monkeypatch, fingerprint="pin-B")

    events: list[Event] = []

    async def _drain() -> None:
        async with svc.bus.subscribe() as q:
            while True:
                events.append(await q.get())

    import asyncio
    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)
    await svc._tofu_compare()
    await svc._tofu_compare()  # second reconnect, same mismatch
    await asyncio.sleep(0.05)
    task.cancel()

    cert_events = [e for e in events if e.name == "cert_changed"]
    assert len(cert_events) == 1


@pytest.mark.asyncio
async def test_repin_then_reconnect_fires_cert_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the operator hits POST /trust, the next compare with a
    matching pin should publish ``cert_trusted`` so the APK can dismiss
    its in-app TOFU prompt without a poll."""
    svc = _make_service(expected="pin-A")
    _stub_tls(monkeypatch, fingerprint="pin-B")
    await svc._tofu_compare()
    assert svc.cert_status == "changed"

    # operator hits /trust: pin updates to the new cert's fingerprint
    svc.expected_fingerprint = "pin-B"

    events: list[Event] = []

    async def _drain() -> None:
        async with svc.bus.subscribe() as q:
            while True:
                events.append(await q.get())

    import asyncio
    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)
    await svc._tofu_compare()
    await asyncio.sleep(0.05)
    task.cancel()

    assert svc.cert_status == "trusted"
    trusted_events = [e for e in events if e.name == "cert_trusted"]
    assert len(trusted_events) == 1
    assert trusted_events[0].data == {"fingerprint": "pin-B"}


@pytest.mark.asyncio
async def test_probe_failure_leaves_status_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient handshake glitch during the post-connect probe must NOT
    overwrite a previously known-good status — best-effort compare."""
    svc = _make_service(expected="pin-A")
    _stub_tls(monkeypatch, fingerprint="pin-A")
    await svc._tofu_compare()
    assert svc.cert_status == "trusted"

    _stub_tls(monkeypatch, fingerprint=None)  # next call raises
    await svc._tofu_compare()
    assert svc.cert_status == "trusted"  # unchanged
