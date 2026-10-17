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
- The search extension lists matching cloud-only files in the overview search;
  clicking one opens it in the browser.

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
systemctl --user enable --now cloudsync.timer cloudsync-index.timer
```

`cloudsync.timer` syncs every 15 minutes (desktop notification on failure),
`cloudsync-index.timer` refreshes the search index every 6 hours.

For the search extension, log out and in once (Wayland), then
`gnome-extensions enable cloudsync-search@htlemke.github.io`.

## Commands

- `cloudsync gui [--tk]`: pick folders per remote
- `cloudsync sync [REMOTE|PAIR …] [--dry-run] [--resync] [--notify]`
- `cloudsync status`: what is selected, free disk space
- `cloudsync pair add NAME LOCAL REMOTE [--filter '+ /sub/**' …]`, `cloudsync pair remove NAME`
- `cloudsync index [REMOTE …]`: file-name index for the search extension
- `cloudsync init [--machine NAME]`

## Where things are

| What | Where |
|---|---|
| synced files | `~/cloud/<remote>/`; change with `~/.config/cloudsync/local.json`: `{"root": "/path"}` |
| folder selection and pairs | `gdrive:.cloudsync/profiles/<machine>.json` |
| machine name, bisync state, filters, logs | `<root>/.cloudsync/` |
| search index | `~/.cache/cloudsync/index.tsv` |
| credentials | rclone's own `rclone.conf`, never touched |

A change of selection triggers a `--resync` (newer file wins) on the next sync.
Deselecting a folder offers to delete its local copy; the cloud copy stays.
In pairs, Python caches, `.pixi`, `.venv` and `node_modules` are skipped;
`.git` folders are synced, so don't work on the same repo on two computers
between syncs.

## Search extension

`cloudsync index` lists the file and folder names of every enabled remote
(no content is downloaded). The extension (`search/`) searches that list as you
type and leaves out results that exist locally, since the normal file search
finds those. Google Drive results open in the browser; clicking the section
header opens Google Drive's own search.

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

## License

MIT
