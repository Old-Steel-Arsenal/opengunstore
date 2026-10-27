app_name = "ffl_integrations"
app_title = "FFL Integrations"
app_publisher = "OpenGunstore"
app_description = "FastBound, Payroc, ACH, ATF EZ Check clients"
app_email = "info@oldsteelarsenal.com"
app_license = "GNU General Public License (v3)"

# Fixtures
# --------
fixtures = [
	{
		"dt": "Email Template",
		"filters": [["name", "in", ["NICS Cleared Pickup"]]],
	},
	# Custom fields this app owns (Item.dropship_distributor,
	# FFL Acquisition.distributor_order — see fixtures/custom_field.json).
	{
		"dt": "Custom Field",
		"filters": [["module", "=", "FFL Integrations"]],
	},
]

# Distributor provider registry. Value = dotted-path list of zero-arg
# factories `() -> DistributorProvider`; distributor/api.py keys them by
# provider.code (first registration wins). RSR is provider #1. Mirrors the
# ffl_acquisition_sync_push inversion — the generic layer never imports a provider.
distributor_providers = [
	"ffl_integrations.distributor.rsr_provider.get_provider",
	"ffl_integrations.distributor.ss_provider.get_provider",
	"ffl_integrations.distributor.lipseys_provider.get_provider",
]

# Where the Extensions page's Update button gets newer extension versions
# (core hook, docs/extensions/hooks.md): the update service /v1/extensions over the Data
# Service connection. A site without Data Service Settings offers no updates.
gunshop_extension_updates = [
	"ffl_integrations.data_service.extension_updates.get_source",
]

# Card processors (core hook, docs/extensions/hooks.md): payments.api keeps the card money
# bookkeeping and talks to a processor only through its CardProcessor adapter. Payroc
# registers here exactly as an extension's processor would.
gunshop_card_processors = [
	"ffl_integrations.payroc.adapter.processor",
]

# An extension switched on or off can change what the site can do (the ``ai``
# capability answers through an extension's gunshop_ai_connected hook).
gunshop_extension_toggled = [
	"ffl_integrations.data_service.on_extension_toggled",
]

# Product change-feed cursors ("DocType.field" on a Single) that numbered their
# changes against the current catalog source: while any holds a value, Data
# Service Settings refuses to switch the source (a cursor from one source is
# meaningless against the other). Extensions that follow the feed add their own
# here (docs/extensions/hooks.md).
gunshop_catalog_change_cursors = ["Data Service Settings.category_products_cursor"]

# The website drop-ship layer (listing distributor products on the website,
# routing web orders to distributors) is the osa_dropship extension. It answers
# the core's drop-ship hooks itself — distributor_stock_source,
# dropship_source_for_item, distributor_web_price, map_mode_for_item,
# distributor_listing_push_failed, woo_order_cancelled and the gunshop_dropship_* /
# gunshop_distributor_* seams (docs/extensions/hooks.md); without it they are empty
# and every caller falls back to the store's own stock, price and routing.

# Counter drop-ship. ffl_core.api.manual_order.create_order
# fires this hook by name INSIDE the counter order's transaction (after
# si.submit()+reload()), so a raise here rolls the whole counter sale back and the
# Draft Distributor Order is built with complete context and no webhook race. An
# empty list on a site that doesn't install this app — create_order no-ops.
ffl_counter_order_distributor = [
	"ffl_integrations.distributor.counter.ffl_counter_order_distributor",
]

# Editing a counter order cancels it and rebuilds it as a new invoice; ffl_core
# fires this hook by name with (old invoice, new invoice) so the orders already
# placed with a house follow the sale instead of dying on the cancelled invoice.
ffl_counter_order_replaced = [
	"ffl_integrations.distributor.counter.carry_forward_replaced_orders",
]

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "ffl_integrations",
# 		"logo": "/assets/ffl_integrations/logo.png",
# 		"title": "FFL Integrations",
# 		"route": "/ffl_integrations",
# 		"has_permission": "ffl_integrations.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ffl_integrations/css/ffl_integrations.css"
# app_include_js = "/assets/ffl_integrations/js/ffl_integrations.js"

# include js, css files in header of web template
# web_include_css = "/assets/ffl_integrations/css/ffl_integrations.css"
# web_include_js = "/assets/ffl_integrations/js/ffl_integrations.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ffl_integrations/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
	"Serial No": "public/js/serial_no.js",
	# GunBroker item-level listings (ammunition, optics, parts). Three apps
	# register handlers for Item and none can see the others, so this block
	# gates itself independently — same arrangement as Serial No.
	"Item": "public/js/item.js",
	# Card on file (payments/saved_cards.py): the customer's saved cards, and Charge saved card.
	"Customer": "public/js/customer_saved_cards.js",
	"Sales Invoice": "public/js/sales_invoice_saved_cards.js",
}

# Server constants exposed to desk JS (frappe.boot.*) — see boot.py.
extend_bootinfo = "ffl_integrations.boot.extend_bootinfo"
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "ffl_integrations/public/icons.svg"

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
# jinja = {
# 	"methods": "ffl_integrations.utils.jinja_methods",
# 	"filters": "ffl_integrations.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "ffl_integrations.install.before_install"
after_install = [
	"ffl_integrations.payments.setup.ensure_mode_of_payment",
	"ffl_integrations.setup.rsr_seed.run",
	# GunBroker category map + fee tiers. Both seeds fill an EMPTY table only, so
	# neither install nor migrate can overturn an operator's edits.
	"ffl_integrations.setup.gunbroker_seed.run",
	# The GunBroker liability, accrual and loss accounts, and the two payment
	# modes. Idempotent, and it re-points each mode at whatever account Settings
	# names right now — a Payment Entry reads its bank account off the MODE, so
	# without that pass changing the setting would be decorative.
	"ffl_integrations.gunbroker.setup.ensure",
	# The local catalog's dc_* tables + the `catalog` queue: a fresh install marks
	# patches done without running them.
	"ffl_integrations.catalog_local.schema.ensure",
]
after_migrate = [
	"ffl_integrations.payments.setup.ensure_mode_of_payment",
	"ffl_integrations.setup.rsr_seed.run",
	# Sports South Distributor row (disabled) + the Overview's manufacturer
	# restrictions as To-Store brand policies. Create-once, Brand-tolerant.
	"ffl_integrations.sports_south.seed.run",
	# Lipsey's Distributor row (disabled). Create-once.
	"ffl_integrations.lipseys.seed.run",
	"ffl_integrations.setup.gunbroker_seed.run",
	# The GunBroker liability, accrual and loss accounts, and the two payment
	# modes. Idempotent, and it re-points each mode at whatever account Settings
	# names right now — a Payment Entry reads its bank account off the MODE, so
	# without that pass changing the setting would be decorative.
	"ffl_integrations.gunbroker.setup.ensure",
	# The local catalog's dc_* tables (CREATE TABLE IF NOT EXISTS) and the
	# `catalog` queue in common_site_config — kept on every migrate, not only by
	# the create patch, so a restored or hand-edited site heals.
	"ffl_integrations.catalog_local.schema.ensure",
]

# Uninstallation
# ------------

# before_uninstall = "ffl_integrations.uninstall.before_uninstall"
# after_uninstall = "ffl_integrations.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ffl_integrations.utils.before_app_install"
# after_app_install = "ffl_integrations.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ffl_integrations.utils.before_app_uninstall"
# after_app_uninstall = "ffl_integrations.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "ffl_integrations.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ffl_integrations.notifications.get_notification_config"

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

# Synchronous, atomic FastBound acquisition push for Receive Goods. ffl_core's
# create_receive calls every function listed here after pr.submit() (inside the
# receive transaction); a rejection raises and rolls the whole receive back, so a
# firearm is in FastBound AND our system or in neither. This inverts the
# dependency — ffl_core need not import ffl_integrations.
ffl_acquisition_sync_push = [
	"ffl_integrations.fastbound.tasks.push_receipt_acquisitions_sync",
]

# The counter's scan-to-add asks every sales channel "has somebody already
# bought this gun?" before a cashier builds a ticket around it. ffl_core defines
# the question so it need not know which channels exist; this is GunBroker's
# answer. ADVISORY — it warns and never blocks. The refusal that does block is
# `block_sale_of_sold_gb_listing` on before_submit, registered below.
#
#     fn(serial_no: str, *, timeout: int) -> dict | None
channel_presale_probe = [
	"ffl_integrations.gunbroker.crosschannel.presale_probe",
]

# The shared pool: a unit sold on one channel and not yet shipped is spoken for on
# every channel. ffl_core asks each channel how many units it has committed, and
# tells each when one channel's number moves; these are GunBroker's answers
# (the web store registers its own in ffl_woo_sync). Neither app imports the other.
#
#     fn(item_code: str) -> int
#     fn(item_code: str, *, source: str) -> None
channel_committed_units = [
	"ffl_integrations.gunbroker.orders.channel_committed_units",
]
channel_commitments_changed = [
	"ffl_integrations.gunbroker.crosschannel.channel_commitments_changed",
]

# An abandoned 4473 wizard leaves an UNCOMMITTED disposition holding the item in
# FastBound: the bound book still reads available, nothing on the POS side
# objects, and the gun's next real disposition 400s against it (DISP-2026-02000).
# ffl_core's Pending 4473 Orders report asks the question; this answers it.
# READ ONLY, never raises — it decorates a report.
#
#     fn(serials: list[str]) -> {serial: {"disposition_id", "item_number"}}
ffl_pending_disposition_probe = [
	"ffl_integrations.fastbound.pos_4473_recovery.pending_disposition_probe",
]

doc_events = {
	# What the site can do follows these Singles (data_service.capabilities): a
	# save drops the cached answer and, after the commit, the desk boot that carries it.
	"Data Service Settings": {"on_update": "ffl_integrations.data_service.on_settings_change"},
	"FFL Settings": {"on_update": "ffl_integrations.data_service.on_settings_change"},
	# A house naming its own catalog service turns the catalog on (or off).
	"Distributor": {"on_update": "ffl_integrations.data_service.on_settings_change",
		"on_trash": "ffl_integrations.data_service.on_settings_change"},
	# Server-side enforcement of the card PARTIAL rule: a partially-captured
	# invoice must never submit as fully paid, no matter where the submit
	# comes from (POS, desk form, direct API).
	"POS Invoice": {
		# Charge rows are server-owned: a stale client save must not delete or
		# mutate them (multi-card r1 F2). Cancel and update-after-submit skip
		# validate but still sync children off the payload, so the same
		# restore hooks those actions too (r2-M1).
		# The scalar guard is the row restore's sibling and rides the SAME
		# events, for the same reason its docstring gives: frappe dispatches
		# per _action — save/submit run validate, cancel runs only
		# before_cancel, update-after-submit its own event; before_save runs
		# on NONE of submit/cancel. The card money-fact fields are written
		# via db.set_value(update_modified=False), so a stale client
		# save/SUBMIT/CANCEL would silently revert them — the
		# case, probe-proven to re-open a double
		# refund through a stale Desk submit.
		"validate": ["ffl_integrations.payments.api.restore_charge_rows",
			"ffl_integrations.payments.api.protect_money_scalars",
			# Standalone-terminal mode: no gateway record of the swipe exists, so a
			# reference typed by the cashier is the only link between this sale and
			# the terminal's own batch report. Inert unless every card processor is switched OFF
			# and the operator asked for it — sibling of ffl_core's Zelle
			# Transaction Number gate, and gated at submit for the same reasons.
			"ffl_integrations.payments.api.require_card_reference_on_manual_tender",
			# A return of a gateway-charged sale needs the manager's portal-refund
			# confirmation while its processor is off. The POS client meets this rule in
			# refund_pos_invoice; a straight desk submit meets it here — submit is
			# the one door everyone walks through.
			"ffl_integrations.payments.api.block_unconfirmed_gateway_refund"],
		"before_cancel": ["ffl_integrations.payments.api.restore_charge_rows",
			"ffl_integrations.payments.api.protect_money_scalars"],
		"before_update_after_submit": ["ffl_integrations.payments.api.restore_charge_rows",
			"ffl_integrations.payments.api.protect_money_scalars"],
		# ORDER IS LOAD-BEARING, in two ways.
		#
		# It must be a LIST. It was a bare string, and appending a second handler
		# by rewriting that string would have silently deleted the card
		# partial-capture guard — three rounds of hardening
		# gone, with nothing failing to say so. A test asserts this key is a list
		# whose first entry is still that guard.
		#
		# The card guard must stay FIRST. It is pure arithmetic on the invoice in hand;
		# the GunBroker gate sends an HTTP request. Local checks before network
		# ones, so a partially-captured invoice is refused instantly rather than
		# after a marketplace round trip.
		"before_submit": [
			"ffl_integrations.payments.api.block_partial_submit",
			"ffl_integrations.gunbroker.doc_events.block_sale_of_sold_gb_listing",
		],
		"on_submit": "ffl_integrations.gunbroker.doc_events.on_invoice_change",
		"on_cancel": "ffl_integrations.gunbroker.doc_events.on_invoice_change",
		# A parked (draft) ticket holds its guns from the first save, and a model
		# listing that goes on offering a held gun is an oversell: the draft's
		# save and delete re-decide the quantity. Lists, with the card guard
		# first on on_trash, for the reason given on before_submit above.
		"on_update": ["ffl_integrations.gunbroker.doc_events.on_invoice_draft_change"],
		# A return carrying ANY refund money fact (lock, completed reference,
		# or booked amount) is the record the refund gates count — deleting
		# it re-arms a clean full-amount refund.
		"on_trash": ["ffl_integrations.payments.api.block_locked_return_trash",
			"ffl_integrations.gunbroker.doc_events.on_invoice_draft_change"],
	},
	# A uniform-price gun's MODEL listing follows its Item: a price or promotion
	# edit reprices it, and an Item that stops being a uniform-price gun (or is
	# disabled) ends it. A list for the reason given on POS Invoice.before_submit.
	"Item": {
		"on_update": ["ffl_integrations.gunbroker.doc_events.on_item_update"],
	},
	# Push every newly-submitted firearm acquisition to FastBound right away
	# (enqueued, after commit). Without this, acquisitions only reach the A&D
	# book via the daily reconcile_failed_pushes sweep. On cancel, delete the
	# FastBound record for NON-PR acquisitions (trade-in / intake / import) —
	# PR-linked rows are handled by the Purchase Receipt cancel path:
	# the two books must not diverge on a cancel.
	"FFL Acquisition": {
		"on_submit": "ffl_integrations.fastbound.tasks.enqueue_push_on_submit",
		"on_cancel": "ffl_integrations.fastbound.tasks.enqueue_delete_on_cancel",
	},
	# Push non-4473 dispositions (FFL transfers / returns / destruction) on
	# submit; delete from FastBound on cancel. 4473 dispositions arrive from the
	# webhook already synced (fastbound_id set) and are skipped by the guard.
	"FFL Disposition": {
		"on_submit": "ffl_integrations.fastbound.tasks.enqueue_disposition_push_on_submit",
		"on_cancel": "ffl_integrations.fastbound.tasks.cancel_disposition_on_cancel",
	},
	# Route a paid web order to its distributor(s). Deferred to a job by
	# the handler — routing reads the catalog and creates documents, which has no
	# business inside a webhook request. The cron sweep below is NOT belt-and-
	# braces: the webhook stamps `dropship_status` with db.set_value, which fires
	# no doc_events at all, and an order can also become payable later.
	# Both keys are LISTS, for the reason given on POS Invoice.before_submit above
	# (the osa_dropship extension adds its router to them). GunBroker's handler is here because a web order
	# reserves a gun with db.set_value — no stock ledger entry, no invoice, no
	# Serial No save — so this is the ONLY event that fires when the commonest
	# way a gun becomes unavailable actually happens.
	"Woo Online Order": {
		"after_insert": [
			"ffl_integrations.gunbroker.doc_events.on_woo_order_change",
			"ffl_integrations.notifications.notify_new_order",
		],
		"on_update": [
			"ffl_integrations.gunbroker.doc_events.on_woo_order_change",
			"ffl_integrations.notifications.notify_new_order",
		],
	},
	# A listed gun moving in the stock ledger is the catch-all exit signal: v16
	# flips Serial No status with a direct query that fires no document event, so
	# the ledger is the only thing that always speaks.
	"Stock Ledger Entry": {
		"on_submit": ["ffl_integrations.gunbroker.doc_events.on_sle_submit"],
		"on_cancel": ["ffl_integrations.gunbroker.doc_events.on_sle_cancel"],
	},
	# Price and availability edits made on the gun itself.
	"Serial No": {
		"on_update": "ffl_integrations.gunbroker.doc_events.on_serial_update",
	},
	# Finish the source web order when a Distributor Order reaches Complete — the
	# poll is not the only way it gets there (an operator can complete it by hand).
	# The handler no-ops on any non-Complete save, and completion is enqueued after
	# commit.
	"Distributor Order": {
		"on_update": ["ffl_integrations.distributor.tasks.on_distributor_order_update"],
	},
	# The counter's bell for a new GunBroker sale (in-app only; the Woo twin is above).
	"GunBroker Order": {
		"after_insert": "ffl_integrations.notifications.notify_new_order",
	},
	# Keep the drop-ship cost PI's goods lines on COGS. ERPNext's
	# set_expense_account forces a stock line to 'Stock Received But Not Billed' in
	# validate() and re-fires on submit; this hook runs AFTER it and re-pins COGS,
	# scoped to PIs carrying a distributor_order. Ordinary purchasing is untouched.
	"Purchase Invoice": {
		"validate": "ffl_integrations.distributor.accounting.repin_dropship_expense_accounts",
	},
	# A counter drop-ship Sales Invoice has no local FFL Disposition / ShipStation
	# record, so ffl_core's edit guard can't see it. Once its Distributor Order is
	# placed at RSR the goods are en route with no cancel API — block the cancel (and
	# amend, which cancels first) and alert; withdraw not-yet-placed orders.
	# on_submit closes the two doors create_order's in-transaction hook cannot see —
	# an AMEND (the amended invoice submits on its own) and a hand-built desktop
	# invoice. Drop-ship lines with no live distributor order = goods nobody ordered;
	# rebuild the Draft order if the destination is recoverable, else refuse.
	"Sales Invoice": {
		# set_pos_fields (inside the controller's own validate) refills `warehouse` on
		# an is_pos invoice, undoing create_order's re-pin of the drop-ship rows. A
		# hooked validate runs AFTER the controller's, so this is the durable last
		# word — same ordering the drop-ship PI's COGS re-pin relies on.
		"validate": ["ffl_integrations.distributor.counter.repin_dropship_lines",
			# Same standalone-terminal gate as POS Invoice above: a counter sale
			# written straight to a Sales Invoice (manual order, desk) tenders cards
			# too, and must not be a way around it.
			"ffl_integrations.payments.api.require_card_reference_on_manual_tender",
			# A GunBroker invoice never carries a negative marketplace liability.
			# The booking path checks immediately before its own submit, which
			# the Desk does not go through — and the Draft that case produces is
			# one an operator can submit by hand in two clicks.
			"ffl_integrations.gunbroker.revenue.refuse_a_negative_liability_on_validate"],
		# Lists, all of them, even where there is only one entry today. A hooks
		# dict written with bare strings has exactly one obvious way to add a
		# second handler on another branch — write the key again — and Python
		# takes the last one silently: no syntax error, no failing test, one
		# handler simply gone. PR-2 (listing sweep) and PR-3 (orders/accounting)
		# both hook these keys; as lists the only resolution is appending.
		# Pinned by `OURS_THAT_A_MERGE_MUST_NOT_DROP` in test_gunbroker_hook_registry.
		# Distributor guard first — same reasoning as POS Invoice's before_submit
		# above. A counter sale parks a gun long before any stock moves, so the
		# GunBroker listing has to come down on submit, not on the stock ledger
		# entry that may be days away.
		"on_submit": [
			"ffl_integrations.distributor.counter.guard_sales_invoice_submit",
			"ffl_integrations.gunbroker.doc_events.on_invoice_change",
			# An amendment is a submit nobody else books: the cancel that
			# preceded it reversed the fee accrual and unlinked the receipt.
			# Restricted to amendments inside the handler — a first submission
			# is booked by whatever raised it.
			"ffl_integrations.gunbroker.revenue.rebook_on_amended_submit",
		],
		"before_cancel": "ffl_integrations.distributor.counter.guard_sales_invoice_cancel",
		"on_cancel": [
			"ffl_integrations.gunbroker.doc_events.on_invoice_change",
			# Cancelling a GunBroker invoice has to take its seller-fee accrual with
			# it. That accrual is a separate Journal Entry and nothing cancels it on
			# its own, so it would stay on the ledger against a sale that no longer
			# exists — channel profit low for ever, monthly statement unreconcilable.
			# on_cancel rather than before_cancel: the reversal belongs after the
			# cancellation has actually succeeded.
			"ffl_integrations.gunbroker.revenue.reverse_fee_accrual_on_cancel",
		],
		# The counter-sale refusal. Nothing else in ffl_integrations hooks Sales
		# Invoice before_submit, so this key is new rather than extended.
		"before_submit": [
			"ffl_integrations.gunbroker.doc_events.block_sale_of_sold_gb_listing",
		],
	},
	# A hand-keyed GunBroker refund carries the order number in its remarks, and
	# ERPNext's own validate() rewrites that field unless Custom Remarks is
	# ticked. BEFORE_validate, not validate: a hooked validate runs after the
	# controller's, by which point set_remarks() has already thrown the text
	# away — the flag would be right on the next save and useless on the one
	# that mattered. List for the same reason as every other key here.
	"Payment Entry": {
		"before_validate": [
			"ffl_integrations.gunbroker.revenue.keep_gunbroker_remarks"],
	},
	# ShipStation is DELIBERATELY not hooked on Sales Invoice on_submit: the
	# push is a manual staff action — the Pending Order page's Ship button
	# calls ffl_integrations.shipstation.api.push_shipment, fully independent
	# of the bound-book Dispose step, so a ShipStation outage can never block
	# a sale or a gun shipment.
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"daily": [
		# SS credit memos post overnight: read yesterday's, fold the shorts onto
		# their Distributor Orders and alert for the consumer refund.
		"ffl_integrations.sports_south.tasks.poll_credits",
		"ffl_integrations.fastbound.tasks.reconcile_failed_pushes",
		"ffl_integrations.fastbound.tasks.reconcile_failed_deletes",
		"ffl_integrations.fastbound.client_api.sync_smart_lists_scheduled",
		"ffl_integrations.fastbound.tasks.alert_stuck_fastbound",
		# Everything a GunBroker order can be stuck halfway through. Every step
		# of that flow logs its failures rather than raising them, so a
		# bound-book exit is never rolled back — which also means nothing
		# objects afterwards unless something reads the wreckage daily.
		"ffl_integrations.gunbroker.audit.daily_gb_audit",
		# Card charges voided/refunded on the terminal or Payroc portal never
		# reach the POS; read the last week's captures back from the gateway.
		"ffl_integrations.payroc.reconcile.reconcile_payroc_charges",
	],
	# Hourly, not daily: a parked trade-in draft has a next-business-day
	# bound-book deadline (478.125(e)) — staff need the nudge the same day,
	# and notify_admins' dedup window keeps the digest to twice a day.
	"hourly": [
		"ffl_integrations.fastbound.tasks.alert_stale_trade_in_drafts",
		# Private copies of signed 4473 PDFs still Pending on the Form 4473 Log
		# (backfilled rows, FastBound outages); bounded per run.
		"ffl_integrations.fastbound.pdf_archive.sweep_pending_4473_pdfs",
		# GunBroker listing reconcile. Self-throttling: switched off is a no-op,
		# and the gate only enqueues. It is the ONLY path that asks GunBroker what
		# is actually true — every other one reacts to a local event, and events
		# go missing.
		"ffl_integrations.gunbroker.tasks.gb_listing_sweep",
		# Read-only mirror of every distributor invoice on the account (orders
		# placed outside the POS included) for the Distributor Orders page.
		"ffl_integrations.distributor.invoices.enqueue_sync",
	],
	# Distributor-generic cron. RSR feed ingest itself lives on the Data Service (15-min
	# timer there); the POS only syncs listings and watches the feed heartbeat.
	"cron": {
		"*/15 * * * *": [
			# Feed staleness watchdog: heartbeat older than an hour
			# alerts once per distributor and, when that distributor opts in, pushes
			# its listings to zero stock so an outage cannot oversell.
			"ffl_integrations.distributor.tasks.check_feed_staleness",
			# GunBroker order poll. Self-throttling: switched off is a no-op, and
			# the gate only enqueues. Fifteen minutes is the window in which a gun
			# can be sold on GunBroker and still be sitting on the shelf looking
			# available — which is the window the counter's pre-sale check covers.
			"ffl_integrations.gunbroker.orders.sync_orders",
			# Confirm the listings we asked GunBroker to end and could not verify at
			# the time. On this timer rather than the hourly sweep's because an
			# unconfirmed end is the one listing state that can still be BOUGHT while
			# the gun has already left the shelf.
			"ffl_integrations.gunbroker.end_pending.poll_end_pending",
			# Bid watch for live auctions: one /ItemsSelling read, so the first
			# bid locks the gun within fifteen minutes rather than an hour. A
			# no-op with no auction live.
			"ffl_integrations.gunbroker.tasks.gb_auction_watch",
			# Catalog master category mirror: the tree → Catalog Category,
			# Item Groups and each store's categories, then (Apply) the master's
			# product category changes. Mode on Data Service Settings › Categories;
			# Off is a no-op.
			"ffl_integrations.catalog_master.scheduled",
			# Local catalog (Data Service Settings › Catalog source = Local): queue
			# each due distributor lane on the `catalog` queue. A no-op otherwise.
			"ffl_integrations.catalog_local.sync.tick",
		],
		# A model's sale window opens or closes at midnight with no Item save to
		# say so; five minutes after ffl_core's daily sale-price refresh.
		"5 0 * * *": [
			"ffl_integrations.gunbroker.tasks.gb_model_price_sweep",
		],
		"*/10 * * * *": [
			# Carry GunBroker tracking numbers back from the label the store
			# bought. Only selects orders with a step still outstanding, so a day
			# with nothing shipping makes no calls at all.
			"ffl_integrations.gunbroker.shipping.poll_gb_tracking",
		],
		# Distributor Order poll gate (RSR Direct Connect). Self-throttling:
		# only enqueues a per-order poll for live orders (Placed/Ack/Hold/Partially
		# Shipped) whose per-order next_poll_at (30m→2h backoff) is due, across
		# enabled + poll-enabled distributors; alerts once on 14-day-stale orders.
		"*/30 * * * *": [
			"ffl_integrations.distributor.tasks.poll_distributor_orders",
		],
	},
}

# Testing
# -------

# before_tests = "ffl_integrations.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "ffl_integrations.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# GunBroker's webhook form refuses an endpoint path of 50+ characters and the real
# ones run to 58-74, so the URLs it is given are /api/method/gb.<slug>
# (see gunbroker/webhook_shield.py).
override_whitelisted_methods = {
	f"gb.{slug}": f"ffl_integrations.gunbroker.webhooks.{slug}"
	for slug in ("items_sold", "winning_bid_received", "order_status_update",
		"items_unsold_not_relisted", "fixed_price_listing_failed")
}
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ffl_integrations.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# GunBroker webhooks authenticate with HTTP Basic credentials of their own; Frappe would
# read that header as one of its API keys and answer its own 401. This lifts it off the
# five webhook URLs (and only those) before that check runs.
before_request = ["ffl_integrations.gunbroker.webhook_shield.shield_basic_auth"]
# after_request = ["ffl_integrations.utils.after_request"]

# Job Events
# ----------
# before_job = ["ffl_integrations.utils.before_job"]
# after_job = ["ffl_integrations.utils.after_job"]

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
# 	"ffl_integrations.auth.validate"
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

