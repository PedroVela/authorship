# Authorship Ledger

A Claude Code plugin that records how you and Claude co-develop an invention, as tamper-evident, timestamped evidence of what you conceived and what Claude proposed.

Every prompt, tool call and response goes into a hash-chained ledger in your project. A background classifier labels each entry (problem, idea, decision, claim, discard; what it builds on; the stage of the work), so nothing has to be tagged. A guard keeps Claude from editing that ledger or writing entries in your name. A local viewer shows the reasoning as stages, IBIS swimlanes, claim genealogy and branches. Drafts for your attorney cite every element to a ledger entry.

This is the reference. New here? Start with the [project README](../README.md): what it is for, three steps to start, and fixes for common problems.

> Drafts are for attorney review. This plugin does not give legal advice and does not replace filing. See [docs/LEGAL-NOTES.md](docs/LEGAL-NOTES.md).

## Requirements

- Claude Code 2.1.281 or later (plugin MCP servers)
- `python3` 3.9 or later (hooks use the standard library only)
- [`uv`](https://docs.astral.sh/uv/) for the MCP server (it fetches the pinned `mcp` package and a Python 3.10+ on first run)
- Optional: `openssl` for RFC 3161 timestamps, [`ots`](https://github.com/opentimestamps/opentimestamps-client) for OpenTimestamps

## Install

From GitHub, once, with `<owner>/authorship` being the repository that holds this plugin (if it is private, your git credentials must reach it):

```text
/plugin marketplace add <owner>/authorship
/plugin install authorship@authorship-dev
```

Updates: `/plugin marketplace update authorship-dev`. To try a local checkout for one session instead: `claude --plugin-dir /path/to/authorship`.

Then put your command on the PATH, in your own terminal:

```bash
python3 ~/.claude/plugins/cache/authorship-dev/authorship/*/scripts/cli.py install   # installed from GitHub
python3 /path/to/checkout/authorship/scripts/cli.py install                            # or from a local checkout
```

In a git repository that is not recording yet, Claude mentions `/authorship:init` once (set `AUTHORSHIP_HINT=0` to turn that off).

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

1. With the plugin installed (above): `mkdir /tmp/demo && cd /tmp/demo && git init && claude`, then `/authorship:init`.
2. Prompt: `#problem reconciliation takes 40 s #idea invalidate the cache by the statement sequence number; write the code and a test, then run pytest`. Watch the viewer fill in.
3. In another terminal: `cd /tmp/demo && authorship log` shows your prompts, and `authorship verify` prints `ok: N entries`.
4. Ask Claude to run `echo x >> .authorship/ledger.jsonl`: the guard answers `authorship guard: blocked` and records a `GuardBlock` entry.

## Daily use

### Tags (optional)

The classifier labels entries on its own (see [Automatic classification](#automatic-classification)). A tag you type overrides it for that entry:

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

Tags are normalized to English in `tags[]`; your text is stored exactly as typed (secrets redacted). Tags set by the classifier live in the index, not the ledger, and show as `#idea~` in `authorship log`.

### What Claude is told

At session start Claude receives a short protocol: implement what you ask, label its own mechanisms `AI proposal: ...`, number alternatives, never touch `.authorship/`, and cite entries as `#<seq>`. Claude can query the ledger through seven read-only MCP tools (`chain_status`, `search`, `get_node`, `lineage`, `open_ideas`, `discarded`, `milestones`).

### The `authorship` command

Your side of the system is one command, `authorship`, installed once with `cli.py install` (see [Install](#install); it writes `~/.local/bin/authorship`). Then, from anywhere inside the project (it finds `.authorship/` the way git finds `.git/`):

```bash
authorship note "#discard Bloom filter on transaction IDs: false positives lose payments"
authorship log              # recent prompts, notes and confirmations; --all for every entry, -n N
authorship status           # authorship ✓ 214 | 12 unsealed | 3 to review, plus the classifier's state
authorship review           # decide the labels the classifier was unsure of; --all to correct any automatic one
authorship verify --anchors
authorship seal             # anchor the current head now
authorship open             # the viewer, with its session secret
authorship classifier       # who labels the entries, where the text goes, how to change it (--test checks it)
authorship restart          # restart the annotator and viewer, to pick up changed settings
```

`note`, `review`, `seal`, `open`, `restart` and `classifier --test` act in your name, so they refuse to run from Claude Code (including `!` commands typed inside it): use a separate terminal. `log`, `status` and `verify` are read-only and work anywhere. Tier 1 consent is recorded with `python3 <plugin>/scripts/annotator.py consent`, where `<plugin>` is the folder `cli.py` lives in, minus `scripts/`.

### Skills

| Skill | What it does |
|---|---|
| `/authorship:init` | Set up the project (above) |
| `/authorship:review` | List machine suggestions waiting for your confirmation; you decide them with `authorship review` (or the viewer's Review tab) |
| `/authorship:disclosure [--claim N]` | Draft `authorship-exports/<date>-disclosure.md` with an element table (human / AI / mixed) and validated citations |
| `/authorship:seal` | Anchor the current head to an RFC 3161 timestamp authority (and OpenTimestamps when installed) |

The `inventorship-reviewer` subagent writes a contribution analysis for one claim, unfavorable facts first.

### Viewer

The viewer starts in the background at session start, on `127.0.0.1` only, and opens your browser once (`authorship open` brings it back). Four tabs, in plain words:
- **Overview**: what you invented. Each claim, the elements it rests on, and who contributed each: you, Claude, or you changing Claude's proposal.
- **Timeline**: what happened, in order, by stage.
- **Map**: how the ideas connect, with a slider to replay the record.
- **Review**: labels to check.

You are a blue circle and Claude an orange square. Review answers are the only writes, and only with the session secret. Full guide with screenshots: [docs/VIEWER.md](docs/VIEWER.md).

## Automatic classification

On by default, as soon as the project is initialized. Full description: [docs/CLASSIFICATION.md](docs/CLASSIFICATION.md).

- **What it does.** Each new prompt, note and reply is classified in the background by the annotator. It finds the kind (problem, idea, hypothesis, decision, claim, discard, instruction), a new technical element, the stance toward an earlier element, its parents, maturity, and a change of stage. The answers become tags, stages, milestones and lineage edges.
- **Backends.** The default is Claude, through the `claude` command and your existing login: no setup, and no new party receives the text. Jev takes over when `TYPESAFE_API_KEY` or `AI_GATEWAY_API_KEY` is set: faster, with measured probabilities, but a new party receives the text.
- **Thresholds.** A label in your favor counts automatically from 0.80 and waits in `authorship review` between 0.50 and 0.80. A label against you (an AI-origin element) counts from 0.50.
- **Where it goes.** Answers go to `annotations.jsonl`, with the backend, the model id, a prompt hash and every confidence. They never go to the ledger.
- **Setup.** `authorship classifier` shows the backend in use and the steps to change it. In short: set `TYPESAFE_API_KEY` or `AI_GATEWAY_API_KEY` in your shell profile for Jev, then run `authorship restart`. Full steps: [CLASSIFICATION.md](docs/CLASSIFICATION.md#setting-it-up).
- **Corrections.** A typed tag wins over the classifier. `authorship review --all` rejects or edits any automatic label, as a human `Confirm` entry.

Deterministic rules run alongside it, offline:

- reduction-to-practice evidence: the first passing test after a failing one, on files edited since an idea;
- `AI proposal:` lines in replies become AI-origin elements;
- numbered alternatives become one AI option each;
- session boundaries.

The spec's Tier 1 mode (`AUTHORSHIP_JEV=1` with `scripts/questions.toml`) remains for calibration. Its labels always wait for confirmation, and it needs a consent typed with `annotator.py consent`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `AUTHORSHIP_SKIP_TOOLS` | `Read,Glob,Grep,LS,TodoWrite` | Tools not recorded |
| `AUTHORSHIP_ANCHOR` | unset | `1`: anchor at every session end |
| `AUTHORSHIP_TSA` | `https://freetsa.org/tsr` | RFC 3161 authority; `off` to disable |
| `AUTHORSHIP_OTS` | `1` | `0`: skip OpenTimestamps |
| `AUTHORSHIP_AUTO` | `1` | `0`: no automatic classification (tags and rules still work) |
| `AUTHORSHIP_AUTO_BACKEND` | chosen | `claude` or `jev`; by default Jev when a Jev key is set, else Claude |
| `AUTHORSHIP_AUTO_MODEL` | `claude-sonnet-5` | Model for the Claude backend |
| `TYPESAFE_API_KEY` / `AI_GATEWAY_API_KEY` | unset | Jev key; setting one switches the classifier to Jev |
| `AUTHORSHIP_JEV_PROVIDER` | by key | `typesafe` or `vercel_gateway` |
| `AUTHORSHIP_JEV` | unset | `1`: also run the spec's Tier 1 questions (calibration) |
| `AUTHORSHIP_VIEWER_PORT` | `47291` | First port tried |
| `AUTHORSHIP_VIEWER_OPEN` | `1` | `0`: do not open the browser |
| `AUTHORSHIP_NO_DAEMONS` | unset | `1`: do not start the annotator and viewer |
| `AUTHORSHIP_AUTHOR` | `$USER` | Author name on notes |
| `AUTHORSHIP_HINT` | `1` | `0`: never suggest `/authorship:init` in uninitialized repositories |

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

More: [docs/VIEWER.md](docs/VIEWER.md), [docs/CLASSIFICATION.md](docs/CLASSIFICATION.md), [docs/THREATS.md](docs/THREATS.md), [docs/DEVIATIONS.md](docs/DEVIATIONS.md), and troubleshooting in the [project README](../README.md#if-something-goes-wrong).
