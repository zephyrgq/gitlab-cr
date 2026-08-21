#!/usr/bin/env python3
"""多模型 LLM 客户端，支持 OpenAI / DashScope / Zhipu"""

import json
import os
import sys
import time
from typing import Optional

import requests


class AIClientBase:
    """AI 客户端基类，定义共同的接口和共享逻辑"""

    RETRY_DELAY = 5
    MAX_RETRY_DELAY = 60
    MAX_DIFF_CHARS = 150000
    RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}

    @classmethod
    def _parse_retry_after(cls, value):
        """从 Retry-After 响应头解析秒数；无法解析时返回 None。"""
        if value is None:
            return None
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        return None

    def _get_retry_delay(self, status_code, attempt, response=None):
        """计算重试等待时间：429 优先使用服务端 Retry-After，否则指数退避。"""
        if response is not None and status_code == 429:
            retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None and retry_after > 0:
                return min(retry_after, self.MAX_RETRY_DELAY)

        if status_code in self.RETRYABLE_HTTP_STATUSES:
            return min(self.RETRY_DELAY * (2 ** (attempt - 1)), self.MAX_RETRY_DELAY)
        return self.RETRY_DELAY

    def _get_max_retries(self):
        return getattr(self.config, "AI_MAX_RETRIES", 3)

    def _get_request_timeout(self):
        return getattr(self.config, "AI_REQUEST_TIMEOUT", 600)

    @staticmethod
    def _log_http_failure(service_name, status_code, attempt, max_retries):
        action = "准备重试" if attempt < max_retries else "不再重试"
        print(f"WARNING: {service_name} API 请求失败（HTTP {status_code}），第 {attempt}/{max_retries} 次尝试失败，{action}")

    @staticmethod
    def _log_exception_failure(service_name, error, attempt, max_retries):
        action = "准备重试" if attempt < max_retries else "不再重试"
        print(f"WARNING: {service_name} API 请求异常: {error}，第 {attempt}/{max_retries} 次尝试失败，{action}")

    @staticmethod
    def _read_sse_stream(resp) -> str:
        """解析 OpenAI 兼容的 SSE 流，打印进度点，返回完整内容。"""
        chunks = []
        for raw in resp.iter_lines():
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0]["delta"].get("content", "")
            except Exception:
                continue
            if delta:
                chunks.append(delta)
                print(".", end="", flush=True)
        print()  # 换行
        return "".join(chunks)

    @staticmethod
    def _split_batches(diffs, max_chars=None):
        if max_chars is None:
            max_chars = AIClientBase.MAX_DIFF_CHARS
        if not diffs:
            return []

        batches = []
        current_batch = []
        current_chars = 0

        for diff_entry in diffs:
            diff_text = diff_entry.get("diff", "")
            entry_chars = len(diff_text)

            if not current_batch:
                current_batch.append(diff_entry)
                current_chars = entry_chars
                continue

            if current_chars + entry_chars > max_chars:
                batches.append(current_batch)
                current_batch = [diff_entry]
                current_chars = entry_chars
            else:
                current_batch.append(diff_entry)
                current_chars += entry_chars

        if current_batch:
            batches.append(current_batch)

        return batches

    def review(self, system_prompt, context, diffs):
        batches = self._split_batches(diffs)
        if not batches:
            return ""

        results = []
        for i, batch in enumerate(batches):
            diff_parts = []
            for entry in batch:
                file_path = entry.get("file_path", "")
                diff_text = entry.get("diff", "")
                source_context = entry.get("source_context")
                section_parts = [f"### {file_path}", f"```diff\n{diff_text}\n```"]
                if source_context:
                    section_parts.append(f"#### 源码上下文\n{source_context}")
                diff_parts.append("\n".join(section_parts))

            diff_content = "\n\n".join(diff_parts)
            user_content = f"{context}\n\n## 代码变更\n\n{diff_content}"

            if len(batches) > 1:
                print(f"INFO: 发送请求：批次 {i + 1}/{len(batches)}")

            result = self._call_api(system_prompt, user_content)
            results.append(result)

        return "\n\n---\n\n".join(results)

    def _call_api(self, system_prompt: str, user_content: str) -> str:
        raise NotImplementedError


class OpenAIClient(AIClientBase):
    def __init__(self, config):
        self.config = config

    def _call_api(self, system_prompt: str, user_content: str) -> str:
        last_error = None
        max_retries = self._get_max_retries()
        request_timeout = self._get_request_timeout()
        for attempt in range(1, max_retries + 1):
            if attempt == 1:
                proxies = getattr(self.config, "OPENAI_PROXIES", None)
            elif attempt == 2:
                proxies = getattr(self.config, "OPENAI_PROXIES_FALLBACK", None)
            else:
                proxies = None
            try:
                headers = {
                    "Authorization": f"Bearer {self.config.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.config.OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.2,
                    "stream": True,
                }
                session = requests.Session()
                session.verify = False

                resp = session.post(
                    f"{self.config.OPENAI_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                    proxies=proxies,
                    stream=True,
                )
                resp.raise_for_status()
                return self._read_sse_stream(resp)

            except requests.exceptions.HTTPError as e:
                last_error = e
                status_code = e.response.status_code if e.response is not None else None
                self._log_http_failure("OpenAI", status_code, attempt, max_retries)
                if attempt < max_retries:
                    delay = self._get_retry_delay(status_code, attempt, e.response)
                    time.sleep(delay)
            except Exception as e:
                last_error = e
                self._log_exception_failure("OpenAI", e, attempt, max_retries)
                if attempt < max_retries:
                    time.sleep(self.RETRY_DELAY)

        print(f"ERROR: OpenAI API 调用失败，已尝试 {max_retries} 次: {last_error}")
        sys.exit(1)


class DashScopeClient(AIClientBase):
    def __init__(self, config):
        self.config = config

    def _call_api(self, system_prompt: str, user_content: str) -> str:
        last_error = None
        max_retries = self._get_max_retries()
        request_timeout = self._get_request_timeout()
        for attempt in range(1, max_retries + 1):
            try:
                headers = {
                    "Authorization": f"Bearer {self.config.DASHSCOPE_API_KEY}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.config.DASHSCOPE_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.2,
                    "stream": True,
                }
                resp = requests.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                    stream=True,
                )
                resp.raise_for_status()
                return self._read_sse_stream(resp)

            except requests.exceptions.HTTPError as e:
                last_error = e
                status_code = e.response.status_code if e.response is not None else None
                self._log_http_failure("DashScope", status_code, attempt, max_retries)
                if attempt < max_retries:
                    delay = self._get_retry_delay(status_code, attempt, e.response)
                    time.sleep(delay)
            except Exception as e:
                last_error = e
                self._log_exception_failure("DashScope", e, attempt, max_retries)
                if attempt < max_retries:
                    time.sleep(self.RETRY_DELAY)

        print(f"ERROR: DashScope API 调用失败，已尝试 {max_retries} 次: {last_error}")
        sys.exit(1)


class ZhipuAIClient(AIClientBase):
    def __init__(self, config):
        self.config = config

    def _call_api(self, system_prompt: str, user_content: str) -> str:
        last_error = None
        max_retries = self._get_max_retries()
        request_timeout = self._get_request_timeout()
        for attempt in range(1, max_retries + 1):
            try:
                headers = {
                    "Authorization": f"Bearer {self.config.ZHIPU_API_KEY}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.config.ZHIPU_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.2,
                    "stream": True,
                }
                resp = requests.post(
                    "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                    stream=True,
                )
                resp.raise_for_status()
                return self._read_sse_stream(resp)

            except requests.exceptions.HTTPError as e:
                last_error = e
                status_code = e.response.status_code if e.response is not None else None
                self._log_http_failure("Zhipu", status_code, attempt, max_retries)
                if attempt < max_retries:
                    delay = self._get_retry_delay(status_code, attempt, e.response)
                    time.sleep(delay)
            except Exception as e:
                last_error = e
                self._log_exception_failure("Zhipu", e, attempt, max_retries)
                if attempt < max_retries:
                    time.sleep(self.RETRY_DELAY)

        print(f"ERROR: Zhipu API 调用失败，已尝试 {max_retries} 次: {last_error}")
        sys.exit(1)


def create_ai_client(config):
    ai_service = os.environ.get("AI_SERVICE", "dashscope")
    if ai_service == "openai":
        print("INFO: 使用 OpenAI 服务")
        return OpenAIClient(config)
    elif ai_service == "zhipu":
        print("INFO: 使用 Zhipu 服务")
        return ZhipuAIClient(config)
    else:
        print("INFO: 使用阿里百炼（DashScope）服务")
        return DashScopeClient(config)