// Storage Locations — where things sit in the shop.
//
// Map: every zone as a grid; a numbered slot shows its gun or is free, a display case /
// rack / safe shows what it holds. Move: scan a location label, then scan things into it.
// Not placed: guns and items that are in no location yet, plus items sold or shipped
// without saying which location they came from. Zones (managers): add zones and
// positions, disable, print the barcode labels.
//
// Nothing here touches stock or the books: a location is only a note on where something
// is. The functions below are a PURE KERNEL (no DOM, no frappe, no __()), unit-tested
// under Node in tests/js/storage_map.test.js. The `stg` prefix keeps them clear of the
// other desk pages, which share one global scope.

function stgNorm(text) {
	return String(text === null || text === undefined ? '' : text).trim().toLowerCase();
}

// A scanned or typed location label ("a5", "Display Case 3") or barcode ("ST-A5") ->
// the location's name, built from the map the page already holds.
function stgBuildIndex(zones) {
	const index = {};
	(zones || []).forEach((z) => (z.locations || []).forEach((l) => {
		index[stgNorm(l.name)] = l.name;
		// A label never overrides a barcode that happens to look the same.
		if (!(stgNorm(l.label) in index)) index[stgNorm(l.label)] = l.name;
	}));
	return index;
}

function stgResolveLocation(index, code) {
	return (index || {})[stgNorm(code)] || null;
}

// free = nothing in it (usable), gun = a slot holding its gun, items = an open location
// holding something, disabled = not in use.
function stgCellState(loc, kind) {
	if (loc.disabled) return 'disabled';
	if (kind === 'Slots') return (loc.guns || []).length ? 'gun' : 'free';
	return (loc.guns || []).length || loc.qty ? 'items' : 'free';
}

function stgCellTitle(loc, kind) {
	const guns = (loc.guns || []).map((g) => `${g.item_name} (${g.serial})`);
	const bits = guns.slice(0, 3);
	if (guns.length > 3) bits.push(`+${guns.length - 3}`);
	if (loc.qty) bits.push(`${loc.items} ${loc.items === 1 ? 'item' : 'items'}, ${loc.qty} units`);
	if (loc.disabled) bits.push('disabled');
	return bits.length ? `${loc.label}: ${bits.join('; ')}` : `${loc.label}: ${kind === 'Slots' ? 'free slot' : 'empty'}`;
}

// "12 of 30 used" for a Slots zone, "5 places in use" for an open one.
function stgZoneSummary(zone) {
	const live = (zone.locations || []).filter((l) => !l.disabled);
	const used = live.filter((l) => (l.guns || []).length || l.qty).length;
	if (zone.kind === 'Slots') {
		return { used, total: live.length, free: live.length - used, text: `${used} of ${live.length} used` };
	}
	return { used, total: live.length, free: null, text: `${used} of ${live.length} in use` };
}

// Which locations match the search box: by label, by a gun's serial or model. null =
// no search typed (nothing is dimmed).
function stgSearchMap(zones, text) {
	const t = stgNorm(text);
	if (!t) return null;
	const hits = new Set();
	(zones || []).forEach((z) => (z.locations || []).forEach((l) => {
		if (stgNorm(l.label).includes(t) || (l.guns || []).some(
			(g) => stgNorm(g.serial).includes(t) || stgNorm(g.item_name).includes(t))) hits.add(l.name);
	}));
	return hits;
}

// What a scan does on the Move tab: a location label picks the destination, anything
// else is put into the destination.
function stgScanPlan(index, destination, code) {
	const text = String(code || '').trim();
	if (!text) return { action: 'none' };
	const loc = stgResolveLocation(index, text);
	if (loc) return { action: 'destination', location: loc };
	if (!destination) return { action: 'need_destination' };
	return { action: 'move', code: text, to: destination };
}

// This session's moves, newest first, capped. Undo marks one rather than removing it.
function stgPushSession(list, entry, max) {
	return [entry].concat(list || []).slice(0, max || 40);
}

function stgMarkUndone(list, move) {
	return (list || []).map((e) => (e.move === move ? Object.assign({}, e, { undone: true }) : e));
}

// Only a move that really happened can be undone, and only once.
function stgCanUndo(entry) {
	return !!(entry && entry.move && !entry.undone);
}

// The Zones form, checked before it is sent; the server checks again.
function stgValidateZone(f) {
	const name = String(f.zone_name || '').trim();
	if (!/^[A-Za-z0-9][A-Za-z0-9 \-]{0,38}$/.test(name)) {
		return 'Give the zone a short name (letters, numbers, spaces and dashes), like A or Display Case.';
	}
	if (f.kind !== 'Slots' && f.kind !== 'Open') return 'Pick Slots or Open.';
	if (f.kind === 'Open') return ''; // an Open zone is one location: no count to check
	const n = Number(f.count);
	if (!Number.isInteger(n) || n < 1 || n > 500) return 'A zone needs between 1 and 500 positions.';
	return stgValidateSides(f.sides);
}

// A zone's Sides as typed: empty (fit to the shape) or 1 to STGP_MAX_LINES.
function stgValidateSides(sides) {
	const k = Number(sides || 0);
	if (!Number.isInteger(k) || k < 0 || k > STGP_MAX_LINES) {
		return `Sides go from 1 to ${STGP_MAX_LINES}. Leave it empty to fit the slots to the shape on the map.`;
	}
	return '';
}

// The labels a zone's positions will be printed with: a Slots zone numbers them (A1, A2 ...),
// an Open zone is ONE location carrying the zone's own name (the server names them; this only
// previews).
function stgPreviewLabels(zoneName, kind, count) {
	if (kind === 'Open') return [zoneName];
	const n = Math.max(0, Math.min(Number(count) || 0, 500));
	const out = [];
	for (let i = 1; i <= n; i++) out.push(`${zoneName}${i}`);
	return out;
}

// The preview line in the New Zone dialog: the real labels from the typed name, side by side
// when it has sides, or an example while the name is still empty.
function stgZonePreviewText(name, kind, count, sides, numbering) {
	const typed = String(name || '').trim();
	if (kind === 'Open') return typed ? typed : 'e.g. Rack 1';
	if (!typed) return 'e.g. A + 30 → A1 … A30';
	const labels = stgPreviewLabels(typed, kind, count);
	if (!labels.length) return '';
	const brief = (l) => (l.length > 3 ? `${l.slice(0, 3).join(', ')} … ${l[l.length - 1]}` : l.join(', '));
	const k = Math.min(Math.floor(Number(sides)) || 0, labels.length);
	if (!k) return `${brief(labels)} (sides follow the shape on the map)`;
	if (k < 2) return brief(labels);
	const side = Array.from({ length: k }, () => []);
	labels.forEach((l, i) => side[stgpSlotLine(i, labels.length, k, numbering === STG_IN_ORDER).line].push(l));
	const shown = side.map((l, j) => `Side ${j + 1}: ${brief(l)}`).filter((t, j) => side[j].length);
	return shown.slice(0, 3).join(' · ') + (shown.length > 3 ? ' …' : '');
}

// Rows of the Not placed tab, filtered by the search box.
function stgFilterRows(rows, text, ...fields) {
	const t = stgNorm(text);
	if (!t) return rows || [];
	return (rows || []).filter((r) => fields.some((f) => stgNorm(r[f]).includes(t)));
}

// The count badges on the tab: how many things still need a place or a word.
function stgNeedsAttention(summary) {
	const s = summary || {};
	return (s.guns || 0) + (s.items || 0) + (s.to_confirm || 0);
}

// ------------------------------------------------------------- floor plan kernel
// A floor plan is one room, drawn in FEET. Every shape is a rectangle by its centre:
// {type: zone|wall|door|area|label, zone?, text?, x, y, w, h, r (degrees), rows?}. The
// server checks the same bounds (ffl_core.storage, "Floor plans").

const STGP_MIN = 0.25;       // thinnest shape, feet
const STG_IN_ORDER = 'In order'; // Storage Zone.numbering; anything else (the default) is odd / even
const STGP_MAX_LINES = 50;   // most lines of slots in a zone (server: storage.MAX_SLOT_ROWS)
const STGP_PLAN_MIN = 4;     // smallest / largest plan side, feet
const STGP_PLAN_MAX = 500;

function stgpRound(v) {
	return Math.round(v * 100) / 100;
}

function stgpSnap(v, step) {
	return stgpRound(step ? Math.round(v / step) * step : v);
}

function stgpClamp(v, lo, hi) {
	return Math.min(hi, Math.max(lo, v));
}

function stgpAngle(r) {
	const a = stgpRound((((Number(r) || 0) % 360) + 360) % 360);
	return a >= 360 ? 0 : a;
}

// How a Slots zone's n slots fill its w × h rectangle. The slots run in lines along the
// LONG side -- rows in a wide zone, columns in a tall one -- so a rack keeps its sides
// along its length whichever way it is drawn. `lines` (the shape's `rows`) is how many lines
// sit across the short side; empty = the grid whose cells are closest to square.
function stgpSlotGrid(w, h, n, lines) {
	n = Math.max(1, Math.floor(n) || 1);
	const down = h > w; // tall: lines are columns, numbered top to bottom
	const long = down ? h : w, short = down ? w : h;
	let k = Math.floor(Number(lines)) || 0;
	if (k < 1) {
		let best = Infinity;
		for (let j = 1; j <= n; j++) {
			const score = Math.abs(Math.log((long / Math.ceil(n / j)) / (short / j)));
			if (score < best - 1e-9) { best = score; k = j; }
		}
	}
	k = Math.min(k, n);
	const along = Math.ceil(n / k);
	return down ? { rows: along, cols: k, down } : { rows: k, cols: along, down };
}

// Which of k lines slot i (from 0) of n sits on, and how far along it. Two lines are a
// two-sided rack numbered like houses -- 1, 3, 5 … down one side, 2, 4, 6 … facing them --
// unless inOrder; any other count fills a line before starting the next.
function stgpSlotLine(i, n, k, inOrder) {
	if (k === 2 && !inOrder) return { line: i % 2, at: Math.floor(i / 2) };
	const along = Math.ceil(n / k);
	return { line: Math.floor(i / along), at: i % along };
}

// Each slot's rectangle from the zone's top-left corner, slot 1 top-left. A wide zone's lines
// are rows read left to right, a tall one's are columns read top to bottom; the first line is
// the top row / left column.
function stgpSlotCells(w, h, n, lines, inOrder) {
	const g = stgpSlotGrid(w, h, n, lines);
	const cw = w / g.cols, ch = h / g.rows;
	const total = Math.max(1, Math.floor(n) || 1), k = g.down ? g.cols : g.rows;
	const out = [];
	for (let i = 0; i < total; i++) {
		const { line, at } = stgpSlotLine(i, total, k, inOrder);
		out.push(g.down ? { x: line * cw, y: at * ch, w: cw, h: ch } : { x: at * cw, y: line * ch, w: cw, h: ch });
	}
	return out;
}

// The four corners in plan feet (a shape turns about its centre).
function stgpCorners(s) {
	const t = (s.r || 0) * Math.PI / 180, c = Math.cos(t), n = Math.sin(t);
	return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([i, j]) => {
		const u = i * s.w / 2, v = j * s.h / 2;
		return { x: s.x + u * c - v * n, y: s.y + u * n + v * c };
	});
}

function stgpBounds(shapes) {
	const pts = [].concat(...(shapes || []).map(stgpCorners));
	if (!pts.length) return null;
	const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
	return { minX: Math.min(...xs), minY: Math.min(...ys), maxX: Math.max(...xs), maxY: Math.max(...ys) };
}

function stgpInside(s, plan) {
	return s.x >= 0 && s.y >= 0 && s.x <= plan.width && s.y <= plan.height;
}

// Drag resize handle (hx, hy) -- each -1, 0 or 1: the right edge is (1, 0), the bottom-left
// corner (-1, 1) -- to plan point (px, py). The opposite edge or corner stays exactly where
// it was at any rotation. Always computed from the shape as it was when the drag began.
function stgpResize(s, hx, hy, px, py, step, max) {
	const t = (s.r || 0) * Math.PI / 180, c = Math.cos(t), n = Math.sin(t); // u = (c, n), v = (-n, c)
	const ax = s.x - (hx * s.w / 2) * c + (hy * s.h / 2) * n;              // the fixed point
	const ay = s.y - (hx * s.w / 2) * n - (hy * s.h / 2) * c;
	const dx = px - ax, dy = py - ay;
	const du = dx * c + dy * n, dv = -dx * n + dy * c;                      // pointer in the shape's frame
	const size = (v) => stgpClamp(stgpSnap(v, step), STGP_MIN, max || Infinity);
	const w = hx ? size(hx * du) : s.w;
	const h = hy ? size(hy * dv) : s.h;
	const a = hx * (w - s.w) / 2, b = hy * (h - s.h) / 2;                  // centre shift along u, v
	return Object.assign({}, s, { w, h, x: s.x + a * c - b * n, y: s.y + a * n + b * c });
}

// Turn the shape so its knob (above the top edge) points at (px, py); 15° steps unless free.
function stgpRotateTo(s, px, py, free) {
	const r = Math.atan2(py - s.y, px - s.x) * 180 / Math.PI + 90;
	return Object.assign({}, s, { r: stgpAngle(free ? r : Math.round(r / 15) * 15) });
}

// Move the centre to (x, y), on the grid and never off the plan.
function stgpMoveTo(s, x, y, plan, step) {
	return Object.assign({}, s, {
		x: stgpClamp(stgpSnap(x, step), 0, plan.width),
		y: stgpClamp(stgpSnap(y, step), 0, plan.height),
	});
}

// What the canvas shows, in plan feet ({x, y, w, h}), always with the canvas' own aspect.
// Fit: the whole plan, centred, with `pad` feet around it.
function stgpFit(plan, vw, vh, pad) {
	pad = pad === undefined ? 1 : pad;
	const W = plan.width + 2 * pad, H = plan.height + 2 * pad, aspect = vw / vh;
	const w = W / H > aspect ? W : H * aspect;
	const h = w / aspect;
	return { x: (plan.width - w) / 2, y: (plan.height - h) / 2, w, h };
}

function stgpZoomLimits(plan) {
	return { min: 3, max: Math.max(plan.width, plan.height) * 4 };
}

// Zoom by `factor` (> 1 = closer) keeping plan point (fx, fy) where it is on screen.
function stgpZoomAt(view, factor, fx, fy, limits) {
	const w = stgpClamp(view.w / factor, limits.min, limits.max);
	const k = w / view.w;
	return { x: fx - (fx - view.x) * k, y: fy - (fy - view.y) * k, w, h: view.h * k };
}

// Two fingers moving from (a0, b0) to (a1, b1), in pixels from the canvas' top-left: the
// plan point under their midpoint follows it, and the view zooms as they spread. One finger
// (a = b) is a pan.
function stgpPinch(view0, a0, b0, a1, b1, pxWidth, limits) {
	const d0 = Math.hypot(b0.x - a0.x, b0.y - a0.y), d1 = Math.hypot(b1.x - a1.x, b1.y - a1.y);
	const w = d0 > 0 && d1 > 0 ? stgpClamp(view0.w * d0 / d1, limits.min, limits.max) : view0.w;
	const k0 = view0.w / pxWidth, k1 = w / pxWidth;
	const fx = view0.x + (a0.x + b0.x) / 2 * k0, fy = view0.y + (a0.y + b0.y) / 2 * k0;
	return { x: fx - (a1.x + b1.x) / 2 * k1, y: fy - (a1.y + b1.y) / 2 * k1, w, h: view0.h * (w / view0.w) };
}

// Undo snapshots of the draft -- shapes and the plan's name and size, so undoing past a resize
// never leaves shapes outside the room -- newest last. lazy: no redo; Cancel throws a whole
// draft away.
function stgpPushUndo(stack, ed, max) {
	const snap = { shapes: ed.shapes, plan_name: ed.plan_name, width: ed.width, height: ed.height };
	return (stack || []).concat([JSON.stringify(snap)]).slice(-(max || 100));
}

// Zones on no plan. The plan being drawn counts with its draft, so a zone taken off it is
// free again at once.
function stgpUnplaced(zones, plans, draft) {
	const placed = new Set();
	(plans || []).forEach((p) => {
		const shapes = draft && draft.plan === p.name ? draft.shapes : ((p.layout || {}).shapes || []);
		shapes.forEach((s) => { if (s.type === 'zone') placed.add(s.zone); });
	});
	return (zones || []).filter((z) => !placed.has(z.name));
}

// A new zone's starting size: half a foot per slot in one band, or a 6 × 3 block -- never
// longer than the plan (the server's limit).
function stgpZoneSize(zone, plan) {
	const big = Math.max(plan.width, plan.height);
	if (zone.kind !== 'Slots') return { w: Math.min(6, big), h: Math.min(3, big) };
	const n = zone.positions || (zone.locations || []).length || 1;
	return { w: Math.min(stgpClamp(stgpRound(n * 0.5), 2, Math.max(2, plan.width - 2)), big), h: Math.min(2, big) };
}

function stgpZoneShape(zone, x, y, plan) {
	const size = stgpZoneSize(zone, plan);
	return Object.assign({ type: 'zone', zone: zone.name, r: 0 }, size, {
		x: stgpClamp(stgpRound(x), 0, plan.width), y: stgpClamp(stgpRound(y), 0, plan.height) });
}

// Lay zones out in rows under what is already drawn, wrapping at the plan's width; when the
// room runs out at the bottom, carry on from the top (over the drawing) rather than piling
// them up on the bottom edge. No packing around other shapes: the manager then drags each
// where it really is. lazy: a plan too small to hold them even once still overlaps them.
function stgpAutoPlace(zones, plan, shapes) {
	const box = stgpBounds(shapes);
	let y = box ? stgpClamp(box.maxY + 1, 0, plan.height - 1) : 1;
	let x = 1, rowH = 0;
	return (zones || []).map((z) => {
		const { w, h } = stgpZoneSize(z, plan);
		if (x > 1 && x + w > plan.width - 1) { x = 1; y += rowH + 1; rowH = 0; }
		if (y > 1 && y + h > plan.height) { x = 1; y = 1; rowH = 0; }
		const s = stgpZoneShape(z, x + w / 2, y + h / 2, plan);
		x += w + 1;
		rowH = Math.max(rowH, h);
		return s;
	});
}

const STGP_NEW = { wall: [10, 0.5, null], door: [3, 0.5, null], area: [6, 4, ''], label: [6, 1.5, 'Label'] };

function stgpNewShape(type, x, y, plan) {
	const [w, h, text] = STGP_NEW[type];
	const big = Math.max(plan.width, plan.height);
	const s = { type, x: stgpClamp(stgpSnap(x, 0.5), 0, plan.width), y: stgpClamp(stgpSnap(y, 0.5), 0, plan.height),
		w: Math.min(w, big), h: Math.min(h, big), r: 0 };
	if (text !== null) s.text = text;
	return s;
}

// Search hits per plan (matching locations each one shows); `none` counts zones on no plan.
function stgpPlanHits(plans, zones, hits) {
	const out = { none: 0 };
	if (!hits) return out;
	const perZone = {};
	(zones || []).forEach((z) => { perZone[z.name] = (z.locations || []).filter((l) => hits.has(l.name)).length; });
	const placed = new Set();
	(plans || []).forEach((p) => {
		out[p.name] = 0;
		((p.layout || {}).shapes || []).forEach((s) => {
			if (s.type === 'zone' && s.zone in perZone) { out[p.name] += perZone[s.zone]; placed.add(s.zone); }
		});
	});
	Object.keys(perZone).forEach((z) => { if (!placed.has(z)) out.none += perZone[z]; });
	return out;
}

// Where text goes in a w × h rectangle turned r degrees: the box it gets (w, h) and the
// turn to give it inside the shape. Near flat or near upright (within 15°) the text stays
// level on screen, so a zone stood on end still reads normally; at a slant it runs along the
// shape's long side instead (a level box inside a slanted case is too small to read), and
// never upside down.
function stgpTextFrame(w, h, r) {
	r = Number(r) || 0;
	const a = stgpAngle(r) % 180;
	if (a <= 15 || a >= 165) return { w, h, turn: -r };
	if (a >= 75 && a <= 105) return { w: h, h: w, turn: -r };
	if (w >= h) return { w, h, turn: stgpTextTurn(r) };
	const turn = stgpTextTurn(r + 90) ? -90 : 90; // along the long (local y) side
	return { w: h, h: w, turn };
}

// Text broken into lines of at most `chars` characters, at spaces; a word longer than a line
// is cut across lines.
function stgpWrap(text, chars) {
	const lines = [];
	let cur = '';
	String(text || '').trim().split(/\s+/).filter(Boolean).forEach((word) => {
		while (word.length > chars) {
			if (cur) { lines.push(cur); cur = ''; }
			lines.push(word.slice(0, chars));
			word = word.slice(chars);
		}
		if (!word) return;
		if (!cur) cur = word;
		else if (cur.length + 1 + word.length <= chars) cur += ` ${word}`;
		else { lines.push(cur); cur = word; }
	});
	if (cur) lines.push(cur);
	return lines;
}

// The biggest font (px, from maxPx down to minPx) at which `text` fits a wPx × hPx box,
// wrapped onto as many lines as the box holds -- so a name fills a long thin case or a big
// square one alike. At the smallest size the last line is cut short. null when not even a
// few characters fit.
function stgpFitText(text, wPx, hPx, maxPx, minPx) {
	text = String(text || '').trim();
	if (!text) return null;
	const room = (px) => ({ chars: Math.floor((wPx * 0.9) / (px * 0.6)), rows: Math.floor((hPx * 0.9) / (px * 1.2)) });
	const longestWord = Math.max(...text.split(/\s+/).map((w) => w.length));
	// Whole words first (a smaller size beats "Displa / y"), then words cut across lines. Each
	// extra line costs 4 px, so "Rack 1" stays on one line rather than "Rack / 1" a size up.
	for (const whole of [true, false]) {
		let best = null;
		for (let px = Math.floor(maxPx); px >= minPx; px--) {
			const { chars, rows } = room(px);
			if (chars < 1 || rows < 1 || (whole && longestWord > chars)) continue;
			const lines = stgpWrap(text, chars);
			if (lines.length > rows) continue;
			const score = px - 4 * (lines.length - 1);
			if (!best || score > best.score || (score === best.score && lines.length < best.lines.length)) {
				best = { px, lines, score };
			}
		}
		if (best) return { px: best.px, lines: best.lines };
	}
	const { chars, rows } = room(minPx);
	if (chars < Math.min(3, text.length) || rows < 1) return null;
	const lines = stgpWrap(text, chars);
	const kept = lines.slice(0, rows);
	if (lines.length > rows) kept[rows - 1] = `${kept[rows - 1].slice(0, Math.max(1, chars - 1))}…`;
	return { px: minPx, lines: kept };
}

// Extra turn for a label's text so it never reads upside down.
function stgpTextTurn(r) {
	const a = stgpAngle(r);
	return a > 90 && a <= 270 ? 180 : 0;
}

// A plan's name and size as typed in its dialog; '' when fine. The shapes must still fit.
function stgpCheckPlan(v, shapes) {
	const name = String(v.plan_name || '').trim();
	if (!name || name.length > 40 || /[<>]/.test(name)) return 'Give the plan a name of up to 40 characters (no < or >), like Showroom.';
	const w = Number(v.width), h = Number(v.height);
	if (!(w >= STGP_PLAN_MIN && w <= STGP_PLAN_MAX && h >= STGP_PLAN_MIN && h <= STGP_PLAN_MAX)) {
		return `A plan is between ${STGP_PLAN_MIN} and ${STGP_PLAN_MAX} feet on each side.`;
	}
	const out = (shapes || []).filter((s) => !stgpInside(s, { width: w, height: h }));
	if (out.length) return `${out.length} shape(s) would be outside the plan. Move them in first.`;
	const big = Math.max(w, h);
	const long = (shapes || []).filter((s) => s.w > big || s.h > big);
	return long.length ? `${long.length} shape(s) would be longer than the plan (${big} ft). Shorten them first.` : '';
}

// Guarded so Node can require the kernel above. Harmless in the browser, where
// frappe is always defined.
if (typeof frappe !== 'undefined') {
	frappe.pages['storage-map'].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __('Storage Locations'),
			single_column: true,
		});
		wrapper.storage_app = new StorageMapApp(page);
	};
	frappe.pages['storage-map'].on_page_show = function (wrapper) {
		wrapper.storage_app?.onShow();
	};
}

const STG_API = 'ffl_core.api.storage';
const STGP_UNIT = 100; // SVG units per foot on the floor plan canvas

class StorageMapApp {
	constructor(page) {
		this.page = page;
		this.tab = 'map';
		this.opts = null;
		this.map = null;
		this.index = {};
		this.destination = null;
		this.session = [];
		this.mapSearch = '';
		this.unassigned = null;
		this.canManage = false;
		this.planName = null;  // the floor plan on screen
		this.mode = 'plan';    // Map tab: 'plan' or 'list'
		this.views = {};       // per plan: what the canvas shows, in feet
		this.edit = null;      // the draft while a manager draws a plan
		this.ptrs = new Map(); // pointers down on the canvas
		// Desk hides a page instead of destroying it, so these stay bound for the page's life
		// and check that the editor is open (and on screen) before doing anything.
		document.addEventListener('keydown', (ev) => this.onKey(ev));
		window.addEventListener('beforeunload', (ev) => {
			if (this.edit && this.edit.dirty) { ev.preventDefault(); ev.returnValue = ''; }
		}, { capture: true });
		this.$body = $(`
			<div class="stg">
				<style>
					.stg { max-width: 1100px; margin: 0 auto; padding: 4px 0 80px; }
					@media (max-width: 767px) { .stg { padding-inline: 15px; } }
					.stg .card-box { background: var(--card-bg); border: 1px solid var(--border-color);
						border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
					.stg .muted { color: var(--text-muted); }
					.stg .tabs { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
					.stg .tabs .btn.active { background: var(--primary); color: #fff; }
					.stg .zone-head { display: flex; justify-content: space-between; align-items: baseline;
						margin-bottom: 8px; gap: 12px; flex-wrap: wrap; }
					.stg .zone-head b { font-size: 15px; }
					.stg .cells { display: grid; grid-template-columns: repeat(auto-fill, minmax(74px, 1fr)); gap: 6px; }
					.stg .cell { border: 1px solid var(--border-color); border-radius: 6px; padding: 5px 6px;
						min-height: 48px; font-size: 12px; cursor: pointer; background: var(--bg-light-gray);
						overflow: hidden; }
					.stg .cell b { display: block; font-size: 12px; }
					.stg .cell .sub { display: block; color: var(--text-muted); font-size: 11px;
						white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
					.stg .cell.free { background: var(--card-bg); }
					.stg .cell.gun { background: var(--blue-100, #e3f0fd); border-color: var(--blue-300, #9cc5f2); }
					.stg .cell.items { background: var(--green-100, #e4f5e9); border-color: var(--green-300, #8fd3a6); }
					.stg .cell.disabled { opacity: .45; text-decoration: line-through; }
					.stg .cell.dim { opacity: .25; }
					.stg .cell.hit { outline: 2px solid var(--primary); }
					.stg table { width: 100%; border-collapse: collapse; }
					.stg th, .stg td { padding: 7px 8px; border-bottom: 1px solid var(--border-color);
						font-size: 13px; vertical-align: middle; text-align: left; }
					.stg th { background: var(--bg-light-gray); font-weight: 600; }
					.stg td.num, .stg th.num { text-align: right; }
					.stg .scan-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
					.stg .scan-bar input.scan { flex: 1; min-width: 220px; font-size: 18px; padding: 8px 12px; }
					.stg .scan-bar input.qty { width: 80px; text-align: right; }
					.stg .dest { display: inline-block; padding: 3px 10px; border-radius: 12px;
						background: var(--blue-100, #e3f0fd); color: var(--blue-700, #1d4f91); font-weight: 600; }
					.stg .feedback { min-height: 40px; margin: 10px 0; padding: 9px 12px; border-radius: 6px;
						font-size: 14px; background: var(--bg-light-gray); }
					.stg .feedback.ok { background: var(--green-100, #e4f5e9); color: var(--green-700, #1f6b3a); }
					.stg .feedback.warn { background: var(--yellow-100, #fff5d6); color: var(--yellow-700, #7a5c00); }
					.stg .feedback.err { background: var(--red-100, #fde8e8); color: var(--red-700, #a12626); }
					.stg .badge-n { display: inline-block; min-width: 18px; padding: 0 6px; border-radius: 9px;
						background: var(--orange-100, #ffe9cc); color: var(--orange-700, #8a4b00); font-size: 11px; }
					.stg a.link-btn { cursor: pointer; }
					.stg .legend { display: flex; gap: 12px; font-size: 12px; margin: 0 0 10px; flex-wrap: wrap; }
					.stg .legend i { display: inline-block; width: 12px; height: 12px; border-radius: 3px;
						border: 1px solid var(--border-color); margin-right: 4px; vertical-align: -2px; }
					.stg .card-box:has(table) { overflow-x: auto; } /* wide tables scroll on a phone */
					/* Zones on a phone: the action links get their own full-width line under each zone. */
					@media (max-width: 767px) {
						.stg table.zones tr { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr) 6em minmax(0, 1.5fr); }
						.stg table.zones th:last-child { display: none; }
						.stg table.zones td:not(:last-child) { border-bottom: 0; }
						.stg table.zones td:last-child { grid-column: 1 / -1; text-align: left; padding-top: 0; }
					}
					.stg.wide { max-width: none; padding-inline: 15px; }
					.stg .plan-bar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
					.stg .plan-tabs { display: flex; gap: 6px; overflow-x: auto; flex: 1 1 200px; min-width: 0;
						-webkit-overflow-scrolling: touch; padding-bottom: 2px; }
					.stg .plan-tabs .btn { white-space: nowrap; flex: none; }
					.stg .plan-tabs .btn.active, .stg .view-mode.active { background: var(--primary); color: #fff; }
					.stg .plan-bar-end { display: flex; gap: 6px; align-items: center; margin-left: auto; }
					.stg .canvas { position: relative; height: 70vh; height: calc(100dvh - 300px); min-height: 360px;
						border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden;
						background: var(--bg-light-gray); margin-bottom: 12px; user-select: none; -webkit-user-select: none; }
					.stg .canvas.fit-plan { width: 100%; height: auto; max-height: 70vh;
						max-height: calc(100dvh - var(--stg-top, 300px)); min-height: 240px; }
					.stg .canvas.full { position: fixed; inset: 0; z-index: 1030; width: auto; height: auto; max-height: none;
						aspect-ratio: auto !important; border: 0; border-radius: 0; margin: 0; }
					.stg .canvas svg { display: block; width: 100%; height: 100%; touch-action: none; cursor: grab; }
					.stg .zoom-ctl { position: absolute; bottom: 8px; right: 8px; display: flex; gap: 4px; }
					.stg .zoom-ctl .btn { min-width: 36px; min-height: 36px; font-size: 16px; line-height: 1; }
					.stg svg * { vector-effect: non-scaling-stroke; }
					.stg svg .floor { fill: var(--card-bg); stroke: var(--gray-500, #9aa5b1); stroke-width: 2; }
					.stg svg .grid-line { fill: none; stroke: var(--border-color); stroke-width: 1; opacity: .6; }
					.stg svg text { fill: var(--text-color); font-family: inherit; pointer-events: none; }
					.stg svg text.cs { fill: var(--text-muted); }
					.stg svg .cell rect { fill: var(--card-bg); stroke: var(--gray-400, #cbd2d9); stroke-width: 1; }
					.stg svg .cell.gun rect { fill: var(--blue-100, #e3f0fd); stroke: var(--blue-300, #9cc5f2); }
					.stg svg .cell.items rect { fill: var(--green-100, #e4f5e9); stroke: var(--green-300, #8fd3a6); }
					.stg svg .cell.gun text, .stg svg .cell.items text { fill: #1f272e; } /* light fills, both themes */
					.stg svg .cell.gun text.cs, .stg svg .cell.items text.cs { fill: #4c5a67; }
					.stg svg .cell.disabled { opacity: .45; }
					.stg svg .cell.dim { opacity: .25; }
					.stg svg .cell.hit rect { stroke: var(--primary); stroke-width: 3; }
					.stg svg .view .cell { cursor: pointer; }
					.stg svg .zone.off { opacity: .45; }
					.stg svg .zone-box { fill: none; stroke: var(--gray-600, #6c7680); stroke-width: 1.5; }
					.stg svg .wall { fill: var(--text-color); opacity: .75; }
					.stg svg .door { fill: var(--card-bg); stroke: var(--gray-600, #6c7680); }
					.stg svg .door-swing { fill: none; stroke: var(--gray-500, #9aa5b1); stroke-dasharray: 4 3; }
					.stg svg .area { fill: var(--control-bg, #f4f5f6); stroke: var(--gray-500, #9aa5b1); stroke-dasharray: 6 3; }
					.stg svg .label-box { fill: transparent; }
					.stg svg .edit .label-box { stroke: var(--gray-300, #dfe3e7); stroke-dasharray: 2 3; }
					.stg svg .edit .shp { cursor: move; }
					.stg svg .fr-box { fill: none; stroke: var(--primary); stroke-width: 2; stroke-dasharray: 5 3; }
					.stg svg .fr-handle, .stg svg .fr-knob { fill: #fff; stroke: var(--primary); stroke-width: 2; }
					.stg svg .fr-line { stroke: var(--primary); stroke-width: 1.5; }
					.stg svg .fr-hit { fill: transparent; cursor: pointer; }
					.stg .unplaced-head { margin: 4px 0 8px; font-weight: 600; }
					.stg .edit-bar .tools { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
					.stg .edit-bar .tools .btn { min-height: 36px; }
					.stg .edit-bar .sep { width: 1px; align-self: stretch; background: var(--border-color); margin: 0 2px; }
					.stg .edit-bar .snap { margin: 0 4px; display: inline-flex; gap: 4px; align-items: center; font-weight: normal; }
					.stg .plan-edit { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 12px; align-items: start; }
					.stg .plan-edit .canvas { margin-bottom: 0; }
					.stg .side { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 10px 12px; }
					.stg .side .panel-sec + .panel-sec { border-top: 1px solid var(--border-color); margin-top: 10px; padding-top: 10px; }
					.stg .side .sec-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 6px; }
					.stg .side .props { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 8px; margin-bottom: 8px; }
					.stg .side .props label { font-size: 12px; color: var(--text-muted); margin: 0; font-weight: normal; }
					.stg .side .props label.wide { grid-column: 1 / -1; }
					.stg .side .props input { font-size: 16px; }
					.stg .side .free-zones { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
					.stg .side .free-zones .btn, .stg .side .btn { min-height: 34px; }
					.stg .sheet-head { display: none; width: 100%; justify-content: space-between; align-items: center;
						background: none; border: 0; padding: 0 0 6px; font-weight: 600; color: var(--text-color); }
					@media (max-width: 991px) {
						.stg .plan-edit { display: block; }
						.stg .plan-edit .canvas { height: 60vh; height: calc(100dvh - 330px); min-height: 300px; }
						.stg .plan-edit .side { position: sticky; bottom: 0; z-index: 5; margin-top: 8px; max-height: 45vh;
							overflow-y: auto; box-shadow: 0 -4px 14px rgba(0, 0, 0, .12); }
						.stg .sheet-head { display: flex; }
						.stg .side.collapsed .panel-body { display: none; }
					}
				</style>
				<div class="stg-view"></div>
			</div>`).appendTo(page.body);
		this.$view = this.$body.find('.stg-view');
		this.onShow();
	}

	// ------------------------------------------------------------- plumbing

	api(method, args, opts) {
		return frappe.call(Object.assign({ method: `${STG_API}.${method}`, args: args || {} }, opts || {}))
			.then((r) => r.message);
	}

	e(text) {
		return frappe.utils.escape_html(String(text === null || text === undefined ? '' : text));
	}

	onShow() {
		// Leaving the page mid-drawing only hides it: keep the draft as it is.
		if (this.edit) return;
		this.load().then(() => this.render());
	}

	load() {
		const jobs = [this.api('get_map'), this.api('get_options')];
		return Promise.all(jobs).then(([map, opts]) => {
			this.map = map;
			this.map.plans = map.plans || [];
			this.opts = opts;
			this.canManage = !!opts.can_manage;
			this.index = stgBuildIndex(map.zones);
			this.zoneByName = {};
			map.zones.forEach((z) => { this.zoneByName[z.name] = z; });
			this.attention = map.unassigned;
			if (this.destination && !Object.values(this.index).includes(this.destination)) this.destination = null;
			if (!this.map.plans.some((p) => p.name === this.planName)) this.planName = (this.map.plans[0] || {}).name || null;
		});
	}

	// ------------------------------------------------------------- shell

	render() {
		this.page.clear_primary_action();
		this.page.clear_menu();
		this.$body.toggleClass('wide', !!this.edit || (this.tab === 'map' && this.mode === 'plan' && this.map.plans.length > 0));
		// Desk's ⌘/Ctrl+S calls the page's save_action: it saves the plan being drawn.
		this.page.wrapper[0].save_action = this.edit ? () => this.savePlan() : null;
		if (this.edit) {
			// While drawing there is no tab row: the only ways out are Save and Cancel.
			this.$view.html('<div class="stg-tab"></div>');
			this.$tab = this.$view.find('.stg-tab');
			return this.renderEditor();
		}
		const tabs = [['map', __('Map')], ['move', __('Move')], ['unplaced', __('Not placed')]];
		if (this.canManage) tabs.push(['zones', __('Zones')]);
		const need = stgNeedsAttention(this.attention);
		this.$view.html(`
			<div class="tabs">${tabs.map(([k, label]) => `
				<button class="btn btn-sm btn-default tab ${this.tab === k ? 'active' : ''}" data-tab="${k}">${label}${
					k === 'unplaced' && need ? ` <span class="badge-n">${need}</span>` : ''}</button>`).join('')}</div>
			<div class="stg-tab"></div>`);
		this.$view.find('[data-tab]').on('click', (ev) => {
			this.tab = $(ev.currentTarget).attr('data-tab');
			if (this._stale) { this._stale = false; this.reload(); } else this.render();
		});
		this.$tab = this.$view.find('.stg-tab');
		if (this.tab === 'map') this.renderMap();
		else if (this.tab === 'move') this.renderMove();
		else if (this.tab === 'unplaced') this.renderUnplaced();
		else this.renderZones();
	}

	reload() {
		return this.load().then(() => this.render());
	}

	noZones() {
		return `<div class="card-box"><b>${__('No zones yet.')}</b>
			<div class="muted">${this.canManage
				? __('Open the Zones tab and create the first one.')
				: __('Ask a manager to set up the store zones.')}</div></div>`;
	}

	// ------------------------------------------------------------- map

	renderMap() {
		if (!this.map.zones.length) return this.$tab.html(this.noZones());
		const plans = this.map.plans;
		const showPlan = this.showPlan();
		this.$tab.html(`
			<div class="card-box">
				<input type="text" class="form-control map-search" autocomplete="off"
					placeholder="${__('Find a gun by serial or model, or a location by name')}" value="${this.e(this.mapSearch)}">
				<div class="legend" style="margin-top:10px">
					<span><i style="background:var(--card-bg)"></i>${__('free')}</span>
					<span><i style="background:var(--blue-100,#e3f0fd)"></i>${__('firearm')}</span>
					<span><i style="background:var(--green-100,#e4f5e9)"></i>${__('holds items')}</span>
					<span><i style="opacity:.45"></i>${__('disabled')}</span>
				</div>
			</div>
			${plans.length ? `<div class="plan-bar">
				<div class="plan-tabs"></div>
				${this.canManage && showPlan ? `<button class="btn btn-sm btn-default new-plan">+ ${__('Floor plan')}</button>` : ''}
				<div class="plan-bar-end">
					<div class="btn-group">
						<button class="btn btn-sm btn-default view-mode ${showPlan ? 'active' : ''}" data-mode="plan">${__('Plan')}</button>
						<button class="btn btn-sm btn-default view-mode ${showPlan ? '' : 'active'}" data-mode="list">${__('List')}</button>
					</div>
					${showPlan && this.canManage ? `<button class="btn btn-sm btn-primary edit-plan">${__('Edit layout')}</button>` : ''}
				</div>
			</div>` : ''}
			${!plans.length && this.canManage ? `<div class="card-box"><b>${__('Draw the store')}</b>
				<div class="muted" style="margin:4px 0 8px">${__('Make a floor plan of each room and put the zones where they really are.')}</div>
				<button class="btn btn-sm btn-primary new-plan">${__('Create floor plan')}</button></div>` : ''}
			${showPlan ? '<div class="canvas fit-plan"></div>' : ''}
			<div class="zones"></div>`);
		this.$tab.find('.map-search').on('input', (ev) => {
			this.mapSearch = ev.target.value;
			clearTimeout(this._searchTimer); // the whole map redraws: once typing pauses
			this._searchTimer = setTimeout(() => this.paintMap(), 150);
		});
		this.$tab.find('.view-mode').on('click', (ev) => {
			this.mode = $(ev.currentTarget).attr('data-mode');
			this.render();
		});
		this.$tab.find('.new-plan').on('click', () => this.newPlanDialog());
		this.$tab.find('.edit-plan').on('click', () => this.startEdit());
		if (showPlan) this.mountCanvas(this.$tab.find('.canvas'));
		this.paintMap();
	}

	showPlan() {
		return this.map.plans.length > 0 && this.mode === 'plan';
	}

	// The search, the plan tabs, the canvas and the zone cards: everything that changes as
	// the search box is typed in.
	paintMap() {
		this.hits = stgSearchMap(this.map.zones, this.mapSearch);
		if (!this.showPlan()) return this.paintZones(this.map.zones);
		const plans = this.map.plans;
		const by = stgpPlanHits(plans, this.map.zones, this.hits);
		// The plan on screen has none of what was searched for, another does: show that one.
		if (this.hits && !by[this.planName]) {
			const other = plans.find((p) => by[p.name]);
			if (other) this.planName = other.name;
		}
		this.$tab.find('.plan-tabs').html(plans.map((p) => `
			<button class="btn btn-sm btn-default plan-pill ${p.name === this.planName ? 'active' : ''}" data-plan="${this.e(p.name)}">
				${this.e(p.plan_name)}${by[p.name] ? ` <span class="badge-n">${by[p.name]}</span>` : ''}</button>`).join(''));
		this.$tab.find('.plan-pill').on('click', (ev) => {
			this.planName = $(ev.currentTarget).attr('data-plan');
			this.paintMap();
		});
		// Viewing, the canvas takes the plan's own shape (no tall empty band on a phone).
		const p = this.canvasPlan();
		this.$tab.find('.canvas.fit-plan').css('aspect-ratio', `${p.width + 2} / ${p.height + 2}`);
		this.fitCanvasHeight();
		this.paintCanvas();
		const free = stgpUnplaced(this.map.zones, plans);
		this.paintZones(free, free.length ? `<div class="unplaced-head">${__('Not on a plan yet')} (${free.length})${
			by.none ? ` <span class="badge-n">${by.none}</span>` : ''}</div>` : '');
	}

	paintZones(zones, head) {
		const hits = this.hits;
		const html = (head || '') + zones.map((z) => {
			const sum = stgZoneSummary(z);
			const cells = z.locations.map((l) => {
				const state = stgCellState(l, z.kind);
				const gun = (l.guns || [])[0];
				const sub = gun ? this.e(gun.item_name) : (l.qty ? `${l.items} · ${l.qty}` : '');
				const cls = ['cell', state, hits ? (hits.has(l.name) ? 'hit' : 'dim') : ''].join(' ');
				return `<div class="${cls}" data-loc="${this.e(l.name)}" title="${this.e(stgCellTitle(l, z.kind))}">
					<b>${this.e(l.label)}</b><span class="sub">${sub}</span></div>`;
			}).join('');
			return `<div class="card-box ${z.disabled ? 'muted' : ''}">
				<div class="zone-head"><b>${this.e(z.zone_name)}${z.disabled ? ` (${__('disabled')})` : ''}</b>
					<span class="muted">${z.kind === 'Slots' ? __('One firearm per slot') : __('Holds anything')} · ${sum.text}</span></div>
				<div class="cells">${cells}</div></div>`;
		}).join('');
		const $zones = this.$tab.find('.zones').html(html);
		$zones.find('.cell').on('click', (ev) => this.showLocation($(ev.currentTarget).attr('data-loc')));
	}

	// ------------------------------------------------------------- floor plan canvas
	// One SVG drawn at STGP_UNIT units per foot (text sized in fractions of a foot would trip
	// Chrome's minimum font size). The viewBox always has the canvas' own aspect, so a pixel
	// is the same both ways and pointer maths is a scale and an offset. Pointer Events only:
	// mouse, pen and fingers share one code path.

	mountCanvas($host) {
		$host.toggleClass('full', !this.edit && !!this.full);
		$host.html(`<svg xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none" role="img"
				aria-label="${__('Floor plan')}"></svg>
			<div class="zoom-ctl">
				<button class="btn btn-default btn-sm" data-zoom="in" aria-label="${__('Zoom in')}">+</button>
				<button class="btn btn-default btn-sm" data-zoom="out" aria-label="${__('Zoom out')}">−</button>
				<button class="btn btn-default btn-sm" data-zoom="fit">${__('Fit')}</button>
				${this.edit ? '' : `<button class="btn btn-default btn-sm" data-zoom="full">${this.full ? __('Close') : __('Full screen')}</button>`}
			</div>`);
		const svg = this.svg = $host.find('svg')[0];
		this.ptrs.clear();
		this.gesture = null;
		this.pinch = null;
		svg.addEventListener('pointerdown', (ev) => this.onPointerDown(ev));
		svg.addEventListener('pointermove', (ev) => this.onPointerMove(ev));
		svg.addEventListener('pointerup', (ev) => this.onPointerUp(ev, false));
		svg.addEventListener('pointercancel', (ev) => this.onPointerUp(ev, true)); // iPad system gestures
		svg.addEventListener('wheel', (ev) => this.onWheel(ev), { passive: false });
		$host.find('[data-zoom]').on('click', (ev) => this.zoomButton($(ev.currentTarget).attr('data-zoom')));
		if (this.resizeObs) this.resizeObs.disconnect();
		if (window.ResizeObserver) {
			// only a real size change repaints (observing fires once at once, after the first paint)
			this.resizeObs = new ResizeObserver(() => { if (this.box().width !== this.paintedWidth || this.box().height !== this.paintedHeight) this.paintCanvas(); });
			this.resizeObs.observe(svg);
		}
	}

	canvasPlan() {
		const ed = this.edit;
		if (ed) return { name: ed.plan, width: ed.width, height: ed.height, shapes: ed.shapes };
		const p = this.map.plans.find((x) => x.name === this.planName);
		return p ? { name: p.name, width: p.width, height: p.height, shapes: p.layout.shapes } : null;
	}

	box() {
		const r = this.svg.getBoundingClientRect();
		return { left: r.left, top: r.top, width: r.width || 1, height: r.height || 1 };
	}

	// What the canvas shows (feet), first the whole plan, then wherever it was panned to,
	// kept at the canvas' aspect when the canvas changes size.
	view() {
		const plan = this.canvasPlan(), b = this.box();
		let v = this.views[plan.name] || stgpFit(plan, b.width, b.height);
		const h = v.w * b.height / b.width;
		if (Math.abs(h - v.h) > 1e-6) v = Object.assign({}, v, { y: v.y + (v.h - h) / 2, h });
		this.views[plan.name] = v;
		return v;
	}

	setView(v) {
		this.views[this.canvasPlan().name] = v;
		this.svg.setAttribute('viewBox', [v.x, v.y, v.w, v.h].map((n) => n * STGP_UNIT).join(' '));
	}

	pxPerFt() {
		return this.box().width / this.view().w;
	}

	toPlan(ev) {
		const b = this.box(), v = this.view();
		return { x: v.x + (ev.clientX - b.left) * v.w / b.width, y: v.y + (ev.clientY - b.top) * v.h / b.height };
	}

	toCanvas(ev) {
		const b = this.box();
		return { x: ev.clientX - b.left, y: ev.clientY - b.top };
	}

	touch() {
		return this.lastPointer === 'touch' || !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
	}

	paintCanvas() {
		if (!this.svg || !this.svg.isConnected || !this.svg.getBoundingClientRect().width) return;
		const plan = this.canvasPlan();
		if (!plan) return;
		this.setView(this.view());
		this.svg.innerHTML = this.planSvg(plan);
		({ width: this.paintedWidth, height: this.paintedHeight } = this.box());
	}

	paintCanvasSoon() {
		clearTimeout(this._paintTimer);
		this._paintTimer = setTimeout(() => this.paintCanvas(), 120);
	}

	// While dragging, redraw only the shape being changed and its frame.
	paintShape(i) {
		if (this._raf) return;
		this._raf = requestAnimationFrame(() => {
			this._raf = null;
			const ed = this.edit;
			if (!this.svg || !ed || !ed.shapes[i]) return;
			const ppf = this.pxPerFt();
			const el = this.svg.querySelector(`[data-i="${i}"]`);
			if (el) this.swapSvg(el, this.shapeSvg(ed.shapes[i], i, ppf));
			const frame = this.svg.querySelector('.stg-frame');
			if (frame) this.swapSvg(frame, this.handlesSvg(ed.shapes[i], ppf));
		});
	}

	swapSvg(el, markup) {
		const tmp = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
		tmp.innerHTML = markup;
		el.replaceWith(...tmp.childNodes);
	}

	planSvg(plan) {
		const U = STGP_UNIT, ppf = this.pxPerFt();
		const grid = ppf >= 8 ? 1 : ppf >= 2 ? 5 : 10;
		const layer = { area: 0, wall: 1, door: 2, zone: 3, label: 4 };
		const order = plan.shapes.map((_, i) => i)
			.sort((a, b) => (layer[plan.shapes[a].type] - layer[plan.shapes[b].type]) || a - b);
		const sel = this.edit ? this.edit.sel : null;
		return `<defs><pattern id="stg-grid" width="${grid * U}" height="${grid * U}" patternUnits="userSpaceOnUse">
				<path class="grid-line" d="M ${grid * U} 0 L 0 0 0 ${grid * U}"/></pattern></defs>
			<g class="${this.edit ? 'edit' : 'view'}">
				<rect class="floor" x="0" y="0" width="${plan.width * U}" height="${plan.height * U}"/>
				<rect x="0" y="0" width="${plan.width * U}" height="${plan.height * U}" fill="url(#stg-grid)" pointer-events="none"/>
				${order.map((i) => this.shapeSvg(plan.shapes[i], i, ppf)).join('')}
				${sel !== null && plan.shapes[sel] ? this.handlesSvg(plan.shapes[sel], ppf) : ''}
			</g>`;
	}

	shapeSvg(s, i, ppf) {
		const U = STGP_UNIT, W = s.w * U, H = s.h * U;
		const rect = (cls) => `<rect class="${cls}" x="${-W / 2}" y="${-H / 2}" width="${W}" height="${H}"/>`;
		let body;
		if (s.type === 'zone') body = this.zoneSvg(s, ppf);
		else if (s.type === 'wall') body = rect('wall');
		else if (s.type === 'door') {
			// The opening, and the door's swing from its hinge (left end) out to the right end.
			body = rect('door') + `<path class="door-swing" d="M ${-W / 2} ${-H / 2} L ${-W / 2} ${-H / 2 - W} A ${W} ${W} 0 0 1 ${W / 2} ${-H / 2}"/>`;
		} else if (s.type === 'area') body = rect('area') + this.textSvg(s.text, s.w, s.h, s.r, ppf, 14, true);
		else body = rect('label-box') + this.textSvg(s.text, s.w, s.h, s.r, ppf, 16, false);
		return `<g class="shp t-${s.type}" data-i="${i}" transform="translate(${s.x * U} ${s.y * U}) rotate(${s.r || 0})">${body}</g>`;
	}

	// Text centred in a w × h (feet) box turned r degrees, as big as fits up to maxPx, wrapped
	// to the box; nothing at all when it would be too small to read. Zones and areas place it
	// by stgpTextFrame (level unless slanted); a label turns with its box (it is placed for its
	// text) but never reads upside down.
	textSvg(text, w, h, r, ppf, maxPx, upright) {
		const box = upright ? stgpTextFrame(w, h, r) : { w, h, turn: stgpTextTurn(r) };
		const fit = stgpFitText(text, box.w * ppf, box.h * ppf, maxPx, 8);
		return fit ? `<g transform="rotate(${box.turn})">${this.linesSvg(fit, ppf, 0, '')}</g>` : '';
	}

	// Wrapped lines centred on (0, y), at fit.px on screen.
	linesSvg(fit, ppf, y, attrs) {
		const fs = fit.px / ppf * STGP_UNIT, step = fs * 1.2, top = y - (fit.lines.length - 1) * step / 2;
		return `<text font-size="${fs}" text-anchor="middle" dominant-baseline="central" ${attrs}>${fit.lines.map((line, i) =>
			`<tspan x="0" y="${top + i * step}">${this.e(line)}</tspan>`).join('')}</text>`;
	}

	zoneSvg(s, ppf) {
		const U = STGP_UNIT, W = s.w * U, H = s.h * U;
		const z = this.zoneByName[s.zone];
		if (!z) return ''; // get_map only sends shapes of zones it also sends
		const open = z.kind !== 'Slots';
		const rects = open ? [{ x: 0, y: 0, w: s.w, h: s.h }] : stgpSlotCells(s.w, s.h, z.locations.length,
			s.rows || z.slot_lines, z.numbering === STG_IN_ORDER);
		// Slot labels share one size (from the longest) so a zone shows all of them or none.
		const longest = Math.max(2, ...z.locations.map((l) => String(l.label).length));
		const cells = (open ? z.locations.slice(0, 1) : z.locations)
			.map((l, k) => this.cellSvg(l, z, rects[k], s.r, ppf, open, longest)).join('');
		return `<g class="zone ${z.disabled ? 'off' : ''}" transform="translate(${-W / 2} ${-H / 2})">${cells}
			<rect class="zone-box" x="0" y="0" width="${W}" height="${H}"/></g>${open ? '' : this.zoneNameSvg(s, z, ppf)}`;
	}

	// A Slots zone's name, level on screen just above the top-left of where the zone shows
	// (whatever its turn), so it never sits under the turning knob or reads sideways. Drawn
	// inside the zone's own group (so a drag moves it too): the point is turned back into it.
	zoneNameSvg(s, z, ppf) {
		const U = STGP_UNIT, px = 12, b = stgpBounds([s]);
		if ((b.maxX - b.minX) * ppf < 16) return '';
		const dx = b.minX - s.x, dy = b.minY - px * 0.9 / ppf - s.y;
		const t = (s.r || 0) * Math.PI / 180, c = Math.cos(t), n = Math.sin(t);
		return `<g transform="translate(${(dx * c + dy * n) * U} ${(-dx * n + dy * c) * U}) rotate(${-(s.r || 0)})">
			<text font-size="${px / ppf * U}" font-weight="600" text-anchor="start" dominant-baseline="central">${this.e(z.zone_name)}</text></g>`;
	}

	cellSvg(l, z, c, r, ppf, open, longest) {
		const U = STGP_UNIT;
		const hit = this.hits ? (this.hits.has(l.name) ? ' hit' : ' dim') : '';
		const frame = stgpTextFrame(c.w, c.h, r); // level on screen unless the zone is slanted
		const wPx = frame.w * ppf, hPx = frame.h * ppf;
		const main = open ? z.zone_name : l.label;
		const centre = `translate(${(c.x + c.w / 2) * U} ${(c.y + c.h / 2) * U}) rotate(${frame.turn})`;
		let text = '';
		if (open) {
			// An open zone's name wraps to fill its shape (a long thin case, a square safe), with
			// what it holds on one more line when there is room.
			const sub = this.cellSub(l), subPx = 11;
			const withSub = !!sub && hPx >= 44 && wPx >= 44;
			const fit = stgpFitText(main, wPx, hPx - (withSub ? subPx * 1.6 : 0), 14, 8);
			if (fit) {
				const fs = fit.px / ppf * U, subFs = subPx / ppf * U;
				const chars = Math.floor((wPx * 0.9) / (subPx * 0.58));
				const block = fit.lines.length * fs * 1.2; // name block, then the line below it: centred as one
				text = `<g transform="${centre}">${this.linesSvg(fit, ppf, withSub ? -subFs * 0.7 : 0, 'font-weight="600"')}
					${withSub ? `<text class="cs" y="${block / 2}" font-size="${subFs}" text-anchor="middle" dominant-baseline="central">${
						this.e(sub.length > chars ? `${sub.slice(0, Math.max(1, chars - 1))}…` : sub)}</text>` : ''}</g>`;
			}
		}
		const px = open ? 0 : Math.min(13, hPx * 0.45, (wPx * 0.85) / (longest * 0.6));
		if (px >= 7) {
			const sub = this.cellSub(l), subPx = px * 0.85;
			const fit = Math.floor((wPx * 0.9) / (subPx * 0.58));
			const withSub = sub && hPx >= px * 3 && fit >= 4;
			const fs = px / ppf * U;
			text = `<g transform="${centre}">
				<text y="${withSub ? -fs * 0.6 : 0}" font-size="${fs}" font-weight="600" text-anchor="middle" dominant-baseline="central">${this.e(main)}</text>
				${withSub ? `<text class="cs" y="${fs * 0.75}" font-size="${subPx / ppf * U}" text-anchor="middle" dominant-baseline="central">${
					this.e(sub.length > fit ? `${sub.slice(0, fit - 1)}…` : sub)}</text>` : ''}</g>`;
		}
		return `<g class="cell ${stgCellState(l, z.kind)}${hit}" data-loc="${this.e(l.name)}">
			<rect x="${c.x * U}" y="${c.y * U}" width="${c.w * U}" height="${c.h * U}"/>
			<title>${this.e(stgCellTitle(l, z.kind))}</title>${text}</g>`;
	}

	cellSub(l) {
		const guns = l.guns || [], bits = [];
		if (guns.length === 1) bits.push(guns[0].item_name);
		else if (guns.length) bits.push(__('{0} firearms', [guns.length]));
		if (l.qty) bits.push(__('{0} units', [l.qty]));
		return bits.join(' · ');
	}

	// The selected shape's frame: resize handles and the turning knob at a constant size on
	// screen, each with a finger-sized (44 px) target on a touch screen. A shape too thin on
	// screen for handles on both sides keeps only its end handles (use the Width / Depth boxes).
	handlesSvg(s, ppf) {
		const U = STGP_UNIT, W = s.w * U, H = s.h * U, k = U / ppf;  // k: units per screen pixel
		const touch = this.touch();
		const hit = (touch ? 22 : 10) * k, r = 6 * k, knob = (touch ? 36 : 26) * k;
		const roomX = s.w * ppf >= (touch ? 48 : 24), roomY = s.h * ppf >= (touch ? 48 : 24);
		let hs = [[-1, -1], [0, -1], [1, -1], [1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0]];
		if (roomX && !roomY) hs = [[-1, 0], [1, 0]];
		else if (!roomX && roomY) hs = [[0, -1], [0, 1]];
		else if (!roomX && !roomY) hs = [[1, 1]];
		return `<g class="stg-frame" transform="translate(${s.x * U} ${s.y * U}) rotate(${s.r || 0})">
			<rect class="fr-box" x="${-W / 2}" y="${-H / 2}" width="${W}" height="${H}"/>
			<line class="fr-line" x1="0" y1="${-H / 2}" x2="0" y2="${-H / 2 - knob}"/>
			<circle class="fr-knob" cx="0" cy="${-H / 2 - knob}" r="${r * 1.2}"/>
			<circle class="fr-hit" data-h="rot" cx="0" cy="${-H / 2 - knob}" r="${hit}"/>
			${hs.map(([hx, hy]) => `<rect class="fr-handle" x="${hx * W / 2 - r}" y="${hy * H / 2 - r}" width="${2 * r}" height="${2 * r}"/>
				<circle class="fr-hit" data-h="${hx},${hy}" cx="${hx * W / 2}" cy="${hy * H / 2}" r="${hit}"/>`).join('')}
		</g>`;
	}

	// ------------------------------------------------------------- canvas gestures
	// One finger / the mouse on the floor pans; two fingers pinch-zoom. While drawing: a mouse
	// grabs a shape and drags it at once, a finger first taps to select, then drags it -- so
	// scrolling around a plan with a finger never moves anything by accident.

	onPointerDown(ev) {
		if (ev.pointerType === 'mouse' && ev.button !== 0) return;
		this.lastPointer = ev.pointerType;
		try { this.svg.setPointerCapture(ev.pointerId); } catch (e) { /* pointer already gone */ }
		this.ptrs.set(ev.pointerId, this.toCanvas(ev));
		if (this.ptrs.size === 2) return this.startPinch();
		if (this.ptrs.size > 2) return;
		const pick = (sel) => (ev.target && ev.target.closest ? ev.target.closest(sel) : null);
		const cell = pick('[data-loc]');
		const g = { id: ev.pointerId, start: this.toCanvas(ev), moved: false, kind: 'pan',
			view0: Object.assign({}, this.view()), cell: cell ? cell.getAttribute('data-loc') : null };
		const ed = this.edit;
		if (ed) {
			const handle = pick('[data-h]'), shape = pick('[data-i]');
			const i = shape ? Number(shape.getAttribute('data-i')) : null;
			if (handle && ed.sel !== null) {
				const h = handle.getAttribute('data-h');
				g.kind = h === 'rot' ? 'rotate' : 'resize';
				g.h = h.split(',').map(Number);
			} else if (i !== null && (i === ed.sel || ev.pointerType !== 'touch')) {
				if (i !== ed.sel) this.select(i);
				g.kind = 'move';
			} else {
				g.tap = i; // a tap selects that shape, or clears the selection on the floor
			}
			if (g.kind !== 'pan') {
				g.shape0 = Object.assign({}, ed.shapes[ed.sel]);
				g.p0 = this.toPlan(ev);
			}
		}
		this.gesture = g;
	}

	onPointerMove(ev) {
		if (!this.ptrs.has(ev.pointerId)) return;
		this.ptrs.set(ev.pointerId, this.toCanvas(ev));
		if (this.pinch) return this.movePinch();
		const g = this.gesture;
		if (!g || g.id !== ev.pointerId) return;
		const p = this.toCanvas(ev);
		if (!g.moved) {
			if (Math.hypot(p.x - g.start.x, p.y - g.start.y) < 4) return; // still a tap
			g.moved = true;
			if (g.kind !== 'pan') this.pushUndo();                          // one undo step per drag
		}
		const plan = this.canvasPlan();
		if (g.kind === 'pan') {
			return this.setView(stgpPinch(g.view0, g.start, g.start, p, p, this.box().width, stgpZoomLimits(plan)));
		}
		const ed = this.edit, at = this.toPlan(ev), step = ed.snap ? 0.5 : 0;
		let next;
		if (g.kind === 'move') next = stgpMoveTo(g.shape0, g.shape0.x + at.x - g.p0.x, g.shape0.y + at.y - g.p0.y, plan, step);
		else if (g.kind === 'rotate') next = stgpRotateTo(g.shape0, at.x, at.y, ev.shiftKey || !ed.snap);
		else {
			next = stgpResize(g.shape0, g.h[0], g.h[1], at.x, at.y, step, Math.max(plan.width, plan.height));
			if (!stgpInside(next, plan)) return; // its centre would leave the plan
		}
		ed.shapes[ed.sel] = next;
		ed.dirty = true;
		this.paintShape(ed.sel);
	}

	onPointerUp(ev, cancelled) {
		if (!this.ptrs.has(ev.pointerId)) return;
		this.ptrs.delete(ev.pointerId);
		if (this.pinch) {
			if (!this.ptrs.size) { this.pinch = null; this.paintCanvas(); }
			return;
		}
		const g = this.gesture;
		this.gesture = null;
		if (!g || g.id !== ev.pointerId) return;
		if (g.moved) {
			if (g.kind !== 'pan') { this.paintCanvas(); this.paintPanel(); }
			return;
		}
		if (cancelled) return;
		if (!this.edit) { if (g.cell) this.showLocation(g.cell); return; }
		if (g.kind === 'pan') this.select(g.tap);
	}

	startPinch() {
		// A second finger ends whatever the first was doing (a drag keeps where it got to).
		if (this.gesture && this.gesture.moved && this.gesture.kind !== 'pan') this.paintPanel();
		this.gesture = null;
		const [a, b] = [...this.ptrs.values()];
		this.pinch = { a0: a, b0: b, view0: Object.assign({}, this.view()) };
	}

	movePinch() {
		if (this.ptrs.size < 2) return;
		const [a, b] = [...this.ptrs.values()];
		this.setView(stgpPinch(this.pinch.view0, this.pinch.a0, this.pinch.b0, a, b, this.box().width,
			stgpZoomLimits(this.canvasPlan())));
	}

	onWheel(ev) {
		// ⌘/Ctrl + wheel zooms (a trackpad pinch arrives like that too); a plain wheel scrolls the page.
		if (!(ev.ctrlKey || ev.metaKey)) return;
		ev.preventDefault();
		const at = this.toPlan(ev);
		const factor = Math.exp(stgpClamp(-ev.deltaY, -100, 100) * 0.005);
		this.setView(stgpZoomAt(this.view(), factor, at.x, at.y, stgpZoomLimits(this.canvasPlan())));
		this.paintCanvasSoon();
	}

	zoomButton(kind) {
		if (kind === 'full') return this.fullScreen(!this.svg.parentNode.classList.contains('full'));
		const plan = this.canvasPlan(), v = this.view(), b = this.box();
		this.views[plan.name] = kind === 'fit' ? stgpFit(plan, b.width, b.height)
			: stgpZoomAt(v, kind === 'in' ? 1.4 : 1 / 1.4, v.x + v.w / 2, v.y + v.h / 2, stgpZoomLimits(plan));
		this.paintCanvas();
	}

	// The map over the whole window (not the browser's full screen, which would hide the
	// location dialogs); it opens fitted to the plan, Esc or the button closes it.
	fullScreen(on) {
		this.full = on; // kept across reloads (a location dialog's Disable redraws the map)
		const host = this.svg.parentNode;
		host.classList.toggle('full', on);
		if (!on) this.fitCanvasHeight(); // measured while it was over the window: measure again
		$(host).find('[data-zoom="full"]').text(on ? __('Close') : __('Full screen'));
		delete this.views[this.canvasPlan().name]; // refit to the new size
		if (!this.resizeObs) this.paintCanvas(); // otherwise the resize observer repaints
	}

	// The viewing canvas reaches down to the bottom of the window, whatever sits above it.
	// Measured before each paint (the plan pills above can wrap as search badges come and go).
	// lazy: a window resize that rewraps the bars keeps the old offset until the next paint (a
	// resize listener would fix it).
	fitCanvasHeight() {
		const host = this.$tab.find('.canvas.fit-plan')[0];
		if (host && !host.classList.contains('full')) host.style.setProperty('--stg-top', `${Math.round(host.getBoundingClientRect().top + window.scrollY + 12)}px`);
	}

	// ------------------------------------------------------------- floor plan editor

	newPlanDialog() {
		const d = new frappe.ui.Dialog({
			title: __('New floor plan'),
			fields: [
				{ fieldname: 'plan_name', fieldtype: 'Data', label: __('Name'), reqd: 1 },
				{ fieldtype: 'Section Break' },
				{ fieldname: 'width', fieldtype: 'Float', label: __('Width (ft)'), reqd: 1, default: 60 },
				{ fieldtype: 'Column Break' },
				{ fieldname: 'height', fieldtype: 'Float', label: __('Depth (ft)'), reqd: 1, default: 40 },
			],
			primary_action_label: __('Create'),
			primary_action: (v) => {
				const problem = stgpCheckPlan(v, []);
				if (problem) return frappe.msgprint({ message: __(problem), indicator: 'red' });
				if (d.creating) return; // a double tap on a slow tablet
				d.creating = true;
				this.api('create_floor_plan', { plan_name: v.plan_name.trim(), width: v.width, height: v.height,
					company: this.map.company }, { freeze: true }).then((r) => {
					d.hide();
					this.planName = r.plan;
					this.mode = 'plan';
					return this.startEdit(); // it loads the map first
				}, () => { d.creating = false; });
			},
		});
		d.show();
	}

	// Always from the plan as saved now (the page may have been open for hours), so a save
	// made meanwhile is not found only at the end of a drawing session.
	startEdit() {
		const planName = this.planName;
		return this.load().then(() => {
			const p = this.map.plans.find((x) => x.name === planName);
			if (!p) {
				frappe.show_alert({ message: __('That plan was deleted.'), indicator: 'orange' }, 5);
				return this.render();
			}
			delete this.views[p.name]; // the editor's canvas is narrower: start from the whole plan
			this.edit = { plan: p.name, plan_name: p.plan_name, width: p.width, height: p.height, modified: p.modified,
				shapes: JSON.parse(JSON.stringify(p.layout.shapes)), sel: null, undo: [], dirty: false, snap: true, sheet: true };
			this.render();
		});
	}

	renderEditor() {
		const ed = this.edit;
		this.hits = null; // the Map tab's search does not dim the drawing
		this.page.set_primary_action(__('Save'), () => this.savePlan());
		this.$tab.html(`
			<div class="card-box edit-bar">
				<div><b>${__('Drawing {0}', [this.e(ed.plan_name)])}</b> <span class="muted">${ed.width} × ${ed.height} ft</span></div>
				<div class="tools">
					<button class="btn btn-sm btn-default" data-add="wall">+ ${__('Wall')}</button>
					<button class="btn btn-sm btn-default" data-add="door">+ ${__('Door')}</button>
					<button class="btn btn-sm btn-default" data-add="area">+ ${__('Area')}</button>
					<button class="btn btn-sm btn-default" data-add="label">+ ${__('Label')}</button>
					<span class="sep"></span>
					<button class="btn btn-sm btn-default" data-act="undo">${__('Undo')}</button>
					<button class="btn btn-sm btn-default" data-act="rotate">${__('Rotate 90°')}</button>
					<button class="btn btn-sm btn-default" data-act="remove">${__('Delete')}</button>
					<span class="sep"></span>
					<label class="snap"><input type="checkbox" class="snap-on" ${ed.snap ? 'checked' : ''}> ${__('Snap to ½ ft')}</label>
					<button class="btn btn-sm btn-default" data-act="settings">${__('Plan settings')}</button>
					<button class="btn btn-sm btn-default" data-act="cancel">${__('Cancel')}</button>
				</div>
			</div>
			<div class="plan-edit">
				<div class="canvas"></div>
				<div class="side ${ed.sheet ? '' : 'collapsed'}"></div>
			</div>`);
		this.$tab.find('[data-add]').on('click', (ev) => this.addShape($(ev.currentTarget).attr('data-add')));
		this.$tab.find('[data-act]').on('click', (ev) => {
			const act = $(ev.currentTarget).attr('data-act');
			if (act === 'undo') this.undo();
			else if (act === 'rotate') this.rotateSelected();
			else if (act === 'remove') this.removeSelected();
			else if (act === 'settings') this.planSettingsDialog();
			else this.cancelEdit();
		});
		this.$tab.find('.snap-on').on('change', (ev) => { ed.snap = ev.target.checked; });
		this.mountCanvas(this.$tab.find('.canvas'));
		this.paintCanvas();
		this.paintPanel();
	}

	paintPanel() {
		const ed = this.edit;
		if (!ed) return;
		const s = ed.sel !== null ? ed.shapes[ed.sel] : null;
		const z = s && s.type === 'zone' ? this.zoneByName[s.zone] : null;
		const kinds = { wall: __('Wall'), door: __('Door'), area: __('Area'), label: __('Label') };
		const title = !s ? '' : s.type === 'zone' ? (z ? z.zone_name : s.zone) : kinds[s.type];
		const free = stgpUnplaced(this.map.zones, this.map.plans, { plan: ed.plan, shapes: ed.shapes });
		const field = (p, label, value, mode) => `<label>${label}<input class="form-control input-sm" data-p="${p}"
			inputmode="${mode || 'decimal'}" value="${this.e(value)}"></label>`;
		const props = s ? `<div class="panel-sec">
				<div class="sec-head"><b>${this.e(title)}</b>
					<button class="btn btn-xs btn-default remove-sel">${s.type === 'zone' ? __('Take off the plan') : __('Delete')}</button></div>
				<div class="props">
					${field('w', __('Width (ft)'), s.w)}${field('h', __('Depth (ft)'), s.h)}${field('r', __('Turn (°)'), s.r || 0)}
					${z && z.kind === 'Slots' ? `<label title="${__('How many lines of slots run along the long side')}">${__('Lines of slots')}<input class="form-control input-sm" data-p="rows"
						inputmode="numeric" placeholder="${z.slot_lines || __('auto')}" value="${s.rows || ''}"></label>` : ''}
					${s.type === 'area' || s.type === 'label' ? `<label class="wide">${__('Text')}<input class="form-control input-sm"
						data-p="text" maxlength="40" value="${this.e(s.text || '')}"></label>` : ''}
				</div></div>` : '';
		const zones = `<div class="panel-sec">
				<div class="sec-head"><b>${__('Zones not on a plan')} (${free.length})</b>
					${free.length > 1 ? `<button class="btn btn-xs btn-primary place-all">${__('Place all')}</button>` : ''}</div>
				${free.length ? `<div class="free-zones">${free.map((f) => `<button class="btn btn-xs btn-default place-one"
						data-zone="${this.e(f.name)}">${this.e(f.zone_name)}</button>`).join('')}</div>
					<div class="muted small">${__('Tap a zone to put it in the middle of the view, then drag it into place.')}</div>`
				: `<div class="muted small">${__('Every zone is on a plan.')}</div>`}</div>`;
		const hint = s ? '' : `<div class="panel-sec muted small">${__('Tap a shape to select it, then drag it to move it or use its handles to resize and turn it.')}</div>`;
		const $side = this.$tab.find('.side');
		$side.html(`<button class="sheet-head" type="button"><span>${s ? __('Shape settings') : __('Zones and shapes')}</span>
				<span>${ed.sheet ? '▾' : '▴'}</span></button>
			<div class="panel-body">${props}${zones}${hint}</div>`);
		$side.find('.sheet-head').on('click', () => {
			ed.sheet = !ed.sheet;
			$side.toggleClass('collapsed', !ed.sheet);
			this.paintPanel();
		});
		$side.find('.remove-sel').on('click', () => this.removeSelected());
		$side.find('[data-p]').on('change', (ev) => this.setProp($(ev.currentTarget).attr('data-p'), ev.target.value));
		$side.find('.place-one').on('click', (ev) => this.placeZone($(ev.currentTarget).attr('data-zone')));
		$side.find('.place-all').on('click', () => this.placeAll(free));
		this.$tab.find('[data-act="remove"]').text(s && s.type === 'zone' ? __('Take off plan') : __('Delete')).prop('disabled', !s);
		this.$tab.find('[data-act="rotate"]').prop('disabled', !s);
		this.$tab.find('[data-act="undo"]').prop('disabled', !ed.undo.length);
	}

	// Every change: one undo step, the draft marked unsaved, canvas and panel redrawn.
	change(fn, sel) {
		const ed = this.edit;
		ed.undo = stgpPushUndo(ed.undo, ed);
		fn(ed);
		ed.dirty = true;
		ed.sel = sel === undefined ? ed.sel : sel;
		this.paintCanvas();
		this.paintPanel();
	}

	pushUndo() {
		this.edit.undo = stgpPushUndo(this.edit.undo, this.edit);
	}

	undo() {
		const ed = this.edit;
		if (!ed || !ed.undo.length) return;
		const snap = JSON.parse(ed.undo.pop());
		const resized = snap.width !== ed.width || snap.height !== ed.height;
		const renamed = snap.plan_name !== ed.plan_name;
		Object.assign(ed, snap, { sel: null, dirty: true });
		if (resized || renamed) {
			if (resized) delete this.views[ed.plan];
			return this.render(); // the header shows the name and size
		}
		this.paintCanvas();
		this.paintPanel();
	}

	select(i) {
		const ed = this.edit;
		ed.sel = i === null || i === undefined || !ed.shapes[i] ? null : i;
		this.paintCanvas();
		this.paintPanel();
	}

	setProp(p, raw) {
		const ed = this.edit, s = ed.shapes[ed.sel];
		if (!s) return;
		const plan = this.canvasPlan(), big = Math.max(plan.width, plan.height), text = String(raw).trim();
		const next = Object.assign({}, s);
		if (p === 'text') {
			if (/[<>]/.test(text)) return this.badInput(__('Text on the plan cannot contain < or >.'));
			next.text = text.slice(0, 40);
		}
		else if (p === 'rows') {
			if (!text) delete next.rows;
			else if (/^\d+$/.test(text) && Number(text) >= 1 && Number(text) <= STGP_MAX_LINES) next.rows = Number(text);
			else return this.badInput(__('Lines go from 1 to {0}. Leave it empty to fit them automatically.', [STGP_MAX_LINES]));
		} else {
			const n = Number(text);
			if (!text || !Number.isFinite(n)) return this.badInput(__('Type a number.'));
			if (p === 'r') next.r = stgpAngle(n);
			else if (n >= STGP_MIN && n <= big) next[p] = stgpSnap(n, 0);
			else return this.badInput(__('Between {0} and {1} feet.', [STGP_MIN, big]));
		}
		this.change((e) => { e.shapes[e.sel] = next; });
	}

	badInput(message) {
		frappe.show_alert({ message, indicator: 'orange' }, 4);
		this.paintPanel(); // puts the old value back
	}

	removeSelected() {
		if (!this.edit || this.edit.sel === null) return;
		this.change((e) => { e.shapes.splice(e.sel, 1); }, null);
	}

	rotateSelected() {
		if (!this.edit || this.edit.sel === null) return;
		this.change((e) => {
			const s = e.shapes[e.sel];
			e.shapes[e.sel] = Object.assign({}, s, { r: stgpAngle((s.r || 0) + 90) });
		});
	}

	nudge(dx, dy, step) {
		this.pushUndo();
		const ed = this.edit, s = ed.shapes[ed.sel];
		ed.shapes[ed.sel] = stgpMoveTo(s, s.x + dx * step, s.y + dy * step, this.canvasPlan(), 0);
		ed.dirty = true;
		this.paintShape(ed.sel);
		this.$tab.find('[data-act="undo"]').prop('disabled', false);
	}

	viewCentre() {
		const v = this.view(), plan = this.canvasPlan();
		return { x: stgpClamp(v.x + v.w / 2, 0, plan.width), y: stgpClamp(v.y + v.h / 2, 0, plan.height) };
	}

	addShape(type) {
		const c = this.viewCentre(), plan = this.canvasPlan();
		this.change((e) => { e.shapes.push(stgpNewShape(type, c.x, c.y, plan)); }, this.edit.shapes.length);
	}

	placeZone(name) {
		const z = this.zoneByName[name];
		if (!z) return;
		const c = this.viewCentre(), plan = this.canvasPlan();
		this.change((e) => { e.shapes.push(stgpZoneShape(z, c.x, c.y, plan)); }, this.edit.shapes.length);
	}

	placeAll(zones) {
		const plan = this.canvasPlan(), b = this.box(), box = stgpBounds(this.edit.shapes);
		const placed = stgpAutoPlace(zones, plan, this.edit.shapes);
		this.views[plan.name] = stgpFit(plan, b.width, b.height);
		this.change((e) => { e.shapes = e.shapes.concat(placed); }, null);
		if (box && placed.some((s) => s.y - s.h / 2 < box.maxY)) {
			frappe.show_alert({ indicator: 'orange', message: __('Not every zone fitted below the drawing, so some went on top of it. Drag them into place, or make the plan bigger in Plan settings.') }, 8);
		}
	}

	onKey(ev) {
		const ed = this.edit;
		if (ev.key === 'Escape' && !ed && this.full && this.svg && this.page.wrapper.is(':visible')
			&& !(window.cur_dialog && window.cur_dialog.display)) return this.fullScreen(false);
		if (!ed || !this.page.wrapper.is(':visible')) return;
		const t = ev.target || {};
		if (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || '') || t.isContentEditable) return;
		if (window.cur_dialog && window.cur_dialog.display) return;
		const mod = ev.metaKey || ev.ctrlKey;
		if (mod && !ev.shiftKey && (ev.key === 'z' || ev.key === 'Z')) { ev.preventDefault(); return this.undo(); }
		if (ed.sel === null || mod) return;
		if (ev.key === 'Delete' || ev.key === 'Backspace') { ev.preventDefault(); return this.removeSelected(); }
		if (ev.key === 'Escape') return this.select(null);
		const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[ev.key];
		if (d) { ev.preventDefault(); this.nudge(d[0], d[1], ev.shiftKey ? 5 : 0.5); }
	}

	planSettingsDialog() {
		const ed = this.edit;
		const d = new frappe.ui.Dialog({
			title: __('Plan settings'),
			fields: [
				{ fieldname: 'plan_name', fieldtype: 'Data', label: __('Name'), reqd: 1, default: ed.plan_name },
				{ fieldtype: 'Section Break' },
				{ fieldname: 'width', fieldtype: 'Float', label: __('Width (ft)'), reqd: 1, default: ed.width },
				{ fieldtype: 'Column Break' },
				{ fieldname: 'height', fieldtype: 'Float', label: __('Depth (ft)'), reqd: 1, default: ed.height },
			],
			primary_action_label: __('Apply'),
			primary_action: (v) => {
				const problem = stgpCheckPlan(v, ed.shapes);
				if (problem) return frappe.msgprint({ message: __(problem), indicator: 'red' });
				this.pushUndo();
				Object.assign(ed, { plan_name: v.plan_name.trim(), width: Number(v.width), height: Number(v.height), dirty: true });
				delete this.views[ed.plan];
				d.hide();
				this.render();
			},
			secondary_action_label: __('Delete this plan'),
			secondary_action: () => {
				d.hide();
				frappe.confirm(__('Delete the plan {0}? Its zones stay as they are, just on no plan.', [this.e(ed.plan_name)]), () =>
					this.api('delete_floor_plan', { plan: ed.plan, company: this.map.company }).then(() => {
						this.edit = null;
						frappe.show_alert({ message: __('Plan deleted.'), indicator: 'green' }, 5);
						this.reload();
					}));
			},
		});
		d.show();
	}

	savePlan() {
		const ed = this.edit;
		if (!ed || ed.saving) return;
		ed.saving = true;
		return this.api('save_floor_plan', {
			plan: ed.plan, plan_name: ed.plan_name, width: ed.width, height: ed.height, shapes: ed.shapes, modified: ed.modified,
			company: this.map.company,
		}, { freeze: true, freeze_message: __('Saving the plan…') }).then((r) => {
			if (this.edit !== ed) return;
			this.edit = null;
			delete this.views[ed.plan];
			frappe.show_alert({ indicator: 'green', message: r.dropped.length
				? __('Plan saved. {0} deleted zone(s) were left out.', [r.dropped.length]) : __('Plan saved.') }, 5);
			return this.reload();
		}, () => { ed.saving = false; });
	}

	cancelEdit() {
		const leave = () => { delete this.views[this.edit.plan]; this.edit = null; this.reload(); };
		if (!this.edit.dirty) return leave();
		frappe.confirm(__('Throw away your changes to this plan?'), leave);
	}

	showLocation(name) {
		this.api('get_location', { location: name }).then((r) => {
			const guns = r.guns.map((g) => `<tr><td>${this.e(g.serial)}</td><td>${this.e(g.item_name)}</td>
				<td>${this.e([g.manufacturer, g.model_name, g.caliber].filter(Boolean).join(' '))}</td></tr>`).join('');
			const items = r.items.map((i) => `<tr><td>${this.e(i.item_name)}</td>
				<td class="num">${this.e(i.qty)}</td></tr>`).join('');
			const d = new frappe.ui.Dialog({
				title: this.e(r.location.label),
				fields: [{ fieldtype: 'HTML', fieldname: 'body' }],
				primary_action_label: __('Put things here'),
				primary_action: () => { d.hide(); this.destination = r.location.name; this.tab = 'move'; this.render(); },
				secondary_action_label: this.canManage ? (r.location.disabled ? __('Enable') : __('Disable')) : null,
				secondary_action: this.canManage ? () => {
					this.api('set_disabled', { location: r.location.name, disabled: r.location.disabled ? 0 : 1 })
						.then(() => { d.hide(); this.reload(); });
				} : null,
			});
			d.fields_dict.body.$wrapper.html(`
				${guns ? `<table class="stg"><thead><tr><th>${__('Serial')}</th><th>${__('Item')}</th><th>${__('Details')}</th></tr></thead><tbody>${guns}</tbody></table>` : ''}
				${items ? `<table class="stg" style="margin-top:8px"><thead><tr><th>${__('Item')}</th><th class="num">${__('Qty')}</th></tr></thead><tbody>${items}</tbody></table>` : ''}
				${!guns && !items ? `<div class="muted">${__('Nothing in here.')}</div>` : ''}`);
			d.show();
		});
	}

	// ------------------------------------------------------------- move

	renderMove() {
		if (!this.map.zones.length) return this.$tab.html(this.noZones());
		this.$tab.html(`
			<div class="card-box">
				<div style="margin-bottom:8px">${__('Putting things into')}:
					<span class="dest-chip">${this.destChip()}</span></div>
				<div class="scan-bar">
					<input type="text" class="scan form-control" autocomplete="off" autocapitalize="off"
						placeholder="${__('Scan a location label first, then scan what goes in it')}">
					<label class="muted">${__('Qty')} <input type="number" min="1" step="1" value="1" class="qty form-control" style="display:inline-block"></label>
				</div>
				<div class="feedback">${this.destination ? __('Ready. Scan a firearm serial or a barcode.') : __('Scan a location label to start.')}</div>
			</div>
			<div class="card-box"><b>${__('This session')}</b><div class="session muted" style="margin-top:6px"></div></div>`);
		this.$scan = this.$tab.find('input.scan');
		this.$feedback = this.$tab.find('.feedback');
		// Opened with a destination already set (Map's "Put things here", or a
		// tab switch back): setDestination is the one place that binds 'change'.
		if (this.destination) this.setDestination(this.destination);
		this.$scan.on('keydown', (ev) => {
			if (ev.key !== 'Enter') return;
			ev.preventDefault();
			const code = this.$scan.val();
			this.$scan.val('');
			this.onScan(code);
		});
		this.paintSession();
		this.$scan.focus();
	}

	destChip() {
		if (!this.destination) return `<span class="muted">${__('no location yet')}</span>`;
		const zone = this.map.zones.find((z) => z.locations.some((l) => l.name === this.destination));
		const loc = zone && zone.locations.find((l) => l.name === this.destination);
		return `<span class="dest">${this.e(loc ? loc.label : this.destination)}</span>
			<a class="link-btn clear-dest" style="margin-left:8px">${__('change')}</a>`;
	}

	setDestination(name) {
		this.destination = name;
		this.$tab.find('.dest-chip').html(this.destChip());
		this.$tab.find('.clear-dest').on('click', () => {
			this.destination = null;
			this.renderMove();
		});
	}

	say(kind, text) {
		this.$feedback.removeClass('ok warn err').addClass(kind).text(text);
	}

	onScan(code) {
		const plan = stgScanPlan(this.index, this.destination, code);
		if (plan.action === 'none') return;
		if (plan.action === 'destination') {
			this.setDestination(plan.location);
			const loc = this.locationLabel(plan.location);
			this.say('ok', __('Putting things into {0}. Scan what goes in it.', [loc]));
			return;
		}
		if (plan.action === 'need_destination') {
			this.say('warn', __('Scan a location label first (like A5), then scan what goes in it.'));
			return;
		}
		this.doMove(plan.to, plan.code, null);
	}

	locationLabel(name) {
		for (const z of this.map.zones) {
			const l = z.locations.find((x) => x.name === name);
			if (l) return l.label;
		}
		return name;
	}

	doMove(to, code, fromLocation) {
		const qty = Number(this.$tab.find('input.qty').val()) || 1;
		this.api('scan_move', { to_location: to, code, qty, from_location: fromLocation || undefined },
			{ freeze: false, no_spinner: true }).then((r) => {
			if (r.result === 'error') return this.say('err', r.message);
			if (r.result === 'already') return this.say('warn', r.message);
			if (r.result === 'choose') return this.chooseSource(to, code, r);
			this.say('ok', r.message);
			this.session = stgPushSession(this.session, Object.assign({ at: frappe.datetime.now_time() }, r));
			this.paintSession();
			this.$tab.find('input.qty').val(1);
			// The map is not refetched per scan (a scanner fires many a minute): the session list
			// is already up to date, and the map reloads when another tab is opened.
			this._stale = true;
		}).finally(() => this.$scan && this.$scan.focus());
	}

	chooseSource(to, code, r) {
		const d = new frappe.ui.Dialog({
			title: __('Where is it coming from?'),
			fields: [
				{ fieldtype: 'HTML', fieldname: 'note' },
				{ fieldname: 'from_location', fieldtype: 'Select', label: __('Take from'), reqd: 1,
					options: r.options.map((o) => ({ value: o.location, label: `${this.e(o.label)} (${this.e(o.qty)})` })) },
			],
			primary_action_label: __('Move'),
			primary_action: (v) => { d.hide(); this.doMove(to, code, v.from_location); },
		});
		d.fields_dict.note.$wrapper.html(`<div class="muted">${this.e(r.message)}</div>`);
		d.show();
	}

	paintSession() {
		const $s = this.$tab.find('.session');
		if (!this.session.length) return $s.text(__('Nothing moved yet.'));
		$s.html(`<table>${this.session.map((e) => `<tr class="${e.undone ? 'muted' : ''}">
			<td>${this.e(e.at || '')}</td><td>${this.e(e.message)}${e.undone ? ` (${__('undone')})` : ''}</td>
			<td class="num">${stgCanUndo(e) ? `<a class="link-btn undo" data-move="${this.e(e.move)}">${__('Undo')}</a>` : ''}</td></tr>`).join('')}</table>`);
		$s.find('.undo').on('click', (ev) => {
			const move = $(ev.currentTarget).attr('data-move');
			this.api('undo_move', { move }, { freeze: false, no_spinner: true }).then((r) => {
				this.say(r.ok ? 'ok' : 'err', r.message);
				if (r.ok) { this.session = stgMarkUndone(this.session, move); this.paintSession(); this._stale = true; }
			});
		});
	}

	// ------------------------------------------------------------- not placed

	renderUnplaced() {
		this.$tab.html(`<div class="card-box muted">${__('Loading…')}</div>`);
		this.api('get_unassigned').then((u) => {
			this.unassigned = u;
			this.paintUnplaced();
		});
	}

	paintUnplaced() {
		const u = this.unassigned;
		const gunRows = u.guns.map((g) => `<tr><td>${this.e(g.serial)}</td><td>${this.e(g.item_name)}</td>
			<td>${this.e([g.manufacturer, g.model_name, g.caliber].filter(Boolean).join(' '))}</td>
			<td class="num"><button class="btn btn-xs btn-default place-gun" data-serial="${this.e(g.serial)}">${__('Place')}</button></td></tr>`).join('');
		const itemRows = u.items.map((i) => `<tr><td>${this.e(i.item_name)}</td><td class="num">${this.e(i.stock)}</td>
			<td class="num">${this.e(i.unassigned)}</td>
			<td class="num"><button class="btn btn-xs btn-default place-item" data-item="${this.e(i.item_code)}" data-qty="${this.e(i.unassigned)}">${__('Place')}</button></td></tr>`).join('');
		const confirmRows = u.to_confirm.map((i) => `<tr><td>${this.e(i.item_name)}</td><td class="num">${this.e(i.over)}</td>
			<td class="num"><button class="btn btn-xs btn-default confirm-item" data-item="${this.e(i.item_code)}" data-qty="${this.e(i.over)}">${__('Say where from')}</button></td></tr>`).join('');
		const none = (text) => `<div class="muted">${text}</div>`;
		this.$tab.html(`
			<div class="card-box"><b>${__('Sold or shipped without a location')} (${u.to_confirm.length})</b>
				<div class="muted" style="margin:4px 0 8px">${__('These left the shop with no location recorded; say where each was taken from so the location counts stay right.')}</div>
				${confirmRows ? `<table><thead><tr><th>${__('Item')}</th><th class="num">${__('Units to confirm')}</th><th></th></tr></thead><tbody>${confirmRows}</tbody></table>` : none(__('Nothing to confirm.'))}</div>
			<div class="card-box"><b>${__('Firearms not in a location')} (${u.guns.length})</b>
				${gunRows ? `<table style="margin-top:8px"><thead><tr><th>${__('Serial')}</th><th>${__('Item')}</th><th>${__('Details')}</th><th></th></tr></thead><tbody>${gunRows}</tbody></table>` : none(__('Every firearm is in a location.'))}</div>
			<div class="card-box"><b>${__('Items not in a location')} (${u.items.length})</b>
				${itemRows ? `<table style="margin-top:8px"><thead><tr><th>${__('Item')}</th><th class="num">${__('In stock')}</th><th class="num">${__('Not in a location')}</th><th></th></tr></thead><tbody>${itemRows}</tbody></table>` : none(__('Every item is in a location.'))}</div>`);
		this.$tab.find('.place-gun').on('click', (ev) => this.placeDialog({ serial: $(ev.currentTarget).attr('data-serial') }));
		this.$tab.find('.place-item').on('click', (ev) => {
			const $b = $(ev.currentTarget);
			this.placeDialog({ item: $b.attr('data-item'), qty: Number($b.attr('data-qty')) });
		});
		this.$tab.find('.confirm-item').on('click', (ev) => {
			const $b = $(ev.currentTarget);
			this.confirmDialog($b.attr('data-item'), Number($b.attr('data-qty')));
		});
	}

	locationField(label, onlyFree) {
		return {
			fieldname: 'location', fieldtype: 'Link', options: 'Storage Location', label, reqd: 1,
			get_query: () => ({ query: `${STG_API}.location_query`, filters: onlyFree ? { only_free: 1 } : {} }),
		};
	}

	placeDialog(what) {
		const isGun = !!what.serial;
		const d = new frappe.ui.Dialog({
			title: isGun ? __('Place {0}', [this.e(what.serial)]) : __('Place {0}', [this.e(what.item)]),
			fields: [this.locationField(__('Location'), isGun)].concat(isGun ? [] : [
				{ fieldname: 'qty', fieldtype: 'Float', label: __('Quantity'), default: what.qty, reqd: 1 }]),
			primary_action_label: __('Place'),
			primary_action: (v) => {
				this.api('scan_move', { to_location: v.location, code: what.serial || what.item, qty: v.qty || 1 })
					.then((r) => {
						if (r.result === 'error') return frappe.msgprint({ message: r.message, indicator: 'red' });
						if (r.result === 'choose') return frappe.msgprint(r.message);
						d.hide();
						frappe.show_alert({ message: r.message, indicator: 'green' }, 4);
						this.reload().then(() => this.renderUnplaced());
					});
			},
		});
		d.show();
	}

	confirmDialog(item, over) {
		const d = new frappe.ui.Dialog({
			title: __('Where were these taken from?'),
			fields: [
				this.locationField(__('Location they were taken from'), false),
				{ fieldname: 'qty', fieldtype: 'Float', label: __('Units'), default: over, reqd: 1 },
			],
			primary_action_label: __('Confirm'),
			primary_action: (v) => {
				this.api('confirm_taken', { item_code: item, location: v.location, qty: v.qty }).then(() => {
					d.hide();
					this.reload().then(() => this.renderUnplaced());
				});
			},
		});
		d.show();
	}

	// ------------------------------------------------------------- zones

	renderZones() {
		this.page.set_primary_action(__('New Zone'), () => this.newZoneDialog(), 'add');
		if (!this.map.zones.length) return this.$tab.html(this.noZones());
		const rows = this.map.zones.map((z) => {
			const sum = stgZoneSummary(z);
			return `<tr class="${z.disabled ? 'muted' : ''}"><td>${this.e(z.zone_name)}</td>
				<td>${z.kind === 'Slots' ? __('Slots (one firearm each)') : __('Open (one location, holds anything)')}</td>
				<td class="num">${z.positions}</td><td>${this.e(sum.text)}</td>
				<td class="num">${z.kind === 'Slots' ? `<a class="link-btn zone-layout" data-zone="${this.e(z.name)}">${__('Sides & numbering')}</a> · <a class="link-btn add-pos" data-zone="${this.e(z.name)}">${__('Add slots')}</a> · ` : ''}${z.kind === 'Slots' && z.positions > 1 ? `<a class="link-btn remove-pos" data-zone="${this.e(z.name)}">${__('Remove slots')}</a> · ` : ''}<a class="link-btn print-zone" data-zone="${this.e(z.name)}">${__('Print labels')}</a>
					· <a class="link-btn toggle-zone" data-zone="${this.e(z.name)}" data-disabled="${z.disabled ? 1 : 0}">${z.disabled ? __('Enable') : __('Disable')}</a>
					· <a class="link-btn delete-zone text-danger" data-zone="${this.e(z.name)}">${__('Delete')}</a></td></tr>`;
		}).join('');
		this.$tab.html(`<div class="card-box"><table class="zones"><thead><tr><th>${__('Zone')}</th><th>${__('Kind')}</th>
			<th class="num">${__('Positions')}</th><th>${__('In use')}</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`);
		this.$tab.find('.zone-layout').on('click', (ev) => this.layoutDialog($(ev.currentTarget).attr('data-zone')));
		this.$tab.find('.add-pos').on('click', (ev) => this.addPositionsDialog($(ev.currentTarget).attr('data-zone')));
		this.$tab.find('.remove-pos').on('click', (ev) => this.removePositionsDialog($(ev.currentTarget).attr('data-zone')));
		this.$tab.find('.delete-zone').on('click', (ev) => {
			const zone = this.map.zones.find((z) => z.name === $(ev.currentTarget).attr('data-zone'));
			frappe.confirm(__('Delete {0} and its {1} position(s)? It must be empty.', [this.e(zone.zone_name), zone.positions]), () =>
				this.api('delete_zone', { zone: zone.name, company: this.map.company }).then(() => {
					frappe.show_alert({ message: __('{0} deleted.', [zone.zone_name]), indicator: 'green' }, 5);
					this.reload();
				}));
		});
		this.$tab.find('.print-zone').on('click', (ev) => this.printLabels($(ev.currentTarget).attr('data-zone')));
		this.$tab.find('.toggle-zone').on('click', (ev) => {
			const $a = $(ev.currentTarget);
			this.api('set_disabled', { zone: $a.attr('data-zone'), disabled: Number($a.attr('data-disabled')) ? 0 : 1 })
				.then(() => this.reload());
		});
	}

	newZoneDialog() {
		const d = new frappe.ui.Dialog({
			title: __('New Zone'),
			fields: [
				{ fieldname: 'zone_name', fieldtype: 'Data', label: __('Zone name'), reqd: 1, change: () => paint() },
				{ fieldtype: 'Column Break' },
				{ fieldname: 'kind', fieldtype: 'Select', label: __('Kind'), reqd: 1, default: 'Slots',
					options: 'Slots\nOpen', change: () => paint(),
					description: __('Slots hold one firearm each; Open holds anything.') },
				{ fieldtype: 'Section Break' },
				{ fieldname: 'count', fieldtype: 'Int', label: __('How many slots'), default: 10,
					depends_on: "eval:doc.kind=='Slots'", mandatory_depends_on: "eval:doc.kind=='Slots'",
					change: () => paint() },
				...this.layoutFields(() => paint(), "doc.kind=='Slots'"),
				{ fieldname: 'preview', fieldtype: 'HTML' },
			],
			primary_action_label: __('Create'),
			primary_action: (v) => {
				const problem = stgValidateZone(v);
				if (problem) return frappe.msgprint({ message: __(problem), indicator: 'red' });
				const slots = v.kind === 'Slots';
				this.api('create_zone', { zone_name: v.zone_name.trim(), kind: v.kind, count: slots ? v.count : 1,
					sides: slots ? v.sides || 0 : 0, numbering: slots ? v.numbering : undefined }).then((r) => {
					d.hide();
					frappe.show_alert({ message: v.kind === 'Open'
						? __('{0} created.', [v.zone_name])
						: __('Zone {0} created with {1} slots.', [v.zone_name, r.positions]), indicator: 'green' }, 5);
					this.reload();
				});
			},
		});
		const paint = () => {
			const v = d.get_values(true) || {};
			d.fields_dict.preview.$wrapper.html(
				`<div class="muted">${this.e(stgZonePreviewText(v.zone_name, v.kind, v.count, v.sides, v.numbering))}</div>`);
		};
		d.show();
		paint();
	}

	// Sides and Numbering, as New Zone and Sides & numbering ask for them.
	// The live preview under these shows what both do, so they carry no help text.
	// No Section/Column Breaks here: a dialog never re-checks whether a section
	// whose fields depends_on hid at first render is empty, so it would stay hidden.
	layoutFields(paint, when, z) {
		return [
			{ fieldname: 'sides', fieldtype: 'Int', label: __('Sides'), default: z && z.slot_lines ? z.slot_lines : undefined,
				depends_on: `eval:${when}`, change: () => paint() },
			{ fieldname: 'numbering', fieldtype: 'Select', label: __('Numbering on two sides'),
				default: z && z.numbering === STG_IN_ORDER ? STG_IN_ORDER : 'Odd / even',
				options: `Odd / even\n${STG_IN_ORDER}`, depends_on: `eval:${when} && (!doc.sides || doc.sides == 2)`,
				change: () => paint() },
		];
	}

	// Change a Slots zone's sides and numbering after it was made; nothing in it moves.
	layoutDialog(zoneName) {
		const z = this.map.zones.find((x) => x.name === zoneName);
		const plan = this.map.plans.find((p) => p.layout.shapes.some((s) => s.zone === z.name && s.rows));
		const rows = plan && plan.layout.shapes.find((s) => s.zone === z.name).rows;
		const d = new frappe.ui.Dialog({
			title: __('{0}: sides and numbering', [z.zone_name]),
			fields: [...this.layoutFields(() => paint(), 'true', z), { fieldname: 'preview', fieldtype: 'HTML' },
				...(plan ? [{ fieldname: 'override', fieldtype: 'HTML', options: `<div class="text-warning">${__(
					'On the {0} plan this zone is drawn with {1} lines of slots (set in Edit layout), and that wins over Sides. Clear Lines of slots there to use these.',
					[this.e(plan.plan_name), rows])}</div>` }] : [])],
			primary_action_label: __('Save'),
			primary_action: (v) => {
				const problem = stgValidateSides(v.sides);
				if (problem) return frappe.msgprint({ message: __(problem), indicator: 'red' });
				this.api('set_zone_layout', { zone: z.name, sides: v.sides || 0, numbering: v.numbering }).then(() => {
					d.hide();
					frappe.show_alert({ message: __('{0} updated.', [z.zone_name]), indicator: 'green' }, 5);
					this.reload();
				});
			},
		});
		const paint = () => {
			const v = d.get_values(true) || {};
			d.fields_dict.preview.$wrapper.html(
				`<div class="muted">${this.e(stgZonePreviewText(z.zone_name, 'Slots', z.positions, v.sides, v.numbering))}</div>`);
		};
		d.show();
		paint();
	}

	addPositionsDialog(zone) {
		const d = new frappe.ui.Dialog({
			title: __('Add slots'),
			fields: [{ fieldname: 'count', fieldtype: 'Int', label: __('How many more slots'), reqd: 1, default: 5 }],
			primary_action_label: __('Add'),
			primary_action: (v) => {
				this.api('add_positions', { zone, count: v.count }).then((r) => {
					d.hide();
					frappe.show_alert({ message: __('Added {0}. The zone now has {1} slots.', [r.added, r.positions]), indicator: 'green' }, 5);
					this.reload();
				});
			},
		});
		d.show();
	}

	removePositionsDialog(zoneName) {
		const zone = this.map.zones.find((z) => z.name === zoneName);
		const max = zone.positions - 1;
		const d = new frappe.ui.Dialog({
			title: __('Remove slots from {0}', [this.e(zone.zone_name)]),
			fields: [
				{ fieldname: 'count', fieldtype: 'Int', label: __('How many to remove from the end'), reqd: 1, default: 1,
					change: () => paint() },
				{ fieldname: 'preview', fieldtype: 'HTML' },
			],
			primary_action_label: __('Remove'),
			primary_action: (v) => {
				this.api('remove_positions', { zone: zoneName, count: v.count }).then((r) => {
					d.hide();
					frappe.show_alert({ message: __('Removed {0}. The zone now has {1} slots.', [r.removed, r.positions]), indicator: 'green' }, 5);
					this.reload();
				});
			},
		});
		const paint = () => {
			const n = Number(d.get_value('count')) || 0;
			const text = n >= 1 && n <= max
				? __('Deletes {0}{1}–{0}{2}. {0}1–{0}{3} stay.', [zone.zone_name, zone.positions - n + 1, zone.positions, zone.positions - n])
				: __('Pick 1 to {0}. To remove every slot, delete the zone.', [max]);
			d.fields_dict.preview.$wrapper.html(`<div class="muted">${this.e(text)}</div>`);
		};
		d.show();
		paint();
	}

	// One label per position: its name big, with a barcode of the code the scanner reads.
	printLabels(zoneName) {
		const zone = this.map.zones.find((z) => z.name === zoneName);
		if (!zone) return;
		const control = frappe.ui.form.make_control({
			df: { fieldtype: 'Barcode', fieldname: 'stg_label', options: JSON.stringify({ format: 'CODE128', height: 40, width: 2, fontSize: 12 }) },
			parent: $('<div style="display:none"></div>').appendTo(document.body), render_input: true,
		});
		const labels = zone.locations.filter((l) => !l.disabled).map((l) => {
			const svg = control.get_barcode_html(l.name) || '';
			return `<div class="lab"><div class="big">${this.e(l.label)}</div>${svg}<div class="code">${this.e(l.name)}</div></div>`;
		}).join('');
		const w = window.open('', '_blank');
		if (!w) return frappe.msgprint(__('Allow pop-ups to print the labels.'));
		w.document.write(`<!doctype html><title>${this.e(zone.zone_name)} labels</title><style>
			body{font-family:sans-serif;margin:8mm} .grid{display:flex;flex-wrap:wrap;gap:4mm}
			.lab{width:60mm;border:1px dashed #999;padding:3mm;text-align:center;page-break-inside:avoid}
			.big{font-size:22pt;font-weight:700} .code{font-size:8pt;color:#555} svg{width:100%;height:auto}
			</style><div class="grid">${labels}</div><script>window.onload=function(){window.print()}<\/script>`);
		w.document.close();
	}
}

// Node loads this file to test the kernel above. The page class and the frappe hooks only
// exist in the browser, so nothing below runs there.
if (typeof module !== 'undefined' && module.exports) {
	module.exports = {
		stgNorm,
		stgBuildIndex,
		stgResolveLocation,
		stgCellState,
		stgCellTitle,
		stgZoneSummary,
		stgSearchMap,
		stgScanPlan,
		stgPushSession,
		stgMarkUndone,
		stgCanUndo,
		stgValidateZone,
		stgValidateSides,
		stgPreviewLabels,
		stgZonePreviewText,
		stgFilterRows,
		stgNeedsAttention,
		stgpSnap,
		stgpAngle,
		stgpSlotGrid,
		stgpSlotLine,
		stgpSlotCells,
		stgpCorners,
		stgpBounds,
		stgpInside,
		stgpResize,
		stgpRotateTo,
		stgpMoveTo,
		stgpFit,
		stgpZoomLimits,
		stgpZoomAt,
		stgpPinch,
		stgpPushUndo,
		stgpUnplaced,
		stgpZoneSize,
		stgpAutoPlace,
		stgpNewShape,
		stgpPlanHits,
		stgpTextFrame,
		stgpWrap,
		stgpFitText,
		stgpTextTurn,
		stgpCheckPlan,
	};
}
