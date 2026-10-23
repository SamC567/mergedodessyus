# JARVIS build log

Strict record of tasks, processes, test runs and commits for the JARVIS layer on
top of Odysseus. Newest entries at the bottom. Every test line records the exact
command scope and result; "not run" or "not verified" is stated explicitly.

Branches: `local/llama-chroma-ddg-setup` (local stack), `feature/jarvis-quest-log`
(JARVIS features, branched from it). Nothing has been pushed to any remote.

## Baseline (known before JARVIS work)

- Full suite on Windows, before any JARVIS change: 195 tests in 65 files already
  fail (Node ESM `c:` path scheme, symlink/hardlink tests, cp1252 decoding,
  missing `rg`, POSIX-only process handling). These are environmental, not
  regressions.
- `tests/test_token_cache_atomic_swap.py` (5 tests) fails only in the real
  checkout because `data/auth.json` exists (the test's `auth_manager.setup("admin")`
  needs an unconfigured instance). Passes in a clean checkout with the same code.

## 2026-10-23

### T1. Local stack: llama-server, ChromaDB, DuckDuckGo search
- llama-server (winget Vulkan build b11406), gpt-oss-20b Q4_K_M, `-c 16384 --jinja
  -ngl 99 --n-cpu-moe 11`. Verified `/slots` n_ctx=16384. Bench: 157 t/s prompt,
  27 t/s generation, 7,354/8,176 MiB VRAM.
- ChromaDB 1.5.9 server in `.venv-chroma`, data `data/chroma`, heartbeat verified.
- `search_provider` searxng -> duckduckgo (only key changed; backup
  `data/settings.json.bak-2026-10-23`). `ddgs` 9.16.0 installed.
- E2E: agent `web_search` tool call verified in logs; auto-memory write
  (memory.json + Chroma 5->7) and recall in a new session verified.
- Commits: `c486cee3` (checkpoint of pre-existing agent_loop edits),
  `db2294f8` (start/stop scripts).

### T2. Quest Log v1 (RPG layer)
- Added QuestCampaign / Quest / QuestEvent (append-only), `/api/quests`,
  `services/quests/engine.py`, `/quests` page, sidebar entry.
- Tests: `tests/test_quests_engine.py` + `tests/test_quest_routes.py`:
  16 passed.
- Regression: full suite 5,740 passed / 200 failed. All 200 accounted for by the
  baseline section above (195 pre-existing in identical baseline run; 5 token
  cache tests pass with these changes in a clean checkout).
- Browser E2E: created campaign "Build Jarvis", 3 quests, prerequisite blocking,
  completion with outcome (+25 XP), edit, sidebar navigation, 375px layout.
- Commit: `7b93903a`.

### T3. Jarvis <-> Quest Log (manage_quests agent tool)
- Input: Sam's ChatGPT memory export (pasted in chat). Saved to Claude memory
  (profile, concise/natural replies, project + privacy rules). Not imported into
  Odysseus Brain.
- Refactor: quest rules moved to `services/quests/store.py`; routes are a thin
  layer over it, so the page and the tool share one implementation. Existing 16
  quest tests unchanged and passing after the refactor.
- New tool `src/tools/quests.py` (`manage_quests`): board, next, history, plan
  (campaign + several quests, `after` prerequisites, de-duplicates), add_quest,
  add_campaign, update, start, complete (outcome required), drop, reopen,
  update_campaign. No delete action for the agent.
- Registered in: tool_execution dispatch, tool_schemas, tool_index (description,
  keyword hints, assistant always-available), tool_capabilities (WRITE_PRIVATE;
  reads/writes/aliases), cloud_privacy (private: never sent to cloud models),
  tool_security plan-mode mutators, tool_parsing aliases, agent_tools known names,
  admin_tools toggle group, chat_routes web-only block list.
- Routing: shared `is_quest_intent()` in `src/action_intents.py` drives both
  chat->agent promotion and the agent `quests` domain + rule pack.
- Live bug found and fixed: "I want to upgrade my PC ... Can you plan that out
  for me?" was classified low-signal (goal before the planning verb), so the
  tool was never offered. Added as a regression test.
- Security decision (documented in `src/tool_capabilities.py`): saved memories
  and the skills index arm the post-external approval gate in almost every chat.
  Exception added ONLY for manage_quests reads (board/next/history) and inert
  creates (plan/add_quest/add_campaign). complete/drop/reopen/update stay gated.
  No other tool's gating changed (manage_notes, manage_memory verified still
  gated in tests).
- Tests: `tests/test_manage_quests_tool.py` 22 tests; quest total 38 passed.
- Regression: 114 test files touching edited modules -> 43 failed / 1,406 passed;
  failure set identical to the same files on the pre-change commit (43).
- Live E2E with Qwen3.6-35B-A3B (Chat mode, web off, shell off):
  - Run 1 (pre-fix): tool not offered (low_signal). Model tried manage_memory,
    left at approval prompt (not approved).
  - Run 2: tool offered, plan call stopped at approval (gate armed by memories).
  - Run 3: plan executed without prompts, created campaign "PC Upgrade for Local
    AI Models" with 6 quests. Issues: no `after` links set; overlaps the
    existing "Upgrade PC to 64 GB RAM" quest. Rules tightened (board first,
    chain sequential steps).
  - Run 4: board read executed without prompt; model then requested a
    manage_memory search, which is gated by existing policy - left pending for
    Sam. Plan step after it NOT verified.
- Not verified live: complete via chat (would award XP for unfinished work);
  covered by unit tests only.
- Commit: `6692a6dd`.
