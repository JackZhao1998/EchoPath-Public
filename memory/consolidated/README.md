# Consolidated Memory Library

This folder stores curated replay-ready memories. A consolidated memory should describe a reusable skill when the source trajectory reveals one, not just preserve a one-off transcript.

- `records/` contains individual consolidated memory JSON records.
- `index.json` is rebuilt from the records and gives coding agents a compact catalog.
- `generalization` describes any reusable intent, required inputs, canonical example values, and replay placeholders.
- `match_keys` remain concrete to the original request. Broad intent phrases belong under `generalization.intent_keys` and must be bound to current inputs before replay.

Raw first-run trajectories stay in `../actions/`; only selected, consolidated memories belong here.

## Packaged Example Memories

The public repo includes local GUI example memories in:

- `records/consolidated-local-demo-stocks-amazon-1w-close.json`
- `records/consolidated-local-demo-weather-location-forecast-close.json`

These four active Stocks/Weather records require macOS; the deprecated Clock
records are excluded. They are recorded examples, not fresh release validation.
They correspond to the prompts in `examples/local_gui_memory_pack/TASKS.md` and are meant for quick
visible GUI replay smoke checks.

To use one, send the exact task prompt to EchoPath. The replay matcher checks consolidated memories
before local raw trajectories. The default memories use `memory_replay_visual_bindings` and crops
from `crops/local-demo-stocks-amazon-1w-close/` and
`crops/local-demo-weather-location-forecast-close/` so the click targets are visually re-aimed on
the current screen.

Direct coordinate replay remains available as an explicit option through the paired
`records/consolidated-local-demo-direct-stocks-amazon-1w-close.json` and
`records/consolidated-local-demo-direct-weather-location-forecast-close.json` memories. To use
direct replay, send `Direct replay: ` followed by the exact task prompt.

The Weather memory adapts the requested location and opens the top Weather search result without
adding it to saved locations.

Bundled memories intentionally omit full raw screenshots, private machine paths, and local run
metadata. They include only the small crop assets needed for visual re-aiming.

Example: a recorded workflow for downloading "Attention Is All You Need" from arXiv should consolidate as `arxiv_paper_download`, a general "download paper from arXiv with a paper URL, arXiv ID, or title" memory. The original paper ID and URLs stay in `canonical`; replay binds the current request to new `paper_title`, `arxiv_id`, `search_url`, and `pdf_url` values.
