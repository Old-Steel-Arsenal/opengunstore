// Gunsmith Work Order form: the buttons that move a job along (rules: ffl_core/gunsmith.py).
frappe.ui.form.on('Gunsmith Work Order', {
	setup(frm) {
		// Charges are things sold over the counter: no templates, nothing switched off.
		frm.set_df_property('state', 'options', [''].concat(frappe.boot.ffl_us_states || []));
		frm.set_query('item_code', 'lines', () => ({ filters: { disabled: 0, has_variants: 0, is_sales_item: 1, is_firearm: 0 } }));
	},
	scan_barcode(frm) {
		// ERPNext's scanner: a new row per item, a repeat scan adds one to its qty.
		new erpnext.utils.BarcodeScanner({ frm, items_table_name: 'lines' }).process_scan();
	},
	refresh(frm) {
		// frm._labor: the store's labor item, from the server each time the form loads.
		frm._estimate = flt(frm.doc.estimated_cost);
		bindScanId(frm);
		if (frm.is_new()) {
			// The labor charge is there from the start (the server adds it too); parts go under it.
			addressRequired(frm);
			frappe.call({ method: M + 'labor_item' }).then((r) => {
				frm._labor = r.message;
				if (frm._labor && !(frm.doc.lines || []).length) {
					frm.add_child('lines', { item_code: frm._labor, qty: 1, rate: flt(frm.doc.estimated_cost) });
					frm.refresh_field('lines');
				}
			});
			return;
		}
		// A closed job is kept as it is (the server refuses changes too).
		if (['Picked Up', 'Cancelled'].includes(frm.doc.status)) frm.disable_form();
		addressRequired(frm);
		frappe.call({ method: 'ffl_core.gunsmith.get_state', args: { name: frm.doc.name } })
			.then((r) => {
				frm._labor = (r.message || {}).labor_item;
				draw(frm, r.message || {});
			});
	},
	estimated_cost(frm) {
		// The labor charge follows the estimate until someone prices it by hand (as the server does).
		const was = frm._estimate || 0;
		frm._estimate = flt(frm.doc.estimated_cost);
		(frm.doc.lines || []).filter((r) => frm._labor && r.item_code === frm._labor && flt(r.rate) === was)
			.forEach((r) => frappe.model.set_value(r.doctype, r.name, 'rate', frm._estimate));
	},
	log_in_bound_book(frm) {
		addressRequired(frm);
		fillScannedId(frm);
	},
	customer(frm) {
		if (!frm.doc.customer) return;
		// The paper form's customer block, refilled now for the new customer, as Receive Goods
		// does (the server fills blanks too); a state the dropdown lacks is left for staff to pick.
		// Cleared first, so a failed lookup never leaves the last customer's address on the job.
		frm.set_value({ phone: '', email: '', address_line1: '', city: '', state: '', zip: '' });
		frm._contact = frappe.call({ method: 'ffl_core.api.trade_in.seller_defaults', args: { customer: frm.doc.customer } })
			.then((r) => {
				const d = r.message || {};
				frm.set_value({
					phone: d.phone || '', email: d.email || '', address_line1: d.source_address_line1 || '',
					city: d.source_city || '', zip: d.source_zip || '',
					state: (frappe.boot.ffl_us_states || []).includes(d.source_state) ? d.source_state : '',
				});
			});
	},
});

frappe.ui.form.on('Gunsmith Work Order Line', {
	qty: lineAmount,
	rate: lineAmount,
	item_code(frm, cdt, cdn) {
		// A part comes with its name and the customer's list price; both stay editable. The labor
		// line keeps no name: on the bill it reads the job's Work Performed.
		const row = locals[cdt][cdn];
		if (!row.item_code || row.item_code === frm._labor) return;
		frappe.call({ method: M + 'charge_details', args: { item_code: row.item_code, customer: frm.doc.customer } })
			.then((r) => {
				const d = r.message || {};
				if (d.item_name && !row.description) frappe.model.set_value(cdt, cdn, 'description', d.item_name);
				if (flt(d.rate) && !flt(row.rate)) frappe.model.set_value(cdt, cdn, 'rate', flt(d.rate));
			});
	},
	lines_add(frm, cdt, cdn) {
		// The scanner adds 1 to the row's qty, so its new row starts from 0, not the default 1.
		if (frappe.flags.trigger_from_barcode_scanner) {
			locals[cdt][cdn].qty = 0;
			return;
		}
		// A first row (the labor charge removed and re-added) gets the labor item.
		if ((frm.doc.lines || []).length === 1) {
			if (frm._labor && !locals[cdt][cdn].item_code) frappe.model.set_value(cdt, cdn, 'item_code', frm._labor);
		}
	},
});

// ---- Scan ID: the licence's barcode picks or creates the customer and fills the block.

// A keyboard-wedge scanner types the whole barcode in 1-3 s; this long with no keystroke is the
// end of it (as the New Customer dialog's Scan ID).
const SCAN_QUIET_MS = 1200;

function bindScanId(frm) {
	const field = frm.get_field('scan_id');
	if (!field || !field.$input || field.$input.data('fflScan')) return;
	field.$input.data('fflScan', 1);
	let timer;
	field.$input.on('input', () => {
		clearTimeout(timer);
		timer = setTimeout(() => takeScan(frm, field.$input.val()), SCAN_QUIET_MS);
	});
}

function takeScan(frm, raw) {
	frm.set_value('scan_id', '');
	const p = window.ffl && ffl.aamva && ffl.aamva.parse(raw, { ids: true });
	if (!p || !p.first || !p.last) {
		frappe.show_alert({ indicator: 'red', message: __("That didn't read as a licence. Scan it again.") });
		return;
	}
	frm._scanId = p.idNumber || null;
	const idsStored = !!frappe.boot.ffl_store_customer_id_numbers;
	frappe.call({ method: 'ffl_core.customer_flags.find_customers_by_scan',
		args: { first: p.first, last: p.last, id_number: idsStored ? p.idNumber : '' } })
		.then((r) => pickCustomer(r.message || []))
		.then((choice) => {
			if (!choice) return;
			if (choice.new) newCustomer(frm, raw);
			else useCustomer(frm, choice.name, p);
		});
}

// A number match is the person; a name match is only a guess, so the clerk picks (or says new).
function pickCustomer(rows) {
	if (rows.length === 1 && rows[0].by_id) return Promise.resolve({ name: rows[0].name });
	if (!rows.length) return Promise.resolve({ new: true });
	return new Promise((resolve) => {
		let picked = null;
		const esc = frappe.utils.escape_html;
		const d = new frappe.ui.Dialog({
			title: __('Who is this?'),
			fields: [{ fieldtype: 'Select', fieldname: 'customer', label: __('Customer'), reqd: 1,
				options: rows.map((x) => ({ value: x.name,
					label: esc(x.customer_name) + ' — ' + (x.address ? esc(x.address) : __('no address')) }))
					.concat([{ value: '__new', label: __('Someone new: create the customer') }]) }],
			primary_action_label: __('Use'),
			primary_action: (v) => { picked = v.customer === '__new' ? { new: true } : { name: v.customer }; d.hide(); },
		});
		d.onhide = () => resolve(picked);
		d.show();
	});
}

// The New Customer dialog, filled from the same scan (its own Scan ID reads it).
function newCustomer(frm, raw) {
	frappe.ui.form.make_quick_entry('Customer', (doc) => useCustomer(frm, doc.name, null), (qe) => {
		if (qe && qe.__fflApplyScan) qe.__fflApplyScan(raw);
	}, { doctype: 'Customer', customer_type: 'Individual' });
}

// Pick the customer, let their contact load, then settle the address against the licence
// (a new customer's is the licence's already) and fill the ID.
async function useCustomer(frm, customer, p) {
	frm._contact = null;
	await frm.set_value('customer', customer);
	await frm._contact;
	if (p) await settleAddress(frm, p);
	if (!frm.doc.id_type) frm.set_value('id_type', 'Driver License');
	fillScannedId(frm);
}

function settleAddress(frm, p) {
	const onId = { address_line1: p.address1 || '', city: p.city || '', state: p.state || '', zip: p.zip || '' };
	if (!onId.address_line1) return Promise.resolve();
	const key = (a) => [a.address_line1, (a.zip || '').slice(0, 5)].join('|').toLowerCase().replace(/[^a-z0-9|]/g, '');
	if (!frm.doc.address_line1) return frm.set_value(onId);
	if (key(onId) === key(frm.doc)) return Promise.resolve();
	const line = (a) => frappe.utils.escape_html([a.address_line1, a.city, a.state, a.zip].filter(Boolean).join(', '));
	return new Promise((resolve) => {
		const d = new frappe.ui.Dialog({
			title: __('Which address for this job?'),
			fields: [{ fieldtype: 'HTML', options:
				'<p><b>' + __('On the ID') + '</b><br>' + line(onId) + '</p>'
				+ '<p><b>' + __('On file') + '</b><br>' + line(frm.doc) + '</p>' }],
			primary_action_label: __("Use the ID's"),
			primary_action: () => { frm.set_value(onId); d.hide(); },
			secondary_action_label: __("Keep the one on file"),
			secondary_action: () => d.hide(),
		});
		d.onhide = () => resolve();
		d.show();
	});
}

// The licence number goes in only where the job shows it (the store keeps ID numbers, or the
// gun is being logged); otherwise it waits in memory for Log in Bound Book.
function fillScannedId(frm) {
	if (!frm._scanId || frm.doc.id_number) return;
	if (frappe.boot.ffl_store_customer_id_numbers || frm.doc.log_in_bound_book) frm.set_value('id_number', frm._scanId);
}

// Logging the gun needs the customer's address; the server names what's missing too.
function addressRequired(frm) {
	const need = !!(frm.doc.log_in_bound_book && !frm.doc.purchase_receipt);
	['address_line1', 'city', 'state', 'zip'].forEach((f) => frm.toggle_reqd(f, need));
}

function lineAmount(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, 'amount', flt(row.qty || 1) * flt(row.rate));
}

const M = 'ffl_core.gunsmith.';

// The server acts on the saved job (charges, deposit), so unsaved edits are saved first.
function act(frm, method, args, after) {
	const run = () => frappe.call({ method: M + method, args: Object.assign({ name: frm.doc.name }, args || {}), freeze: true })
		.then((r) => (after ? after(r.message || {}) : frm.reload_doc()));
	return frm.is_dirty() ? frm.save().then(run) : run();
}

// Payments and pick-up are rung at the register, whose checkout takes cards (Recent
// Orders › Work Orders: Take payment, Complete); Pick up opens this job's Complete there
// (confirm the charges, then the cart) from these route options.
function toRegister(frm, action) {
	const go = () => {
		frappe.route_options = { ffl_work_order: frm.doc.name, ffl_work_order_action: action || 'complete' };
		frappe.set_route('point-of-sale');
	};
	return frm.is_dirty() ? frm.save().then(go) : go();
}

function draw(frm, st) {
	const d = frm.doc;
	const open = !['Picked Up', 'Cancelled'].includes(d.status);
	const cur = (v) => format_currency(v, frappe.defaults.get_default('currency'));
	const auth = st.authorization;

	// What stands: authorization, deposit, open register drafts.
	const lines = [];
	if (auth) {
		const label = { signed: __('signed'), expiring: __('signed'), pending: __('sent, not signed yet'),
			missing: __('not signed'), expired: __('not signed') }[auth.state] || auth.state;
		lines.push(__('Intake authorization: {0}', [label]));
	}
	if (st.deposit_held) lines.push(__('Deposit held: {0}', [cur(st.deposit_held)]));
	if (st.drafts && st.drafts.deposit) lines.push(__('A payment for this job is parked at the register ({0}).', [st.drafts.deposit]));
	if (st.drafts && st.drafts.pickup) lines.push(__('Pick-up invoice {0} is open at the register.', [st.drafts.pickup]));
	if (st.drafts && st.drafts.refund) lines.push(__('Deposit refund {0} is open at the register.', [st.drafts.refund]));
	if (lines.length) frm.dashboard.set_headline(lines.map(frappe.utils.escape_html).join('<br>'));

	if (open && auth && !['signed', 'expiring'].includes(auth.state)) {
		frm.add_custom_button(__('Get authorization signed'), () => {
			ffl.agreements.request(d.customer, auth.template, { customer_name: d.customer_name,
				reference_doctype: d.doctype, reference_name: d.name, done: () => frm.reload_doc() });
		}).addClass('btn-primary');
	}
	if (open && !d.purchase_receipt) {
		frm.add_custom_button(__('Log in bound book'), () => logDialog(frm), __('Bound book'));
	}
	if (d.status === 'Received' || d.status === 'Ready') {
		frm.add_custom_button(d.status === 'Ready' ? __('Reopen work') : __('Start work'),
			() => act(frm, 'start_work'), __('Status'));
	}
	if (d.status === 'Received' || d.status === 'In Progress') {
		frm.add_custom_button(__('Mark ready'), () => act(frm, 'mark_ready'), __('Status'));
	}
	if (d.status === 'Ready') {
		frm.add_custom_button(__('Notify customer'), () => notifyDialog(frm), __('Status'));
		frm.add_custom_button(st.drafts && st.drafts.pickup ? __('Open pick-up at the register') : __('Pick up at the register'),
			() => toRegister(frm)).addClass('btn-primary');
	}
	if (open) {
		frm.add_custom_button(__('Cancel work order'), () => {
			frappe.prompt({ fieldtype: 'Small Text', fieldname: 'reason', label: __('Why?'), reqd: 1 },
				(v) => act(frm, 'cancel_work_order', { reason: v.reason }), __('Cancel work order'), __('Cancel it'));
		}, __('Status'));
	}
	if (!open && flt(st.deposit_held) > 0) {
		// At the register, so a card payment is refunded to the card.
		frm.add_custom_button(st.drafts && st.drafts.refund ? __('Open refund at the register') : __('Refund deposit at the register'),
			() => toRegister(frm, 'refund'), __('Money'));
	}
}

function logDialog(frm) {
	const d = frm.doc;
	frappe.prompt([
		{ fieldtype: 'HTML', options: '<p>' + __('The gun is received from {0} as a Gunsmithing acquisition and held in customer custody at zero cost (never for sale). At pick-up a Gunsmithing disposition returns it to them.', [frappe.utils.escape_html(d.customer_name || d.customer)]) + '</p>' },
		{ fieldtype: 'Select', fieldname: 'id_type', label: __('ID Type'), reqd: 1, default: d.id_type,
			options: ['', 'Driver License', 'State ID', 'Passport', 'Military ID', 'Other'] },
		{ fieldtype: 'Data', fieldname: 'id_number', label: __('ID Number'), reqd: 1, default: d.id_number },
	], (v) => act(frm, 'log_in_bound_book', v), __('Log in bound book'), __('Log it'));
}

function notifyDialog(frm) {
	frappe.prompt({ fieldtype: 'Select', fieldname: 'via', label: __('How'), reqd: 1,
		options: ['Email', 'Phone', 'Text'], default: frm.doc.email ? 'Email' : 'Phone',
		description: __('Email sends the "ready for pick-up" email now. Phone and Text: call or text the customer, then record it here.') },
	(v) => act(frm, 'notify_customer', { via: v.via }, (m) => {
		frappe.show_alert({ indicator: 'green', message: m.email ? __('Emailed {0}', [m.email]) : __('Recorded') });
		frm.reload_doc();
	}), __('Notify customer'), __('Done'));
}
