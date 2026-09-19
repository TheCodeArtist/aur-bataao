CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rank_key INTEGER,
    -- Retained temporarily so existing databases can migrate former subtasks.
    parent_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'in_progress', 'done')),
    due_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_progress_at TEXT,
    create_request_id TEXT
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    blocked_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocker_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (blocked_task_id, blocker_task_id),
    CHECK (blocked_task_id <> blocker_task_id)
);

CREATE TABLE IF NOT EXISTS task_waiting (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    person_name TEXT NOT NULL CHECK (length(person_name) BETWEEN 1 AND 100),
    note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    started_at TEXT NOT NULL,
    last_followed_up_at TEXT,
    next_follow_up_on TEXT,
    next_follow_up_time TEXT
        CHECK (
            next_follow_up_time IS NULL
            OR (
                length(next_follow_up_time) = 5
                AND next_follow_up_time GLOB '[0-2][0-9]:[0-5][0-9]'
                AND CAST(substr(next_follow_up_time, 1, 2) AS INTEGER) BETWEEN 0 AND 23
            )
        ),
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    counts_as_progress INTEGER NOT NULL DEFAULT 0 CHECK (counts_as_progress IN (0, 1)),
    occurred_at_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK (type IN ('manual', 'active_date'))
);

CREATE TABLE IF NOT EXISTS task_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('user', 'automatic')),
    added_at TEXT NOT NULL,
    removed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_task_label
    ON task_labels(task_id, label_id) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_task_waiting
    ON task_waiting(task_id) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS ix_dependencies_blocker ON task_dependencies(blocker_task_id);
CREATE INDEX IF NOT EXISTS ix_waiting_next_follow_up
    ON task_waiting(next_follow_up_on) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_events_task_date ON task_events(task_id, local_date);
CREATE INDEX IF NOT EXISTS ix_task_labels_task ON task_labels(task_id, removed_at);
CREATE INDEX IF NOT EXISTS ix_task_attachments_task ON task_attachments(task_id, id);

CREATE TABLE IF NOT EXISTS llm_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
    base_url TEXT NOT NULL CHECK (length(base_url) BETWEEN 1 AND 500),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 200),
    api_key_env TEXT CHECK (api_key_env IS NULL OR length(api_key_env) BETWEEN 1 AND 100),
    timeout_seconds REAL NOT NULL DEFAULT 60
        CHECK (timeout_seconds BETWEEN 1 AND 600),
    supports_tools INTEGER NOT NULL DEFAULT 1 CHECK (supports_tools IN (0, 1)),
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_default_llm_profile
    ON llm_profiles(is_default) WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES llm_profiles(id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES llm_profiles(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'waiting_approval', 'completed', 'failed', 'cancelled')
    ),
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key_env TEXT,
    timeout_seconds REAL NOT NULL DEFAULT 60 CHECK (timeout_seconds BETWEEN 1 AND 600),
    supports_tools INTEGER NOT NULL DEFAULT 1 CHECK (supports_tools IN (0, 1)),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0)
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    message_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence)
);

CREATE TABLE IF NOT EXISTS agent_approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'executed')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE (run_id, tool_call_id)
);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_updated
    ON agent_sessions(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_agent_runs_session
    ON agent_runs(session_id, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_agent_run
    ON agent_runs(session_id)
    WHERE status IN ('running', 'waiting_approval');
CREATE INDEX IF NOT EXISTS ix_agent_messages_session
    ON agent_messages(session_id, id);
CREATE INDEX IF NOT EXISTS ix_agent_approvals_pending
    ON agent_approvals(status, created_at) WHERE status = 'pending';
