# Connect your assistant

For anyone connecting Claude, ChatGPT or Grok to the store for the first time. It takes about two minutes per assistant. You need the connection switched on first; see [AI assistants](index.md#before-you-start).

## Get the address

Ask your manager for the address, or open **Settings**, then the **MCP Settings** card, if you can see it.

| You want | Use this address |
|---|---|
| Read-only | **Read-only MCP Server URL**, ending in `/connector/cpa/mcp` |
| Full | **Full MCP Server URL**, ending in `/connector/full/mcp` |

Use the read-only address unless you need the assistant to change things.

## Sign in and approve

Every assistant below ends the same way. When you add the address, a POS page opens in your browser:

1. Sign in with **your own** POS email and password.
2. The page says the assistant "wants to access the following details from your account". Click **Allow**. **Deny** cancels.
3. Go back to the assistant. It now lists the store's tools.

You approve each assistant once. To cut a connection off, ask a System Manager.

## Claude: web, desktop and phone

The Claude website, the Claude desktop app and the Claude phone app share one list of connectors, so add it once on the web.

1. On claude.ai, open **Settings**, then **Connectors**. On newer versions it is **Customize**, then **Connectors**.
2. Click **Add custom connector**.
3. Give it a name you will recognize, such as the store's name, and paste the address.
4. Click **Add**, then **Connect**, and sign in and approve as above.
5. In a chat, open the **+** menu, choose **Connectors** and turn the store on for that chat.

On a **Team** or **Enterprise** plan, the account owner adds the connector once for everyone under the organization's connector settings. Each person then clicks **Connect** and signs in with their own POS account.

## Claude Code (command line)

**MCP Settings** shows a ready-made command for each address under **Claude Code Command (Read-only)** and **Claude Code Command (Full)**. Copy it into a terminal. It looks like this:

```bash
claude mcp add -s user --transport http gunstore-pos https://<your POS address>/connector/full/mcp
```

Then:

1. Start `claude` and type `/mcp`.
2. Pick the store's server and choose **Authenticate**. Your browser opens; sign in and approve as above.
3. Start a new session to pick up the tools.

If you connect two stores, give the second one a different name than the command suggests, such as `gunstore-pos-store2`.

## ChatGPT: web and desktop

ChatGPT adds custom connectors in **Developer mode**, on the plans that offer it (Plus and above at the time of writing; check your plan).

1. On chatgpt.com, open **Settings**, then **Apps & Connectors** (or **Connectors**).
2. Under **Advanced settings**, turn on **Developer mode**.
3. Click **Create** (a custom connector), give it a name, paste the address and set authentication to **OAuth**.
4. Click **Create**, then sign in and approve as above.
5. In a chat, pick the connector from the tools menu to use it.

The ChatGPT desktop app uses the connectors you added on the web. On a **Business** or **Enterprise** workspace, an admin may need to add the connector for the workspace first.

## Codex (command line)

```bash
codex mcp add gunstore-pos --url https://<your POS address>/connector/full/mcp
codex mcp login gunstore-pos
```

The second command opens your browser to sign in and approve. On a computer without a browser, add `--no-browser`, open the link it prints on another device, and paste the address you land on back into the terminal. `codex mcp list` shows what is connected.

## Grok: web and phone

1. Go to grok.com/connectors.
2. Click **New Connector**, then **Custom**.
3. Paste the address, then sign in and approve as above.

If you use the Grok command line instead, the same address goes in with:

```bash
grok mcp add --transport http gunstore-pos https://<your POS address>/connector/full/mcp
```

`grok mcp doctor` checks the connection.

On **Grok Business** or **Enterprise**, a team admin adds the connector first. Grok's plans and screens change often; if you do not see custom connectors, check your plan.

## Check it works

Ask: "List the firearms in stock." Compare the answer with the **Firearms In Stock** report on screen. If they match, you are connected to the right store. If you use two stores, check which one answered before you ask for any change.

## If it does not work

| What you see | What it means | Fix |
|---|---|---|
| "The cpa (or full) MCP server is switched off in MCP Settings." | The connection is not switched on | Ask a System Manager to tick it in **MCP Settings** |
| The POS sign-in page keeps coming back | You signed in to a different store, or with a shared login | Sign in with your own account on the store the address belongs to |
| The assistant says a tool does not exist | It kept an old list of tools | Disconnect and connect again, or start a new chat |
| It cannot do something you can do on screen | Your role does not allow it through the connection, or the action is screen-only | Do it on screen |
| Your account has **Restrict IP** set | Calls reach the POS from the assistant's servers, not your computer | Ask a System Manager; a restricted account cannot use a connector |
