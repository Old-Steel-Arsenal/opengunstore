// FastBound Settings client script.
//
// Three actions, all whitelisted Python entry-points that only ever surface
// what the server returns; no credentials touch the client:
//   Test Connection    — on the toolbar: the thing to press after every change.
//   Sync FastBound Lists / Import Bound Book — in the FastBound menu. The
//   import is a one-time migration, not something to make the primary button.

frappe.ui.form.on('FastBound Settings', {
	refresh(frm) {
		// Every button in this group makes a live FastBound call. With the
		// integration switched off they can only ever report "switched off", so
		// offer nothing rather than three ways to be told the same thing. The
		// server refuses regardless — this is the UI half.
		if (!frm.doc.enabled) return;
		const $test = frm.add_custom_button(__('Test Connection'), () => testConnection(frm));
		$test.removeClass('btn-default').addClass('btn-primary');
		frm.add_custom_button(__('Sync FastBound Lists'), () => syncSmartLists(frm), __('FastBound'));
		frm.add_custom_button(__('Import Bound Book'), () => promptImport(frm), __('FastBound'));
	},
});

function testConnection(frm) {
	frappe.call({
		method: 'ffl_integrations.fastbound.client_api.test_connection',
		freeze: true,
		freeze_message: __('Calling FastBound...'),
		callback(r) {
			if (r.message && r.message.ok) {
				frappe.show_alert({
					message: __('FastBound connection OK. Account: {0}', [r.message.account_name || frm.doc.account_num]),
					indicator: 'green',
				});
			}
		},
	});
}

function syncSmartLists(frm) {
	frappe.call({
		method: 'ffl_integrations.fastbound.client_api.sync_smart_lists',
		freeze: true,
		freeze_message: __('Fetching acquisition + disposition types from FastBound...'),
		callback(r) {
			if (r.message && r.message.ok) {
				frappe.show_alert({
					message: __('Synced {0} acquisition + {1} disposition type(s) from FastBound.',
						[r.message.acquire_count, r.message.dispose_count]),
					indicator: 'green',
				});
				frm.reload_doc();
			}
		},
	});
}

function promptImport(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__('Save your changes first, then run import.'));
		return;
	}

	const dialog = new frappe.ui.Dialog({
		title: __('Import Bound Book from FastBound'),
		fields: [
			{
				fieldtype: 'HTML',
				options: `<p>Pulls every item from your FastBound bound book into POS as Item / FFL Acquisition / Stock Receipt; items already imported are skipped, and a large book can take several minutes.</p>`,
			},
			{
				label: __('Aggregate By'),
				fieldname: 'aggregate_by',
				fieldtype: 'Select',
				options: 'auto\nmodel',
				default: 'auto',
				description: __('Auto groups by SKU, UPC or part number first; pick model when distributor SKUs are noisy.'),
			},
			{
				label: __('Page Size'),
				fieldname: 'page_size',
				fieldtype: 'Int',
				default: 100,
			},
			{
				label: __('Dry Run'),
				fieldname: 'dry_run',
				fieldtype: 'Check',
				description: __('If checked, only count what would be imported. Nothing is written.'),
			},
		],
		primary_action_label: __('Start Import'),
		primary_action(values) {
			dialog.hide();
			runImport(frm, values);
		},
	});
	dialog.show();
}

function runImport(frm, values) {
	frappe.call({
		method: 'ffl_integrations.fastbound.importer.import_bound_book',
		args: {
			page_size: values.page_size || 100,
			dry_run: values.dry_run ? 1 : 0,
			aggregate_by: values.aggregate_by || 'auto',
		},
		freeze: true,
		freeze_message: __('Importing from FastBound — this may take a few minutes...'),
		callback(r) {
			if (!r.message) return;
			showResultDialog(r.message, values.dry_run);
			frm.reload_doc();
		},
	});
}

function showResultDialog(stats, dry) {
	// Rows mirror ImportStats.as_dict so they add up to Fetched. A dry run
	// counts every in-stock gun under "skipped" (nothing is written).
	const summaryRows = dry ? [
		[__('Fetched'), stats.fetched],
		[__('Would import / resync'), stats.skipped],
		[__('Sold / deleted in FastBound (not imported)'), stats.disposed_skipped || 0],
		[__('Items it would group into'), stats.projected_items || 0],
		[__('Errors'), stats.error_count || 0],
	] : [
		[__('Fetched'), stats.fetched],
		[__('Created'), stats.created],
		[__('Updated (resynced)'), stats.updated || 0],
		[__('Skipped'), stats.skipped],
		[__('Sold / deleted in FastBound (not imported)'), stats.disposed_skipped || 0],
		[__('Errors'), stats.error_count || 0],
	];
	const summaryHtml = `<table class="table table-bordered" style="margin-top:12px">${
		summaryRows.map(([k, v]) => `<tr><th>${frappe.utils.escape_html(k)}</th><td>${v}</td></tr>`).join('')
	}</table>`;

	const errorsHtml = stats.errors && stats.errors.length
		? `<h5>${__('Errors')}</h5><ul>${
			stats.errors.map((e) => {
				const id = frappe.utils.escape_html(e.serial || e.fastbound_id || '?');
				const msg = frappe.utils.escape_html(e.error);
				return `<li><b>${id}</b>: ${msg}</li>`;
			}).join('')
		}</ul>`
		: '';

	const dialog = new frappe.ui.Dialog({
		title: dry ? __('Dry Run Complete') : __('Import Complete'),
		size: 'large',
		fields: [{fieldtype: 'HTML', options: summaryHtml + errorsHtml}],
		primary_action_label: __('Done'),
		primary_action() { dialog.hide(); },
	});
	dialog.show();
}
