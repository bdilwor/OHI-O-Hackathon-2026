# Outage-risk model: assumptions and limitations

All assumptions behind the outage-risk estimate (`ml/score.py`) and the repair-duration helper
(`ml/duration.py`) are listed here. Numbers come from the current training run; see
`artifacts/METRICS.md` and `artifacts/metrics.json`.

## What the model predicts

- **Asset model:** P(a given pole, line segment, fuse, transformer, recloser or switch is damaged in a storm).
- **Premise estimate:** P(the premise has *any* interruption during a storm scenario), built from the asset probabilities (see below).
- These are **model estimates, not confirmed utility information.** Every response carries `"source": "model_estimate"` and a disclaimer. When the model can't give a reliable answer, it returns `"fallback": true` with a plain-language message.

## Data

| Assumption | Detail |
|---|---|
| Training source | 1,659 ARCOS damage records, Roanoke VA area, Oct 2022 – Aug 2026; 333,010 GIS assets. |
| No weather data | Storm severity is a **scenario input**, not something the model forecasts. |
| Day-level dates | ARCOS has no time of day, so durations are whole days. |
| Damage type | `Asset_Type` → `Inform_Wor` is 1:1 in this extract, so the label is simply "damaged". |
| Not modeled | Capacitor Bank, Light, Step Transformer and blank asset types (13 records): too few to learn from. |

## Storms and validation

- **Storm events:** report dates no more than 3 days apart are chained into one event, which gives 35 events. Three are "major" (≥40 records): Sep 2024 (194), Jan 2025 (46) and Feb 2025 (1,254). The other 32 are minor (≤17 records each).
- **Validation is leave-one-*major*-storm-out** (4 folds: each major storm, plus all minor events pooled). Strict leave-one-storm-out would create folds with 1–3 damaged assets, where PR-AUC is noise. Each fold scores **every** asset in the held-out storm(s); minor-event folds keep a 10% sample of negatives, weighted back.
- **Metrics:** PR-AUC (with lift over the base rate), ROC-AUC, Brier score, and recall in the top 1% / 5% of assets. Accuracy is not reported: at a 0.013% positive rate, predicting "no damage" everywhere is more than 99.98% accurate.

## Labels

- Each ARCOS point is snapped to the nearest asset of the matching type, **same circuit first**, within **30 m**. A 60 m fallback is used only if a type's match rate falls below 80%; none did.
- Match rates: Pole 99.7%, primary 99.4%, secondary 100%, Fuse 99.3%, Transformer 98.6%, Recloser 96.6%, Switch 84.2%.
- A positive is an (asset, storm) pair with at least one matched report: 1,563 positives out of 11.66M pairs (0.0134%).
- **Negative downsampling:** training keeps all positives, 5% of negatives in major storms (weight ×20) and 1% in minor events (weight ×100). The weights restore the true base rate.

## Features

- **Asset:** type, age at storm date, pole material / height / strength class, overhead vs. underground, line usage (backbone / lateral / service), bare conductor, top-15 wire specs, voltage, segment length, transformer kVA, fuse size, equipment subtype, owner type.
- **Network:** customers downstream; number of devices and reclosers upline (from the `upstream_l` chain).
- **History:** computed **only from storms strictly before the scored storm**, asserted in code:
  - the share of all prior damage within 250 m / 500 m
  - the share on the same circuit
  - whether this exact asset was damaged before
  - the access-issue rate of nearby past repairs
  - History is expressed as **shares, not counts**. Raw counts only grow over time, which made later storms look systematically different. Switching to shares raised CV PR-AUC lift for the unchanged models: logistic regression 6.9× → 7.8×, random forest 5.8× → 6.0×.
- **Storm:** `storm_log_damage` = log(1 + damage reports in the storm).
- **Missing data:** about 50% of poles lack an install date, and about 18% have unknown height or class. Logistic regression imputes the median and adds missing-indicator columns; tree models handle NaN natively.
- **Privacy:** `critical_c` (life support), `special_op` and `esri_defau` (threat) are in `customer_private`, which the ML code never reads. No customer field is a model feature. Customer location is used only to find nearby equipment.

## Model choice

| Model (mean of 4 folds) | PR-AUC lift | ROC-AUC | Recall in top 5% |
|---|---|---|---|
| **Logistic regression (selected)** | **7.8×** | **0.808** | **0.34** |
| Random forest | 6.0× | 0.771 | 0.34 |
| LightGBM | 5.1× | 0.792 | 0.32 |
| LightGBM without history features (ablation) | 6.0× | 0.793 | 0.34 |

- The simplest and most explainable model generalized best to unseen storms. With about 1.5k positives concentrated in one storm, the tree models overfit storm-specific patterns. LightGBM used a conservative configuration chosen up front, with no hyperparameter search on the CV folds.
- **Calibration:** sigmoid `CalibratedClassifierCV` with the same storm-grouped folds. Raw class-weighted logistic regression over-predicts by about 100×; the calibrated version follows the diagonal (`artifacts/calibration.png`).
- **Explanations:** SHAP values (log-odds) per asset; the top-3 risk-raising factors are stored per asset. The storm-size feature is excluded from "why *you*" factors because it is the same for everyone in a scenario.

## Severity scenarios

| Scenario | Modeled on | Damage reports |
|---|---|---|
| low | median of minor events with 10–39 reports | 12 |
| medium | September 2024 storm | 194 |
| high | February 2025 storm | 1,254 |

- **Scenario anchoring:** the model decides **where** damage is likely (relative risk), and the scenario decides **how much** in total. Each scenario's asset probabilities are rescaled so the expected number of damaged assets equals the reference storm's (reports × 0.94 damaged assets per report).
  - Without this, pooled calibration (dominated by the 32 minor events) under-predicts big storms by about 2–3×.
  - Scale factors: low ×0.80, medium ×1.92, high ×3.35. Per-asset probabilities are capped at 0.95.

## From assets to a premise

- **Exposure set for a premise:**
  - **your equipment:** the transformer, fuses, reclosers and switches on its upline path (from `matched_upstream.txt`), plus the nearest service/secondary segment within 60 m.
  - **nearby:** primary segments and poles on the same circuit within **250 m** of its transformer.
  - **main line:** all backbone primary segments of its circuit.
- **Why approximate:** ARCOS and the GIS have no segment-to-device connectivity, so line exposure is approximated by distance and circuit. A backbone fault is assumed to interrupt everyone on the circuit (an upper bound: reclosers and step restoration often limit it).
- **Combining:** `P(outage) = 1 − Π(1 − p_asset)`, treating asset failures as **independent**. In reality, damage clusters in a storm (same wind, same trees), so this may overstate risk when many assets are each slightly risky.
- **Any interruption counts,** including momentary ones.
- **Bands:** Low < 5%, Medium 5–20%, High 20–50%, Very High ≥ 50%.
- **Confidence:**
  - High: the premise's circuit has ≥ 20 historical damage records and the premise is in the topology.
  - Medium: the circuit has ≥ 5 records.
  - Low: anything else.
- **Fallback:** an unknown premise, or a map point more than 150 m from any known service location, returns `risk_band: "Unknown"`, `fallback: true`, and general-guidance messaging.
- **Output privacy:** no asset IDs, coordinates, circuit names, raw wire specs or customer fields. Map-point queries don't reveal which premise they snapped to.

## Repair-duration helper

- **Model:** LightGBM quantile regression (p10 / p50 / p90) on 1,583 repaired records. Target: whole days from report to repair. Features: ARCOS asset type, access issue, special equipment.
- **Output:** a **multiplier** (`(p50 + 1) / (typical p50 + 1)`) for the labor-hour restoration estimator, not an ETA.
- **Honest result:** out-of-storm, the medians are **no better than a constant baseline**, and p10–p90 coverage is 34–74% (nominal 80%).
  - Repair spans are dominated by each storm's backlog, which nothing in ARCOS predicts.
  - Storm size was removed as a feature: with only 35 storms it memorized each storm's backlog (it once gave a 23-day median for a routine wire repair).
  - The useful signal is the relative effect: hard access plus special equipment gives about 1.5× longer repairs.
- **Censoring:** the 76 unrepaired records are excluded. Their `Repaired_D` values in the source look like placeholder or extract dates. Excluding them biases durations short.
- **`Media`:** excluded because its meaning isn't documented.

## Conditions that would change the answer

- A real weather forecast (wind, ice, rain): not available, so severity is a scenario.
- Live outage or damage reports during a storm: the estimate is pre-storm, not live.
- Crew counts, mutual aid, road access: these affect restoration time, not damage risk.
- Vegetation management, recent hardening or rebuilds: not in the GIS extract.

---

# Restoration-time estimate (`backend/restoration.py`)

No ETA model is trained, because ARCOS dates are day-level. Restoration time is built from labor hours.

## Labor data
- **Source:** STORMS labor hours per compatible-unit (CU) group from `Install_Labor_HackOHIO.xlsx`, loaded as the `labor_cu_group_avg` table.
- **Indirect labor multiplier:** **4.5**, from the same workbook. It is applied to all labor hours to cover travel, safety briefings and setup.
- **What a repair job contains** comes from AEP distribution-standard drawings (`data/structure_drawings.pdf`). Each labeled assembly maps to a STORMS CU group:

| Drawing assembly (D.S.) | STORMS CU group |
|---|---|
| Pin & insulator (30) | Pin + Insulator |
| Crossarm (20 / 26) | Crossarm |
| Primary dead-end (31), neutral dead-end (501) | Dead-End |
| Neutral assembly (500), twisted secondary (502), risers (1002) | Secondary Conductor Attachment |
| Guying assembly (400) | Guying + Anchor |
| Grounding assembly (65) | Grounding |
| Connector / hot-line tap (70, 74, 75-A) | Connector |
| Cutout + fuse link (61, 1001) | Fuse Cutout |
| Brackets (32-x, 51/52) | Bracket |
| #4 Cu lead (75-A) | Equipment Lead |
| Transformer (1000) | Transformer (by kVA) |

Wire ties (D.S. 40) and arresters (D.S. 62) have no STORMS CU; arresters are counted through their bracket and lead.

## Repair bundles
Replaced parts count removal + install hours; new connectors count install only.

| Damage | Bundle | Crew-hours incl. ×4.5 (mean) |
|---|---|---|
| Pole | Pole sized from GIS height, plus re-framing sampled from the drawings: single-phase tangent, p.5 (50%); three-phase crossarm tangent, p.9 (30%); three-phase dead-end, p.3/p.8 (20%) | ~22 |
| Primary wire | Per phase: 2 connectors + 1 insulator, plus a neutral connector. Single-phase (70%) or three-phase (30%) | ~4.7 |
| Secondary wire | 2 secondary attachments + 2 connectors (D.S. 502) | ~5.2 |
| Service drop | Service CU | ~9.8 |
| Fuse | Cutout + bracket + hot-line-tap connector + lead | ~10.5 |
| Transformer | Transformer (kVA from GIS) + cutout + 2 brackets + 2 leads + 2 connectors + riser + grounding (p.2) | ~33 |
| Recloser / switch | No drawing. Assumed device + 3/2 leads + 3/2 connectors + bracket + grounding | ~26 / ~29 |

- The **structure mix** (50/30/20) and the **phase mix** (70/30) are assumptions: the GIS has no framing or phase-count field.
- **Wire jobs are a lower bound.** STORMS has no conductor-stringing CU, so a long wire-down repair can take longer than estimated.

## Simulation
- **Damage:**
  - The premise's exposure set plus every other asset on its circuit is sampled as Bernoulli(p) from `asset_risk` at the scenario severity.
  - **Conditional on the premise being out:** a run with no damage on the premise's path gets one path asset damaged, drawn in proportion to p.
  - Once damage is *assessed* (storm timeline), confirmed damage replaces the sampled damage on the premise's path.
  - What-if `extra_damage` adds jobs on the premise's path.
- **Job hours:** lognormal around the bundle mean (σ = 0.35, mean-preserving).
- **Blocked access** adds the Access Constraint (0.5 h) and Remote Location (1.312 h) adders × ILM to each path job. It also applies the ARCOS repair-history multiplier from `ml.duration`, but that is 1.0 in practice: historical median repair time doesn't change with access issues alone.
- **Upstream-first sequencing:** tiers are (1) reclosers, switches and backbone, (2) fuses, laterals and poles, (3) transformers, secondary and service. The premise waits for:
  - all circuit work in the tiers above its deepest damaged tier,
  - its own work at that tier,
  - a Uniform(0–1) share of other same-tier work, since crews may reach other branches first.
- **Time** = `crew_arrival_delay_hrs + crew-hours / crews`. Crews are assumed dedicated to the premise's circuit.
- **Output:** P10/P50/P90 over 2,000 runs, rounded to 0.5 h, seeded so results are reproducible.
  - Results are stable to about ±0.5 h across seeds and row orderings.
  - The **bottleneck** is the upline device (named generically, from the topology) whose tier contributes the most expected time.

## Demo storm timeline (`backend/demo/scenario.json`)
- The timeline is **scripted demo state**, not live AEP data.
- Its "confirmed" facts (outage, assessed damage, crews on site) stand in for what the outage-management system would supply, and they are labeled separately from model estimates in every answer.

## Alerts and guidance (`backend/guidance.py`)
- Rules are documented in code, and every response returns the full rules table.
- Special needs come only from what the customer declares.
- The internal life-support registry flag can raise backend escalation priority (`_internal`). It is always redacted before any output and recorded as redacted in the audit log; the assistant never confirms or denies registry membership.

---

# Live forecast → storm size, continuous scenarios, and answer confidence

## Forecast bridge (`backend/forecast.py`)
- **Source:** Open-Meteo daily forecast (free, no key), today plus 6 days, for the customer's home. Cached for 1 hour. If the API can't be reached, the app assumes calm weather and lowers confidence.
- **Why a bridge is needed:** the risk model was trained on *storm size* (damage reports across the area), not on weather. Each forecast day's peak wind gust is mapped to a storm size by log-interpolating these anchors:

| Peak gust | Expected damage reports | Anchor |
|---|---|---|
| ≤ 20 mph | 0.5 | calm |
| 30 mph | 2 | breezy |
| 40 mph | 12 | median minor event in our data |
| 50 mph | 60 | |
| 58 mph (NWS "severe") | 194 | the September 2024 storm |
| ≥ 70 mph | 1,254 | the February 2025 storm |

- **Multipliers (capped at 2,000):** freezing rain or ≥ 4 in of snow ×4; thunderstorms ×2; ≥ 2 in of rain ×1.5.
- **Limits:** this is a transparent, judgment-based bridge, not a fitted weather model. Real storms vary in track, duration and tree loading. The confidence meter reflects forecast lead time.
- **Replay:** a "storm like Feb 2025" uses the historical storm size (1,254 reports) and is always labeled hypothetical.

## Continuous storm size (`ml/score.py: p_at`)
- **Per-asset P(damage) at any storm size** is log–log interpolated between the stored anchors (12, 194 and 1,254 reports). It is linear below 12 and proportional above 1,254, capped at 0.95. The anchors are reproduced exactly, and the risk rises monotonically with storm size.

## Map estimates (`backend/mapdata.py`)
- **A neighbor home's color** is an *area* estimate: 1 − Π(1 − p) over equipment within 150 m. The customer's own home uses the full premise model (supply path + nearby + main line).
- **Trees** are placed only inside OpenStreetMap wooded and park polygons from the basemap and colored like the nearest home. The challenge data has no vegetation, so trees are illustrative of *where* vegetation is, not a model input.

## Answer confidence (`backend/confidence.py`)
- **A data-support score in 0–1, not a statistical probability.** It starts at 0.95 for outage-chance answers and 0.90 for repair-time answers; each gap subtracts a fixed amount:

| Gap | Penalty | Shown as missing |
|---|---|---|
| < 5 / < 20 past damage records on the customer's line | 0.30 / 0.15 | past storm records for your line |
| Home not in the grid map (topology) | 0.15 | your home's exact connection |
| Forecast 2–3 / 4–6 days out | 0.08 / 0.18 | a closer-range forecast |
| No live forecast | 0.25 | a live weather forecast |
| (repair time) no field damage assessment | 0.15 | a field assessment of actual damage |
| (repair time) crew assignment unknown | 0.10 | how many crews are assigned |
| Always: no vegetation data | 0.05 | tree and vegetation data |

- **Labels:** High ≥ 0.75, Medium ≥ 0.60, Low below that. Below 0.60, the app lists the top 3 missing items.
- **"What could change this":** deterministic and taken from tool results.
  - **Outage chance:** the forecast driver, the top model factor, and local damage history.
  - **Repair time:** the bottleneck device, crew count and arrival, and fallen trees or blocked roads.
