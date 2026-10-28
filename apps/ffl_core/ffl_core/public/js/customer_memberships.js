// Memberships panel on the Customer form (rules: ffl_core/membership.py).
//   Lists the customer's memberships (plan, status, period, next charge, renewal card) under the
//   Agreements section and sells a new one: pick the plan → the plan's terms signed for that
//   membership (the existing agreement flow, Per document) → the server builds and submits the
//   Sales Invoice → payment the counter's usual way. With auto-renew on the card is charged on the
//   terminal and saved for recurring charges (card-on-file gates apply); untick auto-renew to take
//   any other tender. The membership turns Active once its invoice is paid (server side).
//   An overdue renewal (Past Due / Suspended / not charged) offers Retry card (managers) and Take
//   payment (any tender on the renewal invoice); paid, it is Active again from the original due date.
//   Shown only once the store has an active plan or the customer has a membership.
(function () {
	const M = 'ffl_core.membership.';
	const READ_ROLES = ['Sales User', 'Sales Manager', 'Stock Manager', 'System Manager', 'Accounts User', 'Accounts Manager'];
	const esc = (s) => frappe.utils.escape_html(s == null ? '' : String(s));
	const date = (d) => (d ? frappe.datetime.str_to_user(d) : '');
	const STATUS_COLOR = { Pending: 'orange', Active: 'green', 'Past Due': 'orange', Suspended: 'red', Cancelled: 'gray', Expired: 'gray' };
	const cardMop = () => (frappe.boot && frappe.boot.card_mop) || 'Credit Card';
	const terminalLive = () => {
		const card = frappe.boot && frappe.boot.ffl_card_processor;
		return !!(Number(card && card.enabled) && window.ffl && window.ffl.terminalCharge);
	};
	const call = (method, args) => frappe.call({ method: M + method, args, freeze: true }).then((r) => r && r.message);

	function render(frm) {
		const field = frm.fields_dict.customer_agreements_html;
		const $anchor = field && field.$wrapper;
		if (!$anchor) return;
		$anchor.siblings('.ffl-memberships').remove();
		if (frm.is_new() || !frappe.user.has_role(READ_ROLES)) return;
		const customer = frm.doc.name;
		frappe.call({ method: M + 'get_memberships', args: { customer } }).then((r) => {
			const out = (r && r.message) || {};
			if (frm.doc.name !== customer || (!(out.plans || []).length && !(out.memberships || []).length)) return;
			const $block = $(`<div class="ffl-memberships" style="margin-top:12px">
				<div class="text-muted small" style="margin-bottom:4px">${esc(__('Memberships'))}</div>
				<table class="table table-sm"><tbody></tbody></table>
				${(out.plans || []).length ? `<button class="btn btn-xs btn-default" data-act="sell">${esc(__('Sell membership'))}</button>` : ''}
			</div>`);
			(out.memberships || []).forEach((m) => $block.find('tbody').append(rowHtml(m, out)));
			$anchor.siblings('.ffl-memberships').remove();
			$anchor.after($block);
			$block.on('click', '[data-act]', (e) => {
				const $b = $(e.currentTarget);
				const m = (out.memberships || []).find((x) => x.name === $b.attr('data-name'));
				act($b.attr('data-act'), frm, out, m);
			});
		}, () => { /* nothing to show */ });
	}

	function rowHtml(m, out) {
		const btn = (act, label) => `<button class="btn btn-xs btn-default" style="margin-left:4px" data-act="${act}" data-name="${esc(m.name)}">${esc(label)}</button>`;
		let detail = '';
		let buttons = '';
		if (m.status === 'Pending') {
			const needsTerms = m.terms_template && !['signed', 'expiring'].includes(m.terms_state);
			detail = m.unpaid_invoice ? __('Invoice {0}: {1} to pay', [esc(m.unpaid_invoice), format_currency(m.outstanding)])
				: needsTerms ? __('Waiting for {0} to be signed', [esc(m.terms_template)]) : __('Not paid yet');
			buttons = (needsTerms ? btn('terms', __('Get terms signed')) : btn('collect', __('Collect payment'))) + btn('drop', __('Drop'));
		} else {
			const period = m.current_period_start ? `${date(m.current_period_start)} – ${date(m.current_period_end)}` : '';
			const renew = m.cancel_at_period_end ? __('Ends {0}', [date(m.current_period_end)])
				: m.next_charge_date ? __('Next charge {0}', [date(m.next_charge_date)]) + (m.card_summary ? ` · ${esc(m.card_summary)}` : '')
					: m.auto_renew ? '' : __('Auto-renew off');
			const owed = m.overdue && m.unpaid_invoice ? __('Invoice {0}: {1} to pay', [esc(m.unpaid_invoice), format_currency(m.outstanding)]) : '';
			const grace = m.status === 'Past Due' && m.grace_until ? __('Grace until {0}', [date(m.grace_until)]) : '';
			detail = [period, renew, grace, owed].filter(Boolean).join(' · ');
			if (m.overdue && !m.cancel_at_period_end) {
				if (out.can_cancel && (m.can_retry || m.charge_waiting)) buttons += btn('retry', m.charge_waiting ? __('Look up card charge') : __('Retry card'));
				if (!m.charge_waiting) buttons += btn('pay', __('Take payment'));
			}
			if (['Active', 'Past Due', 'Suspended'].includes(m.status)) {
				if (!m.cancel_at_period_end) buttons += m.auto_renew ? btn('renew-off', __('Turn auto-renew off')) : btn('renew-on', __('Turn auto-renew on'));
				if ((out.cards || []).length) buttons += btn('card', __('Change card'));
				if (out.can_cancel) buttons += btn('cancel', __('Cancel'));
			}
		}
		return `<tr>
			<td>${esc(m.plan)} <span class="indicator-pill ${STATUS_COLOR[m.status] || 'gray'}">${esc(__(m.status))}</span></td>
			<td class="text-muted">${detail}</td>
			<td class="text-right" style="white-space:nowrap">${buttons}</td></tr>`;
	}

	async function act(what, frm, out, m) {
		const refresh = () => render(frm);
		try {
			if (what === 'sell') return sell(frm, out);
			if (what === 'terms') return askTerms(frm, m);
			if (what === 'collect') return collect(frm, m);
			if (what === 'pay') return collect(frm, m, 'invoice_renewal');
			if (what === 'retry') {
				const r = await call('retry_renewal', { membership: m.name });
				frappe.msgprint({ message: esc(r.message || ''), indicator: r.outcome === 'renewed' ? 'green' : 'orange', title: __('Renewal') });
				return refresh();
			}
			if (what === 'renew-off' || what === 'renew-on') {
				await call('set_auto_renew', { membership: m.name, enabled: what === 'renew-on' ? 1 : 0 });
				return refresh();
			}
			if (what === 'card') return changeCard(frm, out, m);
			if (what === 'cancel') return cancel(frm, m);
			if (what === 'drop') return drop(frm, m);
		} catch (e) { refresh(); /* the server message already showed */ }
	}

	function sell(frm, out) {
		const plans = out.plans || [];
		const label = (p) => `${p.name} (${format_currency(p.price)} ${__(p.billing_period === 'Yearly' ? 'per year' : 'per month')})`;
		const byLabel = {};
		plans.forEach((p) => { byLabel[label(p)] = p; });
		const d = new frappe.ui.Dialog({
			title: __('Sell membership'),
			fields: [
				{ fieldtype: 'Select', fieldname: 'plan', label: __('Plan'), reqd: 1, options: Object.keys(byLabel), default: Object.keys(byLabel)[0],
					onchange: () => { const p = byLabel[d.get_value('plan')]; if (p) d.set_value('auto_renew', p.auto_renew_default ? 1 : 0); } },
				{ fieldtype: 'Check', fieldname: 'auto_renew', label: __('Auto-renew'), default: plans.length && plans[0].auto_renew_default ? 1 : 0,
					description: __('Charges the card on the terminal and saves it for the renewals. Untick to take cash or another tender.') },
			],
			primary_action_label: __('Continue'),
			primary_action: async (v) => {
				const p = byLabel[v.plan];
				d.hide();
				try {
					const m = await call('start_membership', { customer: frm.doc.name, plan: p.name, auto_renew: v.auto_renew ? 1 : 0 });
					if (m.terms_template && !['signed', 'expiring'].includes(m.terms_state)) return askTerms(frm, m);
					return collect(frm, m);
				} catch (e) { render(frm); }
			},
		});
		d.show();
	}

	function askTerms(frm, m) {
		if (!(window.ffl && window.ffl.agreements)) return;
		render(frm);
		window.ffl.agreements.request(frm.doc.name, m.terms_template, {
			customer_name: frm.doc.customer_name, reference_doctype: 'Membership', reference_name: m.name,
			done: () => render(frm),
		});
		frappe.show_alert({ message: __('Once {0} is signed, click Collect payment.', [esc(m.terms_template)]), indicator: 'blue' }, 8);
	}

	async function collect(frm, m, method) {
		const inv = await call(method || 'invoice_membership', { membership: m.name });
		const si = inv.sales_invoice;
		const owed = flt(inv.outstanding);
		if (owed <= 0.005) return render(frm);
		if (inv.auto_renew) return chargeAndSave(frm, m, si, owed);
		const tr = window.ffl && window.ffl.tenderReference;
		const d = new frappe.ui.Dialog({
			title: __('Membership payment — {0}', [si]),
			fields: [
				{ fieldtype: 'HTML', fieldname: 'note', options: `<p>${__('Amount due: <b>{0}</b>', [format_currency(owed)])}</p>` },
				{ fieldtype: 'Select', fieldname: 'mode', label: __('Payment Method'), reqd: 1,
					options: ['Cash', cardMop(), 'Zelle', 'ACH', 'Check/Money Order'].join('\n'), default: terminalLive() ? cardMop() : 'Cash' },
				...(tr && tr.fields ? tr.fields({ terminal: true }) : []),
			],
			primary_action_label: __('Take payment'),
			primary_action: async (v) => {
				d.hide();
				try {
					// Card on the live terminal: the server books the Payment Entry on approval. Anything
					// else (cash, Zelle, a card already taken elsewhere) is the counter's Record Payment.
					if (v.mode === cardMop() && terminalLive() && !v.charged_outside) {
						await window.ffl.terminalCharge(terminalSpec(si, owed, 0));
					} else {
						await frappe.call({ method: 'ffl_core.api.manual_order.record_payment', freeze: true,
							args: { sales_invoice: si, mode_of_payment: v.mode, amount: owed, transaction_number: v.transaction_number || '' } });
					}
				} catch (e) { /* the server message already showed */ }
				render(frm);
			},
		});
		if (tr && tr.wire) tr.wire(d);
		d.show();
	}

	function terminalSpec(si, amount, saveCard) {
		return {
			amount,
			start: { method: 'ffl_integrations.payments.api.charge_sales_invoice', args: { sales_invoice: si, amount, ...(saveCard ? { save_card: 1 } : {}) } },
			poll: { method: 'ffl_integrations.payments.api.poll_sales_invoice_charge', args: { sales_invoice: si } },
			cancel: { method: 'ffl_integrations.payments.api.cancel_sales_invoice_charge', args: { sales_invoice: si } },
		};
	}

	async function chargeAndSave(frm, m, si, owed) {
		const cof = window.ffl && window.ffl.cardOnFile;
		const offer = terminalLive() && cof ? await cof.offer(frm.doc.name) : null;
		if (!offer) {
			// The Pending row has no auto-renew switch: offer to drop auto-renew here and take payment.
			frappe.confirm(__('This store cannot save the card on the terminal right now, so auto-renew cannot be set up. Take this payment without auto-renew?'),
				async () => {
					try {
						await call('set_auto_renew', { membership: m.name, enabled: 0 });
						await collect(frm, m);
					} catch (e) { render(frm); }
				}, () => render(frm));
			return;
		}
		if (!(await cof.ensureSigned(frm.doc.name, offer, frm.doc.customer_name))) return render(frm);
		await window.ffl.terminalCharge(terminalSpec(si, owed, 1));
		render(frm);
	}

	function changeCard(frm, out, m) {
		const cards = out.cards || [];
		const d = new frappe.ui.Dialog({
			title: __('Card for renewals'),
			fields: [{ fieldtype: 'Select', fieldname: 'card', label: __('Saved card'), reqd: 1,
				options: cards.map((c) => ({ value: c.name, label: c.card_summary || c.name })), default: m.saved_card || cards[0].name }],
			primary_action_label: __('Save'),
			primary_action: async (v) => {
				d.hide();
				try { await call('change_card', { membership: m.name, saved_card: v.card }); } catch (e) { /* shown */ }
				render(frm);
			},
		});
		d.show();
	}

	function cancel(frm, m) {
		const d = new frappe.ui.Dialog({
			title: __('Cancel membership {0}', [m.name]),
			fields: [
				{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Reason'), reqd: 1 },
				{ fieldtype: 'Check', fieldname: 'end_now', label: __('End it today'),
					description: __('Off: it runs to {0} and is not renewed. On: it ends today. Either way any refund is a credit note made by hand.', [date(m.current_period_end)]) },
			],
			primary_action_label: __('Cancel membership'),
			primary_action: async (v) => {
				d.hide();
				try { await call('cancel_membership', { membership: m.name, reason: v.reason, end_now: v.end_now ? 1 : 0 }); } catch (e) { /* shown */ }
				render(frm);
			},
		});
		d.show();
	}

	function drop(frm, m) {
		const d = new frappe.ui.Dialog({
			title: __('Drop membership {0}', [m.name]),
			fields: [{ fieldtype: 'Small Text', fieldname: 'reason', label: __('Reason'), reqd: 1,
				description: __('Only for a membership nobody paid for: its unpaid invoice is cancelled.') }],
			primary_action_label: __('Drop'),
			primary_action: async (v) => {
				d.hide();
				try { await call('discard_membership', { membership: m.name, reason: v.reason }); } catch (e) { /* shown */ }
				render(frm);
			},
		});
		d.show();
	}

	frappe.ui.form.on('Customer', { refresh: render });
})();
