"""Review Agent：内部拆为 3 个子 Agent，多线程并行"""

import sys
import threading
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
from core.llm_client import create_ai_client


def _run_sub_review(agent, prompt_path, diffs, context, results, key):
    """单个子 Agent 的执行逻辑（跑在线程中）"""
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
        result = agent.llm.review(prompt, context, diffs)
        parsed = ReviewResultParser.parse(result)
        results[key] = parsed
        print("  [review-%s] 完成，评分: %s/10" % (key, parsed.get("score", 0)))
    except Exception as e:
        print("  [review-%s] 失败: %s" % (key, e))
        results[key] = None


def _merge_results(sub_results):
    """合并 3 个子 Agent 的结果"""
    blocking = []
    non_blocking = []
    suggestions = []
    scores = []
    summaries = []

    for key in ["security", "logic", "quality"]:
        r = sub_results.get(key)
        if not r:
            continue
        blocking.extend(r.get("blocking_issues", []) or [])
        non_blocking.extend(r.get("non_blocking_issues", []) or [])
        suggestions.extend(r.get("other_suggestions", []) or [])
        scores.append(r.get("score", 0))
        if r.get("summary"):
            summaries.append("[%s] %s" % (key, r["summary"]))

    return {
        "summary": "\n".join(summaries) if summaries else "审查完成",
        "blocking_issues": blocking,
        "non_blocking_issues": non_blocking,
        "other_suggestions": suggestions,
        "score": min(scores) if scores else 0,
    }


class ReviewAgent(BaseAgent):
    """代码审查 Agent：内部拆为 security/logic/quality 并行审查，合并结果后发评论+门禁"""

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
        # 不再使用单一 prompt，拆到子线程中
        return ""

    def parse(self, result):
        return result  # 已经是合并后的 dict

    def execute(self, parsed):
        if not self.diffs:
            return

        score = parsed.get("score", 0)
        threshold = getattr(self.config, 'SCORE_THRESHOLD', 7)
        print("INFO: AI 审查最终评分: %s/10，阈值: %s" % (score, threshold))

        # 1. 发逐行评论（所有 blocking + non_blocking）
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
        mr_iid = self.config.MR_IID
        writer = MRCommentWriter(self.gitlab, self.config.PROJECT_ID)

        if score >= threshold:
            score_status = "✅ 评分通过（%s/10 >= 阈值 %s），将自动合并" % (score, threshold)
            writer.write(mr_iid, parsed, score_status=score_status)
            self._merge_mr(mr_iid)
        else:
            score_status = "❌ 评分未通过（%s/10 < 阈值 %s），需要人工审查后手动合并" % (score, threshold)
            writer.write(mr_iid, parsed, score_status=score_status)
            sys.exit(1)

    def run(self):
        """重写 run() 方法：3 个子 Agent 并行，然后合并"""
        data = self.prepare_data()
        if not data["diffs"]:
            return

        diffs = data["diffs"]
        context = data["context"]
        prompt_dir = Path(__file__).parent.parent / "prompts"

        # 3 个子 Agent，每个创建独立的 LLM 客户端（requests 线程安全）
        sub_agents = {
            "security": {
                "agent": ReviewAgent.__new__(ReviewAgent),
                "prompt": prompt_dir / "review_security.md",
            },
            "logic": {
                "agent": ReviewAgent.__new__(ReviewAgent),
                "prompt": prompt_dir / "review_logic.md",
            },
            "quality": {
                "agent": ReviewAgent.__new__(ReviewAgent),
                "prompt": prompt_dir / "review_quality.md",
            },
        }
        for k, v in sub_agents.items():
            v["agent"].config = self.config
            v["agent"].llm = create_ai_client(self.config)

        # 并行执行
        results = {}
        threads = []
        for key, info in sub_agents.items():
            t = threading.Thread(
                target=_run_sub_review,
                args=(info["agent"], info["prompt"], diffs, context, results, key),
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        # 合并结果
        merged = _merge_results(results)
        parsed = merged

        print("安全评分: %s, 逻辑评分: %s, 质量评分: %s" % (
            (results.get("security") or {}).get("score", "N/A"),
            (results.get("logic") or {}).get("score", "N/A"),
            (results.get("quality") or {}).get("score", "N/A"),
        ))
        print("合并评分: %s/10" % parsed.get("score", 0))
        print("阻断问题: %s, 非阻断问题: %s" % (
            len(parsed.get("blocking_issues", [])),
            len(parsed.get("non_blocking_issues", [])),
        ))

        self.execute(parsed)

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

            print("WARNING: MR !%s 暂不可合并（%s），审查评论已发布" % (mr_iid, detail))
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