#!/usr/bin/env python3
"""CLI 入口：gitlab-cr review | describe | improve"""

import argparse
import sys

from core.config import Config


def main():
    parser = argparse.ArgumentParser(description="gitlab-cr: GitLab AI Code Review")
    parser.add_argument("action", nargs="?", default="check-title",
                        choices=["review", "describe", "improve", "check-title"],
                        help="Agent 名称（默认: check-title）")
    args = parser.parse_args()

    config = Config.from_env(action=args.action)

    if args.action == "review":
        from agents.review import ReviewAgent
        agent = ReviewAgent(config)
    elif args.action == "describe":
        from agents.describe import DescribeAgent
        agent = DescribeAgent(config)
    elif args.action == "improve":
        from agents.improve import ImproveAgent
        agent = ImproveAgent(config)
    elif args.action == "check-title":
        from agents.title_checker import TitleCheckerAgent
        agent = TitleCheckerAgent(config)
    else:
        print(f"ERROR: 未知 action: {args.action}")
        sys.exit(1)

    agent.run()


if __name__ == "__main__":
    main()