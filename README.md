# tg_saver_script

Download Telegram history to disk: channels, groups, private chats, your Saved
Messages, or single posts (including purchased paid media).

## Setup (once)

1. Create `.env` with your API credentials from https://my.telegram.org:

   ```
   TG_API_ID=12345
   TG_API_HASH=0123456789abcdef0123456789abcdef
   ```

2. Log in:

   ```
   ./tg login qr        # scan with Telegram -> Settings -> Devices -> Link Desktop Device
   ./tg login status    # check
   ```

`./tg` is the only command you need. It uses the project's own virtualenv and
creates it on first run, so there is nothing to activate. It works from any
directory, e.g. `~/dev/tg_saver_script/tg --dialogs`. If you want it on your
PATH, add `alias tg=~/dev/tg_saver_script/tg` to `~/.zshrc`.

## Everyday use

```
./tg --dialogs                          # 10 most recent chats with a ready-to-use target each
./tg --dialogs 30                       # more of them
./tg @truexakyiv                        # whole history of a channel
./tg me                                 # your Saved Messages
./tg user:123456789                     # a private chat (ids come from --dialogs)
./tg news                               # an alias from aliases.toml
./tg @truexakyiv --new                  # only messages newer than what is on disk
./tg --update                           # the same for every history folder at once
./tg https://t.me/truexakyiv/96262      # one post (the whole album if grouped)
./tg https://t.me/c/2713092035/95 --tag regina   # one post into a folder of your choosing
./tg --help                             # all options
```

Options: `-m ID` stop at a message id, `--over` overwrite an existing folder,
`-j N` concurrent downloads (default 3), `--skip-vid`, `--skip-pic`.

### Targets

| Form              | Meaning                                  |
|-------------------|------------------------------------------|
| `me`              | Saved Messages                           |
| `@username`       | public channel, group, bot or person     |
| `user:123`        | person by id                             |
| `chat:123`        | basic group by id                        |
| `channel:123`     | channel or supergroup by id              |
| `-1001234567890`  | channel id as Telegram clients show it   |
| `alias`           | a name from `aliases.toml`               |
| `t.me/...` link   | a channel, or a single post              |

Aliases: copy `aliases.example.toml` to `aliases.toml` and edit. The file is
gitignored because it maps names to your private channels.

## What lands on disk

```
downloads/
  channel_-1001697250541/        full history of a channel
  channel_-1001697250541-posts/  single posts from it (default tag "posts")
  channel_-1002713092035-regina/ single posts with --tag regina
  saved_messages/                me
  user_123456789/                a private chat
  chat_-123456789/               a basic group
```

Each folder holds:

- `00-info.txt` with name, username, id, description, member count and so on,
  plus `00-<username>.jpg`, the profile picture. Both are refreshed every run
  and sort before all messages.
- `<id>_message.txt` per message. Channel posts are bare text. In chats the
  file starts with `date  sender:` and, for forwards, a `forwarded from` line.
  An album (several photos sent at once) gets one such file, named after its
  first item; the other items have only their media file.
- `<id>_media.jpg` / `.mp4` for photos and videos, `<id>_<filename>` for other
  documents, `<id>_media_1.jpg` ... for paid posts with several items.
- `00-failed.txt` and `00-gaps.txt` only while something is missing, see below.

`--update` and `--new` resume from the highest message id in the folder.
Folders with a `-tag` suffix are never touched by `--update`, so they are
safe for hand-picked posts. Paid posts download only after you have bought
them in the app. Before that the script saves the text and tells you the price.

## When something goes wrong

Lines starting with `telethon:` come from the Telegram library. `Server closed
the connection: [Errno 54] Connection reset by peer` means the connection
dropped; the library reconnects by itself and resends whatever was in flight,
so a few of these are harmless. Only the summary at the end tells you whether
anything was actually lost.

Every run ends with `Done: <saved>/<seen> messages downloaded to <folder>`. If
the two numbers differ, or the listing itself stopped early, the run says so,
exits with status 1 and leaves a record in the folder:

- `00-failed.txt`: ids of messages whose text or media could not be saved after
  the retries (a note is printed for each one as it happens).
- `00-gaps.txt`: id ranges that were never listed because the connection died
  or Telegram kept refusing the request mid-way. Also written when you press
  Ctrl-C, so an aborted download can be continued.

The next `./tg <target> --new` (or `./tg --update`) retries those first and then
fetches the new messages. Once everything is saved the two files disappear.
Messages that were deleted in the meantime are dropped from the list. For a
single post link just run the same link again.
