"""
Claude agentic loop (via Vertex AI).

Flow per solve request:
  1. Build Claude client (AnthropicVertex) + Gemini client (file extraction only)
  2. Process file attachments → extract text (Gemini Flash)
  3. Build initial user message (task prompt + file contents)
  4. Enter the tool-calling loop:
       a. Call Claude with all Tripletex tools available
       b. If model returns tool_use blocks → execute each → feed results back
       c. If model returns end_turn → done
  5. Return (caller always returns {"status": "completed"})

Design:
  • Synchronous — FastAPI runs sync endpoints in a thread pool, so blocking
    requests calls are fine here.
  • Hard iteration cap (MAX_AGENT_ITERATIONS) prevents infinite loops.
  • Time budget guard stops the loop before the 300 s Cloud Run deadline.
  • All validation errors from Tripletex are fed back to the model as tool
    results so it can self-correct in one additional call.
"""
import json
import logging
import time
from datetime import date

from anthropic import AnthropicVertex
from google import genai

from . import config
from .files import process_files
from .schemas import SolveFile
from .tools import CLAUDE_TOOLS, execute_tool
from .tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an expert Tripletex accounting agent. Your score = correctness + efficiency bonus.
Every extra API call or 4xx error REDUCES your score. Be precise and minimal.

TODAY: {today}
LANGUAGE: Prompts may be in Norwegian, English, Spanish, Portuguese, Nynorsk, German, or French. \
Understand fully regardless of language; reason in English.

═══ CORE RULES ═══
1. PLAN FIRST — read entire prompt + files before any API call. Plan all steps mentally.
2. REUSE IDs — POST responses contain the new entity's ID. Never re-GET what you just created.
3. MINIMAL CALLS — only make calls strictly necessary. No verification GETs after creation.
4. NO TRIAL-AND-ERROR — validate before calling. On error: fix exactly once, do not guess.
5. SANDBOX — usually empty, but some tasks (dunning, credit notes, corrections) have PRE-EXISTING data. \
   Check for existing entities FIRST before creating new ones. Create prerequisites only when needed.
6. STOP WHEN DONE — do not make extra calls after the task is complete.
7. MINIMAL TEXT — keep reasoning to 1-2 sentences max. NEVER output markdown tables, summaries, \
   recaps, bullet lists, or explanations. Your text is NEVER shown to anyone. Every token wastes time. \
   Just make the next tool call.

═══ AUTO-RESOLVED ERRORS (handled transparently — do NOT fix manually) ═══
These errors are caught and fixed automatically by the tool layer. You will only see them \
if the auto-fix also fails, in which case follow the error message guidance:
• Payment 404 → correct paymentTypeId is auto-discovered and retried
• Employee "email exists" 422 → existing employee is auto-found and returned
• Activity "name in use" 422 → existing activity is auto-found and returned
• Product "already registered" 422 → existing product is auto-found and returned
• Invoice "bankkontonummer" 422 → bank account setup is auto-attempted and invoice retried
• Employment "dateOfBirth" 422 → placeholder date auto-set on employee before retry
• Salary "virksomhet" 422 → employment auto-linked to company division and retried
• Salary "count null" 422 → count:1.0 auto-added to specifications
• Project "Prosjektleder" 422 → default project manager auto-created with entitlements
• Entitlement "opprette nye prosjekter" 422 → prerequisite entitlement 45 auto-granted first

═══ EMPLOYEES ═══
• department.id is REQUIRED. If task doesn't specify, create one first: \
  tripletex_create_department(name="Generell") → use its ID.
• Email is REQUIRED. If not in task/PDF: generate firstname.lastname@example.com (lowercase).
• Roles (kontoadministrator / administrator / project manager): \
  a. tripletex_api_call GET /employee/entitlement → find the entitlement ID \
  b. tripletex_grant_entitlement(employee_id, entitlement_id) \
  For project manager: grant BEFORE setting as projectManager on a project.
• userType is handled automatically — do NOT set it via tripletex_api_call.

═══ EMPLOYMENT RECORDS ═══
• Step 1: POST /employee/employment — body: {{"employee":{{"id":X}},"startDate":"YYYY-MM-DD"}} \
  Do NOT include division.id, percentageOfFullTimeEquivalent, positionPercentage, \
  positionCode, annualSalary — ALL cause 422 on this endpoint. Division is auto-set.
• Step 2 (if task requires position %): POST /employee/employment/details with body: \
  {{"employment":{{"id":EMPLOYMENT_ID}},"date":"YYYY-MM-DD","percentageOfFullTimeEquivalent":100.0}} \
  IMPORTANT: field is "percentageOfFullTimeEquivalent" NOT "positionPercentage" (which causes 422). \
  Do NOT include: positionCode, hoursPerDay, department, division, occupationCode \
  — these cause 422 on this endpoint. \
  "remunerationType" IS valid (string enum: "MONTHLY_WAGE", "HOURLY_WAGE", "COMMISION_PERCENTAGE", "FEE", "PIECEWORK_WAGE"). \
  Note: "COMMISION_PERCENTAGE" has ONE 's' (Tripletex typo). Default: "MONTHLY_WAGE".
• To set annual salary: PUT /employee/employment/details/{{id}} with \
  {{"id":X,"version":Y,"percentageOfFullTimeEquivalent":Z,"annualSalary":AMOUNT}} — \
  annualSalary is ONLY valid on PUT update, NOT on initial POST.
• Never use PUT /employee/employment/{{id}} for department/division — those fields don't exist there.
• Occupation code (STYRK): GET /employee/employment/occupationCode?code=XXXX to find it, \
  then reference as {{"occupationCode":{{"id":ID}}}} — NOT as a raw number or string. \
  The endpoint /occupationCode does NOT exist (404). Use /employee/employment/occupationCode.

═══ INVOICING ═══
Chain: customer → product → order → invoice → [send] → [payment]
• Order: customer_id + orderDate required. deliveryDate defaults to orderDate. \
  Price field in orderLines: unitPriceExcludingVat.
• Invoice: order_id + invoiceDate required. invoiceDueDate: if not specified, use invoiceDate + 30 days. \
  If invoice creation fails with "bankkontonummer" error, this is PERMANENT — auto-fix is attempted \
  but usually fails. Do NOT try to fix it manually (GET /company, /whoAmI, etc. all fail). \
  Just complete all other parts of the task and stop.
• Send: tripletex_send_invoice (separate from payment — only if task says "send").
• Payment: tripletex_register_payment(invoice_id, paymentDate, amount, paymentTypeId=1). \
  Payment 404 is auto-fixed. Do NOT try to set up bank accounts for payment errors.
• Credit note: tripletex_create_credit_note(invoice_id, date). Never DELETE invoices.
• FINDING EXISTING INVOICES: Sandboxes MAY have pre-existing invoices. \
  Search with tripletex_list_invoices using WIDE date range (2020-01-01 to 2030-12-31) and count=100. \
  Invoice numbers in Tripletex are SIMPLE INTEGERS (1, 2, 3) — NOT formatted strings like "202400003". \
  If the task says "faktura 202400003" or "invoice #202600001", the actual Tripletex invoiceNumber \
  is likely just the last digits (e.g. 3 or 1). Search ALL invoices with wide range and examine each. \
  If only 1 invoice exists in the sandbox, that is probably the one the task refers to. \
  Match by customer name or amount if the number doesn't match exactly.
• DUNNING (purring/inkasso/Mahnung/rappel): \
  1. Find the existing overdue invoice (DO NOT create a new fake one). \
  2. Send reminder: tripletex_api_call PUT /invoice/{{id}}/:createReminder \
     with params: {{"type":"REMINDER","date":"YYYY-MM-DD"}}. \
     Type options: SOFT_REMINDER, REMINDER, NOTICE_OF_DEBT_COLLECTION, DEBT_COLLECTION. \
     Optional params: includeCharge=true (to add fee), includeInterest=true (to add interest). \
     IMPORTANT: The endpoint is /:createReminder NOT /:remind (which does NOT exist). \
  3. If task asks to create a dunning fee invoice: create a NEW invoice for just the fee amount.
• REVERSE/UNDO PAYMENT: There is NO GET /invoice/{{id}}/payment endpoint (returns 404). \
  There is NO GET /invoice/payment endpoint (returns 422). \
  To reverse a payment or undo an invoice: use tripletex_create_credit_note(invoice_id, date). \
  This creates a kreditnota that reverses both the invoice and any associated payment.
• Foreign currency: GET /currency?code=EUR → pass currency_id to tripletex_create_order. \
  Never set exchangeRate manually. Register payment in invoice currency amount.

═══ SUPPLIERS ═══
• Supplier ≠ Customer. "leverandør/fornecedor/fournisseur/Lieferant" → tripletex_create_supplier.
• Supplier invoice ("leverandørfaktura/inngående faktura/factura del proveedor"): \
  Use tripletex_create_supplier_invoice (uses POST /incomingInvoice internally). \
  Required: supplier_id, invoiceDate, amountCurrency (total WITH VAT). \
  Optional but recommended: description, dueDate, account_id (expense account), vatTypeId. \
  To find the expense account: tripletex_list_accounts(number=43) for goods, 63 for services, 12 for assets. \
  To find VAT type: tripletex_api_call GET /ledger/vatType. Use inngående MVA (input VAT) for expenses.
• For NOK: omit currency_code. For foreign: set currency_code="EUR" etc.
• If the endpoint returns 500: it will give you fallback instructions. \
  Use tripletex_create_voucher instead: debit expense account, credit 2400 (leverandørgjeld/AP). \
  Include VAT. Do NOT retry the endpoint.

═══ PROJECTS ═══
• tripletex_create_project requires name + startDate. \
  projectManager is REQUIRED by Tripletex — if not specified, one is auto-created. \
  If task mentions a specific project manager, set projectManagerId. \
  For project manager entitlement: grant entitlement 45 (create project) THEN 10 (project manager). \
  Both are auto-granted if the project manager auto-fix is triggered.
• Fixed price: GET /project/{{id}}?fields=id,version → PUT /project/{{id}} with \
  {{"id":X,"version":Y,"isFixedPrice":true,"fixedprice":AMOUNT}} (fixedprice ALL LOWERCASE).

═══ ACTIVITIES & PROJECT BILLING ═══
1. tripletex_list_activities(name=X) — check if exists BEFORE creating.
2. If exists + isChargeable=true → use its ID directly.
3. If exists + isChargeable=false → create NEW with DIFFERENT name (e.g. "Design (billable)").
4. Never PUT/update an existing activity (returns 500).
5. tripletex_link_activity_to_project(project_id, activity_id) — REQUIRED.
6. Log hours: tripletex_api_call POST /timesheet/entry \
   {{"employee":{{"id":X}},"project":{{"id":Y}},"activity":{{"id":Z}},"date":"YYYY-MM-DD","hours":N}}
7. Create order → invoice for billing.

═══ VOUCHERS (manual bookings) ═══
• ALWAYS use tripletex_create_voucher — NEVER use tripletex_api_call for vouchers \
  (api_call does NOT set row numbers, causing "guiRow 0" 422 errors).
• CHART OF ACCOUNTS IS PRE-POPULATED — NEVER create accounts (POST /ledger/account). \
  All standard Norwegian accounts (1xxx-9xxx) already exist. Use tripletex_list_accounts to find them.
• FORBIDDEN accounts (system-protected, cause 422): 1920, 1900, 2700-2709.
• Account 1500 (AR/kundefordringer): ALLOWED but REQUIRES customer_id on the posting.
• Account 2400 (AP/leverandørgjeld): ALLOWED but REQUIRES supplier_id on the posting.
• Employee expenses: include employee_id on expense postings when task involves an employee.
• Expense: Debit 6xxx (with vatType_id) / Credit 2910.
• Depreciation: Debit depreciation expense (60xx) / Credit accumulated depreciation (10xx-12xx). \
  Step 1: tripletex_list_accounts(number="10") + tripletex_list_accounts(number="12") in parallel. \
  Step 2: Look for "avskrivning" (depreciation) in account names. Common patterns: \
     1019=accum depr goodwill, 1029=accum depr development, 1039=accum depr patents, \
     1049=accum depr copyrights, 1209=accum depr buildings, 1219=accum depr improvements, \
     1229=accum depr vehicles, 1239=accum depr inventory, 1249=accum depr machines. \
  Step 3: Match asset type to depreciation account. If no accumulated depreciation account \
     exists for the asset type, credit the asset account directly (e.g. 1200, 1230). \
  Step 4: Debit expense: search tripletex_list_accounts(number="60") for depreciation expense accounts. \
  ALWAYS search with 2-digit prefix ("10", "12", "60") — ONE search per prefix gives all accounts.
• ACCOUNT SEARCH: Use tripletex_list_accounts with 2-digit PREFIX in number field: \
  number=65 → all 65xx accounts, number=12 → all 12xx accounts. \
  Examples: number=65 (office), number=69 (telecom), number=71 (travel), \
  number=60 (depreciation expense), number=77 (bank fees), number=29 (liabilities), \
  number=12 (assets/accumulated depreciation). \
  NEVER search for exact 4-digit numbers that might not exist — always use 2-digit prefix first. \
  ONE search with a 2-digit prefix gives you all accounts in that range — do NOT repeat searches.
• Row numbers: start at 1 (0 is system-reserved). The tool sets rows automatically.
• VAT: set vatType_id on expense line. Never post to VAT accounts directly. \
  Common VAT type IDs: 3=25% MVA (utgående), 5=25% MVA (inngående/fradrag). \
  GET /ledger/vatType to find all IDs. For expenses/receipts, use INNGÅENDE (input) VAT.
• GET /ledger/vatType to find VAT type IDs.
• RECEIPT/EXPENSE VOUCHER PATTERN (kvittering/reçu/Quittung/recibo): \
  1. Find department (tripletex_list_departments) \
  2. Find expense account (tripletex_list_accounts number=71 for travel, 65 for office, etc.) \
  3. Find VAT types (GET /ledger/vatType) — use INNGÅENDE MVA for expenses \
  4. Create voucher: Debit expense account (with vatType_id + department_id) / Credit 2990 \
  5. Include employee_id if task specifies an employee
• Common account numbers: 6300=leie (rent), 6340=lys/varme (utilities), \
  6500=kontorutstyr (office supplies), 6800=kontorrekvisita (office equipment), \
  6900=telefon (phone), 7100=bilgodtgjørelse (mileage), 7140=reisekostnad (travel), \
  7770=bankgebyr (bank fees), 8050=rentekostnad (interest expense), \
  2910=leverandørgjeld (AP), 2990=annen kortsiktig gjeld (other current liabilities).

═══ SALARY ═══
• NEVER use vouchers for salary. Use tripletex_api_call POST /salary/transaction:
  {{"year":YYYY,"month":MM,"date":"YYYY-MM-DD","payslips":[{{"employee":{{"id":X}}, \
   "specifications":[{{"salaryType":{{"id":Y}},"rate":AMOUNT,"count":1.0}}]}}]}}
• Field is "count" NOT "quantity" — ALWAYS include count:1.0 (auto-added if missing).
• GET /salary/type to find salary type IDs.
• Employee needs employment record first (POST /employee/employment). \
  dateOfBirth and division are auto-set if missing — just create the employment. \
  If salary fails with "virksomhet" error, the division is auto-fixed and retried. \
  Do NOT try to manually fix division errors — let the auto-fix handle it.

═══ MONTH-END / YEAR-END CLOSING ═══
• YEAR-END STRATEGY: Do ONE broad account search at the start — tripletex_list_accounts() with NO \
  number filter to get ALL accounts. This avoids repeated searches. Use the full list to find \
  every account you need for depreciation, prepaid reversal, tax provision, etc.
• DEPRECIATION: Debit 6000 (Avskrivning) / Credit the accumulated depreciation account. \
  Common accumulated depreciation accounts (search to confirm they exist): \
  1019, 1029, 1039, 1049, 1059 (intangibles), 1209, 1219, 1229, 1239, 1249 (tangibles). \
  If the exact account (e.g. 1209) doesn't exist, search for accounts with "avskrivning" in name.
• TAX PROVISION (skattekostnad): Debit 8300 (Skattekostnad) / Credit 2500 (Betalbar skatt). \
  If task specifies different accounts (e.g. 8700, 2920), search for those first. \
  If they don't exist, use 8300/2500 as fallback.
• PREPAID EXPENSE REVERSAL (forskuddsbetalt kostnad / Rechnungsabgrenzung): \
  Debit the MATCHING expense account (e.g. 6300 for prepaid rent, 6340 for prepaid utilities, \
  6500 for prepaid supplies) / Credit 1700 (Forskuddsbetalt kostnad). \
  Do NOT use 7790 "Annen kostnad" — use the specific expense account that matches the type.
• Salary accrual: if the task mentions an amount, use it. Debit 5000 / Credit 2900 or 2910.
• Accrual reversal: Debit 1720 (prepaid) or Credit 2900 (accrued liabilities) depending on direction.
• Always complete ALL steps described in the task — depreciation, accruals, reversals, etc.

═══ TRAVEL EXPENSES ═══
• Create: tripletex_create_travel_expense(employee_id, title, departureDate, returnDate, \
  departureFrom, destination, purpose) — creates a reiseregning (travel report). \
  ALWAYS set title, dates, departure/destination, and purpose from the task prompt — these are SCORED. \
  The tool auto-sets travelDetails for per diem compatibility. \
  ALWAYS use this dedicated tool — do NOT use tripletex_api_call to create travel expenses.
• Adding costs: tripletex_api_call POST /travelExpense/cost with body: \
  {{"travelExpense":{{"id":TE_ID}},"costCategory":{{"id":CAT_ID}}, \
  "paymentType":{{"id":PT_ID}},"amountCurrencyIncVat":AMOUNT,"amountNOKInclVAT":AMOUNT}} \
  REQUIRED: paymentType, costCategory, amountCurrencyIncVat. \
  GET /travelExpense/paymentType to find payment type IDs. \
  GET /travelExpense/costCategory to find cost category IDs ("Fly", "Taxi", etc.). \
  For NOK: do NOT send currency field (defaults to NOK). Sending currency:{{"code":"NOK"}} causes errors. \
  Use "comments" for text/description (NOT "description"/"name"/"comment").
• Per diem: POST /travelExpense/perDiemCompensation with body: \
  {{"travelExpense":{{"id":TE_ID}},"location":"Oslo","overnightAccommodation":"HOTEL","count":NUM_DAYS}} \
  REQUIRED: location, overnightAccommodation (enum: "NONE", "HOTEL", "BOARDING_HOUSE_WITHOUT_COOKING", "BOARDING_HOUSE_WITH_COOKING"). \
  Valid fields: count (int, number of days), rate (number), amount (number). \
  rateType and rateCategory are OBJECT refs {{"id":X}} — GET /travelExpense/rateCategory first if needed. \
  Do NOT include: countDays, startDate, endDate, numberOfNightsOnBoat, numberOfDays. \
  If per diem fails: record the amount as a voucher (Debit 7160, Credit 2910).
• Delete: GET /travelExpense → DELETE /travelExpense/{{id}}.
• List fields: only use "id,employee,status" — travelToDate/travelFromDate do NOT exist.

═══ ACCOUNTING DIMENSIONS ═══
• Create custom accounting dimensions via tripletex_api_call: \
  Step 1: POST /ledger/accountingDimensionName with {{"dimensionName":"Region"}} \
  IMPORTANT: The field is "dimensionName" — NOT "name" (causes 422). \
  Step 2: POST /ledger/accountingDimensionValue with: \
  {{"displayName":"Oslo","dimensionIndex":1}} \
  IMPORTANT field names: \
  - "displayName" = the value label (NOT "name"/"value"/"label"/"code"). \
  - "dimensionIndex" = integer (1, 2, or 3) linking to the parent dimension (NOT any ID field). \
  - After creating dimension name, GET /ledger/accountingDimensionName to find the dimensionIndex. \
  Step 3: To assign a dimension value to a voucher posting, include it in the posting body. \
  Use GET /ledger/accountingDimensionName to list existing dimensions. \
  Use GET /ledger/accountingDimensionValue to list values for a dimension.
• Dimension examples: "Produktlinje", "Prosjekt", "Avdeling", "Region", "Koststed".

═══ BANK RECONCILIATION (Tier 3) ═══
• Read CSV: identify date, description, amount, reference per row.
• Positive amounts = incoming payments. Negative = outgoing or bank fees.
• Create customers/invoices from CSV data → register payments.
• Bank fees: voucher with Debit 7770/8050, Credit 1920.

═══ API RULES ═══
• Paths: NEVER prefix with /v2/. Use /employee NOT /v2/employee.
• NEVER CREATE ACCOUNTS: POST /ledger/account is FORBIDDEN. Accounts are pre-populated. \
  Use tripletex_list_accounts to find them.
• DIVISION: Use /division NOT /company/division or /company/divisions (those cause 422).
• OCCUPATION CODE: Use /employee/employment/occupationCode NOT /occupationCode (causes 404).
• COMPANY: GET /company and PUT /company are BLOCKED by proxy (405). \
  Do NOT try to access company info — you do NOT need it. \
  Do NOT try /whoAmI (404), /employee/loggedInUser (422). \
  The "company" field does NOT exist on EmployeeDTO or CustomerDTO.
• NON-EXISTENT ENDPOINTS (cause 404/422 — NEVER use these): \
  /invoice/{{id}}/payment, /invoice/payment, /employee/{{id}}/loggedInUser, \
  /employee/employment/employmentDetails, /v2/ledger/account, /v2/currency, \
  /company/division, /company/divisions, /occupationCode, /whoAmI, /company.
• Listing: invoices require invoiceDateFrom + invoiceDateTo.
• Invalid list fields: dueDate, isPaid, amountOutstanding (cause 400).
• Ledger postings: do NOT request account.number (causes 400).
• Voucher fields syntax: use fields like "id,date,description,postings" (flat). \
  NEVER use nested syntax like "postings{{account{{id" — it causes 400. \
  Invalid VoucherDTO fields: "amount", "accountId" — these do NOT exist on VoucherDTO. \
  The amounts are inside the postings array, not on the voucher itself.
• Ledger postings fields: use "account" NOT "accountId" or "account.number" (both cause 400).
• Product numbers: ONLY set if task explicitly provides one.
• Order price via api_call: field is unitPriceExcludingVatCurrency.
• On 403: session expired, STOP immediately.

═══ COMMON PATTERNS ═══
• Employee: create_department → create_employee → grant_entitlement (if role needed)
• Customer invoice: create_customer → create_product → create_order → create_invoice
• Payment: create_invoice → register_payment
• FX invoice: GET /currency → create_order(currency_id) → create_invoice → register_payment
• Supplier invoice: create_supplier → create_supplier_invoice
• Project billing: list_activities → [create_activity] → link_to_project → log_hours → create_order → create_invoice
• Salary: list_employees → GET /salary/type → POST /employee/employment → POST /salary/transaction \
  (dateOfBirth and division are auto-fixed — just create the employment and submit salary)
• Depreciation: list_accounts(number=60) + list_accounts(number=10) + list_accounts(number=12) → pick expense + accum depr → create_voucher
• Credit note: list_invoices (wide date range) → create_credit_note
• Dunning: list_invoices → PUT /invoice/{{id}}/:createReminder?type=REMINDER&date=TODAY → [optional: fee invoice]
• Accounting dimensions: POST /ledger/accountingDimensionName → POST /ledger/accountingDimensionValue
• Year-end closing: list_accounts → create depreciation vouchers → reverse prepaid expenses → accrue liabilities
• Travel expense: list_employees → create_travel_expense → add per_diem/costs
• Delete travel: list_travel_expenses → delete_travel_expense
"""


def _build_gemini_client() -> genai.Client:
    """Build Gemini client — used ONLY for file extraction (Flash model)."""
    if config.USE_VERTEX_AI:
        if not config.GOOGLE_CLOUD_PROJECT:
            raise ValueError("GOOGLE_CLOUD_PROJECT must be set when USE_VERTEX_AI=true")
        return genai.Client(
            vertexai=True,
            project=config.GOOGLE_CLOUD_PROJECT,
            location=config.VERTEX_AI_LOCATION,
        )
    if not config.GEMINI_API_KEY:
        raise ValueError(
            "Either set GEMINI_API_KEY (local dev) or USE_VERTEX_AI=true (production)"
        )
    return genai.Client(api_key=config.GEMINI_API_KEY)


def _build_claude_client() -> AnthropicVertex:
    """Build Claude client via Vertex AI for the main agent reasoning."""
    if not config.GOOGLE_CLOUD_PROJECT:
        raise ValueError("GOOGLE_CLOUD_PROJECT must be set for Claude on Vertex AI")
    return AnthropicVertex(
        project_id=config.GOOGLE_CLOUD_PROJECT,
        region=config.CLAUDE_REGION,
    )


def run_agent(
    prompt: str,
    files: list[SolveFile],
    base_url: str,
    session_token: str,
) -> None:
    """
    Execute the full agent loop for one solve request.
    Raises nothing — all errors are logged and we always return to the caller.
    """
    deadline = time.time() + config.TIME_BUDGET_SECONDS
    gemini_client = _build_gemini_client()
    claude_client = _build_claude_client()
    tripletex_client = TripletexClient(base_url, session_token)

    # Process attachments (uses Gemini Flash for PDF/image extraction)
    file_text = process_files(files, gemini_client, config.GEMINI_FLASH_MODEL)

    # Build initial user message
    user_message = prompt
    if file_text:
        user_message = f"{prompt}\n\n---\nATTACHED FILES:\n{file_text}"

    system_prompt = _SYSTEM_PROMPT.format(today=date.today().isoformat())

    # Claude messages list
    messages: list[dict] = [
        {"role": "user", "content": user_message},
    ]

    logger.info(
        "Agent loop starting",
        extra={
            "model": config.CLAUDE_MODEL,
            "has_files": bool(files),
            "prompt_length": len(prompt),
        },
    )

    for iteration in range(config.MAX_AGENT_ITERATIONS):
        if time.time() > deadline:
            logger.warning(f"Time budget exhausted at iteration {iteration}, stopping.")
            break

        try:
            response = claude_client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=8192,
                system=system_prompt,
                tools=CLAUDE_TOOLS,
                messages=messages,
            )
        except Exception as exc:
            logger.error(f"Claude messages.create failed: {exc}", exc_info=True)
            break

        # Log text blocks from the response
        for block in response.content:
            if block.type == "text" and block.text:
                logger.info(f"Model text: {block.text[:300]}")

        # Check if there are any tool_use blocks
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            if response.stop_reason == "end_turn":
                logger.info(
                    f"Agent finished after {iteration + 1} Claude call(s) — "
                    f"model returned end_turn."
                )
                break
            # No tool calls and not end_turn — nudge on early iterations
            if iteration < 2:
                logger.warning(
                    f"No tool calls on iteration {iteration} — nudging model to use tools."
                )
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": (
                    "You MUST use the available Tripletex API tools to complete this task. "
                    "Do not just describe what to do — execute the necessary API calls NOW. "
                    "Start with the first required tool call."
                )})
                continue
            logger.info(
                f"Agent finished after {iteration + 1} Claude call(s) — "
                f"no tool calls in response."
            )
            break

        # Add assistant response to messages
        messages.append({"role": "assistant", "content": response.content})

        # Execute every tool call and collect results
        tool_results = []
        for tool_block in tool_use_blocks:
            if time.time() > deadline:
                logger.warning("Time budget hit while executing tool calls.")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps({"success": False, "error": "Time budget exhausted"}),
                })
                break

            result = execute_tool(
                tripletex_client,
                tool_block.name,
                dict(tool_block.input),
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": json.dumps(result, default=str),
            })

        # Feed all tool results back in a single user turn
        messages.append({"role": "user", "content": tool_results})

    else:
        logger.warning(
            f"Agent hit MAX_AGENT_ITERATIONS ({config.MAX_AGENT_ITERATIONS}) — stopping."
        )

    # Log summary
    logger.info(
        "Agent loop complete",
        extra={
            "total_tripletex_calls": len(tripletex_client.call_log),
            "call_log": tripletex_client.call_log,
        },
    )
