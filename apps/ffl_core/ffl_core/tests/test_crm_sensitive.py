"""What never reaches a provider or a customer memory (ffl_core.crm.sensitive): pure functions."""
from __future__ import annotations

import unittest

from ffl_core.crm import sensitive

SSN, DL, DOB, CARD, NTN = "123-45-6789", "TX12345678", "1/2/1980", "4111 1111 1111 1111", "100ABC123"


class TestRedact(unittest.TestCase):
	def test_numbers_are_blanked_and_the_rest_kept(self):
		text = (f"Looking for a Garand. My DL is {DL}, DOB {DOB}. SSN {SSN}. Card {CARD} if you need a deposit. "
			f"NTN {NTN} came back.\nText me after 5.")
		out = sensitive.redact(text)
		for value in (SSN, DL, DOB, CARD, NTN, "4111"):
			self.assertNotIn(value, out)
		self.assertIn("Looking for a Garand.", out)
		self.assertIn("\nText me after 5.", out, "line breaks and untouched sentences stay")
		self.assertIn("My DL is [removed]", out)

	def test_labelled_numbers_without_dashes(self):
		self.assertNotIn("123456789", sensitive.redact("social security number: 123 45 6789"))
		self.assertNotIn("123456789", sensitive.redact("ssn 123456789").replace(" ", ""))
		self.assertEqual(sensitive.redact("NICS transaction number is 1A2B3C4D5"), "NICS transaction number is [removed]")
		self.assertEqual(sensitive.redact("born March 3, 1975 in Ohio"), "born [removed] in Ohio")
		self.assertEqual(sensitive.redact("My CCW permit number: P-1234567"), "My CCW permit number: [removed]")

	def test_a_sentence_about_eligibility_goes_whole(self):
		out = sensitive.redact("I was convicted of a misdemeanor in 2010. Can I still buy? Also want a Glock 19.")
		self.assertEqual(out, "[removed] Can I still buy? Also want a Glock 19.")
		self.assertEqual(sensitive.redact("My NICS got delayed again!"), "[removed]")

	def test_ordinary_text_is_left_alone(self):
		for text in ("Model 1911 in 45 ACP, budget $1,500", "Do you have a Glock 19 Gen5? ID me at the counter",
				"order 1234567890123 (not a card)", "Mosin 91/30 in stock?", ""):
			self.assertEqual(sensitive.redact(text), text)
			self.assertFalse(sensitive.has_sensitive(text))

	def test_model_numbers_near_a_permit_or_an_id_stay(self):
		for text in ("I have my CCW, looking for a P365XL", "Got my permit last week. Do you have a P320-XCOMPACT?",
				"CCW holder here, want a 1911-A1 for carry", "Bring your ID when you pick up the 1911-A1",
				"My CCW P365XL is my favorite"):
			self.assertEqual(sensitive.redact(text), text, text)
			self.assertFalse(sensitive.has_sensitive(text), text)
		# Real numbers by their label still go.
		for text, number in (("my DL is TX12345678", "TX12345678"), ("passport P12345678 attached", "P12345678"),
				("Permit #: P-1234567", "P-1234567"), ("CCW number 123456789", "123456789"),
				("ID: D1234567", "D1234567"), ("driver's license no. 98765432", "98765432")):
			self.assertNotIn(number, sensitive.redact(text), text)

	def test_ordinary_buying_sentences_stay(self):
		for text in ("I'm committed to buying the Garand this week, when can I come fill out the 4473?",
				"Looking for a birthday gift, a 1911 for my dad",
				"After the background check clears, I'll pick it up"):
			self.assertEqual(sensitive.redact(text), text, text)
		# What can bar a purchase still goes, sentence and all.
		for text in ("I was committed to a mental hospital in 2015.", "I was involuntarily committed once.",
				"I have a restraining order against me.", "My NICS was denied last month.",
				"I'm on probation until March.", "I use medical marijuana.", "born in 1975, by the way."):
			self.assertTrue(sensitive.has_sensitive(text), text)
		self.assertTrue(sensitive.is_compliance_question("Do I need to fill out a 4473 for this?"))

	def test_price_stock_and_compliance_words(self):
		for text in ("budget $1,500", "about 800 dollars", "is it in stock", "sold out?", "we have 3", "available now"):
			self.assertTrue(sensitive.has_price_or_stock(text), text)
		self.assertFalse(sensitive.has_price_or_stock("Wants a Garand, shooting grade"))
		for text in ("Can you ship to CA?", "Is it legal in NY?", "Is a background check needed?", "NFA item?"):
			self.assertTrue(sensitive.is_compliance_question(text), text)
		self.assertFalse(sensitive.is_compliance_question("Does it come with a sling?"))


class TestMemoryProblems(unittest.TestCase):
	CONTEXT = {"new": [{"ref": "E1"}, {"ref": "E2"}],
		"memory": [{"ref": "M1", "kind": "want", "locked": False}, {"ref": "M2", "kind": "budget", "locked": True}]}

	def test_a_good_answer_has_none(self):
		answer = {"add": [{"kind": "want", "text": "M1 Garand", "keywords": "Garand", "source": "E1"},
			{"kind": "budget", "text": "", "amount": 1500, "source": "E2"}],
			"update": [{"ref": "M1", "text": "Glock 19 Gen5", "keywords": "Glock 19", "source": "E2"}],
			"remove": ["M1"], "summary": "Wants a Garand; we owe him an answer."}
		self.assertEqual(sensitive.memory_problems(answer, self.CONTEXT), [])

	def test_each_entry_at_fault_is_named(self):
		answer = {"add": [{"kind": "want", "text": f"DL {DL}", "keywords": "x", "source": "E1"},
			{"kind": "want", "text": "Glock", "keywords": "Glock", "source": "E9"},
			{"kind": "budget", "text": "$1,500", "amount": 1500, "source": "E1"},
			{"kind": "budget", "amount": 0, "source": "E1"},
			{"kind": "question", "text": "Can you ship to CA?", "source": "E1"},
			{"kind": "favorite_color", "text": "red", "source": "E1"}],
			"update": [{"ref": "M2", "amount": 900, "source": "E1"}, {"ref": "M7", "text": "x", "source": "E1"}],
			"remove": ["M2", "M9"], "summary": "Glock 19 in stock at $499"}
		found = {(p["part"], p["index"]) for p in sensitive.memory_problems(answer, self.CONTEXT)}
		self.assertEqual(found, {("add", 0), ("add", 1), ("add", 2), ("add", 3), ("add", 4), ("add", 5),
			("update", 0), ("update", 1), ("remove", 0), ("remove", 1), ("summary", None)})
		self.assertEqual(sensitive.memory_problems("nope", self.CONTEXT)[0]["part"], "answer")
