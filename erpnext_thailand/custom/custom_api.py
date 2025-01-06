import frappe
import urllib3
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.utils import flt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_thai_tax_settings(company):
    settings = frappe.get_single("Thai Tax Settings")
    accounts = list(filter(lambda x: x.company == company, settings.company_accounts))
    if not accounts:
        frappe.throw(_("Please set up {0} for company {1}").format(
            frappe.utils.get_link_to_form("Thai Tax Settings", "Thai Tax Settings"),
            company,
        ))
    return accounts[0]


def create_tax_invoice_on_gl_tax(doc, method):
    if doc.flags.from_repost:
        return

    setting = get_thai_tax_settings(doc.company)
    voucher = frappe.get_doc(doc.voucher_type, doc.voucher_no)
    is_return = is_return_voucher(doc, voucher)
    sign = -1 if is_return else 1

    tax_amount, doctype = calculate_tax_amount(doc, setting, is_return)
    if doctype and tax_amount != 0:
        base_amount = get_base_amount(voucher, doc, sign)
        validate_base_amount(base_amount, tax_amount, doc.account)
        tinv = create_tax_invoice(doc, doctype, base_amount, tax_amount, voucher)
        tinv = update_voucher_tinv(doctype, voucher, tinv)
        tinv.submit()


def is_return_voucher(doc, voucher):
    if doc.voucher_type in ["Sales Invoice", "Purchase Invoice"]:
        return voucher.is_return
    if doc.voucher_type == "Journal Entry":
        return bool(voucher.reversal_of)
    return False


def calculate_tax_amount(doc, setting, is_return):
    tax_amount = 0.0
    doctype = None
    if doc.account in [setting.sales_tax_account, setting.purchase_tax_account]:
        tax_amount = doc.credit - doc.debit
        if (tax_amount > 0 and not is_return) or (tax_amount < 0 and is_return):
            doctype = "Sales Tax Invoice"
        if (tax_amount < 0 and not is_return) or (tax_amount > 0 and is_return):
            doctype = "Purchase Tax Invoice"
        tax_amount = abs(tax_amount) * (-1 if is_return else 1)
    return tax_amount, doctype


def get_base_amount(voucher, doc, sign):
    if voucher.doctype == "Expense Claim":
        return abs(voucher.base_amount_overwrite or voucher.total_sanctioned_amount) * sign
    if voucher.doctype in ["Purchase Invoice", "Sales Invoice"]:
        return abs(voucher.base_net_total) * sign
    if voucher.doctype == "Payment Entry":
        return abs(voucher.tax_base_amount) * sign
    if voucher.doctype == "Journal Entry":
        voucher = frappe.get_doc("Journal Entry Tax Invoice Detail", doc.voucher_detail_no)
        return abs(voucher.tax_base_amount) * sign
    return 0


def validate_base_amount(base_amount, tax_amount, account):
    tax_rate = frappe.get_cached_value("Account", account, "tax_rate")
    if abs((base_amount * tax_rate / 100) - tax_amount) > 0.1:
        frappe.throw(
            _("Tax should be {}% of the base amount<br/>"
              "<b>Note:</b> To correct base amount, fill in Base Amount Overwrite.".format(tax_rate))
        )


def create_tax_invoice(doc, doctype, base_amount, tax_amount, voucher):
    tinv_dict = {
        "doctype": doctype,
        "gl_entry": doc.name,
        "tax_amount": tax_amount,
        "tax_base": base_amount,
        "party": get_party(doc, voucher, doctype)
    }
    if doc.voucher_type == "Journal Entry":
        je = frappe.get_doc(doc.voucher_type, doc.voucher_no)
        if je.for_payment:
            tinv_dict.update({
                "against_voucher_type": "Payment Entry",
                "against_voucher": je.for_payment,
            })
    tinv = frappe.get_doc(tinv_dict)
    tinv.insert(ignore_permissions=True)
    return tinv


def get_party(doc, voucher, doctype):
    gl = frappe.db.get_all(
        "GL Entry",
        filters={"voucher_type": doc.voucher_type, "voucher_no": doc.voucher_no, "party": ["!=", ""]},
        fields=["party", "party_type"],
    )
    party = gl and gl[0].get("party")
    if doc.voucher_type == "Journal Entry":
        if doctype == "Sales Tax Invoice":
            party = voucher.customer or party
            if not party:
                frappe.throw(
                    _("<b>Customer is required for Sales Tax Invoice!</b><br/>"
                      "Please edit accounting entry with Tax account and choose <b>Customer</b> under Overwrite Tax invoice section.")
                )
        if doctype == "Purchase Tax Invoice":
            party = voucher.supplier or party
            if not party:
                frappe.throw(
                    _("<b>Supplier is required for Purchase Tax Invoice!</b><br/>"
                      "Please edit accounting entry with Tax account and choose <b>Supplier</b> under Overwrite Tax invoice section.")
                )
    if doc.voucher_type == "Payment Entry" and doc.party_type == "Employee":
        party = voucher.supplier
    if doc.voucher_type == "Expense Claim":
        party = voucher.supplier
    if not party:
        frappe.throw(_("Please fill in Supplier for Purchase Tax Invoice"))
    return party


def update_voucher_tinv(doctype, voucher, tinv):
    update_company_tax_address(voucher, tinv)
    if doctype == "Sales Tax Invoice":
        voucher.tax_invoice_number = tinv.name
        voucher.tax_invoice_date = tinv.date
        tinv.report_date = tinv.date
    if doctype == "Purchase Tax Invoice":
        if not (voucher.tax_invoice_number and voucher.tax_invoice_date):
            frappe.throw(_("Please enter Tax Invoice Number / Tax Invoice Date"))
        voucher.save()
        tinv.number = voucher.tax_invoice_number
        tinv.report_date = tinv.date = voucher.tax_invoice_date
    voucher.save()
    tinv.save()
    return tinv


def update_company_tax_address(voucher, tinv):
    if tinv.voucher_type == "Sales Invoice":
        tinv.company_tax_address = voucher.company_address
    elif tinv.voucher_type == "Purchase Invoice":
        tinv.company_tax_address = voucher.billing_address
    else:
        tinv.company_tax_address = voucher.company_tax_address
    if not tinv.company_tax_address:
        frappe.throw(_("No Company Billing/Tax Address"))


def validate_company_address(doc, method):
    if not doc.company_tax_address:
        addresses = frappe.db.get_all(
            "Address",
            filters={"is_your_company_address": 1, "address_type": "Billing"},
            fields=["name", "address_type"],
        )
        if len(addresses) == 1:
            doc.company_tax_address = addresses[0]["name"]


def validate_tax_invoice(doc, method):
    setting = get_thai_tax_settings(doc.company)
    tax_account = setting.purchase_tax_account
    voucher = frappe.get_doc(doc.doctype, doc.name)
    has_vat = any(tax.account_head == tax_account for tax in voucher.taxes)
    if has_vat and not doc.tax_invoice_number:
        frappe.throw(_("This document require Tax Invoice Number"))
    if not has_vat and doc.tax_invoice_number:
        frappe.throw(_("This document has no due VAT, please remove Tax Invoice Number"))


@frappe.whitelist()
def to_clear_undue_tax(dt, dn):
    if is_tax_invoice_exists(dt, dn):
        return False
    if not make_clear_vat_journal_entry(dt, dn):
        return False
    return True


def is_tax_invoice_exists(dt, dn):
    doc = frappe.get_doc(dt, dn)
    ptax = frappe.get_all(
        "Purchase Tax Invoice",
        or_filters={"voucher_no": doc.name, "against_voucher": doc.name},
        pluck="name",
    )
    return bool(ptax)


@frappe.whitelist()
def make_clear_vat_journal_entry(dt, dn):
    doc = frappe.get_doc(dt, dn)
    tax = get_thai_tax_settings(doc.company)
    je = frappe.new_doc("Journal Entry")
    je.entry_type = "Journal Entry"
    je.supplier = doc.party_type == "Supplier" and doc.party or False
    je.company_tax_address = doc.company_tax_address
    je.for_payment = doc.name
    je.user_remark = _("Clear Undue Tax on %s" % doc.name)
    base_total, tax_total = process_references(doc, tax, je)
    if not tax_total:
        return False
    je.append(
        "accounts",
        {
            "account": tax.sales_tax_account,
            "credit_in_account_currency": tax_total < 0 and abs(tax_total),
            "debit_in_account_currency": tax_total > 0 and tax_total,
        },
    )
    return je


def process_references(doc, tax, je):
    base_total = 0
    tax_total = 0
    references = filter(
        lambda x: x.reference_doctype in ("Purchase Invoice", "Expense Claim"), doc.references
    )
    for ref in references:
        if not ref.allocated_amount or not ref.total_amount:
            continue
        gl_entries = frappe.db.get_all(
            "GL Entry",
            filters={
                "voucher_type": ref.reference_doctype,
                "voucher_no": ref.reference_name,
            },
            fields=["*"],
        )
        for gl in gl_entries:
            undue_tax, base_amount, account_undue, account = get_undue_tax(doc, ref, gl, tax)
            if ref.reference_doctype in ("Purchase Invoice", "Expense Claim"):
                undue_tax = -undue_tax
                base_amount = -base_amount
            base_total += base_amount
            if undue_tax:
                je.append(
                    "accounts",
                    {
                        "account": account_undue,
                        "credit_in_account_currency": undue_tax > 0 and undue_tax,
                        "debit_in_account_currency": undue_tax < 0 and abs(undue_tax),
                        "tax_base_amount": base_total,
                    },
                )
                tax_total += undue_tax
    return base_total, tax_total


def clear_invoice_undue_tax(doc, method):
    old_doc = doc.get_doc_before_save()
    if (
        old_doc
        and old_doc.total_allocated_amount == doc.total_allocated_amount
        and old_doc.has_purchase_tax_invoice == doc.has_purchase_tax_invoice
    ):
        return
    doc.taxes = []
    tax = get_thai_tax_settings(doc.company)
    base_total, tax_total = process_references(doc, tax, doc)
    if not tax_total:
        if doc.has_purchase_tax_invoice:
            frappe.throw(
                _("No undue tax amount to clear. Please uncheck 'Has Purchase Tax Invoice'")
            )
        return
    doc.append(
        "taxes",
        {
            "add_deduct_tax": "Add",
            "description": "Clear Undue Tax",
            "charge_type": "Actual",
            "account_head": tax.sales_tax_account,
            "tax_amount": tax_total,
        },
    )
    doc.tax_base_amount = base_total
    doc.calculate_taxes()
    doc.save()


def get_undue_tax(doc, ref, gl, tax):
    undue_tax = 0
    base_amount = 0
    tax_account_undue = tax.sales_tax_account_undue
    tax_account = tax.sales_tax_account
    if ref.reference_doctype in ("Purchase Invoice", "Expense Claim"):
        tax_account_undue = tax.purchase_tax_account_undue
        tax_account = tax.purchase_tax_account
    credit = gl["credit"]
    debit = gl["debit"]
    alloc_percent = ref.allocated_amount / ref.total_amount
    report_type = frappe.get_cached_value("Account", gl["account"], "report_type")
    if report_type == "Profit and Loss":
        base_amount = alloc_percent * (credit - debit)
    if gl["account"] == tax_account_undue:
        undue_tax = alloc_percent * (credit - debit)
    return undue_tax, base_amount, tax_account_undue, tax_account


def is_tax_reset(doc, tax_accounts):
    if doc.docstatus != 0:
        return False
    old_doc = doc.get_doc_before_save()
    if old_doc:
        old_tax_lines = list(filter(lambda l: l.account in tax_accounts, old_doc.accounts))
        new_tax_lines = list(filter(lambda l: l.account in tax_accounts, doc.accounts))
        if len(old_tax_lines) != len(new_tax_lines):
            return True
        for old_line, new_line in zip(old_tax_lines, new_tax_lines):
            if (
                old_line.tax_base_amount != new_line.tax_base_amount
                or old_line.debit != new_line.debit
                or old_line.credit != new_line.credit
                or old_line.supplier != new_line.supplier
                or old_line.customer != new_line.customer
                or old_line.tax_invoice_number != new_line.tax_invoice_number
                or str(old_line.tax_invoice_date) != str(new_line.tax_invoice_date)
            ):
                return True
    else:
        return True
    return False


def prepare_journal_entry_tax_invoice_detail(doc, method):
    setting = get_thai_tax_settings(doc.company)
    tax_accounts = [setting.sales_tax_account, setting.purchase_tax_account]
    precision = get_field_precision(
        frappe.get_meta("Journal Entry Tax Invoice Detail").get_field("tax_base_amount")
    )
    reset_tax = is_tax_reset(doc, tax_accounts)
    if reset_tax:
        for d in doc.tax_invoice_details:
            d.delete()
        for tax_line in filter(lambda l: l.account in tax_accounts, doc.accounts):
            tax_rate = frappe.get_cached_value("Account", tax_line.account, "tax_rate")
            tax_amount = abs(tax_line.debit - tax_line.credit)
            tax_base_amount = tax_line.tax_base_amount or (
                tax_rate > 0 and tax_amount * 100 / tax_rate or 0
            )
            company_tax_address = doc.company_tax_address or get_company_tax_address()
            party_name = get_party_name(tax_line)
            tinv_detail = frappe.get_doc(
                {
                    "doctype": "Journal Entry Tax Invoice Detail",
                    "parenttype": "Journal Entry",
                    "parentfield": "tax_invoice_details",
                    "parent": doc.name,
                    "company_tax_address": company_tax_address,
                    "supplier": tax_line.supplier,
                    "customer": tax_line.customer,
                    "party_name": party_name,
                    "tax_invoice_number": tax_line.tax_invoice_number,
                    "tax_invoice_date": tax_line.tax_invoice_date,
                    "tax_base_amount": flt(tax_base_amount, precision),
                    "tax_amount": flt(tax_amount, precision),
                }
            )
            tax_line.reference_detail_no = tinv_detail.insert().name
            tax_line.save()
        doc.reload()


def get_company_tax_address():
    addrs = frappe.get_all("Address", {"is_your_company_address": 1}, pluck="name")
    return len(addrs) == 1 and addrs[0] or ""


def get_party_name(tax_line):
    if tax_line.customer:
        return frappe.get_doc("Customer", tax_line.customer).customer_name
    if tax_line.supplier:
        return frappe.get_doc("Supplier", tax_line.supplier).supplier_name
    return ""
