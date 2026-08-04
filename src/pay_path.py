#!/usr/bin/env python3
"""Pay-path classifier for bounty/issue text.

Honest gate: distinguish escrow/merge-pay vs contest vs unfunded Opire footer
vs illiquid token. Dependency-free. Never invents a dollar amount.
"""
from __future__ import annotations

import re
from typing import Any


_SKIP_ORGS = {
    "myzubster-ecosystem",
    "zhangjiayang6835-cyber",
    "xevrion",
    "xevrion-v2",
    "vikingr2023",
    "securebananalabs",
    "nspg13",
    "raimeecas",
}

_ILLIQUID = (
    "$myz",
    " $fndry",
    "arrow-only",
    "arrow reward",
    "points only",
    "xp-only",
    "xp only",
    "zero-bounty",
    "zero bounty",
    "zero monetary",
)

_CONTEST = (
    "contest",
    "podium",
    "raffle",
    "leaderboard only",
    "status: competition",
    "status:competition",
    "top 3",
    "1st place",
    "hackathon prize",
)

_FUNDED_MARKERS = (
    "algora.io/bounty",
    "/bounty $",
    "escrow",
    "paid on merge",
    "pay on merge",
    "usdc on merge",
    "reward funded",
    "funded bounty",
)

# Opire marketing footer without an actual posted reward amount
_OPIRE_FOOTER = (
    "this repo is using opire",
    "everyone can add rewards",
    "/reward 100",
    "docs.opire.dev",
)


def _text_blob(payload: dict) -> str:
    parts: list[str] = []
    for k in (
        "title",
        "body",
        "text",
        "description",
        "labels",
        "repo",
        "url",
        "org",
    ):
        v = payload.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            parts.append(" ".join(str(x) for x in v))
        else:
            parts.append(str(v))
    return "\n".join(parts).lower()


def _extract_amounts(text: str) -> list[float]:
    amts: list[float] = []
    for m in re.finditer(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s?k\b", text, re.I):
        amts.append(float(m.group(1).replace(",", "")) * 1000.0)
    for m in re.finditer(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)", text):
        amts.append(float(m.group(1).replace(",", "")))
    # bare "USD 50" / "100 USDC"
    for m in re.finditer(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s?(?:usd|usdc)\b", text, re.I):
        amts.append(float(m.group(1).replace(",", "")))
    for m in re.finditer(r"\b(?:usd|usdc)\s?([0-9][0-9,]*(?:\.[0-9]+)?)\b", text, re.I):
        amts.append(float(m.group(1).replace(",", "")))
    # de-dupe preserve order
    out: list[float] = []
    seen: set[float] = set()
    for a in amts:
        if a in seen:
            continue
        seen.add(a)
        out.append(a)
    return out


def _org_from_payload(payload: dict, blob: str) -> str:
    repo = str(payload.get("repo") or payload.get("repository") or "").strip()
    if "/" in repo:
        return repo.split("/", 1)[0].lower()
    org = str(payload.get("org") or "").strip().lower()
    if org:
        return org
    m = re.search(r"github\.com/([a-z0-9_.-]+)/", blob)
    return m.group(1).lower() if m else ""


def classify_pay_path(payload: dict | None = None) -> dict[str, Any]:
    """Classify whether an issue/listing has a real pay path.

    Returns decision: pursue | consider | skip with reasons.
    """
    payload = payload if isinstance(payload, dict) else {}
    blob = _text_blob(payload)
    title = str(payload.get("title") or "").lower()
    org = _org_from_payload(payload, blob)
    comments = 0
    try:
        comments = int(payload.get("comments") or payload.get("comments_count") or 0)
    except (TypeError, ValueError):
        comments = 0

    reasons: list[str] = []
    flags: list[str] = []
    amounts = _extract_amounts(blob)
    # ignore tiny tip-template examples like /tip 20 when only footer present
    max_amt = max(amounts) if amounts else None

    decision = "consider"
    pay_type = "unknown"
    p_pay_hint = 0.15

    if org and org in _SKIP_ORGS:
        decision = "skip"
        pay_type = "blocked_org"
        reasons.append(f"blocked_org:{org}")
        p_pay_hint = 0.01

    if any(k in blob for k in _ILLIQUID):
        decision = "skip"
        pay_type = "illiquid"
        reasons.append("illiquid_or_zero_bounty_marker")
        p_pay_hint = min(p_pay_hint, 0.05)
        flags.append("illiquid")

    if any(k in blob for k in _CONTEST) or title.count("[bounty]") >= 3:
        decision = "skip"
        pay_type = "contest"
        reasons.append("contest_podium_or_title_farm")
        p_pay_hint = min(p_pay_hint, 0.08)
        flags.append("contest")

    if "funding-pending" in blob:
        decision = "skip"
        reasons.append("funding_pending")
        flags.append("funding_pending")
        p_pay_hint = min(p_pay_hint, 0.05)

    footer_only = any(k in blob for k in _OPIRE_FOOTER) and not any(
        k in blob for k in ("[bounty $", "bounty: $", "reward: $", "powered by opire")
    )
    # Real Opire board issues usually have [BOUNTY $N] in title
    has_bounty_title = bool(re.search(r"\[bounty\s*\$\s*[0-9]", title)) or bool(
        re.search(r"bounty[:\s]*\$\s*[0-9]", title)
    )
    has_algora = "algora.io/bounty" in blob or "/bounty $" in blob or "algora bounty" in blob

    if footer_only and not has_bounty_title and not has_algora and decision != "skip":
        decision = "skip"
        pay_type = "unfunded_opire_footer"
        reasons.append("opire_footer_without_posted_reward")
        flags.append("unfunded_footer")
        p_pay_hint = min(p_pay_hint, 0.05)

    if has_algora and decision != "skip":
        pay_type = "algora"
        p_pay_hint = 0.55
        flags.append("algora")
        if max_amt and max_amt >= 25 and comments <= 25:
            decision = "pursue"
            reasons.append("algora_funded_low_contention")
        elif max_amt:
            decision = "consider"
            reasons.append("algora_with_amount")
        else:
            decision = "consider"
            reasons.append("algora_marker_amount_unclear")

    if has_bounty_title and decision != "skip":
        pay_type = "opire" if "opire" in blob or "claude-builders" in blob else "merge_pay"
        flags.append("bounty_title_amount")
        if max_amt and max_amt >= 50 and comments <= 30:
            decision = "pursue" if comments <= 15 else "consider"
            reasons.append("titled_bounty_with_amount")
            p_pay_hint = 0.35 if pay_type == "opire" else 0.45
        elif max_amt:
            decision = "consider"
            reasons.append("titled_bounty_high_or_unknown_contention")
            p_pay_hint = 0.25
        if comments >= 80:
            decision = "skip" if pay_type == "opire" else "consider"
            reasons.append("extreme_comment_contention")
            p_pay_hint = min(p_pay_hint, 0.12)

    if any(k in blob for k in _FUNDED_MARKERS) and decision == "consider" and pay_type == "unknown":
        pay_type = "escrow" if "escrow" in blob else "merge_pay"
        p_pay_hint = 0.5
        reasons.append("funded_marker_present")
        if max_amt and max_amt >= 50 and comments <= 20:
            decision = "pursue"

    if max_amt is None and decision == "consider" and pay_type == "unknown":
        decision = "skip"
        reasons.append("no_amount_and_no_escrow_marker")
        p_pay_hint = 0.08

    if comments > 100 and decision == "pursue":
        decision = "consider"
        reasons.append("downgraded_high_comments")

    if not reasons:
        reasons.append("insufficient_signal")

    return {
        "ok": True,
        "method": "pay_path_filter_v1",
        "decision": decision,
        "pay_type": pay_type,
        "p_pay_hint_central": round(p_pay_hint, 4),
        "amount_guess_usd": max_amt,
        "amounts_found_usd": amounts[:12],
        "org": org or None,
        "comments": comments,
        "flags": flags,
        "reasons": reasons,
        "honesty": (
            "Heuristic classifier only. amount_guess is parsed from text and may be "
            "stale or decorative. Realized utility only after cash lands. Contests/"
            "illiquid/unfunded footers forced to skip."
        ),
        "mode": "demo",
    }
