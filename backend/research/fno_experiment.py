"""Neural-operator (FNO) feasibility test for storm-damage prediction.

    docker compose --profile research run --rm research

Question: can a Fourier Neural Operator, which learns maps between spatial fields, predict where a storm
damages the grid better than the production asset-level model? Each storm becomes one 64x64 sample:
  inputs  per-cell asset counts by type, overhead span length, prior-storm damage density (storms strictly
          before this one), storm size, and the storm's ERA5 peak-gust field (3x3 grid, nearest cell)
  target  damaged assets per cell
The FNO predicts a per-asset damage rate per cell (Poisson loss, rate x asset count, so empty cells stay 0).
Same leave-one-major-storm-out folds as the production model. The production baseline's held-out asset
probabilities (ml.benchmark --dump-baseline) are summed to the same cells for a like-for-like comparison.
Writes ml/artifacts/fno_results.json (read by ml.benchmark's report).
"""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from neuralop.models import FNO
from pyproj import Transformer
from sklearn.metrics import average_precision_score, roc_auc_score

ART = Path(__file__).resolve().parents[1] / "ml" / "artifacts"
N = 64
EPOCHS = 400
SEED = 0
TYPES = ["pole", "primary", "secondary", "transformer", "fuse", "recloser", "switch"]
DAMAGED_PER_REPORT = 0.94  # damaged assets per ARCOS report (ASSUMPTIONS.md, scenario anchoring)


def cell_index(x, y, box):
    ix = np.clip(((x - box[0]) / (box[2] - box[0]) * N).astype(int), 0, N - 1)
    iy = np.clip(((y - box[1]) / (box[3] - box[1]) * N).astype(int), 0, N - 1)
    return iy * N + ix


def grid_count(idx, weights=None):
    return np.bincount(idx, weights=weights, minlength=N * N).reshape(N, N).astype(np.float32)


def build():
    assets = pd.read_parquet(ART / "assets.parquet", columns=["asset_uid", "asset_type", "x", "y", "overhead",
                                                             "segment_len_m"])
    arcos = pd.read_parquet(ART / "arcos.parquet", columns=["storm_id", "matched_uid"])
    storms = pd.read_parquet(ART / "storms.parquet").sort_values("storm_start").reset_index(drop=True)
    weather = pd.read_parquet(ART / "weather.parquet")
    box = (assets["x"].min(), assets["y"].min(), assets["x"].max() + 1, assets["y"].max() + 1)
    assets["cell"] = cell_index(assets["x"].to_numpy(), assets["y"].to_numpy(), box)

    static = [np.log1p(grid_count(assets.loc[assets["asset_type"] == t, "cell"].to_numpy())) for t in TYPES]
    span = (assets["segment_len_m"].fillna(0) * assets["overhead"].fillna(0)).to_numpy()
    static.append(np.log1p(grid_count(assets["cell"].to_numpy(), span)))
    exposure = grid_count(assets["cell"].to_numpy())  # assets per cell

    # Each grid cell -> nearest ERA5 cell (weather grid is 3x3 lon/lat points).
    to_ll = Transformer.from_crs(32617, 4326, always_xy=True)
    cx = box[0] + (np.arange(N) + 0.5) / N * (box[2] - box[0])
    cy = box[1] + (np.arange(N) + 0.5) / N * (box[3] - box[1])
    gx, gy = np.meshgrid(cx, cy)
    lon, lat = to_ll.transform(gx.ravel(), gy.ravel())
    wcells = weather.groupby("cell")[["lat", "lon"]].first()
    near = np.argmin((lon[:, None] - wcells["lon"].to_numpy()) ** 2 + (lat[:, None] - wcells["lat"].to_numpy()) ** 2,
                     axis=1)
    near = wcells.index.to_numpy()[near]

    pos = arcos.dropna(subset=["matched_uid"]).drop_duplicates(["storm_id", "matched_uid"])
    pos = pos.merge(assets[["asset_uid", "cell"]], left_on="matched_uid", right_on="asset_uid")
    X, Y, meta = [], [], []
    prior = np.zeros((N, N), np.float32)
    for s in storms.itertuples():
        w = weather[(weather["date"] >= s.storm_start - pd.Timedelta(days=1)) & (weather["date"] <= s.storm_end)]
        gust_by_cell = w.groupby("cell")["gust_mph"].max()
        gust = gust_by_cell.reindex(near).fillna(gust_by_cell.mean()).to_numpy().reshape(N, N) / 70.0
        y = grid_count(pos.loc[pos["storm_id"] == s.storm_id, "cell"].to_numpy())
        size = np.full((N, N), np.log1p(s.n_records) / 7.0, np.float32)
        X.append(np.stack(static + [np.log1p(prior), size, gust.astype(np.float32)]))
        Y.append(y)
        meta.append((s.storm_id, s.cv_group, s.n_records))
        prior = prior + y  # history only from storms strictly before the next one
    return np.stack(X).astype(np.float32), np.stack(Y).astype(np.float32), exposure, meta, box


def baseline_cells(box):
    """Production model's held-out expected damaged assets per (storm, cell), and the matching truth."""
    p = pd.read_parquet(ART / "bench_baseline_preds.parquet")
    p["cell"] = cell_index(p["x"].to_numpy(), p["y"].to_numpy(), box)
    out = {}
    for sid, g in p.groupby("storm_id"):
        mu = grid_count(g["cell"].to_numpy(), (g["p"] * g["w"]).to_numpy())
        y = grid_count(g["cell"].to_numpy(), g["label"].to_numpy().astype(float))
        out[int(sid)] = (mu, y)
    return out


def augment(x, y):
    k = np.random.randint(4)
    x, y = torch.rot90(x, k, (-2, -1)), torch.rot90(y, k, (-2, -1))
    if np.random.rand() < 0.5:
        x, y = torch.flip(x, (-1,)), torch.flip(y, (-1,))
    return x, y


def train_fno(X, Y, expo, idx):
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    model = FNO(n_modes=(12, 12), hidden_channels=24, in_channels=X.shape[1], out_channels=1, n_layers=4)
    opt = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-3)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, EPOCHS)
    xt, yt = torch.from_numpy(X[idx]), torch.from_numpy(Y[idx])[:, None]
    e = torch.from_numpy(expo)[None, None].expand(len(idx), 1, N, N)
    log_e = torch.log(e.clamp(min=1e-6))
    stack = torch.cat([xt, log_e], 1)  # carry exposure through the same augmentation
    for _ in range(EPOCHS):
        model.train()
        xs, ys = augment(stack, yt)
        xin, le = xs[:, :-1], xs[:, -1:]
        log_rate = model(xin)
        mask = (le > -10).float()  # cells with assets
        log_mu = (log_rate + le) * mask
        loss = ((torch.exp(log_mu) - ys * log_mu) * mask).sum() / len(idx)
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
    return model


def predict(model, X, expo):
    model.eval()
    with torch.no_grad():
        rate = torch.exp(model(torch.from_numpy(X))).numpy()[:, 0]
    return rate * expo[None]


def deviance(y, mu):
    mu = np.maximum(mu, 1e-9)
    return float(2 * np.sum(np.where(y > 0, y * np.log(np.maximum(y, 1e-9) / mu), 0) - (y - mu)))


def scores(y, mu, has_assets):
    yb, m, yy = (y > 0)[has_assets], mu[has_assets], y[has_assets]
    return {"poisson_deviance": deviance(yy, m), "cell_pr_auc": float(average_precision_score(yb, m)),
            "cell_roc_auc": float(roc_auc_score(yb, m)), "predicted_total": float(m.sum()),
            "actual_total": float(yy.sum())}


def main():
    t0 = time.time()
    X, Y, expo, meta, box = build()
    base = baseline_cells(box)
    has = np.broadcast_to(expo > 0, Y.shape)
    groups = np.array([m[1] for m in meta])
    folds, rows = sorted(set(groups)), {}
    for g in folds:
        te = np.where(groups == g)[0]
        tr = np.where(groups != g)[0]
        mu_fno = predict(train_fno(X, Y, expo, tr), X[te], expo)
        sids = [meta[i][0] for i in te]
        mu_base = np.stack([base[s][0] for s in sids])
        y_true = np.stack([base[s][1] for s in sids])  # same truth as the baseline's test set
        # Naive reference: the storm's damage count (known size) spread evenly over all assets.
        n_rep = np.array([meta[i][2] for i in te], float)[:, None, None]
        mu_uniform = DAMAGED_PER_REPORT * n_rep * expo[None] / expo.sum()
        rows[g] = {name: scores(y_true, mu, has[te]) for name, mu in
                   (("fno", mu_fno), ("production_lr", mu_base), ("uniform_by_storm_size", mu_uniform))}
        print(g, {k: round(v["cell_pr_auc"], 3) for k, v in rows[g].items()}, f"({time.time() - t0:.0f}s)", flush=True)

    names = ["fno", "production_lr", "uniform_by_storm_size"]
    mean = {n: {k: float(np.mean([rows[g][n][k] for g in folds])) for k in ("poisson_deviance", "cell_pr_auc",
                                                                          "cell_roc_auc")} for n in names}
    fno_wins = (mean["fno"]["cell_pr_auc"] > mean["production_lr"]["cell_pr_auc"]
                and mean["fno"]["poisson_deviance"] < mean["production_lr"]["poisson_deviance"])
    md = [f"Grid {N}x{N} (~{(box[2] - box[0]) / N:.0f} m cells), {len(meta)} storms, {EPOCHS} epochs, Poisson loss with "
          "an asset-count offset, rotation/flip augmentation. Truth = damaged assets per cell (same as the baseline's "
          "test labels). Cells without assets are excluded.", "",
          "| Model (mean over folds) | Cell PR-AUC | Cell ROC-AUC | Poisson deviance |", "|---|---|---|---|"]
    for n in names:
        md.append(f"| {n} | {mean[n]['cell_pr_auc']:.3f} | {mean[n]['cell_roc_auc']:.3f} | "
                  f"{mean[n]['poisson_deviance']:.0f} |")
    md += ["", "| Held-out storm | FNO PR-AUC | Production PR-AUC | Uniform PR-AUC |", "|---|---|---|---|"]
    for g in folds:
        md.append(f"| {g} | {rows[g]['fno']['cell_pr_auc']:.3f} | {rows[g]['production_lr']['cell_pr_auc']:.3f} | "
                  f"{rows[g]['uniform_by_storm_size']['cell_pr_auc']:.3f} |")
    md += ["", "**Verdict:** " + ("the FNO beats the production model on both measures at the grid level."
                                  if fno_wins else
                                  "the FNO does not beat the production asset-level model, so it is not adopted. "
                                  "With one sample per storm (35, three of them major), a field-to-field operator "
                                  "has far too few training examples, and the asset attributes that drive damage "
                                  "(age, material, height, history) are blurred away when rasterized.")]
    out = {"folds": rows, "mean": mean, "fno_wins": bool(fno_wins), "markdown": "\n".join(md),
           "seconds": round(time.time() - t0)}
    (ART / "fno_results.json").write_text(json.dumps(out, indent=2))
    print(out["markdown"])


if __name__ == "__main__":
    main()
