import asyncio
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import SQLiteSession, StringSession

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

def set_env_key(key: str, value: str) -> None:
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, text, flags=re.MULTILINE):
        text = re.sub(pattern, f"{key}={value}", text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text)

async def main():
    idx = 1
    if Path("usernameclaim.session").exists() and not os.getenv("SESSION_1"):
        s = StringSession.save(SQLiteSession("usernameclaim"))
        if s:
            set_env_key("SESSION_1", s)
            print("✅ Existing usernameclaim.session auto-saved as SESSION_1 in .env")
            idx = 2

    while idx <= 10:
        if os.getenv(f"SESSION_{idx}"):
            print(f"ℹ️ SESSION_{idx} already set in .env, skipping...")
            idx += 1
            continue
        ans = input(f"\nLogin Account #{idx}? (y/n): ").strip().lower()
        if ans != "y":
            break
        async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
            sess_str = client.session.save()
            set_env_key(f"SESSION_{idx}", sess_str)
            me = await client.get_me()
            print(f"✅ Account #{idx} ({me.first_name}) saved as SESSION_{idx} in .env!")
        idx += 1

asyncio.run(main())
