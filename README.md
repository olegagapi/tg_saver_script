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
- `<id>_media.jpg` / `.mp4` for photos and videos, `<id>_<filename>` for other
  documents, `<id>_media_1.jpg` ... for paid posts with several items.

`--update` and `--new` resume from the highest message id in the folder.
Folders with a `-tag` suffix are never touched by `--update`, so they are
safe for hand-picked posts. Paid posts download only after you have bought
them in the app. Before that the script saves the text and tells you the price.
