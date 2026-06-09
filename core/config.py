"""配置管理：从环境变量读取所有配置"""

import os
import sys
from pathlib import Path


class Config:
    """配置管理：从环境变量读取所有配置"""

    DEFAULT_SCORE_THRESHOLD = 7
    DEFAULT_AI_REQUEST_TIMEOUT = 600
    DEFAULT_AI_MAX_RETRIES = 3

    def __init__(self):
        self.GITLAB_URL = os.environ["CI_SERVER_URL"] + "/api/v4"
        self.GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
        self.PROJECT_ID = os.environ["CI_PROJECT_ID"]
        self.MR_IID = os.environ["CI_MERGE_REQUEST_IID"]
        self.SOURCE_BRANCH = os.environ["CI_MERGE_REQUEST_SOURCE_BRANCH_NAME"]
        self.OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
        self.OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
        self.OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
        self.ZHIPU_MODEL = os.environ.get("ZHIPU_MODEL", "GLM-5.1")
        self.DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
        self.DASHSCOPE_MODEL = os.environ.get("DASHSCOPE_MODEL", "glm-5")
        self.REPO_ROOT = Path(os.environ.get("CI_PROJECT_DIR", os.getcwd()))
        self.MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "50000"))
        self.SCORE_THRESHOLD = self._parse_score_threshold()
        self.REVIEW_SCOPE = self._parse_review_scope()
        self.AI_REQUEST_TIMEOUT = self._parse_positive_int(
            "AI_REQUEST_TIMEOUT_SECONDS", self.DEFAULT_AI_REQUEST_TIMEOUT
        )
        self.AI_MAX_RETRIES = self._parse_positive_int(
            "AI_MAX_RETRIES", self.DEFAULT_AI_MAX_RETRIES
        )
        proxy = os.environ.get("OPENAI_PROXY", "")
        self.OPENAI_PROXIES = {"http": proxy, "https": proxy} if proxy else None
        fallback = os.environ.get("OPENAI_PROXY_FALLBACK", "")
        self.OPENAI_PROXIES_FALLBACK = {"http": fallback, "https": fallback} if fallback else None

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量读取配置，验证必需变量"""
        required_vars = {
            "GITLAB_TOKEN": "GITLAB_TOKEN",
            "CI_PROJECT_ID": "CI_PROJECT_ID",
            "CI_MERGE_REQUEST_IID": "CI_MERGE_REQUEST_IID",
            "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",
            "CI_SERVER_URL": "CI_SERVER_URL",
        }

        missing = [name for name in required_vars if not os.environ.get(name)]
        if missing:
            print(f"ERROR: 缺少必需的环境变量: {', '.join(missing)}")
            sys.exit(1)

        # 验证 AI 服务所需的 API Key
        ai_service = os.environ.get("AI_SERVICE", "dashscope")
        key_map = {
            "openai": "OPENAI_API_KEY",
            "zhipu": "ZHIPU_API_KEY",
            "dashscope": "DASHSCOPE_API_KEY",
        }
        required_key = key_map.get(ai_service, "DASHSCOPE_API_KEY")
        if not os.environ.get(required_key):
            print(f"ERROR: 使用 {ai_service} 服务时必须设置 {required_key}")
            sys.exit(1)

        return cls()

    def _parse_score_threshold(self) -> int:
        threshold_str = os.environ.get("AI_REVIEW_SCORE_THRESHOLD", "")
        if not threshold_str:
            return self.DEFAULT_SCORE_THRESHOLD
        try:
            val = int(threshold_str)
            if 1 <= val <= 10:
                return val
            print(f"WARNING: AI_REVIEW_SCORE_THRESHOLD={threshold_str!r} 超出范围，使用默认值 {self.DEFAULT_SCORE_THRESHOLD}")
        except ValueError:
            print(f"WARNING: AI_REVIEW_SCORE_THRESHOLD={threshold_str!r} 不是有效整数，使用默认值 {self.DEFAULT_SCORE_THRESHOLD}")
        return self.DEFAULT_SCORE_THRESHOLD

    def _parse_review_scope(self) -> str:
        scope = os.environ.get("AI_REVIEW_SCOPE", "full").strip().lower()
        if scope not in {"full", "latest"}:
            print(f"WARNING: AI_REVIEW_SCOPE={scope!r} 无效，使用默认值 'full'")
            return "full"
        return scope

    def _parse_positive_int(self, env_name: str, default: int) -> int:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
            if value > 0:
                return value
            print(f"WARNING: {env_name}={raw!r} 必须大于 0，使用默认值 {default}")
        except ValueError:
            print(f"WARNING: {env_name}={raw!r} 不是有效整数，使用默认值 {default}")
        return default