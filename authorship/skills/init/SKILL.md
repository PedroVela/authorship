---
name: init
description: Set up authorship recording in this project. Creates .authorship/, adds the permission rules that keep Claude away from the evidence, and updates .gitignore. Use when the user asks to start recording authorship or inventorship evidence.
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_project.py*)
---

Run this command and show the user its output verbatim:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_project.py --project "${CLAUDE_PROJECT_DIR}"
```

Then tell the user, in a few short lines:

1. Recording is on now, in this session (no restart needed), and the viewer opens in the browser. Every prompt, tool call and response is hash-chained into `.authorship/ledger.jsonl`.
2. Nothing else to do: a background classifier labels each entry (problem, idea, decision, claim, stage) on its own; `authorship log` shows the labels. Tags such as `#idea` or `#claim` are optional and override it.
3. Their own commands live in one CLI, run in their own terminal (never through Claude): install it once with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cli.py install`, then `authorship note "..."`, `authorship log`, `authorship open`.
4. The recommendation to enable the sandbox (`/sandbox`) and the optional status line snippet, both printed above.
5. Keep the repository private: public disclosure before filing can destroy novelty.

The output ends with the authorship protocol. Follow it from this point on in this session, exactly as if it had been given to you at session start. No restart is needed: the viewer is already starting and every prompt from now on is recorded.

Do not edit `.claude/settings.json` yourself; the script already merged the rules. Do not read or write anything under `.authorship/`.
