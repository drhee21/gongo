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
import html
import json
import os
import re
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import auth
import database
import funding_classifier
import llm

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


# config.json의 max_items_per_source(또는 소스별 max_items)를 0 이하로 설정하면
# "제한 없음"으로 취급한다. 각 수집기의 페이지네이션 루프는 이 값과 무관하게
# 빈 페이지/사이트가 알려주는 마지막 페이지/증가 없음 등 자체 종료 조건을 갖고
# 있으므로, 사실상 무제한으로 취급해도 무한 루프가 되지 않는다 — 다만 값 자체를
# 그대로 10억처럼 두면 KHIDI rowCnt처럼 API 파라미터로 그대로 나가는 곳이 있어
# 과도하게 큰 숫자를 보낼 수 있으므로, 매우 크되 상식적인 값으로 대체한다.
UNLIMITED_ITEMS = 100_000


def resolve_max_items(value: Any, default: int = 80) -> int:
    """max_items_per_source 설정값을 해석한다. 0 이하(또는 값이 없거나 잘못됨)면
    UNLIMITED_ITEMS를 돌려줘 사실상 전체 수집이 되게 한다."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return UNLIMITED_ITEMS if n <= 0 else n


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


ROLLING_KEYWORDS_RE = re.compile(r"상시|수시|소진|예산\s*소진|완료\s*시|선착순")


def mark_dates_unknown_if_needed(item: Dict[str, Any], source_text: str, start_was_known: bool = False) -> None:
    """end 날짜를 못 찾았을 때 처리 방식을 소스 전체에 걸쳐 통일한다.

    - 원문에 "상시/수시/소진" 같은 명시적 표현이 있으면 진짜로 마감이 없는
      공고이므로 상시 그대로 둔다(손대지 않음).
    - 시작일을 실제로 찾은 경우(start_was_known=True)는 마감일만 못 찾은
      것이므로 dates_unknown으로 올리지 않는다 — 이미 아는 시작일 정보를
      날짜 미상 처리로 지워버리면 안 된다. end만 비워서 "-"로 보이게 한다.
      (normalize()가 start를 항상 오늘 날짜로 채워주기 때문에
      item.get("start")만으로는 "진짜로 찾았는지"를 알 수 없어, 호출부에서
      정규식 매칭 직후의 원본 start 값을 start_was_known으로 넘겨받는다.)
    - 시작일도 못 찾았고 그런 표현도 없으면, 이 공고의 날짜에 대해 아는 게
      전혀 없는 것이므로 dates_unknown을 표시해 "날짜 미상"으로 보이게
      한다(상시로 지어내지 않음).
    """
    if item.get("end"):
        return
    if ROLLING_KEYWORDS_RE.search(source_text or ""):
        item["rolling_confirmed"] = True
        return
    item["end"] = None
    if start_was_known:
        return
    item["start"] = None
    item["dates_unknown"] = True


def parse_period(text: Any) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None
    t = str(text)
    if ROLLING_KEYWORDS_RE.search(t):
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
        "dates_unknown": False,
        "rolling_confirmed": False,
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
    # 기업마당 API 키는 config.json에 두지 않는다 — 그 키를 실제로 등록한
    # 사용자만 결과를 볼 수 있어야 하므로, 수집도 등록된 사용자의 키로만
    # 이뤄진다. 관리자가 등록한 키를 우선 사용한다.
    key_enc = database.get_any_bizinfo_key_enc()
    if not key_enc:
        raise RuntimeError("등록된 기업마당 API 키가 없습니다. '회사 정보'에서 먼저 키를 등록해주세요")
    key = auth.decrypt_secret(key_enc)

    page_unit = int(cfg.get("page_unit", 200))
    max_items = resolve_max_items(cfg.get("max_items_per_source", 80))
    timeout = cfg.get("timeout_sec", 20)
    delay = cfg.get("request_delay_sec", 0.8)

    # bizinfo 응답을 라우팅 키워드에 따라 bizinfo/biohub/khidi 세 묶음으로 나누는데,
    # 각 묶음은 collect_all()에서 따로따로 max_items_per_source만큼 잘린다. 그래서
    # "bizinfo 묶음이 최소 max_items만큼 찰 때까지" 페이지를 계속 넘긴다 —
    # 대부분의 공고가 bizinfo 묶음에 남으므로 이게 사실상의 종료 조건이다.
    out = {"bizinfo": [], "biohub": [], "khidi": []}
    page = 1
    while len(out["bizinfo"]) < max_items:
        params = {
            "crtfcKey": key,
            "dataType": "json",
            "pageIndex": str(page),
            "pageUnit": str(page_unit),
        }
        r = SESSION.get("https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do", params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        rows = data.get("jsonArray", data) if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = rows.get("item", [])
        if not rows:
            break

        for it in rows:
            if not isinstance(it, dict):
                continue
            title = pick(it, "pblancNm", "title", "bizNm")
            if not title:
                continue
            period_text = pick(it, "reqstBeginEndDe", "reqstDt", "period")
            start, end = parse_period(period_text)
            rel_url = clean(pick(it, "pblancUrl", "url") or "")
            url = urljoin("https://www.bizinfo.go.kr", rel_url)
            org = pick(it, "jrsdInsttNm", "excInsttNm", "insttNm")
            exc = pick(it, "excInsttNm") or ""
            cat = pick(it, "pldirSportRealmLclasCodeNm", "category", "realmNm") or "경영·기술"
            target = pick(it, "trgetNm", "target", "aplyTrgt") or ""
            haystack = f"{title} {org or ''} {exc} {pick(it, 'hashtags') or ''}"
            src = "bizinfo"
            for route_src, keywords in (route or {}).items():
                if route_src.startswith("_"):
                    continue
                if any(kw and kw in haystack for kw in keywords):
                    src = route_src
                    break
            item = normalize(src, title, org, cat, start, end, url, elig=elig_from_text(target, title, haystack), raw=it)
            mark_dates_unknown_if_needed(item, period_text or "", start_was_known=bool(start))
            out.setdefault(src, []).append(item)

        if len(rows) < page_unit:
            break
        page += 1
        if delay:
            time.sleep(delay)

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
    max_items = resolve_max_items(cfg.get("max_items_per_source", 80))
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
        if len(out) >= max_items:
            break
        page = 1
        while len(out) < max_items:
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
                g2b_item = normalize(
                    "g2b", title, org, "나라장터 입찰", start, end, url,
                    budget=budget, elig=elig_from_text(title, haystack), raw=it,
                )
                mark_dates_unknown_if_needed(g2b_item, haystack, start_was_known=bool(start))
                out.append(g2b_item)
                if len(out) >= max_items:
                    break

            if len(out) >= max_items or len(items) < num_of_rows:
                break
            page += 1
            if delay:
                time.sleep(delay)

    return out


KSTARTUP_DEFAULT_URL = "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do"
# 카테고리 배지는 클래스가 "flag typeNN"(NN=두 자리 숫자) 형태이고, 같은 부모 안에
# D-day 배지도 "flag day"로 같은 flag 클래스를 공유한다. day와 구분하려고 day가
# 아닌 것을 걸러내는 대신, "typeNN 모양인가"를 직접 확인한다 — 나중에 flag 클래스가
# 하나 더 늘어나도(day 외의 새 배지) 엉뚱한 걸 카테고리로 오인하지 않는다.
KSTARTUP_TYPE_CLASS_RE = re.compile(r"^type\d{2}$")
KSTARTUP_GO_VIEW_RE = re.compile(r"go_view\(\s*(\d+)\s*\)")
KSTARTUP_LAST_PAGE_RE = re.compile(r"fn_egov_link_page\(\s*(\d+)\s*\)")


def detect_kstartup_last_page(html: str) -> int:
    """페이지네이션 링크(fn_egov_link_page(N))에 나오는 값 중 가장 큰 수가 마지막
    페이지 번호다. "마지막페이지" 버튼도 같은 함수를 호출하므로 별도 텍스트 매칭
    없이 이 값들의 최댓값만 구하면 된다."""
    nums = [int(n) for n in KSTARTUP_LAST_PAGE_RE.findall(html)]
    return max(nums) if nums else 1


def parse_kstartup_html_items(html: str, page_url: str, max_items: int = 80) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("#bizPbancList")
    if not container:
        return []

    out: List[Dict[str, Any]] = []
    for li in container.select("li"):
        inner = li.select_one("div.inner")
        if not inner:
            continue

        tit_el = inner.select_one("div.right > div.middle > a > div.tit_wrap > p.tit")
        title = clean(tit_el.get_text()) if tit_el else ""
        if len(title) < 4:
            continue

        # 목록 링크가 <a href>가 아니라 "javascript:go_view(178627)" 형태라서, 그
        # 안의 글 번호(pbancSn)를 뽑아 상세 페이지 URL을 직접 만든다.
        a = inner.select_one("div.right > div.middle > a")
        href = a.get("href", "") if a else ""
        m_id = KSTARTUP_GO_VIEW_RE.search(href)
        url = urljoin(page_url, f"?schM=view&pbancSn={m_id.group(1)}") if m_id else page_url

        # 카테고리 배지는 "flag typeNN" 형태다. 같은 자리에 D-day 배지("flag day")도
        # 있어서 그냥 첫 span.flag를 집으면 틀릴 수 있으므로 typeNN 모양인 것만 취한다.
        category = "창업지원"
        top = inner.select_one("div.right > div.top")
        if top:
            for span in top.select("span.flag"):
                if any(KSTARTUP_TYPE_CLASS_RE.match(c) for c in (span.get("class") or [])):
                    category = clean(span.get_text()) or category
                    break

        # 기관명/등록일자/시작일자/마감일자/조회수가 전부 같은 모양의 span.list로
        # 나열되어 있다. 순서가 아니라 라벨 문자열로 구분한다 — 위치는 사이트가
        # 배지를 하나 더 넣거나 순서를 바꾸면 바로 깨지지만, 라벨은 그대로다.
        # 맨 첫 번째 span.list는 제목과 중복된 텍스트라 org 후보에서 제외한다.
        spans = inner.select("div.right > div.bottom > span.list")
        org = ""
        start = end = None
        for i, s in enumerate(spans):
            text = clean(s.get_text(" "))
            if i == 0:
                continue
            if text.startswith("등록일자") or text.startswith("조회"):
                continue
            if text.startswith("시작일자"):
                m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
                if m:
                    start = m.group(1)
                continue
            if text.startswith("마감일자"):
                m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
                if m:
                    end = m.group(1)
                continue
            if not org:
                org = text
        org = org or "K-Startup"

        row_text = clean(inner.get_text(" ", strip=True))[:1200]
        elig = elig_from_text(title, category, row_text)
        item = normalize(
            "kstartup", title, org, category, start, end, url,
            budget="공고 참조", elig=elig, raw={"row_text": row_text},
        )
        if not start:
            item["start"] = None
        # 마감일자가 없거나 형식이 안 맞아 못 뽑았을 때, 카드 전체를 버리지 않는다.
        # 페이지 구조가 통째로 바뀌었는지(전체 실패)는 이 함수가 0건을 반환했을 때
        # collect_kstartup()이 이미 예외로 잡아내므로, 카드 하나가 날짜를 못 찾았다고
        # 그 카드까지 버릴 필요는 없다 — 상시류 표현이 있으면 상시로, 없으면
        # 날짜 미상으로 남기고 카드 자체는 보여준다.
        mark_dates_unknown_if_needed(item, row_text, start_was_known=bool(start))
        out.append(item)
        if len(out) >= max_items:
            break
    return out


def collect_kstartup(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """K-Startup 모집중 웹페이지 HTML 직접 크롤링.

    더 이상 collect_all()이 평시 수집에 쓰지 않는다(_collect_via_stored_recipe()로
    대체됨) — 만일을 위해 코드만 남겨뒀다.

    API 키를 사용하지 않는다. 기본 URL은
    https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do 이다.

    이 페이지는 한 번에 15건씩만 보여주고 페이지 크기를 늘릴 수 없어서(API가
    아니라 서버 렌더링 HTML), 더 가져오려면 여러 페이지(?page=N)를 순회해야
    한다. max_items_per_source에 도달하거나, 사이트가 알려주는 마지막 페이지
    번호(첫 페이지에서 읽어온다)를 넘어서면 멈춘다.
    """
    if not cfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    base_url = clean(cfg.get("list_url") or KSTARTUP_DEFAULT_URL)
    if not base_url or base_url.startswith("TODO"):
        raise RuntimeError("list_url 미설정")
    if cfg.get("respect_robots", True) and not robots_allows(base_url):
        raise PermissionError("robots.txt 차단")

    timeout = cfg.get("timeout_sec", 20)
    max_items = resolve_max_items(cfg.get("max_items", cfg.get("max_items_per_source", 80)))

    out: List[Dict[str, Any]] = []
    last_error: Optional[Exception] = None
    last_page: Optional[int] = None
    page = 1
    while (last_page is None or page <= last_page) and len(out) < max_items:
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            if last_page is None:
                last_page = detect_kstartup_last_page(r.text)
            items = parse_kstartup_html_items(r.text, url, max_items=max_items - len(out))
            out.extend(items)
            page += 1
            if (last_page is None or page <= last_page) and len(out) < max_items:
                time.sleep(float(cfg.get("request_delay_sec", 0.3)))
        except Exception as e:
            last_error = e
            break

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


_TITLE_YEAR_SKIP = r"(?:['’]?\d{1,4}[.\s]+)?"
# 날짜처럼 생긴 숫자가 사실은 금액/수량 표현("3.5억원까지", "1.2배까지")인 경우를
# 걸러내기 위한 가드. 이런 문구도 "까지"로 끝나기 때문에 가드가 없으면 날짜로 오인한다.
_TITLE_NOT_UNIT = r"(?!\s*(?:억|만\s*원|원|%|퍼센트|배|톤|킬로|kg|점|개|건|명|회|리터|시간|일간))"
_TITLE_WEEKDAY = r"(?:\s*\([^)]{1,4}\))?"
_TITLE_DATE = r"(\d{1,2})\s*[./월]\s*(\d{1,2})" + _TITLE_NOT_UNIT + r"\s*일?\.?"

TITLE_DATE_RANGE_RE = re.compile(
    _TITLE_YEAR_SKIP + _TITLE_DATE + _TITLE_WEEKDAY + r"\s*~\s*" + _TITLE_YEAR_SKIP + _TITLE_DATE + _TITLE_WEEKDAY
)
# 마감일 신호는 두 가지 중 하나로 잡는다: "~" 뒤에 오는 날짜, 또는 "까지"/"마감"
# 앞에 오는 날짜. 후자의 경우 사이에 다른 날짜가 하나 더 끼어 있으면(범위 표기가
# 아닌데 우연히 걸리는 경우를 막기 위해) 건너뛰지 않도록 막아둔다.
_TITLE_TAIL = r"(?:(?!\d{1,2}\s*[./월]\s*\d{1,2}).){0,16}?(?:까지|마감)"
TITLE_END_DATE_RE = re.compile(
    r"(?:~\s*" + _TITLE_YEAR_SKIP + _TITLE_DATE + r")"
    r"|(?:" + _TITLE_YEAR_SKIP + _TITLE_DATE + _TITLE_TAIL + r")"
)


def parse_title_dates(title: str) -> Tuple[Optional[str], Optional[str]]:
    """제목에 흔히 붙는 '(~7.20.(월))', '(6/3~6/21 17시까지)' 같은 표기에서
    신청기간을 추정한다. 목록 행에 신청기간이 없는 게시판(예: khidi_direct)에서도
    제목에 박힌 날짜만큼은 정확하게 보여줄 수 있다. 연도가 명시되지 않으므로 현재
    연도를 기본으로 쓰되, 범위의 종료월이 시작월보다 앞서면(예: "12.19~2.5")
    연도가 넘어간 것으로 보고 종료일에 1년을 더한다."""
    m = TITLE_DATE_RANGE_RE.search(title)
    if m:
        s_mo, s_d, e_mo, e_d = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        s_year = date.today().year
        e_year = s_year + 1 if e_mo < s_mo else s_year
        if valid_ymd(s_year, s_mo, s_d) and valid_ymd(e_year, e_mo, e_d):
            return f"{s_year:04d}-{s_mo:02d}-{s_d:02d}", f"{e_year:04d}-{e_mo:02d}-{e_d:02d}"

    m = TITLE_END_DATE_RE.search(title)
    if m:
        mo, d = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        mo, d = int(mo), int(d)
        year = date.today().year
        if valid_ymd(year, mo, d):
            return None, f"{year:04d}-{mo:02d}-{d:02d}"

    return None, None


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
    # 일부 게시판은 목록 행에 신청기간이 아예 노출되지 않고 작성일(게시일)만 있어서,
    # row_text에서 날짜를 뽑으면 작성일을 신청 시작일로 잘못 표시하게 된다. 이런
    # 게시판은 config에서 "dates_reliable": false로 표시해 날짜 추출 자체를
    # 건너뛰고, 화면에는 날짜 미상으로 표시되게 한다(잘못된 날짜보다 낫다).
    dates_reliable = bcfg.get("dates_reliable", True)
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
        if dates_reliable:
            if re.search(r"[~∼〜]", row_text):
                start, end = parse_period(row_text)
            else:
                m = re.search(r"마감[^0-9]{0,8}(20\d{2}[.\-/년\s]*\d{1,2}[.\-/월\s]*\d{1,2})", row_text)
                if m:
                    end = parse_date(m.group(1))
                start = parse_date(row_text)
        item = normalize(source_id, title, bcfg.get("org") or bcfg.get("name"), bcfg.get("category") or "R&D", start, end, url, raw={"row_text": row_text})
        if dates_reliable:
            # 정상적으로 range/마감 패턴을 찾았으면 그대로 두고, 못 찾았으면
            # row_text에 상시류 표현이 있는지 확인해 날짜 미상 여부를 정한다.
            mark_dates_unknown_if_needed(item, row_text, start_was_known=bool(start))
        else:
            # normalize()는 start가 비어 있으면 오늘 날짜로 채우는데, 그러면 "오늘부터
            # 접수중"으로 잘못 보이므로 여기서 명시적으로 다시 비운다.
            item["start"] = None
            item["end"] = None
            # row_text에 신청기간이 없어도 제목 자체에 마감일이 박혀 있는 경우가
            # 있다("~7.20.(월)" 등). 이걸 뽑을 수 있으면 정확한 마감일을 쓰고,
            # 못 뽑으면 row_text/제목에 상시류 표현이 있는지 확인한 뒤 그마저
            # 없으면 날짜 미상으로 남긴다.
            title_start, title_end = parse_title_dates(title)
            if title_end:
                item["start"] = title_start
                item["end"] = title_end
            else:
                mark_dates_unknown_if_needed(item, row_text)
        out.append(item)
        if len(out) >= resolve_max_items(common.get("max_items_per_source", 60)):
            break
    if not out:
        raise RuntimeError("0건 파싱: link_pattern 또는 게시판 구조 확인 필요")
    time.sleep(float(common.get("request_delay_sec", 0.8)))
    return out


# ──────────────────────────── 국가신약개발사업단(KDDF) 전용 수집기 ────────────────────────────
# collect_board의 link_pattern 기반 범용 파싱 대신, kddf 게시판이 실제 테이블 구조
# (div.board_table > table > tbody > tr, 각 행에 td.subject/td.td_period/td.td_state)를
# 갖고 있다는 걸 활용해 제목·기간·진행상태를 훨씬 정확하게 뽑는다.


def collect_kddf(bcfg: Dict[str, Any], common: Dict[str, Any]) -> List[Dict[str, Any]]:
    """더 이상 collect_all()이 평시 수집에 쓰지 않는다(_collect_via_stored_recipe()로
    대체됨) — 만일을 위해 코드만 남겨뒀다."""
    if not bcfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    list_url = clean(bcfg.get("list_url"))
    if not list_url or list_url.startswith("TODO"):
        raise RuntimeError("list_url 미설정")
    if common.get("respect_robots", True) and not robots_allows(list_url):
        raise PermissionError("robots.txt 차단")

    base_url = bcfg.get("base_url") or list_url
    org_default = bcfg.get("org") or bcfg.get("name") or "국가신약개발사업단"
    category = bcfg.get("category") or "R&D"
    # 진행/완료 상태는 사이트 자체가 이미 판단해서 보여주는 값이라 참고용으로 쓸모
    # 있지만, 지금은 다른 소스와 마찬가지로 status_of()가 end_date만으로 상태를
    # 계산하므로 기본은 수집하지 않는다. config에서 include_state:true로 켜면
    # raw 데이터에만 남긴다(화면 로직에는 아직 반영하지 않음).
    include_state = bool(bcfg.get("include_state", False))
    max_items = resolve_max_items(common.get("max_items_per_source", 60))
    timeout = common.get("timeout_sec", 20)
    delay = float(common.get("request_delay_sec", 0.8))

    out: List[Dict[str, Any]] = []
    seen = set()
    page = 1
    while len(out) < max_items:
        r = SESSION.get(list_url, params={"page": str(page)}, timeout=timeout)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")

        added = 0
        for tr in soup.select("div.board_table table tbody tr"):
            subj = tr.select_one("td.subject")
            a = subj.select_one("a") if subj else None
            if not a:
                continue
            href = a.get("href", "")
            url = urljoin(base_url, href)
            url = re.sub(r"(?<!:)/{2,}", "/", url)
            if url in seen:
                continue
            seen.add(url)

            span = a.select_one("span")
            title_text = a.get_text(" ", strip=True)
            org = org_default
            if span:
                span_text = clean(span.get_text())
                org = span_text.strip("[]") or org_default
                title_text = title_text.replace(span_text, "", 1)
            title = clean(title_text)
            if len(title) < 4:
                continue

            period_el = tr.select_one("td.td_period")
            period_text = clean(period_el.get_text()) if period_el else ""
            start = end = None
            parts = re.split(r"[~∼]", period_text, maxsplit=1)
            if len(parts) == 2:
                start = parse_date(parts[0])
                end = parse_date(parts[1])

            raw: Dict[str, Any] = {"row_text": clean(tr.get_text(" ", strip=True))}
            if include_state:
                state_el = tr.select_one("td.td_state div.state_txt")
                if state_el:
                    raw["state"] = clean(state_el.get_text())

            item = normalize("kddf", title, org, category, start, end, url, raw=raw)
            mark_dates_unknown_if_needed(item, period_text, start_was_known=bool(start))
            out.append(item)
            added += 1
            if len(out) >= max_items:
                break

        # 페이지에서 새 항목을 하나도 못 뽑았으면(진짜 마지막 페이지를 넘어가면
        # 이 게시판은 빈 테이블 대신 안내용 빈 행 하나를 내려준다) 더 이상
        # 페이지가 없다고 보고 멈춘다.
        if added == 0:
            break
        page += 1
        if delay:
            time.sleep(delay)

    if not out:
        raise RuntimeError("0건 파싱: kddf 게시판 구조 확인 필요")
    return out


# ──────────────────────────── 보건산업진흥원(KHIDI) - 공고 API 수집 ────────────────────────────
# khidi.or.kr의 게시판 목록 화면에는 신청기간이 아예 노출되지 않아 예전에는 게시일만
# 있는 게시판을 크롤링했다. 대신 KHIDI가 제공하는 공고 API를 쓰면 각 공고의 제목을
# 가져올 수 있고, 그 제목에 박힌 마감일 표기(parse_title_dates)로 신청기간을 추정한다.
KHIDI_FEED_URL = "https://www.khidi.or.kr/kps/openAPI/requestxml"

KHIDI_DEADLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "start_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, 본문에서 신청/접수 시작일을 찾지 못했으면 null"},
                    "end_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, 본문에서 신청 마감일을 찾지 못했으면 null"},
                },
                "required": ["id", "start_date", "end_date"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

KHIDI_DEADLINE_SYSTEM_PROMPT = (
    "너는 한국 보건산업진흥원(KHIDI) 공고 본문을 읽고 '신청 접수 시작일'과 '신청 마감일'을 "
    "찾는 어시스턴트다.\n"
    "여러 날짜가 언급되어도 실제로 사업 신청/접수가 시작·마감되는 날짜만 골라라 (공고일, 심사일, "
    "발표일, 사업 수행기간의 시작·종료일 등은 신청기간이 아니다). 상시/수시 모집이거나 본문에 "
    "명확한 시작일/마감일이 없으면 해당 값을 null로 반환해라 — 하나만 명시되어 있으면(예: "
    "마감일만 있고 시작일 언급이 없음) 나머지 하나만 null로 두고 찾은 값은 채워라. 반드시 "
    "YYYY-MM-DD 형식으로, 전달받은 공고 전부에 대해 결과를 반환해라."
)

KHIDI_DEADLINE_CHUNK_SIZE = 20
KHIDI_DEADLINE_MIN_CONTENT_LEN = 30


def _strip_html_to_text(raw: str) -> str:
    if not raw:
        return ""
    return clean(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))


def _valid_ymd_str(s: Optional[str]) -> bool:
    if not s:
        return False
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    return bool(m and valid_ymd(*map(int, m.groups())))


def enrich_khidi_deadlines_with_ai(
    items: List[Dict[str, Any]], contents: Dict[str, str], triggering_user_id: Optional[str] = None
) -> None:
    """제목에서 시작일/마감일 중 못 찾은 게 있는 KHIDI 공고만, 본문 텍스트로 AI에게 물어본다.

    비용을 줄이기 위해 제목으로 시작일·마감일을 이미 둘 다 찾은 공고는 애초에 대상에서
    제외한다(이 함수를 호출하는 쪽에서 items를 그렇게 필터링해서 넘긴다). 이미 알고 있는
    값은 덮어쓰지 않는다 — AI는 제목 파싱이 못 찾은 값만 보충한다. 결과가 유효한
    YYYY-MM-DD 형식이 아니면 무시하고, 원래대로 '날짜 미상' 처리를 유지한다. items는
    in-place로 갱신된다.
    """
    candidates = [
        it for it in items
        if (not it.get("start") or not it.get("end")) and not it.get("rolling_confirmed")
        and len(contents.get(it["id"], "")) >= KHIDI_DEADLINE_MIN_CONTENT_LEN
    ]
    if not candidates:
        return

    profile = database.resolve_background_llm_profile(triggering_user_id)
    if not profile:
        return  # AI 키가 없으면 기존 '날짜 미상' 처리를 그대로 둔다 — 조용히 건너뛴다.
    model_id = profile["model_id"]
    api_key = auth.decrypt_secret(profile["key_enc"])

    by_id = {it["id"]: it for it in candidates}
    for i in range(0, len(candidates), KHIDI_DEADLINE_CHUNK_SIZE):
        batch = candidates[i:i + KHIDI_DEADLINE_CHUNK_SIZE]
        briefs = [
            {"id": it["id"], "title": it["title"], "content": contents[it["id"]]}
            for it in batch
        ]
        try:
            data = llm.structured_call(
                model_id, api_key,
                KHIDI_DEADLINE_SYSTEM_PROMPT,
                json.dumps(briefs, ensure_ascii=False),
                KHIDI_DEADLINE_SCHEMA,
                max_tokens=4000,
            )
        except Exception:
            continue  # 이 배치만 실패 — 나머지 배치는 계속 시도하고, 실패분은 기존 처리 유지.

        for r in data.get("results", []):
            it = by_id.get(r.get("id"))
            if not it:
                continue
            if not it.get("start") and _valid_ymd_str(r.get("start_date")):
                it["start"] = r["start_date"]
            if not it.get("end") and _valid_ymd_str(r.get("end_date")):
                it["end"] = r["end_date"]
                it["dates_unknown"] = False


def collect_khidi_direct(
    bcfg: Dict[str, Any], common: Dict[str, Any], triggering_user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    if not bcfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    if common.get("respect_robots", True) and not robots_allows(KHIDI_FEED_URL):
        raise PermissionError("robots.txt 차단")

    menu_id = bcfg.get("menu_id") or "MENU01108"
    # 이 피드는 offset/페이지 파라미터가 없고 rowCnt(상위 N건)만 지원한다 — 실제로
    # pageIndex/pageNo/startRow/offset 등을 붙여봐도 결과가 동일해 확인했다.
    # 그래서 "페이지네이션"은 곧 rowCnt를 원하는 최대 건수만큼 요청하는 것과 같다.
    # 다만 이 API는 rowCnt=N을 주면 실제로는 N-1건만 돌려주는 off-by-one이 있어서
    # (rowCnt=1 -> 0건, rowCnt=2 -> 1건 ... 실측으로 확인) 1을 더해서 요청한다.
    max_items = resolve_max_items(common.get("max_items_per_source", 80))
    params = {"menuId": menu_id, "rowCnt": max_items + 1}
    r = SESSION.get(KHIDI_FEED_URL, params=params, timeout=common.get("timeout_sec", 20))
    r.raise_for_status()
    root = ET.fromstring(r.content)

    base_url = bcfg.get("base_url") or "https://www.khidi.or.kr"
    org_default = bcfg.get("org") or bcfg.get("name") or "한국보건산업진흥원"
    category = bcfg.get("category") or "바이오·헬스"

    out: List[Dict[str, Any]] = []
    contents: Dict[str, str] = {}
    seen = set()
    for row in root.findall("row"):
        # 이 API는 XML 엔티티를 이중으로 인코딩해서 내려준다(원문의 "&amp;apos;"가
        # XML 파싱을 거치면 "&apos;" 문자열로 남는다). html.unescape를 한 번 더
        # 적용해야 실제 문자("'")로 바뀐다.
        title = clean(html.unescape(row.findtext("title") or ""))
        if len(title) < 4:
            continue
        url = clean(html.unescape(row.findtext("url") or ""))
        if not url:
            continue
        url = urljoin(base_url, url)
        if url in seen:
            continue
        seen.add(url)

        start, end = parse_title_dates(title)
        item = normalize(
            "khidi_direct", title, org_default, category, start, end, url,
            raw={"post_date": clean(row.findtext("date") or "")},
        )
        # normalize()는 start가 비어 있으면 오늘 날짜로 채우는데, 마감일만 뽑히고
        # 시작일은 못 뽑은 경우(제목에 종료일만 있는 경우가 대부분) "오늘부터
        # 접수중"으로 잘못 보이게 된다. mark_dates_unknown_if_needed는 end가 이미
        # 있으면 그냥 반환해버려서 start를 손대지 않으므로, 여기서 먼저 명시적으로
        # 비워준다.
        if not start:
            item["start"] = None
        mark_dates_unknown_if_needed(item, title, start_was_known=bool(start))
        if not end or not start:
            contents[item["id"]] = _strip_html_to_text(html.unescape(row.findtext("content") or ""))
        out.append(item)
        if len(out) >= max_items:
            break

    if not out:
        raise RuntimeError("0건 파싱: khidi 공고 API 구조 확인 필요")
    enrich_khidi_deadlines_with_ai(out, contents, triggering_user_id)
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
        nrf_item = normalize(
            "nrf", title, org or "한국연구재단", "R&D", start, end, detail_url,
            budget="공고 참조", elig=elig, raw={"collector": "nrf_iris", "inst": inst, **etc},
        )
        mark_dates_unknown_if_needed(nrf_item, li.get_text(" ", strip=True), start_was_known=bool(start))
        out.append(nrf_item)
    return out


def collect_nrf_iris(bcfg: Dict[str, Any], common: Dict[str, Any]) -> List[Dict[str, Any]]:
    """더 이상 collect_all()이 평시 수집에 쓰지 않는다(_collect_via_stored_recipe()로
    대체됨) — 만일을 위해 코드만 남겨뒀다."""
    if not bcfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    list_url = clean(bcfg.get("list_url"))
    if not list_url or list_url.startswith("TODO"):
        raise RuntimeError("list_url 미설정")
    if common.get("respect_robots", True) and not robots_allows(list_url):
        raise PermissionError("robots.txt 차단")

    timeout = common.get("timeout_sec", 20)
    max_items = resolve_max_items(common.get("max_items_per_source", 60))
    sep = "&" if "?" in list_url else "?"

    out: List[Dict[str, Any]] = []
    page = 1
    while True:
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
        page += 1
        time.sleep(float(common.get("request_delay_sec", 0.8)))

    out = deduplicate(out)
    if not out:
        raise RuntimeError("NRF(IRIS) 0건: sorgnId 필터 또는 IRIS 목록 페이지 구조 확인 필요")
    return out[:max_items]


# ──────────────────────────── 서울바이오허브 전용 수집기 ────────────────────────────


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

    # choose_biohub_title은 실제 공고면 hidden input(name="title") 또는
    # p.pop-cont-title에서 원본 공고명을 반환하고, 유령/빈 템플릿 페이지면
    # 이 둘이 모두 없어 아래 placeholder를 반환한다. 따라서 placeholder가 곧
    # "실제 공고가 아님" 신호이므로 그대로 버린다. (예전엔 여기에
    # `and not any("공고"/"모집"/"프로그램" in text)` 예외를 뒀는데, 전역 메뉴에
    # "모집"/"프로그램"이 항상 들어 있어 유령 페이지를 걸러내지 못했다.)
    title = choose_biohub_title(soup)
    if title == "서울바이오허브 공고":
        return None

    start, end = extract_biohub_period(lines, text)
    budget = extract_biohub_budget(lines, text)
    category = guess_biohub_category(title, text)
    elig = elig_from_text(title, text[:3500])
    biohub_item = normalize(
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
    mark_dates_unknown_if_needed(biohub_item, text, start_was_known=bool(start))
    return biohub_item


def discover_biohub_program_ids_from_list_html(html: str) -> List[Tuple[str, str]]:
    """목록 페이지(supportManageListPage.do) HTML에서 실제 존재하는 (seq, gubun) 쌍을
    전부 추출한다. 각 카드는 `supportManageViewPage('seq','gubun')` 형태의 JS 호출을
    이미지 링크와 "신청하기" 버튼 두 곳에 중복으로 담고 있으므로, seq 기준으로 먼저
    나온 것만 남긴다. gubun을 사이트가 알려주는 값을 그대로 쓰므로 gubun을 추측하거나
    여러 값을 대입해볼 필요가 없다."""
    seen: set = set()
    pairs: List[Tuple[str, str]] = []
    for m in re.finditer(r"supportManageViewPage\('(\d+)'\s*,\s*'(\w+)'\)", html):
        seq, gubun = m.group(1), m.group(2)
        if seq in seen:
            continue
        seen.add(seq)
        pairs.append((seq, gubun))
    return pairs


def fetch_biohub_program_list(
    base_url: str, timeout: int, page_size: int, list_url: Optional[str] = None
) -> List[Tuple[str, str]]:
    """목록 페이지를 POST로 요청해 (seq, gubun) 전체를 가져온다.

    이 페이지는 서버에서 렌더링되어 반환되므로(클라이언트 JS 없이도 GET만으로 1페이지
    분량은 보이지만), 게시판 폼의 miv_pageNo/miv_pageSize 파라미터를 그대로 POST로
    보내면 페이지네이션 없이 한 번의 요청으로 원하는 만큼의 이력을 받을 수 있다.

    `list_url`을 넘기면 그 URL을 그대로 쓴다 — 관리자 화면의 "소스 URL 재정의"
    패널(server.py의 OVERRIDABLE_SOURCES)로 사이트 구조 변경에 대응할 수 있도록
    하기 위함이다. 넘기지 않으면 base_url 기준 기본 경로를 사용한다.
    """
    url = clean(list_url) or f"{base_url}/front/supportManageReq/supportManageListPage.do"
    r = SESSION.post(url, data={"miv_pageNo": "1", "miv_pageSize": str(page_size)}, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return discover_biohub_program_ids_from_list_html(r.text)


def collect_biohub_direct(bcfg: Dict[str, Any], common: Dict[str, Any]) -> List[Dict[str, Any]]:
    """서울바이오허브 전용 수집기.

    더 이상 collect_all()이 평시 수집에 쓰지 않는다(_collect_via_stored_recipe()로
    대체됨) — 만일을 위해 코드만 남겨뒀다.

    일반 게시판 크롤러와 달리 서울바이오허브는 상세 URL인
    supportManageView.do?gubun=..&seq=.. 페이지를 직접 파싱한다.
    1) 목록 페이지(supportManageListPage.do)를 POST로 조회해 실제 존재하는
       (seq, gubun) 쌍을 전부 가져온다. seq를 추측하지 않고 gubun도 사이트가
       알려주는 값을 그대로 쓰므로, 존재하지 않는 seq에 대한 유령 페이지나
       잘못된 gubun 조합으로 인한 낭비/오탐이 원천적으로 발생하지 않는다.
    2) 위 방식이 실패하면(사이트 구조 변경 등) config의 seed_urls로 보완한다.
       (예전에는 여기서 seq 범위를 추측 스캔하는 3단계가 더 있었으나, 1)이
       실제 목록을 정확히 가져오게 되면서 추측 스캔은 항상 유령 페이지만
       만들어내는 순수 손해였으므로 완전히 제거했다.)
    """
    if not bcfg.get("enabled", False):
        raise RuntimeError("비활성화됨")
    base_url = clean(bcfg.get("base_url")) or "https://www.seoulbiohub.kr"
    timeout = common.get("timeout_sec", 20)
    max_items = resolve_max_items(common.get("max_items_per_source", 80))
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

    # 1) 목록 페이지를 POST로 조회해 (seq, gubun) 쌍을 가져온다. list_url이 설정되어
    #    있으면 그 값을 쓰고(관리자 URL 재정의 대응), 없으면 base_url 기준 기본
    #    경로를 쓴다. 요청이 막히면(예: 사이트가 구조를 바꿔 이 엔드포인트가
    #    사라진 경우) 실패만 기록하고 아래 fallback 단계로 넘어간다.
    list_url = clean(bcfg.get("list_url") or bcfg.get("list_urls", [None])[0] or "") or None
    effective_list_url = list_url or f"{base_url}/front/supportManageReq/supportManageListPage.do"
    try:
        if not (common.get("respect_robots", True) and not robots_allows(effective_list_url)):
            pairs = fetch_biohub_program_list(
                base_url, timeout,
                page_size=max_items,
                list_url=list_url,
            )
            for seq, gubun in pairs:
                add_url(f"{base_url}/front/supportManageReq/supportManageView.do?seq={seq}&gubun={gubun}")
    except Exception:
        pass

    # 2) 목록 조회가 아무것도 못 찾았을 때만 seed_urls로 보완한다.
    if not candidates:
        for u in bcfg.get("seed_urls") or []:
            add_url(u)

    out: List[Dict[str, Any]] = []
    used_title_keys = set()

    for url in candidates:
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
        raise RuntimeError("서울바이오허브 0건: 목록 페이지(list_page) 또는 seed_urls 확인 필요")
    return out


def deduplicate(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """같은 소스 안에서 페이지네이션이 겹쳐 똑같은 항목이 두 번 긁힌 경우만 걸러낸다.

    url이 있으면 그것만으로 판단한다(같은 공고면 url도 같다) — 제목만 보고
    정규화 문자열(공백/괄호 등 제거, 48자 절단)로 비교하면 "OO 모집 (지역)"과
    "OO 모집(지역)"처럼 실제로는 다른 두 공고(K-Startup pbancSn이 다름)를 같은
    항목으로 잘못 합쳐버릴 수 있다. url이 없는 경우(옛 게시판 콜렉터 등)에만
    제목 정규화로 대체한다."""
    seen = set()
    out = []
    for a in items:
        url = clean(a.get("url") or "")
        key = url or re.sub(r"[\s\[\](){}<>〔〕·,._-]", "", a.get("title", ""))[:48]
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


MERGE_DATE_TOLERANCE_DAYS = 1


def _dates_close(d1: str, d2: str) -> bool:
    return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days) <= MERGE_DATE_TOLERANCE_DAYS


def _merge_dates_identical(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[bool]:
    """두 공고의 날짜가 "같다"고 볼 수 있는지 판단한다(하루 정도 차이는 허용 —
    같은 공고를 여러 소스가 하루 어긋나게 표기하는 경우가 있어서다).

    True/False 둘로만 답하지 않고 "판단 불가"(None)도 반환한다 — 필요한 날짜
    정보가 없으면 "다르다"가 아니라 그냥 신호가 없는 것으로 취급해야, 퍼지
    제목 유사도만으로 실제로는 다른 공고를 잘못 합치는 걸 막을 수 있다(날짜
    불명 공고끼리는 우연히 둘 다 비어있다고 "같다"고 보면 안 된다).

    - 둘 다 상시(rolling_confirmed) 공고면 시작일만 비교한다 — 상시 공고는
      실제 의미 있는 마감일이 없어서(수시/소진 시 마감) end 비교가 의미 없다.
    - 그 외에는 시작일·종료일이 둘 다 있어야 하고, 둘 다 하루 이내로 같아야
      한다.
    """
    a_start, b_start = a.get("start"), b.get("start")
    if a.get("rolling_confirmed") and b.get("rolling_confirmed"):
        if not a_start or not b_start:
            return None
        try:
            return _dates_close(a_start, b_start)
        except ValueError:
            return None
    a_end, b_end = a.get("end"), b.get("end")
    if not (a_start and a_end and b_start and b_end):
        return None
    try:
        return _dates_close(a_start, b_start) and _dates_close(a_end, b_end)
    except ValueError:
        return None


def merge_duplicate_notices(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """같은 공고가 여러 사이트(또는 같은 사이트의 흔들리는 URL)로 중복 수집된 경우
    하나의 공고로 합치고, 게재된 사이트 목록(sources)을 함께 붙인다.

    같은 공고로 보는 기준 두 가지 중 하나:
    1. 제목이 정규화 후 완전히 같다 — 이것만으로 충분하다(예: 신청 트랙만 다른
       "(정착,창업형)"/"(일반형)" 같은 괄호 안 표현은 정규화 과정에서 제거됨).
    2. 제목 유사도가 0.8 이상이고, 날짜도 같다(_merge_dates_identical 참고).
       유사도만으로는 부족하다 — 기관명 등 짧은 차이가 전체 제목 길이에
       묻혀서, 서로 다른 기관의 전혀 다른 공고(예: '안양대학교'/'영산대학교'
       창업보육센터 입주기업 모집)가 문구 패턴만 비슷해 잘못 합쳐지는 걸
       날짜 일치 요구로 막는다.
    """
    groups: List[List[Dict[str, Any]]] = []
    keys: List[str] = []
    for it in items:
        key = _merge_title_key(it.get("title"))
        placed = False
        for gi, gkey in enumerate(keys):
            if not key or not gkey:
                continue
            if key == gkey:
                groups[gi].append(it)
                placed = True
                break
            if abs(len(key) - len(gkey)) > max(len(key), len(gkey)) * 0.4:
                continue
            ratio = difflib.SequenceMatcher(None, key, gkey).ratio()
            if ratio < MERGE_TITLE_SIM_THRESHOLD:
                continue
            if _merge_dates_identical(it, groups[gi][0]) is not True:
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


def recover_source_via_recipe(
    source_id: str, list_url: str, common: Dict[str, Any], triggering_user_id: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """flag_source_anomalies()가 이 소스를 '갑자기 0건/급감'으로 표시했을 때 예비로 시도한다.

    먼저 저장된 레시피가 있으면 그대로 재실행해보고(레시피가 여전히 맞으면 LLM
    호출 없이 바로 성공), 없거나 그것도 실패하면 discover_recipe_agentic()으로
    새로 발견을 시도한다 — fetch_url 도구로 외부 JS 파일을 직접 열어보거나 후보
    URL을 실제로 검증해본 뒤 레시피를 제출하는 에이전틱 버전이다. 정적 페이지
    스냅샷만 보는 한 번의 호출보다 비용이 더 들지만(회당 여러 번의 LLM 호출),
    이 함수는 소스가 깨졌을 때만 드물게 실행되므로 정확도를 우선한다. 둘 다
    실패하면 None을 반환해 기존 '이상 감지' 표시를 그대로 둔다 — 이 함수가
    예외를 던지면 안 된다(collect_all()의 정상 흐름을 깨면 안 되므로 내부에서
    전부 처리한다).
    """
    import recipe_engine

    stored = database.get_source_recipe(source_id)
    if stored:
        try:
            return recipe_engine.run_recipe(source_id, stored["recipe"], common)
        except Exception:
            pass  # 저장된 레시피가 더 이상 안 맞을 수 있다 — 아래에서 새로 발견을 시도한다.

    # 재발견은 LLM을 여러 번 호출하므로 여기서부터만 model_id/api_key가 필요하다.
    profile = database.resolve_background_llm_profile(triggering_user_id)
    if not profile:
        return None
    model_id = profile["model_id"]
    api_key = auth.decrypt_secret(profile["key_enc"])
    try:
        r = SESSION.get(list_url, timeout=common.get("timeout_sec", 20))
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        sample = recipe_engine._strip_boilerplate_html(r.text)
        recipe = recipe_engine.discover_recipe_agentic(
            source_id, sample, list_url, "html", model_id, api_key, common
        )
        items = recipe_engine.run_recipe(source_id, recipe, common)
        if not items:
            return None
        database.set_source_recipe(source_id, recipe, verified_ok=True)
        return items
    except Exception:
        return None


def load_sample_items() -> List[Dict[str, Any]]:
    if not SAMPLE_PATH.exists():
        return []
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def _collect_via_stored_recipe(source_id: str, common: Dict[str, Any], cap: int) -> List[Dict[str, Any]]:
    """kstartup/kddf/nrf의 평시 수집 경로 — 예전에 검증해둔 손으로 쓴 파서 대신, 이미
    저장된 레시피를 선택자만으로 결정적으로 재실행한다(LLM 호출 없음). 레시피가 없거나
    실행이 깨지면 여기서 예외를 던져 호출부가 "오류"로 기록하게 하고, 그 뒤 이상 감지
    로직이 recover_source_via_recipe()로 자동 복구를 시도한다 — 손으로 쓴 파서는 그
    복구 경로에서도 더 이상 쓰지 않는다(collector.py의 collect_kstartup/collect_kddf/
    collect_nrf_iris 함수 자체는 참고용으로 남겨뒀을 뿐, collect_all()이 더 이상
    호출하지 않는다)."""
    import recipe_engine

    stored = database.get_source_recipe(source_id)
    if not stored:
        raise RuntimeError("레시피 없음")
    return recipe_engine.run_recipe(source_id, stored["recipe"], common)[:cap]


# collect_all()에서 소스 하나가 실패했을 때, 재시도로도 못 살아나면 이번 실행의
# 결과에서 빠진 채로 남는다 — 그런 상태(state)의 소스는 DB에 남아있던 기존 데이터를
# upsert_notices()의 prune 단계에서 지우지 않고 보존한다. "비활성화"(관리자가 일부러
# 끔)도 마찬가지로 보존 대상이다 — 소스를 끄는 건 "더는 새로 안 가져온다"는 뜻이지
# "이미 모아둔 공고를 지운다"는 뜻이 아니다(화면 노출 여부는 별개로
# get_disabled_source_ids()를 통해 조회 단계에서 걸러진다). "레시피 없음"(아직 한
# 번도 발견된 적 없는 신규 커스텀 소스)만 실패도 의도된 비활성화도 아닌 그냥 "아직
# 없음" 상태라 기존 동작(정리 대상)을 그대로 둔다.
PRESERVE_ON_FAILURE_STATES = {"오류", "차단(robots)", "비활성화"}


def get_disabled_source_ids() -> Set[str]:
    """관리자 화면에서 재정의 가능한 소스 중 현재 비활성화된 것들의 id를 반환한다.
    소스별 페이지(collect_all)뿐 아니라 조회 화면에서도 써서, 비활성화된 소스의
    기존 공고는 DB에는 남아있되(prune 대상 아님) 목록에는 노출되지 않게 한다."""
    cfg = load_config()
    overrides = database.get_source_overrides()
    boards = cfg.get("boards") or {}
    checks = [
        ("kstartup", cfg.get("kstartup", {})),
        ("bizinfo", cfg.get("bizinfo", {})),
        ("g2b", cfg.get("g2b", {})),
        ("nrf", boards.get("nrf", {})),
        ("kddf", boards.get("kddf", {})),
        ("biohub_direct", boards.get("biohub_direct", {})),
        ("khidi_direct", boards.get("khidi_direct", {})),
    ]
    return {
        sid for sid, raw_cfg in checks
        if not _apply_source_override(raw_cfg, sid, overrides).get("enabled", False)
    }


def _run_with_retry(fn: Callable[[], Any], attempts: int = 2, delay_sec: float = 2.0) -> Any:
    """소스 수집 함수 하나를 실행하고, 실패하면 한 번 더 시도한다(기본 총 2회).

    robots.txt 차단(PermissionError)은 재시도해도 결과가 달라지지 않으므로 즉시
    그대로 던진다 — 재시도 대상은 타임아웃/일시적 파싱 실패 같은 우발적 오류로 좁힌다."""
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            raise
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(delay_sec)
    assert last_exc is not None
    raise last_exc


def _apply_source_override(sub_cfg: Dict[str, Any], source_id: str, overrides: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """관리자가 재정의한 이름/URL/활성화 상태가 있으면 해당 소스 설정에 덮어쓴다."""
    override = overrides.get(source_id)
    if not override:
        return sub_cfg
    sub_cfg = dict(sub_cfg)
    if override.get("list_url"):
        sub_cfg["list_url"] = override["list_url"]
        sub_cfg["list_urls"] = [override["list_url"]]
    if override.get("name"):
        sub_cfg["name"] = override["name"]
    if override.get("enabled") is not None:
        sub_cfg["enabled"] = override["enabled"]
    return sub_cfg


def collect_all(write_db: bool = True, triggering_user_id: Optional[str] = None) -> CollectRun:
    cfg = load_config()
    overrides = database.get_source_overrides()
    run = CollectRun()
    common = cfg.get("common", {})
    cap = resolve_max_items(common.get("max_items_per_source", 60))
    board_list_urls: Dict[str, str] = {}

    # Bizinfo and routed institutional notices.
    bcfg = _apply_source_override(cfg.get("bizinfo", {}), "bizinfo", overrides)
    if bcfg.get("enabled", False):
        try:
            routed = _run_with_retry(lambda: collect_bizinfo({**common, **bcfg}, cfg.get("route_from_bizinfo", {})))
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
    gcfg = _apply_source_override(cfg.get("g2b", {}), "g2b", overrides)
    if gcfg.get("enabled", False):
        try:
            run.record("g2b", _run_with_retry(lambda: collect_g2b({**common, **gcfg}))[:cap], "정상")
        except Exception as e:
            run.record("g2b", [], "오류", e)
    else:
        run.record("g2b", [], "비활성화")

    # K-Startup.
    kcfg = _apply_source_override(cfg.get("kstartup", {}), "kstartup", overrides)
    if clean(kcfg.get("list_url") or KSTARTUP_DEFAULT_URL):
        # nrf/kddf 등 게시판과 마찬가지로, source_recipes에 저장된 레시피가 있으면
        # 아래 flag_source_anomalies() 이상 감지 시 recover_source_via_recipe()가
        # 이 소스도 복구 대상으로 볼 수 있게 board_list_urls에 등록해둔다.
        board_list_urls["kstartup"] = clean(kcfg.get("list_url") or KSTARTUP_DEFAULT_URL)
    kstartup_enabled = kcfg.get("enabled", False)
    if kstartup_enabled:
        try:
            items = _run_with_retry(lambda: _collect_via_stored_recipe("kstartup", common, cap))
            run.record("kstartup", items, "정상", name=kcfg.get("name"), method="레시피")
        except Exception as e:
            run.record("kstartup", [], "오류", e, name=kcfg.get("name"))
    else:
        run.record("kstartup", [], "비활성화", name=kcfg.get("name"))

    # Direct boards.
    for sid, raw_board_cfg in (cfg.get("boards") or {}).items():
        if sid.startswith("_"):
            continue
        board_cfg = _apply_source_override(raw_board_cfg, sid, overrides)
        if clean(board_cfg.get("list_url")):
            board_list_urls[sid] = clean(board_cfg.get("list_url"))
        if not board_cfg.get("enabled", False):
            run.record(sid, [], "비활성화", name=board_cfg.get("name"))
            continue
        try:
            if sid == "biohub_direct":
                items = _run_with_retry(lambda: _collect_via_stored_recipe(sid, common, cap))
                run.record(sid, items, "정상", name=board_cfg.get("name") or "서울바이오허브(직접)", method="레시피")
            elif sid == "nrf":
                items = _run_with_retry(lambda: _collect_via_stored_recipe(sid, common, cap))
                run.record(sid, items, "정상", name=board_cfg.get("name") or "한국연구재단", method="레시피")
            elif sid == "kddf":
                items = _run_with_retry(lambda: _collect_via_stored_recipe(sid, common, cap))
                run.record(sid, items, "정상", name=board_cfg.get("name") or "국가신약개발사업단", method="레시피")
            elif sid == "khidi_direct":
                items = _run_with_retry(lambda: collect_khidi_direct(board_cfg, common, triggering_user_id))[:cap]
                run.record(sid, items, "정상", name=board_cfg.get("name") or "보건산업진흥원/KHIDI", method="전용 파서(API)")
            else:
                items = _run_with_retry(lambda: collect_board(sid, board_cfg, common))[:cap]
                run.record(sid, items, "정상", name=board_cfg.get("name"), method="게시판")
        except PermissionError as e:
            run.record(sid, [], "차단(robots)", e, name=board_cfg.get("name"), method="게시판")
        except Exception as e:
            run.record(sid, [], "오류", e, name=board_cfg.get("name"), method="게시판")

    # 관리자가 URL만으로 등록한 커스텀 소스 — 손으로 쓴 수집기 없이 저장된 레시피만으로
    # 수집한다(선택자만으로 결정적으로 실행되므로 LLM 호출이 없다). board_list_urls에
    # 등록해두면, 아래 이상 감지/복구 루프가 다른 게시판 소스와 똑같이 이 소스도
    # 다뤄준다(레시피가 깨지면 재발견까지 자동으로 시도).
    import recipe_engine  # 지연 임포트: recipe_engine이 collector를 임포트하므로 순환 방지

    for cs in database.get_enabled_custom_sources():
        sid = cs["id"]
        board_list_urls[sid] = cs["list_url"]
        stored = database.get_source_recipe(sid)
        if not stored:
            run.record(sid, [], "레시피 없음", name=cs["name"], method="레시피")
            continue
        try:
            items = _run_with_retry(lambda: recipe_engine.run_recipe(sid, stored["recipe"], common))[:cap]
            run.record(sid, items, "정상", name=cs["name"], method="레시피")
        except PermissionError as e:
            run.record(sid, [], "차단(robots)", e, name=cs["name"], method="레시피")
        except Exception as e:
            run.record(sid, [], "오류", e, name=cs["name"], method="레시피")

    run.items = merge_duplicate_notices(run.items)
    used_sample = False

    if not run.items and cfg.get("use_sample_when_empty", True):
        sample = load_sample_items()
        run.record("sample", sample, "샘플 표시")
        run.items = merge_duplicate_notices(sample)
        used_sample = True

    if write_db:
        run.sources = database.flag_source_anomalies(run.sources)
        recovered_any = False
        for sid, entry in run.sources.items():
            if not entry.get("anomaly") or sid not in board_list_urls:
                continue
            recovered = recover_source_via_recipe(sid, board_list_urls[sid], common, triggering_user_id)
            if not recovered:
                continue
            recovered = recovered[:cap]
            run.items.extend(recovered)
            entry["n"] = len(recovered)
            entry["state"] = "레시피로 복구됨"
            entry["anomaly"] = False
            entry["anomaly_note"] = f"기존 수집이 실패해 레시피 기반으로 자동 복구되었습니다 ({len(recovered)}건)."
            recovered_any = True
        if recovered_any:
            run.items = merge_duplicate_notices(run.items)

        # 재시도까지 실패한 소스는 이번 실행 결과에 아무 항목도 없다 — 그렇다고
        # DB에 남아있던 기존 데이터까지 지우면 일시적 오류 한 번에 그 소스의 공고가
        # 전부 사라진다. 그런 소스의 기존 id는 prune 대상에서 제외해 보존한다.
        extra_keep_ids: List[str] = []
        for sid, entry in run.sources.items():
            if entry.get("state") in PRESERVE_ON_FAILURE_STATES:
                extra_keep_ids.extend(database.get_notice_ids_for_source(sid))

        database.upsert_notices(run.items, prune=not used_sample, extra_keep_ids=extra_keep_ids)
        database.record_source_history(run.sources)
        database.replace_source_status(run.sources)

        # classify는 merge_duplicate_notices()가 확정한(그리고 방금 upsert된) 최종
        # id를 써야 한다. merge_duplicate_notices()는 병합 여부와 무관하게 모든
        # 공고의 id를 정규화된 제목 기반으로 다시 계산하므로(make_id 호출부 참고),
        # 병합 *전* 스크래핑 시점의 원본 id를 그대로 쓰면 notices 테이블에 실제로
        # 저장된 id와 어긋나 save_funding_classifications()가 전부 조용히 버린다
        # (FK 존재 체크에 걸림). 그래서 반드시 run.items(merge 이후, upsert 이후)
        # 에서 kstartup 유래 항목만 뽑아 써야 한다.
        if kstartup_enabled:
            kstartup_final_items = [
                it for it in run.items
                if any(s.get("id") == "kstartup" for s in (it.get("sources") or []))
            ]
            if kstartup_final_items:
                try:
                    funding_classifier.classify_new_kstartup_notices(kstartup_final_items, common, triggering_user_id)
                except Exception:
                    pass
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
