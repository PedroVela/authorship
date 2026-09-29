---
name: seal
description: Anchor the current ledger head to external timestamps (RFC 3161 timestamp authority, and OpenTimestamps when installed), so later tampering with the whole chain is detectable. Use when the user asks to seal, timestamp or anchor the record.
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/anchor.py seal), mcp__plugin_authorship_authorship__chain_status
---

1. Run exactly this command (the guard allows only this form):

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/anchor.py seal
   ```

2. Report what it printed: the head `#seq` and hash being sealed and the methods used. Anchoring runs in the background; a `pending` Anchor entry is written first and a `complete` one when the proofs arrive (OpenTimestamps needs a later Bitcoin confirmation, picked up at a later session start).

3. A few seconds later, call `chain_status` and report `sealed_upto` and `unsealed`.

Anchoring contacts the timestamp authority (`AUTHORSHIP_TSA`, default freetsa.org); it sends only a hash of the head, never ledger text.
