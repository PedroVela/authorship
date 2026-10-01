# Vendored viewer libraries

Pinned copies, served locally by `viewer.py`. No CDN at runtime.

| Library | Version | File | License |
|---|---|---|---|
| cytoscape | 3.34.3 | cytoscape.min.js | MIT (LICENSE.cytoscape.txt) |

The Map lays out its lanes itself (time left to right, one lane per kind of entry), so elkjs,
cytoscape-elk and cytoscape-expand-collapse are no longer shipped; see `docs/DEVIATIONS.md`.

Update by `npm pack`-ing the new version and copying the same file; record the change here.
