# The viewer

A local web page that shows the record in three views, in plain words. It runs on your machine only (`127.0.0.1`), starts in the background with each Claude Code session, and opens in your browser once.

To open it again: `authorship open` in your own terminal. A page opened any other way is read-only.

| Tab | Question |
|---|---|
| **What you invented** | What did I invent, and who contributed each part? |
| **Timeline** | What happened, in order? |
| **Review** | Which labels should I check? The badge says how many wait. |

The line at the top answers the first question anyone asks of a record, whether it can be trusted: **Record intact · 40 entries · sealed to #38 · 12 of 12 signed**. It turns red, with a banner, if any entry was changed after it was written. **Help** explains the tabs, the symbols and who labels the entries; **Report** is the draft for your attorney.

## Symbols

The same everywhere. Identity is never shown by color alone: a shape and a word always go with it.

| Symbol | Meaning |
|---|---|
| Blue circle, "You" | Written by you |
| Orange square, "Claude" | Written by Claude |
| Green, "You, changing Claude" | Your element that changes something Claude proposed |
| Dashed outline, struck-through text | Discarded or rejected |
| Solid label (`idea`) | Typed by you, confirmed by you, or found by a deterministic rule |
| Dashed label (`idea`) | Set automatically by the classifier |
| Label with `?` | Waiting for your answer in Review |
| `#4`, `#3.2` | Ledger entry 4; option 2 of Claude's reply #3 |

## What you invented

![What you invented](images/viewer-overview.png)

One card per claim:

- the claim, quoted from the ledger;
- a bar with how many of its elements came from you, from you changing Claude's proposal, and from Claude;
- the list of those elements, each with its origin, its evidence ("tests prove it at #8") and the entry it cites. Click the `#N` to open that entry in the Timeline.

Below the claims, **What came from Claude** lists every AI-originated element plainly. An honest record weighs more. Then the stages of the work.

The links between elements come from your confirmations and the automatic classifier, as in the disclosure draft.

## Timeline

![Timeline](images/viewer-timeline.png)

The conversation in order, grouped by stage, one sentence per entry: "You proposed an idea, changing Claude's #3.3", "Claude offered 3 options", "Claude ran the tests: they pass after failing".

- **Key moments** (the default) shows what you wrote, the options Claude offered, test results that prove an idea, and the guard's blocks. Claude's routine work is folded into one row per stretch.
- **Everything** shows every entry, including each tool call.
- **Search** filters by any word, file name or `#seq`.

Open any row for its full text, its hash and time, the code change for edits, and how it links to other entries. The address keeps the open entry (`#view=timeline&open=4`), so a link to it can be shared with your attorney alongside the export.

## Review

![Review](images/viewer-review.png)

The classifier labels every entry on its own. A label in your favor that it was not sure of (0.50 to 0.80) waits here as a question, for example "Is #11 a maturity jump: the idea became more definite?".

- **Yes**, **No** and **Change…** each record your answer in the ledger as a `Confirm` entry, in your name (and signed, if you set up signing).
- **Counted automatically** lists every label and link already counted, so you can mark one **Not right** or change it.
- **Your answers** lists what you decided, most recent first.

The same can be done from the terminal with `authorship review` (and `authorship review --all`).

## Who labels the entries

By default Claude does, through your own login, and the page says nothing about it: no new party receives your text. A strip under the tabs appears only when that changes: entry text going to another provider (OpenRouter or Jev, in amber, naming the provider), or labels failing. **Help** always says which backend is in use; **How to change it** opens the setup steps.

## Security

The page binds to `127.0.0.1` and rejects any other `Host` header. It writes nothing except Review answers.

Each answer needs the session secret. `authorship open` passes it in the address, and the page moves it out of the URL at once. Each answer also needs the page's own `Origin`.

Claude cannot read the secret or reach the port: the guard blocks both. Details in [THREATS.md](THREATS.md).

The page loads nothing from the internet and has no library: about 65 KB of HTML, CSS and JavaScript. Responses over 16 KB are gzipped when the browser accepts it; antivirus that inspects local HTTP (Avast's Web Shield, for one) otherwise slows large records to a crawl.
