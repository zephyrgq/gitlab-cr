"""Review Agent：代码审查"""

import os
import sys
from pathlib import Path

from agents.base import BaseAgent
from core.comment_writer import (
    MRCommentWriter,
    MRDiscussionWriter,
    ReviewResultParser,
    ScoreParser,
    parse_location,
)
from core.context_builder import (
    MergeRequestFetcher,
    IssueFetcher,
    build_diff_summary,
    assemble_context,
)


class ReviewAgent(BaseAgent):
    """代码审查 Agent：分析 diff，报告问题，评分，门禁"""

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
        print(f"INFO: 审查上下文长度: {len(context)} 字符")

        return {"diffs": self.diffs, "context": context}

    def build_prompt(self):
        prompt_path = Path(__file__).parent.parent / "prompts" / "review.md"
        return prompt_path.read_text(encoding="utf-8")

    def parse(self, result: str):
        return ReviewResultParser.parse(result)

    def execute(self, parsed):
        if not self.diffs:
            return

        score = parsed.get("score", 0)
        threshold = getattr(self.config, 'SCORE_THRESHOLD', 7)
        print(f"INFO: AI 审查评分: {score}/10，阈值: {threshold}")

        # 1. 发逐行评论
        discussion_writer = MRDiscussionWriter(self.gitlab, self.config.PROJECT_ID)
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
                    print(f"WARNING: 行级评论失败 ({issue['title']}): {e}")

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
                    print(f"WARNING: 行级评论失败 ({issue['title']}): {e}")

        # 2. 发总结评论 + 门禁
        mr_iid = self.config.MR_IID
        writer = MRCommentWriter(self.gitlab, self.config.PROJECT_ID)

        if score >= threshold:
            score_status = f"✅ 评分通过（{score}/10 >= 阈值 {threshold}），将自动合并"
            writer.write(mr_iid, parsed, score_status=score_status)
            self._merge_mr(mr_iid)
        else:
            score_status = f"❌ 评分未通过（{score}/10 < 阈值 {threshold}），需要人工审查后手动合并"
            writer.write(mr_iid, parsed, score_status=score_status)
            sys.exit(1)

    def _merge_mr(self, mr_iid: str):
        url = f"/projects/{self.config.PROJECT_ID}/merge_requests/{mr_iid}/merge"
        resp = self.gitlab.put(url, json_data={"should_remove_source_branch": False})

        if resp.status_code == 405:
            try:
                detail = resp.json().get("message", "") or resp.text
            except Exception:
                detail = resp.text

            if "already merged" in detail.lower() or "closed" in detail.lower():
                print(f"INFO: MR !{mr_iid} 已被合并或关闭，跳过合并操作")
                return

            print(f"WARNING: MR !{mr_iid} 暂不可合并（{detail}），审查评论已发布，请人工合并")
            return
        elif not resp.ok:
            try:
                detail = resp.json().get("message", resp.text)
            except Exception:
                detail = resp.text
            print(f"ERROR: 合并 MR !{mr_iid} 失败 (HTTP {resp.status_code}): {detail}")
            sys.exit(1)
        else:
            print(f"INFO: MR !{mr_iid} 自动合并成功")