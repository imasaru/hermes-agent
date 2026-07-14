# ADR: Managed `uv` platform validation on profile `bin/` copies

**Status:** accepted (implemented locally, pending upstream merge)
**Date:** 2026-07-07
**Severity:** P1 — blocks `hermes update`, leaves `.update-incomplete` marker, and breaks interrupted-install auto-recovery
**Incident profile:** `minimac` on macOS ARM64 (`darwin`, `aarch64`)

## Handoff summary (read this first)

Hermes stores a **managed `uv` binary per `HERMES_HOME`** at `$HERMES_HOME/bin/uv`. Profiles are fully isolated `HERMES_HOME` directories (`~/.hermes/profiles/<name>/`). If a profile's `bin/` tree is copied or restored from another OS/architecture (Linux x86-64 → macOS ARM64 is the observed case), the wrong binary can retain the execute bit and pass naive existence checks. `hermes update` then crashes mid-install with:

```text
OSError: [Errno 8] Exec format error: '/Users/.../.hermes/profiles/<profile>/bin/uv'
```

**Fix shipped in this worktree:** validate the managed `uv` header against the current OS before use; delete incompatible binaries; let `ensure_uv()` reinstall via the official Astral installer. Also catch `OSError` in `update_managed_uv()` as a second line of defense.

**Do not copy `$HERMES_HOME/bin/` across platforms.** Managed binaries (`uv`, `uvx`, `tirith`) are platform-specific artifacts, not portable profile data.

---

## Context

Hermes profiles give each named instance its own `HERMES_HOME` (config, sessions, skills, gateway state, and **managed tooling** under `bin/`). The `minimac` profile on a Mac mini was actively used for `hermes update` and `/update`.

Managed `uv` resolution is intentionally single-path (`hermes_cli/managed_uv.py`):

- Lookup: `$HERMES_HOME/bin/uv` (or `uv.exe` on Windows)
- Bootstrap: Astral standalone installer with `UV_UNMANAGED_INSTALL` / `UV_INSTALL_DIR` pointed at `$HERMES_HOME/bin`
- Update path: `update_managed_uv()` runs `uv self update` during `hermes update`

Before this ADR, `resolve_uv()` only checked `is_file()` + `os.access(X_OK)`. That is insufficient when a foreign binary has the execute bit set.

## Symptom

```text
→ Updating Python dependencies...
Traceback (most recent call last):
  ...
  File "hermes_cli/managed_uv.py", line 170, in update_managed_uv
    result = subprocess.run([existing, "self", "update"], ...)
OSError: [Errno 8] Exec format error: '/Users/taka/.hermes/profiles/minimac/bin/uv'
```

Secondary effects:

1. `.update-incomplete` marker written before the failed install
2. Next `hermes` launch enters `_recover_from_interrupted_install()` and also fails if the bad `uv` is still present
3. User sees manual recovery instructions (`ensurepip` + `pip install -e '.[all]'`)

## Root cause

`file` inspection on the affected host:

| Path | Expected (macOS ARM64) | Actual |
|------|------------------------|--------|
| `~/.hermes/bin/uv` | Mach-O arm64 | Mach-O arm64 ✓ |
| `~/.hermes/profiles/minimac/bin/uv` | Mach-O arm64 | **ELF x86-64 (Linux)** ✗ |
| `~/.hermes/profiles/minimac/bin/uvx` | Mach-O arm64 | **ELF x86-64 (Linux)** ✗ |
| `~/.hermes/profiles/minimac/bin/tirith` | Mach-O arm64 | **ELF x86-64 (Linux)** ✗ |

The `minimac` profile `bin/` tree contained Linux binaries (dated 2026-07-04), almost certainly from copying/restoring profile state off a remote Linux host that shares the `minimac` name. Only the `minimac` profile was affected; other local profiles had no managed `bin/uv`.

`resolve_uv()` treated the Linux ELF as valid because it existed and was executable. macOS then refused to exec it (`errno 8`).

## Decision

**Validate managed `uv` platform compatibility at resolve time; auto-discard and reinstall on mismatch.**

Rationale:

1. **Footprint ladder:** extend existing `managed_uv.py` — no new core tools, env vars, or user-facing config.
2. **Self-healing:** `hermes update` and `_recover_from_interrupted_install()` already call `ensure_uv()` after `update_managed_uv()`; returning `None` from `resolve_uv()` triggers the existing bootstrap path.
3. **Fast check:** read the first 4 bytes (magic/header) — no subprocess probe on every lookup.
4. **Defense in depth:** `update_managed_uv()` also catches `OSError` from `subprocess.run` and discards the binary, covering edge cases the header check might miss.

### Alternatives considered

| Option | Rejected because |
|--------|------------------|
| Fall back to `PATH` / `~/.local/bin/uv` | Violates "one path, no guessing" design of `managed_uv.py`; profile isolation would silently use the wrong uv |
| Warn only, don't delete | Update still fails; user must manually intervene every time |
| `uv --version` probe in `resolve_uv()` | Slower; still need header check or exec to catch the failure |
| Exclude `bin/` from profile import/export | Good follow-up, but doesn't fix already-corrupted installs |

## Implementation

### Files changed

| File | Change |
|------|--------|
| `hermes_cli/managed_uv.py` | `_uv_binary_compatible()`, `_discard_incompatible_uv()`, validation in `resolve_uv()`, `OSError` guard in `update_managed_uv()` |
| `tests/hermes_cli/test_managed_uv.py` | Wrong-platform removal, Mach-O acceptance on Darwin, exec-format-error handling |

### Header validation rules (`_uv_binary_compatible`)

| OS | Accept |
|----|--------|
| Any | Shell scripts (`#!` shebang) — preserves test fakes and wrapper scripts |
| Darwin | Mach-O magics (32/64 LE/BE) and universal/fat binaries |
| Linux | ELF (`\x7fELF`) |
| Windows | PE (`MZ`) |

On mismatch, `_discard_incompatible_uv()` logs a warning and `unlink()`s the binary; `resolve_uv()` returns `None`.

### Call graph (unchanged, now self-healing)

```text
hermes update / _recover_from_interrupted_install
  └─ update_managed_uv()
       └─ resolve_uv()  ── incompatible? → delete → None
       └─ subprocess.run([uv, "self", "update"])  ── OSError? → delete → None
  └─ ensure_uv()
       └─ resolve_uv()  ── None → _install_uv() via Astral installer
  └─ [uv, "pip", "install", "-e", ".[all]"]
```

### Tests

```bash
scripts/run_tests.sh tests/hermes_cli/test_managed_uv.py -q
# 22 passed (includes 3 new cases for platform validation)
```

## Field recovery (manual, if fix not yet merged)

```bash
# 1. Inspect the managed uv for the active profile
file "$HERMES_HOME/bin/uv"
# macOS should show: Mach-O 64-bit executable arm64
# Linux should show: ELF 64-bit ...

# 2. Remove the wrong binary and reinstall
rm -f "$HERMES_HOME/bin/uv"
HERMES_HOME=~/.hermes/profiles/<profile> python3 -c \
  'from hermes_cli.managed_uv import ensure_uv; print(ensure_uv())'

# 3. Re-run update (or let interrupted-install recovery finish)
hermes update

# 4. Other managed binaries in bin/ (not auto-fixed by managed_uv)
file "$HERMES_HOME/bin/"*
# If tirith/uvx are also foreign ELF/PE, copy from a healthy same-platform install:
cp ~/.hermes/bin/tirith "$HERMES_HOME/bin/tirith"   # example
```

If `.update-incomplete` persists after a successful dep install, the next healthy `hermes` launch clears it automatically.

## Verified on incident host

After applying the fix to `minimac`:

1. Incompatible Linux `uv` removed; `ensure_uv()` installed `uv 0.11.26 (aarch64-apple-darwin)`
2. `hermes update` progressed past "Updating Python dependencies..."
3. `hermes` launched cleanly (`Up to date`)
4. `tirith` manually replaced from `~/.hermes/bin/tirith` (see follow-ups — not covered by `managed_uv`)

## Prevention guidance for future Hermes work

### Do

- Treat `$HERMES_HOME/bin/{uv,uvx,tirith}` as **platform-local build artifacts**, rebuilt per host
- On profile clone/import/restore flows, either:
  - **Exclude `bin/`** from the archive and re-bootstrap managed tools on first launch, or
  - Run the same platform validation for every executable in `bin/` after restore
- Add a `hermes doctor` check (follow-up) that flags foreign-format binaries under `$HERMES_HOME/bin/`
- Document in profile export/import UX: "managed binaries are not portable across OS/arch"

### Don't

- `rsync -a` or `scp -r` a profile's `bin/` directory between Linux and macOS
- Assume `os.access(X_OK)` means the binary is runnable on this kernel
- Copy only `uv` without checking `uvx` / `tirith` when repairing a profile

### Related code to read before changing this area

- `hermes_cli/managed_uv.py` — single-path uv ownership
- `hermes_cli/main.py` — `_cmd_update_impl()`, `_recover_from_interrupted_install()`
- `hermes_constants.get_hermes_home()` — profile-aware paths
- `scripts/install.sh` — initial managed uv bootstrap at install time
- `hermes_cli/config.py` — `security.tirith_path` (defaults to `"tirith"`, resolved via PATH / `$HERMES_HOME/bin`)

## Follow-ups (not in scope of this ADR)

| Item | Why |
|------|-----|
| `hermes doctor` bin/platform audit | Surfaces the problem before update, especially after profile import |
| Profile export/import excludes `bin/` or re-bootstraps | Stops the corruption class at the source |
| Shared `_binary_compatible()` for `tirith` | `tirith` is not managed by `managed_uv.py`; wrong ELF fails silently when `tirith_fail_open: true` |
| Pre-update backup should warn if `bin/uv` is foreign | Backups faithfully preserve the bad state (observed: pre-update zip included the Linux uv) |

## References

- User report: `hermes update` / `/update` failure on 2026-07-07, `minimac` profile, macOS ARM64
- Prior art: `docs/rca-ssl-cacert-post-git-pull.md` (similar "partial/broken install state" RCA format)
- Design intent: `hermes_cli/managed_uv.py` module docstring ("one path, no guessing")