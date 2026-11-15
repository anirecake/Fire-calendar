"""AI forecast: 14-day fire-activity risk per region (gradient boosting, honest hindcast).

Question: "Will the next 14 days bring MORE fires than usual for this region and time of year?"
  y = next-14-day harmonized hotspots > the region's 80th percentile for that week (training
      years) and at least 10 hotspots.

Features at issue date t, as anomalies vs the region's normal for that week of the year
  weather (NASA POWER, daily)
    tmax30, tmax7     max temperature, last 30 / 7 days            (C)
    rh30              relative humidity, 30 d                      (points)
    vpd30             vapour-pressure deficit (air dryness), 30 d  (kPa)
    pr7, pr30         rain, 7 / 30 d                               (log ratio)
    gw14, gwroot30    topsoil / root-zone soil wetness            (0-1)
    ws14, wsmax14     mean / max wind, 14 d                        (m/s)
    sun30             solar radiation, 30 d                        (kWh/m2/day)
  fuel
    pr90, pr180       rain of the last 3 / 6 months: wet seasons grow grass that burns later
  fires
    fires14           hotspots already burning, 14 d               (log ratio)
    trend7            last 7 days vs the 7 before                  (log ratio)
    nbr14             hotspots in neighbouring regions, 14 d       (log ratio)
    lastyear          same window last year                        (log ratio)
    season            normal next-14-day activity for region & week (log)
Model: sklearn HistGradientBoosting. Drivers shown in the UI = risk drop if a group of
conditions were normal. Analogs = past dates with the most similar weather, same region & season.
Validation: fit 2001-2018, test 2019-2025; ablation shows what each data source adds.

Output: data/processed/risk.json
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import domain  # noqa: E402
DATA = domain.DATA
TRAIN = (2001, 2018)
TEST = (2019, 2025)
HORIZON = 14
STEP = 7
Q = 0.80
MIN_FIRES = 10

WEATHER_V1 = ["tmax30", "rh30", "pr30", "gw14", "ws14", "tmax7", "pr7"]          # first model
WEATHER_NEW = ["vpd30", "gwroot30", "wsmax14", "sun30"]
FUEL = ["pr90", "pr180"]
WEATHER = WEATHER_V1 + WEATHER_NEW + FUEL
FIRES = ["fires14", "trend7", "nbr14", "lastyear"]
FEATURES = WEATHER + FIRES + ["season"]
V1 = WEATHER_V1 + ["fires14", "season"]
LOGRATIO = {"pr30", "pr7", "pr90", "pr180"}
GROUPS = {"heat": ["tmax30", "tmax7", "sun30"], "dry": ["rh30", "vpd30", "pr30", "pr7", "gw14", "gwroot30"],
          "wind": ["ws14", "wsmax14"], "fuel": ["pr90", "pr180"], "fires": ["fires14", "trend7"],
          "neighbors": ["nbr14"], "lastyear": ["lastyear"]}


def daily_fires(last: pd.Timestamp) -> pd.DataFrame:
    con = duckdb.connect()
    p = con.execute(f"SELECT region, date, sum(w) f FROM read_parquet('{(DATA/'points.parquet').as_posix()}') GROUP BY ALL").df()
    fl = con.execute(f"SELECT region, year, month, sum(w) w FROM read_parquet('{(DATA/'fill.parquet').as_posix()}') GROUP BY ALL").df()
    p["date"] = pd.to_datetime(p.date)
    rows = []
    for r in fl.itertuples():                          # spread monthly fill evenly over its days
        days = pd.date_range(pd.Timestamp(r.year, r.month, 1), periods=pd.Timestamp(r.year, r.month, 1).days_in_month)
        rows.append(pd.DataFrame({"region": r.region, "date": days, "f": r.w / len(days)}))
    f = pd.concat([p] + rows).groupby(["region", "date"]).f.sum()
    idx = pd.MultiIndex.from_product([f.index.levels[0], pd.date_range("2000-06-01", last)], names=["region", "date"])
    return f.reindex(idx, fill_value=0.0).rename("fires").reset_index()


def neighbours() -> dict[str, list[str]]:
    fc = json.loads((domain.WEB_OUT / "regions.geojson").read_text(encoding="utf-8"))
    ids = [f["properties"]["id"] for f in fc["features"]]
    g = [shape(f["geometry"]).buffer(0.05) for f in fc["features"]]
    tree = shapely.STRtree(g)
    out = {}
    for i, rid in enumerate(ids):
        out[rid] = [ids[j] for j in tree.query(g[i], predicate="intersects") if j != i]
    return out


def vpd(tmax, tdew):
    es = lambda T: 0.6108 * np.exp(17.27 * T / (T + 237.3))
    return (es(tmax) - es(tdew)).clip(lower=0)


def rolling_features(w: pd.DataFrame, fires: pd.DataFrame) -> pd.DataFrame:
    out = []
    for rid, g in w.groupby("region"):
        g = g.set_index("date").sort_index()
        fr = fires[fires.region == rid].set_index("date").fires.sort_index()
        d = pd.DataFrame(index=g.index)
        r = lambda s, n, how="mean": getattr(s.rolling(n, min_periods=int(n * 0.66)), how)()
        d["tmax30"], d["tmax7"] = r(g.t2m_max, 30), r(g.t2m_max, 7)
        d["rh30"], d["vpd30"] = r(g.rh2m, 30), r(vpd(g.t2m_max, g.t2mdew), 30)
        d["pr7"], d["pr30"] = r(g.prectotcorr, 7, "sum"), r(g.prectotcorr, 30, "sum")
        d["pr90"], d["pr180"] = r(g.prectotcorr, 90, "sum"), r(g.prectotcorr, 180, "sum")
        d["gw14"], d["gwroot30"] = r(g.gwettop, 14), r(g.gwetroot, 30)
        d["ws14"], d["wsmax14"] = r(g.ws10m, 14), r(g.ws10m_max, 14)
        d["sun30"] = r(g.allsky_sfc_sw_dwn, 30)
        f14 = fr.rolling(14, min_periods=1).sum()
        f7, f7p = fr.rolling(7, min_periods=1).sum(), fr.shift(7).rolling(7, min_periods=1).sum()
        nxt = fr[::-1].rolling(HORIZON, min_periods=HORIZON).sum()[::-1].shift(-1)   # sum over (t, t+14]
        d["fires14"] = f14.reindex(d.index)
        d["trend7_raw"] = (np.log1p(f7) - np.log1p(f7p)).reindex(d.index)
        d["next14"] = nxt.reindex(d.index)
        d["lastyear_raw"] = nxt.shift(365).reindex(d.index)                   # same window, one year earlier
        d["region"] = rid
        out.append(d)
    f = pd.concat(out).rename_axis("date").reset_index()
    # neighbours: sum of fires14 in adjacent regions on the same day
    nb = neighbours()
    piv = f.pivot(index="date", columns="region", values="fires14")
    nbr = pd.DataFrame({rid: piv[[n for n in nb.get(rid, []) if n in piv]].sum(axis=1) for rid in piv.columns})
    f = f.merge(nbr.stack().rename("nbr14").reset_index().rename(columns={"level_1": "region"}), on=["date", "region"], how="left")
    f["woy"] = ((f.date.dt.dayofyear - 1) // 7).clip(upper=51)
    return f


def anomalies(f: pd.DataFrame):
    tr = f[f.date.dt.year.between(*TRAIN)]
    base = WEATHER + ["fires14", "nbr14", "next14"]
    clim = tr.groupby(["region", "woy"])[base].mean().add_suffix("_c")
    for c in ("next14", "fires14", "nbr14"):
        clim[f"{c}_log_c"] = tr.assign(l=np.log1p(tr[c])).groupby(["region", "woy"]).l.mean()
    q = {}
    for (rid, wk), _ in clim.iterrows():                 # 80th percentile, pooled over week +-1
        s = tr[(tr.region == rid) & (((tr.woy - wk) % 52).isin([0, 1, 51]))].next14.dropna()
        q[(rid, wk)] = s.quantile(Q) if len(s) else np.nan
    clim["thr"] = pd.Series(q)
    clim["median_next14"] = tr.groupby(["region", "woy"]).next14.median()
    f = f.join(clim, on=["region", "woy"])
    for c in WEATHER:
        f[c + "_a"] = np.log((f[c] + 1) / (f[c + "_c"] + 1)) if c in LOGRATIO else f[c] - f[c + "_c"]
    f["fires14_a"] = np.log1p(f.fires14) - f.fires14_log_c
    f["nbr14_a"] = np.log1p(f.nbr14) - f.nbr14_log_c
    f["trend7_a"] = f.trend7_raw
    f["lastyear_a"] = np.log1p(f.lastyear_raw) - f.next14_log_c
    f["season_a"] = f.next14_log_c
    f["y"] = ((f.next14 > f.thr) & (f.next14 >= MIN_FIRES)).astype(float)
    f.loc[f.next14.isna(), "y"] = np.nan
    return f, clim


def hgb(X, y):
    """Gradient boosting: captures interactions such as 'dry AND in season'."""
    return HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, max_iter=350,
                                          l2_regularization=1.0, min_samples_leaf=80, random_state=0).fit(X, y)


def main() -> None:
    w = pd.read_parquet(DATA / "weather.parquet")
    meta = json.loads((DATA / "model.json").read_text(encoding="utf-8"))
    last_fire = pd.Timestamp(meta["date_range"][1])
    fires = daily_fires(last_fire)
    f = rolling_features(w, fires)
    f, clim = anomalies(f)
    A = [c + "_a" for c in FEATURES]

    issues = f[(f.date.dt.dayofyear - 1) % STEP == 0].dropna(subset=A)
    lab = issues.dropna(subset=["y"])
    tr = lab[lab.date.dt.year.between(*TRAIN)]
    te = lab[lab.date.dt.year.between(*TEST)]
    print(f"issues: train {len(tr):,}, test {len(te):,}, base rate train {tr.y.mean():.3f}, features {len(A)}", flush=True)

    m = hgb(tr[A], tr.y)
    p = m.predict_proba(te[A])[:, 1]
    pc = te.join(tr.groupby(["region", "woy"]).y.mean().rename("pc"), on=["region", "woy"]).pc.fillna(tr.y.mean()).to_numpy()
    bc = brier_score_loss(te.y, pc)

    def score(cols):
        cols = [c + "_a" for c in cols]
        pp = hgb(tr[cols], tr.y).predict_proba(te[cols])[:, 1]
        return {"auc": round(float(roc_auc_score(te.y, pp)), 3), "brier_skill": round(float(1 - brier_score_loss(te.y, pp) / bc), 3)}
    allsc = {"auc": round(float(roc_auc_score(te.y, p)), 3), "brier_skill": round(float(1 - brier_score_loss(te.y, p) / bc), 3)}
    val = {
        "train_years": TRAIN, "test_years": TEST, "issues_test": int(len(te)), "event_rate_test": round(float(te.y.mean()), 3),
        "auc": allsc["auc"], "brier_skill_vs_climatology": allsc["brier_skill"],
        "auc_climatology": round(float(roc_auc_score(te.y, pc)), 3),
        "n_features": len(A), "n_train": int(len(tr)),
        "ablation": {
            "season_only": score(["season"]),
            "season_weather": score(["season"] + WEATHER),
            "season_fires": score(["season", "fires14"]),
            "v1": score(V1),
            "all": allsc,
        },
        "hit_rate_when_high": round(float(te.y[p >= 0.4].mean()), 3) if (p >= 0.4).any() else None,
        "share_of_events_caught_when_elevated": round(float((p[te.y == 1] >= 0.2).mean()), 3),
        "false_alarm_share_when_elevated": round(float(1 - te.y[p >= 0.2].mean()), 3) if (p >= 0.2).any() else None,
    }
    # which inputs matter most overall (permutation importance on the test years, grouped)
    rng = np.random.default_rng(0)
    imp = {}
    for gname, cols in {**GROUPS, "season": ["season"]}.items():
        X = te[A].copy()
        for c in cols:
            X[c + "_a"] = rng.permutation(X[c + "_a"].to_numpy())
        imp[gname] = round(float(allsc["auc"] - roc_auc_score(te.y, m.predict_proba(X)[:, 1])), 4)
    val["importance_auc_drop"] = dict(sorted(imp.items(), key=lambda kv: -kv[1]))
    print(json.dumps(val, indent=2), flush=True)

    # final model on all labelled issues
    m = hgb(lab[A], lab.y)
    clim_all = lab.groupby(["region", "woy"]).y.mean()
    t0 = min(w.groupby("region").date.max().min(), last_fire)
    now = f[f.date == t0].dropna(subset=A).set_index("region")
    prob = pd.Series(m.predict_proba(now[A])[:, 1], index=now.index)
    contrib = pd.DataFrame(index=now.index)
    for gname, cols in GROUPS.items():                 # push = p(actual) - p(if this group were normal)
        alt = now[A].copy()
        alt[[c + "_a" for c in cols]] = 0.0
        contrib[gname] = prob - m.predict_proba(alt)[:, 1]

    WA = [c + "_a" for c in WEATHER]
    mu, sd = lab[WA].mean(), lab[WA].std()
    hist = issues.dropna(subset=["next14"])
    hz = ((hist[WA] - mu) / sd).to_numpy()
    regions = {}
    for rid, row in now.iterrows():
        pcl = float(clim_all.get((rid, row.woy), lab.y.mean()))
        pr = float(prob[rid])
        level = "very_high" if pr >= 0.6 else "high" if pr >= 0.4 else "elevated" if pr >= 0.2 else "low"
        drivers = [{"key": k, "push": round(float(v), 3)} for k, v in contrib.loc[rid].sort_values(ascending=False).items() if v > 0.02][:3]
        doy = abs(hist.date.dt.dayofyear - t0.dayofyear)
        mask = ((hist.region == rid) & (hist.date.dt.year < t0.year) & (np.minimum(doy, 365 - doy) <= 30)).to_numpy()
        cur = ((row[WA].astype(float) - mu) / sd).to_numpy()
        analogs = []
        if mask.any():
            d = np.sqrt(((hz[mask] - cur) ** 2).sum(axis=1))
            cand = hist[mask]
            for i in np.argsort(d)[:3]:
                a = cand.iloc[i]
                analogs.append({"date": str(a.date.date()), "fires_next14": round(float(a.next14)),
                                "typical": round(float(a.median_next14)) if pd.notna(a.median_next14) else None})
        regions[rid] = {
            "prob": round(pr, 3), "usual_prob": round(pcl, 3), "times_usual": round(pr / pcl, 1) if pcl > 0.01 else None,
            "level": level, "drivers": drivers, "analogs": analogs,
            "now": {"tmax30_anom_c": round(float(row.tmax30_a), 1), "rain30_pct_of_normal": round(float((row.pr30 + 1) / (row.pr30_c + 1) * 100)),
                    "rain180_pct_of_normal": round(float((row.pr180 + 1) / (row.pr180_c + 1) * 100)),
                    "rh30_anom": round(float(row.rh30_a), 1), "vpd30_anom": round(float(row.vpd30_a), 2),
                    "soil_wetness_anom": round(float(row.gw14_a), 3), "rootzone_wetness_anom": round(float(row.gwroot30_a), 3),
                    "wind14_anom": round(float(row.ws14_a), 1), "fires_last14": round(float(row.fires14)),
                    "neighbors_fires14": round(float(row.nbr14)),
                    "typical_next14": round(float(row.median_next14)) if pd.notna(row.median_next14) else None},
        }
    out = {"issue_date": str(t0.date()), "horizon_days": HORIZON,
           "definition": f"next {HORIZON} days above the region's {int(Q*100)}th percentile for this time of year (and >= {MIN_FIRES} hotspots)",
           "validation": val, "model": f"AI: gradient boosting (sklearn HistGradientBoosting), {len(A)} inputs; drivers = risk drop if a group of conditions were normal",
           "features": FEATURES, "regions": regions,
           "levels": {"low": "< 20%", "elevated": "20-40%", "high": "40-60%", "very_high": ">= 60%"}}
    (DATA / "risk.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    top = sorted(regions.items(), key=lambda kv: -kv[1]["prob"])[:5]
    print("issue", t0.date())
    for rid, r in top:
        print(rid, r["prob"], r["level"], r["drivers"])


if __name__ == "__main__":
    main()
