# PE x402 / A2A gateway

**Public:** https://x402.lagaceta.net  
**Agent card:** https://x402.lagaceta.net/.well-known/agent-card.json  
**Origin bind:** `127.0.0.1:8791` (not public).

## Status (2026-08-05)

**v0.4.0** — PayAI facilitator **verify + settle** on **Base USDC** (no API key).

| Route | Behavior |
|-------|----------|
| `GET /healthz` | free — reports `payment=payai_base_usdc` |
| `GET /metrics` | free counters |
| `GET /.well-known/agent-card.json` | free discovery card |
| Paid `/v1/*` | **402** + `PAYMENT-REQUIRED` (base64) unless settled or `X-PE-DEMO: 1` |

### Payment flow
1. Client calls paid route → **402** with accepts (Base USDC, EIP-3009 exact).
2. Client signs `TransferWithAuthorization`, retries with **`PAYMENT-SIGNATURE`** (base64 payload).
3. Server → PayAI `POST /verify` then `POST /settle`.
4. On success → **200** + payload + `PAYMENT-RESPONSE` (tx hash). Logged to `deliverables/SETTLEMENTS.jsonl` and `PROFIT_LEDGER.csv`.

- Facilitator: `https://facilitator.payai.network`
- Asset: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (USDC Base)
- payTo: `0x4945092C6586F078E0eD2130a53b0CDEe90c6796`
- CDP facilitator: optional backup if keys present (`CDP_API_KEY_*`)

### Honesty
- Demo header is free (size-capped) and **not** settlement.
- Client headers alone never unlock; only facilitator verify+settle.
- **No discovery/Bazaar publish until first on-chain settlement completes.**

## E2E
```bash
# after funding buyer in secrets.env
/home/dany/.hermes/hermes-agent/venv/bin/python \
  /home/dany/projects/profit-engine/scripts/e2e_x402_pay.py
```

## Run
```bash
# systemd user unit pe-x402-gateway.service
curl -sS https://x402.lagaceta.net/healthz
curl -sS -X POST https://x402.lagaceta.net/v1/citation-check -H 'content-type: application/json' -d '{}'
```

Tests: `python3 -m unittest discover -s tests -v`
