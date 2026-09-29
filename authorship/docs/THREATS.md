# Threat model

The agent being audited (Claude) is also the most capable actor in the session. The ledger is only worth something if Claude cannot alter it, cannot speak in the human's name, and cannot quietly switch the recording off. This file lists what the plugin defends against, how, which test shows it, and what is left.

## Defenses

| Threat | Defense | Test |
|---|---|---|
| Claude edits or deletes the ledger | `Edit(/.authorship/**)` deny rule written by `init`; the guard blocks file tools on `.authorship/` (after `realpath`, so symlinks and `..` do not help) and Bash commands that name it, glob to it, or run destructive commands on a directory that contains it; the sandbox turns the deny rule into an OS-level write denial for Bash | `tests/test_phase1_protection.py` (46 must-block, 31 must-allow and 2 ask cases); init rules present |
| Claude rewrites `.authorship/` history with git | the guard blocks `filter-branch`, `filter-repo`, `reset --hard` when the store has changes or the target differs, `checkout`/`restore`/`stash` over uncommitted store changes, `clean -f` over the store, `commit --amend` of a commit that touched it, `rebase` over such commits | same table |
| Claude writes a human note or confirmation through the CLI | `note`, `confirm` and `anchor` refuse when `CLAUDECODE=1` or `CLAUDE_CODE_ENTRYPOINT` is set, or when an ancestor process is `claude`; the guard also blocks Bash from running `ledger.py`, `viewer.py` or `anchor.py` (except the `anchor.py seal` form, which writes no human entry) and from importing the ledger module | `test_human_only_cli_*` |
| Claude forges a confirmation through the viewer | `POST /api/confirm` needs the 32-byte session secret, an `Origin` equal to the viewer origin and a JSON content type; the secret file is mode 0600 under `run/`, which the guard and a `Read` deny rule keep from Claude; the guard blocks Bash from reaching the viewer port; the server binds `127.0.0.1` and rejects foreign `Host` headers (DNS rebinding) | `tests/test_phase4_viewer.py` (403 without secret, 403 on foreign Origin, 200 with both); guard cases for `curl` to the port |
| Claude disables the hooks | `ask` rules on `.claude/settings.json` and `settings.local.json`; the guard asks before Bash touches those files or runs `claude plugin disable/uninstall`; `SessionStart` records `plugin_version` and `hooks_sha`, so a changed or missing hook set is visible, and missing sessions show as gaps | guard `ask` cases |
| Someone rewrites the whole chain consistently | external anchors: RFC 3161 responses and OpenTimestamps proofs over `<seq>-<hash16>.txt`; `verify --anchors` fails when the recomputed chain does not contain the anchored head | `test_verify_anchors_fails_when_whole_chain_is_rewritten` |
| Secrets end up in evidence | redaction (Anthropic, OpenAI, Stripe, Slack, GitHub, AWS, GCP, JWT, PEM, bearer tokens, `password=`/`secret=` assignments) before hashing and before blobs are written | `tests/test_redact.py` (a positive and a negative case per pattern) |
| Invention text leaks to a third party | Tier 1 (Jev) is off by default; enabling it needs an env flag, a key, and a consent the human types in a terminal, recorded as a `ManualNote`; the Vercel provider requests zero data retention; anchoring sends only a hash | `test_jev_off_opens_zero_sockets` |
| A crash or a torn write corrupts the chain | appends are serialized by a lock and fsynced; a partial last line is moved to `errors.log` before the next append, never a complete entry | `test_torn_tail_is_repaired_without_touching_complete_entries` |
| Hooks break the session | every hook exits 0 on any error and logs to `errors.log`; no network on the hook path; p95 latency is measured (about 33 ms on the development machine against a 100 ms budget) | `test_malformed_stdin_exits_zero_and_logs`, `test_hook_latency_p95_under_100ms` |

## Residual risks

- **Indirect execution.** Claude can write a script that touches `.authorship/` and then run it (`python tool.py`), or build the path at run time in ways the guard's text analysis does not see (base64, variables assembled across commands). The guard catches the obvious forms; the sandbox is the real barrier here. Enable it.
- **Human actions.** Anything the human does in their own terminal is trusted. The human can also delete the repository. Anchors make a later rewrite detectable, not impossible.
- **Unanchored history.** Until an entry is anchored, a party with write access to the files could rebuild the chain from that point with valid hashes. Seal often (`/authorship:seal`, or `AUTHORSHIP_ANCHOR=1`).
- **Hooks off outside Claude Code.** Work done in a session where the plugin was disabled is not recorded; the ledger shows the gap but cannot fill it.
- **Local readers.** Other processes running as the same OS user can read the ledger and the viewer secret. The viewer's read endpoints need no secret (they are bound to loopback and protected against DNS rebinding and cross-origin reads by the browser).
- **Classifier error.** Tier 1 labels can be wrong; they never enter the ledger, favorable labels need the human's confirmation, and every annotation stores the model id, question-set hash and full probability vector so it can be reproduced.
- **Parent-process check.** The second human-only signal looks for an ancestor named `claude`. A process started outside that tree (for example by a launchd job Claude created) would pass it; the env marker, the guard and the sandbox remain.
