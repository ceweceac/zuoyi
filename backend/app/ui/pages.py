"""管理后台 NiceGUI 页面入口。"""
from nicegui import ui, app, run
from datetime import datetime
from typing import Optional
import io
import html as _html

from ..db import SessionLocal, SysUser, QaItem, Conversation, verify_password, hash_password
from ..security import issue
from ..services.qa_store import store
from . import auth_state as A
from sqlalchemy import or_, func
import openpyxl
import httpx

from ..config import settings as cfg
from ..services import runtime_settings as rts
from ..services import llm as llm_svc
from ..services import ingest as ingest_svc


def _csv_safe(value) -> str:
    """CSV 公式注入防护：单元格以 = + - @ 或 tab/CR 开头时，Excel/WPS 会当公式执行。
    前置一个单引号让其被当作纯文本。用户问题/回复等可控字段导出前都要过一遍。
    """
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


_QUASAR_ZH_HEAD = """
<script src="https://cdn.jsdelivr.net/npm/quasar@2.16.0/lang/zh-CN.umd.prod.js"></script>
<script>
  document.documentElement.lang = 'zh-CN';
  function _applyZhCN(){
    try { if (window.Quasar && window.Quasar.lang && window.Quasar.lang.zhCN) {
      window.Quasar.lang.set(window.Quasar.lang.zhCN); return true;
    }} catch(e){}
    return false;
  }
  if (!_applyZhCN()) {
    let n = 0;
    const t = setInterval(()=>{ if (_applyZhCN() || ++n > 50) clearInterval(t); }, 100);
  }
</script>
<style>
/* ===== 全局主色：紫蓝渐变系 ===== */
*, *::before, *::after { box-sizing: border-box; }
body {
  background: #f5f3ff !important;
  font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif !important;
  color: #1f2937; font-size: 14px;
}

/* ===== 侧边栏 ===== */
.q-drawer {
  background: linear-gradient(180deg, #1e1b4b 0%, #312e81 100%) !important;
  width: 210px !important; min-width: 210px !important;
  border-right: none !important;
  box-shadow: 2px 0 16px rgba(99,102,241,0.25) !important;
}
.q-drawer .q-btn {
  border-radius: 8px !important; margin: 2px 10px !important;
  width: calc(100% - 20px) !important; justify-content: flex-start !important;
  padding: 9px 14px !important; font-size: 13.5px !important; font-weight: 500 !important;
  color: rgba(255,255,255,0.5) !important; transition: all 0.15s ease !important;
  min-height: unset !important; letter-spacing: 0 !important;
}
.q-drawer .q-btn:hover { background: rgba(255,255,255,0.09) !important; color: rgba(255,255,255,0.95) !important; transform: translateX(2px) !important; }
.q-drawer .q-btn.bg-blue-600 { background: linear-gradient(135deg,#6366f1,#8b5cf6) !important; color:#fff !important; box-shadow:0 3px 12px rgba(99,102,241,0.45) !important; }
.q-drawer .q-btn .q-icon { font-size: 17px !important; margin-right: 9px !important; opacity: 0.8; }
.q-drawer .q-btn__content { gap: 0 !important; }

/* ===== 顶部 Header ===== */
.q-header {
  background: #fff !important; border-bottom: 1px solid #ede9fe !important;
  box-shadow: 0 1px 6px rgba(99,102,241,0.08) !important;
  min-height: 56px !important; padding: 0 24px !important;
}
.q-header .nicegui-label { font-size: 15px !important; font-weight: 600 !important; color: #1e1b4b !important; }
.q-header .q-btn { color: #a5b4fc !important; transition: color 0.15s !important; }
.q-header .q-btn:hover { color: #6366f1 !important; }

/* ===== 内容区 ===== */
.q-page { padding: 24px 28px !important; }

/* ===== 统计卡片顶色条 ===== */
.stat-card-blue   { border-top: 3px solid #6366f1 !important; }
.stat-card-green  { border-top: 3px solid #10b981 !important; }
.stat-card-orange { border-top: 3px solid #f59e0b !important; }
.stat-card-purple { border-top: 3px solid #8b5cf6 !important; }

/* ===== 通用卡片 ===== */
.q-card {
  border-radius: 12px !important; border: 1px solid #ede9fe !important;
  box-shadow: 0 1px 4px rgba(99,102,241,0.06) !important; background: #fff !important;
  transition: box-shadow 0.2s ease, transform 0.2s ease !important;
}
.q-card:hover { box-shadow: 0 6px 20px rgba(99,102,241,0.13) !important; transform: translateY(-1px) !important; }

/* ===== 按钮 ===== */
.q-btn:not(.q-drawer .q-btn) { border-radius: 8px !important; font-size: 13px !important; font-weight: 500 !important; letter-spacing: 0 !important; transition: all 0.15s ease !important; }
.q-btn--standard:not(.q-drawer .q-btn):hover { filter: brightness(0.92); transform: translateY(-1px); }

/* ===== 上传组件统一成虚线框风格 ===== */
.q-uploader { background: #faf5ff !important; border: 1.5px dashed #c4b5fd !important; border-radius: 8px !important; box-shadow: none !important; min-height: unset !important; }
.q-uploader:hover { border-color: #8b5cf6 !important; background: #f5f3ff !important; }
.q-uploader__header { background: transparent !important; padding: 6px 12px !important; min-height: unset !important; }
.q-uploader__header-content { color: #7c3aed !important; font-size: 13px !important; font-weight: 500 !important; }
.q-uploader__list { display: none !important; }
.q-uploader__dnd { border-radius: 8px !important; }

/* ===== 输入框 ===== */
.q-field__control { border-radius: 8px !important; }
.q-field--outlined .q-field__control { border-color: #ddd6fe !important; transition: all 0.15s !important; }
.q-field--outlined .q-field__control:hover { border-color: #a78bfa !important; }
.q-field--outlined.q-field--focused .q-field__control { border-color: #6366f1 !important; box-shadow: 0 0 0 3px rgba(99,102,241,0.12) !important; }
.q-field__label { font-size: 13px !important; color: #9ca3af !important; }
.q-field__native { font-size: 13.5px !important; color: #1e1b4b !important; }

/* ===== 表格 ===== */
.q-table { border-radius: 12px !important; border: 1px solid #ede9fe !important; box-shadow: 0 1px 4px rgba(99,102,241,0.06) !important; overflow: hidden; }
.q-table thead tr { background: #faf5ff !important; }
.q-table thead tr th { font-size: 12px !important; font-weight: 600 !important; color: #7c3aed !important; text-transform: uppercase; letter-spacing: 0.5px; padding: 11px 16px !important; border-bottom: 1px solid #ede9fe !important; white-space: nowrap; }
.q-table tbody tr { transition: background 0.1s ease !important; }
.q-table tbody tr td { font-size: 13.5px !important; color: #374151 !important; padding: 11px 16px !important; border-bottom: 1px solid #f5f3ff !important; }
.q-table tbody tr:hover td { background: #f5f3ff !important; cursor: pointer; }
.q-table tbody tr:last-child td { border-bottom: none !important; }
.q-table__bottom { border-top: 1px solid #f5f3ff !important; font-size: 13px !important; color: #9ca3af !important; padding: 8px 16px !important; }

/* ===== 通知 ===== */
.q-notification { border-radius: 10px !important; font-size: 13.5px !important; box-shadow: 0 8px 24px rgba(0,0,0,0.15) !important; }

/* ===== 对话框 ===== */
.q-dialog .q-card { border-radius: 16px !important; }

/* ===== 进度条 ===== */
.q-linear-progress { border-radius: 99px !important; height: 6px !important; }

/* ===== 下拉 ===== */
.q-menu { border-radius: 10px !important; border: 1px solid #ede9fe !important; box-shadow: 0 8px 24px rgba(99,102,241,0.12) !important; }
.q-item { font-size: 13.5px !important; border-radius: 6px !important; margin: 2px 4px !important; }
.q-item:hover { background: #f5f3ff !important; }

/* ===== Tab ===== */
.q-tab { font-size: 13.5px !important; font-weight: 500 !important; letter-spacing: 0 !important; }
.q-tab--active { color: #6366f1 !important; }
.q-tab__indicator { background: #6366f1 !important; height: 2px !important; }

/* ===== 分割线 ===== */
.q-separator { background: #f5f3ff !important; }

/* ===== 页面淡入 ===== */
.q-page { animation: pageFadeIn 0.22s ease; }
@keyframes pageFadeIn { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }
</style>
"""


def _inject_zh():
    """每个页面开头调用，注入 Quasar 中文语言包。"""
    ui.add_head_html(_QUASAR_ZH_HEAD)


# 表格中文化 props（每个表格都加一遍，覆盖 Quasar 默认英文）
TABLE_ZH_PROPS = (
    'dense '                  # 紧凑模式，行高减半
    'wrap-cells '             # 长内容自动换行到第二行，避免横向超长省略
    ':pagination-label="(start, end, total) => `第 ${start}-${end} 条，共 ${total} 条`" '
    'rows-per-page-label="每页显示" '
    ':rows-per-page-options="[10, 20, 50, 100]" '
    'no-data-label="暂无数据" '
    'no-results-label="没有匹配的结果" '
    'loading-label="加载中..."'
)


# ------------- 通用：守卫 + 布局 -------------
def _require_login() -> bool:
    if not A.is_logged_in():
        ui.navigate.to("/login")
        return False
    return True


def _layout(active: str):
    _PAGE_MAP = {
        "/": "概览", "/qa": "知识问答管理", "/conversations": "对话记录",
        "/unmatched": "未命中问题", "/broadcast": "群发推送",
        "/users": "用户管理", "/settings": "系统设置",
        "/failure-reports": "失败分析报告",
    }
    with ui.left_drawer(value=True, fixed=True).classes("bg-slate-900 text-white"):
        ui.html('''
          <div style="padding:18px 16px 14px;border-bottom:1px solid rgba(255,255,255,0.07);margin-bottom:6px">
            <div style="font-size:15px;font-weight:700;color:#fff;letter-spacing:-0.3px">🤖 QA Bot</div>
            <div style="font-size:11px;color:rgba(255,255,255,0.3);margin-top:2px">钉钉智能客服管理</div>
          </div>
        ''')
        items = [
            ("/", "概览", "dashboard"),
            ("/qa", "知识问答管理", "menu_book"),
            ("/conversations", "对话记录", "chat"),
            ("/unmatched", "未命中问题", "report"),
            ("/failure-reports", "失败分析报告", "analytics"),
            ("/broadcast", "群发推送", "campaign"),
            ("/users", "用户管理", "people"),
            ("/settings", "系统设置", "settings"),
        ]
        if A.role() != "admin":
            items = [it for it in items if it[0] not in ("/broadcast", "/failure-reports")]
        for path, name, icon in items:
            classes = "w-full justify-start px-4 py-2 text-white"
            if path == active:
                classes += " bg-blue-600"
            ui.button(name, icon=icon, on_click=lambda p=path: ui.navigate.to(p)).props("flat").classes(classes)

    page_name = _PAGE_MAP.get(active, "")
    with ui.header().classes("bg-white shadow-sm justify-between items-center").style("min-height:54px;padding:0 24px"):
        with ui.row().classes("items-center gap-2"):
            ui.html('<span style="font-size:13px;color:#9ca3af">控制台</span>')
            ui.html('<span style="font-size:13px;color:#d1d5db;margin:0 2px">/</span>')
            ui.html(f'<span style="font-size:13px;font-weight:600;color:#1e1b4b">{page_name}</span>')
        with ui.row().classes("items-center gap-3"):
            u = A.user()
            role_map = {"admin": "管理员", "auditor": "审核员", "editor": "运营", "viewer": "只读"}
            role_label = role_map.get(u.get("role", ""), u.get("role", ""))
            ui.html(f'''
              <div style="display:flex;align-items:center;gap:8px">
                <div style="width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:600">{_html.escape(str(u.get("displayName","?"))[:1])}</div>
                <div>
                  <div style="font-size:13px;font-weight:500;color:#1e1b4b">{_html.escape(str(u.get("displayName","")))}</div>
                  <div style="font-size:11px;color:#9ca3af">{role_label}</div>
                </div>
              </div>
            ''')
            ui.button(icon="logout", on_click=lambda: (A.logout(), ui.navigate.to("/login"))).props("flat round").style("color:#9ca3af")


# ------------- 登录 -------------
@ui.page("/login")
def page_login():
    _inject_zh()
    ui.add_head_html("""
<style>
body { margin:0; overflow:hidden; }
/* 浅色渐变网格背景 —— 与主站风格一致 */
#login-bg {
  position:fixed; inset:0; z-index:0;
  background: linear-gradient(135deg, #eef2ff 0%, #f5f3ff 40%, #ede9fe 70%, #e0e7ff 100%);
}
/* 动态彩色光斑 */
.bg-blob {
  position:fixed; border-radius:50%; filter:blur(80px); pointer-events:none; z-index:1; opacity:0.55;
  animation: blobDrift ease-in-out infinite alternate;
}
@keyframes blobDrift {
  from { transform: translate(0,0) scale(1); }
  to   { transform: translate(30px,20px) scale(1.08); }
}
/* 鼠标光晕 */
#cursor-glow {
  position:fixed; width:500px; height:500px; border-radius:50%;
  pointer-events:none; z-index:2;
  transform:translate(-50%,-50%);
  background: radial-gradient(circle, rgba(99,102,241,0.12) 0%, rgba(139,92,246,0.07) 40%, transparent 70%);
  transition: left 0.06s linear, top 0.06s linear;
}
/* 登录卡片 */
#login-card {
  position:fixed; top:50%; left:50%;
  transform:translate(-50%,-50%);
  z-index:10; width:420px;
  background: rgba(255,255,255,0.85);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid rgba(255,255,255,0.9);
  border-radius: 20px;
  padding: 44px 40px 36px;
  box-shadow: 0 8px 32px rgba(99,102,241,0.12), 0 2px 8px rgba(0,0,0,0.06);
}
#login-card .q-card { background:transparent !important; border:none !important; box-shadow:none !important; padding:0 !important; }
.login-logo { font-size:40px; text-align:center; margin-bottom:10px; }
.login-title { font-size:22px; font-weight:700; color:#1e1b4b; text-align:center; letter-spacing:-0.3px; }
.login-sub { font-size:13px; color:#9ca3af; text-align:center; margin-top:4px; margin-bottom:28px; }
/* 输入框 */
#login-card .q-field__control { background:#fff !important; border-color:#e0e7ff !important; border-radius:10px !important; }
#login-card .q-field__control:hover { border-color:#a5b4fc !important; }
#login-card .q-field--focused .q-field__control { border-color:#6366f1 !important; box-shadow:0 0 0 3px rgba(99,102,241,0.15) !important; }
#login-card .q-field__label { color:#9ca3af !important; font-size:13px !important; }
#login-card .q-field__native { color:#1e1b4b !important; font-size:14px !important; }
/* 登录按钮 */
#login-card .q-btn {
  background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%) !important;
  color:#fff !important; border:none !important;
  height:46px !important; font-size:15px !important; font-weight:600 !important;
  border-radius:10px !important;
  box-shadow: 0 4px 14px rgba(99,102,241,0.4) !important;
  transition: all 0.2s ease !important; letter-spacing:0.5px !important;
}
#login-card .q-btn:hover { box-shadow:0 8px 24px rgba(99,102,241,0.55) !important; transform:translateY(-1px) !important; }
.login-hint { font-size:12px; color:#c4b5fd; text-align:center; margin-top:16px; }
</style>
<script>
document.addEventListener('DOMContentLoaded', function(){
  var glow = document.getElementById('cursor-glow');
  document.addEventListener('mousemove', function(e){
    if(glow){ glow.style.left=e.clientX+'px'; glow.style.top=e.clientY+'px'; }
  });
});
</script>
""")
    # 背景光斑
    ui.html('<div id="login-bg"></div>')
    ui.html('<div class="bg-blob" style="width:500px;height:500px;background:#a5b4fc;top:-100px;left:-100px;animation-duration:8s"></div>')
    ui.html('<div class="bg-blob" style="width:400px;height:400px;background:#c4b5fd;bottom:-80px;right:-60px;animation-duration:10s;animation-delay:-3s"></div>')
    ui.html('<div class="bg-blob" style="width:300px;height:300px;background:#93c5fd;top:40%;left:60%;animation-duration:12s;animation-delay:-5s"></div>')
    ui.html('<div id="cursor-glow"></div>')

    with ui.element("div").props('id="login-card"'):
        ui.html('<div class="login-logo">🤖</div>')
        ui.html('<div class="login-title">QA Bot 管理后台</div>')
        ui.html('<div class="login-sub">钉钉智能客服 · Admin Console</div>')

        username = ui.input("用户名", placeholder="请输入登录账号").classes("w-full").props("outlined dense")
        ui.element("div").style("height:10px")
        password = ui.input("密码", password=True, password_toggle_button=True, placeholder="请输入密码").classes("w-full").props("outlined dense")

        def do_login():
            db = SessionLocal()
            try:
                u = db.query(SysUser).filter(
                    SysUser.username == username.value,
                    SysUser.enabled == "1",
                ).first()
            finally:
                db.close()
            if not u or not verify_password(password.value, u.password):
                ui.notify("用户名或密码错误", type="negative")
                return
            A.login(issue(u.username, u.role), {
                "username": u.username, "displayName": u.display_name, "role": u.role,
            })
            ui.notify(f"欢迎回来，{u.display_name} 👋", type="positive")
            ui.navigate.to("/")

        ui.element("div").style("height:16px")
        ui.button("登 录", on_click=do_login).props("color=primary").classes("w-full")
        ui.html('<div class="login-hint">忘记密码请联系系统管理员重置</div>')


# ------------- 概览 -------------
def _token_stats():
    """LLM token 消耗统计：总消耗 + 今日 + 近 7 天每日明细。
    数据来源 conversation 表的 llm_*_tokens 字段（每次调用都已记录）。
    """
    from datetime import date
    db = SessionLocal()
    try:
        # 总消耗
        tot = db.query(
            func.coalesce(func.sum(Conversation.llm_prompt_tokens), 0),
            func.coalesce(func.sum(Conversation.llm_completion_tokens), 0),
            func.coalesce(func.sum(Conversation.llm_total_tokens), 0),
            func.count(Conversation.id),
        ).filter(Conversation.llm_total_tokens > 0).first()
        total_prompt, total_comp, total_all, total_calls = tot

        # 今日消耗（按本地自然日，created_at 存的是 UTC，近似用日期截断）
        today = date.today()
        today_start = datetime(today.year, today.month, today.day)
        td = db.query(
            func.coalesce(func.sum(Conversation.llm_total_tokens), 0),
            func.count(Conversation.id),
        ).filter(Conversation.llm_total_tokens > 0,
                 Conversation.created_at >= today_start).first()
        today_tokens, today_calls = td

        # 每日明细（保留全部历史，新的在前；UI 表格自带分页）
        rows = db.query(
            func.date(Conversation.created_at).label("d"),
            func.coalesce(func.sum(Conversation.llm_prompt_tokens), 0),
            func.coalesce(func.sum(Conversation.llm_completion_tokens), 0),
            func.coalesce(func.sum(Conversation.llm_total_tokens), 0),
            func.count(Conversation.id),
        ).filter(Conversation.llm_total_tokens > 0)\
         .group_by(func.date(Conversation.created_at))\
         .order_by(func.date(Conversation.created_at).desc()).all()
    finally:
        db.close()

    def _fmt(n):
        n = int(n or 0)
        if n >= 1_000_000:
            return f"{n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)

    avg_call = int(total_all / total_calls) if total_calls else 0
    cards = [
        ("累计 token 总消耗", _fmt(total_all), "🔢", "#6366f1", "#eef2ff", f"{total_calls} 次调用"),
        ("今日消耗", _fmt(today_tokens), "📅", "#10b981", "#ecfdf5", f"今日 {today_calls} 次"),
        ("累计输入 / 输出", f"{_fmt(total_prompt)}/{_fmt(total_comp)}", "↕️", "#f59e0b", "#fffbeb", "input / output"),
        ("平均每次消耗", _fmt(avg_call), "📈", "#8b5cf6", "#f5f3ff", "total/调用次数"),
    ]
    with ui.row().classes("w-full gap-4 mt-4"):
        for label, value, icon, color, bg, sub in cards:
            with ui.card().classes("flex-1").style("padding:20px 22px"):
                with ui.row().classes("items-start justify-between w-full"):
                    with ui.column().style("gap:2px"):
                        ui.html(f'<div style="font-size:13px;color:#9ca3af;font-weight:500;margin-bottom:8px">{label}</div>')
                        ui.html(f'<div style="font-size:28px;font-weight:800;color:#111827;line-height:1.1">{value}</div>')
                        ui.html(f'<div style="font-size:12px;color:{color};margin-top:6px;font-weight:500">{sub}</div>')
                    ui.html(f'<div style="width:44px;height:44px;border-radius:10px;background:{bg};display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0">{icon}</div>')

    # 每日明细表（保留全部历史，分页展示）
    # 同时保留原始整数值（raw_*）供 CSV 导出，显示用 _fmt 的 K/M 简写
    daily = [{
        "date": str(r[0]),
        "prompt": _fmt(r[1]),
        "comp": _fmt(r[2]),
        "total": _fmt(r[3]),
        "calls": int(r[4] or 0),
        "raw_prompt": int(r[1] or 0),
        "raw_comp": int(r[2] or 0),
        "raw_total": int(r[3] or 0),
    } for r in rows]

    def _export_daily_csv():
        import csv, io as iio
        buf = iio.StringIO()
        w = csv.writer(buf)
        w.writerow(["日期", "总消耗tokens", "输入tokens", "输出tokens", "调用次数"])
        for d in daily:
            w.writerow([d["date"], d["raw_total"], d["raw_prompt"],
                        d["raw_comp"], d["calls"]])
        # 末尾追加累计合计行
        w.writerow([])
        w.writerow(["累计合计", int(total_all or 0), int(total_prompt or 0),
                    int(total_comp or 0), int(total_calls or 0)])
        ui.download(buf.getvalue().encode("utf-8-sig"),
                    f"token消耗明细-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv")

    with ui.card().classes("w-full mt-4").style("padding:16px 20px"):
        with ui.row().classes("w-full justify-between items-center").style("margin-bottom:10px"):
            ui.html(f'<div style="font-size:14px;font-weight:600;color:#111827">每日 token 消耗明细（共 {len(daily)} 天，全部保留）</div>')
            ui.button("导出 CSV", icon="download",
                      on_click=_export_daily_csv).props("outline color=primary size=sm")
        if daily:
            ui.table(
                columns=[
                    {"name": "date", "label": "日期", "field": "date", "align": "left"},
                    {"name": "total", "label": "总消耗", "field": "total"},
                    {"name": "prompt", "label": "输入", "field": "prompt"},
                    {"name": "comp", "label": "输出", "field": "comp"},
                    {"name": "calls", "label": "调用次数", "field": "calls"},
                ],
                rows=daily, row_key="date", pagination=15,
            ).props('dense flat').classes("w-full")
        else:
            ui.html('<div style="color:#9ca3af;font-size:13px">暂无 LLM 调用记录</div>')


@ui.page("/")
def page_dashboard():
    if not _require_login():
        return
    _inject_zh()
    _layout("/")

    @ui.refreshable
    def stats():
        db = SessionLocal()
        try:
            total = db.query(Conversation).count()
            escalated = db.query(Conversation).filter(Conversation.escalated == "1").count()
            # A = 命中 KB 标准答案，B = LLM 兜底/保护话术（均算"机器人解决"）。
            kb_hit = db.query(Conversation).filter(Conversation.answer_level == "A").count()
            bot_solved = db.query(Conversation).filter(Conversation.answer_level.in_(("A", "B"))).count()
        finally:
            db.close()
        # 命中率=知识库覆盖，只数 A 级（真正命中 KB）；机器人解决率=A+B。
        hit_rate = f"{(kb_hit / total * 100):.1f}%" if total else "—"
        bot_rate = f"{(bot_solved / total * 100):.1f}%" if total else "—"
        cards = [
            ("总对话数",   str(total),       "💬", "#6366f1", "#eef2ff", "全部对话"),
            ("机器人解决", str(bot_solved),  "🤖", "#10b981", "#ecfdf5", f"解决率 {bot_rate}"),
            ("转人工",     str(escalated),   "👤", "#f59e0b", "#fffbeb", f"占比 {f'{escalated/total*100:.1f}' if total else 0}%"),
            ("命中率",     hit_rate,         "📊", "#8b5cf6", "#f5f3ff", f"知识库覆盖（{kb_hit} 条 A 级）"),
        ]
        with ui.row().classes("w-full gap-4 mt-4"):
            for label, value, icon, color, bg, sub in cards:
                with ui.card().classes("flex-1").style("padding:20px 22px"):
                    with ui.row().classes("items-start justify-between w-full"):
                        with ui.column().style("gap:2px"):
                            ui.html(f'<div style="font-size:13px;color:#9ca3af;font-weight:500;margin-bottom:8px">{label}</div>')
                            ui.html(f'<div style="font-size:32px;font-weight:800;color:#111827;line-height:1.1">{value}</div>')
                            ui.html(f'<div style="font-size:12px;color:{color};margin-top:6px;font-weight:500">{sub}</div>')
                        ui.html(f'<div style="width:44px;height:44px;border-radius:10px;background:{bg};display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0">{icon}</div>')
        _token_stats()
        with ui.card().classes("w-full mt-5").style("padding: 20px 24px"):
            ui.html('<div style="font-size:15px;font-weight:600;color:#111827;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #f0f2f5">系统状态</div>')
            with ui.row().classes("gap-8 items-center"):
                ui.html(f'<div style="display:flex;align-items:center;gap:8px;font-size:14px;color:#374151">📚 <span>知识库版本</span> <b style="color:#111827">第 {store.version} 版</b></div>')
                ui.html('<div style="display:flex;align-items:center;gap:8px;font-size:14px;color:#374151"><span style="width:8px;height:8px;border-radius:50%;background:#10b981;display:inline-block"></span> <span>服务状态</span> <b style="color:#10b981">运行中</b></div>')

            def health_check():
                import re
                bad = re.compile("�")
                issues = []
                db2 = SessionLocal()
                try:
                    from ..db import SysSetting
                    for r in db2.query(SysSetting).all():
                        v = r.v or ""
                        if r.k in {"llm_api_key", "dingtalk_client_secret"}:
                            continue
                        if bad.search(v):
                            issues.append(f"系统设置「{r.k}」")
                    for q in db2.query(QaItem).filter(QaItem.deleted=="0").all():
                        if bad.search(q.question or "") or bad.search(q.answer or ""):
                            issues.append(f"知识库条目 #{q.id} {(q.question or '')[:20]}")
                    for c in db2.query(Conversation).order_by(Conversation.id.desc()).limit(100).all():
                        if bad.search(c.answer or "") or bad.search(c.question or ""):
                            issues.append(f"对话记录 #{c.id}")
                finally:
                    db2.close()
                if issues:
                    ui.notify(f"发现 {len(issues)} 处字符损坏：\n" + "\n".join(issues[:5]),
                              type="negative", multi_line=True)
                else:
                    ui.notify("✅ 所有文本字符健康", type="positive")

            with ui.row().classes("mt-3 gap-2"):
                ui.button("刷新统计", icon="refresh", on_click=stats.refresh)
                ui.button("字符健康检查", icon="health_and_safety", on_click=health_check)

    stats()


# ------------- QA 管理 -------------
@ui.page("/qa")
def page_qa():
    if not _require_login():
        return
    _inject_zh()
    _layout("/qa")

    state = {"keyword": "", "status": "", "page": 1, "size": 20}
    table = None

    def load():
        nonlocal table
        db = SessionLocal()
        try:
            q = db.query(QaItem).filter(QaItem.deleted == "0")
            if state["keyword"]:
                kw = f"%{state['keyword']}%"
                q = q.filter(or_(QaItem.question.like(kw), QaItem.answer.like(kw)))
            if state["status"]:
                q = q.filter(QaItem.status == state["status"])
            rows = q.order_by(QaItem.updated_at.desc()).all()
            data = []
            for idx, r in enumerate(rows, 1):
                if r.status == "pending":
                    status_label = "⏳ 待审核"
                elif r.status == "approved":
                    status_label = "✅ 已通过"
                elif r.status == "disabled":
                    status_label = "🚫 已禁用"
                else:
                    status_label = r.status
                data.append({
                    "seq": idx,            # 行序号（1,2,3... 跟着删除/增加变化）
                    "id": r.id,            # 真实 DB 主键，操作按钮要用，不显示
                    "question": r.question, "answer": (r.answer or "")[:80],
                    "category": r.category or "",
                    "status": status_label,
                    "status_raw": r.status,
                    "enabled": "是" if r.enabled == "1" else "否",
                })
        finally:
            db.close()
        table.rows = data
        table.update()
        # 同步刷新顶部统计栏（数字和"一键通过"按钮）
        try:
            stats_row.refresh()
        except Exception:
            pass

    # ── 工具栏第一行：搜索 + 主操作 ──
    with ui.row().classes("w-full mt-4 gap-2 items-center flex-wrap"):
        kw = ui.input("搜索问题或答案").bind_value(state, "keyword").classes("w-64").props("outlined dense")
        st = ui.select({"": "全部", "pending": "待审核", "approved": "已通过"}, value="").bind_value(state, "status").classes("w-28").props("outlined dense")
        ui.button("查询", icon="search", on_click=load).props("color=primary unelevated")
        if A.can_edit_qa():
            ui.button("新增", icon="add", on_click=lambda: open_edit(None)).props("unelevated").style("background:#6366f1;color:#fff")
        if A.can_edit_qa():
            ui.html('<div style="width:1px;height:28px;background:#e5e7eb;margin:0 4px"></div>')
            ui.upload(on_upload=lambda e: do_import(e), auto_upload=True, max_files=1)\
                .props('accept=".xlsx" label="导入表格" dense outlined').classes("w-36")
            async def _on_smart_upload(e):
                await do_smart_import(e)
            ui.upload(on_upload=_on_smart_upload, auto_upload=True, max_files=1)\
                .props('accept=".txt,.md,.docx,.pdf" label="智能导入文档" dense outlined').classes("w-40")
        ui.html('<div style="width:1px;height:28px;background:#e5e7eb;margin:0 4px"></div>')
        ui.button("刷新知识库", icon="sync", on_click=lambda: (store.reload(), ui.notify(f"已刷新至第 {store.version} 版", type="positive"))).props("outline color=primary")

        def do_dedupe():
            if not A.can_edit_qa():
                ui.notify("无权限：仅 admin / editor 可去重", type="negative"); return
            n = ingest_svc.dedupe_existing()
            ui.notify(f"已清理重复条目 {n} 条", type="positive" if n > 0 else "info")
            load()
        if A.can_edit_qa():
            ui.button("去重", icon="cleaning_services", on_click=do_dedupe).props("outline color=primary")
            ui.html('<div style="width:1px;height:28px;background:#e5e7eb;margin:0 4px"></div>')

        async def batch_delete_selected():
            """删除当前勾选的条目。"""
            if not A.can_edit_qa():
                ui.notify("无权限：仅 admin / editor 可删除知识库条目", type="negative"); return
            sel = table.selected or []
            if not sel:
                ui.notify("请先勾选要删除的条目", type="warning"); return
            ids = [r["id"] for r in sel]
            with ui.dialog() as dlg, ui.card():
                ui.label(f"确认删除选中的 {len(ids)} 条？此操作可在数据库找回，但 UI 中不可恢复。")
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button("取消", on_click=dlg.close).props("outline")
                    confirm_btn = ui.button("确认删除", on_click=lambda: dlg.submit("ok")).props("color=negative unelevated")
            result = await dlg
            if result != "ok":
                return
            db = SessionLocal()
            try:
                n = db.query(QaItem).filter(QaItem.id.in_(ids), QaItem.deleted == "0")\
                    .update({"deleted": "1", "enabled": "0"}, synchronize_session=False)
                db.commit()
            finally:
                db.close()
            store.reload()
            table.selected = []
            ui.notify(f"已删除 {n} 条", type="positive")
            load()

        async def delete_all_filtered():
            """删除当前筛选条件下的全部条目（不限于勾选）。"""
            if not A.can_edit_qa():
                ui.notify("无权限：仅 admin / editor 可删除知识库条目", type="negative"); return
            db = SessionLocal()
            try:
                q = db.query(QaItem).filter(QaItem.deleted == "0")
                if state["keyword"]:
                    kw = f"%{state['keyword']}%"
                    q = q.filter(or_(QaItem.question.like(kw), QaItem.answer.like(kw)))
                if state["status"]:
                    q = q.filter(QaItem.status == state["status"])
                ids = [r.id for r in q.all()]
            finally:
                db.close()
            if not ids:
                ui.notify("当前筛选下没有可删除的条目", type="info"); return
            with ui.dialog() as dlg, ui.card():
                ui.label(f"⚠️ 将删除当前筛选下的全部 {len(ids)} 条").classes("text-lg font-semibold")
                ui.label("（已删除条目仍保留在数据库 deleted='1'，可技术恢复，但 UI 不可见）").classes("text-slate-500 text-sm")
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button("取消", on_click=dlg.close)
                    ui.button("确认全部删除", on_click=lambda: dlg.submit("ok")).props("color=negative")
            result = await dlg
            if result != "ok":
                return
            db = SessionLocal()
            try:
                n = db.query(QaItem).filter(QaItem.id.in_(ids), QaItem.deleted == "0")\
                    .update({"deleted": "1", "enabled": "0"}, synchronize_session=False)
                db.commit()
            finally:
                db.close()
            store.reload()
            table.selected = []
            ui.notify(f"已删除 {n} 条", type="positive")
            load()

        if A.can_edit_qa():
            ui.button("批量删除选中", icon="delete_sweep", on_click=batch_delete_selected).props("outline color=negative")
            ui.button("删除筛选全部", icon="delete_forever", on_click=delete_all_filtered).props("outline color=negative")

        def show_pending():
            """快捷：筛选所有待审核"""
            state["status"] = "pending"
            st.value = "pending"
            load()

        def batch_approve_all_pending():
            """一键审核当前所有待审核条目"""
            if not A.can_approve():
                ui.notify("无审核权限", type="negative"); return
            from datetime import datetime
            db2 = SessionLocal()
            try:
                rows = db2.query(QaItem).filter(QaItem.status=="pending", QaItem.deleted=="0").all()
                if not rows:
                    ui.notify("当前没有待审核的条目", type="info"); return
                for r in rows:
                    r.status = "approved"
                    r.approved_by = A.user()["username"]
                    r.approved_at = datetime.utcnow()
                db2.commit()
                n = len(rows)
            finally:
                db2.close()
            store.reload()
            ui.notify(f"✅ 已批量通过 {n} 条，知识库已刷新", type="positive")
            load()

    # 第二行：实时统计 + 待审核快捷操作
    @ui.refreshable
    def stats_row():
        with ui.row().classes("w-full gap-3 items-center mt-2 pb-1"):
            db2 = SessionLocal()
            try:
                total_alive = db2.query(QaItem).filter(QaItem.deleted=="0").count()
                approved_count = db2.query(QaItem).filter(QaItem.status=="approved", QaItem.deleted=="0").count()
                pending_count = db2.query(QaItem).filter(QaItem.status=="pending", QaItem.deleted=="0").count()
            finally:
                db2.close()
            ui.html(f'<span style="font-size:13px;color:#6b7280">共 <b style="color:#1e1b4b">{total_alive}</b> 条</span>')
            ui.html(f'<span style="font-size:13px;color:#6b7280">已通过 <b style="color:#10b981">{approved_count}</b></span>')
            if pending_count > 0:
                ui.html(f'<span style="display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;color:#d97706;background:#fffbeb;padding:2px 10px;border-radius:20px;border:1px solid #fde68a">⏳ 待审核 {pending_count}</span>')
                ui.button("只看待审核", icon="filter_list", on_click=show_pending).props("outline dense").style("color:#6366f1;border-color:#6366f1;font-size:12px")
                ui.button(f"一键全部通过 ({pending_count})", icon="done_all", on_click=batch_approve_all_pending).props("unelevated dense").style("background:#6366f1;color:#fff;font-size:12px")
            else:
                ui.html('<span style="font-size:12px;color:#9ca3af">全部已审核</span>')

    stats_row()

    table = ui.table(
        columns=[
            {"name": "seq", "label": "序号", "field": "seq", "align": "left"},
            {"name": "question", "label": "问题", "field": "question", "align": "left"},
            {"name": "answer", "label": "答案", "field": "answer", "align": "left"},
            {"name": "category", "label": "分类", "field": "category"},
            {"name": "status", "label": "状态", "field": "status"},
            {"name": "enabled", "label": "启用", "field": "enabled"},
            {"name": "action", "label": "操作", "field": "id"},
        ],
        rows=[],
        row_key="id",
        selection="multiple",       # 表头出现勾选列，可多选/全选
    ).classes("w-full mt-2").props(TABLE_ZH_PROPS)

    table.add_slot("body-cell-action", r"""
        <q-td :props="props">
            <q-btn dense flat label="编辑" color="primary" @click="$parent.$emit('edit', props.row)" style="font-size:12px"/>
            <q-btn v-if="props.row.status_raw==='pending'" dense flat label="通过" color="positive" @click="$parent.$emit('approve', props.row)" style="font-size:12px"/>
            <q-btn v-if="props.row.enabled==='是'" dense flat label="禁用" color="grey" @click="$parent.$emit('disable', props.row)" style="font-size:12px"/>
            <q-btn dense flat label="删除" color="negative" @click="$parent.$emit('remove', props.row)" style="font-size:12px"/>
        </q-td>
    """)
    table.on("edit",    lambda e: open_edit(e.args["id"]))
    table.on("approve", lambda e: do_approve(e.args["id"]))
    table.on("disable", lambda e: do_disable(e.args["id"]))
    table.on("remove",  lambda e: do_delete(e.args["id"]))

    # ---- 操作 ----
    def open_edit(qa_id: Optional[int]):
        with ui.dialog() as dlg, ui.card().classes("w-[640px]"):
            ui.label("编辑 QA" if qa_id else "新增 QA").classes("text-lg font-semibold")
            db = SessionLocal()
            try:
                it = db.get(QaItem, qa_id) if qa_id else None
            finally:
                db.close()
            q_in = ui.input("问题", value=it.question if it else "").classes("w-full")
            a_in = ui.textarea("答案", value=it.answer if it else "").classes("w-full").props("rows=8")
            c_in = ui.input("分类", value=it.category if it else "").classes("w-full")
            t_in = ui.input("标签（逗号分隔）", value=it.tags if it else "").classes("w-full")

            def save():
                # 授权校验：仅 admin/editor 可新增/编辑 KB（与 REST qa.py 的 require_role 一致）。
                # NiceGUI 回调在服务端执行，缺这道校验会让 viewer/auditor 越权改库、
                # 甚至把已审核的线上条目改成 pending 后 reload 下线。
                if not A.can_edit_qa():
                    ui.notify("没有权限：仅管理员/编辑可新增或编辑知识库", type="negative")
                    return
                u = A.user()
                db2 = SessionLocal()
                try:
                    if qa_id:
                        cur = db2.get(QaItem, qa_id)
                        cur.question, cur.answer, cur.category, cur.tags = q_in.value, a_in.value, c_in.value, t_in.value
                        cur.status = "pending"
                        cur.version = (cur.version or 1) + 1
                        cur.updated_by = u["username"]
                    else:
                        db2.add(QaItem(
                            question=q_in.value, answer=a_in.value, category=c_in.value, tags=t_in.value,
                            status="pending", enabled="1", deleted="0", version=1,
                            created_by=u["username"], updated_by=u["username"],
                        ))
                    db2.commit()
                finally:
                    db2.close()
                # 编辑/新增后 KB 缓存需失效重建（与 REST update_qa 一致），
                # 否则编辑过的条目仍按旧 approved 内容应答，直到下次 reload。
                store.reload()
                ui.notify("保存成功，待审核", type="positive")
                dlg.close()
                load()

            with ui.row().classes("w-full justify-end mt-2"):
                ui.button("取消", on_click=dlg.close)
                ui.button("保存", on_click=save).props("color=primary")
        dlg.open()

    def do_approve(qa_id: int):
        if not A.can_approve():
            ui.notify("无审核权限", type="negative")
            return
        db = SessionLocal()
        try:
            it = db.get(QaItem, qa_id)
            it.status = "approved"
            it.approved_by = A.user()["username"]
            it.approved_at = datetime.utcnow()
            db.commit()
        finally:
            db.close()
        store.reload()
        ui.notify("已审核通过，知识库已热更新", type="positive")
        load()

    def do_disable(qa_id: int):
        if not A.can_edit_qa():
            ui.notify("没有权限：仅管理员/编辑可禁用 KB", type="negative")
            return
        db = SessionLocal()
        try:
            it = db.get(QaItem, qa_id)
            it.enabled = "0"; db.commit()
        finally:
            db.close()
        store.reload()
        ui.notify("已禁用", type="positive"); load()

    def do_delete(qa_id: int):
        if not A.can_edit_qa():
            ui.notify("没有权限：仅管理员/编辑可删除 KB", type="negative")
            return
        db = SessionLocal()
        try:
            it = db.get(QaItem, qa_id); it.deleted = "1"; db.commit()
        finally:
            db.close()
        store.reload()
        ui.notify("已删除", type="positive"); load()

    async def do_import(e):
        """同步导入：解析 Excel + 落库。放线程池跑，避免阻塞 NiceGUI WebSocket。"""
        if not A.can_edit_qa():
            ui.notify("无权限：仅 admin / editor 可导入知识库", type="negative"); return
        import asyncio
        u = A.user()
        # e.content.read() 是同步 BytesIO 操作，本身不慢，但放线程池更安全
        content = await asyncio.to_thread(lambda: e.content.read())
        try:
            inserted, skipped, duplicated, headers = await asyncio.to_thread(
                ingest_svc.ingest_excel, content, u["username"]
            )
        except Exception as ex:
            ui.notify(f"导入失败：{ex}", type="negative", multi_line=True)
            return
        head_info = []
        for sheet, col in headers.items():
            head_info.append(f"  · {sheet}: 问题={col['question']} 答案={col['answer']} 分类={col['category']} 标签={col['tags']}")
        msg = f"新增 {inserted} 条（⏳ 待审核） · 跳过空行 {skipped} 条 · 已存在跳过 {duplicated} 条\n请下拉【状态】筛选『待审核』，点【通过】按钮才会生效"
        if head_info:
            msg += "\n识别到的表头列序：\n" + "\n".join(head_info)
        ui.notify(msg, type="positive", multi_line=True)
        load()

    async def do_smart_import(e):
        """智能导入：上传文档 → 大模型抽取问答对 → 入库待审核。
        放线程池跑，避免阻塞事件循环导致 NiceGUI WebSocket 断连。"""
        if not A.can_edit_qa():
            ui.notify("无权限：仅 admin / editor 可导入知识库", type="negative"); return
        u = A.user()
        filename = e.name or "uploaded.bin"
        content = e.content.read()

        if not cfg.llm_enabled or not cfg.llm_api_key:
            ui.notify("请先在「系统设置」启用大模型并填写接口密钥", type="negative", multi_line=True)
            return

        with ui.dialog() as dlg, ui.card().classes("w-[520px]"):
            ui.label(f"正在智能分析：{filename}").classes("text-lg font-semibold")
            status = ui.label("准备中……").classes("text-slate-600 mt-2")
            bar = ui.linear_progress(value=0, show_value=False).classes("mt-2")
            ui.label("文档较大时可能需要几分钟，请耐心等待。期间页面会保持连接。").classes("text-slate-400 text-xs mt-2")
        dlg.open()

        # 共享状态，线程里更新，UI 定时器取
        progress = {"done": 0, "total": 1, "msg": "准备中……", "finished": False, "result": None, "error": None}

        def cb(done, total, msg):
            progress["done"] = done
            progress["total"] = total
            progress["msg"] = msg

        def worker():
            try:
                r = ingest_svc.ingest(filename, content, u["username"], progress_cb=cb)
                progress["result"] = r
            except Exception as ex:
                progress["error"] = str(ex)
            finally:
                progress["finished"] = True

        # UI 定时刷新进度
        def tick():
            try:
                bar.value = progress["done"] / max(progress["total"], 1)
                status.text = progress["msg"]
            except Exception:
                pass
            if progress["finished"]:
                timer.cancel()
                dlg.close()
                if progress["error"]:
                    ui.notify(f"智能导入失败：{progress['error']}", type="negative", multi_line=True)
                    return
                chunks, extracted, inserted, duplicated = progress["result"]
                if chunks == 0:
                    ui.notify("文档为空或无法解析", type="warning"); return
                ui.notify(
                    f"已分析 {chunks} 段，抽取 {extracted} 条问答，新增 {inserted} 条（待审核），已存在跳过 {duplicated} 条",
                    type="positive", multi_line=True)
                load()

        timer = ui.timer(0.5, tick)
        # 真正把 ingest 放到线程池跑，不阻塞事件循环
        await run.io_bound(worker)

    load()


# ------------- 对话记录 -------------
@ui.page("/conversations")
def page_conversations(id: int = 0):
    if not _require_login():
        return
    _inject_zh()
    _layout("/conversations")
    # 只在「对话记录」这一页适度放宽表格
    ui.add_head_html("""
<style>
body[data-page="conversations"] .q-page { padding: 16px 23px !important; }
body[data-page="conversations"] .nicegui-content { align-items: stretch !important; width: 100% !important; }
body[data-page="conversations"] .q-table__container { width: 100% !important; }
body[data-page="conversations"] .q-table__middle { overflow-x: auto !important; }
/* 行/单元格紧凑：减小上下间距，展示更多行 */
body[data-page="conversations"] .q-table thead tr th { padding: 6px 10px !important; font-size: 11.5px !important; }
body[data-page="conversations"] .q-table tbody tr td { padding: 5px 10px !important; font-size: 12.5px !important; line-height: 1.45 !important; }
body[data-page="conversations"] .q-table__bottom { padding: 4px 10px !important; }
</style>
""")
    ui.run_javascript('document.body.setAttribute("data-page","conversations")')

    state = {"sender": "", "level": "", "escalated": "", "keyword": ""}
    table = None
    auto_open_id = id   # URL 里 ?id=123 时自动弹详情

    def load():
        db = SessionLocal()
        try:
            q = db.query(Conversation)
            if state["sender"]:    q = q.filter(Conversation.sender.like(f"%{state['sender']}%"))
            if state["level"]:     q = q.filter(Conversation.answer_level == state["level"])
            if state["escalated"]: q = q.filter(Conversation.escalated == state["escalated"])
            if state["keyword"]:
                kw = f"%{state['keyword']}%"
                q = q.filter((Conversation.question.like(kw)) | (Conversation.answer.like(kw)))
            rows = q.order_by(Conversation.created_at.desc()).limit(200).all()
            data = [
                {
                    "seq": idx,
                    "id": r.id,
                    "time": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                    "sender": (r.sender_name or "").strip() or (r.sender or "(未知)"),
                    "question": r.question or "",
                    "answer": r.answer or "",
                    "level": r.answer_level or "",
                    "escalated": "是" if r.escalated == "1" else "否",
                    "latency": r.llm_latency_ms or 0,
                    "tokens": r.llm_total_tokens or 0,
                    "model": (r.llm_model or "")[:24],
                }
                for idx, r in enumerate(rows, 1)
            ]
        finally:
            db.close()
        table.rows = data
        table.update()

    def show_detail(row_id: int):
        db = SessionLocal()
        try:
            r = db.get(Conversation, row_id)
        finally:
            db.close()
        if not r:
            ui.notify("记录不存在", type="warning"); return

        with ui.dialog() as dlg, ui.card().classes("w-[760px] max-h-[80vh] overflow-auto"):
            ui.label(f"对话详情 #{r.id}").classes("text-xl font-semibold mb-2")

            def kv(k, v, mono=False):
                with ui.row().classes("w-full items-start gap-2 mt-1"):
                    ui.label(k).classes("w-32 text-slate-500 shrink-0")
                    txt = ui.label(str(v) if v not in (None, "") else "—")
                    if mono:
                        txt.classes("font-mono text-xs break-all")
                    else:
                        txt.classes("break-all whitespace-pre-wrap")

            kv("时间", r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "")
            kv("用户", f"{r.sender_name or ''} ({r.sender})")
            kv("原始问题", r.question)
            kv("脱敏后问题", r.question_desensitized)
            kv("最终回复", r.answer)
            ui.separator()
            kv("分级", r.answer_level)
            kv("转人工", "是" if r.escalated == "1" else "否")
            kv("红线词命中", "是" if r.redline_hit == "1" else "否")
            kv("敏感词命中", "是" if r.sensitive_hit == "1" else "否")
            kv("知识库版本", f"第 {r.qa_prompt_version} 版" if r.qa_prompt_version else "—")
            ui.separator()
            ui.label("大模型调用").classes("font-semibold mt-2")
            kv("模型", r.llm_model)
            kv("调用 URL", r.llm_url, mono=True)
            kv("HTTP 状态", r.llm_status)
            kv("耗时", f"{r.llm_latency_ms or 0} 毫秒")
            kv("Token (提示/答案/合计)", f"{r.llm_prompt_tokens or 0} / {r.llm_completion_tokens or 0} / {r.llm_total_tokens or 0}")
            if r.raw_llm_answer and r.raw_llm_answer != r.answer:
                kv("模型原始回复", r.raw_llm_answer)
            if r.error_msg:
                kv("异常信息", r.error_msg)

            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("关闭", on_click=dlg.close)
        dlg.open()

    def export_csv():
        import csv, io as iio
        db = SessionLocal()
        try:
            rows = db.query(Conversation).order_by(Conversation.created_at.desc()).limit(2000).all()
        finally:
            db.close()
        buf = iio.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "用户ID", "用户名", "原始问题", "脱敏问题", "最终回复",
                    "模型回复原文", "分级", "转人工", "红线", "敏感词",
                    "模型", "调用URL", "HTTP状态", "耗时(ms)",
                    "提示tokens", "答案tokens", "合计tokens", "知识库版本", "异常"])
        for r in rows:
            w.writerow([
                r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                _csv_safe(r.sender or ""), _csv_safe(r.sender_name or ""),
                _csv_safe(r.question or ""), _csv_safe(r.question_desensitized or ""),
                _csv_safe(r.answer or ""), _csv_safe(r.raw_llm_answer or ""),
                r.answer_level or "", "是" if r.escalated == "1" else "否",
                "是" if r.redline_hit == "1" else "否",
                "是" if r.sensitive_hit == "1" else "否",
                _csv_safe(r.llm_model or ""), _csv_safe(r.llm_url or ""), r.llm_status or "",
                r.llm_latency_ms or 0, r.llm_prompt_tokens or 0,
                r.llm_completion_tokens or 0, r.llm_total_tokens or 0,
                r.qa_prompt_version or 0, _csv_safe(r.error_msg or ""),
            ])
        ui.download(buf.getvalue().encode("utf-8-sig"), f"对话记录-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv")

    with ui.row().classes("w-full mt-4 gap-2 items-center"):
        ui.input("用户（工号或姓名）").bind_value(state, "sender").classes("w-44").props("outlined dense")
        ui.input("内容关键词").bind_value(state, "keyword").classes("w-44").props("outlined dense")
        ui.select({"": "全部分级", "A": "A 精确命中", "B": "B AI 生成", "C": "C 转人工"}, value="").bind_value(state, "level").classes("w-36").props("outlined dense")
        ui.select({"": "全部", "1": "转人工", "0": "未转"}, value="").bind_value(state, "escalated").classes("w-24").props("outlined dense")
        ui.button("查询", icon="search", on_click=load).props("color=primary unelevated")
        ui.button("刷新", icon="refresh", on_click=load).props("outline color=primary")
        ui.html('<div style="width:1px;height:28px;background:#e5e7eb;margin:0 4px"></div>')
        ui.button("导出 CSV", icon="download", on_click=export_csv).props("outline color=primary")
        ui.html('<span style="font-size:12px;color:#9ca3af;margin-left:4px">点击行查看完整详情</span>')

    table = ui.table(
        columns=[
            {"name": "seq", "label": "序号", "field": "seq", "align": "left", "style": "width: 50px"},
            {"name": "time", "label": "时间", "field": "time", "style": "width: 150px; white-space: nowrap"},
            {"name": "sender", "label": "用户", "field": "sender", "style": "width: 90px; white-space: nowrap"},
            {"name": "question", "label": "问题", "field": "question", "align": "left", "style": "min-width: 180px; max-width: 260px; white-space: normal; word-break: break-word"},
            {"name": "answer", "label": "回答", "field": "answer", "align": "left", "style": "min-width: 240px; max-width: 350px; white-space: normal; word-break: break-word"},
            {"name": "level", "label": "分级", "field": "level", "style": "width: 60px"},
            {"name": "escalated", "label": "转人工", "field": "escalated", "style": "width: 70px"},
            {"name": "latency", "label": "耗时(ms)", "field": "latency", "style": "width: 90px"},
            {"name": "tokens", "label": "Tokens", "field": "tokens", "style": "width: 80px"},
            {"name": "model", "label": "模型", "field": "model", "style": "width: 120px; white-space: nowrap"},
        ],
        rows=[],
        row_key="id",
        pagination=20,
    ).classes("w-full mt-2 cursor-pointer").props(TABLE_ZH_PROPS)
    table.on("rowClick", lambda e: show_detail(e.args[1]["id"]))

    load()
    # 每 5 秒自动刷新，让新对话实时出现，不用手动点刷新
    ui.timer(5.0, load)
    # 如果 URL 带了 ?id=xxx，自动弹窗（钉钉告警链接点过来时用）
    if auto_open_id:
        show_detail(auto_open_id)


# ------------- 未命中 -------------
@ui.page("/unmatched")
def page_unmatched():
    if not _require_login():
        return
    _inject_zh()
    _layout("/unmatched")

    ui.label("按对话聚合，频次高的就是知识库缺口。可切换两种视图。").classes("text-slate-500 mt-2")

    can_delete = A.can_edit_qa()  # admin / editor 才能删
    # 视图模式："escalated"=转人工缺口(C级)  "fallback"=LLM兜底缺口(B级，真正大头)
    view_state = {"mode": "fallback"}

    table = ui.table(
        columns=[
            {"name": "question", "label": "问题", "field": "question", "align": "left"},
            {"name": "cnt", "label": "频次", "field": "cnt"},
            {"name": "last_seen", "label": "最近一次", "field": "last_seen"},
            {"name": "action", "label": "操作", "field": "question"},
        ],
        rows=[], row_key="question", pagination=20,
        selection="multiple",  # 表头出现勾选列，可多选/全选
    ).classes("w-full mt-2").props(TABLE_ZH_PROPS)

    # 按钮的事件回调（通过 q-btn 的 click 触发，需要 NiceGUI 的 slot 机制）
    table.add_slot("body-cell-action", r"""
        <q-td :props="props">
            <q-btn dense flat label="删除" color="negative"
                   @click="$parent.$emit('delete_row', props.row)"
                   style="font-size:12px"/>
        </q-td>
    """)

    def do_delete_row(e):
        q_text = (e.args or {}).get("question", "")
        cnt = (e.args or {}).get("cnt", 0)
        if not q_text:
            return
        if not can_delete:
            ui.notify("没有权限：仅管理员/编辑可清理未命中记录", type="negative")
            return
        # 二次确认弹窗
        with ui.dialog() as confirm_dlg, ui.card().classes("w-[480px]"):
            ui.label("⚠️ 确认清理这个未命中问题？").classes("text-lg font-semibold text-red-600")
            ui.separator()
            with ui.column().classes("gap-2 mt-2"):
                ui.label(f"问题文本：").classes("text-slate-600 text-sm")
                ui.label(q_text).classes("text-slate-900 text-sm font-mono bg-slate-50 p-2 rounded")
                ui.label(f"将删除 {cnt} 条相关的【转人工】对话记录。").classes("text-slate-600 text-sm mt-2")
                ui.label("⚠️ 删除后不可恢复（DB 备份在 /tmp 仅作灾难恢复用）。").classes("text-orange-600 text-xs")
            with ui.row().classes("mt-4 justify-end gap-2 w-full"):
                ui.button("取消", on_click=confirm_dlg.close).props("flat")
                def do_confirm():
                    db = SessionLocal()
                    try:
                        dq = db.query(Conversation).filter(Conversation.question == q_text)
                        if view_state["mode"] == "escalated":
                            dq = dq.filter(Conversation.escalated == "1")
                        else:
                            dq = dq.filter(Conversation.answer_level == "B")
                        n = dq.delete(synchronize_session=False)
                        db.commit()
                    finally:
                        db.close()
                    confirm_dlg.close()
                    ui.notify(f"已删除 {n} 条记录", type="positive")
                    load()
                ui.button("确认删除", on_click=do_confirm, color="negative")
        confirm_dlg.open()

    table.on("delete_row", do_delete_row)

    async def batch_delete_selected():
        """批量删除勾选的未命中问题（每个问题对应多条 conversation）。"""
        sel = table.selected or []
        if not sel:
            ui.notify("请先勾选要删除的问题", type="warning")
            return
        if not can_delete:
            ui.notify("没有权限：仅管理员/编辑可清理未命中记录", type="negative")
            return
        questions = [r["question"] for r in sel]
        total_cnt = sum(r.get("cnt", 0) for r in sel)
        with ui.dialog() as dlg, ui.card().classes("w-[520px]"):
            ui.label("⚠️ 确认批量清理选中的未命中问题？").classes("text-lg font-semibold text-red-600")
            ui.separator()
            with ui.column().classes("gap-2 mt-2"):
                ui.label(f"已勾选 {len(questions)} 个问题，共约 {total_cnt} 条对话记录。").classes("text-slate-600 text-sm")
                ui.label("问题列表：").classes("text-slate-600 text-sm mt-2")
                with ui.scroll_area().classes("h-[200px] bg-slate-50 p-2 rounded"):
                    for q in questions:
                        ui.label(f"• {q}").classes("text-slate-900 text-xs font-mono")
                ui.label("⚠️ 删除后不可恢复（DB 备份在 /tmp 仅作灾难恢复用）。").classes("text-orange-600 text-xs mt-2")
            with ui.row().classes("mt-4 justify-end gap-2 w-full"):
                ui.button("取消", on_click=dlg.close).props("flat")
                def do_confirm():
                    db = SessionLocal()
                    try:
                        dq = db.query(Conversation).filter(Conversation.question.in_(questions))
                        if view_state["mode"] == "escalated":
                            dq = dq.filter(Conversation.escalated == "1")
                        else:
                            dq = dq.filter(Conversation.answer_level == "B")
                        n = dq.delete(synchronize_session=False)
                        db.commit()
                    finally:
                        db.close()
                    dlg.close()
                    table.selected = []
                    ui.notify(f"已删除 {n} 条记录", type="positive")
                    load()
                ui.button("确认删除", on_click=do_confirm, color="negative")
        dlg.open()

    def load():
        from sqlalchemy import or_, and_, not_
        db = SessionLocal()
        try:
            q = db.query(
                Conversation.question.label("question"),
                func.count().label("cnt"),
                func.max(Conversation.created_at).label("last_seen"),
            )
            if view_state["mode"] == "escalated":
                # 转人工缺口（C级）
                q = q.filter(Conversation.escalated == "1")
            else:
                # LLM 兜底缺口（B级真实兜底）：排除闲聊/空消息/挫败/充值等保护话术（raw_llm_answer 以 [ 开头的都是标记）
                q = q.filter(
                    Conversation.answer_level == "B",
                    func.length(Conversation.question) > 4,
                    Conversation.question.notlike("%连发%"),
                    or_(
                        Conversation.raw_llm_answer.is_(None),
                        not_(Conversation.raw_llm_answer.like("[%")),
                    ),
                )
            rows = q.group_by(Conversation.question)\
                    .order_by(func.count().desc()).limit(50).all()
        finally:
            db.close()
        table.rows = [
            {"question": r.question, "cnt": r.cnt,
             "last_seen": r.last_seen.strftime("%Y-%m-%d %H:%M") if r.last_seen else ""}
            for r in rows
        ]
        table.update()

    with ui.row().classes("mt-2 gap-2 items-center"):
        # 视图切换
        def switch_view(mode):
            view_state["mode"] = mode
            table.selected = []
            _render_toggle.refresh()
            load()

        @ui.refreshable
        def _render_toggle():
            with ui.row().classes("gap-1"):
                fb = view_state["mode"] == "fallback"
                ui.button("🤖 LLM兜底缺口", on_click=lambda: switch_view("fallback"))\
                    .props(f'{"color=primary" if fb else "flat color=grey"} dense')
                ui.button("👤 转人工缺口", on_click=lambda: switch_view("escalated"))\
                    .props(f'{"color=primary" if not fb else "flat color=grey"} dense')
        _render_toggle()

        ui.button("刷新", icon="refresh", on_click=load).props("flat dense")
        if can_delete:
            ui.button("批量删除选中", icon="delete_sweep", on_click=batch_delete_selected).props("outline color=negative dense")
        else:
            ui.label("（删除功能仅管理员/编辑可见）").classes("text-slate-400 text-xs")

    ui.label("💡 「LLM兜底缺口」= 用户问了但 KB 没有、机器人现编的问题，频次高的最该补进知识库").classes("text-slate-400 text-xs mt-1")
    load()


def _action_label(a: str) -> str:
    return {"add_tag": "🏷️ 建议补Tag", "new_qa": "➕ 建议新增QA",
            "ignore": "⬜ 忽略", "error": "❌ 分析失败"}.get(a, a)


@ui.page("/failure-reports")
def page_failure_reports():
    if not _require_login():
        return
    if A.role() != "admin":
        ui.notify("仅管理员可访问失败分析报告", type="negative")
        ui.navigate.to("/")
        return
    _inject_zh()
    _layout("/failure-reports")
    import json as _json
    from pathlib import Path as _Path

    report_dir = _Path(__file__).resolve().parent.parent.parent / "data" / "failure_reports"

    ui.label("失败案例回流分析：把没命中知识库的真实问题，自动判别该补 Tag、该新增 QA、还是忽略。").classes("text-slate-500 mt-2")
    ui.label("报告由 scripts/failure_replay.py 离线生成；这里只读查看与导出，不会自动改库。").classes("text-slate-400 text-xs")

    def _list_reports():
        if not report_dir.exists():
            return []
        return sorted(report_dir.glob("report_*.json"), reverse=True)

    files = _list_reports()
    if not files:
        ui.label("暂无报告。先在 backend 目录运行：").classes("mt-4 text-slate-600")
        ui.code("EMB_BACKEND=ollama EMB_MODEL=bge-m3 .venv/bin/python ../scripts/failure_replay.py --limit 50").classes("w-full")
        return

    # 文件选择 + 汇总区 + 表格
    options = {str(f): f.name.replace("report_", "").replace(".json", "") for f in files}
    sel = ui.select(options, value=str(files[0]), label="选择报告（按时间倒序）").classes("w-96 mt-3")

    summary_box = ui.row().classes("gap-4 mt-2 items-center")
    table = ui.table(
        columns=[
            {"name": "q", "label": "用户问题", "field": "q", "align": "left"},
            {"name": "action", "label": "处置建议", "field": "action_label", "align": "left"},
            {"name": "target", "label": "目标QA", "field": "target", "align": "left"},
            {"name": "tags", "label": "建议Tag", "field": "tags_str", "align": "left"},
            {"name": "reason", "label": "理由", "field": "reason", "align": "left"},
        ],
        rows=[], row_key="q", pagination=20,
    ).classes("w-full mt-2").props(TABLE_ZH_PROPS)

    def _safe_report_path(path_str: str):
        """约束下载/读取只能落在 report_dir 内的 report_*.json，
        防止前端篡改 select 值读取任意文件（如 data/.master.key、*.db）。"""
        try:
            p = _Path(path_str).resolve()
        except Exception:
            return None
        base = report_dir.resolve()
        if base not in p.parents:
            return None
        if p.suffix != ".json" or not p.name.startswith("report_"):
            return None
        return p

    def load_report(path_str: str):
        summary_box.clear()
        p = _safe_report_path(path_str)
        if p is None:
            ui.notify("非法的报告路径", type="negative")
            table.rows = []
            return
        try:
            data = _json.loads(p.read_text())
        except Exception as e:
            ui.notify(f"读取失败：{e}", type="negative")
            table.rows = []
            return
        stat = data.get("stat", {})
        with summary_box:
            ui.label(f"生成时间：{data.get('generated_at','')}").classes("text-sm text-slate-600")
            ui.badge(f"补Tag {stat.get('add_tag',0)}", color="indigo")
            ui.badge(f"新增QA {stat.get('new_qa',0)}", color="green")
            ui.badge(f"忽略 {stat.get('ignore',0)}", color="grey")
            ui.badge(f"已修复跳过 {stat.get('now_fixed',0)}", color="teal")
            ui.badge(f"失败 {stat.get('error',0)}", color="red")
        rows = []
        for it in data.get("items", []):
            tid = it.get("target_qa_id")
            tq = it.get("target_qa_q") or ""
            tags = it.get("tags") or []
            rows.append({
                "q": it.get("q", ""),
                "action_label": _action_label(it.get("action", "")),
                "target": (f"#{tid} {tq[:20]}" if tid else ""),
                "tags_str": "，".join(tags) if isinstance(tags, list) else str(tags),
                "reason": it.get("reason", ""),
            })
        table.rows = rows
        table.update()

    def do_download():
        p = _safe_report_path(sel.value)
        if p is None:
            ui.notify("非法的报告路径", type="negative"); return
        try:
            content = p.read_bytes()
            ui.download(content, p.name)
        except Exception as e:
            ui.notify(f"导出失败：{e}", type="negative")

    with ui.row().classes("mt-2 gap-2"):
        ui.button("导出当前报告 JSON", icon="download", on_click=do_download).props("color=primary")
        ui.button("刷新列表", icon="refresh", on_click=lambda: ui.navigate.to("/failure-reports")).props("flat")

    sel.on_value_change(lambda e: load_report(e.value))
    load_report(str(files[0]))


# ------------- 用户管理 -------------
ROLE_LABEL = {
    "admin":   "管理员（全部权限）",
    "auditor": "审核员（可审核 QA）",
    "editor":  "运营（可增改 QA、待审）",
    "viewer":  "只读（仅查看）",
}


@ui.page("/users")
def page_users():
    if not _require_login():
        return
    if A.role() != "admin":
        ui.notify("仅管理员可访问用户管理", type="negative")
        ui.navigate.to("/")
        return
    _inject_zh()
    _layout("/users")

    table = None

    def load():
        db = SessionLocal()
        try:
            rows = db.query(SysUser).order_by(SysUser.id).all()
            data = [{
                "seq": idx,
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name or "",
                "role_key": u.role,
                "role": ROLE_LABEL.get(u.role, u.role),
                "enabled": "是" if u.enabled == "1" else "否",
            } for idx, u in enumerate(rows, 1)]
        finally:
            db.close()
        table.rows = data
        table.update()

    def open_form(uid: Optional[int]):
        db = SessionLocal()
        try:
            cur = db.get(SysUser, uid) if uid else None
        finally:
            db.close()
        is_new = cur is None

        with ui.dialog() as dlg, ui.card().classes("w-[520px]"):
            ui.label("新增用户" if is_new else f"编辑用户 #{cur.id}").classes("text-lg font-semibold")
            uname = ui.input("登录账号 / 用户名（创建后不可修改）",
                             value="" if is_new else cur.username)\
                .classes("w-full").props(("readonly" if not is_new else ""))
            dname = ui.input("姓名（这里可以改）", value="" if is_new else (cur.display_name or "")).classes("w-full")
            role  = ui.select(ROLE_LABEL, value="viewer" if is_new else cur.role, label="角色").classes("w-full")
            enabled = ui.switch("启用账号", value=True if is_new else (cur.enabled == "1"))
            pwd_label = "初始密码（必填）" if is_new else "重置密码（留空则保留原密码）"
            pwd = ui.input(pwd_label, password=True, password_toggle_button=True).classes("w-full")

            def save():
                u_name = (uname.value or "").strip()
                d_name = (dname.value or "").strip()
                pwd_v  = (pwd.value or "").strip()
                if not u_name:
                    ui.notify("登录账号不能为空", type="warning"); return
                if is_new and not pwd_v:
                    ui.notify("新建用户必须设置初始密码", type="warning"); return
                if is_new and not d_name:
                    d_name = u_name

                db2 = SessionLocal()
                try:
                    if is_new:
                        if db2.query(SysUser).filter(SysUser.username == u_name).first():
                            ui.notify("账号已存在", type="warning"); return
                        db2.add(SysUser(
                            username=u_name, display_name=d_name,
                            password=hash_password(pwd_v),
                            role=role.value, enabled="1" if enabled.value else "0",
                        ))
                    else:
                        obj = db2.get(SysUser, uid)
                        obj.display_name = d_name
                        obj.role = role.value
                        obj.enabled = "1" if enabled.value else "0"
                        if pwd_v:
                            obj.password = hash_password(pwd_v)
                    db2.commit()
                finally:
                    db2.close()
                ui.notify("已保存", type="positive")
                dlg.close()
                load()

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("取消", on_click=dlg.close)
                ui.button("保存", on_click=save).props("color=primary")
        dlg.open()

    def toggle_enabled(uid: int):
        db = SessionLocal()
        try:
            u = db.get(SysUser, uid)
            if u.username == A.user()["username"]:
                ui.notify("不能禁用自己的账号", type="warning"); return
            u.enabled = "0" if u.enabled == "1" else "1"
            db.commit()
        finally:
            db.close()
        load()

    def reset_pwd(uid: int):
        with ui.dialog() as dlg, ui.card().classes("w-96"):
            ui.label("重置密码").classes("text-lg font-semibold")
            pwd = ui.input("新密码（≥6 位）", password=True, password_toggle_button=True).classes("w-full")

            def do():
                v = (pwd.value or "").strip()
                if len(v) < 6:
                    ui.notify("密码至少 6 位", type="warning"); return
                db = SessionLocal()
                try:
                    u = db.get(SysUser, uid)
                    u.password = hash_password(v)
                    db.commit()
                finally:
                    db.close()
                ui.notify("已重置密码", type="positive"); dlg.close()
            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("取消", on_click=dlg.close)
                ui.button("确认", on_click=do).props("color=primary")
        dlg.open()

    def delete_user(uid: int):
        db = SessionLocal()
        try:
            u = db.get(SysUser, uid)
            if u.username == A.user()["username"]:
                ui.notify("不能删除自己的账号", type="warning"); return
            db.delete(u); db.commit()
        finally:
            db.close()
        ui.notify("已删除", type="positive")
        load()

    # --- 顶部工具栏 ---
    with ui.row().classes("w-full mt-4 gap-2 items-center"):
        ui.button("新增用户", icon="person_add", on_click=lambda: open_form(None)).props("unelevated").style("background:#6366f1;color:#fff")
        ui.html('<span style="font-size:13px;color:#9ca3af;margin-left:4px">管理员可在此创建、停用、改角色、重置密码。</span>')

    # --- 表格 ---
    table = ui.table(
        columns=[
            {"name": "seq", "label": "序号", "field": "seq", "align": "left"},
            {"name": "username", "label": "登录账号", "field": "username"},
            {"name": "display_name", "label": "姓名", "field": "display_name"},
            {"name": "role", "label": "角色", "field": "role"},
            {"name": "enabled", "label": "启用", "field": "enabled"},
            {"name": "action", "label": "操作", "field": "id"},
        ],
        rows=[],
        row_key="id",
    ).classes("w-full mt-2").props(TABLE_ZH_PROPS)
    table.add_slot("body-cell-action", r"""
        <q-td :props="props">
            <q-btn dense flat label="编辑" color="primary" @click="$parent.$emit('edit', props.row)" style="font-size:12px"/>
            <q-btn dense flat label="重置密码" color="primary" @click="$parent.$emit('reset', props.row)" style="font-size:12px"/>
            <q-btn dense flat :label="props.row.enabled==='是' ? '停用' : '启用'" :color="props.row.enabled==='是' ? 'grey' : 'positive'" @click="$parent.$emit('toggle', props.row)" style="font-size:12px"/>
            <q-btn dense flat label="删除" color="negative" @click="$parent.$emit('remove', props.row)" style="font-size:12px"/>
        </q-td>
    """)
    table.on("edit",   lambda e: open_form(e.args["id"]))
    table.on("reset",  lambda e: reset_pwd(e.args["id"]))
    table.on("toggle", lambda e: toggle_enabled(e.args["id"]))
    table.on("remove", lambda e: delete_user(e.args["id"]))

    load()


# ------------- 系统设置 -------------
PRESET_VENDORS = {
    "自定义 / 本地": ("", ""),
    "阿里通义千问": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "Moonshot Kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "智谱 GLM": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-plus"),
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "本地 vLLM / Ollama": ("http://localhost:8000/v1", "qwen2.5"),
}


@ui.page("/settings")
def page_settings():
    if not _require_login():
        return
    if A.role() != "admin":
        ui.notify("仅管理员可访问系统设置", type="negative")
        ui.navigate.to("/")
        return
    _inject_zh()
    _layout("/settings")

    cur = rts.current_masked()

    # llm_api_key / dingtalk_client_secret 显示脱敏占位（如 sk-1234********wxyz）。
    # 用户如果不修改这一栏，就保留原值；如果输入了新值（不再是占位符），才会覆盖。
    masked_api_key = str(cur["llm_api_key"] or "")
    masked_ding_secret = str(cur["dingtalk_client_secret"] or "")

    form = {
        "llm_enabled": bool(cur["llm_enabled"]),
        "llm_base_url": str(cur["llm_base_url"] or ""),
        "llm_api_key": masked_api_key,
        "llm_model": str(cur["llm_model"] or ""),
        "llm_timeout": int(cur["llm_timeout"] or 15),
        "llm_temperature": float(cur["llm_temperature"] or 0.3),
        "llm_system_prompt_header": str(cur["llm_system_prompt_header"] or ""),
        "bot_persona": str(cur.get("bot_persona") or ""),
        "product_background": str(cur.get("product_background") or ""),
        "rephrase_kb_hit": bool(cur.get("rephrase_kb_hit", True)),
        "domain_router_enabled": bool(cur.get("domain_router_enabled", False)),
        "dingtalk_client_id": str(cur["dingtalk_client_id"] or ""),
        "dingtalk_client_secret": masked_ding_secret,
        "dingtalk_robot_code": str(cur.get("dingtalk_robot_code") or ""),
        "escalate_reply": str(cur["escalate_reply"] or ""),
        "watermark": str(cur["watermark"] or ""),
        "contact_routing": str(cur.get("contact_routing") or ""),
        "alert_webhook": str(cur.get("alert_webhook") or ""),
        "alert_secret": str(cur.get("alert_secret") or ""),
        "escalate_user_reply": str(cur.get("escalate_user_reply") or ""),
        "complaint_keywords": str(cur.get("complaint_keywords") or ""),
        "escalate_id_prefixes": str(cur.get("escalate_id_prefixes") or ""),
        "repeat_threshold": int(cur.get("repeat_threshold") or 3),
        "admin_url": str(cur.get("admin_url") or ""),
        "public_base_url": str(cur.get("public_base_url") or ""),
        "recharge_rules": str(cur.get("recharge_rules") or ""),
        "recharge_reply": str(cur.get("recharge_reply") or ""),
        "vid_link": str(cur.get("vid_link") or ""),
        "vid_reply": str(cur.get("vid_reply") or ""),
    }

    with ui.card().classes("w-full mt-4 p-6"):
        ui.label("大模型配置").classes("text-xl font-semibold mb-2")
        ui.label("修改后点保存即时生效，无需重启服务。所有兼容 OpenAI 协议的厂商均可。").classes("text-slate-500 mb-4")

        with ui.row().classes("w-full gap-4 items-center"):
            ui.switch("启用大模型（关闭后机器人将回显原文）", value=form["llm_enabled"]).bind_value(form, "llm_enabled")

        def apply_preset(name: str):
            url, model = PRESET_VENDORS[name]
            if url:
                form["llm_base_url"] = url
                form["llm_model"] = model
                base_url_in.value = url
                model_in.value = model

        with ui.row().classes("w-full gap-4 mt-2"):
            ui.select(list(PRESET_VENDORS.keys()), value="自定义 / 本地", label="选择厂商（自动填地址）",
                      on_change=lambda e: apply_preset(e.value)).classes("w-64")

        base_url_in = ui.input("接口地址 Base URL（必须包含 /v1，不要带 /chat/completions）").bind_value(form, "llm_base_url").classes("w-full")
        api_key_in = ui.input("接口密钥 API Key（已保存的会脱敏展示；不修改即保留）",
                              password=True, password_toggle_button=True)\
            .bind_value(form, "llm_api_key").classes("w-full")
        model_in = ui.select(
            options=[form["llm_model"]] if form["llm_model"] else [],
            value=form["llm_model"] or None,
            label='模型名称（点右侧"自动检测"获取此密钥可用的模型）',
            with_input=True,        # 允许手动输入（厂商列表里没有的私有模型）
            new_value_mode="add",
        ).bind_value(form, "llm_model").classes("w-full")

        with ui.row().classes("w-full gap-4"):
            ui.number("超时时间（秒）", min=1, max=120).bind_value(form, "llm_timeout").classes("w-40")
            ui.number("温度（创造性 0-2，越低越严谨）", min=0, max=2, step=0.1, format="%.1f").bind_value(form, "llm_temperature").classes("w-64")

        ui.label("系统提示词头部（不含 QA 知识库部分，知识库会自动追加在后面）").classes("mt-4 text-sm text-slate-600")
        ui.textarea(value=form["llm_system_prompt_header"]).bind_value(form, "llm_system_prompt_header").classes("w-full").props("rows=4")

        ui.label("机器人人设（决定说话语气，越具体越像真人）").classes("mt-4 text-sm text-slate-600")
        ui.textarea(value=form["bot_persona"]).bind_value(form, "bot_persona").classes("w-full").props("rows=8")

        ui.label("业务背景与核心原则（让机器人真正『懂业务』，不是死记 FAQ）").classes("mt-4 text-sm text-slate-600 font-semibold")
        ui.label("把每个产品/模块的核心结论、关键原则、机制说明写在这里。"
                 "每个模块独立一段，开头用【】包标题。命中知识库时的润色和未命中的兜底回答都会先按这套原则推理。"
                 "示例：【Seedance 审核机制】- 卡人脸的根因是视频没过素材库预审；- 信任锚点是素材 ID 而不是人脸……").classes("text-slate-500 text-xs mb-1")
        ui.textarea(value=form["product_background"],
                    placeholder="【Seedance 审核机制】\n"
                                "- 卡人脸的根因不是系统识别到某个人，而是视频素材没有通过素材库前置审核\n"
                                "- 当前审核体系的信任锚点是「素材 ID」，不是人脸/声音/人物身份\n"
                                "- 图片能过、视频会卡，是因为素材库历史上只支持图片入库\n"
                                "- 任何剪辑、二次加工后的视频都需要重新入库审核\n"
                                "- 落地动作：视频先入素材库 → 拿到素材 ID → 调用 seedance 时带 ID\n"
                                "\n"
                                "【XX 产品 XX 模块】\n- ……")\
            .bind_value(form, "product_background").classes("w-full").props("rows=12")

        with ui.row().classes("w-full gap-4 mt-2 items-center"):
            ui.switch("命中知识库后用人设口吻润色（更自然但慢 1-2 秒）",
                      value=form["rephrase_kb_hit"]).bind_value(form, "rephrase_kb_hit")

        with ui.row().classes("w-full gap-4 mt-2 items-center"):
            ui.switch("启用业务域路由（先把问题分到业务域，缩小匹配范围、降低跨域误命中）",
                      value=form["domain_router_enabled"]).bind_value(form, "domain_router_enabled")
        ui.label("开启前请先用脚本给 QA 打域标签（label_qa_domains.py）。采用软过滤：不会丢命中、"
                 "仅把跨域候选降权排后；异常可随时关闭回退。每次问答会多一次 LLM 分类调用（约 +0.5 秒）。")\
            .classes("text-slate-500 text-xs -mt-2")

        def _real_api_key() -> str:
            """用户没改 → 用 settings 里的真值；改了 → 用框里的新值。"""
            v = (form["llm_api_key"] or "").strip()
            if not v or v == masked_api_key:
                return str(cfg.llm_api_key or "")
            return v

        def _real_ding_secret() -> str:
            v = (form["dingtalk_client_secret"] or "").strip()
            if not v or v == masked_ding_secret:
                return str(cfg.dingtalk_client_secret or "")
            return v

        def test_llm():
            api_key = _real_api_key()
            if not form["llm_base_url"] or not api_key:
                ui.notify("请先填写接口地址和接口密钥", type="warning"); return
            try:
                url = form["llm_base_url"].rstrip("/") + "/chat/completions"
                payload = {
                    "model": form["llm_model"] or "qwen-plus",
                    "messages": [{"role": "user", "content": "你好"}],
                    "temperature": 0.1,
                }
                headers = {"Authorization": f"Bearer {api_key}"}
                r = httpx.post(url, json=payload, headers=headers, timeout=form["llm_timeout"] or 15)
                if r.status_code != 200:
                    ui.notify(f"调用失败 HTTP {r.status_code}：{r.text[:200]}", type="negative", multi_line=True)
                    return
                content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
                ui.notify(f"调用成功，模型回复：{content[:80]}", type="positive", multi_line=True)
            except Exception as e:
                ui.notify(f"调用异常：{e}", type="negative", multi_line=True)

        def save_llm():
            updates = {
                "llm_enabled": form["llm_enabled"],
                "llm_base_url": form["llm_base_url"],
                "llm_api_key": _real_api_key(),     # 不会把脱敏字符串写库
                "llm_model": form["llm_model"],
                "llm_timeout": form["llm_timeout"],
                "llm_temperature": form["llm_temperature"],
                "llm_system_prompt_header": form["llm_system_prompt_header"],
                "bot_persona": form["bot_persona"],
                "product_background": form["product_background"],
                "rephrase_kb_hit": form["rephrase_kb_hit"],
                "domain_router_enabled": form["domain_router_enabled"],
            }
            rts.save_to_db(updates)
            store.reload()
            ui.notify("已保存，密钥已加密入库，模型即时生效", type="positive")

        def detect_models():
            api_key = _real_api_key()
            base = (form["llm_base_url"] or "").strip()
            if not base or not api_key:
                ui.notify("请先填写接口地址和接口密钥", type="warning"); return
            ok, result = llm_svc.list_models(base, api_key, timeout=form["llm_timeout"] or 10)
            if not ok:
                ui.notify(f"获取失败：{result}", type="negative", multi_line=True); return
            if not result:
                ui.notify("拉取到的模型列表为空", type="warning"); return
            # 更新下拉选项
            model_in.options = result
            # 当前值如果还在列表里就保留，否则默认选第一个
            if form["llm_model"] not in result:
                form["llm_model"] = result[0]
            model_in.update()
            ui.notify(f"检测到 {len(result)} 个模型，请下拉选择", type="positive")

        with ui.row().classes("w-full justify-end gap-2 mt-4"):
            ui.button("自动检测", icon="search", on_click=detect_models)
            ui.button("测试调用", icon="bolt", on_click=test_llm)
            ui.button("保存大模型配置", icon="save", on_click=save_llm).props("color=primary")

    with ui.card().classes("w-full mt-4 p-6"):
        ui.label("钉钉应用凭证").classes("text-xl font-semibold mb-2")
        ui.label("修改凭证后需要重启服务才能重新连接。").classes("text-slate-500 mb-2")
        ui.input("应用标识 Client ID（钉钉开放平台 AppKey）").bind_value(form, "dingtalk_client_id").classes("w-full")
        ui.input("应用密钥 Client Secret（已保存的会脱敏；不修改即保留）",
                 password=True, password_toggle_button=True)\
            .bind_value(form, "dingtalk_client_secret").classes("w-full")
        ui.input("机器人 RobotCode（单聊主动推送用；留空默认用 Client ID/AppKey）")\
            .bind_value(form, "dingtalk_robot_code").classes("w-full")

        def save_ding():
            rts.save_to_db({
                "dingtalk_client_id": form["dingtalk_client_id"],
                "dingtalk_client_secret": _real_ding_secret(),
                "dingtalk_robot_code": form["dingtalk_robot_code"],
            })
            ui.notify("已保存，重启服务后生效", type="warning")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("保存钉钉配置", icon="save", on_click=save_ding).props("color=primary")

    # === 群发推送 文件公网访问基址 ===
    with ui.card().classes("w-full mt-4 p-6"):
        ui.label("群发推送 · 文件公网访问基址").classes("text-xl font-semibold mb-2")
        ui.label("群发推送时，本地上传的图片/视频需要钉钉服务器能从公网拉取。").classes("text-slate-500 text-sm")
        ui.label("填上你这台服务器的公网域名/IP（不带尾部斜杠）。例如 http://1.2.3.4:8080 或 https://qa-bot.公司.com").classes("text-slate-500 text-sm mb-2")
        ui.input("文件访问基址",
                 placeholder="http://你的公网IP:8080 或 https://qa-bot.example.com")\
            .bind_value(form, "public_base_url").classes("w-full")
        ui.label("⚠️ 仅本地内网测试可留空，钉钉会拉不到文件，群里链接打不开。").classes("text-orange-600 text-xs mt-1")

        def save_pubbase():
            val = (form.get("public_base_url") or "").strip().rstrip("/")
            if val and not (val.startswith("http://") or val.startswith("https://")):
                ui.notify("必须以 http:// 或 https:// 开头", type="warning"); return
            rts.save_to_db({"public_base_url": val})
            ui.notify("已保存，新上传的文件会使用这个基址。", type="positive")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("保存", icon="save", on_click=save_pubbase).props("color=primary")

    with ui.card().classes("w-full mt-4 p-6"):
        ui.label("机器人回复话术").classes("text-xl font-semibold mb-2")
        ui.input("转人工兜底话术").bind_value(form, "escalate_reply").classes("w-full")
        ui.input("智能回答的署名水印（追加到 AI 生成答案末尾）").bind_value(form, "watermark").classes("w-full")

        def save_text():
            rts.save_to_db({"escalate_reply": form["escalate_reply"], "watermark": form["watermark"]})
            ui.notify("已保存", type="positive")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("保存话术配置", icon="save", on_click=save_text).props("color=primary")

    with ui.card().classes("w-full mt-4 p-6"):
        ui.label("场景联系人").classes("text-xl font-semibold mb-2")
        ui.label("当机器人答不出问题时，会根据用户问题从下面挑相关的一条告诉用户。"
                 "每行一条，格式：「场景关键词：联系方式」。修改后自动生效。").classes("text-slate-500 mb-2 text-sm")

        ui.textarea(value=form.get("contact_routing", ""),
                    placeholder="图片生成 / 视频生成 / 模型问题：钉钉群「产品支持」或找张三\n"
                                "账号 / 登录 / 充值：钉钉找李四\n"
                                "退款 / 发票：钉钉找王五").bind_value(form, "contact_routing")\
            .classes("w-full").props("rows=8")

        def save_contacts():
            rts.save_to_db({"contact_routing": form["contact_routing"]})
            store.reload()
            ui.notify(f"已保存，知识库已刷新到第 {store.version} 版", type="positive")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("保存场景联系人", icon="save", on_click=save_contacts).props("color=primary")

    # === 充值引导 ===
    with ui.card().classes("w-full mt-4 p-6"):
        ui.label("充值引导（多场景）").classes("text-xl font-semibold mb-2")
        ui.label("用户私聊问充值时，机器人直接发对应场景的钉钉文档链接（不走大模型，链接 100% 原样）。"
                 "每个场景独立配：触发词 + 链接 + 该场景专属话术。").classes("text-slate-500 mb-1 text-sm")
        ui.label("· 触发词逗号分隔，命中任意一个即匹配  · 从上到下匹配，第一条命中胜出（具体场景放前面）"
                 "  · 话术里写 {link} 会自动替换成该条链接").classes("text-slate-400 mb-2 text-xs")

        # 解析现有规则（JSON 或旧行格式）成 list[dict]，供动态编辑
        import json as _json

        def _parse_rules(raw: str):
            raw = (raw or "").strip()
            out = []
            if not raw:
                return out
            if raw.startswith("["):
                try:
                    for it in _json.loads(raw):
                        if isinstance(it, dict):
                            out.append({"keywords": it.get("keywords", "") or "",
                                        "link": it.get("link", "") or "",
                                        "reply": it.get("reply", "") or ""})
                except Exception:
                    pass
            else:
                for line in raw.split("\n"):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 2:
                        out.append({"keywords": parts[0], "link": parts[1],
                                    "reply": parts[2] if len(parts) >= 3 else ""})
            return out

        recharge_state = {"rules": _parse_rules(form.get("recharge_rules", ""))}

        rules_container = ui.column().classes("w-full gap-3")

        @ui.refreshable
        def render_rules():
            rules_container.clear()
            with rules_container:
                if not recharge_state["rules"]:
                    ui.label("还没有充值场景，点下方「+ 添加充值场景」开始。").classes("text-slate-400 text-sm")
                for idx, rule in enumerate(recharge_state["rules"]):
                    with ui.card().classes("w-full p-3 bg-slate-50"):
                        with ui.row().classes("w-full justify-between items-center"):
                            ui.label(f"场景 {idx + 1}").classes("text-sm font-semibold text-indigo-700")
                            ui.button(icon="delete", on_click=lambda i=idx: _del_rule(i))\
                                .props("flat dense color=negative").classes("text-xs")
                        ui.input("触发词（逗号分隔，如：个人充值,充钱,积分不够）")\
                            .bind_value(rule, "keywords").classes("w-full")
                        ui.input("钉钉文档链接")\
                            .bind_value(rule, "link").classes("w-full")
                        ui.textarea("该场景专属话术（{link} 替换成上面链接）")\
                            .bind_value(rule, "reply").classes("w-full").props("rows=2")

        def _del_rule(i):
            if 0 <= i < len(recharge_state["rules"]):
                recharge_state["rules"].pop(i)
                render_rules.refresh()

        def _add_rule():
            recharge_state["rules"].append({"keywords": "", "link": "",
                                            "reply": "这个走这个链接填一下哈 👉 {link}"})
            render_rules.refresh()

        render_rules()

        def save_recharge():
            cleaned = []
            errs = []
            for i, r in enumerate(recharge_state["rules"], 1):
                kw = (r.get("keywords") or "").strip()
                # 统一分隔符：中文逗号/顿号 → 英文逗号（用户常混用，避免匹配失效）
                import re as _re_kw
                kw = _re_kw.sub(r"[，、]", ",", kw)
                kw = _re_kw.sub(r",+", ",", kw).strip(",")
                link = (r.get("link") or "").strip()
                reply = (r.get("reply") or "").strip()
                if not kw and not link and not reply:
                    continue  # 整条空 → 跳过
                if not kw:
                    errs.append(f"场景{i}：触发词不能为空")
                elif not link:
                    errs.append(f"场景{i}：链接不能为空")
                elif not link.startswith(("http://", "https://")):
                    errs.append(f"场景{i}：链接必须 http/https 开头")
                elif not reply:
                    errs.append(f"场景{i}：话术不能为空（每个场景要有自己的话术）")
                else:
                    cleaned.append({"keywords": kw, "link": link, "reply": reply})
            if errs:
                ui.notify("保存失败：\n" + "\n".join(errs), type="negative", multi_line=True)
                return
            rts.save_to_db({"recharge_rules": _json.dumps(cleaned, ensure_ascii=False)})
            ui.notify(f"已保存 {len(cleaned)} 个充值场景，即时生效", type="positive")

        with ui.row().classes("w-full justify-between items-center mt-3"):
            ui.button("+ 添加充值场景", icon="add", on_click=_add_rule).props("outline color=primary")
            ui.button("保存充值规则", icon="save", on_click=save_recharge).props("color=primary")

    # === 业务 ID（vid-）填写链接 ===
    with ui.card().classes("w-full mt-4 p-6"):
        ui.label("业务 ID 填写链接（vid- 等）").classes("text-xl font-semibold mb-2")
        ui.label("用户发 vid-xxxx 这类业务 ID 时，机器人会自动转人工并告警群，"
                 "同时给用户发一个填写链接让他补充详情。前缀在「转人工」设置的"
                 "「业务 ID 前缀」里配（当前：vid-）。").classes("text-slate-500 mb-2 text-sm")

        ui.label("钉钉文档链接（用户填写 ID 详情）").classes("text-sm font-medium mt-2")
        ui.input(placeholder="https://alidocs.dingtalk.com/i/nodes/xxx").bind_value(form, "vid_link")\
            .classes("w-full")

        ui.label("追加话术（接在转人工话术后面，{link} 替换成上面链接）").classes("text-sm font-medium mt-3")
        ui.textarea(placeholder="另外你把这个 ID 的详细情况填一下这个表，方便我们快速处理 👉 {link}")\
            .bind_value(form, "vid_reply").classes("w-full").props("rows=2")

        def save_vid():
            link = (form.get("vid_link") or "").strip()
            if link and not link.startswith(("http://", "https://")):
                ui.notify("链接必须 http/https 开头", type="negative")
                return
            rts.save_to_db({
                "vid_link": form["vid_link"],
                "vid_reply": form["vid_reply"],
            })
            ui.notify("已保存业务 ID 填写链接，即时生效", type="positive")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("保存业务 ID 链接", icon="save", on_click=save_vid).props("color=primary")


    # === 转人工告警 ===
    with ui.card().classes("w-full mt-4 p-6"):
        ui.label("转人工告警").classes("text-xl font-semibold mb-2")
        ui.label("当机器人触发转人工时，会把详情推送到钉钉群机器人。请在钉钉里建一个群 → 群设置 → 智能群助手 → 添加机器人 → 自定义机器人 → 复制 Webhook").classes("text-slate-500 mb-2 text-sm")

        webhook_real = form.get("alert_webhook", "")
        webhook_masked = (webhook_real[:35] + "..." + webhook_real[-10:]) if len(webhook_real) > 50 else webhook_real

        ui.input("Webhook 地址", placeholder="https://oapi.dingtalk.com/robot/send?access_token=xxx")\
            .bind_value(form, "alert_webhook").classes("w-full")
        ui.input("加签密钥（仅当机器人安全设置选了'加签'时填）",
                 password=True, password_toggle_button=True,
                 placeholder="留空则使用关键词或 IP 白名单模式")\
            .bind_value(form, "alert_secret").classes("w-full")
        ui.input("对用户回复的话术（{handler} 会替换为对应联系人）",
                 placeholder="这个我帮你转给 {handler}，稍后会联系你哈～")\
            .bind_value(form, "escalate_user_reply").classes("w-full")
        ui.input("投诉关键词（逗号分隔，命中立即转人工 + 紧急告警）",
                 placeholder="投诉,升级,经理,人工,差评,生气")\
            .bind_value(form, "complaint_keywords").classes("w-full")
        ui.input("业务 ID 前缀（逗号分隔，用户消息含『前缀+字母数字』直接转人工）",
                 placeholder="vid-,tid-,task-,order-")\
            .bind_value(form, "escalate_id_prefixes").classes("w-full")
        ui.label("例如配置 vid-，用户发『敏感信息什么情况vid-10ae615ce0174b75』会直接转人工。").classes("text-slate-500 text-xs")
        with ui.row().classes("w-full gap-4 mt-2"):
            ui.number("连续追问几次未解决就强制转人工", min=2, max=10)\
                .bind_value(form, "repeat_threshold").classes("w-72")
        ui.input("管理后台地址（告警里点链接直接跳转到对话详情）",
                 placeholder="http://192.168.95.175:8000")\
            .bind_value(form, "admin_url").classes("w-full")

        def test_alert():
            if not form.get("alert_webhook"):
                ui.notify("请先填 Webhook 地址", type="warning"); return
            # 先保存当前值到内存，让 alert.send_alert 用最新值
            rts.save_to_db({
                "alert_webhook": form["alert_webhook"],
                "alert_secret": form["alert_secret"],
            })
            from ..services import alert as alert_mod
            ok = alert_mod.send_alert(
                level="info", title="测试报警",
                user_text="这是一条测试消息，确认 webhook 工作正常",
                sender_name="后台管理员", sender_id="test",
                bot_reply="（测试）",
                reason="管理员手动测试",
                handler_hint="—",
                at_all=True,    # 测试也 @ 所有人，方便验证提醒到位
            )
            ui.notify("✅ 已推送，去钉钉群看下" if ok else "❌ 推送失败，看 /tmp/qabot.log",
                      type="positive" if ok else "negative")

        def save_alert():
            rts.save_to_db({
                "alert_webhook": form["alert_webhook"],
                "alert_secret": form["alert_secret"],
                "escalate_user_reply": form["escalate_user_reply"],
                "complaint_keywords": form["complaint_keywords"],
                "escalate_id_prefixes": form["escalate_id_prefixes"],
                "repeat_threshold": int(form["repeat_threshold"] or 3),
                "admin_url": form["admin_url"],
            })
            ui.notify("已保存", type="positive")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("发送测试消息", icon="send", on_click=test_alert)
            ui.button("保存告警配置", icon="save", on_click=save_alert).props("color=primary")
