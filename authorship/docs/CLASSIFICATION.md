# Automatic classification

Once a project is initialized, nobody has to tag anything. In the background, a classifier reads each new entry and works out:

- what the entry is: a problem, an idea, a decision, a claim, a discarded approach, or a plain instruction;
- whether it brings in a new technical element;
- which earlier element it builds on, modifies, accepts or rejects;
- whether the work moved into a new stage.

From that it derives tags, stages, milestones and the lineage of every claim, so the viewer, `authorship log`, the disclosure draft and the MCP tools are filled in on their own.

This page explains what is asked, which backend answers, how answers become labels, and how to correct them.

## What gets classified

| Entry | Questions |
|---|---|
| Your prompts and notes | kind, new element, stance, target, builds on, maturity, new stage |
| Claude's replies (with text) | did Claude introduce a mechanism you did not ask for? |

Tool calls, test runs and session events are not classified; deterministic rules handle them (see [Rules that need no model](#rules-that-need-no-model)).

Each request carries:

- the entries to classify (up to 6 per request, 4,000 characters each);
- the 8 entries before them (500 characters each);
- up to 60 earlier elements the answer may point to, with their ids (`4`, or `3.2` for option 2 of reply #3);
- the stage the work is in.

## Backends

| | Claude (default) | OpenRouter (any model) | Jev |
|---|---|---|---|
| When it is used | Always, unless another is chosen or a Jev key is set | When chosen: `authorship classifier use openrouter MODEL` | When chosen, or when `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY` or `AI_GATEWAY_API_KEY` is set and nothing is chosen |
| Setup | None: it uses the `claude` command and the login you already have | `OPENROUTER_API_KEY` | A key from [TypeSafe](https://docs.typesafe.ai/), [OpenRouter](https://openrouter.ai/keys) or the Vercel AI Gateway |
| Who receives the text | Anthropic, which already receives it during the session | OpenRouter and the model's provider: parties that did not have it before | TypeSafe, plus OpenRouter or Vercel when you go through them: parties that did not have it before |
| Confidence values | Stated by the model | Stated by the model | Measured probabilities, with the full vector stored |
| Speed and cost (golden 7-entry session) | ~16 s, ~US$0.026 (Sonnet) | Depends on the model | One call per entry; about $0.042 per million input tokens |
| Free-text summary | Yes | Yes | No |

Choose one with `authorship classifier use claude|openrouter|jev` (see [Setting it up](#setting-it-up)). Turn classification off with `authorship classifier use off`; tags and the rules below still work.

### Claude backend

It runs `claude -p` with `claude-sonnet-5` by default (`authorship classifier use claude MODEL` changes it), with structured JSON output, and with everything else turned off:

- no tools, hooks, MCP servers, CLAUDE.md files or memory;
- no thinking (`MAX_THINKING_TOKENS=0`), no saved session;
- started from a scratch directory outside the project.

So it cannot read or change the project, and it is not recorded in the ledger. Sonnet was chosen over Haiku because it classified the golden session the same way across repeated runs, where Haiku did not.

### OpenRouter backend

The same system prompt and JSON schema as the Claude backend, sent to OpenRouter's chat completions API with strict structured output. Any model that supports structured output works; `authorship classifier models openrouter [FILTER]` lists them with their input price. The plugin asks for zero-data-retention endpoints, no data collection, and only providers that support every parameter it sends. The model OpenRouter reports, and the cost, are stored with each answer.

### Jev backend

Each entry is one Jev request: choice questions for kind, stance, target and stage, a noul question for "new element", and a score question for maturity. Jev cannot write text, so there is no summary. Jev returns one probability vector over all candidate targets, so each element the entry builds on gets a share of it; any candidate with probability 0.30 or more counts as a parent.

Jev is the same model through three providers. They differ in who bills you and who sees the text on the way:

| Provider | Key | Endpoint | Notes |
|---|---|---|---|
| TypeSafe (the maker) | `TYPESAFE_API_KEY` | `POST https://api.typesafe.ai/v1/systemone` | Pinned to `jev-1.13.0`. Zero data retention is for enterprise accounts. |
| OpenRouter | `OPENROUTER_API_KEY` | `POST https://openrouter.ai/api/alpha/decisions` | Model `typesafe/jev-1.13`, answered by a dated snapshot, which is stored. The plugin asks for zero-data-retention endpoints only, no data collection, and no fallback provider. Billed to your OpenRouter account. |
| Vercel AI Gateway | `AI_GATEWAY_API_KEY` | `POST https://ai-gateway.vercel.sh/v1/evaluate` | Asked for zero data retention. Cannot pin a Jev version, so the version that answered is stored. |

With several keys set, the first in that order is used; `authorship classifier use jev --provider typesafe|openrouter|vercel_gateway` picks one. `authorship classifier use jev MODEL` pins another Jev version; a `typesafe/...` id goes to OpenRouter unchanged.

Setting the key is the decision to send entry text to that provider; there is no extra prompt. See [LEGAL-NOTES](LEGAL-NOTES.md#keep-it-private).

## Setting it up

`authorship classifier use` saves the choice in `~/.config/authorship/classifier.json`, for every project. Environment variables, where set, win over it. The annotator reads both when it starts, and it keeps running across sessions, so **after any change run `authorship restart`**. Run every command below in your own terminal, not inside Claude Code: `classifier use` refuses to run from it.

**See what is in use:**

```bash
authorship classifier          # the backend the annotator uses, where the text goes, how to change it
authorship classifier --test   # also sends one made-up sentence and shows the answer (checks your key)
```

The viewer's **Help** says the same; a strip under the tabs appears when the text goes to another provider or labeling fails. **How to change it** opens these steps.

**Claude (default).** Nothing to set up; it needs the `claude` command on the PATH. To pick another model:

```bash
authorship classifier models claude       # sonnet, opus, haiku, or any id `claude --model` accepts
authorship classifier use claude haiku    # default: claude-sonnet-5
authorship restart
```

**Any model through OpenRouter.**

1. Put `export OPENROUTER_API_KEY=your-key` in your shell profile and open a new terminal.
2. Pick a model:

   ```bash
   authorship classifier models openrouter gemini           # models with structured output, filtered
   authorship classifier use openrouter google/gemini-3.8-flash
   authorship restart
   authorship classifier --test
   ```

**Jev.**

1. Get a key from [TypeSafe AI](https://docs.typesafe.ai), from [OpenRouter](https://openrouter.ai/keys), or from the Vercel AI Gateway (see the table above for the differences).
2. Add it to your shell profile (`~/.zshrc` or `~/.bashrc`), so every Claude Code session has it:

   ```bash
   export TYPESAFE_API_KEY=your-key      # or: export OPENROUTER_API_KEY=your-key
                                         # or: export AI_GATEWAY_API_KEY=your-key
   ```

3. Open a new terminal and run `authorship classifier use jev` (optionally `--provider openrouter`, or a model), then `authorship restart`.
4. Check it with `authorship classifier --test`.

With Jev or OpenRouter, the text of your entries goes to that provider. Decide that with your attorney before filing.

**Other switches:**

```bash
authorship classifier use off    # no automatic labels; typed tags and the rules keep working
authorship classifier use auto   # forget the choice: Jev if a Jev key is set, else Claude
authorship restart               # after any of these
```

The same settings as environment variables, which win over the saved choice: `AUTHORSHIP_AUTO_BACKEND` (`claude`, `openrouter`, `jev`), `AUTHORSHIP_AUTO_MODEL` (Claude or OpenRouter model), `AUTHORSHIP_JEV_PROVIDER`, `AUTHORSHIP_JEV_MODEL`, and `AUTHORSHIP_AUTO=0`.

## From answers to labels

Thresholds are asymmetric, so the record never errs in your favor:

| Label | Counts automatically | Waits in `authorship review` | Dropped |
|---|---|---|---|
| Favors you: conception, problem, decision, claim, discard, maturity jump | 0.80 or more | 0.50 to 0.80 | under 0.50 |
| Against you: AI-origin element | 0.50 or more | never | under 0.50 |

How each answer is used:

- **Tags.** A kind with confidence 0.70 or more becomes a tag such as `#idea`. It is shown as `#idea~` in `authorship log` and with a dashed outline in the viewer. It is only added to entries with no tag of yours.
- **Conception.** It follows the spec's rule: the entry brings a new element (`new_element`) and originates or reshapes an earlier one. This applies whether the entry is called an idea, a decision or a claim. Score: the smaller of the two confidences.
- **AI-origin elements.** Two cases raise one:
  - you accept an AI option as proposed (stance "accepts", the target written by Claude);
  - a reply where Claude introduced a mechanism you did not ask for.
- **Lineage.** "Modifies", "extends", "rejects" or "accepts" toward a target, plus every "builds on" parent, become edges. Edges need 0.50 (0.30 for a Jev parent). They are stored with source `auto`. Claim genealogy, disclosures and the report follow these edges together with the ones you confirmed.
- **Stages.** A stage change counts only when it differs from the stage the work is already in.
- **Reduction to practice.** The rule "first passing test after a failing one, on files edited since the idea" anchors on ideas found by the classifier as well as tagged ones.

Every result is written to `.authorship/annotations.jsonl` with:

- `method: "auto"` and the backend;
- the model id the provider reported;
- `questions_hash`, a hash of the prompt and schema;
- every confidence, plus Jev's full probability vectors.

Nothing goes into the ledger: the ledger holds evidence; annotations hold opinions.

## Correcting it

- **Type a tag.** A tag you write (`#idea`, `#claim`, `#stage Prototype` ...) always wins over the classifier for that entry.
- **`authorship review`.** It walks through what is waiting (favorable labels between 0.50 and 0.80). Accept, reject or edit each one; your decision becomes a `Confirm` entry in the ledger, in your name.
- **`authorship review --all`.** It also walks through the automatic labels and links already counted, so you can reject or edit any of them. A rejection is a human entry, and the label or link stops counting.

Milestone states, as shown everywhere: `confirmed` (by you), `automatic`, `pending` (waiting for you), `rejected`.

## Rules that need no model

These run offline, always, whatever the backend:

| Signal | Milestone |
|---|---|
| A tag you typed | the matching milestone, counted as declared by you |
| First passing test after a failing one, on files edited since an idea | reduction-to-practice evidence for that idea |
| A reply line starting `AI proposal:` (Claude is told to write one) | AI-origin element |
| Numbered alternatives in a reply | one AI position per option (`3.1`, `3.2` ...) |
| Session start and end | session boundary |

## When it does not run

`authorship classifier` (and the strip in the viewer) says why:

- `off`: `AUTHORSHIP_AUTO=0`, or `authorship classifier use off`.
- `no-backend`: no Jev key and no `claude` command on the PATH.
- `error`: the backend failed (network, rate limit); details in `.authorship/errors.log`. Nothing is lost: unclassified entries are retried the next time the ledger changes.

The older Tier 1 mode (`AUTHORSHIP_JEV=1` with `scripts/questions.toml`, from the spec) still exists for calibration work. Its labels are suggestions that always wait for confirmation, and it requires the consent note typed in a terminal (`annotator.py consent`).
