# APP-UX.md — Bambu Bridge phone app

UX spec for the React Native / Expo (Android-first APK) phone client that talks
to the self-hosted Bambu Bridge over Tailscale or LAN.

This is the source of truth the engineer codes against. Where it conflicts with
intuition, intuition loses — these calls have already been argued.

---

## 1. Product framing

A real person is in the next room from a P1S that has been printing for six
hours. They pull out their phone while making coffee. In under three seconds
they want to know: **is it still printing fine, or do I need to walk in there?**
Twice a week they also want to start a new print, and once in a blue moon they
need to stop one in a hurry from the couch. That is the entire app. Tailscale
is already set up; the workstation running the bridge is reachable.

Three goals, in priority order:

1. **Glance** — "is the print OK?" answered in one screen, no taps.
2. **Intervene** — stop, pause, light, change a temperature in 1–2 taps.
3. **Submit** — pick a 3mf, pick AMS trays, hit Start.

Everything else is secondary and may live behind a menu.

---

## 2. Information architecture

**Bottom tab bar, three tabs.** No drawer. No nested tabs. Tabs are stateful —
returning to a tab restores its stack.

```
┌──────────────────────────────────────────┐
│  (active screen, full height)            │
│                                          │
├──────────────────────────────────────────┤
│  [ Printer ]  [ Print ]  [ Settings ]    │
└──────────────────────────────────────────┘
```

| Tab        | Stack root           | Why it's a tab                                                                      |
| ---------- | -------------------- | ----------------------------------------------------------------------------------- |
| Printer    | DashboardScreen      | Goal #1 (glance) + #2 (intervene). The most-opened surface — must be one tap home.  |
| Print      | SubmitScreen         | Goal #3. Distinct mental mode (file picking, AMS thinking) — earns its own tab.     |
| Settings   | SettingsScreen       | Base URL + key + printer registry. Rarely visited, but must be findable instantly.  |

**Why not a "files" tab?** Browsing the bridge's file cache is a v0.1 task,
reached from inside the Print tab. A dedicated tab would suggest it matters more
than it does.

**Why not a "jobs history" tab?** Same reason — one print at a time, recent jobs
live as a small list at the bottom of the Dashboard.

### Stacks

```
Printer tab
  DashboardScreen           (auto-selects last-used printer)
    └─ PrinterPickerSheet   (modal, if >1 printer)
    └─ ControlsSheet        (modal, bottom sheet)
    └─ TemperatureSheet     (modal, bottom sheet)
    └─ MoveSheet            (modal — Z/X/Y jog + home)
    └─ AmsSheet             (modal — per-tray detail, change filament)
    └─ CameraFullScreen     (push, landscape OK)
    └─ JobDetailScreen      (push — event log for a job)

Print tab
  SubmitScreen              (file picker + recents)
    └─ AmsMappingScreen     (push — tray-per-filament)
    └─ ConfirmScreen        (push — review + Start)
    └─ FileBrowserScreen    (push — bridge cache/model dir, v0.1)

Settings tab
  SettingsScreen
    └─ PrinterEditScreen    (push — add / edit a printer record)
    └─ AboutScreen          (push — version, log export, v0.1)
```

### Deep-link targets (Android intent filters)

- `bblbridge://printer/{id}` → Dashboard for that printer.
- `bblbridge://print` → Submit tab.
- Notification tap (print finished / failed) → Dashboard.

---

## 3. Onboarding / first-launch

The app has zero account. It needs three things before it's useful:

1. Bridge **base URL** (e.g. `http://bridge.example.invalid:8080`).
2. Bridge **API key** (Bearer).
3. At least one **printer** registered on the bridge.

### Happy path (everything works)

```
Launch
  → Welcome screen (single screen, three fields + Test button)
      Base URL: [____________________]
      API key : [____________________]
                          [Test connection]
  → tap Test → calls GET /api/v1/health, then GET /api/v1/printers
  → green check, [Continue] button enables
  → if /printers returns ≥1 printer:
        → straight to Dashboard, last (or only) printer selected
    if /printers returns 0:
        → push "Register your first printer" (IP, access code, name)
        → POST /api/v1/printers   (the bridge derives the serial; do not send one)
        → on 201, push to Dashboard
```

### Fail paths — what the user sees, what they do next

| Situation                              | What we detect                          | What we show                                                                                          | Next action shown                                       |
| -------------------------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| Tailscale off / DNS fail               | fetch throws `Network request failed`   | "Can't reach the bridge. Is Tailscale on?" + a "Check Tailscale" button that opens the Tailscale app  | Tap to switch to Tailscale, then "Try again"            |
| Wrong base URL (404 root, or refused)  | non-2xx on /health, or connection error | "No bridge at that URL. Check the address and port (default 8080)."                                   | Inline — edit the URL, Test again                       |
| Wrong / missing API key                | 401 on /health                          | "API key rejected." (don't say "invalid" — could be revoked.)                                         | Inline — edit the key, Test again                       |
| Bridge up but no printers registered   | 200 on /printers, empty array           | Push to "Register your first printer" form                                                            | Fill IP/access code/name; POST (serial is derived)      |
| Printer registered but offline (MQTT down) | 409 from later WS connect, or `status` says offline | Dashboard renders with a yellow banner: "Printer offline. Bridge is connected but can't reach the printer." | "Retry" button → POST `/api/v1/printers/{id}/reconnect` (v0.1). For v0, just "Retry" re-opens WS. |
| Bridge reachable, /printers 500        | server error                            | "Bridge error 500. The bridge logs will say why." Do not retry-spam.                                  | "Try again" button only                                 |

The Welcome screen is also what Settings → "Re-test connection" leads to. One
screen, two entry points — no duplicated form.

---

## 4. Per-screen layouts

Each section: purpose, wireframe (portrait), states, API.

### 4.1 WelcomeScreen / SettingsScreen (same component, different chrome)

Purpose: configure bridge URL + key, verify, manage printer list.

```
┌────────────────────────────────────────┐
│  Settings                              │  ← header
├────────────────────────────────────────┤
│  BRIDGE                                │
│   Base URL                             │
│   [http://bridge.example.invalid:8080       ]  │
│   API key                              │
│   [••••••••••••••••••••••••       👁] │
│   [    Test connection    ]            │
│   ✓ Bridge reachable, 1 printer        │  ← status line
│                                        │
│  PRINTERS                              │
│   ┌──────────────────────────────────┐ │
│   │ P1S workshop      online    ›    │ │  ← row, tap to edit
│   └──────────────────────────────────┘ │
│   [ + Add printer ]                    │
│                                        │
│  APP                                   │
│   Haptics on actions       [ on  ]    │
│   Camera FPS               [ 1 fps ▾ ]│
│   Default unload temp °C   [ 220   ]  │
│   Dark mode                [ system▾] │
│                                        │
│  About · v0.1.0                        │
└────────────────────────────────────────┘
```

States:
- **Untested**: status line empty, Test button primary.
- **Testing**: button spinner.
- **OK**: green check + summary.
- **Fail**: red row with the actionable message from §3.

APIs:
- Test → `GET /api/v1/health`, then `GET /api/v1/printers`.
- Add printer → push PrinterEditScreen → `POST /api/v1/printers`.
- Edit row → push PrinterEditScreen prefilled → `PATCH /api/v1/printers/{id}`.
- Delete (in edit screen, destructive button) → `DELETE /api/v1/printers/{id}`.

### 4.2 DashboardScreen

See §5 for the dense version. Wireframe and detail are owned there.

### 4.3 ControlsSheet (bottom modal)

Purpose: every typed control that isn't on the Dashboard surface.

```
┌────────────────────────────────────────┐
│  ━━━━                                  │  ← drag handle
│  Controls                          ✕   │
├────────────────────────────────────────┤
│  PRINT                                 │
│   [ Pause ]  [ Resume ]  [ Stop ▸ ]    │  ← Stop = red, leads to confirm
│                                        │
│  CHAMBER                               │
│   Light             [ ●   off ]        │  ← instant toggle, no confirm
│                                        │
│  TEMPERATURE              ›            │  ← row → TemperatureSheet
│  MOVE / HOME              ›            │  ← row → MoveSheet
│  AMS                      ›            │  ← row → AmsSheet
│  FANS                     ›            │
│  SPEED                    ›            │
│                                        │
│  ADVANCED                              │
│   Send raw G-code         ›            │  ← typing screen, confirm before send
└────────────────────────────────────────┘
```

States: all enabled when WS connected and `gcode_state` not UNKNOWN.
When disconnected, the whole sheet is dimmed and the header reads
"Disconnected — controls disabled". When `gcode_state == IDLE`, Pause/Resume/Stop
are disabled with subtitle "no active print".

APIs: each row hits the corresponding `POST /api/v1/printers/{id}/{...}` endpoint
listed in the spec.

### 4.4 TemperatureSheet

```
┌────────────────────────────────────────┐
│  Temperatures                      ✕   │
├────────────────────────────────────────┤
│  Nozzle    218 °C → target 220 °C     │
│   [ – ] [    220    ] [ + ]   [Set]   │
│   Quick: [ 0 ] [ 200 ] [ 220 ] [ 250 ]│
│                                        │
│  Bed       58 °C  → target 60 °C      │
│   [ – ] [     60    ] [ + ]   [Set]   │
│   Quick: [ 0 ] [ 55 ] [ 60 ] [ 100 ]  │
└────────────────────────────────────────┘
```

[Set] is the commit. Typing into the number field alone does nothing until Set is
tapped. This prevents "I scrolled the picker by accident and now my nozzle is
heating to 280". Quick presets are one-tap with a brief undo toast (4 s,
"Setting nozzle to 220 °C. Undo"). Undo sends a Set to the previous value.

API: `POST /api/v1/printers/{id}/temperature`.

### 4.5 MoveSheet

```
┌────────────────────────────────────────┐
│  Move toolhead                     ✕   │
├────────────────────────────────────────┤
│  [   Home all axes   ]      (confirm) │
│                                        │
│        ▲  Y +                          │
│   ◀ X-     X+ ▶                       │
│        ▼  Y –                          │
│                                        │
│  Step:  [0.1] [1] [ 10 ] [100] mm     │
│                                        │
│  Z       ▲ +                          │
│          ▼ –                          │
│                                        │
│  ⚠ Only available when idle.           │
└────────────────────────────────────────┘
```

States:
- If `gcode_state != IDLE`, all jog/home buttons disabled with footnote
  "Can't move while printing."
- Step defaults to 1 mm. 100 mm step requires a long-press to fire (a real
  guard, not a modal — distance + speed asymmetry is the actual danger).

API: `POST /api/v1/printers/{id}/move` and `/home`.

### 4.6 AmsSheet

```
┌────────────────────────────────────────┐
│  AMS                               ✕   │
├────────────────────────────────────────┤
│  AMS 0       42 % humid    28 °C       │
│  ┌────┬────┬────┬────┐                 │
│  │ T1 │ T2 │ T3 │ T4 │                 │
│  │PLA │PETG│ —  │PLA │                 │
│  │■■■ │■■  │    │■■■■│  ← remaining   │
│  │red │blk │    │wht │                 │
│  └────┴────┴────┴────┘                 │
│  Current tray: T2   Target: T2         │
│                                        │
│  [ Change to other tray › ]            │
│  [ Unload filament       › ]           │
│  [ Pause AMS ] [ Resume ] [ Reset ]    │
└────────────────────────────────────────┘
```

Tap a tray cell → opens detail (color, type, remain %). "Change to other tray"
opens a picker that calls `ams/change`. "Unload" calls `filament/unload` with
default temp from Settings.

### 4.7 SubmitScreen (Print tab root)

See §7 for the flow detail.

### 4.8 PrinterEditScreen

```
┌────────────────────────────────────────┐
│  ‹ New printer                    Save │
├────────────────────────────────────────┤
│  Friendly name                         │
│  [ Workshop P1S                      ] │
│  IP address                            │
│  [ 192.168.1.42                      ] │
│  Access code                           │
│  [ 12345678                       👁 ] │
│                                        │
│  [ Test this printer ]                 │
│                                        │
│  (delete button, only in edit mode)    │
│  [ Remove printer                    ] │
└────────────────────────────────────────┘
```

No Serial field: the bridge derives the printer's serial from its TLS leaf
cert when you register it (sending a `serial` is a 422). The form collects
only IP, access code, and friendly name.

Save = POST or PATCH. Test = the bridge endpoint that validates
access/connectivity — if there isn't one yet, just attempt to open the WS once
and report.

---

## 5. The Dashboard, in detail

This is the screen the user lives in. Everything else is supporting cast.

### 5.1 Wireframe (portrait, printing)

```
┌────────────────────────────────────────┐
│  P1S workshop ▾            ● live      │  ← header: printer picker + WS dot
├────────────────────────────────────────┤
│                                        │
│  Printing                              │  ← 14 pt subdued
│  benchy_v3.3mf                         │  ← 14 pt subdued, 1-line truncate
│                                        │
│      63 %        2 h 14 m left         │  ← 56 pt + 22 pt, same baseline
│  Layer 142 of 224                      │  ← 16 pt
│                                        │
│  ┌──────────────────────────────────┐  │
│  │ ████████████████░░░░░░░░░░░░░░░ │  │  ← 4 pt thick progress bar
│  └──────────────────────────────────┘  │
│                                        │
│  ┌──────────────────────────────────┐  │
│  │                                  │  │
│  │      [ camera frame, 16:9 ]      │  │  ← tap → full screen
│  │                                  │  │
│  └──────────────────────────────────┘  │
│                                        │
│  Nozzle  218 °C → 220 °C   ↑heating    │  ← color cue when heating
│  Bed      58 °C → 60 °C    ↑heating    │
│  Chamber  32 °C                        │
│                                        │
│  Part fan  60 %    Aux 40 %   Cham 0 % │  ← 12 pt, one row
│                                        │
│  AMS: T2 PETG black  (42 %)            │  ← tap → AmsSheet
│                                        │
│  ┌────────┬────────┬────────┬────────┐ │
│  │ Pause  │  Stop  │ Light  │  More  │ │  ← sticky bottom action bar
│  └────────┴────────┴────────┴────────┘ │
└────────────────────────────────────────┘
```

### 5.2 The glanceable answer

Top-of-content hierarchy, biggest first:

1. **Status word** (Printing / Paused / Finished / Failed / Idle) — 14 pt label
   only because the percent below it carries the eye. The status color (§10) is
   the real signal.
2. **Percent + time-left** on one line, same baseline — the percent is the
   anchor; time-left is the answer to "should I wait?". 56 pt / 22 pt.
3. **Layer m/n** — confirms forward progress at a glance.
4. **Progress bar** — redundant with %, but the bar gives a *spatial* sense of
   "almost done" vs "barely started" that numbers don't.

That's the whole top half. If `mc_remaining_time` is null → show "—" in its
place, not "0 m". If `total_layer_num` is 0 or null → show "Layer 142" with no
denominator.

### 5.3 Temperatures

Format: `{actual} °C → {target} °C   {hint}`

- Target equal to actual (±2 °C): no arrow, no hint. Just `220 °C`.
- Target > actual + 2: `↑ heating` (warm color).
- Target < actual - 2: `↓ cooling` (cool color).
- Target = 0 and actual ≤ 35: just `28 °C` (no target shown).
- Target = 0 and actual > 35: `58 °C  ↓ cooling`.
- Chamber: actual only, no target (P1S has no chamber heater control).

Tap any temperature row → opens TemperatureSheet pre-focused on that heater.

### 5.4 Camera

**v0 strategy: poll `GET /api/v1/printers/{id}/camera/snapshot.jpg?t={ms}` at
1 fps.** Cache-busting query param. `Image` component, fade between frames
disabled (any animation will look like jank at 1 fps).

States:
- **Loading first frame**: 16:9 placeholder with a spinner and "Connecting to
  camera…".
- **Loaded**: latest frame, no overlay.
- **Frame stale > 5 s**: dim to 50 %, overlay "Camera reconnecting…" centered.
- **Printer offline**: 16:9 dark placeholder, "Camera offline" text.
- **Tap**: push CameraFullScreen, polls at 2 fps in landscape, tap-to-exit.

Settings → Camera FPS lets the user pick 0.5 / 1 / 2 fps. Default 1.

Upgrade path note (not v0): swap to an `expo-image` + WebView combo that holds
an MJPEG `<img>` tag pointed at `stream.mjpeg`, or use a community RN MJPEG
view. Don't block v0 on this.

### 5.5 Control reachability — defended

Bottom action bar is **sticky**, four slots, never scrolls. The four slots
matter:

| Slot      | Action                                                 | Why 1-tap                                                                  |
| --------- | ------------------------------------------------------ | -------------------------------------------------------------------------- |
| Pause     | `POST /print/pause` (or Resume if `gcode_state==PAUSE`)| Most common safe intervention.                                             |
| Stop      | `POST /print/stop` (with confirm sheet)                | Safety story — must be reachable instantly even though it confirms.        |
| Light     | `POST /light` toggle                                   | Genuinely the second-most-used action ("can I see what's going on?").      |
| More      | Opens ControlsSheet                                    | Everything else lives behind this — temp, move, AMS, fans, raw gcode.      |

When `gcode_state` is IDLE or FINISH, the bar morphs:

```
┌────────┬────────┬────────┬────────┐
│ Light  │  Temp  │  Move  │  More  │
└────────┴────────┴────────┴────────┘
```

Pause/Stop disappear when there's nothing to pause/stop. They don't grey out
and stay visible — that wastes the slot.

### 5.6 Disconnected state

**Top header dot** is the always-on indicator.

- Green `● live` — WS open, last frame < 5 s ago.
- Amber `● reconnecting` — WS closed, retrying. *All live numbers go grey* and
  a thin amber banner appears under the header:

  ```
  ──────────────────────────────────────────
  Reconnecting…  Last update 14 s ago
  ──────────────────────────────────────────
  ```

- Red `● offline` — bridge reachable but printer says offline (or 409 on a
  command). Banner is red: "Printer offline." Numbers fade to 30 % opacity.
  Controls disabled.
- Grey `● no bridge` — fetch to /health fails. Banner: "Can't reach the bridge."
  Numbers wholly hidden (no point showing stale temperatures). Big retry button.

**Critical rule, write it on the wall:** when WS is not "open", never display
temperatures, fan speeds, or progress as if they're current. Grey them out and
show the "last update Xs ago" stamp. The user will trust a "stale" label;
they cannot recover from a wrong number.

---

## 6. Control affordances — confirmation policy

One metaphor: **bottom action sheet** ("Stop print?" / "Cancel" / red "Stop").
No alert dialogs. No long-press confirms (too easy to miss).

| Action                             | Endpoint                       | Confirm?       | Why                                                            |
| ---------------------------------- | ------------------------------ | -------------- | -------------------------------------------------------------- |
| Light on/off                       | `/light`                       | No             | Reversible in one tap. Toast feedback only.                    |
| Part / aux / chamber fan %        | `/fan`                         | No             | Trivially reversible.                                          |
| Speed level 1–4                    | `/speed`                       | No             | Trivially reversible.                                          |
| Pause                              | `/print/pause`                 | No             | Safe, reversible (Resume).                                     |
| Resume                             | `/print/resume`                | No             | Safe.                                                          |
| **Stop print**                     | `/print/stop`                  | **Yes**        | Destroys hours of work. Red destructive sheet.                 |
| Set nozzle / bed temp (typed)      | `/temperature`                 | No (typed Set) | The "Set" button IS the confirm.                               |
| Quick preset temp                  | `/temperature`                 | No + undo      | One-tap with 4 s undo toast.                                   |
| **Home all axes**                  | `/home`                        | **Yes**        | Moves toolhead suddenly. Modal confirm.                        |
| Jog X/Y/Z ≤ 10 mm                 | `/move`                        | No             | Small move, intentional.                                       |
| Jog 100 mm step                    | `/move`                        | **Long-press** | Real guard against a thumb slip into the bed.                  |
| AMS change                         | `/ams/change`                  | No             | Pre-selected target, mid-print AMS swaps are expected use.     |
| AMS pause / resume / reset         | `/ams/control`                 | No (reset: yes)| Reset re-homes the AMS; confirm.                               |
| Unload filament                    | `/filament/unload`             | **Yes**        | Heats nozzle; user needs to know it'll get hot.                |
| **Send raw G-code**                | `/gcode`                       | **Yes**        | Arbitrary. Always confirm with the literal line in the sheet.  |
| Raw `/command` escape hatch        | `/command`                     | **Yes**        | Same reasoning. v0.1 — not in v0.                              |

The "destructive sheet" for Stop print:

```
┌────────────────────────────────────────┐
│  Stop this print?                      │
│  benchy_v3.3mf · 63 % done             │
│                                        │
│  This can't be undone. The print will  │
│  end and the toolhead will park.       │
│                                        │
│  [   Stop print   ]   ← red, full width│
│  [    Cancel      ]                    │
└────────────────────────────────────────┘
```

---

## 7. Submit-a-print flow

Three screens; the user can always back out.

### 7.1 SubmitScreen (tab root)

```
┌────────────────────────────────────────┐
│  Print                                 │
├────────────────────────────────────────┤
│                                        │
│  [   Pick a .3mf from device   ]       │  ← primary, full width
│                                        │
│  RECENT                                │
│   benchy_v3.3mf      4 h ago    ↻      │  ← ↻ = reprint
│   gridfinity_bin.3mf 1 d ago    ↻      │
│                                        │
│  ON THE BRIDGE                         │
│   model/                          ›    │
│   cache/                          ›    │
│                                        │
└────────────────────────────────────────┘
```

Pick → `expo-document-picker` with `type: ['*/*']` (Android SAF). Filter on
`.3mf` extension after the fact (Android MIME for 3mf is unreliable). Validate
file size <300 MB before upload. On success → AmsMappingScreen.

### 7.2 AmsMappingScreen

```
┌────────────────────────────────────────┐
│  ‹ AMS mapping                  Next › │
├────────────────────────────────────────┤
│  benchy_v3.3mf · 4 filaments needed    │
│                                        │
│  Slot 1  PLA red          → [ T1 ▾ ]   │
│  Slot 2  PETG black       → [ T2 ▾ ]   │
│  Slot 3  PLA white        → [ T4 ▾ ]   │
│  Slot 4  PLA grey         → [ ext ▾ ]  │  ← external spool
│                                        │
│  ⓘ Default came from the slice file.   │
│    Bridge will reject if a tray is     │
│    empty or wrong type.                │
└────────────────────────────────────────┘
```

Defaults: parse `slice_info` in the 3mf (the bridge documents this is what it
validates against). If we can't parse, default every slot to T1 and show a
warning banner: "Couldn't read the slice — pick trays manually."

Empty AMS / wrong mapping: don't try to predict. Send to the bridge; the 422
response (`detail: "Tray 3 is empty"`) maps directly to a sheet on the next
screen.

### 7.3 ConfirmScreen

```
┌────────────────────────────────────────┐
│  ‹ Start print?                        │
├────────────────────────────────────────┤
│  benchy_v3.3mf                         │
│  Est. 4 h 12 m · 18 g PLA              │
│                                        │
│  Trays:  T1 PLA red                    │
│          T2 PETG black                 │
│          T4 PLA white                  │
│          ext PLA grey                  │
│                                        │
│  Printer: P1S workshop  (idle)         │
│                                        │
│  [           Start print          ]    │  ← primary
│  [             Cancel             ]    │
└────────────────────────────────────────┘
```

Start → multipart `POST /api/v1/printers/{id}/jobs` (3mf + `ams_mapping`).

States:
- **Uploading**: button → progress bar (3MF can be 50–200 MB, this matters).
- **Success (202 / 201)**: confetti? no. Switch to Printer tab and show
  Dashboard, with a snackbar "Print started: benchy_v3.3mf".
- **409 offline**: stay on screen, banner: "Printer is offline — power on the
  P1S and try again." [Retry] button.
- **422 bad mapping**: stay on screen, banner with the literal `detail` from
  the bridge, [Edit mapping] button → back to AmsMappingScreen.
- **502 FTPS failure**: banner "Upload failed (printer rejected the file)."
  [Retry] button. If it fails twice, add a "Send the bridge log" link
  (v0.1 — for v0 just suggest "check the bridge log").

---

## 8. Error UX

Bridge error envelope (assumed; if different, the engineer adjusts these maps,
they don't redesign the UX):

```json
{ "error": { "code": "printer_offline", "message": "…", "detail": "…" } }
```

Rules:
- **Toast** for transient, no action needed (light command, fan change). 2 s.
- **Inline banner** under the screen header for state-related problems
  (disconnected, offline). Persists until the state changes.
- **Sheet** for actions that failed and need a user decision (retry, edit).
  Modal, dismissible.
- **Never** a system Alert dialog. They look like Android telling you something,
  not the app.

| HTTP / class      | User-facing wording                                              | Surface       | Next action            |
| ----------------- | ---------------------------------------------------------------- | ------------- | ---------------------- |
| 401               | "API key rejected. Update it in Settings."                        | Sheet         | "Open Settings"        |
| 404 unknown printer | "That printer is no longer registered on the bridge."           | Banner        | "Pick another"         |
| 409 offline       | "Printer is offline. Power on the P1S and try again."             | Banner + toast on action attempts | "Retry"     |
| 422 bad params    | Show the `detail` verbatim (it'll say "Tray 3 is empty" etc.)     | Sheet         | "Edit"                 |
| 502 FTPS upload   | "Upload failed (printer rejected the file). Try again."           | Sheet         | "Retry"                |
| 5xx generic       | "Bridge error. The bridge log has the details."                   | Sheet         | "Try again"            |
| Network fail      | "Can't reach the bridge — is Tailscale on?"                       | Banner        | "Open Tailscale"       |
| WS closed unexpectedly | (silent, header dot goes amber) auto-reconnect with backoff   | Header        | none — passive         |

Reconnect backoff: 1 s, 2 s, 5 s, 10 s, 30 s, then hold at 30 s.
Reset to 1 s on any successful HTTP request from the app (e.g. user pulls down
to refresh).

---

## 9. Settings — details

Beyond the wireframe in §4.1:

- **Test connection**: hits `GET /api/v1/health` THEN `GET /api/v1/printers`.
  Both must succeed before showing green. If health passes but printers 401,
  message is "Bridge reachable, but API key rejected." That's two facts and
  matters.
- **Storage**: API key in `expo-secure-store` (Android KeyStore-backed). Base
  URL and other settings in `expo-sqlite` or `AsyncStorage`. Printer list is
  authoritative on the bridge — we don't cache it across sessions; refresh on
  app open.
- **Haptics on actions** (default on): tap = `Haptics.selectionAsync()`,
  destructive confirm = `Haptics.notificationAsync('warning')`.
- **Camera FPS**: 0.5 / 1 / 2. Default 1.
- **Default unload temp**: integer, default 220 °C, range 180–280.
- **Dark mode**: System / Dark / Light. Default System.
- **Notifications** (v0.1): if the OS allows, fire a local notification on the
  WS `print_completed` / `print_failed` event. v0 has no background presence,
  so this only fires when the app is foregrounded — defer.

---

## 10. Visual language

**Library**: hand-rolled on top of React Native primitives + `expo-router` +
`react-native-reanimated` for sheets, `react-native-svg` for the progress
ring (if we want one in v0.1). **Not** `react-native-paper`. Reason: Paper's
Material 3 is heavy, opinionated, and will look like every other Material app.
The dashboard is a custom data view — Paper's chips and surfaces will fight us.
Hand-rolled is ~150 lines of style tokens and we keep total control.

**Bottom sheets**: `@gorhom/bottom-sheet`. Single battle-tested dep.

### Color tokens

| Token              | Light       | Dark        | Used for                                       |
| ------------------ | ----------- | ----------- | ---------------------------------------------- |
| `bg`               | `#FAFAFA`   | `#101113`   | Screen background                              |
| `surface`          | `#FFFFFF`   | `#1B1D20`   | Cards, sheets                                  |
| `text`             | `#111`      | `#F1F1F1`   | Primary text                                   |
| `textDim`          | `#666`      | `#999`      | Labels, captions                               |
| `state.printing`   | `#2E7DFF`   | `#5C9CFF`   | "live" dot, progress bar, "Printing"           |
| `state.paused`     | `#E0A93B`   | `#F0BD55`   | "Paused" banner                                |
| `state.failed`     | `#D6362B`   | `#FF6A60`   | "Failed", red Stop button, errors              |
| `state.idle`       | `#8A8F98`   | `#7A7F88`   | "Idle"                                         |
| `state.finished`   | `#3CAE5D`   | `#5BC97B`   | "Finished"                                     |
| `state.disconnect` | `#B68A1E`   | `#D6A93B`   | Amber reconnecting dot + banner                |
| `heat.hot`         | `#E0552A`   | `#FF7045`   | Heating arrow + glow on hot temp values        |
| `heat.cool`        | `#3C9EE0`   | `#5BB5F0`   | Cooling arrow                                  |

### Typography

Three sizes. iOS-style system font on Android too (Roboto fallback fine).

- **Display** 56 pt / 600 — the percent number.
- **Body** 16 pt / 400 — everything readable.
- **Caption** 12 pt / 500 / `textDim` — labels, fan speeds, timestamps.

Plus one variant: **Subhead** 22 pt / 500 for the time-left.

### Spacing

`4, 8, 12, 16, 24, 32`. Cards: 16 padding. Between sections on a screen: 24.
Sticky bottom bar height: 64.

**Dark mode default**: **System**. Workshop / garage use cases skew dark; living
room use cases skew light. Honor system.

---

## 11. v0 vs v0.1 cut

### v0 (first APK — must ship to be useful)

1. WelcomeScreen + SettingsScreen (base URL, API key, test, 1 printer
   register).
2. DashboardScreen with: status, % + time, layer, progress bar, temps, fan row,
   AMS one-line summary, camera (1 fps snapshot), sticky bottom bar
   (Pause / Stop / Light / More).
3. ControlsSheet with: Pause/Resume/Stop, Light, Temperature, Move/Home,
   AMS (basic view + change), Fans, Speed.
4. TemperatureSheet, MoveSheet, AmsSheet (read + change + unload).
5. SubmitScreen full flow: file pick → AMS mapping → confirm → upload → live
   dashboard.
6. Disconnected / reconnecting / offline indicators per §5.6.
7. Error mapping per §8.
8. Confirmation sheets for Stop, Home, Unload, raw G-code.

### v0.1 (next)

1. Files-on-bridge browser (list/upload/delete from `model/` and `cache/`).
2. Jobs history (last 20) + JobDetailScreen with event log.
3. Raw `/command` escape hatch screen.
4. Camera upgrade: WebView-MJPEG or community library, full 10–15 fps.
5. Local notifications on `print_completed` / `print_failed` while
   foregrounded.
6. About screen with bridge log export.
7. Multi-printer header picker (the data model supports it; v0 just shows
   whichever was registered first if there's >1).
8. Recents list on Submit screen (needs local storage of the last-used 3mfs).

### Not in scope (don't even think about it for v0/v0.1)

- iOS build.
- Background WS reconnection / push notifications via FCM.
- Slicing or 3mf editing.
- Account system, multi-user.
- Filament inventory tracking beyond what the AMS reports.
- Camera recording.
- Time-lapse.

---

## 12. Open questions for the human

1. **Bridge error envelope shape** — I assumed `{ "error": { "code", "message",
   "detail" } }`. Confirm or correct; the §8 mapping reads `detail` directly
   into the UI for 422s.
2. **Printer "reconnect" endpoint** — is there a way to ask the bridge to
   re-attempt MQTT to a configured-but-offline printer, or does the bridge do
   that on its own? Affects whether the offline banner has a Retry button or
   is purely informational.
3. **Slice info parsing in the 3mf** — is the bridge willing to expose a
   "describe this 3mf" endpoint that returns filament slots + estimated time
   from `Metadata/slice_info.config`? If yes, the phone never parses the 3mf
   itself. If no, we ship a minimal parser and accept it'll miss exotic files.
4. **WS `ping` cadence and idle timeout** — confirm 30 s ping is server →
   client and what the server expects back. Affects the reconnect/backoff
   constants in §8.
5. **Camera bandwidth on Tailscale** — at 1 fps × ~40 KB that's ~320 kbps,
   fine. If we're wrong about JPEG size by 5× we may need to drop default FPS
   to 0.5. Want a measurement from the live bridge before locking the default.
