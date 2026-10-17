"""Waivers and agreements: templates, links, the Guest signing endpoints, paper
copies, minors, status / the counter panel, expiry, merge, and the sale gate.

Site tests against the real doctypes. The Guest endpoints are called directly
with the session switched to Guest (the HTTP layer's whitelist and rate limit are
tested on their own: ``frappe.is_whitelisted`` and a fabricated request).
"""
from __future__ import annotations

import base64
import io
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, now_datetime, nowdate
from PIL import Image, ImageDraw

from ffl_core import agreements
from ffl_core.customer_flags import get_customer_flags
from ffl_core.tests.test_customer_flags import _sales_invoice
from ffl_core.tests.test_customer_profile import _customer, _email, _hooks, _public_file, _uniq, _user

_GATE = "ffl_core.agreements.refuse_missing_required"
_PROVIDED = []   # what _provider answers (the gunshop_required_agreements test hook)


def _provider(doc):
	return list(_PROVIDED)


def _broken_provider(doc):
	raise RuntimeError("provider down")


def _template(**fields):
	doc = frappe.get_doc({"doctype": "Agreement Template", "title": _uniq("Waiver"),
		"body": "<p>I, {{ customer_name }}, accept the range rules at {{ store_name }}.</p>", **fields})
	doc.insert(ignore_permissions=True)
	return doc


def _png(ink=True, size=(1200, 400), fmt="PNG") -> str:
	img = Image.new("RGB", size, "white")
	if ink:
		ImageDraw.Draw(img).line([(100, 300), (400, 120), (800, 280)], fill="black", width=6)
	buf = io.BytesIO()
	img.save(buf, fmt)
	return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _as(user):
	"""Context: run as *user*, then back to Administrator."""
	class _Ctx:
		def __enter__(self):
			frappe.set_user(user)

		def __exit__(self, *exc):
			frappe.set_user("Administrator")
	return _Ctx()


class _Base(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.tpl = _template(valid_days=365)
		self.cust = _customer()

	def request(self, deliver="qr", tpl=None, customer=None, replace=1):
		r = agreements.request_agreement((customer or self.cust).name, (tpl or self.tpl).name, deliver, replace)
		token = r["sign_url"].split("#t=", 1)[1] if r.get("sign_url") else None
		return r, token

	def sign(self, token, **kw):
		args = {"signature": _png(), "agreed": 1, "signer_name": "Jane Roe"}
		args.update(kw)
		with _as("Guest"):
			return agreements.submit_signature(token, **args)

	def assertInvalidLink(self, fn, *args, **kw):
		with self.assertRaises(frappe.ValidationError) as cm:
			fn(*args, **kw)
		self.assertEqual(str(cm.exception), agreements.INVALID_LINK)
		self.assertNotIn(self.cust.customer_name, str(cm.exception))


class Wiring(FrappeTestCase):

	def test_counter_sales_are_gated_on_submit(self):
		events = _hooks()["doc_events"]
		for doctype in ("POS Invoice", "Sales Invoice"):
			self.assertIn(_GATE, events[doctype]["before_submit"], doctype)
			self.assertNotIn(_GATE, str(events[doctype].get("validate")), doctype)

	def test_fields_on_install_and_migrate(self):
		hooks = _hooks()
		for key in ("after_install", "after_migrate"):
			self.assertIn("ffl_core.setup.agreements.ensure", hooks[key], key)
		self.assertTrue(frappe.db.has_column("Item", "ffl_required_agreement"))

	def test_only_the_two_signing_endpoints_are_guest(self):
		staff = (agreements.request_agreement, agreements.void_agreement, agreements.record_paper_agreement,
			agreements.get_customer_agreements, agreements.regenerate_signed_pdf)
		with _as("Guest"):
			for fn in staff:
				with self.assertRaises(frappe.PermissionError, msg=fn.__name__):
					frappe.is_whitelisted(fn)
			for fn in (agreements.load_for_signing, agreements.submit_signature):
				frappe.is_whitelisted(fn)

	def test_email_templates_ship_as_fixtures(self):
		for name in ("Agreement Signing Link", "Agreement Signed Copy"):
			self.assertTrue(frappe.db.exists("Email Template", name), name)


class Templates(_Base):

	def test_version_moves_only_when_a_used_text_changes(self):
		tpl = self.tpl
		tpl.body = "<p>New text, nobody sent it yet.</p>"
		tpl.save()
		self.assertEqual(tpl.version, 1)
		self.request()
		snapshot = frappe.db.get_value("Customer Agreement", {"template": tpl.name}, "body_snapshot")
		tpl.reload()
		tpl.valid_days = 30
		tpl.title = tpl.title
		tpl.save()
		self.assertEqual(tpl.version, 1, "only the text counts")
		tpl.body = "<p>Second text.</p>"
		tpl.save()
		self.assertEqual(tpl.version, 2)
		self.assertEqual(frappe.db.get_value("Customer Agreement", {"template": tpl.name}, "body_snapshot"), snapshot)

	def test_an_open_link_shows_the_current_text_and_a_stale_page_cannot_sign(self):
		r, token = self.request()
		self.tpl.reload()
		self.tpl.body = "<p>Revised text for {{ customer_name }}.</p>"
		self.tpl.save()
		with _as("Guest"):
			# A page opened before the change: signing is refused, the copy moves to the new text.
			with self.assertRaisesRegex(frappe.ValidationError, "text was updated"):
				self.sign(token)
			body = agreements.load_for_signing(token)["body_html"]
		self.assertIn("Revised text", body)
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "template_version"), 2)
		self.sign(token)
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "status"), "Signed")

	def test_text_is_rendered_with_the_whitelisted_context_and_sanitized(self):
		tpl = _template(body="<p>{{ customer_name }} / {{ frappe.session.user }}</p><script>alert(1)</script>")
		cust = _customer()
		frappe.db.set_value("Customer", cust.name, "customer_name", "<b>Bold</b> Roe")
		self.request(tpl=tpl, customer=cust)
		body = frappe.db.get_value("Customer Agreement", {"template": tpl.name}, "body_snapshot")
		self.assertNotIn("<script", body)
		self.assertNotIn("<b>Bold</b>", body)
		self.assertIn("&lt;b&gt;Bold", body)

	def test_the_form_cannot_edit_or_delete_a_copy(self):
		r, _ = self.request()
		doc = frappe.get_doc("Customer Agreement", r["name"])
		doc.status = "Signed"
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Customer Agreement", r["name"], ignore_permissions=True)


class Requesting(_Base):

	def test_qr_returns_a_link_and_stores_only_its_hash(self):
		r, token = self.request()
		self.assertEqual(len(token), 43)
		self.assertTrue(r["qr_svg"].startswith("data:image/svg+xml;base64,"))
		doc = frappe.get_doc("Customer Agreement", r["name"])
		self.assertEqual((doc.status, doc.delivery_channel, doc.template_version), ("Pending", "QR", 1))
		self.assertEqual(doc.sign_token_hash, agreements._hash(token))
		self.assertNotIn(token, frappe.as_json(doc.as_dict()))
		self.assertGreater(doc.token_expires, now_datetime() + timedelta(hours=71))

	def test_an_open_link_is_not_replaced_unless_asked(self):
		r1, old = self.request()
		r2, none = self.request(replace=0)
		self.assertEqual((r2["name"], r2.get("link_open"), none), (r1["name"], True, None))
		with _as("Guest"):
			self.assertEqual(agreements.load_for_signing(old)["title"], self.tpl.name)
		# An expired link is replaced without asking.
		frappe.db.set_value("Customer Agreement", r1["name"], "token_expires", now_datetime() - timedelta(minutes=1))
		r3, new = self.request(replace=0)
		self.assertFalse(r3.get("link_open"))
		self.assertTrue(new)

	def test_requesting_again_reuses_the_pending_copy_and_kills_the_old_link(self):
		r1, old = self.request()
		r2, new = self.request()
		self.assertEqual(r1["name"], r2["name"])
		self.assertEqual(frappe.db.count("Customer Agreement", {"customer": self.cust.name}), 1)
		self.assertInvalidLink(agreements.load_for_signing, old)
		with _as("Guest"):
			self.assertEqual(agreements.load_for_signing(new)["title"], self.tpl.name)

	def test_email_needs_an_address_and_sends_the_link(self):
		with self.assertRaises(frappe.ValidationError):
			self.request("email")
		self.assertFalse(frappe.db.exists("Customer Agreement", {"customer": self.cust.name}))
		email = _email()
		frappe.db.set_value("Customer", self.cust.name, "email_id", email)
		with patch("frappe.sendmail") as send:
			r, _ = self.request("email")
		self.assertEqual(r["email"], email)
		self.assertNotIn("sign_url", r)
		kwargs = send.call_args.kwargs
		self.assertEqual(kwargs["recipients"], [email])
		self.assertIn("/agreement/sign#t=", kwargs["message"])
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "delivery_channel"), "Email")

	def test_switched_off_template_cannot_be_sent(self):
		frappe.db.set_value("Agreement Template", self.tpl.name, "active", 0)
		with self.assertRaises(frappe.ValidationError):
			self.request()

	def test_clerk_requests_but_cannot_void_or_record_paper(self):
		clerk = _user(_email(), "Sales User")
		with _as(clerk):
			r, _ = self.request()
			with self.assertRaises(frappe.PermissionError):
				agreements.void_agreement(r["name"], "oops")
			with self.assertRaises(frappe.PermissionError):
				agreements.record_paper_agreement(r["name"], "/files/x.pdf", "Jane", nowdate())
		with _as(_user(_email(), "Stock User")):
			with self.assertRaises(frappe.PermissionError):
				self.request()

	def test_void_kills_the_link(self):
		r, token = self.request()
		with self.assertRaises(frappe.ValidationError):
			agreements.void_agreement(r["name"], " ")
		agreements.void_agreement(r["name"], "Wrong customer")
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "status"), "Void")
		self.assertInvalidLink(self.sign, token)


class Signing(_Base):

	def test_load_returns_only_what_the_page_shows(self):
		_, token = self.request()
		with _as("Guest"):
			out = agreements.load_for_signing(token)
		self.assertEqual(set(out), {"customer_name", "title", "body_html", "store_name",
			"minors_need_guardian", "minor_by_dob"})
		self.assertEqual(out["customer_name"], self.cust.customer_name)
		self.assertIn("accept the range rules", out["body_html"])

	def test_bad_tokens_all_get_the_same_answer(self):
		r, token = self.request()
		for bad in ("", "x" * 43, "short", None, token + "a", token[:-1] + ("A" if token[-1] != "A" else "B")):
			with _as("Guest"):
				self.assertInvalidLink(agreements.load_for_signing, bad)
		frappe.db.set_value("Customer Agreement", r["name"], "token_expires", now_datetime() - timedelta(minutes=1))
		with _as("Guest"):
			self.assertInvalidLink(agreements.load_for_signing, token)

	def test_qr_signature_is_tablet_private_and_the_link_is_used_up(self):
		r, token = self.request()
		self.assertTrue(self.sign(token)["signed"])
		doc = frappe.get_doc("Customer Agreement", r["name"])
		self.assertEqual((doc.status, doc.signed_via, doc.signer_name), ("Signed", "Tablet", "Jane Roe"))
		self.assertIsNone(doc.sign_token_hash)
		self.assertEqual(getdate(doc.expires_on), getdate(add_days(nowdate(), 365)))
		self.assertTrue(doc.signature.startswith("/private/files/"))
		self.assertTrue(frappe.db.get_value("File", {"file_url": doc.signature}, "is_private"))
		with _as("Guest"):
			self.assertInvalidLink(self.sign, token)

	def test_emailed_link_signed_by_a_guest_is_remote(self):
		frappe.db.set_value("Customer", self.cust.name, "email_id", _email())
		with patch("frappe.sendmail"):
			r = agreements.request_agreement(self.cust.name, self.tpl.name, "email")
		doc = frappe.get_doc("Customer Agreement", r["name"])
		token = "t" * 43
		frappe.db.set_value("Customer Agreement", doc.name, "sign_token_hash", agreements._hash(token))
		self.sign(token)
		self.assertEqual(frappe.db.get_value("Customer Agreement", doc.name, "signed_via"), "Remote")

	def test_signature_checks(self):
		_, token = self.request()
		cases = {
			"not agreed": dict(agreed=0),
			"no name": dict(signer_name="  "),
			"blank canvas": dict(signature=_png(ink=False)),
			"jpeg": dict(signature=_png(fmt="JPEG")),
			"png head, junk tail": dict(signature="data:image/png;base64," + base64.b64encode(
				b"\x89PNG\r\n\x1a\n" + b"junk" * 100).decode()),
			"not base64": dict(signature="data:image/png;base64,@@@"),
			"too wide": dict(signature=_png(size=(2400, 400))),
			"too long": dict(signature="A" * (agreements.MAX_SIGNATURE_CHARS + 1)),
		}
		for why, kw in cases.items():
			with self.assertRaises(frappe.ValidationError, msg=why):
				self.sign(token, **kw)
		# Still Pending, still usable.
		self.assertTrue(self.sign(token)["signed"])

	def test_too_many_bytes_refused_before_decoding_the_image(self):
		_, token = self.request()
		big = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (600 * 1024)).decode()
		with self.assertRaises(frappe.ValidationError) as cm:
			self.sign(token, signature=big)
		self.assertIn("too large", str(cm.exception))

	def test_pdf_and_email_after_signing(self):
		email = _email()
		frappe.db.set_value("Customer", self.cust.name, "email_id", email)
		r, token = self.request()
		self.sign(token)
		with patch("frappe.sendmail") as send:
			agreements.finish_signed_agreement(r["name"])
		doc = frappe.get_doc("Customer Agreement", r["name"])
		self.assertTrue(doc.signed_pdf.startswith("/private/files/"))
		pdf = frappe.get_doc("File", {"file_url": doc.signed_pdf}).get_content()
		from pypdf import PdfReader
		# Split and re-join: Ubuntu's wkhtmltopdf (GitHub-hosted CI) extracts word gaps as tabs.
		text = " ".join(" ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages).split())
		self.assertIn(self.cust.customer_name, text)
		self.assertIn("Jane Roe", text)
		kwargs = send.call_args.kwargs
		self.assertEqual(kwargs["recipients"], [email])
		self.assertEqual(kwargs["attachments"][0]["fname"], f"{doc.name}.pdf")

	def test_no_email_address_no_mail(self):
		r, token = self.request()
		self.sign(token)
		with patch("frappe.sendmail") as send, patch("ffl_core.agreements._attach_pdf", return_value=b"%PDF-1.4"):
			agreements.finish_signed_agreement(r["name"])
		send.assert_not_called()

	def test_a_failed_email_keeps_the_pdf(self):
		frappe.db.set_value("Customer", self.cust.name, "email_id", _email())
		r, token = self.request()
		self.sign(token)
		with patch("frappe.sendmail", side_effect=frappe.OutgoingEmailError("no account")), \
				patch("frappe.log_error") as log:
			agreements.finish_signed_agreement(r["name"])
		self.assertTrue(frappe.db.get_value("Customer Agreement", r["name"], "signed_pdf"))
		self.assertIn("email failed", log.call_args.kwargs["title"])

	def test_guest_cannot_read_the_copy_or_its_files(self):
		r, token = self.request()
		self.sign(token)
		doc = frappe.get_doc("Customer Agreement", r["name"])
		f = frappe.get_doc("File", {"file_url": doc.signature})
		self.assertFalse(frappe.has_permission("File", "read", doc=f, user="Guest"))
		with _as("Guest"):
			with self.assertRaises(frappe.PermissionError):
				frappe.client.get("Customer Agreement", r["name"])

	def test_rate_limit_counts_per_link_not_per_shop(self):
		"""30 loads of one link from one IP; another customer's link from the same
		IP (the shop's Wi-Fi) still opens."""
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		_, token = self.request()
		_, other = self.request(customer=_customer())
		ip = "10.66.66." + frappe.generate_hash(length=2).encode().hex()[:2]
		frappe.local.request = Request(EnvironBuilder(method="POST").get_environ())
		frappe.local.request_ip = ip
		cmd = "ffl_core.agreements.load_for_signing"
		keys = [frappe.cache.make_key(f"rl:{cmd}:{ident}") + b":3600" for ident in (ip, f"{ip}:{token}", f"{ip}:{other}")]

		def reset():
			for k in keys:
				frappe.cache.delete(k)
			frappe.local.request = None
		self.addCleanup(reset)
		with _as("Guest"):
			for _ in range(30):
				frappe.local.form_dict = frappe._dict(cmd=cmd, token=token)
				agreements.load_for_signing(token)
			with self.assertRaises(frappe.RateLimitExceededError):
				agreements.load_for_signing(token)
			frappe.local.form_dict = frappe._dict(cmd=cmd, token=other)
			self.assertTrue(agreements.load_for_signing(other)["title"])


class Minors(_Base):

	def setUp(self):
		super().setUp()
		self.tpl = _template(minors_need_guardian=1)

	def dob(self, years):
		frappe.db.set_value("Customer", self.cust.name, "date_of_birth", add_days(nowdate(), -int(years * 365.25) - 2))

	def test_dob_under_18_needs_a_guardian(self):
		self.dob(17)
		r, token = self.request()
		with _as("Guest"):
			self.assertIs(agreements.load_for_signing(token)["minor_by_dob"], True)
		with self.assertRaises(frappe.ValidationError):
			self.sign(token, participant_is_minor=0, signer_relationship="Self")
		self.sign(token, signer_name="Pat Roe", signer_relationship="Parent")
		doc = frappe.get_doc("Customer Agreement", r["name"])
		self.assertEqual((doc.participant_is_minor, doc.minor_determined_by, doc.signer_relationship),
			(1, "Date of Birth", "Parent"))

	def test_adult_by_dob_is_never_asked_even_as_someones_child(self):
		self.dob(30)
		frappe.db.set_value("Customer", self.cust.name, "relationship", "Child")
		r, token = self.request()
		with _as("Guest"):
			self.assertIs(agreements.load_for_signing(token)["minor_by_dob"], False)
		self.sign(token, participant_is_minor=1)   # a DOB answer wins over the tick
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "participant_is_minor"), 0)

	def test_no_dob_the_signer_declares(self):
		r, token = self.request()
		with _as("Guest"):
			self.assertIsNone(agreements.load_for_signing(token)["minor_by_dob"])
		with self.assertRaises(frappe.ValidationError):
			self.sign(token, participant_is_minor=1, signer_relationship="Self")
		self.sign(token, participant_is_minor=1, signer_relationship="Guardian")
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "minor_determined_by"), "Self-declared")

	def test_template_without_the_rule_asks_nothing(self):
		tpl = _template()
		self.dob(10)
		r, token = self.request(tpl=tpl)
		with _as("Guest"):
			out = agreements.load_for_signing(token)
		self.assertEqual((out["minors_need_guardian"], out["minor_by_dob"]), (False, None))
		self.sign(token)
		self.assertEqual(frappe.db.get_value("Customer Agreement", r["name"], "participant_is_minor"), 0)


class Paper(_Base):

	def pending(self):
		return agreements.request_agreement(self.cust.name, self.tpl.name, "none")["name"]

	def test_manager_records_a_scan(self):
		name = self.pending()
		scan = _public_file(attached_to=("Customer Agreement", name))
		day = add_days(nowdate(), -3)
		manager = _user(_email(), "Sales Manager", "Sales User")
		with _as(manager):
			out = agreements.record_paper_agreement(name, scan.file_url, "Jane Roe", day)
		doc = frappe.get_doc("Customer Agreement", name)
		self.assertEqual((doc.status, doc.signed_via, doc.recorded_by), ("Signed", "Paper", manager))
		self.assertEqual(getdate(doc.expires_on), getdate(add_days(day, 365)))
		self.assertTrue(out["signed_pdf"].startswith("/private/files/"))
		self.assertTrue(frappe.db.get_value("File", {"file_url": doc.signed_pdf}, "is_private"))
		comment = frappe.get_all("Comment", filters={"reference_doctype": "Customer Agreement",
			"reference_name": name, "comment_type": "Comment"}, pluck="content")
		self.assertTrue(any("Paper copy recorded by" in c and manager in c for c in comment))

	def test_refused_without_its_own_scan(self):
		name = self.pending()
		with self.assertRaises(frappe.ValidationError):
			agreements.record_paper_agreement(name, "", "Jane", nowdate())
		other = _public_file(attached_to=("Customer", self.cust.name))
		with self.assertRaises(frappe.ValidationError):
			agreements.record_paper_agreement(name, other.file_url, "Jane", nowdate())
		mine = _public_file(attached_to=("Customer Agreement", name))
		with self.assertRaises(frappe.ValidationError):
			agreements.record_paper_agreement(name, mine.file_url, "Jane", add_days(nowdate(), 1))
		with self.assertRaises(frappe.ValidationError):
			agreements.record_paper_agreement(name, mine.file_url, "Jane", nowdate(), participant_is_minor=1)
		self.assertEqual(frappe.db.get_value("Customer Agreement", name, "status"), "Pending")

	def test_only_a_pending_copy(self):
		r, token = self.request()
		self.sign(token)
		scan = _public_file(attached_to=("Customer Agreement", r["name"]))
		with self.assertRaises(frappe.ValidationError):
			agreements.record_paper_agreement(r["name"], scan.file_url, "Jane", nowdate())


class Status(_Base):

	def signed(self, expires_on=None, tpl=None):
		r, token = self.request(tpl=tpl)
		self.sign(token)
		frappe.db.set_value("Customer Agreement", r["name"], "expires_on", expires_on)
		return r["name"]

	def state(self, tpl=None):
		return agreements.agreement_status(self.cust.name, (tpl or self.tpl).name)["state"]

	def test_the_states(self):
		self.assertEqual(self.state(), "missing")
		self.request()
		self.assertEqual(self.state(), "pending")
		name = frappe.db.get_value("Customer Agreement", {"customer": self.cust.name}, "name")
		token = "s" * 43
		frappe.db.set_value("Customer Agreement", name, "sign_token_hash", agreements._hash(token))
		self.sign(token)
		self.assertEqual(self.state(), "signed")
		frappe.db.set_value("Customer Agreement", name, "expires_on", add_days(nowdate(), 10))
		self.assertEqual(self.state(), "expiring")
		frappe.db.set_value("Customer Agreement", name, "expires_on", add_days(nowdate(), -1))
		self.assertEqual(self.state(), "expired", "judged on the day, before the daily job runs")

	def test_daily_job_expires_and_never_expiring_stays(self):
		old = self.signed(expires_on=add_days(nowdate(), -1))
		forever = self.signed(expires_on=None, tpl=_template(valid_days=0))
		agreements.expire_agreements()
		self.assertEqual(frappe.db.get_value("Customer Agreement", old, "status"), "Expired")
		self.assertEqual(frappe.db.get_value("Customer Agreement", forever, "status"), "Signed")
		self.assertEqual(self.state(), "expired")

	def test_counter_panel_shows_marked_templates(self):
		shown = _template(show_on_counter_panel=1)
		_template(show_on_counter_panel=1, active=0)
		rows = {a["template"]: a for a in get_customer_flags(self.cust.name)["agreements"]}
		self.assertEqual(rows[shown.name]["state"], "missing")
		self.assertNotIn(self.tpl.name, rows)
		self.signed(expires_on=add_days(nowdate(), -1), tpl=shown)
		agreements.expire_agreements()
		row = {a["template"]: a for a in get_customer_flags(self.cust.name)["agreements"]}[shown.name]
		self.assertEqual(row["state"], "expired")
		self.assertTrue(row["agreement"])

	def test_require_agreement(self):
		with self.assertRaises(agreements.AgreementRequired):
			agreements.require_agreement(self.cust.name, self.tpl.name)
		self.signed(expires_on=add_days(nowdate(), 100))
		agreements.require_agreement(self.cust.name, self.tpl.name)

	def test_customer_form_list(self):
		self.request()
		out = agreements.get_customer_agreements(self.cust.name)
		self.assertEqual([r["status"] for r in out["rows"]], ["Pending"])
		self.assertTrue(out["can_manage"])

	def test_merge_moves_agreements(self):
		from ffl_core.customer_profile import merge_customer

		name = self.signed(expires_on=add_days(nowdate(), 100))
		target = _customer()
		merge_customer(self.cust.name, target.name)
		self.assertEqual(frappe.db.get_value("Customer Agreement", name, "customer"), target.name)
		self.assertEqual(agreements.agreement_status(target.name, self.tpl.name)["state"], "signed")


class SaleGate(_Base):

	def setUp(self):
		super().setUp()
		self.item = frappe.get_doc({"doctype": "Item", "item_code": _uniq("Lane Rental"), "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 0, "ffl_required_agreement": self.tpl.name}).insert(ignore_permissions=True)
		_PROVIDED.clear()

	def invoice(self, **fields):
		si = _sales_invoice(self.cust.name, **fields)
		si.items[0].item_code = self.item.name
		return si

	def test_unsigned_customer_is_refused_signed_passes(self):
		si = self.invoice()
		with self.assertRaises(agreements.AgreementRequired) as cm:
			agreements.refuse_missing_required(si)
		self.assertIn(self.tpl.name, str(cm.exception))
		r, token = self.request()
		self.sign(token)
		agreements.refuse_missing_required(si)

	def test_drafts_are_not_refused(self):
		si = self.invoice()
		si.insert()
		self.assertEqual(si.docstatus, 0)

	def test_returns_consolidation_and_channel_sales_pass(self):
		for fields in ({"is_return": 1}, {"is_consolidated": 1}, {"woo_online_order": "WOO-1"}):
			si = self.invoice()
			for k, v in fields.items():
				si.set(k, v)
			agreements.refuse_missing_required(si)

	def test_switched_off_template_is_not_required(self):
		frappe.db.set_value("Agreement Template", self.tpl.name, "active", 0)
		agreements.refuse_missing_required(self.invoice())

	def test_extension_hook_adds_and_a_broken_one_is_logged(self):
		other = _template()
		_PROVIDED.append(other.name)
		real = frappe.get_hooks

		def hooks(name=None, *a, **k):
			if name == "gunshop_required_agreements":
				return [f"{__name__}._provider", f"{__name__}._broken_provider"]
			return real(name, *a, **k)
		frappe.db.set_value("Item", self.item.name, "ffl_required_agreement", None)
		with patch("frappe.get_hooks", side_effect=hooks), patch("frappe.log_error") as log:
			self.assertEqual(agreements.required_templates(self.invoice()), [other.name])
			with self.assertRaises(agreements.AgreementRequired):
				agreements.refuse_missing_required(self.invoice())
		self.assertIn("_broken_provider", log.call_args.kwargs["title"])
