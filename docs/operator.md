# The operator: Hermes over MCP, reachable from Telegram

How the operator side of `docs/design/hermes.md` (part 1) is set up on this Mac,
so it can be rebuilt or checked. Set up 2026-09-25.

## Pieces

| Piece | Where | What it does |
|---|---|---|
| MCP server | `tradingagents mcp serve` (optional `mcp` install) | Seven read-only tools: `nightly_status`, `recent_decisions`, `screen_review`, `list_decisions`, `get_report`, `pending_reviews`, `data_store_stats`. None takes a path or config; none returns a key. |
| Hermes | `~/.hermes/hermes-agent`, pinned at `59004a6` (detached) | The agent runtime. Installed with its own installer: `--commit 59004a62356f… --skip-setup --skip-browser`. |
| Operator profile | `~/.hermes/profiles/tradeops` | Claude Haiku 4.5 via the Anthropic API; the only toolset on CLI and Telegram is `mcp-trade-agents`. Every built-in toolset -- terminal, file, browser, web, memory, cron -- is off. |
| Host gateway | LaunchAgent `com.jeremysmith.tradingagents.hermes` (`hermes gateway run --external-supervisor`, KeepAlive) | Hermes runs one gateway per machine; it serves every profile's platforms. Only `tradeops` has one (Telegram); the default profile has none, so nothing inbound reaches it. |
| Morning report | Hermes cron job `morning-report`, `0 8 * * *`, in `tradeops` | Runs `scripts/morning-report.sh` -> `tradingagents morning-report` and posts the output verbatim to the channel (`--no-agent`). No model writes it, so it cannot misreport. |
| Wake | the platform wake daemon (07:55) | The Mac is awake when the 08:00 job fires. |

## Secrets

The profile's `.env` (`~/.hermes/profiles/tradeops/.env`, mode 600) holds
`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL` and
`TELEGRAM_ALLOWED_CHATS` (the channel id, `-100…`). They were copied from the
repo's `.env`, which git ignores. The MCP server itself never reads or returns
any of them.

## Checks

```
hermes -p tradeops tools list --platform telegram   # only "MCP servers: trade-agents"
hermes -p tradeops cron status                      # heartbeat, next run 08:00
hermes -p tradeops cron list                        # morning-report, active
tradingagents morning-report                        # what the 08:00 message will say
```

Asked to list files in the home directory, the operator answers that it cannot:
it has no shell.

## Rebuilding

1. Install Hermes with the installer flags above.
2. `hermes profile create tradeops --no-skills --no-alias`
3. Write the profile's `config.yaml`:
   ```yaml
   model: {default: claude-haiku-4-5-20251001, provider: anthropic}
   platform_toolsets: {cli: [mcp-trade-agents], telegram: [mcp-trade-agents]}
   mcp_servers:
     trade-agents:
       command: /Users/jeremysmith/.local/bin/tradingagents
       args: [mcp, serve]
       sampling: {enabled: false}
   ```
4. Put the four variables above in the profile's `.env`; `chmod 600` it.
5. Copy `scripts/morning-report.sh` into the profile's `scripts/`, then
   `hermes -p tradeops cron create "0 8 * * *" --name morning-report --no-agent --script morning-report.sh --deliver telegram`
6. `sudo scripts/install-platform.sh` installs the gateway LaunchAgent.
