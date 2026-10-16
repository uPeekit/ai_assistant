-- Reminders set by message (app/reminders/store.py). due_at is UTC ISO; a repeating one moves
-- due_at to its next occurrence after each send, and start_at keeps its first local time.
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  event_id INTEGER,
  text TEXT NOT NULL,
  due_at TEXT NOT NULL,
  start_at TEXT NOT NULL,
  repeat TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  sent_at TEXT,
  cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reminders_pending ON reminders (cancelled, sent_at, due_at);
-- What reminder rules (data/reminder_rules.json) already sent: one row per rule, item and the
-- moment it was due, so a restart never sends one twice and a moved meeting fires again.
CREATE TABLE IF NOT EXISTS reminder_fired (
  rule_id TEXT NOT NULL,
  item_key TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (rule_id, item_key, fire_at)
);
