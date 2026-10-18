# Scientific analysis UI prototype

Open [the standalone HTML](scientific-analysis-prototype.html) directly, or serve this directory:

    python3 -m http.server 8766 --bind 127.0.0.1 --directory docs/mockups

Open http://localhost:8766/scientific-analysis-prototype.html?variant=A.

- A: scientific analysis below the existing map or comparison.
- B: location context beside the analysis panel; stacked on smaller screens.
- C: analysis uses the full content width, with location context available on demand.

Switch between Location map and Compare sites, select ZIPs, choose any of the six
chart types, change weather variables, and try recorded, sparse and forecast-only
ranges. Model diagnostics explicitly use a separate illustrative holdout period.

All observations, intervals, model results and map geometry are synthetic. The HTML
has no database connection, external scripts or model inference. CSV rows are flagged
as illustrative. The prototype is a standalone design reference; the application does not load it.
