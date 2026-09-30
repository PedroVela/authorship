# Authorship Ledger

A Claude Code plugin that records how you and Claude co-develop an invention, as tamper-evident, timestamped evidence of what you conceived and what Claude proposed.

Every prompt, tool call and response goes into a hash-chained ledger in your project. A guard keeps Claude from editing that ledger or writing entries in your name. A local viewer shows the reasoning as stages, IBIS swimlanes, claim genealogy and branches. Drafts for your attorney cite every element to a ledger entry.

> Drafts are for attorney review. This plugin does not give legal advice and does not replace filing. See [docs/LEGAL-NOTES.md](docs/LEGAL-NOTES.md).

## Requirements

- Claude Code 2.1.281 or later (plugin MCP servers)
- `python3` 3.9 or later (hooks use the standard library only)
- [`uv`](https://docs.astral.sh/uv/) for the MCP server (it fetches the pinned `mcp` package and a Python 3.10+ on first run)
- Optional: `openssl` for RFC 3161 timestamps, [`ots`](https://github.com/opentimestamps/opentimestamps-client) for OpenTimestamps

## Install

From a local checkout, for one session:

```bash
claude --plugin-dir /path/to/authorship
```

Or through the bundled marketplace (the repository root holds `.claude-plugin/marketplace.json`):

```text
/plugin marketplace add /path/to/repo
/plugin install authorship@authorship-dev
```

## Set up a project

In the project you want to record, run:

```text
/authorship:init
```

It creates `.authorship/`, merges these rules into `.claude/settings.json` (existing keys are kept), and adds the derived files to `.gitignore`:

- deny `Edit(/.authorship/**)` and `Read(/.authorship/run/**)`
- ask `Edit(/.claude/settings.json)` and `Edit(/.claude/settings.local.json)`

Then:

1. Enable the sandbox (`/sandbox`), so the `Edit` deny rule also becomes an OS-level write denial for Bash.
2. Optionally add the status line snippet `init` prints, for `authorship ✓ 214 | 12 unsealed | 3 to review`.
3. Keep the repository private. Commit `.authorship/ledger.jsonl`, `blobs/`, `anchors/` and `annotations.jsonl`.

No restart is needed. `init` also starts the viewer (it opens in your browser) and the annotator, and hands Claude the authorship protocol in the same session, so everything from the next prompt on is recorded and Claude follows the rules. Later sessions get the same through the SessionStart hook.

Recording only happens in projects that have `.authorship/`.

### Try it in five minutes

1. `mkdir /tmp/demo && cd /tmp/demo && git init`, then `claude --plugin-dir /path/to/authorship` and `/authorship:init`.
2. Prompt: `#problem reconciliation takes 40 s #idea invalidate the cache by the statement sequence number; write the code and a test, then run pytest`. Watch the viewer fill in.
3. In another terminal: `python3 /path/to/authorship/scripts/cli.py install`, then `cd /tmp/demo && authorship log` shows your prompts and `authorship verify` prints `ok: N entries`.
4. Ask Claude to run `echo x >> .authorship/ledger.jsonl`: the guard answers `authorship guard: blocked` and records a `GuardBlock` entry.

## Daily use

### Tag your prompts

| Tag | Meaning | Spanish alias |
|---|---|---|
| `#idea` | Your conception | |
| `#claim` | Claim candidate | |
| `#decision` | Choice between alternatives, with the reason | |
| `#problem` | Technical problem being solved | `#problema` |
| `#hypothesis` | Expected to work, not yet shown | `#hipotesis` |
| `#discard` | Failed or rejected approach | `#descarte` |
| `#stage <name>` | Opens a new stage in the map | `#etapa` |

Example: `#stage Prototype #decision long-poll the sequence endpoint every 2 s, not the webhook, because the bank does not sign webhooks`.

Tags are normalized to English in `tags[]`; your text is stored exactly as typed (secrets redacted).

### What Claude is told

At session start Claude receives a short protocol: implement what you ask, label its own mechanisms `AI proposal: ...`, number alternatives, never touch `.authorship/`, and cite entries as `#<seq>`. Claude can query the ledger through seven read-only MCP tools (`chain_status`, `search`, `get_node`, `lineage`, `open_ideas`, `discarded`, `milestones`).

### The `authorship` command

Your side of the system is one command. Install it once, in your own terminal:

```bash
python3 /path/to/authorship/scripts/cli.py install   # writes ~/.local/bin/authorship
```

Then, from anywhere inside the project (it finds `.authorship/` the way git finds `.git/`):

```bash
authorship note "#discard Bloom filter on transaction IDs: false positives lose payments"
authorship log              # recent prompts, notes and confirmations; --all for every entry, -n N
authorship status           # authorship ✓ 214 | 12 unsealed | 3 to review
authorship verify --anchors
authorship seal             # anchor the current head now
authorship open             # the viewer, with its session secret
```

`note`, `seal` and `open` act in your name, so they refuse to run from Claude Code (including `!` commands typed inside it): use a separate terminal. `log`, `status` and `verify` are read-only and work anywhere. Tier 1 consent stays in `python3 /path/to/authorship/scripts/annotator.py consent`.

### Skills

| Skill | What it does |
|---|---|
| `/authorship:init` | Set up the project (above) |
| `/authorship:review` | List machine suggestions waiting for your confirmation; you confirm them in the viewer's Review tab |
| `/authorship:disclosure [--claim N]` | Draft `authorship-exports/<date>-disclosure.md` with an element table (human / AI / mixed) and validated citations |
| `/authorship:seal` | Anchor the current head to an RFC 3161 timestamp authority (and OpenTimestamps when installed) |

The `inventorship-reviewer` subagent writes a contribution analysis for one claim, unfavorable facts first.

### Viewer

The viewer starts in the background at session start, on `127.0.0.1` only, and opens your browser once. Tabs: Stages, Reasoning (IBIS swimlanes), Genealogy (a claim's lineage, with the count of human- and AI-originated ancestors), Replay (a slider over the ledger), Branches, and Review. Human nodes are circles and AI nodes rounded squares. The Review tab is the only place that writes, and only with the session secret.

## Machine annotation

- **Tier 0**, always on, offline: tags become milestones; the first passing test run after a failing one, on files edited since an idea, becomes reduction-to-practice evidence for that idea; `AI proposal:` lines become AI-origin elements.
- **Tier 1**, opt-in: [Jev](https://docs.typesafe.ai/) classifies stance, novelty and maturity. Enable with `AUTHORSHIP_JEV=1` plus `TYPESAFE_API_KEY` (or `AUTHORSHIP_JEV_PROVIDER=vercel_gateway` with `AI_GATEWAY_API_KEY`, zero data retention), then record consent with `annotator.py consent`. Labels that favor you need 0.80 and your confirmation; labels against you surface at 0.50.

Annotations are opinions: they live in `annotations.jsonl`, never in the ledger.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `AUTHORSHIP_SKIP_TOOLS` | `Read,Glob,Grep,LS,TodoWrite` | Tools not recorded |
| `AUTHORSHIP_ANCHOR` | unset | `1`: anchor at every session end |
| `AUTHORSHIP_TSA` | `https://freetsa.org/tsr` | RFC 3161 authority; `off` to disable |
| `AUTHORSHIP_OTS` | `1` | `0`: skip OpenTimestamps |
| `AUTHORSHIP_JEV` | unset | `1`: enable Tier 1 |
| `AUTHORSHIP_JEV_PROVIDER` | `typesafe` | or `vercel_gateway` |
| `AUTHORSHIP_VIEWER_PORT` | `47291` | First port tried |
| `AUTHORSHIP_VIEWER_OPEN` | `1` | `0`: do not open the browser |
| `AUTHORSHIP_NO_DAEMONS` | unset | `1`: do not start the annotator and viewer |
| `AUTHORSHIP_AUTHOR` | `$USER` | Author name on notes |

## Files

```
.authorship/
├── ledger.jsonl        evidence, append-only, hash-chained
├── head.json           {seq, hash} cache
├── state.json          transcript offsets per session
├── blobs/ab/<sha256>   content-addressed payloads
├── anchors/            <seq>-<hash16>.txt/.tsq/.tsr/.ots
├── annotations.jsonl   machine opinions (derived)
├── index.sqlite        query index (derived, gitignored)
├── run/                pids, viewer port and secret (gitignored)
└── errors.log
```

Entry hash: `sha256(canonical_json(entry_without_hash))` with sorted keys, `ensure_ascii=False` and compact separators. Schema v1 (prototype) and v2 entries both verify.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install pytest playwright
.venv/bin/python -m pytest -q authorship/tests
claude plugin validate ./authorship
```

More: [docs/THREATS.md](docs/THREATS.md), [docs/DEVIATIONS.md](docs/DEVIATIONS.md).
