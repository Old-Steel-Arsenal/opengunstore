// Give card money back on a counter Sales Invoice (ffl_integrations/payments/si_reversal.py):
//   "Card refund" button group for managers on a submitted invoice with a card charge left —
//   Void (whole charge, before it settles) / Refund (any part: Return or Collect again), and
//   "Resolve card reversal" on a charge whose void/refund the processor never confirmed.
// Every rule is server-side; this only lists the charges and asks.
(function () {
	const M = 'ffl_integrations.payments.si_reversal.';
	const MANAGER_ROLES = ['Sales Manager', 'System Manager'];   // api._check_refund_perm
	const GROUP = __('Card refund');
	const esc = (s) => frappe.utils.escape_html(s == null ? '' : String(s));
	const label = (c) => `${c.card_summary || __('Card')} · ${format_currency(c.amount)}`
		+ (c.refunded_amount ? ` · ${__('{0} left', [format_currency(c.left)])}` : '') + ` · ${c.unique_reference}`;

	function refresh(frm) {
		const doc = frm.doc;
		if (doc.docstatus !== 1 || doc.is_return || !frappe.user.has_role(MANAGER_ROLES)) return;
		const name = doc.name;
		frappe.call({ method: M + 'card_charges', args: { sales_invoice: name } }).then((r) => {
			const out = (r && r.message) || { charges: [] };
			if (frm.doc.name !== name || !out.charges.length) return;
			const locked = out.charges.filter((c) => c.locked);
			const open = out.charges.filter((c) => !c.locked);
			if (locked.length) frm.add_custom_button(__('Resolve card reversal'), () => resolveDialog(frm, locked), GROUP);
			if (open.some((c) => c.can_void)) frm.add_custom_button(__('Void'), () => voidDialog(frm, open.filter((c) => c.can_void)), GROUP);
			if (open.length) frm.add_custom_button(__('Refund'), () => refundDialog(frm, open, out), GROUP);
		}, () => { /* no buttons */ });
	}

	function pickField(charges) {
		return { fieldtype: 'Select', fieldname: 'charge', label: __('Card charge'), reqd: 1,
			options: charges.map((c) => ({ value: c.name, label: label(c) })), default: charges[0].name };
	}

	function run(frm, method, args, message) {
		frappe.call({ method: M + method, args, freeze: true, freeze_message: message,
			callback: (r) => {
				const m = (r && r.message) || {};
				if (m.ok) {
					frappe.show_alert({ indicator: 'green', message: m.booked === false
						? __('Unlocked — nothing was booked.')
						: __('Done: {0} back to the card. {1} now owes {2}.', [format_currency(m.amount),
							frm.doc.name, format_currency(m.outstanding)]) }, 8);
				}
				frm.reload_doc();
			},
			error: () => frm.reload_doc() });
	}

	function voidDialog(frm, charges) {
		const d = new frappe.ui.Dialog({
			title: __('Void card charge — {0}', [frm.doc.name]),
			fields: [
				{ fieldtype: 'HTML', options: `<p class="text-muted">${esc(__('Gives back the whole charge before the processor settles it; the invoice owes the amount again.'))}</p>` },
				pickField(charges),
				{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Reason'), reqd: 1 },
			],
			primary_action_label: __('Void'),
			primary_action: (v) => {
				d.hide();
				run(frm, 'void_sales_invoice_charge', { terminal_payment: v.charge, reason: v.reason }, __('Voiding the charge…'));
			},
		});
		d.show();
	}

	function refundDialog(frm, charges, out) {
		const left = (n) => (charges.find((c) => c.name === n) || charges[0]).left;
		const types = [{ value: 'Return', label: __('Return / price lowered — credit note takes it off the sale') },
			{ value: 'Collect again', label: __('Collect again — the sale stands, the invoice owes it again') }];
		const note = out.membership
			? __('Membership invoice: end the membership first (Cancel → End it today), then refund as a Return.')
			: (out.return_blocked ? __('A Return is not possible here because {0} — use Cancel Order or a sales return.', [out.return_blocked]) : '');
		const d = new frappe.ui.Dialog({
			title: __('Refund card charge — {0}', [frm.doc.name]),
			fields: [
				pickField(charges),
				{ fieldtype: 'Currency', fieldname: 'amount', label: __('Amount'), reqd: 1, default: charges[0].left,
					description: __('At most what is left on the charge. A Return refunds the credit note total (a cent of rounding).') },
				{ fieldtype: 'Select', fieldname: 'refund_type', label: __('Type'), reqd: 1, options: types,
					default: out.return_blocked && !out.membership ? 'Collect again' : 'Return' },
				...(note ? [{ fieldtype: 'HTML', options: `<p class="text-muted">${esc(note)}</p>` }] : []),
				{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Reason'), reqd: 1 },
			],
			primary_action_label: __('Refund'),
			primary_action: (v) => {
				d.hide();
				run(frm, 'refund_sales_invoice_charge', { terminal_payment: v.charge, amount: v.amount,
					refund_type: v.refund_type, reason: v.reason }, __('Refunding to the card…'));
			},
		});
		d.fields_dict.charge.df.onchange = () => d.set_value('amount', left(d.get_value('charge')));
		d.show();
	}

	function resolveDialog(frm, charges) {
		const what = (c) => c.lock ? `${c.lock.action} ${format_currency(c.lock.amount)}` : '';
		const d = new frappe.ui.Dialog({
			title: __('Resolve card reversal — {0}', [frm.doc.name]),
			fields: [
				{ fieldtype: 'HTML', options: `<p>${esc(__('The card processor never confirmed this void/refund. Look it up in its portal first: book it only if the portal shows it happened.'))}</p>` },
				{ ...pickField(charges), options: charges.map((c) => ({ value: c.name, label: `${label(c)} — ${what(c)}` })) },
				{ fieldtype: 'Select', fieldname: 'went_through', label: __('The portal shows'), reqd: 1,
					options: [{ value: '1', label: __('It happened — book it') }, { value: '0', label: __('It did not happen — unlock') }] },
				{ fieldtype: 'Data', fieldname: 'portal_reference', label: __('Portal reference'), depends_on: 'eval:doc.went_through=="1"',
					mandatory_depends_on: 'eval:doc.went_through=="1"' },
				{ fieldtype: 'Small Text', fieldname: 'note', label: __('What you checked'), reqd: 1 },
			],
			primary_action_label: __('Resolve'),
			primary_action: (v) => {
				d.hide();
				run(frm, 'resolve_card_reversal', { terminal_payment: v.charge, went_through: v.went_through,
					portal_reference: v.portal_reference, note: v.note }, __('Resolving…'));
			},
		});
		d.show();
	}

	frappe.ui.form.on('Sales Invoice', { refresh });
})();
