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

const navItems = document.querySelectorAll('.nav-item');
const views = document.querySelectorAll('.content-wrapper');

let currentABBData = [];
let currentAudibleNarrator = '';
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

function findInLibrary(book) {
    if (book.asin) {
        const byAsin = appLibrary.find(b => b.asin && b.asin === book.asin);
        if (byAsin) return byAsin;
    }
    const titleKey = normKey(String(book.title || '').split(':')[0]);
    const authorKey = normKey(primaryAuthor(book.authors));
    return appLibrary.find(b => normKey(String(b.title || '').split(':')[0]) === titleKey
        && normKey(primaryAuthor(b.authors)) === authorKey);
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
    setupSeriesModal();
    document.getElementById('seriesFilter').addEventListener('input', renderSeries);
}

function setupNavigation() {
    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            navItems.forEach(n => n.classList.remove('active'));
            item.classList.add('active');

            const targetViewId = item.getAttribute('data-view');
            views.forEach(v => { v.hidden = v.id !== targetViewId; });
            document.querySelector('.main-content').scrollTop = 0;
            window.scrollTo(0, 0);

            if (targetViewId === 'libraryView') {
                renderLibrary();
            }
            if (targetViewId === 'seriesView') {
                renderSeries();
            }
            clearInterval(activityTimer);
            if (targetViewId === 'activityView') {
                renderActivity();
                activityTimer = setInterval(renderActivity, 5000);
            }
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
    ['bulkStatus', 'bulkMatch', 'bulkRemove'].forEach(id => { document.getElementById(id).disabled = selectedIds.size === 0; });
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
const BOOK_FIELDS = { bookTitle: 'title', bookAuthors: 'authors', bookNarrators: 'narrators', bookSeries: 'series', bookSequence: 'sequence', bookStatus: 'status', bookAsin: 'asin', bookRuntime: 'runtime_min' };

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
    const review = document.getElementById('bookReview');
    review.hidden = book.status !== 'Needs Review';
    document.getElementById('bookReviewReason').textContent = book.review_reason || '';
    const seriesBtn = document.getElementById('monitorSeriesBtn');
    const seriesTracked = book.series_asin && appSeries.some(sr => sr.asin === book.series_asin && sr.monitored);
    seriesBtn.hidden = !book.series;
    seriesBtn.disabled = Boolean(seriesTracked);
    seriesBtn.textContent = seriesTracked ? 'Series Monitored' : 'Monitor Series';
    document.getElementById('matchPanel').hidden = true;
    document.getElementById('matchBookBtn').textContent = book.asin ? 'Rematch on Audible' : 'Match on Audible';
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

    document.getElementById('monitorSeriesBtn').addEventListener('click', () => {
        const book = appLibrary.find(b => b.id === currentBookId);
        if (book) openSeriesModal({ book_id: book.id, title: book.series });
    });

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
        const details = [c.authors, c.narrators && `read by ${c.narrators}`, seriesLabel(c), runtime, (c.release_date || '').slice(0, 4)].filter(Boolean).join(' · ');
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
    summary.textContent = `${importBooks.length} books found: ${counts.new} new, ${counts.link} already tracked (will be linked to their files), ${counts.in_library} already imported.`;

    tbody.innerHTML = importBooks.map((b, i) => {
        const [badgeClass, label] = IMPORT_STATES[b.state];
        const hint = b.state === 'link' ? ` title="Will link to '${esc(b.match_title)}'"` : '';
        return `<tr>
            <td><input type="checkbox" class="import-check" data-index="${i}" ${b.state === 'in_library' ? 'disabled' : 'checked'}></td>
            <td>${esc(b.authors || '—')}</td>
            <td>${esc(seriesLabel(b))}</td>
            <td>${esc(b.title)}</td>
            <td>${esc(b.format)}</td>
            <td>${esc(formatSize(b.size_bytes))}</td>
            <td>${esc(b.source)}</td>
            <td><span class="badge ${badgeClass}"${hint}>${esc(label)}</span></td>
        </tr>`;
    }).join('') || '<tr><td colspan="8" class="no-results">No audiobooks found in this folder.</td></tr>';

    tbody.querySelectorAll('.import-check').forEach(cb => cb.addEventListener('change', updateImportButton));
    document.getElementById('importSelectAll').checked = counts.new + counts.link > 0;
    updateImportButton();
}

function selectedImportPaths() {
    return [...document.querySelectorAll('.import-check:checked')].map(cb => importBooks[cb.dataset.index].path);
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
            body: JSON.stringify({ paths: selectedImportPaths() })
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

const openSeriesCards = new Set();
const WANTED_IN_SERIES = ['Monitored', 'Unreleased', 'Downloading', 'Downloaded', 'Needs Review', 'Missing'];

// "The Witcher" and "Witcher Series" are the same series
function seriesKey(name) {
    return normKey(String(name || '').replace(/\s+series$/i, ''));
}

function bySequence(a, b) {
    return (parseFloat(a.sequence) || 999) - (parseFloat(b.sequence) || 999) || normKey(a.title).localeCompare(normKey(b.title));
}

// Groups the library by series: monitored Audible series first (matched by Audible id, or
// by name for books imported from folders), then every other series found in the library
function buildSeriesGroups() {
    const tracked = appSeries.map(sr => ({ key: sr.id, title: sr.title, author: sr.author, series: sr, books: [] }));
    const byAsin = new Map(tracked.map(g => [g.series.asin, g]));
    const byName = new Map(tracked.map(g => [seriesKey(g.title), g]));
    const others = new Map();
    appLibrary.forEach(b => {
        if (!b.series) return;
        const group = byAsin.get(b.series_asin) || byName.get(seriesKey(b.series));
        if (group) {
            group.books.push(b);
            return;
        }
        const key = seriesKey(b.series);
        if (!others.has(key)) others.set(key, { key: 'lib:' + key, title: b.series, author: '', series: null, books: [] });
        others.get(key).books.push(b);
    });
    const all = [...tracked, ...others.values()];
    all.forEach(g => {
        g.books.sort(bySequence);
        g.author = g.author || primaryAuthor((g.books[0] || {}).authors);
    });
    const byTitle = (a, b) => normKey(a.title).localeCompare(normKey(b.title));
    return { tracked: tracked.sort(byTitle), others: [...others.values()].sort(byTitle) };
}

function seriesBookRows(books) {
    return books.map(b => `
        <div class="series-book" data-id="${esc(b.id)}">
            <span class="seq">${esc(b.sequence ? '#' + b.sequence : '')}</span>
            <span class="name">${esc(b.title)}<span class="muted">${b.release_date ? ' · ' + esc(String(b.release_date).slice(0, 4)) : ''}</span></span>
            <span class="library-status ${esc(statusClass(b.status))}">${esc(b.status)}</span>
        </div>`).join('');
}

function seriesCard(group) {
    const { series, books } = group;
    const owned = books.filter(b => b.status === 'Imported').length;
    const wanted = books.filter(b => WANTED_IN_SERIES.includes(b.status)).length;
    const card = document.createElement('div');
    card.className = 'series-card';
    let middle, actions;
    if (series) {
        const pct = books.length ? Math.round(owned / books.length * 100) : 0;
        middle = `<div class="progress"><div class="progress-bar" style="width: ${pct}%"></div></div>
            <div class="muted">${owned} of ${books.length} on disk${wanted ? ` · ${wanted} wanted` : ''}</div>`;
        actions = `<label class="checkbox-label muted"><input type="checkbox" class="series-monitored" ${series.monitored ? 'checked' : ''}> Monitored</label>
            <button class="secondary-btn series-sync">Sync</button>
            <button class="danger-btn series-remove">Remove</button>`;
    } else {
        middle = `<div class="muted">${owned} book${owned === 1 ? '' : 's'} on disk${books.length > owned ? ` · ${books.length - owned} other` : ''} · not monitored</div>`;
        actions = `<button class="secondary-btn series-monitor">Monitor Series</button>`;
    }
    card.innerHTML = `
        <div class="series-card-header">
            <div class="series-name">
                <h3>${esc(group.title)}</h3>
                <div class="muted">${esc(group.author || '')}${series && series.mode === 'future' ? ' · new releases only' : ''}</div>
            </div>
            <div class="series-progress">${middle}</div>
            <div class="series-card-actions">${actions}</div>
        </div>
        <div class="series-books" ${openSeriesCards.has(group.key) ? '' : 'hidden'}>
            ${seriesBookRows(books) || '<div class="no-results">No books yet. Click Sync.</div>'}
        </div>`;

    const list = card.querySelector('.series-books');
    card.querySelector('.series-card-header').addEventListener('click', (e) => {
        if (e.target.closest('.series-card-actions')) return;
        list.hidden = !list.hidden;
        list.hidden ? openSeriesCards.delete(group.key) : openSeriesCards.add(group.key);
    });
    card.querySelectorAll('.series-book').forEach(row => row.addEventListener('click', () => openBookModal(row.dataset.id)));

    if (!series) {
        card.querySelector('.series-monitor').addEventListener('click', () =>
            openSeriesModal({ book_id: books[0].id, title: group.title }));
        return card;
    }
    card.querySelector('.series-monitored').addEventListener('change', async (e) => {
        await postJSON(`/api/series/${encodeURIComponent(series.id)}`, { monitored: e.target.checked }, 'PATCH');
        await fetchSeries();
    });
    card.querySelector('.series-sync').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = 'Syncing...';
        const { ok, data } = await postJSON(`/api/series/${encodeURIComponent(series.id)}/sync`);
        toast(ok ? `${series.title}: ${data.added} new book${data.added === 1 ? '' : 's'} added` : (data.detail || 'Sync failed'), ok ? 'ok' : 'error');
        renderSeries();
    });
    card.querySelector('.series-remove').addEventListener('click', async () => {
        const confirmed = await confirmDialog(`Stop tracking "${series.title}"?\n\nIts books stay in your library.`,
            { title: 'Remove series', confirmText: 'Remove', danger: true });
        if (!confirmed) return;
        await fetch(`/api/series/${encodeURIComponent(series.id)}`, { method: 'DELETE' });
        renderSeries();
    });
    return card;
}

async function renderSeries() {
    await Promise.all([fetchSeries(), fetchLibrary()]);
    const container = document.getElementById('seriesContainer');
    const filter = document.getElementById('seriesFilter').value.trim().toLowerCase();
    const { tracked, others } = buildSeriesGroups();
    const matches = g => !filter || `${g.title} ${g.author}`.toLowerCase().includes(filter);
    const shownTracked = tracked.filter(matches);
    const shownOthers = others.filter(matches);
    document.getElementById('seriesStats').textContent =
        `${tracked.length} monitored · ${others.length} more in your library`;

    container.innerHTML = '';
    if (!tracked.length && !others.length) {
        container.innerHTML = `<div class="empty-state">
            <h3>No series yet</h3>
            <p>Books with a series show up here once they're in your library. Use Monitor Series on a series in Search results to add a whole series.</p>
        </div>`;
        return;
    }
    if (!shownTracked.length && !shownOthers.length) {
        container.innerHTML = '<div class="empty-state"><h3>No series match</h3><p>Nothing matches this filter.</p></div>';
        return;
    }
    const section = (title, groups) => {
        if (!groups.length) return;
        const h = document.createElement('h3');
        h.className = 'section-title';
        h.textContent = title;
        container.appendChild(h);
        const fragment = document.createDocumentFragment();
        groups.forEach(g => fragment.appendChild(seriesCard(g)));
        container.appendChild(fragment);
    };
    section(`Monitored (${shownTracked.length})`, shownTracked);
    section(`In your library (${shownOthers.length})`, shownOthers);
}

// Monitor-series dialog
const seriesModal = document.getElementById('seriesModal');
let pendingSeries = null;

function openSeriesModal(target) {
    pendingSeries = target;
    document.getElementById('seriesModalName').textContent = target.title || 'this series';
    setActionStatus(document.getElementById('seriesModalStatus'), '');
    seriesModal.querySelectorAll('[data-mode]').forEach(b => { b.disabled = false; });
    showModal(seriesModal);
}

function setupSeriesModal() {
    document.getElementById('closeSeriesModal').addEventListener('click', () => hideModal(seriesModal));
    seriesModal.addEventListener('click', (e) => { if (e.target === seriesModal) hideModal(seriesModal); });
    seriesModal.querySelectorAll('[data-mode]').forEach(btn => btn.addEventListener('click', async () => {
        const status = document.getElementById('seriesModalStatus');
        seriesModal.querySelectorAll('[data-mode]').forEach(b => { b.disabled = true; });
        setActionStatus(status, 'Loading the series from Audible...');
        const res = await fetch('/api/series', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...pendingSeries, mode: btn.dataset.mode })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            setActionStatus(status, data.detail || 'Failed', 'error');
            seriesModal.querySelectorAll('[data-mode]').forEach(b => { b.disabled = false; });
            return;
        }
        await Promise.all([fetchLibrary(), fetchSeries()]);
        setActionStatus(status, `Monitoring ${data.series.title}: added ${data.added} book${data.added === 1 ? '' : 's'}.`, 'ok');
        const btnInModal = document.getElementById('monitorSeriesBtn');
        btnInModal.disabled = true;
        btnInModal.textContent = 'Series Monitored';
        document.querySelectorAll('.series-group-header button').forEach(b => {
            if (b.previousSibling && b.previousSibling.textContent === pendingSeries.title) { b.disabled = true; b.textContent = 'Series Monitored'; }
        });
        renderLibrary();
        if (!document.getElementById('seriesView').hidden) renderSeries();
        setTimeout(() => hideModal(seriesModal), 1500);
    }));
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

async function performSearch() {
    const query = searchInput.value.trim();
    if (!query) return;

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
            language: product.language ? product.language[0].toUpperCase() + product.language.slice(1) : ""
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
        btn.textContent = tracked ? 'Series Monitored' : 'Monitor Series';
        btn.disabled = Boolean(tracked);
        btn.addEventListener('click', () => openSeriesModal({ series_asin: seriesAsin, title, author: primaryAuthor(books[0].authors) }));
        header.appendChild(btn);
    }
    section.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'results-grid';

    books.forEach(book => {
        const releaseDate = book.release_date || "";
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
                <div class="book-author">${esc(book.authors)}</div>
                <div class="book-narrator">Narrated by: ${esc(book.narrators)}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 8px;">Release: ${esc(releaseDate || 'Unknown')}</div>
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
    currentAudibleNarrator = narrators;
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

    // Clean up title for ABB search (remove subtitles after colon)
    let cleanTitle = title.split(':')[0].trim();

    try {
        // Search ABB using Title and Author params
        const res = await fetch(`/api/search_abb?title=${encodeURIComponent(cleanTitle)}&author=${encodeURIComponent(author)}`);
        const data = await res.json();
        currentABBData = data.results || [];
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

function renderABBResults() {
    abbResults.innerHTML = '';

    if (!currentABBData || currentABBData.length === 0) {
        abbResults.innerHTML = '<tr><td colspan="7" class="no-results">No downloads found.</td></tr>';
        return;
    }

    const filterVal = langFilter.value;

    // Filter and score matches
    let displayData = currentABBData.map(res => {
        let score = 0;
        let isMatch = false;
        if (appSettings.auto_match_narrator && currentAudibleNarrator !== 'Unknown Narrator' && res.abb_narrator !== 'Unknown') {
            // Very simple check: does the ABB narrator contain the Audible narrator's last name?
            const audNames = currentAudibleNarrator.split(' ');
            const audLastName = audNames[audNames.length - 1];
            if (res.abb_narrator.includes(audLastName)) {
                score = 10;
                isMatch = true;
            }
        }
        return { ...res, score, isMatch };
    });

    if (filterVal !== "All") {
        displayData = displayData.filter(d => d.language && d.language.toLowerCase() === filterVal.toLowerCase());
    }

    // Sort by score (matches first)
    displayData.sort((a, b) => b.score - a.score);

    if (displayData.length === 0) {
        abbResults.innerHTML = '<tr><td colspan="7" class="no-results">No downloads match the selected language filter.</td></tr>';
        return;
    }

    displayData.forEach(res => {
        const tr = document.createElement('tr');
        const magnetUrl = safeUrl(res.magnet_url || `/api/download?url=${encodeURIComponent(res.link)}&title=${encodeURIComponent(res.title)}`);
        const isM4b = (res.format || '').toUpperCase() === 'M4B';

        let matchBadge = res.isMatch ? `<span class="badge match">Match</span>` : '';
        let narratorStyle = res.isMatch ? 'font-weight: 500; color: var(--success);' : '';

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
                <div style="font-weight: 500; margin-bottom: 4px;">${esc(res.title)}</div>
                <div style="font-size: 0.8rem; color: var(--text-muted);">${esc(res.author)}</div>
            </td>
            <td>${esc(res.size_str)}</td>
            <td>${isM4b ? '<span class="badge match">M4B</span>' : esc(res.format || 'Unknown')}</td>
            <td>${esc(res.language || 'Unknown')}</td>
            <td style="${narratorStyle}">${esc(res.abb_narrator || 'Unknown')}</td>
            <td>${matchBadge}</td>
            <td>${actionsHtml}</td>
        `;
        abbResults.appendChild(tr);
    });

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
