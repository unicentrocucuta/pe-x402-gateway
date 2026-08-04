#!/usr/bin/env python3
"""Expected-value (VE) estimator for bounty/effort decisions.

Honest ranges: conservative / central / optimistic. Never treats podium contests
as guaranteed pay. Dependency-free.
"""
from __future__ import annotations

from typing import Any


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def estimate_ve(payload: dict) -> dict:
    """Estimate net expected USD for a work item.

    Input fields (all optional with defaults):
      reward_usd: nominal reward if paid
      p_pay_central: subjective P(payment | delivery) 0..1
      hours: effort hours
      hourly_cost_usd: opportunity cost per hour (default 25)
      cash_cost_usd: direct cash outlay
      competitors: number of competing claims/PRs
      pay_type: escrow|merge_pay|opire|contest|token|unknown
      notes: free text
    """
    reward = max(0.0, _f(payload.get("reward_usd") or payload.get("reward")))
    hours = max(0.01, _f(payload.get("hours"), 2.0))
    hourly = max(0.0, _f(payload.get("hourly_cost_usd"), 25.0))
    cash_cost = max(0.0, _f(payload.get("cash_cost_usd"), 0.0))
    competitors = max(0.0, _f(payload.get("competitors"), 0.0))
    pay_type = str(payload.get("pay_type") or payload.get("type") or "unknown").lower()
    p_in = payload.get("p_pay_central")
    p_central = _f(p_in, -1.0)

    # Base P(pay) by rail
    base = {
        "escrow": 0.75,
        "merge_pay": 0.55,
        "algora": 0.55,
        "opire": 0.35,
        "fixed_accept": 0.50,
        "contest": 0.08,
        "podium": 0.08,
        "token": 0.05,
        "illiquid": 0.02,
        "unknown": 0.15,
    }.get(pay_type, 0.15)

    if p_central < 0:
        p_central = base
    else:
        p_central = _clamp(p_central, 0.0, 1.0)

    # Competition haircut (many open PRs → lower merge/select odds)
    if competitors > 0:
        # soft 1/(1+k*n)
        haircut = 1.0 / (1.0 + 0.12 * competitors)
        p_central *= haircut

    if pay_type in ("contest", "podium"):
        p_central = min(p_central, 0.12)
    if pay_type in ("token", "illiquid"):
        p_central = min(p_central, 0.08)

    p_central = _clamp(p_central, 0.0, 0.95)
    p_cons = _clamp(p_central * 0.55, 0.0, 0.9)
    p_opt = _clamp(min(p_central * 1.35, p_central + 0.15), 0.0, 0.98)

    effort_cost = hours * hourly + cash_cost
    # optimistic assumes slightly less effort / higher reward realization
    reward_cons = reward * 0.85
    reward_opt = reward * 1.0

    def net(p: float, r: float, cost_mult: float = 1.0) -> float:
        return p * r - effort_cost * cost_mult

    ve_cons = net(p_cons, reward_cons, 1.15)
    ve_cent = net(p_central, reward, 1.0)
    ve_opt = net(p_opt, reward_opt, 0.85)

    decision = "skip"
    reason = []
    if pay_type in ("contest", "podium"):
        decision = "skip"
        reason.append("contest_podium_not_guaranteed_pay")
    elif pay_type in ("token", "illiquid"):
        decision = "skip"
        reason.append("illiquid_or_token_only")
    elif ve_cent > 20 and p_central >= 0.25:
        decision = "pursue"
        reason.append("central_ve_positive_and_p_pay_ok")
    elif ve_cent > 0 and p_central >= 0.15:
        decision = "consider"
        reason.append("marginal_positive_central_ve")
    elif ve_opt > 10 and ve_cons > -hours * hourly * 0.5:
        decision = "consider"
        reason.append("optimistic_upside_bounded_downside")
    else:
        decision = "skip"
        reason.append("ve_or_p_pay_too_low")

    if competitors >= 50 and pay_type == "opire":
        decision = "skip"
        reason.append("extreme_opire_contention")

    return {
        "ok": True,
        "method": "ve_ranges_v1",
        "inputs": {
            "reward_usd": reward,
            "hours": hours,
            "hourly_cost_usd": hourly,
            "cash_cost_usd": cash_cost,
            "competitors": competitors,
            "pay_type": pay_type,
            "effort_cost_usd": round(effort_cost, 2),
        },
        "p_pay": {
            "conservative": round(p_cons, 4),
            "central": round(p_central, 4),
            "optimistic": round(p_opt, 4),
        },
        "ve_net_usd": {
            "conservative": round(ve_cons, 2),
            "central": round(ve_cent, 2),
            "optimistic": round(ve_opt, 2),
        },
        "decision": decision,
        "reasons": reason,
        "honesty": (
            "Subjective model for triage only. Not a promise of payment. "
            "Contests/podium forced low P(pay). Realized utility only after cash lands."
        ),
        "mode": "demo",
    }
