# Deviations from the build spec

Where the spec (v0.1) and reality differed, the plugin follows reality. Each item says what changed and why. Items marked **VERIFY** in the spec were checked against Claude Code 2.1.282 and its docs (code.claude.com/docs/en/), the MCP Python SDK 2.2.0 and the Jev docs (docs.typesafe.ai).

## Inputs that were missing

1. **No `reference/` prototype.** The repository held only the spec. Everything was built from the spec instead of ported. Consequences:
   - The hash formula is exactly the spec's (`sha256(canonical_json(entry_without_hash))`, sorted keys, `ensure_ascii=False`, compact separators).
   - The genesis `prev` of the first entry is `"0" * 64`. Because the prototype's value is unknown, `verify` also accepts `null`, `""`, `"genesis"` and `"GENESIS"` for entry 1 only.
   - `tests/fixtures/v1_ledger/` is a **synthetic** schema v1 ledger (Spanish tags) written with the same formula, not a real prototype ledger. Replace it with a real one when available; the test then checks real compatibility.
   - Viewer UI is new, in English, not a port.

## Claude Code integration (VERIFY items)

2. **Plugins cannot ship permission rules or a status line** (VERIFY 1, 8). `plugin.json` `settings` only honors `agent` and `subagentStatusLine`. So `init` writes the rules into `.claude/settings.json`, and prints the `statusLine` snippet for the user to add.
3. **`PostToolUseFailure` exists** (VERIFY 2) and carries `error`. A failing Bash command arrives there. `PostToolUse` responses are also checked for `is_error`, `interrupted` and exit-code fields.
4. **SessionStart stdout is added to Claude's context** (VERIFY 3). Confirmed in a live headless session: Claude quoted rule 2 of the protocol.
5. **One SessionStart command, not two.** Hooks registered for the same event run in parallel, so "`ledger.py hook` then `ledger.py session-start`" could not be ordered. `ledger.py session-start` records the event, reconciles, prints the protocol and starts the daemons, in that order.
   - **`init` activates the current session.** The session that runs `/authorship:init` started before `.authorship/` existed, so its SessionStart hook did nothing. `init_project.py` therefore also starts the annotator and viewer and prints the protocol, which the `init` skill tells Claude to follow from then on. No restart is needed. That session has no `SessionStart` entry; its first entries are the `init` tool call and the prompts that follow.
   - **Hint in uninitialized repositories.** With the plugin installed user-wide, a `SessionStart` in a git repository without `.authorship/` prints one line asking Claude to mention `/authorship:init` once. Hinted projects are remembered in `$CLAUDE_PLUGIN_DATA/init-hints.json`; `AUTHORSHIP_HINT=0` turns it off. Nothing is recorded there.
6. **Env marker** (VERIFY 4): `CLAUDECODE=1` is set in Bash tool processes (observed in 2.1.282; not documented). `CLAUDE_CODE_ENTRYPOINT` is also treated as a marker. Side effect: `!` commands typed by the human inside Claude Code are refused too; human-only commands must run in a separate terminal.
7. **Plugin MCP servers and loading** (VERIFY 5): works on 2.1.281+; tools appear as `mcp__plugin_authorship_authorship__<tool>`; local loading is `claude --plugin-dir <path>`. A repository-level `.claude-plugin/marketplace.json` was added for `/plugin marketplace add`.
8. **Skills** (VERIFY 6): frontmatter `name`, `description`, `allowed-tools`, `disable-model-invocation`, `argument-hint`; invoked as `/authorship:<name>`. `${CLAUDE_PLUGIN_ROOT}` is substituted in the skill body and in `allowed-tools`, but is not exported to Bash, so skills spell out full script paths. `init` and `seal` are user-invoked only.
9. **Payload field names** were confirmed in a live session: `prompt` (UserPromptSubmit), `tool_response` (PostToolUse). The hook also accepts `user_message` and `tool_output`, which some docs mention.
10. **Transcript lag at Stop.** The final assistant message may not be flushed to the transcript when `Stop` fires (seen live). The hook also takes `last_assistant_message` from the payload; a per-transcript set of text hashes keeps it from being recorded twice when the transcript catches up.

## Recording and protection

11. **Recording only in initialized projects.** Hooks record only where `.authorship/` exists, so a user-scope install does not create ledgers in every repository.
12. **Guard matcher extended** to `Read|Grep|Glob` so the viewer secret under `run/` is protected by the guard as well as by the `Read` deny rule.
13. **`anchor.py seal` is allowed from Claude.** The spec makes `anchor` human-only (6.3) and has the `seal` skill run `anchor.py` through Claude (6.10). Resolution: `ledger.py anchor` stays human-only; the guard allows exactly `python3 <plugin>/scripts/anchor.py seal`, which timestamps the current head and writes only `system` entries. Every other `anchor.py` invocation from Claude is blocked.
14. **Guard `ask` decisions.** Besides blocking, the guard returns `permissionDecision: "ask"` for Bash commands that touch `.claude/settings*.json` or run `claude plugin disable/uninstall`, so the human decides.
15. **Git rules are state-aware.** `reset --hard`, `checkout`/`restore .`, and `stash` are blocked only when they would discard or move `.authorship/` content (uncommitted store changes, or a reset target that differs in `.authorship/`). `git filter-branch`/`filter-repo` are always blocked. `push --force` is not handled (remote history is out of scope; anchors cover it).
16. **Extra entry fields:** `stage` on tagged entries, `hooks_sha` on `SessionStart`, `tool_use_id` on tool entries, `agent_id`/`agent_type` on `SubagentStop`, `completed_methods`/`pending_seq`/`file` on `Anchor`, and a `consent` object on the consent `ManualNote`.

## Index, MCP and annotations

17. **Node ids are text.** `nodes` is keyed by `node_id` (with a `seq` column), because one AI response yields several positions: option `n` of response `#3` is node `3.n`, and an `AI proposal:` line is `3.p1`. Edges refer to node ids. Citations of sub-nodes read `#3.3 (hash12)`, where the hash is entry 3's.
18. **Confirmed edges come from `Confirm` entries.** Labels: `edge:<src>:<type>:<dst>`, `milestone:<type>`, `maturity:<level>`. The fixture's `edges.json` is loaded by writing one `Confirm` entry per edge, the way the viewer does, so confirmed edges stay evidence and survive rebuilds.
19. **MCP SDK 2.x** (VERIFY): `FastMCP` is now `MCPServer` (`mcp.server.MCPServer`); pinned `mcp==2.2.0`, which needs Python 3.10+ (provided by `uv run --script`). Tools carry `readOnlyHint`. Queries refresh the derived index (`index.sqlite`), which is a cache; nothing writes evidence.
20. **Jev** (VERIFY 7): endpoint `POST https://api.typesafe.ai/v1/systemone` with a bearer key; Vercel AI Gateway `POST https://ai-gateway.vercel.sh/v1/evaluate`, where the noul type is called `boolean` and zero data retention is requested with `providerOptions.gateway.zeroDataRetention`. Noul answers are a single probability. Choice questions accept up to 255 options (the plugin sends at most 21). Price at the time of writing: $0.042 per million input tokens. The gateway cannot pin a Jev version, so each annotation also stores `model_reported`.
21. **`questions.toml` additions.** Jev choice questions need a description per option, so `stance` and `response_act` gained `ask` and `criteria` tables. A `target` choice question is added at run time with the top-20 TF-IDF candidates. The spec's rules are kept verbatim and evaluated by a small expression evaluator.
22. **Status line** reads `head.json` and a chain summary the annotator caches in `index.sqlite`; before the annotator has verified the current head it shows `…` instead of `✓`.
23. **Deterministic drafts.** `scripts/drafts.py` builds the disclosure and contribution drafts from the index, so the acceptance tests do not depend on model output. The `disclosure` skill and the `inventorship-reviewer` agent start from, check and refine these drafts.
24. **The reviewer agent has `Write`.** The spec gives it MCP tools and `Read` only, but also says it writes to `authorship-exports/`. The guard and the deny rule keep it out of `.authorship/`.
25. **Anchors.** An `Anchor` entry with `status: "pending"` is written before any proof is requested; a second one with `status: "complete"` when every method has its proof (RFC 3161 immediately, OpenTimestamps after a later `upgrade`). `sealed_upto` counts complete anchors and anchors whose RFC 3161 proof arrived. For the default authority, freetsa.org's CA certificates are stored under `anchors/tsa/` so responses can be verified offline later.
26. **Structural milestones need no confirmation.** Tag-derived Tier-0 milestones (declared by the human) and session boundaries count as confirmed; reduction-to-practice evidence and all Tier-1 labels wait for the human.

## Viewer

The first build had the spec's six views (Stages, Reasoning, Genealogy, Replay, Branches, Review). In use they overlapped and spoke in jargon ("IBIS", "Genealogy"), and none answered the first question: what did I invent, and who contributed what. The viewer was redesigned around four questions (see [VIEWER.md](VIEWER.md)):

- **Overview** (new, the default) does what Genealogy's summary did, and more. Per claim it shows the elements, their origin (you, Claude, or you changing Claude), the evidence, and a bar of the share. It uses the same curated lineage as the disclosure.
- **Timeline** replaces Stages and Branches.
  - It lists entries by stage, one plain sentence each: options with their fate, discards struck through, and adoption and rejection inline.
  - "Key moments" folds Claude's routine work.
  - Rows open on full text, diff and links.
- **Map** replaces Reasoning, Genealogy and Replay.
  - It uses the same lanes in plain words.
  - It focuses on a claim's lineage by default.
  - It has a "record up to" slider and a Play button in place of the Replay tab.
- **Review** asks plain questions ("Is #11 a maturity jump: the idea became more definite?"). It can also correct labels already counted automatically.

Other changes:
- **No elkjs.** The Map places nodes itself: a column per entry in time order, a row per lane. That reads better than ELK's layering and stays fast at 5,000 nodes (0.3 s, 60 fps in the scale test). elkjs, cytoscape-elk and cytoscape-expand-collapse are no longer vendored.
- **Above 5,000 nodes** only the Map is turned off; Overview, Timeline and Review keep working.
- **Colors** follow a validated categorical palette, checked with the dataviz validator in light and dark: you slot 1 (blue), Claude slot 2 (orange), you changing Claude slot 3 (aqua). Shape and a word always go with color.
- **`/api/graph`** also carries `inventions` (per-claim elements and origin), `review_all`, and `edited_label` on confirmations. **`GET /api/entry/<seq>`** returns one entry's full text.
- **Origin header.** The page sends `Referrer-Policy: no-referrer`, so the confirm request sets `referrerPolicy: 'same-origin'` to carry a real `Origin`. The server check is unchanged.
- **Tests.** Playwright 1.60 (the last release for Python 3.9) runs on the cached headless Chromium. `wait_for_function` is blocked by the page's CSP, so tests poll with `page.evaluate`.

## Automatic classification (after the first build)

The spec makes Tier 0 depend on tags the human types, and keeps Tier 1 (Jev) opt-in, behind a typed consent, with every favorable label waiting for confirmation. In use that was too much friction: the human should only have to turn the plugin on. So:

28. **A background classifier labels every entry.** It labels human prompts and notes, and AI replies. It produces tags, stages, milestones and lineage edges with no action from the human. See [CLASSIFICATION.md](CLASSIFICATION.md). Typed tags still work and always win.
29. **Two backends, picked automatically.**
    - **Claude** (`claude -p`, Sonnet, the existing login) by default. Sonnet replaced Haiku after Haiku gave different labels on repeated runs of the golden session.
    - **Jev** when a Jev key is set. Setting the key replaces the consent note typed in a terminal (spec 6.8): the human chooses by configuring the key, and every annotation records the backend.
30. **Automatic labels count without confirmation, still asymmetric.**
    - Favorable labels count from 0.80, wait for review between 0.50 and 0.80, and are dropped below that.
    - Unfavorable labels count from 0.50.
    - A new confirmation state, `automatic` (`milestones.confirmed = 2`), sits beside confirmed (1), pending (0) and rejected (-1).
31. **A new edge source, `auto`.** The classifier's edges are stored as source `auto` and count for node status and lineage. Jev Tier 1 suggestions keep source `annotation` and still wait for confirmation. A new lineage mode, `curated` (confirmed + auto), drives the disclosure, the report and claim genealogy, leaving out the rule that links every prompt to the previous reply.
32. **Index schema `index-v2`** adds `entries.stage_tag` and `entries.auto_tags_json`, plus `annotations.method`, `auto_tags_json` and `stage`. Older indexes are rebuilt automatically.
33. **Conception follows the spec's rule,** whatever the model calls the entry. That rule is a new element plus a stance of originates, modifies or rejects. Reduction-to-practice evidence also anchors on ideas the classifier found.
34. **`authorship review --all`** lists automatic labels and links too, so the human can reject or edit any of them. `authorship status` prints the classifier's state.

35. **The classifier in use is visible.** The annotator records the backend it used in the index, with the model, where the text goes and the last error. The viewer shows it on Overview and Review, with a setup dialog. `authorship classifier` prints it and the setup steps (`--test` checks a key with one made-up sentence). `authorship restart` restarts the annotator and viewer with the terminal's settings, because the annotator outlives sessions and keeps the environment it started with. `restart` and `classifier --test` are human-only: a restart chooses where entry text is sent.

36. **Jev through OpenRouter.** Jev is also on OpenRouter's Decisions API (`POST https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`, same question format as TypeSafe). `OPENROUTER_API_KEY` selects it, after a TypeSafe key and before a Vercel one. Requests ask for zero-data-retention endpoints only (`provider.zdr`), `data_collection: deny`, and no fallback. The dated snapshot that answered is stored as the model.
37. **Version 0.2.0**, so existing installs pick up the changes since 0.1.0 on `/plugin marketplace update`.

## Process

27. Phases 4 (viewer) and 5 (skills, anchoring, status line) were built in parallel, each against its own acceptance tests, and the full suite was run before each commit.
