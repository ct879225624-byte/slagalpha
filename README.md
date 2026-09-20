# SlagAlpha

SlagAlpha is a deterministic Binance USD-M research/backtest system. **v0.1 was completed and frozen on 2026-09-20 as Frozen DEV Research.** It is not an approved live trading system. VALIDATION and LOCKED_TEST have not been used; realtime scanning and Binance account connectivity have not started.

The frozen DEV baseline returned approximately **+3.199858 R (ZERO)**, **-1.306150 R (BASELINE)**, and **-2.845698 R (STRESS)**. Its gross edge did not cover fees plus slippage. Pivot 3x3 returned approximately **+4.625010 / +1.482079 / +0.305166 R** across those same scenarios, but is only a strong DEV hypothesis for v0.2 research, not a validated profitable strategy or a live-trading conclusion.

See the [v0.1 release overview](releases/v0.1/README.md), [release manifest](releases/v0.1/release_manifest.json), and [research diagnosis](artifacts/p9_v01_research_diagnosis_report.md) for the evidence and limitations. [Current project state](docs/current-state.md) distinguishes the frozen release from uncommitted v0.2 work.

## Requirements

- Python 3.12 or 3.13
- No Binance API key is required or accepted in v0.1

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Verification

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m slagalpha --help
```

## Verified P2 archive sample

```powershell
.venv\Scripts\python.exe -m slagalpha archive-ingest `
  --symbol BTCUSDT `
  --interval 15m `
  --year 2024 `
  --month 1 `
  --require-full-period
```

This downloads only Binance public market data, verifies the official SHA-256 checksum, validates the complete UTC candle grid and writes a partitioned Parquet file plus content-addressed manifests. Raw/normalized data and bulk per-file manifests are kept locally. Git tracks only the reviewed small evidence/input directories listed in `.gitignore`; see `docs/git-baseline.md` for restore limits.

## Archive inventory planning

```powershell
.venv\Scripts\python.exe -m slagalpha archive-plan `
  --symbol ANTUSDT `
  --interval 15m `
  --start 2024-03 `
  --end-exclusive 2024-04
```

This snapshots the official Public Data directory and writes a content-addressed availability plan;
it does not download archive ZIPs.

## Source boundaries

- `domain`: immutable domain contracts shared by research and future realtime paths.
- `data`: exchange/archive adapters and validation, beginning in P2.
- `strategy`: deterministic indicators, setup, triggers and plans, beginning in P3.
- `backtest`: event-driven replay and fill simulation, beginning in P7.
- `reporting`: reproducible research outputs, beginning in P9.

The frozen P0 rules and test scenarios are under `docs/`.

Historical preparation, provenance, and synthetic pipeline details remain in `docs/`.
Those older checkpoint documents describe their own dates, not the current release status.
