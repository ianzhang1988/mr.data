# 性格维度编辑交互

> 状态：**已实施（改进 90，按本文裁决记录定稿）**。契约结论已回填：offline-attribution.md#3「无复活路径」已修订、decisions.md 已登记 D-90、00-index/02-glossary 已登记。
> 一句话目标：实现一个「维度编辑模式」（stateful mini-REPL）——进入后只有维度维护命令可用，带状态条与帮助；chat 里 `/dimension` 进入，CLI 里 `mr-data dimension` 直接进入，两入口跑同一个模式循环。
> 前置阅读：[doc/design/offline-attribution.md](design/offline-attribution.md#3)（维度调整规则、核心保护）、[doc/design/02-glossary.md](design/02-glossary.md)（维度定义）、[testing-guidelines.md](testing-guidelines.md)（测试编写指引）。本文是维度编辑模式的交互契约。
> 修订记录：v2 应用户裁决加入 active 编辑/复活重置计数/删除已停用维度（推翻「无复活路径」，见「与既有设计的关系」节）；v3 应用户要求改为**状态化交互模式**（`/dimension` 进入子状态、TUI 状态条、CLI 直入模式）；v4 裁决完成（见裁决记录节）：**全命令统一单斜杠**（模式内 `/list` `/edit` `/help` `/quit` 等，与 chat 主层同一前缀体系）、不保留直达形式、**变更操作加审计日志**——定稿；v4.2 移除本文对测试的全部要求，测试编写统一由 testing-guidelines.md 指导。

## 1. 目标与范围

**做**：

- 「维度编辑模式」：进入后仅识别维度维护命令（`/` 前缀），`/help` 帮助、`/quit` 退出模式；TUI 有常驻状态条（模式名 + 快捷提示）；所有变更操作写审计日志（见 3.6）。
- 模式内命令：列出全部维度；编辑非 core 维度描述；停用/复活非 core 维度（复活重置计数）；删除已停用维度（含引用与向量清理）。
- 三个入口：chat `/dimension`；CLI `mr-data dimension`（无子命令）与 `mr-data dimension edit`（无参数）直接进入模式；CLI 参数式 one-shot（`list`、`edit --description ... --force`）保留供脚本使用。

**不做**（v1 明确排除）：

- 新建维度（现有唯一新建路径是离线归因引擎自动发现，行为不变）。
- 直接编辑 success/failure 计数（引擎产出的归因证据；唯一合法改变计数的方式是复活重置）。
- core 维度的任何修改/停用/删除（保护语义与归因淘汰一致：core 永不淘汰）。
- 模式外单条斜杠命令（`/dimension edit 5` 这种在 chat 主循环直接执行的形式）——v3 起统一「先进模式再操作」，减少两套交互路径（v4 裁决：不保留直达形式）。

## 2. 模式（mode）设计

### 2.1 状态概览

- **进入**：chat 中 `/dimension`；CLI 中 `mr-data dimension` 或 `mr-data dimension edit`。进入时打印 banner，prompt 变为 `dim>`，出现常驻状态条。
- **退出**：`/quit`。chat 中退回正常对话循环（chat 的 `/` 命令恢复可用）；CLI 中结束进程。
- **模式内只识别 `/` 命令**：全命令统一单斜杠（v4 裁决），模式内可用 `/list` `/edit` `/deactivate` `/reactivate` `/delete` `/help` `/quit`；chat 主层的 `/` 命令（`/exit`、`/newsession` 等）在模式内**不可用**——模式优先，同名命令 `/help` `/quit` 在模式内指模式命令，退出模式后恢复 chat 语义。非 `/` 开头的输入 → 提示「模式内仅识别 / 命令，/help 查看」。
- **无内存脏状态**：每条命令直接落库生效，模式本身不持有未提交修改——`/quit` 随时安全退出，不存在「退出前必须保存」语义。

### 2.2 命令集

| 命令 | 行为 | 破坏性 |
|---|---|---|
| `/list [页码\|all\|active\|off]` | 表格列出维度；≤20 行不分页，超出按 20 行/页分页（3.1），`all` 显式全量，`active`/`off` 过滤 | 否 |
| `/edit [id]` | 编辑描述；无 id 先列出非 core 维度供编号选择（交互流见 2.5） | 否 |
| `/deactivate <id>` | 停用非 core 维度（幂等提示） | 否 |
| `/reactivate <id>` | 复活非 core 维度并清零计数（提示 2/9 → 0/0） | 中 |
| `/delete <id>` | 删除已停用维度（双重确认 + 清理链，见 3.4） | **高** |
| `/help` | 模式内帮助（命令表 + core 保护说明） | 否 |
| `/quit` | 退出模式 | 否 |

参数非数字 / id 不存在 / 目标为 core：就地提示错误并回到 `dim>` 等待（模式不退出）。

### 2.3 chat 入口

chat 循环（cli.py `chat()`）的斜杠命令是硬编码 if 链 + 硬编码 help 文本 `_print_chat_help`，无注册机制——**不为此引入注册抽象**，沿用 if 链一条分派：

```python
if user_input.strip().lower() == "/dimension":
    run_dimension_mode(pg)   # 接管输入循环，/quit 后返回
    continue
```

`_print_chat_help` 同步含 `/dimension` 说明行。

### 2.4 CLI 入口

- `mr-data dimension`（无子命令）→ 校验 store 连通后直接 `run_dimension_mode(pg)`，进程级进入模式。
- `mr-data dimension edit`（无参数）→ 同上（与 chat 行为对齐）。
- one-shot 保留：`mr-data dimension list`；`mr-data dimension edit <id> --description "..." [--force]`（脚本用，`--force` 跳过确认）。
- 三个入口调同一个 `run_dimension_mode`，差异仅在输入输出环境与退出后的去向。

### 2.5 交互流（逐步样例，验收以此为准）

以下样例是**设计契约**：提示文案、步骤顺序、退出条件以此为准实现。通用约定：

- 模式内每一步直接回车空输入 = **放弃该命令**，回到 `dim>` 等待；不产生任何写入。
- 连续两次无效输入 = 放弃该命令，回到 `dim>`。
- 确认一律 `y/N` 默认 N（安全侧）；错误就地消化，不抛到外层循环。

**chat 进入与状态条**：

```
You: /dimension
┌─ 维度编辑模式 ─────────────────────────────────────────┐
│  /list 列表  /edit 编辑  /reactivate 复活          │
│  /help 全部命令  /quit 退出模式                      │
└────────────────────────────────────────────────────────┘
dim> /list
[dims] id  core  active  s/f     description
       1   yes   active  12/3    好奇心……
       ...
       5   no    OFF      2/9    探索人类情感……   ← 可编辑对象
dim> /edit 5
当前描述：探索人类情感，允许在对话中表达共情
新描述（回车放弃）：探索用户情感状态，在适当时机表达共情
确认修改？ [y/N]: y
✓ 已更新维度 5 的描述
dim> /quit
（状态条消失，prompt 恢复 You:）
```

- 状态条用 rich `Live` 实现为终端底部常驻一行（模式名 + 快捷提示，内容同上横幅第二行）；`/quit`/Ctrl-C 时关闭。**chat 现有循环是普通 `Prompt.ask` 滚动式**，状态条只在模式运行期间存在，不改造 chat 主循环。
- prompt 指示符：`dim>`（模式内）vs `You:`（chat 常态）——用户始终知道自己在哪里。

**模式内各命令样例**（在 `dim>` 下输入）：

```text
/edit 5        → 见上样例；与旧值相同则提示「描述未变化」不写
/edit          → 先列非 core 维度重新编号 1..n，输入序号进入编辑；越界重输一次再错放弃
/deactivate 5  → 展示「维度 5：探索人类情感（active，2/9）」+ 影响说明 → 确认 → ✓ 已停用
                  已是 OFF → 「维度 5 已是停用状态」（幂等）
/reactivate 5  → 展示「（OFF，2/9）」+「复活将清零计数（2/9 → 0/0）」→ 确认 → ✓ 已复活，计数已重置
/delete 5      → 展示「（OFF，2/9）」+「将删除该维度并清理：PG 引用 14 条、Chroma 残留约 3 条。
                  此操作不可恢复。」→ y/N → 再次输入维度 id 5 以确认 → 两次一致才执行 → ✓ 已删除
                  目标仍 active → 「请先 /deactivate 5 再删除」
                  目标为 core → 「core 维度受保护，不可删除/停用/复活/编辑」
/foo           → 「未知命令 /foo，/help 查看可用命令」
普通对话文本     → 「模式内仅识别 / 命令，/quit 退出模式」
```

**CLI 进入**：`$ mr-data dimension` 打印同一 banner/状态条，`dim>` 循环，输入与上完全相同；`/quit` 或 Ctrl-C 结束进程。

### 2.6 core 保护双保险

- 模式命令层先校验，给出友好提示；
- store 层各写方法内再查一次 core，core=True 抛错——防止未来其他调用方绕过交互层直接改。

## 3. 功能规格

### 3.1 list

数据源 `PostgresStore.list_dimensions(active_only=False)`（现有方法直接可用）；显示字段：id、描述（截断，规则见下）、core、active、success_count、failure_count；按 id 升序（稳定）。三入口输出一致。

**行数分页**（v4.1 增补——防数据增长后刷屏）：

- 每页 **20 行**（不含表头）；总行数 ≤20 时不分页、无页脚提示（现状无感）。
- `/list` = 第 1 页；`/list <n>` = 第 n 页（越界提示「共 m 页」并回到 `dim>`）；`/list all` = 显式全量打印（用户显式选择，不受分页限制）。
- `/list active` / `/list off` 先按状态过滤、再对结果分页；`/list all` 可与过滤组合（`/list off all`）。
- 页脚格式：`第 p/n 页 · 共 m 条 · /list <p+1> 翻页 · /list all 全部`（末页无翻页提示）。
- 不用「按任意键翻页」式交互：命令式翻页与模式风格一致，交互状态更简单。

**描述截断**：超长描述按行宽截断（取终端宽度与 100 列的较小者减去固定列占用）以 `…` 结尾；完整描述在 `/edit <id>` 第一步展示全文。

### 3.2 edit 描述（非 core）

`get_dimension` 取当前值 → core 校验 → 展示当前描述 → 收集新描述（非空、去首尾空白、与旧值不同才写入）→ 确认 → 写入。存储上只动 PG `personality_dimensions` 单行，**Chroma 无涉**（向量 metadata 只挂 `dimension_ids`，不含描述文本）。描述以 `[id] description` 形式进入在线 chat prompt 与离线归因 prompt 两处，编辑后下一轮生效。

### 3.3 active 编辑：deactivate / reactivate（重置计数）

- **deactivate**：置 active=False，语义与引擎自动停用一致（不再进入在线/离线活跃维度集，行与计数保留）。
- **reactivate**：置 active=True，**同时 success_count=0、failure_count=0**——重置是结构性必须：不重置则旧计数（如 7 败/10 样本）会在下一轮归因按 0.6 失败率**立即再次触发淘汰**，复活形同虚设。清零 = 给维度干净的新生命周期。
- 对 core 拒绝；对已处于目标状态的调用幂等提示（不报错）。

### 3.4 delete：仅已停用的非 core 维度

删除必须清理三层痕迹：

1. **PG 行**：删 `personality_dimensions` 行（store 层 `delete_dimension`，core 拒绝、仅 active=False 可删）。
2. **PG 引用**：`dialogue_dimension_refs` 由 FK ON DELETE CASCADE 自动清理；`adjustment_logs.dimension_id` 的 FK 无级联动作，由 store 层在删行前显式删除其行（返回条数，供删除前展示与审计日志）。
3. **Chroma 残留**：metadata `dimension_ids` 含该 id 的文档需清除——复用引擎停用路径的 purge 函数（`purge_dimension_vectors`，已抽为 `mr_data.offline.attribution` 的共用函数，引擎与模式两条路径同一实现），保证无悬挂 dimension_ids，顺带兜底引擎 purge 失败的历史残留（引擎 purge 失败仅 warning 不回滚）。

删除前必须展示：维度描述、计数、PG 引用数、预计 Chroma 清理量，经双重确认（y/N + 重输 id）后执行。

### 3.5 为什么描述与 active 是仅有的可直改字段

新建仍归引擎自动发现；计数只能经复活重置；core 全拒。范围到此为止，避免人工编辑破坏归因证据链的可解释性。

### 3.6 审计日志（v4 裁决：加日志）

所有变更操作（edit/deactivate/reactivate/delete，含放弃与被拒的不算）**成功后**写审计日志：

- **载体**：复用项目现有 logging 配置（logs/ 应用日志），不新建审计表——人工编辑低频，日志行足够回溯。
- **内容**：操作名、维度 id、旧值→新值（edit 的描述、reactivate 的清零前后计数、delete 的描述与计数）、入口（chat/CLI）。级别 INFO。
- **样例**：`dimension_reactivate id=5 counts=2/9->0/0 entry=chat`
- list 与放弃/被拒的操作不写日志。

## 4. 实现结构

- **store 层**（`PostgresStore` 三个专用方法，承载 core 校验与计数重置；现有 `update_dimension`/`deactivate_dimension` 为引擎计数/淘汰专用，语义不变，新命令不走它们）：
  - `update_dimension_description(dimension_id, description)`：core 拒绝；只动 description + updated_at。
  - `set_dimension_active(dimension_id, active)`：core 拒绝；active=True 时清零计数。
  - `delete_dimension(dimension_id)`：core 拒绝、仅 active=False 可删；先显式删 `adjustment_logs` 行再删维度行（见 3.4）。
- **模式核心**（`src/mr_data/interactive/dimensions.py`）：`run_dimension_mode(store, *, chroma=None, input_fn=None, entry=...)` 为模式主循环，`input_fn`/`confirm_fn` 可注入（默认 rich `Prompt.ask`），模式可被脚本化输入驱动，不依赖真实终端；状态条为 rich `Live` 常驻一行，进入时启动、`/quit`/Ctrl-C 时关闭；命令实现 `cmd_list`/`cmd_edit`/`cmd_deactivate`/`cmd_reactivate`/`cmd_delete` 各承载 2.5 交互流，`/help`/`/quit`/未知命令/非 `/` 输入的分派在主循环内；banner、`dim>` prompt、所有文案集中为模块级常量（单一来源）；`cmd_delete` 负责清理链编排（store 删行 + Chroma purge），Chroma 清理不进 store 层。
- **入口层**（cli.py）：`dimension` Typer subgroup 三个暴露面——`list`、`edit`（带 `--description` 走 one-shot，`--force` 跳过确认）、无子命令默认进模式；chat 入口为 if 链一条分派（2.3）。模式在 CLI 下与 chat 下行为一致，唯一差异是 `/quit` 后 CLI 进程结束。
- **生效时机**：维度描述/活跃集无进程内缓存——在线 `_load_personality` 与离线归因均为每轮/每 run 查询 PG，编辑即时生效。

## 5. 与既有设计的关系（含一项被推翻的决策）

- **「无复活路径」被维度编辑能力推翻**（改进 90）：offline-attribution.md#3 有意设计「停用维度不再参与归因，无复活信号源，reactivate 判为无意义路径」。维度编辑模式引入人工编辑后，**人就是复活信号源**，且复活必须重置计数（否则旧计数立即触发再淘汰，3.3）。offline-attribution.md#3 该句已同步修订，decisions.md 已登记 D-90「允许人工复活（deactive→active 重置计数）+ 允许删除已停用维度，取代『无复活路径』」。
- **核心保护**：归因淘汰路径跳过 core；本计划的 core 只读/不可停用/不可删除是同一语义在人工编辑路径的延伸。
- **引擎自动路径不受影响**：引擎淘汰仍走 `deactivate_dimension` + purge；自动发现维度（insert_dimension）不变；人工 delete 的 Chroma 清理与引擎 purge 同模式，两条路径清理逻辑保持一致。

## 6. 裁决记录（v4 定稿）

| # | 议题 | 裁决 |
|---|---|---|
| 1 | 命令前缀方案 | **全命令统一单斜杠**——chat 主层 `/dimension` 进入，模式内 `/list` `/edit` `/deactivate` `/reactivate` `/delete` `/help` `/quit` 同为 `/` 前缀；模式优先于 chat 主层同名命令，退出后恢复。不用双斜杠，前缀体系统一。 |
| 2 | 是否保留 chat 直达形式（`/dimension edit 5` 不进模式直接执行） | **不保留**。三入口统一先进模式再操作，只维护一套交互路径；CLI 的 one-shot（`list`、`edit --description --force`）保留供脚本使用。 |
| 3 | 审计痕迹 | **加日志**。edit/deactivate/reactivate/delete 成功后写审计日志（复用现有 logs/ 应用日志，不建审计表），格式见 3.6。 |
