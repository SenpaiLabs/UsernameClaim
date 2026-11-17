"""Telegram username claim bot.

Monitors a list of usernames and, as soon as one becomes free, assigns it to
your channel and notifies you.

Note: setting a channel username requires MTProto, so this runs as your own
Telegram account (Telethon), which must be the owner of CHANNEL.

Owner commands (send in Saved Messages, in the private chat with OWNER_ID, or,
if BOT_TOKEN is set, in a private chat with your Telegram bot):
    /add <username>      add a username to the watch list
    /remove <username>   remove a username
    /list                show watch list and status
    /pause               pause monitoring
    /resume              resume monitoring
"""

import asyncio
import logging
import os
import random
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import (
    ChannelsAdminPublicTooMuchError,
    ChatAdminRequiredError,
    FloodWaitError,
    RPCError,
    UsernameInvalidError,
    UsernameNotModifiedError,
    UsernameOccupiedError,
)
from telethon.tl.functions.channels import CheckUsernameRequest, UpdateUsernameRequest

load_dotenv()


def parse_entity(value: str):
    """Numeric strings are IDs, anything else is a username, link or 'me'."""
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
CHANNEL = parse_entity(os.environ["CHANNEL"])  # @username, invite link or numeric ID
OWNER = parse_entity(os.getenv("OWNER_ID", "me"))  # gets notifications, sends commands
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()  # optional: control via a Telegram bot
CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL", "30"))  # seconds per full round
USERNAMES_FILE = Path(os.getenv("USERNAMES_FILE", "usernames.txt"))
SESSION = os.getenv("SESSION_NAME", "usernameclaim")
BOT_SESSION = os.getenv("BOT_SESSION_NAME", "usernameclaim_bot")

if BOT_TOKEN and not isinstance(OWNER, int):
    raise SystemExit("BOT_TOKEN requires OWNER_ID to be your numeric Telegram user ID.")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("usernameclaim")

client = TelegramClient(SESSION, API_ID, API_HASH)
bot_client = None  # set in main() when BOT_TOKEN is configured
paused = False
notified: set[str] = set()  # alert keys already sent, to avoid repeating every round


def normalize(name: str) -> str:
    return name.strip().lstrip("@").lower()


def load_usernames() -> list[str]:
    if not USERNAMES_FILE.exists():
        return []
    seen, result = set(), []
    for line in USERNAMES_FILE.read_text().splitlines():
        name = normalize(line)
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def save_usernames(names: list[str]) -> None:
    USERNAMES_FILE.write_text("\n".join(names) + ("\n" if names else ""))


async def notify(text: str) -> None:
    """Send via the bot if configured, falling back to the user account."""
    for sender in (bot_client, client):
        if sender is None:
            continue
        try:
            await sender.send_message(OWNER, text)
            return
        except Exception:
            log.warning("Notification failed via %s", "bot" if sender is bot_client else "account")
    log.error("Failed to send notification")


async def notify_once(key: str, text: str) -> None:
    """Send an alert only the first time `key` occurs (cleared on /resume)."""
    if key in notified:
        return
    notified.add(key)
    await notify(text)


async def try_claim(username: str, channel) -> bool:
    """Return True if the username was claimed for the channel."""
    try:
        available = await client(CheckUsernameRequest(channel, username))
        if not available:
            log.info("@%s is taken", username)
            return False

        await client(UpdateUsernameRequest(channel, username))
    except FloodWaitError as e:
        log.warning("Flood wait: sleeping %s seconds", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return False
    except UsernameOccupiedError:
        log.info("@%s was taken just before claim", username)
        return False
    except UsernameInvalidError:
        log.warning("@%s is invalid", username)
        await notify_once(
            f"invalid:{username}",
            f"@{username} is not a valid username (check length/characters). "
            "Use /remove to delete it from the list.",
        )
        return False
    except UsernameNotModifiedError:
        await notify(f"Channel already uses @{username}.")
        return True
    except ChannelsAdminPublicTooMuchError:
        await notify_once(
            f"too-many-public:{username}",
            f"@{username} is free but your account owns too many public channels. "
            "Remove a public username from one and try again.",
        )
        return False
    except ChatAdminRequiredError:
        await notify_once(
            "admin-required", "Your account must be the owner/admin of the channel."
        )
        return False
    except RPCError as e:
        log.warning("Could not claim @%s: %s", username, e)
        return False

    log.info("Claimed @%s", username)
    await notify(f"Claimed @{username} for your channel.")
    return True


async def monitor() -> None:
    global paused
    await client.get_dialogs()  # fills the entity cache so numeric IDs resolve
    channel = await client.get_input_entity(CHANNEL)
    log.info("Monitoring started")
    while True:
        try:
            if not paused:
                for username in load_usernames():  # list order = priority
                    if await try_claim(username, channel):
                        paused = True
                        save_usernames(
                            [n for n in load_usernames() if n != username]
                        )
                        break
                    await asyncio.sleep(random.uniform(1, 3))
        except Exception:
            log.exception("Monitor round failed, retrying next round")
        await asyncio.sleep(CHECK_INTERVAL + random.uniform(0, CHECK_INTERVAL * 0.2))


COMMAND_PATTERN = r"^/(add|remove|list|pause|resume|start|help)\b"
USAGE = "Usage: /add <username>, /remove <username>, /list, /pause, /resume"


async def commands(event: events.NewMessage.Event) -> None:
    global paused
    command = event.pattern_match.group(1)
    parts = event.raw_text.split(maxsplit=1)  # also handles newline-separated args
    arg = normalize(parts[1]) if len(parts) > 1 else ""
    names = load_usernames()

    if command == "add" and arg:
        if arg not in names:
            names.append(arg)
            save_usernames(names)
        notified.discard(f"invalid:{arg}")
        await event.reply(f"Added @{arg}. Watching {len(names)} username(s).")
    elif command == "remove" and arg:
        save_usernames([n for n in names if n != arg])
        await event.reply(f"Removed @{arg}.")
    elif command == "list":
        status = "paused" if paused else "running"
        body = "\n".join(f"@{n}" for n in names) or "(empty)"
        await event.reply(f"Status: {status}\n{body}")
    elif command == "resume":
        paused = False
        notified.clear()
        await event.reply("Monitoring resumed.")
    elif command == "pause":
        paused = True
        await event.reply("Monitoring paused.")
    else:
        await event.reply(USAGE)


async def main() -> None:
    global bot_client
    await client.start()  # first run asks for phone number and login code
    client.add_event_handler(
        commands,
        events.NewMessage(chats=OWNER, from_users=OWNER, pattern=COMMAND_PATTERN),
    )
    tasks = [monitor(), client.run_until_disconnected()]

    if BOT_TOKEN:
        bot_client = TelegramClient(BOT_SESSION, API_ID, API_HASH)
        await bot_client.start(bot_token=BOT_TOKEN)
        bot_client.add_event_handler(
            commands,
            events.NewMessage(
                incoming=True,
                from_users=OWNER,
                pattern=COMMAND_PATTERN,
                func=lambda e: e.is_private,
            ),
        )
        log.info("Bot control enabled")
        tasks.append(bot_client.run_until_disconnected())

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
