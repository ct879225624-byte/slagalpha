# SlagAlpha v0.1 — Frozen DEV Research

v0.1 is a **research/backtest system**, not a strategy approved for real-money deployment. It is a reproducible DEV closeout with frozen strategy semantics, immutable input references, content hashes, and explicit limitations.

## What the evidence says

- Baseline is positive only before realistic costs; after BASELINE costs it is negative (`-1.306150 R`) and is more negative under STRESS (`-2.845698 R`).
- Pivot 3×3 is a **strong DEV hypothesis**: it is positive in the three existing cost scenarios, but the sample is small and the improvement is driven largely by candidate-set changes.
- Pivot 3×3 is not “the best strategy”, and it is not a validated profitable strategy.
- LONG/SHORT separation and trigger-family separation remain hypotheses for a later research version.

## Governance boundary

Validation was not consumed. Locked Test was not consumed. No v0.1 artifact authorizes promotion, deployment, or real-money execution. The release does not change frozen strategy semantics and does not claim future profitability.

## Reproduction pointers

Use `release_manifest.json` as the index for the strategy/scanner/replay source hashes, immutable manifests, final `ConfirmedTriggerLedger` hash and reconciliation, baseline replay summaries, and Phase 1/2/3 closeout artifacts. The complete content-addressed ledger and raw market-data blobs remain in their existing local archive locations and are not duplicated into Git.

## Caveats retained

- Windows path-length caused `FileNotFoundError` in four existing transport regression tests.
- The Phase 3 Stage A original candidate report was not independently persisted; Stage B used immutable checkpoint reconstruction and request-set reconciliation.
- Historical tick-size warnings remain.
- DEV approximate funding-mark warnings remain.

This release is archived at tag `v0.1`; v0.2 work must start from a new commit after this freeze and must not rewrite these artifacts.
