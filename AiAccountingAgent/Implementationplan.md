# Tripletex AI Accounting Agent — Implementation Plan v2

## The Assignment In One Paragraph

We expose a `POST /solve` HTTPS endpoint. The contest sends us a fresh Tripletex sandbox account plus a task prompt in one of 7 languages. We use Gemini to interpret the prompt, call the Tripletex v2 REST API through the provided proxy, and return `{"status": "completed"}` within 300 seconds. We are scored field-by-field on correctness, multiplied by task tier (1×, 2×, 3×), then up to doubled by an efficiency bonus if we achieve perfect correctness with minimal 4xx errors and fewest API calls. Best score per task is kept. Max possible: 6.0 per task across 30 task types.

**The contest is live. Every day without a deployed endpoint is a missed score.**

---

## Scoring Math — Why Efficiency Matters

| Scenario | Tier 2 score |
|---|---|
| Failed all checks | 0.0 |
| 80% checks passed | 1.6 |
| Perfect, many errors/extra calls | ~2.1 |
| Perfect, efficient, few errors | ~2.6 |
| Perfect, best-in-class, zero errors | **4.0** |

For Tier 3: same math but ×3 ceiling → up to **6.0**.

The efficiency bonus only kicks in on perfect runs. So:
- **Phase 1 goal**: correctness at any cost
- **Phase 2 goal**: zero 4xx errors + minimal GET calls

Efficiency benchmarks are recalculated every 12 hours against the best known solution. Being first to a clean solution locks in a strong score before others catch up.

---

## Core Strategic Decision: Agentic Tool-Calling Loop

The existing plan described a pipeline: extract intent → deterministic handler. That requires writing a separate handler for each of 30 task types across 7 languages. Fragile and slow to build.

**Better approach: Gemini native function/tool calling.**

Define Tripletex API operations as Gemini tools. Let the model call them directly in an agentic loop. The model sees the prompt + tool results and decides the next call. This:

- handles all 30 task types with one agent definition
- handles all 7 languages (Gemini 2.5 Pro is multilingual)
- handles multi-step workflows naturally (model sees intermediate results)
- handles unknown/novel task types gracefully
- reduces code to maintain from ~30 handlers to ~15 tool wrappers

The loop looks like:

```
prompt + file content
  → Gemini (with Tripletex tools available)
  → model calls tool: POST /employee
  → we execute the call, return result to model
  → model calls tool: GET /employee to verify
  → model returns: "task complete"
  → we return {"status": "completed"}
```

The model decides what to call, in what order, with what parameters. We just execute faithfully and feed back results.

---

## Tech Stack

| Component | Choice | Why |
|---|---|---|
| Runtime | Python 3.13 | Already set up via uv |
| Web framework | FastAPI + uvicorn | Fast, async, typed |
| Package manager | uv | Already in pyproject.toml |
| AI model | `gemini-2.5-pro` via `google-genai` SDK | Strongest reasoning, multilingual, native function calling |
| Fallback model | `gemini-2.5-flash` | For simple Tier 1 tasks to save latency |
| Deployment | Cloud Run | HTTPS, 300s timeout, no cold-start worries with min-instances=1 |
| Auth | Vertex AI + ADC on Cloud Run; API key locally | Best security posture in prod |
| Container registry | Artifact Registry | Standard GCP |
| Secrets | Secret Manager | No keys in code |
| Logs | Cloud Logging (structured JSON) | Filterable by task type, error code |
| PDF extraction | `pypdf` first, Gemini multimodal fallback | Simple wins first |

**No credit limits** — use `gemini-2.5-pro` aggressively. Do not optimize for token cost.

---

## Project Structure

```
AiAccountingAgent/
  app/
    main.py              # FastAPI app, /solve endpoint, /health
    config.py            # env vars, model names, timeouts
    schemas.py           # SolveRequest, SolveResponse Pydantic models
    agent.py             # main agentic loop (calls Gemini with tools)
    tools.py             # Tripletex tool definitions + executor dispatch
    tripletex/
      client.py          # authenticated httpx client, all API methods
      errors.py          # error parsing, retry logic
    files.py             # base64 decode, PDF text extract, multimodal fallback
    logging_setup.py     # structured JSON logging for Cloud Logging
  Dockerfile
  pyproject.toml
  documentation.md       # failure journal (keep this updated!)
  .docs/                 # challenge docs (do not modify)
```

This is intentionally lean. No separate planner/verifier/orchestration modules until the agent loop proves insufficient.

---

## Gemini Tool Definitions

Define these as Gemini function declarations. The model calls them; we execute them.

```python
TRIPLETEX_TOOLS = [
    # Employees
    "tripletex_list_employees",      # GET /employee
    "tripletex_create_employee",     # POST /employee
    "tripletex_update_employee",     # PUT /employee/{id}
    "tripletex_get_employee",        # GET /employee/{id}

    # Customers
    "tripletex_list_customers",      # GET /customer
    "tripletex_create_customer",     # POST /customer
    "tripletex_update_customer",     # PUT /customer/{id}

    # Products
    "tripletex_list_products",       # GET /product
    "tripletex_create_product",      # POST /product

    # Orders
    "tripletex_create_order",        # POST /order
    "tripletex_get_order",           # GET /order/{id}

    # Invoices
    "tripletex_create_invoice",      # POST /invoice
    "tripletex_list_invoices",       # GET /invoice
    "tripletex_send_invoice",        # PUT /invoice/{id}/:send

    # Payments
    "tripletex_register_payment",    # POST /invoice/{id}/:payment

    # Travel expenses
    "tripletex_list_travel_expenses",    # GET /travelExpense
    "tripletex_create_travel_expense",   # POST /travelExpense
    "tripletex_delete_travel_expense",   # DELETE /travelExpense/{id}

    # Projects
    "tripletex_create_project",      # POST /project
    "tripletex_list_projects",       # GET /project

    # Departments
    "tripletex_create_department",   # POST /department
    "tripletex_list_departments",    # GET /department

    # Ledger (Tier 3)
    "tripletex_list_accounts",       # GET /ledger/account
    "tripletex_list_vouchers",       # GET /ledger/voucher
    "tripletex_create_voucher",      # POST /ledger/voucher
    "tripletex_list_postings",       # GET /ledger/posting
]
```

Each tool has a full JSON schema for its parameters. The model fills in the schema; we call the API.

### System prompt for the agent

```
You are an expert accounting agent for the Tripletex accounting system.
You receive a task in natural language (one of: Norwegian, English, Spanish,
Portuguese, Nynorsk, German, French). You must complete the task by calling
the available Tripletex API tools.

Rules:
- Call only the tools needed to complete the task. No extra exploratory calls.
- Reuse IDs from previous tool responses instead of re-fetching.
- When creating entities that depend on others (e.g. invoice needs customer+order),
  create prerequisites first.
- If a POST returns the created object, use its ID directly — do not GET it again.
- After completing the task, stop calling tools. Do not verify unless uncertain.
- Every 4xx error counts against your score. Validate parameters before calling.
- The sandbox starts empty — do not assume any entities exist.
```

---

## Agentic Loop Implementation

```python
async def run_agent(prompt: str, files: list, base_url: str, session_token: str) -> None:
    file_content = extract_files(files)
    full_prompt = build_prompt(prompt, file_content)

    messages = [{"role": "user", "content": full_prompt}]
    client = TripletexClient(base_url, session_token)

    for _ in range(MAX_ITERATIONS):  # hard cap at ~20 iterations
        response = await gemini.generate(
            messages=messages,
            tools=TRIPLETEX_TOOLS,
            model="gemini-2.5-pro",
        )

        if response.has_tool_calls:
            for call in response.tool_calls:
                result = await execute_tool(client, call.name, call.args)
                messages.append(tool_result_message(call.id, result))
        else:
            # model is done
            break
```

This is the entire orchestration. No planner class, no handler registry, no explicit routing.

---

## Tripletex Client Design

Single `httpx.AsyncClient` (or `requests.Session`) per solve request.

Key decisions:
- **Always use `base_url` from the request** — never hardcode Tripletex URLs
- **Basic Auth**: `("0", session_token)`
- **Log every request**: method, path, status code, duration
- **Parse errors**: extract `validationMessages` from 422 responses and return them to the model — it can self-correct
- **No retry on 4xx** — retry wastes score; let the model fix the call instead
- **Retry on 5xx**: 1 retry with 1s delay
- **Rate limit handling**: check `X-Rate-Limit-Remaining`, back off if near 0

```python
class TripletexClient:
    def __init__(self, base_url: str, session_token: str):
        self.base_url = base_url.rstrip("/")
        self.auth = ("0", session_token)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.call_log = []  # for structured logging

    def get(self, path: str, params: dict = None) -> dict: ...
    def post(self, path: str, body: dict) -> dict: ...
    def put(self, path: str, body: dict) -> dict: ...
    def delete(self, path: str) -> None: ...
```

When a tool call fails with 422, return the full `validationMessages` to the model. Gemini can read the error and retry with corrected parameters — this is one retry that actually works.

---

## File Handling

```python
def extract_files(files: list) -> str:
    results = []
    for f in files:
        data = base64.b64decode(f["content_base64"])
        if f["mime_type"] == "application/pdf":
            text = extract_pdf_text(data)  # pypdf
            if len(text.strip()) < 50:
                text = extract_via_gemini_multimodal(data, f["filename"])
        elif f["mime_type"].startswith("image/"):
            text = extract_via_gemini_multimodal(data, f["filename"])
        else:
            text = data.decode("utf-8", errors="replace")
        results.append(f"[File: {f['filename']}]\n{text}")
    return "\n\n".join(results)
```

File content is injected into the agent's initial user message. No separate file layer needed.

---

## FastAPI Endpoint

```python
@app.post("/solve")
async def solve(request: SolveRequest):
    with time_budget(seconds=280):  # 20s buffer before the 300s hard limit
        await run_agent(
            prompt=request.prompt,
            files=request.files,
            base_url=request.tripletex_credentials.base_url,
            session_token=request.tripletex_credentials.session_token,
        )
    return {"status": "completed"}

@app.get("/health")
async def health():
    return {"status": "ok"}
```

The endpoint always returns `{"status": "completed"}` with HTTP 200 unless it times out (which returns 504 from Cloud Run). A graceful timeout handler should still return 200 if the task was mostly done.

---

## Deployment: Cloud Run

### Enable required APIs (one-time)
```bash
gcloud services enable run.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com
```

### Build and deploy
```bash
# Build container
gcloud builds submit --tag gcr.io/$PROJECT_ID/accounting-agent .

# Deploy to Cloud Run
gcloud run deploy accounting-agent \
  --image gcr.io/$PROJECT_ID/accounting-agent \
  --platform managed \
  --region europe-west1 \
  --allow-unauthenticated \
  --timeout 300 \
  --concurrency 1 \
  --min-instances 1 \
  --max-instances 10 \
  --memory 2Gi \
  --cpu 2 \
  --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT_ID \
  --set-env-vars VERTEX_AI_LOCATION=europe-west1
```

### Service account permissions
```bash
# Grant Vertex AI access to the Cloud Run service account
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SERVICE_ACCOUNT" \
  --role="roles/aiplatform.user"
```

**concurrency=1** means each instance handles one request at a time. With max-instances=10 and 3 concurrent submissions allowed, we have plenty of headroom.

**min-instances=1** eliminates cold starts during active competition.

---

## Dockerfile

```dockerfile
FROM python:3.13-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml .
RUN uv pip install --system -e .

COPY app/ ./app/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

Cloud Run expects port 8080 by default.

---

## Dependencies (pyproject.toml additions)

```toml
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "httpx>=0.27",
  "requests>=2.32",
  "pydantic>=2.7",
  "google-genai>=1.0",          # Google Gen AI SDK (Gemini + Vertex AI)
  "pypdf>=4.0",                  # PDF text extraction
  "python-multipart>=0.0.9",
]
```

The `google-genai` SDK supports both API-key mode (local dev) and Vertex AI mode (prod) through the same interface.

---

## Model Configuration

```python
# Local development
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Production (Cloud Run with ADC)
client = genai.Client(
    vertexai=True,
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location="europe-west1",
)
```

Switch is controlled by an env var: `USE_VERTEX_AI=true`.

**Use `gemini-2.5-pro` as default.** Switch to `gemini-2.5-flash` only if Tier 1 tasks consistently finish in 5 seconds and we want lower latency.

For Tier 3 tasks (complex multi-step), consider enabling extended thinking/reasoning in Gemini 2.5 Pro if the task is detected as high-complexity. This is worth it given no credit limits.

---

## Phased Shipping Plan

The contest is live. Phases are measured in hours, not weeks.

### Phase 0 — Working endpoint (target: today)

- [ ] Convert `main.py` to FastAPI with `/solve` and `/health`
- [ ] Add Pydantic schemas (`SolveRequest`, `SolveResponse`)
- [ ] Add `google-genai` to pyproject.toml
- [ ] Write a minimal agent loop: send prompt to Gemini, log response
- [ ] Return `{"status": "completed"}` regardless of what Gemini says
- [ ] Write Dockerfile, test locally with uvicorn
- [ ] Deploy to Cloud Run
- [ ] Submit endpoint URL to the contest platform

**At this point we are on the leaderboard with 0.0 score but the pipeline works.**

### Phase 1 — First correct task (target: next few hours)

- [ ] Implement `TripletexClient` with GET/POST/PUT/DELETE
- [ ] Define tool schemas for the top 6 tools: create/list employee, customer, invoice
- [ ] Implement `execute_tool` dispatcher
- [ ] Wire agent loop: Gemini calls tools → we execute → feed back results
- [ ] Test with the sandbox account locally using example prompts from the docs
- [ ] Deploy and submit — aim for first non-zero scores

**Target: score > 0 on at least "create employee" task.**

### Phase 2 — Tier 1 full coverage (day 2)

- [ ] Add all Tier 1 tool schemas (travel expense, project, department, customer, product)
- [ ] Add structured logging per request: prompt hash, tool calls made, status codes, elapsed
- [ ] Handle 422 errors: return validation messages to model for self-correction
- [ ] Add PDF extraction (`pypdf`)
- [ ] Test across several prompt languages

**Target: consistent near-perfect scores on Tier 1 tasks.**

### Phase 3 — Efficiency optimization (day 2-3)

- [ ] Audit logs for redundant GET calls (most common waste)
- [ ] Tighten system prompt: "reuse IDs from responses, do not re-fetch"
- [ ] Add `?fields=id,name` to all list queries (narrow response)
- [ ] Review for unnecessary verification GETs after POST
- [ ] Log API call count per solve, compare to expected minimum

**Target: efficiency bonus on Tier 1 tasks → scores climbing toward 2.0.**

### Phase 4 — Tier 2 tasks (day 3)

- [ ] Add full tool coverage for multi-step flows: order → invoice, invoice → payment, credit notes
- [ ] Test: create invoice with payment, reverse an invoice
- [ ] Add Gemini multimodal for image/bad-PDF attachments
- [ ] Improve system prompt with few-shot examples for multi-step patterns

**Target: Tier 2 scores > 0, growing toward 4.0.**

### Phase 5 — Tier 3 (day 4+, opens Saturday)

- [ ] Add ledger/voucher tools
- [ ] Enable Gemini extended thinking for complex prompts
- [ ] Handle CSV file parsing (bank reconciliation tasks)
- [ ] Add year-end closing workflow support
- [ ] Tune system prompt for correction/reversal tasks

**Target: any Tier 3 score > 0, improve toward 6.0.**

---

## Execution Principles

### 1. Plan before calling (system prompt enforces this)
The model reads the full prompt + files before making any API call. This prevents the "try and see" pattern that generates 4xx errors.

### 2. Reuse IDs from responses
After `POST /employee` returns `{"value": {"id": 42, ...}}`, the model must use `42` directly. No follow-up `GET /employee/42`. System prompt enforces this.

### 3. Return validation errors to the model (one retry)
If a 422 comes back with `validationMessages`, format them and send back as a tool result. The model can then call the corrected version. This is the only retry pattern. Never retry blind.

### 4. Hard cap on iterations
Max 20 tool calls per solve. If we hit the cap without finishing, return `{"status": "completed"}` anyway — partial credit is better than timeout.

### 5. Time budget awareness
Track elapsed time from request start. If > 240 seconds, stop the agent loop and return. Never let the 300s timeout kill us.

### 6. Document failures
Every failed run should get a note in `documentation.md`:
- What the prompt was
- What the agent did
- What failed (wrong endpoint, missing field, wrong order)
- What to fix

---

## Scoring Optimization Tricks

| Technique | Impact |
|---|---|
| Narrow `?fields=` on GET requests | Fewer tokens returned, faster |
| Use POST response ID directly | Eliminates 1 GET per created entity |
| Batch order line items | Some endpoints accept arrays |
| Skip verification GET after successful POST | Eliminates 1 call per entity |
| Tight system prompt ("no extra calls") | Prevents exploratory GETs |
| Return 422 error text to model | Enables single-retry self-correction |
| Use `?count=10` not `?count=100` | Faster for searches |

---

## What We Know About The 30 Task Types

From the docs, confirmed task families:

**Tier 1** (×1 multiplier):
- Create employee (with role — role assignment is worth 5/10 points!)
- Create customer
- Create product
- Create invoice
- Create project linked to customer
- Create department
- Enable module (e.g. department accounting)

**Tier 2** (×2 multiplier):
- Create invoice + register payment
- Create credit note / reverse invoice
- Create travel expense report
- Update existing entity (add phone, change address)
- Delete entity (travel expense, etc.)
- Create order → invoice flow

**Tier 3** (×3 multiplier, opens Saturday):
- Bank reconciliation from CSV
- Error correction in ledger
- Year-end closing
- Complex multi-entity workflows

**Critical insight on employee creation**: The role check is worth 5 out of 10 points. Getting the role wrong means 50% score even if name and email are correct. The system prompt must emphasize role extraction.

**Critical insight on invoice creation**: An invoice needs a customer AND an order. The order needs an order line with a product. Full chain: create customer → create product → create order (with order line) → create invoice.

---

## Failure Journal Protocol

Every time a submission scores below expected, add an entry to `documentation.md`:

```markdown
## YYYY-MM-DD — [task type] — Score: X.X / expected Y.Y

**Prompt excerpt**: ...
**What the agent did**: ...
**What failed**: ...
**Root cause**: ...
**Fix applied**: ...
**Never repeat**: ...
```

This file is the team's memory. Keep it updated. It prevents fixing the same bug twice.

---

## What Not To Do

- Do not write 30 separate handler modules before testing the agentic approach
- Do not use Compute Engine — Cloud Run handles everything we need
- Do not optimize for token cost — no credit limits, use Pro aggressively
- Do not use preview/unstable models as primary — stick with `gemini-2.5-pro`
- Do not add Document AI before evidence shows PDFs are failing at scale
- Do not retry 4xx blindly — return the error to the model instead
- Do not let the model GET an entity it just POSTed — use the response ID
- Do not add complexity before the simple approach fails

---

## Definition Of Done For Each Phase

**Phase 0**: Cloud Run URL is live, endpoint returns 200 with `{"status": "completed"}`, submitted to contest.

**Phase 1**: At least one task type returns a non-zero score on the leaderboard.

**Phase 2**: All Tier 1 tasks return > 0.8 correctness score consistently.

**Phase 3**: Efficiency bonus is appearing — Tier 1 scores approaching 2.0.

**Phase 4**: Tier 2 scores > 0, growing toward 4.0.

**Phase 5**: Tier 3 tasks covered, scores visible on leaderboard.

**Winning**: Total leaderboard score = sum of best scores across all 30 task types.
