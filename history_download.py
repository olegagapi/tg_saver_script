"""Download Telegram channel history or a single post.

  python history_download.py <channel>                       # whole channel
  python history_download.py <channel> --new                 # only messages newer than the last downloaded
  python history_download.py <channel> -m 1234               # stop at message id 1234
  python history_download.py https://t.me/c/1234567890/42    # one post (the whole album if it is grouped)

<channel> is a channel id (-100... or bare digits), an alias from aliases.toml,
or a public @username. Files go to downloads/channel_<id>/.
"""
import argparse
import asyncio
import os
import re
import sys
import tomllib

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageExtendedMedia,
    MessageMediaDocument,
    MessageMediaPaidMedia,
    MessageMediaPhoto,
)

if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
    sys.exit("TG_API_ID and TG_API_HASH must be set (in .env or the environment).")

SESSION = "session_name"
ALIASES = "aliases.toml"
BASE_FOLDER = "downloads"

MSG_LINK = re.compile(
    r"^(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z]\w{3,}))/(?:\d+/)?(\d+)/?(?:\?.*)?$"
)
CHANNEL_LINK = re.compile(r"^(?:https?://)?t\.me/([A-Za-z]\w{3,})/?$")

parser = argparse.ArgumentParser(
    description="Download Telegram channel history or a single post",
    epilog=f"Aliases are read from {ALIASES} (see aliases.example.toml).",
)
parser.add_argument(
    "target", nargs="?",
    help="channel id, alias, @username, or a t.me message link for a single post",
)
parser.add_argument(
    "-m", type=int, default=None, help="Start from latest and stop at this message ID"
)
parser.add_argument(
    "--over", action="store_true", help="Overwrite files in existing folder"
)
parser.add_argument(
    "--new", action="store_true",
    help="Download only new messages since latest downloaded"
)
parser.add_argument(
    "-j", "--jobs", type=int, default=3,
    help="Number of concurrent downloads (default: 3)"
)
parser.add_argument(
    "--skip-vid", action="store_true", help="Skip downloading video files"
)
parser.add_argument(
    "--skip-pic", action="store_true", help="Skip downloading photo files"
)
args = parser.parse_args()

if args.new and args.m is not None:
    parser.error("--new and -m are mutually exclusive")
if args.jobs < 1:
    parser.error("--jobs must be at least 1")


def load_aliases():
    try:
        with open(ALIASES, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{ALIASES}: {e}")
    bad = [k for k, v in data.items() if not isinstance(v, (int, str))]
    if bad:
        sys.exit(f"{ALIASES}: values must be a channel id or @username, check: {', '.join(bad)}")
    return data


def parse_target(value, aliases):
    """Return (peer, message_id). peer is an int channel id or a username;
    message_id is None unless value is a link to a single post."""
    value = str(value).strip()
    if value in aliases:
        return parse_target(aliases[value], {})  # no alias chains

    m = MSG_LINK.match(value)
    if m:
        internal_id, username, msg_id = m.groups()
        peer = int(f"-100{internal_id}") if internal_id else username
        return peer, int(msg_id)

    m = CHANNEL_LINK.match(value)
    if m:
        return m.group(1), None
    if value.startswith("@"):
        return value[1:], None

    digits = value.lstrip("-")
    if digits.isdigit():
        if not digits.startswith("100"):
            digits = f"100{digits}"
        return int(f"-{digits}"), None

    hint = f"Known aliases: {', '.join(sorted(aliases))}" if aliases else f"No {ALIASES} found."
    sys.exit(f"'{value}' is not a channel id, alias, @username or t.me link. {hint}")


def get_latest_message_id(channel_folder):
    if not os.path.isdir(channel_folder):
        return None
    ids = [int(m.group(1)) for name in os.listdir(channel_folder)
           if (m := re.match(r"^(\d+)_", name))]
    return max(ids) if ids else None


def media_items(message):
    """Downloadable media objects in a message.

    Spoiler-covered media ("click to reveal") is ordinary media with a flag, so it
    needs nothing special. Paid media is a wrapper: once purchased, the real
    photos/videos sit inside it; before purchase there are only blurred previews.
    """
    media = message.media
    if isinstance(media, MessageMediaPaidMedia):
        items = [x.media for x in media.extended_media if isinstance(x, MessageExtendedMedia)]
        if not items:
            print(f"Message {message.id}: paid media not purchased "
                  f"({media.stars_amount} stars), only the text is saved")
        return items
    if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)) and message.file:
        return [media]
    return []  # polls, geo, link previews, ...


def is_video(media):
    doc = getattr(media, "document", None)
    return bool(doc) and (doc.mime_type or "").startswith("video/")


def media_path(message, media, channel_folder, index=None):
    """Target path for one media object of the message."""
    suffix = f"_{index}" if index is not None else ""
    ext = utils.get_extension(media)
    doc = getattr(media, "document", None)
    if isinstance(media, MessageMediaPhoto) or is_video(media) or not doc:
        return os.path.join(channel_folder, f"{message.id}_media{suffix}{ext}")
    name = next((os.path.basename(a.file_name) for a in doc.attributes
                 if isinstance(a, DocumentAttributeFilename) and a.file_name), f"media{ext}")
    return os.path.join(channel_folder, f"{message.id}{suffix}_{name}")


def save_text(channel_folder, message):
    if not message.text:
        return
    with open(os.path.join(channel_folder, f"{message.id}_message.txt"), "w",
              encoding="utf-8") as text_file:
        text_file.write(message.text)


async def save_media(client, message, channel_folder):
    items = media_items(message)
    for i, media in enumerate(items, 1):
        if args.skip_pic and isinstance(media, MessageMediaPhoto):
            continue
        if args.skip_vid and is_video(media):
            continue
        index = i if len(items) > 1 else None
        await client.download_media(media, media_path(message, media, channel_folder, index))


async def download_message(client, message, channel_folder, semaphore, max_retries=3):
    async with semaphore:
        for attempt in range(max_retries):
            try:
                save_text(channel_folder, message)
                await save_media(client, message, channel_folder)
                return message.id
            except FloodWaitError as e:
                print(f"FloodWaitError on message {message.id}, sleeping {e.seconds}s...")
                await asyncio.sleep(e.seconds)
            except (TimeoutError, ConnectionError) as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"{type(e).__name__} on message {message.id}, retrying in {wait}s...")
                    await asyncio.sleep(wait)
            except (RPCError, OSError) as e:
                print(f"Error on message {message.id}: {type(e).__name__}: {e}")
                return None
        print(f"Failed to download message {message.id} after {max_retries} retries")
        return None


async def download_all(client, messages, channel_folder):
    """Download a list of messages concurrently. Returns (succeeded, failed_ids)."""
    semaphore = asyncio.Semaphore(args.jobs)
    results = await asyncio.gather(
        *(download_message(client, msg, channel_folder, semaphore) for msg in messages)
    )
    failed = [msg.id for msg, ok in zip(messages, results) if ok is None]
    return len(messages) - len(failed), failed


async def download_post(client, entity, msg_id, channel_folder):
    message = await client.get_messages(entity, ids=msg_id)
    if message is None:
        sys.exit(f"Message {msg_id} not found in this channel.")
    messages = [message]
    if message.grouped_id:  # an album: fetch its siblings (albums hold at most 10 items)
        around = await client.get_messages(entity, ids=list(range(msg_id - 9, msg_id + 10)))
        messages = [m for m in around if m and m.grouped_id == message.grouped_id]
        print(f"Album with {len(messages)} items")
    os.makedirs(channel_folder, exist_ok=True)
    succeeded, failed = await download_all(client, messages, channel_folder)
    print(f"Done: {succeeded}/{len(messages)} messages saved to {channel_folder}")
    if failed:
        print(f"Failed message IDs: {failed}")


async def download_history(client, entity, channel_folder):
    min_id = args.m
    if args.new:
        latest_id = get_latest_message_id(channel_folder)
        if latest_id is not None:
            min_id = latest_id
            print(f"Resuming from message ID {latest_id}")
        else:
            print("Warning: no existing downloads found, downloading everything")
    elif os.path.exists(channel_folder) and not args.over:
        sys.exit(f"Error: folder '{channel_folder}' already exists. "
                 "Use --over to overwrite files or --new to fetch only new messages.")
    os.makedirs(channel_folder, exist_ok=True)

    iter_kwargs = {"min_id": min_id} if min_id is not None else {}
    batch_size = args.jobs * 5
    total = succeeded = 0
    failed_ids = []

    async def flush(batch):
        nonlocal total, succeeded
        ok, failed = await download_all(client, batch, channel_folder)
        total += len(batch)
        succeeded += ok
        failed_ids.extend(failed)
        print(f"Progress: {total} messages processed")

    batch = []
    async for message in client.iter_messages(entity, **iter_kwargs):
        batch.append(message)
        if len(batch) >= batch_size:
            await flush(batch)
            batch = []
    if batch:
        await flush(batch)

    print(f"\nDone: {succeeded}/{total} messages downloaded to {channel_folder}")
    if failed_ids:
        print(f"Failed message IDs: {failed_ids}")


async def main():
    aliases = load_aliases()
    target = args.target or input("Enter channel id, alias or link: ")
    peer, msg_id = parse_target(target, aliases)
    if msg_id is not None and (args.new or args.m is not None):
        parser.error("--new and -m do not apply to a single post link")

    client = TelegramClient(
        SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
        timeout=120, flood_sleep_threshold=120,
    )
    async with client:
        if not await client.is_user_authorized():
            sys.exit("Not logged in. Run: python tg_login.py qr")
        try:
            entity = await client.get_entity(peer)
        except (ValueError, RPCError) as e:
            sys.exit(f"Cannot access channel {peer!r}: {e}")
        channel_id = utils.get_peer_id(entity)
        print(f"Channel: {getattr(entity, 'title', peer)} ({channel_id})")
        channel_folder = os.path.join(BASE_FOLDER, f"channel_{channel_id}")

        if msg_id is not None:
            await download_post(client, entity, msg_id, channel_folder)
        else:
            await download_history(client, entity, channel_folder)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
