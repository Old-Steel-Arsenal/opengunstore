// Distributors hub — Settings entry for every wholesale house (one card per
// Distributor row). Provider-agnostic: everything renders from hub.overview();
// account and shipping settings stay in each provider's own Single (card links
// to it), so a new house shows up here with ZERO page changes.

frappe.pages['distributor-hub'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Distributors'),
		single_column: true,
	});
	new DistributorHubApp(page);
};

class DistributorHubApp {
	constructor(page) {
		this.page = page;
		this.data = null;
		this.$body = $(`
			<div class="dist-hub">
				<style>
					.dist-hub { max-width: 860px; margin: 0 auto; padding: 4px 0 60px; }
					.dist-hub .hub-card { background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
					.dist-hub .hub-card.off { opacity: .62; }
					.dist-hub .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
					.dist-hub .prio { font-size: 11px; font-weight: 600; color: var(--text-muted);
						border: 1px solid var(--border-color); border-radius: 10px; padding: 0 7px; }
					.dist-hub .hname { font-size: 15px; font-weight: 600; }
					.dist-hub .prov { font-size: 11px; font-weight: 600; text-transform: uppercase;
						background: var(--bg-light-gray); border-radius: 4px; padding: 1px 6px; }
					.dist-hub .prov.bad { color: var(--red-600, #c0392b); background: transparent;
						border: 1px solid var(--red-300, #e8a49c); }
					.dist-hub .supplier { font-size: 12px; color: var(--text-muted); }
					.dist-hub .spacer { flex: 1; }
					.dist-hub .move { display: inline-flex; gap: 2px; }
					.dist-hub .move .btn { padding: 1px 7px; line-height: 1.4; }
					.dist-hub .toggles { display: flex; gap: 14px; flex-wrap: wrap; margin: 10px 0 2px; }
					.dist-hub .toggles label { display: inline-flex; align-items: center; gap: 5px;
						font-size: 13px; margin: 0; cursor: pointer; }
					.dist-hub .toggles label.ro { cursor: default; color: var(--text-muted); }
					.dist-hub .meta { font-size: 12px; color: var(--text-muted); margin-top: 7px;
						display: flex; gap: 16px; flex-wrap: wrap; }
					.dist-hub .meta .warn { color: var(--red-600, #c0392b); }
					.dist-hub .meta a { cursor: pointer; }
					.dist-hub .attn { color: var(--red-600, #c0392b); font-weight: 600; }
					.dist-hub .foot { display: flex; gap: 8px; margin-top: 11px; flex-wrap: wrap; }
					.dist-hub .empty { text-align: center; color: var(--text-muted); padding: 46px 0 26px; }
				</style>
				<div class="cards"></div>
				<div class="hub-actions" style="margin-top:14px"></div>
			</div>`);
		this.$body.appendTo(this.page.main);
		this.refresh();
	}

	refresh() {
		frappe.call({ method: 'ffl_integrations.distributor.hub.overview' }).then((r) => {
			this.data = r.message || { distributors: [], providers: [], can_write: false };
			// The overview carries the site's capabilities: keep the desk's copy current.
			if (this.data.capabilities && frappe.boot) {
				frappe.boot.ffl_capabilities = Object.assign({}, frappe.boot.ffl_capabilities, this.data.capabilities);
			}
			this.render();
		});
	}

	render() {
		const d = this.data;
		const $cards = this.$body.find('.cards').empty();
		// No distributor catalog on this site: one notice, not an error on every card.
		if ((d.capabilities || {}).catalog === 'off' && window.ffl && ffl.capabilities) {
			$(`<div style="margin-bottom:12px">${ffl.capabilities.notice('catalog')}</div>`).appendTo($cards);
		}
		const warning = (d.catalog_local || {}).warning;
		if (warning) {
			$('<div class="alert alert-warning" style="margin-bottom:12px"></div>')
				.text(warning).appendTo($cards);
		}
		if (!d.distributors.length) {
			$cards.append(`<div class="empty">${__('No distributors configured yet.')}</div>`);
		}
		d.distributors.forEach((row, idx) => this.renderCard($cards, row, idx));

		const $actions = this.$body.find('.hub-actions').empty();
		if (d.can_write) {
			$('<button class="btn btn-primary btn-sm"></button>')
				.text(__('Add Distributor'))
				.on('click', () => this.addDialog())
				.appendTo($actions);
		}
	}

	renderCard($cards, row, idx) {
		const esc = frappe.utils.escape_html;
		const d = this.data;
		const $card = $(`<div class="hub-card ${row.enabled ? '' : 'off'}"></div>`).appendTo($cards);

		// ---- header: order, identity, provider, move/probe/jump buttons
		const $head = $('<div class="head"></div>').appendTo($card);
		$head.append(`<span class="prio">#${idx + 1}</span>`);
		$head.append(`<span class="hname">${esc(row.distributor_name || row.name)}</span>`);
		const provTag = row.provider_registered
			? `<span class="prov">${esc(row.provider)}</span>`
			: `<span class="prov bad">${esc(row.provider)} · ${__('not registered')}</span>`;
		$head.append(provTag);
		if (row.supplier) $head.append(`<span class="supplier">${esc(row.supplier)}</span>`);
		$head.append('<span class="spacer"></span>');
		if (d.can_write && d.distributors.length > 1) {
			const $move = $('<span class="move"></span>').appendTo($head);
			$('<button class="btn btn-default btn-xs" title="Move up">↑</button>')
				.prop('disabled', idx === 0)
				.on('click', () => this.move(idx, -1)).appendTo($move);
			$('<button class="btn btn-default btn-xs" title="Move down">↓</button>')
				.prop('disabled', idx === d.distributors.length - 1)
				.on('click', () => this.move(idx, +1)).appendTo($move);
		}

		// ---- routing flag toggles
		const $tg = $('<div class="toggles"></div>').appendTo($card);
		[['enabled', __('Enabled')], ['dropship_enabled', __('Drop-ship')],
			['stocking_enabled', __('Stocking')], ['poll_enabled', __('Poll')]]
			.forEach(([flag, label]) => {
				const $label = $(`<label class="${d.can_write ? '' : 'ro'}"></label>`).appendTo($tg);
				const $cb = $(`<input type="checkbox">`).prop('checked', !!row[flag])
					.prop('disabled', !d.can_write).appendTo($label);
				$label.append(document.createTextNode(label));
				$cb.on('change', () => {
					const value = $cb.prop('checked') ? 1 : 0;
					$cb.prop('disabled', true);
					// frappe.call does not return a standard Promise (no .catch/.finally) --
					// Success and failure go through the callback/error options.
					frappe.call({
						method: 'ffl_integrations.distributor.hub.toggle_flag',
						args: { distributor: row.name, flag, value },
						callback: (r) => {
							row[flag] = r.message.value;
							this.render();
						},
						error: () => {
							$cb.prop('checked', !!row[flag]).prop('disabled', !d.can_write);
						},
					});
				});
			});

		// ---- read-only summary (Ordering / Ship to store): each item opens its tab
		// on the provider's settings page — the URL hash picks the tab.
		if ((row.summary || []).length) {
			const $sum = $('<div class="meta"></div>').appendTo($card);
			row.summary.forEach((item) => {
				const text = `${esc(item.label)}: ${esc(item.value)}`;
				if (!row.settings_doctype) {
					$sum.append(`<span>${text}</span>`);
					return;
				}
				$(`<a>${text}</a>`).on('click', () => {
					frappe.route_hash = `#${item.tab}`;
					frappe.set_route('Form', row.settings_doctype);
				}).appendTo($sum);
			});
		}

		// ---- health line
		const $meta = $('<div class="meta"></div>').appendTo($card);
		if (row.health.catalog_off) {
			// A state, not a fault (the notice above says what to do).
			$meta.append(`<span>${__('Catalog')}: ${__('off')}</span>`);
		} else if (row.health.error) {
			$meta.append(`<span class="warn">${esc(row.health.error)}</span>`);
		} else {
			$meta.append(`<span>${__('Quantity feed')}: ${this.when(row.health.feed_heartbeat)}</span>`);
			$meta.append(`<span>${__('Catalog')}: ${this.when(row.health.catalog_heartbeat)}</span>`);
		}
		if (row.poll_enabled) {
			$meta.append(`<span>${__('Last order poll')}: ${this.when(row.last_poll_run_at)}</span>`);
		}

		// ---- local catalog lane health
		const cl = row.catalog_lanes;
		if (cl) {
			const $lanes = $('<div class="meta"></div>').appendTo($card);
			$lanes.append(`<span>${__('Local catalog')}: ${__('{0} offers, {1} active', [cl.offers_total, cl.offers_active])}</span>`);
			cl.lanes.forEach((ln) => {
				const failed = ln.last_status === 'failed';
				let text = `${esc(ln.lane)}: ${this.when(ln.last_success_at)}`;
				if (ln.last_success_at) {
					text += ` · ${__('{0} rows', [ln.rows])}`;
				}
				if (failed) {
					text += ` · ${__('last run failed ({0})', [esc(ln.error_class)])}`;
				}
				$lanes.append(`<span${failed ? ' class="warn"' : ''}>${text}</span>`);
			});
		}

		// ---- open-order + pricing summary
		const o = row.orders;
		const $line2 = $('<div class="meta"></div>').appendTo($card);
		const orderBits = [
			__('{0} draft', [o.draft]),
			__('{0} in flight', [o.in_flight]),
			o.attention ? `<span class="attn">${__('{0} need attention', [o.attention])}</span>`
				: __('0 need attention'),
		].join(' · ');
		$(`<a>${orderBits}</a>`).on('click', () => {
			frappe.set_route('List', 'Distributor Order', { distributor: row.name });
		}).appendTo($line2);
		if (row.markup_rules !== undefined) {  // the drop-ship layer's columns
			$line2.append(`<span>${__('Markup rules')}: ${row.markup_rules}` +
				(row.default_markup_pct ? ` · ${__('default')} ${row.default_markup_pct}%` : '') + `</span>`);
		}

		// ---- action buttons
		const $foot = $('<div class="foot"></div>').appendTo($card);
		$('<button class="btn btn-default btn-sm"></button>').text(__('Test Connection'))
			.prop('disabled', !row.provider_registered)
			.on('click', (e) => this.testConnection(row, $(e.currentTarget))).appendTo($foot);
		if (d.can_sync_now && row.catalog_lanes) {
			$('<button class="btn btn-default btn-sm"></button>').text(__('Sync now'))
				.on('click', (e) => this.syncNow(row, $(e.currentTarget))).appendTo($foot);
		}
		// Named by what each page holds: the Distributor record (markup, stock
		// publishing, supplier) and the provider's own settings page.
		$('<button class="btn btn-default btn-sm"></button>').text(__('Pricing & stock'))
			.on('click', () => frappe.set_route('Form', 'Distributor', row.name)).appendTo($foot);
		if (row.settings_doctype) {
			$('<button class="btn btn-default btn-sm"></button>').text(__('Account & shipping'))
				.on('click', () => frappe.set_route('Form', row.settings_doctype)).appendTo($foot);
		}
		if (row.remote_catalog && this.data.dropship_layer) {
			$('<button class="btn btn-default btn-sm"></button>').text(__('Catalog Service'))
				.on('click', () => frappe.set_route('catalog-service', row.name)).appendTo($foot);
		}
		if (row.enabled && row.dropship_enabled && this.data.dropship_layer) {
			// Listings, rules, markup and stock publishing for this house's web store.
			$('<button class="btn btn-default btn-sm"></button>').text(__('Drop-ship Center'))
				.on('click', () => frappe.set_route('dropship-center', row.name)).appendTo($foot);
		}
	}

	when(value) {
		return value ? frappe.datetime.comment_when(value) : `<span>${__('never')}</span>`;
	}

	move(idx, delta) {
		const names = this.data.distributors.map((r) => r.name);
		const [n] = names.splice(idx, 1);
		names.splice(idx + delta, 0, n);
		frappe.call({
			method: 'ffl_integrations.distributor.hub.reorder',
			args: { names },
		}).then(() => this.refresh());
	}

	syncNow(row, $btn) {
		$btn.prop('disabled', true).text(__('Queuing…'));
		frappe.call({
			method: 'ffl_integrations.distributor.hub.sync_now',
			args: { distributor: row.name },
			// Not a standard Promise: the reset button must use the always option, .finally throws a TypeError.
			always: () => $btn.prop('disabled', false).text(__('Sync now')),
			callback: (r) => {
				const m = r.message || {};
				const n = (m.enqueued || []).length + (m.deduplicated || []).length;
				frappe.show_alert({
					message: m.worker_running === false
						? __('{0} lanes queued, but the catalog worker is not running', [n])
						: __('{0} lanes queued', [n]),
					indicator: m.worker_running === false ? 'orange' : 'green',
				});
				this.refresh();
			},
		});
	}

	testConnection(row, $btn) {
		$btn.prop('disabled', true).text(__('Testing…'));
		frappe.call({
			method: 'ffl_integrations.distributor.hub.test_connection',
			args: { distributor: row.name },
			// Not a standard Promise: the reset button must use the always option, .finally throws a TypeError.
			always: () => $btn.prop('disabled', false).text(__('Test Connection')),
			callback: (r) => {
				ffl.showConnectionChecks(row.name, (r.message || {}).checks || []);
			},
		});
	}

	addDialog() {
		const esc = frappe.utils.escape_html;
		const providers = this.data.providers;
		const dialog = new frappe.ui.Dialog({
			title: __('Add Distributor'),
			fields: [
				{
					fieldname: 'provider', fieldtype: 'Select', label: __('Provider'),
					reqd: 1, options: providers.map((p) => p.code).join('\n'),
					description: __('Integration backend for the new house.'),
				},
				{ fieldname: 'info', fieldtype: 'HTML' },
			],
			primary_action_label: __('Create'),
			primary_action: (values) => {
				dialog.hide();
				frappe.route_options = { provider: values.provider };
				frappe.new_doc('Distributor');
			},
		});
		const renderInfo = () => {
			const p = providers.find((x) => x.code === dialog.get_value('provider'));
			dialog.get_field('info').$wrapper.html(p && p.used_by.length
				? `<div class="text-muted" style="font-size:12px">${__('Already used by')}: ${p.used_by.map(esc).join(', ')}</div>`
				: '');
		};
		dialog.fields_dict.provider.df.onchange = renderInfo;
		dialog.show();
		if (providers.length === 1) dialog.set_value('provider', providers[0].code);
		renderInfo();
	}
}
