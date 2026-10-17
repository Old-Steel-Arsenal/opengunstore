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
// The pure half (idleMs, initials, pickAction, withMenuItem) is Node-tested.
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

	return { idleMs: idleMs, initials: initials, pickAction: pickAction, withMenuItem: withMenuItem };
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
				if (!r.ok) throw new Error(__('That password is not right.'));
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
				? __('Pick yourself and type your password to carry on, or pick someone else to sign in as them.')
				: __('Pick who is at the counter and type their password.')) + '</p>'
			+ '<div class="ffl-staff-list" style="display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:10px"></div>'
			+ '<div class="ffl-staff-auth" style="margin-top:14px" hidden>'
			+ '<div class="ffl-staff-who" style="font-weight:600; margin-bottom:6px"></div>'
			+ '<input type="password" class="form-control ffl-staff-pwd" autocomplete="current-password" placeholder="' + esc(__('Password')) + '">'
			+ '<div class="text-danger small ffl-staff-err" style="margin-top:6px"></div>'
			+ '<div style="display:flex; gap:8px; margin-top:10px"><button class="btn btn-primary ffl-staff-go">' + __('Continue') + '</button></div></div>'
			+ (locked ? '' : '<div style="text-align:right; margin-top:14px"><button class="btn btn-default ffl-staff-cancel">' + __('Cancel') + '</button></div>'));
		var picked = null;
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
					.html(face + '<span>' + esc(u.full_name) + '</span>' + (u.current ? '<span class="small text-muted">' + __('signed in') + '</span>' : ''))
					.appendTo($list)
					.on('click', function () {
						picked = u;
						var action = api.pickAction(u.user, frappe.session.user, locked);
						if (action === 'close') { close(); return; }
						$list.find('.ffl-staff-user').removeClass('btn-primary').addClass('btn-default');
						$(this).removeClass('btn-default').addClass('btn-primary');
						$card.find('.ffl-staff-who').text(u.full_name);
						$err.text('');
						$pwd.val('');
						$auth.prop('hidden', false);
						$pwd.trigger('focus');
					});
			});
		});

		var go = function () {
			if (!picked) return;
			var pwd = $pwd.val();
			if (!pwd) { $err.text(__('Type the password.')); return; }
			$err.text('');
			var action = api.pickAction(picked.user, frappe.session.user, locked);
			var done = action === 'unlock'
				? post(M + 'unlock_screen', { password: pwd }).then(function () { close(); resetIdle(); tell('unlock'); })
				: login(picked.user, pwd).then(function () { tell('switch'); location.reload(); });
			done.catch(function (e) {
				$err.text((e && e.message) || __('That password is not right.'));
				$pwd.val('').trigger('focus');
			});
		};
		$card.on('click', '.ffl-staff-go', go);
		$pwd.on('keydown', function (e) { if (e.key === 'Enter') go(); });
		$card.on('click', '.ffl-staff-cancel', close);
		if (!locked) overlay.on('keydown', function (e) { if (e.key === 'Escape') close(); });
	}

	// ---- idle lock -------------------------------------------------------

	var idleTimer = null;
	function lock() {
		post(M + 'lock_screen').then(function () { show({ locked: true }); }, function () { show({ locked: true }); });
		tell('lock');
	}
	function resetIdle() {
		var ms = api.idleMs(frappe.boot && frappe.boot.ffl_counter_lock_minutes);
		clearTimeout(idleTimer);
		if (!ms || (overlay && overlay.find('.ffl-staff-cancel').length === 0)) return;   // never, or already locked
		idleTimer = setTimeout(lock, ms);
	}
	var lastPoke = 0;
	function poke() {
		var now = Date.now();
		if (now - lastPoke < 1000) return;   // a mouse move fires many times a second
		lastPoke = now;
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
		else resetIdle();
	}
	$(document).on('app_ready', function () { setTimeout(wire, 0); });

	Object.assign(api, { show: show, lock: lock });
})();
