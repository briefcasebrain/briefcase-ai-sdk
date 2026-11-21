"""Small statistics toolkit for baseline drift, standard library only.

Formulas (``docs/features/drift-baseline.md`` repeats them with their
limits):

- Population stability index over bins ``i`` with baseline proportions
  ``e_i`` and window proportions ``a_i`` (both floored at ``eps`` so empty
  bins do not divide by zero): ``PSI = sum((a_i - e_i) * ln(a_i / e_i))``.
  Rule of thumb, not a statistical test: < 0.1 stable, 0.1-0.25 moderate,
  > 0.25 large shift. With few observations per bin it is noisy and biased
  upwards.
- Two-sample Kolmogorov-Smirnov statistic ``D = sup_x |F_a(x) - F_b(x)|``
  over the empirical CDFs. The asymptotic 5% critical value is
  ``c(alpha) * sqrt((n + m) / (n * m))`` with ``c(alpha) =
  sqrt(-ln(alpha / 2) / 2)`` (1.358 at alpha = 0.05); it is approximate for
  small samples and with ties.
- Two-proportion z test: ``p = (x1 + x2) / (n1 + n2)``,
  ``z = (p2 - p1) / sqrt(p (1 - p) (1/n1 + 1/n2))``, two-sided
  ``p_value = erfc(|z| / sqrt(2))``. The normal approximation needs roughly
  five or more events and non-events on each side.
- Jensen-Shannon divergence, base 2 (so in [0, 1]):
  ``JSD = KL(P || M) / 2 + KL(Q || M) / 2`` with ``M = (P + Q) / 2``.
- Pearson chi-square test of homogeneity on a 2 x k table, ``df = k - 1``,
  p-value from the regularized upper incomplete gamma function. Expected
  counts under five make it unreliable.
"""

from __future__ import annotations

import bisect
import math
import zlib
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PSI_EPS = 1e-4


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (Hyndman-Fan type 7, numpy's default)
    of an already sorted, non-empty sequence."""
    if not sorted_values:
        raise ValueError("quantile of an empty sequence")
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def quantiles(values: Iterable[float], points: int = 101) -> List[float]:
    """``points`` evenly spaced quantiles from the minimum to the maximum."""
    ordered = sorted(values)
    return [quantile(ordered, i / (points - 1)) for i in range(points)]


def bin_index(edges: Sequence[float], value: float) -> int:
    """Bin of ``value`` for interior ``edges``: bin 0 is ``(-inf, e0]``,
    bin i is ``(e_{i-1}, e_i]``, the last bin is ``(e_last, inf)``."""
    return bisect.bisect_left(edges, value)


def proportions(values: Iterable[float], edges: Sequence[float]) -> List[float]:
    counts = [0.0] * (len(edges) + 1)
    n = 0
    for value in values:
        counts[bin_index(edges, value)] += 1
        n += 1
    return [c / n for c in counts] if n else counts


def psi_edges(sorted_values: Sequence[float], bins: int = 10) -> List[float]:
    """Interior bin edges at the baseline's ``1/bins`` quantiles, duplicates
    removed (ties at a quantile collapse bins)."""
    edges: List[float] = []
    for i in range(1, bins):
        edge = quantile(sorted_values, i / bins)
        if not edges or edge > edges[-1]:
            edges.append(edge)
    return edges


def psi(expected: Sequence[float], actual: Sequence[float], eps: float = PSI_EPS) -> float:
    """Population stability index between two proportion vectors."""
    if len(expected) != len(actual):
        raise ValueError("psi needs proportion vectors of the same length")
    total = 0.0
    for e, a in zip(expected, actual):
        e, a = max(e, eps), max(a, eps)
        total += (a - e) * math.log(a / e)
    return total


def ks_2samp(a: Sequence[float], b: Sequence[float]) -> float:
    """Exact two-sample Kolmogorov-Smirnov statistic ``D``."""
    if not a or not b:
        raise ValueError("ks_2samp needs two non-empty samples")
    xs, ys = sorted(a), sorted(b)
    n, m = len(xs), len(ys)
    i = j = 0
    d = 0.0
    while i < n and j < m:
        x = min(xs[i], ys[j])
        while i < n and xs[i] <= x:
            i += 1
        while j < m and ys[j] <= x:
            j += 1
        d = max(d, abs(i / n - j / m))
    return d


def ks_critical(n: int, m: int, alpha: float = 0.05) -> float:
    """Asymptotic critical value of ``D`` at level ``alpha``."""
    c = math.sqrt(-math.log(alpha / 2) / 2)
    return c * math.sqrt((n + m) / (n * m))


def cdf_from_quantiles(table: Sequence[float], x: float) -> float:
    """Baseline CDF at ``x`` read from an evenly spaced quantile table by
    linear interpolation (the profile keeps the table, not the values)."""
    points = len(table) - 1
    if x < table[0]:
        return 0.0
    if x >= table[-1]:
        return 1.0
    hi = bisect.bisect_right(table, x)
    lo = hi - 1
    span = table[hi] - table[lo]
    frac = 0.0 if span <= 0 else (x - table[lo]) / span
    return (lo + frac) / points


def cdf_left_from_quantiles(table: Sequence[float], x: float) -> float:
    """Left limit ``F(x-)`` of :func:`cdf_from_quantiles` (differs from it
    only at atoms, where the table repeats a value)."""
    points = len(table) - 1
    if x <= table[0]:
        return 0.0
    if x > table[-1]:
        return 1.0
    hi = bisect.bisect_left(table, x)
    lo = hi - 1
    span = table[hi] - table[lo]
    frac = 1.0 if span <= 0 else (x - table[lo]) / span
    return (lo + frac) / points


def ks_against_quantiles(table: Sequence[float], sample: Sequence[float]) -> float:
    """Approximate two-sample ``D`` between a baseline summarised by its
    quantile table and a raw window sample. The window ECDF is a step
    function, so the supremum is reached at a window value, either at it
    (``F_w(x)`` against ``F_b(x)``) or just before it (``F_w(x-)`` against
    ``F_b(x-)``). With 101 quantiles the baseline CDF is interpolated to
    within about 0.01."""
    ys = sorted(sample)
    m = len(ys)
    d = 0.0
    k = 0
    while k < m:
        x = ys[k]
        below = k
        while k < m and ys[k] == x:
            k += 1
        d = max(d, abs(below / m - cdf_left_from_quantiles(table, x)),
                abs(k / m - cdf_from_quantiles(table, x)))
    return d


def two_proportion_z(x1: int, n1: int, x2: int, n2: int) -> Tuple[float, float]:
    """``(z, two_sided_p)`` for H0: the two rates are equal."""
    if n1 <= 0 or n2 <= 0:
        raise ValueError("two_proportion_z needs positive sample sizes")
    pooled = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (x2 / n2 - x1 / n1) / se
    return z, math.erfc(abs(z) / math.sqrt(2))


def _normalize(counts: Mapping[str, float], keys: Iterable[str]) -> List[float]:
    total = float(sum(counts.values()))
    return [(counts.get(k, 0) / total) if total else 0.0 for k in keys]


def jsd(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """Jensen-Shannon divergence (base 2) between two count mappings."""
    keys = sorted(set(p) | set(q))
    pp, qq = _normalize(p, keys), _normalize(q, keys)

    def kl(a: List[float], b: List[float]) -> float:
        return sum(x * math.log2(x / y) for x, y in zip(a, b) if x > 0)

    m = [(x + y) / 2 for x, y in zip(pp, qq)]
    return max(0.0, kl(pp, m) / 2 + kl(qq, m) / 2)


def gammaincc(a: float, x: float) -> float:
    """Regularized upper incomplete gamma ``Q(a, x)`` (series for
    ``x < a + 1``, Lentz continued fraction otherwise)."""
    if x <= 0:
        return 1.0
    log_prefix = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(1000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_prefix))
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return min(1.0, math.exp(log_prefix) * h)


def chi2_sf(stat: float, df: int) -> float:
    """Survival function of the chi-square distribution."""
    return gammaincc(df / 2, stat / 2)


def chi_square_homogeneity(a: Mapping[str, float], b: Mapping[str, float]
                           ) -> Tuple[float, int, float, float]:
    """``(statistic, df, p_value, min_expected)`` for H0: both samples come
    from the same categorical distribution."""
    keys = sorted(set(a) | set(b))
    na, nb = float(sum(a.values())), float(sum(b.values()))
    total = na + nb
    if not keys or na == 0 or nb == 0:
        raise ValueError("chi-square needs two non-empty samples")
    stat = 0.0
    min_expected = math.inf
    for key in keys:
        col = a.get(key, 0) + b.get(key, 0)
        for observed, n in ((a.get(key, 0), na), (b.get(key, 0), nb)):
            expected = n * col / total
            min_expected = min(min_expected, expected)
            stat += (observed - expected) ** 2 / expected
    df = len(keys) - 1
    return stat, df, (chi2_sf(stat, df) if df > 0 else 1.0), min_expected


def ngram_buckets(texts: Iterable[str], n: int = 3, buckets: int = 4096) -> Dict[str, int]:
    """Character ``n``-gram counts hashed (CRC-32) into ``buckets`` bins,
    as a sparse ``{bucket: count}`` mapping with string keys. The bins
    cannot be turned back into text."""
    counts: Dict[str, int] = {}
    for text in texts:
        padded = f" {text} "
        for i in range(max(0, len(padded) - n + 1)):
            key = str(zlib.crc32(padded[i:i + n].encode("utf-8")) % buckets)
            counts[key] = counts.get(key, 0) + 1
    return counts


def cosine(u: Sequence[float], v: Sequence[float]) -> Optional[float]:
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(x * x for x in v))
    if nu == 0 or nv == 0 or len(u) != len(v):
        return None
    return sum(x * y for x, y in zip(u, v)) / (nu * nv)


__all__ = [
    "bin_index",
    "cdf_from_quantiles",
    "cdf_left_from_quantiles",
    "chi2_sf",
    "chi_square_homogeneity",
    "cosine",
    "gammaincc",
    "jsd",
    "ks_2samp",
    "ks_against_quantiles",
    "ks_critical",
    "ngram_buckets",
    "proportions",
    "psi",
    "psi_edges",
    "quantile",
    "quantiles",
    "two_proportion_z",
]
