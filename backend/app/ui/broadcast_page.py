"""群发推送管理页 /broadcast。

3 个 tab：群管理 / 推送任务 / 文件库
所有功能仅 admin 可见。
"""
import json
import re
import uuid
from datetime import datetime
from typing import Optional

from nicegui import ui

from ..config import settings
from ..db import SessionLocal, DingtalkGroup, Broadcast, BroadcastSchedule, UploadedFile
from ..services import broadcaster, scheduler as bcast_scheduler
from ..services import uploader
from ..services import direct_pusher
from . import auth_state as A


# 通用 dense + 中文分页 props，所有 broadcast 页表格统一用
_TABLE_PROPS = (
    'dense '
    ':pagination-label="(start, end, total) => `第 ${start}-${end} 条，共 ${total} 条`" '
    'rows-per-page-label="每页显示" '
    ':rows-per-page-options="[10, 20, 50, 100]" '
    'no-data-label="暂无数据" '
    'no-results-label="没有匹配的结果" '
    'loading-label="加载中..."'
)


def _require_admin() -> bool:
    if not A.is_logged_in():
        ui.navigate.to("/login"); return False
    if A.role() != "admin":
        ui.notify("仅管理员可访问群发推送", type="negative")
        ui.navigate.to("/"); return False
    return True


@ui.page("/broadcast")
def page_broadcast():
    if not _require_admin():
        return
    # 复用 pages.py 的布局
    from . import pages as _p
    _p._inject_zh()
    _p._layout("/broadcast")

    # 压缩群发各表格的行高，一屏显示更多内容
    ui.add_head_html("""
    <style>
    body[data-page="broadcast"] .q-table tbody td {
        padding: 4px 10px !important;
        height: auto !important;
        font-size: 12.5px !important;
        line-height: 1.4 !important;
    }
    body[data-page="broadcast"] .q-table thead th {
        padding: 6px 10px !important;
        font-size: 12px !important;
        height: auto !important;
    }
    body[data-page="broadcast"] .q-table .q-btn {
        padding: 1px 6px !important;
        min-height: unset !important;
        font-size: 11.5px !important;
    }
    </style>
    """)
    ui.run_javascript('document.body.setAttribute("data-page","broadcast")')

    with ui.tabs().classes("w-full mt-4") as tabs:
        tab_send = ui.tab("推送任务", icon="campaign")
        tab_direct = ui.tab("私信推送", icon="forward_to_inbox")
        tab_groups = ui.tab("群管理", icon="groups")
        tab_files = ui.tab("文件库", icon="folder")
        tab_history = ui.tab("发送历史", icon="history")

    with ui.tab_panels(tabs, value=tab_send).classes("w-full"):
        with ui.tab_panel(tab_send):
            _render_broadcast_tab()
        with ui.tab_panel(tab_direct):
            _render_direct_tab()
        with ui.tab_panel(tab_groups):
            _render_groups_tab()
        with ui.tab_panel(tab_files):
            _render_files_tab()
        with ui.tab_panel(tab_history):
            _render_history_tab()


# ----------------- 私信推送 -----------------
def _render_direct_tab():
    """给指定用户私信推送（手机号定位，可上传表格批量识别号码）。"""
    with ui.card().classes("w-full bg-amber-50 p-4 mt-2"):
        ui.label("📌 私信推送说明").classes("font-semibold text-amber-800")
        ui.label("1. 在下方填手机号或 staffId（一行一个，或逗号/空格分隔）。")
        ui.label("2. 可上传表格（.xlsx / .csv）批量识别手机号；或点「从聊过的用户选」直接挑。")
        ui.label("3. 手机号定位需开通「根据手机号查询用户」权限；用 staffId / 聊过的用户则只需「机器人发送单聊消息」权限。")
        ui.label("⚠️ 仅能给在机器人可见范围内的用户推送；钉钉对主动推送有每日频控。")\
            .classes("text-slate-600 text-sm mt-1")

    state = {"targets": ""}

    targets_input = ui.textarea(
        "目标手机号 / staffId（一行一个，或逗号/空格分隔）",
    ).classes("w-full").props("rows=6 autogrow")

    def _merge_into_targets(values: list) -> int:
        """把一批 staffId/手机号合并进目标框，去重。返回合并后总数。"""
        existing = re.split(r"[\s,，;；]+", (targets_input.value or "").strip())
        existing = [x for x in existing if x]
        merged = list(dict.fromkeys(existing + [str(v).strip() for v in values if str(v).strip()]))
        targets_input.value = "\n".join(merged)
        return len(merged)

    def _do_upload_sheet(e):
        try:
            data = e.content.read()
            phones = direct_pusher.parse_sheet(data, e.name)
        except Exception as ex:
            ui.notify(f"表格解析失败：{ex}", type="negative", multi_line=True)
            return
        if not phones:
            ui.notify("表格里没识别到手机号", type="warning")
            return
        total = _merge_into_targets(phones)
        ui.notify(f"识别到 {len(phones)} 个手机号，当前共 {total} 个", type="positive")

    def _open_user_picker():
        view = {"mode": "known"}  # known=聊过的用户  excluded=已隐藏
        with ui.dialog() as dlg, ui.card().classes("w-[760px]"):
            header = ui.label().classes("text-lg font-semibold")
            search = ui.input("搜索姓名/staffId").classes("w-full")
            table = ui.table(columns=[], rows=[], row_key="staff_id",
                             selection="multiple").props(
                _TABLE_PROPS + ' :filter="filter"').classes("w-full")
            table.bind_filter_from(search, "value")

            def _reload():
                if view["mode"] == "known":
                    users = direct_pusher.list_known_users()
                    header.text = f"从聊过的用户里选（{len(users)} 人，无需手机号权限）"
                    table.columns = [
                        {"name": "name", "label": "姓名", "field": "name", "align": "left"},
                        {"name": "staff_id", "label": "staffId", "field": "staff_id", "align": "left"},
                        {"name": "msg_count", "label": "消息数", "field": "msg_count"},
                        {"name": "last_at", "label": "最近活跃", "field": "last_at"},
                    ]
                    table.rows = [{
                        "staff_id": u["staff_id"], "name": u["name"],
                        "msg_count": u["msg_count"], "last_at": u["last_at"],
                    } for u in users]
                else:
                    ex = direct_pusher.list_excluded()
                    header.text = f"已隐藏用户（{len(ex)} 人，不会出现在选人列表）"
                    table.columns = [
                        {"name": "name", "label": "姓名", "field": "name", "align": "left"},
                        {"name": "staff_id", "label": "staffId", "field": "staff_id", "align": "left"},
                        {"name": "reason", "label": "原因", "field": "reason", "align": "left"},
                        {"name": "at", "label": "隐藏时间", "field": "at"},
                    ]
                    table.rows = [{
                        "staff_id": r["staff_id"], "name": r["name"],
                        "reason": r["reason"], "at": r["at"],
                    } for r in ex]
                table.selected = []
                table.update()
                actions.refresh()
                toggle_btn.text = "查看聊过的用户" if view["mode"] == "excluded" else "查看已隐藏"
                toggle_btn.update()

            def _add_to_targets():
                picked = [r["staff_id"] for r in table.selected]
                if not picked:
                    ui.notify("请先勾选用户", type="warning"); return
                total = _merge_into_targets(picked)
                ui.notify(f"已添加 {len(picked)} 人，当前共 {total} 个目标", type="positive")
                dlg.close()

            def _hide_selected():
                picked = {r["staff_id"]: r.get("name", "") for r in table.selected}
                if not picked:
                    ui.notify("请先勾选要隐藏的用户", type="warning"); return
                n = direct_pusher.hide_users(
                    list(picked.keys()), reason="手动隐藏",
                    by=A.user().get("username", "admin"), names=picked)
                ui.notify(f"已隐藏 {n} 人，不再出现在选人列表", type="positive")
                _reload()

            def _restore_selected():
                picked = [r["staff_id"] for r in table.selected]
                if not picked:
                    ui.notify("请先勾选要恢复的用户", type="warning"); return
                n = direct_pusher.unhide_users(picked)
                ui.notify(f"已恢复 {n} 人", type="positive")
                _reload()

            def _toggle():
                view["mode"] = "excluded" if view["mode"] == "known" else "known"
                _reload()

            with ui.row().classes("w-full justify-between items-center mt-2"):
                toggle_btn = ui.button("查看已隐藏", icon="visibility_off",
                                       on_click=_toggle).props("flat color=grey-7")

                @ui.refreshable
                def actions():
                    with ui.row().classes("gap-2"):
                        ui.button("取消", on_click=dlg.close).props("flat")
                        if view["mode"] == "known":
                            ui.button("隐藏所选", icon="visibility_off",
                                      on_click=_hide_selected).props("flat color=negative")
                            ui.button("加入目标", icon="check",
                                      on_click=_add_to_targets).props("color=primary")
                        else:
                            ui.button("恢复所选", icon="restore",
                                      on_click=_restore_selected).props("color=primary")
                actions()

            _reload()
        dlg.open()

    def _open_contacts():
        """可复用联系人名单（各位老师）：增/删/选/从聊过的用户导入。"""
        with ui.dialog() as dlg, ui.card().classes("w-[820px]"):
            header = ui.label().classes("text-lg font-semibold")
            search = ui.input("搜索姓名/staffId").classes("w-full")
            table = ui.table(
                columns=[
                    {"name": "name", "label": "姓名", "field": "name", "align": "left"},
                    {"name": "staff_id", "label": "staffId", "field": "staff_id", "align": "left"},
                    {"name": "tag", "label": "分组", "field": "tag", "align": "left"},
                    {"name": "at", "label": "添加时间", "field": "at"},
                ],
                rows=[], row_key="staff_id", selection="multiple",
            ).props(_TABLE_PROPS + ' :filter="filter"').classes("w-full")
            table.bind_filter_from(search, "value")

            def _reload():
                contacts = direct_pusher.list_contacts()
                header.text = f"联系人名单（{len(contacts)} 人，发送时勾选加入目标）"
                table.rows = [{
                    "staff_id": c["staff_id"], "name": c["name"],
                    "tag": c["tag"], "at": c["at"],
                } for c in contacts]
                table.selected = []
                table.update()

            # ── 手动新增一行 ──
            with ui.row().classes("w-full gap-2 items-end mt-1"):
                new_name = ui.input("姓名").classes("w-40")
                new_sid = ui.input("staffId").classes("flex-grow")
                new_tag = ui.input("分组(可空)").classes("w-32")

                def _add_one():
                    name = (new_name.value or "").strip()
                    sid = (new_sid.value or "").strip()
                    if not name or not sid:
                        ui.notify("姓名和 staffId 都要填", type="warning"); return
                    direct_pusher.add_contacts(
                        [{"staff_id": sid, "name": name, "tag": (new_tag.value or "").strip()}],
                        by=A.user().get("username", "admin"))
                    new_name.value = ""; new_sid.value = ""; new_tag.value = ""
                    ui.notify("已添加", type="positive")
                    _reload()
                ui.button("添加", icon="add", on_click=_add_one).props("color=primary")

            def _import_from_known():
                """把当前已存的『聊过的用户』批量导入名单（取姓名+staffId）。"""
                users = direct_pusher.list_known_users()
                if not users:
                    ui.notify("没有可导入的聊过的用户", type="warning"); return
                n = direct_pusher.add_contacts(
                    [{"staff_id": u["staff_id"], "name": u["name"]} for u in users],
                    by=A.user().get("username", "admin"))
                ui.notify(f"已从聊过的用户导入/更新 {n} 人", type="positive")
                _reload()

            def _add_to_targets():
                picked = [r["staff_id"] for r in table.selected]
                if not picked:
                    ui.notify("请先勾选联系人", type="warning"); return
                total = _merge_into_targets(picked)
                ui.notify(f"已添加 {len(picked)} 人，当前共 {total} 个目标", type="positive")
                dlg.close()

            def _delete_selected():
                picked = [r["staff_id"] for r in table.selected]
                if not picked:
                    ui.notify("请先勾选要删除的联系人", type="warning"); return
                n = direct_pusher.remove_contacts(picked)
                ui.notify(f"已从名单删除 {n} 人", type="positive")
                _reload()

            with ui.row().classes("w-full justify-between items-center mt-2"):
                ui.button("从聊过的用户导入", icon="download",
                          on_click=_import_from_known).props("flat color=grey-7")
                with ui.row().classes("gap-2"):
                    ui.button("取消", on_click=dlg.close).props("flat")
                    ui.button("删除所选", icon="delete",
                              on_click=_delete_selected).props("flat color=negative")
                    ui.button("加入目标", icon="check",
                              on_click=_add_to_targets).props("color=primary")

            _reload()
        dlg.open()

    with ui.row().classes("w-full mt-2 gap-4 items-center"):
        ui.upload(on_upload=_do_upload_sheet, auto_upload=True, max_files=1)\
            .props('accept=".xlsx,.csv,.txt"').classes("w-96")
        ui.button("从聊过的用户选", icon="people",
                  on_click=_open_user_picker).props("color=positive")
        ui.button("联系人名单", icon="contacts",
                  on_click=_open_contacts).props("color=primary")
        ui.label("支持 .xlsx / .csv / .txt").classes("text-slate-500 text-sm")

    msg_type = ui.radio({"text": "纯文本", "markdown": "Markdown"}, value="text")\
        .props("inline").classes("mt-2")
    content_input = ui.textarea("推送内容").classes("w-full").props("rows=6 clearable")

    def _open_link_dialog():
        """Markdown 模式下插入钉钉文档/网页链接，自动拼成 [标题](URL) 追加到内容末尾。"""
        with ui.dialog() as dlg, ui.card().classes("w-[520px]"):
            ui.label("插入链接").classes("text-lg font-semibold")
            ui.label("会自动拼成 [标题](URL) 追加到推送内容末尾。")\
                .classes("text-slate-500 text-sm")
            title_in = ui.input("链接标题（显示给用户看的文字）")\
                .classes("w-full")
            url_in = ui.input("链接 URL",
                              placeholder="https://alidocs.dingtalk.com/i/xxxxxxx")\
                .classes("w-full")

            def _insert():
                title = (title_in.value or "").strip()
                url = (url_in.value or "").strip()
                if not title or not url:
                    ui.notify("标题和 URL 都要填", type="warning"); return
                if not (url.startswith("http://") or url.startswith("https://")):
                    ui.notify("URL 必须以 http:// 或 https:// 开头", type="warning"); return
                snippet = f"[{title}]({url})"
                cur = (content_input.value or "").rstrip()
                content_input.value = (cur + ("\n" if cur else "") + snippet).strip() + "\n"
                ui.notify("已插入到推送内容", type="positive")
                dlg.close()

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("取消", on_click=dlg.close).props("flat")
                ui.button("插入", icon="link",
                          on_click=_insert).props("color=primary")
        dlg.open()

    with ui.row().classes("w-full mt-1 gap-2 items-center"):
        link_btn = ui.button("插入链接", icon="link",
                             on_click=_open_link_dialog).props("flat color=primary")
        ui.label("（仅 Markdown 模式可点击成超链接；纯文本下用户看到的是原始 URL）")\
            .classes("text-slate-500 text-sm")
        link_btn.bind_visibility_from(msg_type, "value", lambda v: v == "markdown")

    def _parse_targets() -> list:
        raw = targets_input.value or ""
        items = re.split(r"[\s,，;；]+", raw.strip())
        return [x for x in (i.strip() for i in items) if x]

    async def _send():
        targets = _parse_targets()
        if not targets:
            ui.notify("请先填写、上传或从聊过的用户里选目标", type="warning"); return
        if not (content_input.value or "").strip():
            ui.notify("请填写推送内容", type="warning"); return
        # 异步排队：立即返回 push_id，进度在下方历史里看
        from nicegui import run
        result = await run.io_bound(
            direct_pusher.start_push_async, targets,
            content_input.value, msg_type.value,
            A.user().get("username", "admin"))
        if not result.get("ok"):
            ui.notify(f"提交失败：{result.get('error')}", type="negative", multi_line=True)
            return
        ui.notify(
            f"已排队 {len(targets)} 个目标（任务 #{result['push_id']}），进度见下方历史。",
            type="positive", multi_line=True)
        # 清空内容，避免误重发；目标框保留，方便用户确认
        content_input.value = ""
        history.refresh()

    with ui.row().classes("w-full justify-end gap-2 mt-3"):
        ui.button("发送私信（后台运行）", icon="send",
                  on_click=_send).props("color=primary")

    ui.separator().classes("mt-4")
    ui.label("推送历史").classes("text-lg font-semibold mt-2")

    @ui.refreshable
    def history():
        from ..db import SessionLocal, DirectPush
        db = SessionLocal()
        try:
            rows = db.query(DirectPush).order_by(DirectPush.id.desc()).limit(50).all()
            data = [{
                "id": r.id,
                "time": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
                "content": (r.content_text or "")[:40],
                "type": "Markdown" if r.msg_type == "markdown" else "文本",
                "progress": (f"{r.sent_count}/{r.user_count or r.target_count}"
                             if r.user_count else f"0/{r.target_count}"),
                "status": r.status or "",
                "summary": r.result_summary or "",
                "by": r.created_by or "",
                "_raw_status": r.status,
                "can_retry": r.status in ("partial", "failed")
                             and bool((r.failed_userids and r.failed_userids != "[]")
                                      or (r.unresolved and r.unresolved != "[]")),
            } for r in rows]
        finally:
            db.close()
        table = ui.table(
            columns=[
                {"name": "id", "label": "#", "field": "id", "style": "width: 50px"},
                {"name": "time", "label": "时间", "field": "time"},
                {"name": "content", "label": "内容", "field": "content", "align": "left"},
                {"name": "type", "label": "类型", "field": "type"},
                {"name": "progress", "label": "进度", "field": "progress"},
                {"name": "status", "label": "状态", "field": "status"},
                {"name": "summary", "label": "摘要", "field": "summary", "align": "left"},
                {"name": "by", "label": "操作人", "field": "by"},
                {"name": "action", "label": "操作", "field": "id", "style": "width: 110px"},
            ],
            rows=data, row_key="id",
        ).props(_TABLE_PROPS).classes("w-full mt-2")
        # 状态徽标
        table.add_slot("body-cell-status", r"""
            <q-td :props="props">
                <q-badge v-if="props.row._raw_status==='sent'" color="green" label="全部成功"/>
                <q-badge v-else-if="props.row._raw_status==='partial'" color="orange" label="部分成功"/>
                <q-badge v-else-if="props.row._raw_status==='failed'" color="red" label="失败"/>
                <q-badge v-else-if="props.row._raw_status==='running'" color="blue" label="● 运行中"/>
                <q-badge v-else-if="props.row._raw_status==='pending'" color="grey" label="○ 排队中"/>
                <q-badge v-else :label="props.row._raw_status"/>
            </q-td>
        """)
        table.add_slot("body-cell-action", r"""
            <q-td :props="props">
                <q-btn v-if="props.row.can_retry" dense flat label="重试失败"
                       color="primary" @click="$parent.$emit('retry', props.row)"/>
            </q-td>
        """)

        def _on_retry(e):
            row = e.args
            res = direct_pusher.retry_push(int(row["id"]))
            if res.get("ok"):
                ui.notify(f"已排队重试（新任务 #{res['push_id']}）",
                          type="positive", multi_line=True)
                history.refresh()
            else:
                ui.notify(f"重试失败：{res.get('error')}", type="negative")
        table.on("retry", _on_retry)

        # 有 running/pending 行时自动每 3s 刷新一次，全跑完后停
        if any(r["_raw_status"] in ("running", "pending") for r in data):
            ui.timer(3.0, history.refresh, once=True)

    history()


# ----------------- 群管理 -----------------
def _render_groups_tab():
    with ui.card().classes("w-full bg-blue-50 p-4 mt-2"):
        if settings.qa_workspace_mode:
            ui.label("🧪 测试群 Webhook").classes("font-semibold text-blue-800")
            ui.label("正式群只作为快照展示，已全部禁用并清除 Webhook。测试时请新建一个测试群 Webhook。")
            ui.label("只需要群名、钉钉自定义机器人 Webhook 和 SEC 加签密钥，不需要 openConversationId。")\
                .classes("text-slate-600 text-sm mt-1")
        else:
            ui.label("📌 如何让群出现在这里").classes("font-semibold text-blue-800")
            ui.label("1. 群主在钉钉群里 → 群设置 → 智能群助手 → 添加机器人 → 选择本企业机器人")
            ui.label("2. 添加完成后，群里随便发一条消息（@ 一下机器人最好）")
            ui.label("3. 回到这个页面，点【刷新】，群会自动出现")
            ui.label("💡 不需要手动填 openConversationId。手动添加仅在自动机制失效时使用，"
                     "且需要从钉钉开放平台 API 取，普通用户看不到。").classes("text-slate-600 text-sm mt-1")

    table = ui.table(
        columns=[
            {"name": "id", "label": "ID", "field": "id", "style": "width: 60px"},
            {"name": "title", "label": "群名", "field": "title", "align": "left", "style": "min-width: 200px"},
            {"name": "conv_id", "label": "群唯一ID", "field": "conv_id", "style": "width: 280px; white-space: nowrap"},
            {"name": "active", "label": "启用", "field": "active", "style": "width: 70px"},
            {"name": "atall", "label": "@全体", "field": "atall", "style": "width: 80px"},
            {"name": "added_at", "label": "加入时间", "field": "added_at", "style": "width: 160px; white-space: nowrap"},
            {"name": "note", "label": "备注", "field": "note", "align": "left", "style": "min-width: 160px"},
            {"name": "action", "label": "操作", "field": "id", "style": "width: 220px"},
        ],
        rows=[], row_key="id",
        pagination=10,
    ).classes("w-full mt-2").props(_TABLE_PROPS)

    def load():
        db = SessionLocal()
        try:
            rows = db.query(DingtalkGroup).order_by(DingtalkGroup.id.desc()).all()
            data = [{
                "id": r.id,
                "title": r.conversation_title or "(未命名)",
                "conv_id": r.open_conversation_id,
                "active": "是" if r.active == "1" else "否",
                "atall": "✅已配" if (getattr(r, "webhook_url", "") or "").strip() else "—",
                "added_at": r.added_at.strftime("%Y-%m-%d %H:%M") if r.added_at else "",
                "note": r.note or "",
            } for r in rows]
        finally:
            db.close()
        table.rows = data
        table.update()

    table.add_slot("body-cell-active", r"""
        <q-td :props="props">
            <q-badge v-if="props.row.active==='是'" color="green" label="● 启用"/>
            <q-badge v-else color="grey-5" label="○ 禁用"/>
        </q-td>
    """)
    table.add_slot("body-cell-action", r"""
        <q-td :props="props">
            <q-btn dense flat label="启用/禁用" color="warning" @click="$parent.$emit('toggle', props.row)"/>
            <q-btn dense flat label="@全体" color="negative" @click="$parent.$emit('atall', props.row)"/>
            <q-btn dense flat label="备注" color="primary" @click="$parent.$emit('note', props.row)"/>
            <q-btn dense flat label="删除" color="negative" @click="$parent.$emit('remove', props.row)"/>
        </q-td>
    """)

    def toggle_active(row):
        db = SessionLocal()
        try:
            r = db.get(DingtalkGroup, row["id"])
            if r:
                r.active = "0" if r.active == "1" else "1"
                new = r.active
                db.commit()
        finally:
            db.close()
        ui.notify(f'群「{row["title"]}」已{"启用（可群发）" if new=="1" else "禁用（不在目标群列表）"}', type="positive")
        load()

    def edit_atall(row):
        """配置该群的 @全体 webhook。"""
        db = SessionLocal()
        try:
            r = db.get(DingtalkGroup, row["id"])
            cur_url = (r.webhook_url or "") if r else ""
            from ..services import crypto
            try:
                cur_secret = crypto.decrypt(r.webhook_secret or "") if r else ""
            except crypto.DecryptError:
                cur_secret = ""  # master key 变更，留空让管理员重填
        finally:
            db.close()
        with ui.dialog() as dlg, ui.card().classes("w-[560px]"):
            ui.label(f'群「{row["title"]}」配置 @全体').classes("text-lg font-semibold")
            ui.separator()
            with ui.column().classes("gap-2 mt-2 w-full"):
                ui.label("原理：钉钉只有「自定义机器人 webhook + text」能 @全体。").classes("text-slate-600 text-sm")
                ui.label("配好后，本群群发时会先发一条「文案 + @所有人」纯文字，再正常发图文。").classes("text-slate-600 text-sm")
                ui.label("如何获取：进该钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义 → 安全设置选「加签」→ 复制 Webhook 和 SEC 开头的密钥。").classes("text-orange-600 text-xs")
                url_in = ui.input("Webhook 地址", value=cur_url,
                                  placeholder="https://oapi.dingtalk.com/robot/send?access_token=xxx").classes("w-full")
                secret_in = ui.input("加签密钥（SEC 开头）", value=cur_secret,
                                     placeholder="SECxxxxxxxx").classes("w-full")
            with ui.row().classes("w-full justify-between gap-2 mt-3"):
                ui.button("清除配置", on_click=lambda: _clear_atall(row["id"], dlg, load)).props("flat color=grey")
                with ui.row().classes("gap-2"):
                    ui.button("测试发送", on_click=lambda: _test_atall(url_in.value, secret_in.value)).props("outline color=primary")
                    ui.button("取消", on_click=dlg.close).props("flat")
                    ui.button("保存", on_click=lambda: _save_atall(row["id"], url_in.value, secret_in.value, dlg, load)).props("color=primary")
        dlg.open()

    def edit_note(row):
        with ui.dialog() as dlg, ui.card().classes("w-96"):
            ui.label(f'群「{row["title"]}」备注').classes("text-lg font-semibold")
            note_in = ui.input("备注", value=row.get("note", "")).classes("w-full")
            def save():
                db = SessionLocal()
                try:
                    r = db.get(DingtalkGroup, row["id"])
                    if r:
                        r.note = (note_in.value or "")[:255]
                        db.commit()
                finally:
                    db.close()
                ui.notify("已保存", type="positive"); dlg.close(); load()
            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("取消", on_click=dlg.close)
                ui.button("保存", on_click=save).props("color=primary")
        dlg.open()

    def delete_group(row):
        with ui.dialog() as dlg, ui.card().classes("w-[460px]"):
            ui.label("⚠️ 确认删除这个群？").classes("text-lg font-semibold text-red-600")
            ui.separator()
            with ui.column().classes("gap-1 mt-2"):
                ui.label(f'群名：{row["title"]}').classes("text-sm")
                ui.label(f'群ID：{row["conv_id"]}').classes("text-xs text-slate-500 font-mono")
                ui.label("删除后该群不再出现在群发列表里。").classes("text-slate-600 text-sm mt-1")
                ui.label("如果机器人还在群里、群里再发消息，会被自动重新入库。").classes("text-orange-600 text-xs")
            def do_remove():
                db = SessionLocal()
                try:
                    r = db.get(DingtalkGroup, row["id"])
                    if r:
                        db.delete(r)
                        db.commit()
                finally:
                    db.close()
                ui.notify(f'已删除群「{row["title"]}」', type="positive")
                dlg.close(); load()
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("取消", on_click=dlg.close).props("flat")
                ui.button("确认删除", on_click=do_remove, color="negative")
        dlg.open()

    table.on("toggle", lambda e: toggle_active(e.args))
    table.on("atall", lambda e: edit_atall(e.args))
    table.on("note", lambda e: edit_note(e.args))
    table.on("remove", lambda e: delete_group(e.args))

    with ui.row().classes("w-full justify-between items-center mt-2"):
        ui.label("机器人被拉入群后自动入库 · 也可手动添加").classes("text-slate-500 text-sm")
        with ui.row().classes("gap-2"):
            ui.button("刷新", icon="refresh", on_click=load)
            if settings.qa_workspace_mode:
                ui.button(
                    "新建测试群 Webhook", icon="add_link",
                    on_click=lambda: _open_test_webhook_group_dialog(load),
                ).props("color=positive")
            ui.button("手动添加群", icon="add", on_click=lambda: _open_manual_add_group_dialog(load)).props("color=positive")

    load()


def _open_test_webhook_group_dialog(refresh_cb):
    """测试环境直接用自定义机器人 Webhook 建群，不依赖正式应用机器人。"""
    if not settings.qa_workspace_mode:
        ui.notify("该入口只允许在测试环境使用", type="negative")
        return
    with ui.dialog() as dlg, ui.card().classes("w-[620px]"):
        ui.label("新建测试群 Webhook").classes("text-lg font-semibold")
        ui.label("请只填写专门用于测试的群。正式群 Webhook 禁止放进测试环境。")\
            .classes("text-red-600 text-sm")
        title_in = ui.input("测试群名称", placeholder="例如：佐伊机器人测试群").classes("w-full")
        url_in = ui.input(
            "Webhook 地址", placeholder="https://oapi.dingtalk.com/robot/send?access_token=xxx",
        ).classes("w-full")
        secret_in = ui.input(
            "加签密钥（SEC 开头，可选）", placeholder="SECxxxxxxxx",
        ).classes("w-full")

        def save(test_after_save: bool = False):
            title = (title_in.value or "").strip()
            url = (url_in.value or "").strip()
            secret = (secret_in.value or "").strip()
            if not title:
                ui.notify("请填写测试群名称", type="warning")
                return
            if not url.startswith("https://oapi.dingtalk.com/robot/send"):
                ui.notify("Webhook 必须以 https://oapi.dingtalk.com/robot/send 开头", type="negative")
                return
            from ..services import crypto
            db = SessionLocal()
            try:
                exists = db.query(DingtalkGroup).filter(DingtalkGroup.webhook_url == url).first()
                if exists:
                    ui.notify(f"这个 Webhook 已配置在群「{exists.conversation_title}」", type="warning")
                    return
                db.add(DingtalkGroup(
                    open_conversation_id=f"test-webhook-{uuid.uuid4().hex}",
                    conversation_title=title[:255],
                    active="1",
                    note="TEST ONLY · 测试环境 Webhook 群",
                    webhook_url=url[:500],
                    webhook_secret=crypto.encrypt(secret) if secret else None,
                ))
                db.commit()
            finally:
                db.close()
            dlg.close()
            refresh_cb()
            ui.notify("测试群 Webhook 已保存", type="positive")
            if test_after_save:
                _test_atall(url, secret)

        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            ui.button("取消", on_click=dlg.close).props("flat")
            ui.button("只保存", on_click=lambda: save(False)).props("outline color=primary")
            ui.button("保存并测试发送", on_click=lambda: save(True)).props("color=primary")
    dlg.open()


def _save_atall(group_id: int, url: str, secret: str, dlg, refresh_cb):
    """保存群的 @全体 webhook 配置。"""
    url = (url or "").strip()
    secret = (secret or "").strip()
    if url and not url.startswith("https://oapi.dingtalk.com/robot/send"):
        ui.notify("Webhook 地址必须是 https://oapi.dingtalk.com/robot/send 开头", type="negative")
        return
    from ..services import crypto
    db = SessionLocal()
    try:
        r = db.get(DingtalkGroup, group_id)
        if r:
            r.webhook_url = url[:500] if url else None
            # 加签密钥静态加密入库（拿到库即可伪造该群群发）。crypto.decrypt 对无前缀的
            # 老明文值会原样返回，迁移无缝。
            r.webhook_secret = crypto.encrypt(secret) if secret else None
            db.commit()
    finally:
        db.close()
    ui.notify("已保存 @全体 配置" if url else "已清除 @全体 配置", type="positive")
    dlg.close()
    refresh_cb()


def _clear_atall(group_id: int, dlg, refresh_cb):
    db = SessionLocal()
    try:
        r = db.get(DingtalkGroup, group_id)
        if r:
            r.webhook_url = None
            r.webhook_secret = None
            db.commit()
    finally:
        db.close()
    ui.notify("已清除 @全体 配置", type="positive")
    dlg.close()
    refresh_cb()


def _test_atall(url: str, secret: str):
    """测试发送一条 @全体 消息到该群。"""
    from ..services import broadcaster
    url = (url or "").strip()
    if not url:
        ui.notify("请先填 Webhook 地址", type="warning")
        return
    res = broadcaster._send_webhook_at_all(url, (secret or "").strip(),
                                           "【测试】@全体配置测试，收到说明配置成功")
    if res.get("ok"):
        ui.notify("✅ 测试发送成功，去群里看是否 @ 到全体", type="positive")
    else:
        detail = res.get("response") or res.get("error") or "未知错误"
        ui.notify(f"❌ 测试失败：{detail}", type="negative", multi_line=True)


def _open_manual_add_group_dialog(refresh_cb):
    """手动添加群（当机器人入群事件没捕获到时用，需要群主提供 open_conversation_id）"""
    with ui.dialog() as dlg, ui.card().classes("w-[640px]"):
        ui.label("手动添加群").classes("text-lg font-semibold")
        ui.label("⚠️ 推荐做法：直接让群主把本机器人拉进群，群里发条消息，机器人会自动出现在列表里。"
                 "**不需要手动填 ID**。").classes("text-orange-600 text-sm")
        with ui.expansion("🤔 那 openConversationId 是什么？什么时候需要手动填？", icon="help_outline").classes("w-full mt-1"):
            ui.label("openConversationId 是钉钉自建应用 API 才能获取的群唯一标识，"
                     "形如 \"cid???xxx==\"（一般是几十个字符的加密字符串）。"
                     "**不是群的 webhook URL！不是群名！普通用户在客户端看不到。**").classes("text-slate-600 text-sm")
            ui.label("常见错误：填成 \"https://oapi.dingtalk.com/robot/send?access_token=xxx\""
                     "（这是群自定义机器人 webhook，本系统不用这种）。").classes("text-red-600 text-sm")
            ui.label("仅在自动入群机制失效时才需要手动填，且 ID 必须从开放平台 API 取。").classes("text-slate-500 text-xs")
        title_in = ui.input("群名").classes("w-full")
        conv_in = ui.input("openConversationId（必须是 cid 开头的加密字符串，不是 webhook URL）")\
            .classes("w-full")
        note_in = ui.input("备注（可选）").classes("w-full")
        def save():
            t = (title_in.value or "").strip()
            c = (conv_in.value or "").strip()
            if not t or not c:
                ui.notify("群名和 ID 必填", type="warning"); return
            # 防呆校验：拒绝明显错的输入
            low = c.lower()
            if low.startswith("http://") or low.startswith("https://"):
                ui.notify("❌ 你填的是 URL，不是 openConversationId。请重新看上方说明。",
                          type="negative", multi_line=True); return
            if "access_token=" in low or "/robot/send" in low:
                ui.notify("❌ 这是群自定义机器人 webhook，不是 openConversationId。本系统不用这种。",
                          type="negative", multi_line=True); return
            if len(c) < 10:
                ui.notify("openConversationId 长度太短，应该是几十个字符的加密字符串", type="warning"); return
            db = SessionLocal()
            try:
                exists = db.query(DingtalkGroup).filter(DingtalkGroup.open_conversation_id == c).first()
                if exists:
                    ui.notify("该群已存在", type="warning"); return
                db.add(DingtalkGroup(
                    open_conversation_id=c,
                    conversation_title=t[:255],
                    note=(note_in.value or "")[:255] or None,
                    active="1",
                ))
                db.commit()
            finally:
                db.close()
            ui.notify("已添加", type="positive"); dlg.close(); refresh_cb()
        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("取消", on_click=dlg.close)
            ui.button("保存", on_click=save).props("color=primary")
    dlg.open()


# ----------------- 文件库 -----------------
def _render_files_tab():
    ui.label("上传图片/视频后，系统会生成可访问的链接，群发时直接选用。").classes("text-slate-500 text-sm")
    ui.label("⚠️ 必须在「系统设置」配置「文件访问基址」（公网域名/IP），否则钉钉拉不到文件。").classes("text-orange-600 text-sm")

    table = ui.table(
        columns=[
            {"name": "id", "label": "ID", "field": "id", "style": "width: 60px"},
            {"name": "original_name", "label": "原始文件名", "field": "original_name", "align": "left", "style": "min-width: 200px"},
            {"name": "file_type", "label": "类型", "field": "file_type", "style": "width: 80px"},
            {"name": "size", "label": "大小", "field": "size", "style": "width: 100px"},
            {"name": "public_url", "label": "访问链接", "field": "public_url", "align": "left", "style": "min-width: 280px; word-break: break-all"},
            {"name": "uploaded_by", "label": "上传者", "field": "uploaded_by", "style": "width: 100px"},
            {"name": "created_at", "label": "时间", "field": "created_at", "style": "width: 160px; white-space: nowrap"},
            {"name": "action", "label": "操作", "field": "id", "style": "width: 200px"},
        ],
        rows=[], row_key="id",
        pagination=10,
    ).classes("w-full mt-2").props(_TABLE_PROPS)

    def fmt_size(n):
        if n is None: return ""
        for u in ["B", "KB", "MB", "GB"]:
            if n < 1024:
                return f"{n:.1f}{u}"
            n = n / 1024
        return f"{n:.1f}TB"

    def load():
        rows = uploader.list_files()
        for r in rows:
            r["size"] = fmt_size(r["size_bytes"])
        table.rows = rows
        table.update()

    table.add_slot("body-cell-action", r"""
        <q-td :props="props">
            <q-btn dense flat label="重命名" color="primary" @click="$parent.$emit('rename', props.row)"/>
            <q-btn dense flat label="复制链接" color="primary" @click="$parent.$emit('copy', props.row)"/>
            <q-btn dense flat label="删除" color="negative" @click="$parent.$emit('remove', props.row)"/>
        </q-td>
    """)

    def copy_url(row):
        url = row.get("public_url", "")
        ui.run_javascript(f'navigator.clipboard.writeText({json.dumps(url)})')
        ui.notify("已复制到剪贴板", type="positive")

    def remove_file(row):
        uploader.delete_file(row["id"])
        ui.notify(f'已删除「{row["original_name"]}」', type="positive")
        load()

    def rename_file(row):
        with ui.dialog() as dlg, ui.card().classes("w-[480px]"):
            ui.label("重命名文件").classes("text-lg font-semibold")
            ui.label(f'当前名称: {row.get("original_name", "")}').classes("text-slate-500 text-sm")
            ui.label("修改后只影响显示，不动磁盘文件名和访问链接。").classes("text-slate-500 text-xs")
            name_in = ui.input("新名称", value=row.get("original_name", "")).classes("w-full")
            def save():
                res = uploader.rename_file(row["id"], name_in.value or "")
                if res.get("ok"):
                    ui.notify("已更新", type="positive"); dlg.close(); load()
                else:
                    ui.notify(f'失败：{res.get("error")}', type="negative")
            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("取消", on_click=dlg.close)
                ui.button("保存", on_click=save).props("color=primary")
        dlg.open()

    table.on("copy", lambda e: copy_url(e.args))
    table.on("remove", lambda e: remove_file(e.args))
    table.on("rename", lambda e: rename_file(e.args))

    def do_upload(e):
        try:
            content = e.content.read()
            res = uploader.save_upload(content, e.name, A.user().get("username", "admin"))
            ui.notify(f'上传成功：{res["public_url"]}', type="positive", multi_line=True)
            load()
        except uploader.UploadError as ex:
            ui.notify(f"上传失败：{ex}", type="negative", multi_line=True)
        except Exception as ex:
            ui.notify(f"上传异常：{ex}", type="negative", multi_line=True)

    with ui.row().classes("w-full mt-2 gap-4 items-center"):
        ui.upload(on_upload=do_upload, auto_upload=True, max_files=1).props('accept="image/*,video/*"').classes("w-96")
        ui.button("手动添加外部链接", icon="link",
                  on_click=lambda: _open_external_url_dialog(load)).props("color=positive")
        ui.button("刷新", icon="refresh", on_click=load)

    load()


def _open_external_url_dialog(refresh_cb):
    """手动添加外部 URL 作为视频/图片资源（不下载，仅登记）。
    适用于：视频/图片本来就在公司 OSS / 钉钉云盘 / 公网 CDN，已有公网链接。
    """
    with ui.dialog() as dlg, ui.card().classes("w-[640px]"):
        ui.label("手动添加外部链接").classes("text-lg font-semibold")
        ui.label("适用于：你的视频/图片已经在公司 OSS、钉钉云盘、公网 CDN 等地方，"
                 "有现成的公网链接，直接登记进文件库使用。").classes("text-slate-500 text-sm")
        ui.label("⚠️ 必须是钉钉服务器能访问的公网 URL（http:// 或 https:// 开头）。").classes("text-orange-600 text-xs mt-1")

        ftype_in = ui.radio({"video": "视频", "image": "图片"}, value="video").props("inline")
        url_in = ui.input("公网 URL", placeholder="https://example.com/path/to/video.mp4")\
            .classes("w-full")
        name_in = ui.input("显示名称（可选，留空用 URL 末尾段）",
                           placeholder="例如：演示视频-2026版").classes("w-full")

        def save():
            res = uploader.register_external_url(
                url_in.value or "",
                name_in.value or "",
                ftype_in.value,
                A.user().get("username", "admin"),
            )
            if res.get("ok"):
                ui.notify(f'已添加，URL: {res["public_url"]}', type="positive", multi_line=True)
                dlg.close()
                refresh_cb()
            else:
                ui.notify(f'失败：{res.get("error")}', type="negative", multi_line=True)

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("取消", on_click=dlg.close)
            ui.button("保存", on_click=save).props("color=primary")
    dlg.open()


# ----------------- 推送任务 -----------------
def _render_broadcast_tab():
    state = {"editing_id": None}
    form = {
        "title": "",
        "content_text": "",
        "image_urls": [],   # 多图链接列表（最多 9 张）
        "video_title": "",
        "video_cover_url": "",
        "video_link": "",
        "target_group_ids": [],
        "schedule_mode": "immediate",   # immediate / once / daily / weekly / monthly / cron
        "once_dt": "",
        "daily_time": "09:00",
        "weekly_time": "09:00",
        "weekly_days": [],
        "monthly_time": "09:00",
        "monthly_day": 1,
        "cron_expr": "",
    }

    list_table = None
    file_options_cache = {"images": [], "videos": []}

    def refresh_files():
        files = uploader.list_files()
        file_options_cache["images"] = [(f, f"[{f['original_name']}] {f['public_url']}") for f in files if f["file_type"] == "image"]
        file_options_cache["videos"] = [(f, f"[{f['original_name']}] {f['public_url']}") for f in files if f["file_type"] == "video"]

    def get_groups_options():
        db = SessionLocal()
        try:
            rows = db.query(DingtalkGroup).filter(DingtalkGroup.active == "1").all()
            return {r.id: r.conversation_title or f"(群 #{r.id})" for r in rows}
        finally:
            db.close()

    def load_broadcasts():
        db = SessionLocal()
        try:
            rows = db.query(Broadcast).order_by(Broadcast.id.desc()).limit(100).all()
            data = []
            for r in rows:
                try:
                    gids = json.loads(r.target_group_ids or "[]")
                except Exception:
                    gids = []
                data.append({
                    "id": r.id,
                    "title": r.title,
                    "status": r.status,
                    "groups": f"{len(gids)} 个群",
                    "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
                    "last_sent": r.last_sent_at.strftime("%Y-%m-%d %H:%M") if r.last_sent_at else "—",
                })
        finally:
            db.close()
        list_table.rows = data
        list_table.update()

    def reset_form():
        state["editing_id"] = None
        form["title"] = ""
        form["content_text"] = ""
        form["image_urls"] = []
        form["video_title"] = ""
        form["video_cover_url"] = ""
        form["video_link"] = ""
        form["target_group_ids"] = []
        form["schedule_mode"] = "immediate"
        form["once_dt"] = ""

    def save_broadcast() -> Optional[int]:
        if not (form["title"] or "").strip():
            ui.notify("请填写推送标题", type="warning"); return None
        if not form["target_group_ids"]:
            ui.notify("请至少选一个目标群", type="warning"); return None
        db = SessionLocal()
        try:
            if state["editing_id"]:
                b = db.get(Broadcast, state["editing_id"])
                if not b:
                    ui.notify("找不到推送记录", type="negative"); return None
            else:
                b = Broadcast(created_by=A.user().get("username", "admin"))
                db.add(b)
            b.title = form["title"][:255]
            b.content_text = form["content_text"]
            # 多图：清洗后保存为 JSON 数组（单图也用数组形式，统一格式）
            cleaned_imgs = [u.strip() for u in (form.get("image_urls") or []) if u and u.strip()]
            b.image_url = json.dumps(cleaned_imgs, ensure_ascii=False) if cleaned_imgs else None
            b.video_title = (form["video_title"] or "").strip() or None
            b.video_cover_url = (form["video_cover_url"] or "").strip() or None
            b.video_link = (form["video_link"] or "").strip() or None
            b.target_group_ids = json.dumps(form["target_group_ids"])
            b.status = "draft"
            db.commit()
            db.refresh(b)
            return b.id
        finally:
            db.close()

    def do_send_now():
        bid = save_broadcast()
        if bid is None:
            return
        with ui.dialog() as dlg, ui.card():
            ui.label("立即发送中…").classes("text-lg font-semibold")
            ui.spinner(size="lg")
        dlg.open()
        async def go():
            from nicegui import run
            result = await run.io_bound(broadcaster.send_broadcast, bid)
            dlg.close()
            _notify_send_result(result)
            load_broadcasts()
        ui.timer(0.1, go, once=True)

    def do_save_schedule():
        bid = save_broadcast()
        if bid is None:
            return
        mode = form["schedule_mode"]
        if mode == "immediate":
            ui.notify('立即发送请点"立即发送"按钮，此选项请选择具体定时方式', type="warning"); return
        config = {}
        if mode == "once":
            if not form["once_dt"]:
                ui.notify("请选择具体时间", type="warning"); return
            config = {"datetime": form["once_dt"]}
        elif mode == "daily":
            config = {"time": form["daily_time"] or "09:00"}
        elif mode == "weekly":
            if not form["weekly_days"]:
                ui.notify("请选择周几", type="warning"); return
            config = {"time": form["weekly_time"] or "09:00", "weekdays": form["weekly_days"]}
        elif mode == "monthly":
            config = {"time": form["monthly_time"] or "09:00", "day": int(form["monthly_day"] or 1)}
        elif mode == "cron":
            if not (form["cron_expr"] or "").strip():
                ui.notify("请填写定时表达式", type="warning"); return
            config = {"cron": form["cron_expr"].strip()}
        db = SessionLocal()
        try:
            sch = BroadcastSchedule(
                broadcast_id=bid,
                schedule_type=mode,
                schedule_config=json.dumps(config, ensure_ascii=False),
                enabled="1",
                created_by=A.user().get("username", "admin"),
            )
            db.add(sch)
            db.commit()
            db.refresh(sch)
            sch_id = sch.id
        finally:
            db.close()
        try:
            bcast_scheduler.add_or_update_schedule(sch_id)
            ui.notify("✅ 已创建定时计划", type="positive")
        except Exception as e:
            ui.notify(f"计划创建但调度失败：{e}", type="warning", multi_line=True)
        load_broadcasts()

    def open_edit(broadcast_id):
        db = SessionLocal()
        try:
            b = db.get(Broadcast, broadcast_id)
            if not b: return
            try:
                gids = json.loads(b.target_group_ids or "[]")
            except Exception:
                gids = []
            form["title"] = b.title or ""
            form["content_text"] = b.content_text or ""
            # 兼容老数据：image_url 可能是单 URL 字符串，也可能是 JSON 数组
            raw_img = (b.image_url or "").strip()
            if raw_img.startswith("["):
                try:
                    form["image_urls"] = [str(u).strip() for u in json.loads(raw_img) if str(u).strip()]
                except Exception:
                    form["image_urls"] = []
            else:
                form["image_urls"] = [raw_img] if raw_img else []
            form["video_title"] = b.video_title or ""
            form["video_cover_url"] = b.video_cover_url or ""
            form["video_link"] = b.video_link or ""
            form["target_group_ids"] = gids
        finally:
            db.close()
        state["editing_id"] = broadcast_id
        title_in.set_value(form["title"])
        content_editor.set_value(form["content_text"] or "")
        images_container.refresh()
        v_title_in.set_value(form["video_title"])
        v_cover_in.set_value(form["video_cover_url"])
        v_link_in.set_value(form["video_link"])
        groups_select.set_value(form["target_group_ids"])
        ui.notify(f'已加载 推送 #{broadcast_id}', type="info")

    def send_existing(broadcast_id):
        with ui.dialog() as dlg, ui.card():
            ui.label("发送中…").classes("text-lg font-semibold")
            ui.spinner(size="lg")
        dlg.open()
        async def go():
            from nicegui import run
            result = await run.io_bound(broadcaster.send_broadcast, broadcast_id)
            dlg.close()
            _notify_send_result(result)
            load_broadcasts()
        ui.timer(0.1, go, once=True)

    # ====== UI 渲染 ======
    refresh_files()

    with ui.card().classes("w-full p-4"):
        ui.label("新建/编辑推送").classes("text-lg font-semibold")
        title_in = ui.input("推送标题（内部用，群里不显示）").bind_value(form, "title").classes("w-full")

        # 文案富文本编辑器：自带工具栏（加粗/斜体/下划线/列表/链接/引用等）。
        # 编辑器产出 HTML，实时转成钉钉 markdown 文字存进 form["content_text"]。
        # 注：钉钉文字消息不支持颜色/字号，工具栏里的颜色/字号仅编辑时可见，发送时会丢弃。
        ui.label("文案（必填）").classes("text-sm text-slate-600 mt-1")
        content_editor = ui.editor(placeholder="在这里写文案，选中文字用上方工具栏加粗、列表、链接、字号、颜色…").classes("w-full bcast-content")
        # 用 list 形式设工具栏（字符串形式转义易导致白屏）。含字号、加粗系列、颜色、列表、链接等。
        content_editor._props["toolbar"] = [
            ["left", "center", "right", "justify"],
            ["bold", "italic", "underline", "strike"],
            [{"label": "字号", "icon": "format_size", "list": "no-icons",
              "options": ["size-1", "size-2", "size-3", "size-4", "size-5", "size-6", "size-7"]}],
            ["hr", "link"],
            ["unordered", "ordered", "quote"],
            ["undo", "redo"],
        ]
        content_editor.style("min-height:140px")

        # 字体颜色：QEditor 内置 token 颜色按钮配置繁琐且易不显示，改用独立取色器，
        # 选色后对编辑器内选中文字应用 foreColor（浏览器原生命令，稳定可靠）。
        with ui.row().classes("items-center gap-2 mt-1"):
            ui.label("字体颜色：").classes("text-xs text-slate-500")
            color_pick = ui.color_input(value="#e63946").props("dense")
            def apply_color():
                color = color_pick.value or "#000000"
                ui.run_javascript(f"""
                (() => {{
                    const wrap = document.querySelector('.bcast-content');
                    if (!wrap) return;
                    const ed = wrap.querySelector('.q-editor__content') || wrap.querySelector('[contenteditable]');
                    if (!ed) return;
                    ed.focus();
                    document.execCommand('styleWithCSS', false, true);
                    document.execCommand('foreColor', false, {json.dumps(color)});
                    ed.dispatchEvent(new Event('input', {{bubbles: true}}));
                }})();
                """)
            ui.button("应用到选中文字", icon="format_color_text", on_click=apply_color).props("flat dense color=primary")
            ui.label("（颜色仅后台预览，钉钉文字消息发送时会忽略颜色）").classes("text-xs text-slate-400")
        ui.label("💡 群发是钉钉文字消息：支持加粗/列表/链接/标题等排版（颜色和字号钉钉不支持，发送时会忽略）。图文穿插：在文案里用每张图的【插入[图N]】按钮放占位符。").classes("text-xs text-indigo-500 mt-1")

        from ..services.richtext_render import html_to_dingtalk_markdown as _h2m

        def _sync_content(e=None):
            """编辑器 HTML → 钉钉 markdown，存进 form。"""
            form["content_text"] = _h2m(content_editor.value or "")

        content_editor.on("update:model-value", _sync_content)

        def insert_placeholder(n: int):
            """在富文本编辑器光标处插入 [图N] 占位符（contenteditable）。"""
            tag = f"[图{n}]"
            ui.run_javascript(f"""
            (() => {{
                const wrap = document.querySelector('.bcast-content');
                if (!wrap) return;
                const ed = wrap.querySelector('.q-editor__content') || wrap.querySelector('[contenteditable]');
                if (!ed) return;
                ed.focus();
                const tag = {json.dumps(tag)};
                let ok = false;
                try {{ ok = document.execCommand('insertText', false, tag); }} catch (e) {{}}
                if (!ok) {{ ed.innerHTML = ed.innerHTML + tag; }}
                ed.dispatchEvent(new Event('input', {{bubbles: true}}));
            }})();
            """)

        ui.label("图片（可选，最多 9 张；[图1]对应第1张，[图2]对应第2张）").classes("text-sm text-slate-500 mt-2")

        # 多图输入区：动态渲染（每张图一行：输入框 + 删除按钮）
        @ui.refreshable
        def images_container():
            if not form["image_urls"]:
                ui.label("（还没有图片，点下方按钮添加）").classes("text-slate-400 text-xs")
                return
            for idx, url in enumerate(form["image_urls"]):
                with ui.row().classes("w-full items-center gap-2"):
                    def make_on_change(i):
                        def _h(e):
                            if i < len(form["image_urls"]):
                                form["image_urls"][i] = (e.value or "").strip()
                        return _h
                    url_in = ui.input(f"图片 {idx + 1}", value=url).classes("flex-1")
                    url_in.on("update:model-value", make_on_change(idx))
                    def make_pick(i):
                        def _h():
                            def _pick(picked):
                                if i < len(form["image_urls"]):
                                    form["image_urls"][i] = picked
                                    images_container.refresh()
                            _pick_file_dialog("image", _pick)
                        return _h
                    ui.button(icon="folder", on_click=make_pick(idx)).props("flat dense").tooltip("从文件库选")
                    def make_insert(i):
                        def _h():
                            insert_placeholder(i + 1)
                        return _h
                    ui.button(f"插入[图{idx + 1}]", on_click=make_insert(idx)).props("flat dense color=indigo").tooltip("在文案光标处插入占位符")
                    def make_remove(i):
                        def _h():
                            if i < len(form["image_urls"]):
                                form["image_urls"].pop(i)
                                images_container.refresh()
                        return _h
                    ui.button(icon="delete", on_click=make_remove(idx)).props("flat dense color=negative").tooltip("移除这张图")

        images_container()

        def add_image_slot():
            if len(form["image_urls"]) >= 9:
                ui.notify("最多 9 张图片", type="warning"); return
            form["image_urls"].append("")
            images_container.refresh()

        ui.button("+ 添加图片", icon="add_a_photo", on_click=add_image_slot).props("flat dense color=primary").classes("mt-1")

        ui.label("视频（可选；以「标题+封面+链接」卡片形式发送）").classes("text-sm text-slate-500 mt-2")
        v_title_in = ui.input("视频标题").bind_value(form, "video_title").classes("w-full")
        with ui.row().classes("w-full items-center gap-2"):
            v_cover_in = ui.input("视频封面图链接（可选）").bind_value(form, "video_cover_url").classes("flex-1")
            ui.button("选封面", icon="image",
                      on_click=lambda: _pick_file_dialog("image", lambda url: (v_cover_in.set_value(url), setattr_form("video_cover_url", url)))).props("flat")
        with ui.row().classes("w-full items-center gap-2"):
            v_link_in = ui.input("视频跳转链接").bind_value(form, "video_link").classes("flex-1")
            ui.button("选视频", icon="movie",
                      on_click=lambda: _pick_file_dialog("video", lambda url: (v_link_in.set_value(url), setattr_form("video_link", url)))).props("flat")

        def setattr_form(k, v):
            form[k] = v

        ui.label("目标群（多选）").classes("text-sm text-slate-500 mt-2")
        groups_select = ui.select(
            options=get_groups_options(),
            multiple=True, value=form["target_group_ids"], label="选群",
        ).bind_value(form, "target_group_ids").props("use-chips").classes("w-full")
        ui.button("刷新群列表", icon="refresh",
                  on_click=lambda: groups_select.set_options(get_groups_options())).props("flat dense")

        # 定时设置
        ui.label("发送方式").classes("text-sm text-slate-500 mt-3 font-semibold")
        mode_radio = ui.radio(
            {"immediate": "立即发送（点下方按钮）",
             "once": "一次性定时",
             "daily": "每天",
             "weekly": "每周某几天",
             "monthly": "每月某日",
             "cron": "高级表达式（cron）"},
            value="immediate"
        ).bind_value(form, "schedule_mode").props("inline")

        with ui.column().classes("w-full mt-1"):
            ui.input("一次性发送时间（YYYY-MM-DD HH:MM）", placeholder="2026-06-01 09:00").bind_value(form, "once_dt").bind_visibility_from(form, "schedule_mode", value="once").classes("w-72")

            with ui.row().classes("gap-2 items-center").bind_visibility_from(form, "schedule_mode", value="daily"):
                ui.input("每天时间（HH:MM）", placeholder="09:00").bind_value(form, "daily_time").classes("w-32")

            with ui.row().classes("gap-2 items-center").bind_visibility_from(form, "schedule_mode", value="weekly"):
                ui.input("时间", placeholder="09:00").bind_value(form, "weekly_time").classes("w-32")
                ui.select({1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"},
                          multiple=True, label="周几").bind_value(form, "weekly_days").props("use-chips").classes("w-72")

            with ui.row().classes("gap-2 items-center").bind_visibility_from(form, "schedule_mode", value="monthly"):
                ui.input("时间", placeholder="09:00").bind_value(form, "monthly_time").classes("w-32")
                ui.number("几号", min=1, max=31, value=1).bind_value(form, "monthly_day").classes("w-32")

            ui.input("表达式（5 字段：分 时 日 月 周）", placeholder="0 9 * * 1-5").bind_value(form, "cron_expr").bind_visibility_from(form, "schedule_mode", value="cron").classes("w-96")

        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            ui.button("清空表单", icon="refresh", on_click=lambda: (reset_form(), title_in.set_value(""), content_editor.set_value(""), images_container.refresh(), v_title_in.set_value(""), v_cover_in.set_value(""), v_link_in.set_value(""), groups_select.set_value([])))
            ui.button("立即发送", icon="send", on_click=do_send_now).props("color=positive")
            ui.button("保存为定时", icon="schedule", on_click=do_save_schedule).props("color=primary")

    with ui.card().classes("w-full mt-4 p-4"):
        ui.label("推送列表（最近 100 条）").classes("text-lg font-semibold")
        list_table = ui.table(
            columns=[
                {"name": "id", "label": "ID", "field": "id", "style": "width: 60px"},
                {"name": "title", "label": "标题", "field": "title", "align": "left", "style": "min-width: 200px"},
                {"name": "status", "label": "状态", "field": "status", "style": "width: 90px"},
                {"name": "groups", "label": "目标群", "field": "groups", "style": "width: 90px"},
                {"name": "created_at", "label": "创建时间", "field": "created_at", "style": "width: 160px; white-space: nowrap"},
                {"name": "last_sent", "label": "最近发送", "field": "last_sent", "style": "width: 160px; white-space: nowrap"},
                {"name": "action", "label": "操作", "field": "id", "style": "width: 180px"},
            ],
            rows=[], row_key="id",
            pagination=10,
        ).classes("w-full mt-2").props(_TABLE_PROPS)

        list_table.add_slot("body-cell-action", r"""
            <q-td :props="props">
                <q-btn dense flat label="编辑" color="primary" @click="$parent.$emit('edit', props.row)"/>
                <q-btn dense flat label="发送" color="positive" @click="$parent.$emit('send', props.row)"/>
            </q-td>
        """)
        list_table.on("edit", lambda e: open_edit(e.args["id"]))
        list_table.on("send", lambda e: send_existing(e.args["id"]))

        ui.button("刷新", icon="refresh", on_click=load_broadcasts).classes("mt-1")

    load_broadcasts()


def _notify_send_result(result: dict):
    """统一展示群发结果：成功数、失败群原因、本地图警告。"""
    ok = result.get("ok")
    succ = result.get("success") or []
    failed = result.get("failed") or []
    if ok:
        ui.notify(f'✅ 已发送到 {len(succ)} 个群', type="positive", multi_line=True)
    else:
        err = result.get("error")
        if err:
            ui.notify(f'⚠️ {err}', type="warning", multi_line=True)
        else:
            reasons = "；".join(
                f'{f.get("title") or f.get("group_id")}：{f.get("detail")}' for f in failed[:5]
            )
            ui.notify(f'⚠️ 成功 {len(succ)} 群，失败 {len(failed)} 群。{reasons}',
                      type="warning", multi_line=True)
    if result.get("warning"):
        ui.notify(f'⚠️ {result["warning"]}', type="warning", multi_line=True)


def _pick_file_dialog(file_type: str, on_pick):
    """弹窗：从文件库挑一个文件，回调返回 public_url"""
    files = [f for f in uploader.list_files() if f["file_type"] == file_type]
    if not files:
        ui.notify(f"文件库里还没有{file_type}，请先在「文件库」tab 上传", type="warning")
        return
    with ui.dialog() as dlg, ui.card().classes("w-[720px] max-h-[60vh] overflow-auto"):
        ui.label(f"选择{file_type}").classes("text-lg font-semibold")
        for f in files:
            with ui.row().classes("w-full items-center justify-between p-2 hover:bg-slate-50"):
                with ui.column():
                    ui.label(f["original_name"]).classes("font-medium")
                    ui.label(f["public_url"]).classes("text-xs text-slate-500")
                def make_handler(url):
                    def _h():
                        on_pick(url); dlg.close()
                    return _h
                ui.button("选这个", on_click=make_handler(f["public_url"])).props("color=primary dense")
        with ui.row().classes("w-full justify-end mt-2"):
            ui.button("取消", on_click=dlg.close)
    dlg.open()


# ----------------- 发送历史 -----------------
def _render_history_tab():
    table = ui.table(
        columns=[
            {"name": "id", "label": "推送ID", "field": "id", "style": "width: 80px"},
            {"name": "title", "label": "标题", "field": "title", "align": "left", "style": "min-width: 200px"},
            {"name": "status", "label": "状态", "field": "status", "style": "width: 90px"},
            {"name": "last_sent", "label": "最近发送", "field": "last_sent", "style": "width: 160px; white-space: nowrap"},
            {"name": "result", "label": "结果", "field": "result", "align": "left", "style": "min-width: 400px; word-break: break-word; white-space: normal"},
        ],
        rows=[], row_key="id",
        pagination=10,
    ).classes("w-full mt-2").props(_TABLE_PROPS)

    def load():
        db = SessionLocal()
        try:
            rows = db.query(Broadcast).filter(Broadcast.last_sent_at != None).order_by(Broadcast.last_sent_at.desc()).limit(50).all()
            data = [{
                "id": r.id, "title": r.title, "status": r.status,
                "last_sent": r.last_sent_at.strftime("%Y-%m-%d %H:%M:%S") if r.last_sent_at else "",
                "result": (r.last_result or "")[:300],
            } for r in rows]
        finally:
            db.close()
        table.rows = data
        table.update()

    ui.button("刷新", icon="refresh", on_click=load).classes("mt-1")
    load()
