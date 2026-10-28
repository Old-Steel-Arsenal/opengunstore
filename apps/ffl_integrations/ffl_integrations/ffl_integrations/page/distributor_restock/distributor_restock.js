// Distributor Orders — one cart across every house + tracking of every open
// distributor order (the old single-house Distributor Restock page, Flow A).
//
// Provider-agnostic: every call goes through the whitelisted
// ffl_integrations.distributor.api.* layer (search / check_availability /
// create_orders / confirm_order / list_orders), so the page never hardcodes a
// house. Search with no house named returns one row per product with every
// enabled house's offer; each cart line defaults to the cheapest house that can
// fill it (staff can switch it). Create groups the cart by house and runs
// create_orders once per house — plan_fulfillment may SPLIT further (guns and
// accessories never mix; >400 SKUs chunk). MAP is display-only — no clamp.

frappe.pages['distributor-restock'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Distributor Orders'),
		single_column: true,
	});
	new DistributorOrdersApp(page);
};

const ORDER_TYPES = ['Stocking', 'Dropship Accessory', 'Dropship Firearm', 'Procure To Store'];
const STATUS_COLORS = {
	Draft: 'gray', Queued: 'blue', Placed: 'blue', Acknowledged: 'blue', Hold: 'orange',
	'Partially Shipped': 'yellow', Complete: 'green', Cancelled: 'gray', Error: 'red',
};

class DistributorOrdersApp {
	constructor(page) {
		this.page = page;
		// {key, upc, item_name, item_code, map, msrp, ffl_required, is_firearm, qty,
		//  offers: [{distributor, sku, cost, qty}], pick: distributor, manual: bool}
		this.cart = [];
		this.$body = $(`
			<div class="dist-restock">
				<style>
					.dist-restock { max-width: 1100px; margin: 0 auto; padding: 4px 0 80px; }
					.dist-restock .section { background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 8px; padding: 16px 18px; margin-bottom: 14px; }
					.dist-restock h3 { margin: 0 0 12px; font-size: 15px; font-weight: 600; }
					.dist-restock table { width: 100%; border-collapse: collapse; }
					.dist-restock th, .dist-restock td { padding: 7px 9px; border-bottom: 1px solid var(--border-color);
						font-size: 13px; text-align: left; vertical-align: middle; }
					.dist-restock th { background: var(--bg-light-gray); font-weight: 600; }
					.dist-restock .num { text-align: right; font-variant-numeric: tabular-nums; }
					.dist-restock .map { color: var(--text-muted); }
					.dist-restock .ffl { display: inline-block; font-size: 11px; font-weight: 600;
						color: var(--red-600); border: 1px solid var(--red-300); border-radius: 4px; padding: 0 5px; }
					.dist-restock .thumb { width: 34px; height: 34px; object-fit: contain; border-radius: 4px; }
					.dist-restock input.qty { width: 64px; }
					.dist-restock select.pick { min-width: 220px; }
					.dist-restock .muted { color: var(--text-muted); font-size: 12px; }
					.dist-restock .avail-ok { color: var(--green-600); font-weight: 600; }
					.dist-restock .avail-no { color: var(--red-600); font-weight: 600; }
					.dist-restock .offer { white-space: nowrap; }
					.dist-restock .offer.best { font-weight: 600; }
					.dist-restock .tabs { display: flex; gap: 8px; margin-bottom: 14px; }
					.dist-restock .filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
					.dist-restock .filters select { width: auto; }
					.dist-restock tr.order-row { cursor: pointer; }
					.dist-restock tr.order-lines td { background: var(--bg-light-gray); }
					.dist-restock .wrap { white-space: nowrap; font-size: 12px; }
					/* Narrow screens: tables scroll inside their card instead of spilling out. */
					.dist-restock .results, .dist-restock .cart, .dist-restock .created-list,
					.dist-restock .orders, .dist-restock .outside, .dist-restock .invoices { overflow-x: auto; }
					.dist-restock .ref { overflow-wrap: anywhere; min-width: 110px; }
					.dist-restock .indicator-pill { white-space: nowrap; }
				</style>

				<div class="tabs">
					<button class="btn btn-sm btn-primary tab-btn" data-tab="order">${__('New Order')}</button>
					<button class="btn btn-sm btn-default tab-btn" data-tab="track">${__('Track Orders')}</button>
				</div>

				<div class="tab tab-order">
					<div class="section">
						<h3>${__('Search Catalog')}</h3>
						<div class="filters">
							<select class="form-control input-sm house-filter"></select>
						</div>
						<input type="text" class="form-control search-box"
							placeholder="${__('UPC / distributor SKU / mfg # / manufacturer / model…')}" autocomplete="off">
						<div class="results" style="margin-top:12px;"></div>
					</div>

					<div class="section">
						<h3>${__('Cart')} <span class="muted cart-count"></span></h3>
						<div class="cart"></div>
						<div class="cart-totals muted" style="margin-top:8px;"></div>
						<div style="margin-top:12px; display:flex; gap:8px;">
							<button class="btn btn-default btn-check">${__('Check Availability')}</button>
							<button class="btn btn-primary btn-create">${__('Create Draft Order(s)')}</button>
						</div>
					</div>

					<div class="section created" style="display:none;">
						<h3>${__('Created Orders')}</h3>
						<div class="created-list"></div>
					</div>
				</div>

				<div class="tab tab-track" style="display:none;">
					<div class="section">
						<div class="filters">
							<select class="form-control input-sm t-view">
								<option value="orders">${__('Orders')}</option>
								<option value="invoices">${__('Invoices')}</option>
							</select>
							<select class="form-control input-sm t-house"></select>
							<select class="form-control input-sm t-type v-orders"></select>
							<select class="form-control input-sm t-status v-orders"></select>
							<label class="v-invoices" style="display:none; margin:0;">
								<input type="checkbox" class="t-outside"> ${__('Placed outside POS only')}</label>
							<button class="btn btn-sm btn-default t-refresh">${__('Refresh')}</button>
							<button class="btn btn-sm btn-default t-sync v-invoices" style="display:none;">${__('Pull invoices now')}</button>
							<span class="muted t-count"></span>
						</div>
						<div class="orders v-orders"></div>
						<div class="outside v-orders" style="margin-top:18px;"></div>
						<div class="invoices v-invoices" style="display:none;"></div>
					</div>
				</div>
			</div>
		`);
		$(this.page.body).append(this.$body);
		this._bind();
		this._loadHouses();
		this.renderCart();
		// No distributor catalog on this site: the search says so up front (re-read,
		// Data Service Settings may have changed since the desk loaded).
		if (window.ffl && ffl.capabilities) ffl.capabilities.refresh().then(() => this._catalogNotice());
	}

	// The shared "not set up" notice in place of results; true when the catalog is off.
	_catalogNotice() {
		if (!(window.ffl && ffl.capabilities && ffl.capabilities.isOff('catalog'))) return false;
		this.$body.find('.results').html(ffl.capabilities.notice('catalog'));
		return true;
	}

	_loadHouses() {
		// Ordering offers enabled houses only; tracking lists every house, since a
		// house switched off can still have orders in flight.
		frappe.db.get_list('Distributor', {
			fields: ['name', 'enabled'], order_by: 'priority asc', limit: 50,
		}).then((rows) => {
			const opts = (names) => [`<option value="">${__('All distributors')}</option>`]
				.concat(names.map((h) => `<option value="${frappe.utils.escape_html(h)}">${frappe.utils.escape_html(h)}</option>`))
				.join('');
			this.$body.find('.house-filter').html(opts((rows || []).filter((r) => r.enabled).map((r) => r.name)));
			this.$body.find('.t-house').html(opts((rows || []).map((r) => r.name)));
		});
		this.$body.find('.t-type').html([`<option value="">${__('All types')}</option>`]
			.concat(ORDER_TYPES.map((t) => `<option value="${t}">${__(t)}</option>`)).join(''));
		this.$body.find('.t-status').html([
			`<option value="__open">${__('Open')}</option>`,
			`<option value="">${__('All')}</option>`,
		].concat(Object.keys(STATUS_COLORS).map((s) => `<option value="${s}">${__(s)}</option>`)).join(''));
	}

	_bind() {
		const search = frappe.utils.debounce(() => this.search(), 300);
		this.$body.find('.search-box').on('input', search);
		this.$body.find('.house-filter').on('change', () => this.search());
		this.$body.find('.btn-check').on('click', () => this.checkAvailability());
		this.$body.find('.btn-create').on('click', () => this.createOrders());
		this.$body.find('.tab-btn').on('click', (e) => this.showTab($(e.currentTarget).data('tab')));
		this.$body.find('.t-view, .t-house, .t-type, .t-status, .t-outside').on('change', () => this.loadTrack());
		this.$body.find('.t-refresh').on('click', () => this.loadTrack(true));
		this.$body.find('.t-sync').on('click', (e) => {
			$(e.currentTarget).prop('disabled', true);
			call('ffl_integrations.distributor.invoices.sync_invoices_now', {}).then((r) => {
				$(e.currentTarget).prop('disabled', false);
				if (r) {
					frappe.show_alert({ message: __('Pulling invoices in the background. Press Refresh in a minute.'),
						indicator: 'blue' });
				}
			});
		});
	}

	showTab(tab) {
		this.$body.find('.tab-btn').each((_, b) => {
			$(b).toggleClass('btn-primary', $(b).data('tab') === tab)
				.toggleClass('btn-default', $(b).data('tab') !== tab);
		});
		this.$body.find('.tab-order').toggle(tab === 'order');
		this.$body.find('.tab-track').toggle(tab === 'track');
		if (tab === 'track') this.loadTrack();
	}

	// ------------------------------------------------------------ new order

	search() {
		const q = (this.$body.find('.search-box').val() || '').trim();
		const house = this.$body.find('.house-filter').val() || null;
		const $results = this.$body.find('.results');
		if (this._catalogNotice()) return;
		if (q.length < 2) { $results.empty(); return; }
		frappe.call({
			method: 'ffl_integrations.distributor.api.search',
			args: { query: q, limit: 15, distributor: house },
			callback: (r) => this.renderResults((r.message || []).map(normalizeRow)),
		});
	}

	renderResults(rows) {
		const $r = this.$body.find('.results');
		if (!rows.length) { $r.html(`<div class="muted">${__('No matches.')}</div>`); return; }
		const body = rows.map((row, i) => {
			const img = row.image_url
				? `<img class="thumb" src="${frappe.utils.escape_html(row.image_url)}">` : '';
			const ffl = row.ffl_required ? `<span class="ffl">FFL</span>` : '';
			const best = pickBest(row.offers, 1);
			const offers = row.offers.map((o) => `<div class="offer ${o === best ? 'best' : ''}">
				${frappe.utils.escape_html(o.distributor)} · ${fmt_money(o.cost)} · ${o.qty}</div>`).join('');
			return `<tr data-i="${i}">
				<td>${img}</td>
				<td><b>${frappe.utils.escape_html(row.item_name || '')}</b> ${ffl}<br>
					<span class="muted">${frappe.utils.escape_html(row.upc || row.offers[0].sku || '')}</span></td>
				<td>${offers}</td>
				<td class="num map">${fmt_money(row.map)}</td>
				<td class="num muted">${fmt_money(row.msrp)}</td>
				<td><input type="number" min="1" value="1" class="form-control qty"></td>
				<td><button class="btn btn-xs btn-default btn-add">${__('Add')}</button></td>
			</tr>`;
		}).join('');
		$r.html(`<table><thead><tr>
			<th></th><th>${__('Product')}</th><th>${__('Distributor · Cost · Qty')}</th>
			<th class="num">${__('MAP')}</th><th class="num">${__('MSRP')}</th>
			<th>${__('Order Qty')}</th><th></th>
			</tr></thead><tbody>${body}</tbody></table>`);
		$r.find('.btn-add').on('click', (e) => {
			const $tr = $(e.currentTarget).closest('tr');
			const qty = parseInt($tr.find('.qty').val(), 10) || 1;
			this.addToCart(rows[$tr.data('i')], qty);
		});
	}

	addToCart(row, qty) {
		const key = row.upc || `${row.offers[0].distributor}:${row.offers[0].sku}`;
		const existing = this.cart.find((c) => c.key === key);
		if (existing) {
			existing.qty += qty;
			if (!existing.manual) existing.pick = pickBest(existing.offers, existing.qty).distributor;
		} else {
			this.cart.push({
				key, upc: row.upc, item_name: row.item_name, item_code: row.item_code || '',
				map: row.map, msrp: row.msrp, ffl_required: row.ffl_required,
				is_firearm: row.is_firearm, qty, offers: row.offers,
				pick: pickBest(row.offers, qty).distributor, manual: false,
			});
		}
		this.renderCart();
	}

	renderCart() {
		const $c = this.$body.find('.cart');
		this.$body.find('.cart-count').text(this.cart.length ? `(${this.cart.length})` : '');
		if (!this.cart.length) {
			$c.html(`<div class="muted">${__('Cart is empty.')}</div>`);
			this.$body.find('.cart-totals').empty();
			return;
		}
		const body = this.cart.map((c, i) => {
			const options = c.offers.map((o) => `<option value="${frappe.utils.escape_html(o.distributor)}"
				${o.distributor === c.pick ? 'selected' : ''}>${frappe.utils.escape_html(o.distributor)} — ${fmt_money(o.cost)} · ${o.qty} ${__('in stock')}</option>`).join('');
			const o = chosen(c);
			return `<tr data-i="${i}">
				<td><b>${frappe.utils.escape_html(c.item_name || '')}</b> ${c.ffl_required ? '<span class="ffl">FFL</span>' : ''}
					<br><span class="muted">${frappe.utils.escape_html(c.upc || '')} · ${frappe.utils.escape_html(o.sku || '')}</span></td>
				<td><select class="form-control input-sm pick">${options}</select></td>
				<td class="num">${fmt_money(o.cost)}</td>
				<td class="num map">${fmt_money(c.map)}</td>
				<td><input type="number" min="1" value="${c.qty}" class="form-control qty cart-qty"></td>
				<td class="num">${fmt_money((o.cost || 0) * c.qty)}</td>
				<td class="avail">${availCell(c)}</td>
				<td><button class="btn btn-xs btn-default btn-rm">${__('Remove')}</button></td>
			</tr>`;
		}).join('');
		$c.html(`<table><thead><tr>
			<th>${__('Product')}</th><th>${__('Order From')}</th><th class="num">${__('Cost')}</th>
			<th class="num">${__('MAP')}</th><th>${__('Qty')}</th><th class="num">${__('Line Total')}</th>
			<th>${__('Available')}</th><th></th>
			</tr></thead><tbody>${body}</tbody></table>`);
		$c.find('.pick').on('change', (e) => {
			const c = this.cart[$(e.currentTarget).closest('tr').data('i')];
			c.pick = $(e.currentTarget).val();
			c.manual = true;
			c.switched_from = '';
			this.renderCart();
		});
		$c.find('.cart-qty').on('change', (e) => {
			const c = this.cart[$(e.currentTarget).closest('tr').data('i')];
			c.qty = parseInt($(e.currentTarget).val(), 10) || 1;
			if (!c.manual) c.pick = pickBest(c.offers, c.qty).distributor;
			c.switched_from = '';
			this.renderCart();
		});
		$c.find('.btn-rm').on('click', (e) => {
			this.cart.splice($(e.currentTarget).closest('tr').data('i'), 1);
			this.renderCart();
		});
		const totals = Object.entries(this.byHouse()).map(([h, lines]) =>
			`${frappe.utils.escape_html(h)}: ${lines.length} ${__('line(s)')}, ${fmt_money(
				lines.reduce((s, c) => s + (chosen(c).cost || 0) * c.qty, 0))}`);
		this.$body.find('.cart-totals').html(totals.join(' &nbsp;|&nbsp; '));
	}

	byHouse() {
		const groups = {};
		this.cart.forEach((c) => { (groups[c.pick] = groups[c.pick] || []).push(c); });
		return groups;
	}

	// Live-check EVERY house's offer on every cart line (one call per house), write
	// the live qty / cost onto the offers, then move a line whose house cannot fill
	// it to the best house that can — a hand-picked house too (the note says so,
	// and staff can switch back). A line no house can fill stays put, marked ✗.
	async checkAvailability() {
		if (!this.cart.length) return;
		const perHouse = {};
		this.cart.forEach((c) => c.offers.forEach((o) => {
			(perHouse[o.distributor] = perHouse[o.distributor] || []).push({ c, o });
		}));
		frappe.dom.freeze(__('Checking live availability…'));
		try {
			await Promise.all(Object.entries(perHouse).map(async ([house, pairs]) => {
				const r = await call('ffl_integrations.distributor.api.check_availability', {
					lines: JSON.stringify(pairs.map(({ c, o }) => ({ sku: o.sku, qty: c.qty }))),
					distributor: house,
				});
				const bySku = {};
				(r || []).forEach((x) => { bySku[x.sku] = x; });
				pairs.forEach(({ c, o }) => {
					const a = bySku[o.sku];
					if (!a) return;
					// Blocked outright (discontinued, refused) → 0 at any qty; merely
					// short → the live qty still serves a smaller order.
					o.qty = a.orderable === false ? 0 : a.qty;
					if (parseFloat(a.cost) > 0) o.cost = parseFloat(a.cost);
					o.checked = true;
				});
			}));
		} finally {
			frappe.dom.unfreeze();
		}
		this.cart.forEach((c) => {
			c.switched_from = '';
			const cur = chosen(c);
			if (!cur.checked || cur.qty >= c.qty) return;
			const best = pickBest(c.offers.filter((o) => o.checked), c.qty);
			if (best && best.qty >= c.qty && best.distributor !== c.pick) {
				c.switched_from = c.pick;
				c.pick = best.distributor;
				c.manual = false;
			}
		});
		this.renderCart();
	}

	async createOrders() {
		if (!this.cart.length) { frappe.msgprint(__('Cart is empty.')); return; }
		const groups = this.byHouse();
		const created = [];
		const failed = [];
		frappe.dom.freeze(__('Planning + creating draft order(s)…'));
		try {
			for (const [house, lines] of Object.entries(groups)) {
				const r = await call('ffl_integrations.distributor.api.create_orders', {
					distributor: house,
					lines: JSON.stringify(lines.map((c) => ({
						sku: chosen(c).sku, item_code: c.item_code, upc: c.upc, qty: c.qty,
						description: c.item_name, is_firearm: c.is_firearm,
					}))),
					ship_context: JSON.stringify({ mode: 'stocking' }),
				});
				if (r && r.length) {
					r.forEach((o) => created.push(Object.assign({ distributor: house }, o)));
				} else {
					if (r) frappe.msgprint(__('No order was created for {0}.', [frappe.utils.escape_html(house)]));
					failed.push(house);
				}
			}
		} finally {
			frappe.dom.unfreeze();
		}
		// Keep the lines of a house that failed so staff can fix and retry them.
		this.cart = this.cart.filter((c) => failed.includes(c.pick));
		this.renderCart();
		if (created.length) this.renderCreated(created);
	}

	renderCreated(created) {
		const $sec = this.$body.find('.created');
		const rows = created.map((o) => `<tr data-do="${frappe.utils.escape_html(o.name)}">
			<td><a href="/app/distributor-order/${encodeURIComponent(o.name)}" target="_blank"><b>${frappe.utils.escape_html(o.name)}</b></a></td>
			<td>${frappe.utils.escape_html(o.distributor)}</td>
			<td>${frappe.utils.escape_html(o.order_type)}</td>
			<td>${frappe.utils.escape_html(o.account)}</td>
			<td class="num">${o.line_count}</td>
			<td><button class="btn btn-xs btn-primary btn-confirm">${__('Confirm & Queue')}</button></td>
		</tr>`).join('');
		$sec.find('.created-list').html(`<table><thead><tr>
			<th>${__('Order')}</th><th>${__('Distributor')}</th><th>${__('Type')}</th><th>${__('Account')}</th>
			<th class="num">${__('Lines')}</th><th></th></tr></thead><tbody>${rows}</tbody></table>
			<div style="margin-top:10px; display:flex; gap:8px; align-items:center;">
				<button class="btn btn-sm btn-primary btn-confirm-all">${__('Confirm & Queue All')}</button>
				<span class="muted">${__('One order per distributor; guns and accessories are split into separate orders and >400 SKUs are chunked. Confirm puts each order in the queue for the placement worker.')}</span>
			</div>`);
		$sec.show();
		const confirm = ($btn) => {
			const name = $btn.closest('tr').data('do');
			$btn.prop('disabled', true);
			return call('ffl_integrations.distributor.api.confirm_order', { do_name: name }).then((r) => {
				if (r) {
					$btn.text(__('Queued')).addClass('queued');
				} else {
					$btn.prop('disabled', false);
				}
				return !!r;
			});
		};
		$sec.find('.btn-confirm').on('click', (e) => confirm($(e.currentTarget)));
		$sec.find('.btn-confirm-all').on('click', async (e) => {
			const $all = $(e.currentTarget).prop('disabled', true);
			const pending = $sec.find('.btn-confirm:not(.queued)').toArray();
			let ok = 0;
			for (const b of pending) {
				if (await confirm($(b))) ok += 1;
			}
			if (ok === pending.length) {
				frappe.show_alert({ message: __('All {0} orders queued', [ok]), indicator: 'green' });
			} else {
				$all.prop('disabled', false);
				frappe.show_alert({ message: __('{0} of {1} orders queued; the rest are still drafts.',
					[ok, pending.length]), indicator: 'orange' });
			}
		});
	}

	// ------------------------------------------------------------ tracking

	// Every Track load bumps the sequence; a response for an older one is dropped,
	// so a slow answer never overwrites a newer filter's.
	loadTrack(refresh) {
		this._seq = (this._seq || 0) + 1;
		if (refresh) this._outside = null;
		const view = this.$body.find('.t-view').val();
		this.$body.find('.v-orders').toggle(view === 'orders');
		this.$body.find('.v-invoices').toggle(view === 'invoices');
		if (view === 'invoices') this.loadInvoices(); else this.loadOrders();
	}

	loadOrders() {
		const seq = this._seq;
		const status = this.$body.find('.t-status').val();
		frappe.call({
			method: 'ffl_integrations.distributor.api.list_orders',
			args: {
				distributor: this.$body.find('.t-house').val() || null,
				order_type: this.$body.find('.t-type').val() || null,
				status: status && status !== '__open' ? status : null,
				open_only: status === '__open' ? 1 : 0,
				with_lines: 1,
				limit: 200,
			},
			callback: (r) => { if (seq === this._seq) this.renderOrders(r.message || []); },
		});
		// Orders on a house's account placed elsewhere and not invoiced yet (houses
		// that can list their orders). They carry no type, so a type filter hides them.
		// One live read per page visit (Refresh re-reads); filters apply client-side.
		const $out = this.$body.find('.outside').empty();
		if (this.$body.find('.t-type').val() || (status && status !== '__open')) return;
		this._outside = this._outside
			|| call('ffl_integrations.distributor.invoices.outside_open_orders', {});
		this._outside.then((rows) => {
			if (seq !== this._seq) return;
			const house = this.$body.find('.t-house').val();
			rows = (rows || []).filter((o) => !house || o.distributor === house);
			if (!rows.length) return;
			const esc = frappe.utils.escape_html;
			$out.html(`<h3>${__('Placed outside POS, not invoiced yet')} <span class="muted">(${rows.length})</span></h3>
				<table><thead><tr><th>${__('Distributor')}</th><th>${__('Distributor Order #')}</th>
				<th>${__('PO')}</th><th>${__('Ordered')}</th><th>${__('Status Code')}</th></tr></thead>
				<tbody>${rows.map((o) => `<tr><td>${esc(o.distributor)}</td><td>${esc(o.order_ref)}</td>
					<td>${esc(o.po_number || '')}</td><td>${o.order_date ? frappe.datetime.str_to_user(o.order_date) : '—'}</td>
					<td>${esc(o.status || '')}</td></tr>`).join('')}</tbody></table>`);
		});
	}

	loadInvoices() {
		const seq = this._seq;
		frappe.call({
			method: 'ffl_integrations.distributor.invoices.list_invoices',
			args: {
				distributor: this.$body.find('.t-house').val() || null,
				outside_pos: this.$body.find('.t-outside').is(':checked') ? 1 : 0,
				limit: 200,
			},
			callback: (r) => { if (seq === this._seq) this.renderInvoices(r.message || []); },
		});
	}

	renderInvoices(rows) {
		const $i = this.$body.find('.invoices');
		this.$body.find('.t-count').text(`${rows.length} ${__('invoice(s)')}`);
		if (!rows.length) {
			$i.html(`<div class="muted">${__('No invoices yet. They are pulled from each distributor every hour (last 30 days).')}</div>`);
			return;
		}
		const esc = frappe.utils.escape_html;
		// Where the goods went. Only a Store invoice no POS order matches can be
		// received (Receive Goods → Incoming shipments); "Received" says it was.
		const DEST_COLORS = { 'Store': 'green', 'Drop-ship': 'blue', 'Unknown': 'gray' };
		const destTag = (v) => `<span class="indicator-pill ${DEST_COLORS[v.destination] || 'gray'}">${__(v.destination || 'Unknown')}</span>`
			+ (v.outside_pos && v.destination === 'Store'
				? (v.likely_order
					// Same item as an open POS order, invoice not linked yet: receive the order.
					? ` <span class="indicator-pill blue" title="${__('The status poll links the invoice to the order; receive the order.')}">${__('Likely POS order {0} — awaiting link', [esc(v.likely_order)])}</span>`
					: ` <span class="indicator-pill ${v.received ? 'green' : 'orange'}">${v.received ? __('Received') : __('To receive')}</span>`) : '');
		const body = rows.map((v, i) => `<tr class="order-row" data-i="${i}">
			<td class="ref"><a href="/app/distributor-invoice/${encodeURIComponent(v.name)}" target="_blank"><b>${esc(v.invoice_number)}</b></a>
				<br><span class="muted">${v.invoice_date ? frappe.datetime.str_to_user(v.invoice_date) : ''}</span></td>
			<td>${esc(v.distributor)}${v.account ? `<br><span class="muted">${esc(v.account)}</span>` : ''}</td>
			<td>${destTag(v)}</td>
			<td>${v.distributor_order
				? `<a href="/app/distributor-order/${encodeURIComponent(v.distributor_order)}" target="_blank">${esc(v.distributor_order)}</a>`
				: `<span class="indicator-pill orange">${__('Outside POS')}</span>`}
				${v.po_number && v.po_number !== v.distributor_order ? `<br><span class="muted">PO ${esc(v.po_number)}</span>` : ''}</td>
			<td class="num">${(v.lines || []).length}</td>
			<td class="wrap">${splitList(v.tracking_numbers).map(esc).join('<br>') || '<span class="muted">—</span>'}</td>
			<td class="num">${v.freight ? fmt_money(v.freight) : '—'}${v.fees ? `<br><span class="muted">${__('fees')} ${fmt_money(v.fees)}</span>` : ''}</td>
			<td class="num">${fmt_money(v.total)}</td>
		</tr>
		<tr class="order-lines" data-i="${i}" style="display:none;"><td colspan="8">
			<table><thead><tr><th>${__('SKU')}</th><th>${__('Description')}</th><th class="num">${__('Qty')}</th>
				<th class="num">${__('Unit Cost')}</th><th class="num">${__('Amount')}</th><th>${__('Serials')}</th><th>${__('Tracking')}</th></tr></thead>
			<tbody>${(v.lines || []).map((l) => `<tr><td>${esc(l.sku || '')}</td><td>${esc(l.description || '')}</td>
				<td class="num">${l.qty || 0}</td><td class="num">${fmt_money(l.unit_cost)}</td>
				<td class="num">${fmt_money(l.amount)}</td><td class="wrap">${splitList(l.serials).map(esc).join('<br>')}</td>
				<td class="wrap">${esc(l.tracking_number || '')}</td></tr>`).join('')}</tbody></table>
		</td></tr>`).join('');
		$i.html(`<table><thead><tr><th>${__('Invoice')}</th><th>${__('Distributor')}</th><th>${__('Goods went to')}</th><th>${__('POS Order')}</th>
			<th class="num">${__('Lines')}</th><th>${__('Tracking')}</th><th class="num">${__('Freight')}</th>
			<th class="num">${__('Total')}</th></tr></thead><tbody>${body}</tbody></table>
			<div class="muted" style="margin-top:8px;">${__('Every invoice on each distributor account (last 30 days), whoever placed the order. Read-only: nothing here posts to the books. Click a row to see its lines. A parcel that shipped to the store but was not ordered from this POS is received from Receive Goods → Incoming shipments.')}</div>`);
		$i.find('tr.order-row').on('click', (e) => {
			if ($(e.target).closest('a, button').length) return;
			$i.find(`tr.order-lines[data-i="${$(e.currentTarget).data('i')}"]`).toggle();
		});
	}

	renderOrders(orders) {
		const $o = this.$body.find('.orders');
		this.$body.find('.t-count').text(`${orders.length} ${__('order(s)')}`);
		if (!orders.length) { $o.html(`<div class="muted">${__('No orders.')}</div>`); return; }
		const esc = frappe.utils.escape_html;
		// poll_order_now is gated to the receive roles (receive_bridge.RECEIVE_ROLES).
		const canPoll = frappe.user.has_role(['Stock Manager', 'System Manager']);
		const body = orders.map((o, i) => {
			const lines = o.lines || [];
			const sum = (f) => lines.reduce((s, l) => s + (l[f] || 0), 0);
			const tracking = uniq(lines.flatMap((l) => splitList(l.tracking_numbers)));
			const invoices = uniq(lines.flatMap((l) => splitList(l.invoices)));
			const source = o.source_name
				? `<a href="/app/${frappe.router.slug(o.source_doctype)}/${encodeURIComponent(o.source_name)}" target="_blank">${esc(o.source_name)}</a>`
				: `<span class="muted">${__('Stock')}</span>`;
			const note = o.status === 'Hold' && o.hold_status ? `<br><span class="muted">${esc(o.hold_status)}</span>`
				: o.status === 'Error' && o.error ? `<br><span class="muted">${esc(o.error.slice(0, 80))}</span>` : '';
			return `<tr class="order-row" data-i="${i}">
				<td><a href="/app/distributor-order/${encodeURIComponent(o.name)}" target="_blank"><b>${esc(o.name)}</b></a>
					<br><span class="muted">${frappe.datetime.str_to_user(o.placed_at || o.creation)}</span></td>
				<td>${esc(o.distributor)}</td>
				<td>${__(o.order_type)}<br>${source}</td>
				<td><span class="indicator-pill ${STATUS_COLORS[o.status] || 'gray'}">${__(o.status)}</span>${note}</td>
				<td class="num">${sum('invoiced_qty')}/${sum('wish_qty')}<br><span class="muted">${__('received')} ${sum('received_qty')}</span></td>
				<td class="wrap">${tracking.map(esc).join('<br>') || '<span class="muted">—</span>'}</td>
				<td class="wrap">${invoices.map(esc).join('<br>') || '<span class="muted">—</span>'}</td>
				<td class="num">${fmt_money(o.total_cost)}</td>
				<td>${canPoll && POLLABLE.includes(o.status) ? `<button class="btn btn-xs btn-default btn-poll">${__('Check now')}</button>` : ''}</td>
			</tr>
			<tr class="order-lines" data-i="${i}" style="display:none;"><td colspan="9">
				<table><thead><tr><th>${__('SKU')}</th><th>${__('Description')}</th>
					<th class="num">${__('Ordered')}</th><th class="num">${__('Shipped')}</th>
					<th class="num">${__('Received')}</th><th>${__('Line Status')}</th><th>${__('Tracking')}</th></tr></thead>
				<tbody>${lines.map((l) => `<tr><td>${esc(l.sku || '')}</td><td>${esc(l.description || '')}</td>
					<td class="num">${l.wish_qty || 0}</td><td class="num">${l.invoiced_qty || 0}</td>
					<td class="num">${l.received_qty || 0}</td><td>${esc(l.line_status || '')}</td>
					<td class="wrap">${splitList(l.tracking_numbers).map(esc).join(', ')}</td></tr>`).join('')}</tbody></table>
			</td></tr>`;
		}).join('');
		$o.html(`<table><thead><tr>
			<th>${__('Order')}</th><th>${__('Distributor')}</th><th>${__('Type / Source')}</th>
			<th>${__('Status')}</th><th class="num">${__('Shipped / Ordered')}</th>
			<th>${__('Tracking')}</th><th>${__('Invoices')}</th><th class="num">${__('Total')}</th><th></th>
			</tr></thead><tbody>${body}</tbody></table>
			<div class="muted" style="margin-top:8px;">${__('Statuses refresh from each distributor every 30 minutes. Click a row to see its lines.')}</div>`);
		$o.find('tr.order-row').on('click', (e) => {
			if ($(e.target).closest('a, button').length) return;
			$o.find(`tr.order-lines[data-i="${$(e.currentTarget).data('i')}"]`).toggle();
		});
		$o.find('.btn-poll').on('click', (e) => {
			const o = orders[$(e.currentTarget).closest('tr').data('i')];
			$(e.currentTarget).prop('disabled', true);
			call('ffl_integrations.distributor.tasks.poll_order_now', { do_name: o.name })
				.then(() => { if (this.$body.find('.t-view').val() === 'orders') this.loadTrack(); });
		});
	}
}

// Statuses the 30-minute poll (and "Check now") refreshes — order_state.POLLABLE_STATES.
const POLLABLE = ['Placed', 'Acknowledged', 'Hold', 'Partially Shipped'];

// A search row from a named house carries no offers list — give it one, so the
// cart treats both shapes alike.
function normalizeRow(row) {
	if (!row.offers || !row.offers.length) {
		row.offers = [{ distributor: row.distributor, sku: row.sku, cost: row.cost, qty: row.qty || 0 }];
	}
	return row;
}

// The house to buy from: the first offer that fills the qty, else the first. The
// server sends offers already ranked (offers.buy_order, then house priority), so
// the page never re-ranks them itself.
function pickBest(offers, want) {
	return offers.find((o) => (parseInt(o.qty, 10) || 0) >= want) || offers[0];
}

function availCell(c) {
	const o = chosen(c);
	if (!o.checked) return '<span class="muted">—</span>';
	const mark = o.qty >= c.qty
		? `<span class="avail-ok">${o.qty} ✓</span>` : `<span class="avail-no">${o.qty} ✗</span>`;
	return c.switched_from
		? `${mark}<br><span class="muted">${__('Switched from {0} (short)', [frappe.utils.escape_html(c.switched_from)])}</span>`
		: mark;
}

function chosen(c) {
	return c.offers.find((o) => o.distributor === c.pick) || c.offers[0];
}

// frappe.call as a promise that resolves to the message, or null on a server error
// (frappe has already shown the error dialog). `always` settles it too: frappe
// answers some failures (deadlock, timeout, some 403s) with neither callback nor
// error, and an unsettled promise would leave the page frozen.
function call(method, args) {
	return new Promise((resolve) => {
		frappe.call({
			method, args,
			callback: (r) => resolve(r.message === undefined ? null : r.message),
			error: () => resolve(null),
			always: () => resolve(null),
		});
	});
}

function splitList(v) {
	return String(v || '').split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
}

function uniq(a) {
	return Array.from(new Set(a));
}

function fmt_money(v) {
	if (v == null || v === '') return '—';
	return format_currency(v);
}
