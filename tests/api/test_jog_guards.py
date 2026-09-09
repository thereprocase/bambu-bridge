"""Jog safety guard tests (defence-in-depth, crash-prevention) — FAIL CLOSED.

The P1S does NOT report toolhead position over MQTT; ``home_flag`` (a homing
bitmask) is the only motion-state field it sends. So the envelope guard cannot
clamp against a printer-reported position — it would be dead code. (It was:
four consecutive Z-50 jogs were once accepted and drove the bed ~200 mm into
the toolhead because the clamp had no position to compare against.)

The redesigned guard dead-reckons position in the bridge:
  * unknown position  → REJECT every jog (fail closed; re-home to recover).
  * known position    → clamp the dead-reckoned result to the envelope.

Invariants exercised here:
  1. ``jog_step_not_allowed`` — 422 when distance_mm ∉ {1, 10, 50}.
  2. ``jog_not_homed``        — 409 when home_flag absent / axis bit unset.
  3. unknown-position fail-closed — 409 even when homed, for *any* direction,
     including the gap-closing Z- jog that crashed the bed.
  4. dead-reckon — a successful ``/home`` seeds position; jogs advance it and
     clamp to [SAFE_Z_FLOOR, 256]; the exact 4×Z-50 crash sequence is stopped.
  5. desync — a disconnect resets the estimate → subsequent jog rejected.

State injection strategy: ``home_flag`` is injected directly into ``_state``
(it *is* a real P1S field). The dead-reckoned X/Y/Z position is driven through
the service's public motion-state API (``mark_homed`` / ``apply_jog``) or the
``/home`` endpoint — never by faking an x/y/z field the printer never sends.

P1S home_flag bitmask (ha-bambulab / OpenBambuAPI):
    bit 0 = X homed
    bit 1 = Y homed
    bit 2 = Z homed
    0x07  = all three axes homed
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    MockPrinter,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


def _register(c: TestClient) -> None:
    assert (
        c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={
                "host": "127.0.0.1",
                "access_code": ACCESS_CODE,
                "friendly_name": "Jog P1S",
            },
        ).status_code
        == 201
    )


def _wait_connected(c: TestClient, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH)
        if r.status_code == 200 and r.json().get("session", {}).get("connected"):
            return
        time.sleep(0.05)
    raise AssertionError("printer never connected")


def _wait_request(
    mock: MockPrinter, match: Any, timeout: float = 5.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for req in list(mock.requests):
            if match(req):
                return req
        time.sleep(0.02)
    raise AssertionError(f"command never reached printer; saw {mock.requests}")


def _service(app: Any) -> Any:
    from bambu_bridge.service.registry import Registry

    registry: Registry = app.state.registry
    return registry.get(SERIAL)


def _inject_state(app: Any, patch: dict[str, Any], *, wait_seed: float = 2.0) -> None:
    """Directly update the printer service's ``_state`` dict.

    Only used to inject *real* P1S fields (``home_flag``). The dead-reckoned
    position is never injected this way — the printer does not report it.

    Waits until ``_state`` is non-empty (seed arrived) before patching, so a
    late-arriving pushall cannot overwrite the patch afterwards.
    """
    svc = _service(app)
    deadline = time.time() + wait_seed
    while time.time() < deadline:
        if svc._state:  # noqa: SLF001
            break
        time.sleep(0.02)
    svc._state.update(patch)  # noqa: SLF001
    # Brief pause to let any in-flight MQTT report drain before the HTTP
    # request is issued — avoids a race where a seed report arrives after the
    # patch and replaces home_flag.
    time.sleep(0.08)


def _seed_position(app: Any, x: float, y: float, z: float) -> None:
    """Force the bridge's dead-reckon estimate to a known position.

    Mirrors what a successful ``/home`` then a series of jogs would produce,
    but lets a test place the toolhead anywhere in the envelope deterministically
    without driving real motion. Uses the public motion-state API.
    """
    svc = _service(app)
    svc.mark_homed()  # X=Y=Z=0 (known)
    svc.apply_jog("X", x)
    svc.apply_jog("Y", y)
    svc.apply_jog("Z", z)


# --------------------------------------------------------------------------- #
# 1. Step whitelist
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_jog_arbitrary_step_rejected(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """422 jog_step_not_allowed when distance_mm is not in {1, 10, 50}.

    Checked *first*, before the homed/position gates, so a bad magnitude never
    reaches the printer regardless of motion state.
    """
    app = build_app(tmp_path / "jog_step.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})
            _seed_position(app, 128.0, 128.0, 128.0)

            for bad_step in [0.5, 2.0, 7.0, 100.0, 200.0, -200.0]:
                r = c.post(
                    f"/api/v1/printers/{SERIAL}/move",
                    headers=_AUTH,
                    json={"axis": "Z", "distance_mm": bad_step},
                )
                assert r.status_code == 422, (
                    f"step {bad_step!r} should be 422, got {r.status_code}: {r.text}"
                )
                assert r.json()["error"] == "jog_step_not_allowed", r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# 2. Not-homed guard
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_jog_rejected_when_not_homed(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """409 jog_not_homed when home_flag is absent from printer state."""
    app = build_app(tmp_path / "jog_nh.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            # SAMPLE_PUSH_STATUS has no home_flag — axis position unknown.
            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            body = r.json()
            assert body["error"] == "jog_not_homed"
            assert "hom" in body["message"].lower()

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_jog_rejected_when_axis_bit_not_set(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """409 jog_not_homed when home_flag present but the requested axis bit is 0."""
    app = build_app(tmp_path / "jog_bit.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            # home_flag=0x03 = X + Y homed; Z (bit 2) not set.
            _inject_state(app, {"home_flag": 3})

            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            assert r.json()["error"] == "jog_not_homed"

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# 3. Unknown-position fail-closed — the core of the redesign
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_position_gap_closing_z_rejected(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """409 when homed but position UNKNOWN and the jog closes the Z gap (Z-).

    This is the exact gap the printer crash exploited: homed bit set, but the
    bridge has no position estimate (printer reports none), so a Z- jog must be
    refused. Never allow a gap-closing move on an unknown Z.
    """
    app = build_app(tmp_path / "jog_unk_close.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            # home_flag says homed, but no /home went through the bridge, so
            # the dead-reckon estimate is still unknown.
            _inject_state(app, {"home_flag": 7})
            assert _service(app).tracked_position("Z") is None

            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": -10.0},
            )
            assert r.status_code == 409, r.text
            assert r.json()["error"] == "jog_not_homed", r.text
            # Nothing must have reached the printer.
            assert not any(
                "G1 Z" in req.get("print", {}).get("param", "")
                for req in mock_printer.requests
            ), mock_printer.requests

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_unknown_position_gap_opening_z_also_rejected(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """409 when homed but position UNKNOWN even for the gap-OPENING (Z+) jog.

    From an unknown position the bed could already be at the envelope max, so
    a gap-opening jog could still exit the envelope. Fail closed: re-home.
    """
    app = build_app(tmp_path / "jog_unk_open.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})
            assert _service(app).tracked_position("Z") is None

            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            assert r.json()["error"] == "jog_not_homed", r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_unknown_position_xy_rejected(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """409 for X/Y jogs from an unknown position too.

    Documented decision: X/Y cannot crash the bed, but an unknown-position X/Y
    jog can ram the toolhead into the frame. We fail closed on every axis for a
    single, predictable rule — unknown position rejects, re-home to recover.
    """
    app = build_app(tmp_path / "jog_unk_xy.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})

            for axis in ("X", "Y"):
                r = c.post(
                    f"/api/v1/printers/{SERIAL}/move",
                    headers=_AUTH,
                    json={"axis": axis, "distance_mm": 10.0},
                )
                assert r.status_code == 409, (axis, r.text)
                assert r.json()["error"] == "jog_not_homed", (axis, r.text)

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# 4. Dead-reckon: /home seeds position, jogs advance & clamp it
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_home_seeds_known_position(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """A successful POST /home sets the bridge's dead-reckon estimate to known.

    Post-home Z is the most-pessimistic safe value (gap closed, Z=0). A
    gap-opening Z+ jog is then allowed; a gap-closing Z- jog from Z=0 is
    refused (would drive below the bed-crash floor).
    """
    app = build_app(tmp_path / "jog_home_seed.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})

            assert _service(app).tracked_position("Z") is None
            r = c.post(f"/api/v1/printers/{SERIAL}/home", headers=_AUTH)
            assert r.status_code == 200, r.text
            assert _service(app).tracked_position("Z") == 0.0

            # Gap-opening Z+ from the floor is allowed (0 → 10, inside [0,256]).
            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": 10.0},
            )
            assert r.status_code == 200, r.text
            assert _service(app).tracked_position("Z") == 10.0

            # Gap-closing Z- of 50 from Z=10 would hit -40 < floor → reject.
            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": -50.0},
            )
            assert r.status_code == 409, r.text
            assert r.json()["error"] == "jog_out_of_envelope", r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_four_z_minus_50_sequence_first_unsafe_rejected(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """The exact bed-crash scenario: 4×Z-50 must NOT all be accepted.

    Place the bridge at a known mid-envelope Z (as if homed then jogged up),
    then fire Z-50 repeatedly. Each accepted jog lowers the dead-reckon Z by
    50; the first one that would cross the bed-crash floor (Z<0) is rejected
    and every later one stays rejected. On the real printer the old guard
    accepted all four and crashed the bed ~200 mm.
    """
    app = build_app(tmp_path / "jog_4x.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})
            # Known starting Z = 120 (e.g. homed=0 then four Z+ steps). The
            # accepted Z-50 jogs walk it 120 → 70 → 20 → (would be -30 REJECT).
            _seed_position(app, 128.0, 128.0, 120.0)

            results = []
            for _ in range(4):
                r = c.post(
                    f"/api/v1/printers/{SERIAL}/move",
                    headers=_AUTH,
                    json={"axis": "Z", "distance_mm": -50.0},
                )
                results.append(r.status_code)

            # 120→70 ok, 70→20 ok, 20→-30 REJECT, then stays REJECT at 20.
            assert results == [200, 200, 409, 409], results
            # Final tracked Z must never have gone below the floor.
            assert _service(app).tracked_position("Z") == 20.0
            # The printer saw exactly the two accepted (safe) jogs — and never
            # the rejected ones. Poll briefly so the second publish can drain.
            deadline = time.time() + 5.0
            while time.time() < deadline:
                z_moves = [
                    req["print"]["param"]
                    for req in list(mock_printer.requests)
                    if "G1 Z-50" in req.get("print", {}).get("param", "")
                ]
                if len(z_moves) >= 2:
                    break
                time.sleep(0.02)
            assert len(z_moves) == 2, z_moves

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_jog_rejected_outside_top_envelope(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """409 jog_out_of_envelope when dead-reckoned Z + step would exceed 256."""
    app = build_app(tmp_path / "jog_env.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})
            _seed_position(app, 128.0, 128.0, 250.0)

            # 250 + 50 = 300 > 256 → reject.
            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": 50.0},
            )
            assert r.status_code == 409, r.text
            body = r.json()
            assert body["error"] == "jog_out_of_envelope"
            assert "256" in body["message"]

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# 5. Desync — disconnect resets the dead-reckon estimate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disconnect_resets_position_then_gap_close_rejected(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """A disconnect forgets the estimate → next gap-closing jog is rejected.

    Even though the axis is still flagged homed, motion we couldn't see may
    have happened while disconnected, so the guard fails closed.
    """
    app = build_app(tmp_path / "jog_disc.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})
            _seed_position(app, 128.0, 128.0, 120.0)
            assert _service(app).tracked_position("Z") == 120.0

            # A real disconnect (`_handle_lost`) forgets the estimate AND marks
            # the printer offline — both fail closed. Here we drive the desync
            # path the disconnect uses (`reset_motion_state`) directly so we can
            # isolate the position-forgotten invariant while the link stays up:
            # the jog must still be rejected purely because position is unknown.
            _service(app).reset_motion_state("test_disconnect")
            assert _service(app).tracked_position("Z") is None

            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": -10.0},
            )
            assert r.status_code == 409, r.text
            assert r.json()["error"] == "jog_not_homed", r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# 6. Happy-path: G-code sign convention (Z+ opens gap, Z- closes gap)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_z_plus_sends_positive_gcode(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Z+ (distance_mm=+10) → G1 Z10 (bed down, gap opens — the safe way)."""
    app = build_app(tmp_path / "jog_zp.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})
            _seed_position(app, 128.0, 128.0, 20.0)

            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": 10.0},
            )
            assert r.status_code == 200, r.text

            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "gcode_line"
                and "G1 Z10" in r["print"].get("param", ""),
            )
            param: str = req["print"]["param"]
            assert "G1 Z10" in param, f"expected G1 Z10 in {param!r}"
            assert "G91" in param, "relative mode G91 must be set"
            assert "G90" in param, "absolute mode G90 must be restored"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_z_minus_sends_negative_gcode(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Z- (distance_mm=-10) → G1 Z-10 (bed up, gap closes) when safely inside."""
    app = build_app(tmp_path / "jog_zm.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})
            _seed_position(app, 128.0, 128.0, 50.0)

            r = c.post(
                f"/api/v1/printers/{SERIAL}/move",
                headers=_AUTH,
                json={"axis": "Z", "distance_mm": -10.0},
            )
            assert r.status_code == 200, r.text

            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "gcode_line"
                and "G1 Z-10" in r["print"].get("param", ""),
            )
            assert "G1 Z-10" in req["print"]["param"]

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_all_allowed_steps_pass_when_homed_and_known(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """All three step sizes (1, 10, 50 mm) accepted when homed AND position known."""
    app = build_app(tmp_path / "jog_steps.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"home_flag": 7})
            # Re-seed mid-range before each step so deltas don't accumulate
            # out of the envelope.
            for step in [1.0, 10.0, 50.0]:
                _seed_position(app, 128.0, 128.0, 128.0)
                r = c.post(
                    f"/api/v1/printers/{SERIAL}/move",
                    headers=_AUTH,
                    json={"axis": "Z", "distance_mm": step},
                )
                assert r.status_code == 200, f"step {step} rejected: {r.text}"

    await asyncio.to_thread(run)
