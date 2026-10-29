import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  published_at TEXT,            -- ISO date YYYY-MM-DD
  body TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',   -- new | done | skipped | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  occurred_at TEXT NOT NULL,    -- ISO date
  attacker TEXT DEFAULT '',
  victim TEXT DEFAULT '',
  damage TEXT DEFAULT '',
  category TEXT DEFAULT '',
  summary TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS incident_articles (
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  PRIMARY KEY (incident_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_incidents_date ON incidents(occurred_at DESC);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
