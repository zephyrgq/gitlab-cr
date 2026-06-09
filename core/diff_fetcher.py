#!/usr/bin/env python3
"""Diff 获取、过滤、源码上下文提取"""

import os
import re
import sys
from pathlib import Path
from typing import Optional

from core.gitlab_api import GitLabAPI

# 忽略的文件路径模式
IGNORE_PATTERNS = ["migrations/"]

# 忽略的二进制文件扩展名
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pyc", ".pyo", ".so", ".o",
    ".zip", ".tar", ".gz",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
}

HUNK_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
CONTEXT_PADDING = 20
MAX_LINES_PER_FILE = 160
MAX_FILES = 12


class DiffFetcher:
    """通过 GitLab API 获取 MR 的文件变更及源码上下文"""

    def __init__(self, gitlab_url: str, token: str, project_id: str, mr_iid: str,
                 review_scope: str = "full", repo_root: Optional[str] = None,
                 max_context_chars: int = 2000):
        self.api = GitLabAPI(gitlab_url, token)
        self.project_id = project_id
        self.mr_iid = mr_iid
        self.review_scope = review_scope
        self.repo_root = Path(repo_root or os.environ.get("CI_PROJECT_DIR", os.getcwd()))
        self.max_context_chars = max_context_chars

    @staticmethod
    def _should_include(diff_entry: dict) -> bool:
        file_path = diff_entry.get("new_path", "") or diff_entry.get("old_path", "")
        if diff_entry.get("binary", False):
            return False
        _, ext = os.path.splitext(file_path)
        if ext.lower() in BINARY_EXTENSIONS:
            return False
        for pattern in IGNORE_PATTERNS:
            if pattern in file_path:
                return False
        return True

    def fetch(self):
        if self.review_scope == "latest":
            diffs = self._fetch_latest_push_diffs()
            if diffs is not None:
                if not diffs:
                    print("INFO: latest 模式下无新增代码变更，跳过审查")
                return diffs
            print("WARNING: latest 模式无法获取增量 diff，回退到 full 模式")
        return self._fetch_full_mr_diffs()

    def _fetch_full_mr_diffs(self):
        url = f"/projects/{self.project_id}/merge_requests/{self.mr_iid}/diffs"
        all_diffs = []
        page = 1
        per_page = 100

        try:
            while True:
                response = self.api.get(url, params={"page": page, "per_page": per_page})
                if not response:
                    break
                for entry in response:
                    if self._should_include(entry):
                        all_diffs.append({
                            "file_path": entry.get("new_path", "") or entry.get("old_path", ""),
                            "old_path": entry.get("old_path", "") or entry.get("new_path", ""),
                            "new_path": entry.get("new_path", "") or entry.get("old_path", ""),
                            "diff": entry.get("diff", ""),
                            "new_file": entry.get("new_file", False),
                            "deleted_file": entry.get("deleted_file", False),
                        })
                if len(response) < per_page:
                    break
                page += 1
        except Exception as e:
            print(f"ERROR: 获取 MR diff 失败: {e}")
            sys.exit(1)

        return self._enrich(all_diffs)

    def _fetch_latest_push_diffs(self):
        versions_url = f"/projects/{self.project_id}/merge_requests/{self.mr_iid}/versions"
        try:
            versions = self.api.get(versions_url, params={"per_page": 20})
        except Exception as e:
            print(f"WARNING: 获取 MR versions 失败: {e}")
            return None

        if not isinstance(versions, list) or len(versions) < 2:
            return None

        versions = sorted(
            (item for item in versions if isinstance(item, dict)),
            key=lambda item: int(item.get("id", 0) or 0),
        )
        prev = versions[-2]
        latest = versions[-1]
        from_sha = prev.get("head_commit_sha")
        to_sha = latest.get("head_commit_sha")
        if not from_sha or not to_sha or from_sha == to_sha:
            return []

        compare_url = f"/projects/{self.project_id}/repository/compare"
        try:
            compare_data = self.api.get(compare_url, params={"from": from_sha, "to": to_sha, "straight": True})
        except Exception as e:
            print(f"WARNING: 获取 latest 增量 compare 失败: {e}")
            return None

        diffs = compare_data.get("diffs") if isinstance(compare_data, dict) else None
        if not isinstance(diffs, list):
            return None

        incremental = []
        for entry in diffs:
            if self._should_include(entry):
                incremental.append({
                    "file_path": entry.get("new_path", "") or entry.get("old_path", ""),
                    "old_path": entry.get("old_path", "") or entry.get("new_path", ""),
                    "new_path": entry.get("new_path", "") or entry.get("old_path", ""),
                    "diff": entry.get("diff", ""),
                    "new_file": entry.get("new_file", False),
                    "deleted_file": entry.get("deleted_file", False),
                })
        return self._enrich(incremental)

    def _enrich(self, diffs):
        """为每个 diff 条目添加源码上下文和行数统计"""
        if not diffs:
            return diffs

        for entry in diffs[:MAX_FILES]:
            added, removed = self._count_changed_lines(entry.get("diff", ""))
            entry["added_lines"] = added
            entry["removed_lines"] = removed
            entry["source_context"] = self._build_file_context(entry)

        return diffs

    @staticmethod
    def _count_changed_lines(diff_text: str):
        added = 0
        removed = 0
        for line in diff_text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        return added, removed

    def _build_file_context(self, diff_entry: dict) -> Optional[str]:
        if diff_entry.get("deleted_file"):
            return None
        file_path = (diff_entry.get("file_path") or "").strip()
        if not file_path:
            return None
        abs_path = (self.repo_root / file_path).resolve()
        try:
            if not abs_path.exists() or not abs_path.is_file():
                return None
            content = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return None

        lines = content.splitlines()
        ranges = self._extract_line_ranges(diff_entry.get("diff", ""), len(lines))
        if not ranges:
            if not lines:
                return None
            ranges = [(1, min(len(lines), MAX_LINES_PER_FILE))]

        snippets = []
        consumed = 0
        for start, end in ranges:
            snippet_count = end - start + 1
            if consumed >= MAX_LINES_PER_FILE:
                break
            if consumed + snippet_count > MAX_LINES_PER_FILE:
                end = start + (MAX_LINES_PER_FILE - consumed) - 1
            snippets.append(self._format_snippet(lines, start, end))
            consumed += snippet_count

        return "\n\n".join(snippets) if snippets else None

    def _extract_line_ranges(self, diff_text: str, total_lines: int):
        ranges = []
        for start_str, count_str in HUNK_PATTERN.findall(diff_text or ""):
            start = int(start_str)
            count = int(count_str or "1")
            if count == 0:
                count = 1
            end = start + count - 1
            padded_start = max(1, start - CONTEXT_PADDING)
            padded_end = min(total_lines, end + CONTEXT_PADDING)
            ranges.append((padded_start, padded_end))
        return self._merge_ranges(ranges)

    @staticmethod
    def _merge_ranges(ranges):
        if not ranges:
            return []
        sorted_ranges = sorted(ranges)
        merged = [sorted_ranges[0]]
        for start, end in sorted_ranges[1:]:
            last_start, last_end = merged[-1]
            if start <= last_end + 1:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def _format_snippet(lines, start: int, end: int) -> str:
        body = "\n".join(f"{line_no:>4}: {lines[line_no - 1]}" for line_no in range(start, end + 1))
        return f"### 行 {start}-{end}\n```text\n{body}\n```"