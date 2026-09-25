# The operator: Hermes over MCP, reachable from Telegram

How the operator side of `docs/design/hermes.md` (part 1) is set up on this Mac,
so it can be rebuilt or checked. Set up 2026-09-25.

## Pieces

| Piece | Where | What it does |
|---|---|---|
| MCP server | `tradingagents mcp serve` (optional `mcp` install) | Ten read-only tools (`nightly_status`, `recent_decisions`, `screen_review`, `list_decisions`, `get_report`, `pending_reviews`, `data_store_stats`, `harvest_status`, `job_status`, `trade_status`) and four actions: `screen_run` (at most 5 names) and `retry_failed`, which run as detached jobs and are refused 01:30-07:30 and while another run is going; `submit_order_plan` and `halt_trading` (paper book, below). None takes a path or config; none returns a key. It reads the repo's `.env` wherever Hermes starts it. |
| Hermes | `~/.hermes/hermes-agent`, pinned at `59004a6` (detached) | The agent runtime. Installed with its own installer: `--commit 59004a62356f… --skip-setup --skip-browser`. |
| Operator profile | `~/.hermes/profiles/tradeops` | Claude Haiku 4.5 via the Anthropic API; the only toolset on CLI and Telegram is `mcp-trade-agents`. Every built-in toolset -- terminal, file, browser, web, memory, cron -- is off. |
| Host gateway | LaunchAgent `com.jeremysmith.tradingagents.hermes` (`hermes gateway run --external-supervisor`, KeepAlive) | Hermes runs one gateway per machine; it serves every profile's platforms. Only `tradeops` has one (Telegram); the default profile has none, so nothing inbound reaches it. |
| Morning report | Hermes cron job `morning-report`, `0 8 * * *`, in `tradeops` | Runs `scripts/morning-report.sh` -> `tradingagents morning-report` and posts the output verbatim to the channel (`--no-agent`). No model writes it, so it cannot misreport. |
| Watchers | Hermes cron jobs `watch-earnings` (08:05 daily), `watch-ownership` (08:10 daily), `watch-reviews` (08:05 Mondays), `watch-edge` (08:10 Mondays) | Each runs `tradingagents watch <name>` (`--no-agent`) and is silent when there is nothing to say. |
| Approval | `trust: untrusted` on the `trade-agents` server in the profile's `config.yaml` | Hermes asks before every call of a tool not annotated read-only (the four actions). Without it, missing trust means full and nothing is asked. Checked: a one-shot request to start a screen run was stopped at the approval prompt and denied. |
| Wake | the platform wake daemon (07:55) and the stay-awake agent (`com.jeremysmith.tradingagents.awake`, caffeinate 20 min from 01:56 and 07:56) | A scheduled wake alone may drop back to sleep within minutes; the stay-awake agent keeps the Mac up for the 02:00 run and the 08:00-08:10 jobs. |

## Paper trading (the standard strategy)

Design: `docs/design/go-live.md`. Paper only; the live endpoint is refused
unless `trading_live` is set, which nothing sets.

| When | What |
|---|---|
| Nightly, after the value run | `screen --mandate none` (free); with `NIGHTLY_STANDARD_NAMES` > 0, the agents decide today's picks; with Alpaca keys in `.env`, `trade reconcile` then `trade plan`. Nothing is sent. |
| 08:00 | The morning report leads with the pending plan, if it has orders. |
| Before 09:28 New York | Approve: `tradingagents trade submit <plan>` at the terminal, or ask Hermes to submit it (it asks for approval first). An unapproved plan expires; nothing is sent by default. |
| Any time | `tradingagents trade halt` (or Hermes: halt trading) refuses every new order and cancels open ones; positions are kept. `trade resume` lifts it -- at the terminal only. |
| After a mismatch | Reconciliation blocks new orders until `tradingagents trade ack`, after checking the broker. |

`tradingagents trade status` shows live cohorts, the switches and the account.

## Secrets

The profile's `.env` (`~/.hermes/profiles/tradeops/.env`, mode 600) holds
`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL` and
`TELEGRAM_ALLOWED_CHATS` (the channel id, `-100…`). They were copied from the
repo's `.env`, which git ignores. The MCP server never returns any of them;
it reads the repo's `.env` for `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (paper
account), which stay out of the Hermes profile.

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
       trust: untrusted          # the actions need approval
       sampling: {enabled: false}
   ```
4. Put the four variables above in the profile's `.env`; `chmod 600` it.
5. Copy `scripts/morning-report.sh` into the profile's `scripts/`, then
   `hermes -p tradeops cron create "0 8 * * *" --name morning-report --no-agent --script morning-report.sh --deliver telegram`.
   For each watcher, a script `watch-<name>.sh` running `tradingagents watch <name>`, and
   `hermes -p tradeops cron create "<schedule>" --name watch-<name> --no-agent --script watch-<name>.sh --deliver telegram`
   with the schedules above.
6. `sudo scripts/install-platform.sh` installs the gateway LaunchAgent.
