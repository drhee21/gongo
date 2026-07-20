# 공고모아 — 정부지원사업 통합 조회

기존 v1/v2를 고치는 방식이 아니라, 처음부터 다시 만든 로컬 웹앱입니다.
Windows PowerShell에서 바로 실행되도록 설계했습니다.

## 핵심 구조

- `server.py` : 로컬 웹서버 + API 서버
- `collector.py` : K-스타트업 HTML, 기업마당, 기관 게시판 수집기
- `database.py` : SQLite 저장소
- `auth.py` : 비밀번호 해시, 사용자 Claude API 키 암호화, 세션 토큰
- `ai_match.py` : Claude API로 회사 맞춤 공고 적합도 판정 (사용자 본인 키 사용)
- `uploads.py` : 회사 문서(PDF/DOCX/TXT/MD) 업로드 파싱, 텍스트 추출
- `static/` : 화면 UI
- `data/gongo.sqlite` : 실행 후 자동 생성되는 DB
- `config.json` : 수집 설정/API 키 (git에는 올라가지 않음, `.gitignore` 참고)

계정 가입 후 로그인하면 회사 정보/즐겨찾기/AI 분석 결과가 계정별로 분리되어 저장됩니다.
AI 맞춤 분석은 각자 자신의 Claude API 키를 "회사 정보" 화면에 등록해서 사용합니다 (관리자 키를 공유하지 않음).
기업마당(bizinfo) 공고도 마찬가지로, 조회하는 사용자가 본인의 기업마당 API 키를 등록해야 볼 수 있습니다.

## 바로 실행

```powershell
cd C:\Users\USER\Desktop\gongo_fresh_v3
powershell -ExecutionPolicy Bypass -File .\start-server-v3.ps1
```

브라우저:

```text
http://127.0.0.1:8080
```

서버를 끄려면 실행 중인 PowerShell에서 `Ctrl + C`.

## 수집 설정

`config.json`에서 필요한 소스만 `enabled: true`로 둡니다.

K-Startup은 API 키를 쓰지 않고 모집중 웹페이지 HTML을 직접 읽습니다.

```json
"kstartup": {
  "enabled": true,
  "method": "html",
  "list_url": "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do",
  "max_items": 80
}
```

기업마당은 공식 API 키가 있을 때만 켭니다.

```json
"bizinfo": {
  "enabled": true,
  "crtfcKey": "발급받은_기업마당_API키",
  "page_unit": 200
}
```

나라장터(조달청 입찰공고)도 [공공데이터포털](https://www.data.go.kr/data/15129394/openapi.do)에서 발급받은 서비스키가 있을 때만 켭니다. 전국 물품/용역 입찰공고 전체를 가져오면 수천 건씩 쏟아지므로, `keywords`에 매칭되는 공고만 걸러서 가져옵니다.

```json
"g2b": {
  "enabled": true,
  "serviceKey": "발급받은_나라장터_서비스키",
  "days": 3,
  "keywords": ["바이오", "헬스", "의료", "신약", "R&D"]
}
```

키가 없는 소스나 URL이 맞지 않는 소스는 실패로 앱 전체를 죽이지 않습니다. 수집 결과가 하나도 없으면 샘플 공고가 표시됩니다.

## 업데이트(재수집)

웹 화면의 `업데이트` 버튼을 누르거나 PowerShell에서 실행합니다.

```powershell
cd C:\Users\USER\Desktop\gongo_fresh_v3
powershell -ExecutionPolicy Bypass -File .\run-collect.ps1
```

## 관리자 기능

`ADMIN_EMAILS` 환경변수(쉼표로 구분한 이메일 목록)에 등록된 이메일로 가입/로그인하면 자동으로 관리자 권한이 부여됩니다.

- **소스 URL 재정의**: 관리자 화면 하단 "소스 URL 재정의" 패널에서 K-스타트업/한국연구재단/국가신약개발사업단/서울바이오허브/KHIDI의 수집 URL을 코드 수정·재배포 없이 교체할 수 있습니다. 사이트 주소가 바뀌었을 때 임시로 대응하는 용도이며, 비워두고 저장하면 `config.json`의 기본값으로 돌아갑니다. 사이트 URL은 그대로인데 내부 HTML 구조만 바뀐 경우는 이 기능으로 해결되지 않고 `collector.py`의 파서 수정이 필요합니다.
- **수집 이상감지**: 매 수집마다 소스별 건수 이력을 남겨서, 최근 평균 대비 이번 건수가 0이거나 크게 급감하면 관리자 화면 소스 상태 표에 "⚠ 이상감지" 배지가 뜹니다. 진짜 공고가 없는 것인지 스크래핑이 깨진 것인지 구분하는 용도입니다.

## 회사 문서 기반 AI 매칭

"회사 정보" 화면에서 회사소개서·인증서·실적자료 등을 업로드하면(PDF/DOCX/TXT/MD, 파일당 8MB, 계정당 최대 10개) 텍스트를 추출해 DB에 저장하고, AI 맞춤 분석 시 6개 기본 필드보다 훨씬 풍부한 근거로 판단합니다. 원본 파일은 저장하지 않고 추출된 텍스트만 남습니다. 문서가 많아져도 청크마다 반복 과금되지 않도록 Claude 프롬프트 캐싱(`cache_control`)을 적용해뒀습니다.

## 상태 확인

서버가 켜져 있을 때 새 PowerShell에서:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/health
Invoke-RestMethod -Method Post http://127.0.0.1:8080/api/recollect | ConvertTo-Json -Depth 10
```

## v3에서 고친 문제

1. `gongo_feed.json` 경로 문제 제거: 이제 SQLite 하나만 사용합니다.
2. `collect.py` subprocess 인코딩 문제 제거: 서버가 collector를 직접 import해서 실행합니다.
3. 일부 소스 실패가 전체 실패로 처리되는 문제 제거.
4. API 키가 없을 때도 앱은 뜨고, 샘플 데이터로 확인 가능합니다.
5. 관심 공고는 localStorage가 아니라 SQLite에 저장됩니다.
6. CSV 내보내기를 기본 제공합니다.

## 주의

- `config.json`은 실제 API 키가 들어갈 수 있으므로 GitHub에 올리지 마세요 (`.gitignore`에 이미 포함).
- NRF/KDDF 같은 게시판 수집은 robots.txt 또는 게시판 구조에 따라 막힐 수 있습니다.
- 서울바이오허브/KHIDI는 우선 기업마당 API 결과를 키워드로 라우팅하는 방식이 안정적입니다.
- 사용자 비밀번호는 해시로만 저장되고, 사용자가 등록한 Claude API 키는 `APP_SECRET_KEY`로 암호화되어 저장됩니다. 화면에 다시 표시되지 않습니다.

## 배포 (Railway / Render 등 관리형 플랫폼)

1. **Git 저장소 준비**
   ```powershell
   git init
   git add .
   git commit -m "Initial commit"
   ```
   `config.json`과 `data/`는 `.gitignore`에 있어서 커밋에 포함되지 않습니다. 커밋 후 `git status`로 실제 API 키나 `data/gongo.sqlite`가 올라가지 않았는지 한 번 확인하세요.
2. GitHub에 새 저장소를 만들고 위 로컬 저장소를 push합니다.
3. Railway/Render에서 "새 프로젝트 → GitHub 저장소 연결"로 이 저장소를 선택합니다.
4. **환경변수** 설정 (플랫폼의 Variables/Environment 설정 화면에서):
   - `APP_SECRET_KEY` — 사용자 API 키 암호화에 쓰는 비밀키. 아래 명령으로 한 번 생성해서 그대로 붙여넣으세요.
     ```powershell
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```
     이 값을 잃어버리면 이미 저장된 사용자들의 API 키를 복호화할 수 없게 되니, 별도로 안전하게 보관하세요.
   - `BIZINFO_API_KEY` — 기업마당 API 키를 쓰려면 설정 (선택).
   - `G2B_API_KEY` — 나라장터(조달청) 서비스키를 쓰려면 설정 (선택).
   - `ADMIN_EMAILS` — 관리자 권한을 자동으로 부여할 이메일 목록, 쉼표로 구분 (선택). 예: `me@example.com,teammate@example.com`
5. **퍼시스턴트 볼륨**을 `data/` 경로에 연결하세요. SQLite 파일(`data/gongo.sqlite`)이 재배포/재시작 후에도 유지되려면 반드시 필요합니다.
6. 시작 커맨드는 `Procfile`에 정의되어 있습니다: `python server.py --host 0.0.0.0 --port $PORT` — 대부분의 플랫폼이 `Procfile`을 자동 인식합니다.
7. 배포 후 도메인으로 접속해서 회원가입 → 로그인 → Claude API 키 등록까지 되는지 확인하세요.


## K-Startup HTML 직접 크롤링 버전

이 버전은 K-Startup API를 사용하지 않습니다. `collector.py`의 `collect_kstartup()`이 아래 웹페이지를 직접 요청합니다.

```text
https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do
```

HTML 텍스트 안에 반복되는 카드 구조에서 다음 값을 추출합니다.

- 지원분야: 사업화, 행사ㆍ네트워크, 시설ㆍ공간ㆍ보육 등
- 공고명
- 주관기관 추정값
- 등록일자, 시작일자, 마감일자
- 조회/상태가 섞인 원문 row text

K-Startup 사이트 구조가 바뀌면 `config.json`에서 `list_url`을 수정하거나, `collector.py`의 `parse_kstartup_html_items()` 파서를 조정하면 됩니다. 현재는 1페이지의 모집중 목록을 안정적으로 가져오는 구조입니다.

## 서울바이오허브 직접 수집기 추가 버전

이 버전은 `boards.biohub_direct`를 일반 게시판 크롤러가 아니라 전용 파서로 처리합니다.
서울바이오허브 상세 페이지 구조인 `supportManageView.do?gubun=..&seq=..`를 직접 열어 다음 값을 추출합니다.

- 공고명/프로그램명
- 신청기간·모집기간·접수기간·신청마감
- 모집대상/지원자격 기반 업력·지역·섹터 힌트
- 지원내용/지원혜택/지원규모/모집규모
- 원문 URL

### 동작 방식

1. `list_urls`에서 `supportManageView.do` 상세 링크를 먼저 찾습니다.
2. 목록 페이지가 막히거나 JS 기반이면 `seed_urls`를 보조로 사용합니다.
3. `seq_scan_enabled`가 true이면 `seq_start`~`seq_end` 범위의 최근 상세 페이지를 스캔합니다.

서울바이오허브 사이트 구조가 바뀌면 `config.json`에서 아래 값만 조정하면 됩니다.

```json
"biohub_direct": {
  "enabled": true,
  "seq_start": 700,
  "seq_end": 830,
  "gubun_list": ["14", "08", "09", "04", "03", "12"],
  "max_detail_pages": 500
}
```

수집이 너무 오래 걸리면 `seq_scan_enabled`를 `false`로 바꾸거나 `seq_start`~`seq_end` 범위를 좁히세요.

### 실행

```powershell
cd C:\Users\USER\Desktop\gongo_v3
python .\collector.py
```

서버가 켜져 있으면 관리자 화면의 **수동 재수집** 버튼으로도 동일하게 실행됩니다.
