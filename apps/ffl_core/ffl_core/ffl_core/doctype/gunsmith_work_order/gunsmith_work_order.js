// Gunsmith Work Order form: the buttons that move a job along (rules: ffl_core/gunsmith.py).
frappe.ui.form.on('Gunsmith Work Order', {
	setup(frm) {
		// Charges are things sold over the counter: no templates, nothing switched off.
		frm.set_query('item_code', 'lines', () => ({ filters: { disabled: 0, has_variants: 0, is_sales_item: 1 } }));
	},
	scan_barcode(frm) {
		// ERPNext's scanner: a new row per item, a repeat scan adds one to its qty.
		new erpnext.utils.BarcodeScanner({ frm, items_table_name: 'lines' }).process_scan();
	},
	refresh(frm) {
		frm._estimate = flt(frm.doc.estimated_cost);
		if (frm.is_new()) {
			// The labor charge is there from the start (the server adds it too); parts go under it.
			if (!(frm.doc.lines || []).length) {
				frappe.db.get_single_value('FFL Settings', 'gunsmith_labor_item').then((item) => {
					if (item && !(frm.doc.lines || []).length) {
						frm.add_child('lines', { item_code: item, qty: 1, rate: flt(frm.doc.estimated_cost) });
						frm.refresh_field('lines');
					}
				});
			}
			return;
		}
		// A closed job is kept as it is (the server refuses changes too).
		if (['Picked Up', 'Cancelled'].includes(frm.doc.status)) frm.disable_form();
		frappe.call({ method: 'ffl_core.gunsmith.get_state', args: { name: frm.doc.name } })
			.then((r) => draw(frm, r.message || {}));
	},
	estimated_cost(frm) {
		// The labor charge follows the estimate until someone prices it by hand (as the server does).
		const was = frm._estimate || 0;
		frm._estimate = flt(frm.doc.estimated_cost);
		frappe.db.get_single_value('FFL Settings', 'gunsmith_labor_item').then((item) => {
			(frm.doc.lines || []).filter((r) => r.item_code === item && flt(r.rate) === was)
				.forEach((r) => frappe.model.set_value(r.doctype, r.name, 'rate', frm._estimate));
		});
	},
	customer(frm) {
		if (!frm.doc.customer) return;
		// The paper form's customer block, refilled for the new customer (the server fills blanks too).
		frm.set_value({ phone: '', email: '', address_line1: '', city: '', state: '', zip: '' });
	},
});

frappe.ui.form.on('Gunsmith Work Order Line', {
	qty: lineAmount,
	rate: lineAmount,
	item_code(frm, cdt, cdn) {
		// A part comes with its name and the store's selling price; both stay editable.
		const row = locals[cdt][cdn];
		if (!row.item_code) return;
		frappe.db.get_value('Item', row.item_code, 'item_name').then((r) => {
			const name = r.message && r.message.item_name;
			if (name && !row.description) frappe.model.set_value(cdt, cdn, 'description', name);
		});
		const price_list = frappe.defaults.get_default('selling_price_list');
		if (!price_list || flt(row.rate)) return;
		frappe.db.get_value('Item Price', { item_code: row.item_code, price_list, selling: 1 }, 'price_list_rate')
			.then((r) => {
				const rate = flt(r.message && r.message.price_list_rate);
				if (rate && !flt(row.rate)) frappe.model.set_value(cdt, cdn, 'rate', rate);
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
			frappe.db.get_single_value('FFL Settings', 'gunsmith_labor_item').then((item) => {
				if (item && !locals[cdt][cdn].item_code) frappe.model.set_value(cdt, cdn, 'item_code', item);
			});
		}
	},
});

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
		{ fieldtype: 'Select', fieldname: 'id_type', label: __('Customer ID Type'), reqd: 1, default: d.id_type,
			options: ['', 'Driver License', 'State ID', 'Passport', 'Military ID', 'Other'] },
		{ fieldtype: 'Data', fieldname: 'id_number', label: __('Customer ID Number'), reqd: 1, default: d.id_number },
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
