-- Bambu Bridge SQLite schema (spec 9).
--
-- Idempotent: every statement is IF NOT EXISTS so the file doubles as the
-- migration. M2 introduces `printers`; `jobs` and `events` arrive in M4.
--
-- WAL lets the backup script (.backup) and API reads run while the bridge
-- writes. Set on every connect, not here (a PRAGMA in a script run once is
-- not enough — journal_mode is persistent but we assert it per connection).

CREATE TABLE IF NOT EXISTS printers (
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

CREATE TABLE IF NOT EXISTS jobs (
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

CREATE INDEX IF NOT EXISTS idx_jobs_printer_state ON jobs(printer_id, state);
CREATE INDEX IF NOT EXISTS idx_jobs_queued_at ON jobs(queued_at DESC);

-- Start identities are retained as tombstones, not expired into new commands.
-- A reservation is a durable row, never a long-held database transaction.
CREATE TABLE IF NOT EXISTS start_operations (
  id TEXT PRIMARY KEY,
  printer_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  job_id TEXT NOT NULL,
  queue_id TEXT UNIQUE,
  state TEXT NOT NULL,
  holds_printer INTEGER NOT NULL DEFAULT 1 CHECK (holds_printer IN (0,1)),
  revision INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_start_owner ON start_operations(printer_id)
  WHERE holds_printer=1;

-- Terminal operation identities survive history/printer deletion so replay
-- cannot silently create a new print. Active ownership cannot be cascaded away.
CREATE TRIGGER IF NOT EXISTS protect_start_owner BEFORE DELETE ON printers
WHEN EXISTS (SELECT 1 FROM start_operations WHERE printer_id=OLD.id AND holds_printer=1)
BEGIN
  SELECT RAISE(ABORT, 'unresolved_start_owner');
END;

CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id    TEXT NOT NULL,
  job_id        TEXT,                         -- nullable; not all events are job-scoped
  ts            INTEGER NOT NULL,             -- unix epoch ms
  event_type    TEXT NOT NULL,                -- state_change|error|filament_runout|connection_lost|...
  payload_json  TEXT,
  severity      TEXT,                         -- info|warn|error — contract §8.4
  dismissed_at  INTEGER                       -- unix epoch ms; nullable; NotificationsScreen "Clear all"
);

CREATE INDEX IF NOT EXISTS idx_events_printer_ts ON events(printer_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id) WHERE job_id IS NOT NULL;

-- Optional per-printer push config (M6). Absent row => notifications on for
-- the default event set.
CREATE TABLE IF NOT EXISTS notification_prefs (
  printer_id  TEXT PRIMARY KEY REFERENCES printers(id) ON DELETE CASCADE,
  enabled     INTEGER NOT NULL DEFAULT 1,
  events_json TEXT                          -- JSON array of enabled event names
);

-- User-orderable print queue (design Files / Queue tab). Items here are
-- staged 3MFs waiting to be started; starting an item submits it via
-- JobManager and removes it from the queue.
CREATE TABLE IF NOT EXISTS print_queue (
  id                TEXT PRIMARY KEY,           -- UUID4 hex
  printer_id        TEXT NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
  file_path         TEXT NOT NULL,              -- printer-side path (model/<name>.gcode.3mf)
  file_name         TEXT NOT NULL,              -- display name
  ams_mapping_json  TEXT,                       -- JSON array of physical_slot ints, e.g. "[1,3]"
  position          INTEGER NOT NULL,           -- 0-based ordering within printer_id
  added_at          INTEGER NOT NULL,           -- unix epoch seconds
  notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_printer_position
  ON print_queue(printer_id, position);

-- Per-slot filament memory (G3). User-labelled make/model/profile for each
-- AMS slot; remembered until that slot's tray TYPE changes (tray swap).
-- Empty/absent tray does NOT invalidate — only a different non-empty type does.
CREATE TABLE IF NOT EXISTS filament_memory (
  printer_id    TEXT NOT NULL,
  slot          INTEGER NOT NULL,   -- physical_slot (1-4)
  make          TEXT,
  model         TEXT,
  profile       TEXT,
  tray_type_seen TEXT,              -- the tray_type value when the label was written
  updated_at    INTEGER NOT NULL,   -- unix epoch seconds
  PRIMARY KEY (printer_id, slot)
);

-- Off-AMS filament cabinet inventory (design Filament / Cabinet section).
-- User-managed; bridge does not auto-discover. Distinct from AMS-loaded
-- spools which are reported by the printer in ams.tray[].
CREATE TABLE IF NOT EXISTS spool_inventory (
  id            TEXT PRIMARY KEY,             -- UUID4 hex
  name          TEXT NOT NULL,                -- "PLA Matte Charcoal"
  material      TEXT NOT NULL,                -- "PLA" | "PETG" | "ASA" | ...
  color_hex     TEXT NOT NULL,                -- "#1a1a1f"
  brand         TEXT,                         -- "Bambu" | "Polymaker" | ...
  total_g       REAL,                         -- typical 1000; nullable
  remaining_g   REAL,                         -- user-updated; nullable
  notes         TEXT,
  added_at      INTEGER NOT NULL
);
