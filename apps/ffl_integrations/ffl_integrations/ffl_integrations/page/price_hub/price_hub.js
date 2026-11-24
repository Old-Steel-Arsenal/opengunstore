// Price Hub — look a product up, compare what each enabled distributor charges and
// has charged, see what this store paid and holds, and put it in the distributor
// order cart (public/js/distributor_cart.js, shared with Distributor Orders).
//
// Search and offers: ffl_integrations.distributor.api.search. Cost history and the
// store's record: ffl_integrations.distributor.price_hub.detail.

frappe.pages['price-hub'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __('Price Hub'), single_column: true });
	wrapper.app = new PriceHubApp(page);
};

// Back from Distributor Orders: the cart may have changed there.
frappe.pages['price-hub'].on_page_show = function (wrapper) {
	if (wrapper.app) wrapper.app.reloadCart();
};

const PERIODS = [[30, '30d'], [90, '90d'], [365, '1y'], [0, 'All']];
const SERIES_COLORS = ['var(--blue-500)', 'var(--orange-500)', 'var(--green-500)',
	'var(--purple-500)', 'var(--red-500)', 'var(--cyan-500)'];

class PriceHubApp {
	constructor(page) {
		this.page = page;
		this.rows = [];
		this.row = null;      // the selected search row
		this.detail = null;   // price_hub.detail for it
		this.days = 90;
		this.cart = ffl_integrations.cart.load();
		this.$body = $(`
			<div class="price-hub">
				<style>
					.price-hub { max-width: 1100px; margin: 0 auto; padding: 4px 16px 80px; }
					.price-hub .cols { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 20px; margin-top: 14px; }
					@media (max-width: 800px) { .price-hub .cols { grid-template-columns: minmax(0, 1fr); } }
					.price-hub .hit { display: flex; gap: 10px; align-items: center; padding: 7px 8px;
						border-radius: 6px; cursor: pointer; font-size: 13px; }
					.price-hub .hit:hover { background: var(--bg-light-gray); }
					.price-hub .hit.on { background: var(--control-bg); }
					.price-hub .hit.out { color: var(--text-muted); }
					.price-hub .img { background: var(--bg-light-gray); border-radius: 6px; flex: none; overflow: hidden; }
					.price-hub .img img { width: 100%; height: 100%; object-fit: contain; display: block; }
					.price-hub .thumb { width: 40px; height: 40px; }
					.price-hub .photo { width: 140px; height: 140px; }
					.price-hub .muted { color: var(--text-muted); font-size: 12px; }
					.price-hub .head { display: flex; gap: 16px; margin-bottom: 16px; }
					.price-hub .head h3 { margin: 0 0 2px; font-size: 17px; font-weight: 600; }
					.price-hub .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
					.price-hub .tile { background: var(--bg-light-gray); border-radius: 6px; padding: 8px 12px; }
					.price-hub .tile b { display: block; font-size: 17px; font-weight: 600; }
					.price-hub table { width: 100%; border-collapse: collapse; }
					.price-hub th, .price-hub td { padding: 7px 8px; border-bottom: 1px solid var(--border-color);
						font-size: 13px; text-align: left; vertical-align: middle; }
					.price-hub th { font-weight: 400; color: var(--text-muted); }
					.price-hub .num { text-align: right; font-variant-numeric: tabular-nums; }
					.price-hub .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
					.price-hub .best { font-size: 11px; font-weight: 600; color: var(--green-600);
						border: 1px solid var(--green-300); border-radius: 4px; padding: 0 5px; margin-left: 4px; }
					.price-hub .bar { display: flex; justify-content: space-between; align-items: center; margin: 22px 0 6px; }
					.price-hub .chart svg { width: 100%; display: block; }
					.price-hub .chart text { font-size: 11px; fill: var(--text-muted); }
					.price-hub .block { margin-top: 22px; }
				</style>
				<input type="text" class="form-control search-box" autocomplete="off"
					placeholder="${__('UPC / distributor SKU / mfg # / manufacturer / model…')}">
				<div class="cols"><div class="hits"></div><div class="product"></div></div>
			</div>`);
		$(this.page.body).append(this.$body);
		this.$body.find('.search-box').on('input', frappe.utils.debounce(() => this.search(), 300));
		this.renderCartButton();
		if (window.ffl && ffl.capabilities) ffl.capabilities.refresh().then(() => this._catalogNotice());
	}

	_catalogNotice() {
		if (!(window.ffl && ffl.capabilities && ffl.capabilities.isOff('catalog'))) return false;
		this.$body.find('.hits').html(ffl.capabilities.notice('catalog'));
		return true;
	}

	// ------------------------------------------------------------ cart

	reloadCart() {
		this.cart = ffl_integrations.cart.load();
		this.renderCartButton();
		if (this.row) this.renderProduct();
	}

	renderCartButton() {
		const n = this.cart.reduce((s, c) => s + c.qty, 0);
		this.page.set_primary_action(n ? __('Cart ({0})', [n]) : __('Cart'),
			() => frappe.set_route('distributor-restock'));
	}

	addToCart(distributor, qty) {
		ffl_integrations.cart.add(this.cart, this.row, qty, qty > 0 ? distributor : null);
		ffl_integrations.cart.save(this.cart);
		this.renderCartButton();
		this.renderProduct();
	}

	// ------------------------------------------------------------ search

	search() {
		const q = (this.$body.find('.search-box').val() || '').trim();
		if (this._catalogNotice()) return;
		if (q.length < 2) { this.$body.find('.hits').empty(); return; }
		const seq = this._searchSeq = (this._searchSeq || 0) + 1;
		frappe.call({
			method: 'ffl_integrations.distributor.api.search',
			args: { query: q, limit: 15 },
			callback: (r) => {
				if (seq !== this._searchSeq) return;   // a newer query is on its way
				this.rows = (r.message || []).filter((row) => row.offers && row.offers.length);
				this.renderHits();
			},
		});
	}

	renderHits() {
		const $h = this.$body.find('.hits');
		if (!this.rows.length) { $h.html(`<div class="muted">${__('No matches.')}</div>`); return; }
		const key = this.row && ffl_integrations.cart.keyOf(this.row);
		$h.html(this.rows.map((row, i) => {
			const inStock = row.offers.some((o) => o.qty > 0);
			return `<div class="hit ${ffl_integrations.cart.keyOf(row) === key ? 'on' : ''} ${inStock ? '' : 'out'}" data-i="${i}">
				${image(row.image_url, 'thumb')}
				<div><div>${esc(row.item_name || row.model || '')}</div>
					<div class="muted">${inStock ? __('from {0}', [money(row.offers[0].cost)]) : __('Out of stock')}</div></div>
			</div>`;
		}).join(''));
		$h.find('.hit').on('click', (e) => this.select(this.rows[$(e.currentTarget).data('i')]));
	}

	// ------------------------------------------------------------ product

	select(row) {
		this.row = row;
		this.detail = null;
		this.renderHits();
		this.renderProduct();
		this.loadDetail();
	}

	loadDetail() {
		const row = this.row;
		const first = row.offers[0];
		frappe.call({
			method: 'ffl_integrations.distributor.price_hub.detail',
			args: { upc: row.upc, sku: first.sku, distributor: first.distributor, days: this.days },
			callback: (r) => {
				if (row !== this.row) return;   // another product was picked meanwhile
				this.detail = r.message || {};
				this.renderProduct();
			},
		});
	}

	renderProduct() {
		const row = this.row;
		const d = this.detail || {};
		const series = (d.trend && d.trend.series) || [];
		const color = (name) => {
			const i = series.findIndex((s) => s.distributor === name);
			return i < 0 ? 'var(--gray-400)' : SERIES_COLORS[i % SERIES_COLORS.length];
		};
		const line = this.cart.find((c) => c.key === ffl_integrations.cart.keyOf(row));
		const offers = row.offers.map((o, i) => {
			const mine = line && line.pick === o.distributor;
			return `<tr data-house="${esc(o.distributor)}">
				<td><span class="dot" style="background:${color(o.distributor)}"></span>${esc(o.distributor)}
					${i === 0 && o.qty > 0 ? `<span class="best">${__('Best')}</span>` : ''}</td>
				<td class="num">${money(o.cost)}</td>
				<td class="num ${o.qty > 0 ? '' : 'muted'}">${o.qty}</td>
				<td class="num">${mine ? `<span class="muted">${__('{0} in cart', [line.qty])}</span>
					<button class="btn btn-xs btn-default btn-undo">${__('Undo')}</button> ` : ''}
					<button class="btn btn-xs btn-default btn-add">${__('Add')}</button></td>
			</tr>`;
		}).join('');
		const periods = PERIODS.map(([days, label]) => `<button class="btn btn-xs ${days === this.days
			? 'btn-primary' : 'btn-default'} btn-period" data-days="${days}">${__(label)}</button>`).join(' ');
		this.$body.find('.product').html(`
			<div class="head">
				${image(row.image_url, 'photo')}
				<div style="flex:1; min-width:0;">
					<h3>${esc(row.item_name || '')}</h3>
					<div class="muted" style="margin-bottom:12px;">${esc(row.upc || row.offers[0].sku || '')}</div>
					<div class="tiles">${tile(__('MAP'), money(row.map))}${tile(__('MSRP'), money(row.msrp))}</div>
				</div>
			</div>
			<table><thead><tr><th>${__('Distributor')}</th><th class="num">${__('Cost')}</th>
				<th class="num">${__('Stock')}</th><th></th></tr></thead><tbody>${offers}</tbody></table>
			<div class="bar"><span class="muted">${__('Cost history')}</span><span>${periods}</span></div>
			<div class="chart">${this.detail ? chart(series, color) : ''}</div>
			${store(d.store)}`);
		const $p = this.$body.find('.product');
		$p.find('.btn-add').on('click', (e) => this.addToCart($(e.currentTarget).closest('tr').data('house'), 1));
		$p.find('.btn-undo').on('click', () => this.addToCart(null, -1));
		$p.find('.btn-period').on('click', (e) => {
			this.days = $(e.currentTarget).data('days');
			this.detail = Object.assign({}, this.detail, { trend: null });
			this.renderProduct();
			this.loadDetail();
		});
	}
}

// What this store holds and paid: null when it has no Item for the product.
function store(s) {
	if (!s) return '';
	const paid = s.receipts.length ? s.receipts[0].rate : null;
	const margin = s.price > 0 && paid != null ? `${(((s.price - paid) / s.price) * 100).toFixed(1)}%` : '—';
	const receipts = s.receipts.map((r) => `<tr><td>${frappe.datetime.str_to_user(r.posting_date)}</td>
		<td>${esc(r.supplier || '')}</td><td class="num">${r.qty}</td><td class="num">${money(r.rate)}</td></tr>`).join('');
	return `<div class="block">
		<div class="tiles">${tile(__('On hand'), s.on_hand)}${tile(__('Last paid'), money(paid))}
			${tile(__('Our price'), s.price > 0 ? money(s.price) : '—')}${tile(__('Margin'), margin)}</div>
		${receipts ? `<table style="margin-top:12px;"><thead><tr><th>${__('Received')}</th><th>${__('From')}</th>
			<th class="num">${__('Qty')}</th><th class="num">${__('Paid')}</th></tr></thead><tbody>${receipts}</tbody></table>` : ''}
	</div>`;
}

// Step lines, one per distributor: a cost holds until the next sample; a break
// between segments is time the service did not observe.
function chart(series, color) {
	const points = series.flatMap((s) => s.segments.flat());
	if (!points.length) return `<div class="muted">${__('No cost history yet.')}</div>`;
	const W = 720, H = 190, L = 56, R = 8, T = 10, B = 24;
	const time = (p) => new Date(p.at).getTime();
	const t0 = Math.min(...points.map(time));
	const t1 = Math.max(...points.map(time), t0 + 1);
	let lo = Math.min(...points.map((p) => p.cost));
	let hi = Math.max(...points.map((p) => p.cost));
	const pad = (hi - lo) * 0.15 || Math.max(hi * 0.02, 1);
	lo -= pad; hi += pad;
	const x = (t) => (L + ((t - t0) / (t1 - t0)) * (W - L - R)).toFixed(1);
	const y = (c) => (T + ((hi - c) / (hi - lo)) * (H - T - B)).toFixed(1);
	const grid = [hi - pad, (hi + lo) / 2, lo + pad].map((c) => `
		<line x1="${L}" y1="${y(c)}" x2="${W - R}" y2="${y(c)}" stroke="var(--border-color)" stroke-width="1"/>
		<text x="${L - 6}" y="${Number(y(c)) + 4}" text-anchor="end">${money(c)}</text>`).join('');
	const lines = series.map((s) => s.segments.filter((seg) => seg.length).map((seg) => {
		const path = seg.map((p, i) => (i ? `H${x(time(p))}V${y(p.cost)}` : `M${x(time(p))} ${y(p.cost)}`)).join('');
		const end = seg[seg.length - 1];
		return `<path d="${path}" fill="none" stroke="${color(s.distributor)}" stroke-width="2"/>
			<circle cx="${x(time(end))}" cy="${y(end.cost)}" r="3" fill="${color(s.distributor)}"/>`;
	}).join('')).join('');
	const day = (t) => frappe.datetime.str_to_user(frappe.datetime.obj_to_str(new Date(t)));
	return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${__('Cost history')}">${grid}${lines}
		<text x="${L}" y="${H - 6}">${day(t0)}</text>
		<text x="${W - R}" y="${H - 6}" text-anchor="end">${day(t1)}</text></svg>`;
}

function tile(label, value) {
	return `<div class="tile"><span class="muted">${label}</span><b>${value}</b></div>`;
}

// A catalog image that fails to load leaves its grey box.
function image(url, cls) {
	return `<div class="img ${cls}">${url
		? `<img src="${esc(url)}" loading="lazy" onerror="this.remove()">` : ''}</div>`;
}

function esc(v) {
	return frappe.utils.escape_html(String(v == null ? '' : v));
}

function money(v) {
	return v == null || v === '' || Number(v) === 0 ? '—' : format_currency(v);
}
