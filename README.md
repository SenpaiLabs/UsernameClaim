# UsernameClaim

A Telegram bot that watches a list of usernames and, the moment one becomes free, assigns it to your channel and sends you a message.

Setting a channel username is not possible with the Bot API, so this runs on your own Telegram account through [Telethon](https://docs.telethon.dev/) (MTProto). The account must be the **owner** of the channel.

## Features

- Checks every username on your list repeatedly (default every ~30 seconds)
- Claims the first free username for your channel and notifies you
- List order is priority
- Manage the list from Telegram with simple commands
- Handles Telegram flood limits and keeps running after network errors
- Error alerts (invalid username, too many public channels, missing admin rights) are sent once, not on every round

## Requirements

- Python 3.9+
- A Telegram account that owns the target channel
- `API_ID` and `API_HASH` from https://my.telegram.org (API development tools)

## Installation

```bash
git clone https://github.com/SenpaiLabs/UsernameClaim.git
cd UsernameClaim
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
| --- | --- |
| `API_ID`, `API_HASH` | Credentials from my.telegram.org |
| `CHANNEL` | Your channel: `@username`, invite link or numeric ID |
| `OWNER_ID` | Who receives alerts and can send commands. `me` means Saved Messages, or use a numeric user ID |
| `CHECK_INTERVAL` | Seconds between rounds (default `30`, keep it at 20 or more) |

## Usage

```bash
python bot.py
```

On the first run Telegram asks for your phone number and a login code. A session file is then saved, so you only log in once.

Send these commands in Saved Messages (or in the private chat with `OWNER_ID`):

| Command | Action |
| --- | --- |
| `/add username` | Add a username to the watch list |
| `/remove username` | Remove a username |
| `/list` | Show the list and whether monitoring is running or paused |
| `/resume` | Resume monitoring after a successful claim |

A channel can have only one username, so monitoring pauses after a successful claim. Use `/resume` to continue (a later claim replaces the current username).

The watch list is stored in `usernames.txt`, one username per line.

## Notes

- Nobody can guarantee a username: the first claim request that reaches Telegram wins. Run the bot 24/7 on a server close to Telegram's datacenters for the best chance.
- Do not use extra accounts or very short intervals to get around flood limits. Telegram may restrict or ban the account.
- Never share or commit `.env` or the `*.session` file. They give full access to your account. `.gitignore` already excludes them.
- Usernames held for sale on Fragment cannot be claimed this way.

## CI

The pipeline runs GitLab SAST and Secret Detection (see `.gitlab-ci.yml`).
