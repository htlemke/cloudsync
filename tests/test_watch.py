"""The watcher's fast path (cloudsync watch), against a local folder as cloud."""

import os
import time
import unittest

from harness import SyncTestCase  # noqa: I001 (puts the repo on sys.path)
import cloudsync_watch as cw


class Watch(SyncTestCase):
    def setUp(self):
        super().setUp()
        cw.QUIET = 0.3
        for i in range(8):
            self.write(self.cloud / f"other{i}.txt", str(i))
        self.write(self.cloud / "d/report.txt", "v1", age=600)
        self.write(self.cloud / "skip/x.txt", "not synced")
        self.prof["remotes"]["testcloud"] = {"enabled": True,
                                             "rules": {"": True, "skip": False}}
        self.sync(self.cs.get_rules(self.prof["remotes"]["testcloud"]))
        self.w = cw.Watcher(self.cs)
        self.u = self.w.units["testcloud"]

    def tearDown(self):
        self.assertCloudClean()
        self.w.rc.close()
        os.close(self.w.ino.fd)
        super().tearDown()

    def drain(self, seconds=3):
        """Run the watcher's loop steps until nothing is pending."""
        end = time.time() + seconds
        while time.time() < end:
            self.w.local_events()
            while not self.w.jobs.empty():
                job = self.w.jobs.get()
                with self.cs.sync_lock():
                    job[1].refresh_if_synced()
                    getattr(self.w, "do_" + job[0])(*job[1:])
            time.sleep(0.1)

    def history(self):
        return [(e["path"], e["action"], e["side"]) for e in self.cs.read_history()
                if e.get("via") == "watch"]

    def backups(self):
        return self.tree(self.root / ".backup" / "testcloud")

    def test_local_save_is_uploaded(self):
        self.write(self.local() / "d/report.txt", "local v2")
        self.write(self.local() / "d/new.txt", "brand new")
        self.drain()
        self.assertEqual(self.tree(self.cloud)["d/report.txt"], "local v2")
        self.assertEqual(self.tree(self.cloud)["d/new.txt"], "brand new")
        self.assertIn(("d/new.txt", "new", "local"), self.history())
        self.assertEqual(self.backups(), {})

    def test_excluded_folder_is_not_uploaded(self):
        self.write(self.local() / "skip/y.txt", "local only")
        self.drain()
        self.assertNotIn("skip/y.txt", self.tree(self.cloud))

    def test_cloud_change_is_downloaded_old_version_kept(self):
        self.write(self.cloud / "d/report.txt", "cloud v2")
        self.w.cloud_event(self.u, "d/report.txt", who="Jane")
        self.drain()
        self.assertEqual(self.tree(self.local())["d/report.txt"], "cloud v2")
        self.assertEqual(list(self.backups().values()), ["v1"])
        self.assertIn(("d/report.txt", "changed", "cloud"), self.history())
        # our own download must not come back as a local change
        self.drain(1.5)
        self.assertNotIn(("d/report.txt", "changed", "local"), self.history())

    def test_conflict_cloud_newer(self):
        self.write(self.local() / "d/report.txt", "local edit", age=120)
        self.write(self.cloud / "d/report.txt", "cloud edit")
        self.w.cloud_event(self.u, "d/report.txt")
        self.drain()
        self.assertEqual(self.tree(self.local())["d/report.txt"], "cloud edit")
        [(name, text)] = self.backups().items()
        self.assertTrue(name.endswith("conflict, local version).txt"), name)
        self.assertEqual(text, "local edit")

    def test_conflict_local_newer(self):
        self.write(self.cloud / "d/report.txt", "cloud edit", age=120)
        self.write(self.local() / "d/report.txt", "local edit")
        self.drain()
        self.assertEqual(self.tree(self.cloud)["d/report.txt"], "local edit")
        [(name, text)] = self.backups().items()
        self.assertTrue(name.endswith("conflict, cloud version).txt"), name)
        self.assertEqual(text, "cloud edit")
        self.assertNotIn("cs-", "".join(self.tree(self.cloud)))

    def test_deleted_in_cloud_goes_to_backup(self):
        (self.cloud / "d/report.txt").unlink()
        self.w.cloud_event(self.u, "d/report.txt", deleted=True)
        self.drain()
        self.assertNotIn("d/report.txt", self.tree(self.local()))
        self.assertEqual(list(self.backups().values()), ["v1"])

    def test_save_via_temporary_file(self):
        tmp = self.local() / "d/lu1234.tmp"
        self.write(tmp, "saved by an office program")
        os.rename(tmp, self.local() / "d/report.txt")
        self.drain()
        self.assertEqual(self.tree(self.cloud)["d/report.txt"], "saved by an office program")
        self.assertIsNone(self.u.dirty_at)

    def test_rename_is_a_move_in_the_cloud(self):
        os.rename(self.local() / "d/report.txt", self.local() / "d/renamed.txt")
        self.drain()
        cloud = self.tree(self.cloud)
        self.assertEqual(cloud.get("d/renamed.txt"), "v1")
        self.assertNotIn("d/report.txt", cloud)

    def test_local_delete_is_deleted_in_the_cloud(self):
        (self.local() / "d/report.txt").unlink()
        self.drain(4)  # a deletion counts once the file is still gone 2 s later
        self.assertNotIn("d/report.txt", self.tree(self.cloud))
        self.assertIn(("d/report.txt", "deleted", "local"), self.history())
        self.assertIsNone(self.u.dirty_at)

    def test_file_made_by_the_watcher_stays_deleted(self):
        self.write(self.local() / "d/temp.txt", "short-lived")
        self.drain()
        self.assertIn("d/temp.txt", self.tree(self.cloud))
        (self.local() / "d/temp.txt").unlink()
        self.drain(4)
        self.w.do_full(self.u)      # bisync never knew the file: must not bring it back
        self.assertNotIn("d/temp.txt", self.tree(self.local()))
        self.assertNotIn("d/temp.txt", self.tree(self.cloud))

    def test_changed_in_cloud_then_deleted_here_goes_to_full_sync(self):
        self.write(self.cloud / "d/report.txt", "changed in the cloud")
        (self.local() / "d/report.txt").unlink()
        self.drain(4)
        self.assertIn("d/report.txt", self.tree(self.cloud))
        self.assertIsNotNone(self.u.dirty_at)

    def test_many_deletions_go_to_the_full_sync(self):
        for i in range(8):
            (self.local() / f"other{i}.txt").unlink()
        cw.MASS_DELETE, old = 5, cw.MASS_DELETE
        try:
            self.drain(4)
        finally:
            cw.MASS_DELETE = old
        self.assertEqual(sum(f.startswith("other") for f in self.tree(self.cloud)), 8)
        self.assertIsNotNone(self.u.dirty_at)

    def test_virtualenv_is_ignored(self):
        self.write(self.local() / "env1/pyvenv.cfg", "home = /usr/bin")
        self.drain(1)
        self.write(self.local() / "env1/lib/site.py", "x")
        self.drain()
        self.assertFalse(any("env1" in p for p in self.tree(self.cloud)))

    def test_new_selection_is_synced_at_start(self):
        self.assertIsNone(self.u.dirty_at)          # synced in setUp: up to date
        self.prof["remotes"]["testcloud"]["rules"]["skip"] = True
        w2 = cw.Watcher(self.cs)
        try:
            self.assertIsNotNone(w2.units["testcloud"].dirty_at)
        finally:
            w2.rc.close()
            os.close(w2.ino.fd)

    def test_filters(self):
        f = cw.Filters("- **/.DS_Store\n- **/.venv/**\n+ /projects/**\n+ /CLAUDE.md\n- **")
        self.assertTrue(f.included("projects/a/b.jsonl"))
        self.assertTrue(f.included("CLAUDE.md"))
        self.assertFalse(f.included("cache/x"))
        self.assertFalse(f.included("projects/.DS_Store"))
        self.assertFalse(f.included("projects/x/.venv/lib/a.py"))
        self.assertTrue(f.dir_wanted("projects"))
        self.assertFalse(f.dir_wanted("cache"))
        g = cw.Filters("- /a/b/c/**\n+ /a/b/**\n- /a/**\n- **")
        self.assertTrue(g.dir_wanted("a"))      # a/b is synced inside an excluded a
        self.assertTrue(g.included("a/b/x"))
        self.assertFalse(g.included("a/b/c/x"))
        self.assertFalse(g.included("a/x"))


if __name__ == "__main__":
    unittest.main()
