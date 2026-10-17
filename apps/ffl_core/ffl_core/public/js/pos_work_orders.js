// Gunsmith work orders at the register — a "Work Orders" entry in the Recent Orders
// status filter that lists the shop's open jobs (ffl_core.gunsmith.register_work_orders)
// with their customer, gun, status and what is paid, and what the counter does with
// one: Take payment (any amount towards it, into a cart) and Complete (confirm the
// charges, then the pick-up goes into the cart), each a normal checkout. The actions
// live in the bundle (ffl.gunsmithRegister), next to the Work Order intake button,
// because they drive the live cart.
//
// Upgrade-safe, the pos_recycle_bin.js way: ERPNext's PastOrderList is wrapped on
// its prototype (via ffl.installClassHook), never edited; every other status falls
// through to the refresh_list that was there before (ERPNext's, or the recycle
// bin's). Everything DOM-free lives in the pure API below so Node can pin it.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) {
		module.exports = api;                       // Node test
	} else {
		root.ffl = root.ffl || {};
		root.ffl.posWorkOrders = api;
	}
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var WORK_ORDERS = 'Work Orders';
	var LIST_METHOD = 'ffl_core.gunsmith.register_work_orders';
	var STATUS_COLOR = { 'Received': 'gray', 'In Progress': 'blue', 'Ready': 'green' };

	function __(s, args) {
		var t = (typeof window !== 'undefined' && window.__) ? window.__(s, args) : s;
		if (args && t === s) t = s.replace(/\{(\d+)\}/g, function (_, i) { return args[+i]; });
		return t;
	}
	function flt(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }

	function withWorkOrdersOption(options) {
		var list = typeof options === 'string' ? options.split('\n') : (options || []).slice();
		if (list.indexOf(WORK_ORDERS) === -1) list.push(WORK_ORDERS);
		return list.join('\n');
	}

	// What a job's card offers, on every open job: Take payment, and Complete (its open
	// pick-up draft is opened instead of confirming the charges again).
	function cardActions(row) {
		var drafts = row.drafts || {};
		return [{ action: 'payment', label: __('Take payment') },
			{ action: 'complete', label: drafts.pickup ? __('Open pick-up') : __('Complete'), primary: true }];
	}

	// The cart holds another customer's sale: a job's payment must not land on it.
	function cartConflict(doc, customer) {
		return !!(doc && (doc.items || []).length && doc.customer && doc.customer !== customer);
	}

	// Money already held for the job (it comes off the bill at pick-up), or nothing.
	function depositText(row, fmt) {
		return flt(row.deposit_held) > 0 ? __('Deposit held {0}', [fmt(row.deposit_held)]) : '';
	}

	// The Complete dialog's starting charges: the job's own, else one line of the
	// store's labor item at the estimate (the clerk edits or removes it).
	function defaultCharges(row, laborItem) {
		var lines = (row.lines || []).map(function (l) {
			return { item_code: l.item_code, description: l.description || '', qty: flt(l.qty) || 1, rate: flt(l.rate) };
		});
		if (!lines.length && laborItem) lines.push({ item_code: laborItem, description: '', qty: 1, rate: flt(row.estimated_cost) });
		return lines;
	}

	// One card per open job. Class names mirror ERPNext's get_invoice_html (v16) so the
	// stock card CSS applies; EVERYTHING interpolated goes through esc.
	function cardHtml(row, env) {
		var esc = env.esc;
		var gun = [row.manufacturer, row.model].filter(Boolean).join(' ');
		var serial = row.serial_number ? __('SN {0}', [row.serial_number]) : '';
		var deposit = depositText(row, function (v) { return env.fmt(v); });
		var buttons = cardActions(row).map(function (a) {
			return '<button type="button" class="btn btn-xs ' + (a.primary ? 'btn-primary' : 'btn-default')
				+ ' ffl-wo-action" data-action="' + esc(a.action) + '" style="margin-left:4px">' + esc(a.label) + '</button>';
		}).join('');
		return '<div class="invoice-wrapper ffl-work-order" data-work-order="' + esc(row.name) + '">'
			+ '<div class="invoice-name-customer">'
			+ '<div class="invoice-customer">' + esc(env.ellipsis(row.customer_name || row.customer || '', 24)) + '</div>'
			+ '<div class="invoice-name">' + esc(row.name) + ' · ' + esc(gun) + (serial ? ' · ' + esc(serial) : '') + '</div>'
			+ (deposit ? '<div class="invoice-date text-muted small">' + esc(deposit) + '</div>' : '')
			+ '</div>'
			+ '<div class="invoice-total-date" style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">'
			+ '<span class="indicator-pill whitespace-nowrap ' + (STATUS_COLOR[row.status] || 'gray') + '"><span>'
			+ esc(__(row.status)) + '</span></span>'
			+ '<div>' + buttons + '</div>'
			+ '</div>'
			+ '</div><div class="seperator"></div>';
	}

	function emptyHtml(esc) {
		return '<div class="no-item-wrapper text-muted" style="padding:var(--padding-md)">'
			+ esc(__('No open work orders.')) + '</div>';
	}

	function applyPastOrderListPatch(L, env) {
		if (!L || L.prototype.__fflWorkOrdersPatched) return false;
		var stockMakeFilter = L.prototype.make_filter_section;
		var stockRefresh = L.prototype.refresh_list;
		if (typeof stockMakeFilter !== 'function' || typeof stockRefresh !== 'function') return false;

		L.prototype.make_filter_section = function fflWorkOrdersFilterSection() {
			stockMakeFilter.apply(this, arguments);
			var sf = this.status_field;
			if (sf && sf.df) {
				sf.df.options = withWorkOrdersOption(sf.df.options);
				if (typeof sf.refresh === 'function') sf.refresh();
			}
			bindActions(this, env);
		};

		L.prototype.refresh_list = function fflWorkOrdersRefreshList() {
			var status = this.status_field && this.status_field.get_value();
			if (status !== WORK_ORDERS) return stockRefresh.apply(this, arguments);
			var me = this;
			if (me.events && typeof me.events.reset_summary === 'function') me.events.reset_summary();
			var search_term = me.search_field ? me.search_field.get_value() : '';
			me.$invoices_container.html('');
			return env.call({
				method: LIST_METHOD, freeze: true, args: { search_term: search_term },
				callback: function (r) {
					var out = (r && r.message) || {};
					var rows = out.orders || [];
					me.__fflWorkOrders = { labor_item: out.labor_item, rows: rows };
					if (!rows.length) { me.$invoices_container.append(emptyHtml(env.esc)); return; }
					rows.forEach(function (row) { me.$invoices_container.append(cardHtml(row, env)); });
				},
			});
		};

		L.prototype.__fflWorkOrdersPatched = true;
		return true;
	}

	// A card's own click must not reach ERPNext's `.invoice-wrapper` handler (it would
	// open a POS Invoice of that name); its buttons run the register actions.
	function bindActions(list, env) {
		if (!list.$invoices_container || list.__fflWorkOrdersBound) return;
		list.__fflWorkOrdersBound = true;
		list.$invoices_container.on('click', '.ffl-work-order', function (e) {
			e.stopImmediatePropagation();
		});
		list.$invoices_container.on('click', '.ffl-wo-action', function (e) {
			e.stopImmediatePropagation();
			var $btn = env.$(this);
			var name = $btn.closest('.ffl-work-order').attr('data-work-order');
			runAction(list, env, $btn.attr('data-action'), name);
		});
	}

	function runAction(list, env, action, name) {
		var state = list.__fflWorkOrders || {};
		var row = (state.rows || []).filter(function (r) { return r.name === name; })[0];
		if (!row) return false;
		if (action === 'payment') return env.takePayment(row);
		if (action === 'complete') return env.complete(row, state.labor_item);
		return false;
	}

	return {
		WORK_ORDERS: WORK_ORDERS,
		LIST_METHOD: LIST_METHOD,
		withWorkOrdersOption: withWorkOrdersOption,
		cardActions: cardActions,
		cartConflict: cartConflict,
		depositText: depositText,
		defaultCharges: defaultCharges,
		cardHtml: cardHtml,
		applyPastOrderListPatch: applyPastOrderListPatch,
		runAction: runAction,
	};
});

// Browser wiring: trap erpnext.PointOfSale.PastOrderList the instant ERPNext's lazy
// POS bundle assigns it (ffl.installClassHook from pos_tender_guard.js), with the
// same bounded fallback retry the recycle bin uses.
(function fflWorkOrdersWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	function register() { return (window.ffl && ffl.gunsmithRegister) || null; }
	var env = {
		call: function (opts) { return frappe.call(opts); },
		esc: function (s) { return frappe.utils.escape_html(s == null ? '' : String(s)); },
		fmt: function (v) { return format_currency(v, frappe.defaults.get_default('currency')) || String(v); },
		ellipsis: function (s, n) { return frappe.ellipsis(s, n); },
		$: function (el) { return $(el); },
		takePayment: function (row) { var g = register(); return g && g.takePayment(row); },
		complete: function (row, laborItem) { var g = register(); return g && g.complete(row, laborItem); },
	};
	function apply(L) { ffl.posWorkOrders.applyPastOrderListPatch(L, env); }
	function fallbackRetry(left) {
		var ns = window.erpnext && erpnext.PointOfSale;
		if (ns && ns.PastOrderList) { apply(ns.PastOrderList); return; }
		if (left > 0) setTimeout(function () { fallbackRetry(left - 1); }, 250);
	}
	function wire() {
		if (!window.frappe || typeof frappe.provide !== 'function') return;
		frappe.provide('erpnext.PointOfSale');
		var hook = window.ffl && ffl.installClassHook;
		if (hook && hook(erpnext.PointOfSale, 'PastOrderList', apply)) return;
		fallbackRetry(60);
	}
	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
	else wire();
})();
