// 앱 UI 자체(버튼/메뉴/폼/토스트/관리자 화면)의 한국어/영어 전환. 공고 자체의
// title/org/category 등 스크래핑된 내용은 번역 대상이 아니다 — 그건 그대로
// 한국어로 남는다. 빌드 도구가 없는 순수 vanilla JS 프로젝트라 별도 라이브러리
// 없이 사전(TRANSLATIONS) + 조회 함수(t)로 직접 구현한다.
const TRANSLATIONS = {
  ko: {
    pageTitle: '공고모아 — 정부지원사업 통합 조회',
    brandTitle: '공고모아',
    brandSub: '정부지원사업 통합 조회',
    navList: '공고 목록',
    navFav: '관심 공고',
    navCal: '캘린더',
    navCompany: '회사 정보',
    navAdmin: '관리자',
    loginSignup: '로그인 / 가입',
    logout: '로그아웃',
    aiFitTitle: 'AI 적합도',
    collectionStatusTitle: '수집 상태',
    langKo: '한국어',
    langEn: 'EN',

    authTitleLogin: '로그인',
    authTitleSignup: '회원가입',
    authSubmitLogin: '로그인',
    authSubmitSignup: '가입하기',
    authSwitchToSignup: '계정이 없으신가요?',
    authSwitchToLogin: '이미 계정이 있으신가요?',
    authSwitchLinkSignup: '가입하기',
    authSwitchLinkLogin: '로그인',
    emailLabel: '이메일',
    passwordLabel: '비밀번호',

    listTitle: '공고 목록',
    listSubtitle: 'K-스타트업 · 기업마당 · 서울바이오허브 · KHIDI · NRF · KDDF · 나라장터',
    btnUpdate: '업데이트',
    btnUpdating: '업데이트 중...',
    kpiTotal: '전체',
    kpiOpen: '접수중',
    kpiUrgent: 'D-7 이내',
    kpiFav: '관심',
    searchPlaceholder: '검색: 바이오, R&D, 서울, 수출...',
    optAllSources: '전체 소스',
    optStatusOpen: '접수 가능',
    optStatusAll: '전체',
    optStatusUrgent: '마감임박',
    optStatusClosed: '마감',
    optAiFitAll: '전체',

    favTitle: '관심 공고',
    favSubtitle: '별표를 누른 공고만 모아봅니다.',

    calTitle: '마감 캘린더',
    calSubtitle: '마감일 기준으로 이번 달 공고를 달력으로 보여줍니다.',
    calToday: '오늘',

    companyTitle: '회사 정보',
    companySubtitle: '공고 적합도 필터링을 위한 기본 정보입니다.',
    companyLoginRequiredText: '회사 정보 저장과 AI 맞춤 분석은 로그인 후 이용할 수 있습니다.',
    companyNameLabel: '회사명',
    companyNamePlaceholder: '예: ABC Bio',
    companyYearsLabel: '업력(년)',
    companyYearsPlaceholder: '예: 3',
    companyRegionLabel: '지역',
    companyRegionPlaceholder: '예: 서울',
    companySectorLabel: '분야',
    companySectorPlaceholder: '예: 바이오·헬스',
    companyKeywordsLabel: '키워드',
    companyKeywordsPlaceholder: '예: 신약, 의료기기, 글로벌, R&D',
    companyKeywordsHint: 'AI가 이 키워드를 참고해 공고 적합도를 판단합니다. 구체적이고 명확하게 적을수록 정확도가 높아집니다.',
    companyKeywordModeAnd: '키워드 모두 일치해야 적합 — 기본은 하나 이상 일치 시 적합',
    companyVenture: '벤처기업 인증 보유',
    companyRndCenter: '기업부설연구소 보유',
    btnSave: '저장',
    btnDeleteCompany: '회사 정보 삭제',

    companyDocsTitle: '회사 문서',
    companyDocsDesc: '회사소개서·인증서·실적자료 등을 올리면 AI 맞춤 분석이 이 내용까지 참고해서 판단합니다 (PDF, DOCX, TXT, MD · 파일당 8MB · 최대 10개).',
    fileSelectLabel: '파일 선택',
    btnUpload: '업로드',

    llmModelTitle: 'AI 모델 및 API 키',
    llmModelDesc: 'AI 맞춤 분석, 신규 수집 사이트 분석 등에 쓸 키를 여러 개 등록해두고 그중 하나를 활성으로 골라 쓸 수 있습니다. 키는 암호화되어 저장되고 화면에는 다시 표시되지 않습니다.',
    modelLabel: '모델',
    llmKeyLabel: 'LLM API 키',
    llmKeyPlaceholder: '비워두면 변경 없음',
    btnDeleteKey: '키 삭제',
    btnActivateKey: '이 키로 적용',
    llmActiveProfileLabel: '사용할 키',
    llmNoProfiles: '등록된 키가 없습니다',
    llmAddProfileTitle: '새 키 추가',
    llmProfileLabelLabel: '이름 (선택)',
    llmProfileLabelPlaceholder: '예: 개인 Anthropic 키',
    btnAddLlmProfile: '키 추가',

    bizinfoKeyTitle: '기업마당 API 키',
    bizinfoKeyDescPre: '기업마당(bizinfo) 공고는 본인의 기업마당 API 키를 등록해야 볼 수 있습니다. 키는',
    bizinfoLinkText: '기업마당',
    bizinfoKeyDescPost: '에서 무료로 발급받을 수 있습니다.',
    apiKeyLabel: 'API 키',
    bizinfoKeyPlaceholder: '발급받은 기업마당 API 키 (비워두면 변경 없음)',

    aiFitSectionTitle: 'AI 맞춤 분석',
    aiFitSectionDesc: '회사 정보를 저장한 뒤, 현재 올라와 있는 공고 전체를 등록된 AI 모델로 분석해서 공고별로 적합/확인/부적합을 판정합니다. 공고 건수에 따라 수 분 걸릴 수 있습니다.',
    btnRunAiFit: 'AI로 맞춤 공고 분석 실행',
    btnAiAnalyzing: 'AI 분석 중...',

    onboardStep: '{step} / {total}',
    onboardNext: '다음',
    onboardPrev: '이전',
    onboardSkip: '건너뛰기',
    onboardFinish: '시작하기',
    onboard1Title: '공고 목록',
    onboard1Body: '여기서 정부지원사업 공고를 검색하고 소스·상태별로 필터링할 수 있어요. 지금부터 몇 단계만 거치면 회사에 맞는 공고만 골라볼 수 있게 설정할 수 있습니다.',
    onboard2Title: '회사 정보 입력',
    onboard2Body: '회사명, 분야, 키워드 등을 입력해두면 AI가 이 정보를 바탕으로 공고 적합도를 판단합니다. 구체적으로 적을수록 정확도가 올라가요.',
    onboard3Title: 'AI API 키 등록',
    onboard3Body: 'AI 맞춤 분석을 실행하려면 사용할 모델의 API 키가 필요합니다. 위에서 모델을 고르고 여기에 키를 등록해주세요.',
    onboard4Title: 'AI 맞춤 분석 실행',
    onboard4Body: '회사 정보와 API 키를 등록했다면 이 버튼으로 전체 공고를 분석할 수 있어요. 공고 건수에 따라 몇 분 걸릴 수 있습니다.',
    onboard5Title: '적합도로 필터링',
    onboard5Body: '분석이 끝나면 여기서 적합/부적합/확인 중 하나로 공고를 걸러볼 수 있습니다. 이제 자유롭게 둘러보세요!',

    adminTitle: '관리자',
    adminSubtitle: '수집 상태와 API 오류를 확인합니다.',
    sourceStatusTitle: '소스 상태',

    schedulerTitle: '자동 수집 일정 (관리자)',
    schedulerDesc: '서버가 계속 실행 중인 동안, 지정한 시간/주기에 맞춰 자동으로 공고를 수집합니다.',
    schedulerEnabledLabel: '자동 수집 사용',
    schedulerModeDaily: '매일 지정 시간에',
    schedulerModeInterval: '일정 시간마다',
    timeLabel: '시간',
    dayMon: '월', dayTue: '화', dayWed: '수', dayThu: '목', dayFri: '금', daySat: '토', daySun: '일',
    intervalHoursLabel: '주기(시간)',
    schedulerInUse: '사용 중',
    schedulerNotInUse: '사용 안 함',
    schedulerLastRun: '마지막 자동 수집: {v}',
    schedulerNextRun: '다음 예정: {v}',
    toastSchedulerSaved: '자동 수집 일정 저장 완료',

    adminSourcesTitle: '소스 관리 (관리자)',
    adminSourcesDesc: '이름과 공고 목록 페이지 URL만 넣으면 별도 코드 작성 없이 AI가 자동으로 수집 방법을 찾아냅니다. 미리보기를 확인하고 확정해야 실제 수집에 반영됩니다. 기존 소스는 아래 목록에서 URL을 바로 재정의할 수 있고, 커스텀 소스는 이름/URL 수정과 활성화·삭제까지 가능합니다.',
    siteNamePlaceholder: '사이트 이름',
    siteUrlPlaceholder: '공고 목록 페이지 URL (https://...)',
    allSourcesListTitle: '전체 소스 목록',

    // 서버가 내려주는 상태/방식 문자열 → 화면 표시용 (비교 로직은 원래 한국어 값을
    // 그대로 쓰고, 화면에 그릴 때만 이 표를 거친다)
    statusNormal: '정상',
    statusClosed: '마감',
    statusOpen: '접수중',
    statusUpcoming: '접수예정',
    statusRolling: '상시',
    statusUnknownDate: '날짜 미상',
    anomalyDetected: '이상감지',
    statusBlockedRobots: '차단(robots)',
    statusDisabled: '비활성화',
    disabledBadge: '비활성',
    statusError: '오류',
    statusUnknown: '미확인',
    statusWaiting: '대기',
    statusZero: '0건',
    statusRecoveredViaRecipe: '레시피로 복구됨',
    statusNoRecipe: '레시피 없음',
    badgeExistingSource: '기존 소스',
    aiFitFit: '적합',
    aiFitUnfit: '부적합',
    aiFitUnsure: '확인',
    statusCheck: '확인',

    detailToggle: '상세',
    budgetLabel: '지원규모',
    eligLabel: '신청자격',
    sourceLinkLabel: '원문',
    aiReasonTag: 'AI 판정 근거',
    eligNoLimit: '제한 없음/확인 필요',
    eligYearsRange: '업력 {min}~{max}년',
    eligYearsMax: '업력 {max}년 이내',
    eligYearsMin: '업력 {min}년 이상',
    eligRegion: '지역 {regions}',
    budgetDefault: '공고 참조',
    ddayN: 'D-{n}',

    paginationShowing: '{from}-{to} / 전체 {total}건',
    paginationPrev: '이전',
    paginationNext: '다음',

    emptyNoticesFiltered: '조건에 맞는 공고가 없습니다.',
    emptyFavLoggedIn: '관심 공고가 없습니다.',
    emptyFavLoggedOut: '로그인하면 관심 공고를 저장할 수 있습니다.',
    emptyCalendarMonth: '이번 달 마감인 공고가 없습니다.',
    emptyBeforeCollect: '아직 수집 전',
    emptySources: '소스가 없습니다.',
    emptyDocs: '등록된 문서가 없습니다.',

    colId: 'ID', colSource: '소스', colState: '상태', colCount: '수집 건수', colDisplayedCount: '표시 건수', colError: '오류',
    previewColTitle: '제목', previewColOrg: '기관', previewColPeriod: '기간', previewColUrl: 'URL',

    btnEdit: '수정',
    btnDisable: '비활성화',
    btnEnable: '활성화',
    btnDelete: '삭제',
    btnCancel: '취소',
    overrideNotice: '(기본값 재정의됨)',
    noneValue: '(없음)',
    detailFetchBadgeTitle: '목록 페이지만으로는 부족해서 항목마다 상세 페이지를 추가로 가져옵니다 — 수집이 더 오래 걸립니다',
    detailFetchBadgeText: '🔎 상세 페이지 조회',
    anomalyBadgeText: '⚠ 이상감지',
    editHintOverride: '이 소스는 아직 전용 수집기를 사용합니다 — 이름/URL을 바꾸고 저장하면 재발견 없이 바로 적용됩니다.',
    editHintCustom: 'URL을 바꾸면 다시 미리보기를 거쳐야 합니다.',
    editingBanner: '"{name}" 수정 중 — {hint}',
    cancelLink: '취소',
    confirmRemoveCustomSource: '정말 삭제하시겠습니까? 수집된 공고도 함께 삭제됩니다(즐겨찾기한 공고는 남습니다).',
    previewFoundCount: '— 총 {n}건 발견',
    recipeDetailsSummary: '레시피 원본 보기',
    btnConfirmEdit: '확정 (변경사항 반영)',
    btnConfirmNew: '확정 (수집에 반영)',
    btnAnalyzingRecipe: '분석 중... (최대 1~2분)',

    toastEnterNameUrl: '이름과 URL을 모두 입력해주세요.',
    toastEnabled: '활성화 완료',
    toastDisabled: '비활성화 완료',
    toastEditSaved: '수정 내용 저장 완료',
    toastNameUpdated: '이름 수정 완료',
    toastPreviewReady: '미리보기 준비 완료 — 확인 후 확정해주세요',
    toastDeleted: '삭제 완료',
    toastRegistered: '등록 완료 — 이후부터 자동으로 수집됩니다',
    toastEditConfirmed: '수정 완료',

    toastUpdateDone: '업데이트 완료: {n}건',
    toastUpdateFailed: '업데이트 실패: {msg}',

    toastCompanySaved: '회사 정보 저장 완료',
    confirmDeleteCompany: '저장된 회사 정보를 삭제할까요?',
    toastCompanyDeleted: '회사 정보 삭제 완료',

    toastAiFitDone: 'AI 분석 완료: 총 {n}건',
    toastAiFitFailed: 'AI 분석 실패: {msg}',

    toastLlmSaved: 'AI 모델 설정 저장 완료',
    confirmDeleteApiKey: '등록된 API 키를 삭제할까요?',
    toastApiKeyDeleted: 'API 키 삭제 완료',
    apiKeyRegistered: 'API 키가 등록되어 있습니다.',
    apiKeyNotRegistered: '등록된 API 키가 없습니다.',
    toastLlmProfileAdded: '키 추가 및 활성화 완료',
    toastLlmProfileAddedInactive: '키 추가 완료 — 사용하려면 드롭다운에서 고른 뒤 "이 키로 적용"을 눌러주세요',
    toastLlmProfileActivated: '활성 키 변경 완료',
    toastLlmProfileDeleted: '키 삭제 완료',
    confirmDeleteLlmProfile: '이 키를 삭제할까요?',

    confirmDeleteBizinfoKey: '등록된 기업마당 API 키를 삭제할까요?',
    toastBizinfoSaved: '기업마당 API 키 저장 완료',
    toastBizinfoDeleted: '기업마당 API 키 삭제 완료',
    toastBizinfoNoChange: '입력한 내용이 없어 변경하지 않았습니다',
    bizinfoKeyRegistered: '기업마당 API 키가 등록되어 있습니다.',
    bizinfoKeyNotRegistered: '등록된 기업마당 API 키가 없습니다 — 기업마당 공고가 목록에서 보이지 않습니다.',

    docCharCount: '{n}자',
    toastNoFileSelected: '업로드할 파일을 선택해주세요.',
    toastDocUploaded: '문서 업로드 완료',

    serverConnFailed: '서버 연결 실패',
    serverConnFailedHint: 'PowerShell에서 python server.py 또는 start-server-v3.ps1로 실행했는지 확인하세요.',
    jsonParseError: 'JSON 응답 아님',

    srcBizinfo: '기업마당', srcKstartup: 'K-스타트업', srcBiohub: '서울바이오허브',
    srcKhidi: '보건산업진흥원/KHIDI', srcKddf: '국가신약개발사업단', srcNrf: '한국연구재단',
    srcG2b: '나라장터', srcSample: '샘플',
  },
  en: {
    pageTitle: 'GongoMoa — Government Support Program Search',
    brandTitle: 'GongoMoa',
    brandSub: 'Government Grant Search',
    navList: 'Notices',
    navFav: 'Favorites',
    navCal: 'Calendar',
    navCompany: 'Company Info',
    navAdmin: 'Admin',
    loginSignup: 'Log In / Sign Up',
    logout: 'Log Out',
    aiFitTitle: 'AI Fit',
    collectionStatusTitle: 'Collection Status',
    langKo: '한국어',
    langEn: 'EN',

    authTitleLogin: 'Log In',
    authTitleSignup: 'Sign Up',
    authSubmitLogin: 'Log In',
    authSubmitSignup: 'Sign Up',
    authSwitchToSignup: "Don't have an account?",
    authSwitchToLogin: 'Already have an account?',
    authSwitchLinkSignup: 'Sign up',
    authSwitchLinkLogin: 'Log in',
    emailLabel: 'Email',
    passwordLabel: 'Password',

    listTitle: 'Notices',
    listSubtitle: 'K-Startup · Bizinfo · Seoul Bio Hub · KHIDI · NRF · KDDF · G2B',
    btnUpdate: 'Update',
    btnUpdating: 'Updating...',
    kpiTotal: 'Total',
    kpiOpen: 'Open',
    kpiUrgent: 'Within D-7',
    kpiFav: 'Favorites',
    searchPlaceholder: 'Search: Bio, R&D, Seoul, Export...',
    optAllSources: 'All Sources',
    optStatusOpen: 'Open',
    optStatusAll: 'All',
    optStatusUrgent: 'Closing Soon',
    optStatusClosed: 'Closed',
    optAiFitAll: 'All',

    favTitle: 'Favorites',
    favSubtitle: "Collects only the notices you've starred.",

    calTitle: 'Deadline Calendar',
    calSubtitle: "Shows this month's notices by deadline on a calendar.",
    calToday: 'Today',

    companyTitle: 'Company Info',
    companySubtitle: 'Basic info used to filter notice fit.',
    companyLoginRequiredText: 'Saving company info and running AI fit analysis require logging in.',
    companyNameLabel: 'Company Name',
    companyNamePlaceholder: 'e.g. ABC Bio',
    companyYearsLabel: 'Years in Business',
    companyYearsPlaceholder: 'e.g. 3',
    companyRegionLabel: 'Region',
    companyRegionPlaceholder: 'e.g. Seoul',
    companySectorLabel: 'Sector',
    companySectorPlaceholder: 'e.g. Bio·Health',
    companyKeywordsLabel: 'Keywords',
    companyKeywordsPlaceholder: 'e.g. New Drug, Medical Device, Global, R&D',
    companyKeywordsHint: 'AI uses these keywords to judge notice fit. The more specific and clear, the more accurate.',
    companyKeywordModeAnd: 'All keywords must match to count as fit — by default, one match is enough',
    companyVenture: 'Holds Venture Company Certification',
    companyRndCenter: 'Has an In-house R&D Center',
    btnSave: 'Save',
    btnDeleteCompany: 'Delete Company Info',

    companyDocsTitle: 'Company Documents',
    companyDocsDesc: "Upload company profiles, certificates, track records, etc. — AI fit analysis will factor these in too (PDF, DOCX, TXT, MD · 8MB per file · up to 10 files).",
    fileSelectLabel: 'Select Files',
    btnUpload: 'Upload',

    llmModelTitle: 'AI Model & API Key',
    llmModelDesc: 'Register multiple keys for AI fit analysis, new source discovery, and other AI features, and pick one as active. Keys are stored encrypted and never shown again.',
    modelLabel: 'Model',
    llmKeyLabel: 'LLM API Key',
    llmKeyPlaceholder: 'Leave blank to keep unchanged',
    btnDeleteKey: 'Delete Key',
    btnActivateKey: 'Set Key',
    llmActiveProfileLabel: 'Active key',
    llmNoProfiles: 'No keys registered',
    llmAddProfileTitle: 'Add a key',
    llmProfileLabelLabel: 'Name (optional)',
    llmProfileLabelPlaceholder: 'e.g. Personal Anthropic key',
    btnAddLlmProfile: 'Add Key',

    bizinfoKeyTitle: 'Bizinfo API Key',
    bizinfoKeyDescPre: 'Bizinfo notices require registering your own Bizinfo API key to view. You can get a key for free from',
    bizinfoLinkText: 'Bizinfo',
    bizinfoKeyDescPost: '.',
    apiKeyLabel: 'API Key',
    bizinfoKeyPlaceholder: 'Your Bizinfo API key (leave blank to keep unchanged)',

    aiFitSectionTitle: 'AI Fit Analysis',
    aiFitSectionDesc: 'After saving company info, this analyzes every current notice using your registered AI model and judges each as fit/unsure/unfit. May take a few minutes depending on notice count.',
    btnRunAiFit: 'Run AI Fit Analysis',
    btnAiAnalyzing: 'Analyzing with AI...',

    onboardStep: '{step} of {total}',
    onboardNext: 'Next',
    onboardPrev: 'Back',
    onboardSkip: 'Skip',
    onboardFinish: 'Get started',
    onboard1Title: 'Notice list',
    onboard1Body: "This is where you search and filter government support-program notices by source and status. A few quick steps and you'll have this set up to show only notices relevant to your company.",
    onboard2Title: 'Enter company info',
    onboard2Body: 'Fill in your company name, sector, and keywords — the AI uses this to judge how well each notice fits. The more specific you are, the more accurate the matching.',
    onboard3Title: 'Register an AI API key',
    onboard3Body: 'Running AI fit analysis needs an API key for whichever model you pick. Choose a model above, then register your key here.',
    onboard4Title: 'Run AI fit analysis',
    onboard4Body: "Once your company info and API key are set, use this button to analyze every notice. It may take a few minutes depending on how many there are.",
    onboard5Title: 'Filter by fit',
    onboard5Body: "Once the analysis finishes, filter notices here by fit, unfit, or unsure. You're all set — explore away!",

    adminTitle: 'Admin',
    adminSubtitle: 'Check collection status and API errors.',
    sourceStatusTitle: 'Source Status',

    schedulerTitle: 'Automatic Collection Schedule (Admin)',
    schedulerDesc: 'While the server keeps running, notices are collected automatically at the time/interval you set.',
    schedulerEnabledLabel: 'Enable Automatic Collection',
    schedulerModeDaily: 'Daily at a set time',
    schedulerModeInterval: 'Every set interval',
    timeLabel: 'Time',
    dayMon: 'Mon', dayTue: 'Tue', dayWed: 'Wed', dayThu: 'Thu', dayFri: 'Fri', daySat: 'Sat', daySun: 'Sun',
    intervalHoursLabel: 'Interval (hours)',
    schedulerInUse: 'Enabled',
    schedulerNotInUse: 'Disabled',
    schedulerLastRun: 'Last automatic run: {v}',
    schedulerNextRun: 'Next scheduled: {v}',
    toastSchedulerSaved: 'Schedule saved',

    adminSourcesTitle: 'Source Management (Admin)',
    adminSourcesDesc: "Just enter a name and the notice list page URL — AI figures out how to collect it automatically, no code needed. Review the preview and confirm before it takes effect. Built-in sources can have their URL overridden directly below; custom sources can also be renamed, re-pointed, enabled/disabled, and deleted.",
    siteNamePlaceholder: 'Site Name',
    siteUrlPlaceholder: 'Notice list page URL (https://...)',
    allSourcesListTitle: 'All Sources',

    statusNormal: 'Normal',
    statusClosed: 'Closed',
    statusOpen: 'Open',
    statusUpcoming: 'Upcoming',
    statusRolling: 'Rolling',
    statusUnknownDate: 'Date Unknown',
    anomalyDetected: 'Anomaly',
    statusBlockedRobots: 'Blocked (robots.txt)',
    statusDisabled: 'Disabled',
    disabledBadge: 'Disabled',
    statusError: 'Error',
    statusUnknown: 'Unknown',
    statusWaiting: 'Waiting',
    statusZero: '0 items',
    statusRecoveredViaRecipe: 'Recovered via Recipe',
    statusNoRecipe: 'No Recipe',
    badgeExistingSource: 'Built-in',
    aiFitFit: 'Fit',
    aiFitUnfit: 'Unfit',
    aiFitUnsure: 'Unsure',
    statusCheck: 'Check',

    detailToggle: 'Details',
    budgetLabel: 'Support Scale',
    eligLabel: 'Eligibility',
    sourceLinkLabel: 'Source',
    aiReasonTag: 'AI Reasoning',
    eligNoLimit: 'No limit / needs check',
    eligYearsRange: '{min}–{max} years in business',
    eligYearsMax: 'Under {max} years in business',
    eligYearsMin: 'Over {min} years in business',
    eligRegion: 'Region: {regions}',
    budgetDefault: 'See notice',
    ddayN: 'D-{n}',

    paginationShowing: 'Showing {from}-{to} of {total}',
    paginationPrev: 'Prev',
    paginationNext: 'Next',

    emptyNoticesFiltered: 'No notices match your filters.',
    emptyFavLoggedIn: 'No favorites yet.',
    emptyFavLoggedOut: 'Log in to save favorites.',
    emptyCalendarMonth: 'No notices due this month.',
    emptyBeforeCollect: 'Not collected yet',
    emptySources: 'No sources.',
    emptyDocs: 'No documents uploaded.',

    colId: 'ID', colSource: 'Source', colState: 'State', colCount: 'Fetched', colDisplayedCount: 'Displayed', colError: 'Error',
    previewColTitle: 'Title', previewColOrg: 'Org', previewColPeriod: 'Period', previewColUrl: 'URL',

    btnEdit: 'Edit',
    btnDisable: 'Disable',
    btnEnable: 'Enable',
    btnDelete: 'Delete',
    btnCancel: 'Cancel',
    overrideNotice: '(default overridden)',
    noneValue: '(none)',
    detailFetchBadgeTitle: "The list page alone isn't enough, so each item's detail page is fetched too — collection takes longer",
    detailFetchBadgeText: '🔎 Detail Page Lookup',
    anomalyBadgeText: '⚠ Anomaly',
    editHintOverride: 'This source still uses a dedicated collector — changing the name/URL and saving applies instantly, no rediscovery.',
    editHintCustom: 'Changing the URL requires going through preview again.',
    editingBanner: 'Editing "{name}" — {hint}',
    cancelLink: 'Cancel',
    confirmRemoveCustomSource: 'Are you sure you want to delete this? Its collected notices will be deleted too (favorited ones are kept).',
    previewFoundCount: '— {n} found',
    recipeDetailsSummary: 'View raw recipe',
    btnConfirmEdit: 'Confirm (apply changes)',
    btnConfirmNew: 'Confirm (add to collection)',
    btnAnalyzingRecipe: 'Analyzing... (up to 1–2 min)',

    toastEnterNameUrl: 'Please enter both a name and a URL.',
    toastEnabled: 'Enabled',
    toastDisabled: 'Disabled',
    toastEditSaved: 'Changes saved',
    toastNameUpdated: 'Name updated',
    toastPreviewReady: 'Preview ready — please review and confirm',
    toastDeleted: 'Deleted',
    toastRegistered: 'Registered — will be collected automatically from now on',
    toastEditConfirmed: 'Update confirmed',

    toastUpdateDone: 'Update complete: {n} items',
    toastUpdateFailed: 'Update failed: {msg}',

    toastCompanySaved: 'Company info saved',
    confirmDeleteCompany: 'Delete your saved company info?',
    toastCompanyDeleted: 'Company info deleted',

    toastAiFitDone: 'AI analysis complete: {n} total',
    toastAiFitFailed: 'AI analysis failed: {msg}',

    toastLlmSaved: 'AI model settings saved',
    confirmDeleteApiKey: 'Delete the registered API key?',
    toastApiKeyDeleted: 'API key deleted',
    apiKeyRegistered: 'An API key is registered.',
    apiKeyNotRegistered: 'No API key registered.',
    toastLlmProfileAdded: 'Key added and activated',
    toastLlmProfileAddedInactive: 'Key added — select it above and click "Set Key" to use it',
    toastLlmProfileActivated: 'Active key changed',
    toastLlmProfileDeleted: 'Key deleted',
    confirmDeleteLlmProfile: 'Delete this key?',

    confirmDeleteBizinfoKey: 'Delete the registered Bizinfo API key?',
    toastBizinfoSaved: 'Bizinfo API key saved',
    toastBizinfoDeleted: 'Bizinfo API key deleted',
    toastBizinfoNoChange: 'Nothing entered, so nothing was changed',
    bizinfoKeyRegistered: 'A Bizinfo API key is registered.',
    bizinfoKeyNotRegistered: "No Bizinfo API key registered — Bizinfo notices won't appear in the list.",

    docCharCount: '{n} chars',
    toastNoFileSelected: 'Please select a file to upload.',
    toastDocUploaded: 'Document uploaded',

    serverConnFailed: 'Server connection failed',
    serverConnFailedHint: 'Check that you ran python server.py (or start-server-v3.ps1) in PowerShell.',
    jsonParseError: 'Not a JSON response',

    srcBizinfo: 'Bizinfo', srcKstartup: 'K-Startup', srcBiohub: 'Seoul Bio Hub',
    srcKhidi: 'KHIDI', srcKddf: 'KDDF', srcNrf: 'NRF',
    srcG2b: 'G2B', srcSample: 'Sample',
  },
};

// 서버가 내려주는 상태값(한국어 문자열 그대로)을 화면 표시용 키로 매핑한다.
// 비교 로직(statusClass, ddayText 등)은 이 표와 무관하게 원래 한국어 값을 그대로
// 비교해야 한다 — 이 표는 오직 "화면에 뭐라고 그릴지"에만 쓴다.
const STATUS_LABELS = {
  '정상': 'statusNormal', '마감': 'statusClosed', '접수중': 'statusOpen', '접수예정': 'statusUpcoming', '상시': 'statusRolling',
  '날짜 미상': 'statusUnknownDate', '차단(robots)': 'statusBlockedRobots', '비활성화': 'statusDisabled',
  '비활성': 'disabledBadge', '오류': 'statusError', '미확인': 'statusUnknown',
  '대기': 'statusWaiting', '0건': 'statusZero', '레시피로 복구됨': 'statusRecoveredViaRecipe',
  '레시피 없음': 'statusNoRecipe', '기존 소스': 'badgeExistingSource', '확인': 'statusCheck',
};

let currentLang = localStorage.getItem('lang') || 'ko';

function t(key, vars) {
  let s = (TRANSLATIONS[currentLang] || TRANSLATIONS.ko)[key];
  if (s == null) s = TRANSLATIONS.ko[key] ?? key;
  if (vars) for (const k in vars) s = s.replaceAll(`{${k}}`, vars[k]);
  return s;
}

// 서버가 내려준 상태 문자열(예: "정상")을 화면 표시용으로 번역한다. 목록에 없는
// 값(예: 커스텀 문자열, 이미 영어인 값)은 그대로 돌려준다.
function tStatus(koValue) {
  const key = STATUS_LABELS[koValue];
  return key ? t(key) : koValue;
}

// "공고 건수" 표시(캘린더 날짜별 개수, 소스별 수집 건수). 영어는 단/복수 어미가
// 붙어야 자연스러워서 단순 템플릿 치환({n}건) 대신 별도 함수로 처리한다.
function countText(n) {
  if (currentLang === 'en') return `${n} notice${n === 1 ? '' : 's'}`;
  return `${n}건`;
}

function applyStaticTranslations() {
  document.documentElement.lang = currentLang;
  document.title = t('pageTitle');
  // 진행 중(예: "업데이트 중...")인 버튼은 data-i18n-busy에 그 상태의 번역 키를
  // 담아둔다 — 언어를 전환해도 원래 라벨("업데이트")로 되돌아가지 않고 진행 중
  // 라벨이 새 언어로 다시 번역되게 하기 위해서다 (data-i18n 자체를 덮어써버리면
  // 완료 후 어떤 라벨로 되돌려야 할지 알 수 없어져서, 원래 키는 그대로 두고
  // 별도 속성으로 진행 상태만 표시한다).
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18nBusy || el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  document.querySelectorAll('[data-lang-btn]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.langBtn === currentLang);
  });
}

function setLang(lang) {
  if (lang !== 'ko' && lang !== 'en') return;
  currentLang = lang;
  localStorage.setItem('lang', lang);
  applyStaticTranslations();
  document.dispatchEvent(new CustomEvent('langchange'));
}
