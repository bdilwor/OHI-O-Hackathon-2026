# Outage-damage model: metrics

Validation: **leave-one-major-storm-out**. Each fold holds out one major storm (or all minor
events pooled), trains on the rest, and scores *every* asset in the held-out storm(s).
Damage history features only use storms before the scored storm. Accuracy is not reported:
with a population positive rate of 0.0134%, predicting
"no damage" everywhere would be >99.9% accurate and useless.

Selected model: **logistic_regression** (highest mean PR-AUC lift), then sigmoid-calibrated.

## Mean across folds

| Model | PR-AUC | Lift vs base rate | ROC-AUC | Brier | Recall top 1% | Recall top 5% |
|---|---|---|---|---|---|---|
| logistic_regression | 0.0066 | 7.8x | 0.808 | 0.07489 | 0.10 | 0.34 |
| lightgbm | 0.0059 | 5.1x | 0.792 | 0.00106 | 0.08 | 0.32 |
| random_forest | 0.0064 | 6.0x | 0.771 | 0.00528 | 0.10 | 0.34 |
| lightgbm_no_history | 0.0063 | 6.0x | 0.793 | 0.00106 | 0.10 | 0.34 |
| calibrated_logistic_regression | 0.0066 | 7.8x | 0.808 | 0.00105 | 0.10 | 0.34 |

`lightgbm_no_history` is an ablation without damage-history features, to show their contribution.

## Per fold

### minor_events (158 damaged assets, base rate 0.0015%)

| Model | PR-AUC | Lift vs base rate | ROC-AUC | Brier | Recall top 1% | Recall top 5% |
|---|---|---|---|---|---|---|
| logistic_regression | 0.0001 | 6.2x | 0.833 | 0.00059 | 0.06 | 0.37 |
| lightgbm | 0.0001 | 4.4x | 0.755 | 0.00001 | 0.08 | 0.27 |
| random_forest | 0.0000 | 3.3x | 0.714 | 0.00141 | 0.05 | 0.23 |
| lightgbm_no_history | 0.0001 | 4.4x | 0.740 | 0.00001 | 0.06 | 0.25 |
| calibrated_logistic_regression | 0.0001 | 6.2x | 0.833 | 0.00002 | 0.06 | 0.37 |

### storm_2024-09 (190 damaged assets, base rate 0.0571%)

| Model | PR-AUC | Lift vs base rate | ROC-AUC | Brier | Recall top 1% | Recall top 5% |
|---|---|---|---|---|---|---|
| logistic_regression | 0.0023 | 4.0x | 0.765 | 0.00585 | 0.04 | 0.30 |
| lightgbm | 0.0023 | 4.0x | 0.778 | 0.00057 | 0.06 | 0.27 |
| random_forest | 0.0024 | 4.3x | 0.749 | 0.00086 | 0.08 | 0.35 |
| lightgbm_no_history | 0.0024 | 4.3x | 0.778 | 0.00057 | 0.07 | 0.31 |
| calibrated_logistic_regression | 0.0023 | 4.0x | 0.765 | 0.00057 | 0.04 | 0.30 |

### storm_2025-01 (46 damaged assets, base rate 0.0138%)

| Model | PR-AUC | Lift vs base rate | ROC-AUC | Brier | Recall top 1% | Recall top 5% |
|---|---|---|---|---|---|---|
| logistic_regression | 0.0020 | 14.6x | 0.841 | 0.00050 | 0.22 | 0.37 |
| lightgbm | 0.0009 | 6.2x | 0.824 | 0.00014 | 0.09 | 0.39 |
| random_forest | 0.0014 | 10.2x | 0.828 | 0.00029 | 0.15 | 0.39 |
| lightgbm_no_history | 0.0013 | 9.4x | 0.846 | 0.00014 | 0.15 | 0.43 |
| calibrated_logistic_regression | 0.0020 | 14.6x | 0.841 | 0.00014 | 0.22 | 0.37 |

### storm_2025-02 (1169 damaged assets, base rate 0.3510%)

| Model | PR-AUC | Lift vs base rate | ROC-AUC | Brier | Recall top 1% | Recall top 5% |
|---|---|---|---|---|---|---|
| logistic_regression | 0.0220 | 6.3x | 0.795 | 0.29264 | 0.10 | 0.32 |
| lightgbm | 0.0203 | 5.8x | 0.811 | 0.00350 | 0.11 | 0.35 |
| random_forest | 0.0218 | 6.2x | 0.792 | 0.01856 | 0.11 | 0.38 |
| lightgbm_no_history | 0.0212 | 6.0x | 0.807 | 0.00350 | 0.12 | 0.37 |
| calibrated_logistic_regression | 0.0220 | 6.3x | 0.795 | 0.00349 | 0.10 | 0.32 |

## Top features (mean |SHAP|)

| Feature | mean abs SHAP |
|---|---|
| storm_log_damage | 2.351 |
| voltage_kv | 1.271 |
| device_subtype | 0.795 |
| height_ft | 0.763 |
| overhead | 0.759 |
| pole_class_num | 0.748 |
| material | 0.619 |
| line_usage | 0.615 |
| bare_conductor | 0.611 |
| asset_type | 0.567 |
| wire_spec_top | 0.545 |
| segment_len_m | 0.356 |

See `calibration.png` and `shap_importance.png`.
