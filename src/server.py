#!/usr/bin/env python3
"""Minimal PE agent gateway — health + agent-card + citation MVP + x402-shaped 402.

v0.3: in-process rate limit, request metering, honest payment receipt stub,
metrics endpoint (free, no secrets).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from citation import score_claim  # noqa: E402

CARD = (ROOT / ".well-known" / "agent-card.json").read_text()
HOST = os.environ.get("PE_X402_HOST", "0.0.0.0")
PORT = int(os.environ.get("PE_X402_PORT", "8791"))
VERSION = "0.3.0"

# Placeholder prices (USD) until facilitator live
PRICES = {
    "/v1/citation-check": 0.02,
    "/v1/opportunity-scan": 0.01,
    "/v1/diff-review": 0.03,
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
_started = time.time()
_rate: dict[str, deque] = defaultdict(deque)


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


def _meter(path: str, code: int, *, demo: bool = False, paid_stub: bool = False) -> None:
    with _lock:
        _hits[path] += 1
        _status[str(code)] += 1
        global _demo_hits, _paid_stub_hits
        if demo:
            _demo_hits += 1
        if paid_stub:
            _paid_stub_hits += 1


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
            "payment": "stub_with_demo",
            "rate_limit": {"window_s": RATE_LIMIT_WINDOW_S, "max": RATE_LIMIT_MAX},
        }


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

    def _send_json(self, code: int, obj: dict, *, path: str = "", demo: bool = False, paid_stub: bool = False):
        if path:
            _meter(path, code, demo=demo, paid_stub=paid_stub)
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _is_demo(self) -> bool:
        return (self.headers.get(DEMO_HEADER) or "").strip() in ("1", "true", "yes")

    def _has_valid_payment(self) -> bool:
        """Honest stub: X-PAYMENT present is NOT treated as settled.

        Facilitator verification is not wired. Never grant paid access from a
        client-supplied header alone.
        """
        return False

    def _payment_required(self, path: str):
        challenge = {
            "x402Version": 1,
            "error": "Payment required",
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "base",
                    "maxAmountRequired": str(int(PRICES[path] * 1_000_000)),
                    "asset": "USDC",
                    "payTo": "0x4945092C6586F078E0eD2130a53b0CDEe90c6796",
                    "resource": path,
                    "description": (
                        f"PE paid route {path} (facilitator not wired — "
                        f"use header {DEMO_HEADER}: 1 for free demo)"
                    ),
                }
            ],
            "demo": {
                "header": f"{DEMO_HEADER}: 1",
                "note": "Demo is free, rate/size limited; not a paid settlement",
            },
            "receipt": {
                "status": "unverified",
                "note": "X-PAYMENT headers are ignored until facilitator verify is live",
            },
        }
        body = json.dumps(challenge).encode()
        _meter(path, 402, paid_stub=True)
        self.send_response(402)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-PAYMENT-REQUIRED", "true")
        self.send_header("X-PE-Version", VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _gate_paid(self, path: str) -> bool:
        """Return True if request may proceed (demo only until facilitator)."""
        if self._is_demo():
            return True
        if self._has_valid_payment():
            return True
        self._payment_required(path)
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-PAYMENT, X-PE-DEMO",
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
                    "payment": "stub",
                    "features": [
                        "citation-check",
                        "opportunity-scan",
                        "diff-review",
                        "agent-card",
                        "metrics",
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
            result["mode"] = "demo"
            self._send_json(200, result, path=path, demo=True)
            return
        if path == "/v1/opportunity-scan":
            if not self._gate_paid(path):
                return
            payload = _opportunity_scan_payload()
            payload["mode"] = "demo"
            self._send_json(200, payload, path=path, demo=True)
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
            result["mode"] = "demo"
            self._send_json(200, result, path=path, demo=True)
            return
        if path == "/v1/opportunity-scan":
            if not self._gate_paid(path):
                return
            payload = _opportunity_scan_payload()
            payload["mode"] = "demo"
            self._send_json(200, payload, path=path, demo=True)
            return
        if path == "/v1/diff-review":
            if not self._gate_paid(path):
                return
            data = _read_json(self)
            payload = _diff_review_payload(data)
            self._send_json(200, payload, path=path, demo=True)
            return
        self._send_json(404, {"error": "not_found", "path": path}, path=path)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"pe-x402-gateway {VERSION} on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
