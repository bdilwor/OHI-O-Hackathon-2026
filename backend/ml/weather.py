"""Historical weather for every storm (Open-Meteo archive / ERA5), cached to artifacts/weather.parquet.

    python -m ml.weather          # fetch (once) and fit the gust -> storm-size hazard curve

Daily max gust, precipitation, snowfall and weather code on a 3x3 grid over the service area, for the
whole ARCOS period. The cache is committed so rebuilds work offline. Units match the live forecast
(mph, inches) so a curve fitted here applies directly to forecast days.
"""
import json

import httpx
import numpy as np
import pandas as pd
from scipy.optimize import minimize

from .db import ARTIFACTS

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
WEATHER_PATH = ARTIFACTS / "weather.parquet"
HAZARD_PATH = ARTIFACTS / "hazard_curve.json"
BBOX = (-80.25, 36.99, -79.65, 37.48)  # service-area extent (customers), lon/lat
GRID = 3
DAILY = "wind_gusts_10m_max,precipitation_sum,snowfall_sum,weather_code"
ICE_CODES = {66, 67}
THUNDER_CODES = {95, 96, 99}


def grid_points() -> list[tuple[float, float]]:
    lons = np.linspace(BBOX[0], BBOX[2], GRID)
    lats = np.linspace(BBOX[1], BBOX[3], GRID)
    return [(round(float(la), 3), round(float(lo), 3)) for la in lats for lo in lons]


def fetch(start: str, end: str) -> pd.DataFrame:
    pts = grid_points()
    params = {
        "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
        "start_date": start, "end_date": end, "daily": DAILY, "timezone": "America/New_York",
        "wind_speed_unit": "mph", "precipitation_unit": "inch",
    }
    r = httpx.get(ARCHIVE, params=params, timeout=120)
    r.raise_for_status()
    body = r.json()
    body = body if isinstance(body, list) else [body]
    frames = []
    for i, (pt, loc) in enumerate(zip(pts, body)):
        d = pd.DataFrame(loc["daily"])
        d["cell"], d["lat"], d["lon"] = i, pt[0], pt[1]
        frames.append(d)
    out = pd.concat(frames, ignore_index=True).rename(columns={
        "time": "date", "wind_gusts_10m_max": "gust_mph", "precipitation_sum": "precip_in",
        "snowfall_sum": "snow_in", "weather_code": "code"})
    out["date"] = pd.to_datetime(out["date"])
    return out


def load_weather(arcos: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cached daily weather; fetched once over the ARCOS period (capped at the archive's ~5-day lag)."""
    if WEATHER_PATH.exists():
        return pd.read_parquet(WEATHER_PATH)
    if arcos is None:
        arcos = pd.read_parquet(ARTIFACTS / "arcos.parquet")
    start = (arcos["report_tim"].min() - pd.Timedelta(days=7)).date()
    end = min(arcos["report_tim"].max() + pd.Timedelta(days=7), pd.Timestamp.today() - pd.Timedelta(days=6)).date()
    w = fetch(str(start), str(end))
    w.to_parquet(WEATHER_PATH, index=False)
    return w


def cell_of(x: np.ndarray, y: np.ndarray, to_lonlat) -> np.ndarray:
    """Nearest weather-grid cell for metric x/y coordinates."""
    lon, lat = to_lonlat.transform(x, y)
    pts = np.array([(p[1], p[0]) for p in grid_points()])
    d = (lon[:, None] - pts[None, :, 0]) ** 2 + (lat[:, None] - pts[None, :, 1]) ** 2
    return d.argmin(axis=1)


def storm_weather(storms: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """Per (storm, cell): max gust, total rain, max snow, ice / thunder flags over [start - 1 day, end]."""
    rows = []
    for s in storms.itertuples():
        w = weather[(weather["date"] >= s.storm_start - pd.Timedelta(days=1)) & (weather["date"] <= s.storm_end)]
        for cell, g in w.groupby("cell"):
            codes = set(g["code"].dropna().astype(int))
            rows.append({"storm_id": s.storm_id, "cell": cell, "gust_mph": g["gust_mph"].max(),
                         "precip_in": g["precip_in"].sum(), "snow_in": g["snow_in"].max(),
                         "ice": float(bool(codes & ICE_CODES)), "thunder": float(bool(codes & THUNDER_CODES))})
    return pd.DataFrame(rows)


# ---- gust -> storm-size hazard curve ------------------------------------------------------------------

def daily_area(weather: pd.DataFrame) -> pd.DataFrame:
    """Area-wide daily weather: worst cell per day (what the forecast bridge sees for one day)."""
    g = weather.groupby("date")
    codes = g["code"].apply(lambda c: set(c.dropna().astype(int)))
    return pd.DataFrame({
        "gust_mph": g["gust_mph"].max(), "precip_in": g["precip_in"].max(), "snow_in": g["snow_in"].max(),
        "ice": codes.map(lambda c: float(bool(c & ICE_CODES))),
        "thunder": codes.map(lambda c: float(bool(c & THUNDER_CODES))),
    }).reset_index()


def event_table(storms: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """One row per storm (its worst day) plus quiet days (no report within 3 days) with storm size 0."""
    area = daily_area(weather)
    rows = []
    busy = set()
    for s in storms.itertuples():
        win = area[(area["date"] >= s.storm_start - pd.Timedelta(days=1)) & (area["date"] <= s.storm_end)]
        if win.empty:
            continue
        worst = win.loc[win["gust_mph"].idxmax()]
        rows.append({**worst.to_dict(), "n": s.n_records, "storm_id": s.storm_id})
        busy.update(pd.date_range(s.storm_start - pd.Timedelta(days=3), s.storm_end + pd.Timedelta(days=3)))
    quiet = area[~area["date"].isin(busy)].copy()
    quiet["n"], quiet["storm_id"] = 0, -1
    return pd.concat([pd.DataFrame(rows), quiet], ignore_index=True)


def fit_hazard(ev: pd.DataFrame) -> dict:
    """Poisson fit of expected storm size: E[n] = exp(a + b * relu(gust - g0)), g0 by grid search.

    Expected storm size (damage reports) is what the forecast bridge needs. The hinge is the simplest
    monotone fragility form: little breaks below g0, and above it damage grows exponentially with gust
    (wind pressure ~ v^2). Rain, snow, ice and thunder are not fitted: ERA5 daily codes never report ice or
    thunder here, and rain alone came out with a physically implausible negative sign, so the bridge keeps
    its documented judgment multipliers for those.
    """
    y = ev["n"].to_numpy(float)
    best = None
    for g0 in np.arange(15, 56, 1.0):
        X = np.column_stack([np.ones(len(ev)), np.maximum(ev["gust_mph"].to_numpy(float) - g0, 0)])
        res = minimize(lambda b: np.sum(np.exp(X @ b) - y * (X @ b)), np.zeros(2), method="BFGS")
        if best is None or res.fun < best[0]:
            best = (res.fun, g0, res.x)
    _, g0, (a, b) = best
    return {"g0": float(g0), "intercept": float(a), "slope": float(b)}


def predict_hazard(curve: dict, gust) -> np.ndarray:
    return np.exp(curve["intercept"] + curve["slope"] * np.maximum(np.asarray(gust, float) - curve["g0"], 0))


def current_bridge(gust) -> np.ndarray:
    """The app's previous judgment-based gust anchors (before this fit), for comparison."""
    old = [(20, 0.5), (30, 2.0), (40, 12.0), (50, 60.0), (58, 194.0), (70, 1254.0)]
    g = np.asarray(gust, float)
    xs, ys = np.array([a for a, _ in old]), np.log([b for _, b in old])
    out = np.exp(np.interp(g, xs, ys))
    return np.where(g < xs[0], old[0][1] * np.maximum(g, 1) / xs[0], out)


def poisson_deviance(y, mu) -> float:
    mu = np.maximum(mu, 1e-9)
    return float(2 * np.sum(np.where(y > 0, y * np.log(np.maximum(y, 1e-9) / mu), 0) - (y - mu)))


def compare_hazard(storms: pd.DataFrame, weather: pd.DataFrame) -> dict:
    """Cross-validated: each storm held out on its own, quiet days in 5 random folds."""
    ev = event_table(storms, weather)
    rng = np.random.default_rng(0)
    folds = np.where(ev["storm_id"] >= 0, ev["storm_id"], 1000 + rng.integers(0, 5, len(ev)))
    pred = np.zeros(len(ev))
    for f in np.unique(folds):
        pred[folds == f] = predict_hazard(fit_hazard(ev[folds != f]), ev.loc[folds == f, "gust_mph"])
    cur = current_bridge(ev["gust_mph"])
    y = ev["n"].to_numpy(float)
    s = (ev["storm_id"] >= 0).to_numpy()
    log_err = lambda p: np.log1p(p[s]) - np.log1p(y[s])
    full = fit_hazard(ev)
    return {
        "curve": full, "n_storms": int(s.sum()), "n_quiet_days": int((~s).sum()),
        "deviance_fitted": poisson_deviance(y, pred), "deviance_previous": poisson_deviance(y, cur),
        "storm_mae_log_fitted": float(np.abs(log_err(pred)).mean()),
        "storm_mae_log_previous": float(np.abs(log_err(cur)).mean()),
        "storm_bias_log_fitted": float(log_err(pred).mean()), "storm_bias_log_previous": float(log_err(cur).mean()),
        "total_expected_fitted": float(pred.sum()), "total_expected_previous": float(cur.sum()),
        "total_actual": float(y.sum()),
        "anchors": [[g, round(float(predict_hazard(full, g)), 2)] for g in (20, 30, 40, 50, 58, 70)],
        "storms": [{"storm_id": int(r.storm_id), "n": int(r.n), "gust_mph": round(float(r.gust_mph), 1)}
                   for r in ev[s].itertuples()],
    }


def main() -> None:
    storms = pd.read_parquet(ARTIFACTS / "storms.parquet")
    weather = load_weather()
    print(f"weather: {len(weather):,} cell-days, {weather['date'].min().date()} .. {weather['date'].max().date()}")
    res = compare_hazard(storms, weather)
    HAZARD_PATH.write_text(json.dumps(res, indent=2))
    print(json.dumps({k: v for k, v in res.items() if k != "storms"}, indent=2))


if __name__ == "__main__":
    main()
