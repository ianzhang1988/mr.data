"""维度编辑模式（dimension mode，改进 90）。

状态化 mini-REPL：进入后仅识别维度维护 `/` 命令，每条命令直接落库生效，
模式不持有未提交修改，`/quit` 随时安全退出。

交互文案即设计契约（doc/dimension-editing.md §2.5），测试对照锁定。
测试用注入式 input_fn 脚本驱动全模式（input_fn 返回 None 表示结束）。
"""

import shutil

from rich import print as rprint
from rich.prompt import Prompt
from rich.live import Live

from mr_data.logging import get_logger
from mr_data.models import PersonalityDimension
from mr_data.offline.attribution import purge_dimension_vectors

logger = get_logger("mr_data.interactive.dimensions")

PROMPT = "dim>"
PAGE_SIZE = 20

BANNER = """\
┌─ 维度编辑模式 ─────────────────────────────────────────┐
│  /list 列表  /edit 编辑  /reactivate 复活            │
│  /help 全部命令  /quit 退出模式                        │
└────────────────────────────────────────────────────────┘"""

STATUS_TEXT = "维度编辑模式 · /list 列表 /edit 编辑 /help 全部命令 /quit 退出模式"

HELP_TEXT = """\
可用命令：
  /list [页码|all|active|off]   列出维度（20 行/页，all 全量，active/off 过滤）
  /edit [id]                    编辑非 core 维度描述（无 id 先列出供编号选择）
  /deactivate <id>              停用非 core 维度
  /reactivate <id>              复活非 core 维度（计数清零，重新开始生命周期）
  /delete <id>                  删除已停用维度（不可恢复，双重确认）
  /help                         显示本帮助
  /quit                         退出模式

core 维度受保护，不可删除/停用/复活/编辑。
模式内仅识别 / 命令；每条命令直接落库生效。"""

MSG_NOT_COMMAND = "模式内仅识别 / 命令，/quit 退出模式"
MSG_UNKNOWN_COMMAND = "未知命令 {cmd}，/help 查看可用命令"
MSG_CORE_PROTECTED = "core 维度受保护，不可删除/停用/复活/编辑"
MSG_DIM_NOT_FOUND = "维度 {id} 不存在"
MSG_INVALID_ID = "参数需为维度 id 数字：{arg}"
MSG_GIVE_UP = "已放弃该命令"
MSG_ALREADY_OFF = "维度 {id} 已是停用状态"
MSG_ALREADY_ACTIVE = "维度 {id} 已是活跃状态"


def _counts(dim: PersonalityDimension) -> str:
    return f"{dim.success_count}/{dim.failure_count}"


def _status(dim: PersonalityDimension) -> str:
    return "active" if dim.active else "OFF"


def _dim_line(dim: PersonalityDimension, desc_width: int) -> str:
    desc = dim.description
    if len(desc) > desc_width:
        desc = desc[: desc_width - 1] + "…"
    return f"{dim.id:<4} {'yes' if dim.core else 'no':<5} {_status(dim):<7} {_counts(dim):<6} {desc}"


def _desc_width() -> int:
    """描述列宽：终端宽度与 100 列取小，减固定列占用。"""
    try:
        width = shutil.get_terminal_size().columns
    except OSError:
        width = 100
    return max(20, min(width, 100) - 30)


def _format_list_page(dims: list[PersonalityDimension], page: int, page_count: int, total: int) -> list[str]:
    lines = ["id   core  active  s/f     description"]
    width = _desc_width()
    lines.extend(_dim_line(d, width) for d in dims)
    if page_count > 1:
        footer = f"第 {page}/{page_count} 页 · 共 {total} 条"
        if page < page_count:
            footer += f" · /list {page + 1} 翻页"
        footer += " · /list all 全部"
        lines.append(footer)
    return lines


def cmd_list(store, args: list[str]) -> None:
    show_all = any(a == "all" for a in args)
    pages = [a for a in args if a.isdigit()]
    filters = [a for a in args if a in ("active", "off")]
    unknown = [a for a in args if a not in ("all", "active", "off") and not a.isdigit()]
    if unknown:
        rprint(f"未知参数 {unknown[0]}，用法：/list [页码|all|active|off]")
        return

    dims = store.list_dimensions(active_only=False)
    if "active" in filters:
        dims = [d for d in dims if d.active]
    if "off" in filters:
        dims = [d for d in dims if not d.active]

    total = len(dims)
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = int(pages[0]) if pages else 1
    if page > page_count:
        rprint(f"共 {page_count} 页")
        return
    if not show_all:
        dims = dims[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    for line in _format_list_page(dims, page, page_count, total):
        rprint(line)


def _get_dim_or_report(store, dim_id: int) -> PersonalityDimension | None:
    dim = store.get_dimension(dim_id)
    if dim is None:
        rprint(MSG_DIM_NOT_FOUND.format(id=dim_id))
        return None
    return dim


def _parse_id(args: list[str]) -> int | None:
    if not args:
        rprint("缺少维度 id，用法：/edit <id>（或 /edit 进入编号选择）")
        return None
    try:
        return int(args[0])
    except ValueError:
        rprint(MSG_INVALID_ID.format(arg=args[0]))
        return None


def _confirm(prompt: str, input_fn) -> bool:
    answer = input_fn(f"{prompt} [y/N]: ")
    return bool(answer) and answer.strip().lower() == "y"


def _audit(event: str, *, dimension_id: int, entry: str, **details) -> None:
    logger.info(
        event,
        extra={
            "event": event,
            "details": {"dimension_id": dimension_id, "entry": entry, **details},
        },
    )


def cmd_edit(store, args: list[str], input_fn, entry: str) -> None:
    if args:
        dim_id = _parse_id(args)
        if dim_id is None:
            return
    else:
        # 无 id：列出非 core 维度重新编号 1..n 供选择
        candidates = [d for d in store.list_dimensions() if not d.core]
        if not candidates:
            rprint("没有可编辑的非 core 维度")
            return
        rprint("可编辑的非 core 维度：")
        for idx, d in enumerate(candidates, 1):
            rprint(f"{idx}. id={d.id} {d.description}（{_status(d)}，{_counts(d)}）")
        pick = input_fn("输入序号（回车放弃）：")
        if not pick or not pick.strip():
            rprint(MSG_GIVE_UP)
            return
        try:
            n = int(pick.strip())
            if not 1 <= n <= len(candidates):
                raise ValueError
        except ValueError:
            pick = input_fn("无效序号，再输一次（回车放弃）：")
            if not pick or not pick.strip():
                rprint(MSG_GIVE_UP)
                return
            try:
                n = int(pick.strip())
                if not 1 <= n <= len(candidates):
                    raise ValueError
            except ValueError:
                rprint(MSG_GIVE_UP)
                return
        dim_id = candidates[n - 1].id

    dim = _get_dim_or_report(store, dim_id)
    if dim is None:
        return
    if dim.core:
        rprint(MSG_CORE_PROTECTED)
        return
    rprint(f"当前描述：{dim.description}")
    new_desc = input_fn("新描述（回车放弃）：")
    if not new_desc or not new_desc.strip():
        rprint(MSG_GIVE_UP)
        return
    new_desc = new_desc.strip()
    if new_desc == dim.description:
        rprint("描述未变化")
        return
    if not _confirm("确认修改？", input_fn):
        rprint(MSG_GIVE_UP)
        return
    store.update_dimension_description(dim_id, new_desc)
    _audit("dimension_edit", dimension_id=dim_id, entry=entry,
           old=dim.description, new=new_desc)
    rprint(f"✓ 已更新维度 {dim_id} 的描述")


def cmd_deactivate(store, args: list[str], input_fn, entry: str) -> None:
    dim_id = _parse_id(args)
    if dim_id is None:
        return
    dim = _get_dim_or_report(store, dim_id)
    if dim is None:
        return
    if dim.core:
        rprint(MSG_CORE_PROTECTED)
        return
    if not dim.active:
        rprint(MSG_ALREADY_OFF.format(id=dim_id))
        return
    rprint(f"维度 {dim_id}：{dim.description}（active，{_counts(dim)}）")
    rprint("停用后该维度不再进入在线/离线活跃维度集，行与计数保留。")
    if not _confirm("确认停用？", input_fn):
        rprint(MSG_GIVE_UP)
        return
    store.set_dimension_active(dim_id, False)
    _audit("dimension_deactivate", dimension_id=dim_id, entry=entry,
           counts=_counts(dim))
    rprint(f"✓ 已停用维度 {dim_id}")


def cmd_reactivate(store, args: list[str], input_fn, entry: str) -> None:
    dim_id = _parse_id(args)
    if dim_id is None:
        return
    dim = _get_dim_or_report(store, dim_id)
    if dim is None:
        return
    if dim.core:
        rprint(MSG_CORE_PROTECTED)
        return
    if dim.active:
        rprint(MSG_ALREADY_ACTIVE.format(id=dim_id))
        return
    rprint(f"维度 {dim_id}：{dim.description}（OFF，{_counts(dim)}）")
    rprint(f"复活将清零计数（{_counts(dim)} → 0/0），该维度重新开始生命周期。")
    if not _confirm("确认复活？", input_fn):
        rprint(MSG_GIVE_UP)
        return
    store.set_dimension_active(dim_id, True)
    _audit("dimension_reactivate", dimension_id=dim_id, entry=entry,
           counts=f"{_counts(dim)}->0/0")
    rprint(f"✓ 已复活维度 {dim_id}，计数已重置")


def cmd_delete(store, chroma, args: list[str], input_fn, entry: str) -> None:
    if chroma is None:
        rprint("Chroma 不可用，无法执行删除（删除需清理向量残留）")
        return
    dim_id = _parse_id(args)
    if dim_id is None:
        return
    dim = _get_dim_or_report(store, dim_id)
    if dim is None:
        return
    if dim.core:
        rprint(MSG_CORE_PROTECTED)
        return
    if dim.active:
        rprint(f"请先 /deactivate {dim_id} 再删除")
        return
    with store._cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM dialogue_dimension_refs WHERE dimension_id = %s",
            (dim_id,),
        )
        ref_count = cur.fetchone()["n"]
        cur.execute(
            "SELECT count(*) AS n FROM adjustment_logs WHERE dimension_id = %s", (dim_id,)
        )
        adjustment_count = cur.fetchone()["n"]
    chroma_count = len(chroma.get_personality_event_ids_by_dimension(dim_id))
    rprint(f"维度 {dim_id}：{dim.description}（OFF，{_counts(dim)}）")
    rprint(
        f"将删除该维度并清理：PG 引用 {ref_count + adjustment_count} 条、"
        f"Chroma 残留约 {chroma_count} 条。此操作不可恢复。"
    )
    if not _confirm("确认删除？", input_fn):
        rprint(MSG_GIVE_UP)
        return
    confirm_id = input_fn(f"再次输入维度 id {dim_id} 以确认：")
    if confirm_id is None or confirm_id.strip() != str(dim_id):
        rprint(MSG_GIVE_UP)
        return
    adjustments = store.delete_dimension(dim_id)
    purge_dimension_vectors(store, chroma, dim_id)
    _audit("dimension_delete", dimension_id=dim_id, entry=entry,
           description=dim.description, counts=_counts(dim),
           adjustments=adjustments)
    rprint(f"✓ 已删除维度 {dim_id}")


def _default_input(prompt: str):
    return Prompt.ask(prompt, default="")


def _is_injected(input_fn) -> bool:
    return input_fn is not None and input_fn is not _default_input


def _start_status_bar(enabled: bool):
    """真实终端下 rich Live 常驻状态条；测试（注入式 input_fn）不启动。"""
    if not enabled:
        return None
    from rich.console import Console

    live = Live(STATUS_TEXT, console=Console(), refresh_per_second=4)
    live.start()
    return live


def _stop_status_bar(live) -> None:
    if live is not None:
        live.stop()


def run_dimension_mode(store, *, chroma=None, input_fn=None, entry: str = "chat",
                       status_bar: bool = True) -> None:
    """维度编辑模式主循环。input_fn(prompt)->str|None 可注入（None 结束模式）；
    状态条仅真实终端（默认 input_fn）开启，注入式 input_fn 自动关闭。"""
    if input_fn is None:
        input_fn = _default_input

    rprint(BANNER)
    live = _start_status_bar(status_bar and not _is_injected(input_fn))
    try:
        while True:
            try:
                raw = input_fn(PROMPT)
            except (EOFError, KeyboardInterrupt):
                break
            if raw is None:
                break
            line = raw.strip()
            if not line:
                continue
            if not line.startswith("/"):
                rprint(MSG_NOT_COMMAND)
                continue
            parts = line.split()
            cmd, args = parts[0].lower(), parts[1:]
            if cmd == "/quit":
                break
            if cmd == "/help":
                rprint(HELP_TEXT)
            elif cmd == "/list":
                cmd_list(store, args)
            elif cmd == "/edit":
                cmd_edit(store, args, input_fn, entry)
            elif cmd == "/deactivate":
                cmd_deactivate(store, args, input_fn, entry)
            elif cmd == "/reactivate":
                cmd_reactivate(store, args, input_fn, entry)
            elif cmd == "/delete":
                cmd_delete(store, chroma, args, input_fn, entry)
            else:
                rprint(MSG_UNKNOWN_COMMAND.format(cmd=cmd))
    finally:
        _stop_status_bar(live)
