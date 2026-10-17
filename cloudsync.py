#!/usr/bin/env python3
"""cloudsync: selective two-way sync of cloud remotes with rclone bisync.

Each rclone remote (gdrive, nextcloud, onedrive, nas, ...) is synced
into <root>/<remote>/, but only the folders selected in the GUI. The selection
("profile") lives on gdrive, one file per machine, so it is the same for Linux
and Windows on one laptop and can differ between laptops:

    gdrive:.cloudsync/profiles/<machine>.json

Machine-local state shared by both OSes of a dual-boot laptop sits on the
shared NTFS disk:

    <root>/.cloudsync/machine.txt        machine name (e.g. laptop1)
    <root>/.cloudsync/profile-cache.json last profile seen (offline fallback)
    <root>/.cloudsync/bisync/            rclone bisync listings
    <root>/.cloudsync/filters/           generated filter files
    <root>/.cloudsync/logs/

Per OS: rclone.conf (tokens; same remote names on both OSes) and
<config dir>/cloudsync/local.json with {"root": "..."} if the default root
(~/cloud on Linux) doesn't fit, e.g. "/mnt/data/cloud" or, on Windows, "D:\\cloud".
On a dual-boot laptop, point both OSes at the same folder on a shared NTFS disk.

Usage:
    cloudsync gui
    cloudsync sync [REMOTE ...] [--resync] [--dry-run] [--notify]
    cloudsync status
    cloudsync index [REMOTE ...]
    cloudsync init [--machine NAME]
"""

import argparse
import hashlib
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

SETTINGS_REMOTE = "gdrive"
SETTINGS_DIR = ".cloudsync"
IS_WINDOWS = platform.system() == "Windows"
# rclone's default name encoding on Windows. Used on Linux too, so a file name
# that Windows can't store gets the same substitute name on both OSes.
WINDOWS_ENCODING = ("Slash,LtGt,DoubleQuote,Colon,Question,Asterisk,Pipe,"
                    "BackSlash,Ctl,RightSpace,RightPeriod,InvalidUtf8,Dot")
JUNK = ["**/.DS_Store", "**/Thumbs.db", "**/desktop.ini", "**/~$*", "**/.~lock.*#"]


# --- configuration ---------------------------------------------------------

def config_dir():
    if IS_WINDOWS:
        return Path(os.environ["APPDATA"]) / "cloudsync"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cloudsync"


def local_root():
    f = config_dir() / "local.json"
    if f.exists():
        return Path(json.loads(f.read_text())["root"])
    if IS_WINDOWS:
        sys.exit(f"Set the sync root in {f}, e.g. {{\"root\": \"D:\\\\cloud\"}}")
    return Path.home() / "cloud"


def state_dir():
    return local_root() / ".cloudsync"


def machine_name():
    f = state_dir() / "machine.txt"
    if not f.exists():
        sys.exit("Not initialised. Run: cloudsync init --machine <name>")
    return f.read_text().strip()


def rclone(*args, check=True, env=None):
    exe = shutil.which("rclone") or sys.exit("rclone not found")
    r = subprocess.run([exe, *args], capture_output=True, text=True, env=env)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip() or f"rclone {args[0]} failed")
    return r


def list_remotes():
    """{name: type} of the rclone remotes configured on this OS."""
    out = rclone("listremotes", "--long").stdout
    remotes = {}
    for line in out.splitlines():
        name, _, typ = line.partition(":")
        if name:
            remotes[name.strip()] = typ.strip()
    return remotes


# --- profile ---------------------------------------------------------------

def profile_path(machine):
    return f"{SETTINGS_REMOTE}:{SETTINGS_DIR}/profiles/{machine}.json"


def load_profile():
    """Profile from gdrive, falling back to the local cache when offline."""
    cache = state_dir() / "profile-cache.json"
    r = rclone("cat", profile_path(machine_name()), check=False)
    if r.returncode == 0 and r.stdout.strip():
        prof = json.loads(r.stdout)
        cache.write_text(json.dumps(prof, indent=2))
        return prof, True
    if cache.exists():
        return json.loads(cache.read_text()), False
    return {"version": 1, "remotes": {}}, r.returncode == 0


def save_profile(prof):
    text = json.dumps(prof, indent=2, sort_keys=True)
    (state_dir() / "profile-cache.json").write_text(text)
    r = subprocess.run([shutil.which("rclone"), "rcat", profile_path(machine_name())],
                       input=text, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("Saved locally, but not to gdrive: " + r.stderr.strip())


# A selection is a dict of rules {folder path: True (sync) / False (don't)};
# "" is the whole remote. A folder follows the nearest rule on itself or a
# parent, so "A" on + "A/B" off + "A/B/C" on works at any depth.

def parent(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


def inside(path, top):
    """path is top or below it."""
    return top == "" or path == top or path.startswith(top + "/")


def effective(rules, path):
    while True:
        if path in rules:
            return rules[path]
        if path == "":
            return False
        path = parent(path)


def tri_state(rules, path):
    """'on', 'off' or 'partial' (a rule further down differs)."""
    eff = effective(rules, path)
    if any(v != eff for p, v in rules.items() if p != path and inside(p, path)):
        return "partial"
    return "on" if eff else "off"


def prune(rules):
    """Drop rules that repeat what they'd inherit anyway."""
    out = {}
    for p in sorted(rules, key=lambda p: (p.count("/") + (p != ""), p)):
        inherited = False if p == "" else effective(out, parent(p))
        if rules[p] != inherited:
            out[p] = rules[p]
    return out


def toggle(rules, path):
    """Click on a folder: all on, unless it's fully on already."""
    target = tri_state(rules, path) != "on"
    new = {p: v for p, v in rules.items() if not inside(p, path)}
    new[path] = target
    return prune(new)


def get_rules(cfg):
    rules = {p.strip("/"): bool(v) for p, v in cfg.get("rules", {}).items()}
    for f in cfg.get("folders", []):  # profile format of the first version
        rules[f.strip("/")] = True
    return prune(rules)


# --- sync ------------------------------------------------------------------

def glob_escape(s):
    return "".join("\\" + c if c in "*?[]{}\\" else c for c in s)


LINK_SUFFIX = ".link.html"


def render_filters(name, rules, links=False):
    """Filters for bisync, or with links=True for the Google Docs link files only."""
    lines = [f"# generated by cloudsync for {name}; edit the selection in the GUI"]
    lines += [f"- {j}" for j in JUNK]
    if name == SETTINGS_REMOTE:
        lines.append(f"- /{SETTINGS_DIR}/**")
    if not links:
        # link files are made by sync_links; bisync must never upload them
        lines.append(f"- *{LINK_SUFFIX}")
    # rclone uses the first matching rule, so the deepest folders go first
    for p in sorted(rules, key=lambda p: (-p.count("/") - (p != ""), p)):
        pattern = "/**" if p == "" else f"/{glob_escape(p)}/**"
        if links and rules[p]:
            pattern += glob_escape(LINK_SUFFIX)
        lines.append(f"{'+' if rules[p] else '-'} {pattern}")
    lines.append("- **")
    return "\n".join(lines) + "\n"


def sync_links(name, rules, dry_run=False):
    """One-way: a NAME.link.html file per Google Doc/Sheet/Slides that opens it
    in the browser (like Insync's .gdoc files). Deleted docs lose their link."""
    sd = state_dir()
    ffile = sd / "filters" / f"{name}.links.txt"
    ffile.write_text(render_filters(name, rules, links=True))
    args = ["sync", f"{name}:", str(local_root() / name),
            "--drive-export-formats", "link.html", "--filter-from", str(ffile),
            "-v", "--log-file", str(sd / "logs" / f"{name}.log")]
    if not IS_WINDOWS:
        args += ["--local-encoding", WINDOWS_ENCODING]
    if dry_run:
        args.append("--dry-run")
    return rclone(*args, check=False).returncode


def orphans(top, rules, rel=""):
    """Local files/folders under top that the rules no longer sync (topmost only)."""
    d = top / rel if rel else top
    if not d.is_dir():
        return []
    if not effective(rules, rel) and not any(v and inside(p, rel) for p, v in rules.items()):
        return [rel] if rel else []
    out = []
    for entry in sorted(d.iterdir()):
        sub = f"{rel}/{entry.name}" if rel else entry.name
        if entry.is_dir():
            out += orphans(top, rules, sub)
        elif not effective(rules, rel):
            out.append(sub)
    return out


def remove_local(top, paths):
    for rel in paths:
        p = top / rel
        shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)


def bisync(key, path1, path2, filters, resync=False, dry_run=False, encode=False,
           extra=()):
    """rclone bisync path1 <-> path2; key names the filter, marker and log files."""
    sd = state_dir()
    for d in ("bisync", "filters", "logs"):
        (sd / d).mkdir(parents=True, exist_ok=True)
    ffile = sd / "filters" / f"{key}.txt"
    if not ffile.exists() or ffile.read_text() != filters:
        ffile.write_text(filters)
    digest = hashlib.sha256(filters.encode()).hexdigest()
    # bisync names its listings after the real local path, which differs between
    # Linux and Windows, so each OS keeps its own listings and its own marker
    marker = sd / "bisync" / f"{key}.{platform.system().lower()}.cloudsync-ok"
    # first run, or selection changed since the last good run -> resync
    if not marker.exists() or marker.read_text().strip() != digest:
        resync = True
    args = ["bisync", str(path1), path2,
            "--workdir", str(sd / "bisync"),
            "--filters-file", str(ffile),
            "--create-empty-src-dirs", "--compare", "size,modtime",
            "--resilient", "--recover", "--max-lock", "5m",
            "--conflict-resolve", "newer", "--conflict-loser", "num",
            "-v", "--log-file", str(sd / "logs" / f"{key}.log"), *extra]
    if encode and not IS_WINDOWS:
        args += ["--local-encoding", WINDOWS_ENCODING]
    if resync:
        args += ["--resync", "--resync-mode", "newer"]
    if dry_run:
        args.append("--dry-run")
    rc = rclone(*args, check=False).returncode
    if rc == 0 and not dry_run:
        marker.write_text(digest)
    return rc, resync


def sync_remote(name, rtype, rules, resync=False, dry_run=False):
    local = local_root() / name
    local.mkdir(parents=True, exist_ok=True)
    rc, resync = bisync(name, local, f"{name}:", render_filters(name, rules),
                        resync, dry_run, encode=True,
                        extra=["--drive-skip-gdocs"] if rtype == "drive" else [])
    if rc == 0 and rtype == "drive":
        rc = sync_links(name, rules, dry_run)
    return rc, resync


# Pairs: any local folder <-> any remote path, e.g. ~/code <-> nextcloud:sync/code.
# Stored per machine in the profile under "pairs"; Linux paths, so Linux only.

PAIR_DEFAULT_FILTERS = ["- **/__pycache__/**", "- **/.pixi/**", "- **/.venv/**",
                        "- **/node_modules/**", "- **/.ipynb_checkpoints/**"]


def sync_pair(name, cfg, resync=False, dry_run=False):
    local = Path(os.path.expanduser(cfg["local"]))
    local.mkdir(parents=True, exist_ok=True)
    rclone("mkdir", cfg["remote"], check=False)  # bisync needs both sides to exist
    lines = [f"# generated by cloudsync for pair {name}"]
    lines += [f"- {j}" for j in JUNK] + cfg.get("filters", [])
    if any(f.startswith("+") for f in cfg.get("filters", [])):
        lines.append("- **")
    return bisync(f"pair-{name}", local, cfg["remote"], "\n".join(lines) + "\n",
                  resync, dry_run)


def cmd_sync(args):
    prof, online = load_profile()
    remotes = list_remotes()
    failed = []
    for name, cfg in prof["remotes"].items():
        if args.remote and name not in args.remote:
            continue
        rules = get_rules(cfg)
        if not cfg.get("enabled") or not any(rules.values()):
            continue
        if name not in remotes:
            print(f"{name}: no such rclone remote on this OS, skipped")
            continue
        rc, resynced = sync_remote(name, remotes[name], rules, args.resync, args.dry_run)
        print(f"{name}: {'ok' if rc == 0 else f'FAILED (exit {rc})'}"
              f"{' (resync)' if resynced else ''}")
        if rc:
            failed.append(name)
    for name, cfg in prof.get("pairs", {}).items():
        if args.remote and name not in args.remote:
            continue
        if not cfg.get("enabled", True) or IS_WINDOWS:
            continue
        rc, resynced = sync_pair(name, cfg, args.resync, args.dry_run)
        print(f"pair {name}: {'ok' if rc == 0 else f'FAILED (exit {rc})'}"
              f"{' (resync)' if resynced else ''}")
        if rc:
            failed.append(name)
    if not online:
        print("note: gdrive unreachable, used the cached profile")
    if failed and args.notify and shutil.which("notify-send"):
        subprocess.run(["notify-send", "cloudsync failed", ", ".join(failed) +
                        f"\nsee {state_dir() / 'logs'}"])
    return 1 if failed else 0


def cmd_status(args):
    prof, online = load_profile()
    print(f"machine {machine_name()}, root {local_root()}, profile "
          f"{'from gdrive' if online else 'from cache'}")
    for name, cfg in sorted(prof["remotes"].items()):
        state = "on " if cfg.get("enabled") else "off"
        rules = get_rules(cfg)
        print(f"  [{state}] {name}: " + (", ".join(f"{'+' if v else '-'}{p or '/'}"
                                                   for p, v in sorted(rules.items()))
                                         or "(nothing selected)"))
    for name, cfg in sorted(prof.get("pairs", {}).items()):
        state = "on " if cfg.get("enabled", True) else "off"
        print(f"  [{state}] pair {name}: {cfg['local']} <-> {cfg['remote']}")
        for f in cfg.get("filters", []):
            print(f"          {f}")
    du = shutil.disk_usage(local_root())
    print(f"free on disk: {du.free / 1e9:.1f} GB")


def cmd_pair(args):
    prof, online = load_profile()
    if not online:
        sys.exit("gdrive unreachable: can't change the profile")
    pairs = prof.setdefault("pairs", {})
    if args.action == "add":
        pairs[args.name] = {"local": args.local, "remote": args.remote, "enabled": True,
                            "filters": PAIR_DEFAULT_FILTERS + (args.filter or [])}
    elif args.action == "remove":
        pairs.pop(args.name, None)
    save_profile(prof)
    cmd_status(args)


def cmd_init(args):
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    f = sd / "machine.txt"
    name = args.machine or (f.read_text().strip() if f.exists() else socket.gethostname())
    f.write_text(name + "\n")
    print(f"machine name: {name} (in {f})")
    print(f"profile: {profile_path(name)}")


# --- search index ----------------------------------------------------------

# File names of the whole remotes (not only the synced folders) for the GNOME
# Shell search extension in search/, so it also finds cloud-only files.

INDEX_VERSION = 1
DRIVE_URL = "https://drive.google.com/open?id={id}"
DRIVE_SEARCH = "https://drive.google.com/drive/search?q={terms}"


def index_file():
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "cloudsync" / "index.tsv"


def tsv(s):
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def read_index_blocks(path):
    """{remote: [lines]} of an existing index, to keep remotes not refreshed."""
    blocks, cur = {}, None
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#source\t"):
                cur = blocks.setdefault(line.split("\t")[1], [])
            elif cur is None or line.startswith("#"):
                continue
            cur.append(line)
    return blocks


def cmd_index(args):
    prof, _ = load_profile()
    remotes = list_remotes()
    names = [n for n, cfg in prof["remotes"].items()
             if cfg.get("enabled") and n in remotes]
    out = index_file()
    old = read_index_blocks(out)
    blocks, failed = {}, []
    for name in names:
        if args.remote and name not in args.remote:
            if name in old:
                blocks[name] = old[name]
            continue
        cmd = ["lsjson", "-R", "--fast-list", "--no-modtime", "--no-mimetype",
               # Google Docs as NAME.link.html, the name of their local link file
               "--drive-export-formats", "link.html", f"{name}:"]
        if name == SETTINGS_REMOTE:
            cmd += ["--exclude", f"/{SETTINGS_DIR}/**"]
        r = rclone(*cmd, check=False)
        if r.returncode:
            print(f"{name}: FAILED: {r.stderr.strip().splitlines()[-1:]}")
            failed.append(name)
            if name in old:
                blocks[name] = old[name]
            continue
        drive = remotes[name] == "drive"
        head = "\t".join(["#source", name, name, str(local_root() / name),
                          DRIVE_URL if drive else "", DRIVE_SEARCH if drive else ""])
        items = json.loads(r.stdout or "[]")
        blocks[name] = [head] + [f"{name}\t{int(it['IsDir'])}\t{it.get('ID', '')}\t{tsv(it['Path'])}"
                                 for it in items]
        print(f"{name}: {len(items)} entries")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(f"#cloudsync-index\t{INDEX_VERSION}\n" +
                   "".join(line + "\n" for b in blocks.values() for line in b),
                   encoding="utf-8")
    os.replace(tmp, out)
    return 1 if failed else 0


# --- GUI -------------------------------------------------------------------

MARK = {"on": "☑", "off": "☐", "partial": "◩"}


class Selection:
    """GUI-independent state: profile, rules and enabled flag per remote."""

    def __init__(self):
        self.prof, self.online = load_profile()
        self.remotes = list_remotes()
        cfgs = self.prof["remotes"]
        self.rules = {n: get_rules(cfgs.get(n, {})) for n in self.remotes}
        self.enabled = {n: cfgs.get(n, {}).get("enabled", False) for n in self.remotes}

    def toggle(self, name, path):
        self.rules[name] = toggle(self.rules[name], path)
        if any(self.rules[name].values()):
            self.enabled[name] = True

    def save(self):
        """Save the profile; returns {remote: [local paths no longer synced]}."""
        for n in self.remotes:
            self.prof["remotes"][n] = {"enabled": self.enabled[n],
                                       "rules": self.rules[n]}
        self.prof["version"] = 2
        save_profile(self.prof)
        return {n: o for n in self.remotes
                if (o := orphans(local_root() / n, self.rules[n]))}

    def sync_all(self):
        return [(n, sync_remote(n, self.remotes[n], self.rules[n])[0])
                for n in self.remotes if self.enabled[n] and any(self.rules[n].values())]


def list_dirs(name, path):
    out = rclone("lsjson", "--dirs-only", f"{name}:{path}").stdout
    return sorted((e["Name"] for e in json.loads(out)), key=str.lower)


def folder_size(name, path):
    return json.loads(rclone("size", "--json", f"{name}:{path}").stdout)["bytes"]


def cmd_gui(args):
    if not IS_WINDOWS and not args.tk:
        try:
            import cloudsync_gtk
        except (ImportError, ValueError):
            pass  # no GTK 4 / libadwaita: fall back to Tk
        else:
            return cloudsync_gtk.run(sys.modules[__name__])
    return gui_tk()


def gui_tk():
    import tkinter as tk
    from tkinter import messagebox, ttk

    sel = Selection()
    root = tk.Tk(className="cloudsync")
    root.title(f"cloudsync – {machine_name()}")
    root.geometry("760x560")
    enabled = {n: tk.BooleanVar(value=sel.enabled[n]) for n in sel.remotes}
    status = tk.StringVar(value="" if sel.online else
                          "gdrive unreachable – showing cached profile")
    results = queue.Queue()

    def bg(fn, done):
        """Run fn in a thread, then done(result) in the Tk thread."""
        def run():
            try:
                res = fn()
            except Exception as e:  # shown in the status bar
                res = e
            results.put((done, res))
        threading.Thread(target=run, daemon=True).start()

    def poll():
        while not results.empty():
            done, res = results.get()
            if isinstance(res, Exception):
                status.set(f"Error: {res}")
            else:
                done(res)
        root.after(100, poll)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=6, pady=6)
    trees = {}
    SEP = "\x1f"

    def path_of(iid):
        return iid.split(SEP, 1)[1]

    def refresh(name):
        tv = trees[name]

        def walk(item):
            for c in tv.get_children(item):
                if not c.endswith(SEP + "dummy"):
                    tv.set(c, "sel", MARK[tri_state(sel.rules[name], path_of(c))])
                    walk(c)
        walk("")

    def load_children(name, item):
        tv = trees[name]
        path = path_of(item)

        def done(dirs):
            for c in tv.get_children(item):
                tv.delete(c)
            for d in dirs:
                p = f"{path}/{d}" if path else d
                iid = f"{name}{SEP}{p}"
                tv.insert(item, "end", iid=iid, text=d,
                          values=(MARK[tri_state(sel.rules[name], p)], ""))
                tv.insert(iid, "end", iid=iid + SEP + "dummy", text="…")
            status.set(f"{name}:{path or '/'} – {len(dirs)} folders")
        status.set(f"Listing {name}:{path or '/'} …")
        bg(lambda: list_dirs(name, path), done)

    def load_opened(name, item=""):
        """Load every opened folder that still shows the '…' placeholder."""
        tv = trees[name]
        for c in tv.get_children(item):
            if c.endswith(SEP + "dummy") or not tv.item(c, "open"):
                continue
            kids = tv.get_children(c)
            if len(kids) == 1 and kids[0].endswith(SEP + "dummy"):
                tv.item(kids[0], text="loading …")
                load_children(name, c)
            else:
                load_opened(name, c)

    def on_click(event, name):
        tv = trees[name]
        item = tv.identify_row(event.y)
        if tv.identify_column(event.x) != "#1" or not item or item.endswith(SEP + "dummy"):
            return
        sel.toggle(name, path_of(item))
        enabled[name].set(sel.enabled[name])
        refresh(name)

    def size_of(name, item):
        tv = trees[name]
        path = path_of(item)
        status.set(f"Measuring {name}:{path or '/'} …")
        bg(lambda: folder_size(name, path),
           lambda b: (tv.set(item, "size", f"{b / 1e9:.2f} GB"),
                      status.set(f"{name}:{path or '/'}: {b / 1e9:.2f} GB")))

    for name in sel.remotes:
        frame = ttk.Frame(nb)
        nb.add(frame, text=name)
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Checkbutton(top, text="sync this remote", variable=enabled[name]).pack(side="left")
        ttk.Label(top, text=f"→ {local_root() / name}").pack(side="left", padx=10)
        tv = ttk.Treeview(frame, columns=("sel", "size"), selectmode="browse")
        tv.heading("#0", text="Folder")
        tv.heading("sel", text="Sync")
        tv.heading("size", text="Size")
        tv.column("sel", width=60, anchor="center", stretch=False)
        tv.column("size", width=90, anchor="e", stretch=False)
        sb = ttk.Scrollbar(frame, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(fill="both", expand=True)
        trees[name] = tv
        rid = f"{name}{SEP}"
        tv.insert("", "end", iid=rid, text="(whole remote)", open=True,
                  values=(MARK[tri_state(sel.rules[name], "")], ""))
        # after the click has opened the item
        tv.bind("<<TreeviewOpen>>", lambda e, n=name: root.after_idle(load_opened, n))
        tv.bind("<Button-1>", lambda e, n=name: on_click(e, n))
        load_children(name, rid)

    bottom = ttk.Frame(root)
    bottom.pack(fill="x", padx=6, pady=(0, 6))
    free = shutil.disk_usage(local_root()).free / 1e9
    ttk.Label(bottom, text=f"Free on {local_root()}: {free:.1f} GB").pack(side="left")

    def current():
        return nb.tab(nb.select(), "text")

    def save(then_sync=False):
        for n in sel.remotes:
            sel.enabled[n] = enabled[n].get()
        try:
            gone = sel.save()
            status.set("Saved to gdrive")
        except RuntimeError as e:
            status.set(str(e))
            return
        listing = [f"{n}: {p}" for n, ps in gone.items() for p in ps]
        if listing and messagebox.askyesno(
                "No longer synced",
                "These local files/folders are no longer synced. Delete the local "
                "copies? (The cloud copies stay.)\n\n" + "\n".join(listing[:20]) +
                (f"\n… and {len(listing) - 20} more" if len(listing) > 20 else "")):
            for n, ps in gone.items():
                remove_local(local_root() / n, ps)
        if then_sync:
            status.set("Syncing … (logs in .cloudsync/logs)")
            bg(sel.sync_all, lambda res: status.set(
                "Sync: " + ", ".join(f"{n} {'ok' if rc == 0 else 'FAILED'}" for n, rc in res)))

    ttk.Button(bottom, text="Close", command=root.destroy).pack(side="right")
    ttk.Button(bottom, text="Save & sync now", command=lambda: save(True)).pack(side="right")
    ttk.Button(bottom, text="Save", command=save).pack(side="right")
    ttk.Button(bottom, text="Size of folder",
               command=lambda: (trees[current()].focus() and
                                size_of(current(), trees[current()].focus()))
               ).pack(side="right")
    ttk.Label(root, textvariable=status, anchor="w").pack(fill="x", padx=6, pady=(0, 4))
    root.after(100, poll)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(prog="cloudsync", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gui")
    g.add_argument("--tk", action="store_true", help="use the Tk window (default on Windows)")
    s = sub.add_parser("sync")
    s.add_argument("remote", nargs="*")
    s.add_argument("--resync", action="store_true", help="force a resync")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--notify", action="store_true", help="desktop notification on failure")
    sub.add_parser("status")
    ix = sub.add_parser("index", help="list all file names for the search extension")
    ix.add_argument("remote", nargs="*")
    pr = sub.add_parser("pair", help="sync any local folder with a remote path")
    psub = pr.add_subparsers(dest="action", required=True)
    pa = psub.add_parser("add")
    pa.add_argument("name")
    pa.add_argument("local", help="e.g. ~/code (kept with ~ in the profile)")
    pa.add_argument("remote", help="e.g. nextcloud:sync/code")
    pa.add_argument("--filter", action="append",
                    help="extra rclone filter line, e.g. '+ /projects/**' (repeatable)")
    psub.add_parser("remove").add_argument("name")
    i = sub.add_parser("init")
    i.add_argument("--machine")
    args = ap.parse_args()
    return {"gui": cmd_gui, "sync": cmd_sync, "status": cmd_status,
            "init": cmd_init, "pair": cmd_pair, "index": cmd_index}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
