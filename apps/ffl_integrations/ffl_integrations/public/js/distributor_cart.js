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
			// A live check belongs to the visit that ran it.
			lines.forEach((c) => { c.switched_from = ''; c.offers.forEach((o) => { delete o.checked; }); });
			return lines;
		} catch (e) {
			return [];
		}
	},

	save(lines) {
		try { localStorage.setItem(this._key(), JSON.stringify(lines)); } catch (e) { /* storage off: the cart lasts the visit */ }
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
		if (pick) { line.pick = pick; line.manual = true; }
		if (!line.manual) line.pick = this.pickBest(line.offers, line.qty).distributor;
	},
};
