-- Chat Memory System — run once in Supabase SQL editor
-- Provides persistent chat sessions with full message history

CREATE TABLE IF NOT EXISTS chat_sessions (
  id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     TEXT        NOT NULL DEFAULT 'anonymous',
  session_name TEXT,
  context     JSONB       NOT NULL DEFAULT '{}',
  -- context shape: {selected_policy, budget, diseases, family_size}
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  UUID        NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
  content     TEXT        NOT NULL,
  metadata    JSONB       NOT NULL DEFAULT '{}',
  -- metadata shape: {type, policies, extracted_requirements}
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast session message retrieval (ordered by time)
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_time
  ON chat_messages(session_id, created_at ASC);

-- Index for listing sessions by recency
CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated
  ON chat_sessions(updated_at DESC);
