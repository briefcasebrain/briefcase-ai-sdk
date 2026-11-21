"""Statistical functions against hand-computed fixtures."""

import math
import random

import pytest

from briefcase.drift import _stats as s


def test_quantile_type7():
    assert s.quantile([1, 2, 3, 4], 0.5) == 2.5
    assert s.quantile([1, 2, 3, 4], 0.25) == 1.75
    assert s.quantile([7], 0.9) == 7
    with pytest.raises(ValueError):
        s.quantile([], 0.5)


def test_psi_hand_computed():
    # (0.25-0.5)ln(0.25/0.5) + (0.75-0.5)ln(0.75/0.5) = 0.173287 + 0.101366
    assert s.psi([0.5, 0.5], [0.25, 0.75]) == pytest.approx(0.274653, abs=1e-6)
    assert s.psi([0.2, 0.3, 0.5], [0.2, 0.3, 0.5]) == 0
    # empty bins are floored at 1e-4 instead of dividing by zero
    assert s.psi([1.0, 0.0], [0.0, 1.0]) == pytest.approx(
        2 * (1 - 1e-4) * math.log(1 / 1e-4), rel=1e-9)
    with pytest.raises(ValueError):
        s.psi([1.0], [0.5, 0.5])


def test_bins_and_proportions():
    edges = s.psi_edges([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], bins=2)
    assert edges == [5.5]
    assert s.proportions([1, 5.5, 6, 10], edges) == [0.5, 0.5]  # edge value goes to the lower bin
    assert s.psi_edges([1, 1, 1, 1, 2], bins=4) == [1]  # tied quantiles collapse


def test_ks_hand_computed():
    assert s.ks_2samp([1, 2, 3, 4], [3, 4, 5, 6]) == 0.5
    assert s.ks_2samp([1, 2, 3], [4, 5, 6]) == 1.0
    assert s.ks_2samp([1, 2, 3], [1, 2, 3]) == 0.0
    assert s.ks_2samp([1, 1, 2], [1, 2, 2]) == pytest.approx(1 / 3)  # ties
    assert s.ks_critical(50, 50) == pytest.approx(1.358101 * math.sqrt(100 / 2500), rel=1e-5)


def test_ks_against_quantiles_tracks_the_exact_statistic():
    rng = random.Random(1)
    a = [rng.gauss(0, 1) for _ in range(2000)]
    b = [rng.gauss(0.3, 1) for _ in range(500)]
    assert s.ks_against_quantiles(s.quantiles(a), b) == pytest.approx(s.ks_2samp(a, b), abs=0.01)
    ints_a = [rng.randint(1, 5) for _ in range(300)]
    ints_b = [rng.randint(1, 6) for _ in range(300)]
    assert s.ks_against_quantiles(s.quantiles(ints_a), ints_b) == pytest.approx(
        s.ks_2samp(ints_a, ints_b), abs=0.01)
    assert s.ks_against_quantiles(s.quantiles([5] * 50), [5] * 40) == 0.0


def test_two_proportion_hand_computed():
    # pooled p = 0.15, se = sqrt(0.15*0.85*0.02) = 0.0504975, z = 0.1/se
    z, p = s.two_proportion_z(10, 100, 20, 100)
    assert z == pytest.approx(1.980295, abs=1e-5)
    assert p == pytest.approx(0.047670, abs=1e-5)
    assert s.two_proportion_z(0, 50, 0, 50) == (0.0, 1.0)


def test_jsd_hand_computed():
    assert s.jsd({"a": 1}, {"b": 1}) == pytest.approx(1.0)
    # M = (0.75, 0.25); KL(P||M) = 0.207519, KL(Q||M) = 0.415037
    assert s.jsd({"a": 1, "b": 1}, {"a": 2}) == pytest.approx(0.311278, abs=1e-6)
    assert s.jsd({"a": 3, "b": 1}, {"a": 30, "b": 10}) == pytest.approx(0.0)


def test_chi_square_hand_computed_and_known_quantiles():
    stat, df, p, min_expected = s.chi_square_homogeneity({"x": 10, "y": 20}, {"x": 20, "y": 10})
    assert stat == pytest.approx(20 / 3)  # four cells of (5^2)/15
    assert df == 1 and min_expected == 15
    assert p == pytest.approx(0.009823, abs=1e-6)
    # textbook 5% critical values
    assert s.chi2_sf(3.841459, 1) == pytest.approx(0.05, abs=1e-6)
    assert s.chi2_sf(5.991465, 2) == pytest.approx(0.05, abs=1e-6)
    assert s.chi2_sf(18.307038, 10) == pytest.approx(0.05, abs=1e-6)
    assert s.chi2_sf(0.5, 3) == pytest.approx(0.918891, abs=1e-6)


def test_ngram_buckets_are_stable_and_content_free():
    counts = s.ngram_buckets(["abc"])
    assert sum(counts.values()) == 3  # " ab", "abc", "bc "
    assert counts == s.ngram_buckets(["abc"])
    assert all(k.isdigit() for k in counts)


def test_cosine():
    assert s.cosine([1, 0], [1, 0]) == pytest.approx(1)
    assert s.cosine([1, 0], [0, 1]) == pytest.approx(0)
    assert s.cosine([0, 0], [1, 1]) is None
