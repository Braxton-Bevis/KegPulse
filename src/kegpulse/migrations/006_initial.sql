-- Allow unattributed pour evidence: photos may exist without a provisional session.
CREATE TABLE pour_photos_new (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES provisional_sessions(session_id),
    captured_at TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 4 AND 65536),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64)
);
INSERT INTO pour_photos_new SELECT id, session_id, captured_at, relative_path, size_bytes, sha256 FROM pour_photos;
DROP TABLE pour_photos;
ALTER TABLE pour_photos_new RENAME TO pour_photos;
CREATE INDEX pour_photos_session ON pour_photos(session_id, captured_at);
