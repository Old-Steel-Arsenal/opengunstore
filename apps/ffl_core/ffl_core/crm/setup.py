"""Frappe CRM on the POS — install / migrate plumbing.

Owns: the custom fields (``CUSTOM_FIELDS``), the Lead Mail Settings default, the team mail
setup (``crm.team.apply``: staff Reply-To, team mailboxes in User Emails), the same-site
ERPNext CRM Settings (on, this site, ``sync_products`` off), CRM's currency, the deal stages, moving CRM's own
"create lead from incoming email" switch to ours, the Contact page's Email button, the Deal page's customer card and
Open order and Add want buttons and its Hold gun menu (:func:`apply_pos_link`, following Lead Mail Settings ›
crm_pos_link and crm_deal_holds), and the one-off backfill that makes every Item a CRM Product.

Runs at ffl_core's after_install and after_migrate, and when ``crm`` is installed on a site that
already has ffl_core (after_app_install): an existing store gets Frappe CRM with the release that
brought this package (``bench install-app crm`` in deploy/prod/auto_deploy.sh). Without CRM on the
site it does nothing.

Custom fields: ``create_custom_fields`` (module None, not the fixture) — the route setup/custody.py
documents; a fixture would also fail on a site where CRM's DocTypes do not exist yet.

ERPNext CRM Settings is written field by field, not saved: its save (CRM's controller) also
grants Sales User and Sales Manager write / create / delete on Item, which the POS does not
give its counter. The three setup steps of that save we need (CRM's custom fields, the
Quotation "to" option, CRM's Create Quotation script) are called directly.
"""
from __future__ import annotations

import json

import frappe
from frappe.utils import cint

from ffl_core.crm import TITLE, installed, pos_link_on, team
from ffl_core.crm.customer import CARD_FIELD
from ffl_core.crm.deal import SERIAL_FIELD
from ffl_core.crm.deals import DEAL, STATUS
from ffl_core.crm.gunbroker import MSGID_FIELD
from ffl_core.crm.inquiries import EVENT_FIELD
from ffl_core.crm.mail import AUTO_LEAD, CATEGORY, CATEGORY_FIELD, LEAD, OTHER_MAIL, SETTINGS
from ffl_core.crm import memory
from ffl_core.crm.orders import DEAL_FIELD
from ffl_core.crm import sell_requests
from ffl_core.crm.web_orders import ORDER as WEB_ORDER, WON_BY

CRM_SETTINGS = "ERPNext CRM Settings"
FCRM_SETTINGS = "FCRM Settings"
SEEDED_FLAG = "ffl_crm_settings_seeded"
BACKFILL_FLAG = "ffl_crm_backfill_done"
DEAL_STAGES_FLAG = "ffl_crm_deal_stages_done"
# CRM's stock deal stages and what they become. A stage dropped hands its deals to the one named.
RENAMED_STAGES = {"Qualification": "New", "Demo/Making": "Talking", "Proposal/Quotation": "Quoted"}
DROPPED_STAGES = {"Negotiation": "Talking", "Ready to Close": "Quoted"}
DEAL_STAGES = ("New", "Talking", "Quoted", "Won", "Lost")
MAIL_CATEGORIES = ("Automated", "Blocked", "Distributor", "Dealer", "GunBroker", "Marketing", "Notification",
	"Our store", "Vendor", "Other")
CATEGORY_RULES = {"gunbroker.com": "GunBroker", "rsrgroup.com": "Distributor"}
LEAD_SOURCES = ("Website", "GunBroker")
MAIL_SETUP_FLAG = "ffl_crm_other_mail_seeded"
VIEWS_FLAG = "ffl_crm_views_seeded"
SELL_VIEW_FLAG = "ffl_crm_sell_view_seeded"
SELL_VIEW = "Sell requests"
FOLLOW_UPS_FLAG = "ffl_crm_follow_ups_seeded"
# CRM's switches that keep a lead's / deal's communication status: answered mail Replied, new mail Open.
# My follow-ups (ensure_crm_views) lists the Open ones, so it needs both.
FOLLOW_UP_SWITCHES = ("auto_mark_replied_on_response", "auto_reopen_on_new_communication")
FOLLOW_UPS = "My follow-ups"
# A deal is titled, listed first and searched by the person's name (lead_name, which crm.deals fills),
# as a lead is; CRM's own Deal title is the organization, empty for a person. Kept on every migrate.
DEAL_PROPERTIES = (
	(None, "title_field", "lead_name", "Data"),
	("lead_name", "label", "Full Name", "Data"),
)
NAME_COLUMN = {"label": "Full Name", "type": "Data", "key": "lead_name", "width": "12rem"}
DEAL_COLUMNS_FLAG = "ffl_crm_deal_name_columns_done"
DEAL_FILTER_FLAG = "ffl_crm_deal_name_filter_done"
BACKFILL_JOB = "ffl_crm_product_backfill"
# CRM's own per-page customization (a CRM Form Script record): the Email button on a Contact page.
CONTACT_SCRIPT = "OpenGunstore: Email a contact"
CONTACT_SCRIPT_JS = """function setupForm({ doc, call, router }) {
	return {
		actions: [{
			label: __("Email"),
			onClick: async () => {
				const deal = await call("ffl_core.api.crm.open_deal", { contact: doc.name })
				router.push({ name: "Deal", params: { dealId: deal }, hash: "#emails" })
			},
		}],
	}
}
"""
# The Deal page's customer card, Open order and Add want buttons, and Hold gun in its "..." menu while
# Lead Mail Settings › Hold guns for deals is on (crm/holds.py): a CRM Form Script, on while the switch is.
DEAL_SCRIPT = "OpenGunstore: Deal to POS"
_DEAL_SCRIPT_TEMPLATE = """class CRMDeal {
	onLoad() {
		if (this.doc.__newDocument) return
		if (this.doc.%(sell)s) {
			// A gun offered to the store (crm/sell_requests.py): buying it, not selling.
			if (!this.doc.%(acquisition)s && !this.doc.%(receipt)s) {
				this.actions.push({ label: __("Make offer"), onClick: () => this.fflMakeOffer() })
				this.actions.push({ label: __("Seller accepted"), onClick: () => this.fflSellerAccepted() })
				this.actions.push({ label: __("Receive gun"), onClick: () => this.fflReceiveGun() })
			}
		} else {
			this.actions.push({ label: __("Open order"), onClick: () => this.fflOpenOrder() })
			this.actions.push({ label: __("Add want"), onClick: () => this.fflAddWant() })
%(hold_menu)s		}
		this.fflLoadCard()
	}
	fflError(e) {
		toast.error(e.messages?.[0] || e.message || __("Something went wrong."))
	}
	fflLoadCard() {
		call("ffl_core.api.crm.deal_customer_summary", { deal: this.doc.name })
			.then((summary) => this.setFieldHtml("%(card)s", summary.html))
			.catch((e) => this.fflError(e))
		if (this.doc.%(sell)s) this.fflShowSell(call("ffl_core.api.crm.sell_request_card", { deal: this.doc.name }))
	}
	fflShowSell(request) {
		return request.then((card) => { this.setFieldHtml("%(sell_card)s", card.html); return card })
			.catch((e) => this.fflError(e))
	}
	fflMakeOffer() {
		formDialog({
			title: __("Make offer"),
			fields: [{ fieldname: "amount", fieldtype: "Currency", label: __("Offer"), reqd: 1 }],
			defaults: { amount: this.doc.%(offer)s || undefined },
			submitLabel: __("Save offer"),
			onSubmit: async (data) => {
				try {
					const card = await call("ffl_core.api.crm.make_sell_offer", { deal: this.doc.name, amount: data.amount })
					this.setFieldHtml("%(sell_card)s", card.html)
				} catch (e) {
					throw new Error(e.messages?.[0] || e.message)
				}
			},
		})
	}
	fflSellerAccepted() {
		this.fflShowSell(call("ffl_core.api.crm.sell_offer_accepted", { deal: this.doc.name }))
	}
	fflReceiveGun() {
		return this.fflWithCustomer(async () => {
			await call("ffl_core.api.crm.sell_gun_intake", { deal: this.doc.name })
			window.location.href = "/app/point-of-sale?ffl_crm_deal=" + encodeURIComponent(this.doc.name)
		}, __("Who is the seller?"))
	}
	async fflWithCustomer(next, title) {
		try {
			const linked = await call("ffl_core.api.crm.ensure_deal_customer", { deal: this.doc.name })
			if (linked.customer) return await next()
			const choose = (args) => async (close) => {
				close()
				try {
					await call("ffl_core.api.crm.ensure_deal_customer", { deal: this.doc.name, ...args })
					this.fflLoadCard()
					await next()
				} catch (e) {
					this.fflError(e)
				}
			}
			createDialog({
				title: title || __("Who is the buyer?"),
				html: linked.html,
				actions: [
					...linked.matches.map((m) => ({
						label: m.banned ? __("Use {0} (Banned)", [m.customer_name]) : __("Use {0}", [m.customer_name]),
						variant: "solid",
						theme: m.banned ? "red" : undefined,
						onClick: choose({ customer: m.name }),
					})),
					{
						label: __("Create customer"),
						variant: linked.matches.length ? "outline" : "solid",
						onClick: choose({ create: 1 }),
					},
				],
			})
		} catch (e) {
			this.fflError(e)
		}
	}
	fflOpenOrder() {
		return this.fflWithCustomer(() => this.fflGoToOrder())
	}
	fflAddWant() {
		formDialog({
			title: __("Add want"),
			fields: [
				{ fieldname: "keywords", fieldtype: "Data", label: __("Keywords"), placeholder: __("P228, Mosin 91/30") },
				{ fieldname: "item", fieldtype: "Link", options: "Item", label: __("Item (optional)") },
				{ fieldname: "budget", fieldtype: "Currency", label: __("Budget") },
				{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes") },
			],
			submitLabel: __("Add want"),
			onSubmit: async (data) => {
				if (!data.keywords && !data.item) throw new Error(__("Fill in keywords or an item."))
				try {
					await call("ffl_core.api.crm.add_want", { deal: this.doc.name, ...data })
				} catch (e) {
					throw new Error(e.messages?.[0] || e.message)
				}
				toast.success(__("Added. You get a task when something that fits arrives."))
			},
		})
	}
	fflEsc(s) {
		return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c])
	}
	async fflManager(info) {
		if (info.self_approves) return {}
		const people = info.approvers || []
		const data = await formDialog({
			title: __("Manager approval"),
			fields: [
				{ fieldname: "approver", fieldtype: "Select", label: __("Manager"), reqd: 1,
					options: people.map((p) => ({ label: p.full_name, value: p.user })) },
				{ fieldname: "secret", fieldtype: "Password", label: __("PIN or password"), reqd: 1 },
			],
			submitLabel: __("Approve"),
		})
		if (!data || !data.approver || !data.secret) return null
		const p = people.find((x) => x.user === data.approver) || {}
		return p.has_pin && !p.pin_off ? { approver: data.approver, pin: data.secret } : { approver: data.approver, password: data.secret }
	}
	fflHoldGun() {
		return this.fflWithCustomer(() => this.fflHoldDialog())
	}
	async fflHoldDialog() {
		let info
		try {
			info = await call("ffl_core.api.crm.deal_holds", { deal: this.doc.name })
		} catch (e) {
			return this.fflError(e)
		}
		if (!info.guns.length) return toast.error(__("Pick the gun on the deal's product line first."))
		const esc = (s) => this.fflEsc(s)
		const fields = [], defaults = {}, free = [], mine = []
		info.guns.forEach((g, i) => {
			const label = g.item_name + " · " + g.serial
			const key = "ffl_gun_" + i
			if (g.hold) {
				mine.push(g)
				fields.push({ fieldname: key, fieldtype: "HTML", options: "<div>" + esc(label) + "</div><div class='text-ink-gray-5'>"
					+ esc(__("Held until {0}", [g.hold.expires_label])) + "</div>" })
			} else if (g.blocked || !info.enabled) {
				fields.push({ fieldname: key, fieldtype: "HTML", options: "<div>" + esc(label) + "</div><div class='text-ink-gray-5'>"
					+ esc(g.blocked || __("Holding guns for deals is off. A manager can turn it on in Lead Mail Settings.")) + "</div>" })
			} else {
				free.push([g, key])
				fields.push({ fieldname: key, fieldtype: "Check", label: label })
				defaults[key] = 1
			}
		})
		const run = (fn) => async (ctx) => {
			try {
				await fn(ctx)
			} catch (e) {
				this.fflError(e)
			}
		}
		const actions = []
		if (free.length) {
			actions.push({ label: __("Hold for 48 hours"), variant: "solid", onClick: run(async ({ data, close }) => {
				const picked = free.filter(([, key]) => data[key]).map(([g]) => g)
				if (!picked.length) return toast.error(__("Pick a gun to hold."))
				const auth = picked.some((g) => g.needs_manager) ? await this.fflManager(info) : {}
				if (!auth) return
				const res = await call("ffl_core.api.crm.hold_guns", { deal: this.doc.name, serials: picked.map((g) => g.serial), ...auth })
				close()
				res.refused.forEach((r) => toast.error(r.reason))
				if (res.held.length) toast.success(__("Held until {0}.", [res.held[0].expires_label]))
				this.fflLoadCard()
			}) })
		}
		mine.forEach((g) => {
			const which = info.guns.length > 1 ? " " + g.serial : ""
			actions.push({ label: __("Extend") + which, onClick: run(async ({ close }) => {
				const auth = g.hold.extend_needs_manager ? await this.fflManager(info) : {}
				if (!auth) return
				const hold = await call("ffl_core.api.crm.extend_hold", { hold: g.hold.name, ...auth })
				close()
				toast.success(__("Hold extended to {0}.", [hold.expires_label]))
				this.fflLoadCard()
			}) })
			actions.push({ label: __("Release") + which, theme: "red", onClick: run(async ({ close }) => {
				const auth = g.hold.release_needs_manager ? await this.fflManager(info) : {}
				if (!auth) return
				await call("ffl_core.api.crm.release_hold", { hold: g.hold.name, ...auth })
				close()
				toast.success(__("Hold released."))
				this.fflLoadCard()
			}) })
		})
		formDialog({ title: __("Hold guns"), fields, defaults, actions, cancelLabel: __("Close") })
	}
	async fflGoToOrder() {
		const order = await call("ffl_core.api.crm.open_deal_order", { deal: this.doc.name })
		window.location.href = order.route
	}
}
"""
_HOLD_MENU = """			this.actions.push({ group: __("More"), hideLabel: true, items: [{ label: __("Hold gun"), onClick: () => this.fflHoldGun() }] })
"""


def deal_script_js(holds: bool) -> str:
	"""The Deal page's script, with Hold gun in its "..." menu when *holds*."""
	return _DEAL_SCRIPT_TEMPLATE % {"card": CARD_FIELD, "hold_menu": _HOLD_MENU if holds else "",
		"sell": sell_requests.SELL, "sell_card": sell_requests.CARD, "offer": sell_requests.OFFER,
		"acquisition": sell_requests.ACQUISITION, "receipt": sell_requests.RECEIPT}


DEAL_SCRIPT_JS = deal_script_js(False)
# Draft reply and Insert price on the Lead and Deal pages (crm/reply_draft.py, crm/price_insert.py): one
# script per doctype, always on; the page asks reply_draft_ready which buttons to show. The draft goes into
# CRM's own composer above the quote when it is open on the mail being answered (TipTap keeps its editor on
# the editable element: el.editor), else into the dialog below, which sends through the endpoint CRM's
# composer calls. docs/versions.md lists what a CRM upgrade must check.
LEAD_SCRIPT = "OpenGunstore: Lead"
DEAL_REPLY_SCRIPT = "OpenGunstore: Deal reply"
_REPLY_SCRIPT_TEMPLATE = """class %(cls)s {
	onLoad() {
		if (this.doc.__newDocument) return
		call("ffl_core.api.crm.reply_draft_ready", { doctype: "%(dt)s", name: this.doc.name })
			.then((shown) => {
				const add = []
				if (shown.ready) add.push({ label: __("Draft reply"), onClick: () => this.fflDraftReply() })
				if (shown.insert_price) add.push({ label: __("Insert price"), onClick: () => this.fflInsertPrice() })
				this.actions.unshift(...add)
			})
			.catch((e) => console.warn("[OpenGunstore] reply buttons", e))
		this.fflLoadMemory()
	}
	fflLoadMemory() {
		call("ffl_core.api.crm.memory_card", { doctype: "%(dt)s", name: this.doc.name })
			.then((card) => this.fflShowMemory(card))
			.catch((e) => console.warn("[OpenGunstore] memory card", e))
	}
	fflShowMemory(card) {
		this.fflMemory = card
		const at = this.actions.findIndex((a) => a.fflMemory)
		if (at > -1) this.actions.splice(at, 1)
		this.setFieldProperty("%(card)s", "hidden", !card.shown)
		if (!card.shown) return
		this.setFieldHtml("%(card)s", card.html)
		this.fflWatchSources()
		if (!card.can_edit) return
		const items = [{ label: __("Edit memory"), onClick: () => this.fflEditMemory() }]
		if (card.ready) items.push({ label: __("Update memory"), onClick: () => this.fflUpdateMemory() })
		if (card.has_content) items.push({ label: __("Clear memory"), onClick: () => this.fflClearMemory() })
		this.actions.push({ group: __("More"), hideLabel: true, fflMemory: true, items })
	}
	fflEsc(s) {
		return String(s ?? "").replace(/[&<>"'{}]/g, (c) => "&#" + c.charCodeAt(0) + ";")
	}
	fflWatchSources() {
		// One listener for the page's life: each "said" link on a card carries its own record.
		if (window.fflMemorySources) return
		window.fflMemorySources = true
		window.document.addEventListener("click", (ev) => {
			const link = ev.target.closest && ev.target.closest("[data-ffl-memory-source]")
			if (!link) return
			ev.preventDefault()
			call("ffl_core.api.crm.memory_source", { doctype: link.dataset.fflMemoryDoctype,
				name: link.dataset.fflMemoryName, row: link.dataset.fflMemorySource })
				.then((m) => createDialog({ title: m.subject || __("Message"),
					html: "<div class='text-ink-gray-5'>" + this.fflEsc(m.at) + "</div>"
						+ "<div class='text-ink-gray-8' style='white-space:pre-wrap;margin-top:8px'>" + this.fflEsc(m.text) + "</div>" }))
				.catch((e) => toast.error(this.fflMessage(e)))
		}, true)
	}
	fflEditMemory() {
		formDialog({
			title: __("Edit memory"),
			fields: [
				{ fieldname: "want", fieldtype: "Small Text", label: __("Wants") },
				{ fieldname: "budget", fieldtype: "Currency", label: __("Budget") },
				{ fieldname: "state", fieldtype: "Data", label: __("State") },
				{ fieldname: "trade_in", fieldtype: "Small Text", label: __("Trade-in") },
				{ fieldname: "preference", fieldtype: "Small Text", label: __("Prefers") },
				{ fieldname: "question", fieldtype: "Small Text", label: __("Open") },
				{ fieldname: "auto_update", fieldtype: "Check", label: __("Update with AI") },
			],
			defaults: { ...this.fflMemory.values },
			submitLabel: __("Save"),
			onSubmit: async (data) => {
				try {
					this.fflShowMemory(await call("ffl_core.api.crm.memory_save", {
						doctype: "%(dt)s", name: this.doc.name, values: JSON.stringify(data) }))
				} catch (e) {
					throw new Error(this.fflMessage(e))
				}
			},
		})
	}
	async fflUpdateMemory() {
		if (this.fflUpdating) return
		this.fflUpdating = true
		const waiting = toast.loading(__("Updating…"))
		try {
			this.fflShowMemory(await call("ffl_core.api.crm.memory_update_now", { doctype: "%(dt)s", name: this.doc.name }))
			toast.dismiss(waiting)
			toast.success(__("Memory updated."))
		} catch (e) {
			toast.dismiss(waiting)
			toast.error(e?.messages?.[0] || __("Couldn't update memory."))
		} finally {
			this.fflUpdating = false
		}
	}
	fflClearMemory() {
		createDialog({
			title: __("Clear memory?"),
			actions: [{ label: __("Clear"), variant: "solid", theme: "red", onClick: async (close) => {
				close()
				try {
					const card = await call("ffl_core.api.crm.memory_clear", { doctype: "%(dt)s", name: this.doc.name })
					this.fflShowMemory(card)
					if (card.summaries_kept) toast(__("{0} summaries on records you can't change were kept.", [card.summaries_kept]))
				} catch (e) {
					toast.error(this.fflMessage(e))
				}
			} }],
		})
	}
	fflMessage(e) {
		return e?.messages?.[0] || e?.message || __("Something went wrong.")
	}
	fflComposer() {
		try {
			const found = [...window.document.querySelectorAll(".ProseMirror")].filter((el) => {
				const editor = el.editor
				return editor && editor.isEditable && el.offsetParent !== null
					&& "class" in (editor.schema.nodes.paragraph?.spec?.attrs || {})
			})
			if (found.length === 1) return found[0].editor
		} catch (e) {
			// fall through: the dialog takes the draft
		}
		console.warn("[OpenGunstore] composer not found")
		return null
	}
	fflQuoteAt(editor) {
		let at = null
		editor.state.doc.forEach((node, offset) => {
			if (at === null && node.type.name === "blockquote") at = { pos: offset, text: node.textContent }
		})
		return at
	}
	async fflDraftReply() {
		if (this.fflDrafting) return
		this.fflDrafting = true
		const waiting = toast.loading(__("Drafting…"))
		try {
			const open = this.fflComposer()
			const quote = open && this.fflQuoteAt(open)
			const draft = await call("ffl_core.api.crm.draft_reply", {
				doctype: "%(dt)s", name: this.doc.name, composer: open ? 1 : 0,
				quote_text: quote ? quote.text.replace(/\\s+/g, " ").trim().slice(0, 1000) : "",
			})
			toast.dismiss(waiting)
			const editor = this.fflComposer()
			if (editor && draft.native_ok) {
				const at = this.fflQuoteAt(editor)
				const pos = at ? at.pos : editor.state.doc.content.size
				editor.chain().insertContentAt(pos, draft.content_html).setTextSelection(pos + 1).focus().scrollIntoView().run()
				if (draft.handoff) toast.error(__("Needs a person: {0}", [draft.handoff]), { duration: Infinity })
				else toast.success(__("Draft added."))
			} else {
				this.fflReplyDialog(draft)
			}
		} catch (e) {
			toast.dismiss(waiting)
			toast.error(this.fflMessage(e))
		} finally {
			this.fflDrafting = false
		}
	}
	fflReplyDialog(draft) {
		const esc = (s) => this.fflEsc(s)
		const fields = [{ fieldname: "ffl_envelope", fieldtype: "HTML",
			options: "<div class='text-ink-gray-7'>" + esc(draft.to) + " · " + esc(draft.subject) + "</div>" }]
		if (draft.handoff) fields.push({ fieldname: "ffl_handoff", fieldtype: "HTML",
			options: "<div class='text-ink-red-4'>" + esc(__("Needs a person: {0}", [draft.handoff])) + "</div>" })
		fields.push({ fieldname: "content", fieldtype: "Text Editor", label: "" })
		formDialog({
			title: __("Reply to {0}", [this.doc.first_name || draft.to]),
			fields,
			defaults: { content: draft.content_html + (draft.signature_html || "") },
			actions: [
				{ label: __("Send"), variant: "solid", onClick: async ({ data, close }) => {
					try {
						await call("frappe.core.doctype.communication.email.make", {
							recipients: draft.to, cc: "", bcc: "", subject: draft.subject, content: data.content,
							doctype: "%(dt)s", name: this.doc.name, send_email: 1, sender: draft.sender || undefined,
							attachments: [], in_reply_to: draft.in_reply_to || undefined,
						})
					} catch (e) {
						toast.error(this.fflMessage(e))
						return
					}
					close()
					toast.success(__("Sent."))
				} },
				{ label: __("Close"), onClick: ({ close }) => close() },
			],
		})
	}
	async fflInsertPrice() {
		try {
			const found = await call("ffl_core.api.crm.price_options", { doctype: "%(dt)s", name: this.doc.name })
			const picked = found.options.length === 1 ? found.options[0] : await this.fflPickPrice(found)
			if (!picked) return
			const line = await call("ffl_core.api.crm.price_text", {
				doctype: "%(dt)s", name: this.doc.name, item_code: picked.item_code, serial_no: picked.serial_no || undefined })
			const editor = this.fflComposer()
			if (editor) {
				editor.chain().focus().insertContent({ type: "text", text: line.text }).run()
			} else if (navigator.clipboard) {
				navigator.clipboard.writeText(line.text).then(
					() => toast.success(__("Copied: {0}", [line.text])), () => toast(line.text))
			} else {
				toast(line.text)
			}
		} catch (e) {
			toast.error(this.fflMessage(e))
		}
	}
	fflPickPrice(found) {
		const fields = []
		if (found.options.length) fields.push({ fieldname: "choice", fieldtype: "Select", label: "",
			options: found.options.map((o, i) => ({ label: o.label, value: String(i) })) })
		fields.push({ fieldname: "other_item", fieldtype: "Link", options: "Item", label: __("Other item") })
		return new Promise((resolve) => {
			formDialog({
				title: __("Insert price"),
				fields,
				defaults: found.options.length ? { choice: String(found.default || 0) } : {},
				actions: [{ label: __("Insert"), variant: "solid", onClick: ({ data, close }) => {
					const picked = data.other_item ? { item_code: data.other_item }
						: found.options[Number(data.choice ?? found.default ?? 0)]
					close()
					resolve(picked || null)
				} }],
				onCancel: () => resolve(null),
			})
		})
	}
}
"""


def reply_script_js(doctype: str) -> str:
	"""The Draft reply / Insert price script of the Lead or Deal page, with its Memory card and the card's
	Edit memory / Update memory / Clear memory in the "..." menu (crm/memory.py)."""
	return _REPLY_SCRIPT_TEMPLATE % {"cls": doctype.replace(" ", ""), "dt": doctype, "card": memory.CARD_FIELD}


REPLY_SCRIPTS = {LEAD_SCRIPT: (LEAD, reply_script_js(LEAD)), DEAL_REPLY_SCRIPT: (DEAL, reply_script_js(DEAL))}
# Where the card sits on the Deal page: after Contacts, as CRM places its own Lost Reason section.
SIDE_PANEL = f"{DEAL}-Side Panel"
CARD_SECTION = "ffl_customer_section"
SELL_SECTION = "ffl_sell_section"

# The customer memory's per-record fields (crm.memory). Same names on Lead and Deal: CRM's conversion
# copies a lead's onto its deal, the summary and the watermark with them.
MEMORY_FIELDS = [{
	# The side panel's Memory card: hidden until the page script has a card to show (crm.memory.card).
	"fieldname": memory.CARD_FIELD,
	"fieldtype": "HTML",
	"label": "Memory",
	"hidden": 1,
}, {
	"fieldname": memory.SUMMARY,
	"fieldtype": "Small Text",
	"label": "Summary",
	"read_only": 1,
	"hidden": 1,
	"no_copy": 1,
}, {
	"fieldname": memory.DUE,
	"fieldtype": "Datetime",
	"label": "Memory update due",
	"read_only": 1,
	"hidden": 1,
	"no_copy": 1,
	"search_index": 1,
}, {
	"fieldname": memory.SEEN,
	"fieldtype": "Datetime",
	"label": "Memory read up to",
	"read_only": 1,
	"hidden": 1,
	"no_copy": 1,
}, {
	"fieldname": memory.FAILING,
	"fieldtype": "Datetime",
	"label": "Memory update failing since",
	"read_only": 1,
	"hidden": 1,
	"no_copy": 1,
}]

CUSTOM_FIELDS = {
	"CRM Deal": [*map(dict, MEMORY_FIELDS), {
		# Won since the last memory update: the next one is told what was bought.
		"fieldname": memory.WON,
		"fieldtype": "Check",
		"label": "Memory: won since last update",
		"read_only": 1,
		"hidden": 1,
		"no_copy": 1,
	}, {
		"fieldname": CARD_FIELD,
		"fieldtype": "HTML",
		"label": "Customer card",
		"insert_after": "lead_name",
	}, {
		# The paid web order that won the deal (crm.web_orders): its total is the deal's value.
		"fieldname": WON_BY,
		"fieldtype": "Link",
		"options": WEB_ORDER,
		"label": "Won by web order",
		"read_only": 1,
		"no_copy": 1,
		"search_index": 1,
		"insert_after": "lead",
	}, *sell_requests.DEAL_FIELDS],
	# The website event an inquiry note was filed for (crm.inquiries): a repeat of it files nothing.
	"FCRM Note": [{
		"fieldname": EVENT_FIELD,
		"fieldtype": "Data",
		"label": "Website event id",
		"read_only": 1,
		"hidden": 1,
		"no_copy": 1,
		"search_index": 1,
		"insert_after": "reference_docname",
	}],
	# Data, as CRM's own Quotation.crm_deal: a deal deleted later must never stop a credit note.
	# Not no_copy: a credit note and an amended invoice stay on the deal.
	"Sales Invoice": [{
		"fieldname": DEAL_FIELD,
		"fieldtype": "Data",
		"label": "CRM Deal",
		"read_only": 1,
		"print_hide": 1,
		"search_index": 1,
		"insert_after": "ffl_staff_note",
	}],
	"CRM Products": [{
		"fieldname": SERIAL_FIELD,
		"fieldtype": "Link",
		"options": "Serial No",
		"label": "Gun (serial number)",
		"description": "For a gun priced one by one: the gun this line is for. Blank quotes the cheapest one in stock.",
		"insert_after": "net_amount",
	}],
	"CRM Lead": [{
		"fieldname": CATEGORY_FIELD,
		"fieldtype": "Link",
		"options": CATEGORY,
		"label": "Mail category",
		"insert_after": "status",
		"in_standard_filter": 1,
	}, {
		# The MsgId of a GunBroker member's conversation: its mail all goes to the one lead (crm.gunbroker).
		"fieldname": MSGID_FIELD,
		"fieldtype": "Data",
		"label": "GunBroker message id",
		"read_only": 1,
		"no_copy": 1,
		"search_index": 1,
		"insert_after": CATEGORY_FIELD,
	}, *map(dict, MEMORY_FIELDS)],
	"Email Account": [{
		"fieldname": AUTO_LEAD,
		"fieldtype": "Check",
		"default": "0",
		"label": "Sync business mail to CRM",
		"description": "A staff mailbox: a customer's mail goes on their open deal, a known lead's on the lead, and mail to a team address from someone new becomes a lead. Other mail is not brought in. Team addresses: Lead Mail Settings.",
		"insert_after": "create_lead_from_incoming_email",
	}],
}


def after_install() -> None:
	ensure()


def after_app_install(app: str) -> None:
	"""hooks after_app_install: Frappe CRM installed on a site that already runs the POS. The deploy
	migrates right after (deploy/prod/auto_deploy.sh), so the product backfill waits for that
	migrate's :func:`ensure`: it must not run against a site whose migrate is still going."""
	if app == "crm":
		ensure(backfill=False)


def ensure(backfill: bool = True) -> dict:
	"""after_install / after_migrate / CRM installed: idempotent. Never raises: a thrown hook aborts
	the whole migrate."""
	if not installed():
		return {"skipped": "crm not installed"}
	try:
		_as_administrator(ensure_mail_account_index)
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Communication email_account index not made", message=frappe.get_traceback())
	try:
		_as_administrator(ensure_custom_fields)
		_as_administrator(ensure_settings)
		_as_administrator(ensure_crm_settings)
		_as_administrator(ensure_crm_currency)
		_as_administrator(ensure_deal_stages)
		_as_administrator(ensure_other_mail)
		_as_administrator(ensure_lead_sources)
		_as_administrator(ensure_deal_properties)
		_as_administrator(ensure_crm_views)
		_as_administrator(ensure_deal_name_columns)
		_as_administrator(ensure_deal_name_filter)
		_as_administrator(ensure_sell_view)
		_as_administrator(ensure_follow_up_status)
		_as_administrator(take_over_lead_flags)
		_as_administrator(ensure_contact_script)
		_as_administrator(ensure_reply_scripts)
		_as_administrator(apply_pos_link)
		_as_administrator(lambda: memory.refresh_layout(on_migrate=True))
		_as_administrator(team.apply)
		if backfill:
			enqueue_backfill()
	except Exception:
		frappe.log_error(title=f"[{TITLE}] setup.ensure failed", message=frappe.get_traceback())
		return {"error": 1}
	return {"ok": 1}


def ensure_mail_account_index() -> bool:
	"""An index on ``Communication.email_account``. Frappe's IMAP pull sets every stored mail of an
	account to uid -1 on the account's first pull or a changed UIDVALIDITY (``check_imap_uidvalidity``),
	with ``WHERE communication_medium = 'Email' AND email_account = …``; with no index that statement
	scans, and under REPEATABLE READ locks, every Communication row and the gaps between them, and the
	pull keeps those locks while it fetches up to 100 mails from the server. Every other mail written
	meanwhile (another mailbox's pull, a sent mail) then waits and fails with a lock wait timeout. With
	the index it locks that account's rows only. Kept by a Property Setter (``search_index``), so
	Frappe's schema sync keeps the index instead of dropping it. Returns whether the index was made.

	lazy: the optimizer may still scan the table for an account holding a large share of all mail (a
	busy team mailbox), so its own first pull still locks wide; upgrade path: none on our side short of
	replacing Frappe's statement."""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	if not cint(frappe.get_meta("Communication").get_field("email_account").get("search_index")):
		make_property_setter("Communication", "email_account", "search_index", "1", "Check",
			validate_fields_for_doctype=False)
		frappe.clear_cache(doctype="Communication")
	if frappe.db.get_column_index("tabCommunication", "email_account", unique=False):
		return False
	frappe.db.add_index("Communication", ["email_account"])   # DDL: commits, as every migrate step does
	return True


def ensure_custom_fields() -> None:
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)


def ensure_settings() -> bool:
	"""Once per site: our own domains from the site's Email Domain records. Remembered with a site
	global, so a manager who empties the list later keeps it empty. Waits for the migrate that
	creates the Single (CRM installed on a site still on the code before it)."""
	if frappe.db.get_global(SEEDED_FLAG) or not frappe.db.exists("DocType", SETTINGS):
		return False
	doc = frappe.get_doc(SETTINGS)
	if not (doc.own_domains or "").strip():
		doc.own_domains = "\n".join(sorted(set(frappe.get_all("Email Domain", pluck="domain_name"))))
	doc.save(ignore_permissions=True)  # our own Single, written by install/migrate as Administrator
	frappe.db.set_global(SEEDED_FLAG, "1")
	return True


def _default_company() -> str | None:
	return frappe.defaults.get_global_default("company") \
		or (frappe.get_all("Company", pluck="name", limit=1) or [None])[0]


def ensure_crm_settings() -> dict:
	"""ERPNext CRM Settings on for this site with CRM's own Product → Item push off
	(``sync_products`` 0: products come from Items; ``crm.product`` pushes the edits a
	manager may make). Returns the values it changed."""
	if not frappe.db.exists("DocType", CRM_SETTINGS):
		return {}
	settings = frappe.get_single(CRM_SETTINGS)
	want = {"enabled": 1, "is_erpnext_in_different_site": 0, "sync_products": 0}
	if not settings.erpnext_company:
		want["erpnext_company"] = _default_company()
	changed = {k: v for k, v in want.items() if v is not None and settings.get(k) != v}
	if changed:
		frappe.db.set_single_value(CRM_SETTINGS, dict(changed))   # it adds `modified` to the dict it gets
		settings = frappe.get_single(CRM_SETTINGS)
	# What CRM's save would set up for a same-site integration, minus the Item permission grant.
	settings.create_custom_fields()
	settings.add_quotation_to_option()
	settings.create_crm_form_script()
	frappe.clear_document_cache(CRM_SETTINGS, CRM_SETTINGS)
	return changed


def ensure_crm_currency() -> str | None:
	"""CRM's own currency is the store's. CRM saves its settings at install, so a site that gets CRM
	before its setup wizard keeps whatever Frappe's default was then. A deal in the store's currency
	then asks a public rate API on every insert (CRM Deal ``update_exchange_rate``); with that API
	unreachable CRM refuses the deal, and a customer's mail stays unrouted. Returns the currency set."""
	company = _default_company()
	currency = company and frappe.db.get_value("Company", company, "default_currency")
	if not currency or frappe.db.get_single_value(FCRM_SETTINGS, "currency") == currency:
		return None
	# Not a save: CRM makes the field read-only once set, and its save re-runs its own setup.
	frappe.db.set_single_value(FCRM_SETTINGS, "currency", currency)
	frappe.clear_document_cache(FCRM_SETTINGS, FCRM_SETTINGS)
	return currency


def ensure_deal_stages() -> bool:
	"""Once per site: CRM's stock deal stages become the ones a gun store uses. Remembered with a site
	global, so a stage a manager adds or renames later stays. A stage a site has already replaced is
	left alone."""
	if frappe.db.get_global(DEAL_STAGES_FLAG):
		return False
	moved = {}
	for old, new in RENAMED_STAGES.items():
		if frappe.db.exists(STATUS, old) and not frappe.db.exists(STATUS, new):
			frappe.rename_doc(STATUS, old, new, force=True)
			moved[old] = new
	for old, new in DROPPED_STAGES.items():
		if frappe.db.exists(STATUS, old) and frappe.db.exists(STATUS, new):
			frappe.db.set_value(DEAL, {"status": old}, "status", new)
			frappe.delete_doc(STATUS, old, ignore_permissions=True)
			moved[old] = new
	for position, stage in enumerate(DEAL_STAGES, 1):
		if frappe.db.exists(STATUS, stage):
			frappe.db.set_value(STATUS, stage, "position", position)
	_move_stages_in_saved_views(moved)
	frappe.db.set_global(DEAL_STAGES_FLAG, "1")
	return True


def _move_stages_in_saved_views(moved: dict) -> None:
	"""A saved Deal view keeps stage names as JSON, in its filter and its kanban columns, where a
	rename does not reach them."""
	def swap(value):
		return [swap(v) for v in value] if isinstance(value, list) else moved.get(value, value)

	views = frappe.get_all("CRM View Settings", filters={"dt": DEAL}, fields=["name", "filters", "kanban_columns"])
	for view in views:
		filters = json.loads(view.filters or "{}")
		if "status" in filters:
			filters["status"] = swap(filters["status"])
		columns = {}   # two old stages can land on one new stage: its column appears once
		for column in json.loads(view.kanban_columns or "[]"):
			column["name"] = swap(column.get("name"))
			columns.setdefault(column["name"], column)
		frappe.db.set_value("CRM View Settings", view.name, {"filters": json.dumps(filters),
			"kanban_columns": json.dumps(list(columns.values()))}, update_modified=False)


def ensure_other_mail() -> None:
	"""The Other mail status always (mail depends on it); the categories and first rules once per site,
	so a manager who removes one keeps it removed."""
	if not frappe.db.exists("CRM Lead Status", OTHER_MAIL):
		frappe.get_doc({"doctype": "CRM Lead Status", "lead_status": OTHER_MAIL, "type": "On Hold", "color": "gray",
			"position": 99}).insert(ignore_permissions=True)
	if frappe.db.get_global(MAIL_SETUP_FLAG) or not frappe.db.exists("DocType", CATEGORY):
		return
	for name in MAIL_CATEGORIES:
		if not frappe.db.exists(CATEGORY, name):
			frappe.get_doc({"doctype": CATEGORY, "category_name": name}).insert(ignore_permissions=True)
	settings = frappe.get_doc(SETTINGS)
	have = {rule.domain for rule in settings.get("category_rules") or []}
	for domain, category in CATEGORY_RULES.items():
		if domain not in have:
			settings.append("category_rules", {"domain": domain, "category": category})
	settings.save(ignore_permissions=True)   # our own Single, written by install/migrate as Administrator
	frappe.db.set_global(MAIL_SETUP_FLAG, "1")


def ensure_lead_sources() -> None:
	"""The sources a lead made by the system carries: the website's inquiry form and GunBroker's
	member questions. Each is made when missing, as a lead cannot name a source that is not there."""
	for source in LEAD_SOURCES:
		if not frappe.db.exists("CRM Lead Source", source):
			frappe.get_doc({"doctype": "CRM Lead Source", "source_name": source}).insert(ignore_permissions=True)


def ensure_crm_views() -> None:
	"""The shared views staff work from (CRM's sidebar lists public views). Once per site: a manager may
	change or delete them. Follow-ups name the closed stages, so a stage added later is in the view."""
	from crm.fcrm.doctype.crm_lead.crm_lead import CRMLead

	if frappe.db.get_global(VIEWS_FLAG):
		return
	closed = lambda doctype: frappe.get_all(doctype, filters={"type": ["in", ["Won", "Lost"]]}, pluck="name")  # noqa: E731
	columns = CRMLead.default_list_data()
	_ensure_view(LEAD, "Leads", OTHER_MAIL, {"status": OTHER_MAIL},
		columns=[{"label": "Mail category", "type": "Link", "key": CATEGORY_FIELD, "options": CATEGORY, "width": "10rem"},
			*columns["columns"]], rows=[*columns["rows"], CATEGORY_FIELD])
	_ensure_view(LEAD, "Leads", FOLLOW_UPS, {"lead_owner": ["in", ["@me", ""]], "communication_status": "Open",
		"status": ["not in", [*closed("CRM Lead Status"), OTHER_MAIL]]}, order_by="modified asc")
	_ensure_view(DEAL, "Deals", FOLLOW_UPS, {"deal_owner": ["in", ["@me", ""]], "communication_status": "Open",
		"status": ["not in", closed(STATUS)]}, order_by="modified asc", **deal_list_data())
	frappe.db.set_global(VIEWS_FLAG, "1")


def ensure_deal_properties() -> list[str]:
	"""CRM Deal's title, quick filter and label as :data:`DEAL_PROPERTIES` say, by Property Setter (it
	outranks CRM's JSON on every upgrade). Only a property that differs is written. Returns those."""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	meta = frappe.get_meta(DEAL)
	changed = []
	for field, prop, value, kind in DEAL_PROPERTIES:
		current = meta.get(prop) if field is None else meta.get_field(field).get(prop)
		if str(current if current is not None else "") != value:
			make_property_setter(DEAL, field, prop, value, kind, for_doctype=field is None)
			changed.append(prop if field is None else f"{field}.{prop}")
	if changed:
		frappe.clear_cache(doctype=DEAL)
	return changed


def deal_list_data() -> dict:
	"""The Deals list's columns and rows: CRM's own, the person's name first."""
	from crm.fcrm.doctype.crm_deal.crm_deal import CRMDeal

	data = CRMDeal.default_list_data()
	return {"columns": [dict(NAME_COLUMN), *data["columns"]], "rows": [*data["rows"], NAME_COLUMN["key"]]}


def ensure_deal_name_columns() -> list[str]:
	"""Once per site: saved Deals list views that show CRM's own columns (the POS's shared views, and
	each user's standard view CRM saves on a filter change) get the name first. A view whose columns
	someone chose, and every view a user made, is left alone. Returns the views changed."""
	if frappe.db.get_global(DEAL_COLUMNS_FLAG):
		return []
	from crm.fcrm.doctype.crm_deal.crm_deal import CRMDeal

	stock = [c["key"] for c in CRMDeal.default_list_data()["columns"]]
	data = deal_list_data()
	changed = []
	for view in frappe.get_all("CRM View Settings", filters={"dt": DEAL, "type": "list"},
			fields=["name", "label", "public", "is_standard", "columns"]):
		if not (view.is_standard or (view.public and view.label == FOLLOW_UPS)):
			continue
		keys = [c.get("key") for c in json.loads(view.columns or "[]")]
		if keys and keys != stock:
			continue
		frappe.db.set_value("CRM View Settings", view.name, {"columns": json.dumps(data["columns"]),
			"rows": json.dumps(data["rows"]), "load_default_columns": 0}, update_modified=False)
		changed.append(view.name)
	frappe.db.set_global(DEAL_COLUMNS_FLAG, "1")
	return changed


def ensure_deal_name_filter() -> bool:
	"""Once per site: the name is the Deals list's first quick filter, as on Leads (CRM keeps the
	site's quick filters in CRM Global Settings, made on its install; a manager may change them later).
	Through CRM's own call, which also marks the field a standard filter. Returns whether it was added."""
	if frappe.db.get_global(DEAL_FILTER_FLAG):
		return False
	from crm.api.doc import get_quick_filters, update_quick_filters

	current = [f["fieldname"] for f in get_quick_filters(DEAL, cached=False)]
	added = NAME_COLUMN["key"] not in current
	if added:
		update_quick_filters(json.dumps([NAME_COLUMN["key"], *current]), json.dumps(current), DEAL)
	frappe.db.set_global(DEAL_FILTER_FLAG, "1")
	return added


def ensure_sell_view() -> None:
	"""Once per site, as the other shared views: the Sell requests list (crm.sell_requests)."""
	if frappe.db.get_global(SELL_VIEW_FLAG):
		return
	field = sell_requests.FIELDS
	columns = [dict(NAME_COLUMN, width="16rem"),
		*({"label": label, "type": "Data", "key": field[label], "width": "9rem"}
			for label in ("Manufacturer", "Model", "Caliber", "Condition")),
		{"label": "Offer", "type": "Currency", "key": sell_requests.OFFER, "align": "right", "width": "8rem"},
		{"label": "Status", "type": "Link", "key": "status", "options": STATUS, "width": "9rem"},
		{"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"}]
	_ensure_view(DEAL, "Deals", SELL_VIEW, {sell_requests.SELL: 1}, columns=columns,
		rows=["name", "currency", *(c["key"] for c in columns)])
	frappe.db.set_global(SELL_VIEW_FLAG, "1")


def ensure_follow_up_status() -> list[str]:
	"""Once per site: CRM's two communication-status switches on (:data:`FOLLOW_UP_SWITCHES`), so a
	sent mail marks a record Replied and a new mail reopens it. Remembered with a site global: a manager
	who turns one off later keeps it off. A switch the installed CRM does not have is skipped. Returns
	the switches it turned on."""
	if frappe.db.get_global(FOLLOW_UPS_FLAG):
		return []
	meta = frappe.get_meta(FCRM_SETTINGS)
	changed = [f for f in FOLLOW_UP_SWITCHES if meta.has_field(f) and not frappe.db.get_single_value(FCRM_SETTINGS, f)]
	if changed:
		frappe.db.set_single_value(FCRM_SETTINGS, {f: 1 for f in changed})
		frappe.clear_document_cache(FCRM_SETTINGS, FCRM_SETTINGS)
	frappe.db.set_global(FOLLOW_UPS_FLAG, "1")
	return changed


def _ensure_view(doctype: str, route: str, label: str, filters: dict, order_by: str = "modified desc",
		columns: list | None = None, rows: list | None = None) -> None:
	found = {"dt": doctype, "label": label, "public": 1}
	if frappe.db.exists("CRM View Settings", found):
		return
	view = frappe.get_doc({"doctype": "CRM View Settings", "dt": doctype, "label": label, "type": "list", "public": 1,
		"route_name": route, "filters": json.dumps(filters), "order_by": order_by,
		"load_default_columns": 0 if columns else 1, "columns": json.dumps(columns or []), "rows": json.dumps(rows or [])})
	view.insert(ignore_permissions=True)
	frappe.db.set_value("CRM View Settings", view.name, "user", "")   # shared views have this, not NULL (CRM's get_views)


def take_over_lead_flags() -> list[str]:
	"""Every Email Account with CRM's own lead switch on gets ours instead (CRM's would make a
	lead for a customer we already know)."""
	if not frappe.get_meta("Email Account").has_field("create_lead_from_incoming_email"):
		return []
	names = frappe.get_all("Email Account", filters={"create_lead_from_incoming_email": 1}, pluck="name")
	for name in names:
		frappe.db.set_value("Email Account", name, {AUTO_LEAD: 1, "create_lead_from_incoming_email": 0},
			update_modified=False)
	return names


def ensure_contact_script() -> None:
	"""The Contact page's Email button (CRM runs only enabled scripts)."""
	if not frappe.db.exists("DocType", "CRM Form Script"):
		return
	if not frappe.db.exists("CRM Form Script", CONTACT_SCRIPT):
		frappe.get_doc({"doctype": "CRM Form Script", "name": CONTACT_SCRIPT, "dt": "Contact", "view": "Form",
			"script": CONTACT_SCRIPT_JS, "enabled": 1}).insert(ignore_permissions=True)
		return
	current = frappe.db.get_value("CRM Form Script", CONTACT_SCRIPT, ["script", "enabled"], as_dict=True)
	if current.script != CONTACT_SCRIPT_JS or not current.enabled:
		frappe.db.set_value("CRM Form Script", CONTACT_SCRIPT, {"script": CONTACT_SCRIPT_JS, "enabled": 1})


def ensure_reply_scripts() -> None:
	"""The Lead and Deal pages' Draft reply and Insert price buttons: on always, each button shown by
	what ``reply_draft_ready`` answers (an extension drafting, the POS link on)."""
	if not frappe.db.exists("DocType", "CRM Form Script"):
		return
	for name, (doctype, script) in REPLY_SCRIPTS.items():
		want = {"script": script, "enabled": 1}
		if not frappe.db.exists("CRM Form Script", name):
			frappe.get_doc({"doctype": "CRM Form Script", "name": name, "dt": doctype, "view": "Form", **want}
				).insert(ignore_permissions=True)
		elif frappe.db.get_value("CRM Form Script", name, list(want), as_dict=True) != want:
			frappe.db.set_value("CRM Form Script", name, want)


def apply_pos_link() -> None:
	"""The Deal page follows Lead Mail Settings › crm_pos_link: the script runs and the card's section
	shows only while it is on. Migrate and that page's save, by whoever may change the settings."""
	if not frappe.db.exists("DocType", "CRM Form Script"):
		return
	from ffl_core.crm.holds import holds_on

	on = pos_link_on()
	want = {"script": deal_script_js(holds_on()), "enabled": 1 if on else 0}
	if not frappe.db.exists("CRM Form Script", DEAL_SCRIPT):
		frappe.get_doc({"doctype": "CRM Form Script", "name": DEAL_SCRIPT, "dt": DEAL, "view": "Form", **want}
			).insert(ignore_permissions=True)
	elif frappe.db.get_value("CRM Form Script", DEAL_SCRIPT, list(want), as_dict=True) != want:
		frappe.db.set_value("CRM Form Script", DEAL_SCRIPT, want)
	_place_card_section(on)


def _place_card_section(on: bool) -> None:
	if not frappe.db.exists("CRM Fields Layout", SIDE_PANEL):
		return
	layout = frappe.get_doc("CRM Fields Layout", SIDE_PANEL)
	sections = json.loads(layout.layout or "[]")
	before = json.dumps(sections)
	# The Gun offered card (shown on a sell request only, by its field's depends_on) above the customer's.
	for name, label, column, field in ((CARD_SECTION, "Customer", "ffl_customer_column", CARD_FIELD),
			(SELL_SECTION, "Gun offered", "ffl_sell_column", sell_requests.CARD)):
		present = any(section.get("name") == name for section in sections)
		if on and not present:
			at = 1 if sections and sections[0].get("name") == "contacts_section" else 0
			sections.insert(at, {"name": name, "label": label, "opened": True,
				"columns": [{"name": column, "fields": [field]}]})
		elif not on and present:
			sections = [section for section in sections if section.get("name") != name]
	if json.dumps(sections) == before:
		return
	layout.layout = json.dumps(sections)
	layout.save(ignore_permissions=True)


def enqueue_backfill() -> bool:
	"""Once per site, on the long queue: every Item becomes a CRM Product."""
	if frappe.db.get_global(BACKFILL_FLAG):
		return False
	frappe.enqueue("ffl_core.crm.item.backfill_products", queue="long", job_id=BACKFILL_JOB,
		deduplicate=True, enqueue_after_commit=True)
	return True


def _as_administrator(fn):
	prev = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return fn()
	finally:
		frappe.set_user(prev)
