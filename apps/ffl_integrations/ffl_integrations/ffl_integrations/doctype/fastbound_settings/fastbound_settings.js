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
				options: `<p>${__('Brings every gun in stock in your FastBound bound book into the POS as an item, an acquisition and opening stock. Guns already imported are updated from FastBound. It runs in the background.')}</p>`,
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
				label: __('Cut-over Date'),
				fieldname: 'cutover_date',
				fieldtype: 'Date',
				default: frappe.datetime.get_today(),
				depends_on: 'eval:!doc.dry_run',
				mandatory_depends_on: 'eval:!doc.dry_run',
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
	const before = frm.doc.last_import_result;
	frappe.call({
		method: 'ffl_integrations.fastbound.importer.start_import',
		args: {
			page_size: values.page_size || 100,
			dry_run: values.dry_run ? 1 : 0,
			aggregate_by: values.aggregate_by || 'auto',
			cutover_date: values.dry_run ? null : values.cutover_date,
		},
		callback(r) {
			if (!r.message) return;
			frappe.show_alert({ message: __('Import started. The result opens here when it finishes.'), indicator: 'blue' });
			waitForImport(frm, before);
		},
	});
}

// The fastbound_import_done event or, on a site without a socket server, polling.
function waitForImport(frm, before) {
	let done = false;
	const finish = () => {
		if (done) return;
		done = true;
		frappe.realtime.off('fastbound_import_done', finish);
		frm.reload_doc().then(() => showResultDialog(JSON.parse(frm.doc.last_import_result || '{}')));
	};
	frappe.realtime.on('fastbound_import_done', finish);
	const poll = () => {
		if (done) return;
		frappe.db.get_value('FastBound Settings', 'FastBound Settings', 'last_import_result').then((r) => {
			if (r.message.last_import_result && r.message.last_import_result !== before) finish();
			else setTimeout(poll, 5000);
		});
	};
	setTimeout(poll, 5000);
}

function showResultDialog(stats) {
	const esc = frappe.utils.escape_html;
	const dry = !!stats.dry_run;
	if (stats.error) {
		frappe.msgprint({ title: __('Import failed'), message: esc(stats.error), indicator: 'red' });
		return;
	}
	// Rows mirror ImportStats.as_dict so they add up to Fetched.
	const summaryRows = dry ? [
		[__('Fetched'), stats.fetched],
		[__('Would import or update'), stats.skipped],
		[__('No cost in FastBound (not imported)'), (stats.needs_cost || []).length],
		[__('Sold / deleted in FastBound (not imported)'), stats.disposed_skipped || 0],
		[__('Items it would group into'), stats.projected_items || 0],
	] : [
		[__('Fetched'), stats.fetched],
		[__('Created'), stats.created],
		[__('Updated from FastBound'), stats.updated || 0],
		[__('Skipped'), stats.skipped],
		[__('No cost in FastBound (not imported)'), (stats.needs_cost || []).length],
		[__('Sold / deleted in FastBound (not imported)'), stats.disposed_skipped || 0],
		[__('Errors'), stats.error_count || 0],
	];
	const table = (rows) => `<table class="table table-bordered table-sm">${
		rows.map((r) => `<tr>${r.map((c, i) => (i ? `<td>${c}</td>` : `<th>${c}</th>`)).join('')}</tr>`).join('')}</table>`;
	const section = (title, rows) => (rows.length ? `<h5 style="margin-top:16px">${esc(title)}</h5>${table(rows)}` : '');
	const gun = (g) => esc([g.serial, g.manufacturer, g.model].filter(Boolean).join(' · '));

	const html = table(summaryRows.map(([k, v]) => [esc(k), v]))
		+ section(__('Errors'), (stats.errors || []).map((e) => [esc(e.serial || e.fastbound_id || '?'), esc(e.error)]))
		+ section(__('No cost in FastBound: add the cost there, then import again'),
			(stats.needs_cost || []).map((g) => [esc(g.itemNumber || g.fastbound_id || ''), gun(g)]))
		+ section(__('Acquisition types recorded as Individual'),
			Object.entries(stats.unmapped_sources || {}).map(([k, n]) => [esc(k), n]))
		+ section(__('Same serial or no serial: imported under a tagged serial'),
			(stats.duplicate_groups || []).map((d) => [esc(d.ledger_serial), d.items.map(gun).join('<br>')]))
		+ section(__('Changed from FastBound'),
			(stats.corrected || []).map((c) => [esc(c.serial || ''), esc([c.change, c.from, c.to].filter(Boolean).join(' · '))]))
		+ section(__('Move to the right item by hand'),
			(stats.regroup_needed || []).map((c) => [esc(c.serial || c.fastbound_id || ''), esc(c.reason || '')]));

	const dialog = new frappe.ui.Dialog({
		title: dry ? __('Dry Run Complete') : __('Import Complete'),
		size: 'large',
		fields: [{fieldtype: 'HTML', options: html}],
		primary_action_label: __('Done'),
		primary_action() { dialog.hide(); },
	});
	dialog.show();
}
