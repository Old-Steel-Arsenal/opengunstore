// Card on file (ffl_integrations/payments/saved_cards.py) on the Sales Invoice form:
//   "Charge saved card" on a submitted invoice that still owes, for managers:
//   pick the card, amount defaults to the balance; the server books the payment.
// The token never reaches the browser; every rule is server-side, this only lists and asks.
(function () {
	const M = 'ffl_integrations.payments.saved_cards.';
	const MANAGER_ROLES = ['Sales Manager', 'System Manager'];   // saved_cards.MANAGER_ROLES
	const esc = (s) => frappe.utils.escape_html(s == null ? '' : String(s));
	const expiry = (mmyy) => (mmyy && mmyy.length === 4 ? `${mmyy.slice(0, 2)}/${mmyy.slice(2)}` : '');
	const label = (c) => [c.card_summary || c.name, expiry(c.card_expiry), c.is_default ? __('default') : '']
		.filter(Boolean).join(' · ');

	function listCards(customer) {
		return frappe.call({ method: M + 'list_saved_cards', args: { customer } })
			.then((r) => (r && r.message) || { cards: [], can_manage: false });
	}

	// ---- Sales Invoice: Charge saved card.
	function chargeButton(frm) {
		const doc = frm.doc;
		if (doc.docstatus !== 1 || flt(doc.outstanding_amount) <= 0.005 || doc.is_return
				|| !frappe.user.has_role(MANAGER_ROLES)) return;
		const name = doc.name;
		listCards(doc.customer).then((out) => {
			if (frm.doc.name !== name || !out.cards.length) return;
			frm.add_custom_button(__('Charge saved card'), () => chargeDialog(frm, out.cards));
		}, () => { /* no button */ });
	}

	function chargeDialog(frm, cards) {
		const owed = flt(frm.doc.outstanding_amount);
		const options = cards.map((c) => ({ value: c.name, label: label(c) }));
		const d = new frappe.ui.Dialog({
			title: __('Charge saved card — {0}', [frm.doc.name]),
			fields: [
				{ fieldtype: 'Select', fieldname: 'saved_card', label: __('Card'), reqd: 1, options,
					default: (cards.find((c) => c.is_default) || cards[0]).name },
				{ fieldtype: 'Currency', fieldname: 'amount', label: __('Amount'), reqd: 1, default: owed,
					description: __('Outstanding: {0}', [format_currency(owed)]) },
			],
			primary_action_label: __('Charge'),
			primary_action: (v) => {
				d.hide();
				frappe.call({
					method: M + 'charge_saved_card',
					args: { sales_invoice: frm.doc.name, saved_card: v.saved_card, amount: v.amount },
					freeze: true, freeze_message: __('Charging the saved card…'),
					callback: (r) => {
						const m = (r && r.message) || {};
						if (m.ok) {
							frappe.show_alert({ indicator: 'green', message: m.paid
								? __('Approved · auth {0} — {1} fully paid.', [m.approval_code || m.unique_reference || 'OK', frm.doc.name])
								: __('Approved · auth {0} — {1} still outstanding.', [m.approval_code || m.unique_reference || 'OK', format_currency(m.outstanding)]) }, 7);
						} else if (m.status === 'UNCONFIRMED') {
							releaseDialog(frm, m);
						} else {
							frappe.msgprint({ title: __('Card not charged'), indicator: m.resolved ? 'orange' : 'red',
								message: esc(m.error || __('Card declined.')) });
						}
						frm.reload_doc();
					},
				});
			},
		});
		d.show();
	}

	// An earlier saved-card charge the gateway still has no record of: release it as not
	// charged only after looking in the processor's portal (the server re-checks first).
	function releaseDialog(frm, m) {
		const d = new frappe.ui.Dialog({
			title: __('Charge not confirmed'),
			fields: [
				{ fieldtype: 'HTML', options: `<p>${esc(m.error)}</p>` },
				{ fieldtype: 'Small Text', fieldname: 'reason', label: __('What you checked in the portal'), reqd: 1 },
			],
			primary_action_label: __('Release as not charged'),
			primary_action: (v) => {
				d.hide();
				frappe.call({
					method: M + 'release_saved_card_charge',
					args: { terminal_payment: m.terminal_payment, reason: v.reason },
					freeze: true,
					callback: (r) => {
						const out = (r && r.message) || {};
						frappe.show_alert({ indicator: out.ok ? 'green' : 'orange',
							message: esc(out.ok ? __('The charge was found and recorded.') : out.error) }, 7);
						frm.reload_doc();
					},
				});
			},
		});
		d.show();
	}

	frappe.ui.form.on('Sales Invoice', { refresh: chargeButton });
})();
