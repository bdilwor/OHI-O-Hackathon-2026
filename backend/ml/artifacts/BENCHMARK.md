# Model benchmark

Protocol: leave-one-major-storm-out (same folds, test sets and metrics as `METRICS.md`). Every
candidate is sigmoid-calibrated with storm-grouped inner CV. Adoption rule: mean PR-AUC lift
≥ 10% higher, Brier not worse, ROC-AUC at most 0.01 lower, and the Feb 2025 storm not worse.

## Outage-risk models (mean across folds)

| Model | PR-AUC lift | ROC-AUC | Brier | Recall top 1% | Recall top 5% | Adopt? |
|---|---|---|---|---|---|---|
| baseline_lr | 7.76x | 0.808 | 0.00105 | 0.10 | 0.34 | baseline |
| physics_lr | 7.42x | 0.804 | 0.00105 | 0.10 | 0.36 | no |
| lgbm_monotone | 7.93x | 0.780 | 0.00122 | 0.11 | 0.30 | no |
| ensemble | 7.47x | 0.794 | 0.00108 | 0.12 | 0.32 | no |

### Per held-out storm (PR-AUC lift)

| Model | minor_events | storm_2024-09 | storm_2025-01 | storm_2025-02 |
|---|---|---|---|---|
| baseline_lr | 6.19x | 4.05x | 14.55x | 6.26x |
| physics_lr | 6.71x | 4.21x | 12.29x | 6.46x |
| lgbm_monotone | 4.82x | 2.61x | 17.25x | 7.05x |
| ensemble | 5.02x | 3.87x | 14.57x | 6.41x |

### Adoption checks

- **physics_lr**: lift -4% vs baseline; fails lift_gain, brier_not_worse
- **lgbm_monotone**: lift +2% vs baseline; fails lift_gain, brier_not_worse, roc_auc_ok
- **ensemble**: lift -4% vs baseline; fails lift_gain, brier_not_worse, roc_auc_ok

The EBM (`interpret`) candidate was not run: `libebm` ships no linux-aarch64 build, so it cannot load in this Docker image on Apple Silicon. Run `python -m ml.benchmark ebm` on an x86-64 host.

## Repair-time models (mean across held-out storms)

76 unrepaired records are used as censored observations by the AFT model only.

| Model | p10–p90 coverage (target 0.80) | Pinball q10 | Pinball q50 | Pinball q90 |
|---|---|---|---|---|
| baseline_constant | 0.54 | 2.22 | 13.04 | 13.50 |
| current_lgbm | 0.50 | 2.32 | 13.06 | 13.97 |
| lgbm_storm_weather | 0.42 | 2.26 | 11.69 | 11.57 |
| weibull_aft_censored | 0.98 | 2.22 | 15.16 | 26.69 |

## Forecast bridge: peak gust → storm size

Fitted on ERA5 peak gusts for 35 storms and 1175 quiet days (Open-Meteo archive), cross-validated (each storm held out on its own).

| Curve | Poisson deviance | Storm-day log error | Storm-day log bias | Expected reports (actual 1659) |
|---|---|---|---|---|
| fitted (adopted) | 13526 | 1.19 | -0.30 | 2043 |
| previous judgment anchors | 25259 | 1.81 | +1.61 | 12554 |

## Neural operator (FNO) feasibility test

Grid 64x64 (~943 m cells), 35 storms, 400 epochs, Poisson loss with an asset-count offset, rotation/flip augmentation. Truth = damaged assets per cell (same as the baseline's test labels). Cells without assets are excluded.

| Model (mean over folds) | Cell PR-AUC | Cell ROC-AUC | Poisson deviance |
|---|---|---|---|
| fno | 0.093 | 0.727 | 12250 |
| production_lr | 0.174 | 0.841 | 3950 |
| uniform_by_storm_size | 0.166 | 0.840 | 1911 |

| Held-out storm | FNO PR-AUC | Production PR-AUC | Uniform PR-AUC |
|---|---|---|---|
| minor_events | 0.005 | 0.007 | 0.017 |
| storm_2024-09 | 0.101 | 0.173 | 0.171 |
| storm_2025-01 | 0.057 | 0.100 | 0.099 |
| storm_2025-02 | 0.211 | 0.417 | 0.376 |

**Verdict:** the FNO does not beat the production asset-level model, so it is not adopted. With one sample per storm (35, three of them major), a field-to-field operator has far too few training examples, and the asset attributes that drive damage (age, material, height, history) are blurred away when rasterized.
