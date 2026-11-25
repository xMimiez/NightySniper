<p align="center">
  <img src="assets/icon.webp" alt="Nighty Sniper" width="112" />
</p>

<div align="center">

# Nighty Sniper

Watches Discord for gift, promotion, and invite links and forwards each new one to a webhook.

[![script version](https://img.shields.io/badge/script%20version-1.0-5865F2?style=flat-square)](./nightysniper.py)
[![NightyScript](https://img.shields.io/badge/NightyScript-5.1-111827?style=flat-square)](https://github.com/xmimiez/nighty-docs)
[![Python](https://img.shields.io/badge/Python-3.14-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-227c57?style=flat-square)](./LICENSE)

</div>

## About

Nighty Sniper adds a Sniper tab to Nighty. The account Nighty is logged into is always one source. Extra accounts can be added in the tab and connect on their own gateway. Each source keeps its own live counts for messages, links, sent, repeats, and dropped.

The script posts matching links to a Discord webhook. It does not ask Discord whether a gift code is real, and it does not redeem codes.

> [!WARNING]
> Nighty is a Discord selfbot. Running a user token in an automated client can violate Discord's Terms of Service and may put accounts at risk. This project is not affiliated with Discord or Nighty. You are responsible for how you use it.

## Features

- Watch gift, promotion, and invite links from the Nighty account.
- Add extra accounts by token and label, then connect or remove them from the Accounts table.
- Set extra-account presence to invisible, online, idle, or do not disturb.
- Subscribe extra accounts to every server they are in.
- Skip a repeated link for 24 hours.
- Append each new invite to `scriptData/invites.txt`.
- Show live counters on each account row.
- Check this repository for a newer `SCRIPT_VERSION` and show an in-app update notice.

Extra accounts use direct Discord gateway connections. Tokens stay in a local JSON file and are not encrypted.

## Requirements

- A working [Nighty](https://nighty.one/) installation
- NightyScript 5.1
- Python 3.14 in Nighty's runtime
- `aiohttp`, normally included with Nighty

## Language and architecture

Nighty Sniper is written in **Python for NightyScript 5.1**. NightyScript is Nighty's embedded scripting environment, not a standalone Discord bot framework. Nighty owns the logged-in Discord client and injects its scripting API into the file when the script loads.

- `@nightyScript(...)` declares the script and its metadata. The decorated entry function is called at the bottom of the file.
- `bot`, `UI`, `Tab`, `getConfigData`, `updateConfigData`, and `getScriptsPath` come from Nighty's runtime. They are available without imports.
- The script must not import `discord`, `nighty`, or symbols from either package.
- Discord and Nighty operations are asynchronous.
- The tab uses Nighty's native controls.
- Simple settings use Nighty's configuration store. Extra account tokens use a JSON file under `getScriptsPath()`.

The main runtime path is:

```
Nighty loads nightysniper.py
  -> @nightyScript initializes the script
  -> the Sniper tab is registered
  -> the Nighty account listens for messages
  -> each extra account opens its own gateway
  -> matching links are posted to the webhook
  -> invite links are appended to scriptData/invites.txt
```

## Installation

1. Download [`nightysniper.py`](./nightysniper.py).
2. Import the file through Nighty's script manager, or place it in the Nighty scripts directory.
3. Enable the script.
4. Open the **Sniper** tab.
5. Paste a webhook, choose what to watch, and turn Sniper on.

## Local data

Nighty Sniper creates these files under Nighty's scripts directory:

```
json/sniper_tokens.json     Extra account tokens and labels
scriptData/invites.txt      Invite links the script has forwarded
```

Do not commit or share these files. `sniper_tokens.json` contains usable Discord account tokens in plain text.

## Behavior and limits

- A link is matched by its shape. A gift link is not checked with Discord before it is forwarded.
- The script does not redeem gift codes.
- Extra accounts only see servers and channels Discord sends to that account.
- The script depends on private Nighty and Discord behavior. Client or API updates can break individual connections.
- The update check reads `SCRIPT_VERSION` from the raw `nightysniper.py` on the `main` branch. It does not run the downloaded file.

## Security

- Treat every Discord token as a password.
- Keep Nighty's script-data directory private.
- Never post `sniper_tokens.json` in an issue or support channel.
- Use extra accounts at your own risk.
- Review the script before loading updates from a fork.

## Credits

- [xMimiez](https://github.com/xMimiez) (`Mime | @n0_.q3`)
- [neoarz](https://github.com/neoarz)

## License

Nighty Sniper is licensed under the [PolyForm Noncommercial License 1.0.0](./LICENSE).
