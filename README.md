# gitlab-cr

GitLab 原生 AI Code Review 工具。类似 pr-agent，但深度适配 GitLab。

## 架构

```
gitlab-cr/
├── agents/              # Agent 层
│   ├── review.py        # 代码审查（阻断）
│   ├── describe.py      # MR 描述生成
│   └── improve.py       # 改进建议（不阻断）
├── core/                # Core 层
│   ├── gitlab_api.py    # GitLab API 封装
│   ├── diff_fetcher.py  # Diff 获取 + 源码上下文
│   ├── context_builder.py # Issue + MR 上下文
│   ├── comment_writer.py  # 行级 + 总结评论
│   ├── config.py        # 配置管理
│   └── llm_client.py    # LLM 客户端（OpenAI/DashScope/Zhipu）
├── prompts/             # Agent prompts
├── main.py              # CLI 入口
└── ai_review.py         # 并行编排器（CI 入口）
```

## 快速开始

### 1. 部署 ai_review.py 到 GitLab Runner

将 `ai_review.py` 复制到 GitLab Runner 的 `/home/ytyfsu/apps/` 目录。

### 2. 配置环境变量

在 GitLab CI/CD Settings → Variables 中设置：

| 变量                         | 必需 | 默认值                                              | 说明                           |
| ---------------------------- | ---- | --------------------------------------------------- | ------------------------------ |
| `GITLAB_TOKEN`               | ✅   | -                                                   | GitLab 访问令牌（需 api 权限） |
| `DASHSCOPE_API_KEY`          | ✅   | -                                                   | 阿里百炼 API Key               |
| `AI_SERVICE`                 |      | dashscope                                           | openai / dashscope / zhipu     |
| `AI_REVIEW_SCORE_THRESHOLD`  |      | 7                                                   | 1-10，低于此值阻止合并         |
| `AI_REQUEST_TIMEOUT_SECONDS` |      | 600                                                 | 单次 LLM HTTP 请求超时（秒）   |
| `AI_MAX_RETRIES`             |      | 3                                                   | LLM 请求最大重试次数           |
| `AI_AGENT_TIMEOUT_SECONDS`   |      | `AI_REQUEST_TIMEOUT_SECONDS * AI_MAX_RETRIES + 300` | 单个 Agent 进程总超时（秒）    |

其他可选变量见 `core/config.py`。

### 3. 在 CI 中使用

你的 `.gitlab-ci.yml` 只需一行（不需要改动已有配置）：

```yaml
script:
  - python3 /home/ytyfsu/apps/ai_review.py
```

### 4. 本地调试

```bash
export GITLAB_TOKEN=xxx
export CI_PROJECT_ID=123
export CI_MERGE_REQUEST_IID=456
export CI_MERGE_REQUEST_SOURCE_BRANCH_NAME=feat/xxx
export CI_SERVER_URL=https://gitlab.example.com
export DASHSCOPE_API_KEY=xxx

# 运行审查
python main.py review

# 生成 MR 描述
python main.py describe

# 改进建议
python main.py improve
```

## Agent 说明

| Agent    | 功能                     | 阻断 | 运行方式           |
| -------- | ------------------------ | ---- | ------------------ |
| review   | 代码审查，报告问题并评分 | 是   | `main.py review`   |
| describe | 自动生成 MR 标题和描述   | 否   | `main.py describe` |
| improve  | 代码质量改进建议         | 否   | `main.py improve`  |

三个 Agent 在 CI 中并行运行（通过 `ai_review.py` 编排）。

## 配置

### 模型选择

| AI_SERVICE | API Key 变量      | 默认模型 |
| ---------- | ----------------- | -------- |
| dashscope  | DASHSCOPE_API_KEY | glm-5    |
| openai     | OPENAI_API_KEY    | gpt-4o   |
| zhipu      | ZHIPU_API_KEY     | GLM-5.1  |

### 审查范围

- `AI_REVIEW_SCOPE=full`（默认）：审查 MR 全部变更
- `AI_REVIEW_SCOPE=latest`：只审查最新一次 push 的增量变更
