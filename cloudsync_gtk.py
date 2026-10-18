"""GNOME (GTK 4 / libadwaita) window for cloudsync. Linux only; Windows uses Tk."""

import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

APP_ID = "io.github.htlemke.CloudSync"


def bg(fn, done, error):
    """Run fn in a thread, then done(result) or error(exc) in the GTK thread."""
    def run():
        try:
            res = fn()
        except Exception as e:
            GLib.idle_add(error, e)
        else:
            GLib.idle_add(done, res)
    threading.Thread(target=run, daemon=True).start()


class Folder(GObject.Object):
    def __init__(self, remote, path, name):
        super().__init__()
        self.remote, self.path, self.name = remote, path, name
        self.children = Gio.ListStore(item_type=Folder)
        self.loaded = self.loading = False
        self.size = ""


class Window(Adw.ApplicationWindow):
    def __init__(self, app, cs):
        super().__init__(application=app, title=f"Cloud Sync – {cs.machine_name()}")
        self.cs = cs
        self.sel = cs.Selection()
        self.set_default_size(820, 640)
        self.bound = {}  # remote -> {list item: (folder, check, size label)}
        self.updating = False
        self.switches = {}
        self.models = {}  # remote -> (TreeListModel, ListView)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self.save_btn = Gtk.Button(label="Save", css_classes=["suggested-action"])
        self.save_btn.connect("clicked", lambda *_: self.save(False))
        header.pack_end(self.save_btn)
        self.sync_btn = Gtk.Button(icon_name="emblem-synchronizing-symbolic",
                                   tooltip_text="Save and sync now")
        self.sync_btn.connect("clicked", lambda *_: self.save(True))
        header.pack_end(self.sync_btn)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner)
        view.add_top_bar(header)
        view.set_content(self.stack)

        free = cs.shutil.disk_usage(cs.local_root()).free / 1e9
        self.status = Gtk.Label(xalign=0, css_classes=["dim-label"], margin_start=12,
                                margin_end=12, margin_top=6, margin_bottom=6,
                                label=f"Free on {cs.local_root()}: {free:.1f} GB")
        view.add_bottom_bar(self.status)

        for name in self.sel.remotes:
            self.stack.add_titled_with_icon(self.page(name), name, name,
                                            "folder-remote-symbolic")
        self.stack.add_titled_with_icon(self.history_page(), "history", "History",
                                        "document-open-recent-symbolic")
        self.stack.add_titled_with_icon(self.settings_page(), "settings", "Settings",
                                        "preferences-system-symbolic")
        if not self.sel.online:
            self.toast("gdrive unreachable – showing the cached selection")

    # --- one page per remote ------------------------------------------------

    def page(self, name):
        self.bound[name] = {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        prefs = Adw.PreferencesGroup(margin_start=12, margin_end=12, margin_top=12)
        row = Adw.SwitchRow(title="Sync this remote",
                            subtitle=f"to {self.cs.local_root() / name}",
                            active=self.sel.enabled[name])
        row.connect("notify::active",
                    lambda r, _p: self.sel.enabled.__setitem__(name, r.get_active()))
        self.switches[name] = row
        prefs.add(row)
        box.append(prefs)

        top = Gio.ListStore(item_type=Folder)
        top.append(Folder(name, "", "Whole remote"))
        model = Gtk.TreeListModel.new(top, False, False, lambda f: f.children)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.setup_row)
        factory.connect("bind", self.bind_row, name)
        factory.connect("unbind", lambda _f, li: self.bound[name].pop(li, None))
        lv = Gtk.ListView(model=Gtk.SingleSelection(model=model, autoselect=False),
                          factory=factory, css_classes=["navigation-sidebar"])
        self.models[name] = (model, lv)
        frame = Gtk.Frame(child=Gtk.ScrolledWindow(child=lv, vexpand=True),
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        box.append(frame)
        model.get_row(0).set_expanded(True)
        return box

    def setup_row(self, _factory, li):
        expander = Gtk.TreeExpander()
        h = Gtk.Box(spacing=8)
        check = Gtk.CheckButton()
        label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        size = Gtk.Label(css_classes=["dim-label", "numeric"])
        size_btn = Gtk.Button(icon_name="drive-harddisk-symbolic", css_classes=["flat"],
                              tooltip_text="Measure folder size")
        for w in (check, label, size, size_btn):
            h.append(w)
        expander.set_child(h)
        li.set_child(expander)
        li.widgets = (expander, check, label, size, size_btn)
        check.connect("toggled", self.on_toggled, li)
        size_btn.connect("clicked", self.on_size, li)

    def bind_row(self, _factory, li, name):
        expander, check, label, size, _ = li.widgets
        row = li.get_item()
        folder = row.get_item()
        li.folder, li.row = folder, row
        expander.set_list_row(row)
        label.set_label(folder.name)
        label.set_css_classes(["heading"] if folder.path == "" else [])
        size.set_label(folder.size)
        self.bound[name][li] = folder
        self.show_state(li)
        if getattr(li, "handler", None):
            li.handler_row.disconnect(li.handler)
        li.handler_row = row
        li.handler = row.connect("notify::expanded", lambda r, _p: self.on_expand(r))
        if row.get_expanded():
            self.on_expand(row)

    def show_state(self, li):
        _, check, *_ = li.widgets
        state = self.cs.tri_state(self.sel.rules[li.folder.remote], li.folder.path)
        self.updating = True
        check.set_inconsistent(state == "partial")
        check.set_active(state == "on")
        self.updating = False

    def on_toggled(self, _check, li):
        if self.updating:
            return
        f = li.folder
        self.sel.toggle(f.remote, f.path)
        self.switches[f.remote].set_active(self.sel.enabled[f.remote])
        for other in self.bound[f.remote]:
            self.show_state(other)

    def on_expand(self, row):
        f = row.get_item()
        if not row.get_expanded() or f.loaded or f.loading:
            return
        f.loading = True
        self.busy(+1)

        def done(dirs):
            for d in dirs:
                f.children.append(Folder(f.remote, f"{f.path}/{d}" if f.path else d, d))
            f.loaded, f.loading = True, False
            self.busy(-1)

        def error(e):
            f.loading = False
            self.busy(-1)
            self.toast(f"Listing failed: {e}")
        bg(lambda: self.cs.list_dirs(f.remote, f.path), done, error)

    def on_size(self, _btn, li):
        f = li.folder
        self.busy(+1)

        def done(b):
            f.size = f"{b / 1e9:.2f} GB"
            if getattr(li, "folder", None) is f:
                li.widgets[3].set_label(f.size)
            self.busy(-1)
        bg(lambda: self.cs.folder_size(f.remote, f.path), done,
           lambda e: (self.busy(-1), self.toast(f"Size failed: {e}")))

    def reveal(self, remote, path):
        """Show remote's page with the tree opened down to path."""
        if remote not in self.models:
            self.toast(f"No rclone remote {remote!r}")
            return
        self.stack.set_visible_child_name(remote)
        model, lv = self.models[remote]
        parts = [p for p in path.split("/") if p]

        def step(row, i):
            if i == len(parts):
                pos = row.get_position()
                lv.scroll_to(pos, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
                self.toast(f"Tick “{row.get_item().name}” to sync it, then Save")
                return
            f = row.get_item()
            row.set_expanded(True)
            self.on_expand(row)  # rows off screen have no expand handler yet

            def wait():
                if f.loading:
                    return True  # still listing, check again
                if not f.loaded:
                    return False  # listing failed, already shown as a toast
                for k in range(f.children.get_n_items()):
                    if f.children.get_item(k).name == parts[i]:
                        step(row.get_child_row(k), i + 1)
                        return False
                self.toast(f"Not found: {remote}:{path}")
                return False
            GLib.timeout_add(100, wait)
        step(model.get_row(0), 0)

    # --- save / sync ----------------------------------------------------------

    def save(self, then_sync):
        self.read_settings()
        try:
            gone = self.sel.save()
        except RuntimeError as e:
            self.toast(str(e))
            return
        self.toast("Selection saved to gdrive")
        listing = [f"{n}: {p}" for n, ps in gone.items() for p in ps]
        if not listing:
            if then_sync:
                self.sync()
            return
        body = "\n".join(listing[:15]) + (f"\n… and {len(listing) - 15} more"
                                          if len(listing) > 15 else "")
        dlg = Adw.AlertDialog(heading="Delete local copies?",
                              body="These are no longer synced. The cloud copies stay.\n\n" + body)
        dlg.add_response("keep", "Keep")
        dlg.add_response("delete", "Delete local copies")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")

        def answered(_d, resp):
            if resp == "delete":
                for n, ps in gone.items():
                    self.cs.remove_local(self.cs.local_root() / n, ps)
            if then_sync:
                self.sync()
        dlg.connect("response", answered)
        dlg.present(self)

    def sync(self):
        self.sync_btn.set_sensitive(False)
        self.busy(+1)

        def done(res):
            self.sync_btn.set_sensitive(True)
            self.busy(-1)
            self.toast("Sync: " + (", ".join(f"{n} {'ok' if rc == 0 else 'FAILED'}"
                                             for n, rc in res) or "nothing selected"))
        bg(self.sel.sync_all, done,
           lambda e: (self.sync_btn.set_sensitive(True), self.busy(-1),
                      self.toast(f"Sync failed: {e}")))

    # --- history ----------------------------------------------------------------

    ACTION_ICONS = {"new": "list-add-symbolic", "changed": "document-edit-symbolic",
                    "deleted": "user-trash-symbolic", "moved": "go-next-symbolic",
                    "conflict": "dialog-warning-symbolic"}

    def history_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=12)
        self.history_filter = Gtk.SearchEntry(placeholder_text="Filter by file or folder",
                                              hexpand=True)
        self.history_filter.connect("search-changed", lambda *_: self.fill_history())
        self.history_cloud = Gtk.CheckButton(label="Only changes from the cloud")
        self.history_cloud.connect("toggled", lambda *_: self.fill_history())
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload")
        refresh.connect("clicked", lambda *_: self.fill_history())
        for w in (self.history_filter, self.history_cloud, refresh):
            bar.append(w)
        box.append(bar)
        self.history_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.history_list.set_placeholder(Gtk.Label(label="No changes recorded yet",
                                                    css_classes=["dim-label"], margin_top=24))
        scroll = Gtk.ScrolledWindow(child=self.history_list, vexpand=True,
                                    margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        box.append(scroll)
        self.fill_history()
        return box

    def fill_history(self):
        self.history_list.remove_all()
        text = self.history_filter.get_text().lower()
        cloud_only = self.history_cloud.get_active()
        shown = 0
        for e in self.cs.read_history(3000):
            if text and text not in e.get("path", "").lower():
                continue
            if cloud_only and e.get("side") != "cloud" and e.get("action") != "conflict":
                continue
            self.history_list.append(self.history_row(e))
            shown += 1
            if shown >= 300:
                break

    def history_row(self, e):
        when = GLib.DateTime.new_from_unix_local(int(e.get("t", 0))).format("%Y-%m-%d %H:%M")
        action, side = e.get("action", ""), e.get("side", "")
        if action == "conflict":
            what = f"conflict, {side} version saved in the backup"
        else:
            what = f"{action} {'in the cloud' if side == 'cloud' else 'here'}"
        if e.get("who"):
            what += f" by {e['who']}"
        if e.get("old"):
            what += f" (was {e['old']})"
        row = Adw.ActionRow(title=GLib.markup_escape_text(e.get("path", "")),
                            subtitle=GLib.markup_escape_text(
                                f"{when} · {self.cs.remote_label(e.get('remote', ''))} · {what}"))
        row.add_prefix(Gtk.Image(icon_name=self.ACTION_ICONS.get(action, "emblem-default-symbolic")))
        key = e.get("remote", "")
        local = self.local_of(key)
        if local:
            path = local / e.get("path", "")
            btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Show the folder",
                             valign=Gtk.Align.CENTER, css_classes=["flat"])
            btn.connect("clicked", lambda *_: self.open_path(path if path.exists() else path.parent))
            row.add_suffix(btn)
        if e.get("backup"):
            b = Gtk.Button(icon_name="document-revert-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text="Show the saved older version", css_classes=["flat"])
            b.connect("clicked", lambda *_: self.open_path(Path(e["backup"]).parent))
            row.add_suffix(b)
        return row

    def local_of(self, key):
        if key.startswith("pair-"):
            pair = self.sel.prof.get("pairs", {}).get(key[5:])
            return Path(os.path.expanduser(pair["local"])) if pair else None
        return self.cs.local_root() / key

    def open_path(self, path):
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), None)

    # --- settings ----------------------------------------------------------------

    def settings_page(self):
        s = self.cs.settings(self.sel.prof)
        page = Adw.PreferencesPage()
        notif = Adw.PreferencesGroup(title="Notifications",
                                     description="For changes that come from the cloud")
        modes = ["off", "summary", "each"]
        self.notify_row = Adw.ComboRow(title="Notify",
                                       model=Gtk.StringList.new(["Off", "One summary", "Each file"]),
                                       selected=modes.index(s["notify"]))
        self.notify_own = Adw.SwitchRow(title="Also my own changes",
                                        subtitle="e.g. made on another computer", active=s["notify_own"])
        notif.add(self.notify_row)
        notif.add(self.notify_own)
        page.add(notif)
        backup = Adw.PreferencesGroup(
            title="Backup",
            description="Versions replaced or deleted by a change from the cloud, and the "
                        "losing side of a conflict, are kept here (local only)")
        self.backup_days = Adw.SpinRow.new_with_range(1, 3650, 1)
        self.backup_days.set_title("Keep for days")
        self.backup_days.set_value(s["backup_days"])
        backup.add(self.backup_days)
        open_row = Adw.ActionRow(title="Backup folder", subtitle=str(self.cs.backup_root()),
                                 activatable=True)
        open_row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        open_row.connect("activated", lambda *_: (self.cs.backup_root().mkdir(parents=True, exist_ok=True),
                                                  self.open_path(self.cs.backup_root())))
        backup.add(open_row)
        page.add(backup)
        watch = Adw.PreferencesGroup(title="Watching",
                                     description="Seconds between asking the clouds for changes")
        self.poll = Adw.SpinRow.new_with_range(10, 3600, 5)
        self.poll.set_title("Check every")
        self.poll.set_value(s["poll_seconds"])
        watch.add(self.poll)
        page.add(watch)
        self.modes = modes
        return page

    def read_settings(self):
        """Settings from the widgets into the profile (saved with the selection)."""
        s = self.sel.prof.setdefault("settings", {})
        s["notify"] = self.modes[self.notify_row.get_selected()]
        s["notify_own"] = self.notify_own.get_active()
        s["backup_days"] = int(self.backup_days.get_value())
        s["poll_seconds"] = int(self.poll.get_value())

    # --- helpers --------------------------------------------------------------

    _busy = 0

    def busy(self, delta):
        self._busy += delta
        self.spinner.set_spinning(self._busy > 0)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))


def run(cs, show=None):
    """show: "remote:path" to open at; a second call reuses the open window."""
    app = Adw.Application(application_id=APP_ID,
                          flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    def command_line(a, cl):
        win = a.get_active_window() or Window(a, cs)
        win.present()
        args = cl.get_arguments()[1:]
        if args and args[0] == "@history":
            win.fill_history()
            win.stack.set_visible_child_name("history")
        elif args and ":" in args[0]:
            win.reveal(*args[0].split(":", 1))
        return 0
    app.connect("command-line", command_line)
    return app.run(["cloudsync"] + ([show] if show else []))
