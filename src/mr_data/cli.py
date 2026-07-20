import uuid
from typing import Optional

import typer
from rich import print as rprint
from rich.prompt import Prompt

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.db.personality_loader import load_personality_pack
from mr_data.llm import LLMClient
from mr_data.models import DialogueLog, PersonalityEvent, UserIdentity
from mr_data.online import DialogueGraph
from mr_data.offline import AttributionEngine

app = typer.Typer(help="mr.data personality subsystem CLI")
identity_app = typer.Typer(help="Manage user identities")
app.add_typer(identity_app, name="identity")


def _ensure_session(pg: PostgresStore, session_id: Optional[str]) -> str:
    if session_id:
        pg.create_session(session_id)
        return session_id
    return pg.create_session()


def _print_chat_help(pg: PostgresStore) -> None:
    current = pg.get_current_user_identity()
    current_text = f"{current.name}（{current.role}）" if current else "未设置"
    rprint("""
[bold]mr.data chat 交互命令[/bold]
  /help, /?      显示本帮助信息
  /newsession    结束当前会话并开始新会话
  exit, quit, bye  退出对话

[bold]启动选项[/bold]
  --session-id TEXT    指定会话 ID
  --eval               每轮回复后请求评价
  --web-search / --no-web-search  是否启用网络搜索 RAG

[bold]其他 CLI 命令[/bold]
  chat             启动交互对话
  offline          运行离线归因分析
  init             初始化数据库并写入默认数据
  ingest           导入人格示例台词到向量库
  identity list    列出用户身份
  identity add     添加用户身份
  identity select  切换默认用户身份
  identity edit    编辑用户身份
  identity delete  删除用户身份

[bold]当前用户身份[/bold]: """ + current_text + "\n")


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

            if user_input.lower() in ("/help", "/?"):
                _print_chat_help(pg)
                continue

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
    pack = load_personality_pack()

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    existing = pg.list_dimensions()
    desc_to_id = {dim.description: dim.id for dim in existing}

    # Ensure all dimensions referenced by sample lines exist.
    for desc in pack.sample_lines:
        for dim_desc in desc.dimension_descriptions:
            if dim_desc not in desc_to_id:
                dim_to_id = pg.insert_dimension(dim_desc)
                desc_to_id[dim_desc] = dim_to_id

    store = ChromaStore()
    count = 0
    for line in pack.sample_lines:
        event = PersonalityEvent(
            content=line.content,
            context=line.context,
            speaker=line.speaker,
            dimension_ids=[desc_to_id[d] for d in line.dimension_descriptions if d in desc_to_id],
            source_type="line",
        )
        store.add_personality_event(event)
        count += 1
    rprint(f"[green]Ingested {count} personality lines.[/green]")


@identity_app.command("list")
def identity_list() -> None:
    """List all user identities."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    identities = pg.list_user_identities()
    if not identities:
        rprint("[dim]No user identities found.[/dim]")
        return
    for identity in identities:
        markers = []
        if identity.is_default:
            markers.append("[green]default[/green]")
        if identity.is_protected:
            markers.append("[yellow]protected[/yellow]")
        marker_text = f" ({', '.join(markers)})" if markers else ""
        rprint(f"[bold]{identity.id}[/bold]: {identity.name}{marker_text}")
        rprint(f"  role: {identity.role}")
        rprint(f"  description: {identity.description}")


@identity_app.command("add")
def identity_add(
    name: str = typer.Option(..., prompt=True, help="Unique identity name"),
    role: str = typer.Option(..., prompt=True, help="Identity role"),
    description: str = typer.Option(..., prompt=True, help="Identity description"),
    default: bool = typer.Option(False, "--default", help="Set as default identity"),
) -> None:
    """Add a new user identity."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    try:
        identity_id = pg.insert_user_identity(name, role, description, is_default=default)
        rprint(f"[green]Added identity '{name}' with id {identity_id}.[/green]")
    except Exception as exc:
        rprint(f"[red]Failed to add identity: {exc}[/red]")
        raise typer.Exit(1)


@identity_app.command("select")
def identity_select(
    id_or_name: str = typer.Argument(..., help="Identity id or name"),
) -> None:
    """Set a user identity as the default."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    if pg.set_default_user_identity(id_or_name):
        rprint(f"[green]Set '{id_or_name}' as default identity.[/green]")
    else:
        rprint(f"[red]Identity '{id_or_name}' not found.[/red]")
        raise typer.Exit(1)


@identity_app.command("edit")
def identity_edit(
    id_or_name: str = typer.Argument(..., help="Identity id or name"),
    name: Optional[str] = typer.Option(None, "--name", help="New name"),
    role: Optional[str] = typer.Option(None, "--role", help="New role"),
    description: Optional[str] = typer.Option(None, "--description", help="New description"),
) -> None:
    """Edit a user identity. Protected identities can only have description changed."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    identity = pg.get_user_identity(id_or_name)
    if identity is None:
        rprint(f"[red]Identity '{id_or_name}' not found.[/red]")
        raise typer.Exit(1)
    if identity.is_protected:
        if name is not None or role is not None:
            rprint("[red]Protected identities can only have description edited.[/red]")
            raise typer.Exit(1)
    if not any(v is not None for v in (name, role, description)):
        rprint("[yellow]No changes provided.[/yellow]")
        return
    if pg.update_user_identity(id_or_name, name=name, role=role, description=description):
        rprint(f"[green]Updated identity '{id_or_name}'.[/green]")
    else:
        rprint(f"[red]Failed to update identity '{id_or_name}'.[/red]")
        raise typer.Exit(1)


@identity_app.command("delete")
def identity_delete(
    id_or_name: str = typer.Argument(..., help="Identity id or name"),
    force: bool = typer.Option(False, "--force", help="Force deletion of non-protected identity"),
) -> None:
    """Delete a user identity. Protected identities cannot be deleted."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    identity = pg.get_user_identity(id_or_name)
    if identity is None:
        rprint(f"[red]Identity '{id_or_name}' not found.[/red]")
        raise typer.Exit(1)
    if identity.is_protected:
        rprint(f"[red]Cannot delete protected identity '{identity.name}'.[/red]")
        raise typer.Exit(1)
    if not force:
        confirm = typer.confirm(f"Delete identity '{identity.name}'?")
        if not confirm:
            rprint("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)
    try:
        if pg.delete_user_identity(id_or_name):
            rprint(f"[green]Deleted identity '{identity.name}'.[/green]")
        else:
            rprint(f"[red]Failed to delete identity '{id_or_name}'.[/red]")
            raise typer.Exit(1)
    except ValueError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
