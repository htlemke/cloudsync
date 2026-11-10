"""Conflicts, backups and history of `cloudsync sync` (rclone bisync)."""

import datetime
import unittest

from harness import SyncTestCase


class ConflictsAndBackups(SyncTestCase):
    def start(self):
        """Cloud with a few files, synced once."""
        for i in range(8):  # bisync refuses to sync when *all* files changed
            self.write(self.cloud / f"other{i}.txt", str(i))
        self.write(self.cloud / "d/report.txt", "v1")
        self.write(self.cloud / "gone.txt", "keep me")
        self.sync()

    def tearDown(self):
        if (self.cloud).exists():
            self.assertCloudClean()
        super().tearDown()

    def backups(self):
        return self.tree(self.root / ".backup" / "testcloud")

    def test_cloud_newer_wins_local_version_backed_up(self):
        self.start()
        self.write(self.local() / "d/report.txt", "local edit", age=120)
        self.write(self.cloud / "d/report.txt", "cloud edit")
        self.sync()
        self.assertEqual(self.tree(self.local())["d/report.txt"], "cloud edit")
        self.assertEqual(self.tree(self.cloud)["d/report.txt"], "cloud edit")
        self.assertNotIn("cs-", "".join(self.tree(self.cloud)), "loser copy left in the cloud")
        self.assertNotIn("cs-", "".join(self.tree(self.local())))
        [(name, text)] = self.backups().items()
        self.assertTrue(name.startswith("d/report (") and name.endswith("conflict, local version).txt"), name)
        self.assertEqual(text, "local edit")

    def test_local_newer_wins_cloud_version_backed_up_locally(self):
        self.start()
        self.write(self.cloud / "d/report.txt", "cloud edit", age=120)
        self.write(self.local() / "d/report.txt", "local edit")
        self.sync()
        self.assertEqual(self.tree(self.cloud)["d/report.txt"], "local edit")
        self.assertNotIn("cs-", "".join(self.tree(self.cloud)))
        [(name, text)] = self.backups().items()
        self.assertTrue(name.endswith("conflict, cloud version).txt"), name)
        self.assertEqual(text, "cloud edit")

    def test_deleted_in_cloud_local_copy_backed_up(self):
        self.start()
        (self.cloud / "gone.txt").unlink()
        self.sync()
        self.assertNotIn("gone.txt", self.tree(self.local()))
        [(name, text)] = self.backups().items()
        self.assertTrue(name.startswith("gone (") and name.endswith(").txt"), name)
        self.assertEqual(text, "keep me")

    def test_updated_in_cloud_old_local_version_backed_up(self):
        self.start()
        self.write(self.cloud / "d/report.txt", "v2")
        self.sync()
        self.assertEqual(self.tree(self.local())["d/report.txt"], "v2")
        self.assertEqual(list(self.backups().values()), ["v1"])

    def test_local_changes_need_no_backup(self):
        self.start()
        self.write(self.local() / "d/report.txt", "local v2")
        self.write(self.local() / "new.txt", "new")
        self.sync()
        self.assertEqual(self.tree(self.cloud)["d/report.txt"], "local v2")
        self.assertEqual(self.tree(self.cloud)["new.txt"], "new")
        self.assertEqual(self.backups(), {})

    def test_history(self):
        self.start()
        self.write(self.cloud / "d/report.txt", "v2")
        self.write(self.local() / "new.txt", "new")
        self.sync()
        got = {(e["path"], e["action"], e["side"]) for e in self.cs.read_history()}
        self.assertIn(("d/report.txt", "changed", "cloud"), got)
        self.assertIn(("new.txt", "new", "local"), got)

    def test_virtualenvs_are_not_synced(self):
        self.start()
        self.write(self.local() / "proj/.venv-x/pyvenv.cfg", "home = /usr/bin")
        self.write(self.local() / "proj/.venv-x/lib/m.py", "x")
        self.write(self.local() / "proj/main.py", "code")
        self.sync()
        cloud = self.tree(self.cloud)
        self.assertIn("proj/main.py", cloud)
        self.assertFalse(any(".venv-x" in p for p in cloud), cloud)

    def test_added_rules(self):
        added = self.cs.added_rules
        self.assertIsNone(added({"a": True}, {"a": True}))
        self.assertIsNone(added({}, {"": True}))            # all new: the resync does it
        self.assertEqual(added({"a": True}, {"a": True, "b": True, "b/x": False}),
                         {"": False, "b": True, "b/x": False})
        # a folder turned on inside one that was partly synced
        self.assertEqual(added({"a": True, "a/b": False}, {"a": True}),
                         {"": False, "a/b": True})
        self.assertIsNone(added({"a": True, "b": True}, {"a": True}))  # only removed

    def test_added_folder_is_copied_before_the_resync(self):
        self.start()
        self.write(self.cloud / "skip/x.txt", "cloud only")
        self.write(self.cloud / "skip/old.txt", "cloud, older", age=600)
        self.sync({"": True, "skip": False})
        self.write(self.local() / "skip/old.txt", "local, newer")
        calls = []
        orig = self.cs.bisync
        self.cs.bisync = lambda *a, **k: (calls.append(self.tree(self.local())), orig(*a, **k))[1]
        try:
            self.sync({"": True})
        finally:
            self.cs.bisync = orig
        self.assertEqual(calls[0].get("skip/x.txt"), "cloud only")   # before the bisync ran
        self.assertEqual(self.tree(self.cloud)["skip/old.txt"], "local, newer")
        self.assertEqual(self.tree(self.local())["skip/old.txt"], "local, newer")

    def test_changed_selection_syncs_first(self):
        self.start()
        self.prof["remotes"] = {"testcloud": {"enabled": True, "rules": {"": True}}}
        self.prof["pairs"] = {"p": {"local": str(self.tmp / "pairdir"),
                                    "remote": "testcloud:pair"}}
        self.write(self.cloud / "pair/a.txt", "a")
        order = []
        orig = self.cs.sync_remote, self.cs.sync_pair
        self.cs.sync_remote = lambda n, *a, **k: (order.append(n), orig[0](n, *a, **k))[1]
        self.cs.sync_pair = lambda n, *a, **k: (order.append(n), orig[1](n, *a, **k))[1]
        try:
            import argparse
            args = argparse.Namespace(remote=None, resync=False, dry_run=False, notify=False)
            self.assertEqual(self.cs.run_sync(args), 0)
        finally:
            self.cs.sync_remote, self.cs.sync_pair = orig
        self.assertEqual(order, ["p", "testcloud"])   # the pair never synced: it goes first

    def test_index_keeps_shortcut_files_in_their_own_remote(self):
        blocks = {"gdrive": ["#source\tgdrive", "gdrive\t1\tR\tswissfel/Bernina-group",
                             "gdrive\t0\tF1\tswissfel/Bernina-group/a.pdf",
                             "gdrive\t0\tF2\tmine.pdf", "gdrive\t0\tF3\tmine2.pdf"],
                  "bernina": ["#source\tbernina", "bernina\t0\tF1\ta.pdf"]}
        self.cs.dedupe_shortcuts(blocks)
        self.assertEqual(blocks["bernina"], ["#source\tbernina", "bernina\t0\tF1\ta.pdf"])
        self.assertNotIn("gdrive\t0\tF1\tswissfel/Bernina-group/a.pdf", blocks["gdrive"])
        self.assertIn("gdrive\t0\tF2\tmine.pdf", blocks["gdrive"])

    def test_prune_backups(self):
        old = (datetime.date.today() - datetime.timedelta(days=100)).isoformat()
        b = self.root / ".backup" / "testcloud"
        self.write(b / f"sub/a ({old} 10.00).txt", "old")
        self.write(b / f"b ({datetime.date.today().isoformat()} 10.00).txt", "new")
        self.cs.prune_backups(90)
        self.assertEqual(list(self.tree(b)), [f"b ({datetime.date.today().isoformat()} 10.00).txt"])
        self.assertFalse((b / "sub").exists())


if __name__ == "__main__":
    unittest.main()
