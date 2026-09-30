# authorship

A Claude Code plugin that keeps a tamper-evident, timestamped record of how a human and Claude co-develop an invention.

- Plugin: [`authorship/`](authorship/) — start with [authorship/README.md](authorship/README.md)
- Build spec: [AUTHORSHIP_PLUGIN_SPEC.md](AUTHORSHIP_PLUGIN_SPEC.md); where the build differs, see [authorship/docs/DEVIATIONS.md](authorship/docs/DEVIATIONS.md)

```bash
claude --plugin-dir ./authorship        # try a local checkout
# or, installed from GitHub:  /plugin marketplace add <owner>/authorship  then  /plugin install authorship@authorship-dev
.venv/bin/python -m pytest -q authorship/tests
```

Keep any repository that contains `.authorship/` private.
