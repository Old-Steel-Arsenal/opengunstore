// Switch staff at a shared counter screen, and lock it when nobody is there
// (server side: ffl_core/staff_switch.py).
//
// • The user menu (the name at the sidebar's bottom-left corner, and the dock's avatar)
//   gets a "Switch user" entry. It opens the staff list (everyone with a counter role); pick someone, type their
//   password, and they are signed in through frappe's own /api/method/login (a new
//   session; the page reloads as them). Picking yourself just closes the list.
// • After FFL Settings → Lock Idle Screen After (minutes) with no touch or key
//   (frappe.boot.ffl_counter_lock_minutes, 0 = never) the screen locks: the same
//   list, no way out but a password — yours unlocks, anyone else's signs them in.
//   The lock lives on the server session, so a reload comes back locked
//   (frappe.boot.ffl_screen_locked), and every other desk tab of the browser locks,
//   unlocks and follows a switch of user with it (BroadcastChannel).
//
// • Someone with a counter PIN (Set my PIN, in the list) is picked with the PIN, or with
//   the password through "Use password instead"; five wrong PINs turn it off until
//   their password is used (server side). A PIN switch is a real sign-in too
//   (staff_switch.switch_with_pin). A Sales / System Manager can clear anyone's PIN.
//
// The pure half (idleMs, initials, pickAction, withMenuItem, authMode, pinProblem,
// serverMessage) is Node-tested.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else { root.ffl = root.ffl || {}; root.ffl.staffSwitch = api; }
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	// Minutes from the setting → milliseconds, or 0 for "never lock".
	function idleMs(minutes) {
		var m = parseInt(minutes, 10);
		return m > 0 ? m * 60 * 1000 : 0;
	}

	// How long until the screen may lock, given the latest activity seen in ANY tab of this
	// browser (`lastActive`, ms epoch; 0 = unknown): 0 means lock now. Each tab runs its own
	// timer, so an idle tab must not lock the one being used (the lock reaches every tab).
	function msUntilLock(lastActive, now, ms) {
		if (!ms) return Infinity;
		var since = lastActive > 0 && lastActive <= now ? now - lastActive : ms;
		return Math.max(0, ms - since);
	}

	// "Jane Roe" → "JR" for a user without a picture.
	function initials(name) {
		var parts = String(name || '').trim().split(/\s+/).filter(Boolean);
		if (!parts.length) return '?';
		return ((parts[0][0] || '') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
	}

	// What picking `user` does: 'close' (yourself, not locked), 'unlock' (yourself,
	// locked: your password), 'login' (someone else: their password).
	function pickAction(user, currentUser, locked) {
		if (user === currentUser) return locked ? 'unlock' : 'close';
		return 'login';
	}

	// The user menu's option groups with `item` added at the top of the first group
	// (once). Groups are frappe.ui.Dropdown's [{group, options: [...]}].
	function withMenuItem(groups, item) {
		if (!Array.isArray(groups) || !groups.length || !Array.isArray(groups[0].options)) return groups;
		if (groups[0].options.some(function (o) { return o && o.name === item.name; })) return groups;
		var first = Object.assign({}, groups[0], { options: [item].concat(groups[0].options) });
		return [first].concat(groups.slice(1));
	}

	// How a picked person proves it is them: their PIN when they have one that is not
	// switched off, unless the clerk chose the password; else the password.
	function authMode(user, usePassword) {
		return user && user.has_pin && !user.pin_off && !usePassword ? 'pin' : 'password';
	}

	// Why `pin` cannot be a counter PIN, or null (mirror of staff_switch.pin_problem):
	// 4–6 digits, not one digit repeated, not a run up or down, typed the same twice.
	function pinProblem(pin, confirm) {
		pin = String(pin == null ? '' : pin).trim();
		if (!/^[0-9]{4,6}$/.test(pin)) return 'format';
		var steps = {};
		for (var i = 1; i < pin.length; i++) steps[pin.charCodeAt(i) - pin.charCodeAt(i - 1)] = 1;
		var kinds = Object.keys(steps);
		if (kinds.length === 1 && ['0', '1', '-1'].indexOf(kinds[0]) !== -1) return 'trivial';
		if (confirm !== undefined && String(confirm == null ? '' : confirm).trim() !== pin) return 'mismatch';
		return null;
	}

	// The message frappe sent with a refused call (its _server_messages: a JSON list of
	// JSON-encoded {message}), tags stripped; null when there is none.
	function serverMessage(body) {
		try {
			var list = JSON.parse((body && body._server_messages) || '[]');
			for (var i = 0; i < list.length; i++) {
				var m = typeof list[i] === 'string' ? JSON.parse(list[i]) : list[i];
				var text = String((m && m.message) || '').replace(/<[^>]*>/g, '').trim();
				if (text) return text;
			}
		} catch (e) { /* not frappe's shape */ }
		return null;
	}

	return { idleMs: idleMs, msUntilLock: msUntilLock, initials: initials, pickAction: pickAction, withMenuItem: withMenuItem,
		authMode: authMode, pinProblem: pinProblem, serverMessage: serverMessage };
});

(function fflStaffSwitchWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	var api = ffl.staffSwitch;
	var M = 'ffl_core.staff_switch.';
	var esc = function (s) { return frappe.utils.escape_html(s == null ? '' : String(s)); };
	var overlay = null;
	// Every desk tab of this browser shares the session: lock, unlock and a switch of
	// user reach them all, or the tab next door would be a way round the lock.
	var channel = window.BroadcastChannel ? new BroadcastChannel('ffl-staff-switch') : null;
	function tell(type) { try { if (channel) channel.postMessage({ type: type, user: frappe.session.user }); } catch (e) { /* best effort */ } }

	function close() {
		if (overlay) { overlay.remove(); overlay = null; }
	}

	// A plain POST: frappe.call's error pop-ups would sit under the overlay, so the
	// overlay shows the error itself.
	function post(method, args) {
		return fetch('/api/method/' + method, {
			method: 'POST', credentials: 'same-origin',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token },
			body: JSON.stringify(args || {}),
		}).then(function (r) {
			return r.json().catch(function () { return {}; }).then(function (body) {
				if (r.status === 429) throw new Error(__('Too many tries. Wait a few minutes.'));
				if (!r.ok) throw new Error(api.serverMessage(body) || __('That password is not right.'));
				return body;
			});
		});
	}

	function login(user, password) {
		return post('login', { usr: user, pwd: password }).then(function (body) {
			if (body.verification || body.tmp_id) throw new Error(__('This user signs in with a second step; sign in from the login page.'));
			return body;
		});
	}

	// The staff list: over everything, so a lock covers dialogs too.
	function show(opts) {
		var locked = !!(opts && opts.locked);
		close();
		overlay = $('<div class="ffl-staff-switch" role="dialog" aria-modal="true"></div>').css({
			position: 'fixed', inset: 0, zIndex: 3000, background: 'rgba(17,24,39,0.82)',
			display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '16px',
		}).appendTo(document.body);
		var $card = $('<div></div>').css({
			background: 'var(--card-bg, #fff)', borderRadius: '12px', padding: '20px', width: 'min(640px, 100%)',
			maxHeight: '90vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,.3)',
		}).appendTo(overlay);
		$card.append('<h3 style="margin:0 0 4px">' + (locked ? __('Screen locked') : __('Switch user')) + '</h3>'
			+ '<p class="text-muted" style="margin:0 0 12px">' + (locked
				? __('Pick yourself and type your PIN or password to carry on, or pick someone else to sign in as them.')
				: __('Pick who is at the counter and type their PIN or password.')) + '</p>'
			+ '<div class="ffl-staff-list" style="display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:10px"></div>'
			+ '<div class="ffl-staff-auth" style="margin-top:14px" hidden>'
			+ '<div class="ffl-staff-who" style="font-weight:600; margin-bottom:6px"></div>'
			+ '<input type="password" class="form-control ffl-staff-pwd" autocomplete="current-password" placeholder="' + esc(__('Password')) + '">'
			+ '<div class="text-danger small ffl-staff-err" style="margin-top:6px"></div>'
			+ '<div style="display:flex; gap:8px; align-items:center; margin-top:10px"><button class="btn btn-primary ffl-staff-go">' + __('Continue') + '</button>'
			+ '<a href="#" class="small ffl-staff-use-pwd" hidden>' + esc(__('Use password instead')) + '</a></div></div>'
			+ (locked ? '' : '<div class="ffl-staff-pin-form" style="margin-top:14px" hidden>'
				+ '<div style="font-weight:600; margin-bottom:6px">' + esc(__('Set my PIN')) + '</div>'
				+ '<input type="password" class="form-control ffl-pin-pwd" autocomplete="current-password" placeholder="' + esc(__('Your password')) + '">'
				+ '<input type="password" class="form-control ffl-pin-new" inputmode="numeric" autocomplete="off" maxlength="6" style="margin-top:6px" placeholder="' + esc(__('New PIN (4 to 6 digits)')) + '">'
				+ '<input type="password" class="form-control ffl-pin-again" inputmode="numeric" autocomplete="off" maxlength="6" style="margin-top:6px" placeholder="' + esc(__('New PIN again')) + '">'
				+ '<div class="text-danger small ffl-pin-err" style="margin-top:6px"></div>'
				+ '<button class="btn btn-primary btn-sm ffl-pin-save" style="margin-top:6px">' + esc(__('Save PIN')) + '</button></div>'
				+ '<div style="display:flex; justify-content:space-between; margin-top:14px"><button class="btn btn-default ffl-staff-set-pin">' + esc(__('Set my PIN')) + '</button>'
				+ '<button class="btn btn-default ffl-staff-cancel">' + __('Cancel') + '</button></div>'));
		var picked = null;
		var mode = 'password';
		var manager = frappe.user.has_role(['Sales Manager', 'System Manager']);
		var $list = $card.find('.ffl-staff-list');
		var $auth = $card.find('.ffl-staff-auth');
		var $pwd = $card.find('.ffl-staff-pwd');
		var $err = $card.find('.ffl-staff-err');

		frappe.call({ method: M + 'counter_users' }).then(function (r) {
			(r.message || []).forEach(function (u) {
				var face = u.image
					? '<img src="' + esc(u.image) + '" alt="" style="width:48px; height:48px; border-radius:50%; object-fit:cover">'
					: '<div style="width:48px; height:48px; border-radius:50%; background:var(--gray-200,#e5e7eb); display:flex; align-items:center; justify-content:center; font-weight:600">' + esc(api.initials(u.full_name)) + '</div>';
				$('<button type="button" class="btn btn-default ffl-staff-user"></button>')
					.css({ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px', padding: '12px 8px', height: 'auto', whiteSpace: 'normal' })
					.html(face + '<span>' + esc(u.full_name) + '</span>' + (u.current ? '<span class="small text-muted">' + __('signed in') + '</span>' : '')
						+ (manager && !locked && u.has_pin ? '<span class="small ffl-staff-clear-pin" role="button" style="text-decoration:underline">' + esc(__('Clear PIN')) + '</span>' : ''))
					.appendTo($list)
					.on('click', function (e) {
						if ($(e.target).closest('.ffl-staff-clear-pin').length) {
							e.stopPropagation();
							if (window.confirm(__('Clear the counter PIN of {0}? They sign in with their password until they set a new one.', [u.full_name]))) {
								post(M + 'clear_pin', { user: u.user }).then(function () { show(opts); }, function (err) { $err.text(err.message); });
							}
							return;
						}
						picked = u;
						var action = api.pickAction(u.user, frappe.session.user, locked);
						if (action === 'close') { close(); return; }
						$list.find('.ffl-staff-user').removeClass('btn-primary').addClass('btn-default');
						$(this).removeClass('btn-default').addClass('btn-primary');
						$card.find('.ffl-staff-who').text(u.full_name);
						setMode(api.authMode(u, false));
						$auth.prop('hidden', false);
						$pwd.trigger('focus');
					});
			});
		});

		function setMode(m) {
			mode = m;
			$pwd.val('').attr({ placeholder: m === 'pin' ? __('PIN') : __('Password'), inputmode: m === 'pin' ? 'numeric' : null,
				autocomplete: m === 'pin' ? 'off' : 'current-password' });
			$card.find('.ffl-staff-use-pwd').prop('hidden', m !== 'pin');
			$err.text('');
		}
		$card.on('click', '.ffl-staff-use-pwd', function (e) { e.preventDefault(); setMode('password'); $pwd.trigger('focus'); });

		var go = function () {
			if (!picked) return;
			var pwd = $pwd.val();
			if (!pwd) { $err.text(mode === 'pin' ? __('Type the PIN.') : __('Type the password.')); return; }
			$err.text('');
			var action = api.pickAction(picked.user, frappe.session.user, locked);
			var secret = mode === 'pin' ? { pin: pwd } : { password: pwd };
			var done = action === 'unlock'
				? post(M + 'unlock_screen', secret).then(function () { close(); resetIdle(); tell('unlock'); })
				: (mode === 'pin' ? post(M + 'switch_with_pin', { user: picked.user, pin: pwd }) : login(picked.user, pwd))
					.then(function () { tell('switch'); location.reload(); });
			done.catch(function (e) {
				$err.text((e && e.message) || __('That password is not right.'));
				$pwd.val('').trigger('focus');
			});
		};

		$card.on('click', '.ffl-staff-set-pin', function () {
			$card.find('.ffl-staff-pin-form').prop('hidden', false);
			$card.find('.ffl-pin-pwd').trigger('focus');
		});
		$card.on('click', '.ffl-pin-save', function () {
			var $perr = $card.find('.ffl-pin-err');
			var pin = $card.find('.ffl-pin-new').val();
			var again = $card.find('.ffl-pin-again').val();
			var problem = api.pinProblem(pin, again);
			var why = { format: __('A PIN is 4 to 6 digits.'), trivial: __('That PIN is too easy to guess. Pick another.'),
				mismatch: __('The two PINs are not the same.') };
			if (problem) { $perr.text(why[problem]); return; }
			post(M + 'set_pin', { password: $card.find('.ffl-pin-pwd').val(), pin: pin, confirm: again }).then(function () {
				frappe.show_alert({ message: __('Your counter PIN is set.'), indicator: 'green' });
				show(opts);
			}, function (err) { $perr.text(err.message); });
		});
		$card.on('click', '.ffl-staff-go', go);
		$pwd.on('keydown', function (e) { if (e.key === 'Enter') go(); });
		$card.on('click', '.ffl-staff-cancel', close);
		if (!locked) overlay.on('keydown', function (e) { if (e.key === 'Escape') close(); });
	}

	// ---- idle lock -------------------------------------------------------

	// Activity is shared by every desk tab of this browser (localStorage), so a tab left
	// open in the background never locks the one at work: its timer checks the shared
	// time first and waits on. Per-browser convenience only — the lock itself is on the
	// server session.
	var ACTIVE_KEY = 'ffl-staff-last-active';
	function lastActive() {
		try { return parseInt(window.localStorage.getItem(ACTIVE_KEY), 10) || 0; } catch (e) { return 0; }
	}
	function markActive(now) {
		try { window.localStorage.setItem(ACTIVE_KEY, String(now)); } catch (e) { /* private window: this tab only */ }
	}

	var idleTimer = null;
	var lastPoke = 0;
	function lock() {
		post(M + 'lock_screen').then(function () { show({ locked: true }); }, function () { show({ locked: true }); });
		tell('lock');
	}
	function idleDue() {
		var ms = api.idleMs(frappe.boot && frappe.boot.ffl_counter_lock_minutes);
		var wait = api.msUntilLock(Math.max(lastActive(), lastPoke), Date.now(), ms);
		if (wait === Infinity) return;
		if (wait > 0) { idleTimer = setTimeout(idleDue, wait); return; }   // another tab was used since
		lock();
	}
	function resetIdle() {
		var ms = api.idleMs(frappe.boot && frappe.boot.ffl_counter_lock_minutes);
		clearTimeout(idleTimer);
		if (!ms || (overlay && overlay.find('.ffl-staff-cancel').length === 0)) return;   // never, or already locked
		idleTimer = setTimeout(idleDue, ms);
	}
	function poke() {
		var now = Date.now();
		if (now - lastPoke < 1000) return;   // a mouse move fires many times a second
		lastPoke = now;
		markActive(now);
		resetIdle();
	}

	// ---- the user menu entry --------------------------------------------------

	// frappe builds the user menu inside Sidebar.create_user_menu with a literal option
	// list, so the entry goes in by handing that one Dropdown a longer list. Patched at
	// load: app_include_js runs before the desk builds its sidebar.
	function patchUserMenu() {
		var Sidebar = frappe.ui && frappe.ui.Sidebar;
		if (!Sidebar || !Sidebar.prototype.create_user_menu || Sidebar.prototype.create_user_menu.fflSwitch) return;
		var orig = Sidebar.prototype.create_user_menu;
		Sidebar.prototype.create_user_menu = function () {
			// Built per menu, so the label is translated in the user's language.
			var item = { name: 'ffl-switch-user', label: __('Switch user'), icon: 'users',
				onclick: function () { show({ locked: false }); } };
			var Dropdown = frappe.ui.Dropdown;
			frappe.ui.Dropdown = function (opts) {
				if (frappe.session.user !== 'Guest') opts = Object.assign({}, opts, { options: api.withMenuItem(opts.options, item) });
				return new Dropdown(opts);
			};
			try { return orig.apply(this, arguments); } finally { frappe.ui.Dropdown = Dropdown; }
		};
		Sidebar.prototype.create_user_menu.fflSwitch = true;
	}
	patchUserMenu();

	function wire() {
		if (!window.frappe || !frappe.boot || frappe.session.user === 'Guest') return;
		['mousemove', 'mousedown', 'keydown', 'touchstart', 'wheel', 'scroll'].forEach(function (ev) {
			document.addEventListener(ev, poke, { passive: true, capture: true });
		});
		if (channel) {
			channel.onmessage = function (e) {
				var type = e.data && e.data.type;
				if (type === 'lock') show({ locked: true });
				// Only a lock screen closes; a switch-user list someone has open stays.
				else if (type === 'unlock' && overlay && !overlay.find('.ffl-staff-cancel').length) { close(); resetIdle(); }
				else if (type === 'switch') location.reload();   // the session now belongs to someone else
			};
		}
		if (frappe.boot.ffl_screen_locked) show({ locked: true });
		else { lastPoke = Date.now(); markActive(lastPoke); resetIdle(); }
	}
	$(document).on('app_ready', function () { setTimeout(wire, 0); });

	Object.assign(api, { show: show, lock: lock });
})();
