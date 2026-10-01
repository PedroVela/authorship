# Legal notes

**This is not legal advice.** The plugin produces records and drafts for a patent attorney. Talk to one before relying on anything here, and before filing.

## What the record is for

Under current US practice, AI is treated as a tool and only natural persons can be named as inventors; a human must have made a significant contribution to the conception of each claimed invention. The conversation in which conception happens usually leaves no durable record. This plugin keeps one, and keeps what the human originated apart from what Claude originated.

The record shows what happened, including facts that are unfavorable to you. It does not turn an AI-originated idea into a human one. Drafts state AI-originated elements plainly, and the inventorship reviewer lists unfavorable facts first. A record that hides them would not be credible.

## First to file

The United States and Paraguay are first-to-file systems: priority comes from the filing date, not from the date of conception. A well-kept ledger does not replace filing and does not give you priority. File early; use the record to support inventorship and to answer questions later.

## Keep it private

`.authorship/` holds a detailed account of your invention. Public disclosure before filing can destroy novelty.

- Keep the repository private. Do not push `.authorship/` or `authorship-exports/` to a public remote.
- The automatic classifier sends entry text to a model. By default that model is Claude, through your existing Claude Code login. The same text already goes to Anthropic during the session, so no new party receives it.
- Setting a Jev key (`TYPESAFE_API_KEY`, `OPENROUTER_API_KEY` or `AI_GATEWAY_API_KEY`) switches the classifier to Jev, a third-party service, and from then on the text of your invention goes to it (and, through OpenRouter or Vercel, also passes through them). Do that only after thinking about confidentiality, preferably with zero data retention (the plugin asks for it through OpenRouter and the Vercel AI Gateway) or under a confidentiality agreement. `AUTHORSHIP_AUTO=0` keeps everything local except the Claude Code session itself.
- Anchoring sends only a hash of the ledger head to the timestamp authority, never ledger text.

## Confirmations matter

Machine labels are opinions and never enter the ledger. They are applied automatically, so the record is complete without your effort, but leaning against you:

- A label in your favor (for example "conception candidate") counts only from 0.80 confidence. Between 0.50 and 0.80 it waits for your decision in `authorship review`.
- A label against you (an element that came from Claude) counts from 0.50.
- Every draft and report says which labels are automatic and which you confirmed.

An attorney will give more weight to labels you confirmed. Before relying on a draft, run `authorship review --all` and accept, reject or edit what matters. Your decisions are recorded as human entries in the ledger, hash-chained like everything else. Make them honestly: an attorney or an examiner may read them.

## Timestamps

RFC 3161 responses and OpenTimestamps proofs show that the ledger head existed at a given time. They do not show who wrote an entry or that its content is true; the hash chain, the guard and the human-only commands address that. Keep the anchor files (`.authorship/anchors/`) with the repository.

## Drafts

`/authorship:disclosure` and the `inventorship-reviewer` subagent write drafts under `authorship-exports/`. They quote your text verbatim and cite every element as `#<seq> (<hash12>)`; `tests/validate_citations.py` checks that each citation resolves. They are starting points for your attorney, not filings.
