import json
import shutil
import uuid
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.prompt import Prompt

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore, PgEmbedManager
from mr_data.db.chroma import line_doc_id
from mr_data.db.personality_loader import load_personality_pack
from mr_data.llm import LLMClient
from mr_data.logging import get_logger
from mr_data.models import DialogueLog, PersonalityEvent, UserIdentity
from mr_data.online import DialogueGraph
from mr_data.offline import AttributionEngine

logger = get_logger("mr_data.cli")

app = typer.Typer(help="mr.data personality subsystem CLI")
identity_app = typer.Typer(help="Manage user identities")
app.add_typer(identity_app, name="identity")


def _ensure_session(pg: PostgresStore, session_id: Optional[str]) -> str:
    if session_id:
        pg.create_session(session_id)
        return session_id
    return pg.create_session()


def _ask_session_rating(pg: PostgresStore, session_id: str) -> None:
    """会话关闭前询问一次整体评分（-2..2）与可选评论，空输入跳过。"""
    score_str = Prompt.ask(
        "请为本会话评分：-2 很差 / -1 差 / 0 一般 / 1 好 / 2 很好（回车跳过）",
        default="",
    ).strip()
    if not score_str:
        return
    try:
        score = int(score_str)
        if score not in (-2, -1, 0, 1, 2):
            raise ValueError(score_str)
    except ValueError:
        rprint(f"[yellow]无效评分 {score_str!r}，已跳过记录。[/yellow]")
        logger.warning(
            "Invalid session rating input, skipped",
            extra={
                "event": "cli.session_rating_invalid",
                "session_id": session_id,
                "details": {"raw_input": score_str},
            },
        )
        return
    comment = Prompt.ask("评论（可选）", default="").strip()
    pg.update_session_rating(session_id, score, comment or None)


def _print_chat_help(pg: PostgresStore) -> None:
    current = pg.get_current_user_identity()
    current_text = f"{current.name}（{current.role}）" if current else "未设置"
    rprint("""
[bold]mr.data chat 交互命令[/bold]
  /help, /?      显示本帮助信息
  /newsession    结束当前会话并开始新会话
  /exit, /quit, /bye  退出对话

  会话结束（/exit 或 /newsession）时会询问一次整体评分（-2..2）与可选评论，
  供离线归因参考；直接回车可跳过。

[bold]启动选项[/bold]
  --session-id TEXT    指定会话 ID
  --web-search / --no-web-search  是否启用网络搜索 RAG
  --show-references / --hide-references  是否在回复后显示参考来源

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
  chroma-restore   从备份恢复 Chroma 集合并重算向量
  teardown         清空本地数据目录，回到全新状态

[bold]当前用户身份[/bold]: """ + current_text + "\n")


@app.command()
def chat(
    session_id: str = typer.Option(None, "--session-id", help="Session ID for the conversation"),
    web_search: bool = typer.Option(settings.enable_web_search, "--web-search/--no-web-search", help="Enable web search RAG"),
    show_references: bool = typer.Option(settings.show_references, "--show-references/--hide-references", help="Show reference sources after each reply"),
) -> None:
    """Start an interactive chat with mr.data."""
    import os

    graph = DialogueGraph(enable_web_search=web_search)
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    current_session_id = _ensure_session(pg, session_id)
    rprint(f"[dim]Session: {current_session_id}[/dim]")
    rprint("[dim]Type '/newsession' to start a new session, '/exit' or Ctrl+C to quit.[/dim]\n")

    try:
        while True:
            try:
                user_input = Prompt.ask("You")
            except (EOFError, KeyboardInterrupt):
                break

            user_input = user_input.strip()
            if user_input.lower() in ("/exit", "/quit", "/bye"):
                break

            if user_input.lower() in ("/help", "/?"):
                _print_chat_help(pg)
                continue

            if user_input.lower() == "/newsession":
                _ask_session_rating(pg, current_session_id)
                pg.close_session(current_session_id)
                current_session_id = pg.create_session()
                rprint(f"[dim]New session: {current_session_id}[/dim]\n")
                continue

            try:
                result = graph.chat(current_session_id, user_input)
            except Exception as exc:
                logger.warning(
                    "Chat turn failed",
                    extra={
                        "event": "cli.chat_turn_failed",
                        "session_id": current_session_id,
                        "details": {"error": str(exc)},
                    },
                )
                rprint(f"[red]本轮对话出错：{exc}[/red]\n")
                continue
            rprint(f"[bold cyan]mr.data:[/bold cyan] {result.text}\n")
            if show_references and result.blocks:
                import json

                rprint("[dim]回复块与引用：[/dim]")
                rprint(
                    json.dumps(
                        [block.model_dump() for block in result.blocks],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                rprint()

    finally:
        # Ensure the active session is closed so offline attribution can process it.
        try:
            _ask_session_rating(pg, current_session_id)
        except Exception as exc:
            logger.warning(
                "Failed to ask session rating",
                extra={
                    "event": "cli.session_rating_failed",
                    "session_id": current_session_id,
                    "details": {"error": str(exc)},
                },
            )
        try:
            pg.close_session(current_session_id)
        except Exception as exc:
            logger.warning(
                "Failed to close session",
                extra={
                    "event": "cli.close_session_failed",
                    "session_id": current_session_id,
                    "details": {"error": str(exc)},
                },
            )

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

    store = ChromaStore()
    existing_ids = set(store.personality.get()["ids"])
    added = 0
    skipped = 0
    for line in pack.sample_lines:
        doc_id = line_doc_id(line.speaker, line.content, line.context)
        if doc_id in existing_ids:
            skipped += 1
            continue
        event = PersonalityEvent(
            id=doc_id,
            content=line.content,
            context=line.context,
            speaker=line.speaker,
            source_type="line",
        )
        store.add_personality_event(event)
        existing_ids.add(doc_id)
        added += 1
    rprint(f"[green]Ingested {added} personality lines ({skipped} skipped, already present).[/green]")


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


@app.command("chroma-restore")
def chroma_restore(
    backup_path: str = typer.Argument(..., help="Chroma 备份 JSON 路径（recreate 时自动导出）"),
    force: bool = typer.Option(False, "--force", help="目标集合非空时跳过确认"),
) -> None:
    """Restore a Chroma collection backup, recomputing embeddings with the current model."""
    path = Path(backup_path)
    if not path.is_file():
        rprint(f"[red]Backup file not found: {path}[/red]")
        raise typer.Exit(1)
    payload = json.loads(path.read_text(encoding="utf-8"))
    name = payload.get("collection")
    if name not in ("personality", "memories"):
        rprint(f"[red]Unknown collection {name!r} in backup {path}.[/red]")
        raise typer.Exit(1)

    store = ChromaStore()
    target = store.personality if name == "personality" else store.memories
    existing = target.count()
    if existing > 0 and not force:
        confirm = typer.confirm(
            f"Collection '{name}' already has {existing} document(s); restore anyway (existing ids are skipped)?"
        )
        if not confirm:
            rprint("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)
    report = store.restore_collection_backup(path)
    rprint(
        f"[green]Restored {report['restored']} document(s) into '{report['collection']}' "
        f"({report['skipped']} skipped, already present).[/green]"
    )


@app.command()
def teardown(
    force: bool = typer.Option(False, "--force", help="跳过删除确认"),
) -> None:
    """删除本地数据目录（pgembed + chroma），回到全新状态。"""
    if settings.postgres_dsn:
        rprint(
            "[red]teardown 仅支持 pgembed 嵌入式模式；当前配置了外部 PostgreSQL DSN，"
            "请手工清理对应数据库。[/red]"
        )
        logger.warning(
            "teardown refused: external postgres_dsn configured",
            extra={
                "event": "cli.teardown_refused",
                "details": {"postgres_dsn": settings.postgres_dsn},
            },
        )
        raise typer.Exit(1)

    pgembed_dir = Path(settings.pgembed_data_dir)
    chroma_dir = Path(settings.chroma_persist_dir)
    if not force:
        confirm = typer.confirm(
            "将删除以下目录及其全部内容：\n"
            f"  - {pgembed_dir}\n"
            f"  - {chroma_dir}\n"
            "确认执行 teardown？"
        )
        if not confirm:
            rprint("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    # 先停 pgembed 集群，确保没有进程占用数据目录（未启动时 stop 是安全的）。
    PgEmbedManager().stop()
    removed = []
    for target in (pgembed_dir, chroma_dir):
        shutil.rmtree(target, ignore_errors=True)
        removed.append(str(target))
    rprint(f"[green]Teardown 完成，已删除：{'、'.join(removed)}[/green]")
    logger.info(
        "teardown completed",
        extra={
            "event": "cli.teardown",
            "details": {"removed_dirs": removed},
        },
    )


if __name__ == "__main__":
    app()
