#!/usr/bin/env python3
"""
gitlab-cr 并行编排器

用户 CI 调用的入口（python3 /home/ytyfsu/apps/ai_review.py）。
并行执行 review / describe / improve 三个 Agent，review 结果决定 CI 是否通过。

要求：gitlab-cr 项目已部署在 GITLAB_CR_DIR 路径下。
"""

import os
import subprocess
import sys
import threading

GITLAB_CR_DIR = os.environ.get("GITLAB_CR_DIR", "/home/ytyfsu/apps/scm-cr")


def run_agent(agent_name: str, results: dict):
    """在子进程中运行指定 Agent"""
    print("[%s] === 启动 ===" % agent_name)
    result = subprocess.run(
        [sys.executable, os.path.join(GITLAB_CR_DIR, "main.py"), agent_name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=600,
    )
    for line in (result.stdout or "").splitlines():
        print("  [%s] %s" % (agent_name, line))
    for line in (result.stderr or "").splitlines():
        print("  [%s] [ERR] %s" % (agent_name, line))

    results[agent_name] = result.returncode
    print("[%s] === %s ===" % (agent_name, "完成" if result.returncode == 0 else "失败"))


def main():
    agent_names = ["review", "describe", "improve"]
    threads = []
    results = {}

    for name in agent_names:
        t = threading.Thread(target=run_agent, args=(name, results))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print(f"INFO: review={results.get('review')}, describe={results.get('describe')}, improve={results.get('improve')}")

    if results.get("review", 1) != 0:
        print("ERROR: 代码审查未通过")
        sys.exit(1)
    else:
        print("INFO: 全部完成")
        sys.exit(0)


if __name__ == "__main__":
    main()