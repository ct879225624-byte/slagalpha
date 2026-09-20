# Current Project State

> This file is the canonical snapshot of what is true now. It is intentionally not a
> timeline. Historical checkpoints and older handoffs remain useful evidence, but they do
> not override this page or the repository itself.

## Current main

- Branch: `main`
- `main` has advanced beyond the frozen `v0.1` tag with post-release maintenance.
- Git HEAD, upstream state, and working-tree status must be read from Git; they are not
  copied into this document because they change independently of durable project truth.

## Frozen v0.1

SlagAlpha is a deterministic, replayable Binance USDⓈ-M research/backtest system. It is
not an account connector, trading bot, deployment, or real-money authorization.

The v0.1 DEV research closeout is frozen and archived. Its durable evidence says:

- Frozen commit: `372069639da695fd57dc7059d600bfb7d6e79fec` (tag `v0.1`).
- Baseline: `ZERO +3.199858 R`, `BASELINE -1.306150 R`, `STRESS -2.845698 R`.
- Pivot 3×3 is a strong DEV discovery hypothesis (`ZERO +4.625010 R`,
  `BASELINE +1.482079 R`, `STRESS +0.305166 R`) but is not a promoted or validated
  strategy; its improvement is largely opportunity-set change.
- DEV sensitivity is closed. Validation and Locked Test were not consumed, and no
  parameter or strategy was promoted.
- The historical release README records four Windows transport test failures. The current
  worktree passes all six transport regression tests after a test-temp-path and immutable
  publish-temp-name fix, without changing the frozen release. The Phase 3 Stage A report
  was reconstructed rather than independently persisted;
  and historical tick-size and DEV approximate funding-mark warnings remain.

Authoritative release anchors are `releases/v0.1/release_manifest.json`,
`releases/v0.1/README.md`, `artifacts/p9_v01_closeout_summary.json`, and
`artifacts/p9_v01_research_diagnosis_report.md`.

## Local v0.2 worktree snapshot — reviewed 2026-09-20

This section records one local development snapshot. These files may be uncommitted and
unpushed, and this list is not the live state of the public Git repository. Always inspect
`git status`, the current branch, and the remote before resuming work.

The repository is between a completed v0.1 frozen DEV closeout and a **v0.2 DEV readiness
gate that is blocked**. At review time, the relevant local v0.2 files were:

- Modified tracked file: `src/slagalpha/research/dev_source_scan.py`.
- Untracked design/config: `docs/v0.2-research-design.md`,
  `docs/v0.2-owner-review.md`, and `config/research/v0.2_research_plan.json`.
- Untracked implementation/tests: `src/slagalpha/research/smg1.py`,
  `scripts/v02_dev_enablement.py`, `tests/test_smg1.py`, and
  `tests/test_v02_dev_enablement.py`.
- Untracked readiness evidence: `data/manifests/dev_execution_semantics/`
  and the dated v0.2 files under `docs/handoffs/`.

These local files are not part of the frozen v0.1 release. Their commit/push status must be
verified from Git rather than inferred from this dated snapshot.

The plan/config still state `DESIGN_ONLY`, `execution_allowed=false`, and all execution flags
false. The owner review records `V0.2_DESIGN_GATE=PASS` and approves only the implementation
scope. The v0.2 design proposes one control
(`C0_v01_discovery_control`) and one primary
candidate (`C1_smg1_primary`) for a single 15m structure-maturity hypothesis (SMG-1), with
no parameter sweep or post-hoc variants.

The isolated SMG-1 module passed 12 local tests at the last readiness review. The tracked
`src/slagalpha/research/dev_source_scan.py` now has an uncommitted v0.2 C1 integration
change; it is not independently verified as a formal C1 run and must not be attributed to
v0.1. A formal C1 accepted-request set, request-scoped 1m Candle/Funding coverage, and
C1 Scan/Replay lineage have not been established. The last input gate also
reports missing `CANDLE_ONE_MINUTE` and `FUNDING`; the frozen-environment semantic gate reports
an environment-lock mismatch and unbound dependency artifacts. Formal DEV economics was not
started. Validation and Locked Test remain unconsumed.

## Current blockers

- The v0.2 formal C1 provenance and request-scoped 1m/Funding coverage have not been proven.
- The v0.2 config is still `DESIGN_ONLY` and disables execution.
- The last frozen-environment/dependency gate was blocked. The full historical readiness
  evidence is `artifacts/v0.2_dev_readiness_preflight_20260920.json`.

## Explicitly not authorized

- Do not modify `releases/v0.1/**`, `artifacts/p9_v01_*`, the `v0.1` tag, or the v0.1
  release-pinned semantics and evidence.
- Do not treat the v0.1 Pivot 3×3 result as a final parameter or as a profitability claim.
- Keep Validation and Locked Test blocked until their explicit prerequisites and owner
  authorization exist.
- Preserve historical Universe, time isolation, fail-closed missing-data behavior,
  Funding and cost semantics, manifest lineage, and content hashes.
- Do not connect an account, use secrets, trade, deploy, push, or download new research
  data as part of continuity work.

## Repository map for resumption

- `README.md`: setup, verification commands, scope, and environment boundaries.
- `releases/v0.1/README.md` and `releases/v0.1/release_manifest.json`: frozen release
  interpretation and hashes.
- `artifacts/p9_v01_closeout_summary.json` and
  `artifacts/p9_v01_research_diagnosis_report.md`: final DEV evidence and limitations.
- `docs/strategy-rules-v0.1.md`, `docs/decision-tables-v0.1.md`,
  `docs/data-contracts-v0.1.md`, and `docs/golden-scenarios-v0.1.md`: frozen contracts.
- `docs/v0.2-research-design.md`, `config/research/v0.2_research_plan.json`, and
  `docs/v0.2-owner-review.md`: design-only proposal and passed design gate; not an
  execution authorization.
- `src/slagalpha/research/smg1.py` and `tests/test_smg1.py`: untracked v0.2 implementation
  artifacts; the tracked scanner has a separate uncommitted integration change. None is
  part of the frozen v0.1 release.
- `docs/decision-log.md`: durable decisions and rejected alternatives.
- `docs/handoffs/`: self-contained phase/interruption handoffs; the latest handoff is the
  starting point for a fresh thread after this file.

## Immediate next task (not started here)

Complete and independently verify the formal C1 scanner integration and v0.2 execution
provenance, then generate the C1 accepted-request set and prove request-scoped 1m/Funding
coverage in the frozen environment. Re-run readiness without changing SMG-1 or any economic
gate. Do not run DEV economics, Validation, or Locked Test until readiness passes.
