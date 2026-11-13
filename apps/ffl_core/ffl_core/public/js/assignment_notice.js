// "Assigned to you": a person assigned a task sees it on whatever desk page they are on and says
// they saw it (server side: ffl_core/assignment_notice.py).
//
// • One dialog lists every assignment waiting (frappe.boot.ffl_assignments at first paint, then
//   assignment_notice.pending); one button acknowledges everything listed. Nothing else closes it.
// • It never interrupts: while the screen is locked or switching user (staff_switch.js's overlay)
//   or another dialog is open (a card charge, a 4473, a price approval…) it waits.
// • Delivery after first paint: the server's realtime event, a look on page change and when the
//   tab comes back (at most once a minute), and every 5 minutes for a register that stays put.
// • Another tab of this browser acknowledging closes the dialog here (BroadcastChannel).
//
// The pure half (step, due, merge, rowsHtml) is Node-tested.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.assignmentNotice = api; }
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	function esc(s) {
		return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
			return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
		});
	}

	// What to do with `rows` (what is waiting now), given the screen `s`: {open: our dialog is up,
	// locked: the lock / switch-user overlay is up, dialogs: how many other dialogs are open}.
	// 'show' | 'update' (refresh the open dialog) | 'close' | 'wait' (try again shortly) | 'none'.
	function step(rows, s) {
		var any = !!(rows && rows.length);
		if (s.open) return any ? 'update' : 'close';
		if (!any) return 'none';
		return s.locked || s.dialogs > 0 ? 'wait' : 'show';
	}

	// Whether a throttled look is due: never looked, or `gap` ms since the last one.
	function due(last, now, gap) {
		return !last || now - last >= gap;
	}

	// The server's list replaces what is shown (it is the truth: another tab may have acknowledged
	// some); `changed` says whether the dialog needs drawing again.
	function merge(shown, fresh) {
		var before = (shown || []).map(function (r) { return r.name; }).join('\n');
		var rows = fresh || [];
		return { rows: rows, changed: rows.map(function (r) { return r.name; }).join('\n') !== before };
	}

	function slug(doctype) {
		return String(doctype).toLowerCase().replace(/ /g, '-');
	}

	// The dialog's rows. opts: translate, when(row) → "5 minutes ago". No due date: Assign fills
	// today when nobody picks one, so it would read as a deadline nobody set.
	function rowsHtml(rows, opts) {
		var o = opts || {};
		var t = o.translate || function (s) { return s; };
		var when = o.when || function (r) { return r.creation; };
		return (rows || []).map(function (r) {
			var doc = r.reference_type && r.reference_name
				? '<div><a href="/app/' + esc(slug(r.reference_type)) + '/' + esc(encodeURIComponent(r.reference_name))
					+ '" target="_blank" rel="noopener">' + esc(t(r.reference_type)) + ': ' + esc(r.title || r.reference_name) + '</a></div>'
				: '';
			return '<div class="ffl-assignment" data-name="' + esc(r.name) + '" style="padding:10px 0; border-bottom:1px solid var(--border-color)">'
				+ '<div><b>' + esc(r.assigned_by) + '</b> <span class="text-muted">' + esc(when(r)) + '</span>'
				+ (r.priority === 'High' ? ' <span class="indicator-pill red">' + esc(t('High')) + '</span>' : '') + '</div>'
				+ doc
				+ (r.description ? '<div style="margin-top:4px; white-space:pre-wrap">' + esc(r.description) + '</div>' : '')
				+ '</div>';
		}).join('');
	}

	return { step: step, due: due, merge: merge, rowsHtml: rowsHtml, esc: esc };
});

(function fflAssignmentNoticeWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	var api = ffl.assignmentNotice;
	var M = 'ffl_core.assignment_notice.';
	// lazy: a look on page change / tab return at most once a minute and a 5-minute timer; push-only once realtime proves reliable.
	var GAP = 60 * 1000, TIMER = 5 * 60 * 1000;
	// lazy: a waiting dialog re-checks the screen every 3 s; hook staff_switch's unlock and dialog close if this ever costs.
	var WAIT = 3000;
	var rows = [];       // what the server last said is waiting
	var shown = [];      // what the dialog lists
	var dialog = null, open = false, lastLook = 0, waitTimer = null;
	var channel = window.BroadcastChannel ? new BroadcastChannel('ffl-assignment-notice') : null;

	function screen() {
		return {
			open: open,
			locked: !!document.querySelector('.ffl-staff-switch'),
			dialogs: (frappe.ui.open_dialogs || []).filter(function (d) { return d !== dialog && !d.is_minimized; }).length,
		};
	}

	function apply() {
		clearTimeout(waitTimer);
		var what = api.step(rows, screen());
		if (what === 'show') show();
		else if (what === 'update') draw();
		else if (what === 'close') dialog.hide();
		else if (what === 'wait') waitTimer = setTimeout(apply, WAIT);
	}

	function draw() {
		var m = api.merge(shown, rows);
		if (!m.changed && dialog.$wrapper.find('.ffl-assignment').length) return;
		shown = m.rows;
		dialog.fields_dict.list.$wrapper.html('<div style="max-height:60vh; overflow-y:auto">' + api.rowsHtml(shown, {
			translate: __,
			when: function (r) { return frappe.datetime.prettyDate(r.creation); },
		}) + '</div><div class="text-danger ffl-assignment-error" style="margin-top:8px"></div>');
	}

	var $gotIt = null;

	function make() {
		dialog = new frappe.ui.Dialog({
			title: __('Assigned to you'),
			static: true,   // no Esc, no close icon, no click outside: only the button
			fields: [{ fieldtype: 'HTML', fieldname: 'list' }],
		});
		// Not the dialog's primary action: Frappe's Ctrl+S presses the open dialog's primary button
		// (frappe.app.trigger_primary_action), which would record "seen" with nobody reading it.
		// Only a click, a tap, or tabbing to the button and pressing it counts.
		$gotIt = frappe.ui.button({ label: __('Got it'), variant: 'solid', onclick: acknowledge });
		dialog.custom_actions.append($gotIt);
		dialog.footer.removeClass('hide');
		// Hidden without the button (a page change hides any dialog): show it again if still waiting.
		dialog.onhide = function () { open = false; setTimeout(apply, 500); };
	}

	function show() {
		if (!dialog) make();
		shown = [];
		draw();
		open = true;
		dialog.show();
	}

	function acknowledge() {
		var names = shown.map(function (r) { return r.name; });
		var $err = dialog.$wrapper.find('.ffl-assignment-error').text('');
		$gotIt.prop('disabled', true);
		ffl.staffSwitch.post(M + 'acknowledge', { names: names }, __('Could not save that. Try again.')).then(function (body) {
			rows = (body && body.message) || [];
			try { if (channel) channel.postMessage({ type: 'ack', user: frappe.session.user }); } catch (e) { /* best effort */ }
			apply();
		}, function (e) {
			$err.text((e && e.message) || __('Could not save that. Try again.'));
		}).then(function () { $gotIt.prop('disabled', false); });
	}

	function look(force) {
		var now = Date.now();
		if (!force && !api.due(lastLook, now, GAP)) return;
		lastLook = now;
		ffl.staffSwitch.post(M + 'pending', {}, '').then(function (body) {
			rows = (body && body.message) || [];
			apply();
		}, function () { /* offline or signed out: the next look tries again */ });
	}

	$(document).on('app_ready', function () {
		if (!window.frappe || !frappe.boot || frappe.session.user === 'Guest') return;
		rows = frappe.boot.ffl_assignments || [];
		lastLook = Date.now();
		// After the What's New popup (app_ready + 1 s), which this then waits behind.
		setTimeout(apply, 1500);
		if (frappe.realtime && frappe.realtime.on) frappe.realtime.on('ffl_assignment', function () { look(true); });
		if (frappe.router && frappe.router.on) frappe.router.on('change', function () { look(false); });
		document.addEventListener('visibilitychange', function () { if (!document.hidden) look(false); });
		setInterval(function () { look(true); }, TIMER);
		if (channel) channel.onmessage = function (e) {
			if (e.data && e.data.type === 'ack' && e.data.user === frappe.session.user) look(true);
		};
	});
})();
