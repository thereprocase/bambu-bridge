# Spool inventory API

Spoolman is the only record of spools; the bridge keeps no spool list of its own.

## Configuration

The coordinator starts only when `BRIDGE_SPOOLMAN_URL` and
`BRIDGE_INVENTORY_DIR` are configured on the server. The URL can be a server
root or end in `/api/v1`. An optional `BRIDGE_SPOOLMAN_BEARER` stays server-side;
URL credentials, queries and fragments are rejected. Back up the inventory
directory together with the bridge and Spoolman databases. Never expose an
unauthenticated Spoolman service publicly.

The inventory directory contains SQLite state and a process writer lock.
A second writer cannot open it. Changing the configured provider URL fails
closed and requires explicit relinking, rather than treating reused spool
numbers as the same inventory. A URL change still requires an operator-assisted
configuration cutover; runtime URL rebinding is not implemented.

All routes below are under `/api/v1/inventory`, using the existing authenticated
bridge API. Paired-device credentials follow the same HTTPS rules as other app
controls. Viewer credentials do not grant inventory access.

## Reads

- `GET /`: capability/configuration state, provider UUID, global assignment
  revision and saved assignments. Available even when not configured.
- `GET /health`: safe provider health/version summary; filesystem paths omitted.
- `GET /spools?query=ASA&offset=0&limit=50`: unarchived spool search.
- `GET /spools/43`: current provider record, percentage and an over-capacity
  flag. Raw grams are never clamped.
- `GET /products` and `/vendors`: paginated product/manufacturer search.
- `GET /printers/{serial}/sources`: fresh physical sources. AMS sources require
  hardware identity, not just array position. Disconnected/empty/unidentified
  sources are not assignable. This route does not request or alter printer data.
  Each row carries an opaque `observed_identity` (the slot's declared material,
  product and tag, used for "last spool here" offers) and `spool_key` (its RFID
  tag, or `untagged`). An assignment ends only on physical evidence: an observed
  unload, an empty slot, another tag, or another chemistry (`tray_type`).
  Product, temperature and colour changes, including the bridge's own writes,
  keep it. A blank tray or a tag being read decides nothing. Two identical
  untagged spools swapped while no report showed the slot empty cannot be told
  apart; reassign by hand. The hardware-qualified `source` address remains stable.

Known percentages use product package capacity. A partially used spool first
entered at 400 g on a 1,000 g product does not become a new 400 g denominator.
Provider mass and source freshness are independent.

## Writes

Record changes are forwarded to Spoolman once and answered with its record:
`POST /vendors`, `POST /products`, `POST /spools`, `PUT /spools/{id}/weigh`
and `PUT /spools/{id}/archive`. The bodies are the `NewVendor`, `NewProduct`,
`NewSpool` and `Weight` models in `service/inventory.py`; OpenAPI carries the
field constraints. The bridge translates and checks before sending: weight
accepts exactly one of `remaining_weight` or `gross_weight` in grams, a gross
measurement needs a known tare and is sent as the net correction, a spool
needs a product with a package weight, and a spool still assigned to a slot
cannot be archived (409). Nothing is journaled.

Outcomes: 409 when Spoolman rejects the change (its status is in the message),
503 when Spoolman could not be reached and nothing was sent, and 504 when the
request was sent but the reply was lost — check Spoolman before trying again;
the bridge never retries a write on its own. Creation does not also assign a
source.

`PUT /printers/{serial}/assignment` accepts source and spool ID, plus optional
provider UUID, expected global revision and `set_slot`. It atomically moves the
spool from its previous source. Before assigning it refreshes the slot as
`GET /sources` does; a slot still stale returns 409 ("The printer hasn't
reported its AMS for N s; try again in a moment"). Chemistry must match the
printer's declaration for the slot, else 409 names both materials. With
`set_slot: true` the bridge first rewrites an untagged slot's declaration to the
spool's material and colour and waits for the printer to confirm it; tagged
slots, refill-backup slots and slots in the running print are refused. Use a
null spool ID to remove an assignment; this remains possible after the printer
has been deleted. Without `set_slot`, assigning does not write Spoolman or the
printer. Revision conflicts require refresh, not silent replacement.

### Activity feed

Spool changes are written to the printer's event feed
(`GET /api/v1/printers/{serial}/events`), which the app's Activity tab shows:
`spool_assigned` and `spool_unassigned` (owner changes), `spool_released` (the
AMS reported the slot empty, the spool unloaded, or a different spool),
`assignment_refused` (warn; the 409 message as sent), `slot_set` (a `set_slot`
rewrite), `usage_charged` (a print closed: grams per spool and the planned
total) and `usage_review` (warn; a print closed with grams it could not
charge). Tag seating and colour sync keep their `spool_adopted` and
`slot_colour_set` rows. A failed feed write is logged and never undoes the
change it describes.

## Verification

Disposable local Spoolman: tag v0.22.1, commit
`18882400442d5ea114d064a14c8708d3b8ab4352`, dependencies from its checked-in PDM
lock. Integration tests require `SPOOLMAN_TEST_URL` and reject non-loopback hosts.
They create only fixture inventory records. Tests cover record changes forwarded
to Spoolman and the usage ledger's get, put, get witness on `used_weight`.


## Native inventory display preferences

`GET /inventory/display` reads the provider-namespaced policy (`provider_id`,
`revision`, `global_enabled`, `printers`) plus `runtime_qualified`,
`effective_enabled`, `reason` and `projected_slots`. A saved preference is not
proof of runtime projection. This read does not scan RFID or write the provider.

`PUT /inventory/display` requires the full policy replacement:
`provider_id`, strict integer `expected_revision`, strict boolean
`global_enabled`, and distinct printer IDs in `printers` (maximum 64).
Stale revision/provider returns 409. While runtime qualification is absent,
requests setting either opt-in are rejected without storing a latent enable.
An explicit global-off/empty-printers replacement remains available offline.
Accepted disable immediately withdraws the controller's bindings; publication
uses only the downstream display bus. Both endpoints require inventory auth.
Preferences survive restart within the provider namespace; a replacement
provider starts disabled. Lost responses require readback, not blind retry.

## Refill preview and ownership review

All routes below require the same inventory authentication. They never call a
physical sender or change the printer's normal Auto Refill toggle.

- `POST /printers/{id}/refill/preview`: `{wires:[0,1],color:"FFFFFFFF"}`
  computes exact before/target declarations and source generations, returning a
  `preview_digest`. It stores nothing and works while execution is unqualified.
- `PUT /printers/{id}/refill`: the same fields plus `provider_id`,
  `expected_revision`, `preview_digest` records an explicit group intent.
  Unqualified execution returns 409 without saving. A changed source/declaration
  invalidates the digest. Acceptance is not physical application.
- `POST /printers/{id}/refill/disable`: `{provider_id,expected_revision}` records
  restoration intent, even if disconnected/unqualified. Response state is
  `restoration_pending`; the printer may still refill under existing aliases.
- `POST /printers/{id}/refill/reconcile`: adds `wire`, `source_generation`,
  `note` to the revision body and adopts a fresh matching actual target for an
  uncertain write. It cannot assert non-delivery or resend a command.
- `POST /printers/{id}/refill/release`: the same review body explicitly keeps an
  external declaration for a conflicted (or unsettled uncertain) slot in a
  disabled group. It never restores over that declaration. For a slot with no
  spool, send `slot_empty: true` instead of `source_generation`: the step becomes
  `released_empty`. Only a fresh report of a spool in that slot refuses it (409,
  "The printer reports a spool in slot N, so it isn't empty"). Without the flag,
  a slot the printer reports empty is 409 "Slot N is empty; confirm that to clear
  its backup step".

Stale provider/revision/source context returns 409. Two to four distinct AMS A
slots must share chemistry (`tray_type`); every member is declared as the anchor
slot's product, temperature range and colour. At most one member may hold an RFID
tag; it is then the anchor and is never written.

### Backup pair (the app's routes)

- `POST /printers/{id}/refill/backup/preview {from_wire,to_wire}` →
  `{steps:[{wire,anchor,tagged,writes,changes:[{field,before,after}],before,target}],
  restored_on_disable:true, preview_digest}`. Stores and sends nothing. 422 names
  the refusal: different material, two tags, a tag being read, an unreadable tag,
  an empty or silent slot.
- `PUT /printers/{id}/refill/backup {from_wire,to_wire,preview_digest?}` plans and
  applies while idle; a digest that no longer matches is 409 and nothing is sent.
- `DELETE /printers/{id}/refill/backup/{from_wire}` restores every changed field.
  While the printer is busy or offline it records the request (state `restoring`)
  and the restoration worker finishes it once idle. A never-written step (the
  anchor, a slot that already matched) needs no evidence, so an emptied tag slot
  never blocks removal. A step whose slot the printer reports empty settles as
  `released_empty`, even after the request, as soon as a fresh report shows it.

### Backup state

`state` (and each `backups[].state`) is evidence-only: `active` needs a fresh
report showing every step owned, the printer's backup mask for exactly these
slots and Auto Refill on. An idle P1S goes quiet: when freshness is the only
thing missing, the group is `pending` with `stale: true`, and `confirmed_at`
(epoch seconds) and `last_confirmed_age_seconds` say when this incarnation of the
group was last confirmed; show that as "last confirmed N ago". A new, removed or
retried group, a bridge restart, or fresh contrary evidence (another mask, Auto
Refill off, a slot changed) has none. Confirmations are taken from every report
the printer sends, not only when polled. After a restart a stored group is
re-adopted from the first fresh report when its slots show exactly what the group
left them as and nothing was seen to change there since the bridge started.
