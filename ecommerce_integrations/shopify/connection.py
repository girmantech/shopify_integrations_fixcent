import base64
import functools
import hashlib
import hmac
import json

import frappe
from frappe import _
from shopify.collection import PaginatedIterator
from shopify.resources import Webhook
from shopify.session import Session

from ecommerce_integrations.shopify.constants import (
	API_VERSION,
	EVENT_MAPPER,
	SETTING_DOCTYPE,
	WEBHOOK_EVENTS,
)
from ecommerce_integrations.shopify.utils import create_shopify_log

CALLBACK_PATH = "/api/method/ecommerce_integrations.shopify.connection.store_request_data"


def temp_shopify_session(func):
	"""Any function that needs to access shopify api needs this decorator.
	The decorator starts a temp session that's destroyed when function returns.

	Supports both Static Token and OAuth 2.0 Client Credentials authentication.
	For OAuth, automatically refreshes token if expired or expiring soon.
	"""

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		# no auth in testing
		if frappe.flags.in_test:
			return func(*args, **kwargs)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		if setting.is_enabled():
			access_token = _get_access_token(setting)
			auth_details = (setting.shopify_url, API_VERSION, access_token)

			with Session.temp(*auth_details):
				return func(*args, **kwargs)

	return wrapper


def _get_access_token(setting):
	"""
	Get the appropriate access token based on authentication method.
	For OAuth, ensures token is valid and refreshes if needed.
	"""
	if setting.authentication_method == "OAuth 2.0 Client Credentials":
		# Import here to avoid circular dependency
		from ecommerce_integrations.shopify.oauth import get_valid_access_token

		try:
			return get_valid_access_token(setting)
		except Exception as e:
			create_shopify_log(
				status="Error",
				method="ecommerce_integrations.shopify.connection._get_access_token",
				message=_("Failed to get valid OAuth access token"),
				exception=str(e),
			)
			frappe.throw(
				_("Failed to authenticate with Shopify using OAuth 2.0: {0}").format(str(e)),
				title=_("Authentication Error"),
			)
	else:
		# Static Token authentication (legacy / pre-Jan 2026 apps)
		token = setting.get_password("password", raise_exception=False)
		if not token:
			frappe.throw(
				_("Shopify access token is not configured"),
				title=_("Authentication Error"),
			)
		return token


def _iter_webhooks():
	"""Yield all webhooks for the current Shopify session (all pages)."""
	for page in PaginatedIterator(Webhook.find()):
		yield from page


def _webhook_belongs_to_site(webhook, callback_url: str, domain: str) -> bool:
	address = getattr(webhook, "address", None) or ""
	if not address:
		return False
	if address == callback_url:
		return True
	# Domain match covers slight URL drift; still require our callback path so we
	# do not touch unrelated shop webhooks on the same hostname.
	return bool(domain) and domain in address and CALLBACK_PATH in address


def _existing_webhooks_for_callback(callback_url: str) -> dict[str, Webhook]:
	"""Map topic -> webhook already registered for this site's callback."""
	domain = get_current_domain_name()
	existing = {}
	for webhook in _iter_webhooks():
		if _webhook_belongs_to_site(webhook, callback_url, domain):
			existing[webhook.topic] = webhook
	return existing


def _is_already_taken_error(errors) -> bool:
	messages = errors if isinstance(errors, (list, tuple)) else [errors]
	return any("already been taken" in str(message).lower() for message in messages)


def register_webhooks(shopify_url: str, password: str) -> list[Webhook]:
	"""Register required webhooks with shopify and return registered webhooks.

	Idempotent: reuses webhooks that already point at this site's callback URL
	(avoids Shopify's "address for this topic has already been taken" when the
	local webhook child table is out of sync with Shopify).
	"""
	new_webhooks = []
	callback_url = get_callback_url()

	with Session.temp(shopify_url, API_VERSION, password):
		# Drop stale callbacks for this integration on other hosts (old localtunnel URLs)
		# so Shopify delivers to the current site only.
		_delete_stale_callbacks(callback_url)

		existing_by_topic = _existing_webhooks_for_callback(callback_url)

		for topic in WEBHOOK_EVENTS:
			if topic in existing_by_topic:
				new_webhooks.append(existing_by_topic[topic])
				continue

			webhook = Webhook.create({"topic": topic, "address": callback_url, "format": "json"})

			if webhook.is_valid():
				new_webhooks.append(webhook)
				continue

			errors = webhook.errors.full_messages()
			if _is_already_taken_error(errors):
				# Unregister/list raced or filter missed it — reclaim from Shopify.
				existing_by_topic = _existing_webhooks_for_callback(callback_url)
				if topic in existing_by_topic:
					new_webhooks.append(existing_by_topic[topic])
					continue

			create_shopify_log(
				status="Error",
				response_data=webhook.to_dict(),
				exception=errors,
			)

	return new_webhooks


def _delete_stale_callbacks(callback_url: str) -> None:
	"""Remove stale *tunnel* callbacks for this integration.

	Only prunes other localtunnel/ngrok hosts so rotating the tunnel URL does not
	leave Shopify posting to a dead tunnel. Never deletes production/public hosts.
	"""
	for webhook in _iter_webhooks():
		address = getattr(webhook, "address", None) or ""
		if CALLBACK_PATH not in address or address == callback_url:
			continue
		if ".loca.lt/" in address or ".loca.lt?" in address or address.endswith(".loca.lt") or "ngrok" in address:
			webhook.destroy()


def unregister_webhooks(shopify_url: str, password: str) -> None:
	"""Unregister all webhooks from shopify that correspond to current site url."""
	callback_url = get_callback_url()
	domain = get_current_domain_name()

	with Session.temp(shopify_url, API_VERSION, password):
		for webhook in _iter_webhooks():
			if _webhook_belongs_to_site(webhook, callback_url, domain):
				webhook.destroy()


def get_current_domain_name() -> str:
	"""Get current site domain name. E.g. test.erpnext.com

	If developer_mode is enabled and localtunnel_url is set in site config then domain  is set to localtunnel_url.
	"""
	if frappe.conf.developer_mode and frappe.conf.localtunnel_url:
		return frappe.conf.localtunnel_url
	else:
		return frappe.request.host


def get_callback_url() -> str:
	"""Shopify calls this url when new events occur to subscribed webhooks.

	If developer_mode is enabled and localtunnel_url is set in site config then callback url is set to localtunnel_url.
	"""
	url = get_current_domain_name()

	return f"https://{url}{CALLBACK_PATH}"


@frappe.whitelist(allow_guest=True)
def store_request_data() -> None:
	if frappe.request:
		hmac_header = frappe.get_request_header("X-Shopify-Hmac-Sha256")

		_validate_request(frappe.request, hmac_header)

		data = json.loads(frappe.request.data)
		event = frappe.request.headers.get("X-Shopify-Topic")

		process_request(data, event)


def process_request(data, event):
	# create log
	log = create_shopify_log(method=EVENT_MAPPER[event], request_data=data)

	# enqueue backround job
	frappe.enqueue(
		method=EVENT_MAPPER[event],
		queue="short",
		timeout=300,
		is_async=True,
		**{"payload": data, "request_id": log.name},
	)


def _validate_request(req, hmac_header):
	settings = frappe.get_doc(SETTING_DOCTYPE)

	# Get the appropriate secret key based on authentication method
	if settings.authentication_method == "OAuth 2.0 Client Credentials":
		# For OAuth apps, use client_secret for HMAC validation
		secret_key = settings.get_password("client_secret", raise_exception=False)
	else:
		# For static token apps, use shared_secret
		secret_key = settings.shared_secret

	if not secret_key:
		create_shopify_log(status="Error", request_data=req.data, exception="Secret key not configured")
		frappe.throw(_("Webhook validation failed: Secret key not configured"))

	if not hmac_header:
		create_shopify_log(status="Error", request_data=req.data, exception="Missing HMAC header")
		frappe.throw(_("Unverified Webhook Data"))

	sig = base64.b64encode(hmac.new(secret_key.encode("utf8"), req.data, hashlib.sha256).digest())

	# Timing-safe comparison to prevent signature timing attacks
	if not hmac.compare_digest(sig, hmac_header.encode()):
		create_shopify_log(status="Error", request_data=req.data)
		frappe.throw(_("Unverified Webhook Data"))
