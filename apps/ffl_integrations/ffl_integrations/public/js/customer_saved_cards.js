// Card on file (ffl_integrations/payments/saved_cards.py) on the Customer form:
//   the customer's saved cards (card, expiry, default) under Agreements, with
//   Remove for managers (the card processor forgets the card first).
//   (Sales Invoice → Charge saved card: sales_invoice_saved_cards.js.)
// The token never reaches the browser; every rule is server-side, this only lists and asks.
(function () {
	const M = 'ffl_integrations.payments.saved_cards.';
	const READ_ROLES = ['Sales User', 'Sales Manager', 'Accounts Manager', 'System Manager'];
	const esc = (s) => frappe.utils.escape_html(s == null ? '' : String(s));
	const expiry = (mmyy) => (mmyy && mmyy.length === 4 ? `${mmyy.slice(0, 2)}/${mmyy.slice(2)}` : '');

	function listCards(customer) {
		return frappe.call({ method: M + 'list_saved_cards', args: { customer } })
			.then((r) => (r && r.message) || { cards: [], can_manage: false });
	}

	// ---- Customer: a block after the Agreements section (its own node, so the Agreements
	// renderer replacing its HTML never wipes it).
	function renderCustomerCards(frm) {
		const field = frm.fields_dict.customer_agreements_html;
		const $anchor = field && field.$wrapper;
		if (!$anchor) return;
		$anchor.siblings('.ffl-saved-cards').remove();
		if (frm.is_new() || !frappe.user.has_role(READ_ROLES)) return;
		const name = frm.doc.name;
		listCards(name).then((out) => {
			if (frm.doc.name !== name || !out.cards.length) return;
			const rows = out.cards.map((c) => `<tr>
				<td>${esc(c.card_summary || c.name)}${c.is_default ? ` <span class="indicator-pill green">${esc(__('Default'))}</span>` : ''}</td>
				<td class="text-muted">${esc(expiry(c.card_expiry))}</td>
				<td class="text-muted">${esc(__('Saved {0}', [frappe.datetime.str_to_user(c.saved_on)]))}</td>
				<td class="text-right">${out.can_manage ? `<button class="btn btn-xs btn-default" data-card="${esc(c.name)}">${esc(__('Remove'))}</button>` : ''}</td></tr>`).join('');
			const $block = $(`<div class="ffl-saved-cards" style="margin-top:12px">
				<div class="text-muted small" style="margin-bottom:4px">${esc(__('Saved cards'))}</div>
				<table class="table table-sm"><tbody>${rows}</tbody></table></div>`);
			$anchor.siblings('.ffl-saved-cards').remove();
			$anchor.after($block);
			$block.find('[data-card]').on('click', (e) => removeCard(frm, $(e.currentTarget).attr('data-card')));
		}, () => { /* nothing to show */ });
	}

	function removeCard(frm, card) {
		const d = new frappe.ui.Dialog({
			title: __('Remove saved card'),
			fields: [{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Reason'), reqd: 1 }],
			primary_action_label: __('Remove'),
			primary_action: (v) => frappe.call({
				method: M + 'remove_saved_card', args: { saved_card: card, reason: v.reason }, freeze: true,
				callback: () => { d.hide(); frappe.show_alert({ message: __('Card removed'), indicator: 'green' }); renderCustomerCards(frm); },
			}),
		});
		d.show();
	}

	frappe.ui.form.on('Customer', { refresh: renderCustomerCards });

})();
