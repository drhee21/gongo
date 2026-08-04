# -*- coding: utf-8 -*-
"""K-Startup 공고가 실제로 '스타트업/초기기업 대상 자금·지원'인지 판정한다.

배경: 사장님이 "교육/설명회/세미나 같은 건 걸러내고 스타트업 대상 자금·지원 공고만
보고 싶다"고 요청했고, 제목만 보고 규칙(키워드)으로 거르는 방식은 173건을 실제로
하나씩 검토해보니 정확도가 부족하다는 게 확인됐다(예: 제목/카테고리 태그는
"창업"스러워 보여도 실제 신청대상은 "설립 3년 이상"으로 오히려 초기 스타트업을
배제하는 경우, 반대로 "중소기업" 일반 대상인데 연차 구간 드롭다운만 붙어있는 경우
등). 그래서 K-Startup 상세 페이지가 실제로 공개하는 구조화 필드(지원분야/창업업력/
신청대상/지원내용)를 가져와 그 실제 텍스트를 근거로 LLM이 판정하게 한다 — 제목만
보고 추측하지 않는다.

비용 관리: collect_all()이 매번 전체 공고를 다시 판정하지 않도록,
database.get_unclassified_ids()로 아직 한 번도 판정되지 않은 새 공고만 골라
LLM을 호출한다(같은 공고는 평생 한 번만 판정됨 — ai_fit과 동일한 캐싱 패턴).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import requests
from bs4 import BeautifulSoup

import auth
import database
import llm

CHUNK_SIZE = 30

KSTARTUP_DETAIL_SELECTORS = {
    "support_area": 'p.tit:-soup-contains("지원분야") + p.txt',
    "age_tier": 'p.tit:-soup-contains("창업업력") + p.txt',
    "eligibility_text": 'p.tit:-soup-contains("신청대상") + p.txt',
    "exclusion_text": 'p.tit:-soup-contains("제외대상") + p.txt',
    "benefit_text": 'div.information_list:has(p.title:-soup-contains("지원내용")) ul.dot_list-wrap',
}

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["keep", "exclude"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "verdict", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

CLASSIFY_SYSTEM_PROMPT = (
    "너는 K-Startup 공고가 '스타트업/초기기업이 신청할 수 있는 실질적인 자금 또는 지원'인지\n"
    "판정하는 어시스턴트다. 각 공고에는 제목과 함께 K-Startup 상세 페이지의 실제 필드\n"
    "(지원분야/창업업력/신청대상/제외대상/지원내용)가 주어진다. 이 실제 텍스트를 근거로만 판단해라.\n"
    "\n"
    "keep(포함) 기준 — 아래 중 하나라도 명확히 확인되면 keep:\n"
    "- 신청대상/창업업력에 '예비창업자' 또는 'N년 이내/미만' 같은 설립 연차 상한이 명시됨\n"
    "- 지원내용에 실제 현금(지원금·상금·보증금 등), 사무공간 입주, 1:1 컨설팅/멘토링,\n"
    "  투자자 매칭·피칭 등 실질적인 지원이 포함됨\n"
    "\n"
    "exclude(제외) 기준 — 아래 중 하나라도 해당하면 keep 신호가 있어도 exclude:\n"
    "- 신청대상에 대기업/중견기업/중소기업(연차 제한 없이)도 함께 명시되어 있어 스타트업으로\n"
    "  범위가 좁혀지지 않음\n"
    "- 신청대상에 'N년 이상'처럼 설립 후 일정 기간이 지나야만 지원 가능하다고 되어 있음\n"
    "  (이건 스타트업이 아니라 그 반대 방향이다 — 헷갈리지 마라)\n"
    "- 신청대상이 스타트업 본인이 아니라 액셀러레이터·벤처투자회사·은행·회계법인 등\n"
    "  스타트업을 지원하는 쪽이거나, 임직원 개인 대상 복지성 지원임\n"
    "- 지원분야가 '행사ㆍ네트워크'이고 지원내용에 현금(지원금/상금 지급)이 없음(밋업/네트워킹/\n"
    "  시상식 참가 기회뿐)\n"
    "- 지원분야가 '창업교육'이고 지원내용이 강의/코칭/멘토링뿐, 실질적 자금·공간 지원이 없음\n"
    "\n"
    "위 기준으로도 판단이 안 서면(예: 정책상 특수 지정 시설명이라 배경지식이 필요한 경우처럼\n"
    "정말 애매하면) 최선의 판단으로 keep 또는 exclude 중 하나를 선택하고, reason에 왜 애매했는지\n"
    "짧게 적어라. 판단 근거를 한국어로 한 문장 이내로 설명해라. 전달받은 공고 전부에 대해\n"
    "반드시 결과를 반환해라."
)


def _fetch_kstartup_detail_fields(url: str, timeout: int) -> Dict[str, str]:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "GongoMoa/3.0 (internal notice aggregator; contact: local)"})
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    out: Dict[str, str] = {}
    for key, selector in KSTARTUP_DETAIL_SELECTORS.items():
        el = soup.select_one(selector)
        out[key] = el.get_text(" ", strip=True) if el else ""
    return out


def _notice_brief(item: Dict[str, Any], fields: Dict[str, str]) -> Dict[str, Any]:
    return {
        "id": item["id"],
        "title": item.get("title"),
        "support_area": fields.get("support_area"),
        "age_tier": fields.get("age_tier"),
        "eligibility_text": fields.get("eligibility_text"),
        "exclusion_text": fields.get("exclusion_text"),
        "benefit_text": fields.get("benefit_text"),
    }


def _chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def classify_new_kstartup_notices(
    items: List[Dict[str, Any]], common: Dict[str, Any], triggering_user_id: str | None = None
) -> None:
    """`items`(방금 수집한 kstartup 공고 전체) 중 아직 판정되지 않은 것만 골라
    상세 페이지를 가져오고 LLM으로 keep/exclude를 판정해 DB에 저장한다.

    LLM 키가 없으면 조용히 건너뛴다(판정 전 공고는 기본적으로 화면에 계속
    보이므로, 키가 없다고 수집이 실패하거나 공고가 숨겨지지 않는다). 이 함수는
    예외를 던지지 않는다 — collect_all()의 정상 흐름을 깨면 안 된다.
    """
    by_id = {it["id"]: it for it in items if it.get("id")}
    new_ids = database.get_unclassified_ids(list(by_id.keys()))
    if not new_ids:
        return

    profile = database.resolve_background_llm_profile(triggering_user_id)
    if not profile:
        return
    model_id = profile["model_id"]
    api_key = auth.decrypt_secret(profile["key_enc"])

    timeout = int(common.get("timeout_sec", 20))
    delay = float(common.get("request_delay_sec", 0.8))

    briefs: List[Dict[str, Any]] = []
    for nid in new_ids:
        item = by_id[nid]
        url = item.get("url")
        if not url:
            continue
        try:
            fields = _fetch_kstartup_detail_fields(url, timeout)
        except Exception:
            continue
        briefs.append(_notice_brief(item, fields))
        time.sleep(delay)

    if not briefs:
        return

    results: Dict[str, Dict[str, str]] = {}
    for batch in _chunks(briefs, CHUNK_SIZE):
        try:
            data = llm.structured_call(
                model_id,
                api_key,
                CLASSIFY_SYSTEM_PROMPT,
                f"[K-Startup 공고 {len(batch)}건, JSON]\n" + json.dumps(batch, ensure_ascii=False),
                CLASSIFY_SCHEMA,
                max_tokens=8000,
            )
        except Exception:
            continue
        for r in data.get("results", []):
            nid = r.get("id")
            if not nid:
                continue
            results[nid] = {"verdict": r.get("verdict") or "keep", "reason": r.get("reason") or ""}

    if results:
        database.save_funding_classifications(results, method="llm")
