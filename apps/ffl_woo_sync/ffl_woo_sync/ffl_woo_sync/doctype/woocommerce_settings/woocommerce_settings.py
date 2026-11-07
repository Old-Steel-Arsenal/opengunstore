"""WooCommerce Settings — single doctype storing API credentials and sync state."""
from __future__ import annotations

from frappe.model.document import Document


class WooCommerceSettings(Document):
	def on_update(self):
		from ffl_woo_sync.woocommerce.customer_events import on_settings_update

		on_settings_update(self)
