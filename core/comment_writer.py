#!/usr/bin/env python3
"""评论发布：MR 总结评论 + 行级评论"""

import json
import re
import sys
from typing import Optional

import requests

from core.gitlab_api import GitLabAPI


# 行号匹配模式：文件路径:行号
LOCATION_RE = re.compile(r"^(.+?):(\d+)$")


def parse_location(location: str):
    """从 'src/api/order.py:120' 中解析文件路径和行号"""
    m = LOCATION_RE.match(location.strip())
    if m:
        return m.group(1), int(m.group(2))
    return None, None


class ReviewResultParser:
    """将模型输出解析为结构化审查结果"""

    DEFAULT_REVIEW = {
        "summary": "",
        "blocking_issues": [],
        "non_blocking_issues": [],
        "other_suggestions": [],
        "score": 0,
    }
    JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)

    @classmethod
    def parse(cls, review_text: str):
        payload = cls._extract_json_payload(review_text)
        if payload is None:
            score = ScoreParser.parse(review_text)
            result = dict(cls.DEFAULT_REVIEW)
            result["summary"] = truncate_text(review_text, 2000)
            result["score"] = score
            return result
        return cls._normalize(payload, review_text)

    @classmethod
    def _extract_json_payload(cls, review_text: str):
        text = (review_text or "").strip()
        if not text:
            return None
        fence_match = cls.JSON_BLOCK_PATTERN.search(text)
        if fence_match:
            text = fence_match.group(1).strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start: end + 1])
        except Exception:
            return None

    @classmethod
    def _normalize(cls, payload, review_text: str):
        result = dict(cls.DEFAULT_REVIEW)
        if not isinstance(payload, dict):
            result["summary"] = truncate_text(review_text, 2000)
            result["score"] = ScoreParser.parse(review_text)
            return result
        result["summary"] = str(payload.get("summary") or "").strip()
        result["blocking_issues"] = cls._normalize_issue_list(
            payload.get("blocking_issues") or [], default_severity="critical"
        )
        result["non_blocking_issues"] = cls._normalize_issue_list(
            payload.get("non_blocking_issues") or [], default_severity="warning"
        )
        suggestions = payload.get("other_suggestions") or []
        if isinstance(suggestions, list):
            result["other_suggestions"] = [str(item).strip() for item in suggestions if str(item).strip()]
        score = payload.get("score", 0)
        try:
            score = int(score if score is not None else 0)
        except Exception:
            score = ScoreParser.parse(review_text)
        result["score"] = max(0, min(10, score))
        return result

    @staticmethod
    def _normalize_issue_list(issues, default_severity: str):
        normalized = []
        if not isinstance(issues, list):
            return normalized
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            title = str(issue.get("title") or "").strip()
            location = str(issue.get("location") or "").strip()
            description = str(issue.get("description") or "").strip()
            suggestion = str(issue.get("suggestion") or "").strip()
            severity = str(issue.get("severity") or default_severity).strip().lower()
            if not any([title, location, description, suggestion]):
                continue
            normalized.append({
                "title": title,
                "location": location,
                "description": description,
                "suggestion": suggestion,
                "severity": severity,
            })
        return normalized


class ScoreParser:
    """从 AI 审查结果中解析评分"""

    SCORE_PATTERN = re.compile(r"\*\*评分:\s*(\d+)/10\*\*")
    JSON_SCORE_PATTERN = re.compile(r'"score"\s*:\s*(\d+)', re.IGNORECASE)
    DEFAULT_SCORE = 0

    @staticmethod
    def parse(review_text: str) -> int:
        matches = ScoreParser.SCORE_PATTERN.findall(review_text)
        if not matches:
            matches = ScoreParser.JSON_SCORE_PATTERN.findall(review_text)
        if not matches:
            print("WARNING: 未在审查结果中找到评分，返回默认值 0")
            return ScoreParser.DEFAULT_SCORE
        scores = []
        for m in matches:
            val = int(m)
            val = max(1, min(10, val))
            scores.append(val)
        return min(scores)


def truncate_text(text: str, max_chars: int, suffix: str = "\n...(truncated)") -> str:
    normalized = (text or "").strip()
    if max_chars <= 0 or len(normalized) <= max_chars:
        return normalized
    keep = max_chars - len(suffix)
    if keep <= 0:
        return suffix[:max_chars]
    return normalized[:keep].rstrip() + suffix


class MRDiscussionWriter:
    """在 MR diff 的指定行上创建 Discussion（行级评论）"""

    HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    def __init__(self, api: GitLabAPI, project_id: str, diffs=None):
        self.api = api
        self.project_id = project_id
        self._shas = None
        self._diff_refs = self._build_diff_refs(diffs or [])

    @classmethod
    def _build_diff_position_map(cls, diff_text: str):
        positions = {}
        old_line = None
        new_line = None

        for raw_line in (diff_text or "").splitlines():
            header_match = cls.HUNK_HEADER_RE.match(raw_line)
            if header_match:
                old_line = int(header_match.group(1))
                new_line = int(header_match.group(3))
                continue

            if old_line is None or new_line is None:
                continue
            if raw_line.startswith("\\ No newline at end of file"):
                continue
            if raw_line.startswith("+"):
                positions[new_line] = {"new_line": new_line}
                new_line += 1
                continue
            if raw_line.startswith("-"):
                old_line += 1
                continue

            old_line += 1
            new_line += 1

        return positions

    @classmethod
    def _build_diff_refs(cls, diffs):
        refs = {}
        for entry in diffs:
            if not isinstance(entry, dict):
                continue
            new_path = str(entry.get("new_path") or entry.get("file_path") or "").strip()
            old_path = str(entry.get("old_path") or new_path).strip()
            file_path = str(entry.get("file_path") or new_path or old_path).strip()
            if not file_path:
                continue
            ref = {
                "old_path": old_path or file_path,
                "new_path": new_path or file_path,
                "positions": cls._build_diff_position_map(entry.get("diff", "")),
            }
            for alias in {file_path, old_path, new_path}:
                alias = str(alias or "").strip()
                if alias:
                    refs[alias] = ref
        return refs

    def _build_diff_position(self, file_path: str, line: int):
        ref = self._diff_refs.get((file_path or "").strip())
        if not ref:
            return None
        line_position = ref["positions"].get(line)
        if not line_position:
            return None
        position = {
            "old_path": ref["old_path"],
            "new_path": ref["new_path"],
        }
        position.update(line_position)
        return position

    def is_diff_position(self, file_path: str, line: int) -> bool:
        return self._build_diff_position(file_path, line) is not None

    @staticmethod
    def _format_body(severity: str, title: str, description: str = "", suggestion: str = "", location: str = ""):
        body_parts = [f"**[{severity.upper()}]** {title}"]
        if location:
            body_parts.append(f"> 位置: {location}")
        if description:
            body_parts.append(description)
        if suggestion:
            body_parts.append(f"> 💡 {suggestion}")
        return "\n\n".join(body_parts)

    def _get_diff_shas(self, mr_iid: str):
        """从 MR versions API 获取 base_sha / start_sha / head_sha"""
        if self._shas:
            return self._shas
        versions_url = f"/projects/{self.project_id}/merge_requests/{mr_iid}/versions"
        versions = self.api.get(versions_url, params={"per_page": 1})
        if not versions:
            raise RuntimeError("无法获取 MR versions")
        v = versions[0]
        self._shas = (v["base_commit_sha"], v["start_commit_sha"], v["head_commit_sha"])
        return self._shas

    def post_comment(self, mr_iid: str, file_path: str, line: int, severity: str,
                     title: str, description: str = "", suggestion: str = ""):
        """在指定文件的指定行上创建一个 Discussion"""
        location = f"{file_path}:{line}"
        body = self._format_body(severity, title, description, suggestion)
        url = f"/projects/{self.project_id}/merge_requests/{mr_iid}/discussions"
        position = self._build_diff_position(file_path, line)

        if not position:
            print("INFO: 跳过非 diff 行问题，不发评论: %s" % location)
            return False

        base_sha, start_sha, head_sha = self._get_diff_shas(mr_iid)
        payload = {
            "body": body,
            "position": {
                "base_sha": base_sha,
                "start_sha": start_sha,
                "head_sha": head_sha,
                "position_type": "text",
                **position,
            },
        }

        try:
            self.api.post(url, json_data=payload)
        except requests.exceptions.HTTPError as exc:
            response = getattr(exc, "response", None)
            if response is None or response.status_code != 400:
                raise
            print("WARNING: GitLab 拒绝 diff 行级评论，已跳过 (%s): %s" % (location, exc))
            return False
        return True


class MRCommentWriter:
    """将审查结果以总结评论形式发布到 MR"""

    COMMENT_PREFIX = "🤖 AI Code Review"
    NO_ISSUES_MESSAGE = "🤖 AI Code Review\n\n未发现明显问题，代码看起来不错 ✅"
    METADATA_START = "<!-- AI_REVIEW_METADATA_START"
    METADATA_END = "AI_REVIEW_METADATA_END -->"

    def __init__(self, api: GitLabAPI, project_id: str):
        self.api = api
        self.project_id = project_id

    def write(self, mr_iid: str, review_data, score_status: Optional[str] = None):
        """发布或更新审查评论到 MR"""
        previous_note = self._find_latest_ai_note(mr_iid)
        previous_review = self._extract_metadata((previous_note or {}).get("body", ""))
        delta = self._build_delta(review_data, previous_review)
        body = self._build_body(review_data, delta, score_status)
        url = f"/projects/{self.project_id}/merge_requests/{mr_iid}/notes"

        try:
            if previous_note:
                update_url = f"{url}/{previous_note['id']}"
                resp = self.api.put(update_url, json_data={"body": body})
                resp.raise_for_status()
                print("INFO: 已更新上一轮 AI 审查评论")
            else:
                self.api.post(url, json_data={"body": body})
                print("INFO: 审查评论发布成功")
        except Exception as e:
            print(f"ERROR: 发布审查评论失败: {e}")
            sys.exit(1)

    def _find_latest_ai_note(self, mr_iid: str):
        url = f"/projects/{self.project_id}/merge_requests/{mr_iid}/notes"
        try:
            notes = self.api.get(url, params={"per_page": 100})
        except Exception:
            return None

        if not isinstance(notes, list):
            return None
        ai_notes = [
            note for note in notes
            if isinstance(note, dict) and str(note.get("body") or "").startswith(self.COMMENT_PREFIX)
        ]
        if not ai_notes:
            return None
        return sorted(ai_notes, key=lambda item: item.get("id", 0))[-1]

    def _extract_metadata(self, body: str):
        start = (body or "").find(self.METADATA_START)
        end = (body or "").find(self.METADATA_END)
        if start == -1 or end == -1 or end <= start:
            return None
        payload = body[start + len(self.METADATA_START): end].strip()
        try:
            return json.loads(payload)
        except Exception:
            return None

    @staticmethod
    def _canonicalize_text(value: str) -> str:
        value = str(value or "").strip().lower()
        value = re.sub(r"\d+", "#", value)
        value = re.sub(r"[^\w一-鿿]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _normalize_issue_location(cls, location: str) -> str:
        normalized = cls._canonicalize_text(location)
        if not normalized:
            return ""
        normalized = normalized.replace(" line #", "")
        normalized = re.sub(r"(:|#l)#.*$", "", normalized)
        return normalized.strip()

    @staticmethod
    def _collect_issues(review_data):
        return (review_data.get("blocking_issues") or []) + (review_data.get("non_blocking_issues") or [])

    @classmethod
    def _issue_signature(cls, issue):
        severity = cls._canonicalize_text(issue.get("severity") or "warning")
        location = cls._normalize_issue_location(issue.get("location") or "")
        title = cls._canonicalize_text(issue.get("title") or "")
        description = cls._canonicalize_text(issue.get("description") or "")
        key_parts = [severity, location, title or description]
        return "|".join(part for part in key_parts if part)

    def _build_delta(self, current_review, previous_review):
        current_issues = self._collect_issues(current_review)
        previous_issues = self._collect_issues(previous_review or {})

        current_map = {self._issue_signature(issue): issue for issue in current_issues if self._issue_signature(issue)}
        previous_map = {self._issue_signature(issue): issue for issue in previous_issues if self._issue_signature(issue)}

        current_keys = set(current_map.keys())
        previous_keys = set(previous_map.keys())
        return {
            "added": [current_map[key] for key in sorted(current_keys - previous_keys)],
            "resolved": [previous_map[key] for key in sorted(previous_keys - current_keys)],
            "persisting": [current_map[key] for key in sorted(current_keys & previous_keys)],
        }

    def _build_body(self, review_data, delta, score_status: Optional[str]):
        summary = str(review_data.get("summary") or "").strip()
        score = review_data.get("score", 0)
        blocking_issues = review_data.get("blocking_issues") or []
        non_blocking_issues = review_data.get("non_blocking_issues") or []
        other_suggestions = review_data.get("other_suggestions") or []

        lines = [self.COMMENT_PREFIX, "", "### 审查摘要"]
        lines.append(summary or "未发现明显问题")
        lines.extend(["", "### 本轮变化",
                       f"- 新增问题: {len(delta.get('added') or [])}",
                       f"- 已解决问题: {len(delta.get('resolved') or [])}",
                       f"- 持续存在: {len(delta.get('persisting') or [])}"])

        if delta.get("added"):
            lines.extend(["", "#### 新增问题"])
            lines.extend(self._render_issue_list(delta["added"]))

        if delta.get("resolved"):
            lines.extend(["", "#### 已解决问题"])
            lines.extend(self._render_issue_list(delta["resolved"], resolved=True))

        if blocking_issues:
            lines.extend(["", "### 🔴 阻断问题"])
            lines.extend(self._render_issue_list(blocking_issues))

        if non_blocking_issues:
            lines.extend(["", "### 🟡 非阻断问题"])
            lines.extend(self._render_issue_list(non_blocking_issues))

        if other_suggestions:
            lines.extend(["", "### 💡 其他建议"])
            lines.extend(f"- {item}" for item in other_suggestions)

        if not blocking_issues and not non_blocking_issues and not other_suggestions:
            lines.extend(["", self.NO_ISSUES_MESSAGE.replace(self.COMMENT_PREFIX + "\n\n", "")])

        lines.extend(["", f"**评分: {score}/10**"])
        if score_status:
            lines.extend(["", score_status])

        metadata = {
            "summary": summary,
            "score": score,
            "blocking_issues": blocking_issues,
            "non_blocking_issues": non_blocking_issues,
            "other_suggestions": other_suggestions,
        }
        lines.extend(["", self.METADATA_START, json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), self.METADATA_END])
        return "\n".join(lines)

    @staticmethod
    def _render_issue_list(issues, resolved: bool = False):
        lines = []
        prefix = "- 已解决" if resolved else "-"
        for issue in issues:
            title = issue.get("title") or "未命名问题"
            location = issue.get("location") or "未知位置"
            description = issue.get("description") or ""
            suggestion = issue.get("suggestion") or ""
            severity = issue.get("severity") or "warning"
            lines.append(f"{prefix} [{severity}] {title}")
            lines.append(f"  - 位置: {location}")
            if description:
                lines.append(f"  - 问题: {description}")
            if suggestion:
                lines.append(f"  - 建议: {suggestion}")
        return lines