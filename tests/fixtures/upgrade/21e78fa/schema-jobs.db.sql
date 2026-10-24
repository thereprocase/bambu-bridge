CREATE TABLE printers (
  id                TEXT PRIMARY KEY,        -- printer serial
  friendly_name     TEXT NOT NULL,
  ip                TEXT NOT NULL,
  access_code       TEXT NOT NULL,           -- plaintext; .db is access-controlled
  model             TEXT,                    -- "P1S", "X1C", ...
  added_at          INTEGER NOT NULL,        -- unix epoch seconds
  last_seen_at      INTEGER,                 -- updated on successful MQTT receive
  cert_fingerprint  TEXT,                    -- TOFU sha256 of leaf cert (REPORT §1)
  nozzle_type       TEXT                     -- "hardened_steel" → 300 °C; NULL/"stainless_steel" → 280 °C
);
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,            -- UUID4 hex
  printer_id    TEXT NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
  file_name     TEXT NOT NULL,
  file_path     TEXT,                        -- path on printer after upload
  state         TEXT NOT NULL,               -- queued|uploading|started|printing|completed|failed|canceled
  progress_pct  REAL,
  layer_current INTEGER,
  layer_total   INTEGER,
  queued_at     INTEGER NOT NULL,
  started_at    INTEGER,
  finished_at   INTEGER,
  duration_s    INTEGER,
  filament_used_g REAL,
  error_code    TEXT,
  metadata_json TEXT                          -- AMS mapping, slicer info, etc.
);
CREATE INDEX idx_jobs_printer_state ON jobs(printer_id, state);
CREATE INDEX idx_jobs_queued_at ON jobs(queued_at DESC);
CREATE TABLE events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id    TEXT NOT NULL,
  job_id        TEXT,                         -- nullable; not all events are job-scoped
  ts            INTEGER NOT NULL,             -- unix epoch ms
  event_type    TEXT NOT NULL,                -- state_change|error|filament_runout|connection_lost|...
  payload_json  TEXT,
  severity      TEXT,                         -- info|warn|error — contract §8.4
  dismissed_at  INTEGER                       -- unix epoch ms; nullable; NotificationsScreen "Clear all"
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE INDEX idx_events_printer_ts ON events(printer_id, ts DESC);
CREATE INDEX idx_events_job ON events(job_id) WHERE job_id IS NOT NULL;
CREATE TABLE notification_prefs (
  printer_id  TEXT PRIMARY KEY REFERENCES printers(id) ON DELETE CASCADE,
  enabled     INTEGER NOT NULL DEFAULT 1,
  events_json TEXT                          -- JSON array of enabled event names
);
CREATE TABLE print_queue (
  id                TEXT PRIMARY KEY,           -- UUID4 hex
  printer_id        TEXT NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
  file_path         TEXT NOT NULL,              -- printer-side path (model/<name>.gcode.3mf)
  file_name         TEXT NOT NULL,              -- display name
  ams_mapping_json  TEXT,                       -- Orca ams_mapping: tray per project filament, -1 unused, e.g. "[1,-1]"
  position          INTEGER NOT NULL,           -- 0-based ordering within printer_id
  added_at          INTEGER NOT NULL,           -- unix epoch seconds
  notes             TEXT
);
CREATE INDEX idx_queue_printer_position
  ON print_queue(printer_id, position);
CREATE TABLE filament_memory (
  printer_id    TEXT NOT NULL,
  slot          INTEGER NOT NULL,   -- physical_slot (1-4)
  make          TEXT,
  model         TEXT,
  profile       TEXT,
  tray_type_seen TEXT,              -- the tray_type value when the label was written
  updated_at    INTEGER NOT NULL,   -- unix epoch seconds
  PRIMARY KEY (printer_id, slot)
);
CREATE TABLE spool_inventory (
  id            TEXT PRIMARY KEY,             -- UUID4 hex
  name          TEXT NOT NULL,                -- "PLA Matte Charcoal"
  material      TEXT NOT NULL,                -- "PLA" | "PETG" | "ASA" | ...
  color_hex     TEXT NOT NULL,                -- "#1a1a1f"
  brand         TEXT,                         -- "Bambu" | "Polymaker" | ...
  total_g       REAL,                         -- typical 1000; nullable
  remaining_g   REAL,                         -- user-updated; nullable
  notes         TEXT,
  added_at      INTEGER NOT NULL,
  provider_migration TEXT                     -- durable inventory cutover token
);
CREATE TABLE sliced_dates (
  filename    TEXT NOT NULL,
  size_bytes  INTEGER NOT NULL,
  sliced_at   TEXT NOT NULL,
  learned_at  TEXT NOT NULL,
  PRIMARY KEY (filename, size_bytes)
);
