// Upload Photos by Serial — pick an Item, get one card per serial, drop several
// photos on the gun they belong to. Uploads go through the same Frappe uploader
// and the same server path (ffl_core.api.photos.add_gallery_images) as the form
// photo manager; this page only adds the "many guns at once" layout. Ordering /
// captions / removal stay on the Serial No form to keep this page small.
frappe.pages['serial-photos'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Upload Photos by Serial'),
		single_column: true,
	});
	window.gunstore && window.gunstore.ensurePhotoStyles && window.gunstore.ensurePhotoStyles();
	$('<style>').text(`
		.sp-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; margin-top: 12px; }
		.sp-card { border: 1px solid var(--border-color); border-radius: 8px; background: var(--card-bg); padding: 10px; }
		.sp-card.is-dragover .pm-drop { border-color: var(--primary); }
		.sp-card .sp-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
		.sp-card .sp-serial { font-weight: 600; }
		.sp-card .sp-meta { color: var(--text-muted); font-size: 12px; }
		.sp-card .sp-thumbs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; min-height: 8px; }
		.sp-card .sp-thumbs img { width: 56px; height: 56px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border-color); }
		.sp-card .sp-thumbs .sp-more { width: 56px; height: 56px; display: flex; align-items: center; justify-content: center; border-radius: 6px; border: 1px dashed var(--border-color); color: var(--text-muted); font-size: 12px; }
		.sp-body { padding: 0 15px 24px; }
		.sp-empty { color: var(--text-muted); margin-top: 16px; }
	`).appendTo(page.body);

	const state = { item: null, in_stock_only: 1, search: '' };
	const item = page.add_field({
		fieldtype: 'Link', options: 'Item', label: __('Item'), fieldname: 'item_code',
		get_query: () => ({ filters: { has_serial_no: 1 } }),
		change() { state.item = item.get_value(); load(); },
	});
	// Text filters side by side, the checkbox last (no gap between the two inputs).
	const search = page.add_field({
		fieldtype: 'Data', label: __('Serial contains'), fieldname: 'search',
		change() { state.search = search.get_value() || ''; load(); },
	});
	const only = page.add_field({
		fieldtype: 'Check', label: __('In stock only'), fieldname: 'in_stock_only', default: 1,
		change() { state.in_stock_only = only.get_value() ? 1 : 0; load(); },
	});
	const $body = $('<div class="sp-body"></div>').appendTo(page.body);
	const escape = frappe.utils.escape_html;

	function load() {
		if (!state.item) {
			$body.html(`<div class="sp-empty">${__('Pick an Item to list its serial numbers.')}</div>`);
			return;
		}
		frappe.call({
			method: 'ffl_core.api.photos.serials_for_item',
			args: { item_code: state.item, in_stock_only: state.in_stock_only, search: state.search },
		}).then((r) => render(r.message || []));
	}

	function render(rows) {
		if (!rows.length) {
			$body.html(`<div class="sp-empty">${__('No serials match.')}</div>`);
			return;
		}
		const cards = rows.map((s) => {
			const thumbs = (s.thumbs || []).map((u) => `<img src="${escape(u)}" alt="" loading="lazy">`).join('')
				+ (s.photo_count > (s.thumbs || []).length
					? `<div class="sp-more">+${s.photo_count - s.thumbs.length}</div>` : '');
			return `
				<div class="sp-card" data-serial="${escape(s.serial_no)}">
					<div class="sp-head">
						<a class="sp-serial" href="/app/serial-no/${encodeURIComponent(s.serial_no)}">${escape(s.serial_no)}</a>
						<span class="sp-meta">${escape(s.status || '')}${s.warehouse ? ' · ' + escape(s.warehouse) : ''} · ${s.photo_count} ${__('photos')}</span>
					</div>
					<div class="sp-thumbs">${thumbs}</div>
					<div class="pm-drop" role="button" tabindex="0">
						<div>${__('Drop this gun\'s photos here, or click to choose')}</div>
					</div>
				</div>`;
		}).join('');
		$body.html(`<div class="sp-cards">${cards}</div>`);
	}

	function add(serial, files) {
		window.gunstore.pickPhotos({ doctype: 'Serial No', docname: serial, files }).then((urls) => {
			if (!urls.length) return;
			frappe.call({
				method: 'ffl_core.api.photos.add_gallery_images',
				args: { doctype: 'Serial No', name: serial, file_urls: urls },
				freeze: true,
			}).then(() => {
				frappe.show_alert({ message: __('{0}: {1} photo(s) added', [serial, urls.length]), indicator: 'green' });
				load();
			});
		});
	}

	$body.on('click', '.pm-drop', function () { add($(this).closest('.sp-card').data('serial')); });
	$body.on('keydown', '.pm-drop', function (e) {
		if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); add($(this).closest('.sp-card').data('serial')); }
	});
	$body.on('dragover dragenter', '.sp-card', function (e) { e.preventDefault(); $(this).addClass('is-dragover'); });
	$body.on('dragleave dragend drop', '.sp-card', function () { $(this).removeClass('is-dragover'); });
	$body.on('drop', '.sp-card', function (e) {
		e.preventDefault();
		const files = [...((e.originalEvent.dataTransfer || {}).files || [])];
		if (files.length) add($(this).data('serial'), files);
	});

	// Arriving from an Item form's "Upload by serial…" button.
	const routed = frappe.route_options && frappe.route_options.item_code;
	if (routed) { frappe.route_options = null; item.set_value(routed); } else { load(); }
};
