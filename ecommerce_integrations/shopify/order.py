import json
from typing import Literal, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.customer import ShopifyCustomer
from ecommerce_integrations.shopify.product import create_items_if_not_exist, get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.utils.price_list import get_dummy_price_list
from ecommerce_integrations.utils.taxation import get_dummy_tax_category

DEFAULT_TAX_FIELDS = {
	"sales_tax": "default_sales_tax_account",
	"shipping": "default_shipping_charges_account",
}


def sync_sales_order(payload, request_id=None):
	order = payload
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	if frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: cstr(order["id"])}):
		create_shopify_log(status="Invalid", message="Sales order already exists, not synced")
		return
	try:
		shopify_customer = order.get("customer") if order.get("customer") is not None else {}
		shopify_customer["billing_address"] = order.get("billing_address", "")
		shopify_customer["shipping_address"] = order.get("shipping_address", "")
		customer_id = shopify_customer.get("id")
		if customer_id:
			customer = ShopifyCustomer(customer_id=customer_id)
			if not customer.is_synced():
				customer.sync_customer(customer=shopify_customer)
			else:
				customer.update_existing_addresses(shopify_customer)

		create_items_if_not_exist(order)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		create_order(order, setting)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success")


def create_order(order, setting, company=None):
	# local import to avoid circular dependencies
	from ecommerce_integrations.shopify.fulfillment import create_delivery_note
	from ecommerce_integrations.shopify.invoice import create_sales_invoice

	so = create_sales_order(order, setting, company)
	if so:
		if order.get("financial_status") == "paid":
			create_sales_invoice(order, setting, so)

		if order.get("fulfillments"):
			create_delivery_note(order, setting, so)


def create_sales_order(shopify_order, setting, company=None):
	customer = setting.default_customer
	if shopify_order.get("customer", {}):
		if customer_id := shopify_order.get("customer", {}).get("id"):
			customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")

	so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")

	if not so:
		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
		)

		if not items:
			message = (
				"Following items exists in the shopify order but relevant records were"
				" not found in the shopify Product master"
			)
			product_not_exists = []  # TODO: fix missing items
			message += "\n" + ", ".join(product_not_exists)

			create_shopify_log(status="Error", exception=message, rollback=True)

			return ""

		taxes = get_order_taxes(shopify_order, setting, items)
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"naming_series": setting.sales_order_series or "SO-Shopify-",
				ORDER_ID_FIELD: str(shopify_order.get("id")),
				ORDER_NUMBER_FIELD: shopify_order.get("name"),
				"customer": customer,
				"transaction_date": getdate(shopify_order.get("created_at")) or nowdate(),
				"delivery_date": getdate(shopify_order.get("created_at")) or nowdate(),
				"company": setting.company,
				"selling_price_list": get_dummy_price_list(),
				"ignore_pricing_rule": 1,
				"items": items,
				"taxes": taxes,
				"tax_category": get_dummy_tax_category(),
			}
		)

		if company:
			so.update({"company": company, "status": "Draft"})
		so.flags.ignore_mandatory = True
		so.flags.shopiy_order_json = json.dumps(shopify_order)
		_set_item_wise_tax_details(so)
		so.save(ignore_permissions=True)
		so.submit()

		if shopify_order.get("note"):
			so.add_comment(text=f"Order Note: {shopify_order.get('note')}")

	else:
		so = frappe.get_doc("Sales Order", so)

	return so


def _set_item_wise_tax_details(so):
	"""Provide item-wise tax breakup in the format ERPNext v15+ expects.

	Newer ERPNext ignores the legacy `item_wise_tax_detail` JSON while saving and
	instead maintains `_item_wise_tax_details` (persisted to the "Item Wise Tax
	Detail" child table). For tax rows marked `dont_recompute_tax` ERPNext skips
	building this structure, so apps that rely on it (e.g. india_compliance GST
	validations) see zero item-wise tax and block the transaction.
	"""
	if not so.meta.get_field("item_wise_tax_details"):
		return  # older ERPNext, legacy JSON field is still used

	item_rows = {}
	for item in so.items:
		item_rows.setdefault(item.item_code, item)

	details = []
	for tax in so.taxes:
		if not tax.get("dont_recompute_tax"):
			continue

		try:
			tax_detail = json.loads(tax.item_wise_tax_detail or "{}")
		except ValueError:
			continue

		for item_code, (rate, amount) in tax_detail.items():
			item = item_rows.get(item_code)
			if not item:
				continue

			details.append(
				frappe._dict(
					item=item,
					tax=tax,
					rate=flt(rate),
					amount=flt(amount),
					taxable_amount=flt(item.qty) * flt(item.rate),
				)
			)

	if details:
		so._item_wise_tax_details = details


def set_mapped_item_wise_tax_details(doc, so):
	"""Copy the Sales Order's persisted item-wise tax breakup onto a document
	mapped from it (Delivery Note / Sales Invoice).

	`get_mapped_doc` copies tax rows (including `dont_recompute_tax`) but not the
	"Item Wise Tax Detail" table, so GST validations (india_compliance) would see
	zero item-wise tax on the mapped document and block it.
	"""
	if not so.meta.get_field("item_wise_tax_details"):
		return  # older ERPNext, legacy JSON field is still used

	so_details = so.get("item_wise_tax_details") or []
	if not so_details:
		return

	# tax rows are mapped from the SO in the same order
	tax_map = {}
	for so_tax, tax in zip(so.taxes, doc.taxes):
		tax_map[so_tax.name] = tax

	# mapped item rows reference their SO item row via so_detail
	item_map = {}
	for item in doc.items:
		if item.get("so_detail"):
			item_map.setdefault(item.so_detail, item)

	details = []
	for row in so_details:
		item = item_map.get(row.item_row)
		tax = tax_map.get(row.tax_row)
		if not (item and tax):
			continue

		details.append(
			frappe._dict(
				item=item,
				tax=tax,
				rate=row.rate,
				amount=row.amount,
				taxable_amount=row.taxable_amount,
			)
		)

	if details:
		doc._item_wise_tax_details = details


def set_return_item_wise_tax_details(return_doc, source_doc, detail_field):
	"""Copy item-wise tax breakup from an SI/DN onto its return document.

	`make_return_doc` copies tax rows but not Item Wise Tax Detail, so
	india_compliance GST checks fail with "No GST is being charged on Taxable Items".

	Return docs use negative qty/tax amounts — item-wise amounts must be negative too,
	or ERPNext throws "Item Wise Tax Details do not match" (diff ≈ 2 × tax).
	"""
	if not source_doc.meta.get_field("item_wise_tax_details"):
		_set_return_tax_details_from_json(return_doc, source_doc)
		_align_return_tax_row_amounts(return_doc)
		return

	source_details = source_doc.get("item_wise_tax_details") or []
	if not source_details:
		_set_return_tax_details_from_json(return_doc, source_doc)
		_align_return_tax_row_amounts(return_doc)
		return

	tax_map = {}
	for src_tax, tax in zip(source_doc.taxes, return_doc.taxes):
		tax_map[src_tax.name] = tax

	source_items = {d.name: d for d in source_doc.items}
	item_map = {}
	for item in return_doc.items:
		ref = item.get(detail_field)
		if ref:
			item_map[ref] = item

	details = []
	for row in source_details:
		item = item_map.get(row.item_row)
		tax = tax_map.get(row.tax_row)
		source_item = source_items.get(row.item_row)
		if not (item and tax and source_item):
			continue

		source_qty = abs(flt(source_item.qty)) or 1
		return_qty = abs(flt(item.qty))
		ratio = return_qty / source_qty
		# Returns are negative; keep sign of return qty
		sign = -1 if flt(item.qty) < 0 or cint(return_doc.get("is_return")) else 1

		details.append(
			frappe._dict(
				item=item,
				tax=tax,
				rate=row.rate,
				amount=abs(flt(row.amount) * ratio) * sign,
				taxable_amount=abs(flt(row.taxable_amount) * ratio) * sign,
			)
		)

	if details:
		return_doc._item_wise_tax_details = details
		_align_return_tax_row_amounts(return_doc)


def _set_return_tax_details_from_json(return_doc, source_doc):
	"""Fallback when Item Wise Tax Detail child rows are missing on the source."""
	item_rows = {d.item_code: d for d in return_doc.items}
	source_qty = {d.item_code: abs(flt(d.qty)) or 1 for d in source_doc.items}

	tax_by_account = {t.account_head: t for t in return_doc.taxes}
	details = []

	for tax in source_doc.taxes:
		if not tax.get("dont_recompute_tax"):
			continue
		try:
			tax_detail = json.loads(tax.item_wise_tax_detail or "{}")
		except ValueError:
			continue

		return_tax = tax_by_account.get(tax.account_head)
		if not return_tax:
			continue

		for item_code, values in tax_detail.items():
			item = item_rows.get(item_code)
			if not item:
				continue
			rate, amount = values[0], values[1]
			ratio = abs(flt(item.qty)) / source_qty.get(item_code, 1)
			sign = -1 if flt(item.qty) < 0 or cint(return_doc.get("is_return")) else 1
			details.append(
				frappe._dict(
					item=item,
					tax=return_tax,
					rate=flt(rate),
					amount=abs(flt(amount) * ratio) * sign,
					taxable_amount=flt(item.qty) * flt(item.rate),
				)
			)

	if details:
		return_doc._item_wise_tax_details = details


def _align_return_tax_row_amounts(return_doc):
	"""Force Taxes and Charges amounts to equal item-wise breakup (incl. partial returns)."""
	from collections import defaultdict

	totals = defaultdict(float)
	for row in return_doc.get("_item_wise_tax_details") or []:
		tax = row.get("tax")
		if tax:
			totals[tax.name] += flt(row.amount)

	for tax in return_doc.get("taxes") or []:
		if tax.name not in totals:
			continue
		amt = flt(totals[tax.name])
		tax.tax_amount = amt
		tax.base_tax_amount = amt
		tax.tax_amount_after_discount_amount = amt
		tax.base_tax_amount_after_discount_amount = amt


def get_order_items(order_items, setting, delivery_date, taxes_inclusive):
	items = []
	all_product_exists = True
	product_not_exists = []

	for shopify_item in order_items:
		if not shopify_item.get("product_exists"):
			all_product_exists = False
			product_not_exists.append(
				{"title": shopify_item.get("title"), ORDER_ID_FIELD: shopify_item.get("id")}
			)
			continue

		if all_product_exists:
			item_code = get_item_code(shopify_item)
			items.append(
				{
					"item_code": item_code,
					"item_name": shopify_item.get("name"),
					"rate": _get_item_price(shopify_item, taxes_inclusive),
					"delivery_date": delivery_date,
					"qty": shopify_item.get("quantity"),
					"stock_uom": shopify_item.get("uom") or "Nos",
					"warehouse": setting.warehouse,
					ORDER_ITEM_DISCOUNT_FIELD: (
						_get_total_discount(shopify_item) / cint(shopify_item.get("quantity"))
					),
				}
			)
		else:
			items = []

	return items


def _get_item_price(line_item, taxes_inclusive: bool) -> float:
	price = flt(line_item.get("price"))
	qty = cint(line_item.get("quantity"))

	# remove line item level discounts
	total_discount = _get_total_discount(line_item)

	if not taxes_inclusive:
		return price - (total_discount / qty)

	total_taxes = 0.0
	for tax in line_item.get("tax_lines"):
		total_taxes += flt(tax.get("price"))

	return price - (total_taxes + total_discount) / qty


def _get_total_discount(line_item) -> float:
	discount_allocations = line_item.get("discount_allocations") or []
	return sum(flt(discount.get("amount")) for discount in discount_allocations)


def get_order_taxes(shopify_order, setting, items):
	taxes = []
	line_items = shopify_order.get("line_items")

	for line_item in line_items:
		item_code = get_item_code(line_item)
		for tax in line_item.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax"),
					"description": (
						get_tax_account_description(tax)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax.get("price"),
					"included_in_print_rate": 0,
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {item_code: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]},
					"dont_recompute_tax": 1,
				}
			)

	update_taxes_with_shipping_lines(
		taxes,
		shopify_order.get("shipping_lines"),
		setting,
		items,
		taxes_inclusive=shopify_order.get("taxes_included"),
	)

	if cint(setting.consolidate_taxes):
		taxes = consolidate_order_taxes(taxes)

	for row in taxes:
		tax_detail = row.get("item_wise_tax_detail")
		if isinstance(tax_detail, dict):
			row["item_wise_tax_detail"] = json.dumps(tax_detail)

	return taxes


def consolidate_order_taxes(taxes):
	tax_account_wise_data = {}
	for tax in taxes:
		account_head = tax["account_head"]
		tax_account_wise_data.setdefault(
			account_head,
			{
				"charge_type": "Actual",
				"account_head": account_head,
				"description": tax.get("description"),
				"cost_center": tax.get("cost_center"),
				"included_in_print_rate": 0,
				"dont_recompute_tax": 1,
				"tax_amount": 0,
				"item_wise_tax_detail": {},
			},
		)
		tax_account_wise_data[account_head]["tax_amount"] += flt(tax.get("tax_amount"))
		if tax.get("item_wise_tax_detail"):
			tax_account_wise_data[account_head]["item_wise_tax_detail"].update(tax["item_wise_tax_detail"])

	return tax_account_wise_data.values()


def get_tax_account_head(tax, charge_type: Literal["shipping", "sales_tax"] | None = None):
	tax_title = str(tax.get("title"))

	tax_account = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": SETTING_DOCTYPE, "shopify_tax": tax_title},
		"tax_account",
	)

	if not tax_account and charge_type:
		tax_account = frappe.db.get_single_value(SETTING_DOCTYPE, DEFAULT_TAX_FIELDS[charge_type])

	if not tax_account:
		frappe.throw(_("Tax Account not specified for Shopify Tax {0}").format(tax.get("title")))

	return tax_account


def get_tax_account_description(tax):
	tax_title = tax.get("title")

	tax_description = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": SETTING_DOCTYPE, "shopify_tax": tax_title},
		"tax_description",
	)

	return tax_description


def update_taxes_with_shipping_lines(taxes, shipping_lines, setting, items, taxes_inclusive=False):
	"""Shipping lines represents the shipping details,
	each such shipping detail consists of a list of tax_lines"""
	shipping_as_item = cint(setting.add_shipping_as_item) and setting.shipping_item
	for shipping_charge in shipping_lines:
		if shipping_charge.get("price"):
			shipping_discounts = shipping_charge.get("discount_allocations") or []
			total_discount = sum(flt(discount.get("amount")) for discount in shipping_discounts)

			shipping_taxes = shipping_charge.get("tax_lines") or []
			total_tax = sum(flt(discount.get("price")) for discount in shipping_taxes)

			shipping_charge_amount = flt(shipping_charge["price"]) - flt(total_discount)
			if bool(taxes_inclusive):
				shipping_charge_amount -= total_tax

			if shipping_as_item:
				items.append(
					{
						"item_code": setting.shipping_item,
						"rate": shipping_charge_amount,
						"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
						"qty": 1,
						"stock_uom": "Nos",
						"warehouse": setting.warehouse,
					}
				)
			else:
				taxes.append(
					{
						"charge_type": "Actual",
						"account_head": get_tax_account_head(shipping_charge, charge_type="shipping"),
						"description": get_tax_account_description(shipping_charge)
						or shipping_charge["title"],
						"tax_amount": shipping_charge_amount,
						"cost_center": setting.cost_center,
					}
				)

		for tax in shipping_charge.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax"),
					"description": (
						get_tax_account_description(tax)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax["price"],
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {
						setting.shipping_item: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]
					}
					if shipping_as_item
					else {},
					"dont_recompute_tax": 1,
				}
			)


def get_sales_order(order_id):
	"""Get ERPNext sales order using shopify order id."""
	sales_order = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: order_id})
	if sales_order:
		return frappe.get_doc("Sales Order", sales_order)


def cancel_order(payload, request_id=None):
	"""Called by orders/cancelled webhook. Delegates to refunds handler."""
	from ecommerce_integrations.shopify.refunds import handle_order_cancelled

	handle_order_cancelled(payload, request_id=request_id)


@temp_shopify_session
def sync_old_orders():
	shopify_setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not cint(shopify_setting.sync_old_orders):
		return

	orders = _fetch_old_orders(shopify_setting.old_orders_from, shopify_setting.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"], request_data=json.dumps(order), make_new=True
		)
		sync_sales_order(order, request_id=log.name)

	shopify_setting = frappe.get_doc(SETTING_DOCTYPE)
	shopify_setting.sync_old_orders = 0
	shopify_setting.save()


def _fetch_old_orders(from_time, to_time):
	"""Fetch all shopify orders in specified range and return an iterator on fetched orders."""

	from_time = get_datetime(from_time).astimezone().isoformat()
	to_time = get_datetime(to_time).astimezone().isoformat()
	orders_iterator = PaginatedIterator(
		Order.find(created_at_min=from_time, created_at_max=to_time, limit=250)
	)

	for orders in orders_iterator:
		for order in orders:
			# Using generator instead of fetching all at once is better for
			# avoiding rate limits and reducing resource usage.
			yield order.to_dict()
