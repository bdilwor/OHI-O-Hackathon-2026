# Does the pitch quote hold up?

> "The obvious approach, assuming last storm's outages repeat, warned just 3.6% of homes that lost power.
> For almost everyone else its answer was simply 'no'. Just using the grid's structure beats that by half
> with the same number of warnings. Our model goes further: on storms it never saw, the riskiest 5% of
> equipment held a third of the damage. And it doesn't stop at 'will I lose power'. It tells you when it's
> coming back and how sure we are."

Code: [quote_check.py](quote_check.py). Full output: [results/quote_check_output.txt](results/quote_check_output.txt),
[results/quote_check.json](results/quote_check.json).

## Verdict

| Claim | Holds? | What the data says |
|---|---|---|
| Lookup warned 3.6% of homes that lost power | **Yes, exactly** | 395 of 11,057 homes out in Feb 2025 had been out in Helene: 3.6% (95% CI 1.4–6.8%). |
| For almost everyone else it said "no" | **Yes** | It warned 1,489 of 117,599 homes; 98.7% were told "no". |
| Grid structure beats that by half, same number of warnings | **Point estimate yes, but not proven** | Ranking homes by supply-path length catches 5.1% vs 3.6% (×1.42). The CI is ×0.13 to ×5.0, so the gain could be noise. The trained models do *not* beat the lookup on this test (×0.86–0.96). |
| Riskiest 5% of equipment held a third of the damage, on unseen storms | **Yes** | 34.1% averaged over the 4 held-out folds (30–37% each). Also holds when trained only on earlier storms (32% Feb 2025, 44% Jan 2025). Rankings built on past damage get about 5%. |
| Tells you when power is coming back | **Weakly** | The P10–P90 range contained 96.5% of real Feb 2025 restorations. But giving every home the *same* range does just as well (97.8%), and the P50 has no correlation with actual repair time (Spearman −0.01). |
| …and how sure we are | **No** | The confidence label doesn't track accuracy. "High confidence" homes were the worst-calibrated group (predicted 8%, observed 28.5%). |

**Bottom line:** at the equipment level, the "structure beats history" story is strongly supported and
robust. The model is several times better than any history- or lookup-based ranking on every held-out storm.
At the home level ("will *I* lose power?") the model ties the lookup when both make the same number of
warnings. The model's advantage there is reach: it scores every home, while the lookup can only warn the
1.3% of homes that were hit last time. The restoration-time and confidence parts of the quote are not
backed by the data.

## 1. "Last storm repeats" → 3.6%

Reproduced exactly with the method in [lookup_vs_no.py](lookup_vs_no.py): damage reports are snapped to
the nearest fuse/recloser/transformer, and a home is "out" if a device on its supply path was hit.

| Version of "last storm" (target: Feb 2025, 11,057 homes out = 9.4%) | Warnings | Recall | 95% CI |
|---|---|---|---|
| Helene, Sept 2024 (**the quote's choice**) | 1,489 | **3.6%** | 1.4–6.8% |
| The storm right before (Jan 2025, 46 reports; the project counts it as a major storm) | 1,067 | 2.1% | 0.1–4.8% |
| Out in *any* earlier storm | 3,426 | 7.4% | 4.0–11.4% |
| Labels with a 60 m snap cap (records far from any device dropped) | — | 3.3% | — |

- "Last storm" literally means January 2025, which gives an even lower 2.1%. The quote's 3.6% skips
  that storm. This is fair as "last big storm", but should be stated.
- The lookup is not useless at what it does. Its precision is 26.5%, or 2.8× the 9.4% base rate. It is
  just very narrow.
- On other storm pairs it is erratic: 0.0–0.3% for Helene, 12.3% for Jan 2025 (small storm, CI 0.2–31%).

## 2. "Grid structure beats that by half with the same number of warnings"

No code in the repo produced this number, so I set the structure-only baselines in advance and report
all of them. Every method gets exactly the lookup's number of warnings (ties at the cut-off are split at
random). CIs come from resampling whole circuits (111 circuits; outages cluster heavily because one
recloser can take out thousands of homes).

Feb 2025, budget = Helene lookup (1,489 warnings, 3.6% recall):

| Method (no Feb 2025 data used) | Recall | × lookup (95% CI) | Difference, pts (95% CI) |
|---|---|---|---|
| Path length (# devices on the supply path, no training) | 5.1% | **×1.42** (0.13–5.0) | +1.5 (−5.2 to +8.8) |
| Structure model (project's model, no history features) | 3.4% | ×0.96 (0.34–2.6) | −0.1 (−3.9 to +2.9) |
| Full model (as deployed), leave-one-storm-out | 3.1% | ×0.86 (0.34–2.2) | −0.5 (−4.1 to +2.1) |
| Full model, trained on earlier storms only | 3.2% | ×0.88 (0.34–2.4) | −0.4 (−4.1 to +2.4) |
| History-only model | 0.9% | ×0.25 (0.08–0.71) | −2.7 (−6.0 to −0.5) |

At the Jan-2025-lookup budget (1,067 warnings) path length is ×1.71 (CI 0.31–7.5) and the full model ×1.22
(0.69–13.6).

- "Beats it by half" matches the *point estimate* for path length (×1.4–1.7), but none of the differences
  are statistically clear. Outages come in a few large clusters, so 111 circuits give little power.
- Path length partly wins *because of how the labels are built*: more devices on your path means more
  chances that a damage report snaps onto one of them. It's a real structural signal, but partly circular.
- Over the whole ranking (Feb 2025), path length and the models are similar: ROC-AUC 0.69–0.70 against
  0.58 for history only. The top 5% of homes by model risk hold 12.0% of homes that lost power
  (7.0–17.7%), about 2.4× random.

## 3. "Riskiest 5% of equipment held a third of the damage"

Top-5% recall over all 333,010 assets; the target storm is never used in training. CI = circuit bootstrap.

| Held-out storm | Full model | Structure-only model | Asset-type rule (no ML) | Nearby past damage | History-only model |
|---|---|---|---|---|---|
| Sept 2024 (Helene) | 30.0% (17–42) | 31.6% | 20.4% | 4.3% | 2.9% |
| Jan 2025 | 37.0% (14–57) | 39.1% | 33.1% | 4.5% | 6.4% |
| Feb 2025 | 32.2% (28–36) | 38.2% (34–41) | 26.8% | 6.7% | 4.7% |
| 32 minor events pooled | 37.3% | 39.9% | 16.5% | 6.3% | 24.1% |
| **Mean (as in METRICS.md)** | **34.1%** | **37.2%** | 24.2% | 5.5% | 9.5% |

Trained on earlier storms only, the full model gets 32.2% (Feb 2025) and 43.5% (Jan 2025), so the result
doesn't depend on training with future storms. Random selection gets 5%.

- The claim holds as stated, and the model beats a simple "damage rate by equipment type" rule by
  about 10 points on average (4 to 21 points depending on the held-out storm).
- **The damage-history features add nothing and slightly hurt.** Rankings built only on past damage are
  near random (AUC 0.50–0.63 on major storms). Dropping them improves the model on every fold, including
  Feb 2025 (38.2% vs 32.2%; the CIs overlap slightly, and this is not a paired test) and on average (37.2% vs 34.1%). The model's value comes from the grid's
  structure, which directly supports "structure, not lookup".
- Caveat: the quote sets this equipment-level number next to the home-level 3.6%. They are different units.
  At the home level the model ties the lookup (section 2).

## 4. "When it's coming back and how sure we are"

**Restoration estimate** (app's `RestorationEstimator`, "high" storm, 400 random homes that lost power in Feb
2025). Actual time = the slowest ARCOS repair on the home's path. Dates are whole days, so a match within
±1 day counts as correct.

| | Result |
|---|---|
| Actual repair days | 92% took 0–2 days; 3 homes took 80–142 days |
| Model's typical P10 / P50 / P90 | 1.5 / 16.5 / 53.5 hours |
| P10–P90 contains the actual | 96.5% (93.4–99.0%) |
| Same 1.5–53.5 h range for every home | 97.8% |
| P50 within a day of actual | 72% |
| Does P50 rank who waits longer? | No: Spearman −0.01 (p = 0.80) |

The range is right about as often as a fixed answer would be. Nothing shows the per-home detail
(crews, bottleneck device, repair bundles) helps predict timing. Caveats: `asset_risk` uses history that
includes Feb 2025, which favours the model; ARCOS "repaired" dates are not power-restoration times; the
default of 2 crews is an assumption.

The repair-duration helper (`ml/duration.py`, which sets the access slowdown) is overconfident. Its
"80%" intervals contained 34–74% of held-out repairs, and its median is no better than a constant
(pinball loss 11.15 vs 11.07 on Feb 2025).

**Confidence label** (High / Medium / Low, from damage records on the home's circuit before Feb 2025):

| Band | Homes (circuits) | Lost power | Model predicted | ROC-AUC | Brier vs. constant |
|---|---|---|---|---|---|
| High | 6,261 (4) | 28.5% | 8.1% | 0.62 | 0.241 vs 0.204 (worse) |
| Medium | 28,406 (21) | 12.7% | 5.9% | 0.73 | 0.111 vs 0.111 |
| Low | 82,932 (85) | 6.8% | 5.3% | 0.64 | 0.063 vs 0.064 |

"High confidence" marks the circuits that get hit most often, not the places where the model is most
accurate. It was the least accurate band. (Only 4 circuits are in that band, so treat this as a warning
sign, not a precise estimate.)

## Suggested wording

> "Assuming last big storm's outages repeat warned just 3.6% of the homes that lost power in February 2025.
> Everyone else heard 'no'. Our model ranks all 333,000 poles, wires and devices instead. On storms it never
> saw, the riskiest 5% held about a third of the damage (30–37% across four held-out storms), versus about 5%
> for rankings built on past damage. And it gives every home a restoration window."

Drop "beats that by half" unless you add "in our best estimate". Drop "how sure we are", or recalibrate the
confidence label first.

## Method notes

- **Labels:** ARCOS lists equipment damage, not outages. Home outages are derived (snap to the nearest
  device, then follow the supply path), exactly as in `lookup_vs_no.py`. Results barely change with a 60 m
  snap cap (lookup 3.3%; path length ×1.62, full model ×1.12).
- **Models:** same spec and training rows as `ml/train.py` (calibrated logistic regression,
  `dataset.parquet`). Variants: all features / no damage history / damage history only. Each is trained
  leave-one-storm-out ("never saw") and on earlier storms only.
- **From equipment to homes:** P(device hit) = 1 − ∏(1 − p) over assets nearest that device;
  P(home out) = 1 − ∏ over devices on its path. This matches how the labels are built. It is not the app's
  `PremiseScorer` roll-up, which also counts lines within 250 m and the circuit backbone.
- **Storm size** in the features is the real report count of the scored storm. This is constant within a
  storm, so it doesn't affect rankings, but it does affect the calibration numbers in section 4.
- **Uncertainty:** circuit-level cluster bootstrap, 2,000 replicates. Ratio CIs are wide when the lookup
  catches very few homes; the difference CIs are the more stable read.

## Deck check (video deck, Oct 25)

[deck_numbers.py](deck_numbers.py) reproduces every number on the "Ahead of the Storm" slides
([results/deck_numbers_output.txt](results/deck_numbers_output.txt)). Changes made to the deck:

| Slide | Was | Now | Why |
|---|---|---|---|
| 3 | Device count "catches 47% more outages with the same number of warnings" | "Homes behind 6 or more devices lost power 4× as often as homes behind 4 or fewer" | 47% doesn't reproduce: every tie-break gives +40–45%, and that comparison is one storm pair with a CI from ×0.13 to ×5. The outage-rate gap is 24% vs 6% in Feb 2025 (4.1×, CI 2.1–7.9×), in the same direction in all three major storms. |
| 6 | ROC-AUC 0.77–0.84 | 0.76–0.84 | The Helene fold is 0.7648 (0.765 rounded twice became 0.77). |
| 8 | Card 4 repeated "Ice as its own path to storm size" twice | "Ice, but our forecast only read wind" → "Ice as its own path to storm size" | Copy error. |
| 9 | "For AEP storm operations: rank circuits by crew-hours…" + operations-view screenshot | Storm test and Outage test, with circuit ranking as "Next" | The app has no operations view or circuit ranking yet. |
| 9 | 116 automated tests | 134 | `pytest --collect-only`: 134 (133 pass, 1 live-LLM test skips without a key). |
| 1 | "February 2025 storm replay" screenshot | Storm test at intensity 5 | The app simulates a storm of February 2025's size; it doesn't replay where the real damage was. |

Code change: `ml/score_assets.py` now forces each asset's risk to rise with storm size, so slide 6's
"a worse forecast never lowers anyone's outage chance" holds by construction (it already held for all
333,010 assets; `test_probabilities_valid_and_ordered` checks it).

## Reproduce

```bash
docker compose up -d db loader                           # needs data/ (Data1/, Data2/, matched_upstream.txt)
docker compose run --rm ml python -m ml.build_dataset     # parquet inputs (~2 min)
docker compose run --rm ml python -m analysis.quote_check # ~4 min first run (model fits cached in .cache/)
```
