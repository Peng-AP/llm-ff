"""SQLite storage. One file, data/league.db. NFL reference data lives in nfl_* tables;
league state (rosters, lineups, transactions) will get its own tables alongside."""

import sqlite3
from pathlib import Path

from aiffl.config import ROOT

DB_PATH = ROOT / "data" / "league.db"

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
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
