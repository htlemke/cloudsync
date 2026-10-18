"""cloudsync watch: sync single changes within seconds (Linux).

The full `cloudsync sync` (rclone bisync) stays the reference and runs on its
timer. The watcher only adds a fast path for single files:

- local: inotify reports a saved file; after a few quiet seconds it is uploaded
- cloud: Google Drive's changes feed and Nextcloud's activity feed are polled
  (settings "poll_seconds"); changed files are downloaded
- both changed since the last sync: the newer version wins, the other goes to
  the local backup, as in `cloudsync sync`

Anything else (deleted or renamed folders, deletions in the local folder, names
that need the Windows encoding, errors) marks the folder "dirty", and a full
bisync of it runs a minute later. That keeps bisync's safety checks in charge
of everything that can go badly wrong.

Transfers go through one `rclone rcd` on a unix socket. Known file states come
from bisync's listings, plus what the watcher did since the last bisync.
"""

import base64
import ctypes
import ctypes.util
import datetime
import http.client
import json
import os
import queue
import re
import select
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

QUIET = 3          # seconds without events before a local file is uploaded,
QUIET_PER_MB = 4   # plus this per MB: big files that keep growing (logs,
QUIET_MAX = 120    # session files) go up once instead of on every write
DIRTY_DELAY = 60   # seconds after the last "dirty" event before a full bisync
MASS_DELETE = 20   # more local deletions than this within 10 s: leave to bisync
TOLERANCE = 1.5    # modification times closer than this count as equal
IGNORE_RE = re.compile(r"(\.partial|\.swp|\.swx|~|\.crdownload|\.part|\.kate-swp)$"
                       r"|^\.goutputstream-|^\.#|^\.~lock\.|^~\$")
# characters that the Windows encoding replaces: such names go via bisync
ENCODED_RE = re.compile(r'[:?*|"<>\\\x00-\x1f\uff01-\uff5e\u2400-\u2424\u201b]|[ .]$')


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def parse_time(s):
    """RFC 3339 with up to nanoseconds -> unix time."""
    s = s.replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    frac = (m[2] or ".0")[:7]
    tz = m[3].replace("+0000", "+00:00") if m[3] else "+00:00"
    if re.fullmatch(r"[+-]\d{4}", tz):
        tz = tz[:3] + ":" + tz[3:]
    return datetime.datetime.fromisoformat(m[1] + frac + tz).timestamp()


def same(a, b):
    return bool(a and b) and a[0] == b[0] and abs(a[1] - b[1]) <= TOLERANCE


def local_stat(p):
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_size, st.st_mtime) if os.path.isfile(p) else None


# --- rclone filters ------------------------------------------------------------

def glob_to_regex(pat):
    anchored = pat.startswith("/")
    pat = pat[1:] if anchored else pat
    lead = ""
    if not anchored and pat.startswith("**/"):
        pat, lead = pat[3:], "(.*/)?"
    out, i = "", 0
    while i < len(pat):
        c = pat[i]
        if c == "\\" and i + 1 < len(pat):
            out += re.escape(pat[i + 1])
            i += 2
            continue
        if pat.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[" and "]" in pat[i:]:
            j = pat.index("]", i)
            out += pat[i:j + 1]
            i = j
        elif c == "{" and "}" in pat[i:]:
            j = pat.index("}", i)
            out += "(" + "|".join(re.escape(a) for a in pat[i + 1:j].split(",")) + ")"
            i = j
        else:
            out += re.escape(c)
        i += 1
    return re.compile(("^" if anchored else "(^|/)") + lead + out + "$")


class Filters:
    """The subset of rclone's filter rules that cloudsync generates."""

    def __init__(self, text):
        self.rules = []
        for line in text.splitlines():
            if line[:2] in ("+ ", "- "):
                pat = line[2:].strip()
                self.rules.append((line[0] == "+", glob_to_regex(pat), pat))

    def included(self, rel):
        for inc, rx, _ in self.rules:
            if rx.search(rel):
                return inc
        return True

    def dir_wanted(self, rel):
        """Can anything below directory rel be included?"""
        if self.included(rel + "/"):
            return True
        return any(inc and pat.startswith("/" + rel + "/") for inc, _, pat in self.rules)


# --- inotify ---------------------------------------------------------------------

IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO = 0x8, 0x40, 0x80
IN_CREATE, IN_DELETE, IN_DELETE_SELF, IN_MOVE_SELF = 0x100, 0x200, 0x400, 0x800
IN_Q_OVERFLOW, IN_IGNORED, IN_ISDIR = 0x4000, 0x8000, 0x40000000
WATCH_MASK = (IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
              | IN_DELETE_SELF | IN_MOVE_SELF)


class Inotify:
    def __init__(self):
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.paths = {}  # wd -> directory

    def add(self, path):
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch {path}")
        self.paths[wd] = Path(path)

    def read(self):
        """[(directory, mask, cookie, name)]"""
        try:
            data = os.read(self.fd, 1 << 16)
        except BlockingIOError:
            return []
        out, i = [], 0
        while i + 16 <= len(data):
            wd, mask, cookie, n = struct.unpack_from("iIII", data, i)
            name = os.fsdecode(data[i + 16:i + 16 + n].rstrip(b"\0"))
            i += 16 + n
            if mask & IN_IGNORED:
                self.paths.pop(wd, None)
                continue
            out.append((self.paths.get(wd), mask, cookie, name))
        return out


# --- rclone remote control ------------------------------------------------------------

def die_with_parent():
    """In the child: get SIGTERM when the watcher dies (PR_SET_PDEATHSIG)."""
    ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True).prctl(1, 15)


class RcloneError(Exception):
    pass


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Rclone:
    """One `rclone rcd` for all transfers of the watcher."""

    def __init__(self, sock, logfile):
        self.sock = str(sock)
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        self.proc = subprocess.Popen(
            ["rclone", "rcd", "--rc-no-auth", f"--rc-addr=unix://{self.sock}",
             "--log-level", "NOTICE", "--log-file", str(logfile)],
            stdin=subprocess.DEVNULL, preexec_fn=die_with_parent)
        for _ in range(100):
            if os.path.exists(self.sock):
                return
            time.sleep(0.1)
        raise RcloneError("rclone rcd didn't start")

    def call(self, cmd, timeout=600, **params):
        c = _UnixHTTP(self.sock, timeout)
        try:
            c.request("POST", "/" + cmd, json.dumps(params), {"Content-Type": "application/json"})
            r = c.getresponse()
            body = json.loads(r.read() or b"{}")
        finally:
            c.close()
        if r.status != 200:
            raise RcloneError(f"{cmd}: {body.get('error', r.status)}")
        return body

    def stat(self, fs, remote):
        """(size, mtime) of a file, None if missing."""
        item = self.call("operations/stat", fs=fs, remote=remote, timeout=60).get("item")
        if not item or item.get("IsDir"):
            return None
        return (item["Size"], parse_time(item["ModTime"]))

    def copy(self, src_fs, src, dst_fs, dst):
        self.call("operations/copyfile", srcFs=src_fs, srcRemote=src, dstFs=dst_fs, dstRemote=dst)

    def move(self, src_fs, src, dst_fs, dst):
        self.call("operations/movefile", srcFs=src_fs, srcRemote=src, dstFs=dst_fs, dstRemote=dst)

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --- what to watch ---------------------------------------------------------------------

class Unit:
    """A synced remote or pair: local folder <-> remote path."""

    def __init__(self, cs, key, local, remote, filters, encode, kind, run_bisync):
        self.cs, self.key, self.local, self.remote = cs, key, Path(local), remote
        self.filters_text = filters
        self.filters = Filters(filters)
        self.encode = encode and not cs.IS_WINDOWS
        self.kind = kind            # rclone backend type of the remote
        self.run_bisync = run_bisync
        self.rname = remote.split(":", 1)[0]
        self.rpath = remote.split(":", 1)[1].strip("/")
        enc = f':local,encoding="{cs.WINDOWS_ENCODING}":' if self.encode else ""
        self.local_fs = f"{enc}{self.local}"
        self.backup_fs = f"{enc}{cs.backup_root() / key}"
        self.state = {}
        self.dirty_at = None
        self.state_file = cs.cache_dir() / f"watch-{key}.json"
        self.marker = cs.state_dir() / "bisync" / f"{key}.{cs.platform.system().lower()}.cloudsync-ok"
        self.loaded_at = 0

    def wanted(self, rel):
        name = rel.rsplit("/", 1)[-1]
        if IGNORE_RE.search(name) or self.cs.LOSER_RE.match(name):
            return False
        return self.filters.included(rel) and not self.in_marked_dir(rel)

    def in_marked_dir(self, rel):
        """Below a folder that contains e.g. pyvenv.cfg (EXCLUDE_IF_PRESENT)?"""
        d = self.local / rel
        while True:
            d = d.parent
            if any((d / m).exists() for m in self.cs.EXCLUDE_IF_PRESENT):
                return True
            if d == self.local or self.local not in d.parents:
                return False

    def fast(self, rel):
        """Can the watcher handle this path itself?"""
        return not (self.encode and any(ENCODED_RE.search(part) for part in rel.split("/")))

    # known state: bisync listings + watcher changes since ------------------

    def load_state(self):
        wd = self.cs.state_dir() / "bisync"
        # bisync names its listings <path1>..<path2>.path1.lst, '/' -> '_'
        local_part = str(self.local).replace("/", "_").strip("_")
        state = {}
        for side, suffix in (("l", ".path1.lst"), ("r", ".path2.lst")):
            for f in wd.glob(f"*{suffix}"):
                left = f.name.split("..", 1)[0]
                if not (left == local_part or left.endswith("_" + local_part)):
                    continue
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    m = LISTING_RE.match(line)
                    if m and m[1] == "-":
                        try:
                            rel = json.loads('"' + m[4] + '"')
                        except ValueError:
                            rel = m[4]
                        state.setdefault(rel, {})[side] = (int(m[2]), parse_time(m[3]))
        try:
            for rel, v in json.loads(self.state_file.read_text()).items():
                state[rel] = {k: tuple(x) if x else None for k, x in v.items()}
        except (OSError, ValueError):
            pass
        self.state = state
        self.loaded_at = time.time()

    def refresh_if_synced(self):
        """Reload after a bisync that ran elsewhere (the timer's cloudsync sync)."""
        try:
            if self.marker.stat().st_mtime > self.loaded_at:
                self.state_file.unlink(missing_ok=True)
                self.load_state()
        except OSError:
            pass

    def remember(self, rel, l, r):
        self.state[rel] = {"l": l, "r": r}
        try:
            saved = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            saved = {}
        saved[rel] = {"l": l, "r": r}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(saved))

    def forget(self, rel):
        self.remember(rel, None, None)

    def after_bisync(self):
        self.state_file.unlink(missing_ok=True)
        self.load_state()
        self.dirty_at = None


LISTING_RE = re.compile(r'^([-d])\s+(-?\d+)\s+\S+\s+\S+\s+(\S+)\s+"(.*)"$')


def build_units(cs):
    prof, _ = cs.load_profile()
    remotes = cs.list_remotes()
    units = []
    for name, cfg in prof["remotes"].items():
        rules = cs.get_rules(cfg)
        if cfg.get("enabled") and any(rules.values()) and name in remotes:
            units.append(Unit(cs, name, cs.local_root() / name, f"{name}:",
                              cs.render_filters(name, rules), True, remotes[name],
                              lambda n=name, t=remotes[name], r=rules: cs.sync_remote(n, t, r)))
    for name, cfg in prof.get("pairs", {}).items():
        rname = cfg["remote"].split(":", 1)[0]
        if cfg.get("enabled", True) and rname in remotes:
            lines = [f"- {j}" for j in cs.JUNK] + cfg.get("filters", [])
            if any(f.startswith("+") for f in cfg.get("filters", [])):
                lines.append("- **")
            units.append(Unit(cs, f"pair-{name}", os.path.expanduser(cfg["local"]),
                              cfg["remote"], "\n".join(lines), False, remotes[rname],
                              lambda n=name, c=cfg: cs.sync_pair(n, c)))
    return prof, units


# --- cloud change feeds -----------------------------------------------------------------

def rclone_conf(remote):
    out = subprocess.run(["rclone", "config", "dump"], capture_output=True, text=True).stdout
    return json.loads(out).get(remote, {})


class QuotaError(Exception):
    pass


class DriveFeed:
    """Google Drive changes feed for the drive remotes (one account each)."""

    API = "https://www.googleapis.com/drive/v3/"
    FIELDS = ("nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,"
              "mimeType,parents,trashed,lastModifyingUser(displayName,me)))")

    def __init__(self, watcher, rname, units):
        self.w, self.rname, self.units = watcher, rname, units
        self.token_file = watcher.cs.cache_dir() / f"watch-drive-{rname}.token"
        self.access = None
        self.ids = {}      # file/folder id -> (unit, rel)
        self.meta = {}     # folder id -> (name, parent id) for folders outside the index
        self.roots = {}    # root folder id -> unit

    def request(self, path, retry=True):
        if not self.access:
            self.refresh()
        req = urllib.request.Request(self.API + path,
                                     headers={"Authorization": "Bearer " + self.access})
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 401 and retry:
                self.access = None
                subprocess.run(["rclone", "about", f"{self.rname}:"], capture_output=True)
                return self.request(path, retry=False)
            if e.code in (403, 429) and ("uota" in body or "ate" in body):
                raise QuotaError(body[:200])
            raise

    def refresh(self):
        tok = json.loads(rclone_conf(self.rname).get("token", "{}"))
        exp = tok.get("expiry", "")
        if not tok.get("access_token") or (exp and parse_time(exp) < time.time() + 60):
            # rclone refreshes the token and writes it back to rclone.conf
            subprocess.run(["rclone", "about", f"{self.rname}:"], capture_output=True)
            tok = json.loads(rclone_conf(self.rname).get("token", "{}"))
        self.access = tok.get("access_token")

    def setup(self):
        conf = rclone_conf(self.rname)
        for u in self.units:
            root = conf.get("root_folder_id") or self.request("files/root?fields=id")["id"]
            self.roots[root] = u
        self.load_ids()
        if not self.token_file.exists():
            self.token_file.write_text(self.request("changes/startPageToken")["startPageToken"])

    def load_ids(self):
        """id -> path from the search index (cloudsync index)."""
        try:
            blocks = self.w.cs.read_index_blocks(self.w.cs.index_file())
        except OSError:
            return
        for u in self.units:
            for line in blocks.get(u.key, [])[1:]:
                f = line.split("\t")
                if len(f) >= 4 and f[2]:
                    self.ids[f[2]] = (u, "\t".join(f[3:]))

    def path_of(self, fid, name, parents, depth=0):
        """(unit, rel) of a file from its parent chain, None if outside the units."""
        name = name.replace("/", "／")
        if not parents or depth > 30:
            return None
        par = parents[0]
        if par in self.roots:
            return self.roots[par], name
        if par in self.ids:
            u, rel = self.ids[par]
            return u, f"{rel}/{name}"
        if par not in self.meta:
            try:
                m = self.request(f"files/{par}?fields=name,parents&supportsAllDrives=true")
            except (urllib.error.HTTPError, urllib.error.URLError):
                return None
            self.meta[par] = (m.get("name", ""), m.get("parents", []))
        pname, pparents = self.meta[par]
        up = self.path_of(par, pname, pparents, depth + 1)
        if up:
            self.ids[par] = up
            return up[0], f"{up[1]}/{name}"
        return None

    def poll(self):
        token = self.token_file.read_text().strip()
        while True:
            r = self.request(f"changes?pageToken={token}&pageSize=1000&fields={self.FIELDS}"
                             "&includeItemsFromAllDrives=true&supportsAllDrives=true")
            for ch in r.get("changes", []):
                self.change(ch)
            if "nextPageToken" in r:
                token = r["nextPageToken"]
                continue
            self.token_file.write_text(r["newStartPageToken"])
            return

    def change(self, ch):
        f = ch.get("file") or {}
        fid = ch.get("fileId")
        old = self.ids.get(fid)
        if ch.get("removed") or f.get("trashed"):
            if old:
                self.w.cloud_event(old[0], old[1], deleted=True)
            return
        where = self.path_of(fid, f.get("name", ""), f.get("parents", []))
        mime = f.get("mimeType", "")
        if mime == "application/vnd.google-apps.folder":
            for u, rel in filter(None, (old, where)):
                if u.filters.dir_wanted(rel):
                    self.w.mark_dirty(u, f"folder {rel} changed in the cloud")
            if where:
                self.ids[fid] = where
            return
        if mime.startswith("application/vnd.google-apps."):
            return  # Google Docs: their link files follow with the next full sync
        if not where:
            return
        u, rel = where
        if old and old != where:
            self.ids[fid] = where
            self.w.mark_dirty(u, f"{old[1]} moved to {rel} in the cloud")
            return
        self.ids[fid] = where
        user = f.get("lastModifyingUser") or {}
        self.w.cloud_event(u, rel, who=user.get("displayName"), own=user.get("me", False))


class NextcloudFeed:
    """Nextcloud/ownCloud activity feed (who changed what, when)."""

    def __init__(self, watcher, rname, units):
        self.w, self.rname, self.units = watcher, rname, units
        conf = rclone_conf(rname)
        m = re.match(r"(https?://.+?)/remote\.php/(?:dav/files/[^/]+|webdav)(/.*?)?/?$",
                     conf.get("url", ""))
        if not m:
            raise ValueError(f"{rname}: not a Nextcloud WebDAV URL")
        self.base, self.sub = m[1], (m[2] or "").rstrip("/")
        self.user = conf.get("user", "")
        pw = subprocess.run(["rclone", "reveal", conf.get("pass", "")],
                            capture_output=True, text=True).stdout.strip()
        self.auth = "Basic " + base64.b64encode(f"{self.user}:{pw}".encode()).decode()
        self.since_file = watcher.cs.cache_dir() / f"watch-nextcloud-{rname}.since"

    def request(self, query):
        req = urllib.request.Request(
            f"{self.base}/ocs/v2.php/apps/activity/api/v2/activity/all?format=json&{query}",
            headers={"OCS-APIRequest": "true", "Authorization": self.auth})
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))["ocs"]["data"]
        except urllib.error.HTTPError as e:
            if e.code == 304:  # nothing new
                return []
            raise

    def setup(self):
        if not self.since_file.exists():
            latest = self.request("limit=1")
            self.since_file.write_text(str(latest[0]["activity_id"] if latest else 0))

    def poll(self):
        since = self.since_file.read_text().strip()
        for a in self.request(f"since={since}&sort=asc&limit=200"):
            self.activity(a)
            since = str(a["activity_id"])
        self.since_file.write_text(since)

    def activity(self, a):
        typ = a.get("type", "")
        if typ not in ("file_created", "file_changed", "file_deleted", "file_restored"):
            return
        subject = a.get("subject", "").lower()
        own = a.get("user", "") == self.user
        paths = list((a.get("objects") or {}).values()) or [a.get("object_name", "")]
        for p in paths:
            p = p[len(self.sub):] if self.sub and p.startswith(self.sub) else p
            p = p.strip("/")
            for u in self.units:
                top = u.rpath
                if top and not (p == top or p.startswith(top + "/")):
                    continue
                rel = p[len(top):].strip("/") if top else p
                if not rel:
                    continue
                if "moved" in subject or "renamed" in subject:
                    self.w.mark_dirty(u, f"{rel} moved in the cloud")
                else:
                    self.w.cloud_event(u, rel, deleted=typ == "file_deleted",
                                       who=a.get("user"), own=own)


# --- notifications -------------------------------------------------------------------------

class Notifier:
    """Desktop notifications for changes that came from the cloud."""

    def __init__(self, cs, prof):
        self.cs, self.items, self.last = cs, [], 0
        self.update(prof)

    def update(self, prof):
        s = self.cs.settings(prof)
        self.mode, self.own = s["notify"], s["notify_own"]

    def add(self, unit, rel, action, who, own):
        if self.mode != "off" and (self.own or not own):
            self.items.append((unit, rel, action, who))
            self.last = time.time()

    def flush(self):
        if not self.items or time.time() - self.last < 5:
            return
        items, self.items = self.items, []
        if self.mode == "each" and len(items) <= 5:
            for u, rel, action, who in items:
                self.send(f"{rel.rsplit('/', 1)[-1]} {action}",
                          f"{self.cs.remote_label(u.key)}: {rel}" + (f"\nby {who}" if who else ""),
                          u.local / rel)
            return
        units = sorted({self.cs.remote_label(u.key) for u, *_ in items})
        names = ", ".join(rel.rsplit("/", 1)[-1] for _, rel, _, _ in items[:4])
        more = f" and {len(items) - 4} more" if len(items) > 4 else ""
        self.send(f"{len(items)} files changed in {', '.join(units)}", names + more,
                  items[0][0].local / items[0][1])

    def send(self, title, body, path):
        def run():
            try:
                r = subprocess.run(["notify-send", "-a", "Cloud Sync", "-i", "folder-remote",
                                    "--action=open=Open", "--wait", title, body],
                                   capture_output=True, text=True, timeout=3600)
            except (OSError, subprocess.TimeoutExpired):
                return
            if r.stdout.strip() == "open":
                target = path if path.exists() else path.parent
                subprocess.run(["gio", "open", str(target)])
        threading.Thread(target=run, daemon=True).start()


# --- the watcher -----------------------------------------------------------------------------

class Watcher:
    def __init__(self, cs):
        self.cs = cs
        self.jobs = queue.Queue()
        self.pending = {}         # (key, rel) -> time of last local event
        self.moves = {}           # inotify cookie -> (unit, rel, time)
        self.deleted = {}         # (key, rel) -> time; confirmed if still gone later
        self.suppress = {}        # absolute path -> (size, mtime) written by us
        self.feeds = []
        self.feed_state = {}
        cs.cache_dir().mkdir(parents=True, exist_ok=True)
        run_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or cs.cache_dir())
        self.rc = Rclone(run_dir / f"cloudsync-rclone-{os.getpid()}.sock",
                         cs.state_dir() / "logs" / "watch-rclone.log")
        self.reload()

    # setup ------------------------------------------------------------------

    def reload(self):
        prof, units = build_units(self.cs)
        self.prof = prof
        self.settings = self.cs.settings(prof)
        self.units = {u.key: u for u in units}
        self.notifier = Notifier(self.cs, prof)
        self.ino = Inotify()
        for u in units:
            u.load_state()
            self.watch_tree(u, u.local)
        log(f"watching {len(self.ino.paths)} folders in {', '.join(self.units)}")
        self.signature = self.profile_signature()

    def profile_signature(self):
        f = self.cs.state_dir() / "profile-cache.json"
        try:
            return f.stat().st_mtime
        except OSError:
            return None

    def watch_tree(self, u, top):
        for dirpath, dirs, _ in os.walk(top):
            rel = os.path.relpath(dirpath, u.local).replace(os.sep, "/")
            rel = "" if rel == "." else rel
            if rel and (not u.filters.dir_wanted(rel) or
                        any(os.path.exists(os.path.join(dirpath, m)) for m in self.cs.EXCLUDE_IF_PRESENT)):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in (".pixi", ".venv", "node_modules", "__pycache__")]
            try:
                self.ino.add(dirpath)
            except OSError as e:
                log(f"can't watch {dirpath}: {e}")

    def start_feeds(self):
        groups = {}
        for u in self.units.values():
            groups.setdefault((u.rname, u.kind), []).append(u)
        for (rname, kind), units in groups.items():
            conf = rclone_conf(rname)
            try:
                if kind == "drive":
                    feed = DriveFeed(self, rname, units)
                elif kind == "webdav" and conf.get("vendor") in ("nextcloud", "owncloud"):
                    feed = NextcloudFeed(self, rname, units)
                else:
                    self.feed_state[rname] = "timer only (no change feed)"
                    continue
                threading.Thread(target=self.feed_loop, args=(feed,), daemon=True).start()
            except (ValueError, OSError) as e:
                self.feed_state[rname] = f"error: {e}"

    def feed_loop(self, feed):
        backoff = 0
        while True:
            try:
                if not getattr(feed, "ready", False):
                    feed.setup()
                    feed.ready = True
                feed.poll()
                self.feed_state[feed.rname] = "ok"
                backoff = 0
            except QuotaError:
                self.feed_state[feed.rname] = "quota exceeded (own Google client ID needed)"
                backoff = 600
            except Exception as e:  # network down, server errors: try again later
                self.feed_state[feed.rname] = f"error: {e}"[:200]
                backoff = min(max(backoff * 2, 60), 900)
            time.sleep(backoff or self.settings["poll_seconds"])

    # events -------------------------------------------------------------------

    def unit_of(self, path):
        best = None
        for u in self.units.values():
            if path == u.local or u.local in path.parents:
                if not best or len(str(u.local)) > len(str(best.local)):
                    best = u
        return best

    def local_events(self):
        now = time.time()
        for d, mask, cookie, name in self.ino.read():
            if mask & IN_Q_OVERFLOW:
                for u in self.units.values():
                    self.mark_dirty(u, "too many changes at once")
                continue
            if d is None or not name:
                continue
            path = d / name
            u = self.unit_of(path)
            if not u:
                continue
            rel = path.relative_to(u.local).as_posix()
            isdir = bool(mask & IN_ISDIR)
            if isdir:
                if u.in_marked_dir(rel + "/x"):
                    continue
                if mask & (IN_CREATE | IN_MOVED_TO):
                    if u.filters.dir_wanted(rel):
                        self.watch_tree(u, path)
                        if mask & IN_MOVED_TO or any(path.iterdir()):
                            self.mark_dirty(u, f"folder {rel} added")
                elif mask & (IN_DELETE | IN_MOVED_FROM):
                    if u.filters.dir_wanted(rel):
                        self.mark_dirty(u, f"folder {rel} removed or moved")
                continue
            if not u.wanted(rel):
                continue
            if mask & IN_MOVED_FROM:
                self.moves[cookie] = (u, rel, now)
            elif mask & IN_MOVED_TO and cookie in self.moves:
                src = self.moves.pop(cookie)
                if not src[0].state.get(src[1], {}).get("l"):
                    # saved via a temporary file (LibreOffice & co.): a normal write
                    if u.fast(rel):
                        self.pending[(u.key, rel)] = now
                    else:
                        self.mark_dirty(u, f"{rel} needs the full sync (special characters)")
                elif src[0] is u and u.fast(rel) and u.fast(src[1]):
                    self.jobs.put(("move", u, src[1], rel))
                else:
                    self.mark_dirty(u, f"{src[1]} moved")
            elif mask & (IN_CLOSE_WRITE | IN_MOVED_TO):
                if u.fast(rel):
                    self.pending[(u.key, rel)] = now
                else:
                    self.mark_dirty(u, f"{rel} needs the full sync (special characters)")
            elif mask & IN_DELETE:
                if u.state.get(rel, {}).get("l"):
                    self.deleted[(u.key, rel)] = now
        # replaced files (rclone, editors) show up as deleted for a moment
        recent = {}
        for (key, rel), t in self.deleted.items():
            if now - t < 10:
                recent[key] = recent.get(key, 0) + 1
        for (key, rel), t in list(self.deleted.items()):
            if now - t > 2:
                del self.deleted[(key, rel)]
                u = self.units.get(key)
                if not u or (u.local / rel).exists() or not u.state.get(rel, {}).get("l"):
                    continue
                if recent.get(key, 0) > MASS_DELETE or not u.fast(rel):
                    self.mark_dirty(u, f"{rel} deleted")
                else:
                    self.jobs.put(("delete", u, rel))
        # moved out of the watched folders = deleted
        for cookie, (u, rel, t) in list(self.moves.items()):
            if now - t > 2:
                del self.moves[cookie]
                self.mark_dirty(u, f"{rel} moved away")
        for (key, rel), t in list(self.pending.items()):
            u = self.units.get(key)
            st = local_stat(u.local / rel) if u else None
            wait = min(QUIET + (st[0] / 1e6 * QUIET_PER_MB if st else 0), QUIET_MAX)
            if now - t >= wait:
                del self.pending[(key, rel)]
                if key in self.units:
                    self.jobs.put(("local", self.units[key], rel))

    def cloud_event(self, u, rel, deleted=False, who=None, own=False):
        if not u.wanted(rel):
            return
        if not u.fast(rel):
            self.mark_dirty(u, f"{rel} changed in the cloud (special characters)")
            return
        self.jobs.put(("cloud", u, rel, deleted, who, own))

    def mark_dirty(self, u, why):
        if u.dirty_at is None:
            log(f"{u.key}: full sync soon ({why})")
        u.dirty_at = time.time()

    # jobs (worker thread, under the sync lock) ------------------------------------------

    def worker(self):
        while True:
            job = self.jobs.get()
            try:
                with self.cs.sync_lock():
                    job[1].refresh_if_synced()
                    getattr(self, "do_" + job[0])(*job[1:])
            except Exception as e:  # keep watching; the full sync fixes it
                log(f"{job[0]} {job[2] if len(job) > 2 else ''}: {e}")
                self.mark_dirty(job[1], f"error: {e}")

    def suppressed(self, path):
        want = self.suppress.get(str(path))
        return want is not None and same(local_stat(path), want)

    def wrote(self, path):
        self.suppress[str(path)] = local_stat(path)

    def history(self, u, rel, action, side, **extra):
        self.cs.add_history([{"remote": u.key, "path": rel, "action": action, "side": side,
                              "via": "watch", **extra}])

    def remote_stat(self, u, rel):
        return self.rc.stat(u.remote, rel)

    def do_local(self, u, rel):
        p = u.local / rel
        L = local_stat(p)
        if L is None or self.suppressed(p):
            return
        known = u.state.get(rel, {})
        if same(L, known.get("l")):
            return
        R = self.remote_stat(u, rel)
        if R is None or same(R, known.get("r")):
            self.rc.copy(u.local_fs, rel, u.remote, rel)
            u.remember(rel, L, self.remote_stat(u, rel))
            self.history(u, rel, "new" if R is None else "changed", "local")
            log(f"{u.key}: uploaded {rel}")
        elif same(R, L):
            u.remember(rel, L, R)
        else:
            self.conflict(u, rel, L, R)

    def do_cloud(self, u, rel, deleted, who, own):
        p = u.local / rel
        known = u.state.get(rel, {})
        R = self.remote_stat(u, rel)
        L = local_stat(p)
        if R is None:
            if L is None:
                u.forget(rel)
            elif same(L, known.get("l")):
                target = self.backup(u, rel, "deleted in the cloud")
                u.forget(rel)
                self.history(u, rel, "deleted", "cloud", who=who, backup=str(target))
                self.notifier.add(u, rel, "deleted", who, own)
                log(f"{u.key}: deleted {rel} (cloud), local copy in the backup")
            elif not known.get("r"):
                pass  # new local file, not uploaded yet: the local event does that
            else:
                self.mark_dirty(u, f"{rel} deleted in the cloud but changed here")
            return
        if same(R, known.get("r")):
            return  # already known, e.g. our own upload
        if L is None or same(L, known.get("l")):
            extra = {}
            if L is not None:
                extra["backup"] = str(self.backup(u, rel))
            self.download(u, rel)
            u.remember(rel, local_stat(p), R)
            action = "changed" if L is not None else "new"
            self.history(u, rel, action, "cloud", who=who, **extra)
            self.notifier.add(u, rel, action, who, own)
            log(f"{u.key}: downloaded {rel}" + (f" ({who})" if who else ""))
        elif same(L, R):
            u.remember(rel, L, R)
        else:
            self.conflict(u, rel, L, R, who, own)

    def do_delete(self, u, rel):
        """A single file deleted here: delete it in the cloud (its trash), if
        the cloud still has the version we know. Otherwise leave it to bisync."""
        if (u.local / rel).exists():
            return
        known = u.state.get(rel, {})
        R = self.remote_stat(u, rel)
        if R is None:
            u.forget(rel)
        elif same(R, known.get("r")):
            self.rc.call("operations/deletefile", fs=u.remote, remote=rel)
            u.forget(rel)
            self.history(u, rel, "deleted", "local")
            log(f"{u.key}: deleted {rel} in the cloud")
        else:
            self.mark_dirty(u, f"{rel} deleted here but changed in the cloud")

    def do_move(self, u, old, new):
        known = u.state.get(old, {})
        R = self.remote_stat(u, old)
        if R is not None and same(R, known.get("r")) and self.remote_stat(u, new) is None:
            self.rc.move(u.remote, old, u.remote, new)
            u.forget(old)
            u.remember(new, local_stat(u.local / new), self.remote_stat(u, new))
            self.history(u, new, "moved", "local", old=old)
            log(f"{u.key}: moved {old} -> {new}")
        else:
            self.mark_dirty(u, f"{old} moved")

    def do_full(self, u):
        log(f"{u.key}: full sync")
        rc, _ = u.run_bisync()
        if rc == 0:
            u.after_bisync()
        else:
            log(f"{u.key}: full sync failed (exit {rc}), retrying later")
            u.dirty_at = time.time() + 600

    def download(self, u, rel):
        self.rc.copy(u.remote, rel, u.local_fs, rel)
        self.wrote(u.local / rel)

    def backup(self, u, rel, note=""):
        """Move the local file into the backup, keeping the folder structure."""
        d, _, name = rel.rpartition("/")
        target = f"{d}/{self.cs.backup_name(name, note)}" if d else self.cs.backup_name(name, note)
        self.rc.move(u.local_fs, rel, u.backup_fs, target)
        return self.cs.backup_root() / u.key / target

    def conflict(self, u, rel, L, R, who=None, own=False):
        d, _, name = rel.rpartition("/")
        if R[1] > L[1]:   # cloud is newer
            target = self.backup(u, rel, "conflict, local version")
            self.download(u, rel)
            loser = "local"
        else:
            bname = self.cs.backup_name(name, "conflict, cloud version")
            dst = f"{d}/{bname}" if d else bname
            self.rc.copy(u.remote, rel, u.backup_fs, dst)
            target = self.cs.backup_root() / u.key / dst
            self.rc.copy(u.local_fs, rel, u.remote, rel)
            loser = "cloud"
        p = u.local / rel
        u.remember(rel, local_stat(p), self.remote_stat(u, rel))
        self.history(u, rel, "conflict", loser, who=who, backup=str(target))
        self.notifier.add(u, rel, "changed on both sides", who, own)
        log(f"{u.key}: conflict {rel}, {loser} version in the backup")

    # main loop ------------------------------------------------------------------------------

    def status(self):
        self.cs.write_status(watch={"pid": os.getpid(), "seen": time.time(),
                                    "folders": len(self.ino.paths), "feeds": self.feed_state})

    def run(self):
        threading.Thread(target=self.worker, daemon=True).start()
        self.start_feeds()
        last_status = last_check = 0
        while True:
            select.select([self.ino.fd], [], [], 1.0)
            self.local_events()
            now = time.time()
            for u in self.units.values():
                if u.dirty_at and now - u.dirty_at >= DIRTY_DELAY:
                    u.dirty_at = None
                    self.jobs.put(("full", u))
            self.notifier.flush()
            if now - last_check > 30:
                last_check = now
                if self.profile_signature() != self.signature:
                    log("selection changed, restarting")  # systemd starts us again
                    raise SystemExit(3)
            if now - last_status > 60:
                last_status = now
                self.status()


def run(cs):
    w = Watcher(cs)
    try:
        w.run()
    finally:
        w.rc.close()
