// Price Hub — look a product up, compare what each enabled distributor charges and
// has charged, see what this store paid, holds, has on order and sells, and put it
// in the distributor order cart (public/js/distributor_cart.js, shared with
// Distributor Orders). With nothing looked up it lists what the distributors' feeds
// showed lately: costs that fell and stock running out.
//
// Search and offers: ffl_integrations.distributor.api.search. Cost history, the
// store's record and the lists: ffl_integrations.distributor.price_hub.
//
// A product has its own address (price-hub/<UPC>), so a reload, Back and a link
// from an Item all land on it.

frappe.pages['price-hub'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __('Price Hub'), single_column: true });
	wrapper.app = new PriceHubApp(page);
};

// Shown again — from Distributor Orders (the cart may have changed there), or by a
// link naming a product.
frappe.pages['price-hub'].on_page_show = function (wrapper) {
	if (wrapper.app) wrapper.app.shown();
};

const PERIODS = [[30, '30d'], [90, '90d'], [365, '1y'], [0, 'All']];
const SERIES_COLORS = ['var(--blue-500)', 'var(--orange-500)', 'var(--green-500)',
	'var(--purple-500)', 'var(--red-500)', 'var(--cyan-500)'];
const SIGNALS = [['cost_drop', 'Cost drops'], ['stockout', 'Running out'], ['fast_depletion', 'Selling fast']];

class PriceHubApp {
	constructor(page) {
		this.page = page;
		this.rows = [];
		this.at = -1;         // the highlighted search result
		this.row = null;      // the opened search row
		this.detail = null;   // price_hub.detail for it
		this.days = 90;
		this.cart = ffl_integrations.cart.load();
		this.$body = $(`
			<div class="price-hub">
				<style>
					.price-hub { max-width: 1100px; margin: 0 auto; padding: 4px 16px 80px; }
					.price-hub .cols { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 20px; margin-top: 14px; }
					@media (max-width: 800px) { .price-hub .cols { grid-template-columns: minmax(0, 1fr); } }
					/* The only result, opened: nothing to choose between, the product takes the row. */
					.price-hub .cols.solo { grid-template-columns: minmax(0, 1fr); }
					.price-hub .cols.solo .hits { display: none; }
					.price-hub .hit { display: flex; gap: 10px; align-items: center; padding: 7px 8px;
						border-radius: 6px; cursor: pointer; font-size: 13px; }
					.price-hub .hit:hover, .price-hub .hit.at { background: var(--bg-light-gray); }
					.price-hub .hit.on { background: var(--control-bg); }
					.price-hub .hit.out, .price-hub tr.out td { color: var(--text-muted); }
					.price-hub .img { background: var(--bg-light-gray); border-radius: 6px; flex: none; overflow: hidden; }
					.price-hub .img img { width: 100%; height: 100%; object-fit: contain; display: block; }
					.price-hub .thumb { width: 40px; height: 40px; }
					.price-hub .photo { width: 140px; height: 140px; }
					.price-hub .muted { color: var(--text-muted); font-size: 12px; }
					.price-hub .head { display: flex; gap: 16px; margin-bottom: 16px; }
					.price-hub .head h3 { margin: 0 0 2px; font-size: 17px; font-weight: 600; }
					.price-hub .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(104px, 1fr)); gap: 8px; }
					.price-hub .tile { background: var(--bg-light-gray); border-radius: 6px; padding: 8px 12px; }
					.price-hub .tile b { display: block; font-size: 17px; font-weight: 600; }
					.price-hub table { width: 100%; border-collapse: collapse; }
					.price-hub th, .price-hub td { padding: 7px 8px; border-bottom: 1px solid var(--border-color);
						font-size: 13px; text-align: left; vertical-align: middle; }
					.price-hub th { font-weight: 400; color: var(--text-muted); }
					.price-hub .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
					.price-hub .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
					.price-hub .tag { font-size: 11px; font-weight: 600; border: 1px solid; border-radius: 4px;
						padding: 0 5px; margin-left: 4px; white-space: nowrap; }
					.price-hub .tag.good { color: var(--green-600); border-color: var(--green-300); }
					.price-hub .tag.old { color: var(--text-muted); border-color: var(--border-color); }
					.price-hub input.qty { width: 58px; display: inline-block; height: 24px; padding: 2px 6px; text-align: right; }
					.price-hub .bar { display: flex; justify-content: space-between; align-items: center; margin: 22px 0 6px; }
					.price-hub .chart svg { width: 100%; display: block; }
					.price-hub .chart text { font-size: 11px; fill: var(--text-muted); }
					.price-hub .readout { min-height: 18px; margin-top: 2px; }
					.price-hub .readout span { margin-right: 14px; white-space: nowrap; }
					.price-hub .block { margin-top: 22px; }
					.price-hub .signals { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 24px; margin-top: 18px; }
					.price-hub .signals h4 { font-size: 13px; font-weight: 600; margin: 0 0 6px; }
					.price-hub .signals tr { cursor: pointer; }
					.price-hub .signals tr:hover td { background: var(--bg-light-gray); }
				</style>
				<input type="text" class="form-control search-box" autocomplete="off"
					placeholder="${__('UPC / distributor SKU / mfg # / manufacturer / model…')}">
				<div class="signals"></div>
				<div class="cols"><div class="hits"></div><div class="product"></div></div>
			</div>`);
		$(this.page.body).append(this.$body);
		this.$box = this.$body.find('.search-box');
		this.$box.on('input', frappe.utils.debounce(() => this.search(), 300));
		this.$box.on('keydown', (e) => this.onKey(e));
		this.renderCartButton();
		this.loadSignals();
		if (window.ffl && ffl.capabilities) ffl.capabilities.refresh().then(() => this._catalogNotice());
		this.shown();
	}

	_catalogNotice() {
		if (!(window.ffl && ffl.capabilities && ffl.capabilities.isOff('catalog'))) return false;
		this.$body.find('.signals').empty();
		this.$body.find('.hits').html(ffl.capabilities.notice('catalog'));
		return true;
	}

	shown() {
		this.cart = ffl_integrations.cart.load();
		this.renderCartButton();
		const key = frappe.get_route()[1] || '';
		if (key && (!this.row || ffl_integrations.cart.keyOf(this.row) !== key)) {
			// A product without a UPC is addressed <distributor>:<SKU>; the SKU finds it.
			this.$box.val(key.includes(':') ? key.split(':').pop() : key);
			this.search(true);
		} else if (this.row) {
			this.renderProduct();
		}
		this.$box.trigger('focus');
	}

	// ------------------------------------------------------------ cart

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

	// ------------------------------------------------------------ lists

	loadSignals() {
		frappe.call({
			method: 'ffl_integrations.distributor.price_hub.signals',
			callback: (r) => { this.signals = r.message || {}; this.renderSignals(); },
		});
	}

	// Only while nothing is looked up: the lists are where a visit starts.
	renderSignals() {
		const $s = this.$body.find('.signals');
		if (this.row || this.rows.length || (this.$box.val() || '').trim() || !this.signals) { $s.empty(); return; }
		$s.html(SIGNALS.map(([name, title]) => {
			const rows = this.signals[name] || [];
			if (!rows.length) return '';
			const body = rows.map((r, i) => `<tr data-list="${name}" data-i="${i}">
				<td>${esc(r.name || r.sku)}<div class="muted">${esc(r.distributor)}</div></td>
				<td class="num">${signalFigure(name, r)}</td></tr>`).join('');
			return `<div><h4>${__(title)}</h4><table><tbody>${body}</tbody></table></div>`;
		}).join(''));
		$s.find('tr').on('click', (e) => {
			const r = this.signals[$(e.currentTarget).data('list')][$(e.currentTarget).data('i')];
			this.$box.val(r.upc || r.sku);
			this.search(true);
		});
	}

	// ------------------------------------------------------------ search

	// open: open the result when the search comes back (Enter, a scan, a link) —
	// the only one, else the first.
	search(open) {
		const q = (this.$box.val() || '').trim();
		if (this._catalogNotice()) return;
		const seq = this._searchSeq = (this._searchSeq || 0) + 1;
		if (q.length < 2) {
			this.rows = [];
			this.row = null;
			this.$body.find('.hits, .product').empty();
			this.setRoute('');
			this.renderSignals();
			return;
		}
		frappe.call({
			method: 'ffl_integrations.distributor.api.search',
			args: { query: q, limit: 15 },
			callback: (r) => {
				if (seq !== this._searchSeq) return;   // a newer query is on its way
				this.rows = (r.message || []).filter((row) => row.offers && row.offers.length);
				this.at = -1;
				this.renderSignals();
				this.renderHits();
				if (this.rows.length && (open || this._openNext)) this.select(this.rows[0]);
				this._openNext = false;
			},
		});
	}

	onKey(e) {
		if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			if (!this.rows.length) return;
			e.preventDefault();
			this.at = (this.at + (e.key === 'ArrowDown' ? 1 : -1) + this.rows.length) % this.rows.length;
			this.renderHits();
		} else if (e.key === 'Enter') {
			e.preventDefault();
			if (this.rows[this.at]) { this.select(this.rows[this.at]); return; }
			// A scanner types the UPC and presses Enter before the search has gone out.
			this.search(true);
		}
	}

	renderHits() {
		const $h = this.$body.find('.hits');
		const key = this.row && ffl_integrations.cart.keyOf(this.row);
		// By its key, not the object: a search that comes back with the open product is new rows.
		this.$body.find('.cols').toggleClass('solo',
			this.rows.length === 1 && ffl_integrations.cart.keyOf(this.rows[0]) === key);
		if (!this.rows.length) { $h.html(`<div class="muted">${__('No matches.')}</div>`); return; }
		$h.html(this.rows.map((row, i) => {
			const inStock = row.offers.some((o) => o.qty > 0);
			return `<div class="hit ${ffl_integrations.cart.keyOf(row) === key ? 'on' : ''} ${i === this.at ? 'at' : ''}
					${inStock ? '' : 'out'}" data-i="${i}">
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
		this.qty = {};
		this.setRoute(ffl_integrations.cart.keyOf(row));
		this.renderSignals();
		this.renderHits();
		this.renderProduct();
		this.loadDetail();
	}

	setRoute(key) {
		if ((frappe.get_route()[1] || '') !== key) frappe.set_route(...['price-hub', key].filter(Boolean));
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
		const of = (name) => series.find((s) => s.distributor === name);
		const color = (name) => {
			const i = series.findIndex((s) => s.distributor === name);
			return i < 0 ? 'var(--gray-400)' : SERIES_COLORS[i % SERIES_COLORS.length];
		};
		const period = PERIODS.find(([days]) => days === this.days)[1];
		const line = this.cart.find((c) => c.key === ffl_integrations.cart.keyOf(row));
		const offers = row.offers.map((o, i) => {
			const mine = line && line.pick === o.distributor;
			const s = of(o.distributor);
			const tags = [
				i === 0 && o.qty > 0 ? `<span class="tag good">${__('Best')}</span>` : '',
				s && atLow(s) ? `<span class="tag good">${this.days ? __('{0} low', [period]) : __('Lowest')}</span>` : '',
				s && (s.state === 'stale' || s.state === 'incomplete') ? `<span class="tag old">${__('Not current')}</span>` : '',
			].join('');
			const byCase = o.units_per_case > 0 && o.case_cost > 0 && o.case_cost < o.cost
				? `<div class="muted">${__('{0} by the case of {1}', [money(o.case_cost), o.units_per_case])}</div>` : '';
			return `<tr class="${o.qty > 0 ? '' : 'out'}" data-house="${esc(o.distributor)}">
				<td style="white-space:nowrap;"><span class="dot" style="background:${color(o.distributor)}"></span>${esc(o.distributor)}
					${tags ? `<div style="margin:2px 0 0 10px;">${tags}</div>` : ''}</td>
				<td class="num">${money(o.cost)}${byCase}</td>
				<td class="num">${atMap(row.map, o.cost)}</td>
				<td class="num">${o.qty}</td>
				<td class="num">${mine ? `<span class="muted">${__('{0} in cart', [line.qty])}</span>
					<button class="btn btn-xs btn-default btn-undo">${__('Undo')}</button> ` : ''}
					<input type="number" min="1" value="${this.qty[o.distributor] || 1}" class="form-control qty">
					<button class="btn btn-xs btn-default btn-add">${__('Add')}</button></td>
			</tr>`;
		}).join('');
		const periods = PERIODS.map(([days, label]) => `<button class="btn btn-xs ${days === this.days
			? 'btn-primary' : 'btn-default'} btn-period" data-days="${days}">${__(label)}</button>`).join(' ');
		// Drawn at the width it is shown at, so its text is not scaled with it.
		const width = Math.max(480, Math.round(this.$body.find('.product').width()) || 720);
		const plot = this.detail ? chart(series, color, width) : null;
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
				<th class="num">${__('At MAP')}</th><th class="num">${__('Stock')}</th><th></th></tr></thead>
				<tbody>${offers}</tbody></table>
			<div class="bar"><span class="muted">${__('Cost history')}</span><span>${periods}</span></div>
			<div class="chart">${plot ? plot.html : ''}</div>
			<div class="readout muted"></div>
			${store(d.store)}`);
		const $p = this.$body.find('.product');
		if (plot && plot.bind) plot.bind($p.find('.chart svg'), $p.find('.readout'));
		$p.find('input.qty').on('change', (e) => {
			this.qty[$(e.currentTarget).closest('tr').data('house')] = Math.max(1, parseInt($(e.currentTarget).val(), 10) || 1);
		});
		$p.find('.btn-add').on('click', (e) => {
			const $tr = $(e.currentTarget).closest('tr');
			this.addToCart($tr.data('house'), Math.max(1, parseInt($tr.find('input.qty').val(), 10) || 1));
		});
		$p.find('.btn-undo').on('click', () => this.addToCart(null, -line.qty));
		$p.find('.btn-period').on('click', (e) => {
			this.days = $(e.currentTarget).data('days');
			this.detail = Object.assign({}, this.detail, { trend: null });
			this.renderProduct();
			this.loadDetail();
		});
	}
}

// The cost now is the lowest the window saw — and the window saw it move.
function atLow(s) {
	return s.last_cost != null && s.high_cost > s.low_cost && s.last_cost <= s.low_cost;
}

// What a unit sold at MAP leaves over its cost.
function atMap(map, cost) {
	if (!(map > 0) || !(cost > 0)) return '—';
	return `${money(map - cost)} <span class="muted">${(((map - cost) / map) * 100).toFixed(0)}%</span>`;
}

function signalFigure(name, r) {
	if (name === 'cost_drop') {
		return `${money(r.current_value)}<div class="muted">−${money(r.amount)} · ${Number(r.score).toFixed(0)}%</div>`;
	}
	if (name === 'stockout') {
		return `${Math.round(r.current_value)} ${__('left')}<div class="muted">${__('{0} days', [Number(r.score).toFixed(1)])}</div>`;
	}
	return `${Math.round(r.current_value)} ${__('left')}<div class="muted">−${Math.round(r.amount)} · ${Number(r.score).toFixed(0)}%</div>`;
}

// What this store holds, has coming, sells and paid: null when it has no Item for the product.
function store(s) {
	if (!s) return '';
	const paid = s.receipts.length ? s.receipts[0].rate : null;
	const margin = s.price > 0 && paid != null ? `${(((s.price - paid) / s.price) * 100).toFixed(1)}%` : '—';
	const receipts = s.receipts.map((r) => `<tr><td>${frappe.datetime.str_to_user(r.posting_date)}</td>
		<td>${esc(r.supplier || '')}</td><td class="num">${r.qty}</td><td class="num">${money(r.rate)}</td></tr>`).join('');
	return `<div class="block">
		<div class="tiles">${tile(__('On hand'), s.on_hand)}${tile(__('On order'), s.on_order)}
			${tile(__('Sold 30d / 90d'), `${s.sold_30} / ${s.sold_90}`)}
			${tile(__('Last paid'), money(paid))}${tile(__('Our price'), s.price > 0 ? money(s.price) : '—')}
			${tile(__('Margin'), margin)}</div>
		${receipts ? `<table style="margin-top:12px;"><thead><tr><th>${__('Received')}</th><th>${__('From')}</th>
			<th class="num">${__('Qty')}</th><th class="num">${__('Paid')}</th></tr></thead><tbody>${receipts}</tbody></table>` : ''}
	</div>`;
}

// Round gridlines for a cost range: 1, 2 or 5 times a power of ten apart, three to six of them.
function ticks(lo, hi) {
	const raw = (hi - lo) / 4 || 1;
	const pow = Math.pow(10, Math.floor(Math.log10(raw)));
	const step = [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw);
	const out = [];
	for (let v = Math.floor(lo / step) * step; v <= hi + step / 2; v += step) out.push(Number(v.toFixed(6)));
	// A cost that never moved still needs a band to be drawn in.
	if (out[out.length - 1] < hi || out.length < 2) out.push(Number((out[out.length - 1] + step).toFixed(6)));
	return out;
}

// A series' cost at a moment: the last sample at or before it, inside a segment that
// reaches it. null in a break between segments (the service did not observe then).
function costAt(s, t) {
	for (const seg of s.segments) {
		if (!seg.length || t < seg[0].t || t > seg[seg.length - 1].t) continue;
		let cost = seg[0].cost;
		for (const p of seg) { if (p.t <= t) cost = p.cost; else break; }
		return cost;
	}
	return null;
}

// Step lines, one per distributor: a cost holds until the next sample; a break
// between segments is time the service did not observe. {html, bind($svg, $readout)}:
// bind makes the pointer read out the day and each distributor's cost on it.
function chart(series, color, W) {
	const lines = series.map((s) => ({ distributor: s.distributor,
		segments: s.segments.map((seg) => seg.map((p) => ({ t: new Date(p.at).getTime(), cost: p.cost }))) }));
	const points = lines.flatMap((s) => s.segments.flat());
	if (!points.length) return { html: `<div class="muted">${__('No cost history yet.')}</div>` };
	const H = 190, L = 56, R = 8, T = 10, B = 24;
	const t0 = Math.min(...points.map((p) => p.t));
	const t1 = Math.max(...points.map((p) => p.t), t0 + 1);
	const marks = ticks(Math.min(...points.map((p) => p.cost)), Math.max(...points.map((p) => p.cost)));
	const lo = marks[0], hi = marks[marks.length - 1];
	const x = (t) => (L + ((t - t0) / (t1 - t0)) * (W - L - R)).toFixed(1);
	const y = (c) => (T + ((hi - c) / (hi - lo)) * (H - T - B)).toFixed(1);
	const grid = marks.map((c) => `
		<line x1="${L}" y1="${y(c)}" x2="${W - R}" y2="${y(c)}" stroke="var(--border-color)" stroke-width="1"/>
		<text x="${L - 6}" y="${Number(y(c)) + 4}" text-anchor="end">${money(c)}</text>`).join('');
	const paths = lines.map((s) => s.segments.filter((seg) => seg.length).map((seg) => {
		const path = seg.map((p, i) => (i ? `H${x(p.t)}V${y(p.cost)}` : `M${x(p.t)} ${y(p.cost)}`)).join('');
		const end = seg[seg.length - 1];
		return `<path d="${path}" fill="none" stroke="${color(s.distributor)}" stroke-width="2"/>
			<circle cx="${x(end.t)}" cy="${y(end.cost)}" r="3" fill="${color(s.distributor)}"/>`;
	}).join('')).join('');
	const day = (t) => frappe.datetime.str_to_user(frappe.datetime.obj_to_str(new Date(t)));
	const html = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${__('Cost history')}">${grid}${paths}
		<line class="cursor" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--text-muted)" stroke-width="1" visibility="hidden"/>
		<text x="${L}" y="${H - 6}">${day(t0)}</text>
		<text x="${W - R}" y="${H - 6}" text-anchor="end">${day(t1)}</text></svg>`;
	const bind = ($svg, $readout) => {
		const $cursor = $svg.find('.cursor');
		$svg.on('mousemove', (e) => {
			const box = $svg[0].getBoundingClientRect();
			const px = ((e.clientX - box.left) / box.width) * W;
			const t = t0 + (Math.min(Math.max(px, L), W - R) - L) / (W - L - R) * (t1 - t0);
			$cursor.attr({ x1: x(t), x2: x(t), visibility: 'visible' });
			$readout.html(`<span>${day(t)}</span>` + lines.map((s) => {
				const c = costAt(s, t);
				return `<span><span class="dot" style="background:${color(s.distributor)}"></span>${esc(s.distributor)} ${c == null ? '—' : money(c)}</span>`;
			}).join(''));
		});
		$svg.on('mouseleave', () => { $cursor.attr('visibility', 'hidden'); $readout.empty(); });
	};
	return { html, bind };
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

// The pure parts, for the Node tests (tests/js/price_hub.test.js).
if (typeof module !== 'undefined') module.exports = { ticks, costAt, atLow };
