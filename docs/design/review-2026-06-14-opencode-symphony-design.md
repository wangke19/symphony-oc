# OpenCode Symphony Design Review

## Architecture (Section 2)

- **Overall design is solid.** The orchestrator → executor → reconciler pipeline maps cleanly to the Symphony methodology. Git worktree isolation for concurrency is the right call over a single workspace.
- **Section 2.1 flow diagram** — clear and accurate. The stall detection + retry queue loop is well thought out.
- **Section 2.4 worktree lifecycle** — creation/removal/prune flow is correct. One concern: orphan cleanup only handles `status=running` with dead PIDs. What about runs stuck in `queued` or `retrying` with stale state after a crash? Consider adding a startup scan for stale worktrees that don't appear in `runs.jsonc`.

## Data Model (Section 3)

- **Issue/Run dataclasses** — clean. `content_hash` for deduplication is a good touch for local issues.
- **JSONC flat structure (5.8)** — the shift from 3-section to single list is correct and simpler. Memory indexing via `load_running()` / `load_retry_queue()` is fine for personal-scale use (<100 runs).

## Configuration (Section 4)

- **WORKFLOW.md with YAML frontmatter** — reasonable choice. Jinja2 templating for prompts is standard.
- **Prompt file as positional message** — documented correctly (no `--prompt-file` flag exists). Passing multiline text via `subprocess` positional arg is safe (no shell interpolation).

## Core Loop (Section 5)

- **5.2 `dispatch()`** — atomic state save via tmp+rename is correct. `start_new_session=True` for process group cleanup is good practice.
- **5.3 `reconcile()`** — the "no diff → fail" guard prevents wasted runs. Two-commit split logic aligns with CLAUDE.md.
- **5.7 `should_dispatch()`** — hash-based dedup for local issues works. But there's a subtle race: if two poll cycles discover the same issue simultaneously, both could pass the check before either writes state. Consider a simple file lock or checking `runs` *after* acquiring the lock.
- **5.8 flat state** — clean. The `load_running()` filter including `queued` and `retrying` is correct for concurrency accounting.

## Permissions & Agent (Section 2.5)

- **`symphony-worker.md`** — well scoped. Bash whitelist approach is practical.
- **Key decision: not using `--dangerously-skip-permissions`** — correct. Without TTY, `ask` defaults to deny, which is the desired behavior.
- **`doom_loop: deny` as turn-limit substitute** — clever workaround for missing `--max-steps`. Documented rationale is sound.
- **Global agent install path** — justified correctly (worktree cwd isolation).

## Bootstrap & Safety (Section 6)

- **`bootstrap.py`** — comprehensive pre-flight checks. The three-layer defense against agent fall-back (L1 list check, L2 smoke test, L3 hash verification) is thorough.
- **Smoke test (6.3)** — 2s timeout is tight but sufficient for detecting "not found" / "falling back". Good that it scans for both strings in lowercase.
- **systemd user service** — correct setup. `loginctl enable-linger` note is important for persistence across logouts.

## Risks (Section 9)

- **9.1 #1 version lock** — correct assessment. Pinning to 1.17.7 and documenting in README is the right mitigation.
- **9.1 #4 silent fall-back risk** — well analyzed. The residual risk (agent file deleted during long-running orchestrator) is acceptable for MVP.
- **Missing risk: `gh` CLI auth expiry** — `gh auth token` expires. The orchestrator should verify `gh auth status` periodically or on each PR creation, not just at bootstrap.

## Minor Issues

1. **Line 491** — `run_bash(CI_COMMAND, cwd=wt, timeout=CI_TIMEOUT_MS)` — `CI_COMMAND` is a string like `"pytest -q"`. Need to ensure `run_bash` splits it properly for `subprocess` (shell=False for safety).
2. **Line 504** — Two-commit exclusion list is hardcoded. Should this be configurable in WORKFLOW.md frontmatter?
3. **Line 530** — `removeprefix("GH-")` — Python 3.9+. Document minimum Python version (6.2 says 3.11+, so fine).
4. **Retry queue** — `process_retry_queue()` is called but not implemented in the pseudocode. Should show the backoff calculation + re-dispatch flow.
5. **Error handling** — No `try/except` shown around subprocess calls. A failed `git worktree add` or killed `opencode` process should gracefully transition state rather than crash the orchestrator.

## Verdict

**LGTM for MVP.** The design is pragmatic, security-conscious (permissions, no fall-back), and well-documented. The flat state model and worktree isolation are the strongest architectural decisions. Address the race condition in `should_dispatch()` and add `gh` auth refresh before implementing.
