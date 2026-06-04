"""Agent 基类，定义 run() 生命周期"""

from core.gitlab_api import GitLabAPI
from core.diff_fetcher import DiffFetcher
from core.llm_client import create_ai_client


class BaseAgent:
    """所有 Agent 的基类，定义固定的 run() 流程"""

    def __init__(self, config):
        self.config = config
        self.gitlab = GitLabAPI(config.GITLAB_URL, config.GITLAB_TOKEN)
        self.diffs = DiffFetcher(
            gitlab_url=config.GITLAB_URL,
            token=config.GITLAB_TOKEN,
            project_id=config.PROJECT_ID,
            mr_iid=config.MR_IID,
            review_scope=getattr(config, 'REVIEW_SCOPE', 'full'),
            repo_root=config.REPO_ROOT,
            max_context_chars=config.MAX_CONTEXT_CHARS,
        ).fetch()
        self.llm = create_ai_client(config)

    def run(self):
        """Agent 主流程"""
        data = self.prepare_data()
        prompt = self.build_prompt()
        result = self.llm.review(prompt, data["context"], data["diffs"])
        parsed = self.parse(result)
        self.execute(parsed)

    def prepare_data(self):
        """准备数据——子类实现"""
        raise NotImplementedError

    def build_prompt(self):
        """构建 system prompt——子类实现"""
        raise NotImplementedError

    def parse(self, result: str):
        """解析 LLM 返回结果——子类实现"""
        raise NotImplementedError

    def execute(self, parsed):
        """执行动作（发评论/更新 MR/门禁）——子类实现"""
        raise NotImplementedError