"""Non-interactive two-step Telegram login for history_download.py.

  python tg_login.py send +380XXXXXXXXX        # request a login code
  python tg_login.py code 12345 [2fa-password] # finish sign-in
"""
import asyncio
import json
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

STATE = ".login_state.json"
client = TelegramClient("session_name", int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])


async def send(phone):
    await client.connect()
    if await client.is_user_authorized():
        print("Already logged in.")
        return
    sent = await client.send_code_request(phone)
    json.dump({"phone": phone, "hash": sent.phone_code_hash}, open(STATE, "w"))
    print("Code sent. Check your Telegram app, then run: python tg_login.py code <code>")


async def code(value, password=None):
    st = json.load(open(STATE))
    await client.connect()
    try:
        await client.sign_in(st["phone"], value, phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        if not password:
            print("2FA enabled. Re-run: python tg_login.py code <code> <password>")
            return
        await client.sign_in(password=password)
    me = await client.get_me()
    os.remove(STATE)
    print(f"Logged in as {me.first_name} (id {me.id}). Session saved.")


async def main():
    cmd = sys.argv[1]
    if cmd == "send":
        await send(sys.argv[2])
    elif cmd == "code":
        await code(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print(__doc__)
    await client.disconnect()


asyncio.run(main())
