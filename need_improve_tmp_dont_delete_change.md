
# 未来可选增强(计划中)

- 自动关闭长期未活动的会话（session timeout policy）。
- 超长会话分片处理，避免超出 LLM 上下文窗口。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
1. _assemble_and_generate 函数中，考虑结构化输出。让输出内容中，能保留参考数据的信息（例如对应向量库或数据库id，并附加这个id对应内容的一句话总结）。
  - 在chat的cli中，可以通过配置选择是否显示输出内容中参考的信息
2. 在 DialogueState 里加 messages, 保留最近 10 轮对话(默认值，配置中增加配置项)。
  - 保留用户输入, agent助手的输出, inner_monologue
  - 保留上面提到的id和内容概括在到messages中（agent后续可以自己去取，后续计划，可以先保留数据，不做对应功能）。
  - 入库的不需要保留。
3. _assemble_and_generate 做成个codeagent的方式,给他获取相关内容的能力
4. 确定ollama调用方式
``` python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")

response = client.chat.completions.create(
    model="qwen3.5:9b",
    messages=[
        {"role": "system", "content": "You are an expert Python programmer."},
        {"role": "user", "content": "写一个带异常处理的递归目录遍历脚本"}
    ],
    # --- thinking 控制（OpenAI 兼容方式）---
    reasoning_effort="high",   # "high" | "medium" | "low" | "none"
                               # "high" = thinking 开启且强度最高
                               # "none" = 完全关闭 thinking
    
    # --- OpenAI 标准采样参数 ---
    temperature=0.6,
    top_p=0.95,
    max_tokens=16384,          # 映射到 Ollama 的 num_predict
    
    # --- Ollama 专属参数（必须通过 extra_body）---
    extra_body={
        "options": {
            "num_ctx": 32768,      # 上下文窗口（12GB 显卡安全值）
            "top_k": 20,           # Top-K 采样
            "repeat_penalty": 1.05 # 重复惩罚
        }
    }
)

print(response.choices[0].message.content)
```
```
```
