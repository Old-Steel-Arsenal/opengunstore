// Memberships — one page for the store's memberships (Gun Shop sidebar → Customers).
//
// Totals, the Needs attention list (managers), the plans and the member list. Selling, taking
// payment, retrying a card, cancelling and dropping go through window.ffl.memberships — the same
// code as the Customer form's Memberships panel (public/js/customer_memberships.js). A new plan is
// three fields; saving it makes the plan's billed item (Membership Plan validate).
// Rules: ffl_core/membership.py. The `mshp` prefix keeps clear of the other desk pages' globals.

const MSHP_API = 'ffl_core.membership.';
const MSHP_STATUSES = ['Active', 'Past Due', 'Suspended', 'Pending', 'Cancelled', 'Expired'];

if (typeof frappe !== 'undefined') {
	frappe.pages['memberships'].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __('Memberships'), single_column: true });
		wrapper.memberships_app = new MembershipsPage(page);
	};
	frappe.pages['memberships'].on_page_show = function (wrapper) {
		if (wrapper.memberships_app) wrapper.memberships_app.load();
	};
}

const mshpEsc = (s) => frappe.utils.escape_html(s == null ? '' : String(s));
const mshpDate = (d) => (d ? frappe.datetime.str_to_user(d) : '');
const mshpPill = (status) => {
	const colors = (window.ffl && window.ffl.memberships && window.ffl.memberships.STATUS_COLOR) || {};
	return `<span class="indicator-pill ${colors[status] || 'gray'}">${mshpEsc(__(status))}</span>`;
};

class MembershipsPage {
	constructor(page) {
		this.page = page;
		this.data = null;
		this.filters = { search: '', plan: '', status: 'Active' };
		this.start = 0;
		page.set_primary_action(__('Sell membership'), () => this.sell());
		page.add_button(__('Renewal settings'), () => frappe.set_route('Form', 'Membership Settings'));
		this.$body = $(`<div class="mshp" style="padding:4px 16px 24px">
			<style>
				.mshp .mshp-tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }
				.mshp .mshp-tile { border:1px solid var(--border-color); border-radius:var(--border-radius-md); padding:10px 14px; background:var(--card-bg); }
				.mshp .mshp-tile .v { font-size:22px; font-weight:600; }
				.mshp .mshp-tile.alert-on { border-color:var(--red-500, #e24c4c); }
				.mshp .mshp-tile.alert-on .v, .mshp .mshp-tile.alert-on .text-muted { color:var(--red-600, #c53030) !important; }
				.mshp h5 { margin:20px 0 8px; font-weight:600; }
				.mshp .mshp-plans { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:12px; }
				.mshp .mshp-plan { border:1px solid var(--border-color); border-radius:var(--border-radius-md); padding:10px 14px; background:var(--card-bg); }
				.mshp .mshp-plan.editable { cursor:pointer; }
				.mshp .mshp-plan.editable:hover { border-color:var(--gray-500); }
				.mshp .mshp-plan.off { opacity:.6; }
				.mshp .mshp-plan.new { border-style:dashed; display:flex; align-items:center; justify-content:center; color:var(--text-muted); }
				.mshp .mshp-members tbody tr { cursor:pointer; }
				.mshp .mshp-controls { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px; }
				.mshp .mshp-controls > * { flex:1 1 100px; max-width:220px; min-width:0; }
				@media (max-width:480px) { .mshp .mshp-controls > input { flex-basis:100%; max-width:none; } }
				.mshp .mshp-scroll { overflow-x:auto; }
			</style>
			<div class="mshp-tiles"></div>
			<div class="mshp-attention"></div>
			<div class="mshp-plans-wrap"></div>
			<div class="mshp-members-wrap"></div>
		</div>`).appendTo(page.main);
		this.$body.on('click', '[data-act]', (e) => this.onAct($(e.currentTarget)));
	}

	async load() {
		let out;
		try {
			out = await frappe.xcall(MSHP_API + 'page_overview');
		} catch (e) { return; }
		this.data = out;
		if (out.can_edit_plans && !this.newPlanButton) {
			this.newPlanButton = this.page.add_button(__('New plan'), () => this.planDialog());
		}
		this.renderTiles();
		this.renderAttention();
		this.renderPlans();
		if (!this.$members) this.renderMemberControls();
		this.loadMembers(true);
	}

	activePlans() {
		return ((this.data && this.data.plans) || []).filter((p) => p.active);
	}

	renderTiles() {
		const t = this.data.totals;
		const tile = (label, value, alert) => `<div class="mshp-tile ${alert ? 'alert-on' : ''}">
			<div class="text-muted small">${mshpEsc(label)}</div><div class="v">${value}</div></div>`;
		this.$body.find('.mshp-tiles').html(
			tile(__('Active members'), t.active)
			+ tile(__('Monthly billing'), format_currency(t.monthly))
			+ tile(__('Renewing in 7 days'), t.renewing_soon)
			+ (t.attention == null ? '' : tile(__('Needs attention'), t.attention, t.attention > 0)));
	}

	renderAttention() {
		const $w = this.$body.find('.mshp-attention').empty();
		const att = this.data.attention;
		if (!att) return;
		const rows = att.rows || [];
		if (!att.renewals_on && this.data.plans.length) {
			$w.append(`<div class="alert alert-warning" style="margin-bottom:8px">${mshpEsc(__('Renewals are not charged automatically. Turn on Charge renewals automatically in Renewal settings.'))}</div>`);
		}
		if (!rows.length) return;
		$w.append(`<h5>${mshpEsc(__('Needs attention'))}</h5>`);
		const btn = (act, i, label) => `<button class="btn btn-xs btn-default" style="margin-left:4px" data-act="${act}" data-row="${i}">${mshpEsc(label)}</button>`;
		const $t = $(`<table class="table table-sm"><thead><tr>
			<th>${mshpEsc(__('Customer'))}</th><th>${mshpEsc(__('Plan'))}</th><th>${mshpEsc(__('Why'))}</th><th>${mshpEsc(__('Invoice'))}</th><th></th>
			</tr></thead><tbody></tbody></table>`);
		rows.forEach((row, i) => {
			const pending = row.status === 'Pending';
			// can_cancel is false on a live membership only while its card charge waits for the gateway.
			const waiting = !pending && !row.can_cancel;
			const buttons = (row.can_retry ? btn('retry', i, waiting ? __('Look up card charge') : __('Retry card')) : '')
				+ (!pending && row.can_cancel ? btn('pay', i, __('Take payment')) : '')
				+ (pending && row.can_drop ? btn('collect', i, __('Take payment')) + btn('drop', i, __('Drop')) : '')
				+ (row.can_cancel ? btn('cancel', i, __('Cancel')) : '');
			$t.find('tbody').append(`<tr>
				<td><a href="/app/customer/${encodeURIComponent(row.customer)}">${mshpEsc(row.customer_name || row.customer)}</a></td>
				<td>${mshpEsc(row.plan)} ${mshpPill(row.status)}</td>
				<td>${mshpEsc(row.reason)}${row.detail ? `<div class="text-muted small">${mshpEsc(row.detail)}</div>` : ''}</td>
				<td>${row.sales_invoice ? `<a href="/app/sales-invoice/${encodeURIComponent(row.sales_invoice)}">${mshpEsc(row.sales_invoice)}</a> ${format_currency(row.outstanding)}` : ''}</td>
				<td class="text-right" style="white-space:nowrap">${buttons}</td></tr>`);
		});
		$w.append($('<div class="mshp-scroll"></div>').append($t));
	}

	renderPlans() {
		const $w = this.$body.find('.mshp-plans-wrap').empty();
		const plans = this.data.plans || [];
		const canEdit = this.data.can_edit_plans;
		if (!plans.length) {
			$w.append(`<div class="text-center" style="padding:40px 0">
				<h4>${mshpEsc(__('Start your first membership plan'))}</h4>
				${canEdit ? `<button class="btn btn-primary btn-sm" data-act="new-plan">${mshpEsc(__('New plan'))}</button>`
					: `<p class="text-muted">${mshpEsc(__('Ask a manager to create one.'))}</p>`}
			</div>`);
			return;
		}
		const period = (p) => (p.billing_period === 'Yearly' ? __('year') : __('month'));
		const grace = (p) => (p.grace_enabled ? __('grace {0} days', [p.grace_days]) : __('no grace'));
		$w.append(`<h5>${mshpEsc(__('Plans'))}</h5>`);
		const $g = $('<div class="mshp-plans"></div>').appendTo($w);
		plans.forEach((p, i) => {
			$g.append(`<div class="mshp-plan ${canEdit ? 'editable' : ''} ${p.active ? '' : 'off'}" ${canEdit ? `data-act="edit-plan" data-plan="${i}"` : ''}>
				<div style="font-weight:600">${mshpEsc(p.name)}${p.active ? '' : ` <span class="indicator-pill gray">${mshpEsc(__('Off'))}</span>`}</div>
				<div class="text-muted small">${format_currency(p.price)} / ${mshpEsc(period(p))} · ${mshpEsc(grace(p))}</div>
				<div style="margin-top:4px">${mshpEsc(p.members === 1 ? __('1 member') : __('{0} members', [p.members]))}</div></div>`);
		});
		if (canEdit) $g.append(`<div class="mshp-plan new editable" data-act="new-plan">+ ${mshpEsc(__('New plan'))}</div>`);
	}

	renderMemberControls() {
		const $w = this.$body.find('.mshp-members-wrap');
		$w.append(`<h5>${mshpEsc(__('Members'))}</h5>`);
		const $c = $('<div class="mshp-controls"></div>').appendTo($w);
		const $search = $(`<input type="search" class="form-control input-sm" placeholder="${mshpEsc(__('Name or phone'))}">`).appendTo($c);
		this.$planFilter = $('<select class="form-control input-sm"></select>').appendTo($c);
		const $status = $(`<select class="form-control input-sm"><option value="">${mshpEsc(__('Every status'))}</option>
			${MSHP_STATUSES.map((s) => `<option value="${mshpEsc(s)}" ${s === this.filters.status ? 'selected' : ''}>${mshpEsc(__(s))}</option>`).join('')}</select>`).appendTo($c);
		this.$members = $(`<table class="table table-sm mshp-members"><thead><tr>
			<th>${mshpEsc(__('Member'))}</th><th>${mshpEsc(__('Plan'))}</th><th>${mshpEsc(__('Status'))}</th>
			<th>${mshpEsc(__('Next charge'))}</th><th>${mshpEsc(__('Card'))}</th></tr></thead><tbody></tbody></table>`)
			.appendTo($('<div class="mshp-scroll"></div>').appendTo($w));
		this.$more = $(`<button class="btn btn-xs btn-default" style="display:none">${mshpEsc(__('Show more'))}</button>`).appendTo($w);
		$search.on('input', frappe.utils.debounce(() => { this.filters.search = $search.val(); this.loadMembers(true); }, 300));
		this.$planFilter.on('change', () => { this.filters.plan = this.$planFilter.val(); this.loadMembers(true); });
		$status.on('change', () => { this.filters.status = $status.val(); this.loadMembers(true); });
		this.$more.on('click', () => this.loadMembers(false));
		this.$members.on('click', 'tbody tr[data-customer]', (e) => frappe.set_route('Form', 'Customer', $(e.currentTarget).attr('data-customer')));
	}

	async loadMembers(reset) {
		const plans = (this.data && this.data.plans) || [];
		this.$body.find('.mshp-members-wrap').toggle(plans.length > 0);
		if (!plans.length) return;
		const current = this.filters.plan;
		this.$planFilter.html(`<option value="">${mshpEsc(__('Every plan'))}</option>`
			+ plans.map((p) => `<option value="${mshpEsc(p.name)}" ${p.name === current ? 'selected' : ''}>${mshpEsc(p.name)}</option>`).join(''));
		if (reset) this.start = 0;
		// Only the latest request draws: a slower earlier search must not land under a newer filter.
		const ticket = (this.ticket = (this.ticket || 0) + 1);
		let out;
		try {
			out = await frappe.xcall(MSHP_API + 'page_members', Object.assign({ start: this.start }, this.filters));
		} catch (e) { return; }
		if (ticket !== this.ticket) return;
		const $tb = this.$members.find('tbody');
		if (reset) $tb.empty();
		(out.rows || []).forEach((m) => {
			const next = m.ends_on ? __('Ends {0}', [mshpDate(m.ends_on)])
				: m.next_charge_date ? mshpDate(m.next_charge_date)
					: m.auto_renew ? '' : __('Auto-renew off');
			$tb.append(`<tr data-customer="${mshpEsc(m.customer)}">
				<td>${mshpEsc(m.customer_name || m.customer)}</td><td>${mshpEsc(m.plan)}</td><td>${mshpPill(m.status)}</td>
				<td>${mshpEsc(next)}</td><td class="text-muted">${mshpEsc(m.card_summary)}</td></tr>`);
		});
		if (reset && !(out.rows || []).length) {
			$tb.append(`<tr><td colspan="5" class="text-muted">${mshpEsc(__('No members match.'))}</td></tr>`);
		}
		this.start += (out.rows || []).length;
		this.$more.toggle(!!out.more);
	}

	sell() {
		const plans = this.activePlans();
		if (!plans.length) {
			frappe.msgprint(this.data && this.data.can_edit_plans ? __('Create a plan first: New plan.') : __('No membership plan is on sale yet.'));
			return;
		}
		window.ffl.memberships.act('sell', { refresh: () => this.load() }, null, { plans });
	}

	onAct($b) {
		const what = $b.attr('data-act');
		if (what === 'new-plan') return this.planDialog();
		if (what === 'edit-plan') return this.planDialog(this.data.plans[Number($b.attr('data-plan'))]);
		const row = ((this.data.attention && this.data.attention.rows) || [])[Number($b.attr('data-row'))];
		if (!row) return;
		const ctx = { customer: row.customer, customer_name: row.customer_name, refresh: () => this.load() };
		window.ffl.memberships.act(what, ctx, { name: row.membership, current_period_end: row.current_period_end });
	}

	// New plan: name, period and price; the rest folded under More options. Saving makes the
	// plan's billed item (and the registration fee's, when there is a fee).
	planDialog(plan) {
		const editing = !!plan;
		const d = new frappe.ui.Dialog({
			title: editing ? __('Plan {0}', [plan.name]) : __('New plan'),
			fields: [
				{ fieldtype: 'Data', fieldname: 'plan_name', label: __('Plan name'), reqd: 1, read_only: editing ? 1 : 0, default: editing ? plan.name : '' },
				{ fieldtype: 'Select', fieldname: 'billing_period', label: __('Billed'), reqd: 1, options: 'Monthly\nYearly',
					default: editing ? plan.billing_period : 'Monthly', read_only: editing && plan.members ? 1 : 0 },
				{ fieldtype: 'Currency', fieldname: 'price', label: __('Price per period'), reqd: 1, default: editing ? plan.price : null },
				...(editing ? [{ fieldtype: 'Check', fieldname: 'active', label: __('On sale'), default: plan.active,
					description: __('Off: no new sign-ups. Current members stay as they are.') }] : []),
				{ fieldtype: 'Section Break', label: __('More options'), collapsible: 1 },
				{ fieldtype: 'Currency', fieldname: 'registration_fee', label: __('Registration fee'), default: editing ? plan.registration_fee : 0,
					description: __('Charged once, on the first invoice.') },
				{ fieldtype: 'Check', fieldname: 'auto_renew_default', label: __('Auto-renew by default'), default: editing ? plan.auto_renew_default : 1 },
				{ fieldtype: 'Check', fieldname: 'grace_enabled', label: __('Grace period when a renewal is declined'), default: editing ? plan.grace_enabled : 0 },
				{ fieldtype: 'Int', fieldname: 'grace_days', label: __('Grace days'), depends_on: 'grace_enabled', default: editing ? plan.grace_days : 7 },
				{ fieldtype: 'Link', fieldname: 'terms_template', label: __('Membership terms'), options: 'Agreement Template',
					default: editing ? plan.terms_template : '', get_query: () => ({ filters: { scope: 'Per document', active: 1 } }),
					description: __('Signed on the tablet for each new membership.') },
				{ fieldtype: 'Small Text', fieldname: 'description', label: __('Description'), default: editing ? plan.description : '' },
				{ fieldtype: 'HTML', fieldname: 'tax_note', options: `<p class="text-muted small">${mshpEsc(__('Tax-exempt plan? Set an exempt tax template on the plan\'s item after saving.'))}</p>` },
			],
			primary_action_label: editing ? __('Save') : __('Create plan'),
			primary_action: async (v) => {
				const fields = { billing_period: v.billing_period, price: v.price, registration_fee: v.registration_fee || 0,
					auto_renew_default: v.auto_renew_default ? 1 : 0, grace_enabled: v.grace_enabled ? 1 : 0,
					grace_days: v.grace_enabled ? v.grace_days : 0, terms_template: v.terms_template || null, description: v.description || null };
				try {
					if (editing) {
						fields.active = v.active ? 1 : 0;
						await frappe.xcall('frappe.client.set_value', { doctype: 'Membership Plan', name: plan.name, fieldname: fields });
					} else {
						await frappe.xcall('frappe.client.insert', { doc: Object.assign({ doctype: 'Membership Plan', plan_name: v.plan_name, active: 1 }, fields) });
					}
				} catch (e) { return; /* the server message already showed; the dialog stays open */ }
				d.hide();
				frappe.show_alert({ message: editing ? __('Plan saved') : __('Plan created'), indicator: 'green' });
				this.load();
			},
		});
		d.show();
	}
}
