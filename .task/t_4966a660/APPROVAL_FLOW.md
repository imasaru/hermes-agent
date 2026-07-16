# Approval Flow Architecture

## Overview
This document describes the kanban worker permission approval flow in `tools/approval.py`.

## Components

### 1. `_forward_kanban_permission_approval(task_id, approval_data)`
- Writes pending approval JSON to `~/.hermes/kanban/pending_approvals/<task_id>.json`
- Appends kanban comment + event to task history
- Payload: `{status: "pending", task_id, timestamp, approval: {...}}`

### 2. `_await_kanban_permission_approval(task_id, timeout_seconds=300)`
- Polls the pending file every 1s until resolved or timeout
- Sends activity heartbeats to prevent gateway from killing the worker
- Returns `{"resolved": True/False, "choice": "approve"/"deny", "reason": ...}`
- **BUG**: No return on timeout path (returns `None`)

### 3. `_resolve_kanban_permission_approval(task_id, choice, reason)`
- Called by gateway when user responds `/approve` or `/deny`
- Writes `status: "resolved"` + choice + reason to the pending file
- Appends kanban comment + event for audit trail

### 4. `resolve_kanban_permission_approvals(choice, reason)`
- Batch resolver: scans all pending files, resolves them
- Used by gateway `/approve` command

### 5. `_get_kanban_approval_dir()`
- Returns `~/.hermes/kanban/pending_approvals/`
- Configurable via `HERMES_KANBAN_WORKSPACES_ROOT` env var

## Integration Points

### `_run_approval_gate()` (line ~2132)
When `HERMES_KANBAN_TASK` env var is set:
- Adds `task_id` to approval_data
- Routes through `_forward_kanban_permission_approval()` + `_await_kanban_permission_approval()`
- Worker blocks until user resolves — never sees `pending_approval`

### `check_all_command_guards()` (line ~3064)
Same pattern for the fallback path (no gateway callback registered).

## Data Flow
```
Worker subprocess
  │
  │ HERMES_KANBAN_TASK=t_xxxxx set
  ▼
Dangerous command detected
  │
  ▼
_forward_kanban_permission_approval()
  │
  ├─► Writes pending file
  │
  └─► Appends kanban comment
  │
  ▼
_await_kanban_permission_approval()
  │
  │ polls file every 1s
  ▼
Gateway notifier detects pending file
  │
  ├─► Sends embed+buttons to chat
  │
  ▼
User replies /approve or /deny
  │
  ▼
_resolve_kanban_permission_approval()
  │
  ├─► Writes resolved file
  │
  └─► Appends kanban event
  │
  ▼
Worker sees resolved file, proceeds
```

## Timeout Behavior
- Default timeout: 300s (5 min)
- On timeout: should return `{"resolved": False, "choice": "deny", "reason": "timeout waiting for gateway approval"}`
- **Current bug**: returns `None` → caller crashes

## Security Considerations
- File-based IPC: both worker and gateway must have read/write access to the dir
- No authentication on file reads — trust the file system boundary
- Sensitive data in `approval_data` should be redacted before writing
