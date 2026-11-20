import json
import os
import threading
import uuid
from contextlib import contextmanager
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from mr_data.config import settings
from mr_data.db.pgembed_manager import PgEmbedManager
from mr_data.db.personality_loader import load_personality_pack
from mr_data.models import (
    FixedIdentity,
    UserIdentity,
    PersonalityDimension,
    Session,
    DialogueLog,
    DialogueDimensionRef,
    DialogueVectorRef,
    AdjustmentLog,
)


PICARD_IDENTITY = UserIdentity(
    name="Picard",
    role="星际联邦星舰企业号-D 舰长",
    description=(
        "当前用户是 Jean-Luc Picard，星际联邦星舰企业号-D（USS Enterprise-D）的舰长，"
        "也是星际舰队旗舰的指挥官。他以睿智、冷静、坚定而著称，重视原则、外交与探索，"
        "常在压力下做出公正决策；他热爱莎士比亚、古典音乐和一杯热 Earl Grey 茶；"
        "他信任并倾听下属意见，但在关键时刻会果断下令“Make it so”。"
        "请始终以正式、尊敬的语气与他交谈，称呼他为“舰长”。"
    ),
    is_default=True,
    is_protected=True,
)

NORMAL_USER_IDENTITY = UserIdentity(
    name="User",
    role="普通用户",
    description="没有特殊身份说明。",
    is_default=False,
    is_protected=True,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fixed_identity (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    base_prompt TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS personality_dimensions (
    id SERIAL PRIMARY KEY,
    description TEXT NOT NULL,
    core BOOLEAN DEFAULT FALSE,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE personality_dimensions
    ADD COLUMN IF NOT EXISTS core BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS rating INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS rating_comment TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS attribution_attempts INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS dialogue_logs (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    attribution_status TEXT NOT NULL DEFAULT 'unprocessed',
    metadata JSONB DEFAULT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 改进 99：归因状态三态化迁移——旧 boolean 列回填后删除（历史死信行
-- 无法回溯区分，一律按 processed 迁移，等于旧标记的诚实上限）
ALTER TABLE dialogue_logs ADD COLUMN IF NOT EXISTS attribution_status TEXT NOT NULL DEFAULT 'unprocessed';
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'dialogue_logs' AND column_name = 'processed_for_attribution') THEN
        UPDATE dialogue_logs SET attribution_status = 'processed' WHERE processed_for_attribution;
        ALTER TABLE dialogue_logs DROP COLUMN processed_for_attribution;
    END IF;
END $$;

ALTER TABLE dialogue_logs ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT NULL;

ALTER TABLE dialogue_logs DROP COLUMN IF EXISTS evaluation_score;
ALTER TABLE dialogue_logs DROP COLUMN IF EXISTS evaluation_feedback;

CREATE INDEX IF NOT EXISTS idx_dialogue_session ON dialogue_logs(session_id);
DROP INDEX IF EXISTS idx_dialogue_processed;
CREATE INDEX IF NOT EXISTS idx_dialogue_attribution_status ON dialogue_logs(attribution_status);

CREATE TABLE IF NOT EXISTS dialogue_dimension_refs (
    id SERIAL PRIMARY KEY,
    dialogue_log_id INTEGER NOT NULL REFERENCES dialogue_logs(id) ON DELETE CASCADE,
    dimension_id INTEGER NOT NULL REFERENCES personality_dimensions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(dialogue_log_id, dimension_id)
);

CREATE TABLE IF NOT EXISTS dialogue_vector_refs (
    id SERIAL PRIMARY KEY,
    dialogue_log_id INTEGER NOT NULL REFERENCES dialogue_logs(id) ON DELETE CASCADE,
    vector_doc_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    content TEXT NOT NULL,
    dimension_ids TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vector_ref_dialogue ON dialogue_vector_refs(dialogue_log_id);

CREATE TABLE IF NOT EXISTS adjustment_logs (
    id SERIAL PRIMARY KEY,
    dimension_id INTEGER NOT NULL REFERENCES personality_dimensions(id),
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    delta_success INTEGER DEFAULT 0,
    delta_failure INTEGER DEFAULT 0,
    reason TEXT NOT NULL,
    dialogue_log_id INTEGER REFERENCES dialogue_logs(id),
    event_summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE adjustment_logs ADD COLUMN IF NOT EXISTS event_summary TEXT;

CREATE INDEX IF NOT EXISTS idx_adjustment_session ON adjustment_logs(session_id);

CREATE TABLE IF NOT EXISTS user_identities (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    description TEXT NOT NULL,
    is_default BOOLEAN DEFAULT FALSE,
    is_protected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_identities_default ON user_identities(is_default);
"""

_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()


def _get_pool(dsn: str) -> ConnectionPool:
    """按 dsn 进程级共享连接池（测试中大量临时 PostgresStore 实例从不 close，
    共享池避免每实例一条 min_size 连接累积撑爆 pgembed）。
    查-建-放整体持锁：多线程首次并发建池时只建一个。"""
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=8,
                kwargs={"row_factory": dict_row},
                open=False,
            )
            pool.open()
            _POOLS[dsn] = pool
        return pool


class PostgresStore:
    def __init__(self, dsn: Optional[str] = None):
        if dsn:
            self.dsn = dsn
        elif os.environ.get("MR_DATA_POSTGRES_DSN"):
            # Allow runtime env overrides (e.g. test fixtures).
            self.dsn = os.environ["MR_DATA_POSTGRES_DSN"]
        elif settings.postgres_dsn:
            self.dsn = settings.postgres_dsn
        elif settings.use_pgembed:
            self.dsn = PgEmbedManager(
                data_dir=settings.pgembed_data_dir).get_dsn()
        else:
            raise RuntimeError(
                "No PostgreSQL DSN configured. Set MR_DATA_POSTGRES_DSN or MR_DATA_USE_PGEMBED=true."
            )
        # 事务槽按线程隔离：server 线程池下多线程共享同一实例（改进 93）
        self._tx = threading.local()

    @contextmanager
    def transaction(self):
        """会话级事务：事务态下 _cursor 复用同一连接并抑制逐方法 commit。"""
        if getattr(self._tx, "conn", None) is not None:
            raise RuntimeError("PostgresStore: nested transactions are not supported")
        with _get_pool(self.dsn).connection() as conn:
            self._tx.conn = conn
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._tx.conn = None

    @contextmanager
    def _cursor(self, *, commit: bool = False):
        tx_conn = getattr(self._tx, "conn", None)
        if tx_conn is not None:
            with tx_conn.cursor() as cur:
                yield cur
            return
        with _get_pool(self.dsn).connection() as conn:
            with conn.cursor() as cur:
                yield cur
            if commit:
                conn.commit()

    def init_schema(self) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(SCHEMA_SQL)

    def seed(self) -> None:
        pack = load_personality_pack()
        with self._cursor(commit=True) as cur:
            cur.execute("SELECT id FROM fixed_identity LIMIT 1")
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO fixed_identity (name, role, base_prompt)
                    VALUES (%s, %s, %s)
                    """,
                    (pack.identity.name, pack.identity.role,
                     pack.identity.base_prompt),
                )

            cur.execute("SELECT id FROM personality_dimensions LIMIT 1")
            if cur.fetchone() is None:
                for dim in pack.dimensions:
                    cur.execute(
                        """
                        INSERT INTO personality_dimensions
                        (description, core, success_count, failure_count, active)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (dim.description, dim.core, dim.success_count,
                         dim.failure_count, dim.active),
                    )

            cur.execute("SELECT id FROM user_identities LIMIT 1")
            if cur.fetchone() is None:
                for user_identity in (PICARD_IDENTITY, NORMAL_USER_IDENTITY):
                    cur.execute(
                        """
                        INSERT INTO user_identities
                        (name, role, description, is_default, is_protected)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            user_identity.name,
                            user_identity.role,
                            user_identity.description,
                            user_identity.is_default,
                            user_identity.is_protected,
                        ),
                    )

    def get_identity(self) -> Optional[FixedIdentity]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM fixed_identity ORDER BY id LIMIT 1")
            row = cur.fetchone()
            return FixedIdentity.model_validate(row) if row else None

    # --- User identity management ---

    def list_user_identities(self) -> list[UserIdentity]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM user_identities ORDER BY id")
            return [UserIdentity.model_validate(r) for r in cur.fetchall()]

    def _resolve_user_identity(self, id_or_name: str) -> Optional[UserIdentity]:
        with self._cursor() as cur:
            if id_or_name.isdigit():
                cur.execute("SELECT * FROM user_identities WHERE id = %s", (int(id_or_name),))
            else:
                cur.execute("SELECT * FROM user_identities WHERE name = %s", (id_or_name,))
            row = cur.fetchone()
            return UserIdentity.model_validate(row) if row else None

    def get_user_identity(self, id_or_name: str) -> Optional[UserIdentity]:
        return self._resolve_user_identity(id_or_name)

    def get_current_user_identity(self) -> Optional[UserIdentity]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM user_identities WHERE is_default = TRUE ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if row:
                return UserIdentity.model_validate(row)
            cur.execute("SELECT * FROM user_identities WHERE is_protected = TRUE ORDER BY id LIMIT 1")
            row = cur.fetchone()
            return UserIdentity.model_validate(row) if row else None

    def insert_user_identity(
        self,
        name: str,
        role: str,
        description: str,
        is_default: bool = False,
        is_protected: bool = False,
    ) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO user_identities (name, role, description, is_default, is_protected)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (name, role, description, is_default, is_protected),
            )
            identity_id = cur.fetchone()["id"]
            if is_default:
                cur.execute(
                    "UPDATE user_identities SET is_default = FALSE WHERE id != %s",
                    (identity_id,),
                )
            return identity_id

    def update_user_identity(
        self,
        id_or_name: str,
        name: Optional[str] = None,
        role: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        identity = self._resolve_user_identity(id_or_name)
        if identity is None:
            return False
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE user_identities
                SET name = COALESCE(%s, name),
                    role = COALESCE(%s, role),
                    description = COALESCE(%s, description),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (name, role, description, identity.id),
            )
            return cur.rowcount > 0

    def delete_user_identity(self, id_or_name: str) -> bool:
        identity = self._resolve_user_identity(id_or_name)
        if identity is None:
            return False
        if identity.is_protected:
            raise ValueError(f"Cannot delete protected identity '{identity.name}'")
        with self._cursor(commit=True) as cur:
            cur.execute("DELETE FROM user_identities WHERE id = %s", (identity.id,))
            if identity.is_default:
                cur.execute(
                    """
                    UPDATE user_identities
                    SET is_default = TRUE
                    WHERE id = (
                        SELECT id FROM user_identities
                        WHERE is_protected = TRUE ORDER BY id LIMIT 1
                    )
                    """
                )
            return cur.rowcount > 0

    def set_default_user_identity(self, id_or_name: str) -> bool:
        identity = self._resolve_user_identity(id_or_name)
        if identity is None:
            return False
        with self._cursor(commit=True) as cur:
            cur.execute("UPDATE user_identities SET is_default = FALSE")
            cur.execute(
                "UPDATE user_identities SET is_default = TRUE WHERE id = %s",
                (identity.id,),
            )
            return cur.rowcount > 0

    def list_dimensions(self, active_only: bool = False) -> list[PersonalityDimension]:
        with self._cursor() as cur:
            sql = "SELECT * FROM personality_dimensions"
            if active_only:
                sql += " WHERE active = TRUE"
            sql += " ORDER BY id"
            cur.execute(sql)
            return [PersonalityDimension.model_validate(r) for r in cur.fetchall()]

    def get_dimension(self, dimension_id: int) -> Optional[PersonalityDimension]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM personality_dimensions WHERE id = %s", (dimension_id,))
            row = cur.fetchone()
            return PersonalityDimension.model_validate(row) if row else None

    def insert_dimension(self, description: str, core: bool = False) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO personality_dimensions (description, core)
                VALUES (%s, %s)
                RETURNING id
                """,
                (description, core),
            )
            return cur.fetchone()["id"]

    def update_dimension(
        self,
        dimension_id: int,
        delta_success: int = 0,
        delta_failure: int = 0,
    ) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE personality_dimensions
                SET success_count = success_count + %s,
                    failure_count = failure_count + %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (delta_success, delta_failure, dimension_id),
            )

    def deactivate_dimension(self, dimension_id: int) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE personality_dimensions SET active = FALSE, updated_at = NOW() WHERE id = %s",
                (dimension_id,),
            )

    # --- Manual dimension editing (dimension mode, 改进 90) ---

    def update_dimension_description(self, dimension_id: int, description: str) -> None:
        """人工编辑描述（维度编辑模式专用）；core 拒绝。只动 description+updated_at。"""
        dim = self.get_dimension(dimension_id)
        if dim is None:
            raise ValueError(f"dimension {dimension_id} not found")
        if dim.core:
            raise ValueError(f"core dimension {dimension_id} is read-only")
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE personality_dimensions SET description = %s, updated_at = NOW() WHERE id = %s",
                (description, dimension_id),
            )

    def set_dimension_active(self, dimension_id: int, active: bool) -> None:
        """人工停用/复活（维度编辑模式专用）；core 拒绝。
        复活（active=True）同时清零计数——旧计数否则会按 0.6 失败率阈值
        在下一轮归因立即再次触发淘汰，复活形同虚设。"""
        dim = self.get_dimension(dimension_id)
        if dim is None:
            raise ValueError(f"dimension {dimension_id} not found")
        if dim.core:
            raise ValueError(f"core dimension {dimension_id} is read-only")
        with self._cursor(commit=True) as cur:
            if active:
                cur.execute(
                    """
                    UPDATE personality_dimensions
                    SET active = TRUE, success_count = 0, failure_count = 0, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (dimension_id,),
                )
            else:
                cur.execute(
                    "UPDATE personality_dimensions SET active = FALSE, updated_at = NOW() WHERE id = %s",
                    (dimension_id,),
                )

    def delete_dimension(self, dimension_id: int) -> int:
        """删除已停用维度（维度编辑模式专用）；core 拒绝、仅 active=False 可删。
        adjustment_logs.dimension_id 的 FK 无级联动作，先显式删其行（返回条数，
        供删除前展示与审计日志）；dialogue_dimension_refs 由 ON DELETE CASCADE 自动清理。
        Chroma 清理不在 store 层，由模式层编排 purge。"""
        dim = self.get_dimension(dimension_id)
        if dim is None:
            raise ValueError(f"dimension {dimension_id} not found")
        if dim.core:
            raise ValueError(f"core dimension {dimension_id} is read-only")
        if dim.active:
            raise ValueError(f"dimension {dimension_id} is still active")
        with self._cursor(commit=True) as cur:
            cur.execute("DELETE FROM adjustment_logs WHERE dimension_id = %s", (dimension_id,))
            adjustments = cur.rowcount
            cur.execute("DELETE FROM personality_dimensions WHERE id = %s", (dimension_id,))
        return adjustments

    def count_dimension_references(self, dimension_id: int) -> dict:
        """统计维度被引用情况（维度删除前展示用）：
        dialogue_dimension_refs 与 adjustment_logs 各一条 count 查询。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM dialogue_dimension_refs WHERE dimension_id = %s",
                (dimension_id,),
            )
            ref_count = cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM adjustment_logs WHERE dimension_id = %s",
                (dimension_id,),
            )
            adjustment_count = cur.fetchone()["n"]
        return {"ref_count": ref_count, "adjustment_count": adjustment_count}

    # --- Session management ---

    def create_session(self, session_id: Optional[str] = None) -> str:
        sid = session_id or str(uuid.uuid4())
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO sessions (id, status)
                VALUES (%s, 'active')
                ON CONFLICT (id) DO UPDATE SET status = 'active', closed_at = NULL,
                    rating = NULL, rating_comment = NULL
                RETURNING id
                """,
                (sid,),
            )
            return cur.fetchone()["id"]

    def close_session(self, session_id: str) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE sessions
                SET status = 'closed', closed_at = NOW()
                WHERE id = %s
                """,
                (session_id,),
            )

    def get_session(self, session_id: str) -> Optional[Session]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM sessions WHERE id = %s", (session_id,))
            row = cur.fetchone()
            return Session.model_validate(row) if row else None

    def list_closed_sessions_with_unprocessed(
        self, limit: int = 50
    ) -> list[Session]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT s.*
                FROM sessions s
                JOIN dialogue_logs d ON d.session_id = s.id
                WHERE s.status = 'closed'
                  AND d.attribution_status = 'unprocessed'
                ORDER BY s.closed_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [Session.model_validate(r) for r in cur.fetchall()]

    # --- Dialogue logs ---

    def insert_dialogue(self, log: DialogueLog) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO dialogue_logs
                (session_id, role, content, attribution_status, metadata)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (log.session_id, log.role, log.content,
                 log.attribution_status,
                 json.dumps(log.metadata.model_dump(mode="json")) if log.metadata is not None else None),
            )
            return cur.fetchone()["id"]

    def get_recent_dialogues(
        self,
        session_id: Optional[str] = None,
        unprocessed_only: bool = False,
        limit: int = 50,
    ) -> list[DialogueLog]:
        with self._cursor() as cur:
            conditions: list[str] = []
            params: list = []
            if session_id:
                conditions.append("session_id = %s")
                params.append(session_id)
            if unprocessed_only:
                conditions.append("attribution_status = 'unprocessed'")

            sql = "SELECT * FROM dialogue_logs"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC, id DESC LIMIT %s"
            params.append(limit)

            cur.execute(sql, params)
            return [DialogueLog.model_validate(r) for r in cur.fetchall()]

    def get_adjacent_processed_dialogues(
        self,
        session_id: str,
        after_created_at,
        after_id: int,
        limit: int,
    ) -> list[DialogueLog]:
        """取已处理行中按时间紧邻界线上方的 limit 条（最早优先）。

        拆批上文回顾用（改进 82）：界线为本批最新一行，其上方（更新）的已处理
        行即按时间紧邻本批的上文。单独查询而非与大窗口同取，保证窗口推进到
        任意深度都能取到未处理行。created_at 为 NULL 的行不参与比较。
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM dialogue_logs
                WHERE session_id = %s
                  AND attribution_status = 'processed'
                  AND (created_at, id) > (%s, %s)
                ORDER BY created_at ASC, id ASC
                LIMIT %s
                """,
                (session_id, after_created_at, after_id, limit),
            )
            return [DialogueLog.model_validate(r) for r in cur.fetchall()]

    def count_dialogues(self, session_id: str, unprocessed_only: bool = False) -> int:
        """统计会话日志条数（拆批批号 x/y 计算用）。

        unprocessed_only=True 计 attribution_status = 'unprocessed'；否则计
        <> 'unprocessed'（含 failed 死信行——死信同样了结于窗口，批号推进
        不回退，保持改进 82 的拆批语义）。
        """
        with self._cursor() as cur:
            op = "=" if unprocessed_only else "<>"
            sql = (
                "SELECT COUNT(*) AS n FROM dialogue_logs "
                f"WHERE session_id = %s AND attribution_status {op} 'unprocessed'"
            )
            cur.execute(sql, (session_id,))
            return cur.fetchone()["n"]

    def mark_dialogue_processed(self, dialogue_id: int) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE dialogue_logs SET attribution_status = 'processed' WHERE id = %s",
                (dialogue_id,),
            )

    def increment_attribution_attempts(self, session_id: str) -> int:
        """归因失败计数 +1，返回累计次数（死信判定依据）。"""
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE sessions SET attribution_attempts = attribution_attempts + 1
                WHERE id = %s
                RETURNING attribution_attempts
                """,
                (session_id,),
            )
            row = cur.fetchone()
            return row["attribution_attempts"] if row else 0

    def mark_dialogues_failed(self, dialogue_ids: list[int]) -> int:
        """把指定日志置为 failed 死信（分片级落葬，改进 99），返回标记条数。

        只覆盖 unprocessed 行，已 processed 行不动。"""
        if not dialogue_ids:
            return 0
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE dialogue_logs SET attribution_status = 'failed'
                WHERE id = ANY(%s) AND attribution_status = 'unprocessed'
                """,
                (dialogue_ids,),
            )
            return cur.rowcount

    def update_session_rating(
        self, session_id: str, rating: Optional[int], comment: Optional[str]
    ) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE sessions SET rating = %s, rating_comment = %s WHERE id = %s",
                (rating, comment, session_id),
            )

    def insert_dialogue_dimension_refs(
        self, dialogue_log_id: int, dimension_ids: list[int]
    ) -> None:
        if not dimension_ids:
            return
        with self._cursor(commit=True) as cur:
            for dim_id in dimension_ids:
                cur.execute(
                    """
                    INSERT INTO dialogue_dimension_refs (dialogue_log_id, dimension_id)
                    VALUES (%s, %s)
                    ON CONFLICT (dialogue_log_id, dimension_id) DO NOTHING
                    """,
                    (dialogue_log_id, dim_id),
                )

    def list_session_dimension_ids(self, session_id: str) -> list[int]:
        """Return distinct dimension ids actually selected in the given session."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT r.dimension_id FROM dialogue_dimension_refs r
                JOIN dialogue_logs l ON l.id = r.dialogue_log_id
                WHERE l.session_id = %s
                ORDER BY r.dimension_id
                """,
                (session_id,),
            )
            return [row["dimension_id"] for row in cur.fetchall()]

    def insert_dialogue_vector_refs(
        self, dialogue_log_id: int, refs: list[DialogueVectorRef]
    ) -> None:
        if not refs:
            return
        with self._cursor(commit=True) as cur:
            for ref in refs:
                cur.execute(
                    """
                    INSERT INTO dialogue_vector_refs
                    (dialogue_log_id, vector_doc_id, source_type, content, dimension_ids)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        dialogue_log_id,
                        ref.vector_doc_id,
                        ref.source_type,
                        ref.content,
                        ",".join(
                            str(d) for d in ref.dimension_ids) if ref.dimension_ids else None,
                    ),
                )

    def get_dialogue_vector_refs_by_dimension(
        self, dimension_id: int
    ) -> list[DialogueVectorRef]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM dialogue_vector_refs
                WHERE dimension_ids IS NOT NULL
                ORDER BY id
                """,
            )
            rows = cur.fetchall()
            refs = []
            for row in rows:
                dim_ids_str = row.get("dimension_ids") or ""
                dim_ids = [int(x) for x in dim_ids_str.split(",") if x]
                if dimension_id in dim_ids:
                    row["dimension_ids"] = dim_ids
                    refs.append(DialogueVectorRef.model_validate(row))
            return refs

    def delete_dialogue_vector_refs_by_dimension(self, dimension_id: int) -> None:
        refs = self.get_dialogue_vector_refs_by_dimension(dimension_id)
        if not refs:
            return
        with self._cursor(commit=True) as cur:
            for ref in refs:
                cur.execute(
                    "DELETE FROM dialogue_vector_refs WHERE id = %s", (ref.id,))

    def insert_adjustment(self, adj: AdjustmentLog) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO adjustment_logs
                (dimension_id, session_id, delta_success, delta_failure, reason, dialogue_log_id,
                 event_summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (adj.dimension_id, adj.session_id, adj.delta_success, adj.delta_failure,
                 adj.reason, adj.dialogue_log_id, adj.event_summary),
            )
