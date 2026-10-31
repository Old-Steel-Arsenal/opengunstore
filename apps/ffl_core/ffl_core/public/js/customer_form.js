// Customer form. The FFL dealer helpers (Search ATF / Verify FFL / directory-link
// fill; the dealer-shipment gate in ffl_core.compliance.dealer_transfer requires
// Verified + unexpired before firearms may ship to this customer) live in
// ffl_dealer_atf_form.js, loaded before this file (hooks.py doctype_js).

fflWireAtfDealerForm('Customer', 'ffl_integrations.atf.ez_check_api.verify_customer_ffl', 'customer_name');

frappe.ui.form.on('Customer', {
	// Structured individual name (setup/customer_identity.py): compose the
	// required Customer Name from the parts, but ONLY while it's blank — never
	// clobber a name the operator typed, and (with cust_master_name="Customer
	// Name") the docname is taken from customer_name at insert, so it must be
	// populated by save time. seller_defaults reads the parts for the bound
	// book; this just spares double-entry on a fresh Individual.
	ffl_first_name: composeCustomerName,
	middle_name: composeCustomerName,
	ffl_last_name: composeCustomerName,
	name_suffix: composeCustomerName,
});

function composeCustomerName(frm) {
	if (frm.doc.customer_type !== 'Individual') return;
	if ((frm.doc.customer_name || '').trim()) return;  // never overwrite
	// Same composer the quick-entry dialog and the server rule use
	// (public/js/customer_quick_entry.js, ffl_core.customer_name).
	const composed = window.ffl && ffl.customerQuickEntry && ffl.customerQuickEntry.composeName(frm.doc);
	if (composed) frm.set_value('customer_name', composed);
}

// Profile tab (setup/customer_profile.py): the Family section and "Merge into…".
// The rules themselves are server-side (ffl_core.customer_profile); this only
// lists, links and opens the right dialogs.
frappe.ui.form.on('Customer', {
	refresh(frm) {
		fflHideIdNumbersUnlessStored(frm);
		fflRenderFamily(frm, true);
		fflRenderAgreements(frm);
	},
});

// FFL Settings → Store Customer ID Numbers off: the ID Number column is hidden
// (grid and row form), so a number stored before the switch went off is not
// displayed and none can be typed. The server blanks it on save regardless
// (customer_profile.validate_customer); this keeps the form honest.
function fflHideIdNumbersUnlessStored(frm) {
	const field = frm.fields_dict.customer_ids;
	const grid = field && field.grid;
	if (!grid) return;
	const hide = (frappe.boot && frappe.boot.ffl_store_customer_id_numbers) ? 0 : 1;
	if (((grid.column_disp_overrides || {}).id_number || 0) === hide) return;  // already so
	// Grid-local override that rebuilds the column list (frappe 16.50); the plain
	// set_column_disp only touches the cells of rows already drawn.
	if (typeof grid.set_column_disp_in_list_view === 'function') {
		grid.set_column_disp_in_list_view('id_number', !hide);
	} else {
		grid.set_column_disp('id_number', !hide);
	}
}

function fflFamilyWrapper(frm) {
	const field = frm.fields_dict.family_members_html;
	return field && field.$wrapper;
}

// Desk routing (the router handles the click in place); never a hard-coded
// /app/... path, which redirects and reloads the whole page.
function fflCustomerLink(name, label) {
	return frappe.utils.get_form_link('Customer', name, true, frappe.utils.escape_html(label || name));
}

// Drawn from the saved record (refresh runs after every save). The same answer
// carries can_merge (the server's role list), which decides the "Merge into…"
// button; `withMerge` is only true from refresh, which is when the toolbar is rebuilt.
function fflRenderFamily(frm, withMerge) {
	const $w = fflFamilyWrapper(frm);
	if (!$w) return;
	const esc = frappe.utils.escape_html;
	if (frm.is_new()) {
		$w.html(`<p class="text-muted">${__('Save the customer to add family members.')}</p>`);
		return;
	}
	const name = frm.doc.name;
	frappe.call({
		method: 'ffl_core.customer_profile.get_family',
		args: {customer: name},
	}).then((r) => {
		if (frm.doc.name !== name) return; // the form moved to another customer meanwhile
		const fam = r.message || {};
		if (withMerge && fam.can_merge) {
			frm.add_custom_button(__('Merge into…'), () => fflMergeInto(frm), __('Actions'));
		}
		if (fam.primary) {
			const rel = fam.relationship ? ` (${esc(__(fam.relationship))})` : '';
			$w.html(`<p>${__('Family of {0}', [fflCustomerLink(fam.primary.name, fam.primary.customer_name)])}${rel}</p>`);
			return;
		}
		const members = fam.members || [];
		const rows = members.map((m) => `<tr><td>${fflCustomerLink(m.name, m.customer_name)}</td>`
			+ `<td class="text-muted">${esc(m.relationship ? __(m.relationship) : '')}</td></tr>`).join('');
		$w.html(`${members.length
			? `<table class="table table-sm" style="margin-bottom:8px"><tbody>${rows}</tbody></table>`
			: `<p class="text-muted">${__('No family members yet.')}</p>`}
			<button class="btn btn-xs btn-default" data-ffl-add-member>${__('Add family member')}</button>`);
		$w.find('[data-ffl-add-member]').on('click', () => fflAddFamilyMember(frm));
	});
}

// New Customer quick entry prefilled with this customer's last name and
// primary address, Family Of = this customer. Stays on this form and redraws
// the list when the member is saved.
function fflAddFamilyMember(frm) {
	const qe = window.ffl && ffl.customerQuickEntry;
	const last = frm.doc.customer_type !== 'Individual' ? ''
		: frm.doc.ffl_last_name || (qe ? qe.splitDisplayName(frm.doc.customer_name).ffl_last_name : '');
	const open = (addr) => {
		frappe.route_options = {customer_type: 'Individual', primary_customer: frm.doc.name};
		if (last) frappe.route_options.ffl_last_name = last;
		frappe.ui.form.make_quick_entry('Customer', () => fflRenderFamily(frm), (dialog) => {
			// Quick entry passes itself; the full-form fallback passes the doc.
			if (!addr || !dialog || !dialog.fields_dict) return;
			const map = {address_line1: 'address_line1', address_line2: 'address_line2', city: 'city',
				state: 'state', pincode: 'pincode', country: 'country_address'};
			Object.keys(map).forEach((k) => {
				if (addr[k] && dialog.fields_dict[map[k]]) dialog.set_value(map[k], addr[k]);
			});
		});
	};
	if (!frm.doc.customer_primary_address) {
		open(null);
		return;
	}
	frappe.db.get_value('Address', frm.doc.customer_primary_address,
		['address_line1', 'address_line2', 'city', 'state', 'pincode', 'country'])
		.then((r) => open(r.message || null), () => open(null));
}

function fflMergeInto(frm) {
	const esc = frappe.utils.escape_html;
	const dialog = new frappe.ui.Dialog({
		title: __('Merge {0} into…', [esc(frm.doc.customer_name || frm.doc.name)]),
		fields: [
			{
				fieldname: 'target', fieldtype: 'Link', options: 'Customer', reqd: 1,
				label: __('Customer to keep'),
				get_query: () => ({filters: {name: ['!=', frm.doc.name]}}),
			},
			{
				fieldtype: 'HTML',
				options: `<p class="text-muted">${__('Invoices, orders, 4473s, alerts, IDs and family members of {0} move to the customer you pick, then {0} is deleted. This cannot be undone.', [esc(frm.doc.name)])}</p>`,
			},
		],
		primary_action_label: __('Merge'),
		primary_action(values) {
			frappe.call({
				method: 'ffl_core.customer_profile.merge_customer',
				args: {source: frm.doc.name, target: values.target},
				freeze: true,
				freeze_message: __('Merging…'),
			}).then((r) => {
				dialog.hide();
				const kept = r.message || {};
				frappe.show_alert({message: __('Merged into {0}', [esc(kept.customer_name || kept.name)]), indicator: 'green'});
				frappe.set_route('Form', 'Customer', kept.name);
			});
		},
	});
	dialog.show();
}

// Agreements section (setup/agreements.py, rules in ffl_core/agreements.py): the
// customer's copies, newest first, and Request agreement (agreement_actions.js).
function fflRenderAgreements(frm) {
	const field = frm.fields_dict.customer_agreements_html;
	const $w = field && field.$wrapper;
	if (!$w) return;
	if (frm.is_new()) {
		$w.html(`<p class="text-muted">${__('Save the customer to request agreements.')}</p>`);
		return;
	}
	const esc = frappe.utils.escape_html;
	const name = frm.doc.name;
	const tones = {Signed: 'green', Pending: 'yellow', Expired: 'orange', Void: 'gray'};
	frappe.call({method: 'ffl_core.agreements.get_customer_agreements', args: {customer: name}}).then((r) => {
		if (frm.doc.name !== name) return;
		const rows = (r.message || {}).rows || [];
		const body = rows.map((a) => `<tr>
			<td>${frappe.utils.get_form_link('Customer Agreement', a.name, true, esc(a.template))}${a.serial_no ? `<div class="small text-muted">${esc(__('serial {0}', [a.serial_no]))}</div>` : ''}</td>
			<td><span class="indicator-pill ${tones[a.status] || 'gray'}">${esc(__(a.status))}</span></td>
			<td class="text-muted">${a.signed_on ? esc(frappe.datetime.str_to_user(a.signed_on)) : ''}${a.signed_via ? ` · ${esc(__(a.signed_via))}` : ''}</td>
			<td class="text-muted">${a.expires_on ? esc(__('Expires {0}', [frappe.datetime.str_to_user(a.expires_on)])) : ''}</td></tr>`).join('');
		$w.html(`${rows.length
			? `<table class="table table-sm" style="margin-bottom:8px"><tbody>${body}</tbody></table>`
			: `<p class="text-muted">${__('No agreements yet.')}</p>`}
			<button class="btn btn-xs btn-default" data-ffl-request-agreement>${__('Request agreement')}</button>`);
		$w.find('[data-ffl-request-agreement]').on('click', () => {
			ffl.agreements.request(name, null, {customer_name: frm.doc.customer_name, done: () => fflRenderAgreements(frm)});
		});
	});
}
