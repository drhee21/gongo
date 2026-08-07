# -*- coding: utf-8 -*-
"""공고모아 로컬 웹 서버."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import threading
import uuid
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import auth
import ai_match
import collector
import database
import llm
import recipe_engine
import scheduler
import uploads

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

SESSION_COOKIE = "session"
SESSION_TTL_DAYS = 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RECOLLECT_COOLDOWN_SEC = 300
_last_recollect_at: Optional[datetime] = None
_collect_lock = threading.Lock()
_shutdown_event = threading.Event()

ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()
}
OVERRIDABLE_SOURCES = {"kstartup", "nrf", "kddf", "biohub_direct", "khidi_direct", "bizinfo", "g2b"}
MAX_COMPANY_DOCUMENTS = 10


def maybe_promote_admin(user: Dict[str, Any]) -> Dict[str, Any]:
    """ADMIN_EMAILS 환경변수에 등록된 이메일이면 자동으로 관리자 권한을 부여한다."""
    if user["email"] in ADMIN_EMAILS and not user.get("is_admin"):
        database.set_user_admin(user["id"], True)
        user = dict(user)
        user["is_admin"] = 1
    return user


def json_bytes(obj: Any, status: int = 200) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def status_of(a: Dict[str, Any]) -> Tuple[str, bool]:
    """공고의 접수 상태와, 그 상태가 추정값인지 여부를 함께 반환한다.

    상태값(접수중/접수예정/마감/상시/날짜 미상)은 "지금 어떤 상태인가"만
    나타내고, "그 판단을 확신할 수 있는가"는 별도 불리언으로 분리한다.
    두 축을 상태값 하나에 섞으면(예: '확인 필요' 같은 값을 추가하면) 상태
    가짓수만 늘고 의미도 흐려지므로, dates_unknown/rolling_confirmed와
    같은 방식으로 플래그를 따로 둔다.

    추정으로 표시되는 경우는 날짜 한쪽을 몰라 접수 시작 여부를 확신할 수
    없을 때뿐이다. 마감일이 지났거나(마감) 시작일이 아직 안 왔으면(접수예정)
    나머지 날짜를 몰라도 판단이 확실하므로 추정이 아니다.
    """
    end = a.get("end")
    start = a.get("start")
    today = datetime.now().date()
    if a.get("dates_unknown"):
        return "날짜 미상", False
    if not end:
        # 상시/수시/소진 같은 명시적 표현으로 확인된 경우만 진짜 상시로 본다.
        if a.get("rolling_confirmed"):
            return "상시", False
        if start:
            try:
                s = datetime.strptime(start, "%Y-%m-%d").date()
            except Exception:
                return "날짜 미상", False
            if s > today:
                return "접수예정", False
            # 시작일은 지났는데 마감일을 모른다 — 아직 접수 중인지 확신할 수 없다.
            return "접수중", True
        return "날짜 미상", False
    try:
        e = datetime.strptime(end, "%Y-%m-%d").date()
        if e < today:
            return "마감", False
        if start:
            s = datetime.strptime(start, "%Y-%m-%d").date()
            if s > today:
                return "접수예정", False
            return "접수중", False
        # 마감일은 안 지났는데 시작일을 모른다 — 접수가 이미 시작됐는지 확신할 수 없다.
        return "접수중", True
    except Exception:
        return "날짜 미상", False


def dday(a: Dict[str, Any]) -> Any:
    if not a.get("end"):
        return None
    try:
        e = datetime.strptime(a["end"], "%Y-%m-%d").date()
        return (e - datetime.now().date()).days
    except Exception:
        return None



def canonical_source_id(source_id: str) -> str:
    return {
        "biohub_direct": "biohub",
        "khidi_direct": "khidi",
    }.get(source_id, source_id)

def add_runtime_fields(items):
    # 스타트업 자금·지원과 무관하다고 판정된 공고(교육/행사/일반 중소기업 대상 등)는
    # 항상 걸러진다 — 토글로 다시 보이게 할 수 있는 옵션이 아니라 기본 동작이다.
    # 아직 판정되지 않은 공고(새로 수집됐지만 아직 분류 전)는 여기 포함되지 않으므로
    # 조용히 숨겨지지 않고 그대로 보인다.
    excluded_ids = database.get_excluded_notice_ids()
    # 비활성화된 소스의 기존 공고는 DB에서 지우지 않지만(재활성화하면 바로 다시
    # 보이도록), 꺼둔 동안은 목록에 노출하지 않는다.
    disabled_sources = collector.get_disabled_source_ids()
    out = []
    for a in items:
        if a.get("id") in excluded_ids:
            continue
        if (a.get("src") or a.get("source")) in disabled_sources:
            continue
        b = dict(a)
        b["src"] = canonical_source_id(b.get("src") or b.get("source") or "unknown")
        b["status"], b["status_inferred"] = status_of(b)
        b["dday"] = dday(b)
        out.append(b)
    return out


def company_profile_hash(company: Dict[str, Any]) -> str:
    payload = json.dumps(company or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def attach_ai_fit(items, user_id: Optional[str], company: Dict[str, Any]):
    if not user_id:
        for a in items:
            a["ai_fit"] = None
            a["ai_reason"] = None
        return items
    ai_map = database.get_ai_fit_map(user_id, company_profile_hash(company))
    for a in items:
        info = ai_map.get(a.get("id"))
        a["ai_fit"] = info["fit"] if info else None
        a["ai_reason"] = info["reason"] if info else None
    return items


def get_cookie(handler: BaseHTTPRequestHandler, name: str) -> Optional[str]:
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    jar.load(raw)
    morsel = jar.get(name)
    return morsel.value if morsel else None


def current_user(handler: BaseHTTPRequestHandler) -> Optional[Dict[str, Any]]:
    token = get_cookie(handler, SESSION_COOKIE)
    if not token:
        return None
    user = database.get_session_user(token)
    if not user:
        return None
    return maybe_promote_admin(user)


def session_cookie_header(token: str) -> str:
    return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_DAYS * 24 * 3600}"


def clear_session_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def resolve_active_llm(user: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """사용자의 활성 LLM 키 프로필에서 (model_id, 복호화된 api_key)를 반환한다.
    등록된 프로필이 없으면 (None, None)."""
    profile = database.get_active_llm_profile(user["id"])
    if not profile:
        return None, None
    return profile["model_id"], auth.decrypt_secret(profile["key_enc"])


def user_public(user: Dict[str, Any]) -> Dict[str, Any]:
    profiles = database.list_llm_key_profiles(user["id"])
    active = database.get_active_llm_profile(user["id"])
    return {
        "email": user["email"],
        "has_bizinfo_key": bool(user.get("bizinfo_api_key_enc")),
        "is_admin": bool(user.get("is_admin")),
        "llm_profiles": [
            {"id": p["id"], "label": p["label"], "model_id": p["model_id"]} for p in profiles
        ],
        "active_llm_profile_id": active["id"] if active else None,
        "has_llm_key": bool(active),
        "onboarding_done": bool(database.get_user_setting(user["id"], "onboarding_done", False)),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "GongoMoa/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def send_body(
        self,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
        status: int = 200,
        set_cookie: Optional[str] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj: Any, status: int = 200, set_cookie: Optional[str] = None) -> None:
        self.send_body(json_bytes(obj), status=status, set_cookie=set_cookie)

    def read_json(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if not n:
            return {}
        raw = self.rfile.read(n).decode("utf-8")
        return json.loads(raw or "{}")

    def read_raw_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(n) if n else b""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                database.init_db()
                self.send_json({"ok": True, "version": "v3", "root": str(ROOT), "db": str(database.DB_PATH)})
                return
            if path == "/api/auth/me":
                user = current_user(self)
                self.send_json({"ok": True, "user": user_public(user) if user else None})
                return
            if path == "/api/llm-models":
                self.send_json({"ok": True, "items": llm.MODEL_CATALOG})
                return
            if path == "/api/notices":
                user = current_user(self)
                user_id = user["id"] if user else None
                items = database.list_notices(
                    q=(qs.get("q") or [""])[0],
                    source=(qs.get("source") or [""])[0],
                    category=(qs.get("category") or [""])[0],
                    favorite=(qs.get("favorite") or [""])[0] == "1",
                    user_id=user_id,
                )
                company = database.get_user_setting(user_id, "company", {}) if user_id else {}
                enriched = attach_ai_fit(add_runtime_fields(items), user_id, company)
                if not (user and user.get("bizinfo_api_key_enc")):
                    enriched = database.hide_bizinfo_only(enriched)
                self.send_json({"ok": True, "items": enriched})
                return
            if path == "/api/sources":
                displayed_counts = database.get_displayed_counts_by_source()
                disabled_sources = collector.get_disabled_source_ids()
                for sid in disabled_sources:
                    displayed_counts[database.canonical_source_id(sid)] = 0
                # config.json에서 꺼둔 소스는 admin 재정의로도 되살아나지 않는 상한선이므로
                # (collector._apply_source_override 참고) 관리자 화면 어디에도 아예 나타나지
                # 않아야 한다 — "비활성화" 행으로 보여주는 대신 목록 자체에서 제외한다.
                config_disabled = {
                    database.canonical_source_id(sid) for sid in collector.get_config_disabled_source_ids()
                }
                raw = [
                    r for r in database.list_sources(clean=False)
                    if database.canonical_source_id(r.get("id") or "") not in config_disabled
                ]
                for r in raw:
                    r["displayed_count"] = displayed_counts.get(database.canonical_source_id(r.get("id") or ""), 0)
                items = [it for it in database.list_sources() if it.get("id") not in config_disabled]
                self.send_json({"ok": True, "items": items, "raw": raw})
                return
            if path == "/api/admin/source-overrides":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                cfg = collector.load_config()
                overrides = database.get_source_overrides()
                boards = cfg.get("boards") or {}
                items = []
                for sid, default_name, default_url, default_enabled in [
                    ("kstartup", "K-스타트업", (cfg.get("kstartup") or {}).get("list_url"), (cfg.get("kstartup") or {}).get("enabled", False)),
                    ("nrf", "한국연구재단", (boards.get("nrf") or {}).get("list_url"), (boards.get("nrf") or {}).get("enabled", False)),
                    ("kddf", "국가신약개발사업단", (boards.get("kddf") or {}).get("list_url"), (boards.get("kddf") or {}).get("enabled", False)),
                    ("biohub_direct", "서울바이오허브", (boards.get("biohub_direct") or {}).get("list_url"), (boards.get("biohub_direct") or {}).get("enabled", False)),
                    ("khidi_direct", "보건산업진흥원/KHIDI", (boards.get("khidi_direct") or {}).get("list_url"), (boards.get("khidi_direct") or {}).get("enabled", False)),
                    # bizinfo/g2b는 list_url이 아니라 API 키로 수집하므로 URL 재정의는
                    # 의미가 없다 — 활성화 여부만 재정의 대상이다.
                    ("bizinfo", "기업마당", None, (cfg.get("bizinfo") or {}).get("enabled", False)),
                    ("g2b", "나라장터", None, (cfg.get("g2b") or {}).get("enabled", False)),
                ]:
                    if not default_enabled:
                        # config.json 자체에서 꺼둔 소스는 admin 재정의로도 되살릴 수
                        # 없는 상한선이므로(collector._apply_source_override 참고),
                        # 켜봐야 소용없는 토글을 보여주는 대신 목록에서 아예 뺀다.
                        continue
                    ov = overrides.get(sid) or {}
                    items.append({
                        "source_id": sid,
                        "default_name": default_name,
                        "name": ov.get("name") or default_name,
                        "default_url": default_url,
                        "override_url": ov.get("list_url"),
                        "enabled": default_enabled if ov.get("enabled") is None else ov["enabled"],
                        "enabled_is_override": ov.get("enabled") is not None,
                    })
                self.send_json({"ok": True, "items": items})
                return
            if path == "/api/admin/scheduler-config":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                cfg = scheduler.get_schedule()
                last_run = scheduler.get_last_run()
                self.send_json({
                    "ok": True,
                    "config": cfg,
                    "last_run": last_run,
                    "next_run_estimate": scheduler.next_run_estimate(cfg, last_run, datetime.now()),
                })
                return
            if path == "/api/admin/custom-sources":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                self.send_json({"ok": True, "items": database.list_custom_sources()})
                return
            if path == "/api/favorites":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                self.send_json({"ok": True, "ids": database.favorite_ids(user["id"])})
                return
            if path == "/api/company":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                self.send_json({"ok": True, "company": database.get_user_setting(user["id"], "company", {})})
                return
            if path == "/api/company/documents":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                docs = database.list_company_documents(user["id"])
                items = [{"id": d["id"], "filename": d["filename"], "char_count": d["char_count"], "created_at": d["created_at"]} for d in docs]
                self.send_json({"ok": True, "items": items})
                return
            if path == "/api/export.csv":
                user = current_user(self)
                self.export_csv(user)
                return
            self.serve_static(path)
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/auth/signup":
                data = self.read_json()
                email = str(data.get("email") or "").strip().lower()
                password = str(data.get("password") or "")
                if not EMAIL_RE.match(email):
                    self.send_json({"ok": False, "error": "올바른 이메일 형식이 아닙니다"}, status=400)
                    return
                if len(password) < 8:
                    self.send_json({"ok": False, "error": "비밀번호는 8자 이상이어야 합니다"}, status=400)
                    return
                if database.get_user_by_email(email):
                    self.send_json({"ok": False, "error": "이미 가입된 이메일입니다"}, status=400)
                    return
                salt, pw_hash = auth.hash_password(password)
                uid = database.create_user(email, salt, pw_hash)
                token = auth.new_session_token()
                database.create_session(uid, token, ttl_days=SESSION_TTL_DAYS)
                new_user = maybe_promote_admin(database.get_user_by_id(uid))
                self.send_json(
                    {"ok": True, "user": user_public(new_user)},
                    set_cookie=session_cookie_header(token),
                )
                return
            if path == "/api/auth/login":
                data = self.read_json()
                email = str(data.get("email") or "").strip().lower()
                password = str(data.get("password") or "")
                user = database.get_user_by_email(email)
                if not user or not auth.verify_password(password, user["password_salt"], user["password_hash"]):
                    self.send_json({"ok": False, "error": "이메일 또는 비밀번호가 올바르지 않습니다"}, status=401)
                    return
                token = auth.new_session_token()
                database.create_session(user["id"], token, ttl_days=SESSION_TTL_DAYS)
                user = maybe_promote_admin(user)
                self.send_json({"ok": True, "user": user_public(user)}, set_cookie=session_cookie_header(token))
                return
            if path == "/api/auth/logout":
                token = get_cookie(self, SESSION_COOKIE)
                if token:
                    database.delete_session(token)
                self.send_json({"ok": True}, set_cookie=clear_session_cookie_header())
                return
            if path == "/api/me/bizinfo-key":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                data = self.read_json()
                raw_key = str(data.get("bizinfo_key") or "").strip()
                if not raw_key:
                    database.set_user_bizinfo_key(user["id"], None)
                    self.send_json({"ok": True, "has_bizinfo_key": False})
                    return
                database.set_user_bizinfo_key(user["id"], auth.encrypt_secret(raw_key))
                self.send_json({"ok": True, "has_bizinfo_key": True})
                return
            if path == "/api/me/llm-profiles":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                data = self.read_json()
                model_id = str(data.get("model_id") or "").strip()
                model = llm.MODEL_BY_ID.get(model_id)
                if not model:
                    self.send_json({"ok": False, "error": "지원하지 않는 모델입니다"}, status=400)
                    return
                raw_key = str(data.get("key") or "").strip()
                if not raw_key:
                    self.send_json({"ok": False, "error": "API 키를 입력해주세요"}, status=400)
                    return
                label = str(data.get("label") or "").strip() or model["label"]
                is_first_profile = not database.list_llm_key_profiles(user["id"])
                profile_id = database.create_llm_key_profile(user["id"], label, model_id, auth.encrypt_secret(raw_key))
                # 처음 등록하는 키만 자동으로 활성화한다 — 이미 쓰고 있는 키가 있는데
                # 새 키를 추가했다고 조용히 바뀌면 사용자가 모르는 새 다른 모델/키로
                # 전환되어 버린다. 이미 키가 있으면 명시적으로 "이 키로 적용"을
                # 눌러야 바뀐다.
                if is_first_profile:
                    database.set_active_llm_profile(user["id"], profile_id)
                self.send_json({"ok": True, "user": user_public(user)})
                return
            if path == "/api/me/llm-profiles/activate":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                data = self.read_json()
                profile_id = str(data.get("profile_id") or "").strip()
                if not database.set_active_llm_profile(user["id"], profile_id):
                    self.send_json({"ok": False, "error": "존재하지 않는 키입니다"}, status=400)
                    return
                self.send_json({"ok": True, "user": user_public(user)})
                return
            if path == "/api/me/llm-profiles/delete":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                data = self.read_json()
                profile_id = str(data.get("profile_id") or "").strip()
                database.delete_llm_key_profile(user["id"], profile_id)
                self.send_json({"ok": True, "user": user_public(user)})
                return
            if path == "/api/me/onboarding-complete":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                database.set_user_setting(user["id"], "onboarding_done", True)
                self.send_json({"ok": True})
                return
            if path == "/api/recollect":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                global _last_recollect_at
                now = datetime.now()
                if _last_recollect_at and (now - _last_recollect_at).total_seconds() < RECOLLECT_COOLDOWN_SEC:
                    wait = int(RECOLLECT_COOLDOWN_SEC - (now - _last_recollect_at).total_seconds())
                    self.send_json({"ok": False, "error": f"너무 자주 요청했습니다. {wait}초 후 다시 시도해주세요."}, status=429)
                    return
                _last_recollect_at = now
                run = scheduler.run_collection_locked(_collect_lock, triggering_user_id=user["id"])
                if run is None:
                    self.send_json({"ok": False, "error": "지금 다른 수집이 진행 중입니다. 잠시 후 다시 시도해주세요."}, status=429)
                    return
                # len(run.items)는 이번 실행에서 활성 소스들이 병합 후 만들어낸 원본
                # 건수라, K-Startup 자금성 판정 제외나 비활성 소스에 남아있는 옛 데이터를
                # 고려하지 않는다 — "업데이트 완료: N건" 토스트에 그대로 쓰면 사용자가
                # 실제 목록에 보이는 건수와 안 맞아 혼란스럽다. 화면에 실제로 뜨는 건수
                # (표시 건수)를 대신 돌려준다.
                displayed_counts = database.get_displayed_counts_by_source()
                # get_disabled_source_ids()는 원본 id(biohub_direct/khidi_direct)를
                # 돌려주는데 displayed_counts는 canonical id(biohub/khidi)로 집계되어
                # 있으므로, 그대로 비교하면 안 걸러지고 새어 들어간다.
                disabled_sources = {
                    database.canonical_source_id(sid) for sid in collector.get_disabled_source_ids()
                }
                displayed_total = sum(
                    n for sid, n in displayed_counts.items() if sid not in disabled_sources
                )
                self.send_json({"ok": True, "count": displayed_total, "sources": run.sources})
                return
            if path == "/api/admin/source-overrides":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                data = self.read_json()
                source_id = str(data.get("source_id") or "").strip()
                if source_id not in OVERRIDABLE_SOURCES:
                    self.send_json({"ok": False, "error": "알 수 없는 소스입니다"}, status=400)
                    return
                if source_id in collector.get_config_disabled_source_ids():
                    self.send_json({"ok": False, "error": "config.json에서 꺼둔 소스입니다"}, status=400)
                    return
                list_url = str(data.get("list_url") or "").strip()
                name = str(data.get("name") or "").strip()
                database.set_source_override(source_id, list_url or None, name or None)
                self.send_json({"ok": True, "source_id": source_id, "override_url": list_url or None, "name": name or None})
                return
            if path == "/api/admin/source-overrides/toggle":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                data = self.read_json()
                source_id = str(data.get("source_id") or "").strip()
                if source_id not in OVERRIDABLE_SOURCES:
                    self.send_json({"ok": False, "error": "알 수 없는 소스입니다"}, status=400)
                    return
                if source_id in collector.get_config_disabled_source_ids():
                    self.send_json({"ok": False, "error": "config.json에서 꺼둔 소스입니다"}, status=400)
                    return
                database.set_source_enabled_override(source_id, bool(data.get("enabled")))
                self.send_json({"ok": True, "source_id": source_id})
                return
            if path == "/api/admin/scheduler-config":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                data = self.read_json()
                time_str = str(data.get("time") or "")
                if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", time_str):
                    self.send_json({"ok": False, "error": "time은 HH:MM 형식이어야 합니다"}, status=400)
                    return
                days = data.get("days")
                if not isinstance(days, list) or not days or any(d not in scheduler.WEEKDAY_CODES for d in days):
                    self.send_json({"ok": False, "error": "days가 올바르지 않습니다"}, status=400)
                    return
                try:
                    interval_weeks = int(data.get("interval_weeks"))
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "interval_weeks는 정수여야 합니다"}, status=400)
                    return
                if not (1 <= interval_weeks <= 52):
                    self.send_json({"ok": False, "error": "interval_weeks는 1~52 사이여야 합니다"}, status=400)
                    return
                cfg = {
                    "enabled": bool(data.get("enabled")),
                    "time": time_str,
                    "days": days,
                    "interval_weeks": interval_weeks,
                    # 스케줄러가 자동 실행될 때 이 값을 triggering_user_id로 써서, 그 시점에
                    # 활성이던 이 관리자의 키를 쓰게 한다(scheduler.py 참고) — 매번 "가장 최근에
                    # 승격된 관리자"로 대체되는 대신, 일정을 실제로 설정한 사람의 키가 쓰인다.
                    "updated_by": user["id"],
                }
                database.set_app_setting(scheduler.SETTING_KEY, cfg)
                self.send_json({"ok": True, "config": cfg})
                return
            if path == "/api/admin/custom-sources/discover":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                data = self.read_json()
                name = str(data.get("name") or "").strip()
                url = str(data.get("url") or "").strip()
                existing_source_id = str(data.get("existing_source_id") or "").strip() or None
                if not name or not url:
                    self.send_json({"ok": False, "error": "이름과 URL을 모두 입력해주세요.", "code": "invalid_url"}, status=400)
                    return
                if not re.match(r"^https?://", url):
                    self.send_json({"ok": False, "error": "http(s)://로 시작하는 URL을 입력해주세요.", "code": "invalid_url"}, status=400)
                    return

                editing = None
                if existing_source_id:
                    editing = database.get_custom_source(existing_source_id)
                    if not editing:
                        self.send_json({"ok": False, "error": "수정하려는 커스텀 소스를 찾을 수 없습니다.", "code": "invalid_url"}, status=400)
                        return

                cfg = collector.load_config()
                # 이미 하드코딩된 소스(kstartup/boards)나 다른 커스텀 소스의 URL을 그대로 또
                # 등록하면 같은 사이트가 두 소스로 중복 수집된다 — discover 단계에서 미리
                # 막는다(수정 중인 소스 자기 자신의 기존 URL은 예외로 둔다).
                existing_urls = {collector.clean((cfg.get("kstartup") or {}).get("list_url") or collector.KSTARTUP_DEFAULT_URL)}
                for b in (cfg.get("boards") or {}).values():
                    u = collector.clean(b.get("list_url") or "")
                    if u:
                        existing_urls.add(u)
                existing_urls.update(
                    collector.clean(ov["list_url"]) for ov in database.get_source_overrides().values() if ov.get("list_url")
                )
                existing_urls.update(
                    collector.clean(cs["list_url"]) for cs in database.list_custom_sources()
                    if cs["id"] != existing_source_id
                )
                if collector.clean(url) in existing_urls:
                    self.send_json({"ok": False, "error": "이미 기존 소스가 사용 중인 URL입니다.", "code": "invalid_url"}, status=400)
                    return

                model_id, api_key = resolve_active_llm(user)
                if not api_key:
                    self.send_json(
                        {"ok": False, "error": "먼저 '회사 정보'에서 본인의 API 키를 등록해주세요.", "code": "no_llm_key"}, status=400
                    )
                    return
                common = cfg.get("common", {})

                if common.get("respect_robots", True) and not collector.robots_allows(url):
                    self.send_json({"ok": False, "error": "robots.txt에서 이 URL의 수집을 막고 있습니다.", "code": "robots_blocked"}, status=400)
                    return
                try:
                    r = collector.SESSION.get(url, timeout=common.get("timeout_sec", 20))
                    r.raise_for_status()
                    if not r.encoding or r.encoding.lower() == "iso-8859-1":
                        r.encoding = r.apparent_encoding
                    sample = recipe_engine._strip_boilerplate_html(r.text)
                except Exception as e:
                    self.send_json({"ok": False, "error": f"페이지를 가져오지 못했습니다: {e}", "code": "fetch_failed"}, status=400)
                    return

                source_id = existing_source_id or ("custom_" + uuid.uuid4().hex[:10])
                try:
                    recipe = recipe_engine.discover_recipe_agentic(source_id, sample, url, "html", model_id, api_key, common)
                except Exception as e:
                    self.send_json({"ok": False, "error": f"레시피 발견에 실패했습니다: {e}", "code": "discovery_failed"}, status=400)
                    return

                try:
                    items = recipe_engine.run_recipe(source_id, recipe, common)
                except Exception as e:
                    self.send_json({"ok": False, "error": f"발견된 레시피 실행에 실패했습니다: {e}", "code": "zero_items"}, status=400)
                    return

                warnings = []
                sample_head = items[:10]
                dup_title_count = sum(
                    1 for it in sample_head if it.get("org") and it.get("title") and it["org"] in it["title"]
                )
                if sample_head and dup_title_count >= max(1, len(sample_head) // 2):
                    warnings.append("제목에 기관명이 중복 포함된 것으로 보입니다 — 저장 전 레시피의 title 정규식을 확인해주세요.")
                same_date_count = sum(
                    1 for it in sample_head if it.get("start") and it.get("start") == it.get("end")
                )
                if sample_head and same_date_count >= max(1, len(sample_head) // 2):
                    warnings.append(
                        "시작일과 마감일이 대부분 똑같습니다 — 목록에 신청기간이 없어 게시일을 "
                        "마감일로 착각했을 수 있습니다. 실제 신청기간은 상세 페이지에만 있을 수 있습니다."
                    )

                sample_items = [
                    {"title": it.get("title"), "org": it.get("org"), "start": it.get("start"), "end": it.get("end"), "url": it.get("url")}
                    for it in items[:20]
                ]
                self.send_json({
                    "ok": True,
                    "source_id": source_id,
                    "name": name,
                    "url": url,
                    "category": (editing or {}).get("category"),
                    "recipe": recipe,
                    "item_count": len(items),
                    "sample_items": sample_items,
                    "warnings": warnings,
                    "editing": bool(existing_source_id),
                })
                return
            if path == "/api/admin/custom-sources/confirm":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                data = self.read_json()
                source_id = str(data.get("source_id") or "").strip()
                name = str(data.get("name") or "").strip()
                url = str(data.get("url") or "").strip()
                category = data.get("category")
                recipe = data.get("recipe")
                if not (source_id and name and url and isinstance(recipe, dict)):
                    self.send_json({"ok": False, "error": "필수 항목이 누락되었습니다.", "code": "invalid_recipe"}, status=400)
                    return
                # discover가 반환한 값을 그대로 다시 보내는 것이 정상 흐름이지만, 클라이언트가
                # 보낸 값이므로(중간에서 조작되었거나 손상됐을 가능성) 저장 전 구조를 다시 검증한다.
                fetch = recipe.get("fetch") or {}
                field_map = recipe.get("field_map") or {}
                valid = (
                    isinstance(fetch.get("url"), str)
                    and fetch.get("format") in ("html", "json", "xml")
                    and isinstance(fetch.get("pagination"), dict)
                    and isinstance(recipe.get("item_selector"), str)
                    and all(k in field_map for k in ("title", "url", "org", "start", "end"))
                )
                if not valid:
                    self.send_json({"ok": False, "error": "레시피 형식이 올바르지 않습니다.", "code": "invalid_recipe"}, status=400)
                    return

                cfg = collector.load_config()
                common = cfg.get("common", {})

                # 미리보기 이후 사이트가 바뀌었을 수 있으므로, 저장 직전 한 번 더
                # 실행해본다(재발견이 아니라 결정적 실행 1회라 비용이 거의 없다).
                try:
                    items = recipe_engine.run_recipe(source_id, recipe, common)
                except Exception:
                    self.send_json(
                        {"ok": False, "error": "사이트 내용이 미리보기 이후 바뀐 것 같습니다. 다시 미리보기를 실행해주세요.", "code": "stale_preview"},
                        status=400,
                    )
                    return

                existing = database.get_custom_source(source_id)
                database.set_source_recipe(source_id, recipe, verified_ok=True)
                try:
                    if existing:
                        # URL/이름 수정 확정 — id는 그대로 두고 메타데이터만 갱신한다. 옛
                        # URL/레시피로 모아둔 공고는 더 이상 유효하지 않으므로(즐겨찾기는
                        # 보존) 지우고 새로 발견된 공고로 채운다.
                        database.update_custom_source(source_id, name, url, category)
                        database.delete_notices_by_source(source_id, spare_favorites=True)
                    else:
                        database.insert_custom_source(source_id, name, url, category, user["id"])
                except Exception:
                    # URL 중복(UNIQUE 제약) 등으로 등록/수정에 실패하면, 방금 저장한 레시피가
                    # 고아로 남지 않도록 함께 정리한다(단, 기존 소스를 수정하던 중이었다면
                    # 그 소스 자체는 지우지 않는다 — 레시피만 이전 상태로 되돌릴 방법이 없어
                    # 다음 수정 시도에서 다시 덮어써질 뿐이므로 그대로 둔다).
                    if not existing:
                        database.delete_custom_source(source_id)
                    self.send_json({"ok": False, "error": "이미 등록된 사이트이거나 저장에 실패했습니다.", "code": "duplicate"}, status=409)
                    return

                database.upsert_notices(items, prune=False)
                database.replace_source_status({
                    source_id: {"name": name, "method": "레시피", "state": "정상", "n": len(items), "last": database.now_iso()}
                })
                self.send_json({"ok": True, "source_id": source_id, "editing": bool(existing)})
                return
            if path == "/api/admin/custom-sources/rename":
                # URL은 그대로 두고 이름만 바꾸는 경우 — 레시피가 그대로 유효하므로
                # 재발견(비용이 드는 LLM 호출) 없이 곧바로 처리한다.
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                data = self.read_json()
                source_id = str(data.get("source_id") or "").strip()
                name = str(data.get("name") or "").strip()
                if not source_id or not name:
                    self.send_json({"ok": False, "error": "source_id와 name이 필요합니다"}, status=400)
                    return
                if not database.get_custom_source(source_id):
                    self.send_json({"ok": False, "error": "커스텀 소스를 찾을 수 없습니다"}, status=404)
                    return
                database.rename_custom_source(source_id, name)
                self.send_json({"ok": True})
                return
            if path == "/api/admin/custom-sources/toggle":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                data = self.read_json()
                source_id = str(data.get("source_id") or "").strip()
                if not source_id:
                    self.send_json({"ok": False, "error": "source_id가 필요합니다"}, status=400)
                    return
                database.set_custom_source_enabled(source_id, bool(data.get("enabled")))
                self.send_json({"ok": True})
                return
            if path == "/api/admin/custom-sources/remove":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                data = self.read_json()
                source_id = str(data.get("source_id") or "").strip()
                if not source_id:
                    self.send_json({"ok": False, "error": "source_id가 필요합니다"}, status=400)
                    return
                database.delete_custom_source(source_id)
                database.delete_notices_by_source(source_id, spare_favorites=True)
                self.send_json({"ok": True})
                return
            if path == "/api/favorite/toggle":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                data = self.read_json()
                notice_id = data.get("notice_id")
                if not notice_id:
                    self.send_json({"ok": False, "error": "notice_id required"}, status=400)
                    return
                fav = database.toggle_favorite(user["id"], notice_id)
                self.send_json({"ok": True, "favorite": fav})
                return
            if path == "/api/company":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                data = self.read_json()
                database.set_user_setting(user["id"], "company", data)
                self.send_json({"ok": True, "company": data})
                return
            if path == "/api/company/documents":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                if len(database.list_company_documents(user["id"])) >= MAX_COMPANY_DOCUMENTS:
                    self.send_json(
                        {"ok": False, "error": f"문서는 최대 {MAX_COMPANY_DOCUMENTS}개까지 등록할 수 있습니다."}, status=400
                    )
                    return
                content_type = self.headers.get("Content-Type", "")
                body = self.read_raw_body()
                try:
                    files = uploads.parse_multipart(content_type, body)
                    if not files:
                        raise ValueError("업로드된 파일이 없습니다.")
                    for f in files:
                        text = uploads.extract_text(f.filename, f.data)
                        database.add_company_document(user["id"], f.filename, text)
                except ValueError as e:
                    self.send_json({"ok": False, "error": str(e)}, status=400)
                    return
                docs = database.list_company_documents(user["id"])
                items = [{"id": d["id"], "filename": d["filename"], "char_count": d["char_count"], "created_at": d["created_at"]} for d in docs]
                self.send_json({"ok": True, "items": items})
                return
            if path == "/api/company/documents/delete":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                data = self.read_json()
                doc_id = data.get("doc_id")
                if not doc_id:
                    self.send_json({"ok": False, "error": "doc_id required"}, status=400)
                    return
                database.delete_company_document(user["id"], doc_id)
                self.send_json({"ok": True})
                return
            if path == "/api/ai-fit":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                model_id, api_key = resolve_active_llm(user)
                if not api_key:
                    self.send_json(
                        {"ok": False, "error": "먼저 '회사 정보'에서 본인의 API 키를 등록해주세요."}, status=400
                    )
                    return
                company = database.get_user_setting(user["id"], "company", {})
                documents = database.list_company_documents(user["id"])
                # 화면에 아예 안 보이는 공고(자금성 판정 exclude, 비활성화된 소스)까지
                # 판정하면 LLM 호출만 낭비되고 결과도 쓸 데가 없다 — add_runtime_fields()가
                # /api/notices 목록에 적용하는 것과 같은 필터를 여기서도 그대로 적용한다.
                notices = add_runtime_fields(database.list_notices())
                results = ai_match.judge_company_fit(
                    notices, company, api_key, documents=documents, model_id=model_id
                )
                database.save_ai_fit(user["id"], results, company_profile_hash(company))
                counts: Dict[str, int] = {}
                for r in results.values():
                    key = r.get("fit") or "unsure"
                    counts[key] = counts.get(key, 0) + 1
                self.send_json({"ok": True, "count": len(results), "counts": counts})
                return
            self.send_json({"ok": False, "error": "not found"}, status=404)
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def serve_static(self, path: str) -> None:
        if path in {"/", ""}:
            fp = STATIC / "index.html"
        else:
            rel = path.lstrip("/")
            if rel.startswith("static/"):
                rel = rel[len("static/"):]
            fp = STATIC / rel
        try:
            fp = fp.resolve()
            if not str(fp).startswith(str(STATIC.resolve())) or not fp.exists() or not fp.is_file():
                self.send_json({"ok": False, "error": "not found"}, status=404)
                return
            ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
            if fp.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            elif fp.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            elif fp.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            self.send_body(fp.read_bytes(), ctype)
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def export_csv(self, user: Optional[Dict[str, Any]] = None) -> None:
        items = add_runtime_fields(database.list_notices())
        if not (user and user.get("bizinfo_api_key_enc")):
            items = database.hide_bizinfo_only(items)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["source", "title", "org", "category", "status", "start", "end", "dday", "budget", "url"])
        for a in items:
            w.writerow([a.get("src"), a.get("title"), a.get("org"), a.get("category"), a.get("status"), a.get("start"), a.get("end"), a.get("dday"), a.get("budget"), a.get("url")])
        body = "\ufeff" + buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=gongo_notices.csv")
        data = body.encode("utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

def bootstrap() -> None:
    database.init_db()
    if not database.list_notices():
        try:
            collector.collect_all(write_db=True)
        except Exception as e:
            print(f"Initial collection failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    bootstrap()
    scheduler_thread = scheduler.start(_shutdown_event, _collect_lock)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"공고모아 실행 중: http://{args.host}:{args.port}")
    print(f"Root: {ROOT}")
    print("Stop: Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_event.set()
        scheduler_thread.join(timeout=5)


if __name__ == "__main__":
    main()
