"""
Tool declarations and dispatch for the Tripletex v2 API.

Two exports:
  CLAUDE_TOOLS   — list of tool dicts for Claude messages API
  execute_tool() — dispatches a tool call to the TripletexClient

Design rules:
  • Every tool returns {"success": True, "data": ...} or {"success": False, "error": "..."}
  • Errors are returned AS TOOL RESULTS so the model can self-correct
  • No blind retries here — the model decides whether and how to retry
  • A generic escape-hatch tool (tripletex_api_call) handles anything not
    explicitly mapped, including enabling modules and setting employee roles
"""
import json
import logging
from typing import Any

from .tripletex.client import TripletexClient
from .tripletex.errors import TripletexError

logger = logging.getLogger(__name__)


# ── Tool declarations (Claude JSON Schema format) ────────────────────────────

CLAUDE_TOOLS = [

    # ── Employees ─────────────────────────────────────────────────────────────

    {"name": "tripletex_list_employees",
     "description": "List employees. Use to find an employee by name before updating or deleting.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Filter by full or partial name"},
         "fields": {"type": "string", "description": "Comma-separated fields, e.g. 'id,firstName,lastName,email'"},
     }}},

    {"name": "tripletex_get_employee",
     "description": "Get a single employee by ID. Use when you need the version number for a PUT.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "Employee ID"},
         "fields": {"type": "string", "description": "Fields to return"},
     }, "required": ["id"]}},

    {"name": "tripletex_create_employee",
     "description": (
         "Create a new employee. "
         "IMPORTANT: If the task says the employee should be 'kontoadministrator' or "
         "'administrator', after creating call tripletex_grant_entitlement to grant the role "
         "(first GET /employee/entitlement to find the entitlement ID)."
     ),
     "input_schema": {"type": "object", "properties": {
         "firstName": {"type": "string", "description": "First name"},
         "lastName": {"type": "string", "description": "Last name"},
         "email": {"type": "string", "description": "Email address"},
         "userTypeId": {"type": "integer", "description": "User type ID. Default 1 (standard employee)."},
         "departmentId": {"type": "integer", "description": "Department ID — optional"},
         "employeeNumber": {"type": "string", "description": "Employee number (optional)"},
         "phoneNumberMobile": {"type": "string", "description": "Mobile phone number"},
         "phoneNumberHome": {"type": "string", "description": "Home phone number"},
         "phoneNumberWork": {"type": "string", "description": "Work phone number"},
         "dateOfBirth": {"type": "string", "description": "Date of birth (YYYY-MM-DD)"},
         "nationalIdentityNumber": {"type": "string", "description": "Norwegian national identity number / personnummer"},
         "bankAccountNumber": {"type": "string", "description": "Employee's personal bank account number"},
     }, "required": ["firstName", "lastName"]}},

    {"name": "tripletex_grant_entitlement",
     "description": (
         "Grant a role or access entitlement to an employee. "
         "Step 1: call tripletex_api_call GET /employee/entitlement to list available entitlement IDs. "
         "Step 2: call this tool with the employee_id and the correct entitlement_id. "
         "IMPORTANT: For project manager — grant the entitlement BEFORE creating the project."
     ),
     "input_schema": {"type": "object", "properties": {
         "employee_id": {"type": "integer", "description": "Employee ID"},
         "entitlement_id": {"type": "integer", "description": "Entitlement ID (discover via GET /employee/entitlement)"},
     }, "required": ["employee_id", "entitlement_id"]}},

    {"name": "tripletex_update_employee",
     "description": (
         "Update an existing employee. Requires version number — GET the employee first. "
         "INVALID fields (cause 422): annualSalary, salary, wage."
     ),
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "Employee ID"},
         "version": {"type": "integer", "description": "Current version (optimistic locking)"},
         "firstName": {"type": "string", "description": "First name"},
         "lastName": {"type": "string", "description": "Last name"},
         "email": {"type": "string", "description": "Email address"},
         "phoneNumberMobile": {"type": "string", "description": "Mobile phone"},
         "phoneNumberHome": {"type": "string", "description": "Home phone"},
         "phoneNumberWork": {"type": "string", "description": "Work phone"},
         "dateOfBirth": {"type": "string", "description": "Date of birth (YYYY-MM-DD)"},
         "nationalIdentityNumber": {"type": "string", "description": "Norwegian national identity number"},
         "bankAccountNumber": {"type": "string", "description": "Bank account number"},
     }, "required": ["id", "version"]}},

    # ── Suppliers ─────────────────────────────────────────────────────────────

    {"name": "tripletex_create_supplier",
     "description": "Create a new SUPPLIER (leverandør / fornecedor / fournisseur / Lieferant). Do NOT use tripletex_create_customer for suppliers.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Supplier / company name"},
         "email": {"type": "string", "description": "Email address"},
         "organizationNumber": {"type": "string", "description": "Norwegian org. number (9 digits)"},
         "phoneNumber": {"type": "string", "description": "Phone number"},
     }, "required": ["name"]}},

    {"name": "tripletex_create_supplier_invoice",
     "description": (
         "Register an incoming supplier invoice (leverandørfaktura / inngående faktura). "
         "Creates the invoice and auto-generates AP accounting entries."
     ),
     "input_schema": {"type": "object", "properties": {
         "supplier_id": {"type": "integer", "description": "Supplier ID"},
         "invoiceDate": {"type": "string", "description": "Invoice date (YYYY-MM-DD)"},
         "amountCurrency": {"type": "number", "description": "Total amount INCLUDING VAT"},
         "currency_code": {"type": "string", "description": "Currency code (e.g. 'EUR'). Omit for NOK."},
         "invoiceNumber": {"type": "string", "description": "Supplier's invoice number — optional"},
         "kid": {"type": "string", "description": "Payment reference / KID — optional"},
     }, "required": ["supplier_id", "invoiceDate", "amountCurrency"]}},

    # ── Customers ─────────────────────────────────────────────────────────────

    {"name": "tripletex_list_customers",
     "description": "List customers. Use to find an existing customer by name.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Filter by name (partial match)"},
         "fields": {"type": "string", "description": "Fields to return, e.g. 'id,name,email'"},
     }}},

    {"name": "tripletex_create_customer",
     "description": "Create a new customer (company or individual).",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Customer / company name"},
         "email": {"type": "string", "description": "Email address"},
         "organizationNumber": {"type": "string", "description": "Norwegian org. number (9 digits)"},
         "phoneNumber": {"type": "string", "description": "Phone number"},
         "address": {"type": "string", "description": "Street address line"},
         "postalCode": {"type": "string", "description": "Postal code"},
         "city": {"type": "string", "description": "City"},
         "description": {"type": "string", "description": "Customer description / notes"},
     }, "required": ["name"]}},

    {"name": "tripletex_update_customer",
     "description": "Update an existing customer. Requires version from a prior GET.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "Customer ID"},
         "version": {"type": "integer", "description": "Current version number"},
         "name": {"type": "string", "description": "Customer name"},
         "email": {"type": "string", "description": "Email"},
         "phoneNumber": {"type": "string", "description": "Phone number"},
         "address": {"type": "string", "description": "Street address"},
         "description": {"type": "string", "description": "Customer description / notes"},
     }, "required": ["id", "version"]}},

    # ── Products ──────────────────────────────────────────────────────────────

    {"name": "tripletex_list_products",
     "description": "List products.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Filter by name"},
         "fields": {"type": "string", "description": "Fields to return"},
     }}},

    {"name": "tripletex_create_product",
     "description": "Create a new product. Only set 'number' if the task explicitly provides one.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Product name"},
         "number": {"type": "string", "description": "Product number — only if task provides one"},
         "priceExcludingVatCurrency": {"type": "number", "description": "Sales price excluding VAT"},
         "costExcludingVatCurrency": {"type": "number", "description": "Cost price excluding VAT"},
         "vatTypeId": {"type": "integer", "description": "VAT type ID. 3 = 25% Norwegian VAT."},
     }, "required": ["name"]}},

    # ── Orders ────────────────────────────────────────────────────────────────

    {"name": "tripletex_create_order",
     "description": "Create an order for a customer. An order is REQUIRED before creating an invoice.",
     "input_schema": {"type": "object", "properties": {
         "customer_id": {"type": "integer", "description": "Customer ID"},
         "orderDate": {"type": "string", "description": "Order date (YYYY-MM-DD)"},
         "deliveryDate": {"type": "string", "description": "Delivery date (YYYY-MM-DD) — optional"},
         "currency_id": {"type": "integer", "description": "Currency ID for foreign currency orders. Omit for NOK."},
         "orderLines": {"type": "array", "description": "Order lines", "items": {
             "type": "object", "properties": {
                 "product_id": {"type": "integer", "description": "Product ID — omit for free-text lines"},
                 "description": {"type": "string", "description": "Line item description"},
                 "count": {"type": "number", "description": "Quantity"},
                 "unitPriceExcludingVat": {"type": "number", "description": "Unit price excluding VAT"},
                 "vatTypeId": {"type": "integer", "description": "VAT type ID (3 = 25%)"},
             }}},
     }, "required": ["customer_id", "orderDate"]}},

    {"name": "tripletex_get_order",
     "description": "Get order details by ID.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "Order ID"},
         "fields": {"type": "string", "description": "Fields to return"},
     }, "required": ["id"]}},

    # ── Invoices ──────────────────────────────────────────────────────────────

    {"name": "tripletex_create_invoice",
     "description": "Create an invoice from an existing order. Chain: customer → product → order → invoice.",
     "input_schema": {"type": "object", "properties": {
         "order_id": {"type": "integer", "description": "Order ID to invoice"},
         "invoiceDate": {"type": "string", "description": "Invoice date (YYYY-MM-DD)"},
         "invoiceDueDate": {"type": "string", "description": "Payment due date (YYYY-MM-DD)"},
     }, "required": ["order_id", "invoiceDate"]}},

    {"name": "tripletex_list_invoices",
     "description": (
         "List invoices. invoiceDateFrom and invoiceDateTo are REQUIRED. "
         "Valid fields: id, invoiceDate, invoiceDueDate, amountCurrency, customer, amount, invoiceNumber."
     ),
     "input_schema": {"type": "object", "properties": {
         "invoiceDateFrom": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
         "invoiceDateTo": {"type": "string", "description": "End date (YYYY-MM-DD)"},
         "customerId": {"type": "integer", "description": "Filter by customer ID — optional"},
         "fields": {"type": "string", "description": "Fields to return"},
         "count": {"type": "integer", "description": "Max results"},
     }}},

    {"name": "tripletex_send_invoice",
     "description": "Send an invoice to the customer.",
     "input_schema": {"type": "object", "properties": {
         "invoice_id": {"type": "integer", "description": "Invoice ID"},
         "sendType": {"type": "string", "enum": ["EMAIL", "EHF", "EFAKTURA", "AVTALEGIRO"], "description": "Send method"},
         "recipientEmail": {"type": "string", "description": "Override recipient email — optional"},
     }, "required": ["invoice_id", "sendType"]}},

    {"name": "tripletex_create_credit_note",
     "description": "Create a credit note (reversal / kreditnota) for an invoice.",
     "input_schema": {"type": "object", "properties": {
         "invoice_id": {"type": "integer", "description": "Invoice ID to reverse"},
         "date": {"type": "string", "description": "Credit note date (YYYY-MM-DD)"},
         "comment": {"type": "string", "description": "Reason — optional"},
     }, "required": ["invoice_id", "date"]}},

    # ── Payments ──────────────────────────────────────────────────────────────

    {"name": "tripletex_register_payment",
     "description": "Register a payment against an invoice.",
     "input_schema": {"type": "object", "properties": {
         "invoice_id": {"type": "integer", "description": "Invoice ID"},
         "paymentDate": {"type": "string", "description": "Payment date (YYYY-MM-DD)"},
         "amount": {"type": "number", "description": "Amount paid (in invoice currency)"},
         "paymentTypeId": {"type": "integer", "description": "Payment type ID. 1 = bank transfer (default)."},
     }, "required": ["invoice_id", "paymentDate", "amount"]}},

    # ── Travel Expenses ───────────────────────────────────────────────────────

    {"name": "tripletex_list_travel_expenses",
     "description": "List travel expense reports.",
     "input_schema": {"type": "object", "properties": {
         "fields": {"type": "string", "description": "Fields to return"},
         "count": {"type": "integer", "description": "Max results"},
     }}},

    {"name": "tripletex_create_travel_expense",
     "description": "Create a travel expense report container. Only employee_id is accepted on creation.",
     "input_schema": {"type": "object", "properties": {
         "employee_id": {"type": "integer", "description": "Employee ID who travelled"},
     }, "required": ["employee_id"]}},

    {"name": "tripletex_delete_travel_expense",
     "description": "Delete a travel expense report by ID.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "Travel expense ID to delete"},
     }, "required": ["id"]}},

    # ── Projects ──────────────────────────────────────────────────────────────

    {"name": "tripletex_list_projects",
     "description": "List projects.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Filter by project name"},
         "fields": {"type": "string", "description": "Fields to return"},
     }}},

    {"name": "tripletex_create_project",
     "description": "Create a new project, optionally linked to a customer.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Project name"},
         "number": {"type": "string", "description": "Project number (optional)"},
         "customer_id": {"type": "integer", "description": "Customer ID to link — optional"},
         "startDate": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
         "endDate": {"type": "string", "description": "End date (YYYY-MM-DD) — optional"},
         "description": {"type": "string", "description": "Project description — optional"},
         "projectManagerId": {"type": "integer", "description": "Employee ID of project manager — optional"},
     }, "required": ["name", "startDate"]}},

    # ── Activities ────────────────────────────────────────────────────────────

    {"name": "tripletex_create_activity",
     "description": "Create an activity (work type for time tracking). Link to project after with tripletex_link_activity_to_project.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Activity name"},
         "description": {"type": "string", "description": "Description — optional"},
         "isGeneral": {"type": "boolean", "description": "Whether this is a general/global activity"},
         "isChargeable": {"type": "boolean", "description": "Whether hours are chargeable to the customer"},
     }, "required": ["name"]}},

    {"name": "tripletex_list_activities",
     "description": "List activities. Check BEFORE creating — Tripletex has default activities. NEVER PUT/update an existing activity (returns 500).",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Filter by name (partial match)"},
         "fields": {"type": "string", "description": "Fields to return"},
         "count": {"type": "integer", "description": "Max results"},
     }}},

    {"name": "tripletex_link_activity_to_project",
     "description": "Link an activity to a project. Uses POST /project/projectActivity.",
     "input_schema": {"type": "object", "properties": {
         "project_id": {"type": "integer", "description": "Project ID"},
         "activity_id": {"type": "integer", "description": "Activity ID"},
     }, "required": ["project_id", "activity_id"]}},

    # ── Departments ───────────────────────────────────────────────────────────

    {"name": "tripletex_list_departments",
     "description": "List departments.",
     "input_schema": {"type": "object", "properties": {
         "fields": {"type": "string", "description": "Fields to return"},
     }}},

    {"name": "tripletex_create_department",
     "description": "Create a new department.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Department name"},
         "departmentNumber": {"type": "string", "description": "Department number — optional"},
         "departmentManagerId": {"type": "integer", "description": "Manager employee ID — optional"},
     }, "required": ["name"]}},

    # ── Ledger ────────────────────────────────────────────────────────────────

    {"name": "tripletex_list_accounts",
     "description": "List chart-of-accounts entries. All accounts are PRE-POPULATED — never create accounts. Supports PREFIX search: number=65 returns all 65xx accounts.",
     "input_schema": {"type": "object", "properties": {
         "number": {"type": "string", "description": "Account number prefix (e.g. '65' for all 65xx). Omit for all."},
         "fields": {"type": "string", "description": "Fields to return, e.g. 'id,number,name'"},
     }}},

    {"name": "tripletex_list_vouchers",
     "description": "List vouchers (bilag). Use flat field names only (e.g. 'id,date,description,postings').",
     "input_schema": {"type": "object", "properties": {
         "dateFrom": {"type": "string", "description": "From date (YYYY-MM-DD)"},
         "dateTo": {"type": "string", "description": "To date (YYYY-MM-DD)"},
         "fields": {"type": "string", "description": "Fields to return (flat only, no nested syntax)"},
         "count": {"type": "integer", "description": "Max results (default 100)"},
     }}},

    {"name": "tripletex_create_voucher",
     "description": (
         "Create a manual voucher with debit/credit postings. ALWAYS use this for vouchers. "
         "FORBIDDEN accounts: 1920, 1900, 2700-2709. "
         "Account 1500 (AR): REQUIRES customer_id. Account 2400 (AP): REQUIRES supplier_id."
     ),
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string", "description": "Voucher date (YYYY-MM-DD)"},
         "description": {"type": "string", "description": "Description"},
         "postings": {"type": "array", "description": "Debit/credit postings. Must balance (sum to zero).", "items": {
             "type": "object", "properties": {
                 "account_id": {"type": "integer", "description": "Ledger account ID"},
                 "amount": {"type": "number", "description": "Amount (positive=debit, negative=credit)"},
                 "description": {"type": "string", "description": "Posting description"},
                 "vatType_id": {"type": "integer", "description": "VAT type ID — set on expense lines"},
                 "department_id": {"type": "integer", "description": "Department ID"},
                 "customer_id": {"type": "integer", "description": "Customer ID — REQUIRED for account 1500"},
                 "supplier_id": {"type": "integer", "description": "Supplier ID — REQUIRED for account 2400"},
                 "employee_id": {"type": "integer", "description": "Employee ID — for employee expense postings"},
             }}},
     }, "required": ["date", "postings"]}},

    {"name": "tripletex_list_postings",
     "description": "List ledger postings.",
     "input_schema": {"type": "object", "properties": {
         "dateFrom": {"type": "string", "description": "From date (YYYY-MM-DD)"},
         "dateTo": {"type": "string", "description": "To date (YYYY-MM-DD)"},
         "fields": {"type": "string", "description": "Fields to return"},
     }}},

    # ── Generic escape hatch ──────────────────────────────────────────────────

    {"name": "tripletex_api_call",
     "description": (
         "Make a custom call to ANY Tripletex v2 endpoint. "
         "Use for: entitlements, salary transactions, timesheet entries, action endpoints like /:remind. "
         "Do NOT use for payment registration — use tripletex_register_payment."
     ),
     "input_schema": {"type": "object", "properties": {
         "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "description": "HTTP method"},
         "path": {"type": "string", "description": "API path starting with /, e.g. /employee/entitlement"},
         "params": {"type": "object", "description": "Query string parameters"},
         "body": {"type": "object", "description": "Request body for POST or PUT"},
     }, "required": ["method", "path"]}},
]


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


def _auto_create_division(client: TripletexClient) -> int | None:
    """Create a division with all required fields. Returns division ID or None."""
    from datetime import date as _date
    try:
        # Find a valid municipality
        municipality_id = None
        try:
            munis = client.get("/municipality", params={"fields": "id,name", "count": 1})
            muni_values = (munis or {}).get("values", [])
            if muni_values:
                municipality_id = muni_values[0]["id"]
        except TripletexError:
            pass

        if not municipality_id:
            municipality_id = 301  # Fallback: Oslo

        today = _date.today().isoformat()
        div_resp = client.post("/division", {
            "name": "Hovedkontor",
            "organizationNumber": "999999999",
            "startDate": today,
            "municipalityDate": today,
            "municipality": {"id": municipality_id},
        })
        div_id = div_resp["value"]["id"]
        logger.info(f"Auto-created division 'Hovedkontor' (ID={div_id})")
        return div_id
    except TripletexError as e:
        logger.warning(f"Failed to auto-create division: {e}")
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
            # Try to create a division with all required fields
            div_id = _auto_create_division(client)
            if not div_id:
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
        raise TripletexError(
            422,
            "STOP: Invoice creation PERMANENTLY blocked — company has no bank account and "
            "it CANNOT be added (proxy blocks PUT /company). Do NOT retry invoice creation. "
            "Do NOT try GET /company, /whoAmI, /employee/loggedInUser, or any other approach "
            "to find/fix the company — NONE will work. Instead: complete ALL other parts of "
            "the task (create customer, product, order, project, etc.) and STOP.",
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
        raise TripletexError(
            422,
            "STOP: Invoice creation PERMANENTLY blocked — company has no bank account and "
            "it CANNOT be added (proxy blocks PUT /company). Do NOT retry invoice creation. "
            "Do NOT try GET /company, /whoAmI, /employee/loggedInUser, or any other approach "
            "to find/fix the company — NONE will work. Instead: complete ALL other parts of "
            "the task (create customer, product, order, project, etc.) and STOP.",
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
            fields = args.get("fields", "id,firstName,lastName,email,phoneNumberMobile")
            # Strip invalid EmployeeDTO fields
            bad_emp_fields = {"company", "salary", "annualSalary", "wage"}
            fields = ",".join(f.strip() for f in fields.split(",") if f.strip() not in bad_emp_fields)
            return client.get("/employee", params={
                "name": args.get("name"),
                "fields": fields,
                "count": 10,
            })

        case "tripletex_get_employee":
            fields = args.get("fields", "id,firstName,lastName,email,phoneNumberMobile,version")
            bad_emp_fields = {"company", "salary", "annualSalary", "wage"}
            fields = ",".join(f.strip() for f in fields.split(",") if f.strip() not in bad_emp_fields)
            return client.get(f"/employee/{args['id']}", params={
                "fields": fields,
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
            fields = args.get("fields", "id,name,email,phoneNumber")
            bad_cust_fields = {"company"}
            fields = ",".join(f.strip() for f in fields.split(",") if f.strip() not in bad_cust_fields)
            return client.get("/customer", params={
                "name": args.get("name"),
                "fields": fields,
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
                "count": args.get("count", 100),
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
            # Include travelDetails to create a "reiseregning" (not "ansattutlegg")
            # Per diem compensation only works on reiseregning type
            # typeOfTravel does NOT exist on the DTO — travelDetails is what sets the type
            return client.post("/travelExpense", {
                "employee": {"id": args["employee_id"]},
                "travelDetails": {
                    "isForeignTravel": False,
                    "isDayTrip": False,
                    "isCompensationFromRates": True,
                },
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
            # If model used nested brace syntax like "postings{account{id",
            # replace with safe flat defaults (nested syntax causes 400)
            if "{" in fields:
                fields = "id,date,description,postings"
            # Strip invalid VoucherDTO fields that cause 400
            bad_voucher_fields = {"amount", "accountId", "account"}
            parts = [f.strip() for f in fields.split(",")]
            # Deduplicate fields (duplicate "number" causes 400)
            seen = set()
            clean_parts = []
            for f in parts:
                base = f.split("(")[0].strip()
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

            # Block endpoints that don't exist or are proxy-blocked — prevent wasted iterations
            blocked_paths = {
                "/company": "GET /company is blocked by the proxy (405). Do NOT retry. "
                    "You do NOT need the company ID for any task.",
                "/whoAmI": "/whoAmI does not exist (404). Do NOT retry.",
                "/employee/loggedInUser": "/employee/loggedInUser does not exist. Do NOT retry.",
                "/occupationCode": "/occupationCode does not exist. Use /employee/employment/occupationCode instead.",
            }
            clean_path = path.rstrip("/")
            if clean_path in blocked_paths:
                raise TripletexError(400, f"BLOCKED: {blocked_paths[clean_path]}")

            # Strip invalid fields from GET params
            if method == "GET" and params.get("fields"):
                fields_str = params["fields"]
                # Strip 'company' field from any DTO (doesn't exist on Employee, Customer, etc.)
                bad_get_fields = {"company", "salary", "annualSalary", "wage"}
                params["fields"] = ",".join(
                    f.strip() for f in fields_str.split(",")
                    if f.strip() not in bad_get_fields
                )

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

            # Intercept POST /ledger/accountingDimensionName — fix field names
            # The DTO uses "dimensionName" NOT "name"
            if method == "POST" and path.rstrip("/") == "/ledger/accountingDimensionName":
                # Rename common wrong field names to the correct one
                for wrong_field in ("name", "displayName", "label", "value"):
                    if wrong_field in body and "dimensionName" not in body:
                        body["dimensionName"] = body.pop(wrong_field)
                        logger.info(f"Auto-renamed '{wrong_field}' → 'dimensionName' for accountingDimensionName")
                        break

            # Intercept POST /ledger/accountingDimensionValue — fix field names
            # Schema: displayName (string), dimensionIndex (int 1/2/3), number, active, position
            # The parent is referenced by "dimensionIndex" (1, 2, or 3) — NOT by ID or object ref
            if method == "POST" and path.rstrip("/") == "/ledger/accountingDimensionValue":
                # Rename wrong value name fields to "displayName"
                for wrong_field in ("name", "value", "label", "dimensionValue",
                                    "code", "description"):
                    if wrong_field in body and "displayName" not in body:
                        body["displayName"] = body.pop(wrong_field)
                        logger.info(f"Auto-renamed '{wrong_field}' → 'displayName' for accountingDimensionValue")
                        break
                # Fix parent reference: must be "dimensionIndex" (integer 1, 2, or 3)
                # Model often sends wrong field names — convert to dimensionIndex
                for wrong_parent in ("accountingDimensionNameId", "accountingDimensionName",
                                     "dimensionName", "dimensionId", "dimensionNameId"):
                    val = body.pop(wrong_parent, None)
                    if val is not None and "dimensionIndex" not in body:
                        if isinstance(val, dict) and "id" in val:
                            # Can't convert ID to index — need to look it up
                            dim_id = val["id"]
                            try:
                                dim = client.get(f"/ledger/accountingDimensionName/{dim_id}",
                                                 params={"fields": "id,dimensionIndex"})
                                idx = (dim or {}).get("value", {}).get("dimensionIndex")
                                if idx:
                                    body["dimensionIndex"] = idx
                                    logger.info(f"Auto-resolved dimension ID {dim_id} → dimensionIndex {idx}")
                            except TripletexError:
                                body["dimensionIndex"] = 1  # Fallback to first free dimension
                        elif isinstance(val, (int, float)):
                            # If it looks like a dimension index (1-3), use directly
                            if 1 <= int(val) <= 3:
                                body["dimensionIndex"] = int(val)
                            else:
                                # It's probably a dimension ID — look up the index
                                try:
                                    dim = client.get(f"/ledger/accountingDimensionName/{int(val)}",
                                                     params={"fields": "id,dimensionIndex"})
                                    idx = (dim or {}).get("value", {}).get("dimensionIndex")
                                    if idx:
                                        body["dimensionIndex"] = idx
                                        logger.info(f"Auto-resolved dimension ID {int(val)} → dimensionIndex {idx}")
                                except TripletexError:
                                    body["dimensionIndex"] = 1

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
                            # No divisions exist — create one with required fields
                            div_id = _auto_create_division(client)
                            if div_id:
                                body["division"] = {"id": div_id}
                            else:
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
                # Strip fields that don't belong or cause type errors on details endpoint
                for bad_field in ("positionCode", "hoursPerDay",
                                  "department", "division"):
                    body.pop(bad_field, None)
                # Fix occupationCode — must be object reference, not raw value
                occ = body.get("occupationCode")
                if occ is not None and not isinstance(occ, dict):
                    body.pop("occupationCode", None)  # Strip invalid type
                # Fix remunerationType — map common wrong enum values to correct ones
                rem = body.get("remunerationType")
                if rem is not None:
                    _rem_map = {
                        "MONTHLY_PAY": "MONTHLY_WAGE", "MONTHLY": "MONTHLY_WAGE",
                        "HOURLY_PAY": "HOURLY_WAGE", "HOURLY": "HOURLY_WAGE",
                        "COMMISSION_PAY": "COMMISION_PERCENTAGE", "COMMISSION": "COMMISION_PERCENTAGE",
                        "COMMISSION_PERCENTAGE": "COMMISION_PERCENTAGE",
                    }
                    body["remunerationType"] = _rem_map.get(rem, rem)

            # Intercept POST /travelExpense/cost — fix field names
            # Schema: travelExpense, vatType, currency, costCategory, paymentType,
            #         category, comments, rate, amountCurrencyIncVat, amountNOKInclVAT, date
            if method == "POST" and "/travelExpense/cost" in path and "/costParticipant" not in path:
                # 'description' → 'comments' (correct field name)
                if "description" in body and "comments" not in body:
                    body["comments"] = body.pop("description")
                # Strip fields that don't exist
                for bad_field in ("name", "title", "amount"):
                    body.pop(bad_field, None)
                # Fix currency — strip if just {"code": "NOK"} (defaults to NOK anyway)
                # Sending {"code": "NOK"} without factor causes "currency.factor: Må være minimum 1"
                curr = body.get("currency")
                if isinstance(curr, dict) and curr.get("code", "").upper() == "NOK" and "id" not in curr:
                    body.pop("currency")
                # amountCurrencyIncVat is REQUIRED — auto-fill from amountNOKInclVAT if missing
                if "amountCurrencyIncVat" not in body and "amountNOKInclVAT" in body:
                    body["amountCurrencyIncVat"] = body["amountNOKInclVAT"]

            # Intercept POST /travelExpense/perDiemCompensation — fix field names
            # Schema: travelExpense, rateType, rateCategory, countryCode, travelExpenseZoneId,
            #         overnightAccommodation, location, address, count, rate, amount,
            #         isDeductionForBreakfast/Lunch/Dinner
            if method == "POST" and "/travelExpense/perDiemCompensation" in path:
                # Rename common wrong fields
                if "countDays" in body and "count" not in body:
                    body["count"] = body.pop("countDays")
                if "numberOfDays" in body and "count" not in body:
                    body["count"] = body.pop("numberOfDays")
                if "days" in body and "count" not in body:
                    body["count"] = body.pop("days")
                # Strip fields that don't exist on the DTO
                for bad_field in ("startDate", "endDate", "numberOfNightsOnBoat",
                                  "description", "name", "title"):
                    body.pop(bad_field, None)

            # Intercept POST /travelExpense — ensure travelDetails for reiseregning
            if method == "POST" and path.rstrip("/") == "/travelExpense":
                for bad_field in ("typeOfTravel", "travelType", "type", "description"):
                    body.pop(bad_field, None)
                if "travelDetails" not in body:
                    body["travelDetails"] = {
                        "isForeignTravel": False,
                        "isDayTrip": False,
                        "isCompensationFromRates": True,
                    }

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
