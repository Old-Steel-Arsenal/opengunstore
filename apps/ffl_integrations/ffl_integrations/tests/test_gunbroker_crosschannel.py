"""The two inverted hooks that let GunBroker and WooCommerce talk without importing.

What is worth testing here is not that the hooks fire — it is what happens when
they DON'T, because both are wired so that a missing or broken registrant is
survivable, and "survivable" is easy to write and easy to get wrong.

`serial_channel_exit` runs inside the transaction that records a GunBroker
order. If a registrant's exception escaped, the order would roll back — and an
order that was never recorded is strictly worse than a listing that was not
taken down, because the sweep and reconcile both fix the second one and nothing
fixes the first.

`channel_presale_probe` runs on the counter's scan path. If it raised, or
blocked on a GunBroker outage, the register would stop taking money.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import crosschannel


class _HookCase(FrappeTestCase):
	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _hooks(self, mapping: dict):
		"""Answer for the hooks under test, pass everything else through.

		Patching `frappe.get_hooks` wholesale would break any code in the call
		path that asks about something unrelated — the rule in docs/testing.md.
		"""
		real = frappe.get_hooks

		def _side_effect(name=None, *args, **kwargs):
			if name in mapping:
				return mapping[name]
			return real(name, *args, **kwargs)

		return self._patch("frappe.get_hooks", side_effect=_side_effect)


class FiringTheExitHook(_HookCase):
	def test_a_release_fires_the_return_hook(self):
		seen = []
		self._hooks({crosschannel.RETURN_HOOK: ["x.y.z"]})
		self._patch("frappe.get_attr",
			return_value=lambda serial, **kw: seen.append((serial, kw)))
		ok, _error = crosschannel.fire_serial_channel_return("SN-1")
		self.assertTrue(ok)
		self.assertEqual(seen, [("SN-1", {"channel": "gunbroker", "reason": "released"})])

	def test_no_registrant_is_a_recorded_skip_not_a_failure(self):
		"""A POS without ffl_woo_sync is a legitimate deployment."""
		self._hooks({crosschannel.EXIT_HOOK: []})
		ok, error = crosschannel.fire_serial_channel_exit("SN-1")
		self.assertFalse(ok)
		self.assertIn("no serial_channel_exit registrant", error)

	def test_every_registrant_gets_the_full_contract(self):
		seen = []
		self._hooks({crosschannel.EXIT_HOOK: ["x.y.z"]})
		self._patch("frappe.get_attr",
			return_value=lambda sn, **kw: seen.append((sn, kw)))
		ok, error = crosschannel.fire_serial_channel_exit(
			"SN-1", reason=crosschannel.REASON_RESERVED)
		self.assertTrue(ok)
		self.assertEqual(error, "")
		self.assertEqual(seen, [("SN-1", {"channel": "gunbroker",
			"reason": crosschannel.REASON_RESERVED})])

	def test_a_raising_registrant_never_reaches_the_caller(self):
		"""The order record matters more than the delist, so the exception stops here.

		Deliberately unlike `ffl_acquisition_sync_push`, where a raising
		registrant DOES roll the caller back — an acquisition missing from the
		bound book is a federal problem and a listing left up is not.
		"""
		def _boom(_sn, **_kw):
			raise RuntimeError("WooCommerce is down")

		self._hooks({crosschannel.EXIT_HOOK: ["x.y.z"]})
		self._patch("frappe.get_attr", return_value=_boom)
		logged = self._patch(
			"ffl_integrations.gunbroker.crosschannel.safe_log_error")
		ok, error = crosschannel.fire_serial_channel_exit("SN-1")
		self.assertFalse(ok)
		self.assertIn("WooCommerce is down", error)
		self.assertTrue(logged.called, "a swallowed failure must still be recorded")

	def test_one_bad_registrant_does_not_stop_the_others(self):
		reached = []

		def _resolve(path):
			if path == "bad.one":
				def _boom(_sn, **_kw):
					raise RuntimeError("nope")
				return _boom
			return lambda sn, **kw: reached.append(sn)

		self._hooks({crosschannel.EXIT_HOOK: ["bad.one", "good.one"]})
		self._patch("frappe.get_attr", side_effect=_resolve)
		self._patch("ffl_integrations.gunbroker.crosschannel.safe_log_error")
		ok, _error = crosschannel.fire_serial_channel_exit("SN-1")
		self.assertFalse(ok)
		self.assertEqual(reached, ["SN-1"])


class ThePresaleProbe(_HookCase):
	def test_a_sold_listing_blocks_with_the_item_number_named(self):
		self._patch("ffl_integrations.gunbroker.doc_events.probe_gb_sale",
			return_value={"state": "sold", "item_id": "16087230"})
		answer = crosschannel.presale_probe("SN-1")
		self.assertTrue(answer["blocked"])
		self.assertEqual(answer["channel"], "gunbroker")
		self.assertIn("16087230", answer["message"])

	def test_anything_short_of_sold_is_silence(self):
		"""clear / ended / unknown / no listing all answer None.

		The scan path shows this as a warning; a warning for "we could not reach
		GunBroker" on every gun scanned is a warning nobody reads.
		"""
		for verdict in ({"state": "clear", "item_id": "1"},
				{"state": "ended", "item_id": "1"},
				{"state": "unknown", "item_id": "1"}, None):
			with self.subTest(verdict=verdict):
				self._patch("ffl_integrations.gunbroker.doc_events.probe_gb_sale",
					return_value=verdict)
				self.assertIsNone(crosschannel.presale_probe("SN-1"))


class TheHooksAreRegisteredBothWays(FrappeTestCase):
	"""The contract holds at the site level, not just in one app's file.

	The no-import test proves neither app reaches into the other. This one proves
	they still reach each OTHER — an inversion that nobody registers for is just
	a function that never runs, and the failure is silent in both directions.
	"""

	def test_woocommerce_answers_serial_channel_exit(self):
		registrants = frappe.get_hooks("serial_channel_exit") or []
		if "ffl_woo_sync" not in frappe.get_installed_apps():
			self.skipTest("ffl_woo_sync is not installed in this bench")
		self.assertIn("ffl_woo_sync.woocommerce.channel.serial_channel_exit",
			registrants)

	def test_woocommerce_answers_serial_channel_return(self):
		registrants = frappe.get_hooks("serial_channel_return") or []
		if "ffl_woo_sync" not in frappe.get_installed_apps():
			self.skipTest("ffl_woo_sync is not installed in this bench")
		self.assertIn("ffl_woo_sync.woocommerce.channel.serial_channel_return",
			registrants)

	def test_gunbroker_answers_channel_presale_probe(self):
		registrants = frappe.get_hooks("channel_presale_probe") or []
		self.assertIn("ffl_integrations.gunbroker.crosschannel.presale_probe",
			registrants)

	def test_both_registered_paths_resolve(self):
		for name in ("serial_channel_exit", "serial_channel_return",
				"channel_presale_probe"):
			for path in (frappe.get_hooks(name) or []):
				with self.subTest(hook=name, path=path):
					self.assertTrue(callable(frappe.get_attr(path)))


class TheWooRegistrantTakesGunsOffTheStore(FrappeTestCase):
	"""The ffl_woo_sync half. Lives here because the contract is what is being
	tested, and reading both halves in one file is how the contract stays one
	thing rather than two."""

	def setUp(self):
		if "ffl_woo_sync" not in frappe.get_installed_apps():
			self.skipTest("ffl_woo_sync is not installed in this bench")
		self.enqueued: list[dict] = []
		patcher = patch("frappe.enqueue",
			side_effect=lambda path, **kw: self.enqueued.append({"path": path, **kw}))
		patcher.start()
		self.addCleanup(patcher.stop)

	def _serial(self, **row):
		base = {"item_code": "COLT", "woo_product_id": ""}
		base.update(row)

		def _get_value(doctype, _name, field=None, **_kw):
			if doctype == "Serial No":
				return frappe._dict(base) if isinstance(field, list) else base.get(field)
			return ""  # Item: not listed unless a test says so

		patcher = patch("frappe.db.get_value", side_effect=_get_value)
		patcher.start()
		self.addCleanup(patcher.stop)
		patcher = patch("frappe.db.set_value")  # the exit's woo_channel_delisted mark
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_listed_serial_is_delisted_on_the_retail_store(self):
		from ffl_woo_sync.woocommerce import channel

		self._serial(woo_product_id="11")
		channel.serial_channel_exit("SN-1", channel="gunbroker", reason="sold")
		sites = sorted(job["site"] for job in self.enqueued)
		self.assertEqual(sites, ["retail"])
		for job in self.enqueued:
			self.assertTrue(job["path"].endswith("delist_serial_task"))
			# After commit, or the delist could run before the reservation it is
			# reacting to is visible.
			self.assertTrue(job["enqueue_after_commit"])
			self.assertTrue(job["deduplicate"])

	def test_a_gun_on_neither_store_costs_nothing(self):
		from ffl_woo_sync.woocommerce import channel

		self._serial()
		channel.serial_channel_exit("SN-1", channel="gunbroker", reason="sold")
		self.assertEqual(self.enqueued, [])

	def test_a_released_serial_is_relisted_on_the_retail_store(self):
		"""The exit drafted the page and set ``woo_delisted`` (marking
		``woo_channel_delisted``); only a relist push lifts that, so the return
		must ask for one explicitly."""
		from ffl_woo_sync.woocommerce import channel

		self._serial(woo_product_id="11", woo_channel_delisted=1)
		channel.serial_channel_return("SN-1", channel="gunbroker", reason="released")
		self.assertEqual(len(self.enqueued), 1)
		job = self.enqueued[0]
		self.assertTrue(job["path"].endswith("push_serial_task"))
		self.assertEqual(job["site"], "retail")
		self.assertTrue(job["relist"])
		self.assertTrue(job["enqueue_after_commit"])
