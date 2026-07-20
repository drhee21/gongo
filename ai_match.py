# -*- coding: utf-8 -*-
"""Claude API를 이용해 공고와 회사 정보를 비교, 적합도를 판정한다."""
from __future__ import annotations

import json
from typing import Any, Dict, List

MODEL = "claude-opus-4-8"
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
    "회사 프로필의 벤처기업 인증/기업부설연구소 보유 여부와 대조해서 판단에 반영해라."
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
        parts.append(f"키워드: {company['keywords']}")
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
) -> Dict[str, Dict[str, str]]:
    """공고 목록과 회사 정보를 Claude API로 비교해 각 공고의 적합도를 판정한다.

    `api_key`는 호출자(서버)가 요청한 사용자 본인의 Claude API 키를 넘겨줘야 한다 —
    이 함수는 더 이상 config.json/환경변수에서 키를 찾지 않는다.
    `documents`는 사용자가 업로드한 회사 문서(파일명+추출된 텍스트) 목록으로,
    간단한 폼 필드보다 더 풍부한 판단 근거를 제공한다.

    반환값: {notice_id: {"fit": "fit"|"unfit"|"unsure", "reason": str}}
    """
    import anthropic

    if not api_key:
        raise RuntimeError("Claude API 키가 없습니다. '회사 정보'에서 본인의 API 키를 먼저 등록해주세요.")

    client = anthropic.Anthropic(api_key=api_key)
    company_text = _company_text(company)
    documents_text = _documents_text(documents)
    profile_block = company_text
    if documents_text:
        profile_block += f"\n\n[회사 관련 첨부 문서]\n{documents_text}"

    out: Dict[str, Dict[str, str]] = {}
    for batch in _chunks(notices, CHUNK_SIZE):
        briefs = [_notice_brief(n) for n in batch]
        # 회사 정보/문서 블록은 청크마다 동일하게 반복되므로 별도 content block으로
        # 분리해 cache_control을 붙인다 — 같은 실행 안의 반복 호출이 캐시를 재사용해
        # 문서 텍스트만큼 토큰 비용이 배로 늘어나는 걸 막는다.
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

        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                output_config={"format": {"type": "json_schema", "schema": FIT_SCHEMA}},
                messages=[{"role": "user", "content": user_content}],
            ) as stream:
                response = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise RuntimeError("등록된 Claude API 키가 유효하지 않습니다. '회사 정보'에서 키를 다시 확인해주세요.")
        except anthropic.PermissionDeniedError:
            raise RuntimeError("이 Claude API 키로는 해당 모델을 사용할 권한이 없습니다.")
        except anthropic.RateLimitError:
            raise RuntimeError("Claude API 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요.")
        except anthropic.BadRequestError as e:
            msg = str(e)
            if "credit balance is too low" in msg:
                raise RuntimeError(
                    "Claude API 크레딧 잔액이 부족합니다. "
                    "console.anthropic.com의 Plans & Billing에서 결제 수단을 등록하거나 크레딧을 충전한 뒤 다시 시도해주세요."
                )
            raise RuntimeError(f"Claude API 요청이 거부되었습니다: {msg}")
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Claude API 오류가 발생했습니다: {e}")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            continue
        data = json.loads(text)
        for r in data.get("results", []):
            nid = r.get("id")
            if not nid:
                continue
            out[nid] = {"fit": r.get("fit") or "unsure", "reason": r.get("reason") or ""}

    return out
