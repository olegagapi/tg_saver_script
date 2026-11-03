"""Download Telegram history: channels, groups, private chats, Saved Messages, or one post.

  ./tg --dialogs [N]                   # list recent chats with their targets (default 10)
  ./tg <target>                        # whole history
  ./tg <target> --new                  # only messages newer than the last downloaded
  ./tg <target> -m 1234                # stop at message id 1234
  ./tg https://t.me/c/1234567890/42    # one post (the whole album if it is grouped)
  ./tg <link> --tag regina             # same, into downloads/<peer>-regina/
  ./tg --update                        # --new for every history folder in downloads/

<target> is one of:
  me                 your Saved Messages            -> downloads/saved_messages/
  @username          public channel, group or user
  user:123           a person by id                 -> downloads/user_123/
  chat:123           a basic group by id            -> downloads/chat_-123/
  channel:123        a channel/supergroup by id     -> downloads/channel_-100123/
  -1001234567890     channel id as shown by Telegram clients (bare digits mean the same)
  an alias from aliases.toml, or a t.me link.

Channel posts are saved as bare text; chats get a "date  sender:" header per message.
An album (several photos sent together) gets a single text file, named after its first item.
Every folder gets 00-info.txt (name, username, id, description, ...) and the profile
picture as 00-<username>.jpg, written before any message is fetched.

Single posts are saved to downloads/<peer>-<tag>/ (default tag: posts) so they stay apart
from full histories. --update refreshes only the plain history folders and skips any
folder with a -tag suffix.

A run that could not save everything ends with a summary naming what is missing, exits
with status 1 and leaves 00-failed.txt (message ids) and/or 00-gaps.txt (id ranges that
were never listed) in the folder. The next --new or --update run retries those first.
"""
import argparse
import asyncio
import logging
import os
import re
import sys
import tomllib
from collections import namedtuple
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))  # .env, session, downloads/ live here

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient, functions, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeFilename,
    MessageExtendedMedia,
    MessageMediaDocument,
    MessageMediaPaidMedia,
    MessageMediaPhoto,
    User,
)

SESSION = "session_name"
ALIASES = "aliases.toml"
BASE_FOLDER = "downloads"
INFO_PREFIX = "00-"  # sorts before every message id
FAILED_FILE = f"{INFO_PREFIX}failed.txt"  # ids the last run could not save
GAPS_FILE = f"{INFO_PREFIX}gaps.txt"      # id ranges the last run never got to list

# Telethon reports dropped connections ("Server closed the connection: ...") and
# reconnects on its own; prefix its lines so they are not mistaken for ours.
logging.basicConfig(level=logging.WARNING, format="telethon: %(message)s")

MSG_LINK = re.compile(
    r"^(?:https?://)?t\.me/(?:s/)?(?:c/(\d+)|([A-Za-z]\w{3,}))/(?:\d+/)?(\d+)/?(?:\?.*)?$"
)
CHANNEL_LINK = re.compile(r"^(?:https?://)?t\.me/([A-Za-z]\w{3,})/?$")
PREFIXED_ID = re.compile(r"^(user|chat|channel):(\d+)$")

parser = argparse.ArgumentParser(
    prog="tg",
    description=__doc__,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=f"Aliases are read from {ALIASES} (see aliases.example.toml).",
)
parser.add_argument(
    "target", nargs="?",
    help="me, @username, user:ID, chat:ID, channel:ID, alias, or a t.me link",
)
parser.add_argument(
    "--dialogs", nargs="?", const=10, type=int, metavar="N",
    help="list the N most recent chats (default 10) with their targets, then exit",
)
parser.add_argument(
    "--update", action="store_true",
    help="download new messages for every history folder in downloads/ "
         "(folders with a -tag suffix are skipped), then exit",
)
parser.add_argument(
    "--tag", default="posts", metavar="NAME",
    help="folder suffix for single-post downloads: downloads/<peer>-NAME/ (default: posts)",
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

if args.target is None and args.dialogs is None and not args.update:
    parser.print_help()
    sys.exit(2)
if args.update and (args.target or args.new or args.over or args.m is not None):
    parser.error("--update takes no target and no -m/--new/--over")
if not re.fullmatch(r"[A-Za-z0-9_]+", args.tag):
    parser.error("--tag may only contain letters, digits and _")
if args.new and args.m is not None:
    parser.error("--new and -m are mutually exclusive")
if args.jobs < 1:
    parser.error("--jobs must be at least 1")
if args.dialogs is not None and args.dialogs < 1:
    parser.error("--dialogs needs a positive number")
if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
    sys.exit("TG_API_ID and TG_API_HASH must be set (in .env or the environment).")


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
        sys.exit(f"{ALIASES}: values must be an id, @username or target string, check: {', '.join(bad)}")
    return data


def parse_target(value, aliases):
    """Return (peer, message_id). peer is anything client.get_entity accepts:
    'me', a username, a phone, or a Telethon marked id (user 123, chat -123,
    channel -100123). message_id is None unless value links to a single post."""
    value = str(value).strip()
    if value in aliases:
        return parse_target(aliases[value], {})  # no alias chains

    if value == "me":
        return "me", None

    m = PREFIXED_ID.match(value)
    if m:
        kind, digits = m.groups()
        mark = {"user": "", "chat": "-", "channel": "-100"}[kind]
        return int(f"{mark}{digits}"), None

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
    if value.startswith("+") and value[1:].isdigit():
        return value, None  # phone number of a contact

    digits = value.lstrip("-")
    if digits.isdigit():
        if not digits.startswith("100"):
            digits = f"100{digits}"
        return int(f"-{digits}"), None

    hint = f"Known aliases: {', '.join(sorted(aliases))}" if aliases else f"No {ALIASES} found."
    sys.exit(f"'{value}' is not a valid target (me, @username, user:ID, chat:ID, "
             f"channel:ID, channel id, alias or t.me link). {hint}")


class Peer(namedtuple("Peer", "kind folder target internal_name")):
    """What we need to know about a resolved entity: its kind (me/user/bot/group/channel),
    the history folder name, a copy-pasteable target and a name for the picture file."""

    @property
    def with_header(self):
        return self.kind != "channel"  # chats record who said what; channel posts are bare


def describe(entity):
    peer_id = utils.get_peer_id(entity)
    username = getattr(entity, "username", None)
    if isinstance(entity, User):
        if entity.is_self:
            return Peer("me", "saved_messages", "me", username or "me")
        kind = "bot" if entity.bot else "user"
        target = f"@{username}" if username else f"user:{peer_id}"
        return Peer(kind, f"user_{peer_id}", target, username or f"user_{peer_id}")
    if isinstance(entity, Chat):
        return Peer("group", f"chat_{peer_id}", f"chat:{abs(peer_id)}", f"chat_{abs(peer_id)}")
    if isinstance(entity, Channel):
        kind = "group" if entity.megagroup else "channel"
        internal = str(peer_id)[4:]
        target = f"@{username}" if username else f"channel:{internal}"
        return Peer(kind, f"channel_{peer_id}", target, username or f"channel_{internal}")
    raise ValueError(f"unsupported peer type {type(entity).__name__}")


HISTORY_FOLDER = re.compile(r"^(?:saved_messages|user_(\d+)|chat_(-\d+)|channel_(-100\d+))$")


def peer_from_folder(name):
    """Inverse of describe(): the peer a plain history folder belongs to, else None."""
    m = HISTORY_FOLDER.match(name)
    if not m:
        return None
    user_id, chat_id, channel_id = m.groups()
    return int(user_id or chat_id or channel_id) if any(m.groups()) else "me"


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


def message_header(message):
    """'date  sender:' line plus a 'forwarded from' line when applicable."""
    sender = message.sender
    if sender is None:
        name = message.post_author or "?"
    elif getattr(sender, "deleted", False):
        name = "Deleted Account"
    else:
        name = utils.get_display_name(sender) or f"user {sender.id}"
    lines = [f"{message.date.astimezone():%Y-%m-%d %H:%M}  {name}:"]
    fwd = message.forward
    if fwd:
        origin = fwd.chat or fwd.sender
        name = utils.get_display_name(origin) if origin else fwd.from_name
        lines.append(f"forwarded from {name or 'hidden sender'}")
    return "\n".join(lines)


def save_text(channel_folder, group, with_header):
    """Write the text file for one message, or for one album (a list of messages that
    share a grouped_id): one file named after the first item, holding the header (chats
    only) and the caption. Album items without a caption of their own get no file."""
    first = group[0]
    text = "\n\n".join(m.text for m in group if m.text)
    if not text and not (with_header and first.media):
        return  # a channel post without text, or a service message: nothing to record
    parts = [message_header(first)] if with_header else []
    if text:
        parts.append(text)
    with open(os.path.join(channel_folder, f"{first.id}_message.txt"), "w",
              encoding="utf-8") as text_file:
        text_file.write("\n\n".join(parts))


def same_album(a, b):
    return a.grouped_id is not None and a.grouped_id == b.grouped_id


def album_safe_chunks(messages, size):
    """Split into batches of about `size` messages without cutting an album in two,
    so that every album is saved as one unit by a single download_all() call."""
    batch = []
    for message in messages:
        if len(batch) >= size and not same_album(batch[-1], message):
            yield batch
            batch = []
        batch.append(message)
    if batch:
        yield batch


async def save_media(client, message, channel_folder):
    items = media_items(message)
    for i, media in enumerate(items, 1):
        if args.skip_pic and isinstance(media, MessageMediaPhoto):
            continue
        if args.skip_vid and is_video(media):
            continue
        index = i if len(items) > 1 else None
        path = media_path(message, media, channel_folder, index)
        part = path + ".part"  # so an interrupted download is never mistaken for a finished file
        try:
            await client.download_media(media, part)
            os.replace(part, path)
        except BaseException:
            if os.path.exists(part):
                os.remove(part)
            raise


def read_state(folder):
    """What earlier runs left undone: (failed ids, gaps). A gap (below, above) means the
    messages with above < id < below were never listed because a run stopped early."""
    def rows(name):
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                return [line.split() for line in f if line.strip() and not line.startswith("#")]
        except FileNotFoundError:
            return []
    failed = {int(row[0]) for row in rows(FAILED_FILE)}
    gaps = [(int(below), int(above)) for below, above in rows(GAPS_FILE)]
    return failed, gaps


def write_state(folder, failed, gaps):
    """Persist what is still missing so the next --new run retries it; nothing -> no file."""
    files = {
        FAILED_FILE: ("# Messages that could not be saved. `./tg <target> --new` retries them.",
                      [str(i) for i in sorted(failed)]),
        GAPS_FILE: ("# Id ranges never listed because a run stopped early, as `below above`:\n"
                    "# messages with above < id < below are missing (0 = no bound).\n"
                    "# `./tg <target> --new` fills them.",
                    [f"{below} {above}" for below, above in gaps]),
    }
    for name, (header, lines) in files.items():
        path = os.path.join(folder, name)
        if lines:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join([header, *lines]) + "\n")
        elif os.path.exists(path):
            os.remove(path)


# Errors that end a listing: the connection is gone, Telegram refused the request, or
# Telethon gave up on it after its own retries (which surfaces as a bare ValueError).
LISTING_ERRORS = (ConnectionError, RPCError, ValueError, OSError)


async def download_message(client, message, channel_folder, with_header, semaphore,
                           text_group=None, max_retries=3):
    """Save one message. text_group is the list of messages whose text file this one
    writes (itself, or the whole album it opens); None when another item does it.
    Returns the message id, or None after reporting why it failed.
    Never raises: one broken message must not take the whole run down."""
    async with semaphore:
        attempt = 0
        while attempt < max_retries:
            try:
                if text_group:
                    save_text(channel_folder, text_group, with_header)
                await save_media(client, message, channel_folder)
                return message.id
            except FloodWaitError as e:  # waiting is not an attempt
                print(f"Message {message.id}: flood wait, sleeping {e.seconds}s...")
                await asyncio.sleep(e.seconds)
            except (TimeoutError, ConnectionError) as e:
                if not client.is_connected():  # Telethon gave up reconnecting
                    print(f"Message {message.id}: connection to Telegram is gone")
                    return None
                attempt += 1
                if attempt < max_retries:
                    wait = 2 ** attempt
                    print(f"Message {message.id}: {type(e).__name__}, retrying in {wait}s...")
                    await asyncio.sleep(wait)
            except (RPCError, OSError, ValueError) as e:
                print(f"Message {message.id}: {type(e).__name__}: {e}")
                return None
            except Exception as e:  # noqa: BLE001 - reported in the summary instead of a traceback
                print(f"Message {message.id}: unexpected {type(e).__name__}: {e}")
                return None
        print(f"Message {message.id}: giving up after {max_retries} connection errors")
        return None


async def download_all(client, messages, channel_folder, with_header):
    """Download a list of messages concurrently. Returns (succeeded, failed_ids)."""
    groups = {}  # an album is one group; a lone message is a group of its own
    for msg in messages:
        groups.setdefault(("album", msg.grouped_id) if msg.grouped_id else ("msg", msg.id),
                          []).append(msg)
    text_groups = {}  # id of the first item -> its group
    for group in groups.values():
        group.sort(key=lambda m: m.id)
        text_groups[group[0].id] = group
    semaphore = asyncio.Semaphore(args.jobs)
    results = await asyncio.gather(
        *(download_message(client, msg, channel_folder, with_header, semaphore,
                           text_groups.get(msg.id))
          for msg in messages)
    )
    failed = [msg.id for msg, ok in zip(messages, results) if ok is None]
    return len(messages) - len(failed), failed


async def download_post(client, entity, msg_id, channel_folder, with_header):
    """Returns True when every message of the post was saved."""
    message = await client.get_messages(entity, ids=msg_id)
    if message is None:
        sys.exit(f"Message {msg_id} not found in this channel.")
    messages = [message]
    if message.grouped_id:  # an album: fetch its siblings (albums hold at most 10 items)
        around = await client.get_messages(entity, ids=list(range(msg_id - 9, msg_id + 10)))
        messages = [m for m in around if m and m.grouped_id == message.grouped_id]
        print(f"Album with {len(messages)} items")
    succeeded, failed = await download_all(client, messages, channel_folder, with_header)
    print(f"Done: {succeeded}/{len(messages)} messages saved to {channel_folder}")
    if failed:
        print(f"NOT saved: {failed}. Run the same link again to retry.")
    return not failed


async def download_history(client, entity, channel_folder, with_header, min_id, resume):
    """Download the history and whatever earlier runs left undone (when resuming).
    Returns True when the folder is complete; otherwise the summary says what is
    missing and the state files let the next --new run pick it up."""
    failed, gaps = read_state(channel_folder) if resume else (set(), [])
    if resume:
        latest_id = get_latest_message_id(channel_folder)
        if latest_id is not None:
            min_id = latest_id
            print(f"Resuming from message ID {latest_id}")
        else:
            print("Warning: no existing downloads found, downloading everything")
    min_id = min_id or 0

    batch_size = args.jobs * 5
    total = succeeded = 0
    now_failed = []
    stopped = None      # why a listing ended early, if it did
    in_progress = None  # [below, above] of the range being listed right now

    async def flush(batch):
        nonlocal total, succeeded
        ok, bad = await download_all(client, batch, channel_folder, with_header)
        total += len(batch)
        succeeded += ok
        now_failed.extend(bad)
        tail = f", {len(now_failed)} failed" if now_failed else ""
        print(f"Progress: {total} messages processed{tail}")

    async def list_range(below, above):
        """Newest first, from just below `below` (0 = the top) down to just above `above`.
        in_progress[0] tracks the lowest id fully handled, so an interrupted listing
        can resume exactly there."""
        nonlocal in_progress
        in_progress = [below, above]
        batch = []
        async for message in client.iter_messages(entity, offset_id=below, min_id=above):
            if len(batch) >= batch_size and not same_album(batch[-1], message):
                await flush(batch)
                in_progress[0], batch = batch[-1].id, []
                if not client.is_connected():
                    raise ConnectionError("connection to Telegram is gone")
            batch.append(message)
        if batch:
            await flush(batch)
        in_progress = None

    todo = gaps + [(0, min_id)]  # old gaps first, then the new messages
    try:
        if failed:
            print(f"Retrying {len(failed)} message(s) that failed last time")
            found = [m for m in await client.get_messages(entity, ids=sorted(failed)) if m]
            if len(found) < len(failed):
                print(f"{len(failed) - len(found)} of them no longer exist")
            failed = {m.id for m in found}
            for chunk in album_safe_chunks(found, batch_size):
                await flush(chunk)
                failed -= {m.id for m in chunk}  # whatever failed again is in now_failed
        if gaps:
            print(f"Filling {len(gaps)} gap(s) left by runs that stopped early")
        while todo:
            await list_range(*todo.pop(0))
    except LISTING_ERRORS as e:
        stopped = f"{type(e).__name__}: {e}"
        print(f"Listing stopped early: {stopped}")
    finally:
        # Runs on Ctrl-C too, so an aborted run can always be continued with --new.
        gaps = ([tuple(in_progress)] if in_progress else []) + todo
        failed |= set(now_failed)
        write_state(channel_folder, failed, gaps)
        print(f"\nDone: {succeeded}/{total} messages downloaded to {channel_folder}")
        if failed:
            print(f"NOT saved: {len(failed)} message(s), ids in {channel_folder}/{FAILED_FILE}")
        if gaps:
            print(f"NOT listed: {len(gaps)} id range(s), see {channel_folder}/{GAPS_FILE}")
        if failed or gaps:
            print("Run again with --new to retry what is missing.")
    return not (failed or gaps)


async def full_info(client, entity):
    """Description and extra facts that need a second request. Best effort."""
    try:
        if isinstance(entity, Channel):
            full = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
            extra = {"participants": full.participants_count,
                     "linked chat": full.linked_chat_id and f"channel:{full.linked_chat_id}"}
        elif isinstance(entity, Chat):
            full = (await client(functions.messages.GetFullChatRequest(entity.id))).full_chat
            extra = {"participants": len(getattr(full.participants, "participants", []))}
        else:
            full = (await client(functions.users.GetFullUserRequest(entity))).full_user
            extra = {"personal channel": full.personal_channel_id and f"channel:{full.personal_channel_id}"}
    except RPCError as e:
        return None, {"info error": f"{type(e).__name__}: {e}"}
    return full.about, extra


async def write_info(client, entity, peer, folder):
    """Write 00-info.txt and the profile picture into the folder. Overwrites each run."""
    about, extra = await full_info(client, entity)
    rows = {"kind": peer.kind, "name": utils.get_display_name(entity), "target": peer.target,
            "username": getattr(entity, "username", None), "id": utils.get_peer_id(entity)}
    if isinstance(entity, User):
        rows.update({"first name": entity.first_name, "last name": entity.last_name,
                     "phone": entity.phone and f"+{entity.phone}", "bot": entity.bot or None,
                     "premium": entity.premium or None})
    if isinstance(entity, Channel):
        rows.update({"joined": entity.date and f"{entity.date:%Y-%m-%d}",
                     "megagroup": entity.megagroup or None, "verified": entity.verified or None,
                     "scam": (entity.scam or entity.fake) or None,
                     "restricted": entity.restricted and
                     "; ".join(f"{r.platform}: {r.text}" for r in entity.restriction_reason or [])})
    rows.update(extra)
    rows["saved"] = f"{datetime.now().astimezone():%Y-%m-%d %H:%M %Z}"
    lines = [f"{k}: {v}" for k, v in rows.items() if v not in (None, "", False)]
    if about:
        lines += ["", "description:", about]
    with open(os.path.join(folder, f"{INFO_PREFIX}info.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    pic = os.path.join(folder, f"{INFO_PREFIX}{peer.internal_name}.jpg")
    try:
        saved = await client.download_profile_photo(entity, file=pic, download_big=True)
    except RPCError as e:
        saved = None
        print(f"Profile picture: {type(e).__name__}: {e}")
    print(f"Info saved to {folder}/{INFO_PREFIX}info.txt"
          + (f", picture to {os.path.basename(saved)}" if saved else ", no profile picture"))


async def update_all(client):
    if not os.path.isdir(BASE_FOLDER):
        sys.exit(f"Nothing downloaded yet: no {BASE_FOLDER}/ folder.")
    updated, incomplete, skipped, failed = 0, [], [], []
    for name in sorted(os.listdir(BASE_FOLDER)):
        folder = os.path.join(BASE_FOLDER, name)
        if not os.path.isdir(folder):
            continue
        peer_id = peer_from_folder(name)
        if peer_id is None:
            skipped.append(name)
            continue
        print(f"\n== {name}")
        if not client.is_connected():
            print("connection to Telegram is gone, not trying")
            failed.append(name)
            continue
        try:
            entity = await client.get_entity(peer_id)
            peer = describe(entity)
            print(f"Target: {utils.get_display_name(entity) or peer_id} ({peer.target})")
            await write_info(client, entity, peer, folder)
            if await download_history(client, entity, folder, peer.with_header, None, True):
                updated += 1
            else:
                incomplete.append(name)
        except (ValueError, ConnectionError, RPCError) as e:
            print(f"{name}: cannot update: {type(e).__name__}: {e}")
            failed.append(name)
    print(f"\nUpdated {updated} folder(s).")
    if skipped:
        print(f"Skipped (not a history folder): {', '.join(skipped)}")
    if incomplete:
        print(f"Incomplete (run --update again to retry): {', '.join(incomplete)}")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    return not (incomplete or failed)


async def list_dialogs(client, limit):
    rows = []
    async for dialog in client.iter_dialogs(limit=limit, archived=False):
        try:
            peer = describe(dialog.entity)
        except ValueError:
            continue  # e.g. a chat we were banned from
        rows.append((peer.kind, dialog.name or "(no name)", peer.target))
    width = max((len(name) for _, name, _ in rows), default=0)
    for kind, name, target in rows:
        print(f"{kind:<8} {name:<{width}}  {target}")


async def main():
    aliases = load_aliases()
    peer_id = msg_id = None
    if args.dialogs is None and not args.update:
        peer_id, msg_id = parse_target(args.target, aliases)
        if msg_id is not None and (args.new or args.m is not None):
            parser.error("--new and -m do not apply to a single post link")
        if msg_id is None and args.tag != parser.get_default("tag"):
            parser.error("--tag only applies to a single post link")

    client = TelegramClient(
        SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
        timeout=120, flood_sleep_threshold=120,
    )
    async with client:
        if not await client.is_user_authorized():
            sys.exit("Not logged in. Run: ./tg login qr")
        if args.dialogs is not None:
            await list_dialogs(client, args.dialogs)
            return True
        if args.update:
            return await update_all(client)
        try:
            entity = await client.get_entity(peer_id)
            peer = describe(entity)
        except (ValueError, RPCError) as e:
            sys.exit(f"Cannot access {peer_id!r}: {e}")
        print(f"Target: {utils.get_display_name(entity) or peer_id} ({peer.kind} {utils.get_peer_id(entity)})")
        folder_name = f"{peer.folder}-{args.tag}" if msg_id is not None else peer.folder
        channel_folder = os.path.join(BASE_FOLDER, folder_name)

        if (msg_id is None and not args.new and not args.over
                and get_latest_message_id(channel_folder) is not None):
            sys.exit(f"Error: folder '{channel_folder}' already has messages. "
                     "Use --over to overwrite files or --new to fetch only new messages.")
        os.makedirs(channel_folder, exist_ok=True)
        await write_info(client, entity, peer, channel_folder)

        if msg_id is not None:
            return await download_post(client, entity, msg_id, channel_folder, peer.with_header)
        return await download_history(client, entity, channel_folder, peer.with_header,
                                      args.m, args.new)


if __name__ == "__main__":
    try:
        complete = asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
    except FloodWaitError as e:
        sys.exit(f"Telegram rate limit: wait {e.seconds}s and try again.")
    except RPCError as e:
        sys.exit(f"Telegram error: {type(e).__name__}: {e}")
    except ConnectionError as e:
        sys.exit(f"Cannot reach Telegram: {e}")
    sys.exit(0 if complete else 1)
