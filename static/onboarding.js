// 신규 가입자 대상 가이드 투어. currentUserState.onboarding_done이 false인 계정에서만
// loadAll() 마지막에 자동 시작된다(app.js 참고). 완료/건너뛰기 시 서버에 저장해
// 다음 로그인부터는 다시 뜨지 않는다.
//
// 맨 처음(onboardIndex === -1)은 언어 선택 단계다 — 신규 가입 직후에는 사용자가
// 아직 언어를 고를 기회가 없었으므로(currentLang은 그냥 기본값 'ko'), 이 단계의
// 문구만은 t()를 쓰지 않고 한국어·영어를 한 문장에 같이 보여준다. 언어 버튼을
// 누르면 그 즉시 실제 투어(0단계)로 넘어간다.
const ONBOARDING_STEPS = [
  { view: 'list', selector: '.filters', titleKey: 'onboard1Title', bodyKey: 'onboard1Body' },
  { view: 'company', selector: '#companyForm', titleKey: 'onboard2Title', bodyKey: 'onboard2Body' },
  { view: 'company', selector: '#llmKeyInput', titleKey: 'onboard3Title', bodyKey: 'onboard3Body' },
  { view: 'company', selector: '#btnAiFit', titleKey: 'onboard4Title', bodyKey: 'onboard4Body' },
  { view: 'list', selector: '#aiFitFilter', titleKey: 'onboard5Title', bodyKey: 'onboard5Body' },
];

let onboardActive = false;
let onboardIndex = -1;

function startOnboardingTour(){
  if(onboardActive) return;
  onboardActive = true;
  onboardIndex = -1;
  buildOnboardingDom();
  showOnboardingStep(-1);
}

function buildOnboardingDom(){
  if($('#onboardTooltip')) return;

  const spot = document.createElement('div');
  spot.id = 'onboardSpotlight';
  spot.className = 'onboard-spotlight';
  document.body.appendChild(spot);

  const tip = document.createElement('div');
  tip.id = 'onboardTooltip';
  tip.className = 'onboard-tooltip';
  tip.innerHTML = `
    <div class="onboard-step-count" id="onboardStepCount"></div>
    <h3 id="onboardTipTitle"></h3>
    <p id="onboardTipBody"></p>
    <div class="onboard-actions">
      <button type="button" id="onboardSkipBtn" class="button"></button>
      <div class="onboard-actions-right">
        <button type="button" id="onboardPrevBtn" class="button"></button>
        <button type="button" id="onboardNextBtn" class="primary"></button>
      </div>
    </div>
  `;
  document.body.appendChild(tip);

  $('#onboardSkipBtn').addEventListener('click', endOnboardingTour);
  $('#onboardPrevBtn').addEventListener('click', ()=>{
    if(onboardIndex > 0){ onboardIndex--; showOnboardingStep(onboardIndex); }
  });
  $('#onboardNextBtn').addEventListener('click', ()=>{
    if(onboardIndex === -1){ onboardIndex = 0; showOnboardingStep(0); return; }
    if(onboardIndex >= ONBOARDING_STEPS.length - 1) endOnboardingTour();
    else { onboardIndex++; showOnboardingStep(onboardIndex); }
  });
  window.addEventListener('resize', ()=>{
    if(onboardActive) positionOnboardingSpotlight(currentOnboardSpotlightTarget());
  });

  // 언어 버튼을 누르면: 아직 언어 선택 단계면 바로 실제 투어를 시작하고,
  // 이미 투어 중이면(다른 단계에서 언어를 바꾼 경우) 현재 단계 문구만 새 언어로 다시 그린다.
  document.querySelectorAll('[data-lang-btn]').forEach(btn=>{
    btn.addEventListener('click', ()=>{
      if(!onboardActive) return;
      if(onboardIndex === -1){ onboardIndex = 0; showOnboardingStep(0); }
      else { showOnboardingStep(onboardIndex); }
    });
  });
}

function currentOnboardSpotlightTarget(){
  return onboardIndex === -1 ? { selector: '.lang-switch' } : ONBOARDING_STEPS[onboardIndex];
}

function showOnboardingStep(i){
  onboardIndex = i;

  if(i === -1){
    $('#onboardStepCount').textContent = '';
    $('#onboardTipTitle').textContent = '언어를 선택해주세요 · Choose a language';
    $('#onboardTipBody').textContent = '아래에서 사용할 언어를 고르세요. 나중에 언제든 바꿀 수 있습니다. · Pick the language you want below — you can change it anytime later.';
    $('#onboardSkipBtn').textContent = '건너뛰기 · Skip';
    $('#onboardPrevBtn').textContent = t('onboardPrev');
    $('#onboardPrevBtn').disabled = true;
    $('#onboardNextBtn').textContent = '다음 · Next';
    setTimeout(()=>positionOnboardingSpotlight({selector: '.lang-switch'}), 0);
    return;
  }

  const step = ONBOARDING_STEPS[i];
  const navBtn = document.querySelector(`.nav[data-view="${step.view}"]`);
  if(navBtn && !navBtn.classList.contains('active')) navBtn.click();

  $('#onboardStepCount').textContent = t('onboardStep', {step: i + 1, total: ONBOARDING_STEPS.length});
  $('#onboardTipTitle').textContent = t(step.titleKey);
  $('#onboardTipBody').textContent = t(step.bodyKey);
  $('#onboardSkipBtn').textContent = t('onboardSkip');
  $('#onboardPrevBtn').textContent = t('onboardPrev');
  $('#onboardPrevBtn').disabled = i === 0;
  $('#onboardNextBtn').textContent = i === ONBOARDING_STEPS.length - 1 ? t('onboardFinish') : t('onboardNext');

  // 탭 전환/스크롤이 끝난 뒤 위치를 재야 스포트라이트가 실제 요소 자리에 맞는다.
  // requestAnimationFrame은 탭이 백그라운드/비포커스 상태면 아예 멈출 수 있어서
  // (실제로 확인됨) 대신 setTimeout을 쓴다 — 느려질 순 있어도 항상 실행은 된다.
  setTimeout(()=>positionOnboardingSpotlight(step), 0);
}

function positionOnboardingSpotlight(step){
  const target = document.querySelector(step.selector);
  const spot = $('#onboardSpotlight');
  const tip = $('#onboardTooltip');
  if(!target || !spot || !tip) return;
  // 'smooth'는 애니메이션이 언제 끝날지 알 수 없어 고정 delay와 경합이 생긴다
  // (실제로 260ms 뒤에도 스크롤이 거의 안 끝나 있는 걸 확인함) — 즉시 이동시켜서
  // 그 문제 자체를 없앤다.
  target.scrollIntoView({block: 'center', behavior: 'instant'});

  setTimeout(()=>{
    const r = target.getBoundingClientRect();
    const pad = 8;
    spot.style.left = (r.left - pad) + 'px';
    spot.style.top = (r.top - pad) + 'px';
    spot.style.width = (r.width + pad * 2) + 'px';
    spot.style.height = (r.height + pad * 2) + 'px';

    const tipRect = tip.getBoundingClientRect();
    let top = r.bottom + 16;
    if(top + tipRect.height > window.innerHeight - 8) top = r.top - tipRect.height - 16;
    if(top < 8) top = 8;
    let left = r.left;
    if(left + tipRect.width > window.innerWidth - 8) left = window.innerWidth - tipRect.width - 8;
    if(left < 8) left = 8;
    tip.style.top = top + 'px';
    tip.style.left = left + 'px';
  }, 60);
}

async function endOnboardingTour(){
  onboardActive = false;
  $('#onboardSpotlight')?.remove();
  $('#onboardTooltip')?.remove();
  if(currentUserState){
    currentUserState.onboarding_done = true;
    try{ await api('/api/me/onboarding-complete', {method: 'POST', body: '{}'}); }catch(err){ /* ignore */ }
  }
}
