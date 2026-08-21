"""Title Checker Agent：根据项目名称和分支号校验并修复 MR 标题前缀"""

import re
import sys

from core.gitlab_api import GitLabAPI

# 匹配 feat/123/desc、bug/456/xxx、feat/erp/123/desc 等格式，提取 Issue ID
_BRANCH_ISSUE_RE = re.compile(
    r"^(?:bug|feat|hotfix|fix|chore|release)/(?:[^/\d][^/]*/)?(\d+)/"
)


def extract_issue_id(branch_name: str) -> str | None:
    """从分支名中提取 Issue ID，支持 feat/123/desc 和 feat/erp/123/desc 两种格式"""
    m = _BRANCH_ISSUE_RE.match(branch_name)
    return m.group(1) if m else None


def _normalize_title(title: str, prefix: str) -> tuple[str, bool]:
    """
    返回 (规范化后的标题, 是否已修改)。
    规则：
      prefix: xxx   → 不变
      prefix:xxx    → prefix: xxx
      prefix xxx    → prefix: xxx
      prefix直连xxx → prefix: xxx
      无前缀        → prefix: title
    """
    p = re.escape(prefix)
    m = re.match(rf"^({p})(:\s*|\s+|(?=[^\s:]))(.*)", title, re.IGNORECASE)
    if m:
        rest = m.group(3).lstrip()
        normalized = f"{prefix}: {rest}"
        already_correct = title == normalized
        return normalized, not already_correct
    # 前缀完全不存在
    return f"{prefix}: {title}", True


class TitleCheckerAgent:
    """检查 MR 标题是否符合 {PROJECT_NAME}#{issue_id}: 格式，不符合则自动补全"""

    def __init__(self, config):
        self.config = config
        self.gitlab = GitLabAPI(config.GITLAB_URL, config.GITLAB_TOKEN)

    def run(self):
        project_name = self.config.PROJECT_NAME
        source_branch = self.config.SOURCE_BRANCH
        project_id = self.config.PROJECT_ID
        mr_iid = self.config.MR_IID

        issue_id = extract_issue_id(source_branch)
        if not issue_id:
            print(f"INFO: 分支 '{source_branch}' 未匹配到 Issue ID，跳过标题检查")
            return

        expected_prefix = f"{project_name}#{issue_id}"
        mr_url = f"/projects/{project_id}/merge_requests/{mr_iid}"

        try:
            mr = self.gitlab.get(mr_url)
        except Exception as e:
            print(f"ERROR: 获取 MR 详情失败: {e}")
            sys.exit(1)

        title = mr.get("title", "").strip()
        print(f"INFO: 当前 MR 标题: {title!r}")
        print(f"INFO: 期望前缀: {expected_prefix}")

        new_title, changed = _normalize_title(title, expected_prefix)
        if not changed:
            print("INFO: 标题前缀正确，无需修改")
            return
        print(f"INFO: 更新 MR 标题为: {new_title!r}")

        resp = self.gitlab.put(mr_url, {"title": new_title})
        if resp.status_code in (200, 201):
            print("INFO: MR 标题更新成功")
        else:
            print(f"ERROR: MR 标题更新失败: HTTP {resp.status_code} — {resp.text[:200]}")
            sys.exit(1)
