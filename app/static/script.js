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

// The main title for matching, like the server's: no bracketed tags, edition words or
// subtitle ("Title (Dramatized Adaptation)", "Title Graphic Audio")
function titleKey(title) {
    const plain = String(title || '').replace(/\s*[([][^)\]]*[)\]]/g, '')
        .replace(/\b(dramati[sz]ed|adaptation|graphic\s?audio|full[\s-]?cast|(un)?abridged)\b/gi, '')
        .trim().replace(/^-+|-+$/g, '').trim() || String(title || '');
    return normKey(plain.split(':')[0]);
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
    const key = titleKey(book.title);
    const authorKeys = authorKeyList(book.authors);
    const edition = editionOf(book);
    return appLibrary.find(b => titleKey(b.title) === key && editionOf(b) === edition
        && authorKeyList(b.authors).some(a => authorKeys.includes(a)));
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
    return book.sequence ? `${book.series} #${book.sequence}` : book.series;
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
    window.addEventListener('hashchange', route);
    route();
}

// Each page has its own address (#/library, #/series/<key>, ...), so the browser's
// Back and Forward buttons, refreshing and bookmarks all work
const PAGES = { search: 'searchView', library: 'libraryView', series: 'seriesView', calendar: 'calendarView', activity: 'activityView', settings: 'settingsView' };
const PAGE_NAMES = Object.fromEntries(Object.entries(PAGES).map(([name, view]) => [view, name]));

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
    activateView(PAGES[name]);
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
        document.getElementById('setHardlinks').checked = appSettings.use_hardlinks ?? true;
        document.getElementById('setStallHours').value = appSettings.stall_hours ?? 6;
        document.getElementById('setRemoveStalled').checked = appSettings.remove_stalled ?? true;
        document.getElementById('setVerifyRuntime').checked = appSettings.verify_runtime ?? true;
        document.getElementById('setRuntimeTolerance').value = appSettings.runtime_tolerance ?? 10;
        document.getElementById('setWriteMetadata').checked = appSettings.write_metadata ?? true;
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
            use_hardlinks: document.getElementById('setHardlinks').checked,
            stall_hours: parseInt(document.getElementById('setStallHours').value, 10) || 0,
            remove_stalled: document.getElementById('setRemoveStalled').checked,
            verify_runtime: document.getElementById('setVerifyRuntime').checked,
            runtime_tolerance: parseInt(document.getElementById('setRuntimeTolerance').value, 10) || 10,
            write_metadata: document.getElementById('setWriteMetadata').checked,
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
        section.addEventListener('input', markSettingsDirty);
        section.addEventListener('change', markSettingsDirty);
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

function showSettingsSection(name) {
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.section === name));
    document.querySelectorAll('.settings-section').forEach(sec => { sec.hidden = sec.dataset.section !== name; });
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

function librarySortKey(book, sort) {
    const seq = parseFloat(book.sequence);
    const seqKey = isNaN(seq) ? '9999' : String(seq.toFixed(2)).padStart(8, '0');
    const title = normKey(book.title);
    switch (sort) {
        case 'title': return title;
        case 'series': return `${book.series ? normKey(book.series) : '~'}|${seqKey}|${title}`;
        case 'added': return book.added || '';
        default: return `${normKey(primaryAuthor(book.authors)) || '~'}|${normKey(book.series)}|${seqKey}|${title}`;
    }
}

function renderLibrary() {
    const container = document.getElementById('libraryContainer');
    const loader = document.getElementById('libraryLoader');
    const stats = document.getElementById('libStats');
    const text = document.getElementById('libFilterText').value.trim().toLowerCase();
    const status = document.getElementById('libFilterStatus').value;
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
        } else if (status === '__dramatized' || status === '__abridged') {
            if (editionOf(b) !== status.slice(2)) return false;
        } else if (status === '__checkedition') {
            if (!b.edition_check) return false;
        } else if (status && b.status !== status) return false;
        if (!text) return true;
        return [b.title, b.authors, b.series, b.narrators].join(' ').toLowerCase().includes(text);
    });
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
            renderLibrary();
        });
        return;
    }

    const fragment = document.createDocumentFragment();
    books.forEach(book => {
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
    ['bulkStatus', 'bulkEdition', 'bulkMatch', 'bulkRemove'].forEach(id => { document.getElementById(id).disabled = selectedIds.size === 0; });
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

    ['libFilterText', 'libFilterStatus', 'libSort'].forEach(id => {
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

const IMPORT_STATES = {
    new: ['new', 'New'],
    link: ['link', 'Link to library'],
    in_library: ['owned', 'In library'],
};

function renderImportResults() {
    const tbody = document.getElementById('importResults');
    const summary = document.getElementById('importSummary');
    const counts = { new: 0, link: 0, in_library: 0 };
    importBooks.forEach(b => counts[b.state]++);
    const toCheck = importBooks.filter(b => b.edition_check && b.state !== 'in_library').length;
    summary.textContent = `${importBooks.length} books found: ${counts.new} new, ${counts.link} already tracked (will be linked to their files), ${counts.in_library} already imported.`
        + (toCheck ? ` ${toCheck} edition${toCheck === 1 ? '' : 's'} to check (highlighted).` : '');

    tbody.innerHTML = importBooks.map((b, i) => {
        const [badgeClass, label] = IMPORT_STATES[b.state];
        const hint = b.state === 'link' ? ` title="Will link to '${esc(b.match_title)}'"` : '';
        return `<tr>
            <td><input type="checkbox" class="import-check" data-index="${i}" ${b.state === 'in_library' ? 'disabled' : 'checked'}></td>
            <td>${esc(b.authors || '—')}</td>
            <td>${esc(seriesLabel(b))}</td>
            <td>${esc(b.title)}</td>
            <td><select class="form-select import-edition${b.edition_check ? ' check' : ''}" data-index="${i}" title="${esc(b.edition_reason || '')}"${b.state === 'in_library' ? ' disabled' : ''}>
                ${Object.entries(EDITION_LABELS).map(([v, l]) => `<option value="${v}"${editionOf(b) === v ? ' selected' : ''}>${l}</option>`).join('')}
            </select></td>
            <td>${esc(b.format)}</td>
            <td>${esc(formatSize(b.size_bytes))}</td>
            <td>${esc(b.source)}</td>
            <td><span class="badge ${badgeClass}"${hint}>${esc(label)}</span></td>
        </tr>`;
    }).join('') || '<tr><td colspan="9" class="no-results">No audiobooks found in this folder.</td></tr>';

    tbody.querySelectorAll('.import-check').forEach(cb => cb.addEventListener('change', updateImportButton));
    document.getElementById('importSelectAll').checked = counts.new + counts.link > 0;
    updateImportButton();
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
        renderImportResults();
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

    document.getElementById('importSelectAll').addEventListener('change', (e) => {
        document.querySelectorAll('.import-check:not(:disabled)').forEach(cb => { cb.checked = e.target.checked; });
        updateImportButton();
    });

    document.getElementById('importSelectedBtn').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        const msg = document.getElementById('importStatusMsg');
        btn.disabled = true;
        setActionStatus(msg, 'Importing...');
        const res = await fetch('/api/library/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths: selectedImportPaths(), editions: importEditions() })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            setActionStatus(msg, data.detail || 'Import failed', 'error');
            btn.disabled = false;
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

function seriesPageKey(entry) {
    return entry.asin ? 'asin:' + entry.asin : 'name:' + seriesKey(entry.name);
}

// Book details: every series the book is in, each opening its series page
function renderBookSeriesLinks(book) {
    const box = document.getElementById('bookSeriesLinks');
    const entries = seriesEntries(book);
    box.hidden = !entries.length;
    box.innerHTML = entries.length ? `<span class="muted">Series:</span> ` + entries.map((e, i) =>
        `<button class="series-chip" data-index="${i}">${esc(e.name)}${e.sequence ? ' #' + esc(e.sequence) : ''}</button>`).join('') : '';
    box.querySelectorAll('.series-chip').forEach(chip => chip.addEventListener('click', () => {
        hideModal(bookModal);
        openSeriesDetail(seriesPageKey(entries[chip.dataset.index]));
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
        <td>${esc(r.title)} ${editionOf(r) !== 'narrated' ? `<span class="edition-badge edition-${editionOf(r)}">${EDITION_LABELS[editionOf(r)]}</span>` : ''}</td>
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
                <div class="muted">${esc(sr.author || '')}</div>
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
        ${sr.unresolved ? '<p class="lookup-banner">Couldn\'t find this series on Audible, so only the books in your library are shown. Match one of its books on Audible, then open this page again.</p>' : ''}
        <div class="table-container series-books-table"><table class="data-table">
            <thead><tr><th class="col-num">#</th><th>Title</th><th>Narrator</th><th class="col-date">Released</th><th class="col-len">Length</th><th class="col-status">Status</th></tr></thead>
            <tbody>${sr.rows.map((r, i) => seriesRowHtml(r, 'rows', i)).join('')}</tbody></table></div>
        ${(sr.alternates || []).length ? `<details class="other-editions"${(sr.alternates || []).some(a => a.book_id) ? ' open' : ''}>
            <summary>Other editions (${sr.alternates.length}): dramatized and abridged versions of these books</summary>
            <div class="table-container series-books-table"><table class="data-table">
                <thead><tr><th class="col-num">#</th><th>Title</th><th>Narrator</th><th class="col-date">Released</th><th class="col-len">Length</th><th class="col-status">Status</th></tr></thead>
                <tbody>${sr.alternates.map((r, i) => seriesRowHtml(r, 'alternates', i)).join('')}</tbody></table></div>
        </details>` : ''}`;

    const rowOf = el => sr[el.dataset.src][el.dataset.index];
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

async function renderActivity() {
    let queueData, historyData;
    try {
        [queueData, historyData] = await Promise.all([
            fetch('/api/queue').then(r => r.json()),
            fetch('/api/history?limit=200').then(r => r.json()),
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
            <td><a href="#" class="queue-book" data-id="${esc(q.id)}">${esc(q.title)}</a>
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
    qRows.querySelectorAll('.queue-book').forEach(a => a.addEventListener('click', (e) => { e.preventDefault(); openBookModal(a.dataset.id); }));

    document.getElementById('historyRows').innerHTML = historyData.history.map(h => `
        <tr>
            <td class="muted">${esc(new Date(h.time).toLocaleString())}</td>
            <td><span class="event event-${esc(h.event)}">${esc(EVENT_LABELS[h.event] || h.event)}</span></td>
            <td>${esc(h.title)}</td>
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
            addBtnHtml = `<div class="add-btn monitored-btn" style="pointer-events: none;">${esc(tracked.status === 'Imported' ? 'In Library' : tracked.status)}</div>`;
        } else {
            addBtnHtml = `<div class="add-btn">Add to Library</div>`;
        }

        card.innerHTML = `
            <img src="${safeUrl(book.imageUrl, '')}" alt="${esc(book.title)}" class="book-cover">
            <div class="book-info">
                <div class="book-title" title="${esc(book.title)}">${esc(book.title)}</div>
                ${editionBadge(book) ? `<div class="edition-badges">${editionBadge(book)}</div>` : ''}
                <div class="book-author">${esc(book.authors)}</div>
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
        const magnetUrl = safeUrl(res.magnet_url || `/api/download?url=${encodeURIComponent(res.link)}&title=${encodeURIComponent(res.title)}`);
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
                    <button class="download-icon-btn send-to-client-btn" data-url="${esc(res.link)}" title="Send to qBittorrent">
                        <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"></path></svg>
                    </button>
                    ${actionsHtml}
                </div>
            `;
        }

        tr.innerHTML = `
            <td>
                <a class="release-title" href="${safeUrl(res.link)}" target="_blank" rel="noopener noreferrer">${esc(res.raw_title || res.title)}</a>
                <div class="release-meta">${esc(res.author || '')}${posted}</div>
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

            button.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div>';
            button.disabled = true;

            try {
                const res = await fetch('/api/send_to_client', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, book: currentModalBook })
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

function calEventHtml(ev, index) {
    const b = ev.book;
    const trendBadge = ev.kind === 'trending' && b.rank && b.rank <= 100 ? `<span class="cal-rank" title="Audible best seller #${b.rank}">#${b.rank}</span>` : '';
    return `<button class="cal-event cal-${ev.status}${trendBadge ? ' ranked' : ''}" data-event="${index}" title="${esc(`${b.title} — ${CAL_STATUS_LABELS[ev.status]}`)}">
        <span class="cal-event-title">${esc(b.title)}</span>${trendBadge}
        <span class="cal-event-sub">${esc(eventSubtitle(b))}</span></button>`;
}

function calCardHtml(ev, index) {
    const b = ev.book;
    const label = ev.kind === 'library' ? CAL_STATUS_LABELS[ev.status] : (b.rank ? `Best seller #${b.rank}` : 'Trending');
    return `<button class="cal-card cal-${ev.status}" data-event="${index}">
        <img src="${esc(coverUrl(b))}" alt="" loading="lazy" onerror="this.src='${PLACEHOLDER_COVER}'">
        <span class="cal-card-text">
            <span class="cal-event-title">${esc(b.title)}</span>
            <span class="cal-event-sub">${esc(eventSubtitle(b))}</span>
            <span class="cal-card-status">${esc(label)}</span>
        </span></button>`;
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
                <div class="cal-detail-author">${esc(b.authors)}</div>
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
