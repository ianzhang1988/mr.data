# mr.data

mr.data 性格子程序原型。

## 概念

mr.data 需要性格子程序：

* 使用结构数据库，保存基本性格与对话。
* 采用定时任务，分析对话，正向和负向归因到性格上，记录性格的成功和失败次数；淘汰多次失败的性格。
* 使用性格数据库，首先注入 mr.data 的所有台词，并根据后续对话更新。

## 架构简图

```mermaid
flowchart TB
    subgraph Offline["离线定时任务层（独立进程）"]
        direction LR
        OFF1["拉取最近对话 + 评估数据"]
        OFF2["LLM归因分析 (成功率分析)"]
        OFF3["更新人格参数 (PostgreSQL)"]
        OFF1 --> OFF2 --> OFF3
    end

    subgraph Online["LangGraph 对话编排层（在线请求）"]
        direction TB
        ON1["加载人格: 从 PostgreSQL 读取 current_personality"]
        ON2["Think: 生成语义解释 (用于向量检索)"]
        ON3["检索人格素材: 查询 personality 向量库"]
        ON4["检索记忆: 查询 memories 对话记忆向量库"]
        ON5["组装上下文: 合并人格参数 + 素材 + 记忆"]
        ON6["LLM生成: 调用模型，返回回复"]
        ON7["记录日志: 写入对话记录表"]
        ON1 --> ON2 --> ON3 --> ON4 --> ON5 --> ON6 --> ON7
    end

    subgraph Data["数据层（外置服务）"]
        direction LR
        DB1["PostgreSQL: 人格维度表 / 调整日志表 / 固定身份表"]
        DB2["Chroma: personality 人格素材向量库 (line + event)"]
        DB3["Chroma: memories 对话记忆向量库 (episodic memory)"]
    end

    OFF3 -->|"人格参数更新后"| ON1
    ON7 -->|"供离线任务分析"| OFF1

    ON1 -.->|"读取"| DB1
    ON3 -.->|"查询"| DB2
    ON4 -.->|"查询"| DB3
    ON7 -.->|"写入"| DB1

    classDef offline fill:#e3f2fd,stroke:#1565c0,stroke-width:2px
    classDef online fill:#fff8e1,stroke:#ff6f00,stroke-width:2px
    classDef data fill:#f3e5f5,stroke:#6a1b9a,stroke-width:2px

    class OFF1,OFF2,OFF3 offline
    class ON1,ON2,ON3,ON4,ON5,ON6,ON7 online
    class DB1,DB2,DB3 data
```

## 当前实现要点

* 使用 PostgreSQL 保存结构化人格维度与对话记录。
* 使用 Chroma（原型阶段替代 Qdrant）保存人格素材向量库与对话记忆向量库；`personality` 集合既包含原始台词，也包含后续交流中产生的、影响人格的事件。
* 在线对话由 LangGraph 编排：加载人格 → 生成检索意图 → 检索人格素材与记忆 → 组装上下文 → LLM 生成回复 → 记录对话。
* 离线定时任务分析近期对话，将成功/失败归因到性格维度，更新维度计数并淘汰多次失败维度；同时将值得记住的事件写回人格素材库。

## 环境要求

* Python >= 3.10
* PostgreSQL 14+（需自行安装并启动）
* LLM：OpenAI 兼容 API 或本地模型（Ollama / vLLM 等提供 OpenAI 兼容接口）

## 快速开始

### 1. 安装依赖

```bash
cd mr.data
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 LLM 与 PostgreSQL 连接信息
```

示例 `.env`：

```env
MR_DATA_LLM_BASE_URL=https://api.openai.com/v1
MR_DATA_LLM_API_KEY=sk-xxx
MR_DATA_LLM_MODEL=gpt-4o-mini
MR_DATA_POSTGRES_DSN=postgresql://user:password@localhost:5432/mrdata
MR_DATA_CHROMA_PERSIST_DIR=./data/chroma
```

本地模型示例（Ollama）：

```env
MR_DATA_LLM_BASE_URL=http://localhost:11434/v1
MR_DATA_LLM_API_KEY=ollama
MR_DATA_LLM_MODEL=llama3.1
```

### 3. 初始化数据库

```bash
mr-data init
# 或
python -m mr_data.cli init
```

### 4. 导入人格素材

```bash
mr-data ingest
```

### 5. 启动交互对话

```bash
mr-data chat
```

带评估反馈（用于离线归因）：

```bash
mr-data chat --eval
```

### 6. 运行离线归因

```bash
mr-data offline
```

## 运行测试

需要先在环境变量中配置 `MR_DATA_POSTGRES_DSN` 才能跑涉及数据库的集成测试。

```bash
export MR_DATA_POSTGRES_DSN=postgresql://user:password@localhost:5432/mrdata
pytest
```

不依赖数据库的单测（Chroma、模型序列化）可直接运行：

```bash
pytest -k "not postgres and not dialogue_graph and not offline"
```

## 项目结构

```
mr.data/
├── pyproject.toml
├── .env.example
├── src/mr_data/
│   ├── config.py          # 配置
│   ├── db/
│   │   ├── postgres.py    # PostgreSQL 封装
│   │   └── chroma.py      # Chroma 向量库封装
│   ├── models/            # Pydantic 模型
│   ├── llm/               # 统一 LLM 客户端
│   ├── online/            # LangGraph 在线对话
│   ├── offline/           # 离线归因引擎
│   └── cli.py             # 命令行入口
├── scripts/               # 独立脚本
└── tests/                 # 测试
```

## 从原型到生产

* 向量库：Chroma → Qdrant（接口封装在 `mr_data.db.chroma`，替换成本低）。
* 数据库：当前使用 `psycopg` 直连；如需更复杂查询可迁移到 SQLAlchemy。
* 评估数据：MVP 阶段 CLI 支持人工评分；后续可接入真实用户反馈或自动评估。
