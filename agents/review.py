"""Review Agent：单次 LLM 调用，内部按 security/logic/quality 三阶段分析"""

import json
import sys
from pathlib import Path

from agents.base import BaseAgent
from core.comment_writer import (
    MRCommentWriter,
    MRDiscussionWriter,
    ReviewResultParser,
    parse_location,
)
from core.context_builder import (
    MergeRequestFetcher,
    IssueFetcher,
    build_diff_summary,
    assemble_context,
)


def _extract_dimension_scores(raw: str):
    """从 LLM 输出中提取每个维度的评分（用于日志）"""
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return {
        "security": (data.get("security") or {}).get("score", "?"),
        "logic": (data.get("logic") or {}).get("score", "?"),
        "quality": (data.get("quality") or {}).get("score", "?"),
    }


class ReviewAgent(BaseAgent):
    """代码审查 Agent：基于结构化 prompt 一次完成安全+逻辑+质量审查"""

    def prepare_data(self):
        if not self.diffs:
            print("INFO: 无代码变更，跳过审查")
            return {"diffs": [], "context": ""}

        mr_context = MergeRequestFetcher(
            self.gitlab, self.config.PROJECT_ID, self.config.MR_IID,
            self.config.SOURCE_BRANCH, self.config.MAX_CONTEXT_CHARS
        ).fetch()

        issue_context = IssueFetcher(
            self.gitlab, self.config.PROJECT_ID, self.config.MAX_CONTEXT_CHARS
        ).fetch(self.config.SOURCE_BRANCH)

        diff_summary = build_diff_summary(self.diffs)

        context = assemble_context(
            mr_context, issue_context, diff_summary, self.config.MAX_CONTEXT_CHARS
        )
        print("INFO: 审查上下文长度: %s 字符" % len(context))

        return {"diffs": self.diffs, "context": context}

    def build_prompt(self):
        prompt_path = Path(__file__).parent.parent / "prompts" / "review.md"
        return prompt_path.read_text(encoding="utf-8")

    def parse(self, result: str):
        # 日志显示各维度评分
        dims = _extract_dimension_scores(result)
        if dims:
            print("INFO: 安全=%s  逻辑=%s  质量=%s" % (
                dims.get("security"), dims.get("logic"), dims.get("quality")
            ))
        return ReviewResultParser.parse(result)

    def execute(self, parsed):
        if not self.diffs:
            return

        discussion_writer = MRDiscussionWriter(self.gitlab, self.config.PROJECT_ID, self.diffs)
        parsed = self._filter_to_diff_issues(parsed, discussion_writer)
        score = parsed.get("score", 0)
        threshold = getattr(self.config, 'SCORE_THRESHOLD', 7)
        print("INFO: 最终评分: %s/10，阈值: %s" % (score, threshold))
        print("INFO: 阻断问题: %s  非阻断问题: %s" % (
            len(parsed.get("blocking_issues", [])),
            len(parsed.get("non_blocking_issues", [])),
        ))

        # 1. 发逐行评论
        for issue in parsed.get("blocking_issues", []):
            file_path, line = parse_location(issue.get("location", ""))
            if file_path and line:
                try:
                    discussion_writer.post_comment(
                        self.config.MR_IID, file_path, line,
                        issue.get("severity", "critical"),
                        issue["title"],
                        issue.get("description", ""),
                        issue.get("suggestion", ""),
                    )
                except Exception as e:
                    print("WARNING: 行级评论失败 (%s): %s" % (issue["title"], e))

        for issue in parsed.get("non_blocking_issues", []):
            file_path, line = parse_location(issue.get("location", ""))
            if file_path and line:
                try:
                    discussion_writer.post_comment(
                        self.config.MR_IID, file_path, line,
                        issue.get("severity", "warning"),
                        issue["title"],
                        issue.get("description", ""),
                        issue.get("suggestion", ""),
                    )
                except Exception as e:
                    print("WARNING: 行级评论失败 (%s): %s" % (issue["title"], e))

        # 2. 发总结评论 + 门禁
        writer = MRCommentWriter(self.gitlab, self.config.PROJECT_ID)

        if score >= threshold:
            score_status = "✅ 评分通过（%s/10 >= 阈值 %s），将自动合并" % (score, threshold)
            writer.write(self.config.MR_IID, parsed, score_status=score_status)
            self._merge_mr(self.config.MR_IID)
        else:
            score_status = "❌ 评分未通过（%s/10 < 阈值 %s），需要人工审查后手动合并" % (score, threshold)
            writer.write(self.config.MR_IID, parsed, score_status=score_status)
            sys.exit(1)

    def _filter_to_diff_issues(self, parsed, discussion_writer):
        filtered = dict(parsed)
        dropped = []
        for field in ("blocking_issues", "non_blocking_issues"):
            kept = []
            for issue in parsed.get(field, []) or []:
                file_path, line = parse_location(issue.get("location", ""))
                if file_path and line and discussion_writer.is_diff_position(file_path, line):
                    kept.append(issue)
                else:
                    dropped.append(issue)
            filtered[field] = kept

        if dropped:
            print("INFO: 已丢弃 %s 条不在 MR diff 中的问题" % len(dropped))
        return filtered

    def _merge_mr(self, mr_iid: str):
        url = "/projects/%s/merge_requests/%s/merge" % (self.config.PROJECT_ID, mr_iid)
        resp = self.gitlab.put(url, json_data={"should_remove_source_branch": False})

        if resp.status_code == 405:
            try:
                detail = resp.json().get("message", "") or resp.text
            except Exception:
                detail = resp.text

            if "already merged" in detail.lower() or "closed" in detail.lower():
                print("INFO: MR !%s 已被合并或关闭，跳过合并操作" % mr_iid)
                return

            print("WARNING: MR !%s 暂不可合并（%s）" % (mr_iid, detail))
            return
        elif not resp.ok:
            try:
                detail = resp.json().get("message", resp.text)
            except Exception:
                detail = resp.text
            print("ERROR: 合并 MR !%s 失败 (HTTP %s): %s" % (mr_iid, resp.status_code, detail))
            sys.exit(1)
        else:
            print("INFO: MR !%s 自动合并成功" % mr_iid)