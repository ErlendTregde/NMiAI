# Astar Island Experiment Ledger

## Stable Assumptions

- API docs are treated as the source of truth for tensor ordering and class mapping.
- Predictions use `H×W×6` ordering and class `0` means `Ocean/Plains/Empty`.
- Manual submission remains the default safety gate.

## Attempt Log

<!-- command workflows append entries above the mistakes section -->

### 2026-03-20T12:53:34.175410+00:00 | observe | round 36e581f1-73f8-453f-ab98-cbe3052b701b
- Hypothesis: Coverage, repeats, and adaptive follow-ups will estimate both state transitions and uncertainty.
- Query allocation: Phase A 25 / Phase B 15 / Phase C 10
- Model version: v1-baseline
- Outcome: Executed 50 observations and saved raw payloads under data/rounds/36e581f1-73f8-453f-ab98-cbe3052b701b/observations/raw.
- Score: pending
- Do not repeat: Do not spend reserved Phase B/C budget on extra coverage before variance has been measured.
- Note: Observation summary saved under summaries/observation-run.md

### 2026-03-20T12:53:53.850212+00:00 | predict | round 36e581f1-73f8-453f-ab98-cbe3052b701b
- Hypothesis: Blending structural priors with same-cell, round-bucket, and historical posteriors should beat a uniform baseline.
- Query allocation: Prediction uses all saved observations for the round.
- Model version: v1-baseline
- Outcome: Built 5 calibrated prediction tensors.
- Score: pending
- Do not repeat: Do not save or submit predictions before enforcing probability floors and row-sum validation.
- Note: Prediction summary saved under summaries/prediction-summary.md

### 2026-03-20T12:55:19.768973+00:00 | submit | round 36e581f1-73f8-453f-ab98-cbe3052b701b
- Hypothesis: Manual submission after validation is safer than unattended writes to the competition API.
- Query allocation: Submission only; no additional queries consumed.
- Model version: v1-baseline
- Outcome: Submitted seeds [0, 1, 2, 3, 4].
- Score: pending
- Do not repeat: Do not call submit before confirming the intended seed set.
- Note: Submission summary saved under summaries/submission-summary.md

### Round 7 Snapshot | 36e581f1-73f8-453f-ab98-cbe3052b701b
- Status after submission: `active`
- Live state checked after submission: `queries_used=50/50`, `seeds_submitted=5/5`, `round_score=null`
- Observation execution completed all planned phases: `A=25`, `B=15`, `C=10`
- Per-seed observation counts: seed 0 = 10, seed 1 = 10, seed 2 = 10, seed 3 = 9, seed 4 = 11
- Per-seed observed-cell counts used by the predictor: seed 0 = 956, seed 1 = 727, seed 2 = 1025, seed 3 = 668, seed 4 = 1137
- Prediction tensors saved for all 5 seeds and validated with exact row sums of `1.0`
- Follow-up trigger: run `uv run astar-island analyze --round-id 36e581f1-73f8-453f-ab98-cbe3052b701b` only after the round status becomes `scoring` or `completed`
- Operational reminder: check both observation count and observed-cell count by seed before future submissions so seed coverage imbalance is visible earlier

### 2026-03-20T14:52:43.307983+00:00 | analyze | round 36e581f1-73f8-453f-ab98-cbe3052b701b
- Hypothesis: Completed-round analysis should expose calibration gaps and produce better historical bucket priors for later rounds.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v1-baseline
- Outcome: Analyzed 5 seeds with average local score 23.083.
- Score: 23.082815441013725
- Do not repeat: Do not discard completed-round analysis artifacts; they are the historical prior source for future predictions.
- Note: Analysis summary and feature-bucket priors were saved under the round analysis directory.

### Round 7 Postmortem | 36e581f1-73f8-453f-ab98-cbe3052b701b
- Final API scores: seed 0 = 20.6477, seed 1 = 22.5316, seed 2 = 25.6852, seed 3 = 21.3555, seed 4 = 25.1941, average = 23.0828
- Main scoring failure: overall argmax accuracy looked high on the full map, but entropy-weighted KL punished the uncertain cells where the model was too confident and too conservative
- Dynamic-cell problem: on entropy-heavy cells the model assigned only about `0.10-0.12` settlement probability on average, while ground truth was about `0.30-0.34`
- Very-dynamic-cell problem: on the highest-entropy cells the model still over-assigned `Empty` and `Forest`, and under-assigned `Settlement` and `Port`
- Coverage problem: many of the worst weighted-KL cells were outside observed windows, especially for seeds 0, 1, and 3
- Inference problem: even on covered dynamic cells, weighted error stayed high, so better coverage alone would not have fixed the score
- Missed signal: the baseline did not use observed settlement stats like `population`, `food`, `wealth`, `defense`, or `owner_id` to model expansion, collapse, or port survival
- Round 8 priority: optimize for dynamic-cell probability quality, not full-map argmax accuracy
- Round 8 priority: increase probability mass for plausible settlement and port transitions near active frontiers
- Round 8 priority: reduce confidence on uncertain cells instead of defaulting to very confident `Empty`

### Round 8 Upgrade Benchmark | archived Round 7 replay
- Implemented `v2-dynamic-frontier` with `20/20/10` planning, repeated-window summaries, frontier pressure fields, 2-stage dynamic-mass prediction, conservative-class caps, and learned frontier priors aggregated at `data/learned/frontier-priors.json`
- Offline replay benchmark used archived Round 7 observations plus completed-round analysis artifacts as the learned-prior source
- Replayed seed scores after the upgrade: seed 0 = 31.4600, seed 1 = 32.6728, seed 2 = 31.4274, seed 3 = 32.4705, seed 4 = 31.0364, average = 31.8134
- Acceptance target met: replay average improved from `23.0828` to `31.8134`, and every seed beat the original Round 7 submission
- Most important winning changes: explicit frontier uncertainty, stronger settlement and port mass near active frontiers, repeated-window variance, and historical shrinkage from completed-round frontier priors
- Do not repeat: do not benchmark the upgraded model only on fixture data; keep the archived-round replay test because it catches the exact dynamic-cell failure mode that the small fixture hides

### 2026-03-20T16:35:08.743416+00:00 | observe | round c5cdf100-a876-4fb7-b5d8-757162c97989
- Hypothesis: Frontier-aware coverage, exact repeats, and adaptive hotspot follow-ups will expose the high-entropy cells that drive score.
- Query allocation: Phase A 20 / Phase B 20 / Phase C 10
- Model version: v2-dynamic-frontier
- Outcome: Executed 50 observations and saved raw payloads under data/rounds/c5cdf100-a876-4fb7-b5d8-757162c97989/observations/raw.
- Score: pending
- Do not repeat: Do not skip repeated corridor windows; the predictor now depends on measured frontier variance and settlement-stat volatility.
- Note: Observation summary saved under summaries/observation-run.md
- Note: Window dynamics summaries saved under observations/window-dynamics.json

### 2026-03-20T16:40:03.396425+00:00 | predict | round c5cdf100-a876-4fb7-b5d8-757162c97989
- Hypothesis: A 2-stage dynamic-frontier model should raise settlement and port mass on uncertain frontier cells without breaking static-cell calibration.
- Query allocation: Prediction uses all saved observations for the round.
- Model version: v2-dynamic-frontier
- Outcome: Built 5 calibrated prediction tensors.
- Score: pending
- Do not repeat: Do not let frontier-eligible cells collapse back to very confident Empty when variance and pressure fields indicate dynamic risk.
- Note: Prediction summary saved under summaries/prediction-summary.md

### 2026-03-20T16:40:16.873318+00:00 | submit | round c5cdf100-a876-4fb7-b5d8-757162c97989
- Hypothesis: Manual submission after validation is safer than unattended writes to the competition API.
- Query allocation: Submission only; no additional queries consumed.
- Model version: v2-dynamic-frontier
- Outcome: Submitted seeds [0, 1, 2, 3, 4].
- Score: pending
- Do not repeat: Do not call submit before confirming the intended seed set.
- Note: Submission summary saved under summaries/submission-summary.md

### 2026-03-21T08:45:49.479584+00:00 | observe | round 7b4bda99-6165-4221-97cc-27880f5e6d95
- Hypothesis: Frontier-aware coverage, exact repeats, and adaptive hotspot follow-ups will expose the high-entropy cells that drive score.
- Query allocation: Phase A 20 / Phase B 20 / Phase C 10
- Model version: v2-dynamic-frontier
- Outcome: Executed 50 observations and saved raw payloads under data/rounds/7b4bda99-6165-4221-97cc-27880f5e6d95/observations/raw.
- Score: pending
- Do not repeat: Do not skip repeated corridor windows; the predictor now depends on measured frontier variance and settlement-stat volatility.
- Note: Observation summary saved under summaries/observation-run.md
- Note: Window dynamics summaries saved under observations/window-dynamics.json

### 2026-03-21T08:47:46.154569+00:00 | predict | round 7b4bda99-6165-4221-97cc-27880f5e6d95
- Hypothesis: A 2-stage dynamic-frontier model should raise settlement and port mass on uncertain frontier cells without breaking static-cell calibration.
- Query allocation: Prediction uses all saved observations for the round.
- Model version: v2-dynamic-frontier
- Outcome: Built 5 calibrated prediction tensors.
- Score: pending
- Do not repeat: Do not let frontier-eligible cells collapse back to very confident Empty when variance and pressure fields indicate dynamic risk.
- Note: Prediction summary saved under summaries/prediction-summary.md

### 2026-03-21T08:48:09.744056+00:00 | submit | round 7b4bda99-6165-4221-97cc-27880f5e6d95
- Hypothesis: Manual submission after validation is safer than unattended writes to the competition API.
- Query allocation: Submission only; no additional queries consumed.
- Model version: v2-dynamic-frontier
- Outcome: Submitted seeds [0, 1, 2, 3, 4].
- Score: pending
- Do not repeat: Do not call submit before confirming the intended seed set.
- Note: Submission summary saved under summaries/submission-summary.md

### 2026-03-21T10:07:58.450238+00:00 | analyze | round 7b4bda99-6165-4221-97cc-27880f5e6d95
- Hypothesis: Completed-round analysis should expose false-positive frontier activation, refresh fitted training data, and retrain the offline active-frontier bundle.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v3-active-frontier-gating
- Outcome: Analyzed 5 seeds with average local score 29.793.
- Score: 29.793261510479645
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json

### 2026-03-21T11:18:17.509955+00:00 | observe | round d0a2c894-2162-4d49-86cf-435b9013f3b8
- Hypothesis: Active-frontier coverage with exact repeats, positive follow-ups, and hard-negative checks should reduce false dynamic predictions while preserving dynamic-cell recall.
- Query allocation: Phase A 20 / Phase B 20 / Phase C 10
- Model version: v3-active-frontier-gating
- Outcome: Executed 50 observations and saved raw payloads under data/rounds/d0a2c894-2162-4d49-86cf-435b9013f3b8/observations/raw.
- Score: pending
- Do not repeat: Do not spend all adaptive windows on frontier-positive guesses; the fitted model also needs hard-negative evidence to suppress false dynamic mass.
- Note: Observation summary saved under summaries/observation-run.md
- Note: Window dynamics summaries saved under observations/window-dynamics.json

### 2026-03-21T11:18:47.182020+00:00 | predict | round d0a2c894-2162-4d49-86cf-435b9013f3b8
- Hypothesis: A fitted 3-stage active-frontier model should gate dynamic cells first, then calibrate dynamic mass and class splits against learned round history.
- Query allocation: Prediction uses all saved observations for the round.
- Model version: v3-active-frontier-gating
- Outcome: Built 5 calibrated prediction tensors.
- Score: pending
- Do not repeat: Do not let total dynamic mass drift far above observed plus learned round priors; Round 13 showed that false frontier activation destroys score.
- Note: Prediction summary saved under summaries/prediction-summary.md

### 2026-03-21T11:19:10.045674+00:00 | submit | round d0a2c894-2162-4d49-86cf-435b9013f3b8
- Hypothesis: Manual submission after validation is safer than unattended writes to the competition API.
- Query allocation: Submission only; no additional queries consumed.
- Model version: v3-active-frontier-gating
- Outcome: Submitted seeds [0, 1, 2, 3, 4].
- Score: pending
- Do not repeat: Do not call submit before confirming the intended seed set.
- Note: Submission summary saved under summaries/submission-summary.md

### 2026-03-21T12:04:10.063308+00:00 | analyze | round d0a2c894-2162-4d49-86cf-435b9013f3b8
- Hypothesis: Completed-round analysis should expose false-positive frontier activation, refresh fitted training data, and retrain the offline active-frontier bundle.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v3-active-frontier-gating
- Outcome: Analyzed 5 seeds with average local score 39.169.
- Score: 39.169362648863505
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json

### 2026-03-21T14:29:33.640171+00:00 | observe | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: Active-frontier coverage with exact repeats, positive follow-ups, and hard-negative checks should reduce false dynamic predictions while preserving dynamic-cell recall.
- Query allocation: Phase A 20 / Phase B 20 / Phase C 10
- Model version: v4-regime-calibrated-heuristic
- Outcome: Executed 50 observations and saved raw payloads under data/rounds/cc5442dd-bc5d-418b-911b-7eb960cb0390/observations/raw.
- Score: pending
- Do not repeat: Do not spend all adaptive windows on frontier-positive guesses; the fitted model also needs hard-negative evidence to suppress false dynamic mass.
- Note: Observation summary saved under summaries/observation-run.md
- Note: Window dynamics summaries saved under observations/window-dynamics.json

### 2026-03-21T14:29:49.294648+00:00 | predict | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: A regime-based heuristic should shrink total dynamic mass toward observed and learned targets while suppressing unsupported Port and Ruin mass.
- Query allocation: Prediction uses all saved observations for the round.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Built 5 calibrated prediction tensors.
- Score: pending
- Do not repeat: Do not let Port and Ruin absorb frontier mass without direct trade or collapse evidence.
- Note: Prediction summary saved under summaries/prediction-summary.md

### 2026-03-21T14:30:23.043932+00:00 | submit | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: Manual submission after validation is safer than unattended writes to the competition API.
- Query allocation: Submission only; no additional queries consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Submitted seeds [0, 1, 2, 3, 4].
- Score: pending
- Do not repeat: Do not call submit before confirming the intended seed set.
- Note: Submission summary saved under summaries/submission-summary.md

### 2026-03-21T14:31:57.354775+00:00 | analyze | round 36e581f1-73f8-453f-ab98-cbe3052b701b
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 23.083.
- Score: 23.082815441013725
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T14:33:02.815658+00:00 | analyze | round 7b4bda99-6165-4221-97cc-27880f5e6d95
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 29.793.
- Score: 29.793261510479645
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T14:33:08.401051+00:00 | analyze | round d0a2c894-2162-4d49-86cf-435b9013f3b8
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 39.169.
- Score: 39.169362648863505
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T14:33:31.464193+00:00 | predict | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: A regime-based heuristic should shrink total dynamic mass toward observed and learned targets while suppressing unsupported Port and Ruin mass.
- Query allocation: Prediction uses all saved observations for the round.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Built 5 calibrated prediction tensors.
- Score: pending
- Do not repeat: Do not let Port and Ruin absorb frontier mass without direct trade or collapse evidence.
- Note: Prediction summary saved under summaries/prediction-summary.md

### 2026-03-21T14:33:47.006993+00:00 | submit | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: Manual submission after validation is safer than unattended writes to the competition API.
- Query allocation: Submission only; no additional queries consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Submitted seeds [0, 1, 2, 3, 4].
- Score: pending
- Do not repeat: Do not call submit before confirming the intended seed set.
- Note: Submission summary saved under summaries/submission-summary.md

### 2026-03-21T15:02:19.979903+00:00 | analyze | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 60.320.
- Score: 60.31954126849958
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### Round 15 Postmortem | cc5442dd-bc5d-418b-911b-7eb960cb0390
- Final API scores: seed 0 = 60.4746, seed 1 = 59.4310, seed 2 = 60.8541, seed 3 = 59.5536, seed 4 = 61.2844, average = 60.3195
- Major improvement: `v4-regime-calibrated-heuristic` raised the live score from Round 14 `39.169` to Round 15 `60.320`
- Operational lesson: the first Round 15 build used `Calibration rounds: 0`; refreshing completed-round analyses before the rebuild changed the live submission to `Calibration rounds: 3`
- Dynamic-mass problem is much smaller now: per-seed dynamic mass gap is only about `+0.035` to `+0.061`
- Remaining high-entropy class gaps: `Settlement` is slightly low at about `-0.002` to `-0.018`, `Port` is still high at about `+0.020` to `+0.026`, and `Ruin` is still high at about `+0.028` to `+0.035`
- Key structural finding: `collapse_frontier` and `maritime_frontier` never activated in the final Round 15 prediction summary, so most excess `Port` and `Ruin` mass is leaking out of `growth_frontier`
- Top remaining error mode: high-entropy settlement spikes still get underpredicted badly on some cells, with truth settlement around `0.33-0.43` while prediction stays around `0.03-0.07`
- Next-round priority: rewrite the `growth_frontier` dynamic split so unsupported `Port` and `Ruin` are much smaller and the leftover mass goes to `Settlement`
- Fitted-model status: the `v3-active-frontier-gating` bundle is still not production ready; its replay on Round 15 stayed around `8.318`, so the heuristic safety gate is still correct

### 2026-03-21T15:55:10.255444+00:00 | analyze | round 36e581f1-73f8-453f-ab98-cbe3052b701b
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 23.083.
- Score: 23.082815441013725
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T15:56:32.487642+00:00 | analyze | round 7b4bda99-6165-4221-97cc-27880f5e6d95
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 29.793.
- Score: 29.793261510479645
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T15:57:51.988314+00:00 | analyze | round d0a2c894-2162-4d49-86cf-435b9013f3b8
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 39.169.
- Score: 39.169362648863505
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T15:59:06.688539+00:00 | analyze | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 60.320.
- Score: 60.31954126849958
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T16:00:06.972023+00:00 | analyze | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 60.320.
- Score: 60.31954126849958
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T16:31:43.119567+00:00 | analyze | round 36e581f1-73f8-453f-ab98-cbe3052b701b
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 23.083.
- Score: 23.082815441013725
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T16:33:03.918525+00:00 | analyze | round 7b4bda99-6165-4221-97cc-27880f5e6d95
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 29.793.
- Score: 29.793261510479645
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T16:34:22.917237+00:00 | analyze | round d0a2c894-2162-4d49-86cf-435b9013f3b8
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 39.169.
- Score: 39.169362648863505
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T16:35:41.513438+00:00 | analyze | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 60.320.
- Score: 60.31954126849958
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T16:36:01.508443+00:00 | analyze | round cc5442dd-bc5d-418b-911b-7eb960cb0390
- Hypothesis: Completed-round analysis should refresh both the learned frontier priors and the lightweight heuristic calibration used by the production predictor.
- Query allocation: Analysis endpoint fetches only; no simulation budget consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Analyzed 5 seeds with average local score 60.320.
- Score: 60.31954126849958
- Do not repeat: Do not let global dynamic mass drift far above observed plus learned round priors.
- Note: Analysis summary saved under summaries/analysis-summary.md
- Note: Frontier priors saved under analysis/frontier-priors.json and data/learned/frontier-priors.json
- Note: Fitted model artifacts saved under data/learned/frontier-model.joblib and data/learned/frontier-model-metadata.json
- Note: Heuristic calibration artifacts saved under data/learned/heuristic-calibration.json and data/learned/heuristic-backtest-summary.json

### 2026-03-21T17:40:29.644979+00:00 | observe | round 8f664aed-8839-4c85-bed0-77a2cac7c6f5
- Hypothesis: Active-frontier coverage with exact repeats, positive follow-ups, and hard-negative checks should reduce false dynamic predictions while preserving dynamic-cell recall.
- Query allocation: Phase A 20 / Phase B 20 / Phase C 10
- Model version: v4-regime-calibrated-heuristic
- Outcome: Executed 50 observations and saved raw payloads under data/rounds/8f664aed-8839-4c85-bed0-77a2cac7c6f5/observations/raw.
- Score: pending
- Do not repeat: Do not spend all adaptive windows on frontier-positive guesses; the fitted model also needs hard-negative evidence to suppress false dynamic mass.
- Note: Observation summary saved under summaries/observation-run.md
- Note: Window dynamics summaries saved under observations/window-dynamics.json

### 2026-03-21T17:40:46.031119+00:00 | predict | round 8f664aed-8839-4c85-bed0-77a2cac7c6f5
- Hypothesis: A regime-based heuristic should shrink total dynamic mass toward observed and learned targets while suppressing unsupported Port and Ruin mass.
- Query allocation: Prediction uses all saved observations for the round.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Built 5 calibrated prediction tensors.
- Score: pending
- Do not repeat: Do not let Port and Ruin absorb frontier mass without direct trade or collapse evidence.
- Note: Prediction summary saved under summaries/prediction-summary.md

### 2026-03-21T17:41:02.763718+00:00 | submit | round 8f664aed-8839-4c85-bed0-77a2cac7c6f5
- Hypothesis: Manual submission after validation is safer than unattended writes to the competition API.
- Query allocation: Submission only; no additional queries consumed.
- Model version: v4-regime-calibrated-heuristic
- Outcome: Submitted seeds [0, 1, 2, 3, 4].
- Score: pending
- Do not repeat: Do not call submit before confirming the intended seed set.
- Note: Submission summary saved under summaries/submission-summary.md

## Mistakes To Avoid Repeating

- Do not resubmit unvalidated tensors.
- Do not spend the full query budget before reserving repeat and adaptive windows.
- Do not judge the model by full-map argmax accuracy alone; static cells hide failures on the entropy-weighted cells that determine score.
- Do not let the baseline default to `~0.89 Empty` on cells that are plausibly settlement or port frontiers.
- Do not ignore settlement-level stats returned by the simulator; they are some of the only direct clues about growth, collapse, and maritime activity.
- Do not let global dynamic mass drift far above observed plus learned round priors; Round 13 showed that false frontier activation and Port overprediction can dominate the score.
- Do not promote the fitted `v3-active-frontier-gating` bundle into production until its archived replay scores clear the safety gate; the heuristic path is currently much stronger.
- Do not start a live round on a newly changed heuristic without checking `Calibration rounds` in `summaries/prediction-summary.md`; Round 15 initially built with `Calibration rounds: 0` until completed-round analyses were refreshed.
- Do not assume `Port` and `Ruin` false positives come from maritime or collapse regimes; Round 15 showed they were leaking from `growth_frontier` even when those regimes never activated.

## Validated Heuristics

- Historical completed-round analyses should be stored because they become reusable priors.
- Per-seed observed-cell counts are a useful last-minute sanity check before submission, not just the raw query count.
- Compare predicted class marginals against ground-truth marginals after each round; large settlement underprediction is a red flag.
- Hard-negative windows are useful training data, not wasted budget; they help the fitted gate learn where dynamic mass should stay low.
- A regime-based heuristic with seed-level target calibration is currently the best production path; archived replays reached roughly `47.6` on Round 7, `66.0` on Round 13, and `56.8` on Round 14.
- Non-coastal `Port <= 0.02`, weak-maritime coastal `Port <= 0.05`, and unsupported `Ruin <= 0.08` are strong default caps.
- Growth-frontier cells need explicit Settlement uplift after global dynamic shrink; otherwise the model redistributes too much mass into Port and Ruin.
- Refreshing completed-round analyses before a live rebuild is worth doing when the heuristic calibration logic changes; it converted Round 15 from a default-calibration run into a persisted-calibration run.
- The archived heuristic backtest average of about `59.9` was a useful real-world guide; the live Round 15 score landed at `60.32`.
- The next likely gain is not more global dynamic shrink; it is a better local class split that moves unsupported `Port` and `Ruin` mass into `Settlement` on high-entropy growth-frontier cells.
