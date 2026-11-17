"""改进 90 模式核心：维度编辑模式状态机全分支。

注入式 input_fn 脚本驱动（返回 None 结束模式），文案对照
doc/dimension-editing-plan.md §2.5 锁定（文案即契约）。
审计日志断言用 open_log_propagation + caplog（坑 #5：logger 构造后开启）。
"""

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


def _scripted(lines):
    """input_fn：按脚本逐行返回，耗尽后返回 None（结束模式）。"""
    it = iter(lines)

    def input_fn(prompt):
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

def test_mode_enters_and_quits(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["/quit"]))
    out = capsys.readouterr().out
    assert "维度编辑模式" in out  # banner


def test_non_command_input_rejected(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["你好", "/quit"]))
    assert "模式内仅识别 / 命令" in capsys.readouterr().out


def test_unknown_command(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["/foo", "/quit"]))
    assert "未知命令 /foo" in capsys.readouterr().out


def test_help_lists_commands(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["/help", "/quit"]))
    out = capsys.readouterr().out
    for cmd in ("/list", "/edit", "/deactivate", "/reactivate", "/delete", "/quit"):
        assert cmd in out
    assert "core 维度受保护" in out


def test_eof_ends_mode(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted([]))
    assert "维度编辑模式" in capsys.readouterr().out


# --- /list ---

def test_list_shows_dimensions(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["/list", "/quit"]))
    out = capsys.readouterr().out
    assert "id" in out and "core" in out and "description" in out
    assert "Picard" not in out  # 维度描述而非身份
    dims = pg.list_dimensions()
    assert any(d.description[:10] in out for d in dims)


def test_list_pagination(pg, capsys):
    for i in range(21):
        pg.insert_dimension(f"分页测试维度 {i:02d}")
    run_dimension_mode(pg, input_fn=_scripted(["/list", "/quit"]))
    out = capsys.readouterr().out
    assert "第 1/2 页" in out and "共 2" in out and "/list 2 翻页" in out


def test_list_page_out_of_range(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["/list 9", "/quit"]))
    assert "共 1 页" in capsys.readouterr().out


def test_list_off_filter(pg, capsys):
    dim_id = _noncore(pg)
    pg.set_dimension_active(dim_id, False)
    run_dimension_mode(pg, input_fn=_scripted(["/list off", "/quit"]))
    out = capsys.readouterr().out
    assert str(dim_id) in out
    assert "OFF" in out


# --- /edit ---

def test_edit_with_id(pg, capsys):
    dim_id = _noncore(pg, "旧描述")
    run_dimension_mode(pg, input_fn=_scripted([
        f"/edit {dim_id}", "新描述", "y", "/quit",
    ]))
    out = capsys.readouterr().out
    assert "当前描述：旧描述" in out
    assert f"✓ 已更新维度 {dim_id} 的描述" in out
    assert pg.get_dimension(dim_id).description == "新描述"


def test_edit_unchanged_description(pg, capsys):
    dim_id = _noncore(pg, "原样描述")
    run_dimension_mode(pg, input_fn=_scripted([
        f"/edit {dim_id}", "原样描述", "/quit",
    ]))
    assert "描述未变化" in capsys.readouterr().out


def test_edit_empty_input_gives_up(pg, capsys):
    dim_id = _noncore(pg, "保持我")
    run_dimension_mode(pg, input_fn=_scripted([f"/edit {dim_id}", "", "/quit"]))
    assert "已放弃该命令" in capsys.readouterr().out
    assert pg.get_dimension(dim_id).description == "保持我"


def test_edit_decline_confirm(pg, capsys):
    dim_id = _noncore(pg, "保持我")
    run_dimension_mode(pg, input_fn=_scripted([
        f"/edit {dim_id}", "新描述", "n", "/quit",
    ]))
    assert "已放弃该命令" in capsys.readouterr().out
    assert pg.get_dimension(dim_id).description == "保持我"


def test_edit_without_id_numbered_selection(pg, capsys):
    dim_id = _noncore(pg, "编号选择的维度")
    candidates = [d for d in pg.list_dimensions() if not d.core]
    n = candidates.index(next(d for d in candidates if d.id == dim_id)) + 1
    run_dimension_mode(pg, input_fn=_scripted(["/edit", str(n), "改后的描述", "y", "/quit"]))
    out = capsys.readouterr().out
    assert "可编辑的非 core 维度" in out
    assert f"{n}. id={dim_id}" in out
    assert pg.get_dimension(dim_id).description == "改后的描述"


def test_edit_selection_invalid_twice_gives_up(pg, capsys):
    _noncore(pg)
    run_dimension_mode(pg, input_fn=_scripted(["/edit", "99", "x", "/quit"]))
    assert "已放弃该命令" in capsys.readouterr().out


def test_edit_core_rejected(pg, capsys):
    core = _core_id(pg)
    run_dimension_mode(pg, input_fn=_scripted([f"/edit {core}", "/quit"]))
    assert "core 维度受保护" in capsys.readouterr().out


def test_edit_missing_id_reported(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["/edit 9999", "/quit"]))
    assert "维度 9999 不存在" in capsys.readouterr().out


def test_edit_non_numeric_arg(pg, capsys):
    run_dimension_mode(pg, input_fn=_scripted(["/edit abc", "/quit"]))
    assert "参数需为维度 id 数字" in capsys.readouterr().out


# --- /deactivate / /reactivate ---

def test_deactivate_flow(pg, capsys):
    dim_id = _noncore(pg, delta_success=2, delta_failure=9)
    run_dimension_mode(pg, input_fn=_scripted([f"/deactivate {dim_id}", "y", "/quit"]))
    out = capsys.readouterr().out
    assert "（active，2/9）" in out
    assert f"✓ 已停用维度 {dim_id}" in out
    dim = pg.get_dimension(dim_id)
    assert dim.active is False and (dim.success_count, dim.failure_count) == (2, 9)


def test_deactivate_idempotent(pg, capsys):
    dim_id = _noncore(pg)
    pg.set_dimension_active(dim_id, False)
    run_dimension_mode(pg, input_fn=_scripted([f"/deactivate {dim_id}", "/quit"]))
    assert f"维度 {dim_id} 已是停用状态" in capsys.readouterr().out


def test_reactivate_resets_counts_flow(pg, capsys):
    dim_id = _noncore(pg, delta_success=2, delta_failure=9)
    pg.set_dimension_active(dim_id, False)
    run_dimension_mode(pg, input_fn=_scripted([f"/reactivate {dim_id}", "y", "/quit"]))
    out = capsys.readouterr().out
    assert "（OFF，2/9）" in out
    assert "复活将清零计数（2/9 → 0/0）" in out
    assert f"✓ 已复活维度 {dim_id}，计数已重置" in out
    dim = pg.get_dimension(dim_id)
    assert dim.active is True and (dim.success_count, dim.failure_count) == (0, 0)


def test_reactivate_idempotent(pg, capsys):
    dim_id = _noncore(pg)
    run_dimension_mode(pg, input_fn=_scripted([f"/reactivate {dim_id}", "/quit"]))
    assert f"维度 {dim_id} 已是活跃状态" in capsys.readouterr().out


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


def test_delete_core_refused(pg, chroma_store, capsys):
    core = _core_id(pg)
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/delete {core}", "/quit",
    ]))
    assert "core 维度受保护" in capsys.readouterr().out


def test_delete_requires_chroma(pg, capsys):
    dim_id = _noncore(pg)
    pg.set_dimension_active(dim_id, False)
    run_dimension_mode(pg, chroma=None, input_fn=_scripted([
        f"/delete {dim_id}", "/quit",
    ]))
    assert "Chroma 不可用" in capsys.readouterr().out


def test_delete_full_flow_cleans_all(pg, chroma_store, capsys):
    dim_id, doc_id = _seed_deletable(pg, chroma_store)
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/delete {dim_id}", "y", str(dim_id), "/quit",
    ]))
    out = capsys.readouterr().out
    assert "待删除维度" in out and "此操作不可恢复" in out
    assert f"✓ 已删除维度 {dim_id}" in out
    # 清理链：维度行 / adjustment / vector refs / Chroma 文档全部消失
    assert pg.get_dimension(dim_id) is None
    with pg._cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM adjustment_logs WHERE dimension_id = %s", (dim_id,))
        assert cur.fetchone()["n"] == 0
    assert chroma_store.get_personality_event_ids_by_dimension(dim_id) == []
    with pytest.raises(Exception):
        chroma_store.get_personality_events([doc_id])


def test_delete_second_confirm_mismatch_gives_up(pg, chroma_store, capsys):
    dim_id, _ = _seed_deletable(pg, chroma_store)
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/delete {dim_id}", "y", "9999", "/quit",
    ]))
    assert "已放弃该命令" in capsys.readouterr().out
    assert pg.get_dimension(dim_id) is not None


def test_delete_decline_first_confirm(pg, chroma_store, capsys):
    dim_id, _ = _seed_deletable(pg, chroma_store)
    run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
        f"/delete {dim_id}", "n", "/quit",
    ]))
    assert "已放弃该命令" in capsys.readouterr().out
    assert pg.get_dimension(dim_id) is not None


# --- 审计日志 ---

def _audit_records(caplog, event):
    # 同一 record 可能被 root 多个 handler 重复收集，按对象身份去重
    records = []
    for r in caplog.records:
        if r.name == "mr_data.interactive.dimensions" and r.message == event and r not in records:
            records.append(r)
    return records


def test_audit_log_on_edit(pg, caplog, monkeypatch):
    open_log_propagation(caplog, monkeypatch, "mr_data.interactive.dimensions")
    dim_id = _noncore(pg, "旧审计描述")
    with caplog.at_level("INFO"):
        run_dimension_mode(pg, input_fn=_scripted([
            f"/edit {dim_id}", "新审计描述", "y", "/quit",
        ]), entry="chat")
    rec = _audit_records(caplog, "dimension.edit")
    assert len(rec) == 1
    assert rec[0].details["dimension_id"] == dim_id and rec[0].details["entry"] == "chat"


def test_audit_log_on_reactivate(pg, caplog, monkeypatch):
    open_log_propagation(caplog, monkeypatch, "mr_data.interactive.dimensions")
    dim_id = _noncore(pg, delta_success=2, delta_failure=9)
    pg.set_dimension_active(dim_id, False)
    with caplog.at_level("INFO"):
        run_dimension_mode(pg, input_fn=_scripted([
            f"/reactivate {dim_id}", "y", "/quit",
        ]), entry="cli")
    rec = _audit_records(caplog, "dimension.reactivate")
    assert len(rec) == 1
    assert rec[0].details["entry"] == "cli" and rec[0].details["counts"] == "2/9->0/0"


def test_audit_log_on_delete(pg, chroma_store, caplog, monkeypatch):
    open_log_propagation(caplog, monkeypatch, "mr_data.interactive.dimensions")
    dim_id, _ = _seed_deletable(pg, chroma_store)
    with caplog.at_level("INFO"):
        run_dimension_mode(pg, chroma=chroma_store, input_fn=_scripted([
            f"/delete {dim_id}", "y", str(dim_id), "/quit",
        ]), entry="chat")
    rec = _audit_records(caplog, "dimension.delete")
    assert len(rec) == 1
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
    assert _audit_records(caplog, "dimension.edit") == []


# --- 状态条 smoke（步骤 3） ---

def test_status_bar_smoke_with_default_input(pg, monkeypatch):
    """真实终端路径：默认 Prompt.ask + rich Live 状态条启停不抛异常。"""
    monkeypatch.setattr(dimmode.Prompt, "ask", lambda *a, **k: "/quit")
    run_dimension_mode(pg, status_bar=True)  # Live start/stop 无异常即通过
