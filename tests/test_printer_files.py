"""Which bridge job files on the printer may go: the selection is pure, so it is tested
without a printer."""

from bambu_bridge.printer_files import prunable

NOW = 1_000_000.0
DAY = 86400


def upload(identifier, *, created, state="delivered", start_state="completed"):
    return {"id": identifier, "state": state, "start_state": start_state, "created": created}


def name(identifier, suffix=".gcode.3mf"):
    return f"beluga-{identifier}{suffix}"


A, B, C, D, E = (f"{i:032x}" for i in range(1, 6))


def test_keeps_current_active_recent_and_young_files():
    listing = [
        name(A),
        name(B),
        name(C),
        name(D),
        name(E),
        "owner-model.gcode.3mf",
        name(E, ".bin"),
    ]
    rows = [
        upload(A, created=NOW - 10 * DAY),  # old, settled: goes
        upload(B, created=NOW - 9 * DAY, start_state="running"),  # start still owned: stays
        upload(C, created=NOW - 8 * DAY, state="delivering"),  # still being delivered: stays
        upload(D, created=NOW - 7 * DAY),  # the printer's current file: stays
        upload(E, created=NOW - 60, start_state=None),  # never started, but young: stays
    ]
    doomed = prunable(listing, rows, "/" + name(D), keep=0, min_age_s=DAY, now=NOW)
    assert doomed == [name(A)]


def test_newest_settled_uploads_are_kept_and_orphans_go():
    listing = [name(A), name(B), name(C), name(D)]
    rows = [
        upload(A, created=NOW - 5 * DAY),
        upload(B, created=NOW - 4 * DAY),
        upload(C, created=NOW - 3 * DAY),
        # D has no inbox row at all: an orphan from before the inbox's retention.
    ]
    doomed = prunable(listing, rows, None, keep=2, min_age_s=DAY, now=NOW)
    assert doomed == [name(D), name(A)]


def test_other_files_on_the_card_are_never_candidates():
    listing = ["benchy.gcode.3mf", "kraken-left.gcode.3mf", "beluga-notahexid.gcode.3mf"]
    assert prunable(listing, [], None, keep=0, min_age_s=0, now=NOW) == []
