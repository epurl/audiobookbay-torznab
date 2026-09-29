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

// Initialize
initApp();

async function initApp() {
    await fetchSettings();
    await fetchLibrary();
    // Setup listeners
    setupNavigation();
    setupSettings();
}

function setupNavigation() {
    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            navItems.forEach(n => n.classList.remove('active'));
            item.classList.add('active');
            
            const targetViewId = item.getAttribute('data-view');
            views.forEach(v => {
                v.style.display = v.id === targetViewId ? 'block' : 'none';
            });

            if (targetViewId === 'libraryView') {
                renderLibrary();
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
            delete newSettings.qbt_pass;
            appSettings = { ...appSettings, ...newSettings };
            document.getElementById('setQbtPass').value = "";
            langFilter.value = appSettings.language; // update modal sync
            
            const status = document.getElementById('settingsSaveStatus');
            status.style.display = 'inline';
            setTimeout(() => { status.style.display = 'none'; }, 2000);
        } catch (err) {
            console.error(err);
        }
    });

    document.getElementById('saveAuthBtn').addEventListener('click', async () => {
        const status = document.getElementById('authSaveStatus');
        const res = await fetch('/api/auth', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                username: document.getElementById('setAuthUser').value,
                password: document.getElementById('setAuthPass').value
            })
        });
        const data = await res.json().catch(() => ({}));
        status.style.color = res.ok ? 'var(--success)' : '#e74c3c';
        status.textContent = res.ok ? 'Saved! Your browser will ask you to sign in.' : (data.detail || 'Failed to save');
        status.style.display = 'inline';
        document.getElementById('setAuthPass').value = "";
    });
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

function renderLibrary() {
    const container = document.getElementById('libraryContainer');
    const loader = document.getElementById('libraryLoader');
    
    loader.style.display = 'flex';
    container.innerHTML = '';
    
    if (appLibrary.length === 0) {
        container.innerHTML = '<div class="no-results">Your library is empty.</div>';
        loader.style.display = 'none';
        return;
    }
    
    appLibrary.forEach(book => {
        const card = document.createElement('div');
        card.className = 'book-card';
        
        const status = book.status || 'Monitored';
        const statusClass = `status-${status.toLowerCase()}`;
        const releaseDate = book.release_date ? `Release: ${book.release_date}` : '';
        
        card.innerHTML = `
            <div class="library-status ${esc(statusClass)}">${esc(status)}</div>
            <img src="${safeUrl(book.imageUrl, '')}" alt="${esc(book.title)}" class="book-cover">
            <div class="book-info">
                <div class="book-title" title="${esc(book.title)}">${esc(book.title)}</div>
                <div class="book-author">${esc(book.authors)}</div>
                <div class="book-narrator">Narrated by: ${esc(book.narrators)}</div>
                ${releaseDate ? `<div style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 8px;">${esc(releaseDate)}</div>` : ''}
            </div>
        `;
        
        card.addEventListener('click', () => openModal(book));
        container.appendChild(card);
    });
    
    loader.style.display = 'none';
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
            imageUrl: product.product_images && product.product_images[500] ? product.product_images[500] : '/static/images/placeholder.jpg',
            release_date: product.release_date || product.issue_date || ""
        };

        if (product.series && product.series.length > 0) {
            const seriesTitle = product.series[0].title;
            const seq = parseFloat(product.series[0].sequence) || 999;
            bookData.sequence = seq;
            if (!groups[seriesTitle]) groups[seriesTitle] = [];
            groups[seriesTitle].push(bookData);
        } else {
            standalone.push(bookData);
        }
    });

    for (const [seriesTitle, books] of Object.entries(groups)) {
        books.sort((a, b) => a.sequence - b.sequence);
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
    header.className = 'series-header';
    header.textContent = title;
    section.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'results-grid';
    
    books.forEach(book => {
        const releaseDate = book.release_date || "";
        const isTracked = appLibrary.some(b => b.title === book.title);
        const card = document.createElement('div');
        card.className = 'book-card';
        
        let addBtnHtml = '';
        if (isTracked) {
            addBtnHtml = `<div class="add-btn monitored-btn" style="pointer-events: none;">Monitored</div>`;
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
        document.getElementById(activeFolderInputId).value = currentFolderPath.value;
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
        const url = `/api/browse?path=${encodeURIComponent(path || "")}`;
        const res = await fetch(url);
        const data = await res.json();
        
        currentFolderPath.value = data.path;
        currentParentPath = data.parent;
        
        folderList.innerHTML = '';
        if (data.dirs && data.dirs.length > 0) {
            data.dirs.forEach(dirName => {
                const item = document.createElement('div');
                item.className = 'folder-item';
                // Simple SVG folder icon
                const icon = `<svg class="folder-icon" viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2" fill="none"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`;
                item.innerHTML = `${icon}<span>${esc(dirName)}</span>`;
                
                // When a folder is clicked, navigate into it. We append safely.
                item.addEventListener('click', () => {
                    const separator = data.path.includes('\\') ? '\\' : '/';
                    const newPath = data.path.endsWith(separator) ? `${data.path}${dirName}` : `${data.path}${separator}${dirName}`;
                    loadFolder(newPath);
                });
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
