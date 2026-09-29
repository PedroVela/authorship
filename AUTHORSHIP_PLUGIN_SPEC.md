# Authorship Ledger: Claude Code plugin build spec

Version 0.1 of this spec. Target: a Claude Code plugin named `authorship` that records, protects, classifies and visualizes how a human and Claude co-develop an invention, so the human's inventive contribution can be shown later with tamper-evident, timestamped evidence.

## 0. How to use this file

You are Claude Code building this plugin. Read the whole file before writing code.

- Build phase by phase (section 10). Do not start a phase until the previous phase's acceptance tests pass.
- Items marked **VERIFY** depend on Claude Code or third-party behavior that may have changed. Before implementing one, check the current docs (`https://code.claude.com/docs/en/` for Claude Code; the provider's API reference for Jev). If reality differs from this spec, follow reality, then record the difference in `docs/DEVIATIONS.md`.
- The invariants in section 2 are non-negotiable. If a task seems to require breaking one, stop and ask the human.
- The prototype in `reference/` works and is tested. Port it; do not redesign it from scratch. Its UI strings and some tags are in Spanish; everything you ship is in English.

## 1. Purpose and non-goals

**Purpose.** Under current US practice, AI is treated as a tool and only natural persons can be inventors; the human must conceive each claimed element. The conversation where conception happens usually leaves no durable record. This plugin makes one, in a way that separates what the human originated from what Claude originated.

**Non-goals.**
- It does not replace filing. US and Paraguay are first-to-file; priority comes from the filing date.
- It does not make AI-originated ideas human. The record shows what happened, including facts unfavorable to the user.
- It does not give legal advice. All generated documents are drafts for an attorney.
- It is not published. `.authorship/` belongs in a private repository; public disclosure before filing can destroy novelty.

## 2. Invariants

1. **Capture everything.** Hooks never filter or judge. Milestones are a derived layer over the complete record.
2. **Hooks never break the session.** Every hook exits 0 on any error, logs to `.authorship/errors.log`, makes no network calls, and runs under 100 ms at p95 (the prototype measures 44 ms median, 50 ms p95).
3. **Append-only, hash-chained ledger.** Each entry stores the SHA-256 of the previous entry. Hash = `sha256(canonical_json(entry_without_hash))` with `sort_keys=True, ensure_ascii=False, separators=(",", ":")`. This formula must not change, so prototype ledgers keep verifying.
4. **Evidence and opinion are separate.** Classifier output never enters the ledger. Only human acts (prompts, notes, confirmations) and raw events do.
5. **The audited agent cannot write evidence.** Claude must not be able to edit `.authorship/`, write entries with `actor: "human"`, or forge confirmations. Section 6 lists the defenses; all of them ship.
6. **Secrets are redacted before hashing or storing.**
7. **Local-first.** Network use is limited to anchoring (opt-in) and Jev (opt-in, off by default). Nothing else leaves the machine.
8. **Hook-path code is Python 3.9+ standard library only.** The MCP server may use one pinned dependency (section 6.7). Viewer assets are vendored; no CDN at runtime.
9. **English** for all UI, docs, logs, tags and generated text.
10. **Reproducible annotations.** Every annotation stores the pinned model id, the question-set hash and the full probability vector.

## 3. Target layout

```
authorship/                         plugin root
├── .claude-plugin/plugin.json
├── hooks/hooks.json
├── .mcp.json
├── scripts/
│   ├── ledger.py                   hook entrypoint + CLI (port of reference/authorship_ledger.py)
│   ├── guard.py                    PreToolUse protection
│   ├── protocol.md                 text injected at SessionStart
│   ├── init_project.py             writes project settings, .gitignore, folders
│   ├── index.py                    builds .authorship/index.sqlite
│   ├── annotator.py                milestone + Jev worker
│   ├── jev_client.py               provider-agnostic Jev client
│   ├── questions.toml              Jev question set
│   ├── anchor.py                   OpenTimestamps / RFC 3161, detached
│   ├── viewer.py                   local server (port of reference/authorship_viewer.py)
│   ├── statusline.py
│   └── mcp_server.py               read-only MCP server
├── viewer/
│   ├── index.html  app.js  app.css
│   └── vendor/                     pinned cytoscape, elkjs, cytoscape-elk, cytoscape-expand-collapse + LICENSE files
├── skills/
│   ├── init/SKILL.md
│   ├── disclosure/SKILL.md
│   ├── review/SKILL.md
│   └── seal/SKILL.md
├── agents/inventorship-reviewer.md
├── tests/                          pytest suite + fixtures
└── docs/  README.md  THREATS.md  LEGAL-NOTES.md  DEVIATIONS.md
```

Code lives in the plugin and is referenced as `${CLAUDE_PLUGIN_ROOT}/scripts/...`. Data lives in each project at `$CLAUDE_PROJECT_DIR/.authorship/`, never in the plugin's data directory: the evidence belongs with the repository it describes.

## 4. Data model

### 4.1 Project folder

```
.authorship/
├── ledger.jsonl        evidence, append-only
├── head.json           {seq, hash} cache; rebuildable
├── state.json          transcript byte offsets per session
├── blobs/ab/<sha256>   content-addressed payloads
├── anchors/            <seq>-<hash16>.txt, .ots, .tsq, .tsr
├── annotations.jsonl   machine opinions (derived)
├── index.sqlite        query index (derived, gitignored)
├── run/                pids, viewer secret (gitignored)
└── errors.log
```

`ledger.jsonl`, `blobs/`, `anchors/` and `annotations.jsonl` are committed to the private repo. `index.sqlite` and `run/` are gitignored.

### 4.2 Ledger entry (schema v2)

Common fields: `v`, `seq`, `ts` (UTC ISO-8601, ms), `event`, `actor` (`human` | `ai` | `system`), `session`, `prev`, `hash`.

| event | actor | extra fields |
|---|---|---|
| `SessionStart` | system | `source`, `model`, `git_head`, `cwd`, `plugin_version` |
| `UserPromptSubmit` | human | `kind: "prompt"`, `tags[]`, `text` or `preview`+`blob`, `sha256` |
| `PostToolUse` / `PostToolUseFailure` | ai | `kind: "tool"`, `tool`, `outcome`, `input` (blob ref), `response` (blob ref), `error`, `file`, `file_sha_after`, `command` |
| `Stop` / `SubagentStop` | ai | `kind: "response"`, `n_blocks`, `transcript_offset`, text fields |
| `PreCompact` | system | `trigger` |
| `SessionEnd` | system | `reason`, `git_head`, `transcript` (blob ref) |
| `SessionReconciled` | system | `for_session`, `transcript` (blob ref); written when a crashed session never produced `SessionEnd` |
| `ManualNote` | human | `kind: "note"`, `author`, `tags[]`, text fields |
| `Confirm` | human | `target_seq`, `target_hash`, `annotation_id`, `decision` (`accept` \| `reject` \| `edit`), `label`, `edited_label` |
| `GuardBlock` | system | `tool`, `reason`, `command_or_path` (redacted) |
| `Anchor` | system | `anchored_seq`, `anchored_hash`, `methods[]`, `status` (`pending` \| `complete`) |

Text fields: inline `text` + `sha256` when the UTF-8 size is at most 8 KB; otherwise `preview` (first 400 chars) + `sha256` + `blob` + `bytes`.

**Compatibility.** Schema v1 entries from the prototype must verify unchanged. `verify` accepts `v: 1` and `v: 2`. Do not migrate or rewrite old entries.

### 4.3 Tags

Canonical English tags, with Spanish aliases accepted on input and normalized to English in `tags[]`. The original text is never altered.

| Tag | Meaning | Alias |
|---|---|---|
| `#idea` | Human conception | |
| `#claim` | Claim candidate | |
| `#decision` | Choice between alternatives, with reason | |
| `#problem` | Technical problem being solved | `#problema` |
| `#hypothesis` | Expected to work, not yet shown | `#hipotesis` |
| `#discard` | Failed or rejected approach | `#descarte` |
| `#stage <name>` | Opens a new stage in the map | `#etapa` |

### 4.4 Annotation (annotations.jsonl)

```json
{"id":"ann_<uuid>","ts":"...","target_seq":42,"target_hash":"...",
 "model":"jev-1.13.0","questions_hash":"<sha256 of questions.toml>",
 "answers":{"stance":{"choice":"modifies","probabilities":{"originates":0.08,"modifies":0.81}}},
 "milestones":[{"type":"conception_candidate","tier":1,"score":0.83}],
 "supersedes":"ann_<uuid or null>"}
```

Annotations are append-only too; a re-evaluation writes a new annotation with `supersedes`. The file is not hash-chained, because it is opinion.

## 5. Claude Code integration map

| Component | Role |
|---|---|
| Hooks | Capture, guard, SessionStart protocol injection and reconciliation, daemon start |
| Permissions (written by `init`) | `Edit` deny on `.authorship/**`, `ask` on settings files |
| MCP server | Read-only query tools for Claude |
| Skills | `init`, `disclosure`, `review`, `seal` |
| Subagent | `inventorship-reviewer` |
| Status line | Chain status, unsealed count, review queue size |

## 6. Components

### 6.1 plugin.json and hooks.json

`plugin.json`: `name: "authorship"`, `version`, `description`, `author`, `license`. **VERIFY** the current manifest fields with `claude plugin validate`.

`hooks/hooks.json` uses a top-level `"hooks"` key in the same shape as `settings.json`. Register:

| Event | Matcher | Command | Timeout |
|---|---|---|---|
| `SessionStart` | (none) | `ledger.py hook` then `ledger.py session-start` | 10 s |
| `UserPromptSubmit` | (none) | `ledger.py hook` | 10 s |
| `PreToolUse` | `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash` | `guard.py` | 5 s |
| `PostToolUse` | `*` | `ledger.py hook` | 10 s |
| `PostToolUseFailure` | `*` | `ledger.py hook` | 10 s |
| `Stop`, `SubagentStop` | (none) | `ledger.py hook` | 15 s |
| `PreCompact` | (none) | `ledger.py hook` | 10 s |
| `SessionEnd` | (none) | `ledger.py hook` | 15 s |

Commands are `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/<file>" <args>`. **VERIFY** that `PostToolUseFailure` exists in the installed version; if not, detect failures from `PostToolUse` responses.

`AUTHORSHIP_SKIP_TOOLS` (default `Read,Glob,Grep,LS,TodoWrite`) keeps read-only noise out of the ledger. MCP tool calls from this plugin's own server are recorded, because lookups are part of the reasoning trail.

### 6.2 SessionStart behavior

`ledger.py session-start` does three things, in order, and must finish within the timeout:

1. **Reconcile.** For every session in `state.json` with no `SessionEnd`, if its transcript still exists, store it as a blob and write `SessionReconciled`.
2. **Inject the protocol.** Print `scripts/protocol.md` to stdout; SessionStart stdout is added to Claude's context. **VERIFY** this behavior.
3. **Ensure daemons.** Start `annotator.py` and `viewer.py` detached if their pid files are stale. Never wait on them.

`scripts/protocol.md` (exact text):

```
This project records authorship for patent purposes. Follow these rules:
1. Implement what the human asks. Do not add inventive elements they did not request.
2. When you propose a new technical mechanism, label it on its own line as: "AI proposal: <one-line summary>".
3. When offering alternatives, number them, so the human's choice can be traced.
4. Never read, edit, move or delete anything under .authorship/, and never run the authorship CLI commands note, confirm or anchor.
5. You may query the authorship MCP tools to recall prior ideas, decisions and discarded approaches. Cite entries as #<seq>.
```

### 6.3 Protection (guard.py, permissions, human-only writes)

Threat: Claude, the agent being audited, alters the audit trail or impersonates the human. Five layers, all required:

1. **`permissions.deny`: `Edit(/.authorship/**)`** in project settings, written by `init`. Only `Edit(path)` rules are consulted for file writes, and they cover all file-editing tools; `Write(path)` rules are ignored. When the sandbox is enabled, `Edit` deny paths also become OS-level write denials for Bash. `init` recommends enabling it.
2. **`permissions.ask` on `Edit(/.claude/settings.json)` and `Edit(/.claude/settings.local.json)`**, so hooks and rules can't be silently removed.
3. **`guard.py` (PreToolUse).** Resolve paths with `realpath` (defeat symlinks and `..`). Block with exit code 2 and a stderr reason when:
   - a file tool targets `.authorship/`;
   - a Bash command references `.authorship`, the plugin's `ledger.py`/`anchor.py`/`viewer.py`, or the viewer port;
   - a Bash command uses `git` operations that rewrite `.authorship/` history (`filter-branch`, `filter-repo`, `reset --hard` or `checkout --` touching it, `commit --amend` on commits that touched it).

   Every block is recorded as `GuardBlock`. Allow everything else; false positives on normal work are bugs.
4. **Human-only CLI.** `note`, `confirm` and `anchor` refuse to run when the process was spawned by Claude Code's tool runner. **VERIFY** the env marker (for example `CLAUDECODE=1`) and check the parent-process chain as a second signal.
5. **Viewer write protection** (section 6.9).

### 6.4 Redaction

Port `SECRET_RE` from the prototype and extend it with Anthropic, OpenAI, Stripe, Slack, GitHub, AWS and GCP key shapes, JWTs, PEM blocks and `password=`/`secret=` assignments. Redact before hashing and before writing blobs. Tests must include a positive and a negative case per pattern.

### 6.5 Anchoring (anchor.py)

- Methods: `ots stamp` if installed; RFC 3161 via `openssl ts -query` plus an HTTP POST to `AUTHORSHIP_TSA` (default `https://freetsa.org/tsr`). Store `.tsq`/`.tsr`/`.ots` next to the anchor text file.
- Trigger: `SessionEnd` when `AUTHORSHIP_ANCHOR=1`, the `seal` skill, or `ledger.py anchor`. Always run detached so `SessionEnd` returns fast.
- An `Anchor` entry is written with `status: "pending"`. `ots upgrade` runs on later SessionStarts and writes `status: "complete"` once the Bitcoin attestation exists.
- `verify --anchors` checks every stored RFC 3161 response with `openssl ts -verify`, and every `.ots` with `ots verify` when available.

### 6.6 Index (index.py)

SQLite built from `ledger.jsonl` + `annotations.jsonl` + `Confirm` entries. It is incremental from the last indexed `seq` and fully rebuildable (`index.py --rebuild`).

Tables:
- `entries(seq PK, ts, event, actor, session, kind, tool, outcome, file, text, tags_json, hash)`
- `nodes(seq PK, ibis_type, stage, author, status, maturity)`
- `edges(src, dst, type, source)`, where `type` is one of `responds_to`, `refines`, `modifies`, `implements`, `supports`, `objects_to`, `rejects`, `supersedes`, `discards`, `derived_from`, and `source` is one of `rule`, `annotation`, `confirmed`
- `annotations(id PK, target_seq, model, questions_hash, answers_json, superseded_by)`
- `milestones(target_seq, type, tier, score, confirmed)`
- `fts` (FTS5 over text)

Lineage is a recursive CTE over `edges` with `type IN ('derived_from','modifies','refines','responds_to')`. Views must be able to show only `confirmed` edges.

### 6.7 MCP server (mcp_server.py)

Use the official Python MCP SDK (FastMCP), pinned, run with `uv run --script` using PEP 723 inline metadata. **VERIFY** the current SDK API. It runs as stdio and is declared in `.mcp.json`. **VERIFY** the minimum Claude Code version for plugin MCP servers.

Tools, all read-only, all returning `seq` and short `hash` for every item so Claude can cite:

| Tool | Args | Returns |
|---|---|---|
| `chain_status` | none | entries, head, ok, broken_at, sealed_upto, unsealed |
| `search` | `query`, `tags?`, `actor?`, `limit=20` | matching entries with previews |
| `get_node` | `seq` | entry, annotations, confirmed labels, edges |
| `lineage` | `seq`, `depth=10`, `confirmed_only=false` | ancestor subgraph with authors |
| `open_ideas` | none | ideas not adopted, superseded or discarded |
| `discarded` | none | discarded approaches with the reason entry |
| `milestones` | `since_seq?` | milestone list with tier and confirmation state |

No tool writes anything. Output is capped at 20 KB per call, with a `truncated` flag.

### 6.8 Annotator (annotator.py, jev_client.py, questions.toml)

A daemon that tails the ledger, evaluates milestones, writes `annotations.jsonl`, and re-evaluates earlier nodes when later events change their status.

**Tier 0: deterministic rules, always on.**
- Tags map to milestones (`#problem` → problem fixed, `#idea` → conception, `#decision`, `#discard`, `#claim`, `#stage`).
- **Reduction-to-practice evidence**: the first passing test command after a failing one, on files edited since the idea node they serve.
- `SessionStart`/`SessionEnd` boundaries.

**Tier 1: Jev, opt-in** (`AUTHORSHIP_JEV=1` plus an API key). On first activation, print once which content will be sent where and require the human to type `yes` in a terminal (not via Claude). Record the consent as a `ManualNote`.

`questions.toml`:

```toml
version = "milestones-v1"
model = "jev-1.13.0"          # pinned; never "latest"
state = ["entry", "previous_ai_response", "open_ideas"]

[questions.new_element]
type = "noul"
ask = "Does the human introduce a technical element absent from prior context?"

[questions.stance]
type = "choice"
options = ["originates", "extends", "modifies", "accepts", "rejects", "unrelated"]

[questions.maturity]
type = "score"
levels = ["goal", "approach", "mechanism", "operative_spec"]

[questions.response_act]
type = "choice"
on = ["Stop"]
options = ["implements_instruction", "offers_alternatives", "unprompted_mechanism", "explains", "asks"]

[questions.kills_approach]
type = "noul"
on = ["PostToolUseFailure"]

[rules]
conception_candidate = "new_element >= 0.80 and stance in [originates, modifies, rejects] and p(stance) >= 0.70 and maturity_delta >= 1"
ai_origin_flag = "stance == accepts and p >= 0.50 or response_act == unprompted_mechanism and p >= 0.50"
review_band = [0.50, 0.80]
```

Rules:
- **Asymmetric thresholds.** Labels that favor the human need 0.80 and always require confirmation. Labels against the human surface at 0.50.
- **Candidates for "which node" questions.** Jev can't extract, so candidates come from local similarity. Default: stdlib TF-IDF over human nodes. Optional: a local embedding model behind a feature flag. Send the top 20 as Choice options.
- **Stable client.** `jev_client.py` exposes `evaluate(state, questions) -> answers` with providers `typesafe` (direct) and `vercel_gateway` (with zero data retention). **VERIFY** endpoint, auth and request/response shapes against the provider docs. Tests use a fake provider with fixed outputs.
- **Offline mode.** Without a key, only Tier 0 runs. Nothing fails.
- **Calibration helper.** `annotator.py calibrate` shows about 100 entries for the human to label in the terminal, then reports precision and recall per threshold.

### 6.9 Viewer (viewer.py + viewer/)

Port the prototype server and UI to English. Keep the security properties: bind to 127.0.0.1 only, reject foreign `Host` headers, and keep the page read-only except for confirmations.

**Views** (tabs; state kept in the URL hash):

| View | Content |
|---|---|
| Stages | The prototype's lane map: one column per stage, expandable nodes, diffs, discards as dead branches |
| Reasoning | IBIS swimlanes (Issues, Positions, Arguments, Decisions) with time on the x axis. Human nodes are circles and AI nodes rounded squares; color is paired with shape. Discarded nodes are dashed. `objects_to` and `rejects` edges are dashed. Cytoscape.js with an elkjs layered layout, lanes as partitions |
| Genealogy | Select a `#claim` and walk lineage backwards, dimming everything else. Summary: count of human-originated and AI-originated ancestors, listing every AI-originated one and where the human modified it |
| Replay | A slider over `seq` that grows the graph turn by turn. Works in every graph view |
| Branches | Git-graph metaphor: an approach is a branch, adoption is a merge, a discard is a dead end with its failure entry attached |
| Review | Queue of Tier 1 suggestions in the review band. Accept, reject or edit writes a `Confirm` entry |

**Confirmation write path.** This is the only write the viewer performs.
- On start, the viewer generates a 32-byte secret, stores it in `.authorship/run/viewer.secret` (mode 0600), and opens the browser to `http://127.0.0.1:<port>/#k=<secret>`. The page moves it to `sessionStorage` and strips the hash.
- `POST /api/confirm` requires the secret in a header, an `Origin` equal to the viewer origin, and `Content-Type: application/json`. Otherwise it returns 403.
- The guard blocks Claude from reading `run/` or contacting the viewer port. `init` adds `Read(/.authorship/run/**)` to `permissions.deny`.

The existing endpoints stay: `/api/graph`, `/api/blob/<sha256>`, `/api/report.md`. Poll every 4 s, using the etag.

Vendor pinned versions of cytoscape, elkjs, cytoscape-elk and cytoscape-expand-collapse under `viewer/vendor/`, with their licenses. Views must work offline with 5,000 nodes: layout under 3 s on a laptop, panning at 30 fps or better. Fall back to the Stages view above 5,000 nodes.

### 6.10 Skills

Each skill is `skills/<name>/SKILL.md` with frontmatter `name` and `description`. **VERIFY** the current frontmatter fields and invocation naming (`/authorship:<name>`).

- **`init`**: runs `init_project.py`. It merges (never overwrites) into `.claude/settings.json`:
  - deny: `Edit(/.authorship/**)`, `Read(/.authorship/run/**)`;
  - ask: `Edit(/.claude/settings.json)`, `Edit(/.claude/settings.local.json)`.

  It also appends `.authorship/index.sqlite` and `.authorship/run/` to `.gitignore`, creates the folder, suggests enabling the sandbox, and prints a status line snippet for the user to add. **VERIFY** whether `plugin.json` `settings` can carry `permissions`; if so, ship the rules there as well.
- **`disclosure`**: drafts `authorship-exports/<date>-disclosure.md`, outside `.authorship/` because Claude cannot write there. Sections:
  1. Problem.
  2. Alternatives considered.
  3. The invention.
  4. Element table: element, origin (human / AI / mixed), citations `#seq (hash12)`.
  5. Reduction-to-practice evidence.
  6. Discarded approaches.
  7. Timeline.
  8. Chain and anchor status.

  Rules: quote human text verbatim; never strengthen or paraphrase human conception; state AI-origin elements plainly; every citation must resolve (`tests/validate_citations.py`); header "Draft for attorney review. Not legal advice."
- **`review`**: walks the review queue in the terminal and prints the viewer link. Confirmations are made by the human in the viewer, never by Claude.
- **`seal`**: runs `anchor.py` detached and reports what was anchored.

### 6.11 Subagent: agents/inventorship-reviewer.md

Purpose: for a given claim `seq`, produce a contribution analysis for the attorney. Tools: this plugin's MCP tools and `Read` only. Output order: unfavorable facts first (AI-origin elements, "accepts" stances), then human-originated elements, then gaps where the evidence is thin. Every statement cites `#seq`. It writes to `authorship-exports/`.

### 6.12 Status line (statusline.py)

Prints one line, for example `authorship ✓ 214 | 12 unsealed | 3 to review`, or `authorship ✗ broken at #57`. It reads `head.json` and `index.sqlite` only and must finish under 50 ms. **VERIFY** how a status line is configured; `init` prints the snippet rather than editing user settings.

## 7. Milestone catalogue

| Milestone | Legal relevance | Tier 0 signal | Tier 1 signal |
|---|---|---|---|
| Problem fixed | Frames the technical problem | `#problem` | move = problem |
| Conception moment | Human introduces a new technical element | `#idea` | `conception_candidate` rule |
| Maturity jump | Toward a definite, operative idea | none | Score rises one level or more on the same idea |
| Decision with reason | Choice among recorded alternatives | `#decision` | move = decision |
| Reduction-to-practice evidence | Shows the idea works | green-after-red rule | none |
| Discard with reason | Negative knowledge | `#discard` | `kills_approach` ≥ 0.80 |
| AI-origin element | Unfavorable fact; needed for credibility | "AI proposal:" line (protocol rule 2) | `ai_origin_flag` |
| Claim candidate | Goes to the attorney | `#claim` | none |

Milestones can be retroactive: an idea at `#5` gains "reduction-to-practice evidence" when a later test passes. The milestone points back to `#5`; `#5`'s own timestamp and hash never change.

## 8. Golden fixture

`tests/fixtures/qr_session/` holds the QR payment reconciliation session used throughout design, as a hook-event script that the test harness replays through `ledger.py hook`.

| seq | actor | content | expected |
|---|---|---|---|
| 1 | system | SessionStart | |
| 2 | human | `#stage Exploration` `#problem` reconciliation of QR payments takes 40 s because the bank is queried one by one | Issue |
| 3 | ai | response offering (1) batch per lot, (2) bank webhook, (3) TTL cache | 3 Positions, AI |
| 4 | human | `#idea` instead of TTL, invalidate by the statement sequence number | Position; stance `modifies` → option 3 |
| 5 | ai | Write `src/recon.py` | implements #4 |
| 6 | ai | Bash `pytest` fails | Argument con #4 |
| 7 | ai | Edit `src/recon.py` | implements #4 |
| 8 | ai | Bash `pytest` passes | reduction-to-practice evidence for #4 |
| 9 | ai | response | |
| 10 | human | note `#discard` Bloom filter on transaction IDs: false positives lose payments | discarded Position |
| 11 | human | `#stage Prototype` `#decision` long-poll the sequence endpoint every 2 s, not the webhook, because the bank does not sign webhooks | Decision; rejects option 2 |
| 12 | ai | Write `src/poller.py` | implements #11 |
| 13 | human | `#claim` method that detects the absence of new transactions by comparing a monotonic statement counter, without downloading the detail | Claim |
| 14 | system | SessionEnd | |

Expected lineage of `#13` when the edges in `tests/fixtures/qr_session/edges.json` are loaded as confirmed: `{2, 4, 11, 13}` plus AI option 3 from `#3`, reported as "AI-originated, modified by the human at #4". Expected stages: Exploration (2–10) and Prototype (11–13).

## 9. Threat model (summary for docs/THREATS.md)

| Threat | Defense | Test |
|---|---|---|
| Claude edits or deletes the ledger | `Edit` deny, guard, sandbox | guard cases; permission rules present after `init` |
| Claude writes a human note or confirmation via the CLI | human-only CLI check, guard | spawn with the Claude Code env marker → refused |
| Claude forges a confirmation through the viewer | secret + Origin + guard on the port | POST without the secret → 403; `curl` to the port blocked by the guard |
| Claude disables the hooks | `ask` on settings files; SessionStart records `plugin_version` and the hook set hash | editing the settings triggers ask; missing hooks are visible as gaps in the ledger |
| Anyone rewrites the whole chain | external anchors | recomputed chain ≠ anchored head → `verify --anchors` fails |
| Secrets end up in evidence | redaction before hashing | pattern tests |
| Invention text leaks to a third party | Jev off by default, consent note, zero data retention | no network calls with `AUTHORSHIP_JEV` unset (socket monkeypatch test) |

## 10. Phases and acceptance tests

Run `pytest -q` and `claude plugin validate ./authorship` at the end of every phase. **VERIFY** the local plugin loading flag (for example `claude --plugin-dir ./authorship`) for manual checks.

**Phase 0: scaffold and port.**
- Plugin skeleton, `hooks.json`, `ledger.py` ported from the reference, English strings, tag aliases, schema v2 fields.
- Tests:
  - Golden fixture replays and `verify` passes.
  - A prototype v1 ledger (`reference/`) verifies unchanged.
  - Editing one byte of any entry or blob fails `verify` at the right seq.
  - Malformed stdin exits 0 and logs an error.
  - Hook latency over 200 runs: p95 < 100 ms.
  - The `Stop` hook does not duplicate text on repeated calls.
  - Interrupted-turn text is picked up at the next `Stop`.

**Phase 1: protection.**
- `guard.py`, `init_project.py`, human-only CLI, `GuardBlock` entries.
- Tests:
  - A table of at least 30 commands and paths, each marked must-block or must-allow (include `sed -i`, `>`, `tee`, `mv`, `rm -rf`, `git filter-repo`, symlink tricks, `python -c open(...)`, and normal work like `pytest`, `git commit` of source files).
  - `init` is idempotent and merges existing settings without losing keys.

**Phase 2: index and MCP.**
- `index.py`, `mcp_server.py`, `.mcp.json`.
- Tests:
  - Fixture lineage query matches section 8.
  - Rebuild and incremental index produce identical rows.
  - MCP tool list has exactly the seven read-only tools.
  - Every result item carries `seq` and `hash`.
  - The 20 KB cap is respected.

**Phase 3: annotator.**
- Tier 0 rules, retroactive re-evaluation, the Jev client with a fake provider, `questions.toml`, consent flow, calibration helper.
- Tests:
  - Fixture yields the expected Tier 0 milestones, including reduction to practice at `#8` → `#4`.
  - The fake provider drives Tier 1 through each threshold band.
  - `ledger.jsonl` bytes are unchanged after the annotator runs.
  - With Jev off, zero sockets are opened.

**Phase 4: viewer.**
- English port, Reasoning, Genealogy, Replay, Branches and Review views, confirmation endpoint, vendored assets.
- Tests:
  - API tests for `/api/graph` and `/api/confirm` (403 without the secret, 403 on a foreign Origin, 200 with both, which writes a `Confirm` entry).
  - A headless browser smoke test loads every view on the fixture and on a synthetic 5,000-node ledger within the budgets.
  - The status badge shows broken at the right seq after tampering.

**Phase 5: skills, subagent, anchoring, status line.**
- Tests:
  - `disclosure` on the fixture passes `validate_citations.py`.
  - The subagent's output lists AI-origin facts before human ones.
  - `anchor.py` with a fake TSA stores `.tsq`/`.tsr` and writes a pending, then complete, `Anchor`.
  - The status line runs under 50 ms.

**Phase 6: docs.**
- `README.md` (install, init, daily use, tags), `THREATS.md`, `LEGAL-NOTES.md` (not legal advice; first-to-file; keep private; confirmations matter), `DEVIATIONS.md`.

## 11. Open items (VERIFY list)

1. Plugin manifest fields, the `settings` key, and whether it can carry permissions or a status line.
2. The `PostToolUseFailure` event and its payload fields.
3. That SessionStart stdout is injected as context, and its size limit.
4. The env marker for processes spawned by Claude Code's Bash tool.
5. Minimum Claude Code version for plugin MCP servers; the local plugin loading flag.
6. The skill frontmatter schema and invocation naming.
7. Jev: endpoint, auth, request/response schema, Choice option limit, pricing, zero-data-retention options.
8. Status line configuration location.
