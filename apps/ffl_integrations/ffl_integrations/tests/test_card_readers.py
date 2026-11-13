"""Card readers (payments/card_readers.py): a store with several card devices lists them in the
processor's settings (Payroc Settings → Card Readers) and every terminal charge names the reader
of the computer it was started on.

Real schema throughout (POS Invoice charge rows, counter Sales Invoices, Card Terminal Payments,
the settings table); only the Payroc client (the gateway) is a MagicMock, as in the other card
suites. With no readers listed nothing changes: the request goes to the one configured device
and the records name no reader (the existing card suites pin the rest of that behaviour).
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import frappe

from ffl_integrations import integration_gate
from ffl_integrations.payments import api, card_readers
from ffl_integrations.payments.card_readers import CardReaderRequired, CardReaderUnavailable
from ffl_integrations.payroc.adapter import PayrocProcessor
from ffl_integrations.payroc.client import PayrocClient, PayrocConfig
from ffl_integrations.tests.site_fixtures import payroc_configured
from ffl_integrations.tests.test_card_on_file import PAYMENT_HREF, _CardOnFileBase, _payment
from ffl_integrations.tests.test_payroc import _mock_response

SETTINGS = "Payroc Settings"
FRONT, RANGE, OLD = ("Front counter", "PAX-FRONT-01"), ("Range desk", "PAX-RANGE-02"), ("Back room", "PAX-OLD-03")

_PAYROC_CONFIGURED = payroc_configured()


def setUpModule():
	_PAYROC_CONFIGURED.start()


def tearDownModule():
	_PAYROC_CONFIGURED.stop()


def _list_readers(*rows) -> None:
	"""Payroc Settings → Card Readers holds exactly ``rows`` ((name, serial[, enabled]))."""
	frappe.db.delete(card_readers.DOCTYPE, {"parenttype": SETTINGS})
	for i, row in enumerate(rows, start=1):
		name, serial = row[0], row[1]
		doc = frappe.get_doc({"doctype": card_readers.DOCTYPE, "parent": SETTINGS, "parenttype": SETTINGS,
			"parentfield": card_readers.TABLE_FIELD, "idx": i, "reader_name": name,
			"serial_number": serial, "enabled": row[2] if len(row) > 2 else 1})
		doc.name = frappe.generate_hash(length=10)
		doc.db_insert()
	# Rows written under the Single's feet: drop its cached copy, as saving the settings does.
	frappe.clear_document_cache(SETTINGS, SETTINGS)


class _Readers(_CardOnFileBase):
	def setUp(self):
		super().setUp()
		self.addCleanup(_list_readers)        # no readers left behind for the other suites

	@staticmethod
	def _start(client, iid):
		client.create_payment_instruction.reset_mock()
		client.create_payment_instruction.return_value = {"paymentInstructionId": iid, "status": "IN_PROGRESS"}

	@staticmethod
	def _serial_sent(client):
		return client.create_payment_instruction.call_args.kwargs.get("serial_number")


@patch("ffl_integrations.payments.api._integration_ready", return_value=True)
@patch("ffl_integrations.payments.api._check_perm")
@patch("ffl_integrations.payroc.adapter.PayrocClient")
class ChargesNameTheirReader(_Readers):
	def test_no_readers_listed_charges_as_before(self, cc, _p, _r):
		client = cc.from_settings.return_value
		inv = self._pos_invoice(self._customer())
		self._start(client, "PI-NONE")
		api.charge_pos_invoice(inv)
		# No serial argument at all: the client's configured device (Payroc Settings).
		self.assertNotIn("serial_number", client.create_payment_instruction.call_args.kwargs)
		self.assertIsNone(frappe.db.get_value("Card Charge", {"parent": inv}, "card_reader"))

	def test_a_named_reader_charges_on_its_device_and_is_recorded(self, cc, _p, _r):
		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE)
		inv = self._pos_invoice(self._customer())
		self._start(client, "PI-RANGE")
		api.charge_pos_invoice(inv, reader="Range desk")
		self.assertEqual(self._serial_sent(client), "PAX-RANGE-02")
		self.assertEqual(frappe.db.get_value("Card Charge", {"parent": inv},
			["card_reader", "device_serial_number"]), ("Range desk", "PAX-RANGE-02"))

	def test_the_answer_names_the_reader_the_charge_is_on(self, cc, _p, _r):
		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE)
		inv = self._pos_invoice(self._customer())
		self._start(client, "PI-ANS")
		self.assertEqual(api.charge_pos_invoice(inv, reader="Range desk")["reader"], "Range desk")
		si, _owed = self._unpaid_order(self._customer())
		self._start(client, "PI-ANS-SI")
		self.assertEqual(api.charge_sales_invoice(si, reader="Front counter")["reader"], "Front counter")
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": si},
			["card_reader", "device_serial_number"]), ("Front counter", "PAX-FRONT-01"))
		# No readers listed: the answer names none.
		_list_readers()
		inv2 = self._pos_invoice(self._customer())
		self._start(client, "PI-ANS-NONE")
		self.assertIsNone(api.charge_pos_invoice(inv2)["reader"])
		self.assertIsNone(frappe.db.get_value("Card Charge", {"parent": inv2}, "device_serial_number"))

	def test_missing_unknown_or_disabled_reader_is_refused_before_the_gateway(self, cc, _p, _r):
		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE, OLD + (0,))
		inv = self._pos_invoice(self._customer())
		available = "Available card readers: Front counter, Range desk."
		for reader, exc in ((None, CardReaderRequired), ("Back room", CardReaderUnavailable),   # switched off
				("Garage", CardReaderUnavailable)):                                             # never listed
			with self.assertRaises(exc) as ctx:
				api.charge_pos_invoice(inv, reader=reader)
			# The refusal names what a screen or an API client can charge on instead.
			self.assertIn(available, str(ctx.exception))
		si, _owed = self._unpaid_order(self._customer())
		with self.assertRaises(CardReaderRequired):
			api.charge_sales_invoice(si)
		client.create_payment_instruction.assert_not_called()
		self.assertFalse(api._charge_rows(inv))
		self.assertFalse(api._terminal_payments(si))

	def test_a_reader_named_after_the_table_was_emptied_is_refused(self, cc, _p, _r):
		client = cc.from_settings.return_value
		inv = self._pos_invoice(self._customer())
		with self.assertRaises(CardReaderUnavailable) as ctx:
			api.charge_pos_invoice(inv, reader="Front counter")
		self.assertIn("Charge again without a card reader", str(ctx.exception))
		client.create_payment_instruction.assert_not_called()

	def test_the_old_payroc_paths_pass_the_reader_on(self, cc, _p, _r):
		from ffl_integrations.payroc import api as old

		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE)
		inv = self._pos_invoice(self._customer())
		with self.assertRaises(CardReaderRequired):
			old.charge_pos_invoice(inv)
		self._start(client, "PI-OLD")
		self.assertEqual(old.charge_pos_invoice(inv, reader="Range desk")["reader"], "Range desk")
		self.assertEqual(self._serial_sent(client), "PAX-RANGE-02")
		si, _owed = self._unpaid_order(self._customer())
		self._start(client, "PI-OLD-SI")
		old.charge_sales_invoice(si, reader="Front counter")
		self.assertEqual(self._serial_sent(client), "PAX-FRONT-01")

	def test_each_card_of_a_split_records_its_own_reader(self, cc, _p, _r):
		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE)
		inv = self._pos_invoice(self._customer(), rate=100)
		for ask, reader, iid, ref in ((60, "Front counter", "PI-S1", "REF-S1"), (None, "Range desk", "PI-S2", "REF-S2")):
			self._start(client, iid)
			api.charge_pos_invoice(inv, amount=ask, reader=reader)
			self.assertEqual(self._serial_sent(client), dict((FRONT, RANGE))[reader])
			self._approve(client, _payment(ref, ask or 40, token=None))
			self.assertTrue(api.poll_pos_charge(inv, iid)["ok"])
		rows = frappe.get_all("Card Charge", filters={"parent": inv}, fields=["status", "card_reader"],
			order_by="idx asc")
		self.assertEqual([(r.status, r.card_reader) for r in rows],
			[("CHARGED", "Front counter"), ("CHARGED", "Range desk")])

	def test_counter_invoice_and_saving_the_card_name_the_reader(self, cc, _p, _r):
		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE)
		cust = self._customer()
		si, owed = self._unpaid_order(cust)
		self._start(client, "PI-SI")
		api.charge_sales_invoice(si, save_card=1, reader="Front counter")
		kw = client.create_payment_instruction.call_args.kwargs
		self.assertEqual(kw["serial_number"], "PAX-FRONT-01")
		self.assertTrue(kw["credential_on_file"]["tokenize"])        # saving on the terminal, same reader
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": si}, "card_reader"),
			"Front counter")
		self._approve(client, _payment("REF-SI", owed, reference=kw["credential_on_file"]["merchantReference"]))
		res = api.poll_sales_invoice_charge(si, "PI-SI")
		self.assertTrue(res["ok"] and res["paid"] and res["card_saved"])

	def test_a_keyed_charge_needs_no_reader(self, cc, _p, _r):
		_list_readers(FRONT, RANGE)
		self.assertIsNone(card_readers.resolve(PayrocProcessor(), None, keyed=True))


@patch("ffl_integrations.payments.api._integration_ready", return_value=True)
@patch("ffl_integrations.payments.api._check_perm")
@patch("ffl_integrations.payroc.adapter.PayrocClient")
class FollowUpsStayWithTheChargesReader(_Readers):
	"""Poll, cancel and resume go by the charge's own handle and record — never by the reader the
	browser has chosen since."""

	def test_resume_poll_and_cancel_use_the_recorded_reader(self, cc, _p, _r):
		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE)
		inv = self._pos_invoice(self._customer())
		self._start(client, "PI-F1")
		api.charge_pos_invoice(inv, reader="Front counter")

		# This computer switches to the range desk while the card is still on the front counter's
		# device: charging again hands back the same charge, no second instruction anywhere.
		client.create_payment_instruction.reset_mock()
		client.poll_payment_instruction.return_value = {"status": "IN_PROGRESS"}
		again = api.charge_pos_invoice(inv, reader="Range desk")
		self.assertEqual((again["instruction_id"], again["status"]), ("PI-F1", "IN_PROGRESS"))
		# ... and says where that charge is, so the waiting screen shows the right reader.
		self.assertEqual(again["reader"], "Front counter")
		client.create_payment_instruction.assert_not_called()

		# Cancel goes to that charge (no reader in the call at all) and the record keeps its reader.
		client.cancel_payment_instruction.return_value = {"status": "CANCELED"}
		self.assertEqual(api.cancel_pos_charge(inv, "PI-F1")["status"], "CANCELED")
		client.cancel_payment_instruction.assert_called_once_with("PI-F1")
		row = frappe.get_all("Card Charge", filters={"parent": inv}, fields=["status", "card_reader"])
		self.assertEqual([(r.status, r.card_reader) for r in row], [("CANCELED", "Front counter")])

		# The next card goes to the reader chosen now.
		self._start(client, "PI-F2")
		api.charge_pos_invoice(inv, reader="Range desk")
		self.assertEqual(self._serial_sent(client), "PAX-RANGE-02")
		self._approve(client, _payment("REF-F2", 50, token=None))
		self.assertTrue(api.poll_pos_charge(inv, "PI-F2")["ok"])
		rows = frappe.get_all("Card Charge", filters={"parent": inv}, fields=["status", "card_reader"],
			order_by="idx asc")
		self.assertEqual([(r.status, r.card_reader) for r in rows],
			[("CANCELED", "Front counter"), ("CHARGED", "Range desk")])

	def test_a_counter_charge_resumed_from_another_computer_names_its_own_reader(self, cc, _p, _r):
		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE)
		si, _owed = self._unpaid_order(self._customer())
		self._start(client, "PI-SI-F")
		api.charge_sales_invoice(si, reader="Range desk")
		client.create_payment_instruction.reset_mock()
		client.poll_payment_instruction.return_value = {"status": "IN_PROGRESS"}
		again = api.charge_sales_invoice(si, reader="Front counter")
		self.assertEqual((again["instruction_id"], again["reader"]), ("PI-SI-F", "Range desk"))
		client.create_payment_instruction.assert_not_called()

	def test_a_switched_off_readers_charge_in_flight_still_settles(self, cc, _p, _r):
		client = cc.from_settings.return_value
		_list_readers(FRONT, RANGE)
		si, owed = self._unpaid_order(self._customer())
		self._start(client, "PI-OFF")
		api.charge_sales_invoice(si, reader="Range desk")
		_list_readers(FRONT, RANGE + (0,))                           # a manager switches it off
		self._approve(client, _payment("REF-OFF", owed, token=None))
		res = api.poll_sales_invoice_charge(si, "PI-OFF")
		self.assertTrue(res["ok"] and res["paid"])
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": si},
			["status", "card_reader"]), ("CHARGED", "Range desk"))


class ReadersSetUp(_Readers):
	def test_ready_needs_an_enabled_reader_once_readers_are_listed(self):
		cfg = MagicMock(default_serial_number=None)
		with _PAYROC_CONFIGURED_OFF(), patch("ffl_integrations.integration_gate.payroc_enabled", return_value=True), \
				patch("ffl_integrations.payroc.client.PayrocConfig.from_settings", return_value=cfg):
			self.assertFalse(integration_gate.payroc_ready(require_device=True))      # nothing at all
			cfg.default_serial_number = "SIM-0001"
			self.assertTrue(integration_gate.payroc_ready(require_device=True))       # as before
			_list_readers(FRONT + (0,), RANGE + (0,))
			self.assertFalse(integration_gate.payroc_ready(require_device=True))      # all switched off
			self.assertTrue(integration_gate.payroc_ready(require_device=False))
			_list_readers(FRONT + (0,), RANGE)
			cfg.default_serial_number = None
			self.assertTrue(integration_gate.payroc_ready(require_device=True))

	def test_the_counter_gets_the_enabled_names_only(self):
		p = PayrocProcessor()
		self.assertEqual(card_readers.boot_names(p), [])
		self.assertEqual(card_readers.boot_names(None), [])
		_list_readers(FRONT, OLD + (0,), RANGE)
		self.assertEqual(card_readers.boot_names(p), ["Front counter", "Range desk"])

	def test_readers_come_from_the_cached_settings_and_saving_refreshes_them(self):
		_list_readers(FRONT, RANGE)
		self.assertEqual(card_readers.boot_names(PayrocProcessor()), ["Front counter", "Range desk"])
		with patch.object(card_readers.frappe, "get_all") as get_all:
			card_readers.readers(SETTINGS)
			get_all.assert_not_called()
		doc = frappe.get_doc(SETTINGS)
		doc.card_readers[1].enabled = 0
		doc.flags.ignore_permissions = True
		doc.save()
		self.assertEqual(card_readers.boot_names(PayrocProcessor()), ["Front counter"])

	def test_card_tender_mode_lists_the_readers_switched_on_now(self):
		with patch("ffl_integrations.payments.api._check_perm"), \
				patch("ffl_integrations.payments.api._integration_ready", return_value=True), \
				patch("ffl_integrations.payments.registry.active_processor", return_value=PayrocProcessor()):
			self.assertEqual(api.card_tender_mode(), {"integrated": True, "readers": []})
			_list_readers(FRONT, OLD + (0,), RANGE)
			self.assertEqual(api.card_tender_mode()["readers"], ["Front counter", "Range desk"])

	def test_names_and_serial_numbers_are_used_once(self):
		doc = frappe.get_doc(SETTINGS)
		doc.set("card_readers", [{"reader_name": "Front", "serial_number": "A"},
			{"reader_name": " front ", "serial_number": "B"}])
		with self.assertRaises(frappe.ValidationError):
			card_readers.validate_table(doc)
		doc.set("card_readers", [{"reader_name": "Front", "serial_number": "A"},
			{"reader_name": "Range", "serial_number": "A "}])
		with self.assertRaises(frappe.ValidationError):
			card_readers.validate_table(doc)
		doc.set("card_readers", [{"reader_name": "Front", "serial_number": "A"},
			{"reader_name": "Range", "serial_number": "B"}])
		card_readers.validate_table(doc)

	def test_the_screen_forgets_its_reader_on_exactly_these_refusals(self):
		js = os.path.join(frappe.get_app_path("ffl_core"), "public", "js", "card_reader.js")
		with open(js) as f:
			source = f.read()
		for exc in (CardReaderRequired, CardReaderUnavailable):
			self.assertIn(f"'{exc.__name__}'", source)


class _PAYROC_CONFIGURED_OFF:
	"""The real payroc_ready for one block (this module patches it for the charge tests)."""

	def __enter__(self):
		_PAYROC_CONFIGURED.stop()

	def __exit__(self, *exc):
		_PAYROC_CONFIGURED.start()


class AdapterSendsTheDevice(_Readers):
	"""On the wire: the device serial goes in the URL — the chosen reader's, else Settings'."""

	def _sent_url(self, device):
		cfg = PayrocConfig(api_key="k", base_url="https://x/merchant", default_terminal="4480001",
			default_serial_number="SIM-0001", default_currency="USD", integration_id=None, timeout=5)
		session = MagicMock()
		session.get.return_value = _mock_response(200, {"token": "jwt", "expiresIn": 1})
		session.request.return_value = _mock_response(202, {"paymentInstructionId": "PI", "status": "IN_PROGRESS"})
		p = PayrocProcessor()
		p._client = PayrocClient(cfg, session=session)
		with patch("ffl_integrations.payroc.client._bearer") as bearer:
			bearer.is_valid.return_value, bearer.token = True, "jwt"
			p.start_charge(document="POS-1", attempt=1, amount=10, terminal=None, description="d",
				operator="o", **card_readers.start_kwargs({"name": "R", "serial": device} if device else None))
		return session.request.call_args.args[1]

	def test_device_in_the_url(self):
		self.assertTrue(self._sent_url(None).endswith("/devices/SIM-0001/paymentInstructions"))
		self.assertTrue(self._sent_url("PAX-RANGE-02").endswith("/devices/PAX-RANGE-02/paymentInstructions"))
