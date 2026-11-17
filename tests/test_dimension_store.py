"""改进 90 store 层：人工维度编辑三方法（update_dimension_description /
set_dimension_active / delete_dimension）——core 保护、复活清零、删除清理链。

引擎路径（update_dimension 计数 / deactivate_dimension 淘汰）语义不变，不在此重复测试。
"""

import pytest

from mr_data.models import DialogueLog

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _make_noncore(pg, description="探索人类情感", delta_success=0, delta_failure=0):
    dim_id = pg.insert_dimension(description)
    if delta_success or delta_failure:
        pg.update_dimension(dim_id, delta_success=delta_success, delta_failure=delta_failure)
    return dim_id


def test_update_description(pg):
    dim_id = _make_noncore(pg, "旧描述")
    pg.update_dimension_description(dim_id, "新描述")
    assert pg.get_dimension(dim_id).description == "新描述"


def test_update_description_rejects_core(pg):
    core_dim = next(d for d in pg.list_dimensions() if d.core)
    with pytest.raises(ValueError):
        pg.update_dimension_description(core_dim.id, "篡改")


def test_update_description_missing(pg):
    with pytest.raises(ValueError):
        pg.update_dimension_description(9999, "x")


def test_deactivate_keeps_counts(pg):
    dim_id = _make_noncore(pg, delta_success=3, delta_failure=9)
    pg.set_dimension_active(dim_id, False)
    dim = pg.get_dimension(dim_id)
    assert dim.active is False
    assert (dim.success_count, dim.failure_count) == (3, 9)


def test_reactivate_resets_counts(pg):
    dim_id = _make_noncore(pg, delta_success=2, delta_failure=9)
    pg.set_dimension_active(dim_id, False)
    pg.set_dimension_active(dim_id, True)
    dim = pg.get_dimension(dim_id)
    assert dim.active is True
    assert (dim.success_count, dim.failure_count) == (0, 0)


def test_set_active_rejects_core(pg):
    core_dim = next(d for d in pg.list_dimensions() if d.core)
    with pytest.raises(ValueError):
        pg.set_dimension_active(core_dim.id, False)


def test_delete_rejects_active(pg):
    dim_id = _make_noncore(pg)
    with pytest.raises(ValueError):
        pg.delete_dimension(dim_id)


def test_delete_rejects_core(pg):
    core_dim = next(d for d in pg.list_dimensions() if d.core)
    with pytest.raises(ValueError):
        pg.delete_dimension(core_dim.id)


def test_delete_cleans_adjustments_and_refs(pg):
    dim_id = _make_noncore(pg)
    pg.set_dimension_active(dim_id, False)
    # 造 adjustment 行与 dimension_refs 行（FK 级联对象）
    session_id = pg.create_session()
    with pg._cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO adjustment_logs (dimension_id, session_id, reason) VALUES (%s, %s, %s)",
            (dim_id, session_id, "test setup"),
        )
        cur.execute("SELECT count(*) AS n FROM adjustment_logs WHERE dimension_id = %s", (dim_id,))
        assert cur.fetchone()["n"] == 1
    log_id = pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content="你好"))
    pg.insert_dialogue_dimension_refs(log_id, [dim_id])
    with pg._cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM dialogue_dimension_refs WHERE dimension_id = %s", (dim_id,))
        assert cur.fetchone()["n"] == 1

    adjustments = pg.delete_dimension(dim_id)

    assert adjustments == 1  # 返回值 = 清理的 adjustment 行数（setup 恰好 1 行）
    assert pg.get_dimension(dim_id) is None
    with pg._cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM adjustment_logs WHERE dimension_id = %s", (dim_id,))
        assert cur.fetchone()["n"] == 0
        cur.execute(
            "SELECT count(*) AS n FROM dialogue_dimension_refs WHERE dimension_id = %s", (dim_id,))
        assert cur.fetchone()["n"] == 0
