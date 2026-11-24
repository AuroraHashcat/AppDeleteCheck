# -*- coding: utf-8 -*-
"""多模态 LLM 调用的统一入口(兼容 Responses API 与 chat/completions)。

背景与原因:
  - `client.responses` 只有 openai SDK >= 1.66 才提供;
    (本项目环境是 openai 1.35.10, 直接调用会报
     `'OpenAI' object has no attribute 'responses'`)
  - 很多中转站只实现了传统 /chat/completions;
  - 所以这里做一次「能力探测 + 自动降级」: 能走 responses 就走,
    否则(或调用失败)自动改用 chat/completions, 调用方无需关心。

对外只暴露:
  call_llm(client, model, system, user_text, img_url=None, style="responses")
  extract_json(text)
"""
import json
import re

# 只提示一次, 避免每一步都刷屏
_warned = set()

# ---------------- token 用量统计 ----------------
# 每次 LLM 调用都会累加到 _usage, 由调用方 reset_usage() 分阶段清零。
_usage = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "missing_usage": 0,   # 中转站没有返回 usage 的调用次数
}


def reset_usage():
    """开始统计前清零(每跑一个 app / 一个阶段调用一次)。"""
    for k in _usage:
        _usage[k] = 0


def get_usage():
    """返回当前累计用量(副本)。"""
    return dict(_usage)


def _pick_int(obj, *names):
    """从 dict 或对象里按顺序取第一个存在的整数字段, 都没有返回 None。"""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
    return None


def _record_usage(resp):
    """从响应对象里累加 token 用量。

    chat/completions 用法是 usage.prompt_tokens / completion_tokens;
    Responses API 用法是 usage.input_tokens / output_tokens。
    """
    _usage["calls"] += 1
    u = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
    if not u:
        _usage["missing_usage"] += 1
        return
    pt = _pick_int(u, "input_tokens", "prompt_tokens") or 0
    ct = _pick_int(u, "output_tokens", "completion_tokens") or 0
    total = _pick_int(u, "total_tokens")
    if total is None:
        total = pt + ct
    _usage["prompt_tokens"] += pt
    _usage["completion_tokens"] += ct
    _usage["total_tokens"] += total


def _warn_once(key, msg):
    if key not in _warned:
        _warned.add(key)
        print(msg)


def _first_json_object(text):
    """括号配对提取第一个完整 JSON 对象(兼容嵌套与字符串内大括号)。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def extract_json(text):
    """从 LLM 输出里提取并解析 JSON, 兼容 markdown 包裹 / 多对象 / 前后杂字。"""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    obj = _first_json_object(text)
    if obj is not None:
        try:
            return json.loads(obj)
        except Exception:
            pass
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    raise ValueError(f"LLM 未返回合法 JSON: {text[:200]}")


def supports_responses(client):
    """当前 SDK 是否提供 Responses API。"""
    return hasattr(client, "responses")


def _call_responses(client, model, system, user_text, img_url):
    content = [{"type": "input_text", "text": user_text}]
    if img_url:
        content.append({"type": "input_image", "image_url": img_url})
    resp = client.responses.create(
        model=model,
        instructions=system,
        input=[{"role": "user", "content": content}],
    )
    _record_usage(resp)
    text = getattr(resp, "output_text", "") or ""
    if not text:  # 兜底: 某些中转站返回的 output 结构不同
        for item in (resp.output or []):
            if getattr(item, "type", "") == "message":
                for c in (getattr(item, "content", None) or []):
                    if getattr(c, "type", "") == "output_text":
                        text += c.text
    return text


def _call_chat(client, model, system, user_text, img_url):
    content = []
    if img_url:
        content.append({"type": "image_url", "image_url": {"url": img_url}})
    content.append({"type": "text", "text": user_text})
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        temperature=0,
    )
    _record_usage(resp)
    return resp.choices[0].message.content


def call_llm(client, model, system, user_text, img_url=None, style="responses"):
    """统一调用多模态 LLM 并解析出 JSON(自动降级)。

    style="responses": 优先 Responses API, 不支持或失败时自动改用 chat/completions;
    style="chat":      直接用 chat/completions。
    """
    text = None
    if style == "responses":
        if supports_responses(client):
            try:
                text = _call_responses(client, model, system, user_text, img_url)
            except Exception as e:
                _warn_once(
                    "responses_failed",
                    f"[llm] Responses API 调用失败({type(e).__name__}: {e}); "
                    f"后续自动改用 chat/completions",
                )
                text = None
        else:
            _warn_once(
                "no_responses_attr",
                "[llm] 当前 openai SDK 不支持 Responses API"
                "(需 >=1.66), 自动改用 chat/completions",
            )
    if text is None:
        text = _call_chat(client, model, system, user_text, img_url)
    return extract_json(text)
