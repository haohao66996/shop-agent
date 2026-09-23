# -*- coding: utf-8 -*-
"""LLM 接入层：provider 链 + 简易熔断 + JSON 提取（Instructor 结构化在阶段二接入）"""
import json
import logging
import re

from openai import OpenAI

from src.config import settings

log = logging.getLogger("llm")


class _Provider:
    def __init__(self, spec: str):
        name, model = spec.split(":", 1)
        self.name, self.model = name.strip(), model.strip()
        if self.name in ("vllm", "vllm2"):
            base = settings.vllm_base_url if self.name == "vllm" else settings.vllm2_base_url
            self.client = OpenAI(base_url=base,
                                 api_key=settings.vllm_api_key or "EMPTY", timeout=180)
        elif self.name == "deepseek" and settings.deepseek_api_key:
            self.client = OpenAI(base_url=settings.deepseek_base_url,
                                 api_key=settings.deepseek_api_key, timeout=180)
        else:
            self.client = None  # 未配置 key，跳过


_providers = [_Provider(k) for k in settings.llm_provider_chain.split(",") if k.strip()]
_providers = [p for p in _providers if p.client]

# 角色级 provider（模型分层）：Analyst/Reporter 指定更强的 14B，其余走全局链
_ROLE_PROVIDERS: dict[str, list[_Provider]] = {}
for _role in ("analyst", "reporter"):
    _spec = getattr(settings, f"{_role}_provider", "")
    if _spec:
        _p = _Provider(_spec)
        if _p.client:
            _ROLE_PROVIDERS[_role] = [_p]

if not _providers:
    raise RuntimeError("没有可用的 LLM provider，请检查 LLM_PROVIDER_CHAIN / API key")


def chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 2048,
         prefer: str | None = None) -> str:
    """按链路顺序调用（prefer 角色级 provider 最先），失败自动降级到下一个"""
    cands = list(_ROLE_PROVIDERS.get(prefer or "", [])) + list(_providers)
    last_err: Exception | None = None
    for p in cands:
        try:
            resp = p.client.chat.completions.create(
                model=p.model, messages=messages,
                temperature=temperature, max_tokens=max_tokens)
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            log.warning("provider %s(%s) 失败，尝试下一个: %s", p.name, p.model, e)
            last_err = e
    raise RuntimeError(f"所有 LLM provider 均不可用: {last_err}")


def extract_json(text: str):
    """从 LLM 输出中提取第一个 JSON 对象/数组；失败返回 None"""
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`")
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        start = text.find(start_ch)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == start_ch:
                depth += 1
            elif text[i] == end_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        # 尝试修复尾逗号等轻微问题
        snippet = text[start:text.rfind(end_ch) + 1]
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", snippet))
        except json.JSONDecodeError:
            continue
    return None
