import asyncio
import os
from posix import wait3

from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from test.test_bufio import lengths

# Replace with your credentials
api_id = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]

private_channel_id = os.environ.get("TG_CHANNEL_ID")
if private_channel_id is None:
    private_channel_id = input("Enter channel ID: ").strip()
else:
    private_channel_id = private_channel_id.strip()
if not private_channel_id.startswith("-100"):
    private_channel_id = f"-100{private_channel_id.lstrip('-')}"
private_channel_id = int(private_channel_id)


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


async def download_message(client, message, channel_folder):
    if not message:
        return

    message_folder = channel_folder

    if message.text:
        write_text_file(channel_folder, message)

    if message.media:
        await download_media(client, message, message_folder)


def write_text_file(channel_folder, message):
    text_file_path = os.path.join(channel_folder, f"{message.id}_message.txt")
    with open(text_file_path, "w", encoding="utf-8", closefd=True) as text_file:
        text_file.write(message.text)
        text_file.close()


async def download_media(client, message, folder):
    file_resolution = await media_type(message)
    file_name = (
        os.path.join(folder, f"{message.id}_media.{file_resolution}")
        if file_resolution
        else folder
    )
    await client.download_media(message, file_name)


async def main():
    base_folder = "downloads"
    os.makedirs(base_folder, exist_ok=True)

    channel_folder = os.path.join(base_folder, f"channel_{private_channel_id}")
    os.makedirs(channel_folder, exist_ok=True)

    tasks = []
    task_count = 0
    async for message in client.iter_messages(private_channel_id):
        if task_count == 20:
            await asyncio.gather(*tasks)
            tasks = []
            task_count = 0
        tasks.append(download_message(client, message, channel_folder))
        task_count += 1

    await asyncio.gather(*tasks)


with client:
    client.loop.run_until_complete(main())
