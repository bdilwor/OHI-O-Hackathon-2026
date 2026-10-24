"""Prediction + SHAP helpers for the saved risk-model bundle (model.joblib)."""
import joblib
import numpy as np
import pandas as pd

from .db import ARTIFACTS

BUNDLE_PATH = ARTIFACTS / "model.joblib"


def load_bundle() -> dict:
    return joblib.load(BUNDLE_PATH)


def predict_risk(bundle: dict, rows: pd.DataFrame) -> np.ndarray:
    """Calibrated P(asset damaged) for feature rows."""
    Xt = bundle["prep"].transform(rows[bundle["numeric"] + bundle["categorical"]])
    return bundle["model"].predict_proba(Xt)[:, 1]


def original_feature(name: str, features: list[str]) -> str:
    """Map a transformed column (e.g. one-hot `material_Pine`, `missingindicator_age_years`) to its source feature."""
    name = name.removeprefix("missingindicator_")
    matches = [f for f in features if name == f or name.startswith(f + "_")]
    return max(matches, key=len) if matches else name


def shap_by_feature(bundle: dict, rows: pd.DataFrame, background: pd.DataFrame | None = None) -> pd.DataFrame:
    """SHAP values (log-odds of the uncalibrated base model) summed back to original features."""
    import shap

    features = bundle["numeric"] + bundle["categorical"]
    Xt = bundle["prep"].transform(rows[features])
    base = bundle["model"].calibrated_classifiers_[0].estimator
    if bundle["model_name"] == "logistic_regression":
        bg = bundle["prep"].transform((background if background is not None else rows)[features])
        values = shap.LinearExplainer(base, bg).shap_values(Xt)
    else:
        values = shap.TreeExplainer(base).shap_values(Xt, check_additivity=False)
    if isinstance(values, list):
        values = values[1]
    values = np.asarray(values)
    if values.ndim == 3:
        values = values[:, :, 1]
    names = bundle["prep"].get_feature_names_out()
    df = pd.DataFrame(values, columns=names, index=rows.index)
    return df.T.groupby(lambda n: original_feature(n, features)).sum().T
