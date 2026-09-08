# SlagAlpha

SlagAlpha is a deterministic research project for the personal Binance USDⓈ-M Crypto Trading Copilot described in `CRYPTO_AGENT_DEVELOPMENT_SPEC.md`.

Current scope: **P0–P8, P9.1 input audit, and P9.2a default sensitivity planning completed**. Real DEV research is blocked until verified historical tick/step intervals exist. No strategy study or locked test has been executed. Realtime scanning and account connectivity have not started.

## Requirements

- Python 3.12 or 3.13
- No Binance API key is required or accepted in V0.1

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

## P9 research preparation (no strategy execution)

```powershell
.venv\Scripts\python.exe scripts\p9_research_input_audit.py
.venv\Scripts\python.exe scripts\p9_sensitivity_plan.py
.venv\Scripts\python.exe scripts\p9_dev_preflight.py
```

The first command freezes and audits the 548/274/274-day global split. The second saves
10 default-centered, single-parameter candidates with three cost scenarios each. Both are
planning/audit commands; they do not consume the locked test. The real DEV gate currently
reports `NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS`.

The preflight command checks the frozen inputs, read-only Git state and the exact local
environment recorded in `requirements.lock`. Exit 1 means expected research blockers;
exit 2 means a lock/input error. It never starts research, changes Git or installs packages.
The lock targets CPython 3.12.13 on Windows AMD64, not Ubuntu deployment, and pins versions
without hashes in the lock file itself. The frozen wheel manifest supplies the installation
artifact hashes. If the default `.venv` no longer uses CPython 3.12.13, restore an isolated
verification environment outside the checkout with:

```powershell
pwsh -NoProfile -File scripts\p9_restore_frozen_environment.ps1
```

Use the `venv_python` path from the final `READY` JSON for P9 verification commands. The
script pins and verifies the bootstrap tool, installs the exact interpreter without changing
PATH or the registry, and installs all dependencies from the frozen local wheels. It does not
authorize or start research. See `docs/p9-dependency-artifacts.md` and
`docs/p9-run-provenance.md` for the artifact and RunManifest boundaries.

The local Git baseline procedure and its acceptance receipt are documented in
`docs/git-baseline.md`. Generated preflight reports are ignored so checking a clean
checkout does not itself make that checkout dirty. No remote push is part of this workflow.

The offline, source-revalidated P3–P6 scanner input path and its current limitations are
documented in `docs/p9-scanner-input-contract.md`. Positive strategy paths are tested with
explicit synthetic archives and rules; they do not approve historical seeds or real research.
See `docs/development-checkpoint-2026-09-08.md` for the latest staged development checkpoint.

The slower opt-in synthetic end-to-end check is documented in
`docs/p9-source-pipeline-acceptance.md`; it uses real validators without approving real research.
It invokes `research.source_request_set.compute_source_bound_request_set_with_checkpoints` for
ordered cross-day computation and immutable daily checkpoints. That entry uses
`research.scan_days.compute_source_bound_scan_day` and the per-slot P3–P6 computation path.
The complete request set is returned only after every required DEV day and slot passes.
