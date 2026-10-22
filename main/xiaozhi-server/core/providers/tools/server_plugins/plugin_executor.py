"""服务端插件工具执行器"""

import asyncio
import copy
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import all_function_registry, module_func_map, Action, ActionResponse


class ServerPluginExecutor(ToolExecutor):
    """服务端插件工具执行器"""

    def __init__(self, conn: "ConnectionHandler"):
        self.conn = conn
        self.config = conn.config

    async def execute(
        self, conn: "ConnectionHandler", tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行服务端插件工具"""
        func_item = all_function_registry.get(tool_name)
        if not func_item:
            return ActionResponse(
                action=Action.NOTFOUND, response=f"插件函数 {tool_name} 不存在"
            )

        try:
            # 根据工具类型决定如何调用
            if hasattr(func_item, "type"):
                func_type = func_item.type
                if func_type.code in [4, 5]:  # SYSTEM_CTL, IOT_CTL (需要conn参数)
                    result = func_item.func(conn, **arguments)
                elif func_type.code == 2:  # WAIT
                    result = func_item.func(**arguments)
                elif func_type.code == 3:  # CHANGE_SYS_PROMPT
                    result = func_item.func(conn, **arguments)
                else:
                    result = func_item.func(**arguments)
            else:
                # 默认不传conn参数
                result = func_item.func(**arguments)

            # 兼容 async def 工具函数
            if asyncio.iscoroutine(result):
                result = await result

            return result

        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                response=str(e),
            )

    def _expand_plugin_names(self, config_functions):
        """将模块级别的插件名展开为具体函数名。

        在一个 function 文件中可能注册多个 @register_function，
        配置中如果使用的是模块名（文件名），需要展开为具体的函数名列表。
        """
        if not isinstance(config_functions, list):
            try:
                config_functions = list(config_functions)
            except TypeError:
                return []

        expanded = []
        for name in config_functions:
            if name in module_func_map:
                # 模块名，展开为该模块下所有注册函数名
                expanded.extend(module_func_map[name])
            elif name in all_function_registry:
                # 精确匹配函数名，直接保留
                expanded.append(name)
            else:
                # 未知名称，保留原值（可能是 MCP 或其他工具）
                expanded.append(name)
        return expanded

    def _get_plugin_description(self, func_name):
        """获取插件函数的描述，优先精确匹配函数名，其次匹配模块名。"""
        plugins = self.config.get("plugins", {})
        # 精确匹配函数名
        if func_name in plugins:
            return plugins[func_name].get("description", "")
        # 通过 module_func_map 反向查找模块名
        for module_name, func_names in module_func_map.items():
            if func_name in func_names and module_name in plugins:
                return plugins[module_name].get("description", "")
        return ""

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有注册的服务端插件工具"""
        tools = {}

        # 获取必要的函数
        necessary_functions = ["handle_exit_intent", "get_lunar"]

        # 获取配置中的函数
        config_functions = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ].get("functions", [])

        # 将模块级别的插件名展开为具体函数名（兜底机制）
        config_functions = self._expand_plugin_names(config_functions)

        # 合并所有需要的函数
        all_required_functions = list(set(necessary_functions + config_functions))

        for func_name in all_required_functions:
            func_item = all_function_registry.get(func_name)
            if func_item:
                description = copy.deepcopy(func_item.description)
                # 从函数注册中获取描述（支持模块名和函数名的双层查找）
                fun_description = self._get_plugin_description(func_name)
                if fun_description is not None and len(fun_description) > 0:
                    if "function" in description and isinstance(
                        description["function"], dict
                    ):
                        description["function"]["description"] = fun_description

                # 新闻插件：根据配置更新新闻源参数描述
                if func_name == "get_news_from_newsnow":
                    self._init_news_source_description(description, func_name)

                # 本机软件调用插件：把配置里的软件清单写进参数描述，
                # 让大模型知道"能打开哪些软件"，避免它自己编软件名
                if func_name in (
                    "launch_local_app",
                    "close_local_app",
                    "app_act",
                    "app_task",
                    "desktop_task",
                ):
                    self._init_local_app_description(description, func_name)

                # 宏优先：把 app_macros / desktop_task.macros 里登记过的宏名与场景
                # 写进工具描述，模型才知道"该先用哪个宏"，而不是每次临时想 steps
                if func_name in ("app_task", "desktop_task"):
                    self._init_macro_description(description, func_name)

                # 文件查找插件：把"允许查找的目录/是否整机"写进 scope 描述，
                # 避免大模型把范围外的路径填进来（会被拒绝）
                if func_name == "search_files":
                    self._init_file_search_description(description)

                tools[func_name] = ToolDefinition(
                    name=func_name,
                    description=description,
                    tool_type=ToolType.SERVER_PLUGIN,
                )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的服务端插件工具"""
        return tool_name in all_function_registry

    def _init_local_app_description(self, description, func_name):
        """把最新软件目录写进 app_name 描述，限制大模型只操作白名单。"""
        from plugins_func.functions.local_app_catalog import get_effective_apps

        local_config = self.config.get("plugins", {}).get("local_app", {}) or {}
        apps = [app for app in get_effective_apps(local_config) if isinstance(app, dict)]
        names = [str(app["name"]) for app in apps if app.get("name")]
        if len(names) > 80:
            shown = names[:80]
            app_hint = (
                f"要操作的软件名称。常用清单：{'、'.join(shown)}。"
                f"本次共登记{len(names)}个软件；用户明确点名其他软件时按原话填写，服务端会校验白名单。"
            )
        elif names:
            app_hint = f"要操作的软件名称，只允许填：{'、'.join(names)}"
        else:
            app_hint = "当前没有可操作的软件；请让用户先明确要求扫描，或在软件设置中手动添加。"

        # 用户在软件管理里关掉了"允许语音关闭"的软件：提前告知，免得模型反复尝试
        if func_name == "close_local_app" and names:
            blocked = [
                str(app["name"])
                for app in apps
                if app.get("name") and str(app.get("close") or "").lower() == "off"
            ]
            if blocked:
                app_hint += (
                    f"。其中以下软件已被用户设置为不允许语音关闭，调用会被直接拒绝，"
                    f"请如实告知用户「已设置为不允许语音关闭」，不要假装关掉了：{'、'.join(blocked[:30])}"
                )
        try:
            description["function"]["parameters"]["properties"]["app_name"][
                "description"
            ] = app_hint
        except (KeyError, TypeError):
            pass

    def _init_macro_description(self, description, func_name):
        """把登记过的宏（名字 + when 场景）写进工具描述，落实"宏优先、steps 兜底"。"""
        plugins = self.config.get("plugins", {}) or {}
        lines = []

        def _when_text(spec):
            if not isinstance(spec, dict):
                return ""
            raw = spec.get("when")
            if isinstance(raw, (list, tuple)):
                return "、".join(str(item) for item in raw if str(item).strip())
            return str(raw or "").strip()

        if func_name == "app_task":
            table = (plugins.get("local_app", {}) or {}).get("app_macros") or {}
            for app_name, macros in table.items():
                if not isinstance(macros, dict):
                    continue
                for macro_name, spec in macros.items():
                    when_text = _when_text(spec)
                    lines.append(
                        f"{app_name}→{macro_name}"
                        + (f"（{when_text}）" if when_text else "")
                    )
        else:
            table = (plugins.get("desktop_task", {}) or {}).get("macros") or {}
            for macro_name, spec in table.items():
                when_text = _when_text(spec)
                lines.append(
                    f"{macro_name}" + (f"（{when_text}）" if when_text else "")
                )

        if not lines:
            return
        try:
            description["function"]["description"] = (
                str(description["function"].get("description") or "")
                + "\n已登记的场景宏（优先把 macro 填成这些名字）："
                + "；".join(lines[:20])
            )
        except (KeyError, TypeError):
            pass

    def _init_file_search_description(self, description):
        """把"当前允许查找的目录"写进 search_files 的 scope 描述。

        允许的目录来自 `plugins.file_search`（roots / extra_dirs / whole_pc），
        所以在描述里如实列出，能显著减少"把范围外路径填进 scope 被拒"的情况。
        """
        from plugins_func.functions.file_search import (
            _resolve_roots,
            _scope_hint,
            _whole_pc,
        )
        from plugins_func.functions.file_search_catalog import get_effective_config

        file_config = get_effective_config(
            self.config.get("plugins", {}).get("file_search", {}) or {}
        )
        # max_labels=0：给大模型的清单要列全（提示语那边才会省略）
        hint = _scope_hint(_resolve_roots(file_config), max_labels=0)
        try:
            depth = int(file_config.get("max_depth", 6) or 0)
        except (TypeError, ValueError):
            depth = 6
        depth_hint = (
            "只查所选目录的第一层（不进子文件夹）" if depth <= 0 else f"最多往下查 {depth} 层子文件夹"
        )
        try:
            if _whole_pc(file_config):
                value = (
                    f"可选。当前已开启整台电脑查找：{hint}都可以，"
                    "也可以填某个盘符（如“C盘”）或盘内目录的绝对路径；留空表示全部允许目录。"
                    f"当前{depth_hint}。"
                )
            else:
                value = (
                    f"可选。当前允许查找的目录：{hint}（也可以是它们的子目录）。"
                    "留空表示在全部允许目录里找。"
                    f"当前{depth_hint}。"
                    "用户明确说了在别的目录/盘里找（例如“K盘 xiaozhi 文件夹下”）时，"
                    "照原话填进 scope：服务端会检查范围——不在范围内会明确说“这个目录不能查”，"
                    "这比留空后返回“没找到”要诚实得多，不要为了避免被拒而留空。"
                )
            description["function"]["parameters"]["properties"]["scope"][
                "description"
            ] = value
        except (KeyError, TypeError):
            pass

    def _init_news_source_description(self, description, func_name):
        """根据连接配置初始化新闻工具的参数描述"""
        news_sources = (
            self.config.get("plugins", {})
            .get(func_name, {})
            .get("news_sources", "")
        )
        if not news_sources:
            news_sources = "澎湃新闻;百度热搜;财联社"
        sources_str = news_sources.replace(";", "、")
        try:
            description["function"]["parameters"]["properties"]["source"][
                "description"
            ] = f"新闻源的标准中文名称，例如{sources_str}等。可选参数，如果不提供则使用默认新闻源"
        except (KeyError, TypeError):
            pass
