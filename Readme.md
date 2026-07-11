# 概念
mr.data 需要性格子程序

* 使用结构数据库，保存基本性格与对话
* 采用定时任务，分析对话，正向和负向归因到性格上，记录性格的成功和失败次数
  * 淘汰多次失败的性格
* 使用性格数据库, 首先注入mr.data的所有台词，并根据后续对话更新

# 架构简图
``` mermaid
flowchart TB
    %% ============================================================
    %% 1. 离线定时任务层（独立进程）
    %% ============================================================
    subgraph Offline["🕐 离线定时任务层（独立进程）"]
        direction LR
        OFF1[("拉取最近对话 + 评估数据")] 
        OFF2["LLM归因分析<br/>(性格成功率分析)"]
        OFF3[("更新人格参数<br/>PostgreSQL")]
        
        OFF1 --> OFF2 --> OFF3
    end

    %% ============================================================
    %% 2. LangGraph 对话编排层（在线请求）
    %% ============================================================
    subgraph Online["⚡ LangGraph 对话编排层（在线请求）"]
        direction TB
        ON1["加载人格<br/>从 PostgreSQL 读取 current_personality"]
        ON2["Think<br/>生成语义解释（用于向量检索）"]
        ON3["检索台词<br/>查询 Qdrant（人格向量库）"]
        ON4["检索记忆<br/>查询 Qdrant（对话记忆向量库）"]
        ON5["组装上下文<br/>合并：人格参数 + 台词 + 记忆"]
        ON6["LLM生成<br/>调用模型，返回回复"]
        ON7["记录日志<br/>写入对话记录表"]
        
        ON1 --> ON2 --> ON3 --> ON4 --> ON5 --> ON6 --> ON7
    end

    %% ============================================================
    %% 3. 数据层（外置服务）
    %% ============================================================
    subgraph Data["💾 数据层（外置服务）"]
        direction LR
        DB1[(PostgreSQL<br/>人格维度表 / 调整日志表 / 固定身份表)]
        DB2[(Qdrant<br/>台词向量库<br/>带人格标签 metadata)]
        DB3[(Qdrant<br/>对话记忆向量库<br/>episodic memory)]
    end

    %% ============================================================
    %% 跨层连接
    %% ============================================================
    OFF3 -->|"人格参数更新后"| ON1
    ON7 -->|"供离线任务分析"| OFF1
    
    ON1 -.->|"读取"| DB1
    ON3 -.->|"查询"| DB2
    ON4 -.->|"查询"| DB3
    ON7 -.->|"写入"| DB1

    %% ============================================================
    %% 样式定义（方便统一调整）
    %% ============================================================
    classDef offline fill:#e3f2fd,stroke:#1565c0,stroke-width:2px
    classDef online  fill:#fff8e1,stroke:#ff6f00,stroke-width:2px
    classDef data    fill:#f3e5f5,stroke:#6a1b9a,stroke-width:2px
    classDef trigger fill:#ffcdd2,stroke:#c62828,stroke-dasharray: 5 5

    class OFF1,OFF2,OFF3 offline
    class ON1,ON2,ON3,ON4,ON5,ON6,ON7 online
    class DB1,DB2,DB3 data
```
