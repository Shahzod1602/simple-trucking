-- 0001: expiring, revocable bearer-token sessions (replaces never-expiring dispatchers.token)
CREATE TABLE IF NOT EXISTS sessions (
    token         TEXT PRIMARY KEY,
    dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
    created_at    TIMESTAMP NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMP NOT NULL DEFAULT now(),
    expires_at    TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_dispatcher ON sessions(dispatcher_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
