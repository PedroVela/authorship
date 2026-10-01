# authorship

**Proof of what you invented, and of what the AI suggested.**

When you design something with Claude, the moment you have the idea happens in a chat that nobody keeps. If you later file a patent, you may have to show that a person conceived the invention, not the AI. This Claude Code plugin keeps that record for you. You turn it on once; after that there is nothing to remember:

- **Everything is recorded.** Your prompts, Claude's replies, every file it edits and every test it runs go into a ledger inside your project.
- **Nobody can quietly change it.** Each entry is chained to the previous one by a hash, so editing any entry breaks the chain. Claude is blocked from touching the ledger or writing in your name. External timestamps can prove when the record existed.
- **Your ideas and Claude's ideas stay apart, on their own.** A classifier reads each entry in the background and works out what it is. That covers your problems, ideas, decisions and claims, the options Claude offered, and where you changed or rejected them. It also tracks when the work moved to a new stage. You never have to tag anything.
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

**While you work**, just work. Describe the problem, propose, decide, ask Claude to build and test, as you normally would. The classifier labels each entry within seconds; the tests Claude runs are recorded too. When a test passes after failing, that counts as evidence your idea works.

**In your terminal**, see what was recorded and how it was read:

```text
$ authorship log
#2     10-01 12:11  you     prompt   #problem~ reconciliation of QR payments takes 40 s because the bank is queried ...
#4     10-01 12:11  you     prompt   #idea~ instead of TTL, invalidate by the statement sequence number
#10    10-01 12:11  you     note     #discard~ Bloom filter on transaction IDs: false positives lose payments
#11    10-01 12:11  you     prompt   #decision~ long-poll the sequence endpoint every 2 s, not the webhook, ...
#13    10-01 12:11  you     prompt   #claim~ method that detects the absence of new transactions by comparing ...
(#tag~ = set automatically by the classifier)

$ authorship status
authorship ✓ 14 | 14 unsealed | 0 to review
classifier: on (claude-cli)
```

Add a note in your own name when something happened off the chat, for example an approach you tried and dropped: `authorship note "Bloom filter on transaction IDs: false positives lose payments"`.

**If you want to steer it**, type a tag: `#idea`, `#claim`, `#decision`, `#problem`, `#hypothesis`, `#discard`, `#stage <name>`. Spanish works too: `#problema`, `#hipotesis`, `#descarte`, `#etapa`. Your tag always wins over the classifier. Tags are optional.

**Every so often**, close the loop:

| Step | Command | What it does |
|---|---|---|
| Review | `authorship review` | Labels in your favor that the classifier was unsure of (0.50 to 0.80) wait here; accept, reject or edit each one. `--all` also lets you correct the ones already counted. |
| Seal | `authorship seal` | Gets an external timestamp for the current state of the ledger, so any later rewrite is detectable. |
| Draft | `/authorship:disclosure` (in Claude Code) | Writes `authorship-exports/<date>-disclosure.md` for your attorney, with every element cited to the ledger. |

### The viewer

`authorship open` shows the record in your browser, in four tabs:
- **Overview**: what you invented, and who contributed each element.
- **Timeline**: what happened, in order.
- **Map**: how the ideas connect, with a slider to replay how the record grew.
- **Review**: labels to check.

![Overview: a claim, the elements it rests on, and who contributed each](authorship/docs/images/viewer-overview.png)

Guide: [docs/VIEWER.md](authorship/docs/VIEWER.md).

### Who classifies, and what it sees

By default the classifier is Claude itself, run in the background through the `claude` command with your existing login. No setup is needed, and the text goes to no one new: Claude Code already sends it to Anthropic. A 7-entry session takes about 16 s and US$0.03.

If you set a [Jev](https://docs.typesafe.ai/) key (`TYPESAFE_API_KEY`, or `AI_GATEWAY_API_KEY` for the Vercel AI Gateway), Jev is used instead. It is faster and cheaper, and returns measured probabilities. But it sends the text of your invention to a new party, so set the key only if that is acceptable before filing.

To switch to Jev:

```bash
echo 'export TYPESAFE_API_KEY=your-key' >> ~/.zshrc   # or AI_GATEWAY_API_KEY; then open a new terminal
authorship restart                                    # the annotator picks up the new settings
authorship classifier --test                          # confirms which backend answers
```

`authorship classifier` shows what is in use at any time, and so does the viewer, in a strip on Overview and Review with a **How to change it** link.

The record leans against you, never for you:
- A label in your favor counts only from 0.80 confidence.
- A label against you (an idea that came from Claude) counts from 0.50.
- Labels are opinions: they never enter the ledger, and you can reject any of them.

Details: [docs/CLASSIFICATION.md](authorship/docs/CLASSIFICATION.md).

## If something goes wrong

| What you see | What to do |
|---|---|
| `refuses to run from Claude Code` | `note`, `review`, `seal` and `open` act in your name. Run them in a separate terminal, not through Claude and not with `!`. |
| The viewer did not open | `authorship open`. The page explains itself under "How to read this"; the full guide is [docs/VIEWER.md](authorship/docs/VIEWER.md). |
| Claude cannot look things up in the ledger | Install [`uv`](https://docs.astral.sh/uv/), then restart Claude Code. |
| `authorship: no .authorship/ here` | You are outside a recorded project; `cd` into it, or run `/authorship:init`. |
| `authorship verify` says `BROKEN at #N` | Entry N was changed after it was written. Do not "fix" the ledger; tell your attorney. Git history shows when it changed. |
| Claude says `authorship guard: blocked` on normal work | The guard is too strict for that command: run it yourself, and report it as a bug. |
| The classifier is not running, or uses the wrong backend | `authorship classifier` says why and what this terminal would use. Usually the `claude` command is not on the PATH, `AUTHORSHIP_AUTO=0` is set, or a key was added after the annotator started: run `authorship restart`. Unclassified entries are picked up later. |
| The classifier read an entry wrong | Type the right tag next time, or run `authorship review --all` and reject or edit the label. |
| Anything else | Hook errors are logged in `.authorship/errors.log`; they never interrupt your session. |

To pause recording: `claude plugin disable authorship@authorship-dev` (the ledger stays; the pause shows up as a gap). To keep recording but stop the classifier: `export AUTHORSHIP_AUTO=0`. To stop suggesting `/authorship:init` in other repositories: `export AUTHORSHIP_HINT=0`.

## More

- [Plugin reference](authorship/README.md): all commands, skills, settings and files
- [Automatic classification](authorship/docs/CLASSIFICATION.md): what is asked, which backend answers, thresholds, how to correct it
- [The viewer](authorship/docs/VIEWER.md): the four tabs, the symbols, and how answers are recorded
- [Threat model](authorship/docs/THREATS.md): what the plugin protects against, and what it does not
- [Build spec](AUTHORSHIP_PLUGIN_SPEC.md) and [deviations from it](authorship/docs/DEVIATIONS.md)

Development: `python3 -m venv .venv && .venv/bin/pip install pytest playwright`, then `.venv/bin/python -m pytest -q authorship/tests` and `claude plugin validate ./authorship`.
