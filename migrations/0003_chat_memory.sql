-- What was said in each chat lately (app/conversation/memory.py): the user's message as handled
-- and the bot's reply, so a follow-up is understood. Read back for 2 hours, then deleted.
CREATE TABLE IF NOT EXISTS chat_memory (
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_memory_chat ON chat_memory (chat_id, at);
