/* 蜗牛4K 精选 → 115 转存工作台 前端逻辑 */

const $ = (id) => document.getElementById(id);
const state = {
  category: 1,
  page: 1,
  pages: 1,
  keyword: '',
  sort: 'default',
  orderDir: 'desc',
  // 服务端筛选项（站点真实支持）
  year: '',
  area: '',
  genre: '',
  serverOrder: '',
  minScore: null,
  items: [],
  selected: new Map(),   // vod_id -> {title, link_indexes}
  facets: { year: [], region: [], quality: [] },
  options: { genres: [], regions: [], orders: [], categories: [] },
  hasNext: false,
  cid: '0',
  crumb: [],
  template: '',
  templates: [],
  genreRules: null,
};

const DEFAULT_TEMPLATE = '{category}/{genre}/{year}/{title}';

/* ---------------- 工具 ---------------- */
function toast(msg, ms = 2200) {
  const el = $('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let data;
  try { data = await res.json(); } catch { data = { ok: false, error: '响应解析失败' }; }
  if (!res.ok || data.ok === false) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtSize(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 ? 0 : 1) + u[i];
}

/* ---------------- 状态栏 ---------------- */
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const st = $('connSite'), h = $('conn115');
    st.className = 'conn ' + (s.site.logged_in ? 'ok' : 'bad');
    st.innerHTML = `站点 <b>${s.site.logged_in ? '已登录' : '未登录'}</b>`;
    st.title = s.site.error || '';
    h.className = 'conn ' + (s.p115.logged_in ? 'ok' : 'bad');
    h.innerHTML = `115 <b>${s.p115.logged_in ? (s.p115.user_name || '已登录') : '未登录'}</b>`;
    h.title = s.p115.error || '';
    if (s.default_cid && state.cid === '0') state.cid = s.default_cid;
  } catch (e) { /* 忽略 */ }
}

async function loadLayout() {
  try {
    const { config } = await api('/api/config');
    state.template = (config.layout && config.layout.template) || DEFAULT_TEMPLATE;
    if ($('tplInput')) $('tplInput').value = state.template;
  } catch (e) { state.template = DEFAULT_TEMPLATE; }
}

async function loadOptions() {
  try {
    const d = await api('/api/facets');
    state.options = {
      genres: d.genres || [],
      regions: d.regions || [],
      orders: d.orders || [],
      categories: d.categories || [],
    };
    $('svOrder').innerHTML = (d.orders || []).map((o) =>
      `<option value="${esc(o.value)}">${esc(o.name)}</option>`).join('') ||
      '<option value="">网站默认</option>';
  } catch (e) { /* 忽略 */ }
}

/* ---------------- 分类与筛选 ---------------- */
async function loadTabs() {
  const { categories } = await api('/api/categories');
  $('tabs').innerHTML = categories.map((c) =>
    `<div class="tab" data-cat="${c.id}">${esc(c.name)}</div>`).join('');
  $('tabs').querySelectorAll('.tab').forEach((el) => {
    el.onclick = () => {
      state.category = Number(el.dataset.cat);
      state.keyword = '';
      $('searchInput').value = '';
      state.page = 1; state.pages = 1;
      state.year = ''; state.area = ''; state.genre = '';
      load();
    };
  });
  syncTabs();
}

function syncTabs() {
  $('tabs').querySelectorAll('.tab').forEach((el) =>
    el.classList.toggle('active', Number(el.dataset.cat) === state.category && !state.keyword));
}

function renderFacets() {
  const bar = $('filterBar');
  const parts = [];
  const group = (label, key, values) => {
    if (!values || !values.length) return;
    parts.push(`<span class="chip-sep">${label}</span>`);
    for (const v of values.slice(0, 16)) {
      const on = state[key] === v.value;
      parts.push(`<span class="chip ${on ? 'on' : ''}" data-key="${key}" data-val="${esc(v.value)}">${esc(v.value)}${v.count ? ` <i>${v.count}</i>` : ''}</span>`);
    }
  };
  // 年份来自服务端分面；类型/地区来自站点支持的筛选清单
  group('类型', 'genre', (state.options.genres || []).map((g) => ({ value: g })));
  group('年份', 'year', state.facets.year);
  group('地区', 'area', state.facets.region);
  if (state.year || state.area || state.genre) {
    parts.push('<span class="chip" data-clear="1">✕ 清除筛选</span>');
  }
  bar.innerHTML = parts.join('');

  bar.querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      if (el.dataset.clear) {
        state.year = ''; state.area = ''; state.genre = '';
      } else {
        const key = el.dataset.key, val = el.dataset.val;
        // 服务端筛选：切换后需要重新请求站点（结果集变了）
        state[key] = (state[key] === val) ? '' : val;
      }
      state.page = 1;
      load();
    };
  });
}

/* ---------------- 列表 ---------------- */
function skeleton(n = 12) {
  $('grid').innerHTML = Array.from({ length: n }, () => '<div class="skeleton"></div>').join('');
  $('empty').hidden = true;
}

async function load() {
  skeleton();
  syncTabs();
  const p = new URLSearchParams({
    page: String(state.page),
    pages: String(state.pages),
    sort: state.sort,
    order_dir: state.orderDir,
  });
  if (state.keyword) p.set('keyword', state.keyword);
  else p.set('category', String(state.category));
  if (state.year) p.set('year', state.year);
  if (state.area) p.set('area', state.area);
  if (state.genre) p.set('genre', state.genre);
  if (state.serverOrder) p.set('order', state.serverOrder);
  if (state.minScore != null) p.set('min_score', String(state.minScore));

  try {
    const data = await api('/api/videos?' + p.toString());
    state.items = data.items;
    state.facets = data.facets;
    state.hasNext = data.has_next;
    const sf = data.server_filters || {};
    const active = [sf.year && `年份 ${sf.year}`, sf.area && `地区 ${sf.area}`, sf.class && `类型 ${sf.class}`]
      .filter(Boolean).join(' · ');
    $('pageInfo').textContent =
      `第 ${data.page}–${data.last_page} 页 · 本页 ${data.collected} 条 · 显示 ${data.shown} 条` +
      (data.total_pages ? ` · 全站 ${data.total_pages} 页` : '') +
      (active ? ` · 筛选：${active}` : '') +
      `（单次最多 ${data.max_pages} 页）`;
    $('btnMore').disabled = !data.has_next;
    renderFacets();
    renderList();
  } catch (e) {
    $('grid').innerHTML = '';
    $('empty').hidden = false;
    $('empty').textContent = '加载失败：' + e.message;
    $('btnMore').disabled = true;
  }
}

function passesFilters(it) {
  // year/area/genre 已由服务端筛选，这里只处理评分下限
  if (state.minScore != null && Number(it.score || 0) < state.minScore) return false;
  return true;
}

function renderList() {
  const items = state.items.filter(passesFilters);
  if (!items.length) {
    $('grid').innerHTML = '';
    $('empty').hidden = false;
    $('empty').textContent = state.items.length ? '当前筛选条件下没有影片' : '没有内容';
    return;
  }
  $('empty').hidden = true;
  $('grid').innerHTML = items.map((it) => {
    const sel = state.selected.has(it.id);
    return `<div class="card ${sel ? 'selected' : ''}" data-id="${it.id}">
      <div class="pick">✓</div>
      <img loading="lazy" src="${esc(it.poster)}" alt="${esc(it.title)}" referrerpolicy="no-referrer">
      <div class="badges">
        ${it.score ? `<span class="badge score">★ ${esc(it.score)}</span>` : ''}
        ${it.quality ? `<span class="badge">${esc(it.quality)}</span>` : ''}
      </div>
      <div class="info">
        <div class="t">${esc(it.title)}</div>
        <div class="m">${esc(it.year || '')} · ${esc(it.region || '')}</div>
      </div>
    </div>`;
  }).join('');

  $('grid').querySelectorAll('.card').forEach((el) => {
    const id = Number(el.dataset.id);
    el.onclick = (ev) => {
      if (ev.shiftKey || ev.ctrlKey || ev.metaKey) { toggleSelect(id); return; }
      openDetail(id);
    };
    el.oncontextmenu = (ev) => { ev.preventDefault(); toggleSelect(id); };
  });
}

function toggleSelect(id) {
  if (state.selected.has(id)) state.selected.delete(id);
  else {
    const it = state.items.find((x) => x.id === id);
    // 带上当前浏览的类型，作为目录名里的权威 genre
    state.selected.set(id, { vod_id: id, title: it ? it.title : '', link_indexes: null, genre: state.genre || '' });
  }
  updateSelBar();
  renderList();
}

function updateSelBar() {
  const n = state.selected.size;
  $('selCount').textContent = String(n);
  $('btnTransferTop').disabled = n === 0;
  $('selbar').hidden = n === 0;
  $('selText').textContent = `已选择 ${n} 部影片（右键或 Ctrl+点击可多选）`;
}

/* ---------------- 详情 ---------------- */
async function openDetail(id) {
  const modal = $('detailModal');
  modal.hidden = false;
  $('detailBody').innerHTML = '<div class="muted">加载中…（会请求站点详情页）</div>';
  try {
    const d = await api(`/api/videos/${id}`);
    const links = (d.links || []).map((l, i) => {
      const cls = l.locked ? 'locked-note' : '';
      const action = l.locked
        ? '<span class="locked-note">需登录站点</span>'
        : `<button class="btn small primary" data-link="${i}">转存</button>`;
      return `<div class="link-item">
        <div>
          <div class="lt">${esc(l.title || '(无标题)')}</div>
          <div class="lg">${esc(l.group || '')} · ${esc(l.display || '')}</div>
        </div>
        <div>${action}</div>
      </div>`;
    }).join('') || '<div class="locked-note">没有解析到网盘资源</div>';

    $('detailBody').innerHTML = `
      <div class="detail-body">
        ${d.poster ? `<img class="poster" src="${esc(d.poster)}" referrerpolicy="no-referrer">` : ''}
        <div class="detail-main">
          <h2>${esc(d.title || ('#' + d.id))}</h2>
          <div class="tags">${(d.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join('')}</div>
          ${Object.entries(d.meta || {}).map(([k, v]) =>
            `<div class="meta-line"><b>${esc(k)}</b>：${esc(v)}</div>`).join('')}
          <div class="desc">${esc(d.desc || '暂无简介')}</div>
          <div class="muted">${d.cached ? '（缓存数据）' : ''} 共 ${d.link_count} 条资源，可用 ${d.usable_count} 条</div>
          <div class="link-list">${links}</div>
          <div class="modal-actions">
            <button class="btn ghost" id="btnPickAll">选择该片转存</button>
          </div>
        </div>
      </div>`;

    $('detailBody').querySelectorAll('[data-link]').forEach((btn) => {
      btn.onclick = () => transferSingle(id, Number(btn.dataset.link));
    });
    const pickAll = $('detailBody').querySelector('#btnPickAll');
    if (pickAll) pickAll.onclick = () => {
      const it = state.items.find((x) => x.id === id);
      state.selected.set(id, { vod_id: id, title: it ? it.title : d.title, link_indexes: null, genre: state.genre || '' });
      updateSelBar(); renderList();
      $('detailModal').hidden = true;
      openTransfer();
    };
  } catch (e) {
    $('detailBody').innerHTML = `<div class="msg err">加载失败：${esc(e.message)}</div>`;
  }
}

async function transferSingle(id, linkIndex) {
  try {
    const d = await api(`/api/videos/${id}`);
    const link = (d.links || [])[linkIndex];
    if (!link || link.locked) throw new Error('该链接不可用（需先登录站点）');
    const only = { vod_id: id, link_indexes: [linkIndex], title: d.title, genre: state.genre || '' };
    const plan = await api('/api/transfer/plan', {
      method: 'POST',
      body: JSON.stringify({ selections: [only], cid: state.cid }),
    });
    $('detailModal').hidden = true;
    state.selected.clear();
    state.selected.set(id, only);
    updateSelBar();
    openTransfer(plan.plan);
  } catch (e) { toast(e.message); }
}

/* ---------------- 转存 ---------------- */
async function openTransfer(prePlan = null) {
  $('transferModal').hidden = false;
  $('jobBox').hidden = true;
  $('transferMsg').textContent = '';
  $('planBox').innerHTML = '';
  if (!($('tplInput').value || '').trim()) $('tplInput').value = state.template || DEFAULT_TEMPLATE;
  await loadDirs(state.cid);
  previewTemplate();
  if (prePlan) renderPlan(prePlan);
}

async function loadDirs(cid) {
  $('dirList').innerHTML = '<div class="dir-empty">加载中…</div>';
  try {
    const d = await api('/api/p115/dirs?cid=' + encodeURIComponent(cid));
    state.cid = d.cid;
    state.crumb = d.path || [];
    $('crumb').innerHTML = (d.path || [])
      .map((p, i, arr) => `<span data-cid="${esc(p.cid)}">${esc(p.name)}</span>${i < arr.length - 1 ? ' / ' : ''}`)
      .join('');
    $('crumb').querySelectorAll('span').forEach((el) => {
      el.onclick = () => loadDirs(el.dataset.cid);
    });
    const folders = d.folders || [];
    const files = (d.files || []).slice(0, 30);
    $('dirList').innerHTML =
      (folders.length ? folders.map((f) =>
        `<div class="dir-item" data-dir="${esc(f.id)}">📁 ${esc(f.name)}</div>`).join('')
        : '<div class="dir-empty">（当前目录没有子文件夹）</div>') +
      files.map((f) =>
        `<div class="dir-item file">📄 ${esc(f.name)} <span class="muted">${fmtSize(f.size)}</span></div>`).join('');
    $('dirList').querySelectorAll('[data-dir]').forEach((el) => {
      el.onclick = () => loadDirs(el.dataset.dir);
    });
  } catch (e) {
    $('dirList').innerHTML = `<div class="dir-empty">目录读取失败：${esc(e.message)}</div>`;
  }
  // 上一级
  $('dirUp').onclick = () => {
    const path = state.crumb || [];
    if (path.length >= 2) loadDirs(path[path.length - 2].cid);
  };
}

function selectionsPayload() {
  return [...state.selected.values()];
}

function renderPlan(plan) {
  $('planBox').innerHTML = `
    <div class="muted">目标目录 cid：<b>${esc(plan.cid)}</b> · 影片 ${plan.videos} 部 · 链接 ${plan.links} 条
      ${plan.template ? `· 模板 <code>${esc(plan.template)}</code>` : '· <span class="tpl-warn">未启用自动分层</span>'}</div>
    ${plan.items.map((it) => `
      <div style="margin-top:10px">
        <div><b>${esc(it.title)}</b>
          <span class="muted">${esc(it.category || '')} / ${esc(it.genre || '')} / ${esc(it.year || '')}</span></div>
        ${it.rel_path ? `<div class="muted">→ 将创建并转存到：<code>${esc(it.rel_path)}</code></div>` : ''}
        <div class="muted">可用资源 ${it.available_links} 条</div>
        ${it.picked.map((p) => `<div class="pv"><span class="pl">${esc(p.title || p.kind)}</span><span class="pl">${esc(p.display || '')}</span></div>`).join('')}
        ${(it.skipped || []).map((s) => `<div class="pv"><span class="pl">已跳过：${esc(s)}</span></div>`).join('')}
        ${it.warning ? `<div class="msg err">${esc(it.warning)}</div>` : ''}
      </div>`).join('')}`;
}

function transferPayload() {
  return {
    selections: selectionsPayload(),
    cid: state.cid,
    template: ($('tplInput') ? $('tplInput').value.trim() : '') || DEFAULT_TEMPLATE,
    use_template: ($('tplInput') ? $('tplInput').value.trim() : '') !== '-',
  };
}

async function previewTemplate() {
  const tpl = ($('tplInput').value || '').trim();
  state.template = tpl;
  const box = $('tplPreview');
  if (state.selected.size === 0) {
    box.innerHTML = '<div class="tpl-row">先勾选影片，这里会显示实际生成的目录。</div>';
    return;
  }
  box.innerHTML = '<div class="tpl-row">生成中…</div>';
  try {
    const { plan } = await api('/api/transfer/plan', {
      method: 'POST',
      body: JSON.stringify(transferPayload()),
    });
    const rows = plan.items.map((it) =>
      `<div class="tpl-row"><b>${esc(it.title)}</b> → <code>${esc(it.rel_path || '(直接转存到当前目录)')}</code>
       ${it.warning ? `<span class="tpl-warn">· ${esc(it.warning)}</span>` : ''}</div>`).join('');
    const uniq = new Set(plan.items.map((i) => i.rel_path).filter(Boolean));
    box.innerHTML = rows + `<div class="tpl-row">共 ${uniq.size} 个目标目录</div>`;
  } catch (e) {
    box.innerHTML = `<div class="tpl-row tpl-warn">${esc(e.message)}</div>`;
  }
}

/* ---------- 类型规则编辑 ---------- */
async function openGenres() {
  $('genreModal').hidden = false;
  $('genreMsg').textContent = '';
  try {
    const d = await api('/api/genres');
    state.genreRules = d.rules;
    renderGenres();
  } catch (e) {
    $('genreBody').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

function renderGenres() {
  const rules = state.genreRules || {};
  $('genreBody').innerHTML = Object.entries(rules).map(([name, words]) => `
    <div class="genre-row">
      <input class="input g-name" value="${esc(name)}" data-role="name">
      <input class="input g-words" value="${esc((words || []).join('、'))}" data-role="words"
             placeholder="关键词，用顿号或逗号分隔">
      <button class="btn ghost small g-del" data-role="del">删</button>
    </div>`).join('') || '<div class="muted">暂无规则</div>';

  $('genreBody').querySelectorAll('[data-role="del"]').forEach((btn) => {
    btn.onclick = () => { btn.closest('.genre-row').remove(); };
  });
}

function collectGenres() {
  const rules = {};
  $('genreBody').querySelectorAll('.genre-row').forEach((row) => {
    const name = row.querySelector('[data-role="name"]').value.trim();
    const words = row.querySelector('[data-role="words"]').value
      .split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean);
    if (name) rules[name] = words;
  });
  return rules;
}

$('btnPreview').onclick = async () => {
  $('transferMsg').textContent = '正在生成预览…';
  $('transferMsg').className = 'msg';
  try {
    const { plan } = await api('/api/transfer/plan', {
      method: 'POST',
      body: JSON.stringify(transferPayload()),
    });
    renderPlan(plan);
    previewTemplate();
    if (!plan.links) {
      $('transferMsg').className = 'msg err';
      $('transferMsg').textContent = '没有可转存的链接，请检查站点登录状态。';
    } else {
      $('transferMsg').textContent = '';
    }
  } catch (e) {
    $('transferMsg').className = 'msg err';
    $('transferMsg').textContent = e.message;
  }
};

$('btnDoTransfer').onclick = async () => {
  const btn = $('btnDoTransfer');
  btn.disabled = true;
  $('transferMsg').className = 'msg';
  $('transferMsg').textContent = '正在提交…';
  try {
    const data = await api('/api/transfer/execute', {
      method: 'POST',
      body: JSON.stringify(transferPayload()),
    });
    renderPlan(data.plan);
    $('transferMsg').textContent = '任务已提交，正在执行…';
    pollJob(data.job_id);
  } catch (e) {
    $('transferMsg').className = 'msg err';
    $('transferMsg').textContent = e.message;
  } finally {
    btn.disabled = false;
  }
};

function pollJob(jobId) {
  $('jobBox').hidden = false;
  const tick = async () => {
    try {
      const j = await api('/api/transfer/jobs/' + jobId);
      const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
      $('jobBox').innerHTML = `
        <div class="muted">${esc(j.status)} · ${j.done}/${j.total} · 目标 cid ${esc(j.target_cid)}</div>
        <div class="progress"><i style="width:${pct}%"></i></div>
        ${j.results.map((r) => `<div class="job-item ${r.ok ? 'ok' : 'err'}">${r.ok ? '✓' : '✗'} ${esc(r.title)} — ${esc(r.detail)}</div>`).join('')}`;
      if (j.status === 'running' || j.status === 'pending') setTimeout(tick, 1200);
      else {
        $('transferMsg').className = j.errors.length ? 'msg err' : 'msg ok';
        $('transferMsg').textContent = j.message;
        state.selected.clear(); updateSelBar(); renderList();
      }
    } catch (e) {
      $('jobBox').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    }
  };
  tick();
}

/* ---------------- 目录操作 ---------------- */
$('btnNewDir').onclick = () => { $('newDirName').focus(); };
$('btnDoNewDir').onclick = async () => {
  const name = $('newDirName').value.trim();
  if (!name) { toast('请输入文件夹名称'); return; }
  try {
    const r = await api('/api/p115/mkdir', {
      method: 'POST',
      body: JSON.stringify({ name, pid: state.cid }),
    });
    $('newDirName').value = '';
    toast('已创建：' + (r.name || name));
    await loadDirs(r.cid || state.cid);
  } catch (e) { toast(e.message); }
};

/* ---------------- 登录 ---------------- */
$('btnLogin').onclick = async () => {
  $('loginModal').hidden = false;
  $('siteMsg').textContent = '';
  $('p115Msg').textContent = '';
};

$('btnSiteLogin').onclick = async () => {
  const username = $('siteUser').value.trim();
  const password = $('sitePwd').value;
  if (!username || !password) { $('siteMsg').className = 'msg err'; $('siteMsg').textContent = '请填写账号和密码'; return; }
  $('siteMsg').className = 'msg';
  $('siteMsg').textContent = '登录中…';
  try {
    const r = await api('/api/site/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    $('siteMsg').className = r.ok ? 'msg ok' : 'msg err';
    $('siteMsg').textContent = r.message || (r.ok ? '登录成功' : '登录失败');
    refreshStatus();
    if (r.ok) { $('sitePwd').value = ''; }
  } catch (e) {
    $('siteMsg').className = 'msg err';
    $('siteMsg').textContent = e.message;
  }
};

$('btnSiteCookie').onclick = async () => {
  const cookie = $('siteCookie').value.trim();
  if (!cookie) { toast('请粘贴 Cookie'); return; }
  try {
    const r = await api('/api/site/cookie', { method: 'POST', body: JSON.stringify({ cookie }) });
    $('siteMsg').className = r.ok ? 'msg ok' : 'msg err';
    $('siteMsg').textContent = r.ok ? 'Cookie 有效，已登录站点' : 'Cookie 无效';
    refreshStatus();
  } catch (e) {
    $('siteMsg').className = 'msg err';
    $('siteMsg').textContent = e.message;
  }
};

let qrTimer = null;
$('btnQr').onclick = async () => {
  clearInterval(qrTimer);
  $('qrWrap').hidden = false;
  $('qrStatus').textContent = '正在获取二维码…';
  try {
    const q = await api('/api/p115/qrcode/start', { method: 'POST', body: '{}' });
    $('qrImg').src = q.image;
    $('qrStatus').textContent = '请用 115 App 扫码';
    qrTimer = setInterval(async () => {
      try {
        const r = await api('/api/p115/qrcode/poll', {
          method: 'POST',
          body: JSON.stringify({ uid: q.uid, time: q.time, sign: q.sign }),
        });
        $('qrStatus').textContent = r.text || '等待扫码…';
        if (r.logged_in) {
          clearInterval(qrTimer);
          $('qrWrap').hidden = true;
          $('p115Msg').className = 'msg ok';
          $('p115Msg').textContent = '115 登录成功：' + (r.user_name || '');
          refreshStatus();
        } else if (r.status === -1 || r.status === -2) {
          clearInterval(qrTimer);
        }
      } catch (e) {
        clearInterval(qrTimer);
        $('qrStatus').textContent = '轮询失败：' + e.message;
      }
    }, 2000);
  } catch (e) {
    $('qrStatus').textContent = '获取二维码失败：' + e.message;
  }
};

$('btn115Cookie').onclick = async () => {
  const cookie = $('p115Cookie').value.trim();
  if (!cookie) { toast('请粘贴 115 Cookie'); return; }
  try {
    const r = await api('/api/p115/cookie', { method: 'POST', body: JSON.stringify({ cookie }) });
    $('p115Msg').className = r.ok ? 'msg ok' : 'msg err';
    $('p115Msg').textContent = r.ok ? '115 登录成功：' + (r.user_name || '') : 'Cookie 无效或已失效';
    refreshStatus();
  } catch (e) {
    $('p115Msg').className = 'msg err';
    $('p115Msg').textContent = e.message;
  }
};

$('btn115Logout').onclick = async () => {
  await api('/api/p115/logout', { method: 'POST', body: '{}' });
  toast('已退出 115');
  refreshStatus();
};

/* ---------------- 记录 ---------------- */
$('btnHistory').onclick = async () => {
  $('historyModal').hidden = false;
  $('historyBody').innerHTML = '<div class="muted">加载中…</div>';
  try {
    const { history } = await api('/api/history');
    $('historyBody').innerHTML = history.length ? history.map((h) => `
      <div class="h-row">
        <div class="h-time">${new Date(h.at * 1000).toLocaleString()}</div>
        <div style="flex:1">
          <div class="${h.ok ? 'h-ok' : 'h-err'}">${h.ok ? '✓' : '✗'} ${esc(h.action)} · ${esc(h.payload.title || '')}</div>
          <div class="muted">${esc(h.detail || '')} ${h.target_cid ? '· cid ' + esc(h.target_cid) : ''}</div>
        </div>
      </div>`).join('') : '<div class="muted">暂无记录</div>';
  } catch (e) {
    $('historyBody').innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
};

/* ---------------- 目录模板 / 类型规则 ---------------- */
let tplTimer = null;
$('tplInput').oninput = () => {
  clearTimeout(tplTimer);
  tplTimer = setTimeout(previewTemplate, 350);   // 防抖，避免频繁请求
};
$('btnResetTpl').onclick = () => {
  $('tplInput').value = DEFAULT_TEMPLATE;
  previewTemplate();
};
$('btnEditGenres').onclick = () => openGenres();
$('btnGenreAdd').onclick = () => {
  state.genreRules = collectGenres();
  state.genreRules['新类型'] = [];
  renderGenres();
};
$('btnGenreReset').onclick = async () => {
  try {
    const d = await api('/api/genres/reset', { method: 'POST', body: '{}' });
    state.genreRules = d.rules;
    renderGenres();
    $('genreMsg').className = 'msg ok';
    $('genreMsg').textContent = '已恢复默认规则';
  } catch (e) {
    $('genreMsg').className = 'msg err';
    $('genreMsg').textContent = e.message;
  }
};
$('btnGenreSave').onclick = async () => {
  try {
    const d = await api('/api/genres', {
      method: 'POST',
      body: JSON.stringify({ rules: collectGenres() }),
    });
    state.genreRules = d.rules;
    renderGenres();
    $('genreMsg').className = 'msg ok';
    $('genreMsg').textContent = '已保存，立即生效';
    previewTemplate();
  } catch (e) {
    $('genreMsg').className = 'msg err';
    $('genreMsg').textContent = e.message;
  }
};

/* ---------------- 事件绑定 ---------------- */
$('searchInput').onkeydown = (e) => {
  if (e.key !== 'Enter') return;
  state.keyword = $('searchInput').value.trim();
  state.page = 1; state.pages = 1;
  load();
};
$('sortSelect').onchange = () => { state.sort = $('sortSelect').value; load(); };
$('svOrder').onchange = () => { state.serverOrder = $('svOrder').value; state.page = 1; load(); };
$('orderSelect').onchange = () => { state.orderDir = $('orderSelect').value; load(); };
$('minScore').onchange = () => {
  const v = $('minScore').value;
  state.minScore = v === '' ? null : Number(v);
  renderList();
};
$('btnMore').onclick = () => { state.pages = Math.min(state.pages + 1, 5); state.page = 1; load(); };
$('btnClearSel').onclick = () => { state.selected.clear(); updateSelBar(); renderList(); };
$('btnTransferTop').onclick = () => openTransfer();

document.querySelectorAll('[data-close]').forEach((el) => {
  el.onclick = (e) => { e.target.closest('.modal').hidden = true; };
});
document.querySelectorAll('.modal').forEach((m) => {
  m.onclick = (e) => { if (e.target === m) m.hidden = true; };
});
document.onkeydown = (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.modal').forEach((m) => (m.hidden = true));
};

/* ---------------- 启动 ---------------- */
(async function boot() {
  await refreshStatus();
  await loadLayout();
  await loadOptions();
  await loadTabs();
  await load();
  updateSelBar();
})();