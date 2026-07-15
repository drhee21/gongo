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


def judge_company_fit(notices: List[Dict[str, Any]], company: Dict[str, Any], api_key: str) -> Dict[str, Dict[str, str]]:
    """공고 목록과 회사 정보를 Claude API로 비교해 각 공고의 적합도를 판정한다.

    `api_key`는 호출자(서버)가 요청한 사용자 본인의 Claude API 키를 넘겨줘야 한다 —
    이 함수는 더 이상 config.json/환경변수에서 키를 찾지 않는다.

    반환값: {notice_id: {"fit": "fit"|"unfit"|"unsure", "reason": str}}
    """
    import anthropic

    if not api_key:
        raise RuntimeError("Claude API 키가 없습니다. '회사 정보'에서 본인의 API 키를 먼저 등록해주세요.")

    client = anthropic.Anthropic(api_key=api_key)
    company_text = _company_text(company)

    out: Dict[str, Dict[str, str]] = {}
    for batch in _chunks(notices, CHUNK_SIZE):
        briefs = [_notice_brief(n) for n in batch]
        user_content = (
            f"[회사 정보]\n{company_text}\n\n"
            f"[공고 목록 ({len(briefs)}건, JSON)]\n{json.dumps(briefs, ensure_ascii=False)}"
        )

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
