# -*- coding: utf-8 -*-
"""LLM이 한 번 분석해서 만든 '레시피'로 공고 사이트를 결정적으로 수집한다.

`discover_recipe()`가 사이트 구조를 LLM에 한 번 보여주고 선택자/필드 매핑을
돌려받으면(레시피), `run_recipe()`는 그 레시피를 코드로만 반복 실행한다 —
매 수집마다 LLM을 다시 부르지 않는다. 새 사이트 등록과 "사이트 구조가 바뀌어
갑자기 0건이 됨" 복구가 둘 다 이 두 함수를 그대로 사용한다.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment

import collector
import llm

FIELD_SPEC = {
    "type": "object",
    "properties": {
        "selector": {"type": ["string", "null"], "description": "항목 요소 기준 CSS 선택자. 항목 자체 텍스트를 쓰려면 null"},
        "attr": {"type": ["string", "null"], "description": "가져올 HTML 속성명(예: href). 텍스트를 쓰려면 null"},
        "regex": {"type": ["string", "null"], "description": "추출한 값에서 원하는 부분만 뽑는 정규식. 그룹을 여러 개 잡아서 template과 같이 쓸 수 있다. 불필요하면 null"},
        "template": {
            "type": ["string", "null"],
            "description": "regex로 잡은 그룹들을 {1},{2}... 로 참조해 최종 값을 조립하는 템플릿 (예: 여러 인자로 URL을 만들 때). 그룹 하나만 그대로 쓰면 null",
        },
    },
    "required": ["selector", "attr", "regex", "template"],
    "additionalProperties": False,
}

RECIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "fetch": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "format": {"type": "string", "enum": ["html", "json", "xml"]},
                "pagination": {
                    "type": "object",
                    "properties": {
                        "param": {"type": ["string", "null"], "description": "페이지 번호를 넣는 쿼리 파라미터명. 페이지네이션이 없으면 null"},
                        "type": {"type": "string", "enum": ["increment", "none"]},
                        "start": {"type": "integer", "description": "첫 페이지 번호 (보통 1)"},
                    },
                    "required": ["param", "type", "start"],
                    "additionalProperties": False,
                },
            },
            "required": ["url", "format", "pagination"],
            "additionalProperties": False,
        },
        "item_selector": {
            "type": "string",
            "description": "format이 html이면 개별 공고 항목을 가리키는 CSS 선택자. json/xml이면 목록에 이르는 점(.) 구분 경로",
        },
        "field_map": {
            "type": "object",
            "properties": {
                "title": FIELD_SPEC,
                "url": FIELD_SPEC,
                "org": FIELD_SPEC,
                "start": FIELD_SPEC,
                "end": FIELD_SPEC,
            },
            "required": ["title", "url", "org", "start", "end"],
            "additionalProperties": False,
        },
        "mode": {
            "type": "string",
            "enum": ["structured", "llm_direct"],
            "description": "structured: 위 선택자로 결정적으로 파싱. llm_direct: 선택자로 담기 어려울 만큼 불규칙해서 매 수집마다 LLM이 직접 추출해야 함",
        },
    },
    "required": ["fetch", "item_selector", "field_map", "mode"],
    "additionalProperties": False,
}

DISCOVER_SYSTEM_PROMPT = (
    "너는 한국 정부지원사업 공고 목록 페이지의 구조를 분석해서, 이후 코드가 매번 LLM 없이도\n"
    "그대로 재사용할 수 있는 '수집 레시피'를 만드는 어시스턴트다. 전달받은 페이지 원본(HTML/JSON/XML)을\n"
    "분석해서 공고 목록의 각 항목을 가리키는 선택자와, 항목별 제목/링크/기관/시작일/종료일을\n"
    "뽑아내는 필드별 선택자를 찾아라.\n"
    "- CSS 선택자는 실제 페이지에 있는 클래스/태그만 사용해라.\n"
    "- pagination.param은 전달받은 텍스트 안에 그 정확한 쿼리 파라미터 문자열이 실제로 보일 때만\n"
    "  채워라. 페이지네이션 링크/버튼이 텍스트에 안 보이면(잘려서 없거나 원래 없거나) 절대\n"
    "  'pageNo', 'page' 같은 흔한 이름을 추측해서 채우지 마라 — 반드시 null로 남겨라. 잘못된\n"
    "  파라미터는 없는 것보다 나쁘다. 이후 코드가 null이면 페이지네이션 없이 첫 페이지만 쓴다.\n"
    "- 날짜가 '2026.01.01 ~ 2026.02.01'처럼 한 필드에 같이 있으면 start/end 모두 그 필드의 selector를\n"
    "  가리키게 하고 regex로 각각 앞/뒤 날짜만 뽑아라.\n"
    "- 선택자 몇 개로는 도저히 안정적으로 추출할 수 없을 만큼 구조가 불규칙하면, mode를 llm_direct로\n"
    "  하고 나머지 필드는 최선의 추정치로 채워라."
)


def _content_for_prompt(source_id: str, sample_content: str) -> str:
    return f"[소스 ID: {source_id}]\n[페이지 원본]\n{sample_content}"


def _strip_boilerplate_html(html_text: str) -> str:
    """head/script/style/nav/footer/이미지/영상/주석 등을 제거해 본문(목록) 내용이 글자수 상한 안에 들어오게 한다."""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "head", "nav", "footer", "header", "noscript", "svg", "img", "video"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    body = soup.find("body") or soup
    return collector.clean(body.decode())


def discover_recipe(
    source_id: str,
    sample_content: str,
    list_url: str,
    fmt: str,
    model_id: str,
    api_key: str,
) -> Dict[str, Any]:
    """샘플 페이지 내용을 LLM에 한 번 보여주고 재사용 가능한 레시피를 받는다."""
    recipe = llm.structured_call(
        model_id,
        api_key,
        DISCOVER_SYSTEM_PROMPT,
        _content_for_prompt(source_id, sample_content),
        RECIPE_SCHEMA,
        max_tokens=4000,
    )
    # 호출자가 넘긴 list_url이 진짜 수집 대상 URL이다 — LLM이 페이지 안의 다른
    # 링크를 보고 그럴듯하지만 틀린 URL을 추측해 반환하는 경우가 있어서, 항상
    # 호출자 값으로 덮어쓴다 (LLM에게는 fetch.url을 채우라고 요청하지 않는 편이
    # 낫지만, 스키마가 요구 필드라 결과에 남는다).
    recipe.setdefault("fetch", {})["url"] = list_url
    recipe["fetch"]["format"] = fmt
    return recipe


def _apply_field(value: Optional[str], spec: Dict[str, Any]) -> Optional[str]:
    if value is None:
        return None
    regex = spec.get("regex")
    if not regex:
        return value
    import re

    m = re.search(regex, value)
    if not m:
        return None
    template = spec.get("template")
    if template:
        out = template
        for i, g in enumerate(m.groups(), start=1):
            out = out.replace("{" + str(i) + "}", g or "")
        return out
    return m.group(1) if m.groups() else m.group(0)


def _extract_html_field(item_el, spec: Dict[str, Any]) -> Optional[str]:
    selector = spec.get("selector")
    target = item_el.select_one(selector) if selector else item_el
    if target is None:
        return None
    attr = spec.get("attr")
    raw = target.get(attr) if attr else target.get_text(" ", strip=True)
    return _apply_field(collector.clean(raw) if raw else raw, spec)


def _json_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _extract_json_field(item: Any, spec: Dict[str, Any]) -> Optional[str]:
    selector = spec.get("selector")
    raw = _json_path(item, selector) if selector else item
    if raw is None:
        return None
    return _apply_field(str(raw), spec)


def _fetch_page(url: str, fmt: str, timeout: int) -> str:
    r = collector.SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    if fmt == "html" and (not r.encoding or r.encoding.lower() == "iso-8859-1"):
        r.encoding = r.apparent_encoding
    return r.text


def _paged_urls(recipe: Dict[str, Any], max_pages: int = 20) -> List[str]:
    fetch = recipe["fetch"]
    url = fetch["url"]
    pagination = fetch.get("pagination") or {}
    if pagination.get("type") != "increment" or not pagination.get("param"):
        return [url]
    param = pagination["param"]
    start = int(pagination.get("start", 1))
    sep = "&" if "?" in url else "?"
    return [url if p == start else f"{url}{sep}{param}={p}" for p in range(start, start + max_pages)]


EXTRACT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": ["string", "null"]},
                    "org": {"type": ["string", "null"]},
                    "start": {"type": ["string", "null"], "description": "YYYY-MM-DD 또는 모르면 null"},
                    "end": {"type": ["string", "null"], "description": "YYYY-MM-DD 또는 모르면 null"},
                },
                "required": ["title", "url", "org", "start", "end"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

EXTRACT_SYSTEM_PROMPT = (
    "너는 한국 정부지원사업 공고 목록 페이지 원본(HTML)에서 공고 항목을 전부 추출하는 어시스턴트다.\n"
    "각 공고의 제목, 상세 링크(URL, 상대경로면 그대로), 주관기관, 접수 시작일/종료일(YYYY-MM-DD, 모르면 null)을\n"
    "뽑아라. 목록에 보이는 공고 전부를 반환해라 (메뉴/배너/광고 등 공고가 아닌 요소는 제외)."
)


def _run_llm_direct(
    source_id: str,
    recipe: Dict[str, Any],
    common: Dict[str, Any],
    model_id: str,
    api_key: str,
) -> List[Dict[str, Any]]:
    """선택자로 안정적으로 담기 어려운 사이트를 위한 예비 실행 경로 — 매 수집마다 LLM을 부른다.

    레시피의 mode가 'llm_direct'로 저장된 소스에서만 쓰인다. 비용이 페이지 수만큼
    매번 발생하므로, 이 경로를 타는 소스가 admin 화면에 드러나야 한다 (Task #8에서 연결).
    """
    fetch = recipe["fetch"]
    fmt = fetch.get("format", "html")
    timeout = int(common.get("timeout_sec", 20))
    delay = float(common.get("request_delay_sec", 0.8))
    max_items = int(common.get("max_items_per_source", 60))
    base_url = fetch["url"]

    if common.get("respect_robots", True) and not collector.robots_allows(base_url):
        raise PermissionError("robots.txt 차단")

    out: List[Dict[str, Any]] = []
    prev_count = 0
    for url in _paged_urls(recipe):
        text = _fetch_page(url, fmt, timeout)
        if fmt == "html":
            text = _strip_boilerplate_html(text)
        data = llm.structured_call(
            model_id, api_key,
            EXTRACT_SYSTEM_PROMPT,
            text,
            EXTRACT_ITEM_SCHEMA,
            max_tokens=8000,
        )
        page_items = data.get("items") or []
        if not page_items:
            break
        for fields in page_items:
            title = fields.get("title")
            if not title:
                continue
            start = collector.parse_date(fields.get("start"))
            end = collector.parse_date(fields.get("end"))
            item = collector.normalize(
                source_id, title, fields.get("org"), "기타", start, end,
                fields.get("url"), raw={"collector": "recipe_engine_llm_direct", "source_id": source_id},
            )
            collector.mark_dates_unknown_if_needed(item, title, start_was_known=bool(start))
            out.append(item)
        # 페이지 안에 중복 DOM(모바일/데스크톱 버전 등)이 섞여 있는 사이트가 있어서,
        # 중복 제거 전 개수로 max_items 도달 여부를 판단하면 아직 안 채워졌는데도
        # 다음 페이지를 안 가져오는 일이 생긴다 — 매 페이지마다 중복 제거 후 판단한다.
        out = collector.deduplicate(out)
        if len(out) >= max_items:
            break
        if len(out) == prev_count:
            # 직전 페이지 대비 새 항목이 하나도 안 늘었다 — 페이지네이션 파라미터가
            # 실제 사이트와 안 맞아서 같은 페이지를 반복해서 받고 있을 수 있다
            # (예: LLM이 추측한 쿼리 파라미터가 틀린 경우). 더 가봐야 어차피 같은
            # 내용일 가능성이 높으므로, 페이지당 LLM 호출 비용만 낭비하지 않게
            # 여기서 멈춘다.
            break
        prev_count = len(out)
        time.sleep(delay)

    if not out:
        raise RuntimeError(f"레시피(llm_direct) 실행 결과 0건: '{source_id}'")
    return collector.deduplicate(out)[:max_items]


def run_recipe(
    source_id: str,
    recipe: Dict[str, Any],
    common: Dict[str, Any],
    model_id: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """레시피를 실행해 공고 목록을 만든다.

    mode가 'structured'면 선택자만으로 결정적으로 파싱해 LLM 호출이 전혀 없다.
    mode가 'llm_direct'면 페이지마다 LLM을 불러 직접 추출한다 — 이때는
    model_id/api_key가 필요하다.
    """
    if recipe.get("mode") == "llm_direct":
        if not (model_id and api_key):
            raise RuntimeError(f"'{source_id}' 레시피는 mode=llm_direct라 model_id/api_key가 필요합니다")
        return _run_llm_direct(source_id, recipe, common, model_id, api_key)

    fetch = recipe["fetch"]
    fmt = fetch.get("format", "html")
    timeout = int(common.get("timeout_sec", 20))
    delay = float(common.get("request_delay_sec", 0.8))
    max_items = int(common.get("max_items_per_source", 60))
    field_map = recipe["field_map"]
    item_selector = recipe["item_selector"]

    if common.get("respect_robots", True) and not collector.robots_allows(fetch["url"]):
        raise PermissionError("robots.txt 차단")

    out: List[Dict[str, Any]] = []
    prev_count = 0
    for i, url in enumerate(_paged_urls(recipe)):
        text = _fetch_page(url, fmt, timeout)
        page_items: List[Dict[str, Any]] = []

        if fmt == "html":
            soup = BeautifulSoup(text, "html.parser")
            for el in soup.select(item_selector):
                fields = {k: _extract_html_field(el, spec) for k, spec in field_map.items()}
                page_items.append(fields)
        elif fmt == "json":
            data = json.loads(text)
            items = _json_path(data, item_selector) or []
            for it in items:
                fields = {k: _extract_json_field(it, spec) for k, spec in field_map.items()}
                page_items.append(fields)
        else:
            raise RuntimeError(f"지원하지 않는 레시피 포맷입니다: {fmt}")

        if not page_items:
            break

        for fields in page_items:
            title = fields.get("title")
            if not title:
                continue
            start = collector.parse_date(fields.get("start"))
            end = collector.parse_date(fields.get("end"))
            item = collector.normalize(
                source_id,
                title,
                fields.get("org"),
                "기타",
                start,
                end,
                fields.get("url"),
                raw={"collector": "recipe_engine", "source_id": source_id},
            )
            collector.mark_dates_unknown_if_needed(item, title, start_was_known=bool(start))
            out.append(item)

        # llm_direct 경로와 동일한 이유로, 페이지마다 중복 제거 후 개수를 판단한다.
        out = collector.deduplicate(out)
        if len(out) >= max_items:
            break
        if len(out) == prev_count:
            # 직전 페이지 대비 새 항목이 없다 — 페이지네이션 파라미터가 실제
            # 사이트와 안 맞아서 같은 페이지를 반복 요청하고 있을 수 있다.
            break
        prev_count = len(out)
        time.sleep(delay)

    if not out:
        raise RuntimeError(f"레시피 실행 결과 0건: '{source_id}' 사이트 구조가 바뀌었을 수 있습니다")
    return out[:max_items]


def _url_like(value: Optional[str]) -> bool:
    """레시피가 뽑은 url 값이 실제로 링크로 쓸 수 있는 형태인지 대충 판별한다.

    NRF의 onclick 인자 하나만 뽑은 "023178" 같은 값처럼, 그럴듯해 보이지만
    실제로는 페이지 이동에 못 쓰는 값을 잡아내기 위한 최소한의 체크다.
    """
    if not value:
        return False
    return value.startswith(("http://", "https://", "/", "./", "../", "#"))


def _diagnose_broken_output(items: List[Dict[str, Any]]) -> Optional[str]:
    """추출 결과가 뻔히 잘못됐다고 볼 수 있는 신호를 찾는다. 문제 없으면 None."""
    if not items:
        return "결과가 0건입니다."
    bad_url_count = sum(1 for it in items if not _url_like(it.get("url")))
    if bad_url_count > len(items) / 2:
        samples = [it.get("url") for it in items[:3]]
        return f"url 필드 대부분이 실제 링크 형태가 아닙니다 (예: {samples}) — 페이지 이동에 쓸 수 없는 값입니다."
    empty_title_count = sum(1 for it in items if not (it.get("title") or "").strip())
    if empty_title_count > len(items) / 2:
        return "title 필드 대부분이 비어 있습니다."
    return None


REPAIR_SYSTEM_PROMPT = (
    DISCOVER_SYSTEM_PROMPT
    + "\n\n이번엔 처음이 아니라, 네가 방금 만든 레시피를 실제로 실행해봤더니 문제가 발견되어 "
    "다시 요청하는 것이다. 아래 [이전 레시피]와 [발견된 문제]를 보고, 같은 페이지 원본을 "
    "다시 분석해서 문제를 해결한 새 레시피를 만들어라. 특히 url 필드가 문제라면, onclick이나 "
    "href 같은 속성 하나만으로는 완전한 링크를 못 만들 수 있다는 점을 감안해라 — 그런 경우 "
    "선택자/속성을 다른 곳으로 바꿔보거나, 그래도 안 되면 mode를 llm_direct로 바꿔서 이후에는 "
    "선택자 대신 LLM이 직접 추출하게 해라."
)


def discover_recipe_verified(
    source_id: str,
    sample_content: str,
    list_url: str,
    fmt: str,
    model_id: str,
    api_key: str,
    common: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """discover_recipe()로 레시피를 만든 뒤, 실제로 한 번 실행해보고 결과가 뻔히
    잘못됐으면(0건, 링크가 아닌 url, 빈 title) 그 실패 내용을 보여주고 한 번만
    다시 만들어보게 한다. 그래도 못 고치면 마지막 레시피와 그 실행 결과를 그대로
    반환한다 — 이 함수는 실패해도 예외를 던지지 않는다(호출부가 discover_recipe()와
    동일하게 다룰 수 있어야 한다). 검증 과정에서 이미 실행해본 결과를 반환값에
    같이 담아서, 호출부가 같은 레시피를 다시 실행해보는 중복 호출을 안 해도 된다.
    """
    recipe = discover_recipe(source_id, sample_content, list_url, fmt, model_id, api_key)

    try:
        items = run_recipe(source_id, recipe, common, model_id=model_id, api_key=api_key)
        problem = _diagnose_broken_output(items)
    except Exception as e:
        items = []
        problem = f"레시피 실행이 실패했습니다: {e}"

    if not problem:
        return recipe, items

    repair_content = (
        _content_for_prompt(source_id, sample_content)
        + f"\n\n[이전 레시피]\n{json.dumps(recipe, ensure_ascii=False)}"
        + f"\n\n[발견된 문제]\n{problem}"
    )
    repaired = llm.structured_call(
        model_id, api_key, REPAIR_SYSTEM_PROMPT, repair_content, RECIPE_SCHEMA, max_tokens=4000,
    )
    repaired.setdefault("fetch", {})["url"] = list_url
    repaired["fetch"]["format"] = fmt

    try:
        repaired_items = run_recipe(source_id, repaired, common, model_id=model_id, api_key=api_key)
    except Exception:
        repaired_items = items  # 복구도 실패하면 첫 시도 결과라도 남긴다 (보통 빈 목록).

    return repaired, repaired_items


# ──────────────────────────── 에이전틱 발견 (실험) ────────────────────────────
# discover_recipe()는 목록 페이지 스냅샷 하나만 보고 한 번에 레시피를 만든다 —
# 그래서 페이지네이션 파라미터나 상세 URL 조합 로직이 <script src="..."> 외부
# 파일에만 있는 사이트는 원리적으로 못 맞힌다. 아래는 LLM에게 fetch_url 도구를
# 줘서, 필요하면 그 외부 JS도 직접 열어보고, 후보 URL을 실제로 가져와서
# 검증까지 한 뒤에 레시피를 제출하게 만든 버전이다.

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "이 사이트 안의 URL(상대경로 가능)을 실제로 가져와서 원본 내용을 본다. "
            "목록 페이지가 참조하는 외부 JS 파일을 열어서 네비게이션 로직을 확인하거나, "
            "후보 상세 URL이 진짜 유효한 페이지를 반환하는지 검증할 때 써라."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "가져올 URL. 상대경로면 목록 페이지 기준으로 해석된다."},
                "reason": {"type": "string", "description": "왜 이 URL을 확인하려는지 한 줄 설명"},
            },
            "required": ["url", "reason"],
            "additionalProperties": False,
        },
    },
}

SUBMIT_RECIPE_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_recipe",
        "description": "조사를 마치고 최종 수집 레시피를 제출한다. 이 도구를 호출하면 조사가 끝난다.",
        "parameters": RECIPE_SCHEMA,
    },
}

AGENTIC_DISCOVER_SYSTEM_PROMPT = (
    "너는 한국 정부지원사업 공고 목록 페이지를 조사해서, 이후 코드가 매번 LLM 없이도 그대로 "
    "재사용할 수 있는 '수집 레시피'를 만드는 어시스턴트다. 목록 페이지 원본을 먼저 보여준다.\n"
    "그 안의 링크나 onclick 핸들러만으로 상세 페이지 URL을 확실히 못 만들겠으면, fetch_url "
    "도구로 페이지가 참조하는 외부 JS 파일을 열어서 실제 네비게이션 로직(폼 action, URL 조합 "
    "방식 등)을 확인해라. 후보 URL을 만들었으면 반드시 fetch_url로 그 URL을 실제로 가져와서 "
    "진짜 상세 페이지가 맞는지(제목·날짜 등 실제 공고 내용이 보이는지) 검증하고 나서 제출해라 "
    "— 검증 없이 추측만으로 제출하지 마라. url 필드를 여러 값 조합으로 만들어야 하면 정규식에 "
    "그룹을 여러 개 잡고 template에 {1},{2}... 로 참조해서 조립해라.\n"
    "- CSS 선택자는 실제 페이지에 있는 클래스/태그만 사용해라.\n"
    "- pagination.param도 마찬가지로, 실제 페이지네이션에 쓰이는 정확한 파라미터 이름이라는 "
    "확신이 들 때만 채워라 — 필요하면 fetch_url로 두 번째 페이지를 실제로 가져와서 확인해라. "
    "확인 못 하면 null로 남겨라.\n"
    "- 날짜가 한 필드에 같이 있으면 start/end 모두 그 필드의 selector를 가리키게 하고 regex로 "
    "각각 뽑아라.\n"
    "- 그래도 선택자로 안정적으로 담기 어려우면 mode를 llm_direct로 해라.\n"
    "조사가 끝나면 submit_recipe 도구를 호출해서 최종 레시피를 제출해라."
)


def _agentic_fetch(list_url: str, url: str, timeout: int) -> str:
    """도구 호출로 임의 URL을 가져온다. list_url과 같은 도메인으로만 제한한다
    (모델이 페이지 안에서 본 적 없는 외부 URL로 유도되는 걸 막기 위한 최소한의 안전장치)."""
    resolved = urljoin(list_url, url)
    if urlparse(resolved).netloc != urlparse(list_url).netloc:
        return f"[거부됨] {resolved} — 목록 페이지와 다른 도메인이라 가져올 수 없습니다."
    r = collector.SESSION.get(resolved, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    text = r.text
    content_type = r.headers.get("Content-Type", "")
    if "html" in content_type or "<html" in text[:500].lower():
        text = _strip_boilerplate_html(text)
    return text


def discover_recipe_agentic(
    source_id: str,
    sample_content: str,
    list_url: str,
    fmt: str,
    model_id: str,
    api_key: str,
    common: Optional[Dict[str, Any]] = None,
    max_tool_calls: int = 8,
) -> Dict[str, Any]:
    """discover_recipe()의 에이전틱 버전. LLM이 fetch_url 도구로 외부 JS 파일을
    직접 열어보거나 후보 URL을 실제로 검증해본 뒤 submit_recipe로 레시피를
    제출한다. max_tool_calls를 넘기면(무한 루프 방지) 마지막으로 본 레시피
    후보가 없으면 예외를 던진다.
    """
    timeout = int((common or {}).get("timeout_sec", 20))
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": AGENTIC_DISCOVER_SYSTEM_PROMPT},
        {"role": "user", "content": _content_for_prompt(source_id, sample_content)},
    ]
    tools = [FETCH_URL_TOOL, SUBMIT_RECIPE_TOOL]

    for _ in range(max_tool_calls):
        message = llm.tool_call(model_id, api_key, messages, tools, max_tokens=4000)
        messages.append(message.model_dump())

        if not message.tool_calls:
            # 도구를 안 부르고 그냥 텍스트로 답했다 — 계속하라고 한 번 더 요청한다.
            messages.append({"role": "user", "content": "도구를 사용해서 계속 조사하거나, 확인이 끝났으면 submit_recipe를 호출해라."})
            continue

        submitted = None
        for tc in message.tool_calls:
            if tc.function.name == "submit_recipe":
                submitted = json.loads(tc.function.arguments)
                # 다른 tool_call이 같은 턴에 섞여 있어도, 나머지는 결과 없이 넘어가지
                # 않도록 최소한의 tool 응답을 채워준다 (일부 공급자는 모든 tool_call에
                # 대응하는 결과가 없으면 다음 요청을 거부한다).
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "레시피 제출 접수"})
                continue
            if tc.function.name == "fetch_url":
                args = json.loads(tc.function.arguments)
                try:
                    result = _agentic_fetch(list_url, args["url"], timeout)
                except Exception as e:
                    result = f"[가져오기 실패] {args.get('url')}: {e}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            else:
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": f"알 수 없는 도구: {tc.function.name}"})

        if submitted is not None:
            submitted.setdefault("fetch", {})["url"] = list_url
            submitted["fetch"]["format"] = fmt
            return submitted

    raise RuntimeError(f"'{source_id}' 레시피 발견이 {max_tool_calls}번의 도구 호출 안에 끝나지 않았습니다")
