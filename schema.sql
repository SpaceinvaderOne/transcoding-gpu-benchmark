-- GPU Transcode Benchmark leaderboard (Cloudflare D1 / SQLite)
-- One row per (install_id, gpu, profile, hw_variant); resubmits UPDATE (keep best). Raw
-- envelope kept for audit. hw_variant (''/locked/unlocked/unknown — the NVIDIA driver lock
-- state, see nvencVariant) is part of the IDENTITY: a locked and an unlocked run of the same
-- card are separate rows, so unlocking a driver never overwrites the locked history.
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  install_id TEXT NOT NULL,
  gpu TEXT NOT NULL,
  vendor TEXT,
  profile TEXT NOT NULL,
  tool_version TEXT,
  max_sustained INTEGER,
  capped INTEGER DEFAULT 0,
  projected INTEGER,
  single_stream REAL,
  peak_combined REAL,
  watts_per_stream REAL,
  power_estimated INTEGER DEFAULT 0,
  driver TEXT,
  os_version TEXT,
  kernel TEXT,
  ram TEXT,
  cpu TEXT,
  hidden INTEGER DEFAULT 0,
  submitted_at INTEGER,
  updated_at INTEGER,
  ip_hash TEXT,
  raw TEXT,
  hw_variant TEXT NOT NULL DEFAULT '',
  -- flagged = 1 → held for review by the plausibility check; it does not publish automatically
  -- when the hold window passes. Public reads require hidden = 0 AND flagged = 0.
  flagged INTEGER NOT NULL DEFAULT 0,
  UNIQUE(install_id, gpu, profile, hw_variant)
);
CREATE INDEX IF NOT EXISTS idx_sub_profile ON submissions(profile, hidden);

-- sliding-window rate limiting (ip hashes only; raw IPs are never stored)
CREATE TABLE IF NOT EXISTS ratelimit (ip_hash TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rl ON ratelimit(ip_hash, ts);

-- moderation audit trail (one admin, so no identity column yet): every hide/restore is recorded
CREATE TABLE IF NOT EXISTS moderation_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id INTEGER NOT NULL,
  action TEXT NOT NULL,          -- hide | restore
  reason TEXT,
  created_at INTEGER NOT NULL
);
