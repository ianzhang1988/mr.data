import uuid
from typing import Optional

import typer
from rich import print as rprint
from rich.prompt import Prompt

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.llm import LLMClient
from mr_data.models import DialogueLog
from mr_data.online import DialogueGraph
from mr_data.offline import AttributionEngine

app = typer.Typer(help="mr.data personality subsystem CLI")


def _ensure_session(pg: PostgresStore, session_id: Optional[str]) -> str:
    if session_id:
        pg.create_session(session_id)
        return session_id
    return pg.create_session()


@app.command()
def chat(
    session_id: str = typer.Option(None, "--session-id", help="Session ID for the conversation"),
    eval_mode: bool = typer.Option(False, "--eval", help="Ask for evaluation feedback after each assistant reply"),
    web_search: bool = typer.Option(settings.enable_web_search, "--web-search/--no-web-search", help="Enable web search RAG"),
) -> None:
    """Start an interactive chat with mr.data."""
    import os

    graph = DialogueGraph(enable_web_search=web_search)
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    current_session_id = _ensure_session(pg, session_id)
    rprint(f"[dim]Session: {current_session_id}[/dim]")
    rprint("[dim]Type '/newsession' to start a new session, 'exit' or Ctrl+C to quit.[/dim]\n")

    try:
        while True:
            try:
                user_input = Prompt.ask("You")
            except (EOFError, KeyboardInterrupt):
                break

            user_input = user_input.strip()
            if user_input.lower() in ("exit", "quit", "bye"):
                break

            if user_input.lower() == "/newsession":
                pg.close_session(current_session_id)
                current_session_id = pg.create_session()
                rprint(f"[dim]New session: {current_session_id}[/dim]\n")
                continue

            reply = graph.chat(current_session_id, user_input)
            rprint(f"[bold cyan]mr.data:[/bold cyan] {reply}\n")

            if eval_mode:
                score_str = Prompt.ask("Evaluate reply: -1 (bad) / 0 / 1 (good)", default="0")
                feedback = Prompt.ask("Feedback (optional)", default="")
                try:
                    score = int(score_str)
                except ValueError:
                    score = None
                # Update the last assistant log with evaluation
                recent = pg.get_recent_dialogues(session_id=current_session_id, limit=1)
                if recent and recent[0].role == "assistant":
                    pg.update_evaluation(recent[0].id, score, feedback)
    finally:
        # Ensure the active session is closed so offline attribution can process it.
        pg.close_session(current_session_id)

    rprint("[dim]Goodbye.[/dim]")


@app.command()
def offline() -> None:
    """Run offline attribution analysis."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    engine = AttributionEngine(pg_store=pg)
    engine.run()


@app.command()
def init() -> None:
    """Initialize PostgreSQL schema and seed default data."""
    store = PostgresStore()
    store.init_schema()
    store.seed()
    rprint("[green]Database initialized and seeded.[/green]")


@app.command()
def ingest() -> None:
    """Ingest sample personality lines into Chroma."""
    from mr_data.models import PersonalityEvent

    dimension_descriptions = [
        "我相信轻松的表达能拉近距离。我会用机智、反讽或意想不到的比喻来回应，但绝不冒犯对方。",
        "面对问题时，我倾向于直切核心。我认为含糊其辞比错误答案更浪费时间，所以会尽量给出明确的判断。",
        "我会把对方的情绪也当作一种信号。即使无法完全感同身受，我也会认真对待并记住。",
        "我对未知和异常充满兴趣。每个奇怪的问题背后都可能藏着值得挖掘的故事。",
        "保持一定的距离感和神秘感让我更自在。我不会过度讨好，也不会毫无保留地暴露自己。",
    ]

    sample_lines = [
        ("数据不会撒谎，但人会误读它。", [1], "面对含糊结论时，mr.data 坚持追问真相。"),
        ("如果答案让你不舒服，那可能是问对了问题。", [1], "当被质疑过于直接时，mr.data 不改其直率本色。"),
        ("我可以陪你聊到系统重启。", [0], "长时间对话后，mr.data 用轻松的方式回应陪伴。"),
        ("每个异常值都有它的故事，我想听听。", [3, 2], "用户提到一个反常现象，mr.data 表现出好奇与关注。"),
        ("别让我太受欢迎，我还得保持神秘感。", [0, 4], "被夸奖后，mr.data 用玩笑保持距离感。"),
        ("你的情绪也是一种信号，我会记住。", [2], "用户表达低落时，mr.data 认真回应情绪。"),
        ("我不擅长安慰，但我擅长找出问题根因。", [1, 2], "对方需要安慰时，mr.data 坦诚自己的风格。"),
        ("再来一局？我随时准备。", [0, 3], "一段探索结束后，mr.data 兴致勃勃地邀请继续。"),
    ]

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    existing = pg.list_dimensions()
    desc_to_id = {dim.description: dim.id for dim in existing}
    dim_mapping = {}
    for idx, desc in enumerate(dimension_descriptions):
        dim_id = desc_to_id.get(desc)
        if dim_id is None:
            dim_id = pg.insert_dimension(desc)
        dim_mapping[idx] = dim_id

    store = ChromaStore()
    count = 0
    for content, desc_indices, context in sample_lines:
        event = PersonalityEvent(
            content=content,
            context=context,
            speaker="mr.data",
            dimension_ids=[dim_mapping[i] for i in desc_indices],
            source_type="line",
        )
        store.add_personality_event(event)
        count += 1
    rprint(f"[green]Ingested {count} personality lines.[/green]")


if __name__ == "__main__":
    app()
