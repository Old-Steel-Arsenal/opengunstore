// FFL Disposition — populate the "Disposition Type" dropdown from the FastBound
// DisposeType SmartList (account presets + custom), cached on FastBound Settings.
// Refresh via FastBound Settings → "Sync FastBound Lists".
//
// The doctype's static options stay as a pre-sync fallback: disposition_type is
// required and drives compliance gating, so the dropdown must never be empty.
// Soft-coupled to ffl_integrations — the call fails silently if it's absent.

// Types FastBound may offer but this system never creates manually:
//   Sale                              — duplicate of Retail Sale
//   Donation, Private Party Transfer  — handle directly in FastBound
// Retail Sale is filtered out for NEW docs without a fastbound_id; the
// webhook-imported ones (4473 flow) keep it visible.
const FFL_DISPOSITION_MANUAL_FORBIDDEN = new Set([
	'Sale', 'Donation', 'Private Party Transfer',
]);

// Disposition types whose destination_ffl field is shown (and required).
// Kept in sync with the JSON depends_on / mandatory_depends_on so the
// Lookup-FFL button shows up exactly when the field does.
const FFL_DISPOSITION_DESTINATION_TYPES = new Set([
	'FFL Transfer', 'Dealer Transfer', 'Return',
]);

// Types this system pushes to FastBound itself — the mirror of
// ffl_integrations.fastbound.client.PUSHABLE_DISPOSITION_TYPES (transfers +
// destruction). Everything else is a 4473 / to-individual disposition that
// FastBound's own wizard owns. This set only decides whether the button is
// DRAWN; the server re-checks it, because drawing the wrong button here would
// otherwise let someone record a sale to an individual as a plain bound-book
// entry. A test asserts the two lists still match.
const FFL_DISPOSITION_PUSHABLE_TYPES = new Set([
	'Dealer Transfer', 'FFL Transfer', 'Return', 'Scrap', 'Destroyed',
]);

// Apply the manual-forbidden + new-doc-Retail-Sale filter consistently to
// whichever options list we end up using (synced or static fallback).
function _filterDispositionTypes(types, frm) {
	const isManualNew = frm.doc.__islocal && !frm.doc.fastbound_id;
	const filtered = types.filter((t) => {
		if (FFL_DISPOSITION_MANUAL_FORBIDDEN.has(t)) return false;
		if (t === 'Retail Sale' && isManualNew) return false;
		return true;
	});
	// Keep an already-stored value selectable even if it's no longer in the
	// filtered list (e.g. webhook-imported Retail Sale).
	const cur = frm.doc.disposition_type;
	if (cur && !filtered.includes(cur)) filtered.unshift(cur);
	return filtered;
}

frappe.ui.form.on('FFL Disposition', {
	refresh(frm) {
		// Apply the manual-forbidden filter to the STATIC options immediately so
		// that even when the FastBound SmartList sync hasn't run yet (fresh
		// install) or briefly fails, the dropdown doesn't expose 'Retail Sale' /
		// 'Sale' / 'Donation' / 'Private Party Transfer' to manual creation.
		const staticOptions = (frm.fields_dict.disposition_type?.df?.options || '')
			.split('\n').filter(Boolean);
		if (staticOptions.length) {
			const filteredStatic = _filterDispositionTypes(staticOptions, frm);
			frm.set_df_property('disposition_type', 'options', filteredStatic.join('\n'));
			frm.refresh_field('disposition_type');
		}
		// Disposition Type dropdown — sync override from FastBound when available.
		frappe.call({
			method: 'ffl_integrations.fastbound.client_api.get_dispose_types',
			callback(r) {
				const types = (r.message || []).slice();
				if (!types.length) return;  // not synced yet — keep filtered static options
				const filtered = _filterDispositionTypes(types, frm);
				frm.set_df_property('disposition_type', 'options', filtered.join('\n'));
				frm.refresh_field('disposition_type');
			},
			error() { /* FastBound integration unavailable — keep filtered static options */ },
		});

		// "Lookup FFL via ATF" button — only when destination_ffl is in play.
		// Hits the same EZ Check endpoint the ATF FFL Record list view uses
		// (lookup_and_create), then sets destination_ffl to the resolved
		// record so the Link field validates against the local library.
		if (FFL_DISPOSITION_DESTINATION_TYPES.has(frm.doc.disposition_type)) {
			frm.add_custom_button(__('Lookup FFL via ATF'), () => {
				_openAtfLookupDialog(frm);
			});
		}

		// "Retry FastBound Push" — a submitted disposition whose push failed had
		// no way back but the daily reconcile sweep, i.e. up to 24 h with a
		// firearm gone and no bound-book entry (DISP-2026-02000). Shown ONLY on
		// the failure state: submitted, no fastbound_id, and a type we push.
		if (frm.doc.docstatus === 1 && !frm.doc.fastbound_id
				&& FFL_DISPOSITION_PUSHABLE_TYPES.has(frm.doc.disposition_type)) {
			frm.add_custom_button(__('Retry FastBound Push'), () => {
				frappe.call({
					method: 'ffl_integrations.fastbound.reconcile.retry_disposition_push',
					args: { name: frm.doc.name },
					freeze: true,
					freeze_message: __('Pushing to FastBound…'),
					callback(r) {
						const m = r.message || {};
						frappe.show_alert({
							message: m.ok
								? __('Booked in FastBound as {0}', [m.fastbound_id])
								: __('FastBound refused it again: {0}', [m.fastbound_sync_error || '?']),
							indicator: m.ok ? 'green' : 'red',
						}, 10);
						frm.reload_doc();
					},
				});
			}, __('FastBound'));
		}
	},

	disposition_type(frm) {
		// Toggle the Lookup-FFL button when the type changes. Don't call
		// frm.refresh() — that re-fires the get_dispose_types HTTP and risks
		// stacking duplicate custom buttons across rapid toggles.
		const needsButton = FFL_DISPOSITION_DESTINATION_TYPES.has(frm.doc.disposition_type);
		const existing = frm.custom_buttons && frm.custom_buttons[__('Lookup FFL via ATF')];
		if (needsButton && !existing) {
			frm.add_custom_button(__('Lookup FFL via ATF'), () => _openAtfLookupDialog(frm));
		} else if (!needsButton && existing) {
			frm.remove_custom_button(__('Lookup FFL via ATF'));
		}
	},

	item(frm) {
		// Refresh the Serial autocomplete options to the in-stock serials of
		// the selected Item. Every firearm now has has_serial_no=1 (the legacy
		// Used Firearm / Item.firearm_serial path is gone), so Serial No is the
		// only source.
		//
		// CLEAR a stale serial bound to the previous Item before refilling —
		// auto-pick is guarded by `!frm.doc.serial`, so a leftover serial
		// would silently persist (and the FastBound push would dispose the
		// wrong firearm). Server-side only_active_serial fill won't catch it
		// either (it short-circuits on truthy serial).
		const ctrl = frm.fields_dict.serial;
		if (!ctrl) return;
		if (frm.doc.serial) {
			// Verify the stale serial actually belongs to the new item; if
			// not, clear it. (set_value with same value is a no-op so
			// matching serials don't re-trigger anything.)
			frappe.db.get_value('Serial No', frm.doc.serial, 'item_code').then((r) => {
				const owner = (r && r.message && r.message.item_code) || null;
				if (owner !== frm.doc.item) {
					frm.set_value('serial', null);
				}
			});
		}
		if (!frm.doc.item) {
			if (typeof ctrl.set_data === 'function') ctrl.set_data([]);
			return;
		}
		// Use the shared helper instead of a raw get_list so the POS picker
		// and this picker stay aligned on what counts as 'in stock'.
		frappe.call({
			method: 'ffl_core.firearm.available_serials_for_items',
			args: { item_codes: [frm.doc.item] },
		}).then((r) => {
			const names = ((r && r.message) || {})[frm.doc.item] || [];
			if (typeof ctrl.set_data === 'function') {
				ctrl.set_data(names);
			} else {
				frm.set_df_property('serial', 'options', names.join('\n'));
				frm.refresh_field('serial');
			}
			if (names.length === 1 && !frm.doc.serial) {
				frm.set_value('serial', names[0]);
			}
		});
	},
});

function _openAtfLookupDialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Lookup Destination FFL via ATF EZ Check'),
		size: 'small',
		fields: [
			{
				fieldtype: 'Data',
				fieldname: 'ffl_number',
				label: __('FFL Number'),
				reqd: 1,
				description: __('Format: 1-99-999-99-9X-99999; hyphens are optional.'),
			},
		],
		primary_action_label: __('Lookup & Use'),
		primary_action(values) {
			const num = (values.ffl_number || '').trim();
			if (!num) return;
			frappe.call({
				method: 'ffl_integrations.atf.ez_check.lookup_and_create',
				args: { ffl_number: num },
				freeze: true,
				freeze_message: __('Verifying with ATF EZ Check…'),
				callback(r) {
					const m = r.message || {};
					if (!m.ok || !m.name) return;
					d.hide();
					frappe.show_alert({
						message: __('Saved {0} · {1}', [m.name, m.licensee_name || m.status]),
						indicator: m.status === 'Verified' ? 'green' : 'orange',
					});
					frm.set_value('destination_ffl', m.name);
				},
			});
		},
	});
	d.show();
	setTimeout(() => d.fields_dict.ffl_number.input?.focus(), 60);
}
