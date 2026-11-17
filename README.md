# UsernameClaim

A Telegram bot that watches a list of usernames and, the moment one becomes free, assigns it to a channel and sends you a message. It can create the channel for you.

Setting a channel username is not possible with the Bot API, so this runs on your own Telegram account through [Telethon](https://docs.telethon.dev/) (MTProto).

## Features

- Checks every username on your list repeatedly (default every ~30 seconds)
- Claims the first free username for a channel and notifies you with the `t.me` link
- List order is priority
- Creates the channel itself, or uses a channel you already own
- Manage the list from Telegram with simple commands
- Handles Telegram flood limits and keeps running after network errors
- Error alerts (invalid username, too many public channels, missing admin rights) are sent once, not on every round

## Requirements

- Python 3.9+
- A Telegram account
- `API_ID` and `API_HASH` from https://my.telegram.org (API development tools)

## Installation

```bash
git clone https://gitlab.com/senpailabs-group/usernameclaim.git
cd usernameclaim
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
| --- | --- |
| `API_ID`, `API_HASH` | Credentials from my.telegram.org |
| `CHANNEL` | Optional. `@username`, invite link or numeric ID of a channel you own. **Leave empty** and the bot creates a channel itself |
| `CHANNEL_TITLE` | Title of the channel the bot creates (default `My Channel`) |
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

Monitoring pauses after a successful claim because a channel can have only one username. Use `/resume` to continue.

### Channel modes

- **Your own channel** (`CHANNEL` set): a later claim replaces that channel's current username.
- **Auto-created channel** (`CHANNEL` empty): the bot creates a channel on first start and remembers it in `channel_id.txt`. After a claim, that channel keeps its username. On `/resume` the bot creates a fresh channel for the next username, so you can collect several usernames.

The watch list is stored in `usernames.txt`, one username per line.

## Notes

- Nobody can guarantee a username: the first claim request that reaches Telegram wins. Run the bot 24/7 on a server close to Telegram's datacenters for the best chance.
- Telegram limits how many channels and public usernames one account can own. In auto mode the bot alerts you when a limit is hit. Do not create many channels in a short time, or the account may get restricted.
- Do not use extra accounts or very short intervals to get around flood limits. Telegram may restrict or ban the account.
- Never share or commit `.env` or the `*.session` file. They give full access to your account. `.gitignore` already excludes them.
- Usernames held for sale on Fragment cannot be claimed this way.

## CI

The pipeline runs GitLab SAST and Secret Detection (see `.gitlab-ci.yml`).
