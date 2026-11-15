"""Build the harmonized MODIS -> VIIRS hotspot dataset.

Pipeline
  1. Load FIRMS yearly CSVs (MODIS C6.1, VIIRS S-NPP) with DuckDB.
  2. Keep only presumed vegetation fires (type = 0) with usable confidence
     (MODIS >= 30, VIIRS nominal/high).
  3. Aggregate to a 0.25 deg grid x calendar month.
  4. Fit a scaling model on the overlap years where both sensors fly:
        VIIRS_hat(cell, month) = MODIS(cell, month) * r[m] * k[cell]
     r[m]   - regional VIIRS/MODIS ratio for calendar month m (seasonality)
     k[c]   - per-cell multiplier, shrunk toward 1 with a pseudo-count prior
              (cells with little data behave like the regional average)
     Before Aqua (Nov 2000 - Jun 2002) only Terra flew, so MODIS counts are
     first scaled to "Terra+Aqua" with a per-month factor fitted on 2003-2011.
  5. Validate out-of-sample (train on early overlap years, test on later ones).
  6. Write:
        data/processed/points.parquet  every filtered hotspot + harmonization weight
        data/processed/model.json      coefficients + validation metrics

The harmonized series = VIIRS detections from VIIRS start onward, and
weighted MODIS detections before that. Each point carries `w`, so any area
of interest can be summed exactly from points.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import sys

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import regions_util  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
import domain  # noqa: E402

RAW = domain.RAW
OUT = domain.DATA

CELL = 0.25                      # grid size, degrees
VIIRS_START = dt.date(2012, 1, 20)   # first S-NPP VIIRS data in FIRMS
AQUA_START = dt.date(2002, 7, 4)     # first Aqua MODIS data
FIT_YEARS = (2012, 2021)         # both sensors stable (before Terra/Aqua orbit drift)
VAL_TRAIN = (2012, 2018)         # out-of-sample check: fit on these...
VAL_TEST = (2019, 2021)          # ...predict these
TERRA_FIT_YEARS = (2003, 2011)   # Terra-only -> Terra+Aqua factor
PRIOR = 30.0                     # pseudo-count for per-cell shrinkage of k
PRIOR_B = 6.0                    # pseudo-months for shrinkage of b (VIIRS-only small fires)
MODIS_MIN_CONF = 30
UNC_TRAIN = (2012, 2015)         # uncertainty: estimate error parameters on this inner split...
UNC_EVAL = (2016, 2018)
Z90 = 1.645
NRT_SIGMA = 0.05                 # current-year API data: ~5% (2025 back-test of country + static mask)


def load_points(con: duckdb.DuckDBPyConnection) -> None:
    modis = (RAW / "modis").as_posix() + "/*.csv*"            # .csv or .csv.gz (big domains are stored compressed)
    viirs = (RAW / "viirs-snpp").as_posix() + "/*.csv*"
    country = r"replace(regexp_extract(filename, '_\d{4}_(.+?)\.csv', 1), '_', ' ')"   # United_States -> United States
    vcols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_csv('{viirs}', union_by_name = true)").fetchall()}
    sat_scale = "coalesce(TRY_CAST(sat_scale AS DOUBLE), 1.0)" if "sat_scale" in vcols else "1.0"
    w, s_, e, n = domain.BBOX
    inside = f"latitude BETWEEN {s_} AND {n} AND longitude BETWEEN {w} AND {e}"
    con.execute(f"""
        CREATE OR REPLACE TABLE pts AS
        SELECT latitude AS lat, longitude AS lon,
               CAST(acq_date AS DATE) AS date,
               lpad(CAST(acq_time AS VARCHAR), 4, '0') AS time,
               'MODIS' AS sensor, satellite,
               CAST(confidence AS VARCHAR) AS conf,
               frp, daynight, {country} AS country, 1.0 AS vscale
        FROM read_csv('{modis}', filename = true, union_by_name = true)
        WHERE type = 0 AND TRY_CAST(confidence AS INTEGER) >= {MODIS_MIN_CONF} AND {inside}
        UNION ALL
        SELECT latitude, longitude, CAST(acq_date AS DATE),
               lpad(CAST(acq_time AS VARCHAR), 4, '0'),
               'VIIRS', CASE WHEN CAST(satellite AS VARCHAR) = 'N20' THEN 'NOAA-20' ELSE 'Suomi NPP' END,
               CAST(confidence AS VARCHAR),
               frp, daynight, {country},
               -- NOAA-20 points filling an S-NPP outage carry a scale to S-NPP sensitivity
               {sat_scale}
        FROM read_csv('{viirs}', filename = true, union_by_name = true)
        WHERE type = 0 AND confidence IN ('n', 'h') AND {inside}
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE pts AS
        SELECT *, year(date) AS year, month(date) AS month,
               CAST(floor(lon / {CELL}) AS INTEGER) AS cx,
               CAST(floor(lat / {CELL}) AS INTEGER) AS cy
        FROM pts
    """)


def monthly_cells(con) -> pd.DataFrame:
    """Counts per cell x year x month: MODIS (all), Terra only, VIIRS."""
    return con.execute("""
        SELECT cx, cy, year, month,
               count(*) FILTER (WHERE sensor = 'MODIS')                        AS modis,
               count(*) FILTER (WHERE sensor = 'MODIS' AND satellite = 'Terra') AS terra,
               count(*) FILTER (WHERE sensor = 'VIIRS')                        AS viirs
        FROM pts GROUP BY ALL
    """).df()


def in_years(df, yrs):
    return df[(df.year >= yrs[0]) & (df.year <= yrs[1])]


def dense(df: pd.DataFrame, years) -> pd.DataFrame:
    """Cell x month table for `years` incl. zero months, over cells active in those years."""
    d = in_years(df, years)
    cells = d[["cx", "cy"]].drop_duplicates()
    ym = pd.DataFrame([(y, m) for y in range(years[0], years[1] + 1) for m in range(1, 13)],
                      columns=["year", "month"])
    out = cells.merge(ym, how="cross").merge(d, on=["cx", "cy", "year", "month"], how="left")
    return out.fillna({"modis": 0, "terra": 0, "viirs": 0})


def fit(df: pd.DataFrame, years) -> dict:
    """Two-part (hurdle) model, fitted on a dense cell x month table.

    MODIS saw fire  : VIIRS_hat = MODIS * r[month] * k[cell]
        r = seasonal regional ratio, k = local multiplier shrunk toward 1.
    MODIS saw none  : VIIRS_hat = b[cell, month]
        expected VIIRS-only small fires, shrunk toward the regional monthly mean.
    """
    d = dense(df, years)
    d = d[~((d.year == 2012) & (d.month == 1))]          # VIIRS starts on 20 Jan 2012
    pos, zero = d[d.modis > 0], d[d.modis == 0]
    by_m = pos.groupby("month")[["modis", "viirs"]].sum()
    r = (by_m.viirs / by_m.modis.clip(lower=1)).to_dict()
    pos = pos.assign(expected=pos.modis * pos.month.map(r))
    by_c = pos.groupby(["cx", "cy"])[["viirs", "expected"]].sum()
    k = (by_c.viirs + PRIOR) / (by_c.expected + PRIOR)
    gm = zero.groupby("month").viirs.mean().to_dict()
    z = zero.groupby(["cx", "cy", "month"]).viirs.agg(["sum", "count"]).reset_index()
    z["b"] = (z["sum"] + PRIOR_B * z.month.map(gm)) / (z["count"] + PRIOR_B)
    return {"r": {int(m): float(v) for m, v in r.items()}, "k": k,
            "b": z.set_index(["cx", "cy", "month"]).b, "b_region": {int(m): float(v) for m, v in gm.items()},
            "cells": d[["cx", "cy"]].drop_duplicates()}


def terra_factor(df: pd.DataFrame) -> dict:
    d = in_years(df, TERRA_FIT_YEARS).groupby("month")[["modis", "terra"]].sum()
    return {int(m): float(v) for m, v in (d.modis / d.terra.clip(lower=1)).items()}


def lookup(df: pd.DataFrame, series: pd.Series, keys: list[str], default) -> np.ndarray:
    v = df.set_index(keys).index.map(series).to_numpy(dtype=float)
    return np.where(np.isnan(v), default, v)


def predict(df: pd.DataFrame, model: dict) -> np.ndarray:
    k = lookup(df, model["k"], ["cx", "cy"], 1.0)
    b = lookup(df, model["b"], ["cx", "cy", "month"], df.month.map(model["b_region"]).to_numpy())
    return np.where(df.modis > 0, df.modis * df.month.map(model["r"]).to_numpy() * k, b)


def metrics(y: np.ndarray, yhat: np.ndarray) -> dict:
    y, yhat = np.asarray(y, float), np.asarray(yhat, float)
    ly, lh = np.log1p(y), np.log1p(yhat)
    ss = ((ly - ly.mean()) ** 2).sum()
    return {
        "r2_log": float(1 - ((ly - lh) ** 2).sum() / ss) if ss else None,
        "pearson_r": float(np.corrcoef(y, yhat)[0, 1]) if len(y) > 2 else None,
        "bias_pct": float((yhat.sum() - y.sum()) / max(y.sum(), 1) * 100),
        "mae": float(np.abs(y - yhat).mean()),
        "n": int(len(y)),
    }


def eval_table(df: pd.DataFrame, train, test_years) -> pd.DataFrame:
    """Fit on `train`, predict VIIRS from MODIS for `test_years` on a dense cell x month table."""
    model = fit(df, train)
    test = dense(df, test_years)
    test = pd.concat([test, model["cells"].merge(test[["year", "month"]].drop_duplicates(), how="cross")
                      ]).drop_duplicates(["cx", "cy", "year", "month"]).fillna({"modis": 0, "terra": 0, "viirs": 0})
    test["ours"] = predict(test, model)
    total_ratio = in_years(df, train).viirs.sum() / max(in_years(df, train).modis.sum(), 1)
    test["const"] = test.modis * total_ratio
    test["raw"] = test.modis.astype(float)
    test["bx"], test["by"] = test.cx // 4, test.cy // 4
    return test


def error_params(test: pd.DataFrame) -> dict:
    """Error model for a harmonized area-month total H:  Var = phi * H + (sigma_sys * H)^2
    phi       - overdispersion of independent cell-level errors (Poisson-like part)
    sigma_sys - month-to-month relative error shared by all cells (regional part)."""
    pos = test[test.ours > 0]
    phi = float(((pos.viirs - pos.ours) ** 2).sum() / pos.ours.sum())
    reg = test.groupby(["year", "month"])[["viirs", "ours"]].sum()
    reg = reg[reg.ours >= 100]
    sigma = float(((reg.viirs - reg.ours) / reg.ours).std())
    return {"phi": phi, "sigma_sys": sigma}


def interval(h, p: dict, z: float = Z90):
    """Half-width = sqrt(z^2*phi*H + (1.645*sigma_sys*H)^2).
    `z` scales only the heavy-tailed local part (calibrated); the regional part is Gaussian."""
    h = np.asarray(h, float)
    half = np.sqrt(z ** 2 * p["phi"] * h + (Z90 * p["sigma_sys"] * h) ** 2)
    return np.maximum(h - half, 0), h + half


LEVELS = {"region_monthly": ["year", "month"], "block_1deg_monthly": ["bx", "by", "year", "month"],
          "cell_monthly": ["cx", "cy", "year", "month"]}


def coverage(test: pd.DataFrame, p: dict, z: float = Z90, levels=LEVELS) -> dict:
    out = {}
    for name, keys in levels.items():
        t = test.groupby(keys)[["viirs", "ours"]].sum()
        t = t[(t.viirs > 0) | (t.ours > 0.5)]
        lo, hi = interval(t.ours, p, z)
        out[name] = float(((t.viirs >= np.floor(lo)) & (t.viirs <= np.ceil(hi))).mean())
    return out


def calibrate_z(test: pd.DataFrame, p: dict, target: float = 0.90) -> float:
    """Heavy-tailed errors make the Gaussian z too wide: pick z giving `target` coverage
    for 1-degree blocks (a typical area of interest)."""
    lvl = {"b": LEVELS["block_1deg_monthly"]}
    for z in np.arange(0.20, 2.51, 0.05):
        if coverage(test, p, z, lvl)["b"] >= target:
            return round(float(z), 2)
    return Z90


def validate(df: pd.DataFrame) -> tuple[dict, dict]:
    test = eval_table(df, VAL_TRAIN, VAL_TEST)
    out = {"train_years": VAL_TRAIN, "test_years": VAL_TEST}
    cols = ["viirs", "ours", "const", "raw"]
    levels = {
        "region_monthly": test.groupby(["year", "month"])[cols].sum(),          # whole region
        "block_1deg_monthly": test.groupby(["bx", "by", "year", "month"])[cols].sum(),  # AOI-sized
        "cell_monthly": test.groupby(["cx", "cy", "year", "month"])[cols].sum(),
    }
    for name, tab in levels.items():
        out[name] = {m: metrics(tab.viirs, tab[m]) for m in ("raw", "const", "ours")}

    # uncertainty: error parameters estimated on an inner split of the training years,
    # then 90% interval coverage checked on the untouched test years
    inner = eval_table(df, UNC_TRAIN, UNC_EVAL)
    p_inner = error_params(inner)
    z_inner = calibrate_z(inner, p_inner)
    p_final = error_params(test)
    unc = {
        "model": "half-width = sqrt(z^2*phi*H + (1.645*sigma_sys*H)^2); z calibrated to 90% coverage on 1-deg blocks",
        "estimated_on": {"train": UNC_TRAIN, "eval": UNC_EVAL, **p_inner, "z": z_inner},
        # honest check: parameters + z from the inner split, coverage on untouched test years
        "coverage_90_on_test": coverage(test, p_inner, z_inner),
        # production parameters: re-estimated on the larger 2012-2018 -> 2019-2021 split
        **p_final,
        "z": calibrate_z(test, p_final),
    }
    return out, unc


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    print("loading CSVs ...", flush=True)
    load_points(con)
    n = con.execute("SELECT sensor, count(*), min(date), max(date) FROM pts GROUP BY 1").fetchall()
    print("filtered points:", n, flush=True)

    df = monthly_cells(con)
    print("validating ...", flush=True)
    val, unc = validate(df)
    for lvl in ("region_monthly", "block_1deg_monthly", "cell_monthly"):
        for m, v in val[lvl].items():
            print(f"  {lvl:20s} {m:6s} R2(log)={v['r2_log']:.3f} r={v['pearson_r']:.3f} bias={v['bias_pct']:+.1f}%")
    print(f"  uncertainty phi={unc['phi']:.2f} sigma_sys={unc['sigma_sys']:.3f} "
          f"90% coverage on test: {unc['coverage_90_on_test']}", flush=True)

    print("fitting final model ...", flush=True)
    model = fit(df, FIT_YEARS)
    tf = terra_factor(df)

    # per-point harmonization weight
    k_df = model["k"].rename("k").reset_index()
    con.register("k_df", k_df)
    con.register("r_df", pd.DataFrame({"month": list(model["r"]), "r": list(model["r"].values())}))
    con.register("t_df", pd.DataFrame({"month": list(tf), "tf": list(tf.values())}))
    con.execute(f"""
        CREATE OR REPLACE TABLE pts_w AS
        SELECT p.*,
          CASE
            WHEN p.sensor = 'VIIRS' THEN p.vscale
            WHEN p.date >= DATE '{VIIRS_START}' THEN 0.0
            ELSE r.r * coalesce(k.k, 1.0) * CASE WHEN p.date < DATE '{AQUA_START}' THEN t.tf ELSE 1.0 END
          END AS w
        FROM pts p
        JOIN r_df r USING (month)
        JOIN t_df t USING (month)
        LEFT JOIN k_df k USING (cx, cy)
        WHERE NOT (p.sensor = 'VIIRS' AND p.date < DATE '{VIIRS_START}')
        ORDER BY p.date
    """)
    # admin-1 region per hotspot (on unique ~100 m positions to keep it fast)
    print("assigning regions ...", flush=True)
    u = con.execute("SELECT DISTINCT round(lat, 3) AS la, round(lon, 3) AS lo, country FROM pts_w").df()
    u["region"] = regions_util.assign(u.la.to_numpy(), u.lo.to_numpy(), u.country.to_numpy())
    con.register("u_reg", u)
    con.execute("""
        CREATE OR REPLACE TABLE pts_w AS
        SELECT p.*, r.region FROM pts_w p
        LEFT JOIN u_reg r ON round(p.lat, 3) = r.la AND round(p.lon, 3) = r.lo AND p.country = r.country
        ORDER BY p.date
    """)
    dest = (OUT / "points.parquet").as_posix()
    con.execute(f"COPY pts_w TO '{dest}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)")

    # "fill": expected VIIRS-only small fires in pre-VIIRS cell-months where MODIS saw nothing
    first = pd.Timestamp(con.execute("SELECT min(date) FROM pts WHERE sensor='MODIS'").fetchone()[0])
    months = pd.period_range(first, pd.Timestamp(VIIRS_START) - pd.offsets.MonthEnd(1), freq="M")
    grid = model["cells"].merge(pd.DataFrame({"year": months.year, "month": months.month}), how="cross")
    seen = df[df.modis > 0][["cx", "cy", "year", "month"]].assign(seen=True)
    grid = grid.merge(seen, how="left", on=["cx", "cy", "year", "month"])
    grid = grid[grid.seen.isna()].drop(columns="seen")
    grid["w"] = lookup(grid, model["b"], ["cx", "cy", "month"], grid.month.map(model["b_region"]).to_numpy())
    cell_country = con.execute("""
        SELECT cx, cy, mode(country) AS country FROM pts GROUP BY ALL
    """).df()
    grid = grid.merge(cell_country, how="left", on=["cx", "cy"])
    grid["lat"] = (grid.cy + 0.5) * CELL
    grid["lon"] = (grid.cx + 0.5) * CELL
    grid = grid[grid.w > 0.001].astype({"w": "float32", "year": "int16", "month": "int8"})
    cells = grid[["cx", "cy", "lat", "lon", "country"]].drop_duplicates(["cx", "cy"])
    cells["region"] = regions_util.assign(cells.lat.to_numpy(), cells.lon.to_numpy(), cells.country.to_numpy())
    grid = grid.merge(cells[["cx", "cy", "region"]], on=["cx", "cy"], how="left")
    grid.to_parquet(OUT / "fill.parquet", index=False, compression="zstd")
    print(f"fill rows: {len(grid):,}  sum={grid.w.sum():,.0f}", flush=True)

    # record ends on the last VIIRS day: since 2012 the harmonized series is VIIRS
    bounds = con.execute("""SELECT min(lat), max(lat), min(lon), max(lon), min(date),
                                   max(date) FILTER (WHERE sensor = 'VIIRS') FROM pts_w""").fetchone()
    k = model["k"]
    meta = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "cell_deg": CELL,
        "viirs_start": str(VIIRS_START),
        "aqua_start": str(AQUA_START),
        "fit_years": FIT_YEARS,
        "filters": {"type": 0, "modis_min_conf": MODIS_MIN_CONF, "viirs_conf": ["n", "h"]},
        "monthly_ratio_r": model["r"],
        "terra_to_terra_aqua": tf,
        "cell_multiplier_k": {"n_cells": int(len(k)), "p10": float(k.quantile(.1)),
                              "median": float(k.median()), "p90": float(k.quantile(.9))},
        "small_fire_term_b_region": model["b_region"],
        # VIIRS/MODIS ratio per overlap year: stable while orbits are nominal, drifts after ~2022
        "annual_ratio": {int(y): float(g.viirs.sum() / max(g.modis.sum(), 1))
                         for y, g in df[df.year >= 2013].groupby("year")},
        "validation": val,
        "uncertainty": unc,
        # current year comes from the FIRMS API (SP/NRT), not the yearly archive
        "recent_from": f"{bounds[5].year}-01-01" if (bounds[5].month, bounds[5].day) != (12, 31) else None,
        "recent_sigma": NRT_SIGMA,
        "bounds": {"south": bounds[0], "north": bounds[1], "west": bounds[2], "east": bounds[3]},
        "date_range": [str(bounds[4]), str(bounds[5])],
        "counts": {s: c for s, c, *_ in n},
    }
    (OUT / "model.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    print("wrote", dest, "and model.json", flush=True)


if __name__ == "__main__":
    main()
