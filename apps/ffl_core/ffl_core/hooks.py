app_name = "ffl_core"
app_title = "OpenGunstore"
app_publisher = "OpenGunstore"
app_description = "FFL gun store core: Item/Customer extensions, A&D doctypes, Intake Tool"
app_email = "info@oldsteelarsenal.com"
app_license = "GNU General Public License (v3)"

# After-login landing for Desk users. This value is get_home_page()'s answer,
# and login.js:230/249 feeds it to `window.location.href` on the /login page,
# where the browser resolves it RELATIVE to /login. It MUST carry the "desk/"
# prefix: a bare "gun-shop" resolves to /gun-shop — a website route that 404s
# — whereas "desk/gun-shop" resolves to
# /desk/gun-shop, served by the desk shell. get_home_page() strips leading
# slashes (website/utils.py:127), so the stored form is prefix-only, no slash.
#
# This is the LAST resort of website.utils.get_home_page(), and it answers for
# EVERY user, not just Desk ones. A portal user must therefore be diverted
# earlier — but never via Role.home_page or role_home_page below, because both
# are `for role in frappe.get_roles()` loops and Administrator implicitly holds
# every role on the site (permissions.py:544). Stamping the dealer role's
# home_page is what sent the store owner to /consignment. Divert
# portal users from get_website_user_home_page, the one callable in the chain.
#
# The landing is the Overview page inside the Gun Shop shell (page store-overview), not the shell's
# workspace: the page's role list covers every role below (roles.OVERVIEW_ROLES).
_LANDING = "desk/gun-shop/store-overview"
home_page = _LANDING
role_home_page = {
	"System Manager": _LANDING,
	"Sales User": _LANDING,
	"Stock User": _LANDING,
	"Stock Manager": _LANDING,
	"Accounts User": _LANDING,
}
# Asks the extensions' providers (hooks gunshop_website_user_home_page) where a
# Website User lands and returns None for everyone else, so staff fall through
# to role_home_page / home_page above.
# Outranks both of them, and Portal Settings; is outranked by Role.home_page,
# which is why that field must stay empty on every role. See ffl_core/__init__.py.
get_website_user_home_page = "ffl_core.get_website_user_home_page"

required_apps = ["erpnext"]

# Each entry surfaces as a tile in the app launcher (top-left brand-logo
# dropdown on every Desk page) AND in the desk landing tile selector.
# `has_permission` is NOT cosmetic: frappe.apps.get_default_path() reads this
# same list, and auth.py:201 uses ITS answer as a Website User's post-login
# landing page — ahead of home_page / role_home_page below. Without the gate a
# dealer's login resolved to this route and dead-ended on "Not Permitted"
#. erpnext gates its own tile the same way.
#
# `route` must be a desk route (^/desk, apps.py:DESK_APP_PATTERN). get_apps()
# drops the "frappe" tile, so a System User's surviving tiles are erpnext
# ("/desk") and this one; get_default_path() returns "/desk" only when EVERY
# one of them matches the pattern. "/app/gun-shop" did not, so is_desk_apps()
# was False and the update-password first login / apps screen resolved to
# "/apps" (the tile picker) instead of the desk. "/desk/gun-shop" matches.
# "What's New in POS" sits at the top level of the sidebar-header menu (the
# store name at the top of the sidebar), synced into Navbar Settings.
# settings_dropdown by frappe on every migrate (navbar_settings.
# sync_standard_items: label-deduped, removed again if this entry goes away).
# Not under Help: the Display / Reload / Help group is only built for users
# whose Desk Settings allow notifications, and it is three clicks deep. Opens
# every release note, see api/changelog.py and public/js/ffl_core.bundle.js.
standard_navbar_items = [
	{
		"item_label": "What's New in POS",
		"item_type": "Action",
		"action": "gunstore.showWhatsNew()",
		"is_standard": 1,
	},
]

add_to_apps_screen = [
	{
		# The app id on the apps screen = the Python app: the Gun Shop Sidebar
		# ffl_core ships (app ffl_core) is this shell's (setup/sidebar.py).
		"name": "ffl_core",
		# A new file name, not new bytes under the old one: the asset carries no Cache-Control, so a
		# browser keeps an image it fetched long ago for days and would go on showing the old logo.
		"logo": "/assets/ffl_core/images/gunstore_logo.svg",
		"title": "OpenGunstore",
		"route": "/" + _LANDING,
		"has_permission": "ffl_core.check_app_permission",
	},
]

# Deploy-window backstop for the home_page rename above. get_home_page()
# memoises its answer per user in a redis hash; a `bench migrate` clears it,
# but old bookmarks and any request in the gap before the cache turns over can
# still emit the bare "/gun-shop". Redirect it into the desk shell so it never
# 404s. Cheap and permanent — /gun-shop is not, and never was, a real route.
website_redirects = [
	{"source": "/gun-shop", "target": "/" + _LANDING},
	# A short address to type on the signing tablet (www/agreement/terminal).
	# 302, not the default 301: Safari keeps a 301 forever, so the target stays movable.
	{"source": "/kiosk", "target": "/agreement/terminal", "redirect_http_status": 302},
]

fixtures = [
	{
		"dt": "Custom Field",
		"filters": [["module", "=", "FFL Core"]],
	},
	{
		"dt": "Property Setter",
		"filters": [["module", "=", "FFL Core"]],
	},
	{
		"dt": "Email Template",
		"filters": [["name", "in", ["Sale Receipt", "Agreement Signing Link", "Agreement Signed Copy", "Gunsmith Work Ready"]]],
	},
	{
		"dt": "Print Format",
		"filters": [["name", "in", ["Sales Receipt", "POS Receipt"]]],
	},
	# NOTE: Letter Head is deliberately NOT a fixture. It shipped as one store's
	# letterhead, and every migrate rewrote it — so a second store's site could
	# never keep its own default. ERPNext's built-in "Company Letterhead"
	# templates cover a fresh site; each store configures its own from there.
]

# Frontend asset injected on every Desk page; the script self-gates to the POS route.
# customer_display_logic.js is the dependency-free core shared with the standalone
# customer-display popup (and the Node test); the bundle's sender IIFE reads it as
# window.FFLCustomerDisplay, so the desk must load it too.
# pos_tender_guard.js exposes window.ffl.hasLiveTender — the pure predicate the
# bundle's resume paths (loadInvoiceIntoCart + the edit_order monkey-patch) read to
# preserve a held order's tender.
# card_charges.js exposes window.ffl.cardCharges — the pure multi-card
# charge math (live rows, remaining balance, slice validation, void picker)
# shared by the bundle's charge loop and pos_invoice.js, Node-tested.
# pos_recycle_bin.js adds the "Deleted" filter to the POS Recent Orders list
# (restore a deleted draft on the spot — a past incident); it
# needs ffl.installClassHook from pos_tender_guard.js, so it loads after it.
# Standalone (non-bundle) includes get no content hash from frappe's build, so
# browsers kept serving stale copies after a deploy (real scan, 2026-09-09:
# the POS still ran the previous aamva.js). Append the file's own content hash
# as a query string — changes with every edit, stable otherwise.
def _hashed(rel_path: str) -> str:
	import hashlib
	import os

	full = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", rel_path)
	try:
		with open(full, "rb") as f:
			digest = hashlib.md5(f.read()).hexdigest()[:10]
	except OSError:
		return f"/assets/ffl_core/{rel_path}"
	return f"/assets/ffl_core/{rel_path}?v={digest}"


# Extension SDK (since core 0.1.0): frappe.boot.gunshop_extensions = {id: {enabled, version, title}}
# is what gunstore.extensions renders from.
# A password sign-in turns a counter PIN that wrong tries switched off back on (staff_switch.py).
on_login = ["ffl_core.staff_switch.on_password_login"]
# Wrong counter-PIN tries outlive `bench clear-cache` (staff_switch.PIN_FAIL_KEY).
persistent_cache_keys = ["ffl-pin-fail:*"]

extend_bootinfo = [
	"ffl_core.extensions.extend_bootinfo",
	# Idle lock minutes and whether this session's screen is locked (staff_switch.py).
	"ffl_core.staff_switch.extend_bootinfo",
	# Navbar Settings' Dark Mode Logo for the dark desk theme (store_identity.py).
	"ffl_core.store_identity.extend_bootinfo",
	# The peace-officer exempt excise account, for the register's LE Exempt button.
	"ffl_core.compliance.le_excise_exempt.extend_bootinfo",
	# Whether this user's screens ask a manager before saving a lowered price (price_approval.py).
	"ffl_core.price_approval.extend_bootinfo",
	# The US state codes for the address forms' State dropdowns (interstate_tax.US_STATES).
	"ffl_core.compliance.interstate_tax.extend_bootinfo",
]


app_include_js = [
	"ffl_core.bundle.js",
	# extensions_registry.js defines gunstore.extensions (pure, Node-tested) —
	# it must be on the page before any extension bundle calls register().
	_hashed("js/extensions_registry.js"),
	# ffl.capabilities: frappe.boot.ffl_capabilities and the one "not set up" notice
	# (pure kernel, Node-tested); before the pages and the bundle's picker use it.
	_hashed("js/capability_notice.js"),
	_hashed("js/customer_display_logic.js"),
	_hashed("js/pos_tender_guard.js"),
	_hashed("js/card_charges.js"),
	_hashed("js/pos_recycle_bin.js"),
	# Recent Orders › Work Orders: open gunsmith jobs and their Complete (ffl.gunsmithRegister in the
	# bundle above); needs ffl.installClassHook from pos_tender_guard.js above.
	_hashed("js/pos_work_orders.js"),
	# Cart "Charge exactly" row (tax-inclusive total → line rates); needs pos_tender_guard.js above.
	_hashed("js/pos_charge_exactly.js"),
	# Recent Orders draft chips (paid / entered / 4473); needs pos_tender_guard.js above.
	_hashed("js/pos_order_status.js"),
	# Desk password fields: autocomplete="new-password", so the browser never fills
	# the store's saved login into a key/secret pair on some form.
	_hashed("js/password_no_autofill.js"),
	# aamva.js parses a driver-licence PDF417 payload (pure); customer_quick_entry.js
	# subclasses ERPNext's CustomerQuickEntryForm (name parts once + Scan ID).
	_hashed("js/aamva.js"),
	# customer_flags.js: the counter customer panel (Banned box + manager override,
	# alerts, status strip, Scan ID lookup, duplicate check) — needs pos_tender_guard.js
	# (installClassHook) and aamva.js above; customer_quick_entry.js calls it.
	_hashed("js/customer_flags.js"),
	_hashed("js/customer_quick_entry.js"),
	# customer_memberships.js: the Customer form's Memberships panel and ffl.memberships, the
	# membership actions the Memberships page shares with it (membership.py).
	_hashed("js/customer_memberships.js"),
	# ffl.agreements.request: the Request agreement dialog (QR code / email / paper)
	# the Customer form and the counter panel's agreement chips open.
	_hashed("js/agreement_actions.js"),
	# Switch staff from the sidebar's bottom-left corner, and the idle lock screen.
	_hashed("js/staff_switch.js"),
	# A lowered price asks a manager (PIN + reason) before the save: POS, the three forms, Create
	# Order (price_approval.py). Needs staff_switch.js above (authMode / serverMessage).
	_hashed("js/price_approval.js"),
]

doc_events = {
	# A copy requested for a gunsmith work order is its intake authorization (gunsmith.py).
	"Customer Agreement": {"on_update": "ffl_core.gunsmith.link_intake_agreement"},
	# A store flipping an integration's switch re-shapes the Gun Shop sidebar and home page
	# (setup/gunshop_sidebar.py).
	"WooCommerce Settings": {"on_update": "ffl_core.setup.gunshop_sidebar.on_settings_update"},
	"GunBroker Settings": {"on_update": "ffl_core.setup.gunshop_sidebar.on_settings_update"},
	"Payroc Settings": {"on_update": "ffl_core.setup.gunshop_sidebar.on_settings_update"},
	# A journal entry the cash drawer made is cancelled only from the Cash Drawer page (cash_drawer._cancel_je).
	"Journal Entry": {
		"validate": "ffl_core.cash_drawer.refuse_drawer_je_copies",
		"before_cancel": "ffl_core.cash_drawer.refuse_drawer_je_cancel",
	},
	# Dealer parties: keep license fields synced from the linked ATF FFL
	# Record (and backfill the link from a bare matching ffl_number).
	"Customer": {
		# Individual name parts ↔ Customer Name (customer_name.py): compose
		# before autoname takes the docname, and again on every save.
		"before_insert": "ffl_core.customer_name.sync_individual_name",
		"validate": [
			"ffl_core.customer_name.sync_individual_name",
			"ffl_core.atf_link.sync_party_atf_link",
			# Status / ban reason, the Store Customer ID Numbers switch (blanks
			# DOB + ID numbers when off), one-level family (customer_profile.py).
			"ffl_core.customer_profile.validate_customer",
		],
		# An ID document's attachment is always a private file.
		"on_update": "ffl_core.customer_profile.privatize_id_attachments",
		# A head of family with members cannot be deleted.
		"on_trash": "ffl_core.customer_profile.refuse_delete_with_family",
	},
	"Supplier": {
		"validate": "ffl_core.atf_link.sync_party_atf_link",
	},
	"Item": {
		"before_save": "ffl_core.firearm.auto_set_is_firearm",
		# Runs ahead of ERPNext's validate_barcode so a 13-digit GTIN entered in
		# the single UPC field is re-typed to EAN before core rejects it as an
		# invalid UPC.
		"before_validate": "ffl_core.item_hooks.sync_upc_barcode",
		"validate": [
			"ffl_core.item_hooks.sync_item_image_gallery",
			# Block flagging an Item antique once it has bound-book firearms.
			"ffl_core.firearm.block_antique_on_booked_item",
			# Refuse a sale price WooCommerce would silently drop (>= regular).
			"ffl_core.firearm.validate_item_sale_price",
		],
		# Keep the POS-facing Item Price honest: create-if-missing from
		# standard_rate; uniform-priced firearms hard-sync (card = picker price).
		"on_update": "ffl_core.item_hooks.sync_standard_selling_price",
	},
	"Serial No": {
		# Inherit a firearm's manufacturer (never importer — that's per-gun only,
		# 2026-08-20) from its model Item when the serial doesn't set its own, so
		# the by-serial form matches the Firearms In Stock report (which COALESCEs
		# blank serial fields to the Item — importer excluded there too).
		"before_save": "ffl_core.firearm.inherit_serial_firearm_attrs",
		# Keep the gunsmith ToDo queue in sync when needs_service is toggled
		# via the standard Serial No form or REST (the pill toggle goes
		# through ffl_core.firearm.toggle_serial_service_need directly).
		"on_update": "ffl_core.firearm.manage_service_todo",
		# is_consignment / consignor are editable on the Serial No form (setup/
		# consignment.py) so staff can flag an in-stock gun missed at intake;
		# require a consignor + default the commission % when flagged.
		"validate": [
			# Where a gun sits is written only by ffl_core.storage.move.
			"ffl_core.storage.guard_serial_location",
			"ffl_core.compliance.consignment.validate_consignment_serial",
			# Refuse a sale price WooCommerce would silently drop (>= regular).
			"ffl_core.firearm.validate_serial_sale_price",
			# Same photo model as Item: the gallery is the full set, exactly one
			# row is primary and `image` mirrors it. The photo manager
			# (api/photos.py) relies on this running here too.
			"ffl_core.item_hooks.sync_item_image_gallery",
		],
	},
	"Sales Invoice": {
		# The timeline comment of a price approval the save recorded (price_approval.py).
		"on_update": "ffl_core.price_approval.post_approval_comment",
		# Units a web order is waiting on are not the counter's to sell (a counter
		# order that ships from the shelf; web orders' own invoices are skipped).
		"before_submit": [
			"ffl_core.compliance.web_hold.refuse_held_units",
			# The customer must have signed what the sale's Items (or extensions) require
			# (agreements.refuse_missing_required; no override — sign or a paper copy).
			"ffl_core.agreements.refuse_missing_required",
		],
		# Dealer firearm invoices ship from the invoice unless a DN is in
		# play — default Update Stock on at creation (once; unchecking sticks).
		"before_insert": [
			"ffl_core.compliance.dealer_transfer.auto_enable_update_stock_for_dealer_shipment",
			# A new invoice's freight tax basis from FFL Settings.
			"ffl_core.compliance.shipping_charge.stamp_shipping_tax_basis",
		],
		# Must run before core validate: ERPNext's calculate_taxes_and_totals()
		# round_floats_in()'s every Currency/Float/Percent field on each item row
		# — including this custom one — which turns a truly-blank
		# consignment_commission_pct (None) into 0.0 before any `validate`
		# doc_event runs, indistinguishable from an explicit 0% override. See
		# the Item doc_events above for the same "before core mangles the
		# field" pattern (sync_upc_barcode / before_validate).
		"before_validate": [
			# No Desk credit note against a merged invoice holding a negative trade-in
			# ticket (compliance/trade_in_settlement): first, before core totals it.
			"ffl_core.compliance.trade_in_settlement.refuse_desk_return_of_negative_ticket",
			# A credit note against a membership invoice is not deferred (membership.py).
			"ffl_core.membership.book_refunds_at_once",
			"ffl_core.compliance.consignment.freeze_consignment_commission",
			# Locked lines (trade-in credit) pinned to their amount BEFORE core
			# prices, totals and taxes the rows.
			"ffl_core.compliance.locked_lines.pin_locked_lines",
		],
		"validate": [
			# No counter sale to a Banned customer without a manager's open override
			# (customer_flags.refuse_banned_customer; returns and channel mirrors pass).
			"ffl_core.customer_flags.refuse_banned_customer",
			# A price a person lowered saves only with a manager's approval (price_approval.py).
			"ffl_core.price_approval.enforce_price_approval",
			# Owner policy: consignment guns are never returnable — block before
			# any other validation bothers running. See the function's own
			# docstring for why this fires on every validate, not just submit.
			"ffl_core.compliance.consignment.block_consignment_returns",
			"ffl_core.compliance.zero_cost_sale_alert.warn_on_unpriced_firearm_sale",
			"ffl_core.compliance.firearm.canonicalize_row_serials",
			"ffl_core.compliance.firearm.require_serial_for_firearm",
			"ffl_core.compliance.firearm.require_4473_and_nics_for_firearm_sale",
			"ffl_core.compliance.dealer_transfer.require_shippable_ffl_dealer",
			"ffl_core.compliance.trade_in_credit.validate_trade_in_credit_rows",
			"ffl_core.compliance.interstate_tax.apply_interstate_tax",
			# MUST stay after apply_interstate_tax: that hook does doc.set("taxes", …),
			# wholesale-replacing the table on the out-of-state template swap; this one
			# rebuilds the shipping charge row from ffl_shipping_amount afterward.
			"ffl_core.compliance.shipping_charge.apply_shipping_charge",
			# A peace officer's sale drops the excise rows (compliance/le_excise_exempt):
			# after apply_shipping_charge, which may have rebuilt an excise freight row.
			"ffl_core.compliance.le_excise_exempt.apply_le_excise_exemption",
			# MUST stay LAST among the tax hooks, for two reasons: (1) a tax-exempt
			# customer's 0% must win over anything apply_interstate_tax picked
			# (including the Out-of-State template) — see the module docstring for
			# why a mere pre-save clear in manual_order.create_order isn't enough on
			# its own; (2) apply_shipping_charge is what materializes the freight
			# "Actual" row THIS validate cycle — clear_tax_keep_freight's keep logic
			# only has a row to preserve if apply_shipping_charge already ran.
			"ffl_core.compliance.customer_tax_exempt.enforce_customer_tax_exemption",
			# Zelle tenders must carry their bank/app Transaction Number (reference_no),
			# Check/Money Order tenders their check or money order number.
			"ffl_core.compliance.payment_mode.require_zelle_transaction_no",
			"ffl_core.compliance.payment_mode.require_check_money_order_number",
			# A merged negative trade-in ticket gives no change: ERPNext's paid - total
			# would pay its payout out a second time (compliance/trade_in_settlement).
			"ffl_core.compliance.trade_in_settlement.keep_merged_change",
			# A gunsmith deposit line only on a register invoice (gunsmith.py).
			"ffl_core.gunsmith.guard_deposit_lines",
			# Locked lines: refuse what ERPNext's own validate did to them after the
			# before_validate pin (pricing rules, coupons). Last, after every other hook.
			"ffl_core.compliance.locked_lines.assert_locked_lines",
		],
		"on_submit": [
			"ffl_core.listing.status_sync.on_sales_invoice_submit",
			"ffl_core.receipts.send_sale_receipt",
			# Dealer shipment (update_stock=1) → auto-book the bound-book exit.
			"ffl_core.compliance.dealer_transfer.create_dealer_dispositions",
			# Consignment gun sold → book the consignor's payable + COGS.
			# Self-guards against is_consolidated (a POS Invoice's own
			# consignment JE already covers that sale — see the docstring).
			"ffl_core.compliance.consignment.post_consignment_payable",
			# Pay-on-sale gun sold → reclass its parked SRBNB cost into a named
			# supplier payable (Dr SRBNB / Cr Creditors, no COGS). Same
			# is_return/is_consolidated skips + per-serial idempotency guard.
			"ffl_core.compliance.pay_on_sale.settle_pay_on_sale_on_submit",
			# Non-serialized units leave the location the line says they came from
			# (a POS sale reaches here as the consolidated invoice; a ship-now counter
			# order directly). Never blocks the sale -- see ffl_core/storage.py.
			"ffl_core.storage.stock_document_submitted",
			# A merged negative trade-in ticket: its payout is set against this invoice
			# (compliance/trade_in_settlement), so nothing is left outstanding.
			"ffl_core.compliance.trade_in_settlement.reconcile_payout_on_merge",
			# Each firearm sold agreements this sale used (agreements.py).
			"ffl_core.agreements.mark_firearm_agreements_used",
		],
		"on_cancel": [
			# First: a merged invoice whose ticket's payout a drawer count relied on stays
			# (compliance/trade_in_settlement). on_cancel, not before_cancel: the merge
			# log cancels its invoice with ignore_validate, which skips before_cancel.
			"ffl_core.compliance.trade_in_settlement.refuse_merged_cancel_after_count",
			"ffl_core.listing.status_sync.on_sales_invoice_cancel",
			"ffl_core.compliance.dealer_transfer.cancel_dealer_dispositions",
			# Consignment gun sale cancelled → reverse the consignor's payable
			# + COGS JE posted at submit (B1/B2).
			"ffl_core.compliance.consignment.reverse_consignment_payable",
			# Pay-on-sale settlement reversed symmetrically (Dr Creditors / Cr
			# SRBNB) + pay_on_sale_settled flipped back so the gun can re-sell.
			"ffl_core.compliance.pay_on_sale.reverse_pay_on_sale_on_cancel",
			"ffl_core.storage.stock_document_cancelled",
			# Each firearm sold agreements this sale used (agreements.py).
			"ffl_core.agreements.release_firearm_agreements",
		],
		# A membership invoice is never cancelled from the invoice: refunds are credit notes (membership.py).
		"before_cancel": "ffl_core.membership.refuse_invoice_cancel",
	},
	# The payment that settles a membership's invoice makes the membership Active (membership.py).
	"Payment Entry": {"on_submit": "ffl_core.membership.activate_paid_memberships"},
	"Sales Order": {
		# The timeline comment of a price approval the save recorded (price_approval.py).
		"on_update": "ffl_core.price_approval.post_approval_comment",
		# A new order's freight tax basis from FFL Settings.
		"before_insert": "ffl_core.compliance.shipping_charge.stamp_shipping_tax_basis",
		"validate": [
			# No counter sale to a Banned customer without a manager's open override
			# (customer_flags.refuse_banned_customer; returns and channel mirrors pass).
			"ffl_core.customer_flags.refuse_banned_customer",
			# A price a person lowered saves only with a manager's approval (price_approval.py).
			"ffl_core.price_approval.enforce_price_approval",
			"ffl_core.compliance.firearm.require_serial_for_firearm",
			"ffl_core.compliance.interstate_tax.apply_interstate_tax",
			# Keep after apply_interstate_tax — see the Sales Invoice note above.
			"ffl_core.compliance.shipping_charge.apply_shipping_charge",
			# After the freight row is rebuilt — see the Sales Invoice note above.
			"ffl_core.compliance.le_excise_exempt.apply_le_excise_exemption",
			# Keep LAST — see the Sales Invoice note above.
			"ffl_core.compliance.customer_tax_exempt.enforce_customer_tax_exemption",
		],
		"on_submit": "ffl_core.listing.status_sync.on_sales_order_submit",
		"on_cancel": "ffl_core.listing.status_sync.on_sales_order_cancel",
	},
	"Delivery Note": {
		"validate": [
			"ffl_core.compliance.firearm.canonicalize_row_serials",
			"ffl_core.compliance.firearm.require_serial_for_firearm",
			"ffl_core.compliance.dealer_transfer.require_shippable_ffl_dealer",
			"ffl_core.compliance.interstate_tax.apply_interstate_tax",
		],
		"on_submit": [
			"ffl_core.compliance.dealer_transfer.create_dealer_dispositions",
			# Non-serialized units leave the location the line was taken from
			# (ffl_core/storage.py) -- when the stock ledger moves them.
			"ffl_core.storage.stock_document_submitted",
		],
		"on_cancel": [
			"ffl_core.compliance.dealer_transfer.cancel_dealer_dispositions",
			"ffl_core.storage.stock_document_cancelled",
		],
	},
	"POS Closing Entry": {
		# Every closing carries a Cash row built from the cash ledger (cash drawer).
		"before_insert": "ffl_core.cash_drawer.stamp_closing_entry",
	},
	"Price List": {
		# A selling list made after the migrate gets the locked items' tombstones
		# (setup/locked_lines), so ERPNext never auto-inserts a live price there.
		"on_update": "ffl_core.setup.locked_lines.on_price_list_update",
	},
	"POS Invoice": {
		# The timeline comment of a price approval the save recorded (price_approval.py).
		"on_update": "ffl_core.price_approval.post_approval_comment",
		# Same ordering reason as the Sales Invoice entry above.
		"before_validate": [
			# Counter pickup ticket: $0 on every save, refusing items the order did
			# not sell — first, so the hooks below and ERPNext's validate (pricing,
			# totals, change amount) all see $0. Closed by assert_ticket_free.
			"ffl_core.api.counter_pickup.keep_ticket_free",
			"ffl_core.compliance.consignment.freeze_consignment_commission",
			# Locked lines pinned before core prices/totals the rows — see the
			# Sales Invoice entry.
			"ffl_core.compliance.locked_lines.pin_locked_lines",
			# A peace officer's sale: the excise rows off before ERPNext totals the ticket
			# (compliance/le_excise_exempt), and before the trade-in floor reads the rows.
			"ffl_core.compliance.le_excise_exempt.apply_le_excise_exemption_before",
			# A ticket with trade-in credit never takes a negative sales tax: rates settled before ERPNext totals
			# (its own refusal of a negative ticket must see the floored total).
			"ffl_core.compliance.negative_trade_in.floor_trade_in_tax_before",
			# A return of a negative trade-in ticket: goods only, no tax refunded
			# (compliance/trade_in_settlement), before core totals it.
			"ffl_core.compliance.trade_in_settlement.return_goods_only",
		],
		# The register's Return button maps the ticket, then asks the server for its POS
		# defaults (set_missing_values): a return of a negative trade-in ticket comes back
		# goods-only there, so the cashier's cart is right before Checkout.
		"set_missing_values": "ffl_core.compliance.trade_in_settlement.correct_return_cart",
		# P1: a stale browser doc must not revert the
		# webhook-owned 4473 fields on a Draft save. Restores them from the DB.
		"before_save": "ffl_core.compliance.webhook_fields.protect_webhook_4473_fields",
		"on_trash": "ffl_core.setup.pos_compat.guard_cancelled_delete",
		"validate": [
			# Memberships are sold as Sales Invoices only: the merge drops their service dates (membership.py).
			"ffl_core.membership.refuse_on_pos_invoice",
			# No counter sale to a Banned customer without a manager's open override
			# (customer_flags.refuse_banned_customer; returns and channel mirrors pass).
			"ffl_core.customer_flags.refuse_banned_customer",
			# A price a person lowered saves only with a manager's approval (price_approval.py).
			"ffl_core.price_approval.enforce_price_approval",
			# Owner policy: consignment guns are never returnable — block before
			# any other validation bothers running. See the function's own
			# docstring for why this fires on every validate, not just submit.
			"ffl_core.compliance.consignment.block_consignment_returns",
			"ffl_core.compliance.zero_cost_sale_alert.warn_on_unpriced_firearm_sale",
			"ffl_core.compliance.firearm.canonicalize_row_serials",
			"ffl_core.compliance.firearm.require_serial_for_firearm",
			# One gun per line — fires on EVERY validate (Draft too) so a qty>1 /
			# multi-serial firearm line never persists to break the 4473 match
			#. Ahead of the 4473 gate, which
			# assumes the invariant this enforces.
			"ffl_core.compliance.firearm.require_one_firearm_per_line",
			"ffl_core.compliance.firearm.require_4473_and_nics_for_firearm_sale",
			# Trade-in tax floor again on ERPNext's final amounts, before anything reads the total.
			"ffl_core.compliance.negative_trade_in.floor_trade_in_tax",
			"ffl_core.compliance.trade_in_credit.validate_trade_in_credit_rows",
			# Below zero: no tender, no change, how the store paid the customer.
			"ffl_core.compliance.negative_trade_in.guard_negative_ticket",
			"ffl_core.compliance.interstate_tax.guard_pos_exemption",
			# Zelle tenders must carry their bank/app Transaction Number (reference_no),
			# Check/Money Order tenders their check or money order number.
			"ffl_core.compliance.payment_mode.require_zelle_transaction_no",
			"ffl_core.compliance.payment_mode.require_check_money_order_number",
			# Alert (never block) when the register's open shift belongs to another
			# account: the owner-scoped nightly close will skip this sale (8/25,
			# 8/26 incidents); the sweep posts it later.
			"ffl_core.api.pos_consolidate.warn_owner_shift_mismatch",
			# Take Payment on a draft: the server's clock, not the browser's, dates the cash for the drawer.
			"ffl_core.cash_drawer.stamp_draft_payment",
			# Counter pickup ticket: refuse one ERPNext's validate put money back on
			# after the before_validate pin (fail closed).
			"ffl_core.api.counter_pickup.assert_ticket_free",
			# A gunsmith deposit line comes only from its work order, within what it holds (gunsmith.py).
			"ffl_core.gunsmith.guard_deposit_lines",
			# Locked lines: refuse what ERPNext's own validate did to them after the
			# before_validate pin (pricing rules, coupons). Last, after every other hook.
			"ffl_core.compliance.locked_lines.assert_locked_lines",
		],
		# Checkout completing IS the trade completing: book the deferred
		# trade-in acquisitions the credit rows reference (draft acq → submit →
		# Material Receipt → stamp; FastBound push enqueues after commit).
		# before_submit runs AFTER the validate gate above accepted the draft
		# rows and BEFORE on_submit consolidation copies them to the Sales
		# Invoice (whose gate requires them already booked).
		"before_submit": [
			# The whole total paid, whatever the POS Profile's partial-payment flag says. Reads the
			# outstanding amount ERPNext's own before_submit has just set — first, before anything books.
			"ffl_core.compliance.payment_mode.require_full_payment",
			# Before anything books: units a web order is waiting on are not
			# the counter's to sell.
			"ffl_core.compliance.web_hold.refuse_held_units",
			# The guns' half of the same refusal: the last uniform-price gun a sold
			# online order is owed (web or GunBroker) is not the counter's to sell.
			"ffl_core.compliance.web_hold.refuse_claimed_firearm_units",
			# Before anything books: the customer must have signed what the sale needs
			# (agreements.refuse_missing_required; no override — sign or a paper copy).
			"ffl_core.agreements.refuse_missing_required",
			"ffl_core.compliance.trade_in_credit.book_trade_ins_on_submit",
			# Counter pickup's 4473 ticket: the gun leaves only against a settled
			# order (the money is on the Sales Invoice, not the ticket).
			"ffl_core.api.counter_pickup.require_settled_counter_order",
		],
		"on_submit": [
			# Below zero: book what the store paid the customer (the trade-ins were booked in before_submit),
			# first, so the receipt and an immediate merge see it.
			"ffl_core.compliance.negative_trade_in.book_trade_in_payout",
			"ffl_core.listing.status_sync.on_sales_invoice_submit",
			"ffl_core.receipts.send_sale_receipt",
			# Firearm sold/transferred → consolidate THIS invoice immediately so
			# the gun leaves stock at sale, not at the nightly POS close.
			"ffl_core.api.pos_consolidate.consolidate_firearm_invoice_on_submit",
			# Counter pickup's 4473 ticket → the order's gun is handed over;
			# clear the order from the queue when nothing else is owed.
			"ffl_core.api.counter_pickup.close_counter_order_on_pos_submit",
			# Consignment gun sold → book the consignor's payable + COGS.
			"ffl_core.compliance.consignment.post_consignment_payable",
			# Pay-on-sale gun sold → reclass parked SRBNB cost into a named
			# supplier payable (Dr SRBNB / Cr Creditors, no COGS). The POS
			# Invoice (is_consolidated=0) settles; its consolidated Sales Invoice
			# (is_consolidated=1) is skipped, so each sale settles exactly once.
			"ffl_core.compliance.pay_on_sale.settle_pay_on_sale_on_submit",
			# Each firearm sold agreements this sale used (agreements.py).
			"ffl_core.agreements.mark_firearm_agreements_used",
			# A gunsmith work order's deposit / pick-up invoice (gunsmith.py).
			"ffl_core.gunsmith.on_invoice_submit",
		],
		# A negative trade-in ticket: the payout goes back to the drawer first,
		# refused once a count relied on it (compliance/trade_in_settlement).
		"before_cancel": [
			"ffl_core.compliance.trade_in_settlement.void_payout_on_cancel",
			# A gunsmith deposit already used, or a pick-up whose gun left the bound book, stays (gunsmith.py).
			"ffl_core.gunsmith.before_invoice_cancel",
		],
		"on_cancel": [
			"ffl_core.listing.status_sync.on_sales_invoice_cancel",
			# Counter pickup's 4473 ticket cancelled → the gun is back in stock
			# and still the customer's; put the order back on the queue.
			"ffl_core.api.counter_pickup.reopen_counter_order_on_pos_cancel",
			# Consignment gun sale cancelled → reverse the consignor's payable
			# + COGS JE posted at submit (B1/B2).
			"ffl_core.compliance.consignment.reverse_consignment_payable",
			# Pay-on-sale settlement reversed symmetrically + flag flipped back.
			"ffl_core.compliance.pay_on_sale.reverse_pay_on_sale_on_cancel",
			# Each firearm sold agreements this sale used (agreements.py).
			"ffl_core.agreements.release_firearm_agreements",
			"ffl_core.gunsmith.on_invoice_cancel",
		],
	},
	"Purchase Receipt": {
		"validate": [
			"ffl_core.compliance.purchase.require_ffl_supplier_for_firearm_lines",
			# A row claiming a $0 acquisition must carry a readable reason. Narrow
			# by design: only rows already ticked are constrained, so no other
			# receive path needs an exemption (see the docstring).
			"ffl_core.compliance.purchase.validate_zero_cost_confirmation",
		],
		"on_submit": [
			"ffl_core.compliance.purchase.create_acquisitions_for_firearm_lines",
			# An amendment takes over the private seller's payment its cancelled
			# original still carries (after its own acquisitions exist).
			"ffl_core.cash_drawer.take_over_goods_payout_on_amend",
		],
		"on_cancel": [
			# Units this receipt put in a location come out again with it.
			"ffl_core.storage.stock_document_cancelled",
			"ffl_core.compliance.purchase.cancel_acquisitions_for_firearm_lines",
			# A cancelled receipt never happened: each gun answers again to the
			# stay before it.
			"ffl_core.compliance.purchase.restore_stay_on_cancel",
			# The private seller's payment stays booked; say how to take it back.
			"ffl_core.cash_drawer.note_goods_payout_on_cancel",
		],
	},
	# FFL Disposition: take the firearm out of ERPNext inventory on submit so
	# the Item form, POS picker, and other disposition pickers immediately
	# reflect that the gun is gone. Retail Sale is intentionally skipped (POS
	# / Sales Invoice already moves stock). The FastBound bound-book push is
	# wired separately in ffl_integrations.hooks — Frappe runs hooks across
	# apps in installation order; ffl_core runs first, so a stock failure
	# here aborts the submit BEFORE any FastBound push is enqueued.
	"FFL Disposition": {
		"on_submit": "ffl_core.compliance.disposition_stock.issue_stock_on_submit",
		"on_cancel": "ffl_core.compliance.disposition_stock.restore_stock_on_cancel",
	},
	# A Stock Entry is judged after ALL its ledger entries (a transfer between own warehouses
	# posts its outgoing leg first): guns really off our shelves lose their storage location.
	# Cancelling one also replays the location moves it made (a GunBroker stock issue, say).
	"Stock Entry": {
		"on_submit": "ffl_core.storage.stock_entry_posted",
		"on_cancel": [
			"ffl_core.storage.stock_entry_posted",
			"ffl_core.storage.stock_document_cancelled",
		],
	},
	"Stock Ledger Entry": {
		# Mirror Bin.actual_qty into the Item.actual_qty custom field so the
		# Item list view's "Quantity" column stays in sync with stock movements.
		# The second hook clears a gun's storage location when this entry took
		# it off the shelf (ERPNext updates Serial No by SQL, so no Serial No hook
		# can see it) -- see ffl_core/storage.py.
		"on_submit": [
			"ffl_core.api.item_admin.on_stock_ledger_entry_change",
			"ffl_core.storage.on_stock_ledger_entry",
		],
		"on_cancel": [
			"ffl_core.api.item_admin.on_stock_ledger_entry_change",
			"ffl_core.storage.on_stock_ledger_entry",
		],
	},
}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "ffl_core",
# 		"logo": "/assets/ffl_core/logo.png",
# 		"title": "FFL Core",
# 		"route": "/ffl_core",
# 		"has_permission": "ffl_core.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ffl_core/css/ffl_core.css"
# app_include_js = "/assets/ffl_core/js/ffl_core.js"

# include js, css files in header of web template
# web_include_css = "/assets/ffl_core/css/ffl_core.css"
# web_include_js = "/assets/ffl_core/js/ffl_core.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ffl_core/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
doctype_js = {
    "Supplier": "public/js/supplier_form.js",
    "Customer": "public/js/customer_form.js",
    "Item": "public/js/item_form.js",
    "Serial No": "public/js/serial_no.js",
    "Purchase Receipt": "public/js/purchase_receipt.js",
    "POS Invoice": "public/js/pos_invoice.js",
}
doctype_list_js = {
    "Customer": "public/js/customer_list.js",
    "Item": "public/js/item_list.js",
    "ATF FFL Record": "public/js/atf_ffl_record_list.js",
    "Serial No": "public/js/serial_no_list.js",
}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk: "gunstore" is the Gun Shop mark the sidebar header shows
app_include_icons = ["/assets/ffl_core/icons/gunstore.svg"]

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
#
# company_brand(company) is how the receipt Print Formats and the Email
# Templates get the store's name / logo / address / footer note. Registering it
# here (rather than each template digging into Company itself) is what keeps
# ONE source of truth for branding — and it is the upgrade-safe route: no core
# template is touched, the templates just gain a global.
jinja = {
	"methods": [
		"ffl_core.store_identity.company_brand",
		# Tax-inclusive shipping lines on the Sales Receipt.
		"ffl_core.compliance.shipping_charge.inclusive_shipping_lines",
	],
}

# Installation
# ------------

# before_install = "ffl_core.install.before_install"
# NOTE: trade_in_credit.ensure is deliberately NOT here — it inserts an Item,
# and at after_install time on a fresh site the UOM master doesn't exist yet
# (same landmine the transfer-fee item hit; see setup/transfer_fee.py). It
# runs lazily at first use + in after_migrate (where masters always exist).
after_install = [
	# POS Extension registry ⇄ bench + site (ffl_core.extensions.registry).
	"ffl_core.extensions.registry.sync",
	"ffl_core.setup.sidebar.ensure_settings_sidebar_link",
	"ffl_core.setup.sidebar.ensure_settings_sidebar_backlink",
	"ffl_core.setup.gunshop_sidebar.ensure",
	"ffl_core.setup.desktop_icons.ensure_gun_shop_desktop_tile",
	"ffl_core.setup.item_groups.ensure",
	"ffl_core.setup.tax.ensure",
	"ffl_core.setup.pos_compat.ensure_pos_invoice_compat_fields",
	"ffl_core.setup.pos_compat.ensure_pos_invoice_mode",
	# ERPNext ≥ 16.50 gates the register on POS Profile select-or-read (pos_compat).
	"ffl_core.setup.pos_compat.ensure_cashier_pos_profile_select",
	# Permissions granted by user decision (pos_compat.ROLE_GRANTS): the counter's 4473 / pickup reports.
	"ffl_core.setup.pos_compat.ensure_role_grants",
	"ffl_core.setup.customer_tax.ensure",
	"ffl_core.setup.customer_identity.ensure",
	# Customer Profile tab (status, alerts, IDs, family) + existing customers → Active.
	"ffl_core.setup.customer_profile.ensure",
	# Item "Agreement Required to Buy" + the Customer form's Agreements section.
	"ffl_core.setup.agreements.ensure",
	"ffl_core.setup.buyer_type.ensure",
	"ffl_core.setup.selling.ensure",
	# Custom fields only (no Item, no Warehouse, no master) — safe at
	# after_install time, unlike the ensures deliberately excluded above.
	"ffl_core.setup.custody.ensure",
	# A Single write only — store the switch's ON default (patches don't run on install).
	"ffl_core.setup.shipping_charge.ensure_shipping_tax_switch",
	# Same for the idle counter lock's 5-minute default (staff_switch).
	"ffl_core.staff_switch.ensure_lock_default",
	# Manager approval for lowered prices: its fields, and the switch ON for a new store.
	"ffl_core.setup.price_approval.ensure",
	"ffl_core.price_approval.ensure_default",
	# Cash drawer: the two POS Closing Entry fields (accounts wait for a company, at after_migrate).
	"ffl_core.setup.cash_drawer.ensure_fields_only",
]
before_migrate = [
	# Extension upgrades: any migrate that runs an upgrade's new code (a deploy's
	# too) is on record in sites/extensions.json before it touches the database —
	# docs/self-hosting.md#extensions decides a rollback on that record. Touches no table.
	"ffl_core.extensions.sideload.note_migrate_started",
]
after_migrate = [
	# POS Extension registry ⇄ bench + site (ffl_core.extensions.registry).
	"ffl_core.extensions.registry.sync",
	# OAuth Settings follow the MCP server switches (mcp_settings.sync_oauth).
	"ffl_core.ffl_core.doctype.mcp_settings.mcp_settings.sync_oauth",
	# Item form layout belongs to the custom_field fixtures (insert_after). A
	# Customize Form save pins a field_order Property Setter that overrides them
	# for every field it saw — drop it each migrate (setup/item_layout.py).
	"ffl_core.setup.item_layout.ensure",
	"ffl_core.setup.sidebar.ensure_settings_sidebar_link",
	"ffl_core.setup.sidebar.ensure_settings_sidebar_backlink",
	"ffl_core.setup.gunshop_sidebar.ensure",
	"ffl_core.setup.desktop_icons.ensure_gun_shop_desktop_tile",
	"ffl_core.setup.item_groups.ensure",
	"ffl_core.setup.tax.ensure",
	"ffl_core.setup.pos_compat.ensure_pos_invoice_compat_fields",
	"ffl_core.setup.pos_compat.ensure_pos_invoice_mode",
	"ffl_core.setup.pos_compat.ensure_cashier_pos_profile_select",
	# Permissions granted by user decision (pos_compat.ROLE_GRANTS): the counter's 4473 / pickup reports.
	"ffl_core.setup.pos_compat.ensure_role_grants",
	# After the grants: report POS Profile / Serial and Batch Bundle rules that drifted from ERPNext's (frozen Custom DocPerm).
	"ffl_core.setup.pos_compat.check_perm_drift",
	# Report a Company with no usable address (the home state has no default).
	"ffl_core.setup.company_address.ensure",
	"ffl_core.setup.trade_in_credit.ensure",
	# Customer Deposits liability account per company (looked up by name by
	# the GunBroker cash-basis report).
	"ffl_core.setup.customer_deposits.ensure",
	# Deferred Membership Revenue + Membership Revenue per company (accounts only; Accounts
	# Settings change only when a store saves its first active Membership Plan).
	"ffl_core.setup.membership.ensure",
	"ffl_core.setup.consignment.ensure",
	# Stock at Consignee per company — the account every consignment
	# warehouse points at (ffl_core.warehouses); core-owned custody primitive.
	"ffl_core.warehouses.ensure_stock_at_consignee_accounts",
	# Pay-on-sale stamp fields (Supplier flag + Serial No srbnb_cost/supplier/
	# settled + JE source tag). Idempotent, created here at after_migrate — same
	# route as consignment.ensure (not committed to fixtures/custom_field.json).
	"ffl_core.setup.pay_on_sale.ensure",
	# Zero-cost confirmation fields (Serial No + Purchase Receipt Item). Lets a
	# genuinely-free acquisition be stated on purpose and QUERIED later, instead
	# of being indistinguishable from a cost nobody filled in. Same route +
	# rationale as pay_on_sale.ensure above.
	"ffl_core.setup.zero_cost.ensure",
	# Customer-custody fields (Warehouse.is_custody_warehouse, Serial No.
	# is_transfer_gun, Purchase Receipt.is_ffl_transfer). Same route + rationale
	# as pay_on_sale.ensure above. Fields only — the custody WAREHOUSE itself is
	# created lazily at the first transfer receive, because warehouse creation
	# needs a company and a fresh install has none yet.
	"ffl_core.setup.custody.ensure",
	"ffl_core.setup.payments.ensure_manual_modes_of_payment",
	"ffl_core.setup.customer_tax.ensure",
	"ffl_core.setup.customer_identity.ensure",
	# Customer Profile tab (status, alerts, IDs, family) + existing customers → Active.
	"ffl_core.setup.customer_profile.ensure",
	# Item "Agreement Required to Buy" + the Customer form's Agreements section.
	"ffl_core.setup.agreements.ensure",
	# Gunsmith work orders: labor + deposit items, invoice links, the intake agreement (after
	# tax / customer_deposits / agreements, which it builds on).
	"ffl_core.setup.gunsmith.ensure",
	"ffl_core.setup.buyer_type.ensure",
	"ffl_core.setup.shipping_charge.ensure",
	# Pin "Allow Negative rates for Items" on so trade-in credit
	# negative lines can consolidate into a Sales Invoice (see setup/selling).
	"ffl_core.setup.selling.ensure",
	# Cash drawer: POS Closing Entry fields + Cash Over/Short and
	# Operating Bank per company, default bank account filled when empty.
	"ffl_core.setup.cash_drawer.ensure",
	# Manager approval for lowered prices: the record + token fields (setup/price_approval).
	"ffl_core.setup.price_approval.ensure",
]

# Uninstallation
# ------------

# before_uninstall = "ffl_core.uninstall.before_uninstall"
# after_uninstall = "ffl_core.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ffl_core.utils.before_app_install"
after_app_install = ["ffl_core.extensions.registry.on_app_change", "ffl_core.setup.gunshop_sidebar.on_app_change"]

# The Gun Shop home page's "Needs attention" cards are standard Number Cards shipped as files
# (ffl_core/number_card/); Frappe walks that folder only for doctypes an app names here.
importable_doctypes = ["Number Card"]

# Core is the first provider of its own hook: an extension turning on/off re-shapes the Gun Shop
# sidebar (it may declare gunshop_sidebar_items). See setup/gunshop_sidebar.py.
gunshop_extension_toggled = ["ffl_core.setup.gunshop_sidebar.on_extension_toggled"]

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ffl_core.utils.before_app_uninstall"
after_app_uninstall = ["ffl_core.extensions.registry.on_app_change", "ffl_core.setup.gunshop_sidebar.on_app_change"]

# Build
# ------------------
# To hook into the build process

# after_build = "ffl_core.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ffl_core.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		# Nightly POS close at 23:00 store-local. Frappe evaluates cron in the
		# SITE timezone (America/Chicago), so this fires at 11 PM Central — after
		# the store has closed. It submits a POS Closing Entry for every shift
		# still open, consolidating the day's POS Invoices into Sales Invoices
		# that night so the books are clean by morning. (Replaces the old
		# "hourly, 18 h after the shift OPENED" model, which closed an afternoon
		# shift the next mid-morning instead of that night.) By 11 PM anything
		# still open is a finished shift; a 1 h guard in the function skips a
		# fluke just-opened one, and a missed night is caught the next night.
		"0 23 * * *": [
			"ffl_core.api.pos_close.auto_close_open_pos",
		],
		# Safety net: retry any POS Invoice whose consolidation failed/was missed
		# (immediate firearm consolidation is async; the nightly close is
		# owner-scoped + one-shot, so a sale rung up by a clerk other than the
		# shift opener is skipped — the sweep takes those past-day leftovers).
		# Keeps a sold item from silently staying in stock; alerts via ToDo if
		# an invoice keeps failing. See api/pos_consolidate.
		"*/15 * * * *": [
			"ffl_core.api.pos_consolidate.reconsolidate_stuck_pos_invoices",
		],
		# Membership renewals at 7 AM store-local (site timezone): charge the saved cards that are
		# due, retry past-due ones once a day, suspend when the grace runs out, yearly reminders
		# (membership.py). Morning, so a decline reaches the staff the same day.
		"0 7 * * *": [
			"ffl_core.membership.renew_memberships",
		],
	},
	"hourly": [
		# Backstop for the Stock Ledger Entry hook: clear the storage location of any
		# gun that left stock by a path that hook did not see.
		"ffl_core.storage.sweep_departed",
	],
	"daily": [
		# A membership that does not renew turns Cancelled / Expired after its period (membership.py).
		"ffl_core.membership.close_ended_memberships",
		# Self-heal Item.actual_qty (the list-view "Quantity" column) from
		# Bin — the incremental SLE hook can't recover from a missed entry.
		"ffl_core.api.item_admin.recompute_all_item_actual_qty",
		# Sale windows open/close on pure calendar days — no Item save fires —
		# so the uniform-item Item Price hard-sync must be re-run daily.
		"ffl_core.item_hooks.refresh_sale_item_prices",
		# A Draft POS Invoice holds its firearm off every sales channel and
		# ages out of nobody's view; DISP-2026-02000 sat that way for eleven
		# days. ALERTS ONLY — clearing one deletes a draft and touches the
		# bound book, which stays a manager's decision behind a button.
		"ffl_core.api.pending_4473.alert_overdue_pending_4473",
		# Managers' morning list of blocked / overdue counter orders.
		# Sends nothing when nothing is stuck.
		"ffl_core.api.manual_order.pending_orders_digest",
		# What newer extension versions the update service offers — the
		# Extensions page's Update button. Installs nothing.
		"ffl_core.extensions.updates.check",
		# Signed agreements past their expiry date → Expired (agreements.py).
		"ffl_core.agreements.expire_agreements",
	],
}

# Testing
# -------

# before_tests = "ffl_core.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "ffl_core.custom.task.CustomTaskMixin"
# }
# A ticket may go negative only when trade-in credit takes it there (compliance/negative_ticket).
extend_doctype_class = {
	"POS Invoice": ["ffl_core.compliance.negative_ticket.NegativeTradeInTicket"],
	"Sales Invoice": ["ffl_core.compliance.negative_ticket.NegativeTradeInTicket"],
}

# Overriding Methods
# ------------------------------
#
# One register, many clerks: resume ANY open shift instead of deadlocking
# when the open entry belongs to a different user (see api/pos_session.py).
override_whitelisted_methods = {
	"erpnext.selling.page.point_of_sale.point_of_sale.check_opening_entry":
		"ffl_core.api.pos_session.check_opening_entry",
	# MCP clients that sign in from a terminal (Claude Code) register an
	# http://localhost callback, which Frappe's registration refuses (api/oauth_dcr.py).
	"frappe.integrations.oauth2.register_client": "ffl_core.api.oauth_dcr.register_client",
}

# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "ffl_core.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ffl_core.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["ffl_core.utils.before_request"]
# after_request = ["ffl_core.utils.after_request"]

# Job Events
# ----------
# before_job = ["ffl_core.utils.before_job"]
# after_job = ["ffl_core.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"ffl_core.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

