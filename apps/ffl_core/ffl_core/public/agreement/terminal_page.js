// The signing tablet's page (www/agreement/terminal.html): pair once with a code,
// then poll every 2 s (ffl_core.signing_terminal.terminal_poll). When the counter
// sends an agreement, open it on the ordinary signing screen
// (/agreement/sign#t=…&from=terminal), which comes back here once it is signed.
// The device key lives in this browser only (SigningPad.device); an unknown or
// switched-off tablet goes back to the pairing screen. While idle it also offers
// Start Form 4473 (FastBound's customer page, 4473.io) when the store uses it, in a
// new tab so this one keeps polling: a link sent meanwhile lives only 10 minutes.
(function () {
	'use strict';
	var $ = function (sel, el) { return (el || document).querySelector(sel); };
	var app = $('#app');
	var csrf = ($('meta[name="ffl-csrf"]') || {}).content || '';
	var device = SigningPad.device(window.localStorage);
	var form = $('[data-screen="pair"]');
	var POLL_MS = 2000;
	var timer = null;

	function show(name) {
		app.querySelectorAll('[data-screen]').forEach(function (s) { s.hidden = s.getAttribute('data-screen') !== name; });
	}

	function call(method, args) {
		return fetch('/api/method/ffl_core.signing_terminal.' + method, {
			method: 'POST', credentials: 'same-origin',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Frappe-CSRF-Token': csrf },
			body: JSON.stringify(args),
		}).then(function (r) {
			return r.json().catch(function () { return {}; }).then(function (body) {
				if (!r.ok) {
					var err = new Error(SigningPad.serverMessage(body, r.status));
					err.status = r.status;
					throw err;
				}
				return body.message;
			});
		});
	}

	function problem(text) {
		var p = $('[data-problem]');
		p.textContent = text || '';
		p.hidden = !text;
	}

	function pairScreen() {
		clearTimeout(timer);
		show('pair');
		setTimeout(function () { form.code.focus(); }, 100);
	}

	function poll() {
		var key = device.get();
		if (!key) return pairScreen();
		call('terminal_poll', { secret: key.secret }).then(function (r) {
			$('[data-offline]').hidden = true;
			if (!r || !r.paired) { device.clear(); return pairScreen(); }
			if (r.token) {
				location.href = '/agreement/sign#t=' + encodeURIComponent(r.token) + '&from=terminal';
				return;
			}
			$('[data-store]').textContent = r.store_name || '';
			$('[data-terminal]').textContent = r.terminal_name || key.name || '';
			var f4473 = $('[data-form4473]');
			if (r.form_4473_url) f4473.href = r.form_4473_url;
			f4473.hidden = !r.form_4473_url;
			show('idle');
			timer = setTimeout(poll, POLL_MS);
		}, function () {
			// Offline or the server is busy: keep the waiting screen and try again.
			if (!app.querySelector('[data-screen="idle"]').hidden) $('[data-offline]').hidden = false;
			else show('idle');
			timer = setTimeout(poll, POLL_MS * 3);
		});
	}

	form.addEventListener('submit', function (e) {
		e.preventDefault();
		problem('');
		var code = (form.code.value || '').replace(/\D/g, '');
		if (code.length !== 6) return problem('Type the 6-digit code.');
		var btn = $('[data-pair]');
		btn.disabled = true;
		call('pair_terminal', { code: code }).then(function (r) {
			if (!device.set(r.secret, r.terminal_name)) {
				problem('This browser cannot keep the pairing (private browsing?). Open the page in a normal tab.');
				return;
			}
			form.reset();
			poll();
		}, function (err) {
			problem(err.message);
		}).then(function () { btn.disabled = false; });
	});

	// Keep the screen on while waiting, where the browser allows it (Safari 16.4+, Chrome).
	function stayAwake() {
		if (navigator.wakeLock && document.visibilityState === 'visible') {
			navigator.wakeLock.request('screen').catch(function () { /* the device's own setting rules */ });
		}
	}
	document.addEventListener('visibilitychange', stayAwake);
	document.addEventListener('click', stayAwake, { once: true });
	stayAwake();

	poll();
})();
