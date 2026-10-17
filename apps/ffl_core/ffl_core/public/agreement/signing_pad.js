// Signature pad for the agreement signing page (www/agreement/sign.html), plus
// the paired signing tablet's device key (www/agreement/terminal.html).
//
// Made for tablets first: fingers, Apple Pencil (iPad Safari), S Pen / stylus
// (Android Chrome) and a mouse all arrive as Pointer Events. The pad keeps the
// signature as strokes of points relative to its own size (0..1), so a resize or a
// turn of the tablet redraws the same signature at the new size, and the export is
// always the same 1200 x 400 white PNG whatever the screen. Once a pen has touched
// the pad, touches are ignored (a palm resting on the glass draws nothing).
// No dependencies; the pure half (geometry, ink check, drawing, server messages) is
// Node-tested (tests/js/signing_pad.test.js).
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) module.exports = api;
	else root.SigningPad = api;
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var EXPORT = { width: 1200, height: 400 };
	// A signature narrower and shorter than this share of the pad is a dot, not a signature.
	var MIN_SPAN = 0.04;

	function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

	// Pad-relative point from CSS pixels inside a w x h box.
	function normalize(x, y, w, h) {
		return [clamp01(w ? x / w : 0), clamp01(h ? y / h : 0)];
	}

	// Canvas backing store for a CSS size on a screen with this devicePixelRatio.
	function backingSize(cssW, cssH, dpr) {
		var r = dpr > 0 ? dpr : 1;
		return { width: Math.max(1, Math.round(cssW * r)), height: Math.max(1, Math.round(cssH * r)) };
	}

	// Strokes in pixels of a w x h surface.
	function toPixels(strokes, w, h) {
		return strokes.map(function (s) { return s.map(function (p) { return [p[0] * w, p[1] * h]; }); });
	}

	// {minX, minY, maxX, maxY} of every point (pad-relative), or null with no points.
	function bbox(strokes) {
		var b = null;
		strokes.forEach(function (s) {
			s.forEach(function (p) {
				if (!b) b = { minX: p[0], minY: p[1], maxX: p[0], maxY: p[1] };
				else {
					b.minX = Math.min(b.minX, p[0]); b.minY = Math.min(b.minY, p[1]);
					b.maxX = Math.max(b.maxX, p[0]); b.maxY = Math.max(b.maxY, p[1]);
				}
			});
		});
		return b;
	}

	// Enough ink to count as a signature: a stroke with movement, spanning MIN_SPAN.
	function hasInk(strokes) {
		var b = bbox(strokes || []);
		if (!b) return false;
		return Math.max(b.maxX - b.minX, b.maxY - b.minY) >= MIN_SPAN;
	}

	// Pen width for a surface `width` pixels wide (3.6 px on the 1200 px export).
	function lineWidth(width) { return Math.max(2, width * 0.003); }

	// Draw strokes (pad-relative) on a 2D context sized w x h, smoothed through midpoints.
	function draw(ctx, strokes, w, h) {
		ctx.lineCap = 'round';
		ctx.lineJoin = 'round';
		ctx.strokeStyle = '#111';
		ctx.fillStyle = '#111';
		ctx.lineWidth = lineWidth(w);
		toPixels(strokes, w, h).forEach(function (s) {
			if (!s.length) return;
			if (s.length === 1) {
				ctx.beginPath();
				ctx.arc(s[0][0], s[0][1], ctx.lineWidth / 2, 0, Math.PI * 2);
				ctx.fill();
				return;
			}
			ctx.beginPath();
			ctx.moveTo(s[0][0], s[0][1]);
			for (var i = 1; i < s.length - 1; i++) {
				var mx = (s[i][0] + s[i + 1][0]) / 2, my = (s[i][1] + s[i + 1][1]) / 2;
				ctx.quadraticCurveTo(s[i][0], s[i][1], mx, my);
			}
			var last = s[s.length - 1];
			ctx.lineTo(last[0], last[1]);
			ctx.stroke();
		});
	}

	// The message of a frappe error response ({_server_messages} / {exception}), as plain text.
	function serverMessage(body, status) {
		if (status === 429) return 'Too many tries. Please wait a few minutes and try again.';
		var text = '';
		try {
			var msgs = JSON.parse((body && body._server_messages) || '[]');
			text = msgs.map(function (m) {
				try { return JSON.parse(m).message || ''; } catch (e) { return String(m); }
			}).filter(Boolean).join(' ');
		} catch (e) { /* fall through */ }
		if (!text && body && body.exception) text = String(body.exception).replace(/^[\w.]+:\s*/, '');
		text = text.replace(/<[^>]*>/g, '').trim();
		return text || 'Something went wrong. Please try again.';
	}

	// ---- browser ------------------------------------------------------------

	// Make `canvas` a pad. opts.onchange(hasInk) after every stroke and clear.
	function attach(canvas, opts) {
		opts = opts || {};
		var strokes = [];
		var current = null, active = null, rect = null, penSeen = false;
		var ctx = canvas.getContext('2d');
		var cssW = 0, cssH = 0;

		function changed() { if (opts.onchange) opts.onchange(hasInk(strokes)); }

		function resize() {
			var r = canvas.getBoundingClientRect();
			if (!r.width || !r.height) return;
			cssW = r.width; cssH = r.height;
			var size = backingSize(cssW, cssH, window.devicePixelRatio || 1);
			canvas.width = size.width;
			canvas.height = size.height;
			ctx.setTransform(size.width / cssW, 0, 0, size.height / cssH, 0, 0);
			redraw();
		}

		function redraw() {
			ctx.clearRect(0, 0, cssW, cssH);
			draw(ctx, strokes, cssW, cssH);
		}

		function point(e) {
			return normalize(e.clientX - rect.left, e.clientY - rect.top, rect.width, rect.height);
		}

		function down(e) {
			if (e.pointerType === 'pen') penSeen = true;
			else if (penSeen && e.pointerType === 'touch') return;   // palm while writing with a pen
			if (e.button > 0 || active !== null) return;
			e.preventDefault();
			active = e.pointerId;
			rect = canvas.getBoundingClientRect();
			try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* old browsers */ }
			current = [point(e)];
			strokes.push(current);
			redraw();
		}

		function move(e) {
			if (e.pointerId !== active) return;
			e.preventDefault();
			var events = (e.getCoalescedEvents && e.getCoalescedEvents()) || [];
			if (!events.length) events = [e];
			events.forEach(function (ev) { current.push(point(ev)); });
			// Only the new tail: a full redraw per event lags under a 240 Hz Pencil.
			draw(ctx, [current.slice(-(events.length + 2))], cssW, cssH);
		}

		function up(e) {
			if (e.pointerId !== active) return;
			active = null;
			current = null;
			redraw();
			changed();
		}

		canvas.style.touchAction = 'none';
		canvas.addEventListener('pointerdown', down);
		canvas.addEventListener('pointermove', move);
		canvas.addEventListener('pointerup', up);
		canvas.addEventListener('pointercancel', up);
		// iOS Safari: no pinch-zoom, scroll or callout from inside the pad.
		['touchstart', 'touchmove', 'gesturestart', 'gesturechange'].forEach(function (t) {
			canvas.addEventListener(t, function (e) { e.preventDefault(); }, { passive: false });
		});
		if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas);
		window.addEventListener('orientationchange', function () { setTimeout(resize, 250); });
		window.addEventListener('resize', resize);
		resize();

		return {
			clear: function () { strokes = []; current = null; active = null; redraw(); changed(); },
			hasInk: function () { return hasInk(strokes); },
			strokes: function () { return strokes; },
			redraw: resize,
			// PNG data URL at the fixed export size, white background.
			toDataURL: function () {
				var out = document.createElement('canvas');
				out.width = EXPORT.width;
				out.height = EXPORT.height;
				var c = out.getContext('2d');
				c.fillStyle = '#fff';
				c.fillRect(0, 0, out.width, out.height);
				draw(c, strokes, out.width, out.height);
				return out.toDataURL('image/png');
			},
		};
	}

	// The paired tablet's device key (www/agreement/terminal), kept in this browser.
	// `storage` is localStorage in the browser; every access is guarded (a private
	// window or blocked storage just means "not paired").
	var DEVICE_KEY = 'ffl-signing-terminal';
	function device(storage) {
		return {
			get: function () {
				try {
					var v = JSON.parse(storage.getItem(DEVICE_KEY) || 'null');
					return v && typeof v.secret === 'string' ? v : null;
				} catch (e) { return null; }
			},
			set: function (secret, name) {
				try { storage.setItem(DEVICE_KEY, JSON.stringify({ secret: secret, name: name || '' })); return true; } catch (e) { return false; }
			},
			clear: function () { try { storage.removeItem(DEVICE_KEY); } catch (e) { /* nothing to clear */ } },
		};
	}

	return {
		EXPORT: EXPORT, MIN_SPAN: MIN_SPAN, device: device,
		normalize: normalize, backingSize: backingSize, toPixels: toPixels, bbox: bbox, hasInk: hasInk,
		lineWidth: lineWidth, draw: draw, serverMessage: serverMessage, attach: attach,
	};
});
