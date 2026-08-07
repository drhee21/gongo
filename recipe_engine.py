# -*- coding: utf-8 -*-
"""LLM이 한 번 분석해서 만든 '레시피'로 공고 사이트를 결정적으로 수집한다.

`discover_recipe_agentic()`이 사이트 구조를 LLM에게 조사시켜(필요하면 외부 JS도
직접 열어보고 후보 URL을 검증해가며) 선택자/필드 매핑을 돌려받으면(레시피),
`run_recipe()`는 그 레시피를 코드로만 반복 실행한다 — 매 수집마다 LLM을 다시
부르지 않는다. 새 사이트 등록과 "사이트 구조가 바뀌어 갑자기 0건이 됨" 복구가
둘 다 이 두 함수를 그대로 사용한다.
"""
from __future__ import annotations

import html
import json
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment

import collector
import llm

FIELD_SPEC = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "enum": ["list", "detail"],
            "description": "이 필드를 목록 페이지(list)에서 뽑을지, 각 항목의 상세 페이지(detail)에서 "
            "뽑을지. url 필드는 항상 list여야 한다(상세 페이지를 가져오려면 그 URL이 먼저 필요하므로). "
            "detail로 표시한 필드가 하나라도 있으면, 수집기가 각 항목마다 상세 페이지를 한 번씩 더 "
            "가져와서 그 필드들을 채운다 — 목록에 없는 정보(예: 실제 마감일)가 상세 페이지에만 있을 "
            "때만 detail을 써라.",
        },
        "selector": {"type": ["string", "null"], "description": "항목 요소 기준 CSS 선택자. 항목 자체 텍스트를 쓰려면 null"},
        "attr": {"type": ["string", "null"], "description": "가져올 HTML 속성명(예: href). 텍스트를 쓰려면 null"},
        "regex": {"type": ["string", "null"], "description": "추출한 값에서 원하는 부분만 뽑는 정규식. 그룹을 여러 개 잡아서 template과 같이 쓸 수 있다. 불필요하면 null"},
        "template": {
            "type": ["string", "null"],
            "description": "regex로 잡은 그룹들을 {1},{2}... 로 참조해 최종 값을 조립하는 템플릿 (예: 여러 인자로 URL을 만들 때). 그룹 하나만 그대로 쓰면 null",
        },
    },
    "required": ["source", "selector", "attr", "regex", "template"],
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
                        "param": {"type": ["string", "null"], "description": "type이 increment일 때, 페이지 번호를 넣는 쿼리 파라미터명. 없으면 null"},
                        "type": {"type": "string", "enum": ["increment", "path_template", "none"]},
                        "url_template": {
                            "type": ["string", "null"],
                            "description": "type이 path_template일 때, 페이지 번호가 URL 쿼리가 아니라 경로 중간에 들어가는 경우 쓴다(예: 실제 2페이지 URL이 '.../list/page/2'). 페이지 번호가 들어갈 자리를 {page}로 표시한 전체 URL(예: '.../list/page/{page}'). increment/none이면 null",
                        },
                        "start": {"type": "integer", "description": "첫 페이지 번호 (보통 1)"},
                    },
                    "required": ["param", "type", "url_template", "start"],
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
    },
    "required": ["fetch", "item_selector", "field_map"],
    "additionalProperties": False,
}

CLASSIFY_TEXT_MAX_CHARS = 6000


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


def _classify_text_from_doc(fmt: str, doc: Any) -> Optional[str]:
    """상세 페이지 문서에서 자금성 판정용 순수 텍스트를 뽑는다. 특정 절(section)을 짚어내는
    선택자 없이 페이지 전체 텍스트를 쓴다 — 어떤 라벨이 어디 있는지는 판정하는 LLM이 문맥으로
    알아서 읽는다(신청대상/지원분야 등 라벨 자체가 보통 본문에 그대로 남아 있으므로 텍스트로도
    충분하다). html이 아니면(json/xml) 문서를 그대로 문자열화해서 쓴다."""
    if fmt == "html":
        soup = BeautifulSoup(str(doc), "html.parser") if not isinstance(doc, BeautifulSoup) else doc
        for tag in soup(["script", "style", "head", "nav", "footer", "header", "noscript", "svg", "img", "video"]):
            tag.decompose()
        text = collector.clean(soup.get_text("\n", strip=True))
    elif fmt == "json":
        text = collector.clean(json.dumps(doc, ensure_ascii=False))
    else:
        text = collector.clean(ET.tostring(doc, encoding="unicode", method="text"))
    return text[:CLASSIFY_TEXT_MAX_CHARS] if text else None


def _apply_field(value: Optional[str], spec: Dict[str, Any]) -> Optional[str]:
    regex = spec.get("regex")
    if not regex and spec.get("template") and not spec.get("selector"):
        # selector도 regex도 없이 template만 있으면, 정규식 그룹을 조합하는 용도가
        # 아니라 고정값(예: 게시판 전체가 한 기관 소속이라 org를 매번 같은 문자열로
        # 고정하는 경우)으로 쓰려는 의도로 보고 template을 그대로 반환한다. 페이지에서
        # 뭘 추출했는지와 무관하게 항상 이 값을 쓴다.
        return spec["template"]
    if value is None:
        return None
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


def _xml_find(item_el: ET.Element, path: str) -> Optional[ET.Element]:
    """json의 점(.) 구분 경로와 같은 convention으로 xml 요소를 찾는다."""
    node = item_el
    for part in path.split("."):
        if not part:
            continue
        found = node.find(part)
        if found is None:
            return None
        node = found
    return node


def _xml_findall(root: ET.Element, path: str) -> List[ET.Element]:
    """item_selector: 반복되는 항목 요소들을 점(.) 구분 경로의 마지막 태그명으로 찾는다."""
    if not path:
        return list(root)
    parts = [p for p in path.split(".") if p]
    if not parts:
        return list(root)
    # ET.fromstring()이 돌려주는 root는 이미 문서의 최상위 태그 그 자체인데,
    # LLM이 경로 첫 조각에 그 루트 태그 이름까지 포함시켜 줄 때가 있다(예: 실제
    # root가 <openAPI>인데 selector를 "openAPI.row"로 지정). 그 경우를 중복으로
    # 보고 건너뛴다 — 안 그러면 root의 자식 중 "openAPI"를 찾다가 못 찾아 0건이 된다.
    if parts[0] == root.tag:
        parts = parts[1:]
    if not parts:
        return list(root)
    *ancestors, last = parts
    node = root
    for part in ancestors:
        found = node.find(part)
        if found is None:
            return []
        node = found
    return node.findall(last)


def _extract_xml_field(item_el: ET.Element, spec: Dict[str, Any]) -> Optional[str]:
    selector = spec.get("selector")
    target = _xml_find(item_el, selector) if selector else item_el
    if target is None:
        return None
    attr = spec.get("attr")
    raw = target.get(attr) if attr else target.text
    if raw:
        # 일부 공공기관 XML 피드는 엔티티를 이중 인코딩해서 내려준다(예: 원문의
        # "&amp;apos;"가 XML 파싱을 거치면 "&apos;" 문자열로 남는다). 정상
        # 텍스트에는 영향이 없으므로 항상 한 번 더 unescape해서 안전하게 복원한다.
        raw = collector.clean(html.unescape(raw))
    return _apply_field(raw, spec)


def _fetch_page(url: str, fmt: str, timeout: int) -> str:
    r = collector.SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    if fmt == "html" and (not r.encoding or r.encoding.lower() == "iso-8859-1"):
        r.encoding = r.apparent_encoding
    return r.text


def _paged_urls(recipe: Dict[str, Any], max_pages: int = 20) -> List[str]:
    """increment(쿼리 파라미터, 예: ?page=2)와 path_template(경로 중간, 예:
    /list/page/2) 두 가지 페이지네이션 방식을 지원한다. 많은 한국 공공기관 CMS가
    쿼리 파라미터가 아니라 경로에 페이지 번호를 넣는데, 예전에는 그런 사이트를
    발견해도 param이 없다는 이유로 항상 첫 페이지만 가져왔다."""
    fetch = recipe["fetch"]
    url = fetch["url"]
    pagination = fetch.get("pagination") or {}
    ptype = pagination.get("type")
    start = int(pagination.get("start", 1))

    if ptype == "increment" and pagination.get("param"):
        param = pagination["param"]
        sep = "&" if "?" in url else "?"
        return [url if p == start else f"{url}{sep}{param}={p}" for p in range(start, start + max_pages)]

    if ptype == "path_template" and pagination.get("url_template"):
        template = pagination["url_template"]
        return [template.replace("{page}", str(p)) for p in range(start, start + max_pages)]

    return [url]


def _extract_field(fmt: str, doc: Any, spec: Dict[str, Any]) -> Optional[str]:
    if fmt == "html":
        return _extract_html_field(doc, spec)
    if fmt == "json":
        return _extract_json_field(doc, spec)
    return _extract_xml_field(doc, spec)


def _parse_doc(fmt: str, url: str, timeout: int) -> Any:
    """html이면 BeautifulSoup, json이면 dict/list, xml이면 Element를 돌려준다 — 목록
    페이지와 상세 페이지 둘 다 같은 포맷을 쓴다고 가정하고 이 함수로 공통 처리한다."""
    if fmt == "xml":
        r = collector.SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return ET.fromstring(r.content)
    text = _fetch_page(url, fmt, timeout)
    if fmt == "json":
        return json.loads(text)
    return BeautifulSoup(text, "html.parser")


def run_recipe(source_id: str, recipe: Dict[str, Any], common: Dict[str, Any]) -> List[Dict[str, Any]]:
    """레시피를 실행해 공고 목록을 만든다. 선택자만으로 결정적으로 파싱하며 LLM 호출이
    전혀 없다 — discover_recipe_agentic()이 구조를 한 번 알아내고 나면, 그 이후 실제
    수집은 항상 이 함수가 코드로만 반복한다.

    field_map의 어떤 필드가 source="detail"이면, 목록에서 뽑은 그 항목의 url을 이용해
    상세 페이지를 항목마다 한 번씩 추가로 가져와서 그 필드들을 채운다(목록에 없는 정보,
    예: 실제 마감일이 상세 페이지에만 있는 경우). 이 상세 페이지 요청은 구조를 다시
    LLM에게 물어보는 게 아니라, 발견 단계에서 이미 확인된 선택자를 그대로 재사용하는
    것이므로 여전히 LLM 호출이 없다 — 다만 항목 수만큼 HTTP 요청이 늘어난다.

    상세 페이지는 field_map에 detail 필드가 하나도 없어도, url이 있는 항목이면 항상
    한 번 가져온다 — 자금성 판정(funding_classifier)이 쓰는 순수 텍스트(raw["classify_text"])를
    채우기 위해서다. 이건 소스별로 켜고 끄는 옵션이 아니다 — 판정 자체가 이 흐름에
    내재된 동작이라, 레시피로 수집되는 모든 소스가 예외 없이 동일하게 겪는다."""
    fetch = recipe["fetch"]
    fmt = fetch.get("format", "html")
    timeout = int(common.get("timeout_sec", 20))
    delay = float(common.get("request_delay_sec", 0.8))
    max_items = collector.resolve_max_items(common.get("max_items_per_source", 60))
    field_map = dict(recipe["field_map"])
    item_selector = recipe["item_selector"]

    list_field_map = {k: v for k, v in field_map.items() if v.get("source") != "detail"}
    detail_field_map = {k: v for k, v in field_map.items() if v.get("source") == "detail"}

    if common.get("respect_robots", True) and not collector.robots_allows(fetch["url"]):
        raise PermissionError("robots.txt 차단")

    detail_robots_checked = False

    # max_items_per_source가 0(=무제한)이면 페이지네이션도 20페이지에서 끊기지
    # 않아야 "전체 수집"이라는 말이 실제로 성립한다. 페이지가 실제로 끝나면
    # page_items가 비거나(위 stagnation 체크) 늘어나지 않아 어차피 멈추므로,
    # 상한만 넉넉하게 늘려도 무한 루프가 되지는 않는다.
    page_cap = 1000 if max_items >= collector.UNLIMITED_ITEMS else 20
    out: List[Dict[str, Any]] = []
    prev_count = 0
    for i, url in enumerate(_paged_urls(recipe, max_pages=page_cap)):
        page_items: List[Dict[str, Any]] = []

        if fmt == "html":
            text = _fetch_page(url, fmt, timeout)
            soup = BeautifulSoup(text, "html.parser")
            for el in soup.select(item_selector):
                fields = {k: _extract_html_field(el, spec) for k, spec in list_field_map.items()}
                page_items.append(fields)
        elif fmt == "json":
            text = _fetch_page(url, fmt, timeout)
            data = json.loads(text)
            items = _json_path(data, item_selector) or []
            for it in items:
                fields = {k: _extract_json_field(it, spec) for k, spec in list_field_map.items()}
                page_items.append(fields)
        elif fmt == "xml":
            # requests가 이미 디코딩한 문자열(_fetch_page의 결과)을 ET.fromstring에
            # 넘기면, 그 문자열에 XML 인코딩 선언이 남아있을 때 ValueError가 난다
            # ("Unicode strings with encoding declaration are not supported").
            # 그래서 xml만 원본 바이트를 직접 가져와 파싱한다 — ET가 선언된 인코딩을
            # 스스로 존중하게 하기 위함이다.
            r = collector.SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for el in _xml_findall(root, item_selector):
                fields = {k: _extract_xml_field(el, spec) for k, spec in list_field_map.items()}
                page_items.append(fields)
        else:
            raise RuntimeError(f"지원하지 않는 레시피 포맷입니다: {fmt}")

        if not page_items:
            break

        for fields in page_items:
            title = fields.get("title")
            if not title:
                continue

            # 목록 페이지를 실제 마지막 페이지 너머로 요청하면 사이트에 따라
            # 404/빈 페이지 대신 "등록된 데이터가 없습니다" 같은 안내 문구를
            # 담은 플레이스홀더 항목을 selector가 그대로 매치해버리는 경우가
            # 있다(K-Startup 확인됨). 이런 항목은 url이 비어 있는 게 특징이라
            # url 필드가 레시피에 정의돼 있는데도 비어 있으면 실제 공고가
            # 아니라고 보고 건너뛴다.
            if "url" in list_field_map and not fields.get("url"):
                continue

            if fields.get("url"):
                # 목록에서 뽑은 url이 상대경로일 수 있다(예: href="/board/view?id=1") —
                # detail_field_map이 없어도(상세 페이지를 안 가져오는 레시피여도) 최종
                # 저장되는 url은 항상 절대경로여야 링크가 실제로 동작한다.
                fields["url"] = urljoin(fetch["url"], fields["url"])

            classify_text: Optional[str] = None
            if fields.get("url"):
                detail_url = fields["url"]
                if not detail_robots_checked:
                    detail_robots_checked = True
                    if common.get("respect_robots", True) and not collector.robots_allows(detail_url):
                        raise PermissionError("robots.txt 차단(상세 페이지)")
                try:
                    detail_doc = _parse_doc(fmt, detail_url, timeout)
                    for k, spec in detail_field_map.items():
                        fields[k] = _extract_field(fmt, detail_doc, spec)
                    classify_text = _classify_text_from_doc(fmt, detail_doc)
                    time.sleep(delay)
                except Exception:
                    # 상세 페이지 하나가 깨져도 목록 정보만으로 항목 자체는 살린다 —
                    # 전체 수집을 중단할 이유는 아니다.
                    pass

            start = collector.parse_date(fields.get("start"))
            end = collector.parse_date(fields.get("end"))
            elig = collector.elig_from_text(title, classify_text) if classify_text else None
            raw: Dict[str, Any] = {"collector": "recipe_engine", "source_id": source_id}
            if classify_text:
                raw["classify_text"] = classify_text
            item = collector.normalize(
                source_id,
                title,
                fields.get("org"),
                "기타",
                start,
                end,
                fields.get("url"),
                elig=elig,
                raw=raw,
            )
            collector.mark_dates_unknown_if_needed(item, title, start_was_known=bool(start))
            out.append(item)

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


# ──────────────────────────── 에이전틱 레시피 발견 ────────────────────────────
# 정적 페이지 스냅샷 한 번만 보고 레시피를 만드는 방식은 페이지네이션 파라미터나
# 상세 URL 조합 로직이 <script src="..."> 외부 파일에만 있는 사이트를 원리적으로
# 못 맞히고, 실제로 실패율이 높아 폐기했다. 아래는 LLM에게 fetch_url 도구를 줘서,
# 필요하면 외부 JS도 직접 열어보고, 후보 URL을 실제로 가져와서 검증까지 한 뒤에
# 레시피를 제출하게 만든 버전이다 — 발견은 이 방식 하나만 쓴다.

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
    "핵심 원칙: 너는 구조를 딱 한 번만 확인하면 된다. 목록 페이지든 상세 페이지든 페이지네이션 "
    "다음 페이지든, 그 구조(선택자)는 그 사이트 안에서 항상 같다고 가정해라 — 각 항목의 실제 "
    "값(제목, 날짜 등)만 다를 뿐이다. 그래서 목록 페이지는 이미 준 원본으로 충분하고, 상세 "
    "페이지가 필요하면 대표로 한 건만 fetch_url로 확인하면 된다(item마다 반복해서 확인할 필요 "
    "없다) — 실제로 모든 항목의 상세 페이지를 하나씩 가져오면서 값을 채우는 반복 작업은 네가 "
    "할 일이 아니라 나중에 이 레시피로 실행되는 코드(로컬 스크래퍼)가 할 일이다.\n"
    "그 안의 링크나 onclick 핸들러만으로 상세 페이지 URL을 확실히 못 만들겠으면, fetch_url "
    "도구로 페이지가 참조하는 외부 JS 파일을 열어서 실제 네비게이션 로직(폼 action, URL 조합 "
    "방식 등)을 확인해라. 후보 URL을 만들었으면 반드시 fetch_url로 그 URL을 실제로 가져와서 "
    "진짜 상세 페이지가 맞는지(제목·날짜 등 실제 공고 내용이 보이는지) 검증하고 나서 제출해라 "
    "— 검증 없이 추측만으로 제출하지 마라. url 필드를 여러 값 조합으로 만들어야 하면 정규식에 "
    "그룹을 여러 개 잡고 template에 {1},{2}... 로 참조해서 조립해라.\n"
    "- format이 html이면 selector는 실제 페이지에 있는 클래스/태그로 만든 CSS 선택자다.\n"
    "- format이 json/xml이면 selector는 항목 요소 기준 점(.) 구분 경로다(예: 자식 태그/키 이름이 "
    "'title'이면 selector는 'title'). item_selector도 마찬가지로 반복되는 항목에 이르는 점(.) "
    "구분 경로이고, 마지막 조각이 반복되는 태그/키 이름이다(예: 목록 루트 밑에 <row> 여러 개가 "
    "바로 있으면 'row', 그 밑에 한 단계 더 있으면 '상위태그.row').\n"
    "- 페이지네이션은 두 가지 방식이 있다. (1) 쿼리 파라미터 방식(예: '?page=2')이면 "
    "type을 increment로, 그 파라미터 이름을 param에 채워라. (2) 페이지 번호가 쿼리가 아니라 "
    "URL 경로 중간에 들어가는 방식(예: 2페이지 실제 URL이 '.../list/page/2')이면 type을 "
    "path_template으로 하고, 페이지 번호 자리를 {page}로 표시한 전체 URL을 url_template에 "
    "채워라(예: '.../list/page/{page}') — 이때 param은 null로 둔다. 어느 경우든 실제로 "
    "fetch_url로 두 번째 페이지를 가져와서 그 URL이 진짜 다음 페이지 내용을 보여주는지 확인하고 "
    "채워라 — 확인 못 하면 type을 none으로, param과 url_template 둘 다 null로 남겨라. type을 "
    "increment로 하면서 param을 null로 남기는 것처럼 서로 안 맞는 조합은 절대 만들지 마라.\n"
    "- 목록 페이지 안에 '진행중' 항목과 '마감된/지난' 항목이 서로 다른 영역(별도 컨테이너,\n"
    "  섹션 제목 등)에 구조적으로 분리되어 있으면, item_selector를 그 진행중 영역만 가리키도록\n"
    "  좁혀라(예: '.content-box > .board-list-program li a' 처럼 상위 컨테이너까지 포함한\n"
    "  선택자로 마감 영역과 구분해라) — 두 영역이 같은 태그/클래스를 재사용해서 무심코 하나의\n"
    "  선택자로 둘 다 잡힐 수 있으니 실제로 페이지를 열어 두 영역의 부모 구조가 다른지 반드시\n"
    "  확인해라. 반대로 이런 구분이 전혀 없이 하나의 목록에 다 섞여 있으면(별도 영역이 아니라\n"
    "  단순히 날짜가 지난 항목도 같은 목록에 계속 남아있는 경우) 굳이 나누지 말고 그대로 다\n"
    "  가져와라.\n"
    "- 날짜가 한 필드에 같이 있으면 start/end 모두 그 필드의 selector를 가리키게 하고 regex로 "
    "각각 뽑아라.\n"
    "- 목록에 보이는 날짜 칼럼이 진짜 접수 시작일/마감일이라는 확신이 들 때만 start/end에 "
    "매핑해라. 등록일/게시일/작성일(글이 올라온 날짜)은 접수 시작일이 아니다 — 이 둘은 서로 "
    "다른 정보다. 칼럼 헤더나 주변 문맥에 '접수', '신청', '모집', '마감' 같은 표현이 없고 "
    "그냥 '등록일'/'작성일'/'게시일'류로만 보이면, 그건 접수기간이 아니라 글이 언제 올라왔는지 "
    "일 뿐이므로 start(또는 end)에 매핑하지 마라 — 확실하지 않으면 차라리 null로 남기고 "
    "detail(상세 페이지)에서 실제 접수기간을 찾아라.\n"
    "- 목록/상세 어디에도 날짜를 안정적으로 뽑을 selector가 없다고 해서, 제목 텍스트에서 "
    "정규식으로 날짜를 추출하려 하지 마라(예: 제목 끝에 어쩌다 보이는 \"(~7/30)\" 같은 표기를 "
    "정규식으로 뽑아내는 것). 제목에 박힌 날짜 표기는 사이트마다, 공고마다 형식이 제각각이라 "
    "한 번의 정규식으로 안정적으로 일반화되지 않는다 — 이런 편법에 기대지 말고, 실제 값이 "
    "안정적인 selector로 뽑히는 경우에만 채우고 그렇지 않으면 null로 남겨라(제목에서 날짜를 "
    "추정하는 건 이 레시피가 아니라 별도의 공용 로직이 이미 맡고 있다).\n"
    "- field_map의 각 필드는 source를 list 또는 detail로 표시해야 한다. url은 항상 list다. "
    "나머지 필드도 목록 페이지 안에서 안정적으로 뽑을 수 있으면 기본적으로 list로 해라 — "
    "detail은 그 정보가 목록에는 전혀 없고 상세 페이지에만 있을 때만 써라(흔한 예: 목록에는 "
    "게시일만 있고 실제 접수 마감일은 상세 페이지 본문에만 있는 경우). 어떤 필드든 detail로 "
    "표시하려면, 대표 상세 페이지 하나를 fetch_url로 실제로 열어서 그 필드가 정말 거기 있는지, "
    "그리고 어떤 선택자로 뽑을 수 있는지 확인하고 나서 채워라 — 확인 안 하고 detail로 표시하지 "
    "마라.\n"
    "- title 필드의 원본 텍스트가 org 필드와 중복되는 내용을 접두어/접미어로 포함하는 경우가 "
    "흔하다(예: 제목이 \"[기관명] 실제 제목\"처럼 시작하는데 그 '기관명'을 org로도 따로 뽑는 "
    "경우). 이런 순수 중복은 title의 regex로 반드시 걷어내서 실제 제목만 남겨라(예: "
    "\"^\\[.+?\\]\\s*(.+)$\" 로 대괄호 접두어를 떼고 남은 그룹만 쓰기). 단, \"(재공고)\", "
    "\"1차\"처럼 서로 다른 공고를 구분해주는 표시는 org와 중복되는 게 아니므로 절대 지우지 "
    "마라 — title은 공고를 유일하게 식별할 수 있어야 한다(같은 사업을 재공고/차수만 다르게 "
    "여러 번 올리는 경우가 흔하고, 그 구분 표시가 사라지면 서로 다른 공고가 같은 제목으로 "
    "보여 하나로 오인될 수 있다).\n"
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
    """레시피 발견의 유일한 경로. LLM이 fetch_url 도구로 외부 JS 파일을 직접
    열어보거나 후보 URL을 실제로 검증해본 뒤 submit_recipe로 레시피를 제출한다.
    max_tool_calls를 넘기면(무한 루프 방지) 마지막으로 본 레시피 후보가 없으면
    예외를 던진다.
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
