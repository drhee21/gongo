const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));

function toast(msg, type=''){
  let stack = $('#toastStack');
  if(!stack){
    stack = document.createElement('div');
    stack.id = 'toastStack';
    stack.className = 'toast-stack';
    document.body.appendChild(stack);
  }
  const el = document.createElement('div');
  el.className = `toast${type ? ' '+type : ''}`;
  el.innerHTML = `<span class="toast-msg">${esc(msg)}</span><button type="button" class="toast-close" aria-label="닫기">✕</button>`;
  // (aria-label는 스크린리더용이라 시각적 검증 범위 밖 — 번역 대상에서 제외)
  const remove = () => {
    el.classList.add('leaving');
    setTimeout(()=>el.remove(), 180);
  };
  el.querySelector('.toast-close').addEventListener('click', remove);
  stack.appendChild(el);
  setTimeout(remove, 4000);
}

let notices = [];
let sources = [];
let rawSources = [];
let company = {};
let currentUserState = null;

const PAGE_SIZE = 50;
let currentPage = 1;

function api(path, opts={}){
  return fetch(path, {headers:{'Content-Type':'application/json'}, credentials:'same-origin', ...opts}).then(async r=>{
    const data = await r.json().catch(()=>({ok:false,error:t('jsonParseError')}));
    if(!r.ok || data.ok === false) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  });
}

function canonicalSource(id){
  return ({biohub_direct:'biohub', khidi_direct:'khidi'}[id] || id || 'unknown');
}
const SOURCE_NAME_KEYS = {
  bizinfo:'srcBizinfo', kstartup:'srcKstartup', biohub:'srcBiohub', khidi:'srcKhidi',
  kddf:'srcKddf', nrf:'srcNrf', g2b:'srcG2b', sample:'srcSample',
};
function sourceName(id){
  const cid = canonicalSource(id);
  // 8개 고정 기관은 서버가 뭐라고 이름을 내려주든(한국어 고정값) 항상 번역해서
  // 보여준다 — 서버 값이 항상 존재해서 예전 fallback 방식으로는 절대 안 쓰였다.
  // 관리자가 URL로 직접 등록한 커스텀 소스는 이 표에 없으므로 그대로 서버 이름을 쓴다.
  if(SOURCE_NAME_KEYS[cid]) return t(SOURCE_NAME_KEYS[cid]);
  const s = sources.find(x=>canonicalSource(x.id)===cid);
  return s?.name || cid;
}
function statusClass(st){
  if(st==='접수중' || st==='상시') return 'open';
  if(st==='마감') return 'closed';
  if(st==='날짜 미상') return '';
  return 'urgent';
}
function ddayText(a){
  if(a.status==='상시' || a.status==='날짜 미상') return a.status;
  if(a.dday == null) return a.status || '확인';
  if(a.dday < 0) return '마감';
  if(a.dday === 0) return 'D-DAY';
  return `D-${a.dday}`;
}
function eligText(e){
  if(!e) return t('eligNoLimit');
  const p=[];
  if(e.minYears != null && e.maxYears != null) p.push(t('eligYearsRange', {min:e.minYears, max:e.maxYears}));
  else if(e.maxYears != null) p.push(t('eligYearsMax', {max:e.maxYears}));
  else if(e.minYears != null) p.push(t('eligYearsMin', {min:e.minYears}));
  if(e.regions?.length) p.push(t('eligRegion', {regions:e.regions.join('·')}));
  if(e.sectors?.length) p.push(e.sectors.join('·'));
  return p.join(' / ') || t('eligNoLimit');
}
function noticeSources(a){
  const list = (a.sources && a.sources.length) ? a.sources : [{id:a.src, url:a.url}];
  const seen = new Set();
  const out = [];
  list.forEach(s=>{
    const cid = canonicalSource(s.id);
    if(seen.has(cid)) return;
    seen.add(cid);
    out.push({id:cid, url:s.url || a.url});
  });
  return out;
}
function aiFitBadge(a){
  if(!a.ai_fit) return '';
  const label = {fit:t('aiFitFit'), unfit:t('aiFitUnfit'), unsure:t('aiFitUnsure')}[a.ai_fit] || t('aiFitUnsure');
  const cls = {fit:'open', unfit:'closed', unsure:''}[a.ai_fit] || '';
  const hasReason = !!a.ai_reason;
  const trig = hasReason ? ' ai-trigger' : '';
  const caret = hasReason ? '<span class="ai-caret">▾</span>' : '';
  return `<span class="badge ai ${cls}${trig}"${hasReason?` data-ai-toggle="${esc(a.id)}"`:''}>${esc(label)}${caret}</span>`;
}

// 판정 근거를 배지 옆 접이식 줄로 보여준다. 기본은 접힌 상태이고, 배지를 클릭하면 펼쳐진다.
function aiReasonRow(a){
  if(!a.ai_fit || !a.ai_reason) return '';
  return `<div class="ai-reason ${a.ai_fit}" data-ai-reason="${esc(a.id)}">
    <span class="ai-reason-tag">${esc(t('aiReasonTag'))}</span>
    <span class="ai-reason-text">${esc(a.ai_reason)}</span>
  </div>`;
}

function noticeHTML(a){
  const srcs = noticeSources(a);
  const srcBadges = srcs.map(s=>`<span class="src">${esc(sourceName(s.id))}</span>`).join('');
  const srcLinks = srcs.map(s=>`<a href="${esc(s.url || a.url)}" target="_blank" rel="noopener">${esc(sourceName(s.id))} ↗</a>`).join(' · ');
  return `<article class="notice" data-id="${esc(a.id)}">
    <div class="src-group">${srcBadges}</div>
    <div>
      <h3>${esc(a.title)}</h3>
      <div class="meta">${esc(a.org)} · ${esc(a.category)} · ${a.dates_unknown ? esc(t('statusUnknownDate')) : `${a.start ? esc(a.start) + ' ' : ''}~ ${a.end ? esc(a.end) : (a.status === '상시' ? esc(t('statusRolling')) : '-')}`}</div>
      <div class="badges">
        <span class="badge ${statusClass(a.status)}">${esc(tStatus(a.status))}${a.status_inferred ? '?' : ''}</span>
        ${ddayText(a) !== a.status ? `<span class="badge ${a.dday != null && a.dday <= 7 && a.dday >= 0 ? 'urgent' : ''}">${esc(tStatus(ddayText(a)))}</span>` : ''}
        ${aiFitBadge(a)}
      </div>
      ${aiReasonRow(a)}
    </div>
    <div class="right">
      <button class="star ${a.favorite?'on':''}" data-star="${esc(a.id)}">${a.favorite?'★':'☆'}</button>
      <button class="detail-toggle" data-detail-toggle="${esc(a.id)}" aria-expanded="false">${esc(t('detailToggle'))} <span class="detail-caret">▾</span></button>
    </div>
    <div class="detail">
      <div class="meta"><b>${esc(t('budgetLabel'))}</b> ${esc(a.budget || t('budgetDefault'))}</div>
      <div class="meta"><b>${esc(t('eligLabel'))}</b> ${esc(eligText(a.elig))}</div>
      <div class="meta"><b>${esc(t('sourceLinkLabel'))}</b> ${srcLinks}</div>
    </div>
  </article>`;
}

function filtered(list){
  const q = $('#q')?.value.trim().toLowerCase() || '';
  const src = $('#sourceFilter')?.value || '';
  const st = $('#statusFilter')?.value || 'open';
  const aiFit = $('#aiFitFilter')?.value || '';
  return list.filter(a=>{
    const hay = `${a.title} ${a.org} ${a.category}`.toLowerCase();
    if(q && !hay.includes(q)) return false;
    if(src && !noticeSources(a).some(s=>s.id===src)) return false;
    if(st==='open' && a.status==='마감') return false;
    if(st==='urgent' && !(a.dday != null && a.dday >= 0 && a.dday <= 7)) return false;
    if(st==='closed' && a.status !== '마감') return false;
    if(aiFit && a.ai_fit !== aiFit) return false;
    return true;
  });
}

function renderPagination(total){
  const box = $('#noticePagination');
  if(!box) return;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if(currentPage > pageCount) currentPage = pageCount;
  if(currentPage < 1) currentPage = 1;

  if(total === 0){
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');

  const from = (currentPage - 1) * PAGE_SIZE + 1;
  const to = Math.min(currentPage * PAGE_SIZE, total);
  $('#pageStatus').textContent = t('paginationShowing', {from: from.toLocaleString(), to: to.toLocaleString(), total: total.toLocaleString()});
  $('#pagePrev').disabled = currentPage <= 1;
  $('#pageNext').disabled = currentPage >= pageCount;
}

function renderList(){
  const list = filtered(notices);
  renderPagination(list.length);
  const pageStart = (currentPage - 1) * PAGE_SIZE;
  const pageItems = list.slice(pageStart, pageStart + PAGE_SIZE);
  $('#noticeList').innerHTML = pageItems.length ? pageItems.map(noticeHTML).join('') : `<div class="empty">${esc(t('emptyNoticesFiltered'))}</div>`;
  bindNoticeEvents($('#noticeList'));

  const favs = notices.filter(x=>x.favorite);
  const favEmptyMsg = currentUserState ? t('emptyFavLoggedIn') : t('emptyFavLoggedOut');
  $('#favList').innerHTML = favs.length ? favs.map(noticeHTML).join('') : `<div class="empty">${esc(favEmptyMsg)}</div>`;
  bindNoticeEvents($('#favList'));

  $('#kpiTotal').textContent = notices.length;
  $('#kpiOpen').textContent = notices.filter(x=>x.status!=='마감').length;
  $('#kpiUrgent').textContent = notices.filter(x=>x.dday != null && x.dday >= 0 && x.dday <= 7).length;
  $('#kpiFav').textContent = favs.length;
  renderAiFitMini();
  renderCalendar();
}

function onFilterChange(){
  currentPage = 1;
  renderList();
}

function renderAiFitMini(){
  const box = $('#aiFitMiniBox');
  const el = $('#aiFitMini');
  if(!box || !el) return;
  const judged = notices.filter(x=>x.ai_fit);
  if(!judged.length){ box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  const counts = {fit:0, unfit:0, unsure:0};
  judged.forEach(x=>{ counts[x.ai_fit] = (counts[x.ai_fit]||0) + 1; });
  const rows = [
    [t('aiFitFit'), counts.fit, 'state-ok'],
    [t('aiFitUnsure'), counts.unsure, 'state-wait'],
    [t('aiFitUnfit'), counts.unfit, 'state-bad'],
  ];
  el.innerHTML = rows.map(([label, n, cls])=>`<div class="source-mini"><span>${label}</span><span class="${cls}">${n}</span></div>`).join('');
}

function bindNoticeEvents(root){
  root.querySelectorAll('.star').forEach(btn=>btn.addEventListener('click', async e=>{
    e.stopPropagation();
    const id = btn.dataset.star;
    try{
      const res = await api('/api/favorite/toggle', {method:'POST', body:JSON.stringify({notice_id:id})});
      notices = notices.map(a => a.id===id ? {...a, favorite:res.favorite} : a);
      renderList();
    }catch(err){ toast(err.message, 'error'); }
  }));
  root.querySelectorAll('[data-ai-toggle]').forEach(badge=>badge.addEventListener('click', e=>{
    e.stopPropagation();
    const row = root.querySelector(`[data-ai-reason="${CSS.escape(badge.dataset.aiToggle)}"]`);
    if(!row) return;
    const open = row.classList.toggle('show');
    badge.classList.toggle('ai-expanded', open);
    badge.setAttribute('aria-expanded', String(open));
  }));
  root.querySelectorAll('[data-detail-toggle]').forEach(btn=>btn.addEventListener('click', e=>{
    e.stopPropagation();
    const row = root.querySelector(`.notice[data-id="${CSS.escape(btn.dataset.detailToggle)}"]`);
    if(!row) return;
    const open = row.classList.toggle('open-detail');
    btn.setAttribute('aria-expanded', String(open));
  }));
}

let calViewDate = null;
let calSelectedDate = null;

function renderCalendar(){
  const today = new Date();
  if(!calViewDate) calViewDate = {y: today.getFullYear(), m: today.getMonth()+1};
  const {y, m} = calViewDate;
  const monthLabel = $('#calMonthLabel');
  if(monthLabel){
    const MONTH_NAMES_EN = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    monthLabel.textContent = currentLang === 'en' ? `${MONTH_NAMES_EN[m-1]} ${y}` : `${y}년 ${m}월`;
  }

  const groups = {};
  notices.filter(a=>a.end).forEach(a=>{
    (groups[a.end] ||= []).push(a);
  });

  const first = new Date(y, m-1, 1);
  const startWeekday = first.getDay();
  const daysInMonth = new Date(y, m, 0).getDate();
  const pad2 = n => String(n).padStart(2, '0');
  const todayStr = `${today.getFullYear()}-${pad2(today.getMonth()+1)}-${pad2(today.getDate())}`;

  const cells = [];
  for(let i=0;i<startWeekday;i++) cells.push('<div class="cal-cell empty"></div>');
  for(let d=1; d<=daysInMonth; d++){
    const dateStr = `${y}-${pad2(m)}-${pad2(d)}`;
    const dayNotices = groups[dateStr] || [];
    cells.push(`<div class="cal-cell ${dateStr===todayStr?'today':''} ${dayNotices.length?'has-notice':''} ${dateStr===calSelectedDate?'selected':''}" data-date="${dateStr}">
      <div class="cal-daynum">${d}</div>
      ${dayNotices.length ? `<div class="cal-count">${esc(countText(dayNotices.length))}</div>` : ''}
    </div>`);
  }
  const grid = $('#calendarGrid');
  if(grid){
    const WEEKDAYS = currentLang === 'en' ? ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'] : ['일','월','화','수','목','금','토'];
    grid.innerHTML = `
      <div class="cal-weekdays">${WEEKDAYS.map(w=>`<div>${w}</div>`).join('')}</div>
      <div class="cal-cells">${cells.join('')}</div>
    `;
    grid.querySelectorAll('.cal-cell.has-notice').forEach(cell=>{
      cell.addEventListener('click', ()=>{
        calSelectedDate = calSelectedDate === cell.dataset.date ? null : cell.dataset.date;
        renderCalendar();
      });
    });
  }

  const monthPrefix = `${y}-${pad2(m)}-`;
  let days = Object.keys(groups).filter(d=>d.startsWith(monthPrefix)).sort();
  if(calSelectedDate && days.includes(calSelectedDate)) days = [calSelectedDate];

  $('#calendarList').innerHTML = days.length ? days.map(d=>{
    const entries = groups[d];
    const rep = entries[0];
    return `<div class="day" data-date="${esc(d)}"><h3>${esc(d)} <span class="badge ${statusClass(rep.status)}">${esc(tStatus(ddayText(rep)))}</span></h3>${entries.map(a=>`<div class="day-entry"><a href="${esc(a.url)}" target="_blank">${esc(a.title)}</a></div>`).join('')}</div>`;
  }).join('') : `<div class="empty">${esc(t('emptyCalendarMonth'))}</div>`;
}

function cleanSourceRows(rawSources){
  const order = ['bizinfo','kstartup','biohub','khidi','kddf','nrf','sample'];
  const groups = new Map();
  const noticeCounts = {};
  notices.forEach(a=>{
    noticeSources(a).forEach(s=>{
      noticeCounts[s.id] = (noticeCounts[s.id] || 0) + 1;
    });
  });
  rawSources.forEach(s=>{
    const id = canonicalSource(s.id);
    if(!groups.has(id)){
      groups.set(id, {...s, id, count:0});
    }
    const g = groups.get(id);
    g.name = sourceName(id);
    if(s.last_collected_at && (!g.last_collected_at || s.last_collected_at > g.last_collected_at)) g.last_collected_at = s.last_collected_at;
    if(!g.error && s.error) g.error = s.error;
    if(['대기','비활성화','0건','미확인'].includes(g.state) && s.state) g.state = s.state;
  });
  Object.entries(noticeCounts).forEach(([id,count])=>{
    if(!groups.has(id)) groups.set(id, {id, name:sourceName(id), state:'정상', count});
    const g = groups.get(id);
    g.count = count;
    if(count > 0){
      g.state = '정상';
      g.error = '';
    }
  });
  const realTotal = [...groups.entries()].filter(([id])=>id!=='sample').reduce((acc,[,g])=>acc+(Number(g.count)||0),0);
  return [...groups.values()]
    .filter(s=>!(s.id==='sample' && realTotal>0))
    .filter(s=>{
      const count = Number(s.count)||0;
      const state = s.state || '';
      if(count === 0 && ['대기','비활성화','0건','오류','차단(robots)','미확인'].includes(state)) return false;
      return true;
    })
    .sort((a,b)=>(order.indexOf(a.id)<0?999:order.indexOf(a.id))-(order.indexOf(b.id)<0?999:order.indexOf(b.id)));
}

function renderSources(){
  const displaySources = cleanSourceRows(sources);
  const mini = displaySources.map(s=>`<div class="source-mini"><span>${esc(s.name || s.id)}</span><span class="state-ok">${esc(t('statusNormal'))} ${Number(s.count)||0}</span></div>`).join('');
  $('#sourceMini').innerHTML = mini || `<div class="source-mini">${esc(t('emptyBeforeCollect'))}</div>`;
  const adminRows = rawSources.length ? rawSources : displaySources;
  $('#sourcesTable').innerHTML = `<table class="table"><thead><tr><th>${esc(t('colId'))}</th><th>${esc(t('colSource'))}</th><th>${esc(t('colState'))}</th><th>${esc(t('colCount'))}</th><th>${esc(t('colError'))}</th></tr></thead><tbody>${adminRows.map(s=>`<tr><td>${esc(s.id)}</td><td>${esc(SOURCE_NAME_KEYS[canonicalSource(s.id)] ? t(SOURCE_NAME_KEYS[canonicalSource(s.id)]) : (s.name||s.id))}</td><td>${esc(tStatus(s.state||'미확인'))}${s.anomaly?`<span class="badge-warn" title="${esc(s.anomaly_note||'')}">${esc(t('anomalyBadgeText'))}</span>`:''}</td><td>${esc(Number(s.count)||0)}</td><td>${esc(s.error||s.anomaly_note||'')}</td></tr>`).join('')}</tbody></table>`;
  const sel = $('#sourceFilter');
  const cur = canonicalSource(sel.value);
  const ids = [...new Set(notices.flatMap(a=>noticeSources(a).map(s=>s.id)))];
  sel.innerHTML = `<option value="">${esc(t('optAllSources'))}</option>` + ids.map(id=>`<option value="${esc(id)}">${esc(sourceName(id))}</option>`).join('');
  sel.value = ids.includes(cur) ? cur : '';
}

async function loadAll(){
  currentPage = 1;
  const [n, s, meRes] = await Promise.all([api('/api/notices'), api('/api/sources'), api('/api/auth/me')]);
  notices = n.items || [];
  sources = s.items || [];
  rawSources = s.raw || [];
  currentUserState = meRes.user || null;
  company = {};
  if(currentUserState){
    try{
      const c = await api('/api/company');
      company = c.company || {};
    }catch(err){ company = {}; }
  }
  fillCompany();
  renderAuthUI();
  renderSources();
  renderList();
  if(currentUserState && !currentUserState.onboarding_done) startOnboardingTour();
}

function renderAuthUI(){
  const loggedOut = $('#accountLoggedOut');
  const loggedIn = $('#accountLoggedIn');
  const companyLoginRequired = $('#companyLoginRequired');
  const companyLoggedInArea = $('#companyLoggedInArea');
  if(currentUserState){
    loggedOut?.classList.add('hidden');
    loggedIn?.classList.remove('hidden');
    if($('#accountEmail')) $('#accountEmail').textContent = currentUserState.email;
    companyLoginRequired?.classList.add('hidden');
    companyLoggedInArea?.classList.remove('hidden');
    loadCompanyDocs();
  } else {
    loggedOut?.classList.remove('hidden');
    loggedIn?.classList.add('hidden');
    companyLoginRequired?.classList.remove('hidden');
    companyLoggedInArea?.classList.add('hidden');
  }
  updateApiKeyStatus();
  fillLlmPreference();
  const sourcesPanel = $('#adminSourcesPanel');
  const schedulerPanel = $('#adminSchedulerPanel');
  if(currentUserState?.is_admin){
    sourcesPanel?.classList.remove('hidden');
    loadAllSources();
    schedulerPanel?.classList.remove('hidden');
    loadSchedulerConfig();
  } else {
    sourcesPanel?.classList.add('hidden');
    schedulerPanel?.classList.add('hidden');
  }
}

// ── 자동 수집 일정(관리자) ──
function updateSchedulerModeFields(){
  const form = $('#schedulerForm');
  if(!form) return;
  const mode = form.querySelector('input[name="mode"]:checked')?.value;
  $('#schedulerDailyFields')?.classList.toggle('hidden', mode !== 'daily');
  $('#schedulerIntervalFields')?.classList.toggle('hidden', mode !== 'interval');
}

async function loadSchedulerConfig(){
  const form = $('#schedulerForm');
  const statusEl = $('#schedulerStatus');
  if(!form) return;
  try{
    const res = await api('/api/admin/scheduler-config');
    const cfg = res.config || {};
    form.querySelector('#schedulerEnabled').checked = !!cfg.enabled;
    form.querySelector(`input[name="mode"][value="${cfg.mode === 'interval' ? 'interval' : 'daily'}"]`).checked = true;
    form.querySelector('#schedulerTime').value = cfg.time || '03:00';
    form.querySelectorAll('input[name="days"]').forEach(cb => {
      cb.checked = (cfg.days || []).includes(cb.value);
    });
    form.querySelector('#schedulerIntervalHours').value = cfg.interval_hours || 6;
    updateSchedulerModeFields();
    if(statusEl){
      const parts = [];
      parts.push(cfg.enabled ? t('schedulerInUse') : t('schedulerNotInUse'));
      if(res.last_run) parts.push(t('schedulerLastRun', {v:res.last_run}));
      if(res.next_run_estimate) parts.push(t('schedulerNextRun', {v:res.next_run_estimate}));
      statusEl.textContent = parts.join(' · ');
    }
  }catch(err){ if(statusEl) statusEl.textContent = err.message; }
}

$('#schedulerForm')?.addEventListener('change', (e)=>{
  if(e.target.name === 'mode') updateSchedulerModeFields();
});

$('#schedulerForm')?.addEventListener('submit', async (e)=>{
  e.preventDefault();
  const form = e.currentTarget;
  const fd = new FormData(form);
  const body = {
    enabled: form.querySelector('#schedulerEnabled').checked,
    mode: fd.get('mode'),
    time: fd.get('time'),
    days: fd.getAll('days'),
    interval_hours: Number(fd.get('interval_hours')),
  };
  try{
    await api('/api/admin/scheduler-config', {method:'POST', body:JSON.stringify(body)});
    toast(t('toastSchedulerSaved'), 'success');
    loadSchedulerConfig();
  }catch(err){ toast(err.message, 'error'); }
});

// ── 소스 관리(관리자): 기존 하드코딩 소스 + 커스텀 소스를 한 목록에 같이 보여준다 ──
// 발견된 레시피는 확정 전까지 서버에 저장하지 않는다 — 확정 버튼을 누를 때 이 값을
// 그대로 다시 서버에 보낸다(재발견 없이). 취소하면 그냥 이 값을 비우고 끝난다.
let pendingCustomSource = null;
// 소스의 이름/URL을 고치는 중이면 그 소스의 id/원래 값/종류를 들고 있는다.
// kind가 'custom'(레시피 기반 커스텀 소스)이면: URL을 안 바꿨으면(이름만 바꿈)
// 재발견 없이 이름만 바로 바꾸고, URL을 바꿨으면 새로 등록할 때와 같은 미리보기
// 절차를 거치되 새 소스가 아니라 이 소스를 갱신한다.
// kind가 'override'(K-스타트업/NRF 등 기존 하드코딩 소스)이면: 이 소스들은 아직
// AI 레시피가 아니라 전용 수집기를 쓰므로, 이름/URL 중 뭘 바꿔도 재발견 없이
// 항상 바로 저장한다(전용 수집기가 새 URL에서도 같은 구조를 기대할 뿐).
let editingCustomSource = null; // {id, originalUrl, originalName, kind}

async function loadAllSources(){
  const el = $('#allSourcesTable');
  if(!el) return;
  try{
    const [overridesRes, customRes] = await Promise.all([
      api('/api/admin/source-overrides'),
      api('/api/admin/custom-sources'),
    ]);
    const hardcoded = (overridesRes.items || []).map(s => {
      // 관리자가 이름을 직접 바꾼 게 아니면(기본값 그대로면) 고정된 기관명이므로
      // 화면 언어에 맞게 번역해서 보여준다 — 직접 바꾼 이름은 그대로 존중한다.
      const displayName = (!s.name || s.name === s.default_name) && SOURCE_NAME_KEYS[canonicalSource(s.source_id)]
        ? t(SOURCE_NAME_KEYS[canonicalSource(s.source_id)])
        : s.name;
      // bizinfo/g2b는 목록 URL이 아니라 API 키로 수집하므로 URL 재정의가 의미
      // 없다 — default_url이 없는 소스는 URL 줄과 "수정" 버튼을 아예 생략한다.
      const hasUrl = !!s.default_url;
      return `
      <div class="custom-source-row" data-source="${esc(s.source_id)}" data-kind="override">
        <div class="custom-source-info">
          <div><strong>${esc(displayName)}</strong> <span class="badge-warn" style="background:rgba(0,0,0,.06);color:var(--muted)">${esc(t('badgeExistingSource'))}</span>${s.enabled ? '' : `<span class="badge-warn">${esc(t('disabledBadge'))}</span>`}</div>
          ${hasUrl ? `<div class="custom-source-url">${esc(s.override_url || s.default_url || t('noneValue'))}${s.override_url ? ` <span class="meta">${esc(t('overrideNotice'))}</span>` : ''}</div>` : ''}
        </div>
        <div class="custom-source-actions">
          ${hasUrl ? `<button type="button" class="button override-source-edit">${esc(t('btnEdit'))}</button>` : ''}
          <button type="button" class="button override-source-toggle">${s.enabled ? esc(t('btnDisable')) : esc(t('btnEnable'))}</button>
        </div>
      </div>
    `;
    }).join('');
    const custom = (customRes.items || []).map(s => `
      <div class="custom-source-row" data-source="${esc(s.id)}" data-kind="custom">
        <div class="custom-source-info">
          <div><strong>${esc(s.name)}</strong> ${s.enabled ? '' : `<span class="badge-warn">${esc(t('disabledBadge'))}</span>`}${s.anomaly ? `<span class="badge-warn" title="${esc(s.anomaly_note||'')}">${esc(t('anomalyBadgeText'))}</span>` : ''}${s.uses_detail_fetch ? `<span class="badge-warn" title="${esc(t('detailFetchBadgeTitle'))}">${esc(t('detailFetchBadgeText'))}</span>` : ''}</div>
          <div class="custom-source-url">${esc(s.list_url)}</div>
          <div class="meta">${esc(tStatus(s.state || '미확인'))} · ${esc(countText(Number(s.count)||0))} ${s.last_collected_at ? '· ' + esc(s.last_collected_at) : ''}</div>
        </div>
        <div class="custom-source-actions">
          <button type="button" class="button custom-source-edit">${esc(t('btnEdit'))}</button>
          <button type="button" class="button custom-source-toggle">${s.enabled ? esc(t('btnDisable')) : esc(t('btnEnable'))}</button>
          <button type="button" class="button custom-source-remove">${esc(t('btnDelete'))}</button>
        </div>
      </div>
    `).join('');
    el.innerHTML = hardcoded + custom || `<p class="meta">${esc(t('emptySources'))}</p>`;

    el.querySelectorAll('.override-source-edit').forEach(btn=>{
      btn.addEventListener('click', ()=>{
        const row = btn.closest('.custom-source-row');
        const source_id = row.dataset.source;
        const item = (overridesRes.items || []).find(s => s.source_id === source_id);
        if(!item) return;
        startEditingSource({id: source_id, originalUrl: item.override_url || item.default_url || '', originalName: item.name, kind: 'override'});
      });
    });
    el.querySelectorAll('.override-source-toggle').forEach(btn=>{
      btn.addEventListener('click', async ()=>{
        const row = btn.closest('.custom-source-row');
        const source_id = row.dataset.source;
        const item = (overridesRes.items || []).find(s => s.source_id === source_id);
        const enabling = !(item && item.enabled);
        try{
          await api('/api/admin/source-overrides/toggle', {method:'POST', body:JSON.stringify({source_id, enabled: enabling})});
          toast(enabling ? t('toastEnabled') : t('toastDisabled'), 'success');
          loadAllSources();
          await loadAll();
        }catch(err){ toast(err.message, 'error'); }
      });
    });
    el.querySelectorAll('.custom-source-edit').forEach(btn=>{
      btn.addEventListener('click', ()=>{
        const row = btn.closest('.custom-source-row');
        const source_id = row.dataset.source;
        const item = (customRes.items || []).find(s => s.id === source_id);
        if(!item) return;
        startEditingSource({id: source_id, originalUrl: item.list_url, originalName: item.name, kind: 'custom'});
      });
    });
    el.querySelectorAll('.custom-source-toggle').forEach(btn=>{
      btn.addEventListener('click', async ()=>{
        const row = btn.closest('.custom-source-row');
        const source_id = row.dataset.source;
        // 버튼 글자로 현재 상태를 판단하면 영어 화면에서는 "활성화"라는 글자가 아예
        // 없어서 항상 false가 되는 버그가 생긴다 — 데이터에서 실제 enabled 값을 본다.
        const item = (customRes.items || []).find(s => s.id === source_id);
        const enabling = !(item && item.enabled);
        try{
          await api('/api/admin/custom-sources/toggle', {method:'POST', body:JSON.stringify({source_id, enabled: enabling})});
          toast(enabling ? t('toastEnabled') : t('toastDisabled'), 'success');
          loadAllSources();
        }catch(err){ toast(err.message, 'error'); }
      });
    });
    el.querySelectorAll('.custom-source-remove').forEach(btn=>{
      btn.addEventListener('click', async ()=>{
        const row = btn.closest('.custom-source-row');
        const source_id = row.dataset.source;
        if(!confirm(t('confirmRemoveCustomSource'))) return;
        try{
          await api('/api/admin/custom-sources/remove', {method:'POST', body:JSON.stringify({source_id})});
          toast(t('toastDeleted'), 'success');
          if(editingCustomSource?.id === source_id) editingCustomSource = null;
          loadAllSources();
          await loadAll();
        }catch(err){ toast(err.message, 'error'); }
      });
    });
  }catch(err){ el.innerHTML = `<p class="meta">${esc(err.message)}</p>`; }
}

function clearCustomSourceForm(){
  editingCustomSource = null;
  $('#customSourceEditBanner')?.classList.add('hidden');
  $('#customSourceName').value = '';
  $('#customSourceUrl').value = '';
}

function startEditingSource({id, originalUrl, originalName, kind}){
  editingCustomSource = {id, originalUrl, originalName, kind};
  $('#customSourceName').value = originalName;
  $('#customSourceUrl').value = originalUrl;
  const banner = $('#customSourceEditBanner');
  if(banner){
    banner.classList.remove('hidden');
    const hint = kind === 'override' ? t('editHintOverride') : t('editHintCustom');
    banner.innerHTML = `${esc(t('editingBanner', {name: originalName, hint}))} <a href="#" id="btnCancelEditCustomSource">${esc(t('cancelLink'))}</a>`;
    $('#btnCancelEditCustomSource')?.addEventListener('click', (e)=>{
      e.preventDefault();
      clearCustomSourceForm();
    });
  }
  $('#customSourceName')?.scrollIntoView({behavior:'smooth', block:'center'});
}

function renderCustomSourcePreview(){
  const el = $('#customSourcePreview');
  if(!el) return;
  if(!pendingCustomSource){
    el.classList.add('hidden');
    el.innerHTML = '';
    return;
  }
  const p = pendingCustomSource;
  const warningsHtml = (p.warnings && p.warnings.length)
    ? `<ul class="warning-list">${p.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul>`
    : '';
  const rowsHtml = (p.sample_items || []).map(it => `
    <tr>
      <td>${esc(it.title||'')}</td>
      <td>${esc(it.org||'')}</td>
      <td>${esc(it.start||'')} ~ ${esc(it.end||'')}</td>
      <td>${esc(it.url||'')}</td>
    </tr>
  `).join('');
  el.className = 'custom-source-preview';
  el.innerHTML = `
    <p><strong>${esc(p.name)}</strong> ${esc(t('previewFoundCount', {n: p.item_count}))}</p>
    ${warningsHtml}
    <table>
      <thead><tr><th>${esc(t('previewColTitle'))}</th><th>${esc(t('previewColOrg'))}</th><th>${esc(t('previewColPeriod'))}</th><th>${esc(t('previewColUrl'))}</th></tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>
    <details><summary>${esc(t('recipeDetailsSummary'))}</summary><pre>${esc(JSON.stringify(p.recipe, null, 2))}</pre></details>
    <div class="preview-actions">
      <button type="button" class="primary" id="btnConfirmCustomSource">${p.editing ? esc(t('btnConfirmEdit')) : esc(t('btnConfirmNew'))}</button>
      <button type="button" class="button" id="btnCancelCustomSource">${esc(t('btnCancel'))}</button>
    </div>
  `;
  $('#btnConfirmCustomSource')?.addEventListener('click', async ()=>{
    const btn = $('#btnConfirmCustomSource');
    btn.disabled = true;
    try{
      await api('/api/admin/custom-sources/confirm', {method:'POST', body:JSON.stringify(pendingCustomSource)});
      toast(pendingCustomSource.editing ? t('toastEditConfirmed') : t('toastRegistered'), 'success');
      pendingCustomSource = null;
      clearCustomSourceForm();
      renderCustomSourcePreview();
      loadAllSources();
      await loadAll();
    }catch(err){
      toast(err.message, 'error');
      btn.disabled = false;
    }
  });
  $('#btnCancelCustomSource')?.addEventListener('click', ()=>{
    pendingCustomSource = null;
    renderCustomSourcePreview();
  });
}

$('#btnDiscoverCustomSource')?.addEventListener('click', async ()=>{
  const nameEl = $('#customSourceName');
  const urlEl = $('#customSourceUrl');
  const name = nameEl?.value.trim();
  const url = urlEl?.value.trim();
  if(!name || !url){ toast(t('toastEnterNameUrl'), 'error'); return; }

  // 기존 하드코딩 소스(K-스타트업/NRF 등)는 아직 전용 수집기를 쓰므로 이름/URL 중
  // 뭘 바꿔도 재발견 없이 항상 바로 저장한다.
  if(editingCustomSource?.kind === 'override'){
    try{
      await api('/api/admin/source-overrides', {method:'POST', body:JSON.stringify({source_id: editingCustomSource.id, list_url: url, name})});
      toast(t('toastEditSaved'), 'success');
      clearCustomSourceForm();
      loadAllSources();
      await loadAll();
    }catch(err){ toast(err.message, 'error'); }
    return;
  }

  // 커스텀 소스를 수정 중이고 URL을 안 바꿨으면(이름만 바꿈) 재발견 없이 바로 이름만
  // 바꾼다 — 레시피는 그대로 유효하므로 LLM 호출을 아낄 수 있다.
  if(editingCustomSource && url === editingCustomSource.originalUrl){
    try{
      await api('/api/admin/custom-sources/rename', {method:'POST', body:JSON.stringify({source_id: editingCustomSource.id, name})});
      toast(t('toastNameUpdated'), 'success');
      clearCustomSourceForm();
      loadAllSources();
      await loadAll();
    }catch(err){ toast(err.message, 'error'); }
    return;
  }

  const btn = $('#btnDiscoverCustomSource');
  btn.disabled = true;
  btn.dataset.i18nBusy = 'btnAnalyzingRecipe';
  btn.textContent = t('btnAnalyzingRecipe');
  try{
    const body = {name, url};
    if(editingCustomSource) body.existing_source_id = editingCustomSource.id;
    const res = await api('/api/admin/custom-sources/discover', {method:'POST', body:JSON.stringify(body)});
    pendingCustomSource = res;
    renderCustomSourcePreview();
    toast(t('toastPreviewReady'), 'success');
  }catch(err){
    toast(err.message, 'error');
  }finally{
    btn.disabled = false;
    delete btn.dataset.i18nBusy;
    btn.textContent = t('btnSave');
  }
});

function updateApiKeyStatus(){
  const bizEl = $('#bizinfoKeyStatus');
  if(bizEl) bizEl.textContent = currentUserState?.has_bizinfo_key ? t('bizinfoKeyRegistered') : t('bizinfoKeyNotRegistered');
  const el = $('#llmPreferenceStatus');
  if(el) el.textContent = currentUserState?.has_llm_key ? t('apiKeyRegistered') : t('apiKeyNotRegistered');
}

let llmModels = [];

async function loadLlmModels(){
  if(llmModels.length) return llmModels;
  try{
    const res = await api('/api/llm-models');
    llmModels = res.items || [];
    const sel = $('#llmModelSelect');
    if(sel) sel.innerHTML = llmModels.map(m => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
  }catch(err){ /* 목록을 못 가져와도 나머지 화면은 계속 쓸 수 있어야 한다 */ }
  return llmModels;
}

function llmModelLabel(modelId){
  const m = llmModels.find(x => x.id === modelId);
  return m ? m.label : modelId;
}

function renderLlmProfiles(){
  const sel = $('#llmProfileSelect');
  if(!sel) return;
  const profiles = currentUserState?.llm_profiles || [];
  const activateBtn = $('#btnActivateLlmProfile');
  const deleteBtn = $('#btnDeleteLlmProfile');
  if(!profiles.length){
    sel.innerHTML = `<option value="">${esc(t('llmNoProfiles'))}</option>`;
    sel.disabled = true;
    if(activateBtn) activateBtn.disabled = true;
    if(deleteBtn) deleteBtn.disabled = true;
    return;
  }
  sel.disabled = false;
  if(activateBtn) activateBtn.disabled = false;
  if(deleteBtn) deleteBtn.disabled = false;
  sel.innerHTML = profiles.map(p =>
    `<option value="${esc(p.id)}">${esc(p.label)} — ${esc(llmModelLabel(p.model_id))}</option>`
  ).join('');
  sel.value = currentUserState.active_llm_profile_id || profiles[0].id;
}

async function fillLlmPreference(){
  await loadLlmModels();
  renderLlmProfiles();
  updateApiKeyStatus();
}

async function recollect(){
  const btns = $$('#btnCollect');
  btns.forEach(b=>{
    b.disabled = true;
    b.dataset.i18nBusy = 'btnUpdating';
    b.textContent = t('btnUpdating');
  });
  try{
    const res = await api('/api/recollect', {method:'POST', body:'{}'});
    await loadAll();
    toast(t('toastUpdateDone', {n: res.count}), 'success');
  }catch(err){
    toast(t('toastUpdateFailed', {msg: err.message}), 'error');
  }finally{
    btns.forEach(b=>{
      b.disabled = false;
      delete b.dataset.i18nBusy;
      b.textContent = t('btnUpdate');
    });
  }
}

function fillCompany(){
  const f = $('#companyForm');
  if(!f) return;
  ['name','years','region','sector','keywords'].forEach(k=>{ if(f.elements[k]) f.elements[k].value = company[k] ?? ''; });
  ['venture','rnd_center'].forEach(k=>{ if(f.elements[k]) f.elements[k].checked = !!company[k]; });
  if(f.elements.keyword_mode_and) f.elements.keyword_mode_and.checked = company.keyword_mode === 'and';
}

$$('.nav').forEach(btn=>btn.addEventListener('click',()=>{
  $$('.nav').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  $$('.view').forEach(v=>v.classList.remove('active'));
  $(`#view-${btn.dataset.view}`).classList.add('active');
}));
['q','sourceFilter','statusFilter','aiFitFilter'].forEach(id=>$('#'+id)?.addEventListener('input', onFilterChange));
$('#pagePrev')?.addEventListener('click', ()=>{ if(currentPage > 1){ currentPage--; renderList(); } });
$('#pageNext')?.addEventListener('click', ()=>{ currentPage++; renderList(); });
$('#calPrev')?.addEventListener('click', ()=>{
  calViewDate.m--; if(calViewDate.m<1){ calViewDate.m=12; calViewDate.y--; }
  renderCalendar();
});
$('#calNext')?.addEventListener('click', ()=>{
  calViewDate.m++; if(calViewDate.m>12){ calViewDate.m=1; calViewDate.y++; }
  renderCalendar();
});
$('#calToday')?.addEventListener('click', ()=>{
  const t = new Date();
  const pad2 = n => String(n).padStart(2, '0');
  const todayStr = `${t.getFullYear()}-${pad2(t.getMonth()+1)}-${pad2(t.getDate())}`;
  calViewDate = {y: t.getFullYear(), m: t.getMonth()+1};
  if(notices.some(a=>a.end===todayStr)) calSelectedDate = todayStr;
  renderCalendar();
});
$('#btnCollect')?.addEventListener('click', recollect);
$('#companyForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  ['name','years','region','sector','keywords'].forEach(k=>{
    if(typeof data[k] === 'string' && data[k].trim() === '') delete data[k];
  });
  if(data.years !== undefined) data.years = Number(data.years);
  data.venture = form.elements.venture.checked;
  data.rnd_center = form.elements.rnd_center.checked;
  data.keyword_mode = form.elements.keyword_mode_and.checked ? 'and' : 'or';
  delete data.keyword_mode_and;
  const res = await api('/api/company', {method:'POST', body:JSON.stringify(data)});
  company = res.company || {};
  renderList();
  toast(t('toastCompanySaved'), 'success');
});
$('#btnDeleteCompany')?.addEventListener('click', async ()=>{
  if(!confirm(t('confirmDeleteCompany'))) return;
  const res = await api('/api/company', {method:'POST', body:JSON.stringify({})});
  company = res.company || {};
  fillCompany();
  renderList();
  toast(t('toastCompanyDeleted'), 'success');
});
$('#btnAiFit')?.addEventListener('click', async ()=>{
  const btn = $('#btnAiFit');
  if(btn){
    btn.disabled = true;
    btn.dataset.i18nBusy = 'btnAiAnalyzing';
    btn.textContent = t('btnAiAnalyzing');
  }
  try{
    const res = await api('/api/ai-fit', {method:'POST', body:'{}'});
    await loadAll();
    toast(t('toastAiFitDone', {n: res.count}), 'success');
  }catch(err){
    toast(t('toastAiFitFailed', {msg: err.message}), 'error');
  }finally{
    if(btn){
      btn.disabled = false;
      delete btn.dataset.i18nBusy;
      btn.textContent = t('btnRunAiFit');
    }
  }
});

$('#btnActivateLlmProfile')?.addEventListener('click', async ()=>{
  const profileId = $('#llmProfileSelect')?.value;
  if(!profileId) return;
  try{
    const res = await api('/api/me/llm-profiles/activate', {method:'POST', body:JSON.stringify({profile_id: profileId})});
    currentUserState = res.user;
    updateApiKeyStatus();
    toast(t('toastLlmProfileActivated'), 'success');
  }catch(err){ toast(err.message, 'error'); renderLlmProfiles(); }
});
$('#llmProfileForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  const hadNoProfiles = !(currentUserState?.llm_profiles?.length);
  try{
    const res = await api('/api/me/llm-profiles', {method:'POST', body:JSON.stringify(data)});
    currentUserState = res.user;
    form.reset();
    renderLlmProfiles();
    updateApiKeyStatus();
    toast(t(hadNoProfiles ? 'toastLlmProfileAdded' : 'toastLlmProfileAddedInactive'), 'success');
  }catch(err){ toast(err.message, 'error'); }
});
$('#btnDeleteLlmProfile')?.addEventListener('click', async ()=>{
  const profileId = $('#llmProfileSelect')?.value;
  if(!profileId) return;
  if(!confirm(t('confirmDeleteLlmProfile'))) return;
  try{
    const res = await api('/api/me/llm-profiles/delete', {method:'POST', body:JSON.stringify({profile_id: profileId})});
    currentUserState = res.user;
    renderLlmProfiles();
    updateApiKeyStatus();
    toast(t('toastLlmProfileDeleted'), 'success');
  }catch(err){ toast(err.message, 'error'); }
});

$('#bizinfoKeyForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  if(!data.bizinfo_key){ toast(t('toastBizinfoNoChange')); return; }
  try{
    const res = await api('/api/me/bizinfo-key', {method:'POST', body:JSON.stringify({bizinfo_key: data.bizinfo_key})});
    if(currentUserState) currentUserState.has_bizinfo_key = res.has_bizinfo_key;
    updateApiKeyStatus();
    form.reset();
    await loadAll();
    toast(t('toastBizinfoSaved'), 'success');
  }catch(err){ toast(err.message, 'error'); }
});
$('#btnDeleteBizinfoKey')?.addEventListener('click', async ()=>{
  if(!confirm(t('confirmDeleteBizinfoKey'))) return;
  try{
    const res = await api('/api/me/bizinfo-key', {method:'POST', body:JSON.stringify({bizinfo_key: ''})});
    if(currentUserState) currentUserState.has_bizinfo_key = res.has_bizinfo_key;
    updateApiKeyStatus();
    $('#bizinfoKeyForm')?.reset();
    await loadAll();
    toast(t('toastBizinfoDeleted'), 'success');
  }catch(err){ toast(err.message, 'error'); }
});


async function loadCompanyDocs(){
  const el = $('#companyDocsList');
  if(!el || !currentUserState) return;
  try{
    const res = await api('/api/company/documents');
    const items = res.items || [];
    el.innerHTML = items.length
      ? items.map(d => `
          <div class="doc-row" data-id="${esc(d.id)}">
            <span class="doc-name">${esc(d.filename)}</span>
            <span class="doc-meta">${esc(t('docCharCount', {n: d.char_count}))}</span>
            <button type="button" class="button doc-delete">${esc(t('btnDelete'))}</button>
          </div>
        `).join('')
      : `<p class="meta">${esc(t('emptyDocs'))}</p>`;
    el.querySelectorAll('.doc-delete').forEach(btn=>{
      btn.addEventListener('click', async ()=>{
        const doc_id = btn.closest('.doc-row').dataset.id;
        try{
          await api('/api/company/documents/delete', {method:'POST', body:JSON.stringify({doc_id})});
          loadCompanyDocs();
        }catch(err){ toast(err.message, 'error'); }
      });
    });
  }catch(err){ el.innerHTML = `<p class="meta">${esc(err.message)}</p>`; }
}

$('#companyDocsForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const input = $('#companyDocsInput');
  if(!input.files.length){ toast(t('toastNoFileSelected'), 'error'); return; }
  const fd = new FormData();
  for(const f of input.files) fd.append('files', f);
  try{
    const res = await fetch('/api/company/documents', {method:'POST', credentials:'same-origin', body: fd});
    const data = await res.json();
    if(!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
    form.reset();
    toast(t('toastDocUploaded'), 'success');
    loadCompanyDocs();
  }catch(err){ toast(err.message, 'error'); }
});

let authMode = 'login';

function openAuthModal(mode){
  authMode = mode;
  $('#authModalTitle').textContent = mode === 'login' ? t('authTitleLogin') : t('authTitleSignup');
  $('#authSubmit').textContent = mode === 'login' ? t('authSubmitLogin') : t('authSubmitSignup');
  $('#authSwitchLabel').textContent = mode === 'login' ? t('authSwitchToSignup') : t('authSwitchToLogin');
  $('#authSwitchMode').textContent = mode === 'login' ? t('authSwitchLinkSignup') : t('authSwitchLinkLogin');
  $('#authModal').classList.remove('hidden');
}
function closeAuthModal(){
  const modal = $('#authModal');
  modal.classList.add('closing');
  setTimeout(()=>{
    modal.classList.add('hidden');
    modal.classList.remove('closing');
    $('#authForm').reset();
  }, 70);
}

$('#btnOpenLogin')?.addEventListener('click', ()=>openAuthModal('login'));
$('#btnCompanyLogin')?.addEventListener('click', ()=>openAuthModal('login'));
$('#authModalClose')?.addEventListener('click', closeAuthModal);
$('#authModal')?.addEventListener('click', e=>{ if(e.target.id === 'authModal') closeAuthModal(); });
$('#authSwitchMode')?.addEventListener('click', e=>{
  e.preventDefault();
  openAuthModal(authMode === 'login' ? 'signup' : 'login');
});
$('#authForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.currentTarget).entries());
  try{
    const res = await api(authMode === 'login' ? '/api/auth/login' : '/api/auth/signup', {method:'POST', body:JSON.stringify(data)});
    currentUserState = res.user;
    closeAuthModal();
    await loadAll();
  }catch(err){ toast(err.message, 'error'); }
});
$('#btnLogout')?.addEventListener('click', async ()=>{
  try{ await api('/api/auth/logout', {method:'POST', body:'{}'}); }catch(err){ /* ignore */ }
  currentUserState = null;
  await loadAll();
});

const EYE_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 7 11 7a18.5 18.5 0 0 1-3.22 4.19M6.61 6.61C3.9 8.36 2 11 2 11s4 7 11 7a10.6 10.6 0 0 0 5.39-1.61"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

$$('[data-eye-toggle]').forEach(btn=>{
  btn.innerHTML = EYE_ICON;
  btn.addEventListener('click', ()=>{
    const input = $('#'+btn.dataset.eyeToggle);
    if(!input) return;
    const hidden = input.type === 'password';
    input.type = hidden ? 'text' : 'password';
    btn.innerHTML = hidden ? EYE_OFF_ICON : EYE_ICON;
  });
});

applyStaticTranslations();
document.addEventListener('langchange', ()=>{
  renderSources();
  renderList();
  fillLlmPreference();
  if(currentUserState) loadCompanyDocs();
  if(currentUserState?.is_admin){
    loadAllSources();
    loadSchedulerConfig();
  }
});
$$('[data-lang-btn]').forEach(btn=>{
  btn.addEventListener('click', ()=>setLang(btn.dataset.langBtn));
});

loadAll().catch(err=>{
  document.body.innerHTML = `<main style="padding:30px"><h1>${esc(t('serverConnFailed'))}</h1><pre>${esc(err.stack || err.message)}</pre><p>${esc(t('serverConnFailedHint'))}</p></main>`;
});
