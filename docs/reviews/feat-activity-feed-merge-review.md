# Merge review: feat/activity-feed

| | |
|---|---|
| Branch | `feat/activity-feed` |
| Base | `main` at `b8fc94d` |
| Date | 2026-10-25 |
| Commits | 3 feature, 5 review fixes, 1 report |
| Files / lines | 17 files, +1154 / −55, plus this report |

## Summary

Spool changes and print charges now appear in the printer's Activity feed: links and
unlinks, slots the AMS released, links the bridge refused and why, slot material
rewrites, and one row per finished print with the grams charged to each spool or the
reason it waits for review. The sources route refreshes a stale AMS frame before
listing, and a refresh the printer leaves unanswered is logged. Deployed on CT 105 as
`spool-beta-34686b8` (the three feature commits) since 2026-10-25 19:22Z.

## Review history

| Reviewer | Role | Findings (C / W / N) | Status |
|---|---|---|---|
| Sauron | Correctness, state and data flow | 0 / 3 / 6 | Fixed in `7e3cfe2`, `661e911`, `56784dc`; `7e3cfe2` verified clean |
| Frodo | User-facing wording | 0 / 5 / 10 | Fixed in `7e3cfe2`, `56784dc`, `5474150`, `138f1df`; verified clean |
| Ent | Triage of the fix diff | 0 / 2 / 0, plus test gaps | Fixed in `661e911` |

## Findings

### Warnings

1. `service/consumption_delivery.py` (Sauron W1): a charged print's row was written
   before its review notes (inferred refill, changed tag, over-charge) were recorded, so
   the feed showed a clean charge for a flagged print. The notes are now gathered before
   the print closes and the row lists them.
2. `service/inventory.py` `assign()` (Sauron W2): linking the spool a slot already holds
   wrote another row; `assign-spool.sh` sends no revision, so each rerun did. Such a
   re-link now writes no row.
3. `tests/test_activity_feed.py` (Sauron W3): nothing checked one row per closed print
   across delivery passes or one row per released assignment. Both are tested now.
4. `api/events.py` (Frodo): "Linked from the app" was untrue for scripted links; the
   bridge cannot tell the app from a script, so rows say "Linked by hand".
5. `api/events.py` (Frodo): titles now read "… linked" / "… unlinked"; "removed" read as
   a physical removal.
6. `api/events.py` (Frodo): review rows showed a machine code ("Unassigned slot"); they
   now give the reason in words and "Charge or skip it on the Filament tab."
7. `api/inventory.py` (Frodo): the late-slot refusal gave raw seconds and "try again"
   even when the printer was offline. It now gives minutes past two, has an offline
   text, and is `info` because retries are routine.
8. `service/consumption_delivery.py` (Ent): the tag check moved ahead of the close
   without a guard, so an error there would have kept a print open. It is logged and the
   print closes.
9. `api/events.py` (Ent): a tag note on the external slot would have read "Slot 255".
   Notes now name slots as the rest of the feed does.

### Notes

- A spool moved from another slot says where it came from (Sauron N4).
- The feed and the assignments live in different databases, so a row is written after
  the change commits: at most once, never a duplicate. Documented (Sauron N5).
- Refusals fall back to "A slot" and titles start with a capital (Sauron N6).
- Archived, wrong-material and unavailable-slot refusals name the next step; slot rewrites
  and refusals name the spool (Frodo).
- `API-CONTRACT.md`, `SPOOL-INVENTORY-API.md` and the daily-use manual match the code.

## Fix log

| Commit | What |
|---|---|
| `7e3cfe2` | Sauron and Frodo findings: notes on charged rows, re-link and move rows, wording, docs, tests |
| `661e911` | Ent findings: guarded tag check, slot names in notes, the missing tests |
| `56784dc` | Verify notes: "Nothing charged yet.", hours for long-late reports, contract severity, a test for the mid-pass resolve guard |
| `5474150` | Frodo verify of `661e911`: notes as sentences ("·" is already in slot names), one name for the external holder |
| `138f1df` | An unrecognised note ends with a full stop |

Each behaviour fix (notes on charged rows, the re-link skip, "moved from", the tag-check
guard, the mid-pass resolve guard) was reverted in a scratch copy and fails a test.
Full suite at `138f1df`: 2508 passed, 8 skipped; one geometry test deselected because the
local venv lacks `fast_simplification`.

## Deferred

- Refill backup set and remove have no Activity row yet.
- Refusals made before the spool is read (late slot report, RFID or refill-backup slot,
  slot in the current print) name the spool by number only.
- Rows cannot say which client made a link; that needs the auth check to record whether a
  paired device or the owner key matched.
- The deployed bridge runs the three feature commits; the five fix commits deploy with the
  next release.
