"""AI forecast at the level of places: 14-day fire probability for every ~25 km cell (0.25° grid).

For a regional emergency service a province-wide number is too coarse. This model answers, for each
0.25° cell (~25 x 28 km) inside a province: "what is the chance that satellites detect at least one
fire here in the next 14 days?"

Data: VIIRS 375 m hotspots (2012-2026, the sensor that sees small fires), NASA POWER weather of the
province the cell belongs to (same anomalies as the province model, scripts/build_risk.py).
Features at issue date t (every 7 days):
  place      cell_f14 (fires in the cell, last 14 d), cell_f56 (last 8 weeks), cell_ly (same 14 d last year),
             cell_season (cell's usual next-14-day fires for this week of the year, training years),
             cell_level (cell's long-term weekly activity), nbr_f14 (fires in the 8 surrounding cells, 14 d)
  province   weather anomalies (heat, dryness, rain 1 week-6 months, soil moisture, wind, sun),
             province fires14 / neighbours anomalies
  season     week of year (sin / cos)
Model: sklearn HistGradientBoosting. Validation: fit 2012-2019, test 2020-2025 (never seen), compared with
the cell's own seasonal climatology. Output: data/processed/risk_cells.json (current forecast per cell).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_risk as br  # noqa: E402  (province weather features, same definitions)

ROOT = Path(__file__).resolve().parents[1]
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import domain  # noqa: E402
DATA = domain.DATA
TRAIN = (2012, 2019)
TEST = (2020, 2025)
CELL = 0.25
DAY0 = pd.Timestamp("2011-06-01")
PROV = [c + "_a" for c in br.WEATHER] + ["fires14_a", "nbr14_a"]
PLACE = ["cell_f14", "cell_f56", "cell_ly", "cell_season", "cell_level", "nbr_f14"]
FEATURES = PLACE + PROV + ["woy_sin", "woy_cos"]
GROUPS = {"place_fires": ["cell_f14", "cell_f56"], "neighbours": ["nbr_f14"], "last_year": ["cell_ly"],
          "place_season": ["cell_season", "cell_level"],
          "weather": [c + "_a" for c in br.WEATHER], "province_fires": ["fires14_a", "nbr14_a"]}
LEVELS = [(0.5, "very_high"), (0.25, "high"), (0.1, "elevated"), (0.0, "low")]
MIN_TOTAL = int(os.environ.get("FC_CELL_MIN", 1))        # US: skip cells with almost no fires (memory)
KEEP_NEG = float(os.environ.get("FC_CELL_NEG", 1.0))     # US: keep this share of negative cell-weeks; corrected below


def calib(p):
    """Undo negative sampling: true odds = sampled odds x KEEP_NEG."""
    if KEEP_NEG >= 1:
        return p
    o = p / np.clip(1 - p, 1e-9, None) * KEEP_NEG
    return o / (1 + o)


def level(p: float) -> str:
    return next(name for thr, name in LEVELS if p >= thr)


def main() -> None:
    t_start = time.time()
    meta = json.loads((DATA / "model.json").read_text(encoding="utf-8"))
    last = pd.Timestamp(meta["date_range"][1])
    con = duckdb.connect()
    pts = con.execute(f"""
        SELECT cx, cy, date, count(*) n, mode(region) region FROM read_parquet('{(DATA / 'points.parquet').as_posix()}')
        WHERE sensor = 'VIIRS' AND region IS NOT NULL GROUP BY ALL""").df()
    pts["date"] = pd.to_datetime(pts.date)
    cells = pts.groupby(["cx", "cy"]).agg(region=("region", lambda s: s.value_counts().index[0]), total=("n", "sum")).reset_index()
    cells = cells[cells.total >= MIN_TOTAL].reset_index(drop=True)          # large domains: drop places with almost no fires
    cells["i"] = np.arange(len(cells))
    print(f"{len(cells):,} active cells (>= {MIN_TOTAL} hotspots), {pts.n.sum():,} VIIRS hotspots", flush=True)

    days = pd.date_range(DAY0, last)
    nd = len(days)
    cum = np.zeros((len(cells), nd + 1), np.float32)              # +1: index 0 = before DAY0
    idx = pts.merge(cells[["cx", "cy", "i"]], on=["cx", "cy"])
    di = ((idx.date - DAY0).dt.days + 1).to_numpy()
    ok = di >= 1
    np.add.at(cum, (idx.i.to_numpy()[ok], di[ok]), idx.n.to_numpy()[ok].astype(np.float32))
    np.cumsum(cum, axis=1, out=cum)                               # cum[:, k] = fires up to day k (in place: one array)

    # issue dates: same weekly schedule as the province model
    iss = [d for d in days if (d.dayofyear - 1) % br.STEP == 0 and d >= DAY0 + pd.Timedelta(days=400)]
    k = np.array([(d - DAY0).days + 1 for d in iss])
    def win(a, b):                                                 # fires in (t-a, t-b] for every issue, shape cells x issues
        lo, hi = np.clip(k - a, 0, nd), np.clip(k - b, 0, nd)
        return cum[:, hi] - cum[:, lo]
    f14, f56, ly = win(14, 0), win(56, 0), win(365, 351)         # ly = (t-365, t-351]: same 14 days a year earlier
    nxt = np.where((k + 14 <= nd)[None, :], cum[:, np.clip(k + 14, 0, nd)] - cum[:, k], np.nan).astype(np.float32)
    del cum

    # 8 neighbours by index lookup (no dense grid: works for continent-sized domains)
    gx, gy = cells.cx.to_numpy(), cells.cy.to_numpy()
    pos = {(x, y): i for i, (x, y) in enumerate(zip(gx, gy))}
    nbr = np.zeros_like(f14)
    for a in (-1, 0, 1):
        for b in (-1, 0, 1):
            if a == 0 and b == 0:
                continue
            j = np.array([pos.get((x + a, y + b), -1) for x, y in zip(gx, gy)])
            m_ = j >= 0
            nbr[m_] += f14[j[m_]]

    dates = pd.DatetimeIndex(iss)
    woy = np.minimum((dates.day_of_year - 1) // 7, 51).to_numpy()
    years = dates.year.to_numpy()
    trcols = (years >= TRAIN[0]) & (years <= TRAIN[1])
    # place climatology from the training years only (full data, before any sampling)
    season = np.zeros((len(cells), 52), np.float32)
    lognext = np.log1p(np.nan_to_num(nxt))
    for wk in range(52):
        cols = trcols & (woy == wk)
        if cols.any():
            season[:, wk] = lognext[:, cols].mean(axis=1)
    lvl = f14[:, trcols].mean(axis=1)

    # rows to keep: every positive, a share of negatives (KEEP_NEG, corrected below), all rows of the last weeks
    y_all = np.where(np.isnan(nxt), np.nan, (nxt >= 1).astype(np.float32))
    rng0 = np.random.default_rng(1)
    keep = (y_all == 1) | ((y_all == 0) & (rng0.random(y_all.shape) < KEEP_NEG))
    keep[:, np.array([d >= dates[-1] - pd.Timedelta(days=21) for d in dates])] = True
    ci, ii = np.nonzero(keep)
    df = pd.DataFrame({"i": ci, "date": dates[ii], "woy": woy[ii], "year": years[ii],
                       "f14": f14[ci, ii], "f56": f56[ci, ii], "ly": ly[ci, ii], "nbr": nbr[ci, ii], "next14": nxt[ci, ii]})
    df = df.merge(cells[["i", "region", "total"]], on="i")
    df["cell_season"] = season[df.i.to_numpy(), df.woy.to_numpy()]
    df["cell_level"] = np.log1p(lvl[df.i.to_numpy()])
    df["cell_f14"], df["cell_f56"], df["cell_ly"], df["nbr_f14"] = np.log1p(df.f14), np.log1p(df.f56), np.log1p(df.ly), np.log1p(df.nbr)
    df["woy_sin"], df["woy_cos"] = np.sin(2 * np.pi * df.woy / 52), np.cos(2 * np.pi * df.woy / 52)
    df["y"] = (df.next14 >= 1).astype(float)
    df.loc[df.next14.isna(), "y"] = np.nan
    df["wt"] = np.where(df.y == 0, 1.0 / KEEP_NEG, 1.0)               # weight undoes the negative sampling in metrics
    print(f"cell-issues kept {len(df):,} of {y_all.size:,} ({time.time() - t_start:.0f}s)", flush=True)

    # province weather anomalies (exactly as in the province model)
    w = pd.read_parquet(DATA / "weather.parquet")
    fires = br.daily_fires(last)
    pf, _ = br.anomalies(br.rolling_features(w, fires))
    pf = pf[["region", "date"] + PROV]
    df = df.merge(pf, on=["region", "date"], how="left")
    print(f"weather joined ({time.time() - t_start:.0f}s)", flush=True)

    lab = df.dropna(subset=["y"] + PROV)
    tr = lab[lab.year.between(*TRAIN)]
    te = lab[lab.year.between(*TEST)]
    print(f"train {len(tr):,} rows (positive {tr.y.mean():.3f}), test {len(te):,} (positive {te.y.mean():.3f})", flush=True)

    def fit(X, y):
        return HistGradientBoostingClassifier(max_depth=6, learning_rate=0.08, max_iter=300, l2_regularization=1.0,
                                              min_samples_leaf=200, early_stopping=False, random_state=0).fit(X, y)
    m = fit(tr[FEATURES], tr.y)
    p = calib(m.predict_proba(te[FEATURES])[:, 1])
    pcs = tr.assign(yw=tr.y * tr.wt).groupby(["i", "woy"]).agg(yw=("yw", "sum"), w=("wt", "sum"))
    pc_tab = (pcs.yw / pcs.w).rename("pc")
    base_rate = float((tr.y * tr.wt).sum() / tr.wt.sum())
    clim = te.join(pc_tab, on=["i", "woy"]).pc.fillna(base_rate).to_numpy()
    wt = te.wt.to_numpy()
    bc = brier_score_loss(te.y, clim, sample_weight=wt)
    auc = roc_auc_score(te.y, p, sample_weight=wt)
    rng = np.random.default_rng(0)
    imp = {}
    for g, cols in GROUPS.items():
        X = te[FEATURES].copy()
        for c in cols:
            X[c] = rng.permutation(X[c].to_numpy())
        imp[g] = round(float(auc - roc_auc_score(te.y, m.predict_proba(X)[:, 1], sample_weight=wt)), 4)
    hi = p >= 0.25
    val = {"train_years": TRAIN, "test_years": TEST, "n_train": int(len(tr)), "n_test": int(len(te)),
           "event_rate_test": round(float((te.y * te.wt).sum() / te.wt.sum()), 3), "negative_sampling": KEEP_NEG,
           "auc": round(float(auc), 3), "auc_place_climatology": round(float(roc_auc_score(te.y, clim, sample_weight=wt)), 3),
           "brier_skill_vs_climatology": round(float(1 - brier_score_loss(te.y, p, sample_weight=wt) / bc), 3),
           "hit_rate_when_high": round(float((te.y[hi] * wt[hi]).sum() / wt[hi].sum()), 3) if hi.any() else None,
           "share_of_fires_in_high_cells": round(float(hi[te.y.to_numpy() == 1].mean()), 3),
           "importance_auc_drop": dict(sorted(imp.items(), key=lambda kv: -kv[1])), "n_features": len(FEATURES)}
    print(json.dumps(val, indent=2), flush=True)

    # final model on everything labelled, forecast for the latest issue date
    m = fit(lab[FEATURES], lab.y)
    t0 = min(w.groupby("region").date.max().min(), last)
    t0 = max(d for d in iss if d <= t0)
    now = df[(df.date == t0)].dropna(subset=PROV).copy()
    now["p"] = calib(m.predict_proba(now[FEATURES])[:, 1])
    contrib = {}
    for g, cols in GROUPS.items():                                  # push = p - p(if this group were typical)
        alt = now[FEATURES].copy()
        for c in cols:
            alt[c] = 0.0 if c in PROV else lab[c].median()
        contrib[g] = now.p.to_numpy() - calib(m.predict_proba(alt)[:, 1])
    now = now.merge(cells[["i", "cx", "cy"]], on="i")
    out_cells: dict[str, list] = {}
    for j, r in enumerate(now.itertuples()):
        drv = sorted(((g, float(v[j])) for g, v in contrib.items() if v[j] > 0.02), key=lambda kv: -kv[1])[:2]
        out_cells.setdefault(r.region, []).append([
            round(r.cy * CELL, 2), round(r.cx * CELL, 2), round(float(r.p), 3), int(r.f14), int(r.ly),
            [g for g, _ in drv]])
    for rid in out_cells:
        out_cells[rid].sort(key=lambda c: -c[2])
    out = {"issue_date": str(t0.date()), "horizon_days": 14, "cell_deg": CELL,
           "definition": "probability that satellites (VIIRS) detect at least one fire in this ~25 km cell in the next 14 days",
           "columns": ["lat", "lon", "prob", "fires_last14", "fires_same14_last_year", "drivers"],
           "levels": {"low": "< 10%", "elevated": "10-25%", "high": "25-50%", "very_high": ">= 50%"},
           "model": f"AI: gradient boosting (sklearn HistGradientBoosting), {len(FEATURES)} inputs, VIIRS + NASA POWER",
           "validation": val, "regions": out_cells}
    (DATA / "risk_cells.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    allp = now.p.to_numpy()
    print(f"issue {t0.date()}: {len(now):,} cells, high+ {(allp >= 0.25).sum()}, max {allp.max():.2f} ({time.time() - t_start:.0f}s)")


if __name__ == "__main__":
    main()
