-- Music history + favourites. Additive only: no existing table is touched.
-- Only what recommendations need: track identity, title/artist, source, requester, time. The room id is kept for
-- de-duplication/statistics but is never shown to members.
CREATE TABLE IF NOT EXISTS music_plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    track_key TEXT NOT NULL,          -- uri (or source:identifier) — the dedup key
    title TEXT NOT NULL,
    author TEXT,
    uri TEXT,
    source TEXT,
    length_ms INTEGER,
    requester_id INTEGER,
    channel_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_music_plays_key ON music_plays (guild_id, track_key);
CREATE INDEX IF NOT EXISTS idx_music_plays_ts ON music_plays (guild_id, ts);

CREATE TABLE IF NOT EXISTS music_favorites (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,         -- 0 = server favourite (owner-pinned shortcut)
    track_key TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    uri TEXT NOT NULL,
    label TEXT,                       -- optional short name for server shortcuts
    added_ts TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, track_key)
);
