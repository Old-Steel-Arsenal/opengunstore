# Loaded by the prod image's gunicorn (Dockerfile CMD -c). Trust the scheme the
# frontend nginx relays from Caddy — and ONLY X-Forwarded-Proto: gunicorn's
# default secure_scheme_headers also honour X-Forwarded-Ssl / -Protocol, which
# nothing upstream strips, so a client could 400 a request by contradicting them.
# Any source may set it: the backend publishes no port; only the stack's nginx
# reaches it. Without this the request scheme reads http and Frappe's OAuth
# issuer/endpoints (/.well-known/oauth-authorization-server) say http://.
forwarded_allow_ips = "*"
secure_scheme_headers = {"X-FORWARDED-PROTO": "https"}

EXT_DIR = "/home/frappe/frappe-bench/apps-ext"


def post_fork(server, worker):
	"""Make a worker forked after an extension install or upgrade run the code on the volume.

	The master runs with --preload: it imported frappe once, at container start, and every
	worker is a fork of it. The extension agent installs or upgrades an extension without a
	container restart (deploy/prod/ext-reload.sh: replay in place, then HUP, which forks new
	workers from this same master and retires the old ones gracefully). Two things in a fork
	are as old as the master, and are put right here, in the worker, before it takes a request:

	* sys.path: site.py read the .pth files at interpreter start. An extension installed since
	  (apps/<id> via <id>.pth) or one that gained wheels (zz-ext-deps-<id>.pth) is not on it.
	  Path lines of .pth files not on sys.path yet are appended, in file-name order — what
	  site.py would have done, minus its ``import`` lines (editable-install finders must not
	  run twice). Appended means after every core path, as the zz- ordering intends.
	* sys.modules: anything the master imported from apps-ext (nothing, with frappe's preload
	  list, which is core only — this is the belt) is dropped, so the worker imports the tree
	  that is on the volume now.

	Cheap (a directory listing and a dict scan) and idempotent: a fork at container start finds
	nothing to do."""
	import importlib
	import os
	import sys
	import sysconfig

	purelib = sysconfig.get_paths()["purelib"]
	try:
		names = sorted(n for n in os.listdir(purelib) if n.endswith(".pth"))
	except OSError:
		names = []
	for name in names:
		try:
			with open(os.path.join(purelib, name)) as f:
				lines = f.read().splitlines()
		except OSError:
			continue
		for line in lines:
			line = line.strip()
			if not line or line.startswith(("#", "import ", "import\t")):
				continue
			path = os.path.abspath(os.path.join(purelib, line))
			if path not in sys.path and os.path.isdir(path):
				sys.path.append(path)

	root = os.path.realpath(EXT_DIR) + os.sep
	stale = [name for name, mod in list(sys.modules.items())
		if (f := getattr(mod, "__file__", None)) and os.path.realpath(f).startswith(root)]
	for name in stale:
		del sys.modules[name]
	importlib.invalidate_caches()
