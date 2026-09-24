/* Dokumenten-Sortierer frontend. Talks to Python via pywebview (native) or /api (browser mode). */

const $ = sel => document.querySelector(sel);
const $$ = sel => [...document.querySelectorAll(sel)];

async function api(method, ...args) {
  if (window.pywebview?.api?.[method]) return window.pywebview.api[method](...args);
  const res = await fetch('/api/' + method, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args) });
  const body = await res.json();
  if (body.error) throw new Error(body.error);
  return body.result;
}

const S = {
  view: 'inbox',
  pending: [],
  processing: [],
  currentId: null,
  data: null,             // {proposal, suggestions, tree}
  selected: new Set(),
  planned: new Set(),     // new folders created with "+" (made on approve)
  external: new Set(),    // folders outside the Ablage (other drives), picked in Finder
  filenameManual: false,
  page: 0, pages: 1, docMode: 'page',
  logMode: 'placements', logRows: [],
  health: null,
  busy: false,
};

/* ================================================================ helpers */
function toast(msg, { kind = 'ok', action, onAction, ms = 5200 } = {}) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `${icon(kind === 'error' ? 'alert' : kind === 'info' ? 'info' : 'check')}<span>${msg}</span>${action ? `<button>${action}</button>` : ''}`;
  $('#toasts').appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  const close = () => { el.classList.remove('show'); setTimeout(() => el.remove(), 250); };
  el.querySelector('button')?.addEventListener('click', () => { onAction?.(); close(); });
  setTimeout(close, ms);
}

function relTime(iso) {
  if (!iso) return '';
  const d = new Date(iso), s = (Date.now() - d) / 1000;
  if (s < 60) return 'gerade eben';
  if (s < 3600) return `vor ${Math.floor(s / 60)} Min.`;
  if (s < 86400) return `vor ${Math.floor(s / 3600)} Std.`;
  return d.toLocaleDateString('de-DE', { day: '2-digit', month: 'short', year: 'numeric' });
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const stem = name => (name || '').replace(/\.pdf$/i, '');
const typing = () => ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);

/* ================================================================ navigation */
function showView(view) {
  S.view = view;
  $$('#nav button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  hideTip();
  if (view === 'archive') loadArchive();
  if (view === 'log') loadLog();
  if (view === 'settings') loadSettings();
  if (view === 'search') setTimeout(() => $('#search-input').focus(), 50);
  if (view === 'inbox') setTimeout(() => reviewMap.fit(false), 30);
}

/* ================================================================ queue polling */
async function poll() {
  try {
    const st = await api('state');
    const ids = st.pending.map(p => p.id).join(',');
    const changed = ids !== S.pending.map(p => p.id).join(',') || JSON.stringify(st.processing) !== JSON.stringify(S.processing);
    S.pending = st.pending; S.processing = st.processing; S.incoming = st.incoming; S.output = st.output;
    if (changed) renderQueue();
    $('#inbox-badge').textContent = st.pending.length || '';
    $('#empty-incoming').innerHTML = st.incoming.map(d => `<div>${icon('folder')}<span>${esc(d)}</span></div>`).join('');
    renderPause(st);
    if (!S.pending.some(p => p.id === S.currentId)) selectProposal(S.pending[0]?.id ?? null);
    for (const e of st.errors) {
      if (!S.seenErrors) S.seenErrors = new Set(st.errors.map(x => x.id));
      if (!S.seenErrors.has(e.id)) { S.seenErrors.add(e.id); toast(`Fehler bei ${esc((e.source_path || '').split('/').pop())}: ${esc(e.detail)}`, { kind: 'error', ms: 9000 }); }
    }
    if (!S.seenErrors) S.seenErrors = new Set();
  } catch (err) {
    console.error(err);
  }
}

function renderQueue() {
  const q = $('#queue');
  // One line for the document being worked on, one summary line for the rest of the batch.
  const active = S.processing.filter(p => p.stage !== 'Wartet');
  const waiting = S.processing.length - active.length;
  const proc = active.map(p => `
    <div class="q-item processing"><span class="spinner"></span>
      <div class="q-text"><div class="q-name">${esc(p.name)}</div><div class="q-sub">${esc(p.stage)}</div></div></div>`).join('')
    + (waiting ? `<div class="q-item waiting"><span class="q-count">${waiting}</span>
      <div class="q-text"><div class="q-name">weitere in der Warteschlange</div><div class="q-sub">werden im Hintergrund analysiert</div></div></div>` : '');
  const pend = S.pending.map((p, i) => `
    <button class="q-item ${p.id === S.currentId ? 'active' : ''}" data-id="${p.id}">
      <span class="q-dot"></span>
      <div class="q-text"><div class="q-name">${esc(stem(p.filename))}</div><div class="q-sub">${esc(p.name)} · ${relTime(p.created)}</div></div>
      ${i < 9 ? `<kbd class="q-kbd">${i + 1}</kbd>` : ''}
    </button>`).join('');
  q.innerHTML = proc + pend || `<div class="q-empty">Keine Dokumente in Arbeit</div>`;
  $('#inbox-empty').classList.toggle('working', S.processing.length > 0);
  $('#empty-title').textContent = S.processing.length ? 'Dokument wird gelesen …' : 'Alles abgelegt';
  $('#empty-processing').innerHTML = S.processing.length ? `<span class="spinner"></span>${esc(S.processing[0].name)} · ${esc(S.processing[0].stage)}` : '';
}

$('#queue').addEventListener('click', e => {
  const b = e.target.closest('.q-item[data-id]');
  if (b) { selectProposal(+b.dataset.id); if (S.view !== 'inbox') showView('inbox'); }
});

function renderPause(st) {
  const box = $('#pause-banner');
  box.classList.toggle('show', !!st.paused);
  if (!st.paused) return;
  const until = st.paused_until ? new Date(st.paused_until * 1000).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }) : null;
  box.querySelector('span').textContent = until ? `Pausiert bis ${until}` : 'Überwachung pausiert';
}
$('#pause-banner button').addEventListener('click', async () => { await api('resume'); toast('Überwachung läuft wieder', { kind: 'info', ms: 2500 }); poll(); });

/* ================================================================ review */
const reviewMap = new MindMap($('#review-map'), {
  mode: 'review',
  onToggle: path => toggleTarget(path),
  onAddChild: (parent, anchor) => addFolder(parent, anchor),
});

async function selectProposal(id) {
  S.currentId = id;
  renderQueue();
  $('#review').classList.toggle('show', id != null);
  $('#view-inbox .topbar-actions').classList.toggle('hidden', id == null);
  $('#inbox-empty').classList.toggle('show', id == null);
  if (id == null) { S.data = null; $('#crumb-name').textContent = '—'; return; }
  const data = await api('proposal', id);
  if (S.currentId !== id || !data) return;
  S.data = data;
  const p = data.proposal;
  S.planned = new Set();
  S.external = new Set(data.external || []);
  S.filenameManual = false;
  const top = data.suggestions[0];
  S.selected = new Set(top ? [top.path] : []);
  data.suggestions.slice(1).filter(s => s.score >= 0.85 && !s.path.startsWith(top.path.split('/')[0] + '/') ).forEach(s => S.selected.add(s.path));
  $('#crumb-name').textContent = p.name;
  $('#filename').value = stem(p.filename);
  sizeFilename();
  $('#m-date').value = p.date_extracted || '';
  $('#m-sender').value = p.sender || '';
  $('#m-type').value = p.document_type || '';
  $('#m-keyword').value = p.keyword || '';
  $('#source-line').innerHTML = `
    <span>${icon('file')}${esc(p.name)}</span><span>${icon('scan')}${esc(p.ocr_method || '—')}</span>
    <span>${icon('clock')}${relTime(p.created_at)}</span>`;
  $('#ocr-text').textContent = p.ocr_text || '';
  S.page = 0;
  loadPage();
  reviewMap.setData({ tree: data.tree, external: [...S.external], suggestions: data.suggestions, selected: S.selected, planned: S.planned, docLabel: $('#filename').value + '.pdf' });
  renderSuggestChips();
  renderTargets();
}

async function loadPage() {
  const frame = $('#page-frame');
  frame.classList.add('loading');
  const id = S.currentId;
  try {
    const res = await api('page', id, S.page);
    if (id !== S.currentId || !res) return;
    $('#page-img').src = res.image;
    S.pages = res.pages;
    $('#pg-label').textContent = `${res.page + 1} / ${res.pages}`;
  } finally { frame.classList.remove('loading'); }
}

function toggleTarget(path) {
  S.selected.has(path) ? S.selected.delete(path) : S.selected.add(path);
  refreshMap();
  reviewMap.ensureVisible();
}

function refreshMap() {
  reviewMap.update({ selected: S.selected, planned: S.planned, docLabel: $('#filename').value + '.pdf' });
  renderSuggestChips();
  renderTargets();
}

function isNewPath(path) {
  if (path.startsWith('/')) return false;
  const s = S.data?.suggestions.find(x => x.path === path);
  return S.planned.has(path) || (s && s.new) || reviewMap.index?.get(path)?.virtual;
}

function renderSuggestChips() {
  $('#suggest-chips').innerHTML = (S.data?.suggestions || []).map((s, i) => `
    <button class="s-chip ${S.selected.has(s.path) ? 'on' : ''}" data-path="${esc(s.path)}" title="${esc(s.reasons.join(' · '))}">
      <kbd>${i + 1}</kbd><span class="s-path">${esc(labelFor(s.path))}</span>
      ${s.new ? '<span class="mm-new">neu</span>' : ''}<span class="s-score">${Math.round(s.score * 100)}%</span>
    </button>`).join('');
}

$('#suggest-chips').addEventListener('click', e => {
  const b = e.target.closest('.s-chip');
  if (b) toggleTarget(b.dataset.path);
});

function renderTargets() {
  const chips = [...S.selected].map(p => `
    <span class="t-chip ${isNewPath(p) ? 'new' : ''}">${icon(isNewPath(p) ? 'folderPlus' : 'folder')}
      <span title="${esc(p)}">${esc(labelFor(p))}</span><button data-path="${esc(p)}" title="Entfernen">${icon('x')}</button></span>`).join('');
  $('#target-chips').innerHTML = chips || `<span class="t-none">Kein Ziel gewählt – klicke einen Ordner in der Karte an</span>`;
  $('#approve-btn').disabled = !S.selected.size;
  $('#approve-btn').innerHTML = `${icon('check')}${S.selected.size > 1 ? `In ${S.selected.size} Ordner ablegen` : 'Ablegen'}`;
}

$('#target-chips').addEventListener('click', e => {
  const b = e.target.closest('button[data-path]');
  if (b) toggleTarget(b.dataset.path);
});

/* metadata → filename */
let nameTimer;
function metadata() {
  return { date: $('#m-date').value || null, sender: $('#m-sender').value.trim() || null,
           type: $('#m-type').value.trim() || null, keyword: $('#m-keyword').value.trim() || null };
}
['#m-date', '#m-sender', '#m-type', '#m-keyword'].forEach(sel => $(sel).addEventListener('input', () => {
  if (S.filenameManual) return;
  clearTimeout(nameTimer);
  nameTimer = setTimeout(async () => {
    $('#filename').value = stem(await api('filename_for', metadata()));
    flash($('#filename'));
    refreshMap();
  }, 180);
}));
$('#filename').addEventListener('input', () => { S.filenameManual = true; $('#filename-reset').classList.add('show'); reviewMap.update({ docLabel: $('#filename').value + '.pdf' }); });
$('#filename-reset').addEventListener('click', async () => {
  S.filenameManual = false;
  $('#filename-reset').classList.remove('show');
  $('#filename').value = stem(await api('filename_for', metadata()));
  flash($('#filename'));
  refreshMap();
});
function sizeFilename() {
  const el = $('#filename');
  const ctx = sizeFilename.ctx || (sizeFilename.ctx = document.createElement('canvas').getContext('2d'));
  ctx.font = getComputedStyle(el).font;
  const text = el.value || ' ';
  el.style.width = Math.ceil(ctx.measureText(text).width - text.length * 0.3 + 20) + 'px';
}
['input', 'change'].forEach(ev => $('#filename').addEventListener(ev, sizeFilename));
function flash(el) { sizeFilename(); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }

/* approve / skip */
async function approve() {
  if (S.busy || S.currentId == null) return;
  if (!S.selected.size) { toast('Bitte mindestens einen Zielordner wählen', { kind: 'info' }); return; }
  const name = $('#filename').value.trim().replace(/\.pdf$/i, '');
  if (!name) { toast('Der Dateiname darf nicht leer sein', { kind: 'info' }); $('#filename').focus(); return; }
  S.busy = true;
  $('#approve-btn').classList.add('working');
  const id = S.currentId;
  try {
    const res = await api('approve', id, name + '.pdf', [...S.selected], metadata());
    $('#review').classList.add('sent');
    setTimeout(() => $('#review').classList.remove('sent'), 450);
    const where = res.paths.length > 1 ? `${res.paths.length} Ordnern` : esc(res.paths[0].split('/').slice(-2, -1)[0]);
    toast(`<b>${esc(name)}.pdf</b> abgelegt in ${where}`, { action: 'Rückgängig', onAction: () => undo(id), ms: 8000 });
    S.pending = S.pending.filter(p => p.id !== id);
    const next = S.pending[0]?.id ?? null;
    await selectProposal(next);
    poll();
  } catch (err) {
    toast(esc(err.message || err), { kind: 'error', ms: 9000 });
  } finally {
    S.busy = false;
    $('#approve-btn').classList.remove('working');
  }
}

async function undo(proposalId) {
  try {
    const path = await api('undo', proposalId);
    toast(`Zurück im Eingang: ${esc(path.split('/').pop())}`, { kind: 'info' });
    await poll();
    if (S.pending.some(p => p.id === proposalId)) selectProposal(proposalId);
  } catch (err) { toast(esc(err.message || err), { kind: 'error' }); }
}

async function skip() {
  if (S.currentId == null) return;
  const id = S.currentId;
  await api('skip', id);
  toast('Übersprungen – bleibt im Eingang, bis sich die Datei ändert', { kind: 'info' });
  S.pending = S.pending.filter(p => p.id !== id);
  await selectProposal(S.pending[0]?.id ?? null);
  poll();
}

$('#approve-btn').addEventListener('click', approve);
$('#skip-btn').addEventListener('click', skip);
$('#reanalyze-btn').addEventListener('click', async () => {
  if (S.currentId == null) return;
  await api('reanalyze', S.currentId);
  toast('Dokument wird neu analysiert', { kind: 'info' });
  S.pending = S.pending.filter(p => p.id !== S.currentId);
  selectProposal(S.pending[0]?.id ?? null);
  poll();
});
$('#reveal-src-btn').addEventListener('click', () => S.data && api('reveal', S.data.proposal.source_path));
$('#scan-btn').addEventListener('click', async () => { await api('scan'); toast('Eingang wird geprüft', { kind: 'info', ms: 2000 }); poll(); });

/* document preview */
$('#doc-seg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  S.docMode = b.dataset.mode;
  $$('#doc-seg button').forEach(x => x.classList.toggle('on', x === b));
  $('.doc-card').classList.toggle('text-mode', S.docMode === 'text');
});
$('#pg-prev').addEventListener('click', () => { if (S.page > 0) { S.page--; loadPage(); } });
$('#pg-next').addEventListener('click', () => { if (S.page < S.pages - 1) { S.page++; loadPage(); } });
$('#page-frame').addEventListener('dblclick', () => S.data && api('open_path', S.data.proposal.source_path));

/* "+ Ordner wählen": native Finder dialog (can also create folders, other drives work too).
   Browser mode has no dialog, so it falls back to the small name popover. */
async function addFolder(parent, anchor) {
  if (!window.pywebview) return openFolderPopover(parent, anchor);
  let res;
  try { res = await api('pick_destination', parent === '§ext' ? '' : parent || '.'); }
  catch (err) { toast(esc(err.message || err), { kind: 'error' }); return; }
  if (!res) return;
  if (res.external) S.external.add(res.path);
  S.data.tree = res.tree;
  S.selected.add(res.path);
  reviewMap._ancestors(res.path).forEach(a => reviewMap.expanded.add(a));
  reviewMap.update({ tree: res.tree, external: [...S.external] });
  refreshMap();
  reviewMap.setFocus(res.path);
  reviewMap.ensureVisible();
  toast(`Ziel hinzugefügt: <b>${esc(labelFor(res.path))}</b>`, { kind: 'info', ms: 3000 });
}

function labelFor(path) {
  if (path === '.') return S.data?.tree.name || 'Ablage';
  if (!path.startsWith('/')) return path;
  const parts = path.split('/').filter(Boolean);
  return (path.startsWith('/Volumes/') ? parts[1] + ' › ' : '') + parts.slice(-2).join('/');
}

/* new-folder popover (browser mode only) */
let popParent = '.';
function openFolderPopover(parent, anchor) {
  popParent = parent || '.';
  const pop = $('#folder-pop');
  const rootName = S.data?.tree.name || 'Ablage';
  $('#pop-parent').innerHTML = `in ${icon('folder')}<b>${esc(popParent === '.' ? rootName : popParent)}</b>`;
  $('#pop-name').value = '';
  updatePopPreview();
  pop.classList.add('show');
  const r = (anchor || $('#review-map')).getBoundingClientRect(), pr = pop.getBoundingClientRect();
  let left = r.left + r.width / 2 - pr.width / 2, top = r.top - pr.height - 12;
  if (top < 12) top = r.bottom + 12;
  pop.style.left = Math.max(12, Math.min(innerWidth - pr.width - 12, left)) + 'px';
  pop.style.top = Math.max(12, Math.min(innerHeight - pr.height - 12, top)) + 'px';
  setTimeout(() => $('#pop-name').focus(), 20);
}
function closePopover() { $('#folder-pop').classList.remove('show'); }
function updatePopPreview() {
  const name = $('#pop-name').value.trim();
  const path = name ? (popParent === '.' ? name : popParent + '/' + name) : '…';
  $('#pop-preview').innerHTML = `${icon('folderPlus')}<code>${esc(path)}</code>`;
  $('#pop-ok').disabled = !name || /[\/\\:]|^\./.test(name);
}
$('#pop-name').addEventListener('input', updatePopPreview);
$('#pop-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); $('#pop-ok').click(); }
  if (e.key === 'Escape') { e.stopPropagation(); closePopover(); }
});
$('#pop-cancel').addEventListener('click', closePopover);
$('#pop-ok').addEventListener('click', () => {
  const name = $('#pop-name').value.trim();
  if (!name || /[\/\\:]|^\./.test(name)) return;
  const path = popParent === '.' ? name : popParent + '/' + name;
  S.planned.add(path);
  S.selected.delete(popParent);          // a new subfolder refines its (selected) parent
  S.selected.add(path);
  reviewMap.expanded.add(popParent);
  closePopover();
  refreshMap();
  reviewMap.setFocus(path);
  reviewMap.ensureVisible();
  toast(`Neuer Ordner <b>${esc(path)}</b> wird beim Ablegen angelegt`, { kind: 'info', ms: 3500 });
});
document.addEventListener('pointerdown', e => {
  if (!e.target.closest('#folder-pop') && !e.target.closest('.mm-fab') && !e.target.closest('.mm-add')) closePopover();
});

/* keyboard */
document.addEventListener('keydown', e => {
  if ($('#dialog').classList.contains('show')) {
    if (e.key === 'Escape') $('#dialog-cancel').click();
    return;
  }
  if ($('#wizard').classList.contains('show')) {   // the wizard owns the keyboard while open
    if (e.key === 'Enter' && !typing()) { e.preventDefault(); $('#wz-next').click(); }
    return;
  }
  if (e.key === 'Escape') { closePopover(); if (typing()) document.activeElement.blur(); return; }
  if (S.view !== 'inbox' || $('#folder-pop').classList.contains('show')) return;
  if (e.key === 'Enter' && typing() && document.activeElement.closest('.doc-head')) { e.preventDefault(); document.activeElement.blur(); approve(); return; }
  if (typing()) return;
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); approve(); return; }
  if (e.metaKey && e.key === 'Backspace') { e.preventDefault(); skip(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (/^[1-4]$/.test(e.key) && S.data?.suggestions[+e.key - 1]) { toggleTarget(S.data.suggestions[+e.key - 1].path); return; }
  if (e.key === 'n' || e.key === 'N') { e.preventDefault(); addFolder(reviewMap.focus, $('.mm-fab')); return; }
  if (e.key === 'f' || e.key === 'F') { reviewMap.fit(true); return; }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'j' || e.key === 'k') {
    const i = S.pending.findIndex(p => p.id === S.currentId);
    const next = S.pending[i + (e.key === 'ArrowDown' || e.key === 'j' ? 1 : -1)];
    if (next) { e.preventDefault(); selectProposal(next.id); }
  }
});

/* ================================================================ archive */
const archiveMap = new MindMap($('#archive-map'), { mode: 'browse', onFocus: path => showFolder(path) });
async function loadArchive() {
  const tree = await api('tree');
  $('#archive-root').textContent = S.output || '';
  archiveMap.setData({ tree, suggestions: [], selected: new Set() });
  showFolder(archiveMap.focus || '.');
}
$('#archive-refresh').addEventListener('click', loadArchive);
async function showFolder(path) {
  const files = await api('folder_documents', path);
  const tree = archiveMap.data.tree;
  $('#folder-title').innerHTML = `${icon('folder')}${esc(path === '.' ? tree.name : path.split('/').pop())}`;
  $('#folder-path').textContent = path === '.' ? tree.name : tree.name + ' / ' + path.split('/').join(' / ');
  $('#folder-files').innerHTML = files.length ? files.map(f => `
    <div class="file-row" data-path="${esc(f.path)}">
      <span class="f-ic">${icon('file')}</span><span class="f-text"><span class="f-name">${esc(f.name)}</span>${f.sub ? `<span class="f-sub">${esc(f.sub)}</span>` : ''}</span>
      <button class="icon-btn small" data-act="open" title="Öffnen">${icon('open')}</button>
      <button class="icon-btn small" data-act="reveal" title="Im Finder zeigen">${icon('finder')}</button>
    </div>`).join('') : `<div class="hint-empty">Dieser Ordner ist leer.</div>`;
}
$('#folder-files').addEventListener('click', fileActions);
function fileActions(e) {
  const row = e.target.closest('[data-path]'); if (!row) return;
  const act = e.target.closest('button')?.dataset.act;
  api(act === 'reveal' ? 'reveal' : 'open_path', row.dataset.path);
}
$('#folder-files').addEventListener('dblclick', e => { const row = e.target.closest('[data-path]'); if (row) api('open_path', row.dataset.path); });

/* ================================================================ search */
$('#search-input').addEventListener('keydown', async e => {
  if (e.key !== 'Enter') return;
  const q = $('#search-input').value.trim();
  if (!q) return;
  $('#search-mode').innerHTML = `<span class="spinner"></span> Suche läuft …`;
  const res = await api('search', q);
  $('#search-mode').innerHTML = `${icon(res.mode === 'Semantische Suche' ? 'sparkle' : 'search')}<b>${res.mode}</b> · ${res.results.length} Treffer${res.note ? ` · <span class="warn">${esc(res.note)}</span>` : ''}`;
  const max = Math.max(...res.results.map(r => r.score), 1e-9);
  $('#search-results').innerHTML = res.results.map(r => `
    <div class="result card ${r.exists ? '' : 'missing'}" data-path="${esc(r.destination_path)}">
      <div class="r-ic">${icon('file')}</div>
      <div class="r-main">
        <div class="r-name">${esc(r.destination_path.split('/').pop())}</div>
        <div class="r-path">${icon('folder')}${esc(r.relative)}</div>
        <div class="r-tags">${[r.document_type, r.sender, r.keyword, r.date_extracted].filter(Boolean).map(t => `<span>${esc(t)}</span>`).join('')}</div>
      </div>
      <div class="r-score"><div class="bar"><i style="width:${Math.round(r.score / max * 100)}%"></i></div></div>
      <div class="r-actions">
        <button class="icon-btn" data-act="open" title="Öffnen">${icon('open')}</button>
        <button class="icon-btn" data-act="reveal" title="Im Finder zeigen">${icon('finder')}</button>
      </div>
    </div>`).join('') || `<div class="hint-empty big">Nichts gefunden. Versuche es mit anderen Worten.</div>`;
});
$('#search-results').addEventListener('click', fileActions);

/* ================================================================ log */
async function loadLog() {
  S.logRows = S.logMode === 'placements' ? await api('history', 500) : await api('events', 300);
  renderLog();
}
function renderLog() {
  const f = $('#log-filter').value.trim().toLowerCase();
  const rows = S.logRows.filter(r => !f || JSON.stringify(r).toLowerCase().includes(f));
  if (S.logMode === 'placements') {
    $('#log-table').innerHTML = `<table><thead><tr><th>Abgelegt</th><th>Dateiname</th><th>Ordner</th><th>Absender</th><th>Art</th><th>Original</th><th></th></tr></thead><tbody>
      ${rows.map(r => `<tr data-path="${esc(r.destination_path)}">
        <td class="nowrap muted">${fmtDate(r.approved_at)}</td>
        <td class="strong">${esc(r.destination_path.split('/').pop())}</td>
        <td><span class="folder-tag ${r.exists ? '' : 'gone'}" title="${r.exists ? '' : 'Datei nicht mehr vorhanden'}">${icon('folder')}${esc(r.relative)}</span></td>
        <td>${esc(r.sender || '—')}</td><td>${esc(r.document_type || '—')}</td>
        <td class="muted">${esc(r.original_filename)}</td>
        <td class="row-actions">
          <button class="icon-btn small" data-act="open" title="Öffnen">${icon('open')}</button>
          <button class="icon-btn small" data-act="reveal" title="Im Finder zeigen">${icon('finder')}</button>
          ${r.proposal_id ? `<button class="icon-btn small" data-act="undo" data-id="${r.proposal_id}" title="Rückgängig">${icon('undo')}</button>` : ''}
        </td></tr>`).join('')}</tbody></table>
      ${rows.length ? '' : '<div class="hint-empty big">Noch keine Ablagen protokolliert.</div>'}`;
  } else {
    const kind = a => ({ error: 'bad', index_error: 'bad', ocr_fallback: 'warn', placed: 'good', undone: 'warn', skipped: 'muted' }[a] || '');
    const label = a => ({ error: 'Fehler', index_error: 'Index-Fehler', ocr_fallback: 'OCR-Fallback', placed: 'Abgelegt', proposal: 'Vorschlag', skipped: 'Übersprungen', indexed: 'Indiziert', folder: 'Ordner', undone: 'Rückgängig' }[a] || a);
    $('#log-table').innerHTML = `<table><thead><tr><th>Zeit</th><th>Ereignis</th><th>Datei</th><th>Details</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td class="nowrap muted">${fmtDate(r.at)}</td><td><span class="ev ${kind(r.action)}">${label(r.action)}</span></td>
        <td class="strong">${esc((r.destination_path || r.source_path || '').split('/').pop() || '—')}</td><td class="muted wrap">${esc(r.detail || '')}</td></tr>`).join('')}
      </tbody></table>`;
  }
}
$('#log-seg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  S.logMode = b.dataset.mode;
  $$('#log-seg button').forEach(x => x.classList.toggle('on', x === b));
  loadLog();
});
$('#log-filter').addEventListener('input', renderLog);
$('#log-table').addEventListener('click', async e => {
  const btn = e.target.closest('button'); if (!btn) return;
  if (btn.dataset.act === 'undo') { await undo(+btn.dataset.id); loadLog(); return; }
  fileActions(e);
});

/* ================================================================ settings */
const TASK_INFO = {
  llm: { title: 'Analyse & Umbenennung', sub: 'liest Datum, Absender, Art, Stichwort', filter: m => !/embed|ocr/i.test(m) },
  ocr: { title: 'GLM-OCR', sub: 'genaue Texterkennung für Scans (optional)', filter: m => !/embed/i.test(m) },
  embedding: { title: 'Suche & ähnliche Ablagen', sub: 'Embedding-Modell', filter: m => /embed/i.test(m) },
};

async function loadSettings() {
  const [s, h, p] = await Promise.all([api('settings'), api('health'), api('providers')]);
  S.settingsEmbedded = s.embedded_text || 'never';
  S.health = h; S.providers = p; S.endpoints = JSON.parse(JSON.stringify(s.endpoints));
  S.incomingDirs = [...s.incoming_dirs];
  renderIncoming();
  $('#s-output').value = s.output_dir;
  $('#s-own').value = (s.own_names || []).join('\n');
  $$('#analysis-seg button').forEach(b => b.classList.toggle('on', b.dataset.analysis === (s.analysis_mode || 'ocr')));
  $$('#effort-seg button').forEach(b => b.classList.toggle('on', b.dataset.effort === (s.reasoning_effort || 'xhigh')));
  $$('#embedded-seg button').forEach(b => b.classList.toggle('on', b.dataset.embedded === (s.embedded_text || 'never')));
  $$('#policy-seg button').forEach(b => b.classList.toggle('on', b.dataset.policy === (s.compute_policy || 'always')));
  renderEndpoints();
  $$('#ocr-mode-seg button').forEach(b => b.classList.toggle('on', b.dataset.mode === (s.ocr_mode || 'vision')));
  $('#icloud-note').innerHTML = s.icloud ? `${icon('cloud')}<span>iCloud Drive gefunden: <code>${esc(s.icloud)}</code></span><button class="btn small" id="use-icloud">Ablage in iCloud Drive</button>` : '';
  $('#use-icloud')?.addEventListener('click', () => { $('#s-output').value = s.icloud + '/Dokumente'; });
  $('#s-login').checked = await api('login_item');
  $('#app-version').textContent = await api('version');
  renderPipeline(h);
  renderComponents(h);
}

function renderEndpoints() {
  const h = S.health?.endpoints || {};
  $('#endpoints').innerHTML = Object.entries(TASK_INFO).map(([task, info]) => {
    const ep = S.endpoints[task], st = h[task] || {};
    const models = (st.models || []).filter(info.filter);
    if (ep.model && !models.includes(ep.model)) models.unshift(ep.model);
    const state = !st.reachable ? ['bad', 'nicht erreichbar'] : st.ready ? ['ok', 'bereit'] : ['warn', 'Modell fehlt'];
    return `<div class="ep" data-task="${task}">
      <div class="ep-head"><div><b>${info.title}</b><small>${info.sub}</small></div>
        <span class="ep-state ${state[0]}"><i></i>${state[1]}</span>${st.remote ? `<span class="ep-remote">${icon('cloud')}extern</span>` : ''}</div>
      <div class="ep-grid">
        <div class="seg ep-provider">${Object.entries(S.providers.providers).map(([key, pr]) =>
          `<button data-provider="${key}" class="${ep.provider === key ? 'on' : ''}">${pr.label}</button>`).join('')}</div>
        <input class="ep-url" value="${esc(ep.url)}" spellcheck="false" placeholder="http://server:1234/v1">
        <input class="ep-key ${ep.provider === 'custom' ? '' : 'hidden-field'}" value="${esc(ep.api_key || '')}" placeholder="API-Schlüssel (optional)" spellcheck="false">
        <select class="ep-model">${task === 'ocr' ? `<option value="" ${!ep.model ? 'selected' : ''}>– nicht verwenden –</option>` : ''}${models.map(m => `<option ${m === ep.model ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select>
        <button class="btn small ep-test">${icon('refresh')}Verbinden</button>
      </div></div>`;
  }).join('');
}

function readEndpoints() {
  $$('#endpoints .ep').forEach(el => {
    const ep = S.endpoints[el.dataset.task];
    ep.url = el.querySelector('.ep-url').value.trim();
    ep.api_key = el.querySelector('.ep-key').value.trim();
    ep.model = el.querySelector('.ep-model').value;
  });
  return S.endpoints;
}

$('#endpoints').addEventListener('click', async e => {
  const el = e.target.closest('.ep'); if (!el) return;
  const task = el.dataset.task, ep = readEndpoints()[task];
  const prov = e.target.closest('[data-provider]');
  if (prov) {
    ep.provider = prov.dataset.provider;
    ep.url = S.providers.providers[ep.provider].url;
    ep.model = S.providers.defaults[ep.provider][task];
    renderEndpoints();
    return;
  }
  if (e.target.closest('.ep-test')) {
    el.querySelector('.ep-test').innerHTML = '<span class="spinner"></span>Verbinde …';
    const res = await api('test_endpoint', ep, true);
    S.health.endpoints[task] = { reachable: res.ok, models: res.models, remote: res.remote,
                                 ready: res.ok && (!ep.model || res.models.includes(ep.model)) };
    renderEndpoints();
    toast(res.ok ? `Verbunden – ${res.models.length} Modelle gefunden` : `Keine Verbindung: ${esc(res.error)}`, { kind: res.ok ? 'ok' : 'error' });
  }
});
$('#ep-copy').addEventListener('click', () => {
  const src = readEndpoints().llm;
  for (const task of ['ocr', 'embedding']) {
    const ep = S.endpoints[task];
    if (ep.provider !== src.provider) ep.model = S.providers.defaults[src.provider][task];
    Object.assign(ep, { provider: src.provider, url: src.url, api_key: src.api_key });
  }
  renderEndpoints();
});
$('#effort-seg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  $$('#effort-seg button').forEach(x => x.classList.toggle('on', x === b));
  const hints = { off: 'Antwortet sofort – am schnellsten, am wenigsten genau.', low: 'Kurz überlegen, direkt zum Ergebnis.',
                  medium: 'Ausgewogen zwischen Tempo und Sorgfalt.', xhigh: 'Gründlich: prüft Annahmen und wägt Alternativen ab (Qwen-Standard).' };
  $('#effort-hint').textContent = hints[b.dataset.effort] + ' Kein Token-Limit.';
});
$('#analysis-seg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  $$('#analysis-seg button').forEach(x => x.classList.toggle('on', x === b));
});
$('#embedded-seg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  $$('#embedded-seg button').forEach(x => x.classList.toggle('on', x === b));
});
$('#policy-seg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  $$('#policy-seg button').forEach(x => x.classList.toggle('on', x === b));
});
$('#s-login').addEventListener('change', e => api('set_login_item', e.target.checked));
$('#rerun-setup').addEventListener('click', () => openWizard());

function renderComponents(h) {
  const missing = [['paddle', 'PaddleOCR installieren', '~1 GB, eigene Python-Umgebung', h.paddle],
                   ['tesseract', 'Tesseract installieren', 'über Homebrew', h.tesseract]].filter(c => !c[3]);
  $('#components').innerHTML = missing.map(([key, label, sub]) =>
    `<button class="btn small" data-component="${key}">${icon('open')}${label}<span class="muted">· ${sub}</span></button>`).join('')
    + '<span class="comp-status" id="comp-status"></span>';
}
$('#components').addEventListener('click', async e => {
  const b = e.target.closest('[data-component]'); if (!b) return;
  $$('#components [data-component]').forEach(x => x.disabled = true);
  await api('install_component', b.dataset.component);
  const timer = setInterval(async () => {
    const st = await api('pull_status');
    $('#comp-status').innerHTML = st.error ? `<span class="bad">${esc(st.error)}</span>` : `<span class="spinner"></span> ${esc(st.status || '')}`;
    if (!st.running) {
      clearInterval(timer);
      if (!st.error) toast(`${b.dataset.component === 'paddle' ? 'PaddleOCR' : 'Tesseract'} ist installiert`);
      loadSettings();
    }
  }, 1000);
});

async function checkUpdate(silent = false) {
  const res = await api('check_update');
  S.update = res;
  if (!silent) {
    $('#update-info').textContent = res.error ? `GitHub nicht erreichbar (${res.error})`
      : res.available ? `Neue Version ${res.latest} verfügbar` : `Aktuell – ${res.current} ist die neueste Version`;
    $('#update-install').classList.toggle('hidden-field', !res.available);
  }
  if (res.available && silent) {
    toast(`Update auf Version ${res.latest} verfügbar`, { kind: 'info', action: 'Aktualisieren', onAction: installUpdate, ms: 15000 });
  }
  return res;
}
async function installUpdate() {
  if (S.update && !S.update.installed) {
    toast('Entwicklungs-Version: bitte im Projektordner <code>git pull</code> ausführen', { kind: 'info', ms: 8000 });
    return;
  }
  toast('Update wird installiert – der Sortierer startet gleich neu …', { kind: 'info', ms: 20000 });
  await api('install_update');
}
function confirmDialog(title, html, okLabel) {
  return new Promise(resolve => {
    $('#dialog-title').textContent = title;
    $('#dialog-text').innerHTML = html;
    $('#dialog-ok').textContent = okLabel;
    $('#dialog').classList.add('show');
    const done = value => { $('#dialog').classList.remove('show'); resolve(value); };
    $('#dialog-ok').onclick = () => done(true);
    $('#dialog-cancel').onclick = () => done(false);
  });
}

$('#reset-all').addEventListener('click', async () => {
  const p = await api('reset_preview');
  if (!p.filed && !p.pending) { toast('Es gibt noch nichts zum Zurücksetzen', { kind: 'info' }); return; }
  const ok = await confirmDialog('Alles zurücksetzen?',
    `<b>${p.restorable}</b> sortierte${p.restorable === 1 ? 's Dokument wandert' : ' Dokumente wandern'} unter dem Originalnamen zurück in den Eingang`
    + (p.filed > p.restorable ? ` (${p.filed - p.restorable} nicht mehr auffindbar)` : '') + `.<br>`
    + `Zusätzliche Kopien und leere Ordner der App werden entfernt, Verlauf und ${p.pending} offene Vorschläge gelöscht. `
    + `Anschließend analysiert der Sortierer alles neu.`, 'Zurücksetzen');
  if (!ok) return;
  try {
    const r = await api('reset_all');
    toast(`${r.restored} Dokument(e) zurück im Eingang, ${r.folders_removed} leere Ordner entfernt – Analyse läuft neu`, { ms: 8000 });
    S.pending = []; S.currentId = null;
    poll();
  } catch (err) { toast(esc(err.message || err), { kind: 'error', ms: 9000 }); }
});

$('#update-check').addEventListener('click', () => checkUpdate(false));
$('#update-install').addEventListener('click', installUpdate);

function renderPipeline(h) {
  const step = (name, sub, ok) => `<div class="pipe-step ${ok ? 'ok' : 'off'}"><span class="pipe-dot"></span><div><b>${name}</b><small>${sub}</small></div></div>`;
  $('#pipeline').innerHTML = [
    step('PDF-Text', S.settingsEmbedded === 'digital' ? 'nur digitale PDFs' : 'aus – immer OCR', S.settingsEmbedded === 'digital'),
    step('Apple Vision', h.vision ? 'eingebaut · ~0,2 s/Seite' : 'nicht verfügbar', h.vision),
    step('GLM-OCR', h.glm ? 'bereit · genau' : 'Modell nicht verfügbar', h.glm),
    step('PaddleOCR', h.paddle ? 'lokal · bereit' : 'nicht installiert', h.paddle),
    step('Tesseract', h.tesseract ? 'letzter Ausweg' : 'nicht installiert', h.tesseract),
  ].join(`<span class="pipe-arrow">${icon('chevron')}</span>`);
}
$('#ocr-mode-seg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  $$('#ocr-mode-seg button').forEach(x => x.classList.toggle('on', x === b));
});
$$('[data-choose]').forEach(btn => btn.addEventListener('click', async () => {
  const input = $('#' + btn.dataset.choose);
  const chosen = await api('choose_folder', input.value);
  if (chosen) input.value = chosen;
  else if (!window.pywebview) toast('Ordnerauswahl gibt es nur im App-Fenster – Pfad bitte eintippen', { kind: 'info' });
}));
$('#settings-save').addEventListener('click', async () => {
  try {
    await api('save_settings', {
      incoming_dirs: S.incomingDirs, output_dir: $('#s-output').value, endpoints: readEndpoints(),
      own_names: $('#s-own').value.split('\n').map(x => x.trim()).filter(Boolean),
      compute_policy: $('#policy-seg button.on')?.dataset.policy || 'always',
      embedded_text: $('#embedded-seg button.on')?.dataset.embedded || 'never',
      analysis_mode: $('#analysis-seg button.on')?.dataset.analysis || 'ocr',
      reasoning_effort: $('#effort-seg button.on')?.dataset.effort || 'xhigh',
      ocr_mode: $('#ocr-mode-seg button.on')?.dataset.mode || 'vision',
    });
    toast('Einstellungen gespeichert');
    loadSettings(); refreshHealth(); poll();
  } catch (err) { toast(esc(err.message || err), { kind: 'error' }); }
});

function renderIncoming() {
  $('#incoming-list').innerHTML = S.incomingDirs.map((d, i) => `
    <div class="inc-row">${icon('inbox')}<code title="${esc(d)}">${esc(d)}</code>
      <button class="icon-btn small" data-i="${i}" title="Nicht mehr beobachten" ${S.incomingDirs.length < 2 ? 'disabled' : ''}>${icon('trash')}</button></div>`).join('');
}
$('#incoming-list').addEventListener('click', e => {
  const b = e.target.closest('button[data-i]'); if (!b) return;
  S.incomingDirs.splice(+b.dataset.i, 1);
  renderIncoming();
});
$('#incoming-add').addEventListener('click', async () => {
  const chosen = await api('choose_folder', S.incomingDirs[0] || '');
  if (chosen && !S.incomingDirs.includes(chosen)) { S.incomingDirs.push(chosen); renderIncoming(); }
  else if (!window.pywebview) {
    const typed = prompt('Pfad des zusätzlichen Eingangsordners');
    if (typed) { S.incomingDirs.push(typed.trim()); renderIncoming(); }
  }
});

/* ================================================================ health footer */
async function refreshHealth() {
  try {
    const h = await api('health');
    S.health = h;
    const dot = (ok, label) => `<span class="h-item ${ok ? 'ok' : 'bad'}" title="${label}: ${ok ? 'bereit' : 'nicht verfügbar'}"><i></i>${label}</span>`;
    const where = h.endpoints?.llm?.remote ? 'KI-Server (extern)' : 'KI lokal';
    $('#health').innerHTML = `<div class="h-title">${where}</div>${dot(h.llm, 'Analyse')}${dot(h.vision || h.glm, 'Texterkennung')}${dot(h.embedding, 'Suche')}`;
  } catch (err) { console.error(err); }
}

/* ================================================================ boot */
function boot() {
  document.documentElement.classList.toggle('native', !!window.pywebview);
  hydrateIcons();
  $('#nav').addEventListener('click', e => { const b = e.target.closest('button[data-view]'); if (b) showView(b.dataset.view); });
  poll();
  refreshHealth();
  api('providers').then(async p => {
    if (p.setup_pending) return openWizard();
    // Set up, but the analysis model is missing (e.g. a download never finished): offer help instead of failing silently.
    const h = await api('health');
    const llm = h.endpoints?.llm;
    if (llm && !llm.ready) {
      toast(llm.reachable ? 'Das KI-Modell für die Analyse fehlt noch.' : 'Die KI ist gerade nicht erreichbar.',
            { kind: 'info', action: 'Einrichten', onAction: () => openWizard(), ms: 20000 });
    }
  });
  setTimeout(() => checkUpdate(true), 8000);
  setInterval(() => checkUpdate(true), 12 * 3600 * 1000);
  setInterval(poll, 1500);
  setInterval(refreshHealth, 20000);
}

let booted = false;
function start() { if (!booted) { booted = true; boot(); } }
window.addEventListener('pywebviewready', start);
if (location.port === '8765') start();                        // browser mode (main.py --browser)
else setTimeout(() => { if (window.pywebview?.api) start(); }, 1500);
