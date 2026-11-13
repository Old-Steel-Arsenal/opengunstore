// Serial No image preview — show this gun's Photo + every Photo Gallery image.
//
// Reuses the shared renderers (window.gunstore.*, in ffl_core.bundle.js) so the
// Serial No form gets the same visual treatment as the Item form: a hero strip
// at the top (big current image + clickable thumbs) plus inline thumbnails in
// the Photo Gallery grid. Without this the Media fields render only as file
// paths, so the photos aren't visible.
//
// (The FastBound-sync confirm lives in ffl_integrations' own Serial No script;
// both register on 'Serial No' and load together.)

frappe.ui.form.on('Serial No', {
	refresh(frm) {
		_renderImages(frm);
		_addCorrectSerialButton(frm);
		_addCorrectCostButton(frm);
		_showDealHold(frm);
	},

	// Re-render the strip live when the Photo is changed/cleared.
	image(frm) {
		_renderImages(frm);
	},

	// A photo added/removed in the gallery grid should reflect in the strip.
	image_gallery(frm) {
		_renderImages(frm);
	},
});

// Manager-only "Correct Serial" button → the shared correction dialog (defined in
// ffl_core.bundle.js, the same one the Item form's Serial Numbers tab uses). Only
// offered for an in-stock (Active) gun; the server hard-rejects the rest, but
// there's no point tempting the operator with a button that can't succeed.
function _addCorrectSerialButton(frm) {
	const g = window.gunstore;
	if (!g || !g.correctSerialDialog || !g.canCorrectSerial) return;
	if (frm.is_new() || !g.canCorrectSerial()) return;
	if ((frm.doc.status || '') !== 'Active') return;
	frm.add_custom_button(__('Correct Serial'), () => {
		g.correctSerialDialog(frm.doc.name, { onDone: () => frm.reload_doc() });
	});
}

// Manager-only "Correct Cost" button → the SAME dialog and the SAME
// server method the Item page uses (window.gunstore.correctCostDialog), just
// pinned to this one gun. Offered only for an in-stock gun the store owns; the
// server re-checks all of it, and the dialog explains itself if the gun turns
// out not to be eligible.
function _addCorrectCostButton(frm) {
	const g = window.gunstore;
	if (!g || !g.correctCostDialog || !g.canCorrectSerial) return;
	if (frm.is_new() || !g.canCorrectSerial()) return;
	if ((frm.doc.status || '') !== 'Active') return;
	if (frm.doc.is_consignment) return;  // consigned TO us — never ours to revalue
	frm.add_custom_button(__('Correct Cost'), () => {
		g.correctCostDialog({
			item_code: frm.doc.item_code,
			serial: frm.doc.name,
			onDone: () => frm.reload_doc(),
		});
	});
}

function _renderImages(frm) {
	const g = window.gunstore;
	if (!g) return;
	if (g.customizeImageGalleryGrid) g.customizeImageGalleryGrid(frm);
	// Same photo manager as the Item form; this gun's own shots live here.
	if (g.renderPhotoManager) g.renderPhotoManager(frm, { sectionField: 'ffl_media_section' });
}

// A gun held for a CRM deal (ffl_core/crm/holds.py): who for and until when, linked to the deal.
// Released on the Deal page or at the counter, so no button here.
function _showDealHold(frm) {
	if (frm.is_new() || (frm.doc.status || '') !== 'Active') return;
	// The endpoint's own roles (COUNTER_ROLES): anyone else would get a permission pop-up.
	if (!frappe.user.has_role(['Sales User', 'Sales Manager', 'Stock Manager', 'System Manager'])) return;
	frappe.xcall('ffl_core.api.crm.serial_hold', { serial: frm.doc.name }).then((r) => {
		const h = r && r.hold;
		if (!h || frm.doc.name !== h.serial) return;
		const esc = frappe.utils.escape_html;
		const deal = `<a href="/crm/deals/${encodeURIComponent(h.deal)}">${esc(h.deal)}</a>`;
		frm.set_intro(__('Held for deal {0} ({1}) until {2}.', [deal, esc(h.customer_name || ''), esc(h.expires_label)]), 'orange');
	}).catch(() => { /* no counter role: nothing to show */ });
}
