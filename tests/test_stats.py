"""Statistics helpers: trend test, fire season, circle geometry."""
import math

import numpy as np


def test_mann_kendall_increasing(main):
    t = main.mann_kendall(np.arange(20) * 10 + np.random.default_rng(0).normal(0, 5, 20))
    assert t["direction"] == "increasing" and t["significant_95"] and t["sen_slope_per_year"] > 5


def test_mann_kendall_flat_not_significant(main):
    t = main.mann_kendall(np.random.default_rng(1).normal(100, 10, 25))
    assert not t["significant_95"]


def test_mann_kendall_needs_8_years(main):
    assert main.mann_kendall(np.arange(5)) is None


def test_fire_season_simple(main):
    p = np.array([0, 0, 1, 10, 5, 1, 1, 8, 9, 2, 0, 0], float)
    s = main.fire_season(p)
    assert s["peak_month"] == 4 and s["share"] >= 0.8 and s["risk"][3] == "high" and s["risk"][0] == "low"


def test_fire_season_wraps_new_year(main):
    p = np.array([10, 5, 0, 0, 0, 0, 0, 0, 0, 0, 3, 12], float)
    s = main.fire_season(p)
    assert s["start_month"] in (11, 12) and s["end_month"] in (1, 2)


def test_fire_season_empty(main):
    assert main.fire_season(np.zeros(12)) is None


def test_circle_area(main):
    c = main.circle(45, 70, 50)
    km2 = c.area * 111.32 ** 2 * math.cos(math.radians(45))
    assert abs(km2 - math.pi * 50 ** 2) / (math.pi * 50 ** 2) < 0.01
