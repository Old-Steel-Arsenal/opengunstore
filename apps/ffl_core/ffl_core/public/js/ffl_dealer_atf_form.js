// FFL-dealer helpers shared by the Supplier and Customer forms (hooks.py doctype_js
// loads this file first, in the same form-script scope as supplier_form.js /
// customer_form.js).
// - "Search ATF" opens a dialog that searches the local ATF FFL Record cache
//   (populated by the monthly CSV loader) and fills the dealer fields from the
//   selected row.
// - "Verify FFL" hits the live ATF eZ Check; for a customer, the dealer-shipment
//   gate (ffl_core.compliance.dealer_transfer) requires Verified + unexpired
//   before firearms may ship to them.
// - Picking the formal directory link (atf_ffl_record) fills the license fields.

// Wire the FFL buttons and the directory-link fill on one doctype. `verifyMethod`
// and `verifyArg` are that doctype's eZ Check endpoint and its party argument.
function fflWireAtfDealerForm(doctype, verifyMethod, verifyArg) {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			if (!frm.doc.is_ffl_dealer) return;
			frm.add_custom_button(__('Search ATF'), () => openSearchDialog(frm), __('FFL'));
			if (!frm.is_new()) {
				frm.add_custom_button(__('Verify FFL'), () => verifyLive(frm, verifyMethod, verifyArg), __('FFL'));
			}
		},
		// Picking the formal directory link fills the license fields immediately
		// (the server validate re-syncs authoritatively on save).
		atf_ffl_record(frm) {
			fflFillFromRecord(frm);
		},
	});
}

function openSearchDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __('Search ATF FFL'),
		size: 'large',
		fields: [
			{label: __('FFL # or licensee name'), fieldname: 'q', fieldtype: 'Data', reqd: 1},
			{fieldtype: 'HTML', fieldname: 'results'},
		],
		primary_action_label: __('Search'),
		primary_action(values) {
			frappe.call({
				method: 'ffl_integrations.atf.search.search',
				args: {query: values.q, limit: 20},
				callback(r) { renderResults(dialog, frm, r.message || []); },
			});
		},
	});
	dialog.show();
}

function renderResults(dialog, frm, rows) {
	const $area = dialog.fields_dict.results.$wrapper;
	// National FFL search off (frappe.boot.ffl_capabilities): only this store's
	// FFL records were searched — the shared notice says so, above any rows.
	const notice = (window.ffl && ffl.capabilities && ffl.capabilities.isOff('ffl_national_search'))
		? ffl.capabilities.notice('ffl_national_search') : '';
	if (!rows.length) {
		$area.html(notice || `<p style="color:var(--text-muted)">${__('No matches.')}</p>`);
		return;
	}
	const html = `
		<table class="table table-sm" style="margin-top:8px">
			<thead><tr>
				<th>${__('FFL #')}</th><th>${__('Licensee')}</th>
				<th>${__('City, State')}</th><th>${__('Expiry')}</th><th></th>
			</tr></thead>
			<tbody>
				${rows.map((r, i) => `
					<tr>
						<td>${frappe.utils.escape_html(r.ffl_number)}</td>
						<td>${frappe.utils.escape_html(r.licensee_name || '')}<br>
							<span style="color:var(--text-muted)">${frappe.utils.escape_html(r.licensee_dba || '')}</span></td>
						<td>${frappe.utils.escape_html((r.city || '') + ', ' + (r.state || ''))}</td>
						<td>${r.expiry_date || ''}</td>
						<td><button class="btn btn-xs btn-primary" data-idx="${i}">${__('Pick')}</button></td>
					</tr>`).join('')}
			</tbody>
		</table>`;
	$area.html(notice + html);
	$area.find('button[data-idx]').on('click', (e) => {
		const idx = parseInt(e.currentTarget.dataset.idx, 10);
		applyRow(frm, rows[idx]);
		dialog.hide();
	});
}

function applyRow(frm, r) {
	// A different licence (renewals change the number): drop the old directory link, or
	// the save-time sync (atf_link.sync_party_atf_link) puts the old number back. With
	// it blank, that sync re-links to the picked number when the directory has it.
	if (frm.doc.atf_ffl_record && frm.doc.atf_ffl_record !== r.ffl_number) frm.set_value('atf_ffl_record', '');
	frm.set_value('ffl_number', r.ffl_number);
	// ffl_license_type is a Select of 2-digit codes; the cache stores
	// the full label ("07 - Manufacturer of ..."), strip to the prefix.
	const code = (window.gunstore && window.gunstore.extractLicenseCode)
		? window.gunstore.extractLicenseCode(r.license_type)
		: (r.license_type || '').match(/^\s*(\d{2})/)?.[1] || '';
	frm.set_value('ffl_license_type', code);
	frm.set_value('licensee_name', r.licensee_name || '');
	frm.set_value('licensee_address', [r.street, r.street2, r.city, r.state, r.zip]
		.filter(Boolean).join(', '));
	if (r.expiry_date) frm.set_value('ffl_expiry_date', r.expiry_date);
	// Local cache fill is NOT a live verification — the user must click 'Verify FFL'
	// (shipping to a dealer customer insists on status Verified). Leave status Pending until then.
	frm.set_value('ez_check_status', 'Pending');
	// Don't update ez_check_last_run — that field tracks live ATF calls, not local cache fills.
	frappe.show_alert({
		message: __('Filled from ATF FFL Record. Click Verify FFL to confirm against the live ATF.'),
		indicator: 'orange',
	});
}

async function verifyLive(frm, method, argName) {
	// The server verifies the SAVED number and the callback reloads the form: save an
	// unsaved Search ATF fill first, or the old licence is checked and the fill lost.
	// frm.save() resolves even when the save fails, hence the second is_dirty().
	if (frm.is_dirty()) {
		await frm.save();
		if (frm.is_dirty()) return;
	}
	frappe.call({
		method,
		args: {[argName]: frm.doc.name},
		freeze: true,
		freeze_message: __('Calling ATF...'),
		callback(r) {
			if (!r.message) return;
			const indicator = r.message.status === 'Verified' ? 'green'
				: r.message.status === 'Expired' ? 'orange' : 'red';
			frappe.show_alert({
				message: __('FFL status: {0}', [r.message.status]),
				indicator,
			});
			frm.reload_doc();
		},
	});
}

function fflFillFromRecord(frm) {
	if (!frm.doc.atf_ffl_record) return;
	frappe.db.get_doc('ATF FFL Record', frm.doc.atf_ffl_record).then((rec) => {
		frm.set_value('ffl_number', rec.ffl_number);
		const code = (rec.license_type || '').match(/^\s*(\d{2})/)?.[1] || '';
		if (code) frm.set_value('ffl_license_type', code);
		if (rec.licensee_name) frm.set_value('licensee_name', rec.licensee_name);
		frm.set_value('licensee_address', [rec.street, rec.street2, rec.city,
			[rec.state, rec.zip].filter(Boolean).join(' ')].filter(Boolean).join(', '));
		if (rec.expiry_date) frm.set_value('ffl_expiry_date', rec.expiry_date);
	});
}
