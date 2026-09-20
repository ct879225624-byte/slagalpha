# Durable Decision Log

This log contains finalized decisions that should survive a thread change. It is not a
daily progress log. Entries retain their original decision order (D-001 through D-007);
historical statements describe the decision at the time, not a current Git status.

## D-001 — Repository documentation is the continuity authority

- **Status:** Final
- `docs/current-state.md` is the current truth; `docs/decision-log.md` is the durable
  decision record; dated files under `docs/handoffs/` are self-contained resume points.
- Chat history, progress chatter, and old checkpoints are context only. A stale handoff
  cannot override current files, Git state, or release manifests.
- **Rejected alternative:** keeping the only resume context in one indefinitely growing
  chat thread or appending every event to the current-state page.

## D-002 — v0.1 is a frozen DEV research release

- **Status:** Final
- The `v0.1` tag and commit `372069639da695fd57dc7059d600bfb7d6e79fec` freeze the DEV
  closeout. `releases/v0.1/**`, `artifacts/p9_v01_*`, and release-pinned evidence are
  read-only.
- Validation and Locked Test were not consumed; the release authorizes neither promotion
  nor deployment.
- **Rejected alternative:** rewriting v0.1 artifacts or semantics while exploring v0.2.

## D-003 — v0.1 evidence is diagnostic, not a promotion decision

- **Status:** Final
- Baseline is negative after BASELINE and STRESS costs. Pivot 3×3 is a strong DEV
  discovery hypothesis but remains sample-limited and opportunity-set-sensitive; it is
  not “the best strategy” and is not promoted.
- **Rejected alternative:** selecting a candidate solely because ZERO or one cost scenario
  is positive, or promoting Pivot 3×3 without a separately registered research version.

## D-004 — Historical uncertainty is disclosed, not silently upgraded

- **Status:** Final
- DEV may carry explicitly disclosed approximate historical tick-size and funding-mark
  warnings under the existing provenance rules. VALIDATION and Locked Test remain strict
  and cannot inherit DEV approximations as verified evidence.
- **Rejected alternative:** backfilling current exchange metadata into historical periods
  or treating synthetic/approximate evidence as verified historical rules.

## D-005 — v0.2 starts as one pre-registered hypothesis, not a search grid

- **Status:** Design gate passed; design decision final. Implementation is in progress
  in the local worktree, but execution has not been authorized or established.
- The design contains one v0.1 control (`C0`) and one SMG-1 primary (`C1`), applies SMG-1
  only to 15m discovery structure, and allows no parameter sweep, post-hoc variant,
  direction filter, trigger-family deletion, or execution retuning.
- `docs/v0.2-owner-review.md` records `V0.2_DESIGN_GATE=PASS` and approves only the narrow
  implementation scope. The plan/config remain `DESIGN_ONLY` with `execution_allowed=false`;
  PIT/reproducibility gates and later phase gates must precede any economics.
- **Rejected alternative:** renaming Pivot 3×3, widening the pivot window, adding SMG-2/3,
  or using subgroup diagnostics to choose a candidate after results are known.

## D-006 — Continuity changes do not change application/research behavior

- **Status:** Final for the original continuity infrastructure task
- The original continuity task added repository truth, decisions, and handoff workflow only.
  It did not alter strategy, data, tests, research execution, frozen artifacts, or release behavior.
- **Rejected alternative:** using the continuity task as an opportunity to clean generated
  outputs, rerun research, or start the next development phase.

## D-007 — v0.2 DEV readiness is blocked before economics

- **Status:** Final for the 2026-09-20 readiness attempt
- At the time of this readiness attempt, four Windows transport failures were a non-blocking
  path-length caveat for the formal artifact root: Scan did not call transport and the formal
  receipt temporary path was 247 characters, below the failure condition. Transport was not
  changed in that attempt. Later post-release test repair does not alter this gate conclusion.
- Formal C1 is not connected to the production scanner, no C1 accepted-request set or
  request-scoped 1m/Funding lineage exists, the frozen config still disables execution, and
  the current execution-input/environment gates are blocked. Therefore DEV economics was not
  started and no economic PASS/FAIL was assigned.
- **Rejected alternative:** treating isolated SMG-1 unit tests as a completed formal scanner
  integration, or using old C0/Pivot replay inputs as if they proved C1 data completeness.
