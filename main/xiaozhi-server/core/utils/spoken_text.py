"""把"给机器看的内容"从"该说给用户听的话"里摘出去。

为什么需要这个模块：TTS 队列里的文本有两个去处 —— 合成语音，以及原样下发
给客户端当字幕（桌宠气泡显示的就是它）。模型偶尔会把工具调用、JSON 参数、
执行步骤（``1. click -> 完成``）或文件完整路径写进正文；这些内容一旦进队列，
既会被念出来，也会显示在屏幕气泡里，用户看到的就是"一对话就冒出一段代码"。

这里只做保守过滤：命中"明显是机器文本"的片段就丢掉，正常中文口语原样保留。
``strip_spoken_text`` 带代码块状态，适合按句喂（Markdown 的 ``` 可能跨句）；
``sanitize_spoken_text`` 是无状态整段版本，适合工具返回值、字幕兜底。
"""

import re

_FENCE = "```"

# 标签 / DSML / 特殊 token（模型把工具调用写成文本时的残留）
_TAG_RE = re.compile(r"</?\s*[A-Za-z][\w:.-]*(?:\s[^<>\n]{0,200})?/?>")
_DSML_LOOSE_RE = re.compile(r"[<｜|]{1,3}\s*\|*\s*DSML[^>\n]{0,200}>?")
# 全角竖线 U+FF5C 也在里面：DeepSeek 的标记常写成 <｜｜...｜｜>
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|\n]{0,40}\|>")

# 行内代码：里面的东西是标识符就整段删，是人话（如 `稻香`）就留下内容
_INLINE_CODE_RE = re.compile(r"`([^`\n]{0,160})`")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][\w.:\-/]{0,80}")

# 单行 JSON / 数组；带 "key": 的才认定是 JSON，否则可能是正常方括号
_JSON_INLINE_RE = re.compile(r"\{[^{}\n]{0,1500}\}|\[[^\[\]\n]{0,1500}\]")
_JSON_KEY_HINT_RE = re.compile(r"\"\s*[\w\u4e00-\u9fff-]{1,40}\s*\"\s*[:：]")

# "1. click -> 完成" 这类执行步骤
_TRACE_LINE_RE = re.compile(
    r"(?m)^\s*\d{1,2}\s*[.、)]\s*[A-Za-z_][\w.]{0,30}\s*(?:->|→|=>|：|:).*$"
)

# 整行就是一次函数调用：search_files(scope="桌面")
_FUNC_CALL_LINE_RE = re.compile(r"(?m)^\s*[A-Za-z_][\w.]{0,40}\s*\([^\n]{0,800}\)\s*$")

# "调用工具：…" / "tool_calls: …"
_TOOL_NOISE_LINE_RE = re.compile(
    r"(?m)^\s*(?:调用工具|工具调用|调用函数|tool_calls?|function_call|arguments?|parameters?|invoke|request)\s*[:：].*$",
    re.I,
)

# 单独一行的段标题（"执行记录："），正文可能是人话，标题本身没信息
_SECTION_TITLE_RE = re.compile(
    r"(?m)^\s*(?:执行记录|执行步骤|已完成|当前窗口(?:/页面)?|失败原因|trace|steps?)\s*[:：]\s*$",
    re.I,
)

_URL_RE = re.compile(r"https?://[^\s，。；;、）)\]】\"']+")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s，。；;、）)\]】\"']{1,400}")

_MD_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_MD_QUOTE_RE = re.compile(r"(?m)^\s{0,3}>+\s*")
_MD_LIST_RE = re.compile(r"(?m)^\s{0,3}(?:[*+-]|•)\s+")
_MD_EMPHASIS_RE = re.compile(r"(\*\*|__)(.*?)\1")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]{1,80})\*(?!\*)")

_MULTI_PUNCT_RE = re.compile(r"([，,；;、])\s*(?=[，,；;、])")
_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")
_LINE_TAIL_RE = re.compile(r"(?m)[ \t]+$")

_CODE_SYMBOLS = set("{}[]()<>;:=_\"'`/\\|")

_PATH_SPLIT_RE = re.compile(r"[\\/]")


def _basename(path: str) -> str:
    parts = [p for p in _PATH_SPLIT_RE.split(path.strip()) if p]
    return parts[-1] if parts else ""


def _looks_like_code(text: str) -> bool:
    """整段没有中文、符号密度又很高 —— 基本就是代码/JSON 碎片。"""
    stripped = text.strip()
    if not stripped:
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in stripped):
        return False
    symbols = sum(1 for ch in stripped if ch in _CODE_SYMBOLS)
    return symbols >= 3 and symbols >= len(stripped) * 0.2


def _drop_multiline_json(text: str) -> str:
    """删掉没被 ``` 包起来的多行 JSON 块（模型常直接吐参数对象）。"""
    if "{" not in text and "[" not in text:
        return text
    out = []
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if depth > 0:
            depth += stripped.count("{") + stripped.count("[")
            depth -= stripped.count("}") + stripped.count("]")
            if depth < 0:
                depth = 0
            continue
        opens = stripped.count("{") + stripped.count("[")
        closes = stripped.count("}") + stripped.count("]")
        if opens > closes and stripped[:1] in ("{", "["):
            depth = opens - closes
            continue
        out.append(line)
    return "\n".join(out)


def _strip_inline_code(text: str) -> str:
    def repl(match):
        inner = (match.group(1) or "").strip()
        # `play_song` 这种是工具名，整段删；`稻香` 是人话，只脱掉反引号
        if _IDENTIFIER_RE.fullmatch(inner):
            return ""
        return inner

    return _INLINE_CODE_RE.sub(repl, text)


def _strip_json_inline(text: str) -> str:
    def repl(match):
        body = match.group(0)
        if _JSON_KEY_HINT_RE.search(body):
            return ""
        return body

    # 嵌套 JSON 一次替换不干净，多跑几轮直到稳定
    for _ in range(5):
        new_text = _JSON_INLINE_RE.sub(repl, text)
        if new_text == text:
            break
        text = new_text
    return text


def _strip_paths(text: str) -> str:
    def repl(match):
        name = _basename(match.group(0))
        return f" {name} " if name else ""

    # 完整路径念出来又长又难懂，只留文件名
    return _WIN_PATH_RE.sub(repl, text)


def _tidy(text: str) -> str:
    text = _MULTI_PUNCT_RE.sub(r"\1", text)
    # 中文之间的空格是排版噪音（英文单词之间的空格要保留）
    text = re.sub(
        r"(?<=[\u4e00-\u9fff，。；、：）])[ \t\u3000]+(?=[\u4e00-\u9fff，。；、：（])",
        "",
        text,
    )
    text = _SPACE_RE.sub(" ", text)
    text = _LINE_TAIL_RE.sub("", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = text.strip()
    text = re.sub(r"^[，,；;、：:\-—\s]+", "", text)
    text = re.sub(r"[，,；;、：:\s]+$", "", text)
    return text


def strip_spoken_text(
    text: str, in_code_fence: bool = False, keep_code_like: bool = False
) -> tuple:
    """净化一句/一段待播报文本。

    Args:
        text: 待净化文本（可以是一句，也可以是整段）。
        in_code_fence: 上一次调用是否停在未闭合的 ``` 代码块里。
        keep_code_like: 暂不丢弃"看着像代码"的残句（流式场景交给
            :class:`SpokenTextFilter` 先判 JSON，避免只留下半个括号）。

    Returns:
        (净化后的文本, 是否仍在代码块里)。整段都是机器文本时第一项为空串。
    """
    if not text:
        return "", in_code_fence
    if not isinstance(text, str):
        text = str(text)
    had_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in text)

    # 上一句停在代码块里：先找收尾的 ```
    if in_code_fence:
        end = text.find(_FENCE)
        if end < 0:
            return "", True
        text = text[end + len(_FENCE) :]
        in_code_fence = False

    # 成对的代码块整段删掉（含语言标记）；落单的开头表示后文都还在块内
    while True:
        start = text.find(_FENCE)
        if start < 0:
            break
        end = text.find(_FENCE, start + len(_FENCE))
        if end < 0:
            text = text[:start]
            in_code_fence = True
            break
        text = text[:start] + text[end + len(_FENCE) :]

    if not text.strip():
        return "", in_code_fence

    # 工具调用痕迹 / 标记 / 标签
    text = _DSML_LOOSE_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _SPECIAL_TOKEN_RE.sub("", text)
    # 整行都是机器信息的行（"调用工具：…"、trace、段标题）
    text = _TOOL_NOISE_LINE_RE.sub("", text)
    text = _SECTION_TITLE_RE.sub("", text)
    text = _TRACE_LINE_RE.sub("", text)
    text = _FUNC_CALL_LINE_RE.sub("", text)

    # 参数对象：多行 JSON 块 + 单行 JSON
    if not keep_code_like:
        text = _drop_multiline_json(text)
    text = _strip_json_inline(text)
    text = _strip_inline_code(text)
    text = _URL_RE.sub("", text)
    text = _strip_paths(text)

    # Markdown 残留
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_QUOTE_RE.sub("", text)
    text = _MD_LIST_RE.sub("", text)
    text = _MD_EMPHASIS_RE.sub(r"\2", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)

    text = _tidy(text)

    # 整段没中文、只剩符号/英文：是代码残渣，不念也不上屏
    if (
        not keep_code_like
        and text
        and _looks_like_code(text)
        and not had_cjk
    ):
        return "", in_code_fence
    return text, in_code_fence


def sanitize_spoken_text(text: str, fallback: str = "") -> str:
    """整段净化（无状态）。返回空串表示这段内容不该播报/显示。"""
    cleaned, _ = strip_spoken_text(text or "", False)
    return cleaned or fallback


# JSON 起点：{ 后面紧跟 "key": 或又一个 {（数组同理交给 _scan_json_end 判深度）
_JSON_START_RE = re.compile(r"[\{\[]\s*(?:\"[^\"\n]{1,60}\"\s*[:：]|\{)")


def _scan_json_end(text: str, start: int, depth: int) -> tuple:
    """从 start 扫括号，返回 (JSON 结束位置, 剩余深度)；没闭合时位置为 -1。"""
    for index in range(start, len(text)):
        ch = text[index]
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth <= 0:
                return index + 1, 0
    return -1, max(depth, 0)


class SpokenTextFilter:
    """带状态的播报过滤器：代码块与 JSON 都可能跨句，状态要留在实例上。

    TTS 是按句（甚至半个 JSON 片段）调用净化的，单看一句识别不出"这是 JSON"；
    这里记住括号深度，第一片 `{"a": 3.5, ` 就把状态打开，后面的片段整段丢弃，
    直到配平为止 —— 否则模型直接吐 JSON 时会把半截代码念出来。
    """

    def __init__(self):
        self.in_code_fence = False
        self.json_depth = 0

    def reset(self):
        self.in_code_fence = False
        self.json_depth = 0

    def _strip_json_state(self, text: str) -> str:
        if not text:
            return ""
        if self.json_depth > 0:
            end, self.json_depth = _scan_json_end(text, 0, self.json_depth)
            if end < 0:
                return ""
            text = text[end:]

        while True:
            match = _JSON_START_RE.search(text)
            if not match:
                break
            start = match.start()
            end, depth = _scan_json_end(text, start, 0)
            if end < 0:
                # 这一句里没闭合：记下深度，后续片段继续吞
                self.json_depth = depth
                return text[:start]
            text = text[:start] + text[end:]

        # 只剩半个括号也是残渣
        if text.strip() in ("{", "}", "[", "]", "{}", "[]"):
            return ""
        return text

    def feed(self, text: str) -> str:
        """喂一句/一段待播报文本，返回可以念、可以上屏的部分（可能为空串）。"""
        cleaned, self.in_code_fence = strip_spoken_text(
            text, self.in_code_fence, keep_code_like=True
        )
        cleaned = self._strip_json_state(cleaned)
        if cleaned and _looks_like_code(cleaned) and not any(
            "\u4e00" <= ch <= "\u9fff" for ch in cleaned
        ):
            return ""
        return cleaned
