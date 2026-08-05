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
import collector
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
    "너는 K-Startup 공고가 '스타트업/초기기업이 신청할 수 있는 실제 자금·펀딩(현금성 지원)'인지\n"
    "판정하는 어시스턴트다. 각 공고에는 제목과 함께 K-Startup 상세 페이지의 실제 필드\n"
    "(지원분야/창업업력/신청대상/제외대상/지원내용)가 주어진다. 이 실제 텍스트를 근거로만 판단해라.\n"
    "\n"
    "가장 중요한 원칙: 이 목록은 '자금·펀딩' 목록이다. 지원내용에 실제 현금성 지원(자금)이\n"
    "없으면, 다른 부분이 아무리 훌륭해 보여도(전담 컨설턴트 배정, 고급 멘토링, 사무공간 입주,\n"
    "투자자 소개 등) 무조건 exclude다. 자금이 없으면 그 즉시 탈락 — 이게 최우선 기준이며 다른\n"
    "어떤 판단보다 먼저, 그리고 반드시 적용된다.\n"
    "\n"
    "판정은 반드시 아래 순서로 한다.\n"
    "\n"
    "0단계 — 자금 존재 확인 (가장 먼저, 반드시 확인). 지원내용(그리고 제목)에 아래 같은 실제\n"
    "현금성 지원이 명시돼 있는지 본다:\n"
    "- 지원금ㆍ보조금ㆍ사업화자금ㆍ사업비ㆍ바우처처럼 금액이 지급되는 것\n"
    "- 융자ㆍ보증(대출 관련 실제 자금 지원)\n"
    "- 경진대회ㆍ공모전의 확정된 현금 상금\n"
    "- 확정된(검토·후보가 아닌) 직접 지분투자\n"
    "다음은 '자금'이 아니다 — 이것만 있고 위 항목이 전혀 없으면 그 즉시 exclude:\n"
    "- 컨설팅ㆍ멘토링ㆍ자문(1:1이든 그룹이든 상관없이 컨설팅 자체는 자금이 아니다)\n"
    "- 사무공간ㆍ입주ㆍ시설ㆍ장비 이용\n"
    "- 투자자 매칭ㆍIR 피칭 기회ㆍ데모데이 참가ㆍ투자 '검토'ㆍ투자 유치 '지원'(실제 지분투자가\n"
    "  확정된 게 아니라 기회/주선/검토 수준이면 자금이 아니다)\n"
    "- 교육ㆍ세미나ㆍ워크숍ㆍ네트워킹ㆍ행사 참가\n"
    "- 판로ㆍ마케팅ㆍ홍보 지원(현금 지급이 아닌 경우)\n"
    "- 인증ㆍ추천서ㆍ비자 등 행정적 지원\n"
    "지원내용 텍스트에 위 '자금 아님' 항목들만 나열되어 있고 실제 지급 금액/바우처/융자/상금/\n"
    "확정투자 중 어느 것도 없으면, 신청대상이 아무리 스타트업에 딱 맞아도 exclude다.\n"
    "\n"
    "1단계 — 0단계를 통과했을 때만 본다. 아래 중 하나라도 해당하면 exclude:\n"
    "- 신청대상에 대기업/중견기업/중소기업(연차 제한 없이)도 함께 명시되어 있어 스타트업으로\n"
    "  범위가 좁혀지지 않음\n"
    "- 신청대상이 업종ㆍ제품군ㆍ자격증처럼 창업 여부와 무관한 기준으로만 정의되고, 예비창업자ㆍ\n"
    "  창업 N년 이내ㆍ초기창업기업ㆍ벤처기업 같은 창업 관련 자격이 전혀 없음(즉 스타트업이 아닌\n"
    "  일반 업체도 그냥 신청 가능함)\n"
    "- 신청대상에 'N년 이상'처럼 설립 후 일정 기간이 지나야만 지원 가능하다고 되어 있음\n"
    "  (이건 스타트업이 아니라 그 반대 방향이다 — 헷갈리지 마라)\n"
    "- 신청대상이 스타트업 본인이 아니라 액셀러레이터·벤처투자회사·은행·회계법인 등\n"
    "  스타트업을 지원하는 쪽이거나, 임직원 개인 대상 복지성 지원임\n"
    "\n"
    "2단계 — keep(포함) 기준. 0단계(자금 있음)와 1단계(제외 아님)를 모두 통과해야 keep이다:\n"
    "- 지원내용에 0단계에서 확인한 실제 자금(지원금/바우처/융자·보증/확정 상금/확정 투자)이 있고\n"
    "- 신청대상/창업업력에 '예비창업자' 또는 'N년 이내/미만' 같은 설립 연차 상한, 또는 명시적\n"
    "  '창업기업ㆍ벤처기업' 자격이 확인됨\n"
    "\n"
    "위 기준으로도 판단이 안 서면(예: 정책상 특수 지정 시설명이라 배경지식이 필요한 경우처럼\n"
    "정말 애매하면) 최선의 판단으로 keep 또는 exclude 중 하나를 선택하고, reason에 왜 애매했는지\n"
    "짧게 적어라. reason에는 반드시 0단계(자금 확인 여부와 어떤 자금인지, 없으면 왜 없다고\n"
    "판단했는지)를 먼저 언급해라. 판단 근거를 한국어로 한 문장 이내로 설명해라. 전달받은 공고\n"
    "전부에 대해 반드시 결과를 반환해라."
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

    timeout = int(common.get("timeout_sec", 20))
    delay = float(common.get("request_delay_sec", 0.8))

    # 상세 페이지는 여기서 한 번만 가져온다 — keep/exclude 판정용 텍스트와
    # ai_match.py가 쓰는 elig(연차 상한/지역/분야)를 같은 요청에서 함께 뽑아 쓴다.
    # elig는 LLM 없이도 계산되는 규칙 기반 값이라 LLM 키 여부와 무관하게 채운다.
    briefs: List[Dict[str, Any]] = []
    elig_updates: Dict[str, Any] = {}
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
        elig_updates[nid] = collector.elig_from_text(
            item.get("title"),
            fields.get("support_area"),
            fields.get("age_tier"),
            fields.get("eligibility_text"),
            fields.get("benefit_text"),
        )
        time.sleep(delay)

    if elig_updates:
        database.update_notice_elig(elig_updates)

    if not briefs:
        return

    profile = database.resolve_background_llm_profile(triggering_user_id)
    if not profile:
        return
    model_id = profile["model_id"]
    api_key = auth.decrypt_secret(profile["key_enc"])

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
