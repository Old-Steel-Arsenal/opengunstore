// The agreement signing page (www/agreement/sign.html): load the agreement for the
// one-time token in the URL fragment, collect the signer's name, "I agree", the
// age answer and the drawn signature, and submit. Endpoints: ffl_core.agreements
// load_for_signing / submit_signature (Guest). Nothing is kept in the browser.
(function () {
	'use strict';
	var $ = function (sel, el) { return (el || document).querySelector(sel); };
	var app = $('#app');
	var csrf = ($('meta[name="ffl-csrf"]') || {}).content || '';
	var params = new URLSearchParams(location.hash.replace(/^#/, ''));
	var token = params.get('t') || '';
	// Opened by the store's signing tablet (www/agreement/terminal): sign with its
	// device key, go back to the waiting screen afterwards, and follow a newer
	// agreement the counter sends meanwhile.
	var device = params.get('from') === 'terminal' ? SigningPad.device(window.localStorage).get() : null;
	var TERMINAL_PAGE = '/agreement/terminal';
	var form = $('[data-screen="sign"]');
	var pad = null;
	var data = null;

	function show(name) {
		app.querySelectorAll('[data-screen]').forEach(function (s) { s.hidden = s.getAttribute('data-screen') !== name; });
		if (name === 'sign' && pad) pad.redraw();   // sized only once visible
	}

	function fail(message) {
		$('[data-error]').textContent = message;
		show('error');
		if (device) setTimeout(function () { location.replace(TERMINAL_PAGE); }, 8000);
	}

	function call(method, args, module) {
		return fetch('/api/method/ffl_core.' + (module || 'agreements') + '.' + method, {
			method: 'POST', credentials: 'same-origin',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Frappe-CSRF-Token': csrf },
			body: JSON.stringify(args),
		}).then(function (r) {
			return r.json().catch(function () { return {}; }).then(function (body) {
				if (!r.ok) throw new Error(SigningPad.serverMessage(body, r.status));
				return body.message;
			});
		}, function () { throw new Error('No connection. Check the Wi-Fi and try again.'); });
	}

	function problem(text) {
		var p = $('[data-problem]');
		p.textContent = text || '';
		p.hidden = !text;
		if (text) p.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
	}

	// Under 18 known from the date of birth (true/false), or asked (null).
	function minor() {
		if (!data.minors_need_guardian) return false;
		if (data.minor_by_dob !== null && data.minor_by_dob !== undefined) return !!data.minor_by_dob;
		return form.participant_is_minor.checked;
	}

	function paintMinor() {
		var on = minor();
		$('[data-guardian]').hidden = !on;
		$('[data-name-label]').textContent = on ? 'Parent or guardian\'s full name' : 'Your full name';
	}

	function render() {
		$('[data-store]').textContent = data.store_name || '';
		$('[data-title]').textContent = data.title;
		$('[data-customer]').textContent = data.customer_name;
		$('[data-body]').innerHTML = data.body_html;   // sanitized by the server
		document.title = data.title;
		var needs = !!data.minors_need_guardian;
		var known = data.minor_by_dob !== null && data.minor_by_dob !== undefined;
		$('[data-minor]').hidden = !needs || (known && !data.minor_by_dob);
		$('[data-minor-declare]').hidden = known;
		$('[data-minor-note]').hidden = !(known && data.minor_by_dob);
		form.participant_is_minor.addEventListener('change', paintMinor);
		paintMinor();
		show('sign');
	}

	function submit(e) {
		e.preventDefault();
		problem('');
		var name = form.signer_name.value.trim();
		var rel = minor() ? ((form.querySelector('input[name="signer_relationship"]:checked') || {}).value || '') : 'Self';
		if (minor() && !rel) return problem('Choose Parent or Guardian.');
		if (!name) { form.signer_name.focus(); return problem('Type the full name of the person signing.'); }
		if (!form.agreed.checked) return problem('Tick "I agree" to sign.');
		if (!pad.hasInk()) return problem('Please sign in the box.');
		var btn = $('[data-submit]');
		btn.disabled = true;
		btn.textContent = 'Signing…';
		call('submit_signature', {
			token: token, signature: pad.toDataURL(), agreed: 1, signer_name: name,
			participant_is_minor: minor() ? 1 : 0, signer_relationship: rel,
			terminal_secret: device ? device.secret : null,
		}).then(function (r) {
			// Nothing of the agreement stays on the screen or in the address bar.
			history.replaceState(null, '', location.pathname);
			token = '';
			data = null;
			pad.clear();
			form.reset();
			$('[data-body]').innerHTML = '';
			$('[data-done-store]').textContent = r && r.store_name ? ' by ' + r.store_name : '';
			show('done');
			if (device) {
				clearTimeout(watch);
				setTimeout(function () { location.replace(TERMINAL_PAGE); }, 4000);
			}
		}, function (err) {
			btn.disabled = false;
			btn.textContent = 'Sign';
			problem(err.message);
		});
	}

	// A new link opened in this same tab (the tablet scans the next customer's QR code)
	// changes only the fragment, which does not reload the page: start over for it.
	window.addEventListener('hashchange', function () { location.reload(); });

	// On the signing tablet: a newer agreement from the counter replaces this one
	// (the newest wins, as on the waiting screen); a tablet cut off goes back there.
	var watch = null;
	function watchTerminal() {
		if (!device) return;
		call('terminal_poll', { secret: device.secret }, 'signing_terminal').then(function (r) {
			if (!r || !r.paired) return location.replace(TERMINAL_PAGE);
			if (r.token) return location.replace('/agreement/sign#t=' + encodeURIComponent(r.token) + '&from=terminal');
			watch = setTimeout(watchTerminal, 2000);
		}, function () { watch = setTimeout(watchTerminal, 6000); });
	}

	pad = SigningPad.attach($('[data-pad]'), { onchange: function () { problem(''); } });
	$('[data-clear]').addEventListener('click', function () { pad.clear(); });
	// The keyboard's Done / Enter closes the keyboard; it does not submit half a form.
	form.signer_name.addEventListener('keydown', function (e) {
		if (e.key === 'Enter') { e.preventDefault(); form.signer_name.blur(); }
	});
	form.addEventListener('submit', submit);

	if (!token) {
		fail('This page opens from a signing link. Ask the store for a new one.');
		return;
	}
	call('load_for_signing', { token: token }).then(function (r) { data = r; render(); watchTerminal(); },
		function (err) { fail(err.message); });
})();
