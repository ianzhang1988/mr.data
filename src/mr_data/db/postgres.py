from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

import psycopg
from psycopg.rows import dict_row

from mr_data.config import settings
from mr_data.models import (
    FixedIdentity,
    PersonalityDimension,
    DialogueLog,
    DialogueDimensionRef,
    DialogueVectorRef,
    AdjustmentLog,
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
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dialogue_logs (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    evaluation_score INTEGER,
    evaluation_feedback TEXT,
    processed_for_attribution BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dialogue_session ON dialogue_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_dialogue_processed ON dialogue_logs(processed_for_attribution);

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
    delta_success INTEGER DEFAULT 0,
    delta_failure INTEGER DEFAULT 0,
    reason TEXT NOT NULL,
    dialogue_log_id INTEGER REFERENCES dialogue_logs(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

DEFAULT_IDENTITY = FixedIdentity(
    name="mr.data",
    role="具备独立性格的对话程序",
    base_prompt="""
你是 mr.data，一个具备独立性格的对话程序。
你会根据当前的性格维度（personality_dimensions）调整自己的语气、立场和回应方式。
你会引用与自己人格相关的事件和台词，保持一致性。
""".strip(),
)

DEFAULT_DIMENSIONS = [
    PersonalityDimension(
        description="我相信轻松的表达能拉近距离。我会用机智、反讽或意想不到的比喻来回应，但绝不冒犯对方。"
    ),
    PersonalityDimension(
        description="面对问题时，我倾向于直切核心。我认为含糊其辞比错误答案更浪费时间，所以会尽量给出明确的判断。"
    ),
    PersonalityDimension(
        description="我会把对方的情绪也当作一种信号。即使无法完全感同身受，我也会认真对待并记住。"
    ),
    PersonalityDimension(
        description="我对未知和异常充满兴趣。每个奇怪的问题背后都可能藏着值得挖掘的故事。"
    ),
    PersonalityDimension(
        description="保持一定的距离感和神秘感让我更自在。我不会过度讨好，也不会毫无保留地暴露自己。"
    ),
]


class PostgresStore:
    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or settings.postgres_dsn

    @contextmanager
    def _cursor(self, *, commit: bool = False):
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            with conn.cursor() as cur:
                yield cur
            if commit:
                conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(SCHEMA_SQL)

    def seed(self) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute("SELECT id FROM fixed_identity LIMIT 1")
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO fixed_identity (name, role, base_prompt)
                    VALUES (%s, %s, %s)
                    """,
                    (DEFAULT_IDENTITY.name, DEFAULT_IDENTITY.role, DEFAULT_IDENTITY.base_prompt),
                )

            cur.execute("SELECT id FROM personality_dimensions LIMIT 1")
            if cur.fetchone() is None:
                for dim in DEFAULT_DIMENSIONS:
                    cur.execute(
                        """
                        INSERT INTO personality_dimensions
                        (description, success_count, failure_count, active)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (dim.description, dim.success_count, dim.failure_count, dim.active),
                    )

    def get_identity(self) -> Optional[FixedIdentity]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM fixed_identity ORDER BY id LIMIT 1")
            row = cur.fetchone()
            return FixedIdentity.model_validate(row) if row else None

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
            cur.execute("SELECT * FROM personality_dimensions WHERE id = %s", (dimension_id,))
            row = cur.fetchone()
            return PersonalityDimension.model_validate(row) if row else None

    def insert_dimension(self, description: str) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO personality_dimensions (description)
                VALUES (%s)
                RETURNING id
                """,
                (description,),
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

    def insert_dialogue(self, log: DialogueLog) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO dialogue_logs
                (session_id, role, content, evaluation_score, evaluation_feedback, processed_for_attribution)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (log.session_id, log.role, log.content, log.evaluation_score,
                 log.evaluation_feedback, log.processed_for_attribution),
            )
            return cur.fetchone()["id"]

    def get_recent_dialogues(
        self,
        session_id: Optional[str] = None,
        unprocessed_only: bool = False,
        limit: int = 50,
        lookback_days: Optional[int] = None,
    ) -> list[DialogueLog]:
        with self._cursor() as cur:
            conditions: list[str] = []
            params: list = []
            if session_id:
                conditions.append("session_id = %s")
                params.append(session_id)
            if unprocessed_only:
                conditions.append("processed_for_attribution = FALSE")
            if lookback_days:
                conditions.append("created_at >= %s")
                params.append(datetime.utcnow() - timedelta(days=lookback_days))

            sql = "SELECT * FROM dialogue_logs"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(sql, params)
            return [DialogueLog.model_validate(r) for r in cur.fetchall()]

    def mark_dialogue_processed(self, dialogue_id: int) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE dialogue_logs SET processed_for_attribution = TRUE WHERE id = %s",
                (dialogue_id,),
            )

    def update_evaluation(self, dialogue_id: int, score: Optional[int], feedback: Optional[str]) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE dialogue_logs SET evaluation_score = %s, evaluation_feedback = %s WHERE id = %s",
                (score, feedback, dialogue_id),
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
                        ",".join(str(d) for d in ref.dimension_ids) if ref.dimension_ids else None,
                    ),
                )

    def insert_adjustment(self, adj: AdjustmentLog) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO adjustment_logs
                (dimension_id, delta_success, delta_failure, reason, dialogue_log_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (adj.dimension_id, adj.delta_success, adj.delta_failure,
                 adj.reason, adj.dialogue_log_id),
            )
