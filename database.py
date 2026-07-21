# -*- coding: utf-8 -*-
"""공고모아 SQLite 헬퍼."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "gongo.sqlite"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _ensure_column(con: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """이미 배포된 DB에 새 컬럼을 안전하게 추가한다 (있으면 아무 것도 안 함)."""
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _migrate_to_per_user(con: sqlite3.Connection, table: str) -> None:
    """즐겨찾기/AI 판정 테이블을 사용자별 구조로 옮긴다.

    두 테이블 모두 원래 notice_id 하나만 PRIMARY KEY였기 때문에, 단순
    ALTER TABLE ADD COLUMN으로는 사용자별로 여러 행을 가질 수 없다
    (같은 notice_id에 사용자별로 다른 값이 있어야 하는데 기존 PK가 막는다).
    이미 이 앱은 배포 직전 단계라 예전 단일 사용자 데이터는 보존할 필요가
    없으므로, user_id 컬럼이 없는 옛 스키마를 발견하면 통째로 지우고
    새 스키마(복합 PK)로 다시 만든다.
    """
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if cols and "user_id" not in cols:
        con.execute(f"DROP TABLE {table}")


def init_db() -> None:
    with connect() as con:
        _migrate_to_per_user(con, "favorites")
        _migrate_to_per_user(con, "ai_fit")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS notices (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                org TEXT,
                category TEXT,
                start_date TEXT,
                end_date TEXT,
                budget TEXT,
                elig_json TEXT,
                url TEXT,
                raw_json TEXT,
                dates_unknown INTEGER DEFAULT 0,
                rolling_confirmed INTEGER DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                name TEXT,
                method TEXT,
                state TEXT,
                count INTEGER DEFAULT 0,
                last_collected_at TEXT,
                error TEXT,
                anomaly INTEGER DEFAULT 0,
                anomaly_note TEXT
            );

            CREATE TABLE IF NOT EXISTS source_overrides (
                source_id TEXT PRIMARY KEY,
                list_url TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_run_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                count INTEGER NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_source_run_history_source
                ON source_run_history(source_id, created_at);

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                anthropic_api_key_enc TEXT,
                bizinfo_api_key_enc TEXT,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_documents (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                char_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS favorites (
                user_id TEXT NOT NULL,
                notice_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, notice_id),
                FOREIGN KEY(notice_id) REFERENCES notices(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notice_sources (
                notice_id TEXT NOT NULL,
                source TEXT NOT NULL,
                url TEXT,
                PRIMARY KEY (notice_id, source),
                FOREIGN KEY(notice_id) REFERENCES notices(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ai_fit (
                user_id TEXT NOT NULL,
                notice_id TEXT NOT NULL,
                fit TEXT,
                reason TEXT,
                profile_hash TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, notice_id),
                FOREIGN KEY(notice_id) REFERENCES notices(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_notices_source ON notices(source);
            CREATE INDEX IF NOT EXISTS idx_notices_end ON notices(end_date);
            CREATE INDEX IF NOT EXISTS idx_notices_category ON notices(category);
            """
        )
        _ensure_column(con, "users", "bizinfo_api_key_enc", "TEXT")
        _ensure_column(con, "users", "is_admin", "INTEGER DEFAULT 0")
        _ensure_column(con, "sources", "anomaly", "INTEGER DEFAULT 0")
        _ensure_column(con, "sources", "anomaly_note", "TEXT")
        _ensure_column(con, "notices", "dates_unknown", "INTEGER DEFAULT 0")
        _ensure_column(con, "notices", "rolling_confirmed", "INTEGER DEFAULT 0")


def upsert_notices(items: Iterable[Dict[str, Any]], prune: bool = True) -> int:
    """Insert/update notices and their per-site source list.

    `prune=True` removes notices that are no longer part of the current
    collection run (and are not favorited), so notices that a source stops
    reporting -- or duplicate rows left behind by an old id scheme -- do not
    accumulate forever. Callers should pass `prune=False` when `items` is a
    partial/fallback set (e.g. the sample data shown when every real source
    fails) so a temporary outage doesn't wipe out previously collected data.
    """
    init_db()
    items = list(items)
    count = 0
    ts = now_iso()
    ids: List[str] = []
    with connect() as con:
        for a in items:
            nid = a.get("id")
            if not nid:
                continue
            ids.append(nid)
            con.execute(
                """
                INSERT INTO notices (
                    id, source, title, org, category, start_date, end_date,
                    budget, elig_json, url, raw_json, dates_unknown, rolling_confirmed, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source=excluded.source,
                    title=excluded.title,
                    org=excluded.org,
                    category=excluded.category,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    budget=excluded.budget,
                    elig_json=excluded.elig_json,
                    url=excluded.url,
                    raw_json=excluded.raw_json,
                    dates_unknown=excluded.dates_unknown,
                    rolling_confirmed=excluded.rolling_confirmed,
                    updated_at=excluded.updated_at
                """,
                (
                    nid,
                    a.get("src") or a.get("source") or "unknown",
                    a.get("title") or "제목 없음",
                    a.get("org") or "기관 미표기",
                    a.get("category") or "기타",
                    a.get("start") or a.get("start_date"),
                    a.get("end") or a.get("end_date"),
                    a.get("budget") or "공고 참조",
                    json.dumps(a.get("elig"), ensure_ascii=False) if a.get("elig") is not None else None,
                    a.get("url") or "",
                    json.dumps(a, ensure_ascii=False),
                    1 if a.get("dates_unknown") else 0,
                    1 if a.get("rolling_confirmed") else 0,
                    ts,
                    ts,
                ),
            )
            sources = a.get("sources") or [{"id": a.get("src") or a.get("source") or "unknown", "url": a.get("url")}]
            con.execute("DELETE FROM notice_sources WHERE notice_id=?", (nid,))
            seen_src = set()
            for s in sources:
                sid = s.get("id")
                if not sid or sid in seen_src:
                    continue
                seen_src.add(sid)
                con.execute(
                    "INSERT OR REPLACE INTO notice_sources(notice_id, source, url) VALUES (?, ?, ?)",
                    (nid, sid, s.get("url") or ""),
                )
            count += 1
        if prune and ids:
            placeholders = ",".join("?" * len(ids))
            con.execute(
                f"DELETE FROM notices WHERE id NOT IN ({placeholders}) AND id NOT IN (SELECT notice_id FROM favorites)",
                ids,
            )
    return count


def replace_source_status(statuses: Dict[str, Dict[str, Any]]) -> None:
    init_db()
    ts = now_iso()
    with connect() as con:
        for sid, s in statuses.items():
            con.execute(
                """
                INSERT INTO sources (id, name, method, state, count, last_collected_at, error, anomaly, anomaly_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    method=excluded.method,
                    state=excluded.state,
                    count=excluded.count,
                    last_collected_at=excluded.last_collected_at,
                    error=excluded.error,
                    anomaly=excluded.anomaly,
                    anomaly_note=excluded.anomaly_note
                """,
                (
                    sid,
                    s.get("name") or sid,
                    s.get("method") or "",
                    s.get("state") or "미확인",
                    int(s.get("n") or s.get("count") or 0),
                    s.get("last") or ts,
                    s.get("error"),
                    1 if s.get("anomaly") else 0,
                    s.get("anomaly_note"),
                ),
            )


HISTORY_KEEP_PER_SOURCE = 20
ANOMALY_ZERO_MIN_AVG = 3       # 0건 경고를 띄우려면 최근 평균이 최소 이 정도는 되어야 함
ANOMALY_DROP_MIN_AVG = 5       # 급감 경고를 띄우려면 최근 평균이 최소 이 정도는 되어야 함
ANOMALY_DROP_RATIO = 0.3       # 최근 평균의 이 비율 미만이면 급감으로 간주


def record_source_history(statuses: Dict[str, Dict[str, Any]]) -> None:
    """매 수집 실행마다 소스별 건수를 append하고, 소스당 최근 N건만 남긴다."""
    init_db()
    ts = now_iso()
    with connect() as con:
        for sid, s in statuses.items():
            con.execute(
                "INSERT INTO source_run_history(source_id, count, state, error, created_at) VALUES (?, ?, ?, ?, ?)",
                (sid, int(s.get("n") or s.get("count") or 0), s.get("state") or "미확인", s.get("error"), ts),
            )
            con.execute(
                """
                DELETE FROM source_run_history
                WHERE source_id = ? AND id NOT IN (
                    SELECT id FROM source_run_history WHERE source_id = ?
                    ORDER BY created_at DESC LIMIT ?
                )
                """,
                (sid, sid, HISTORY_KEEP_PER_SOURCE),
            )


def flag_source_anomalies(statuses: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """이번 실행 건수를 소스별 최근 이력 평균과 비교해 이상 급감을 표시한다.

    반드시 `record_source_history`로 이번 실행을 기록하기 *전에* 호출해야
    한다 (그래야 비교 기준이 이번 실행을 제외한 과거 이력이 된다).
    """
    init_db()
    out: Dict[str, Dict[str, Any]] = {}
    with connect() as con:
        for sid, s in statuses.items():
            entry = dict(s)
            rows = con.execute(
                "SELECT count FROM source_run_history WHERE source_id = ? ORDER BY created_at DESC LIMIT ?",
                (sid, HISTORY_KEEP_PER_SOURCE),
            ).fetchall()
            positive = [r["count"] for r in rows if r["count"] > 0]
            avg = sum(positive) / len(positive) if positive else 0
            current = int(s.get("n") or s.get("count") or 0)
            entry["anomaly"] = False
            entry["anomaly_note"] = None
            if len(positive) >= 3 and avg >= ANOMALY_ZERO_MIN_AVG and current == 0:
                entry["anomaly"] = True
                entry["anomaly_note"] = f"최근 평균 {avg:.0f}건 대비 0건 — 사이트 구조 변경으로 수집이 깨졌을 수 있습니다."
            elif len(positive) >= 3 and avg >= ANOMALY_DROP_MIN_AVG and 0 < current < avg * ANOMALY_DROP_RATIO:
                entry["anomaly"] = True
                entry["anomaly_note"] = f"최근 평균 {avg:.0f}건 대비 {current}건으로 급감했습니다."
            out[sid] = entry
    return out


def get_source_overrides() -> Dict[str, str]:
    """관리자가 재정의한 소스별 수집 URL을 반환한다 ({source_id: list_url})."""
    init_db()
    with connect() as con:
        rows = con.execute(
            "SELECT source_id, list_url FROM source_overrides WHERE list_url IS NOT NULL AND list_url != ''"
        ).fetchall()
    return {r["source_id"]: r["list_url"] for r in rows}


def set_source_override(source_id: str, list_url: Optional[str]) -> None:
    """소스 수집 URL을 재정의한다. list_url이 비어있으면 재정의를 해제한다."""
    init_db()
    with connect() as con:
        con.execute(
            """
            INSERT INTO source_overrides(source_id, list_url, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET list_url=excluded.list_url, updated_at=excluded.updated_at
            """,
            (source_id, list_url or None, now_iso()),
        )


def row_to_notice(row: sqlite3.Row) -> Dict[str, Any]:
    elig = None
    if row["elig_json"]:
        try:
            elig = json.loads(row["elig_json"])
        except Exception:
            elig = None
    return {
        "id": row["id"],
        "src": row["source"],
        "title": row["title"],
        "org": row["org"],
        "category": row["category"],
        "start": row["start_date"],
        "end": row["end_date"],
        "budget": row["budget"],
        "elig": elig,
        "dates_unknown": bool(row["dates_unknown"]) if "dates_unknown" in row.keys() else False,
        "rolling_confirmed": bool(row["rolling_confirmed"]) if "rolling_confirmed" in row.keys() else False,
        "url": row["url"],
        "favorite": bool(row["favorite"]),
        "first_seen_at": row["first_seen_at"],
        "updated_at": row["updated_at"],
    }


def list_notices(
    q: str = "", source: str = "", category: str = "", favorite: bool = False, user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    init_db()
    where = []
    args: List[Any] = []
    if q:
        where.append("(n.title LIKE ? OR n.org LIKE ? OR n.category LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like])
    if source:
        where.append("n.id IN (SELECT notice_id FROM notice_sources WHERE source = ?)")
        args.append(source)
    if category:
        where.append("n.category = ?")
        args.append(category)
    if favorite:
        where.append("f.notice_id IS NOT NULL")
    sql = """
        SELECT n.*, CASE WHEN f.notice_id IS NULL THEN 0 ELSE 1 END AS favorite
        FROM notices n
        LEFT JOIN favorites f ON f.notice_id = n.id AND f.user_id = ?
    """
    args = [user_id, *args]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE WHEN n.end_date IS NULL THEN 1 ELSE 0 END, n.end_date ASC, n.updated_at DESC"
    with connect() as con:
        notices = [row_to_notice(r) for r in con.execute(sql, args)]
        ids = [n["id"] for n in notices]
        by_id: Dict[str, List[Dict[str, Any]]] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            for r in con.execute(
                f"SELECT notice_id, source, url FROM notice_sources WHERE notice_id IN ({placeholders})", ids
            ):
                by_id.setdefault(r["notice_id"], []).append({"id": r["source"], "url": r["url"]})
        for n in notices:
            n["sources"] = by_id.get(n["id"]) or ([{"id": n["src"], "url": n["url"]}] if n.get("src") else [])
        return notices


def hide_bizinfo_only(notices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """기업마당 API로만 확보된 공고(다른 무료 소스에 없는 것)를 걸러낸다.

    기업마당 API 키를 등록하지 않은 사용자(비로그인 포함)에게는 이 공고들을
    숨긴다. K-스타트업 등 무료 소스에도 함께 올라온 공고는 그 소스를 통해
    합법적으로 볼 수 있으므로 그대로 노출한다.
    """
    out = []
    for n in notices:
        source_ids = {s.get("id") for s in (n.get("sources") or [])} or {n.get("src")}
        if source_ids - {"bizinfo"}:
            out.append(n)
    return out


SOURCE_ALIASES = {
    "biohub_direct": "biohub",
    "khidi_direct": "khidi",
}

SOURCE_META = {
    "kstartup": {"name": "K-스타트업", "method": "HTML"},
    "bizinfo": {"name": "기업마당", "method": "API"},
    "biohub": {"name": "서울바이오허브", "method": "직접/기업마당"},
    "khidi": {"name": "보건산업진흥원/KHIDI", "method": "직접/기업마당"},
    "kddf": {"name": "국가신약개발사업단", "method": "게시판"},
    "nrf": {"name": "한국연구재단", "method": "게시판"},
    "g2b": {"name": "나라장터", "method": "API"},
    "sample": {"name": "샘플", "method": "내장 데이터"},
}

SOURCE_ORDER = ["bizinfo", "kstartup", "biohub", "khidi", "kddf", "nrf", "g2b", "sample"]


def canonical_source_id(source_id: str) -> str:
    return SOURCE_ALIASES.get(source_id, source_id)


def list_sources(clean: bool = True) -> List[Dict[str, Any]]:
    """Return source status for the UI.

    The collector can create both routed and direct rows, for example
    `biohub` plus `biohub_direct`.  The sidebar should not show those as
    separate duplicated sources.  This function aggregates aliases, uses the
    actual notice table count as the source of truth, and hides zero-count
    waiting/disabled/error rows from the compact UI.
    """
    init_db()
    with connect() as con:
        source_rows = [dict(r) for r in con.execute("SELECT * FROM sources ORDER BY id")]
        notice_counts: Dict[str, int] = {}
        for r in con.execute("SELECT source, COUNT(*) AS n FROM notices GROUP BY source"):
            sid = canonical_source_id(r["source"])
            notice_counts[sid] = notice_counts.get(sid, 0) + int(r["n"])

    if not clean:
        return source_rows

    grouped: Dict[str, Dict[str, Any]] = {}

    # Seed groups from status rows so names/methods are preserved.
    for row in source_rows:
        sid = canonical_source_id(row.get("id") or "unknown")
        meta = SOURCE_META.get(sid, {})
        g = grouped.setdefault(sid, {
            "id": sid,
            "name": meta.get("name") or row.get("name") or sid,
            "method": meta.get("method") or row.get("method") or "",
            "state": row.get("state") or "미확인",
            "count": 0,
            "last_collected_at": row.get("last_collected_at"),
            "error": None,
            "anomaly": False,
            "anomaly_note": None,
        })
        if row.get("last_collected_at") and (not g.get("last_collected_at") or row.get("last_collected_at") > g.get("last_collected_at")):
            g["last_collected_at"] = row.get("last_collected_at")
        if row.get("error") and not g.get("error"):
            g["error"] = row.get("error")
        if row.get("anomaly") and not g.get("anomaly"):
            g["anomaly"] = True
            g["anomaly_note"] = row.get("anomaly_note")
        # Keep a non-waiting state only when there is no actual notice count.
        if g.get("state") in {"대기", "비활성화", "0건", "미확인"} and row.get("state"):
            g["state"] = row.get("state")

    # Actual notice count is the source of truth.  If a source has notices, the
    # UI state should be normal even if the latest collection attempt produced
    # an error row.
    for sid, cnt in notice_counts.items():
        meta = SOURCE_META.get(sid, {})
        g = grouped.setdefault(sid, {
            "id": sid,
            "name": meta.get("name") or sid,
            "method": meta.get("method") or "",
            "state": "정상",
            "count": 0,
            "last_collected_at": None,
            "error": None,
            "anomaly": False,
            "anomaly_note": None,
        })
        g["count"] = int(cnt)
        if cnt > 0:
            g["state"] = "정상"
            g["error"] = None

    # If real sources exist, do not show sample fallback in the sidebar.
    real_total = sum(v.get("count", 0) for k, v in grouped.items() if k != "sample")

    cleaned: List[Dict[str, Any]] = []
    for sid, g in grouped.items():
        cnt = int(g.get("count") or 0)
        state = g.get("state") or "미확인"
        if sid == "sample" and real_total > 0:
            continue
        # Hide non-actionable zero rows.  They are still available in the raw DB,
        # but the user-facing sidebar should be clean and non-duplicated.
        # Anomaly-flagged rows are the one exception: hiding those would defeat
        # the whole point of surfacing a silent scraping failure.
        if cnt == 0 and state in {"대기", "비활성화", "0건", "오류", "차단(robots)", "미확인"} and not g.get("anomaly"):
            continue
        cleaned.append(g)

    cleaned.sort(key=lambda x: SOURCE_ORDER.index(x["id"]) if x["id"] in SOURCE_ORDER else 999)
    return cleaned


def toggle_favorite(user_id: str, notice_id: str) -> bool:
    init_db()
    with connect() as con:
        exists = con.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND notice_id=?", (user_id, notice_id)
        ).fetchone()
        if exists:
            con.execute("DELETE FROM favorites WHERE user_id=? AND notice_id=?", (user_id, notice_id))
            return False
        con.execute(
            "INSERT OR IGNORE INTO favorites(user_id, notice_id, created_at) VALUES (?, ?, ?)",
            (user_id, notice_id, now_iso()),
        )
        return True


def favorite_ids(user_id: str) -> List[str]:
    init_db()
    with connect() as con:
        return [
            r[0]
            for r in con.execute(
                "SELECT notice_id FROM favorites WHERE user_id=? ORDER BY created_at DESC", (user_id,)
            )
        ]


def save_ai_fit(user_id: str, results: Dict[str, Dict[str, str]], profile_hash: str) -> None:
    init_db()
    ts = now_iso()
    with connect() as con:
        for nid, r in results.items():
            con.execute(
                """
                INSERT INTO ai_fit(user_id, notice_id, fit, reason, profile_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, notice_id) DO UPDATE SET
                    fit=excluded.fit,
                    reason=excluded.reason,
                    profile_hash=excluded.profile_hash,
                    updated_at=excluded.updated_at
                """,
                (user_id, nid, r.get("fit"), r.get("reason"), profile_hash, ts),
            )


def get_ai_fit_map(user_id: str, profile_hash: str) -> Dict[str, Dict[str, str]]:
    init_db()
    with connect() as con:
        rows = con.execute(
            "SELECT notice_id, fit, reason FROM ai_fit WHERE user_id=? AND profile_hash=?", (user_id, profile_hash)
        ).fetchall()
    return {r["notice_id"]: {"fit": r["fit"], "reason": r["reason"]} for r in rows}


def get_user_setting(user_id: str, key: str, default: Optional[Any] = None) -> Any:
    init_db()
    with connect() as con:
        row = con.execute(
            "SELECT value_json FROM user_settings WHERE user_id=? AND key=?", (user_id, key)
        ).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except Exception:
        return default


def set_user_setting(user_id: str, key: str, value: Any) -> None:
    init_db()
    with connect() as con:
        con.execute(
            """
            INSERT INTO user_settings(user_id, key, value_json, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (user_id, key, json.dumps(value, ensure_ascii=False), now_iso()),
        )


# ──────────────────────────── 계정 / 세션 ────────────────────────────

def create_user(email: str, password_salt: str, password_hash: str) -> str:
    init_db()
    uid = uuid.uuid4().hex
    with connect() as con:
        con.execute(
            "INSERT INTO users(id, email, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (uid, email.strip().lower(), password_salt, password_hash, now_iso()),
        )
    return uid


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    init_db()
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def set_user_api_key(user_id: str, encrypted_key: Optional[str]) -> None:
    init_db()
    with connect() as con:
        con.execute("UPDATE users SET anthropic_api_key_enc=? WHERE id=?", (encrypted_key, user_id))


def set_user_admin(user_id: str, is_admin: bool) -> None:
    init_db()
    with connect() as con:
        con.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, user_id))


def set_user_bizinfo_key(user_id: str, encrypted_key: Optional[str]) -> None:
    init_db()
    with connect() as con:
        con.execute("UPDATE users SET bizinfo_api_key_enc=? WHERE id=?", (encrypted_key, user_id))


def create_session(user_id: str, token: str, ttl_days: int = 30) -> None:
    init_db()
    ts = now_iso()
    expires = (datetime.now() + timedelta(days=ttl_days)).isoformat(timespec="seconds")
    with connect() as con:
        con.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, ts, expires),
        )


def get_session_user(token: str) -> Optional[Dict[str, Any]]:
    init_db()
    with connect() as con:
        row = con.execute(
            """
            SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now_iso()),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    init_db()
    with connect() as con:
        con.execute("DELETE FROM sessions WHERE token=?", (token,))


# ──────────────────────────── 회사 문서 ────────────────────────────

def add_company_document(user_id: str, filename: str, content: str) -> str:
    init_db()
    doc_id = uuid.uuid4().hex
    with connect() as con:
        con.execute(
            "INSERT INTO company_documents(id, user_id, filename, content, char_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, user_id, filename, content, len(content), now_iso()),
        )
    return doc_id


def list_company_documents(user_id: str) -> List[Dict[str, Any]]:
    init_db()
    with connect() as con:
        rows = con.execute(
            "SELECT id, filename, content, char_count, created_at FROM company_documents WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_company_document(user_id: str, doc_id: str) -> None:
    init_db()
    with connect() as con:
        con.execute("DELETE FROM company_documents WHERE user_id=? AND id=?", (user_id, doc_id))
