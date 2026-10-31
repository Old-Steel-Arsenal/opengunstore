// Inventory Count — walk the shop and scan.
//
// Firearms: scan the FastBound label (its barcode is the gun's serial number), or
// tick a gun by hand. Non-serialized items: scan the UPC (each scan adds 1) or type a
// quantity for an item with no barcode. Every scan is saved the moment it happens, so
// several devices can scan into the same count and a count can be picked up another day.
//
// Firearm differences are only REPORTED. Finalize adjusts the non-serialized items with
// one Stock Reconciliation and never touches a gun.
//
// The functions below are a PURE KERNEL (no DOM, no frappe, no __()), unit-tested under
// Node in tests/js/stocktake.test.js. The `stkc` prefix keeps them clear of the other
// desk pages, which share one global scope.

function stkcKey(itemCode, warehouse) {
	return `${itemCode}||${warehouse}`;
}

// The expected shelf list with what has been found laid over it. A gun that was scanned
// but is not on the shelf list comes last, flagged `extra`.
function stkcMergeSerials(expected, found) {
	found = found || {};
	const seen = new Set();
	const rows = (expected || []).map((g) => {
		seen.add(g.serial);
		const f = found[g.serial];
		return Object.assign({}, g, {
			found: !!f, entry: f ? f.entry : null, by: f ? f.by : null,
			at: f ? f.at : null, method: f ? f.method : null, extra: false,
		});
	});
	Object.keys(found).sort().forEach((serial) => {
		if (seen.has(serial)) return;
		const f = found[serial];
		rows.push({
			serial, item_code: f.item_code, item_name: f.item_code, manufacturer: '',
			model_name: '', caliber: '', warehouse: '', reason: '', label: '',
			found: true, entry: f.entry, by: f.by, at: f.at, method: f.method, extra: true,
		});
	});
	return rows;
}

// Expected stock per item and warehouse with the counted quantity laid over it.
function stkcMergeItems(items, counted) {
	const byKey = {};
	(counted || []).forEach((c) => { byKey[stkcKey(c.item_code, c.warehouse)] = c; });
	return (items || []).map((r) => {
		const c = byKey[stkcKey(r.item_code, r.warehouse)];
		return Object.assign({}, r, {
			counted: c ? c.counted : 0,
			last_entry: c ? c.last_entry : null,
			last_delta: c ? c.last_delta : null,
		});
	});
}

// Counted rows the item list has no row for yet (another device scanned something this
// page has not heard of), as {item_code, warehouse} pairs for fillMissing to fetch.
function stkcMissingRows(items, counted) {
	const have = new Set((items || []).map((r) => stkcKey(r.item_code, r.warehouse)));
	return (counted || []).filter((c) => !have.has(stkcKey(c.item_code, c.warehouse)))
		.map((c) => ({ item_code: c.item_code, warehouse: c.warehouse }));
}

function stkcMatches(text, ...fields) {
	const t = String(text || '').trim().toLowerCase();
	if (!t) return true;
	return fields.some((f) => String(f || '').toLowerCase().includes(t));
}

// "Show uncounted only" hides what has been found, so the list that is left is what
// still has to be walked to.
function stkcFilterSerials(rows, opts) {
	const o = opts || {};
	return (rows || []).filter((r) => !(o.uncountedOnly && r.found)
		&& stkcMatches(o.text, r.serial, r.item_name, r.manufacturer, r.model_name, r.caliber));
}

// An item with any entry (even a typed 0) has been walked to; the rest has not.
function stkcTouched(row) {
	return !!row.last_entry;
}

function stkcFilterItems(rows, opts) {
	const o = opts || {};
	return (rows || []).filter((r) => (!o.warehouse || r.warehouse === o.warehouse)
		&& !(o.uncountedOnly && stkcTouched(r))
		&& stkcMatches(o.text, r.item_code, r.item_name, r.upc));
}

// none = not counted yet, match = counted what the system says, short / over otherwise.
function stkcItemStatus(row) {
	if (!stkcTouched(row)) return row.expected ? 'none' : 'match';
	if (row.counted === row.expected) return 'match';
	return row.counted < row.expected ? 'short' : 'over';
}

function stkcTotals(serialRows, itemRows) {
	const guns = serialRows || [];
	const items = itemRows || [];
	return {
		gunsExpected: guns.filter((g) => !g.extra).length,
		gunsFound: guns.filter((g) => !g.extra && g.found).length,
		gunsExtra: guns.filter((g) => g.extra).length,
		itemsTouched: items.filter(stkcTouched).length,
		itemsTotal: items.length,
	};
}

// What a scan, a typed quantity or an Undo leaves behind for one item, laid over the
// counted list the page holds. No entry left (and nothing counted) removes the row.
function stkcPatchCounted(counted, patch) {
	const key = stkcKey(patch.item_code, patch.warehouse);
	const rest = (counted || []).filter((c) => stkcKey(c.item_code, c.warehouse) !== key);
	if (!patch.last_entry) return rest;
	rest.push({ item_code: patch.item_code, warehouse: patch.warehouse, counted: patch.counted,
		last_entry: patch.last_entry, last_delta: patch.last_delta });
	return rest;
}

// Finalize: the rows an item the staff walked to start ticked; items nobody scanned start
// unticked (ticking one sets it to 0) and are shown apart.
function stkcDefaultTicked(rows) {
	return (rows || []).filter((r) => !r.never_scanned).map((r) => stkcKey(r.item_code, r.warehouse));
}

function stkcSelectedRows(rows, ticked) {
	const on = new Set(ticked || []);
	return (rows || []).filter((r) => on.has(stkcKey(r.item_code, r.warehouse)));
}

function stkcNeverScanned(rows) {
	return (rows || []).filter((r) => r.never_scanned);
}

// Rows with a register sale not yet closed: they cannot be adjusted until the register closes.
function stkcBlockedRows(rows) {
	return (rows || []).filter((r) => (r.open_register_sales || []).length);
}

// Guarded so Node can require the kernel above. Harmless in the browser, where
// frappe is always defined.
if (typeof frappe !== 'undefined') {
	frappe.pages['stocktake'].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __('Inventory Count'),
			single_column: true,
		});
		wrapper.stocktake_app = new StocktakeApp(page);
	};
	frappe.pages['stocktake'].on_page_show = function (wrapper) {
		wrapper.stocktake_app?.onShow();
	};
}

const STKC_API = 'ffl_core.api.inventory_count';
const STKC_POLL_MS = 5000;

class StocktakeApp {
	constructor(page) {
		this.page = page;
		this.count = null; // the open view's count name; null = the list
		this.state = null; // heavy state (expected guns / items)
		this.tab = 'guns';
		this.warehouse = null;
		this.filters = { uncountedOnly: false, text: '' };
		this._chain = Promise.resolve();
		this._failures = 0; // queued requests that failed since the last full refresh
		this.frozen = false; // true while the Finalize dialog is open: no scans, no edits
		this._poll = null;
		this.canFinalize = frappe.user.has_role('Stock Manager') || frappe.user.has_role('System Manager');
		this.$body = $(`
			<div class="stkc">
				<style>
					.stkc { max-width: 1000px; margin: 0 auto; padding: 4px 0 80px; }
					@media (max-width: 767px) { .stkc { padding-inline: 15px; } }
					.stkc .card-box { background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
					.stkc table { width: 100%; border-collapse: collapse; }
					.stkc th, .stkc td { padding: 7px 8px; border-bottom: 1px solid var(--border-color);
						font-size: 13px; vertical-align: middle; text-align: left; }
					.stkc th { background: var(--bg-light-gray); font-weight: 600; }
					.stkc td.num, .stkc th.num { text-align: right; }
					.stkc .muted { color: var(--text-muted); }
					.stkc .scan-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
					.stkc .scan-bar input.scan { flex: 1; min-width: 220px; font-size: 18px; padding: 8px 12px; }
					.stkc .feedback { min-height: 40px; margin: 10px 0; padding: 9px 12px; border-radius: 6px;
						font-size: 14px; background: var(--bg-light-gray); }
					.stkc .feedback.ok { background: var(--green-100, #e4f5e9); color: var(--green-700, #1f6b3a); }
					.stkc .feedback.warn { background: var(--yellow-100, #fff5d6); color: var(--yellow-700, #7a5c00); }
					.stkc .feedback.err { background: var(--red-100, #fde8e8); color: var(--red-700, #a12626); }
					.stkc tr.flash td { animation: stkc-flash 1.2s ease-out; }
					@keyframes stkc-flash { from { background: var(--green-200, #bfe8cb); } to { background: transparent; } }
					.stkc tr.is-found td { color: var(--text-muted); }
					.stkc tr.is-extra td { background: var(--yellow-50, #fffbea); }
					.stkc .tabs { display: flex; gap: 6px; margin-bottom: 10px; }
					.stkc .tabs .btn.active { background: var(--primary); color: #fff; }
					.stkc .badge-s { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; }
					.stkc .badge-s.short { background: var(--red-100, #fde8e8); color: var(--red-700, #a12626); }
					.stkc .badge-s.over { background: var(--yellow-100, #fff5d6); color: var(--yellow-700, #7a5c00); }
					.stkc .badge-s.match { background: var(--green-100, #e4f5e9); color: var(--green-700, #1f6b3a); }
					.stkc .qty-input { width: 80px; text-align: right; }
					.stkc .tools { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin: 6px 0 10px; }
					.stkc a.link-btn { cursor: pointer; }
				</style>
				<div class="stkc-view"></div>
			</div>`).appendTo(page.body);
		this.$view = this.$body.find('.stkc-view');
		this.showList();
	}

	// ------------------------------------------------------------- plumbing

	api(method, args, opts) {
		return frappe.call(Object.assign({ method: `${STKC_API}.${method}`, args: args || {} }, opts || {}))
			.then((r) => r.message);
	}

	// Scans are sent one after another, in the order they were made: a scanner can
	// fire faster than the server answers, and the feedback must follow the scans.
	enqueue(fn) {
		const run = this._chain.then(fn, fn);
		this._chain = run.catch(() => { this._failures += 1; });
		return run;
	}

	e(text) {
		return frappe.utils.escape_html(String(text === null || text === undefined ? '' : text));
	}

	onShow() {
		if (this.count) this.refresh();
		else this.showList();
	}

	stopPolling() {
		if (this._poll) clearInterval(this._poll);
		this._poll = null;
	}

	startPolling() {
		this.stopPolling();
		this._poll = setInterval(() => this.pollTick(), STKC_POLL_MS);
	}

	// ------------------------------------------------------------- list view

	showList() {
		this.count = null;
		this.state = null;
		this.stopPolling();
		this.page.clear_primary_action();
		this.page.clear_secondary_action();
		this.page.clear_menu();
		this.page.set_title(__('Inventory Count'));
		this.page.set_primary_action(__('New Count'), () => this.newCountDialog(), 'add');
		this.$view.html(`<div class="card-box muted">${__('Loading…')}</div>`);
		this.api('get_counts').then((counts) => {
			if (this.count) return;
			if (!counts.length) {
				this.$view.html(`<div class="card-box"><b>${__('No counts yet.')}</b>
					<div class="muted">${__('Press New Count to start.')}</div></div>`);
				return;
			}
			const rows = counts.map((c) => `
				<tr>
					<td><a class="link-btn" data-open="${this.e(c.name)}">${this.e(c.name)}</a></td>
					<td>${this.e(c.status)}</td>
					<td>${this.e([...c.warehouses, ...c.item_groups].join(', ') || __('Whole store'))}</td>
					<td class="num">${c.entries}</td>
					<td>${this.e(c.owner)}</td>
					<td>${this.e(frappe.datetime.prettyDate(c.creation))}</td>
					<td class="num"><button class="btn btn-xs btn-default" data-open="${this.e(c.name)}">${c.status === 'Open' ? __('Continue') : __('View')}</button></td>
				</tr>`).join('');
			this.$view.html(`<div class="card-box"><table>
				<thead><tr><th>${__('Count')}</th><th>${__('Status')}</th><th>${__('Scope')}</th>
				<th class="num">${__('Scans')}</th><th>${__('Started by')}</th><th>${__('When')}</th><th></th></tr></thead>
				<tbody>${rows}</tbody></table></div>`);
			this.$view.find('[data-open]').on('click', (ev) => this.openCount($(ev.currentTarget).attr('data-open')));
		});
	}

	newCountDialog() {
		this.api('get_scope_options').then((opts) => {
			const d = new frappe.ui.Dialog({
				title: __('New Count'),
				fields: [
					{ fieldname: 'warehouses', fieldtype: 'MultiSelectList', label: __('Warehouses'),
						description: __('Leave both empty to count the whole store.'),
						get_data: () => Promise.resolve(opts.warehouses.map((w) => ({ value: w, description: '' }))) },
					{ fieldtype: 'Column Break' },
					{ fieldname: 'item_groups', fieldtype: 'MultiSelectList', label: __('Item groups'),
						get_data: (txt) => frappe.db.get_link_options('Item Group', txt) },
					{ fieldtype: 'Section Break' },
					{ fieldname: 'include_custody', fieldtype: 'Check', default: 1,
						label: __('Include guns held for customers') },
				],
				primary_action_label: __('Start Counting'),
				primary_action: (v) => {
					d.hide();
					this.api('create_count', {
						company: opts.company, warehouses: v.warehouses || [],
						item_groups: v.item_groups || [], include_custody: v.include_custody ? 1 : 0,
					}).then((r) => this.openCount(r.name));
				},
			});
			d.show();
		});
	}

	// ------------------------------------------------------------ count view

	openCount(name) {
		this.count = name;
		this.state = null;
		this.tab = 'guns';
		this.filters = { uncountedOnly: false, text: '' };
		this.page.clear_primary_action();
		this.page.set_title(`${__('Inventory Count')} ${name}`);
		this.frozen = false;
		this.refresh();
	}

	refresh() {
		const count = this.count;
		return this.api('get_state', { count }).then((st) => {
			if (this.count !== count) return;
			this.state = st;
			this._failures = 0;
			// Only an open count changes under us; a closed one needs no polling.
			if (st.status === 'Open') this.startPolling();
			else this.stopPolling();
			if (!this.warehouse || !st.warehouses.includes(this.warehouse)) {
				this.warehouse = st.warehouses.includes(st.default_warehouse) ? st.default_warehouse : st.warehouses[0];
			}
			this.renderCount();
		});
	}

	// Cheap poll: only what has been scanned. Another device's scan shows up here, and so
	// does an Undo (a "changes since" cursor would never see a deleted row).
	pollTick() {
		if (!this.count || !this.open || this.frozen || !this.$view.is(':visible') || document.hidden) return;
		const count = this.count;
		this.api('get_state', { count, counts_only: 1 }, { freeze: false, no_spinner: true }).then((p) => {
			if (this.count !== count || !this.state) return;
			// Someone finalized or cancelled it: redraw everything (scanning off, banner on).
			if (p.status !== this.state.status) return this.refresh();
			this.state.found = p.found;
			this.state.counted = p.counted;
			this.state.unknown = p.unknown;
			return this.fillMissing().then(() => {
				// Do not redraw under a quantity being typed: it would eat the keystrokes.
				if (!this.qtyEditing()) this.renderBody();
			});
		});
	}

	// An item another device (or a typed entry) added that this page has no row for: fetch
	// just those rows, not the whole state.
	fillMissing() {
		const missing = stkcMissingRows(this.state.items, this.state.counted);
		if (!missing.length) return Promise.resolve();
		const count = this.count;
		return Promise.all(missing.map((m) => this.api('get_item_row', Object.assign({ count }, m), { freeze: false, no_spinner: true })))
			.then((rows) => { if (this.count === count && this.state) this.state.items = [...this.state.items, ...rows]; });
	}

	qtyEditing() {
		const active = document.activeElement;
		return !!(active && active.classList.contains('qty-input'))
			|| this.$view.find('.qty-input[data-dirty]').length > 0;
	}

	get open() {
		return this.state && this.state.status === 'Open';
	}

	// Scanning and editing are allowed while the count is open and the Finalize dialog is not.
	get editable() {
		return this.open && !this.frozen;
	}

	renderCount() {
		const st = this.state;
		const scope = st.warehouses.join(', ');
		this.page.clear_primary_action();
		this.page.clear_menu();
		this.page.set_secondary_action(__('All Counts'), () => this.showList());
		if (this.open && this.canFinalize) {
			this.page.set_primary_action(__('Finalize Count'), () => this.startFinalize());
			this.page.add_menu_item(__('Cancel this count'), () => frappe.confirm(
				__('Cancel {0}? Nothing is adjusted, and the scans are kept for the record.', [this.count]),
				() => this.api('cancel_count', { count: this.count }).then(() => this.showList())));
		}
		this.$view.html(`
			<div class="card-box">
				<div class="scan-bar">
					<input type="text" class="scan form-control" autocomplete="off" autocapitalize="off"
						placeholder="${this.open ? __('Scan a firearm label or a barcode, then press Enter') : __('This count is {0}', [st.status.toLowerCase()])}"
						${this.editable ? '' : 'disabled'}>
					${st.warehouses.length > 1 ? `<select class="form-control wh-pick" style="max-width:260px" title="${__('Warehouse for barcode scans')}">
						${st.warehouses.map((w) => `<option value="${this.e(w)}" ${w === this.warehouse ? 'selected' : ''}>${this.e(w)}</option>`).join('')}
					</select>` : `<span class="muted">${this.e(scope)}</span>`}
				</div>
				<div class="feedback">${this.open ? __('Ready to scan.') : this.closedNotice()}</div>
				<div class="unknown-bar"></div>
			</div>
			<div class="card-box">
				<div class="tabs">
					<button class="btn btn-sm btn-default tab-guns">${__('Firearms')}</button>
					<button class="btn btn-sm btn-default tab-items">${__('Items')}</button>
				</div>
				<div class="tools">
					<label class="checkbox" style="margin:0"><input type="checkbox" class="only-uncounted"> ${__('Show uncounted only')}</label>
					<input type="text" class="form-control filter-text" style="max-width:240px" placeholder="${__('Filter')}">
					<span class="totals muted"></span>
				</div>
				<div class="add-item" style="display:none; gap:8px; align-items:flex-end; margin-bottom:10px">
					<div class="add-item-link" style="min-width:280px"></div>
					<input type="number" min="0" step="any" class="form-control add-item-qty" style="width:100px" placeholder="${__('Qty')}">
					<button class="btn btn-sm btn-default add-item-btn">${__('Add item')}</button>
				</div>
				<div class="tab-body"></div>
			</div>`);
		this.makeAddItem();
		this.$scan = this.$view.find('input.scan');
		this.$feedback = this.$view.find('.feedback');
		this.$scan.on('keydown', (ev) => {
			if (ev.key !== 'Enter') return;
			ev.preventDefault();
			const code = this.$scan.val().trim();
			this.$scan.val('');
			if (code) this.scanCode(code);
			this.$scan.trigger('focus');
		});
		this.$view.find('.wh-pick').on('change', (ev) => { this.warehouse = ev.target.value; this.renderBody(); this.focusScan(); });
		this.$view.find('.tab-guns').on('click', () => { this.tab = 'guns'; this.renderBody(); this.focusScan(); });
		this.$view.find('.tab-items').on('click', () => { this.tab = 'items'; this.renderBody(); this.focusScan(); });
		this.$view.find('.only-uncounted').on('change', (ev) => { this.filters.uncountedOnly = ev.target.checked; this.renderBody(); });
		this.$view.find('.filter-text').on('input', (ev) => { this.filters.text = ev.target.value; this.renderBody(); });
		this.renderBody();
		this.focusScan();
	}

	// For an item with no stock on the books and no barcode: pick it and type what was found.
	makeAddItem() {
		this.addItem = frappe.ui.form.make_control({
			parent: this.$view.find('.add-item-link')[0],
			df: { fieldtype: 'Link', options: 'Item', fieldname: 'add_item', placeholder: __('Item with no barcode or stock'),
				get_query: () => ({ filters: Object.assign({ is_stock_item: 1, has_serial_no: 0, has_batch_no: 0, disabled: 0 },
					this.state.item_groups ? { item_group: ['in', this.state.item_groups] } : {}) }) },
			render_input: true,
		});
		this.addItem.refresh();
		this.$view.find('.add-item-btn').on('click', () => {
			const item = this.addItem.get_value();
			const qty = this.$view.find('.add-item-qty').val();
			if (!item || String(qty).trim() === '') return frappe.show_alert({ message: __('Pick an item and type the quantity you found.'), indicator: 'orange' });
			this.setQty(item, this.warehouse, qty);
			this.addItem.set_value('');
			this.$view.find('.add-item-qty').val('');
		});
	}

	closedNotice() {
		const st = this.state;
		if (st.status === 'Finalized') {
			const sr = st.stock_reconciliation
				? ` ${__('Stock Reconciliation')}: <a href="/app/stock-reconciliation/${encodeURIComponent(st.stock_reconciliation)}">${this.e(st.stock_reconciliation)}</a>.`
				: ` ${__('No stock adjustment was needed.')}`;
			return `${__('This count is finalized.')}${sr} <a class="link-btn show-report">${__('Firearm report')}</a>`;
		}
		return __('This count was cancelled. Nothing was adjusted.');
	}

	focusScan() {
		if (this.editable && this.$scan) this.$scan.trigger('focus');
	}

	say(kind, html) {
		this.$feedback.attr('class', `feedback ${kind}`).html(html);
		if (kind === 'err') frappe.utils.play_sound('error');
		else if (kind === 'warn') frappe.utils.play_sound('alert');
	}

	// -------------------------------------------------------------- the body

	rows() {
		const st = this.state;
		return {
			guns: stkcMergeSerials(st.serials, st.found),
			items: stkcMergeItems(st.items, st.counted),
		};
	}

	renderBody() {
		if (!this.state) return;
		if (this.$scan) this.$scan.prop('disabled', !this.editable);
		const { guns, items } = this.rows();
		const t = stkcTotals(guns, items.filter((r) => !this.warehouse || r.warehouse === this.warehouse));
		this.$view.find('.add-item').css('display', this.tab === 'items' && this.editable ? 'flex' : 'none');
		this.$view.find('.tab-guns').toggleClass('active', this.tab === 'guns').text(`${__('Firearms')} (${t.gunsFound}/${t.gunsExpected})`);
		this.$view.find('.tab-items').toggleClass('active', this.tab === 'items').text(`${__('Items')} (${t.itemsTouched}/${t.itemsTotal})`);
		this.$view.find('.totals').text(this.tab === 'guns' && t.gunsExtra
			? __('{0} scanned but not on the shelf list', [t.gunsExtra]) : '');
		this.renderUnknown();
		this.$view.find('.show-report').off('click').on('click', () => this.showReport());
		if (this.tab === 'guns') this.renderGuns(guns);
		else this.renderItems(items);
	}

	renderUnknown() {
		const u = this.state.unknown || [];
		const $bar = this.$view.find('.unknown-bar');
		if (!u.length) return $bar.empty();
		$bar.html(`<div class="muted" style="font-size:12px">${__('Unknown barcodes')}: ${u.map((x) =>
			`${this.e(x.code)} <a class="link-btn undo" data-entry="${this.e(x.entry)}">${__('Undo')}</a>`).join(' · ')}</div>`);
		$bar.find('.undo').on('click', (ev) => this.undo($(ev.currentTarget).attr('data-entry')));
	}

	renderGuns(guns) {
		const rows = stkcFilterSerials(guns, this.filters);
		const head = `<thead><tr><th style="width:36px"></th><th>${__('Serial')}</th><th>${__('Item')}</th>
			<th>${__('Warehouse')}</th><th>${__('Note')}</th><th>${__('Counted by')}</th></tr></thead>`;
		const body = rows.map((g) => {
			const note = g.extra ? __('Not on the shelf list') : (g.label || '');
			const model = [g.manufacturer, g.model_name, g.caliber].filter(Boolean).join(' ') || g.item_name;
			return `<tr data-serial="${this.e(g.serial)}" class="${g.found ? 'is-found' : ''} ${g.extra ? 'is-extra' : ''}">
				<td><input type="checkbox" class="tick" ${g.found ? 'checked' : ''} ${this.editable ? '' : 'disabled'}></td>
				<td><b>${this.e(g.serial)}</b></td><td>${this.e(model)}</td><td>${this.e(g.warehouse)}</td>
				<td>${this.e(note)}</td>
				<td class="muted">${g.found ? this.e(`${g.by}${g.method === 'manual' ? ' (' + __('by hand') + ')' : ''}`) : ''}</td></tr>`;
		}).join('');
		this.$view.find('.tab-body').html(rows.length
			? `<table>${head}<tbody>${body}</tbody></table>`
			: `<div class="muted">${this.filters.uncountedOnly ? __('Every firearm on the list has been counted.') : __('No firearms to count here.')}</div>`);
		this.$view.find('.tab-body .tick').on('change', (ev) => {
			const serial = $(ev.currentTarget).closest('tr').attr('data-serial');
			this.toggleSerial(serial, ev.currentTarget.checked);
		});
	}

	renderItems(items) {
		const rows = stkcFilterItems(items, Object.assign({ warehouse: this.warehouse }, this.filters));
		const head = `<thead><tr><th>${__('Item')}</th><th>${__('UPC')}</th><th class="num">${__('System')}</th>
			<th class="num">${__('Counted')}</th><th>${__('Type a quantity')}</th><th></th></tr></thead>`;
		const body = rows.map((r) => {
			const status = stkcItemStatus(r);
			const badge = status === 'none' ? '' : `<span class="badge-s ${status}">${status === 'match' ? __('OK') : status === 'short' ? __('Short') : __('Over')}</span>`;
			// Every row takes a typed quantity (a case of 50, a damaged label); scanning still adds 1.
			const typed = `<input type="number" min="0" step="any" class="form-control input-xs qty-input" value="${stkcTouched(r) ? r.counted : ''}" placeholder="${__('qty')}" ${this.editable ? '' : 'disabled'}>`;
			const undo = r.last_entry && this.editable
				? `<a class="link-btn undo" data-entry="${this.e(r.last_entry)}">${__('Undo last')} (${r.last_delta > 0 ? '+' : ''}${r.last_delta})</a>` : '';
			return `<tr data-item="${this.e(r.item_code)}" data-wh="${this.e(r.warehouse)}">
				<td><b>${this.e(r.item_name)}</b><div class="muted" style="font-size:11px">${this.e(r.item_code)}</div></td>
				<td>${this.e(r.upc) || `<span class="muted">${__('no barcode')}</span>`}</td>
				<td class="num">${r.expected}</td><td class="num"><b>${r.counted}</b> ${badge}</td>
				<td>${typed}</td><td>${undo}</td></tr>`;
		}).join('');
		this.$view.find('.tab-body').html(rows.length
			? `<table>${head}<tbody>${body}</tbody></table>`
			: `<div class="muted">${this.filters.uncountedOnly ? __('Every item here has been counted.') : __('Nothing to count here yet. Scan a barcode to add an item.')}</div>`);
		this.$view.find('.tab-body .qty-input').on('input', (ev) => $(ev.currentTarget).attr('data-dirty', 1))
			.on('change', (ev) => {
				const $tr = $(ev.currentTarget).closest('tr');
				$(ev.currentTarget).attr('data-saving', 1);
				this.setQty($tr.attr('data-item'), $tr.attr('data-wh'), ev.currentTarget.value);
			})
			// Leaving the box without a change (typed and put back) must not hold the poll off for good;
			// one that is being saved keeps it off until the answer redraws the row.
			.on('blur', (ev) => { if (!$(ev.currentTarget).attr('data-saving')) $(ev.currentTarget).removeAttr('data-dirty'); });
		this.$view.find('.tab-body .undo').on('click', (ev) => this.undo($(ev.currentTarget).attr('data-entry')));
	}

	flash(selector) {
		const $row = this.$view.find(selector);
		$row.addClass('flash');
		setTimeout(() => $row.removeClass('flash'), 1300);
	}

	// ----------------------------------------------------------------- actions

	scanCode(code) {
		if (!this.editable) return;
		// The warehouse the scan was MADE in, not whatever is selected when its turn comes.
		const count = this.count;
		const warehouse = this.warehouse;
		return this.enqueue(() => this.api('scan', { count, code, warehouse }, { freeze: false, no_spinner: true })
			.then((r) => this.afterScan(r)));
	}

	afterScan(r) {
		if (!this.state || !r) return;
		const st = this.state;
		const msg = this.e(r.message);
		if (r.result === 'serial' || r.result === 'already') {
			if (r.result === 'serial') {
				st.found = Object.assign({}, st.found, { [r.serial]: {
					entry: r.entry, by: frappe.session.user, at: frappe.datetime.now_datetime(),
					method: 'scan', item_code: r.item_code } });
			}
			this.say(r.warning ? 'warn' : (r.result === 'already' ? 'warn' : 'ok'), r.warning ? this.e(r.warning) : msg);
			this.tab = 'guns';
			this.renderBody();
			this.flash(`tr[data-serial="${CSS.escape(r.serial)}"]`);
		} else if (r.result === 'item') {
			st.counted = stkcPatchCounted(st.counted, { item_code: r.item_code, warehouse: r.warehouse,
				counted: r.counted, last_entry: r.entry, last_delta: 1 });
			this.say('ok', msg);
			return this.fillMissing().then(() => {
				this.tab = 'items';
				this.renderBody();
				this.flash(`tr[data-item="${CSS.escape(r.item_code)}"][data-wh="${CSS.escape(r.warehouse)}"]`);
			});
		} else if (r.result === 'unknown') {
			st.unknown = [...(st.unknown || []), { entry: r.entry, code: r.code }];
			this.say('err', msg);
			this.renderBody();
		} else {
			this.say('err', msg);
		}
	}

	toggleSerial(serial, found) {
		const count = this.count;
		return this.enqueue(() => this.api('toggle_serial', { count, serial, found: found ? 1 : 0 }, { freeze: false, no_spinner: true })
			.then((r) => {
				const rest = Object.assign({}, this.state.found);
				if (r.found && r.entry) rest[serial] = { entry: r.entry, by: frappe.session.user,
					at: frappe.datetime.now_datetime(), method: 'manual', item_code: (this.state.serials.find((g) => g.serial === serial) || {}).item_code };
				else if (!r.found) delete rest[serial];
				this.state.found = rest;
				this.renderBody();
			}, () => this.refresh()));
	}

	setQty(item_code, warehouse, qty) {
		const count = this.count;
		if (String(qty).trim() === '') return;
		return this.enqueue(() => this.api('set_qty', { count, item_code, warehouse, qty }, { freeze: false, no_spinner: true })
			.then((r) => {
				if (r.entry) this.state.counted = stkcPatchCounted(this.state.counted, { item_code, warehouse,
					counted: r.counted, last_entry: r.last_entry, last_delta: r.last_delta });
				return this.fillMissing().then(() => this.renderBody());
			}, () => this.refresh()));
	}

	undo(entry) {
		return this.enqueue(() => this.api('undo', { entry }, { freeze: false, no_spinner: true })
			.then((r) => {
				const st = this.state;
				if (r.kind === 'serial') {
					const rest = Object.assign({}, st.found);
					delete rest[r.serial_no];
					st.found = rest;
				} else if (r.kind === 'unknown') {
					st.unknown = (st.unknown || []).filter((u) => u.entry !== entry);
				} else if (r.kind === 'qty') {
					st.counted = stkcPatchCounted(st.counted, { item_code: r.item_code, warehouse: r.warehouse,
						counted: r.counted, last_entry: r.last_entry, last_delta: r.last_delta });
				}
				this.say('ok', __('Removed.'));
				this.renderBody();
			}, () => this.refresh()));
	}

	// ---------------------------------------------------------------- finalize

	// Stop taking scans, let everything already sent finish, and only then ask the server
	// for the differences — so the list on screen is the list that gets posted.
	startFinalize() {
		this.frozen = true;
		this.renderBody();
		this.$scan.prop('disabled', true);
		this._chain.then(() => {
			if (this._failures) {
				this.say('err', __('A scan did not save. The list has been reloaded — check it, then press Finalize again.'));
				return this.unfreeze(true);
			}
			return this.api('variance', { count: this.count }).then((v) => this.finalizeDialog(v), () => this.unfreeze());
		});
	}

	unfreeze(reload) {
		this.frozen = false;
		return reload ? this.refresh() : (this.renderBody(), this.focusScan());
	}

	finalizeDialog(v) {
		const ticked = new Set(stkcDefaultTicked(v.items));
		const d = new frappe.ui.Dialog({ title: __('Finalize {0}', [this.count]), size: 'extra-large', fields: [{ fieldtype: 'HTML', fieldname: 'body' }] });
		let finishing = false;
		// Closed without finishing: scanning is allowed again.
		d.onhide = () => { if (!finishing) this.unfreeze(); };
		const sec = v.serial;
		const never = stkcNeverScanned(v.items);
		const walked = v.items.filter((r) => !r.never_scanned);
		const gunLine = (label, list) => (list.length
			? `<div><b>${label}: ${list.length}</b> <span class="muted">${this.e(list.slice(0, 8).map((g) => g.serial || g.code).join(', '))}${list.length > 8 ? '…' : ''}</span></div>` : '');
		const num = (n) => (n > 0 ? '+' : '') + n;
		const rowsHtml = (list) => list.map((r) => {
			const key = stkcKey(r.item_code, r.warehouse);
			return `<tr><td><input type="checkbox" data-key="${this.e(key)}" ${ticked.has(key) ? 'checked' : ''}></td>
				<td>${this.e(r.item_name)}<div class="muted" style="font-size:11px">${this.e(r.warehouse)}${(r.open_register_sales || []).length ? ` · <span style="color:var(--red-600)">${__('register sale not closed')}</span>` : ''}</div></td>
				<td class="num">${r.expected}</td><td class="num">${r.counted}</td>
				<td class="num">${r.moved ? num(r.moved) : ''}</td>
				<td class="num"><b>${r.adjust_to}</b></td>
				<td class="num" style="color:${r.difference < 0 ? 'var(--red-600)' : 'var(--green-600)'}">${num(r.difference)}</td></tr>`;
		}).join('');
		const table = (list) => `<table><thead><tr><th></th><th>${__('Item')}</th>
			<th class="num">${__('System')}</th><th class="num">${__('Counted')}</th>
			<th class="num">${__('Sold/received since')}</th>
			<th class="num">${__('Adjust to')}</th><th class="num">${__('Change')}</th></tr></thead>
			<tbody>${rowsHtml(list)}</tbody></table>`;
		const render = () => {
			const qtyCounted = v.quantity_counted;
			const chosen = qtyCounted ? stkcSelectedRows(v.items, [...ticked]) : [];
			const blocked = stkcBlockedRows(chosen);
			const sales = new Set(blocked.flatMap((r) => r.open_register_sales.map((x) => x.invoice)));
			const since = blocked.length ? frappe.datetime.str_to_user(blocked.flatMap((r) => r.open_register_sales.map((x) => x.posted)).sort()[0].slice(0, 10)) : '';
			const blockedNote = blocked.length
				? `<div class="feedback err">${__('{0} register sales from {1} are not closed yet (items: {2}). Close the register — or wait for the nightly close at 11 PM — then finalize. Or untick those items to adjust the rest. Scanning is not affected.',
					[sales.size, since, this.e(blocked.slice(0, 8).map((r) => r.item_name).join(', '))])}</div>` : '';
			d.fields_dict.body.$wrapper.html(`
				<div class="stkc">
					<div class="card-box">
						<h5 style="margin-top:0">${__('Firearms — reported only, nothing is changed')}</h5>
						${gunLine(__('Missing from the shelf'), sec.missing) + gunLine(__('Scanned but not expected'), sec.unexpected) + gunLine(__('Unknown barcodes'), sec.unknown)
							|| `<div class="muted">${__('Every firearm matches.')}</div>`}
						<button class="btn btn-xs btn-default print-report" style="margin-top:6px">${__('Print firearm report')}</button>
					</div>
					${qtyCounted ? '' : `<div class="muted" style="margin:10px 0">${__('No stock quantities were counted, so nothing is adjusted.')}</div>`}
					${qtyCounted ? `${blockedNote}
					<h5>${__('Items to adjust')}</h5>
					${walked.length ? table(walked) : `<div class="muted">${__('Every counted item matches the system.')}</div>`}` : ''}
					${qtyCounted && never.length ? `<h5 style="margin-top:16px">${__('Never scanned')} (${never.length})
						<button class="btn btn-xs btn-default set-zero" style="margin-left:8px">${__('Set all to 0')}</button></h5>
						<div class="muted" style="margin-bottom:6px">${__('Left alone unless you tick them, which sets them to 0.')}</div>
						${table(never)}` : ''}
					${qtyCounted && (v.skipped || []).length ? `<h5 style="margin-top:16px">${__('Cannot be adjusted here')}</h5>
						<div class="muted" style="margin-bottom:6px">${__('Fix the item, then adjust these by hand.')}</div>
						<table><tbody>${v.skipped.map((x) => `<tr><td>${this.e(x.item_name)}<div class="muted" style="font-size:11px">${this.e(x.warehouse)}</div></td>
							<td class="num">${__('Counted')} ${x.counted}</td><td>${this.e(x.reason)}</td></tr>`).join('')}</tbody></table>` : ''}
				</div>`);
			d.set_primary_action(chosen.length ? __('Adjust {0} and finish', [`${chosen.length} ${chosen.length === 1 ? __('item') : __('items')}`]) : __('Finish count'), () => {
				if (blocked.length) return;
				finishing = true;
				d.hide();
				this.api('finalize', { count: this.count, item_rows: JSON.stringify(chosen.map((r) => ({ item_code: r.item_code, warehouse: r.warehouse }))) })
					.then((r) => this.afterFinalize(r), () => { finishing = false; this.unfreeze(true); });
			});
			d.get_primary_btn().prop('disabled', blocked.length > 0);
			d.$wrapper.find('input[data-key]').on('change', (ev) => {
				const key = $(ev.currentTarget).attr('data-key');
				if (ev.currentTarget.checked) ticked.add(key); else ticked.delete(key);
				render();
			});
			d.$wrapper.find('.set-zero').on('click', () => { never.forEach((r) => ticked.add(stkcKey(r.item_code, r.warehouse))); render(); });
			d.$wrapper.find('.print-report').on('click', () => this.printReport(sec));
		};
		render();
		d.show();
	}

	afterFinalize(r) {
		this.stopPolling();
		this.frozen = false;
		const sr = r.stock_reconciliation
			? __('Stock Reconciliation {0} posted.', [`<a href="/app/stock-reconciliation/${encodeURIComponent(r.stock_reconciliation)}">${this.e(r.stock_reconciliation)}</a>`])
			: __('No stock adjustment was needed.');
		const skipped = (r.skipped || []).length
			? `<br>${__('Not adjusted — do these by hand')}: ${this.e(r.skipped.map((x) => `${x.item_name} (${x.reason})`).join('; '))}` : '';
		frappe.msgprint({ title: __('Count finished'), message: `${sr}<br>${__('Firearms were not changed — check the firearm report for anything missing.')}${skipped}`, indicator: 'green' });
		this.openCount(this.count);
	}

	showReport() {
		this.api('variance', { count: this.count }).then((v) => this.printReport(v.serial));
	}

	printReport(sec) {
		const table = (title, rows, cols) => (rows.length ? `<h3>${this.e(title)} (${rows.length})</h3><table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;width:100%">
			<tr>${cols.map((c) => `<th align="left">${this.e(c[0])}</th>`).join('')}</tr>
			${rows.map((r) => `<tr>${cols.map((c) => `<td>${this.e(r[c[1]] || '')}</td>`).join('')}</tr>`).join('')}</table>` : '');
		const html = `<html><head><title>${this.e(this.count)}</title></head><body style="font-family:sans-serif">
			<h2>${__('Firearm count report')} — ${this.e(this.count)}</h2>
			${table(__('Missing from the shelf'), sec.missing, [[__('Serial'), 'serial'], [__('Item'), 'item_name'], [__('Warehouse'), 'warehouse'], [__('Note'), 'label']])}
			${table(__('Scanned but not expected'), sec.unexpected, [[__('Serial'), 'serial'], [__('Item'), 'item_name'], [__('Note'), 'label']])}
			${table(__('Unknown barcodes'), sec.unknown, [[__('Barcode'), 'code'], [__('Times scanned'), 'times']])}
			</body></html>`;
		const w = window.open('', '_blank');
		if (!w) return frappe.msgprint(__('Allow pop-ups for this site to print the report.'));
		w.document.write(html);
		w.document.close();
		w.focus();
		w.print();
	}
}

// Node unit tests import the pure kernel (no DOM / Frappe). Harmless in the browser,
// where `module` is undefined.
if (typeof module !== 'undefined' && module.exports) {
	module.exports = {
		stkcKey,
		stkcMergeSerials,
		stkcMergeItems,
		stkcMissingRows,
		stkcBlockedRows,
		stkcFilterSerials,
		stkcFilterItems,
		stkcTouched,
		stkcItemStatus,
		stkcTotals,
		stkcPatchCounted,
		stkcDefaultTicked,
		stkcSelectedRows,
		stkcNeverScanned,
	};
}
