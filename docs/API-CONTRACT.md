# bambu-bridge HTTP/WS API Contract — v0.1

**Status:** target shape after PR A + PR B both land. Bridge implements to this; APK consumes this.
**Audience:** the implementer building the RN/Expo P1S companion app, and bridge-server implementing the matching changes.
**Authority:** this doc — when it disagrees with current code, code is wrong.
**Source of design:** the P1S LAN protocol investigation (printer trust model, the prep-vs-progress "printed air" failure mode, the print state machine) and the companion-app UX spec in `docs/APP-UX.md`. This contract is self-contained; you do not need any other document to implement against it.

---

## ⚠ Two warnings you MUST read before you write any APK code

**1. The "moonraker · klipper" trap (full detail in §14.1.1):** the design bundle's `screens-misc.jsx` ProfileScreen renders `Backend: moonraker · klipper v0.12`. **The design was templated against a Klipper backend.** Do NOT ship that string. Replace with `Backend: bambu-bridge v{version}` and `Printer: P1S firmware {info.module.firmware_version}`. Watch for ANY Klipper-isms during port (`M105` console examples, "moonraker", `klipper.cfg`).

**2. The prep-vs-progress rule (full detail in §6.1):** the design's `job.status` enum is `idle | printing | paused | finished` only — **no `preparing`**. The bridge contract's `phase` field has `idle | preparing | printing | paused | completed | failed | unknown`. **`RUNNING + layer_num == 0` MUST render as `preparing` with an indeterminate indicator, NOT a progress bar.** This is the §6.3 air-print prevention rule baked into the wire. If you skip this and render progress during prep, the APK lies to the user and a 17-minute air-print becomes possible. The bridge derives `phase` server-side; APK trusts `phase`, NOT raw `gcode_state` or `mc_percent`.

---

## 0. Scope, versioning, and the v0 / v0.1 split

- **All paths are versioned under `/api/v1/`.** Breaking changes bump to `/api/v2/`. Additive fields don't bump.
- **v0 = APK first ship.** Three tabs: Connect/Onboard, Print, Settings. Camera 1 fps polled. No jobs-history tab. Single-printer.
- **v0.1 = follow-on.** Jobs-history tab, MJPEG camera, multi-printer tab, server-side camera frame-1 discard.
- **Each endpoint below has a `Tier` field: `v0` or `v0.1`.** APK builds v0 first; bridge implements all of v0 in PR A + PR B.

---

## 1. Transport, base URL, auth

- **Base URL:** configurable in APK Settings. Two presets — LAN (`http://<host>:8080`) and Tailscale (`http://<tailscale-name>:8080`). Stored via `expo-secure-store`.
- **Bearer token:** `Authorization: Bearer <BRIDGE_API_KEY>`. Stored via `expo-secure-store`. Same key for HTTP, WS, and media.
- **WS / media token-via-query:** for `<Image src>` and `WebSocket` where headers aren't settable, `?token=<BRIDGE_API_KEY>` is honored as an equivalent of the bearer header.
- **Fail-closed:** if the bridge is started without `BRIDGE_API_KEY`, every authenticated route returns `503 auth_not_configured`. `/health` and `/version` stay reachable.

### 1.1 Read-only viewer token (`BRIDGE_VIZ_TOKEN`)

For HA dashboard iframe embedding the bridge supports a scoped read-only
credential that is separate from the master `BRIDGE_API_KEY`.

**Configuration:** set `BRIDGE_VIZ_TOKEN=<token>` in the environment (or
`.env`).  When unset (default), the feature is off and only the master key
is accepted on every route.

**Scope — routes that accept the viz token:**

| Method | Route | Purpose |
|--------|-------|---------|
| `GET` | `/api/v1/printers/{id}` | State snapshot |
| `GET` | `/api/v1/printers/{id}/viz` | Viewer HTML page |
| `GET` | `/api/v1/printers/{id}/viz/mesh` | 3MF mesh JSON |

**All other routes require the master key.** The viz token is silently
rejected (401) on control, files, jobs, filament-memory PUT/DELETE, events,
camera, and WebSocket endpoints.  The WebSocket (`/status`) stays master-only;
do not pass the viz token as `?token=` there.

**Token delivery:** `Authorization: Bearer <BRIDGE_VIZ_TOKEN>` header, or
`?token=<BRIDGE_VIZ_TOKEN>` query param (the query param path is needed for
the `/viz` iframe URL where a browser cannot set headers).  On the snapshot
route the query param is also honored so the HA REST sensor can use a URL
without a custom header.

**Security notes:**
- The viz token grants read access to the current print job name, temperatures,
  and the 3MF mesh of the running job.  Do not expose it as broadly as a public
  URL if that data is sensitive.
- The master key is not weakened when a viz token is configured; it continues
  to work everywhere.
- Constant-time comparison (`hmac.compare_digest`) is used for both tokens.

### 1.2 Auth response codes

| HTTP | `error` enum | Cause | APK action |
|---|---|---|---|
| 401 | `auth_missing` | no `Authorization` header, no `?token` | prompt for API key |
| 401 | `auth_invalid` | mismatch | prompt for API key (re-enter or scan QR) |
| 503 | `auth_not_configured` | bridge has no API key set | show "bridge is not set up" with `deploy/DEPLOY.md` link |

---

## 2. Universal error envelope

Every 4xx/5xx response on every endpoint MUST use this shape:

```json
{
  "error": "<stable_enum_string>",
  "message": "<short human sentence, end-user-facing>",
  "likely_cause": "<optional enum>",
  "remediation_hint": "<optional human sentence with literal menu paths>",
  "context": {
    "printer_id": "01P00A3C…643",
    "job_id": "...",
    "transport_phase": "tls_handshake | mqtt_connack | mqtt_no_telemetry | ftps_login | ...",
    "last_attempt_at": "2026-05-20T03:14:15.926Z",
    "last_failure_phase": "mqtt_connack"
  },
  "_raw": {
    "exc_type": "MqttError",
    "exc_str": "rc=5 not authorised"
  }
}
```

**Note on naming:** `context.transport_phase` (the *connection* phase) is intentionally distinct from `phase` at the snapshot root (the *print lifecycle* phase). Different namespace, different enum, never collide. Older contract drafts called the envelope field `context.phase`; renamed to `transport_phase` so codegen can disambiguate.

**Rules:**
- `error` is a **stable enum string**, flat (not hierarchical). Never free text. **Canonical source of truth: `src/bambu_bridge/api/errors.py` `ERR_*` constants.** If this doc disagrees with `errors.py`, the constants win and the doc is stale.
- `message` is the **APK's primary user copy**. Bridge owns the words. APK renders verbatim. No protocol nouns. **`message` MUST be non-empty** — beware the `str(TimeoutError())==""` trap, where an exception stringifies to the empty string. When the underlying exception's `str()` is empty, format as `"{type(exc).__name__}: {repr(exc)}"` or fall back to the per-status default message.
- `remediation_hint` includes the literal printer-screen menu path when applicable ("Settings ▸ WLAN ▸ Access Code"). APK renders verbatim.
- `transport_phase` (renamed from `phase`) lives **under `context`**, never at top level.
- **`_raw` is dev-mode-only.** Bridge MUST gate `_raw` emission behind `settings.bridge_dev_mode` (or equivalent env var). Default production: `_raw` is OMITTED from the wire. APK Settings has a Dev Mode toggle; turning it on tells the bridge to include `_raw`. Without the gate, stack traces and protocol exception strings leak by default.
- **Top-level keys enumeration (the full set bridge may emit):** `error`, `message`, `likely_cause?`, `remediation_hint?`, `context`, `_raw?`, `discovered?`, `actions?`, `issues?`, `existing_printer_id?`, `active_job_ids?`, `active_jobs?`, `deleted_job_ids?`, `discovered_serial?`. Per-endpoint additions ride here (not splatted ad-hoc). When a new endpoint needs a new top-level key, **add it to this list first**, then implement.
- Legacy `detail: <string>` is the fallback ONLY when the envelope can't be assembled (panic path). Format: `"{ExceptionType}: {message}"` — never empty.
- **All times in the API are ISO-8601 UTC strings** (`"2026-05-20T03:14:15.926Z"`). Bridge's internal DB uses unix seconds; the API layer serializes as ISO-8601. APK parses to native Date. **Bridge ships numbers (minutes, seconds, percent); APK formats `"1h 47m"` / `"11:17"` locally per i18n.** Never ship pre-formatted time strings.
- **HTTP status code conventions:** `401 auth_invalid` / `auth_missing` is reserved for **Bearer key** (bridge API auth) failures. **`printer_auth_failed` returns 403** to avoid the 401 collision — the APK must distinguish "your bridge key is wrong" (re-enter API key) from "your printer code is wrong" (re-onboard printer); collapsing both to 401 forces body-sniffing.

### 2.1 Standard `error` enum (extend per-endpoint; reserve these globally)

```
auth_missing, auth_invalid, auth_not_configured,
not_found, conflict, invalid_input, internal_error,
printer_offline, printer_unreachable, printer_auth_failed,
mqtt_no_telemetry, ftps_failed, ftps_auth_failed,
invalid_3mf, print_submit_failed, print_command_failed,
camera_unavailable, camera_no_frame
```

---

## 3. Onboarding — `POST /api/v1/printers` (the three-fork contract)

**Tier:** v0. **PR:** A. **The headline endpoint.** Gets the APK from "IP + access code" to "registered, connected printer."

### 3.1 Request

```http
POST /api/v1/printers
Authorization: Bearer <key>
Content-Type: application/json

{
  "host": "192.168.1.42",          // required — printer IP, no port
  "access_code": "abcd1234",       // required — 8-char code from printer screen
  "friendly_name": "Living-room P1S"  // optional — defaults to discovered serial
}
```

**No `serial` field.** Bridge derives it from the TLS leaf cert CN at `host:8883`. **Sending `serial` is a 422.**

### 3.2 Bridge probe sequence

1. TCP connect to `host:8883` with TLS 1.2 (the P1S rejects TLS 1.3 with a handshake_failure).
2. Extract leaf cert subject CN → `serial`.
3. MQTT CONNECT with username=`bblp`, password=`access_code`, client_id=`serial`.
4. On CONNACK 0, publish `pushall`; await first `report` (5s timeout).
5. On first `report`, persist + return 201.

### 3.3 Success response (201)

```json
{
  "printer_id": "01P00A3C…643",
  "serial": "01P00A3C…643",
  "model": "P1S",
  "friendly_name": "Living-room P1S",
  "connected": true,
  "first_telemetry_at": "2026-05-20T03:14:18.402Z"
}
```

### 3.4 The three failure forks (E1 / E2 / E3)

**Fork E1 — TCP/TLS unreachable** (wrong IP, printer off, different VLAN):

```http
502 Bad Gateway
{
  "error": "printer_unreachable",
  "message": "Can't reach the printer at 192.168.1.42.",
  "remediation_hint": "Check that the printer is powered on, the screen is awake, and this device is on the same Wi-Fi network (not a Guest network). The IP can change — confirm at Settings ▸ WLAN ▸ IP on the printer.",
  "context": { "transport_phase": "tls_handshake", "host": "192.168.1.42", "port": 8883 }
}
```

**Fork E2 — MQTT `CONNACK 5`** (wrong access code OR LAN-Only Mode off — protocol can't disambiguate):

The bridge's TLS handshake succeeded, so the **cert CN is already known**. Return it under `discovered` so the APK can confirm-the-printer-was-reached without putting the serial into user-facing copy. `message` stays serial-free.

```http
403 Forbidden
{
  "error": "printer_auth_failed",
  "message": "We reached your P1S but it refused the access code.",
  "likely_cause": "lan_mode_off_or_wrong_access_code",
  "remediation_hint": "Two things to check, in this order:\n(1) On the printer, Settings ▸ Network ▸ LAN-Only Mode must be ON.\n(2) Settings ▸ WLAN ▸ Access Code — case-sensitive, regenerable.",
  "context": { "transport_phase": "mqtt_connack" },
  "_raw": { "connack_code": 5 },
  "discovered": { "serial": "01P00A3C…643", "model": null }
}
```

**Note three Frodo-driven changes vs prior contract draft:**
- HTTP status is **403** (not 401) — avoids collision with bearer-auth 401.
- Serial is **NOT in `message`** — demoted to `discovered`. APK can still render "We reached your P1S 01P00A3C…643" by composing `message` + `discovered.serial` itself if it wants.
- `remediation_hint` lists **LAN-Only Mode first** — more common first-time cause than wrong access code.
- `model` is **nullable in v0** — bridge derives model from `info.module[]` after first telemetry, which hasn't arrived yet during E2. APK falls back to "P1S" assumption or "printer" generic.

**Fork E3 — authed but silent after `pushall`** (CONNACK 0, no telemetry within 5s — Developer Mode off, or LAN-mode flapping):

```http
502 Bad Gateway
{
  "error": "mqtt_no_telemetry",
  "message": "Connected — but the printer is ignoring requests. This usually means LAN-Only Mode is off.",
  "likely_cause": "developer_mode_off_or_lan_drop",
  "remediation_hint": "On the printer, enable Settings ▸ Network ▸ LAN-Only Mode. Then try again.",
  "context": { "transport_phase": "mqtt_no_telemetry", "telemetry_wait_ms": 5000 },
  "discovered": { "serial": "01P00A3C…643", "model": null }
}
```

Note: `message` names the likely cause directly (per Frodo war-council finding); old version ("isn't sending status") was mealy. Key renamed `post_pushall_wait_ms` → `telemetry_wait_ms` to drop the `pushall` protocol noun.

### 3.5 Other failures

| HTTP | `error` | Cause |
|---|---|---|
| 409 | `conflict` | a printer with this serial is already registered; response includes `existing_printer_id` for the APK to redirect to GET /printers/{id} |
| 422 | `invalid_input` | malformed body — bad IP, missing access_code, sent `serial` field |
| 503 | `auth_not_configured` | bridge has no `BRIDGE_API_KEY` |

### 3.6 Cert TOFU change (rare, post-firmware-update)

The printer's leaf cert regenerates on every firmware update. The bridge MUST:
- On register, store the leaf cert fingerprint keyed by serial.
- On every connect, compare. On mismatch: **return 403 not 5xx** — clean error, not a generic transport failure.

```http
403 Forbidden
{
  "error": "printer_cert_changed",
  "message": "Your P1S's security key changed. This is normal right after a firmware update.",
  "remediation_hint": "If you just updated firmware on 01P00A3C…643, tap Trust.",
  "context": { "previous_fingerprint": "...", "current_fingerprint": "..." },
  "actions": [
    { "id": "trust", "label": "Trust this printer", "method": "POST", "path": "/api/v1/printers/{id}/trust" },
    { "id": "deny", "label": "Not now", "method": null }
  ]
}
```

Never the words "certificate," "MITM," "fingerprint" in `message`. Hex in `context._raw` only.

---

## 4. Printer CRUD

### 4.1 `GET /api/v1/printers` — list

**Tier:** v0. Returns a list of `PrinterSummary` (compact, list-view shape):

```json
[
  {
    "printer_id": "01P00A3C…643",
    "serial": "01P00A3C…643",
    "friendly_name": "Living-room P1S",
    "model": "P1S",
    "connected": true,
    "phase": "preparing",
    "phase_reason": "heating_nozzle",
    "subtask_name": "Benchy_PETG.gcode.3mf",
    "progress": { "layer_num": 0, "total_layer_num": 240, "percent": null, "estimate_min": 56 },
    "last_telemetry_at": "2026-05-20T03:14:15.926Z"
  }
]
```

**Note:** `progress.percent` is **null until `layer_num > 0`**. This is the prep-vs-progress rule (see §6.0.1) enforced server-side. APK renders the headline from `phase` alone; never derives a percent from `mc_percent` when `layer_num == 0`.

### 4.2 `GET /api/v1/printers/{printer_id}` — full state

**Tier:** v0. Returns the **translated snapshot** (see §6 for shape).

### 4.3 `PATCH /api/v1/printers/{printer_id}` — update

**Tier:** v0. Body:

```json
{ "friendly_name": "Garage P1S", "ip": "192.168.1.43", "access_code": "wxyz5678" }
```

All fields optional. On `ip` or `access_code` change, bridge tears down + reconnects MQTT. Returns the updated `PrinterSummary`. Failures use the three-fork errors of §3.4.

### 4.4 `DELETE /api/v1/printers/{printer_id}` — remove

**Tier:** v0. `204 No Content` on success. `404 not_found` if unknown. **Query `?cascade_jobs=true|false` (default `false`):** when `true`, also deletes job rows for this printer; when `false` and any non-terminal job exists, returns `409 conflict` with `{active_job_ids: [...]}`.

### 4.5 `POST /api/v1/printers/{printer_id}/trust` — TOFU re-trust

**Tier:** v0. Empty body. Accepts the current leaf cert fingerprint, replaces the stored one. Returns `204 No Content`.

---

## 5. Live status WebSocket

### 5.1 `WS /api/v1/printers/{printer_id}/status`

**Tier:** v0. Auth via `Authorization: Bearer` header OR `?token=<key>`.

**Wire sequence per connection:**
1. Server → `{"type": "hello", "protocol_version": 1, "printer_id": "...", "server_time": "..."}`
2. Server → `{"type": "snapshot", "data": <full translated state from §6>}`
3. Server → `{"type": "delta", "data": <translated changed leaves>}` repeatedly
4. Server → `{"type": "event", "event": "<name>", "data": {...}}` on transitions
5. Server → `{"type": "ping"}` every 30s; client replies any frame (or pong text); client ALSO sends `{"type": "pong"}` every 30s as keepalive

### 5.2 Close codes (`code`, `reason` — both stable)

| Code | `reason` | Meaning | APK action |
|---|---|---|---|
| 1000 | `normal_close` | client closed | — |
| 1008 | `unauthorized` | bad/missing token | re-fetch API key, do not auto-reconnect |
| 1008 | `unknown_printer` | printer_id not registered | navigate to printer list |
| 1011 | `internal_error` | bridge crash on this stream | reconnect with backoff |
| 4000 | `protocol_version_mismatch` | reserved for future | show "app update needed" |

### 5.3 Delta semantics

- Deltas carry **only fields that changed**, deep-nested (e.g. `{"state": {"nozzle_temper": 254.5}}`).
- Client MUST deep-merge into the cached snapshot. **Never render from the latest delta alone**: a single 87-byte delta carries only the fields that changed, so rendering from it directly leaves most fields blank.
- On reconnect, the next snapshot is the new source of truth — discard the local merged cache and reseed.

### 5.4 Reconnect (client side, this is APK behavior)

- Exponential backoff: 1s, 2s, 4s, 8s, capped at 30s.
- During disconnect: **dim the last-known cached state** and show `Reconnecting · last update 14 s ago` using `last_telemetry_at`. Do not blank.
- On 1008/unauthorized or 1008/unknown_printer: stop, surface the error.

---

## 6. Translated state shape (the snapshot/delta contract)

This is the shape inside `GET /printers/{id}` response.data and `{type:snapshot,data:...}` and the `data` of every `delta`.

```json
{
  "printer_id": "01P00A3C…643",
  "serial": "01P00A3C…643",
  "friendly_name": "Living-room P1S",
  "model": "P1S",

  "session": {
    "connected": true,
    "last_telemetry_at": "2026-05-20T03:14:15.926Z",
    "last_connect_attempt": "2026-05-20T03:09:01.000Z",
    "last_failure_phase": null
  },

  "phase": "preparing",
  "phase_reason": "heating_nozzle",

  "headline": {
    "title": "Preparing",
    "subtitle": "Heating nozzle 75/255 °C · leveling bed",
    "indicator": "indeterminate"
  },

  "job": {
    "subtask_name": "Benchy_PETG.gcode.3mf",
    "layer_num": 0,
    "total_layer_num": 240,
    "percent": null,
    "estimate_total_min": 56,
    "remaining_min": null,
    "started_at": "2026-05-20T03:14:01.000Z"
  },

  "temps": {
    "nozzle": { "current_c": 75.2, "target_c": 255.0 },
    "bed":    { "current_c": 36.1, "target_c": 70.0 },
    "chamber":{ "current_c": 28.0, "target_c": null }
  },

  "cooling": {
    "part_fan":    { "percent": 0,  "_raw": "0"  },
    "aux_fan":     { "percent": 53, "_raw": "8"  },
    "chamber_fan": { "percent": 0,  "_raw": "0"  }
  },

  "lights": { "chamber_on": true },

  "print_params": {
    "speed_mm_s": 220,
    "flow_pct": 100
  },

  "motion": {
    "x": 117.4, "y": 89.2, "z": 28.4, "e": 142.6
  },

  "ams": {
    "present": true,
    "engaged_slot": 1,
    "slots": [
      { "physical_slot": 1, "type": "ASA",  "color": "#1E88E5", "rfid_tray": "GFB61", "state": "loaded",   "remaining_g": 412, "remaining_pct": 41, "_raw_id": 0 },
      { "physical_slot": 2, "type": "PETG", "color": "#212121", "rfid_tray": "GFG99", "state": "loaded",   "remaining_g": null, "remaining_pct": null, "_raw_id": 1 },
      { "physical_slot": 3, "type": "ASA",  "color": "#E53935", "rfid_tray": "GFB61", "state": "empty",    "remaining_g": 0,   "remaining_pct": 0,   "_raw_id": 2 },
      { "physical_slot": 4, "type": "ASA",  "color": "#FAFAFA", "rfid_tray": "GFB61", "state": "loaded",   "remaining_g": null, "remaining_pct": null, "_raw_id": 3 }
    ],
    "external_spool": { "in_use": false, "type": null, "color": null, "_raw_id": 254 }
  },

  "print_error": null,

  "_raw": {
    "gcode_state": "RUNNING",
    "mc_percent": 10,
    "tray_now": "1",
    "print_error": 0,
    "info": { /* full info category preserved */ },
    "mc_print": { /* full mc_print category preserved */ }
  }
}
```

### 6.0.1 ⚠ The `phase` rule (the single most important rule in this doc)

`RUNNING + layer_num == 0` is **heat-soak and bed leveling, not printing.** The bridge MUST emit:

- `phase: "preparing"` when `gcode_state == RUNNING && layer_num == 0`
- `phase: "printing"` ONLY when `gcode_state == RUNNING && layer_num > 0`

The APK MUST render `phase: "preparing"` as **"Preparing"** with an **indeterminate** indicator (no progress bar, no percentage). Show live nozzle/bed temp climbing toward target as the real progress signal. Only show a percentage progress bar when `phase == "printing"`.

If you skip this rule and render `mc_percent` during `preparing`, the APK shows fake progress for ~5 minutes while the printer is heating, then a 17-minute "printed air" failure becomes possible (the printer reports a percent before the first layer exists). The APK has no way to detect this — bridge enforces the boundary.

**The design preview's `job.status` enum (`idle | printing | paused | finished`) does NOT include `preparing`. When porting the design, add it.**

### 6.1 Field translation table (PR B implements)

| User-facing field | Source (raw `_state` key) | Translation rule |
|---|---|---|
| `phase` | `gcode_state` + `layer_num` | enum: `idle \| preparing \| printing \| paused \| completed \| failed \| unknown`. **`RUNNING + layer_num == 0` → `preparing`**, **`RUNNING + layer_num > 0` → `printing`**. The single most important rule in this doc. |
| `phase_reason` | derived | `heating_nozzle`, `heating_bed`, `leveling`, `purging`, `printing_layers`, `cooling_down`, `feed_not_engaged`, etc. APK uses this to render the subtitle. |
| `job.percent` | `mc_percent` | **`null` when `phase != "printing"`**. NEVER expose `mc_percent` during `preparing` — it lies (see §6.0.1). |
| `temps.*.current_c` | `nozzle_temper`, `bed_temper`, `chamber_temper` | numeric pass-through |
| `temps.*.target_c` | `nozzle_target_temper`, `bed_target_temper`, … | numeric pass-through |
| `cooling.*.percent` | `cooling_fan_speed`, `big_fan1_speed`, `big_fan2_speed` (P1S native 0–15 string) | **percent = round(raw * 100 / 15)**; `_raw` retains the original string |
| `ams.present` | `ams.ams_exist_bits` | bool. Hex-ish bitmask string; **nonzero ⇒ AMS hardware attached**, absent/empty/all-zero ⇒ `false`. Independent of `ams.ams[]` so it stays `true` across an RFID re-scan (see §6.1.1). |
| `ams.slots[].physical_slot` | array index of `ams.tray[]` | **physical_slot = raw_id + 1**. `_raw_id` retains the 0-based. |
| `ams.engaged_slot` | `ams.tray_now` | `255 → null` (nothing engaged), `254 → "external"`, `0-3 → 1-4` (physical slot). The §6.3 feed-confirmation signal. |

**AMS tray/slot numbering convention (canonical):** Tray/slot indices are **0-based protocol indices end-to-end** on the wire between the bridge and the printer. The `_raw_id` field in `ams.slots[]` carries the 0-based value; `physical_slot` is `_raw_id + 1` for display. The `/ams/change` endpoint takes `target_tray` as a 0-based protocol index (0–3). The APK MUST NOT pass physical slots (1–4) to `/ams/change` without subtracting 1 first. The `/jobs` submission endpoint uses `ams_mapping` in physical-slot space (1–4) because that is what the user selects; the bridge converts internally.
| `print_error` | `print_error` (int) + HMS table | When 0/null: `null`. When non-zero: `{code, hex, text, category, severity, _raw}`. Decode hex from int (`50348044 → 0x0300400C`). Text from HMS lookup (ship a copy of the ha-bambulab `hms_error_text/` table). **The `print_error` channel is SEPARATE from the `hms[]` string array.** |
| `session.last_telemetry_at` | bridge bookkeeping (touch on every report) | ISO 8601 UTC |
| `session.last_connect_attempt` | bridge bookkeeping | ISO 8601 UTC |
| `session.last_failure_phase` | bridge bookkeeping (set on connect failure, cleared on success) | enum: `tls_handshake`, `mqtt_connack`, `mqtt_no_telemetry`, `ftps_login`, or `null` |
| `print_params.speed_mm_s` | `mc_print_speed` or `spd_mag` | integer mm/s; numeric, NOT formatted |
| `print_params.flow_pct` | `flow_rate` or `m1` | integer percent (0-200 ish; design clamps in UI) |
| `ams.slots[].remaining_g` | `ams.tray[i].remain` (P1S sometimes ships grams) | integer or `null` if P1S didn't report |
| `ams.slots[].remaining_pct` | derived from `remaining_g` assuming ~1000g spool, OR direct from `remain` when shipped as % | integer 0-100 or `null` |
| `motion.{x,y,z,e}` | `mc_print_sub_stage` / position fields (v0.1 — bridge needs to surface) | numeric mm |

### 6.1.1 ⚠ `ams.present` vs `ams.slots == []` — the RFID re-scan boundary

`ams.slots` comes from `ams.ams[0].tray[]`, which the P1S **transiently empties to `[]`** during an RFID re-scan — physically nudging the AMS, inserting/removing a spool, or a periodic tag poll. During that window the raw report is `ams.ams == []` while `ams_exist_bits` stays `"1"` (hardware never left). `ams.present` is derived from `ams_exist_bits`, NOT from `ams.ams[]`, so it does not flap during a scan.

The two states clients MUST distinguish:

| `ams.present` | `ams.slots` | Meaning | Client rule |
|---|---|---|---|
| `false` | `[]` | **No AMS hardware** on this printer | Show "No AMS detected" |
| `true` | `[…4 slots…]` | AMS attached, settled | Render the slots |
| `true` | `[]` | **AMS attached, RFID re-scan in progress** | **Hold the previous slot view** (render last-known slots, optionally dimmed) and show "AMS re-scanning…" — do NOT render "No AMS detected", and do NOT render the raw transient |

The bridge does not buffer the previous slot view server-side (the snapshot is stateless per §6); `present && slots == []` is the explicit signal that lets the **client** hold its last non-empty slots across the transient instead of latching an empty/"gibberish" state.

**Tagless spools (no RFID tag — common):** `tray_uuid` / `tray_id_name` arrive all-zero / empty. Slots therefore carry **no human name** — `rfid_tray` is `null` and there is no `tray_id_name` in the contract at all. Clients MUST render a slot from `type` + `color` only (e.g. an "ASA" chip in the tray color), never from a tag-derived name or info code.

### 6.2 The `headline` block — APK rendering rule

The headline is the bridge's recommendation for the dashboard title/subtitle. APK renders it verbatim. Source of truth (PR B implements this in `service/printer.py`):

| `phase` | `title` | `subtitle` rule | `indicator` |
|---|---|---|---|
| `idle` | `"Ready"` | last completed job name + result, else `"Ready to print"` | none |
| `preparing` | `"Preparing"` | `phase_reason`-driven copy ("Heating nozzle 75/255 °C · leveling bed") | indeterminate |
| `printing` | `"Printing"` | `"Layer {layer_num}/{total_layer_num} · {percent}% · ~{remaining_min} min left"` | progress (percent) |
| `paused` | `"Paused"` | `"Tap Resume to continue"` | amber |
| `completed` | `"Done"` | subtask_name + `" · completed"` | green |
| `failed` | `"Print failed"` | `print_error.text` (HMS lookup), sticky | red |
| `unknown` | `"Connecting…"` | `"Loading status from your P1S"` | indeterminate |

If `session.connected == false`: APK overrides title with `"Reconnecting…"` and subtitle with `"Last update {n}s ago"` using `last_telemetry_at`. Phase doesn't drive the headline when disconnected.

---

## 7. Print submission — `POST /api/v1/printers/{printer_id}/jobs`

**Tier:** v0. **PR:** A (sync validate lift). **The §6.3 boundary.**

### 7.1 Request

```http
POST /api/v1/printers/{printer_id}/jobs
Authorization: Bearer <key>
Content-Type: multipart/form-data

file: <binary .gcode.3mf>
ams_mapping: "1,3"             // optional; comma-separated PHYSICAL slot numbers (1-4)
external_spool: "false"        // optional; if "true", use external spool (vt_tray)
```

**`ams_mapping` is in PHYSICAL-SLOT space** (1-4), not the 0-based protocol space. Bridge maps to `0-3` internally. **APK MUST NOT pass 0.** If 0 appears, return 422.

### 7.2 Synchronous pre-validation (BEFORE 201)

The bridge MUST run `slicedoc.validate()` synchronously inside `JobManager.submit()` BEFORE creating the job row. Failures → 422 with structured issues:

```http
422 Unprocessable Entity
{
  "error": "invalid_3mf",
  "message": "This .gcode.3mf can't be printed safely.",
  "issues": [
    { "code": "G3", "category": "md5",        "message": "md5 is not UPPERCASE" },
    { "code": "G5", "category": "ams",        "message": "ams_mapping arity 1 != slice_info <filament> count 2 (§6.3 trap)" },
    { "code": "G4", "category": "temperature", "message": "nozzle 320 °C > 280 °C" }
  ],
  "context": { "validator_version": "1" }
}
```

Gates G1–G5 from `slicedoc/validate.py`: zip integrity, required members, md5 contract, temperature envelope, AMS consistency. **All five are 422-class — fix the file, don't retry the same file.**

Other 422 reasons:
- `printer_offline` — bridge can't reach printer right now (also a class of "no point uploading")
- `ams_slot_invalid` — passed `ams_mapping: [0]` or a slot not in 1-4
- `ams_slot_empty` — passed `ams_mapping: [2]` but slot 2 is empty per current AMS state
- `ams_material_mismatch` — bridge knows the slice expects PETG and the chosen slot has ASA. **Warning, returnable as `422` only when client passed `confirm_mismatch: false`**; if `confirm_mismatch: true`, accepted

### 7.3 Success response (201)

```json
{
  "job_id": "abcdef123...",
  "printer_id": "01P00A3C…643",
  "state": "queued",
  "file_name": "Benchy_PETG.gcode.3mf",
  "queued_at": "2026-05-20T03:13:55.000Z",
  "ams_mapping": [2],
  "validation": { "ok": true, "gates_passed": ["G1","G2","G3","G4","G5"] }
}
```

**`state: "queued"` means accepted + validated, awaiting FTPS upload.** It does NOT mean printing. The APK MUST NOT show "Printing" on this response. APK shows "Submitted — waiting for the printer to start."

### 7.4 The state-machine after 201

The job advances through these states (subscribe to `WS /printers/{printer_id}/status` for `event` messages with `event: "job_state_change"`):

```
queued → uploading → submitted → preparing → printing → completed
                          │            │           │
                          └────────────┴─→ failed ◄┘
                                      └─→ canceled
```

| State | Trigger | APK headline |
|---|---|---|
| `queued` | sync validate passed | "Submitted — waiting" |
| `uploading` | FTPS upload begins | "Uploading file" |
| `submitted` | `project_file` published, printer returned `result:"success"` | "Submitted — preparing soon" |
| `preparing` | first non-IDLE gcode_state seen | "Preparing — heating & leveling" |
| `printing` | `layer_num > 0` | "Printing" + progress bar |
| `paused` | `gcode_state == PAUSE` | "Paused" |
| `completed` | `gcode_state == FINISH` AND `layer_num == total_layer_num` | "Done" |
| `failed` | `gcode_state == FAILED` OR `print_error != 0` OR `FED_NO_PROGRESS` (10min) | "Print failed" (sticky) |
| `canceled` | user-initiated stop confirmed | "Stopped" |

**Note: state enum renames vs current code (PR B):**
- `started` → `submitted` (clarity: printer accepted, not begun)
- new state `preparing` between `submitted` and `printing`
- `printing` gated on `layer_num > 0`
- `canceled` (US single-l) — **kept**, matches existing `JobState.CANCELED` and existing DB rows. Deliberate inconsistency with `completed`; not worth a migration for cosmetic uniformity.

---

## 8. Jobs CRUD

### 8.1 `GET /api/v1/jobs` — list/history

**Tier:** v0.1 (APK has no history tab in v0; endpoint exists). Query: `printer_id`, `state`, `since`, `until`, `limit`, `offset`. Returns `[Job, ...]`.

### 8.2 `GET /api/v1/jobs/{job_id}` — detail with event log

**Tier:** v0. Returns:

```json
{
  "job": {
    "job_id": "...",
    "printer_id": "...",
    "file_name": "...",
    "file_path": "/sdcard/Benchy_PETG.gcode.3mf",
    "state": "printing",
    "progress_pct": 12.5,
    "layer_current": 30,
    "layer_total": 240,
    "queued_at": "...",
    "started_at": "...",
    "finished_at": null,
    "duration_s": null,
    "filament_used_g": null,
    "error_code": null
  },
  "events": [
    { "ts": 1747700400000, "event_type": "job_created",  "payload": {...} },
    { "ts": 1747700401000, "event_type": "state_change", "payload": {"from":"queued","to":"uploading","trigger":"ftps_begin"} },
    ...
  ]
}
```

### 8.3 `POST /api/v1/jobs/{job_id}/cancel`

**Tier:** v0. Empty body. Response:

```json
{
  "job_id": "...",
  "command_accepted": true,        // bridge sent print.stop to printer
  "print_stopped": false,          // not yet confirmed via gcode_state — async
  "state": "printing"              // still printing until printer FINISHes/FAILs
}
```

**`command_accepted` vs `print_stopped` are distinct.** Same §6.3 logic: command-parsed ≠ thing-happened. The APK shows "Stopping…" on `command_accepted: true, print_stopped: false`, transitions to "Stopped" on the WS event `state_change → canceled`.

### 8.4 `GET /api/v1/printers/{printer_id}/events`

**Tier:** v0. **The flat per-printer event feed** for the APK's NotificationsScreen (design `app.jsx` `buildEvents()` consumes this shape).

Query: `since=<ISO-8601>`, `until=<ISO-8601>`, `limit=` (default 50, max 200), `severity=info|warn|error`.

```json
[
  {
    "id": 12345,
    "ts": "2026-05-20T09:30:00Z",
    "severity": "info",
    "kind": "print_started",
    "title": "Print started",
    "detail": "pegboard_hook_v3 · 334 layers · est. 3h 05m",
    "context": "profile · 0.2mm · 220°/65° · PLA Basic A1",
    "job_id": "abcdef...",
    "dismissed": false
  },
  {
    "id": 12346,
    "ts": "2026-05-20T09:34:00Z",
    "severity": "warn",
    "kind": "filament_runout",
    "title": "Filament runout — Slot 3",
    "detail": "Optical sensor detected end-of-spool. Print paused.",
    "context": "layer 142 · 67.4mm of extrusion remaining for hold",
    "job_id": "abcdef...",
    "dismissed": false
  }
]
```

`kind` enum: `print_started`, `print_completed`, `print_failed`, `filament_runout`, `error`, `feed_warning`, `bed_level_passed`, `spool_low`, `firmware_update`, `connection_lost`, `connection_restored`. Bridge derives these from the persistent events table (`db/jobs.py` EventRepo) + the in-memory bus events. **Already-emitted WS events have a row here** with `kind` matching the WS event name.

`severity` is bridge-assigned: `info` for benign transitions, `warn` for `filament_runout` / `feed_warning` / `spool_low` / `door`, `error` for `print_failed` / thermal / `error`.

`title` + `detail` + `context` are pre-formatted (English-first; i18n is v0.1). APK renders verbatim.

### 8.5 `POST /api/v1/printers/{printer_id}/events/{id}/dismiss`

**Tier:** v0. Marks an event as dismissed. Empty body. Returns `204 No Content`. Idempotent.

### 8.6 `POST /api/v1/printers/{printer_id}/events/clear`

**Tier:** v0. Bulk-dismiss every event for this printer. Empty body. Returns `204 No Content`. Idempotent. Drives the NotificationsScreen "Clear all" action — cheaper than N individual dismiss calls.

---

## 9. Control endpoints

All under `/api/v1/printers/{printer_id}/`. All require the printer to be `connected`; offline → `409 printer_offline` with `last_connect_attempt`/`last_failure_phase` in context.

| Method | Path | Body | Notes | Tier |
|---|---|---|---|---|
| POST | `/print/pause` | — | pause active print | v0 |
| POST | `/print/resume` | — | resume paused print | v0 |
| POST | `/print/stop` | — | stop active print (same body as POST /jobs/{id}/cancel response semantics) | v0 |
| POST | `/light` | `{"on": bool}` | chamber light on/off | v0 |
| POST | `/temperature` | `{"nozzle": int?, "bed": int?}` | both optional but one required. Range-validated server-side: nozzle ≤280 °C (stainless nozzle, default) or ≤300 °C when `printers.nozzle_type=hardened_steel`; bed ≤120 °C always. APK fires liberally; bridge 422s out-of-range. | v0 |
| POST | `/fan` | `{"part": "part\|aux\|chamber", "percent": 0-100}` | bridge translates to native 0-15 | v0 |
| POST | `/speed` | `{"level": 1-4}` | 1 silent · 2 standard · 3 sport · 4 ludicrous. **Response echoes labels** so APK doesn't hardcode i18n. | v0 |
| POST | `/gcode` | `{"line": "G28"}` | raw G-code. Marked `safety: false`. APK should NOT expose to user UI casually; reserve for an explicit "advanced" pane. | v0.1 |
| POST | `/home` | — | G28 home all axes | v0.1 |
| POST | `/move` | `{"axis": "X\|Y\|Z", "distance_mm": float, "feed_mm_min": int}` | relative jog | v0.1 |
| POST | `/ams/control` | `{"action": "pause\|resume\|reset"}` | AMS state machine control | v0.1 |
| POST | `/ams/change` | `{"target_tray": 0-3, "cur_temp": int, "tar_temp": int}` | mid-print filament change. **`target_tray` is a 0-based protocol index** (0–3); the APK converts from physical slot (1–4) before sending. | v0.1 |
| POST | `/filament/unload` | — | unload current filament | v0.1 |
| POST | `/work_light` | `{"mode": "on\|off\|flashing", "loop_times": int?, "interval_time": int?}` | work/task light; mode `"flashing"` accepts `loop_times` (default 1; 0=forever) and `interval_time` ms (default 500). Not all P1S configs have this node; printer ignores when absent. | v0.1 |
| POST | `/ipcam/record` | `{"enabled": bool}` | enable/disable print video recording to SD card | v0.1 |
| POST | `/ipcam/timelapse` | `{"enabled": bool}` | enable/disable timelapse generation to SD card | v0.1 |
| POST | `/get_version` | — | request firmware/module version refresh; printer echoes `info.module[]`; bridge stores under `state["info"]` | v0.1 |
| POST | `/command` | `{"category", "command", "params"}` | **Dev escape hatch.** Marked `safety: false`. Not exposed in APK UI. Reserved for advanced/debug. | v0.1 |

**Response shape (success):** `{"sent": <envelope sent to printer>, "sequence_id": "..."}`. The presence of this response means **command accepted by bridge and forwarded to printer**, NOT confirmed by printer. APK relies on WS state changes for actual confirmation.

---

## 10. Files

All under `/api/v1/printers/{printer_id}/files/`. **Tier:** v0.1 (APK doesn't expose file browser in v0 — file upload is via `POST /jobs`).

| Method | Path | Tier | Notes |
|---|---|---|---|
| POST | `/files` | v0.1 | upload `.gcode.3mf` to printer's persistent storage (`model/`); returns `{path, name}` |
| GET | `/files?dir=\|cache\|timelapse` | v0.1 | list files sorted newest-first; see §10.1 for response shape |
| GET | `/files/{filename}?dir=\|cache\|timelapse` | v0.1 | download |
| DELETE | `/files/{filename}` | v0.1 | delete |

**Error mapping (PR B fixes):**
- FTPS auth failure → `401 ftps_auth_failed` (NOT 502 with bare `str(exc)` — Frodo CRIT)
- FTPS network/protocol failure → `502 ftps_failed` with typed-exception envelope
- Invalid `dir` param → `422 invalid_input`

### 10.1 File listing response shape (GET /files)

Files are returned **newest-first** using the sliced → modified → created
timestamp priority chain.  Each entry in `files` is an object:

```json
{
  "dir": "",
  "files": [
    {
      "name": "benchy.gcode.3mf",
      "sliced_at": "2026-05-19T07:37:44Z",
      "modified_at": "2026-05-19T07:37:44Z",
      "created_at": null,
      "sort_basis": "sliced"
    },
    {
      "name": "old_project.gcode.3mf",
      "sliced_at": null,
      "modified_at": "2024-01-15T00:00:00Z",
      "created_at": "2024-01-14T23:00:00Z",
      "sort_basis": "modified"
    },
    {
      "name": "no_timestamp.gcode.3mf",
      "sliced_at": null,
      "modified_at": null,
      "created_at": null,
      "sort_basis": "none"
    }
  ],
  "file_names": ["benchy.gcode.3mf", "old_project.gcode.3mf", "no_timestamp.gcode.3mf"]
}
```

**Timestamp field semantics:**

| Field | Source | Notes |
|---|---|---|
| `sliced_at` | Embedded in `.gcode.3mf` archive (`Metadata/slice_info.config` ZIP entry date, or gcode header comment) | Available only when the bridge has already downloaded the file for viz pre-warm. `null` otherwise. |
| `modified_at` | FTPS server `MLSD` `modify` fact (RFC 3659); or `LIST` date field (Unix-format, both `HH:MM` and `YYYY` forms); or `MDTM` per-file fallback (capped at 30 round-trips) | `null` when none of the three tiers succeeds. |
| `created_at` | FTPS server `MLSD` `create` fact (RFC 3659). Rarely supported — most firmware FTP daemons do not advertise it. | `null` when absent. |
| `sort_basis` | `"sliced" \| "modified" \| "created" \| "none"` | Records which tier determined the sort position of this entry. |

**Sort ordering:**
- Newest-first; the highest-priority non-null timestamp wins.
- Files with no timestamp at all (`sort_basis == "none"`) sort last, in stable relative order.

**Backward compatibility:** `file_names` is a flat list of names in sorted order. Pre-existing consumers that read `files` as a flat string list must migrate to `file_names`.

**`sliced_at` availability design:**
Downloading every archive to extract the slice timestamp for a listing call is unacceptable.
`sliced_at` is populated **opportunistically** — the bridge fills a per-(filename, size) memo
whenever it downloads a file for viz pre-warm (`print_started` → 15 s delay → download).
The memo is **persisted to SQLite** (`sliced_dates` table, keyed by `(filename, size_bytes)`)
and **survives bridge restarts** — including mid-print restarts.  On startup, if a live job
row exists in the database for any printer, the bridge schedules a viz pre-warm immediately,
which fills the sliced-date entry for the currently-printing file before any listing call
arrives (backfill for the restart-during-print case).
The in-memory cap has been raised to 2000 entries (from 64); entries are pruned by
`learned_at` (oldest-first, batch of 200) only when the table exceeds 2000 rows.
Clients that need authoritative slice dates should call `GET /files/{filename}` (download
the archive) and parse `Metadata/slice_info.config` or the `Metadata/plate_1.gcode` header
comment themselves.

**FTP capability negotiation (server-side, not visible to APK):**
1. **MLSD** (RFC 3659): tried first. Provides `modify` and (sometimes) `create` facts.
2. **LIST** (Unix-format): fallback when MLSD is not supported. Provides `modified_at` only.
3. **MDTM** (per-file): last resort, capped at 30 round-trips per listing. P1S firmware
   may not support MLSD; LIST is the most likely happy path on real hardware.

---

## 11. Advanced controls

All under `/api/v1/printers/{printer_id}/`. All require the printer to be connected; offline → `409`. All require the master API key — the viz token is rejected.

### 11.1 Endpoint table

| Method | Path | Body summary | Risk | Notes |
|---|---|---|---|---|
| POST | `/xcam` | `{module_name, enabled, print_halt?}` | YELLOW | `module_name` whitelist-validated to matrix-confirmed P1S set. `print_halt=true` auto-pauses on detection. |
| POST | `/print_option` | `{<flag_name>: bool, ...}` | YELLOW | Flags allowlist: `auto_recovery`, `air_print_detect`, `filament_tangle_detect`, `nozzle_blob_detect`, `sound_enable`. Unknown keys → 422. |
| POST | `/skip_objects` | `{obj_list: [int, ...]}` | YELLOW | Non-empty int list of Bambu object IDs from slice. |
| POST | `/ams/filament_setting` | `{ams_id, tray_id, tray_info_idx, tray_color, nozzle_temp_min, nozzle_temp_max, tray_type}` | YELLOW | `tray_color` = 8-char RRGGBBAA hex; `tray_type` from known materials list; `temp_min < temp_max ≤ 300`. |
| POST | `/ams/rfid` | `{ams_id, slot_id}` | GREEN | Trigger RFID re-read; no physical motion. |
| POST | `/ams/drying` | `{ams_id, temp, cooling_temp, duration, humidity, mode?, rotate_tray?}` | YELLOW | Starts AMS drying cycle; requires AMS firmware support. |
| POST | `/ams/user_setting` | `{ams_id, startup_read_option, tray_read_option}` | YELLOW | Configures RFID auto-read behaviour. |
| POST | `/calibration` | `{option: 1\|2\|4\|7, bed_type?}` | RED | P1S-confirmed bits only (matrix §8). Option 3, 5, 6, 8+ → 422 naming the matrix. |
| POST | `/set_accessories/nozzle` | `{nozzle_type, nozzle_diameter}` | YELLOW | `nozzle_type ∈ {stainless_steel, hardened_steel}`; `nozzle_diameter ∈ {0.2, 0.4, 0.6, 0.8}`. Updates in-memory temp clamp immediately. |
| POST | `/extrude` | `{distance_mm, feedrate?}` | RED | 5-layer guard (see §11.2). |
| POST | `/steppers/off` | — | RED | Sends M84; resets dead-reckon position to UNKNOWN. |
| POST | `/gcode/raw` | `{line: str}` | BLACK | Gated by `BRIDGE_ENABLE_RAW_GCODE` env var; disabled by default (403). Every line logged at WARNING. 4 KB cap inherited. |

### 11.2 Extrude guard chain

All five guards run server-side; no human confirmation step. Any guard failure
produces a typed error envelope (no bare `detail` string).

1. **State guard** — `gcode_state ∈ {IDLE, PAUSE}`; else `409 extrude_state_not_allowed`.
2. **Homed gate** — `home_flag` present in state; else `409 jog_not_homed`.
3. **Cold-extrude guard** — `nozzle_temper ≥ 170 °C`; unavailable data = refuse (`422 nozzle_too_cold`). Conservative: unknown temp = cold.
4. **Bounds** — `|distance_mm| ≤ 100`; else `422` from builder.
5. **Feedrate whitelist** — `feedrate ∈ {120, 300, 600}` mm/min; else `422` from builder.

### 11.3 Raw G-code console — BLACK endpoint

`POST /gcode/raw` is the **no-guardrails** path. Every command submitted through this endpoint:
- Is logged at WARNING level (both structlog + stdlib logger, for ops pipelines that don't parse JSON).
- Has a 4096-byte hard ceiling (MQTT RX buffer limit, same as `/gcode`).
- Has **no content validation** — EEPROM operations (M500/M501/M502/M503), homing, temperature overrides, and arbitrary motion are all forwarded.

**Gate:** the endpoint returns `403 raw_gcode_disabled` unless the environment variable `BRIDGE_ENABLE_RAW_GCODE` is non-empty. Default: off. Enable deliberately after reading this section.

**When to use:** firmware debugging, factory resets, one-off calibration sequences not covered by typed endpoints, testing new G-code command behaviour before writing a typed builder.

---

## 12. Camera

### 12.1 `GET /api/v1/printers/{printer_id}/camera/snapshot.jpg`

**Tier:** v0. Auth via Bearer OR `?token=`.

Returns `image/jpeg`. **Bridge MUST internally discard the stale buffered first frame** — the caller never sees frame 1 (the P1S camera ships a stale buffered frame on connect). **(v0: APK-side 2.5s mount delay; v0.1: bridge-side discard.)**

If no frame available within 10s:

```http
503 Service Unavailable
{
  "error": "camera_no_frame",
  "message": "Camera isn't sending frames yet.",
  "remediation_hint": "The P1S camera takes ~3 seconds to wake up. Try again."
}
```

### 12.2 `GET /api/v1/printers/{printer_id}/camera/stream.mjpeg`

**Tier:** v0.1. `multipart/x-mixed-replace; boundary=frame`. Same auth, same discard-first-frame rule.

### 12.3 APK rendering rules

- Label the snapshot view: `"Live · updates ~1/sec"`. Do NOT show buffering spinners or video playback controls.
- From any failure screen, deep-link to camera with caption: `"Look at the plate — is anything being printed?"` (the air-print catch — a print can fail by extruding into thin air, and the camera is how the user confirms it).
- Poll cadence v0: 1 fps. APK fires GET /snapshot.jpg on a 1s timer.

---

## 13. Named WS events catalog + Alert mapping

### 13.1 Alert mapping (APK-side derivation)

The design's `AlertBanner` has 3 visual kinds (`door`, `runout`, `thermal`) — `app.jsx` `ALERTS` enum. **These are NOT new bridge events.** The APK derives them from existing WS events. Mapping table:

| WS event | `data` shape | AlertBanner `kind` | severity | title template | detail template | action label |
|---|---|---|---|---|---|---|
| `filament_runout` | `{code, slot}` | `runout` | `warn` | `"Filament runout — Slot {slot}"` | `"{material name} ran out at layer {layer}. Swap spool and resume, or reassign."` | `"Swap & resume"` |
| `feed_warning` | `{since_ms, advice}` | `runout`-styled (or `door` if user prefers a distinct kind) | `warn` | `"Print may not be feeding filament"` | `"The printer is heating and moving, but hasn't started laying plastic for {since_ms/1000}s. Likely a slot/filament mismatch."` | `"View camera"` + `"Stop"` |
| `error` with `print_error.category="thermal"` | `{print_error: {code, text, category, severity}}` | `thermal` | `critical` | `"Thermal anomaly — {component}"` | `"{print_error.text}"` | `"Acknowledge"` |
| `error` with `print_error.category="door"` (P1S has door sensor on some configs) | same | `door` | `warn` | `"Chamber door open"` | `"Print paused automatically. Close the door and resume."` | `"Resume"` |
| `error` with other categories | same | derive from `print_error.category`; fallback `"error"` | `error.severity` (default `warn`) | `print_error.text` | (none — error remains sticky until ack) | `"Acknowledge"` |

**APK side, single function:**

```ts
function eventToAlert(event: WsEvent, state: PrinterState): AlertBanner | null { ... }
```

Bridge does NOT need to add an alert event type. APK consumes the existing event stream and renders the banner. This decouples server-side wire schema from UI presentation — the same event can drive a banner today, a toast tomorrow, a notification card next week.

### 13.2 Named WS events catalog

All wrapped in `{type: "event", event: "<name>", data: {...}}`.

| `event` name | Fires when | `data` shape | APK action |
|---|---|---|---|
| `print_started` | gcode_state IDLE/PREPARE/FINISH → RUNNING | `{subtask_name, started_at}` | Don't claim "Printing" until layer_num>0; show "Preparing" |
| `print_completed` | gcode_state → FINISH at layer_num==total | `{subtask_name, layer_num, total_layer_num}` | Headline → "Done", offer Print Again |
| `print_failed` | gcode_state → FAILED | `{print_error: {code, text, ...}, layer_num}` | Sticky failure screen (§4 of UX spec). Latch until ack. |
| `error` | print_error transitions to non-zero | `{print_error: {code, text, category, severity}}` | If category=feed → render feed-failure copy; else generic recover |
| `filament_runout` | print_error matches runout codes | `{code, slot: physical_slot}` | Offer "swap filament" / "stop" |
| `connection_lost` | MQTT link dropped | `{at}` | Dim cached state, "Reconnecting · last update Ns ago" |
| `connection_restored` | MQTT reconnected after a loss | `{at, missed_ms}` | Brighten state; if `missed_ms > 30000`, show toast "Reconnected — state refreshed" |
| `feed_warning` | RUNNING + temps at target + ams.engaged_slot==null + layer_num unchanged for ≥90s | `{since_ms, advice: "look at the plate"}` | **The headline v0 UX feature.** Non-blocking banner: "Print may not be feeding filament — view camera / stop print / keep waiting." Auto-clears when ANY of: layer_num advances, gcode_state leaves RUNNING, ams.engaged_slot changes off null. (PR B implements.) |
| `job_state_change` | JobState transitions | `{job_id, from, to, trigger}` | Update job tab |

---

## 14. APK build mapping (RN/Expo TypeScript)

This section is informational for the APK implementer. Bridge doesn't enforce.

### 14.1 Stack lock

| Concern | Choice | Rationale |
|---|---|---|
| Navigation | `expo-router` | canonical Expo nav |
| Secrets (API key, base URL, IP) | `expo-secure-store` | Keychain/EncryptedSharedPreferences |
| State cache (last-known telemetry) | `react-native-mmkv` | sync, fast cold-launch |
| State store | Zustand | light single-printer store; deep-merge reducer |
| WS | RN built-in `WebSocket` | no extra dep |
| HTTP | `fetch` | no extra dep |
| TypeScript types | generated from this contract | hand-write to start; codegen v0.1 |

### 14.1.0 AMS slot label rendering — APK MUST use "Slot N"

The contract ships `physical_slot: 1-4` as **integers** — the P1S native convention. The design preview renders slot labels as `"A1"`, `"A2"`, etc. — Klipper-style notation that does NOT match the P1S touchscreen.

**APK MUST render `"Slot 1"`, `"Slot 2"`, `"Slot 3"`, `"Slot 4"`** to match the P1S touchscreen. Do NOT render as `"A1"/"A2"` (Klipper convention) or as bare integers. Never invent labels server-side; always derive in the APK from the `physical_slot: int` contract field.

### 14.1.1 Critical APK-port trap: ProfileScreen "moonraker · klipper" — see ⚠ callout at top of doc

(Detail also called out in the top-of-doc ⚠ block; repeated here for visibility from §14.)


The design's `screens-misc.jsx` ProfileScreen About section renders:

```
Backend  moonraker · klipper v0.12
```

**The design was templated against a Klipper backend.** This is NOT the bambu-bridge. When porting, replace with the actual backend:

- `Backend: bambu-bridge v{X.Y.Z}` (from `GET /api/v1/version` — v0.1 endpoint; before then, hardcode the build version)
- `Printer: P1S firmware {info.module.firmware_version}` (from `state.info` category in snapshot)

The same `screens-misc.jsx` SettingRow patterns can stay; only the data binding changes. Watch for any other Klipper-isms during port (e.g., `M105` console example, "moonraker" anywhere, `klipper.cfg` references) — none of those apply.

### 14.1.3 Critical APK-port note: design preview lacks `preparing`

The design bundle's `app.jsx` reducer has `job.status` enum `idle | printing | paused | finished` only. **No `preparing` state.** When porting to RN, the APK MUST:

- Map `phase: "preparing"` from the bridge contract to a distinct visual state, NOT collapse it to `printing`.
- During `preparing`: show "Preparing — heating & leveling" subtitle, indeterminate indicator (no progress bar), live nozzle/bed temps as the real progress signal. **No percentage.**
- Only transition to design's progress-bar visual when `phase: "printing"` (i.e., `layer_num > 0`).

This is the §6.3 air-print prevention rule, enforced server-side via `phase` + `headline`, but the design's React reducer doesn't model it. The bridge's `phase` field is the source of truth — the APK's local view model derives all UI from `phase`, ignoring any direct `gcode_state` or `mc_percent` reading.

**APK adapter contract:** the Zustand store keeps a single `phase` field (bridge-derived); the dashboard's "is this 'Printing' or 'Preparing'?" check is `state.phase === 'printing'`, not `job.status === 'printing'`.

### 14.2 Cold-launch behavior

- Load cached printer state from MMKV → render dashboard immediately with `session.connected = false` and dimmed values.
- Open WS in parallel → on snapshot, brighten + replace cache.
- This is the "no 3-second blank dashboard on pocket open" rule (UX spec §2.4).

### 14.3 Settings screen contract

Minimum settings the APK exposes:
- **Base URL** (LAN/Tailscale preset + custom)
- **API key** (Bearer; QR scan from `deploy/DEPLOY.md` output)
- **Dev mode** toggle (shows `_raw` fields + protocol nouns in error screens; default off)
- **Notification preferences** — uses `GET/PUT /api/v1/printers/{id}/notifications` (deferred to v0.1)

---

## 15. v0 / v0.1 endpoint summary

| Endpoint | v0 | v0.1 |
|---|---|---|
| `POST /printers` (IP + access code → serial-from-cert + 3-fork errors) | ✅ | |
| `GET /printers` (list summary) | ✅ | |
| `GET /printers/{id}` (translated snapshot) | ✅ | |
| `PATCH /printers/{id}` | ✅ | |
| `DELETE /printers/{id}` | ✅ | |
| `POST /printers/{id}/trust` (TOFU re-trust) | ✅ | |
| `WS /printers/{id}/status` | ✅ | |
| `POST /printers/{id}/jobs` (sync validate + 422 issues) | ✅ | |
| `GET /jobs/{job_id}` (detail + events) | ✅ | |
| `POST /jobs/{job_id}/cancel` | ✅ | |
| `GET /printers/{id}/events` (flat per-printer event feed) | ✅ | |
| `POST /printers/{id}/events/{id}/dismiss` | ✅ | |
| `POST /printers/{id}/events/clear` | ✅ | |
| `GET /jobs` (history) | | ✅ |
| `POST /printers/{id}/print/{pause\|resume\|stop}` | ✅ | |
| `POST /printers/{id}/light` | ✅ | |
| `POST /printers/{id}/temperature` | ✅ | |
| `POST /printers/{id}/fan` | ✅ | |
| `POST /printers/{id}/speed` | ✅ | |
| `POST /printers/{id}/{gcode,home,move,ams/*,filament/unload,command}` | | ✅ |
| `POST/GET/DELETE /printers/{id}/files/*` | | ✅ |
| `GET /printers/{id}/camera/snapshot.jpg` (with bridge-side frame-1 discard) | ✅ (APK-side discard) | ✅ (server-side discard) |
| `GET /printers/{id}/camera/stream.mjpeg` | | ✅ |

---

## 16. Bridge implementation split (PR A / PR B)

**PR A — onboarding contract unblocker (~120 LOC):**
1. Serial-from-cert + rewritten `POST /printers` body shape
2. 3-fork error envelope with discovered-serial in E2 (top-level `discovered`), `developer_mode_off_or_lan_drop` for E3
3. `mqtt.py:133-134` translation table replacing `str(exc)`
4. Lift `slicedoc.validate()` from `JobRun._lifecycle()` to `JobManager.submit()` → synchronous 422-issues, structured `{code, category, message}` per issue
5. Add session-health fields (`last_connect_attempt`, `last_failure_phase`, `last_telemetry_at`) to PrinterService; emit in summary + snapshot
6. **TOFU cert fingerprint storage** (DB column `cert_fingerprint`, MqttClient comparison hook, `POST /printers/{id}/trust` endpoint per §3.6 + §4.5)
7. `POST /printers` 409 includes `existing_printer_id` for APK redirect
8. `DELETE /printers/{id}?cascade_jobs=true|false` with 409 + `active_job_ids` when blocking
9. `deploy/bridge.env.example` storage paths use the systemd `%h` user-home specifier (set in the unit), not a hard-coded home directory
10. README onboarding text: remove the serial-required stanza (serial is derived from the cert)

**PR B — dashboard contract unblocker (~140 LOC):**
1. In-place translation layer in `service/printer.py:snapshot()` + `summary()` — phase, headline, AMS dual-emission (+ `remaining_g`/`remaining_pct` per slot), fan native→percent, print_error int→HMS lookup, `print_params.{speed_mm_s, flow_pct}`
2. JobState enum remap: `started`→`submitted`, +new `preparing` state, `printing` gated on `layer_num > 0`. (`canceled` stays — see §7.4 note.)
3. `feed_warning` named event (90s timer, auto-clears per §13.2)
4. `GET /api/v1/printers/{id}/events` flat event feed endpoint + `POST .../dismiss` (per §8.4–8.5)
5. `api/files.py` + `api/control.py` envelope cleanups (typed-exception text, FTPS auth→401)

---

## 17. Open items deferred to post-v0

**v0.1 features (design has them; bridge needs new code/persistence):**
- `motion: {x, y, z, e}` surfacing (Controls screen jog/home) — bridge surfaces from MQTT position fields.
- `printer.lifetime: {runtime_hours, prints_count, filament_used_kg}` — derive from `info` category + EventRepo aggregation.
- `GET/POST /api/v1/printers/{id}/queue` — bridge-side ordered print queue (reorderable per design), new persistence collection.
- `GET/POST /api/v1/spools` — off-AMS cabinet inventory (user-managed filament), new persistence collection.
- `POST /home {axes: "all"|"xy"|"z"}` — partial homing per design Controls screen.
- `POST /filament/load {physical_slot}` — design "Load Filament" macro.
- `POST /macros/{name}` or specific endpoints: `/calibrate/bed_mesh`, `/steppers/off` — design Controls quick actions.
- `GET /printers/{id}/preflight` — bed cleared / filament loaded / auto-leveling / door status. Drives design StartSheet preflight checks. Derive from latest snapshot fields.
- `GET/PUT /printers/{id}/notifications` — design ProfileScreen has 4 toggles (`notifyComplete`, `notifyRunout`, `notifyError`, `notifyAI`). Maps cleanly to existing `db/jobs.py` `NotificationPrefs`. **`notifyAI` drives `JobManager.spaghetti_detection`** — surface the per-printer setting through this endpoint.
- `GET /api/v1/server` — bridge introspection: version, hostname, Tailscale status, camera port config. Drives ProfileScreen Connection section. **Replaces design's "moonraker · klipper" placeholder** with real backend data (see §14.1.1).
- Maintenance counters (nozzle hours / belt tension / bed mesh age / firmware version) — derive from `info` category + EventRepo aggregation.
- MJPEG `/camera/stream.mjpeg` consumption (snapshot polling is v0).
- Multi-printer tab.

**Bridge-internal cleanups (not contract-visible):**
- Server-side camera frame-1 discard (CameraStream multiplexer change; v0 has APK-side discard).
- HMS lookup table bundle for `print_error` text — vendor a copy of the ha-bambulab `hms_error_text/` table (see §8.5).
- `print_error` channel decode beyond first-mile (the `0x0300400C` hex field-layout).
- Hardware-verify pass to disambiguate `CONNACK 5 ↔ developer_mode_off_or_lan_drop` — bridge-server runs against live P1S when both PRs land.

**Protocol open questions:**
- Multi-plate `.gcode.3mf` semantics.
- `bed_type` enum surfacing.
- `project_file` `url` scheme variants.
- Minimal viable `.gcode.3mf` (do thumbnails / `project_settings.config` matter).

---

## 18. Cross-references

This contract is self-contained. The companion documents below are the only
ones you may also need:

- **`docs/APP-UX.md`** — the companion-app UX spec. Read alongside this
  contract: this document is the API target, that one is the screen target.
- **`docs/P1S-CONTROL-MATRIX.md`** — developer reference for the underlying
  P1S MQTT command surface that the control endpoints (§9, §11) wrap.
- **`src/bambu_bridge/api/errors.py`** — the canonical `ERR_*` enum constants
  and the exact `message` / `remediation_hint` copy the bridge emits. If this
  doc ever disagrees with that module, the code wins.
