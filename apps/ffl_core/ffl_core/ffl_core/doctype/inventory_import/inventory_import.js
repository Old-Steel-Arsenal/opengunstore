// Inventory Import: Download Template → attach → Preview → Import.
const II = 'ffl_core.ffl_core.doctype.inventory_import.inventory_import.';
const II_TEMPLATE = '/assets/ffl_core/templates/inventory-import-template.csv';

frappe.ui.form.on('Inventory Import', {
	setup(frm) {
		frappe.realtime.on('inventory_import_done', () => frm.reload_doc());
	},
	refresh(frm) {
		frm.disable_save();
		const wh = frm.doc.__onload && frm.doc.__onload.default_warehouse;
		if (!frm.doc.warehouse && wh) frm.set_value('warehouse', wh);
		frm.add_custom_button(__('Download Template'), () => window.open(II_TEMPLATE));
		if (frm.doc.csv_file) {
			frm.add_custom_button(__('Preview'), () => iiPreview(frm));
		}
		const last = frm.doc.last_run ? JSON.parse(frm.doc.last_run) : null;
		iiRender(frm, last, last ? __('Last import, {0}', [frappe.datetime.str_to_user(last.finished_at)]) : '');
	},
	csv_file: (frm) => frm.save(),
	warehouse: (frm) => frm.save(),
	posting_date: (frm) => frm.save(),
});

function iiPreview(frm) {
	frappe.call({
		method: II + 'preview',
		freeze: true,
		freeze_message: __('Checking every row...'),
		callback(r) {
			const res = r.message;
			iiRender(frm, res, __('Preview'));
			const ready = res.summary.counts.matched + res.summary.counts.catalog + res.summary.counts.new;
			if (!ready) return;
			frm.add_custom_button(__('Import {0} Rows', [ready]), () => {
				frappe.confirm(
					__('Bring in {0} units worth {1} as opening stock in {2}, as of {3}?',
						[res.summary.units, format_currency(res.summary.value), res.warehouse,
							frappe.datetime.str_to_user(frm.doc.posting_date)]),
					() => frappe.call({
						method: II + 'start_import',
						callback() {
							frappe.show_alert({ message: __('Import started. This page updates when it finishes.'), indicator: 'blue' });
							frm.remove_custom_button(__('Import {0} Rows', [ready]));
							iiWait(frm, frm.doc.last_run);
						},
					}));
			}).removeClass('btn-default').addClass('btn-primary');
		},
	});
}

// The realtime event reloads the page; this covers a site without a socket server.
function iiWait(frm, before) {
	setTimeout(() => frappe.db.get_value('Inventory Import', 'Inventory Import', 'last_run').then((r) => {
		if (r.message.last_run !== before) frm.reload_doc();
		else iiWait(frm, before);
	}), 4000);
}

function iiRender(frm, res, title) {
	const $w = frm.get_field('result').$wrapper.empty();
	if (!res) return;
	const esc = frappe.utils.escape_html;
	if (res.error) {
		$w.html(`<div class="alert alert-danger">${esc(res.error)}</div>`);
		return;
	}
	const c = res.summary.counts;
	const pills = [
		[__('Existing items'), c.matched, 'blue'],
		[__('From distributor catalog'), c.catalog, 'blue'],
		[__('New items'), c.new, 'blue'],
		[__('Skipped'), c.skipped, 'orange'],
		[__('Errors'), c.error, 'red'],
	].map(([label, n, color]) => `<span class="indicator-pill ${n ? color : 'gray'}" style="margin-right:8px">${label}: ${n}</span>`).join('');
	const problems = res.rows.filter((r) => r.outcome === 'skipped' || r.outcome === 'error');
	const table = problems.length ? `
		<table class="table table-bordered table-sm" style="margin-top:12px">
			<thead><tr><th>${__('Line')}</th><th>${__('UPC')}</th><th>${__('Name')}</th><th>${__('Problem')}</th></tr></thead>
			<tbody>${problems.map((r) => `<tr><td>${r.line}</td><td>${esc(r.upc || '')}</td><td>${esc(r.name || '')}</td><td>${esc(r.message || '')}</td></tr>`).join('')}</tbody>
		</table>` : '';
	const recs = (res.reconciliations || []).map((n) => `<a href="/app/stock-reconciliation/${encodeURIComponent(n)}">${esc(n)}</a>`).join(', ');
	$w.html(`
		<h5>${esc(title)} <button class="btn btn-xs btn-default ii-report" style="margin-left:8px">${__('Download Report')}</button></h5>
		<div>${pills}</div>
		<p style="margin-top:8px">${__('{0} units, {1} at cost', [res.summary.units, format_currency(res.summary.value)])}${recs ? ' · ' + recs : ''}</p>
		${table}`);
	$w.find('.ii-report').on('click', () => iiReport(res.rows));
}

function iiReport(rows) {
	const cols = ['line', 'upc', 'name', 'outcome', 'item_code', 'qty', 'cost', 'message'];
	const cell = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
	const text = [cols.join(','), ...rows.map((r) => cols.map((c) => cell(r[c])).join(','))].join('\n');
	const a = document.createElement('a');
	a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
	a.download = 'inventory-import-report.csv';
	a.click();
	URL.revokeObjectURL(a.href);
}
