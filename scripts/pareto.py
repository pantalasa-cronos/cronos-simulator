"""Shared weighting / sampling helpers for the cronos-simulator.

These utilities intentionally depend only on the Python standard library so
they can run on any GitHub Actions `ubuntu-latest` runner without extra setup.
"""

from __future__ import annotations

import random
from typing import Iterable, Mapping, Sequence, TypeVar

T = TypeVar("T")


def weighted_choice(items: Mapping[T, float] | Sequence[tuple[T, float]],
                    rng: random.Random | None = None) -> T:
    """Return one item sampled proportional to its weight.

    Accepts either a mapping ``{item: weight}`` or a sequence of
    ``(item, weight)`` pairs. Weights must be non-negative and not all zero.
    """
    if isinstance(items, Mapping):
        pairs = list(items.items())
    else:
        pairs = list(items)
    if not pairs:
        raise ValueError("weighted_choice: empty items")

    values, weights = zip(*pairs)
    total = float(sum(weights))
    if total <= 0:
        raise ValueError("weighted_choice: all weights are zero")

    r = (rng or random).random() * total
    upto = 0.0
    for value, weight in zip(values, weights):
        upto += weight
        if upto >= r:
            return value
    return values[-1]


def weighted_sample(items: Mapping[T, float] | Sequence[tuple[T, float]],
                    k: int,
                    rng: random.Random | None = None) -> list[T]:
    """Draw ``k`` items with replacement proportional to their weights.

    Sampling with replacement keeps high-weight items able to appear multiple
    times in the same batch, which is what we want for Pareto-style activity
    (a handful of "hot" repos getting many commits per run).
    """
    return [weighted_choice(items, rng=rng) for _ in range(k)]


def tier_weights(tiers: Mapping[str, float], per_tier: Mapping[str, float]) -> dict[str, float]:
    """Combine tier population shares with per-tier activity weight.

    ``tiers`` maps ``tier_name -> share of repo population (0-1)``.
    ``per_tier`` maps ``tier_name -> activity weight``. The result can be
    fed straight into :func:`weighted_choice`.

    Example::

        tiers     = {"hot": 0.05, "active": 0.25, "dormant": 0.70}
        per_tier  = {"hot": 60,   "active": 10,   "dormant": 0}
        -> {"hot": 3.0, "active": 2.5, "dormant": 0.0}
    """
    out: dict[str, float] = {}
    for name, share in tiers.items():
        out[name] = share * per_tier.get(name, 0.0)
    return out


def pareto_tier(rng: random.Random | None = None,
                shares: Mapping[str, float] | None = None) -> str:
    """Assign a newly-created repo to an activity tier.

    The default shares are tuned for a Pareto-like distribution: a small
    "hot" minority produces most of the activity, a "dormant" tail never
    commits, and everything else is in between. Override ``shares`` to
    change the population mix.
    """
    shares = shares or {
        "hot": 0.05,
        "active": 0.25,
        "maintenance": 0.40,
        "dormant": 0.30,
    }
    return weighted_choice(shares, rng=rng)


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` to the inclusive ``[low, high]`` range."""
    return max(low, min(high, value))


def take(seq: Iterable[T], n: int) -> list[T]:
    """Take up to ``n`` items from an iterable. Convenience for list slicing."""
    out: list[T] = []
    for item in seq:
        if len(out) >= n:
            break
        out.append(item)
    return out
