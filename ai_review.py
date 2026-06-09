#!/usr/bin/env python3
"""
gitlab-cr 并行编排器

用户 CI 调用的入口（python3 /home/ytyfsu/apps/ai_review.py）。
默认并行执行 review / describe / improve 三个 Agent，review 结果决定 CI 是否通过。
可通过 AI_REVIEW_AGENTS 调整执行范围。

要求：gitlab-cr 项目已部署在 GITLAB_CR_DIR 路径下。
"""

import os
import subprocess
import sys
import threading

GITLAB_CR_DIR = os.environ.get("GITLAB_CR_DIR", "/home/ytyfsu/apps/scm-cr")
DEFAULT_AGENT_NAMES = ["review", "describe", "improve"]
VALID_AGENT_NAMES = {"review", "describe", "improve"}


def _parse_positive_int(env_name: str, default: int) -> int:
    value = os.environ.get(env_name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
        if parsed > 0:
            return parsed
    except ValueError:
        pass
    print("WARNING: %s=%r 无效，使用默认值 %s" % (env_name, value, default))
    return default


def _get_agent_timeout_seconds() -> int:
    request_timeout = _parse_positive_int("AI_REQUEST_TIMEOUT_SECONDS", 600)
    max_retries = _parse_positive_int("AI_MAX_RETRIES", 1)
    default_timeout = request_timeout * max_retries + 300
    return _parse_positive_int("AI_AGENT_TIMEOUT_SECONDS", default_timeout)


def _ensure_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _get_agent_names():
    raw = os.environ.get("AI_REVIEW_AGENTS", "").strip()
    if not raw:
        return DEFAULT_AGENT_NAMES

    names = []
    for item in raw.split(","):
        name = item.strip().lower()
        if not name:
            continue
        if name not in VALID_AGENT_NAMES:
            print("WARNING: 忽略未知 Agent: %s" % name)
            continue
        if name not in names:
            names.append(name)

    if "review" not in names:
        names.insert(0, "review")
    return names or DEFAULT_AGENT_NAMES


def run_agent(agent_name: str, results: dict):
    """在子进程中运行指定 Agent"""
    print("[%s] === 启动 ===" % agent_name)
    timeout_seconds = _get_agent_timeout_seconds()
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(GITLAB_CR_DIR, "main.py"), agent_name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_text = _ensure_text(exc.stdout)
        stderr_text = _ensure_text(exc.stderr)

        for line in stdout_text.splitlines():
            print("  [%s] %s" % (agent_name, line))
        for line in stderr_text.splitlines():
            print("  [%s] [ERR] %s" % (agent_name, line))

        results[agent_name] = 124
        print("[%s] === 超时失败（>%ss） ===" % (agent_name, timeout_seconds))
        return

    for line in (result.stdout or "").splitlines():
        print("  [%s] %s" % (agent_name, line))
    for line in (result.stderr or "").splitlines():
        print("  [%s] [ERR] %s" % (agent_name, line))

    results[agent_name] = result.returncode
    print("[%s] === %s ===" % (agent_name, "完成" if result.returncode == 0 else "失败"))


def main():
    agent_names = _get_agent_names()
    threads = []
    results = {}

    print("INFO: 本次执行 Agent: %s" % ", ".join(agent_names))

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