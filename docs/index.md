# OpenGunstore documentation

OpenGunstore is an open-source point of sale and back office for US firearms retailers (FFL holders). It is built on Frappe and ERPNext v16 and released under the GNU GPL v3. One inventory of serialized firearms is sold at the counter, on a WooCommerce site and on GunBroker. The bound book and the Form 4473 are written by the sale flow itself. A sale does not complete until the form is done and the NICS result is Proceed.

## Where to start

| If you are | Start here |
|---|---|
| Staff or a store manager | [User guide](guide/index.md): selling, inventory, compliance, channels, distributors, reports and settings, written for the counter. |
| Installing it on a server | [Self-hosting](self-hosting.md): install, back up, restore, upgrade; extensions on a server in the [Extensions runbook](extensions-runbook.md). An AI coding agent can follow [Install with an AI agent](deploy-with-ai.md) instead. |
| Changing it | [Developers](developers/index.md): the architecture, the dev stack, contributing, testing, and the MCP server for AI assistants. |
| Writing an extension | [Build an extension](extensions/README.md): quick start, manifest, hooks, rules, CI and release. |

## Reference

- [API reference](api/index.html): every whitelisted endpoint, generated from the source and kept current by CI.
- [Pinned versions](versions.md): the Frappe, ERPNext and Python versions this release runs on.
- [Data service](data-service.md): what the optional hosted service provides and what works without it.
- [Source code and issues](https://github.com/Old-Steel-Arsenal/opengunstore) on GitHub.

## For AI assistants

Every page of this site is also served as Markdown: add `.md` to the page's address. `llms.txt` at the root of the site lists every page with a one-line summary, and `llms-full.txt` is the whole documentation in one file. To work on a store through an assistant, see [The MCP server](ai/mcp.md) and [Working as an AI agent](ai/operating.md).
