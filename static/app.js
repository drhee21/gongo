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
        <span class="badge ${statusClass(a.status)}">${esc(a.status)}${a.status_inferred ? '?' : ''}</span>
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
  renderAiFitMini();
  renderCalendar();
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
    ['적합', counts.fit, 'state-ok'],
    ['확인', counts.unsure, 'state-wait'],
    ['부적합', counts.unfit, 'state-bad'],
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

  $('#calendarList').innerHTML = days.length ? days.map(d=>{
    const entries = groups[d];
    const rep = entries[0];
    return `<div class="day" data-date="${esc(d)}"><h3>${esc(d)} <span class="badge ${statusClass(rep.status)}">${esc(ddayText(rep))}</span></h3>${entries.map(a=>`<div class="day-entry"><a href="${esc(a.url)}" target="_blank">${esc(a.title)}</a></div>`).join('')}</div>`;
  }).join('') : `<div class="empty">이번 달 마감인 공고가 없습니다.</div>`;
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
  fillLlmPreference();
  const sourcesPanel = $('#adminSourcesPanel');
  if(currentUserState?.is_admin){
    sourcesPanel?.classList.remove('hidden');
    loadAllSources();
  } else {
    sourcesPanel?.classList.add('hidden');
  }
}

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
    const hardcoded = (overridesRes.items || []).map(s => `
      <div class="custom-source-row" data-source="${esc(s.source_id)}" data-kind="override">
        <div class="custom-source-info">
          <div><strong>${esc(s.name)}</strong> <span class="badge-warn" style="background:rgba(0,0,0,.06);color:var(--muted)">기존 소스</span>${s.enabled ? '' : '<span class="badge-warn">비활성</span>'}</div>
          <div class="custom-source-url">${esc(s.override_url || s.default_url || '(없음)')}${s.override_url ? ' <span class="meta">(기본값 재정의됨)</span>' : ''}</div>
        </div>
        <div class="custom-source-actions">
          <button type="button" class="button override-source-edit">수정</button>
          <button type="button" class="button override-source-toggle">${s.enabled ? '비활성화' : '활성화'}</button>
        </div>
      </div>
    `).join('');
    const custom = (customRes.items || []).map(s => `
      <div class="custom-source-row" data-source="${esc(s.id)}" data-kind="custom">
        <div class="custom-source-info">
          <div><strong>${esc(s.name)}</strong> ${s.enabled ? '' : '<span class="badge-warn">비활성</span>'}${s.anomaly ? `<span class="badge-warn" title="${esc(s.anomaly_note||'')}">⚠ 이상감지</span>` : ''}${s.recipe_mode === 'llm_direct' ? '<span class="badge-warn" title="매 수집마다 LLM 호출이 필요합니다">⚡ llm_direct</span>' : ''}</div>
          <div class="custom-source-url">${esc(s.list_url)}</div>
          <div class="meta">${esc(s.state || '미확인')} · ${esc(Number(s.count)||0)}건 ${s.last_collected_at ? '· ' + esc(s.last_collected_at) : ''}</div>
        </div>
        <div class="custom-source-actions">
          <button type="button" class="button custom-source-edit">수정</button>
          <button type="button" class="button custom-source-toggle">${s.enabled ? '비활성화' : '활성화'}</button>
          <button type="button" class="button custom-source-remove">삭제</button>
        </div>
      </div>
    `).join('');
    el.innerHTML = hardcoded + custom || '<p class="meta">소스가 없습니다.</p>';

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
          toast(enabling ? '활성화 완료' : '비활성화 완료', 'success');
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
        const enabling = btn.textContent === '활성화';
        try{
          await api('/api/admin/custom-sources/toggle', {method:'POST', body:JSON.stringify({source_id, enabled: enabling})});
          toast(enabling ? '활성화 완료' : '비활성화 완료', 'success');
          loadAllSources();
        }catch(err){ toast(err.message, 'error'); }
      });
    });
    el.querySelectorAll('.custom-source-remove').forEach(btn=>{
      btn.addEventListener('click', async ()=>{
        const row = btn.closest('.custom-source-row');
        const source_id = row.dataset.source;
        if(!confirm('정말 삭제하시겠습니까? 수집된 공고도 함께 삭제됩니다(즐겨찾기한 공고는 남습니다).')) return;
        try{
          await api('/api/admin/custom-sources/remove', {method:'POST', body:JSON.stringify({source_id})});
          toast('삭제 완료', 'success');
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
    const hint = kind === 'override'
      ? '이 소스는 아직 전용 수집기를 사용합니다 — 이름/URL을 바꾸고 저장하면 재발견 없이 바로 적용됩니다.'
      : 'URL을 바꾸면 다시 미리보기를 거쳐야 합니다.';
    banner.innerHTML = `"${esc(originalName)}" 수정 중 — ${hint} <a href="#" id="btnCancelEditCustomSource">취소</a>`;
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
    <p><strong>${esc(p.name)}</strong> — 총 ${esc(p.item_count)}건 발견</p>
    ${warningsHtml}
    <table>
      <thead><tr><th>제목</th><th>기관</th><th>기간</th><th>URL</th></tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>
    <details><summary>레시피 원본 보기</summary><pre>${esc(JSON.stringify(p.recipe, null, 2))}</pre></details>
    <div class="preview-actions">
      <button type="button" class="primary" id="btnConfirmCustomSource">${p.editing ? '확정 (변경사항 반영)' : '확정 (수집에 반영)'}</button>
      <button type="button" class="button" id="btnCancelCustomSource">취소</button>
    </div>
  `;
  $('#btnConfirmCustomSource')?.addEventListener('click', async ()=>{
    const btn = $('#btnConfirmCustomSource');
    btn.disabled = true;
    try{
      await api('/api/admin/custom-sources/confirm', {method:'POST', body:JSON.stringify(pendingCustomSource)});
      toast(pendingCustomSource.editing ? '수정 완료' : '등록 완료 — 이후부터 자동으로 수집됩니다', 'success');
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
  if(!name || !url){ toast('이름과 URL을 모두 입력해주세요.', 'error'); return; }

  // 기존 하드코딩 소스(K-스타트업/NRF 등)는 아직 전용 수집기를 쓰므로 이름/URL 중
  // 뭘 바꿔도 재발견 없이 항상 바로 저장한다.
  if(editingCustomSource?.kind === 'override'){
    try{
      await api('/api/admin/source-overrides', {method:'POST', body:JSON.stringify({source_id: editingCustomSource.id, list_url: url, name})});
      toast('수정 내용 저장 완료', 'success');
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
      toast('이름 수정 완료', 'success');
      clearCustomSourceForm();
      loadAllSources();
      await loadAll();
    }catch(err){ toast(err.message, 'error'); }
    return;
  }

  const btn = $('#btnDiscoverCustomSource');
  btn.disabled = true;
  btn.dataset.originalText = btn.textContent;
  btn.textContent = '분석 중... (최대 1~2분)';
  try{
    const body = {name, url};
    if(editingCustomSource) body.existing_source_id = editingCustomSource.id;
    const res = await api('/api/admin/custom-sources/discover', {method:'POST', body:JSON.stringify(body)});
    pendingCustomSource = res;
    renderCustomSourcePreview();
    toast('미리보기 준비 완료 — 확인 후 확정해주세요', 'success');
  }catch(err){
    toast(err.message, 'error');
  }finally{
    btn.disabled = false;
    btn.textContent = btn.dataset.originalText || '저장';
  }
});

function updateApiKeyStatus(){
  const bizEl = $('#bizinfoKeyStatus');
  if(bizEl) bizEl.textContent = currentUserState?.has_bizinfo_key ? '기업마당 API 키가 등록되어 있습니다.' : '등록된 기업마당 API 키가 없습니다 — 기업마당 공고가 목록에서 보이지 않습니다.';
  const el = $('#llmPreferenceStatus');
  if(el) el.textContent = currentUserState?.has_llm_key ? 'API 키가 등록되어 있습니다.' : '등록된 API 키가 없습니다.';
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

async function fillLlmPreference(){
  await loadLlmModels();
  const f = $('#llmPreferenceForm');
  if(!f) return;
  const pref = currentUserState?.llm_preference || {};
  if(f.elements.model_id && pref.model_id) f.elements.model_id.value = pref.model_id;
  updateApiKeyStatus();
}

async function recollect(){
  const btns = $$('#btnCollect');
  btns.forEach(b=>{
    b.disabled = true;
    b.dataset.originalText = b.textContent;
    b.textContent = '업데이트 중...';
  });
  try{
    const res = await api('/api/recollect', {method:'POST', body:'{}'});
    await loadAll();
    toast(`업데이트 완료: ${res.count}건`, 'success');
  }catch(err){
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
  toast('회사 정보 저장 완료', 'success');
});
$('#btnDeleteCompany')?.addEventListener('click', async ()=>{
  if(!confirm('저장된 회사 정보를 삭제할까요?')) return;
  const res = await api('/api/company', {method:'POST', body:JSON.stringify({})});
  company = res.company || {};
  fillCompany();
  renderList();
  toast('회사 정보 삭제 완료', 'success');
});
$('#btnAiFit')?.addEventListener('click', async ()=>{
  const btn = $('#btnAiFit');
  if(btn){
    btn.disabled = true;
    btn.dataset.originalText = btn.textContent;
    btn.textContent = 'AI 분석 중...';
  }
  try{
    const res = await api('/api/ai-fit', {method:'POST', body:'{}'});
    await loadAll();
    toast(`AI 분석 완료: 총 ${res.count}건`, 'success');
  }catch(err){
    toast(`AI 분석 실패: ${err.message}`, 'error');
  }finally{
    if(btn){
      btn.disabled = false;
      btn.textContent = btn.dataset.originalText || 'AI로 맞춤 공고 분석 실행';
    }
  }
});

$('#llmPreferenceForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  try{
    const prefRes = await api('/api/me/llm-preference', {method:'POST', body:JSON.stringify({model_id: data.model_id})});
    if(data.key){
      const keyRes = await api('/api/me/llm-key', {method:'POST', body:JSON.stringify({model_id: data.model_id, key: data.key})});
      prefRes.has_llm_key = keyRes.has_llm_key;
    }
    if(currentUserState){
      currentUserState.llm_preference = prefRes.llm_preference;
      currentUserState.has_llm_key = prefRes.has_llm_key;
    }
    form.elements.key.value = '';
    fillLlmPreference();
    toast('AI 모델 설정 저장 완료', 'success');
  }catch(err){ toast(err.message, 'error'); }
});
$('#btnDeleteLlmKey')?.addEventListener('click', async ()=>{
  const modelId = $('#llmModelSelect')?.value;
  if(!modelId) return;
  if(!confirm('등록된 API 키를 삭제할까요?')) return;
  try{
    const res = await api('/api/me/llm-key', {method:'POST', body:JSON.stringify({model_id: modelId, key: ''})});
    if(currentUserState) currentUserState.has_llm_key = res.has_llm_key;
    updateApiKeyStatus();
    $('#llmPreferenceForm')?.reset();
    toast('API 키 삭제 완료', 'success');
  }catch(err){ toast(err.message, 'error'); }
});

$('#bizinfoKeyForm')?.addEventListener('submit', async e=>{
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  if(!data.bizinfo_key){ toast('입력한 내용이 없어 변경하지 않았습니다'); return; }
  try{
    const res = await api('/api/me/bizinfo-key', {method:'POST', body:JSON.stringify({bizinfo_key: data.bizinfo_key})});
    if(currentUserState) currentUserState.has_bizinfo_key = res.has_bizinfo_key;
    updateApiKeyStatus();
    form.reset();
    await loadAll();
    toast('기업마당 API 키 저장 완료', 'success');
  }catch(err){ toast(err.message, 'error'); }
});
$('#btnDeleteBizinfoKey')?.addEventListener('click', async ()=>{
  if(!confirm('등록된 기업마당 API 키를 삭제할까요?')) return;
  try{
    const res = await api('/api/me/bizinfo-key', {method:'POST', body:JSON.stringify({bizinfo_key: ''})});
    if(currentUserState) currentUserState.has_bizinfo_key = res.has_bizinfo_key;
    updateApiKeyStatus();
    $('#bizinfoKeyForm')?.reset();
    await loadAll();
    toast('기업마당 API 키 삭제 완료', 'success');
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
