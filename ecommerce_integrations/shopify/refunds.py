"""Shopify order cancellation and refund handling for ERPNext."""

from __future__ import annotations

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from frappe.utils import cint, cstr, flt, getdate, nowdate

from ecommerce_integrations.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	REFUND_ID_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.product import get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log


def handle_order_cancelled(payload, request_id=None):
	"""Handle Shopify `orders/cancelled` webhook."""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	order = payload
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)

	try:
		order_id = cstr(order.get("id"))
		order_status = order.get("financial_status") or "voided"
		sales_order = _get_sales_order(order_id)

		if not sales_order:
			create_shopify_log(status="Invalid", message="Sales Order does not exist")
			return

		_set_order_status(sales_order.name, order_status)

		if not cint(setting.get("sync_order_refunds")):
			_legacy_cancel_or_status(order, sales_order, order_status)
			create_shopify_log(status="Success", message="Refund sync disabled; applied legacy cancel/status update")
			return

		messages = []
		refunds = order.get("refunds") or []

		for refund in refunds:
			msg = process_refund(refund, order=order, setting=setting)
			if msg:
				messages.append(msg)

		sales_invoice = _get_submitted_sales_invoice(order_id)
		delivery_notes = _get_submitted_delivery_notes(order_id)

		# Unpaid / unfulfilled cancel: cancel SO (and release reserved qty → inventory sync)
		if not refunds and not sales_invoice and not delivery_notes:
			if sales_order.docstatus == 1:
				item_codes = [d.item_code for d in sales_order.items]
				sales_order.reload()
				sales_order.cancel()
				messages.append(f"Cancelled Sales Order {sales_order.name}")
				_enqueue_inventory_sync(item_codes)
			create_shopify_log(status="Success", message="; ".join(messages) or "Nothing to reverse")
			return

		# Paid/refunded cancel with no embedded refunds: rely on refunds/create webhook
		# Voided with unpaid invoice: cancel SI then SO
		if not refunds and sales_invoice:
			if order_status == "voided" and _is_unpaid_invoice(sales_invoice):
				item_codes = []
				si = frappe.get_doc("Sales Invoice", sales_invoice)
				item_codes.extend(d.item_code for d in si.items)
				si.cancel()
				messages.append(f"Cancelled Sales Invoice {si.name}")
				sales_order.reload()
				if sales_order.docstatus == 1 and not _get_submitted_delivery_notes(order_id):
					sales_order.cancel()
					messages.append(f"Cancelled Sales Order {sales_order.name}")
				_enqueue_inventory_sync(item_codes)
			else:
				messages.append(
					"Order status updated; Credit Note will be created from refunds/create if not already synced"
				)

		if order.get("cancel_reason"):
			_add_comment(sales_order, f"Shopify cancel reason: {order.get('cancel_reason')}")

		create_shopify_log(status="Success", message="; ".join(messages) or "Order cancel processed")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def handle_refund_created(payload, request_id=None):
	"""Handle Shopify `refunds/create` webhook."""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	refund = payload
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)

	try:
		if not cint(setting.get("sync_order_refunds")):
			create_shopify_log(status="Invalid", message="Refund sync is disabled in Shopify Setting")
			return

		msg = process_refund(refund, order=None, setting=setting)
		create_shopify_log(status="Success", message=msg or "Refund already processed")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def process_refund(refund, order=None, setting=None, allow_full_without_lines=False) -> str | None:
	"""Create Credit Note (+ refund PE / DN return) for one Shopify refund. Idempotent."""
	setting = setting or frappe.get_cached_doc(SETTING_DOCTYPE)
	refund_id = cstr(refund.get("id"))
	order_id = cstr(refund.get("order_id") or (order or {}).get("id"))

	if not order_id:
		frappe.throw("Shopify refund is missing order_id")

	if _refund_already_processed(refund_id):
		return f"Refund {refund_id} already processed"

	sales_order = _get_sales_order(order_id)
	if not sales_order:
		frappe.throw(f"Sales Order not found for Shopify order {order_id}")

	order_status = (order or {}).get("financial_status") or "refunded"
	_set_order_status(sales_order.name, order_status)

	sales_invoice_name = _get_submitted_sales_invoice(order_id)
	delivery_notes = _get_submitted_delivery_notes(order_id)
	created = []
	item_codes_for_sync = []

	# No invoice: cancel SO if possible; return stock via DN returns if any
	if not sales_invoice_name:
		for dn_name in delivery_notes:
			dn_return = _create_delivery_note_return(
				dn_name, refund, order_id, order_status, setting, allow_full_without_lines
			)
			if dn_return:
				created.append(f"Delivery Note Return {dn_return.name}")
				item_codes_for_sync.extend(d.item_code for d in dn_return.items)

		if sales_order.docstatus == 1 and not _get_submitted_delivery_notes(order_id):
			item_codes_for_sync.extend(d.item_code for d in sales_order.items)
			sales_order.reload()
			sales_order.cancel()
			created.append(f"Cancelled Sales Order {sales_order.name}")

		_enqueue_inventory_sync(item_codes_for_sync)
		return ", ".join(created) if created else f"No documents to reverse for refund {refund_id}"

	credit_note = _create_credit_note(
		sales_invoice_name,
		refund,
		order_id,
		order_status,
		setting,
		allow_full_without_lines=allow_full_without_lines,
	)
	created.append(f"Credit Note {credit_note.name}")
	item_codes_for_sync.extend(d.item_code for d in credit_note.items)

	# Stock via DN return when invoice did not update stock
	should_restock = _should_restock(refund)
	if should_restock and not cint(credit_note.update_stock):
		for dn_name in delivery_notes:
			if _dn_return_exists_for_refund(dn_name, refund_id):
				continue
			dn_return = _create_delivery_note_return(
				dn_name, refund, order_id, order_status, setting, allow_full_without_lines
			)
			if dn_return:
				created.append(f"Delivery Note Return {dn_return.name}")
				item_codes_for_sync.extend(d.item_code for d in dn_return.items)

	refund_amount = _get_successful_refund_amount(refund)
	if refund_amount > 0 and cint(setting.get("submit_credit_notes", 1)) and credit_note.docstatus == 1:
		payment_entry = _create_refund_payment_entry(credit_note, refund, setting)
		if payment_entry:
			created.append(f"Payment Entry {payment_entry.name}")

	if refund.get("note"):
		_add_comment(credit_note, f"Shopify refund note: {refund.get('note')}")

	_enqueue_inventory_sync(item_codes_for_sync)
	return ", ".join(created)


def _legacy_cancel_or_status(order, sales_order, order_status):
	"""Previous cancel_order behaviour when refund sync is disabled."""
	order_id = cstr(order["id"])
	sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
	delivery_notes = frappe.db.get_list("Delivery Note", filters={ORDER_ID_FIELD: order_id})

	if sales_invoice:
		frappe.db.set_value("Sales Invoice", sales_invoice, ORDER_STATUS_FIELD, order_status)

	for dn in delivery_notes:
		frappe.db.set_value("Delivery Note", dn.name, ORDER_STATUS_FIELD, order_status)

	if not sales_invoice and not delivery_notes and sales_order.docstatus == 1:
		item_codes = [d.item_code for d in sales_order.items]
		sales_order.cancel()
		_enqueue_inventory_sync(item_codes)
	else:
		frappe.db.set_value("Sales Order", sales_order.name, ORDER_STATUS_FIELD, order_status)


def _create_credit_note(
	sales_invoice_name, refund, order_id, order_status, setting, allow_full_without_lines=False
):
	from ecommerce_integrations.shopify.order import set_return_item_wise_tax_details

	sales_invoice = frappe.get_doc("Sales Invoice", sales_invoice_name)
	credit_note = make_sales_return(sales_invoice_name)
	credit_note.set(ORDER_ID_FIELD, order_id)
	credit_note.set(ORDER_NUMBER_FIELD, sales_invoice.get(ORDER_NUMBER_FIELD))
	credit_note.set(ORDER_STATUS_FIELD, order_status)
	credit_note.set(REFUND_ID_FIELD, cstr(refund.get("id")))
	credit_note.set_posting_time = 1
	credit_note.posting_date = getdate(refund.get("created_at")) or nowdate()
	credit_note.naming_series = setting.sales_invoice_series or "SI-Shopify-"

	_apply_refund_quantities(credit_note, refund, allow_full_without_lines=allow_full_without_lines)

	# Prefer DN return for stock when DN sync is on; otherwise restock on CN if needed
	if _should_restock(refund) and not _get_submitted_delivery_notes(order_id):
		credit_note.update_stock = 1
	elif _get_submitted_delivery_notes(order_id):
		credit_note.update_stock = 0

	set_return_item_wise_tax_details(credit_note, sales_invoice, "sales_invoice_item")

	credit_note.flags.ignore_mandatory = True
	credit_note.insert(ignore_permissions=True, ignore_mandatory=True)

	if cint(setting.get("submit_credit_notes", 1)):
		credit_note.submit()
	return credit_note


def _create_delivery_note_return(
	dn_name, refund, order_id, order_status, setting, allow_full_without_lines=False
):
	from ecommerce_integrations.shopify.order import set_return_item_wise_tax_details

	if not cint(setting.sync_delivery_note):
		return None

	delivery_note = frappe.get_doc("Delivery Note", dn_name)
	dn_return = make_return_doc("Delivery Note", dn_name)
	dn_return.set(ORDER_ID_FIELD, order_id)
	dn_return.set(ORDER_STATUS_FIELD, order_status)
	dn_return.set(REFUND_ID_FIELD, cstr(refund.get("id")))
	dn_return.set_posting_time = 1
	dn_return.posting_date = getdate(refund.get("created_at")) or nowdate()
	dn_return.naming_series = setting.delivery_note_series or "DN-Shopify-"

	_apply_refund_quantities(dn_return, refund, allow_full_without_lines=allow_full_without_lines)
	if not dn_return.items:
		return None

	set_return_item_wise_tax_details(dn_return, delivery_note, "dn_detail")

	dn_return.flags.ignore_mandatory = True
	dn_return.insert(ignore_permissions=True, ignore_mandatory=True)
	dn_return.submit()
	return dn_return


def _create_refund_payment_entry(credit_note, refund, setting):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if flt(credit_note.outstanding_amount) == 0:
		return None

	payment_entry = get_payment_entry(
		credit_note.doctype, credit_note.name, bank_account=setting.cash_bank_account
	)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = cstr(refund.get("id"))
	payment_entry.reference_date = getdate(refund.get("created_at")) or nowdate()
	payment_entry.posting_date = getdate(refund.get("created_at")) or nowdate()
	payment_entry.set(REFUND_ID_FIELD, cstr(refund.get("id")))
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	return payment_entry


def _apply_refund_quantities(doc, refund, allow_full_without_lines=False):
	"""Reduce return doc to refunded quantities. Full return if no line items and allowed."""
	refund_lines = refund.get("refund_line_items") or []
	if not refund_lines:
		if allow_full_without_lines or not refund.get("id"):
			return
		# Refund with money only / no lines — keep full return against invoice
		return

	qty_by_item = {}
	for line in refund_lines:
		line_item = line.get("line_item") or {}
		try:
			item_code = get_item_code(line_item)
		except Exception:
			item_code = None
		if not item_code:
			sku = line_item.get("sku")
			if sku and frappe.db.exists("Item", sku):
				item_code = sku
		if not item_code:
			continue
		qty_by_item[item_code] = qty_by_item.get(item_code, 0) + flt(line.get("quantity"))

	if not qty_by_item:
		return

	new_items = []
	for item in doc.items:
		refund_qty = qty_by_item.get(item.item_code)
		if not refund_qty:
			continue
		# Return documents use negative qty in ERPNext
		max_qty = abs(flt(item.qty))
		applied = min(refund_qty, max_qty)
		item.qty = -applied if flt(item.qty) < 0 or cint(getattr(doc, "is_return", 0)) else applied
		if item.qty:
			new_items.append(item)

	doc.items = new_items
	if hasattr(doc, "calculate_taxes_and_totals"):
		doc.calculate_taxes_and_totals()


def _should_restock(refund) -> bool:
	if refund.get("restock"):
		return True
	for line in refund.get("refund_line_items") or []:
		if line.get("restock_type") in ("return", "cancel"):
			return True
	# Synthetic cancel without lines — restock by default
	if not (refund.get("refund_line_items") or []) and cstr(refund.get("id")).startswith("cancel-"):
		return True
	return False


def _get_successful_refund_amount(refund) -> float:
	total = 0.0
	for txn in refund.get("transactions") or []:
		if txn.get("kind") == "refund" and txn.get("status") == "success":
			total += flt(txn.get("amount"))
	# Synthetic cancel: use credit note outstanding later; treat as needing PE if no txns
	if not (refund.get("transactions") or []) and cstr(refund.get("id")).startswith("cancel-"):
		return 1.0  # signal to attempt PE against CN outstanding
	return total


def _refund_already_processed(refund_id) -> bool:
	if not refund_id:
		return False
	return bool(
		frappe.db.exists("Sales Invoice", {REFUND_ID_FIELD: refund_id, "docstatus": ["<", 2]})
		or frappe.db.exists("Delivery Note", {REFUND_ID_FIELD: refund_id, "docstatus": ["<", 2]})
	)


def _dn_return_exists_for_refund(dn_name, refund_id) -> bool:
	if not refund_id:
		return False
	return bool(
		frappe.db.exists(
			"Delivery Note",
			{"return_against": dn_name, REFUND_ID_FIELD: refund_id, "docstatus": ["<", 2]},
		)
	)


def _get_sales_order(order_id):
	name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: cstr(order_id)})
	return frappe.get_doc("Sales Order", name) if name else None


def _get_submitted_sales_invoice(order_id):
	return frappe.db.get_value(
		"Sales Invoice",
		{ORDER_ID_FIELD: cstr(order_id), "docstatus": 1, "is_return": 0},
		"name",
	)


def _get_submitted_delivery_notes(order_id) -> list[str]:
	return frappe.get_all(
		"Delivery Note",
		filters={ORDER_ID_FIELD: cstr(order_id), "docstatus": 1, "is_return": 0},
		pluck="name",
	)


def _is_unpaid_invoice(sales_invoice_name) -> bool:
	outstanding = flt(frappe.db.get_value("Sales Invoice", sales_invoice_name, "outstanding_amount"))
	grand_total = flt(frappe.db.get_value("Sales Invoice", sales_invoice_name, "grand_total"))
	return outstanding == grand_total


def _set_order_status(sales_order_name, order_status):
	if not order_status:
		return
	frappe.db.set_value("Sales Order", sales_order_name, ORDER_STATUS_FIELD, order_status)
	order_id = frappe.db.get_value("Sales Order", sales_order_name, ORDER_ID_FIELD)
	if not order_id:
		return
	for doctype in ("Sales Invoice", "Delivery Note"):
		for name in frappe.get_all(doctype, filters={ORDER_ID_FIELD: order_id}, pluck="name"):
			frappe.db.set_value(doctype, name, ORDER_STATUS_FIELD, order_status)


def _add_comment(doc, text):
	try:
		doc.add_comment(text=text)
	except Exception:
		pass


def _enqueue_inventory_sync(item_codes):
	item_codes = sorted({c for c in item_codes if c})
	if not item_codes:
		return

	frappe.enqueue(
		"ecommerce_integrations.shopify.inventory.sync_inventory_for_item_codes",
		item_codes=item_codes,
		queue="short",
		enqueue_after_commit=True,
	)
