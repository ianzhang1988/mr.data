"""Chat HTTP API 端点测试（chat-http-api 计划 §3/§5/§10）。

TestClient + create_app(graph=..., pg=...) 注入 FakeLLM/真实 pgembed：
断言响应形状（reply/references/events）、错误码全集、关会话后 PG 最终状态。
契约样例与 doc/chat-http-api.md §3 对齐（机器消费格式 = 稳定契约，精确锁定）。
"""

import pytest
from fastapi.testclient import TestClient

from mr_data.db import PostgresStore
from mr_data.online import DialogueGraph
from mr_data.server import create_app

pytestmark = pytest.mark.usefixtures("reset_pg_state")


@pytest.fixture
def api(fake_llm, pg, chroma_store):
    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )
    app = create_app(graph=graph, pg=pg)
    return TestClient(app), graph, pg


def _error_code(response) -> str:
    body = response.json()
    assert set(body["error"].keys()) == {"code", "message"}
    return body["error"]["code"]


def test_health(api):
    client, _, _ = api
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_session_generates_uuid_and_persists(api):
    client, _, pg = api
    r = client.post("/sessions", json={})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "active"
    assert pg.get_session(body["session_id"]) is not None


def test_create_session_with_explicit_id_and_conflict(api):
    client, _, _ = api
    r1 = client.post("/sessions", json={"session_id": "sess-x"})
    assert r1.status_code == 201
    assert r1.json()["session_id"] == "sess-x"

    r2 = client.post("/sessions", json={"session_id": "sess-x"})
    assert r2.status_code == 409
    assert _error_code(r2) == "session_already_exists"


def test_get_session_shape_and_not_found(api):
    client, _, _ = api
    missing = client.get("/sessions/nope")
    assert missing.status_code == 404
    assert _error_code(missing) == "session_not_found"

    sid = client.post("/sessions", json={}).json()["session_id"]
    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == sid
    assert body["status"] == "active"
    assert body["rating"] is None
    assert body["rating_comment"] is None
    assert body["attribution_attempts"] == 0
    assert body["created_at"]
    assert body["closed_at"] is None


def test_message_round_trip_with_events_and_persistence(api):
    client, _, pg = api
    sid = client.post("/sessions", json={}).json()["session_id"]

    r = client.post(f"/sessions/{sid}/message", json={"content": "你好"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == sid
    assert body["reply"]
    assert isinstance(body["references"], list)
    names = [e["event"] for e in body["events"]]
    assert names[0] == "chat.start"
    assert names[-1] == "chat.turn"

    logs = pg.get_recent_dialogues(session_id=sid)
    assert sorted(log.role for log in logs) == ["assistant", "user"]

    # 第二轮：服务端自动衔接历史（用户输入之后是 assistant 回复）
    r2 = client.post(f"/sessions/{sid}/message", json={"content": "接着问"})
    assert r2.status_code == 200
    logs = pg.get_recent_dialogues(session_id=sid)
    assert len(logs) == 4
    by_time = sorted(
        logs,
        key=lambda log: (log.created_at is None, log.created_at or log.id, log.id or 0),
    )
    assert by_time[-2].role == "user" and by_time[-2].content == "接着问"
    assert by_time[-1].role == "assistant"


def test_message_rejects_invalid_empty_and_missing_content(api):
    client, _, _ = api
    sid = client.post("/sessions", json={}).json()["session_id"]

    for payload in ({}, {"content": ""}, {"content": "   "}):
        r = client.post(f"/sessions/{sid}/message", json=payload)
        assert r.status_code == 400
        assert _error_code(r) == "invalid_message"


def test_message_to_missing_or_closed_session(api):
    client, _, _ = api
    missing = client.post("/sessions/ghost/message", json={"content": "hi"})
    assert missing.status_code == 404
    assert _error_code(missing) == "session_not_found"

    sid = client.post("/sessions", json={}).json()["session_id"]
    client.post(f"/sessions/{sid}/close", json={})
    closed = client.post(f"/sessions/{sid}/message", json={"content": "hi"})
    assert closed.status_code == 409
    assert _error_code(closed) == "session_closed"


def test_message_chat_turn_failure_returns_500(api):
    client, graph, _ = api
    sid = client.post("/sessions", json={}).json()["session_id"]

    original = graph.chat
    graph.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        r = client.post(f"/sessions/{sid}/message", json={"content": "hi"})
    finally:
        graph.chat = original
    assert r.status_code == 500
    assert _error_code(r) == "chat_turn_failed"


def test_close_without_rating_enters_offline_candidates(api):
    client, _, pg = api
    sid = client.post("/sessions", json={}).json()["session_id"]
    client.post(f"/sessions/{sid}/message", json={"content": "你好"})

    r = client.post(f"/sessions/{sid}/close", json={})
    assert r.status_code == 200
    assert r.json() == {"session_id": sid, "status": "closed"}

    session = pg.get_session(sid)
    assert session.status == "closed"
    assert session.closed_at is not None
    candidates = pg.list_closed_sessions_with_unprocessed()
    assert any(s.id == sid for s in candidates)


def test_close_with_rating_persisted(api):
    client, _, pg = api
    sid = client.post("/sessions", json={}).json()["session_id"]

    r = client.post(
        f"/sessions/{sid}/close", json={"rating": {"score": 2, "comment": "很好"}}
    )
    assert r.status_code == 200
    session = pg.get_session(sid)
    assert session.rating == 2
    assert session.rating_comment == "很好"


def test_close_twice_conflicts(api):
    client, _, _ = api
    sid = client.post("/sessions", json={}).json()["session_id"]
    assert client.post(f"/sessions/{sid}/close", json={}).status_code == 200
    r = client.post(f"/sessions/{sid}/close", json={})
    assert r.status_code == 409
    assert _error_code(r) == "session_already_closed"


@pytest.mark.parametrize("payload", [
    {"rating": {"score": 3}},
    {"rating": {"score": -3}},
    {"rating": {}},
    {"rating": {"comment": "缺 score"}},
])
def test_close_invalid_rating_rejected_and_session_stays_active(api, payload):
    client, _, pg = api
    sid = client.post("/sessions", json={}).json()["session_id"]
    r = client.post(f"/sessions/{sid}/close", json=payload)
    assert r.status_code == 400
    assert _error_code(r) == "invalid_rating"
    # 评分失败不执行关会话：驱动方可修正后重试（§3.6 执行序）
    assert pg.get_session(sid).status == "active"


def test_close_rating_write_failure_keeps_session_active(api):
    client, _, pg = api
    sid = client.post("/sessions", json={}).json()["session_id"]

    original = pg.update_session_rating
    pg.update_session_rating = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db down")
    )
    try:
        r = client.post(f"/sessions/{sid}/close", json={"rating": {"score": 1}})
    finally:
        pg.update_session_rating = original
    assert r.status_code == 500
    assert _error_code(r) == "rating_failed"
    assert pg.get_session(sid).status == "active"


def test_offline_run_completes_and_processes_closed_session(
    api, fake_llm, pg_available
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    client, graph, pg = api
    sid = client.post("/sessions", json={}).json()["session_id"]
    client.post(f"/sessions/{sid}/message", json={"content": "测试输入"})
    client.post(f"/sessions/{sid}/close", json={})

    # 指定归因 target（改进 54：无 target 不写 evidence，但会话处理流程照走）
    assistant_id = pg.get_recent_dialogues(session_id=sid)[-1].id
    fake_llm.attribution_target_log_id = assistant_id

    r = client.post("/offline", json={})
    assert r.status_code == 200
    assert r.json() == {"status": "completed"}
    candidates = pg.list_closed_sessions_with_unprocessed()
    assert all(s.id != sid for s in candidates)
