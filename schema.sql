CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'in_progress', 'blocked', 'done')),
    due_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_progress_at TEXT
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    blocked_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocker_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (blocked_task_id, blocker_task_id),
    CHECK (blocked_task_id <> blocker_task_id)
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
CREATE INDEX IF NOT EXISTS ix_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS ix_dependencies_blocker ON task_dependencies(blocker_task_id);
CREATE INDEX IF NOT EXISTS ix_events_task_date ON task_events(task_id, local_date);
CREATE INDEX IF NOT EXISTS ix_task_labels_task ON task_labels(task_id, removed_at);
CREATE INDEX IF NOT EXISTS ix_task_attachments_task ON task_attachments(task_id, id);
