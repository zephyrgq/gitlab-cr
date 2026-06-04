#!/usr/bin/env python3
"""GitLab API 基础客户端，封装 GET/POST/PUT 请求"""

from typing import Optional

import requests


class GitLabAPI:
    """GitLab API 客户端"""

    def __init__(self, gitlab_url: str, token: str):
        self.base_url = gitlab_url
        self.headers = {"PRIVATE-TOKEN": token}

    def get(self, url: str, params: Optional[dict] = None) -> dict:
        full_url = f"{self.base_url}{url}"
        resp = requests.get(full_url, headers=self.headers, params=params or {}, timeout=180)
        resp.raise_for_status()
        return resp.json()

    def post(self, url: str, json_data: Optional[dict] = None) -> dict:
        full_url = f"{self.base_url}{url}"
        resp = requests.post(full_url, headers=self.headers, json=json_data or {}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def put(self, url: str, json_data: Optional[dict] = None) -> requests.Response:
        full_url = f"{self.base_url}{url}"
        resp = requests.put(full_url, headers=self.headers, json=json_data or {}, timeout=30)
        return resp