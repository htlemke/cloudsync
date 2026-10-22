# cloudsync

Selective two-way sync of cloud storage with [rclone](https://rclone.org)
`bisync`, as a replacement for clients like Insync, with a GNOME Shell
extension that also finds the files you *didn't* sync.

- Any rclone remote (Google Drive, Nextcloud, OneDrive, SMB, …) is synced into
  `~/cloud/<remote>/`, but only the folders you pick.
- Pick folders in a GUI (GTK 4/libadwaita on GNOME, Tk elsewhere and on Windows).
  Ticking a folder syncs everything inside; untick a subfolder to leave it out,
  tick a folder inside that again, and so on, at any depth.
- Any other local folder can be paired with any remote path
  (`cloudsync pair add`), e.g. a code folder with a Nextcloud folder.
- Google Docs/Sheets/Slides get a `NAME.link.html` next to the real files that
  opens them in the browser.
- The folder selection ("profile") is stored on a Google Drive remote, one per
  machine, so it is shared by both OSes of a dual-boot computer.
- `cloudsync watch` syncs single changes within seconds: local saves are
  uploaded right away, changes in Google Drive and Nextcloud are picked up from
  their change feeds. The full sync stays as a safety net.
- Conflicts: the newer version wins. The other one, deleted files and files
  replaced by a change from the cloud go to a **local** backup folder with the
  same folder structure (kept 90 days), never as extra copies into the cloud.
- A history of all changes (what, when, who, from where), optional desktop
  notifications for changes from the cloud.
- The GNOME Shell extension lists matching cloud-only files in the overview
  search (click: open in the browser, Ctrl+click: open the folder in the
  Cloud Sync window to add it), and shows the sync state in the top bar.

## Install (Linux)

Needs Python 3, rclone, and for the GUI GTK 4 + libadwaita (`gir1.2-adw-1`) or Tk.

```bash
git clone https://github.com/htlemke/cloudsync && cd cloudsync
./install.sh
```

Set up the rclone remotes (`rclone config`). The settings remote must be called
`gdrive` (a Google Drive remote); the others can have any name. For Google
Drive, [use your own client ID](https://rclone.org/drive/#making-your-own-client-id).
If your Drive contains shortcuts to big shared folders, consider
`rclone config update gdrive skip_shortcuts=true`.

```bash
cloudsync init --machine laptop1     # once per computer
cloudsync gui                        # pick folders ("Cloud Sync" in the app menu)
cloudsync sync --dry-run             # look at the logs first
cloudsync sync
systemctl --user enable --now cloudsync.timer cloudsync-index.timer cloudsync-watch.service
```

- `cloudsync-watch.service`: single changes within seconds (see below)
- `cloudsync.timer`: full sync every 30 minutes (desktop notification on failure)
- `cloudsync-index.timer`: refreshes the search index every 6 hours

For the search extension, log out and in once (Wayland), then
`gnome-extensions enable cloudsync-search@htlemke.github.io`.

## Commands

- `cloudsync gui [--tk] [--show REMOTE:PATH]`: pick folders per remote, optionally opened at a folder
- `cloudsync sync [REMOTE|PAIR …] [--dry-run] [--resync] [--notify]`
- `cloudsync status`: what is selected, free disk space
- `cloudsync pair add NAME LOCAL REMOTE [--filter '+ /sub/**' …]`, `cloudsync pair remove NAME`
- `cloudsync index [REMOTE …]`: file-name index for the search extension
- `cloudsync watch`: the watcher (normally run by `cloudsync-watch.service`)
- `cloudsync mount REMOTE [--stop]`: the whole remote as a folder (see below)
- `cloudsync config [KEY [VALUE]]`: settings, see below
- `cloudsync init [--machine NAME]`

## Where things are

| What | Where |
|---|---|
| synced files | `~/cloud/<remote>/`; change with `~/.config/cloudsync/local.json`: `{"root": "/path"}` |
| folder selection and pairs | `gdrive:.cloudsync/profiles/<machine>.json` |
| machine name, bisync state, filters, logs | `<root>/.cloudsync/` |
| search index | `~/.cache/cloudsync/index.tsv` |
| credentials | rclone's own `rclone.conf`, never touched |

| backups | `<root>/.backup/<remote>/` (local only, same folder structure) |
| history | `~/.local/share/cloudsync/history.jsonl` |

A change of selection triggers a `--resync` (newer file wins) on the next sync.
Deselecting a folder offers to delete its local copy; the cloud copy stays.
Never synced: folders that contain a `pyvenv.cfg` (Python virtualenvs of any
name), the side files of open SQLite databases (`*.db-wal`, `*.db-shm`), and
`.DS_Store`/`Thumbs.db`/Office lock files. In pairs, also Python caches,
`.pixi`, `.venv` and `node_modules`;
`.git` folders are synced, so don't work on the same repo on two computers
between syncs.

## Watching

`cloudsync watch` (Linux) adds a fast path to the full sync:

- **Local changes**: inotify reports saved files; a few seconds later (longer
  for big files that keep growing) the file is uploaded. Renames are moves in
  the cloud.
- **Cloud changes**: Google Drive's changes feed and the Nextcloud/ownCloud
  activity feed are polled every 30 s (`poll_seconds`); changed files are
  downloaded, with the name of who changed them. Other remotes only sync with
  the timer.
- Deleted or renamed folders, local deletions, names that need the Windows
  encoding and any error mark the folder for a full sync a minute later, so
  bisync's safety checks (e.g. `--max-delete`) stay in charge of those.
- Watcher and full sync never run at the same time (a lock file).

**Google Drive needs your own client ID** for the changes feed: rclone's
shared one is rate-limited ("quota exceeded"). See the rclone docs, then
`rclone config update gdrive client_id=… client_secret=…` and
`rclone config reconnect gdrive:`.

## Cloud folders (mount on demand)

`cloudsync mount REMOTE` (via `cloudsync-mount@REMOTE.service`) mounts the
whole remote with `rclone mount` at `~/Cloud/<remote>` (`mount_dir`): files are
fetched when opened (cache up to 5 GB, 72 h), Google Docs show up as
`NAME.link.html` that open in the browser. Started on first use, it stays
mounted until you log out. Used by:

- the search: a cloud-only result opens from the mount in its normal program
  (`open_cloud_files`: `mount`, or `browser` for the web page); **Shift+click**
  opens the containing folder; Ctrl+click still adds it to the sync
- the top-bar menu: *Browse cloud folders*
- the window: right-click a folder → *Open in cloud folder*

Edits in the mount go straight to the cloud (and from there to synced copies).
Needs a connection; the first open of a big file takes as long as its download.

## Conflicts, backups, history

- A file changed on both sides: the newer version wins everywhere. The other
  one goes to `<root>/.backup/<remote>/<same folders>/NAME (2026-10-18 14.03
  conflict, local version).ext` (or `cloud version`): only on this computer,
  never as a copy in the cloud.
- Files that a change from the cloud replaces or deletes are kept there too
  (`NAME (date time).ext`). Local deletions go to the cloud's trash.
- Backups older than `backup_days` (90) are deleted.
- Every change is recorded in the history (Cloud Sync window → History, and
  *Recent changes* in the top-bar menu).

## Settings

`cloudsync config` (or the Settings tab in the window). Stored in the profile,
so per machine and shared by its OSes.

| Setting | Default | |
|---|---|---|
| `notify` | `summary` | changes from the cloud: `off`, `summary` (one notification) or `each` |
| `notify_own` | `false` | also notify my own changes (e.g. from another computer) |
| `backup_days` | `90` | how long backups and history are kept |
| `poll_seconds` | `30` | how often the watcher asks the clouds |
| `index_remotes` | `[]` | remotes to include in the search without syncing them, e.g. `'["nextcloud"]'` |
| `labels` | `{}` | names for remotes in search results, history and notifications, e.g. `'{"gdrive": "Google Drive"}'` |
| `mount_dir` | `~/Cloud` | where remotes are mounted on demand |
| `open_cloud_files` | `mount` | search results not synced here: open from the mount, or `browser` |

## GNOME Shell extension

`search/cloudsync-search@htlemke.github.io`, installed by `install.sh`.

**Search.** `cloudsync index` lists the file and folder names of every enabled
remote (no content is downloaded). The extension searches that list as you
type and leaves out results that exist locally, since the normal file search
finds those.

- Click: open in the browser. Google Drive works out of the box, Nextcloud/
  ownCloud WebDAV remotes get links to their web UI from the rclone config.
- Ctrl+click: open the folder (or the file's folder) in the Cloud Sync window
  to add it to the sync.
- Click on the section header: Google Drive's own web search.

**Top bar.** An icon shows the sync state (from `~/.cache/cloudsync/status.json`,
written by `cloudsync sync`): idle, syncing, or a warning when the last sync
failed or the last good one is more than 3 hours old. The menu shows whether
the watcher runs (and the state of each change feed), the recent changes, and
has *Sync now*, *Open Cloud Sync*, *Refresh search index* and *Open logs*.

Search results show where they are from (the remote's label). Ranking: exact
name, then the search word at the start of the name or of a word in it
(`26b_TTO_Circular` for "tto"), then anywhere; folders before files; results
inside hidden folders (`.git`, `.ipynb_checkpoints`) last.

## Tests

```bash
cd tests && python3 -m unittest -v
```

They run the real sync and watcher code against a local folder as "cloud"
(an rclone alias in a temporary config); no real remote is touched.

## Dual boot with Windows

Use a folder on a shared NTFS partition as root on both OSes, e.g.
`{"root": "/mnt/data/cloud"}` on Linux and `{"root": "D:\\cloud"}` in
`%APPDATA%\cloudsync\local.json`.

1. Turn **off Fast Startup** in Windows (Power Options → *Choose what the power
   buttons do*). Otherwise Linux writes to the NTFS partition can be lost.
2. Install rclone (`winget install Rclone.Rclone`) and Python 3.
3. Create the same remotes, with the same names, with `rclone config`.
4. Run `python cloudsync.py gui` / `sync`. Pairs are Linux only.

File names Windows can't store (`: ? * |` …) get rclone's substitute characters
on both OSes, so both see the same names.

## Development

See [cloudsync-dev.md](cloudsync-dev.md): architecture, verified rclone
behaviour, lessons learned, open issues.

## License

MIT
