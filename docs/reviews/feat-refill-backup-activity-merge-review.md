# Merge review: feat/refill-backup-activity

| | |
|---|---|
| Branch | `feat/refill-backup-activity` |
| Base | `main` at `d9d125d` (branched from `f32f838`, its second parent) |
| Date | 2026-10-25 |
| Commits | 1 feature, 3 review fixes, 1 report |

## Summary

Setting or removing a refill backup now writes a row to the printer's Activity feed:
"AMS A · slot 1 backed up by slot 2", or "AMS A · slot 1 no longer backed up by slot 2",
with whether the printer confirmed it and, when it did not, the bridge's own reason
("Turn on AMS auto-refill on the printer") and where to tap Try again. Try again writes no
row. A backup that did not take, or a removal that could not put a slot back, is a warning.

## Review history

| Reviewer | Role | Findings (C / W / N) | Status |
|---|---|---|---|
| Frodo | User-facing wording, app tap path | 0 / 2 / 4 | Fixed in `b011d98`; verified clean |
| Sauron | Correctness, every handler path | 0 / 2 / 5 | Fixed in `b011d98`, `af3d8b6`, `7c248d5`; verified clean |

## Findings

### Warnings

1. `api/events.py` (Frodo): the Try again instruction named a control the app does not
   have. It now reads "On the Filament tab, tap Open backup on the Needs you card, then Try
   again."
2. `api/events.py` (Frodo): pending and failed rows dropped the reason the bridge already
   shows on the Backup screen, the most useful being "Turn on AMS auto-refill on the
   printer". Rows now carry it.
3. `api/inventory_refill.py` `remove_backup` (Sauron): a refusal (409) after the removal was
   stored left no row, and the retry then returned 404. The row is now written once
   `disable()` has stored the change, whatever happens after.
4. `api/inventory_refill.py` (Sauron): removal rows read the raw stored state, so an
   unconfirmed backup removed while the printer was away read as a pending restore at
   `info`. Rows now take the state `status()` derives, the one the app shows.

### Notes

- The removed title mirrors the set title; a failed set says "didn't take" (Frodo).
- Waiting rows point to the slot's Backup screen for the live state (Frodo).
- Every row is written outside the inventory write lock (Sauron).
- An unexpected removal state no longer reads as both slots restored (Sauron).
- Tests pin the state, a late refusal, a refusal before the change was stored, and the
  away warning (Sauron).

## Fix log

| Commit | What |
|---|---|
| `b011d98` | Frodo findings: the bridge's reason, real button names, titles |
| `af3d8b6` | Sauron findings: a row once the removal is stored, derived state, neutral fallback, tests |
| `7c248d5` | A test that a removal refused before it was stored writes no row |

Each guard (`retried`, the row once the removal is stored, the derived state, the order of
the stored flag against `disable()`) was reverted in a scratch copy and fails a test.
Full suite at `7c248d5`: 2515 passed, 8 skipped; one geometry test deselected because the
local venv lacks `fast_simplification`.

## Deferred

- Refusals from the refill routes ("Remove the slot 1 backup first", "Slot 1 has no
  backup") and the bridge's backup reasons name slots by number only, while titles say
  "AMS A · slot 1".
- A pending or restoring row gets no closing row when the printer catches up; the Backup
  screen shows the live state.
- Pre-existing: setting the same pair again while its removal still waits for the printer
  is taken as Try again and does not re-enable the backup.
