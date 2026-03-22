"""
Gemini function/tool declarations for the Tripletex v2 API.

Two exports:
  TOOLS          — types.Tool passed to Gemini generate_content()
  execute_tool() — dispatches a function call to the TripletexClient

Design rules:
  • Every tool returns {"success": True, "data": ...} or {"success": False, "error": "..."}
  • Errors are returned AS TOOL RESULTS (not raised) so Gemini can self-correct
  • No blind retries here — the model decides whether and how to retry
  • A generic escape-hatch tool (tripletex_api_call) handles anything not
    explicitly mapped, including enabling modules and setting employee roles
"""
import json
import logging
from typing import Any

from google.genai import types

from .tripletex.client import TripletexClient
from .tripletex.errors import TripletexError

logger = logging.getLogger(__name__)

# ── Type aliases for brevity ───────────────────────────────────────────────────
_S = types.Type.STRING
_I = types.Type.INTEGER
_N = types.Type.NUMBER
_B = types.Type.BOOLEAN
_A = types.Type.ARRAY
_O = types.Type.OBJECT


def _s(desc: str, **kw) -> types.Schema:
    return types.Schema(type=_S, description=desc, **kw)

def _i(desc: str) -> types.Schema:
    return types.Schema(type=_I, description=desc)

def _n(desc: str) -> types.Schema:
    return types.Schema(type=_N, description=desc)

def _b(desc: str) -> types.Schema:
    return types.Schema(type=_B, description=desc)

def _obj(props: dict, required: list[str] | None = None, desc: str = "") -> types.Schema:
    kw: dict = {"type": _O, "properties": props}
    if desc:
        kw["description"] = desc
    if required:
        kw["required"] = required
    return types.Schema(**kw)

def _arr(items: types.Schema, desc: str = "") -> types.Schema:
    kw: dict = {"type": _A, "items": items}
    if desc:
        kw["description"] = desc
    return types.Schema(**kw)


# ── Tool declarations ──────────────────────────────────────────────────────────

_DECLARATIONS = [

    # ── Employees ─────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_list_employees",
        description="List employees. Use to find an employee by name before updating or deleting.",
        parameters=_obj({
            "name": _s("Filter by full or partial name"),
            "fields": _s("Comma-separated fields, e.g. 'id,firstName,lastName,email'"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_get_employee",
        description="Get a single employee by ID. Use when you need the version number for a PUT.",
        parameters=_obj({"id": _i("Employee ID"), "fields": _s("Fields to return")}, required=["id"]),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_employee",
        description=(
            "Create a new employee. "
            "IMPORTANT: If the task says the employee should be 'kontoadministrator' or "
            "'administrator', after creating call tripletex_grant_entitlement to grant the role "
            "(first GET /employee/entitlement to find the entitlement ID)."
        ),
        parameters=_obj(
            {
                "firstName": _s("First name"),
                "lastName": _s("Last name"),
                "email": _s("Email address"),
                "userTypeId": _i("User type integer ID. Default 1 (standard employee). Use 1 unless the task explicitly requires a different type."),
                "departmentId": _i("Department ID to assign employee to — optional"),
                "employeeNumber": _s("Employee number (optional)"),
                "phoneNumberMobile": _s("Mobile phone number"),
                "phoneNumberHome": _s("Home phone number"),
                "phoneNumberWork": _s("Work phone number"),
                "dateOfBirth": _s("Date of birth (YYYY-MM-DD) — from employment contract"),
                "nationalIdentityNumber": _s("Norwegian national identity number / personnummer — from employment contract"),
                "bankAccountNumber": _s("Employee's personal bank account number — from employment contract"),
            },
            required=["firstName", "lastName"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_grant_entitlement",
        description=(
            "Grant a role or access entitlement to an employee. "
            "Use this to grant 'kontoadministrator' (admin) or 'project manager' access. "
            "Step 1: call tripletex_api_call GET /employee/entitlement to list available "
            "entitlement IDs for this account. "
            "Step 2: call this tool with the employee_id and the correct entitlement_id. "
            "IMPORTANT: For project manager — grant the entitlement BEFORE creating "
            "the project with that employee as projectManager."
        ),
        parameters=_obj(
            {
                "employee_id": _i("Employee ID to grant the entitlement to"),
                "entitlement_id": _i("Entitlement ID (discover via GET /employee/entitlement)"),
            },
            required=["employee_id", "entitlement_id"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_update_employee",
        description=(
            "Update an existing employee. "
            "Requires the current version number — GET the employee first to obtain it. "
            "INVALID fields (cause 422): annualSalary, salary, wage — salary is handled via "
            "/salary/transaction, NOT via the employee record."
        ),
        parameters=_obj(
            {
                "id": _i("Employee ID"),
                "version": _i("Current version (required for optimistic locking)"),
                "firstName": _s("First name"),
                "lastName": _s("Last name"),
                "email": _s("Email address"),
                "phoneNumberMobile": _s("Mobile phone"),
                "phoneNumberHome": _s("Home phone"),
                "phoneNumberWork": _s("Work phone"),
                "dateOfBirth": _s("Date of birth (YYYY-MM-DD)"),
                "nationalIdentityNumber": _s("Norwegian national identity number / personnummer"),
                "bankAccountNumber": _s("Employee's personal bank account number"),
            },
            required=["id", "version"],
        ),
    ),

    # ── Suppliers ─────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_create_supplier",
        description=(
            "Create a new SUPPLIER (leverandør / fornecedor / fournisseur / Lieferant). "
            "Use this when the task says 'supplier', 'leverandør', 'fornecedor', 'fournisseur', or 'Lieferant'. "
            "Do NOT use tripletex_create_customer for suppliers — they are different entities."
        ),
        parameters=_obj(
            {
                "name": _s("Supplier / company name"),
                "email": _s("Email address"),
                "organizationNumber": _s("Norwegian org. number (9 digits)"),
                "phoneNumber": _s("Phone number"),
            },
            required=["name"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_supplier_invoice",
        description=(
            "Register an incoming supplier invoice (leverandørfaktura / inngående faktura). "
            "Use when the task says 'register invoice from supplier', 'book incoming invoice', "
            "'registrer leverandørfaktura', or similar. "
            "This creates the invoice in Tripletex and auto-generates the AP accounting entries. "
            "After creating, you can approve it via tripletex_api_call PUT /supplierInvoice/{id}/:approve."
        ),
        parameters=_obj(
            {
                "supplier_id": _i("Supplier ID (create supplier first if needed)"),
                "invoiceDate": _s("Invoice date (YYYY-MM-DD)"),
                "amountCurrency": _n("Total invoice amount INCLUDING VAT. Do NOT compute or pass amountExcludingVatCurrency — send only the total."),
                "currency_code": _s("Currency code, e.g. 'NOK', 'EUR', 'USD' — defaults to NOK. Omit for NOK."),
                "invoiceNumber": _s("Supplier's invoice number — optional"),
                "kid": _s("Payment reference / KID number — optional"),
            },
            required=["supplier_id", "invoiceDate", "amountCurrency"],
        ),
    ),

    # ── Customers ─────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_list_customers",
        description="List customers. Use to find an existing customer by name.",
        parameters=_obj({
            "name": _s("Filter by name (partial match)"),
            "fields": _s("Fields to return, e.g. 'id,name,email'"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_customer",
        description="Create a new customer (company or individual).",
        parameters=_obj(
            {
                "name": _s("Customer / company name"),
                "email": _s("Email address"),
                "organizationNumber": _s("Norwegian org. number (9 digits)"),
                "phoneNumber": _s("Phone number"),
                "address": _s("Street address line"),
                "postalCode": _s("Postal code"),
                "city": _s("City"),
                "description": _s("Customer description / notes — optional"),
            },
            required=["name"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_update_customer",
        description="Update an existing customer. Requires version from a prior GET.",
        parameters=_obj(
            {
                "id": _i("Customer ID"),
                "version": _i("Current version number"),
                "name": _s("Customer name"),
                "email": _s("Email"),
                "phoneNumber": _s("Phone number"),
                "address": _s("Street address"),
                "description": _s("Customer description / notes"),
            },
            required=["id", "version"],
        ),
    ),

    # ── Products ──────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_list_products",
        description="List products.",
        parameters=_obj({
            "name": _s("Filter by name"),
            "fields": _s("Fields to return"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_product",
        description=(
            "Create a new product. "
            "The 'number' field is optional — only set it if the task explicitly gives a specific product number. "
            "For non-standard VAT rates (15%, 0%, etc.) query GET /ledger/vatType to find the correct vatTypeId."
        ),
        parameters=_obj(
            {
                "name": _s("Product name"),
                "number": _s("Product number — ONLY set if the task explicitly provides a specific number. Omit to let Tripletex auto-assign."),
                "priceExcludingVatCurrency": _n("Sales price excluding VAT"),
                "costExcludingVatCurrency": _n("Cost price excluding VAT"),
                "vatTypeId": _i("VAT type ID. 3 = standard 25% Norwegian VAT. Query GET /ledger/vatType for other rates."),
            },
            required=["name"],
        ),
    ),

    # ── Orders ────────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_create_order",
        description=(
            "Create an order for a customer. "
            "An order is REQUIRED before you can create an invoice. "
            "Order lines specify what products or services are being sold."
        ),
        parameters=_obj(
            {
                "customer_id": _i("Customer ID"),
                "orderDate": _s("Order date (YYYY-MM-DD)"),
                "deliveryDate": _s("Delivery date (YYYY-MM-DD) — optional"),
                "currency_id": _i("Currency ID for foreign currency orders — find with GET /currency?code=EUR. Omit for NOK."),
                "orderLines": _arr(
                    _obj({
                        "product_id": _i("Product ID — omit for free-text lines"),
                        "description": _s("Line item description"),
                        "count": _n("Quantity"),
                        "unitPriceExcludingVat": _n("Unit price excluding VAT"),
                        "vatTypeId": _i("VAT type ID (3 = 25%)"),
                    }),
                    desc="List of order lines",
                ),
            },
            required=["customer_id", "orderDate"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_get_order",
        description="Get order details by ID.",
        parameters=_obj(
            {"id": _i("Order ID"), "fields": _s("Fields to return")},
            required=["id"],
        ),
    ),

    # ── Invoices ──────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_create_invoice",
        description=(
            "Create an invoice from an existing order. "
            "You MUST have an order_id — create the order first if needed. "
            "Full chain: create customer → create product → create order → create invoice. "
            "To send the invoice to the customer, call tripletex_send_invoice AFTER creation."
        ),
        parameters=_obj(
            {
                "order_id": _i("Order ID to invoice"),
                "invoiceDate": _s("Invoice date (YYYY-MM-DD)"),
                "invoiceDueDate": _s("Payment due date (YYYY-MM-DD)"),
            },
            required=["order_id", "invoiceDate"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_list_invoices",
        description=(
            "List invoices. invoiceDateFrom and invoiceDateTo are REQUIRED by the API. "
            "INVALID fields (cause 400): dueDate, isPaid, amountOutstanding, amountOutstandingCurrency — auto-stripped. "
            "Valid fields: id, invoiceDate, invoiceDueDate, amountCurrency, customer, amount, invoiceNumber."
        ),
        parameters=_obj({
            "invoiceDateFrom": _s("Start date filter (YYYY-MM-DD) — REQUIRED"),
            "invoiceDateTo": _s("End date filter (YYYY-MM-DD) — REQUIRED"),
            "customerId": _i("Filter by customer ID — optional"),
            "fields": _s("Fields to return. AVOID: dueDate, isPaid, amountOutstanding (cause 400)"),
            "count": _i("Max number of results"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_send_invoice",
        description="Send an invoice to the customer.",
        parameters=_obj(
            {
                "invoice_id": _i("Invoice ID"),
                "sendType": _s("Send method", enum=["EMAIL", "EHF", "EFAKTURA", "AVTALEGIRO"]),
                "recipientEmail": _s("Override recipient email — optional"),
            },
            required=["invoice_id", "sendType"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_credit_note",
        description="Create a credit note (reversal / kreditnota) for an invoice.",
        parameters=_obj(
            {
                "invoice_id": _i("Invoice ID to reverse"),
                "date": _s("Credit note date (YYYY-MM-DD)"),
                "comment": _s("Reason for credit note — optional"),
            },
            required=["invoice_id", "date"],
        ),
    ),

    # ── Payments ──────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_register_payment",
        description="Register a payment against an invoice.",
        parameters=_obj(
            {
                "invoice_id": _i("Invoice ID"),
                "paymentDate": _s("Payment date (YYYY-MM-DD)"),
                "amount": _n("Amount paid (in invoice currency)"),
                "paymentTypeId": _i("Payment type ID. 1 = bank transfer (default), 2 = cash."),
            },
            required=["invoice_id", "paymentDate", "amount"],
        ),
    ),

    # ── Travel Expenses ───────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_list_travel_expenses",
        description="List travel expense reports.",
        parameters=_obj({
            "fields": _s("Fields to return, e.g. 'id,description,employee,travelFromDate'"),
            "count": _i("Max results"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_travel_expense",
        description=(
            "Create a travel expense report container. "
            "IMPORTANT: Only 'employee_id' is accepted on creation. "
            "Do NOT send description, travelFromDate, travelToDate, or purpose — "
            "those fields don't exist on POST (they are read-only / set via sub-resources). "
            "After creating the report, use tripletex_api_call to add details: "
            "POST /travelExpense/{id}/perDiemCompensation for per-diem with dates, "
            "POST /travelExpense/{id}/cost for individual costs."
        ),
        parameters=_obj(
            {
                "employee_id": _i("Employee ID who travelled"),
            },
            required=["employee_id"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_delete_travel_expense",
        description="Delete a travel expense report by ID.",
        parameters=_obj({"id": _i("Travel expense ID to delete")}, required=["id"]),
    ),

    # ── Projects ──────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_list_projects",
        description="List projects.",
        parameters=_obj({
            "name": _s("Filter by project name"),
            "fields": _s("Fields to return"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_project",
        description="Create a new project, optionally linked to a customer.",
        parameters=_obj(
            {
                "name": _s("Project name"),
                "number": _s("Project number (optional)"),
                "customer_id": _i("Customer ID to link — optional"),
                "startDate": _s("Start date (YYYY-MM-DD)"),
                "endDate": _s("End date (YYYY-MM-DD) — optional"),
                "description": _s("Project description — optional"),
                "projectManagerId": _i("Employee ID of project manager — optional"),
            },
            required=["name", "startDate"],
        ),
    ),

    # ── Activities ────────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_create_activity",
        description=(
            "Create an activity (work type used for time tracking on projects). "
            "Use this when the task asks to create activities, tasks, or work types. "
            "The activityType is set automatically to 1 (GENERAL_HOURS) — do not pass it. "
            "After creating, link the activity to the project using tripletex_link_activity_to_project."
        ),
        parameters=_obj(
            {
                "name": _s("Activity name"),
                "description": _s("Description — optional"),
                "isGeneral": _b("Whether this is a general/global activity (default false)"),
                "isChargeable": _b("Whether hours on this activity are chargeable to the customer"),
            },
            required=["name"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_list_activities",
        description=(
            "List activities. Call this BEFORE creating an activity to check if one with that name "
            "already exists (Tripletex has default global activities for common names like 'Utvikling', "
            "'Design', 'Administrasjon'). If the activity exists and isChargeable=true, use its ID "
            "directly. If it exists but isChargeable=false, create a NEW activity with a DIFFERENT name. "
            "NEVER try to update/PUT an existing activity — it returns 500."
        ),
        parameters=_obj({
            "name": _s("Filter by activity name (partial match)"),
            "fields": _s("Fields to return, e.g. 'id,name,isChargeable,isGeneral'"),
            "count": _i("Max results"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_link_activity_to_project",
        description=(
            "Link an activity to a project so it can be used for time tracking and billing. "
            "Call AFTER creating both the project and the activity. "
            "NEVER use PUT /project/{id} or PUT /project/{id}/activity — those do not work. "
            "The correct endpoint is POST /project/projectActivity."
        ),
        parameters=_obj(
            {
                "project_id": _i("Project ID"),
                "activity_id": _i("Activity ID to link to the project"),
            },
            required=["project_id", "activity_id"],
        ),
    ),

    # ── Departments ───────────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_list_departments",
        description="List departments.",
        parameters=_obj({"fields": _s("Fields to return")}),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_department",
        description="Create a new department.",
        parameters=_obj(
            {
                "name": _s("Department name"),
                "departmentNumber": _s("Department number — optional"),
                "departmentManagerId": _i("Employee ID of department manager — optional"),
            },
            required=["name"],
        ),
    ),

    # ── Ledger (Tier 3) ───────────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_list_accounts",
        description=(
            "List chart-of-accounts entries (ledger accounts). "
            "All accounts are PRE-POPULATED — never create accounts. "
            "Supports PREFIX search: number=65 returns all 65xx accounts, "
            "number=12 returns all 12xx. Omit number to get ALL accounts. "
            "Call ONCE with the right prefix — do not repeat searches."
        ),
        parameters=_obj({
            "number": _s("Account number prefix (e.g. '65' for all 65xx accounts). Omit to get all accounts."),
            "fields": _s("Fields to return, e.g. 'id,number,name'"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_list_vouchers",
        description="List vouchers (bilag). Default returns up to 100 vouchers.",
        parameters=_obj({
            "dateFrom": _s("From date (YYYY-MM-DD)"),
            "dateTo": _s("To date (YYYY-MM-DD)"),
            "fields": _s("Fields to return"),
            "count": _i("Max results (default 100)"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_voucher",
        description=(
            "Create a manual voucher (bilag) with debit/credit postings. "
            "ALWAYS use this tool for vouchers — NEVER use tripletex_api_call for /ledger/voucher "
            "(api_call does not set row numbers, causing row-0 422 errors). "
            "FORBIDDEN accounts (system-protected — will cause 422 guiRow-0 error): "
            "bank/cash (1920, 1900), VAT (2700-2709). "
            "Account 1500 (AR): allowed but REQUIRES customer_id on the posting. "
            "Account 2400 (AP): allowed but REQUIRES supplier_id on the posting. "
            "For expenses/receipts: Debit expense account (6xxx) + specify vatType_id on that line; "
            "Credit accounts payable (2910 leverandørgjeld) or other liability (2990). "
            "For employee expenses (reise/expense): include employee_id on the posting. "
            "For depreciation: Debit depreciation expense (6010/6020 etc.), "
            "Credit accumulated depreciation (12x9 — search with tripletex_list_accounts number=12). "
            "NEVER manually post to VAT accounts — set vatType_id on the expense line instead."
        ),
        parameters=_obj(
            {
                "date": _s("Voucher date (YYYY-MM-DD)"),
                "description": _s("Description"),
                "postings": _arr(
                    _obj({
                        "account_id": _i("Ledger account ID (from tripletex_list_accounts)"),
                        "amount": _n("Amount (positive = debit, negative = credit)"),
                        "description": _s("Posting description"),
                        "vatType_id": _i(
                            "VAT type ID — set this on expense lines instead of posting manually "
                            "to VAT accounts. Find IDs via tripletex_api_call GET /ledger/vatType."
                        ),
                        "department_id": _i("Department ID to assign this posting to"),
                        "customer_id": _i("Customer ID — REQUIRED when posting to AR account 1500 (kundefordringer)"),
                        "supplier_id": _i("Supplier ID — REQUIRED when posting to AP account 2400 (leverandørgjeld)"),
                        "employee_id": _i("Employee ID — required for employee-related expense postings (reise, personal expenses)"),
                    }),
                    desc="Debit/credit postings. Must balance (sum to zero).",
                ),
            },
            required=["date", "postings"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_list_postings",
        description="List ledger postings.",
        parameters=_obj({
            "dateFrom": _s("From date (YYYY-MM-DD)"),
            "dateTo": _s("To date (YYYY-MM-DD)"),
            "fields": _s("Fields to return"),
        }),
    ),

    # ── Generic escape hatch ──────────────────────────────────────────────────

    types.FunctionDeclaration(
        name="tripletex_api_call",
        description=(
            "Make a custom call to ANY Tripletex v2 endpoint. "
            "Use this for operations not covered by the other tools, such as: "
            "granting entitlements (POST /employee/entitlement with employee, entitlement, customer:{id:0}), "
            "enabling modules (PUT /company/{id}/settings), "
            "timesheet entries (POST /timesheet/entry), "
            "salary transactions (POST /salary/transaction), "
            "action endpoints like /invoice/{id}/:remind. "
            "IMPORTANT: Do NOT use this for payment registration — "
            "use tripletex_register_payment instead."
        ),
        parameters=_obj(
            {
                "method": _s("HTTP method", enum=["GET", "POST", "PUT", "DELETE"]),
                "path": _s("API path starting with /, e.g. /employee/42/loggedInUser"),
                "params": types.Schema(
                    type=_O,
                    description="Query string parameters as key-value pairs",
                ),
                "body": types.Schema(
                    type=_O,
                    description="Request body for POST or PUT",
                ),
            },
            required=["method", "path"],
        ),
    ),
]

TOOLS = types.Tool(function_declarations=_DECLARATIONS)


# ── Auto-fix helpers ──────────────────────────────────────────────────────────
# These transparently resolve known error patterns without consuming LLM
# iterations.  The model never sees the error — only the fixed result.

def _auto_fix_payment_404(client: TripletexClient, args: dict) -> Any | None:
    """Fix payment 404 by discovering the correct paymentTypeId for this sandbox."""
    # If we already discovered the correct payment type, reuse it
    cached_id = getattr(client, "_cached_payment_type_id", None)
    if cached_id:
        logger.info(f"Payment 404 — reusing cached paymentTypeId={cached_id}")
        try:
            return client.put(
                f"/invoice/{args['invoice_id']}/:payment",
                params={
                    "paymentDate": args["paymentDate"],
                    "paidAmount": args["amount"],
                    "paymentTypeId": cached_id,
                },
            )
        except TripletexError:
            return None

    logger.info("Payment 404 — auto-fetching valid payment types")
    try:
        pt = client.get("/invoice/paymentType", params={"fields": "id,description", "count": 5})
        values = (pt or {}).get("values", [])
        if not values:
            return None
        new_id = values[0]["id"]
        client._cached_payment_type_id = new_id  # Cache for future payments
        return client.put(
            f"/invoice/{args['invoice_id']}/:payment",
            params={
                "paymentDate": args["paymentDate"],
                "paidAmount": args["amount"],
                "paymentTypeId": new_id,
            },
        )
    except TripletexError:
        return None


def _auto_fix_employee_email(client: TripletexClient, args: dict, exc: TripletexError) -> Any | None:
    """Find existing employee when email-conflict 422 occurs."""
    has_email_error = any(
        "email" in (vm.get("field") or "").lower()
        for vm in exc.validation_messages
    )
    if not has_email_error or not args.get("email"):
        return None
    logger.info(f"Employee email conflict — searching for existing: {args.get('email')}")
    try:
        search = client.get("/employee", params={
            "email": args["email"],
            "fields": "id,firstName,lastName,email,version",
            "count": 1,
        })
        values = (search or {}).get("values", [])
        if values:
            return {"value": values[0], "_note": "Employee already existed (found by email)"}
    except TripletexError:
        pass
    return None


def _auto_fix_activity_name(client: TripletexClient, args: dict) -> Any | None:
    """Find existing activity when name-conflict 422 occurs."""
    name = args.get("name")
    if not name:
        return None
    logger.info(f"Activity name conflict — searching for existing: {name}")
    try:
        search = client.get("/activity", params={
            "name": name,
            "fields": "id,name,isChargeable,isGeneral",
            "count": 5,
        })
        values = (search or {}).get("values", [])
        if values:
            return {"value": values[0], "_note": "Activity already existed (found by name)"}
    except TripletexError:
        pass
    return None


def _auto_fix_product_exists(client: TripletexClient, args: dict) -> Any | None:
    """Find existing product when creation fails with 'already registered'."""
    name = args.get("name")
    if not name:
        return None
    logger.info(f"Product already exists — searching for: {name}")
    try:
        search = client.get("/product", params={
            "name": name,
            "fields": "id,name,number,priceExcludingVatCurrency",
            "count": 1,
        })
        values = (search or {}).get("values", [])
        if values:
            return {"value": values[0], "_note": "Product already existed (found by name)"}
    except TripletexError:
        pass
    return None


def _auto_create_project_manager(client: TripletexClient) -> int | None:
    """Create a default employee and grant project manager entitlements."""
    try:
        # Try to find an existing employee first
        existing = client.get("/employee", params={"fields": "id,firstName", "count": 1})
        existing_values = (existing or {}).get("values", [])
        if existing_values:
            emp_id = existing_values[0]["id"]
        else:
            # Create a minimal employee
            dept_id = None
            try:
                dept_resp = client.post("/department", {"name": "Generell"})
                dept_id = dept_resp["value"]["id"]
            except TripletexError:
                try:
                    depts = client.get("/department", params={"fields": "id", "count": 1})
                    dept_id = (depts or {}).get("values", [{}])[0].get("id")
                except TripletexError:
                    pass
            emp_body: dict = {
                "firstName": "Admin",
                "lastName": "User",
                "email": "admin@example.com",
                "dateOfBirth": "1990-01-01",
            }
            if dept_id:
                emp_body["department"] = {"id": dept_id}
            emp = client.post("/employee", emp_body)
            emp_id = emp["value"]["id"]

        # Grant entitlements: 45 (create project) MUST come before 10 (project manager)
        for ent_id in (45, 10):
            try:
                client.post("/employee/entitlement", {
                    "employee": {"id": emp_id},
                    "entitlementId": ent_id,
                    "customer": {"id": 0},
                })
            except TripletexError:
                pass  # May already be granted

        logger.info(f"Auto-created project manager (employee {emp_id})")
        return emp_id
    except TripletexError as e:
        logger.warning(f"Failed to auto-create project manager: {e}")
        return None


def _auto_fix_salary_division(client: TripletexClient, body: dict) -> bool:
    """Fix salary division error by linking the employee's employment to a division."""
    try:
        # Find the employee ID from the salary transaction body
        payslips = body.get("payslips") or []
        if not payslips:
            return False
        emp_id = (payslips[0].get("employee") or {}).get("id")
        if not emp_id:
            return False

        # Find a valid division
        try:
            divs = client.get("/division", params={"fields": "id,name", "count": 1})
            div_values = (divs or {}).get("values", [])
        except TripletexError:
            div_values = []

        if not div_values:
            # Try to create a division
            try:
                div_resp = client.post("/division", {"name": "Hovedkontor", "organizationNumber": ""})
                div_id = div_resp["value"]["id"]
                logger.info(f"Auto-created division 'Hovedkontor' (ID={div_id})")
            except TripletexError:
                logger.warning("No divisions found and cannot create one — salary division fix failed")
                return False
        else:
            div_id = div_values[0]["id"]

        # Find the employee's employment record
        emps = client.get("/employee/employment", params={
            "employeeId": emp_id,
            "fields": "id,version,division",
            "count": 1,
        })
        emp_records = (emps or {}).get("values", [])
        if not emp_records:
            logger.warning(f"No employment record found for employee {emp_id}")
            return False

        employment = emp_records[0]
        # Update employment with division
        logger.info(f"Auto-linking employment {employment['id']} to division {div_id}")
        client.put(f"/employee/employment/{employment['id']}", body={
            "id": employment["id"],
            "version": employment.get("version", 0),
            "division": {"id": div_id},
        })
        return True
    except TripletexError as e:
        logger.warning(f"Auto-fix salary division failed: {e}")
        return False


def _auto_fix_invoice_bank(client: TripletexClient, args: dict) -> Any | None:
    """Fix invoice 422 'bankkontonummer' by setting up company bank account."""
    if "bank_account" in client._auto_fix_attempted:
        # Already tried once — raise STOP to prevent infinite retries
        raise TripletexError(
            422,
            "BLOCKED: Invoice creation requires a company bank account, but setup already "
            "failed. Do NOT retry invoice creation — it will always fail. "
            "Complete any other parts of the task instead.",
        )
    client._auto_fix_attempted.add("bank_account")
    logger.info("Invoice blocked by missing bank account — attempting auto-setup")
    try:
        ent = client.get("/employee/entitlement", params={"fields": "id,customer", "count": 1})
        company_id = ent["values"][0]["customer"]["id"]
        company = client.get(f"/company/{company_id}", params={"fields": "id,version"})
        version = company["value"]["version"]
        client.put(f"/company/{company_id}", body={
            "id": company_id,
            "version": version,
            "bankAccountNumber": "12345678903",
        })
        logger.info("Bank account set — retrying invoice creation")
        from datetime import date as _date, timedelta as _td
        invoice_date = args["invoiceDate"]
        due_date = args.get("invoiceDueDate") or (
            _date.fromisoformat(invoice_date) + _td(days=30)
        ).isoformat()
        return client.post("/invoice", {
            "invoiceDate": invoice_date,
            "invoiceDueDate": due_date,
            "orders": [{"id": args["order_id"]}],
        })
    except TripletexError as bank_exc:
        logger.warning(f"Bank account auto-setup failed: {bank_exc}")
        # Return a clear STOP message so the model doesn't waste iterations retrying
        raise TripletexError(
            422,
            "BLOCKED: Invoice creation requires a company bank account, but bank account "
            "setup is blocked by the proxy (405). Do NOT retry invoice creation — it will "
            "always fail. Complete any other parts of the task instead.",
        )


# ── Tool executor ──────────────────────────────────────────────────────────────

def execute_tool(client: TripletexClient, name: str, args: dict) -> dict:
    """
    Execute a Gemini function call against the Tripletex API.
    Always returns a dict — errors become {"success": False, "error": "..."} so
    Gemini can read the message and self-correct in one retry.
    """
    logger.info(
        f"Tool call: {name}",
        extra={"tool": name, "tool_args": json.dumps(args, default=str)[:300]},
    )
    try:
        result = _dispatch(client, name, args)
        logger.info(f"Tool {name} succeeded")
        return {"success": True, "data": result}
    except TripletexError as exc:
        logger.warning(f"Tool {name} → Tripletex error: {exc.to_agent_message()}")
        msg = exc.to_agent_message()
        # Make 403 errors unmistakable — model must stop immediately
        if exc.status_code == 403:
            msg = (
                "FATAL: Session token expired or invalid (403 Forbidden). "
                "ALL further API calls will fail. STOP IMMEDIATELY — do not make "
                "any more tool calls. The task cannot be completed."
            )
        return {"success": False, "error": msg}
    except Exception as exc:
        logger.error(f"Tool {name} → unexpected error: {exc}", exc_info=True)
        return {"success": False, "error": str(exc)}


def _none_stripped(d: dict) -> dict:
    """Remove keys whose value is None."""
    return {k: v for k, v in d.items() if v is not None}


def _dispatch(client: TripletexClient, name: str, args: dict) -> Any:  # noqa: C901
    match name:

        # ── Employees ─────────────────────────────────────────────────────────

        case "tripletex_list_employees":
            return client.get("/employee", params={
                "name": args.get("name"),
                "fields": args.get("fields", "id,firstName,lastName,email,phoneNumberMobile"),
                "count": 10,
            })

        case "tripletex_get_employee":
            return client.get(f"/employee/{args['id']}", params={
                "fields": args.get("fields", "id,firstName,lastName,email,phoneNumberMobile,version"),
            })

        case "tripletex_create_employee":
            # Auto-create department if none provided (Tripletex requires it)
            dept_id = args.get("departmentId")
            if not dept_id:
                try:
                    dept_resp = client.post("/department", {"name": "Generell"})
                    dept_id = dept_resp["value"]["id"]
                    logger.info(f"Auto-created department 'Generell' (ID={dept_id})")
                except TripletexError:
                    # Department might already exist — try to find it
                    try:
                        depts = client.get("/department", params={"fields": "id,name", "count": 5})
                        values = (depts or {}).get("values", [])
                        if values:
                            dept_id = values[0]["id"]
                    except TripletexError:
                        pass
            emp_body = _none_stripped({
                "firstName": args.get("firstName"),
                "lastName": args.get("lastName"),
                "email": args.get("email"),
                "userType": args.get("userTypeId", 1),
                "department": {"id": dept_id} if dept_id else None,
                "employeeNumber": args.get("employeeNumber"),
                "phoneNumberMobile": args.get("phoneNumberMobile"),
                "phoneNumberHome": args.get("phoneNumberHome"),
                "phoneNumberWork": args.get("phoneNumberWork"),
                "dateOfBirth": args.get("dateOfBirth"),
                "nationalIdentityNumber": args.get("nationalIdentityNumber"),
                "bankAccountNumber": args.get("bankAccountNumber"),
            })
            try:
                return client.post("/employee", emp_body)
            except TripletexError as exc:
                if exc.status_code == 422:
                    fix = _auto_fix_employee_email(client, args, exc)
                    if fix is not None:
                        return fix
                raise

        case "tripletex_grant_entitlement":
            # Note: field is "entitlementId" (integer), NOT "entitlement": {object}
            ent_body = {
                "employee": {"id": args["employee_id"]},
                "entitlementId": args["entitlement_id"],
                "customer": {"id": 0},  # 0 = current company (required by API)
            }
            try:
                return client.post("/employee/entitlement", ent_body)
            except TripletexError as exc:
                # If project manager (10) needs "create project" (45) first, auto-grant it
                if exc.status_code == 422 and any(
                    "opprette nye prosjekter" in (vm.get("message") or "").lower()
                    for vm in exc.validation_messages
                ):
                    logger.info("Auto-granting 'create project' entitlement (45) before project manager (10)")
                    try:
                        client.post("/employee/entitlement", {
                            "employee": {"id": args["employee_id"]},
                            "entitlementId": 45,
                            "customer": {"id": 0},
                        })
                    except TripletexError:
                        pass  # May already be granted
                    return client.post("/employee/entitlement", ent_body)
                raise

        case "tripletex_update_employee":
            body = _none_stripped({
                "id": args["id"],
                "version": args["version"],
                "firstName": args.get("firstName"),
                "lastName": args.get("lastName"),
                "email": args.get("email"),
                "phoneNumberMobile": args.get("phoneNumberMobile"),
                "phoneNumberHome": args.get("phoneNumberHome"),
                "phoneNumberWork": args.get("phoneNumberWork"),
                "dateOfBirth": args.get("dateOfBirth"),
                "nationalIdentityNumber": args.get("nationalIdentityNumber"),
                "bankAccountNumber": args.get("bankAccountNumber"),
            })
            if args.get("departmentId"):
                body["department"] = {"id": args["departmentId"]}
            return client.put(f"/employee/{args['id']}", body)

        # ── Suppliers ─────────────────────────────────────────────────────────

        case "tripletex_create_supplier":
            return client.post("/supplier", _none_stripped({
                "name": args.get("name"),
                "email": args.get("email"),
                "organizationNumber": args.get("organizationNumber"),
                "phoneNumber": args.get("phoneNumber"),
                "isSupplier": True,
            }))

        case "tripletex_create_supplier_invoice":
            currency_code = args.get("currency_code") or "NOK"
            invoice_date = args.get("invoiceDate")
            body = _none_stripped({
                "invoiceDate": invoice_date,
                # NOTE: voucherDate does NOT exist on supplierInvoice — never send it
                "supplier": {"id": args["supplier_id"]},
                "amountCurrency": args.get("amountCurrency"),
                # Omit currency for NOK (Tripletex default) — sending it causes 500.
                # For foreign currencies, include with the exchange rate factor.
                "currency": {"code": currency_code, "factor": 1} if currency_code != "NOK" else None,
                "invoiceNumber": args.get("invoiceNumber"),
                "kid": args.get("kid"),
            })
            try:
                return client.post("/supplierInvoice", body)
            except TripletexError as exc:
                if exc.status_code >= 500:
                    # Endpoint is permanently broken — fail fast, no retries.
                    # Guide model to use manual voucher as fallback.
                    logger.warning("Supplier invoice 500 — returning voucher fallback guidance")
                    return {
                        "success": False,
                        "error": (
                            "supplierInvoice endpoint returned 500 (known issue). "
                            "FALLBACK: Create a manual voucher instead with tripletex_create_voucher: "
                            "debit the appropriate expense/asset account (e.g. 4300 for goods, "
                            "6300 for services, 1200 for assets), credit 2400 (leverandørgjeld/AP). "
                            "Amount should include VAT. Add supplier info in the description."
                        ),
                    }
                raise

        # ── Customers ─────────────────────────────────────────────────────────

        case "tripletex_list_customers":
            return client.get("/customer", params={
                "name": args.get("name"),
                "fields": args.get("fields", "id,name,email,phoneNumber"),
                "count": 10,
            })

        case "tripletex_create_customer":
            body: dict = _none_stripped({
                "name": args.get("name"),
                "email": args.get("email"),
                "organizationNumber": args.get("organizationNumber"),
                "phoneNumber": args.get("phoneNumber"),
                "description": args.get("description"),
                "isCustomer": True,
            })
            if args.get("address") or args.get("postalCode") or args.get("city"):
                body["deliveryAddress"] = _none_stripped({
                    "addressLine1": args.get("address"),
                    "postalCode": args.get("postalCode"),
                    "city": args.get("city"),
                })
            return client.post("/customer", body)

        case "tripletex_update_customer":
            return client.put(f"/customer/{args['id']}", _none_stripped({
                "id": args["id"],
                "version": args["version"],
                "name": args.get("name"),
                "email": args.get("email"),
                "phoneNumber": args.get("phoneNumber"),
                "description": args.get("description"),
            }))

        # ── Products ──────────────────────────────────────────────────────────

        case "tripletex_list_products":
            return client.get("/product", params={
                "name": args.get("name"),
                "fields": args.get("fields", "id,name,number,priceExcludingVatCurrency"),
                "count": 10,
            })

        case "tripletex_create_product":
            try:
                return client.post("/product", _none_stripped({
                    "name": args.get("name"),
                    "number": args.get("number"),
                    "priceExcludingVatCurrency": args.get("priceExcludingVatCurrency"),
                    "costExcludingVatCurrency": args.get("costExcludingVatCurrency"),
                    "vatType": {"id": args["vatTypeId"]} if args.get("vatTypeId") else None,
                }))
            except TripletexError as exc:
                if exc.status_code == 422:
                    msg = (exc.message or "").lower()
                    # Check validation messages too (number conflicts appear there)
                    vm_text = " ".join(
                        (vm.get("message") or "") for vm in exc.validation_messages
                    ).lower()
                    all_text = msg + " " + vm_text
                    if any(kw in all_text for kw in ("allerede", "already", "i bruk", "in use")):
                        fix = _auto_fix_product_exists(client, args)
                        if fix is not None:
                            return fix
                raise

        # ── Orders ────────────────────────────────────────────────────────────

        case "tripletex_create_order":
            order_lines = []
            for line in args.get("orderLines") or []:
                ol = _none_stripped({
                    "product": {"id": line["product_id"]} if line.get("product_id") else None,
                    "description": line.get("description"),
                    "count": line.get("count", 1),
                    "unitPriceExcludingVatCurrency": line.get("unitPriceExcludingVat"),
                    "vatType": {"id": line["vatTypeId"]} if line.get("vatTypeId") else None,
                })
                order_lines.append(ol)
            # Default deliveryDate to orderDate — Tripletex requires it for project orders
            delivery_date = args.get("deliveryDate") or args.get("orderDate")
            body = _none_stripped({
                "customer": {"id": args["customer_id"]},
                "orderDate": args["orderDate"],
                "deliveryDate": delivery_date,
                "currency": {"id": args["currency_id"]} if args.get("currency_id") else None,
                "orderLines": order_lines or None,
            })
            return client.post("/order", body)

        case "tripletex_get_order":
            return client.get(f"/order/{args['id']}", params={
                "fields": args.get("fields", "id,customer,orderLines,orderDate,version"),
            })

        # ── Invoices ──────────────────────────────────────────────────────────

        case "tripletex_create_invoice":
            from datetime import date as _date, timedelta as _td
            invoice_date = args["invoiceDate"]
            due_date = args.get("invoiceDueDate") or (
                _date.fromisoformat(invoice_date) + _td(days=30)
            ).isoformat()
            inv_body = {
                "invoiceDate": invoice_date,
                "invoiceDueDate": due_date,
                "orders": [{"id": args["order_id"]}],
            }
            try:
                return client.post("/invoice", inv_body)
            except TripletexError as exc:
                if exc.status_code == 422 and "bankkontonummer" in (exc.message or "").lower():
                    fix = _auto_fix_invoice_bank(client, args)
                    if fix is not None:
                        return fix
                raise

        case "tripletex_list_invoices":
            fields = args.get("fields", "id,invoiceDate,invoiceDueDate,amountCurrency,customer,amount,invoiceNumber")
            # Strip invalid fields that cause 400
            bad_fields = {"dueDate", "isPaid", "amountOutstanding", "amountOutstandingCurrency"}
            fields = ",".join(f for f in fields.split(",") if f.strip() not in bad_fields)
            return client.get("/invoice", params=_none_stripped({
                "invoiceDateFrom": args.get("invoiceDateFrom", "2020-01-01"),
                "invoiceDateTo": args.get("invoiceDateTo", "2030-12-31"),
                "customerId": args.get("customerId"),
                "fields": fields,
                "count": args.get("count", 10),
            }))

        case "tripletex_send_invoice":
            params: dict = {"sendType": args["sendType"]}
            if args.get("recipientEmail"):
                params["overrideEmailAddress"] = args["recipientEmail"]
            return client.put(f"/invoice/{args['invoice_id']}/:send", params=params)

        case "tripletex_create_credit_note":
            params = {"date": args["date"]}
            if args.get("comment"):
                params["comment"] = args["comment"]
            return client.put(
                f"/invoice/{args['invoice_id']}/:createCreditNote",
                params=params,
            )

        # ── Payments ──────────────────────────────────────────────────────────

        case "tripletex_register_payment":
            if not args.get("paymentDate") or not args.get("amount"):
                return {"success": False, "error": "Required: paymentDate (YYYY-MM-DD) and amount (number). paymentTypeId defaults to 1."}
            # Pre-fetch payment type to avoid 404 + retry overhead
            payment_type_id = args.get("paymentTypeId")
            if not payment_type_id:
                cached_id = getattr(client, "_cached_payment_type_id", None)
                if cached_id:
                    payment_type_id = cached_id
                else:
                    try:
                        pt = client.get("/invoice/paymentType", params={"fields": "id", "count": 1})
                        values = (pt or {}).get("values", [])
                        if values:
                            payment_type_id = values[0]["id"]
                            client._cached_payment_type_id = payment_type_id
                    except TripletexError:
                        pass
                if not payment_type_id:
                    payment_type_id = 1
            # NOTE: /:payment is an action endpoint — uses query params, NOT JSON body
            try:
                return client.put(
                    f"/invoice/{args['invoice_id']}/:payment",
                    params={
                        "paymentDate": args["paymentDate"],
                        "paidAmount": args["amount"],
                        "paymentTypeId": payment_type_id,
                    },
                )
            except TripletexError as exc:
                if exc.status_code == 404:
                    fix = _auto_fix_payment_404(client, args)
                    if fix is not None:
                        return fix
                raise

        # ── Travel Expenses ───────────────────────────────────────────────────

        case "tripletex_list_travel_expenses":
            return client.get("/travelExpense", params={
                "fields": args.get("fields", "id,employee,status"),
                "count": args.get("count", 10),
            })

        case "tripletex_create_travel_expense":
            return client.post("/travelExpense", {
                "employee": {"id": args["employee_id"]},
            })

        case "tripletex_delete_travel_expense":
            client.delete(f"/travelExpense/{args['id']}")
            return {"deleted": True, "id": args["id"]}

        # ── Projects ──────────────────────────────────────────────────────────

        case "tripletex_list_projects":
            return client.get("/project", params={
                "name": args.get("name"),
                "fields": args.get("fields", "id,name,number,customer"),
                "count": 10,
            })

        case "tripletex_create_project":
            project_body = _none_stripped({
                "name": args.get("name"),
                "number": args.get("number"),
                "customer": {"id": args["customer_id"]} if args.get("customer_id") else None,
                "startDate": args.get("startDate"),
                "endDate": args.get("endDate"),
                "description": args.get("description"),
                "projectManager": {"id": args["projectManagerId"]} if args.get("projectManagerId") else None,
            })
            try:
                return client.post("/project", project_body)
            except TripletexError as exc:
                if exc.status_code == 422 and any(
                    "prosjektleder" in (vm.get("message") or vm.get("field") or "").lower()
                    for vm in exc.validation_messages
                ):
                    pm_id = _auto_create_project_manager(client)
                    if pm_id:
                        project_body["projectManager"] = {"id": pm_id}
                        return client.post("/project", project_body)
                raise

        # ── Activities ────────────────────────────────────────────────────────

        case "tripletex_list_activities":
            return client.get("/activity", params=_none_stripped({
                "name": args.get("name"),
                "fields": args.get("fields", "id,name,isChargeable,isGeneral,description"),
                "count": args.get("count", 10),
            }))

        case "tripletex_create_activity":
            try:
                return client.post("/activity", _none_stripped({
                    "name": args.get("name"),
                    "description": args.get("description"),
                    "isGeneral": args.get("isGeneral", False),
                    "isChargeable": args.get("isChargeable"),
                    "activityType": 1,
                }))
            except TripletexError as exc:
                if exc.status_code == 422:
                    has_name_error = any(
                        "navn" in (vm.get("message") or "").lower()
                        for vm in exc.validation_messages
                    )
                    if has_name_error:
                        fix = _auto_fix_activity_name(client, args)
                        if fix is not None:
                            return fix
                raise

        case "tripletex_link_activity_to_project":
            return client.post("/project/projectActivity", {
                "project": {"id": args["project_id"]},
                "activity": {"id": args["activity_id"]},
            })

        # ── Departments ───────────────────────────────────────────────────────

        case "tripletex_list_departments":
            fields = args.get("fields", "id,name,departmentNumber")
            # Strip invalid fields that cause 400
            bad_fields = {"division"}
            fields = ",".join(f for f in fields.split(",") if f.strip() not in bad_fields)
            return client.get("/department", params={
                "fields": fields,
                "count": 10,
            })

        case "tripletex_create_department":
            return client.post("/department", _none_stripped({
                "name": args.get("name"),
                "departmentNumber": args.get("departmentNumber"),
                "departmentManager": (
                    {"id": args["departmentManagerId"]}
                    if args.get("departmentManagerId") else None
                ),
            }))

        # ── Ledger ────────────────────────────────────────────────────────────

        case "tripletex_list_accounts":
            # The Tripletex API 'number' param does EXACT matching only.
            # To support prefix/range searches, we fetch all accounts and
            # filter in Python.
            result = client.get("/ledger/account", params={
                "fields": args.get("fields", "id,number,name"),
                "count": 1000,
            })
            number_filter = args.get("number")
            if number_filter and result and "values" in result:
                prefix = str(number_filter)
                result["values"] = [
                    a for a in result["values"]
                    if str(a.get("number", "")).startswith(prefix)
                ]
            return result

        case "tripletex_list_vouchers":
            fields = args.get("fields", "id,date,description,postings")
            # Strip invalid VoucherDTO fields that cause 400
            bad_voucher_fields = {"amount", "accountId", "account"}
            parts = [f.strip() for f in fields.split(",")]
            # Deduplicate fields (duplicate "number" causes 400)
            seen = set()
            clean_parts = []
            for f in parts:
                base = f.split("(")[0].strip()  # handle nested like postings(...)
                if base not in bad_voucher_fields and base not in seen:
                    seen.add(base)
                    clean_parts.append(f)
            fields = ",".join(clean_parts)
            return client.get("/ledger/voucher", params={
                "dateFrom": args.get("dateFrom"),
                "dateTo": args.get("dateTo"),
                "fields": fields,
                "count": args.get("count", 100),
            })

        case "tripletex_create_voucher":
            postings = []
            for i, p in enumerate(args.get("postings") or [], start=1):
                postings.append(_none_stripped({
                    "row": i,  # 1-indexed; row 0 is system-reserved and causes 422
                    "account": {"id": p["account_id"]},
                    "amount": p.get("amount"),
                    "description": p.get("description"),
                    "vatType": {"id": p["vatType_id"]} if p.get("vatType_id") else None,
                    "department": {"id": p["department_id"]} if p.get("department_id") else None,
                    "customer": {"id": p["customer_id"]} if p.get("customer_id") else None,
                    "supplier": {"id": p["supplier_id"]} if p.get("supplier_id") else None,
                    "employee": {"id": p["employee_id"]} if p.get("employee_id") else None,
                }))
            body = {"date": args["date"], "postings": postings}
            if args.get("description"):
                body["description"] = args["description"]
            return client.post("/ledger/voucher", body)

        case "tripletex_list_postings":
            fields = args.get("fields", "id,date,description,amount,account")
            # Fix common model mistakes in fields
            fields = fields.replace("accountId", "account")
            fields = fields.replace("account.number", "account")
            return client.get("/ledger/posting", params={
                "dateFrom": args.get("dateFrom"),
                "dateTo": args.get("dateTo"),
                "fields": fields,
                "count": 1000,
            })

        # ── Generic escape hatch ──────────────────────────────────────────────

        case "tripletex_api_call":
            method = args["method"].upper()
            path = args["path"]
            params = args.get("params") or {}
            body = args.get("body") or {}

            # Redirect /company/division → /division (model keeps using wrong path)
            if path.rstrip("/") in ("/company/division", "/company/divisions"):
                path = "/division"

            # BLOCK POST /ledger/account — accounts are pre-populated in the
            # chart of accounts and must NEVER be created via the API.
            # NOTE: exact match to avoid blocking /ledger/accountingDimensionName etc.
            if method == "POST" and path.rstrip("/") == "/ledger/account":
                raise TripletexError(
                    422,
                    "STOP: You must NEVER create ledger accounts. The chart of accounts is "
                    "pre-populated in every Tripletex sandbox. Use tripletex_list_accounts "
                    "to find existing accounts. Search without a number filter to see all "
                    "accounts, or use a 2-digit prefix like number=65 to find all 65xx accounts.",
                )

            # Intercept POST /ledger/voucher — auto-add row numbers to prevent
            # the "guiRow 0" 422 error that occurs when rows are missing.
            if method == "POST" and path.rstrip("/") == "/ledger/voucher":
                postings = body.get("postings") or []
                for i, p in enumerate(postings, start=1):
                    if not p.get("row"):
                        p["row"] = i
                body["postings"] = postings

            # Intercept POST /employee/employment — strip bad fields + auto-fix prerequisites
            if method == "POST" and path.rstrip("/") == "/employee/employment":
                # Strip model's potentially bad values for these fields
                for bad_field in ("percentageOfFullTimeEquivalent",
                                  "positionPercentage", "positionCode", "annualSalary"):
                    body.pop(bad_field, None)
                # Auto-set dateOfBirth if missing (required for employment creation)
                emp_ref = body.get("employee") or {}
                emp_id = emp_ref.get("id")
                if emp_id:
                    try:
                        emp_data = client.get(f"/employee/{emp_id}", params={"fields": "id,dateOfBirth,version"})
                        emp_val = (emp_data or {}).get("value", {})
                        if not emp_val.get("dateOfBirth"):
                            logger.info(f"Auto-setting dateOfBirth for employee {emp_id}")
                            client.put(f"/employee/{emp_id}", body={
                                "id": emp_id,
                                "version": emp_val.get("version", 0),
                                "dateOfBirth": "1990-01-01",
                            })
                    except TripletexError as e:
                        logger.warning(f"Failed to auto-set dateOfBirth: {e}")
                # Auto-discover and set division (required for salary transactions later)
                if not body.get("division"):
                    body.pop("division", None)  # Remove empty/None division
                    try:
                        divs = client.get("/division", params={"fields": "id,name", "count": 1})
                        div_values = (divs or {}).get("values", [])
                        if div_values:
                            body["division"] = {"id": div_values[0]["id"]}
                            logger.info(f"Auto-linked employment to division {div_values[0]['id']}")
                        else:
                            # No divisions exist — create one
                            try:
                                div_resp = client.post("/division", {"name": "Hovedkontor", "organizationNumber": ""})
                                div_id = div_resp["value"]["id"]
                                body["division"] = {"id": div_id}
                                logger.info(f"Auto-created division 'Hovedkontor' (ID={div_id}) for employment")
                            except TripletexError:
                                logger.warning("Could not create division for employment — salary may fail later")
                    except TripletexError:
                        logger.warning("Could not find division for employment — salary may fail later")

            # Intercept PUT /employee/employment/{id} — strip invalid fields
            if method == "PUT" and "/employee/employment/" in path and "/details" not in path:
                for bad_field in ("department", "division", "percentageOfFullTimeEquivalent",
                                  "positionPercentage", "positionCode", "annualSalary"):
                    body.pop(bad_field, None)

            # Intercept POST/PUT /employee/employment/details — auto-rename
            # positionPercentage → percentageOfFullTimeEquivalent
            if "/employment/details" in path:
                if "positionPercentage" in body and "percentageOfFullTimeEquivalent" not in body:
                    body["percentageOfFullTimeEquivalent"] = body.pop("positionPercentage")
                # Strip fields that don't belong on details endpoint
                for bad_field in ("annualSalary", "positionCode", "hoursPerDay",
                                  "department", "division"):
                    body.pop(bad_field, None)
                # Fix occupationCode — must be object reference, not raw value
                occ = body.get("occupationCode")
                if occ is not None and not isinstance(occ, dict):
                    body.pop("occupationCode", None)  # Strip invalid type

            # Intercept POST /salary/transaction — auto-fix division on employment
            if method == "POST" and path.rstrip("/") == "/salary/transaction":
                # Auto-add count:1.0 to salary specifications if missing
                for payslip in (body.get("payslips") or []):
                    for spec in (payslip.get("specifications") or []):
                        if spec.get("count") is None:
                            spec["count"] = 1.0

            match method:
                case "GET":
                    return client.get(path, params=params)
                case "POST":
                    try:
                        return client.post(path, body)
                    except TripletexError as exc:
                        # Auto-fix salary division error by patching employment
                        if (path.rstrip("/") == "/salary/transaction"
                                and exc.status_code == 422
                                and any("virksomhet" in (vm.get("message") or "").lower()
                                        for vm in exc.validation_messages)):
                            fix = _auto_fix_salary_division(client, body)
                            if fix:
                                return client.post(path, body)
                        raise
                case "PUT":
                    # Intercept PUT /employee/employment/details too
                    if "/employment/details" in path:
                        if "positionPercentage" in (body or {}) and "percentageOfFullTimeEquivalent" not in (body or {}):
                            body["percentageOfFullTimeEquivalent"] = body.pop("positionPercentage")
                    return client.put(path, body=body or None, params=params or None)
                case "DELETE":
                    client.delete(path)
                    return {"deleted": True}
                case _:
                    raise ValueError(f"Unsupported HTTP method: {method}")

        case _:
            raise ValueError(f"Unknown tool: {name}")
