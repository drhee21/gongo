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
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

import auth
import ai_match
import collector
import database
import uploads

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

SESSION_COOKIE = "session"
SESSION_TTL_DAYS = 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RECOLLECT_COOLDOWN_SEC = 300
_last_recollect_at: Optional[datetime] = None

ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()
}
OVERRIDABLE_SOURCES = {"kstartup", "nrf", "kddf", "biohub_direct", "khidi_direct"}
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


def status_of(a: Dict[str, Any]) -> str:
    end = a.get("end")
    start = a.get("start")
    today = datetime.now().date()
    if a.get("dates_unknown"):
        return "날짜 미상"
    if not end:
        # 상시/수시/소진 같은 명시적 표현으로 확인된 경우만 진짜 상시로 보여준다.
        # 그런 표현 없이 시작일만 알고 마감일을 못 찾은 경우는 dates_unknown인
        # 경우(정보가 아예 없음)와 화면 상태는 동일하게 "날짜 미상"으로 보여준다 —
        # 다만 dates_unknown 자체는 데이터에서 계속 구분해서 남기므로, 시작일을
        # 알고 있으면 메타 줄에는 그 시작일이 그대로 표시된다.
        return "상시" if a.get("rolling_confirmed") else "날짜 미상"
    try:
        e = datetime.strptime(end, "%Y-%m-%d").date()
        if e < today:
            return "마감"
        if start:
            s = datetime.strptime(start, "%Y-%m-%d").date()
            if s > today:
                return "접수예정"
        return "접수중"
    except Exception:
        return "날짜 미상"


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
    out = []
    for a in items:
        b = dict(a)
        b["src"] = canonical_source_id(b.get("src") or b.get("source") or "unknown")
        b["status"] = status_of(b)
        b["dday"] = dday(b)
        out.append(b)
    return out


def company_profile_hash(company: Dict[str, Any]) -> str:
    payload = json.dumps(company or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def attach_ai_fit(items, user_id: Optional[str], company: Dict[str, Any]):
    if not user_id or not company:
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


def user_public(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "email": user["email"],
        "has_api_key": bool(user.get("anthropic_api_key_enc")),
        "has_bizinfo_key": bool(user.get("bizinfo_api_key_enc")),
        "is_admin": bool(user.get("is_admin")),
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
                self.send_json({"ok": True, "items": database.list_sources(), "raw": database.list_sources(clean=False)})
                return
            if path == "/api/admin/source-overrides":
                user = current_user(self)
                if not (user and user.get("is_admin")):
                    self.send_json({"ok": False, "error": "관리자 권한이 필요합니다"}, status=403)
                    return
                cfg = collector.load_config()
                overrides = database.get_source_overrides()
                boards = cfg.get("boards") or {}
                items = [
                    {"source_id": sid, "name": name, "default_url": default_url, "override_url": overrides.get(sid)}
                    for sid, name, default_url in [
                        ("kstartup", "K-스타트업", (cfg.get("kstartup") or {}).get("list_url")),
                        ("nrf", "한국연구재단", (boards.get("nrf") or {}).get("list_url")),
                        ("kddf", "국가신약개발사업단", (boards.get("kddf") or {}).get("list_url")),
                        ("biohub_direct", "서울바이오허브", (boards.get("biohub_direct") or {}).get("list_url")),
                        ("khidi_direct", "보건산업진흥원/KHIDI", (boards.get("khidi_direct") or {}).get("list_url")),
                    ]
                ]
                self.send_json({"ok": True, "items": items})
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
            if path == "/api/me/api-key":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                data = self.read_json()
                raw_key = str(data.get("api_key") or "").strip()
                if not raw_key:
                    database.set_user_api_key(user["id"], None)
                    self.send_json({"ok": True, "has_api_key": False})
                    return
                if not raw_key.startswith("sk-ant-"):
                    self.send_json({"ok": False, "error": "Claude API 키 형식이 아닙니다 ('sk-ant-'로 시작해야 합니다)"}, status=400)
                    return
                database.set_user_api_key(user["id"], auth.encrypt_secret(raw_key))
                self.send_json({"ok": True, "has_api_key": True})
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
            if path == "/api/recollect":
                user = current_user(self)
                if not user:
                    self.send_json({"ok": False, "error": "로그인이 필요합니다"}, status=401)
                    return
                global _last_recollect_at
                now = datetime.now()
                if _last_recollect_at and (now - _last_recollect_at).total_seconds() < RECOLLECT_COOLDOWN_SEC:
                    wait = int(RECOLLECT_COOLDOWN_SEC - (now - _last_recollect_at).total_seconds())
                    self.send_json({"ok": False, "error": f"너무 자주 요청했습니다. {wait}초 후 다시 시도해주세요."}, status=429)
                    return
                _last_recollect_at = now
                run = collector.collect_all(write_db=True)
                self.send_json({"ok": True, "count": len(run.items), "sources": run.sources})
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
                list_url = str(data.get("list_url") or "").strip()
                database.set_source_override(source_id, list_url or None)
                self.send_json({"ok": True, "source_id": source_id, "override_url": list_url or None})
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
                enc_key = user.get("anthropic_api_key_enc")
                if not enc_key:
                    self.send_json(
                        {"ok": False, "error": "먼저 '회사 정보'에서 본인의 Claude API 키를 등록해주세요."}, status=400
                    )
                    return
                api_key = auth.decrypt_secret(enc_key)
                company = database.get_user_setting(user["id"], "company", {})
                documents = database.list_company_documents(user["id"])
                notices = database.list_notices()
                results = ai_match.judge_company_fit(notices, company, api_key, documents=documents)
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
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"공고모아 실행 중: http://{args.host}:{args.port}")
    print(f"Root: {ROOT}")
    print("Stop: Ctrl+C")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
