# gitlab-cr: GitLab AI Code Review 设计文档

> 日期: 2026-06-04
> 项目: gitlab-cr — 原生 GitLab 的 AI Code Review 开源工具，类似 CodiumAI pr-agent 但深度适配 GitLab

## 1. 项目概述

gitlab-cr 是一个开源、原生 GitLab 的 AI Code Review 工具。用户通过一行 `python3 ai_review.py` 在 CI 中触发，自动对 Merge Request 进行代码审查、MR 描述生成和代码改进建议。

### 核心能力（v1）

| Agent | 功能 | 阻断性 |
|---|---|---|
| `review` | 代码审查：基于 diff + Issue 上下文 + 源码上下文，报告 blocking / non-blocking 问题并评分 | 是（低分阻止合并） |
| `describe` | 自动生成 MR 标题、描述、变更类型和影响模块 | 否 |
| `improve` | 代码质量改进建议（纯建议，不阻断） | 否 |

### 设计原则

1. **原生 GitLab**：所有交互通过 GitLab API，不依赖第三方平台
2. **多模型支持**：OpenAI / DashScope / Zhipu，用户自由选择
3. **私有化部署**：代码和数据都在用户自己的基础设施内
4. **并行执行**：三个 Agent 并行运行，总耗时 = 最慢的 Agent
5. **轻量依赖**：仅依赖 `requests`，不需要额外框架

## 2. 目录结构

```
gitlab-cr/
├── agents/
│   ├── __init__.py
│   ├── base.py              # Agent 基类，定义 run() 流程
│   ├── review.py            # review Agent
│   ├── describe.py          # describe Agent
│   └── improve.py           # improve Agent
│
├── core/
│   ├── __init__.py
│   ├── gitlab_api.py        # GitLab API 封装（GET/POST/PUT）
│   ├── diff_fetcher.py      # Diff 获取、过滤（二进制/路径）、源码上下文
│   ├── context_builder.py   # Issue + MR + diff_summary 上下文组装
│   ├── comment_writer.py    # 行级评论 + 总结评论发布
│   └── llm_client.py        # 多模型 LLM 客户端
│
├── prompts/
│   ├── review.md            # review Agent 的 system prompt
│   ├── describe.md          # describe Agent 的 system prompt
│   └── improve.md           # improve Agent 的 system prompt
│
├── main.py                  # CLI 入口
├── ai_review.py             # 并行编排器（用户 CI 中调用的入口）
├── .gitlab-ci.yml           # CI 模板
├── requirements.txt         # 依赖
└── README.md                # 项目说明
```

## 3. Agent 基类设计

所有 Agent 继承自同一个基类，遵循固定的生命周期：

```python
class BaseAgent:
    def __init__(self, config):
        self.config = config
        self.gitlab = GitLabAPI(config)
        self.diffs = DiffFetcher(config).fetch()
        self.llm = create_ai_client(config)

    def run(self):
        data = self.prepare_data()
        prompt = self.build_prompt()
        result = self.llm.call(prompt, data)
        parsed = self.parse(result)
        self.execute(parsed)
```

每个子类只需实现 `prepare_data`、`build_prompt`、`parse`、`execute` 四个方法。

## 4. Core 层详细设计

### 4.1 `core/gitlab_api.py`

从现有 `ai_review.py` 的 `GitLabAPI` 类直接迁移。

```
GitLabAPI
├── get(url, params) → dict       # GET 请求
├── post(url, json) → dict        # POST 请求
└── put(url, json) → Response     # PUT 请求（用于合并 MR）
```

### 4.2 `core/diff_fetcher.py`

合并现有 `DiffFetcher` 和 `SourceContextFetcher`，一次调用即可获取带有源码上下文的 diff 列表。

```
DiffFetcher
├── fetch() → list[DiffEntry]
│   ├── 支持 REVIEW_SCOPE=full / latest
│   ├── 自动分页获取所有 diff
│   └── 过滤二进制文件、migrations/等路径

DiffEntry = {
    "file_path": str,
    "diff": str,
    "new_file": bool,
    "deleted_file": bool,
    "source_context": Optional[str],  # 变更行周边的源码片段
    "added_lines": int,
    "removed_lines": int,
}
```

### 4.3 `core/context_builder.py`

合并现有 `IssueFetcher`、`MergeRequestFetcher`、`build_diff_summary`、`assemble_context`。

```
ContextBuilder
├── build() → str  # 组装完整的上下文文本
│   ├── MR 元信息（标题、源/目标分支、作者、reviewers、labels）
│   ├── Issue 上下文（从分支名解析后的关联需求）
│   └── 变更摘要（文件总数、新增/删除/修改、文件列表）
│   
│   支持 MAX_CONTEXT_CHARS 截断
│   优先保留 MR 元信息，其次变更摘要，最后 Issue 上下文
```

### 4.4 `core/comment_writer.py`

保留现有 `MRCommentWriter`（总结评论），新增 `MRDiscussionWriter`（行级评论）。

#### 总结评论

保留现有逻辑：
- 以 `🤖 AI Code Review` 为前缀发布到 MR notes
- 包含区块：审查摘要、本轮变化（新增/已解决/持续存在）、阻断问题、非阻断问题、评分
- 自动查找上一条 AI 评论并更新（增量对比）
- 嵌入元数据 JSON 用于后续增量比较

#### 行级评论（新增）

```python
class MRDiscussionWriter:
    def __init__(self, config):
        # 从 MR versions API 获取 base_sha / start_sha / head_sha

    def post_line_comment(self, mr_iid, file_path, line, severity, title, description="", suggestion=""):
        """在指定文件的指定行上创建一个 GitLab Discussion"""
        POST /projects/:id/merge_requests/:iid/discussions
        {
            "body": f"**[{severity}]** {title}\n{description}",
            "position": {
                "base_sha": ...,
                "start_sha": ...,
                "head_sha": ...,
                "position_type": "text",
                "new_path": file_path,
                "new_line": line,
                "old_path": file_path,
                "old_line": null,
            }
        }
```

### 4.5 `core/llm_client.py`

从现有 `ai_review.py` 直接迁移：

```
AIClientBase          → 基类（分批、重试、超时）
├── OpenAIClient      → OpenAI API
├── DashScopeClient   → 阿里百炼 API
└── ZhipuAIClient     → 智谱 AI API

create_ai_client(config) → 工厂函数
```

#### 分批策略

保留现有 `_split_batches` 逻辑：
- 按文件逐个拼接，累计字符数超过 `MAX_DIFF_CHARS`（150K）时切分
- 多批次结果用 `\n\n---\n\n` 拼接
- 保留源码上下文关联

## 5. Agent 层详细设计

### 5.1 Review Agent

```
prepare_data()
├── 调 DiffFetcher.fetch() 获取带源码上下文的 diff
├── 调 ContextBuilder.build() 获取 Issue + MR + 变更摘要
└── 返回 { "diffs": [...], "context": "..." }

build_prompt()
└── 从 prompts/review.md 读取

parse(result)
└── ReviewResultParser.parse(result) → { blocking_issues, non_blocking_issues, other_suggestions, score }

execute(parsed)
├── MRDiscussionWriter.post_line_comment()  ← 逐行评论（新增）
│   └── 对 blocking_issues + non_blocking_issues 中有明确 location 的行发评论
├── MRCommentWriter.write()                 ← 总结评论（已有）
└── Score 门禁
    ├── score >= threshold → exit(0) 允许合并
    └── score < threshold  → exit(1) 阻止合并
```

### 5.2 Describe Agent

```
prepare_data()
├── 只需要 diff（不需要 Issue 上下文和源码上下文）
├── 调 ContextBuilder 只获取 MR 标题（可选）
└── 返回 { "diffs": [...], "mr_title": "..." }

build_prompt()
└── 从 prompts/describe.md 读取，引导 LLM 输出结构化 MR 描述

parse(result)
└── JSON.parse → { title, description, type, changed_components }

execute(parsed)
└── 调 GitLab API 更新 MR 标题和描述
    PUT /projects/:id/merge_requests/:iid
    { "title": parsed.title, "description": parsed.description }
```

### 5.3 Improve Agent

```
prepare_data()
├── 只需要 diff（不需要 Issue 上下文）
├── 需要源码上下文（以便给出精准建议）
└── 返回 { "diffs": [...] }

build_prompt()
└── 从 prompts/improve.md 读取，引导只输出 non_blocking 建议

parse(result)
└── 复用 ReviewResultParser.parse()，但只取 non_blocking 部分

execute(parsed)
├── MRDiscussionWriter.post_line_comment()  ← 发逐行建议
└── 不设门禁，不阻止合并
```

## 6. CLI 入口

```python
$ python main.py review     # 代码审查
$ python main.py describe   # 自动生成 MR 描述
$ python main.py improve    # 改进建议
```

所有 Agent 共享相同的环境变量配置（Config.from_env()）：

| 变量 | 必需 | 默认值 | 说明 |
|---|---|---|---|
| GITLAB_TOKEN | ✅ | - | GitLab 访问令牌 |
| CI_PROJECT_ID | ✅ | - | CI 项目 ID |
| CI_MERGE_REQUEST_IID | ✅ | - | MR IID |
| CI_MERGE_REQUEST_SOURCE_BRANCH_NAME | ✅ | - | 源分支名 |
| CI_SERVER_URL | ✅ | - | GitLab 服务器 URL |
| DASHSCOPE_API_KEY | 按需 | - | 阿里百炼 API Key |
| AI_SERVICE | | dashscope | openai / dashscope / zhipu |
| DASHSCOPE_MODEL | | glm-5 | 阿里百炼模型名 |
| OPENAI_MODEL | | gpt-4o | OpenAI 模型名 |
| ZHIPU_MODEL | | GLM-5.1 | 智谱模型名 |
| MAX_CONTEXT_CHARS | | 50000 | 上下文最大字符数 |
| AI_REVIEW_SCORE_THRESHOLD | | 7 | 审查通过阈值(1-10) |
| AI_REVIEW_SCOPE | | full | full / latest |
| ISSUE_PROJECT_MAP | | - | 项目前缀映射 |

## 7. 并行编排与 CI 兼容

### 7.1 设计思路

**不改动现有 CI 模板**，在 gitlab-cr 项目中新增 `ai_review.py` 作为并行编排入口。用户在 CI 中仍然只调用 `python3 /home/ytyfsu/apps/ai_review.py` 一行代码。

`ai_review.py` 的作用：
1. 首次运行时 `git clone` gitlab-cr 项目到 `/tmp/gitlab-cr/`
2. 后续运行时 `git pull` 更新到最新版本
3. 启动三个线程并行执行 review / describe / improve
4. 等待全部线程结束，根据 review 结果 exit(0/1)

### 7.2 并行架构

```python
# ai_review.py（并行编排器）
def main():
    ensure_repo()                # git clone/pull gitlab-cr
    threads = []
    results = {}
    
    for agent_name in ["review", "describe", "improve"]:
        t = Thread(target=run_agent, args=(agent_name, results))
        t.start()
        threads.append(t)
    
    for t in threads:
        t.join()                 # 等待全部完成
    
    # review 的结果决定 CI 是否通过
    if results.get("review") != 0:
        sys.exit(1)              # review 不通过，阻止合并
```

### 7.3 CI 模板（供用户参考）

用户**不需要改动自己的 CI 配置**，继续保持原来的 `python3 /home/ytyfsu/apps/ai_review.py`。

gitlab-cr 项目提供的 `.gitlab-ci.yml` 作为官方模板：

```yaml
# .gitlab-ci.yml（用户只需保留一行）
stages:
  - review

ai-code-review:
  stage: review
  script:
    - python3 /home/ytyfsu/apps/ai_review.py
```

## 8. 与现有 ai_review.py 的关系

| 现有代码 | 迁移目标 | 改动 |
|---|---|---|
| `GitLabAPI` | `core/gitlab_api.py` | 直接复制，不改 |
| `DiffFetcher` + `SourceContextFetcher` | `core/diff_fetcher.py` | 合并两个类，增加 added_lines/removed_lines |
| `IssueFetcher` + `MergeRequestFetcher` | `core/context_builder.py` | 合并，基本不改 |
| `MRCommentWriter` + `ReviewResultParser` + `ScoreParser` | `core/comment_writer.py` | MRCommentWriter 直接迁移，新增 MRDiscussionWriter |
| `OpenAIClient` + `DashScopeClient` + `ZhipuAIClient` | `core/llm_client.py` | 直接复制，不改 |
| `main()` + `system_prompt` | `agents/review.py` + `prompts/review.md` | 拆分逻辑 |
| Config 管理 | 保留在 core 层 | 各 Agent 共用 |
| - | `agents/describe.py` | 新写 |
| - | `agents/improve.py` | 新写 |
| - | `prompts/describe.md` + `prompts/improve.md` | 新写 |
| - | `ai_review.py`（并行编排器） | 新写 |
| - | `main.py`（CLI 入口） | 新写 |

v1 新增代码量约 500 行（describe Agent + improve Agent + MRDiscussionWriter + ai_review.py + main.py）。

## 9. 未来规划（v1 不包含）

- `question` Agent：MR 问答功能
- pip 包发布：`pip install gitlab-cr`
- Docker 镜像支持
- 更多 LLM 模型支持（Claude、DeepSeek 等）
- 项目规范/CLAUDE.md 感知审查

## 10. 错误处理策略

| 场景 | 处理方式 |
|---|---|
| GitLab API 调用失败 | 3 次重试后 exit(1) |
| LLM API 调用失败 | 3 次重试后 exit(1) |
| diff 为空 | 打印日志后正常退出 |
| Issue 获取失败 | 打印 WARNING 后跳过（不阻断审查） |
| 行级评论中的 location 无法解析 | 跳过该 issue 的行级评论，仍保留在总结评论中 |
| context 超过最大字符数 | 按优先级截断：Issue > 变更摘要 > MR 元信息 |
| 一个 Agent 线程失败 | 其他 Agent 继续运行，不影响 |