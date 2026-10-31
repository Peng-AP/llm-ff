"""SQLite storage. One file, data/league.db. NFL reference data lives in nfl_* tables;
league state (rosters, lineups, transactions) will get its own tables alongside."""

import os
import sqlite3
from pathlib import Path

from aiffl.config import ROOT

DB_PATH = Path(os.environ["AIFFL_DB"]).resolve() if os.environ.get("AIFFL_DB") else ROOT / "data" / "league.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS nfl_players (
    player_id              TEXT PRIMARY KEY,   -- Sleeper ID; team abbreviation for DEF
    full_name              TEXT NOT NULL,
    position               TEXT NOT NULL,      -- QB RB WR TE K DEF
    team                   TEXT,               -- NULL = free agent / unsigned
    status                 TEXT,               -- Active, Injured Reserve, Practice Squad, ...
    injury_status          TEXT,               -- Questionable, Doubtful, Out, IR, ...
    injury_body_part       TEXT,
    injury_notes           TEXT,
    practice_participation TEXT,
    depth_chart_position   TEXT,
    depth_chart_order      INTEGER,
    age                    INTEGER,
    years_exp              INTEGER,
    news_updated           TEXT,               -- ISO UTC time Sleeper last saw news
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nfl_games (
    game_id     TEXT PRIMARY KEY,              -- ESPN event ID
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    kickoff_utc TEXT NOT NULL,                 -- ISO 8601, e.g. 2026-11-06T01:15:00Z
    home        TEXT NOT NULL,
    away        TEXT NOT NULL,
    state       TEXT NOT NULL,                 -- pre, in, post
    home_score  INTEGER,
    away_score  INTEGER,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nfl_games_week ON nfl_games (season, week);

CREATE TABLE IF NOT EXISTS nfl_stats (
    season     INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    player_id  TEXT NOT NULL,
    stats      TEXT NOT NULL,                  -- raw Sleeper stat line, JSON
    updated_at TEXT NOT NULL,
    PRIMARY KEY (season, week, player_id)
);

CREATE TABLE IF NOT EXISTS nfl_projections (
    season     INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    player_id  TEXT NOT NULL,
    stats      TEXT NOT NULL,                  -- raw Sleeper projected stat line, JSON
    updated_at TEXT NOT NULL,
    PRIMARY KEY (season, week, player_id)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    ran_at  TEXT NOT NULL,
    detail  TEXT
);

-- Derived per-player numbers for search and drafting; rebuilt by the sync.
CREATE TABLE IF NOT EXISTS nfl_player_summary (
    player_id       TEXT PRIMARY KEY,
    games           INTEGER NOT NULL,          -- games played so far this season
    season_points   REAL NOT NULL,             -- league scoring, weeks played so far
    points_per_game REAL,
    ros_projection  REAL NOT NULL,             -- projected points, league start week .. week 17
    next_week_proj  REAL,                      -- projected points, next unplayed week
    bye_week        INTEGER,                   -- remaining bye only; NULL if already taken
    updated_at      TEXT NOT NULL
);

-- League state ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS league_teams (
    team_id    TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    harness    TEXT NOT NULL,
    draft_slot INTEGER UNIQUE                  -- 1-based position in round 1
);

CREATE TABLE IF NOT EXISTS draft_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    status      TEXT NOT NULL,                 -- pending, prep, live, done
    rounds      INTEGER NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS draft_picks (
    pick_no   INTEGER PRIMARY KEY,             -- 1-based overall pick
    round     INTEGER NOT NULL,
    team_id   TEXT NOT NULL REFERENCES league_teams (team_id),
    player_id TEXT UNIQUE REFERENCES nfl_players (player_id),   -- NULL until made
    made_at   TEXT,
    method    TEXT                             -- agent, autopick_queue, autopick_projection
);

CREATE TABLE IF NOT EXISTS draft_queue (
    team_id   TEXT NOT NULL REFERENCES league_teams (team_id),
    rank      INTEGER NOT NULL,
    player_id TEXT NOT NULL REFERENCES nfl_players (player_id),
    PRIMARY KEY (team_id, rank)
);

CREATE TABLE IF NOT EXISTS rosters (
    player_id   TEXT PRIMARY KEY REFERENCES nfl_players (player_id),  -- a player is on one team at most
    team_id     TEXT NOT NULL REFERENCES league_teams (team_id),
    acquired_at TEXT NOT NULL,
    how         TEXT NOT NULL                  -- draft, waiver, free_agent, trade
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    team_id    TEXT NOT NULL,
    phase      TEXT NOT NULL,                  -- e.g. draft
    harness    TEXT NOT NULL,
    session_id TEXT,                           -- the harness's own conversation ID, for resume
    prepped    INTEGER NOT NULL DEFAULT 0,     -- draft prep turn completed
    PRIMARY KEY (team_id, phase)
);

-- Season --------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS matchups (
    week    INTEGER NOT NULL,
    team_a  TEXT NOT NULL REFERENCES league_teams (team_id),
    team_b  TEXT NOT NULL REFERENCES league_teams (team_id),
    kind    TEXT NOT NULL,                     -- regular, semifinal, final, third_place
    score_a REAL NOT NULL DEFAULT 0,
    score_b REAL NOT NULL DEFAULT 0,
    status  TEXT NOT NULL DEFAULT 'scheduled', -- scheduled, live, final
    PRIMARY KEY (week, team_a)
);

CREATE TABLE IF NOT EXISTS lineups (
    week      INTEGER NOT NULL,
    team_id   TEXT NOT NULL REFERENCES league_teams (team_id),
    slot      TEXT NOT NULL,                   -- QB, RB1, RB2, WR1, WR2, TE, FLEX, K, DEF
    player_id TEXT REFERENCES nfl_players (player_id),
    set_by    TEXT NOT NULL,                   -- agent, auto
    set_at    TEXT NOT NULL,
    PRIMARY KEY (week, team_id, slot)
);

CREATE TABLE IF NOT EXISTS waiver_claims (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id      TEXT NOT NULL REFERENCES league_teams (team_id),
    add_player   TEXT NOT NULL REFERENCES nfl_players (player_id),
    drop_player  TEXT REFERENCES nfl_players (player_id),
    bid          INTEGER NOT NULL,
    priority     INTEGER NOT NULL,             -- the team's own order among equal bids, 1 = first
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending, won, lost, invalid, cancelled
    note         TEXT,
    created_at   TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    team_id   TEXT NOT NULL,
    kind      TEXT NOT NULL,                   -- add, drop, trade, ir, activate
    player_id TEXT,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_team    TEXT NOT NULL REFERENCES league_teams (team_id),
    to_team      TEXT NOT NULL REFERENCES league_teams (team_id),
    give         TEXT NOT NULL,                -- JSON list of player_ids from_team sends
    receive      TEXT NOT NULL,                -- JSON list of player_ids from_team gets
    message      TEXT,
    status       TEXT NOT NULL DEFAULT 'open', -- open, accepted, rejected, cancelled, expired, failed
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    responded_at TEXT,
    response     TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    from_team TEXT NOT NULL,
    to_team   TEXT,                            -- NULL = the whole league
    text      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id    TEXT NOT NULL,
    why        TEXT NOT NULL,
    started_at TEXT NOT NULL,
    seconds    REAL,
    ok         INTEGER,
    error      TEXT,
    cost_usd   REAL,
    turns      INTEGER
);

-- Scheduler jobs already done, so a tick never repeats one.
CREATE TABLE IF NOT EXISTS jobs_done (
    key     TEXT PRIMARY KEY,                  -- e.g. waivers:2026-11-11T08:00:00Z
    done_at TEXT NOT NULL,
    detail  TEXT
);

-- Every tool call any agent makes, for auditing and analysis.
CREATE TABLE IF NOT EXISTS tool_calls (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL,
    at      TEXT NOT NULL,
    tool    TEXT NOT NULL,
    args    TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    summary TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    # Several agent processes read and write at once (one MCP server per agent).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after a table was first created: (table, column, definition).
ADDED_COLUMNS = [
    ("agent_sessions", "prepped", "INTEGER NOT NULL DEFAULT 0"),
    ("league_teams", "faab", "INTEGER"),               # remaining budget; NULL until the season starts
    ("rosters", "on_ir", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in ADDED_COLUMNS:
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()
