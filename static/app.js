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

function api(path, opts={}){
  return fetch(path, {headers:{'Content-Type':'application/json'}, credentials:'same-origin', ...opts}).then(async r=>{
    const data = await r.json().catch(()=>({ok:false,error:'JSON 응답 아님'}));
    if(!r.ok || data.ok === false) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  });
}

function canonicalSource(id){
  return ({biohub_direct:'biohub', khidi_direct:'khidi'}[id] || id || 'unknown');
}
function sourceName(id){
  const cid = canonicalSource(id);
  const fallback = {
    bizinfo:'기업마당',
    kstartup:'K-스타트업',
    biohub:'서울바이오허브',
    khidi:'보건산업진흥원/KHIDI',
    kddf:'국가신약개발사업단',
    nrf:'한국연구재단',
    g2b:'나라장터',
    sample:'샘플'
  };
  const s = sources.find(x=>canonicalSource(x.id)===cid);
  return s?.name || fallback[cid] || cid;
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
  if(!e) return '제한 없음/확인 필요';
  const p=[];
  if(e.minYears != null && e.maxYears != null) p.push(`업력 ${e.minYears}~${e.maxYears}년`);
  else if(e.maxYears != null) p.push(`업력 ${e.maxYears}년 이내`);
  else if(e.minYears != null) p.push(`업력 ${e.minYears}년 이상`);
  if(e.regions?.length) p.push(`지역 ${e.regions.join('·')}`);
  if(e.sectors?.length) p.push(e.sectors.join('·'));
  return p.join(' / ') || '제한 없음/확인 필요';
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
  const label = {fit:'적합', unfit:'부적합', unsure:'확인'}[a.ai_fit] || '확인';
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
    <span class="ai-reason-tag">AI 판정 근거</span>
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
      <div class="meta">${esc(a.org)} · ${esc(a.category)} · ${a.dates_unknown ? '날짜 미상' : `${a.start ? esc(a.start) + ' ' : ''}~ ${a.end ? esc(a.end) : (a.status === '상시' ? '상시' : '-')}`}</div>
      <div class="badges">
        <span class="badge ${statusClass(a.status)}">${esc(a.status)}</span>
        ${ddayText(a) !== a.status ? `<span class="badge ${a.dday != null && a.dday <= 7 && a.dday >= 0 ? 'urgent' : ''}">${esc(ddayText(a))}</span>` : ''}
        ${aiFitBadge(a)}
      </div>
      ${aiReasonRow(a)}
    </div>
    <div class="right">
      <button class="star ${a.favorite?'on':''}" data-star="${esc(a.id)}">${a.favorite?'★':'☆'}</button>
      <button class="detail-toggle" data-detail-toggle="${esc(a.id)}" aria-expanded="false">상세 <span class="detail-caret">▾</span></button>
    </div>
    <div class="detail">
      <div class="meta"><b>지원규모</b> ${esc(a.budget || '공고 참조')}</div>
      <div class="meta"><b>신청자격</b> ${esc(eligText(a.elig))}</div>
      <div class="meta"><b>원문</b> ${srcLinks}</div>
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

function renderList(){
  const list = filtered(notices);
  $('#noticeList').innerHTML = list.length ? list.map(noticeHTML).join('') : `<div class="empty">조건에 맞는 공고가 없습니다.</div>`;
  bindNoticeEvents($('#noticeList'));

  const favs = notices.filter(x=>x.favorite);
  const favEmptyMsg = currentUserState ? '관심 공고가 없습니다.' : '로그인하면 관심 공고를 저장할 수 있습니다.';
  $('#favList').innerHTML = favs.length ? favs.map(noticeHTML).join('') : `<div class="empty">${esc(favEmptyMsg)}</div>`;
  bindNoticeEvents($('#favList'));

  $('#kpiTotal').textContent = notices.length;
  $('#kpiOpen').textContent = notices.filter(x=>x.status!=='마감').length;
  $('#kpiUrgent').textContent = notices.filter(x=>x.dday != null && x.dday >= 0 && x.dday <= 7).length;
  $('#kpiFav').textContent = favs.length;
  renderCalendar();
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
  if(monthLabel) monthLabel.textContent = `${y}년 ${m}월`;

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
      ${dayNotices.length ? `<div class="cal-count">${dayNotices.length}건</div>` : ''}
    </div>`);
  }
  const grid = $('#calendarGrid');
  if(grid){
    grid.innerHTML = `
      <div class="cal-weekdays">${['일','월','화','수','목','금','토'].map(w=>`<div>${w}</div>`).join('')}</div>
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

  $('#calendarList').innerHTML = days.length ? days.map(d=>`
    <div class="day" data-date="${esc(d)}"><h3>${esc(d)}</h3>${groups[d].map(a=>`<div class="day-entry"><a href="${esc(a.url)}" target="_blank">${esc(a.title)}</a> <span class="badge ${statusClass(a.status)}">${esc(ddayText(a))}</span></div>`).join('')}</div>`).join('') : `<div class="empty">이번 달 마감인 공고가 없습니다.</div>`;
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
    g.method = g.method || s.method || '';
    if(s.last_collected_at && (!g.last_collected_at || s.last_collected_at > g.last_collected_at)) g.last_collected_at = s.last_collected_at;
    if(!g.error && s.error) g.error = s.error;
    if(['대기','비활성화','0건','미확인'].includes(g.state) && s.state) g.state = s.state;
  });
  Object.entries(noticeCounts).forEach(([id,count])=>{
    if(!groups.has(id)) groups.set(id, {id, name:sourceName(id), method:'', state:'정상', count});
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
  const mini = displaySources.map(s=>`<div class="source-mini"><span>${esc(s.name || s.id)}</span><span class="state-ok">정상 ${Number(s.count)||0}</span></div>`).join('');
  $('#sourceMini').innerHTML = mini || '<div class="source-mini">아직 수집 전</div>';
  const adminRows = rawSources.length ? rawSources : displaySources;
  $('#sourcesTable').innerHTML = `<table class="table"><thead><tr><th>ID</th><th>소스</th><th>방식</th><th>상태</th><th>건수</th><th>오류</th></tr></thead><tbody>${adminRows.map(s=>`<tr><td>${esc(s.id)}</td><td>${esc(s.name||s.id)}</td><td>${esc(s.method||'')}</td><td>${esc(s.state||'미확인')}${s.anomaly?`<span class="badge-warn" title="${esc(s.anomaly_note||'')}">⚠ 이상감지</span>`:''}</td><td>${esc(Number(s.count)||0)}</td><td>${esc(s.error||s.anomaly_note||'')}</td></tr>`).join('')}</tbody></table>`;
  const sel = $('#sourceFilter');
  const cur = canonicalSource(sel.value);
  const ids = [...new Set(notices.flatMap(a=>noticeSources(a).map(s=>s.id)))];
  sel.innerHTML = `<option value="">전체 소스</option>` + ids.map(id=>`<option value="${esc(id)}">${esc(sourceName(id))}</option>`).join('');
  sel.value = ids.includes(cur) ? cur : '';
}

async function loadAll(){
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
  const overridesPanel = $('#adminOverridesPanel');
  if(currentUserState?.is_admin){
    overridesPanel?.classList.remove('hidden');
    loadSourceOverrides();
  } else {
    overridesPanel?.classList.add('hidden');
  }
}

async function loadSourceOverrides(){
  const el = $('#sourceOverridesTable');
  if(!el) return;
  try{
    const res = await api('/api/admin/source-overrides');
    const items = res.items || [];
    el.innerHTML = items.map(s => `
      <div class="override-row" data-source="${esc(s.source_id)}">
        <div class="override-label">${esc(s.name)}</div>
        <div class="override-current">기본값: ${esc(s.default_url || '(없음)')}</div>
        <input type="text" class="override-input" placeholder="재정의할 URL (비우면 기본값 사용)" value="${esc(s.override_url || '')}" />
        <button type="button" class="button override-save">저장</button>
      </div>
    `).join('') || '<p class="meta">재정의 가능한 소스가 없습니다.</p>';
    el.querySelectorAll('.override-save').forEach(btn=>{
      btn.addEventListener('click', async ()=>{
        const row = btn.closest('.override-row');
        const source_id = row.dataset.source;
        const list_url = row.querySelector('.override-input').value.trim();
        try{
          await api('/api/admin/source-overrides', {method:'POST', body:JSON.stringify({source_id, list_url})});
          toast(list_url ? '재정의 저장 완료' : '재정의 해제 완료', 'success');
          loadSourceOverrides();
        }catch(err){ toast(err.message, 'error'); }
      });
    });
  }catch(err){ el.innerHTML = `<p class="meta">${esc(err.message)}</p>`; }
}

function updateApiKeyStatus(){
  const el = $('#apiKeyStatus');
  if(el) el.textContent = currentUserState?.has_api_key ? 'API 키가 등록되어 있습니다.' : '등록된 API 키가 없습니다.';
  const bizEl = $('#bizinfoKeyStatus');
  if(bizEl) bizEl.textContent = currentUserState?.has_bizinfo_key ? '기업마당 API 키가 등록되어 있습니다.' : '등록된 기업마당 API 키가 없습니다 — 기업마당 공고가 목록에서 보이지 않습니다.';
}

async function recollect(){
  const result = $('#collectResult');
  const btns = $$('#btnCollect, #btnCollectAdmin');
  btns.forEach(b=>{
    b.disabled = true;
    b.dataset.originalText = b.textContent;
    b.textContent = '업데이트 중...';
  });
  if(result) result.textContent = '업데이트 중...';
  try{
    const res = await api('/api/recollect', {method:'POST', body:'{}'});
    if(result) result.textContent = JSON.stringify(res, null, 2);
    await loadAll();
    toast(`업데이트 완료: ${res.count}건`, 'success');
  }catch(err){
    if(result) result.textContent = err.stack || err.message;
    toast(`업데이트 실패: ${err.message}`, 'error');
  }finally{
    btns.forEach(b=>{
      b.disabled = false;
      b.textContent = b.dataset.originalText || '업데이트';
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
['q','sourceFilter','statusFilter','aiFitFilter'].forEach(id=>$('#'+id)?.addEventListener('input', renderList));
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
  calViewDate = {y: t.getFullYear(), m: t.getMonth()+1};
  renderCalendar();
});
$('#btnCollect')?.addEventListener('click', recollect);
$('#btnCollectAdmin')?.addEventListener('click', recollect);
$('#companyForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  data.years = data.years === '' ? '' : Number(data.years);
  data.venture = form.elements.venture.checked;
  data.rnd_center = form.elements.rnd_center.checked;
  data.keyword_mode = form.elements.keyword_mode_and.checked ? 'and' : 'or';
  delete data.keyword_mode_and;
  const res = await api('/api/company', {method:'POST', body:JSON.stringify(data)});
  company = res.company || {};
  renderList();
  toast('회사 정보 저장 완료', 'success');
});
$('#btnAiFit')?.addEventListener('click', async ()=>{
  const btn = $('#btnAiFit');
  const result = $('#aiFitResult');
  if(btn){
    btn.disabled = true;
    btn.dataset.originalText = btn.textContent;
    btn.textContent = 'AI 분석 중...';
  }
  if(result) result.textContent = 'AI 분석 중... (공고 건수에 따라 수 분 걸릴 수 있습니다)';
  try{
    const res = await api('/api/ai-fit', {method:'POST', body:'{}'});
    if(result) result.textContent = `분석 완료: 총 ${res.count}건\n${JSON.stringify(res.counts, null, 2)}`;
    await loadAll();
    toast(`AI 분석 완료: 총 ${res.count}건`, 'success');
  }catch(err){
    if(result) result.textContent = err.stack || err.message;
    toast(`AI 분석 실패: ${err.message}`, 'error');
  }finally{
    if(btn){
      btn.disabled = false;
      btn.textContent = btn.dataset.originalText || 'AI로 맞춤 공고 분석 실행';
    }
  }
});

$('#apiKeyForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  try{
    const res = await api('/api/me/api-key', {method:'POST', body:JSON.stringify({api_key: data.api_key})});
    if(currentUserState) currentUserState.has_api_key = res.has_api_key;
    updateApiKeyStatus();
    form.reset();
    toast(res.has_api_key ? 'API 키 저장 완료' : 'API 키 삭제 완료', 'success');
  }catch(err){ toast(err.message, 'error'); }
});

$('#bizinfoKeyForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  try{
    const res = await api('/api/me/bizinfo-key', {method:'POST', body:JSON.stringify({bizinfo_key: data.bizinfo_key})});
    if(currentUserState) currentUserState.has_bizinfo_key = res.has_bizinfo_key;
    updateApiKeyStatus();
    form.reset();
    await loadAll();
    toast(res.has_bizinfo_key ? '기업마당 API 키 저장 완료' : '기업마당 API 키 삭제 완료', 'success');
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
            <span class="doc-meta">${esc(d.char_count)}자</span>
            <button type="button" class="button doc-delete">삭제</button>
          </div>
        `).join('')
      : '<p class="meta">등록된 문서가 없습니다.</p>';
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
  if(!input.files.length){ toast('업로드할 파일을 선택해주세요.', 'error'); return; }
  const fd = new FormData();
  for(const f of input.files) fd.append('files', f);
  try{
    const res = await fetch('/api/company/documents', {method:'POST', credentials:'same-origin', body: fd});
    const data = await res.json();
    if(!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
    form.reset();
    toast('문서 업로드 완료', 'success');
    loadCompanyDocs();
  }catch(err){ toast(err.message, 'error'); }
});

let authMode = 'login';

function openAuthModal(mode){
  authMode = mode;
  $('#authModalTitle').textContent = mode === 'login' ? '로그인' : '회원가입';
  $('#authSubmit').textContent = mode === 'login' ? '로그인' : '가입하기';
  $('#authSwitchLabel').textContent = mode === 'login' ? '계정이 없으신가요?' : '이미 계정이 있으신가요?';
  $('#authSwitchMode').textContent = mode === 'login' ? '가입하기' : '로그인';
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

loadAll().catch(err=>{
  document.body.innerHTML = `<main style="padding:30px"><h1>서버 연결 실패</h1><pre>${esc(err.stack || err.message)}</pre><p>PowerShell에서 <code>python server.py</code> 또는 <code>start-server-v3.ps1</code>로 실행했는지 확인하세요.</p></main>`;
});
