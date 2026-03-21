# Astar Island Competition Baseline Plan

## Summary

- Build a Python 3.13, CLI-first competition client with clean architecture: `domain -> application -> infrastructure -> cli`, with manual submission as the default safety gate.
- Use the API docs as the source of truth: prediction tensors are `prediction[y][x][class]` with shape `H×W×6`, and `Ocean/Plains/Empty -> class 0`, `Settlement -> 1`, `Port -> 2`, `Ruin -> 3`, `Forest -> 4`, `Mountain -> 5`.
- Deliver one end-to-end workflow: discover active round, plan 50 viewport queries, collect observations, generate calibrated probability tensors for all seeds, validate, and submit manually.
- First repo artifacts created in implementation: `/home/coder/NMiAI/AstarIsland/implementation-plan.md` as the saved plan, and `/home/coder/NMiAI/AstarIsland/documentation.md` as the permanent experiment ledger.

## Implementation Changes

- Create `src/astarisland/` with four layers:
  - `domain`: terrain enums, prediction-class mapping, round/seed/settlement/viewport models, tensor validator, official scoring implementation, pure rule helpers.
  - `application`: use cases for `get_active_round`, `plan_observations`, `run_observations`, `build_predictions`, `submit_predictions`, `analyze_completed_round`, and `append_experiment_entry`.
  - `infrastructure`: `httpx` API client with auth, timeouts, retry/backoff, 5 req/sec simulate limit, 2 req/sec submit limit, filesystem artifact store, env-based settings, markdown log writer.
  - `cli`: Typer commands; `main.py` becomes a thin launcher and `pyproject.toml` gets scripts and dependencies.
- Adopt a fixed v1 dependency set: `httpx`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `numpy`, `tenacity`, `orjson`, `pytest`, `respx`.
- Store all run data under `data/rounds/<round_id>/`: round detail, raw observation responses, derived feature tables, predictions, submissions, analysis, and markdown summaries. Raw API payloads are immutable once written.
- Implement a 3-phase query planner:
  - Phase A: 25 coverage queries, 5 per seed, chosen greedily from a saliency map built from initial settlements, coastal land, forest-adjacent land, and uncovered buildable land.
  - Phase B: 15 repeat queries, 3 per seed, re-query the highest-dynamics Phase A windows to estimate stochastic variance.
  - Phase C: 10 adaptive queries, assigned to the highest-uncertainty windows globally, capped at 3 extra queries per seed.
- Implement the baseline predictor as a weighted blend of:
  - soft structural priors from initial terrain and port/settlement placement,
  - same-cell empirical frequencies from observed queries,
  - feature-bucket posteriors pooled across seeds in the current round,
  - historical bucket priors from completed-round analyses when available.
- Use fixed feature buckets for transfer across cells: initial terrain code, mapped prediction class, coastal flag, initial port flag, distance-to-nearest-initial-settlement bucket, distance-to-nearest-forest bucket, and local landmass membership.
- Final prediction calibration is standardized: apply soft masks, blend with a small uniform prior, enforce a `0.01` floor on every class, renormalize, then validate shape and row sums before any file save or submit.
- Add a retrospective pipeline using `/my-predictions`, `/my-rounds`, and `/analysis/{round_id}/{seed_index}` to compute score breakdowns, calibration gaps, dynamic-cell hotspots, and learned priors for later rounds.

## Public Interfaces And Artifacts

- CLI commands:
  - `astar-island round active`
  - `astar-island observe plan --round-id <id>`
  - `astar-island observe run --round-id <id>`
  - `astar-island predict --round-id <id>`
  - `astar-island submit --round-id <id> --all-seeds`
  - `astar-island analyze --round-id <id>`
- Core types exposed across layers: `Settings`, `RoundDetail`, `InitialState`, `ObservationRequest`, `ObservationResult`, `ObservationPlan`, `PredictionTensor`, `SubmissionResult`, `AnalysisResult`, `ExperimentEntry`.
- `/home/coder/NMiAI/AstarIsland/documentation.md` becomes mandatory project memory with these sections:
  - `Stable Assumptions`
  - `Attempt Log`
  - `Mistakes To Avoid Repeating`
  - `Validated Heuristics`
- Every observation campaign, prediction build, submission, and post-round analysis appends one entry containing timestamp, round id, hypothesis, query allocation, model version, outcome, score if known, and one explicit “do not repeat” note.

## Test Plan

- Unit tests for terrain mapping, tensor validation, probability flooring/renormalization, scoring math, feature extraction, query-budget accounting, and query-planner phase allocation.
- Contract tests with mocked API responses for auth handling, round discovery, viewport clamping, submission validation failures, rate-limit retries, exhausted budget, and overwrite-on-resubmit behavior.
- End-to-end dry-run test from stored fixtures: fetch round detail, generate observation plan, ingest mock observations, build `H×W×6` predictions for all seeds, and produce submit-ready payloads without network access.
- Regression tests for retrospective learning: completed-round analysis must reproduce the official score formula locally and the baseline predictor must beat a uniform prediction on archived round fixtures before model changes are accepted.

## Assumptions And Required Inputs

- User action required for live API usage: provide the `access_token` JWT in env configuration; bearer auth is the default, but the same token can also be used as a cookie if needed.
- V1 is CLI-only; no notebook UI or web dashboard is included in the first implementation.
- Manual submission remains the default even after prediction generation; automation stops before the final API write unless the explicit submit command is run.
- Docs inconsistencies resolved intentionally:
  - `simulationmechanics.md` line 6 is treated as a typo; Plains maps to class `0`, not `2`.
  - `overview.md` uses `W×H×6`, but implementation follows the API and quickstart `H×W×6`.
  - Even where endpoints are labeled “Public,” the client will send auth whenever available and will not depend on anonymous access.

