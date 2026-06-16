"""定时推送调度器。

支持 5 种 schedule_type:
- once    一次性，config: {"datetime": "2026-06-01 09:00"}
- daily   每天某时间，config: {"time": "09:00"}
- weekly  每周某几天，config: {"time": "09:00", "weekdays": [1,3,5]}  # 1=Mon..7=Sun
- monthly 每月某日，config: {"time": "09:00", "day": 1}
- cron    高级模式，config: {"cron": "0 9 * * 1-5"}

时区固定 Asia/Shanghai。
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from pytz import timezone

from ..db import SessionLocal, BroadcastSchedule
from . import broadcaster

log = logging.getLogger(__name__)

TZ = timezone("Asia/Shanghai")

_scheduler: Optional[BackgroundScheduler] = None


def _job_key(schedule_id: int) -> str:
    return f"bcast_schedule_{schedule_id}"


def _run_schedule(schedule_id: int):
    """实际触发：发送推送 + 更新 schedule 记录 + 一次性的话禁用。"""
    log.info("running broadcast schedule #%s", schedule_id)
    db = SessionLocal()
    try:
        sch = db.get(BroadcastSchedule, schedule_id)
        if sch is None:
            log.warning("schedule #%s missing, removing job", schedule_id)
            try:
                _scheduler.remove_job(_job_key(schedule_id))
            except Exception:
                pass
            return
        if sch.enabled != "1":
            log.info("schedule #%s disabled, skip", schedule_id)
            return
    finally:
        db.close()

    try:
        result = broadcaster.send_broadcast(sch.broadcast_id)
    except Exception as e:
        log.exception("send_broadcast in scheduler failed")
        result = {"ok": False, "error": str(e)}

    db = SessionLocal()
    try:
        sch = db.get(BroadcastSchedule, schedule_id)
        if sch is None:
            return
        sch.last_run_at = datetime.utcnow()
        try:
            sch.last_result = json.dumps(result, ensure_ascii=False)[:2000]
        except Exception:
            sch.last_result = str(result)[:2000]
        # 一次性触发后自动禁用
        if sch.schedule_type == "once":
            sch.enabled = "0"
            try:
                _scheduler.remove_job(_job_key(schedule_id))
            except Exception:
                pass
        db.commit()
    finally:
        db.close()


def _build_trigger(schedule_type: str, config: dict):
    """根据 schedule_type 构造 APScheduler 触发器。"""
    if schedule_type == "once":
        # config: {"datetime": "2026-06-01 09:00"} 北京时间
        dt_str = config.get("datetime")
        if not dt_str:
            raise ValueError("once 类型必须提供 datetime")
        dt = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
        dt = TZ.localize(dt)
        return DateTrigger(run_date=dt)

    if schedule_type == "daily":
        hh, mm = _parse_hm(config.get("time", "09:00"))
        return CronTrigger(hour=hh, minute=mm, timezone=TZ)

    if schedule_type == "weekly":
        hh, mm = _parse_hm(config.get("time", "09:00"))
        weekdays = config.get("weekdays") or [1, 2, 3, 4, 5]
        # APScheduler day_of_week: 0=mon..6=sun
        days_str = ",".join(str(int(d) - 1) for d in weekdays)
        return CronTrigger(day_of_week=days_str, hour=hh, minute=mm, timezone=TZ)

    if schedule_type == "monthly":
        hh, mm = _parse_hm(config.get("time", "09:00"))
        day = int(config.get("day", 1))
        return CronTrigger(day=day, hour=hh, minute=mm, timezone=TZ)

    if schedule_type == "cron":
        cron = (config.get("cron") or "").strip()
        if not cron:
            raise ValueError("cron 类型必须提供 cron 字段")
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError("cron 表达式必须是 5 字段：分 时 日 月 周")
        return CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4], timezone=TZ,
        )

    raise ValueError(f"unknown schedule_type: {schedule_type}")


def _parse_hm(s: str) -> tuple:
    s = (s or "09:00").strip()
    hh, mm = s.split(":")
    return int(hh), int(mm)


def add_or_update_schedule(schedule_id: int):
    """从 DB 读取计划并注册/更新到 scheduler。"""
    if _scheduler is None:
        return
    db = SessionLocal()
    try:
        sch = db.get(BroadcastSchedule, schedule_id)
        if sch is None:
            return
    finally:
        db.close()

    job_id = _job_key(schedule_id)
    # 先移除旧 job（如果存在）
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass

    if sch.enabled != "1":
        log.info("schedule #%s disabled, not registered", schedule_id)
        return

    try:
        config = json.loads(sch.schedule_config or "{}")
    except Exception:
        config = {}
    try:
        trigger = _build_trigger(sch.schedule_type, config)
    except Exception as e:
        log.exception("build trigger failed for schedule #%s", schedule_id)
        return

    _scheduler.add_job(
        _run_schedule,
        trigger=trigger,
        id=job_id,
        args=[schedule_id],
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,    # 同一计划同一时刻只允许 1 个实例跑，防止重叠
    )

    # 写回 next_run_at 方便 UI 展示
    next_run = _scheduler.get_job(job_id).next_run_time if _scheduler.get_job(job_id) else None
    if next_run:
        db = SessionLocal()
        try:
            sch = db.get(BroadcastSchedule, schedule_id)
            if sch is not None:
                # 转无时区时间存（DB 是 naive UTC）
                sch.next_run_at = next_run.astimezone(TZ).replace(tzinfo=None)
                db.commit()
        finally:
            db.close()
    log.info("schedule #%s registered, next_run=%s", schedule_id, next_run)


def remove_schedule(schedule_id: int):
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_key(schedule_id))
        log.info("schedule #%s removed", schedule_id)
    except Exception:
        pass


def run_in_background(func, *args, job_id: Optional[str] = None) -> bool:
    """把一个函数丢到后台调度器里立即执行一次（一次性 date 触发）。

    复用 broadcast scheduler 的进程生命周期（lifespan 退出时 shutdown(wait=True)
    会给在跑的任务收尾机会）。用于私信大批量异步推送，避免阻塞 UI 请求。
    返回是否成功排入。scheduler 未启动时返回 False（调用方需回退同步执行）。
    """
    if _scheduler is None:
        log.warning("scheduler 未启动，run_in_background 无法排入")
        return False
    run_date = datetime.now(TZ)
    _scheduler.add_job(
        func,
        trigger=DateTrigger(run_date=run_date),
        args=list(args),
        id=job_id,
        replace_existing=bool(job_id),
        misfire_grace_time=3600,   # 进程繁忙时容忍延迟
        coalesce=True,
        max_instances=5,           # 允许多个不同推送任务并行
    )
    log.info("background job 已排入: func=%s args=%s", getattr(func, "__name__", func), args)
    return True


def start():
    """启动调度器并 load 所有 enabled 的计划。"""
    global _scheduler
    if _scheduler is not None:
        log.warning("scheduler already started")
        return
    _scheduler = BackgroundScheduler(timezone=TZ)
    _scheduler.start()

    # 加载所有 enabled 的计划
    db = SessionLocal()
    try:
        rows = db.query(BroadcastSchedule).filter(BroadcastSchedule.enabled == "1").all()
        ids = [r.id for r in rows]
    finally:
        db.close()
    for sid in ids:
        try:
            add_or_update_schedule(sid)
        except Exception as e:
            log.exception("load schedule %s failed: %s", sid, e)
    log.info("broadcast scheduler started, loaded %d schedules", len(ids))

    # 每日运营简报（主动推送，借鉴 Hermes Cron）。开关在 settings.daily_brief_enabled。
    # 无论开关状态都挂 job，job 内部再判断开关（这样后台改开关无需重启即可下次生效）。
    from ..config import settings
    from . import daily_brief
    try:
        cron_expr = (getattr(settings, "daily_brief_cron", "") or "0 9 * * *").strip()
        m, h, dom, mon, dow = cron_expr.split()
        _scheduler.add_job(
            daily_brief.send_daily_brief,
            CronTrigger(minute=m, hour=h, day=dom, month=mon, day_of_week=dow, timezone=TZ),
            id="daily_brief", replace_existing=True,
        )
        log.info("每日简报 job 已挂载: cron=%s (enabled=%s)", cron_expr,
                 getattr(settings, "daily_brief_enabled", False))
    except Exception as e:
        log.exception("挂载每日简报 job 失败: %s", e)

    # 失败回流分析（语义诊断未命中→建议补tag/新增QA）。开关 failure_replay_enabled。
    # 依赖 ollama+向量索引，用子进程跑脚本（隔离特殊依赖，不污染主进程）。
    try:
        fr_cron = (getattr(settings, "failure_replay_cron", "") or "0 3 * * 1").strip()
        m, h, dom, mon, dow = fr_cron.split()
        _scheduler.add_job(
            _run_failure_replay,
            CronTrigger(minute=m, hour=h, day=dom, month=mon, day_of_week=dow, timezone=TZ),
            id="failure_replay", replace_existing=True,
        )
        log.info("失败回流 job 已挂载: cron=%s (enabled=%s)", fr_cron,
                 getattr(settings, "failure_replay_enabled", False))
    except Exception as e:
        log.exception("挂载失败回流 job 失败: %s", e)


def _run_failure_replay():
    """周期跑 failure_replay.py 生成失败回流报告（子进程 + ollama 环境变量）。"""
    from ..config import settings
    if not getattr(settings, "failure_replay_enabled", False):
        log.info("failure_replay_enabled=False，跳过失败回流")
        return
    import subprocess
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent.parent
    script = backend.parent / "scripts" / "failure_replay.py"
    env = dict(os.environ)
    env.update({
        "EMB_BACKEND": "ollama", "EMB_MODEL": "bge-m3:latest",
        "QABOT_ALLOW_WEAK_JWT": "1", "QABOT_SKIP_CHARCHECK": "1",
    })
    try:
        r = subprocess.run(
            [str(backend / ".venv" / "bin" / "python"), "-u", str(script), "--limit", "30"],
            cwd=str(backend), env=env, capture_output=True, text=True, timeout=600,
        )
        log.info("失败回流跑完 rc=%s: %s", r.returncode, (r.stdout or "")[-200:])
    except Exception as e:
        log.exception("失败回流子进程失败: %s", e)


def stop():
    global _scheduler
    if _scheduler is not None:
        # wait=True 让在跑的任务有机会完成；FastAPI lifespan 退出有 ~30s 余地
        # 注意：APScheduler 不支持超时参数，所以可能会被进程强杀
        try:
            _scheduler.shutdown(wait=True)
        except Exception as e:
            log.warning("scheduler shutdown(wait=True) failed: %s, fallback to wait=False", e)
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
        _scheduler = None
        log.info("broadcast scheduler stopped")
