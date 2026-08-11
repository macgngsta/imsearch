# imsearch

Search, reconstruct, and browse your local **iMessage** history — from the command line or
a local web UI. Reads `~/Library/Messages/chat.db` **strictly read-only** and never writes
to it.

- 🔎 **Search** message text with contact / sender / date filters
- 🧵 **Reconstruct conversations** as chronological transcripts, or show context around hits
- 👤 **Contact names** resolved from macOS Contacts (phone *and* iCloud/email handles)
- 🗑️ **Recently Deleted** messages (opt-in, flagged)
- 📎 **Attachments** listed with paths, openable, and shown **inline as images** in the web UI
- ⏱️ **Relative time windows** (`--last 3h`, `2d`, `1w`)
- 🌐 **Local web app** (FastAPI + DaisyUI 5), fully offline

---

## Why this exists (the non-obvious parts)

Two things make naive iMessage scripts return wrong or empty results — `imsearch` handles
both:

1. **Message text isn't in the `text` column.** On modern macOS ~99% of messages store
   their body in the `attributedBody` BLOB (a NeXTSTEP *typedstream* / `NSAttributedString`).
   `imsearch` decodes it with [`pytypedstream`](https://pypi.org/project/pytypedstream/).
   A plain `SELECT text FROM message` returns almost nothing.
2. **The newest messages live in the write-ahead log.** Messages.app buffers recent
   messages in `chat.db-wal` before checkpointing them into the main file. Opening the db
   with `immutable=1` (a common trick to dodge the lock) **ignores the WAL and hides the
   last several hours of messages**. `imsearch` instead snapshots the db + `-wal`/`-shm` to
   a private temp copy when a pending WAL exists, so recent messages are visible — without
   ever touching the original.

---

## Requirements

- **macOS** with Messages configured (`~/Library/Messages/chat.db`).
- [`mise`](https://mise.jdx.dev/) — manages Python 3.14 + `uv` for you.
- **Full Disk Access** for your terminal. macOS protects `~/Library/Messages` and
  `~/Library/Application Support/AddressBook`; grant access under **System Settings →
  Privacy & Security → Full Disk Access** (add Terminal / iTerm / your IDE). Without it
  you'll get a permissions error.

## Setup

```sh
mise trust      # first time only, approves this repo's mise.toml
mise setup      # installs Python 3.14, uv, and dependencies (uv sync)
```

---

## Web UI

A local DaisyUI search form that renders messages and shows **attachment images inline**.

```sh
mise run serve:start     # start in the background → http://127.0.0.1:8000
mise run serve:stop      # stop it
```

`serve:start` runs the server detached (PID in `.serve.pid`, logs in `.serve.log`).
Open <http://127.0.0.1:8000>, then:

- Search by **text** and/or **contact name**
- Scope to a **time window** (past 3h / day / week / 30 days)
- Toggle **Images only** and **Include deleted**
- Click any thumbnail to open the full image

Foreground / custom port:

```sh
uv run imsearch serve --port 9000
```

**Design & safety:** read-only; binds to `127.0.0.1` only (not reachable from the network);
attachments are served by integer id and confined to `~/Library/Messages/Attachments` (no
arbitrary file access). Tailwind + DaisyUI are vendored under `src/imsearch/static/vendor`,
so nothing is fetched from a CDN at runtime.

---

## CLI

Two query subcommands: **`search`** (find messages) and **`thread`** (reconstruct a
conversation). Pass args through mise with `--`, e.g. `mise run search -- "dinner"`.

### `search` — find messages

```sh
mise run search -- "dinner"                          # substring search
mise run search -- "dinner" --name "Jane Doe"        # filter by contact name
mise run search -- "thanks" --from-me                # only messages you sent
mise run search -- "dinner" --context 3              # 3 messages around each hit
mise run search -- "flight" --last 2d                # past 2 days
mise run search -- "invoice" --open-attachments      # open matched attachments
```

| Flag | Meaning |
|------|---------|
| `query` (positional) | Case-insensitive substring to search for |
| `--contact` | Filter by the other party's phone/email (substring) |
| `--name` | Filter by contact name, resolved via AddressBook |
| `--from-me` / `--from-them` | Only messages you sent / received |
| `--context N` | Show N messages before/after each hit, in order |
| `--since` / `--until` | ISO date bounds (`2025-01-31` or `2025-01-31T14:00`) |
| `--last DURATION` | Relative window ending now, e.g. `3h`, `2d`, `1w` |
| `--deleted` | Also include "Recently Deleted" messages (marked with `*`) |
| `--open-attachments` | Open matched messages' attachment files (macOS `open`) |
| `--limit` | Max results (default 50) |
| `--no-names` | Show raw handles instead of resolved contact names |
| `--db` | Path to an alternate `chat.db` |

### `thread` — reconstruct a conversation

```sh
mise run thread -- --name "Jane Doe"                              # whole conversation
mise run thread -- --name "Jane Doe" --last 3h                    # past 3 hours
mise run thread -- --contact 5555550142 --since 2025-01-01        # by phone/email
mise run thread -- --chat "Lunch Crew" --deleted                  # group chat + deleted
```

| Flag | Meaning |
|------|---------|
| `--name` | Contact name, resolved via AddressBook |
| `--contact` | Phone/email of the conversation (substring) |
| `--chat` | Group name or chat identifier (substring) |
| `--since` / `--until` | ISO date bounds |
| `--last DURATION` | Relative window ending now, e.g. `3h`, `2d`, `1w` |
| `--deleted` | Also include "Recently Deleted" messages (marked with `*`) |
| `--open-attachments` | Open the transcript's attachment files (macOS `open`) |
| `--limit` | Max messages (default 1000) |
| `--no-names` | Show raw handles instead of resolved contact names |
| `--db` | Path to an alternate `chat.db` |

You must pass at least one of `--name`, `--contact`, or `--chat`. Messages you sent show
as `me`; incoming messages show the resolved contact name (or the raw handle if unknown),
so group threads stay readable.

### `serve` — the web UI

```sh
uv run imsearch serve [--host 127.0.0.1] [--port 8000]
```

Both query subcommands also run directly inside the venv, e.g.
`uv run imsearch thread --name "…"`.

---

## Features in depth

### Contact names

Handles are resolved to names from your macOS **Contacts** (`AddressBook-v22.abcddb`
databases under `~/Library/Application Support/AddressBook`, read-only). Phone numbers are
matched on their **last 10 digits** (formatting and country code don't matter); emails
match case-insensitively. Because a contact card holds a person's phone(s) **and** their
Apple ID / iCloud email, `--name` matches messages across **all** of their handles — an
iMessage that arrived on their iCloud email counts the same as one on their phone number.
Pass `--no-names` to show raw handles; if Contacts is unavailable, `imsearch` silently
falls back to raw handles.

### Recently Deleted messages

macOS keeps deleted messages in a "Recently Deleted" store (`chat_recoverable_message_join`)
for ~30 days. Pass `--deleted` to `search`/`thread` to include them; they render with a red
trailing **`*`** on the sender name (and a "deleted" badge in the web UI).

### Attachments

Images, files, and links are joined into `search` and `thread`. In the CLI each attachment
prints **name · type · size** plus its **full on-disk path** (⌘-click in most terminals to
open); a red `(file not on disk)` note means the file was removed or hasn't downloaded from
iCloud. Add `--open-attachments` to open them via macOS `open`. In the web UI, image
attachments render inline as thumbnails. (HEIC images can't render in browsers and fall
back to a download link.)

### Relative time windows

`--last DURATION` gives a window ending now instead of computing a `--since` date by hand.
Units: `s` seconds, `m` minutes, `h` hours, `d` days, `w` weeks. Mutually exclusive with
`--since`.

---

## Limitations

- **Substring search only** — matching is plain case-insensitive substring, not fuzzy or
  regex, and there's no full-text index. Common terms are matched against a bounded
  candidate set, so an extremely frequent word may not surface every historical hit at once
  (narrow with `--contact`/`--name`/date filters).
- **macOS only** — it reads Apple's `chat.db` and `AddressBook` schemas directly.
- **Non-text payloads** render as a placeholder; reactions/tapbacks and edited-message
  history aren't reconstructed specially. HEIC attachments fall back to a link in the web UI.
- **Schema drift** — Apple can change the `chat.db` schema between macOS releases; a future
  version may need adjustments.

## How it works

- **Text decoding** — prefers the plain `text` column, falls back to decoding the
  `attributedBody` typedstream to recover the message body.
- **Timestamps** — Messages stores `date` as nanoseconds since the Apple/Cocoa epoch
  (2001-01-01 UTC); `imsearch` converts to your local time zone.
- **Read-only + WAL** — a non-empty `chat.db-wal` triggers a temp-file snapshot of the db
  and its sidecars (read with `PRAGMA query_only`); otherwise the db is opened
  `mode=ro&immutable=1`. The source database is only ever read.
- **Query shape** — structural filters (contact, sender, date, chat, deleted) run in SQL
  over a unioned view of the live + recoverable chat joins; text matching happens in Python
  after decoding a bounded candidate set. Context and transcripts are ordered by
  `(date, ROWID)` and anchored on the exact raw timestamp to avoid float-rounding drift.
- **Attachment serving (web)** — files are looked up by attachment ROWID, resolved, and
  verified to live under `~/Library/Messages/Attachments` before being returned.

## Project structure

```
imsearch/
├── mise.toml                 # Python 3.14 + uv; tasks: setup, search, thread, serve:start/stop
├── pyproject.toml            # deps: pytypedstream, rich, fastapi, uvicorn
├── src/imsearch/
│   ├── db.py                 # read-only access, WAL snapshot, decode, search/thread/context, attachments
│   ├── contacts.py           # AddressBook name resolution (phone + email/iCloud)
│   ├── cli.py                # argparse CLI: search / thread / serve
│   ├── web.py                # FastAPI app: page, /api/search, /attachment/{id}
│   └── static/               # index.html + vendored Tailwind/DaisyUI
└── tests/                    # db, queries, contacts, web (incl. path-traversal safety)
```

## Testing

```sh
uv run pytest -q
```

Covers date conversion, `attributedBody` fallback, search/transcript/context queries,
deleted-message filtering, attachment joins, contact-name resolution (phone/email), and the
web layer (search API, attachment serving, and path-traversal protection).

## Privacy & safety

- **Read-only**: the source `chat.db` and AddressBook databases are never modified.
- **Local only**: the web server binds to `127.0.0.1`; nothing leaves your machine and no
  external CDNs are contacted (front-end assets are vendored).
- Your messages and contacts stay on disk — `imsearch` just reads and displays them.

## License

[MIT](LICENSE) © Greg Tam.

Vendored front-end assets under `src/imsearch/static/vendor/` are third-party and MIT
licensed: [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) and
[daisyUI](https://github.com/saadeghi/daisyui).
