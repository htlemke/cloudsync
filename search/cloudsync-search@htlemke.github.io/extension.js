// cloudsync for GNOME Shell:
// - overview search over the file names of rclone remotes, including files
//   that aren't synced locally (index written by `cloudsync index`,
//   ~/.cache/cloudsync/index.tsv). Click: open in the browser.
//   Ctrl+click: open the Cloud Sync window at that folder to add it.
// - top-bar indicator with the sync state (~/.cache/cloudsync/status.json).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

Gio._promisify(Gio.File.prototype, 'load_contents_async');

const APP_ID = 'cloudsync.desktop';
const MAX_CANDIDATES = 200;  // checked for a local copy, best matches first
const LINK_SUFFIX = '.link.html';
const CACHE_DIR = GLib.build_filenamev([GLib.get_user_cache_dir(), 'cloudsync']);
const HISTORY = GLib.build_filenamev([GLib.get_user_data_dir(), 'cloudsync', 'history.jsonl']);
const RECENT = 8;

function cloudsyncPath() {
    return GLib.find_program_in_path('cloudsync') ??
        GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'cloudsync']);
}

function spawn(argv) {
    try {
        Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE);
    } catch (e) {
        Main.notify('Cloud Sync', `Can't run ${argv[0]}: ${e.message}`);
    }
}

function openUri(uri) {
    try {
        Gio.AppInfo.launch_default_for_uri(uri, global.create_app_launch_context(0, -1));
    } catch (e) {
        logError(e, `cloudsync: can't open ${uri}`);
    }
}

function ago(seconds) {
    const s = Math.max(0, GLib.get_real_time() / 1e6 - seconds);
    if (s < 90)
        return 'just now';
    if (s < 5400)
        return `${Math.round(s / 60)} min ago`;
    if (s < 129600)
        return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
}

function fileMtime(path) {
    try {
        return Gio.File.new_for_path(path)
            .query_info('time::modified', Gio.FileQueryInfoFlags.NONE, null)
            .get_modification_date_time().to_unix();
    } catch {
        return null;
    }
}

// --- search ------------------------------------------------------------------

class Index {
    constructor() {
        this.path = GLib.build_filenamev([CACHE_DIR, 'index.tsv']);
        this._file = Gio.File.new_for_path(this.path);
        this._mtime = null;
        this.sources = new Map();
        this.entries = [];
    }

    async refresh(cancellable) {
        const mtime = fileMtime(this.path);
        if (mtime === null) {
            this.sources.clear();
            this.entries = [];
            return;
        }
        if (mtime === this._mtime)
            return;
        const [bytes] = await this._file.load_contents_async(cancellable);
        const sources = new Map();
        const entries = [];
        for (const line of new TextDecoder().decode(bytes).split('\n')) {
            if (!line)
                continue;
            const f = line.split('\t');
            if (line.startsWith('#')) {
                if (f[0] === '#source')
                    sources.set(f[1], {label: f[2], local: f[3], url: f[4], searchUrl: f[5]});
                continue;
            }
            if (f.length < 4 || !sources.has(f[0]))
                continue;
            const path = f.slice(3).join('\t');
            const slash = path.lastIndexOf('/');
            const name = path.slice(slash + 1);
            entries.push({
                source: f[0], isDir: f[1] === '1', id: f[2], path,
                name, dir: slash < 0 ? '' : path.slice(0, slash),
                lowerName: name.toLowerCase(), lowerPath: path.toLowerCase(),
            });
        }
        this.sources = sources;
        this.entries = entries;
        this._mtime = mtime;
    }
}

class CloudSearchProvider {
    constructor(extension, index) {
        this.id = extension.uuid;
        this.isRemoteProvider = false;
        this.canLaunchSearch = true;
        this._index = index;
        this._appInfo = Gio.DesktopAppInfo.new(APP_ID);
    }

    get appInfo() {
        return this._appInfo;
    }

    _localPath(e) {
        const src = this._index.sources.get(e.source);
        return src?.local ? GLib.build_filenamev([src.local, e.path]) : null;
    }

    _isLocal(e) {
        const local = this._localPath(e);
        return local !== null && GLib.file_test(local, GLib.FileTest.EXISTS);
    }

    _search(terms) {
        terms = terms.map(t => t.toLowerCase()).filter(t => t);
        if (!terms.length)
            return [];
        const scored = [];
        this._index.entries.forEach((e, i) => {
            // every term somewhere in the path, at least one in the name
            if (!terms.every(t => e.lowerPath.includes(t)) ||
                !terms.some(t => e.lowerName.includes(t)))
                return;
            let score = 3;
            for (const t of terms) {
                if (e.lowerName === t || e.lowerName === t + LINK_SUFFIX)
                    score = Math.min(score, 0);
                else if (e.lowerName.startsWith(t))
                    score = Math.min(score, 1);
                else if (e.lowerName.includes(t))
                    score = Math.min(score, 2);
            }
            scored.push([score, e.path.length, i]);
        });
        scored.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
        // synced items are found by the normal file search already
        return scored.slice(0, MAX_CANDIDATES)
            .map(([, , i]) => i)
            .filter(i => !this._isLocal(this._index.entries[i]))
            .map(String);
    }

    async getInitialResultSet(terms, cancellable) {
        try {
            await this._index.refresh(cancellable);
        } catch (e) {
            if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                logError(e, 'cloudsync: reading the index failed');
            return [];
        }
        return this._search(terms);
    }

    getSubsearchResultSet(previousResults, terms, cancellable) {
        return this.getInitialResultSet(terms, cancellable);
    }

    filterResults(results, maxResults) {
        return results.slice(0, maxResults);
    }

    async getResultMetas(ids, _cancellable) {
        return ids.map(id => [id, this._index.entries[Number(id)]]).filter(([, e]) => e)
            .map(([id, e]) => {
                const src = this._index.sources.get(e.source);
                const isLink = e.name.endsWith(LINK_SUFFIX);
                const name = isLink ? e.name.slice(0, -LINK_SUFFIX.length) : e.name;
                let gicon;
                if (e.isDir)
                    gicon = new Gio.ThemedIcon({name: 'folder-remote'});
                else if (isLink)
                    gicon = new Gio.ThemedIcon({name: 'x-office-document'});
                else
                    gicon = Gio.content_type_get_icon(Gio.content_type_guess(name, null)[0]);
                return {
                    id,
                    name,
                    description: `${src?.label ?? e.source} · ${e.dir || '/'}`,
                    createIcon: size => new St.Icon({gicon, icon_size: size}),
                };
            });
    }

    activateResult(id, _terms) {
        const e = this._index.entries[Number(id)];
        if (!e)
            return;
        const [, , mods] = global.get_pointer();
        if (mods & Clutter.ModifierType.CONTROL_MASK) {
            // add to sync: open the folder (or the file's folder) in the window
            spawn([cloudsyncPath(), 'gui', '--show', `${e.source}:${e.isDir ? e.path : e.dir}`]);
            return;
        }
        const src = this._index.sources.get(e.source);
        if (this._isLocal(e)) {
            openUri(Gio.File.new_for_path(this._localPath(e)).get_uri());
        } else if (src?.url) {
            openUri(src.url.replace('{id}', encodeURIComponent(e.id))
                .replace('{path}', encodeURI(e.path))
                .replace('{dir}', encodeURI(e.isDir ? e.path : e.dir))
                .replace('{name}', encodeURIComponent(e.isDir ? '' : e.name)));
        } else {
            Main.notify('Cloud Sync', `No web link for ${src?.label ?? e.source}. ` +
                'Ctrl+click to open it in Cloud Sync.');
        }
    }

    launchSearch(terms) {
        const urls = [...this._index.sources.values()].map(s => s.searchUrl).filter(u => u);
        if (urls.length)
            openUri(urls[0].replace('{terms}', encodeURIComponent(terms.join(' '))));
    }
}

// --- top-bar indicator ---------------------------------------------------------

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(index) {
        super._init(0.5, 'Cloud Sync');
        this._index = index;
        this._statusPath = GLib.build_filenamev([CACHE_DIR, 'status.json']);
        this._status = {};

        this._icon = new St.Icon({icon_name: 'folder-remote-symbolic', style_class: 'system-status-icon'});
        this.add_child(this._icon);

        this._stateItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._watchItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._indexItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this.menu.addMenuItem(this._stateItem);
        this.menu.addMenuItem(this._watchItem);
        this.menu.addMenuItem(this._indexItem);
        this._recent = new PopupMenu.PopupSubMenuMenuItem('Recent changes');
        this.menu.addMenuItem(this._recent);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Sync now',
            () => spawn(['systemctl', '--user', 'start', '--no-block', 'cloudsync.service']));
        this.menu.addAction('Open Cloud Sync', () => spawn([cloudsyncPath(), 'gui']));
        this.menu.addAction('Refresh search index',
            () => spawn(['systemctl', '--user', 'start', '--no-block', 'cloudsync-index.service']));
        this.menu.addAction('Open logs', () => {
            if (this._status.logs)
                openUri(Gio.File.new_for_path(this._status.logs).get_uri());
        });
        this.menu.connect('open-state-changed', (_m, open) => {
            if (open) {
                this._update();
                this._fillRecent();
            }
        });

        GLib.mkdir_with_parents(CACHE_DIR, 0o755);
        this._monitor = Gio.File.new_for_path(this._statusPath)
            .monitor_file(Gio.FileMonitorFlags.NONE, null);
        this._monitor.connect('changed', () => this._load());
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, 60, () => {
            this._update();
            return GLib.SOURCE_CONTINUE;
        });
        this._load();
    }

    _load() {
        try {
            const [, bytes] = GLib.file_get_contents(this._statusPath);
            this._status = JSON.parse(new TextDecoder().decode(bytes));
        } catch {
            this._status = {};
        }
        this._update();
    }

    _update() {
        const s = this._status;
        const running = s.state === 'running' && s.pid &&
            GLib.file_test(`/proc/${s.pid}`, GLib.FileTest.EXISTS);
        let icon = 'folder-remote-symbolic';
        let text;
        if (running) {
            icon = 'emblem-synchronizing-symbolic';
            text = `Syncing since ${ago(s.started)}`;
        } else if (s.state === 'failed' || s.state === 'running') {
            icon = 'dialog-warning-symbolic';
            text = `Sync failed ${ago(s.finished ?? s.started)}: ${(s.failed ?? []).join(', ') || 'stopped'}`;
            if (s.last_ok)
                text += `\nLast good sync ${ago(s.last_ok)}`;
        } else if (s.last_ok) {
            text = `Last sync ${ago(s.last_ok)}`;
            if (GLib.get_real_time() / 1e6 - s.last_ok > 3 * 3600)
                icon = 'dialog-warning-symbolic';
        } else {
            text = 'No sync yet';
        }
        const w = s.watch ?? {};
        const watching = w.pid && GLib.file_test(`/proc/${w.pid}`, GLib.FileTest.EXISTS);
        if (watching) {
            const feeds = Object.entries(w.feeds ?? {})
                .map(([name, state]) => state === 'ok' ? name : `${name}: ${state}`);
            this._watchItem.label.text = `Watching for changes${feeds.length ? '\n' + feeds.join('\n') : ''}`;
        } else {
            this._watchItem.label.text = 'Not watching (changes sync with the timer)';
        }
        this._icon.icon_name = icon;
        this._stateItem.label.text = text;
        const mtime = fileMtime(this._index.path);
        this._indexItem.label.text = mtime === null ? 'No search index yet'
            : `Search index updated ${ago(mtime)}`;
    }

    _fillRecent() {
        this._recent.menu.removeAll();
        let lines = [];
        try {
            const [, bytes] = GLib.file_get_contents(HISTORY);
            lines = new TextDecoder().decode(bytes).trimEnd().split('\n').slice(-RECENT).reverse();
        } catch {}
        const entries = lines.map(l => {
            try {
                return JSON.parse(l);
            } catch {
                return null;
            }
        }).filter(e => e);
        if (!entries.length) {
            this._recent.menu.addMenuItem(new PopupMenu.PopupMenuItem('No changes yet', {reactive: false}));
            return;
        }
        for (const e of entries) {
            const name = e.path.split('/').pop();
            const where = e.side === 'cloud' ? 'from the cloud' : 'here';
            const who = e.who ? `, ${e.who}` : '';
            const label = `${name} – ${e.action} ${where}${who}, ${ago(e.t)}`;
            this._recent.menu.addAction(label, () => spawn([cloudsyncPath(), 'gui', '--show-history']));
        }
        this._recent.menu.addAction('Show all…', () => spawn([cloudsyncPath(), 'gui', '--show-history']));
    }

    destroy() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = null;
        this._monitor?.cancel();
        this._monitor = null;
        super.destroy();
    }
});

export default class CloudsyncExtension extends Extension {
    enable() {
        this._index = new Index();
        this._provider = new CloudSearchProvider(this, this._index);
        Main.overview.searchController.addProvider(this._provider);
        this._indicator = new Indicator(this._index);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        Main.overview.searchController.removeProvider(this._provider);
        this._indicator.destroy();
        this._provider = null;
        this._indicator = null;
        this._index = null;
    }
}
