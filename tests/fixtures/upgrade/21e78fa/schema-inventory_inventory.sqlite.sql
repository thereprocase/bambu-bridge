CREATE TABLE spool_assignments (singleton INTEGER PRIMARY KEY CHECK(singleton=1), provider_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE inventory_provider (singleton INTEGER PRIMARY KEY CHECK(singleton=1), locator_hash TEXT NOT NULL);
CREATE TABLE inventory_material_revision (
                    provider_id TEXT PRIMARY KEY, revision INTEGER NOT NULL
                );
CREATE TABLE inventory_material_mappings (
                    provider_id TEXT NOT NULL, product_id INTEGER NOT NULL,
                    stock_key TEXT NOT NULL, catalogue_digest TEXT NOT NULL,
                    product_fingerprint TEXT NOT NULL, PRIMARY KEY(provider_id,product_id)
                );
CREATE TABLE inventory_rollbacks (
                    token TEXT PRIMARY KEY, migration_token TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    spool_id INTEGER NOT NULL, baseline TEXT NOT NULL, source TEXT NOT NULL,
                    state TEXT NOT NULL, note TEXT, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
CREATE UNIQUE INDEX inventory_rollback_active
                    ON inventory_rollbacks(migration_token)
                    WHERE state NOT IN ('cancelled','released');
CREATE TABLE inventory_migrations (
                    token TEXT PRIMARY KEY, legacy_uuid TEXT NOT NULL, provider_id TEXT NOT NULL,
                    mode TEXT NOT NULL, target_id INTEGER NOT NULL, source TEXT NOT NULL,
                    target TEXT NOT NULL, state TEXT NOT NULL, spool_id INTEGER, cache TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
CREATE UNIQUE INDEX inventory_migration_active_uuid
                    ON inventory_migrations(legacy_uuid)
                    WHERE state NOT IN ('cancelled','rolled_back');
CREATE UNIQUE INDEX inventory_migration_active_spool
                    ON inventory_migrations(provider_id,spool_id)
                    WHERE state NOT IN ('cancelled','rolled_back');
CREATE TABLE inventory_migration_events (
                    id INTEGER PRIMARY KEY, token TEXT NOT NULL, state TEXT NOT NULL,
                    detail TEXT NOT NULL, occurred_at REAL NOT NULL
                );
CREATE TABLE inventory_recoveries (
                    token TEXT PRIMARY KEY, old_provider TEXT NOT NULL, new_provider TEXT NOT NULL,
                    nonce TEXT NOT NULL, baseline TEXT NOT NULL, state TEXT NOT NULL,
                    note TEXT, created_at REAL NOT NULL, accepted_at REAL
                );
CREATE TABLE inventory_provider_checkpoint (
                    provider_id TEXT PRIMARY KEY, expected TEXT, pending TEXT,
                    pending_token TEXT, blocked_reason TEXT
                );
CREATE TABLE inventory_display_policy (
                    provider_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), printers TEXT NOT NULL
                );
CREATE TABLE inventory_checkpoint_events (
                    id INTEGER PRIMARY KEY, provider_id TEXT NOT NULL, token TEXT NOT NULL,
                    checkpoint TEXT NOT NULL, occurred_at REAL NOT NULL
                );
CREATE TABLE inventory_operations (
                    token TEXT PRIMARY KEY, provider_id TEXT NOT NULL,
                    kind TEXT NOT NULL, resource TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('prepared','inflight','applied','rejected','uncertain','superseded')),
                    result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
CREATE UNIQUE INDEX inventory_resource_pending
                    ON inventory_operations(provider_id, resource)
                    WHERE state IN ('prepared','inflight','uncertain');
CREATE TABLE inventory_observations (
                    provider_id TEXT NOT NULL, spool_id INTEGER NOT NULL,
                    remaining_g REAL, basis TEXT NOT NULL, observed_at REAL NOT NULL,
                    operation_token TEXT NOT NULL UNIQUE
                );
CREATE INDEX inventory_observation_spool
                    ON inventory_observations(provider_id, spool_id, observed_at);
CREATE TABLE inventory_operation_events (
                    id INTEGER PRIMARY KEY, token TEXT NOT NULL,
                    state TEXT NOT NULL, detail TEXT NOT NULL, occurred_at REAL NOT NULL
                );
CREATE UNIQUE INDEX inventory_consumption_attempt ON inventory_operations(provider_id,json_extract(payload,'$.attempt_id'),json_extract(payload,'$.spool_id')) WHERE kind='consume';
