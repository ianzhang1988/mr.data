
# 未来可选增强(计划中)

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

2. 更完善的日志查看/搜索 UI 或 CLI 命令。
3. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
4. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？让他有更好的思考完成任务的机会，可以考虑再加上获取相关内容的能力
1. 写个计划检查所有的测试项目，是否有可以合并的，失效的，或者意义不大的等等。目标是简化测试集。另外，注意测试使用的时间，例如sleep(1)这样，除了数据库网络等，超过100ms的测试就比较可疑。
8. 性格维度增加一个编辑交互。
1. 增加chat http交互api，这样后续可以用agent测试agent，先不要改项目，考虑设计。
