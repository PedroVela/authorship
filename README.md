# authorship

**Proof of what you invented, and of what the AI suggested.**

When you design something with Claude, the moment you have the idea happens in a chat that nobody keeps. If you later file a patent, you may have to show that a person conceived the invention, not the AI. This Claude Code plugin keeps that record for you, automatically, as you work:

- **Everything is recorded.** Your prompts, Claude's replies, every file it edits and every test it runs go into a ledger inside your project.
- **Nobody can quietly change it.** Each entry is chained to the previous one by a hash, so editing any entry breaks the chain. Claude is blocked from touching the ledger or writing in your name. External timestamps can prove when the record existed.
- **Your ideas and Claude's ideas stay apart.** You mark your ideas with tags like `#idea` or `#claim`. The plugin tracks which elements came from you, which came from Claude, and where you changed Claude's proposal.
- **It ends in a draft for your attorney.** An invention disclosure where every element is quoted and cited to the exact ledger entry, AI-originated parts stated plainly.

> Not legal advice, and no substitute for filing: patent priority comes from the filing date. Keep any repository that holds a ledger private. See [LEGAL-NOTES](authorship/docs/LEGAL-NOTES.md).

## Start in three steps

**1. Install the plugin** (inside Claude Code; `<owner>/authorship` is the GitHub repository that holds this project):

```text
/plugin marketplace add <owner>/authorship
/plugin install authorship@authorship-dev
```

**2. Turn it on in your project** (inside Claude Code, in the project's folder):

```text
/authorship:init
```

Recording starts right away, with no restart, and a local viewer opens in your browser.

**3. Install your command** (once, in a normal terminal, outside Claude Code):

```bash
python3 ~/.claude/plugins/cache/authorship-dev/authorship/*/scripts/cli.py install
```

This gives you `authorship`, your side of the system. It works from any folder inside the project.

Requirements: Claude Code 2.1.281+, Python 3.9+, and [`uv`](https://docs.astral.sh/uv/) (for the tools Claude uses to look things up in the ledger).

## How you use it

**While you work**, tag the prompts that matter. Untagged prompts are recorded too; tags only make the important ones stand out.

```text
#problem reconciliation of QR payments takes 40 s because the bank is queried one by one
#idea instead of a TTL cache, invalidate it by the statement sequence number
#decision long-poll the sequence endpoint every 2 s, not the webhook, because the bank does not sign webhooks
#claim method that detects the absence of new transactions by comparing a monotonic statement counter
```

Other tags: `#hypothesis`, `#discard` (an approach that failed), `#stage <name>` (opens a new phase). Spanish works too: `#problema`, `#hipotesis`, `#descarte`, `#etapa`.

**In your terminal**, check what was recorded and add notes in your own name:

```text
$ authorship log
#2     09-30 19:49  you     prompt   #stage Exploration #problem reconciliation of QR payments takes 40 s ...
#4     09-30 19:49  you     prompt   #idea instead of TTL, invalidate by the statement sequence number
#10    09-30 19:49  you     note     #discard Bloom filter on transaction IDs: false positives lose payments
#11    09-30 19:49  you     prompt   #stage Prototype #decision long-poll the sequence endpoint every 2 s ...
#13    09-30 19:49  you     prompt   #claim method that detects the absence of new transactions ...

$ authorship note "#discard Bloom filter on transaction IDs: false positives lose payments"
$ authorship status
authorship ✓ 14 | 14 unsealed | 0 to review
```

**Every so often**, close the loop:

| Step | Command | What it does |
|---|---|---|
| Review | `authorship review` | The plugin suggests labels, for example "this was a conception moment" or "this idea came from Claude". You accept, reject or edit each one. Only your answers count. |
| Seal | `authorship seal` | Gets an external timestamp for the current state of the ledger, so any later rewrite is detectable. |
| Draft | `/authorship:disclosure` (in Claude Code) | Writes `authorship-exports/<date>-disclosure.md` for your attorney, with every element cited to the ledger. |

The viewer (`authorship open`) shows the same history as a map: stages, the reasoning behind each decision, and the full lineage of a claim, with your contributions and Claude's marked apart.

## If something goes wrong

| What you see | What to do |
|---|---|
| `refuses to run from Claude Code` | `note`, `review`, `seal` and `open` act in your name. Run them in a separate terminal, not through Claude and not with `!`. |
| The viewer did not open | `authorship open`. |
| Claude cannot look things up in the ledger | Install [`uv`](https://docs.astral.sh/uv/), then restart Claude Code. |
| `authorship: no .authorship/ here` | You are outside a recorded project; `cd` into it, or run `/authorship:init`. |
| `authorship verify` says `BROKEN at #N` | Entry N was changed after it was written. Do not "fix" the ledger; tell your attorney. Git history shows when it changed. |
| Claude says `authorship guard: blocked` on normal work | The guard is too strict for that command: run it yourself, and report it as a bug. |
| Anything else | Hook errors are logged in `.authorship/errors.log`; they never interrupt your session. |

To pause recording: `claude plugin disable authorship@authorship-dev` (the ledger stays; the pause shows up as a gap). To stop suggesting `/authorship:init` in other repositories: `export AUTHORSHIP_HINT=0`.

## More

- [Plugin reference](authorship/README.md): all commands, skills, settings, files and the optional AI classifier
- [Threat model](authorship/docs/THREATS.md): what the plugin protects against, and what it does not
- [Build spec](AUTHORSHIP_PLUGIN_SPEC.md) and [deviations from it](authorship/docs/DEVIATIONS.md)

Development: `python3 -m venv .venv && .venv/bin/pip install pytest playwright`, then `.venv/bin/python -m pytest -q authorship/tests` and `claude plugin validate ./authorship`.
