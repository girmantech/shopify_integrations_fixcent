from typing import Optional

import time

import frappe
from frappe import _, msgprint
from frappe.utils import cint, create_batch, cstr
from frappe.utils.nestedset import get_root_of
from pyactiveresource.connection import ResourceNotFound
from shopify.resources import Image, Product, Variant

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	IS_SHOPIFY_ITEM_FIELD,
	ITEM_SELLING_RATE_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
	SHOPIFY_VARIANTS_ATTR_LIST,
	SUPPLIER_ID_FIELD,
	WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopify.utils import create_shopify_log

UPLOAD_NEW_ITEMS_JOB = "shopify.job.upload_new_items"
# Shopify REST product create uses multiple API calls per item; keep batches small
# and cap work per job so large imports (e.g. 1000 items) chain safely.
UPLOAD_BATCH_SIZE = 50
UPLOAD_ITEMS_PER_JOB = 200
UPLOAD_ITEM_DELAY_SECONDS = 0.5


class ShopifyProduct:
	def __init__(
		self,
		product_id: str,
		variant_id: str | None = None,
		sku: str | None = None,
		has_variants: int | None = 0,
	):
		self.product_id = str(product_id)
		self.variant_id = str(variant_id) if variant_id else None
		self.sku = str(sku) if sku else None
		self.has_variants = has_variants
		self.setting = frappe.get_doc(SETTING_DOCTYPE)

		if not self.setting.is_enabled():
			frappe.throw(_("Can not create Shopify product when integration is disabled."))

	def is_synced(self) -> bool:
		return ecommerce_item.is_synced(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
		)

	def get_erpnext_item(self):
		return ecommerce_item.get_erpnext_item(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
			has_variants=self.has_variants,
		)

	@temp_shopify_session
	def sync_product(self):
		if not self.is_synced():
			shopify_product = Product.find(self.product_id)
			product_dict = shopify_product.to_dict()
			self._make_item(product_dict)

	def _make_item(self, product_dict):
		_add_weight_details(product_dict)

		warehouse = self.setting.warehouse

		if _has_variants(product_dict):
			self.has_variants = 1
			attributes = self._create_attribute(product_dict)
			self._create_item(product_dict, warehouse, 1, attributes)
			self._create_item_variants(product_dict, warehouse, attributes)

		else:
			product_dict["variant_id"] = product_dict["variants"][0]["id"]
			self._create_item(product_dict, warehouse)

	def _create_attribute(self, product_dict):
		attribute = []
		for attr in product_dict.get("options"):
			if not frappe.db.get_value("Item Attribute", attr.get("name"), "name"):
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": attr.get("name"),
						"item_attribute_values": [
							{"attribute_value": attr_value, "abbr": attr_value}
							for attr_value in attr.get("values")
						],
					}
				).insert()
				attribute.append({"attribute": attr.get("name")})

			else:
				# check for attribute values
				item_attr = frappe.get_doc("Item Attribute", attr.get("name"))
				if not item_attr.numeric_values:
					self._set_new_attribute_values(item_attr, attr.get("values"))
					item_attr.save()
					attribute.append({"attribute": attr.get("name")})

				else:
					attribute.append(
						{
							"attribute": attr.get("name"),
							"from_range": item_attr.get("from_range"),
							"to_range": item_attr.get("to_range"),
							"increment": item_attr.get("increment"),
							"numeric_values": item_attr.get("numeric_values"),
						}
					)

		return attribute

	def _set_new_attribute_values(self, item_attr, values):
		for attr_value in values:
			if not any(
				(d.abbr.lower() == attr_value.lower() or d.attribute_value.lower() == attr_value.lower())
				for d in item_attr.item_attribute_values
			):
				item_attr.append("item_attribute_values", {"attribute_value": attr_value, "abbr": attr_value})

	def _create_item(self, product_dict, warehouse, has_variant=0, attributes=None, variant_of=None):
		item_dict = {
			"variant_of": variant_of,
			"is_stock_item": 1,
			"item_code": cstr(product_dict.get("item_code")) or cstr(product_dict.get("id")),
			"item_name": product_dict.get("title", "").strip(),
			"description": product_dict.get("body_html") or product_dict.get("title"),
			"item_group": self._get_item_group(product_dict.get("product_type")),
			"has_variants": has_variant,
			"attributes": attributes or [],
			"stock_uom": product_dict.get("uom") or _("Nos"),
			"sku": product_dict.get("sku") or _get_sku(product_dict),
			"default_warehouse": warehouse,
			"image": _get_item_image(product_dict),
			"weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP[product_dict.get("weight_unit")],
			"weight_per_unit": product_dict.get("weight"),
			"default_supplier": self._get_supplier(product_dict),
		}

		integration_item_code = product_dict["id"]  # shopify product_id
		variant_id = product_dict.get("variant_id", "")  # shopify variant_id if has variants
		sku = item_dict["sku"]

		if not _match_sku_and_link_item(
			item_dict, integration_item_code, variant_id, variant_of=variant_of, has_variant=has_variant
		):
			ecommerce_item.create_ecommerce_item(
				MODULE_NAME,
				integration_item_code,
				item_dict,
				variant_id=variant_id,
				sku=sku,
				variant_of=variant_of,
				has_variants=has_variant,
			)

	def _create_item_variants(self, product_dict, warehouse, attributes):
		template_item = ecommerce_item.get_erpnext_item(
			MODULE_NAME, integration_item_code=product_dict.get("id"), has_variants=1
		)

		if template_item:
			for variant in product_dict.get("variants"):
				shopify_item_variant = {
					"id": product_dict.get("id"),
					"variant_id": variant.get("id"),
					"item_code": variant.get("id"),
					"title": product_dict.get("title", "").strip() + "-" + variant.get("title"),
					"product_type": product_dict.get("product_type"),
					"sku": variant.get("sku"),
					"uom": template_item.stock_uom or _("Nos"),
					"item_price": variant.get("price"),
					"weight_unit": variant.get("weight_unit"),
					"weight": variant.get("weight"),
				}

				for i, variant_attr in enumerate(SHOPIFY_VARIANTS_ATTR_LIST):
					if variant.get(variant_attr):
						attributes[i].update(
							{
								"attribute_value": self._get_attribute_value(
									variant.get(variant_attr), attributes[i]
								)
							}
						)
				self._create_item(shopify_item_variant, warehouse, 0, attributes, template_item.name)

	def _get_attribute_value(self, variant_attr_val, attribute):
		attribute_value = frappe.db.sql(
			"""select attribute_value from `tabItem Attribute Value`
			where parent = %s and (abbr = %s or attribute_value = %s)""",
			(attribute["attribute"], variant_attr_val, variant_attr_val),
			as_list=1,
		)
		return attribute_value[0][0] if len(attribute_value) > 0 else cint(variant_attr_val)

	def _get_item_group(self, product_type=None):
		parent_item_group = get_root_of("Item Group")

		if not product_type:
			return parent_item_group

		if frappe.db.get_value("Item Group", product_type, "name"):
			return product_type
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": product_type,
				"parent_item_group": parent_item_group,
				"is_group": "No",
			}
		).insert()
		return item_group.name

	def _get_supplier(self, product_dict):
		if product_dict.get("vendor"):
			supplier = frappe.db.sql(
				f"""select name from tabSupplier
				where name = %s or {SUPPLIER_ID_FIELD} = %s """,
				(product_dict.get("vendor"), product_dict.get("vendor").lower()),
				as_list=1,
			)

			if supplier:
				return product_dict.get("vendor")
			supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": product_dict.get("vendor"),
					SUPPLIER_ID_FIELD: product_dict.get("vendor").lower(),
					"supplier_group": self._get_supplier_group(),
				}
			).insert()
			return supplier.name
		else:
			return ""

	def _get_supplier_group(self):
		supplier_group = frappe.db.get_value("Supplier Group", _("Shopify Supplier"))
		if not supplier_group:
			supplier_group = frappe.get_doc(
				{"doctype": "Supplier Group", "supplier_group_name": _("Shopify Supplier")}
			).insert()
			return supplier_group.name
		return supplier_group


def _add_weight_details(product_dict):
	variants = product_dict.get("variants")
	if variants:
		product_dict["weight"] = variants[0]["weight"]
		product_dict["weight_unit"] = variants[0]["weight_unit"]


def _has_variants(product_dict) -> bool:
	options = product_dict.get("options")
	return bool(options and "Default Title" not in options[0]["values"])


def _get_sku(product_dict):
	if product_dict.get("variants"):
		return product_dict.get("variants")[0].get("sku")
	return ""


def _get_item_image(product_dict):
	if product_dict.get("image"):
		return product_dict.get("image").get("src")
	return None


def _match_sku_and_link_item(item_dict, product_id, variant_id, variant_of=None, has_variant=False) -> bool:
	"""Tries to match new item with existing item using Shopify SKU == item_code.

	Returns true if matched and linked.
	"""
	sku = item_dict["sku"]
	if not sku or variant_of or has_variant:
		return False

	item_name = frappe.db.get_value("Item", {"item_code": sku})
	if item_name:
		try:
			ecommerce_item = frappe.get_doc(
				{
					"doctype": "Ecommerce Item",
					"integration": MODULE_NAME,
					"erpnext_item_code": item_name,
					"integration_item_code": product_id,
					"has_variants": 0,
					"variant_id": cstr(variant_id),
					"sku": sku,
				}
			)

			ecommerce_item.insert()
			return True
		except Exception:
			return False


def create_items_if_not_exist(order):
	"""Using shopify order, sync all items that are not already synced."""
	for item in order.get("line_items", []):
		product_id = item["product_id"]
		variant_id = item.get("variant_id")
		sku = item.get("sku")
		product = ShopifyProduct(product_id, variant_id=variant_id, sku=sku)

		if not product.is_synced():
			product.sync_product()


def get_item_code(shopify_item):
	"""Get item code using shopify_item dict.

	Item should contain both product_id and variant_id."""

	item = ecommerce_item.get_erpnext_item(
		integration=MODULE_NAME,
		integration_item_code=shopify_item.get("product_id"),
		variant_id=shopify_item.get("variant_id"),
		sku=shopify_item.get("sku"),
	)
	if item:
		return item.item_code


def should_sync_item_to_shopify(item) -> bool:
	if item.has_variants:
		return False
	return bool(item.get(IS_SHOPIFY_ITEM_FIELD))


def get_shopify_product(product_id) -> Product | None:
	"""Fetch Shopify product by id. Returns None if it was deleted (404)."""
	try:
		return Product.find(product_id)
	except ResourceNotFound:
		return None


def delete_shopify_ecommerce_items(erpnext_item_codes: set[str]) -> None:
	"""Remove stale Shopify Ecommerce Item rows so a product can be recreated."""
	if not erpnext_item_codes:
		return

	for name in frappe.get_all(
		"Ecommerce Item",
		filters={
			"integration": MODULE_NAME,
			"erpnext_item_code": ("in", list(erpnext_item_codes)),
		},
		pluck="name",
	):
		frappe.delete_doc("Ecommerce Item", name, force=True, ignore_permissions=True)


def unpublish_shopify_product_on_uncheck(doc, template_item) -> None:
	if not doc.has_value_changed(IS_SHOPIFY_ITEM_FIELD) or doc.get(IS_SHOPIFY_ITEM_FIELD):
		return

	product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": template_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	if not product_id:
		return

	product = get_shopify_product(product_id)
	if not product:
		return

	product.status = "draft"
	product.published = False
	is_successful = product.save()
	write_upload_log(status=is_successful, product=product, item=doc, action="Unpublished")
	if is_successful:
		msgprint(_("Status of linked Shopify product is changed to Draft."))


def _reactivate_shopify_product(product: Product, setting) -> None:
	product.status = "active" if setting.sync_new_item_as_active else "draft"
	product.published = product.status == "active"


@temp_shopify_session
def upload_erpnext_item(doc, method=None):
	"""This hook is called when inserting new or updating existing `Item`.

	New items are pushed to shopify and changes to existing items are
	updated depending on what is configured in "Shopify Setting" doctype.
	"""
	template_item = item = doc  # alias for readability
	# a new item recieved from ecommerce_integrations is being inserted
	if item.flags.from_integration:
		return

	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.upload_erpnext_items:
		return

	if frappe.flags.in_import:
		return

	if item.has_variants:
		return

	if len(item.attributes) > 3:
		msgprint(_("Template items/Items with 4 or more attributes can not be uploaded to Shopify."))
		return

	if doc.variant_of and not setting.upload_variants_as_items:
		msgprint(_("Enable variant sync in setting to upload item to Shopify."))
		return

	if item.variant_of:
		template_item = frappe.get_doc("Item", item.variant_of)

	if not should_sync_item_to_shopify(item):
		unpublish_shopify_product_on_uncheck(doc, template_item)
		return

	product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": template_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)

	product = None
	if product_id:
		product = get_shopify_product(product_id)
		if product is None:
			# Mapping points at a deleted Shopify product — clear and recreate.
			delete_shopify_ecommerce_items({item.name, template_item.name})
			product_id = None

	is_new_product = not bool(product_id)
	is_recheck = doc.has_value_changed(IS_SHOPIFY_ITEM_FIELD) and bool(doc.get(IS_SHOPIFY_ITEM_FIELD))

	if is_new_product:
		product = Product()
		product.published = False
		product.status = "active" if setting.sync_new_item_as_active else "draft"

		map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
		is_successful = product.save()

		if is_successful:
			update_default_variant_properties(
				product,
				sku=template_item.item_code,
				price=template_item.get(ITEM_SELLING_RATE_FIELD),
				is_stock_item=template_item.is_stock_item,
			)
			if item.variant_of:
				product.options = []
				product.variants = []
				variant_attributes = {
					"title": template_item.item_name,
					"sku": item.item_code,
					"price": item.get(ITEM_SELLING_RATE_FIELD),
				}
				max_index_range = min(3, len(template_item.attributes))
				for i in range(0, max_index_range):
					attr = template_item.attributes[i]
					product.options.append(
						{
							"name": attr.attribute,
							"values": frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							),
						}
					)
					try:
						variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
					except IndexError:
						frappe.throw(
							_("Shopify Error: Missing value for attribute {}").format(attr.attribute)
						)
				product.variants.append(Variant(variant_attributes))

			product.save()  # push variant

			ecom_items = list(set([item, template_item]))
			for d in ecom_items:
				ecom_item = frappe.get_doc(
					{
						"doctype": "Ecommerce Item",
						"erpnext_item_code": d.name,
						"integration": MODULE_NAME,
						"integration_item_code": str(product.id),
						"variant_id": "" if d.has_variants else str(product.variants[0].id),
						"sku": "" if d.has_variants else str(product.variants[0].sku),
						"has_variants": d.has_variants,
						"variant_of": d.variant_of,
					}
				)
				ecom_item.insert()

			sync_item_image_to_shopify(product, item)

		write_upload_log(status=is_successful, product=product, item=item)
	elif product and (setting.update_shopify_item_on_update or is_recheck):
		if is_recheck:
			_reactivate_shopify_product(product, setting)

		if setting.update_shopify_item_on_update:
			map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
			if not item.variant_of:
				update_default_variant_properties(
					product,
					is_stock_item=template_item.is_stock_item,
					price=item.get(ITEM_SELLING_RATE_FIELD),
				)
			else:
				variant_attributes = {"sku": item.item_code, "price": item.get(ITEM_SELLING_RATE_FIELD)}
				product.options = []
				max_index_range = min(3, len(template_item.attributes))
				for i in range(0, max_index_range):
					attr = template_item.attributes[i]
					product.options.append(
						{
							"name": attr.attribute,
							"values": frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							),
						}
					)
					try:
						variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
					except IndexError:
						frappe.throw(
							_("Shopify Error: Missing value for attribute {}").format(attr.attribute)
						)
				product.variants.append(Variant(variant_attributes))

		is_successful = product.save()
		if is_successful and item.variant_of and setting.update_shopify_item_on_update:
			map_erpnext_variant_to_shopify_variant(product, item, variant_attributes)

		if is_successful and doc.has_value_changed("image"):
			sync_item_image_to_shopify(product, item)

		action = "Updated"
		if is_recheck and not setting.update_shopify_item_on_update:
			action = "Reactivated"
		write_upload_log(status=is_successful, product=product, item=item, action=action)
	elif product and doc.has_value_changed("image"):
		# Image-only change while field updates are disabled.
		sync_item_image_to_shopify(product, item)


def get_items_pending_shopify_upload(limit: int | None = None) -> list[str]:
	"""Return ERPNext items marked for Shopify that are not linked yet.

	Skips template items (has_variants). Variants are included only when
	"Upload ERPNext Variants as Shopify Items" is enabled.
	"""
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	variant_filter = ""
	if not setting.upload_variants_as_items:
		variant_filter = "AND item.variant_of IS NULL"

	limit_clause = f"LIMIT {cint(limit)}" if limit else ""

	return frappe.db.sql_list(
		f"""
		SELECT item.name
		FROM `tabItem` item
		LEFT JOIN `tabEcommerce Item` ei
			ON ei.erpnext_item_code = item.name
			AND ei.integration = %(integration)s
		WHERE ei.name IS NULL
			AND item.{IS_SHOPIFY_ITEM_FIELD} = 1
			AND item.has_variants = 0
			AND item.disabled = 0
			{variant_filter}
		ORDER BY item.modified
		{limit_clause}
		""",
		{"integration": MODULE_NAME},
	)


def upload_new_items(force=False) -> None:
	"""Upload pending ERPNext items to Shopify in batches.

	Picks up items with Is Shopify Item checked that have no Ecommerce Item
	row yet — including those skipped during Data Import (`frappe.flags.in_import`).

	Processes up to UPLOAD_ITEMS_PER_JOB items per run in batches of
	UPLOAD_BATCH_SIZE. If more items remain, another background job is enqueued
	so large imports (e.g. 1000 items) complete across chained jobs.

	Called hourly by the scheduler and manually from Shopify Setting.
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled() or not setting.upload_erpnext_items:
		return

	# Fetch one extra to detect whether another job is needed after this run.
	item_codes = get_items_pending_shopify_upload(limit=UPLOAD_ITEMS_PER_JOB + 1)
	if not item_codes:
		return

	has_more = len(item_codes) > UPLOAD_ITEMS_PER_JOB
	item_codes = item_codes[:UPLOAD_ITEMS_PER_JOB]

	log = create_shopify_log(
		status="Queued",
		message=_("Bulk item upload started ({0} items in this job{1})").format(
			len(item_codes),
			_(", more pending") if has_more else "",
		),
		method="upload_new_items",
		make_new=True,
	)

	synced_items: list[str] = []
	failed_items: list[str] = []

	for batch in create_batch(item_codes, UPLOAD_BATCH_SIZE):
		for item_code in batch:
			try:
				item = frappe.get_doc("Item", item_code)
				upload_erpnext_item(item)

				if frappe.db.exists(
					"Ecommerce Item",
					{"erpnext_item_code": item_code, "integration": MODULE_NAME},
				):
					synced_items.append(item_code)
				else:
					failed_items.append(item_code)
			except Exception:
				failed_items.append(item_code)
				create_shopify_log(
					status="Error",
					message=_("Failed to upload item {0} during bulk sync").format(item_code),
					method="upload_new_items",
					make_new=True,
				)
			finally:
				frappe.db.commit()
				if not frappe.flags.in_test:
					time.sleep(UPLOAD_ITEM_DELAY_SECONDS)

		# Progress checkpoint after each batch of 50
		log.db_set(
			"message",
			_(
				"Bulk item upload in progress — synced: {0}, failed: {1}, remaining in job: {2}"
			).format(
				len(synced_items),
				len(failed_items),
				len(item_codes) - len(synced_items) - len(failed_items),
			),
			update_modified=False,
		)

	if failed_items and synced_items:
		status = "Partial Success"
	elif failed_items:
		status = "Error"
	else:
		status = "Success"

	log.status = status
	log.message = (
		_("Bulk item upload job completed")
		+ f"\n{_('Synced')}: {len(synced_items)}"
		+ f"\n{_('Failed')}: {len(failed_items)}"
	)
	if failed_items:
		# List failures only — synced list can be hundreds of codes.
		shown = failed_items[:50]
		log.message += f"\n{_('Failed items')}: {', '.join(shown)}"
		if len(failed_items) > 50:
			log.message += _(" (and {0} more)").format(len(failed_items) - 50)
	if has_more:
		log.message += "\n" + _("More items pending — queuing next batch job.")
	log.save(ignore_permissions=True)
	frappe.db.commit()

	if has_more:
		_enqueue_upload_job(force=force)


def get_pending_shopify_upload_count() -> int:
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	variant_filter = ""
	if not setting.upload_variants_as_items:
		variant_filter = "AND item.variant_of IS NULL"

	return cint(
		frappe.db.sql(
			f"""
			SELECT COUNT(*)
			FROM `tabItem` item
			LEFT JOIN `tabEcommerce Item` ei
				ON ei.erpnext_item_code = item.name
				AND ei.integration = %(integration)s
			WHERE ei.name IS NULL
				AND item.{IS_SHOPIFY_ITEM_FIELD} = 1
				AND item.has_variants = 0
				AND item.disabled = 0
				{variant_filter}
			""",
			{"integration": MODULE_NAME},
		)[0][0]
	)


def _enqueue_upload_job(force: bool = False) -> None:
	# Unique job name so chained batch jobs are not blocked by the finishing one.
	frappe.enqueue(
		upload_new_items,
		queue="long",
		timeout=3600,
		job_name=f"{UPLOAD_NEW_ITEMS_JOB}.{frappe.generate_hash(length=6)}",
		enqueue_after_commit=True,
		force=force,
	)


@frappe.whitelist()
def enqueue_upload_new_items() -> None:
	"""Queue a background job to upload pending Shopify items."""
	frappe.only_for("System Manager")

	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled() or not setting.upload_erpnext_items:
		frappe.throw(_("Enable Shopify and 'Upload new ERPNext Items to Shopify' first."))

	pending_count = get_pending_shopify_upload_count()
	if not pending_count:
		frappe.msgprint(_("No pending items to upload to Shopify."))
		return

	_enqueue_upload_job(force=True)
	jobs_needed = (pending_count + UPLOAD_ITEMS_PER_JOB - 1) // UPLOAD_ITEMS_PER_JOB
	frappe.msgprint(
		_(
			"Queued upload of {0} item(s) to Shopify — {1} job(s),"
			" batches of {2}, max {3} items per job."
			" Check Ecommerce Integration Log for progress."
		).format(pending_count, jobs_needed, UPLOAD_BATCH_SIZE, UPLOAD_ITEMS_PER_JOB)
	)


def map_erpnext_variant_to_shopify_variant(shopify_product: Product, erpnext_item, variant_attributes):
	variant_product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": erpnext_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	if not variant_product_id:
		for variant in shopify_product.variants:
			if (
				variant.option1 == variant_attributes.get("option1")
				and variant.option2 == variant_attributes.get("option2")
				and variant.option3 == variant_attributes.get("option3")
			):
				variant_product_id = str(variant.id)
				if not frappe.flags.in_test:
					frappe.get_doc(
						{
							"doctype": "Ecommerce Item",
							"erpnext_item_code": erpnext_item.name,
							"integration": MODULE_NAME,
							"integration_item_code": str(shopify_product.id),
							"variant_id": variant_product_id,
							"sku": str(variant.sku),
							"variant_of": erpnext_item.variant_of,
						}
					).insert()
				break
		if not variant_product_id:
			msgprint(_("Shopify: Couldn't sync item variant."))
	return variant_product_id


def map_erpnext_item_to_shopify(shopify_product: Product, erpnext_item):
	"""Map erpnext fields to shopify, called both when updating and creating new products."""

	shopify_product.title = erpnext_item.item_name
	shopify_product.body_html = erpnext_item.description
	shopify_product.product_type = erpnext_item.item_group

	if erpnext_item.weight_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.values():
		# reverse lookup for key
		uom = get_shopify_weight_uom(erpnext_weight_uom=erpnext_item.weight_uom)
		shopify_product.weight = erpnext_item.weight_per_unit
		shopify_product.weight_unit = uom

	if erpnext_item.disabled:
		shopify_product.status = "draft"
		shopify_product.published = False
		msgprint(_("Status of linked Shopify product is changed to Draft."))


def sync_item_image_to_shopify(shopify_product: Product, erpnext_item) -> None:
	"""Upload or clear the Shopify product image from the ERPNext Item image field.

	Uses base64 attachment for site files so private/local sites work. Failures are
	logged and do not block Item save.
	"""
	if not shopify_product or not shopify_product.id:
		return

	try:
		_clear_shopify_product_images(shopify_product)

		image_url = erpnext_item.get("image")
		if not image_url:
			return

		shopify_image = Image({"product_id": shopify_product.id})

		if image_url.startswith(("http://", "https://")):
			shopify_image.src = image_url
		else:
			content, filename = _get_erpnext_image_content(image_url)
			if not content:
				create_shopify_log(
					status="Error",
					message=_("Could not read Item image file: {0}").format(image_url),
					method="sync_item_image_to_shopify",
				)
				return
			shopify_image.attach_image(content, filename=filename)

		if not shopify_image.save():
			errors = (
				", ".join(shopify_image.errors.full_messages())
				if getattr(shopify_image, "errors", None)
				else _("Unknown error")
			)
			create_shopify_log(
				status="Error",
				request_data=shopify_image.to_dict() if hasattr(shopify_image, "to_dict") else {},
				message=_("Failed to sync Item image to Shopify: {0}").format(errors),
				method="sync_item_image_to_shopify",
			)
	except Exception:
		create_shopify_log(
			status="Error",
			message=_("Failed to sync Item image to Shopify"),
			method="sync_item_image_to_shopify",
			exception=frappe.get_traceback(),
		)


def _clear_shopify_product_images(shopify_product: Product) -> None:
	"""Remove existing Shopify product images before replacing them."""
	try:
		existing_images = Image.find(product_id=shopify_product.id)
	except ResourceNotFound:
		return

	for existing in existing_images or []:
		try:
			existing.destroy()
		except Exception:
			pass


def _get_erpnext_image_content(image_url: str) -> tuple[bytes | None, str | None]:
	"""Return (file bytes, filename) for an ERPNext File URL like /files/... or /private/files/..."""
	file_name = frappe.db.get_value("File", {"file_url": image_url}, "name")
	if not file_name:
		# Item.image sometimes stores only the file name
		file_name = frappe.db.get_value("File", {"file_name": image_url.rsplit("/", 1)[-1]}, "name")

	if not file_name:
		return None, None

	file_doc = frappe.get_doc("File", file_name)
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode("utf-8")

	filename = file_doc.file_name or image_url.rsplit("/", 1)[-1]
	return content, filename


def get_shopify_weight_uom(erpnext_weight_uom: str) -> str:
	for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
		if erpnext_uom == erpnext_weight_uom:
			return shopify_uom


def update_default_variant_properties(
	shopify_product: Product,
	is_stock_item: bool,
	sku: str | None = None,
	price: float | None = None,
):
	"""Shopify creates default variant upon saving the product.

	Some item properties are supposed to be updated on the default variant.
	Input: saved shopify_product, sku and price
	"""
	default_variant: Variant = shopify_product.variants[0]

	# this will create Inventory item and qty will be updated by scheduled job.
	if is_stock_item:
		default_variant.inventory_management = "shopify"

	if price is not None:
		default_variant.price = price
	if sku is not None:
		default_variant.sku = sku


def write_upload_log(status: bool, product: Product, item, action="Created") -> None:
	if not status:
		msg = _("Failed to upload item to Shopify") + "<br>"
		msg += _("Shopify reported errors:") + " " + ", ".join(product.errors.full_messages())
		msgprint(msg, title="Note", indicator="orange")

		create_shopify_log(
			status="Error",
			request_data=product.to_dict(),
			message=msg,
			method="upload_erpnext_item",
		)
	else:
		create_shopify_log(
			status="Success",
			request_data=product.to_dict(),
			message=f"{action} Item: {item.name}, shopify product: {product.id}",
			method="upload_erpnext_item",
		)
