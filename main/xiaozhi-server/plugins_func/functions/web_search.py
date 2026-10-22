import asyncio
import time

import httpx
from urllib.parse import quote
from config.logger import setup_logging
from plugins_func.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_DEFAULT_DESCRIPTION = (
    "联网搜索工具。当用户明确需要联网搜索问题时使用此工具。"
)

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题",
                }
            },
            "required": ["query"],
        },
    },
}


# ── 天气类查询的兜底：补设备所在地 + 补时间词 ────────────────────────────────
# 2026-10-16 背景：按用户要求去掉了 get_weather（和风 Key 401），天气改由本工具承接。
# 但实测"今天天气怎么样"时，模型把 query 只填成"天气"，搜到的是天气网站首页，
# 拿不到具体气温（PROGRESS 第 32 节）。所以在插件侧兜底两件事：
#   1) 没点名地点 → 拼上设备所在地（"广州市 今天 天气"）
#      地点与提示词 [上下文信息] 的设备所在地同源（prompt_manager 写的 LOCATION 缓存，
#      未命中再按 IP 查一次），保证"播报里说的城市"和"搜索用的城市"一致；
#   2) 没有时间词 → 补"今天"（离线实测："广州 天气"的摘要只有网站导航，
#      "广州 今天 天气"才会带出"气温 23到28℃"这类可直接播报的信息）。
_WEATHER_WORDS = ("天气", "气温", "温度", "下雨", "降雨", "下雪", "带伞")

# 时间词：判定"问的是什么时候"，也算"没点名地点"时要剔除的词
_WEATHER_TIME_WORDS = (
    "今天", "明天", "后天", "大后天", "现在", "目前", "此刻",
    "本周", "这周", "这个星期", "这星期", "周末", "今后", "未来", "最近",
    "早上", "上午", "中午", "下午", "晚上", "夜里", "今晚", "明早", "白天",
)
_WEATHER_NOISE_CHARS = "的地得吗呢吧啊呀么了会不有多能要怎么样如何？?，,。.、：: \t\r\n"


def _mentions_place(query: str) -> bool:
    """判断 query 里是否已经点名了城市/地点。

    判据：把天气词、时间词、常见虚词去掉后，还剩 ≥2 个字符就认为用户自己说了地点
    （"北京 今天天气" → 剩 "北京"；"今天天气" → 剩空 → 没点名）。
    只是启发式：宁可漏补（少带城市）也不要给用户的明确城市硬塞一个错误城市。
    """
    text = query
    for word in _WEATHER_WORDS + _WEATHER_TIME_WORDS:
        text = text.replace(word, " ")
    for ch in _WEATHER_NOISE_CHARS:
        text = text.replace(ch, " ")
    return len("".join(text.split())) >= 2


def _device_location(conn: "ConnectionHandler") -> str:
    """取设备所在地（优先用 prompt_manager 写好的缓存，未命中再按 IP 查一次）。"""
    client_ip = (getattr(conn, "client_ip", "") or "").strip()
    try:
        from core.utils.cache.manager import cache_manager, CacheType

        cached = cache_manager.get(CacheType.LOCATION, client_ip)
        if cached:
            return str(cached)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读设备所在地缓存失败: {e}")

    try:
        from core.utils.util import get_ip_info

        info = get_ip_info(client_ip, logger) or {}
        return str(info.get("city") or "")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"按 IP 取设备所在地失败: {e}")
        return ""


def _augment_weather_query(conn: "ConnectionHandler", query: str) -> str:
    """天气类查询：补时间词（默认"今天"）、没点名地点时补设备所在地。"""
    if not query or not any(word in query for word in _WEATHER_WORDS):
        return query

    core = query.strip()
    time_word = "" if any(word in core for word in _WEATHER_TIME_WORDS) else "今天"

    if _mentions_place(core):
        # 用户自己说了城市：只在末尾补时间词（"北京天气" → "北京天气 今天"）
        new_query = f"{core} {time_word}".strip()
    else:
        # 没点名城市：城市 + 时间词 + 原话（"天气" → "广州市 今天 天气"）
        location = _device_location(conn)
        new_query = " ".join(
            part for part in (location, time_word, core) if part
        ).strip()

    new_query = " ".join(new_query.split())
    if new_query != query:
        logger.bind(tag=TAG).info(f"天气类查询已改写 | {query} → {new_query}")
    return new_query


async def _search_metaso(api_key: str, query: str, max_results: int) -> str:
    """调用秘塔搜索API"""
    url = "https://metaso.cn/api/v1/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "size": max_results,
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    logger.bind(tag=TAG).debug(f"秘塔搜索请求 | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"秘塔搜索响应 | status: {response.status_code}")

    # 秘塔在 Key 无效/额度用尽等情况下仍返回 HTTP 200，
    # 仅在 body 中用 errCode/errMsg 表达错误（例如 errCode=2005 "API密钥无效"）。
    # 若不检查这里，会退化成"未找到相关搜索结果"，让使用者误以为是"搜不到"而非"Key 配置错误"。
    err_code = data.get("errCode")
    if err_code not in (None, 0, 200):
        err_msg = data.get("errMsg") or "未知错误"
        logger.bind(tag=TAG).error(
            f"秘塔搜索失败 | errCode={err_code} | errMsg={err_msg}"
        )
        return f"联网搜索失败：{err_msg}（请检查 web_search 的 api_key 配置）"

    webpages = data.get("webpages", [])
    if not webpages:
        logger.bind(tag=TAG).warning(f"秘塔搜索返回 0 条结果 | query={query}")
        return "未找到相关搜索结果。"

    lines = ["【联网搜索结果】"]
    for i, item in enumerate(webpages, 1):
        title = item.get("title", "无标题")
        snippet = item.get("summary", "")
        date = item.get("date", "")
        lines.append(f"{i}. 标题：{title}")
        if date:
            lines.append(f"   日期：{date}")
        if snippet:
            lines.append(f"   摘要：{snippet}")

    return "\n".join(lines)


async def _search_tavily(api_key: str, query: str, max_results: int) -> str:
    """调用Tavily搜索API"""
    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": "advanced",
    }
    logger.bind(tag=TAG).debug(f"Tavily搜索请求 | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Tavily搜索响应 | status: {response.status_code} | data: {data}")

    # Tavily 在 Key 无效时返回非 200，且错误信息在 detail 字段中。
    # 不检查这里同样会退化成"未找到相关搜索结果"，掩盖真实原因。
    if response.status_code != 200:
        detail = data.get("detail") if isinstance(data, dict) else data
        logger.bind(tag=TAG).error(
            f"Tavily搜索失败 | status: {response.status_code} | detail: {detail}"
        )
        return f"联网搜索失败：HTTP {response.status_code}（请检查 web_search 的 api_key 配置）"

    results = data.get("results", [])
    if not results:
        logger.bind(tag=TAG).warning(f"Tavily 返回 0 条结果 | query={query}")
        return "未找到相关搜索结果。"

    answer = data.get("answer", "")
    lines = [f"【联网搜索结果】\n总结：{answer}"]
    # for i, item in enumerate(results, 1):
    #     title = item.get("title", "无标题")
    #     summary = item.get("content", "")
    #     lines.append(f"{i}. 标题：{title}")
    #     if summary:
    #         lines.append(f"   摘要：{summary}")

    return "\n".join(lines)


async def _search_serply(api_key: str, query: str, max_results: int) -> str:
    """调用Serply搜索API"""
    url = "https://api.serply.io/v1/search"
    headers = {
        "X-Api-Key": api_key,
        "User-Agent": "xiaozhi-esp32-server",
    }
    # Serply单页最多返回10条，且返回条数可能略多于num，需要本地再截断一次
    params = {"q": query, "num": min(max_results, 10)}
    logger.bind(tag=TAG).debug(f"Serply搜索请求 | URL: {url} | params: {params}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.get(url, params=params, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Serply搜索响应 | status: {response.status_code}")

    if response.status_code != 200:
        detail = data.get("detail", "") if isinstance(data, dict) else ""
        logger.bind(tag=TAG).error(
            f"Serply搜索失败 | status: {response.status_code} | detail: {detail}"
        )
        return "联网搜索请求失败，请检查API Key是否正确。"

    results = data.get("results", [])
    if not results:
        return "未找到相关搜索结果。"

    lines = ["【联网搜索结果】"]
    for i, item in enumerate(results[:max_results], 1):
        title = item.get("title", "无标题")
        snippet = item.get("description", "")
        lines.append(f"{i}. 标题：{title}")
        if snippet:
            lines.append(f"   摘要：{snippet}")

    return "\n".join(lines)


# 免 Key 搜索引擎对连续请求敏感：短时间内多发几次，会返回被降级的通用结果页
# （实测 Bing 连续 7 次请求后，多个不同查询都返回同一批不相关结果；
#   冷却后单发同一查询又能拿到准确结果）。
# 因此做一层节流：串行化 + 两次真实请求之间至少间隔 _FREE_MIN_INTERVAL 秒。
_FREE_MIN_INTERVAL = 4.0
_free_lock = asyncio.Lock()
_last_free_call = 0.0

# 免 Key 搜索引擎抓取需要浏览器 UA，否则可能被拒绝或返回简化页面
_FREE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 免 Key 引擎的尝试顺序：先用实测最稳定的国内引擎，Bing 放最后
_FREE_ENGINE_ORDER = ("sogou", "so360", "baidu", "bing")


def _clean_text(node) -> str:
    """提取文本并规整空白（各免 Key 搜索引擎共用）。

    搜索引擎会把命中的关键词包在 <strong> 之类的标签里，直接用
    get_text(strip=True) 会把标签两侧的单词粘连
    （例如 "Welcome to<strong>Python</strong>.org" 变成 "Welcome toPython.org"）。
    用空格作为分隔符再折叠多余空白即可。
    """
    import re

    text = node.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


async def _search_bing_free(query: str, max_results: int) -> str:
    """免 Key 搜索源：抓取 Bing 搜索结果页（cn.bing.com 国内可达）

    背景：metaso / tavily / serply 三个源都需要各自的 API Key。
    在没有 Key 的场景下，本函数提供一个零配置兜底。

    取舍与已知局限（务必知悉）：
      - 依赖 Bing 的 HTML 结构，页面改版即可能解析失败（届时改用带 Key 的源即可）
      - 属于网页抓取，请求频率不宜过高；单次只取前 max_results 条
      - 实测本机环境：cn.bing.com 可达（HTTP 200，含 li.b_algo），
        DuckDuckGo 与公共 SearXNG 实例均连接超时，不可用
      - 解析使用 bs4（项目已有依赖，见 requirements.txt 的 bs4==0.0.2）
    """
    from bs4 import BeautifulSoup

    url = f"https://cn.bing.com/search?q={quote(query)}&setlang=zh-CN"
    response = await _fetch_free(url)

    if response.status_code != 200:
        logger.bind(tag=TAG).error(f"Bing 免Key搜索失败 | status: {response.status_code}")
        return f"联网搜索失败：HTTP {response.status_code}（Bing 免 Key 搜索源）"

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select("li.b_algo")
    if not items:
        logger.bind(tag=TAG).warning(f"Bing 未解析到结果，可能页面结构已变更 | query={query}")
        return "未找到相关搜索结果。"

    lines = ["【联网搜索结果】"]
    count = 0
    for item in items:
        h2 = item.select_one("h2 a") or item.select_one("h2")
        if not h2:
            continue
        title = _clean_text(h2)
        if not title:
            continue
        href = h2.get("href", "")

        # Bing 的摘要容器类名有多个变体，逐个兜底尝试
        snippet = ""
        for sel in (
            "div.b_caption p",
            "p.b_lineclamp2",
            "p.b_lineclamp3",
            "p.b_lineclamp4",
            "div.b_caption",
            "p",
        ):
            node = item.select_one(sel)
            if node:
                candidate = _clean_text(node)
                if candidate:
                    snippet = candidate
                    break

        count += 1
        lines.append(f"{count}. 标题：{title}")
        if href:
            lines.append(f"   链接：{href}")
        if snippet:
            lines.append(f"   摘要：{snippet}")
        if count >= max_results:
            break

    if count == 0:
        return "未找到相关搜索结果。"

    logger.bind(tag=TAG).info(f"Bing 免Key搜索成功 | 返回 {count} 条")
    return "\n".join(lines)


async def _fetch_free(url: str):
    """免 Key 引擎共用的抓取：串行化 + 最小间隔节流，降低被反爬降级的概率"""
    global _last_free_call
    async with _free_lock:
        gap = _FREE_MIN_INTERVAL - (time.monotonic() - _last_free_call)
        if gap > 0:
            logger.bind(tag=TAG).info(f"免Key搜索触发节流，等待 {gap:.1f}s")
            await asyncio.sleep(gap)
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
            response = await client.get(url, headers=_FREE_HEADERS, follow_redirects=True)
        _last_free_call = time.monotonic()
        return response


def _query_keywords(query: str):
    """提取用于「结果是否相关」校验的片段：ASCII 词（>=3 字符）+ 中文二字片段

    用途是识别搜索引擎返回的"降级通用页"：这类页面标题与查询毫无关系。
    实测 Bing 被限流时，查询"小智AI 是什么项目"会返回
    "小红书 / 汉语汉字小 / 小游戏"等与查询无关的结果。
    """
    import re

    kws = set()
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-]*", query or ""):
        if len(w) >= 3:
            kws.add(w.lower())
    cjk = re.sub(r"[^\u4e00-\u9fff]", " ", query or "")
    for seg in cjk.split():
        for i in range(len(seg) - 1):
            kws.add(seg[i : i + 2])
    return kws


def _looks_relevant(results, query: str) -> bool:
    """结果中是否出现查询关键词片段（命中任意一个即判定相关）

    results 可以是 list[dict]（标准化结果）或 str（已格式化文本）。
    """
    if not results:
        return False
    kws = _query_keywords(query)
    if not kws:
        return True
    if isinstance(results, str):
        blob = results.lower()
    else:
        blob = " ".join(
            f"{r.get('title', '')} {r.get('url', '')} {r.get('snippet', '')}"
            for r in results
        ).lower()
    return any(k in blob for k in kws)


def _parse_sogou(html: str):
    """搜狗结果页：div.vrwrap / div.rb 下的 h3 为标题"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("div.vrwrap, div.rb"):
        a = item.select_one("h3 a") or item.select_one("h3")
        if not a:
            continue
        title = _clean_text(a)
        if not title:
            continue
        snippet = ""
        for sel in ("div.fz-mid", "div.str_info", "div.text-layout", "p"):
            node = item.select_one(sel)
            if node:
                cand = _clean_text(node)
                if cand:
                    snippet = cand
                    break
        out.append({"title": title, "url": a.get("href", ""), "snippet": snippet})
    return out


def _parse_so360(html: str):
    """360 结果页：li.res-list 下的 h3 为标题"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("li.res-list"):
        a = item.select_one("h3 a") or item.select_one("h3")
        if not a:
            continue
        title = _clean_text(a)
        if not title:
            continue
        snippet = ""
        for sel in ("p.res-desc", "div.res-rich", "span.res-rich", "p"):
            node = item.select_one(sel)
            if node:
                cand = _clean_text(node)
                if cand:
                    snippet = cand
                    break
        out.append({"title": title, "url": a.get("href", ""), "snippet": snippet})
    return out


def _parse_baidu(html: str):
    """百度结果页：div.result / div.c-container 下的 h3 为标题"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("div.result, div.c-container"):
        a = item.select_one("h3 a") or item.select_one("h3")
        if not a:
            continue
        title = _clean_text(a)
        if not title:
            continue
        snippet = ""
        for sel in ("div.c-abstract", "span.content-right_8Zs40", "div.c-span-last"):
            node = item.select_one(sel)
            if node:
                cand = _clean_text(node)
                if cand and cand != title:
                    snippet = cand
                    break
        out.append({"title": title, "url": a.get("href", ""), "snippet": snippet})
    return out


_FREE_ENGINES = {
    "sogou": ("搜狗", "https://www.sogou.com/web?query={q}", _parse_sogou),
    "so360": ("360", "https://www.so.com/s?q={q}", _parse_so360),
    "baidu": ("百度", "https://www.baidu.com/s?wd={q}&ie=utf-8", _parse_baidu),
}


async def _search_free_multi(query: str, max_results: int) -> str:
    """免 Key 搜索：按 _FREE_ENGINE_ORDER 依次尝试，用相关性校验剔除降级页

    为什么需要多引擎：实测单引擎都不够稳 —— Bing 在连续请求或 IP 级检测下
    会返回与查询无关的通用结果页（"小红书/汉语汉字小/小游戏"），
    而搜狗/360/百度连续多轮实测结果一致且相关。
    策略：逐个引擎尝试，一旦结果通过相关性校验就立即返回；全部失败才报错。
    """
    tried = []
    for key in _FREE_ENGINE_ORDER:
        # Bing 复用既有实现（它直接返回格式化文本）
        if key == "bing":
            text = await _search_bing_free(query, max_results)
            if _looks_relevant(text, query):
                logger.bind(tag=TAG).info(f"免Key搜索[Bing]命中 | query={query}")
                return text
            logger.bind(tag=TAG).warning("免Key搜索[Bing]结果与查询不相关（疑似降级页）")
            tried.append("Bing: 结果不相关")
            continue

        name, url_tpl, parser = _FREE_ENGINES[key]
        url = url_tpl.format(q=quote(query))
        try:
            response = await _fetch_free(url)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"免Key搜索[{name}]请求异常: {e}")
            tried.append(f"{name}: 请求异常")
            continue

        if response.status_code != 200:
            logger.bind(tag=TAG).warning(
                f"免Key搜索[{name}]失败 | status={response.status_code}"
            )
            tried.append(f"{name}: HTTP {response.status_code}")
            continue

        try:
            results = parser(response.text)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"免Key搜索[{name}]解析异常: {e}")
            tried.append(f"{name}: 解析异常")
            continue

        if not _looks_relevant(results, query):
            logger.bind(tag=TAG).warning(
                f"免Key搜索[{name}]结果与查询不相关（疑似降级页），换下一个引擎 | query={query}"
            )
            tried.append(f"{name}: 结果不相关")
            continue

        results = results[:max_results]
        logger.bind(tag=TAG).info(f"免Key搜索[{name}]命中 {len(results)} 条 | query={query}")
        lines = ["【联网搜索结果】"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. 标题：{r['title']}")
            if r["url"]:
                lines.append(f"   链接：{r['url']}")
            if r["snippet"]:
                lines.append(f"   摘要：{r['snippet']}")
        return "\n".join(lines)

    logger.bind(tag=TAG).error(f"免Key搜索全部引擎均失败 | 尝试记录: {tried}")
    return (
        "联网搜索暂时不可用（所有免 Key 搜索源均未返回有效结果），请稍后再试，"
        "或配置带 Key 的搜索源（tavily / metaso / serply）。"
    )


@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    logger.bind(tag=TAG).info(f"web_search 被调用 | query={query}")
    if not query:
        return ActionResponse(Action.REQLLM, "请提供搜索关键词。", None)

    # 天气类问题兜底：模型经常只填"天气"，这里补上设备所在地（详见上方注释）
    query = _augment_weather_query(conn, query)

    web_search_config = conn.config.get("plugins", {}).get("web_search", {})
    provider = web_search_config.get("provider", "").lower()
    max_results = int(web_search_config.get("max_results", 3))
    logger.bind(tag=TAG).info(f"web_search 配置 | provider={provider} | max_results={max_results} | config_keys={list(web_search_config.keys())}")

    try:
        # free 是免 Key 多引擎搜索源（搜狗/360/百度/Bing 依次尝试），无需校验 api_key；
        # 其余源（metaso / tavily / serply）必须提供各自的 API Key。
        if provider in ("free", "bing_free", "bing"):
            result_text = await _search_free_multi(query, max_results)
        else:
            api_key = web_search_config.get("api_key", "")
            if not api_key:
                return ActionResponse(
                    Action.REQLLM,
                    "联网搜索功能未配置API Key，请在配置文件中填写。",
                    None,
                )
            if provider == "metaso":
                result_text = await _search_metaso(api_key, query, max_results)
            elif provider == "tavily":
                result_text = await _search_tavily(api_key, query, max_results)
            elif provider == "serply":
                result_text = await _search_serply(api_key, query, max_results)
            else:
                return ActionResponse(
                    Action.REQLLM,
                    f"联网搜索功能未配置或配置的搜索源无效（当前：{provider}），"
                    f"可选值：free（免Key多引擎）/ metaso / tavily / serply，请检查配置。",
                    None,
                )
        logger.bind(tag=TAG).info(f"搜索结果组装完成:\n{result_text}")
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("联网搜索请求超时")
        result_text = "联网搜索请求超时，请稍后重试。"
    except httpx.HTTPStatusError as e:
        logger.bind(tag=TAG).error(f"联网搜索请求失败: {e}")
        result_text = "联网搜索请求失败，请稍后重试。"
    except Exception as e:
        logger.bind(tag=TAG).error(f"联网搜索异常: {e}")
        result_text = "联网搜索出现异常，请稍后重试。"

    return ActionResponse(Action.REQLLM, result_text, None)
