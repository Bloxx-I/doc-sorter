/* Interactive folder mind map: horizontal tidy tree with pan/zoom and animated re-layout.
 *
 * mode 'review': nodes toggle as destinations, suggestions glow, the document is drawn as a
 *                ghost card attached to every chosen folder, a big "+" adds a folder.
 * mode 'browse': nodes are focused (to list their files); document counts are shown.
 */
class MindMap {
  constructor(host, opts = {}) {
    this.host = host;
    this.opts = opts;
    this.mode = opts.mode || 'review';
    this.view = { x: 40, y: 0, k: 1 };
    this.pos = new Map();         // key -> {x, y, w, h}  (current, animated)
    this.els = new Map();         // key -> element
    this.expanded = new Set(['.']);
    this.data = null;
    this.focus = '.';
    this._build();
  }

  /* ---------------------------------------------------------------- DOM scaffold */
  _build() {
    this.host.innerHTML = `
      <div class="mm-viewport">
        <div class="mm-stage"><svg class="mm-edges"><g></g></svg><div class="mm-nodes"></div></div>
      </div>
      <div class="mm-controls">
        <button class="icon-btn" data-act="zoom-out" title="Verkleinern">${icon('minus')}</button>
        <button class="icon-btn" data-act="zoom-in" title="Vergrößern">${icon('plus')}</button>
        <button class="icon-btn" data-act="fit" title="Einpassen (F)">${icon('fit')}</button>
        <span class="mm-sep"></span>
        <button class="icon-btn" data-act="expand" title="Alle Ordner auf-/zuklappen">${icon('expand')}</button>
      </div>
      ${this.mode === 'review' ? `
      <div class="mm-legend">
        <span><i class="lg lg-sel"></i>Ziel</span><span><i class="lg lg-sug"></i>Vorschlag</span><span><i class="lg lg-new"></i>Neu</span>
      </div>
      <button class="mm-fab" title="Ordner im Finder wählen – auch auf anderen Laufwerken (N)">${icon('folderPlus')}<span>Ordner wählen</span></button>` : `
      <div class="mm-legend"><span>Zahl = Dokumente inkl. Unterordner · Klick zeigt Dateien</span></div>`}`;
    this.viewport = this.host.querySelector('.mm-viewport');
    this.stage = this.host.querySelector('.mm-stage');
    this.svg = this.host.querySelector('.mm-edges g');
    this.nodesLayer = this.host.querySelector('.mm-nodes');
    this.host.querySelector('.mm-controls').addEventListener('click', e => {
      const act = e.target.closest('button')?.dataset.act;
      if (act === 'zoom-in') this.zoomBy(1.2);
      if (act === 'zoom-out') this.zoomBy(1 / 1.2);
      if (act === 'fit') this.fit(true);
      if (act === 'expand') this.toggleAll();
    });
    const fab = this.host.querySelector('.mm-fab');
    if (fab) fab.addEventListener('click', e => { e.stopPropagation(); this.opts.onAddChild?.(this.focus, fab); });
    this._bindPanZoom();
    new ResizeObserver(() => { if (this.data && !this._userMoved) this.fit(false); }).observe(this.host);
  }

  _bindPanZoom() {
    let drag = null;
    this.viewport.addEventListener('pointerdown', e => {
      if (e.target.closest('.mm-node')) return;
      drag = { x: e.clientX, y: e.clientY, vx: this.view.x, vy: this.view.y, moved: false };
      this.viewport.setPointerCapture(e.pointerId);
      this.viewport.classList.add('grabbing');
    });
    this.viewport.addEventListener('pointermove', e => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      this.view.x = drag.vx + dx; this.view.y = drag.vy + dy;
      this._userMoved = true;
      this._applyView();
    });
    const end = () => { drag = null; this.viewport.classList.remove('grabbing'); };
    this.viewport.addEventListener('pointerup', end);
    this.viewport.addEventListener('pointercancel', end);
    this.viewport.addEventListener('wheel', e => {
      e.preventDefault();
      this._userMoved = true;
      if (e.ctrlKey || e.metaKey) {
        const r = this.viewport.getBoundingClientRect();
        this.zoomBy(Math.exp(-e.deltaY * 0.012), e.clientX - r.left, e.clientY - r.top, false);
      } else {
        this.view.x -= e.deltaX; this.view.y -= e.deltaY;
        this._applyView();
      }
    }, { passive: false });
  }

  zoomBy(f, cx, cy, animate = true) {
    const r = this.viewport.getBoundingClientRect();
    cx ??= r.width / 2; cy ??= r.height / 2;
    const k = Math.min(2.2, Math.max(0.25, this.view.k * f));
    const target = { k, x: cx - (cx - this.view.x) * (k / this.view.k), y: cy - (cy - this.view.y) * (k / this.view.k) };
    this._userMoved = true;
    animate ? this._animateView(target) : (Object.assign(this.view, target), this._applyView());
  }

  _applyView() {
    const { x, y, k } = this.view;
    this.stage.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
    this.viewport.style.backgroundSize = `${22 * k}px ${22 * k}px`;
    this.viewport.style.backgroundPosition = `${x}px ${y}px`;
  }

  _animateView(target, ms = 320) {
    const from = { ...this.view }, t0 = performance.now();
    cancelAnimationFrame(this._viewAnim);
    const step = now => {
      const t = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - t, 3);
      for (const key of ['x', 'y', 'k']) this.view[key] = from[key] + (target[key] - from[key]) * e;
      this._applyView();
      if (t < 1) this._viewAnim = requestAnimationFrame(step);
    };
    this._viewAnim = requestAnimationFrame(step);
  }

  /* ---------------------------------------------------------------- data */
  setData(data, { refit = true } = {}) {
    // data: {tree, suggestions: [{path, score, reasons, new}], selected: Set, docLabel}
    const fresh = !this.data || this.data.tree !== data.tree;
    this.data = data;
    this.suggestion = new Map((data.suggestions || []).map(s => [s.path, s]));
    this._buildIndex();
    if (fresh) {
      this.expanded = new Set(['.']);
      const reveal = [...this.suggestion.keys(), ...(data.selected || [])];
      for (const p of reveal) for (const a of this._ancestors(p)) this.expanded.add(a);
      if (this.mode === 'browse') this.index.get('.').children.forEach(c => this.expanded.add(c.path));
      this.focus = [...(data.selected || [])][0] || '.';
      this._userMoved = false;
    }
    this.render();
    if (fresh && refit) requestAnimationFrame(() => this.fit(false));
  }

  update(patch) { Object.assign(this.data, patch); this.suggestion = new Map((this.data.suggestions || []).map(s => [s.path, s])); this._buildIndex(); this.render(); }

  _buildIndex() {
    this.index = new Map();
    const visit = (node, parent) => {
      const n = { path: node.path, name: node.name, count: node.count || 0, parent, children: [], virtual: !!node.virtual };
      this.index.set(n.path, n);
      (node.children || []).forEach(c => n.children.push(visit(c, n)));
      return n;
    };
    visit(this.data.tree, null);
    const total = n => (n.total = n.count + n.children.reduce((sum, c) => sum + total(c), 0));
    total(this.index.get('.'));
    // Planned folders (suggested-new) that do not exist yet
    const planned = [...(this.data.planned || [])];
    for (const s of this.data.suggestions || []) if (s.new) planned.push(s.path);
    for (const path of planned) if (!path.startsWith('/')) this._ensurePath(path);
    // Folders outside the Ablage (other drives, picked in Finder) hang under "Andere Orte"
    const external = new Set([...(this.data.external || []), ...(this.data.selected || []),
                              ...(this.data.suggestions || []).map(s => s.path)].filter(p => p.startsWith('/')));
    if (external.size) {
      const root = this.index.get('.');
      const group = { path: '§ext', name: 'Andere Orte', count: 0, parent: root, children: [], group: true };
      root.children.push(group);
      this.index.set(group.path, group);
      for (const path of external) {
        const n = { path, name: path.split('/').filter(Boolean).pop(), count: 0, parent: group, children: [], external: true,
                    drive: path.startsWith('/Volumes/') ? path.split('/')[2] : '' };
        group.children.push(n);
        this.index.set(path, n);
      }
    }
    for (const n of this.index.values()) n.children.sort((a, b) => (a.group ? 1 : 0) - (b.group ? 1 : 0) || a.name.localeCompare(b.name, 'de', { numeric: true }));
  }

  _ensurePath(path) {
    if (this.index.has(path) || path === '.') return;
    const parts = path.split('/');
    let parentPath = '.';
    parts.forEach((part, i) => {
      const p = parts.slice(0, i + 1).join('/');
      if (!this.index.has(p)) {
        const parent = this.index.get(parentPath);
        const n = { path: p, name: part, count: 0, parent, children: [], virtual: true };
        parent.children.push(n);
        this.index.set(p, n);
      }
      parentPath = p;
    });
  }

  _ancestors(path) {
    const out = [];
    let n = this.index?.get(path);
    if (n) { for (n = n.parent; n; n = n.parent) out.push(n.path); return out; }
    if (path.startsWith('/')) return ['§ext', '.'];
    const parts = path.split('/');
    for (let i = parts.length - 1; i > 0; i--) out.push(parts.slice(0, i).join('/'));
    out.push('.');
    return out;
  }

  toggleExpand(path) {
    this.expanded.has(path) ? this.expanded.delete(path) : this.expanded.add(path);
    this.render();
  }

  toggleAll() {
    const all = [...this.index.values()].filter(n => n.children.length).map(n => n.path);
    const allOpen = all.every(p => this.expanded.has(p));
    this.expanded = new Set(allOpen ? ['.'] : all);
    if (allOpen) for (const p of [...this.suggestion.keys(), ...(this.data.selected || [])]) this._ancestors(p).forEach(a => this.expanded.add(a));
    this.render();
    setTimeout(() => this.fit(true), 300);
  }

  /* Pan (animated) so that all target folders and their document cards are on screen. */
  ensureVisible() {
    requestAnimationFrame(() => setTimeout(() => {
      const r = this.viewport.getBoundingClientRect();
      const out = [...this.els.values()].some(el => {
        if (!el.classList.contains('doc') && !el.classList.contains('selected')) return false;
        const b = el.getBoundingClientRect();
        return b.right > r.right - 8 || b.left < r.left + 8 || b.top < r.top + 50 || b.bottom > r.bottom - 80;
      });
      if (out) this.fit(true);
    }, 360));
  }

  setFocus(path) { this.focus = path; this._paintClasses(); }

  /* ---------------------------------------------------------------- layout */
  _measure(text, weight = 500) {
    const ctx = this._ctx || (this._ctx = document.createElement('canvas').getContext('2d'));
    ctx.font = `${weight} 13px -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif`;
    return ctx.measureText(text).width;
  }

  _layout() {
    const selected = this.data.selected || new Set();
    const items = [];
    const H = 36, GAP = 10;
    const make = (n, depth) => {
      const sug = this.suggestion.get(n.path);
      const hidden = n.children.length && !this.expanded.has(n.path) ? n.children.length : 0;
      let w = 56 + this._measure(n.path === '.' ? n.name : n.name, n.path === '.' ? 650 : 500);
      if (sug && this.mode === 'review') w += 50;
      if (n.virtual) w += 40;
      if (n.drive) w += 16 + this._measure(n.drive, 600);
      if (this.mode === 'browse' && n.total) w += 12 + this._measure(String(n.total), 600) + 6;
      if (n.children.length) w += 26;
      const item = { key: n.path, node: n, depth, w: Math.max(96, Math.round(w)), h: H, kids: [], hidden };
      items.push(item);
      if (this.expanded.has(n.path)) n.children.forEach(c => item.kids.push(make(c, depth + 1)));
      if (this.mode === 'review' && selected.has(n.path)) {
        const label = this.data.docLabel || 'Dokument';
        const doc = { key: '§doc:' + n.path, doc: true, target: n.path, depth: depth + 1,
                      w: Math.min(280, Math.round(52 + this._measure(label))), h: 40, kids: [] };
        items.push(doc);
        item.kids.push(doc);
      }
      return item;
    };
    const root = make(this.index.get('.'), 0);
    const colW = [];
    items.forEach(it => { colW[it.depth] = Math.max(colW[it.depth] || 0, it.w); });
    const colX = [0];
    for (let d = 1; d < colW.length; d++) colX[d] = colX[d - 1] + colW[d - 1] + 60;
    let cursor = 0;
    const place = it => {
      it.x = colX[it.depth];
      if (!it.kids.length) { it.y = cursor + it.h / 2; cursor += it.h + GAP; return; }
      it.kids.forEach(place);
      it.y = (it.kids[0].y + it.kids[it.kids.length - 1].y) / 2;
      cursor += 8;
    };
    place(root);
    return { root, items };
  }

  /* ---------------------------------------------------------------- render */
  render() {
    if (!this.data) return;
    const { root, items } = this._layout();
    this.items = items;
    const target = new Map(items.map(it => [it.key, { x: it.x, y: it.y, w: it.w, h: it.h }]));
    const live = new Set(items.map(i => i.key));
    for (const [key, el] of this.els) if (!live.has(key)) { el.remove(); this.els.delete(key); this.pos.delete(key); }
    const parentOf = new Map();
    const walk = it => it.kids.forEach(k => { parentOf.set(k.key, it); walk(k); });
    walk(root);
    for (const it of items) {
      let el = this.els.get(it.key);
      if (!el) {
        el = document.createElement('div');
        this.els.set(it.key, el);
        this.nodesLayer.appendChild(el);
        const p = parentOf.get(it.key);
        const start = p && this.pos.get(p.key) ? { ...this.pos.get(p.key) } : { ...target.get(it.key) };
        start.w = it.w; start.h = it.h;
        this.pos.set(it.key, start);
        el.style.opacity = '0';
        requestAnimationFrame(() => { el.style.opacity = ''; });
        this._bindNode(el, it);
      }
      el.style.width = it.w + 'px';
      el.style.height = it.h + 'px';
      el._item = it;
      el.innerHTML = it.doc ? this._docHTML() : this._nodeHTML(it);
    }
    this.parentOf = parentOf;
    this._paintClasses();
    this._animateTo(target);
  }

  _nodeHTML(it) {
    const n = it.node, sug = this.suggestion.get(n.path);
    const selected = this.data.selected?.has(n.path);
    const label = esc(n.name);
    const glyph = selected ? 'check' : n.group ? 'drive' : 'folder';
    let html = `<span class="mm-ic">${icon(glyph)}</span><span class="mm-label">${label}</span>`;
    if (n.drive) html += `<span class="mm-drive">${esc(n.drive)}</span>`;
    if (sug && this.mode === 'review') html += `<span class="mm-score">${Math.round(sug.score * 100)}%</span>`;
    if (n.virtual) html += `<span class="mm-new">neu</span>`;
    if (this.mode === 'browse' && n.total) html += `<span class="mm-count">${n.total}</span>`;
    if (n.children.length) html += `<button class="mm-exp" title="${this.expanded.has(n.path) ? 'Zuklappen' : 'Aufklappen'}">${this.expanded.has(n.path) ? '−' : '+' + it.hidden}</button>`;
    if (this.mode === 'review' && !n.group) html += `<button class="mm-add" title="Ordner im Finder wählen">${icon('plus')}</button>`;
    return html;
  }

  _docHTML() {
    return `<span class="mm-doc-ic">${icon('file')}</span><span class="mm-label">${esc(this.data.docLabel || 'Dokument')}</span>`;
  }

  _paintClasses() {
    const selected = this.data.selected || new Set();
    const onPath = new Set();
    for (const p of selected) { onPath.add(p); this._ancestors(p).forEach(a => onPath.add(a)); }
    this.onPath = onPath;
    for (const [key, el] of this.els) {
      const it = el._item;
      if (it.doc) { el.className = 'mm-node doc'; continue; }
      const n = it.node;
      el.className = ['mm-node', n.path === '.' ? 'root' : '', selected.has(n.path) ? 'selected' : '',
        this.suggestion.has(n.path) && this.mode === 'review' ? 'suggested' : '', n.virtual ? 'virtual' : '',
        onPath.has(n.path) && !selected.has(n.path) ? 'on-path' : '', this.focus === n.path ? 'focus' : '',
        n.group ? 'group' : '', n.external ? 'external' : ''].join(' ');
    }
    this._drawEdges();
  }

  _bindNode(el, it) {
    el.addEventListener('click', e => {
      const item = el._item;
      if (item.doc) return;
      if (e.target.closest('.mm-exp')) { e.stopPropagation(); this.toggleExpand(item.key); return; }
      if (e.target.closest('.mm-add')) { e.stopPropagation(); this.setFocus(item.key); this.opts.onAddChild?.(item.key, el); return; }
      if (item.node.group) { this.toggleExpand(item.key); return; }
      this.setFocus(item.key);
      if (this.mode === 'review') this.opts.onToggle?.(item.key);
      else { this.opts.onFocus?.(item.key); if (item.node.children.length && !this.expanded.has(item.key)) this.toggleExpand(item.key); }
    });
    el.addEventListener('dblclick', e => {
      const item = el._item;
      if (!item.doc && item.node.children.length && !e.target.closest('button')) this.toggleExpand(item.key);
    });
    el.addEventListener('mouseenter', () => {
      const item = el._item;
      if (item.doc) return showTip(el, `<b>${esc(this.data.docLabel)}</b><br>landet in <code>${esc(item.target === '.' ? this.data.tree.name : item.target)}</code>`);
      const sug = this.suggestion.get(item.key);
      if (item.node.group) return showTip(el, 'Ordner außerhalb der Ablage, z. B. auf anderen Laufwerken');
      let tip = `<code>${esc(item.key === '.' ? this.data.tree.name : item.key)}</code>`;
      if (item.node.virtual) tip += `<br><span class="tip-new">wird beim Ablegen angelegt</span>`;
      if (sug && this.mode === 'review') tip += `<br><b>Vorschlag ${Math.round(sug.score * 100)}%</b> · ${sug.reasons.map(esc).join(' · ')}`;
      if (item.node.total) tip += `<br>${item.node.count} Dokument${item.node.count === 1 ? '' : 'e'} direkt · ${item.node.total} insgesamt`;
      showTip(el, tip);
    });
    el.addEventListener('mouseleave', hideTip);
  }

  _animateTo(target, ms = 340) {
    const from = new Map([...this.pos].map(([k, v]) => [k, { ...v }])), t0 = performance.now();
    cancelAnimationFrame(this._anim);
    const step = now => {
      const t = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - t, 3);
      for (const [key, to] of target) {
        const f = from.get(key) || to;
        const p = { x: f.x + (to.x - f.x) * e, y: f.y + (to.y - f.y) * e, w: to.w, h: to.h };
        this.pos.set(key, p);
        const el = this.els.get(key);
        if (el) el.style.transform = `translate(${p.x}px, ${p.y - p.h / 2}px)`;
      }
      this._drawEdges();
      if (t < 1) this._anim = requestAnimationFrame(step);
    };
    this._anim = requestAnimationFrame(step);
  }

  _drawEdges() {
    if (!this.items) return;
    let html = '';
    for (const it of this.items) {
      const parent = this.parentOf.get(it.key);
      if (!parent) continue;
      const a = this.pos.get(parent.key), b = this.pos.get(it.key);
      if (!a || !b) continue;
      const x1 = a.x + a.w, y1 = a.y, x2 = b.x, y2 = b.y, dx = Math.max(24, (x2 - x1) * 0.55);
      const d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
      let cls = 'edge';
      if (it.doc) cls += ' doc';
      else if (this.onPath?.has(it.key)) cls += ' on';
      else if (it.node.virtual) cls += ' virtual';
      else if (this.suggestion.has(it.key) && this.mode === 'review') cls += ' sug';
      html += `<path class="${cls}" d="${d}"/>`;
      if (it.doc) html += `<circle class="edge-dot" r="3.5"><animateMotion dur="1.6s" repeatCount="indefinite" path="${d}"/></circle>`;
    }
    this.svg.innerHTML = html;
  }

  fit(animate = true) {
    if (!this.items?.length) return;
    const r = this.viewport.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const padX = 48, padTop = 64, padBottom = this.mode === 'review' ? 96 : 56;
    const box = items => {
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const it of items) {
        minX = Math.min(minX, it.x); maxX = Math.max(maxX, it.x + it.w);
        minY = Math.min(minY, it.y - it.h / 2); maxY = Math.max(maxY, it.y + it.h / 2);
      }
      const k = Math.min(1.1, (r.width - padX * 2) / (maxX - minX), (r.height - padTop - padBottom) / (maxY - minY));
      return { minX, minY, maxX, maxY, k };
    };
    let b = box(this.items);
    if (b.k < 0.8 && this.mode === 'review') {
      // Too big to read: frame what matters (root, suggestions, targets, the document) instead.
      const key = it => it.doc || it.key === '.' || this.suggestion.has(it.key) || this.onPath?.has(it.key);
      const focused = box(this.items.filter(key));
      b = { ...focused, k: Math.max(focused.k, 0.55) };
    }
    const k = Math.max(0.35, b.k);
    const target = { k, x: padX + ((r.width - padX * 2) - (b.maxX - b.minX) * k) / 2 - b.minX * k,
                     y: padTop + ((r.height - padTop - padBottom) - (b.maxY - b.minY) * k) / 2 - b.minY * k };
    this._userMoved = false;
    animate ? this._animateView(target) : (Object.assign(this.view, target), this._applyView());
  }
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function showTip(el, html) {
  const tip = document.getElementById('tooltip');
  tip.innerHTML = html;
  tip.classList.add('show');
  const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
  let left = r.left + r.width / 2 - t.width / 2, top = r.top - t.height - 10;
  if (top < 8) top = r.bottom + 10;
  left = Math.max(8, Math.min(window.innerWidth - t.width - 8, left));
  tip.style.transform = `translate(${left}px, ${top}px)`;
}

function hideTip() { document.getElementById('tooltip').classList.remove('show'); }
