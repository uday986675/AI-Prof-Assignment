# README5 — Phase 5 Walkthrough (AI Agent)

Every command below was executed live during Phase 5 delivery (Git Bash on
Windows, `curl`). Phase 5 adds a conversational agent that searches REAL
availability, books through the controlled capability path, and reports the
EHR outcome honestly. Architecture: `docs/ai-agent.md`.

## 0. Prerequisites

- Phases 1–4 in place; Python 3.10+; deps from `requirements.txt`.
- Optional (full NLU): `GROQ_API_KEY` / `GEMINI_API_KEY` in `.env`.
  **Without any key the agent still works** — it uses the deterministic
  fallback interpreter (keyword understanding, same conversation flow).
- LLM model override (defaults): `GROQ_MODEL=llama-3.3-70b-versatile`,
  `GEMINI_MODEL=gemini-2.0-flash`.

## 1. Seed + start both services

```bash
python -m backend.scripts.seed_demo          # idempotent
python -m backend.scripts.seed_demo | grep Mehta   # (see note)

# Terminal 1 — Mock EHR on 8001 (must match MOCK_EHR_URL in .env)
python -m uvicorn mock_ehr.app.main:app --host 127.0.0.1 --port 8001

# Terminal 2 — Platform API on 8002
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002
```

> **Restart rule:** if a platform/uvicorn process was started before a code
> change, restart it — Python caches modules at import.
>
> **Env-var gotcha (Git Bash/MSYS):** when launching the EHR with fault vars
> for Phase 4 demos, values like `/ehr/appointments` must be protected with
> `MSYS_NO_PATHCONV=1` or MSYS rewrites them into Windows paths.
> For this walkthrough keep `MOCK_EHR_FAULT_MODE=none` (the default).

```bash
curl -s http://127.0.0.1:8001/health   # mock_ehr ok
curl -s http://127.0.0.1:8002/health   # platform-api ok
```

## 2. Log in as the patient

```bash
API=http://127.0.0.1:8002
TOK=$(curl -s -X POST $API/auth/login -H "Content-Type: application/json" \
  -d '{"email":"patient@demo.health","password":"Demo1234!"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo $TOK | head -c 25; echo " ..."
```

## 3. Start a session and talk to the agent

```bash
CONV=$(curl -s -X POST $API/agent/sessions -H "Authorization: Bearer $TOK" \
  | python -c "import sys,json; print(json.load(sys.stdin)['conversation_id'])")
echo "conversation: $CONV"

# Turn 1 — the PRD utterance
curl -s -X POST $API/agent/sessions/$CONV/messages \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"message":"I need to see an orthopedic doctor for my shoulder pain sometime this week."}' \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d['reply']); print(d['meta'])"
# → "Got it — a orthopedics doctor and this week. Would you prefer an in-person
#    visit or a video consultation?"   meta: {'next_action': 'collect_details'}

# Turn 2 — visit type → REAL availability from the Phase 2 slot engine
curl -s -X POST $API/agent/sessions/$CONV/messages \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"message":"in person please"}' \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d['reply']); print(d['meta'])"
# → numbered list of Dr. Rao's actual open slots this week
#   meta: {'next_action': 'present_availability'}
```

> The seed adds a Monday 09:00–09:30 window for Dr. Mehta and Dr. Rao's
> Mon–Fri 09:00–13:00/14:00–17:00 windows, so the demo always has near-term
> slots. Blocked/leave periods and booked slots are already excluded.

## 4. Book a slot (turn 3) — booking + EHR sync + honest outcome

```bash
BOOK=$(curl -s -X POST $API/agent/sessions/$CONV/messages \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"message":"1"}')
echo "$BOOK" | python -c "import sys,json; d=json.load(sys.stdin); print(d['reply']); print(d['meta'])"
APPT=$(echo "$BOOK" | python -c "import sys,json; print(json.load(sys.stdin)['meta']['appointment_id'])")
```

Expected reply (happy path, EHR up):

```
Booked! Dr. Rao (orthopedics) on 2026-09-18 09:00. It's confirmed and
synchronized with the hospital EHR.
meta: {'next_action': 'booked', 'appointment_id': '…', 'ehr_sync_status': 'synced'}
```

If the EHR is down/timeouts, the reply is deliberately honest —
`"…I've sent it to the hospital system and I'm confirming the outcome — I'll
verify before calling it final."` with `ehr_sync_status: unknown` — and
recovery is the existing Phase 4 flow (below). The agent NEVER claims an
unverified confirmation.

## 5. Verify persistence, EHR record, and recovery

```bash
# Persisted conversation (checkpoint + turn log)
curl -s $API/agent/sessions/$CONV -H "Authorization: Bearer $TOK" \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d['status'], len(d['events']), 'events'); print(d['state']['specialty'])"

# The appointment exists and is EHR-verified (platform side)
curl -s $API/appointments/$APPT/ehr-status -H "Authorization: Bearer $TOK"

# Phase 4 recovery endpoint (idempotent for a synced appointment)
curl -s -X POST $API/appointments/$APPT/reconcile-ehr -H "Authorization: Bearer $TOK"

# Direct EHR read by platform reference
EHR_KEY=$(grep -E '^MOCK_EHR_API_KEY=' .env | cut -d= -f2 | tr -d '\r\"'"'"' ')
curl -s http://127.0.0.1:8001/ehr/appointments/by-platform-ref/$APPT -H "X-API-Key: $EHR_KEY"
```

## 6. RBAC / isolation (expected status codes)

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/agent/sessions                      # 401 (no token)
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/agent/sessions \
  -H "Authorization: Bearer $(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
      -d '{"email":"dr.rao@citygeneral.health","password":"Demo1234!"}' \
      | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")"        # 403 (doctor)
curl -s -o /dev/null -w "%{http_code}\n" $API/agent/sessions/$CONV \
  -H "Authorization: Bearer <OTHER-PATIENT-TOKEN>"                                        # 404 (hidden)
```

## 7. Tests

```bash
python -m unittest discover -s tests -v
# Phase 5 adds 28 tests; expected total: 134 tests, OK
```

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/agent/*` returns 404 on :8002 | the uvicorn process predates Phase 5 — restart it |
| Agent replies are keyword-ish, no rich NLU | no `GROQ_API_KEY`/`GEMINI_API_KEY` in `.env` (fallback mode) — this is by design |
| `LLM` warnings in the log | provider outage/rate limit; the turn still completes via fallback |
| Booking says `unknown` | EHR unreachable or fault mode on; see README4 for recovery, or set `MOCK_EHR_FAULT_MODE=none` and restart the EHR |
| `409 slot_already_booked` inside chat | the slot was taken meanwhile; the agent re-presents fresh availability automatically |
| Empty availability | demo data blocked/leave/full; run the seed script, pick another week utterance ("next week" → say "this week" after clearing blocks) |

## 9. Reset

```bash
# stop both services, then delete data/ and re-seed:
rm -rf data
python -m backend.scripts.seed_demo
```
