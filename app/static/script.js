const searchInput = document.getElementById('searchInput');
const searchBtn = document.getElementById('searchBtn');
const resultsContainer = document.getElementById('resultsContainer');
const loader = document.getElementById('loader');
const modal = document.getElementById('downloadModal');
const modalClose = document.getElementById('modalClose');
const modalCover = document.getElementById('modalCover');
const modalTitle = document.getElementById('modalTitle');
const modalAuthor = document.getElementById('modalAuthor');
const modalNarrator = document.getElementById('modalNarrator');
const modalLoader = document.getElementById('modalLoader');
const abbResults = document.getElementById('abbResults');
const langFilter = document.getElementById('langFilter');
const abbSearchInfo = document.getElementById('abbSearchInfo');

const navItems = document.querySelectorAll('.nav-item');
const views = document.querySelectorAll('.content-wrapper');

let currentABBData = [];
let currentModalBook = null;
let appSettings = { language: "English", auto_match_narrator: true };
let appLibrary = [];
let appSeries = [];
let activityTimer = null;

// Escape text before inserting it into HTML (titles etc. come from third-party sites)
function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// Release date as Audible gives it, minus its "2200-01-01" placeholder for books without one
function releaseDate(value, length = 10) {
    const date = String(value || '');
    return date >= '2100' ? 'TBA' : date.slice(0, length);
}

// Only allow link/image URLs with expected schemes
function safeUrl(url, fallback = '#') {
    const value = String(url ?? '');
    return /^(https?:|magnet:|\/)/i.test(value) ? esc(value) : fallback;
}

const PLACEHOLDER_COVER = '/static/placeholder.svg';

// Same matching rules as the server: ASIN, or title (before any subtitle) + first author
function normKey(text) {
    return String(text || '').toLowerCase().replace(/^(the|a|an)\s+/, '').replace(/[^a-z0-9]+/g, '');
}

function primaryAuthor(authors) {
    return String(authors || '').split(',')[0].split('&')[0].trim();
}

// Editions: narrated (one or a few narrators), dramatized (full cast, e.g. GraphicAudio), abridged
const EDITION_LABELS = { narrated: 'Narrated', dramatized: 'Dramatized', abridged: 'Abridged' };

function editionOf(book) {
    return EDITION_LABELS[book && book.edition] ? book.edition : 'narrated';
}

// A badge for anything but a narrated edition, plus a flag when the edition needs checking
function editionBadge(book) {
    const edition = editionOf(book);
    const badge = edition !== 'narrated'
        ? `<span class="edition-badge edition-${edition}" title="${esc(book.edition_reason || '')}">${EDITION_LABELS[edition]}</span>` : '';
    const check = book.edition_check
        ? `<span class="edition-badge edition-check" title="${esc(book.edition_reason || '')}">Check edition</span>` : '';
    return badge + check;
}

// Title matching, like the server's: without bracketed tags or edition words, the same
// book's title is equal, or is the other's main title or subtitle part ("Storm Front" /
// "Storm Front: Dresden Files, Book 1"), but "Series: One" and "Series: Two" differ
function titleKeys(title) {
    const plain = String(title || '').replace(/\s*[([][^)\]]*[)\]]/g, '')
        .replace(/\b(dramati[sz]ed|adaptation|graphic\s?audio|full[\s-]?cast|(un)?abridged)\b/gi, '')
        .trim().replace(/^-+|-+$/g, '').trim() || String(title || '');
    const [main, ...rest] = plain.split(':');
    const full = normKey(plain);
    return { full, keys: [full, normKey(main.trim()), normKey(rest.join(':').trim())].filter(Boolean) };
}

function titlesMatch(a, b) {
    const ka = titleKeys(a), kb = titleKeys(b);
    return Boolean(ka.full && kb.full) && (kb.keys.includes(ka.full) || ka.keys.includes(kb.full));
}

function authorKeyList(authors) {
    const keys = String(authors || '').split(',').map(normKey).filter(Boolean);
    return keys.length ? keys : [''];
}

function findInLibrary(book) {
    if (book.asin) {
        const byAsin = appLibrary.find(b => b.asin && b.asin === book.asin);
        if (byAsin) return byAsin;
    }
    // Title (before any subtitle) and any shared author, in the same edition: books don't
    // always list their authors in the same order, and a dramatized version is its own book
    const authorKeys = authorKeyList(book.authors);
    const edition = editionOf(book);
    const same = appLibrary.filter(b => editionOf(b) === edition && authorKeyList(b.authors).some(a => authorKeys.includes(a)));
    const full = titleKeys(book.title).full;
    return same.find(b => titleKeys(b.title).full === full) || same.find(b => titlesMatch(b.title, book.title));
}

function coverUrl(book) {
    if (book.id && book.path && book.cover) return `/api/library/${encodeURIComponent(book.id)}/cover`;
    return /^(https?:|\/)/i.test(book.imageUrl || '') ? book.imageUrl : PLACEHOLDER_COVER;
}

function statusClass(status) {
    return `status-${String(status || '').toLowerCase().replace(/\s+/g, '-')}`;
}

function formatDuration(seconds) {
    if (seconds == null || seconds < 0 || seconds >= 8640000) return '';
    const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
}

function seriesLabel(book) {
    if (!book.series) return '';
    const name = editionOf(book) === 'dramatized' ? `${book.series} (Dramatized)` : book.series;
    return book.sequence ? `${name} #${book.sequence}` : name;
}

function formatSize(bytes) {
    if (!bytes) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, value = bytes;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return `${value.toFixed(i >= 3 ? 2 : 0)} ${units[i]}`;
}

function setActionStatus(el, text, kind = '') {
    el.textContent = text;
    el.className = `action-status ${kind}`;
}

// Toast notifications for results of actions
function toast(message, kind = '') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.textContent = message;
    document.getElementById('toasts').appendChild(el);
    setTimeout(() => {
        el.classList.add('leaving');
        setTimeout(() => el.remove(), 300);
    }, kind === 'error' ? 7000 : 4000);
}

// A styled replacement for window.confirm(); resolves to true or false
let confirmResolve = null;
function confirmDialog(message, { title = 'Are you sure?', confirmText = 'OK', danger = false } = {}) {
    const dialog = document.getElementById('confirmModal');
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    const ok = document.getElementById('confirmOk');
    ok.textContent = confirmText;
    ok.className = danger ? 'danger-btn' : 'primary-btn';
    if (confirmResolve) confirmResolve(false);
    showModal(dialog);
    setTimeout(() => ok.focus(), 50);
    return new Promise(resolve => { confirmResolve = resolve; });
}

function closeConfirm(result) {
    hideModal(document.getElementById('confirmModal'));
    if (confirmResolve) {
        confirmResolve(result);
        confirmResolve = null;
    }
}

document.querySelectorAll('#confirmModal [data-confirm]').forEach(btn =>
    btn.addEventListener('click', () => closeConfirm(btn.dataset.confirm === 'ok')));
document.getElementById('confirmModal').addEventListener('click', (e) => {
    if (e.target.id === 'confirmModal') closeConfirm(false);
});

// Escape closes the top-most open dialog
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const open = [...document.querySelectorAll('.modal.show')];
    const top = open[open.length - 1];
    if (!top) return;
    if (top.id === 'confirmModal') closeConfirm(false);
    else if (top.id === 'downloadModal') closeModal();
    else hideModal(top);
});

async function postJSON(url, body = {}, method = 'POST') {
    const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, data };
}

// Initialize
initApp();

async function initApp() {
    await fetchSettings();
    await Promise.all([fetchLibrary(), fetchSeries()]);
    // Setup listeners
    setupNavigation();
    setupSettings();
    setupLibrary();
    setupSeriesPages();
    setupCalendar();
    setupSystem();
    setupIndexers();
    setupAuthors();
    setupHistoryLimit();
    setupManualImport();
    window.addEventListener('hashchange', route);
    route();
}

// Each page has its own address (#/library, #/series/<key>, ...), so the browser's
// Back and Forward buttons, refreshing and bookmarks all work
const PAGES = { book: 'libraryView', import: 'importView', search: 'searchView', library: 'libraryView', series: 'seriesView', calendar: 'calendarView', activity: 'activityView', system: 'systemView', settings: 'settingsView', author: 'authorView', authors: 'authorsView' };
const PAGE_NAMES = Object.fromEntries(Object.entries(PAGES).map(([name, view]) => [view, name]));

function bookLink(id, title) {
    if (!id || !appLibrary.some(b => b.id === id)) return esc(title);
    return `<a href="#/book/${encodeURIComponent(id)}" class="book-link" data-id="${esc(id)}">${esc(title)}</a>`;
}

document.addEventListener('click', (e) => {
    const link = e.target.closest('a.book-link');
    if (!link || e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
    e.preventDefault();
    e.stopPropagation();
    openBookModal(link.dataset.id);
}, true);

function navigate(path) {
    if (location.hash === '#' + path) {
        route();  // Same page again (e.g. reloading a series after a change)
    } else {
        location.hash = path;  // Adds a history entry; hashchange calls route()
    }
}

function route() {
    // Going Back from a page with a dialog open leaves the dialog behind
    document.querySelectorAll('.modal.show').forEach(hideModal);
    const [name = 'library', ...rest] = location.hash.replace(/^#\/?/, '').split('/');
    const arg = rest.length ? decodeURIComponent(rest.join('/')) : '';
    if (!PAGES[name]) {
        history.replaceState(null, '', '#/library');  // The landing page
        return route();
    }
    if (name === 'series' && arg) {
        showSeriesDetail(arg);
        return;
    }
    if (name === 'book') {
        // A book's own address (opened in a new tab, or a bookmark): the Library with its details
        history.replaceState(null, '', '#/library');
        activateView('libraryView');
        if (arg) openBookModal(arg);
        return;
    }
    if (name === 'author' && arg) {
        showAuthorPage(arg);
        return;
    }
    activateView(PAGES[name]);
    if (name === 'system') showSystemTab(arg || 'health');
    if (name === 'search' && arg && arg !== lastSearchQuery) {
        searchInput.value = arg;
        runSearch(arg);
    }
}

function activateView(viewId) {
    showView(viewId, viewId);
    document.querySelector('.main-content').scrollTop = 0;
    if (viewId === 'libraryView') renderLibrary();
    if (viewId === 'seriesView') renderSeries();
    if (viewId === 'calendarView') renderCalendar();
    if (viewId === 'settingsView') pollSettingsQueue();
    if (viewId === 'authorsView') renderFollowedAuthors();
    if (viewId === 'importView') openManualImport();
    if (viewId === 'activityView') {
        renderActivity();
        activityTimer = setInterval(renderActivity, 5000);
    }
}

function setupNavigation() {
    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            navigate('/' + PAGE_NAMES[item.getAttribute('data-view')]);
        });
    });
}

// API Calls
async function fetchSettings() {
    try {
        const res = await fetch('/api/settings');
        const data = await res.json();
        appSettings = data.settings || appSettings;

        // Sync UI
        document.getElementById('setLanguage').value = appSettings.language || "English";
        document.getElementById('setAutoMatch').checked = appSettings.auto_match_narrator ?? true;
        document.getElementById('setQbtEnabled').checked = appSettings.qbt_enabled ?? false;
        document.getElementById('setQbtHost').value = appSettings.qbt_host || "http://localhost:8080";
        document.getElementById('setRootFolder').value = appSettings.root_folder || "";
        document.getElementById('setDownloadsFolder').value = appSettings.downloads_folder || "";
        document.getElementById('setNamingFormat').value = appSettings.naming_format || "";
        document.getElementById('setRenameFiles').checked = appSettings.rename_files ?? true;
        document.getElementById('setStallHours').value = appSettings.stall_hours ?? 6;
        document.getElementById('setRemoveStalled').checked = appSettings.remove_stalled ?? true;
        document.getElementById('setVerifyRuntime').checked = appSettings.verify_runtime ?? true;
        document.getElementById('setRuntimeTolerance').value = appSettings.runtime_tolerance ?? 10;
        document.getElementById('setWriteMetadata').checked = appSettings.write_metadata ?? true;
        document.getElementById('setAutoConvert').checked = appSettings.auto_convert_m4b ?? false;
        document.getElementById('setDeleteOriginals').checked = appSettings.delete_originals_after_convert ?? false;
        convertStatus().then(st => {
            document.getElementById('convertAvailability').textContent = st && st.available ? ''
                : "ffmpeg isn't installed here, so books can't be converted (it's included in the Docker image).";
        });
        document.getElementById('setAbbEnabled').checked = appSettings.abb_enabled ?? true;
        document.getElementById('setAbbUrl').value = appSettings.abb_url || '';
        document.getElementById('setAbbUserAgent').value = appSettings.abb_user_agent || '';
        document.getElementById('setAbbCookie').value = '';
        abbCookieClearing = false;
        updateAbbCookieState();
        document.getElementById('setPrefNarrators').value = appSettings.pref_narrators || '';
        document.getElementById('setAvoidNarrators').value = appSettings.avoid_narrators || '';
        document.getElementById('setPreferredWords').value = appSettings.preferred_words || '';
        document.getElementById('setBlockedWords').value = appSettings.blocked_words || '';
        document.getElementById('setBlockedUploaders').value = appSettings.blocked_uploaders || '';
        document.getElementById('setMinBitrate').value = appSettings.min_bitrate || 0;
        document.getElementById('setMaxSize').value = appSettings.max_size_gb || 0;
        document.getElementById('setAbsUrl').value = appSettings.abs_url || "";
        document.getElementById('setAbsToken').value = "";
        document.getElementById('setAbsToken').placeholder = appSettings.abs_token_set ? "Unchanged" : "";
        const absSelect = document.getElementById('setAbsLibrary');
        if (appSettings.abs_library_id && ![...absSelect.options].some(o => o.value === appSettings.abs_library_id)) {
            absSelect.add(new Option(`Saved library (${appSettings.abs_library_id})`, appSettings.abs_library_id));
        }
        absSelect.value = appSettings.abs_library_id || "";
        document.getElementById('setQbtUser').value = appSettings.qbt_user || "admin";
        document.getElementById('setQbtPass').value = "";
        document.getElementById('setQbtPass').placeholder = appSettings.qbt_pass_set ? "Unchanged" : "";
        document.getElementById('setFormatPref').value = appSettings.format_preference || "prefer_m4b";
        document.getElementById('setEditionPref').value = appSettings.edition_preference || "narrated";
        document.getElementById('setAuthUser').value = appSettings.auth_username || "";
        if (appSettings.auth_from_env) {
            document.getElementById('setAuthUser').disabled = true;
            document.getElementById('setAuthPass').disabled = true;
            document.getElementById('saveAuthBtn').disabled = true;
            document.getElementById('authHint').textContent = "Login is set by the BAYARR_USERNAME / BAYARR_PASSWORD environment variables.";
        }

        // Sync filter in modal
        langFilter.value = appSettings.language;
    } catch (err) {
        console.error("Failed to load settings", err);
    }
}

async function fetchLibrary() {
    try {
        const res = await fetch('/api/library');
        const data = await res.json();
        appLibrary = data.library || [];
        updateActivityBadge();
    } catch (err) {
        console.error("Failed to load library", err);
    }
}

function setupSettings() {
    document.getElementById('saveSettingsBtn').addEventListener('click', async () => {
        const newSettings = {
            language: document.getElementById('setLanguage').value,
            format_preference: document.getElementById('setFormatPref').value,
            edition_preference: document.getElementById('setEditionPref').value,
            auto_match_narrator: document.getElementById('setAutoMatch').checked,
            qbt_enabled: document.getElementById('setQbtEnabled').checked,
            qbt_host: document.getElementById('setQbtHost').value,
            root_folder: document.getElementById('setRootFolder').value,
            downloads_folder: document.getElementById('setDownloadsFolder').value,
            naming_format: document.getElementById('setNamingFormat').value,
            rename_files: document.getElementById('setRenameFiles').checked,
            stall_hours: parseInt(document.getElementById('setStallHours').value, 10) || 0,
            remove_stalled: document.getElementById('setRemoveStalled').checked,
            verify_runtime: document.getElementById('setVerifyRuntime').checked,
            runtime_tolerance: parseInt(document.getElementById('setRuntimeTolerance').value, 10) || 10,
            write_metadata: document.getElementById('setWriteMetadata').checked,
            auto_convert_m4b: document.getElementById('setAutoConvert').checked,
            delete_originals_after_convert: document.getElementById('setDeleteOriginals').checked,
            pref_narrators: document.getElementById('setPrefNarrators').value.trim(),
            avoid_narrators: document.getElementById('setAvoidNarrators').value.trim(),
            preferred_words: document.getElementById('setPreferredWords').value.trim(),
            blocked_words: document.getElementById('setBlockedWords').value.trim(),
            blocked_uploaders: document.getElementById('setBlockedUploaders').value.trim(),
            min_bitrate: parseInt(document.getElementById('setMinBitrate').value, 10) || 0,
            max_size_gb: parseFloat(document.getElementById('setMaxSize').value) || 0,
            abb_enabled: document.getElementById('setAbbEnabled').checked,
            abb_url: document.getElementById('setAbbUrl').value.trim(),
            abb_user_agent: document.getElementById('setAbbUserAgent').value.trim(),
            abb_cookie: document.getElementById('setAbbCookie').value.trim(),
            abb_cookie_clear: abbCookieClearing,
            abs_url: document.getElementById('setAbsUrl').value.trim(),
            abs_token: document.getElementById('setAbsToken').value,
            abs_library_id: document.getElementById('setAbsLibrary').value,
            qbt_user: document.getElementById('setQbtUser').value,
            qbt_pass: document.getElementById('setQbtPass').value
        };

        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newSettings)
            });
            if (!res.ok) throw new Error(`Save failed (${res.status})`);
            if (newSettings.abs_token) appSettings.abs_token_set = true;
            delete newSettings.qbt_pass;
            delete newSettings.abs_token;
            delete newSettings.abb_cookie;
            delete newSettings.abb_cookie_clear;
            appSettings = { ...appSettings, ...newSettings };
            document.getElementById('setQbtPass').value = "";
            document.getElementById('setAbsToken').value = "";
            langFilter.value = appSettings.language; // update modal sync
            await fetchSettings();
            markSettingsClean();
            toast('Settings saved', 'ok');
        } catch (err) {
            console.error(err);
            toast('Could not save settings', 'error');
        }
    });

    // Sections
    document.querySelectorAll('.settings-tab').forEach(tab =>
        tab.addEventListener('click', () => showSettingsSection(tab.dataset.section)));

    // Unsaved-changes hint on the save bar
    document.querySelectorAll('.settings-section:not([data-section="security"]):not([data-section="backup"])').forEach(section => {
        // Indexers are saved on their own, not with the save bar
        section.addEventListener('input', e => { if (!e.target.closest('.no-dirty')) markSettingsDirty(); });
        section.addEventListener('change', e => { if (!e.target.closest('.no-dirty')) markSettingsDirty(); });
    });

    document.getElementById('qbtTestBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        const status = document.getElementById('qbtStatus');
        btn.disabled = true;
        status.textContent = 'Connecting...';
        const { ok, data } = await postJSON('/api/qbittorrent/test', {
            host: document.getElementById('setQbtHost').value.trim(),
            user: document.getElementById('setQbtUser').value,
            password: document.getElementById('setQbtPass').value,
        });
        btn.disabled = false;
        status.textContent = ok ? `Connected to qBittorrent ${data.version}.` : (data.detail || 'Connection failed');
        status.className = `settings-hint ${ok ? 'ok-text' : 'error-text'}`;
    });

    document.getElementById('restoreBtn').addEventListener('click', async () => {
        const file = document.getElementById('restoreFile').files[0];
        if (!file) {
            toast('Choose a backup file first', 'error');
            return;
        }
        let data;
        try {
            data = JSON.parse(await file.text());
        } catch (err) {
            toast("That file isn't valid JSON", 'error');
            return;
        }
        const books = Array.isArray(data.library) ? data.library.length : 0;
        const confirmed = await confirmDialog(
            `Replace your library, series, history and settings with this backup (${books} books)?\n\nThe current database is saved to the backups folder first. Your login stays as it is.`,
            { title: 'Restore backup', confirmText: 'Restore', danger: true });
        if (!confirmed) return;
        const res = await postJSON('/api/restore', data);
        if (!res.ok) {
            toast(res.data.detail || 'Restore failed', 'error');
            return;
        }
        toast(`Restored ${res.data.books} books. Reloading…`, 'ok');
        setTimeout(() => location.reload(), 1200);
    });

    document.getElementById('absLoadBtn').addEventListener('click', async () => {
        const status = document.getElementById('absStatus');
        const select = document.getElementById('setAbsLibrary');
        status.textContent = 'Connecting...';
        const res = await fetch('/api/audiobookshelf/libraries', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: document.getElementById('setAbsUrl').value.trim(), token: document.getElementById('setAbsToken').value })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            status.textContent = data.detail || 'Connection failed';
            return;
        }
        const current = select.value;
        select.innerHTML = '<option value="">Don\'t scan</option>';
        data.libraries.forEach(lib => select.add(new Option(lib.name, lib.id)));
        select.value = data.libraries.some(l => l.id === current) ? current : (data.libraries[0]?.id || '');
        status.textContent = `Connected: ${data.libraries.length} book librar${data.libraries.length === 1 ? 'y' : 'ies'} found. Save to keep the choice.`;
    });

    document.getElementById('saveAuthBtn').addEventListener('click', async () => {
        const res = await fetch('/api/auth', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                username: document.getElementById('setAuthUser').value,
                password: document.getElementById('setAuthPass').value
            })
        });
        const data = await res.json().catch(() => ({}));
        toast(res.ok ? 'Login saved. Your browser will ask you to sign in.' : (data.detail || 'Could not save the login'), res.ok ? 'ok' : 'error');
        document.getElementById('setAuthPass').value = "";
    });
}

let settingsQueueTimer = null;

function pollSettingsQueue() {
    clearTimeout(settingsQueueTimer);
    const media = document.querySelector('.settings-section[data-section="media"]');
    if (document.getElementById('settingsView').hidden || media.hidden) return;
    renderConversions();
    settingsQueueTimer = setTimeout(pollSettingsQueue, 3000);
}

function showSettingsSection(name) {
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.section === name));
    document.querySelectorAll('.settings-section').forEach(sec => { sec.hidden = sec.dataset.section !== name; });
    if (name === 'media') pollSettingsQueue();
    // Security and Backup have their own buttons
    document.getElementById('settingsSaveBar').hidden = ['security', 'backup'].includes(name);
}

function markSettingsDirty() {
    document.getElementById('settingsSaveBar').classList.add('dirty');
    setActionStatus(document.getElementById('settingsSaveStatus'), 'Unsaved changes');
}

function markSettingsClean() {
    document.getElementById('settingsSaveBar').classList.remove('dirty');
    setActionStatus(document.getElementById('settingsSaveStatus'), '');
}

async function addToLibrary(e, bookData) {
    e.stopPropagation(); // prevent modal opening

    // Optimistic UI update
    const prevText = e.target.textContent;
    e.target.textContent = "Adding...";
    e.target.disabled = true;

    try {
        const res = await fetch('/api/library', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(bookData)
        });
        if (!res.ok) throw new Error(`Add failed (${res.status})`);
        const data = await res.json();
        await fetchLibrary(); // Refresh library
        e.target.textContent = data.status || "Monitored";
        e.target.classList.add('monitored-btn');
    } catch (err) {
        console.error(err);
        e.target.textContent = prevText;
        e.target.disabled = false;
    }
}

const STATUS_ORDER = ['Missing', 'Downloading', 'Downloaded', 'Monitored', 'Unreleased', 'Unmonitored', 'Imported'];

let librarySeriesNames = {};  // Audible series id -> the name its books are sorted under

function librarySortKey(book, sort) {
    const seq = parseFloat(book.sequence);
    const seqKey = isNaN(seq) ? '9999' : String(seq.toFixed(2)).padStart(8, '0');
    const title = normKey(book.title);
    // Dramatizations after the narrated books, as their own series; parts in order. Books
    // of one Audible series sort together even when they name it differently.
    const name = (book.series_asin && librarySeriesNames[book.series_asin]) || book.series;
    const series = book.series ? `0|${normKey(name)}|${editionOf(book) === 'dramatized' ? 1 : 0}` : '1';
    const release = `${seqKey}|${String(partOf(book.title)).padStart(3, '0')}|${title}`;
    switch (sort) {
        case 'title': return title;
        case 'series': return `${series}|${release}`;
        case 'added': return book.added || '';
        default: return `${normKey(primaryAuthor(book.authors)) ? '0|' + normKey(primaryAuthor(book.authors)) : '1'}|${series}|${release}`;
    }
}

// The heading a book sorts under in series order: its series, with dramatizations apart
function libraryGroupName(book) {
    if (!book.series) return 'Not in a series';
    const name = (book.series_asin && librarySeriesNames[book.series_asin]) || book.series;
    return editionOf(book) === 'dramatized' ? `${name} (Dramatized)` : name;
}

// "Book Title (Part 2 of 3)" -> 2; books not sold in parts -> 0
function partOf(title) {
    const m = String(title || '').match(/[([]\s*(?:part\s+)?(\d+)\s+of\s+\d+\s*[)\]]/i);
    return m ? parseInt(m[1], 10) : 0;
}

function renderLibrary() {
    const container = document.getElementById('libraryContainer');
    const loader = document.getElementById('libraryLoader');
    const stats = document.getElementById('libStats');
    const text = document.getElementById('libFilterText').value.trim().toLowerCase();
    const status = document.getElementById('libFilterStatus').value;
    const edition = document.getElementById('libFilterEdition').value;
    const sort = document.getElementById('libSort').value;

    loader.style.display = 'none';
    container.innerHTML = '';

    const counts = {};
    appLibrary.forEach(b => { counts[b.status] = (counts[b.status] || 0) + 1; });
    const parts = STATUS_ORDER.filter(st => counts[st]).map(st => `${counts[st]} ${st.toLowerCase()}`);
    stats.textContent = `${appLibrary.length} books${parts.length ? ' · ' + parts.join(' · ') : ''}`;

    if (appLibrary.length === 0) {
        container.innerHTML = `<div class="empty-state">
            <h3>Your library is empty</h3>
            <p>Bring in the audiobooks you already have, or find new ones on Audible.</p>
            <div class="empty-actions">
                <button class="primary-btn" data-empty-action="import">Import Existing</button>
                <button class="secondary-btn" data-empty-action="search">Search</button>
            </div>
        </div>`;
        container.querySelector('[data-empty-action="import"]').addEventListener('click', () => document.getElementById('openImportBtn').click());
        container.querySelector('[data-empty-action="search"]').addEventListener('click', () => document.querySelector('[data-view="searchView"]').click());
        return;
    }

    let books = appLibrary.filter(b => {
        if (status === '__wanted') {
            if (!WANTED_STATUSES.includes(b.status)) return false;
        } else if (status === '__unmatched') {
            if (b.asin) return false;
        } else if (status === '__checkedition') {
            if (!b.edition_check) return false;
        } else if (status && b.status !== status) return false;
        if (edition && editionOf(b) !== edition) return false;
        if (!text) return true;
        return [b.title, b.authors, b.series, b.narrators].join(' ').toLowerCase().includes(text);
    });
    librarySeriesNames = {};
    appLibrary.forEach(b => { if (b.series_asin && b.series && !librarySeriesNames[b.series_asin]) librarySeriesNames[b.series_asin] = b.series; });
    books.sort((a, b) => librarySortKey(a, sort).localeCompare(librarySortKey(b, sort)));
    if (sort === 'added') books.reverse();

    shownBookIds = books.map(b => b.id);
    container.classList.toggle('selecting', selectMode);
    updateBulkBar();

    if (books.length === 0) {
        container.innerHTML = `<div class="empty-state">
            <h3>No books match</h3>
            <p>Nothing in your library matches this filter.</p>
            <div class="empty-actions"><button class="secondary-btn" data-empty-action="clear">Clear Filter</button></div>
        </div>`;
        container.querySelector('[data-empty-action="clear"]').addEventListener('click', () => {
            document.getElementById('libFilterText').value = '';
            document.getElementById('libFilterStatus').value = '';
            document.getElementById('libFilterEdition').value = '';
            renderLibrary();
        });
        return;
    }

    const fragment = document.createDocumentFragment();
    let lastGroup = null;
    books.forEach(book => {
        if (sort === 'series') {
            const group = libraryGroupName(book);
            if (group !== lastGroup) {
                const count = books.filter(b => libraryGroupName(b) === group).length;
                const heading = document.createElement('h3');
                heading.className = 'library-group-heading';
                heading.innerHTML = `${esc(group)} <span class="muted">${count}</span>`;
                fragment.appendChild(heading);
                lastGroup = group;
            }
        }
        const card = document.createElement('div');
        card.className = 'book-card';
        const bookStatus = book.status || 'Monitored';
        const series = seriesLabel(book);

        card.classList.toggle('selected', selectedIds.has(book.id));
        card.innerHTML = `
            <span class="select-box" aria-hidden="true"></span>
            <div class="library-status ${esc(statusClass(bookStatus))}">${esc(bookStatus)}</div>
            <img src="${esc(coverUrl(book))}" alt="" class="book-cover" loading="lazy">
            <div class="book-info">
                <div class="book-title" title="${esc(book.title)}">${esc(book.title)}</div>
                ${series ? `<div class="book-series" title="${esc(series)}">${esc(series)}</div>` : ''}
                ${editionBadge(book) ? `<div class="edition-badges">${editionBadge(book)}</div>` : ''}
                <div class="book-author">${esc(book.authors)}</div>
                ${book.narrators ? `<div class="book-narrator">Narrated by: ${esc(book.narrators)}</div>` : ''}
            </div>
        `;
        card.querySelector('img').addEventListener('error', e => { e.target.src = PLACEHOLDER_COVER; }, { once: true });
        card.addEventListener('click', () => {
            if (!selectMode) {
                openBookModal(book.id);
                return;
            }
            selectedIds.has(book.id) ? selectedIds.delete(book.id) : selectedIds.add(book.id);
            card.classList.toggle('selected', selectedIds.has(book.id));
            updateBulkBar();
        });
        fragment.appendChild(card);
    });
    container.appendChild(fragment);
}

const WANTED_STATUSES = ['Monitored', 'Unreleased', 'Downloading', 'Downloaded', 'Needs Review', 'Missing'];
let selectMode = false;
const selectedIds = new Set();
let shownBookIds = [];

function updateBulkBar() {
    document.getElementById('bulkBar').hidden = !selectMode;
    document.getElementById('bulkCount').textContent = `${selectedIds.size} selected`;
    ['bulkStatus', 'bulkEdition', 'bulkMatch', 'bulkConvert', 'bulkRemove'].forEach(id => { document.getElementById(id).disabled = selectedIds.size === 0; });
    document.getElementById('selectModeBtn').textContent = selectMode ? 'Done' : 'Select';
}

function setSelectMode(on) {
    selectMode = on;
    if (!on) selectedIds.clear();
    renderLibrary();
}

async function runBulk(action, extra = {}) {
    const ids = [...selectedIds];
    const { ok, data } = await postJSON('/api/library/bulk', { ids, action, ...extra });
    if (!ok) {
        toast(data.detail || 'That didn\'t work', 'error');
        return false;
    }
    return data;
}

async function watchMatchJob() {
    for (;;) {
        await new Promise(r => setTimeout(r, 1500));
        const job = await fetch('/api/library/match_status').then(r => r.json()).catch(() => null);
        if (!job) return;
        if (!job.running) {
            await fetchLibrary();
            renderLibrary();
            const unsure = job.unsure ? ` ${job.unsure} weren't clear-cut; match those by hand from their details.` : '';
            toast(`Matched ${job.matched} of ${job.total} books on Audible.${unsure}`, job.matched ? 'ok' : '');
            return;
        }
    }
}

function setupLibrary() {
    document.getElementById('selectModeBtn').addEventListener('click', () => setSelectMode(!selectMode));
    document.getElementById('bulkSelectAll').addEventListener('click', () => {
        shownBookIds.forEach(id => selectedIds.add(id));
        renderLibrary();
    });
    document.getElementById('bulkClear').addEventListener('click', () => {
        selectedIds.clear();
        renderLibrary();
    });
    document.getElementById('bulkStatus').addEventListener('change', async (e) => {
        const status = e.target.value;
        e.target.value = '';
        if (!status) return;
        const data = await runBulk('status', { status });
        if (!data) return;
        toast(`${data.count} book${data.count === 1 ? '' : 's'} set to ${status}`, 'ok');
        await fetchLibrary();
        setSelectMode(false);
    });
    document.getElementById('bulkEdition').addEventListener('change', async (e) => {
        const edition = e.target.value;
        e.target.value = '';
        if (!edition) return;
        const detect = edition === '__detect';
        const data = await runBulk(detect ? 'detect_edition' : 'edition', detect ? {} : { edition });
        if (!data) return;
        toast(detect ? `Checked the edition of ${data.count} book${data.count === 1 ? '' : 's'}`
            : `${data.count} book${data.count === 1 ? '' : 's'} set to ${EDITION_LABELS[edition].toLowerCase()}`, 'ok');
        await fetchLibrary();
        setSelectMode(false);
    });
    document.getElementById('bulkMatch').addEventListener('click', async () => {
        const data = await runBulk('match');
        if (!data) return;
        toast(`Matching ${data.count} book${data.count === 1 ? '' : 's'} on Audible in the background…`);
        setSelectMode(false);
        watchMatchJob();
    });
    document.getElementById('bulkRemove').addEventListener('click', async () => {
        const n = selectedIds.size;
        const confirmed = await confirmDialog(`Remove ${n} book${n === 1 ? '' : 's'} from Bayarr?\n\nFiles on disk are not deleted.`,
            { title: 'Remove books', confirmText: 'Remove', danger: true });
        if (!confirmed) return;
        const data = await runBulk('remove');
        if (!data) return;
        toast(`Removed ${data.count} book${data.count === 1 ? '' : 's'}`, 'ok');
        await fetchLibrary();
        setSelectMode(false);
    });

    ['libFilterText', 'libFilterStatus', 'libFilterEdition', 'libSort'].forEach(id => {
        document.getElementById(id).addEventListener(id === 'libFilterText' ? 'input' : 'change', renderLibrary);
    });

    document.getElementById('rescanBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = 'Rescanning...';
        try {
            const res = await fetch('/api/library/rescan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            const data = await res.json();
            await fetchLibrary();
            renderLibrary();
            toast(`Rescan finished: ${data.missing} newly missing, ${data.restored} found again`, data.missing ? 'error' : 'ok');
        } catch (err) {
            console.error(err);
            toast('Rescan failed', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Rescan';
        }
    });

    setupBookModal();
    setupImportModal();
    setupSplitModal();
    setupOrganizeModal();
    setupListImport();
    setupConvert();
}

function showModal(el) {
    el.style.display = 'flex';
    void el.offsetWidth;
    el.classList.add('show');
}

function hideModal(el) {
    el.classList.remove('show');
    // Skip if it was reopened while fading out
    setTimeout(() => { if (!el.classList.contains('show')) el.style.display = 'none'; }, 200);
}

// -----------------
// BOOK DETAILS MODAL
// -----------------
let currentBookId = null;
const bookModal = document.getElementById('bookModal');
const BOOK_FIELDS = { bookTitle: 'title', bookAuthors: 'authors', bookNarrators: 'narrators', bookSeries: 'series', bookSequence: 'sequence', bookStatus: 'status', bookAsin: 'asin', bookRuntime: 'runtime_min', bookEdition: 'edition' };

async function openBookModal(bookId) {
    const book = appLibrary.find(b => b.id === bookId);
    if (!book) return;
    currentBookId = bookId;

    const cover = document.getElementById('bookCover');
    cover.src = coverUrl(book);
    cover.onerror = () => { cover.onerror = null; cover.src = PLACEHOLDER_COVER; };
    for (const [elId, key] of Object.entries(BOOK_FIELDS)) {
        document.getElementById(elId).value = book[key] || '';
    }
    document.getElementById('bookStatus').value = book.status || 'Monitored';
    document.getElementById('bookEdition').value = editionOf(book);
    document.getElementById('bookEditionReason').textContent = book.edition_reason
        ? `Edition: ${book.edition_reason}${book.edition_check ? '. Check this, then save to confirm.' : ''}` : '';
    const review = document.getElementById('bookReview');
    review.hidden = book.status !== 'Needs Review';
    document.getElementById('bookReviewReason').textContent = book.review_reason || '';
    renderBookSeriesLinks(book);
    document.getElementById('matchPanel').hidden = true;
    document.getElementById('matchBookBtn').textContent = book.asin ? 'Rematch on Audible' : 'Match on Audible';
    // Folders with several audio files might hold several books (a collection)
    document.getElementById('splitBookBtn').hidden = !(book.path && (book.file_count || 0) > 1);
    setActionStatus(document.getElementById('bookStatusMsg'), '');
    document.getElementById('searchNowBtn').disabled = !appSettings.qbt_enabled;
    document.getElementById('searchNowBtn').title = appSettings.qbt_enabled ? 'Search AudiobookBay and grab the best match' : 'Enable qBittorrent in Settings first';

    const pathEl = document.getElementById('bookPath');
    const filesEl = document.getElementById('bookFiles');
    pathEl.textContent = book.path ? `Location: ${book.path}` : 'Not on disk yet.';
    document.getElementById('convertBookBtn').hidden = true;
    document.getElementById('deleteOriginalsBtn').hidden = true;
    filesEl.innerHTML = '';
    showModal(bookModal);

    if (!book.path) return;
    filesEl.innerHTML = '<tr><td colspan="2" class="no-results">Loading files...</td></tr>';
    try {
        const res = await fetch(`/api/library/${encodeURIComponent(bookId)}/files`);
        const data = await res.json();
        if (!data.exists) {
            filesEl.innerHTML = '<tr><td colspan="2" class="no-results">This folder no longer exists.</td></tr>';
            return;
        }
        const total = data.files.reduce((sum, f) => sum + f.size_bytes, 0);
        pathEl.textContent = `Location: ${data.path} — ${data.files.length} audio file${data.files.length === 1 ? '' : 's'}, ${formatSize(total)}`;
        filesEl.innerHTML = data.files.length
            ? data.files.map(f => `<tr><td>${esc(f.name)}</td><td>${esc(formatSize(f.size_bytes))}</td></tr>`).join('')
            : '<tr><td colspan="2" class="no-results">No audio files found.</td></tr>';
        updateConvertButtons(bookId, data.convert);
    } catch (err) {
        filesEl.innerHTML = '<tr><td colspan="2" class="no-results">Could not load files.</td></tr>';
    }
}

function setupBookModal() {
    const msg = document.getElementById('bookStatusMsg');
    document.getElementById('closeBookModal').addEventListener('click', () => hideModal(bookModal));
    bookModal.addEventListener('click', (e) => { if (e.target === bookModal) hideModal(bookModal); });

    document.getElementById('saveBookBtn').addEventListener('click', async () => {
        const changes = {};
        for (const [elId, key] of Object.entries(BOOK_FIELDS)) {
            changes[key] = document.getElementById(elId).value.trim();
        }
        const res = await fetch(`/api/library/${encodeURIComponent(currentBookId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(changes)
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            setActionStatus(msg, data.detail || 'Save failed', 'error');
            return;
        }
        await fetchLibrary();
        renderLibrary();
        setActionStatus(msg, 'Saved', 'ok');
    });

    document.getElementById('searchNowBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        setActionStatus(msg, 'Searching AudiobookBay...');
        try {
            const res = await fetch(`/api/library/${encodeURIComponent(currentBookId)}/search`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setActionStatus(msg, data.detail || 'Search failed', 'error');
            } else if (data.grabbed) {
                setActionStatus(msg, 'Found a match and sent it to qBittorrent', 'ok');
                await fetchLibrary();
                renderLibrary();
                document.getElementById('bookStatus').value = 'Downloading';
            } else {
                setActionStatus(msg, 'No suitable release found. Try Manual Search.', 'error');
            }
        } finally {
            btn.disabled = false;
        }
    });

    document.getElementById('matchBookBtn').addEventListener('click', () => {
        const book = appLibrary.find(b => b.id === currentBookId);
        if (!book) return;
        document.getElementById('matchPanel').hidden = false;
        document.getElementById('matchQuery').value = `${String(book.title || '').split(':')[0]} ${primaryAuthor(book.authors)}`.trim();
        searchMatches();
    });
    document.getElementById('matchSearchBtn').addEventListener('click', searchMatches);
    document.getElementById('matchQuery').addEventListener('keypress', (e) => { if (e.key === 'Enter') searchMatches(); });
    document.getElementById('matchCloseBtn').addEventListener('click', () => { document.getElementById('matchPanel').hidden = true; });

    document.getElementById('bookImportAnyway').addEventListener('click', () => reviewAction(currentBookId, 'import_anyway', msg));
    document.getElementById('bookReject').addEventListener('click', () => reviewAction(currentBookId, 'reject', msg));

    document.getElementById('manualSearchBtn').addEventListener('click', () => {
        const book = appLibrary.find(b => b.id === currentBookId);
        if (!book) return;
        hideModal(bookModal);
        openModal({ ...book, imageUrl: coverUrl(book) });
    });

    document.getElementById('removeBookBtn').addEventListener('click', async () => {
        const book = appLibrary.find(b => b.id === currentBookId);
        if (!book) return;
        const confirmed = await confirmDialog(`Remove "${book.title}" from Bayarr?\n\nFiles on disk are not deleted.`,
            { title: 'Remove book', confirmText: 'Remove', danger: true });
        if (!confirmed) return;
        const res = await fetch(`/api/library/${encodeURIComponent(currentBookId)}`, { method: 'DELETE' });
        if (res.ok) {
            await fetchLibrary();
            renderLibrary();
            hideModal(bookModal);
            toast(`Removed "${book.title}"`, 'ok');
        } else {
            setActionStatus(msg, 'Remove failed', 'error');
        }
    });
}

async function searchMatches() {
    const results = document.getElementById('matchResults');
    const query = document.getElementById('matchQuery').value.trim();
    results.innerHTML = '<div class="muted">Searching Audible…</div>';
    const res = await fetch(`/api/library/${encodeURIComponent(currentBookId)}/match_candidates?q=${encodeURIComponent(query)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        results.innerHTML = `<div class="muted">${esc(data.detail || 'Search failed')}</div>`;
        return;
    }
    if (!data.candidates.length) {
        results.innerHTML = '<div class="muted">No Audible results. Try fewer words.</div>';
        return;
    }
    results.innerHTML = data.candidates.map((c, i) => {
        const runtime = c.runtime_min ? `${Math.floor(c.runtime_min / 60)}h ${c.runtime_min % 60}m` : '';
        const details = [editionOf(c) !== 'narrated' ? EDITION_LABELS[editionOf(c)] : '', c.authors, c.narrators && `read by ${c.narrators}`,
            seriesLabel(c), runtime, releaseDate(c.release_date, 4)].filter(Boolean).join(' · ');
        return `<div class="match-result">
            <img src="${esc(safeUrl(c.imageUrl, PLACEHOLDER_COVER))}" alt="">
            <div class="match-info"><b>${esc(c.title)}</b><span class="muted">${esc(details)}</span></div>
            <button class="primary-btn" data-index="${i}">Use This</button>
        </div>`;
    }).join('');
    results.querySelectorAll('button[data-index]').forEach(btn => btn.addEventListener('click', async () => {
        const chosen = data.candidates[btn.dataset.index];
        btn.disabled = true;
        const { ok, data: out } = await postJSON(`/api/library/${encodeURIComponent(currentBookId)}/match`, { asin: chosen.asin });
        if (!ok) {
            toast(out.detail || 'Match failed', 'error');
            btn.disabled = false;
            return;
        }
        await fetchLibrary();
        renderLibrary();
        await openBookModal(currentBookId);
        toast(`Matched to "${chosen.title}" on Audible`, 'ok');
    }));
}

// -----------------
// IMPORT EXISTING LIBRARY
// -----------------
const importModal = document.getElementById('importModal');
let importBooks = [];
// By folder: the Audible match ({kind: 'audible', ...}, or {kind: 'as_is'} for none), rows being
// looked up, where each would go, and import results
let importMatches = {};
let importLooking = new Set();
let importDest = {};
let importDone = {};
let importRun = 0;

const IMPORT_STATES = {
    new: ['new', 'New'],
    link: ['link', 'Link to library'],
    in_library: ['owned', 'In library'],
};

function importMode() {
    return document.getElementById('importMode').value;
}

function importAudibleHtml(b, i) {
    if (b.state === 'in_library') return '<span class="muted">—</span>';
    const m = importMatches[b.path];
    const change = `<button class="link-btn import-choose" data-index="${i}">${m && m.kind === 'audible' ? 'Change' : 'Choose…'}</button>`;
    if (!m) return importLooking.has(b.path) ? '<span class="muted">Looking…</span>' : `<span class="muted">Not matched</span> ${change}`;
    if (m.kind === 'as_is') return `<span class="muted">As named</span> ${change}`;
    const sub = [m.series ? `${m.series}${m.sequence ? ' #' + m.sequence : ''}` : '', m.asin].filter(Boolean).join(' · ');
    return `${esc(m.title)} ${m.edition && m.edition !== 'narrated' ? `<span class="edition-badge edition-${esc(m.edition)}">${esc(EDITION_LABELS[m.edition] || m.edition)}</span>` : ''}
        ${change}<span class="book-sub">${esc(sub)}</span>`;
}

function importDestHtml(b) {
    if (b.state === 'in_library') return '<span class="muted">Already in your library</span>';
    if (importMode() === 'in_place') return '<span class="muted">Stays where it is</span>';
    const d = importDest[b.path];
    if (!d) return '<span class="muted">…</span>';
    if (d.error) return `<span class="muted">${esc(d.error)}</span>`;
    const files = d.files.slice(0, 2).join(', ') + (d.files.length > 2 ? ` +${d.files.length - 2} more` : '');
    return `<div class="dest-folder">${esc(d.folder)}/</div><span class="book-sub">${esc(files)}${d.exists ? ' · the folder exists; files are added to it' : ''}</span>`;
}

function importStatusHtml(b) {
    const r = importDone[b.path];
    if (r) return r.ok ? '<span class="result-ok">Imported</span>' + (r.message ? `<span class="book-sub">${esc(r.message)}</span>` : '')
        : `<span class="result-error">Failed</span><span class="book-sub">${esc(r.message)}</span>`;
    const [badgeClass, label] = IMPORT_STATES[b.state];
    const hint = b.state === 'link' ? ` title="Will link to '${esc(b.match_title)}'"` : '';
    return `<span class="badge ${badgeClass}"${hint}>${esc(label)}</span>`;
}

function renderImportResults() {
    const tbody = document.getElementById('importResults');
    const summary = document.getElementById('importSummary');
    const counts = { new: 0, link: 0, in_library: 0 };
    importBooks.forEach(b => counts[b.state]++);
    const toCheck = importBooks.filter(b => b.edition_check && b.state !== 'in_library').length;
    summary.textContent = `${importBooks.length} books found: ${counts.new} new, ${counts.link} already tracked (will be linked to their files), ${counts.in_library} already imported.`
        + (toCheck ? ` ${toCheck} edition${toCheck === 1 ? '' : 's'} to check (highlighted).` : '');

    tbody.innerHTML = importBooks.map((b, i) => {
        const sub = [b.authors, seriesLabel(b), `from the ${b.source}`].filter(Boolean).join(' · ');
        return `<tr data-index="${i}">
            <td><input type="checkbox" class="import-check" data-index="${i}" ${b.state === 'in_library' || importDone[b.path]?.ok ? 'disabled' : 'checked'}></td>
            <td title="${esc(b.path)}">${esc(b.title)}<span class="book-sub">${esc(sub)}</span></td>
            <td><select class="form-select import-edition${b.edition_check ? ' check' : ''}" data-index="${i}" title="${esc(b.edition_reason || '')}"${b.state === 'in_library' ? ' disabled' : ''}>
                ${Object.entries(EDITION_LABELS).map(([v, l]) => `<option value="${v}"${editionOf(b) === v ? ' selected' : ''}>${l}</option>`).join('')}
            </select></td>
            <td class="import-audible">${importAudibleHtml(b, i)}</td>
            <td class="import-dest">${importDestHtml(b)}</td>
            <td class="nowrap">${esc(formatSize(b.size_bytes))}<span class="book-sub">${esc(b.format)}</span></td>
            <td class="import-status">${importStatusHtml(b)}</td>
        </tr>`;
    }).join('') || '<tr><td colspan="7" class="no-results">No audiobooks found in this folder.</td></tr>';

    tbody.querySelectorAll('.import-check').forEach(cb => cb.addEventListener('change', updateImportButton));
    tbody.querySelectorAll('.import-edition').forEach(sel => sel.addEventListener('change', () => {
        // Another edition: look it up again
        const b = importBooks[sel.dataset.index];
        delete importMatches[b.path];
        delete importDest[b.path];
        updateImportRow(+sel.dataset.index);
        lookUpImportRows([+sel.dataset.index]);
    }));
    tbody.querySelectorAll('.import-choose').forEach(btn => btn.addEventListener('click', () => chooseImportMatch(+btn.dataset.index)));
    document.getElementById('importSelectAll').checked = counts.new + counts.link > 0;
    updateImportButton();
}

// Redraws one row's Audible, destination and status cells
function updateImportRow(i) {
    const row = document.querySelector(`#importResults tr[data-index="${i}"]`);
    if (!row) return;
    const b = importBooks[i];
    row.querySelector('.import-audible').innerHTML = importAudibleHtml(b, i);
    row.querySelector('.import-dest').innerHTML = importDestHtml(b);
    row.querySelector('.import-status').innerHTML = importStatusHtml(b);
    const choose = row.querySelector('.import-choose');
    if (choose) choose.addEventListener('click', () => chooseImportMatch(i));
}

function importEditionOf(i) {
    const sel = document.querySelector(`.import-edition[data-index="${i}"]`);
    return sel ? sel.value : editionOf(importBooks[i]);
}

function chooseImportMatch(i) {
    const b = importBooks[i];
    openMatchChooser(b.title, b, false, choice => {
        importMatches[b.path] = choice;
        delete importDest[b.path];
        updateImportRow(i);
        lookUpImportRows([i], true);
    });
}

// Audible matches (when not chosen yet) and destinations, one row at a time
async function lookUpImportRows(indexes, destOnly = false) {
    const run = importRun;
    for (const i of indexes) {
        const b = importBooks[i];
        if (run !== importRun) return;  // Scanned again
        if (!b || b.state === 'in_library') continue;
        if (!destOnly && !importMatches[b.path]) {
            importLooking.add(b.path);
            updateImportRow(i);
            const { ok, data } = await postJSON('/api/library/scan/suggest', { path: b.path, edition: importEditionOf(i) });
            importLooking.delete(b.path);
            if (run !== importRun) return;
            if (ok && data.match && !importMatches[b.path]) importMatches[b.path] = audibleChoice(data.match);
            updateImportRow(i);
        }
        await fetchImportDest(i, run);
    }
}

async function fetchImportDest(i, run = importRun) {
    const b = importBooks[i];
    if (importMode() === 'in_place' || importDest[b.path]) return;
    const m = importMatches[b.path];
    const { ok, data } = await postJSON('/api/library/scan/preview',
        { path: b.path, edition: importEditionOf(i), asin: m && m.kind === 'audible' ? m.asin : '' });
    if (run !== importRun) return;
    importDest[b.path] = ok ? data : { error: data.detail || 'Unknown' };
    updateImportRow(i);
}

function updateImportModeHint() {
    const mode = importMode();
    document.getElementById('importModeHint').textContent = mode === 'in_place'
        ? 'Books are added where they are; nothing is copied or renamed.'
        : mode === 'copy' ? 'Files are renamed into your Book Folder Format; the originals stay (and keep seeding).'
            : 'Files are renamed into your Book Folder Format; the originals are deleted afterwards.';
}

function selectedImportPaths() {
    return [...document.querySelectorAll('.import-check:checked')].map(cb => importBooks[cb.dataset.index].path);
}

// Editions as shown in the preview (possibly changed), by folder
function importEditions() {
    const chosen = {};
    document.querySelectorAll('.import-edition').forEach(sel => { chosen[importBooks[sel.dataset.index].path] = sel.value; });
    return chosen;
}

function updateImportButton() {
    const n = selectedImportPaths().length;
    const btn = document.getElementById('importSelectedBtn');
    btn.disabled = n === 0;
    btn.textContent = n ? `Import ${n} Book${n === 1 ? '' : 's'}` : 'Import Selected';
}

async function scanForImport() {
    const loader = document.getElementById('importLoader');
    const msg = document.getElementById('importStatusMsg');
    const path = document.getElementById('importPath').value.trim();
    importBooks = [];
    importMatches = {}; importLooking = new Set(); importDest = {}; importDone = {};
    importRun++;
    document.getElementById('importResults').innerHTML = '';
    document.getElementById('importSummary').textContent = '';
    setActionStatus(msg, '');
    updateImportButton();
    loader.classList.remove('hidden');
    try {
        const res = await fetch('/api/library/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            setActionStatus(msg, data.detail || 'Scan failed', 'error');
            return;
        }
        document.getElementById('importPath').value = data.root;
        importBooks = data.books;
        // Books outside the Root Folder are brought into it; your library stays where it is
        document.getElementById('importMode').value = importBooks.length && importBooks.every(b => b.in_root) ? 'in_place' : 'copy';
        updateImportModeHint();
        renderImportResults();
        lookUpImportRows(importBooks.map((_, i) => i));
    } finally {
        loader.classList.add('hidden');
    }
}

function setupImportModal() {
    document.getElementById('openImportBtn').addEventListener('click', () => {
        const pathInput = document.getElementById('importPath');
        if (!pathInput.value) pathInput.value = appSettings.root_folder || '';
        showModal(importModal);
    });
    document.getElementById('closeImportModal').addEventListener('click', () => hideModal(importModal));
    importModal.addEventListener('click', (e) => { if (e.target === importModal) hideModal(importModal); });
    document.getElementById('scanBtn').addEventListener('click', scanForImport);
    document.getElementById('importPath').addEventListener('keypress', (e) => { if (e.key === 'Enter') scanForImport(); });

    document.getElementById('importMode').addEventListener('change', () => {
        updateImportModeHint();
        importBooks.forEach((_, i) => updateImportRow(i));
        if (importMode() !== 'in_place') lookUpImportRows(importBooks.map((_, i) => i), true);
    });

    document.getElementById('importSelectAll').addEventListener('change', (e) => {
        document.querySelectorAll('.import-check:not(:disabled)').forEach(cb => { cb.checked = e.target.checked; });
        updateImportButton();
    });

    document.getElementById('importSelectedBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        const msg = document.getElementById('importStatusMsg');
        const mode = importMode();
        const paths = selectedImportPaths();
        if (mode === 'move' && !await confirmDialog(`Move ${paths.length} book${paths.length === 1 ? '' : 's'} into the Root Folder? The originals are deleted afterwards, so a torrent of them stops seeding.`,
            { title: 'Move files?', confirmText: 'Move', danger: true })) return;
        btn.disabled = true;
        setActionStatus(msg, 'Importing...');
        const matches = {};
        paths.forEach(p => { const m = importMatches[p]; if (m && m.kind === 'audible') matches[p] = m.asin; });
        const { ok, data } = await postJSON('/api/library/import', { paths, editions: importEditions(), matches, mode });
        if (!ok) {
            setActionStatus(msg, data.detail || 'Import failed', 'error');
            btn.disabled = false;
            return;
        }
        if (data.job) {
            // Copying or moving runs in the background, like Manual Import
            let st = data.job;
            while (st.running) {
                setActionStatus(msg, `Importing ${Math.min(st.done + 1, st.total)} of ${st.total}…`);
                await new Promise(r => setTimeout(r, 1000));
                st = await fetch('/api/manual_import/status').then(r => r.json()).catch(() => st);
            }
            st.results.forEach(r => { importDone[r.path] = r; });
            await fetchLibrary();
            renderLibrary();
            renderImportResults();
            setActionStatus(msg, `${st.imported} imported${st.failed ? `, ${st.failed} failed` : ''}`, st.failed ? 'error' : 'ok');
            return;
        }
        await fetchLibrary();
        renderLibrary();
        await scanForImport();
        setActionStatus(msg, `Imported ${data.added} new book${data.added === 1 ? '' : 's'}${data.linked ? `, linked ${data.linked} tracked book${data.linked === 1 ? '' : 's'} to their files` : ''}.`, 'ok');
    });
}

// -----------------
// NEEDS REVIEW ACTIONS
// -----------------
async function reviewAction(bookId, action, msgEl) {
    const book = appLibrary.find(b => b.id === bookId);
    if (action === 'reject') {
        const confirmed = await confirmDialog(
            `Reject this download of "${book ? book.title : 'this book'}"?\n\nThe release won't be grabbed again for this book and a new search starts. The torrent stays in qBittorrent for you to remove.`,
            { title: 'Reject download', confirmText: 'Reject', danger: true });
        if (!confirmed) return;
    }
    const res = await fetch(`/api/library/${encodeURIComponent(bookId)}/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const data = await res.json().catch(() => ({}));
    await fetchLibrary();
    renderLibrary();
    if (!msgEl) {
        toast(res.ok ? (action === 'reject' ? 'Rejected; searching for another release' : 'Approved; it will be imported within a minute') : (data.detail || 'Failed'), res.ok ? 'ok' : 'error');
    } else {
        setActionStatus(msgEl, res.ok ? (action === 'reject' ? 'Rejected; searching for another release' : 'Will be imported within a minute') : (data.detail || 'Failed'), res.ok ? 'ok' : 'error');
        const book2 = appLibrary.find(b => b.id === bookId);
        document.getElementById('bookReview').hidden = !book2 || book2.status !== 'Needs Review';
        if (book2) document.getElementById('bookStatus').value = book2.status;
    }
    if (!document.getElementById('activityView').hidden) renderActivity();
}

function updateActivityBadge() {
    const badge = document.getElementById('activityBadge');
    const n = appLibrary.filter(b => b.status === 'Needs Review').length;
    badge.hidden = n === 0;
    badge.textContent = n;
    badge.title = `${n} download${n === 1 ? '' : 's'} need${n === 1 ? 's' : ''} review`;
}

// -----------------
// SERIES
// -----------------
async function fetchSeries() {
    try {
        const res = await fetch('/api/series');
        appSeries = (await res.json()).series || [];
    } catch (err) {
        console.error('Failed to load series', err);
    }
}

// "The Name" and "Name Series" are the same series (matches the server)
function seriesKey(name) {
    return normKey(String(name || '').replace(/\s+series$/i, ''));
}

function seriesEntries(book) {
    if (Array.isArray(book.series_list) && book.series_list.length) return book.series_list;
    return book.series ? [{ name: book.series, asin: book.series_asin || '', sequence: book.sequence || '' }] : [];
}

function seriesPageKey(entry, book) {
    const key = entry.asin ? 'asin:' + entry.asin : 'name:' + seriesKey(entry.name);
    return book && editionOf(book) === 'dramatized' ? key + '~dramatized' : key;
}

// Book details: every series the book is in, each opening its series page
function renderBookSeriesLinks(book) {
    const box = document.getElementById('bookSeriesLinks');
    const entries = seriesEntries(book);
    const people = String(book.authors || '').split(',').map(a => a.trim()).filter(Boolean);
    box.hidden = !entries.length && !people.length;
    box.innerHTML = (people.length ? `<span class="muted">Author:</span> ` + people.map(a =>
        `<button class="series-chip author-chip" data-author="${esc(a)}">${esc(a)}</button>`).join('') + ' ' : '')
        + (entries.length ? `<span class="muted">Series:</span> ` + entries.map((e, i) =>
        `<button class="series-chip" data-index="${i}">${esc(e.name)}${e.sequence ? ' #' + esc(e.sequence) : ''}</button>`).join('') : '');
    box.querySelectorAll('.author-chip').forEach(chip => chip.addEventListener('click', () => {
        hideModal(bookModal);
        openAuthorPage(chip.dataset.author);
    }));
    box.querySelectorAll('.series-chip:not(.author-chip)').forEach(chip => chip.addEventListener('click', () => {
        hideModal(bookModal);
        openSeriesDetail(seriesPageKey(entries[chip.dataset.index], book));
    }));
}

function showView(viewId, navView) {
    views.forEach(v => { v.hidden = v.id !== viewId; });
    navItems.forEach(n => n.classList.toggle('active', n.dataset.view === navView));
    clearInterval(activityTimer);
    window.scrollTo(0, 0);
}

let seriesIndexData = [];
let seriesViewMode = 'posters';
try { seriesViewMode = localStorage.getItem('bayarr.seriesView') || 'posters'; } catch (e) { /* storage unavailable */ }
let seriesLookupTimer = null;
let seriesLookupStartedThisVisit = false;

function seriesProgress(sr) {
    const total = sr.total ?? sr.in_library;
    const pct = total ? Math.round(sr.owned / total * 100) : 0;
    const complete = sr.total != null && sr.owned >= sr.total && sr.total > 0;
    const kind = complete ? 'complete' : sr.monitored ? 'monitored' : 'unmonitored';
    const label = sr.total == null ? `${sr.owned} / …` : `${sr.owned} / ${sr.total}`;
    return `<div class="series-bar ${kind}" title="${sr.owned} on disk${sr.total != null ? ` of ${sr.total} books` : ''}">
        <div class="series-bar-fill" style="width: ${pct}%"></div><span>${esc(label)}</span></div>`;
}

function seriesBookmark(sr) {
    return `<span class="bookmark ${sr.monitored ? 'on' : ''}" title="${sr.monitored ? 'Monitored' : 'Not monitored'}">
        <svg viewBox="0 0 24 24" width="16" height="16"><path d="M6 3h12v18l-6-4-6 4z"></path></svg></span>`;
}

function filteredSeries() {
    const text = document.getElementById('seriesFilter').value.trim().toLowerCase();
    const show = document.getElementById('seriesShow').value;
    const sort = document.getElementById('seriesSort').value;
    let list = seriesIndexData.filter(sr => {
        if (text && !`${sr.title} ${sr.author}`.toLowerCase().includes(text)) return false;
        if (show === 'monitored') return sr.monitored;
        if (show === 'unmonitored') return !sr.monitored;
        if (show === 'missing') return (sr.missing ?? 0) > 0;
        if (show === 'complete') return sr.total != null && sr.owned >= sr.total;
        return true;
    });
    const byTitle = (a, b) => normKey(a.title).localeCompare(normKey(b.title));
    const sorters = {
        title: byTitle,
        author: (a, b) => normKey(a.author).localeCompare(normKey(b.author)) || byTitle(a, b),
        missing: (a, b) => (b.missing ?? -1) - (a.missing ?? -1) || byTitle(a, b),
        latest: (a, b) => String(b.latest || '').localeCompare(String(a.latest || '')) || byTitle(a, b),
    };
    return list.sort(sorters[sort] || byTitle);
}

async function renderSeries() {
    let data;
    try {
        data = await fetch('/api/series/index').then(r => r.json());
    } catch (err) {
        console.error('Failed to load series', err);
        return;
    }
    seriesIndexData = data.series || [];
    const monitored = seriesIndexData.filter(sr => sr.monitored).length;
    document.getElementById('seriesStats').textContent = `${seriesIndexData.length} series · ${monitored} monitored`;
    document.querySelectorAll('[data-series-view]').forEach(b => b.classList.toggle('active', b.dataset.seriesView === seriesViewMode));
    drawSeriesList();
    updateSeriesLookup(data.refresh);

    // Totals come from Audible: look up series that haven't been yet, once per visit
    if (!seriesLookupStartedThisVisit && !data.refresh.running && seriesIndexData.some(sr => !sr.catalog_loaded)) {
        seriesLookupStartedThisVisit = true;
        const res = await postJSON('/api/series/refresh');
        updateSeriesLookup(res.data);
    }
}

function updateSeriesLookup(job) {
    const banner = document.getElementById('seriesLookup');
    clearTimeout(seriesLookupTimer);
    if (job && job.running) {
        banner.hidden = false;
        banner.textContent = `Looking up series on Audible… ${job.done} of ${job.total}`;
        seriesLookupTimer = setTimeout(() => {
            if (!document.getElementById('seriesView').hidden) renderSeries();
        }, 3000);
    } else {
        banner.hidden = true;
    }
}

function drawSeriesList() {
    const container = document.getElementById('seriesContainer');
    const list = filteredSeries();
    if (!seriesIndexData.length) {
        container.innerHTML = `<div class="empty-state"><h3>No series yet</h3>
            <p>Series show up here once books that belong to one are in your library, or when you open a series from Search results.</p></div>`;
        return;
    }
    if (!list.length) {
        // This page only has series you own books in; others are found by searching Audible
        const text = document.getElementById('seriesFilter').value.trim();
        container.innerHTML = `<div class="empty-state"><h3>No series match</h3>
            <p>${text ? 'None of your series match. Series you have no books from are found on Audible.' : 'Nothing matches this filter.'}</p>
            ${text ? `<button class="primary-btn" id="seriesSearchAudible">Search Audible for "${esc(text)}"</button>` : ''}</div>`;
        document.getElementById('seriesSearchAudible')?.addEventListener('click', () => navigate('/search/' + encodeURIComponent(text)));
        return;
    }
    if (seriesViewMode === 'table') {
        container.innerHTML = `<div class="table-container series-table"><table class="data-table">
            <thead><tr><th class="col-check"></th><th>Series</th><th>Author</th><th class="col-progress">Books</th><th class="col-seeds">Missing</th><th class="col-when">Latest release</th></tr></thead>
            <tbody>${list.map(sr => `<tr class="clickable" data-key="${esc(sr.key)}">
                <td>${seriesBookmark(sr)}</td>
                <td><b>${esc(sr.title)}</b></td>
                <td class="muted">${esc(sr.author || '')}</td>
                <td>${seriesProgress(sr)}</td>
                <td>${sr.missing == null ? '<span class="muted">…</span>' : esc(sr.missing)}</td>
                <td class="muted">${esc(releaseDate(sr.latest))}</td>
            </tr>`).join('')}</tbody></table></div>`;
        container.querySelectorAll('tr[data-key]').forEach(tr => tr.addEventListener('click', () => openSeriesDetail(tr.dataset.key)));
        return;
    }
    container.innerHTML = `<div class="results-grid series-grid">${list.map(sr => `
        <div class="series-poster" data-key="${esc(sr.key)}" tabindex="0" role="button" aria-label="${esc(sr.title)}">
            <div class="poster-art">
                <img src="${esc(safeUrl(sr.cover, PLACEHOLDER_COVER))}" alt="" loading="lazy">
                ${seriesBookmark(sr)}
            </div>
            ${seriesProgress(sr)}
            <div class="poster-info">
                <div class="book-title" title="${esc(sr.title)}">${esc(sr.title)}</div>
                <div class="book-author">${esc(sr.author || '')}</div>
            </div>
        </div>`).join('')}</div>`;
    container.querySelectorAll('.series-poster').forEach(card => {
        card.querySelector('img').addEventListener('error', e => { e.target.src = PLACEHOLDER_COVER; }, { once: true });
        card.addEventListener('click', () => openSeriesDetail(card.dataset.key));
        card.addEventListener('keypress', e => { if (e.key === 'Enter') openSeriesDetail(card.dataset.key); });
    });
}

// ---- Series details page ----
let currentSeries = null;

function formatRuntime(min) {
    if (!min) return '';
    return `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, '0')}m`;
}

function openSeriesDetail(key) {
    navigate('/series/' + encodeURIComponent(key));
}

async function showSeriesDetail(key) {
    showView('seriesDetailView', 'seriesView');
    const box = document.getElementById('seriesDetail');
    box.innerHTML = '<div class="loader"><div class="spinner"></div></div>';
    const res = await fetch(`/api/series/detail?key=${encodeURIComponent(key)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        box.innerHTML = `<div class="empty-state"><h3>Couldn't load this series</h3><p>${esc(data.detail || 'Try again in a moment.')}</p></div>`;
        return;
    }
    currentSeries = data;
    await Promise.all([fetchLibrary(), fetchSeries()]);
    renderSeriesDetail();
}

// Reloads the open series page after a change, in place: no spinner, same scroll position
async function refreshSeriesDetail() {
    const main = document.querySelector('.main-content');
    const scroll = { window: window.scrollY, main: main.scrollTop };
    const res = await fetch(`/api/series/detail?key=${encodeURIComponent(currentSeries.key)}`);
    if (!res.ok) return;
    currentSeries = await res.json();
    await Promise.all([fetchLibrary(), fetchSeries()]);
    renderSeriesDetail();
    main.scrollTop = scroll.main;
    window.scrollTo(0, scroll.window);
}

function seriesRowHtml(r, src, i) {
    const other = r.other
        ? ` <span class="muted" title="You have the ${esc(EDITION_LABELS[r.other.edition] || '')} edition">· ${esc(EDITION_LABELS[r.other.edition] || '')}: ${esc(r.other.status)}</span>` : '';
    const status = r.book_id
        ? `<span class="library-status ${esc(statusClass(r.status))} inline-status">${esc(r.status)}</span>`
        : `<span class="muted">Not in library</span> <button class="link-btn add-row" data-src="${src}" data-index="${i}">Add</button>${other}`;
    return `<tr class="${r.book_id ? 'clickable' : 'not-owned'}" data-src="${src}" data-index="${i}">
        <td class="muted">${esc(r.sequence)}</td>
        <td>${bookLink(r.book_id, r.title)} ${editionOf(r) !== 'narrated' ? `<span class="edition-badge edition-${editionOf(r)}">${EDITION_LABELS[editionOf(r)]}</span>` : ''}</td>
        <td class="muted" title="${esc(r.narrators || '')}">${esc(shortNames(r.narrators))}</td>
        <td class="muted nowrap">${esc(releaseDate(r.release_date))}</td>
        <td class="muted nowrap">${esc(formatRuntime(r.runtime_min))}</td>
        <td class="nowrap">${status}</td>
    </tr>`;
}

// "A, B, C, D, E" -> "A, B +3 more" (full-cast productions list dozens of narrators)
function shortNames(names) {
    const list = String(names || '').split(',').map(n => n.trim()).filter(n => n && !/full[\s-]?cast/i.test(n));
    return list.length > 3 ? `${list.slice(0, 2).join(', ')} +${list.length - 2} more` : list.join(', ');
}

function renderSeriesDetail() {
    const sr = currentSeries;
    const box = document.getElementById('seriesDetail');
    const missing = sr.rows.filter(r => !r.book_id);
    const tracked = sr.series_id ? appSeries.find(x => x.id === sr.series_id) : null;
    const actions = sr.asin ? (tracked
        ? `<label class="checkbox-label"><input type="checkbox" id="seriesMonitoredToggle" ${tracked.monitored ? 'checked' : ''}> Monitored</label>
           <button class="secondary-btn" id="seriesSyncBtn">Sync</button>
           <button class="danger-btn" id="seriesRemoveBtn">Stop Tracking</button>`
        : `<button class="primary-btn" id="seriesMonitorOpen">Monitor Series</button>`) : '';
    box.innerHTML = `
        <div class="series-hero">
            <img src="${esc(safeUrl(sr.cover, PLACEHOLDER_COVER))}" alt="" class="series-hero-cover">
            <div class="series-hero-info">
                <h2>${esc(sr.title)}</h2>
                <div class="muted">${sr.author ? `<button class="link-btn author-link" data-author="${esc(sr.author)}">${esc(sr.author)}</button>` : ''}</div>
                <div class="series-hero-stats">
                    ${seriesProgress(sr)}
                    <span class="stat"><b>${sr.owned}</b> on disk</span>
                    ${sr.wanted ? `<span class="stat"><b>${sr.wanted}</b> wanted</span>` : ''}
                    ${sr.missing != null ? `<span class="stat"><b>${sr.missing}</b> not in library</span>` : ''}
                    ${tracked ? `<span class="stat">${tracked.monitored ? 'Monitored' : 'Paused'}${tracked.last_sync ? ' · synced ' + esc(new Date(tracked.last_sync).toLocaleDateString()) : ''}</span>` : ''}
                </div>
                <div class="series-hero-actions">${actions}</div>
            </div>
        </div>
        ${(sr.editions || []).length ? `<div class="edition-tabs" role="tablist">${sr.editions.map(ed =>
            `<button class="edition-tab${ed.active ? ' active' : ''}" role="tab" aria-selected="${ed.active}" data-key="${esc(ed.key)}">${esc(ed.label)}</button>`).join('')}</div>` : ''}
        ${sr.unresolved ? '<p class="lookup-banner">Couldn\'t find this series on Audible, so only the books in your library are shown. Match one of its books on Audible, then open this page again.</p>' : ''}
        <div class="table-container series-books-table"><table class="data-table">
            <thead><tr><th class="col-num">#</th><th>Title</th><th>Narrator</th><th class="col-date">Released</th><th class="col-len">Length</th><th class="col-status">Status</th></tr></thead>
            <tbody>${sr.rows.map((r, i) => seriesRowHtml(r, 'rows', i)).join('')}</tbody></table></div>
        ${(sr.alternates || []).length ? `<details class="other-editions"${(sr.alternates || []).some(a => a.book_id) ? ' open' : ''}>
            <summary>Abridged editions (${sr.alternates.length})</summary>
            <div class="table-container series-books-table"><table class="data-table">
                <thead><tr><th class="col-num">#</th><th>Title</th><th>Narrator</th><th class="col-date">Released</th><th class="col-len">Length</th><th class="col-status">Status</th></tr></thead>
                <tbody>${sr.alternates.map((r, i) => seriesRowHtml(r, 'alternates', i)).join('')}</tbody></table></div>
        </details>` : ''}`;

    const rowOf = el => sr[el.dataset.src][el.dataset.index];
    box.querySelectorAll('.edition-tab:not(.active)').forEach(tab => tab.addEventListener('click', () => openSeriesDetail(tab.dataset.key)));
    box.querySelectorAll('tr.clickable').forEach(tr => tr.addEventListener('click', () => openBookModal(rowOf(tr).book_id)));
    box.querySelectorAll('.add-row').forEach(btn => btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const row = rowOf(btn);
        btn.disabled = true;
        const res = await postJSON('/api/library', row.catalog);
        if (!res.ok) {
            toast(res.data.detail || 'Could not add the book', 'error');
            btn.disabled = false;
            return;
        }
        toast(`Added "${row.title}" (${res.data.status})${res.data.status === 'Monitored' && appSettings.qbt_enabled ? '; searching now' : ''}`, 'ok');
        refreshSeriesDetail();
    }));

    const monitorOpen = document.getElementById('seriesMonitorOpen');
    if (monitorOpen) monitorOpen.addEventListener('click', () => openMonitorDialog(sr.monitor_candidates || missing));
    const toggle = document.getElementById('seriesMonitoredToggle');
    if (toggle) toggle.addEventListener('change', async () => {
        await postJSON(`/api/series/${encodeURIComponent(tracked.id)}`, { monitored: toggle.checked }, 'PATCH');
        toast(toggle.checked ? 'Monitoring resumed' : 'Monitoring paused: new releases won\'t be added', 'ok');
        await fetchSeries();
    });
    const syncBtn = document.getElementById('seriesSyncBtn');
    if (syncBtn) syncBtn.addEventListener('click', async () => {
        syncBtn.disabled = true;
        const { ok, data } = await postJSON(`/api/series/${encodeURIComponent(tracked.id)}/sync`);
        toast(ok ? `${data.added} new book${data.added === 1 ? '' : 's'} added` : (data.detail || 'Sync failed'), ok ? 'ok' : 'error');
        refreshSeriesDetail();
    });
    const removeBtn = document.getElementById('seriesRemoveBtn');
    if (removeBtn) removeBtn.addEventListener('click', async () => {
        const confirmed = await confirmDialog(`Stop tracking "${sr.title}"?\n\nNew releases won't be added any more. Books already in your library stay.`,
            { title: 'Stop tracking series', confirmText: 'Stop Tracking', danger: true });
        if (!confirmed) return;
        await fetch(`/api/series/${encodeURIComponent(tracked.id)}`, { method: 'DELETE' });
        refreshSeriesDetail();
    });
}

// ---- Monitor dialog: choose which missing books to add ----
const seriesModal = document.getElementById('seriesModal');

function openMonitorDialog(missingRows) {
    document.getElementById('seriesModalName').textContent = currentSeries.title;
    setActionStatus(document.getElementById('seriesModalStatus'), '');
    const list = document.getElementById('seriesPickList');
    list.innerHTML = missingRows.length ? missingRows.map(r => `
        <label class="pick-row">
            <input type="checkbox" value="${esc(r.asin)}" ${r.other ? '' : 'checked'}>
            <span class="muted pick-seq">${esc(r.sequence ? '#' + r.sequence : '')}</span>
            <span class="pick-title">${esc(r.title)}${editionOf(r) !== 'narrated' ? ` <span class="edition-badge edition-${editionOf(r)}">${EDITION_LABELS[editionOf(r)]}</span>` : ''}${r.other ? ` <span class="muted">(you have the ${esc((EDITION_LABELS[r.other.edition] || '').toLowerCase())} edition)</span>` : ''}</span>
            <span class="muted">${esc(releaseDate(r.release_date, 4))}</span>
        </label>`).join('') : '<p class="muted">You already have every book in this series (in the editions your settings ask for).</p>';
    list.querySelectorAll('input').forEach(cb => cb.addEventListener('change', updateMonitorButton));
    updateMonitorButton();
    showModal(seriesModal);
}

function pickedAsins() {
    return [...document.querySelectorAll('#seriesPickList input:checked')].map(cb => cb.value);
}

function updateMonitorButton() {
    const n = pickedAsins().length;
    document.getElementById('seriesMonitorBtn').textContent = n ? `Monitor & Add ${n} Book${n === 1 ? '' : 's'}` : 'Monitor (New Releases Only)';
}

function setupSeriesPages() {
    document.getElementById('seriesFilter').addEventListener('input', drawSeriesList);
    document.getElementById('seriesShow').addEventListener('change', drawSeriesList);
    document.getElementById('seriesSort').addEventListener('change', drawSeriesList);
    document.querySelectorAll('[data-series-view]').forEach(btn => btn.addEventListener('click', () => {
        seriesViewMode = btn.dataset.seriesView;
        try { localStorage.setItem('bayarr.seriesView', seriesViewMode); } catch (e) { /* storage unavailable */ }
        document.querySelectorAll('[data-series-view]').forEach(b => b.classList.toggle('active', b === btn));
        drawSeriesList();
    }));
    document.getElementById('seriesRefreshBtn').addEventListener('click', async () => {
        const { data } = await postJSON('/api/series/refresh');
        updateSeriesLookup(data);
        toast('Looking up series on Audible in the background');
    });
    document.getElementById('seriesBackBtn').addEventListener('click', () => navigate('/series'));

    document.getElementById('closeSeriesModal').addEventListener('click', () => hideModal(seriesModal));
    seriesModal.addEventListener('click', (e) => { if (e.target === seriesModal) hideModal(seriesModal); });
    document.getElementById('seriesPickAll').addEventListener('click', () => {
        document.querySelectorAll('#seriesPickList input').forEach(cb => { cb.checked = true; });
        updateMonitorButton();
    });
    document.getElementById('seriesPickNone').addEventListener('click', () => {
        document.querySelectorAll('#seriesPickList input').forEach(cb => { cb.checked = false; });
        updateMonitorButton();
    });
    document.getElementById('seriesMonitorBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        setActionStatus(document.getElementById('seriesModalStatus'), 'Adding…');
        const { ok, data } = await postJSON('/api/series', {
            series_asin: currentSeries.asin, title: currentSeries.title, author: currentSeries.author, add_asins: pickedAsins(),
        });
        btn.disabled = false;
        if (!ok) {
            setActionStatus(document.getElementById('seriesModalStatus'), data.detail || 'Failed', 'error');
            return;
        }
        hideModal(seriesModal);
        toast(`Monitoring ${data.series.title}${data.added ? `: added ${data.added} book${data.added === 1 ? '' : 's'}` : ''}`, 'ok');
        if (currentSeries.key === 'asin:' + currentSeries.asin) {
            refreshSeriesDetail();
        } else {
            openSeriesDetail('asin:' + currentSeries.asin);
        }
    });
}

// -----------------
// ACTIVITY: QUEUE AND HISTORY
// -----------------
const EVENT_LABELS = {
    grabbed: 'Grabbed', imported: 'Imported', needs_review: 'Needs review', approved: 'Approved',
    rejected: 'Rejected', failed: 'Failed', missing: 'Missing', series: 'Series', released: 'Released',
};

// -----------------
// MANUAL IMPORT
// -----------------
// Items from the last scan, and the book chosen for each: {kind: 'library', book_id, title},
// {kind: 'audible', asin, title, authors}, or {kind: 'as_is'}
let manualItems = [];
let manualChoices = {};
let manualChecked = new Set();
let manualResults = {};
let manualSuggestRun = 0;
// The open "Choose the Book" dialog: {library: show library books, onChoose(choice)}
let matchChooser = null;

function openManualImport() {
    const input = document.getElementById('manualImportPath');
    if (!input.value) input.value = appSettings.downloads_folder || '';
    if (!manualItems.length && input.value) scanManualImport();
}

function setupManualImport() {
    document.getElementById('manualImportOpen').addEventListener('click', () => navigate('/import'));
    document.getElementById('manualImportScan').addEventListener('click', scanManualImport);
    document.getElementById('manualImportPath').addEventListener('keypress', e => { if (e.key === 'Enter') scanManualImport(); });
    document.getElementById('manualImportAll').addEventListener('change', e => {
        manualItems.forEach(it => {
            if (e.target.checked && manualChoices[it.id] && !manualResults[it.id]?.ok) manualChecked.add(it.id);
            else manualChecked.delete(it.id);
        });
        drawManualImport();
    });
    document.getElementById('manualImportGo').addEventListener('click', runManualImport);
    document.getElementById('manualImportMode').addEventListener('change', e => {
        if (e.target.value === 'move') toast('Move deletes the originals after importing: a torrent of them stops seeding', 'error');
    });
    const modal = document.getElementById('manualMatchModal');
    document.getElementById('closeManualMatch').addEventListener('click', () => hideModal(modal));
    modal.addEventListener('click', e => { if (e.target === modal) hideModal(modal); });
    document.getElementById('manualMatchSearch').addEventListener('click', searchManualMatch);
    document.getElementById('manualMatchQuery').addEventListener('keypress', e => { if (e.key === 'Enter') searchManualMatch(); });
    document.getElementById('manualMatchAsIs').addEventListener('click', () => {
        matchChooser.onChoose({ kind: 'as_is' });
        hideModal(modal);
    });
}

async function scanManualImport() {
    const path = document.getElementById('manualImportPath').value.trim();
    const loader = document.getElementById('manualImportLoader');
    const summary = document.getElementById('manualImportSummary');
    manualItems = []; manualChoices = {}; manualChecked = new Set(); manualResults = {};
    drawManualImport();
    loader.classList.remove('hidden');
    summary.textContent = '';
    const { ok, data } = await postJSON('/api/manual_import/scan', { path });
    loader.classList.add('hidden');
    if (!ok) {
        summary.textContent = data.detail || 'Could not scan that folder.';
        return;
    }
    if (!path) document.getElementById('manualImportPath').value = data.path;
    manualItems = data.items;
    summary.textContent = manualItems.length ? `${manualItems.length} item${manualItems.length === 1 ? '' : 's'}`
        : 'Nothing to import here: no audio files or archives.';
    // A library book it matches (not on disk yet) is chosen; the rest are looked up on Audible
    manualItems.forEach(it => {
        if (it.match && !it.match.on_disk) {
            manualChoices[it.id] = { kind: 'library', book_id: it.match.book_id, title: it.match.title };
            manualChecked.add(it.id);
        }
    });
    drawManualImport();
    suggestManualMatches();
}

async function suggestManualMatches() {
    const run = ++manualSuggestRun;
    for (const it of manualItems) {
        if (run !== manualSuggestRun) return;  // A new scan started
        if (manualChoices[it.id] || it.match?.on_disk) continue;
        it.looking = true;
        drawManualImport();
        const { ok, data } = await postJSON('/api/manual_import/suggest', { id: it.id });
        it.looking = false;
        if (run !== manualSuggestRun) return;
        if (ok && data.match && !manualChoices[it.id]) {
            manualChoices[it.id] = audibleChoice(data.match);
            manualChecked.add(it.id);
        }
        drawManualImport();
    }
}

function audibleChoice(b) {
    return { kind: 'audible', asin: b.asin, title: b.title, authors: b.authors, edition: b.edition,
             sequence: b.sequence, series: b.series };
}

function manualChoiceHtml(it) {
    const c = manualChoices[it.id];
    const change = `<button class="link-btn manual-choose" data-id="${esc(it.id)}">${c ? 'Change' : 'Choose…'}</button>`;
    if (!c) {
        if (it.looking) return '<span class="muted">Looking on Audible…</span>';
        if (it.match?.on_disk) return `<span class="muted">Already on disk: ${bookLink(it.match.book_id, it.match.title)}</span> ${change}`;
        return `<span class="muted">Not matched</span> ${change}`;
    }
    if (c.kind === 'library') return `${bookLink(c.book_id, c.title)} <span class="muted">· in your library</span> ${change}`;
    if (c.kind === 'audible') {
        const extra = [c.authors, c.series ? `${c.series}${c.sequence ? ' #' + c.sequence : ''}` : '', c.asin].filter(Boolean).join(' · ');
        return `${esc(c.title)} ${c.edition && c.edition !== 'narrated' ? `<span class="edition-badge edition-${esc(c.edition)}">${esc(EDITION_LABELS[c.edition] || c.edition)}</span>` : ''}
            <span class="muted">· Audible</span> ${change}<div class="muted">${esc(extra)}</div>`;
    }
    const g = it.guess;
    return `${esc(g.title)} <span class="muted">· as named${g.authors ? ', by ' + esc(g.authors) : ''}</span> ${change}`;
}

function drawManualImport() {
    const rows = document.getElementById('manualImportRows');
    document.getElementById('manualImportTableBox').hidden = !manualItems.length;
    document.getElementById('manualImportFooter').hidden = !manualItems.length;
    rows.innerHTML = manualItems.map(it => {
        const r = manualResults[it.id];
        const result = !r ? '' : r.ok
            ? `<span class="result-ok">Imported</span>${r.message ? `<div class="muted">${esc(r.message)}</div>` : ''}`
            : `<span class="result-error">Failed</span><div class="muted">${esc(r.message)}</div>`;
        const done = r && r.ok;
        return `<tr>
            <td class="col-check"><input type="checkbox" class="manual-check" data-id="${esc(it.id)}" ${manualChecked.has(it.id) ? 'checked' : ''} ${!manualChoices[it.id] || done ? 'disabled' : ''} aria-label="Import ${esc(it.name)}"></td>
            <td class="item-name">${esc(it.name)}<div class="muted">${it.kind === 'archive' ? 'Archive · ' : ''}${it.file_count} file${it.file_count === 1 ? '' : 's'} · ${esc(formatSize(it.size))}</div></td>
            <td>${done ? bookLink(r.book_id, (manualChoices[it.id] || {}).title || it.guess.title) : manualChoiceHtml(it)}</td>
            <td>${result}</td>
        </tr>`;
    }).join('');
    rows.querySelectorAll('.manual-check').forEach(box => box.addEventListener('change', () => {
        box.checked ? manualChecked.add(box.dataset.id) : manualChecked.delete(box.dataset.id);
        updateManualImportButton();
    }));
    rows.querySelectorAll('.manual-choose').forEach(btn => btn.addEventListener('click', () => openManualMatch(btn.dataset.id)));
    updateManualImportButton();
}

function updateManualImportButton() {
    const count = [...manualChecked].filter(id => manualChoices[id]).length;
    const btn = document.getElementById('manualImportGo');
    btn.disabled = !count || manualImportRunning;
    btn.textContent = count ? `Import ${count}` : 'Import';
}

function setManualChoice(id, choice) {
    manualChoices[id] = choice;
    manualChecked.add(id);
    drawManualImport();
}

function openManualMatch(id) {
    const it = manualItems.find(x => x.id === id);
    if (!it) return;
    openMatchChooser(it.name, it.guess, true, choice => setManualChoice(id, choice));
}

function openMatchChooser(name, guess, withLibrary, onChoose) {
    matchChooser = { library: withLibrary, onChoose };
    document.getElementById('manualMatchItem').textContent = name;
    document.getElementById('manualMatchQuery').value = `${String(guess.title || '').split(':')[0]} ${primaryAuthor(guess.authors)}`.trim();
    document.getElementById('manualMatchAsIs').textContent = `Use it as named: "${guess.title}"${guess.authors ? ' by ' + guess.authors : ''}`;
    document.getElementById('manualMatchAsIs').hidden = !guess.title;
    document.getElementById('manualMatchLibraryBox').hidden = !withLibrary;
    showModal(document.getElementById('manualMatchModal'));
    searchManualMatch();
}

function matchOptionHtml(cover, title, lines, data) {
    return `<button type="button" class="match-option" ${data}>
        <img src="${esc(safeUrl(cover, PLACEHOLDER_COVER))}" alt="" loading="lazy">
        <span><b>${esc(title)}</b>${lines.filter(Boolean).map(l => `<span class="muted">${esc(l)}</span>`).join('')}</span></button>`;
}

async function searchManualMatch() {
    const query = document.getElementById('manualMatchQuery').value.trim();
    const words = normKey(query).split(' ').filter(w => w.length > 1);
    // Library books not on disk yet first; ones on disk can't take another copy
    const lib = !matchChooser.library ? [] : appLibrary.filter(b => !b.path && words.length
        && words.every(w => normKey([b.title, b.authors, b.series].join(' ')).includes(w))).slice(0, 20);
    const libBox = document.getElementById('manualMatchLibrary');
    libBox.innerHTML = lib.map(b => matchOptionHtml(coverUrl(b), b.title,
        [[b.authors, seriesLabel(b)].filter(Boolean).join(' · '), `${b.status}${editionOf(b) !== 'narrated' ? ' · ' + EDITION_LABELS[editionOf(b)] : ''}`],
        `data-book="${esc(b.id)}"`)).join('') || '<p class="muted">No books waiting for files match.</p>';
    libBox.querySelectorAll('[data-book]').forEach(btn => btn.addEventListener('click', () => {
        const b = appLibrary.find(x => x.id === btn.dataset.book);
        matchChooser.onChoose({ kind: 'library', book_id: b.id, title: b.title });
        hideModal(document.getElementById('manualMatchModal'));
    }));
    const box = document.getElementById('manualMatchAudible');
    const loader = document.getElementById('manualMatchLoader');
    box.innerHTML = '';
    loader.classList.remove('hidden');
    try {
        const res = await fetch(`/api/manual_import/candidates?q=${encodeURIComponent(query)}`);
        const data = await res.json();
        const found = res.ok ? data.candidates : [];
        box.innerHTML = found.map((b, i) => matchOptionHtml(b.imageUrl, b.title,
            [[b.authors, b.series ? `${b.series}${b.sequence ? ' #' + b.sequence : ''}` : ''].filter(Boolean).join(' · '),
             [b.narrators ? 'Narrated by ' + shortNames(b.narrators) : '', formatRuntime(b.runtime_min), releaseDate(b.release_date),
              b.edition !== 'narrated' ? EDITION_LABELS[b.edition] : ''].filter(Boolean).join(' · ')],
            `data-index="${i}"`)).join('') || `<p class="muted">${res.ok ? 'Nothing found on Audible.' : esc(data.detail || 'Audible search failed.')}</p>`;
        box.querySelectorAll('[data-index]').forEach(btn => btn.addEventListener('click', () => {
            matchChooser.onChoose(audibleChoice(found[btn.dataset.index]));
            hideModal(document.getElementById('manualMatchModal'));
        }));
    } catch (err) {
        box.innerHTML = '<p class="muted">Audible search failed.</p>';
    } finally {
        loader.classList.add('hidden');
    }
}

let manualImportRunning = false;

async function runManualImport() {
    const mode = document.getElementById('manualImportMode').value;
    const chosen = manualItems.filter(it => manualChecked.has(it.id) && manualChoices[it.id] && !manualResults[it.id]?.ok);
    if (!chosen.length) return;
    if (mode === 'move' && !await confirmDialog(`Move ${chosen.length} item${chosen.length === 1 ? '' : 's'} into the library? The originals are deleted afterwards, so a torrent of them stops seeding.`, { title: 'Move files?', confirmText: 'Move', danger: true })) return;
    const items = chosen.map(it => {
        const c = manualChoices[it.id];
        return c.kind === 'library' ? { id: it.id, book_id: c.book_id } : c.kind === 'audible' ? { id: it.id, asin: c.asin } : { id: it.id, as_is: true };
    });
    const status = document.getElementById('manualImportStatus');
    const { ok, data } = await postJSON('/api/manual_import/import', { items, mode });
    if (!ok) {
        setActionStatus(status, data.detail || 'Could not start the import', 'error');
        return;
    }
    manualImportRunning = true;
    updateManualImportButton();
    let st = data;
    while (st.running) {
        setActionStatus(status, `Importing ${st.done + 1} of ${st.total}…`);
        await new Promise(r => setTimeout(r, 1000));
        st = await fetch('/api/manual_import/status').then(r => r.json()).catch(() => st);
    }
    manualImportRunning = false;
    st.results.forEach(r => { manualResults[r.id] = r; if (r.ok) manualChecked.delete(r.id); });
    await fetchLibrary();
    setActionStatus(status, `${st.imported} imported${st.failed ? `, ${st.failed} failed` : ''}`, st.failed ? 'error' : 'ok');
    drawManualImport();
}

function historyLimit() {
    let value = '50';
    try { value = localStorage.getItem('bayarr.historyLimit') || value; } catch (e) { /* storage unavailable */ }
    return ['10', '20', '50', '100', 'all'].includes(value) ? value : '50';
}

function setupHistoryLimit() {
    const select = document.getElementById('historyLimit');
    select.value = historyLimit();
    select.addEventListener('change', () => {
        try { localStorage.setItem('bayarr.historyLimit', select.value); } catch (e) { /* storage unavailable */ }
        renderActivity();
    });
}

async function renderActivity() {
    renderConversions();
    let queueData, historyData;
    try {
        [queueData, historyData] = await Promise.all([
            fetch('/api/queue').then(r => r.json()),
            fetch(`/api/history?limit=${historyLimit() === 'all' ? 1000 : historyLimit()}`).then(r => r.json()),
        ]);
    } catch (err) {
        console.error('Failed to load activity', err);
        return;
    }

    const notice = document.getElementById('queueNotice');
    notice.textContent = !appSettings.qbt_enabled ? 'qBittorrent is not enabled in Settings, so live progress is unavailable.'
        : (!queueData.client_reachable ? "Can't reach qBittorrent; showing the last known state." : '');

    const qRows = document.getElementById('queueRows');
    qRows.innerHTML = queueData.queue.map(q => {
        const pct = q.progress != null ? Math.round(q.progress * 100) : null;
        const actions = q.status === 'Needs Review' ? `
            <div class="muted" style="margin-top: 6px;">${esc(q.review_reason)}</div>
            <div class="row-actions">
                <button class="secondary-btn" data-action="import_anyway" data-id="${esc(q.id)}">Import Anyway</button>
                <button class="danger-btn" data-action="reject" data-id="${esc(q.id)}">Reject &amp; Search Again</button>
            </div>` : '';
        return `<tr>
            <td>${bookLink(q.id, q.title)}
                <div class="muted">${esc(q.release_title || q.authors || '')}</div>${actions}</td>
            <td><span class="library-status ${esc(statusClass(q.status))}" style="position: static; box-shadow: none;">${esc(q.status)}</span>
                ${q.state ? `<div class="muted" style="margin-top: 4px;">${esc(q.state)}</div>` : ''}</td>
            <td>${pct != null ? `<div class="progress"><div class="progress-bar" style="width: ${pct}%"></div></div>
                <div class="muted">${pct}%${q.size_bytes ? ' of ' + esc(formatSize(q.size_bytes)) : ''}</div>` : '<span class="muted">—</span>'}</td>
            <td>${q.dlspeed ? esc(formatSize(q.dlspeed)) + '/s' : ''}<div class="muted">${esc(q.progress < 1 ? formatDuration(q.eta) : '')}</div></td>
            <td>${q.seeds != null ? esc(q.seeds) : ''}</td>
        </tr>`;
    }).join('') || '<tr><td colspan="5" class="no-results">Nothing downloading.</td></tr>';

    qRows.querySelectorAll('[data-action]').forEach(btn => btn.addEventListener('click', () => reviewAction(btn.dataset.id, btn.dataset.action)));

    document.getElementById('historyRows').innerHTML = historyData.history.map(h => `
        <tr>
            <td class="muted">${esc(new Date(h.time).toLocaleString())}</td>
            <td><span class="event event-${esc(h.event)}">${esc(EVENT_LABELS[h.event] || h.event)}</span></td>
            <td>${bookLink(h.book_id, h.title)}</td>
            <td class="muted">${esc(h.message)}</td>
        </tr>`).join('') || '<tr><td colspan="4" class="no-results">Nothing has happened yet.</td></tr>';

    if (queueData.queue.some(q => q.status === 'Needs Review') !== appLibrary.some(b => b.status === 'Needs Review')) {
        await fetchLibrary();
    }
}

// Listeners
searchBtn.addEventListener('click', performSearch);
searchInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') performSearch();
});

modalClose.addEventListener('click', closeModal);
window.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
});

let lastSearchQuery = '';

function performSearch() {
    const query = searchInput.value.trim();
    if (!query) return;
    if (location.hash === '#/search/' + encodeURIComponent(query)) {
        runSearch(query);  // Searching again for the same thing
    } else {
        navigate('/search/' + encodeURIComponent(query));
    }
}

async function runSearch(query) {
    lastSearchQuery = query;
    resultsContainer.innerHTML = '';
    loader.classList.remove('hidden');

    try {
        const res = await fetch(`/api/search_audible?title=${encodeURIComponent(query)}`);
        const data = await res.json();
        renderResults(data);
    } catch (err) {
        console.error(err);
        resultsContainer.innerHTML = '<div class="no-results">Error fetching results.</div>';
    } finally {
        loader.classList.add('hidden');
    }
}

function renderResults(data) {
    resultsContainer.innerHTML = '';

    if (!data.products || data.products.length === 0) {
        resultsContainer.innerHTML = '<div class="no-results">No audiobooks found on Audible.</div>';
        return;
    }

    const groups = {};
    const standalone = [];

    data.products.forEach(product => {
        const bookData = {
            title: product.title,
            authors: product.authors ? product.authors.map(a => a.name).join(', ') : 'Unknown Author',
            narrators: product.narrators ? product.narrators.map(n => n.name).join(', ') : 'Unknown Narrator',
            imageUrl: product.product_images && product.product_images[500] ? product.product_images[500] : PLACEHOLDER_COVER,
            release_date: product.release_date || product.issue_date || "",
            asin: product.asin || "",
            runtime_min: product.runtime_length_min || 0,
            description: product.publisher_summary || "",
            publisher: product.publisher_name || "",
            language: product.language ? product.language[0].toUpperCase() + product.language.slice(1) : "",
            edition: product.edition || "",
            edition_reason: product.edition_reason || ""
        };

        if (product.series && product.series.length > 0) {
            const seriesTitle = product.series[0].title;
            bookData.series = seriesTitle;
            bookData.series_asin = product.series[0].asin || "";
            bookData.sequence = product.series[0].sequence || "";
            if (!groups[seriesTitle]) groups[seriesTitle] = [];
            groups[seriesTitle].push(bookData);
        } else {
            standalone.push(bookData);
        }
    });

    for (const [seriesTitle, books] of Object.entries(groups)) {
        books.sort((a, b) => (parseFloat(a.sequence) || 999) - (parseFloat(b.sequence) || 999));
        renderGroup(seriesTitle, books);
    }

    if (standalone.length > 0) {
        renderGroup("Standalone Books", standalone);
    }
}

function renderGroup(title, books) {
    const section = document.createElement('div');
    section.className = 'series-section';

    const header = document.createElement('div');
    header.className = 'series-header series-group-header';
    const name = document.createElement('span');
    name.textContent = title;
    header.appendChild(name);
    const seriesAsin = books[0] && books[0].series_asin;
    if (seriesAsin) {
        const tracked = appSeries.find(sr => sr.asin === seriesAsin && sr.monitored);
        const btn = document.createElement('button');
        btn.className = 'secondary-btn';
        btn.textContent = tracked ? 'View Series (Monitored)' : 'View Series';
        btn.addEventListener('click', () => openSeriesDetail('asin:' + seriesAsin));
        header.appendChild(btn);
    }
    section.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'results-grid';

    books.forEach(book => {
        const released = releaseDate(book.release_date);
        const tracked = findInLibrary(book);
        const isTracked = Boolean(tracked);
        const card = document.createElement('div');
        card.className = 'book-card';

        let addBtnHtml = '';
        if (isTracked) {
            addBtnHtml = `<a href="#/book/${encodeURIComponent(tracked.id)}" class="add-btn monitored-btn book-link" data-id="${esc(tracked.id)}" title="Open it in your library">${esc(tracked.status === 'Imported' ? 'In Library' : tracked.status)}</a>`;
        } else {
            addBtnHtml = `<div class="add-btn">Add to Library</div>`;
        }

        card.innerHTML = `
            <img src="${safeUrl(book.imageUrl, '')}" alt="${esc(book.title)}" class="book-cover">
            <div class="book-info">
                <div class="book-title" title="${esc(book.title)}">${esc(book.title)}</div>
                ${editionBadge(book) ? `<div class="edition-badges">${editionBadge(book)}</div>` : ''}
                <div class="book-author"><button class="link-btn author-link" data-author="${esc(primaryAuthor(book.authors))}">${esc(book.authors)}</button></div>
                <div class="book-narrator">Narrated by: ${esc(book.narrators)}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 8px;">Release: ${esc(released || 'Unknown')}</div>
                ${addBtnHtml}
            </div>
        `;

        // Add event listener to the add button specifically
        const addBtn = card.querySelector('.add-btn');
        if (addBtn && !isTracked) {
            addBtn.addEventListener('click', (e) => addToLibrary(e, book));
        }

        // Modal opens when clicking the card (but not the button)
        card.addEventListener('click', (e) => {
            if (!e.target.classList.contains('add-btn')) {
                openModal(book);
            }
        });

        grid.appendChild(card);
    });

    section.appendChild(grid);
    resultsContainer.appendChild(section);
}

async function openModal(book) {
    const { title, authors: author, narrators, imageUrl: coverUrl } = book;
    currentModalBook = book;
    modalTitle.textContent = title;
    modalAuthor.textContent = author;
    modalNarrator.textContent = `Narrated by: ${narrators}`;
    modalCover.src = /^(https?:|\/)/i.test(coverUrl || '') ? coverUrl : '';

    // Reset ABB results
    abbResults.innerHTML = '';

    modal.style.display = 'flex';
    // Trigger reflow for animation
    void modal.offsetWidth;
    modal.classList.add('show');

    modalLoader.style.display = 'flex';

    currentABBData = [];
    abbSearchInfo.textContent = 'Searching AudiobookBay…';

    try {
        const { data } = await postJSON('/api/search_abb', { book });
        currentABBData = data.results || [];
        abbSearchInfo.textContent = data.error ? data.error
            : data.queries?.length ? `Searched for: ${data.queries.join(' · ')}` : '';
        renderABBResults();
    } catch (err) {
        console.error(err);
        abbResults.innerHTML = '<tr><td colspan="7" class="no-results">Failed to fetch downloads.</td></tr>';
    } finally {
        modalLoader.style.display = 'none';
    }
}

langFilter.addEventListener('change', () => {
    renderABBResults();
});
document.getElementById('showRejected').addEventListener('change', renderABBResults);

const VERDICT_LABELS = { match: 'Match', possible: 'Possible', weak: 'Weak', rejected: 'No' };

function renderABBResults() {
    abbResults.innerHTML = '';

    if (!currentABBData || currentABBData.length === 0) {
        abbResults.innerHTML = '<tr><td colspan="7" class="no-results">No downloads found.</td></tr>';
        return;
    }

    const filterVal = langFilter.value;
    const showRejected = document.getElementById('showRejected').checked;
    // The server sends releases best first, scored against this book
    let displayData = currentABBData.filter(d => filterVal === 'All' || !d.language || d.language === 'Unknown'
        || d.language.toLowerCase() === filterVal.toLowerCase());
    const rejected = displayData.filter(d => d.verdict === 'rejected').length;
    if (!showRejected) displayData = displayData.filter(d => d.verdict !== 'rejected');

    if (displayData.length === 0) {
        const hint = rejected ? `All ${rejected} release${rejected === 1 ? ' was' : 's were'} rejected. Tick "Show rejected" to see why.`
            : 'No downloads match the selected language filter.';
        abbResults.innerHTML = `<tr><td colspan="7" class="no-results">${esc(hint)}</td></tr>`;
        return;
    }

    displayData.forEach(res => {
        const tr = document.createElement('tr');
        if (res.verdict === 'rejected') tr.className = 'release-rejected';
        const magnetUrl = safeUrl(res.magnet_url || res.download_url || `/api/download?url=${encodeURIComponent(res.link)}&title=${encodeURIComponent(res.title)}`);
        const isM4b = (res.format || '').toUpperCase() === 'M4B';
        const narratorMatch = (res.reasons || []).includes('Narrator matches');
        const why = [...(res.problems || []), ...(res.reasons || [])];
        const verdict = res.verdict || 'weak';
        const scoreBadge = `<span class="score-badge score-${esc(verdict)}" title="${esc(why.join('\n'))}">${esc(res.score ?? '')}<small>${esc(VERDICT_LABELS[verdict] || '')}</small></span>`;
        const notes = (res.problems || []).length ? `<div class="release-problems">${esc(res.problems.join(' · '))}</div>` : '';
        const posted = res.posted ? ` · posted ${esc(res.posted)}` : '';

        let actionsHtml = `
            <a href="${magnetUrl}" class="download-icon-btn" target="_blank" title="Manual Magnet Link">
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
            </a>
        `;

        if (appSettings.qbt_enabled) {
            actionsHtml = `
                <div style="display: flex; gap: 8px;">
                    <button class="download-icon-btn send-to-client-btn" data-url="${esc(res.link)}" data-index="${displayData.indexOf(res)}" title="Send to qBittorrent">
                        <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"></path></svg>
                    </button>
                    ${actionsHtml}
                </div>
            `;
        }

        tr.innerHTML = `
            <td>
                <a class="release-title" href="${safeUrl(res.link)}" target="_blank" rel="noopener noreferrer">${esc(res.release_name || res.raw_title || res.title)}</a>
                <div class="release-meta">${esc(res.author || '')}${posted}${res.seeders != null ? ` · ${esc(res.seeders)} seeders` : ''}${res.source && res.source !== 'AudiobookBay' ? `<span class="release-source">${esc(res.source)}</span>` : ''}</div>
                ${notes}
            </td>
            <td class="nowrap">${esc(res.size_str)}</td>
            <td>${isM4b ? '<span class="badge match">M4B</span>' : esc(res.format || 'Unknown')}${res.bitrate && res.bitrate !== 'Unknown' ? `<div class="release-meta nowrap">${esc(res.bitrate)}</div>` : ''}</td>
            <td>${esc(res.language || 'Unknown')}</td>
            <td class="${narratorMatch ? 'narrator-match' : ''}">${esc(res.abb_narrator || 'Not loaded')}</td>
            <td>${scoreBadge}</td>
            <td>${actionsHtml}</td>
        `;
        abbResults.appendChild(tr);
    });

    if (!showRejected && rejected) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td colspan="7" class="no-results">${rejected} rejected release${rejected === 1 ? '' : 's'} hidden.</td>`;
        abbResults.appendChild(tr);
    }

    // Add listeners for send to client buttons
    document.querySelectorAll('.send-to-client-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const button = e.currentTarget;
            const url = button.getAttribute('data-url');
            const picked = displayData[Number(button.dataset.index)] || {};
            const release = { magnet_url: picked.magnet_url, download_url: picked.download_url, link: picked.link,
                              title: picked.title, raw_title: picked.raw_title, format: picked.format, size_str: picked.size_str,
                              source: picked.source };

            button.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div>';
            button.disabled = true;

            try {
                const res = await fetch('/api/send_to_client', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, book: currentModalBook, release })
                });
                if (res.ok) {
                    button.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--success)" stroke-width="2" fill="none"><polyline points="20 6 9 17 4 12"></polyline></svg>';
                    fetchLibrary(); // refresh library status
                } else {
                    button.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" stroke="red" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
                }
            } catch (err) {
                button.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" stroke="red" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
            }
        });
    });
}

function closeModal() {
    modal.classList.remove('show');
    setTimeout(() => {
        modal.style.display = 'none';
    }, 300);
}

// -----------------
// FOLDER PICKER MODAL
// -----------------
let activeFolderInputId = null;
let currentParentPath = null;
let currentFolderRaw = '';

const folderModal = document.getElementById('folderModal');
const closeFolderModal = document.getElementById('closeFolderModal');
const folderList = document.getElementById('folderList');
const currentFolderPath = document.getElementById('currentFolderPath');

document.querySelectorAll('.browse-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
        e.preventDefault();
        activeFolderInputId = e.currentTarget.getAttribute('data-target');
        const currentVal = document.getElementById(activeFolderInputId).value;
        folderModal.style.display = "flex";
        setTimeout(() => folderModal.classList.add('show'), 10);
        loadFolder(currentVal);
    });
});

closeFolderModal.addEventListener('click', () => {
    folderModal.classList.remove('show');
    setTimeout(() => {
        folderModal.style.display = 'none';
    }, 200);
});

document.getElementById('selectFolderBtn').addEventListener('click', () => {
    if (activeFolderInputId && currentFolderPath.value) {
        // The exact path, not the readable version, so it works on disk
        document.getElementById(activeFolderInputId).value = currentFolderRaw || currentFolderPath.value;
    }
    folderModal.classList.remove('show');
    setTimeout(() => {
        folderModal.style.display = 'none';
    }, 200);
});

document.getElementById('folderUpBtn').addEventListener('click', () => {
    if (currentParentPath) {
        loadFolder(currentParentPath);
    }
});

async function loadFolder(path) {
    folderList.innerHTML = '<div style="padding: 16px; text-align: center;">Loading...</div>';
    try {
        const res = await fetch('/api/browse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path || '' })
        });
        if (!res.ok) throw new Error(`the server returned ${res.status}`);
        const data = await res.json();

        currentFolderPath.value = data.display_path || data.path;
        currentFolderRaw = data.path;
        currentParentPath = data.parent;

        folderList.innerHTML = '';
        if (data.dirs && data.dirs.length > 0) {
            data.dirs.forEach(dir => {
                const item = document.createElement('div');
                item.className = 'folder-item';
                // Simple SVG folder icon
                const icon = `<svg class="folder-icon" viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2" fill="none"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`;
                item.innerHTML = `${icon}<span>${esc(dir.name)}</span>`;
                item.addEventListener('click', () => loadFolder(dir.path));
                folderList.appendChild(item);
            });
        } else {
            folderList.innerHTML = '<div style="padding: 16px; color: var(--text-muted);">No subfolders found</div>';
        }
    } catch (err) {
        console.error("Folder load error:", err);
        folderList.innerHTML = `<div style="padding: 16px; color: red;">Error loading folder structure: ${esc(err.message || err)}</div>`;
    }
}


// -----------------
// Calendar: library books on their release dates, coloured by status like Sonarr's
// calendar, plus trending Audible releases (best sellers, new books from your authors)
// -----------------
let calData = { items: [], genres: [] };
let calDate = new Date();
let calView = 'month';
try { calView = localStorage.getItem('bayarr.calView') || (window.innerWidth < 768 ? 'agenda' : 'month'); } catch (e) { /* storage unavailable */ }
let calLookupTimer = null;
let calRefreshStartedThisVisit = false;
const CAL_MAX_PER_DAY = 4;

function isoDate(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// Where a library book stands, in the calendar's terms
function calStatus(book, today) {
    switch (book.status) {
        case 'Imported': return 'ondisk';
        case 'Downloading': case 'Downloaded': return 'downloading';
        case 'Needs Review': return 'review';
        case 'Missing': return 'missing';
        case 'Unmonitored': return 'unmonitored';
        case 'Unreleased': return 'upcoming';
        default: return (book.release_date || '') > today ? 'upcoming' : 'missing';
    }
}

const CAL_STATUS_LABELS = { ondisk: 'On disk', downloading: 'Downloading', missing: 'Missing', review: 'Needs review',
    upcoming: 'Upcoming', unmonitored: 'Unmonitored', trending: 'Not in library' };

function librarySeriesKeys() {
    const keys = new Set();
    appLibrary.forEach(b => seriesEntries(b).forEach(e => { keys.add(seriesKey(e.name)); if (e.asin) keys.add(e.asin); }));
    appSeries.forEach(sr => { keys.add(sr.asin); keys.add(seriesKey(sr.title)); });
    return keys;
}

function trendingMatches(item, trend, genre, authors, seriesKeys) {
    if (genre && !(item.genres || []).includes(genre)) return false;
    switch (trend) {
        case 'top100': return item.rank && item.rank <= 100;
        case 'genretop': return item.genre_rank && item.genre_rank <= 25;
        case 'authors': return item.from_author || authorKeyList(item.authors).some(a => authors.has(a));
        case 'series': return seriesEntries(item).some(e => seriesKeys.has(e.asin) || seriesKeys.has(seriesKey(e.name)));
        case 'starts': return String(item.sequence || '') === '1';
        case 'rated': return item.rating >= 4.5 && item.ratings >= 25;
        default: return true;
    }
}

// Every event to show: {date, kind: 'library'|'trending', status, book}
function calendarEvents() {
    const today = isoDate(new Date());
    const events = [];
    if (document.getElementById('calShowLibrary').checked) {
        appLibrary.forEach(b => {
            const date = String(b.release_date || '').slice(0, 10);
            if (/^\d{4}-\d{2}-\d{2}$/.test(date) && date < '2100') {
                events.push({ date, kind: 'library', status: calStatus(b, today), book: b });
            }
        });
    }
    if (document.getElementById('calShowTrending').checked) {
        const trend = document.getElementById('calTrend').value;
        const genre = document.getElementById('calGenre').value;
        const authors = new Set(appLibrary.flatMap(b => authorKeyList(b.authors)).filter(Boolean));
        const seriesKeys = librarySeriesKeys();
        calData.items.forEach(item => {
            if (findInLibrary(item) || !trendingMatches(item, trend, genre, authors, seriesKeys)) return;
            events.push({ date: item.release_date.slice(0, 10), kind: 'trending', status: 'trending', book: item });
        });
    }
    // Your books first, then by best-seller rank
    const order = e => e.kind === 'library' ? 0 : 1;
    events.sort((a, b) => order(a) - order(b) || (a.book.rank || 9999) - (b.book.rank || 9999)
        || String(a.book.title).localeCompare(String(b.book.title)));
    return events;
}

function eventSubtitle(book) {
    return book.series ? `${book.series}${book.sequence ? ' #' + book.sequence : ''}` : primaryAuthor(book.authors);
}

// A library book's calendar entry is a link to it; a trending one is a button (it offers to add)
function calTag(ev) {
    return ev.kind === 'library' && ev.book.id
        ? { open: `a href="#/book/${encodeURIComponent(ev.book.id)}" data-id="${esc(ev.book.id)}"`, link: ' book-link', close: 'a' }
        : { open: 'button', link: '', close: 'button' };
}

function calEventHtml(ev, index) {
    const b = ev.book;
    const tag = calTag(ev);
    const trendBadge = ev.kind === 'trending' && b.rank && b.rank <= 100 ? `<span class="cal-rank" title="Audible best seller #${b.rank}">#${b.rank}</span>` : '';
    return `<${tag.open} class="cal-event cal-${ev.status}${trendBadge ? ' ranked' : ''}${tag.link}" data-event="${index}" title="${esc(`${b.title} — ${CAL_STATUS_LABELS[ev.status]}`)}">
        <span class="cal-event-title">${esc(b.title)}</span>${trendBadge}
        <span class="cal-event-sub">${esc(eventSubtitle(b))}</span></${tag.close}>`;
}

function calCardHtml(ev, index) {
    const b = ev.book;
    const label = ev.kind === 'library' ? CAL_STATUS_LABELS[ev.status] : (b.rank ? `Best seller #${b.rank}` : 'Trending');
    const tag = calTag(ev);
    return `<${tag.open} class="cal-card cal-${ev.status}${tag.link}" data-event="${index}">
        <img src="${esc(coverUrl(b))}" alt="" loading="lazy" onerror="this.src='${PLACEHOLDER_COVER}'">
        <span class="cal-card-text">
            <span class="cal-event-title">${esc(b.title)}</span>
            <span class="cal-event-sub">${esc(eventSubtitle(b))}</span>
            <span class="cal-card-status">${esc(label)}</span>
        </span></${tag.close}>`;
}

function startOfWeek(d) {
    const s = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    s.setDate(s.getDate() - s.getDay());  // Weeks start on Sunday
    return s;
}

function addDays(d, n) {
    const r = new Date(d);
    r.setDate(r.getDate() + n);
    return r;
}

function calRange() {
    if (calView === 'week') {
        const start = startOfWeek(calDate);
        return { start, end: addDays(start, 6) };
    }
    const first = new Date(calDate.getFullYear(), calDate.getMonth(), 1);
    if (calView === 'agenda') {
        return { start: first, end: new Date(calDate.getFullYear(), calDate.getMonth() + 1, 0) };
    }
    const start = startOfWeek(first);
    return { start, end: addDays(start, 41) };  // Six weeks, like Sonarr's month view
}

function drawCalendar() {
    const container = document.getElementById('calendarContainer');
    const { start, end } = calRange();
    const today = isoDate(new Date());
    const from = isoDate(start), to = isoDate(end);
    const events = calendarEvents().filter(ev => ev.date >= from && ev.date <= to);
    const byDay = {};
    events.forEach((ev, i) => (byDay[ev.date] = byDay[ev.date] || []).push(i));

    const monthName = calDate.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    document.getElementById('calLabel').textContent = calView === 'week'
        ? `${start.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} – ${end.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}`
        : monthName;
    document.querySelectorAll('[data-cal-view]').forEach(b => b.classList.toggle('active', b.dataset.calView === calView));

    const dayNames = [...Array(7)].map((_, i) => addDays(startOfWeek(new Date()), i).toLocaleDateString(undefined, { weekday: 'short' }));

    if (calView === 'agenda') {
        const days = Object.keys(byDay).sort();
        container.innerHTML = days.length ? `<div class="cal-agenda">${days.map(day => {
            const d = new Date(day + 'T00:00:00');
            return `<div class="cal-agenda-day${day === today ? ' today' : ''}">
                <div class="cal-agenda-date"><span>${esc(d.toLocaleDateString(undefined, { weekday: 'short' }))}</span><b>${d.getDate()}</b><span>${esc(d.toLocaleDateString(undefined, { month: 'short' }))}</span></div>
                <div class="cal-agenda-items">${byDay[day].map(i => calCardHtml(events[i], i)).join('')}</div></div>`;
        }).join('')}</div>` : `<div class="empty-state"><h3>Nothing this month</h3><p>No releases match these filters in ${esc(monthName)}.</p></div>`;
    } else {
        const days = [];
        for (let d = new Date(start); d <= end; d = addDays(d, 1)) days.push(new Date(d));
        const week = calView === 'week';
        container.innerHTML = `<div class="cal-grid ${week ? 'cal-week' : 'cal-month'}">
            ${dayNames.map(n => `<div class="cal-dayname">${esc(n)}</div>`).join('')}
            ${days.map(d => {
                const day = isoDate(d);
                const list = byDay[day] || [];
                const outside = !week && d.getMonth() !== calDate.getMonth();
                const shown = week ? list : list.slice(0, CAL_MAX_PER_DAY);
                const more = list.length - shown.length;
                return `<div class="cal-day${outside ? ' outside' : ''}${day === today ? ' today' : ''}" data-day="${day}">
                    <div class="cal-daynum">${week ? esc(d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })) : d.getDate()}</div>
                    ${shown.map(i => week ? calCardHtml(events[i], i) : calEventHtml(events[i], i)).join('')}
                    ${more > 0 ? `<button class="cal-more" data-day="${day}">+${more} more</button>` : ''}
                </div>`;
            }).join('')}</div>`;
    }

    container.querySelectorAll('[data-event]').forEach(el => el.addEventListener('click', () => openCalendarEvent(events[el.dataset.event])));
    container.querySelectorAll('.cal-more').forEach(btn => btn.addEventListener('click', () => {
        // Show that day's whole list in place
        const cell = btn.closest('.cal-day');
        cell.querySelectorAll('.cal-event, .cal-more').forEach(el => el.remove());
        cell.insertAdjacentHTML('beforeend', byDay[btn.dataset.day].map(i => calEventHtml(events[i], i)).join(''));
        cell.classList.add('expanded');
        cell.querySelectorAll('[data-event]').forEach(el => el.addEventListener('click', () => openCalendarEvent(events[el.dataset.event])));
    }));
}

function openCalendarEvent(ev) {
    if (ev.kind === 'library') {
        openBookModal(ev.book.id);
        return;
    }
    const b = ev.book;
    const facts = [
        ['Release', b.release_date],
        ['Length', b.runtime_min ? formatDuration(b.runtime_min * 60) : ''],
        ['Edition', editionOf(b) !== 'narrated' ? EDITION_LABELS[editionOf(b)] : ''],
        ['Narrated by', b.narrators],
        ['Series', b.series ? `${b.series}${b.sequence ? ' #' + b.sequence : ''}` : ''],
        ['Genres', [...(b.genres || []), ...(b.subgenres || [])].join(', ')],
        ['Rating', b.ratings ? `${b.rating.toFixed(1)} (${b.ratings} rating${b.ratings === 1 ? '' : 's'})` : ''],
        ['Best seller', [b.rank ? `#${b.rank} overall` : '', b.genre_rank ? `#${b.genre_rank} in its genre` : ''].filter(Boolean).join(', ')],
        ['Publisher', b.publisher],
    ].filter(([, v]) => v);
    document.getElementById('calendarModalBody').innerHTML = `
        <div class="cal-detail">
            <img src="${esc(coverUrl(b))}" alt="" onerror="this.src='${PLACEHOLDER_COVER}'">
            <div>
                <h3>${esc(b.title)}</h3>
                ${b.subtitle ? `<div class="muted">${esc(b.subtitle)}</div>` : ''}
                <div class="cal-detail-author"><button class="link-btn author-link" data-author="${esc(primaryAuthor(b.authors))}">${esc(b.authors)}</button></div>
                ${b.from_author ? '<span class="badge match">From your authors</span>' : ''}
                <dl class="cal-facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
                <div class="cal-detail-actions">
                    <button class="primary-btn" id="calAddBtn">Add to Library</button>
                    ${b.series_asin ? '<button class="secondary-btn" id="calSeriesBtn">View Series</button>' : ''}
                </div>
            </div>
        </div>`;
    const modalEl = document.getElementById('calendarModal');
    showModal(modalEl);
    document.getElementById('calAddBtn').addEventListener('click', async (e) => {
        e.target.disabled = true;
        const { ok, data } = await postJSON('/api/library', b);
        if (!ok) {
            toast(data.detail || 'Could not add the book', 'error');
            e.target.disabled = false;
            return;
        }
        toast(`Added "${b.title}" (${data.status})`, 'ok');
        hideModal(modalEl);
        await fetchLibrary();
        drawCalendar();
    });
    document.getElementById('calSeriesBtn')?.addEventListener('click', () => {
        hideModal(modalEl);
        openSeriesDetail('asin:' + b.series_asin);
    });
}

function updateCalendarLookup(job) {
    const banner = document.getElementById('calLookup');
    clearTimeout(calLookupTimer);
    if (job && job.running) {
        banner.hidden = false;
        banner.textContent = job.total
            ? `Loading trending releases from Audible… ${job.done} of ${job.total} lists`
            : 'Loading trending releases from Audible…';
        calLookupTimer = setTimeout(async () => {
            if (document.getElementById('calendarView').hidden) return;
            await loadCalendarData();
        }, 3000);
    } else {
        banner.hidden = true;
    }
}

async function loadCalendarData() {
    const res = await fetch('/api/calendar');
    const data = await res.json().catch(() => null);
    if (!data) return;
    const changed = data.fetched !== calData.fetched || !calData.items.length;
    calData = data;
    const genreSelect = document.getElementById('calGenre');
    const chosen = genreSelect.value;
    genreSelect.innerHTML = '<option value="">All genres</option>' +
        (data.genres || []).map(g => `<option value="${esc(g)}"${g === chosen ? ' selected' : ''}>${esc(g)}</option>`).join('');
    if (data.stale && !data.refresh.running && !calRefreshStartedThisVisit) {
        calRefreshStartedThisVisit = true;
        const { data: job } = await postJSON('/api/calendar/refresh');
        updateCalendarLookup({ ...job, running: true });
    } else {
        updateCalendarLookup(data.refresh);
    }
    if (changed) drawCalendar();
}

async function renderCalendar() {
    drawCalendar();
    await loadCalendarData();
}

function setupCalendar() {
    const step = dir => {
        if (calView === 'week') calDate = addDays(calDate, 7 * dir);
        else calDate = new Date(calDate.getFullYear(), calDate.getMonth() + dir, 1);
        drawCalendar();
    };
    document.getElementById('calPrev').addEventListener('click', () => step(-1));
    document.getElementById('calNext').addEventListener('click', () => step(1));
    document.getElementById('calToday').addEventListener('click', () => { calDate = new Date(); drawCalendar(); });
    document.querySelectorAll('[data-cal-view]').forEach(btn => btn.addEventListener('click', () => {
        calView = btn.dataset.calView;
        try { localStorage.setItem('bayarr.calView', calView); } catch (e) { /* storage unavailable */ }
        drawCalendar();
    }));
    ['calShowLibrary', 'calShowTrending', 'calTrend', 'calGenre'].forEach(id =>
        document.getElementById(id).addEventListener('change', drawCalendar));
    document.getElementById('calRefreshBtn').addEventListener('click', async () => {
        const { data } = await postJSON('/api/calendar/refresh');
        updateCalendarLookup({ ...data, running: true });
        toast('Loading trending releases from Audible in the background');
    });
    const modalEl = document.getElementById('calendarModal');
    document.getElementById('closeCalendarModal').addEventListener('click', () => hideModal(modalEl));
    modalEl.addEventListener('click', (e) => { if (e.target === modalEl) hideModal(modalEl); });
}


// -----------------
// Split a collection folder ("Book 1 ... Part 1 of 2", "Book 2 ...") into one book per folder
// -----------------
const splitModal = document.getElementById('splitModal');
let splitProposal = null;

function updateSplitButton() {
    const n = document.querySelectorAll('.split-check:checked').length;
    const btn = document.getElementById('splitBtn');
    btn.disabled = n === 0;
    btn.textContent = n ? `Split into ${n} Book${n === 1 ? '' : 's'}` : 'Split';
}

async function openSplitDialog(bookId) {
    splitProposal = null;
    const lead = document.getElementById('splitLead');
    const loader = document.getElementById('splitLoader');
    document.getElementById('splitTable').hidden = true;
    document.getElementById('splitRows').innerHTML = '';
    setActionStatus(document.getElementById('splitStatus'), '');
    lead.textContent = 'Looking for the books in this folder and their series on Audible…';
    updateSplitButton();
    showModal(splitModal);
    loader.classList.remove('hidden');
    const res = await fetch(`/api/library/${encodeURIComponent(bookId)}/split`);
    const data = await res.json().catch(() => ({}));
    loader.classList.add('hidden');
    if (!res.ok) {
        lead.textContent = data.detail || "Couldn't read this folder.";
        return;
    }
    if (data.groups.length < 2) {
        lead.textContent = 'Bayarr couldn\'t find separate books in this folder. It looks for a book number in the file or folder names, like "Book 2 Golden Son Part 1 of 2.m4b" or a "Book 2 - Title" folder.';
        return;
    }
    splitProposal = data;
    const matched = data.groups.filter(g => g.match).length;
    lead.textContent = `${data.groups.length} books found in "${data.title}"`
        + (data.series ? `, matched to ${matched} ${EDITION_LABELS[data.edition].toLowerCase()} books of ${data.series.name} on Audible.` : '. The series wasn\'t found on Audible, so titles come from the file names.')
        + ' Check the titles and numbers, then split.';
    document.getElementById('splitRows').innerHTML = data.groups.map(g => {
        const m = g.match;
        return `<tr>
            <td><input type="checkbox" class="split-check" data-index="${g.index}" checked></td>
            <td><input type="text" class="form-input split-number" data-index="${g.index}" value="${esc(g.number)}" aria-label="Series number"></td>
            <td><input type="text" class="form-input split-title" data-index="${g.index}" value="${esc(g.title)}" aria-label="Title"></td>
            <td class="nowrap" title="${esc(g.files.join('\n'))}">${g.files.length} file${g.files.length === 1 ? '' : 's'}, ${esc(formatSize(g.size))}</td>
            <td>${m ? `${esc(m.title)}<div class="muted">${esc([formatRuntime(m.runtime_min), m.asin].filter(Boolean).join(' · '))}</div>` : '<span class="muted">No match</span>'}</td>
            <td class="muted">${esc(g.folder)}${g.exists ? ' <span class="edition-badge edition-check">Exists</span>' : ''}</td>
        </tr>`;
    }).join('');
    document.getElementById('splitTable').hidden = false;
    document.querySelectorAll('.split-check').forEach(cb => cb.addEventListener('change', updateSplitButton));
    updateSplitButton();
}

function setupSplitModal() {
    document.getElementById('splitBookBtn').addEventListener('click', () => openSplitDialog(currentBookId));
    document.getElementById('closeSplitModal').addEventListener('click', () => hideModal(splitModal));
    splitModal.addEventListener('click', (e) => { if (e.target === splitModal) hideModal(splitModal); });
    document.getElementById('splitBtn').addEventListener('click', async (e) => {
        if (!splitProposal) return;
        const btn = e.currentTarget;
        const msg = document.getElementById('splitStatus');
        const books = [...document.querySelectorAll('.split-check:checked')].map(cb => {
            const i = cb.dataset.index;
            const original = splitProposal.groups.find(g => String(g.index) === i);
            const title = document.querySelector(`.split-title[data-index="${i}"]`).value.trim();
            return { index: Number(i), number: document.querySelector(`.split-number[data-index="${i}"]`).value.trim(),
                     title: title !== original.title ? title : '' };
        });
        btn.disabled = true;
        setActionStatus(msg, 'Creating the folders…');
        const { ok, data } = await postJSON(`/api/library/${encodeURIComponent(splitProposal.book_id)}/split`, { books });
        if (!ok) {
            setActionStatus(msg, data.detail || 'Split failed', 'error');
            btn.disabled = false;
            return;
        }
        hideModal(splitModal);
        hideModal(bookModal);
        await fetchLibrary();
        renderLibrary();
        toast(`Split into ${data.created} books. The original folder is unchanged; remove it when you're ready.`, 'ok');
    });
}


// -----------------
// Organize: rename existing folders to the Book Folder Format, approving each change
// -----------------
const organizeModal = document.getElementById('organizeModal');
let organizePlan = null;

function organizeRowHtml(item) {
    const files = item.files.length
        ? `<details class="organize-files"><summary>${item.files.length} audio file${item.files.length === 1 ? '' : 's'} renamed</summary>
            ${item.files.map(f => `<div><span class="old">${esc(f.from)}</span> → <span class="new">${esc(f.to)}</span></div>`).join('')}</details>` : '';
    const action = item.conflict
        ? `<span class="organize-conflict">${esc(item.conflict)}</span>`
        : `<button class="secondary-btn organize-approve" data-id="${esc(item.book_id)}">Approve</button>`;
    return `<div class="organize-item" data-id="${esc(item.book_id)}">
        <div class="organize-paths">
            <div class="organize-title">${esc(item.title)} <span class="muted">· ${esc(primaryAuthor(item.authors))}</span></div>
            <div class="old" title="${esc(item.current)}">${esc(item.current_rel)}</div>
            <div class="new" title="${esc(item.proposed)}">→ ${esc(item.proposed_rel)}</div>
            ${files}
        </div>
        <div class="organize-action">${action}</div>
    </div>`;
}

async function loadOrganizePlan() {
    const list = document.getElementById('organizeList');
    const loader = document.getElementById('organizeLoader');
    const renameFiles = document.getElementById('organizeRenameFiles').checked;
    list.innerHTML = '';
    setActionStatus(document.getElementById('organizeStatus'), '');
    loader.classList.remove('hidden');
    const res = await fetch(`/api/organize?rename_files=${renameFiles}`);
    organizePlan = await res.json().catch(() => null);
    loader.classList.add('hidden');
    if (!res.ok || !organizePlan) {
        list.innerHTML = '<p class="muted">Couldn\'t work out the changes.</p>';
        return;
    }
    document.getElementById('organizeFormat').textContent = organizePlan.format || '';
    const ready = organizePlan.items.filter(i => !i.conflict).length;
    const conflicts = organizePlan.items.length - ready;
    document.getElementById('organizeSummary').textContent = `${organizePlan.items.length} to change`
        + (conflicts ? ` (${conflicts} can't be: see why)` : '') + ` · ${organizePlan.unchanged} already match`;
    list.innerHTML = organizePlan.items.length
        ? organizePlan.items.map(organizeRowHtml).join('')
        : '<p class="muted">Every book already matches your Book Folder Format.</p>';
    list.querySelectorAll('.organize-approve').forEach(btn => btn.addEventListener('click', () => approveOrganize([btn.dataset.id])));
    updateOrganizeAll();
}

function updateOrganizeAll() {
    const n = document.querySelectorAll('.organize-approve:not(:disabled)').length;
    const btn = document.getElementById('organizeAllBtn');
    btn.disabled = n === 0;
    btn.textContent = n ? `Approve All (${n})` : 'Approve All';
}

async function approveOrganize(ids) {
    const renameFiles = document.getElementById('organizeRenameFiles').checked;
    ids.forEach(id => {
        const btn = document.querySelector(`.organize-approve[data-id="${CSS.escape(id)}"]`);
        if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
    });
    const { ok, data } = await postJSON('/api/organize', { book_ids: ids, rename_files: renameFiles });
    if (!ok) {
        setActionStatus(document.getElementById('organizeStatus'), data.detail || 'Organize failed', 'error');
        return;
    }
    let done = 0;
    data.results.forEach(r => {
        const row = document.querySelector(`.organize-item[data-id="${CSS.escape(r.book_id)}"]`);
        if (!row) return;
        const action = row.querySelector('.organize-action');
        if (r.ok) {
            done++;
            row.classList.add('done');
            action.innerHTML = '<span class="organize-done">Done</span>';
        } else {
            action.innerHTML = `<span class="organize-conflict">${esc(r.error)}</span>`;
        }
    });
    setActionStatus(document.getElementById('organizeStatus'), `${done} book${done === 1 ? '' : 's'} organized`, done ? 'ok' : 'error');
    updateOrganizeAll();
    await fetchLibrary();
    renderLibrary();
}

function setupOrganizeModal() {
    document.getElementById('organizeBtn').addEventListener('click', () => {
        showModal(organizeModal);
        loadOrganizePlan();
    });
    document.getElementById('closeOrganizeModal').addEventListener('click', () => hideModal(organizeModal));
    organizeModal.addEventListener('click', (e) => { if (e.target === organizeModal) hideModal(organizeModal); });
    document.getElementById('organizeRenameFiles').addEventListener('change', loadOrganizePlan);
    document.getElementById('organizeAllBtn').addEventListener('click', async () => {
        const ids = [...document.querySelectorAll('.organize-approve:not(:disabled)')].map(b => b.dataset.id);
        if (!ids.length) return;
        const confirmed = await confirmDialog(`Rename or move ${ids.length} folder${ids.length === 1 ? '' : 's'} as shown?`,
            { title: 'Organize library', confirmText: 'Approve All' });
        if (confirmed) approveOrganize(ids);
    });
}


// -----------------
// System: library health (and stats)
// -----------------
let healthData = null;
let healthFilter = '';
let healthDeepTimer = null;

function showSystemTab(tab) {
    document.querySelectorAll('.system-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.system-section').forEach(s => { s.hidden = s.dataset.tab !== tab; });
    if (tab === 'health') renderHealth();
    if (tab === 'stats' && typeof renderStats === 'function') renderStats();
}

const HEALTH_FIX_LABELS = { open: 'Open', match: 'Match on Audible', cover: 'Get Cover', split: 'Split', activity: 'Review' };
const HEALTH_BULK = { match: 'Match All on Audible', cover: 'Get All Covers' };

async function renderHealth() {
    const loader = document.getElementById('healthLoader');
    loader.classList.remove('hidden');
    const res = await fetch('/api/health');
    healthData = await res.json().catch(() => null);
    loader.classList.add('hidden');
    if (!healthData) return;
    drawHealth();
    updateHealthDeep(healthData.deep);
}

function drawHealth() {
    const { issues, counts, kinds } = healthData;
    const books = new Set(issues.map(i => i.book_id)).size;
    document.getElementById('healthSummary').textContent = issues.length
        ? `${issues.length} issue${issues.length === 1 ? '' : 's'} across ${books} book${books === 1 ? '' : 's'}`
        : 'No issues found.';
    document.getElementById('healthChips').innerHTML = Object.entries(counts).filter(([, n]) => n).map(([kind, n]) =>
        `<button class="health-chip sev-${kinds[kind].severity}${healthFilter === kind ? ' active' : ''}" data-kind="${kind}">${esc(kinds[kind].label)} <b>${n}</b></button>`).join('');
    document.querySelectorAll('.health-chip').forEach(chip => chip.addEventListener('click', () => {
        healthFilter = healthFilter === chip.dataset.kind ? '' : chip.dataset.kind;
        drawHealth();
    }));

    const groups = {};
    issues.filter(i => !healthFilter || i.kind === healthFilter).forEach(i => (groups[i.kind] = groups[i.kind] || []).push(i));
    const list = document.getElementById('healthList');
    list.innerHTML = Object.keys(groups).length ? Object.entries(groups).map(([kind, items]) => {
        const k = kinds[kind];
        const bulk = HEALTH_BULK[k.fix] && items.length > 1
            ? `<button class="secondary-btn health-bulk" data-kind="${kind}">${HEALTH_BULK[k.fix]} (${items.length})</button>` : '';
        return `<div class="health-group sev-${k.severity}">
            <div class="health-group-head"><h3>${esc(k.label)} <span class="muted">${items.length}</span></h3>${bulk}</div>
            ${items.map((i, n) => `<div class="health-row">
                <div class="health-book"><b>${bookLink(i.book_id, i.title)}</b> <span class="muted">· ${esc(primaryAuthor(i.authors))}</span>
                    ${i.detail ? `<div class="muted health-detail">${esc(i.detail)}</div>` : ''}</div>
                <button class="link-btn health-fix" data-kind="${kind}" data-index="${n}">${HEALTH_FIX_LABELS[k.fix]}</button>
            </div>`).join('')}
        </div>`;
    }).join('') : '<div class="empty-state"><h3>All good</h3><p>Nothing in your library needs attention.</p></div>';

    list.querySelectorAll('.health-fix').forEach(btn => btn.addEventListener('click', () =>
        fixHealth(groups[btn.dataset.kind][btn.dataset.index].fix, [groups[btn.dataset.kind][btn.dataset.index].book_id], btn)));
    list.querySelectorAll('.health-bulk').forEach(btn => btn.addEventListener('click', () =>
        fixHealth(kinds[btn.dataset.kind].fix, groups[btn.dataset.kind].map(i => i.book_id), btn)));
}

async function fixHealth(fix, ids, btn) {
    if (fix === 'open') return openBookModal(ids[0]);
    if (fix === 'split') return openSplitDialog(ids[0]);
    if (fix === 'activity') return navigate('/activity');
    btn.disabled = true;
    if (fix === 'match') {
        const { ok, data } = await postJSON('/api/library/bulk', { ids, action: 'match' });
        if (!ok) { toast(data.detail || 'Match failed', 'error'); btn.disabled = false; return; }
        toast(`Matching ${ids.length} book${ids.length === 1 ? '' : 's'} on Audible in the background…`);
        await watchMatchJob();
    } else if (fix === 'cover') {
        const { data } = await postJSON('/api/covers', { ids });
        toast(`Saved ${data.fetched} of ${data.count} cover${data.count === 1 ? '' : 's'} from Audible`, data.fetched ? 'ok' : 'error');
    }
    await fetchLibrary();
    renderHealth();
}

function updateHealthDeep(job) {
    const status = document.getElementById('healthDeepStatus');
    const btn = document.getElementById('healthDeepBtn');
    clearTimeout(healthDeepTimer);
    if (job && job.running) {
        btn.disabled = true;
        status.textContent = `Checking files… ${job.done} of ${job.total} books`;
        healthDeepTimer = setTimeout(async () => {
            if (document.getElementById('systemView').hidden) return;
            const next = await fetch('/api/health/deep').then(r => r.json()).catch(() => null);
            if (next && !next.running) renderHealth(); else updateHealthDeep(next);
        }, 2000);
    } else {
        btn.disabled = false;
        status.textContent = job && job.checked ? `Files last checked ${new Date(job.checked).toLocaleString()}` : 'Files not checked yet';
    }
}

function setupSystem() {
    document.querySelectorAll('.system-tab').forEach(tab => tab.addEventListener('click', () => navigate('/system/' + tab.dataset.tab)));
    document.getElementById('healthRefreshBtn').addEventListener('click', renderHealth);
    document.getElementById('healthDeepBtn').addEventListener('click', async () => {
        const { data } = await postJSON('/api/health/deep');
        updateHealthDeep({ ...data, running: true });
    });
}


// -----------------
// Settings > Indexers: AudiobookBay (cookie etc.) and Torznab indexers
// -----------------
let abbCookieClearing = false;

function updateAbbCookieState() {
    const state = document.getElementById('abbCookieState');
    const clear = document.getElementById('abbCookieClear');
    const saved = appSettings.abb_cookie_set && !abbCookieClearing;
    state.textContent = abbCookieClearing ? 'The saved cookie will be removed when you save.'
        : saved ? 'A cookie is saved (it is never shown again). Paste a new one to replace it.'
        : appSettings.abb_cookie_env ? 'Using the cookie from the ABB_COOKIE environment variable. One saved here takes its place.'
        : 'No cookie: searching mostly works without one, but logging in helps against Cloudflare checks.';
    clear.hidden = !saved;
}

let indexerList = [];

function drawIndexers() {
    const box = document.getElementById('indexerList');
    box.innerHTML = indexerList.length ? indexerList.map(ix => `<div class="indexer-row" data-id="${esc(ix.id)}">
        <label class="check-label" title="Search this indexer"><input type="checkbox" class="ix-enabled" ${ix.enabled ? 'checked' : ''}></label>
        <div class="indexer-info"><b>${esc(ix.name)}</b><div class="muted">${esc(ix.url)} · categories ${esc(ix.categories)}${ix.api_key_set ? ' · API key saved' : ''}</div></div>
        <button type="button" class="link-btn ix-edit">Edit</button>
        <button type="button" class="link-btn ix-remove">Remove</button>
    </div>`).join('') : '<p class="muted">No indexers yet.</p>';
    box.querySelectorAll('.indexer-row').forEach(row => {
        const ix = indexerList.find(i => i.id === row.dataset.id);
        row.querySelector('.ix-enabled').addEventListener('change', async (e) => {
            const { data } = await postJSON('/api/indexers', { ...ix, enabled: e.target.checked, api_key: '' });
            indexerList = data.indexers || indexerList;
            toast(`${ix.name} ${e.target.checked ? 'enabled' : 'disabled'}`, 'ok');
        });
        row.querySelector('.ix-edit').addEventListener('click', () => fillIndexerForm(ix));
        row.querySelector('.ix-remove').addEventListener('click', async () => {
            if (!await confirmDialog(`Remove ${ix.name}?`, { title: 'Remove indexer', confirmText: 'Remove', danger: true })) return;
            const res = await fetch(`/api/indexers/${encodeURIComponent(ix.id)}`, { method: 'DELETE' });
            indexerList = (await res.json()).indexers || [];
            drawIndexers();
        });
    });
}

function fillIndexerForm(ix) {
    document.getElementById('ixId').value = ix ? ix.id : '';
    document.getElementById('ixName').value = ix ? ix.name : '';
    document.getElementById('ixUrl').value = ix ? ix.url : '';
    document.getElementById('ixKey').value = '';
    document.getElementById('ixKey').placeholder = ix && ix.api_key_set ? 'Unchanged' : '';
    document.getElementById('ixCats').value = ix ? ix.categories : '';
    document.getElementById('ixSaveBtn').textContent = ix ? 'Save Indexer' : 'Add Indexer';
    document.getElementById('ixCancelBtn').hidden = !ix;
    setActionStatus(document.getElementById('ixStatus'), '');
}

function indexerFormValues() {
    return {
        id: document.getElementById('ixId').value || undefined,
        name: document.getElementById('ixName').value.trim(),
        url: document.getElementById('ixUrl').value.trim(),
        api_key: document.getElementById('ixKey').value.trim(),
        categories: document.getElementById('ixCats').value.trim(),
        enabled: true,
    };
}

async function loadIndexers() {
    const res = await fetch('/api/indexers');
    indexerList = (await res.json().catch(() => ({}))).indexers || [];
    drawIndexers();
}

function setupIndexers() {
    loadIndexers();
    document.getElementById('abbCookieClear').addEventListener('click', () => {
        abbCookieClearing = true;
        updateAbbCookieState();
        markSettingsDirty();
    });
    document.getElementById('abbTestBtn').addEventListener('click', async (e) => {
        const status = document.getElementById('abbTestStatus');
        const btn = e.currentTarget;
        btn.disabled = true;
        setActionStatus(status, 'Testing…');
        const { ok, data } = await postJSON('/api/abb/test', {
            url: document.getElementById('setAbbUrl').value.trim(),
            cookie: document.getElementById('setAbbCookie').value.trim(),
            user_agent: document.getElementById('setAbbUserAgent').value.trim(),
        });
        btn.disabled = false;
        setActionStatus(status, ok ? data.message : (data.detail || 'Test failed'), ok && data.ok ? (data.logged_in ? 'ok' : '') : 'error');
    });
    document.getElementById('ixSaveBtn').addEventListener('click', async () => {
        const status = document.getElementById('ixStatus');
        const { ok, data } = await postJSON('/api/indexers', indexerFormValues());
        if (!ok) return setActionStatus(status, data.detail || 'Could not save', 'error');
        indexerList = data.indexers;
        drawIndexers();
        fillIndexerForm(null);
        setActionStatus(status, 'Saved', 'ok');
    });
    document.getElementById('ixTestBtn').addEventListener('click', async (e) => {
        const status = document.getElementById('ixStatus');
        const btn = e.currentTarget;
        btn.disabled = true;
        setActionStatus(status, 'Testing…');
        const { ok, data } = await postJSON('/api/indexers/test', indexerFormValues());
        btn.disabled = false;
        setActionStatus(status, ok ? data.message : (data.detail || 'Test failed'), ok && data.ok ? 'ok' : 'error');
    });
    document.getElementById('ixCancelBtn').addEventListener('click', () => fillIndexerForm(null));
}


// -----------------
// Authors: an author's books, and following them (new releases added automatically)
// -----------------
let currentAuthor = null;

function openAuthorPage(name) {
    document.querySelectorAll('.modal.show').forEach(hideModal);
    navigate('/author/' + encodeURIComponent(name));
}

async function showAuthorPage(name) {
    showView('authorView', 'seriesView');
    const box = document.getElementById('authorDetail');
    box.innerHTML = '<div class="loader"><div class="spinner"></div></div>';
    const res = await fetch(`/api/authors/detail?name=${encodeURIComponent(name)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        box.innerHTML = `<div class="empty-state"><h3>Couldn't load ${esc(name)}</h3><p>${esc(data.detail || 'Try again in a moment.')}</p></div>`;
        return;
    }
    currentAuthor = data;
    drawAuthorPage();
}

async function refreshAuthorPage() {
    const main = document.querySelector('.main-content');
    const scroll = { window: window.scrollY, main: main.scrollTop };
    const res = await fetch(`/api/authors/detail?name=${encodeURIComponent(currentAuthor.name)}`);
    if (!res.ok) return;
    currentAuthor = await res.json();
    await fetchLibrary();
    drawAuthorPage();
    main.scrollTop = scroll.main;
    window.scrollTo(0, scroll.window);
}

function drawAuthorPage() {
    const a = currentAuthor;
    const f = a.followed;
    const actions = f
        ? `<label class="checkbox-label"><input type="checkbox" id="authorMonitored" ${f.monitored ? 'checked' : ''}> Following</label>
           <button class="secondary-btn" id="authorSyncBtn">Sync</button>
           <button class="danger-btn" id="authorUnfollowBtn">Unfollow</button>`
        : `<button class="primary-btn" id="authorFollowOpen">Follow Author</button>`;
    document.getElementById('authorDetail').innerHTML = `
        <div class="series-hero">
            <div class="series-hero-info">
                <h2>${esc(a.name)}</h2>
                <div class="series-hero-stats">
                    <span class="stat"><b>${a.books.length}</b> on Audible</span>
                    <span class="stat"><b>${a.in_library}</b> in library</span>
                    <span class="stat"><b>${a.owned}</b> on disk</span>
                    ${a.upcoming ? `<span class="stat"><b>${a.upcoming}</b> upcoming</span>` : ''}
                    ${f ? `<span class="stat">${f.monitored ? 'Following' : 'Paused'}${f.last_sync ? ' · checked ' + esc(new Date(f.last_sync).toLocaleDateString()) : ''}</span>` : ''}
                </div>
                <div class="series-hero-actions">${actions}</div>
            </div>
        </div>
        ${a.books.length ? `<div class="table-container series-books-table"><table class="data-table">
            <thead><tr><th class="col-date">Released</th><th>Title</th><th>Series</th><th class="col-len">Length</th><th class="col-status">Status</th></tr></thead>
            <tbody>${a.books.map((b, i) => `<tr class="${b.book_id ? 'clickable' : 'not-owned'}" data-index="${i}">
                <td class="muted nowrap">${esc(releaseDate(b.release_date))}${b.upcoming ? ' <span class="edition-badge edition-upcoming">Upcoming</span>' : ''}</td>
                <td>${bookLink(b.book_id, b.title)} ${editionOf(b) !== 'narrated' ? `<span class="edition-badge edition-${editionOf(b)}">${EDITION_LABELS[editionOf(b)]}</span>` : ''}</td>
                <td class="muted">${esc(seriesLabel(b))}</td>
                <td class="muted nowrap">${esc(formatRuntime(b.runtime_min))}</td>
                <td class="nowrap">${b.book_id ? `<span class="library-status ${esc(statusClass(b.status))} inline-status">${esc(b.status)}</span>`
                    : `<span class="muted">Not in library</span> <button class="link-btn author-add" data-index="${i}">Add</button>`}</td>
            </tr>`).join('')}</tbody></table></div>`
        : '<div class="empty-state"><h3>No books found</h3><p>Audible lists no books by this name (in your language setting).</p></div>'}`;

    const box = document.getElementById('authorDetail');
    box.querySelectorAll('tr.clickable').forEach(tr => tr.addEventListener('click', () => openBookModal(a.books[tr.dataset.index].book_id)));
    box.querySelectorAll('.author-add').forEach(btn => btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        btn.disabled = true;
        const book = a.books[btn.dataset.index];
        const { ok, data } = await postJSON('/api/library', book);
        if (!ok) { toast(data.detail || 'Could not add the book', 'error'); btn.disabled = false; return; }
        toast(`Added "${book.title}" (${data.status})`, 'ok');
        refreshAuthorPage();
    }));
    document.getElementById('authorFollowOpen')?.addEventListener('click', openFollowDialog);
    document.getElementById('authorMonitored')?.addEventListener('change', async (e) => {
        await postJSON(`/api/authors/${encodeURIComponent(f.id)}`, { monitored: e.target.checked }, 'PATCH');
        toast(e.target.checked ? `Following ${a.name}` : `Paused: new books by ${a.name} won't be added`, 'ok');
    });
    document.getElementById('authorSyncBtn')?.addEventListener('click', async (e) => {
        e.currentTarget.disabled = true;
        const { ok, data } = await postJSON(`/api/authors/${encodeURIComponent(f.id)}/sync`);
        toast(ok ? `${data.added} new book${data.added === 1 ? '' : 's'} added` : (data.detail || 'Sync failed'), ok ? 'ok' : 'error');
        refreshAuthorPage();
    });
    document.getElementById('authorUnfollowBtn')?.addEventListener('click', async () => {
        if (!await confirmDialog(`Stop following ${a.name}?\n\nTheir new books won't be added any more. Books already in your library stay.`,
            { title: 'Unfollow author', confirmText: 'Unfollow', danger: true })) return;
        await fetch(`/api/authors/${encodeURIComponent(f.id)}`, { method: 'DELETE' });
        refreshAuthorPage();
    });
}

function authorPicked() {
    return [...document.querySelectorAll('#authorPickList input:checked')].map(cb => cb.value);
}

function updateFollowButton() {
    const n = authorPicked().length;
    document.getElementById('authorFollowBtn').textContent = n ? `Follow & Add ${n} Book${n === 1 ? '' : 's'}` : 'Follow (New Books Only)';
}

function openFollowDialog() {
    const a = currentAuthor;
    document.getElementById('authorModalName').textContent = a.name;
    setActionStatus(document.getElementById('authorModalStatus'), '');
    const list = document.getElementById('authorPickList');
    list.innerHTML = a.candidates.length ? a.candidates.map(b => `
        <label class="pick-row">
            <input type="checkbox" value="${esc(b.asin)}" ${b.upcoming ? 'checked' : ''}>
            <span class="muted pick-seq">${esc(releaseDate(b.release_date, 4))}</span>
            <span class="pick-title">${esc(b.title)}${b.series ? ` <span class="muted">(${esc(seriesLabel(b))})</span>` : ''}${b.upcoming ? ' <span class="edition-badge edition-upcoming">Upcoming</span>' : ''}</span>
        </label>`).join('') : '<p class="muted">You already have every book by this author (in the editions your settings ask for).</p>';
    list.querySelectorAll('input').forEach(cb => cb.addEventListener('change', updateFollowButton));
    updateFollowButton();
    showModal(document.getElementById('authorModal'));
}

async function renderFollowedAuthors() {
    const box = document.getElementById('authorsList');
    const data = await fetch('/api/authors').then(r => r.json()).catch(() => ({ authors: [] }));
    box.innerHTML = data.authors.length ? data.authors.map(a => `<button class="author-card" data-author="${esc(a.name)}">
        <img src="${esc(safeUrl(a.cover, PLACEHOLDER_COVER))}" alt="" loading="lazy" onerror="this.src='${PLACEHOLDER_COVER}'">
        <span class="author-card-text"><b>${esc(a.name)}</b>
            <span class="muted">${a.in_library} in library · ${a.on_disk} on disk${a.monitored ? '' : ' · paused'}</span>
            ${a.last_sync ? `<span class="muted">Checked ${esc(new Date(a.last_sync).toLocaleDateString())}</span>` : ''}</span>
    </button>`).join('') : '<div class="empty-state"><h3>No followed authors</h3><p>Open an author from a book\'s details, a series page or Search, then Follow.</p></div>';
    box.querySelectorAll('.author-card').forEach(card => card.addEventListener('click', () => openAuthorPage(card.dataset.author)));
}

function setupAuthors() {
    // Author links anywhere (search results, series pages, calendar)
    document.addEventListener('click', (e) => {
        const link = e.target.closest('.author-link');
        if (!link) return;
        e.preventDefault();
        e.stopPropagation();
        openAuthorPage(link.dataset.author);
    }, true);
    document.getElementById('authorBackBtn').addEventListener('click', () => history.back());
    document.getElementById('followedAuthorsBtn').addEventListener('click', () => navigate('/authors'));
    const modal = document.getElementById('authorModal');
    document.getElementById('closeAuthorModal').addEventListener('click', () => hideModal(modal));
    modal.addEventListener('click', (e) => { if (e.target === modal) hideModal(modal); });
    document.getElementById('authorPickAll').addEventListener('click', () => {
        document.querySelectorAll('#authorPickList input').forEach(cb => { cb.checked = true; });
        updateFollowButton();
    });
    document.getElementById('authorPickNone').addEventListener('click', () => {
        document.querySelectorAll('#authorPickList input').forEach(cb => { cb.checked = false; });
        updateFollowButton();
    });
    document.getElementById('authorFollowBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        setActionStatus(document.getElementById('authorModalStatus'), 'Following…');
        const { ok, data } = await postJSON('/api/authors', { name: currentAuthor.name, add_asins: authorPicked() });
        btn.disabled = false;
        if (!ok) return setActionStatus(document.getElementById('authorModalStatus'), data.detail || 'Failed', 'error');
        hideModal(modal);
        toast(`Following ${currentAuthor.name}${data.added ? `: added ${data.added} book${data.added === 1 ? '' : 's'}` : ''}`, 'ok');
        refreshAuthorPage();
    });
}


// -----------------
// Import a reading list (Goodreads / StoryGraph CSV)
// -----------------
const listModal = document.getElementById('listModal');
let listTimer = null;
const listUnticked = new Set();  // Matches you untick; the rest start ticked as they come in

function listChecked() {
    return [...document.querySelectorAll('.list-check:checked')].map(cb => cb.value);
}

function updateListAdd() {
    const n = listChecked().length;
    const btn = document.getElementById('listAddBtn');
    btn.disabled = n === 0;
    btn.textContent = n ? `Add ${n} Book${n === 1 ? '' : 's'}` : 'Add';
}

function drawListResults(job) {
    const rows = document.getElementById('listRows');
    rows.innerHTML = job.results.map(r => {
        const m = r.match;
        const status = r.in_library ? `<span class="library-status ${esc(statusClass(r.in_library))} inline-status">${esc(r.in_library)}</span>`
            : m ? '<span class="muted">New</span>' : '<span class="muted">No clear match</span>';
        return `<tr>
            <td>${m && !r.in_library ? `<input type="checkbox" class="list-check" value="${esc(m.asin)}" ${listUnticked.has(m.asin) ? '' : 'checked'}>` : ''}</td>
            <td><b>${esc(r.list_title)}</b><div class="muted">${esc(r.list_author)}</div></td>
            <td>${m ? `<div class="list-match"><img src="${esc(safeUrl(m.imageUrl, PLACEHOLDER_COVER))}" alt="" loading="lazy">
                <span><b>${esc(m.title)}</b><span class="muted">${esc([seriesLabel(m), formatRuntime(m.runtime_min), releaseDate(m.release_date, 4)].filter(Boolean).join(' · '))}</span></span></div>`
                : '<span class="muted">—</span>'}</td>
            <td class="nowrap">${status}</td>
        </tr>`;
    }).join('');
    rows.querySelectorAll('.list-check').forEach(cb => cb.addEventListener('change', () => {
        cb.checked ? listUnticked.delete(cb.value) : listUnticked.add(cb.value);
        updateListAdd();
    }));
    updateListAdd();
}

async function watchListJob() {
    clearTimeout(listTimer);
    const job = await fetch('/api/lists/match').then(r => r.json()).catch(() => null);
    if (!job) return;
    document.getElementById('listTable').hidden = false;
    drawListResults(job);
    const found = job.results.filter(r => r.match).length;
    setActionStatus(document.getElementById('listStatus'), job.running
        ? `Looking up books on Audible… ${job.done} of ${job.total}`
        : `${found} of ${job.total} found on Audible`, job.running ? '' : 'ok');
    if (job.running && listModal.classList.contains('show')) listTimer = setTimeout(watchListJob, 1500);
}

function setupListImport() {
    document.getElementById('listImportBtn').addEventListener('click', () => showModal(listModal));
    document.getElementById('closeListModal').addEventListener('click', () => hideModal(listModal));
    listModal.addEventListener('click', (e) => { if (e.target === listModal) hideModal(listModal); });
    document.getElementById('listFile').addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const info = document.getElementById('listInfo');
        const shelf = document.getElementById('listShelf');
        info.textContent = 'Reading…';
        const { ok, data } = await postJSON('/api/lists/parse', { csv: await file.text() });
        if (!ok) {
            info.textContent = data.detail || "Couldn't read that file.";
            shelf.hidden = document.getElementById('listMatchBtn').hidden = true;
            return;
        }
        info.textContent = `${data.format} list with ${data.count} book${data.count === 1 ? '' : 's'}. Choose a shelf:`;
        shelf.innerHTML = Object.entries(data.shelves).map(([name, n]) =>
            `<option value="${esc(name)}"${name === 'to-read' ? ' selected' : ''}>${esc(name)} (${n})</option>`).join('');
        shelf.hidden = false;
        document.getElementById('listMatchBtn').hidden = false;
    });
    document.getElementById('listMatchBtn').addEventListener('click', async () => {
        const { ok, data } = await postJSON('/api/lists/match', { shelf: document.getElementById('listShelf').value });
        if (!ok) return setActionStatus(document.getElementById('listStatus'), data.detail || 'Failed', 'error');
        listUnticked.clear();
        watchListJob();
    });
    document.getElementById('listSelectAll').addEventListener('change', (e) => {
        document.querySelectorAll('.list-check').forEach(cb => {
            cb.checked = e.target.checked;
            cb.checked ? listUnticked.delete(cb.value) : listUnticked.add(cb.value);
        });
        updateListAdd();
    });
    document.getElementById('listAddBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        const { ok, data } = await postJSON('/api/lists/add', { asins: listChecked(), status: document.getElementById('listAddAs').value });
        if (!ok) { setActionStatus(document.getElementById('listStatus'), data.detail || 'Failed', 'error'); btn.disabled = false; return; }
        toast(`Added ${data.added} book${data.added === 1 ? '' : 's'} from your list`, 'ok');
        await fetchLibrary();
        renderLibrary();
        watchListJob();
    });
}


// -----------------
// System > Stats
// -----------------
function statBars(title, items, unit = '') {
    if (!items || !items.length) return '';
    const max = Math.max(...items.map(i => i.value), 1);
    return `<div class="stats-card"><h3>${esc(title)}</h3>${items.map(i => `<div class="stat-bar">
        <span class="stat-bar-name" title="${esc(i.name)}">${esc(i.name)}</span>
        <span class="stat-bar-track"><span class="stat-bar-fill" style="width:${Math.max(2, i.value / max * 100)}%"></span></span>
        <span class="stat-bar-value">${esc(i.value.toLocaleString())}${unit}</span>
    </div>`).join('')}</div>`;
}

function growthChart(months) {
    const max = Math.max(...months.map(m => m.total), 1);
    const w = 600, h = 140, bw = w / months.length;
    const bars = months.map((m, i) => {
        const th = m.total / max * (h - 20), ah = m.added / max * (h - 20);
        const label = new Date(m.month + '-01T00:00:00').toLocaleDateString(undefined, { month: 'short', year: '2-digit' });
        return `<g><title>${esc(label)}: ${m.total} books (${m.added} added)</title>
            <rect x="${i * bw + 2}" y="${h - th}" width="${bw - 4}" height="${th}" class="growth-total"></rect>
            <rect x="${i * bw + 2}" y="${h - ah}" width="${bw - 4}" height="${ah}" class="growth-added"></rect></g>`;
    }).join('');
    const first = new Date(months[0].month + '-01T00:00:00').toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
    return `<div class="stats-card stats-wide"><h3>Library growth <span class="muted">(last 24 months; orange: added that month)</span></h3>
        <svg viewBox="0 0 ${w} ${h}" class="growth-chart" preserveAspectRatio="none" role="img" aria-label="Library growth">${bars}</svg>
        <div class="growth-axis"><span>${esc(first)}</span><span>Now: ${months[months.length - 1].total} books</span></div></div>`;
}

async function renderStats() {
    const body = document.getElementById('statsBody');
    const d = await fetch('/api/stats').then(r => r.json()).catch(() => null);
    if (!d) { body.innerHTML = '<p class="muted">Couldn\'t load stats.</p>'; return; }
    const t = d.totals;
    const card = (label, value, hint = '') => `<div class="stat-tile"><b>${esc(value)}</b><span>${esc(label)}</span>${hint ? `<small>${esc(hint)}</small>` : ''}</div>`;
    const dl = d.downloads;
    const dlRows = [['Grabbed', 'grabbed'], ['Imported', 'imported'], ['Held for review', 'needs_review'], ['Approved', 'approved'],
        ['Rejected', 'rejected'], ['Stalled', 'stalled'], ['Failed', 'failed']];
    body.innerHTML = `
        <div class="stat-tiles">
            ${card('Books', t.books.toLocaleString())}
            ${card('On disk', t.on_disk.toLocaleString())}
            ${card('Hours on disk', t.hours.toLocaleString(), `${(t.hours / 24).toFixed(1)} days`)}
            ${card('Size on disk', formatSize(t.size_bytes))}
            ${card('Wanted', t.wanted.toLocaleString(), t.upcoming ? `+ ${t.upcoming} upcoming` : '')}
            ${card('Authors', t.authors.toLocaleString())}
            ${card('Series', t.series.toLocaleString())}
            ${card('Matched on Audible', t.books ? Math.round(t.matched / t.books * 100) + '%' : '—')}
        </div>
        <div class="stats-grid">
            ${growthChart(d.growth)}
            ${statBars('Top authors (hours on disk)', d.top_authors, ' h')}
            ${statBars('Top narrators (hours on disk)', d.top_narrators, ' h')}
            ${statBars('Top series (books)', d.top_series)}
            ${statBars('Status', d.statuses)}
            ${statBars('Editions', d.editions)}
            ${statBars('Formats (on disk)', d.formats)}
            ${statBars('Languages', d.languages)}
            <div class="stats-card"><h3>Downloads</h3>
                <table class="data-table stats-table"><thead><tr><th></th><th>Last 30 days</th><th>All time</th></tr></thead>
                <tbody>${dlRows.map(([label, key]) => `<tr><td>${label}</td><td>${dl[key].recent}</td><td>${dl[key].all}</td></tr>`).join('')}</tbody></table>
            </div>
            ${statBars('Books by release year', d.release_years.slice().reverse().slice(0, 15))}
        </div>`;
}


// -----------------
// Convert to M4B: a queue converted one book at a time (Activity shows it); the
// originals are kept until deleted
// -----------------
let convertTimer = null;
let convertInfo = null;  // The open book's conversion info (from its files)

async function convertStatus() {
    return fetch('/api/convert').then(r => r.json()).catch(() => null);
}

// The book details' Convert button: queue it, or show its place in the queue / progress
async function updateConvertButtons(bookId, info) {
    if (info) convertInfo = info;
    if (!convertInfo || bookId !== currentBookId) return;
    const convertBtn = document.getElementById('convertBookBtn');
    const deleteBtn = document.getElementById('deleteOriginalsBtn');
    deleteBtn.hidden = !convertInfo.originals;
    deleteBtn.textContent = `Delete Originals (${convertInfo.originals}, ${formatSize(convertInfo.originals_bytes)})`;
    const st = await convertStatus();
    if (!st || bookId !== currentBookId) return;
    const queued = st.queue.find(q => q.book_id === bookId);
    const running = st.current && st.current.book_id === bookId;
    convertBtn.dataset.state = running ? 'running' : queued ? 'queued' : 'idle';
    convertBtn.textContent = running ? `Converting ${Math.round(st.current.progress * 100)}%… (Cancel)`
        : queued ? `Queued #${queued.position} (Remove)` : 'Convert to M4B';
    convertBtn.hidden = !st.available || (!running && !queued && Boolean(convertInfo.reason));
    clearTimeout(convertTimer);
    if ((running || queued) && bookModal.classList.contains('show')) {
        convertTimer = setTimeout(() => updateConvertButtons(bookId), 2000);
    } else if (convertBtn.dataset.wasActive === '1' && !running && !queued) {
        // Just finished: reload the book's files
        convertBtn.dataset.wasActive = '';
        await fetchLibrary();
        openBookModal(bookId);
        return;
    }
    convertBtn.dataset.wasActive = running || queued ? '1' : '';
}

function conversionRow(label, title, detail, action, bookId) {
    return `<div class="conversion-row">
        <div class="conversion-text"><b>${bookLink(bookId, title)}</b><span class="muted">${detail}</span></div>
        <span class="conversion-label">${label}</span>${action}</div>`;
}

// Activity: the book being converted, the queue, and recent results
async function renderConversions() {
    const section = document.getElementById('conversionSection');
    const st = await convertStatus();
    if (!st) return;
    const recent = st.recent.slice(0, 5);
    section.hidden = !st.current && !st.queue.length && !recent.length;
    drawConversions(st, recent, document.getElementById('conversionList'));
    drawConversions(st, recent, document.getElementById('settingsConversionList'));
}

function drawConversions(st, recent, list) {
    const resultLabels = { converted: 'Converted', error: 'Failed', cancelled: 'Cancelled', skipped: 'Skipped' };
    const rows = [];
    if (st.current) {
        rows.push(conversionRow('Converting', st.current.title,
            `<span class="conversion-bar"><span style="width:${Math.round(st.current.progress * 100)}%"></span></span> ${Math.round(st.current.progress * 100)}%`,
            `<button class="link-btn conversion-remove" data-id="${esc(st.current.book_id)}">Cancel</button>`, st.current.book_id));
    }
    st.queue.forEach(q => rows.push(conversionRow(`#${q.position}`, q.title,
        esc(q.source === 'auto' ? 'Queued after import' : q.source === 'bulk' ? 'Queued with others' : 'Queued by you'),
        `<button class="link-btn conversion-remove" data-id="${esc(q.book_id)}">Remove</button>`, q.book_id)));
    recent.forEach(r => rows.push(conversionRow(resultLabels[r.result] || r.result, r.title,
        esc([new Date(r.finished).toLocaleString(), r.message].filter(Boolean).join(' · ')), '', r.book_id)
        .replace('conversion-row', `conversion-row result-${esc(r.result)}`)));
    list.innerHTML = (st.available ? '' : '<p class="settings-hint">ffmpeg isn\'t installed, so nothing is converted (it\'s included in the Docker image).</p>')
        + (rows.join('') || '<p class="muted">Nothing queued.</p>');
    list.querySelectorAll('.conversion-remove').forEach(btn => btn.addEventListener('click', async () => {
        await fetch(`/api/convert/${encodeURIComponent(btn.dataset.id)}`, { method: 'DELETE' });
        renderConversions();
    }));
}

function setupConvert() {
    document.getElementById('convertBookBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        const book = appLibrary.find(b => b.id === currentBookId);
        if (btn.dataset.state === 'queued' || btn.dataset.state === 'running') {
            const running = btn.dataset.state === 'running';
            if (running && !await confirmDialog(`Cancel converting "${book.title}"? Its files stay as they are.`,
                { title: 'Cancel conversion', confirmText: 'Cancel Conversion', danger: true })) return;
            await fetch(`/api/convert/${encodeURIComponent(currentBookId)}`, { method: 'DELETE' });
            toast(running ? 'Conversion cancelled' : 'Removed from the conversion queue', 'ok');
            return updateConvertButtons(currentBookId);
        }
        if (!await confirmDialog(`Convert "${book.title}" to one M4B file?\n\nEach current file becomes a chapter, with the book's tags and cover. `
            + 'The new file must match the originals\' length, have a chapter per file and play through cleanly before anything changes. '
            + 'The originals are then kept (renamed to .original, which Audiobookshelf ignores) until you delete them, unless Settings say to delete them.',
            { title: 'Convert to M4B', confirmText: 'Add to Queue' })) return;
        const { ok, data } = await postJSON(`/api/library/${encodeURIComponent(currentBookId)}/convert`);
        if (!ok) return setActionStatus(document.getElementById('bookStatusMsg'), data.detail || 'Could not queue it', 'error');
        const position = data.queue.find(q => q.book_id === currentBookId);
        toast(position ? `Queued for conversion (#${position.position})` : 'Converting now', 'ok');
        updateConvertButtons(currentBookId);
    });
    document.getElementById('deleteOriginalsBtn').addEventListener('click', async () => {
        const book = appLibrary.find(b => b.id === currentBookId);
        if (!await confirmDialog(`Permanently delete the original files of "${book.title}"?\n\nOnly the files kept from before converting to M4B (*.original) are deleted. `
            + 'This can\'t be undone.', { title: 'Delete original files', confirmText: 'Delete', danger: true })) return;
        const { ok, data } = await postJSON(`/api/library/${encodeURIComponent(currentBookId)}/originals/delete`);
        if (!ok) return setActionStatus(document.getElementById('bookStatusMsg'), data.detail || 'Could not delete', 'error');
        toast(`Deleted ${data.deleted} original file${data.deleted === 1 ? '' : 's'} (${formatSize(data.bytes)})`, 'ok');
        openBookModal(currentBookId);
    });
    document.getElementById('bulkConvert').addEventListener('click', async () => {
        const data = await runBulk('convert');
        if (!data) return;
        toast(`${data.count} book${data.count === 1 ? '' : 's'} queued for conversion`
            + (data.skipped ? ` (${data.skipped} skipped: already M4B, queued, or not on disk)` : ''), data.count ? 'ok' : '');
        setSelectMode(false);
    });
    document.getElementById('convertQueueAllBtn').addEventListener('click', async () => {
        if (!await confirmDialog('Add every book on disk that isn\'t a single M4B to the conversion queue?\n\nThey\'re converted one at a time in the background; Activity shows the queue.',
            { title: 'Queue existing books', confirmText: 'Queue Them' })) return;
        const { ok, data } = await postJSON('/api/convert/queue_all');
        toast(ok ? `${data.added} book${data.added === 1 ? '' : 's'} queued for conversion` : (data.detail || 'Could not queue them'), ok ? 'ok' : 'error');
        renderConversions();
    });
}
