---
name: review
description: Show the queue of machine suggestions (milestones and lineage edges) that wait for the human's confirmation, and point to the viewer where the human confirms them. Use when the user asks what needs review or confirmation.
allowed-tools: Bash(sh ${CLAUDE_PLUGIN_ROOT}/scripts/py.sh index.py review-queue*), mcp__plugin_authorship_authorship__get_node
---

1. Print the queue:

   ```bash
   sh ${CLAUDE_PLUGIN_ROOT}/scripts/py.sh index.py review-queue --project "${CLAUDE_PROJECT_DIR}"
   ```

2. For each item, give one line: the entry (`#seq`), the suggested label in plain words, the score, and, when useful, the entry's text from `get_node`. Group AI-origin suggestions first.

3. Tell the user that confirmations are theirs to make, in their own terminal (outside Claude Code): `authorship review` asks accept / reject / edit for each item. The viewer's Review tab (`authorship open`) does the same.

You never confirm, reject or edit a suggestion yourself, and you never contact the viewer.
