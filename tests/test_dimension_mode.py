"""改进 90 模式核心：维度编辑模式状态机分支。

注入式 input_fn 脚本驱动（返回 None 结束模式）。断言要求遵循
doc/testing-guidelines.md：锁定行为与状态，提示文案不逐字断言
（安全关键文案除外）。
审计日志断言用 open_log_propagation + caplog（坑 #5：logger 构造后开启）。
"""

import re

import pytest

from mr_data.interactive import dimensions as dimmode
from mr_data.interactive.dimensions import run_dimension_mode
from conftest import open_log_propagation
from mr_data.models import (
    DialogueLog,
    DialogueVectorRef,
    PersonalityEvent,
)

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _scripted(lines, calls=None):
    """input_fn：按脚本逐行返回，耗尽后返回 None（结束模式）。
    calls 可传 list 记录每次调用（行为断言用，替代文案断言）。"""

    it = iter(lines)

    def input_fn(prompt):
        if calls is not None:
            calls.append(prompt)
        return next(it, None)

    return input_fn


def _noncore(pg, description="探索人类情感", delta_success=0, delta_failure=0):
    dim_id = pg.insert_dimension(description)
    if delta_success or delta_failure:
        pg.update_dimension(dim_id, delta_success=delta_success, delta_failure=delta_failure)
    return dim_id


def _core_id(pg):
    return next(d for d in pg.list_dimensions() if d.core).id


# --- 进入/退出/分派 ---

def test_abnormal_input_digested_and_loop_survives(pg):
    """非 / 输入与未知命令被就地消化，循环继续（后续 /quit 生效）。"""
    calls = []
    run_dimension_mode(pg, input_fn=_scripted(["你好", "/foo", "/quit"], calls))
    assert len(calls) == 3


def test_help_lists_commands(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["/help", "/quit"]))
    out = capsys.readouterr().out
    for cmd in ("/list", "/edit", "/deactivate", "/reactivate", "/delete", "/quit"):
        assert cmd in out


def test_eof_ends_mode(pg):
    calls = []
    run_dimension_mode(pg, input_fn=_scripted([], calls))  # 不抛异常即正常退出
    assert len(calls) == 1


# --- /list ---

def _data_lines(out):
    """表格数据行（id 开头），不含表头与页脚。"""
    return [l for l in out.splitlines() if re.match(r"^\d+\s", l)]


def test_list_pagination(pg, capsys):
    for i in range(21):
        pg.insert_dimension(f"分页测试维度 {i:02d}")
    total = len(pg.list_dimensions())  # 5 种子 + 21 新建
    run_dimension_mode(pg, input_fn=_scripted(["/list", "/quit"]))
    assert len(_data_lines(capsys.readouterr().out)) == 20  # 首页 20 行
    run_dimension_mode(pg, input_fn=_scripted(["/list 2", "/quit"]))
    assert len(_data_lines(capsys.readouterr().out)) == total - 20  # 第二页余量


def test_list_page_out_of_range_keeps_loop(pg):
    calls = []
    run_dimension_mode(pg, input_fn=_scripted(["/list 9", "/quit"], calls))
    assert len(calls) == 2  # 越界被消化，循环继续


def test_list_off_filter(pg, capsys):
    active_marker = _noncore(pg, "活跃标记维度甲")
    off_marker = _noncore(pg, "停用标记维度乙")
    pg.set_dimension_active(off_marker, False)
    run_dimension_mode(pg, input_fn=_scripted(["/list off", "/quit"]))
    out = capsys.readouterr().out
    assert pg.get_dimension(off_marker).description in out
    assert pg.get_dimension(active_marker).description not in out


# --- /edit ---

def test_edit_with_id(pg):
    dim_id = _noncore(pg, "旧描述")
    run_dimension_mode(pg, input_fn=_scripted([
        f"/edit {dim_id}", "新描述", "y", "/quit",
    ]))
    assert pg.get_dimension(dim_id).description == "新描述"


def test_edit_unchanged_description_no_write(pg, caplog, monkeypatch):
    open_log_propagation(caplog, monkeypatch, "mr_data.interactive.dimensions")
    dim_id = _noncore(pg, "原样描述")
    with caplog.at_level("INFO"):
        run_dimension_mode(pg, input_fn=_scripted([
            f"/edit {dim_id}", "原样描述", "/quit",
        ]))
    assert pg.get_dimension(dim_id).description == "原样描述"
    assert _audit_records(caplog, "dimension_edit") == []


def test_edit_empty_input_gives_up(pg, capsys):
    dim_id = _noncore(pg, "保持我")
    run_dimension_mode(pg, input_fn=_scripted([f"/edit {dim_id}", "", "/quit"]))
    assert "已放弃该命令" in capsys.readouterr().out
    assert pg.get_dimension(dim_id).description == "保持我"


def test_edit_decline_confirm(pg):
    dim_id = _noncore(pg, "保持我")
    run_dimension_mode(pg, input_fn=_scripted([
        f"/edit {dim_id}", "新描述", "n", "/quit",
    ]))
    assert pg.get_dimension(dim_id).description == "保持我"


def test_edit_without_id_numbered_selection(pg, capsys):
    dim_id = _noncore(pg, "编号选择的维度")
    candidates = [d for d in pg.list_dimensions() if not d.core]
    n = candidates.index(next(d for d in candidates if d.id == dim_id)) + 1
    run_dimension_mode(pg, input_fn=_scripted(["/edit", str(n), "改后的描述", "y", "/quit"]))
    out = capsys.readouterr().out
    assert f"{n}. id={dim_id}" in out
    assert pg.get_dimension(dim_id).description == "改后的描述"


def test_edit_selection_invalid_twice_gives_up(pg, capsys):
    dim_id = _noncore(pg)
    run_dimension_mode(pg, input_fn=_scripted(["/edit", "99", "x", "/quit"]))
    # 无效选择两次后放弃：维度未被改动（「已放弃该命令」文案断言见
    # test_edit_empty_input_gives_up，此处按 §2.1 只锁状态）
    assert pg.get_dimension(dim_id).description == "探索人类情感"


# --- core 保护与错误路径 ---

def test_core_dimension_rejected_by_all_commands(pg, chroma_store):
    """三条变更命令对 core 均拒绝，且不进入后续交互（无多余 prompt）。"""
    core = _core_id(pg)
    calls = []
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/edit {core}", f"/deactivate {core}", f"/delete {core}", "/quit",
    ], calls))
    assert len(calls) == 4
    dim = pg.get_dimension(core)
    assert dim is not None and dim.active is True


def test_edit_bad_id_digested(pg):
    calls = []
    run_dimension_mode(pg, input_fn=_scripted(["/edit 9999", "/edit abc", "/quit"], calls))
    assert len(calls) == 3  # 错误消化，循环继续


# --- /deactivate / /reactivate ---

def test_deactivate_flow(pg):
    dim_id = _noncore(pg, delta_success=2, delta_failure=9)
    run_dimension_mode(pg, input_fn=_scripted([f"/deactivate {dim_id}", "y", "/quit"]))
    dim = pg.get_dimension(dim_id)
    assert dim.active is False and (dim.success_count, dim.failure_count) == (2, 9)


def test_deactivate_idempotent_no_write(pg, caplog, monkeypatch):
    open_log_propagation(caplog, monkeypatch, "mr_data.interactive.dimensions")
    dim_id = _noncore(pg)
    pg.set_dimension_active(dim_id, False)
    with caplog.at_level("INFO"):
        run_dimension_mode(pg, input_fn=_scripted([f"/deactivate {dim_id}", "/quit"]))
    assert pg.get_dimension(dim_id).active is False
    assert _audit_records(caplog, "dimension_deactivate") == []


def test_reactivate_resets_counts_flow(pg):
    dim_id = _noncore(pg, delta_success=2, delta_failure=9)
    pg.set_dimension_active(dim_id, False)
    run_dimension_mode(pg, input_fn=_scripted([f"/reactivate {dim_id}", "y", "/quit"]))
    dim = pg.get_dimension(dim_id)
    assert dim.active is True and (dim.success_count, dim.failure_count) == (0, 0)


def test_reactivate_idempotent_no_write(pg, caplog, monkeypatch):
    open_log_propagation(caplog, monkeypatch, "mr_data.interactive.dimensions")
    dim_id = _noncore(pg)
    with caplog.at_level("INFO"):
        run_dimension_mode(pg, input_fn=_scripted([f"/reactivate {dim_id}", "/quit"]))
    assert pg.get_dimension(dim_id).active is True
    assert _audit_records(caplog, "dimension_reactivate") == []


# --- /delete ---

def _seed_deletable(pg, chroma_store):
    dim_id = _noncore(pg, "待删除维度", delta_success=1, delta_failure=2)
    pg.set_dimension_active(dim_id, False)
    session_id = pg.create_session()
    with pg._cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO adjustment_logs (dimension_id, session_id, reason) VALUES (%s, %s, %s)",
            (dim_id, session_id, "setup"),
        )
    log_id = pg.insert_dialogue(DialogueLog(session_id=session_id, role="assistant", content="x"))
    event = PersonalityEvent(
        content="待清理证据", dimension_ids=[dim_id],
        source_type="evidence", source_id="legacy",
    )
    doc_id = chroma_store.add_personality_event(event)
    pg.insert_dialogue_vector_refs(
        log_id,
        [DialogueVectorRef(dialogue_log_id=log_id, vector_doc_id=doc_id,
                           source_type="evidence", content=event.content,
                           dimension_ids=[dim_id])],
    )
    return dim_id, doc_id


def test_delete_active_dim_refused(pg, chroma_store, capsys):
    dim_id = _noncore(pg)
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/delete {dim_id}", "/quit",
    ]))
    assert f"请先 /deactivate {dim_id} 再删除" in capsys.readouterr().out
    assert pg.get_dimension(dim_id) is not None


def test_delete_requires_chroma(pg):
    dim_id = _noncore(pg)
    pg.set_dimension_active(dim_id, False)
    calls = []
    run_dimension_mode(pg, chroma=None, input_fn=_scripted([
        f"/delete {dim_id}", "/quit",
    ], calls))
    assert len(calls) == 2  # 守卫拒绝后不进入后续交互
    assert pg.get_dimension(dim_id) is not None


def test_delete_full_flow_cleans_all(pg, chroma_store, capsys):
    dim_id, doc_id = _seed_deletable(pg, chroma_store)
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/delete {dim_id}", "y", str(dim_id), "/quit",
    ]))
    out = capsys.readouterr().out
    assert "待删除维度" in out  # 删除前展示目标
    assert "此操作不可恢复" in out  # 安全关键文案（testing-guidelines 2.1 例外）
    # 清理链：维度行 / adjustment / vector refs / Chroma 文档全部消失
    assert pg.get_dimension(dim_id) is None
    with pg._cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM adjustment_logs WHERE dimension_id = %s", (dim_id,))
        assert cur.fetchone()["n"] == 0
    assert chroma_store.get_personality_event_ids_by_dimension(dim_id) == []
    with pytest.raises(Exception):
        chroma_store.get_personality_events([doc_id])


def test_delete_second_confirm_mismatch_gives_up(pg, chroma_store):
    dim_id, _ = _seed_deletable(pg, chroma_store)
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/delete {dim_id}", "y", "9999", "/quit",
    ]))
    assert pg.get_dimension(dim_id) is not None


def test_delete_decline_first_confirm(pg, chroma_store):
    dim_id, _ = _seed_deletable(pg, chroma_store)
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/delete {dim_id}", "n", "/quit",
    ]))
    assert pg.get_dimension(dim_id) is not None


# --- 审计日志 ---

def _audit_records(caplog, event):
    # 同一 record 可能被 root 多个 handler 重复收集，按对象身份去重
    records = []
    for r in caplog.records:
        if r.name == "mr_data.interactive.dimensions" and r.message == event and r not in records:
            records.append(r)
    return records


def test_audit_log_written(pg, chroma_store, caplog, monkeypatch):
    """三种变更各写一条审计，事件名与 details 字段锁定（机器契约，
    对齐 dimension-editing §3.6 样例 dimension_reactivate 等）。"""
    open_log_propagation(caplog, monkeypatch, "mr_data.interactive.dimensions")
    edit_id = _noncore(pg, "旧审计描述")
    re_id = _noncore(pg, delta_success=2, delta_failure=9)
    pg.set_dimension_active(re_id, False)
    del_id, _ = _seed_deletable(pg, chroma_store)
    with caplog.at_level("INFO"):
        run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
            f"/edit {edit_id}", "新审计描述", "y",
            f"/reactivate {re_id}", "y",
            f"/delete {del_id}", "y", str(del_id),
            "/quit",
        ]), entry="chat")
    rec = _audit_records(caplog, "dimension_edit")
    assert len(rec) == 1 and rec[0].details["dimension_id"] == edit_id
    assert rec[0].details["entry"] == "chat"
    rec = _audit_records(caplog, "dimension_reactivate")
    assert len(rec) == 1 and rec[0].details["dimension_id"] == re_id
    assert rec[0].details["counts"] == "2/9->0/0"
    rec = _audit_records(caplog, "dimension_delete")
    assert len(rec) == 1 and rec[0].details["dimension_id"] == del_id
    assert rec[0].details["adjustments"] == 1


def test_no_audit_on_aborted_or_readonly(pg, caplog, monkeypatch):
    open_log_propagation(caplog, monkeypatch, "mr_data.interactive.dimensions")
    dim_id = _noncore(pg)
    with caplog.at_level("INFO"):
        run_dimension_mode(pg, input_fn=_scripted([
            f"/edit {dim_id}", "",  # 放弃
            "/list",
            "/quit",
        ]))
    assert _audit_records(caplog, "dimension_edit") == []


# --- 状态条 smoke（步骤 3） ---

def test_status_bar_smoke_with_default_input(pg, monkeypatch):
    """真实终端路径：默认 Prompt.ask + rich Live 状态条启停不抛异常。"""
    monkeypatch.setattr(dimmode.Prompt, "ask", lambda *a, **k: "/quit")
    run_dimension_mode(pg, status_bar=True)  # Live start/stop 无异常即通过
