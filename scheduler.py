# -*- coding: utf-8 -*-
"""자동 수집 스케줄러 — 서버 프로세스 안에서 백그라운드 스레드로 돌며, 관리자가 DB에
저장한 일정(매일 지정 시간 또는 N시간마다)에 맞춰 collector.collect_all()을 호출한다.

관리자 화면에서 바로 켜고 끄고 시간을 바꿀 수 있어야 해서 config.json이 아니라 DB
(app_settings)에 저장한다 — source_overrides/custom_sources가 이미 그런 이유로
config.json 대신 DB를 쓰고 있는 것과 같은 이유다.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import collector
import database

SETTING_KEY = "collection_schedule"
LAST_RUN_KEY = "collection_schedule_last_run"
POLL_INTERVAL_SEC = 60
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_SCHEDULE: Dict[str, Any] = {
    "enabled": False,
    "mode": "daily",
    "time": "03:00",
    "days": list(WEEKDAY_CODES),
    "interval_hours": 6,
}


def get_schedule() -> Dict[str, Any]:
    return database.get_app_setting(SETTING_KEY, DEFAULT_SCHEDULE)


def get_last_run() -> Optional[str]:
    return database.get_app_setting(LAST_RUN_KEY, None)


def should_run_now(cfg: Dict[str, Any], last_run_iso: Optional[str], now: datetime) -> bool:
    """cfg만 보고 지금이 실행할 때인지 판단하는 순수 함수 — 테스트하기 쉽게 실제 시계/DB와
    분리해뒀다."""
    last_run = None
    if last_run_iso:
        try:
            last_run = datetime.fromisoformat(last_run_iso)
        except ValueError:
            last_run = None

    if cfg.get("mode") == "interval":
        hours = float(cfg.get("interval_hours") or 0)
        if hours <= 0:
            return False
        if last_run is None:
            return True
        return now - last_run >= timedelta(hours=hours)

    # mode == "daily"
    today_code = WEEKDAY_CODES[now.weekday()]
    if today_code not in (cfg.get("days") or []):
        return False
    try:
        target_h, target_m = (int(x) for x in str(cfg.get("time") or "00:00").split(":"))
    except ValueError:
        return False
    if (now.hour, now.minute) < (target_h, target_m):
        return False
    # 오늘 이미 실행했으면 다시 안 한다(시간이 지나 있는 동안 매 폴링마다 또 도는 것을 방지).
    if last_run is not None and last_run.date() == now.date():
        return False
    return True


def next_run_estimate(cfg: Dict[str, Any], last_run_iso: Optional[str], now: datetime) -> Optional[str]:
    """관리자 화면에 참고용으로만 보여준다 — 실행 여부 판단에는 쓰이지 않는다."""
    if not cfg.get("enabled"):
        return None
    if cfg.get("mode") == "interval":
        hours = float(cfg.get("interval_hours") or 0)
        if hours <= 0:
            return None
        last_run = None
        if last_run_iso:
            try:
                last_run = datetime.fromisoformat(last_run_iso)
            except ValueError:
                last_run = None
        base = last_run or now
        return (base + timedelta(hours=hours)).isoformat(timespec="seconds")

    try:
        target_h, target_m = (int(x) for x in str(cfg.get("time") or "00:00").split(":"))
    except ValueError:
        return None
    days = cfg.get("days") or []
    for offset in range(8):
        candidate_date = now.date() + timedelta(days=offset)
        if WEEKDAY_CODES[candidate_date.weekday()] not in days:
            continue
        candidate = datetime.combine(candidate_date, datetime.min.time()).replace(hour=target_h, minute=target_m)
        if candidate > now:
            return candidate.isoformat(timespec="seconds")
    return None


def run_collection_locked(lock: threading.Lock) -> Optional["collector.CollectRun"]:
    """수동(/api/recollect)과 스케줄러가 공유하는 유일한 진입점 — collect_all()이 두
    곳에서 동시에 실행되는 걸 막는다. 이미 다른 수집이 진행 중이면 None을 돌려준다."""
    if not lock.acquire(blocking=False):
        return None
    try:
        return collector.collect_all(write_db=True)
    finally:
        lock.release()


def scheduler_loop(shutdown_event: threading.Event, lock: threading.Lock) -> None:
    while not shutdown_event.wait(POLL_INTERVAL_SEC):
        try:
            cfg = get_schedule()
            if not cfg.get("enabled"):
                continue
            now = datetime.now()
            if not should_run_now(cfg, get_last_run(), now):
                continue
            run = run_collection_locked(lock)
            if run is not None:
                database.set_app_setting(LAST_RUN_KEY, now.isoformat(timespec="seconds"))
        except Exception:
            # 이 스레드가 죽으면 그 뒤로는 영원히 자동 수집이 멈춘다 — 한 번의 실패가
            # 다음 폴링을 막으면 안 되므로 무조건 잡고 계속 돈다.
            traceback.print_exc()


def start(shutdown_event: threading.Event, lock: threading.Lock) -> threading.Thread:
    thread = threading.Thread(target=scheduler_loop, args=(shutdown_event, lock), daemon=True)
    thread.start()
    return thread
