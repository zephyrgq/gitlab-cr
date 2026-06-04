#!/usr/bin/env python3
"""
gitlab-cr 并行编排器

这是用户 CI 中调用的入口（python3 /home/ytyfsu/apps/ai_review.py）。
作用：
1. 自动 clone/pull gitlab-cr 项目到 /tmp/gitlab-cr/
2. 并行执行 review / describe / improve 三个 Agent
3. review 结果决定 CI 是否通过
"""

import os
import subprocess
import sys
import threading

# ⚠️ 使用前将 <your-namespace> 替换为你的 GitLab 命名空间
GITLAB_CR_REPO = os.environ.get(
    "GITLAB_CR_REPO",
    "https://gitlab.com/<your-namespace>/gitlab-cr.git",
)
GITLAB_CR_DIR = "/tmp/gitlab-cr"


def ensure_repo():
    """确保 gitlab-cr 代码已 clone，已存在则 pull 更新"""
    if os.path.isdir(GITLAB_CR_DIR):
        print("INFO: gitlab-cr 已存在，执行 git pull 更新...")
        subprocess.run(
            ["git", "-C", GITLAB_CR_DIR, "pull", "--ff-only"],
            capture_output=True, timeout=30,
        )
    else:
        print("INFO: 首次运行，clone gitlab-cr...")
        subprocess.run(
            ["git", "clone", GITLAB_CR_REPO, GITLAB_CR_DIR],
            capture_output=True, timeout=60,
        )


def run_agent(agent_name: str, results: dict):
    """在子进程中运行指定 Agent"""
    print(f"INFO: === 启动 {agent_name} Agent ===")
    result = subprocess.run(
        [sys.executable, os.path.join(GITLAB_CR_DIR, "main.py"), agent_name],
        capture_output=True, text=True, timeout=600,
    )
    # 输出 Agent 的日志
    for line in (result.stdout or "").splitlines():
        print(f"  [{agent_name}] {line}")
    for line in (result.stderr or "").splitlines():
        print(f"  [{agent_name}] [ERR] {line}")

    results[agent_name] = result.returncode
    if result.returncode == 0:
        print(f"INFO: === {agent_name} Agent 完成 ===")
    else:
        print(f"INFO: === {agent_name} Agent 失败 (exit code {result.returncode}) ===")


def main():
    # 1. 确保代码
    ensure_repo()

    # 2. 设置环境变量（透传当前环境）
    env = os.environ.copy()

    # 3. 并行启动三个 Agent
    threads = []
    results = {}

    agent_names = ["review", "describe", "improve"]

    for name in agent_names:
        t = threading.Thread(target=run_agent, args=(name, results))
        t.start()
        threads.append(t)

    # 4. 等待全部完成
    for t in threads:
        t.join()

    # 5. review 的结果决定 CI 是否通过
    review_code = results.get("review", 1)
    print(f"INFO: review={results.get('review')}, describe={results.get('describe')}, improve={results.get('improve')}")

    if review_code != 0:
        print("ERROR: 代码审查未通过，请查看 MR 中的评论详情")
        sys.exit(1)
    else:
        print("INFO: AI Code Review 全部完成")
        sys.exit(0)


if __name__ == "__main__":
    main()