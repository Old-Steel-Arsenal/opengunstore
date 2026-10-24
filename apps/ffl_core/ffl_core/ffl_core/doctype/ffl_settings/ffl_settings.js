// FFL Settings client script.
//
// One toolbar action: Verify Our FFL runs the configured live EZ Check
// provider against this store's own licence — the form's Test Connection.
// The endpoint lives in ffl_integrations (the provider code is there), so
// the button only appears when that app is installed.

frappe.ui.form.on('FFL Settings', {
	setup(frm) {
		frm.set_query('peace_officer_exempt_tax_account', () => ({
			filters: { account_type: 'Tax', is_group: 0 },
		}));
	},
	// Store Customer ID Numbers on -> off erases every stored DOB / ID number when
	// saved (FFLSettings.on_update). The confirm sits in the save itself, so
	// Ctrl+S cannot get past it; ticking the box again after a change of mind
	// asks again.
	before_save(frm) {
		return fflEraseGate(frm);
	},
	store_customer_id_numbers(frm) {
		frm.__fflEraseOk = false;
	},
	refresh(frm) {
		// What the database holds (refresh runs after every save too).
		frm.__fflIdsSaved = !!frm.doc.store_customer_id_numbers;
		frm.__fflEraseOk = false;
		if (!frappe.boot.versions || !frappe.boot.versions.ffl_integrations) return;
		const $btn = frm.add_custom_button(__('Verify Our FFL'), () => verifyOwnFFL(frm));
		$btn.removeClass('btn-default').addClass('btn-primary');
	},
});

// before_save handlers are awaited, so the count is fetched fresh here (never
// stale, never "not back yet"). Nothing stored = nothing to confirm. A failed
// count request fails the save: no erase without a number to show.
// No (or closing the dialog) puts the tick back; Yes marks it confirmed and saves again.
function fflEraseGate(frm) {
	if (!frm.__fflIdsSaved || frm.doc.store_customer_id_numbers || frm.__fflEraseOk) return;
	return frappe.call({method: 'ffl_core.customer_profile.get_stored_id_counts'}).then((r) => {
		const total = (r.message || {}).total || 0;
		if (!total) return;
		frappe.validated = false;
		frappe.confirm(
			__('Turning this off erases {0} stored ID number(s) and date(s) of birth. Attached ID copies, change history and deleted records are kept. Continue?', [total]),
			() => {
				frm.__fflEraseOk = true;
				frm.save();
			},
			() => frm.set_value('store_customer_id_numbers', 1),
		);
	});
}
if (typeof module === 'object' && module.exports) module.exports = {fflEraseGate};

function verifyOwnFFL(frm) {
	frappe.call({
		method: 'ffl_integrations.atf.ez_check_api.verify_own_ffl',
		freeze: true,
		freeze_message: __('Checking with {0}...', [frm.doc.live_check_provider || 'ATF']),
		callback(r) {
			const m = r.message || {};
			if (m.ok) {
				frappe.show_alert({
					message: __('FFL verified: {0} (expires {1})', [m.licensee_name || frm.doc.own_ffl_license, m.expiry_date || '—']),
					indicator: 'green',
				});
			} else {
				frappe.msgprint({
					title: __('FFL verification did not succeed'),
					indicator: 'orange',
					message: m.error || __('Status: {0}', [m.status]),
				});
			}
		},
	});
}
