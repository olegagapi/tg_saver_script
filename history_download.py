import argparse
import asyncio
import os
import re

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

if load_dotenv:
    load_dotenv()

# Replace with your credentials
api_id = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]

parser = argparse.ArgumentParser(description="Download Telegram channel history")
parser.add_argument("channel_id", nargs="?", help="Channel ID to download from")
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

if args.channel_id:
    private_channel_id = args.channel_id.strip()
else:
    private_channel_id = input("Enter channel ID: ").strip()
if not private_channel_id.startswith("-100"):
    private_channel_id = f"-100{private_channel_id.lstrip('-')}"
private_channel_id = int(private_channel_id)
if args.new and args.m is not None:
    parser.error("--new and -m are mutually exclusive")


def get_latest_message_id(channel_folder):
    if not os.path.exists(channel_folder):
        return None
    max_id = 0
    for filename in os.listdir(channel_folder):
        match = re.match(r"^(\d+)_", filename)
        if match:
            max_id = max(max_id, int(match.group(1)))
    return max_id if max_id > 0 else None


min_message_id = args.m

if args.new:
    channel_folder = os.path.join("downloads", f"channel_{private_channel_id}")
    latest_id = get_latest_message_id(channel_folder)
    if latest_id is not None:
        min_message_id = latest_id
        print(f"Resuming from message ID {latest_id}")
    else:
        print("Warning: no existing downloads found, downloading everything")

client = TelegramClient(
    "session_name", api_id, api_hash,
    timeout=120,
    flood_sleep_threshold=120,
)


def media_type(message):
    if isinstance(message.media, MessageMediaPhoto):
        return "jpg"
    elif isinstance(message.media, MessageMediaDocument):
        if message.media.document.mime_type.startswith("video/"):
            return "mp4"
        else:
            return ""
    else:
        return ""


def save_text(channel_folder, message):
    if not message.text:
        return
    text_file_path = os.path.join(channel_folder, f"{message.id}_message.txt")
    with open(text_file_path, "w", encoding="utf-8") as text_file:
        text_file.write(message.text)


async def save_media(client, message, channel_folder):
    if not message.media:
        return
    if args.skip_pic and isinstance(message.media, MessageMediaPhoto):
        return
    if args.skip_vid and isinstance(message.media, MessageMediaDocument):
        if message.media.document.mime_type.startswith("video/"):
            return
    file_resolution = media_type(message)
    if file_resolution != "":
        file_name = os.path.join(channel_folder, f"{message.id}_media.{file_resolution}")
        await client.download_media(message, file_name)
    else:
        await client.download_media(message, channel_folder)


async def download_message(client, message, channel_folder, semaphore, max_retries=3):
    async with semaphore:
        for attempt in range(max_retries):
            try:
                save_text(channel_folder, message)
                await save_media(client, message, channel_folder)
                return message.id
            except TimeoutError:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"TimeoutError on message {message.id}, retrying in {wait}s...")
                    await asyncio.sleep(wait)
            except FloodWaitError as e:
                print(f"FloodWaitError on message {message.id}, sleeping {e.seconds}s...")
                await asyncio.sleep(e.seconds)
            except ConnectionError:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"ConnectionError on message {message.id}, retrying in {wait}s...")
                    await asyncio.sleep(wait)
        print(f"Failed to download message {message.id} after {max_retries} retries")
        return None


async def main():
    base_folder = "downloads"
    os.makedirs(base_folder, exist_ok=True)

    channel_folder = os.path.join(base_folder, f"channel_{private_channel_id}")

    if os.path.exists(channel_folder) and not args.over and not args.new:
        print(
            f"Error: Folder '{channel_folder}' already exists. Use --over to overwrite files."
        )
        return

    os.makedirs(channel_folder, exist_ok=True)

    iter_kwargs = {}
    if min_message_id is not None:
        iter_kwargs["min_id"] = min_message_id

    jobs = args.jobs
    semaphore = asyncio.Semaphore(jobs)
    batch_size = jobs * 5

    total = 0
    succeeded = 0
    failed_ids = []

    batch = []
    async for message in client.iter_messages(private_channel_id, **iter_kwargs):
        if not message:
            continue
        batch.append(message)
        if len(batch) >= batch_size:
            results = await asyncio.gather(
                *(download_message(client, msg, channel_folder, semaphore) for msg in batch)
            )
            for msg, result in zip(batch, results):
                total += 1
                if result is not None:
                    succeeded += 1
                else:
                    failed_ids.append(msg.id)
            print(f"Progress: {total} messages processed")
            batch = []

    if batch:
        results = await asyncio.gather(
            *(download_message(client, msg, channel_folder, semaphore) for msg in batch)
        )
        for msg, result in zip(batch, results):
            total += 1
            if result is not None:
                succeeded += 1
            else:
                failed_ids.append(msg.id)

    print(f"\nDone: {succeeded}/{total} messages downloaded successfully")
    if failed_ids:
        print(f"Failed message IDs: {failed_ids}")


with client:
    client.loop.run_until_complete(main())
