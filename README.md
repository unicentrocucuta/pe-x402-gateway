# PE x402 / A2A gateway (line A1 + B3)

**Public:** https://x402.lagaceta.net  
**Agent card:** https://x402.lagaceta.net/.well-known/agent-card.json  
**Origin bind:** `127.0.0.1:8791` (not public).

Goal: zero-human paid agent endpoints on USDC rails (x402-style), discoverable via agent card.

## Status (2026-08-04T19:40Z)

**v0.3.5** on VPS port **8791** (`pe-x402-gateway.service` or manual process).

| Route | Behavior |
|-------|----------|
| `GET /healthz` | free |
| `GET /metrics` | free counters (no secrets) |
| `GET /.well-known/agent-card.json` | free discovery |
| `GET /` | static index |
| `POST /v1/citation-check` | **402** unless `X-PE-DEMO: 1` |
| `GET/POST /v1/opportunity-scan` | **402** unless demo header |
| `POST /v1/diff-review` | **402** unless demo header |
| `GET/POST /v1/bounty-triage` | **402** unless demo header — ranks local hunter/watchdog snapshots |
| `GET/POST /v1/ve-estimate` | **402** unless demo header — cons/central/opt net VE ranges |
| `GET/POST /v1/pay-path-filter` | **402** unless demo header — classify real pay path vs noise |
| `POST /v1/batch-pay-path` | **402** unless demo — bulk classify ≤25 items + pursue/consider shortlists |
| `GET/POST /v1/portfolio-status` | **402** unless demo — local RUN_STATE lines + realized net |
| `GET/POST /v1/pr-watch` | **402** unless demo — tracked PR open/mergeable/attention from local hunter |

Citation MVP: deterministic lexical overlap scorer (`src/citation.py`). Client supplies source texts — **no silent web fetch**. Tests: `python3 -m unittest discover -s tests -v`.

v0.3.5 adds: `/v1/pr-watch` + MRG/AIPOU illiquid markers in pay-path filter.
v0.3.4 adds: `/v1/batch-pay-path`, `/v1/portfolio-status`.
v0.3.3 adds: `/v1/pay-path-filter` (skip unfunded Opire footers, contests, illiquid tokens; pursue algora/titled $).
v0.3.2 adds: `/v1/ve-estimate` (honest ranges; contests/token forced low P(pay)).
v0.3.1 adds: `/v1/bounty-triage` (skip farms/contests/illiquid markers; never invents $).
v0.3 adds: sliding-window rate limit, in-process metering, honest receipt stub (never trusts bare `X-PAYMENT`), `/v1/diff-review`.

## Pay path

- Per-call USDC on **Base** once facilitator wired.
- Receive wallet: `0x4945092C6586F078E0eD2130a53b0CDEe90c6796`
- Until facilitator: demo header is free (size-capped); 402 body documents real challenge shape.
- **Honesty:** client-supplied payment headers do **not** unlock paid routes.

## Run local

```bash
python3 src/server.py
# GET http://127.0.0.1:8791/healthz
curl -sS -H 'X-PE-DEMO: 1' -H 'Content-Type: application/json' \
  -d '{"claim":"USDC settles fast on Base","sources":[{"text":"USDC on Base settles in seconds"}]}' \
  http://127.0.0.1:8791/v1/citation-check
curl -sS -H 'X-PE-DEMO: 1' http://127.0.0.1:8791/v1/pr-watch
curl -sS http://127.0.0.1:8791/metrics
```

## Next (pay rail)

1. Wire free-tier x402 facilitator / CDP if available without cash spend
2. Publish public URL + agent-card in registries when HTTPS front exists
3. Meter demo → paid once verify path exists
