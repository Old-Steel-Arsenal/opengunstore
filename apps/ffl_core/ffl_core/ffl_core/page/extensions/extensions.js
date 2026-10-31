// Extensions — /app/extensions (System Manager). The store's view of the
// POS Extension registry: one card per extension on this bench, the on/off
// switch, install-on-this-site / uninstall (background jobs, polled), Release
// notes, History. Upload: a zip or a public Git repository is linted
// on the server, reviewed here, and installed — or, for an id already on the
// server, upgraded in place — by the store's agent. Marketplace is a
// placeholder.
// Styles only use Frappe
// tokens (prefix xt-) and live inside the page root so they leave with it.

// The two anchors of the self-hosting guide the page's messages point at (the server's own messages use
// ffl_core.extensions.EXTENSIONS_DOC / ROLLBACK_DOC; test_extension_upgrade checks both anchors exist).
const XT_EXTENSIONS_DOC = 'docs/self-hosting.md#extensions';
const XT_ROLLBACK_DOC = 'docs/self-hosting.md#roll-back-an-upgrade';

// Pure kernel (no DOM / frappe / __()): the decisions a card and the review card
// make, unit-tested under Node in tests/js/extensions_panel.test.js (the desk
// runs a page script inside `new Function`, so none of this is global).

// busy_states comes with the payload (jobs.BUSY_STATES) — the server owns that
// list. The switch shows `effective` (on ∧ installed here ∧ compatible), not
// `enabled`, the manager's intent that a paused extension keeps.
function xtCardState(row, busyStates) {
	if ((busyStates || []).includes(row.job_state)) return 'busy';
	if (!row.installed_on_site) return 'available';
	if (!row.compatible) return 'incompatible';
	return row.effective ? 'on' : 'off';
}

// Which progress copy a busy card shows. Staged is an upgrade when the pending
// restart request says so (sideload.request_restart(action, app)).
function xtBusyKind(row, pending) {
	if (row.job_state === 'Staged') {
		return pending && pending.app === row.name && pending.action === 'upgrade' ? 'staged-upgrade' : 'staged';
	}
	return { Installing: 'installing', Upgrading: 'upgrading', Uninstalling: 'uninstalling' }[row.job_state] || 'other';
}

// The status-bar line for a pending restart request: 'install' / 'upgrade' while
// the restart is still to come (row Staged, or no row yet), 'upgrading' once the
// agent has restarted and the database is being updated, null otherwise (an
// Installing card shows its own progress).
function xtRestartBanner(pending, rows) {
	if (!pending) return null;
	const row = (rows || []).find((r) => r.name === pending.app);
	if (row && row.job_state === 'Upgrading') return 'upgrading';
	if (row && row.job_state !== 'Staged') return null;
	return pending.action === 'upgrade' ? 'upgrade' : 'install';
}

// Not compatible, yet the manager left it on: it resumes by itself once compatible.
// Off and not compatible is a locked Off, not Paused.
function xtPaused(row) {
	return !!(row.installed_on_site && !row.compatible && row.enabled);
}

// "<reason> Backup: <absolute path> — docs/self-hosting.md#roll-back-an-upgrade …" → { reason, backup }. Every
// message that names a backup carries that token, followed only by a space or the
// end; one without a backup says "No database backup was recorded".
function xtSplitBackup(message) {
	const text = String(message || '');
	const m = /\bBackup:\s*(\/\S+)/.exec(text);
	if (!m) return { reason: text, backup: '' };
	return { reason: text.slice(0, m.index).replace(/[\s.,;:—-]+$/, ''), backup: m[1] };
}

// The version an upgrade is putting in place, as its job message names it ("Putting <id> <v> in
// place", "<id> <v> is on the server", "Updating the database for <id> <v>."): the row's own
// version stays the installed one until finish_upgrade syncs it. '' when the message names none.
function xtTargetVersion(row) {
	const words = String(row.job_message || '').split(/\s+/);
	const i = words.indexOf(row.name);
	return i >= 0 && words[i + 1] ? words[i + 1].replace(/[.,;:]+$/, '') : '';
}

// A Failed card's note. `unchanged` is only for a job that may say "nothing on this
// POS was changed": never for a failed upgrade ("Upgrade failed…", or a message that
// points at the rollback recipe) or a job that took a backup. Rows that failed before
// the guide moved still carry the old pointer ("RUNBOOK §14" / "Roll back an upgrade"),
// so both forms count.
function xtFailedNote(message) {
	const { reason, backup } = xtSplitBackup(message);
	const text = String(message || '');
	return { reason, backup, unchanged: !backup && !/^Upgrade failed\b|Roll back an upgrade|RUNBOOK §14/.test(text) && !text.includes(XT_ROLLBACK_DOC) };
}

// The agent consumed this row's restart request, but the row is still busy (a
// restart failed, the replay wait ran out, a finish step could not run): the done
// file says why. Only a done written for the request this page saw pending
// (`seen`), or for an Upgrading row (begin_upgrade runs while its request is
// pending), is about this job — an older one belongs to an earlier attempt, and
// the row may be busy with a new job that has not asked for a restart yet.
function xtAgentFailure(row, restart, busyStates, seen) {
	const done = restart && restart.done;
	if (!(busyStates || []).includes(row.job_state) || (restart && restart.pending) || !done || done.app !== row.name || done.ok) return null;
	const upgrading = row.job_state === 'Upgrading';
	if (!upgrading && !(seen && Date.parse(done.finished_at) >= Date.parse(seen.requested_at))) return null;
	const { reason, backup } = xtSplitBackup(done.message);
	return { reason, backup: backup || xtSplitBackup(row.job_message).backup, upgrade: upgrading || seen.action === 'upgrade' };
}

// While a restart is pending or a job runs the store is unreachable for a while on
// purpose (nginx or backend restarting: no answer, 502/504; a migrate: 503): such a
// poll stays quiet and the next one tries again.
function xtQuietPoll(data) {
	return !!(data && (data.busy || (data.restart && data.restart.pending)));
}
function xtOutage(status) {
	return [502, 503, 504].includes(status);
}

// What the review card of an inspected package offers. `upgrade` is null unless
// the id is already on this server; the server decides `allowed` (and
// install_upload refuses again) — this only mirrors it.
function xtReviewMode(report) {
	const up = report.upgrade;
	if (up) return { mode: 'upgrade', blocked: !report.ok || !up.allowed };
	return { mode: 'install', blocked: !report.ok || !!report.on_bench };
}

// An upgrade's permissions: new (hi = high-risk, painted red; the rest yellow),
// kept, and no longer asked for.
function xtPermissionDiff(report) {
	const up = report.upgrade || {};
	const hi = new Set(report.high_risk || []);
	const added = new Set(up.permissions_added || []);
	return {
		added: [...added].map((perm) => ({ perm, hi: hi.has(perm) })),
		kept: (report.permissions || []).filter((perm) => !added.has(perm)).map((perm) => ({ perm, hi: hi.has(perm) })),
		removed: (up.permissions_removed || []).slice(),
	};
}

// The update service's offer on an installed card (row.update from the server):
// a pill, and an Update button only when this POS can run it and nothing is busy.
function xtUpdateOffer(row, state) {
	const up = row && row.update;
	if (!up || state === 'available') return null;
	return { version: up.version, compatible: !!up.compatible, button: !!up.compatible && state !== 'busy' };
}

// The footer line: the payload says no update source is set up here. A payload
// without the key (an older server) is not "missing".
function xtUpdateServiceMissing(data) {
	return !!data && data.update_service === false;
}

// After Install / Upgrade on an upload, poll until the registry row shows the job
// took it: an install's row appears; an upgrade's row (already there) moves off
// the snapshot taken at the click.
function xtStagingSettled(row, before) {
	if (!row) return false;
	if (!before) return true;
	return row.job_state !== before.job_state || row.job_message !== before.job_message || row.version !== before.version;
}

if (typeof frappe !== 'undefined') {
	frappe.pages['extensions'].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __('Extensions'), single_column: true });
		wrapper.extensions = new gunstore.ExtensionsPage(page, wrapper);
	};
	frappe.pages['extensions'].on_page_show = function (wrapper) {
		if (wrapper.extensions) wrapper.extensions.load();
	};
	frappe.pages['extensions'].on_page_hide = function (wrapper) {
		if (wrapper.extensions) wrapper.extensions.stopPolling();
	};
}

globalThis.gunstore = globalThis.gunstore || {};     // window.gunstore in the desk; lets Node load the kernel

gunstore.ExtensionsPage = class ExtensionsPage {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.data = null;
		this.pollTimer = null;
		this.tab = 'installed';
		this.upload = null;              // the last inspected zip (server report), while on the Upload tab
		this.seenRequests = {};          // id → its restart request, while this page saw it pending (xtAgentFailure)
		this.page.set_primary_action(__('Refresh'), () => this.refresh(), 'refresh');
		this.page.add_menu_item(__('Registry (list view)'), () => frappe.set_route('List', 'POS Extension'));
		this.page.add_menu_item(__("What's New in POS"), () => window.gunstore.showWhatsNew && window.gunstore.showWhatsNew());
		this.$root = $(`
			<div class="xt-wrap">
				<style>${gunstore.ExtensionsPage.css()}</style>
				<div class="xt-status"></div>
				<div class="xt-tabs"></div>
				<div class="xt-list"></div>
				<div class="xt-upload" hidden></div>
				<div class="xt-foot"></div>
			</div>`).appendTo(this.page.main);
	}

	// --- data (read endpoints are GET-only on the server; xcall's third arg) --
	load() {
		return frappe.xcall('ffl_core.api.extensions.list_extensions', {}, 'GET').then((data) => this.render(data));
	}
	refresh() {
		this.page.btn_primary.prop('disabled', true);
		return frappe.xcall('ffl_core.api.extensions.refresh')
			.then((data) => {
				this.render(data);
				const s = data.sync || {};
				const n = (s.created || []).length + (s.updated || []).length + (s.removed || []).length + (s.reset || []).length;
				frappe.show_alert({ message: n ? __('Registry updated ({0} changes)', [n]) : __('Registry is up to date'), indicator: 'green' });
				if (data.updates && data.updates.error) {
					frappe.show_alert({ message: __('Could not check for extension updates right now — try Refresh again later.'), indicator: 'orange' }, 8);
				}
			})
			.finally(() => this.page.btn_primary.prop('disabled', false));
	}
	render(data) {
		this.data = data;
		const pending = data.restart && data.restart.pending;
		if (pending && pending.app) this.seenRequests[pending.app] = pending;
		for (const r of data.extensions || []) if (!(data.busy_states || []).includes(r.job_state)) delete this.seenRequests[r.name];
		this.renderStatus();
		this.renderTabs();
		this.renderList();
		this.renderFoot();
		this.$root.find('.xt-list').prop('hidden', this.tab !== 'installed');
		this.$root.find('.xt-upload').prop('hidden', this.tab !== 'upload');
		if (this.tab === 'upload' && !this.$root.find('.xt-upload').children().length) this.renderUpload();
		if (this.stagingWatch) this.checkStaging(data);
		if (data.busy || (data.restart && data.restart.pending) || this.stagingWatch) this.startPolling(); else this.stopPolling();
	}
	// After "Install" on an upload there is no registry row until the staging job
	// has copied the code (after "Upgrade", the row does not move until the job
	// starts); if it fails before that point the reason lives next to the upload —
	// fetch it instead of polling forever.
	checkStaging(data) {
		const w = this.stagingWatch;
		if (xtStagingSettled((data.extensions || []).find((r) => r.name === w.app_id), w.before)) { this.stagingWatch = null; return; }
		if (Date.now() - w.since < 3000) return;
		frappe.xcall('ffl_core.api.extensions.upload_failure', { sha256: w.sha256 }, 'GET').then((f) => {
			if (!f) { if (Date.now() - w.since > 180000) this.stagingWatch = null; return; }
			this.stagingWatch = null;
			const title = frappe.utils.escape_html(w.title || w.app_id);
			frappe.msgprint({ title: w.mode === 'upgrade' ? __('{0} could not be upgraded', [title]) : __('{0} could not be installed', [title]),
				message: `${__('Nothing on this POS was changed.')} ${frappe.utils.escape_html(f.message || '')} ${__('The details are in Error Log.')}`, indicator: 'red' });
		});
	}
	showTab(tab) {
		this.tab = tab;
		this.render(this.data);
	}
	startPolling() {
		if (this.pollTimer) return;
		this.pollTimer = setInterval(() => this.poll(), 3000);
	}
	// One request at a time (a restarting store can hold one for long). While the store
	// is expected to be unreachable the poll goes through fetch — frappe.call opens an
	// error dialog for every 502/504 — and an outage stays quiet; any other answer is
	// a real error, so frappe's load shows it.
	poll() {
		if (this.polling) return this.polling;
		const quiet = xtQuietPoll(this.data)
			? fetch('/api/method/ffl_core.api.extensions.list_extensions', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
				.then((res) => (xtOutage(res.status) ? null : res.ok ? res.json().then((r) => this.render(r.message)) : this.load()),
					() => null)                        // no answer at all: nginx is restarting
			: this.load();
		this.polling = quiet.finally(() => { this.polling = null; });
		return this.polling;
	}
	stopPolling() {
		if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
	}

	// --- pieces -----------------------------------------------------------
	installedRows() { return this.data.extensions.filter((r) => r.installed_on_site); }
	renderStatus() {
		const d = this.data;
		const e = frappe.utils.escape_html;
		const parts = [__('Core version {0}', [`<span class="xt-mono">${e(d.core_version)}</span>`])];
		parts.push(__('{0} installed on this site', [this.installedRows().length]));
		const paused = d.extensions.filter(xtPaused).length;
		if (paused) parts.push(`<span class="xt-bad">${__('{0} paused — not compatible with this core', [paused])}</span>`);
		if (d.busy) parts.push(__('{0} job running', [d.busy]));
		const rp = d.restart && d.restart.pending;
		const banner = xtRestartBanner(rp, d.extensions);
		if (banner) {
			const text = banner === 'install' ? __('The store is about to restart to load {0} — the POS pauses for a few seconds.', [e(rp.app)])
				: banner === 'upgrade' ? __('The store is about to restart to upgrade {0} — the POS is unavailable for about 1–3 minutes.', [e(rp.app)])
				: __('Upgrading {0}: the database is being updated — the POS may be unavailable for a minute or two.', [e(rp.app)]);
			parts.push(`<span class="xt-restart">${text}</span>`);
		}
		this.$root.find('.xt-status').html(
			`<span class="xt-ni">i</span><div>${parts.join(' · ')} · <button type="button" class="btn-link xt-link xt-whatsnew">${__("What's New in POS")}</button></div>`);
		this.$root.find('.xt-whatsnew').on('click', () => window.gunstore.showWhatsNew && window.gunstore.showWhatsNew());
	}
	// No update source here: no update cards and no error, just this line (the shared
	// "not set up" notice, ffl.capabilities). An older payload without the key says nothing.
	renderFoot() {
		const $foot = this.$root.find('.xt-foot').empty();
		if (!xtUpdateServiceMissing(this.data)) return;
		$foot.html(window.ffl && ffl.capabilities ? ffl.capabilities.notice('extension_updates')
			: `<div class="ffl-cap-notice">${__('Update service not configured')}</div>`);
	}
	renderTabs() {
		const n = this.installedRows().length;
		this.$root.find('.xt-tabs').html(`
			<button type="button" class="xt-tab ${this.tab === 'installed' ? 'on' : ''}" data-tab="installed">${__('Installed')}<span class="xt-cnt">${n}</span></button>
			<div class="xt-tab disabled" title="${__('Coming soon')}">${__('Marketplace')}</div>
			<button type="button" class="xt-tab ${this.tab === 'upload' ? 'on' : ''}" data-tab="upload">${__('Upload')}</button>`);
		this.$root.find('.xt-tab[data-tab]').on('click', (ev) => this.showTab(ev.currentTarget.dataset.tab));
	}
	renderList() {
		const $list = this.$root.find('.xt-list').empty();
		if (!this.data.extensions.length) {
			$list.html(`<div class="xt-empty">
				<div class="xt-empty-t">${__('No extensions on this POS yet')}</div>
				<div class="xt-empty-d">${__('When your developer puts one on the server, press Refresh and it appears here.')}</div>
			</div>`);
			return;
		}
		for (const row of this.data.extensions) $list.append(this.card(row));
	}
	stateOf(row) {
		return xtCardState(row, this.data.busy_states);
	}
	paused(row) {
		return xtPaused(row);
	}
	// A busy card's pill and progress line (HTML-safe).
	busyCopy(row) {
		const e = frappe.utils.escape_html;
		const version = e(xtTargetVersion(row));      // not row.version: that is the installed one until the upgrade finishes
		switch (xtBusyKind(row, this.data.restart && this.data.restart.pending)) {
			case 'installing': return { pill: __('Installing…'), text: __('Installing on this site — about a minute. You can leave this page.') };
			// the Backup row under the line shows the message's path
			case 'staged': return { pill: __('Waiting for restart…'), text: row.job_message ? this.sentence(xtSplitBackup(row.job_message).reason) : __('Waiting for the store to restart.') };
			case 'staged-upgrade': return { pill: __('Upgrade waiting for restart…'), text: version
				? __('Version {0} is on the server. The store restarts shortly to finish the upgrade — the POS is unavailable for about 1–3 minutes.', [version])
				: __('The new version is on the server. The store restarts shortly to finish the upgrade — the POS is unavailable for about 1–3 minutes.') };
			case 'upgrading': return { pill: __('Upgrading…'), text: version
				? __('Updating the database for version {0}. The POS may be unavailable for a minute or two. You can leave this page.', [version])
				: __('Updating the database. The POS may be unavailable for a minute or two. You can leave this page.') };
			case 'uninstalling': return { pill: __('Uninstalling…'), text: __('Backing up, then removing from this site. You can leave this page.') };
			default: return { pill: `${e(row.job_state)}…`, text: e(row.job_message || '') };
		}
	}
	pills(row, state, stuck) {
		const e = frappe.utils.escape_html;
		const p = [];
		if (stuck) p.push(`<span class="xt-pill bad">${__('Did not finish')}</span>`);
		else if (state === 'busy') p.push(`<span class="xt-pill busy">${this.busyCopy(row).pill}</span>`);
		else if (state === 'available') p.push(`<span class="xt-pill off">${__('Not installed')}</span>`);
		else if (state === 'incompatible') {
			// The reason decides the pill: a core-range miss says which core; an
			// unreadable manifest must not pretend to be a version problem.
			const needsCore = /core/i.test(row.incompatible_reason || '');
			p.push(`<span class="xt-pill bad">${needsCore ? __('Needs core {0}', [e(row.requires_core || '?')]) : __('Cannot be read')}</span>`);
		}
		else if (state === 'on') p.push(`<span class="xt-pill on">${__('Enabled')}</span>`);
		else p.push(`<span class="xt-pill off">${__('Disabled')}</span>`);
		if (row.job_state === 'Failed') p.push(`<span class="xt-pill bad">${__('Last job failed')}</span>`);
		const offer = xtUpdateOffer(row, state);
		if (offer) p.push(offer.compatible
			? `<span class="xt-pill upd">${__('{0} available', [e(offer.version)])}</span>`
			: `<span class="xt-pill bad" title="${e(row.update.reason || '')}">${__('{0} needs a newer POS', [e(offer.version)])}</span>`);
		// Anything not proven to come from the image, the marketplace or our update service is sideloaded
		// (the server sends the source normalized: api.extensions._rows).
		if (!['baked', 'marketplace', 'update-service'].includes(row.source)) p.push(`<span class="xt-pill side">${__('Sideloaded')}</span>`);
		if (row.license_required) p.push(`<span class="xt-pill paid">${__('Paid')}</span>`);
		return p.join('');
	}
	// What a permission token means, in the store's words.
	permLabel(perm) {
		const [kind, rest] = perm.includes(':') ? perm.split(/:(.+)/) : [perm, ''];
		const map = {
			first_party: __('Built by the OpenGunstore maintainers — may act on the bound book, custody and settlement'),
			doc_events: __('Reacts when a {0} is saved', [rest]),
			custom_fields: __('Adds its own fields to {0}', [rest]),
			pos_ui: __('Adds buttons to the sale screen'),
			desk_ui: __('Adds pages and form buttons'),
			scheduler: __('Runs scheduled tasks'),
			network: __('Sends data to {0}', [rest]),
			portal: __('Provides a website portal'),
			files: __('Reads and writes files'),
			compliance_read: __('Reads bound-book and 4473 records'),
			public_endpoint: __('Has endpoints that work without signing in'),
		};
		return map[kind] || perm;
	}
	// cls: 'hi' (red), 'new' (yellow: asked for by an upgrade), 'gone' (dropped by an upgrade) or ''.
	permChip(perm, cls) {
		const e = frappe.utils.escape_html;
		const note = { hi: __('High-risk permission'), new: __('New in this version'), gone: __('No longer asked for') }[cls];
		return `<span class="xt-perm ${cls}" title="${e(perm)}${note ? ' · ' + note : ''}">${e(this.permLabel(perm))}</span>`;
	}
	permChips(row) {
		const hi = new Set(row.high_risk || []);
		return (row.permissions || []).map((perm) => this.permChip(perm, hi.has(perm) ? 'hi' : '')).join('');
	}
	// An upgrade's permissions: new ones red when high-risk, yellow otherwise.
	permissionDiff(r) {
		const d = xtPermissionDiff(r);
		const chips = (list, cls) => list.map((p) => this.permChip(p.perm, p.hi ? 'hi' : cls)).join('');
		return `<div class="xt-kv xt-permdiff">
			<span class="k">${__('New permissions')}</span><span class="v xt-perms">${d.added.length ? chips(d.added, 'new') : __('None')}</span>
			${d.kept.length ? `<span class="k">${__('Unchanged')}</span><span class="v xt-perms">${chips(d.kept, '')}</span>` : ''}
			${d.removed.length ? `<span class="k">${__('No longer asked for')}</span><span class="v xt-perms">${d.removed.map((perm) => this.permChip(perm, 'gone')).join('')}</span>` : ''}
		</div>`;
	}
	settingsLinks(row) {
		const reg = window.gunstore.extensions;
		return (reg && reg.settingsLinks(row.name)) || [];
	}
	sourceLine(row) {
		const e = frappe.utils.escape_html;
		if (row.installed_at) {
			return __('Installed {0}{1}', [frappe.datetime.prettyDate(row.installed_at), row.installed_by ? ` · ${e(row.installed_by)}` : '']);
		}
		if (row.source === 'baked') return __('Shipped with the POS image');
		if (row.source === 'marketplace') return __('From the marketplace');
		return __('Put on the server by hand');
	}
	card(row) {
		const e = frappe.utils.escape_html;
		const state = this.stateOf(row);
		const stuck = state === 'busy' ? xtAgentFailure(row, this.data.restart, this.data.busy_states, this.seenRequests[row.name]) : null;
		const title = e(row.title || row.name);
		const initial = (row.title || row.name).trim().charAt(0).toUpperCase();
		const reason = e(row.incompatible_reason || __('not compatible'));
		const compat = row.compatible
			? `<span class="xt-ok">✓ ${__('works with this core')}</span>`
			: this.paused(row)
				? `<span class="xt-bad">✗ ${__('Paused: {0}.', [reason])} ${__('Nothing was removed; it turns back on by itself once it is compatible again.')}</span>`
				: `<span class="xt-bad">✗ ${reason}. ${__('Nothing was removed.')}</span>`;
		const links = this.settingsLinks(row);
		const publisher = row.publisher ? `${e(row.publisher)}${row.publisher_id ? ` (${e(row.publisher_id)})` : ''}` : __('Unknown publisher');
		const $card = $(`
			<div class="xt-card ${state}" data-id="${e(row.name)}">
				<div class="xt-icon" aria-hidden="true">${e(initial)}</div>
				<div class="xt-body">
					<div class="xt-head">
						<span class="xt-name">${title}</span>
						<span class="xt-ver">${e(row.version || '')}</span>
						${this.pills(row, state, stuck)}
					</div>
					<div class="xt-pub">${publisher} · <span class="xt-mono">${e(row.name)}</span></div>
					${row.description ? `<div class="xt-desc">${e(row.description)}</div>` : ''}
					${(row.permissions || []).length ? `<div class="xt-perms">${this.permChips(row)}</div>` : ''}
					<div class="xt-meta">${compat}<span>${this.sourceLine(row)}</span>
						<button type="button" class="btn-link xt-link xt-notes">${__('Release notes')}</button>
						<button type="button" class="btn-link xt-link xt-history">${__('History')}</button>
					</div>
					${stuck ? this.stuckNote(stuck) : state === 'busy' ? `<div class="xt-progress" role="status"><div class="xt-bar"><i></i></div><span>${this.busyCopy(row).text}</span>${this.backupRow(xtSplitBackup(row.job_message).backup)}</div>` : ''}
					${row.job_state === 'Failed' ? this.failedNote(row) : ''}
				</div>
				<div class="xt-actions"></div>
			</div>`);
		this.actions($card, row, state, links, stuck);
		$card.find('.xt-notes').on('click', () => this.releaseNotes(row));
		$card.find('.xt-history').on('click', () => frappe.set_route('Form', 'POS Extension', row.name));
		$card.find('.xt-copy').on('click', (ev) => frappe.utils.copy_to_clipboard(ev.currentTarget.dataset.path, __('Backup path copied')));
		return $card;
	}
	// The backup a job message names, with a Copy button ('' when it names none).
	backupRow(path) {
		if (!path) return '';
		const e = frappe.utils.escape_html;
		return `<div class="xt-backup"><span class="k">${__('Backup')}</span><code class="xt-mono">${e(path)}</code>`
			+ `<button type="button" class="btn btn-default btn-xs xt-btn xt-copy" data-path="${e(path)}">${__('Copy')}</button></div>`;
	}
	failedNote(row) {
		const e = frappe.utils.escape_html;
		const { reason, backup, unchanged } = xtFailedNote(row.job_message);
		if (unchanged) {
			return `<div class="xt-failed" role="alert"><b>${__('The last job did not finish.')}</b> ${__('Nothing on this POS was changed.')}${reason ? ` — ${e(this.firstLine(reason))} —` : ''} ${__('You can try again, or ask your developer: the details are in Error Log.')}</div>`;
		}
		// A failed upgrade may have changed this POS: the whole reason (a double failure's key
		// sentence comes last) and, with a backup, the way back — never "nothing was changed".
		// The sweep's "finish it" (its migrate got through) is the way on by itself:
		// a restore would lose what was written since the backup.
		const way = /\bAsk your developer to finish it\b/.test(reason) ? ''
			: backup ? __('A backup was taken before it started — give this path to your developer to roll back ({0}).', [XT_ROLLBACK_DOC])
				: __('You can try again, or ask your developer: the details are in Error Log.');
		return `<div class="xt-failed" role="alert"><b>${__('The last job did not finish.')}</b> ${this.sentence(reason)}${way}${this.backupRow(backup)}</div>`;
	}
	// The agent gave up on this row's restart (xtAgentFailure): what it said, and the way on.
	stuckNote(f) {
		// The agent gave up after its migrate got through (ext-agent.sh: "migrate succeeded — …"): finished by hand,
		// not restored — a restore would lose what was written since the backup.
		const migrated = f.upgrade && /\bmigrate succeeded\b/.test(f.reason || '');
		const way = !f.upgrade ? __('Ask your developer to check the ext-agent timer ({0}). Refresh unlocks this card after 30 minutes.', [XT_EXTENSIONS_DOC])
			: migrated ? __('The database was updated — ask your developer to finish it ({0}).', [XT_ROLLBACK_DOC])
			: f.backup ? __('A backup was taken before it started — give this path to your developer to roll back ({0}).', [XT_ROLLBACK_DOC])
				: __('Ask your developer to roll it back ({0}).', [XT_ROLLBACK_DOC]);
		return `<div class="xt-failed" role="alert"><b>${__('The store could not finish this.')}</b> ${this.sentence(f.reason)}${way}${this.backupRow(f.upgrade ? f.backup : '')}</div>`;
	}
	sentence(text) {
		const t = String(text || '').replace(/[\s.]+$/, '');
		return t ? `${frappe.utils.escape_html(t)}. ` : '';
	}
	firstLine(text) {
		const line = String(text || '').split('\n')[0];
		return line.length > 160 ? `${line.slice(0, 157)}…` : line;
	}
	actions($card, row, state, links, stuck) {
		const e = frappe.utils.escape_html;
		const $a = $card.find('.xt-actions');
		if (state === 'available') {
			const $btn = $(`<button type="button" class="btn btn-primary btn-sm xt-btn">${__('Install')}</button>`).appendTo($a);
			if (!row.compatible) $btn.prop('disabled', true).attr('title', row.incompatible_reason || '');
			$btn.on('click', () => this.install(row));
			return;
		}
		if (state === 'busy') {
			// a row the store could not finish says so once, in its pill and note: nothing is working on it
			if (!stuck) $a.append(`<button type="button" class="btn btn-default btn-sm xt-btn" disabled>${__('Working…')}</button>`);
			return;
		}
		const locked = state === 'incompatible';
		const on = !!row.effective;                   // what runs — a paused extension keeps enabled = 1
		const paused = this.paused(row);
		const label = on ? __('Turn {0} off', [row.title || row.name]) : __('Turn {0} on', [row.title || row.name]);
		const why = row.incompatible_reason || __('not compatible');
		// Desktop: a switch. ≤768px: the same action as a 44px text button.
		const $switch = $(`
			<label class="xt-switch ${on ? 'on' : ''} ${locked ? 'locked' : ''}" title="${locked ? e(paused ? __('Paused: {0}', [why]) : __('Locked: {0}', [why])) : ''}">
				<input type="checkbox" role="switch" aria-checked="${on ? 'true' : 'false'}" aria-label="${e(label)}" ${on ? 'checked' : ''} ${locked ? 'disabled' : ''}>
				<span class="xt-knob" aria-hidden="true"></span><span class="xt-switch-text">${paused ? __('Paused') : on ? __('On') : __('Off')}</span>
			</label>`).appendTo($a);
		$switch.find('input').on('change', (ev) => this.toggle(row, ev.target.checked));
		const $row = $('<div class="xt-btnrow"></div>').appendTo($a);
		const offer = xtUpdateOffer(row, state);
		if (offer && offer.button) {
			$(`<button type="button" class="btn btn-primary btn-sm xt-btn xt-update">${__('Update to {0}', [e(offer.version)])}</button>`)
				.appendTo($row).on('click', () => this.update(row));
		}
		const $mobileToggle = $(`<button type="button" class="btn btn-sm xt-btn xt-mobile-toggle ${on || locked ? 'btn-default' : 'btn-primary'}" ${locked ? 'disabled' : ''}>${paused ? __('Paused') : on ? __('Turn off') : __('Turn on')}</button>`).appendTo($row);
		$mobileToggle.on('click', () => this.toggle(row, !on));
		if (links.length) {
			const $s = $(`<button type="button" class="btn btn-default btn-sm xt-btn" ${on ? '' : 'disabled'}>${__('Settings')}</button>`).appendTo($row);
			$s.on('click', () => this.openSettings(links));
		}
		const $menu = $(`
			<div class="dropdown xt-more">
				<button type="button" class="btn btn-default btn-sm xt-btn" data-toggle="dropdown" aria-haspopup="true" aria-expanded="false" aria-label="${__('More actions for {0}', [e(row.title || row.name)])}">⋮</button>
				<div class="dropdown-menu dropdown-menu-right">
					<button type="button" class="dropdown-item xt-m-notes">${__('Release notes')}</button>
					<button type="button" class="dropdown-item xt-m-history">${__('History')}</button>
					<div class="dropdown-divider"></div>
					<button type="button" class="dropdown-item xt-danger-text xt-m-uninstall">${__('Uninstall from this site…')}</button>
				</div>
			</div>`).appendTo($row);
		$menu.find('.xt-m-notes').on('click', () => this.releaseNotes(row));
		$menu.find('.xt-m-history').on('click', () => frappe.set_route('Form', 'POS Extension', row.name));
		$menu.find('.xt-m-uninstall').on('click', () => this.uninstall(row));
	}

	// --- actions ----------------------------------------------------------
	toggle(row, on) {
		const title = frappe.utils.escape_html(row.title || row.name);
		const apply = () => frappe.xcall('ffl_core.api.extensions.set_enabled', { extension: row.name, enabled: on ? 1 : 0 })
			.then(() => {
				frappe.show_alert({ message: on ? __('{0} is on', [title]) : __('{0} is off — its data is kept', [title]), indicator: on ? 'green' : 'orange' });
				return this.load();
			})
			.catch(() => this.load());
		if (on) return apply();
		return frappe.xcall('ffl_core.api.extensions.disable_notice', { extension: row.name }, 'GET').then((notice) => {
			if (!notice) return apply();
			const d = new frappe.ui.Dialog({
				title: __('Turn off {0}?', [title]),
				fields: [{ fieldtype: 'HTML', options: `
					<div>${__('Its buttons disappear from the POS and its automatic steps stop running.')} <b>${__('All of its data is kept.')}</b> ${__('Turn it back on any time.')}</div>
					<div class="xt-notice warn"><span class="xt-ni">!</span><div>${frappe.utils.escape_html(notice)}</div></div>` }],
				primary_action_label: __('Turn off'),
				primary_action: () => { d.hide(); apply(); },
				secondary_action_label: __('Cancel'),
				secondary_action: () => { d.hide(); this.load(); },
			});
			d.$wrapper.addClass('xt-dialog').on('hidden.bs.modal', () => this.load());
			d.show();
		});
	}
	install(row) {
		const title = frappe.utils.escape_html(row.title || row.name);
		return frappe.xcall('ffl_core.api.extensions.install_on_site', { extension: row.name })
			.then(() => { frappe.show_alert({ message: __('Installing {0}…', [title]), indicator: 'blue' }); return this.load(); })
			.catch(() => this.load());
	}
	uninstall(row) {
		const title = frappe.utils.escape_html(row.title || row.name);
		frappe.xcall('ffl_core.api.extensions.uninstall_preview', { extension: row.name }, 'GET').then((p) => {
			const e = frappe.utils.escape_html;
			const tables = (p.doctypes || []).map((t) => `<b>${e(t.name)}</b> (${t.rows} ${t.child ? __('child rows') : __('rows')})`).join(', ');
			if (p.blocked) {
				// Records = history a backup file does not bring back by itself: the
				// server refuses too (uninstall_from_site), this just says so first.
				const d = new frappe.ui.Dialog({
					title: __('{0} has records', [title]),
					fields: [{ fieldtype: 'HTML', options: `
						<div class="xt-notice warn"><span class="xt-ni">!</span><div>${__('{0} still holds {1} records on this site ({2}). Uninstalling would delete them, so it is not offered.', [title, p.records, tables])}</div></div>
						<div class="xt-kv">
							<span class="k">${__('Instead')}</span><span class="v">${__('Turn it off — its pages and automatic steps stop, and every record is kept.')}</span>
						</div>` }],
					primary_action_label: __('Close'),
					primary_action: () => d.hide(),
				});
				d.$wrapper.addClass('xt-dialog');
				d.show();
				return;
			}
			const removed = [
				tables ? __('{0} tables — {1}', [p.doctypes.length, tables]) : __('no tables of its own'),
				p.custom_fields ? __('{0} fields it added to core records', [p.custom_fields]) : '',
				(p.roles || []).length ? __('roles {0}', [p.roles.map(e).join(', ')]) : '',
			].filter(Boolean).join('; ');
			const d = new frappe.ui.Dialog({
				title: __('Uninstall {0}?', [title]),
				fields: [
					{ fieldtype: 'HTML', options: `
						<div class="xt-notice info"><span class="xt-ni">i</span><div>${__('A database backup is taken first. It appears under Download Backups (search bar) and can be restored by your developer.')}</div></div>
						<div class="xt-kv">
							<span class="k">${__('Removed')}</span><span class="v">${removed}.</span>
							<span class="k">${__('Kept')}</span><span class="v">${__('Every sale, invoice and gun record. Nothing of the core POS is touched. The code stays on the server until your developer removes it there.')}</span>
							<span class="k">${__('Instead?')}</span><span class="v">${__('If you only want it out of the way, turn it off — that keeps its data.')}</span>
						</div>` },
					{ fieldtype: 'Data', fieldname: 'confirm_id', label: __('Type {0} to confirm', [e(row.name)]) },
				],
				primary_action_label: __('Back up and uninstall'),
				primary_action: (v) => {
					d.hide();
					frappe.xcall('ffl_core.api.extensions.uninstall_from_site', { extension: row.name, confirm_id: v.confirm_id || '' })
						.then(() => { frappe.show_alert({ message: __('Uninstalling {0}…', [title]), indicator: 'orange' }); return this.load(); })
						.catch(() => this.load());
				},
				secondary_action_label: __('Cancel'),
				secondary_action: () => d.hide(),
			});
			d.$wrapper.addClass('xt-dialog');
			d.show();
			// The typed-id gate. Frappe's Data control only fires change on blur/enter
			// and swallows synthetic input events, so read the control on a short
			// timer while the dialog is open — the button unlocks as the id is typed.
			const $btn = d.get_primary_btn().addClass('btn-danger').removeClass('btn-primary').prop('disabled', true);
			const gate = setInterval(() => $btn.prop('disabled', String(d.get_value('confirm_id') || '').trim() !== row.name), 150);
			d.$wrapper.on('hidden.bs.modal', () => clearInterval(gate));
		});
	}
	// --- Upload tab (sideload) ---------------------------------------------
	renderUpload() {
		const $u = this.$root.find('.xt-upload').empty();
		$u.html(`
			<div class="xt-up-intro">
				<div class="xt-notice info"><span class="xt-ni">i</span><div>${__('Upload the .zip your developer gave you or fetch it from a public Git repository; nothing is installed until you confirm.')}</div></div>
				<label class="xt-drop" for="xt-file">
					<input type="file" id="xt-file" accept=".zip,application/zip" hidden>
					<span class="xt-drop-t">${__('Choose a .zip file')}</span>
					<span class="xt-drop-d">${__('up to 50 MB')}</span>
				</label>
				<form class="xt-git" novalidate>
					<div class="xt-git-t">${__('Or fetch it from a public Git repository')}</div>
					<div class="xt-git-row">
						<div class="xt-field"><label for="xt-git-url">${__('Repository address')}</label>
							<input type="url" id="xt-git-url" class="form-control" placeholder="https://github.com/owner/repo" autocomplete="off" spellcheck="false" maxlength="300"></div>
						<div class="xt-field"><label for="xt-git-ref">${__('Tag or branch')}</label>
							<input type="text" id="xt-git-ref" class="form-control" placeholder="v1.2.0" autocomplete="off" spellcheck="false" maxlength="100"></div>
						<button type="submit" class="btn btn-default btn-sm xt-btn xt-git-fetch">${__('Fetch')}</button>
					</div>
					<div class="xt-git-d">${__('https only, no password in the address; prefer a release tag over a branch.')}</div>
					<div class="xt-failed xt-git-err" role="alert" hidden></div>
				</form>
				<div class="xt-up-busy" hidden><div class="xt-bar"><i></i></div><span></span></div>
			</div>
			<div class="xt-review"></div>`);
		$u.find('#xt-file').on('change', (ev) => { const f = ev.target.files && ev.target.files[0]; if (f) this.inspectFile(f); ev.target.value = ''; });
		$u.find('.xt-git').on('submit', (ev) => {
			ev.preventDefault();
			this.inspectGit(String($u.find('#xt-git-url').val() || '').trim(), String($u.find('#xt-git-ref').val() || '').trim());
		});
		if (this.upload) this.renderReview(this.upload);
	}
	// POST with fetch, not xcall, so the caller decides how a refusal is shown (a
	// dialog for a file, in place for a Git address). Resolves with the report;
	// rejects with the server's refusal as plain text (it can quote the package).
	postForReview(method, body, headers) {
		return fetch(`/api/method/${method}`, {
			method: 'POST', body, credentials: 'same-origin',
			headers: Object.assign({ 'X-Frappe-CSRF-Token': frappe.csrf_token }, headers),
		}).then(async (res) => {
			const data = await res.json().catch(() => ({}));
			if (res.ok && !data.exc) return data.message;
			let msg = __('The package could not be checked.');
			try { const m = JSON.parse(data._server_messages || '[]'); if (m.length) msg = JSON.parse(m[0]).message || msg; } catch (e) { /* keep default */ }
			throw String(msg);
		}, () => { throw __('The server could not be reached. Try again.'); });
	}
	checking($u, text) {
		$u.find('.xt-up-busy').prop('hidden', !text).find('span').text(text || '');
		$u.find('.xt-git-fetch').prop('disabled', !!text);
		if (text) $u.find('.xt-review').empty();
	}
	inspectFile(file) {
		const $u = this.$root.find('.xt-upload');
		if (file.size > 50 * 1024 * 1024) { frappe.msgprint({ title: __('Too large'), message: __('The file is larger than 50 MB.'), indicator: 'red' }); return; }
		this.checking($u, __('Checking the package…'));
		const form = new FormData();
		form.append('file', file, file.name);
		return this.postForReview('ffl_core.api.extensions.inspect_upload', form)
			.then((report) => { this.upload = report; this.renderReview(report); },
				(msg) => frappe.msgprint({ title: __('Cannot use this file'), message: frappe.utils.escape_html(msg), indicator: 'red' }))
			.finally(() => this.checking($u, ''));
	}
	inspectGit(url, ref) {
		const $u = this.$root.find('.xt-upload');
		const $err = $u.find('.xt-git-err').prop('hidden', true).text('');
		if (!url || !ref) { $err.text(__('Enter the repository address and a tag or branch.')).prop('hidden', false); return; }
		this.checking($u, __('Fetching from Git and checking the package — this can take up to a minute…'));
		return this.postForReview('ffl_core.api.extensions.inspect_git', JSON.stringify({ url, ref }), { 'Content-Type': 'application/json' })
			.then((report) => { this.upload = report; this.renderReview(report); },
				(msg) => $err.text(msg).prop('hidden', false))
			.finally(() => this.checking($u, ''));
	}
	renderReview(r) {
		const e = frappe.utils.escape_html;
		const $r = this.$root.find('.xt-review').empty();
		const errors = (r.findings || []).filter((f) => f.level === 'error');
		const warns = (r.findings || []).filter((f) => f.level !== 'error');
		const { mode, blocked } = xtReviewMode(r);
		const up = r.upgrade;
		const chips = this.permChips({ permissions: r.permissions, high_risk: r.high_risk });
		const finding = (f) => `<li class="${f.level}"><span class="xt-mono">${e(f.rule)}</span> ${f.path ? `<span class="xt-mono">${e(f.path)}${f.line ? ':' + f.line : ''}</span> — ` : ''}${e(f.message)}</li>`;
		const git = r.source && r.source.type === 'git' ? r.source : null;
		const origin = git
			? `<span class="xt-mono">${e(git.url)}@${e(git.ref)}</span> · ${__('commit {0}', [`<span class="xt-mono">${e(String(git.commit || '').slice(0, 7))}</span>`])}`
			: `${e(r.filename || '')} · ${frappe.form.formatters.FileSize ? frappe.form.formatters.FileSize(r.size) : r.size + ' B'}`;
		$r.html(`
			<div class="xt-card review">
				<div class="xt-icon" aria-hidden="true">${e((r.title || r.app_id || '?').trim().charAt(0).toUpperCase())}</div>
				<div class="xt-body">
					<div class="xt-head"><span class="xt-name">${e(r.title || r.app_id || __('Unknown package'))}</span><span class="xt-ver">${up ? `${e(up.from)} → ${e(up.to)}` : e(r.version || '')}</span>
						<span class="xt-pill ${r.ok ? 'on' : 'bad'}">${r.ok ? __('Checks passed') : __('{0} problems', [errors.length])}</span>
						${up ? `<span class="xt-pill ${up.allowed ? 'upd' : 'bad'}">${up.allowed ? __('Upgrade') : __('Cannot upgrade')}</span>` : ''}
						<span class="xt-pill side">${__('Unsigned')}</span></div>
					<div class="xt-pub">${e(r.publisher || __('Unknown publisher'))}${r.publisher_id ? ` (${e(r.publisher_id)})` : ''} · <span class="xt-mono">${e(r.app_id || '')}</span> · ${origin}</div>
					<div class="xt-notice warn"><span class="xt-ni">!</span><div><b>${__('You are installing code that nobody on the OpenGunstore team has reviewed.')}</b> ${__('Once installed it can read and change everything in this POS — sales, customers, the bound book. Only install packages from a developer you trust.')}</div></div>
					${up ? this.permissionDiff(r) : chips ? `<div class="xt-perms">${chips}</div>` : ''}
					${up && !up.allowed ? `<div class="xt-failed" role="alert">${__('This package cannot replace the version on this server: {0}', [e(up.reason || '')])}</div>` : ''}
					${!up && r.on_bench ? `<div class="xt-failed" role="alert">${__('{0} is already on this server. Uninstall it first.', [e(r.app_id)])}</div>` : ''}
					${errors.length ? `<div class="xt-findings"><b>${__('Fix these before it can be installed')}</b><ul>${errors.map(finding).join('')}</ul></div>` : ''}
					${warns.length ? `<div class="xt-findings warn"><b>${__('Notes for the developer (not blocking)')}</b><ul>${warns.map(finding).join('')}</ul></div>` : ''}
					${up ? this.notesFold(up) : ''}
					${blocked ? '' : `<div class="xt-kv">
						<span class="k">${__('What happens')}</span><span class="v">${mode === 'upgrade'
							? __('A database backup is taken, version {0} replaces {1} on the server, the store restarts and the database is updated. The POS is unavailable for about 1–3 minutes. Its data and its on/off switch are kept.', [e(up.to), e(up.from)])
							: __('The code is copied to the server, the store restarts once (the POS pauses for a few seconds), then the extension is installed and turned on. You can turn it off or uninstall it from the Installed tab.')}</span>
					</div>`}
					<div class="xt-confirm ${blocked ? 'blocked' : ''}">
						<label for="xt-confirm-id">${__('Type {0} to confirm', [`<span class="xt-mono">${e(r.app_id || '')}</span>`])}</label>
						<input type="text" id="xt-confirm-id" class="form-control" autocomplete="off" ${blocked ? 'disabled' : ''}>
						<button type="button" class="btn btn-primary btn-sm xt-btn xt-install-upload" disabled>${mode === 'upgrade' ? __('Upgrade to {0}', [e(up.to)]) : __('Install {0}', [e(r.title || r.app_id || '')])}</button>
					</div>
				</div>
			</div>`);
		$r.find('.xt-copy').on('click', (ev) => frappe.utils.copy_to_clipboard(ev.currentTarget.dataset.path, __('Backup path copied')));
		const $btn = $r.find('.xt-install-upload');
		const $in = $r.find('#xt-confirm-id');
		$in.on('input', () => $btn.prop('disabled', blocked || $in.val().trim() !== r.app_id));
		$btn.on('click', () => (mode === 'upgrade' ? this.confirmUpgrade(r, $in.val().trim()) : this.installUpload(r, $in.val().trim())));
	}
	// The release notes between the two versions, folded. The package is not
	// installed yet: frappe.markdown's allow-list already drops script and event
	// attributes; images go too, so opening the card fetches nothing from hosts
	// the package names.
	notesFold(up, { open = false } = {}) {
		const e = frappe.utils.escape_html;
		if (!up.release_notes) return `<div class="xt-pub">${__('No release notes for this version.')}</div>`;
		const doc = new DOMParser().parseFromString(window.gunstore.renderReleaseNotes([[up.to, up.release_notes]]), 'text/html');
		doc.querySelectorAll('img').forEach((img) => img.remove());
		return `<details class="xt-fold"${open ? ' open' : ''}><summary>${__('Release notes {0} → {1}', [e(up.from), e(up.to)])}</summary><div class="xt-fold-body">${doc.body.innerHTML}</div></details>`;
	}
	// Update: the server downloads the package from the update service and checks
	// it; one confirm (what changes, when) and it upgrades. Two clicks.
	update(row) {
		const e = frappe.utils.escape_html;
		frappe.call({
			method: 'ffl_core.api.extensions.fetch_update', args: { extension: row.name },
			freeze: true, freeze_message: __('Downloading {0} {1}…', [e(row.title || row.name), e(row.update.version)]),
		}).then((res) => {
			const r = res && res.message;
			if (!r) return;
			const { blocked } = xtReviewMode(r);
			if (blocked || !r.upgrade) {
				const errors = (r.findings || []).filter((f) => f.level === 'error');
				const message = !r.upgrade ? e(__('{0} is not on this server any more; refresh the page.', [row.name]))
					: !r.ok ? `${e(__('The package does not pass the checks:'))}<ul>${errors.map((f) =>
						`<li><span class="xt-mono">${e(f.rule)}</span> ${f.path ? `<span class="xt-mono">${e(f.path)}</span> — ` : ''}${e(f.message)}</li>`).join('')}</ul>`
						: e(r.upgrade.reason || '');
				frappe.msgprint({ title: __('Cannot update'), message, indicator: 'red' });
				return this.load();
			}
			this.confirmUpgrade(r, r.app_id, { fromUpdate: true });
		});
	}
	confirmUpgrade(r, confirmId, opts = {}) {
		const e = frappe.utils.escape_html;
		const up = r.upgrade;
		// From the update service there was no review card: what changes goes in the dialog.
		const extra = opts.fromUpdate ? `${this.notesFold(up, { open: true })}${this.permissionDiff(r)}` : '';
		const d = new frappe.ui.Dialog({
			title: __('Upgrade {0} to {1}?', [e(r.title || r.app_id), e(up.to)]),
			fields: [{ fieldtype: 'HTML', options: `
				<div class="xt-notice warn"><span class="xt-ni">!</span><div><b>${__('The POS is unavailable for about 1–3 minutes while it upgrades.')}</b> ${__('Pick a moment with no sale in progress — outside business hours is best.')}</div></div>
				<div class="xt-kv">
					<span class="k">${__('First')}</span><span class="v">${__('A database backup is taken automatically.')}</span>
					<span class="k">${__('Then')}</span><span class="v">${__('Version {0} replaces {1}, the store restarts and the database is updated.', [e(up.to), e(up.from)])}</span>
					<span class="k">${__('Kept')}</span><span class="v">${__('All of its data, and whether it is turned on.')}</span>
					<span class="k">${__('If it fails')}</span><span class="v">${__('The card shows the backup, and your developer can roll back.')}</span>
				</div>${extra}` }],
			primary_action_label: __('Back up and upgrade'),
			primary_action: () => { d.hide(); this.installUpload(r, confirmId, { keepUpload: !!opts.fromUpdate }); },
			secondary_action_label: __('Cancel'),
			secondary_action: () => d.hide(),
		});
		d.$wrapper.addClass('xt-dialog');
		d.show();
	}
	installUpload(r, confirmId, { keepUpload = false } = {}) {
		const title = frappe.utils.escape_html(r.title || r.app_id);
		const row = ((this.data && this.data.extensions) || []).find((x) => x.name === r.app_id);
		const before = row ? { job_state: row.job_state, job_message: row.job_message, version: row.version } : null;
		return frappe.xcall('ffl_core.api.extensions.install_upload', { sha256: r.sha256, confirm_id: confirmId })
			.then((res) => {
				frappe.show_alert({ message: res && res.mode === 'upgrade'
					? __('Upgrading {0} — the store restarts shortly; the POS is unavailable for about 1–3 minutes.', [title])
					: __('Installing {0} — the store will restart shortly.', [title]), indicator: 'blue' });
				if (!keepUpload) {                             // an Update from a card leaves the Upload tab as it was
					this.upload = null;
					this.tab = 'installed';
					this.$root.find('.xt-upload').empty();     // the next visit starts from a clean form
				}
				this.stagingWatch = { app_id: r.app_id, sha256: r.sha256, title: r.title, mode: res && res.mode, since: Date.now(), before };
				return this.load();
			})
			.catch(() => this.load());
	}
	openSettings(links) {
		if (links.length === 1) return frappe.set_route('Form', links[0].doctype);
		const d = new frappe.ui.Dialog({ title: __('Settings'), fields: [{ fieldtype: 'HTML', options: links.map((l) =>
			`<a class="btn btn-default btn-sm xt-btn" style="margin:0 6px 6px 0" href="/app/${frappe.router.slug(l.doctype)}">${frappe.utils.escape_html(l.label || l.doctype)}</a>`).join('') }] });
		d.show();
	}
	releaseNotes(row) {
		const title = frappe.utils.escape_html(row.title || row.name);
		return frappe.xcall('ffl_core.api.extensions.release_notes', { extension: row.name }, 'GET').then((entries) => {
			const html = window.gunstore.renderReleaseNotes(entries)
				|| `<p>${__('This extension ships no release notes.')}</p>`;
			frappe.msgprint({ title: __('{0} — release notes', [title]), message: html, wide: true });
		});
	}

	// --- styles (Frappe tokens only; verified against v16 at runtime) ---------
	static css() {
		return `
			.xt-wrap{max-width:1100px;margin:0 auto;padding:0 16px 40px}
			.xt-mono{font-family:var(--font-family-monospace);font-size:12px}
			.xt-link{padding:0;border:0;background:none;color:var(--text-muted);text-decoration:underline;font-size:inherit;cursor:pointer}
			.xt-link:focus-visible{outline:2px solid var(--blue-500);outline-offset:2px}
			.xt-status{display:flex;gap:10px;align-items:flex-start;padding:10px 14px;border:1px solid var(--bg-blue);background:var(--bg-blue);color:var(--text-on-blue);border-radius:8px;font-size:13px;margin-bottom:12px}
			.xt-status .xt-link{color:var(--text-on-blue)}
			.xt-ni{flex:none;width:18px;height:18px;border-radius:50%;background:var(--blue-500);color:var(--white);font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center}
			.xt-notice{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:8px;font-size:13px;margin:10px 0}
			.xt-notice.info{background:var(--bg-light-blue);border:1px solid var(--bg-blue);color:var(--text-color)}
			.xt-notice.warn{background:var(--alert-bg-warning);border:1px solid var(--alert-bg-warning);color:var(--text-color)}
			.xt-notice.warn .xt-ni{background:var(--yellow-700)}
			.xt-tabs{display:flex;gap:4px;border-bottom:1px solid var(--border-color);margin-bottom:12px;overflow-x:auto}
			.xt-tab{padding:8px 12px;font-size:13px;font-weight:500;color:var(--text-muted);border-bottom:2px solid transparent;white-space:nowrap;cursor:default}
			.xt-tab.on{color:var(--text-color);border-bottom-color:var(--text-color)}
			.xt-tab.disabled{opacity:.5}
			button.xt-tab{background:none;border:0;border-bottom:2px solid transparent;cursor:pointer}
			button.xt-tab:focus-visible{outline:2px solid var(--blue-500);outline-offset:-2px}
			.xt-restart{color:var(--text-on-blue);font-weight:600}
			.xt-drop{display:flex;flex-direction:column;align-items:center;gap:4px;padding:28px 16px;border:2px dashed var(--border-color);border-radius:8px;cursor:pointer;margin:12px 0;background:var(--card-bg)}
			.xt-drop:hover,.xt-drop:focus-within{border-color:var(--blue-500)}
			.xt-drop-t{font-weight:600;font-size:14px} .xt-drop-d{font-size:12px;color:var(--text-muted)}
			.xt-up-busy{font-size:12px;color:var(--text-muted);margin:8px 0}
			.xt-card.review{grid-template-columns:40px 1fr}
			.xt-findings{margin-top:10px;font-size:12px;background:var(--bg-red);color:var(--text-on-red, var(--red-700));padding:8px 10px;border-radius:6px}
			.xt-findings.warn{background:var(--alert-bg-warning);color:var(--text-color)}
			.xt-findings ul{margin:4px 0 0 16px;padding:0} .xt-findings li{margin:2px 0;word-break:break-word}
			.xt-confirm{display:grid;grid-template-columns:1fr auto;gap:6px 10px;align-items:center;margin-top:12px;max-width:520px}
			.xt-confirm label{grid-column:1 / -1;font-size:12px;color:var(--text-muted);margin:0}
			.xt-confirm.blocked{opacity:.6}
			.xt-cnt{margin-left:6px;font-size:11px;background:var(--gray-200);color:var(--gray-700);border-radius:10px;padding:1px 7px}
			.xt-list{display:flex;flex-direction:column;gap:10px}
			.xt-foot{margin-top:14px}
			.xt-card{display:grid;grid-template-columns:40px 1fr auto;gap:14px;padding:14px 16px;background:var(--card-bg);border:1px solid var(--border-color);border-radius:8px}
			.xt-card:is(.off,.incompatible) .xt-icon{opacity:.68}   /* only the decorative icon: faded text falls under 4.5:1 (light: #525252 at .68 on white ≈ 3.5:1) */
			.xt-card:is(.off,.incompatible) .xt-desc{color:var(--text-muted)}
			.xt-card.incompatible{border-color:var(--orange-100)}
			.xt-icon{width:40px;height:40px;border-radius:8px;background:var(--gray-200);color:var(--gray-700);font-weight:700;font-size:18px;display:flex;align-items:center;justify-content:center}
			.xt-head{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center}
			.xt-name{font-weight:600;font-size:14px}
			.xt-ver{font-family:var(--font-family-monospace);font-size:12px;color:var(--text-muted)}
			.xt-pill{font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;line-height:16px}
			.xt-pill.on{background:var(--bg-green);color:var(--text-on-green)}
			.xt-pill.off,.xt-pill.busy{background:var(--gray-200);color:var(--gray-700)}
			.xt-pill.bad{background:var(--bg-red);color:var(--text-on-red)}
			.xt-pill.side{background:var(--yellow-100);color:var(--yellow-800)}
			.xt-pill.paid{background:var(--teal-100);color:var(--teal-600)}
			.xt-pill.upd{background:var(--bg-orange);color:var(--text-on-orange)}
			.xt-pub{font-size:12px;color:var(--text-muted);margin-top:2px}
			.xt-desc{font-size:13px;margin-top:6px;max-width:66ch}
			.xt-perms{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
			.xt-perm{font-family:var(--font-family-monospace);font-size:11px;padding:1px 6px;border-radius:4px;background:var(--gray-200);color:var(--gray-700)}
			.xt-perm.hi{background:var(--bg-red);color:var(--text-on-red)}
			.xt-perm.new{background:var(--yellow-100);color:var(--yellow-800)}
			.xt-perm.gone{text-decoration:line-through}
			.xt-kv.xt-permdiff{grid-template-columns:max-content 1fr;align-items:start}
			.xt-permdiff .xt-perms{margin-top:0}
			.xt-pub .xt-mono,.xt-backup code{overflow-wrap:anywhere}
			.xt-fold{margin:10px 0;font-size:13px}
			.xt-fold summary{cursor:pointer;font-weight:600;color:var(--text-color)}
			.xt-fold summary:focus-visible{outline:2px solid var(--blue-500);outline-offset:2px}
			.xt-fold-body{margin-top:6px;padding:4px 12px;border-left:2px solid var(--border-color);max-height:320px;overflow:auto;font-weight:400}
			.xt-fold-body :is(h1,h2,h3,h4,h5,h6){font-size:13px;font-weight:600;margin:10px 0 4px}
			.xt-fold-body ul{margin:2px 0;padding-left:18px}
			.xt-backup{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;margin-top:6px}
			.xt-backup .k{font-weight:600}
			.xt-backup code{background:none;padding:0;color:inherit}
			.xt-git{margin:0 0 12px;padding:12px 14px;border:1px solid var(--border-color);border-radius:8px;background:var(--card-bg)}
			.xt-git-t{font-weight:600;font-size:13px;margin-bottom:8px}
			.xt-git-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,200px) auto;gap:8px 10px;align-items:end}
			.xt-field label{display:block;font-size:12px;color:var(--text-muted);margin:0 0 2px}
			.xt-git-d{font-size:12px;color:var(--text-muted);margin-top:6px}
			.xt-meta{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--text-muted);margin-top:8px;align-items:center}
			.xt-ok{color:var(--text-on-green)} .xt-bad{color:var(--text-on-red)}
			.xt-progress{margin-top:8px;font-size:12px;color:var(--text-muted)}
			.xt-bar{height:4px;border-radius:2px;background:var(--gray-200);overflow:hidden;margin-bottom:4px}
			.xt-bar i{display:block;width:40%;height:100%;background:var(--blue-500);animation:xt-slide 1.2s ease-in-out infinite}
			@keyframes xt-slide{0%{margin-left:-40%}100%{margin-left:100%}}
			@media (prefers-reduced-motion: reduce){.xt-bar i{animation:none;width:100%}}
			.xt-failed{margin-top:8px;font-size:12px;color:var(--text-on-red);background:var(--bg-red);padding:6px 8px;border-radius:6px;line-height:1.45}
			.xt-actions{display:flex;flex-direction:column;align-items:flex-end;gap:10px}
			.xt-btnrow{display:flex;gap:6px}
			.xt-mobile-toggle{display:none}
			.xt-switch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;font-size:12px;color:var(--text-muted);user-select:none;margin:0}
			.xt-switch input{position:absolute;opacity:0;width:0;height:0}
			.xt-knob{width:34px;height:20px;border-radius:10px;background:var(--gray-400);position:relative;transition:background .15s}
			.xt-knob::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:var(--white);transition:left .15s}
			.xt-switch.on .xt-knob{background:var(--green-500)} .xt-switch.on .xt-knob::after{left:16px}
			.xt-switch.locked{cursor:not-allowed} .xt-switch.locked .xt-knob{opacity:.6}
			.xt-switch input:focus-visible + .xt-knob{outline:2px solid var(--blue-500);outline-offset:2px}
			.xt-more .dropdown-item{cursor:pointer}
			.xt-danger-text{color:var(--red-600)}
			.xt-kv{display:grid;grid-template-columns:90px 1fr;gap:6px 12px;font-size:13px;margin:10px 0}
			.xt-kv .k{color:var(--text-muted);font-weight:600} .xt-kv .v{line-height:1.45}
			.xt-empty{text-align:center;padding:48px 16px;border:1px dashed var(--border-color);border-radius:8px}
			.xt-empty-t{font-weight:600;font-size:14px} .xt-empty-d{font-size:13px;color:var(--text-muted);margin-top:6px;max-width:52ch;margin-inline:auto}
			@media (max-width:768px){
				.xt-card{grid-template-columns:36px 1fr}
				.xt-actions{grid-column:1 / -1;flex-direction:row;flex-wrap:wrap;align-items:center;justify-content:flex-start}
				.xt-switch{display:none}
				.xt-btnrow{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:8px}
				.xt-mobile-toggle{display:inline-block}
				.xt-actions .xt-btn,.xt-confirm .xt-btn,.xt-drop{min-height:44px}
				.xt-confirm{grid-template-columns:1fr} .xt-confirm .xt-btn{width:100%}
				.xt-btnrow .xt-btn,.xt-btnrow .dropdown{min-height:44px;min-width:44px;width:100%}
				.xt-btnrow .dropdown > .xt-btn{width:100%;height:44px}
				.xt-kv,.xt-kv.xt-permdiff{grid-template-columns:1fr}
				.xt-git-row{grid-template-columns:1fr}
				.xt-git .xt-btn,.xt-copy,.xt-link{min-height:44px;min-width:44px}
				.xt-git .xt-btn{width:100%}
				.xt-fold summary{padding-block:13px}
				.xt-dialog .modal-footer .btn{min-height:44px}
				.xt-dialog .modal-header .btn-modal-close{min-height:44px;min-width:44px}
			}`;
	}
};

// Node unit tests import the pure kernel. Harmless in the browser, where `module` is undefined.
if (typeof module !== 'undefined' && module.exports) {
	module.exports = {
		xtCardState, xtBusyKind, xtRestartBanner, xtPaused, xtSplitBackup, xtFailedNote, xtAgentFailure, xtQuietPoll, xtOutage,
		xtReviewMode, xtPermissionDiff, xtStagingSettled, xtTargetVersion, xtUpdateOffer, xtUpdateServiceMissing,
	};
}
