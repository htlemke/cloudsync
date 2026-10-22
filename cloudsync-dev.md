# cloudsync – developer notes

For whoever (human or Claude Code) continues developing cloudsync. Read this,
then [README.md](README.md), then run the tests. It collects the design, why it
is that way, what rclone really does (verified by experiment), the incidents and
lessons from the first days in production, and the open issues.

Personal deployment details (accounts, machines, paths) are kept elsewhere, not
in this public repo.

## 1. Before you change anything

- **The running services execute this checkout.** `~/.local/bin/cloudsync` is a
  symlink to `cloudsync.py`, and `cloudsync-watch.service`, `cloudsync.timer`,
  `cloudsync-index.timer` run it. Stop them while editing, so no sync runs
  half-edited code against real data:
  ```bash
  systemctl --user stop cloudsync-watch.service cloudsync.timer
  # … edit, test …
  ./install.sh && systemctl --user daemon-reload
  systemctl --user start cloudsync-watch.service cloudsync.timer
  ```
- If the checkout lives inside a synced folder pair (e.g. `~/code`), every save
  is uploaded by the watcher and lands on the other computers. Prefer git for
  the repo itself and exclude it from the pair.
- Run the tests (about a minute, no real cloud touched):
  `cd tests && python3 -m unittest -v`.
- Logs: `<root>/.cloudsync/logs/<remote>.log` (rclone bisync, `-v`),
  `journalctl --user -u cloudsync-watch.service`, `-u cloudsync.service`.
  State for the indicator: `~/.cache/cloudsync/status.json`.
- Public repo: no personal names, emails, hosts, tokens in code, docs, tests or
  commit messages.

## 2. Architecture

| File | Role |
|---|---|
| `cloudsync.py` | CLI and engine: profile/selection, filters, `bisync` wrapper, conflicts/backups, history, status, settings, index, Tk GUI |
| `cloudsync_gtk.py` | GTK 4/libadwaita window: one tab per remote (tri-state folder tree), History, Settings; `--show REMOTE:PATH`, `--show-history` |
| `cloudsync_watch.py` | `cloudsync watch`: inotify + cloud change feeds, single-file fast path through `rclone rcd` |
| `search/…/extension.js` | GNOME Shell extension: overview search provider over the index, top-bar indicator |
| `systemd/` | `cloudsync.service`+`.timer` (full sync, 30 min), `cloudsync-index.*` (6 h), `cloudsync-watch.service`, `cloudsync-mount@.service` (Type=notify, `cloudsync mount --run` execs `rclone mount`) |
| `tests/` | `harness.py` (temp root, rclone alias remote as "cloud", in-memory profile), `test_sync.py`, `test_watch.py` |

**Data and state**

| What | Where | Notes |
|---|---|---|
| synced files | `<root>/<remote>/`, root from `~/.config/cloudsync/local.json` (default `~/cloud`) | on dual boot: a shared NTFS folder |
| selection, pairs, settings | `gdrive:.cloudsync/profiles/<machine>.json` ("profile") | per machine, shared by its OSes; cached in `<root>/.cloudsync/profile-cache.json` |
| machine name | `<root>/.cloudsync/machine.txt` | |
| bisync listings, filters, logs | `<root>/.cloudsync/{bisync,filters,logs}/` | listings and the `<key>.<os>.cloudsync-ok` marker are per OS |
| backups | `<root>/.backup/<key>/<same folders>/NAME (YYYY-MM-DD HH.MM[ note]).ext` | `.backup/.incoming/` is transient |
| history | `~/.local/share/cloudsync/history.jsonl` | one JSON object per line |
| index, status, watcher state | `~/.cache/cloudsync/` | `index.tsv`, `status.json`, `watch-<key>.json`, feed tokens |
| rclone rcd socket | `$XDG_RUNTIME_DIR/cloudsync-rclone-<pid>.sock` | per process: tests never collide with the real watcher |

`key` = remote name (`gdrive`) or `pair-<name>`.

**Selection model.** A remote's selection is a dict of rules `{folder path: bool}`,
`""` = whole remote; a folder follows the nearest rule on itself or a parent
(`effective`), `tri_state` gives on/off/partial for the GUI, `prune` drops
redundant rules. `render_filters` turns it into an rclone filter file, deepest
rules first (rclone uses the first match). A change of the filter text (or of
`EXCLUDE_IF_PRESENT`) changes the digest in the marker and forces one
`--resync`.

**Full sync** (`bisync()`): `rclone bisync <local> <remote>` with
`--compare size,modtime --resilient --recover --max-lock 5m
--conflict-resolve newer --conflict-loser num --conflict-suffix cs-local,cs-cloud
--backup-dir1 <root>/.backup/.incoming/<key> --exclude-if-present pyvenv.cfg
--color NEVER -v --log-file …`, plus `--local-encoding <Windows set>` for
remotes on Linux (same file names as Windows on a shared disk). After the run:
`file_backups` (incoming → dated names), `fix_conflicts` (only if the log
mentions `.cs-`), history from the log, auto-resync marker reset on
"Must run --resync". Google Drive remotes also get `sync_links` (one-way
`NAME.link.html` for Google Docs).

**Watcher.** The full sync stays the reference; the watcher is only a fast path:

- local: inotify (ctypes, recursive watches, skipping excluded folders and
  folders with `pyvenv.cfg`) → debounce (3 s + 4 s/MB, max 120 s) → job
- cloud: `DriveFeed` (Drive API `changes.list`, token read from rclone.conf,
  refreshed by running `rclone about`), `NextcloudFeed` (OCS activity API,
  app password via `rclone reveal`), polled every `poll_seconds`
- jobs run one at a time in a worker thread under the same `fcntl` lock as
  `cloudsync sync`; transfers via `rclone rcd` (`operations/stat|copyfile|movefile|deletefile`)
- known state per file = bisync listings (path1 = local, path2 = cloud) plus
  the watcher's own changes since (`watch-<key>.json`); reloaded when the
  marker is newer (a full sync ran elsewhere)
- decisions: local changed and cloud = known → upload; cloud changed and local
  = known → download (old local version to backup); both changed → newer wins,
  loser to backup; single local deletion with cloud = known → `deletefile`
  (cloud trash)
- everything else is **"dirty"** → a full bisync of that unit 60 s after the
  last such event: folder create/delete/rename, renames in the cloud, > 20
  local deletions in 10 s, names that need the Windows encoding, errors,
  inotify queue overflow
- its own downloads are suppressed (path → size/mtime); deletions are only
  believed if the file is still gone 2 s later (rclone and editors replace files)

**Index and extension.** `cloudsync index` writes `#source` lines (key, label,
local folder, URL template, search URL) and `key \t isdir \t id \t path` per
entry from `rclone lsjson -R --fast-list --drive-export-formats link.html`.
Folder pairs are excluded (local anyway); `index_remotes` adds remotes that are
searched but not synced. The extension hides results that exist locally
(`GLib.file_test`), opens the web link (Drive `open?id=`, Nextcloud
`apps/files/?dir=…&scrollto=…`), Ctrl+click runs `cloudsync gui --show`.

## 3. rclone behaviour, verified by experiment

| Fact | Consequence |
|---|---|
| bisync `--conflict-loser num` renames the loser to `NAME.cs-local1.ext` / `NAME.cs-cloud1.ext` (suffix before the extension) **on both sides**; the winner keeps the name | `fix_conflicts` moves local losers to the backup and deletes the cloud copies |
| `--conflict-loser delete` + `--backup-dir1` backs up a *local* loser, but a *cloud* loser is just deleted | not used: cloud losers must also end up in the local backup |
| `--backup-dir1` catches local files deleted or overwritten by changes from the cloud | used, into `.backup/.incoming/` |
| **`--suffix` without `--backup-dir2` renames overwritten files in place on path2, i.e. in the cloud** | never use `--suffix` with bisync (incident 4.4) |
| bisync aborts ("Safety abort: all files were changed") when every file changed | tests need a few untouched files |
| listing names are `<path1>..<path2>.path{1,2}.lst` with `/`→`_`, from the *resolved* backend (an alias shows its local target, a local path with encoding gets a `local_` prefix) | the watcher matches listings by the local path only |
| listing line: `- size hash id modtime "path"` (Go-quoted path) | parsed by `LISTING_RE` |
| `-v` log lines: `- Path1 File is new|File changed: …|File was deleted - path`, `The winner is: PathN` | history from the log; `--color NEVER` |
| `--resync` copies missing files both ways, newer wins, **never deletes** | files deleted on one side since the last sync come back once |
| a file that changes during upload → "corrupted on transfer: sha1 hashes differ" → "Bisync critical error … Must run --resync" | marker reset → next run resyncs |
| `--exclude-if-present pyvenv.cfg` works with bisync | virtualenvs of any name excluded |
| Drive: `skip_shortcuts=true` | otherwise shortcuts into shared folders download the target again (and shortcut loops log errors) |
| Drive names with `/` are `／` in rclone's standard form | used when mapping Drive API names to paths |
| `rclone rcd --rc-addr unix://…` works; fs strings can carry options: `:local,encoding="A,B,…":/path` | per-unit local encoding in one rcd |
| `operations/stat` returns `{"item": null}` for a missing file | |
| Nextcloud modtimes have 1 s precision, Drive ms, local ns | equality tolerance 1.5 s plus size |
| rclone's shared Google client ID: "Quota exceeded … Requests per minute" for direct Drive API calls (and it is retired during 2026) | own client ID required for `DriveFeed` |

## 4. Incidents and lessons

1. **`pkill -f <pattern>` killed the shell running it** (the pattern was in its
   own command line). Kill by PID or match exactly.
2. **Shortcut downloaded a shared folder a second time**: My Drive had a
   shortcut to a shared group folder that also has its own remote →
   `skip_shortcuts=true`; the shared folder gets a remote with `root_folder_id`.
3. **Taking over a selection from Insync**: its SQLite DB (`~/.config/Insync/data/gd-*.db`,
   tables `nodes`, `cl_items`, `fs_items`; copy first, it's locked) gives the
   exact synced tree. Partial folders + `AUTO_SYNC_PARTIAL` map best to "whole
   remote on, unsynced folders off". Before deleting Insync's copy compare it
   one-way with the new one: it held ~200 files that no longer existed on Drive
   (not in the trash either).
4. **`--suffix` created timestamped copies in the cloud** (33 files in one
   run). The test only checked the content of the right file. Fixed: backups via
   `.backup/.incoming`; every test's `tearDown` asserts the cloud has no
   `" (20"` or `.cs-` names (`assertCloudClean`), and the old behaviour was shown
   to fail that check.
5. **A 981 MB virtualenv almost went up**: the pair filter only knew `.venv`.
   Now `--exclude-if-present pyvenv.cfg` in bisync and the same check in the
   watcher (also not watched at all).
6. **A file created by the watcher and deleted before the next bisync came
   back**: bisync never saw it, so it only saw "new in the cloud". Fixed: the
   watcher deletes single files in the cloud itself when the cloud version is
   the known one.
7. **rclone's own downloads looked like deletions** (it replaces files) →
   deletions are confirmed after 2 s.
8. **LibreOffice saves via a temp file + rename** → a `MOVED_TO` whose source
   was never known counts as a write, not a move.
9. **Growing files** (Claude Code session `.jsonl`, 11 MB, written every few
   seconds) → debounce grows with size.
10. **`.git/FETCH_HEAD` changed during upload** → bisync critical abort → now
    auto-resync. Syncing live `.git` folders stays racy; git repos are better
    moved via git and excluded from pairs.
11. **SQLite `*-wal`/`*-shm` of running programs** caused conflicts → in `JUNK`.
12. **A Google refresh token ended up in a Claude Code session transcript**
    (printed while debugging `rclone config`), which a folder pair would upload.
    Never print `rclone config dump`/`show` unfiltered; grep session folders for
    `ya29.`, `1//0…`, `GOCSPX-` before syncing them; revoke at
    myaccount.google.com/permissions if it happens.
13. **Testing GNOME Shell without logging out**:
    `dbus-run-session -- env GSETTINGS_BACKEND=keyfile XDG_CONFIG_HOME=<tmp> XDG_RUNTIME_DIR=<short tmp> gnome-shell --headless --no-x11 --virtual-monitor 1280x800 --wayland-display wcs`
    with `enabled-extensions` in `<tmp>/glib-2.0/settings/keyfile`; inside the
    same session `gnome-extensions info <uuid>` shows the state. Socket paths
    must stay under 108 bytes. Side effect: the test shell's extension updater
    staged updates of *other* installed extensions into
    `~/.local/share/gnome-shell/extension-updates/`.
14. **Testing the GTK window without a display**: `gtk4-broadwayd :7 &` and
    `GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python3 …`.
15. **GNOME 46 search providers from extensions**: with `appInfo == null` the
    results show as an app grid, and clicking the header calls
    `Shell.AppSystem.lookup_app(appInfo.get_id())` → the provider needs a real
    `.desktop` file (`cloudsync.desktop`). On Wayland a new extension loads only
    after logging out and in.
16. **Live speeds measured with Nextcloud** (activity feed, 30 s poll): ~5 s
    upload after save, 10–15 s download, single deletion ~5 s.

## 5. Open issues (in order of importance)

1. ~~**Watcher restarts after every full sync.**~~ Fixed: the signature is
   the profile content, the cache is only rewritten when it changes, exit 0.
   (Was: `profile_signature()` uses
   the mtime of `<root>/.cloudsync/profile-cache.json`, but `load_profile()`
   rewrites that file on every call (each `cloudsync sync`, `index`, GUI). In
   two days: 39 restarts, all `status=3`, systemd logs each as "Failed". Fix:
   signature = hash of the profile's `remotes`, `pairs`, `settings`; exit 0 on
   a real change (`Restart=always` restarts anyway); optionally write the cache
   only when the content differs.)
2. **Live application data in folder pairs.** A Home Assistant config inside a
   synced project (`ha-config/.storage/`) produced 68 conflict entries in two
   days; session files and `.git` produced two critical aborts (recovered by the
   auto-resync). Ideas: a per-folder ignore file (e.g. `.cloudsyncignore` with
   rclone filter lines, read by bisync via `--filter-from` and by the watcher),
   a default exclude for known app-state folders, retrying a file that changed
   during upload instead of aborting.
3. **Conflict losers echo through the activity feed.** bisync's rename of a
   loser (`NAME.cs-cloud1`) shows up in the Nextcloud activity as "moved",
   which marks the unit dirty → an extra full sync each time. Ignore feed
   entries whose name matches `LOSER_RE` (and the backup paths).
4. **`DriveFeed` is untested against the real API** (blocked by the shared
   client ID quota). To verify with an own client ID: path resolution via
   parents and the index, files in "Shared with me" folders (does the
   account's changes feed include them for a `root_folder_id` remote?),
   trashed/removed, Google Docs (ignored), moves (→ dirty), `me`/displayName.
   Add a test with a fake feed (calling `DriveFeed.change()` with recorded API
   responses).
5. **Nextcloud rename detection** relies on the English `subject` text
   ("moved"/"renamed"); better: use `subject_rich`/activity types, or compare
   `objects` ids.
6. `fix_conflicts` deletes the cloud copies per folder using the *local*
   (encoded) folder name; folders whose names need the Windows encoding fall
   through (loser stays in the cloud until handled by hand).
7. Windows: the watcher is Linux only; the Tk window has no History/Settings tabs.
8. `history.jsonl` is pruned only after full syncs (fine), but the GUI reads up
   to 3000 lines each time; may need an index or a smaller window.
9. Extension: only tested on GNOME 46; spawning `cloudsync` and `systemctl`
   from the shell is a review point for extensions.gnome.org; the indexer could
   move into the extension before submitting there. Recent-changes items all
   open the History tab (no per-entry action yet).
10. OneDrive for Business: rclone works technically, but organisations often
    require admin consent for rclone's app (seen: "Need admin approval"). A
    feed would use Microsoft Graph `delta`.

11. Every Google Drive request is slow while the shared client ID is
    throttled (loading the profile took 38 s, listing My Drive 6 s). The GUI no
    longer waits for it (cached profile first, current one in the background,
    saving in the background), but folder listing and "Open in browser" for
    Drive folders still do. The own client ID is the real fix.

12. The watcher starts a full sync at once for units whose filter digest
    differs from the last good bisync (`needs_sync`): new remotes and newly
    ticked folders no longer wait for the timer. `pair_filters()` and
    `filter_digest()` are shared with `bisync()` so both compute the same text.
13. Mount: the Drive mount waits for the throttled shared client ID (22 s to
    mount My Drive); the extension shows "Connecting to …" after 2 s and falls
    back to the browser if `systemctl start` fails.

14. Synced folders vanish from the cloud search (it hides local files), so the
    desktop search must index them: `cloudsync sync` adds `<root>/<remote>` of
    every synced remote to Tracker's `index-recursive-directories`.

## 6. Ideas and backlog

- Per-remote notification and history filters (e.g. hide a session-file pair).
- "Restore" button in the History tab (copy a backup back, upload it).
- Show backup size and a "clean now" action in Settings.
- Folder-level "keep local only" / "cloud only" modes beyond on/off.
- Pause/resume watching from the top-bar menu.
- A test for the profile signature and for feed handling (see open issues 1, 4).
