"""AI 作答/写作：OpenAI 兼容 + Anthropic 两种协议。

Key 只从 state.json 读，绝不写进代码。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                 "protocol": "openai"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "protocol": "openai"},
    "anthropic": {"base_url": "https://api.anthropic.com", "model": "claude-3-5-sonnet-latest",
                  "protocol": "anthropic"},
    "custom": {"base_url": "", "model": "", "protocol": "openai"},
}

DEFAULT = {
    "enabled": False,
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_key": "",
    "temperature": 0.3,
    "protocol": "openai",
    "vision": "auto",        # auto / on / off
    "extra_prompt": "",
}

# 模型不认 temperature 时，用它加进系统提示里做等价约束
STYLE_FALLBACK = (
    "注意：不要调整语气强度，保持克制、学术化的表达。"
    "避免口语化、情绪化和网络用语，结论要有依据。"
)

# 要求结构化输出时统一挂在 system 后面。措辞要硬：模型很容易先来一句
# "好的，这是答案" 再给 JSON，那层壳会让解析器找不到花括号。
STRICT_JSON = (
    "输出格式是硬性要求：你的整条回复必须且只能是一个 JSON 对象。"
    "第一个字符必须是 {，最后一个字符必须是 }。"
    "不要输出任何前言、结语、解释、Markdown 代码块、反引号或额外空白。"
    "即使不确定答案，也要按同样的 JSON 结构给出，不要改用文字说明。"
)

# 模型名里出现这些就认为能吃图
VISION_HINTS = (
    "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-5", "o3", "o4",
    "claude-3", "claude-4", "claude-sonnet", "claude-opus",
    "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl",
    "gemini", "glm-4v", "glm-4.5v", "yi-vision", "internvl", "llava",
)


class AIError(RuntimeError):
    pass


class AI:
    def __init__(self, conf: dict | None = None):
        c = dict(DEFAULT)
        c.update(conf or {})
        self.enabled = bool(c.get("enabled"))
        self.provider = c.get("provider") or "deepseek"
        preset = PRESETS.get(self.provider, PRESETS["custom"])
        self.base_url = (c.get("base_url") or preset["base_url"] or "").rstrip("/")
        self.model = c.get("model") or preset["model"]
        self.api_key = (c.get("api_key") or "").strip()
        self.protocol = c.get("protocol") or preset["protocol"]
        self.temperature = float(c.get("temperature", 0.3))
        self.vision = c.get("vision") or "auto"
        self.extra_prompt = (c.get("extra_prompt") or "").strip()

    @property
    def supports_vision(self) -> bool:
        v = str(self.vision or "auto").lower()
        if v in ("on", "true", "1", "yes"):
            return True
        if v in ("off", "false", "0", "no"):
            return False
        m = (self.model or "").lower()
        return any(k in m for k in VISION_HINTS)

    # ------------------------------------------------------------- 基础
    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url and self.model)

    def _post(self, path: str, body: dict, headers: dict, timeout: int,
              _retry_no_temp: list | None = None, _system: str = "",
              _timeout: int | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            # 模型不认 temperature：去掉重试，并在系统提示里补风格约束
            if _retry_no_temp is not None and "temperature" in detail.lower():
                body2 = dict(body)
                body2.pop("temperature", None)
                body2["messages"] = _retry_no_temp
                if _system:
                    first = body2["messages"][0]
                    if first.get("role") == "system":
                        body2["messages"] = [
                            {"role": "system",
                             "content": str(first.get("content")) + "\n" + STYLE_FALLBACK},
                            *body2["messages"][1:],
                        ]
                data2 = json.dumps(body2, ensure_ascii=False).encode("utf-8")
                req2 = urllib.request.Request(
                    url, data=data2, method="POST",
                    headers={"Content-Type": "application/json", **headers})
                with urllib.request.urlopen(req2, timeout=_timeout or timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            raise AIError(f"HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise AIError(f"连不上 {url}: {e.reason}") from e
        except Exception as e:
            raise AIError(str(e)) from e

    def chat(self, system: str, user: str, max_tokens: int = 900,
             timeout: int = 90, images: list[str] | None = None,
             temperature: float | None = None, json_mode: bool = False) -> str:
        """images 是 PNG 的 base64 字符串列表（不带 data: 前缀）。

        temperature 传了就用它，否则用配置值。模型不接受这个参数时，
        自动去掉重试，并往系统提示里补一句风格约束来等价控制。
        json_mode=True 时追加 STRICT_JSON，并在 OpenAI 协议下带
        response_format，让服务端强制出 JSON。
        """
        if not self.usable:
            raise AIError("AI 未启用或缺少 API Key")
        images = [b for b in (images or []) if b]
        if images and not self.supports_vision:
            raise AIError(f"模型 {self.model} 不支持看图")

        temp = self.temperature if temperature is None else float(temperature)
        system_use = system
        if json_mode:
            system_use = f"{system}\n\n{STRICT_JSON}"
        if self.extra_prompt:
            system_use = f"{system_use}\n\n{self.extra_prompt}"

        if self.protocol == "anthropic":
            content: list[dict] = [{"type": "text", "text": user}]
            for b64 in images:
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                })
            body = {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temp,
                "system": system_use,
                "messages": [{"role": "user", "content": content}],
            }
            js = self._post("/v1/messages", body,
                            {"x-api-key": self.api_key,
                             "anthropic-version": "2023-06-01"}, timeout)
            blocks = js.get("content") or []
            return "".join(b.get("text", "") for b in blocks if isinstance(b, dict)).strip()

        if images:
            content = [{"type": "text", "text": user}]
            for b64 in images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
            messages = [
                {"role": "system", "content": system_use},
                {"role": "user", "content": content},
            ]
        else:
            messages = [
                {"role": "system", "content": system_use},
                {"role": "user", "content": user},
            ]

        body = {
            "model": self.model,
            "temperature": temp,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        js = self._post("/chat/completions", body,
                        {"Authorization": f"Bearer {self.api_key}"}, timeout,
                        _retry_no_temp=messages, _system=system_use, _timeout=timeout)
        try:
            return str(js["choices"][0]["message"]["content"]).strip()
        except Exception as e:
            raise AIError(f"返回格式异常: {str(js)[:200]}") from e

    def _get(self, path: str, headers: dict, timeout: int = 30) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise AIError(f"HTTP {e.code} {detail}") from e
        except Exception as e:
            raise AIError(f"请求失败：{e}") from e

    def list_models(self, timeout: int = 30) -> list[str]:
        """按公开接口拉可用模型。OpenAI 协议 /models，Anthropic /v1/models。"""
        if not self.api_key:
            raise AIError("还没填 API Key")
        if self.protocol == "anthropic":
            js = self._get("/v1/models", {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }, timeout)
            rows = js.get("data") or []
            return [str(m.get("id")) for m in rows if m.get("id")]

        js = self._get("/models", {"Authorization": f"Bearer {self.api_key}"}, timeout)
        rows = js.get("data") or js.get("models") or []
        out = []
        for m in rows:
            mid = m.get("id") or m.get("name") if isinstance(m, dict) else m
            if mid:
                out.append(str(mid))
        return out

    def ping(self, timeout: int = 30) -> str:
        return self.chat("你是一个测试助手。", "只回复两个字：可用", max_tokens=16, timeout=timeout)


# ---------------------------------------------------------------- 任务封装

def parse_json_loose(raw: str, keys: tuple[str, ...] = ("answer",)) -> dict | None:
    """尽量从任意回复里挖出一个 JSON 对象。

    模型偶尔会先寒暄一句再给 JSON，或者干脆只给一段话。这里按
    从严格到宽松的顺序试，最后再从纯文本里抠 key: value。
    """
    if not raw:
        return None
    text = raw.strip()

    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    # 1) 整段就是一个对象
    for cand in (text, re.sub(r"[\r\n]+", " ", text)):
        try:
            js = json.loads(cand)
            if isinstance(js, dict):
                return js
        except Exception:
            pass

    # 2) 抠最外层花括号（贪心，取最长的匹配）
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        body = m.group(0)
        for cand in (body,
                     re.sub(r"[\r\n]+", " ", body),
                     re.sub(r",\s*([}\]])", r"\1", body),
                     re.sub(r"[\r\n]+", " ", re.sub(r",\s*([}\]])", r"\1", body))):
            try:
                js = json.loads(cand)
                if isinstance(js, dict):
                    return js
            except Exception:
                continue

    # 3) 从纯文本里抠 "answer": "..." 或「答案：A」这种
    out: dict = {}
    aliases = {
        "answer": ("answer", "答案", "正确选项", "正确答", "选项", "选择"),
        "reason": ("reason", "理由", "解析", "分析"),
    }
    for k in keys:
        names = aliases.get(k, (k,))
        for nm in names:
            mm = re.search(rf'{nm}\s*[:：]?\s*([^\n，。；]{{1,200}})', text, re.I)
            if mm:
                v = mm.group(1).strip().strip('"\'.,;:，。；')
                if v:
                    out[k] = v
                    break
    if out:
        rm = re.search(r'(?:reason|理由|解析|分析)\s*[:：]\s*(.{0,300})', text, re.I)
        if rm and "reason" not in out:
            out["reason"] = rm.group(1).strip()
        return out
    return None


DISC_SYSTEM = (
    "你是一名大学课堂上的学生。你正在雨课堂的课程讨论区发言。"
    "你的发言要具体、有个人思考、贴合该节课内容，不要空话套话，不要复述标题。"
    "只输出发言正文，不要加引号、不要加解释、不要markdown。"
)


def gen_discussion(ai: AI, course: str, unit: str, outline: str, lo: int = 40, hi: int = 90) -> str:
    """按课程内容现写一条讨论发言。"""
    src = (outline or "").strip()[:1200]
    user = (
        f"课程：《{course or '本课程'}》\n"
        f"当前学习单元：{unit or '本节课'}\n"
        + (f"本节课讲稿/要点：\n{src}\n" if src else "")
        + f"\n请写一条 {lo}-{hi} 字的课堂讨论发言。"
    )
    text = ai.chat(DISC_SYSTEM, user, max_tokens=400)
    text = text.strip().strip('"').strip("“”").replace("\n", " ")
    if len(text) > hi * 2:
        text = text[: hi * 2]
    return text


EXAM_SYSTEM = (
    "你是答题助手。根据题目和选项给出答案。"
    "单选题输出一个选项字母；多选题输出多个字母按字母序用逗号分隔；"
    "判断/填空/简答直接输出答案本身。只输出答案，不要解释。"
)


def answer_choice(ai: AI, body: str, options: list[dict], qtype: str) -> str:
    """给客观题选答案，返回如 'A' 或 'A,C'。"""
    lines = []
    for o in options:
        key = o.get("key") or "?"
        val = o.get("value") or ""
        lines.append(f"{key}. {val}")
    user = (
        f"题型：{qtype}\n题干：{body}\n选项：\n" + "\n".join(lines)
        + "\n请给出答案。"
    )
    return ai.chat(EXAM_SYSTEM, user, max_tokens=200).strip()
