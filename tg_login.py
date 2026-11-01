"""Telegram login for history_download.py.

  python tg_login.py qr                  # scan QR from the app (recommended)
  python tg_login.py send +380XXXXXXXXX  # request a login code
  python tg_login.py code 12345          # finish sign-in with the code

If two-step verification is enabled you are prompted for the password
(input is hidden).

QR login: open Telegram on your phone -> Settings -> Devices -> Link Desktop Device,
then scan the QR printed in the terminal.
"""
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime, timezone

import qrcode

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

SESSION = "session_name"
STATE = ".login_state.json"

if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
    sys.exit("TG_API_ID and TG_API_HASH must be set (in .env or the environment).")

client = TelegramClient(SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])


def done(me):
    print(f"Logged in as {me.first_name} (id {me.id}). Session saved.")


async def ask_2fa(attempts=3):
    """Prompt for the two-step verification password (hidden input)."""
    for _ in range(attempts):
        password = getpass.getpass("Two-step verification password: ")
        if not password:
            continue
        try:
            return await client.sign_in(password=password)
        except PasswordHashInvalidError:
            print("Wrong password, try again.")
    sys.exit("Too many failed password attempts.")


async def qr(attempts=6):
    if await client.is_user_authorized():
        print("Already logged in.")
        return
    login = await client.qr_login()
    for i in range(attempts):
        print("\033c", end="")  # clear screen so only the current QR is visible
        q = qrcode.QRCode(border=1)
        q.add_data(login.url)
        q.print_ascii(invert=True)
        ttl = int((login.expires - datetime.now(timezone.utc)).total_seconds())
        print("\nTelegram -> Settings -> Devices -> Link Desktop Device -> scan this.")
        print(f"QR expires in {ttl}s. Attempt {i + 1}/{attempts}.")
        try:
            done(await login.wait())  # waits until the token expires
            return
        except SessionPasswordNeededError:
            done(await ask_2fa())
            return
        except asyncio.TimeoutError:
            await login.recreate()
    sys.exit("QR not scanned in time. Run again.")


async def send(phone):
    if await client.is_user_authorized():
        print("Already logged in.")
        return
    sent = await client.send_code_request(phone)
    with open(STATE, "w") as f:
        json.dump({"phone": phone, "hash": sent.phone_code_hash}, f)
    via = type(sent.type).__name__.replace("SentCodeType", "")
    print(f"Code sent via: {via}" + (f" (expires in {sent.timeout}s)" if sent.timeout else ""))
    if sent.next_type:
        print(f"Fallback available: {type(sent.next_type).__name__.replace('CodeType', '')}")
    print("Then run: python tg_login.py code <code>")


async def code(value):
    if await client.is_user_authorized():
        print("Already logged in.")
        return
    try:
        with open(STATE) as f:
            st = json.load(f)
    except FileNotFoundError:
        sys.exit("No pending code request. Run: python tg_login.py send <phone>")
    try:
        me = await client.sign_in(st["phone"], value, phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        me = await ask_2fa()
    except PhoneCodeInvalidError:
        sys.exit("Wrong code. Check the Telegram service chat and try again.")
    except PhoneCodeExpiredError:
        os.remove(STATE)
        sys.exit("Code expired. Run: python tg_login.py send <phone>")
    os.remove(STATE)
    done(me)


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "qr":
        action = qr()
    elif cmd == "send" and arg:
        action = send(arg)
    elif cmd == "code" and arg:
        action = code(arg)
    else:
        print(__doc__)
        return
    await client.connect()
    try:
        await action
    except SessionPasswordNeededError:
        sys.exit(f"A previous login was left unfinished. Delete {SESSION}.session and run again.")
    except FloodWaitError as e:
        sys.exit(f"Telegram rate limit: wait {e.seconds}s and try again.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
