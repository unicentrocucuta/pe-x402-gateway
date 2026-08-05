#!/usr/bin/env python3
"""Minimal PE agent gateway — health + agent-card + citation MVP + x402 PayAI settle.

v0.4: PayAI facilitator verify+settle (Base USDC). Demo header still free/size-capped.
"""
from __future__ import annotations

import base64
import csv
import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from citation import score_claim  # noqa: E402
from facilitator import (  # noqa: E402
    PAYAI_URL,
    PAY_TO_DEFAULT,
    USDC_BASE,
    challenge_body,
    payment_requirements,
    verify_and_settle,
)
from pay_path import classify_pay_path, classify_pay_path_batch  # noqa: E402
from ve import estimate_ve  # noqa: E402

CARD = (ROOT / ".well-known" / "agent-card.json").read_text()
HOST = os.environ.get("PE_X402_HOST", "0.0.0.0")
PORT = int(os.environ.get("PE_X402_PORT", "8791"))
VERSION = "0.4.0"
PUBLIC_BASE = os.environ.get("PE_X402_PUBLIC_BASE", "https://x402.lagaceta.net").rstrip("/")
PE_ROOT = Path(os.environ.get("PE_ROOT", "/home/dany/projects/profit-engine"))
SETTLEMENTS_PATH = PE_ROOT / "deliverables" / "SETTLEMENTS.jsonl"
PROFIT_LEDGER = PE_ROOT / "PROFIT_LEDGER.csv"
HEARTBEAT = PE_ROOT / "deliverables" / "ACTIVITY_HEARTBEAT.txt"

# Placeholder prices (USD) until facilitator live
PRICES = {
    "/v1/citation-check": 0.02,
    "/v1/opportunity-scan": 0.01,
    "/v1/diff-review": 0.03,
    "/v1/bounty-triage": 0.02,
    "/v1/ve-estimate": 0.01,
    "/v1/pay-path-filter": 0.01,
    "/v1/batch-pay-path": 0.02,
    "/v1/portfolio-status": 0.01,
    "/v1/pr-watch": 0.01,
}

# Free demo: limited body size; header X-PE-DEMO: 1 bypasses 402 for paid routes
DEMO_HEADER = "x-pe-demo"
MAX_DEMO_CLAIM = 500
MAX_DEMO_SOURCE_CHARS = 4000
MAX_DEMO_DIFF_CHARS = 12000

# Rate limit (per IP, sliding window)
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX = int(os.environ.get("PE_X402_RATE_LIMIT", "60"))

# Metering
_lock = threading.Lock()
_hits: dict[str, int] = defaultdict(int)
_status: dict[str, int] = defaultdict(int)
_demo_hits = 0
_paid_stub_hits = 0
_paid_settled = 0
_started = time.time()
_rate: dict[str, deque] = defaultdict(deque)
# request-local settlement receipt (thread-local)
_tls = threading.local()


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    xff = handler.headers.get("X-Forwarded-For") or ""
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (handler.client_address[0] if handler.client_address else "unknown")[:64]


def _rate_allow(ip: str) -> bool:
    now = time.time()
    with _lock:
        q = _rate[ip]
        while q and now - q[0] > RATE_LIMIT_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT_MAX:
            return False
        q.append(now)
        return True


def _meter(
    path: str,
    code: int,
    *,
    demo: bool = False,
    paid_stub: bool = False,
    settled: bool = False,
) -> None:
    with _lock:
        _hits[path] += 1
        _status[str(code)] += 1
        global _demo_hits, _paid_stub_hits, _paid_settled
        if demo:
            _demo_hits += 1
        if paid_stub:
            _paid_stub_hits += 1
        if settled:
            _paid_settled += 1


def _metrics_snapshot() -> dict:
    with _lock:
        return {
            "ok": True,
            "service": "pe-x402-gateway",
            "version": VERSION,
            "uptime_s": int(time.time() - _started),
            "hits_by_path": dict(_hits),
            "status_counts": dict(_status),
            "demo_hits": _demo_hits,
            "paid_stub_challenges": _paid_stub_hits,
            "paid_settled": _paid_settled,
            "payment": "payai_base_usdc",
            "facilitator": PAYAI_URL,
            "payTo": os.environ.get("PE_X402_PAY_TO", PAY_TO_DEFAULT),
            "asset": USDC_BASE,
            "network": "base",
            "rate_limit": {"window_s": RATE_LIMIT_WINDOW_S, "max": RATE_LIMIT_MAX},
        }


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()).decode()


def _decode_payment_header(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    # Accept raw JSON or base64 JSON
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    try:
        pad = "=" * (-len(raw) % 4)
        data = json.loads(base64.b64decode(raw + pad).decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _log_settlement(path: str, result, amount_atomic: str, usd: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "at": now,
        "path": path,
        "ok": True,
        "tx": result.tx,
        "payer": result.payer,
        "rail": result.rail,
        "amount_atomic": str(amount_atomic),
        "usd": usd,
        "payTo": os.environ.get("PE_X402_PAY_TO", PAY_TO_DEFAULT),
        "network": "base",
        "asset": USDC_BASE,
    }
    try:
        SETTLEMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SETTLEMENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        with HEARTBEAT.open("a", encoding="utf-8") as f:
            f.write(f"{now} SETTLEMENT path={path} tx={result.tx} usd={usd} rail={result.rail}\n")
    except Exception:  # noqa: BLE001
        pass
    # Realized ledger (receive only)
    try:
        new_file = not PROFIT_LEDGER.exists() or PROFIT_LEDGER.stat().st_size == 0
        with PROFIT_LEDGER.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(
                    [
                        "date_utc",
                        "tx_id",
                        "source_platform",
                        "opportunity_id",
                        "gross_received_usd",
                        "direct_costs_usd",
                        "net_realized_usd",
                        "currency_original",
                        "amount_original",
                        "payment_method",
                        "notes",
                    ]
                )
            w.writerow(
                [
                    now,
                    result.tx,
                    "x402_payai",
                    f"pe_x402:{path}",
                    f"{usd:.6f}",
                    "0",
                    f"{usd:.6f}",
                    "USDC",
                    f"{usd:.6f}",
                    "usdc_base",
                    f"payer={result.payer};rail={result.rail}",
                ]
            )
    except Exception:  # noqa: BLE001
        pass


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(min(length, 256_000))
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _opportunity_scan_payload() -> dict:
    """Local snapshot of funded/cash candidates — no invented rewards."""
    hunt = Path("/home/dany/projects/profit-engine/deliverables/HUNTER_LATEST.json")
    items = []
    if hunt.exists():
        try:
            data = json.loads(hunt.read_text())
            for c in (data.get("cash_candidates") or [])[:15]:
                items.append(
                    {
                        "repo": c.get("repo"),
                        "title": c.get("title"),
                        "url": c.get("url"),
                        "amount_guess_usd": c.get("amount_guess"),
                        "comments": c.get("comments"),
                        "score": c.get("score"),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "items": []}
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "filter": "hunter_cash_candidates_no_contests",
        "payment": "stub_or_demo",
    }


def _bounty_triage_payload(data: dict | None = None) -> dict:
    """Rank local hunter/watchdog snapshots; never invent rewards."""
    data = data or {}
    limit = int(data.get("limit") or 10)
    limit = max(1, min(limit, 25))
    skip_orgs = {
        "myzubster-ecosystem",
        "zhangjiayang6835-cyber",
        "xevrion",
    }
    paths = [
        Path("/home/dany/projects/profit-engine/deliverables/HUNTER_LATEST.json"),
        Path("/home/dany/projects/profit-engine/deliverables/HUNT_TICK.json"),
        Path("/home/dany/projects/profit-engine/deliverables/WATCHDOG_STATE.json"),
    ]
    rows: list[dict] = []
    sources_used: list[str] = []
    for p in paths:
        if not p.exists():
            continue
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            continue
        sources_used.append(p.name)
        if isinstance(blob.get("cash_candidates"), list):
            for c in blob["cash_candidates"]:
                rows.append(
                    {
                        "title": c.get("title"),
                        "url": c.get("url"),
                        "repo": c.get("repo"),
                        "comments": c.get("comments"),
                        "amount_guess_usd": c.get("amount_guess") or c.get("amount_guess_usd"),
                        "score_in": c.get("score"),
                        "src": p.name,
                    }
                )
        if isinstance(blob.get("top"), list):
            for c in blob["top"]:
                rows.append(
                    {
                        "title": c.get("title"),
                        "url": c.get("url"),
                        "repo": c.get("repo"),
                        "comments": c.get("comments"),
                        "amount_guess_usd": c.get("amount_guess"),
                        "score_in": c.get("score"),
                        "src": p.name,
                    }
                )
        if isinstance(blob.get("top_actionable"), list):
            for c in blob["top_actionable"]:
                rows.append(
                    {
                        "title": c.get("title"),
                        "url": c.get("url"),
                        "repo": c.get("repo"),
                        "comments": c.get("comments"),
                        "amount_guess_usd": c.get("reward_guess_usd"),
                        "score_in": c.get("score"),
                        "src": p.name,
                    }
                )
    # de-dupe by url
    seen: set[str] = set()
    ranked: list[dict] = []
    for r in rows:
        url = (r.get("url") or "").strip()
        if not url or url in seen:
            continue
        repo = (r.get("repo") or "").lower()
        org = repo.split("/")[0] if "/" in repo else ""
        title = (r.get("title") or "").lower()
        if org in skip_orgs or "funding-pending" in title:
            continue
        if title.count("[bounty]") >= 3:
            continue
        comments = int(r.get("comments") or 0)
        amount = r.get("amount_guess_usd")
        try:
            amount_f = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount_f = None
        score = float(r.get("score_in") or 0)
        if amount_f:
            score += min(amount_f / 50.0, 15.0)
        score -= min(comments, 100) * 0.12
        if comments <= 5:
            score += 3.0
        if comments > 50:
            score -= 6.0
        # contests / illiquid markers
        if any(k in title for k in ("contest", "podium", "raffle", "$myz", "arrow reward")):
            score -= 20.0
        decision = "consider"
        if score < 0:
            decision = "skip"
        elif amount_f and amount_f >= 50 and comments <= 20:
            decision = "pursue"
        elif amount_f is None and comments > 15:
            decision = "skip_unfunded_or_noisy"
        ranked.append(
            {
                "title": r.get("title"),
                "url": url,
                "repo": r.get("repo"),
                "comments": comments,
                "amount_guess_usd": amount_f,
                "score": round(score, 2),
                "decision": decision,
                "src": r.get("src"),
            }
        )
        seen.add(url)
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return {
        "ok": True,
        "count": len(ranked[:limit]),
        "items": ranked[:limit],
        "sources": sources_used,
        "filters": {
            "skip_orgs": sorted(skip_orgs),
            "skip": ["contests", "funding-pending", "title-farms", "illiquid-token-markers"],
        },
        "method": "local_snapshot_triage_v1",
        "payment": "stub_or_demo",
        "honesty": "amount_guess may be null; never treat as guaranteed pay",
    }


def _portfolio_status_payload() -> dict:
    """Local RUN_STATE / open-line snapshot — no invented balances."""
    run_path = Path("/home/dany/projects/profit-engine/RUN_STATE.json")
    profit_path = Path("/home/dany/projects/profit-engine/PROFIT_LEDGER.csv")
    out: dict = {
        "ok": True,
        "method": "portfolio_status_v1",
        "payment": "stub_or_demo",
        "honesty": "Reads local PE state only; net_realized is ledger truth, not pipeline nominal.",
    }
    if run_path.exists():
        try:
            st = json.loads(run_path.read_text(encoding="utf-8"))
            lines = st.get("active_lines") or []
            out["net_realized_usd"] = st.get("net_realized_usd")
            out["last_worker_at_utc"] = st.get("last_worker_at_utc")
            out["active_lines"] = [
                {
                    "id": ln.get("id"),
                    "type": ln.get("type"),
                    "status": ln.get("status"),
                    "reward": ln.get("reward"),
                    "pr": ln.get("pr"),
                    "pay_path": ln.get("pay_path"),
                    "repo": ln.get("repo"),
                }
                for ln in lines
                if isinstance(ln, dict)
            ][:20]
            out["next_actions"] = (st.get("next_actions") or [])[:8]
            out["strategy_prefer"] = (st.get("strategy") or {}).get("prefer")
        except Exception as exc:  # noqa: BLE001
            out["ok"] = False
            out["error"] = str(exc)
    else:
        out["ok"] = False
        out["error"] = "RUN_STATE.json missing"
    # ledger row count only (no full dump)
    if profit_path.exists():
        try:
            rows = profit_path.read_text(encoding="utf-8").strip().splitlines()
            out["profit_ledger_rows"] = max(0, len(rows) - 1)
        except Exception:  # noqa: BLE001
            out["profit_ledger_rows"] = None
    return out


def _pr_watch_payload() -> dict:
    """Local snapshot of tracked PRs from hunter/RUN_STATE — no live network.

    Agents use this to decide whether to ping maintainers or sit quiet.
    Never invents review/merge outcomes.
    """
    hunt = Path("/home/dany/projects/profit-engine/deliverables/HUNTER_LATEST.json")
    run_path = Path("/home/dany/projects/profit-engine/RUN_STATE.json")
    out: dict = {
        "ok": True,
        "method": "pr_watch_local_v1",
        "payment": "stub_or_demo",
        "honesty": (
            "Reads local HUNTER_LATEST + RUN_STATE only. "
            "Does not call GitHub. Stale if hunter not refreshed."
        ),
        "prs": [],
        "open_count": 0,
        "merged_count": 0,
        "needs_attention": [],
    }
    known: list[dict] = []
    if hunt.exists():
        try:
            blob = json.loads(hunt.read_text(encoding="utf-8"))
            out["hunter_scanned_at_utc"] = blob.get("scanned_at_utc")
            for key in ("known_prs", "our_prs"):
                for p in blob.get(key) or []:
                    if isinstance(p, dict):
                        known.append(p)
        except Exception as exc:  # noqa: BLE001
            out["hunter_error"] = str(exc)
    # Enrich with reward labels from RUN_STATE when PR URL matches
    reward_by_url: dict[str, str] = {}
    if run_path.exists():
        try:
            st = json.loads(run_path.read_text(encoding="utf-8"))
            for ln in st.get("active_lines") or []:
                if not isinstance(ln, dict):
                    continue
                pru = (ln.get("pr") or "").strip()
                if pru:
                    reward_by_url[pru] = str(ln.get("reward") or "")
            for opp in st.get("active_opportunities") or []:
                if not isinstance(opp, dict):
                    continue
                pru = (opp.get("pr") or "").strip()
                if pru and pru not in reward_by_url:
                    reward_by_url[pru] = str(opp.get("reward") or "")
        except Exception as exc:  # noqa: BLE001
            out["run_state_error"] = str(exc)

    seen: set[str] = set()
    prs: list[dict] = []
    for p in known:
        url = (p.get("url") or p.get("html_url") or "").strip()
        if not url:
            repo = p.get("repo") or ""
            num = p.get("number")
            if repo and num:
                url = f"https://github.com/{repo}/pull/{num}"
        if not url or url in seen:
            continue
        seen.add(url)
        state = (p.get("state") or "").lower()
        merged = bool(p.get("merged"))
        mergeable = p.get("mergeable")
        row = {
            "repo": p.get("repo"),
            "number": p.get("number"),
            "title": p.get("title"),
            "url": url,
            "state": state or None,
            "merged": merged,
            "mergeable": mergeable,
            "updated_at": p.get("updated_at") or p.get("updated"),
            "draft": p.get("draft"),
            "reward": reward_by_url.get(url) or None,
            "error": p.get("error"),
        }
        prs.append(row)
        if merged or state == "closed" and merged:
            out["merged_count"] += 1
        elif state == "open" or (not merged and state != "closed"):
            out["open_count"] += 1
        # attention heuristics (local only)
        if p.get("error"):
            out["needs_attention"].append({"url": url, "reason": "snapshot_error"})
        elif state == "open" and mergeable is False:
            out["needs_attention"].append({"url": url, "reason": "not_mergeable"})
        elif state == "open" and p.get("draft") is True:
            out["needs_attention"].append({"url": url, "reason": "still_draft"})

    prs.sort(key=lambda r: (0 if r.get("state") == "open" else 1, r.get("url") or ""))
    out["prs"] = prs[:40]
    out["count"] = len(out["prs"])
    return out


def _diff_review_payload(data: dict) -> dict:
    """Lightweight structured review of a provided unified diff (no network)."""
    title = str(data.get("title") or "")[:300]
    diff = str(data.get("diff") or data.get("patch") or "")[:MAX_DEMO_DIFF_CHARS]
    # reuse citation scorer vocabulary lightly + line stats
    lines = diff.splitlines()
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    deleted = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    files = sorted(
        {
            ln.split(" b/", 1)[-1]
            for ln in lines
            if ln.startswith("diff --git ")
        }
    )
    risks = []
    if "eval(" in diff:
        risks.append("Use of eval() in diff")
    if "shell=True" in diff:
        risks.append("subprocess shell=True")
    if not any("test" in f.lower() for f in files) and added > 20:
        risks.append("No test files detected in diff")
    if not risks:
        risks.append("No high-severity static issues detected (heuristic only)")
    conf = "High" if added < 400 and len(risks) <= 1 else "Medium"
    if any("eval" in r.lower() for r in risks):
        conf = "Low"
    summary = (
        f"Diff review for '{title or 'untitled'}': {len(files)} file(s), "
        f"+{added}/-{deleted} lines. Heuristic static pass only."
    )
    return {
        "ok": True,
        "summary": summary,
        "risks": risks,
        "suggestions": [
            "Run project tests before merge",
            "Keep secrets out of the diff",
        ],
        "confidence": conf,
        "files": files[:40],
        "stats": {"added": added, "deleted": deleted, "files": len(files)},
        "method": "diff_heuristic_v1",
        "mode": "demo",
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # quiet

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-PE-Version", VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self,
        code: int,
        obj: dict,
        *,
        path: str = "",
        demo: bool = False,
        paid_stub: bool = False,
        settled: bool = False,
        extra_headers: dict | None = None,
    ):
        if path:
            _meter(path, code, demo=demo, paid_stub=paid_stub, settled=settled)
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-PE-Version", VERSION)
        if settled:
            receipt = getattr(_tls, "receipt", None)
            if isinstance(receipt, dict):
                self.send_header("PAYMENT-RESPONSE", _b64(receipt))
                self.send_header("X-PAYMENT-RESPONSE", _b64(receipt))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _is_demo(self) -> bool:
        return (self.headers.get(DEMO_HEADER) or "").strip() in ("1", "true", "yes")

    def _payment_header_raw(self) -> str:
        for name in (
            "PAYMENT-SIGNATURE",
            "Payment-Signature",
            "X-PAYMENT",
            "X-PAYMENT-SIGNATURE",
            "x-payment",
        ):
            v = self.headers.get(name)
            if v:
                return v
        return ""

    def _try_settle(self, path: str) -> tuple[bool, str | None]:
        """Verify+settle via facilitator. Returns (ok, error_message)."""
        raw = self._payment_header_raw()
        payload = _decode_payment_header(raw)
        if not payload:
            return False, None  # no header → 402 challenge

        usd = float(PRICES.get(path, 0.01))
        amount_atomic = str(int(round(usd * 1_000_000)))
        resource_url = f"{PUBLIC_BASE}{path}"
        reqs = payment_requirements(
            path=path,
            amount_atomic=amount_atomic,
            resource_url=resource_url,
            description=f"PE paid route {path}",
            version=1,
        )
        # Align requirements with client-accepted if present
        accepted = payload.get("accepted") if isinstance(payload.get("accepted"), dict) else None
        if accepted:
            for k in ("network", "asset", "payTo", "scheme"):
                if accepted.get(k):
                    reqs[k] = accepted[k]
            if accepted.get("amount") and not reqs.get("maxAmountRequired"):
                reqs["maxAmountRequired"] = str(accepted["amount"])
            if accepted.get("amount"):
                reqs["amount"] = str(accepted["amount"])
            if accepted.get("maxAmountRequired"):
                reqs["maxAmountRequired"] = str(accepted["maxAmountRequired"])

        result = verify_and_settle(payload, reqs, prefer=os.environ.get("PE_X402_FACILITATOR", "payai"))
        if result.ok and result.tx:
            receipt = {
                "success": True,
                "transaction": result.tx,
                "network": reqs.get("network") or "base",
                "payer": result.payer or "",
                "rail": result.rail,
            }
            _tls.receipt = receipt
            _log_settlement(path, result, amount_atomic, usd)
            return True, None

        err = result.error or "settlement_failed"
        _tls.receipt = {
            "success": False,
            "errorReason": err,
            "transaction": result.tx or "",
            "network": reqs.get("network") or "base",
            "payer": result.payer or "",
            "rail": result.rail,
            "detail": result.body if isinstance(result.body, dict) else {},
        }
        return False, err

    def _payment_required(self, path: str, *, error: str | None = None):
        usd = float(PRICES.get(path, 0.01))
        amount_atomic = str(int(round(usd * 1_000_000)))
        challenge = challenge_body(
            path=path,
            amount_atomic=amount_atomic,
            public_base=PUBLIC_BASE,
            description=f"PE paid route {path}",
        )
        if error:
            challenge["error"] = error
            challenge["settlement_error"] = error
        # Keep demo path documented
        challenge["demo"] = {
            "header": f"{DEMO_HEADER}: 1",
            "note": "Demo is free, rate/size limited; not a paid settlement",
        }
        body = json.dumps(challenge, ensure_ascii=False).encode()
        _meter(path, 402, paid_stub=True)
        self.send_response(402)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-PAYMENT-REQUIRED", "true")
        self.send_header("PAYMENT-REQUIRED", _b64(challenge))
        self.send_header("X-PE-Version", VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _gate_paid(self, path: str) -> bool:
        """Return True if request may proceed (demo or settled payment)."""
        _tls.receipt = None
        _tls.mode = "none"
        if self._is_demo():
            _tls.mode = "demo"
            return True
        ok, err = self._try_settle(path)
        if ok:
            _tls.mode = "settled"
            return True
        if err:
            # Payment header present but failed
            self._payment_required(path, error=err)
            return False
        self._payment_required(path)
        return False

    def _paid_mode_fields(self) -> dict:
        mode = getattr(_tls, "mode", "demo")
        out = {"mode": mode, "payment": "payai_base_usdc" if mode == "settled" else mode}
        receipt = getattr(_tls, "receipt", None)
        if isinstance(receipt, dict) and receipt.get("transaction"):
            out["settlement"] = {
                "tx": receipt.get("transaction"),
                "payer": receipt.get("payer"),
                "network": receipt.get("network"),
                "rail": receipt.get("rail"),
            }
        return out

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-PAYMENT, X-PAYMENT-SIGNATURE, "
            "PAYMENT-SIGNATURE, PAYMENT-REQUIRED, X-PE-DEMO",
        )
        self.end_headers()

    def do_GET(self):
        if not _rate_allow(_client_ip(self)):
            self._send_json(429, {"error": "rate_limited", "window_s": RATE_LIMIT_WINDOW_S}, path="/rate")
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in ("/healthz", "/health"):
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "pe-x402-gateway",
                    "payment": "payai_base_usdc",
                    "facilitator": PAYAI_URL,
                    "payTo": os.environ.get("PE_X402_PAY_TO", PAY_TO_DEFAULT),
                    "asset": USDC_BASE,
                    "network": "base",
                    "public_base": PUBLIC_BASE,
                    "features": [
                        "citation-check",
                        "opportunity-scan",
                        "diff-review",
                        "bounty-triage",
                        "ve-estimate",
                        "pay-path-filter",
                        "batch-pay-path",
                        "portfolio-status",
                        "pr-watch",
                        "agent-card",
                        "metrics",
                        "payai-settle",
                    ],
                    "version": VERSION,
                },
                path=path,
            )
            return
        if path in ("/metrics", "/v1/metrics"):
            self._send_json(200, _metrics_snapshot(), path="/metrics")
            return
        if path in ("/.well-known/agent-card.json", "/agent-card.json"):
            _meter(path, 200)
            self._send(200, CARD.encode())
            return
        if path == "/v1/citation-check":
            if not self._gate_paid(path):
                return
            qs = parse_qs(parsed.query)
            claim = (qs.get("claim") or [""])[0]
            source_text = (qs.get("source") or [""])[0][:MAX_DEMO_SOURCE_CHARS]
            result = score_claim(
                claim[:MAX_DEMO_CLAIM],
                [{"title": "query_source", "text": source_text}] if source_text else [],
            )
            result.update(self._paid_mode_fields())
            self._send_json(
                200,
                result,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/opportunity-scan":
            if not self._gate_paid(path):
                return
            payload = _opportunity_scan_payload()
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/bounty-triage":
            if not self._gate_paid(path):
                return
            qs = parse_qs(parsed.query)
            limit = (qs.get("limit") or ["10"])[0]
            payload = _bounty_triage_payload({"limit": limit})
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/ve-estimate":
            if not self._gate_paid(path):
                return
            qs = parse_qs(parsed.query)
            data = {
                "reward_usd": (qs.get("reward_usd") or qs.get("reward") or ["0"])[0],
                "hours": (qs.get("hours") or ["2"])[0],
                "competitors": (qs.get("competitors") or ["0"])[0],
                "pay_type": (qs.get("pay_type") or ["unknown"])[0],
                "hourly_cost_usd": (qs.get("hourly_cost_usd") or ["25"])[0],
            }
            if qs.get("p_pay_central"):
                data["p_pay_central"] = qs["p_pay_central"][0]
            payload = estimate_ve(data)
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/pay-path-filter":
            if not self._gate_paid(path):
                return
            qs = parse_qs(parsed.query)
            data = {
                "title": (qs.get("title") or [""])[0][:500],
                "body": (qs.get("body") or qs.get("text") or [""])[0][:8000],
                "repo": (qs.get("repo") or [""])[0][:200],
                "comments": (qs.get("comments") or ["0"])[0],
                "labels": (qs.get("labels") or [""])[0],
            }
            payload = classify_pay_path(data)
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/batch-pay-path":
            if not self._gate_paid(path):
                return
            payload = classify_pay_path_batch({"items": []})
            payload["note"] = "POST JSON {items:[...]} for bulk classify"
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/portfolio-status":
            if not self._gate_paid(path):
                return
            payload = _portfolio_status_payload()
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/pr-watch":
            if not self._gate_paid(path):
                return
            payload = _pr_watch_payload()
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/":
            index = ROOT / "public" / "index.html"
            if index.exists():
                _meter("/", 200)
                self._send(200, index.read_bytes(), "text/html; charset=utf-8")
                return
            self._send_json(200, {"service": "pe-x402-gateway", "health": "/healthz"}, path="/")
            return
        self._send_json(404, {"error": "not_found", "path": path}, path=path)

    def do_POST(self):
        if not _rate_allow(_client_ip(self)):
            self._send_json(429, {"error": "rate_limited", "window_s": RATE_LIMIT_WINDOW_S}, path="/rate")
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/v1/citation-check":
            if not self._gate_paid(path):
                return
            data = _read_json(self)
            claim = str(data.get("claim") or "")
            sources = data.get("sources") or []
            if not isinstance(sources, list):
                sources = []
            if len(claim) > MAX_DEMO_CLAIM:
                claim = claim[:MAX_DEMO_CLAIM]
            capped = []
            budget = MAX_DEMO_SOURCE_CHARS
            for s in sources[:8]:
                if not isinstance(s, dict):
                    continue
                text = str(s.get("text") or s.get("body") or s.get("content") or "")
                take = text[: max(0, budget)]
                budget -= len(take)
                capped.append({**s, "text": take})
                if budget <= 0:
                    break
            result = score_claim(claim, capped, min_support=float(data.get("min_support") or 0.35))
            result.update(self._paid_mode_fields())
            self._send_json(
                200,
                result,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/opportunity-scan":
            if not self._gate_paid(path):
                return
            payload = _opportunity_scan_payload()
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/diff-review":
            if not self._gate_paid(path):
                return
            data = _read_json(self)
            payload = _diff_review_payload(data)
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/bounty-triage":
            if not self._gate_paid(path):
                return
            data = _read_json(self)
            payload = _bounty_triage_payload(data)
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/ve-estimate":
            if not self._gate_paid(path):
                return
            data = _read_json(self)
            payload = estimate_ve(data if isinstance(data, dict) else {})
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/pay-path-filter":
            if not self._gate_paid(path):
                return
            data = _read_json(self)
            payload = classify_pay_path(data if isinstance(data, dict) else {})
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/batch-pay-path":
            if not self._gate_paid(path):
                return
            data = _read_json(self)
            payload = classify_pay_path_batch(data if isinstance(data, dict) else {})
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/portfolio-status":
            if not self._gate_paid(path):
                return
            payload = _portfolio_status_payload()
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        if path == "/v1/pr-watch":
            if not self._gate_paid(path):
                return
            payload = _pr_watch_payload()
            payload.update(self._paid_mode_fields())
            self._send_json(
                200,
                payload,
                path=path,
                demo=self._is_demo(),
                settled=getattr(_tls, "mode", None) == "settled",
            )
            return
        self._send_json(404, {"error": "not_found", "path": path}, path=path)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"pe-x402-gateway {VERSION} on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
