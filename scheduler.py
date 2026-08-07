# -*- coding: utf-8 -*-
"""자동 수집 스케줄러 — 서버 프로세스 안에서 백그라운드 스레드로 돌며, 관리자가 DB에
저장한 일정(지정 요일·시간에, N주 간격으로)에 맞춰 collector.collect_all()을 호출한다.

예전에는 "매일 지정 시간에"와 "N시간마다"를 별개 모드로 뒀었는데, 요일/시간 없이 흐르는
간격 모드와 간격 없이 매주 도는 요일 모드가 서로의 기능을 하나씩 빠뜨리고 있어서(전자는
시간 지정이 안 되고, 후자는 매주보다 더 긴 간격을 못 나타냄) 하나로 합쳤다 — 요일·시간
선택은 그대로 두고, 거기에 "최소 N주는 지나야 다시 돈다"는 간격 조건을 얹었다.
interval_weeks=1이면 예전 "매일 지정 시간에" 모드와 동일하게 동작한다.

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
    "time": "03:00",
    "days": list(WEEKDAY_CODES),
    "interval_weeks": 1,
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

    today_code = WEEKDAY_CODES[now.weekday()]
    if today_code not in (cfg.get("days") or []):
        return False
    try:
        target_h, target_m = (int(x) for x in str(cfg.get("time") or "00:00").split(":"))
    except ValueError:
        return False
    if (now.hour, now.minute) < (target_h, target_m):
        return False
    if last_run is not None:
        # 오늘 이미 실행했으면 다시 안 한다(시간이 지나 있는 동안 매 폴링마다 또 도는 것을 방지).
        if last_run.date() == now.date():
            return False
        # interval_weeks가 1보다 크면, 요일·시간이 맞아도 마지막 실행으로부터 최소
        # 그만큼의 주가 지나기 전까지는 건너뛴다.
        weeks = int(cfg.get("interval_weeks") or 1)
        if weeks > 1 and (now.date() - last_run.date()).days < weeks * 7:
            return False
    return True


def next_run_estimate(cfg: Dict[str, Any], last_run_iso: Optional[str], now: datetime) -> Optional[str]:
    """관리자 화면에 참고용으로만 보여준다 — 실행 여부 판단에는 쓰이지 않는다."""
    if not cfg.get("enabled"):
        return None
    try:
        target_h, target_m = (int(x) for x in str(cfg.get("time") or "00:00").split(":"))
    except ValueError:
        return None
    days = cfg.get("days") or []
    weeks = int(cfg.get("interval_weeks") or 1)

    last_run = None
    if last_run_iso:
        try:
            last_run = datetime.fromisoformat(last_run_iso)
        except ValueError:
            last_run = None
    min_date = last_run.date() + timedelta(days=weeks * 7) if (last_run and weeks > 1) else None

    for offset in range(weeks * 7 + 8):
        candidate_date = now.date() + timedelta(days=offset)
        if WEEKDAY_CODES[candidate_date.weekday()] not in days:
            continue
        if min_date and candidate_date < min_date:
            continue
        candidate = datetime.combine(candidate_date, datetime.min.time()).replace(hour=target_h, minute=target_m)
        if candidate > now:
            return candidate.isoformat(timespec="seconds")
    return None


def run_collection_locked(lock: threading.Lock, triggering_user_id: Optional[str] = None) -> Optional["collector.CollectRun"]:
    """관리자가 직접 누르는 "지금 업데이트"(/api/recollect)와 스케줄러가 공유하는 유일한
    진입점 — collect_all()이 두 곳에서 동시에 실행되는 걸 막는다. 이미 다른 수집이
    진행 중이면 None을 돌려준다.

    triggering_user_id가 있으면 배경 LLM 작업(자금성 판정, KHIDI 마감일 추출, 레시피
    복구)이 그 사용자 본인의 활성 키를 쓴다. 수동 실행이면 지금 로그인한 관리자,
    스케줄러 자동 실행이면 일정을 마지막으로 저장한 관리자(scheduler_loop() 참고)다.
    둘 다 없으면(예: 일정을 아직 아무도 저장한 적 없음) 관리자 계정 중 하나로
    대체된다."""
    if not lock.acquire(blocking=False):
        return None
    try:
        return collector.collect_all(write_db=True, triggering_user_id=triggering_user_id)
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
            # 이 일정을 마지막으로 저장한 관리자의 키를 쓴다(server.py의
            # /api/admin/scheduler-config가 저장 시점에 updated_by를 함께 저장해둠) —
            # 없으면(예: 아직 아무도 저장한 적 없는 기본값) None으로 떨어져
            # run_collection_locked()가 관리자 계정 중 하나로 대체한다.
            run = run_collection_locked(lock, triggering_user_id=cfg.get("updated_by"))
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
