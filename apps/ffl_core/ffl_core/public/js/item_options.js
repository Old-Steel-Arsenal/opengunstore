// "Comes in options" on the Item form (server: ffl_core/item_options.py).
//
// A new Item ticked "Comes in options" shows its options (a name each, values as chips in
// storefront order) and one row per combination: include | combination | SKU | Price | Cost |
// Pre-order. One Save creates everything (create_item_with_options) and opens the saved item.
// A saved item that comes in options shows the same grid as its panel: each existing row saves
// itself as it is edited, a new value adds rows that the form's Save creates, an unticked row is
// switched off. A row of such an item shows which item and values it belongs to.
//
// The pure kernel (combinations, default SKUs, row checks) runs under Node:
// tests/js/item_options.test.js.
(function () {
	'use strict';

	const SKU_RE = /^[A-Za-z0-9_][A-Za-z0-9._/-]*$/;
	const MAX_OPTIONS = 3;

	function fold(s) {
		return String(s == null ? '' : s).trim().toLowerCase();
	}

	// [{name, values: [string]}] → every combination, in option order then value order.
	function combinations(options) {
		if (!options.length || options.some((o) => !o.values.length)) return [];
		return options.reduce((acc, o) => acc.flatMap((prefix) => o.values.map((v) => prefix.concat([v]))), [[]]);
	}

	function comboKey(values) {
		return values.map(fold).join('\u0001');
	}

	function defaultSku(code, codes) {
		return [String(code || '').trim()].concat(codes.filter(Boolean)).filter(Boolean).join('-');
	}

	function isBlank(v) {
		return v === '' || v === null || v === undefined;
	}

	// Rows still to be created ({key, include, existing, sku, price, cost, preorder}) → {key: {field: message}}.
	// `taken` holds item codes that already exist (lower case).
	function rowErrors(rows, { taken, templateCode } = {}) {
		const errors = {};
		const add = (key, field, msg) => { (errors[key] = errors[key] || {})[field] = msg; };
		const skus = {};
		rows.forEach((r) => {
			if (!r.include || r.existing) return;
			const sku = String(r.sku || '').trim();
			const f = fold(sku);
			if (!sku) add(r.key, 'sku', 'Enter a SKU');
			else if (sku.length > 140) add(r.key, 'sku', 'Too long');
			else if (!SKU_RE.test(sku)) add(r.key, 'sku', 'Letters, numbers and . _ / - only');
			else if (f === fold(templateCode) || (taken && taken.has(f))) add(r.key, 'sku', 'Already an item');
			else if (skus[f]) {
				add(r.key, 'sku', 'Used twice');
				add(skus[f], 'sku', 'Used twice');
			} else skus[f] = r.key;
			if (!(Number(r.price) > 0)) add(r.key, 'price', 'Enter a price');
			if (!isBlank(r.cost) && !(Number(r.cost) >= 0)) add(r.key, 'cost', 'Not a price');
			if (!isBlank(r.preorder) && !(Number.isInteger(Number(r.preorder)) && Number(r.preorder) >= 0)) {
				add(r.key, 'preorder', 'A whole number');
			}
		});
		return errors;
	}

	// The saved options from the server ({name, values, abbr}) laid onto the editor's option objects.
	// The objects are kept (the editor's inputs hold them), and a value typed here and not saved yet
	// stays at the end — a row saving itself must not drop a value added a moment before.
	function mergeOptions(current, incoming) {
		return incoming.map((o) => {
			const cur = (current || []).find((c) => fold(c.name) === fold(o.name));
			const pending = cur ? cur.values.filter((v) => v.isNew && !o.values.some((s) => fold(s) === fold(v.value))) : [];
			const target = cur || {};
			target.name = o.name;
			target.fixed = true;
			target.values = o.values.map((v) => ({ value: v, code: (o.abbr || {})[v] || '', isNew: false })).concat(pending);
			return target;
		});
	}

	const kernel = { SKU_RE, MAX_OPTIONS, fold, combinations, comboKey, defaultSku, rowErrors, mergeOptions };
	if (typeof module !== 'undefined' && module.exports) module.exports = kernel;
	if (typeof window !== 'undefined') {
		window.ffl = window.ffl || {};
		window.ffl.itemOptionsKernel = kernel;
	}
})();

if (typeof frappe !== 'undefined' && frappe.ui && frappe.ui.form) (function () {
	'use strict';

	const K = window.ffl.itemOptionsKernel;
	const M = 'ffl_core.item_options.';
	// The template's own price, cost, codes and stock: hidden while it comes in options.
	const OWN_FIELDS = ['standard_rate', 'dealer_price', 'map_price', 'msrp', 'sale_price', 'sale_from', 'sale_to',
		'ffl_preorder_available', 'ffl_preorder_eta', 'upc', 'actual_qty'];
	const esc = (s) => frappe.utils.escape_html(String(s == null ? '' : s));
	const num = (v) => (K.fold(v) === '' ? '' : Number(v));

	const CSS = `
.ffl-io { --io-gap: 8px; container-type: inline-size; }
.ffl-io-toggle { display:flex; align-items:center; gap:8px; margin: 2px 0 12px; font-weight:500; }
.ffl-io-toggle input { margin:0; }
.ffl-io-option { display:grid; grid-template-columns: 180px 1fr 24px; gap: var(--io-gap); align-items:start; margin-bottom: var(--io-gap); }
.ffl-io-option .io-name-fixed { padding: 6px 0; font-weight:500; }
.ffl-io-chips { display:flex; flex-wrap:wrap; gap:6px; align-items:center; min-height: 32px; padding: 3px 6px;
	border-radius: var(--border-radius); background: var(--control-bg); }
.ffl-io-chip { display:inline-flex; align-items:center; gap:4px; padding: 2px 8px; border-radius: 12px;
	background: var(--bg-color); border: 1px solid var(--border-color); cursor: grab; font-size: var(--text-sm); user-select:none; }
.ffl-io-chip.io-dragging { opacity: .4; }
.ffl-io-chip.io-over { border-color: var(--primary); }
.ffl-io-chip .io-x { cursor:pointer; color: var(--text-muted); line-height:1; }
.ffl-io-chips .awesomplete { flex: 1; min-width: 120px; }
.ffl-io-chips input.io-value { border:0; background:transparent; box-shadow:none; height: 26px; padding: 0 2px; width:100%; }
.ffl-io-remove { cursor:pointer; color: var(--text-muted); padding-top: 6px; text-align:center; }
.ffl-io-add { margin: 2px 0 14px; }
.ffl-io-grid { width:100%; table-layout:fixed; border-collapse:separate; border-spacing:0; font-size: var(--text-sm); }
.ffl-io-grid col.io-c-chk { width: 34px; } .ffl-io-grid col.io-c-n { width: 104px; } .ffl-io-grid col.io-c-slots { width: 150px; }
.ffl-io-grid th { font-weight:500; color: var(--text-muted); text-align:left; padding: 4px 6px; white-space:nowrap; }
.ffl-io-grid thead tr.io-fill th { padding-top:0; padding-bottom: 8px; border-bottom: 1px solid var(--border-color); }
.ffl-io-grid td { padding: 3px 6px; border-bottom: 1px solid var(--border-color); vertical-align: middle; }
.ffl-io-grid input.form-control { height: 28px; padding: 2px 8px; }
.ffl-io-grid input.io-n { width: 100%; text-align:right; }
.ffl-io-grid input.io-slots { width: 72px; text-align:right; }
.ffl-io-grid input.io-eta { width: 100%; margin-top: 4px; }
.ffl-io-grid tr.io-off td:not(.io-chk) { opacity: .45; }
.ffl-io-grid tr.io-new td.io-label { font-style: italic; }
.ffl-io-grid .io-bad { border: 1px solid var(--red-500) !important; background: var(--red-50, #fff5f5); }
.ffl-io-grid .io-saved { box-shadow: 0 0 0 2px var(--green-200, #bbf7d0); transition: box-shadow .6s; }
.ffl-io-grid .io-count { color: var(--text-color); }
.ffl-io-grid td a { overflow-wrap: anywhere; }
.ffl-io-variant { display:flex; gap:12px; align-items:baseline; padding: 10px var(--padding-md, 15px) 6px; font-size: var(--text-md); }
.ffl-io-variant a { font-weight:600; }
.ffl-io-variant span { color: var(--text-muted); }
@media (max-width: 767px) { .ffl-io-option { grid-template-columns: 1fr 24px; } .ffl-io-option .io-name-wrap { grid-column: 1 / -1; } }
/* Too narrow for the table (a phone; a tablet with the form's side column): one card per row.
   The table needs about 560px (fixed columns 34 + 104 + 104 + 150, the rest for name and SKU). */
@container (max-width: 600px) {
	.ffl-io-grid, .ffl-io-grid thead, .ffl-io-grid tbody { display:block; width:100%; }
	.ffl-io-grid tr { display:grid; grid-template-columns: 28px repeat(3, minmax(0, 1fr)); column-gap: 8px; row-gap: 6px;
		align-items:end; padding: 10px 0; border-bottom: 1px solid var(--border-color); }
	.ffl-io-grid td, .ffl-io-grid th, .ffl-io-grid thead tr.io-fill th { display:block; padding:0; border:0; }
	.ffl-io-grid thead tr:first-child th:not(.io-chk):not(.io-count) { display:none; }
	.ffl-io-grid thead tr:first-child .io-count { grid-column: 2 / -1; }
	.ffl-io-grid tr.io-fill th:empty { display:none; }
	.ffl-io-grid tr.io-fill .io-fill-price-th { grid-column: 2; }
	.ffl-io-grid tr.io-fill .io-fill-slots-th { grid-column: 3 / -1; display:flex; flex-wrap:wrap; gap:6px; }
	.ffl-io-grid tr.io-fill .io-fill-slots-th::before { flex-basis:100%; }
	.ffl-io-grid td.io-chk { grid-row: 1; align-self:start; }
	.ffl-io-grid td.io-label, .ffl-io-grid td.io-sku-td { grid-column: 2 / -1; }
	.ffl-io-grid td.io-sku-td a { white-space: normal; overflow-wrap: anywhere; }
	.ffl-io-grid td.io-price-td { grid-column: 2; }
	.ffl-io-grid td.io-cost-td { grid-column: 3; }
	.ffl-io-grid td.io-slots-td { grid-column: 4; }
	.ffl-io-grid [data-label]::before { content: attr(data-label); display:block; font-size: var(--text-xs); color: var(--text-muted); margin-bottom: 2px; }
	.ffl-io-grid input.io-n, .ffl-io-grid input.io-slots, .ffl-io-grid input.io-eta { width:100%; max-width:none; margin-left:0; }
	.ffl-io-grid tr.io-fill .io-fill-slots-th input.io-slots { flex: 0 0 70px; width:70px; }
	.ffl-io-grid tr.io-fill .io-fill-slots-th input.io-eta { flex: 1 1 120px; width:auto; margin-top:0; }
	.ffl-io-grid colgroup { display:none; }
}
`;

	function ensureCss() {
		if (document.getElementById('ffl-io-css')) return;
		const el = document.createElement('style');
		el.id = 'ffl-io-css';
		el.textContent = CSS;
		document.head.appendChild(el);
	}

	// ---- state -----------------------------------------------------------------------------
	// options: [{name, fixed, values: [{value, code, isNew}]}]
	// rows: {key: {values, include, sku, skuEdited, price, cost, preorder, eta, item_code, disabled}}
	function state(frm) {
		const key = frm.is_new() ? frm.docname : `saved:${frm.doc.name}`;
		if (!frm._fflOptions || frm._fflOptions.key !== key) {
			frm._fflOptions = { key, options: [], rows: {}, fill: { price: '', preorder: '', eta: '' },
				taken: new Set(), loaded: false, optionsDirty: false };
		}
		return frm._fflOptions;
	}

	function templateCode(frm) {
		return frm.is_new() ? (frm.doc.item_code || '') : frm.doc.name;
	}

	function combos(st) {
		return K.combinations(st.options.map((o) => ({ name: o.name, values: o.values.map((v) => v.value) })));
	}

	function codesFor(st, values) {
		return values.map((v, i) => {
			const opt = st.options[i];
			const hit = opt && opt.values.find((x) => K.fold(x.value) === K.fold(v));
			return hit ? hit.code : '';
		});
	}

	// Rebuild rows from the options, keeping every row's own edits.
	function syncRows(frm) {
		const st = state(frm);
		const keep = {};
		combos(st).forEach((values) => {
			const key = K.comboKey(values);
			let row = st.rows[key];
			if (!row) {
				const isNewValue = values.some((v, i) => (st.options[i].values.find((x) => x.value === v) || {}).isNew);
				row = { values, include: frm.is_new() || isNewValue, sku: '', skuEdited: false,
					price: st.fill.price, cost: frm.is_new() ? (frm.doc.dealer_price || '') : '', preorder: st.fill.preorder, eta: null, item_code: null };
			}
			row.values = row.item_code ? row.values : values;
			if (!row.item_code && !row.skuEdited) row.sku = K.defaultSku(templateCode(frm), codesFor(st, values));
			keep[key] = row;
		});
		Object.entries(st.rows).forEach(([key, row]) => { if (row.item_code && !keep[key]) keep[key] = row; });
		st.rows = keep;
	}

	function orderedRows(st) {
		const order = combos(st).map(K.comboKey);
		const extra = Object.keys(st.rows).filter((k) => !order.includes(k));
		return order.concat(extra).filter((k) => st.rows[k]).map((k) => Object.assign({ key: k }, st.rows[k]));
	}

	function pendingRows(st) {
		return orderedRows(st).filter((r) => !r.item_code && r.include);
	}

	function errors(frm) {
		const st = state(frm);
		return K.rowErrors(orderedRows(st).map((r) => Object.assign({}, r, { existing: !!r.item_code })),
			{ taken: st.taken, templateCode: templateCode(frm) });
	}

	function optionsPayload(st) {
		return st.options.map((o) => ({ name: o.name, values: o.values.map((v) => v.value) }));
	}

	function rowPayload(st, r) {
		const values = {};
		st.options.forEach((o, i) => { values[o.name] = r.values[i]; });
		const slots = num(r.preorder) || 0;
		return { values, include: r.include ? 1 : 0, sku: (r.sku || '').trim(), price: num(r.price) || 0,
			cost: num(r.cost) || 0, preorder: slots, eta: slots ? (st.fill.eta || null) : null };
	}

	// ---- server lookups ----------------------------------------------------------------------
	function refreshCodes(frm, opt) {
		if (!opt.name || !opt.values.length) return Promise.resolve();
		return frappe.xcall(M + 'option_codes', { option: opt.name, values: opt.values.map((v) => v.value) })
			.then((codes) => {
				opt.values.forEach((v) => { if (codes[v.value]) v.code = codes[v.value]; });
				syncRows(frm);
				renderGrid(frm);
				checkTaken(frm);
			}).catch(() => {});
	}

	function checkTaken(frm) {
		clearTimeout(frm._fflTakenTimer);
		frm._fflTakenTimer = setTimeout(() => {
			const st = state(frm);
			const codes = pendingRows(st).map((r) => (r.sku || '').trim()).filter(Boolean);
			if (!codes.length) { markErrors(frm); return; }
			frappe.xcall(M + 'taken_item_codes', { codes }).then((taken) => {
				st.taken = new Set((taken || []).map(K.fold));
				markErrors(frm);
			}).catch(() => {});
		}, 300);
	}

	function suggest(input, option) {
		const aw = new Awesomplete(input, { minChars: 0, maxItems: 20, autoFirst: false, list: [],
			filter: () => true, sort: false });
		let timer;
		const load = () => {
			clearTimeout(timer);
			timer = setTimeout(() => {
				frappe.xcall(M + 'option_suggestions', { option: option ? option() : null, txt: input.value })
					.then((rows) => { aw.list = (rows || []).map((r) => r.value); aw.evaluate(); })
					.catch(() => {});
			}, 150);
		};
		input.addEventListener('input', load);
		input.addEventListener('focus', load);
		return aw;
	}

	// ---- render ------------------------------------------------------------------------------
	function wrapper(frm) {
		const f = frm.fields_dict.item_options_html;
		return f ? $(f.wrapper) : null;
	}

	function showOwnFields(frm, show) {
		OWN_FIELDS.forEach((f) => { if (frm.fields_dict[f]) frm.toggle_display(f, show); });
	}

	function render(frm) {
		const $w = wrapper(frm);
		if (!$w) return;
		ensureCss();
		variantLine(frm);
		if (frm.doc.variant_of) {
			$w.empty();
			return;
		}
		if (!frm.is_new() && !frm.doc.has_variants) {
			$w.empty();
			showOwnFields(frm, true);
			return;
		}
		showOwnFields(frm, !frm.doc.has_variants);
		// ERPNext's own note on a template names Item Variant Settings: the panel below says it all.
		if (!frm.is_new()) frm.set_intro();
		$w.html(`<div class="ffl-io">
			${frm.is_new() ? `<label class="ffl-io-toggle"><input type="checkbox" class="io-toggle" ${frm.doc.has_variants ? 'checked' : ''}>${__('Comes in options')}</label>` : ''}
			<div class="io-body"></div></div>`);
		$w.find('.io-toggle').on('change', (e) => {
			frm.set_value('has_variants', e.target.checked ? 1 : 0);
			if (e.target.checked) seedFromForm(frm);
			render(frm);
		});
		if (!frm.doc.has_variants) return;
		const st = state(frm);
		if (!frm.is_new() && !st.loaded) {
			$w.find('.io-body').html(`<div class="text-muted">${__('Loading…')}</div>`);
			load(frm);
			return;
		}
		$w.find('.io-body').html('<div class="io-options"></div><div class="io-grid"></div>');
		if (frm.is_new() && !st.options.length) st.options.push({ name: '', fixed: false, values: [] });
		renderOptions(frm);
		syncRows(frm);
		renderGrid(frm);
	}

	// The form's own price and cost, typed before the box was ticked, become the fill values.
	function seedFromForm(frm) {
		const st = state(frm);
		if (!st.fill.price && frm.doc.standard_rate) st.fill.price = frm.doc.standard_rate;
	}

	function load(frm) {
		return frappe.xcall(M + 'item_options_state', { template: frm.doc.name }).then((data) => {
			applyState(frm, data);
			render(frm);
		});
	}

	function applyState(frm, data) {
		const st = state(frm);
		st.options = K.mergeOptions(st.options, data.options);
		st.rows = {};
		data.rows.forEach((r) => {
			const values = data.options.map((o) => r.values[o.name] || '');
			st.rows[K.comboKey(values)] = { values, include: !r.disabled, disabled: !!r.disabled, sku: r.item_code,
				skuEdited: true, price: r.price, cost: r.cost, preorder: r.preorder, eta: r.eta, item_code: r.item_code };
		});
		const etas = [...new Set(data.rows.filter((r) => r.preorder > 0 && r.eta).map((r) => r.eta))];
		st.fill = { price: '', preorder: '', eta: etas.length === 1 ? etas[0] : '' };
		st.loaded = true;
		st.optionsDirty = false;
		st.preorder = data.preorder;
	}

	function renderOptions(frm) {
		const st = state(frm);
		const $box = wrapper(frm).find('.io-options').empty();
		st.options.forEach((opt, oi) => {
			const $row = $(`<div class="ffl-io-option" data-oi="${oi}">
				<div class="io-name-wrap">${opt.fixed
					? `<div class="io-name-fixed">${esc(opt.name)}</div>`
					: `<input type="text" class="form-control io-name" value="${esc(opt.name)}" aria-label="${__('Option')}">`}</div>
				<div class="ffl-io-chips"></div>
				<div>${opt.fixed ? '' : `<span class="ffl-io-remove" title="${__('Remove')}">×</span>`}</div>
			</div>`).appendTo($box);
			const $chips = $row.find('.ffl-io-chips');
			opt.values.forEach((v, vi) => {
				const removable = !opt.fixed || v.isNew;
				$(`<span class="ffl-io-chip" draggable="true" data-vi="${vi}">${esc(v.value)}${removable ? '<span class="io-x">×</span>' : ''}</span>`)
					.appendTo($chips);
			});
			const $value = $(`<input type="text" class="io-value" aria-label="${esc(opt.name || __('Values'))}">`).appendTo($chips);
			suggest($value[0], () => opt.name);
			wireValueInput(frm, opt, $value);
			wireChips(frm, opt, oi, $chips);
			if (!opt.fixed) {
				const $name = $row.find('.io-name');
				suggest($name[0]);
				const commit = () => {
					const name = $name.val().replace(/\s+/g, ' ').trim();
					if (name === opt.name) return;
					if (st.options.some((o, i) => i !== oi && K.fold(o.name) === K.fold(name))) {
						$name.addClass('io-bad');
						return;
					}
					$name.removeClass('io-bad');
					opt.name = name;
					opt.values.forEach((v) => { v.code = ''; });
					refreshCodes(frm, opt);
				};
				$name.on('change', commit);
				$name[0].addEventListener('awesomplete-selectcomplete', commit);
				$row.find('.ffl-io-remove').on('click', () => {
					st.options.splice(oi, 1);
					if (!st.options.length) st.options.push({ name: '', fixed: false, values: [] });
					renderOptions(frm);
					syncRows(frm);
					renderGrid(frm);
				});
			}
		});
		if (frm.is_new() && st.options.length < K.MAX_OPTIONS) {
			$(`<button type="button" class="btn btn-xs btn-default ffl-io-add">${__('+ Option')}</button>`)
				.appendTo($box).on('click', () => {
					st.options.push({ name: '', fixed: false, values: [] });
					renderOptions(frm);
					$box.find('.io-name').last().trigger('focus');
				});
		}
	}

	function addValue(frm, opt, raw) {
		const value = String(raw || '').replace(/\s+/g, ' ').trim();
		if (!value || value.length > 140) return false;
		if (opt.values.some((v) => K.fold(v.value) === K.fold(value))) return false;
		opt.values.push({ value, code: '', isNew: true });
		markOptionsChanged(frm);
		return true;
	}

	function markOptionsChanged(frm) {
		const st = state(frm);
		if (!frm.is_new()) {
			st.optionsDirty = true;
			frm.dirty();
		}
	}

	function wireValueInput(frm, opt, $input) {
		const commit = () => {
			const parts = $input.val().split(',');
			let added = false;
			parts.forEach((p) => { added = addValue(frm, opt, p) || added; });
			$input.val('');
			if (added) {
				renderOptions(frm);
				syncRows(frm);
				renderGrid(frm);
				refreshCodes(frm, opt);
				const oi = state(frm).options.indexOf(opt);
				wrapper(frm).find(`.ffl-io-option[data-oi="${oi}"] .io-value`).trigger('focus');
			}
		};
		$input.on('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ',') {
				// Awesomplete takes Enter while its list is open and a row is picked.
				if (e.key === 'Enter' && $input.closest('.awesomplete').find('li[aria-selected="true"]').length) return;
				e.preventDefault();
				commit();
			} else if (e.key === 'Backspace' && !$input.val() && opt.values.length) {
				const last = opt.values[opt.values.length - 1];
				if (!opt.fixed || last.isNew) {
					opt.values.pop();
					renderOptions(frm);
					syncRows(frm);
					renderGrid(frm);
					wrapper(frm).find(`.ffl-io-option[data-oi="${state(frm).options.indexOf(opt)}"] .io-value`).trigger('focus');
				}
			}
		});
		$input[0].addEventListener('awesomplete-selectcomplete', commit);
	}

	function wireChips(frm, opt, oi, $chips) {
		const st = state(frm);
		$chips.on('click', '.io-x', (e) => {
			const vi = Number($(e.target).closest('.ffl-io-chip').data('vi'));
			opt.values.splice(vi, 1);
			renderOptions(frm);
			syncRows(frm);
			renderGrid(frm);
		});
		$chips.on('dragstart', '.ffl-io-chip', (e) => {
			st.drag = { oi, vi: Number($(e.currentTarget).data('vi')) };
			$(e.currentTarget).addClass('io-dragging');
			e.originalEvent.dataTransfer.effectAllowed = 'move';
			e.originalEvent.dataTransfer.setData('text/plain', String(st.drag.vi));
		});
		$chips.on('dragend', '.ffl-io-chip', (e) => { $(e.currentTarget).removeClass('io-dragging'); });
		$chips.on('dragover', '.ffl-io-chip', (e) => {
			if (!st.drag || st.drag.oi !== oi) return;
			e.preventDefault();
			$chips.find('.ffl-io-chip').removeClass('io-over');
			$(e.currentTarget).addClass('io-over');
		});
		$chips.on('drop', '.ffl-io-chip', (e) => {
			if (!st.drag || st.drag.oi !== oi) return;
			e.preventDefault();
			const to = Number($(e.currentTarget).data('vi'));
			const [moved] = opt.values.splice(st.drag.vi, 1);
			opt.values.splice(to, 0, moved);
			st.drag = null;
			markOptionsChanged(frm);
			renderOptions(frm);
			renderGrid(frm);
		});
	}

	function renderGrid(frm) {
		const st = state(frm);
		const $grid = wrapper(frm) && wrapper(frm).find('.io-grid');
		if (!$grid || !$grid.length) return;
		const rows = orderedRows(st);
		if (!rows.length) { $grid.empty(); return; }
		const on = rows.filter((r) => r.include).length;
		const slots = st.preorder !== false;
		const body = rows.map((r) => {
			const label = esc(r.values.join(' / '));
			const sku = r.item_code
				? `<a href="/app/item/${encodeURIComponent(r.item_code)}">${esc(r.item_code)}</a>`
				: `<input type="text" class="form-control io-sku" value="${esc(r.sku)}" aria-label="${__('SKU')}">`;
			const cls = [!r.include ? 'io-off' : '', !r.item_code && !frm.is_new() ? 'io-new' : ''].join(' ');
			return `<tr data-key="${esc(r.key)}" class="${cls}">
				<td class="io-chk"><input type="checkbox" class="io-include" ${r.include ? 'checked' : ''} aria-label="${label}"></td>
				<td class="io-label">${label}</td>
				<td class="io-sku-td">${sku}</td>
				<td class="io-price-td" data-label="${__('Price')}"><input type="number" min="0" step="0.01" class="form-control io-n io-cell" data-f="price" value="${esc(r.price)}" aria-label="${__('Price')}"></td>
				<td class="io-cost-td" data-label="${__('Cost')}"><input type="number" min="0" step="0.01" class="form-control io-n io-cell" data-f="cost" value="${esc(r.cost)}" aria-label="${__('Cost')}"></td>
				${slots ? `<td class="io-slots-td" data-label="${__('Pre-order')}"><input type="number" min="0" step="1" class="form-control io-slots io-cell" data-f="preorder" value="${esc(r.preorder)}" aria-label="${__('Pre-order')}"></td>` : ''}
			</tr>`;
		}).join('');
		$grid.html(`<table class="ffl-io-grid">
			<colgroup><col class="io-c-chk"><col><col><col class="io-c-n"><col class="io-c-n">${slots ? '<col class="io-c-slots">' : ''}</colgroup>
			<thead>
				<tr><th class="io-chk"><input type="checkbox" class="io-all" ${on === rows.length ? 'checked' : ''} aria-label="${__('All')}"></th>
					<th class="io-count">${__('{0} variants', [on])}</th><th>${__('SKU')}</th><th>${__('Price')}</th><th>${__('Cost')}</th>
					${slots ? `<th>${__('Pre-order')}</th>` : ''}</tr>
				<tr class="io-fill"><th></th><th></th><th></th>
					<th class="io-fill-price-th" data-label="${__('Price')}"><input type="number" min="0" step="0.01" class="form-control io-n io-fill-price" value="${esc(st.fill.price)}" aria-label="${__('Price for all')}"></th>
					<th></th>
					${slots ? `<th class="io-fill-slots-th" data-label="${__('Pre-order')}"><input type="number" min="0" step="1" class="form-control io-slots io-fill-slots" value="${esc(st.fill.preorder)}" aria-label="${__('Pre-order for all')}"><input type="date" class="form-control io-eta" value="${esc(st.fill.eta || '')}" aria-label="${__('Pre-order ETA')}"></th>` : ''}
				</tr>
			</thead><tbody>${body}</tbody></table>`);
		wireGrid(frm, $grid);
		markErrors(frm);
	}

	function markErrors(frm) {
		const $w = wrapper(frm);
		if (!$w) return {};
		const errs = errors(frm);
		$w.find('.ffl-io-grid tbody tr').each((_i, tr) => {
			const e = errs[tr.dataset.key] || {};
			$(tr).find('.io-sku').toggleClass('io-bad', !!e.sku).attr('title', e.sku ? __(e.sku) : null);
			['price', 'cost', 'preorder'].forEach((f) => {
				$(tr).find(`.io-cell[data-f="${f}"]`).toggleClass('io-bad', !!e[f]).attr('title', e[f] ? __(e[f]) : null);
			});
		});
		return errs;
	}

	// An existing row saves itself; the panel shows the saved values back.
	function saveRows(frm, changes, $cells) {
		const st = state(frm);
		return frappe.xcall(M + 'update_option_rows', { template: frm.doc.name, rows: changes })
			.then((data) => {
				const fill = st.fill;
				const pending = pendingRows(st);
				applyState(frm, data);
				st.fill = Object.assign(st.fill, { price: fill.price, preorder: fill.preorder });
				pending.forEach((r) => { if (!st.rows[r.key]) st.rows[r.key] = r; });
				syncRows(frm);
				renderGrid(frm);
				($cells || []).forEach((sel) => {
					const $c = wrapper(frm).find(sel).addClass('io-saved');
					setTimeout(() => $c.removeClass('io-saved'), 700);
				});
			})
			.catch(() => { syncRows(frm); renderGrid(frm); });
	}

	function wireGrid(frm, $grid) {
		const st = state(frm);
		$grid.off(); // the container outlives each render: one set of handlers
		const rowOf = (el) => st.rows[$(el).closest('tr').data('key')];
		$grid.on('input', '.io-sku', (e) => {
			const r = rowOf(e.target);
			r.sku = e.target.value;
			r.skuEdited = true;
			checkTaken(frm);
			markErrors(frm);
		});
		$grid.on('input', '.io-cell', (e) => {
			const r = rowOf(e.target);
			r[e.target.dataset.f] = e.target.value;
			if (!r.item_code) markErrors(frm);
		});
		$grid.on('change', '.io-cell', (e) => {
			const r = rowOf(e.target);
			if (!r.item_code) return;
			const f = e.target.dataset.f;
			const change = { item_code: r.item_code, [f]: num(e.target.value) || 0 };
			if (f === 'preorder' && num(e.target.value) > 0 && st.fill.eta) change.eta = st.fill.eta;
			const key = $(e.target).closest('tr').data('key');
			saveRows(frm, [change], [`tr[data-key="${key}"] .io-cell[data-f="${f}"]`]);
		});
		$grid.on('change', '.io-include', (e) => {
			const r = rowOf(e.target);
			r.include = e.target.checked;
			if (r.item_code) saveRows(frm, [{ item_code: r.item_code, include: r.include ? 1 : 0 }]);
			else {
				if (!frm.is_new()) frm.dirty();
				renderGrid(frm);
				checkTaken(frm);
			}
		});
		$grid.on('change', '.io-all', (e) => {
			const on = e.target.checked;
			const saved = [];
			Object.values(st.rows).forEach((r) => {
				if (r.include === on) return;
				r.include = on;
				if (r.item_code) saved.push({ item_code: r.item_code, include: on ? 1 : 0 });
			});
			if (saved.length) saveRows(frm, saved);
			else renderGrid(frm);
		});
		const fill = (field, value) => {
			const saved = [];
			Object.values(st.rows).forEach((r) => {
				if (!r.include) return;
				r[field] = value;
				if (r.item_code) {
					const change = { item_code: r.item_code, [field]: num(value) || 0 };
					if (field === 'preorder') change.eta = num(value) > 0 ? (st.fill.eta || null) : null;
					saved.push(change);
				}
			});
			if (saved.length) saveRows(frm, saved);
			else renderGrid(frm);
		};
		$grid.on('change', '.io-fill-price', (e) => { st.fill.price = e.target.value; fill('price', e.target.value); });
		$grid.on('change', '.io-fill-slots', (e) => { st.fill.preorder = e.target.value; fill('preorder', e.target.value); });
		$grid.on('change', '.io-eta', (e) => {
			st.fill.eta = e.target.value || '';
			const saved = Object.values(st.rows).filter((r) => r.item_code && r.include && Number(r.preorder) > 0)
				.map((r) => ({ item_code: r.item_code, eta: st.fill.eta || null }));
			if (saved.length) saveRows(frm, saved);
		});
	}

	// ---- a row of an item that comes in options -----------------------------------------------
	// A slim line above the tabs (the form's message slot is the stock line's): the item it
	// belongs to, linked, and its values.
	function variantLine(frm) {
		frm.layout.wrapper.find('.ffl-io-variant').remove();
		if (!frm.doc.variant_of) return;
		const values = (frm.doc.attributes || []).map((a) => a.attribute_value).filter(Boolean);
		const template = frm.doc.variant_of;
		frappe.db.get_value('Item', template, 'item_name').then((r) => {
			if (frm.doc.variant_of !== template) return;
			const name = (r && r.message && r.message.item_name) || template;
			frm.layout.wrapper.find('.ffl-io-variant').remove();
			const $line = $(`<div class="ffl-io-variant"><a href="/app/item/${encodeURIComponent(template)}">${esc(name)}</a>`
				+ (values.length ? `<span>${esc(values.join(' / '))}</span>` : '') + '</div>');
			const $target = frm.layout.wrapper.find('.form-tabs-list, .form-layout').first();
			if ($target.length) $target.before($line); else frm.layout.wrapper.prepend($line);
		});
	}

	// ---- Save ----------------------------------------------------------------------------------
	function blockedByErrors(frm) {
		const errs = markErrors(frm);
		if (Object.keys(errs).length) {
			frappe.msgprint({ message: __('Fix the cells marked in red.'), indicator: 'red' });
			return true;
		}
		return false;
	}

	function call(method, args) {
		return new Promise((resolve) => {
			frappe.call({ method: M + method, args, freeze: true,
				callback: (r) => resolve(r && r.message), error: () => resolve(null) });
		});
	}

	function saveNew(frm) {
		const st = state(frm);
		frappe.validated = false; // the standard insert never runs: the server makes everything
		if (!st.options.length || st.options.some((o) => !o.name || !o.values.length)) {
			frappe.msgprint({ message: __('Give each option a name and at least one value.'), indicator: 'red' });
			return;
		}
		if (!pendingRows(st).length) {
			frappe.msgprint({ message: __('Tick at least one combination.'), indicator: 'red' });
			return;
		}
		if (blockedByErrors(frm)) return;
		// The template's own attribute rows (mandatory once it has variants); the server writes them again.
		frm.doc.attributes = [];
		st.options.forEach((o) => frm.add_child('attributes', { attribute: o.name }));
		if (!frappe.ui.form.check_mandatory(frm)) return;
		const rows = orderedRows(st).map((r) => rowPayload(st, r));
		return call('create_item_with_options', { item: frm.doc, options: optionsPayload(st), rows }).then((m) => {
			if (!m) return;
			frappe.show_alert({ message: __('Saved {0} with {1} variants', [m.template, m.variants.length]), indicator: 'green' });
			frm.doc.__unsaved = 0;
			frappe.model.remove_from_locals(frm.doctype, frm.docname);
			frappe.set_route('Form', 'Item', m.template);
		});
	}

	function savePanel(frm) {
		const st = state(frm);
		const pending = pendingRows(st);
		if (!pending.length && !st.optionsDirty) return;
		if (blockedByErrors(frm)) { frappe.validated = false; return; }
		return call('update_option_rows', { template: frm.doc.name, options: optionsPayload(st),
			rows: pending.map((r) => rowPayload(st, r)) }).then((data) => {
			if (!data) { frappe.validated = false; return; }
			applyState(frm, data);
			if (data.created && data.created.length) {
				frappe.show_alert({ message: __('Added {0} variants', [data.created.length]), indicator: 'green' });
			}
		});
	}

	// ---- Group existing items ------------------------------------------------------------------
	function groupDialog(frm) {
		const st = state(frm);
		Promise.all(st.options.map((o) => frappe.xcall(M + 'option_suggestions', { option: o.name, txt: '', limit: 500 })))
			.then((lists) => {
				const d = new frappe.ui.Dialog({
					title: __('Group existing items'),
					size: 'large',
					fields: [{
						fieldname: 'items', fieldtype: 'Table', cannot_add_rows: false, in_place_edit: true, data: [],
						fields: [{ fieldname: 'item_code', fieldtype: 'Link', options: 'Item', label: __('Item'), in_list_view: 1,
							reqd: 1, columns: 4,
							get_query: () => ({ filters: { has_variants: 0, variant_of: ['is', 'not set'], name: ['!=', frm.doc.name] } }) }]
							.concat(st.options.map((o, i) => ({ fieldname: `o${i}`, fieldtype: 'Select', label: o.name,
								options: [''].concat((lists[i] || []).map((r) => r.value)).join('\n'), in_list_view: 1, reqd: 1, columns: 2 }))),
					}],
					primary_action_label: __('Group'),
					primary_action(values) {
						const variants = {};
						(values.items || []).forEach((row) => {
							if (!row.item_code) return;
							variants[row.item_code] = {};
							st.options.forEach((o, i) => { variants[row.item_code][o.name] = row[`o${i}`] || ''; });
						});
						call('group_items', { template: frm.doc.name, variants }).then((m) => {
							if (!m) return;
							if (!m.ok) {
								frappe.msgprint({ title: __('Not grouped'), indicator: 'red',
									message: m.errors.map(esc).join('<br>') });
								return;
							}
							d.hide();
							frappe.show_alert({ message: __('Grouped {0} items', [m.variants.length]), indicator: 'green' });
							st.loaded = false;
							render(frm);
						});
					},
				});
				d.show();
			});
	}

	frappe.ui.form.on('Item', {
		refresh(frm) {
			render(frm);
			if (!frm.is_new() && frm.doc.has_variants) {
				frm.page.add_menu_item(__('Group existing items'), () => groupDialog(frm));
			}
		},
		item_code(frm) {
			if (frm.is_new() && frm.doc.has_variants) {
				syncRows(frm);
				renderGrid(frm);
				checkTaken(frm);
			}
		},
		before_save(frm) {
			if (!frm.doc.has_variants) return;
			return frm.is_new() ? saveNew(frm) : savePanel(frm);
		},
	});
})();
