// Pure, dependency-free multi-card charge math for the POS card flow.
// Shared by the POS bundle (multi-card charge loop) and the desk form's
// per-charge void picker (pos_invoice.js), and unit-tested under Node — same
// pattern as pos_tender_guard.js: NO DOM, NO Frappe, only stable POS Invoice
// docfields, so the invariants stay testable outside a bench.
(function (factory) {
	'use strict';
	var api = factory();
	if (typeof module !== 'undefined' && module.exports) module.exports = api;   // Node test
	if (typeof window !== 'undefined') {                                          // desk POS
		window.ffl = window.ffl || {};
		window.ffl.cardCharges = api;
	}
})(function () {
	'use strict';

	function flt(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }

	// Cent-level tolerance for money comparisons — the repo-wide convention
	// (create_order.js, pending_dispositions.js, GL tests all use 0.005).
	var TOLERANCE = 0.005;

	// Rows that hold live captured money. VOIDED/DECLINED/CANCELED rows never
	// count; a row-less doc (legacy single-stamp era) yields [].
	function liveChargeRows(doc) {
		return ((doc && doc.card_charges) || []).filter(function (r) {
			return r && r.status === 'CHARGED';
		});
	}

	function sumLiveCharges(doc) {
		return liveChargeRows(doc).reduce(function (s, r) { return s + flt(r.amount); }, 0);
	}

	// remaining to collect on cards; negative ⇒ over-captured (cart edited
	// down after money moved — the mismatch case).
	function remainingCardBalance(target, doc) {
		return round2(flt(target) - sumLiveCharges(doc));
	}

	// Is `amount` a valid next slice given what remains? (> 0, ≤ remaining.)
	function isValidSlice(amount, remaining) {
		var a = flt(amount);
		return a > 0 && a <= flt(remaining) + TOLERANCE;
	}

	function round2(v) { return Math.round(flt(v) * 100) / 100; }

	// Payment screen on an order that already has captured card money: the
	// Credit Card tile holds the amount to charge NOW, while the tender row
	// keeps the card TOTAL, so ERPNext's paid/remaining/change maths and the
	// charge loop (remaining = tender − captured) stay as they are (incident
	// A past invoice: $712.25 on file, 1,000 typed, terminal asked 287.75).
	// ERPNext's own row-resync handler reads the tile as the row amount
	// (get_value() vs row.amount, then set_value(row.amount)); it gets this
	// facade, which speaks card totals, so it neither fights the tile nor adds
	// the captured money twice. `real` is the tile's control (amount now);
	// `charged()` reads the live captured total each time.
	function cardTotalFacade(real, charged) {
		return {
			__fflReal: real,
			get _label() { return real._label; },
			get df() { return real.df; },
			get value() { return real.value; },
			get_value: function () { return round2(charged() + flt(real.get_value())); },
			set_value: function (total) { return real.set_value(round2(flt(total) - charged())); },
			toggle_label: function (show) { return real.toggle_label && real.toggle_label(show); },
		};
	}

	// Wire the tile's control: its onchange writes captured + now as the card
	// tender (o.writeTotal). Inert until arm(), which the tile click calls —
	// ERPNext's render queues set_value(card total) on the control and its
	// resync handler may push totals at it, so until the counter picks the tile
	// its value is in neither unit; arm() re-derives it from the tender row
	// (the source of truth) first. No timing assumption about ERPNext's chain.
	//   o: { real, charged(), tenderTotal(), writeTotal(total), show(now)? }
	// Returns { facade, arm }; the facade is what ERPNext's own code gets.
	function chargeNowControl(o) {
		var armed = false;
		o.real.df.onchange = function () {
			if (!armed) return;
			var now = flt(this.value);
			var total = round2(o.charged() + now);
			if (o.tenderTotal() !== total) o.writeTotal(total);
			if (o.show) o.show(now);
		};
		return {
			facade: cardTotalFacade(o.real, o.charged),
			arm: function () {
				o.real.set_input(round2(o.tenderTotal() - o.charged()));
				armed = true;
			},
		};
	}

	// REFUND LOCK sentinels — the two values the server's _lock_and_throw
	// stamps into card_refund_reference (mirror of ffl_integrations
	// payments/api.py REFUND_LOCK_*). Anything else in that field is a real
	// gateway refund reference (or the legacy "UNKNOWN"/"MULTI" success
	// markers) — a COMPLETED refund, never a lock.
	var REFUND_LOCK_UNKNOWN = 'UNKNOWN-OUTCOME';
	var REFUND_LOCK_PARTIAL_PREFIX = 'PARTIAL:';

	function isRefundLock(value) {
		var v = String(value || '');
		return v === REFUND_LOCK_UNKNOWN || v.indexOf(REFUND_LOCK_PARTIAL_PREFIX) === 0;
	}

	// Only a zero-booked lock may be unlocked for retry: a PARTIAL:* lock is
	// the ONE record of how much this return already pushed back — clearing
	// it re-arms a full-amount retry that the per-original-card caps cannot
	// bound (the double-refund). PARTIAL residue
	// goes to back-office manual processing instead.
	function isUnlockableLock(value) {
		return String(value || '') === REFUND_LOCK_UNKNOWN;
	}

	// Void picker: label/value must not round-trip through display text (the
	// gateway ref could contain any separator). Options are 1-based indexed
	// labels; resolve maps the picked label back to the row's unique_reference.
	function buildVoidPicker(rows) {
		var live = (rows || []).filter(function (r) { return r && r.status === 'CHARGED'; });
		var labels = live.map(function (r, i) {
			var amt = flt(r.amount).toFixed(2);
			return (i + 1) + ' · ' + (r.card_summary || 'Card') + ' $' + amt;
		});
		return {
			labels: labels,
			resolve: function (label) {
				var i = parseInt(String(label || '').split(' · ')[0], 10);
				return (i >= 1 && i <= live.length) ? (live[i - 1].unique_reference || '') : '';
			},
		};
	}

	// Confirm Portal Refund applies when the processor that charged the original
	// sale cannot send its refund: no processor is switched on, or the one that is
	// on is another (the store has moved on). chargedBy: the charge row's processor
	// code ('' = recorded before rows named one, which was Payroc); active:
	// frappe.boot.ffl_card_processor. An absent boot key reads as "on" — the
	// behaviour before the switch existed — so no button; the server re-checks.
	var LEGACY_PROCESSOR = 'payroc';
	function chargingProcessorOff(chargedBy, active) {
		if (!active) return false;
		if (!Number(active.enabled)) return true;
		return (chargedBy || LEGACY_PROCESSOR) !== active.code;
	}

	return {
		TOLERANCE: TOLERANCE,
		chargingProcessorOff: chargingProcessorOff,
		REFUND_LOCK_UNKNOWN: REFUND_LOCK_UNKNOWN,
		REFUND_LOCK_PARTIAL_PREFIX: REFUND_LOCK_PARTIAL_PREFIX,
		isRefundLock: isRefundLock,
		isUnlockableLock: isUnlockableLock,
		liveChargeRows: liveChargeRows,
		sumLiveCharges: sumLiveCharges,
		remainingCardBalance: remainingCardBalance,
		isValidSlice: isValidSlice,
		round2: round2,
		cardTotalFacade: cardTotalFacade,
		chargeNowControl: chargeNowControl,
		buildVoidPicker: buildVoidPicker,
	};
});
