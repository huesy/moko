-- Moko's mind — consolidated initial schema (v1).
--
-- One migration that creates the complete current schema. It replaces the former
-- 25-file migration history, squashed before the first public release so a fresh
-- install starts from a single clean slate. See SCHEMA.md for the rationale behind
-- every table. Principles enforced here: everything sourced + time-aware (UTC text),
-- typed values, soft-not-destructive (valid_to over DELETE), thoughts decay but are
-- never deleted. IDs are ULIDs (text).


-- ── Tables ────────────────────────────────────────────
CREATE TABLE "subjects" (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- person | place | org | object | event | topic | pet | self ...
    name        TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL           -- UTC ISO-8601
, merged_into TEXT REFERENCES "subjects" (id), world_id TEXT REFERENCES worlds (id));
CREATE TABLE "subject_aliases" (
    id          TEXT PRIMARY KEY,
    subject_id   TEXT NOT NULL REFERENCES "subjects" (id),
    alias       TEXT NOT NULL,
    source_id   TEXT REFERENCES sources (id),
    created_at  TEXT NOT NULL
);
CREATE TABLE "subject_merges" (
    id          TEXT PRIMARY KEY,
    from_id     TEXT NOT NULL,
    into_id     TEXT NOT NULL,
    at          TEXT NOT NULL,
    by          TEXT NOT NULL DEFAULT 'write',  -- write | dream | user
    reason      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE merge_candidates (
    id          TEXT PRIMARY KEY,
    a_id        TEXT NOT NULL REFERENCES "subjects" (id),
    b_id        TEXT NOT NULL REFERENCES "subjects" (id),
    score       REAL NOT NULL DEFAULT 0.0,
    evidence    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',   -- open | confirmed | rejected
    created_at  TEXT NOT NULL
);
CREATE TABLE sources (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- message | email | web | file | calendar | inference | user_stated
    ref         TEXT NOT NULL DEFAULT '',
    channel     TEXT,
    excerpt     TEXT,
    observed_at TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE inference_inputs (
    id                  TEXT PRIMARY KEY,
    inference_source_id TEXT NOT NULL REFERENCES sources (id),
    input_ref           TEXT NOT NULL   -- a fact/source id this inference rested on
);
CREATE TABLE attributes (
    id          TEXT PRIMARY KEY,
    subject_id   TEXT NOT NULL REFERENCES "subjects" (id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,          -- serialized; parsed per value_type
    value_type  TEXT NOT NULL DEFAULT 'string',  -- string | date | datetime | number | bool | json
    valid_from  TEXT,
    valid_to    TEXT,                   -- NULL = still true
    source_id   TEXT NOT NULL REFERENCES sources (id),
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL
);
CREATE TABLE relationships (
    id          TEXT PRIMARY KEY,
    from_id     TEXT NOT NULL REFERENCES "subjects" (id),
    to_id       TEXT NOT NULL REFERENCES "subjects" (id),
    type        TEXT NOT NULL,          -- married_to | parent_of | works_at | friend_of | lives_in ...
    valid_from  TEXT,
    valid_to    TEXT,                   -- NULL = current
    directed    INTEGER NOT NULL DEFAULT 1,  -- 1 = directed, 0 = symmetric
    source_id   TEXT NOT NULL REFERENCES sources (id),
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL
);
CREATE TABLE messages (
    id                   TEXT PRIMARY KEY,
    channel              TEXT NOT NULL,
    speaker_id           TEXT REFERENCES "subjects" (id),
    direction            TEXT NOT NULL,  -- in | out
    body                 TEXT NOT NULL,
    sent_at              TEXT NOT NULL,  -- UTC
    sentiment            TEXT,           -- perceived emotion of the SPEAKER
    valence              REAL,           -- -1..1
    arousal              REAL,           -- 0..1
    sentiment_confidence REAL,
    meta                 TEXT,           -- json
    created_at           TEXT NOT NULL
, session_id TEXT REFERENCES sessions (id));
CREATE TABLE thoughts (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,      -- opinion | concern | hypothesis | prediction | curiosity | feeling | note
    body             TEXT NOT NULL,
    valence          REAL,               -- how MOKO feels (distinct from message sentiment)
    shareability     TEXT NOT NULL DEFAULT 'private',  -- private | on_ask | shareable
    prudence         REAL NOT NULL DEFAULT 1.0,        -- current weight; decays, never to deletion
    stability        REAL NOT NULL DEFAULT 0.5,        -- resistance to decay
    created_at       TEXT NOT NULL,
    last_reinforced_at TEXT,
    last_recompute_at  TEXT,
    reconsider_at      TEXT
, world_id TEXT REFERENCES worlds (id));
CREATE TABLE thought_links (
    id          TEXT PRIMARY KEY,
    thought_id  TEXT NOT NULL REFERENCES thoughts (id),
    target_kind TEXT NOT NULL,          -- entity | fact | relationship | message | gap
    target_id   TEXT NOT NULL
);
CREATE TABLE knowledge_gaps (
    id              TEXT PRIMARY KEY,
    about_id        TEXT REFERENCES "subjects" (id),
    description     TEXT NOT NULL,
    kind            TEXT NOT NULL,       -- missing_attr | unconfirmed_rel | dangling_ref | stale_fact | causal | verification | open_curiosity
    resolution_path TEXT NOT NULL DEFAULT 'either',  -- research | ask | either
    salience        REAL NOT NULL DEFAULT 0.5,
    status          TEXT NOT NULL DEFAULT 'open',     -- open | researching | queued | asked | resolved | abandoned
    opened_at       TEXT NOT NULL,
    last_attempt_at TEXT,
    last_asked_at   TEXT,
    resolved_by     TEXT REFERENCES sources (id)
, world_id TEXT REFERENCES worlds (id));
CREATE TABLE "outreach" (
    id               TEXT PRIMARY KEY,
    channel          TEXT NOT NULL,
    kind             TEXT NOT NULL,      -- question | checkin | surface | notice | consent_request | urgent
    gap_id           TEXT REFERENCES knowledge_gaps (id),
    counts_to_budget INTEGER NOT NULL DEFAULT 1,
    sent_at          TEXT NOT NULL,
    engaged          INTEGER             -- NULL unknown, 0 no, 1 yes
);
CREATE TABLE "dreams" (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    summary     TEXT NOT NULL DEFAULT '',
    stats       TEXT,                   -- json
    wonders     TEXT
);
CREATE TABLE "approvals" (
    id           TEXT PRIMARY KEY,
    action       TEXT NOT NULL,
    kind         TEXT NOT NULL,         -- upgrade | system | other
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied | executed | expired
    requested_at TEXT NOT NULL,
    decided_at   TEXT,
    executed_at  TEXT
);
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,                          -- NULL = the active session
    started_reason  TEXT NOT NULL DEFAULT 'first', -- first | dream | user | size | idle
    ended_reason    TEXT,
    carryover       TEXT,                          -- short hand-off note to the next session
    token_estimate  INTEGER NOT NULL DEFAULT 0     -- rough running size, for size-based rotation
);
CREATE TABLE commitments (
    id           TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'reminder',  -- reminder | task | followup
    about_id     TEXT REFERENCES "subjects" (id),
    due_at       TEXT,                              -- UTC; NULL = someday / no fixed time
    status       TEXT NOT NULL DEFAULT 'open',      -- open | done | cancelled
    source_id    TEXT REFERENCES sources (id),
    created_at   TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE "outreach_decisions" (
    id               TEXT PRIMARY KEY,
    at               TEXT NOT NULL,
    acted            INTEGER NOT NULL,  -- 1 = decided to reach out, 0 = held off
    candidate        TEXT,              -- the [token] it weighed (curiosity/commitment/…)
    reasoning        TEXT,              -- its private note on why
    reconsider_minutes REAL              -- when it'll next consider (hours from then)
);
CREATE TABLE "actions" (
    id      TEXT PRIMARY KEY,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,   -- system | file | skill | outreach | …
    summary TEXT NOT NULL,   -- a human one-liner ("run the command: npm run build")
    ok      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE media (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources (id),  -- provenance (mandatory)
    message_id  TEXT REFERENCES messages (id),          -- the message it arrived on (NULL = generated/outbound)
    kind        TEXT NOT NULL,                           -- image | audio | video | document
    mime        TEXT,                                    -- image/jpeg, audio/ogg, application/pdf ...
    sha256      TEXT NOT NULL,                           -- content hash: dedup + integrity
    bytes       INTEGER NOT NULL DEFAULT 0,              -- size on disk
    path        TEXT NOT NULL,                           -- stored name under ~/.moko/media/ (relative → portable)
    filename    TEXT,                                    -- the sender's original filename, for display/recall
    caption     TEXT,                                    -- the sender's caption, if any
    direction   TEXT NOT NULL DEFAULT 'in',              -- in (received) | out (sent/generated)
    created_at  TEXT NOT NULL                            -- UTC ISO-8601
, description TEXT, taken_at TEXT, world_analyzed INTEGER NOT NULL DEFAULT 0, world_id TEXT REFERENCES worlds (id));
CREATE TABLE observations (
    id          TEXT PRIMARY KEY,                          -- ULID
    subject_id  TEXT NOT NULL REFERENCES subjects (id),    -- usually the companion's self node
    key         TEXT NOT NULL,                             -- stable series name: sleep_hours, weight_kg, run_cancelled
    value       TEXT NOT NULL,                             -- serialized; parsed per value_type
    value_type  TEXT NOT NULL DEFAULT 'string',            -- string | date | datetime | number | bool | json
    observed_at TEXT NOT NULL,                             -- UTC instant the datum is TRUE (event time)
    source_id   TEXT NOT NULL REFERENCES sources (id),     -- provenance floor, mandatory
    created_at  TEXT NOT NULL,                             -- UTC, when Moko recorded it
    -- One reading per (subject, key, instant): restating a datapoint updates it
    -- in place rather than double-counting in a COUNT/AVG over the series.
    UNIQUE (subject_id, key, observed_at)
);
CREATE TABLE "llm_calls" (
    id                 TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,                   -- UTC ISO-8601
    kind               TEXT NOT NULL,                   -- reply | vision | reflection | dream | world | outreach | research | other
    model              TEXT,                            -- the brain model id used
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,      -- input served from prompt cache (the win)
    cache_write_tokens INTEGER NOT NULL DEFAULT 0       -- tokens written to cache (Anthropic; one-time)
, system_prompt TEXT, request TEXT, response TEXT, tool_calls TEXT);
CREATE TABLE worlds (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,   -- 'companion' | 'moko'  (extensible)
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL    -- UTC ISO-8601
);

-- ── Full-text recall (app-maintained FTS5 index) ──────────────
CREATE VIRTUAL TABLE search USING fts5 (
    content,
    kind UNINDEXED,     -- entity | attribute | message | thought
    ref_id UNINDEXED
);

-- ── Indexes ─────────────────────────────────────────
CREATE INDEX idx_aliases_alias ON "subject_aliases" (alias);
CREATE INDEX idx_merge_candidates_status ON merge_candidates (status);
CREATE INDEX idx_attributes_current ON attributes (subject_id, key, valid_to);
CREATE INDEX idx_messages_speaker ON messages (speaker_id);
CREATE INDEX idx_messages_sent_at ON messages (sent_at);
CREATE INDEX idx_thoughts_kind ON thoughts (kind);
CREATE INDEX idx_thought_links_thought ON thought_links (thought_id);
CREATE INDEX idx_thought_links_target ON thought_links (target_kind, target_id);
CREATE INDEX idx_gaps_status_salience ON knowledge_gaps (status, salience);
CREATE INDEX idx_outreach_sent_at ON "outreach" (sent_at);
CREATE INDEX idx_sessions_active ON sessions (ended_at);
CREATE INDEX idx_commitments_due ON commitments (status, due_at);
CREATE INDEX idx_messages_session_sent ON messages (session_id, sent_at);
CREATE INDEX idx_rel_from_current ON relationships (from_id, valid_to);
CREATE INDEX idx_rel_to_current ON relationships (to_id, valid_to);
CREATE INDEX idx_thoughts_recent ON thoughts (created_at DESC);
CREATE INDEX idx_media_message ON media (message_id);
CREATE INDEX idx_media_sha ON media (sha256);
CREATE INDEX idx_observations_series ON observations (subject_id, key, observed_at);
CREATE INDEX idx_subjects_kind_name ON subjects (kind, name);
CREATE INDEX idx_subjects_live ON subjects (merged_into);
CREATE INDEX idx_aliases_subject ON subject_aliases (subject_id);
CREATE INDEX idx_attributes_subject_current ON attributes (subject_id, valid_to);
CREATE INDEX idx_subjects_world ON subjects (world_id);
CREATE INDEX idx_thoughts_world ON thoughts (world_id);
CREATE INDEX idx_gaps_world ON knowledge_gaps (world_id);
CREATE INDEX idx_media_world_id ON media (world_id);
CREATE INDEX idx_llm_calls_created ON llm_calls (created_at);
CREATE INDEX idx_llm_calls_kind ON llm_calls (kind);
CREATE INDEX idx_outreach_decisions_at ON outreach_decisions (at);
CREATE INDEX idx_actions_at ON actions (at);

-- ── Seed: the two worlds (companion's vs Moko's own) ───────────
INSERT INTO worlds (id, kind, name, created_at) VALUES ('world-companion', 'companion', 'the companion''s world', '2026-06-22T00:00:00+00:00');
INSERT INTO worlds (id, kind, name, created_at) VALUES ('world-moko', 'moko', 'Moko''s world', '2026-06-22T00:00:00+00:00');
