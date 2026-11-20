import json
import shutil
import threading
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
from mr_data.interactive.dimensions import cmd_list
from mr_data.llm import LLMClient
from mr_data.logging import get_logger
from mr_data.models import DialogueLog, PersonalityEvent, UserIdentity
from mr_data.offline import (
    AttributionEngine,
    OfflineScheduler,
    guarded_offline_run,
)
from mr_data.runtime import build_runtime

logger = get_logger("mr_data.cli")

app = typer.Typer(help="mr.data personality subsystem CLI")
identity_app = typer.Typer(help="Manage user identities")
app.add_typer(identity_app, name="identity")
dimension_app = typer.Typer(help="Manage personality dimensions")
app.add_typer(dimension_app, name="dimension")


@dimension_app.callback(invoke_without_command=True)
def dimension_mode(ctx: typer.Context) -> None:
    """无子命令时直接进入维度编辑模式（改进 90）。"""
    if ctx.invoked_subcommand is not None:
        return
    _run_dimension_mode()


@dimension_app.command("list")
def dimension_list() -> None:
    """List all personality dimensions (one-shot)."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    cmd_list(pg, [])


@dimension_app.command("edit")
def dimension_edit(
    dimension_id: Optional[int] = typer.Argument(None, help="Dimension id (omit to enter edit mode)"),
    description: Optional[str] = typer.Option(None, "--description", help="New description (one-shot)"),
    force: bool = typer.Option(False, "--force", help="Skip confirmation (one-shot)"),
) -> None:
    """Edit a dimension description; without arguments enters edit mode."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    if dimension_id is None:
        _run_dimension_mode(pg)
        return
    if description is None:
        rprint("[red]one-shot 编辑需指定 --description；或省略参数进入维度编辑模式。[/red]")
        raise typer.Exit(1)
    if not force and not typer.confirm(f"确认修改维度 {dimension_id} 的描述？"):
        rprint("[dim]已取消。[/dim]")
        return
    old_dim = pg.get_dimension(dimension_id)
    try:
        pg.update_dimension_description(dimension_id, description.strip())
    except ValueError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    logger.info(
        "dimension_edit",
        extra={
            "event": "dimension_edit",
            "details": {
                "dimension_id": dimension_id,
                "entry": "cli",
                "old": old_dim.description if old_dim else None,
                "new": description.strip(),
            },
        },
    )
    rprint(f"[green]✓ 已更新维度 {dimension_id} 的描述[/green]")


def _run_dimension_mode(pg: Optional[PostgresStore] = None) -> None:
    """CLI 侧进入维度编辑模式（改进 90）；chat 侧由 /dimension 分派各自调用。"""
    from mr_data.db import ChromaStore
    from mr_data.interactive.dimensions import run_dimension_mode

    pg = pg or PostgresStore()
    pg.init_schema()
    pg.seed()
    run_dimension_mode(pg, chroma=ChromaStore(), entry="cli")


def _newsession_offline_run(engine, dsn: str) -> None:
    """/newsession 触发的后台归因（改进 86）：与定时调度/手动 offline 共用
    同一双层护栏；护栏函数自身永不抛出，这里只按结果给用户一行反馈。"""
    status = guarded_offline_run(engine, dsn, source="newsession")
    if status in ("locked", "busy"):
        rprint("[yellow]另一个离线归因正在运行，本次跳过，对话继续。[/yellow]")
    elif status == "completed":
        rprint("[dim]会话已关闭，离线归因在后台完成。[/dim]")
    elif status == "failed":
        # 失败详情已由护栏记 warning（offline.newsession_run_failed）；
        # 失败是暂时的——未处理标记还在，下个 cron 整点自动重试。
        rprint(
            "[yellow]离线归因本次未成功，将在下个定时周期自动重试；"
            "详情见日志。[/yellow]"
        )


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
  /dimension     进入维度编辑模式（/list /edit /deactivate /reactivate
                 /delete 维护性格维度，/quit 退出模式）
  /newsession    结束当前会话并开始新会话（随后在后台异步执行一次离线
                 归因；与定时调度/手动 offline 互斥，占用时自动跳过）
  /exit, /quit, /bye  退出对话

  会话结束（/exit 或 /newsession）时会询问一次整体评分（-2..2）与可选评论，
  供离线归因参考；直接回车可跳过。

  chat 启动后会在后台线程按 cron 串（MR_DATA_OFFLINE_SCHEDULE_CRON，
  默认每小时整点）定时执行离线归因分析；与手动 `mr-data offline`
  互斥（PG advisory lock），执行期间不阻塞对话。

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
  dimension        维度编辑模式（同 chat 内 /dimension）
  dimension list   列出性格维度（one-shot）
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
    # 装配收敛到 runtime.build_runtime()（chat-http-api 计划 §8.2 薄壳重构）：
    # 启动序列、调度器降级语义、offline_engine 全在通用层，此处只留交互层。
    runtime = build_runtime(enable_web_search=web_search)
    graph = runtime.graph
    pg = runtime.pg
    offline_engine = runtime.offline_engine
    scheduler = runtime.scheduler
    for warning in runtime.startup_warnings:
        rprint(warning)
    runtime.start()
    if scheduler is not None:
        rprint(
            f"[dim]离线归因定时任务已启动（cron: {scheduler.cron_expr}，"
            f"下次触发: {scheduler.next_fire().strftime('%Y-%m-%d %H:%M:%S %Z')}）[/dim]"
        )

    current_session_id = _ensure_session(pg, session_id)
    rprint(f"[dim]Session: {current_session_id}[/dim]")
    rprint("[dim]Type '/newsession' to start a new session, '/exit' or Ctrl+C to quit.[/dim]\n")

    # /newsession 触发的后台归因线程（改进 86），退出时在 finally 中 join
    run_threads: list[threading.Thread] = []

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

            if user_input.lower() == "/dimension":
                from mr_data.interactive.dimensions import run_dimension_mode

                run_dimension_mode(pg, chroma=graph.chroma, entry="chat")
                continue

            if user_input.lower() == "/newsession":
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
                    rprint("[yellow]关闭旧会话失败，本次未创建新会话。[/yellow]")
                    continue
                try:
                    current_session_id = pg.create_session()
                except Exception as exc:
                    logger.warning(
                        "Failed to create session",
                        extra={
                            "event": "cli.create_session_failed",
                            "details": {"error": str(exc)},
                        },
                    )
                    rprint("[red]新建会话失败，请稍后重试 /newsession。[/red]")
                    continue
                rprint(f"[dim]New session: {current_session_id}[/dim]\n")
                # 关闭的会话即可供离线归因：后台异步触发一轮，护栏内与
                # 定时调度/手动 offline 互斥；护栏函数永不抛出，不阻塞对话。
                if offline_engine is not None:
                    t = threading.Thread(
                        target=_newsession_offline_run,
                        args=(offline_engine, graph.pg.dsn),
                        name="mr-data-offline-newsession",
                        daemon=True,
                    )
                    run_threads.append(t)
                    t.start()
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
        try:
            runtime.stop()
        except Exception as exc:
            logger.warning(
                "Failed to stop scheduler",
                extra={
                    "event": "cli.scheduler_stop_failed",
                    "session_id": current_session_id,
                    "details": {"error": str(exc)},
                },
            )
        # 给在途的 /newsession 归因线程收尾机会（daemon 线程，超时即弃）
        try:
            for t in run_threads:
                t.join(timeout=5)
        except Exception as exc:
            logger.warning(
                "Failed to join run threads",
                extra={
                    "event": "cli.thread_join_failed",
                    "session_id": current_session_id,
                    "details": {"error": str(exc)},
                },
            )
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
def serve(
    host: str = typer.Option(settings.server_host, "--host", help="Bind host"),
    port: int = typer.Option(settings.server_port, "--port", help="Bind port"),
) -> None:
    """Start the chat HTTP API server (agent 驱动对话生命周期)."""
    from mr_data.server.main import serve as run_server

    run_server(host=host, port=port)


@app.command()
def offline() -> None:
    """Run offline attribution analysis."""
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    engine = AttributionEngine(pg_store=pg)
    # 与定时调度/newsession 收敛到同一双层护栏（改进 101 F-9）；
    # locked/busy 提示后退出码 1，failed 详情在护栏 warning 日志里。
    status = guarded_offline_run(engine, pg.dsn, source="manual")
    if status in ("locked", "busy"):
        rprint("[yellow]另一个离线归因正在运行，本次跳过。[/yellow]")
        raise typer.Exit(1)
    if status == "failed":
        raise typer.Exit(1)


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
    # 纵深防御（改进 100）：配置归一后此处不应触发；但删除目标为空/CWD
    # 时 rmtree 会清空当前工作目录，必须拒绝执行。
    for name, target in (("pgembed_data_dir", pgembed_dir), ("chroma_persist_dir", chroma_dir)):
        if not str(target) or target.resolve() == Path.cwd().resolve():
            rprint(
                f"[red]teardown 拒绝执行：{name} 解析为异常目标 {target!s}（空路径或当前工作目录），"
                "请检查配置。[/red]"
            )
            logger.warning(
                "teardown refused: unsafe target",
                extra={
                    "event": "cli.teardown_refused",
                    "details": {"reason": "unsafe_target", "field": name, "target": str(target)},
                },
            )
            raise typer.Exit(1)
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
