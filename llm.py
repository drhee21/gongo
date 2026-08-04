# -*- coding: utf-8 -*-
"""여러 LLM 공급자를 하나의 인터페이스로 호출한다.

공급자별로 직접 SDK를 붙이는 대신 litellm(여러 공급자를 하나의 OpenAI 호환
인터페이스로 감싸주는 라이브러리)을 통해 호출한다. 모델을 바꾸는 건
`MODEL_CATALOG`에 항목 하나를 추가하는 것과 사용자가 그 모델을 고르는 것뿐이고,
공급자별 분기/예외 처리를 이 파일에 따로 두지 않는다.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Union

import litellm

# litellm이 이해하지 못하는 공급자 전용 파라미터(예: OpenAI 모델에 Anthropic만의
# thinking을 넘기는 경우)를 에러 대신 조용히 무시하게 한다 — 그래야 llm.py가
# 공급자별로 파라미터를 걸러내는 코드를 따로 두지 않아도 된다.
litellm.drop_params = True

# Anthropic 모델로 가는 요청에 한해 system 메시지와 마지막 메시지에 자동으로
# prompt caching 브레이크포인트를 건다 (다른 공급자·미지원 모델이면 조용히
# 건너뛴다). recipe_engine.py의 에이전틱 레시피 발견처럼 매 턴 전체 대화
# 기록을 통째로 재전송하는 도구 호출 루프에서 반복되는 앞부분을 캐시로 읽어
# 비용/지연을 크게 줄여준다 — 호출부(tool_call/structured_call)는 아무것도
# 몰라도 된다.
litellm.enable_anthropic_prompt_caching = True

UserContent = Union[str, List[Dict[str, Any]]]

# id는 litellm이 요구하는 "<공급자>/<모델명>" 형식이다 — 이 접두어가 어떤 공급자
# API로 보낼지 결정하므로, litellm이 그 모델명을 몰라도(신규/커스텀 모델이어도)
# 그대로 해당 공급자에 전달된다.
MODEL_CATALOG: List[Dict[str, str]] = [
    {"id": "anthropic/claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "anthropic"},
    {"id": "anthropic/claude-sonnet-5", "label": "Claude Sonnet 5", "provider": "anthropic"},
    {"id": "anthropic/claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "provider": "anthropic"},
    {"id": "openai/gpt-5", "label": "GPT-5", "provider": "openai"},
    {"id": "openai/gpt-5-mini", "label": "GPT-5 mini", "provider": "openai"},
    # OpenRouter는 Anthropic/OpenAI를 직접 호출하는 것과 별개로, 다른 공급자 계열
    # 모델을 키 하나로 쓸 수 있게 해준다 — 여기서는 이미 위에서 직접 지원하는
    # Anthropic/OpenAI 대신, OpenRouter로만 접근 가능한 계열 위주로 추린다.
    {"id": "openrouter/google/gemini-3-pro-preview", "label": "Gemini 3 Pro (OpenRouter)", "provider": "openrouter"},
    {"id": "openrouter/google/gemini-2.5-flash", "label": "Gemini 2.5 Flash (OpenRouter)", "provider": "openrouter"},
    {"id": "openrouter/deepseek/deepseek-v3.2", "label": "DeepSeek V3.2 (OpenRouter)", "provider": "openrouter"},
    {"id": "openrouter/mistralai/mistral-large-2512", "label": "Mistral Large (OpenRouter)", "provider": "openrouter"},
    {"id": "openrouter/x-ai/grok-4", "label": "Grok 4 (OpenRouter)", "provider": "openrouter"},
]
MODEL_BY_ID: Dict[str, Dict[str, str]] = {m["id"]: m for m in MODEL_CATALOG}
DEFAULT_MODEL_ID = "anthropic/claude-opus-4-8"
PROVIDER_LABELS = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI", "openrouter": "OpenRouter"}


def provider_of(model_id: str) -> str:
    entry = MODEL_BY_ID.get(model_id)
    if entry:
        return entry["provider"]
    return model_id.split("/", 1)[0]


def _strip_cache_control_if_unsupported(user_content: UserContent, provider: str) -> UserContent:
    # 프롬프트 캐싱(cache_control)은 Anthropic 전용 확장이다. 다른 공급자로 보내면
    # 알 수 없는 필드로 거부될 수 있어서, Anthropic이 아닐 때만 제거한다.
    if provider == "anthropic" or isinstance(user_content, str):
        return user_content
    return [{k: v for k, v in block.items() if k != "cache_control"} for block in user_content]


def _call(model_id: str, api_key: str, **kwargs: Any):
    """litellm.completion()을 공통 예외 처리로 감싼다. 원본 응답 객체를 그대로 반환한다."""
    if not api_key:
        raise RuntimeError("API 키가 없습니다. '회사 정보'에서 API 키를 먼저 등록해주세요.")
    try:
        return litellm.completion(model=model_id, api_key=api_key, **kwargs)
    except litellm.AuthenticationError:
        raise RuntimeError("등록된 API 키가 유효하지 않습니다. '회사 정보'에서 키를 다시 확인해주세요.")
    except litellm.PermissionDeniedError:
        raise RuntimeError("이 API 키로는 해당 모델을 사용할 권한이 없습니다.")
    except litellm.RateLimitError:
        raise RuntimeError("API 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요.")
    except litellm.BadRequestError as e:
        msg = str(e)
        if "credit balance is too low" in msg or "insufficient_quota" in msg:
            raise RuntimeError("API 크레딧/한도가 부족합니다. 공급자 콘솔에서 결제 수단을 등록하거나 충전한 뒤 다시 시도해주세요.")
        raise RuntimeError(f"API 요청이 거부되었습니다: {msg}")
    except litellm.APIError as e:
        raise RuntimeError(f"API 오류가 발생했습니다: {e}")


def structured_call(
    model_id: str,
    api_key: str,
    system: str,
    user_content: UserContent,
    json_schema: Dict[str, Any],
    **extra: Any,
) -> Dict[str, Any]:
    """모델에 관계없이 동일한 시그니처로 구조화된 JSON 응답을 요청한다.

    `model_id`는 MODEL_CATALOG의 id (예: "anthropic/claude-sonnet-5")다.
    `extra`는 특정 공급자에서만 의미 있는 추가 옵션(예: Anthropic의 `thinking`,
    모든 공급자가 쓰는 `max_tokens`)이며, litellm.drop_params 설정 덕분에
    지원하지 않는 공급자로 가면 조용히 무시된다.
    """
    provider = provider_of(model_id)
    user_content = _strip_cache_control_if_unsupported(user_content, provider)
    response = _call(
        model_id,
        api_key,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "structured_response", "schema": json_schema, "strict": True},
        },
        **extra,
    )
    content = response.choices[0].message.content
    if not content:
        return {}
    return json.loads(content)


def tool_call(
    model_id: str,
    api_key: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    **extra: Any,
) -> Any:
    """도구 호출이 가능한 대화 한 턴을 요청하고, litellm 응답 메시지를 그대로 반환한다.

    structured_call()과 달리 한 번에 끝나는 게 아니라, 호출부가 직접 루프를 돌며
    `message.tool_calls`를 확인하고 도구를 실행한 뒤 결과를 messages에 append해서
    다시 불러야 한다 (에이전틱 루프). `messages`는 OpenAI 호환 형식
    (`{"role": ..., "content": ...}`, 도구 결과는 `{"role": "tool", "tool_call_id": ..., "content": ...}`)
    이며, litellm이 공급자별 형식으로 변환해준다.
    """
    response = _call(model_id, api_key, messages=messages, tools=tools, tool_choice="auto", **extra)
    return response.choices[0].message
