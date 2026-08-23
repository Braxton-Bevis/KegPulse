ALTER TABLE participants ADD COLUMN balance_cents INTEGER NOT NULL DEFAULT 0;

CREATE TABLE pour_charges (
    pour_id TEXT PRIMARY KEY REFERENCES pour_events(id),
    participant_id TEXT NOT NULL REFERENCES participants(id),
    volume_ml TEXT NOT NULL,
    rate_cents_per_fl_oz TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE account_ledger (
    id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents != 0),
    kind TEXT NOT NULL CHECK(kind IN ('adjustment', 'charge', 'refund')),
    pour_id TEXT REFERENCES pour_events(id),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 500),
    balance_after_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX account_ledger_participant ON account_ledger(participant_id, created_at DESC);

CREATE TABLE pour_photos (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES provisional_sessions(session_id),
    captured_at TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 4 AND 65536),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64)
);
CREATE INDEX pour_photos_session ON pour_photos(session_id, captured_at);
