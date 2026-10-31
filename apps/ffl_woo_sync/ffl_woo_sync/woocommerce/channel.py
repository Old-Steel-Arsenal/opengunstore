"""What WooCommerce does when a gun leaves through some OTHER sales channel.

The registrant half of the `serial_channel_exit` hook. The inversion is the
same one `distributor_listing_push` uses and exists for the same reason:
ffl_integrations owns the GunBroker channel and CALLS the hook; this app owns
the WooCommerce storefront and answers it. Neither app imports the other —
the argument list is the entire contract.

    fn(serial_no: str, *, channel: str, reason: str) -> None

`channel` is who took the gun ("gunbroker"). `reason` is what happened there:

    "sold"      a buyer paid. The gun is gone.
    "reserved"  probably sold — GunBroker has stopped serving the listing and
                the same SKU shows in its sold list, but the order has not been
                polled in yet. Treated exactly like "sold" HERE: the whole point
                of the flag is that we do not know, and the safe answer to "do
                we keep this buyable?" under uncertainty is no.
    "ended"     the listing was withdrawn without a sale.

All three produce the same action, and deliberately so. This function's job is
to make the gun unbuyable on WooCommerce, and the difference between the three
is about the OTHER channel's paperwork, not about whether the storefront may
keep selling it. What differs is what happens LATER: a "sold" gun goes on to be
disposed and picks up its SOLD-archive page through the ordinary exit routing,
which is a different code path (`sync.route_serial_exit`) reached from the
stock ledger.

Why this is not just `route_serial_exit`: nothing has moved in the stock ledger
yet. The gun is still Active, still in its warehouse — a GunBroker sale is a
reservation until somebody disposes it, exactly as a paid web order is. The
exit router answers "this gun left the shelf, what happens to its page?"; this
answers "this gun is spoken for, stop selling it", which is the delist half
only.
"""
from __future__ import annotations

import frappe

from ffl_core.reservations import CHANNEL_WOO
from ffl_woo_sync.woocommerce.client import SITES
from ffl_woo_sync.woocommerce.sync import _f


def serial_channel_exit(serial_no: str, *, channel: str, reason: str) -> None:
	"""Take one gun off the WooCommerce storefront because another channel has it.

	Args:
	    serial_no: The Serial No that has left through another channel.
	    channel: Which channel took it (e.g. "gunbroker").
	    reason: "sold" / "reserved" / "ended" — see the module docstring.

	Enqueues rather than pushing inline, and after commit: the caller is inside
	the transaction that writes the reservation, and a WooCommerce round-trip
	inside it would hold that transaction open across the network. `job_id` +
	`deduplicate` so a GunBroker order re-polled three times (its `lastModified`
	moves on payment, on FFL receipt, on shipping) queues one delist per store,
	not three.

	Per-serial product on a store → delist that one product. No per-serial
	product but the parent Item is listed → re-push the Item's stock count
	instead: the Item-level product stands for several interchangeable guns, and
	delisting it would pull every OTHER unit off sale too. Neither → nothing is
	enqueued, so an unlisted gun costs no queue job and no Error Log line.
	"""
	serial_no = (serial_no or "").strip()
	if not serial_no:
		return
	row = frappe.db.get_value(
		"Serial No", serial_no,
		["item_code", *(_f(site, key) for site in SITES for key in ("product_id", "delisted"))],
		as_dict=True)
	if not row:
		return
	item_code = (row.get("item_code") or "").strip()
	for site in SITES:
		if row.get(_f(site, "product_id")):
			# Record that THIS exit is what hides it, so serial_channel_return undoes
			# only its own delist. A gun the owner had already hidden stays unmarked.
			# Never cleared here: a re-polled order finds its own delist already set.
			if not row.get(_f(site, "delisted")):
				frappe.db.set_value("Serial No", serial_no, _f(site, "channel_delisted"), 1,
					update_modified=False)
			frappe.enqueue(
				"ffl_woo_sync.woocommerce.tasks.delist_serial_task",
				queue="short", enqueue_after_commit=True,
				job_id=f"chan-exit:{serial_no}:{site}", deduplicate=True,
				serial_no=serial_no, site=site)
		elif item_code and frappe.db.get_value("Item", item_code, _f(site, "product_id")):
			frappe.enqueue(
				"ffl_woo_sync.woocommerce.tasks.push_stock_task",
				queue="short", enqueue_after_commit=True,
				job_id=f"chan-exit-stock:{item_code}:{site}", deduplicate=True,
				item_code=item_code, site=site)
	frappe.logger("ffl_woo_sync").info(
		f"serial_channel_exit: {serial_no} left via {channel} ({reason})")


def serial_channel_return(serial_no: str, *, channel: str, reason: str) -> None:
	"""Put a gun back on the WooCommerce storefront: the other channel let it go.

	The undo of :func:`serial_channel_exit`. GunBroker released its hold (the
	order was cancelled before invoicing, a unit was dropped or reassigned), so
	the gun is on the shelf again and the page the exit took down — draft, stock
	0, ``{site}_delisted`` set — goes back up, unless a person had hidden it.

	Args:
	    serial_no: The Serial No the other channel released.
	    channel: Which channel released it (e.g. "gunbroker").
	    reason: Always "released" today; kept for the shared contract.

	Same door the web store's own cancellation uses
	(``webhook._enqueue_serial_or_item_push``), so a gun with no price, one in
	custody, or one another channel still holds is refused by ``push_serial``'s
	own guards. Enqueued after commit, so the push reads the released hold.

	``relist`` only where the exit itself raised ``{site}_delisted``
	(``{site}_channel_delisted``): a gun the owner hid before GunBroker took it
	stays hidden. The marker is cleared by the relisting push, not here, so a
	push refused this time leaves it for the next release.
	"""
	from ffl_woo_sync.woocommerce.webhook import _enqueue_serial_or_item_push

	serial_no = (serial_no or "").strip()
	if not serial_no:
		return
	row = frappe.db.get_value(
		"Serial No", serial_no, [_f(site, "channel_delisted") for site in SITES],
		as_dict=True) or {}
	for site in SITES:
		_enqueue_serial_or_item_push(
			serial_no, site=site, relist=bool(row.get(_f(site, "channel_delisted"))))
	frappe.logger("ffl_woo_sync").info(
		f"serial_channel_return: {serial_no} released by {channel} ({reason})")


def channel_commitments_changed(item_code: str, *, source: str) -> None:
	"""Re-push this Item's stock because ANOTHER channel's committed units moved.

	The web store's count is the shelf less what the other channels have sold and
	not yet shipped (``mapping.local_stock_for_item``): a GunBroker order for the
	last box of ammunition, or its claim on the last of a uniform-price model,
	changes the number the storefront must show — and posts no stock ledger entry
	that would say so.

	Args:
	    item_code: The Item whose commitments moved.
	    source: Who moved them. The web store's own checkout and cancellation
	        already push what they changed, so ``woo`` is not echoed back.

	Queues one push per store the Item is listed on, after commit. An Item listed
	nowhere enqueues nothing. Cheap by design: it runs inside the transaction that
	records the other channel's sale.
	"""
	if source == CHANNEL_WOO:
		return
	for site in SITES:
		if frappe.db.get_value("Item", item_code, _f(site, "product_id")):
			# lazy: no job_id/dedup — a push re-reads the count when it RUNS, so a
			# duplicate is a wasted call, never a stale number, and Frappe counts a
			# started job as already queued (a deduped enqueue could drop the one
			# that matters). Upgrade path: dedup per (item, site) if a burst of
			# GunBroker orders ever makes the duplicates measurable.
			frappe.enqueue(
				"ffl_woo_sync.woocommerce.tasks.push_stock_task",
				queue="short", enqueue_after_commit=True,
				item_code=item_code, site=site)
