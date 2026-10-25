"""Does "you lost power last storm, so you will again" beat simply answering "no"?

usage: python3 lookup_vs_no.py <data_dir>
  data_dir holds the extracted challenge files: arcos_prod_damage_09_28_Clip.shp, Fuse_Bank_Clip.shp,
  Recloser_Bank_Clip.shp, Transformer_Clip.shp, matched_upstream.txt (same folder or Data1/ Data2/ subfolders).

Method
  1. Label which homes lost power in each storm (ARCOS has damage, not outages, so this is derived):
     - each ARCOS damage point is snapped to the nearest fuse, recloser or transformer;
     - a home is "out" in a storm if any device on its upstream chain (matched_upstream.txt) was damaged.
  2. Lookup prediction for the February 2025 storm = "out in Helene (Sept 2024)".
  3. Score it against February's labels, next to the trivial baseline "nobody loses power".
"""
import glob, os, sys
import numpy as np, pandas as pd, pyogrio
from scipy.spatial import cKDTree

d = sys.argv[1]
find = lambda name: glob.glob(os.path.join(d, "**", name), recursive=True)[0]

# --- storms (report-date windows) ---
dmg = pyogrio.read_dataframe(find("arcos_prod_damage_09_28_Clip.shp"), columns=["Report_Tim"]).to_crs(32617)
day = pd.to_datetime(dmg.Report_Tim)
storm = np.where(day.between("2024-09-25", "2024-10-03"), "helene",
                 np.where(day.between("2025-02-10", "2025-02-20"), "feb2025", "other"))

# --- snap each damage point to the nearest protective device / transformer ---
devs = pd.concat([pyogrio.read_dataframe(find(f), columns=["location_n"]).to_crs(32617)
                  for f in ("Fuse_Bank_Clip.shp", "Recloser_Bank_Clip.shp", "Transformer_Clip.shp")])
devs = devs[devs.location_n.notna()]
_, idx = cKDTree(np.c_[devs.geometry.x, devs.geometry.y]).query(np.c_[dmg.geometry.x, dmg.geometry.y])
damaged_device = devs.location_n.to_numpy()[idx]

# --- each home's upstream chain ---
paths = {}
for line in open(find("matched_upstream.txt")):
    f = line.rstrip("\n").split("\t")
    paths[f[0]] = [hop.split("*")[-1] for hop in f[1:] if hop]

def outages(name):
    hit = set(damaged_device[storm == name])
    return pd.Series({home: any(dev in hit for dev in chain) for home, chain in paths.items()})

helene, feb = outages("helene"), outages("feb2025")
pred, truth = helene, feb                      # lookup: "out last storm" -> "out this storm"

tp, fp = int((pred & truth).sum()), int((pred & ~truth).sum())
fn, tn = int((~pred & truth).sum()), int((~pred & ~truth).sum())
n = len(truth)
print(f"homes scored: {n:,}   out in Helene: {helene.mean():.2%}   out in Feb 2025: {feb.mean():.2%}\n")
print("confusion matrix (lookup vs Feb 2025 reality)")
print(f"                 actually out   actually on")
print(f"  predicted out  {tp:12,}   {fp:11,}")
print(f"  predicted on   {fn:12,}   {tn:11,}\n")
print(f"lookup accuracy        {(tp + tn) / n:.1%}")
print(f'"always no" accuracy   {(fp + tn) / n:.1%}   <- the baseline: predict nobody loses power')
print(f"lookup recall          {tp / (tp + fn):.1%}   (share of real outages it warned about)")
print(f"lookup precision       {tp / (tp + fp):.1%}   (share of its warnings that came true)")

# --- optional: score the project's own premise probabilities against "no" on the same labels ---
# python3 lookup_vs_no.py <data_dir> premise_probs.csv   (columns: premise_nu,p ; p = P(outage) for Feb 2025)
# For a fair test the probabilities must come from a model trained WITHOUT the February 2025 storm.
if len(sys.argv) > 2:
    from sklearn.metrics import roc_auc_score, brier_score_loss
    probs = pd.read_csv(sys.argv[2], dtype={"premise_nu": str}).set_index("premise_nu")["p"]
    y = feb.reindex(probs.index).dropna().astype(int)
    p = probs.loc[y.index].clip(0, 1)
    base = y.mean()
    top = p.rank(ascending=False, method="first") <= 0.10 * len(p)
    print(f"\nproject model on {len(y):,} homes (Feb 2025 outage rate {base:.2%})")
    print(f"  ROC-AUC                {roc_auc_score(y, p):.3f}   (\"no\" = 0.500: it cannot rank)")
    print(f"  recall in top 10%      {y[top].sum() / y.sum():.1%}   (\"no\" warns nobody: 0%)")
    b_model, b_no = brier_score_loss(y, p), brier_score_loss(y, np.zeros(len(y)))
    b_rate = brier_score_loss(y, np.full(len(y), base))
    print(f"  Brier score            {b_model:.4f}   vs \"always 0\" {b_no:.4f}, vs \"everyone gets the base rate\" {b_rate:.4f}")
    print(f"  Brier skill vs base    {1 - b_model / b_rate:+.1%}   (above 0 = probabilities beat a constant)")
