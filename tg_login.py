"""Telegram login for history_download.py.

  ./tg login status              # show login state
  ./tg login qr                  # scan QR from the app (recommended)
  ./tg login send +380XXXXXXXXX  # request a login code
  ./tg login code 12345          # finish sign-in with the code

If two-step verification is enabled you are prompted for the password
(input is hidden). An attempt left unfinished by a previous run is
discarded automatically and the login starts over.

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

os.chdir(os.path.dirname(os.path.abspath(__file__)))  # .env and the session file live here

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient, functions
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RPCError,
    SessionPasswordNeededError,
)

SESSION = "session_name"
STATE = ".login_state.json"

if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
    sys.exit("TG_API_ID and TG_API_HASH must be set (in .env or the environment).")


def new_client():
    return TelegramClient(SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])


client = new_client()


def done(me):
    print(f"Logged in as {me.first_name} (id {me.id}). Session saved.")


async def reset_session():
    """Discard the current session file and reconnect with a fresh one."""
    global client
    await client.disconnect()
    client.session.delete()
    client = new_client()
    await client.connect()


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


async def status():
    try:
        await client(functions.updates.GetStateRequest())
    except SessionPasswordNeededError:
        print("Not logged in: a previous attempt is waiting for the 2FA password.")
        print("Run `qr` or `code` to start over.")
        return
    except RPCError:
        print("Not logged in.")
        if os.path.exists(STATE):
            with open(STATE) as f:
                phone = json.load(f)["phone"]
            print(f"Pending login code for {phone[:4]}***{phone[-2:]}. Run: ./tg login code <code>")
        else:
            print("Run: ./tg login qr")
        return

    me = await client.get_me()
    handle = f" @{me.username}" if me.username else ""
    print(f"Logged in as {me.first_name}{handle} (id {me.id}, phone +{me.phone})")
    auths = await client(functions.account.GetAuthorizationsRequest())
    this = next((a for a in auths.authorizations if a.current), None)
    if this:
        print(f"This session: {this.app_name} {this.app_version} on {this.device_model}, "
              f"created {this.date_created:%Y-%m-%d}")
    print(f"Active sessions on the account: {len(auths.authorizations)}")
    print(f"Session file: {SESSION}.session")


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
    print("Then run: ./tg login code <code>")


async def code(value):
    if await client.is_user_authorized():
        print("Already logged in.")
        return
    try:
        with open(STATE) as f:
            st = json.load(f)
    except FileNotFoundError:
        sys.exit("No pending code request. Run: ./tg login send <phone>")
    try:
        me = await client.sign_in(st["phone"], value, phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        me = await ask_2fa()
    except PhoneCodeInvalidError:
        sys.exit("Wrong code. Check the Telegram service chat and try again.")
    except PhoneCodeExpiredError:
        os.remove(STATE)
        sys.exit("Code expired. Run: ./tg login send <phone>")
    os.remove(STATE)
    done(me)


COMMANDS = {"status": lambda arg: status(), "qr": lambda arg: qr(),
            "send": send, "code": code}


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd not in COMMANDS or (cmd in ("send", "code") and not arg):
        print(__doc__)
        return
    await client.connect()
    try:
        for attempt in range(2):
            try:
                await COMMANDS[cmd](arg)
                break
            except SessionPasswordNeededError:
                # Only reaches here when a previous run was aborted between the
                # QR scan / code and the 2FA password. The commands handle their
                # own 2FA prompt, so this state is stale: discard it and retry.
                if attempt:
                    raise
                print("Discarding an unfinished login attempt and starting over.")
                await reset_session()
    except FloodWaitError as e:
        sys.exit(f"Telegram rate limit: wait {e.seconds}s and try again.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
