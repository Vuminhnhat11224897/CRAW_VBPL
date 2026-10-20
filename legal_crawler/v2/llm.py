"""Lào Cai LLM server (OpenAI-compatible vLLM) with the v2 writer's call_llm/validated_json contract.

call_llm and validated_json are vendored from data_vbpl/legal_ingest/integrations.py (commit 22d9796).
CRAWLER: endpoint and limits come from LLM_* settings (no auth, thinking disabled, 4000 max tokens,
5 s connect / 30 s read by default), and the daytime/night concurrency rule lives in `concurrency()`.
"""
import json
import os
import re
import threading
import time
from datetime import datetime

import requests


def setting(name, default, kind=str):
    return kind(os.getenv(name, default))


class Gate:
    """Global limit on simultaneous LLM requests, re-read from the clock on every request so a run that
    crosses 07:00 or 22:00 follows the new limit without a restart."""

    def __init__(self):
        self.cond = threading.Condition()
        self.active = 0

    def __enter__(self):
        with self.cond:
            while self.active >= concurrency():
                self.cond.wait(timeout=30)  # wake up periodically: the limit changes with the hour, not only on release
            self.active += 1

    def __exit__(self, *exc):
        with self.cond:
            self.active -= 1
            self.cond.notify_all()


GATE = Gate()


def max_workers():
    """Thread-pool size: the larger limit; GATE enforces the one that applies right now."""
    return max(setting("LLM_CCU", "3", int), setting("LLM_CCU_NIGHT", "10", int))


def call_llm(prompt, text, folder, model=None, json_output=True, json_schema=None, *, max_tokens=None, timeout=None):
    with GATE:
        return _call_llm(prompt, text, folder, model, json_output, json_schema, max_tokens=max_tokens, timeout=timeout)


def _call_llm(prompt, text, folder, model=None, json_output=True, json_schema=None, *, max_tokens=None, timeout=None):
    max_tokens = max_tokens or setting("LLM_MAX_TOKENS", "4000", int)
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError('max_tokens must be a positive integer')
    body = {"model": model or setting("LLM_MODEL", "qwen3.6-35b"), "temperature": 0, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}]}
    if json_schema is not None:
        body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'legal_extraction', 'strict': True, 'schema': json_schema}}
    elif json_output:
        body["response_format"] = {"type": "json_object"}
    base = setting("LLM_BASE_URL", "http://100.91.202.88:8012/v1").rstrip("/")
    response = requests.post(base + "/chat/completions",
                             headers={"Authorization": "Bearer " + setting("LLM_API_KEY", "EMPTY")}, json=body,
                             timeout=(setting("LLM_TIMEOUT_CONNECT", "5", float), timeout or setting("LLM_TIMEOUT_READ", "30", float)))
    response.raise_for_status()
    completion = response.json()
    if folder is not None:
        (folder / "llm_response.json").write_text(json.dumps(completion, ensure_ascii=False, indent=2), encoding="utf-8")
    choices = completion.get('choices') if isinstance(completion, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError('LLM response must contain a completion choice')
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise ValueError(f"LLM chưa hoàn tất: {choice.get('finish_reason')}")
    message = choice.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError('LLM response must contain nonempty text')
    return content, completion.get("usage")


def validated_json(prompt, text, folder, cache_name, validate, attempts=3, schema=None, *,
                   max_tokens=None, timeout=None, caller=None, check=None):
    """Cache only validated JSON; retain invalid attempts and request a bounded repair."""
    cache = folder / cache_name
    error = None
    if check:check()
    if cache.exists():
        try:
            payload = json.loads(cache.read_text())
            validate(payload)
            return payload
        except (ValueError, TypeError, KeyError) as exc:
            error = exc
    attempts_folder = folder / (cache.stem + '-attempts')
    existing = max((int(p.name) for p in attempts_folder.glob('*') if p.is_dir() and p.name.isdigit()), default=0)
    for attempt in range(attempts):
        audit = attempts_folder / str(existing + attempt + 1)
        audit.mkdir(parents=True, exist_ok=True)
        repair = '' if error is None else ('\nPhản hồi trước không hợp lệ: ' + str(error)[:800] +
            '. Hãy sửa schema/quy tắc đã yêu cầu. Chép dẫn chứng NGUYÊN VĂN từ nguồn nếu cần dẫn chứng. '
            'Trả một JSON hoàn chỉnh, ngắn gọn; giữ đủ các field yêu cầu. Không viết quá trình suy luận.')
        try:
            if check:check()
            options = {'json_schema': schema}
            if max_tokens is not None:options['max_tokens'] = max_tokens
            if timeout is not None:options['timeout'] = timeout
            content, usage = (caller or call_llm)(prompt + repair, text, audit, **options)
            if check:check()
            payload = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip()))
            validate(payload)
            cache.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            (folder / (cache.stem + '-usage.json')).write_text(json.dumps(usage))
            return payload
        except (ValueError, TypeError, KeyError, requests.ConnectionError, requests.Timeout) as exc:
            error = exc
        except requests.HTTPError as exc:
            if exc.response.status_code < 500 and exc.response.status_code != 429:
                raise
            error = exc
        (audit / 'error.json').write_text(json.dumps({'type': type(error).__name__, 'message': str(error)}, ensure_ascii=False))
        if attempt + 1 < attempts:
            time.sleep(attempt + 1)
    raise error


def concurrency(now=None):
    """Daytime (7h-22h) LLM_CCU = 3; night (22h-7h) LLM_CCU_NIGHT = 10."""
    hour = (now or datetime.now()).hour
    start, end = setting("LLM_NIGHT_START", "22", int), setting("LLM_NIGHT_END", "7", int)
    night = (hour >= start or hour < end) if start > end else start <= hour < end
    return setting("LLM_CCU_NIGHT", "10", int) if night else setting("LLM_CCU", "3", int)
