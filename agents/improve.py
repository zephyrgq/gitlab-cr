"""Improve Agent：代码改进建议"""

from pathlib import Path

from agents.base import BaseAgent
from core.comment_writer import MRDiscussionWriter, ReviewResultParser, parse_location


class ImproveAgent(BaseAgent):
    """代码改进建议 Agent：只提 non_blocking 建议，不阻断"""

    def include_source_context(self) -> bool:
        return False

    def prepare_data(self):
        if not self.diffs:
            print("INFO: 无代码变更，跳过 improve")
            return {"diffs": [], "context": ""}
        return {"diffs": self.diffs, "context": ""}

    def build_prompt(self):
        prompt_path = Path(__file__).parent.parent / "prompts" / "improve.md"
        return prompt_path.read_text(encoding="utf-8")

    def parse(self, result: str):
        return ReviewResultParser.parse(result)

    def execute(self, parsed):
        if not self.diffs:
            return

        discussion_writer = MRDiscussionWriter(self.gitlab, self.config.PROJECT_ID, self.diffs)
        non_blocking = self._filter_to_diff_issues(parsed.get("non_blocking_issues", []), discussion_writer)
        suggestions = parsed.get("other_suggestions", [])

        if not non_blocking and not suggestions:
            print("INFO: improve 未发现改进建议")
            return

        # 发逐行建议评论
        for issue in non_blocking:
            file_path, line = parse_location(issue.get("location", ""))
            if file_path and line:
                try:
                    discussion_writer.post_comment(
                        self.config.MR_IID, file_path, line,
                        issue.get("severity", "suggestion"),
                        issue["title"],
                        issue.get("description", ""),
                        issue.get("suggestion", ""),
                    )
                except Exception as e:
                    print(f"WARNING: 行级建议评论失败 ({issue['title']}): {e}")

        print(f"INFO: improve 完成，发现 {len(non_blocking)} 条建议")

    @staticmethod
    def _filter_to_diff_issues(issues, discussion_writer):
        kept = []
        dropped = 0
        for issue in issues or []:
            file_path, line = parse_location(issue.get("location", ""))
            if file_path and line and discussion_writer.is_diff_position(file_path, line):
                kept.append(issue)
            else:
                dropped += 1
        if dropped:
            print(f"INFO: improve 已丢弃 {dropped} 条不在 MR diff 中的建议")
        return kept