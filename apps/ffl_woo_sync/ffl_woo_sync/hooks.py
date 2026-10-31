app_name = "ffl_woo_sync"
app_title = "FFL Woo Sync"
app_publisher = "OpenGunstore"
app_description = "Bidirectional WooCommerce sync (products + orders + stock)"
app_email = "info@oldsteelarsenal.com"
app_license = "GNU General Public License (v3)"

# Fixtures
# --------
# Export / import custom fields owned by this app.
fixtures = [
    {"dt": "Custom Field", "filters": [["module", "=", "FFL Woo Sync"]]}
]

# DocType JS
# ----------
doctype_js = {
    "Item": "public/js/item_woo.js",
    "Serial No": "public/js/serial_no_woo.js",
    "WooCommerce Settings": "public/js/woo_settings.js",
}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "ffl_woo_sync",
# 		"logo": "/assets/ffl_woo_sync/logo.png",
# 		"title": "FFL Woo Sync",
# 		"route": "/ffl_woo_sync",
# 		"has_permission": "ffl_woo_sync.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ffl_woo_sync/css/ffl_woo_sync.css"
# app_include_js = "/assets/ffl_woo_sync/js/ffl_woo_sync.js"

# include js, css files in header of web template
# web_include_css = "/assets/ffl_woo_sync/css/ffl_woo_sync.css"
# web_include_js = "/assets/ffl_woo_sync/js/ffl_woo_sync.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ffl_woo_sync/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "ffl_woo_sync/public/icons.svg"

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
# 	"methods": "ffl_woo_sync.utils.jinja_methods",
# 	"filters": "ffl_woo_sync.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "ffl_woo_sync.install.before_install"
# after_install = "ffl_woo_sync.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "ffl_woo_sync.uninstall.before_uninstall"
# after_uninstall = "ffl_woo_sync.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ffl_woo_sync.utils.before_app_install"
# after_app_install = "ffl_woo_sync.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ffl_woo_sync.utils.before_app_uninstall"
# after_app_uninstall = "ffl_woo_sync.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "ffl_woo_sync.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ffl_woo_sync.notifications.get_notification_config"

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

# Cross-app hooks (RSR Direct Connect)
# -----------------------------------------------
# `distributor_listing_push` is INVERTED relative to the rest of the hook table:
# ffl_integrations owns the listing pipeline and CALLS the hook; the WooCommerce
# side is here, so this app registers the implementation.  That inversion is what
# keeps ffl_integrations free of any ffl_woo_sync import (and vice versa) — the
# payload dict is the entire contract.
#
#     fn(item_code: str, payload: dict) -> None
#
# See woocommerce/distributor.py for the payload keys.
distributor_listing_push = [
    "ffl_woo_sync.woocommerce.distributor.distributor_listing_push",
]

# `serial_channel_exit` is inverted the same way, and for the same reason. A
# THIRD sales channel (GunBroker) shares this store's physical shelf, and the
# moment it takes a gun the two WooCommerce storefronts must stop offering it —
# otherwise one rifle is sold twice, and a GunBroker sale cannot be cancelled
# from the seller's side. ffl_integrations owns that channel and fires the hook;
# the storefront half is here.
#
#     fn(serial_no: str, *, channel: str, reason: str) -> None
#
# See woocommerce/channel.py for what each `reason` means and why all three do
# the same thing.
serial_channel_exit = [
    "ffl_woo_sync.woocommerce.channel.serial_channel_exit",
]

# `serial_channel_return` is its undo: GunBroker let go of a gun it held (order
# cancelled before invoicing, unit dropped or reassigned), so the page the exit
# took down goes back up.
#
#     fn(serial_no: str, *, channel: str, reason: str) -> None
serial_channel_return = [
    "ffl_woo_sync.woocommerce.channel.serial_channel_return",
]

# `channel_committed_units` / `channel_commitments_changed` are ffl_core's two
# hooks for the pool every sales channel shares (a uniform-price gun, or a box of
# ammunition, sold on more than one channel before it ships). This app answers
# the first with the units the web store has sold and not yet shipped, and hears
# the second when ANOTHER channel's number moves, to re-push the storefront.
#
#     fn(item_code: str) -> int
#     fn(item_code: str, *, source: str) -> None
#
# See ffl_core/reservations.py (committed_units, notify_channel_commitments_changed).
channel_committed_units = [
    "ffl_woo_sync.woocommerce.mapping.channel_committed_units",
]
channel_commitments_changed = [
    "ffl_woo_sync.woocommerce.channel.channel_commitments_changed",
]

# `map_meta_changed` is inverted too: ffl_integrations owns the per-brand MAP
# modes and calls it when a brand's mode changes; this app re-pushes the retail
# products whose `_osa_map_mode` that moves.
#
#     fn(item_codes: list[str]) -> None
map_meta_changed = [
    "ffl_woo_sync.woocommerce.tasks.map_meta_changed",
]

# `dropship_items_changed` is inverted the same way: ffl_integrations says which
# drop-ship Items' listing facts moved (a house's lead time — _osa_lead_days) and
# this app re-pushes their products.
#
#     fn(item_codes: list[str]) -> None
dropship_items_changed = [
    "ffl_woo_sync.woocommerce.tasks.dropship_items_changed",
]

# `store_stock_for_item` is inverted the same way: ffl_integrations' source
# switcher asks how many of an Item the store itself can sell online when its own
# stock competes with the distributors — the storefront's local stock rules
# (custody, consignment, web claims, web lines the store fills), so the quantity it
# compares is the one this app would publish; a uniform-price gun's free serials
# too (the store's cost is theirs).
#
#     fn(item_code: str) -> {"qty": int, "serials": list[str] | None}
store_stock_for_item = [
    "ffl_woo_sync.woocommerce.mapping.store_stock_for_item",
]

# ffl_core fires this with the Sales Invoice when a submitted counter order starts
# holding a gun without a submit/cancel event (Fulfill from local stock): the
# same reconcile the invoice's on_submit runs.
ffl_counter_hold_changed = [
    "ffl_woo_sync.woocommerce.doc_events.on_invoice_change",
]

# Document Events
# ---------------
# Auto-push to WooCommerce when POS records change.
# Handlers live in woocommerce/doc_events.py — each one guards against:
#   • woo_sync_inbound loop flag (changes caused by inbound webhooks are skipped)
#   • install / migrate / import system states
#   • missing woo_product_id (never auto-creates; only updates existing listings)

doc_events = {
    "Item": {
        "on_update": "ffl_woo_sync.woocommerce.doc_events.on_item_update",
    },
    "Serial No": {
        "on_update": "ffl_woo_sync.woocommerce.doc_events.on_serial_no_update",
    },
    "Stock Ledger Entry": {
        "on_submit": "ffl_woo_sync.woocommerce.doc_events.on_sle_submit",
        "on_cancel": "ffl_woo_sync.woocommerce.doc_events.on_sle_cancel",
    },
    # v16 stores a line's serials in a bundle, and the serial picker REWRITES an
    # existing bundle in place — its own request, committed before the invoice is
    # saved, so the invoice's own save can no longer tell that the gun changed.
    # The bundle's before/after entries are the only place the gun that left a
    # draft counter order is still visible. The handler acts on a DRAFT POS
    # Invoice only — the one voucher whose draft already holds its guns.
    "Serial and Batch Bundle": {
        "on_update": "ffl_woo_sync.woocommerce.doc_events.on_serial_batch_bundle_update",
    },
    # Counter orders park guns long before stock moves — a submitted (or
    # cancelled) invoice must reconcile the held guns' online listings.
    "Sales Invoice": {
        "on_submit": "ffl_woo_sync.woocommerce.doc_events.on_invoice_change",
        "on_cancel": "ffl_woo_sync.woocommerce.doc_events.on_invoice_change",
    },
    "POS Invoice": {
        # A parked POS sale holds its gun from the first DRAFT save — days
        # before submit. Nothing used to tell the storefront (DISP-2026-02000).
        "on_update": "ffl_woo_sync.woocommerce.doc_events.on_pos_draft_change",
        # ...and deleting the draft (the POS recycle bin / the manager cleanup
        # button) is what releases it again.
        "on_trash": "ffl_woo_sync.woocommerce.doc_events.on_pos_invoice_trash",
        # An in-store pickup's ticket was paid on WooCommerce: it completes on
        # that tender, in full, or not at all — never on a second, counter one.
        "before_submit": "ffl_woo_sync.woocommerce.pickup.require_web_tender_on_submit",
        "on_submit": [
            "ffl_woo_sync.woocommerce.doc_events.on_invoice_change",
            # An in-store pickup's counter invoice: submitting it IS the moment
            # the sale becomes real (the 4473 cleared its gate, stock is issued,
            # revenue is booked), so that is when its web order leaves the
            # Pending Order queue — not when somebody pressed Pickup.
            "ffl_woo_sync.woocommerce.pickup.close_web_order_on_pos_submit",
        ],
        "on_cancel": [
            "ffl_woo_sync.woocommerce.doc_events.on_invoice_change",
            # Cancelling the counter invoice returns the gun to stock, so the web
            # order has to go BACK in the queue. Without this it stayed
            # Transferred for ever: invisible, rifle on the shelf, sale unbooked,
            # and Pickup refusing to reopen it.
            "ffl_woo_sync.woocommerce.pickup.reopen_web_order_on_pos_cancel",
        ],
    },
}

# Scheduled Tasks
# ---------------
# Cron fires every 2 hours (finest supported frequency).  The gate function
# scheduled_reconcile() reads WooCommerce Settings and applies a _due() throttle
# based on the reconcile_frequency Select field (Every 2 Hours / Daily / Weekly /
# Disabled), so a single cron covers all frequency options.

# The catalog master's category mirror lives in ffl_integrations and
# reaches the stores only through these two hooks (the channel apps never import
# each other): link / rename / re-parent every store's product categories, and
# point one Item's store products at its category.
catalog_category_store_mirror = ["ffl_woo_sync.woocommerce.categories.mirror_stores"]
catalog_category_recategorize = ["ffl_woo_sync.woocommerce.categories.recategorize"]

scheduler_events = {
    "cron": {
        # Drop-ship status events the shop did not acknowledge (an outage on
        # either side) are re-posted hourly; no-op unless a store has the
        # outbound webhook secret set.
        "15 * * * *": [
            "ffl_woo_sync.woocommerce.status_events.resend_status_events",
        ],
        # One 2-hourly cron carries all three, and all three gate on the same
        # per-site auto_sync_enabled switch before touching a store.
        # scheduled_reconcile additionally self-throttles via reconcile_frequency
        # and scheduled_order_audit via a daily _due on last_order_audit_at,
        # covering the product reconcile AND the order-integrity audit (D2).
        # retry_stuck_serial_reservations needs no throttle of its own — it is a
        # no-op unless a serial row is genuinely stuck — but it IS bounded, by a
        # per-run LIMIT and an age cut-off, because each stuck order costs it one
        # synchronous REST call.
        "0 */2 * * *": [
            "ffl_woo_sync.woocommerce.tasks.scheduled_reconcile",
            "ffl_woo_sync.woocommerce.order_audit.scheduled_order_audit",
            "ffl_woo_sync.woocommerce.tasks.retry_stuck_serial_reservations",
            # Shipped on WooCommerce, still Pending Fulfillment here = a firearm
            # out of the building with no A&D entry. Read-only and self-throttled
            # to one report a day, so it rides the same 2-hourly tick rather than
            # needing its own schedule.
            "ffl_woo_sync.woocommerce.tasks.alert_shipped_undisposed_orders",
            # Transferred with no Sales Invoice / web invoice with an accessory
            # row and no Delivery Note = a completion job that never landed.
            # Re-runs the idempotent entry points and logs
            # each hit. Not gated on auto_sync — it never touches the store.
            "ffl_woo_sync.woocommerce.order_audit.scheduled_web_revenue_backstop",
        ],
    },
}

# Testing
# -------

# before_tests = "ffl_woo_sync.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "ffl_woo_sync.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "ffl_woo_sync.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ffl_woo_sync.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["ffl_woo_sync.utils.before_request"]
# after_request = ["ffl_woo_sync.utils.after_request"]

# Job Events
# ----------
# before_job = ["ffl_woo_sync.utils.before_job"]
# after_job = ["ffl_woo_sync.utils.after_job"]

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
# 	"ffl_woo_sync.auth.validate"
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

