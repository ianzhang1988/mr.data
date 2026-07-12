import uuid

import typer
from rich import print as rprint
from rich.prompt import Prompt

from mr_data.db import PostgresStore, ChromaStore
from mr_data.llm import LLMClient
from mr_data.models import DialogueLog
from mr_data.online import DialogueGraph
from mr_data.offline import AttributionEngine

app = typer.Typer(help="mr.data personality subsystem CLI")


@app.command()
def chat(
    session_id: str = typer.Option(None, "--session-id", help="Session ID for the conversation"),
    eval_mode: bool = typer.Option(False, "--eval", help="Ask for evaluation feedback after each assistant reply"),
) -> None:
    """Start an interactive chat with mr.data."""
    session_id = session_id or str(uuid.uuid4())
    rprint(f"[dim]Session: {session_id}[/dim]")
    rprint("[dim]Type 'exit' or press Ctrl+C to quit.[/dim]\n")

    graph = DialogueGraph()
    pg = PostgresStore()

    while True:
        try:
            user_input = Prompt.ask("You")
        except (EOFError, KeyboardInterrupt):
            break

        user_input = user_input.strip()
        if user_input.lower() in ("exit", "quit", "bye"):
            break

        reply = graph.chat(session_id, user_input)
        rprint(f"[bold cyan]mr.data:[/bold cyan] {reply}\n")

        if eval_mode:
            score_str = Prompt.ask("Evaluate reply: -1 (bad) / 0 / 1 (good)", default="0")
            feedback = Prompt.ask("Feedback (optional)", default="")
            try:
                score = int(score_str)
            except ValueError:
                score = None
            # Update the last assistant log with evaluation
            recent = pg.get_recent_dialogues(session_id=session_id, limit=1)
            if recent and recent[0].role == "assistant":
                pg.update_evaluation(recent[0].id, score, feedback)

    rprint("[dim]Goodbye.[/dim]")


@app.command()
def offline() -> None:
    """Run offline attribution analysis."""
    engine = AttributionEngine()
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

    store = ChromaStore()
    sample_lines = [
        ("数据不会撒谎，但人会误读它。", ["直接性", "幽默感"]),
        ("如果答案让你不舒服，那可能是问对了问题。", ["直接性", "好奇心"]),
        ("我可以陪你聊到系统重启。", ["幽默感", "同理心"]),
        ("每个异常值都有它的故事，我想听听。", ["好奇心", "同理心"]),
        ("别让我太受欢迎，我还得保持神秘感。", ["幽默感", "防御性"]),
        ("你的情绪也是一种信号，我会记住。", ["同理心"]),
        ("我不擅长安慰，但我擅长找出问题根因。", ["直接性", "同理心"]),
        ("再来一局？我随时准备。", ["幽默感", "好奇心"]),
    ]
    count = 0
    for content, tags in sample_lines:
        event = PersonalityEvent(content=content, dimension_tags=tags, source_type="line")
        store.add_personality_event(event)
        count += 1
    rprint(f"[green]Ingested {count} personality lines.[/green]")


if __name__ == "__main__":
    app()
