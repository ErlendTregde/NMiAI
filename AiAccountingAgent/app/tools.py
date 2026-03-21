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
            "'administrator', after creating call tripletex_api_call with "
            "PUT /employee/{id}/loggedInUser to grant administrator access."
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
            },
            required=["firstName", "lastName"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_update_employee",
        description=(
            "Update an existing employee. "
            "Requires the current version number — GET the employee first to obtain it."
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
            },
            required=["id", "version"],
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
            "Do NOT supply a product number — Tripletex auto-assigns one. "
            "Only set vatTypeId=3 for standard 25% Norwegian VAT."
        ),
        parameters=_obj(
            {
                "name": _s("Product name"),
                "priceExcludingVatCurrency": _n("Sales price excluding VAT"),
                "costExcludingVatCurrency": _n("Cost price excluding VAT"),
                "vatTypeId": _i("VAT type ID. Use 3 for standard 25% Norwegian VAT."),
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
            "Full chain: create customer → create product → create order → create invoice."
        ),
        parameters=_obj(
            {
                "order_id": _i("Order ID to invoice"),
                "invoiceDate": _s("Invoice date (YYYY-MM-DD)"),
                "invoiceDueDate": _s("Payment due date (YYYY-MM-DD)"),
                "sendToCustomer": _b("Send to customer immediately (default false)"),
            },
            required=["order_id", "invoiceDate"],
        ),
    ),

    types.FunctionDeclaration(
        name="tripletex_list_invoices",
        description="List invoices.",
        parameters=_obj({
            "fields": _s("Fields to return"),
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
        description="Create a travel expense report.",
        parameters=_obj(
            {
                "description": _s("Description / purpose of travel"),
                "employee_id": _i("Employee ID who travelled"),
                "travelFromDate": _s("Travel start date (YYYY-MM-DD)"),
                "travelToDate": _s("Travel end date (YYYY-MM-DD)"),
                "destination": _s("Destination"),
                "purpose": _s("Purpose of travel"),
                "isCompleted": _b("Whether the report is submitted/completed"),
            },
            required=["description", "employee_id", "travelFromDate", "travelToDate"],
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
            "After creating, the activity can be linked to a project."
        ),
        parameters=_obj(
            {
                "name": _s("Activity name"),
                "description": _s("Description — optional"),
                "isProject": _b("Whether this is a project activity (default true)"),
                "isGeneral": _b("Whether this is a general/global activity (default false)"),
                "isChargeable": _b("Whether hours on this activity are chargeable to the customer"),
            },
            required=["name"],
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
        description="List chart-of-accounts entries (ledger accounts).",
        parameters=_obj({
            "number": _s("Filter by account number"),
            "fields": _s("Fields to return, e.g. 'id,number,name'"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_list_vouchers",
        description="List vouchers (bilag).",
        parameters=_obj({
            "dateFrom": _s("From date (YYYY-MM-DD)"),
            "dateTo": _s("To date (YYYY-MM-DD)"),
            "fields": _s("Fields to return"),
        }),
    ),

    types.FunctionDeclaration(
        name="tripletex_create_voucher",
        description="Create a voucher (bilag) with debit/credit postings.",
        parameters=_obj(
            {
                "date": _s("Voucher date (YYYY-MM-DD)"),
                "description": _s("Description"),
                "postings": _arr(
                    _obj({
                        "account_id": _i("Ledger account ID"),
                        "amount": _n("Amount (positive = debit, negative = credit)"),
                        "description": _s("Posting description"),
                    }),
                    desc="Debit/credit postings",
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
            "granting employee administrator access (PUT /employee/{id}/loggedInUser), "
            "enabling modules (PUT /company/{id}/settings), "
            "action endpoints like /invoice/{id}/:remind. "
            "IMPORTANT: Do NOT use this for payment registration — "
            "use tripletex_register_payment instead. "
            "Example: method='PUT', path='/employee/42/loggedInUser', "
            "body={'role': 'ADMINISTRATOR'}"
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
        return {"success": False, "error": exc.to_agent_message()}
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
            return client.post("/employee", _none_stripped({
                "firstName": args.get("firstName"),
                "lastName": args.get("lastName"),
                "email": args.get("email"),
                "userType": args.get("userTypeId", 1),  # Required: 0 is invalid, 1 = standard employee
                "department": {"id": args["departmentId"]} if args.get("departmentId") else None,
                "employeeNumber": args.get("employeeNumber"),
                "phoneNumberMobile": args.get("phoneNumberMobile"),
                "phoneNumberHome": args.get("phoneNumberHome"),
                "phoneNumberWork": args.get("phoneNumberWork"),
            }))

        case "tripletex_update_employee":
            return client.put(f"/employee/{args['id']}", _none_stripped({
                "id": args["id"],
                "version": args["version"],
                "firstName": args.get("firstName"),
                "lastName": args.get("lastName"),
                "email": args.get("email"),
                "phoneNumberMobile": args.get("phoneNumberMobile"),
                "phoneNumberHome": args.get("phoneNumberHome"),
                "phoneNumberWork": args.get("phoneNumberWork"),
            }))

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
            }))

        # ── Products ──────────────────────────────────────────────────────────

        case "tripletex_list_products":
            return client.get("/product", params={
                "name": args.get("name"),
                "fields": args.get("fields", "id,name,number,priceExcludingVatCurrency"),
                "count": 10,
            })

        case "tripletex_create_product":
            return client.post("/product", _none_stripped({
                "name": args.get("name"),
                "number": args.get("number"),
                "priceExcludingVatCurrency": args.get("priceExcludingVatCurrency"),
                "costExcludingVatCurrency": args.get("costExcludingVatCurrency"),
                "vatType": {"id": args["vatTypeId"]} if args.get("vatTypeId") else None,
            }))

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
            body = _none_stripped({
                "customer": {"id": args["customer_id"]},
                "orderDate": args["orderDate"],
                "deliveryDate": args.get("deliveryDate") or None,
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
            body = {
                "invoiceDate": invoice_date,
                "invoiceDueDate": due_date,
                "orders": [{"id": args["order_id"]}],
            }
            if args.get("sendToCustomer") is not None:
                body["sendToCustomer"] = args["sendToCustomer"]
            return client.post("/invoice", body)

        case "tripletex_list_invoices":
            return client.get("/invoice", params={
                "fields": args.get("fields", "id,invoiceDate,invoiceDueDate,amountCurrency,customer"),
                "count": args.get("count", 10),
            })

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
            return client.put(
                f"/invoice/{args['invoice_id']}/:payment",
                body={
                    "paymentDate": args["paymentDate"],
                    "paidAmount": args["amount"],
                    "paymentTypeId": args.get("paymentTypeId", 1),
                },
            )

        # ── Travel Expenses ───────────────────────────────────────────────────

        case "tripletex_list_travel_expenses":
            return client.get("/travelExpense", params={
                "fields": args.get("fields", "id,description,employee,travelFromDate,travelToDate"),
                "count": args.get("count", 10),
            })

        case "tripletex_create_travel_expense":
            return client.post("/travelExpense", _none_stripped({
                "description": args.get("description"),
                "employee": {"id": args["employee_id"]},
                "travelFromDate": args.get("travelFromDate"),
                "travelToDate": args.get("travelToDate"),
                "destination": args.get("destination"),
                "purpose": args.get("purpose"),
                "isCompleted": args.get("isCompleted", False),
            }))

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
            return client.post("/project", _none_stripped({
                "name": args.get("name"),
                "number": args.get("number"),
                "customer": {"id": args["customer_id"]} if args.get("customer_id") else None,
                "startDate": args.get("startDate"),
                "endDate": args.get("endDate"),
                "description": args.get("description"),
                "projectManager": {"id": args["projectManagerId"]} if args.get("projectManagerId") else None,
            }))

        # ── Activities ────────────────────────────────────────────────────────

        case "tripletex_create_activity":
            return client.post("/activity", _none_stripped({
                "name": args.get("name"),
                "description": args.get("description"),
                "isProject": args.get("isProject", True),
                "isGeneral": args.get("isGeneral", False),
                "isChargeable": args.get("isChargeable"),
            }))

        # ── Departments ───────────────────────────────────────────────────────

        case "tripletex_list_departments":
            return client.get("/department", params={
                "fields": args.get("fields", "id,name,departmentNumber"),
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
            return client.get("/ledger/account", params={
                "number": args.get("number"),
                "fields": args.get("fields", "id,number,name,description"),
                "count": 50,
            })

        case "tripletex_list_vouchers":
            return client.get("/ledger/voucher", params={
                "dateFrom": args.get("dateFrom"),
                "dateTo": args.get("dateTo"),
                "fields": args.get("fields", "id,date,description,postings"),
                "count": 20,
            })

        case "tripletex_create_voucher":
            postings = []
            for p in args.get("postings") or []:
                postings.append(_none_stripped({
                    "account": {"id": p["account_id"]},
                    "amount": p.get("amount"),
                    "description": p.get("description"),
                }))
            body = {"date": args["date"], "postings": postings}
            if args.get("description"):
                body["description"] = args["description"]
            return client.post("/ledger/voucher", body)

        case "tripletex_list_postings":
            return client.get("/ledger/posting", params={
                "dateFrom": args.get("dateFrom"),
                "dateTo": args.get("dateTo"),
                "fields": args.get("fields", "id,date,description,amount,account"),
                "count": 20,
            })

        # ── Generic escape hatch ──────────────────────────────────────────────

        case "tripletex_api_call":
            method = args["method"].upper()
            path = args["path"]
            params = args.get("params") or {}
            body = args.get("body") or {}
            match method:
                case "GET":
                    return client.get(path, params=params)
                case "POST":
                    return client.post(path, body)
                case "PUT":
                    return client.put(path, body=body or None, params=params or None)
                case "DELETE":
                    client.delete(path)
                    return {"deleted": True}
                case _:
                    raise ValueError(f"Unsupported HTTP method: {method}")

        case _:
            raise ValueError(f"Unknown tool: {name}")
