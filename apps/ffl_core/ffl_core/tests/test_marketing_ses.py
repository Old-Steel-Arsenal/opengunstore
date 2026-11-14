"""SES bounces and complaints through SNS (ffl_core.marketing.providers.ses). Messages are signed for real
with a self-signed certificate, and the module verifies them for real; only fetching the certificate is
handed in. Forged and altered signatures, foreign topics and certificate hosts are refused before
anything is read; a permanent bounce, a transient one and a complaint each do what they should, once."""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import frappe
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from ffl_core.marketing import SETTINGS
from ffl_core.marketing.contacts import BOUNCED, COMPLAINED, SUBSCRIBED, UNSUBSCRIBED
from ffl_core.marketing.providers import ses
from ffl_core.tests.marketing_mail_support import MailTestCase, contact
from ffl_core.tests.marketing_support import address, events_of, status_of

TOPIC = "arn:aws:sns:us-east-1:123456789012:store-ses-events"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-0000000000000000000000.pem"
SUBSCRIBE_URL = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=x&Token=y"


def _key_and_cert(days: int = 30, start: int = -1):
	key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
	name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
	now = datetime.now(UTC)
	cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
		.serial_number(x509.random_serial_number()).not_valid_before(now + timedelta(days=start))
		.not_valid_after(now + timedelta(days=days)).sign(key, hashes.SHA256()))
	return key, cert.public_bytes(serialization.Encoding.PEM)


KEY, PEM = _key_and_cert()
OTHER_KEY, _ = _key_and_cert()


def _signed(message: dict, key=KEY, version: str = "1") -> dict:
	message = {"SignatureVersion": version, "SigningCertURL": CERT_URL, **message}
	digest = hashes.SHA1() if version == "1" else hashes.SHA256()
	signature = key.sign(ses.string_to_sign(message).encode(), padding.PKCS1v15(), digest)
	return {**message, "Signature": base64.b64encode(signature).decode()}


def _notification(event: dict, message_id: str | None = None, topic: str = TOPIC, **kwargs) -> dict:
	return _signed({"Type": "Notification", "MessageId": message_id or frappe.generate_hash(length=20),
		"TopicArn": topic, "Message": json.dumps(event), "Timestamp": "2026-11-13T10:00:00.000Z"}, **kwargs)


def _bounce(email: str, kind: str = "Permanent", **kwargs) -> dict:
	return _notification({"notificationType": "Bounce", "bounce": {"bounceType": kind, "bounceSubType": "General",
		"bouncedRecipients": [{"emailAddress": email, "diagnosticCode": "smtp; 550 5.1.1 user unknown"}]},
		"mail": {"destination": [email]}}, **kwargs)


def _complaint(email: str, **kwargs) -> dict:
	return _notification({"eventType": "Complaint", "complaint": {"complaintFeedbackType": "abuse",
		"complainedRecipients": [{"emailAddress": email}]}}, **kwargs)


class SESCase(MailTestCase):
	def setUp(self):
		super().setUp()
		self.set_single(SETTINGS, "sns_topic_arn", TOPIC)
		self.fetched: list[str] = []

	def _get(self, url: str) -> bytes:
		self.fetched.append(url)
		return PEM if url == CERT_URL else b"<ConfirmSubscriptionResponse/>"

	def handle(self, message: dict) -> dict:
		return ses.handle(json.dumps(message), http_get=self._get)


class TestWhatIsAccepted(SESCase):
	def test_a_valid_message_verifies_with_either_signature_version(self):
		for version in ("1", "2"):
			with self.subTest(version=version):
				result = self.handle(_notification({"eventType": "Delivery"}, version=version))
				self.assertEqual(result, {"type": "Notification", "processed": 0})
		self.assertEqual(self.fetched, [CERT_URL, CERT_URL])

	def test_a_forged_or_altered_signature_is_refused(self):
		email = contact(SUBSCRIBED)
		forged = _bounce(email, key=OTHER_KEY)
		altered = _bounce(address())
		altered["Message"] = altered["Message"].replace(altered["Message"].split('"emailAddress": "')[1].split('"')[0], email)
		for message in (forged, altered, {**_bounce(email), "Signature": "not base64!"}, {**_bounce(email), "Signature": ""},
				{**_bounce(email), "SignatureVersion": "3"}):
			with self.subTest(message=message.get("SignatureVersion")), self.assertRaises(ses.Refused):
				self.handle(message)
		self.assertEqual(status_of(email), SUBSCRIBED)

	def test_another_topic_is_refused_before_anything_is_fetched(self):
		email = contact(SUBSCRIBED)
		with self.assertRaises(ses.Refused):
			self.handle(_bounce(email, topic="arn:aws:sns:us-east-1:999999999999:someone-else"))
		self.set_single(SETTINGS, "sns_topic_arn", None)
		with self.assertRaises(ses.Refused):
			self.handle(_bounce(email))
		self.assertEqual(self.fetched, [])
		self.assertEqual(status_of(email), SUBSCRIBED)

	def test_a_certificate_anywhere_but_sns_is_never_fetched(self):
		for url in ("http://sns.us-east-1.amazonaws.com/cert.pem", "https://sns.us-east-1.amazonaws.com.evil.example/cert.pem",
				"https://evil.example/sns.us-east-1.amazonaws.com/cert.pem", "https://sns.us-east-1.amazonaws.com:8443/cert.pem",
				"https://user@sns.us-east-1.amazonaws.com/cert.pem", "https://sns.us-east-1.amazonaws.com/cert.txt",
				"https://s3.amazonaws.com/cert.pem"):
			with self.subTest(url=url), self.assertRaises(ses.Refused):
				message = _bounce(address())
				message["SigningCertURL"] = url
				self.handle(message)
		self.assertEqual(self.fetched, [])

	def test_an_expired_certificate_is_refused(self):
		key, pem = _key_and_cert(days=-1, start=-30)
		with self.assertRaises(ses.Refused):
			ses.handle(json.dumps(_bounce(address(), key=key)), http_get=lambda url: pem)

	def test_not_json_or_not_sns(self):
		for body in ("", "not json", "[]", json.dumps({"TopicArn": TOPIC})):
			with self.subTest(body=body), self.assertRaises(ses.Refused):
				ses.handle(body, http_get=self._get)


class TestSubscription(SESCase):
	def _confirmation(self, url: str = SUBSCRIBE_URL, topic: str = TOPIC) -> dict:
		return _signed({"Type": "SubscriptionConfirmation", "MessageId": "m-1", "Token": "tok", "TopicArn": topic,
			"Message": "You have chosen to subscribe", "SubscribeURL": url, "Timestamp": "2026-11-13T10:00:00.000Z"})

	def test_the_configured_topic_is_confirmed(self):
		self.assertEqual(self.handle(self._confirmation()), {"type": "SubscriptionConfirmation", "processed": 0})
		self.assertEqual(self.fetched, [CERT_URL, SUBSCRIBE_URL])

	def test_another_topic_or_a_foreign_confirm_address_is_not(self):
		with self.assertRaises(ses.Refused):
			self.handle(self._confirmation(topic="arn:aws:sns:us-east-1:999999999999:other"))
		with self.assertRaises(ses.Refused):
			self.handle(self._confirmation(url="https://evil.example/?Action=ConfirmSubscription"))
		self.assertNotIn(SUBSCRIBE_URL, self.fetched)
		self.assertFalse([u for u in self.fetched if "evil" in u])


class TestFeedback(SESCase):
	def test_a_permanent_bounce_bounces_the_contact_once(self):
		email = contact(SUBSCRIBED)
		message = _bounce(email, message_id="msg-permanent")
		self.assertEqual(self.handle(message)["processed"], 1)
		self.assertEqual(status_of(email), BOUNCED)
		events = events_of(email, BOUNCED)
		self.assertEqual([e.dedupe_key for e in events], [f"sns:msg-permanent:{email}"])
		detail = frappe.parse_json(events[0].detail)
		self.assertEqual((detail["provider"], detail["bounce_type"]), ("ses", "Permanent"))
		self.assertEqual(frappe.db.get_value("Marketing Contact", email, "status_reason"), "Bounce (General)")
		self.assertEqual(self.handle(message)["processed"], 0, "SNS retrying the same message changes nothing")
		self.assertEqual(len(events_of(email, BOUNCED)), 1)

	def test_a_transient_bounce_is_logged_and_keeps_the_status(self):
		email = contact(SUBSCRIBED)
		message = _bounce(email, kind="Transient", message_id="msg-transient")
		self.assertEqual(self.handle(message)["processed"], 1)
		self.assertEqual(status_of(email), SUBSCRIBED)
		self.assertEqual(frappe.parse_json(events_of(email, BOUNCED)[0].detail)["bounce_type"], "Transient")
		self.handle(message)
		self.assertEqual(len(events_of(email, BOUNCED)), 1)

	def test_a_complaint_outranks_an_unsubscribe(self):
		email = contact(UNSUBSCRIBED)
		self.assertEqual(self.handle(_complaint(email))["processed"], 1)
		self.assertEqual(status_of(email), COMPLAINED)

	def test_a_bounce_does_not_undo_a_complaint(self):
		email = contact(COMPLAINED)
		self.assertEqual(self.handle(_bounce(email))["processed"], 0)
		self.assertEqual(status_of(email), COMPLAINED)

	def test_an_address_that_is_not_a_contact_is_ignored(self):
		email = address("transactional")
		self.assertEqual(self.handle(_bounce(email))["processed"], 0)
		self.assertEqual(self.handle(_complaint(email))["processed"], 0)
		self.assertFalse(frappe.db.exists("Marketing Contact", email), "nothing is created")

	def test_an_address_in_another_case_or_with_a_name_is_found(self):
		email = contact(SUBSCRIBED)
		self.handle(_bounce(f"Pat <{email.upper()}>"))
		self.assertEqual(status_of(email), BOUNCED)

	def test_other_events_and_other_messages_are_ignored(self):
		email = contact(SUBSCRIBED)
		for event in ({"eventType": "Delivery", "delivery": {"recipients": [email]}}, {"eventType": "Open"}, "not an object"):
			with self.subTest(event=event):
				self.assertEqual(self.handle(_notification(event))["processed"], 0)
		self.assertEqual(status_of(email), SUBSCRIBED)
