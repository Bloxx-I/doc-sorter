/* First-run setup wizard: AI provider (LM Studio / Ollama / own server), models, OCR, folders. */

const WZ = {
  step: 0,
  steps: ['Willkommen', 'KI-Quelle', 'Modelle', 'Texterkennung', 'Ordner', 'Fertig'],
  provider: 'lmstudio', url: '', key: '', models: [], connected: false,
  ocr: 'vision', incoming: '', output: '', settings: null, providers: null,
};

const RECOMMENDED = {};   // filled per provider from the model catalogue (model_catalog)

async function openWizard() {
  const [settings, providers] = await Promise.all([api('settings'), api('providers')]);
  WZ.settings = settings; WZ.providers = providers;
  const ep = settings.endpoints.llm;
  WZ.catalogue = null; WZ.ocrTouched = false;
  Object.assign(WZ, { step: 0, provider: ep.provider, url: ep.url, key: ep.api_key || '', connected: false,
                      ocr: settings.analysis_mode === 'vision' ? 'direct' : settings.ocr_mode || 'vision', incoming: settings.incoming_dirs[0] || '', output: settings.output_dir,
                      chosen: { llm: ep.model, embedding: settings.endpoints.embedding.model, ocr: settings.endpoints.ocr.model } });
  if (!providers.installed[WZ.provider] && WZ.provider !== 'custom') {
    WZ.provider = providers.installed.lmstudio ? 'lmstudio' : providers.installed.ollama ? 'ollama' : WZ.provider;
    WZ.url = providers.providers[WZ.provider].url;
  }
  $('#wz-login').checked = settings.setup_pending || await api('login_item');   // keep what the user has
  $('#wizard').classList.add('show');
  renderWizard();
}

function renderWizard() {
  $('#wz-steps').innerHTML = WZ.steps.map((name, i) =>
    `<div class="${i === WZ.step ? 'on' : i < WZ.step ? 'done' : ''}"><span>${i < WZ.step ? icon('check') : i + 1}</span>${name}</div>`).join('');
  $$('.wz-page').forEach(p => p.classList.toggle('show', +p.dataset.step === WZ.step));
  $('#wz-back').style.visibility = WZ.step ? 'visible' : 'hidden';
  $('#wz-next').innerHTML = WZ.step === WZ.steps.length - 1 ? `${icon('check')}Los geht's` : 'Weiter';
  ({ 1: renderProviders, 2: renderModels, 3: renderOcr, 4: renderFolders, 5: renderSummary })[WZ.step]?.();
}

/* ---- step 1: provider */
function renderProviders() {
  const inst = WZ.providers.installed;
  const card = (key, title, text, badge) => `
    <button data-provider="${key}" class="${WZ.provider === key ? 'on' : ''}">
      <b>${title}</b><span>${text}</span>${badge}</button>`;
  const ok = '<em class="ok">installiert</em>', missing = url => `<em class="warn">nicht installiert · <a href="${url}" data-ext="${url}">herunterladen</a></em>`;
  $('#wz-providers').innerHTML =
    card('lmstudio', 'LM Studio', 'Komfortable App mit eigener Oberfläche. Wird bei Bedarf automatisch gestartet.', inst.lmstudio ? ok : missing('https://lmstudio.ai')) +
    card('ollama', 'Ollama', 'Schlank, läuft unsichtbar im Hintergrund. Modelle lädt der Sortierer selbst.', inst.ollama ? ok : missing('https://ollama.com/download')) +
    card('custom', 'Eigener Server', 'Ein schneller Rechner im Heimnetz (LM Studio, Ollama, vLLM …).', '<em>OpenAI-kompatibel</em>');
  $('#wz-url').value = WZ.url;
  $('#wz-key').value = WZ.key;
  $('#wz-key-row').style.display = WZ.provider === 'custom' ? '' : 'none';
  $('#wz-status').innerHTML = WZ.connected ? `<span class="ok">${icon('check')}Verbunden – ${WZ.models.length} Modelle verfügbar</span>` : '';
}
$('#wz-providers').addEventListener('click', e => {
  const link = e.target.closest('[data-ext]');
  if (link) { e.preventDefault(); api('open_url', link.dataset.ext); return; }
  const b = e.target.closest('[data-provider]'); if (!b) return;
  WZ.provider = b.dataset.provider;
  WZ.url = WZ.providers.providers[WZ.provider].url;
  WZ.connected = false; WZ.catalogue = null;
  renderProviders();
});
$('#wz-test').addEventListener('click', wizardConnect);
async function wizardConnect() {
  WZ.url = $('#wz-url').value.trim(); WZ.key = $('#wz-key').value.trim();
  $('#wz-status').innerHTML = `<span class="spinner"></span> ${WZ.provider === 'custom' ? 'Verbinde …' : 'Starte ' + WZ.providers.providers[WZ.provider].label + ' und verbinde …'}`;
  const res = await api('test_endpoint', { provider: WZ.provider, url: WZ.url, api_key: WZ.key }, true);
  WZ.connected = res.ok; WZ.models = res.models || [];
  $('#wz-status').innerHTML = res.ok
    ? `<span class="ok">${icon('check')}Verbunden – ${WZ.models.length} Modelle verfügbar</span>${res.remote ? `<span class="warn">${icon('cloud')}Dokumenttexte werden an diesen Server geschickt.</span>` : ''}`
    : `<span class="bad">${icon('alert')}Keine Verbindung (${esc(res.error)}). ${WZ.provider === 'lmstudio' ? 'Ist LM Studio installiert und der Server in LM Studio erlaubt?' : WZ.provider === 'ollama' ? 'Ist Ollama installiert?' : 'Stimmt die Adresse (inkl. /v1)?'}</span>`;
  return res.ok;
}

/* ---- step 2: models */
function hasModel(name) {
  const base = n => n.toLowerCase().split(':')[0].split('@')[0];
  return WZ.models.some(m => m === name || base(m) === base(name) || base(m).endsWith('/' + base(name)) || base(name).endsWith('/' + base(m)));
}
function chosenModels() {
  const embed = WZ.catalogue?.embedding;
  const list = [{ task: 'llm', model: WZ.llmModel, label: 'Analyse & Umbenennung', size: WZ.llmSize || '' }];
  if (embed) list.push({ task: 'embedding', model: embed, label: 'Suche (Embedding)', size: '≈0,3 GB' });
  return list;
}
async function renderModels() {
  if (WZ.provider === 'custom') {
    $('#wz-tiers').innerHTML = ''; $('#wz-custom').style.display = 'none';
    $('#wz-models-lead').textContent = 'Wähle aus den Modellen, die dein Server anbietet.';
    $('#wz-models').innerHTML = ['llm', 'embedding', 'ocr'].map(task => {
      const opts = WZ.models.filter(TASK_INFO[task].filter);
      return `<label class="wz-model pick"><div><b>${TASK_INFO[task].title}</b><small>${TASK_INFO[task].sub}</small></div>
        <select data-task="${task}">${task !== 'llm' ? '<option value="">– nicht verwenden –</option>' : ''}${opts.map(m =>
          `<option ${m === WZ.chosen[task] ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select></label>`;
    }).join('');
    $('#wz-pull').style.display = 'none';
    return;
  }
  $('#wz-custom').style.display = '';
  if (!WZ.catalogue || WZ.catalogue.provider !== WZ.provider) {
    WZ.catalogue = { ...(await api('model_catalog', WZ.provider)), provider: WZ.provider };
    const tier = WZ.catalogue.tiers.find(t => t.id === WZ.catalogue.recommended);
    Object.assign(WZ, { tier: tier.id, llmModel: tier.model, llmSize: tier.size, llmVision: tier.vision, llmDirect: tier.direct });
  }
  const c = WZ.catalogue;
  $('#wz-models-lead').textContent = `Größere Modelle sind genauer, brauchen aber mehr Arbeitsspeicher. Dein Mac hat ${c.ram} GB – die passende Größe ist vorausgewählt.`;
  $('#wz-tiers').innerHTML = c.tiers.map(t => `
    <button data-tier="${t.id}" class="${WZ.tier === t.id ? 'on' : ''} ${t.fits ? '' : 'toobig'}">
      <b>${t.label}</b>${t.id === c.recommended ? '<em class="ok">passt zu deinem Mac</em>' : !t.fits ? `<em class="warn">braucht ${t.min_ram} GB</em>` : ''}
      <span>${t.text}</span><code>${esc(t.model)} · ${t.size}</code>
      ${hasModel(t.model) ? '<em class="ok">bereits geladen</em>' : ''}</button>`).join('');
  $('#wz-models').innerHTML = chosenModels().map(m => {
    const have = hasModel(m.model);
    return `<div class="wz-model ${have ? 'have' : ''}"><div><b>${m.label}</b><small><code>${esc(m.model)}</code>${m.size ? ' · ' + m.size : ''}</small></div>
      <em>${have ? `${icon('check')}vorhanden` : 'wird geladen'}</em></div>`;
  }).join('');
  $('#wz-pull').style.display = chosenModels().some(m => !hasModel(m.model)) ? '' : 'none';
}
$('#wz-tiers').addEventListener('click', e => {
  const b = e.target.closest('[data-tier]'); if (!b) return;
  const t = WZ.catalogue.tiers.find(x => x.id === b.dataset.tier);
  Object.assign(WZ, { tier: t.id, llmModel: t.model, llmSize: t.size, llmVision: t.vision, llmDirect: t.direct });
  WZ.ocrTouched = false;
  $('#wz-custom-state').textContent = '';
  renderModels();
});
$('#wz-custom-check').addEventListener('click', async () => {
  const name = $('#wz-custom-name').value.trim();
  if (!name) return;
  $('#wz-custom-state').innerHTML = '<span class="spinner"></span>';
  const exists = hasModel(name) || await api('check_model', WZ.provider, name);
  if (exists === false) { $('#wz-custom-state').innerHTML = `<span class="bad">${icon('alert')}Gibt es bei ${WZ.providers.providers[WZ.provider].label} nicht</span>`; return; }
  Object.assign(WZ, { tier: 'custom', llmModel: name, llmSize: '', llmVision: null, llmDirect: null });
  $('#wz-custom-state').innerHTML = `<span class="ok">${icon('check')}${exists === null ? 'Übernommen (offline nicht prüfbar)' : 'Gefunden – übernommen'}</span>`;
  renderModels();
});
$('#wz-custom-name').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); $('#wz-custom-check').click(); } });
$('#wz-models').addEventListener('change', e => {
  if (e.target.dataset.task && e.target.tagName === 'SELECT') WZ.chosen[e.target.dataset.task] = e.target.value;
});
$('#wz-pull').addEventListener('click', async () => {
  const models = chosenModels().filter(m => !hasModel(m.model)).map(m => m.model);
  if (!models.length) return;
  $('#wz-pull').disabled = true;
  $('#wz-progress').classList.add('show');
  await api('pull_models', { provider: WZ.provider, url: WZ.url }, models);
  const timer = setInterval(async () => {
    const st = await api('pull_status');
    $('#wz-progress span').textContent = st.error ? `Fehler: ${st.error}` : `${st.model}: ${st.status}${st.fraction != null ? ' · ' + Math.round(st.fraction * 100) + '%' : ''}`;
    $('#wz-progress i').style.width = st.fraction != null ? Math.round(st.fraction * 100) + '%' : '35%';
    $('#wz-progress').classList.toggle('indeterminate', st.fraction == null && st.running);
    if (!st.running) {
      clearInterval(timer);
      $('#wz-pull').disabled = false;
      if (!st.error) { $('#wz-progress span').textContent = 'Alle Modelle sind da.'; $('#wz-progress i').style.width = '100%'; }
      const res = await api('test_endpoint', { provider: WZ.provider, url: WZ.url, api_key: WZ.key }, false);
      WZ.models = res.models || WZ.models;
      renderModels();
    }
  }, 700);
});

/* ---- step 3: OCR */
async function renderOcr() {
  if (WZ.provider !== 'custom' && WZ.llmVision === null && WZ.llmModel) {
    WZ.llmVision = await api('model_reads_images', { provider: WZ.provider, url: WZ.url, api_key: WZ.key }, installedName(WZ.llmModel));
  }
  const vision = WZ.provider === 'custom' ? true : WZ.llmVision !== false;
  const direct = $('#wz-ocr [data-ocr="direct"]');
  direct.disabled = !vision;
  const recommendDirect = vision && WZ.llmDirect !== false;
  direct.querySelector('span').textContent = !vision ? 'Dieses Modell kann keine Bilder lesen.'
    : recommendDirect ? 'Die Seiten gehen als Bilder an das Analysemodell – ohne OCR, beste Qualität. Empfohlen für dieses Modell.'
    : 'Möglich, aber für kleine Modelle nicht empfohlen – Apple Vision + KI ist hier schneller und zuverlässiger.';
  if (!WZ.ocrTouched) WZ.ocr = recommendDirect ? 'direct' : 'vision';
  if (!vision && WZ.ocr === 'direct') WZ.ocr = 'vision';
  $$('#wz-ocr button').forEach(b => b.classList.toggle('on', b.dataset.ocr === WZ.ocr));
}
function installedName(name) {
  const base = n => n.toLowerCase().split(':')[0].split('@')[0];
  return WZ.models.find(m => m === name || base(m) === base(name) || base(m).endsWith('/' + base(name)) || base(name).endsWith('/' + base(m))) || name;
}
$('#wz-ocr').addEventListener('click', e => { const b = e.target.closest('[data-ocr]'); if (b && !b.disabled) { WZ.ocr = b.dataset.ocr; WZ.ocrTouched = true; renderOcr(); } });

/* ---- step 4: folders */
function renderFolders() {
  $('#wz-in').textContent = WZ.incoming || '– noch nicht gewählt –';
  $('#wz-out').textContent = WZ.output || '– noch nicht gewählt –';
  $('#wz-icloud').style.display = WZ.settings.icloud ? '' : 'none';
  if (!$('#wz-own').value) $('#wz-own').value = (WZ.settings.own_names || []).join('\n');
}
$$('[data-wz-choose]').forEach(b => b.addEventListener('click', async () => {
  const key = b.dataset.wzChoose === 'in' ? 'incoming' : 'output';
  const chosen = await api('choose_folder', WZ[key]);
  if (chosen) { WZ[key] = chosen; renderFolders(); }
}));
$('#wz-icloud').addEventListener('click', () => { WZ.output = WZ.settings.icloud + '/Dokumente'; renderFolders(); });

/* ---- step 5: summary + save */
function wizardEndpoints() {
  const base = { provider: WZ.provider, url: WZ.url, api_key: WZ.key };
  if (WZ.provider === 'custom') {
    return { llm: { ...base, model: WZ.chosen.llm || '' }, ocr: { ...base, model: WZ.chosen.ocr || '' },
             embedding: { ...base, model: WZ.chosen.embedding || '' } };
  }
  const embed = WZ.catalogue?.embedding || '';
  return { llm: { ...base, model: installedName(WZ.llmModel) },
           ocr: { ...base, model: WZ.ocr === 'glm' ? installedName('glm-ocr') : '' },
           embedding: { ...base, model: embed ? installedName(embed) : '' } };
}
function renderSummary() {
  const eps = wizardEndpoints();
  $('#wz-summary').innerHTML = `
    <div>${icon('sparkle')}<span>KI: <b>${WZ.providers.providers[WZ.provider].label}</b> · ${esc(eps.llm.model)}</span></div>
    <div>${icon('scan')}<span>Texterkennung: <b>${{ glm: 'GLM-OCR', direct: 'Direkt an die KI (Bilder)' }[WZ.ocr] || 'Apple Vision'}</b></span></div>
    <div>${icon('inbox')}<span>Eingang: <code>${esc(WZ.incoming)}</code></span></div>
    <div>${icon('folder')}<span>Ablage: <code>${esc(WZ.output)}</code></span></div>`;
}

$('#wz-back').addEventListener('click', () => { WZ.step = Math.max(0, WZ.step - 1); renderWizard(); });
$('#wz-next').addEventListener('click', async () => {
  if (WZ.step === 1 && !WZ.connected && !(await wizardConnect())) {
    toast('Ohne Verbindung zur KI funktioniert die Umbenennung nicht. Du kannst trotzdem weiter und es später einrichten.', { kind: 'info', ms: 6000 });
    if (!WZ.warned) { WZ.warned = true; return; }
  }
  if (WZ.step === 4 && (!WZ.incoming || !WZ.output)) { toast('Bitte beide Ordner wählen', { kind: 'info' }); return; }
  if (WZ.step < WZ.steps.length - 1) { WZ.step++; renderWizard(); return; }
  try {
    await api('save_settings', { incoming_dirs: [WZ.incoming], output_dir: WZ.output, endpoints: wizardEndpoints(),
                                 ocr_mode: WZ.ocr === 'direct' ? 'vision' : WZ.ocr, analysis_mode: WZ.ocr === 'direct' ? 'vision' : 'ocr',
                                 own_names: $('#wz-own').value.split('\n').map(x => x.trim()).filter(Boolean) });
    await api('set_login_item', $('#wz-login').checked);
    $('#wizard').classList.remove('show');
    toast('Eingerichtet – leg einfach ein PDF in den Eingang!', { ms: 7000 });
    poll(); refreshHealth();
  } catch (err) { toast(esc(err.message || err), { kind: 'error', ms: 9000 }); }
});
