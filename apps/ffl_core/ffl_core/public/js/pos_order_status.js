// Recent Orders draft status — on every Draft row of the POS Recent Orders list,
// and in the summary a click on a draft opens: how much money is on the order
// and whether its 4473 is done. ERPNext's list carries only name, customer,
// total and date, so the counter had to open each parked order to find out.
//
// Upgrade-safe, the pos_recycle_bin.js way: ERPNext's PastOrderList and
// PastOrderSummary are wrapped on their prototypes (via ffl.installClassHook),
// never edited, and the stock HTML is returned byte-for-byte. The chips are
// added afterwards, from one batched GET per rendered page
// (ffl_core.api.pos_order_status.draft_order_status). Labels come from the pure
// mapping in pos_tender_guard.js (form4473State / draftPaymentSummary), the same
// one the cart's 4473 badge uses. Everything DOM-light lives in the API below so
// Node can pin it without a browser.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) {
		module.exports = api;                       // Node test
	} else {
		root.ffl = root.ffl || {};
		root.ffl.posOrderStatus = api;
	}
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var STATUS_METHOD = 'ffl_core.api.pos_order_status.draft_order_status';
	var DRAFT = 'Draft';
	var CHIPS_CLASS = 'ffl-order-status';          // never ffl-4473-*: the bundle's observer selects those

	function __(s, args) {
		var t = (typeof window !== 'undefined' && window.__) ? window.__(s, args) : s;
		if (args && t === s) t = s.replace(/\{(\d+)\}/g, function (_, i) { return args[+i]; });
		return t;
	}

	// The only HTML builder: `color` is one of this module's constants and `text`
	// always goes through `esc` (frappe.utils.escape_html), so the markup handed to
	// insertAdjacentHTML never carries an unescaped value.
	function pill(color, text, esc) {
		return '<span class="indicator-pill whitespace-nowrap ' + color + '" style="font-size:var(--text-xs)"><span>'
			+ esc(text) + '</span></span>';
	}

	// The confirmed-money label. `long` (the summary) names the amount on a fully
	// paid order; the list row already shows the total beside it.
	function paymentLabel(confirmed, currency, env, long) {
		if (!confirmed) return null;
		switch (confirmed.state) {
			case 'in_progress':
				// A split tender parked mid-charge: keep the card money already taken in view.
				return { color: 'blue', text: confirmed.paid > 0 && confirmed.due > 0
					? __('Card in progress · Paid {0} of {1}', [env.fmt(confirmed.paid, currency), env.fmt(confirmed.due, currency)])
					: __('Card in progress') };
			case 'partial':
				// A counter pickup ticket: the order took a deposit; say what is
				// still due, which is what the register collects at hand-over.
				if (confirmed.counter) {
					return { color: 'orange', text: __('Deposit {0} · {1} due',
						[env.fmt(confirmed.paid, currency), env.fmt(confirmed.due - confirmed.paid, currency)]) };
				}
				return { color: 'orange', text: __('Paid {0} of {1}', [env.fmt(confirmed.paid, currency), env.fmt(confirmed.due, currency)]) };
			case 'paid': return { color: 'green', text: long ? __('Paid {0}', [env.fmt(confirmed.paid, currency)]) : __('Paid') };
			default:
				if (confirmed.counter) return { color: 'orange', text: __('Unpaid · {0} due', [env.fmt(confirmed.due, currency)]) };
				return { color: 'gray', text: __('Not paid') };
		}
	}

	// The 4473 label, in the cart badge's order: a complete FastBound 4473 first,
	// then the configuration dead end, then paper mode, then the three levels.
	function form4473Label(facts, env) {
		var state = env.state(facts.form_4473_status, facts.nics_status);
		var paper = env.paperMode();
		if (state === 'complete' && !paper) return { color: 'green', text: __('4473 Complete') };
		if (!paper && !env.fastboundOn()) return { color: 'red', text: __('4473 Not Configured') };
		if (paper) {
			return state === 'complete'
				? { color: 'green', text: __('4473 Recorded') }
				: { color: 'gray', text: __('Record Paper 4473') };
		}
		if (state === 'awaiting') return { color: 'orange', text: __('Awaiting 4473') };
		return { color: 'gray', text: __('4473 not started') };
	}

	function enteredPills(summary, currency, env) {
		return summary.entered.map(function (e) {
			return pill('gray', __('{0} {1} entered · not confirmed', [e.mode, env.fmt(e.amount, currency)]), env.esc);
		});
	}

	// Chips for one list row: confirmed money, entered (unconfirmed) tender, 4473.
	function rowChipsHtml(facts, env) {
		var summary = env.summary(facts);
		var parts = [];
		var pay = paymentLabel(summary.confirmed, facts.currency, env, false);
		if (pay) parts.push(pill(pay.color, pay.text, env.esc));
		parts = parts.concat(enteredPills(summary, facts.currency, env));
		if (facts.has_firearm) {
			var f = form4473Label(facts, env);
			parts.push(pill(f.color, f.text, env.esc));
		}
		if (!parts.length) return '';
		return '<div class="' + CHIPS_CLASS + '" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">'
			+ parts.join('') + '</div>';
	}

	function removeChips(el) {
		var old = el && el.querySelector && el.querySelector('.' + CHIPS_CLASS);
		if (old && typeof old.remove === 'function') old.remove();
	}

	// ---------------------------------------------------------------- list
	// Wrap only get_invoice_html: it sees every card on every render path (status
	// change, search, refresh after a delete, a recycle-bin restore) and needs no
	// refresh_list wrap, which the recycle bin owns. ERPNext appends the cards in a
	// synchronous loop, so one deferred flush runs after the last of them.
	function applyPastOrderListPatch(L, env) {
		if (!L || L.prototype.__fflOrderStatusPatched) return false;
		var stock = L.prototype.get_invoice_html;
		if (typeof stock !== 'function') return false;
		L.prototype.get_invoice_html = function fflOrderStatusInvoiceHtml(invoice) {
			var html = stock.apply(this, arguments);
			try { queueRow(this, invoice, env); } catch (e) { /* never break ERPNext's loop */ }
			return html;
		};
		L.prototype.__fflOrderStatusPatched = true;
		return true;
	}

	function queueRow(list, invoice, env) {
		if (!invoice || invoice.doctype !== 'POS Invoice' || !invoice.name) return;
		var status = list.status_field && typeof list.status_field.get_value === 'function'
			? list.status_field.get_value() : null;
		if (status !== DRAFT) return;
		var queue = list.__fflStatusQueue || (list.__fflStatusQueue = []);
		if (queue.indexOf(invoice.name) === -1) queue.push(invoice.name);
		if (queue.length === 1) {
			env.defer(function () {
				try { flushRows(list, env); } catch (e) { list.__fflStatusQueue = []; /* the list works without chips */ }
			});
		}
	}

	function flushRows(list, env) {
		var names = list.__fflStatusQueue || [];
		list.__fflStatusQueue = [];
		if (!names.length || !env.canRead('POS Invoice')) return;
		// Capture the cards now: a newer refresh_list empties the container, which
		// detaches these, so a late answer for an old page decorates nothing.
		var container = list.$invoices_container && list.$invoices_container[0];
		var cards = [];
		if (container && typeof container.querySelectorAll === 'function') {
			var found = container.querySelectorAll('.invoice-wrapper[data-invoice-doctype="POS Invoice"]');
			for (var i = 0; i < found.length; i++) {
				if (names.indexOf(found[i].getAttribute('data-invoice-name')) !== -1) cards.push(found[i]);
			}
		}
		if (!cards.length) return;
		env.call({
			method: STATUS_METHOD, type: 'GET', args: { invoices: names }, silent: true,
			callback: function (r) { decorateRows(cards, (r && r.message) || {}, env); },
			error: function () { /* the list works without chips */ },
		});
	}

	function decorateRows(cards, factsByName, env) {
		cards.forEach(function (card) {
			if (!card.isConnected) return;
			var facts = factsByName[card.getAttribute('data-invoice-name')];
			var target = card.querySelector('.invoice-name-customer') || card;
			removeChips(target);
			if (!facts) return;
			var html = rowChipsHtml(facts, env);
			if (html) target.insertAdjacentHTML('beforeend', html);
		});
	}

	// ------------------------------------------------------------- summary
	// ERPNext renders the summary's upper section inside a promise
	// (attach_document_info), after load_summary_of returns, so the hook is on
	// get_upper_section_html. For a draft sale, the misleading paid amount (it can
	// be the payment screen's pre-fill) becomes the confirmed figure, and the
	// entered tender and the 4473 state are added under the status pill.
	function applyPastOrderSummaryPatch(S, env) {
		if (!S || S.prototype.__fflOrderStatusPatched) return false;
		var stock = S.prototype.get_upper_section_html;
		if (typeof stock !== 'function') return false;
		S.prototype.get_upper_section_html = function fflOrderStatusUpperSection(doc) {
			var html = stock.apply(this, arguments);
			try {
				if (doc && doc.doctype === 'POS Invoice' && !doc.docstatus && !doc.is_return && doc.name) {
					var summary = this;
					var name = doc.name;
					env.defer(function () {
						try { refreshSummary(summary, name, env); } catch (e) { /* the summary works without it */ }
					});
				}
			} catch (e) { /* never break the summary */ }
			return html;
		};
		S.prototype.__fflOrderStatusPatched = true;
		return true;
	}

	function refreshSummary(summary, name, env) {
		if (!env.canRead('POS Invoice')) return;
		env.call({
			method: STATUS_METHOD, type: 'GET', args: { invoices: [name] }, silent: true,
			callback: function (r) {
				var facts = r && r.message && r.message[name];
				if (!facts || !summary.doc || summary.doc.name !== name) return;   // another order is open now
				var root = summary.$upper_section && summary.$upper_section[0];
				if (!root || !root.isConnected) return;
				decorateSummary(root, facts, env);
			},
			error: function () { /* the summary works without it */ },
		});
	}

	function decorateSummary(root, facts, env) {
		var s = env.summary(facts);
		var paid = root.querySelector('.paid-amount');
		var label = paymentLabel(s.confirmed, facts.currency, env, true);
		if (paid && label) {
			paid.textContent = label.text;
			// ERPNext sizes this slot for a bare amount; a sentence at that size wraps.
			if (paid.style) paid.style.fontSize = 'var(--text-lg)';
		}
		var right = root.querySelector('.right-section') || root;
		removeChips(right);
		var parts = enteredPills(s, facts.currency, env);
		if (facts.has_firearm) {
			var f = form4473Label(facts, env);
			parts.push(pill(f.color, f.text, env.esc));
		}
		if (parts.length) {
			right.insertAdjacentHTML('beforeend', '<div class="' + CHIPS_CLASS
				+ '" style="display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-end;margin-top:4px">'
				+ parts.join('') + '</div>');
		}
	}

	return {
		STATUS_METHOD: STATUS_METHOD,
		paymentLabel: paymentLabel,
		form4473Label: form4473Label,
		rowChipsHtml: rowChipsHtml,
		applyPastOrderListPatch: applyPastOrderListPatch,
		applyPastOrderSummaryPatch: applyPastOrderSummaryPatch,
		decorateSummary: decorateSummary,
	};
});

// Browser wiring: trap PastOrderList and PastOrderSummary the moment ERPNext's
// lazy POS bundle assigns them (ffl.installClassHook keeps every patch on the
// same key — the recycle bin also wraps PastOrderList), with the same bounded
// fallback retry pos_recycle_bin.js uses.
(function fflOrderStatusWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	function sw() { return (window.ffl && ffl.switches) || null; }
	var env = {
		call: function (opts) { return frappe.call(opts); },
		esc: function (s) { return frappe.utils.escape_html(s == null ? '' : String(s)); },
		fmt: function (v, c) { return format_currency(v, c) || String(v); },
		defer: function (fn) { setTimeout(fn, 0); },
		canRead: function (dt) { return !!(frappe.model && typeof frappe.model.can_read === 'function' && frappe.model.can_read(dt)); },
		paperMode: function () { var s = sw(); return s ? s.paper4473Mode() : false; },
		fastboundOn: function () { var s = sw(); return s ? s.fastboundEnabled() : true; },
		state: function (f, n) { return ffl.form4473State(f, n); },
		summary: function (facts) {
			var s = sw();
			return ffl.draftPaymentSummary(facts, {
				cardMop: s ? s.cardMop() : 'Credit Card',
				cardProcessorOn: s ? s.cardProcessorEnabled() : true,
			});
		},
	};
	var patches = {
		PastOrderList: function (C) { ffl.posOrderStatus.applyPastOrderListPatch(C, env); },
		PastOrderSummary: function (C) { ffl.posOrderStatus.applyPastOrderSummaryPatch(C, env); },
	};
	function fallbackRetry(key, left) {
		var ns = window.erpnext && erpnext.PointOfSale;
		if (ns && ns[key]) { patches[key](ns[key]); return; }
		if (left > 0) setTimeout(function () { fallbackRetry(key, left - 1); }, 250);
	}
	function wire() {
		if (!window.frappe || typeof frappe.provide !== 'function') return;
		if (!window.ffl || typeof ffl.form4473State !== 'function') return;   // pos_tender_guard.js not loaded
		frappe.provide('erpnext.PointOfSale');
		Object.keys(patches).forEach(function (key) {
			var hook = window.ffl && ffl.installClassHook;
			if (hook && hook(erpnext.PointOfSale, key, patches[key])) return;
			fallbackRetry(key, 60);
		});
	}
	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
	else wire();
})();
