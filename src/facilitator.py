#!/usr/bin/env python3
"""x402 facilitator client — PayAI primary, CDP optional backup.

No API key required for PayAI (https://facilitator.payai.network).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# Base mainnet USDC (Circle)
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO_DEFAULT = "0x4945092C6586F078E0eD2130a53b0CDEe90c6796"

# Network identifiers accepted by PayAI (v1 name + v2 CAIP-2)
NETWORK_V1 = "base"
NETWORK_V2 = "eip155:8453"
CHAIN_ID = 8453

PAYAI_URL = os.environ.get("PE_X402_FACILITATOR_URL", "https://facilitator.payai.network").rstrip("/")
CDP_URL = os.environ.get(
    "PE_X402_CDP_FACILITATOR_URL",
    "https://api.cdp.coinbase.com/platform/v2/x402",
).rstrip("/")


@dataclass
class FacilitatorResult:
    ok: bool
    stage: str  # verify | settle
    body: dict[str, Any]
    rail: str
    error: str | None = None
    tx: str | None = None
    payer: str | None = None


def _post_json(url: str, payload: dict, *, headers: dict | None = None, timeout: int = 45) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "pe-x402-gateway/0.4"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"raw": raw[:500]}
            return int(r.status), body if isinstance(body, dict) else {"data": body}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            body = json.loads(raw) if raw else {"error": str(e)}
        except json.JSONDecodeError:
            body = {"error": raw[:500] or str(e)}
        if not isinstance(body, dict):
            body = {"error": str(body)}
        return int(e.code), body
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)[:300]}


def cdp_headers() -> dict[str, str] | None:
    """Optional CDP JWT/API key auth if operator placed secrets."""
    # Prefer prebuilt bearer if present
    bearer = (os.environ.get("CDP_X402_BEARER") or os.environ.get("CDP_ACCESS_TOKEN") or "").strip()
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}
    key_id = (os.environ.get("CDP_API_KEY_ID") or "").strip()
    secret = (os.environ.get("CDP_API_KEY_SECRET") or "").strip()
    if key_id and secret:
        # Basic fallback; CDP often wants JWT — still try key header variants
        return {
            "Authorization": f"Bearer {secret}",
            "X-Api-Key": key_id,
            "Content-Type": "application/json",
        }
    return None


def payment_requirements(
    *,
    path: str,
    amount_atomic: str,
    resource_url: str,
    description: str,
    pay_to: str | None = None,
    version: int = 1,
) -> dict[str, Any]:
    """Build facilitator paymentRequirements object (v1 shape preferred by PayAI examples)."""
    pay_to = pay_to or os.environ.get("PE_X402_PAY_TO", PAY_TO_DEFAULT)
    if version >= 2:
        return {
            "scheme": "exact",
            "network": NETWORK_V2,
            "amount": str(amount_atomic),
            "asset": USDC_BASE,
            "payTo": pay_to,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USD Coin", "version": "2"},
            "resource": resource_url,
            "description": description,
            "mimeType": "application/json",
        }
    return {
        "scheme": "exact",
        "network": NETWORK_V1,
        "maxAmountRequired": str(amount_atomic),
        "resource": resource_url,
        "description": description,
        "mimeType": "application/json",
        "payTo": pay_to,
        "maxTimeoutSeconds": 60,
        "asset": USDC_BASE,
        "extra": {"name": "USD Coin", "version": "2"},
    }


def challenge_body(
    *,
    path: str,
    amount_atomic: str,
    public_base: str,
    description: str | None = None,
) -> dict[str, Any]:
    """HTTP 402 body + material for PAYMENT-REQUIRED header (v1, Base USDC)."""
    public_base = public_base.rstrip("/")
    resource_url = f"{public_base}{path}"
    desc = description or f"PE paid route {path}"
    accepts = [
        {
            "scheme": "exact",
            "network": NETWORK_V1,
            "maxAmountRequired": str(amount_atomic),
            "amount": str(amount_atomic),
            "asset": USDC_BASE,
            "payTo": os.environ.get("PE_X402_PAY_TO", PAY_TO_DEFAULT),
            "resource": resource_url,
            "description": desc,
            "mimeType": "application/json",
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USD Coin", "version": "2"},
        }
    ]
    return {
        "x402Version": 1,
        "error": "PAYMENT-SIGNATURE header is required",
        "accepts": accepts,
        "resource": {
            "url": resource_url,
            "description": desc,
            "mimeType": "application/json",
        },
        "facilitator": PAYAI_URL,
        "extensions": {},
    }


def _normalize_payload(payment_payload: dict) -> dict:
    """Ensure payment payload has scheme/network at top level for facilitator."""
    p = dict(payment_payload)
    if "x402Version" not in p:
        p["x402Version"] = 1
    # Some clients nest under accepted only
    acc = p.get("accepted") if isinstance(p.get("accepted"), dict) else {}
    if "scheme" not in p and acc.get("scheme"):
        p["scheme"] = acc["scheme"]
    if "network" not in p and acc.get("network"):
        p["network"] = acc["network"]
    # Map CAIP-2 base → v1 name if payload is mixed
    if p.get("network") == NETWORK_V2:
        # Keep as-is for v2 facilitators; PayAI accepts both
        pass
    return p


def verify_and_settle(
    payment_payload: dict,
    requirements: dict,
    *,
    prefer: str = "payai",
) -> FacilitatorResult:
    """Verify then settle. Mutates rail on failure: payai → cdp (if keys)."""
    payload = _normalize_payload(payment_payload)
    body = {"paymentPayload": payload, "paymentRequirements": requirements}

    rails: list[tuple[str, str, dict | None]] = []
    if prefer == "cdp":
        rails.append(("cdp", CDP_URL, cdp_headers()))
        rails.append(("payai", PAYAI_URL, None))
    else:
        rails.append(("payai", PAYAI_URL, None))
        rails.append(("cdp", CDP_URL, cdp_headers()))

    last: FacilitatorResult | None = None
    attempts: list[dict] = []

    for rail, base, hdrs in rails:
        if rail == "cdp" and not hdrs:
            attempts.append({"rail": "cdp", "skip": "no_keys"})
            continue

        # --- verify ---
        st, vbody = _post_json(f"{base}/verify", body, headers=hdrs)
        attempts.append({"rail": rail, "stage": "verify", "status": st, "body": _snip(vbody)})
        is_valid = bool(vbody.get("isValid") is True or vbody.get("valid") is True or vbody.get("success") is True)
        if st not in (200, 201) or not is_valid:
            # §6 mutation: try alternate field shapes once per rail
            alt = _alt_body(payload, requirements)
            if alt != body:
                st2, vbody2 = _post_json(f"{base}/verify", alt, headers=hdrs)
                attempts.append({"rail": rail, "stage": "verify_alt", "status": st2, "body": _snip(vbody2)})
                if st2 in (200, 201) and (
                    vbody2.get("isValid") is True or vbody2.get("valid") is True or vbody2.get("success") is True
                ):
                    body = alt
                    vbody = vbody2
                    is_valid = True
                else:
                    last = FacilitatorResult(
                        ok=False,
                        stage="verify",
                        body=vbody2 if st2 else vbody,
                        rail=rail,
                        error=str(
                            (vbody2 or vbody).get("invalidReason")
                            or (vbody2 or vbody).get("invalidMessage")
                            or (vbody2 or vbody).get("error")
                            or f"verify_http_{st2 or st}"
                        )[:240],
                        payer=_payer_of(vbody2 or vbody, payload),
                    )
                    continue
            else:
                last = FacilitatorResult(
                    ok=False,
                    stage="verify",
                    body=vbody,
                    rail=rail,
                    error=str(
                        vbody.get("invalidReason")
                        or vbody.get("invalidMessage")
                        or vbody.get("error")
                        or f"verify_http_{st}"
                    )[:240],
                    payer=_payer_of(vbody, payload),
                )
                continue

        # --- settle ---
        st_s, sbody = _post_json(f"{base}/settle", body, headers=hdrs)
        attempts.append({"rail": rail, "stage": "settle", "status": st_s, "body": _snip(sbody)})
        success = bool(sbody.get("success") is True or sbody.get("ok") is True)
        tx = sbody.get("transaction") or sbody.get("txHash") or sbody.get("tx")
        data_obj = sbody.get("data")
        if not tx and isinstance(data_obj, dict):
            tx = data_obj.get("transaction") or data_obj.get("txHash")
        if isinstance(tx, dict):
            tx = tx.get("hash") or tx.get("transactionHash")
        if st_s in (200, 201) and success and tx:
            return FacilitatorResult(
                ok=True,
                stage="settle",
                body={"verify": vbody, "settle": sbody, "attempts": attempts},
                rail=rail,
                tx=str(tx),
                payer=_payer_of(sbody, payload) or _payer_of(vbody, payload),
            )

        last = FacilitatorResult(
            ok=False,
            stage="settle",
            body={"verify": vbody, "settle": sbody, "attempts": attempts},
            rail=rail,
            error=str(sbody.get("errorReason") or sbody.get("error") or f"settle_http_{st_s}")[:240],
            tx=str(tx) if tx else None,
            payer=_payer_of(sbody, payload),
        )

    if last is None:
        last = FacilitatorResult(ok=False, stage="verify", body={"attempts": attempts}, rail="none", error="no_rail")
    else:
        last.body = {**(last.body if isinstance(last.body, dict) else {}), "attempts": attempts}
    return last


def _alt_body(payload: dict, requirements: dict) -> dict:
    """Single-shape mutation for facilitator quirks (v1 maxAmount vs amount)."""
    req = dict(requirements)
    # If maxAmountRequired present, also set amount and vice versa
    if "maxAmountRequired" in req and "amount" not in req:
        req["amount"] = req["maxAmountRequired"]
    elif "amount" in req and "maxAmountRequired" not in req:
        req["maxAmountRequired"] = req["amount"]
    # Toggle network base <-> eip155:8453 once
    net = req.get("network")
    if net == NETWORK_V1:
        req["network"] = NETWORK_V2
        p2 = dict(payload)
        p2["network"] = NETWORK_V2
        if isinstance(p2.get("accepted"), dict):
            acc = dict(p2["accepted"])
            acc["network"] = NETWORK_V2
            if "amount" not in acc and req.get("amount"):
                acc["amount"] = req["amount"]
            p2["accepted"] = acc
        return {"paymentPayload": p2, "paymentRequirements": req}
    if net == NETWORK_V2:
        req["network"] = NETWORK_V1
        p2 = dict(payload)
        p2["network"] = NETWORK_V1
        if isinstance(p2.get("accepted"), dict):
            acc = dict(p2["accepted"])
            acc["network"] = NETWORK_V1
            p2["accepted"] = acc
        return {"paymentPayload": p2, "paymentRequirements": req}
    return {"paymentPayload": payload, "paymentRequirements": req}


def _payer_of(body: dict, payload: dict) -> str | None:
    if not isinstance(body, dict):
        return None
    if body.get("payer"):
        return str(body["payer"])
    try:
        auth = (payload.get("payload") or {}).get("authorization") or {}
        if auth.get("from"):
            return str(auth["from"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _snip(body: dict, n: int = 400) -> dict:
    try:
        s = json.dumps(body, default=str)
        if len(s) <= n:
            return body
        return {"_snip": s[:n]}
    except Exception:  # noqa: BLE001
        return {"_err": "snip_fail"}
