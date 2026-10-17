// cloudsync search: find files of rclone remotes in the GNOME Shell overview,
// including files that aren't synced locally. Reads the name index written
// by `cloudsync index` (~/.cache/cloudsync/index.tsv).

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

Gio._promisify(Gio.File.prototype, 'load_contents_async');

const APP_ID = 'cloudsync.desktop';
const MAX_CANDIDATES = 200;  // checked for a local copy, best matches first
const LINK_SUFFIX = '.link.html';

class Index {
    constructor(path) {
        this._file = Gio.File.new_for_path(path);
        this._mtime = null;
        this.sources = new Map();
        this.entries = [];
    }

    async refresh(cancellable) {
        let mtime;
        try {
            const info = this._file.query_info('time::modified', Gio.FileQueryInfoFlags.NONE, null);
            mtime = info.get_modification_date_time().to_unix();
        } catch {
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
    constructor(extension) {
        this.id = extension.uuid;
        this.isRemoteProvider = false;
        this.canLaunchSearch = true;
        this._index = new Index(GLib.build_filenamev(
            [GLib.get_user_cache_dir(), 'cloudsync', 'index.tsv']));
        this._appInfo = Gio.DesktopAppInfo.new(APP_ID);
    }

    get appInfo() {
        return this._appInfo;
    }

    _localPath(e) {
        const src = this._index.sources.get(e.source);
        return src?.local ? GLib.build_filenamev([src.local, e.path]) : null;
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
        const ids = [];
        for (const [, , i] of scored.slice(0, MAX_CANDIDATES)) {
            // synced items are found by the normal file search already
            const local = this._localPath(this._index.entries[i]);
            if (local && GLib.file_test(local, GLib.FileTest.EXISTS))
                continue;
            ids.push(String(i));
        }
        return ids;
    }

    async getInitialResultSet(terms, cancellable) {
        try {
            await this._index.refresh(cancellable);
        } catch (e) {
            if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                logError(e, 'cloudsync search: reading the index failed');
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
        return ids.map(id => this._index.entries[Number(id)]).filter(e => e).map((e, n) => {
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
                id: ids[n],
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
        const src = this._index.sources.get(e.source);
        const local = this._localPath(e);
        let uri;
        if (local && GLib.file_test(local, GLib.FileTest.EXISTS))
            uri = Gio.File.new_for_path(local).get_uri();
        else if (src?.url)
            uri = src.url.replace('{id}', encodeURIComponent(e.id))
                .replace('{path}', encodeURI(e.path))
                .replace('{dir}', encodeURI(e.dir));
        if (!uri) {
            Main.notify('Cloud search', `No web link configured for ${src?.label ?? e.source}`);
            return;
        }
        this._open(uri);
    }

    launchSearch(terms) {
        const urls = [...this._index.sources.values()].map(s => s.searchUrl).filter(u => u);
        if (urls.length)
            this._open(urls[0].replace('{terms}', encodeURIComponent(terms.join(' '))));
    }

    _open(uri) {
        try {
            Gio.AppInfo.launch_default_for_uri(uri, global.create_app_launch_context(0, -1));
        } catch (e) {
            logError(e, `cloudsync search: can't open ${uri}`);
        }
    }
}

export default class CloudsyncSearchExtension extends Extension {
    enable() {
        this._provider = new CloudSearchProvider(this);
        Main.overview.searchController.addProvider(this._provider);
    }

    disable() {
        Main.overview.searchController.removeProvider(this._provider);
        this._provider = null;
    }
}
