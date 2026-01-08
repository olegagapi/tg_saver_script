import argparse
import os

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from telethon import TelegramClient
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
args = parser.parse_args()

if args.channel_id:
    private_channel_id = args.channel_id.strip()
else:
    private_channel_id = input("Enter channel ID: ").strip()
if not private_channel_id.startswith("-100"):
    private_channel_id = f"-100{private_channel_id.lstrip('-')}"
private_channel_id = int(private_channel_id)
min_message_id = args.m

client = TelegramClient("session_name", api_id, api_hash)


async def media_type(message):
    if isinstance(message.media, MessageMediaPhoto):
        return "jpg"
    elif isinstance(message.media, MessageMediaDocument):
        if message.media.document.mime_type.startswith("video/"):
            return "mp4"
        else:
            return ""
    else:
        return ""


async def main():
    base_folder = "downloads"

    # Ensure base folder exists
    os.makedirs(base_folder, exist_ok=True)

    channel_folder = os.path.join(base_folder, f"channel_{private_channel_id}")

    # Check if folder exists and --over flag is not set
    if os.path.exists(channel_folder) and not args.over:
        print(
            f"Error: Folder '{channel_folder}' already exists. Use --over to overwrite files."
        )
        return

    async for message in client.iter_messages(
        private_channel_id, min_id=min_message_id
    ):
        if not message:
            continue

        os.makedirs(channel_folder, exist_ok=True)
        # Create a folder for each message using its ID
        # message_folder = os.path.join(channel_folder, f'message_{message.id}')
        # Or dump all messages in the same folder
        message_folder = channel_folder

        # Save message text if available
        if message.text:
            text_file_path = os.path.join(channel_folder, f"{message.id}_message.txt")
            with open(text_file_path, "w", encoding="utf-8") as text_file:
                text_file.write(message.text)
            # print(f"Saved text: {text_file_path}")

        # Save media if available
        if message.media:
            file_resolution = await media_type(message)
            if file_resolution != "":
                file_name = os.path.join(
                    message_folder, f"{message.id}_media.{file_resolution}"
                )
                media_file_path = await client.download_media(message, file_name)
            else:
                media_file_path = await client.download_media(message, message_folder)
            # print(f"Saved media: {media_file_path}")


with client:
    client.loop.run_until_complete(main())
