# t_4966a660: Kanban Worker Gateway Approval Flow

## Status
Proposed

## Summary
Route kanban worker permission approvals through the gateway notification system
(Discord/Telegram/Zulip) instead of surfacing `pending_approval` to the LLM.
Workers block on a shared file until the user resolves via chat.

## Problem
When a kanban worker subprocess hits a dangerous command, the current flow:
1. Returns `{"approved": False, "status": "approval_required"}` to the LLM
2. The LLM sees "approval_required" and may retry, rephrase, or get confused
3. No user notification — the task sits silently blocked

## Solution
New approval flow using shared file + gateway notification:
1. Worker writes pending approval to `~/.hermes/kanban/pending_approvals/<task_id>.json`
2. Gateway notifier sees the file, sends embed+buttons to user's chat
3. User replies `/approve` or `/deny`
4. Gateway writes `status: "resolved"` to the file
5. Worker polls the file (up to 300s timeout), then proceeds

## Files Modified
- `tools/approval.py` — new functions + integration in `_run_approval_gate()` and `check_all_command_guards()`

## Bugs Found
- `_await_kanban_permission_approval()` has no return on timeout path → returns `None` → caller crashes on `.get()`
- Fix: add `return {"resolved": False, "choice": "deny", "reason": "timeout waiting for gateway approval"}`

## Testing
- Unit tests for timeout, approve, deny, file corruption paths
- Integration test: worker → file → gateway → user → file → worker

## Rollout
1. Fix bug + add tests in project folder
2. Commit to fork branch `feat/kanban-approval-flow`
3. Open PR for review
4. Merge only after approval
