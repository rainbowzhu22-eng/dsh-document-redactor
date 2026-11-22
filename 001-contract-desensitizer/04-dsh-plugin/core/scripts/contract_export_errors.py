"""Structured failures shared by export, restore and download entry points."""
import errno


ROUTES = (
    ("/api/export-plan/", "导出前检查"),
    ("/api/apply/", "生成脱敏文书"),
    ("/api/pdf/export/", "生成脱敏 PDF"),
    ("/api/xlsx/export/", "生成脱敏 Excel"),
    ("/api/restore/", "还原文书"),
    ("/download/", "下载文件"),
    ("/history/download/", "下载文件"),
)
RECOVERY = {
    "RULES_CHANGED": "回到上传页重新上传原件，按当前规则复核后再导出。",
    "EXPORT_SOURCE_CHANGED": "确认原件版本，从首页重新上传并复核。",
    "EXPORT_SELECTION_STALE": "重新核对当前勾选和导出清单，确认后再导出。",
    "EXPORT_NO_REPLACEMENTS_ACK_REQUIRED": "返回复核页选中需处理的位置。确需零替换导出时，先核对提示并明确确认。",
    "DOCX_REVISION_BLOCKED": "在 Word 副本中核对相关修订，完成必要调整后重新上传。",
    "DOCX_REVISION_ACK_REQUIRED": "查看修订清单并确认相应处置。",
    "DOCX_REVISION_ACK_STALE": "重新打开复核页，核对更新后的修订清单。",
}
EXACT = {
    "session not found": ("EXPORT_SESSION_EXPIRED", "当前复核项目已过期。", "回到上传页重新上传并复核；已生成的文件可从历史记录查看。"),
    "invalid json": ("EXPORT_REQUEST_INVALID", "本次操作的请求格式无效。", "刷新当前复核页，重新核对选择后再试。"),
    "类型或位置选择无效": ("EXPORT_SELECTION_INVALID", "本次导出的类型或位置选择无法读取。", "刷新复核页，重新勾选要脱敏的位置。"),
    "零替换导出必须明确确认": ("EXPORT_NO_REPLACEMENTS_ACK_REQUIRED", "当前没有选中要替换的位置。", RECOVERY["EXPORT_NO_REPLACEMENTS_ACK_REQUIRED"]),
    "外部链接清单已变化，请重新检查": ("DOCX_LINK_LIST_STALE", "Word 外部链接清单已变化。", "刷新复核页，重新核对外部链接的保留或移除选项，再导出。"),
    "请求体超过总大小上限": ("EXPORT_REQUEST_TOO_LARGE", "提交的数据超过本机服务可处理的大小。", "检查选用的配套文件，改用对应的较小文件或缩小本次处理范围后重试。"),
    "请求体读取不完整或超时": ("EXPORT_REQUEST_INTERRUPTED", "本机服务未能完整接收本次请求。", "确认文书脱敏工作台仍在运行，重新提交本次操作。"),
    "project access denied": ("EXPORT_LOCAL_ACCESS_REQUIRED", "当前页面无法操作本机项目。", "返回 DSH，重新打开文书脱敏工作台后再试。"),
    "invalid request origin": ("EXPORT_LOCAL_ACCESS_REQUIRED", "当前页面无法访问本机脱敏服务。", "从 DSH 的文书脱敏入口重新打开复核页。"),
    "invalid local host": ("EXPORT_LOCAL_ACCESS_REQUIRED", "当前地址无法访问本机脱敏服务。", "从 DSH 的文书脱敏入口重新打开该项目。"),
    "bad download path": ("EXPORT_DOWNLOAD_LINK_INVALID", "当前下载地址无效。", "从历史项目或导出结果重新点击下载。"),
    "file not found": ("EXPORT_DOWNLOAD_MISSING", "本机找不到这份下载文件。", "从历史项目查看已有产物；若文件已移走或删除，请重新导出。"),
}


class ExportFailure(ValueError):
    def __init__(self, code, message, advice, *, stage=None, location=None):
        super().__init__(message)
        self.code = code
        self.advice = advice
        self.stage = stage
        self.location = location

    def payload(self):
        result = {"ok": False, "error": str(self), "code": self.code, "advice": self.advice}
        if self.stage:
            result["stage"] = self.stage
        if self.location:
            result["location"] = self.location
        return result


def file_failure(exc, *, saving=False, stage=None):
    if saving:
        if exc.errno == errno.ENOSPC:
            reason, advice = "可用空间不足，文件未能写完。", "释放保存位置的空间后再导出。"
        elif isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
            reason, advice = "保存位置无写入权限。", "确认文书脱敏工作台可写入本机项目目录，再重新导出。"
        else:
            reason, advice = "本机保存文件时出现写入错误。", "检查项目目录是否可访问，或文件是否正在被占用，再重试。"
        return ExportFailure("EXPORT_WRITE_FAILED", reason, advice, stage=stage or "保存文件")
    if isinstance(exc, FileNotFoundError):
        reason, advice = "找不到本次导出所需的原件或项目数据。", "在本机项目中重新关联原件，或回到首页重新上传复核。"
    elif isinstance(exc, PermissionError):
        reason, advice = "本机服务无权读取所需文件。", "检查原件和项目目录的访问权限，随后重新打开该文书。"
    else:
        reason, advice = "本机读取项目数据时出现错误。", "检查原件是否可打开、是否已移动，随后重新上传复核。"
    return ExportFailure("EXPORT_READ_FAILED", reason, advice, stage=stage or "读取项目")


def with_export_context(path, response, status):
    if not isinstance(response, dict) or response.get("ok") is not False or status < 400:
        return response
    path = path.split("?", 1)[0]
    stage = next((label for prefix, label in ROUTES if path.startswith(prefix)), None)
    if stage is None:
        return response
    result = dict(response)
    message = result.get("error") or "本次操作未完成。"
    if message in EXACT:
        code, message, advice = EXACT[message]
        result.update(error=message, code=result.get("code") or code)
        result.setdefault("advice", advice)
    result.setdefault("stage", stage)
    if not result.get("code"):
        result["code"] = "EXPORT_REQUEST_FAILED"
    result.setdefault("error", message)
    if not result.get("advice"):
        if result["code"] in RECOVERY:
            result["advice"] = RECOVERY[result["code"]]
        elif stage == "下载文件":
            result["advice"] = "从导出结果或历史项目重新下载；若产物已不存在，请重新导出。"
        elif stage == "还原文书":
            result["advice"] = "核对脱敏稿与 JSON 映射是否配对，重新选择文件后再试。"
        elif status >= 500:
            result["advice"] = "核对原件可打开且本机项目目录可写。重试仍失败时，保留原件并反馈当前提示和错误码。"
        else:
            result["advice"] = "按提示核对本机项目的文件状态和当前选择后再试。"
    return result
