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
- Tier 1 (Jev) sends entry text to a third-party service. It is off by default. Turn it on only after thinking about confidentiality, preferably with zero data retention (the Vercel AI Gateway provider asks for it) or under a confidentiality agreement.
- Anchoring sends only a hash of the ledger head to the timestamp authority, never ledger text.

## Confirmations matter

Machine annotations are opinions and never enter the ledger. Labels that favor you (for example "conception candidate") need a score of 0.80 and your explicit confirmation in the viewer; labels against you surface at 0.50. Your confirmations are recorded as human entries in the ledger, hash-chained like everything else. Review them honestly: an attorney or an examiner may read them.

## Timestamps

RFC 3161 responses and OpenTimestamps proofs show that the ledger head existed at a given time. They do not show who wrote an entry or that its content is true; the hash chain, the guard and the human-only commands address that. Keep the anchor files (`.authorship/anchors/`) with the repository.

## Drafts

`/authorship:disclosure` and the `inventorship-reviewer` subagent write drafts under `authorship-exports/`. They quote your text verbatim and cite every element as `#<seq> (<hash12>)`; `tests/validate_citations.py` checks that each citation resolves. They are starting points for your attorney, not filings.
