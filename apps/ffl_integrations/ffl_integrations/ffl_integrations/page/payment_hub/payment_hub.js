// Payments hub — Settings entry for every card processor (one card per
// registry entry). Processor-agnostic: everything renders from
// payments.hub.overview(); credentials stay in each processor's own Single
// (card links to it), so the next processor shows up here with ZERO page
// changes — the registry lives in ffl_integrations/payments/hub.py.

frappe.pages['payment-hub'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Payments'),
		single_column: true,
	});
	new PaymentHubApp(page);
};

class PaymentHubApp {
	constructor(page) {
		this.page = page;
		this.data = null;
		this.$body = $(`
			<div class="pay-hub">
				<style>
					.pay-hub { max-width: 860px; margin: 0 auto; padding: 4px 0 60px; }
					.pay-hub .hub-card { background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
					.pay-hub .hub-card.off { opacity: .62; }
					.pay-hub .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
					.pay-hub .hname { font-size: 15px; font-weight: 600; }
					.pay-hub .kind { font-size: 11px; font-weight: 600; text-transform: uppercase;
						background: var(--bg-light-gray); border-radius: 4px; padding: 1px 6px; }
					.pay-hub .state { font-size: 11px; font-weight: 600; border-radius: 4px; padding: 1px 6px; }
					.pay-hub .state.ok { color: var(--green-600, #1f7a35); border: 1px solid var(--green-300, #9ad2a9); }
					.pay-hub .state.warn { color: var(--red-600, #c0392b); border: 1px solid var(--red-300, #e8a49c); }
					.pay-hub .state.idle { color: var(--text-muted); border: 1px solid var(--border-color); }
					.pay-hub .spacer { flex: 1; }
					.pay-hub .toggles { display: flex; gap: 14px; flex-wrap: wrap; margin: 10px 0 2px; }
					.pay-hub .toggles label { display: inline-flex; align-items: center; gap: 5px;
						font-size: 13px; margin: 0; cursor: pointer; }
					.pay-hub .toggles label.ro { cursor: default; color: var(--text-muted); }
					.pay-hub .meta { font-size: 12px; color: var(--text-muted); margin-top: 7px;
						display: flex; gap: 16px; flex-wrap: wrap; }
					.pay-hub .foot { display: flex; gap: 8px; margin-top: 11px; flex-wrap: wrap; }
					.pay-hub .note { font-size: 12px; color: var(--text-muted); margin-top: 14px; }
					.pay-hub .chk { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
						margin-right: 7px; vertical-align: middle; }
				</style>
				<div class="cards"></div>
				<div class="note"></div>
			</div>`);
		this.$body.appendTo(this.page.main);
		this.refresh();
	}

	refresh() {
		frappe.call({ method: 'ffl_integrations.payments.hub.overview' }).then((r) => {
			this.data = r.message || { processors: [], can_write: false };
			this.render();
		});
	}

	render() {
		const $cards = this.$body.find('.cards').empty();
		this.data.processors.forEach((row) => this.renderCard($cards, row));
		if (this.data.card_on_file) this.renderCardOnFile($cards, this.data.card_on_file);
		this.$body.find('.note').text(__(
			'Adding a processor is a registry entry in ffl_integrations/payments/hub.py '
			+ 'plus its own Settings doctype — this page and the workspace card stay as they are.'));
	}

	renderCard($cards, row) {
		const esc = frappe.utils.escape_html;
		const $card = $(`<div class="hub-card ${row.enabled ? '' : 'off'}"></div>`).appendTo($cards);

		// ---- header: identity + live state
		const $head = $('<div class="head"></div>').appendTo($card);
		$head.append(`<span class="hname">${esc(row.label)}</span>`);
		$head.append(`<span class="kind">${esc(row.kind)}</span>`);
		// Three states, not two: the switch can be ON while the key is missing,
		// and painting that "ready" is how a store finds out at the counter.
		const state = !row.enabled
			? `<span class="state idle">${__('switched off — cards record as plain tender')}</span>`
			: (row.ready
				? `<span class="state ok">${__('ready — terminal flow armed')}</span>`
				: `<span class="state warn">${__('enabled but NOT ready — credentials missing')}</span>`);
		$head.append(state);
		$head.append('<span class="spacer"></span>');

		// ---- enable switch
		const $tg = $('<div class="toggles"></div>').appendTo($card);
		const $label = $(`<label class="${row.can_write ? '' : 'ro'}"></label>`).appendTo($tg);
		const $cb = $(`<input type="checkbox">`).prop('checked', !!row.enabled)
			.prop('disabled', !row.can_write).appendTo($label);
		$label.append(document.createTextNode(__('Enabled')));
		$cb.on('change', () => {
			const value = $cb.prop('checked') ? 1 : 0;
			$cb.prop('disabled', true);
			// frappe.call does not return a standard Promise (no .catch/.finally) —
			// success and failure go through the callback/error options. The most common reason enabling fails is missing credentials
			// (the server runs a real doc.save(), so the settings form's required-field rules apply unchanged).
			frappe.call({
				method: 'ffl_integrations.payments.hub.toggle',
				args: { processor: row.code, value },
				callback: () => this.refresh(),
				error: () => {
					$cb.prop('checked', !!row.enabled).prop('disabled', !row.can_write);
				},
			});
		});

		// ---- health + tender line
		const $meta = $('<div class="meta"></div>').appendTo($card);
		(row.health || []).forEach((h) => {
			$meta.append(`<span>${esc(__(h.label))}: ${this.when(h.at)}</span>`);
		});
		$meta.append(`<span>${__('Mode of Payment')}: ${esc(row.mode_of_payment)}</span>`);

		// ---- action buttons
		const $foot = $('<div class="foot"></div>').appendTo($card);
		$('<button class="btn btn-default btn-sm"></button>').text(__('Test Connection'))
			.on('click', (e) => this.testConnection(row, $(e.currentTarget))).appendTo($foot);
		$('<button class="btn btn-default btn-sm"></button>').text(__('Credentials'))
			.on('click', () => frappe.set_route('Form', row.settings_doctype)).appendTo($foot);
	}

	// Card on file (payments/saved_cards.py): "Save this card" at the terminal, after the
	// customer signs the authorization picked here. Saved through the Single's own rules.
	renderCardOnFile($cards, cof) {
		const esc = frappe.utils.escape_html;
		const supported = this.data.processors.some((p) => p.enabled && p.ready && p.supports_card_on_file);
		const $card = $(`<div class="hub-card ${cof.enabled ? '' : 'off'}"></div>`).appendTo($cards);
		const $head = $('<div class="head"></div>').appendTo($card);
		$head.append(`<span class="hname">${esc(__('Card on file'))}</span>`);
		$head.append(cof.enabled
			? (supported ? `<span class="state ok">${esc(__('on — "Save this card" is offered at the terminal'))}</span>`
				: `<span class="state warn">${esc(__('on, but the active processor cannot save cards'))}</span>`)
			: `<span class="state idle">${esc(__('off'))}</span>`);
		const $form = $('<div style="margin-top:10px; max-width:420px"></div>').appendTo($card);
		const control = frappe.ui.form.make_control({
			parent: $form, render_input: true,
			df: { fieldtype: 'Link', fieldname: 'card_on_file_template', options: 'Agreement Template',
				label: __('Authorization the customer signs first'),
				get_query: () => ({ filters: { active: 1, scope: 'Per customer' } }) },
		});
		control.set_value(cof.template || '');
		control.$input.prop('disabled', !cof.can_write);
		const $tg = $('<div class="toggles"></div>').appendTo($card);
		const $label = $(`<label class="${cof.can_write ? '' : 'ro'}"></label>`).appendTo($tg);
		const $cb = $('<input type="checkbox">').prop('checked', !!cof.enabled)
			.prop('disabled', !cof.can_write).appendTo($label);
		$label.append(document.createTextNode(__('Offer "Save this card"')));
		$card.append(`<div class="meta"><span>${esc(__('A saved card can later be charged against a counter order\'s balance (Sales Invoice → Charge saved card). The card number never reaches this system: the processor keeps it and hands back a token, stored encrypted.'))}</span></div>`);
		const $foot = $('<div class="foot"></div>').appendTo($card);
		$('<button class="btn btn-default btn-sm"></button>').text(__('Save'))
			.prop('disabled', !cof.can_write).appendTo($foot)
			.on('click', () => frappe.call({
				method: 'ffl_integrations.payments.hub.save_card_on_file',
				args: { enabled: $cb.prop('checked') ? 1 : 0, template: control.get_value() || '' },
				callback: () => { frappe.show_alert({ message: __('Saved'), indicator: 'green' }); this.refresh(); },
			}));
	}

	when(value) {
		return value ? frappe.datetime.comment_when(value) : `<span>${__('never')}</span>`;
	}

	testConnection(row, $btn) {
		const esc = frappe.utils.escape_html;
		$btn.prop('disabled', true).text(__('Testing…'));
		frappe.call({
			method: 'ffl_integrations.payments.hub.test_connection',
			args: { processor: row.code },
			// Not a standard Promise: resetting the button has to use the always option; .finally throws a TypeError.
			always: () => $btn.prop('disabled', false).text(__('Test Connection')),
			callback: (r) => {
				const checks = (r.message || {}).checks || [];
				const color = (ok) => ok === true ? 'var(--green-500, #28a745)'
					: ok === false ? 'var(--red-500, #dc3545)' : 'var(--gray-400, #adb5bd)';
				const rows = checks.map((c) =>
					`<tr><td style="white-space:nowrap;padding:4px 12px 4px 0">
						<span class="chk" style="background:${color(c.ok)}"></span>${esc(c.label)}</td>
					<td style="padding:4px 0">${esc(c.detail || '')}</td></tr>`).join('');
				const allOk = checks.every((c) => c.ok !== false);
				frappe.msgprint({
					title: allOk ? __('{0} — connection OK', [row.label])
						: __('{0} — connection problems', [row.label]),
					indicator: allOk ? 'green' : 'red',
					message: `<table class="pay-hub" style="width:100%">${rows}</table>`,
				});
			},
		});
	}
}
