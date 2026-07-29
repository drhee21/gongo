# -*- coding: utf-8 -*-
"""LLM을 이용해 공고와 회사 정보를 비교, 적합도를 판정한다."""
from __future__ import annotations

import json
from typing import Any, Dict, List

import llm

CHUNK_SIZE = 40

FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "fit": {"type": "string", "enum": ["fit", "unfit", "unsure"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "fit", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "너는 한국 정부지원사업 공고와 회사 프로필을 비교해 적합도를 판정하는 어시스턴트다.\n"
    "각 공고에 대해 회사가 실제로 지원 가능한지, 분야·지역·업력 요건과 사업 성격을 종합적으로 판단해서\n"
    "fit(적합) / unfit(부적합, 요건상 명백히 지원 불가) / unsure(정보 부족 또는 애매함) 중 하나로 분류하고,\n"
    "판단 근거를 한국어로 한 문장 이내로 짧게 설명해라. 전달받은 공고 전부에 대해 반드시 결과를 반환해라.\n"
    "공고에 '벤처기업' 또는 '기업부설연구소' 보유가 지원 자격 요건이나 가점 사항으로 언급되어 있으면,\n"
    "회사 프로필의 벤처기업 인증/기업부설연구소 보유 여부와 대조해서 판단에 반영해라.\n"
    "회사 키워드·분야와 공고 내용이 같은 범주에 속하면(예: '의료기기' 키워드에 백신·진단키트도 포함)\n"
    "세부 품목이 정확히 일치하지 않아도 적합으로 판단해라.\n"
    "같은 범주인지 애매하더라도 최선의 판단으로 fit 또는 unfit 중 하나를 선택해라.\n"
    "unsure는 공고에 필요한 정보 자체가 없을 때만 사용해라."
)


def _company_text(company: Dict[str, Any]) -> str:
    parts = []
    if company.get("name"):
        parts.append(f"회사명: {company['name']}")
    if company.get("years") not in (None, ""):
        parts.append(f"업력: {company['years']}년")
    if company.get("region"):
        parts.append(f"소재지: {company['region']}")
    if company.get("sector"):
        parts.append(f"주요 분야: {company['sector']}")
    if company.get("keywords"):
        if company.get("keyword_mode") == "and":
            parts.append(f"키워드(모두 관련되어야 적합으로 판단): {company['keywords']}")
        else:
            parts.append(f"키워드(하나 이상 관련되면 적합으로 판단): {company['keywords']}")
    parts.append(f"벤처기업 인증: {'보유' if company.get('venture') else '미보유'}")
    parts.append(f"기업부설연구소: {'보유' if company.get('rnd_center') else '미보유'}")
    return "\n".join(parts) or "회사 정보 없음 (일반적인 기준으로 판단)"


DOC_MAX_CHARS_PER_FILE = 3000
DOC_MAX_CHARS_TOTAL = 10000


def _documents_text(documents: List[Dict[str, Any]] | None) -> str:
    """업로드된 회사 문서들을 프롬프트에 넣을 텍스트로 합친다.

    문서가 많거나 길면 토큰 비용이 커지므로, 파일당/전체 글자수 상한을 둔다.
    """
    if not documents:
        return ""
    parts = []
    total = 0
    for doc in documents:
        content = (doc.get("content") or "")[:DOC_MAX_CHARS_PER_FILE]
        if total + len(content) > DOC_MAX_CHARS_TOTAL:
            content = content[: max(0, DOC_MAX_CHARS_TOTAL - total)]
        if not content:
            break
        note = "" if len(content) == len(doc.get("content") or "") else " (일부 생략됨)"
        parts.append(f"--- {doc.get('filename', '문서')}{note} ---\n{content}")
        total += len(content)
        if total >= DOC_MAX_CHARS_TOTAL:
            break
    return "\n\n".join(parts)


def _notice_brief(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": n.get("id"),
        "title": n.get("title"),
        "org": n.get("org"),
        "category": n.get("category"),
        "start": n.get("start"),
        "end": n.get("end"),
        "budget": n.get("budget"),
        "elig": n.get("elig"),
    }


def _chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def judge_company_fit(
    notices: List[Dict[str, Any]],
    company: Dict[str, Any],
    api_key: str,
    documents: List[Dict[str, Any]] | None = None,
    model_id: str = llm.DEFAULT_MODEL_ID,
) -> Dict[str, Dict[str, str]]:
    """공고 목록과 회사 정보를 LLM으로 비교해 각 공고의 적합도를 판정한다.

    `api_key`는 호출자(서버)가 요청한 사용자 본인의 API 키를 넘겨줘야 한다 —
    이 함수는 더 이상 config.json/환경변수에서 키를 찾지 않는다.
    `documents`는 사용자가 업로드한 회사 문서(파일명+추출된 텍스트) 목록으로,
    간단한 폼 필드보다 더 풍부한 판단 근거를 제공한다.
    `model_id`는 사용자가 '회사 정보'에서 선택한 모델이다 (llm.MODEL_CATALOG 참고).

    반환값: {notice_id: {"fit": "fit"|"unfit"|"unsure", "reason": str}}
    """
    company_text = _company_text(company)
    documents_text = _documents_text(documents)
    profile_block = company_text
    if documents_text:
        profile_block += f"\n\n[회사 관련 첨부 문서]\n{documents_text}"

    def call_batch(batch: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
        briefs = [_notice_brief(n) for n in batch]
        # 회사 정보/문서 블록은 청크마다 동일하게 반복되므로 별도 content block으로
        # 분리해 cache_control을 붙인다 — 같은 실행 안의 반복 호출이 캐시를 재사용해
        # 문서 텍스트만큼 토큰 비용이 배로 늘어나는 걸 막는다. (Anthropic 외 공급자는
        # 이 필드를 무시하고 텍스트만 사용한다.)
        user_content = [
            {
                "type": "text",
                "text": f"[회사 정보]\n{profile_block}",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"[공고 목록 ({len(briefs)}건, JSON)]\n{json.dumps(briefs, ensure_ascii=False)}",
            },
        ]

        data = llm.structured_call(
            model_id,
            api_key,
            SYSTEM_PROMPT,
            user_content,
            FIT_SCHEMA,
            max_tokens=16000,
            thinking={"type": "adaptive"},
        )
        result: Dict[str, Dict[str, str]] = {}
        for r in data.get("results", []):
            nid = r.get("id")
            if not nid:
                continue
            result[nid] = {"fit": r.get("fit") or "unsure", "reason": r.get("reason") or ""}
        return result

    out: Dict[str, Dict[str, str]] = {}
    for batch in _chunks(notices, CHUNK_SIZE):
        out.update(call_batch(batch))

        # 모델이 배치 안의 공고 일부를 응답에서 빠뜨리는 경우가 있다 —
        # 프롬프트에서 전부 반환하라고 지시해도 가끔 누락되므로, 빠진 공고만
        # 추려 한 번 더 요청해 채운다. 재시도에도 빠지면 그대로 결과 없음으로 둔다.
        missing = [n for n in batch if n.get("id") not in out]
        if missing:
            out.update(call_batch(missing))

    return out
