"""Test setup: a temporary sync root and a local folder as "cloud" remote.

No real cloud is touched. RCLONE_CONFIG points to a temporary config with one
remote "testcloud" (an rclone alias for a local folder), and the profile is kept
in memory instead of on gdrive.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cloudsync-test-"))
        self.root = self.tmp / "root"          # local sync root
        self.cloud = self.tmp / "cloud"        # what the remote shows
        self.root.mkdir()
        self.cloud.mkdir()
        conf = self.tmp / "rclone.conf"
        conf.write_text(f"[testcloud]\ntype = alias\nremote = {self.cloud}\n")
        self._env = {k: os.environ.get(k) for k in
                     ("RCLONE_CONFIG", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")}
        os.environ["RCLONE_CONFIG"] = str(conf)
        for k, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                       ("XDG_DATA_HOME", "data")):
            os.environ[k] = str(self.tmp / sub)
        import cloudsync as cs
        self.cs = cs
        self.prof = {"version": 2, "remotes": {}, "pairs": {}, "settings": {}}
        self._orig = (cs.local_root, cs.load_profile, cs.save_profile)
        cs.local_root = lambda: self.root
        cs.load_profile = lambda: (self.prof, True)
        cs.save_profile = lambda prof: None
        (self.root / ".cloudsync").mkdir()
        (self.root / ".cloudsync" / "machine.txt").write_text("test\n")

    def tearDown(self):
        cs = self.cs
        cs.local_root, cs.load_profile, cs.save_profile = self._orig
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ------------------------------------------------------------------

    def write(self, path, text, age=0):
        """Write a file; age = seconds in the past for its modification time."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if age:
            t = time.time() - age
            os.utime(path, (t, t))

    def tree(self, top):
        """{relative path: content} of all files below top."""
        top = Path(top)
        return {p.relative_to(top).as_posix(): p.read_text()
                for p in sorted(top.rglob("*")) if p.is_file()}

    def sync(self, rules=None):
        rules = rules or {"": True}
        rc, _ = self.cs.sync_remote("testcloud", "alias", rules)
        self.assertEqual(rc, 0, "bisync failed, see " + str(self.root / ".cloudsync/logs"))

    def local(self):
        return self.root / "testcloud"
