"""Declarative layout for the four TUI project-settings pages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FieldSpec:
    code: str
    label: str
    kind: str = "boolean"
    minimum: int = -(2**31)
    maximum: int = 2**31 - 1
    choices: tuple[tuple[str, str], ...] = ()
    wide: bool = False


@dataclass(frozen=True, slots=True)
class GroupSpec:
    title: str
    fields: tuple[FieldSpec, ...]


@dataclass(frozen=True, slots=True)
class PageSpec:
    id: str
    title: str
    groups: tuple[GroupSpec, ...]
    restart_warning: bool = False

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(field.code for group in self.groups for field in group.fields)


LANGUAGE_CHOICES = (
    ("日语", "JAPANESE"),
    ("韩语", "KOREAN"),
    ("简体中文", "CHINESE_HANS"),
    ("繁体中文", "CHINESE_HANT"),
)
WARNING_CHOICES = (
    ("忽略", "IGNORE"),
    ("延后显示", "LATER"),
    ("每文件一次", "ONCE"),
    ("立即显示", "DISPLAY"),
)
CHARACTER_WIDTH_CHOICES = (
    ("自动", "AUTOMATIC"),
    ("模糊字符按窄字符", "AMBIGUOUS_NARROW"),
    ("模糊字符按宽字符", "AMBIGUOUS_WIDE"),
)

PAGES = (
    PageSpec(
        "interface",
        "界面与交互",
        (
            GroupSpec(
                "导航与输入",
                (
                    FieldSpec("UseMouse", "使用鼠标"),
                    FieldSpec(
                        "AllowLongInputByMouse",
                        "允许用鼠标向 ONEINPUT 输入多个字符",
                        wide=True,
                    ),
                    FieldSpec("Ctrl_Z_Enabled", "启用 Ctrl+Z 输入撤销", wide=True),
                ),
            ),
            GroupSpec(
                "历史与缓冲",
                (FieldSpec("MaxLog", "历史日志行数", "integer", 500),),
            ),
            GroupSpec(
                "文本显示",
                (
                    FieldSpec(
                        "ReplaceFullWidthSpaces",
                        "以两个半角空格替代全角空格",
                        wide=True,
                    ),
                    FieldSpec(
                        "CharacterWidthMode",
                        "字符列宽计算模式",
                        "select",
                        choices=CHARACTER_WIDTH_CHOICES,
                        wide=True,
                    ),
                ),
            ),
            GroupSpec(
                "PRINTC 与换行",
                (
                    FieldSpec("ButtonWrap", "防止按钮中途换行"),
                    FieldSpec(
                        "CompatiLinefeedAs1739",
                        "重现 1739 版以前的非按钮换行",
                        wide=True,
                    ),
                    FieldSpec("PrintCPerLine", "每行 PRINTC 项数", "integer", 1),
                    FieldSpec("PrintCLength", "PRINTC 项字符数", "integer", 1),
                ),
            ),
            GroupSpec(
                "颜色",
                (
                    FieldSpec("ForeColor", "默认文字颜色", "color", wide=True),
                    FieldSpec("BackColor", "默认背景颜色", "color", wide=True),
                    FieldSpec("FocusColor", "选中项文字颜色", "color", wide=True),
                ),
            ),
        ),
    ),
    PageSpec(
        "project",
        "项目与数据",
        (
            GroupSpec(
                "项目文件发现",
                (
                    FieldSpec("UseRenameFile", "使用 _Rename.csv"),
                    FieldSpec("UseReplaceFile", "使用 _Replace.csv"),
                    FieldSpec("SearchSubdirectory", "搜索子目录"),
                    FieldSpec("SortWithFilename", "按文件名排序加载顺序"),
                ),
            ),
            GroupSpec(
                "角色 CSV",
                (
                    FieldSpec("CompatiCALLNAME", "CALLNAME 为空时使用 NAME"),
                    FieldSpec("CompatiSPChara", "使用 SP 角色"),
                ),
            ),
            GroupSpec(
                "ERD 扩展定义",
                (
                    FieldSpec("UseERD", "使用 ERD 功能"),
                    FieldSpec(
                        "VarsizeDimConfig",
                        "VARSIZE 维度指定与 ERD 保持一致",
                        wide=True,
                    ),
                ),
            ),
            GroupSpec(
                "文本解析与传统编码",
                (
                    FieldSpec("SystemAllowFullSpace", "将全角空格视为空白"),
                    FieldSpec(
                        "useLanguage",
                        "内部使用的东亚语言",
                        "select",
                        choices=LANGUAGE_CHOICES,
                        wide=True,
                    ),
                    FieldSpec(
                        "ReplaceContinuationBR",
                        "行连接时的换行替换文本",
                        "text",
                        wide=True,
                    ),
                ),
            ),
        ),
        True,
    ),
    PageSpec(
        "script",
        "脚本与诊断",
        (
            GroupSpec(
                "名称与可达性",
                (
                    FieldSpec("IgnoreCase", "忽略标识符大小写差异"),
                    FieldSpec("IgnoreUncalledFunction", "忽略未被调用的函数"),
                ),
            ),
            GroupSpec(
                "函数覆盖",
                (
                    FieldSpec("AllowFunctionOverloading", "允许覆盖系统函数"),
                    FieldSpec("WarnFunctionOverloading", "系统函数被覆盖时显示警告"),
                ),
            ),
            GroupSpec(
                "诊断策略",
                (
                    FieldSpec("DisplayWarningLevel", "显示的最低警告级别", "integer", 0, 255),
                    FieldSpec(
                        "FunctionNotFoundWarning",
                        "找不到函数时的警告处理",
                        "select",
                        choices=WARNING_CHOICES,
                        wide=True,
                    ),
                    FieldSpec(
                        "FunctionNotCalledWarning",
                        "函数未被调用时的警告处理",
                        "select",
                        choices=WARNING_CHOICES,
                        wide=True,
                    ),
                ),
            ),
            GroupSpec(
                "函数调用兼容性",
                (
                    FieldSpec("CompatiCallEvent", "允许 CALL 事件函数"),
                    FieldSpec("CompatiFuncArgOptional", "允许省略用户函数的所有参数"),
                    FieldSpec(
                        "CompatiFuncArgAutoConvert",
                        "为用户函数参数自动补充 TOSTR",
                        wide=True,
                    ),
                ),
            ),
            GroupSpec(
                "FORM 解析兼容性",
                (FieldSpec("SystemIgnoreTripleSymbol", "不展开 FORM 中的三连符号"),),
            ),
        ),
        True,
    ),
    PageSpec(
        "save",
        "存档与配置",
        (
            GroupSpec(
                "自动保存与槽位",
                (
                    FieldSpec("AutoSave", "执行自动保存"),
                    FieldSpec("SaveDataNos", "显示的存档数量", "integer", 20, 80),
                ),
            ),
            GroupSpec(
                "传统存档格式",
                (
                    FieldSpec("SystemSaveInBinary", "使用二进制存档格式"),
                    FieldSpec("ZipSaveData", "压缩二进制存档"),
                ),
            ),
        ),
        True,
    ),
)

FIELDS = {field.code: field for page in PAGES for group in page.groups for field in group.fields}
