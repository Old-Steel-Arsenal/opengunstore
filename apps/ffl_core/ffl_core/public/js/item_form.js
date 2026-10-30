// Item-form RSR quick-add.
//
// Two entry points to the same lookup_for_item_form RPC:
//
//   1. INLINE BANNER on a brand-new Item form. Auto-focuses a scan input
//      above the form layout; live typeahead resolves UPC / RSR stock # /
//      MFG #. Click the result (or scan an exact UPC) to fill the entire
//      form: item_code, item_name, item_group, manufacturer/model, prices
//      (selling=MAP/MSRP, dealer cost), description, image, image_gallery
//      primary row, UPC barcode child row, firearm_type, is_used.
//
//   2. TOP-LEVEL BUTTON ("Scan UPC / RSR") visible on saved items so the
//      operator can backfill missing fields from the catalog any time.
//
// On a saved Item, the apply path only touches empty fields so user edits
// are never overwritten. On a new Item, defaults flow freely.

frappe.ui.form.on('Item', {
	onload(frm) {
		if (frm.is_new() && !frm.doc.item_code) {
			ensureScanBanner(frm);
		}
	},
	refresh(frm) {
		// Fallback button — top-level (no group dropdown) so it's always one click
		frm.add_custom_button(__('Scan UPC / RSR'), () => openScanDialog(frm));

		// Stock-modification buttons. Skipped for new / non-stock /
		// serial-tracked items (those go through Receive Goods so the FFL
		// flow runs and each unit gets its own Serial No record).
		if (!frm.is_new() && frm.doc.is_stock_item && !frm.doc.has_serial_no) {
			const $addStock = frm.add_custom_button(__('+ Add Stock'),
				() => openAddStockDialog(frm));
			$addStock.removeClass('btn-default').addClass('btn-success');

			frm.add_custom_button(__('Adjust Quantity'),
				() => openAdjustQtyDialog(frm));
		}

		// Correct Cost — per-serial firearm cost correction (Stock Reconciliation
		// + Serial No.purchase_rate). Serial-tracked firearms only, managers only.
		// The server (ffl_core.api.cost_correction) re-enforces every gate.
		if (!frm.is_new() && frm.doc.is_firearm && frm.doc.has_serial_no &&
			(frappe.user.has_role('Stock Manager') || frappe.user.has_role('System Manager'))) {
			frm.add_custom_button(__('Correct Cost'), () => openCorrectCostDialog(frm));
		}

		if (!frm.is_new() && frappe.user.has_role('System Manager')) {
			const $btn = frm.add_custom_button(__('Force Delete'), () => {
				if (typeof window.gunStoreForceDeleteItems === 'function') {
					window.gunStoreForceDeleteItems([frm.doc.name]);
				}
			});
			$btn.removeClass('btn-default').addClass('btn-danger');
		}
		// Live stock indicator above the form (saved Items only — new items
		// have no stock yet by definition).
		if (!frm.is_new() && frm.doc.is_stock_item) {
			showStockIndicator(frm);
		}
		// Photo manager at the very top of the form (multi-upload, order, featured).
		window.gunstore.renderPhotoManager(frm, { batchRoute: !!frm.doc.has_serial_no });
		window.gunstore.customizeImageGalleryGrid(frm);
	},

	// Uploading via the standard top-right image widget should immediately show
	// the photo in the gallery below (the server `validate` hook also does this
	// on save — this is just instant feedback). The featured slot is unchanged:
	// the new photo is added non-primary; featured is controlled by is_primary.
	image(frm) {
		const img = (frm.doc.image || '').trim();
		if (!img) return;
		const exists = (frm.doc.image_gallery || [])
			.some((r) => (r.image || '').trim() === img);
		if (exists) return;
		frm.add_child('image_gallery', { image: img, is_primary: 0 });
		frm.refresh_field('image_gallery');
		window.gunstore.renderPhotoManager(frm, { batchRoute: !!frm.doc.has_serial_no });
	},
});


// Make the gallery's `is_primary` checkbox behave like a radio: ticking one
// row clears the others, so there's always exactly one featured photo. The
// server hook enforces the same invariant on save (covers RSR / API writes).
frappe.ui.form.on('Item Image Gallery', {
	is_primary(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.is_primary) return; // only react when a row is ticked ON
		(frm.doc.image_gallery || []).forEach((r) => {
			if (r.name !== cdn) r.is_primary = 0;
		});
		frm.refresh_field('image_gallery');
	},
});




// --- Quick "+ Add Stock" dialog (Material Receipt Stock Entry) -----------

// Pre-fill a stock dialog's warehouse from the first POS Profile of the user's company.
function prefillPosWarehouse(d) {
	const company = frappe.defaults.get_user_default('Company');
	if (!company) return;
	frappe.db.get_list('POS Profile', {
		filters: { company, disabled: 0 },
		fields: ['warehouse'],
		limit: 1,
	}).then((rows) => {
		const wh = rows && rows[0] && rows[0].warehouse;
		if (wh) d.set_value('warehouse', wh);
	});
}

function openAddStockDialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Add Stock — {0}', [frm.doc.item_name || frm.doc.name]),
		size: 'small',
		fields: [
			{
				label: __('Quantity'), fieldname: 'qty', fieldtype: 'Float',
				reqd: 1, default: 1,
			},
			{
				label: __('Warehouse'), fieldname: 'warehouse', fieldtype: 'Link',
				options: 'Warehouse', reqd: 1,
				get_query: () => ({ filters: { is_group: 0, disabled: 0 } }),
			},
			{
				label: __('Cost / Rate'), fieldname: 'rate', fieldtype: 'Currency',
				default: frm.doc.last_purchase_rate || frm.doc.dealer_price || 0,
				description: __('Defaults to the last purchase rate, then the RSR dealer price.'),
			},
			{
				label: __('Remarks'), fieldname: 'remarks', fieldtype: 'Small Text',
			},
		],
		primary_action_label: __('Add to Stock'),
		primary_action(values) {
			frappe.call({
				method: 'ffl_core.api.item_admin.add_stock',
				args: {
					item_code: frm.doc.name,
					qty: values.qty,
					warehouse: values.warehouse,
					rate: values.rate || null,
					remarks: values.remarks || null,
				},
				freeze: true,
				freeze_message: __('Adding stock…'),
				callback: (r) => {
					const m = r.message || {};
					if (!m.ok) return;
					d.hide();
					frappe.show_alert({
						message: __('Added {0} unit(s) · Stock Entry {1}', [m.qty, m.name]),
						indicator: 'green',
					});
					// Refresh the headline indicator without a full doc reload.
					showStockIndicator(frm);
				},
			});
		},
	});

	prefillPosWarehouse(d);

	d.show();
	setTimeout(() => d.fields_dict.qty.input?.focus(), 60);
}


// --- "Adjust Quantity" dialog (Stock Reconciliation) ---------------------

function openAdjustQtyDialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Adjust Quantity — {0}', [frm.doc.item_name || frm.doc.name]),
		size: 'small',
		fields: [
			{
				label: __('Set Quantity To'), fieldname: 'new_qty', fieldtype: 'Float',
				reqd: 1, default: frm.doc.actual_qty || 0,
				description: __('Sets the absolute on-hand quantity at the chosen warehouse.'),
			},
			{
				label: __('Warehouse'), fieldname: 'warehouse', fieldtype: 'Link',
				options: 'Warehouse', reqd: 1,
				get_query: () => ({ filters: { is_group: 0, disabled: 0 } }),
			},
			{
				label: __('Reason'), fieldname: 'reason', fieldtype: 'Small Text',
				description: __('For example "Annual physical count" or "Write-off — damaged".'),
				reqd: 1,
			},
		],
		primary_action_label: __('Update Quantity'),
		primary_action(values) {
			frappe.call({
				method: 'ffl_core.api.item_admin.set_stock',
				args: {
					item_code: frm.doc.name,
					new_qty: values.new_qty,
					warehouse: values.warehouse,
					reason: values.reason,
				},
				freeze: true,
				freeze_message: __('Adjusting…'),
				callback: (r) => {
					const m = r.message || {};
					if (!m.ok) return;
					d.hide();
					const sign = m.delta > 0 ? '+' : '';
					frappe.show_alert({
						message: __('Set to {0} ({1}{2}) · Stock Reconciliation {3}',
							[m.new_qty, sign, m.delta, m.name]),
						indicator: 'green',
					});
					showStockIndicator(frm);
				},
			});
		},
	});

	prefillPosWarehouse(d);

	d.show();
	setTimeout(() => d.fields_dict.new_qty.input?.focus(), 60);
}


// The Item-page "Correct Cost" entry point. The dialog itself lives in
// ffl_core.bundle.js (window.gunstore.correctCostDialog) because the Serial No
// form and the Firearms In Stock report open the same one — see the note there.

function openCorrectCostDialog(frm) {
	const g = window.gunstore;
	if (!g || !g.correctCostDialog) return;
	g.correctCostDialog({ item_code: frm.doc.name, onDone: () => frm.reload_doc() });
}


// --- Live stock indicator (for saved stock Items) -------------------------

function showStockIndicator(frm) {
	frappe.call({
		method: 'ffl_core.api.item_admin.get_item_stock',
		args: { item_codes: JSON.stringify([frm.doc.name]) },
		callback: (r) => {
			const stock = r.message || {};
			const qty = Number(stock[frm.doc.name] || 0);
			const n = Math.round(qty * 100) / 100;
			const color = qty > 0 ? 'green' : 'orange';
			frm.dashboard.clear_headline();
			frm.dashboard.set_headline_alert(
				`<div style="display:flex; align-items:center; gap:10px">
					<span style="font-weight:600">${__('Stock on hand')}: ${n}</span>
					<a href="/app/stock-balance?item_code=${encodeURIComponent(frm.doc.name)}"
						target="_blank" style="font-size:11px">${__('View Stock Balance →')}</a>
				</div>`,
				color
			);
		},
	});
}


// --- Inline scan banner (the primary new-item flow) ------------------------

function ensureScanBanner(frm) {
	if (frm._scanBanner) {
		frm._scanBanner.toggle(frm.is_new() && !frm.doc.item_code);
		return;
	}

	const $banner = $(`
		<div class="rsr-quick-add" style="
			background: var(--subtle-fg);
			border: 1px solid var(--border-color);
			border-radius: 8px;
			padding: 14px 16px;
			margin: 0 0 18px 0;
			box-shadow: 0 1px 3px rgba(0,0,0,0.04);
		">
			<div style="display:flex; align-items:center; gap:10px; margin-bottom:10px">
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
					stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--text-muted)">
					<rect x="3" y="3" width="6" height="18" rx="0"></rect>
					<rect x="11" y="3" width="2" height="18" rx="0"></rect>
					<rect x="15" y="3" width="6" height="18" rx="0"></rect>
				</svg>
				<div style="font-weight:600; font-size:13px">${__('Quick Add — Scan UPC / RSR Stock # / MFG #')}</div>
				<div style="flex:1"></div>
				<button class="btn btn-xs btn-default rsr-scan-skip" type="button">${__('Skip')}</button>
			</div>
			<input type="text"
				class="form-control rsr-scan-input"
				placeholder="${__('Scan barcode or type 3+ characters to search RSR catalog…')}"
				style="font-size:14px; padding:8px 12px;"
				autocomplete="off"
				spellcheck="false">
			<div class="rsr-scan-results" style="margin-top:8px"></div>
			<div class="rsr-scan-help" style="color:var(--text-muted); font-size:11px; margin-top:6px">
				${__('Tip: scan a UPC and the form fills automatically. Press <b>Esc</b> or click <b>Skip</b> to enter the item manually.')}
			</div>
		</div>
	`);
	frm._scanBanner = $banner;

	// Inject before the form fields. The .form-layout container is created
	// by Frappe; prepending there puts the banner above all sections without
	// disrupting tab/accordion behaviour.
	const $target = frm.layout.wrapper.find('.form-layout, .form-tabs-list').first();
	if ($target.length) {
		$target.before($banner);
	} else {
		frm.layout.wrapper.prepend($banner);
	}

	const $input = $banner.find('.rsr-scan-input');
	const $results = $banner.find('.rsr-scan-results');
	window.gunstore.wireRsrScan($input, $results, {
		cardSize: {thumb: 72, name: 13, meta: 12},
		decorate: ($card) => {
			$card.addClass('rsr-scan-row').css({transition: 'background .15s, border-color .15s'});
			$card.append(`<div style="color:var(--text-muted); font-size:11px; align-self:center; padding-right:4px">${__('Click or ↵ Enter')}</div>`);
			$card.hover(
				function () { $(this).css({background: 'var(--fg-hover-color)', borderColor: 'var(--blue-300)'}); },
				function () { $(this).css({background: 'var(--card-bg)', borderColor: 'var(--border-color)'}); }
			);
		},
		noMatchHtml: (q) => `
			<div style="color:var(--text-muted); font-size:12px; padding:10px; text-align:center; border:1px dashed var(--border-color); border-radius:6px">
				${__('No RSR catalog match for {0}.', [frappe.utils.escape_html(q)])}
				${__('Continue typing or fill the form manually below.')}
			</div>
		`,
		onApply: (m) => applyAndCollapse(frm, $banner, m),
	});

	// Esc = enter the item manually (Enter is the shared scan box's)
	$input.on('keydown', function (e) {
		if (e.key === 'Escape') {
			$banner.remove();
			frm._scanBanner = null;
		}
	});

	$banner.find('.rsr-scan-skip').on('click', () => {
		$banner.remove();
		frm._scanBanner = null;
	});

	setTimeout(() => $input.focus(), 100);
}

function applyAndCollapse(frm, $banner, m) {
	applyDefaults(frm, m.defaults || {}, m.rsr || {});
	$banner.fadeOut(180, () => {
		$banner.remove();
		frm._scanBanner = null;
	});
	frappe.show_alert({
		message: __('Filled from RSR · {0}', [m.rsr?.rsr_stock_number || '']),
		indicator: 'green',
	});
}


// --- Modal dialog (re-scan path for saved items) --------------------------

function openScanDialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Lookup from RSR Catalog'),
		size: 'small',
		fields: [
			{
				fieldtype: 'Data',
				fieldname: 'scan',
				label: __('UPC / RSR Stock # / MFG #'),
				reqd: 1,
			},
			{ fieldtype: 'HTML', fieldname: 'preview' },
		],
		primary_action_label: __('Apply to Form'),
		primary_action() {
			const v = (d.get_value('scan') || '').trim();
			if (!v) return;
			if (ffl.FirearmItemPicker.catalogOff()) {
				d.fields_dict.preview.$wrapper.html(ffl.capabilities.notice('catalog'));
				return;
			}
			frappe.call({
				method: 'ffl_integrations.rsr.promote.lookup_for_item_form',
				args: { scan: v },
				callback: (r) => {
					const m = r.message || {};
					if (!m.found) {
						frappe.show_alert({ message: __('No RSR row matched: {0}', [v]), indicator: 'orange' });
						return;
					}
					applyDefaults(frm, m.defaults || {}, m.rsr || {});
					d.hide();
					frappe.show_alert({
						message: __('Filled from RSR · {0}', [m.rsr?.rsr_stock_number || v]),
						indicator: 'green',
					});
				},
			});
		},
	});
	d.fields_dict.scan.df.change = () => livePreview(d);
	d.fields_dict.scan.df.onchange = () => livePreview(d);
	d.show();
	setTimeout(() => d.fields_dict.scan.input?.focus(), 50);
}

function livePreview(d) {
	const $p = d.fields_dict.preview.$wrapper;
	const v = (d.get_value('scan') || '').trim();
	if (ffl.FirearmItemPicker.catalogOff()) { $p.html(ffl.capabilities.notice('catalog')); return; }
	if (v.length < 3) { $p.empty(); return; }
	frappe.call({
		method: 'ffl_integrations.rsr.promote.lookup_for_item_form',
		args: { scan: v },
		callback: (r) => {
			const m = r.message || {};
			if (!m.found) {
				$p.html(`<div style="color:var(--text-muted);font-size:12px;padding:8px 0">${__('No match.')}</div>`);
				return;
			}
			const $card = $(window.gunstore.renderRsrCard(m.rsr || {}, m.defaults || {},
				{thumb: 80, name: 13, meta: 12})).css({marginTop: '8px'});
			$p.empty().append($card);
		},
	});
}


function applyDefaults(frm, defaults, rsr) {
	window.gunstore.applyRsrToDoc(frm, defaults, rsr, {overwriteNonEmpty: frm.is_new()});
}
