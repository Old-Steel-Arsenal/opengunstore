// The distributor order cart, shared by the Distributor Orders and Price Hub pages
// (hooks.page_js). It lives in this browser, one per signed-in user.
// lazy: one browser only; store it on the server to follow the user across devices.
//
// A line: {key, upc, item_name, item_code, map, msrp, ffl_required, is_firearm, qty,
//  offers: [{distributor, sku, cost, qty}], pick: distributor, manual: bool}
frappe.provide('ffl_integrations');

ffl_integrations.cart = {
	_key() { return `ffl_distributor_cart:${frappe.session.user}`; },

	load() {
		try {
			const lines = JSON.parse(localStorage.getItem(this._key()) || '[]');
			lines.forEach((c) => c.offers.length);   // not a cart: read as empty
			return lines;
		} catch (e) {
			return [];
		}
	},

	// A live check (offers[].checked, switched_from) belongs to the page that ran it and is
	// not stored: what is stored then changes only when the cart does, so a page that
	// reloads on `watch` and saves again does not set the other pages reloading in turn.
	save(lines) {
		const text = JSON.stringify(lines, (k, v) => (k === 'checked' || k === 'switched_from' ? undefined : v));
		try { localStorage.setItem(this._key(), text); } catch (e) { /* storage off: the cart lasts the visit */ }
	},

	// Call fn when another tab or window changed the cart, so a page never writes
	// its own older copy over it.
	watch(fn) {
		window.addEventListener('storage', (e) => { if (e.key === this._key()) fn(); });
	},

	keyOf(row) { return row.upc || `${row.offers[0].distributor}:${row.offers[0].sku}`; },

	// The house to buy from: the first offer that fills the qty, else the first. The
	// server sends offers already ranked (offers.buy_order, then house priority).
	pickBest(offers, want) {
		return offers.find((o) => (parseInt(o.qty, 10) || 0) >= want) || offers[0];
	},

	// Add qty of a search row to lines (negative takes it back; a line at 0 leaves).
	// pick: the house the buyer chose; without one the line follows the best offer.
	add(lines, row, qty, pick) {
		const key = this.keyOf(row);
		let line = lines.find((c) => c.key === key);
		if (!line) {
			if (qty <= 0) return;
			line = {
				key, upc: row.upc, item_name: row.item_name, item_code: row.item_code || '',
				map: row.map, msrp: row.msrp, ffl_required: row.ffl_required,
				is_firearm: row.is_firearm, qty: 0, offers: row.offers, manual: false,
			};
			lines.push(line);
		}
		line.qty += qty;
		if (line.qty <= 0) { lines.splice(lines.indexOf(line), 1); return; }
		if (qty > 0 && line.offers !== row.offers) {
			// The row is the catalog now, the line may be days old: its offers replace the
			// line's own for the same house (ranked first), and a house it does not name stays.
			const fresh = new Set(row.offers.map((o) => o.distributor));
			line.offers = row.offers.concat(line.offers.filter((o) => !fresh.has(o.distributor)));
			line.map = row.map;
			line.msrp = row.msrp;
		}
		if (pick) { line.pick = pick; line.manual = true; }
		if (!line.manual) line.pick = this.pickBest(line.offers, line.qty).distributor;
	},
};
