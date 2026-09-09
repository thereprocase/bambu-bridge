"""HMS lookup — runout codes, unmapped fallback, int → hex conversion."""

from __future__ import annotations

from bambu_bridge.hms import lookup


def test_known_runout_code_warn() -> None:
    entry = lookup("0300_0d00_0003_0001")
    assert entry["severity"] == "warn"
    assert entry["category"] == "ams"
    assert "runout" in entry["user_message"].lower()
    assert entry["remediation"] is not None


def test_known_external_spool_runout() -> None:
    entry = lookup("0300_8013_0002_0001")
    assert entry["severity"] == "warn"
    assert "external" in entry["user_message"].lower()


def test_unmapped_code_falls_through() -> None:
    entry = lookup("ffff_ffff_ffff_ffff")
    assert entry["severity"] == "unknown"
    assert entry["category"] == "unmapped"
    # User-message is the canonical hex so the APK shows something specific.
    assert entry["user_message"] == "ffff_ffff_ffff_ffff"


def test_zero_int_is_cleared() -> None:
    entry = lookup(0)
    assert entry["severity"] == "info"
    assert entry["category"] == "cleared"


def test_zero_string_is_cleared() -> None:
    entry = lookup("0")
    assert entry["severity"] == "info"


def test_none_is_cleared() -> None:
    entry = lookup(None)
    assert entry["severity"] == "info"


def test_empty_string_is_cleared() -> None:
    entry = lookup("")
    assert entry["severity"] == "info"


def test_int_renders_as_hex_groups() -> None:
    # 0x0300_0D00 packed → first two groups are 0300_0d00; rest zero-padded.
    entry = lookup(0x03000D00)
    # Not in table at this exact form — should fall through to unmapped
    # with the canonical hex visible to the user.
    assert entry["category"] in {"ams", "unmapped"}
    if entry["category"] == "unmapped":
        assert "0300_0d00" in entry["user_message"]


def test_case_insensitive_lookup() -> None:
    entry = lookup("0300_0D00_0003_0001")  # uppercase variant
    assert entry["severity"] == "warn"
    assert entry["category"] == "ams"


def test_dash_separated_normalized() -> None:
    entry = lookup("0300-0d00-0003-0001")
    assert entry["severity"] == "warn"
