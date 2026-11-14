// My tasks — every open task assigned to you (Gun Shop sidebar, near the top). A task stays here
// until you press Done; Done closes it the way Frappe's own assignment close does and leaves
// "<you> completed this task" on the document. No role list: anyone can be assigned a task.
// Rules: ffl_core/assignment_notice.py (my_tasks, done). The `mytk` prefix keeps clear of the other
// desk pages' globals.

const MYTK_API = 'ffl_core.assignment_notice.';

if (typeof frappe !== 'undefined') {
	frappe.pages['my-tasks'].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __('My tasks'), single_column: true });
		wrapper.mytk = new MyTasksPage(page);
	};
	frappe.pages['my-tasks'].on_page_show = function (wrapper) {
		if (wrapper.mytk) wrapper.mytk.load();
	};
}

class MyTasksPage {
	constructor(page) {
		this.page = page;
		this.$body = $(`<div class="mytk" style="padding:4px 16px 24px">
			<style>
				.mytk .mytk-row { display:flex; gap:12px; align-items:flex-start; padding:12px 0; border-bottom:1px solid var(--border-color); }
				.mytk .mytk-main { flex:1 1 auto; min-width:0; }
				.mytk .mytk-desc { margin-top:4px; white-space:pre-wrap; overflow-wrap:anywhere; }
				.mytk .mytk-row button { flex:0 0 auto; }
				.mytk .mytk-empty { color:var(--text-muted); margin-top:16px; }
			</style>
			<div class="mytk-list"></div>
		</div>`).appendTo(page.body);
		this.$list = this.$body.find('.mytk-list');
		this.$list.on('click', '.mytk-done', (e) => this.done($(e.currentTarget)));
	}

	load() {
		frappe.call({ method: MYTK_API + 'my_tasks' }).then((r) => this.render(r.message || []));
	}

	count(n) {
		if (n) this.page.set_indicator(__('{0} open', [n]), 'orange');
		else this.page.clear_indicator();
		if (window.ffl && ffl.assignmentNotice && ffl.assignmentNotice.setCount) ffl.assignmentNotice.setCount(n);
	}

	render(rows) {
		this.count(rows.length);
		if (!rows.length) {
			this.$list.html(`<div class="mytk-empty">${__('No open tasks.')}</div>`);
			return;
		}
		const esc = (s) => frappe.utils.escape_html(s == null ? '' : String(s));
		this.$list.html(rows.map((r) => {
			const doc = r.reference_type && r.reference_name
				? `<div><a href="/app/${esc(frappe.router.slug(r.reference_type))}/${esc(encodeURIComponent(r.reference_name))}">${esc(__(r.reference_type))}: ${esc(r.title || r.reference_name)}</a></div>`
				: '';
			const seen = r.seen_on
				? `<span class="text-muted">· ${esc(__('Seen {0}', [frappe.datetime.prettyDate(r.seen_on)]))}</span>`
				: (r.asks ? `<span class="indicator-pill orange">${esc(__('Not seen'))}</span>` : '');
			return `<div class="mytk-row" data-name="${esc(r.name)}">
				<div class="mytk-main">
					<div><b>${esc(r.assigned_by)}</b> <span class="text-muted">${esc(frappe.datetime.prettyDate(r.creation))}</span>
						${r.priority === 'High' ? `<span class="indicator-pill red">${esc(__('High'))}</span>` : ''} ${seen}</div>
					${doc}
					${r.description ? `<div class="mytk-desc">${esc(r.description)}</div>` : ''}
				</div>
				<button class="btn btn-primary btn-sm mytk-done">${esc(__('Done'))}</button>
			</div>`;
		}).join(''));
	}

	done($btn) {
		const $row = $btn.closest('.mytk-row');
		$btn.prop('disabled', true);
		frappe.call({ method: MYTK_API + 'done', type: 'POST', args: { name: $row.attr('data-name') } }).then((r) => {
			$row.remove();
			const left = this.$list.find('.mytk-row').length;
			if (!left) this.$list.html(`<div class="mytk-empty">${__('No open tasks.')}</div>`);
			this.count(r.message ? r.message.open : left);
		}, () => $btn.prop('disabled', false));
	}
}
