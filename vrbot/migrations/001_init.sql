-- Core schema
CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,              -- ISO-8601 UTC
    guild_id         INTEGER,
    type             TEXT    NOT NULL,              -- member_join, voice_move, member_ban, ...
    category         TEXT    NOT NULL,              -- membership|voice|moderation|structure|message|bot|security
    target_id        INTEGER,
    target_name      TEXT,
    actor_id         INTEGER,
    actor_name       TEXT,
    actor_confidence TEXT    NOT NULL DEFAULT 'unknown',  -- confirmed|likely|self|unknown
    channel_id       INTEGER,
    channel_name     TEXT,
    role_id          INTEGER,
    role_name        TEXT,
    reason           TEXT,
    details          TEXT,                          -- JSON
    source           TEXT                           -- gateway|audit_log|bot
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_type_ts ON events(type, ts);
CREATE INDEX IF NOT EXISTS ix_events_target_ts ON events(target_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_actor_ts ON events(actor_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_cat_ts ON events(category, ts);

CREATE TABLE IF NOT EXISTS mod_cases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    guild_id       INTEGER NOT NULL,
    action         TEXT NOT NULL,        -- warn|timeout|untimeout|kick|ban|unban|purge|lock|unlock|slowmode
    user_id        INTEGER,
    user_name      TEXT,
    moderator_id   INTEGER NOT NULL,
    moderator_name TEXT,
    reason         TEXT,
    duration_s     INTEGER,
    channel_id     INTEGER,
    active         INTEGER NOT NULL DEFAULT 1,
    extra          TEXT
);
CREATE INDEX IF NOT EXISTS ix_cases_user ON mod_cases(user_id, ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    guild_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,       -- manual|auto|pre-change
    label      TEXT,
    created_by INTEGER,
    summary    TEXT,
    data       TEXT NOT NULL        -- JSON (model.Guild.to_dict, members stripped)
);
CREATE INDEX IF NOT EXISTS ix_snap_ts ON snapshots(guild_id, ts);

CREATE TABLE IF NOT EXISTS change_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    guild_id    INTEGER NOT NULL,
    actor_id    INTEGER,
    source      TEXT NOT NULL,      -- repair|restore|autoheal|rollback|lock
    status      TEXT NOT NULL,      -- planned|applied|partial|failed|rolled_back
    summary     TEXT,
    changes     TEXT NOT NULL,      -- JSON list of Change (before/after = rollback data)
    result      TEXT,
    snapshot_id INTEGER
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
