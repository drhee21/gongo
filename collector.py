# -*- coding: utf-8 -*-
"""공고모아 수집기.

Design principles:
- A single source failure must never kill the whole collection.
- Missing API keys are treated as "skipped", not fatal errors.
- Output goes into SQLite directly, so the web server and collector always see the same data.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import database

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SAMPLE_PATH = ROOT / "data" / "sample_notices.json"
UA = "GongoMoa/3.0 (internal notice aggregator; contact: local)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})

SOURCE_CATALOG: Dict[str, Dict[str, str]] = {
    "kstartup": {"name": "K-스타트업", "method": "HTML"},
    "bizinfo": {"name": "기업마당", "method": "API"},
    "biohub": {"name": "서울바이오허브", "method": "기업마당 경유"},
    "khidi": {"name": "보산원/KHIDI", "method": "기업마당 경유"},
    "nrf": {"name": "한국연구재단", "method": "게시판"},
    "kddf": {"name": "국가신약개발사업단", "method": "게시판"},
    "sample": {"name": "샘플", "method": "내장 데이터"},
    "biohub_direct": {"name": "서울바이오허브", "method": "전용 파서"},
    "khidi_direct": {"name": "보건산업진흥원/KHIDI", "method": "게시판"},
    "g2b": {"name": "나라장터", "method": "API"},
}

DATE_SEP = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
DATE_COMPACT = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
YEAR_LIMIT = re.compile(r"(\d+)\s*년\s*(미만|이내|이하)")


def is_blank_key(value: Any) -> bool:
    if not value:
        return True
    s = str(value).strip()
    return (not s) or ("여기에" in s) or ("YOUR_" in s.upper()) or s.upper() in {"TODO", "NONE", "NULL"}


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        example = ROOT / "config.example.json"
        if example.exists():
            CONFIG_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            raise FileNotFoundError("config.json이 없습니다.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def valid_ymd(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def parse_date(text: Any, prefer_last: bool = False) -> Optional[str]:
    if not text:
        return None
    s = str(text)
    found: List[str] = []
    for m in DATE_SEP.finditer(s):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if valid_ymd(y, mo, d):
            found.append(f"{y:04d}-{mo:02d}-{d:02d}")
    for m in DATE_COMPACT.finditer(s):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if valid_ymd(y, mo, d):
            found.append(f"{y:04d}-{mo:02d}-{d:02d}")
    if not found:
        return None
    return found[-1] if prefer_last else found[0]


def parse_period(text: Any) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None
    t = str(text)
    if re.search(r"상시|수시|소진|예산\s*소진", t):
        return None, None
    parts = re.split(r"[~∼〜–]|(?<=\d)\s+-\s+(?=20\d{2})", t)
    if len(parts) >= 2:
        start = parse_date(parts[0], prefer_last=True)
        end = parse_date(parts[1])
        # 물결(~) 바로 뒤가 '7.31.'처럼 연도 없는 짧은 날짜이면 그걸 우선한다.
        # 그렇지 않으면 뒷부분 텍스트에 섞인 다른 날짜(게시일 등)를 마감일로 잘못 집는 경우가 있다.
        m_short = re.match(r"\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*[.일]?", parts[1])
        if m_short:
            mo, d = int(m_short.group(1)), int(m_short.group(2))
            base_year = int(start[:4]) if start else date.today().year
            if valid_ymd(base_year, mo, d):
                end = f"{base_year:04d}-{mo:02d}-{d:02d}"
        if start and not end:
            m = re.search(r"(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", parts[1])
            if m:
                mo, d = int(m.group(1)), int(m.group(2))
                if valid_ymd(int(start[:4]), mo, d):
                    end = f"{start[:4]}-{mo:02d}-{d:02d}"
        return start, end
    return None, parse_date(t)


def pick(d: Dict[str, Any], *keys: str) -> Optional[Any]:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def make_id(url: str, title: str) -> str:
    base = (url or "") + "|" + (title or "")
    return "n" + hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:12]


def clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def elig_from_text(*texts: Any) -> Optional[Dict[str, Any]]:
    joined = " ".join(clean(t) for t in texts if t)
    elig: Dict[str, Any] = {}
    years = [int(m.group(1)) for m in YEAR_LIMIT.finditer(joined)]
    if years:
        elig["maxYears"] = max(years)
    if "예비창업" in joined:
        elig.setdefault("maxYears", 0)
    regions = []
    for r in ["서울", "경기", "인천", "대전", "대구", "부산", "광주", "울산", "세종", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]:
        if r in joined and r not in regions:
            regions.append(r)
    if regions and "전국" not in joined:
        elig["regions"] = regions
    sectors = []
    sector_map = {
        "바이오·헬스": ["바이오", "의료", "헬스", "제약", "신약", "의약"],
        "뷰티·에스테틱": ["뷰티", "화장품", "에스테틱", "미용"],
        "AI·SW": ["AI", "인공지능", "소프트웨어", "SW", "플랫폼", "데이터"],
        "제조": ["제조", "소부장", "스마트공장"],
        "수출": ["수출", "글로벌", "해외", "인증"],
        "R&D": ["R&D", "연구개발", "기술개발", "과제"],
    }
    for sector, kws in sector_map.items():
        if any(kw in joined for kw in kws):
            sectors.append(sector)
    if sectors:
        elig["sectors"] = sectors
    return elig or None


def normalize(src: str, title: Any, org: Any, category: Any, start: Optional[str], end: Optional[str], url: Any, budget: Any = "공고 참조", elig: Optional[Dict[str, Any]] = None, raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    title_s = clean(title) or "제목 없음"
    url_s = clean(url)
    return {
        "id": make_id(url_s, title_s),
        "src": src,
        "title": title_s,
        "org": clean(org) or "기관 미표기",
        "category": clean(category) or "기타",
        "start": start or date.today().isoformat(),
        "end": end,
        "budget": clean(budget) or "공고 참조",
        "elig": elig,
        "url": url_s,
        "raw": raw or {},
    }


@dataclass
class CollectRun:
    items: List[Dict[str, Any]] = field(default_factory=list)
    sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def record(self, source_id: str, items: Optional[List[Dict[str, Any]]] = None, state: str = "정상", error: Optional[Exception | str] = None, name: Optional[str] = None, method: Optional[str] = None) -> None:
        items = items or []
        cat = SOURCE_CATALOG.get(source_id, {})
        self.sources[source_id] = {
            "name": name or cat.get("name") or source_id,
            "method": method or cat.get("method") or "",
            "state": state,
            "n": len(items),
            "last": datetime.now().isoformat(timespec="seconds"),
            "error": str(error)[:300] if error else None,
        }
        self.items.extend(items)


def collect_bizinfo(cfg: Dict[str, Any], route: Dict[str, List[str]]) -> Dict[str, List[Dict[str, Any]]]:
    if not cfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    key = cfg.get("crtfcKey")
    if is_blank_key(key):
        key = os.environ.get("BIZINFO_API_KEY")
    if is_blank_key(key):
        raise RuntimeError("기업마당 API 키가 없습니다")

    params = {
        "crtfcKey": key,
        "dataType": "json",
        "pageIndex": "1",
        "pageUnit": str(cfg.get("page_unit", 200)),
    }
    r = SESSION.get("https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do", params=params, timeout=cfg.get("timeout_sec", 20))
    r.raise_for_status()
    data = r.json()
    rows = data.get("jsonArray", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = rows.get("item", [])

    out = {"bizinfo": [], "biohub": [], "khidi": []}
    for it in rows or []:
        if not isinstance(it, dict):
            continue
        title = pick(it, "pblancNm", "title", "bizNm")
        if not title:
            continue
        start, end = parse_period(pick(it, "reqstBeginEndDe", "reqstDt", "period"))
        rel_url = clean(pick(it, "pblancUrl", "url") or "")
        url = urljoin("https://www.bizinfo.go.kr", rel_url)
        org = pick(it, "jrsdInsttNm", "excInsttNm", "insttNm")
        exc = pick(it, "excInsttNm") or ""
        cat = pick(it, "pldirSportRealmLclasCodeNm", "category", "realmNm") or "경영·기술"
        target = pick(it, "trgetNm", "target", "aplyTrgt") or ""
        haystack = f"{title} {org or ''} {exc} {pick(it, 'hashTags') or ''}"
        src = "bizinfo"
        for route_src, keywords in (route or {}).items():
            if route_src.startswith("_"):
                continue
            if any(kw and kw in haystack for kw in keywords):
                src = route_src
                break
        out.setdefault(src, []).append(normalize(src, title, org, cat, start, end, url, elig=elig_from_text(target, title, haystack), raw=it))
    return out


G2B_BASE = "http://apis.data.go.kr/1230000/ad/BidPublicInfoService"
G2B_OPERATIONS = ["getBidPblancListInfoThng", "getBidPblancListInfoServc"]  # 물품, 용역


def collect_g2b(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """나라장터(조달청) 입찰공고정보서비스에서 공고를 가져온다.

    전국 모든 입찰공고(물품/용역만 해도 하루 수백~수천 건)를 그대로 가져오면
    나머지 소스와 균형이 맞지 않으므로, `cfg["keywords"]`에 매칭되는 공고만
    남긴다. 키워드가 비어 있으면(설정 실수 방지) 아무것도 반환하지 않는다.
    """
    if not cfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    key = cfg.get("serviceKey")
    if is_blank_key(key):
        key = os.environ.get("G2B_API_KEY")
    if is_blank_key(key):
        raise RuntimeError("나라장터 API 키가 없습니다")

    keywords = [k for k in (cfg.get("keywords") or []) if k]
    if not keywords:
        return []

    days = int(cfg.get("days", 3))
    max_pages = int(cfg.get("max_pages", 6))
    num_of_rows = int(cfg.get("page_unit", 500))
    timeout = cfg.get("timeout_sec", 20)
    delay = cfg.get("request_delay_sec", 0.3)

    end_dt = datetime.now()
    begin_dt = end_dt - timedelta(days=days)
    inqry_bgn = begin_dt.strftime("%Y%m%d") + "0000"
    inqry_end = end_dt.strftime("%Y%m%d") + "2359"

    out: List[Dict[str, Any]] = []
    seen_ids = set()
    for op_name in G2B_OPERATIONS:
        for page in range(1, max_pages + 1):
            params = {
                "serviceKey": key,
                "pageNo": str(page),
                "numOfRows": str(num_of_rows),
                "inqryDiv": "1",
                "inqryBgnDt": inqry_bgn,
                "inqryEndDt": inqry_end,
                "type": "json",
            }
            r = SESSION.get(f"{G2B_BASE}/{op_name}", params=params, timeout=timeout)
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                raise RuntimeError(f"나라장터 API 응답 파싱 실패(키/쿼터 문제일 수 있음): {r.text[:200]}")
            header = (data.get("response") or {}).get("header") or {}
            if header.get("resultCode") not in ("00", 0, "0"):
                raise RuntimeError(f"나라장터 API 오류: {header.get('resultMsg') or header.get('resultCode')}")
            body = (data.get("response") or {}).get("body") or {}
            items = body.get("items") or []
            if isinstance(items, dict):
                items = [items]
            if not items:
                break

            for it in items:
                title = pick(it, "bidNtceNm")
                if not title:
                    continue
                cls_name = pick(it, "dtilPrdctClsfcNoNm") or ""
                inst_name = pick(it, "ntceInsttNm") or ""
                haystack = f"{title} {cls_name} {inst_name}"
                if not any(kw in haystack for kw in keywords):
                    continue
                bid_no = pick(it, "bidNtceNo")
                bid_ord = pick(it, "bidNtceOrd") or "000"
                uid = f"{bid_no}-{bid_ord}"
                if not bid_no or uid in seen_ids:
                    continue
                seen_ids.add(uid)

                start = (pick(it, "bidNtceDt") or "")[:10] or None
                end = (pick(it, "bidClseDt") or pick(it, "opengDt") or "")[:10] or None
                url = pick(it, "bidNtceDtlUrl", "bidNtceUrl") or "https://www.g2b.go.kr"
                budget_raw = pick(it, "presmptPrce", "asignBdgtAmt")
                try:
                    budget = f"{int(float(budget_raw)):,}원"
                except (TypeError, ValueError):
                    budget = "공고 참조"
                org = pick(it, "ntceInsttNm", "dminsttNm")
                out.append(
                    normalize(
                        "g2b", title, org, "나라장터 입찰", start, end, url,
                        budget=budget, elig=elig_from_text(title, haystack), raw=it,
                    )
                )

            if len(items) < num_of_rows:
                break
            time.sleep(delay)

    return out


KSTARTUP_DEFAULT_URL = "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do"
KSTARTUP_CATEGORIES = [
    "사업화",
    "기술개발(R&D)",
    "시설ㆍ공간ㆍ보육",
    "멘토링ㆍ컨설팅ㆍ교육",
    "글로벌",
    "인력",
    "융자ㆍ보증",
    "융자",
    "행사ㆍ네트워크",
    "창업교육",
    "판로ㆍ해외진출",
    "정책자금",
]
KSTARTUP_CATEGORY_RE = re.compile(
    r"^(사업화|기술개발\(R&D\)|시설ㆍ공간ㆍ보육|멘토링ㆍ컨설팅ㆍ교육|글로벌|인력|융자ㆍ보증|융자|행사ㆍ네트워크|창업교육|판로ㆍ해외진출|정책자금)\s*(?:D-?\d+|D-DAY|오늘마감|상시|마감)?$"
)
KSTARTUP_BAD_TITLE_WORDS = [
    "새로운게시글", "스크랩", "조회", "등록일자", "시작일자", "마감일자", "정렬 유형", "검색어 입력",
    "공공", "민간", "지자체", "중앙부처", "처음페이지", "다음페이지", "마지막페이지", "이전페이지",
]


def discover_kstartup_detail_urls(html: str, base_url: str) -> List[str]:
    """HTML 안에 남아 있는 K-Startup 상세 링크를 최대한 추출한다.

    K-Startup 목록은 JS/form 방식으로 렌더링되는 부분이 있어 href가 없을 때도 있다.
    링크가 잡히지 않으면 목록 URL을 item url로 사용한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    urls: List[str] = []

    def add(u: str) -> None:
        u = clean(u).replace("&amp;", "&")
        if not u:
            return
        if "pbancSn=" in u and "bizpbanc" not in u:
            u = "/web/contents/bizpbanc-ongoing.do?schM=view&" + u.lstrip("?&")
        u = urljoin(base_url, u)
        if "pbancSn=" not in u and "schM=view" not in u:
            return
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "pbancSn=" in href or "schM=view" in href or "bizpbanc" in href:
            add(href)
    # script 안의 문자열/onclick 보완
    for m in re.finditer(r"(?:/web/contents/)?bizpbanc-[^\"'<>\s]+?\.do\?[^\"'<>\s]*?(?:schM=view|pbancSn=\d+)[^\"'<>\s]*", html):
        add(m.group(0))
    for m in re.finditer(r"pbancSn=\d+", html):
        add(m.group(0))
    return urls


def extract_kstartup_org(prefix: str, title: str) -> str:
    """'제목/사업명 기관명 등록일자 ...' 형태에서 기관명을 보수적으로 추출."""
    p = clean(prefix)
    t = clean(title)
    if t and p.startswith(t):
        p = clean(p[len(t):])
    # 제목과 다른 사업명이 앞에 붙는 경우가 많아, 기관명 접미사 위주로 마지막 덩어리를 잡는다.
    suffixes = "창조경제혁신센터|경제진흥원|산업진흥원|디지털혁신진흥원|기술진흥원|기술원|진흥원|재단|공단|협회|센터|대학교|대학|연구원|연구소|공사|파트너스|컴퍼니|시장|대표이사|장관"
    no_paren = re.sub(r"\([^)]*\)", " ", p)
    # 우선 공백 없는 마지막 기관명 후보를 잡는다. 예: '... 서울경제진흥원' -> '서울경제진흥원'
    m_short = re.search(r"((?:\(재\)|\(주\)|재단법인|사단법인)?[가-힣A-Za-z0-9·&]{2,35}(?:" + suffixes + r"))$", clean(no_paren))
    if m_short:
        return clean(m_short.group(1))
    org_pat = re.compile(
        r"((?:\(재\)|\(주\)|재단법인|사단법인)?[가-힣A-Za-z0-9·&()\s]{1,45}"
        r"(?:" + suffixes + r"))$"
    )
    m = org_pat.search(p)
    if m:
        return clean(m.group(1))
    # fallback: 마지막 1~4 토큰을 기관명으로 추정
    toks = [x for x in p.split() if x]
    if toks:
        tail = " ".join(toks[-3:])
        if len(tail) <= 50:
            return tail
    return "K-Startup"


def parse_kstartup_html_items(html: str, page_url: str, max_items: int = 80) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    raw_text = soup.get_text("\n", strip=True)
    lines = [clean(x) for x in raw_text.splitlines() if clean(x)]
    detail_urls = discover_kstartup_detail_urls(html, page_url)

    # 실제 목록은 '정렬 유형 선택' 이후에 카드형으로 반복된다. 없으면 모집중 이후부터 탐색.
    start_i = 0
    for marker in ["정렬 유형 선택", "다양한 조건을 설정하여", "검색어 입력"]:
        idx = next((i for i, line in enumerate(lines) if marker in line), None)
        if idx is not None:
            start_i = idx
            break
    scan_lines = lines[start_i:]

    cat_positions = []
    for i, line in enumerate(scan_lines):
        if KSTARTUP_CATEGORY_RE.match(line):
            cat_positions.append(i)

    out: List[Dict[str, Any]] = []
    for pos_idx, pos in enumerate(cat_positions):
        nxt = cat_positions[pos_idx + 1] if pos_idx + 1 < len(cat_positions) else min(len(scan_lines), pos + 18)
        block = scan_lines[pos:nxt]
        joined = clean(" ".join(block))
        if "마감일자" not in joined:
            continue

        category_match = KSTARTUP_CATEGORY_RE.match(block[0])
        category = category_match.group(1) if category_match else "창업지원"
        reg = None
        start = None
        end = None
        m_reg = re.search(r"등록일자\s*(20\d{2}-\d{2}-\d{2})", joined)
        m_start = re.search(r"시작일자\s*(20\d{2}-\d{2}-\d{2})", joined)
        m_end = re.search(r"마감일자\s*(20\d{2}-\d{2}-\d{2})", joined)
        if m_reg:
            reg = m_reg.group(1)
        if m_start:
            start = m_start.group(1)
        if m_end:
            end = m_end.group(1)
        if not end:
            continue

        title_candidates = []
        for line in block[1:8]:
            if not line or len(line) < 6:
                continue
            if any(w in line for w in KSTARTUP_BAD_TITLE_WORDS):
                continue
            if KSTARTUP_CATEGORY_RE.match(line):
                continue
            title_candidates.append(line)
        if not title_candidates:
            continue
        title = title_candidates[0]

        # 상세 정보 줄: '... 기관명 등록일자 YYYY-MM-DD 시작일자 ...' 형태
        # joined 전체를 쓰면 제목/새로운게시글이 앞에 섞여 기관명 추정이 흔들리므로
        # 등록일자가 들어 있는 실제 카드 설명 줄만 우선 사용한다.
        org = "K-Startup"
        detail_line = next((line for line in block if "등록일자" in line), "")
        m_prefix = re.search(r"(.+?)\s+등록일자\s*20\d{2}-\d{2}-\d{2}", detail_line or joined)
        if m_prefix:
            prefix = m_prefix.group(1).replace("새로운게시글", " ")
            org = extract_kstartup_org(prefix, title)
        else:
            # 상단 추천 영역은 별도 줄에 기관명이 나오는 경우가 있다.
            for line in block:
                if any(suf in line for suf in ["진흥원", "센터", "재단", "공단", "협회", "대학교", "연구원", "기술원"]):
                    if len(line) <= 60 and not any(w in line for w in ["조회", "마감일자", "시작일자", "등록일자"]):
                        org = line
                        break

        url = detail_urls[len(out)] if len(detail_urls) > len(out) else page_url
        raw = {"collector": "kstartup_html", "row_text": joined[:1200]}
        elig = elig_from_text(title, category, joined)
        item = normalize(
            "kstartup",
            title,
            org,
            category,
            start or reg,
            end,
            url,
            budget="공고 참조",
            elig=elig,
            raw=raw,
        )
        out.append(item)
        if len(out) >= max_items:
            break
    return out


def collect_kstartup(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """K-Startup 모집중 웹페이지 HTML 직접 크롤링.

    API 키를 사용하지 않는다. 기본 URL은
    https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do 이다.
    """
    if not cfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    list_urls = cfg.get("list_urls") or cfg.get("page_urls") or [cfg.get("list_url") or KSTARTUP_DEFAULT_URL]
    timeout = cfg.get("timeout_sec", 20)
    max_items = int(cfg.get("max_items", cfg.get("max_items_per_source", 80)))
    out: List[Dict[str, Any]] = []
    last_error: Optional[Exception] = None

    for url in list_urls:
        url = clean(url)
        if not url or url.startswith("TODO"):
            continue
        try:
            if cfg.get("respect_robots", True) and not robots_allows(url):
                raise PermissionError("robots.txt 차단")
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            items = parse_kstartup_html_items(r.text, url, max_items=max_items)
            out.extend(items)
            if len(out) >= max_items:
                break
            time.sleep(float(cfg.get("request_delay_sec", 0.3)))
        except Exception as e:
            last_error = e
            continue

    out = deduplicate(out)
    if not out:
        raise last_error or RuntimeError("K-Startup HTML 0건: 페이지 구조 변경 또는 접속 차단")
    return out[:max_items]


def robots_allows(url: str) -> bool:
    try:
        u = urlparse(url)
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{u.scheme}://{u.netloc}/robots.txt")
        rp.read()
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def collect_board(source_id: str, bcfg: Dict[str, Any], common: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not bcfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    list_url = clean(bcfg.get("list_url"))
    if not list_url or list_url.startswith("TODO"):
        raise RuntimeError("list_url 미설정")
    if common.get("respect_robots", True) and not robots_allows(list_url):
        raise PermissionError("robots.txt 차단")

    r = SESSION.get(list_url, timeout=common.get("timeout_sec", 20))
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    pattern = re.compile(bcfg.get("link_pattern") or "view|View|bbs|notice", re.I)
    seen = set()
    out: List[Dict[str, Any]] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not pattern.search(href):
            continue
        title = clean(a.get_text(" ", strip=True) or a.get("title"))
        if len(title) < 6:
            continue
        url = urljoin(bcfg.get("base_url") or list_url, href)
        if url in seen:
            continue
        seen.add(url)
        parent = a.find_parent(["tr", "li", "article", "div"]) or a.parent
        row_text = clean(parent.get_text(" ", strip=True) if parent else title)
        start = end = None
        if re.search(r"[~∼〜]", row_text):
            start, end = parse_period(row_text)
        else:
            m = re.search(r"마감[^0-9]{0,8}(20\d{2}[.\-/년\s]*\d{1,2}[.\-/월\s]*\d{1,2})", row_text)
            if m:
                end = parse_date(m.group(1))
            start = parse_date(row_text)
        out.append(normalize(source_id, title, bcfg.get("org") or bcfg.get("name"), bcfg.get("category") or "R&D", start, end, url, raw={"row_text": row_text}))
        if len(out) >= int(common.get("max_items_per_source", 60)):
            break
    if not out:
        raise RuntimeError("0건 파싱: link_pattern 또는 게시판 구조 확인 필요")
    time.sleep(float(common.get("request_delay_sec", 0.8)))
    return out




# ──────────────────────────── 한국연구재단(NRF) - IRIS 경유 수집 ────────────────────────────
# nrf.re.kr 자체 사이트는 robots.txt가 루트(/) 외 전체를 차단해서 직접 크롤링이 불가능하다.
# 대신 범부처통합연구지원시스템(IRIS, iris.go.kr)의 사업공고 목록에서 한국연구재단(sorgnId=10001)
# 공고만 필터링해 가져온다. 이 목록/상세 경로는 IRIS의 robots.txt에서 막혀있지 않다.
IRIS_VIEW_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmView.do"
IRIS_VIEW_ONCLICK_RE = re.compile(
    r"f_bsnsAncmListForm_view\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'\)"
)


def parse_iris_list_items(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict[str, Any]] = []
    for li in soup.select(".tstyle.list.biz_announce .dbody li"):
        a = li.select_one(".title a")
        if not a:
            continue
        title = clean(a.get_text(" ", strip=True))
        if not title:
            continue

        detail_url = IRIS_VIEW_URL
        start = end = None
        m = IRIS_VIEW_ONCLICK_RE.search(a.get("onclick") or "")
        if m:
            ancm_id, bsns_yy, sorgn_bsns_cd, bsns_ancm_sn, d_day, rcve_str, rcve_end = m.groups()
            detail_url = (
                f"{IRIS_VIEW_URL}?ancmId={ancm_id}&bsnsYyDetail={bsns_yy}"
                f"&sorgnBsnsCd={sorgn_bsns_cd}&bsnsAncmSn={bsns_ancm_sn}&detailDDay={d_day}"
            )
            start, end = parse_period(f"{rcve_str}~{rcve_end}")

        period_el = li.select_one(".period")
        if period_el and (not start or not end):
            st2, en2 = parse_period(clean(period_el.get_text(" ", strip=True)))
            start, end = start or st2, end or en2

        inst_el = li.select_one(".inst_title")
        inst = clean(inst_el.get_text(" ", strip=True)) if inst_el else ""
        org = inst.rsplit(">", 1)[-1].strip() if ">" in inst else (inst or "한국연구재단")

        etc: Dict[str, str] = {}
        for span in li.select(".etc_info span"):
            em = span.find("em")
            if not em:
                continue
            label = clean(em.get_text())
            value = clean(span.get_text()).replace(label, "", 1).strip()
            if label and value:
                etc[label] = value

        elig = elig_from_text(title, etc.get("세부사업명", ""), etc.get("사업공고명", ""))
        out.append(normalize(
            "nrf", title, org or "한국연구재단", "R&D", start, end, detail_url,
            budget="공고 참조", elig=elig, raw={"collector": "nrf_iris", "inst": inst, **etc},
        ))
    return out


def collect_nrf_iris(bcfg: Dict[str, Any], common: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not bcfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    list_url = clean(bcfg.get("list_url"))
    if not list_url or list_url.startswith("TODO"):
        raise RuntimeError("list_url 미설정")
    if common.get("respect_robots", True) and not robots_allows(list_url):
        raise PermissionError("robots.txt 차단")

    timeout = common.get("timeout_sec", 20)
    max_items = int(common.get("max_items_per_source", 60))
    max_pages = int(bcfg.get("max_pages", 6))
    sep = "&" if "?" in list_url else "?"

    out: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        url = list_url if page == 1 else f"{list_url}{sep}pageIndex={page}"
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        items = parse_iris_list_items(r.text)
        if not items:
            break
        out.extend(items)
        if len(out) >= max_items:
            break
        time.sleep(float(common.get("request_delay_sec", 0.8)))

    out = deduplicate(out)
    if not out:
        raise RuntimeError("NRF(IRIS) 0건: sorgnId 필터 또는 IRIS 목록 페이지 구조 확인 필요")
    return out[:max_items]


# ──────────────────────────── 서울바이오허브 전용 수집기 ────────────────────────────

BIOHUB_DEFAULT_GUBUNS = ["14", "08", "09", "04", "03", "12"]


def guess_biohub_category(title: str, text: str) -> str:
    hay = f"{title} {text}"
    if any(k in hay for k in ["입주", "공간", "센터"]):
        return "입주·공간"
    if any(k in hay for k in ["오픈이노베이션", "파트너링", "공동연구", "PoC", "대원제약", "SK바이오팜"]):
        return "오픈이노베이션"
    if any(k in hay for k in ["IR", "투자", "피칭"]):
        return "IR·투자유치"
    if any(k in hay for k in ["AI", "의료데이터", "데이터"]):
        return "AI·의료데이터"
    if any(k in hay for k in ["행사", "세미나", "교육", "네트워킹"]):
        return "행사·네트워크"
    return "바이오·헬스"


def compact_biohub_title(title: str) -> str:
    title = clean(title)
    title = re.sub(r"^(예약신청\s*>\s*)?프로그램\s*>\s*프로그램\s*상세페이지\s*\|\s*서울바이오.*$", "", title)
    title = re.sub(r"^서울바이오허브\s*", "", title)
    return clean(title)


def choose_biohub_title(soup: BeautifulSoup) -> str:
    # 서울바이오허브 상세 페이지는 실제 공고명을 두 곳에 그대로 담고 있다:
    # 1) hidden input(name="title") — 상세 페이지 뷰 폼이 제출하는 원본 값
    # 2) p.pop-cont-title > strong — 화면에 보이는 제목 영역
    # 두 값은 실사용 페이지들에서 항상 일치함을 확인했다. meta 태그(og:title 등)는
    # 이 사이트에서는 사이트 공용 값("Seoulbiohub")이거나 애초에 존재하지 않고,
    # 본문 헤딩(h1~h6/strong/b) 스캔은 페이지 상단 접근성 링크나 개인정보 재동의
    # 모달 텍스트를 오탐하기 쉬워 전부 제거했다.
    title_input = soup.find("input", attrs={"name": "title"})
    if title_input and clean(title_input.get("value")):
        t = compact_biohub_title(title_input.get("value"))
        if len(t) >= 8 and "서울바이오허브" not in {t}:
            return t

    pop_title = soup.select_one("p.pop-cont-title strong")
    if pop_title:
        t = compact_biohub_title(pop_title.get_text(" ", strip=True))
        if len(t) >= 8 and "서울바이오허브" not in {t}:
            return t

    return "서울바이오허브 공고"


def first_line_after_label(lines: List[str], labels: List[str], max_next: int = 2) -> Optional[str]:
    label_re = re.compile("|".join(re.escape(x) for x in labels))
    for i, line in enumerate(lines):
        if not label_re.search(line):
            continue
        # 같은 줄에 값이 붙어 있는 경우
        after = re.sub(r"^.*?(" + "|".join(re.escape(x) for x in labels) + r")\s*[:：]?\s*", "", line).strip()
        if after and after != line and len(after) > 2:
            return after
        for j in range(1, max_next + 1):
            if i + j < len(lines):
                nxt = clean(lines[i + j])
                if nxt and not label_re.fullmatch(nxt):
                    return nxt
    return None


def extract_biohub_period(lines: List[str], text: str) -> Tuple[Optional[str], Optional[str]]:
    labels = ["신청기간", "모집기간", "모집 기간", "접수기간", "접수 기간", "신청 마감", "신청마감", "마감"]
    # 1) 라벨 다음 줄/같은 줄 우선
    v = first_line_after_label(lines, labels, max_next=3)
    if v:
        st, en = parse_period(v)
        if st or en:
            return st, en
    # 2) 라벨 주변 160자 검색
    for lab in labels:
        m = re.search(re.escape(lab) + r"\s*[:：]?\s*(.{0,180})", text)
        if m:
            st, en = parse_period(m.group(1))
            if st or en:
                return st, en
    # 3) 상세 공고 안에서 자주 나오는 날짜 범위 패턴
    m = re.search(r"(20\d{2}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일[^~∼〜\n]{0,20}[~∼〜][^\n]{0,80})", text)
    if m:
        st, en = parse_period(m.group(1))
        if st or en:
            return st, en
    return None, None


def extract_biohub_budget(lines: List[str], text: str) -> str:
    labels = ["지원내용", "지원 내용", "지원혜택", "지원 혜택", "지원규모", "지원 규모", "모집규모", "모집 규모", "선발규모", "선발 규모"]
    v = first_line_after_label(lines, labels, max_next=2)
    if v:
        return clean(v)[:180]
    for lab in labels:
        m = re.search(re.escape(lab) + r"\s*[:：]?\s*(.{10,180})", text)
        if m:
            return clean(m.group(1))[:180]
    return "공고 참조"


def parse_biohub_detail(url: str, html: str) -> Optional[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    # script/style/nav성 텍스트 제거
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    raw_text = soup.get_text("\n", strip=True)
    lines = [clean(x) for x in raw_text.splitlines() if clean(x)]
    text = clean(raw_text)

    if len(text) < 200:
        return None
    # 유효 상세 페이지 여부. 개인정보/신청폼만 있는 페이지를 방지한다.
    if not any(k in text for k in ["신청기간", "모집기간", "접수기간", "신청 마감", "모집대상", "지원자격", "지원내용", "공고명"]):
        return None

    title = choose_biohub_title(soup)
    if title == "서울바이오허브 공고" and not any(k in text for k in ["공고", "모집", "프로그램"]):
        return None

    start, end = extract_biohub_period(lines, text)
    budget = extract_biohub_budget(lines, text)
    category = guess_biohub_category(title, text)
    elig = elig_from_text(title, text[:3500])
    return normalize(
        "biohub",
        title,
        "서울바이오허브",
        category,
        start,
        end,
        url,
        budget=budget,
        elig=elig,
        raw={"collector": "biohub_direct", "text_head": text[:1200]},
    )


def discover_biohub_links_from_html(html: str, base_url: str, pattern: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    pat = re.compile(pattern or r"supportManageView\.do|seq=", re.I)
    urls: List[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not pat.search(href):
            continue
        url = urljoin(base_url, href)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    # HTML 안에 문자열로 들어간 view URL도 보완 추출
    for m in re.finditer(r"supportManageView\.do\?[^\"'<>\s]+", html):
        url = urljoin(base_url, m.group(0).replace("&amp;", "&"))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def biohub_seq_urls(bcfg: Dict[str, Any]) -> List[str]:
    base_url = clean(bcfg.get("base_url")) or "https://www.seoulbiohub.kr"
    gubuns = bcfg.get("gubun_list") or BIOHUB_DEFAULT_GUBUNS
    try:
        seq_start = int(bcfg.get("seq_start") or 730)
        seq_end = int(bcfg.get("seq_end") or 790)
    except Exception:
        seq_start, seq_end = 730, 790
    urls: List[str] = []
    # 최신 seq가 큰 쪽일 가능성이 높으므로 역순 탐색
    for seq in range(seq_end, seq_start - 1, -1):
        for g in gubuns:
            urls.append(f"{base_url}/front/supportManageReq/supportManageView.do?gubun={g}&seq={seq}")
    return urls


def collect_biohub_direct(bcfg: Dict[str, Any], common: Dict[str, Any]) -> List[Dict[str, Any]]:
    """서울바이오허브 전용 수집기.

    일반 게시판 크롤러와 달리 서울바이오허브는 상세 URL인
    supportManageView.do?gubun=..&seq=.. 페이지를 직접 파싱한다.
    1) list_url에서 상세 링크를 먼저 발견하고,
    2) config의 seed_urls를 보조로 쓰고,
    3) 필요 시 최근 seq 범위를 스캔한다.
    """
    if not bcfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    base_url = clean(bcfg.get("base_url")) or "https://www.seoulbiohub.kr"
    pattern = bcfg.get("link_pattern") or r"supportManageView\.do|seq="
    timeout = common.get("timeout_sec", 20)
    max_items = int(common.get("max_items_per_source", 80))
    delay = float(bcfg.get("detail_delay_sec", 0.06))

    if common.get("respect_robots", True):
        probe = f"{base_url}/front/supportManageReq/supportManageView.do"
        if not robots_allows(probe):
            raise PermissionError("robots.txt 차단")

    candidates: List[str] = []
    seen = set()

    def add_url(u: str) -> None:
        u = clean(u).replace("&amp;", "&")
        if not u:
            return
        u = urljoin(base_url, u)
        if "supportManageView.do" not in u:
            return
        if u not in seen:
            seen.add(u)
            candidates.append(u)

    # 1) list_url에서 상세 링크 발견. totalSearch.do처럼 GET이 막혀도 실패만 기록하고 다음 단계 진행.
    list_urls = bcfg.get("list_urls") or [bcfg.get("list_url")]
    for lu in list_urls:
        lu = clean(lu)
        if not lu or lu.startswith("TODO"):
            continue
        try:
            if common.get("respect_robots", True) and not robots_allows(lu):
                continue
            r = SESSION.get(lu, timeout=timeout)
            if r.status_code >= 400:
                continue
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            for u in discover_biohub_links_from_html(r.text, base_url, pattern):
                add_url(u)
        except Exception:
            continue

    # 2) seed_urls. 목록 URL이 막히거나 JS 렌더링이면 이 값으로 보완한다.
    for u in bcfg.get("seed_urls") or []:
        add_url(u)

    # 3) seq 스캔. 기본은 최근 범위만 짧게 스캔하도록 설정한다.
    if bcfg.get("seq_scan_enabled", True):
        for u in biohub_seq_urls(bcfg):
            add_url(u)

    out: List[Dict[str, Any]] = []
    used_title_keys = set()
    max_detail_pages = int(bcfg.get("max_detail_pages", max(180, max_items * 6)))

    for url in candidates[:max_detail_pages]:
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code >= 400:
                continue
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            item = parse_biohub_detail(url, r.text)
            if not item:
                continue
            key = re.sub(r"[\s\[\](){}<>〔〕·,._-]", "", item["title"])[:54]
            if key in used_title_keys:
                continue
            used_title_keys.add(key)
            out.append(item)
            if len(out) >= max_items:
                break
            if delay:
                time.sleep(delay)
        except Exception:
            continue

    if not out:
        raise RuntimeError("서울바이오허브 0건: list_url/seed_urls/seq_start·seq_end 확인 필요")
    return out


def deduplicate(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for a in items:
        key = re.sub(r"[\s\[\](){}<>〔〕·,._-]", "", a.get("title", ""))[:48]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


# 병합 시 같은 소스가 여러 개 있으면 이 순서상 앞쪽을 "대표" 항목으로 쓴다.
MERGE_SOURCE_PRIORITY = ["bizinfo", "kstartup", "biohub_direct", "khidi_direct", "kddf", "nrf", "biohub", "khidi", "sample"]
MERGE_TITLE_SIM_THRESHOLD = 0.8


def _merge_title_key(title: str) -> str:
    t = re.sub(r"\([^)]*\)", "", title or "")
    return re.sub(r"[^\w가-힣]", "", t)


def _merge_end_dates_conflict(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    e1, e2 = a.get("end"), b.get("end")
    if not e1 or not e2 or e1 == e2:
        return False
    try:
        d1 = date.fromisoformat(e1)
        d2 = date.fromisoformat(e2)
    except ValueError:
        return False
    return abs((d1 - d2).days) > 3


def merge_duplicate_notices(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """같은 공고가 여러 사이트(또는 같은 사이트의 흔들리는 URL)로 중복 수집된 경우
    하나의 공고로 합치고, 게재된 사이트 목록(sources)을 함께 붙인다.

    제목 유사도가 0.8 이상이고 마감일이 크게 다르지 않을 때만 같은 공고로 본다.
    (예: '대구콘텐츠코리아랩'과 '광주콘텐츠코리아랩'처럼 유사도가 낮은 건 합치지 않는다.)
    """
    groups: List[List[Dict[str, Any]]] = []
    keys: List[str] = []
    for it in items:
        key = _merge_title_key(it.get("title"))
        placed = False
        for gi, gkey in enumerate(keys):
            if not key or not gkey:
                continue
            if abs(len(key) - len(gkey)) > max(len(key), len(gkey)) * 0.4:
                continue
            ratio = difflib.SequenceMatcher(None, key, gkey).ratio()
            if ratio < MERGE_TITLE_SIM_THRESHOLD:
                continue
            if _merge_end_dates_conflict(it, groups[gi][0]):
                continue
            groups[gi].append(it)
            placed = True
            break
        if not placed:
            groups.append([it])
            keys.append(key)

    def priority(it: Dict[str, Any]) -> Tuple[int, int]:
        src = it.get("src") or ""
        rank = MERGE_SOURCE_PRIORITY.index(src) if src in MERGE_SOURCE_PRIORITY else len(MERGE_SOURCE_PRIORITY)
        return (rank, -len(it.get("title") or ""))

    merged: List[Dict[str, Any]] = []
    for g in groups:
        g_sorted = sorted(g, key=priority)
        primary = dict(g_sorted[0])
        sources: List[Dict[str, Any]] = []
        seen_src = set()
        for it in g_sorted:
            cid = database.canonical_source_id(it.get("src") or "")
            if not cid or cid in seen_src:
                continue
            seen_src.add(cid)
            sources.append({"id": cid, "url": it.get("url") or ""})
        primary["id"] = make_id("", _merge_title_key(primary.get("title")))
        primary["sources"] = sources
        merged.append(primary)
    return merged


def load_sample_items() -> List[Dict[str, Any]]:
    if not SAMPLE_PATH.exists():
        return []
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def _apply_source_override(sub_cfg: Dict[str, Any], source_id: str, overrides: Dict[str, str]) -> Dict[str, Any]:
    """관리자가 재정의한 URL이 있으면 해당 소스 설정의 list_url/list_urls를 덮어쓴다."""
    url = overrides.get(source_id)
    if not url:
        return sub_cfg
    sub_cfg = dict(sub_cfg)
    sub_cfg["list_url"] = url
    sub_cfg["list_urls"] = [url]
    return sub_cfg


def collect_all(write_db: bool = True) -> CollectRun:
    cfg = load_config()
    overrides = database.get_source_overrides()
    run = CollectRun()
    common = cfg.get("common", {})
    cap = int(common.get("max_items_per_source", 60))

    # Bizinfo and routed institutional notices.
    bcfg = cfg.get("bizinfo", {})
    if bcfg.get("enabled", False):
        try:
            routed = collect_bizinfo({**common, **bcfg}, cfg.get("route_from_bizinfo", {}))
            for sid in ["bizinfo", "biohub", "khidi"]:
                items = routed.get(sid, [])[:cap]
                run.record(sid, items, "정상" if items or sid == "bizinfo" else "0건")
        except Exception as e:
            # Keep the error in the admin detail, but do not create extra zero-count
            # routed source rows such as "서울바이오허브 대기 0".  Direct
            # collectors for those institutions are recorded separately.
            run.record("bizinfo", [], "오류", e)
    else:
        run.record("bizinfo", [], "비활성화")

    # 나라장터(G2B) 입찰공고 — 물품/용역 중 키워드에 매칭되는 것만.
    gcfg = cfg.get("g2b", {})
    if gcfg.get("enabled", False):
        try:
            run.record("g2b", collect_g2b({**common, **gcfg})[:cap], "정상")
        except Exception as e:
            run.record("g2b", [], "오류", e)
    else:
        run.record("g2b", [], "비활성화")

    # K-Startup.
    kcfg = _apply_source_override(cfg.get("kstartup", {}), "kstartup", overrides)
    if kcfg.get("enabled", False):
        try:
            run.record("kstartup", collect_kstartup({**common, **kcfg})[:cap], "정상")
        except Exception as e:
            run.record("kstartup", [], "오류", e)
    else:
        run.record("kstartup", [], "비활성화")

    # Direct boards.
    for sid, raw_board_cfg in (cfg.get("boards") or {}).items():
        if sid.startswith("_"):
            continue
        board_cfg = _apply_source_override(raw_board_cfg, sid, overrides)
        if not board_cfg.get("enabled", False):
            run.record(sid, [], "비활성화")
            continue
        try:
            if sid == "biohub_direct":
                items = collect_biohub_direct(board_cfg, common)[:cap]
                run.record(sid, items, "정상", name=board_cfg.get("name") or "서울바이오허브(직접)", method="전용 파서")
            elif sid == "nrf":
                items = collect_nrf_iris(board_cfg, common)[:cap]
                run.record(sid, items, "정상", name=board_cfg.get("name") or "한국연구재단", method="IRIS 경유")
            else:
                items = collect_board(sid, board_cfg, common)[:cap]
                run.record(sid, items, "정상", name=board_cfg.get("name"), method="게시판")
        except PermissionError as e:
            run.record(sid, [], "차단(robots)", e, name=board_cfg.get("name"), method="게시판")
        except Exception as e:
            run.record(sid, [], "오류", e, name=board_cfg.get("name"), method="게시판")

    run.items = merge_duplicate_notices(run.items)
    used_sample = False

    if not run.items and cfg.get("use_sample_when_empty", True):
        sample = load_sample_items()
        run.record("sample", sample, "샘플 표시")
        run.items = merge_duplicate_notices(sample)
        used_sample = True

    if write_db:
        database.upsert_notices(run.items, prune=not used_sample)
        run.sources = database.flag_source_anomalies(run.sources)
        database.record_source_history(run.sources)
        database.replace_source_status(run.sources)
    return run


def main() -> None:
    run = collect_all(write_db=True)
    print(f"Collected {len(run.items)} notices")
    for sid, s in run.sources.items():
        msg = f"- {sid}: {s['state']} / {s['n']}"
        if s.get("error"):
            msg += f" / {s['error']}"
        print(msg)


if __name__ == "__main__":
    main()
