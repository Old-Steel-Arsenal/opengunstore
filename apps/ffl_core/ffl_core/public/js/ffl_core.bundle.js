// FFL Core: Desk-side extensions.
//
// 1. Redirect /app, /app/home and the Gun Shop workspace to the Overview page
//    (/desk/gun-shop/store-overview) instead of ERPNext's default Apps tile selector.
// 2. Navbar language toggle (EN ↔ zh) for one-click switching of the
//    current user's language preference.
// 3. POS-page Trade-in button injection (Spike 3 / Phase 0). Uses a
//    MutationObserver so it survives ERPNext POS DOM changes; we avoid
//    monkey-patching ERPNext's POS controller class so future upgrades
//    don't break the integration.

// ===========================================================================
// ffl.FirearmItemPicker — reusable item search (local + RSR catalog + promote/
// create). Extracted from the Receive Goods page so the same search powers both
// the receive page and the POS trade-in dialog. Owns its dropdown DOM, search,
// keyboard nav and the promote/create dialogs; the consumer supplies the input,
// a dropdown element, and onSelect(item) (item = the picked / promoted / created
// item dict: item_code, item_name, is_firearm, has_serial_no, last_rate,
// uniform_sell_price). Backend methods are unchanged.
// ===========================================================================
frappe.provide('ffl');

// ffl.open4473Pdf — open a 4473's signed PDF (a Form 4473 Log row) through the viewer
// (ffl_integrations.fastbound.pos_4473.view_4473_pdf; the stored FastBound link
// needs the API key, so a browser opening it directly gets a 401). The tab opens
// on the click itself (popup blockers) and is pointed at the PDF once it
// arrives; a refusal — no PDF, FastBound down, no 4473 role — closes the tab and
// shows the server's message instead of a raw JSON error page.
ffl.open4473Pdf = async (log) => {
	const tab = window.open('', '_blank');
	const fail = (message) => {
		if (tab) tab.close();
		frappe.msgprint({ title: __('4473 PDF'), indicator: 'red', message });
	};
	try {
		const res = await fetch('/api/method/ffl_integrations.fastbound.pos_4473.view_4473_pdf?log='
			+ encodeURIComponent(log));
		if (res.ok && (res.headers.get('Content-Type') || '').includes('application/pdf')) {
			const url = URL.createObjectURL(await res.blob());
			// Kept long enough to reload the tab or save the file from it.
			setTimeout(() => URL.revokeObjectURL(url), 30 * 60 * 1000);
			if (tab) {
				tab.location = url;
			} else {
				// The popup was blocked: a second window.open here runs outside the
				// click and would be blocked too — hand over a link to click instead.
				frappe.msgprint({ title: __('4473 PDF'), message:
					`<a href="${url}" target="_blank" rel="noopener">${__('Open the signed 4473')}</a>` });
			}
			return;
		}
		const body = await res.json().catch(() => ({}));
		let messages = [];
		try {
			messages = JSON.parse(body._server_messages || '[]').map((m) => JSON.parse(m).message);
		} catch (e) { /* fall through to the generic text */ }
		fail(messages.join('<br>') || __('The 4473 PDF could not be opened.'));
	} catch (e) {
		fail(__('Could not reach the server. Check the connection and try again.'));
	}
};

// Canonical firearm-type vocabulary for Select controls (bound-book / ATF
// wording, NFA classes included). Mirrors ffl_core.firearm.ATF_FIREARM_TYPES —
// test_nfa_firearm_types pins the two together, so edit both or neither.
ffl.FIREARM_TYPE_OPTIONS = ['', 'Pistol', 'Revolver', 'Rifle', 'Shotgun',
	'Receiver', 'Other Firearm', 'Silencer', 'Short Barreled Rifle',
	'Short Barreled Shotgun', 'Machine Gun', 'Destructive Device',
	'Any Other Weapon'].join('\n');

// The Trade-in dialog's firearm fields a picked existing Item fills: its model facts (receive_goods.search_items /
// lookup_item carry them), as [fieldname, value] pairs, blank facts skipped so a field typed by hand keeps its value.
ffl.TRADE_IN_MODEL_FIELDS = ['manufacturer', 'model_name', 'caliber', 'firearm_type'];
ffl.tradeInModelFill = function (item) {
	return ffl.TRADE_IN_MODEL_FIELDS.filter((f) => item && item[f]).map((f) => [f, item[f]]);
};

ffl.FirearmItemPicker = class FirearmItemPicker {
	constructor(opts) {
		this.$input = opts.$input;
		this.$dropdown = opts.$dropdown;
		this.onSelect = opts.onSelect || (() => {});
		// Optional: when an Enter-scan matches no item (barcode/SKU miss), hand
		// the raw term to the consumer instead of alerting — Create Order uses
		// this to fall back to a firearm serial-number lookup.
		this.onNoMatch = opts.onNoMatch || null;
		// Optional companion to onNoMatch: an async (term) => row|null probe that
		// SILENTLY checks whether the term is a sellable firearm serial. A hit is
		// shown as the FIRST suggestion while typing (staff hand-keying a serial
		// expect a dropdown, not an Enter-only shortcut); picking it routes the
		// serial through onNoMatch, same as the Enter path.
		this.serialLookup = opts.serialLookup || null;
		this._serialHit = null;
		this.includeRsr = opts.includeRsr !== false;
		// Remote (distributor catalog) behavior — all opt-in, so the two legacy
		// consumers (Receive Goods, POS trade-in) keep their exact behavior:
		//   remoteAlways: fetch the remote catalog IN PARALLEL with the local
		//     search and show both groups (Create Order's unified search).
		//     Default (false) = legacy fallback: remote only when local misses.
		//   remoteSearch(term, limit) => Promise<rows>: override the remote
		//     source (Create Order passes the provider-agnostic
		//     distributor.api.search; default stays the rsr.search shim).
		//   onRemoteSelect(row): route a picked remote row (normalized shape)
		//     instead of opening the promote dialog.
		this.remoteAlways = !!opts.remoteAlways;
		this.remoteSearch = opts.remoteSearch || null;
		this.onRemoteSelect = opts.onRemoteSelect || null;
		this.includeCreate = opts.includeCreate !== false;
		this.results = [];
		this.active = -1;
		this.lastQuery = '';
		this.$dropdown.addClass('ffl-picker-dd');
		ffl.FirearmItemPicker._injectCss();
		this._wire();
	}

	// Is the distributor catalog off here? (ffl.capabilities: capability_notice.js)
	static catalogOff() {
		return !!(window.ffl && ffl.capabilities && ffl.capabilities.isOff('catalog'));
	}

	static _injectCss() {
		if (document.getElementById('ffl-picker-css')) return;
		const css = `
			.ffl-picker-dd { position:absolute; top:100%; left:0; right:0; z-index:100;
				background:var(--card-bg); border:1px solid var(--border-color); border-radius:6px;
				box-shadow:0 8px 24px rgba(0,0,0,0.08); max-height:320px; overflow-y:auto; margin-top:4px; display:none; }
			.ffl-picker-dd .typeahead-row { padding:8px 12px; cursor:pointer; display:grid;
				grid-template-columns:1fr auto; gap:8px; align-items:center; border-bottom:1px solid var(--border-color); }
			.ffl-picker-dd .typeahead-row:last-child { border-bottom:none; }
			.ffl-picker-dd .typeahead-row:hover, .ffl-picker-dd .typeahead-row.active { background:var(--fg-hover-color); }
			.ffl-picker-dd .typeahead-row .name { font-weight:500; }
			.ffl-picker-dd .typeahead-row .meta { font-size:11px; color:var(--text-muted); }
			.ffl-picker-dd .typeahead-row .right { text-align:right; }
			.ffl-picker-dd .typeahead-row .firearm-pill { background:var(--blue-100); color:var(--blue-700);
				padding:1px 6px; border-radius:4px; font-size:10px; margin-right:6px; }
			.ffl-picker-dd .typeahead-empty { padding:14px; color:var(--text-muted); font-size:12px; text-align:center; }
			.ffl-picker-dd .typeahead-create { background:var(--bg-green); }
			.ffl-picker-dd .typeahead-create:hover { background:var(--bg-green); }
			.ffl-picker-dd .typeahead-row .thumb { width:36px; height:36px; object-fit:cover; border-radius:4px;
				border:1px solid var(--border-color); margin-right:10px; background:var(--bg-light-gray); }
			.ffl-picker-dd .typeahead-row .thumb-wrap { display:flex; align-items:center; }
			.ffl-picker-dd .typeahead-row .rsr-pill { background:var(--orange-100,#fde68a); color:var(--orange-700,#92400e);
				padding:1px 6px; border-radius:4px; font-size:10px; margin-right:6px; }
			.ffl-picker-dd .typeahead-section-divider { padding:6px 12px; font-size:10px; font-weight:600;
				color:var(--text-muted); background:var(--bg-light-gray); }
		`;
		const style = document.createElement('style');
		style.id = 'ffl-picker-css';
		style.textContent = css;
		document.head.appendChild(style);
	}

	_q() { return (this.$input.val() || '').trim(); }

	_wire() {
		this.$input.on('input', () => {
			clearTimeout(this._debounce);
			this._debounce = setTimeout(() => this._run(), 200);
		});
		this.$input.on('keydown', (e) => {
			const open = this.$dropdown.is(':visible') && this.results.length;
			if (e.key === 'Enter') {
				e.preventDefault();
				if (open && this.active >= 0) this._pick(this.results[this.active]);
				else if (open && this.results.length === 1) this._pick(this.results[0]);
				else this._scan();
				return;
			}
			if (!open) return;
			if (e.key === 'ArrowDown') {
				e.preventDefault();
				this.active = Math.min(this.active + 1, this.results.length - 1);
				this._render(this.results);
			} else if (e.key === 'ArrowUp') {
				e.preventDefault();
				this.active = Math.max(this.active - 1, 0);
				this._render(this.results);
			} else if (e.key === 'Escape') {
				this._close();
			}
		});
		this._docClick = (e) => {
			if (!$(e.target).closest(this.$dropdown).length
				&& !$(e.target).closest(this.$input).length) this._close();
		};
		$(document).on('click.ffl-picker', this._docClick);
	}

	_run() {
		const q = this._q();
		if (!q) { this._close(); return; }
		if (q === this.lastQuery) return;
		this.lastQuery = q;
		// Sequence token: two chained async calls race against fast typing — a
		// later query can resolve its local search before an earlier query's
		// slow RSR callback returns and overwrites the dropdown with stale rows.
		// Bail in each callback if a newer _run has since started.
		const seq = (this._seq = (this._seq || 0) + 1);
		this._lastLocal = null;
		this._lastRsr = null;
		// No distributor catalog on this site (frappe.boot.ffl_capabilities): the local
		// search runs as always, the distributor group is the "not set up" notice.
		this._catalogOff = this.includeRsr && ffl.FirearmItemPicker.catalogOff();
		if (this._catalogOff) this._lastRsr = [];
		if (this.serialLookup && this.onNoMatch && q.length >= 3) {
			Promise.resolve(this.serialLookup(q)).then((row) => {
				if (seq !== this._seq || !row) return;
				this._serialHit = { q, row: { ...row, _source: 'serial' } };
				this._compose();
			});
		}
		const fireRemote = () => {
			const fetch = this.remoteSearch
				? Promise.resolve(this.remoteSearch(q, 10))
				: new Promise((resolve) => frappe.call({
					method: 'ffl_integrations.rsr.search.search',
					args: { query: q, limit: 10 },
					callback: (rr) => resolve(rr.message || []),
					error: () => resolve([]),
				}));
			fetch.then((rows) => {
				if (seq !== this._seq) return;
				this._lastRsr = (rows || []).map((x) => this._normalizeRemote(x));
				this._compose();
			}).catch(() => {
				if (seq !== this._seq) return;
				this._lastRsr = [];
				this._compose();
			});
		};
		if (this.remoteAlways && this.includeRsr && !this._catalogOff) fireRemote();
		frappe.call({
			method: 'ffl_core.api.receive_goods.search_items',
			args: { query: q, limit: 10 },
			callback: (r) => {
				if (seq !== this._seq) return;
				this._lastLocal = (r.message || []).map((x) => ({ ...x, _source: 'local' }));
				// Legacy fallback mode: remote fires only when local misses, and the
				// dropdown waits for it (no "No matches" flash). Parallel mode
				// composes what it has — the remote group appends when it lands.
				const fallbackPending = !this.remoteAlways && this.includeRsr && !this._catalogOff
					&& !this._lastLocal.length;
				if (fallbackPending) { fireRemote(); return; }
				this._compose();
			},
		});
	}

	// Remote rows arrive in two shapes: the legacy rsr.search shim
	// (rsr_stock_number/dealer_price/quantity — the default source) and the
	// provider-agnostic distributor.api.search (sku/cost/qty + a `distributor`
	// provenance tag). One normalized shape downstream, so _render/_openPromote/
	// onRemoteSelect never care which endpoint answered.
	_normalizeRemote(r) {
		return {
			_source: 'remote',
			sku: r.sku || r.rsr_stock_number || '',
			item_name: r.item_name || r.short_description || '',
			manufacturer: r.manufacturer || '',
			model: r.model || '',
			upc: r.upc || '',
			image_url: r.image_url || '',
			department: (r.department != null && r.department !== '')
				? r.department : (r.department_number || ''),
			cost: (r.cost != null) ? r.cost : r.dealer_price,
			map: (r.map != null) ? r.map : r.map_price,
			msrp: r.msrp,
			qty: (r.qty != null) ? r.qty : r.quantity,
			ffl_required: !!r.ffl_required,
			// The law's answer (the service class); ffl_required is the house's rule.
			is_firearm: (r.is_firearm != null) ? !!r.is_firearm : !!r.ffl_required,
			distributor: r.distributor || '',
			// Unnamed (product) rows: every enabled house carrying the UPC.
			sources: r.sources || [],
			offers: r.offers || [],
		};
	}

	// Item rows and the async serial probe resolve in either order; compose the
	// dropdown from whatever has arrived so neither callback clobbers the other.
	_compose() {
		const serialRow = (this._serialHit && this._serialHit.q === this.lastQuery)
			? [this._serialHit.row] : [];
		this.results = [...serialRow, ...(this._lastLocal || []), ...(this._lastRsr || [])];
		this.active = this.results.length ? 0 : -1;
		this._render(this.results);
	}

	_render(rows) {
		const $td = this.$dropdown;
		// Parallel mode: the remote group may still be in flight while local
		// already answered — say "searching" instead of a premature "No matches".
		const remotePending = this.remoteAlways && this.includeRsr && this._lastRsr === null;
		// The catalog is off: its notice stands where "No matches." / the distributor group would.
		const offNotice = this._catalogOff ? ffl.capabilities.notice('catalog', { inline: true }) : '';
		if (!rows.length) {
			const empty = offNotice || `<div class="typeahead-empty">${remotePending
				? __('Searching the drop-ship catalog…') : __('No matches.')}</div>`;
			$td.html(this.includeCreate ? empty + this._createRowHtml() : empty).show();
			$td.find('[data-create]').on('click', () => this._openCreate());
			return;
		}
		let html = '';
		let inRsrSection = false;
		rows.forEach((r, i) => {
			if (r._source === 'serial') {
				const a = i === this.active ? 'active' : '';
				const price = r.sell_price
					? frappe.format(r.sell_price, { fieldtype: 'Currency' }) : '';
				html += `
					<div class="typeahead-row ${a}" data-idx="${i}">
						<div>
							<span class="firearm-pill">${__('FIREARM')}</span><span class="firearm-pill" style="background:var(--green-100);color:var(--green-700)">${__('SN MATCH')}</span><span class="name">${frappe.utils.escape_html(r.item_name || r.item_code)}</span>
							<div class="meta">${__('Serial')} ${frappe.utils.escape_html(r.serial)} · ${__('adds this exact gun')}</div>
						</div>
						<div class="right meta">${price}</div>
					</div>
				`;
				return;
			}
			if (r._source === 'remote' && !inRsrSection) {
				// Provenance-tagged rows (distributor.api.search) name their house;
				// the legacy shim keeps the historical receive-flow wording.
				html += `<div class="typeahead-section-divider">${r.distributor
					? __('{0} — drop-ship catalog', [frappe.utils.escape_html(r.distributor)])
					: __('Catalog API — not in local stock')}</div>`;
				inRsrSection = true;
			}
			const active = i === this.active ? 'active' : '';
			const isRsr = r._source === 'remote';
			const pillFirearm = (r.is_firearm ?? r.ffl_required) ? `<span class="firearm-pill">${__('FIREARM')}</span>` : '';
			const pillRsr = isRsr
				? `<span class="rsr-pill">${frappe.utils.escape_html(r.distributor || __('API'))}</span>` : '';
			const nameText = isRsr
				? (r.item_name || (r.manufacturer + ' ' + (r.model || '')).trim() || r.sku)
				: r.item_name;
			// Unnamed (API) rows lead with the UPC already in the meta line; no code prefix.
			const codeText = isRsr ? (r.distributor ? r.sku : '') : r.item_code;
			const metaSuffix = isRsr
				? [(r.upc ? `${__('UPC')}: ${frappe.utils.escape_html(r.upc)}` : ''),
					(r.manufacturer ? frappe.utils.escape_html(r.manufacturer) : ''),
					(!r.distributor && (r.sources || []).length
						? __('{0} source(s): {1}', [r.sources.length, frappe.utils.escape_html(r.sources.join(', '))]) : ''),
					`${__('qty')} ${cint(r.qty)}`].filter(Boolean).join(' · ')
				: (r.item_group ? frappe.utils.escape_html(r.item_group) : '');
			const rate = isRsr
				? (r.cost ? frappe.format(r.cost, { fieldtype: 'Currency' }) : '')
				: (r.last_rate ? frappe.format(r.last_rate, { fieldtype: 'Currency' }) : '');
			const thumb = isRsr && r.image_url
				? `<img class="thumb" src="${frappe.utils.escape_html(r.image_url)}" loading="lazy" onerror="this.style.visibility='hidden'">`
				: '';
			html += `
				<div class="typeahead-row ${active}" data-idx="${i}">
					<div class="thumb-wrap">
						${thumb}
						<div>
							${pillRsr}${pillFirearm}<span class="name">${frappe.utils.escape_html(nameText)}</span>
							<div class="meta">${frappe.utils.escape_html(codeText)}${metaSuffix ? (codeText ? ' · ' : '') + metaSuffix : ''}</div>
						</div>
					</div>
					<div class="right meta">${rate}</div>
				</div>
			`;
		});
		html += offNotice;
		// Always offer "create new" at the bottom, even when there ARE results —
		// the operator may want a brand-new item (e.g. a "… USED" model).
		if (this.includeCreate) html += this._createRowHtml();
		$td.html(html).show();
		$td.find('.typeahead-row[data-idx]').on('click', (e) => {
			const idx = parseInt($(e.currentTarget).data('idx'), 10);
			this._pick(rows[idx]);
		});
		$td.find('[data-create]').on('click', () => this._openCreate());
		const $active = $td.find('.typeahead-row.active');
		if ($active.length) $active[0].scrollIntoView({ block: 'nearest' });
	}

	_pick(row) {
		if (row && row._source === 'serial') {
			// Route through the consumer's serial handler — same path as an
			// Enter-scan miss, so add-by-serial has exactly one implementation.
			this._close();
			this.$input.val('');
			this.lastQuery = '';
			this._serialHit = null;
			this.onNoMatch(row.serial);
			return;
		}
		if (row && row._source === 'remote') {
			if (this.onRemoteSelect) {
				// Create Order routes remote picks to its drop-ship pending card;
				// close + clear FIRST so the card owns the focus that follows.
				this._close();
				this.$input.val('');
				this.lastQuery = '';
				this.onRemoteSelect(row);
				return;
			}
			this._openPromote(row);
			return;
		}
		this._finishCreated(row);  // local item — same close + clear + emit
	}

	// Public: the Create Order drop-ship card's "sell as a local line" path
	// re-enters the SAME promote dialog a remote pick would open by default.
	openPromote(row) { this._openPromote(row); }

	_finishCreated(message) {
		this._close();
		this.$input.val('');
		this.lastQuery = '';
		this.onSelect(message);
	}

	_createRowHtml() {
		const q = frappe.utils.escape_html(this.lastQuery || '');
		return `<div class="typeahead-row typeahead-create" data-create="1">
				<div><span class="firearm-pill" style="background:var(--green-100);color:var(--green-700)">${__('NEW')}</span><span class="name">${__('Create new item')}${q ? ': "' + q + '"' : ''}</span></div>
				<div class="right meta">＋</div>
			</div>`;
	}

	_openPromote(row) {
		const itemName = row.item_name
			|| ((row.manufacturer || '') + ' ' + (row.model || '')).trim()
			|| row.sku;
		// The law's answer (the service class), not RSR's dealer-only shipping flag.
		const serialDefault = (row.is_firearm ?? row.ffl_required) ? 1 : 0;
		const dialog = new frappe.ui.Dialog({
			title: __('Create Item from RSR'),
			size: 'small',
			fields: [
				{ fieldtype: 'HTML', fieldname: 'preview' },
				{ label: __('Item Code (SKU)'), fieldname: 'item_code', fieldtype: 'Data', reqd: 1, default: row.sku },
				{ label: __('Item Name'), fieldname: 'item_name', fieldtype: 'Data', default: itemName },
				{ label: __('Item Group'), fieldname: 'item_group', fieldtype: 'Link', options: 'Item Group',
					description: __('Leave blank to file it under the catalog\'s category; set only to override.') },
				{ label: __('Has Serial No (track per unit)'), fieldname: 'has_serial_no', fieldtype: 'Check',
					default: serialDefault,
					description: __('Always on for firearm groups (incl. NFA) — the server forces it; only meaningful for non-firearm stock.') },
				{ label: __('Uniform Selling Price'), fieldname: 'uniform_sell_price', fieldtype: 'Check', default: 1,
					description: __('When checked (recommended for RSR SKUs — one price for many like units), all units share the Selling Price below. Uncheck for per-gun pricing entered at receive.') },
				{ label: __('Selling Price (MAP / MSRP)'), fieldname: 'standard_rate', fieldtype: 'Currency',
					default: row.map || row.msrp || 0,
					depends_on: 'eval:doc.uniform_sell_price',
					mandatory_depends_on: 'eval:doc.uniform_sell_price',
					description: __('Item.standard_rate — only used in Uniform mode. Defaults to MAP (RSR-enforced floor), else MSRP. In per-gun mode the price lives on each Serial No.') },
			],
			primary_action_label: __('Create & Add'),
			primary_action: (values) => {
				const uniform = values.uniform_sell_price ? 1 : 0;
				frappe.call({
					method: 'ffl_integrations.rsr.promote.promote_to_item',
					args: { payload: JSON.stringify({
						rsr_stock_number: row.sku,
						item_code: values.item_code,
						item_name: values.item_name,
						item_group: values.item_group || undefined,
						// Only an operator's change overrides the server's own inference.
						has_serial_no: (values.has_serial_no ? 1 : 0) !== serialDefault ? (values.has_serial_no ? 1 : 0) : undefined,
						standard_rate: uniform ? (values.standard_rate || undefined) : undefined,
						uniform_sell_price: uniform,
					}) },
					callback: (r) => {
						if (r.message?.found) { dialog.hide(); this._finishCreated(r.message); }
					},
				});
			},
		});
		dialog.show();
		const $preview = dialog.fields_dict.preview.$wrapper;
		const thumb = row.image_url
			? `<img src="${frappe.utils.escape_html(row.image_url)}" style="width:100px;height:100px;object-fit:cover;border-radius:6px;border:1px solid var(--border-color);">`
			: '';
		$preview.html(`
			<div style="display:flex;gap:14px;align-items:flex-start;padding:8px 0 14px 0;border-bottom:1px solid var(--border-color);margin-bottom:14px">
				${thumb}
				<div style="flex:1">
					<div style="font-weight:600">${frappe.utils.escape_html(itemName)}</div>
					<div style="font-size:11px;color:var(--text-muted);margin-top:4px">
						${frappe.utils.escape_html(row.sku)}
						${row.upc ? ' · UPC ' + frappe.utils.escape_html(row.upc) : ''}
						${row.manufacturer ? ' · ' + frappe.utils.escape_html(row.manufacturer) : ''}
					</div>
				</div>
			</div>
		`);
	}

	_openCreate() {
		const seed = (this.lastQuery || '').trim();
		const dialog = new frappe.ui.Dialog({
			title: __('Create New Item'),
			size: 'small',
			fields: [
				{ label: __('Item Code (SKU)'), fieldname: 'item_code', fieldtype: 'Data', reqd: 1, default: seed },
				{ label: __('Item Name'), fieldname: 'item_name', fieldtype: 'Data', default: seed },
				{ label: __('Item Group'), fieldname: 'item_group', fieldtype: 'Link', options: 'Item Group', reqd: 1 },
				{ label: __('Has Serial No (track per unit)'), fieldname: 'has_serial_no', fieldtype: 'Check',
					description: __('Always on for firearm groups (incl. NFA, used guns) — the server forces it. Off only for non-serialized stock.') },
				{ label: __('Stock UoM'), fieldname: 'stock_uom', fieldtype: 'Link', options: 'UOM', default: 'Nos' },
				{ label: __('Uniform Selling Price'), fieldname: 'uniform_sell_price', fieldtype: 'Check',
					description: __('When checked, all serials of this item sell at the Default Selling Price below. Uncheck (default) for per-gun pricing entered at receive.') },
				{ label: __('Default Selling Price'), fieldname: 'standard_rate', fieldtype: 'Currency',
					depends_on: 'eval:doc.uniform_sell_price',
					mandatory_depends_on: 'eval:doc.uniform_sell_price',
					description: __('Item.standard_rate — only used in Uniform mode. In per-gun mode the price lives on each Serial No, not the Item.') },
				{ label: __('Barcode (optional)'), fieldname: 'barcode', fieldtype: 'Data' },
			],
			primary_action_label: __('Create & Add'),
			primary_action: (values) => {
				if (!values.uniform_sell_price) values.standard_rate = 0;
				frappe.call({
					method: 'ffl_core.api.receive_goods.create_item',
					args: { payload: JSON.stringify(values) },
					callback: (r) => {
						if (r.message?.found) { dialog.hide(); this._finishCreated(r.message); }
					},
				});
			},
		});
		dialog.show();
	}

	_scan() {
		const v = this._q();
		if (!v) return;
		frappe.call({
			method: 'ffl_core.api.receive_goods.lookup_item',
			args: { scan: v },
			callback: (r) => {
				const m = r.message || {};
				if (!m.found) {
					if (this.onNoMatch) { this.onNoMatch(v); return; }
					// A barcode no Item carries: ask the distributor catalogs for the
					// UPC and, when a house lists it, open the promote dialog pre-filled
					// — the receiver creates the Item from the catalog instead of typing
					// it (scan-first receiving). Not a UPC shape → the old alert.
					if (/^\d{8,14}$/.test(v) && ffl.FirearmItemPicker.catalogOff()) {
						// No catalog to ask: say why instead of a bare "not found".
						frappe.show_alert({ message: `${__('No item found for: {0}', [frappe.utils.escape_html(v)])}`
							+ ffl.capabilities.notice('catalog'), indicator: 'orange' }, 8);
						return;
					}
					if (/^\d{8,14}$/.test(v)) {
						frappe.call({
							method: 'ffl_integrations.distributor.api.catalog_by_upc',
							args: { upc: v },
							callback: (cr) => {
								const rows = cr.message || [];
								if (rows.length) { this._openPromote(this._normalizeRemote(rows[0])); return; }
								frappe.show_alert({ message: __('No item found for: {0}', [v]), indicator: 'orange' });
							},
							error: () => frappe.show_alert({ message: __('No item found for: {0}', [v]), indicator: 'orange' }),
						});
						return;
					}
					frappe.show_alert({ message: __('No item found for: {0}', [v]), indicator: 'orange' });
					return;
				}
				this._finishCreated(m);
			},
		});
	}

	close() { this._close(); }  // public: dismiss the dropdown from a consumer

	_close() {
		// Kill in-flight search callbacks (parallel remote / serial probe): a
		// late compose after a pick or dismiss must not re-open the dropdown.
		this._seq = (this._seq || 0) + 1;
		this.$dropdown.hide().empty();
		this.results = [];
		this.active = -1;
		this.lastQuery = '';
	}

	destroy() {
		clearTimeout(this._debounce);
		$(document).off('click.ffl-picker', this._docClick);
	}
};

(function () {
	'use strict';

	// --- 1. Default-landing redirect ---
	// Frappe v16 normalises /app → /desk, so we also catch the /desk variants.
	// Only redirect from "no specific route" landings — deep links keep working.
	const NEUTRAL_ROUTES = new Set([
		'/', '/app', '/app/', '/app/home',
		'/desk', '/desk/', '/desk/home',
	]);
	// The landing is the Overview page in the Gun Shop shell (hooks.home_page). The shell's own
	// workspace (/desk/gun-shop: old bookmarks, the app tile on a site migrated before the change)
	// is sent there too, through the router's own re-route table, so in-desk navigation is caught
	// as well as a page load.
	const LANDING = '/desk/gun-shop/store-overview';
	if (window.frappe) {
		frappe.re_route = frappe.re_route || {};
		frappe.re_route['gun-shop'] = LANDING;
		// The workspace spelled inside each shell this user has (/desk/settings/gun-shop: the Settings
		// sidebar's Gun Shop row) is re-routed before the old workspace draws.
		Object.keys((frappe.boot && frappe.boot.module_sidebars) || {}).forEach((shell) => {
			frappe.re_route[`${frappe.router.shell_slug(shell)}/gun-shop`] = LANDING;
		});
		// Any other spelling is caught once it has routed, and replaced in history so Back does not
		// land on it again.
		if (frappe.router && frappe.router.on) frappe.router.on('change', () => {
			const route = frappe.get_route() || [];
			if (route[0] === 'Workspaces' && route[1] === 'Gun Shop') {
				frappe.route_flags.replace_route = true;
				frappe.set_route(LANDING);
			}
		});
	}
	function maybeRedirectToGunShop() {
		if (!NEUTRAL_ROUTES.has(location.pathname)) return;
		if (location.hash && location.hash !== '#' && !location.hash.startsWith('#workspace')) return;
		location.replace(LANDING);
	}
	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', maybeRedirectToGunShop);
	} else {
		maybeRedirectToGunShop();
	}

	// --- 1b. Dark-theme brand logo ---
	// Navbar Settings has one Application Logo; a black-on-white mark sits as a white box on the
	// dark desk. The Dark Mode Logo field (frappe.boot.app_logo_dark) replaces it under the dark
	// theme, CSS-only: the original image is pushed out of its own box and the dark one painted as
	// its background, so it follows a theme switch with no re-render. #brand-logo is the logo on
	// the /desk landing (frappe desktop.html / desktop_icons.html).
	if (window.frappe && frappe.boot && frappe.boot.app_logo_dark) {
		const style = document.createElement('style');
		style.textContent = `[data-theme="dark"] #brand-logo {
			object-position: -99999px 0;
			background: url(${JSON.stringify(frappe.boot.app_logo_dark)}) center / contain no-repeat;
		}`;
		document.head.appendChild(style);
	}

	// --- 2. Language toggle ---
	// Inject a small EN ↔ zh button into the navbar next to the bell icon.
	// One click flips the current user's language preference and reloads.
	const LANG_TOGGLE_ID = 'ffl-lang-toggle';
	// Order is significant: nextLang() rotates through this array.
	const LANG_CYCLE = Object.freeze([
		Object.freeze({ code: 'en', short: 'EN', full: 'English' }),
		Object.freeze({ code: 'zh', short: '中', full: '简体中文' }),
	]);

	function nextLang() {
		const lang = (window.frappe && frappe.boot && frappe.boot.lang) || 'en';
		const i = Math.max(0, LANG_CYCLE.findIndex((l) => lang === l.code || lang.toLowerCase().startsWith(l.code)));
		return LANG_CYCLE[(i + 1) % LANG_CYCLE.length];
	}

	function injectLanguageToggle() {
		if (document.getElementById(LANG_TOGGLE_ID)) return true;
		// Anchor: the bell's parent. We probe a few selectors so future
		// ERPNext re-skins still find a host.
		const bell =
			document.querySelector('.desktop-notifications') ||
			document.querySelector('.dropdown-notifications');
		const host = bell?.parentElement
			|| document.querySelector('.dropdown-notifications')?.closest('.flex, .navbar-collapse, header');
		if (!host) return false;

		const btn = document.createElement('button');
		btn.id = LANG_TOGGLE_ID;
		btn.type = 'button';
		btn.className = 'btn-reset nav-link text-muted ffl-lang-toggle';
		const target = nextLang();
		btn.title = `Switch to ${target.full}`;
		btn.style.cssText =
			'padding:6px 10px;border-radius:6px;font-weight:600;font-size:12px;line-height:1;cursor:pointer;color:var(--text-muted);';
		btn.textContent = target.short;
		btn.addEventListener('mouseover', () => (btn.style.background = 'var(--fg-hover-color)'));
		btn.addEventListener('mouseout', () => (btn.style.background = ''));
		btn.addEventListener('click', switchLanguage);

		// Visual order: [EN/zh]  [🔔]  [⌘K]  [user]
		(bell || host.firstChild)?.parentElement
			? bell.parentElement.insertBefore(btn, bell)
			: host.insertBefore(btn, host.firstChild);
		return true;
	}

	function switchLanguage() {
		const target = nextLang();
		const user = frappe.session && frappe.session.user;
		if (!user || user === 'Guest') {
			location.reload();
			return;
		}
		frappe.db.set_value('User', user, 'language', target.code).then(() => {
			// Bust the per-language client-side translation cache.
			try { window.localStorage?.removeItem('translations'); } catch (e) { /* ignore */ }
			location.reload();
		});
	}

	function startLangToggleObserver() {
		if (injectLanguageToggle()) return;
		// Anchor wasn't there yet — watch for it and disconnect once injected.
		const observer = new MutationObserver(() => {
			if (injectLanguageToggle()) observer.disconnect();
		});
		observer.observe(document.body, { childList: true, subtree: true });
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', startLangToggleObserver);
	} else {
		startLangToggleObserver();
	}

	// --- 4. Version + What's New ---
	// Frappe's own change log (frappe.utils.change_log): one markdown note per
	// release under ffl_core/change_log/v<major>/, diffed per user against
	// User.last_known_versions and pushed into frappe.boot.change_log. desk.js
	// only pops it for System Manager (show_notices), so fire the same native
	// dialog for everyone else once the desk is up — closing it records the
	// version on the user (native custom_onhide), so it shows once per person.
	// Sidebar-header menu → "What's New in POS" (hooks.standard_navbar_items) re-opens every
	// note (same markdown, no banner).
	// frappe.boot.change_log carries one entry per app whose version moved,
	// ERPNext's and Frappe's included — developer release notes with PR
	// numbers, not for the counter. Keep only ours (hooks.app_title). Done on
	// app_ready, before desk.js's 1 s timer fires the native popup for
	// System Manager, so admins see the same trimmed dialog.
	const OUR_APP_TITLE = 'OpenGunstore';
	function keepOnlyOurChangeLog() {
		if (!window.frappe || !Array.isArray(frappe.boot?.change_log)) return;
		frappe.boot.change_log = frappe.boot.change_log.filter((e) => e.title === OUR_APP_TITLE);
	}
	function showChangeLogForNonAdmins() {
		if (!window.frappe || !frappe.app || typeof frappe.app.show_change_log !== 'function') return;
		if ((frappe.user_roles || []).includes('System Manager')) return; // native path already does it
		frappe.app.show_change_log();
	}
	$(document).on('app_ready', () => {
		keepOnlyOurChangeLog();
		setTimeout(showChangeLogForNonAdmins, 1000);
	});

	window.gunstore = window.gunstore || {};
	// renderReleaseNotes: the releaseNotes block below this IIFE (shared with the Extensions page).
	window.gunstore.showWhatsNew = function () {
		frappe.xcall('ffl_core.api.changelog.get_changelog').then((r) => {
			const entries = (r && r.entries) || [];
			// Every note, newest first — the same markdown the login popup shows,
			// without the popup's "updated to" banner (this is the archive).
			const message = entries.length
				? window.gunstore.renderReleaseNotes(entries)
				: __('No release notes yet.');
			frappe.msgprint({
				title: __("What's New in POS (v{0})", [r.version]),
				message,
				wide: true,
			});
		}).catch(() => frappe.show_alert({ message: __('Could not load the release notes.'), indicator: 'red' }));
	};

	const BUTTON_ID = 'ffl-trade-in-btn';
	// Who sees the Trade-in button: roles.TRADE_IN_ROLES, mirrored (the server gate on
	// trade_in.create_trade_in_intake; test_trade_in_counter_roles pins the two lists together).
	const TRADE_IN_ROLES = ['Sales User', 'Stock Manager', 'System Manager'];

	function maybeInjectButton(root) {
		// Probed against ERPNext v16: #page-point-of-sale .page-actions sits
		// next to "Recent Orders" / "New Invoice". Fallbacks downgrade to the
		// app root so future re-skins still get a button somewhere visible.
		const candidates = [
			'#page-point-of-sale .page-actions',
			'.point-of-sale-app .menu-btn-group',
			'.point-of-sale-app .header',
			'.point-of-sale-app',
		];
		let host = null;
		for (const sel of candidates) {
			host = (root || document).querySelector(sel);
			if (host) break;
		}
		if (!host) return;

		// The one button factory for this toolbar (core and extension buttons
		// alike, so a restyle moves them together). placement 'first' (default,
		// core) or 'last' (extensions, after the core buttons); returns the button.
		const ensureBtn = (id, label, onClick, { placement = 'first', dataset = {} } = {}) => {
			const existing = document.getElementById(id);
			if (existing) return existing;
			const btn = document.createElement('button');
			btn.id = id;
			btn.type = 'button';
			btn.className = 'btn btn-default btn-sm';
			btn.style.marginRight = '6px';
			btn.textContent = label;
			Object.assign(btn.dataset, dataset);
			btn.addEventListener('click', onClick);
			if (placement === 'last') host.appendChild(btn);
			else host.insertBefore(btn, host.firstChild);
			return btn;
		};
		// Insert order: each goes to firstChild, so the LAST call lands
		// leftmost. Customer Display first => it sits rightmost, after the
		// firearm-workflow buttons: Trade-in | Work Order | Customer Display.
		ensureBtn('ffl-customer-display-btn', __('Customer Display'), function () {
			if (window.fflOpenCustomerDisplay) window.fflOpenCustomerDisplay();
		});
		if (frappe.user.has_role(WORK_ORDER_ROLES)) ensureBtn(WORK_ORDER_BTN_ID, __('Work Order'), openWorkOrderDialog);
		if (frappe.user.has_role(TRADE_IN_ROLES)) ensureBtn(BUTTON_ID, __('Trade-in'), openTradeInDialog);
		renderExtensionPosActions(ensureBtn);
	}

	// Extension POS actions (gunstore.extensions). One
	// <button> per action of a switched-on extension, appended AFTER the core
	// buttons; visibility re-evaluated on every DOM pass through the action's
	// own condition(ctx). ctx is a read-only snapshot — an extension changes
	// the cart through ctx.actions or its own endpoint, never the DOM.
	function extensionContext() {
		const pos = window.cur_pos;
		const doc = (pos && pos.frm && pos.frm.doc) || null;
		return Object.freeze({
			invoice: doc,
			customer: (doc && doc.customer) || '',
			pos_profile: (doc && doc.pos_profile) || '',
			actions: Object.freeze({
				loadInvoice: loadInvoiceIntoCart,
				refreshCart: () => {
					if (pos && pos.cart && typeof pos.cart.load_invoice === 'function') pos.cart.load_invoice();
				},
			}),
		});
	}
	// The action list only changes with boot (switch flips reload the desk), so
	// compute it once per registry+boot; only the extensions' condition(ctx)
	// runs on each DOM pass, and never more than every 100 ms.
	let extActionsCache = null;
	let extLastPass = 0;
	let extTrailing = null;
	function extensionPosActions(reg) {
		if (!extActionsCache) extActionsCache = reg.actions('posActions', frappe.boot);
		return extActionsCache;
	}
	function renderExtensionPosActions(ensureBtn) {
		const reg = window.gunstore && window.gunstore.extensions;
		if (!reg || !window.frappe) return;
		const now = Date.now();
		if (now - extLastPass < 100) {
			// Throttled: schedule one trailing pass so the last DOM change of a
			// burst is still reflected, instead of waiting for the next one.
			if (!extTrailing) extTrailing = setTimeout(() => { extTrailing = null; renderExtensionPosActions(ensureBtn); }, 120);
			return;
		}
		extLastPass = now;
		const ctx = extensionContext();
		for (const action of extensionPosActions(reg)) {
			const btn = ensureBtn(`ext-${action.id}`, action.label || action.id, () => {
				const fresh = extensionContext();
				if (!reg.applies(action, fresh)) return;
				try {
					Promise.resolve(action.onClick(fresh)).catch((e) => reg.reportFailure(action, e));
				} catch (e) {
					reg.reportFailure(action, e);
				}
			}, { placement: 'last', dataset: { extension: action.extension } });
			btn.hidden = !reg.applies(action, ctx);
		}
	}

	// Load a server-built draft POS Invoice into the live cart — the same
	// sequence the Recent Orders → Edit flow runs (pos_controller.edit_order).
	async function loadInvoiceIntoCart(name) {
		const pos = window.cur_pos;
		if (!pos || !pos.frm) return false;
		try {
			if (typeof pos.sync_draft_invoice_to_frm === 'function') {
				await pos.sync_draft_invoice_to_frm('POS Invoice', name);
			}
			await pos.frm.refresh(name);
			// UPGRADE-SAFE tender guard: only reset
			// payments to the POS Profile default on a genuinely unpaid draft. A held,
			// already-charged firearm order must keep its recorded card/Zelle split;
			// resetting it silently drops the tender. hasLiveTender is the pure,
			// unit-tested predicate in pos_tender_guard.js (window.ffl).
			const _preserveTender = window.ffl && ffl.hasLiveTender && ffl.hasLiveTender(pos.frm.doc);
			if (!_preserveTender) {
				try { await pos.frm.call('reset_mode_of_payments'); } catch (e) { /* cosmetic */ }
			}
			if (pos.cart && typeof pos.cart.load_invoice === 'function') pos.cart.load_invoice();
			return true;
		} catch (e) {
			console.error('ffl: failed to load invoice into cart', e);
			return false;
		}
	}

	// Put the trade-in allowance ON the ticket: a negative TRADE-IN-CREDIT
	// line in the live cart, via POS's own add-item path (on_cart_update →
	// get_item_details fills item_name/uom/income_account properly). The row is
	// tamper-proof: the submit gate (compliance/trade_in_credit.py) re-derives
	// the amount from the linked acquisition and blocks reuse. A cart whose
	// credits exceed the goods now saves as a NEGATIVE draft — the store pays
	// the customer the difference (compliance/negative_ticket lifts ERPNext's
	// is_negative_grand_total_allowed refusal for exactly that case; the
	// payment screen shows the payout instead of tender tiles). Before that
	// refusal was lifted a credit-only draft could not be parked server-side —
	// ERPNext's check, not a docfield constraint (grand_total is not non_negative).
	// Keep a LOCKED line (a trade-in credit) at exactly
	// its ffl_locked_rate on screen, whatever re-priced it (pos_tender_guard.js
	// lockedRowFix; a past incident showed +180). The server pin
	// (compliance/locked_lines.py) is the authority on every save; this is the
	// live cart. Form events on the item row — every path that moves the rate
	// or its pricing fields fires one, drafts resumed from Recent Orders
	// included — and the lock travels on the row, so no read on FFL
	// Acquisition / Sales Order (a counter-only cashier has neither) and no
	// cache that can go stale when a trade-in's cost is edited.
	// Pricing residue is assigned silently (no triggers): set through
	// set_value, price_list_rate 0 would make ERPNext recompute rate from it;
	// only rate goes through set_value so ERPNext's own rate handler re-totals.
	// Registered via frappe.ui.form.on, so it runs BEFORE ERPNext's cscript
	// handlers for the same field (script_manager: new-style first) — by the
	// time ERPNext's rate handler looks at the row, price_list_rate is 0 and it
	// derives no margin.
	function repinLockedRow(frm, cdt, cdn) {
		const row = locals[cdt] && locals[cdt][cdn];
		const fix = row && window.ffl && ffl.lockedRowFix && ffl.lockedRowFix(row);
		if (!fix) return;
		Object.assign(row, fix.silent);
		if (fix.rate !== undefined) return frappe.model.set_value(cdt, cdn, 'rate', fix.rate);
		const pos = window.cur_pos;
		if (pos && pos.frm === frm && typeof pos.update_cart_html === 'function') pos.update_cart_html(row);
		else frm.refresh_field('items');
	}
	if (window.frappe && frappe.ui && frappe.ui.form && frappe.ui.form.on) {
		['POS Invoice Item', 'Sales Invoice Item'].forEach((cdt) => frappe.ui.form.on(cdt, {
			rate: repinLockedRow,
			price_list_rate: repinLockedRow,
			discount_percentage: repinLockedRow,
			discount_amount: repinLockedRow,
			margin_type: repinLockedRow,
			margin_rate_or_amount: repinLockedRow,
		}));
	}

	// A locked line is added by its own flow only. Through POS's cart path a tap on
	// its tile (a $0 price: the tombstone hides any list price) would match the
	// locked row — price_list_rate 0 — and add a unit to it (a credit booked
	// twice), and a qty or rate typed on the row would change it. Refused, except
	// for injectLockedLine's own call. Removing the line is a different path
	// (remove_item_from_cart), untouched.
	let injectingLocked = false;
	function guardLockedCartLines() {
		const C = window.erpnext && erpnext.PointOfSale && erpnext.PointOfSale.Controller;
		if (!C || C.prototype.__fflLockedCartGuard) return;
		const orig = C.prototype.on_cart_update;
		C.prototype.on_cart_update = function (args) {
			const item = args && args.item;
			if (!injectingLocked && window.ffl && ffl.isLockedLine && ffl.isLockedLine(item)) {
				frappe.show_alert({ indicator: 'orange', message: __('Trade-in credits come from the Trade-in button and gunsmith deposits from their work order, and their amount is fixed. Remove the line to undo it.') });
				return Promise.resolve({});
			}
			return orig.apply(this, arguments);
		};
		C.prototype.__fflLockedCartGuard = true;
	}
	(function tryGuard(retriesLeft) {
		if (window.erpnext && erpnext.PointOfSale && erpnext.PointOfSale.Controller) return guardLockedCartLines();
		if (retriesLeft > 0) setTimeout(() => tryGuard(retriesLeft - 1), 250);
	})(40);
	if (frappe.router && frappe.router.on) frappe.router.on('change', guardLockedCartLines);

	// One locked line into the live cart: `stamp` is the row fields that tie it to its
	// source (a trade-in's acquisition, a work order), and how a re-run finds it; `extra`
	// is other row fields (a description).
	async function injectLockedLine(itemCode, rate, stamp, extra) {
		const pos = window.cur_pos;
		if (!pos || typeof pos.on_cart_update !== 'function' || !pos.frm) return false;
		guardLockedCartLines();
		try {
			// Dialog re-run against the same cart → don't stack a second row.
			const existing = (pos.frm.doc.items || []).find(
				(r) => Object.keys(stamp).every((k) => r[k] === stamp[k]));
			if (existing) return true;
			// rate is never 0, so POS's "Price is not set" guard passes. stock_uom too:
			// with negative stock off, ERPNext's check_stock_availability reads
			// item_row.stock_uom.bold() and, without it, throws before get_item_details
			// ever runs — a bare row with no tax template or income account.
			let row;
			injectingLocked = true;
			try {
				row = await pos.on_cart_update({
					field: 'qty', value: '+1',
					item: { item_code: itemCode, uom: 'Nos', stock_uom: 'Nos', rate },
				});
			} finally {
				injectingLocked = false;
			}
			if (!row || $.isEmptyObject(row)) return false;
			// trigger_new_item_events priced the row like any item (0, or the
			// item's Item Price if one slipped in); stamp the source + the lock,
			// then pin. The rate trigger runs repinLockedRow, which also clears
			// the pricing residue; the explicit call covers a rate that already
			// happened to equal the locked amount (no trigger then).
			await frappe.model.set_value(row.doctype, row.name, Object.assign({}, extra || {}, stamp, {
				ffl_locked_rate: rate,
				rate,
			}));
			await repinLockedRow(pos.frm, row.doctype, row.name);
			pos.update_cart_html(row);
			return true;
		} catch (e) {
			console.error('ffl: locked line injection failed', e);
			return false;
		}
	}

	// A gun's credit is locked to its acquisition, an accessories / ammo credit to its draft Material Receipt.
	function injectTradeInCredit(credit) {
		const stamp = credit.stock_entry
			? { ffl_trade_in_stock_entry: credit.stock_entry } : { ffl_trade_in_acquisition: credit.acquisition };
		return injectLockedLine(credit.item_code, credit.rate, stamp);
	}

	// Where accessories / ammo taken in trade may be filed (mirror of trade_in.TRADE_IN_ACCESSORY_PARENTS).
	const TRADE_IN_ACCESSORY_PARENTS = ['Ammunition', 'Accessory', 'Parts'];

	// "New item" under the accessories table: the counter makes an accessory / ammo Item the store has never
	// stocked (trade_in.create_accessory_item) and it lands as a new row.
	function newTradeInAccessoryItem(onMade) {
		const d = new frappe.ui.Dialog({
			title: __('New accessory / ammo item'),
			size: 'small',
			fields: [
				{ label: __('Item name'), fieldname: 'item_name', fieldtype: 'Data', reqd: 1 },
				{
					label: __('Category'), fieldname: 'item_group', fieldtype: 'Link', options: 'Item Group', reqd: 1,
					get_query: () => ({ filters: { is_group: 0, parent_item_group: ['in', TRADE_IN_ACCESSORY_PARENTS] } }),
				},
				{ label: __('Selling price ($)'), fieldname: 'sell_price', fieldtype: 'Currency',
					description: __('Optional — set later when pricing it.') },
			],
			primary_action_label: __('Create'),
			primary_action(v) {
				frappe.call({
					method: 'ffl_core.api.trade_in.create_accessory_item',
					args: v,
					callback(r) {
						if (!r.message) return;
						d.hide();
						onMade(r.message);
					},
				});
			},
		});
		d.show();
	}

	async function openTradeInDialog() {
		// POS sets cur_pos.frm.doc.customer when a customer is selected. We
		// pre-fill source_name (the seller — i.e. the customer trading in)
		// from there so the operator doesn't retype.
		const posCustomer = (window.cur_pos && window.cur_pos.frm && window.cur_pos.frm.doc.customer) || '';

		// Model SKU is picked/created via the shared FirearmItemPicker (built
		// after dialog.show() below — same search as Receive Goods: local + RSR
		// + promote/create). onSelect stores the chosen item_code here; the
		// primary action injects it into the payload.
		let selectedItemCode = '';
		// Creating an Item (receive_goods.create_item) and promoting an RSR row
		// (rsr.promote.promote_to_item) are stock jobs: the picker offers them
		// only to roles.STOCK_ROLES (Stock Manager / System Manager, mirrored
		// here). The counter picks an existing item or leaves it blank (the
		// trade-in then makes one for this gun).
		const stockRole = frappe.user.has_role(['Stock Manager', 'System Manager']);
		// The firearm's fields show, and are required, only while the trade-in includes one.
		const GUN = 'eval:doc.has_firearm';

		// Seller prefill: the suffix-aware name split + primary address live
		// server-side (trade_in.seller_defaults, shared with Receive Goods).
		// Only fills fields the operator hasn't typed into — except the state
		// dropdown, whose TX default would otherwise mask an out-of-state
		// customer address.
		const prefillSeller = () => {
			const cust = dialog.get_value('source_name');
			if (!cust) return;
			frappe.call({
				method: 'ffl_core.api.trade_in.seller_defaults',
				args: { customer: cust },
				callback: (r) => {
					const d = r.message || {};
					['source_first_name', 'source_middle_name', 'source_last_name',
						'source_suffix', 'source_address_line1', 'source_city',
						'source_zip'].forEach((f) => {
						if (d[f] && !dialog.get_value(f)) dialog.set_value(f, d[f]);
					});
					if (d.source_state) dialog.set_value('source_state', d.source_state);
				},
			});
		};

		const dialog = new frappe.ui.Dialog({
			title: __('Trade-in (Individual)'),
			fields: [
				{ fieldtype: 'Section Break', label: __('Seller (Customer)') },
				{
					label: __('Customer'), fieldname: 'source_name', fieldtype: 'Link',
					options: 'Customer', reqd: 1, default: posCustomer,
					description: __('Person trading in the firearm. Pre-filled from POS customer.'),
					change: () => {
						// Banned box / alerts first (customer_flags.js): a Banned seller is
						// cleared and nothing of theirs is filled in.
						const cf = window.ffl && ffl.customerFlags;
						const ok = cf && cf.guardControl ? cf.guardControl(dialog.fields_dict.source_name) : Promise.resolve(true);
						ok.then((go) => go && prefillSeller());
					},
				},
				{
					label: __('First Name'), fieldname: 'source_first_name', fieldtype: 'Data', reqd: 1,
				},
				{
					label: __('Middle Name'), fieldname: 'source_middle_name', fieldtype: 'Data',
				},
				{ fieldtype: 'Column Break' },
				{
					label: __('Last Name'), fieldname: 'source_last_name', fieldtype: 'Data', reqd: 1,
				},
				{
					label: __('Suffix'), fieldname: 'source_suffix', fieldtype: 'Select',
					options: ['', 'Jr', 'Sr', 'II', 'III', 'IV', 'V', 'VI'].join('\n'),
				},
				{ fieldtype: 'Section Break', label: __('Address (as on ID)') },
				{
					label: __('Street Address'), fieldname: 'source_address_line1',
					fieldtype: 'Data', reqd: 1,
				},
				{
					label: __('City'), fieldname: 'source_city', fieldtype: 'Data', reqd: 1,
				},
				{ fieldtype: 'Column Break' },
				{
					label: __('State'), fieldname: 'source_state', fieldtype: 'Select', reqd: 1,
					options: ['', 'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'DC', 'FL',
						'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME',
						'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH',
						'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI',
						'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI',
						'WY'].join('\n'),
				},
				{
					label: __('ZIP'), fieldname: 'source_zip', fieldtype: 'Data', reqd: 1,
				},
				{ fieldtype: 'Section Break', label: __('Identification') },
				{
					label: __('ID Type'), fieldname: 'id_type', fieldtype: 'Select', reqd: 1,
					options: ['Driver License', 'State ID', 'Passport', 'Military ID', 'Other'].join('\n'),
					default: 'Driver License',
				},
				{ fieldtype: 'Column Break' },
				{
					label: __('ID Number'), fieldname: 'id_number', fieldtype: 'Data', reqd: 1,
					description: __('Number on the ID. ATF requires this for individual acquisitions.'),
				},
				{ fieldtype: 'Section Break', label: __('Firearm') },
				{
					label: __('Includes a firearm'), fieldname: 'has_firearm', fieldtype: 'Check', default: 1,
					description: __('Untick for accessories / ammo only.'),
				},
				{
					fieldtype: 'HTML', fieldname: 'item_code_picker', depends_on: GUN,
					options: `<label class="control-label" style="padding-right:0">${__('Item')}</label>
						<div class="ffl-ti-sku" style="position:relative">
							<input type="text" class="form-control input-sm ffl-ti-sku-input" placeholder="${stockRole ? __('Search item / RSR, or create a new one (e.g. Glock19 USED)') : __('Search an existing item, or leave blank')}">
							<div class="ffl-ti-sku-dd"></div>
						</div>`,
				},
				{
					label: __('Serial Number'), fieldname: 'serial_no', fieldtype: 'Data', depends_on: GUN, mandatory_depends_on: GUN,
				},
				{
					label: __('Manufacturer'), fieldname: 'manufacturer', fieldtype: 'Data', depends_on: GUN, mandatory_depends_on: GUN,
				},
				{
					label: __('Model'), fieldname: 'model_name', fieldtype: 'Data', depends_on: GUN, mandatory_depends_on: GUN,
				},
				{
					label: __('Importer'), fieldname: 'importer', fieldtype: 'Data', depends_on: GUN,
					description: __('Imported guns: the importer marking on the firearm (bound-book field).'),
				},
				{ fieldtype: 'Column Break' },
				{
					label: __('Caliber'), fieldname: 'caliber', fieldtype: 'Link', options: 'Caliber',
					depends_on: GUN, mandatory_depends_on: GUN,
				},
				{
					label: __('Firearm Type'), fieldname: 'firearm_type', fieldtype: 'Select', depends_on: GUN, mandatory_depends_on: GUN,
					options: ffl.FIREARM_TYPE_OPTIONS,
					description: __('Bound-book firearm type (ATF wording).'),
				},
				{
					label: __('Condition Grade'), fieldname: 'condition_grade', fieldtype: 'Select',
					depends_on: GUN, mandatory_depends_on: GUN,
					options: ['New', 'Excellent', 'Very Good', 'Good', 'Fair'].join('\n'),
					default: 'Very Good',
				},
				{
					label: __('Storage location'), fieldname: 'storage_location', fieldtype: 'Link',
					options: 'Storage Location', depends_on: GUN,
					description: __('Where the gun goes in the store. A slot holds one firearm. '
						+ 'Leave blank to place it later from Storage Locations.'),
					get_query: () => ({
						query: 'ffl_core.api.storage.location_query', filters: { only_free: 1 },
					}),
				},
				{ fieldtype: 'Section Break', label: __('Pricing'), depends_on: GUN },
				{
					label: __('Acquisition Cost ($)'), fieldname: 'acquisition_cost',
					fieldtype: 'Currency', depends_on: GUN, mandatory_depends_on: GUN,
					description: __('Trade-in credit, or what the store pays the seller, for the firearm.'),
				},
				{ fieldtype: 'Column Break' },
				{
					label: __('Retail Price ($)'), fieldname: 'retail_price',
					fieldtype: 'Currency', depends_on: GUN,
					description: __('Optional — set later when pricing the unit.'),
				},
				{ fieldtype: 'Section Break', label: __('Accessories & ammo') },
				{
					fieldname: 'accessories', fieldtype: 'Table', label: __('Accessories & ammo'),
					in_place_edit: true, data: [],
					description: __('Anything else the seller brings, counted by quantity. No serial numbers.'),
					fields: [
						{
							label: __('Item'), fieldname: 'item_code', fieldtype: 'Link', options: 'Item',
							in_list_view: 1, columns: 6,
							get_query: () => ({ filters: { disabled: 0, is_stock_item: 1, has_serial_no: 0, is_firearm: 0 } }),
						},
						{ label: __('Qty'), fieldname: 'qty', fieldtype: 'Float', default: 1, in_list_view: 1, columns: 2 },
						{ label: __('Each ($)'), fieldname: 'rate', fieldtype: 'Currency', in_list_view: 1, columns: 2 },
					],
				},
				{
					fieldtype: 'Button', fieldname: 'new_accessory_item', label: __('New item'),
					click: () => newTradeInAccessoryItem((item) => {
						const grid = dialog.fields_dict.accessories;
						const rows = (grid.df.data || []).filter((r) => r.item_code);
						rows.push({ item_code: item.item_code, qty: 1 });
						// The dialog grid finds (and deletes) rows by idx.
						rows.forEach((r, i) => { r.idx = i + 1; });
						grid.df.data = rows;
						grid.grid.refresh();
					}),
				},
				{ fieldtype: 'Section Break' },
				{
					label: __('Apply as credit to this sale'), fieldname: 'apply_credit',
					fieldtype: 'Check', default: 1,
					description: __('Adds locked negative Trade-in Credit lines (the firearm\'s cost, and the '
						+ 'accessories\' total) to the current cart, so the allowance reduces the total and '
						+ 'the tax base. Uncheck when the store pays the seller now (no sale on this ticket).'),
				},
				{
					label: __('Seller is paid by'), fieldname: 'payout_method', fieldtype: 'Select',
					options: ['Cash', 'Zelle', 'Check', 'ACH'].join('\n'), default: 'Cash',
					depends_on: 'eval:!doc.apply_credit', mandatory_depends_on: 'eval:!doc.apply_credit',
					description: __('Cash comes out of the drawer; the others from the bank.'),
				},
				{
					label: __('Payment reference'), fieldname: 'payout_reference', fieldtype: 'Data',
					depends_on: "eval:!doc.apply_credit && doc.payout_method !== 'Cash'",
					mandatory_depends_on: "eval:!doc.apply_credit && doc.payout_method !== 'Cash'",
					description: __('Check number, Zelle confirmation or ACH reference.'),
				},
			],
			primary_action_label: __('Submit Trade-in'),
			primary_action(values) {
				// The credit line lands in the live cart, and the cart refuses
				// items without a customer — fail BEFORE the intake so we never
				// strand a recorded gun with an unapplied credit.
				if (values.apply_credit && !(window.cur_pos
						&& window.cur_pos.frm && window.cur_pos.frm.doc.customer)) {
					frappe.show_alert({
						message: __('Select the POS customer (the buyer) before applying a trade-in credit.'),
						indicator: 'orange',
					});
					return;
				}
				const accessories = (values.accessories || []).filter((r) => r.item_code)
					.map((r) => ({ item_code: r.item_code, qty: r.qty, rate: r.rate }));
				frappe.call({
					method: 'ffl_core.api.trade_in.create_trade_in_intake',
					args: { payload: JSON.stringify({
						...values, accessories, has_firearm: values.has_firearm ? 1 : 0,
						item_code: values.has_firearm ? selectedItemCode : '',
					}) },
					freeze: true,
					freeze_message: __('Recording trade-in…'),
					async callback(r) {
						if (!(r.message && r.message.ok)) return;
						dialog.hide();
						const credits = r.message.credits || [];
						if (!credits.length) {
							frappe.show_alert({
								message: __('Trade-in recorded · Receipt {0}. '
									+ 'The payment to the seller is booked.',
									[r.message.purchase_receipt || '']),
								indicator: 'green',
							}, 8);
							return;
						}
						// In order, stopping at the first failure: the gun's line goes first, so the re-run the modal
						// below asks for (it reuses the gun's pending record) finds no accessories line from this run
						// in the cart. Accessories re-entered on any other re-run get a line of their own.
						let injected = true;
						for (const credit of credits) {
							if (!(injected = await injectTradeInCredit(credit))) break;
						}
						if (injected) {
							frappe.show_alert({
								message: __('Trade-in recorded · {0} credit added to this sale.',
									[format_currency(credits.reduce((sum, c) => sum + c.rate, 0))]),
								indicator: 'green',
							}, 8);
						} else {
							// Modal, not a toast: without the credit row the
							// checkout charges full price AND the parked
							// trade-in never books (a past invoice lost
							// its credit exactly this way).
							frappe.msgprint({
								title: __('Credit Line Not Added'),
								indicator: 'red',
								message: __('The trade-in was recorded, but the credit line could '
									+ 'NOT be added to this cart. Do not complete checkout yet: '
									+ 're-open the item cart and run Trade-in again (a gun\'s serial '
									+ 'reuses its pending record), so the sale carries '
									+ 'the credit and the trade-in books at checkout.'),
							});
						}
					},
				});
			},
		});
		dialog.show();
		if (posCustomer) prefillSeller();  // pre-filled Customer fires no change event
		// Wire the Model SKU picker (shared with Receive Goods): local + RSR
		// search + promote/create. onSelect stores the chosen/created item_code.
		const $skuWrap = dialog.fields_dict.item_code_picker.$wrapper;
		const skuPicker = new ffl.FirearmItemPicker({
			$input: $skuWrap.find('.ffl-ti-sku-input'),
			$dropdown: $skuWrap.find('.ffl-ti-sku-dd'),
			includeCreate: stockRole,
			includeRsr: stockRole,
			onSelect: (item) => {
				selectedItemCode = (item && item.item_code) || '';
				$skuWrap.find('.ffl-ti-sku-input').val(selectedItemCode);
				// A model filed before: its maker / model / caliber / type fill the gun's fields (still editable).
				ffl.tradeInModelFill(item).forEach(([f, v]) => dialog.set_value(f, v));
			},
		});
		// The picker binds a document-level click handler (+ a debounce timer);
		// tear it down when the dialog closes so it doesn't accumulate across a
		// long POS shift that never reloads the page.
		dialog.onhide = () => skuPicker.destroy();
	}

	// --- Gunsmith work orders at the register (rules: ffl_core/gunsmith.py) ---
	// The Work Order button takes a gun in (gunsmith.create_work_order); what the customer
	// pays now goes into the cart as the job's locked GUNSMITH-DEPOSIT line. Recent Orders
	// › Work Orders (pos_work_orders.js) lists the open jobs and calls Take payment (another
	// such line, any amount, any time) and Complete (the job's pick-up invoice into the
	// cart) below. Each is a sale like any other, so its checkout takes the card. The Desk
	// work-order form sends the clerk here with route options for Complete.
	const WORK_ORDER_BTN_ID = 'ffl-work-order-btn';
	// Who sees it: roles.COUNTER_ROLES, mirrored (the server gate on create_work_order;
	// test_gunsmith_work_order pins the two lists together).
	const WORK_ORDER_ROLES = ['Sales User', 'Sales Manager', 'Stock Manager', 'System Manager'];
	const GW = 'ffl_core.gunsmith.';

	function workOrdersApi() { return (window.ffl && ffl.posWorkOrders) || null; }

	function hideRecentOrders(pos) {
		const list = pos.recent_order_list;
		if (list && list.$component && list.$component.is(':visible') && typeof pos.toggle_recent_order === 'function') {
			pos.toggle_recent_order();
		}
	}

	// The cart's customer, the way the cart's own customer field sets it (pos_item_cart
	// make_customer_selector's onchange), so the customer section and totals follow.
	async function setCartCustomer(pos, customer) {
		const frm = pos.frm;
		if (!customer || frm.doc.customer === customer) return;
		await frappe.model.set_value(frm.doc.doctype, frm.doc.name, 'customer', customer);
		await frm.script_manager.trigger('customer', frm.doc.doctype, frm.doc.name);
		const cart = pos.cart;
		if (!cart || typeof cart.fetch_customer_details !== 'function') return;
		try {
			await cart.fetch_customer_details(customer);
			if (cart.events && typeof cart.events.customer_details_updated === 'function') {
				cart.events.customer_details_updated(cart.customer_info);
			}
			cart.update_customer_section();
			cart.update_totals_section();
		} catch (e) { /* cosmetic: the invoice has the customer */ }
	}

	function askToPark() {
		return new Promise((resolve) => frappe.confirm(
			__('Save the sale in the cart as a draft first? It stays in Recent Orders.'),
			() => resolve(true), () => resolve(false)));
	}

	// The job's locked payment line into a cart for its customer: this one, or — when it
	// holds another customer's sale — a new one, that sale parked as a draft first.
	async function addPaymentToCart(customer, line) {
		const pos = window.cur_pos;
		if (!pos || !pos.frm) return false;
		const api = workOrdersApi();
		hideRecentOrders(pos);
		if (api && api.cartConflict(pos.frm.doc, customer)) {
			if (pos.frm.is_new() || pos.frm.is_dirty()) {
				if (!(await askToPark())) return false;
				try { await pos.frm.save(); } catch (e) { return false; }   // the server said why
			}
			await pos.make_new_invoice();
		}
		try { await setCartCustomer(pos, customer); } catch (e) { /* checked below */ }
		if (pos.frm.doc.customer !== customer) return false;
		if ((pos.frm.doc.items || []).some((r) => r.item_code === line.item_code)) {
			// One gunsmith payment line per sale (gunsmith.guard_deposit_lines).
			frappe.msgprint({ title: __('One payment per sale'), indicator: 'orange',
				message: __('This sale already has a gunsmith payment line. Check it out first, then take the next payment.') });
			return null;
		}
		return injectLockedLine(line.item_code, line.rate, { ffl_work_order: line.ffl_work_order },
			{ description: line.description });
	}

	function paymentAdded(ok, name, rate) {
		if (ok === null) return;
		if (ok) {
			frappe.show_alert({ indicator: 'green', message: __('{0} towards {1} is in the cart: check out to take it.',
				[format_currency(rate), name]) }, 8);
		} else {
			frappe.msgprint({ title: __('Payment line not added'), indicator: 'red',
				message: __('The payment towards {0} could not be added to the cart. Try Take payment from Recent Orders › Work Orders.', [name]) });
		}
	}

	// Take payment: any amount towards an open job, as often as the customer likes.
	function takePaymentAtRegister(row) {
		frappe.prompt({ fieldtype: 'Currency', fieldname: 'amount', label: __('Amount'), reqd: 1,
			description: __('Held for the customer and taken off the bill at pick-up.') }, async (v) => {
			const r = await frappe.call({ method: GW + 'deposit_line', args: { name: row.name, amount: v.amount }, freeze: true });
			const m = (r && r.message) || {};
			if (!m.deposit_line) return;
			paymentAdded(await addPaymentToCart(m.customer, m.deposit_line), row.name, m.deposit_line.rate);
		}, __('Take payment for {0}', [row.name]), __('Add to cart'));
	}

	// A server-built draft (the job's pick-up invoice) into the cart. A sale
	// still unsaved in the cart is saved as a draft first (it stays in Recent Orders),
	// never dropped.
	async function openInRegister(name) {
		const pos = window.cur_pos;
		if (!pos || !pos.frm) return false;
		const frm = pos.frm;
		if (frm.doc.name !== name && (frm.doc.items || []).length && (frm.is_new() || frm.is_dirty())) {
			if (!(await askToPark())) return false;
			try { await frm.save(); } catch (e) { return false; }   // the server said why
		}
		hideRecentOrders(pos);
		const ok = await loadInvoiceIntoCart(name);
		if (ok && typeof pos.toggle_components === 'function') pos.toggle_components(true);
		if (!ok) frappe.msgprint({ title: __('Not loaded'), indicator: 'red',
			message: __('Invoice {0} could not be loaded into the cart. Open it from Recent Orders › Draft.', [name]) });
		return ok;
	}

	// Complete: the clerk confirms Work Performed and the charges, the job keeps them
	// and is Ready, and its pick-up invoice (the charges less the deposit held) goes into
	// the cart; its checkout picks the job up (gunsmith.on_invoice_submit). An open
	// pick-up draft is opened as it is.
	function completeAtRegister(row, laborItem) {
		if (row.drafts && row.drafts.pickup) return openInRegister(row.drafts.pickup);
		const api = workOrdersApi();
		const cur = (v) => format_currency(v, frappe.defaults.get_default('currency'));
		const esc = (v) => frappe.utils.escape_html(v == null ? '' : String(v));
		const gun = [row.manufacturer, row.model, row.serial_number && __('SN {0}', [row.serial_number])].filter(Boolean).join(' · ');
		const notes = [esc(row.customer_name || row.customer) + ' · ' + esc(gun)];
		if (flt(row.deposit_held) > 0) notes.push(esc(__('Deposit held {0} comes off the bill.', [cur(row.deposit_held)])));
		if (row.logged) notes.push(esc(__('At checkout the gun is returned to its owner in the bound book.')));
		const dialog = new frappe.ui.Dialog({
			title: __('Complete {0}', [row.name]),
			size: 'large',
			fields: [
				{ fieldtype: 'HTML', fieldname: 'about', options: '<p>' + notes.join('<br>') + '</p>' },
				{ fieldtype: 'Small Text', fieldname: 'work_performed', label: __('Work Performed'), reqd: 1,
					default: row.work_performed || '' },
				{ fieldtype: 'Table', fieldname: 'lines', label: __('Charges'), cannot_add_rows: false, in_place_edit: true,
					data: api ? api.defaultCharges(row, laborItem) : [],
					fields: [
						{ fieldtype: 'Link', fieldname: 'item_code', options: 'Item', label: __('Item'), reqd: 1, in_list_view: 1, columns: 3 },
						{ fieldtype: 'Data', fieldname: 'description', label: __('Description'), in_list_view: 1, columns: 4 },
						{ fieldtype: 'Float', fieldname: 'qty', label: __('Qty'), default: 1, in_list_view: 1, columns: 1 },
						{ fieldtype: 'Currency', fieldname: 'rate', label: __('Rate'), in_list_view: 1, columns: 2 },
					] },
			],
			primary_action_label: __('Confirm and load the cart'),
			primary_action(values) {
				const lines = (values.lines || []).filter((l) => l.item_code).map((l) => ({
					item_code: l.item_code, description: l.description || '', qty: flt(l.qty) || 1, rate: flt(l.rate) }));
				frappe.call({
					method: GW + 'complete_work', freeze: true,
					args: { name: row.name, work_performed: values.work_performed, lines: JSON.stringify(lines) },
				}).then(async (r) => {
					const m = (r && r.message) || {};
					dialog.hide();
					if (m.pos_invoice) return openInRegister(m.pos_invoice);
					if (m.picked_up) {
						frappe.show_alert({ indicator: 'green', message: flt(m.refund_due) > 0
							? __('{0} picked up. {1} of deposit is still held: refund it from Recent Orders › Work Orders.', [row.name, cur(m.refund_due)])
							: __('{0} picked up.', [row.name]) }, 10);
						refreshWorkOrders();
					}
				});
			},
		});
		dialog.show();
		return dialog;
	}

	function refreshWorkOrders() {
		const pos = window.cur_pos;
		if (pos && pos.recent_order_list && pos.recent_order_list.$component.is(':visible')) pos.recent_order_list.refresh_list();
	}

	// The customer signs the job's intake authorization (tablet, QR, email or a paper copy).
	function authorizeAtRegister(row, template) {
		if (!template || !(window.ffl && ffl.agreements)) return false;
		return ffl.agreements.request(row.customer, template, { customer_name: row.customer_name,
			reference_doctype: 'Gunsmith Work Order', reference_name: row.name, done: refreshWorkOrders });
	}

	// Refund a closed job's leftover payment: its return goes into the cart and is checked
	// out here, where a card payment is refunded to the card (never from the Desk).
	async function refundAtRegister(row) {
		if (row.drafts && row.drafts.refund) return openInRegister(row.drafts.refund);
		const r = await frappe.call({ method: GW + 'refund_deposit', args: { name: row.name }, freeze: true });
		const m = (r && r.message) || {};
		return m.pos_invoice ? openInRegister(m.pos_invoice) : false;
	}

	window.ffl = window.ffl || {};
	window.ffl.gunsmithRegister = { complete: completeAtRegister, takePayment: takePaymentAtRegister,
		refund: refundAtRegister, authorize: authorizeAtRegister };

	// The Desk form's Pick up: route options {ffl_work_order, ffl_work_order_action:
	// 'complete'} (the router also keeps them in the URL — dropped once read, so a reload
	// does not run them again).
	function takeRouteJob() {
		const ro = frappe.route_options;
		if (!ro || !ro.ffl_work_order) return;
		const job = { name: ro.ffl_work_order, action: ro.ffl_work_order_action };
		frappe.route_options = null;
		if (location.search.includes('ffl_work_order')) history.replaceState(history.state, '', location.pathname + location.hash);
		// The register may still be opening (POS Opening Entry dialog, first load).
		(function whenReady(left) {
			const pos = window.cur_pos;
			if (pos && pos.frm && pos.cart && pos.recent_order_list) return runRouteJob(job);
			if (left > 0) setTimeout(() => whenReady(left - 1), 500);
		})(240);
	}
	async function runRouteJob(job) {
		if (job.action !== 'complete' && job.action !== 'refund') return;
		const r = await frappe.call({ method: GW + 'register_work_orders', args: { name: job.name } });
		const out = (r && r.message) || {};
		const row = (out.orders || [])[0];
		if (!row) return frappe.show_alert({ indicator: 'orange', message: __('{0} has nothing to do at the register.', [job.name]) });
		return job.action === 'refund' ? refundAtRegister(row) : completeAtRegister(row, out.labor_item);
	}

	function openWorkOrderDialog() {
		const pos = window.cur_pos;
		const posCustomer = (pos && pos.frm && pos.frm.doc.customer) || '';
		const dialog = new frappe.ui.Dialog({
			title: __('Gunsmith Work Order'),
			size: 'large',
			fields: [
				{ fieldtype: 'Section Break', label: __('Customer') },
				{
					label: __('Customer'), fieldname: 'customer', fieldtype: 'Link', options: 'Customer', reqd: 1,
					default: posCustomer,
					change: () => {
						// Banned box / alerts first (customer_flags.js), as the Trade-in does.
						const cf = window.ffl && ffl.customerFlags;
						if (cf && cf.guardControl) cf.guardControl(dialog.fields_dict.customer);
					},
				},
				{ fieldtype: 'Section Break', label: __('Firearm') },
				{ label: __('Manufacturer'), fieldname: 'manufacturer', fieldtype: 'Data', reqd: 1 },
				{ label: __('Model'), fieldname: 'model', fieldtype: 'Data', reqd: 1 },
				{ label: __('Caliber / Gauge'), fieldname: 'caliber', fieldtype: 'Data',
					mandatory_depends_on: 'eval:doc.log_in_bound_book' },
				{ fieldtype: 'Column Break' },
				{ label: __('Serial Number'), fieldname: 'serial_number', fieldtype: 'Data', reqd: 1 },
				{ label: __('Firearm Type'), fieldname: 'firearm_type', fieldtype: 'Select', options: ffl.FIREARM_TYPE_OPTIONS,
					mandatory_depends_on: 'eval:doc.log_in_bound_book' },
				{ fieldtype: 'Section Break' },
				{ label: __('Accessories left with it'), fieldname: 'accessories', fieldtype: 'Small Text' },
				{ fieldtype: 'Column Break' },
				{ label: __('Condition at intake'), fieldname: 'condition', fieldtype: 'Small Text' },
				{ fieldtype: 'Section Break', label: __('Work') },
				{ label: __('Work Requested'), fieldname: 'work_requested', fieldtype: 'Small Text', reqd: 1 },
				{ label: __('Estimated Cost'), fieldname: 'estimated_cost', fieldtype: 'Currency' },
				{ label: __('Estimated Completion'), fieldname: 'estimated_completion', fieldtype: 'Date' },
				{ fieldtype: 'Column Break' },
				{ label: __('Pay now'), fieldname: 'pay_now', fieldtype: 'Currency',
					description: __('Optional. Goes into this sale as a payment towards the job, taken at checkout, held for the customer and taken off the bill at pick-up.') },
				{ fieldtype: 'Section Break', label: __('Bound book') },
				{ label: __('Log in bound book'), fieldname: 'log_in_bound_book', fieldtype: 'Check',
					description: __('Received from the customer as a Gunsmithing acquisition, held in customer custody at zero cost (never for sale); a Gunsmithing disposition returns it at pick-up.') },
				{ label: __('Customer ID Type'), fieldname: 'id_type', fieldtype: 'Select',
					options: ['', 'Driver License', 'State ID', 'Passport', 'Military ID', 'Other'].join('\n'),
					depends_on: 'eval:doc.log_in_bound_book', mandatory_depends_on: 'eval:doc.log_in_bound_book' },
				{ fieldtype: 'Column Break' },
				{ label: __('Customer ID Number'), fieldname: 'id_number', fieldtype: 'Data',
					depends_on: 'eval:doc.log_in_bound_book', mandatory_depends_on: 'eval:doc.log_in_bound_book' },
			],
			primary_action_label: __('Save work order'),
			primary_action(values) {
				frappe.call({
					method: GW + 'create_work_order', freeze: true, freeze_message: __('Saving work order…'),
					args: { payload: JSON.stringify(values) },
				}).then(async (r) => {
					const m = (r && r.message) || {};
					if (!m.name) return;
					dialog.hide();
					if (!m.deposit_line) {
						frappe.show_alert({ indicator: 'green', message: __('Work order {0} saved.', [m.name]) }, 8);
					} else {
						paymentAdded(await addPaymentToCart(m.customer, m.deposit_line), m.name, m.deposit_line.rate);
					}
					// Then the customer signs the intake authorization (the paper form's signature).
					authorizeAtRegister({ name: m.name, customer: m.customer, customer_name: m.customer_name },
						m.authorization_template);
				});
			},
		});
		dialog.show();
	}

	function startObserver() {
		// Try once immediately in case the page is already rendered.
		maybeInjectButton(document);
		const observer = new MutationObserver((mutations) => {
			for (const m of mutations) {
				if (m.addedNodes.length) maybeInjectButton(document);
			}
		});
		observer.observe(document.body, { childList: true, subtree: true });
	}

	function isPosRoute() {
		return location.hash.includes('point-of-sale')
			|| location.pathname.includes('/point-of-sale');
	}

	function init() {
		if (!window.frappe) return;
		// Only activate on the POS route; we still load the script everywhere
		// because Frappe Desk is a SPA and route changes don't reload scripts.
		frappe.router && frappe.router.on('change', () => {
			if (isPosRoute()) { startObserver(); takeRouteJob(); }
		});
		if (isPosRoute()) { startObserver(); takeRouteJob(); }
		injectPosTouchStyles();
	}

	// Tablet-friendly POS payment screen: ERPNext's default numpad-container
	// is a small 262×278 block floating in the bottom-right of the right
	// pane, leaving most of the panel empty. Centers it horizontally,
	// enlarges buttons to ~72 px for thumb taps, hides the empty
	// .fields-section so the numpad can sit near the top of its column.
	let _posStylesInjected = false;
	function injectPosTouchStyles() {
		if (_posStylesInjected) return;
		_posStylesInjected = true;
		const style = document.createElement('style');
		style.setAttribute('data-gunstore-pos', '1');
		style.textContent = `
			.point-of-sale-app .fields-numpad-container {
				display: flex;
				flex-direction: column;
				justify-content: flex-start;
				align-items: center;
				padding: 24px 16px;
				gap: 16px;
			}
			.point-of-sale-app .fields-numpad-container .fields-section {
				width: 100%;
				display: flex;
				justify-content: center;
			}
			.point-of-sale-app .fields-numpad-container .fields-section:has(> .invoice-fields > .addl-fields.hidden:only-child) {
				display: none;
			}
			.point-of-sale-app .number-pad {
				position: static !important;
				width: 100%;
				max-width: 560px;
				margin-left: auto !important;
				margin-right: auto !important;
				align-self: center !important;
				left: auto !important;
				right: auto !important;
				bottom: auto !important;
			}
			.point-of-sale-app .numpad-container {
				display: grid !important;
				grid-template-columns: repeat(3, 1fr);
				gap: 12px;
				width: 100%;
			}
			.point-of-sale-app .numpad-btn {
				min-height: 72px !important;
				font-size: 22px !important;
				font-weight: 500;
				border-radius: 10px;
				display: flex !important;
				align-items: center;
				justify-content: center;
				cursor: pointer;
				user-select: none;
				-webkit-tap-highlight-color: rgba(0,0,0,0.05);
				transition: background-color .08s, transform .04s;
			}
			.point-of-sale-app .numpad-btn:active {
				transform: scale(.97);
			}
			/* Payment mode tiles also wider for touch */
			.point-of-sale-app .payment-modes .mode-of-payment-control,
			.point-of-sale-app .payment-modes [class*="mode-of-payment"] input {
				min-height: 44px;
				font-size: 16px;
			}
			.point-of-sale-app .submit-order-btn {
				min-height: 56px;
				font-size: 18px !important;
				font-weight: 600;
			}
			/* Payment held for the 4473: the button has nothing left to do until
			   the 4473 row above lands, so it goes quiet (label says why). Still
			   clickable on purpose — the click re-checks the 4473 server-side. */
			.point-of-sale-app .submit-order-btn.ffl-submit-held,
			.point-of-sale-app .submit-order-btn.ffl-submit-blocked {
				opacity: .55;
			}
			/* Negative ticket: the store pays the customer (negative-ticket screen). */
			.point-of-sale-app .ffl-payout { padding: 8px 4px; }
			.point-of-sale-app .ffl-payout-head { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
			.point-of-sale-app .ffl-payout-label { font-size: 12px; margin: 14px 0 6px; }
			.point-of-sale-app .ffl-payout-methods { display: flex; gap: 8px; flex-wrap: wrap; }
			.point-of-sale-app .ffl-payout-btn {
				flex: 1 1 72px; min-height: 56px; display: flex; align-items: center; justify-content: center;
				border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
				font-weight: 600; cursor: pointer; user-select: none;
			}
			.point-of-sale-app .ffl-payout-btn.ffl-payout-on { border: 2px solid var(--primary); background: var(--control-bg); }
			.point-of-sale-app .ffl-payout-ref { min-height: 44px; font-size: 16px; }
			.point-of-sale-app .ffl-payout-hint { margin-top: 10px; font-size: 13px; min-height: 18px; }
		`;
		document.head.appendChild(style);
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', init);
	} else {
		init();
	}
})();


// --- Release notes → HTML (What's New above; the Extensions page) ---------------
//
// window.gunstore.renderReleaseNotes([[version, markdown], …]) — frappe's change_log
// reader shape, newest first as given. frappe.markdown is frappe's one shared showdown
// converter (frappe.md2html, made on its first use), which by default reads an underscore
// inside a word as emphasis: ext_hello_dep became ext<em>hello</em>dep. Release notes turn
// showdown's own literalMidWordUnderscores on for this call only and put the converter's
// option back afterwards, so the rest of the desk's markdown reads as frappe has it.
// _em_ and __strong__ stay markdown; code, inline HTML and link ids are showdown's to read.
// Node-tested (tests/js/release_notes.test.js runs this block alone), so it uses nothing
// but window and frappe.markdown / frappe.md2html.

(function releaseNotes() {
	'use strict';

	window.gunstore = window.gunstore || {};
	window.gunstore.renderReleaseNotes = function (entries) {
		if (!(entries || []).length) return '';
		if (!frappe.md2html) frappe.markdown('');
		const converter = frappe.md2html;
		const before = converter.getOption('literalMidWordUnderscores');
		converter.setOption('literalMidWordUnderscores', true);
		try {
			return `<div class="text-left app-change-log">${entries.map((e) => frappe.markdown(String(e[1] || ''))).join('<hr>')}</div>`;
		} finally {
			converter.setOption('literalMidWordUnderscores', before);
		}
	};
})();

// --- Force-delete dialog (shared between Item form + Item list view) -------
//
// Exposes window.gunStoreForceDeleteItems(itemCodes). Called from
// public/js/item_form.js (single-item Delete button) and item_list.js
// (bulk-actions menu). Both Item routes have the bundle loaded already
// via app_include_js, so this is the single source of truth for the
// preview + cancel/delete confirm flow.

(function () {
	'use strict';

	window.gunStoreForceDeleteItems = function (itemCodes) {
		if (!itemCodes || !itemCodes.length) return;
		frappe.call({
			method: 'ffl_core.api.item_admin.preview_delete',
			args: {item_codes: JSON.stringify(itemCodes)},
			callback: (r) => showPreview(itemCodes, r.message || {}),
		});
	};

	function showPreview(itemCodes, preview) {
		const submitted = preview.submitted || [];
		const drafts = preview.drafts || [];
		const simple = preview.simple || [];
		const missing = preview.missing || [];

		const rowsHtml = (arr, label, color) => arr.length
			? `<div style="margin-top:8px"><div style="font-weight:600;color:${color}">${label}</div><ul style="margin:4px 0 0 16px;padding:0;font-size:12px">${
				arr.map((r) => `<li>${frappe.utils.escape_html(r.doctype)} <span style="color:var(--text-muted)">(${r.count})</span></li>`).join('')
			}</ul></div>`
			: '';

		const hasSubmitted = submitted.length > 0;
		const totalLinks = submitted.reduce((s, x) => s + x.count, 0)
			+ drafts.reduce((s, x) => s + x.count, 0)
			+ simple.reduce((s, x) => s + x.count, 0);

		const itemsHtml = itemCodes.map((c) => `<li>${frappe.utils.escape_html(c)}</li>`).join('');
		const missingHtml = missing.length
			? `<div style="margin-top:8px;color:var(--red-600)"><b>${__('Not found')}:</b> ${missing.map(frappe.utils.escape_html).join(', ')}</div>`
			: '';

		const headlineColor = hasSubmitted ? 'var(--red-600)' : (totalLinks ? 'var(--orange-600)' : 'var(--green-600)');
		const headline = hasSubmitted
			? __('Will cancel {0} submitted document(s) and delete the item(s).', [submitted.reduce((s, x) => s + x.count, 0)])
			: totalLinks
			? __('Will delete {0} linked record(s) and the item(s).', [totalLinks])
			: __('No linked records. Safe to delete.');

		const d = new frappe.ui.Dialog({
			title: __('Force Delete Items'),
			size: 'large',
			fields: [{fieldtype: 'HTML', fieldname: 'body'}],
			primary_action_label: hasSubmitted ? __('Cancel & Delete') : __('Delete'),
			primary_action() {
				frappe.call({
					method: 'ffl_core.api.item_admin.force_delete',
					args: {
						item_codes: JSON.stringify(preview.item_codes || itemCodes),
						cancel_submitted: hasSubmitted ? 1 : 0,
					},
					freeze: true,
					freeze_message: __('Deleting…'),
					callback: (r) => {
						const out = r.message || {};
						d.hide();
						if (!out.ok && (out.errors || []).length) {
							frappe.msgprint({
								title: __('Delete completed with errors'),
								message: `<pre style="white-space:pre-wrap;font-size:12px">${frappe.utils.escape_html((out.errors || []).join('\n'))}</pre>`,
								indicator: 'orange',
							});
						} else {
							frappe.show_alert({
								message: __('Deleted {0} item(s); cancelled {1}; removed {2} draft(s).',
									[(out.deleted || []).length, (out.cancelled || []).length, (out.deleted_drafts || []).length]),
								indicator: 'green',
							});
						}
						if (window.cur_frm && window.cur_frm.doctype === 'Item' && (out.deleted || []).includes(window.cur_frm.doc.name)) {
							frappe.set_route('List', 'Item');
						} else if (window.cur_list && window.cur_list.refresh) {
							window.cur_list.refresh();
						}
					},
				});
			},
		});

		const $body = d.fields_dict.body.$wrapper;
		$body.html(`
			<div style="font-size:13px">
				<div style="color:${headlineColor};font-weight:600">${headline}</div>
				<div style="margin-top:8px"><b>${__('Items to delete')}:</b></div>
				<ul style="margin:4px 0 0 16px;padding:0;font-size:12px">${itemsHtml}</ul>
				${rowsHtml(submitted, __('Submitted documents to CANCEL'), 'var(--red-600)')}
				${rowsHtml(drafts, __('Draft documents to delete'), 'var(--orange-600)')}
				${rowsHtml(simple, __('Linked records to remove'), 'var(--text-muted)')}
				${missingHtml}
				<div style="margin-top:12px;padding:8px;background:var(--subtle-fg);border-radius:4px;font-size:12px;color:var(--text-muted)">
					${__('This action is irreversible. Cancelled documents retain their cancelled history; deleted ledger / serial / FFL records cannot be recovered.')}
				</div>
			</div>
		`);
		d.get_primary_btn().removeClass('btn-primary').addClass('btn-danger');
		d.show();
	}
})();


// --- ItemQuickEntryForm — adds RSR scan banner to the "+ Add Item" dialog
//
// frappe.new_doc('Item') from the list view (and any "+ New" Link field)
// dispatches through frappe.ui.form.make_quick_entry, which checks for a
// custom `${Doctype}QuickEntryForm` class and falls back to QuickEntryForm.
// We register an Item-specific subclass that injects a UPC scan input at
// the top of the dialog body. On match, the visible dialog fields fill in
// (item_code, item_name, item_group, is_stock_item, is_used) and the
// non-dialog fields (manufacturer, model_name, dealer_price, image,
// firearm_type, listing_status, description) are stamped onto the
// underlying doc so they persist when Save is clicked.
//
// Loaded globally via app_include_js so the override is registered before
// any frappe.new_doc('Item') call regardless of source route.

frappe.provide('frappe.ui.form');

(function () {
	'use strict';

	if (!frappe.ui.form.QuickEntryForm) return; // base not loaded yet — skip

	const Base = frappe.ui.form.ItemQuickEntryForm || frappe.ui.form.QuickEntryForm;

	frappe.ui.form.ItemQuickEntryForm = class ItemQuickEntryForm extends Base {
		render_dialog() {
			super.render_dialog();
			injectRsrScanBanner(this);
		}
	};

	function injectRsrScanBanner(qe) {
		const $banner = $(`
			<div class="rsr-quick-add-banner" style="
				background: var(--subtle-fg);
				border: 1px solid var(--border-color);
				border-radius: 6px;
				padding: 10px 12px;
				margin: 0 0 14px 0;
			">
				<div style="display:flex; align-items:center; gap:8px; margin-bottom:8px">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
						stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--text-muted)">
						<rect x="3" y="3" width="6" height="18"></rect>
						<rect x="11" y="3" width="2" height="18"></rect>
						<rect x="15" y="3" width="6" height="18"></rect>
					</svg>
					<div style="font-weight:600; font-size:12px">${__('Quick Add — Scan UPC / RSR Stock # / MFG #')}</div>
				</div>
				<input type="text"
					class="form-control rsr-qe-scan-input"
					placeholder="${__('Scan barcode or type 3+ characters…')}"
					style="font-size:13px; padding:6px 10px"
					autocomplete="off"
					spellcheck="false">
				<div class="rsr-qe-scan-results" style="margin-top:8px"></div>
			</div>
		`);
		qe.$body.prepend($banner);

		const $input = $banner.find('.rsr-qe-scan-input');
		const $results = $banner.find('.rsr-qe-scan-results');
		let lastQuery = '';
		let debounceTimer = null;

		$input.on('input', function () {
			const q = ($(this).val() || '').trim();
			if (q === lastQuery) return;
			lastQuery = q;
			clearTimeout(debounceTimer);
			if (q.length < 3) {
				$results.empty();
				return;
			}
			debounceTimer = setTimeout(() => doLookup(qe, $banner, q), 220);
		});

		$input.on('keydown', function (e) {
			if (e.key === 'Enter') {
				e.preventDefault();
				const q = ($(this).val() || '').trim();
				if (q) doLookup(qe, $banner, q, /*autoApply=*/true);
			}
		});

		setTimeout(() => $input.focus(), 80);
	}

	function doLookup(qe, $banner, q, autoApply) {
		// No distributor catalog here: the shared notice, not a request the server refuses.
		if (ffl.FirearmItemPicker.catalogOff()) {
			$banner.find('.rsr-qe-scan-results').html(ffl.capabilities.notice('catalog'));
			return;
		}
		frappe.call({
			method: 'ffl_integrations.rsr.promote.lookup_for_item_form',
			args: { scan: q },
			callback: (r) => {
				const m = r.message || {};
				renderResult($banner, m, q);
				if (m.found) {
					const isUpcShape = /^\d{8,14}$/.test(q);
					if (autoApply || (isUpcShape && m.rsr.upc === q)) {
						window.gunstore.applyRsrToDoc(qe, m.defaults, m.rsr);
						$banner.find('.rsr-qe-scan-input').val('');
						$banner.find('.rsr-qe-scan-results').empty();
						frappe.show_alert({
							message: __('Filled from RSR · {0}', [m.rsr.rsr_stock_number]),
							indicator: 'green',
						});
					}
				} else if (autoApply) {
					frappe.show_alert({ message: __('No RSR match for {0}', [q]), indicator: 'orange' });
				}
			},
		});
	}

	function renderResult($banner, m, q) {
		const $results = $banner.find('.rsr-qe-scan-results');
		if (!m.found) {
			$results.html(`
				<div style="color:var(--text-muted); font-size:11px; padding:8px; text-align:center; border:1px dashed var(--border-color); border-radius:4px">
					${__('No RSR catalog match for {0}.', [frappe.utils.escape_html(q)])}
				</div>
			`);
			return;
		}
		// Compact card variant for the QuickEntry dialog (smaller thumb / fonts).
		const $card = $(window.gunstore.renderRsrCard(m.rsr || {}, m.defaults || {},
			{thumb: 56, name: 12, meta: 11}));
		$card.addClass('rsr-qe-scan-row');
		$results.empty().append($card);
		$card.on('click', () => {
			const qe = frappe.quick_entry;
			if (qe) window.gunstore.applyRsrToDoc(qe, m.defaults, m.rsr);
			$banner.find('.rsr-qe-scan-input').val('');
			$results.empty();
			frappe.show_alert({
				message: __('Filled from RSR · {0}', [m.rsr.rsr_stock_number]),
				indicator: 'green',
			});
		});
	}
})();


// --- Shared helpers exposed on window.gunstore -----------------------------
//
// Used by item_form.js (full-form scan banner + re-scan modal) and the
// QuickEntryForm scan injection above. Single source of truth for:
//   - renderRsrCard: HTML for an RSR result row (thumb + name + prices + badge)
//   - applyRsrToDoc: copy defaults onto a doc-like + append UPC barcode
//                     and image_gallery primary row (no double-add)
//
// `target` is either a Frappe form (frm) or a QuickEntryForm instance (qe).
// Both expose set_value / fields_dict / doc / add_child or doc-mutation paths;
// the helper detects which and routes accordingly.

(function () {
	'use strict';
	window.gunstore = window.gunstore || {};

	// --- Serial-number correction (shared by BOTH entry points) --------------
	// The Serial No form button (public/js/serial_no.js) and the Item form's
	// Serial Numbers tab (this bundle) both open THIS one dialog and call THIS one
	// server method, so the two paths can never drift: New Serial + Reason are
	// required, then correct_serial atomically renames the Serial No (stock / price
	// / photos / A&D / FastBound link preserved), repoints the submitted A&D and
	// edits the FastBound bound book. UX gating only — the server re-checks the
	// manager role via frappe.only_for.
	window.gunstore.canCorrectSerial = function () {
		const roles = frappe.user_roles || [];
		return roles.includes('Stock Manager') || roles.includes('System Manager');
	};

	window.gunstore.correctSerialDialog = function (serial, opts) {
		const o = opts || {};
		const d = new frappe.ui.Dialog({
			title: __('Correct Serial Number'),
			fields: [
				{
					fieldtype: 'HTML', fieldname: 'intro',
					options: `<div class="alert alert-warning" style="margin-bottom:10px">${
						__('Fix a mistyped serial on an <b>in-stock</b> firearm. This renames the Serial No (stock, price, photos, A&amp;D record and the FastBound link are all preserved) and updates the FastBound bound book in one atomic step. Sold / disposed guns are not supported.')}</div>`,
				},
				{
					fieldtype: 'Data', fieldname: 'new_serial', reqd: 1,
					label: __('Corrected (new) Serial Number'),
					description: __('Current serial: {0}', [frappe.utils.escape_html(String(serial))]),
				},
				{
					fieldtype: 'Small Text', fieldname: 'reason', reqd: 1,
					label: __('Reason for correction'),
					description: __('Written to the A&amp;D audit trail and pushed to FastBound.'),
				},
			],
			primary_action_label: __('Correct Serial'),
			primary_action(values) {
				const newSerial = (values.new_serial || '').trim();
				const reason = (values.reason || '').trim();
				if (!newSerial) { frappe.msgprint(__('Enter the corrected serial number.')); return; }
				if (!reason) { frappe.msgprint(__('Enter a reason for the correction.')); return; }
				if (newSerial === String(serial)) {
					frappe.msgprint(__('The corrected serial is the same as the current one.'));
					return;
				}
				frappe.call({
					method: 'ffl_core.api.serial_correction.correct_serial',
					args: { serial: serial, new_serial: newSerial, reason: reason },
					freeze: true,
					freeze_message: __('Correcting serial…'),
					callback(r) {
						// On a server throw, frappe.call surfaces the error itself and
						// this callback carries no ok — leave the dialog open to retry.
						const m = (r && r.message) || {};
						if (!m.ok) return;
						d.hide();
						frappe.show_alert({
							message: __('Serial corrected {0} → {1}. FastBound updated.',
								[m.old_serial, m.new_serial]),
							indicator: 'green',
						}, 7);
						if (typeof o.onDone === 'function') o.onDone(m);
					},
				});
			},
		});
		d.show();
	};

	// --- Cost correction (shared by ALL THREE entry points) ------------------
	// The Item form, the Serial No form and the Firearms In Stock report all open
	// THIS dialog and call THIS pair of server methods
	// (ffl_core.api.cost_correction.list_item_serials_for_cost /
	// correct_serial_cost), for the same reason the serial-number correction
	// above is shared: three copies of a money-writing dialog would drift, and
	// Extra entry points are wanted, not extra APIs.
	//
	// Two modes, one dialog:
	//   * {item_code}          — pick among that Item's eligible serials.
	//   * {item_code, serial}  — that ONE gun, no picker (the Serial No form and
	//                            the report row already know which gun it is).
	// Eligibility always comes from the server list, so a gun the server would
	// refuse is never offered — including the case where the caller passes a
	// serial that is sold or consigned TO us.
	// UX gating only; correct_serial_cost re-checks the role and every gate.
	window.gunstore.correctCostDialog = function (opts) {
		const o = opts || {};
		const fmt = (v) => format_currency(v, frappe.defaults.get_default('currency'));

		frappe.call({
			method: 'ffl_core.api.cost_correction.list_item_serials_for_cost',
			args: { item_code: o.item_code },
			freeze: true,
			freeze_message: __('Loading serials…'),
			callback: (r) => {
				let serials = ((r.message || {}).serials) || [];
				if (o.serial) serials = serials.filter((s) => s.serial === o.serial);
				if (!serials.length) {
					frappe.msgprint({
						title: __('No eligible serials'),
						message: o.serial
							? __('{0} cannot be cost-corrected: only an in-stock gun the store owns qualifies (a sold gun\'s cost is already realised, and a gun consigned TO us was never ours).', [o.serial])
							: __('This firearm has no in-stock, store-owned serials whose cost can be corrected.'),
						indicator: 'orange',
					});
					return;
				}

				const byName = {};
				serials.forEach((s) => { byName[s.serial] = s; });
				const first = serials[0];

				const d = new frappe.ui.Dialog({
					title: o.serial ? __('Correct Cost — {0}', [o.serial])
						: __('Correct Cost — {0}', [o.item_code]),
					size: 'small',
					fields: [
						{
							label: __('Serial Number'), fieldname: 'serial', fieldtype: 'Select',
							options: serials.map((s) => s.serial).join('\n'), reqd: 1,
							default: first.serial,
							// One gun = nothing to choose; keep the field visible so the
							// dialog still states WHICH gun is about to be revalued.
							read_only: o.serial ? 1 : 0,
						},
						{ fieldtype: 'HTML', fieldname: 'current' },
						{
							label: __('New Cost'), fieldname: 'new_cost', fieldtype: 'Currency',
							reqd: 1, default: first.purchase_rate || null,
							description: __('The corrected acquisition cost. Must be greater than 0. Records to Stock Adjustment (P&L) — no cash/AP entry.'),
						},
						{
							label: __('Reason'), fieldname: 'reason', fieldtype: 'Small Text', reqd: 1,
							description: __('Required for the audit trail (e.g. "Found dealer invoice — true cost $900").'),
						},
						{
							label: __('The amount actually paid to the seller was different'),
							fieldname: 'payout_was_different', fieldtype: 'Check', default: 0,
							hidden: first.cash_payout ? 0 : 1,
							description: __('Tick it to book the difference to the cash drawer or bank; leave it empty and only the cost changes.'),
						},
					],
					primary_action_label: __('Correct Cost'),
					primary_action(values) {
						frappe.call({
							method: 'ffl_core.api.cost_correction.correct_serial_cost',
							args: {
								serial: values.serial,
								new_cost: values.new_cost,
								reason: values.reason,
								payout_was_different: values.payout_was_different ? 1 : 0,
							},
							freeze: true,
							freeze_message: __('Posting correction…'),
							callback: (res) => {
								const m = res.message || {};
								if (!m.ok) return;
								d.hide();
								frappe.show_alert({
									message: __('{0}: cost {1} → {2} · Stock Reconciliation {3}',
										[m.serial, fmt(m.old_cost), fmt(m.new_cost), m.stock_reconciliation]),
									indicator: 'green',
								}, 7);
								if (m.payout_note) {
									frappe.msgprint({ title: __('Cash drawer'), message: m.payout_note, indicator: 'orange' });
								}
								if (m.source_of_cost_hint) {
									frappe.msgprint({
										title: __('Bookkeeping note'),
										message: __('This gun had ~no recorded cost before. This correction records the value to <b>Stock Adjustment (P&amp;L)</b> only — it does <b>not</b> record any cash/AP outflow. If the purchase was paid but never booked, a bookkeeper should post a source-of-cost journal entry (Dr Stock Adjustment / Cr Cash or A/P).'),
										indicator: 'blue',
									});
								}
								if (o.onDone) o.onDone(m);
							},
						});
					},
				});

				const renderCurrent = (serial) => {
					const s = byName[serial];
					const $w = d.fields_dict.current.$wrapper;
					if (!s) { $w.empty(); return; }
					d.set_df_property('payout_was_different', 'hidden', s.cash_payout ? 0 : 1);
					d.set_value('payout_was_different', 0);
					$w.html(`<div style="padding:4px 0 2px; color:var(--text-muted); font-size:12px">
						${__('Current recorded cost')}: <b>${fmt(s.purchase_rate)}</b>
						&nbsp;·&nbsp; ${__('ledger valuation')}: <b>${fmt(s.valuation)}</b>
						&nbsp;·&nbsp; ${__('warehouse')}: ${frappe.utils.escape_html(s.warehouse || '')}</div>${s.goods_payout
						? `<div style="color:var(--text-muted); font-size:12px">${__('The seller was paid with its receipt ({0}): a different amount paid is booked with that payment.',
							[frappe.utils.escape_html(s.goods_payout)])}</div>` : ''}`);
				};

				d.fields_dict.serial.df.onchange = () => {
					const serial = d.get_value('serial');
					renderCurrent(serial);
					const s = byName[serial];
					if (s) d.set_value('new_cost', s.purchase_rate || null);
				};

				d.show();
				renderCurrent(first.serial);
			},
		});
	};

	// --- Image preview helpers (shared by the Item + Serial No forms) --------
	// Both doctypes carry the same `image` (Attach Image) + `image_gallery`
	// (Item Image Gallery child table) fields, so the rendering is identical.

	// Replace the gallery grid's image-URL text with an inline thumbnail.
	window.gunstore.customizeImageGalleryGrid = function (frm) {
		const grid = frm.fields_dict.image_gallery && frm.fields_dict.image_gallery.grid;
		if (!grid) return;
		const escape = frappe.utils.escape_html;
		grid.update_docfield_property('image', 'formatter', (value) => {
			if (!value) return '';
			return `<img src="${escape(value)}"
				style="height:40px; max-width:80px; object-fit:cover;
					border-radius:3px; border:1px solid var(--border-color);
					vertical-align:middle;"
				onerror="this.style.display='none'">`;
		});
		grid.refresh();
	};

	// Photo manager — the one UI for a document's photo gallery (Item and Serial
	// No share it). Multi-file upload through Frappe's own uploader dialog (it
	// owns progress / per-file failures / size+type limits), drag to reorder
	// (Desk's global Sortable), one click to feature, inline caption, remove.
	// Every action persists at once through ffl_core.api.photos and reloads the
	// form — so it refuses to act while the form has unsaved changes (a server
	// save would overwrite them) and until a new document is saved (uploads need
	// a docname to attach to). Data model unchanged: still the image_gallery
	// child table; the raw grid stays below, collapsed, for the rare row-level look.
	window.gunstore.ensurePhotoStyles = function () {
		if (document.getElementById('gunstore-photo-styles')) return;
		$('<style id="gunstore-photo-styles">').text(`
			.gunstore-photo-manager { padding: 14px var(--padding-md, 15px) 18px; margin: 0 0 4px; border-bottom: 1px solid var(--border-color); }
			.gunstore-photo-manager.is-dragover .pm-drop { border-color: var(--primary); background: var(--bg-light-blue, var(--fg-hover-color)); }
			.pm-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
			.pm-head .pm-title { font-weight: 600; font-size: 14px; }
			.pm-head .pm-spacer { flex: 1; }
			.pm-grid { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-start; margin-bottom: 12px; }
			.pm-card { position: relative; width: 120px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--card-bg); overflow: hidden; outline: none; }
			.pm-card:focus { box-shadow: 0 0 0 2px var(--primary); }
			.pm-card.is-primary { width: 200px; border-color: var(--primary); }
			.pm-card.is-broken { opacity: .45; }
			.pm-card .pm-img { display: block; }
			.pm-card .pm-img img { display: block; width: 100%; height: 120px; object-fit: cover; }
			.pm-card.is-primary .pm-img img { height: 200px; }
			.pm-badge { position: absolute; top: 6px; left: 6px; font-size: 11px; padding: 2px 7px; border-radius: 10px; background: var(--primary); color: #fff; }
			.pm-drag { position: absolute; top: 4px; left: 6px; font-size: 16px; line-height: 1; color: #fff; text-shadow: 0 0 3px rgba(0,0,0,.8); cursor: grab; user-select: none; }
			.pm-card.sortable-ghost { opacity: .4; }
			.pm-tools { display: flex; gap: 4px; justify-content: flex-end; padding: 4px 6px; }
			.pm-tools .btn-xs { padding: 0 6px; line-height: 20px; font-size: 13px; }
			.pm-cap { font-size: 11px; color: var(--text-muted); padding: 0 6px 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
			.pm-drop { border: 2px dashed var(--border-color); border-radius: 8px; padding: 16px; text-align: center; color: var(--text-muted); font-size: 13px; transition: border-color .15s, background .15s; }
			.pm-drop.is-disabled { opacity: .6; }
			.pm-drop .pm-drop-sub { font-size: 12px; margin-top: 4px; }
			.pm-empty { color: var(--text-muted); font-size: 13px; margin-bottom: 10px; }
		`).appendTo(document.head);
	};

	// Frappe uploader dialog for photos. Resolves with the file_urls that made
	// it (the dialog itself shows what did not). `files` pre-fills a drop.
	window.gunstore.pickPhotos = function ({ doctype, docname, files }) {
		return new Promise((resolve) => {
			const urls = [];
			const up = new frappe.ui.FileUploader({
				doctype, docname,
				files: files && files.length ? files : undefined,
				allow_multiple: true,
				make_attachments_public: true,
				disable_file_browser: true,
				allow_web_link: false,
				dialog_title: __('Add photos'),
				restrictions: {
					allowed_file_types: ['image/jpeg', 'image/png', 'image/webp', 'image/gif'],
					max_file_size: 10 * 1024 * 1024,
				},
				upload_notes: __('JPG / PNG / WEBP, up to 10 MB each. You can select several at once. Photos are made public — the web store uses them.'),
				on_success(file) { if (file && file.file_url) urls.push(file.file_url); },
			});
			const $w = up.dialog && up.dialog.$wrapper;
			if ($w) { $w.one('hidden.bs.modal', () => resolve(urls)); } else { resolve(urls); }
		});
	};

	window.gunstore.renderPhotoManager = function (frm, opts) {
		const o = opts || {};
		const escape = frappe.utils.escape_html;
		window.gunstore.ensurePhotoStyles();
		const rows = [...(frm.doc.image_gallery || [])]
			.filter((r) => (r.image || '').trim())
			.sort((a, b) => ((b.is_primary ? 1 : 0) - (a.is_primary ? 1 : 0))
				|| ((a.sort_order || a.idx || 0) - (b.sort_order || b.idx || 0)));
		const isNew = frm.is_new();
		const dirty = !isNew && frm.is_dirty();
		const key = [frm.doctype, frm.doc.name, isNew ? 'new' : (dirty ? 'dirty' : 'clean'),
			...rows.map((r) => `${r.name}:${r.image}:${r.is_primary ? 1 : 0}:${r.caption || ''}`)].join('|');
		if (frm._photoKey === key) return;
		frm._photoKey = key;
		frm.layout.wrapper.find('.gunstore-photo-manager').remove();

		const canAct = !isNew && !dirty;
		const hintFor = () => frm.is_new() ? __('Save first, then add photos.')
			: frm.is_dirty() ? __('Save the form first — photo actions write to the document directly.')
			: __('Drop photos here, or click Add photos. Several at once is fine.');
		const hint = hintFor();
		const rerender = () => { frm._photoKey = null; window.gunstore.renderPhotoManager(frm, o); };
		// Guard at ACTION time, not render time: the user may have typed into a
		// field since this rendered, and a server-side save would throw that away.
		const blocked = () => {
			if (!frm.is_new() && !frm.is_dirty()) return false;
			frappe.show_alert({ message: hintFor(), indicator: 'orange' });
			rerender();
			return true;
		};
		const call = (method, args) => frappe.call({ method: `ffl_core.api.photos.${method}`, args, freeze: true })
			.then(() => { frm._photoKey = null; return frm.reload_doc(); });
		const dis = canAct ? '' : ' disabled';
		// Re-render the moment the form turns dirty so the disabled state shows
		// without waiting for the next refresh; refresh() covers turning clean again.
		if (!frm._photoDirtyHook) {
			frm._photoDirtyHook = true;
			frm.$wrapper.on('dirty', () => rerender());
		}

		const cards = rows.map((r) => `
			<div class="pm-card${r.is_primary ? ' is-primary' : ''}" data-row="${escape(r.name)}" tabindex="0"
				aria-label="${escape(r.caption || frm.doc.name)}${r.is_primary ? ' — ' + __('featured') : ''}">
				<a class="pm-img" href="${escape(r.image)}" target="_blank" rel="noopener" title="${__('Open full size')}">
					<img src="${escape(r.image)}" alt="${escape(r.caption || frm.doc.name)}" loading="lazy"
						onerror="this.closest('.pm-card').classList.add('is-broken')">
				</a>
				${r.is_primary ? `<span class="pm-badge">★ ${__('Featured')}</span>`
					: `<span class="pm-drag" title="${__('Drag to reorder')}" aria-hidden="true">⠿</span>`}
				<div class="pm-tools">
					${r.is_primary ? '' : `<button type="button" class="btn btn-default btn-xs pm-star" data-row="${escape(r.name)}" title="${__('Make featured')}" aria-label="${__('Make featured')}"${dis}>☆</button>`}
					<button type="button" class="btn btn-default btn-xs pm-caption" data-row="${escape(r.name)}" title="${__('Caption')}" aria-label="${__('Caption')}"${dis}>✎</button>
					<button type="button" class="btn btn-default btn-xs pm-remove" data-row="${escape(r.name)}" title="${__('Remove')}" aria-label="${__('Remove')}"${dis}>×</button>
				</div>
				${r.caption ? `<div class="pm-cap" title="${escape(r.caption)}">${escape(r.caption)}</div>` : ''}
			</div>`).join('');

		const batchLink = o.batchRoute
			? `<button type="button" class="btn btn-default btn-xs pm-batch">${__('Upload by serial…')}</button>` : '';
		const $root = $(`
			<div class="gunstore-photo-manager">
				<div class="pm-head">
					<span class="pm-title">${__('Photos')} (${rows.length})</span>
					<span class="pm-spacer"></span>
					${batchLink}
					<button type="button" class="btn btn-primary btn-xs pm-add"${dis}>＋ ${__('Add photos')}</button>
				</div>
				${rows.length ? `<div class="pm-grid">${cards}</div>` : `<div class="pm-empty">${__('No photos yet.')}</div>`}
				<div class="pm-drop${canAct ? '' : ' is-disabled'}" role="button" tabindex="0">
					<div>${escape(hint)}</div>
					<div class="pm-drop-sub">${__('JPG / PNG / WEBP · up to 10 MB each · made public for the web store')}</div>
				</div>
			</div>`);

		const dt = frm.doctype, dn = frm.doc.name;
		const add = (files) => {
			if (blocked()) return;
			window.gunstore.pickPhotos({ doctype: dt, docname: dn, files }).then((urls) => {
				if (urls.length) call('add_gallery_images', { doctype: dt, name: dn, file_urls: urls });
			});
		};
		$root.on('click', '.pm-add, .pm-drop', () => add());
		$root.on('keydown', '.pm-drop', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); add(); } });
		$root.on('dragover dragenter', (e) => { e.preventDefault(); if (canAct) $root.addClass('is-dragover'); });
		$root.on('dragleave dragend drop', () => $root.removeClass('is-dragover'));
		$root.on('drop', (e) => {
			e.preventDefault();
			const files = [...((e.originalEvent.dataTransfer || {}).files || [])];
			if (files.length) add(files);
		});
		$root.on('click', '.pm-star', function () {
			if (blocked()) return;
			call('set_primary_image', { doctype: dt, name: dn, row_name: $(this).data('row') });
		});
		$root.on('click', '.pm-caption', function () {
			if (blocked()) return;
			const rowName = $(this).data('row');
			const row = rows.find((r) => r.name === rowName) || {};
			frappe.prompt(
				{ fieldname: 'caption', fieldtype: 'Data', label: __('Caption (also the alt text)'), default: row.caption || '' },
				(v) => call('set_image_caption', { doctype: dt, name: dn, row_name: rowName, caption: v.caption || '' }),
				__('Photo caption'), __('Save'));
		});
		$root.on('click', '.pm-remove', function () {
			if (blocked()) return;
			const rowName = $(this).data('row');
			frappe.confirm(__('Remove this photo? The file is deleted if nothing else uses it.'),
				() => call('remove_gallery_image', { doctype: dt, name: dn, row_name: rowName }));
		});
		$root.on('click', '.pm-batch', () => {
			frappe.route_options = { item_code: dn };
			frappe.set_route('serial-photos');
		});

		const $target = frm.layout.wrapper.find('.form-layout, .form-tabs-list').first();
		if ($target.length) { $target.before($root); } else { frm.layout.wrapper.prepend($root); }

		const grid = $root.find('.pm-grid').get(0);
		if (grid && canAct && window.Sortable && rows.length > 2) {
			new window.Sortable(grid, {
				draggable: '.pm-card', handle: '.pm-drag', filter: '.is-primary', preventOnFilter: false,
				animation: 150,
				onEnd() {
					if (blocked()) return;
					const order = [...grid.querySelectorAll('.pm-card')].map((c) => c.dataset.row);
					call('reorder_gallery', { doctype: dt, name: dn, row_names: order });
				},
			});
		}

		// The raw child grid stays for row-level inspection but starts collapsed.
		const sec = frm.fields_dict[o.sectionField || 'section_gallery'];
		if (sec && sec.collapse && sec.df && sec.df.collapsible && !frm._photoSectionCollapsed) {
			frm._photoSectionCollapsed = true;
			sec.collapse(true);
		}
	};

	window.gunstore.renderRsrCard = function (rsr, defaults, opts) {
		const o = Object.assign({thumb: 64, name: 13, meta: 12}, opts || {});
		if (!rsr) return '';
		const thumb = rsr.image_url
			? `<img src="${frappe.utils.escape_html(rsr.image_url)}"
					style="width:${o.thumb}px;height:${o.thumb}px;object-fit:cover;border-radius:4px;border:1px solid var(--border-color)"
					loading="lazy"
					onerror="this.style.visibility='hidden'">`
			: `<div style="width:${o.thumb}px;height:${o.thumb}px;background:var(--bg-color);border-radius:4px"></div>`;
		const fmt = (n) => n ? frappe.format(n, {fieldtype: 'Currency'}) : '—';
		const name = (defaults && defaults.item_name) || rsr.rsr_stock_number || '';
		const mfg = rsr.manufacturer ? ' · ' + frappe.utils.escape_html(rsr.manufacturer) : '';
		const model = rsr.model ? ' ' + frappe.utils.escape_html(rsr.model) : '';
		return `
			<div style="display:flex; gap:10px; align-items:flex-start;
				padding:10px; border:1px solid var(--border-color);
				border-radius:6px; cursor:pointer; background:var(--card-bg);">
				${thumb}
				<div style="flex:1; font-size:${o.meta}px; line-height:1.5">
					<div style="font-weight:600; font-size:${o.name}px">${frappe.utils.escape_html(name)}</div>
					<div style="color:var(--text-muted)">
						${frappe.utils.escape_html(rsr.rsr_stock_number || '')}${mfg}${model}
					</div>
					<div style="color:var(--text-muted); margin-top:2px">
						${__('Dealer')} <b>${fmt(rsr.dealer_price)}</b>
						${rsr.map_price ? ' · ' + __('MAP') + ' <b>' + fmt(rsr.map_price) + '</b>' : ''}
						${rsr.msrp ? ' · ' + __('MSRP') + ' ' + fmt(rsr.msrp) : ''}
					</div>
					${rsr.is_firearm ? `<div style="margin-top:4px"><span style="background:var(--blue-100);color:var(--blue-700);padding:1px 6px;border-radius:4px;font-size:11px;font-weight:600">${__('FIREARM')}</span></div>` : ''}
				</div>
			</div>
		`;
	};

	// Apply RSR defaults to a target. `target` is either a Frappe form (has
	// .doc + .set_value + .add_child) or a QuickEntryForm (has .doc +
	// .set_value + .fields_dict). Both shapes share enough that one helper
	// works for both.
	window.gunstore.applyRsrToDoc = function (target, defaults, rsr, opts) {
		const o = Object.assign({overwriteNonEmpty: false}, opts || {});
		const doc = target.doc;

		for (const [k, v] of Object.entries(defaults || {})) {
			if (v === null || v === undefined || v === '') continue;
			if (k === 'upc') continue;
			const inDialog = target.fields_dict && target.fields_dict[k];
			const allowOverwrite = o.overwriteNonEmpty || !doc[k];
			if (inDialog) {
				if (allowOverwrite) target.set_value(k, v);
			} else if (target.set_value && doc[k] === undefined) {
				doc[k] = v;
			} else if (allowOverwrite) {
				if (target.set_value) target.set_value(k, v);
				else doc[k] = v;
			}
		}

		if (defaults && defaults.upc) {
			doc.barcodes = doc.barcodes || [];
			if (!doc.barcodes.some((b) => b.barcode === defaults.upc)) {
				const row = {
					doctype: 'Item Barcode',
					barcode: defaults.upc,
					barcode_type: 'UPC',
				};
				if (target.add_child) target.add_child('barcodes', row);
				else doc.barcodes.push(row);
			}
			if (target.refresh_field) target.refresh_field('barcodes');
		}

		if (rsr && rsr.image_url) {
			doc.image_gallery = doc.image_gallery || [];
			const variants = window.gunstore.rsrVariantUrls(rsr.image_url);
			for (const [url, idx] of variants) {
				if (doc.image_gallery.some((g) => g.image === url)) continue;
				const row = {
					doctype: 'Item Image Gallery',
					image: url,
					is_primary: idx === 1 && doc.image_gallery.length === 0 ? 1 : 0,
					sort_order: idx,
					caption: `RSR image ${idx}`,
				};
				if (target.add_child) target.add_child('image_gallery', row);
				else doc.image_gallery.push(row);
			}
			if (target.refresh_field) target.refresh_field('image_gallery');
		}
	};

	// JS sibling of ffl_integrations.rsr.promote._rsr_variant_urls — given an
	// RSR primary image URL ending in _1.{ext}, return [(url,1),(url,2),(url,3)]
	// candidates. Variants that 404 are hidden by the gallery renderer's
	// onerror, so we don't HEAD-check.
	window.gunstore.rsrVariantUrls = function (primaryUrl) {
		if (!primaryUrl) return [];
		const m = /_1(\.jpg|\.jpeg|\.png)$/i.exec(primaryUrl);
		if (!m) return [[primaryUrl, 1]];
		const base = primaryUrl.slice(0, m.index);
		const ext = m[1];
		return [1, 2, 3].map((n) => [`${base}_${n}${ext}`, n]);
	};
})();


// --- SupplierQuickEntryForm — adds "Lookup by FFL #" to the supplier dialog
//
// When "+ Create new" is clicked from a Supplier Link field (e.g. the
// supplier picker on Receive Goods / Receive Firearm), Frappe opens its
// QuickEntryForm with a small set of fields. For an FFL dealer, the
// authoritative source is ATF EZ Check — typing the FFL number and
// clicking Lookup populates licensee_name + ffl_license_type +
// expiry_date + address in one shot, and stamps ez_check_status=Verified
// + ez_check_last_run for the audit trail.

(function () {
	'use strict';

	if (!frappe.ui.form.QuickEntryForm) return;

	const Base = frappe.ui.form.SupplierQuickEntryForm || frappe.ui.form.QuickEntryForm;

	frappe.ui.form.SupplierQuickEntryForm = class SupplierQuickEntryForm extends Base {
		render_dialog() {
			super.render_dialog();
			injectFflScanBanner(this);
			// Single-store gun shop in the US — Country defaults to United States
			// for every new Supplier (overridable). Frappe's Supplier Quick
			// Entry exposes the field as `country_address`, NOT `country` —
			// the latter exists only on the saved Supplier doctype itself.
			if (this.fields_dict && this.fields_dict.country_address
					&& !this.doc.country_address) {
				this.set_value('country_address', 'United States');
			}
		}
	};

	function injectFflScanBanner(qe) {
		const $banner = $(`
			<div class="ffl-quick-add-banner" style="
				background: var(--subtle-fg);
				border: 1px solid var(--border-color);
				border-radius: 6px;
				padding: 10px 12px;
				margin: 0 0 14px 0;
			">
				<div style="display:flex; align-items:center; gap:8px; margin-bottom:8px">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
						stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--text-muted)">
						<path d="M12 2 4 6v6c0 5 3.5 9.5 8 10 4.5-.5 8-5 8-10V6l-8-4z"></path>
					</svg>
					<div style="font-weight:600; font-size:12px">${__('FFL Dealer? Lookup by FFL #')}</div>
				</div>
				<div style="display:flex; gap:8px">
					<input type="text"
						class="form-control ffl-qe-scan-input"
						placeholder="${__('1-99-999-99-9X-99999')}"
						style="font-size:13px; padding:6px 10px; flex:1"
						autocomplete="off"
						spellcheck="false">
					<button class="btn btn-primary btn-sm ffl-qe-scan-btn" type="button">
						${__('Lookup')}
					</button>
				</div>
				<div class="ffl-qe-scan-status" style="margin-top:6px; font-size:11px; color:var(--text-muted)"></div>
			</div>
		`);
		qe.$body.prepend($banner);

		const $input = $banner.find('.ffl-qe-scan-input');
		const $btn = $banner.find('.ffl-qe-scan-btn');
		const $status = $banner.find('.ffl-qe-scan-status');

		const doLookup = () => {
			const num = ($input.val() || '').trim();
			if (!num) {
				$status.text(__('Enter an FFL number first.')).css('color', 'var(--orange-600)');
				return;
			}
			$status.text(__('Verifying with ATF EZ Check…')).css('color', 'var(--text-muted)');
			frappe.call({
				method: 'ffl_integrations.atf.ez_check.lookup_and_create',
				args: { ffl_number: num },
				freeze: true,
				freeze_message: __('Verifying with ATF EZ Check…'),
				callback: (r) => {
					const m = r.message || {};
					if (!m.ok) return;
					applyFflToSupplierQE(qe, m);
					$status.html(`<b style="color:var(--green-700)">✓ ${frappe.utils.escape_html(m.licensee_name || m.name)}</b> · ${frappe.utils.escape_html(m.status)} · ${m.expiry_date || ''}`);
					frappe.show_alert({
						message: __('Filled from ATF · {0}', [m.name]),
						indicator: 'green',
					});
				},
			});
		};

		$btn.on('click', doLookup);
		$input.on('keydown', (e) => {
			if (e.key === 'Enter') {
				e.preventDefault();
				doLookup();
			}
		});
		setTimeout(() => $input.focus(), 80);
	}

	function applyFflToSupplierQE(qe, m) {
		const fields = qe.fields_dict || {};
		const setIf = (k, v) => {
			if (v === null || v === undefined || v === '') return;
			if (fields[k]) qe.set_value(k, v);
			else qe.doc[k] = v;
		};
		// Supplier-name default: prefer DBA when present, fall back to licensee
		const display = m.licensee_dba || m.licensee_name || m.name;
		setIf('supplier_name', display);
		setIf('supplier_group', _firstSupplierGroup());
		setIf('supplier_type', 'Company');
		setIf('is_ffl_dealer', 1);
		setIf('ffl_number', m.name);
		// Supplier.ffl_license_type is a Select of bare 2-digit codes
		// ("01"/"07"/...). The ATF cache stores the full label
		// ("07 - Manufacturer of..."); extract just the prefix code.
		setIf('ffl_license_type', _extractLicenseCode(m.license_type));
		setIf('licensee_name', m.licensee_name);
		setIf('licensee_address', m.address);
		setIf('ffl_expiry_date', m.expiry_date);
		// We just verified live (lookup_and_create calls force_live=True).
		// Stamp the audit fields directly so the saved Supplier shows green
		// without forcing the user to click "Verify FFL" again.
		setIf('ez_check_status', m.status === 'Verified' ? 'Verified'
			: m.status === 'Expired' ? 'Expired' : 'Failed');
		setIf('ez_check_last_run', frappe.datetime.now_datetime());
		// Primary Address fields shown in Supplier Quick Entry. Frappe
		// translates these into a linked Address doc on save.
		setIf('address_line1', m.street);
		setIf('address_line2', m.street2);
		setIf('city', m.city);
		setIf('state', m.state);
		setIf('pincode', m.zip);
		// Country is defaulted at render — keep this as a safety net in case
		// the user blanked it before clicking Lookup. Field is named
		// `country_address` in Supplier Quick Entry (not `country`).
		setIf('country_address', 'United States');
	}

	function _extractLicenseCode(s) {
		const match = (s || '').match(/^\s*(\d{2})/);
		return match ? match[1] : '';
	}
	// Re-exported so supplier_form.js can use the same parser.
	window.gunstore = window.gunstore || {};
	window.gunstore.extractLicenseCode = _extractLicenseCode;

	function _firstSupplierGroup() {
		// Best-effort only: no reliable sync source for a default Supplier Group
		// here, so leave it blank. The user picks one before Save, and Frappe
		// blocks the save with a clear error if it's still missing.
		return null;
	}
})();


// --- Integration switches (server-provided) --------------------------------
//
// Which integrations this store actually runs. Served on frappe.boot by
// ffl_integrations.boot.extend_bootinfo and read LAZILY — boot may not be
// populated when this bundle first executes, the same reason payrocMop() is a
// function rather than a constant.
//
// These only shape the UI and spare the counter dead round-trips; the server
// gates are authoritative and unchanged, so a stale page can neither charge a
// switched-off terminal nor satisfy a 4473 the settings do not allow. An ABSENT
// key means a server older than the switches, so every default below is the
// behaviour that shipped before them.
(function fflIntegrationSwitches() {
	function flag(key, dflt) {
		var v = frappe.boot && frappe.boot[key];
		return (v === undefined || v === null) ? dflt : !!Number(v);
	}
	window.ffl = window.ffl || {};
	window.ffl.switches = {
		payrocEnabled: function () { return flag('ffl_payroc_enabled', true); },
		fastboundEnabled: function () { return flag('ffl_fastbound_enabled', true); },
		paper4473Mode: function () { return flag('ffl_paper_4473_mode', false); },
		requireCardReference: function () { return flag('ffl_require_card_reference', false); },
		// Defaults FALSE, unlike the two above. Those describe integrations that
		// were already running when the switches were added, so an absent key has
		// to mean "carry on". GunBroker is off until a store turns it on, and a
		// server that cannot answer must not be what puts listing buttons in front
		// of somebody.
		gunbrokerEnabled: function () { return flag('ffl_gunbroker_enabled', false); },
		// The Payroc tender name, so desk consumers don't re-derive it.
		payrocMop: function () { return (frappe.boot && frappe.boot.payroc_mop) || 'Credit Card'; },
	};

	// A Desk page for a subsystem this store has switched off. Frappe has no
	// native answer here: Page.is_permitted() only reads Has Role, and
	// restrict_to_domain hides a page from the menus without stopping anyone who
	// types its URL. So the page keeps existing and says so, in one place rather
	// than three, and — because it renders INSTEAD of the page's app object
	// being constructed — loads none of the subsystem's data.
	// `where` names the switch the administrator flips: FFL Settings for a core
	// subsystem, Settings → Extensions for an extension's pages.
	window.ffl.featureOffPage = function (page, feature, where) {
		const $wrap = $('<div class="ffl-feature-off">')
			.css({ maxWidth: '560px', margin: '60px auto', textAlign: 'center' });
		$('<div>')
			.css({ fontSize: '15px', fontWeight: 600, marginBottom: '6px' })
			.text(__('{0} is turned off for this store.', [feature]))
			.appendTo($wrap);
		$('<div>')
			.css({ fontSize: '13px', color: 'var(--text-muted)' })
			.text(__('A manager can turn it on under {0}.', [where || __('FFL Settings')]))
			.appendTo($wrap);
		$wrap.appendTo(page.body);
	};

	// --- Desk tender-reference field (shared by the Record Payment dialogs) ---
	// One definition for the desk dialogs that collect a manually-settled
	// tender's reference, so their rules can't drift (earlier, two dialogs
	// shipped with a Zelle-only field while the server demanded a card code —
	// a gate nothing on screen could satisfy): which modes need one (Zelle, ACH
	// and Check/Money Order always; the card tender when the standalone-terminal
	// rule is on), and
	// clear-on-hide — a Zelle number typed then mode-switched must not ride
	// along as a Cash reference or a card "approval code".
	window.ffl.tenderReference = {
		modes: function () {
			var m = ['Zelle', 'ACH', 'Check/Money Order'];
			var sw = window.ffl.switches;
			if (sw && sw.requireCardReference()) m.push(sw.payrocMop());
			return m;
		},
		// The whole tender block for a Record Payment dialog. opts.terminal: the
		// dialog runs the A80 for the card tender, so it also gets "Already charged
		// on Virtual Terminal" (the only way to book a card taken there without
		// charging again) and needs the code only once that is ticked. A dialog
		// that never runs the terminal books every card as typed, so with the
		// terminal on its card always needs the code (osa_consignment's Dealer
		// Orders dialog, via field()). Keyed on the switch, a superset of the
		// server's "terminal armed" rule: at worst stricter, never a gate the
		// screen can't satisfy.
		fields: function (opts) {
			var sw = window.ffl.switches;
			var card = JSON.stringify(sw.payrocMop());
			var terminal = !!(opts && opts.terminal);
			var cardRule = terminal ? '(doc.mode==' + card + ' && doc.charged_outside)'
				: (sw.payrocEnabled() ? 'doc.mode==' + card : 'false');
			var needs = 'eval:' + JSON.stringify(this.modes()) + '.includes(doc.mode) || ' + cardRule;
			var ref = {
				fieldtype: 'Data', fieldname: 'transaction_number',
				label: __('Transaction Number / Approval Code'),
				depends_on: needs, mandatory_depends_on: needs,
				description: __('Zelle / ACH: the bank or app confirmation number. Check/Money Order: the check or money order number. Credit Card: the approval code from the terminal or Virtual Terminal receipt — never the card number.'),
			};
			if (!terminal) return [ref];
			return [{
				fieldtype: 'Check', fieldname: 'charged_outside',
				label: __('Already charged on Virtual Terminal'),
				depends_on: sw.payrocEnabled() ? 'eval:doc.mode==' + card : 'eval:false',
				description: __('Records the card payment without running the terminal. Enter the approval code below.'),
			}, ref];
		},
		// Single-field form, kept for dialogs built before fields() existed.
		field: function () { return this.fields()[0]; },
		wire: function (dialog) {
			var self = this;
			var fd = dialog.fields_dict || {};
			if (!fd.mode) return;
			var sw = window.ffl.switches;
			var terminal = !!fd.charged_outside;
			// Mirrors fields(): does this mode still need its reference once the box is off?
			var needsRef = function (m) {
				return self.modes().indexOf(m) !== -1
					|| (!terminal && m === sw.payrocMop() && sw.payrocEnabled());
			};
			// Write only on a real change: a Check's onchange also fires when the
			// dialog renders, and an unconditional set re-fires it forever.
			var clear = function (f) {
				if (fd[f] && dialog.get_value(f)) dialog.set_value(f, f === 'charged_outside' ? 0 : '');
			};
			var prev = fd.mode.df.onchange;
			fd.mode.df.onchange = function () {
				if (prev) prev.apply(this, arguments);
				// A Virtual Terminal code is a card code — never a Zelle/ACH number;
				// otherwise a reference the new mode still needs is kept.
				var wasVt = terminal && !!dialog.get_value('charged_outside');
				clear('charged_outside');
				if (wasVt || !needsRef(dialog.get_value('mode'))) clear('transaction_number');
			};
			if (terminal) {
				fd.charged_outside.df.onchange = function () {
					if (!dialog.get_value('charged_outside') && !needsRef(dialog.get_value('mode'))) {
						clear('transaction_number');
					}
				};
			}
		},
	};
})();


// --- Payroc terminal payment on POS ---------------------------------------
//
// When the cashier clicks "Complete Order" on /app/point-of-sale and the
// invoice has a payment row using Mode of Payment "Credit Card", we
// route the card portion through the Payroc terminal before letting
// ERPNext submit the invoice. The cashier sees a "Waiting for terminal"
// spinner; on approval we proceed to submit and a green toast shows.
//
// We can't intercept ERPNext's `.submit-order-btn` click handler cleanly
// without forking pos_payment.js — but the click handler does nothing
// more than call `this.events.submit_invoice()`. So we wrap the events
// callback installed by pos_controller.js's `init_payments()` once, and
// every Complete-Order click flows through us.
//
// Save-then-charge ordering: the Payroc API needs an `order_id` (we use
// the POS Invoice name) for receipt + idempotency. The default ERPNext
// flow is single-shot savesubmit; we split it into save → charge →
// (backend stamps payroc_unique_reference) → reload → submit.

(function setupPayrocPosHook() {
	const POLL_TIMEOUT_MS = 120000;   // overall wait for the terminal

	// The Payroc tender name, served from the backend's single definition
	// (ffl_integrations.boot → frappe.boot.payroc_mop). Read lazily — boot may
	// not be populated yet when this bundle first executes.
	function payrocMop() {
		return (frappe.boot && frappe.boot.payroc_mop) || 'Credit Card';
	}

	// Sum of the card tender rows in DECIMAL major units — Payroc wants
	// order.totalAmount as 42.50, NOT integer cents.
	function payrocAmount(doc) {
		return (doc.payments || [])
			.filter((p) => p.mode_of_payment === payrocMop())
			.reduce((s, p) => s + (parseFloat(p.amount) || 0), 0);
	}

	function showWaitingDialog(amount, onCancel) {
		const dollars = amount.toFixed(2);
		const dlg = new frappe.ui.Dialog({
			title: __('Waiting for terminal'),
			fields: [{
				fieldname: 'msg', fieldtype: 'HTML',
				options: `
					<div class="payroc-waiting" style="text-align:center;padding:24px 8px;">
						<div class="payroc-spinner" style="margin:0 auto 18px;width:48px;height:48px;
							border:4px solid var(--gray-300);border-top-color:var(--green-600);border-radius:50%;
							animation: payroc-spin 0.9s linear infinite;"></div>
						<p style="font-size:20px;font-weight:600;margin:0 0 6px;">$${dollars}</p>
						<p style="margin:0;color:var(--text-muted);">${__('Customer should dip / tap card on terminal.')}</p>
					</div>
					<style>@keyframes payroc-spin { to { transform: rotate(360deg); } }</style>
				`,
			}],
			primary_action_label: __('Cancel'),
			primary_action: () => { if (onCancel) onCancel(); },
		});
		dlg.show();
		// Only the explicit Cancel button dismisses — hide the X and block
		// backdrop/keyboard close so a stray click can't orphan the terminal.
		dlg.$wrapper.find('.modal-header .close').hide();
		dlg.$wrapper.attr('data-keyboard', 'false');
		dlg.$wrapper.off('click.bs.modal');
		return dlg;
	}

	function hideDialog(dlg) {
		if (dlg.$wrapper && dlg.$wrapper.is(':visible')) dlg.hide();
	}

	// Best-effort cancel of an in-flight instruction. The server reconciles a
	// raced approval (stamps it, voidable) instead of stranding the capture.
	async function cancelInstruction(frm, instructionId) {
		try {
			await frappe.call({
				method: 'ffl_integrations.payroc.api.cancel_pos_charge',
				args: { invoice_name: frm.doc.name, instruction_id: instructionId },
			});
		} catch (e) { /* best-effort */ }
	}

	// Multi-card charge math lives in the pure, Node-tested module
	// payroc_charges.js (window.ffl.payrocCharges) — single source shared with
	// the desk form's void picker.
	function payrocMath() {
		return (window.ffl && window.ffl.payrocCharges) || null;
	}

	// Per-card amount dialog: shows what each card already captured and asks
	// how much to put on the NEXT card. Default = everything remaining, so the
	// single-card majority path is dialog → Charge; typing a smaller amount is
	// what splits the sale across cards.
	function promptChargeAmount(frm, remaining) {
		return new Promise((resolve) => {
			let settled = false;
			const pc = payrocMath();
			const done = (pc ? pc.liveChargeRows(frm.doc) : []).map((r) =>
				`<div style="display:flex;justify-content:space-between;gap:12px;">
					<span>${frappe.utils.escape_html(r.card_summary || r.unique_reference || __('Card'))}</span>
					<b>${format_currency(flt(r.amount))}</b>
				</div>`).join('');
			const dlg = new frappe.ui.Dialog({
				title: __('Card payment'),
				fields: [
					{ fieldname: 'prior', fieldtype: 'HTML', options: done
						? `<div style="margin-bottom:8px;color:var(--text-muted);">${done}</div>` : '' },
					{ fieldname: 'amount', fieldtype: 'Currency', label: __('Charge this card'),
					  default: remaining, reqd: 1,
					  description: __('Remaining card balance {0}. Enter less to split across more cards.',
						[format_currency(remaining)]) },
				],
				primary_action_label: __('Charge'),
				primary_action(values) {
					const amt = flt(values.amount);
					if (!pc || !pc.isValidSlice(amt, remaining)) {
						frappe.msgprint({ title: __('Invalid amount'), indicator: 'red',
							message: __('Amount must be between $0.01 and the remaining {0}.',
								[format_currency(remaining)]) });
						return;
					}
					settled = true;
					dlg.hide();
					resolve({ amount: amt });
				},
			});
			dlg.$wrapper.on('hidden.bs.modal', () => { if (!settled) resolve(null); });
			dlg.show();
		});
	}

	// Payment screen on an order with card money already captured (an earlier
	// visit's deposit, an earlier card of a split): the Credit Card tile takes
	// the amount to charge NOW; the tender row keeps the card total, so ERPNext's
	// totals and the charge loop below (remaining = tender − captured) ask for
	// exactly what was typed. Runs after every render_payment_mode_dom
	// (fflRenderPaymentModes). Payroc off → the tile keeps its plain meaning.
	function bindCardChargeNow(payment) {
		const pc = payrocMath();
		if (!pc || !pc.chargeNowControl || !payrocLive()) return;
		// Payment section hidden: ERPNext's render bailed out, so payment[key] is
		// the last render's control — never wrap it again or append to stale DOM.
		if (!payment.$payment_modes || !payment.$payment_modes.is(':visible')) return;
		const frm = payment.events.get_frm();
		const row = ((frm && frm.doc.payments) || []).find((p) => p.mode_of_payment === payrocMop());
		const mode = payment.sanitize_mode_of_payment(payrocMop());
		const key = `${mode}_control`;
		const real = payment[key];
		if (!row || !real || real.__fflReal) return;
		const charged = () => flt(pc.sumLiveCharges(frm.doc), 2);
		const tenderTotal = () => flt(frappe.model.get_value(row.doctype, row.name, 'amount'));
		const now = pc.remainingCardBalance(tenderTotal(), frm.doc);
		// No card money yet → plain tile. Captured MORE than the card tender (cart
		// edited down after a deposit) → plain tile too: the charge loop's
		// mismatch message explains that; a negative "amount now" would not.
		if (charged() <= pc.TOLERANCE || now < -pc.TOLERANCE) return;
		// Looked up live: every tender change re-renders the tiles (ERPNext's
		// paid_amount handler), and the keypad may still hold this control.
		const showNow = (v) => payment.$payment_modes.find(`.${mode}-amount`)
			.html(v ? format_currency(v, frm.doc.currency) : '');
		const tile = pc.chargeNowControl({
			real, charged, tenderTotal, show: showNow,
			writeTotal: (total) => frappe.model.set_value(row.doctype, row.name, 'amount', total)
				.then(() => payment.update_totals_section()),
		});
		real.__fflArm = tile.arm;
		payment[key] = tile.facade;
		showNow(now);
		payment.$payment_modes.find(`.mode-of-payment[data-mode="${mode}"]`).append(
			`<div class="text-muted small" style="pointer-events:none;margin-top:4px;">${
				__('Already charged {0} — enter only the amount to charge now.', [format_currency(charged(), frm.doc.currency)])}</div>`);
		if (payment.__fflCardChargeNowClick) return;
		payment.__fflCardChargeNowClick = true;
		// Runs after ERPNext's own tile-click handler, which selected the facade:
		// arm the real control, hand it to the keypad, and rerun ERPNext's
		// fill-the-remainder against it (ERPNext's own run saw the facade).
		payment.$payment_modes.on('click', '.mode-of-payment', () => {
			const sel = payment.selected_mode;
			if (!sel || !sel.__fflReal) return;
			sel.__fflReal.__fflArm();
			payment.selected_mode = sel.__fflReal;
			payment.auto_set_remaining_amount();
		});
	}

	window.ffl = window.ffl || {};
	window.ffl.bindCardChargeNow = bindCardChargeNow;

	// Which integrations are live. Lazy, like payrocMop() — boot isn't guaranteed
	// populated when this bundle first runs.
	function switches() {
		return (window.ffl && window.ffl.switches) || null;
	}

	function payrocLive() {
		const s = switches();
		return s ? s.payrocEnabled() : true;   // no switches module → pre-switch behaviour
	}

	// A counter pickup's 4473 ticket (pos_tender_guard.pickupTicketState). Kernel
	// missing (asset problem) → the same reading here, so a ticket is still never
	// charged and a $0 one still completes. Keep the two in step.
	function pickupTicket(doc) {
		const k = window.ffl && window.ffl.pickupTicketState;
		if (k) return k(doc);
		doc = doc || {};
		const order = (doc.ffl_counter_order || '').trim();
		if (!order || cint(doc.is_return)) return null;
		const carriesMoney = [doc.grand_total, doc.rounded_total, doc.paid_amount, doc.change_amount].some((v) => flt(v))
			|| (doc.items || []).some((r) => flt((r || {}).rate))
			|| (doc.payments || []).some((p) => flt((p || {}).amount));
		return { order, carriesMoney };
	}

	// The save did not bring the ticket to $0 (something re-priced this copy
	// again): never let it take money, and say so — a silent stop reads as a
	// dead button.
	function ticketStillPriced(order) {
		frappe.msgprint({
			title: __('Pickup ticket still shows a price'), indicator: 'red',
			message: __('This pickup ticket for {0} is still showing a price. Do not take any payment — the money is on that order. Reload the page and open the ticket again.', [order]),
		});
	}

	// When the register loads a pickup ticket into its cart (Recent Orders → Edit,
	// Open counter order, any load_invoice) and this copy carries money — the
	// register re-applies the price list to a draft — save it so the server pins
	// $0 (counter_pickup.keep_ticket_free), repaint, and tell the cashier to take
	// no payment. Before Complete Order, so cash is never collected against a
	// list price. A $0 ticket is left alone; one save per load (the repaint's
	// own load_invoice is a no-op while this ticket is being pinned).
	const pinningTickets = new Set();
	async function pinLoadedTicket(cart) {
		const frm = cart && cart.events && typeof cart.events.get_frm === 'function' && cart.events.get_frm();
		const doc = frm && frm.doc;
		if (!doc || doc.__islocal || cint(doc.docstatus) !== 0 || pinningTickets.has(doc.name)) return;
		const ticket = pickupTicket(doc);
		if (!ticket || !ticket.carriesMoney) return;
		pinningTickets.add(doc.name);
		try {
			frappe.show_alert({ message: __('Pickup ticket — the money is on order {0}. Take no payment.',
				[ticket.order]), indicator: 'orange' }, 10);
			frm.dirty();
			try { await frm.save(); } catch (e) { return; }   // the server said why
			try { cart.load_invoice(); } catch (e) { /* cosmetic */ }
			const after = pickupTicket(frm.doc);
			if (after && after.carriesMoney) ticketStillPriced(ticket.order);
		} finally {
			pinningTickets.delete(doc.name);
		}
	}

	function patchCartLoad(ItemCart) {
		const proto = ItemCart && ItemCart.prototype;
		if (!proto || proto.__fflTicketLoadPatched || typeof proto.load_invoice !== 'function') return;
		const original = proto.load_invoice;
		proto.load_invoice = function fflLoadInvoice(...args) {
			const out = original.apply(this, args);
			pinLoadedTicket(this).catch((e) => console.error('ffl: pickup ticket pin failed', e));
			return out;
		};
		proto.__fflTicketLoadPatched = true;
	}

	async function chargeViaPayroc(frm) {
		// A counter pickup ticket carries no money — it is on the order the ticket
		// hands over. Never the card dialog, never the terminal, whatever this copy
		// of the doc says (submit_invoice has already saved it back to $0).
		if (pickupTicket(frm.doc)) return { skip: true };

		// Payroc switched off: cards are swiped on a standalone terminal and the
		// Credit Card row is ordinary tender, so Complete Order finishes in one
		// click. Decided HERE rather than after the save + round-trip the server
		// would only answer {"skip": true} to — that round-trip is the difference
		// between a one-click sale and a visible pause at the counter.
		if (!payrocLive()) return { skip: true };

		const target = payrocAmount(frm.doc);
		// No positive card tender (e.g. a return) → don't intercept here.
		if (target <= 0) return { skip: true };

		// Legacy single-stamp era: already charged on a prior click? Don't
		// double-charge. A stamped but PARTIAL capture is recorded for voiding,
		// NOT fully paid — never let it submit the order as if it were.
		const hasRows = (frm.doc.payroc_charges || []).length > 0;
		if (!hasRows && frm.doc.payroc_unique_reference) {
			if (frm.doc.payroc_status === 'PARTIAL') {
				frappe.msgprint({
					title: __('Partial approval on file'), indicator: 'orange',
					message: __('A prior attempt captured only part of the amount. Get a manager to void it (back office), then re-tender.'),
				});
				return { skip: false, ok: false };
			}
			// Held-order safety: the card was charged on an earlier visit (e.g.
			// while the 4473 was pending). If the cart was edited since, the
			// captured amount no longer matches the card tender — block instead
			// of submitting a mispriced order.
			const charged = flt(frm.doc.payroc_charged_amount);
			if (charged > 0 && flt(target, 2) !== flt(charged, 2)) {
				frappe.msgprint({
					title: __('Charged amount mismatch'), indicator: 'red',
					message: __('The card was charged {0} but the Credit Card tender now totals {1} — the order changed after payment. Get a manager to void the charge (back office), then re-tender.',
						[format_currency(charged), format_currency(target)]),
				});
				return { skip: false, ok: false };
			}
			return { skip: false, ok: true };
		}

		// Save-as-draft to mint a name; the backend needs an existing invoice.
		if (frm.doc.__islocal || frm.is_dirty()) {
			try {
				await frm.save();
			} catch (e) {
				frappe.msgprint({
					title: __('Could not save invoice'), indicator: 'red',
					message: (e && e.message) || __('Frappe blocked save — fix validation errors and retry.'),
				});
				return { skip: false, ok: false };
			}
		}

		// Multi-card loop: one dialog + one terminal instruction per card until
		// the captured rows cover the card tender. Each approved slice reloads
		// the doc, so the loop re-evaluates rows AND the tender target fresh
		// every pass (a reload may recompute payments).
		const pc = payrocMath();
		if (!pc) {
			// Pure module missing (asset build problem) — fail safe: no charge
			// is better than an unvalidated one.
			frappe.msgprint({ title: __('POS asset error'), indicator: 'red',
				message: __('payroc_charges.js is not loaded — reload the page; if it persists, rebuild assets.') });
			return { skip: false, ok: false };
		}
		while (true) {
			const targetNow = payrocAmount(frm.doc);
			const charged = flt(pc.sumLiveCharges(frm.doc), 2);
			const remaining = flt(targetNow - charged, 2);
			if (charged > 0 && remaining < -pc.TOLERANCE) {
				// Cart was edited DOWN after money was captured — same rule as
				// the legacy mismatch guard: a manager voids, then re-tender.
				frappe.msgprint({
					title: __('Charged amount mismatch'), indicator: 'red',
					message: __('Cards captured {0} but the Credit Card tender now totals {1} — the order changed after payment. Get a manager to void a charge (back office), then re-tender.',
						[format_currency(charged), format_currency(targetNow)]),
				});
				return { skip: false, ok: false };
			}
			if (remaining <= pc.TOLERANCE) return { skip: false, ok: true };
			const req = await promptChargeAmount(frm, remaining);
			if (!req) return { skip: false, ok: false };   // cashier backed out
			const res = await runSingleCharge(frm, req.amount);
			if (res.skip || !res.ok) return res;
		}
	}

	// One card: create the instruction for `amount` and poll it to a terminal
	// state — the single-charge era flow, parameterized by amount.
	async function runSingleCharge(frm, amount) {

		// 1. Create the device payment instruction (returns fast with an id).
		let instructionId = '';
		try {
			const c = await frappe.call({
				method: 'ffl_integrations.payroc.api.charge_pos_invoice',
				args: {
					invoice_name: frm.doc.name, amount: amount,
					description: `${frm.doc.name} — ${frm.doc.customer_name || frm.doc.customer || ''}`,
				},
			});
			const created = (c && c.message) || {};
			// Integration not configured (go-live gate) — plain tender, no gateway.
			if (created.skip) return { skip: true };
			instructionId = created.instruction_id || '';
		} catch (err) {
			if (!(err && err._server_messages)) {
				frappe.msgprint({
					title: __('Terminal error'), indicator: 'red',
					message: (err && err.message) || __('Could not start the terminal charge.'),
				});
			}
			return { skip: false, ok: false };
		}
		if (!instructionId) return { skip: false, ok: false };

		// 2. Poll loop (overall timeout). Cancel sets the flag + cancels the instruction.
		let cancelled = false;
		const dlg = showWaitingDialog(amount, async () => {
			cancelled = true;
			hideDialog(dlg);   // free the UI now, don't wait out the in-flight poll
			await cancelInstruction(frm, instructionId);
		});
		const deadline = Date.now() + POLL_TIMEOUT_MS;
		try {
			while (Date.now() < deadline && !cancelled) {
				let p;
				try {
					p = await frappe.call({
						method: 'ffl_integrations.payroc.api.poll_pos_charge',
						args: { invoice_name: frm.doc.name, instruction_id: instructionId, expected_amount: amount },
					});
				} catch (err) {
					// Poll failed (network / gateway error after GET retries). The
					// charge may still be live on the terminal — cancel it (the server
					// reconciles a raced approval) rather than leaving it stranded
					// and letting the rejection escape submit_invoice unhandled.
					if (!cancelled) {
						await cancelInstruction(frm, instructionId);
						frappe.msgprint({
							title: __('Terminal error'), indicator: 'red',
							message: __('Lost contact while waiting for the terminal. The charge was cancelled — please retry.'),
						});
					}
					return { skip: false, ok: false };
				}
				const m = (p && p.message) || {};
				// Cancel may have fired during the await — respect it. If the charge
				// raced to approval, cancel_pos_charge has reconciled+stamped it, so
				// it is recorded (voidable), not stranded; we just don't auto-submit.
				if (cancelled) { hideDialog(dlg); return { skip: false, ok: false }; }
				if (m.status === 'IN_PROGRESS') {
					// Pace the loop — the terminal takes seconds to prompt/read; a
					// zero-delay re-poll hammers the gateway (each poll is a full
					// auth + GET round-trip) and risks rate-limiting mid-auth.
					await new Promise((r) => setTimeout(r, 1500));
					continue;
				}
				dlg.hide();
				if (m.ok) {
					// Backend stamped payroc_unique_reference — reload so
					// savesubmit doesn't write the stale doc back.
					await frm.reload_doc();
					frappe.show_alert({
						message: __('Approved · auth {0}', [m.approval_code || m.unique_reference || 'OK']),
						indicator: 'green',
					});
					return { skip: false, ok: true };
				}
				if (m.partial) {
					// Backend stamped the partial capture (ref + PARTIAL status) —
					// reload so a re-click hits the partial guard above instead of
					// reading a stale, unstamped local doc.
					await frm.reload_doc();
				}
				const why = m.partial
					? __('Partial approval ${0} of ${1} — get a manager to reverse and re-tender.',
						[(m.authorized_amount || 0).toFixed(2), amount.toFixed(2)])
					: (m.error || __('Card declined. Try another card or payment method.'));
				frappe.msgprint({ title: __('Payment not completed'), indicator: 'red', message: why });
				return { skip: false, ok: false };
			}
		} finally {
			hideDialog(dlg);
		}
		// Timed out without a response (and not cancelled) → cancel to avoid a hang.
		if (!cancelled) {
			await cancelInstruction(frm, instructionId);
			frappe.msgprint({
				title: __('Terminal timed out'), indicator: 'orange',
				message: __('No response from the terminal. The charge was cancelled — please retry.'),
			});
		}
		return { skip: false, ok: false };
	}

	// Terminal charge for a document that is NOT a POS Invoice — a counter Sales
	// Invoice (Create Order right after the order books; Pending Order → Record
	// Payment for a balance). Same waiting dialog, cancel path and poll pacing as
	// the POS flow; the endpoints and their args are the caller's (poll/cancel get
	// instruction_id merged in). Resolves {skip:true} when the integration is
	// off, {ok:true, ...server result} on approval (the server has already booked
	// the Payment Entry), {ok:false} on decline / cancel / timeout — the caller
	// decides what an on-account order does next. Never throws.
	// lazy: runSingleCharge above keeps its own copy of this loop — its partial-
	// approval and frm.reload_doc semantics are POS-specific; fold it onto this
	// helper once the multi-card flow is expressed in terms of it.
	window.ffl = window.ffl || {};
	window.ffl.terminalCharge = async function ({ amount, start, poll, cancel }) {
		const call = (spec, extra) => frappe.call({
			method: spec.method, args: Object.assign({}, spec.args, extra || {}) });
		let instructionId = '';
		let created = {};
		try {
			const c = await call(start);
			created = (c && c.message) || {};
			if (created.skip) return { skip: true };
			// An earlier approval on this document was still unrecorded — the
			// server settled it instead of charging again.
			if (created.already) return Object.assign({ skip: false }, created);
			instructionId = created.instruction_id || '';
		} catch (err) {
			if (!(err && err._server_messages)) {
				frappe.msgprint({ title: __('Terminal error'), indicator: 'red',
					message: (err && err.message) || __('Could not start the terminal charge.') });
			}
			return { skip: false, ok: false };
		}
		if (!instructionId) return { skip: false, ok: false };
		const cancelIt = async () => {
			try { await call(cancel, { instruction_id: instructionId }); } catch (e) { /* best-effort */ }
		};
		let cancelled = false;
		const dlg = showWaitingDialog(flt(created.amount || amount), async () => {
			cancelled = true;
			hideDialog(dlg);
			await cancelIt();
		});
		const deadline = Date.now() + POLL_TIMEOUT_MS;
		try {
			while (Date.now() < deadline && !cancelled) {
				let p;
				try {
					p = await call(poll, { instruction_id: instructionId });
				} catch (err) {
					if (!cancelled) {
						await cancelIt();
						frappe.msgprint({ title: __('Terminal error'), indicator: 'red',
							message: __('Lost contact while waiting for the terminal. The charge was cancelled — please retry.') });
					}
					return { skip: false, ok: false };
				}
				const m = (p && p.message) || {};
				if (cancelled) return { skip: false, ok: false };
				if (m.status === 'IN_PROGRESS') {
					await new Promise((r) => setTimeout(r, 1500));
					continue;
				}
				hideDialog(dlg);
				if (m.ok) {
					frappe.show_alert({ indicator: 'green',
						message: __('Approved · auth {0}', [m.approval_code || m.unique_reference || 'OK']) });
					return Object.assign({ skip: false }, m);
				}
				frappe.msgprint({ title: __('Payment not completed'), indicator: 'red',
					message: m.error || __('Card declined. Try another card or payment method.') });
				return Object.assign({ skip: false, ok: false }, m);
			}
		} finally {
			hideDialog(dlg);
		}
		if (!cancelled) {
			await cancelIt();
			frappe.msgprint({ title: __('Terminal timed out'), indicator: 'orange',
				message: __('No response from the terminal. The charge was cancelled — please retry.') });
		}
		return { skip: false, ok: false };
	};

	// COUNTER PICKUP TICKET: the ticket is $0 — the money is on the counter order
	// (Sales Invoice) it hands the gun over for. Complete Order collects whatever
	// that order still owes right here (terminal / cash / Zelle / ACH / check), booking it
	// on the order exactly as Pending Order → Record Payment would, then goes on
	// to hand over. The server refuses the submit while the order owes
	// (counter_pickup.require_settled_counter_order), so this is the counter's
	// path, not the gate.
	function promptCounterBalance(si, owed) {
		return new Promise((resolve) => {
			let settled = false;
			const modes = ['Cash', payrocMop(), 'Zelle', 'ACH', 'Check/Money Order'];
			const refFields = (window.ffl && window.ffl.tenderReference)
				? window.ffl.tenderReference.fields({ terminal: true })
				: [{ fieldtype: 'Data', fieldname: 'transaction_number', label: __('Zelle Transaction Number'),
					depends_on: "eval:doc.mode=='Zelle'", mandatory_depends_on: "eval:doc.mode=='Zelle'" }];
			const dlg = new frappe.ui.Dialog({
				title: __('Balance due on {0}', [si]),
				fields: [
					{ fieldname: 'note', fieldtype: 'HTML', options:
						`<p style="margin:0 0 8px">${__('This order still owes <b>{0}</b>. Collect it now — the gun is handed over against a settled order.', [format_currency(owed)])}</p>` },
					{ fieldtype: 'Select', fieldname: 'mode', label: __('Payment Method'), reqd: 1,
					  options: modes.join('\n'), default: payrocLive() ? payrocMop() : 'Cash' },
					...refFields,
					{ fieldtype: 'Currency', fieldname: 'amount', label: __('Amount'), default: owed, reqd: 1,
					  description: __('Less than the balance leaves the rest to collect before hand-over.') },
				],
				primary_action_label: __('Collect'),
				primary_action(values) {
					const amt = flt(values.amount);
					if (amt <= 0 || amt > owed + 0.005) {
						frappe.msgprint({ title: __('Invalid amount'), indicator: 'red',
							message: __('Amount must be between $0.01 and the balance {0}.', [format_currency(owed)]) });
						return;
					}
					settled = true;
					dlg.hide();
					resolve({ mode: values.mode, amount: amt, transaction_number: values.transaction_number || '',
						charged_outside: !!values.charged_outside });
				},
			});
			if (window.ffl && window.ffl.tenderReference) window.ffl.tenderReference.wire(dlg);
			dlg.$wrapper.on('hidden.bs.modal', () => { if (!settled) resolve(null); });
			dlg.show();
		});
	}

	async function collectCounterBalance(frm) {
		const si = ((frm.doc || {}).ffl_counter_order || '').trim();
		if (!si) return { ok: true };
		while (true) {
			let owed = 0;
			try {
				const r = await frappe.db.get_value('Sales Invoice', si, 'outstanding_amount');
				owed = flt(((r && r.message) || {}).outstanding_amount);
			} catch (e) {
				return { ok: true };   // can't read it here — the server gate decides
			}
			if (owed <= 0.005) return { ok: true };
			const choice = await promptCounterBalance(si, owed);
			if (!choice) return { ok: false };
			if (choice.mode === payrocMop() && payrocLive() && !choice.charged_outside && window.ffl.terminalCharge) {
				const res = await window.ffl.terminalCharge({
					amount: choice.amount,
					start: { method: 'ffl_integrations.payroc.api.charge_sales_invoice', args: { sales_invoice: si, amount: choice.amount } },
					poll: { method: 'ffl_integrations.payroc.api.poll_sales_invoice_charge', args: { sales_invoice: si } },
					cancel: { method: 'ffl_integrations.payroc.api.cancel_sales_invoice_charge', args: { sales_invoice: si } },
				});
				if (res && res.skip) {
					frappe.msgprint({ title: __('Terminal not configured'), indicator: 'orange',
						message: __('The Payroc device is not set up on this store. Take the card on the Virtual Terminal or your standalone terminal first, then tick "Already charged on Virtual Terminal" and enter its approval code.') });
				}
				continue;   // approved → the loop re-reads the balance; declined → ask again
			}
			try {
				await frappe.call({
					method: 'ffl_core.api.manual_order.record_payment',
					args: { sales_invoice: si, mode_of_payment: choice.mode, amount: choice.amount,
						transaction_number: choice.transaction_number || '' },
				});
			} catch (e) { /* the server message already showed; ask again */ }
		}
	}

	function payrocRefundAmount(doc) {
		// Returns carry NEGATIVE Credit Card rows — refund their absolute total.
		return (doc.payments || [])
			.filter((p) => p.mode_of_payment === payrocMop())
			.reduce((s, p) => s + Math.abs(parseFloat(p.amount) || 0), 0);
	}

	async function refundViaPayroc(frm) {
		// NO client-side short-circuit here, unlike the charge path. Two reasons,
		// both about money going OUT:
		//   1. refund_pos_invoice runs _check_refund_perm() BEFORE its own skip, so
		//      skipping the call skips the MANAGER GATE — any counter role could
		//      then ring a negative card return that reverses revenue and sales tax.
		//   2. only the server can see whether the ORIGINAL was charged through the
		//      gateway. A sale taken on the terminal and returned after the switch
		//      was flipped must not quietly book a refund that never happened.
		// The round-trip costs nothing on a return, and it is the thing enforcing
		// both controls.
		const amount = payrocRefundAmount(frm.doc);
		if (amount <= 0) return { skip: true };                       // no card refund portion
		const priorRef = frm.doc.payroc_refund_reference;
		if (priorRef) {
			const pc = payrocMath();
			if (!pc) {
				// Pure module missing: we cannot tell a lock from a completed
				// refund — fail CLOSED with the canonical asset-error copy
				// (same as the charge path above), never a sentinel-specific
				// claim we can't back.
				frappe.msgprint({ title: __('POS asset error'), indicator: 'red',
					message: __('payroc_charges.js is not loaded — reload the page; if it persists, rebuild assets.') });
				return { skip: false, ok: false };
			}
			// A LOCK sentinel, not a completed refund — treating it as
			// "already refunded" would let the return complete while part
			// of the card refund never happened. Per-sentinel instructions
			// Sending a PARTIAL manager to Unlock Card
			// Refund only feeds them the "cannot be resolved" refusal.
			if (pc.isRefundLock(priorRef)) {
				frappe.msgprint({
					title: __('Card refund is locked'), indicator: 'red',
					message: pc.isUnlockableLock(priorRef)
						? __('This return\'s automatic refund is locked ({0}) after an attempt with an UNKNOWN outcome — it has NOT completed. A manager must verify in the Payroc portal that no refund landed, then use Unlock Card Refund on the return and retry.', [priorRef])
						: __('This return\'s automatic refund is locked ({0}): some slices already refunded, the rest did not. The residue is settled manually at the back office (Payroc portal); after it is reconciled and documented on the return, a manager completes the return from Desk.', [priorRef]),
				});
				return { skip: false, ok: false };
			}
			return { skip: false, ok: true };  // already refunded
		}

		if (frm.doc.__islocal || frm.is_dirty()) {
			try {
				await frm.save();
			} catch (e) {
				frappe.msgprint({
					title: __('Could not save return'), indicator: 'red',
					message: (e && e.message) || __('Fix validation errors and retry.'),
				});
				return { skip: false, ok: false };
			}
		}

		const dlg = new frappe.ui.Dialog({
			title: __('Refunding to card'),
			fields: [{
				fieldname: 'm', fieldtype: 'HTML',
				options: `<p style="text-align:center;padding:18px;">${__('Refunding ${0} to the original card…', [amount.toFixed(2)])}</p>`,
			}],
		});
		dlg.show();
		dlg.$wrapper.find('.modal-header .close').hide();
		try {
			const r = await frappe.call({
				method: 'ffl_integrations.payroc.api.refund_pos_invoice',
				args: { return_invoice_name: frm.doc.name },
			});
			dlg.hide();
			const m = (r && r.message) || {};
			// Not configured / original not Payroc-charged — the return submits
			// normally; the card refund is handled manually on the terminal.
			if (m.skip) return { skip: true };
			if (m.ok) {
				await frm.reload_doc();
				frappe.show_alert({ message: __('Refunded ${0}', [amount.toFixed(2)]), indicator: 'green' });
				return { skip: false, ok: true };
			}
			return { skip: false, ok: false };
		} catch (err) {
			dlg.hide();
			if (!(err && err._server_messages)) {
				frappe.msgprint({
					title: __('Refund error'), indicator: 'red',
					message: (err && err.message) || __('Could not process the refund.'),
				});
			}
			return { skip: false, ok: false };
		}
	}

	// Per-gun priced firearm lines that aren't sellable yet: serial unpicked
	// (the serial sets the price) or rate still <= 0 (un-priced serial).
	async function unpricedPerGunLines(frm) {
		const rows = (frm.doc.items || []).filter((r) => r.item_code);
		if (!rows.length || !(window.ffl && ffl.posPricingSummaries)) return [];
		const summ = await ffl.posPricingSummaries([...new Set(rows.map((r) => r.item_code))]);
		return rows.filter((r) => {
			const s = summ[r.item_code];
			return s && s.is_firearm && !s.uniform && s.n > 0
				&& (!r.serial_no || flt(r.rate) <= 0);
		});
	}

	function showPickTheGun(bad) {
		frappe.msgprint({
			title: __('Pick the gun'), indicator: 'orange',
			message: __('These per-gun priced firearms need a serial picked (the serial sets the price): {0}',
				[bad.map((r) => '<b>' + frappe.utils.escape_html(r.item_code) + '</b>'
					+ (r.serial_no ? '' : ' ' + __('(no serial)'))).join(', ')]),
		});
	}

	// Manually-settled tender reference capture. A tender with no gateway behind it
	// leaves no machine record of the money moving, so the cashier records the
	// confirmation the OTHER system gave them and we store it on the payment row's
	// reference_no. Three tenders need this, for the same reason:
	//   - Zelle: a bank transfer the customer made in an app;
	//   - Check/Money Order: paper deposited at the bank later;
	//   - Credit Card with Payroc switched off: a swipe on a standalone terminal.
	// Backend gates enforce both at submit (ffl_core.compliance.payment_mode and
	// ffl_integrations.payroc.api respectively), so these prompts are the friendly
	// POS path, never the only line of defence.
	// Mirrors ffl_core.compliance.payment_mode.require_tender_reference, INCLUDING
	// its includeRefunds parameter. The two implementations must agree row-for-row:
	// the server decides whether the sale may submit, this decides whether anyone
	// was ever asked — and a row the server demands but the client never prompts
	// for is a counter with no way forward.
	function unreferencedRows(doc, mode, includeRefunds) {
		return (doc.payments || []).filter((p) => {
			if (p.mode_of_payment !== mode) return false;
			const amt = parseFloat(p.amount) || 0;
			if (amt === 0) return false;
			if (amt < 0 && !includeRefunds) return false;
			return !(p.reference_no || '').trim();
		});
	}

	async function ensureTenderReference(frm, opts) {
		const rows = unreferencedRows(frm.doc, opts.mode, opts.includeRefunds);
		if (!rows.length) return { ok: true };
		// ONE PROMPT PER ROW, not one value written across all of them. A sale split
		// across two cards has two approval codes, and copying the first onto both
		// would make the reconciliation key a lie in the one case it matters most.
		// Single-row is still a single dialog, which is the overwhelming majority.
		for (const row of rows) {
			const ok = await promptTenderReference(frm, row, opts, rows.length > 1);
			if (!ok) return { ok: false };   // dismissing aborts checkout
		}
		return { ok: true };
	}

	function promptTenderReference(frm, row, { title, label, description }, many) {
		return new Promise((resolve) => {
			let saved = false;
			const amount = format_currency(Math.abs(parseFloat(row.amount) || 0));
			const dlg = new frappe.ui.Dialog({
				title: many ? `${title} — ${amount}` : title,
				fields: [{
					fieldtype: 'Data', fieldname: 'txn', reqd: 1,
					label: label,
					description: many
						? __('{0} (this line: {1})', [description, amount])
						: description,
				}],
				primary_action_label: __('Confirm'),
				primary_action: (v) => {
					const txn = ((v && v.txn) || '').trim();
					if (!txn) return;   // reqd guards this; double-check anyway
					// Set by object reference (POS payment rows may be unsaved/local,
					// so don't rely on row names) and mark dirty so savesubmit keeps it.
					row.reference_no = txn;
					frm.dirty();
					saved = true;
					dlg.hide();
					resolve(true);
				},
			});
			// Dismissing without confirming aborts checkout — never submit a sale
			// whose manually-settled tender has no reference recorded.
			dlg.onhide = () => { if (!saved) resolve(false); };
			dlg.show();
		});
	}

	function ensureZelleReference(frm) {
		return ensureTenderReference(frm, {
			mode: 'Zelle',
			title: __('Zelle Transaction Number'),
			label: __('Transaction Number'),
			description: __('Enter the Zelle confirmation / transaction number to complete this sale.'),
		});
	}

	function ensureCheckReference(frm) {
		return ensureTenderReference(frm, {
			mode: 'Check/Money Order',
			title: __('Check or Money Order Number'),
			label: __('Check / money order number'),
			description: __('Enter the number printed on the check or money order to complete this sale.'),
		});
	}

	// Only when the store runs on a standalone terminal AND asked for the
	// reference (Payroc Settings → Require Card Reference). With the gateway live
	// the approval code comes back from the device and there is nothing to type.
	function ensureCardReference(frm) {
		const s = switches();
		if (!s || !s.requireCardReference()) return Promise.resolve({ ok: true });
		return ensureTenderReference(frm, {
			mode: payrocMop(),
			title: __('Card Approval Code'),
			label: __('Approval code'),
			// The APPROVAL CODE, not the batch number (one batch covers many sales
			// and does not exist until the batch is closed) and not the card's last
			// 4 (not unique). Only the approval code identifies this transaction.
			description: __('The approval code printed on the terminal receipt for this transaction.'),
			// Refunds too: a negative card row reverses revenue and sales tax, and on
			// a standalone terminal nothing else shows the money went back. The server
			// gate says the same (include_refunds=True) — if this did not, the counter
			// would hit a validation error with no field anywhere to satisfy it.
			includeRefunds: true,
		});
	}

	function wrapSubmitInvoice(controller) {
		if (!controller || !controller.payment || controller.payment.__payrocWrapped) return;
		// Checkout gate: the $0 interim line trips frappe's child-mandatory save
		// error ("Data missing in table Items") on the way to the payment screen
		// — catch it here first with an actionable message.
		const cartEvents = controller.cart && controller.cart.events;
		if (cartEvents && cartEvents.checkout && !controller.cart.__fflPerGunWrapped) {
			const originalCheckout = cartEvents.checkout;
			cartEvents.checkout = async (...a) => {
				const d = controller.frm.doc;
				if (!d.is_return && !d.is_ffl_transfer) {
					const bad = await unpricedPerGunLines(controller.frm);
					if (bad.length) return showPickTheGun(bad);
				}
				return originalCheckout(...a);
			};
			controller.cart.__fflPerGunWrapped = true;
		}
		const events = controller.payment.events;
		const originalSubmit = events.submit_invoice;
		events.submit_invoice = async () => {
			// RETURNS: refund the card against the original sale. Returns do NOT go
			// through the forward-sale 4473/NICS gate (it would wrongly block a
			// firearm-containing return; firearm returns are refused server-side).
			if (controller.frm.doc.is_return) {
				// The standalone-terminal approval code is collected HERE too. This
				// branch returns before the sale path, so leaving the prompt down
				// there meant a card refund could never be given one — while the
				// server demanded it — and the counter had nowhere to type it.
				const refundRef = await ensureCardReference(controller.frm);
				if (!refundRef.ok) return;
				const r = await refundViaPayroc(controller.frm);
				if (!r.skip && !r.ok) return;
				return originalSubmit();
			}
			// COUNTER PICKUP TICKET: the money is on the order it hands over. A copy
			// the register re-priced (it re-applies the price list when it loads a
			// draft, then fills a tender for that total) is saved first — the server
			// pins it back to $0 (counter_pickup.keep_ticket_free) — and repainted,
			// so the cashier sees $0 and nothing below asks for a tender reference
			// or reaches the terminal.
			const ticket = pickupTicket(controller.frm.doc);
			if (ticket && ticket.carriesMoney) {
				frappe.show_alert({ message: __('This is a pickup ticket for {0} — the money is on that order.',
					[ticket.order]), indicator: 'orange' }, 7);
				controller.frm.dirty();
				try { await controller.frm.save(); } catch (e) { return; }   // the server said why
				try { controller.cart.load_invoice(); controller.payment.render_payment_section(); } catch (e) { /* cosmetic */ }
				if (pickupTicket(controller.frm.doc).carriesMoney) {   // never submit a ticket still charging
					ticketStillPriced(ticket.order);
					return;
				}
			}
			// NEGATIVE TICKET (trade-ins exceed the goods): the store pays the customer.
			// Nothing is tendered, so the Zelle / card prompts and the charge below
			// have nothing to do; what has to hold is the payout method (+ reference
			// unless Cash). Blocked here, not just greyed on the screen, so no path
			// (ctrl+Enter, a dialog) completes it without.
			const neg = window.ffl && ffl.negativeTicketState
				? ffl.negativeTicketState(controller.frm.doc) : { negative: false };
			if (neg.negative && neg.problem) {
				frappe.show_alert({ message: ffl.negativeTicketReason(neg), indicator: 'orange' }, 7);
				return;
			}
			// PER-GUN PRICING gate (backstop — the Checkout wrap above catches the
			// normal path): a non-uniform firearm must have its serial picked (the
			// serial sets the price) and a positive rate before it can sell.
			// Transfers deliberately move guns at $0 — skip those.
			if (!controller.frm.doc.is_ffl_transfer) {
				const bad = await unpricedPerGunLines(controller.frm);
				if (bad.length) return showPickTheGun(bad);
			}
			// Any Zelle tender must carry its Transaction Number before we take a
			// card portion or complete the order. Prompt now; cancel aborts checkout.
			const zelle = await ensureZelleReference(controller.frm);
			if (!zelle.ok) return;
			const check = await ensureCheckReference(controller.frm);
			if (!check.ok) return;
			// Standalone-terminal card reference, same rule and same moment: taken
			// before any money moves, cancelling aborts checkout.
			const cardRef = await ensureCardReference(controller.frm);
			if (!cardRef.ok) return;
			// FORWARD SALE: take the payment FIRST (store policy: money may be
			// collected while the 4473/NICS is still pending), but the ORDER only
			// completes after the gate — the gun never leaves stock early. The
			// charge stamps payroc_unique_reference + payroc_charged_amount, so
			// re-clicking Complete Order after the 4473 clears never re-charges.
			// Counter pickup ticket: collect what the ORDER still owes before the
			// gun is handed over (the ticket itself is $0 — pinned above and by the
			// server — and the charge below never runs for it).
			const balance = await collectCounterBalance(controller.frm);
			if (!balance.ok) return;
			const pcm = payrocMath();
			const capturedBefore = pcm ? pcm.sumLiveCharges(controller.frm.doc) : 0;
			const payResult = await chargeViaPayroc(controller.frm);
			// A card was captured (the doc reloaded): repaint the tiles so the card
			// tile shows what is still to charge and the new captured total.
			if (pcm && pcm.sumLiveCharges(controller.frm.doc) !== capturedBefore) {
				try { controller.payment.render_payment_mode_dom(); controller.payment.update_totals_section(); } catch (e) { /* cosmetic */ }
			}
			if (!payResult.skip && !payResult.ok) return;
			if (!await is4473Cleared(controller.frm)) {
				// Persist the held draft so a captured payment reference (e.g. the
				// Zelle Transaction Number set above) survives until the 4473 clears
				// and isn't re-prompted on resume — the card path already saved in
				// chargeViaPayroc; this covers no-card tenders. The 4473 gate is
				// submit-only, so a Draft save here is allowed; best-effort.
				// Stamp the hold — ONLY when the tender covers the order. The charge
				// loop above returned ok, so any card portion is fully captured; the
				// cash / Zelle / ACH rows are whatever the cashier typed, and an
				// under-tendered draft must keep offering Take Payment.
				// The stamp is what the payment-screen button reads
				// (pos_tender_guard.hasHeldPayment); mark dirty explicitly so the
				// save is not contingent on unrelated edits (a card-path doc was just
				// reloaded clean).
				const fullyTendered = !neg.negative && !!(window.ffl && ffl.isFullyTendered
					&& ffl.isFullyTendered(controller.frm.doc));
				if (fullyTendered && !parseInt(controller.frm.doc.ffl_payment_held, 10)) {
					controller.frm.doc.ffl_payment_held = 1;
					controller.frm.dirty();
				}
				if (controller.frm.is_dirty() || controller.frm.doc.__islocal) {
					try { await controller.frm.save(); } catch (e) { /* held regardless; backend gate backstops */ }
				}
				try { if (window.__ffl4473 && window.__ffl4473.refreshBadge) window.__ffl4473.refreshBadge(); } catch (e) { /* cosmetic */ }
				if (neg.negative) {
					// No money moved: the payout is booked when the order completes.
					frappe.msgprint({
						title: __('Order saved — waiting for the 4473'),
						indicator: 'orange',
						message: __('Nothing has been paid out yet. Once the 4473 clears, open this order from <b>Recent Orders → Draft</b> and click <b>Complete Order</b> — that takes the trade-ins and records the {0} paid to the customer.',
							[format_currency(neg.due, controller.frm.doc.currency)]),
					});
					return;
				}
				const s = switches();
				const paper = !!(s && s.paper4473Mode());
				const fbLive = s ? s.fastboundEnabled() : true;
				frappe.msgprint({
					title: __('Payment taken — order held for 4473'),
					indicator: 'orange',
					message: (!paper && !fbLive)
						? __('Payment is recorded on this draft, but it contains a firearm and there is no way to record a 4473: FastBound is switched off and Paper 4473 Mode is not on. A manager needs to switch on <b>FFL Settings → Paper 4473 Mode</b>, or re-enable FastBound, before this order can be completed.')
						: paper
						? __('Payment is recorded on this draft, but it contains a firearm and no 4473 has been recorded. Complete the paper 4473 and the background check, then click <b>Record Paper 4473</b> and Complete Order to finish. If the buyer is denied, a manager refunds on the terminal / returns the cash, then deletes the draft.')
						: __('Payment is recorded on this draft, but it contains a firearm and the 4473/NICS has not cleared. Click <b>FastBound 4473</b> to run it; once the dealer commits the disposition, open this order from <b>Recent Orders → Draft</b> and click Complete Order to finish. If the buyer is denied, a manager refunds: void the card charge (back office) / return the cash, then delete the draft.'),
				});
				return;
			}
			return originalSubmit();
		};
		controller.payment.__payrocWrapped = true;
	}

	// Read the authoritative 4473 flags WITHOUT frm.reload_doc(): this runs on the
	// payment screen, where a reload discards the tender split the cashier just
	// typed (P2). Patch the two fields the badge and
	// the submit gate actually read. The badge itself lives in the 4473 button's
	// own IIFE below, which publishes a repaint hook on window.__ffl4473.
	async function syncGateFlags(frm) {
		if (!frm || !frm.doc || frm.doc.__islocal) return {};
		try {
			const r = await frappe.db.get_value('POS Invoice', frm.doc.name,
				['form_4473_status', 'nics_status']);
			const v = (r && r.message) || {};
			if (v.form_4473_status) {
				frm.doc.form_4473_status = v.form_4473_status;
				frm.doc.nics_status = v.nics_status;
				try {
					if (window.__ffl4473 && window.__ffl4473.refreshBadge) {
						window.__ffl4473.refreshBadge();
					}
				} catch (e) { /* badge repaint is cosmetic */ }
			}
			return v;
		} catch (e) {
			return {};
		}
	}

	// Returns true iff either no firearm in cart OR both 4473 signed + NICS Proceed.
	// The 4473 IIFE below paints the primary button from the same two facts, so
	// the firearm-subset probe is shared via window.__ffl4473 (one cache, one call).
	let _firearmCheckCache = null;
	let _firearmCheckCacheKey = null;
	async function cartHasFirearm(frm) {
		const codes = (frm.doc.items || []).map((r) => r.item_code).filter(Boolean);
		// Cache the firearm subset for the current cart shape so re-clicks
		// don't refetch.
		const cacheKey = codes.slice().sort().join('|');
		if (_firearmCheckCacheKey !== cacheKey) {
			_firearmCheckCacheKey = cacheKey;
			_firearmCheckCache = null;
		}
		if (_firearmCheckCache === null) {
			if (!codes.length) {
				_firearmCheckCache = [];
			} else {
				const r = await frappe.call({
					method: 'ffl_core.firearm.firearm_items_in',
					args: { item_codes: codes },
				});
				_firearmCheckCache = (r && r.message) || [];
			}
		}
		return _firearmCheckCache.length > 0;
	}
	window.__ffl4473 = window.__ffl4473 || {};
	window.__ffl4473.cartHasFirearm = cartHasFirearm;

	async function is4473Cleared(frm) {
		if (!await cartHasFirearm(frm)) return true;  // no firearm → gate doesn't apply
		// A page loaded before the webhook landed still holds the pre-completion
		// flags; ask the server before telling the cashier the 4473 is missing.
		// Deliberately NOT refresh_4473_status: that path can hit FastBound over
		// the network, which has no business on the Complete Order hot path.
		await syncGateFlags(frm);
		return (frm.doc.form_4473_status === 'Completed')
			&& (frm.doc.nics_status === 'Proceed');
	}

	function tryPatch(retriesLeft) {
		// Cart loads of a pickup ticket (pinLoadedTicket). installClassHook
		// (pos_tender_guard.js, loaded after this bundle — hence here, on the POS
		// route, not at parse time) patches ItemCart now or when ERPNext defines it.
		const hook = window.ffl && window.ffl.installClassHook;
		if (hook && window.erpnext && window.erpnext.PointOfSale && !tryPatch.__cartHooked) {
			tryPatch.__cartHooked = hook(window.erpnext.PointOfSale, 'ItemCart', patchCartLoad);
		}
		const C = window.erpnext && window.erpnext.PointOfSale && window.erpnext.PointOfSale.Controller;
		if (!C) {
			if (retriesLeft > 0) setTimeout(() => tryPatch(retriesLeft - 1), 200);
			return;
		}
		// Patch the prototype so any future Controller instance (after the
		// cashier closes + reopens POS without reload) also gets wrapped.
		if (!C.prototype.__payrocPatched) {
			const originalInit = C.prototype.init_payments;
			C.prototype.init_payments = function payrocInitPayments() {
				originalInit.apply(this, arguments);
				wrapSubmitInvoice(this);
			};
			C.prototype.__payrocPatched = true;
		}
		// The live instance was constructed before our prototype patch
		// landed (POS bundle loads ahead of app_include_js bundles in some
		// boot orderings) — wrap it directly. If cur_pos / cur_pos.payment
		// isn't ready yet, retry briefly: either order is possible
		// depending on Frappe/ERPNext version and cache state.
		if (window.cur_pos && cur_pos.payment) {
			wrapSubmitInvoice(cur_pos);
		} else if (retriesLeft > 0) {
			setTimeout(() => tryPatch(retriesLeft - 1), 200);
		}
	}

	function isPosRoute() {
		return location.hash.includes('point-of-sale')
			|| location.pathname.includes('/point-of-sale');
	}

	function init() {
		if (!window.frappe) return;
		const trigger = () => { if (isPosRoute()) tryPatch(20); };
		if (frappe.router) frappe.router.on('change', trigger);
		trigger();
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', init);
	} else {
		init();
	}
})();


// === FastBound 4473 button on POS Payment screen =========================
//
// Renders a "FastBound 4473" button next to "Complete Order" whenever the
// POS Payment screen is shown. Clicking it opens a dialog that collects:
//   - One serial per firearm row in the cart
//   - Transferee fields (auto-filled from the linked Customer / address)
// Submitting the dialog calls ffl_integrations.fastbound.pos_4473.start_4473_for_pos_invoice
// which creates a pending FastBound Disposition + Contact and returns
// the FastBound URL to open in a new tab. The cashier completes the 4473
// + NICS in FastBound's own UI; webhook events back here flip
// form_4473_status / nics_status on the invoice and the realtime listener
// reloads the form so Complete Order unlocks.

(function setup4473PosHook() {

	function isPosRoute() {
		return location.hash.includes('point-of-sale')
			|| location.pathname.includes('/point-of-sale');
	}

	function curFrm() {
		return window.cur_pos && cur_pos.frm;
	}

	// Paper 4473 Mode: 4473s and NICS happen off-system, so the counter records
	// one instead of starting one. Lazy read — boot may not be ready at load.
	function paperMode() {
		const s = window.ffl && window.ffl.switches;
		return !!(s && s.paper4473Mode());
	}

	function fastboundLive() {
		const s = window.ffl && window.ffl.switches;
		return s ? s.fastboundEnabled() : true;   // no switches module → pre-switch behaviour
	}

	function getStatusBadge(frm) {
		const f = (frm && frm.doc && frm.doc.form_4473_status) || 'N/A';
		const n = (frm && frm.doc && frm.doc.nics_status) || 'N/A';
		const done = f === 'Completed' && n === 'Proceed';
		// A recorded 4473 is a recorded 4473 whatever the settings say — an order a
		// manager already overrode, or one whose webhook landed before FastBound was
		// switched off, submits fine and must not be painted as broken. So the
		// dead-end check comes AFTER this.
		if (done && !paperMode()) return { color: '#388e3c', label: __('4473 Complete') };
		if (!paperMode() && !fastboundLive()) {
			// The dead end: FastBound switched off, paper mode not switched on. The
			// server's refusal names the setting to change, but the counter never
			// gets that far — the client's own 4473 check blocks before submit — so
			// the button has to say it. Red, because this is a configuration fault,
			// not a step in anyone's workflow.
			return { color: '#c0392b', label: __('4473 Not Configured') };
		}
		if (paperMode()) {
			// Never "Awaiting": nothing is coming, and a cashier told to wait for a
			// webhook this store does not receive is stuck with no way forward.
			return done
				? { color: '#388e3c', label: __('4473 Recorded') }
				: { color: '#6c7680', label: __('Record Paper 4473') };
		}
		// Pending, or a finished form whose NICS did not clear (Delayed / Denied /
		// Cancelled): a 4473 is under way, so never the grey "FastBound 4473" that
		// reads as not started. The same three-level rule as the Recent Orders
		// chips (pos_tender_guard.js form4473State).
		const state = (window.ffl && typeof window.ffl.form4473State === 'function')
			? window.ffl.form4473State(f, n)
			: (f === 'Pending' || f === 'Completed' ? 'awaiting' : 'not_started');
		if (state === 'awaiting') return { color: '#f57c00', label: __('Awaiting 4473') };
		return { color: '#6c7680', label: __('FastBound 4473') };
	}

	// The Refresh 4473 button's server call: re-read the order, self-heal the
	// doc-level flags from the local disposition ledger, and — only when coverage
	// is incomplete — report what FastBound knows. Never reloads the doc: this
	// button sits on the payment screen and a reload would discard the tender
	// split the cashier just typed (P2).
	async function refresh4473FromServer(frm, { silent = false } = {}) {
		if (!frm || !frm.doc || frm.doc.__islocal) return {};
		let m = {};
		try {
			const r = await frappe.call({
				method: 'ffl_integrations.fastbound.pos_4473_recovery.refresh_4473_status',
				args: { invoice_name: frm.doc.name },
			});
			m = (r && r.message) || {};
		} catch (e) {
			if (!silent) {
				frappe.show_alert({ message: __('Could not refresh the 4473 status.'),
					indicator: 'orange' });
			}
			return {};
		}
		if (m.form_4473_status) {
			frm.doc.form_4473_status = m.form_4473_status;
			frm.doc.nics_status = m.nics_status;
			refresh4473ButtonStyle();
		}
		if (silent) return m;
		if (m.state === 'completed' || m.state === 'repaired_from_ledger') {
			frappe.show_alert({
				message: m.state === 'repaired_from_ledger'
					? __('4473 is complete — this order was out of date and has been corrected. You can Complete Order.')
					: __('4473 is complete. You can Complete Order.'),
				indicator: 'green',
			}, 7);
		} else if (m.state === 'fastbound_only') {
			const rows = (m.fastbound || []).map((f) => `
				<tr><td>${frappe.utils.escape_html(f.serial || '')}</td>
				<td>${f.disposed ? __('Disposed') : __('In stock')}</td>
				<td>${frappe.utils.escape_html(f.ttsn || '—')}</td>
				<td style="font-family:monospace">${frappe.utils.escape_html(f.disposition_id || '—')}</td></tr>`).join('');
			frappe.msgprint({
				title: __('FastBound already shows this firearm as disposed'),
				message: `
					<p>${__('No completed 4473 has synced back for every firearm on this order, but FastBound reports:')}</p>
					<table class="table table-bordered"><thead><tr>
						<th>${__('Serial')}</th><th>${__('Status')}</th>
						<th>${__('TTSN')}</th><th>${__('Disposition')}</th>
					</tr></thead><tbody>${rows}</tbody></table>
					<p>${__('FastBound\'s API does not expose the NICS outcome — a manager must confirm NICS Proceed and use the 4473 override.')}</p>`,
			});
		} else if (m.state === 'fb_unreachable') {
			frappe.show_alert({
				message: __('Could not reach FastBound — checked the local records only. Still waiting on the 4473.'),
				indicator: 'orange',
			}, 7);
		} else if (m.state === 'not_ready') {
			frappe.show_alert({
				message: __('FastBound has not sent the completed 4473 back yet.'),
				indicator: 'orange',
			}, 7);
		} else if (m.state === 'no_serials') {
			frappe.show_alert({
				message: __('No serial on this order yet — click FastBound 4473 to start.'),
				indicator: 'orange',
			}, 7);
		} else if (m.state === 'no_firearm') {
			frappe.show_alert({
				message: __('No firearm on this order — no 4473 needed.'),
				indicator: 'blue',
			}, 5);
		}
		return m;
	}

	// The Complete Order patch lives in its own IIFE above and needs to repaint
	// the badge after re-reading the flags; hand it a hook rather than a global
	// function (these two closures can't see each other's declarations).
	window.__ffl4473 = window.__ffl4473 || {};
	window.__ffl4473.refreshBadge = refresh4473ButtonStyle;

	function inject4473Button(host) {
		if (!host) return;
		host = $(host);
		if (host.find('.ffl-4473-row').length) return;

		const frm = curFrm();
		const badge = getStatusBadge(frm);
		// One row: the 4473 button takes the width that's left, the refresh sits
		// beside it. Re-reading state is a companion to starting a 4473, not a
		// second full-width action competing with Complete Order.
		const $row = $(`
			<div class="ffl-4473-row" style="
				display:flex; gap:8px; margin:8px 12px; align-items:stretch;
			">
				<div class="ffl-4473-btn" style="
					flex:1 1 auto; padding:14px 16px; min-height:56px;
					background:${badge.color}; color:white;
					font-weight:600; font-size:16px;
					cursor:pointer; border-radius:8px;
					display:flex; align-items:center; justify-content:center;
					user-select:none; box-shadow:0 1px 3px rgba(0,0,0,.12);
				">${badge.label}</div>
				<div class="ffl-4473-refresh-btn" title="${__('Refresh 4473 status')}" style="
					flex:0 0 64px; min-height:56px;
					background:#6c7680; color:white;
					font-weight:600; font-size:22px; line-height:1;
					cursor:pointer; border-radius:8px;
					display:flex; align-items:center; justify-content:center;
					user-select:none; box-shadow:0 1px 3px rgba(0,0,0,.12);
				">⟳</div>
			</div>
		`);
		const $submit = host.find('.submit-order-btn').first();
		if ($submit.length) $submit.before($row);
		else host.append($row);

		// The refresh button asks FastBound what it knows. In Paper 4473 Mode there
		// is nothing on the other end, so drop it rather than offer a control that
		// can only ever report "still waiting".
		if (paperMode() || !fastboundLive()) $row.find('.ffl-4473-refresh-btn').remove();

		$row.find('.ffl-4473-btn').on('click', () => {
			const f = curFrm() && curFrm().doc;
			const recorded = f && f.form_4473_status === 'Completed' && f.nics_status === 'Proceed';
			// Green badge, green answer — don't tell someone their recorded 4473 has
			// no route just because the settings have none for the NEXT one.
			if (!recorded && !paperMode() && !fastboundLive()) return showNo4473Route();
			return paperMode() ? openPaper4473Dialog() : open4473Dialog();
		});

		// Re-read the 4473 state on demand. The webhook stamps the invoice with
		// db.set_value and a POS page can miss the realtime nudge (other tab,
		// reopened page), which is how a completed order kept showing
		// "Awaiting 4473" — a past incident.
		$row.find('.ffl-4473-refresh-btn').on('click', async () => {
			const f = curFrm();
			if (!f) return;
			frappe.dom.freeze(__('Checking 4473 status…'));
			try {
				await refresh4473FromServer(f);
			} finally {
				frappe.dom.unfreeze();
			}
		});
	}

	function refresh4473ButtonStyle() {
		// Idempotent: only mutate when the desired state differs from
		// rendered state. Otherwise the MutationObserver re-triggers on
		// every render and we get an infinite loop / renderer freeze.
		const $btn = $('.ffl-4473-btn');
		if (!$btn.length) return;
		const badge = getStatusBadge(curFrm());
		refreshSubmitButton();
		if ($btn.data('badge-label') === badge.label) return;
		$btn.data('badge-label', badge.label);
		$btn.css('background', badge.color).text(badge.label);
	}

	// The primary button says what its click does (pos_tender_guard.submitButtonState):
	// "Take Payment" while a firearm order's 4473 is pending, quiet once the money is
	// held in full, ERPNext's own label once the 4473 clears (or when there is
	// nothing to gate). Async because "is there a firearm in the cart" is a server
	// probe (cached per cart shape by the Complete Order IIFE). Never disables the
	// button: the click handler is untouched, so a click on the held state runs the
	// submit wrapper, which re-checks the 4473 server-side (self-heal on a page
	// that missed the webhook nudge) and otherwise just re-shows the held notice.
	// A generation counter drops the write of an overlapping older run (the
	// observer fires per mutation batch); the write is idempotent so it never loops.
	const SUBMIT_LABELS = { pay: () => __('Take Payment'), held: () => __('Paid — waiting for 4473'),
		wait: () => __('Save — waiting for 4473') };
	let _submitGen = 0;
	async function refreshSubmitButton() {
		const frm = curFrm();
		const $submit = $('.submit-order-btn').first();
		const api = window.ffl;
		if (!frm || !frm.doc || !$submit.length || !api || !api.submitButtonState) return;
		const gen = ++_submitGen;
		const probe = window.__ffl4473 && window.__ffl4473.cartHasFirearm;
		let hasFirearm = false;
		try { hasFirearm = probe ? await probe(frm) : false; } catch (e) { hasFirearm = false; }
		if (gen !== _submitGen) return;                       // a newer run owns the button
		const doc = frm.doc;
		const cleared = doc.form_4473_status === 'Completed' && doc.nics_status === 'Proceed';
		const state = api.submitButtonState(doc, { hasFirearm, cleared });
		if ($submit.data('ffl-submit-key') === state.key) return;
		$submit.data('ffl-submit-key', state.key);
		if (SUBMIT_LABELS[state.key]) {
			// Ours: remember ERPNext's own label once so `complete` can hand it back.
			if ($submit.data('ffl-native-label') === undefined) $submit.data('ffl-native-label', $submit.text());
			$submit.text(SUBMIT_LABELS[state.key]());
		} else if ($submit.data('ffl-native-label') !== undefined) {
			$submit.text($submit.data('ffl-native-label'));
		}
		$submit.toggleClass('ffl-submit-held', !!state.quiet);
	}

	// Paper 4473 Mode's whole counter interaction: confirm that the paper form is
	// filled in and the background check cleared, then record it. The server books
	// one bound-book Disposition per gun on this ticket, stamped with who ticked
	// the box (ffl_integrations.fastbound.pos_4473.attest_paper_4473).
	//
	// A confirmation step rather than a bare checkbox because this is an
	// attestation, not a preference: the wording has to state what is being
	// asserted, and it is what an audit reads back later.
	// Neither route to a 4473 is open: FastBound is switched off and Paper 4473
	// Mode is not on. Nothing the cashier can do at the counter fixes it, so say
	// exactly which setting a manager has to change instead of failing vaguely.
	function showNo4473Route() {
		frappe.msgprint({
			title: __('No 4473 route configured'),
			indicator: 'red',
			message: __('FastBound is switched off and Paper 4473 Mode is not on, so the counter has no way to record a 4473 for this sale.<br><br>A manager needs to switch on <b>FFL Settings → Paper 4473 Mode</b> (if this store keeps paper 4473s) or re-enable <b>FastBound Settings → Enable FastBound</b>.<br><br>For a one-off, a manager can still use <b>Manager Override 4473</b> on the order in the desk view — it works in this configuration and records who overrode it and why.'),
		});
	}

	async function openPaper4473Dialog() {
		const frm = curFrm();
		if (!frm) {
			frappe.show_alert({ message: __('POS not ready.'), indicator: 'orange' });
			return;
		}
		if (frm.doc.__islocal || frm.is_dirty()) {
			// The server books against a saved invoice; save first so the cashier
			// isn't told "not found" for a cart that plainly exists.
			try {
				await frm.save();
			} catch (e) {
				frappe.msgprint({
					title: __('Could not save the order'), indicator: 'red',
					message: (e && e.message) || __('Fix the errors on the order, then record the 4473.'),
				});
				return;
			}
		}
		// Never decide this from the in-memory doc — the same reason open4473Dialog
		// gives (a page loaded before the flags landed shows a stale status;
		// A past incident). Reuse the existing server read.
		await refresh4473FromServer(frm, { silent: true });
		if ((frm.doc.form_4473_status || 'N/A') === 'Completed') {
			frappe.show_alert({
				message: __('The paper 4473 is already recorded on this order.'), indicator: 'blue' });
			return;
		}
		const dlg = new frappe.ui.Dialog({
			title: __('Record paper 4473'),
			fields: [{
				fieldtype: 'HTML',
				options: `<div class="alert alert-warning" style="margin-bottom:10px">
					<p style="margin:0 0 8px">${__('Confirm, for every firearm on this order:')}</p>
					<ol style="margin:0 0 8px 18px;padding:0">
						<li>${__('the ATF Form 4473 is completed and signed;')}</li>
						<li>${__('the background check was run and came back Proceed;')}</li>
						<li>${__('the form is filed with your records.')}</li>
					</ol>
					<p style="margin:0">${__('This is recorded in the bound book under your name and cannot be undone from the counter.')}</p>
				</div>`,
			}],
			primary_action_label: __('Yes — 4473 completed'),
			primary_action: async () => {
				dlg.hide();
				frappe.dom.freeze(__('Recording the 4473…'));
				try {
					const r = await frappe.call({
						method: 'ffl_integrations.fastbound.pos_4473.attest_paper_4473',
						args: { invoice_name: frm.doc.name },
					});
					const m = (r && r.message) || {};
					if (!m.ok) return;
					frm.doc.form_4473_status = 'Completed';
					frm.doc.nics_status = 'Proceed';
					refresh4473ButtonStyle();
					frappe.show_alert({
						message: __('4473 recorded for {0} firearm(s). You can Complete Order.',
							[m.n_firearms || 0]),
						indicator: 'green',
					}, 7);
				} finally {
					frappe.dom.unfreeze();
				}
			},
		});
		dlg.show();
	}

	async function open4473Dialog() {
		const frm = curFrm();
		if (!frm) {
			frappe.show_alert({ message: __('POS not ready.'), indicator: 'orange' });
			return;
		}
		// Never decide this from the in-memory doc: a page loaded before the
		// webhook landed shows Pending, and clicking through used to reset a
		// completed 4473 (a past incident — the server now refuses
		// outright, so this also keeps the cashier from hitting that error).
		await refresh4473FromServer(frm, { silent: true });
		if ((frm.doc.form_4473_status || 'N/A') === 'Completed') {
			const isManager = (frappe.user_roles || []).some(
				(r) => r === 'Sales Manager' || r === 'System Manager');
			const d = frappe.msgprint({
				title: __('4473 already complete'),
				message: __('This order already has a completed 4473. To redo it, void the FastBound disposition first.'),
				primary_action: isManager ? {
					label: __('Redo 4473'),
					action() { d.hide(); promptRedo4473(frm); },
				} : undefined,
			});
			return;
		}
		const itemCodes = (frm.doc.items || []).map((r) => r.item_code).filter(Boolean);
		if (!itemCodes.length) {
			frappe.show_alert({ message: __('Add items to the cart first.'), indicator: 'orange' });
			return;
		}
		frappe.call({
			method: 'ffl_core.firearm.firearm_items_in',
			args: { item_codes: itemCodes },
		}).then((r) => {
			const firearmCodes = new Set((r && r.message) || []);
			const firearmRows = (frm.doc.items || [])
				.filter((row) => firearmCodes.has(row.item_code));
			if (!firearmRows.length) {
				frappe.show_alert({ message: __('No firearms in this cart — 4473 is not needed.'), indicator: 'blue' });
				return;
			}
			if (!frm.doc.customer) {
				frappe.msgprint({ title: __('Customer required'), indicator: 'red',
					message: __('Pick a customer before starting the 4473.') });
				return;
			}
			const fcodes = [...new Set(firearmRows.map((r2) => r2.item_code))];
			frappe.call({
				method: 'ffl_core.firearm.available_serials_for_items',
				// Pass the current invoice so a serial already parked on ITS OWN line
				// stays offered (and default-selects below) — incident
				// A past invoice self-lockout, serial 21696 vanishing.
				args: { item_codes: fcodes, current_invoice: frm.doc.name },
			}).then((sr) => {
				renderDialog(frm, firearmRows, firearmCodes, (sr && sr.message) || {});
			});
		});
	}

	function renderDialog(frm, firearmRows, firearmCodes, serialsByItem) {
		serialsByItem = serialsByItem || {};
		let anyUnavailable = false;
		const fields = [{
			fieldtype: 'HTML',
			options: `<div class="alert alert-info" style="margin-bottom:10px">
				${__('Enter each firearm\'s serial below, then click <b>Open FastBound</b>.')}
				<ol style="margin:8px 0 0 18px;padding:0">
					<li>${__('Click <b>Start New 4473</b> in the FastBound tab that opens.')}</li>
					<li>${__('Scan or type the serial, fill the buyer\'s details, customer signs in FastBound.')}</li>
					<li>${__('Save & Commit in FastBound — this order will unlock automatically.')}</li>
				</ol></div>`,
		}, {
			fieldtype: 'Section Break',
			label: __('Firearm Serial Numbers'),
		}];
		firearmRows.forEach((row) => {
			const opts = serialsByItem[row.item_code] || [];
			if (!opts.length) {
				anyUnavailable = true;
				fields.push({
					fieldtype: 'HTML',
					options: `<div class="alert alert-warning" style="margin:4px 0">${__('No in-stock serial for {0} (row {1}). Receive it before selling.', [frappe.utils.escape_html(row.item_name || row.item_code), row.idx])}</div>`,
				});
				return;
			}
			fields.push({
				fieldtype: 'Select',
				fieldname: `serial_${row.name}`,
				label: `${row.item_name || row.item_code} (row ${row.idx})`,
				reqd: 1,
				options: opts,
				default: opts.indexOf(row.serial_no) >= 0 ? row.serial_no : opts[0],
			});
		});

		const dialog = new frappe.ui.Dialog({
			title: __('Start FastBound 4473'),
			size: 'large',
			fields,
			primary_action_label: __('Open FastBound'),
			primary_action(values) {
				const serial_by_item_row = {};
				firearmRows.forEach((row) => {
					serial_by_item_row[row.name] = (values[`serial_${row.name}`] || '').trim();
				});
				dialog.disable_primary_action();
				frappe.dom.freeze(__('Holding order for 4473…'));
				// POS Invoice may still be client-side (e.g. "new-pos-invoice-…").
				// Save first so the backend can frappe.get_doc by real name.
				const ensureSaved = frm.doc.__islocal ? frm.save() : Promise.resolve();
				ensureSaved.then(() => {
					// After save, dialog's serial_by_item_row keys reference the old
					// child-row names — remap to current row names by ordinal.
					const remapped = {};
					const oldKeys = Object.keys(serial_by_item_row);
					const currentRows = (frm.doc.items || [])
						.filter((row) => firearmCodes.has(row.item_code));
					currentRows.forEach((row, idx) => {
						const oldKey = oldKeys[idx];
						if (oldKey) remapped[row.name] = serial_by_item_row[oldKey];
					});
					return frappe.call({
						method: 'ffl_integrations.fastbound.pos_4473.start_4473_for_pos_invoice',
						args: {
							invoice_name: frm.doc.name,
							serial_by_item_row: remapped,
						},
					});
				}).then((r) => {
					frappe.dom.unfreeze();
					const m = (r && r.message) || {};
					if (!m.ok) {
						dialog.enable_primary_action();
						return;
					}
					dialog.hide();
					// Paper mode returns no URL — there is no wizard to send anyone to,
					// and window.open('') would pop a blank tab in the cashier's face.
					if (m.fastbound_url) window.open(m.fastbound_url, '_blank');
					frappe.show_alert({
						message: m.fastbound_url
							? __('Order parked for 4473. Serve other customers meanwhile — reopen it from Recent Orders → Draft; submit unlocks once the 4473 + NICS clear in FastBound.')
							: __('Order parked for the paper 4473. Complete the form and the background check, then reopen it from Recent Orders → Draft and click <b>Record Paper 4473</b>.'),
						indicator: 'green',
					}, 8);
					// Park this firearm order (already saved as Draft) and start a
					// fresh cart, so the register isn't stuck waiting on NICS (which
					// can take minutes to days). Resume later from Recent Orders →
					// Draft (built-in). Fall back to a plain reload if the POS
					// controller API isn't available.
					if (window.cur_pos && typeof window.cur_pos.load_new_invoice_on_pos === 'function') {
						window.cur_pos.load_new_invoice_on_pos();
					} else {
						frm.reload_doc();
					}
				}).catch(() => {
					frappe.dom.unfreeze();
					dialog.enable_primary_action();
				});
			},
		});
		if (anyUnavailable) dialog.disable_primary_action();
		dialog.show();
	}

	// Manager-only: re-open a COMPLETED 4473. The server cancels this order's
	// Retail Sale dispositions and clears the webhook-owned field group, so the
	// Refresh button can't resurrect the old status afterwards.
	function promptRedo4473(frm) {
		frappe.prompt([
			{
				fieldtype: 'Check', fieldname: 'voided', default: 0,
				label: __('I have already voided this disposition in FastBound'),
			},
			{
				fieldtype: 'Small Text', fieldname: 'reason', reqd: 1,
				label: __('Reason (recorded on the order)'),
			},
		], (values) => {
			if (!values.voided) {
				frappe.show_alert({
					message: __('Void the disposition in FastBound first, then redo the 4473.'),
					indicator: 'orange',
				}, 7);
				return;
			}
			frappe.dom.freeze(__('Re-opening the 4473…'));
			frappe.call({
				method: 'ffl_integrations.fastbound.pos_4473_recovery.manager_redo_4473',
				args: { invoice_name: frm.doc.name, reason: values.reason },
			}).then((r) => {
				frappe.dom.unfreeze();
				const m = (r && r.message) || {};
				if (!m.ok) return;
				frm.doc.form_4473_status = 'Pending';
				frm.doc.nics_status = 'N/A';
				refresh4473ButtonStyle();
				frappe.show_alert({
					message: __('4473 re-opened ({0} disposition(s) cancelled). Start a new 4473 when ready.', [m.n_cancelled]),
					indicator: 'green',
				}, 8);
			}).catch(() => frappe.dom.unfreeze());
		}, __('Redo 4473'), __('Redo 4473'));
	}

	// ── Customer Transfer-In (4473) ──────────────────────────────────────────
	// A customer buys a gun elsewhere, ships it to the store; we run the 4473 +
	// NICS and charge only a Transfer Fee (the gun is NOT sold — it's already
	// the customer's). Model: N $0 firearm lines (to drive the existing 4473 →
	// disposition → stock-out machinery) + one Transfer Fee line (qty = guns).
	// Reuses start_4473_for_pos_invoice as-is; normal sales add no fee.

	// The transfer button lives on the MAIN cart screen (above Checkout), not the
	// payment screen where the 4473 button sits: a transfer starts from an EMPTY
	// cart, and ERPNext POS won't advance an empty cart to payment. Scoped to
	// .cart-totals-section so it never lands on the payment numpad's checkout-btn.
	function injectTransferButton() {
		const $checkout = $('.cart-totals-section .checkout-btn').first();
		if (!$checkout.length) return;
		if ($checkout.parent().find('.ffl-transfer-btn').length) return;
		const $btn = $(`
			<div class="ffl-transfer-btn" style="
				padding:12px 16px; margin:8px 0;
				background:#5b3e9e; color:white;
				font-weight:600; font-size:15px;
				cursor:pointer; border-radius:8px;
				text-align:center; user-select:none;
			">${__('Transfer (4473)')}</div>
		`);
		$checkout.before($btn);
		$btn.on('click', () => openTransferDialog());
	}

	function openTransferDialog() {
		const frm = curFrm();
		if (!frm) {
			frappe.show_alert({ message: __('POS not ready.'), indicator: 'orange' });
			return;
		}
		if (!frm.doc.customer) {
			frappe.msgprint({ title: __('Customer required'), indicator: 'red',
				message: __('Pick the customer receiving the transfer before starting the 4473.') });
			return;
		}
		// A transfer is a counter 4473 disposition to an individual. The server
		// 4473 gate is SKIPPED for FFL dealers (compliance/firearm.py), so block
		// dealer transferees here — dealer-to-dealer is a separate B2B flow.
		frappe.db.get_value('Customer', frm.doc.customer, 'is_ffl_dealer').then((r) => {
			if (r && r.message && r.message.is_ffl_dealer) {
				frappe.msgprint({ title: __('Not for dealer transfers'), indicator: 'red',
					message: __('{0} is an FFL dealer — a counter 4473 transfer is for individuals only. Handle a dealer-to-dealer shipment through the bound book disposition instead.', [frappe.utils.escape_html(frm.doc.customer_name || frm.doc.customer)]) });
				return;
			}
			// Independent calls — fetch the stock list and the fee config together.
			Promise.all([
				frappe.call({ method: 'ffl_core.firearm.in_stock_firearm_serials' }),
				frappe.call({ method: 'ffl_core.firearm.get_transfer_config' }),
			]).then(([sr, cr]) => {
				const serials = (sr && sr.message) || [];
				if (!serials.length) {
					frappe.msgprint({ title: __('No firearms in stock'), indicator: 'orange',
						message: __("No in-stock firearm serials to transfer. Receive the customer's firearm first (Receive Goods, cost $0).") });
					return;
				}
				renderTransferDialog(frm, serials, (cr && cr.message) || {});
			});
		});
	}

	function renderTransferDialog(frm, serials, cfg) {
		// Scan or type a serial to pick the gun, instead of dumping the whole
		// in-stock list. The fetched list is used as a client-side lookup +
		// type-ahead source (suggestions filter as you type); each resolved gun
		// is added to the list below. Multiple guns per transfer are supported.
		const bySerial = {};
		const acOptions = serials.map((s) => {
			const price = (s.sell_price != null) ? format_currency(s.sell_price) : __('un-priced');
			bySerial[s.serial] = { item_name: s.item_name || s.item_code || s.serial, price };
			return {
				label: `${s.serial} — ${frappe.utils.escape_html(s.item_name || s.item_code || '')} — ${price}`,
				value: s.serial,
			};
		});
		// Guns already on THIS invoice (e.g. a parked transfer restored from
		// Recent Orders) — exclude them from the picker so they can't be added a
		// second time on re-open. The cart already shows them.
		const onInvoice = (frm.doc.items || [])
			.map((it) => (it.serial_no || '').trim())
			.filter((ser) => ser && bySerial[ser]);
		const selected = [];       // serials added in THIS dialog session
		let dialog;
		let lastSerial = '', lastTs = 0;  // de-dupe a select + Enter double-fire

		function refreshAdded() {
			const $w = dialog.fields_dict.added.$wrapper;
			if (!selected.length) {
				$w.html(`<div class="text-muted" style="margin:6px 2px">${__('No firearms added yet — scan or type a serial above.')}</div>`);
				return;
			}
			const rows = selected.map((serial, i) => {
				const info = bySerial[serial] || {};
				return `<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 8px;border:1px solid var(--border-color);border-radius:6px;margin-bottom:4px">
					<span>${frappe.utils.escape_html(serial)} — ${frappe.utils.escape_html(info.item_name || '')} — ${info.price || ''}</span>
					<button class="btn btn-xs btn-default ffl-xfer-remove" data-idx="${i}" title="${__('Remove')}">✕</button>
				</div>`;
			}).join('');
			$w.html(`<div style="margin:4px 0 2px"><b>${__('Added ({0})', [String(selected.length)])}</b></div>${rows}`);
		}

		function _syncSuggestions() {
			// Drop already-added guns from the type-ahead so one can't reappear or
			// be picked twice (bare-serial dedup in addSerial still backs this up).
			const c = dialog.fields_dict.serial_input;
			if (c && typeof c.set_data === 'function') {
				c.set_data(acOptions.filter((o) =>
					selected.indexOf(o.value) < 0 && onInvoice.indexOf(o.value) < 0));
			}
		}

		function addSerial(raw) {
			// Accept either the bare serial (scan) or the "serial — name — price"
			// label (dropdown pick), so it works whichever the control hands back.
			const serial = (raw || '').split(' — ')[0].trim();
			if (!serial) return;
			const now = Date.now();
			if (serial === lastSerial && (now - lastTs) < 600) return;  // ignore the paired select+Enter
			lastSerial = serial; lastTs = now;
			if (!bySerial[serial]) {
				frappe.show_alert({ message: __('{0} is not an in-stock firearm serial.', [serial]), indicator: 'orange' });
				return;
			}
			if (selected.indexOf(serial) >= 0) {
				frappe.show_alert({ message: __('{0} is already added.', [serial]), indicator: 'orange' });
				return;
			}
			selected.push(serial);
			refreshAdded();
			_syncSuggestions();
			// Clear the box for the next scan. Defer past Frappe's awesomplete
			// change cycle: a mouse-PICK asynchronously re-writes the input to the
			// picked label AFTER a synchronous clear (its set_input runs as a
			// microtask while inside_change_event is still true), leaving the
			// just-added gun lingering in the field. A 0ms timeout fires after that
			// microtask, so the field reliably empties on BOTH the Enter and click
			// paths. set_value('') doesn't dispatch keydown/selectcomplete, so it
			// can't re-enter addSerial.
			setTimeout(() => {
				const c = dialog.fields_dict.serial_input;
				c.set_value('');
				if (c.$input) { c.$input.val(''); c.$input.focus(); }
			}, 0);
		}

		dialog = new frappe.ui.Dialog({
			title: __('Customer Transfer (4473)'),
			fields: [{
				fieldtype: 'HTML',
				options: `<div class="alert alert-info" style="margin-bottom:10px">
					${(window.ffl && window.ffl.switches && window.ffl.switches.paper4473Mode())
						? __("Scan or type the serial of each firearm this customer is transferring in. Each is added at $0 (already the customer's) plus one Transfer Fee per gun. Then complete the PAPER 4473 and record it with <b>Record Paper 4473</b> on the payment screen.")
						: __("Scan or type the serial of each firearm this customer is transferring in. Each is added at $0 (already the customer's) plus one Transfer Fee per gun, then you finish the 4473 in FastBound.")}
				</div>`,
			}, {
				fieldtype: 'Autocomplete',
				fieldname: 'serial_input',
				label: __('Scan or type serial'),
				options: acOptions,
				description: __('Type a few characters to match in-stock firearms, or scan the serial; press Enter to add.'),
			}, {
				fieldtype: 'HTML',
				fieldname: 'added',
			}, {
				fieldtype: 'Section Break',
			}, {
				fieldtype: 'Currency',
				fieldname: 'fee',
				label: __('Transfer Fee (per firearm)'),
				default: cfg.fee_amount || 0,
				description: __('Charged once per added firearm. Editable.'),
			}],
			// Paper mode never opens FastBound — this button parks the transfer so the
			// counter can record the paper 4473 on it.
			primary_action_label: (window.ffl && window.ffl.switches && window.ffl.switches.paper4473Mode())
				? __('Start Transfer') : __('Open FastBound'),
			primary_action() {
				if (!selected.length) {
					frappe.show_alert({ message: __('Scan or enter at least one firearm serial to transfer.'), indicator: 'orange' });
					return;
				}
				const fee = flt(dialog.get_value('fee'));
				const feeItem = cfg.fee_item || 'TRANSFER-FEE';
				dialog.disable_primary_action();
				frappe.dom.freeze(__('Building transfer order…'));
				// The whole transfer invoice is built server-side: the cart is
				// empty (can't save empty client-side) and a bare add_child row
				// lacks item_name/uom/income_account. The backend builds N $0 gun
				// lines + one Transfer Fee line, sets is_ffl_transfer, and kicks
				// off the 4473 — then we open FastBound and park.
				frappe.call({
					method: 'ffl_integrations.fastbound.pos_4473.start_transfer_for_pos_invoice',
					args: {
						serials: selected.slice(),
						fee_item: feeItem,
						fee_amount: fee,
						customer: frm.doc.customer,
						pos_profile: frm.doc.pos_profile || '',
						invoice_name: frm.doc.__islocal ? '' : frm.doc.name,
					},
				}).then((r) => {
					frappe.dom.unfreeze();
					const m = (r && r.message) || {};
					if (!m.ok) { dialog.enable_primary_action(); return; }
					dialog.hide();
					// Paper mode returns no URL — there is no wizard to send anyone to,
					// and window.open('') would pop a blank tab in the cashier's face.
					if (m.fastbound_url) window.open(m.fastbound_url, '_blank');
					frappe.show_alert({
						message: m.fastbound_url
							? __('Transfer parked for 4473. Serve other customers meanwhile — reopen from Recent Orders → Draft; submit unlocks once the 4473 + NICS clear in FastBound.')
							: __('Transfer parked for the paper 4473. Complete the form and the background check, then reopen it from Recent Orders → Draft and click <b>Record Paper 4473</b>.'),
						indicator: 'green',
					}, 8);
					if (window.cur_pos && typeof window.cur_pos.load_new_invoice_on_pos === 'function') {
						window.cur_pos.load_new_invoice_on_pos();
					} else {
						frm.reload_doc();
					}
				}).catch(() => {
					frappe.dom.unfreeze();
					dialog.enable_primary_action();
				});
			},
		});

		// Remove a gun from the added list (event-delegated — rows re-render).
		dialog.fields_dict.added.$wrapper.on('click', '.ffl-xfer-remove', function () {
			const idx = parseInt($(this).attr('data-idx'), 10);
			if (!isNaN(idx)) { selected.splice(idx, 1); refreshAdded(); _syncSuggestions(); }
		});
		// Add a gun on either interaction mode (both routed through addSerial,
		// which de-dupes the paired fire): Enter (scanner sends serial + Enter,
		// or manual type + Enter) and clicking a type-ahead suggestion.
		const $inp = dialog.fields_dict.serial_input.$input;
		if ($inp) {
			$inp.on('keydown', (e) => {
				if (e.which === 13) { e.preventDefault(); e.stopPropagation(); addSerial($inp.val()); }
			});
			$inp.on('awesomplete-selectcomplete', () => addSerial($inp.val()));
		}

		refreshAdded();
		_syncSuggestions();  // pre-filter: drop guns already on this invoice
		dialog.show();
		setTimeout(() => { if ($inp) $inp.focus(); }, 200);
	}

	function tryInjectButton(retriesLeft) {
		if (!isPosRoute()) return;
		const $submit = $('.submit-order-btn').first();
		if ($submit.length && $submit.is(':visible')) {
			inject4473Button($submit.parent());
			return;
		}
		if (retriesLeft > 0) setTimeout(() => tryInjectButton(retriesLeft - 1), 250);
	}

	function tryInjectTransferButton(retriesLeft) {
		if (!isPosRoute()) return;
		if ($('.cart-totals-section .checkout-btn').first().length) {
			injectTransferButton();
			return;
		}
		if (retriesLeft > 0) setTimeout(() => tryInjectTransferButton(retriesLeft - 1), 250);
	}

	function setupDomObserver() {
		// The Payment screen DOM is rendered lazily when the cashier clicks
		// Checkout. Watch the POS app root for the .submit-order-btn arrival.
		// Throttle so a mutation storm doesn't pin a CPU; ignore mutations
		// whose target is our own injected button (otherwise our own DOM
		// writes re-trigger the observer in a loop and freeze the renderer).
		const root = document.querySelector('.point-of-sale-app') || document.body;
		if (root.__ffl4473ObsBound) return;
		root.__ffl4473ObsBound = true;
		let scheduled = false;
		const obs = new MutationObserver((mutations) => {
			for (const m of mutations) {
				if (m.target && m.target.closest && m.target.closest('.ffl-4473-row, .ffl-4473-btn, .ffl-4473-refresh-btn, .ffl-transfer-btn')) {
					return;
				}
			}
			if (scheduled) return;
			scheduled = true;
			requestAnimationFrame(() => {
				scheduled = false;
				const $submit = $('.submit-order-btn').first();
				if ($submit.length && !$submit.parent().find('.ffl-4473-btn').length) {
					inject4473Button($submit.parent());
				}
				injectTransferButton();
				refresh4473ButtonStyle();
			});
		});
		obs.observe(root, { childList: true, subtree: true });
		return obs;
	}

	function listenForDocUpdates() {
		if (!frappe.realtime || frappe.realtime.__ffl4473Bound) return;
		frappe.realtime.on('doc_update', (evt) => {
			try {
				const frm = curFrm();
				if (!frm || !frm.doc) return;
				if ((evt.doctype === 'POS Invoice' || evt.doctype === 'Sales Invoice')
					&& evt.name === frm.doc.name) {
					frm.reload_doc().then(() => refresh4473ButtonStyle());
				}
			} catch (e) { /* ignore */ }
		});
		frappe.realtime.__ffl4473Bound = true;
	}

	function init() {
		if (!window.frappe) return;
		const trigger = () => {
			if (!isPosRoute()) return;
			tryInjectButton(40);
			tryInjectTransferButton(40);
			setupDomObserver();
			listenForDocUpdates();
		};
		if (frappe.router) frappe.router.on('change', trigger);
		trigger();
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', init);
	} else {
		init();
	}

	// Item form: render the Serial Numbers tab when the Item is a firearm.
	// Lists every Serial No tied to this item, with the FastBound item id so
	// the dealer can cross-reference the bound book.
	if (window.frappe && frappe.ui && frappe.ui.form && frappe.ui.form.on) {
		frappe.ui.form.on('Item', {
			refresh(frm) {
				// Hide the "Advanced" tab from the nav — but first lift the
				// per-unit serial controls (has_serial_no + serial_no_series)
				// up next to "Maintain Stock" on the main tab, so New Listing
				// can mark an item serialized without surfacing the rest of the
				// Advanced fields (valuation_method, batch settings, etc.).
				// Those remaining fields stay readable/writeable via API.
				// Done in JS rather than a Property Setter because Frappe only
				// honours `insert_after` for custom fields (not standard ones),
				// and hiding the inventory_section Tab Break would cascade
				// hidden=true to every subsequent Tab Break.
				// Tabs render slightly after refresh — retry briefly until
				// the nav appears.
				const surfaceSerialFields = () => {
					const stock = frm.fields_dict.is_stock_item;
					const serial = frm.fields_dict.has_serial_no;
					const series = frm.fields_dict.serial_no_series;
					if (!stock || !serial || !stock.$wrapper || !serial.$wrapper) return;
					// Already lifted? Avoid re-moving on every refresh tick.
					if (serial.$wrapper.prev().is(stock.$wrapper)) return;
					stock.$wrapper.after(serial.$wrapper);
					if (series && series.$wrapper) serial.$wrapper.after(series.$wrapper);
				};
				const hideAdv = (tries) => {
					const $advTab = frm.$wrapper.find('.nav-item').filter(function () {
						return $(this).find('.nav-link').text().trim() === 'Advanced';
					});
					if ($advTab.length) {
						surfaceSerialFields();
						// Bootstrap's `.show` outranks `.hide` in this stylesheet,
						// so toggle both classes (and set display directly as a
						// belt-and-suspenders against future Frappe refresh that
						// re-adds .show).
						$advTab.removeClass('show').addClass('hide').css('display', 'none');
					} else if (tries > 0) {
						setTimeout(() => hideAdv(tries - 1), 150);
					}
				};
				hideAdv(10);

				if (!frm.doc.is_firearm) return;
				const $wrap = frm.fields_dict.firearm_serials_html
					&& frm.fields_dict.firearm_serials_html.$wrapper;
				if (!$wrap) return;
				// The serials table is wide. Its custom HTML field sits in a
				// half-width (col-sm-6) form-column inside a flex section, so on
				// wide viewports the table overflows under the ADJACENT column —
				// which, being position:relative, paints on top and swallows real
				// mouse clicks on the right-hand cells (incl. the Service Needed
				// pill: clicks land on the empty neighbour column instead). Narrow
				// screens stack the columns so it only bites on wide displays —
				// hence "works in dev, not prod". Force this field's column to span
				// the full row so the table owns its space and nothing overlays it.
				$wrap.closest('.form-column').css({ flex: '1 0 100%', maxWidth: '100%' });
				$wrap.html('<em>Loading serial numbers…</em>');
				// `frm._ffl_show_all_serials` is a transient form-level flag set
				// by the "Show disposed" link at the bottom of the rendered
				// table. Default view shows only in-stock (Active) serials so
				// a disposed firearm visually disappears from the Item form
				// the moment FFL Disposition submit issues stock.
				const showAll = !!frm._ffl_show_all_serials;
				const filters = { item_code: frm.doc.name };
				if (!showAll) filters.status = 'Active';
				Promise.all([
					frappe.db.get_list('Serial No', {
						filters,
						fields: ['name', 'serial_no', 'warehouse', 'status', 'image',
							'fastbound_item_id', 'needs_service', 'purchase_rate', 'sell_price', 'condition_grade', 'creation',
							'woo_product_id'],
						order_by: 'creation desc',
						limit: 200,
					}),
					frappe.xcall('ffl_core.reservations.reserved_counter_serials',
						{ item_code: frm.doc.name }).catch(() => ({})),
					frappe.xcall('ffl_woo_sync.woocommerce.fulfillment.reserved_web_serials',
						{ item_code: frm.doc.name }).catch(() => ({})),
					// Third sales channel. Same shape, same .catch(() => ({})) —
					// this file must not gain a hard dependency on any backend app,
					// so an uninstalled ffl_integrations is an empty lane, not a
					// broken serials tab.
					frappe.xcall('ffl_integrations.gunbroker.fulfillment.reserved_gb_serials',
						{ item_code: frm.doc.name }).catch(() => ({})),
				]).then(([rows, counter, web, gb]) => {
					// One serial is held by at most one open order; on the rare
					// collision the counter source wins, then GunBroker, then web —
					// a GunBroker sale is the one that cannot be undone, so it
					// outranks a web hold when both are somehow set.
					const reserved = Object.assign({}, web || {}, gb || {}, counter || {});
					if (!rows || !rows.length) {
						const msg = showAll
							? __('No serial numbers registered yet. Receive one via the Receive Goods page or a Stock Entry with serial_no set.')
							: __('No in-stock serials. <a href="#" class="ffl-show-all-serials">Show disposed / inactive</a>');
						$wrap.html('<div class="text-muted">' + msg + '</div>');
						$wrap.off('click.ffl-show-all').on('click.ffl-show-all', '.ffl-show-all-serials', (e) => {
							e.preventDefault();
							frm._ffl_show_all_serials = true;
							frm.trigger('refresh');
						});
						return;
					}
					const esc = frappe.utils.escape_html;
					const fbBase = 'https://cloud.fastbound.com';
					// Uniform-pricing mode: read the Item-level flag + standard_rate
					// once, drive the Sell Price column off Item.standard_rate, and
					// disable the per-serial click-to-edit.
					const uniform = !!frm.doc.uniform_sell_price;
					const uniformPrice = parseFloat(frm.doc.standard_rate || 0);
					// Manager-only per-row "fix serial" action (server re-checks the role).
					const canCorrect = !!(window.gunstore
						&& window.gunstore.canCorrectSerial
						&& window.gunstore.canCorrectSerial());
					const lines = rows.map((r) => {
						const link = `/app/serial-no/${encodeURIComponent(r.name)}`;
						const fbShort = r.fastbound_item_id ? r.fastbound_item_id.slice(-12) : '';
						const fb = r.fastbound_item_id
							? `<a href="${fbBase}/Items/${encodeURIComponent(r.fastbound_item_id)}" target="_blank" rel="noopener" title="${esc(r.fastbound_item_id)}" style="font-family:var(--font-monospace,monospace);font-size:11px">…${esc(fbShort)}</a>`
							: `<span class="text-muted">${__('Not synced')}</span>`;
						const svc = r.needs_service
							? `<span class="indicator-pill orange ffl-svc-toggle" data-serial="${esc(r.name)}" data-current="1" style="cursor:pointer" title="${__('Click to mark as serviced')}">${__('Service Needed')}</span>`
							: `<span class="ffl-svc-toggle text-muted" data-serial="${esc(r.name)}" data-current="0" style="cursor:pointer;font-size:11px" title="${__('Click to flag for service')}">${__('— mark')}</span>`;
						const woo = r.woo_product_id
							? `<span class="indicator-pill green ffl-woo-pill" data-serial="${esc(r.name)}" data-woo-listed="1" style="cursor:pointer" title="${__('Click to delist from WooCommerce')}">${__('Listed')}</span>`
							: `<span class="ffl-woo-pill text-muted" data-serial="${esc(r.name)}" data-woo-listed="0" style="cursor:pointer;font-size:11px" title="${__('Click to list on WooCommerce')}">${__('— list')}</span>`;
						const costCell = `<span class="text-muted">${frappe.format(r.purchase_rate || 0, {fieldtype: 'Currency'})}</span>`;
						const sellCell = uniform
							? `<span title="${__('Uniform pricing — change Item Standard Selling Rate to update all serials')}">${frappe.format(uniformPrice, {fieldtype: 'Currency'})} <span style="font-size:10px;opacity:0.6">· ${__('uniform')}</span></span>`
							: `<span class="ffl-sell-edit" data-serial="${esc(r.name)}" data-current="${r.sell_price || 0}" style="cursor:pointer;border-bottom:1px dotted var(--text-muted)" title="${__('Click to edit')}">${frappe.format(r.sell_price || 0, {fieldtype: 'Currency'})}</span>`;
						const photo = r.image
							? `<a href="${link}"><img src="${esc(r.image)}" alt="" style="width:36px;height:36px;object-fit:cover;border-radius:3px;border:1px solid var(--border-color)"></a>`
							: `<a href="${link}" class="text-muted" title="${__('Add photo')}" style="text-decoration:none;font-size:15px">＋</a>`;
						// A serial reserved on an open order (counter POS draft, Create
						// Order SI, or Woo online) stays Active — flag it so staff don't
						// double-sell. resv carries a link + tooltip to the holding doc.
						const resv = reserved[r.serial_no] || reserved[r.name];
						const pendingPill = resv
							? ` <a href="${esc(resv.url)}" class="indicator-pill orange" title="${esc(resv.tooltip)}" style="text-decoration:none">${__('Pending')}</a>`
							: '';
						const correctAction = (canCorrect && r.status === 'Active')
							? ` <a href="#" class="ffl-correct-serial" data-serial="${esc(r.name)}" title="${__('Correct a mistyped serial')}" style="font-size:11px;white-space:nowrap">${__('✎ fix')}</a>`
							: '';
						return `<tr>
							<td style="text-align:center;width:48px">${photo}</td>
							<td style="white-space:nowrap"><a href="${link}">${esc(r.serial_no || r.name)}</a>${correctAction}</td>
							<td style="white-space:nowrap">${esc(r.warehouse || '')}</td>
							<td>${esc(r.status || '')}${pendingPill}</td>
							<td style="white-space:nowrap">${esc(r.condition_grade || '')}</td>
							<td style="text-align:right;white-space:nowrap">${costCell}</td>
							<td style="text-align:right;white-space:nowrap">${sellCell}</td>
							<td style="white-space:nowrap">${fb}</td>
							<td style="text-align:center;white-space:nowrap">${svc}</td>
							<td style="text-align:center;white-space:nowrap">${woo}</td>
						</tr>`;
					}).join('');
					const toggleLink = showAll
						? `<a href="#" class="ffl-show-active-only">${__('Show in-stock only')}</a>`
						: `<a href="#" class="ffl-show-all-serials">${__('Show disposed / inactive')}</a>`;
					$wrap.html(`
						<table class="table table-bordered ffl-firearm-serials-table" style="margin-top:8px">
							<thead><tr>
								<th>${__('Photo')}</th>
								<th>${__('Serial No')}</th>
								<th>${__('Warehouse')}</th>
								<th>${__('Status')}</th>
								<th>${__('Condition')}</th>
								<th style="text-align:right">${__('Cost')}</th>
								<th style="text-align:right">${__('Sell Price')}</th>
								<th>${__('FastBound Item Id')}</th>
								<th style="text-align:center">${__('Service Needed')}</th>
								<th style="text-align:center">${__('WooCommerce')}</th>
							</tr></thead>
							<tbody>${lines}</tbody>
						</table>
						<div class="text-muted" style="font-size:11px;margin-top:4px">
							${showAll ? __('Showing all serials (including disposed / inactive).')
								: __('Showing in-stock only.')} · ${toggleLink}
						</div>
					`);
					$wrap.off('click.ffl-show-all click.ffl-show-active')
						.on('click.ffl-show-all', '.ffl-show-all-serials', (e) => {
							e.preventDefault();
							frm._ffl_show_all_serials = true;
							frm.trigger('refresh');
						})
						.on('click.ffl-show-active', '.ffl-show-active-only', (e) => {
							e.preventDefault();
							frm._ffl_show_all_serials = false;
							frm.trigger('refresh');
						});
					// Click a pill to toggle Serial No.needs_service AND keep the
					// gunsmith ToDo queue in sync (create on set, close on clear).
					$wrap.off('click.ffl-svc').on('click.ffl-svc', '.ffl-svc-toggle', function (e) {
						e.preventDefault();
						const $a = $(this);
						// .attr() (not .data()): jQuery's .data() coerces a numeric-
						// looking value to a JS Number, so a numeric Serial No name
						// (e.g. "1393829") would be sent as an int and make the server
						// compare an integer against the varchar `name` column —
						// MySQL then errors (1292) on the first non-numeric serial.
						const serial = $a.attr('data-serial');
						const now = parseInt($a.data('current'), 10) ? 0 : 1;
						frappe.call({
							method: 'ffl_core.firearm.toggle_serial_service_need',
							args: { serial: serial, value: now },
						}).then(() => frm.trigger('refresh'));
					});
					// Click a WooCommerce pill to push (list) or delist the serial.
					// .attr() (not .data()) for the same numeric-serial-name reason above.
					$wrap.off('click.ffl-woo').on('click.ffl-woo', '.ffl-woo-pill', function (e) {
						e.preventDefault();
						const $a = $(this);
						const serial = $a.attr('data-serial');
						const listed = parseInt($a.attr('data-woo-listed'), 10);
						if (listed) {
							frappe.confirm(
								__('Delist {0} from WooCommerce (set to Draft + stock 0)?', [serial]),
								() => {
									frappe.call({
										method: 'ffl_woo_sync.woocommerce.client_api.delist_serial_now',
										args: { serial_no: serial },
										freeze: true,
										freeze_message: __('Delisting {0}…', [serial]),
									}).then(() => frm.trigger('refresh'));
								}
							);
						} else {
							frappe.confirm(
								__('List {0} on WooCommerce (publish product + set stock)?', [serial]),
								() => {
									frappe.call({
										method: 'ffl_woo_sync.woocommerce.client_api.push_serial_now',
										args: { serial_no: serial },
										freeze: true,
										freeze_message: __('Pushing {0} to WooCommerce…', [serial]),
									}).then(() => frm.trigger('refresh'));
								}
							);
						}
					});
					// Click "fix" to correct a mistyped serial (manager-only; the server
					// re-checks). Reuses the shared dialog so this row action and the
					// Serial No form button behave identically.
					$wrap.off('click.ffl-correct').on('click.ffl-correct', '.ffl-correct-serial', function (e) {
						e.preventDefault();
						const serial = $(this).attr('data-serial');
						if (window.gunstore && window.gunstore.correctSerialDialog) {
							window.gunstore.correctSerialDialog(serial, { onDone: () => frm.trigger('refresh') });
						}
					});
					// Click the sell-price cell to edit (prompt → save → re-render).
					$wrap.off('click.ffl-sell').on('click.ffl-sell', '.ffl-sell-edit', function (e) {
						e.preventDefault();
						const $a = $(this);
						// .attr() (not .data()): jQuery's .data() coerces a numeric-
						// looking value to a JS Number, so a numeric Serial No name
						// (e.g. "1393829") would be sent as an int and make the server
						// compare an integer against the varchar `name` column —
						// MySQL then errors (1292) on the first non-numeric serial.
						const serial = $a.attr('data-serial');
						const current = parseFloat($a.data('current')) || 0;
						frappe.prompt({
							label: __('Sell Price'),
							fieldname: 'sell_price',
							fieldtype: 'Currency',
							default: current,
							reqd: 1,
						}, (values) => {
							frappe.db.set_value('Serial No', serial, 'sell_price', values.sell_price)
								.then(() => frm.trigger('refresh'));
						}, __('Edit Sell Price for {0}', [serial]), __('Save'));
					});
				});
			},
		});
	}
})();

// === Per-gun pricing (uniform_sell_price=0) POS support =====================
// Used guns carry per-unit prices (condition differs gun to gun) and have NO
// Item Price record — three POS gaps close here, all fed by one TTL-cached
// fetcher over ffl_core.firearm.pos_pricing_summary:
//   1. ERPNext refuses to add a rate-0 line ("Price is not set for the item")
//      → seed the click with the cheapest in-stock serial price;
//   2. item cards show no price → paint the "$min – $max" range;
//   3. (consumed by the serial picker + checkout gate elsewhere in this bundle)
(function setupPosPerGunPricing() {
	const TTL_MS = 20000;
	const cache = {}; // item_code -> { ts, data }

	async function summaries(codes) {
		codes = [...new Set((codes || []).filter(Boolean))];
		const out = {};
		const missing = [];
		const now = Date.now();
		codes.forEach((c) => {
			const hit = cache[c];
			if (hit && now - hit.ts < TTL_MS) out[c] = hit.data;
			else missing.push(c);
		});
		if (missing.length) {
			try {
				const r = await frappe.call({
					method: 'ffl_core.firearm.pos_pricing_summary',
					args: { item_codes: missing },
				});
				const got = (r && r.message) || {};
				missing.forEach((c) => {
					cache[c] = { ts: Date.now(), data: got[c] };
					out[c] = got[c];
				});
			} catch (e) { /* unfetched codes stay undefined — callers fall back to native */ }
		}
		return out;
	}

	window.ffl = window.ffl || {};
	ffl.posPricingSummaries = summaries;
	ffl.posPricingSummary = async (code) => (await summaries([code]))[code];

	// --- gap 1: let a per-gun priced firearm into the cart, unassigned --------
	// Two native behaviors fight per-gun pricing on add:
	//   a) on_cart_update returns early (alert) when a NEW line arrives with
	//      rate 0/undefined — a per-gun item has no Item Price by design;
	//   b) get_item_details FIFO-auto-assigns a serial (Stock Settings
	//      auto_create_serial_and_batch_bundle_for_outward) WITHOUT its price —
	//      the line would carry a gun the clerk never chose, silently.
	// Seed the rate to defeat (a); after the row lands, clear the auto-assigned
	// serial and open the details panel so the clerk explicitly picks the gun
	// (the picker prices the line from the chosen serial).
	function patchOnCartUpdate() {
		const C = window.erpnext && erpnext.PointOfSale && erpnext.PointOfSale.Controller;
		if (!C || C.prototype.__fflRateSeedPatched) return;
		const orig = C.prototype.on_cart_update;
		C.prototype.on_cart_update = async function (args) {
			let perGunNewItem = false;
			try {
				const it = args && args.item;
				if (it && it.item_code && $.isEmptyObject(this.get_item_from_frm(it))) {
					const s = await ffl.posPricingSummary(it.item_code);
					if (s && s.is_firearm && !s.uniform && s.n > 0) {
						perGunNewItem = true;
						if (it.rate == undefined || flt(it.rate) == 0) {
							it.rate = s.min > 0 ? s.min : 0.01;
						}
					}
				}
			} catch (e) { /* fall through — the native alert is the worst case */ }
			const row = await orig.call(this, args);
			try {
				if (perGunNewItem && row && row.name) {
					if (row.serial_no) {
						await frappe.model.set_value(row.doctype, row.name, 'serial_no', '');
					}
					// POS Invoice Item has no has_serial_no docfield, and the details
					// panel renders the serial_no control only when the row claims one
					// (item.has_serial_no || item.serial_no) — set it locally so the
					// picker has a control to mount on.
					row.has_serial_no = 1;
					this.update_cart_html(row);
					if (!this.item_details.$component.is(':visible')) this.edit_item_details_of(row);
				}
			} catch (e) { /* row keeps the auto serial — picker still lets the clerk change it */ }
			return row;
		};
		C.prototype.__fflRateSeedPatched = true;
	}
	(function tryPatchCart(retriesLeft) {
		if (window.erpnext && erpnext.PointOfSale && erpnext.PointOfSale.Controller)
			return patchOnCartUpdate();
		if (retriesLeft > 0) setTimeout(() => tryPatchCart(retriesLeft - 1), 250);
	})(40);

	// --- gap 2: card price ranges ---------------------------------------------
	function isPosRoute() {
		return (frappe.get_route() || [])[0] === 'point-of-sale';
	}
	function fmt(v) {
		// format_currency returns a plain string (frappe.format wraps Currency
		// in a right-aligned div — unusable as text).
		const cur = (window.cur_pos && cur_pos.frm && cur_pos.frm.doc && cur_pos.frm.doc.currency)
			|| frappe.defaults.get_default('currency');
		return format_currency(v, cur);
	}
	function patchCards() {
		if (!isPosRoute()) return;
		const $cards = $('.items-selector .item-wrapper:not([data-ffl-priced])');
		if (!$cards.length) return;
		// Mark BEFORE the async fetch so a render mid-flight can't loop; a
		// re-render rebuilds the DOM (marker gone) and re-triggers us anyway.
		$cards.attr('data-ffl-priced', '1');
		const codes = [...new Set($cards.map(function () {
			return unescape($(this).attr('data-item-code') || '');
		}).get().filter(Boolean))];
		if (!codes.length) return;
		summaries(codes).then((summ) => {
			$('.items-selector .item-wrapper[data-ffl-priced]').each(function () {
				const $c = $(this);
				const s = summ[unescape($c.attr('data-item-code') || '')];
				// Only per-gun priced firearms with stock get painted; uniform /
				// non-firearm cards keep their (now Item-Price-correct) display.
				if (!s || !s.is_firearm || s.uniform || !s.n) return;
				if (s.min > 0) $c.attr('data-rate', s.min); // honest seed for the click
				const label = s.min == null
					? __('unpriced')
					: (s.min === s.max ? fmt(s.min) : fmt(s.min) + ' – ' + fmt(s.max));
				$c.find('.item-rate, .item-price').first().text(label);
			});
		});
	}
	(function startCardObserver() {
		let scheduled = false;
		const observer = new MutationObserver(function () {
			if (scheduled) return;
			scheduled = true;
			window.requestAnimationFrame(function () {
				scheduled = false;
				try { patchCards(); } catch (e) {}
			});
		});
		function go() {
			observer.observe(document.body, { childList: true, subtree: true });
			try { patchCards(); } catch (e) {}
		}
		if (document.readyState !== 'loading') go();
		else document.addEventListener('DOMContentLoaded', go);
	})();
})();

// === In-stock serial picker for ERPNext's POS Item Details panel ============
// Adds a dropdown of the item's on-hand serials above the native serial_no
// field so the cashier picks a real in-stock serial instead of free-typing one
// (a typo would sell a unit not in stock + break the 4473 webhook serial match).
// Drives ERPNext's own control (set_value + form_updated) so the cart row syncs;
// the native field is left in place as a fallback. Defensive + idempotent.
(function setupItemDetailsSerialPicker() {
  const TTL_MS = 20000; // re-fetch serials at most every 20s (stock shifts mid-session)
  const cache = {}; // item_code -> { ts, serials, inflight }

  function isPosRoute() {
    return (frappe.get_route() || [])[0] === 'point-of-sale';
  }

  function inject($ctrl, serials, summary) {
    const idet = window.cur_pos && cur_pos.item_details;
    if (!idet || !idet.serial_no_control || !$ctrl || !$ctrl.length) return;
    if ($ctrl.find('.ffl-serial-select').length) return; // already injected
    const current = (idet.serial_no_control.get_value && idet.serial_no_control.get_value()) || '';
    // serials is now [{serial, sell_price, purchase_rate}, ...] sorted by
    // sell_price ascending (cheapest first).
    const priceFor = {};
    serials.forEach(function (s) { priceFor[s.serial] = s.sell_price; });
    const opts = ['<option value="">' + __('— pick in-stock serial —') + '</option>']
      .concat(serials.map(function (s) {
        const priceLabel = s.sell_price != null
          ? ' — ' + frappe.format(s.sell_price, {fieldtype: 'Currency'})
          : '';
        return '<option value="' + frappe.utils.escape_html(s.serial) + '"' +
          (s.serial === current ? ' selected' : '') + '>' +
          frappe.utils.escape_html(s.serial) + priceLabel + '</option>';
      })).join('');
    const $sel = $('<select class="ffl-serial-select form-control" style="margin-bottom:6px"></select>').html(opts);
    function applySerial(v) {
      try {
        idet.serial_no_control.set_value(v);
        if (idet.events && idet.events.form_updated) {
          idet.events.form_updated(idet.current_item, 'serial_no', v);
          // Pull the cart line's rate to the chosen serial's per-gun price.
          // Gate on `> 0`, not `!= null` — a uniform-pricing item whose
          // Item.standard_rate is 0/unset, or a Serial No that legitimately
          // has sell_price=0 (free sample), must NOT silently zero the
          // cashier's cart line. Treat 0 / missing as "no price, leave it".
          const price = priceFor[v];
          if (v && price > 0) {
            idet.events.form_updated(idet.current_item, 'rate', price);
          }
        }
      } catch (e) { /* native field remains as fallback */ }
    }
    $sel.on('change', function () { applySerial($sel.val()); });
    $ctrl.prepend($sel);
    // Auto-pick only when the price can't differ: uniform-priced items (every
    // serial charges standard_rate) or a single unit in stock. Per-gun priced
    // items with 2+ units need the clerk to pick the PHYSICAL gun — condition
    // varies and the serial sets the price. Missing summary (fetch failure)
    // falls to the safe side: explicit pick.
    const autoPick = !current && serials.length
      && ((summary && summary.uniform) || serials.length === 1);
    if (autoPick) {
      $sel.val(serials[0].serial);
      applySerial(serials[0].serial);
    } else if (!current && serials.length) {
      $sel.css({ borderColor: 'var(--orange-500, #f57c00)', boxShadow: '0 0 0 1px #f57c00' });
      $sel.before(
        $('<div class="ffl-pick-hint" style="font-size:11px;color:var(--orange-500, #f57c00);margin-bottom:2px"></div>')
          .text(__('Per-gun pricing — pick the serial to price this line')));
    }
  }

  function tryInject() {
    if (!isPosRoute()) return;
    const idet = window.cur_pos && cur_pos.item_details;
    // Cheap gates first — bail before any jQuery work unless the panel is open.
    if (!idet || !idet.current_item || !idet.serial_no_control || !idet.$component) return;
    if (!idet.$component.is(':visible')) return;
    const code = idet.current_item.item_code;
    if (!code) return;
    const $ctrl = idet.$form_container && idet.$form_container.find('.serial_no-control');
    if (!$ctrl || !$ctrl.length || $ctrl.find('.ffl-serial-select').length) return;

    // Cache + fetch per (code, invoice): the current-invoice self-hold exemption
    // makes availability invoice-specific, so a
    // bare code key could serve another cart's list.
    const invName = (window.cur_pos && cur_pos.frm && cur_pos.frm.doc && cur_pos.frm.doc.name) || '';
    const cacheKey = code + '|' + invName;
    const hit = cache[cacheKey];
    if (hit && hit.inflight) return;
    if (hit && (Date.now() - hit.ts) < TTL_MS) {
      if (hit.serials.length) inject($ctrl, hit.serials, hit.summary);
      return;
    }
    cache[cacheKey] = { ts: Date.now(), serials: (hit && hit.serials) || [], summary: hit && hit.summary, inflight: true };
    Promise.all([
      frappe.call({
        method: 'ffl_core.firearm.available_serials_with_prices',
        // current_invoice → the serial on this invoice's own line stays offered.
        args: { item_codes: [code], current_invoice: invName || undefined },
      }),
      // uniform-vs-per-gun flag for the auto-pick decision; tolerate absence
      (window.ffl && ffl.posPricingSummaries) ? ffl.posPricingSummaries([code]) : Promise.resolve({}),
    ]).then(function (res) {
      const serials = (((res[0] && res[0].message) || {})[code]) || [];
      const summary = (res[1] || {})[code] || null;
      cache[cacheKey] = { ts: Date.now(), serials: serials, summary: summary, inflight: false };
      const live = window.cur_pos && cur_pos.item_details;
      if (!serials.length || !live || !live.current_item || live.current_item.item_code !== code) return;
      const $c = live.$form_container && live.$form_container.find('.serial_no-control');
      if ($c && $c.length) inject($c, serials, summary);
    }).catch(function () { delete cache[cacheKey]; }); // let a failed fetch retry
  }

  function start() {
    let scheduled = false;
    const observer = new MutationObserver(function () {
      if (scheduled) return;
      scheduled = true;
      window.requestAnimationFrame(function () {
        scheduled = false;
        try { tryInject(); } catch (e) {}
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
    try { tryInject(); } catch (e) {}
  }
  if (document.readyState !== 'loading') start();
  else document.addEventListener('DOMContentLoaded', start);
})();


// === In-store storage location for a non-serialized POS line =================
// A sale line says which location its units were taken from; when the consolidated
// invoice posts, they leave that location (ffl_core/storage.py). The cart picks it
// itself when the item is only in one place; with several places the item-details
// panel gets a "Taken from" dropdown. Never blocks the sale: with nothing chosen the
// units simply show up on the Storage Locations "Not placed" tab to be confirmed.
(function setupPosStorageLocationPicker() {
  const TTL_MS = 20000;
  const cache = {}; // item_code -> { ts, info, inflight }

  function isPosRoute() {
    return (frappe.get_route() || [])[0] === 'point-of-sale';
  }

  // {locations: [{location, label, qty}], unassigned, total} for one item; null when the
  // lookup fails (the picker then stays out of the way).
  async function locationsFor(code) {
    const hit = cache[code];
    if (hit && hit.info && Date.now() - hit.ts < TTL_MS) return hit.info;
    try {
      const r = await frappe.call({
        method: 'ffl_core.api.storage.stock_locations',
        args: { item_codes: [code] },
        freeze: false, no_spinner: true,
      });
      const info = ((r && r.message) || {})[code] || null;
      cache[code] = { ts: Date.now(), info };
      return info;
    } catch (e) { return null; }
  }

  // The one place an item can only have come from, or '' when it needs a choice. The
  // rule lives on the server (storage.sole_location), shared with Create Order and Dispose.
  function onlyPlace(info) {
    return (info && info.sole) || '';
  }

  // --- auto-pick when the cart adds a line ---------------------------------
  function patchCartForLocations() {
    const C = window.erpnext && erpnext.PointOfSale && erpnext.PointOfSale.Controller;
    if (!C || C.prototype.__fflLocationPatched) return;
    const orig = C.prototype.on_cart_update;
    C.prototype.on_cart_update = async function (args) {
      // Only a line that is NEW to the cart is preselected: a cashier who clears the
      // location on an existing line meant it, and a later qty tap must not put it back.
      let isNew = false;
      try { isNew = $.isEmptyObject(this.get_item_from_frm((args || {}).item)); } catch (e) { /* not new */ }
      const row = await orig.call(this, args);
      try {
        if (isNew && row && row.name && row.item_code && !row.ffl_storage_location && !row.serial_no
            && frappe.meta.has_field(row.doctype, 'ffl_storage_location')) {
          const only = onlyPlace(await locationsFor(row.item_code));
          if (only) await frappe.model.set_value(row.doctype, row.name, 'ffl_storage_location', only);
        }
      } catch (e) { /* tracking only: the line is in the cart either way */ }
      return row;
    };
    C.prototype.__fflLocationPatched = true;
  }
  // The POS controller class only exists once the POS page's code has loaded, which can be
  // long after this bundle (the cashier opens POS after other pages). So try now, and again
  // every time the route changes to POS (a few quick retries while the page loads).
  function tryPatch(retriesLeft) {
    if (window.erpnext && erpnext.PointOfSale && erpnext.PointOfSale.Controller) return patchCartForLocations();
    if (retriesLeft > 0) setTimeout(() => tryPatch(retriesLeft - 1), 250);
  }
  tryPatch(40);
  if (frappe.router && frappe.router.on) {
    frappe.router.on('change', () => {
      if (isPosRoute()) tryPatch(40);
    });
  }

  // --- the dropdown in the item-details panel -------------------------------
  function inject(idet, row, info) {
    const $form = idet.$form_container;
    if (!$form || !$form.length) return;
    $form.find('.ffl-loc-pick').remove();
    const current = row.ffl_storage_location || '';
    const opts = ['<option value="">' + __('— pick where it came from —') + '</option>']
      .concat(info.locations.map((l) => '<option value="' + frappe.utils.escape_html(l.location) + '"'
        + (l.location === current ? ' selected' : '') + '>'
        + frappe.utils.escape_html(l.label) + ' (' + flt(l.qty) + ')</option>'));
    if (flt(info.unassigned) > 0) {
      opts.push('<option value="" data-none="1">' + __('Not in a location') + ' (' + flt(info.unassigned) + ')</option>');
    }
    const $box = $('<div class="ffl-loc-pick" data-row="' + frappe.utils.escape_html(row.name)
      + '" style="grid-column:1 / -1; margin-bottom:8px"></div>');
    $box.append($('<label class="control-label" style="font-size:11px"></label>').text(__('Taken from')));
    const $sel = $('<select class="form-control"></select>').html(opts.join(''));
    if (!current && info.locations.length) {
      $sel.css({ borderColor: 'var(--orange-500, #f57c00)' });
    }
    $sel.on('change', function () {
      const v = $sel.val() || '';
      try {
        idet.events.form_updated(idet.current_item, 'ffl_storage_location', v);
        $sel.css({ borderColor: '' });
      } catch (e) { /* the cart row keeps what it had */ }
    });
    $box.append($sel);
    $form.prepend($box);
  }

  function tryInject() {
    if (!isPosRoute()) return;
    const idet = window.cur_pos && cur_pos.item_details;
    if (!idet || !idet.current_item || !idet.$component || !idet.$form_container) return;
    if (!idet.$component.is(':visible')) return;
    const row = idet.current_item;
    if (!row.item_code || row.serial_no || !frappe.meta.has_field(row.doctype, 'ffl_storage_location')) return;
    const $have = idet.$form_container.find('.ffl-loc-pick');
    if ($have.length && $have.attr('data-row') === row.name) return;
    locationsFor(row.item_code).then((info) => {
      const live = window.cur_pos && cur_pos.item_details;
      if (!info || !info.locations.length || !live || live.current_item !== row) return;
      const $now = live.$form_container.find('.ffl-loc-pick');
      if ($now.length && $now.attr('data-row') === row.name) return;
      inject(live, row, info);
    });
  }

  function start() {
    let scheduled = false;
    const observer = new MutationObserver(function () {
      if (scheduled) return;
      scheduled = true;
      window.requestAnimationFrame(function () {
        scheduled = false;
        try { tryInject(); } catch (e) {}
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState !== 'loading') start();
  else document.addEventListener('DOMContentLoaded', start);
})();


// --- POS Customer-Facing Display (sender) ----------------------------------
//
// Opens a same-origin popup (the customer display page) and drives it over a
// BroadcastChannel by POLLING cur_pos.frm.doc — deliberately NOT hooking the
// POS controller, so ERPNext upgrades that rename controller methods can't
// break us (same philosophy as the Trade-in button above). Exposed as
// window.fflOpenCustomerDisplay so the toolbar button (in the POS IIFE) can
// call across IIFE scopes, mirroring window.gunStoreForceDeleteItems.
//
// No route-gate (unlike the other POS hooks): the loop self-terminates on
// win.closed, and off the POS route cur_pos.frm.doc is empty, so a tick just
// yields a harmless "welcome". The display is only ever started by the POS
// toolbar button, so it can't run before a POS session exists.

(function () {
	'use strict';

	var CHANNEL = 'ffl-pos-customer-display';
	var DISPLAY_URL = '/assets/ffl_core/customer_display/index.html';
	var POLL_MS = 300;

	var bc = null;          // BroadcastChannel
	var win = null;         // the popup window handle
	var pollTimer = null;
	var lastJson = null;    // last broadcast payload (string) for change detection
	var lastPayload = null; // last broadcast payload (object) for replay
	var prevStage = null;   // last emitted stage, fed back into deriveStage
	var branding = null;    // { name, logo }, fetched once

	function channel() {
		if (!bc) {
			bc = new BroadcastChannel(CHANNEL);
			bc.onmessage = function (e) {
				if (e.data && e.data.type === 'ready') replay();
			};
		}
		return bc;
	}

	// The desk's own desk.bundle.css <link> — hash-correct for the running
	// version, so the popup styles match the desk across upgrades.
	function deskCssHref() {
		var l = document.querySelector('link[href*="desk.bundle"][href$=".css"]')
			|| document.querySelector('link[href*="/css/desk"]');
		return l ? l.href : '';
	}

	// Branding message carries store name/logo PLUS the desk CSS href so the popup
	// renders in the desk's look (read live, not cached, so a rebuilt desk bundle's
	// new hash is picked up). The popup intentionally forces a light theme (a
	// customer-facing screen stays white regardless of the cashier's dark mode),
	// so we do NOT send the desk's data-theme.
	function brandingMsg() {
		return {
			type: 'branding',
			name: branding ? branding.name : '',
			logo: branding ? branding.logo : '',
			deskCss: deskCssHref(),
		};
	}

	// Re-send branding + current snapshot, e.g. when a freshly-opened or
	// refreshed display window announces itself with {type:'ready'}.
	function replay() {
		var ch = channel();
		ch.postMessage(brandingMsg());
		if (lastPayload) ch.postMessage(lastPayload);
	}

	function fetchBranding() {
		if (branding) { replay(); return; }
		var doc = (window.cur_pos && cur_pos.frm && cur_pos.frm.doc) || {};
		var company = doc.company || (frappe.defaults && frappe.defaults.get_default('company'));
		if (!company) { branding = { name: '', logo: '' }; replay(); return; }
		frappe.db.get_value('Company', company, ['company_name', 'company_logo']).then(function (r) {
			var m = (r && r.message) || {};
			branding = { name: m.company_name || company, logo: m.company_logo || '' };
			replay();
		});
	}

	function tick() {
		if (!win || win.closed) { stop(); return; }
		var doc = (window.cur_pos && cur_pos.frm && cur_pos.frm.doc) || {};
		var snap = window.FFLCustomerDisplay.buildSnapshot(doc);
		var stage = window.FFLCustomerDisplay.deriveStage(prevStage, snap);
		var payload = {
			type: 'snapshot', stage: stage,
			items: snap.items, totals: snap.totals, payment: snap.payment,
		};
		var json = JSON.stringify(payload);
		if (json !== lastJson) {
			channel().postMessage(payload);
			lastJson = json; lastPayload = payload; prevStage = stage;
		}
	}

	function start() {
		stop();
		lastJson = null;   // force a broadcast on the first tick after (re)opening
		prevStage = null;  // a reopen is a fresh session — don't inherit the prior
		                   // sale's terminal stage, or an empty cart after a sale
		                   // would greet the NEXT customer with "Thank you!".
		pollTimer = setInterval(tick, POLL_MS);
		tick();
	}

	function stop() {
		if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
	}

	window.fflOpenCustomerDisplay = function () {
		if (!window.FFLCustomerDisplay) {
			frappe.show_alert({ message: __('Customer display module not loaded — run bench build.'), indicator: 'red' });
			return;
		}
		// Cache-bust: the display page + its css/js are static assets served with
		// a 12h cache, so a rebuilt display would otherwise load stale. The page
		// propagates this ?v= to its own css/js (see index.html).
		win = window.open(DISPLAY_URL + '?v=' + Date.now(), 'ffl-customer-display');
		if (!win) {
			frappe.show_alert({
				message: __('Popup blocked. Allow popups for this site, then click Customer Display again.'),
				indicator: 'orange',
			});
			return;
		}
		win.focus();
		channel();
		fetchBranding();
		start();
	};
})();

// ===========================================================================
// Negative ticket — the trade-ins exceed the goods, so the STORE pays the
// customer. The payment screen has nothing to tender: the tender tiles, the
// number pad and the loyalty tile give way to "Store pays customer $X", a
// payout-method picker (Cash preselected) and a reference box (every method but
// Cash). The choice is written onto the invoice (ffl_trade_in_payout_method /
// _reference); the server books the payout at submit (cash_drawer.
// record_trade_in_payout) and refuses a non-zero tender on such a ticket. What
// is valid is decided by the pure kernel in pos_tender_guard.js
// (negativeTicketState / negativeTicketFix); this block only draws and writes.
// Hooked from the Payment-class patch (render_payment_mode_dom /
// update_totals_section) below — one idempotent wrap, same style as the
// card-charge-now tile.
(function fflNegativeTicketScreen() {
	'use strict';
	if (!window.frappe) return;
	window.ffl = window.ffl || {};
	const kernel = () => window.ffl;
	const esc = (v) => frappe.utils.escape_html(String(v == null ? '' : v));

	// Why Complete is blocked — keyed by the kernel's `problem`, as __() strings
	// here so Frappe's extractor sees them.
	window.ffl.negativeTicketReason = function (ng) {
		switch (ng && ng.problem) {
		case 'card':
			return __('A card was charged on this order. A manager has to void it before the store can pay the customer.');
		case 'method':
			return __('Pick how the customer is paid.');
		case 'reference':
			return __('Enter the {0} reference (check number or confirmation number) to complete.', [ng.method]);
		default:
			return '';
		}
	};

	// ERPNext's Complete Order click refuses "no payment" unless the POS Profile
	// allows partial payment; a negative ticket has none by design. Flip the
	// instance flag only while the ticket is negative and put it back after.
	function allowNoTender(payment, on) {
		if (on) {
			if (payment.__fflAllowPartial === undefined) payment.__fflAllowPartial = payment.allow_partial_payment;
			payment.allow_partial_payment = 1;
		} else if (payment.__fflAllowPartial !== undefined) {
			payment.allow_partial_payment = payment.__fflAllowPartial;
			delete payment.__fflAllowPartial;
		}
	}

	// Keep the primary button honest: quiet while the ticket is not valid yet,
	// and the hint under the picker says why. Still clickable — the submit
	// wrapper repeats the check and tells the cashier.
	function paintGate(payment, ng) {
		payment.$component.find('.submit-order-btn').toggleClass('ffl-submit-blocked', !ng.ok);
		payment.$payment_modes.find('.ffl-payout-hint')
			.text(ng.ok ? '' : window.ffl.negativeTicketReason(ng));
	}

	// The selected method carries ERPNext's own `border-primary` marker: its Ctrl+Enter shortcut completes the order
	// only while a tile is highlighted, and on this screen the picked method is the highlighted one — so Ctrl+Enter
	// on a valid negative ticket does what a click on Complete does (the submit wrapper still runs the checks).
	function panel(ng, currency) {
		const methods = kernel().PAYOUT_METHODS.map((m) =>
			`<div class="ffl-payout-btn${ng.method === m ? ' ffl-payout-on border-primary' : ''}" data-method="${esc(m)}" role="button">${esc(__(m))}</div>`
		).join('');
		const ref = ng.needsReference
			? `<div class="ffl-payout-label text-muted">${esc(__('{0} reference (required)', [__(ng.method)]))}</div>
			   <input type="text" class="form-control ffl-payout-ref" maxlength="140" autocomplete="off"
				placeholder="${esc(ng.method === 'Check' ? __('Check number') : __('Confirmation / reference number'))}"
				value="${esc(ng.reference)}">`
			: '';
		return `<div class="ffl-payout">
			<div class="ffl-payout-head">${esc(__('Store pays customer'))} ${esc(format_currency(ng.due, currency))}</div>
			<div class="ffl-payout-label text-muted">${esc(__('Paid by'))}</div>
			<div class="ffl-payout-methods">${methods}</div>
			${ref}
			<div class="ffl-payout-hint text-danger"></div>
		</div>`;
	}

	function writeFields(doc, fields) {
		return Promise.all(Object.keys(fields).map((f) =>
			frappe.model.set_value(doc.doctype, doc.name, f, fields[f])));
	}

	function bind(payment) {
		const $m = payment.$payment_modes;
		const frm = () => payment.events.get_frm();
		$m.off('.fflneg');
		$m.on('click.fflneg', '.ffl-payout-btn', function () {
			const doc = frm().doc;
			writeFields(doc, kernel().payoutMethodFields(doc, $(this).attr('data-method')))
				.then(() => payment.render_payment_mode_dom());
		});
		// Typing here must never reach ERPNext's document-level keydown, which
		// types digits into the selected tender row.
		$m.on('keydown.fflneg', '.ffl-payout-ref', (e) => e.stopPropagation());
		$m.on('input.fflneg', '.ffl-payout-ref', function () {
			const doc = frm().doc;
			writeFields(doc, { ffl_trade_in_payout_reference: $(this).val() });
			paintGate(payment, kernel().negativeTicketState(
				Object.assign({}, doc, { ffl_trade_in_payout_reference: $(this).val() })));
		});
	}

	// Called from render_payment_mode_dom. True = the payout screen was drawn
	// (the caller skips ERPNext's tender tiles); false = an ordinary ticket.
	window.ffl.renderNegativeTicket = function (payment) {
		const k = kernel();
		if (!k.negativeTicketState) return false;
		const frm = payment.events.get_frm();
		const doc = frm.doc;
		const fix = k.negativeTicketFix(doc);
		if (fix) {
			// Not yet on the doc if set_value is async: judge the merged view.
			writeFields(doc, fix.set);
			(doc.payments || []).filter((r) => fix.zeroRows.indexOf(r.name) !== -1)
				.forEach((r) => frappe.model.set_value(r.doctype, r.name, 'amount', 0));
		}
		const ng = k.negativeTicketState(Object.assign({}, doc, fix ? fix.set : {}));
		allowNoTender(payment, ng.negative);
		if (!ng.negative) {
			payment.$numpad.css('display', '');
			payment.$component.find('.submit-order-btn').removeClass('ffl-submit-blocked');
			return false;
		}
		if (!payment.$payment_modes.is(':visible')) return true;   // same early-out as ERPNext's
		payment.selected_mode = '';   // a stale tender tile must not catch the keyboard
		payment.$numpad.css('display', 'none');
		payment.$payment_modes.html(panel(ng, doc.currency));
		bind(payment);
		paintGate(payment, ng);
		return true;
	};

	// Order summary after the sale (Recent Orders / right after Complete): ERPNext shows paid_amount, which is 0 by
	// design on a negative ticket, with an empty Payments list. Show what the store paid the customer instead —
	// the same wording as the receipt ("Paid to customer (method, reference)").
	window.ffl.applyNegativeSummaryPatch = function (S) {
		if (!S || S.prototype.__fflNegativeSummaryPatched) return false;
		const upper = S.prototype.get_upper_section_html;
		const payments = S.prototype.attach_payments_info;
		if (typeof upper !== 'function' || typeof payments !== 'function') return false;
		S.prototype.get_upper_section_html = function fflNegUpperSection(doc) {
			let html = upper.apply(this, arguments);
			try {
				const p = kernel().payoutSummary(doc);
				if (p) {
					html = html.replace(/(<div class="paid-amount">)[^<]*(<\/div>)/,
						(_m, a, b) => `${a}${esc(__('Paid out {0}', [format_currency(p.due, doc.currency)]))}${b}`);
				}
			} catch (e) { /* the stock summary stands */ }
			return html;
		};
		S.prototype.attach_payments_info = function fflNegPayments(doc) {
			const out = payments.apply(this, arguments);
			try {
				const p = kernel().payoutSummary(doc);
				if (p) {
					const label = __('Paid to customer') + (p.detail ? ` (${p.detail})` : '');
					this.$payment_container.append(this.get_payment_html(doc, { mode_of_payment: label, amount: p.due }));
				}
			} catch (e) { /* the stock summary stands */ }
			return out;
		};
		S.prototype.__fflNegativeSummaryPatched = true;
		return true;
	};

	// Trap ERPNext's PastOrderSummary the moment it is defined (installClassHook is in pos_tender_guard.js, which
	// loads after this bundle — so wait for it, boundedly).
	(function wireSummary(retries) {
		if (!window.frappe || typeof frappe.provide !== 'function') return;
		const hook = window.ffl && window.ffl.installClassHook;
		if (!hook) { if (retries > 0) setTimeout(() => wireSummary(retries - 1), 250); return; }
		frappe.provide('erpnext.PointOfSale');
		hook(erpnext.PointOfSale, 'PastOrderSummary', window.ffl.applyNegativeSummaryPatch);
	})(60);

	// Called after update_totals_section: Grand Total (negative) | Store pays.
	window.ffl.renderNegativeTotals = function (payment) {
		const doc = payment.events.get_frm().doc;
		const ng = kernel().negativeTicketState(doc);
		if (!ng.negative || !payment.$totals.is(':visible')) return;
		payment.$totals.html(
			`<div class="col"><div class="total-label">${esc(__('Grand Total'))}</div>
				<div class="value">${esc(format_currency(doc.grand_total, doc.currency))}</div></div>
			<div class="seperator-y"></div>
			<div class="col"><div class="total-label">${esc(__('Store pays customer'))}</div>
				<div class="value text-success">${esc(format_currency(ng.due, doc.currency))}</div></div>`);
	};
})();

// ===========================================================================
// P2 — Recent Orders → Edit must preserve a held order's tender.
//
// ERPNext's PastOrderSummary "Edit" fires controller events.edit_order, which
// runs `frm.call("reset_mode_of_payments")` — that rebuilds `payments` from the
// POS Profile and zeroes paid_amount. Fine for an unpaid draft, but a firearm
// order is HELD (already charged) while the 4473/NICS clears; resetting silently
// drops the recorded card/Zelle split (the saved
// 855.17+584.55 split was replaced by a profile-default 1,439.72 card row,
// tripping the false "Charged amount mismatch"). We re-decorate events.edit_order
// to skip the reset when hasLiveTender(frm.doc) is true.
//
// UPGRADE-SAFE: same Controller-prototype monkey-patch style as the payroc
// init_payments wrap and the on_cart_update rate-seed wrap above. edit_order is
// an arrow-fn on the PastOrderSummary events object (not a prototype method), so
// we wrap the prototype `init_order_summary` that builds it and swap the event.
// The reset step is the ONLY change; the other run_serially steps mirror
// ERPNext's stock edit_order (pinned version) via public controller methods, and
// we fall back to the stock handler if any of those methods is renamed — resume
// never breaks. Re-verify the step list against ERPNext pos_controller.js
// edit_order on any ERPNext version bump.
(function fflEditOrderTenderGuard() {
	// Idempotent prototype wrapper; applied via installClassHook the moment the POS
	// Controller class is available (see wire()).
	function applyEditOrderPatch(C) {
		if (!C || C.prototype.__fflEditOrderTenderPatched) return;
		const originalInit = C.prototype.init_order_summary;
		if (typeof originalInit !== 'function') return;
		C.prototype.init_order_summary = function fflInitOrderSummary() {
			originalInit.apply(this, arguments);
			const controller = this;
			const os = this.order_summary;
			if (!os || !os.events || os.events.__fflTenderGuard) return;
			const stockEditOrder = os.events.edit_order;
			os.events.edit_order = function fflEditOrder(doctype, name) {
				// Fall back to the stock handler if the controller API we mirror
				// changed — never break resume just to save a tender.
				if (!(window.ffl && typeof ffl.editOrderResumeSteps === 'function')
					|| typeof controller.make_invoice_frm !== 'function'
					|| typeof controller.sync_draft_invoice_to_frm !== 'function'
					|| typeof controller.toggle_components !== 'function'
					|| !controller.cart || typeof controller.cart.load_invoice !== 'function') {
					return stockEditOrder && stockEditOrder.call(this, doctype, name);
				}
				if (typeof controller.toggle_recent_order === 'function') controller.toggle_recent_order();
				// editOrderResumeSteps (pos_tender_guard.js) is the single, unit-tested
				// source of the step list + the "skip reset iff live tender" rule.
				return frappe.run_serially(ffl.editOrderResumeSteps(controller, doctype, name));
			};
			os.events.__fflTenderGuard = true;
		};
		C.prototype.__fflEditOrderTenderPatched = true;
	}
	// Wrap the POS Payment component's checkout so a held/paid order's preserved split
	// isn't zeroed by ERPNext's set_default_payment on entering the payment screen.
	// Idempotent via __fflPaymentTenderPatched.
	function applyPaymentPatch(P) {
		if (!P || P.prototype.__fflPaymentTenderPatched) return;
		const originalCheckout = P.prototype.checkout;
		if (typeof originalCheckout !== 'function') return;
		P.prototype.checkout = function fflCheckout() {
			try {
				const frm = this.events && typeof this.events.get_frm === 'function'
					&& this.events.get_frm();
				if (frm && window.ffl && ffl.applyHeldTenderCheckoutGuard) {
					ffl.applyHeldTenderCheckoutGuard(frm);
				}
			} catch (e) { /* never break checkout just to save a tender */ }
			return originalCheckout.apply(this, arguments);
		};
		// Card tile on an order with card money already captured takes the
		// amount to charge now (bindCardChargeNow, Payroc block).
		const originalRenderModes = P.prototype.render_payment_mode_dom;
		if (typeof originalRenderModes === 'function') {
			P.prototype.render_payment_mode_dom = function fflRenderPaymentModes() {
				// Trade-ins exceed the goods: no tender tiles, the payout screen instead.
				try { if (window.ffl && ffl.renderNegativeTicket && ffl.renderNegativeTicket(this)) return; } catch (e) { /* tiles stay */ }
				const out = originalRenderModes.apply(this, arguments);
				try { if (window.ffl && ffl.bindCardChargeNow) ffl.bindCardChargeNow(this); } catch (e) { /* tile keeps its plain meaning */ }
				return out;
			};
		}
		const originalTotals = P.prototype.update_totals_section;
		if (typeof originalTotals === 'function') {
			P.prototype.update_totals_section = function fflUpdateTotals() {
				const out = originalTotals.apply(this, arguments);
				try { if (window.ffl && ffl.renderNegativeTotals) ffl.renderNegativeTotals(this); } catch (e) { /* vanilla totals stay */ }
				return out;
			};
		}
		P.prototype.__fflPaymentTenderPatched = true;
	}
	// Bounded retry ONLY for the degraded path (installClassHook unavailable or
	// defineProperty rejected) — never the primary mechanism. Patches are idempotent,
	// so re-applying whichever class is already present is safe.
	function fallbackRetry(retriesLeft) {
		const ns = window.erpnext && erpnext.PointOfSale;
		if (ns) {
			if (ns.Controller) applyEditOrderPatch(ns.Controller);
			if (ns.Payment) applyPaymentPatch(ns.Payment);
			if (ns.Controller && ns.Payment) return;
		}
		if (retriesLeft > 0) setTimeout(() => fallbackRetry(retriesLeft - 1), 250);
	}
	function wire() {
		if (!window.frappe || typeof frappe.provide !== 'function') return;
		// Ensure the shared namespace exists (frappe.provide is idempotent, so ERPNext's
		// own frappe.provide reuses this object and our traps survive), then wire
		// DETERMINISTICALLY: each applyPatch fires the instant ERPNext assigns
		// erpnext.PointOfSale.Controller (pos_controller.js:1) / .Payment (pos_payment.js:2)
		// — no finite retry window to lose against the lazy POS bundle.
		// Degrade to a bounded retry only if the hook module isn't loaded / defineProperty
		// was rejected.
		frappe.provide('erpnext.PointOfSale');
		const hook = window.ffl && ffl.installClassHook;
		if (hook) {
			const c = hook(erpnext.PointOfSale, 'Controller', applyEditOrderPatch);
			const p = hook(erpnext.PointOfSale, 'Payment', applyPaymentPatch);
			if (c && p) return;
		}
		fallbackRetry(60);
	}
	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', wire);
	} else {
		wire();
	}
})();

// === Extension registry — Desk-side wiring ==========
// serialActions / customerActions become custom buttons on the Serial No /
// Customer forms under an "Extensions" group; onDeskReady runs once per
// switched-on extension after app_ready. Only extensions whose switch is ON
// (frappe.boot.gunshop_extensions) take part — the registry decides. The one
// failure reporter (POS toolbar + forms) lives on the registry so an
// extension's error always reads the same way.
(function () {
	'use strict';
	if (!window.frappe) return;
	const FORM_KINDS = [['serialActions', 'Serial No'], ['customerActions', 'Customer']];
	// extensions_registry.js loads AFTER this bundle (hooks.app_include_js order),
	// so the registry is resolved lazily — at app_ready, never at eval time.
	const registry = () => window.gunstore && window.gunstore.extensions;

	function reportFailure(action, e) {
		console.error(`extension ${action.extension}: ${action.id} failed`, e);
		frappe.show_alert({
			message: __('{0} could not complete. See the browser console.',
				[frappe.utils.escape_html(action.label || action.id)]),
			indicator: 'red',
		});
	}
	function wireForms(reg) {
		for (const [kind, doctype] of FORM_KINDS) {
			const actions = reg.actions(kind, frappe.boot);
			if (!actions.length) continue;
			frappe.ui.form.on(doctype, {
				refresh(frm) {
					for (const a of actions) {
						if (!reg.applies(a, { frm })) continue;
						frm.add_custom_button(frappe.utils.escape_html(a.label || a.id), () => {
							try {
								Promise.resolve(a.onClick({ frm })).catch((e) => reg.reportFailure(a, e));
							} catch (e) {
								reg.reportFailure(a, e);
							}
						}, __('Extensions'));
					}
				},
			});
		}
	}
	function deskReady(reg) {
		for (const id of reg.enabledIds(frappe.boot)) {
			const spec = reg.get(id);
			if (spec && typeof spec.onDeskReady === 'function') {
				try { spec.onDeskReady(); } catch (e) { console.error(`extension ${id}: onDeskReady failed`, e); }
			}
		}
	}
	$(document).on('app_ready', () => {
		const reg = registry();
		if (!reg) return;
		reg.reportFailure = reportFailure;   // the one failure reporter (POS toolbar + forms)
		wireForms(reg);
		deskReady(reg);
	});
})();
