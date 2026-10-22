// Pure, dependency-free tender-preservation logic for the POS resume flow.
// Shared by the POS bundle (loadInvoiceIntoCart + the edit_order monkey-patch,
// via window.ffl) and unit-tested under Node. NO DOM, NO Frappe, NO POS
// controller internals — only stable POS Invoice docfields — so ERPNext upgrades
// that rename controller methods can't break the decision.
//
// Why: ERPNext's Recent Orders → Edit runs POS Invoice.reset_mode_of_payments,
// which rebuilds `payments` from the POS Profile and zeroes paid_amount. That is
// correct for an unpaid draft, but a firearm order is HELD (already charged)
// while the 4473/NICS clears — resetting silently drops the recorded card/Zelle
// split (the saved 855.17+584.55 split was
// replaced by a profile-default 1,439.72 card row, tripping the false
// "Charged amount mismatch" and requiring the tender be re-entered by hand).
(function (factory) {
	'use strict';
	var api = factory();
	if (typeof module !== 'undefined' && module.exports) module.exports = api;   // Node test
	if (typeof window !== 'undefined') {                                          // desk POS
		window.ffl = window.ffl || {};
		window.ffl.hasLiveTender = api.hasLiveTender;
		window.ffl.editOrderResumeSteps = api.editOrderResumeSteps;
		window.ffl.installClassHook = api.installClassHook;
		window.ffl.applyHeldTenderCheckoutGuard = api.applyHeldTenderCheckoutGuard;
		window.ffl.isFullyTendered = api.isFullyTendered;
		window.ffl.balanceDue = api.balanceDue;
		window.ffl.hasHeldPayment = api.hasHeldPayment;
		window.ffl.submitButtonState = api.submitButtonState;
		window.ffl.form4473State = api.form4473State;
		window.ffl.draftPaymentSummary = api.draftPaymentSummary;
		window.ffl.lockedRowFix = api.lockedRowFix;
		window.ffl.isLockedLine = api.isLockedLine;
		window.ffl.negativeTicketState = api.negativeTicketState;
		window.ffl.negativeTicketFix = api.negativeTicketFix;
		window.ffl.payoutMethodFields = api.payoutMethodFields;
		window.ffl.payoutSummary = api.payoutSummary;
		window.ffl.PAYOUT_METHODS = api.PAYOUT_METHODS;
		window.ffl.payoutProblem = api.payoutProblem;
		window.ffl.pickupTicketState = api.pickupTicketState;
	}
})(function () {
	'use strict';

	function flt(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }

	// True when a resumed Draft POS Invoice carries tender we must NOT discard.
	// Two independent signals (either is sufficient):
	//   1. card_charged_amount > 0 — a live (un-voided) card capture. void_pos_invoice
	//      clears card_charged_amount to 0 (its Currency column is NOT NULL, so
	//      never null), so > 0 is exactly an un-voided capture (full CHARGED or a
	//      re-chargeable PARTIAL).
	//   2. any `payments` row with a non-zero amount — a recorded Cash/Zelle split
	//      (reset_mode_of_payments would replace these rows with zeroed defaults).
	// Signal 2 is intentionally broad (the store's stated rule: "any non-zero payment
	// row → don't reset"). If a POS flow ever seeds the default mode-of-payment with
	// the grand total on an UNPAID held draft, that draft is preserved rather than
	// re-defaulted — harmless (the saved amount matches the saved cart, and on_cart_update
	// recomputes on edit), and erring toward preserve is the safe direction here.
	function hasLiveTender(doc) {
		doc = doc || {};
		if (flt(doc.card_charged_amount) > 0) return true;
		var rows = doc.payments || [];
		for (var i = 0; i < rows.length; i++) {
			if (rows[i] && flt(rows[i].amount) !== 0) return true;
		}
		return false;
	}

	// The Recent Orders → Edit resume sequence, extracted so the doctype/name arg
	// threading and the "skip reset iff live tender" rule are unit-tested (the
	// monkey-patch that installs this into ERPNext's PastOrderSummary events is
	// browser-only and can't be unit-tested). Mirrors ERPNext pos_controller.js
	// edit_order for the pinned v16: make_invoice_frm(doctype) →
	// sync_draft_invoice_to_frm(doctype, name) → frm.refresh(name) →
	// [reset_mode_of_payments UNLESS the held order carries a live tender] →
	// cart.load_invoice() → toggle_components(true). Re-verify the step list against
	// ERPNext's edit_order on any ERPNext version bump.
	function editOrderResumeSteps(controller, doctype, name) {
		return [
			function () { return controller.make_invoice_frm(doctype); },
			function () { return controller.sync_draft_invoice_to_frm(doctype, name); },
			function () { return controller.frm.refresh(name); },
			function () {
				return hasLiveTender(controller.frm.doc)
					? Promise.resolve()                              // keep the held tender
					: controller.frm.call('reset_mode_of_payments'); // vanilla: re-default
			},
			function () { return controller.cart.load_invoice(); },
			function () { return controller.toggle_components(true); },
		];
	}

	// Deterministically run applyPatch(ns[key]) the moment ERPNext's POS class named
	// `key` is available on the namespace object — WITHOUT a finite retry window.
	// ERPNext does `frappe.provide("erpnext.PointOfSale")` then plain assignments
	// `erpnext.PointOfSale.Controller = class {...}` (pos_controller.js:1) and
	// `erpnext.PointOfSale.Payment = class {...}` (pos_payment.js:2) lazily when the POS
	// page loads. The old bounded tryPatch raced that lazy load and, once its window
	// elapsed with no further router-change, never wrapped the prototype — leaving the
	// tender guards dead code. A setter trap on the shared, frappe.provide'd
	// namespace object fires exactly when ns[key] is assigned, whatever the load order.
	//   ns        — the erpnext.PointOfSale namespace object (must already exist;
	//               callers frappe.provide it first so the trap survives ERPNext's
	//               own idempotent frappe.provide).
	//   key       — the class name to trap ('Controller' or 'Payment').
	//   applyPatch(Class) — idempotent prototype wrapper.
	// Returns true if engaged (immediate patch or trap installed); false if it could
	// not (falsy ns, or defineProperty rejected) so the caller can fall back.
	// Several patches may trap the SAME key (the recycle bin and the draft-status
	// chips both wrap PastOrderList): the setter keeps a hook list and runs every
	// hook, each in its own try/catch. Re-defining the property per call would
	// silently replace the earlier trap, leaving that patch dead.
	function installClassHook(ns, key, applyPatch) {
		if (!ns) return false;
		if (ns[key]) {                             // warm: already defined → patch now
			try { applyPatch(ns[key]); } catch (e) { /* never break POS boot */ }
			return true;
		}
		var existing = Object.getOwnPropertyDescriptor(ns, key);
		if (existing && existing.set && existing.set.__fflHooks) {
			existing.set.__fflHooks.push(applyPatch); // another patch already trapped this key
			return true;
		}
		try {                                       // lazy: trap the future assignment
			var current;
			var hooks = [applyPatch];
			var setter = function (value) {
				current = value;
				for (var i = 0; i < hooks.length; i++) {
					try { hooks[i](value); } catch (e) { /* never break the assignment */ }
				}
			};
			setter.__fflHooks = hooks;
			Object.defineProperty(ns, key, {
				configurable: true,
				enumerable: true,
				get: function () { return current; },
				set: setter,
			});
			return true;
		} catch (e) {
			return false;                           // caller degrades to a bounded retry
		}
	}

	// Entering the payment screen runs ERPNext set_default_payment
	// (taxes_and_totals.js:1071), which zeroes every non-default payment row when the
	// POS Profile's set_grand_total_to_default_mop is on. A held/paid order's preserved
	// split has no default (Cash) row, so ALL rows get zeroed and paid_amount -> 0.
	// Disabling frm.set_default_payment for a live-tender order makes that guard skip,
	// so the recorded split survives to the payment screen (semantically correct: a
	// held order with an entered split must not auto-fill the default MOP with the
	// grand total). Returns whether it acted.
	function applyHeldTenderCheckoutGuard(frm) {
		if (!frm || !hasLiveTender(frm.doc)) return false;
		frm.set_default_payment = 0;
		return true;
	}

	// Fully tendered: the recorded tender, plus any write-off within the profile's
	// limit, covers the order — the server's payment_mode.require_full_payment
	// rule, applied here BEFORE we park or submit the draft.
	// rounded_total wins when the POS Profile rounds; TOLERANCE mirrors
	// card_charges.js — half a cent, never a dependency on that module.
	var TOLERANCE = 0.005;
	function balanceDue(doc) {
		doc = doc || {};
		var due = flt(doc.rounded_total) || flt(doc.grand_total);
		return due - flt(doc.paid_amount) - flt(doc.write_off_amount);
	}
	function isFullyTendered(doc) {
		return balanceDue(doc) <= TOLERANCE;
	}

	// True when this draft's tender has actually been taken IN FULL and the
	// order parked for the 4473 — the ffl_payment_held stamp the Complete Order
	// wrapper writes only after the charge loop finished and isFullyTendered
	// held. Deliberately NOT card_charged_amount (> 0 after the FIRST card of a
	// multi-card split, while card_status is still PARTIAL and the balance is
	// owed) and NOT hasLiveTender (true for the default-MOP row ERPNext pre-fills
	// with the grand total when the payment screen opens). A manager's card void
	// clears the stamp server-side: the order is no longer fully tendered.
	function hasHeldPayment(doc) {
		doc = doc || {};
		return !!parseInt(doc.ffl_payment_held, 10);
	}

	// What the POS payment screen's primary button should say for the current
	// cart. Under the charge-before-4473 flow the SAME click does different things
	// depending on the 4473 state, and a button labelled "Complete Order" that
	// merely takes the money (and then parks the order) misleads the counter:
	//   no firearm / return / 4473 cleared → complete (vanilla ERPNext label)
	//   firearm, 4473 pending, nothing held → pay   ("Take Payment")
	//   firearm, 4473 pending, fully paid  → held  ("Paid — waiting for 4473", quiet)
	// `held` is NEVER disabled: the click still runs the submit wrapper, which
	// re-checks the 4473 on the server (a page that missed the webhook nudge
	// self-heals on click — a past incident) and resumes a
	// multi-card charge loop if the balance is somehow still owed. `quiet` is a
	// visual hint only. Pure: hasFirearm / cleared need a server round-trip,
	// so the caller resolves them. Labels are keyed, not literal — the bundle
	// maps keys to __() strings so Frappe's extractor sees them.
	function submitButtonState(doc, ctx) {
		doc = doc || {};
		ctx = ctx || {};
		if (doc.is_return || !ctx.hasFirearm || ctx.cleared) {
			return { key: 'complete', quiet: false };
		}
		// A negative ticket takes no money — nothing is "taken" or "held": the
		// store pays the customer when the order completes, after the 4473.
		if (isNegativeTicket(doc)) return { key: 'wait', quiet: false };
		if (hasHeldPayment(doc)) return { key: 'held', quiet: true };
		return { key: 'pay', quiet: false };
	}

	// The 4473 state the counter reads, in the store's three levels (2026-09-13):
	//   complete    — form Completed AND NICS Proceed, the submit gate's own rule
	//   awaiting    — a 4473 is under way but not cleared: Pending, or Completed
	//                 with any other NICS result (Delayed / Denied / Cancelled)
	//   not_started — anything else (N/A or blank)
	// One home for the rule: the cart badge and the Recent Orders chips both ask
	// here, so the two can never disagree about the same draft.
	function form4473State(form, nics) {
		if (form === 'Completed' && nics === 'Proceed') return 'complete';
		if (form === 'Pending' || form === 'Completed') return 'awaiting';
		return 'not_started';
	}

	// What a parked draft really has on it, from the facts
	// ffl_core.api.pos_order_status.draft_order_status returns:
	//   confirmed — money the POS can prove. A card instruction still on the
	//     terminal is `in_progress`. A held order (fully tendered when it was
	//     parked) counts its recorded tender, which the tender guard preserves;
	//     a cart that grew after the hold reads `partial`. Otherwise only captured
	//     card money counts (card_charged_amount). null when nothing is due.
	//     A web-order pickup ticket (`paid_online`) was paid on WooCommerce
	//     before it reached the counter; its recorded tender counts the same way.
	//   entered — the other tender typed on a draft that was never held (cash,
	//     Zelle…), shown as NOT confirmed: ERPNext pre-fills the default payment
	//     method with the grand total when the payment screen opens, so a typed
	//     amount is not proof of payment. The integrated card tender is represented by
	//     `confirmed` and left out, unless no card processor is on (a standalone terminal
	//     leaves only the typed row).
	// opts: { cardMop: the card Mode of Payment name, cardProcessorOn: bool }.
	function draftPaymentSummary(facts, opts) {
		facts = facts || {};
		opts = opts || {};
		if (facts.counter_order) {
			// A counter pickup's 4473 ticket carries no money — its ORDER does.
			// The confirmed state is the order's: what has been collected against
			// the Sales Invoice (a deposit on the terminal, Record Payment) versus
			// its total. The ticket's own $0 rows are never "entered".
			var total = flt(facts.counter_order.total);
			var owed = flt(facts.counter_order.outstanding);
			var got = Math.max(total - owed, 0);
			return {
				confirmed: total > 0 ? {
					state: owed <= TOLERANCE ? 'paid' : (got > 0 ? 'partial' : 'none'),
					paid: got, due: total, counter: true,
				} : null,
				entered: [],
			};
		}
		var due = flt(facts.due);
		var charged = flt(facts.charged);
		var held = hasHeldPayment({ ffl_payment_held: facts.held }) || !!facts.paid_online;
		var confirmed = null;
		if (facts.card_status === 'IN_PROGRESS') {
			confirmed = { state: 'in_progress', paid: charged, due: due };
		} else if (due > 0) {
			var paid = held ? Math.max(flt(facts.paid_amount), charged) : charged;
			var state = paid <= 0 ? 'none' : (paid + TOLERANCE >= due ? 'paid' : 'partial');
			confirmed = { state: state, paid: paid, due: due };
		}
		var entered = [];
		if (!held) {
			var cardMop = opts.cardProcessorOn === false ? null : (opts.cardMop || 'Credit Card');
			var byMode = {};
			(facts.payments || []).forEach(function (p) {
				var amount = p ? flt(p.amount) : 0;
				if (amount <= 0 || !p.mode_of_payment || p.mode_of_payment === cardMop) return;
				if (!(p.mode_of_payment in byMode)) {
					byMode[p.mode_of_payment] = { mode: p.mode_of_payment, amount: 0 };
					entered.push(byMode[p.mode_of_payment]);
				}
				byMode[p.mode_of_payment].amount += amount;
			});
		}
		return { confirmed: confirmed, entered: entered };
	}

	// A LOCKED line (a trade-in credit) carries
	// ffl_locked_rate — the amount the server pinned it to (compliance/
	// locked_lines.py); its rate is never a price. ERPNext re-prices rows from
	// many client paths (price-list / pricing-rule responses write rate =
	// price_list_rate whenever they land, other-item rules, every
	// price_list_rate trigger recomputes rate = plr + margin - discount) and
	// derives a margin whenever rate > price_list_rate — on prod the credit
	// item's auto-inserted Item Price (-869) turned a -180 credit into +180
	//. Whatever moved the row, put it back. Returns
	// null when the row is not locked or already exact, else
	//   { silent: {…}, rate }: `silent` are pricing fields to assign WITHOUT
	// triggers (price_list_rate 0, no margin / discount — set through
	// set_value they would fire ERPNext's handlers and re-derive rate from
	// the stale list price); `rate` is the one value to set through
	// frappe.model.set_value (ERPNext's own rate handler re-totals), or
	// undefined when rate is already right.
	// price_list_rate is 0, not the credit: with 0 neither client nor server
	// derives a margin/discount from it, pricing rules leave rate alone, and
	// POS's add-to-cart matching (item_code + uom + price_list_rate === rate)
	// never merges a second same-value trade-in into this row.
	function cents(v) { return Math.round(flt(v) * 100) / 100; }
	function lockedRowFix(row) {
		if (!row) return null;
		// 0 is "no lock": a locked amount is never 0, and rows that predate the
		// field carry the column's 0 default.
		var locked = flt(row.ffl_locked_rate);
		if (!locked) return null;
		var silent = {};
		if (cents(row.price_list_rate) !== 0) silent.price_list_rate = 0;
		if (row.margin_type) silent.margin_type = '';
		if (cents(row.margin_rate_or_amount) !== 0) silent.margin_rate_or_amount = 0;
		if (cents(row.discount_percentage) !== 0) silent.discount_percentage = 0;
		if (cents(row.discount_amount) !== 0) silent.discount_amount = 0;
		var want = cents(locked);
		var rate = cents(row.rate) !== want ? want : undefined;
		if (rate === undefined && !Object.keys(silent).length) return null;
		return { silent: silent, rate: rate };
	}

	// The locked line items (mirror of setup/locked_lines.locked_items():
	// setup/trade_in_credit TRADE_IN_CREDIT_ITEM, setup/gunsmith DEPOSIT_ITEM). Their
	// lines come from their own flow only — the Trade-in dialog, a work order.
	var LOCKED_ITEMS = ['TRADE-IN-CREDIT', 'GUNSMITH-DEPOSIT'];

	// Is this cart item / row a locked line? By its code (a tile tap carries no
	// lock yet) or by the lock it carries.
	function isLockedLine(item) {
		if (!item) return false;
		return LOCKED_ITEMS.indexOf(item.item_code) !== -1 || flt(item.ffl_locked_rate) !== 0;
	}

	// ---- Negative ticket: trade-ins exceed the goods, the store pays the customer ----
	// The server lets a ticket go below zero only then (compliance/trade_in_credit.
	// negative_trade_in_ok) and books the payout at submit (cash_drawer.
	// record_trade_in_payout) from these two header fields. Mirror of
	// setup/trade_in_credit.TRADE_IN_PAYOUT_METHODS. Everything here is pure: the
	// bundle draws the screen and writes the fields, this decides what is valid.
	var PAYOUT_METHODS = ['Cash', 'Zelle', 'Check', 'ACH'];
	var DEFAULT_PAYOUT_METHOD = 'Cash';   // fewest clicks: the common case is preselected
	var F_METHOD = 'ffl_trade_in_payout_method';
	var F_REF = 'ffl_trade_in_payout_reference';

	var CREDIT_ITEM = 'TRADE-IN-CREDIT';   // mirror of setup/trade_in_credit.TRADE_IN_CREDIT_ITEM

	// What the ticket comes to, as the server reads it (negative_trade_in.invoice_total): the rounded total when
	// the POS Profile rounds, else the grand total. The payout is the negative of this.
	function ticketTotal(doc) { return flt(doc.rounded_total) || flt(doc.grand_total); }

	// Client mirror of compliance/trade_in_credit.negative_trade_in_ok: below zero AND only trade-in credit takes it
	// there — not a return; a credit row on the ticket; no other row negative (only a credit may make a
	// ticket negative); no header discount; shortfall no more than the credit. A cart that is negative for any other
	// reason gets the ordinary screen, so the cashier meets the server's real refusal instead of a payout screen the
	// server will not honour.
	function isNegativeTicket(doc) {
		doc = doc || {};
		if (parseInt(doc.is_return, 10) || !(ticketTotal(doc) < -TOLERANCE)) return false;
		var credit = 0, hasCredit = false;
		var rows = doc.items || [];
		for (var i = 0; i < rows.length; i++) {
			var r = rows[i] || {};
			if (r.item_code === CREDIT_ITEM) { hasCredit = true; credit += -flt(r.amount); }
			else if (flt(r.amount) < 0) return false;
		}
		if (!hasCredit || flt(doc.discount_amount)) return false;
		return -ticketTotal(doc) <= credit + 0.005;
	}

	function trim(v) { return String(v == null ? '' : v).replace(/^\s+|\s+$/g, ''); }

	// The one client check of how the store paid someone (server: cash_drawer.check_payout_method): '' when it will
	// be taken, 'method' when the method is not one of `methods` (the server's list where the page has it, else this
	// file's mirror), 'reference' when a bank method (anything but Cash) names no reference. Used by the register's
	// negative ticket, the Receive Goods "Pay the seller" dialog and the Cash Drawer page.
	function payoutProblem(method, reference, methods) {
		if ((methods || PAYOUT_METHODS).indexOf(method) === -1) return 'method';
		if (method !== DEFAULT_PAYOUT_METHOD && !trim(reference)) return 'reference';
		return '';
	}

	// What the payment screen shows and enforces for the current cart.
	//   negative      — show "Store pays customer" instead of the tender tiles
	//   due           — dollars the store pays (the shortfall, cents)
	//   needsReference— every method but Cash leaves a trace in the bank, so it
	//                   must name it (check number / Zelle confirmation / ACH ref)
	//   problem       — why Complete must stay blocked, or null:
	//     'card'      a card was already charged on this order (the server refuses
	//                 a non-zero tender here; a manager voids it first)
	//     'method'    no valid payout method picked
	//     'reference' non-Cash method without a reference
	function negativeTicketState(doc) {
		doc = doc || {};
		var negative = isNegativeTicket(doc);
		var method = trim(doc[F_METHOD]);
		var reference = trim(doc[F_REF]);
		var state = {
			negative: negative, due: negative ? cents(-ticketTotal(doc)) : 0,
			method: method, reference: reference,
			needsReference: negative && PAYOUT_METHODS.indexOf(method) !== -1 && method !== DEFAULT_PAYOUT_METHOD,
			problem: null, ok: true,
		};
		if (!negative) return state;
		if (flt(doc.card_charged_amount) > 0) state.problem = 'card';
		else state.problem = payoutProblem(method, reference) || null;
		state.ok = !state.problem;
		return state;
	}

	// The writes that bring the doc in line with its cart, or null when it is.
	//   set      — header fields: Cash preselected on a negative ticket; both
	//              cleared once the cart is positive again (a stale payout must not
	//              ride onto a normal sale)
	//   zeroRows — names of payment rows that still carry money: a negative ticket
	//              has no tender (server guard), and ERPNext's default-payment fill
	//              does not always clear a row typed before the credit went in
	function negativeTicketFix(doc) {
		doc = doc || {};
		var set = {};
		var method = trim(doc[F_METHOD]);
		if (isNegativeTicket(doc)) {
			if (PAYOUT_METHODS.indexOf(method) === -1) set[F_METHOD] = DEFAULT_PAYOUT_METHOD;
		} else {
			if (doc[F_METHOD]) set[F_METHOD] = '';
			if (doc[F_REF]) set[F_REF] = '';
		}
		var zeroRows = [];
		// A card already captured on the draft is real money: never zero its row away (that would hide the
		// captured tender and let the cashier believe it was never taken) — the 'card' problem blocks instead.
		if (isNegativeTicket(doc) && !(flt(doc.card_charged_amount) > 0)) {
			(doc.payments || []).forEach(function (r) {
				if (r && flt(r.amount) !== 0) zeroRows.push(r.name);
			});
		}
		if (!Object.keys(set).length && !zeroRows.length) return null;
		return { set: set, zeroRows: zeroRows };
	}

	// Field writes for picking a payout method. A reference typed for Zelle must
	// not ride along as a Cash payout's (nor as another method's): switching clears it.
	function payoutMethodFields(doc, method) {
		var out = {};
		out[F_METHOD] = PAYOUT_METHODS.indexOf(method) === -1 ? '' : method;
		if (trim((doc || {})[F_METHOD]) !== out[F_METHOD]) out[F_REF] = '';
		return out;
	}

	// What a SUBMITTED negative ticket paid the customer, for the order summary and anywhere else that
	// would otherwise read paid_amount (0 by design) as "nothing was paid". null for anything else.
	function payoutSummary(doc) {
		if (!doc || parseInt(doc.docstatus, 10) !== 1 || !isNegativeTicket(doc)) return null;
		var method = trim(doc[F_METHOD]);
		var reference = trim(doc[F_REF]);
		return {
			due: cents(-ticketTotal(doc)), method: method, reference: reference,
			detail: method ? (reference ? method + ', ' + reference : method) : '',
		};
	}

	// A counter pickup's 4473 ticket (POS Invoice.ffl_counter_order): the money is on the order it hands over, so
	// the ticket never takes a tender. null for anything else (a return of a ticket mirrors the ticket); else the
	// order and whether THIS copy carries money — the register re-applies the price list when it loads a draft —
	// which a save hands to the server to pin back to $0 (counter_pickup.keep_ticket_free).
	function pickupTicketState(doc) {
		doc = doc || {};
		var order = trim(doc.ffl_counter_order);
		if (!order || parseInt(doc.is_return, 10)) return null;
		var money = !!(flt(doc.grand_total) || flt(doc.rounded_total) || flt(doc.paid_amount) || flt(doc.change_amount));
		(doc.items || []).forEach(function (r) { if (flt((r || {}).rate)) money = true; });
		(doc.payments || []).forEach(function (p) { if (flt((p || {}).amount)) money = true; });
		return { order: order, carriesMoney: money };
	}

	return {
		PAYOUT_METHODS: PAYOUT_METHODS,
		pickupTicketState: pickupTicketState,
		payoutProblem: payoutProblem,
		payoutSummary: payoutSummary,
		isNegativeTicket: isNegativeTicket,
		negativeTicketState: negativeTicketState,
		negativeTicketFix: negativeTicketFix,
		payoutMethodFields: payoutMethodFields,
		isLockedLine: isLockedLine,
		lockedRowFix: lockedRowFix,
		isFullyTendered: isFullyTendered,
		balanceDue: balanceDue,
		hasHeldPayment: hasHeldPayment,
		submitButtonState: submitButtonState,
		hasLiveTender: hasLiveTender,
		editOrderResumeSteps: editOrderResumeSteps,
		installClassHook: installClassHook,
		applyHeldTenderCheckoutGuard: applyHeldTenderCheckoutGuard,
		form4473State: form4473State,
		draftPaymentSummary: draftPaymentSummary,
	};
});
