#!/usr/bin/env python3
"""上下文组装：MR 元信息 + Issue 上下文 + 变更摘要"""

import os
import re
import sys
from typing import Optional
from urllib.parse import quote_plus

from core.gitlab_api import GitLabAPI


def truncate_text(text: str, max_chars: int, suffix: str = "\n...(truncated)") -> str:
    normalized = (text or "").strip()
    if max_chars <= 0 or len(normalized) <= max_chars:
        return normalized
    keep = max_chars - len(suffix)
    if keep <= 0:
        return suffix[:max_chars]
    return normalized[:keep].rstrip() + suffix


class MergeRequestFetcher:
    """获取 MR 自身元信息"""

    def __init__(self, api: GitLabAPI, project_id: str, mr_iid: str, source_branch: str, max_context_chars: int):
        self.api = api
        self.project_id = project_id
        self.mr_iid = mr_iid
        self.source_branch = source_branch
        self.max_context_chars = max_context_chars

    def fetch(self) -> Optional[str]:
        url = f"/projects/{self.project_id}/merge_requests/{self.mr_iid}"
        try:
            mr = self.api.get(url)
        except Exception as e:
            print(f"WARNING: 获取 MR 详情失败: {e}")
            return None

        labels = ", ".join(mr.get("labels") or []) or "无"
        reviewers = ", ".join(
            user.get("name", "") for user in mr.get("reviewers") or [] if user.get("name")
        ) or "无"
        assignees = ", ".join(
            user.get("name", "") for user in mr.get("assignees") or [] if user.get("name")
        ) or "无"
        description = truncate_text(mr.get("description", ""), max(400, self.max_context_chars // 2))

        parts = [
            "## 合并请求背景",
            f"### MR !{self.mr_iid}: {mr.get('title', '')}",
            f"- 源分支: {mr.get('source_branch', self.source_branch)}",
            f"- 目标分支: {mr.get('target_branch', '')}",
            f"- 当前状态: {mr.get('state', '')}",
            f"- Draft: {'是' if mr.get('draft') else '否'}",
            f"- 作者: {(mr.get('author') or {}).get('name', '未知')}",
            f"- 指派人: {assignees}",
            f"- Reviewers: {reviewers}",
            f"- Labels: {labels}",
        ]
        if description:
            parts.append(f"### MR 描述\n{description}")
        return "\n".join(parts)


class IssueFetcher:
    """从分支名提取 Issue 号并获取 Issue 详情"""

    BRANCH_PATTERN = re.compile(r"(bug|feat|hotfix)/(?:(.+?)/)?(\d+)/(.+)$")
    DEFAULT_PREFIX = "erp"
    DEFAULT_PROJECT_MAP = {"erp": "erp/erp"}

    def __init__(self, api: GitLabAPI, project_id: str, max_context_chars: int):
        self.api = api
        self.project_id = project_id
        self.max_context_chars = max_context_chars
        self.project_map = dict(self.DEFAULT_PROJECT_MAP)
        extra = os.environ.get("ISSUE_PROJECT_MAP", "")
        if extra:
            for item in extra.split(","):
                if "=" in item:
                    prefix, path = item.strip().split("=", 1)
                    self.project_map[prefix.strip()] = path.strip()

    def fetch(self, source_branch: str) -> Optional[str]:
        parsed = self._parse_branch(source_branch)
        if not parsed:
            print("INFO: 分支名不符合命名规范，跳过 Issue 上下文获取")
            return None

        project_prefix, issue_iid = parsed
        target_project_id = self._resolve_project_id(project_prefix)
        if not target_project_id:
            print(f"WARNING: 无法确定项目前缀 '{project_prefix}' 对应的项目")
            return None

        try:
            issue = self.api.get(f"/projects/{target_project_id}/issues/{issue_iid}")
            title = issue.get("title", "")
            description = truncate_text(
                issue.get("description", ""), max(400, self.max_context_chars // 2)
            )
            return f"## 需求背景\n### Issue {project_prefix}#{issue_iid}: {title}\n{description}"
        except Exception as e:
            print(f"WARNING: 获取 Issue {project_prefix}#{issue_iid} 失败: {e}")
            return None

    def _parse_branch(self, source_branch):
        match = self.BRANCH_PATTERN.match(source_branch)
        if not match:
            return None
        project_prefix = match.group(2) or self.DEFAULT_PREFIX
        issue_iid = match.group(3)
        return (project_prefix, issue_iid)

    def _resolve_project_id(self, project_prefix):
        project_path = self.project_map.get(project_prefix, f"erp/{project_prefix}")
        if project_path == "erp/sms":
            return self.project_id
        encoded_path = quote_plus(project_path)
        try:
            resp = self.api.get(f"/projects/{encoded_path}")
            return str(resp["id"])
        except Exception:
            return None


def build_diff_summary(diffs, max_files: int = 20):
    """生成变更摘要"""
    if not diffs:
        return None

    new_files = sum(1 for entry in diffs if entry.get("new_file"))
    deleted_files = sum(1 for entry in diffs if entry.get("deleted_file"))
    modified_files = len(diffs) - new_files - deleted_files

    file_lines = []
    for entry in diffs[:max_files]:
        if entry.get("new_file"):
            change_type = "新增"
        elif entry.get("deleted_file"):
            change_type = "删除"
        else:
            change_type = "修改"
        file_lines.append(
            f"- {entry.get('file_path', '')} ({change_type}, +{entry.get('added_lines', 0)}/-{entry.get('removed_lines', 0)})"
        )

    parts = [
        "## 变更摘要",
        f"- 文件总数: {len(diffs)}",
        f"- 新增文件: {new_files}",
        f"- 删除文件: {deleted_files}",
        f"- 修改文件: {modified_files}",
        "### 变更文件列表",
        *file_lines,
    ]
    if len(diffs) > max_files:
        parts.append(f"- 其余 {len(diffs) - max_files} 个文件未展开")
    return "\n".join(parts)


def assemble_context(mr_context, issue_context, diff_summary, max_chars):
    """将上下文组装为完整的用户消息，按优先级截断"""
    parts = [part for part in [mr_context, issue_context, diff_summary] if part]
    context = "\n\n".join(parts)
    if len(context) <= max_chars:
        return context

    trimmed_issue = issue_context
    if trimmed_issue:
        trimmed_issue = truncate_text(trimmed_issue, max(300, max_chars // 3))

    trimmed_mr = mr_context
    if trimmed_mr:
        trimmed_mr = truncate_text(trimmed_mr, max(400, max_chars // 3))

    remaining = max_chars - len(trimmed_mr or "") - len(trimmed_issue or "") - 4
    trimmed_summary = diff_summary
    if trimmed_summary and remaining > 0:
        trimmed_summary = truncate_text(trimmed_summary, max(300, remaining))

    return truncate_text(
        "\n\n".join(part for part in [trimmed_mr, trimmed_issue, trimmed_summary] if part),
        max_chars,
    )