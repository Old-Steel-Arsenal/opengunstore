# Ext Hello — started from `make ext-new` (OpenGunstore): the standard extension
# skeleton, working. A manifest, a doc_events handler that honours the store's
# switch, a gated and documented endpoint, a button on the POS through the
# registry, a release note, and the tests every extension owes (switched off /
# no role / repeat call). Replace the greeting with your feature.
app_name = "ext_hello"
app_title = "Ext Hello"
app_publisher = "Acme Software LLC"
app_description = "Says hello to a customer from the POS. A starting point for an extension, not a feature."
app_license = "MIT"
required_apps = ["ffl_core"]

# Extension manifest — the single source of truth about
# this extension; the registry snapshots it into POS Extension.
gunshop_extension = {
	"id": "ext_hello",
	"title": "Ext Hello",
	"publisher": "Acme Software LLC",
	"publisher_id": "ext",
	"version": "0.1.0",
	"requires_core": ">=0.1.0,<2",
	"requires_extensions": {},
	"permissions": ["doc_events:Customer", "pos_ui"],
	"license_required": False,
	# Shown to the manager on the Extensions page: where to read more, whom to ask.
	"homepage": "",
	"support": "",
	"release_notes": "change_log",
	# Optional: one sentence the Extensions page shows before turning it off.
	"disable_notice": "ext_hello.hooks_impl.customer.disable_notice",
}

app_include_js = ["ext_hello.bundle.js"]

doc_events = {
	"Customer": {"after_insert": "ext_hello.hooks_impl.customer.after_insert"},
}
