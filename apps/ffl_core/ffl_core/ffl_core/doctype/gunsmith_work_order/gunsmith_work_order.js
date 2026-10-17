// Gunsmith Work Order form: the buttons that move a job along (rules: ffl_core/gunsmith.py).
frappe.ui.form.on('Gunsmith Work Order', {
	refresh(frm) {
		if (frm.is_new()) return;
		// A closed job is kept as it is (the server refuses changes too).
		if (['Picked Up', 'Cancelled'].includes(frm.doc.status)) frm.disable_form();
		frappe.call({ method: 'ffl_core.gunsmith.get_state', args: { name: frm.doc.name } })
			.then((r) => draw(frm, r.message || {}));
	},
	customer(frm) {
		if (!frm.doc.customer) return;
		// The paper form's customer block, refilled for the new customer (the server fills blanks too).
		frm.set_value({ phone: '', email: '', address: '' });
	},
});

frappe.ui.form.on('Gunsmith Work Order Line', {
	qty: lineAmount,
	rate: lineAmount,
	lines_add(frm, cdt, cdn) {
		// The first charge is usually the labor: offer the store's labor item.
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

function openInvoice(name) {
	frappe.set_route('Form', 'POS Invoice', name);
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
	if (st.drafts && st.drafts.deposit) lines.push(__('Deposit invoice {0} is open at the register.', [st.drafts.deposit]));
	if (st.drafts && st.drafts.pickup) lines.push(__('Pick-up invoice {0} is open at the register.', [st.drafts.pickup]));
	if (st.drafts && st.drafts.refund) lines.push(__('Deposit refund {0} is open at the register.', [st.drafts.refund]));
	if (lines.length) frm.dashboard.set_headline(lines.map(frappe.utils.escape_html).join('<br>'));

	if (open && auth && !['signed', 'expiring'].includes(auth.state)) {
		frm.add_custom_button(__('Get authorization signed'), () => {
			ffl.agreements.request(d.customer, auth.template, { customer_name: d.customer_name,
				reference_doctype: d.doctype, reference_name: d.name, done: () => frm.reload_doc() });
		}).addClass('btn-primary');
	}
	if (open && flt(d.deposit_amount) > 0 && !st.deposit_collected) {
		frm.add_custom_button(st.drafts && st.drafts.deposit ? __('Open deposit invoice') : __('Take deposit'),
			() => act(frm, 'take_deposit', {}, (m) => openInvoice(m.pos_invoice)), __('Money'));
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
		frm.add_custom_button(st.drafts && st.drafts.pickup ? __('Open pick-up invoice') : __('Pick up'), () => {
			act(frm, 'open_pickup', {}, (m) => (m.pos_invoice ? openInvoice(m.pos_invoice) : frm.reload_doc()));
		}).addClass('btn-primary');
	}
	if (open) {
		frm.add_custom_button(__('Cancel work order'), () => {
			frappe.prompt({ fieldtype: 'Small Text', fieldname: 'reason', label: __('Why?'), reqd: 1 },
				(v) => act(frm, 'cancel_work_order', { reason: v.reason }), __('Cancel work order'), __('Cancel it'));
		}, __('Status'));
	}
	if (!open && flt(st.deposit_held) > 0) {
		frm.add_custom_button(st.drafts && st.drafts.refund ? __('Open deposit refund') : __('Refund deposit'),
			() => act(frm, 'refund_deposit', {}, (m) => openInvoice(m.pos_invoice)), __('Money'));
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
