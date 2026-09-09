# P1S Control Surface Matrix â€” Local MQTT

> **Developer reference.** This is an engineering inventory of the raw P1S MQTT
> command surface and what the bridge wraps. It is not a user guide â€” if you
> just want to know what you can do from the app, see the "What you can
> control" section of `docs/GETTING-STARTED.md`.

**Generated:** 2026-06-11  
**Purpose:** Historical engineering inventory for "one bridge to rule them all" â€” every command the P1S accepts over local MQTT, what the bridge already exposes, and what gaps remain.  
**Firmware baseline:** 01.09.01.00 was recorded in the original notes; this source-release review has not independently reproduced that hardware coverage. Command support and risk classifications below remain unverified.  
**Topic:** publish to `device/<SERIAL>/request` (QoS 1 for stop/pause/resume; QoS 0 acceptable elsewhere).

---

## Risk Tier Definitions

| Tier | Meaning |
|------|---------|
| GREEN | Harmless anytime â€” lights, fan speeds, speed level, xcam toggles, timelapse, get_version, pushall |
| YELLOW | Print-affecting but reversible â€” pause/resume, target temps within clamp, AMS tray ops while idle |
| RED | Motion or destructive potential â€” jog/home, extrude/retract, stop print, calibration runs, bed level |
| BLACK | Raw passthrough â€” gcode_line / gcode_file arbitrary input |

---

## 1. Print Lifecycle Control

| Command | MQTT Payload (topic: `print`) | What It Does | P1S Support | Handy Exposes? | Bridge Status | Risk |
|---------|-------------------------------|--------------|-------------|----------------|---------------|------|
| pause | `{"print":{"command":"pause","sequence_id":"<id>","param":""}}` | Pause active print | Confirmed (REPORT Â§7) | Yes | EXPOSED: `POST /printers/{id}/print/pause` | YELLOW |
| resume | `{"print":{"command":"resume","sequence_id":"<id>","param":""}}` | Resume paused print | Confirmed (REPORT Â§7) | Yes | EXPOSED: `POST /printers/{id}/print/resume` | YELLOW |
| stop | `{"print":{"command":"stop","sequence_id":"<id>","param":""}}` | Stop/abort print | Confirmed (REPORT Â§7) | Yes | EXPOSED: `POST /printers/{id}/print/stop` | RED |
| print_speed | `{"print":{"command":"print_speed","sequence_id":"<id>","param":"<1-4>"}}` | Speed preset: 1=silent, 2=standard, 3=sport, 4=ludicrous | Confirmed (research/01, ha-bambulab) | Yes | EXPOSED: `POST /printers/{id}/speed` `{"level":1-4}` | YELLOW |
| print_option (auto_recovery) | `{"print":{"command":"print_option","sequence_id":"<id>","auto_recovery":true\|false}}` | Toggle auto-recovery on jam/clog | Confirmed (OpenBambuAPI) | Partial | ABSENT | YELLOW |
| print_option (air_print_detect) | `{"print":{"command":"print_option","sequence_id":"<id>","air_print_detect":true\|false}}` | Toggle air-print detection | Confirmed (OpenBambuAPI) | Yes | ABSENT | YELLOW |
| print_option (filament_tangle_detect) | `{"print":{"command":"print_option","sequence_id":"<id>","filament_tangle_detect":true\|false}}` | Toggle filament tangle detection | Confirmed (OpenBambuAPI) | Yes | ABSENT | YELLOW |
| print_option (nozzle_blob_detect) | `{"print":{"command":"print_option","sequence_id":"<id>","nozzle_blob_detect":true\|false}}` | Toggle nozzle blob detection | Confirmed (OpenBambuAPI) | Yes | ABSENT | YELLOW |
| print_option (sound_enable) | `{"print":{"command":"print_option","sequence_id":"<id>","sound_enable":true\|false}}` | Toggle printer beep/sound | Likely (OpenBambuAPI notes A1/H2D; P1S unconfirmed) | Unknown | ABSENT | GREEN |
| skip_objects | `{"print":{"command":"skip_objects","sequence_id":"<id>","timestamp":<unix_int>,"obj_list":[<id_int>...]}}` | Cancel specific objects mid-print without stopping job | Confirmed (OpenBambuAPI, ha-bambulab) | Partial (Handy/Studio show object map) | ABSENT | YELLOW |

### Notes on print lifecycle
- QoS 1 is required for `stop`, `pause`, `resume` per OpenBambuAPI specification.
- The bridge's `print_stop` builder omits `"param":""` â€” the OpenBambuAPI spec includes it. No observed breakage on 01.09 but worth aligning (source: research/01-mqtt-protocol.md vs OpenBambuAPI raw).
- `print_option` fields are sent as individual booleans in the same command body â€” multiple toggles can be combined in a single publish.
- `skip_objects` `obj_list` contains internal Bambu object IDs from the slice, not user-facing indices.

---

## 2. File / Print Submission

| Command | MQTT Payload (topic: `print`) | What It Does | P1S Support | Handy Exposes? | Bridge Status | Risk |
|---------|-------------------------------|--------------|-------------|----------------|---------------|------|
| project_file | See payload below | Start print from uploaded `.gcode.3mf` on SD card | Confirmed â€” the ONLY working print-start path on fw 01.09 (REPORT Â§6.2) | Yes | EXPOSED: via `POST /printers/{id}/jobs` (FTPS upload + project_file) | RED |
| gcode_file | `{"print":{"command":"gcode_file","sequence_id":"<id>","param":"<path>"}}` | Start from raw `.gcode` on SD | BLOCKED on fw 01.09 regardless of path form (REPORT Â§6.1) | No | ABSENT (correctly; blocked by firmware) | RED |

**project_file full payload:**
```json
{
  "print": {
    "sequence_id": "<id>",
    "command": "project_file",
    "param": "Metadata/plate_1.gcode",
    "url": "file:///sdcard/<filename>.gcode.3mf",
    "subtask_name": "<name>",
    "project_id": "0",
    "profile_id": "0",
    "task_id": "0",
    "subtask_id": "0",
    "use_ams": true,
    "ams_mapping": [<0-based-tray-id>],
    "timelapse": false,
    "bed_leveling": true,
    "flow_cali": false,
    "vibration_cali": true,
    "layer_inspect": false,
    "bed_type": "textured_plate"
  }
}
```
Sources: REPORT Â§6.2, research/01-mqtt-protocol.md Â§8, ha-bambulab commands.py `PRINT_PROJECT_FILE_TEMPLATE`.

**Notes:** `bed_type` options: `"auto"`, `"textured_plate"`, `"smooth_plate"`, `"high_temp_plate"`, `"cool_plate"` (ha-bambulab const.py). `ams_mapping` is a 5-element right-padded array in some implementations; `-1` = unused slot. All indices are 0-based and MUST agree with gcode `M620 S<n>A` and `slice_info.config` arity (REPORT Â§6.3 â€” three-cause failure).

---

## 3. Fans

| Fan | M106 Index | MQTT Payload | What It Does | P1S Support | Bridge Status | Risk |
|-----|-----------|--------------|--------------|-------------|---------------|------|
| Part cooling fan | P1 (from bridge `_FAN_PARTS`) | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"M106 P1 S<0-255>\n"}}` | Set part fan speed; 0=off, 255=100% | Confirmed (commands.py, ha-bambulab) | EXPOSED: `POST /printers/{id}/fan` `{"part":"part","percent":0-100}` | GREEN |
| Auxiliary / exhaust fan | P2 | Same pattern, `P2 S<0-255>` | Set aux/exhaust fan speed | Confirmed | EXPOSED: `POST /printers/{id}/fan` `{"part":"aux","percent":0-100}` | GREEN |
| Chamber circulation fan | P3 | Same pattern, `P3 S<0-255>` | Set chamber fan speed | Confirmed | EXPOSED: `POST /printers/{id}/fan` `{"part":"chamber","percent":0-100}` | GREEN |
| Heatbreak fan | (no user command) | Not directly controllable; internal only | Reported in push_status as `heatbreak_fan_speed` but no M106 Px mapping exposed | Not controllable | N/A | N/A |

**Fan index note:** The bridge uses `_FAN_PARTS = {"part": 1, "aux": 2, "chamber": 3}` in `commands.py`. The research doc and ha-bambulab both confirm these mappings for P1S. The push_status fields `cooling_fan_speed` = part fan, `big_fan1_speed` = aux fan, `big_fan2_speed` = chamber fan (research/01 Â§7).

**Fan speed scale:** Printer reports on 0â€“15 native scale (string). Commands use M106 0â€“255 scale. The bridge converts percentâ†’255 correctly. Translate push_status back with `pct = int(value) / 15 * 100` (research/01 Â§7, confirmed in models.py field comment).

---

## 4. Lights

| Node | MQTT Payload | What It Does | P1S Support | Bridge Status | Risk |
|------|--------------|--------------|-------------|---------------|------|
| chamber_light on/off | `{"system":{"command":"ledctrl","sequence_id":"<id>","led_node":"chamber_light","led_mode":"on\|off","led_on_time":500,"led_off_time":500,"loop_times":0,"interval_time":0}}` | Chamber LED steady on or off | Confirmed (REPORT, research/01 Â§8) | EXPOSED: `POST /printers/{id}/light` `{"on":bool}` | GREEN |
| chamber_light flashing | Same but `"led_mode":"flashing"`, `"loop_times":<n>`, `"interval_time":<ms>` | Chamber LED blink pattern | Confirmed (research/01, OpenBambuAPI) | ABSENT (only steady on/off exposed) | GREEN |
| work_light on/off | `{"system":{"command":"ledctrl","sequence_id":"<id>","led_node":"work_light","led_mode":"on\|off","led_on_time":0,"led_off_time":0,"loop_times":0,"interval_time":0}}` | Work/task light (if present on P1S config) | Likely (documented in OpenBambuAPI and ha-bambulab; not all P1S hardware configs have this LED) | ABSENT | GREEN |
| chamber_light2 | Same pattern, `"led_node":"chamber_light2"` | Secondary chamber LED (some hardware revisions) | Uncertain for P1S (ha-bambulab has `CHAMBER_LIGHT_2`; may be X1/H2D only) | ABSENT | GREEN |
| heatbed_light | `"led_node":"heatbed_light"`, `"led_mode":"on\|off"`, timing fields 0 | Under-bed lighting (if present) | P1S likely absent; seen in ha-bambulab but may be H2D only | ABSENT | GREEN |

**Source conflict:** ha-bambulab defines `chamber_light2` and `heatbed_light`; OpenBambuAPI documents only `chamber_light` and `work_light`. For P1S specifically, `chamber_light` is the confirmed node; others are speculative pending hardware verification.

---

## 5. Temperatures

| Target | MQTT Payload | What It Does | P1S Support | Bridge Status | Risk |
|--------|--------------|--------------|-------------|---------------|------|
| Nozzle temp (no-wait) | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"M104 S<0-300>\n"}}` | Set nozzle target, return immediately (M104) | Confirmed | EXPOSED: `POST /printers/{id}/temperature` `{"nozzle":<int>}` | YELLOW |
| Bed temp (no-wait) | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"M140 S<0-120>\n"}}` | Set bed target, return immediately (M140) | Confirmed | EXPOSED: `POST /printers/{id}/temperature` `{"bed":<int>}` | YELLOW |
| Nozzle temp (wait) | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"M109 S<temp>\n"}}` | Set nozzle and block until reached (M109) | Confirmed; CAUTION: blocks printer motion planner until temp reached | ABSENT (bridge always uses M104) | YELLOW |
| Bed temp (wait) | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"M190 S<temp>\n"}}` | Set bed and block until reached (M190) | Confirmed; same caution as M109 | ABSENT | YELLOW |
| Chamber temp target | No direct gcode; chamber is passive on P1S (no heater) | Read-only; no M141 or equivalent on P1S | P1S has no chamber heater | N/A | N/A |

**Validation in bridge:** `NOZZLE_MAX_C = 300`, `BED_MAX_C = 120` â€” both clamped in `commands.py`. The API-CONTRACT.md Â§9 lists nozzle â‰¤280 as the G4 envelope but `commands.py` uses 300. This is a discrepancy â€” the contract says 280 but the code allows 300. The physical max nozzle temp for P1S hardened steel is 300 Â°C; for stainless 280 Â°C. The bridge should validate against nozzle type when known, or default to 280 Â°C as the safe bound.

**bambulabs_api firmware branching note:** firmware â‰¤01.06 uses M104/M140; firmware >01.06 uses M109/M190 with guard conditions (bed < 40Â°C, nozzle < 60Â°C skips the wait form). The bridge always uses non-wait forms (M104/M140) which is safe but means the gcode_line completes immediately without printer-side confirmation of temp reached. For the app this is fine because the app observes temperature via telemetry.

---

## 6. Motion (Jog / Home / Extrude)

| Command | MQTT Payload | What It Does | P1S Support | Bridge Status | Risk |
|---------|--------------|--------------|-------------|---------------|------|
| Home all axes | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"G28\n"}}` | Home X, Y, Z (moves toolhead + bed) | Confirmed | EXPOSED: `POST /printers/{id}/home` â€” seeds dead-reckon position on success | RED |
| Home specific axes | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"G28 X Y\n"}}` etc. | Partial homing | Likely (standard Marlin) | ABSENT (API-CONTRACT Â§16 deferred: `POST /home {axes:"all"\|"xy"\|"z"}`) | RED |
| Relative jog X/Y/Z | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"G91\nG1 <axis><dist> F<feed>\nG90\n"}}` | Relative move on one axis | Confirmed | EXPOSED: `POST /printers/{id}/move` with 3-layer guard (step whitelist Â±{1,10,50}mm, homed gate, envelope clamp) | RED |
| Extrude (manual) | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"M83\nG1 E<mm> F<feed>\nM82\n"}}` | Extrude filament (E-axis move) | Likely (standard Marlin; gcode_line passthrough) | ABSENT | RED |
| Retract (manual) | Same, negative E value | Retract filament | Likely | ABSENT | RED |
| Disable steppers | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"M84\n"}}` | Disable all stepper motors | Likely (API-CONTRACT Â§16 deferred: `/steppers/off`) | ABSENT | RED |
| Set absolute positioning | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"G90\n"}}` | Switch to absolute mode | Likely | Via gcode passthrough only | BLACK |
| Set relative positioning | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"G91\n"}}` | Switch to relative mode | Likely | Via gcode passthrough only | BLACK |

**Bridge jog guards (control.py Â§_check_jog):**
1. Step whitelist: `abs(distance_mm) âˆˆ {1.0, 10.0, 50.0}` â€” else 422.
2. Homed gate: `home_flag` bitmask bit must be set for axis (X=0x01, Y=0x02, Z=0x04) â€” else 409.
3. Dead-reckon envelope: tracked position must be KNOWN; proposed position must be within Z âˆˆ [0.0, 256.0], X/Y âˆˆ [0.0, 256.0] â€” else 409 fail-closed.
4. Dead-reckon is advanced only after successful publish and reset on disconnect/session-loss.

**ha-bambulab validation pattern for comparison:** ha-bambulab does not expose jog over the HA integration; it leaves motion entirely to the printer's touchscreen. The bridge's jog endpoint is more capable than ha-bambulab and requires the above guards. The hardware safety doctrine (MEMORY: crashed P1S bed 2026-06-11) confirms these guards are not theoretical.

---

## 7. AMS (Automatic Material System)

| Command | MQTT Payload | What It Does | P1S Support | Bridge Status | Risk |
|---------|--------------|--------------|-------------|---------------|------|
| ams_control (pause) | `{"print":{"command":"ams_control","sequence_id":"<id>","param":"pause"}}` | Pause AMS feed (e.g. during runout handling) | Confirmed (research/01 Â§8, OpenBambuAPI) | EXPOSED: `POST /printers/{id}/ams/control` `{"action":"pause"}` | YELLOW |
| ams_control (resume) | Same with `"param":"resume"` | Resume AMS after pause | Confirmed | EXPOSED: `{"action":"resume"}` | YELLOW |
| ams_control (reset) | Same with `"param":"reset"` | Reset AMS state machine (e.g. after jam/runout) | Confirmed | EXPOSED: `{"action":"reset"}` | YELLOW |
| ams_change_filament | `{"print":{"command":"ams_change_filament","sequence_id":"<id>","target":<0-based-tray>,"curr_temp":<int>,"tar_temp":<int>}}` | Switch active AMS tray (mid-print or pre-print) | Confirmed (research/01, ha-bambulab SWITCH_AMS_TEMPLATE) | EXPOSED: `POST /printers/{id}/ams/change` `{"target_tray":<0-based>,"cur_temp":<int>,"tar_temp":<int>}` | YELLOW |
| unload_filament | `{"print":{"command":"unload_filament","sequence_id":"<id>"}}` | Unload current filament from hotend | Confirmed (commands.py, OpenBambuAPI) | EXPOSED: `POST /printers/{id}/filament/unload` | YELLOW |
| load_filament (macro) | No direct MQTT equivalent; done via gcode_line M701/M702 or AMS change | Load filament into hotend | Via gcode_line; deferred in API-CONTRACT Â§16 as `POST /filament/load {physical_slot}` | ABSENT as typed endpoint | YELLOW |
| ams_get_rfid | `{"print":{"command":"ams_get_rfid","sequence_id":"<id>","ams_id":<int>,"slot_id":<int>}}` | Trigger RFID re-read for specific slot | Confirmed (OpenBambuAPI, ha-bambulab AMS_READ_RFID_TEMPLATE) | ABSENT | GREEN |
| ams_filament_setting | `{"print":{"command":"ams_filament_setting","sequence_id":"<id>","ams_id":<int>,"tray_id":<int>,"tray_info_idx":"<sku>","tray_color":"<RRGGBBAA>","nozzle_temp_min":<int>,"nozzle_temp_max":<int>,"tray_type":"<PLA\|ABS\|...>"}}` | Write filament profile to AMS slot (override RFID or set untagged spool) | Confirmed (OpenBambuAPI, ha-bambulab AMS_FILAMENT_SETTING_TEMPLATE) | ABSENT | YELLOW |
| ams_user_setting | `{"print":{"command":"ams_user_setting","sequence_id":"<id>","ams_id":<int>,"startup_read_option":true\|false,"tray_read_option":true\|false}}` | Configure AMS RFID read behavior per unit | Confirmed (OpenBambuAPI) | ABSENT | YELLOW |
| ams_filament_drying | `{"print":{"command":"ams_filament_drying","sequence_id":"<id>","ams_id":<int>,"temp":<int>,"cooling_temp":<int>,"duration":<int>,"humidity":<int>,"mode":<int>,"rotate_tray":false}}` | Start filament drying cycle in AMS (P1S AMS has drying capability) | Confirmed (ha-bambulab AMS_FILAMENT_DRYING_TEMPLATE; requires AMS firmware support) | ABSENT | YELLOW |

**Source conflict on ams_change_filament:** OpenBambuAPI shows `"target":<tray_id>` only; ha-bambulab SWITCH_AMS_TEMPLATE adds `"ams_id":255` and `"slot_id":0` fields alongside `"target"`. The bridge uses the simpler `target`-only form. The ha-bambulab form may be needed for multi-AMS-unit setups (more than one AMS attached). For single-AMS P1S, both forms appear to work; the bridge's form is simpler and confirmed against 01.09.

**Physical slot vs. protocol index:** AMS slots are 0-based in the protocol (`tray_id` 0â€“3); physical labels on the AMS are 1â€“4. The bridge API-CONTRACT Â§9 documents `target_tray` in the request but the contract note says "PHYSICAL slot, not 0-based" for the `/ams/change` endpoint â€” however `commands.py::ams_change_filament()` accepts `target_tray` as 0-based (`if target_tray < 0`) and passes it straight to the `"target"` field. There is an **inconsistency**: `control.py::AmsChangeBody` documents "0-based AMS protocol index" but API-CONTRACT Â§9 table says "mid-print filament change. PHYSICAL slot, not 0-based." The code sends 0-based to the printer (correct); the contract language is contradictory and needs resolution before the APK is built.

---

## 8. Calibration

| Command | MQTT Payload | What It Does | P1S Support | Bridge Status | Risk |
|---------|--------------|--------------|-------------|---------------|------|
| Calibration (bitmask) | `{"print":{"command":"calibration","sequence_id":"<id>","option":<bitmask>,"bed_type":<int>}}` | Run calibration routines per bitmask: bit0=vibration, bit1=bed level, bit2=first layer (flow cali) | Confirmed for P1S (research/01 Â§8); bit0=LIDAR in OpenBambuAPI but P1S has no LIDAR â€” bit0 is vibration compensation on P1S | ABSENT | RED |
| Vibration compensation only | `option: 1` (bit 0) | Resonance/vibration calibration | Confirmed P1S (no LIDAR) | ABSENT | RED |
| Bed level only | `option: 2` (bit 1) | Auto bed leveling (ABL) | Confirmed P1S | ABSENT | RED |
| Flow calibration | `option: 4` (bit 2) | First layer flow/PA calibration | Confirmed P1S | ABSENT | RED |
| All calibrations | `option: 7` (bits 0+1+2) | Full calibration sequence | Confirmed | ABSENT | RED |
| bed_type parameter | `"bed_type": 1` in calibration command | Selects bed surface type for leveling profile | Likely (research/01 documents it; ha-bambulab uses it) | ABSENT (not in any bridge command builder) | RED |
| Pressure Advance (K-value) via gcode | `M900 K<value>` via gcode_line | Set pressure advance K value directly | Confirmed (standard Marlin-ish) | Via gcode passthrough only | BLACK |

**Source conflict on calibration option bitmask:** OpenBambuAPI documents bit 0 = LIDAR, bit 1 = bed leveling, bit 2 = vibration, bit 3 = motor. Research/01 Â§8 documents `option: 7` as `1=vibration, 2=bed level, 4=first layer`. The P1S has no LIDAR; ha-bambulab uses feature detection (`supports_feature(Features.LIDAR_CALIBRATION)`) to guard the LIDAR bit. For P1S, safe options are `option: 2` (bed level), `option: 1` (vibration), `option: 3` (both), `option: 4` (flow). Use `option: 7` with caution â€” the "first layer" bit runs a flow calibration that extrudes purge material. The `bed_type` field in the calibration command selects the leveling mesh profile; leaving it absent uses the printer's current bed type.

**ha-bambulab guard pattern:** ha-bambulab checks `supports_feature(Features.LIDAR_CALIBRATION)` before sending bit 0, and `supports_feature(Features.ABL_CALIBRATION)` before bit 1. These guard the X1-only LIDAR path from hitting P1S firmware. The bridge should apply the same pattern â€” either hardcode P1S-safe bits or add feature detection.

---

## 9. Camera / XCam (AI Vision)

| Command | MQTT Payload | What It Does | P1S Support | Bridge Status | Risk |
|---------|--------------|--------------|-------------|---------------|------|
| ipcam_record_set | `{"camera":{"command":"ipcam_record_set","sequence_id":"<id>","control":"enable\|disable"}}` | Enable/disable print video recording to SD card | Confirmed P1S (OpenBambuAPI, push_status `ipcam.ipcam_record`) | ABSENT | GREEN |
| ipcam_timelapse | `{"camera":{"command":"ipcam_timelapse","sequence_id":"<id>","control":"enable\|disable"}}` | Enable/disable timelapse generation | Confirmed P1S (OpenBambuAPI, push_status `ipcam.timelapse`) | ABSENT | GREEN |
| xcam first_layer_inspector | `{"xcam":{"command":"xcam_control_set","sequence_id":"<id>","module_name":"first_layer_inspector","control":true\|false,"print_halt":false\|true}}` | AI first-layer inspection on/off; `print_halt` pauses on detection | Confirmed P1S (OpenBambuAPI, ha-bambulab; P1S has xcam hardware) | ABSENT | YELLOW |
| xcam spaghetti_detector | `{"xcam":{"command":"xcam_control_set","sequence_id":"<id>","module_name":"spaghetti_detector","control":true\|false,"print_halt":false\|true}}` | Spaghetti detection (print_halt=true pauses on detection) | Confirmed P1S (ha-bambulab; see API-CONTRACT Â§16 `notifyAI` / `spaghetti_detection`) | ABSENT | YELLOW |
| xcam airprint_detector | Same pattern, `"module_name":"airprint_detector"` | Air-print detection | Confirmed (OpenBambuAPI, ha-bambulab) | ABSENT | YELLOW |
| xcam printing_monitor | Same pattern, `"module_name":"printing_monitor"` | General printing quality monitor | Likely P1S (ha-bambulab) | ABSENT | YELLOW |
| xcam pileup_detector | Same pattern, `"module_name":"pileup_detector"` | Plastic pileup detection | Likely P1S (ha-bambulab) | ABSENT | YELLOW |
| xcam clump_detector | Same pattern, `"module_name":"clump_detector"` | Clump/blob detection | Likely P1S (ha-bambulab) | ABSENT | YELLOW |
| xcam buildplate_marker_detector | Same pattern, `"module_name":"buildplate_marker_detector"` | Detects build plate marker pattern for calibration | Likely X1-only (LIDAR-dependent on X1); P1S uncertain | ABSENT | YELLOW |

**P1S xcam note:** The P1S camera is a fixed 1080p module (not LIDAR). The `xcam_control_set` commands that depend on LIDAR (buildplate_marker_detector) are X1-only. All `spaghetti_detector`, `first_layer_inspector`, `airprint_detector` use the camera image and are confirmed for P1S. The `print_halt` boolean in xcam commands is critical â€” setting it `true` enables auto-pause on detection, which is what end users typically want for spaghetti protection.

**Recommended validation for xcam RED/YELLOW:** `module_name` should be whitelist-validated server-side (closed set of known values) before forwarding. An unknown module_name will either be ignored or could trigger unexpected printer behavior on future firmware.

---

## 10. System / Info

| Command | MQTT Payload | What It Does | P1S Support | Bridge Status | Risk |
|---------|--------------|--------------|-------------|---------------|------|
| get_version | `{"info":{"command":"get_version","sequence_id":"<id>"}}` | Request firmware/module version map (echoed in `info.module[]` response) | Confirmed (REPORT Â§3, research/01) | PARTIAL: builder exists in `commands.py::get_version()` but no HTTP endpoint exposes it to clients | GREEN |
| pushall | `{"pushing":{"command":"pushall","version":1,"push_target":1}}` | Request full state snapshot | Confirmed (REPORT Â§3) | INTERNAL: called automatically on connect/reconnect in `mqtt.py`; not an HTTP endpoint | GREEN |
| set_accessories (nozzle) | `{"system":{"command":"set_accessories","sequence_id":"<id>","accessory_type":"nozzle","nozzle_diameter":<float>,"nozzle_type":"stainless_steel\|hardened_steel"}}` | Notify printer of installed nozzle diameter and type | Confirmed (OpenBambuAPI) | ABSENT | YELLOW |
| get_access_code | `{"system":{"command":"get_access_code","sequence_id":"<id>"}}` | Retrieve LAN access code from printer | Confirmed (OpenBambuAPI) | ABSENT (security-sensitive: exposes auth credential) | YELLOW |
| upgrade.get_history | `{"upgrade":{"command":"get_history","sequence_id":"<id>"}}` | Retrieve firmware update history and available versions | Confirmed (OpenBambuAPI) | ABSENT | GREEN |
| upgrade.start | `{"upgrade":{"command":"start","sequence_id":"<id>","src_id":1,"url":"<url>","module":"ota\|ams","version":""}}` | Initiate firmware/AMS OTA update | Confirmed (OpenBambuAPI) | ABSENT (HIGH RISK if misused) | RED |
| upgrade.upgrade_confirm | `{"upgrade":{"command":"upgrade_confirm","sequence_id":"<id>","src_id":1}}` | Confirm firmware upgrade stage | Confirmed (OpenBambuAPI) | ABSENT | RED |

**Note on get_version endpoint gap:** `commands.py` has `def get_version()` but there is no HTTP endpoint in `control.py` or any router that calls it. Clients cannot currently request a version refresh via the bridge API without using the raw `/command` escape hatch.

**Note on get_access_code:** This command should NOT be exposed in the bridge API without explicit access control since it would allow a client with the API key to extract the printer's LAN access code. If exposed, it must be master-key-only and documented as a privileged operation.

---

## 11. Raw / Black Passthrough

| Command | MQTT Payload | What It Does | Bridge Status | Risk |
|---------|--------------|--------------|---------------|------|
| gcode_line | `{"print":{"command":"gcode_line","sequence_id":"<id>","param":"<G-code>\n"}}` | Send arbitrary G-code or multi-line sequence | EXPOSED: `POST /printers/{id}/gcode` (v0.1) AND via `POST /printers/{id}/command` escape hatch (also v0.1) | BLACK |
| Raw command envelope | Any `{"<category>":{"command":"<cmd>","sequence_id":"<id>",...}}` | Forward arbitrary MQTT envelope | EXPOSED: `POST /printers/{id}/command` with `category âˆˆ {print,system,info}` (v0.1) | BLACK |

**Bridge restriction on raw command category:** `control.py::RawCommand` limits `category` to `Literal["print","system","info"]` â€” this is an important guard that prevents forwarding to `camera`, `xcam`, `upgrade`, or `pushing` categories. Typed endpoints are the only path to those categories currently.

**Recommended validation for BLACK gcode_line:**
- Reject or require confirmation for destructive G-codes: `M600` (filament change), `M702` (unload), `G34` (Z leveling), `M503` (dump EEPROM), `M500/M501/M502` (EEPROM save/load/reset).
- Rate limiting: unbounded gcode_line calls can cause printer motion queue saturation.
- Length limit: the bridge validates `len(line.strip()) > 0` but no maximum length; a very large gcode block could exhaust the printer's ~4 KB MQTT RX buffer (research/02 Â§4).
- The `user_id` optional field in gcode_line (OpenBambuAPI) is not populated by the bridge builder; omitting it is fine.

---

## 12. ha-bambulab Guard Patterns (Validation Reference)

ha-bambulab (greghesp/ha-bambulab, pybambu/commands.py) is battle-tested against P1 firmware and uses these guard patterns:

| Area | ha-bambulab Pattern | Bridge Equivalent | Gap |
|------|---------------------|-------------------|-----|
| Motion (jog/home) | Not exposed in HA integration at all â€” touchscreen-only | 3-layer guard in control.py | Bridge is MORE permissive than ha-bambulab; guards are necessary and are implemented |
| Temperature set | Firmware version branch: fw â‰¤01.06 uses M104/M140; fw >01.06 uses M109/M190 with guard (skip wait form if temp already near target) | Always uses M104/M140 (no-wait) | Bridge safe but less precise; M109/M190 could cause the printer's motion planner to block |
| Calibration | `supports_feature()` checks before sending LIDAR bit (X1-only) | No feature detection | Bridge should whitelist P1S-safe calibration bits (1, 2, 4) and reject bit 8+ |
| AMS filament change | Sends `ams_id: 255` and `slot_id: 0` alongside `target` | Only `target` field | Multi-AMS edge case; safe for single-AMS P1S |
| Print speed | Validates param is string "1"â€“"4" | Validates level 1â€“4 integer, converts to string | Functionally equivalent |
| XCam module_name | Closed enum in const.py `XCAM_MODULES` | No validation (not yet implemented) | Add whitelist before exposing xcam endpoint |
| Upgrade commands | Not exposed in HA integration | Not exposed in bridge | Correct; keep upgrade off by default |

---

## 13. Coverage Gap Summary

### GREEN â€” Absent (low urgency, safe to add)
1. **Chamber light flashing mode** â€” `system.ledctrl` with `led_mode:"flashing"` â€” useful for notifications
2. **work_light** node â€” `system.ledctrl` with `led_node:"work_light"` â€” P1S hardware support unclear, low cost to try
3. **ipcam_record_set** â€” `camera.ipcam_record_set` enable/disable print recording â€” natural app setting
4. **ipcam_timelapse** â€” `camera.ipcam_timelapse` enable/disable â€” natural app setting
5. **get_version HTTP endpoint** â€” builder exists, no route exposes it
6. **upgrade.get_history** â€” read-only firmware history query

### YELLOW â€” Absent (implement with input validation)
7. **print_option flags** â€” `auto_recovery`, `air_print_detect`, `filament_tangle_detect`, `nozzle_blob_detect` â€” these are key safety detection toggles; `air_print_detect` in particular is directly relevant to the Â§6.3 air-print problem
8. **skip_objects** â€” cancel specific objects mid-print without stopping; useful for multi-object plates
9. **ams_get_rfid** â€” trigger RFID rescan on specific slot; useful after spool swap
10. **ams_filament_setting** â€” write filament profile to untagged spool; needed for "set filament type" feature
11. **ams_filament_drying** â€” start drying cycle; new AMS firmware supports this
12. **ams_user_setting** â€” configure RFID read triggers per AMS unit
13. **xcam spaghetti_detector** with print_halt â€” the most user-requested AI safety feature; API-CONTRACT Â§16 already identifies `notifyAI` â†’ `spaghetti_detection` as a needed setting
14. **xcam airprint_detector / printing_monitor / pileup_detector / clump_detector** â€” full xcam suite
15. **xcam first_layer_inspector** with print_halt â€” AI first layer QA
16. **set_accessories (nozzle)** â€” needed when user changes nozzle diameter/type
17. **Calibration typed endpoint** â€” `POST /printers/{id}/calibrate` with `{option: 1|2|4|7, bed_type: int}` â€” already planned per API-CONTRACT Â§16 as `/calibrate/bed_mesh`
18. **load_filament typed endpoint** â€” `POST /printers/{id}/filament/load {physical_slot}` â€” API-CONTRACT Â§16 deferred item
19. **Home partial axes** â€” `POST /printers/{id}/home {axes:"all"|"xy"|"z"}` â€” API-CONTRACT Â§16 deferred item

### RED â€” Absent (implement with hard guards)
20. **Extrude/retract typed endpoint** â€” E-axis gcode_line; need bounds (e.g. max 100mm per call), homed gate, and speed whitelist
21. **Stepper disable (M84)** â€” `POST /printers/{id}/steppers/off` â€” API-CONTRACT Â§16 deferred item; resets all positional tracking

### BLACK â€” Partial
22. **Raw command category restriction** â€” bridge correctly limits to `{print,system,info}` but `camera` and `xcam` categories cannot be reached without typed endpoints. Adding those typed endpoints is preferable to expanding the raw passthrough categories.
23. **gcode_line length limit** â€” no max length enforced; add a practical ceiling (e.g. 4096 bytes) to avoid MQTT RX buffer saturation

### Inconsistencies to Resolve
24. **Nozzle temp max discrepancy** â€” `commands.py` uses 300Â°C; API-CONTRACT Â§9 says 280Â°C. Resolve: 280 for stainless, 300 for hardened steel (align to nozzle type when known or default 280).
25. **ams_change_filament slot numbering** â€” `AmsChangeBody` docstring says "0-based AMS protocol index" but API-CONTRACT Â§9 table says "PHYSICAL slot, not 0-based." Code sends 0-based to printer (correct). Fix the contract language and ensure the APK does the conversion before sending.
26. **print.stop/pause/resume `param:""` field** â€” OpenBambuAPI includes it; bridge builders omit it. Harmless on tested firmware but worth aligning.

---

## 14. Payload Validation Notes (RED and BLACK commands)

| Command | Validation Required |
|---------|---------------------|
| print.stop | None beyond connectivity; already protected by online gate |
| calibration | Whitelist `option` to `{1, 2, 4, 7}` for P1S (no LIDAR); validate `bed_type` is a known enum value; refuse during active print unless `option` is vibration-only (vibration cali during print is documented but risky) |
| upgrade.start | Block entirely at HTTP layer; if ever exposed, require explicit confirmation token, validate `module âˆˆ {"ota","ams"}`, validate URL scheme is `https://`; do not accept user-supplied URLs |
| gcode_line | Min length 1 (present), max length 4096 bytes; disallow multi-command strings containing M500/M501/M502/M503 (EEPROM ops) unless in explicit admin mode; rate-limit to prevent motion queue saturation |
| home (all axes) | Already gated: seeds dead-reckon on success; refuse during active print (add check for gcode_state âˆˆ {IDLE, PAUSE, FINISH, FAILED}) |
| move (jog) | Guards already implemented: step whitelist, homed gate, envelope clamp, fail-closed on unknown position |
| extrude/retract | When added: require homed gate; max extrude per call (e.g. 100mm); require explicit idle/pause state |
| ams_filament_setting | Validate `tray_type` against known material list; validate `nozzle_temp_min < nozzle_temp_max`; validate `nozzle_temp_max â‰¤ 300`; validate `tray_color` is 8-char hex |
| xcam xcam_control_set | Whitelist `module_name` to confirmed P1S set; validate `control` and `print_halt` are booleans; refuse unknown module names |

---

## Sources

| Claim Type | Primary Source | Secondary Source |
|------------|---------------|-----------------|
| Command payloads, field names | Doridian/OpenBambuAPI mqtt.md (fetched 2026-06-11) | repo: research/01-mqtt-protocol.md |
| ha-bambulab patterns | greghesp/ha-bambulab commands.py (fetched 2026-06-11) | repo: research/03-reference-impls.md |
| Bridge current state | repo: src/bambu_bridge/protocol/commands.py, src/bambu_bridge/api/control.py | docs/API-CONTRACT.md |
| P1S firmware behavior | repo: REPORT.md (fw 01.09.01.00 empirical) | research/02-tips-gotchas.md |
| Model-specific notes | research/03-reference-impls.md (bambulabs_api fw branching) | ha-bambulab pybambu const.py Features enum |
| P1S xcam confirmation | OpenBambuAPI (all xcam except LIDAR-dependent modules) | ha-bambulab spaghetti_detector usage |
| Fan index mapping | research/01-mqtt-protocol.md Â§7 + commands.py `_FAN_PARTS` | ha-bambulab models.py fan field mapping |
| Calibration bitmask conflict | OpenBambuAPI (bit0=LIDAR) vs research/01 Â§8 (bit0=vibration) | Reconciled: bit0 = LIDAR on X1, vibration on P1S (no LIDAR) |
