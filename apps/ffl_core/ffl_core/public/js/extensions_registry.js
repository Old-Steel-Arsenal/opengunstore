// gunstore.extensions — the one place an extension plugs its UI into the POS
// Pure: no DOM, no Frappe; the bundle renders what
// this returns and unit tests run it under Node.
//
//   gunstore.extensions.register('acme_layaway', {
//     posActions:      [{ id, label, icon, condition(ctx), onClick(ctx) }],
//     serialActions:   [{ id, label, onClick({ frm }) }],   // Serial No form
//     customerActions: [{ id, label, onClick({ frm }) }],   // Customer form
//     pendingOrderActions: [{ id, label, stages, condition({ row }), onClick({ row, reload }) }],
//     settingsLinks:   [{ label, doctype }],
//     onDeskReady()    {},                                   // once, re-entrant
//   });
//
// Rendering rules the core owns: only extensions that are effectively on appear
// — switched on, installed on this site and compatible; the boot's
// frappe.boot.gunshop_extensions[id].enabled carries that, not the bare switch
// (a paused extension keeps its switch on). Action ids are unique across
// extensions (first registration wins); order = registration order.
(function () {
	'use strict';

	function createRegistry() {
		const specs = Object.create(null);
		const order = [];

		function register(id, spec) {
			if (!id || typeof id !== 'string') throw new Error('gunstore.extensions.register: extension id is required');
			if (!(id in specs)) order.push(id);
			specs[id] = Object.assign({}, spec || {});
			return specs[id];
		}
		// Effectively on, as the desk knows it (frappe.boot.gunshop_extensions). A
		// page of an extension asks this — it need not have register()ed.
		function isEnabled(id, boot) {
			const map = (boot && boot.gunshop_extensions) || {};
			return !!(map[id] && map[id].enabled);
		}
		function enabledIds(boot) {
			return order.filter((id) => isEnabled(id, boot));
		}
		// Flatten one kind of action across the enabled extensions, deduped by action id.
		function actions(kind, boot) {
			const seen = new Set();
			const out = [];
			for (const id of enabledIds(boot)) {
				for (const a of (specs[id] && specs[id][kind]) || []) {
					if (!a || !a.id || typeof a.onClick !== 'function' || seen.has(a.id)) continue;
					seen.add(a.id);
					out.push(Object.assign({ extension: id }, a));
				}
			}
			return out;
		}
		// condition() is the extension's; a throwing condition hides the action instead of breaking the POS.
		function applies(action, ctx) {
			if (typeof action.condition !== 'function') return true;
			try { return !!action.condition(ctx); } catch (e) { return false; }
		}
		function settingsLinks(id) { return (specs[id] && specs[id].settingsLinks) || []; }
		function get(id) { return specs[id]; }
		function ids() { return order.slice(); }

		return { register, isEnabled, enabledIds, actions, applies, settingsLinks, get, ids };
	}

	const api = { createRegistry };
	if (typeof module !== 'undefined' && module.exports) module.exports = api;   // Node test
	if (typeof window !== 'undefined') {
		window.gunstore = window.gunstore || {};
		if (!window.gunstore.extensions) window.gunstore.extensions = createRegistry();
		window.gunstore.extensions.createRegistry = createRegistry;
	}
})();
