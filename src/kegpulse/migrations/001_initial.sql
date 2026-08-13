CREATE TABLE participants (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 80),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE kegs (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 120),
    starting_volume_ml TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    notes TEXT NOT NULL DEFAULT '' CHECK(length(notes) <= 1000)
);
CREATE UNIQUE INDEX one_open_keg ON kegs((1)) WHERE closed_at IS NULL;

CREATE TABLE calibrations (
    id TEXT PRIMARY KEY,
    liquid TEXT NOT NULL CHECK(length(liquid) BETWEEN 1 AND 80),
    default_density_g_per_ml TEXT NOT NULL,
    pulses_per_ml TEXT,
    status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'superseded')),
    notes TEXT NOT NULL DEFAULT '' CHECK(length(notes) <= 1000),
    created_at TEXT NOT NULL,
    activated_at TEXT
);
CREATE UNIQUE INDEX one_active_calibration ON calibrations((1)) WHERE status = 'active';

CREATE TABLE calibration_samples (
    id TEXT PRIMARY KEY,
    calibration_id TEXT NOT NULL REFERENCES calibrations(id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 10),
    raw_pulses INTEGER NOT NULL CHECK(raw_pulses > 0),
    mass_g TEXT NOT NULL,
    density_g_per_ml TEXT NOT NULL,
    derived_volume_ml TEXT NOT NULL,
    included INTEGER NOT NULL CHECK(included IN (0, 1)),
    suspected_outlier INTEGER NOT NULL DEFAULT 0 CHECK(suspected_outlier IN (0, 1)),
    captured_at TEXT NOT NULL,
    UNIQUE(calibration_id, ordinal)
);

CREATE TABLE verification_checks (
    id TEXT PRIMARY KEY,
    calibration_id TEXT NOT NULL REFERENCES calibrations(id),
    keg_id TEXT REFERENCES kegs(id),
    raw_pulses INTEGER NOT NULL CHECK(raw_pulses > 0),
    mass_g TEXT NOT NULL,
    density_g_per_ml TEXT NOT NULL,
    predicted_volume_ml TEXT NOT NULL,
    actual_volume_ml TEXT NOT NULL,
    absolute_error_ml TEXT NOT NULL,
    percentage_error TEXT NOT NULL,
    warning INTEGER NOT NULL CHECK(warning IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE provisional_sessions (
    session_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL DEFAULT 'pour' CHECK(purpose IN ('pour', 'calibration', 'verification')),
    participant_id TEXT REFERENCES participants(id),
    keg_id TEXT REFERENCES kegs(id),
    calibration_id TEXT REFERENCES calibrations(id),
    target_ordinal INTEGER CHECK(target_ordinal BETWEEN 1 AND 10),
    device_id TEXT,
    boot_id TEXT,
    event_seq INTEGER,
    confirmed_lifetime TEXT NOT NULL DEFAULT '0',
    captured_raw_pulses INTEGER CHECK(captured_raw_pulses >= 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(device_id, boot_id, event_seq)
);

CREATE TABLE pour_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    participant_id TEXT REFERENCES participants(id),
    keg_id TEXT REFERENCES kegs(id),
    calibration_id TEXT REFERENCES calibrations(id),
    device_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    event_seq INTEGER,
    raw_pulses INTEGER NOT NULL CHECK(raw_pulses >= 0),
    volume_ml TEXT,
    attributed INTEGER NOT NULL CHECK(attributed IN (0, 1)),
    quality TEXT NOT NULL CHECK(quality IN ('complete', 'unattributed', 'interrupted', 'estimated_recovered', 'needs_review')),
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    device_started_ms INTEGER NOT NULL,
    device_ended_ms INTEGER NOT NULL,
    fault TEXT NOT NULL DEFAULT 'none',
    created_at TEXT NOT NULL,
    UNIQUE(device_id, boot_id, event_seq)
);

CREATE TABLE device_results (
    device_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL,
    raw_pulses INTEGER NOT NULL,
    pour_id TEXT REFERENCES pour_events(id),
    committed_at TEXT NOT NULL,
    PRIMARY KEY(device_id, boot_id, event_seq)
);

CREATE TABLE inventory_adjustments (
    id TEXT PRIMARY KEY,
    keg_id TEXT NOT NULL REFERENCES kegs(id),
    amount_ml TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 500),
    created_at TEXT NOT NULL
);

CREATE TABLE attribution_audit (
    id TEXT PRIMARY KEY,
    pour_id TEXT NOT NULL REFERENCES pour_events(id),
    old_participant_id TEXT REFERENCES participants(id),
    new_participant_id TEXT REFERENCES participants(id),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 500),
    created_at TEXT NOT NULL
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE device_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    context_json TEXT NOT NULL
);
CREATE INDEX diagnostics_created ON device_diagnostics(created_at DESC);
CREATE INDEX pours_ended ON pour_events(ended_at DESC);
CREATE INDEX pours_participant ON pour_events(participant_id, ended_at DESC);
