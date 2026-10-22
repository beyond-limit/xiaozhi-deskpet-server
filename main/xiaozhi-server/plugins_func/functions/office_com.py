"""Word / Excel 的 COM 适配（只新建文档 → 写入 → 另存，不动用户已有文档）。

设计要点
--------
1. **能附着就不新起**：先 ``GetActiveObject``，用户已经把 Word/Excel 打开时直接用它，
   避免同机起两个实例、也避免第二个实例拿不到用户当前上下文；
2. **从不 Quit / Close**：用户已经打开的文档原样保留，我们新建的文档留在窗口里给用户看
   —— 关闭与保存的最终决定权始终在用户手上；
3. **DisplayAlerts=0**：保存同名文件时不弹「是否替换」对话框，否则语音场景会卡死；
4. **同步实现**：这里只做阻塞 COM 调用，异步入口（``desktop_save`` / ``desktop_task``）
   一律用 ``asyncio.to_thread`` 调进来，不阻塞事件循环；
5. **缺 Office 时明确说**：抛 ``OfficeUnavailable``，由上层转成
   「这台电脑没装 Word/Excel（或 COM 没注册）」这类话术，而不是笼统的“出错了”。

格式常量
--------
- Word ``SaveAs2(FileFormat=16)`` = wdFormatDocumentDefault（.docx）
- Excel ``SaveAs(FileFormat=51)`` = xlOpenXMLWorkbook（.xlsx）

PowerPoint 暂不实现（需求里后置）：真要做时同样只新建演示文稿再 SaveAs，
不要 Quit、不要关闭用户已经打开的演示文稿。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Tuple

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

WORD_PROG_ID = "Word.Application"
EXCEL_PROG_ID = "Excel.Application"

WD_FORMAT_DOCX = 16
XL_FORMAT_XLSX = 51

# Excel 单元格字符上限 32767，留点余量
EXCEL_CELL_LIMIT = 30000


class OfficeError(RuntimeError):
    """Office 调用失败（缺组件 / 目标文件被占用 / COM 报错），message 可直接念给用户。"""


class OfficeUnavailable(OfficeError):
    """本机没有可用的 Word/Excel（没装、或 COM 没注册）。"""


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )
    except Exception:
        return False


def _occupied_by(app: Any, target: str, word: bool) -> str:
    """目标文件是否已经被同一个 Office 实例里的某个文档/工作簿占着。

    这时候 **不能** SaveAs 覆盖：那会把用户正在编辑的内容顶掉，Word 也会直接拒绝
    （"名称不能与已打开文档的名称相同"）。所以先查出来，给出明确提示。
    """
    try:
        items = app.Documents if word else app.Workbooks
        count = int(items.Count)
    except Exception:
        return ""
    for index in range(1, count + 1):
        try:
            full_name = str(items(index).FullName or "")
        except Exception:
            continue
        if full_name and _same_path(full_name, target):
            return full_name
    return ""


def _client():
    try:
        import pythoncom  # noqa: F401 - 只用来确认 pywin32 装好了
        import win32com.client as win32_client
    except Exception as error:  # pragma: no cover - 依赖缺失时
        raise OfficeUnavailable(
            "没装 pywin32，我没法调用 Office；请先安装 pywin32 再试。"
        ) from error
    return win32_client


def _com_error_text(error: Any) -> str:
    text = str(error).strip()
    # com_error 的字符串很长，取前 160 字够定位
    return text[:160] if text else type(error).__name__


def _attach_or_start(client: Any, prog_id: str, label: str) -> Tuple[Any, bool]:
    """优先附着已运行的实例，返回 ``(app, attached)``。"""
    try:
        return client.GetActiveObject(prog_id), True
    except Exception:
        pass
    try:
        return client.Dispatch(prog_id), False
    except Exception as error:
        raise OfficeUnavailable(
            f"这台电脑上没有可用的 {label}（没安装，或 COM 组件没注册），"
            f"我没法用它新建文档；你可以让我改存成记事本（txt）。"
        ) from error


def _quiet(app: Any) -> None:
    """关掉弹框、让窗口可见：语音场景下任何模态对话框都会把流程卡死。"""
    for attr, value in (("DisplayAlerts", 0), ("Visible", True)):
        try:
            setattr(app, attr, value)
        except Exception:
            continue


def _split_lines(text: str):
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return lines or [""]


def _start_new(client: Any, prog_id: str, label: str) -> Any:
    """另起一个独立实例（DispatchEx）：不碰用户那个可能卡住的实例。"""
    try:
        app = client.DispatchEx(prog_id)
    except Exception as error:
        raise OfficeUnavailable(
            f"这台电脑上没有可用的 {label}（没安装，或 COM 组件没注册）。"
        ) from error
    _quiet(app)
    return app


def _with_com(prog_id: str, body: Callable[[Any], Dict[str, Any]], label: str) -> Dict[str, Any]:
    """在当前线程 CoInitialize → 附着/启动 → 执行 body → 归还 COM。"""
    client = _client()
    try:
        import pythoncom
    except Exception as error:  # pragma: no cover
        raise OfficeUnavailable("没装 pywin32，我没法调用 Office。") from error

    pythoncom.CoInitialize()
    try:
        app, attached = _attach_or_start(client, prog_id, label)
        _quiet(app)
        try:
            data = body(app)
        except OfficeError:
            raise
        except (AttributeError, TypeError) as error:
            # 已运行的实例可能正卡在模态对话框上（例如用户手动点了“另存为”），
            # 此时 Documents.Add() 会返回一个空壳代理，任何属性都取不到。
            # 这里换一个独立实例重试一次，绝不 Quit 用户原来那个。
            if not attached:
                raise OfficeError(
                    f"调用 {label} 时出错：{_com_error_text(error)}"
                ) from error
            logger.bind(tag=TAG).warning(
                f"{label} 已运行实例不可用，改用独立实例重试 | {error}"
            )
            app = _start_new(client, prog_id, label)
            try:
                data = body(app)
            except OfficeError:
                raise
            except Exception as inner:
                raise OfficeError(
                    f"调用 {label} 时出错：{_com_error_text(inner)}"
                ) from inner
            attached = False
        except Exception as error:
            raise OfficeError(
                f"调用 {label} 保存时出错：{_com_error_text(error)}"
            ) from error
        data["attached"] = attached
        return data
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _save_word(app: Any, path: str, text: str) -> Dict[str, Any]:
    occupied = _occupied_by(app, path, word=True)
    if occupied:
        raise OfficeError(
            f"“{occupied}”已经在 Word 里打开了；为了不覆盖你正在编辑的内容，我没有再保存。"
            "可以换个文件名，或先在 Word 里关掉它。"
        )
    # 只新建一个空文档，不碰用户已经打开的文档
    document = app.Documents.Add()
    selection = app.Selection
    for index, line in enumerate(_split_lines(text)):
        if index:
            selection.TypeParagraph()
        if line:
            selection.TypeText(line)
    # wdFormatDocumentDefault = 16 → .docx
    try:
        document.SaveAs2(str(path), WD_FORMAT_DOCX)
    except Exception:
        # 保存失败时别把自己的空文档留在用户窗口里
        try:
            document.Close(False)
        except Exception:
            pass
        raise
    saved = str(getattr(document, "FullName", path) or path)
    return {
        "ok": True,
        "path": saved,
        "via": "word",
        "message": f"已经在 Word 里新建并保存到 {saved}。",
    }


def _save_excel(app: Any, path: str, text: str) -> Dict[str, Any]:
    occupied = _occupied_by(app, path, word=False)
    if occupied:
        raise OfficeError(
            f"“{occupied}”已经在 Excel 里打开了；为了不覆盖你正在编辑的内容，我没有再保存。"
            "可以换个文件名，或先在 Excel 里关掉它。"
        )
    workbook = app.Workbooks.Add()
    sheet = workbook.Worksheets(1)
    lines = [line[:EXCEL_CELL_LIMIT] for line in _split_lines(text)]
    if not lines:
        lines = [""]
    sheet.Range("A1").Value = lines[0]
    if len(lines) > 1:
        # 一次性写一列，比逐格赋值快很多；Range.Value 接受二维元组
        sheet.Range("A2").Resize(len(lines) - 1, 1).Value = tuple(
            (line,) for line in lines[1:]
        )
    try:
        sheet.Columns(1).AutoFit()
    except Exception:
        pass
    # xlOpenXMLWorkbook = 51 → .xlsx
    try:
        workbook.SaveAs(str(path), XL_FORMAT_XLSX)
    except Exception:
        try:
            workbook.Close(False)
        except Exception:
            pass
        raise
    saved = str(getattr(workbook, "FullName", path) or path)
    return {
        "ok": True,
        "path": saved,
        "via": "excel",
        "message": f"已经在 Excel 里新建并保存到 {saved}。",
    }


def save_word(path: str, text: str) -> Dict[str, Any]:
    """新建 Word 文档并写入正文，另存为 docx（阻塞调用，请放进 to_thread）。"""
    return _with_com(WORD_PROG_ID, lambda app: _save_word(app, path, text), "Word")


def save_excel(path: str, text: str) -> Dict[str, Any]:
    """新建 Excel 工作簿并把每行写进 A 列，另存为 xlsx（阻塞调用，请放进 to_thread）。"""
    return _with_com(EXCEL_PROG_ID, lambda app: _save_excel(app, path, text), "Excel")


def availability() -> Dict[str, bool]:
    """探活：Word / Excel 的 COM 是否能附着或启动（给自检/设置界面用）。

    只做 ``Dispatch`` 探测，**不 Quit**：如果用户正好开着 Word，Quit 会连他的文档一起关掉。
    """
    result = {"word": False, "excel": False}
    try:
        client = _client()
        import pythoncom
    except OfficeUnavailable:
        return result

    pythoncom.CoInitialize()
    try:
        for key, prog_id in (("word", WORD_PROG_ID), ("excel", EXCEL_PROG_ID)):
            try:
                label = "Word" if key == "word" else "Excel"
                app, _attached = _attach_or_start(client, prog_id, label)
                try:
                    app.DisplayAlerts = 0
                except Exception:
                    pass
                result[key] = True
            except Exception:
                result[key] = False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
    return result
