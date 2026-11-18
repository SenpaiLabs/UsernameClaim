"""Multi-Account Round-Robin Telegram Username Claimer (up to 10 accounts).

Features:
- Up to 10 StringSession accounts (SESSION_1..SESSION_10) with per-account or shared API_ID/API_HASH
- Optional per-account SOCKS5/HTTP/MTProto proxy (PROXY_1..PROXY_10)
- Per-account pre-created channel mapping (CHANNEL_1..CHANNEL_10) or staggered auto-creation
- 1.0s global non-blocking Round-Robin loop (asyncio.create_task)
- Per-account FloodWait cooldown tracking (cooldown[account_index] = time.time() + e.seconds)
- Atomic claiming via asyncio.Event (claimed_event) with rollback on transient failure
- Preserves /add, /remove, /list, /resume owner commands and cleanly disconnects all clients
"""

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, connection, events
from telethon.errors import (
    ChannelsAdminPublicTooMuchError,
    ChannelsTooMuchError,
    ChatAdminRequiredError,
    FloodWaitError,
    RPCError,
    UsernameInvalidError,
    UsernameNotModifiedError,
    UsernameOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    CheckUsernameRequest,
    CreateChannelRequest,
    UpdateUsernameRequest,
)
from telethon.tl.types import PeerChannel

load_dotenv()


def parse_entity(value: str):
    """Numeric strings are IDs, anything else is a username, link or 'me'."""
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


def parse_proxy_config(proxy_url: str) -> dict[str, Any]:
    """Parse socks5://user:pass@host:port, http://host:port, or mtproto://secret@host:port."""
    proxy_url = proxy_url.strip()
    if not proxy_url:
        return {}
    u = urlparse(proxy_url)
    if not u.scheme or not u.hostname or not u.port:
        return {}
    scheme = u.scheme.lower()
    if scheme in ("mtproto", "mtproxy"):
        secret = (u.username or u.path.lstrip("/")).strip()
        return {
            "connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (u.hostname, u.port, secret),
        }
    return {
        "proxy": {
            "proxy_type": scheme,  # 'socks5', 'socks4', or 'http'
            "addr": u.hostname,
            "port": u.port,
            "rdns": True,
            "username": u.username,
            "password": u.password,
        }
    }


MAX_ACCOUNTS = 10
DEFAULT_API_ID = int(os.getenv("API_ID", "0") or "0")
DEFAULT_API_HASH = os.getenv("API_HASH", "").strip()
CHANNEL_TITLE = os.getenv("CHANNEL_TITLE", "My Channel")
CHANNELS_MAP_FILE = Path(os.getenv("CHANNELS_MAP_FILE", "channels_map.json"))
OWNER = parse_entity(os.getenv("OWNER_ID", "me"))
LOGGER_ID = parse_entity(os.getenv("LOGGER_ID", "") or os.getenv("OWNER_ID", "me"))
CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL", "1.0"))  # 1.0s global round-robin tick
USERNAMES_FILE = Path(os.getenv("USERNAMES_FILE", "usernames.txt"))
EXIT_ON_CLAIM = os.getenv("EXIT_ON_CLAIM", "true").strip().lower() in ("1", "true", "yes")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
log = logging.getLogger("usernameclaim")


@dataclass
class AccountWorker:
    index: int
    client: TelegramClient
    channel_spec: int | str | None
    target: Any = None
    channel_id: int | str | None = None
    name: str = ""
    disabled: bool = False
    in_flight: bool = False


workers: list[AccountWorker] = []
cooldown: dict[int, float] = {}  # account_index -> unix timestamp when FloodWait expires
claimed_event = asyncio.Event()
shutdown_event = asyncio.Event()
paused = False
notified: set[str] = set()
invalid_usernames: set[str] = set()
in_flight_tasks: set[asyncio.Task] = set()


def is_worker_healthy(w: AccountWorker, now: float) -> bool:
    return not w.disabled and not w.in_flight and now >= cooldown.get(w.index, 0.0)


def normalize(name: str) -> str:
    return name.strip().lstrip("@").lower()


def load_usernames() -> list[str]:
    if not USERNAMES_FILE.exists():
        return []
    seen, result = set(), []
    for line in USERNAMES_FILE.read_text().splitlines():
        name = normalize(line)
        if name and name not in seen and name not in invalid_usernames:
            seen.add(name)
            result.append(name)
    return result


def save_usernames(names: list[str]) -> None:
    USERNAMES_FILE.write_text("\n".join(names) + ("\n" if names else ""))


def load_saved_channels() -> dict[str, int]:
    if not CHANNELS_MAP_FILE.exists():
        return {}
    try:
        return json.loads(CHANNELS_MAP_FILE.read_text())
    except Exception:
        return {}


def save_channel_id(index: int, channel_id: int) -> None:
    data = load_saved_channels()
    data[str(index)] = channel_id
    CHANNELS_MAP_FILE.write_text(json.dumps(data, indent=2))


def remove_saved_channel(index: int) -> None:
    data = load_saved_channels()
    if str(index) in data:
        del data[str(index)]
        CHANNELS_MAP_FILE.write_text(json.dumps(data, indent=2))


async def notify(text: str, client: TelegramClient | None = None) -> None:
    """Send alert via the given client (or primary worker client) to OWNER_ID / LOGGER_ID."""
    sender = client or (workers[0].client if workers else None)
    if sender is None:
        return
    destinations = [OWNER] if LOGGER_ID == OWNER else [OWNER, LOGGER_ID]
    for dest in destinations:
        try:
            await sender.send_message(dest, text)
        except Exception:
            log.exception("Failed to send notification to %s", dest)


async def notify_once(key: str, text: str, client: TelegramClient | None = None) -> None:
    if key in notified:
        return
    notified.add(key)
    await notify(text, client=client)


def build_workers() -> list[AccountWorker]:
    """Load SESSION_1..SESSION_10 from env (falls back to single SESSION_NAME if none set)."""
    pool: list[AccountWorker] = []
    for i in range(1, MAX_ACCOUNTS + 1):
        sess_str = os.getenv(f"SESSION_{i}", "").strip()
        if not sess_str:
            continue
        api_id = int(os.getenv(f"API_ID_{i}", "") or DEFAULT_API_ID)
        api_hash = (os.getenv(f"API_HASH_{i}", "") or DEFAULT_API_HASH).strip()
        if not api_id or not api_hash:
            raise ValueError(f"Account #{i}: API_ID / API_HASH missing")

        proxy_kwargs = parse_proxy_config(os.getenv(f"PROXY_{i}", ""))
        ch_raw = os.getenv(f"CHANNEL_{i}", "").strip()
        ch_spec = parse_entity(ch_raw) if ch_raw else None

        tg_client = TelegramClient(
            StringSession(sess_str),
            api_id,
            api_hash,
            **proxy_kwargs,
        )
        tg_client.flood_sleep_threshold = 0  # never auto-sleep inside Telethon; handle per account
        pool.append(AccountWorker(index=i, client=tg_client, channel_spec=ch_spec))
        cooldown[i] = 0.0

    if not pool:
        # Legacy single-account fallback
        session_name = os.getenv("SESSION_NAME", "usernameclaim")
        ch_raw = os.getenv("CHANNEL", "").strip()
        tg_client = TelegramClient(session_name, DEFAULT_API_ID, DEFAULT_API_HASH)
        tg_client.flood_sleep_threshold = 0
        pool.append(
            AccountWorker(
                index=1,
                client=tg_client,
                channel_spec=parse_entity(ch_raw) if ch_raw else None,
            )
        )
        cooldown[1] = 0.0
    return pool


async def ensure_worker_channel(w: AccountWorker) -> None:
    """Resolve pre-configured channel or auto-create a target channel for worker w."""
    if w.channel_spec is not None:
        w.target = await w.client.get_input_entity(w.channel_spec)
        w.channel_id = w.channel_spec
        return

    saved = load_saved_channels()
    if str(w.index) in saved:
        saved_id = int(saved[str(w.index)])
        try:
            w.target = await w.client.get_input_entity(PeerChannel(saved_id))
            w.channel_id = saved_id
            return
        except Exception:
            log.warning("[Acc #%d] Saved channel %s not found, creating a new one", w.index, saved_id)

    try:
        result = await w.client(
            CreateChannelRequest(title=f"{CHANNEL_TITLE} #{w.index}", about="", broadcast=True)
        )
    except ChannelsTooMuchError:
        w.disabled = True
        await notify_once(
            f"channels-too-much:{w.index}",
            f"[Acc #{w.index} ({w.name})] Too many channels/groups. Account disabled until /resume.",
            client=w.client,
        )
        raise

    created = result.chats[0]
    w.target = await w.client.get_input_entity(created)
    w.channel_id = created.id
    save_channel_id(w.index, created.id)
    log.info("[Acc #%d (%s)] Auto-created channel (ID: %s)", w.index, w.name, created.id)


async def try_claim_task(w: AccountWorker, username: str) -> None:
    """Non-blocking single-check task launched via asyncio.create_task."""
    global paused
    if claimed_event.is_set() or paused or w.disabled:
        return

    w.in_flight = True
    try:
        if w.target is None:
            await ensure_worker_channel(w)

        available = await w.client(CheckUsernameRequest(w.target, username))
        if not available:
            log.info("[Acc #%d (%s)] @%s is taken", w.index, w.name, username)
            return

        # Atomic lock via claimed_event: stop other accounts immediately
        if claimed_event.is_set():
            return
        claimed_event.set()
        log.info("[Acc #%d (%s)] @%s is AVAILABLE! Locking pool & claiming...", w.index, w.name, username)

        try:
            await w.client(UpdateUsernameRequest(w.target, username))
        except UsernameNotModifiedError:
            pass  # channel already holds this username
        except Exception as claim_err:
            # Rollback claimed_event so other accounts can retry immediately if still free
            claimed_event.clear()
            raise claim_err

        # Claim succeeded!
        save_usernames([n for n in load_usernames() if n != username])
        if w.channel_spec is None:
            remove_saved_channel(w.index)
            w.target = None

        msg = (
            f"Claimed @{username} (https://t.me/{username})\n"
            f"Winning Account: #{w.index} ({w.name})\n"
            f"Channel ID: {w.channel_id}"
        )
        log.info(msg.replace("\n", " | "))
        await notify(msg, client=w.client)

        if EXIT_ON_CLAIM:
            shutdown_event.set()
        else:
            paused = True
            claimed_event.clear()

    except FloodWaitError as e:
        cooldown[w.index] = time.time() + e.seconds
        log.warning(
            "[Acc #%d (%s)] FloodWait %ds on @%s -> skipping until %s",
            w.index,
            w.name,
            e.seconds,
            username,
            time.strftime("%H:%M:%S", time.localtime(cooldown[w.index])),
        )
    except UsernameOccupiedError:
        log.warning(
            "[Acc #%d (%s)] @%s passed check but UpdateUsername was refused (reserved/Fragment)",
            w.index,
            w.name,
            username,
        )
        await notify_once(
            f"occupied:{username}",
            f"@{username} shows free on check, but Telegram refused assignment (reserved/Fragment/cooldown).",
            client=w.client,
        )
    except UsernameInvalidError:
        invalid_usernames.add(username)
        log.warning("[Acc #%d (%s)] @%s is invalid; skipping", w.index, w.name, username)
        await notify_once(
            f"invalid:{username}",
            f"@{username} is invalid. Use /remove {username} to delete it from the watch list.",
            client=w.client,
        )
    except ChannelsAdminPublicTooMuchError:
        w.disabled = True
        log.warning("[Acc #%d (%s)] Too many public channels; disabling worker", w.index, w.name)
        await notify_once(
            f"too-many-public:{w.index}",
            f"[Acc #{w.index} ({w.name})] Owns too many public channels. Disabled until freed.",
            client=w.client,
        )
    except ChatAdminRequiredError:
        w.disabled = True
        await notify_once(
            f"admin-required:{w.index}",
            f"[Acc #{w.index} ({w.name})] Must be owner/admin of channel {w.channel_id}.",
            client=w.client,
        )
    except RPCError as e:
        log.warning("[Acc #%d (%s)] RPCError on @%s: %s", w.index, w.name, username, e)
    except Exception:
        log.exception("[Acc #%d (%s)] Unexpected error checking @%s", w.index, w.name, username)
    finally:
        w.in_flight = False


async def monitor_pool() -> None:
    """Global 1.0-second Round-Robin loop firing 1 account per second via asyncio.create_task."""
    account_cursor = 0
    username_cursor = 0
    log.info(
        "Round-Robin monitor started (%d accounts, %.1fs global interval)",
        len(workers),
        CHECK_INTERVAL,
    )

    while not shutdown_event.is_set():
        tick_start = time.monotonic()

        if not paused and not claimed_event.is_set():
            usernames = load_usernames()
            if usernames and workers:
                now = time.time()
                selected_worker: AccountWorker | None = None

                # Select the next healthy (non-cooldown, non-busy) account in Round-Robin order
                for _ in range(len(workers)):
                    candidate = workers[account_cursor % len(workers)]
                    account_cursor = (account_cursor + 1) % len(workers)
                    if is_worker_healthy(candidate, now):
                        selected_worker = candidate
                        break

                if selected_worker is not None:
                    target_username = usernames[username_cursor % len(usernames)]
                    username_cursor = (username_cursor + 1) % len(usernames)

                    # Non-blocking fire-and-forget check so slow RPC never delays the 1.0s timer
                    task = asyncio.create_task(try_claim_task(selected_worker, target_username))
                    in_flight_tasks.add(task)
                    task.add_done_callback(in_flight_tasks.discard)
                else:
                    ready_times = [cooldown.get(w.index, 0.0) for w in workers if not w.disabled]
                    wait_left = max(0.0, min(ready_times, default=now) - now)
                    log.warning("No healthy account free right now (earliest cooldown ends in %.0fs)", wait_left)

        elapsed = time.monotonic() - tick_start
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=max(0.05, CHECK_INTERVAL - elapsed),
            )
        except asyncio.TimeoutError:
            pass


def register_commands(primary_client: TelegramClient) -> None:
    """Preserve /add, /remove, /list, /resume owner commands on primary client."""

    @primary_client.on(
        events.NewMessage(
            chats=OWNER, from_users=OWNER, pattern=r"^/(add|remove|list|resume)\b"
        )
    )
    async def commands(event: events.NewMessage.Event) -> None:
        global paused
        command = event.pattern_match.group(1)
        parts = event.raw_text.split(maxsplit=1)
        arg = normalize(parts[1]) if len(parts) > 1 else ""
        names = load_usernames()

        if command == "add" and arg:
            invalid_usernames.discard(arg)
            if arg not in names:
                names.append(arg)
                save_usernames(names)
            notified.discard(f"invalid:{arg}")
            await event.reply(f"Added @{arg}. Watching {len(names)} username(s).")
        elif command == "remove" and arg:
            invalid_usernames.discard(arg)
            save_usernames([n for n in names if n != arg])
            await event.reply(f"Removed @{arg}.")
        elif command == "list":
            now = time.time()
            status = "claimed/stopping" if claimed_event.is_set() else ("paused" if paused else "running")
            uname_body = "\n".join(f"• @{n}" for n in names) or "(empty)"
            acc_lines = []
            for w in workers:
                cd_until = cooldown.get(w.index, 0.0)
                if w.disabled:
                    state = "disabled"
                elif cd_until > now:
                    state = f"cooldown ({int(cd_until - now)}s left)"
                else:
                    state = "healthy"
                acc_lines.append(f"#{w.index} {w.name}: {state} (ch: {w.channel_id})")
            acc_body = "\n".join(acc_lines)
            await event.reply(f"Status: {status}\n\nUsernames:\n{uname_body}\n\nPool:\n{acc_body}")
        elif command == "resume":
            paused = False
            claimed_event.clear()
            notified.clear()
            invalid_usernames.clear()
            for w in workers:
                w.disabled = False
            await event.reply("Monitoring resumed across all healthy accounts.")
        else:
            await event.reply("Usage: /add <username>, /remove <username>, /list, /resume")


async def main() -> None:
    global workers
    workers = build_workers()

    # Connect and initialize each account with a staggered startup delay
    active_workers: list[AccountWorker] = []
    for idx, w in enumerate(workers):
        try:
            await w.client.start()
            me = await w.client.get_me()
            w.name = getattr(me, "first_name", None) or getattr(me, "username", None) or f"Acc{w.index}"
            await w.client.get_dialogs(limit=50)
            await ensure_worker_channel(w)
            active_workers.append(w)
            log.info("[Acc #%d (%s)] Ready (Target Channel ID: %s)", w.index, w.name, w.channel_id)
            if idx < len(workers) - 1:
                await asyncio.sleep(random.uniform(1.5, 3.0))  # staggered delay for channel/session init
        except Exception:
            log.exception("[Acc #%d] Failed to initialize during startup", w.index)

    workers = active_workers
    if not workers:
        log.error("No healthy accounts initialized. Exiting.")
        return

    register_commands(workers[0].client)

    try:
        await monitor_pool()
    finally:
        if in_flight_tasks:
            await asyncio.gather(*in_flight_tasks, return_exceptions=True)
        for w in workers:
            try:
                await w.client.disconnect()
                log.info("[Acc #%d (%s)] Disconnected cleanly", w.index, w.name)
            except Exception:
                log.exception("[Acc #%d] Error during disconnect", w.index)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Stopped by user.")
