---
name: disclosure
description: Draft an invention disclosure for the attorney from the authorship ledger, with every element cited to a ledger entry and its origin (human, AI or mixed) stated plainly. Use when the user asks for a disclosure, an invention write-up, or a summary of what they invented.
argument-hint: "[claim seq]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/drafts.py*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/tests/validate_citations.py*), Read, Edit(authorship-exports/**), mcp__plugin_authorship_authorship__*
---

Draft `authorship-exports/<date>-disclosure.md` for attorney review.

1. Generate the deterministic first draft (it quotes human text verbatim and cites every item):

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/drafts.py disclosure --project "${CLAUDE_PROJECT_DIR}" $ARGUMENTS
   ```

   Pass `--claim <seq>` when the user named a claim.

2. Read the file it printed. Use the authorship MCP tools (`lineage`, `get_node`, `milestones`, `discarded`, `search`) to check it and to add short connecting prose where a section is unclear. The sections, in order:
   1. Problem. 2. Alternatives considered. 3. The invention. 4. Element table: element, origin (human / AI / mixed), citations `#seq (hash12)`. 5. Reduction-to-practice evidence. 6. Discarded approaches. 7. Timeline. 8. Chain and anchor status.

3. Rules, all mandatory:
   - Quote human text verbatim, inside `>` quotes. Never strengthen, summarize or paraphrase what the human conceived.
   - State AI-originated elements plainly, including when the human accepted them unchanged.
   - Every citation has the form `#<seq> (<hash12>)` (or `#<seq>.<n> (<hash12>)` for an AI option) and must come from a tool result. Never invent one; never write a bare `#<seq>`.
   - Keep the header line `Draft for attorney review. Not legal advice.`
   - Write only under `authorship-exports/`. Never touch `.authorship/`.

4. Validate, fix any error it reports, and re-run until it passes:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/tests/validate_citations.py authorship-exports/<file>.md --project "${CLAUDE_PROJECT_DIR}"
   ```

5. Tell the user the path, how many citations were checked, and which elements are AI-originated or mixed. Remind them it is a draft for their attorney.
