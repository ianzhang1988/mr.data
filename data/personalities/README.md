# 人格文件格式

`data/personalities/` 目录用于存放可切换的人格配置。每个 JSON 文件即为一个 **PersonalityPack**，包含：

- `identity`：固定身份（名称、角色、基础人设 prompt）
- `dimensions`：性格维度列表（描述 + 是否为核心维度）
- `sample_lines`（可选）：用于 `mr-data ingest` 的示例台词与场景

## 默认人格

- `data.json`：星际迷航 TNG 中的 **Data**。

## 字段说明

```json
{
  "identity": {
    "name": "Data",
    "role": "星际联邦星舰企业号-D 少校，仿生人军官",
    "base_prompt": "你是 Data，..."
  },
  "dimensions": [
    {
      "description": "我对人类行为、艺术与未知现象抱有强烈的好奇心...",
      "core": true
    }
  ],
  "sample_lines": [
    {
      "content": "我无法确定这是否是一个玩笑。",
      "context": "用户讲了一个双关语后，Data 坦诚自己的局限。",
      "speaker": "Data",
      "dimension_descriptions": [
        "我倾向于逻辑、字面、精确地表达..."
      ]
    }
  ]
}
```

### identity

| 字段 | 说明 |
|------|------|
| `name` | 角色名称 |
| `role` | 角色身份 |
| `base_prompt` | 注入 LLM 系统提示的基础人设说明 |

### dimensions

| 字段 | 说明 |
|------|------|
| `description` | 性格自白，用于注入提示词与归因 |
| `core` | 是否核心维度。核心维度不会因失败次数达到阈值而被自动失效 |

### sample_lines

| 字段 | 说明 |
|------|------|
| `content` | 角色台词（注入 prompt 时使用的 utterance） |
| `context` | 前置场景，用于向量检索 embedding |
| `speaker` | 说话者标识，默认 `assistant` |
| `dimension_descriptions` | 该台词对应哪些维度，按 `dimensions` 中的 `description` 精确匹配 |

## 切换人格

1. 在 `data/personalities/` 下新建 JSON 文件，例如 `spock.json`。
2. 设置环境变量：

```bash
export MR_DATA_PERSONALITY_FILE=./data/personalities/spock.json
```

3. 重新初始化数据库并导入人格素材：

```bash
mr-data init
mr-data ingest
```

若未设置 `MR_DATA_PERSONALITY_FILE`，则默认使用 `data/personalities/data.json`。
