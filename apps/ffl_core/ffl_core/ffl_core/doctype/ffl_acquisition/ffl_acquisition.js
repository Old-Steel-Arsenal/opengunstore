// FFL Acquisition — populate two dropdowns dynamically:
//   • Acquisition Type ← FastBound AcquireType SmartList (presets + custom),
//     cached on FastBound Settings; refresh via "Sync FastBound Lists".
//   • Receive Source   ← FFL Settings → Receive Sources (admin-edited list).
//
// Both are soft-coupled — calls fail silently if the back-end is unavailable.
frappe.ui.form.on('FFL Acquisition', {
	refresh(frm) {
		// Acquisition Type (quiet: FastBound integration may be unavailable)
		loadFormSelect(frm, 'transaction_type', 'ffl_integrations.fastbound.client_api.get_acquire_types', true);
		// Receive Source
		loadFormSelect(frm, 'receive_source', 'ffl_core.api.receive_goods.get_receive_sources', false);
	},
});

function loadFormSelect(frm, field, method, quiet) {
	ffl.loadSelectOptions({
		method, quiet,
		get: () => frm.doc[field],
		set: (options) => {
			frm.set_df_property(field, 'options', options);
			frm.refresh_field(field);
		},
	});
}
