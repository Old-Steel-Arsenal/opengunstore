// Overview — the Gun Shop's landing page: today's sales, the forms and guns waiting on someone, the
// cash drawer, the bound book, what is arriving, and every list in one place.
//
// One call (ffl_core.api.store_overview.get_overview) brings every block; a block this user may not
// see comes back null and is left out, one that failed says so in its own card. The page asks again
// every minute while it is on screen.
//
// The functions below are a PURE KERNEL (no DOM, no frappe, no __()), unit-tested under Node in
// tests/js/store_overview.test.js. The `sov` prefix keeps them clear of the other desk pages, which
// share one global scope.

const SOV_OLD_PICKUP_DAYS = 21;
const SOV_ROWS_4473 = 5;
const SOV_WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const SOV_MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

function sovMoney(n) {
	const v = Math.round((Number(n) || 0) * 100) / 100;
	const s = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	return (v < 0 ? '-$' : '$') + s;
}

function sovEsc(text) {
	return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => (
		{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// "2026-10-08" -> Date at local noon (no timezone slip to the day before).
function sovDate(text) {
	const m = /(\d{4})-(\d{2})-(\d{2})/.exec(String(text || ''));
	return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12) : null;
}

// "2026-10-08" -> "THU · OCT 8"
function sovEyebrow(today) {
	const d = sovDate(today);
	if (!d) return '';
	return `${SOV_WEEKDAYS[d.getDay()].slice(0, 3).toUpperCase()} · ${SOV_MONTHS[d.getMonth()]} ${d.getDate()}`;
}

function sovWeekday(today) {
	const d = sovDate(today);
	return d ? SOV_WEEKDAYS[d.getDay()] : '';
}

// "2026-10-08 14:03:11.12" -> "2:03 PM"; a day other than today -> "Yesterday" / "Oct 6".
function sovStarted(when, today) {
	const m = /(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(when || ''));
	if (!m) return '';
	const day = `${m[1]}-${m[2]}-${m[3]}`;
	if (day !== today) {
		const t = sovDate(today);
		const y = t ? new Date(t.getFullYear(), t.getMonth(), t.getDate() - 1, 12) : null;
		const d = sovDate(day);
		if (y && d && y.getTime() === d.getTime()) return 'Yesterday';
		return d ? `${SOV_MONTHS[d.getMonth()].charAt(0)}${SOV_MONTHS[d.getMonth()].slice(1).toLowerCase()} ${d.getDate()}` : day;
	}
	const h = Number(m[4]);
	return `${h % 12 || 12}:${m[5]} ${h < 12 ? 'AM' : 'PM'}`;
}

// How long ago a server datetime was, against the server's now: "just now", "4 min ago", "3 h ago", "2 days ago".
function sovAgo(when, now) {
	const parse = (t) => {
		const m = /(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(String(t || ''));
		return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6] || 0)) : null;
	};
	const a = parse(when);
	const b = parse(now);
	if (!a || !b) return '';
	const min = Math.floor((b - a) / 60000);
	if (min < 1) return 'just now';
	if (min < 60) return `${min} min ago`;
	if (min < 60 * 24) return `${Math.floor(min / 60)} h ago`;
	const days = Math.floor(min / (60 * 24));
	return `${days} day${days === 1 ? '' : 's'} ago`;
}

function sovInitials(name) {
	const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
	if (!parts.length) return '?';
	return ((parts[0][0] || '') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

// ---- Open 4473s

// No NICS result yet: a new order is stamped 'N/A' (pos_4473.start_4473, the field's default);
// older rows can be blank.
function sovNoNics(nics) {
	return !nics || nics === 'N/A';
}

// The page's four tabs over the Pending 4473 Orders rows, by NICS result.
function sovNicsState(nics) {
	if (nics === 'Proceed') return 'Ready';
	if (nics === 'Delayed') return 'Delayed';
	if (sovNoNics(nics)) return 'In progress';
	return ''; // Denied / Cancelled: only under All
}

function sovNicsPill(nics) {
	if (nics === 'Proceed') return { cls: 'ok', label: 'Proceed' };
	if (nics === 'Delayed') return { cls: 'warn', label: 'Delayed' };
	if (nics === 'Denied') return { cls: 'bad', label: 'Denied' };
	if (sovNoNics(nics)) return { cls: 'mute', label: 'No NICS result' };
	return { cls: 'mute', label: nics };
}

const SOV_TABS = ['All', 'Ready', 'Delayed', 'In progress'];

function sovTabs(rows) {
	return SOV_TABS.map((t) => ({
		key: t,
		n: t === 'All' ? (rows || []).length : (rows || []).filter((r) => sovNicsState(r.nics_status) === t).length,
	}));
}

// A tab's rows, newest order first (the report lists oldest first), five at most.
function sovTabRows(rows, tab, limit) {
	const picked = tab === 'All' ? (rows || []) : (rows || []).filter((r) => sovNicsState(r.nics_status) === tab);
	const newest = picked.slice().sort((a, b) => String(b.started || '').localeCompare(String(a.started || '')));
	return newest.slice(0, limit == null ? SOV_ROWS_4473 : limit);
}

// The button on a row: what the counter does next with that order.
function sovRowAction(nics) {
	if (nics === 'Proceed') return 'Complete';
	if (sovNoNics(nics)) return 'Continue';
	return 'View';
}

// "Glock 19 Gen5" for one gun, "Glock 19 Gen5 +1" for two; the serials, comma-joined.
function sovGunLabel(guns) {
	const g = guns || [];
	if (!g.length) return { name: '', serials: '' };
	const first = g[0].item_name || g[0].serial || '';
	return { name: g.length > 1 ? `${first} +${g.length - 1}` : first, serials: g.map((x) => x.serial).join(', ') };
}

// ---- pickups

function sovDaysClass(days) {
	return Number(days) > SOV_OLD_PICKUP_DAYS ? 'old' : '';
}

// ---- sales

// The four sales figures and the bars from the per-day list (oldest first, today last, eight days).
// Sales today = the report's Net Sales (returns included); Transactions and Avg. ticket count the
// sales only (the server leaves returns out of transactions / ticket_net). Last week's same day is
// shown as its whole-day figure, not as a change: today is only part of a day.
function sovSales(days) {
	const list = days || [];
	const today = list[list.length - 1] || {};
	const lastWeek = list.length >= 8 ? list[list.length - 8] : null;
	const tx = Number(today.transactions) || 0;
	const last = lastWeek ? Math.round((Number(lastWeek.net_sales) || 0) * 100) / 100 : 0;
	return {
		net: Number(today.net_sales) || 0,
		tx,
		avg: tx ? Math.round(((Number(today.ticket_net) || 0) / tx) * 100) / 100 : 0,
		lastWeek: last,                     // 0 = no sales that day (or no data)
		bars: sovBars(list.slice(-7).map((d) => Number(d.net_sales) || 0)),
	};
}

// All records is asked for until it has arrived without an error.
function sovNeedRecords(records) {
	return !records || !!records.error;
}

// Bar heights in px (4..36) for the seven-day strip; a day with no sales is the 4px stub.
function sovBars(values) {
	const max = Math.max(0, ...values);
	return values.map((v) => (max > 0 && v > 0 ? Math.max(6, Math.round((v / max) * 36)) : 4));
}

// ---- arriving

// What an order's row says on the right: units received so far as a bar once something came in,
// otherwise the order's state. The distributor order carries no arrival date.
function sovArrival(o) {
	const got = Number(o.received) || 0;
	const of = Number(o.expected) || 0;
	if (got > 0 && of > 0) return { kind: 'progress', got, of, pct: Math.min(100, Math.round((got / of) * 100)) };
	if (o.status === 'Partially Shipped' || o.status === 'Complete') return { kind: 'status', label: 'Shipped' };
	if (o.status === 'Hold') return { kind: 'status', label: 'On hold' };
	return { kind: 'status', label: o.status || '' };
}

// ---- header sentence

function sovPlural(n, one, many) {
	return `${n} ${n === 1 ? one : many}`;
}

// "3 forms open, 5 transfers waiting on pickup, and 2 distributor orders arriving." -- from the blocks
// this user has; a block they do not have (null), that failed, or that has nothing waiting is not
// mentioned. When every block they have is clear, it says so.
function sovSummary(d) {
	const ok = (b) => b && !b.error;
	const parts = [];
	let asked = false;
	const add = (block, n, one, many, tail) => {
		if (!ok(block)) return;
		asked = true;
		if (n) parts.push(`${sovPlural(n, one, many)} ${tail}`);
	};
	add(d.open_4473, d.open_4473 && d.open_4473.rows && d.open_4473.rows.length, 'form', 'forms', 'open');
	add(d.pickups, d.pickups && d.pickups.total, 'transfer', 'transfers', 'waiting on pickup');
	add(d.arriving, d.arriving && d.arriving.total, 'distributor order', 'distributor orders', 'arriving');
	if (ok(d.ad) && !d.ad.off) add(d.ad, (d.ad.retrying || 0) + (d.ad.needs_person || 0), 'A&D record', 'A&D records', 'not in the bound book yet');
	if (!parts.length) return asked ? 'Nothing is waiting on anyone right now.' : '';
	const text = parts.length === 1 ? parts[0]
		: `${parts.slice(0, -1).join(', ')}${parts.length > 2 ? ',' : ''} and ${parts[parts.length - 1]}`;
	return `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
}

// ---- all records

function sovFilterRecords(groups, q) {
	const needle = String(q || '').trim().toLowerCase();
	return (groups || [])
		.map((g) => ({ title: g.title, items: needle ? g.items.filter((i) => i.label.toLowerCase().includes(needle)) : g.items }))
		.filter((g) => g.items.length);
}

// A desk route for a link, without the shell: the router writes the shell in on arrival.
function sovRoute(linkType, linkTo) {
	const slug = String(linkTo || '').toLowerCase().replace(/ /g, '-');
	if (linkType === 'DocType') return `/desk/${slug}`;
	if (linkType === 'Report') return `/desk/query-report/${encodeURIComponent(linkTo)}`;
	if (linkType === 'Page') return `/desk/${linkTo}`;
	if (linkType === 'Workspace') return `/desk/${slug}`;
	return '';
}

// An All records link's href: a URL as it is, anything else through Frappe's own route builder
// (``gen`` = frappe.utils.generate_route; passed in so Node can test this). '' = not routable.
const SOV_ROUTED = ['DocType', 'Report', 'Page', 'Dashboard', 'Workspace'];
function sovRecordHref(item, gen) {
	if (item.link_type === 'URL') return String(item.link_to || '');
	if (!SOV_ROUTED.includes(item.link_type)) return '';
	return gen({
		type: item.link_type, name: item.link_to, public: true,
		is_query_report: !!item.is_query_report, report_ref_doctype: item.ref_doctype || null,
	});
}

// Guarded so Node can require the kernel above. Harmless in the browser, where frappe is always defined.
if (typeof frappe !== 'undefined') {
	frappe.pages['store-overview'].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __('Dashboard'), single_column: true });
		wrapper.store_overview = new StoreOverview(page);
	};
	frappe.pages['store-overview'].on_page_show = function (wrapper) {
		if (wrapper.store_overview) wrapper.store_overview.refresh();
	};
}

const SOV_API = 'ffl_core.api.store_overview.get_overview';
const SOV_REFRESH_MS = 60 * 1000;
const SOV_CSS = `
	.sov { container-type: inline-size; padding: 4px 16px 64px; }
	.sov .sov-body { display: flex; flex-direction: column; gap: 16px; }
	.sov .sov-records { display: flex; flex-direction: column; gap: 16px; margin-top: 16px; }
	.sov .sov-eyebrow { font-size: 11px; letter-spacing: .06em; color: var(--text-muted); font-weight: 500; }
	.sov .sov-lede { color: var(--text-muted); margin-top: 2px; }
	.sov .sov-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; min-width: 0; }
	.sov .sov-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
	.sov .sov-kpi { display: flex; align-items: flex-end; gap: 12px; padding: 14px 16px 13px; }
	.sov .sov-kpit { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
	.sov .sov-kpil { color: var(--text-muted); }
	.sov .sov-kpiv { font-size: 28px; line-height: 1.15; font-weight: 600; letter-spacing: -.02em;
		font-variant-numeric: tabular-nums; margin-top: 4px; color: var(--heading-color, var(--text-color)); }
	.sov .sov-kpis2 { font-size: 12px; color: var(--text-muted); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
	.sov .sov-up { color: var(--green-600); } .sov .sov-down { color: var(--red-600); }
	.sov .sov-bars { margin-left: auto; display: flex; align-items: flex-end; gap: 3px; height: 36px; flex: none; }
	.sov .sov-bar { width: 6px; border-radius: 2px; background: var(--border-color); }
	.sov .sov-bar.now { background: var(--text-color); }
	.sov .sov-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: none; }
	.sov .sov-okc { color: var(--green-600); } .sov .sov-warnc { color: var(--orange-600); } .sov .sov-mutec { color: var(--gray-500); }
	.sov .sov-grid { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 16px; align-items: start; }
	.sov .sov-grid.sov-one { grid-template-columns: minmax(0, 1fr); }
	.sov .sov-col { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
	.sov .sov-ch { display: flex; align-items: flex-start; gap: 12px; padding: 14px 16px 12px; }
	.sov .sov-ct { font-weight: 600; font-size: 14px; line-height: 1.3; color: var(--heading-color, var(--text-color)); }
	.sov .sov-cs { color: var(--text-muted); font-size: 12px; display: flex; align-items: center; gap: 6px; }
	.sov .sov-push { margin-left: auto; }
	.sov .sov-tabs { display: inline-flex; gap: 2px; padding: 3px; background: var(--subtle-fg, var(--control-bg)); border-radius: 8px; margin: 0 16px 12px; align-self: flex-start; }
	.sov .sov-tab { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border-radius: 6px; color: var(--text-muted);
		font-weight: 500; cursor: pointer; user-select: none; border: 0; background: transparent; font-size: 13px; }
	.sov .sov-tab.on { background: var(--card-bg); color: var(--text-color); box-shadow: 0 1px 2px rgba(0,0,0,.07); }
	.sov .sov-tabn { color: var(--text-light, var(--text-muted)); font-size: 11.5px; }
	.sov .sov-cardcol { display: flex; flex-direction: column; }
	.sov .sov-trow { display: grid; grid-template-columns: minmax(0,1.1fr) minmax(0,1.5fr) minmax(0,1.1fr) 84px 96px; gap: 12px;
		align-items: center; padding: 11px 16px; border-top: 1px solid var(--border-color); }
	.sov .sov-thead { background: var(--subtle-fg, var(--control-bg)); color: var(--text-muted); font-weight: 500; padding-top: 8px; padding-bottom: 8px; }
	.sov .sov-who { display: flex; align-items: center; gap: 10px; min-width: 0; }
	.sov .sov-av { width: 28px; height: 28px; border-radius: 50%; background: var(--subtle-fg, var(--control-bg)); border: 1px solid var(--border-color);
		display: inline-flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 600; color: var(--text-muted); flex: none; }
	.sov .sov-strong { font-weight: 500; color: var(--text-color); }
	.sov .sov-trunc { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; max-width: 100%; }
	.sov .sov-stack { display: flex; flex-direction: column; gap: 2px; min-width: 0; align-items: flex-start; }
	.sov .sov-mono { font-family: var(--font-family-monospace, monospace); font-size: 11.5px; color: var(--text-muted); white-space: nowrap; }
	.sov .sov-sub { font-size: 12px; color: var(--text-muted); }
	.sov .sov-endc { display: flex; justify-content: flex-end; }
	.sov .sov-pill { display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 8px; border: 1px solid var(--border-color);
		border-radius: 6px; font-size: 12px; font-weight: 500; background: var(--card-bg); white-space: nowrap; color: var(--text-color); }
	.sov .sov-pill.ok .sov-dot { color: var(--green-600); } .sov .sov-pill.warn .sov-dot { color: var(--orange-600); }
	.sov .sov-pill.bad .sov-dot { color: var(--red-600); } .sov .sov-pill.mute .sov-dot { color: var(--gray-500); }
	.sov .sov-empty { padding: 18px 16px; color: var(--text-muted); border-top: 1px solid var(--border-color); }
	.sov .sov-prow { display: grid; grid-template-columns: 150px minmax(0,1.3fr) minmax(0,1fr) 72px; gap: 12px; align-items: center;
		padding: 11px 16px; border-top: 1px solid var(--border-color); }
	.sov .sov-days { text-align: right; font-weight: 500; font-variant-numeric: tabular-nums; }
	.sov .sov-days.old { color: var(--orange-600); }
	.sov .sov-foot { padding: 10px 16px; border-top: 1px solid var(--border-color); font-size: 12px; color: var(--text-muted); }
	.sov .sov-pad { display: flex; flex-direction: column; gap: 10px; padding: 14px 16px 16px; }
	.sov .sov-rowh { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 2px; }
	.sov .sov-kv { display: flex; justify-content: space-between; gap: 12px; font-variant-numeric: tabular-nums; }
	.sov .sov-kvl { color: var(--text-muted); }
	.sov .sov-kvt { border-top: 1px solid var(--border-color); padding-top: 10px; font-weight: 600; }
	.sov .sov-two { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 4px; }
	.sov .sov-stats { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); border: 1px solid var(--border-color); border-radius: 8px; }
	.sov .sov-stat { padding: 10px 12px; border-left: 1px solid var(--border-color); }
	.sov .sov-stat:first-child { border-left: 0; }
	.sov .sov-statv { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.3; }
	.sov .sov-arr { display: flex; align-items: center; gap: 12px; padding-top: 10px; border-top: 1px solid var(--border-color); }
	.sov .sov-arrl { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
	.sov .sov-prog { height: 4px; border-radius: 2px; background: var(--subtle-fg, var(--control-bg)); overflow: hidden; margin-top: -2px; }
	.sov .sov-progf { height: 100%; background: var(--text-color); border-radius: 2px; }
	.sov .sov-rech { display: flex; align-items: flex-end; gap: 16px; margin-top: 16px; flex-wrap: wrap; }
	.sov .sov-filter { margin-left: auto; width: 260px; max-width: 100%; }
	.sov .sov-recs { display: grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap: 24px 20px; padding: 18px 20px 22px; }
	.sov .sov-recg { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
	.sov .sov-recgt { font-size: 12px; color: var(--text-muted); font-weight: 500; margin-bottom: 2px; }
	.sov .sov-rec { color: var(--text-color); width: fit-content; }
	.sov .sov-rec:hover { text-decoration: underline; text-underline-offset: 3px; }
	.sov .btn.sov-wide { width: 100%; }
	.sov .sov-err { padding: 14px 16px; color: var(--text-muted); }
	@container (max-width: 1000px) {
		.sov .sov-kpis { grid-template-columns: repeat(2, minmax(0,1fr)); }
		.sov .sov-grid { grid-template-columns: minmax(0,1fr); }
		.sov .sov-side { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); align-items: start; }
		.sov .sov-span2 { grid-column: 1 / -1; }
		.sov .sov-recs { grid-template-columns: repeat(3, minmax(0,1fr)); }
		.sov .sov-trow { grid-template-columns: minmax(0,1.2fr) minmax(0,1.4fr) minmax(0,1.1fr) 76px 92px; }
		.sov .sov-prow { grid-template-columns: 120px minmax(0,1.3fr) minmax(0,1fr) 64px; }
	}
	@container (max-width: 560px) {
		.sov .sov-kpis, .sov .sov-side { grid-template-columns: minmax(0,1fr); }
		.sov .sov-recs { grid-template-columns: repeat(2, minmax(0,1fr)); }
		.sov .sov-trow { grid-template-columns: minmax(0,1fr) minmax(0,1fr); }
		.sov .sov-trow > :nth-child(n+3) { display: none; }
		.sov .sov-prow { grid-template-columns: minmax(0,1fr) 56px; }
		.sov .sov-prow > :nth-child(2), .sov .sov-prow > :nth-child(3) { display: none; }
	}
`;

class StoreOverview {
	constructor(page) {
		this.page = page;
		this.data = null;
		this.tab = 'All';
		this.q = '';
		// The live blocks are redrawn on every refresh; All records is drawn once (its filter box keeps
		// what was typed and its focus across refreshes).
		this.$root = $(`<div class="sov"><style>${SOV_CSS}</style><div class="sov-body"></div>
			<div class="sov-records"></div></div>`).appendTo(this.page.main);
		this.$body = this.$root.find('.sov-body');
		this.$records = this.$root.find('.sov-records');
		this.records = null;
		this.$body.html(`<div class="text-muted">${__('Loading…')}</div>`);
		this.$root.on('click', '[data-sov-tab]', (e) => { this.tab = $(e.currentTarget).attr('data-sov-tab'); this.render(); });
		this.$root.on('click', '[data-sov-review]', () => this.showReview());
		this.$root.on('click', '[data-sov-refresh]', () => this.refresh({ fresh_ad: 1 }));
		this.$root.on('input', '.sov-filter input', (e) => { this.q = e.target.value; this.renderRecords(); });
		// Every minute while the page is the one on screen and the tab is visible.
		this.timer = setInterval(() => {
			if (document.visibilityState === 'visible' && (frappe.get_route() || [])[0] === 'store-overview') this.refresh();
		}, SOV_REFRESH_MS);
		this.refresh();
	}

	// All records is asked for until it has arrived once; the A&D refresh button recounts that block.
	refresh(opts) {
		if (this.loading) {
			// The A&D button pressed while a refresh is out: ask again, recounting, once it is back.
			if (opts && opts.fresh_ad) this.loading.always(() => this.refresh(opts));
			return this.loading;
		}
		const args = Object.assign({ with_records: sovNeedRecords(this.records) ? 1 : 0 }, opts || {});
		this.loading = frappe.call({ method: SOV_API, args }).then((r) => {
			if (!r || !r.message) return;
			this.data = r.message;
			this.makeActions();
			this.render();
			if ('records' in r.message) {
				this.records = r.message.records;
				this.drawRecords();
			}
		}).always(() => { this.loading = null; });
		return this.loading;
	}

	makeActions() {
		const l = this.data.links || {};
		const key = JSON.stringify(l);
		if (this.actionsKey === key) return;
		this.actionsKey = key;
		this.page.clear_primary_action();
		this.page.clear_inner_toolbar();
		this.page.clear_actions_menu && this.page.clear_actions_menu();
		if (this.page.btn_secondary) this.page.btn_secondary.addClass('hide');
		if (l.add_customer) this.page.add_inner_button(__('Add customer'), () => frappe.new_doc('Customer'));
		if (l.receive) this.page.add_inner_button(__('Receive goods'), () => frappe.set_route('receive-goods'));
		if (l.new_order) this.page.add_inner_button(__('New order'), () => frappe.set_route('create-order'));
		if (l.pos) this.page.set_primary_action(__('Open POS'), () => frappe.set_route('point-of-sale'), 'monitor');
	}

	render() {
		const d = this.data;
		if (!d) return;
		const kpis = [this.salesTiles(), this.tile4473(), this.tileStock()].join('');
		const main = [this.card4473(), this.cardPickups()].filter(Boolean).join('');
		const side = [this.cardDrawer(), this.cardAD(), this.cardArriving()].filter(Boolean).join('');
		const lede = sovSummary(d);
		this.$body.html(`
			<div>
				<div class="sov-eyebrow">${sovEsc(sovEyebrow(d.today))}</div>
				${lede ? `<div class="sov-lede">${sovEsc(lede)}</div>` : ''}
			</div>
			${kpis ? `<div class="sov-kpis">${kpis}</div>` : ''}
			${main || side ? `<div class="sov-grid${main && side ? '' : ' sov-one'}">
				${main ? `<div class="sov-col">${main}</div>` : ''}
				${side ? `<div class="sov-col sov-side">${side}</div>` : ''}
			</div>` : ''}`);
	}

	error(title) {
		return `<section class="sov-card"><div class="sov-ch"><div class="sov-ct">${sovEsc(title)}</div></div>
			<div class="sov-err">${__('This could not be loaded. The rest of the page is up to date.')}</div></section>`;
	}

	tile(label, value, sub, extra) {
		return `<div class="sov-card sov-kpi"><div class="sov-kpit">
			<div class="sov-kpil">${sovEsc(label)}</div>
			<div class="sov-kpiv">${value}</div>
			<div class="sov-kpis2">${sub || '&nbsp;'}</div></div>${extra || ''}</div>`;
	}

	salesTiles() {
		const s = this.data.sales;
		if (!s) return '';
		if (s.error) return this.tile(__('Sales today'), '—', __('Could not be loaded')) + this.tile(__('Transactions'), '—', '');
		const k = sovSales(s.days);
		const day = __(sovWeekday(this.data.today));
		const delta = k.lastWeek
			? `<span>${__('Last {0}', [day])}</span><span class="sov-strong">${sovMoney(k.lastWeek)}</span>`
			: `<span>${__('No sales last {0}', [day])}</span>`;
		const bars = `<div class="sov-bars" title="${__('Last 7 days')}">${k.bars.map((h, i) =>
			`<span class="sov-bar${i === k.bars.length - 1 ? ' now' : ''}" style="height:${h}px"></span>`).join('')}</div>`;
		const value = s.report
			? `<a href="${sovRoute('Report', 'Sales Report')}" style="color:inherit">${sovMoney(k.net)}</a>` : sovMoney(k.net);
		return this.tile(__('Sales today'), value, delta, bars)
			+ this.tile(__('Transactions'), String(k.tx), `<span>${__('Avg. ticket')}</span><span class="sov-strong">${sovMoney(k.avg)}</span>`);
	}

	tile4473() {
		const b = this.data.open_4473;
		if (!b) return '';
		if (b.error) return this.tile(__('Open 4473s'), '—', __('Could not be loaded'));
		const delayed = b.rows.filter((r) => r.nics_status === 'Delayed').length;
		const sub = delayed
			? `<span class="sov-warnc"><span class="sov-dot"></span></span><span>${__('{0} delayed by NICS', [delayed])}</span>`
			: `<span class="sov-okc"><span class="sov-dot"></span></span><span>${__('none delayed')}</span>`;
		return this.tile(__('Open 4473s'), String(b.rows.length), sub);
	}

	tileStock() {
		const b = this.data.stock;
		if (!b) return '';
		if (b.error) return this.tile(__('Firearms in stock'), '—', __('Could not be loaded'));
		const parts = [];
		if (b.consignment) parts.push(__('{0} on consignment', [b.consignment]));
		if (b.transfers) parts.push(__('{0} customer transfers', [b.transfers]));
		const value = `<a href="${sovRoute('Report', 'Firearms In Stock')}" style="color:inherit">${b.total}</a>`;
		return this.tile(__('Firearms in stock'), value, `<span>${sovEsc(parts.join(' · '))}</span>`);
	}

	card4473() {
		const b = this.data.open_4473;
		if (!b) return '';
		if (b.error) return this.error(__('Open 4473s'));
		const tabs = sovTabs(b.rows).map((t) => `<button class="sov-tab${t.key === this.tab ? ' on' : ''}" data-sov-tab="${t.key}">
			<span>${__(t.key)}</span><span class="sov-tabn">${t.n}</span></button>`).join('');
		const rows = sovTabRows(b.rows, this.tab).map((r) => {
			const gun = sovGunLabel(r.guns);
			const pill = sovNicsPill(r.nics_status);
			const sub = r.form_4473_status ? __('4473 {0}', [r.form_4473_status]) : '';
			return `<div class="sov-trow">
				<span class="sov-who"><span class="sov-av">${sovEsc(sovInitials(r.customer_name))}</span>
					<span class="sov-strong sov-trunc">${sovEsc(r.customer_name)}</span></span>
				<span class="sov-stack"><span class="sov-trunc">${sovEsc(gun.name)}</span>
					<span class="sov-mono sov-trunc">${gun.serials ? 'SN ' + sovEsc(gun.serials) : ''}</span></span>
				<span class="sov-stack"><span class="sov-pill ${pill.cls}"><span class="sov-dot"></span><span>${__(pill.label)}</span></span>
					<span class="sov-sub">${sovEsc(sub)}</span></span>
				<span class="sov-mono">${sovEsc(sovStarted(r.started, this.data.today))}</span>
				<span class="sov-endc">${b.can_open ? `<a class="btn btn-default btn-xs" href="${sovRoute('DocType', 'POS Invoice')}/${encodeURIComponent(r.invoice)}">${__(sovRowAction(r.nics_status))}</a>` : ''}</span>
			</div>`;
		}).join('');
		const records = b.records
			? `<a class="btn btn-default btn-sm sov-push" href="${sovRoute('Report', 'Form 4473 Records')}">${__('All records')}</a>` : '';
		return `<section class="sov-card sov-cardcol">
			<div class="sov-ch"><div><div class="sov-ct">${__('Open 4473s')}</div>
				<div class="sov-cs">${__('Background checks started, firearm not yet transferred.')}</div></div>${records}</div>
			<div class="sov-tabs">${tabs}</div>
			<div class="sov-trow sov-thead"><span>${__('Customer')}</span><span>${__('Firearm')}</span><span>${__('NICS')}</span><span>${__('Started')}</span><span></span></div>
			${rows || `<div class="sov-empty">${__('Nothing in this state.')}</div>`}
		</section>`;
	}

	cardPickups() {
		const b = this.data.pickups;
		if (!b) return '';
		if (b.error) return this.error(__('Waiting on pickup'));
		const rows = b.rows.map((p) => `<div class="sov-prow">
			<span class="sov-strong sov-trunc">${sovEsc(p.customer)}</span>
			<span class="sov-stack"><span class="sov-trunc">${sovEsc(p.item_name)}</span><span class="sov-mono">SN ${sovEsc(p.serial)}</span></span>
			<span class="sov-sub sov-trunc">${p.source_name ? __('from {0}', [sovEsc(p.source_name)]) : ''}</span>
			<span class="sov-days ${sovDaysClass(p.days_in_store)}">${__('{0} days', [p.days_in_store])}</span>
		</div>`).join('');
		const all = this.data.links.pickups
			? `<a class="btn btn-default btn-sm sov-push" href="${sovRoute('Report', 'Pending Transfer Pickups')}">${__('View all {0}', [b.total])}</a>` : '';
		return `<section class="sov-card sov-cardcol">
			<div class="sov-ch"><div><div class="sov-ct">${__('Waiting on pickup')}</div>
				<div class="sov-cs">${__('Inbound FFL transfers logged in, customer not in yet.')}</div></div>${all}</div>
			${rows || `<div class="sov-empty">${__('No transfer guns waiting.')}</div>`}
			<div class="sov-foot">${__('Past {0} days turns amber.', [SOV_OLD_PICKUP_DAYS])}</div>
		</section>`;
	}

	cardDrawer() {
		const b = this.data.drawer;
		if (!b) return '';
		if (b.error) return this.error(__('Cash drawer'));
		const status = b.counted
			? `<span class="sov-pill ok sov-push"><span class="sov-dot"></span><span>${__('Counted')}</span></span>`
			: `<span class="sov-pill warn sov-push"><span class="sov-dot"></span><span>${__('Not counted')}</span></span>`;
		const sub = b.open_shifts ? __('{0} register shift(s) open', [b.open_shifts]) : __('Since the last close');
		const go = this.data.links.cash_drawer
			? `<div class="sov-two"><a class="btn btn-default" href="/desk/cash-drawer">${__('Cash expense')}</a>
				<a class="btn btn-default" href="/desk/cash-drawer">${__('Close the day')}</a></div>` : '';
		return `<section class="sov-card sov-pad">
			<div class="sov-rowh"><div><div class="sov-ct">${__('Cash drawer')}</div><div class="sov-cs">${sovEsc(sub)}</div></div>${status}</div>
			<div class="sov-kv"><span class="sov-kvl">${__('Opening')}</span><span>${sovMoney(b.opening)}</span></div>
			<div class="sov-kv"><span class="sov-kvl">${__('Cash in')}</span><span>${sovMoney(b.cash_in)}</span></div>
			<div class="sov-kv"><span class="sov-kvl">${__('Paid out')}</span><span>${sovMoney(-b.cash_out)}</span></div>
			<div class="sov-kv sov-kvt"><span>${__('Expected in drawer')}</span><span>${sovMoney(b.expected)}</span></div>
			${go}
		</section>`;
	}

	cardAD() {
		const b = this.data.ad;
		if (!b) return '';
		if (b.error) return this.error(__('A&D'));
		if (b.off) {
			return `<section class="sov-card sov-pad"><div class="sov-rowh"><div><div class="sov-ct">${__('A&D')}</div>
				<div class="sov-cs"><span class="sov-mutec"><span class="sov-dot"></span></span><span>${__('FastBound off')}</span></div></div></div></section>`;
		}
		const open = (b.retrying || 0) + (b.needs_person || 0);
		const synced = b.synced_at ? __('synced {0}', [sovAgo(b.synced_at, this.data.now || '')]) : __('nothing synced yet');
		const notLogged = `<div class="sov-statv${b.needs_person ? ' sov-warnc' : ''}">${open}</div>`;
		const split = open ? `<div class="sov-sub">${__('{0} retrying · {1} need a person', [b.retrying || 0, b.needs_person || 0])}</div>` : '';
		const review = b.can_review && open
			? `<button class="btn btn-default sov-wide" data-sov-review="1">${__('Review {0} not logged', [open])}</button>` : '';
		return `<section class="sov-card sov-pad">
			<div class="sov-rowh"><div><div class="sov-ct">${__('A&D')}</div>
				<div class="sov-cs"><span class="${b.needs_person ? 'sov-warnc' : 'sov-okc'}"><span class="sov-dot"></span></span><span>${__('FastBound')} · ${sovEsc(synced)}</span></div></div>
				<button class="btn btn-default btn-xs sov-push" data-sov-refresh="1" title="${__('Refresh')}">${frappe.utils.icon('refresh', 'xs')}</button></div>
			<div class="sov-stats">
				<div class="sov-stat"><div class="sov-statv">${b.acquired}</div><div class="sov-sub">${__('Acquired')}</div></div>
				<div class="sov-stat"><div class="sov-statv">${b.disposed}</div><div class="sov-sub">${__('Disposed')}</div></div>
				<div class="sov-stat">${notLogged}<div class="sov-sub">${__('Not logged')}</div></div>
			</div>
			${split}${review}
		</section>`;
	}

	showReview() {
		const recs = (this.data.ad && this.data.ad.records) || [];
		const row = (r) => `<tr><td><a href="${sovRoute('DocType', r.doctype)}/${encodeURIComponent(r.name)}">${sovEsc(r.name)}</a></td>
			<td>${r.terminal ? `<span class="indicator-pill orange">${__('Needs a person')}</span>` : `<span class="indicator-pill blue">${__('Will retry')}</span>`}</td>
			<td class="small text-muted">${sovEsc(r.error || __('Not pushed yet'))}</td></tr>`;
		const d = new frappe.ui.Dialog({ title: __('Not in the bound book yet'), size: 'large', fields: [{ fieldtype: 'HTML', fieldname: 'list' }] });
		d.fields_dict.list.$wrapper.html(`<p class="text-muted">${__('Submitted here, not yet written to FastBound (submitted over 15 minutes ago). "Will retry" ones are pushed again by the daily retry; the others need someone to fix the cause and push again.')}</p>
			<table class="table table-sm"><thead><tr><th>${__('Record')}</th><th>${__('State')}</th><th>${__('Last error')}</th></tr></thead>
			<tbody>${recs.map(row).join('')}</tbody></table>`);
		d.show();
	}

	cardArriving() {
		const b = this.data.arriving;
		if (!b) return '';
		if (b.error) return this.error(__('Arriving'));
		const rows = b.orders.map((o) => {
			const a = sovArrival(o);
			const right = a.kind === 'progress'
				? `<span class="sov-strong">${a.got} <span class="sov-sub">/ ${a.of}</span></span>`
				: `<span class="sov-sub">${sovEsc(__(a.label))}</span>`;
			const bar = a.kind === 'progress' ? `<div class="sov-prog"><div class="sov-progf" style="width:${a.pct}%"></div></div>` : '';
			return `<div class="sov-arr"><div class="sov-arrl"><span class="sov-strong">${sovEsc(o.distributor)}</span>
				<span class="sov-mono">${sovEsc(o.name)} · ${__('{0} units to come', [o.outstanding])}</span></div>${right}</div>${bar}`;
		}).join('');
		const receive = this.data.links.receive ? `<a class="btn btn-primary sov-wide" href="/desk/receive-goods">${__('Receive goods')}</a>` : '';
		return `<section class="sov-card sov-pad sov-span2">
			<div><div class="sov-ct">${__('Arriving')}</div><div class="sov-cs">${__('Distributor orders not fully received.')}</div></div>
			${rows || `<div class="sov-sub">${__('Nothing on the way.')}</div>`}
			${b.total > b.orders.length ? `<div class="sov-sub">${__('and {0} more', [b.total - b.orders.length])}</div>` : ''}
			${receive}
		</section>`;
	}

	drawRecords() {
		const b = this.records;
		if (!b || b.error) {
			this.$records.empty();
			return;
		}
		this.$records.html(`<div class="sov-rech"><div><div class="sov-ct" style="font-size:15px">${__('All records')}</div>
			<div class="sov-cs">${__('Every list, report and setting, in one place.')}</div></div>
			<div class="sov-filter"><input type="search" class="form-control input-sm" placeholder="${__('Filter…')}" value="${sovEsc(this.q)}"></div></div>
			<div class="sov-card sov-recs"></div>`);
		this.renderRecords();
	}

	renderRecords() {
		const b = this.records;
		if (!b || b.error) return;
		const groups = sovFilterRecords(b, this.q);
		this.$records.find('.sov-recs').html(groups.length ? groups.map((g) => `<div class="sov-recg">
			<div class="sov-recgt">${sovEsc(g.title)}</div>
			${g.items.map((i) => `<a class="sov-rec" href="${sovEsc(sovRecordHref(i, (o) => frappe.utils.generate_route(o)))}">${sovEsc(i.label)}</a>`).join('')}
		</div>`).join('') : `<div class="sov-sub">${__('No list matches that filter.')}</div>`);
	}
}

// Node (the unit tests) has `module`; the browser does not.
if (typeof module !== 'undefined' && module.exports) {
	module.exports = {
		sovMoney, sovEsc, sovNoNics, sovEyebrow, sovWeekday, sovStarted, sovAgo, sovInitials, sovNicsState, sovNicsPill,
		sovTabs, sovTabRows, sovRowAction, sovGunLabel, sovDaysClass, sovSales, sovBars, sovArrival,
		sovSummary, sovFilterRecords, sovRoute, sovRecordHref, sovNeedRecords,
	};
}
