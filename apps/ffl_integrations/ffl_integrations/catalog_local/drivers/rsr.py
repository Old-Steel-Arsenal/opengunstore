"""RSR Group for the local catalog: FTPS feed files → the dc_* store.

A port of the Data Service's RSR driver and its fetch layer. Lanes, in
execution order (the tick enqueues them in this order on the one catalog
worker):

  catalog       heavy  the 77-column inventory file, when its last success is
                       older than CATALOG_STALE (2 h). It reads the deleted list
                       first and loads a listed SKU as Deleted; an unreadable
                       list: the SKUs already Deleted stand in
  deleted              rsrdeletedinv.txt, straight after the catalog in the same
                       job, from the list the catalog read, or its own download
                       when the catalog could not read it; never after a failed
                       catalog. Alone when forced, or on the next tick after it
                       failed behind a catalog success
  quantity             IM-QTY-CSV.csv, every tick
  restrictions  heavy  rsr-ship-restrictions.txt, when its last success is older
                       than RESTRICTIONS_STALE (2 h); an unchanged file (same
                       content hash) is not even parsed. A file far smaller than
                       the last one loaded is refused unless the run is forced
                       (from bench: never Sync now), which loads it with a warning
  case_price           the full-case price (per unit) and case size of every SKU,
                       from Direct Connect get-items on the dealer account (no feed
                       file carries them), when its last success is older than
                       CASE_PRICE_STALE (24 h). Skipped — no run recorded — while the
                       API is off or has no dealer login, and inside RSR's 3–5am ET
                       maintenance window; it writes only once every page arrived

Credentials: RSR Settings › Catalog feed (ftp_host, ftp_port, ftp_username,
ftp_password, key_dealer, ftps_skip_verify). The server's TLS certificate is
verified unless that last box is ticked. A key dealer reads every file from ``keydealer/``
(the catalog under its own name); everyone else from ``ftpdownloads/``.

Downloads stream to a temp file (after a free-disk check against the file's
size) and are deleted after the lane; the parsers stream from it. Transport
failures are ``sync.FeedUnavailable`` / ``FeedAuthError``, layout failures
``FeedFormatError``; no message carries a credential.
"""
from __future__ import annotations

import ftplib
import os
import shutil
import ssl
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from ftplib import FTP_TLS

import frappe

from ffl_integrations.catalog_local import store, sync
from ffl_integrations.catalog_local.drivers import rsr_feed
from ffl_integrations.catalog_local.sync import FeedAuthError, FeedUnavailable

CODE = "rsr"
SETTINGS = "RSR Settings"
DEFAULT_HOST = "ftp.rsrgroup.com"
DEFAULT_PORT = 2222
CONNECT_TIMEOUT_SECONDS = 60
SKIP_VERIFY_LABEL = "Skip TLS certificate check"  # RSR Settings.ftps_skip_verify

CATALOG_STALE = timedelta(hours=2)
RESTRICTIONS_STALE = timedelta(hours=2)
CASE_PRICE_STALE = timedelta(hours=24)
# lazy: get-items' page cap is not documented; 1000 rows a call is ~70 calls for the
# whole book. Advance by what each page returned, so a lower server cap still pages.
CASE_PAGE_SIZE = 1000
CASE_MAX_PAGES = 1000  # a server that keeps answering short pages cannot loop forever

FILES = {
	"catalog": "rsrinventory-new.txt",
	"quantity": "IM-QTY-CSV.csv",
	"deleted": "rsrdeletedinv.txt",
	"restrictions": "rsr-ship-restrictions.txt",
}
CATALOG_FILE_KEYDEALER = "rsrinventory-keydlr-new.txt"

# Free disk a download needs beyond the file itself; a server that will not
# say the size is assumed to send UNKNOWN_SIZE_BYTES.
DISK_HEADROOM_BYTES = 256 << 20
UNKNOWN_SIZE_BYTES = 512 << 20


@dataclass(frozen=True)
class FeedCredentials:
	host: str
	port: int
	username: str
	# Out of the repr, and underscored: the Error Log's traceback formatter
	# ignores a generated dataclass repr and prints every public attribute.
	_password: str = field(repr=False)
	key_dealer: bool = False
	skip_tls_verify: bool = False

	@property
	def password(self) -> str:
		return self._password

	@property
	def feed_dir(self) -> str:
		# A key-dealer login has no ftpdownloads/ at all (the Data Service, verified 2026-06-14).
		return "keydealer" if self.key_dealer else "ftpdownloads"

	def filename(self, kind: str) -> str:
		if kind == "catalog" and self.key_dealer:
			return CATALOG_FILE_KEYDEALER
		return FILES[kind]


def feed_username() -> str:
	return (frappe.db.get_single_value(SETTINGS, "ftp_username") or "").strip()


def credentials() -> FeedCredentials | None:
	"""RSR Settings' feed login, or None when the username or password is blank.
	Read fresh (not cached): a re-saved password takes effect on the next lane. A
	password that is saved but cannot be decrypted (the site's encryption key
	changed) raises FeedAuthError — the lane records it, never a silent skip."""
	s = frappe.get_single(SETTINGS)
	username = (s.get("ftp_username") or "").strip()
	if not username:
		return None
	password = _feed_password(s)
	if not password:
		return None
	return FeedCredentials(host=(s.get("ftp_host") or "").strip() or DEFAULT_HOST,
		port=int(s.get("ftp_port") or DEFAULT_PORT), username=username, _password=password,
		key_dealer=bool(s.get("key_dealer")), skip_tls_verify=bool(s.get("ftps_skip_verify")))


UNDECRYPTABLE = ("the RSR feed password in RSR Settings cannot be decrypted (the site's "
	"encryption key changed?); enter it again")


def quiet_password(doc, fieldname: str):
	"""``doc.get_password(fieldname, raise_exception=False)`` without Frappe's own
	"Encryption key is invalid" message: a password that cannot be decrypted reads
	as None and adds nothing to the request's message log, so the caller's own
	message is the only one shown."""
	muted = frappe.flags.mute_messages
	frappe.flags.mute_messages = True  # msgprint raises without logging while this is set
	try:
		return doc.get_password(fieldname, raise_exception=False)
	finally:
		frappe.flags.mute_messages = muted


def saved_password(settings, doctype: str, fieldname: str, undecryptable: str) -> str:
	"""The saved password of ``doctype.fieldname``; "" when none is saved. Frappe's
	``get_password(raise_exception=False)`` answers None for both "nothing saved"
	and "saved but undecryptable", so a None is checked against ``__Auth``: a
	stored value there means the decrypt failed, and that raises
	FeedAuthError(undecryptable). It is read through quiet_password, so Test
	Connection shows that message alone, not Frappe's decrypt message next to it."""
	from frappe.utils.password import Auth

	password = quiet_password(settings, fieldname)
	if password:
		return password
	saved = (frappe.qb.from_(Auth).select(Auth.password)
		.where((Auth.doctype == doctype) & (Auth.name == doctype)
			& (Auth.fieldname == fieldname) & (Auth.encrypted == 1))
		.limit(1)).run()
	if saved and saved[0][0]:
		raise FeedAuthError(undecryptable)
	return ""


def _feed_password(settings) -> str:
	return saved_password(settings, SETTINGS, "ftp_password", UNDECRYPTABLE)


def tls_context(creds: FeedCredentials) -> ssl.SSLContext:
	"""The server must prove it is the host we dialled: certificate chain and
	hostname checked. RSR Settings' "Skip TLS certificate check" (off by
	default) turns that off for one broken chain, with a warning per use."""
	context = ssl.create_default_context()
	if creds.skip_tls_verify:
		_log().warning("RSR FTPS: TLS certificate check skipped for %s (RSR Settings); the "
			"feed login is exposed to anyone in the path", creds.host)
		context.check_hostname = False
		context.verify_mode = ssl.CERT_NONE
	return context


def _log():
	return frappe.logger("catalog_local", allow_site=True)


# ------------------------------------------------------------------ transport

def _feed_error(exc: BaseException, what: str) -> FeedUnavailable:
	"""An ftplib / socket / TLS failure → the sync error class. The server's
	reply text is kept (it never echoes a password); 530/532 is an auth failure."""
	text = str(exc).strip()[:200]
	if isinstance(exc, ssl.SSLCertVerificationError):
		reason = getattr(exc, "verify_message", None) or text
		return FeedUnavailable(f"RSR FTPS {what} failed: the server's TLS certificate did not "
			f"verify ({reason}). If you trust the network path to the feed server, tick "
			f"\"{SKIP_VERIFY_LABEL}\" in RSR Settings › Catalog feed")
	if isinstance(exc, ftplib.error_perm):
		if text[:3] in ("530", "532"):
			return FeedAuthError(f"RSR FTPS rejected the login ({text})")
		return FeedUnavailable(f"RSR FTPS {what} failed: {text}")
	return FeedUnavailable(f"RSR FTPS {what} failed: {type(exc).__name__}: {text}")


def _quit(ftps) -> None:
	try:
		ftps.quit()
	except Exception:  # noqa: BLE001 — the session is over either way
		try:
			ftps.close()
		except Exception:  # noqa: BLE001
			pass


def _unlink(path) -> None:
	if path:
		try:
			os.unlink(path)
		except OSError:
			pass


def _remote_size(ftps, name: str) -> int | None:
	"""The file's size in bytes, or None when the server will not say."""
	try:
		ftps.voidcmd("TYPE I")  # SIZE is refused in ASCII mode
		return ftps.size(name)
	except (ftplib.Error, ValueError, TypeError):
		return None


def check_disk(directory: str, size: int | None, name: str) -> None:
	"""Refuse a download the temp directory has no room for (the file plus
	DISK_HEADROOM_BYTES) — a full disk would take the database down with it."""
	need = (size if size is not None else UNKNOWN_SIZE_BYTES) + DISK_HEADROOM_BYTES
	free = shutil.disk_usage(directory).free
	if free < need:
		raise FeedUnavailable(f"not enough free disk to download {name}: {free >> 20} MiB free "
			f"in {directory}, {need >> 20} MiB needed")


class FtpsTransport:
	"""RSR's feed server: explicit FTPS (AUTH TLS on the control channel at
	login, then PROT P for data), passive mode — the Data Service's transport, with the
	server's certificate verified (``tls_context``)."""

	def __init__(self, creds: FeedCredentials | None = None):
		self._creds = creds

	def credentials(self) -> FeedCredentials:
		creds = self._creds or credentials()
		if creds is None:
			raise FeedUnavailable("RSR Settings › Catalog feed is missing the FTP username "
				"or password")
		return creds

	def _open(self, creds: FeedCredentials):
		ftps = FTP_TLS(context=tls_context(creds), timeout=CONNECT_TIMEOUT_SECONDS)
		try:
			ftps.connect(host=creds.host, port=creds.port, timeout=CONNECT_TIMEOUT_SECONDS)
			ftps.login(user=creds.username, passwd=creds.password)
			ftps.prot_p()
			ftps.set_pasv(True)
		except ftplib.all_errors as exc:
			_quit(ftps)
			raise _feed_error(exc, f"login to {creds.host}:{creds.port}") from None
		return ftps

	def download(self, kind: str) -> str:
		"""Fetch one feed file (``kind`` is a FILES key) to a temp path; the
		caller deletes it. A transfer shorter than the size the server announced
		is refused."""
		creds = self.credentials()
		name = creds.filename(kind)
		directory = tempfile.gettempdir()
		ftps = self._open(creds)
		path = None
		try:
			ftps.cwd(creds.feed_dir)
			size = _remote_size(ftps, name)
			check_disk(directory, size, name)
			fd, path = tempfile.mkstemp(prefix="rsr-feed-", suffix="-" + name, dir=directory)
			with os.fdopen(fd, "wb") as fh:
				ftps.retrbinary(f"RETR {name}", fh.write, blocksize=1 << 16)
			got = os.path.getsize(path)
			if size is not None and got != size:
				raise FeedUnavailable(f"RSR FTPS download of {name} was cut short: {got} of "
					f"{size} bytes")
			return path
		except ftplib.all_errors as exc:
			_unlink(path)
			raise _feed_error(exc, f"download of {name}") from None
		except BaseException:
			_unlink(path)
			raise
		finally:
			_quit(ftps)

	def probe(self) -> dict:
		"""Log in and list the feed directory — Test Connection. Downloads nothing."""
		creds = self.credentials()
		ftps = self._open(creds)
		try:
			ftps.cwd(creds.feed_dir)
			names = ftps.nlst()
		except ftplib.all_errors as exc:
			raise _feed_error(exc, f"listing of {creds.feed_dir}/") from None
		finally:
			_quit(ftps)
		return {"directory": creds.feed_dir, "files": len(names)}


# ------------------------------------------------------------------ driver

class RsrDriver(sync.Driver):
	label = "RSR"
	feed_stale = {"quantity": 5400, "catalog": 21600, "deleted": 21600, "restrictions": 21600,
		"case_price": 172800}

	def __init__(self, code: str = CODE, transport=None, items_client=None):
		self.code = code
		self.transport = transport  # None: FtpsTransport over RSR Settings (tests pass a fake)
		# The Direct Connect client the case_price lane pages get-items with. None: the
		# dealer account in RSR Settings — unless a feed transport was passed in (a
		# test, a bench probe), which never reaches the live API by default.
		self.items_client = items_client
		self._lanes = (
			sync.Lane("catalog", gate=lambda run: run.due("catalog", CATALOG_STALE),
				run=self._catalog, heavy=True, timeout=sync.HEAVY_TIMEOUT_SECONDS),
			sync.Lane("deleted", gate=self._deleted_due, run=self._deleted, follows="catalog"),
			sync.Lane("quantity", gate=lambda run: True, run=self._quantity),
			sync.Lane("restrictions", gate=lambda run: run.due("restrictions", RESTRICTIONS_STALE),
				run=self._restrictions, heavy=True, timeout=sync.HEAVY_TIMEOUT_SECONDS),
			# Not heavy: network-bound and one small UPDATE, and a lane that is never
			# attempted (the API not set up) must not pin RSR first in the heavy slot's
			# fairness order. The longer timeout is for paging the whole book.
			sync.Lane("case_price", gate=self._case_price_due, run=self._case_price,
				timeout=sync.HEAVY_TIMEOUT_SECONDS),
		)

	def lanes(self):
		return self._lanes

	def configured(self) -> bool:
		"""An enabled Distributor buys through RSR and a feed username is saved.
		The password is not read here: one that is missing or cannot be decrypted
		fails the lanes out loud (a failed run) instead of skipping them."""
		return super().configured() and bool(feed_username())

	def _fetch(self, kind: str, handler) -> dict:
		path = (self.transport or FtpsTransport()).download(kind)
		try:
			return handler(path)
		finally:
			_unlink(path)

	def _deleted_list(self, run) -> frozenset:
		"""RSR's deleted list, read at the start of the catalog job. When it cannot be
		read, the SKUs the deleted lane already retired here (Deleted and discontinued)
		stand in for it: the catalog still loads and revives none of them, and the
		deleted lane that follows downloads the file itself and records the failure
		under its own name."""
		try:
			skus = self._fetch("deleted", lambda p: frozenset(filter(None, (
				store.norm_sku(s) for s in rsr_feed.iter_deleted(p)))))
		except (FeedUnavailable, sync.FeedFormatError, OSError) as exc:
			_log().warning("catalog %s: the deleted list could not be read (%s: %s); the SKUs "
				"already Deleted stay Deleted", self.code, type(exc).__name__, exc)
			return frozenset(frappe.db.sql_list("SELECT sku FROM dc_offer WHERE distributor = %s "
				f"AND discontinued = 1 AND {store.deleted_sql()}", (self.code,)))
		run.state["deleted_skus"] = skus
		return skus

	def _catalog(self, run) -> dict:
		# The deleted list first, in this job: a SKU on it is loaded as Deleted, so the
		# catalog never revives one for the deleted lane to flag again (two change_seq
		# bumps and a window where it is orderable). Every column, qty too, on every
		# run (the Data Service's RSR lane): the quantity lane moves qty in between.
		# lazy: the list is held as a set (tens of thousands of SKUs, a few MB); stage it in a temp table if it grows far past that
		deleted = self._deleted_list(run)
		out = self._fetch("catalog", lambda p: store.load_catalog(
			rsr_feed.iter_catalog(p, deleted=deleted), distributor=self.code))
		run.state["catalog_ran"] = True  # a success or a lock skip; a failure raised above
		return out

	def _quantity(self, run) -> dict:
		return self._fetch("quantity", lambda p: store.apply_quantity(
			rsr_feed.iter_quantity(p), distributor=self.code))

	def _deleted_due(self, run) -> bool:
		"""Due when the gates are open (a forced run or Sync now), when this job's catalog ran (a success or a lock skip), or
		- the retry for a deleted run that failed - once a catalog success is newer
		than the last deleted success (the next tick queues it alone)."""
		if run.tick.gates_open or run.state.get("catalog_ran"):
			return True
		catalog = sync.last_success(run.source("catalog"))
		if catalog is None:
			return False
		deleted = sync.last_success(run.source("deleted"))
		return deleted is None or catalog > deleted

	def _deleted(self, run) -> dict:
		skus = run.state.get("deleted_skus")
		if skus is not None:  # this job's catalog lane read the list
			return store.apply_deleted(skus, distributor=self.code)
		return self._fetch("deleted", lambda p: store.apply_deleted(
			rsr_feed.iter_deleted(p), distributor=self.code))

	def _restrictions(self, run) -> dict:
		# Only a forced run from bench overrides the shrink guard; Sync now (skip_schedule) never does.
		return self._fetch("restrictions", lambda p: store.load_restrictions(
			rsr_feed.iter_restrictions(p), rsr_feed.file_content_hash(p), distributor=self.code,
			mode="snapshot", force=run.tick.force))

	# ------------------------------------------------------------ case prices

	def _api_client(self):
		"""The get-items client, or None when the lane has no API to read: Direct
		Connect off, or no dealer login saved."""
		if self.items_client is not None or self.transport is not None:
			return self.items_client
		from ffl_integrations.rsr.api_client import RSRClient, RSRConfig

		config = RSRConfig.from_settings()
		if not (config.api_enabled and config.dealer_account and config.dealer_password):
			return None
		return RSRClient(config)

	def _case_price_due(self, run) -> bool:
		"""Not configured or inside the maintenance window: skipped, not failed (no run
		recorded; the next tick asks again). Otherwise the usual 24-hour gate."""
		client = self._api_client()
		if client is None or client._in_maintenance_window():
			return False
		run.state["items_client"] = client
		return run.due("case_price", CASE_PRICE_STALE)

	def _case_price(self, run) -> dict:
		client = run.state.get("items_client") or self._api_client()
		if client is None:  # the API was switched off after the gate
			raise FeedUnavailable("RSR Direct Connect is off or has no dealer login (RSR Settings)")
		# Every page first, then one write: a run cut short (the maintenance window
		# starting mid-way, a network error) writes nothing.
		rows = fetch_case_prices(client)
		return store.apply_case_prices(rows, distributor=self.code)


def _case_money(value) -> float:
	"""A get-items money string ("7.83", maybe "$7.83") -> float; blank or
	unreadable reads as 0 = no case price (the line then keeps its unit cost)."""
	try:
		return float(str(value if value is not None else "").replace("$", "").replace(",", "").strip() or 0)
	except ValueError:
		return 0.0


def _case_units(value) -> int:
	try:
		return int(float(str(value if value is not None else "").strip() or 0))
	except ValueError:
		return 0


def case_price_row(item: dict) -> tuple:
	"""One get-items item -> ``(sku, case_cost, units_per_case)``. DealerCasePrice is
	the per-unit price of a full case (live 2026-10-28: a box of 20 at DealerPrice 7.99
	is DealerCasePrice 7.83 with UnitsPerCase 50), not the case total; UnitsPerCase
	"0" or a missing price means the SKU is not sold by the case."""
	return (str(item.get("SKU") or "").strip(), _case_money(item.get("DealerCasePrice")),
		_case_units(item.get("UnitsPerCase")))


def fetch_case_prices(client) -> list:
	"""Page get-items (dealer account) through the whole book -> case_price_row per
	item. Raises FeedUnavailable on an API error and FeedFormatError when the pages
	do not add up to TotalFound (an empty book included), so a partial read never
	reaches the store."""
	from ffl_integrations.rsr.api_client import RSRError

	rows, offset, total = [], 0, None
	for _page in range(CASE_MAX_PAGES):
		try:
			data = client.get_items({"Limit": CASE_PAGE_SIZE, "Offset": offset}, account="dealer")
		except RSRError as exc:
			raise FeedUnavailable(f"RSR get-items failed at offset {offset}: {exc}") from None
		data = data if isinstance(data, dict) else {}
		try:
			total = int(data.get("TotalFound"))
		except (TypeError, ValueError):
			raise sync.FeedFormatError("RSR get-items answered without a TotalFound") from None
		items = data.get("Items") if isinstance(data.get("Items"), list) else []
		rows.extend(case_price_row(i) for i in items if isinstance(i, dict))
		offset += len(items)
		if offset >= total or not items:
			break
	if not total or offset < total:
		raise sync.FeedFormatError(f"RSR get-items returned {offset} of {total or 0} items; "
			"case prices left as they were")
	return rows


DRIVER = sync.register(RsrDriver())
