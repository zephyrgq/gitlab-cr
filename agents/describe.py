"""Describe Agent：自动生成 MR 描述"""

import json
import sys
from pathlib import Path

from agents.base import BaseAgent


class DescribeAgent(BaseAgent):
    """自动生成 MR 标题和描述"""

    def prepare_data(self):
        if not self.diffs:
            print("INFO: 无代码变更，跳过 describe")
            return {"diffs": [], "context": ""}

        # 获取 MR 当前标题作为参考
        mr_title = ""
        try:
            url = f"/projects/{self.config.PROJECT_ID}/merge_requests/{self.config.MR_IID}"
            mr = self.gitlab.get(url)
            mr_title = mr.get("title", "")
        except Exception:
            pass

        context = f"## MR 标题（可选参考）\n{mr_title}" if mr_title else ""
        return {"diffs": self.diffs, "context": context}

    def build_prompt(self):
        prompt_path = Path(__file__).parent.parent / "prompts" / "describe.md"
        return prompt_path.read_text(encoding="utf-8")

    def parse(self, result: str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            print(f"WARNING: describe 解析失败，原始输出: {result[:200]}")
            return None

    def execute(self, parsed):
        if not parsed or not self.diffs:
            return

        title = parsed.get("title", "")
        description = parsed.get("description", "")

        if not title and not description:
            print("INFO: describe 未生成有效内容，跳过")
            return

        update_data = {}
        if title:
            update_data["title"] = title
        if description:
            current_desc = ""
            try:
                mr = self.gitlab.get(
                    f"/projects/{self.config.PROJECT_ID}/merge_requests/{self.config.MR_IID}"
                )
                current_desc = mr.get("description", "") or ""
            except Exception:
                pass
            # 在原有描述前添加 AI 生成的描述
            update_data["description"] = f"> 🤖 AI 自动生成\n\n{description}\n\n---\n\n{current_desc}"

        try:
            url = f"/projects/{self.config.PROJECT_ID}/merge_requests/{self.config.MR_IID}"
            resp = self.gitlab.put(url, json_data=update_data)
            if resp.ok:
                print("INFO: MR 描述已自动更新")
            else:
                print(f"WARNING: 更新 MR 描述失败: {resp.status_code}")
        except Exception as e:
            print(f"WARNING: 更新 MR 描述异常: {e}")