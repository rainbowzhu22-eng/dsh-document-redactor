#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 contract_sensitive_detector.py
 合同敏感信息「候选项」识别器：首次安装依赖需要联网，安装完成后文书处理不联网
================================================================================

定位
------
本文件是「合同脱敏方案」的第 1 阶段：**识别**（Detection）。
它只负责把 Word / PDF 合同里的敏感信息"找出来、定位好、打上分"，
**不负责**改写原文档（改写是第 2 阶段 Apply 的职责）。

输入：.docx / .pdf（带文本层）/ .txt / .md
输出：JSON 清单、CSV 清单、标注文本、人工复核 HTML、脱敏预览文本

离线保证
--------
* 不调用任何大模型、云端 API、网络服务、第三方识别接口；
* 仅依赖本地规则：正则 + 上下文关键词 + 国标校验位算法（GB 11643 / GB 32100 / Luhn）；
* 默认开启 `--enforce-offline`（可用 `--allow-network` 关闭），该开关会直接
  禁用 socket，任何联网尝试都会立即抛错，从机制上杜绝"偷偷外传"。

识别思路（四层漏斗，逐层收窄）
------------------------------
  L0 解析层  : docx 保留段落/表格/页眉页脚归属；pdf 保留页码 + 词级坐标（供后续涂黑）
  L1 归一化层: 全角→半角、去零宽字符，**保留 norm→raw 的下标映射**，定位不错位
  L2 召回层  : 正则 / 上下文规则产出候选（宁可多召回，允许一定误报）
  L3 判别层  : 校验位验真、占位符剔除、白名单剔除、上下文加权、跨类型重叠消解、置信度重排

依赖
----
    pip install python-docx pdfplumber pypdf
（均为纯本地解析库；pdfplumber 缺失时自动降级到 pypdf，仅丢失 PDF 坐标）

用法
----
    python contract_sensitive_detector.py 合同.docx
    python contract_sensitive_detector.py *.pdf --out-dir reports --format json,html
    python contract_sensitive_detector.py c.docx --min-confidence 0.7 --min-severity medium
    python contract_sensitive_detector.py c.docx --allowlist allowlist.txt --config cfg.json
    python contract_sensitive_detector.py --selftest

作者：合同脱敏方案 / v1.0
================================================================================
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import html
import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

__version__ = "1.3.0-dev"
ENGINE_NAME = "offline-rule-detector"

# ==============================================================================
# 0. 元数据类型定义
# ==============================================================================

SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}


@dataclass(frozen=True)
class TypeMeta:
    """某一类敏感信息的元信息与默认脱敏策略。"""

    type_id: str
    label: str            # 中文名
    category: str         # 归类
    severity: str         # high / medium / low
    keep_head: int = 0    # 脱敏时保留前 N 位
    keep_tail: int = 0    # 脱敏时保留后 N 位
    mask_char: str = "*"
    replacement: Optional[str] = None  # 非空则整体替换为该串
    mask_fn: Optional[Callable[[str], str]] = None
    priority: int = 0     # 重叠消解优先级，越大越优先
    enabled: bool = True  # 是否默认启用（可在规则清单 Markdown 中调整）


def _mask_email(v: str) -> str:
    if "@" not in v:
        return v[0] + "*" * max(len(v) - 1, 1)
    local, domain = v.rsplit("@", 1)
    keep = local[0] if local else ""
    return f"{keep}{'*' * max(len(local) - 1, 3)}@{domain}"


# 常见复姓（脱敏姓名时保留 2 字）
COMPOUND_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于",
    "司空", "闾丘", "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里",
    "呼延", "东郭", "南门", "羊舌", "微生", "梁丘", "左丘", "西门", "第五",
}

# 常见单姓（用于姓名候选项的姓氏校验，覆盖《百家姓》前 300 余位）
SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章"
    "云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常"
    "乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞"
    "熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁"
    "荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫"
    "宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙"
    "池乔阴鬱胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农"
    "温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东"
    # 补充：常见异体/简写姓（姓氏表偏窄，导致含少见姓氏的姓名整条漏检）。
    # 「付/代」为高频普通字（付款/代理），本轮不纳入，避免扩大误报面。
    "闫肖兰覃冼邝"
    "欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红"
    "游竺权逯盖益桓公岳帅缑亢况后有琴梁丘左丘东门商牟佘佴伯赏南宫墨哈谯笪年爱阳佟言福"
)

# 姓名右边界：命中这些字/标点才认为姓名到此为止，避免把「张三丰有限公司」切成「张三是本」
NAME_BOUNDARY = set("，。；、：？！（）()[]「」『』《》〈〉“”‘’ \t\r\n·-—_/\\\u3000")
NAME_TAIL_WORDS = ("先生", "女士", "小姐", "同志", "老师", "教授", "医生", "律师", "的", "等", "与", "和", "及")

# 组织机构名：强后缀（可独立判定）/ 弱后缀（需更高门槛）
ORG_SUFFIX_STRONG = (
    "股份有限公司", "有限责任公司", "有限公司", "集团有限公司", "（有限合伙）", "(有限合伙)",
    "有限合伙", "股份合作公司", "集团公司", "总公司", "分公司", "子公司",
    "律师事务所", "会计师事务所", "税务师事务所", "研究院", "设计院", "勘察院",
    "大学", "学院", "医院", "银行", "信用社", "合作社", "基金会", "商会", "行业协会",
    "支行", "分行", "营业部",
)
# 合同里高频出现、但不带「公司」字样的主体后缀：
#   「大厦」——「上海某某大厦ABCD项目」这类写法；
#   「个体工商户」——个体户没有「有限公司」后缀；
#   「经营部/商行/门市部/服务中心/工作站/分店/专卖店」——同属无「公司」后缀的经营主体。
# 这些仍放在**弱后缀**里（置信度低于法定后缀），靠门槛把误报挡在外面。
# 注意：刻意不收「酒店/宾馆/超市/商场」这类通用名词——「甲方安排酒店住宿」会被
#      向左扩张成「甲方安排酒店」并误判为机构名，收益不抵风险。
ORG_SUFFIX_WEAK = ("事务所", "中心", "研究院", "实验室", "集团", "协会", "学会", "管委会", "办事处",
                   "工作室", "大厦", "个体工商户", "经营部", "商行", "门市部", "服务中心",
                   "工作站", "分店", "专卖店")
ORG_SHORT_SUFFIXES = ("集团", "公司")
ORG_SHORT_PARTY_LABELS = (
    "甲方", "乙方", "丙方", "供方", "需方", "卖方", "买方", "受托方", "委托方",
    "开票方", "付款方", "收款方", "发包方", "承包方", "出租方", "承租方", "保证方",
)
ORG_SHORT_ACTIONS = (
    "开具发票", "签署合同", "履行合同", "提供服务", "支付款项", "收取款项", "承担责任",
)
ORG_SHORT_PARTY_LABEL_RE = re.compile(
    r"(?:" + "|".join(re.escape(label) for label in sorted(ORG_SHORT_PARTY_LABELS, key=len, reverse=True))
    + r")\s*(?::|：|为)\s*$"
)
ORG_SPAN_FIELD_LABELS = (
    "开户银行", "开户行", "收款银行", "收款行", "付款银行", "付款行",
    "汇款银行", "汇款行", "结算银行", "结算行",
)
ORG_SPAN_FIELD_LABEL_RE = re.compile(
    r"(?:" + "|".join(re.escape(label) for label in sorted(ORG_SPAN_FIELD_LABELS, key=len, reverse=True))
    + r")\s*(?:(?:[:：]\s*)|(?:为|是)\s*)$"
)
ORG_SHORT_ACTION_RE = re.compile(
    r"\s*(?:" + "|".join(re.escape(action) for action in sorted(ORG_SHORT_ACTIONS, key=len, reverse=True)) + ")"
)
EN_ORG_SUFFIXES = (
    "COMPANY LIMITED", "CO LTD", "CO LIMITED", "CO", "LIMITED", "LTD", "INC",
    "CORPORATION", "CORP", "LLC", "LLP", "LP", "L P", "PLC", "GMBH", "B V", "N V", "PTE LTD",
    "PTY LTD", "OY", "K K", "S A", "S P A", "A G", "AG", "AB", "A S", "A/S",
    "APS", "SP Z O O", "LTDA", "SARL", "SRL",
)

# 组织机构名白名单：这些"看起来像机构"的词在合同里是代词/通用表述，不该脱敏
ORG_STOPWORDS = {
    "甲方", "乙方", "丙方", "丁方", "戊方", "双方", "各方", "单方", "对方", "第三方",
    "本公司", "贵公司", "该公司", "子公司", "母公司", "总公司", "借款人", "出租人",
    "承租人", "出卖人", "买受人", "发包人", "承包人", "委托人", "受托人", "保证人",
    "债权人", "债务人", "甲方公司", "乙方公司", "开户银行", "收款银行", "付款银行",
    "开户行", "银行账号", "仲裁委员会", "人民法院",
    "对方公司", "双方公司", "各方公司", "上述公司", "下列公司", "相关公司", "一家公司", "有限公司",
}

# 机构「显式简称定义」的排除名单。
# 「（下称“附表”）」「（以下简称“初始期限”）」「（以下简称“本协议”）」定义的是
# 文件 / 条款 / 期限 / 角色，**不是公司**，不得记为紧邻机构全称的简称，
# 否则正文里这些词会被整片替换成机构占位符、条款指向被改写。
# 命中即不采纳 —— 宁可漏一个真简称（可由「字号」机制或手动补录兜底），也不把正文改错。
ORG_ALIAS_STOPWORDS = {
    # 文件 / 条款
    "附表", "附件", "附录", "清单", "目录", "正文", "序言", "前言",
    "本协议", "本合同", "本文件", "本约定", "本订单", "本单据", "补充协议", "主合同",
    "条款", "约定", "报价单", "订单", "方案", "说明书", "技术方案", "服务方案",
    # 期限
    "初始期限", "合同期限", "服务期限", "有效期", "保密期限", "通知期限", "履行期限",
    "合作期限", "质保期", "保修期", "服务期", "合同期", "合作期",
    # 角色 / 指代
    "双方", "各方", "我方", "他方", "客户", "供方", "需方", "买方", "卖方",
    "发包方", "承包方", "委托方", "受托方", "服务方", "供应商", "采购人", "中标人", "招标人",
    # 保密、数据及其他合同角色
    "披露方", "接收方", "信息披露方", "信息接收方", "信息提供方", "数据提供方", "数据接收方", "发送方",
    "签约方", "合同一方", "交易方", "合作方", "参与方", "授权方", "被授权方", "许可方", "被许可方",
    "转让方", "受让方", "投标人", "运营方", "关联方", "个人信息处理者", "数据处理者",
    "投保人", "被保险人", "保险人", "受益人",
    # 平台、商户及合作单位通称
    "平台", "合作平台", "服务平台", "交易平台", "商户", "合作商户", "入驻商户", "签约商户",
    "服务机构", "合作单位",
    "company", "agreement", "contract", "party", "buyer", "seller", "customer",
    "supplier", "client", "licensor", "licensee", "counterparty", "the counterparty",
    "disclosing party",
    "receiving party", "effective date", "services", "the company",
    # 泛化名词
    "服务", "项目", "产品", "货物", "工程", "工作", "事项", "内容",
}
ORG_ALIAS_STOP_RE = re.compile(
    r"^(?:附[表件录][一二三四五六七八九十\d]*"
    r"|第[一二三四五六七八九十百\d]+(?:条|款|章|节|部分|阶段)"
    r"|本[协合文约订单据文].{0,5}"
    r"|[甲乙丙丁戊]方"
    r"|.{0,5}(?:期限|有效期|质保期|保修期|服务期|合同期|合作期)"
    r")$"
)


def _is_nonparty_alias(alt: str) -> bool:
    """判断「显式简称定义」候选是否其实是文件/条款/期限/角色词，而非公司简称。"""
    a = (alt or "").strip()
    if not a:
        return True
    english_stopwords = {value.casefold() for value in ORG_ALIAS_STOPWORDS
                         if re.search(r"[A-Za-z]", value)}
    if a in ORG_STOPWORDS or a in ORG_ALIAS_STOPWORDS or a.casefold() in english_stopwords:
        return True
    return bool(ORG_ALIAS_STOP_RE.match(a))

# 常见占位符 / 示例值
PLACEHOLDER_PATTERNS = [
    re.compile(r"^(\d)\1{5,}$"),                      # 000000 / 11111111
    re.compile(r"^(?:1234567890|0123456789)"),         # 顺子
    re.compile(r"^[Xx×＊*·．.]{3,}$"),                  # XXX / ***
    re.compile(r"^[某甲乙丙丁]{1,3}(?:某|方|公司)*$"),   # 某某 / 甲方
]


# 服务热线（400 / 800 号段）。这类号码本身可能只由 0/8 组成（如 800 800 8888），
# 会被 is_placeholder 的「数字种类 ≤ 2」规则误杀，所以单独放行。
HOTLINE_RE = re.compile(r"^(?:400|800)[\s\-—–－]?\d{3}[\s\-—–－]?\d{4}$")


def is_placeholder(value: str) -> bool:
    v = value.strip()
    if not v:
        return True
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 6:
        # 含千分位（, / ，）的是「格式化数字」（如 500,000.00 / 60,000.00），属结构化
        # 真实数据而非占位符 —— 原判据无视这一点，会把整数金额里「数字种类 ≤ 2」的
        # （500,000.00 → 数字 50000000）整条误杀，导致金额明文残留。
        if not re.search(r"[,，]", v) and len(set(digits)) <= 2:                      # 只由 1~2 种数字组成
            return True
        if digits in ("1234567890", "0123456789", "123456789", "012345678"):
            return True
    for p in PLACEHOLDER_PATTERNS:
        if p.match(v):
            return True
    if re.fullmatch(r"[*xX×#]{4,}", v):
        return True
    return False


# ------------------------------------------------------------------------------
# 敏感信息类型注册表
# ------------------------------------------------------------------------------

def _mask_person(v: str) -> str:
    """姓名：保留姓氏（复姓保留 2 字），其余替换 **。"""
    head = 2 if v[:2] in COMPOUND_SURNAMES else 1
    return v[:head] + "*" * max(len(v) - head, 1)


def _mask_amount(v: str) -> str:
    """金额：按规则清单替换为 ¥******.**元。"""
    currency = (
        (r"US\$", "US$"), (r"USD", "USD"), (r"CA\$", "CA$"),
        (r"CAD", "CAD"), (r"EUR|€", "EUR"), (r"GBP|£", "GBP"),
        (r"HKD", "HKD"), (r"SGD", "SGD"), (r"AUD", "AUD"),
        (r"JPY", "JPY"), (r"¥", "¥"),
    )
    for pattern, label in currency:
        if re.search(pattern, v, re.I):
            return label + " ******"
    return "¥******.**元"


def _mask_hk_id(v: str) -> str:
    """香港身份证 A123456(7)：保留首字母与括号内校验位，中间 6 位掩码。"""
    m = re.match(r"^([A-Z]{1,2})(\d{6})(?:[(（](\d)[)）])?$", v.strip().upper())
    if not m:
        return v[0] + "*" * max(len(v) - 1, 1)
    tail = f"({m.group(3)})" if m.group(3) else ""
    return f"{m.group(1)}******{tail}"


def _mask_landline(v: str) -> str:
    """固定电话：保留区号与末 4 位，中间掩码。

    支持区号与号码之间、号码内部分组之间的空格/横线：
        0755-86329871  → 0755-****9871
        010-8666 2188  → 010-****2188
        010 8666 2188  → 010-****2188
        400-800-8888   → 400-***-8888
    """
    s = v.strip()
    # 服务热线：400/800 号段
    hot = re.match(r"^(400|800)\s*[-—–]?\s*(.*)$", s)
    if hot and re.sub(r"\D", "", s) and len(re.sub(r"\D", "", s)) >= 7:
        rest = re.sub(r"\D", "", hot.group(2))
        if len(rest) <= 4:
            return f"{hot.group(1)}-" + "*" * len(rest)
        return f"{hot.group(1)}-" + "*" * (len(rest) - 4) + rest[-4:]
    m = re.match(r"^\(?\s*(0\d{2,3})\s*\)?\s*[-—–]?\s*(.*)$", s)
    if not m:
        digits = re.sub(r"\D", "", s)
        if len(digits) <= 4:
            return "*" * len(digits)
        return digits[0] + "*" * max(len(digits) - 5, 1) + digits[-4:]
    area, rest = m.group(1), re.sub(r"\D", "", m.group(2))
    if len(rest) <= 4:
        return f"{area}-" + "*" * len(rest)
    return f"{area}-" + "*" * (len(rest) - 4) + rest[-4:]


_ADDR_ADMIN_RE = re.compile(
    r"^\s*[\u4e00-\u9fff]{1,8}?(?:省|自治区)?"
    r"[\u4e00-\u9fff]{1,10}?(?:市|自治州|地区|盟)"
    r"[\u4e00-\u9fff]{1,15}?(?:区|县|旗|市)"
)


def _mask_address(v: str) -> str:
    """地址：保留行政区划（省/市/区县），掩掉街道门牌。"""
    if re.search(r"[A-Za-z]", v):
        return "********"
    m = _ADDR_ADMIN_RE.match(v)
    head = m.group(0) if m else v[:6]
    rest = len(v) - len(head)
    if rest <= 0:
        return v
    return head + "*" * rest


def _mask_ipv4(v: str) -> str:
    """IP：保留第一段，其余掩码。192.*.*.*"""
    parts = v.split(".")
    if len(parts) == 4:
        return parts[0] + ".*.*.*"
    return "*" * len(v)


def _mask_mac(v: str) -> str:
    """MAC：保留前 2 位，其余掩码。AA:*:*:*:*:*"""
    parts = re.split(r"[:\-]", v)
    if len(parts) == 6:
        sep = ":" if ":" in v else "-"
        return parts[0] + sep + "*".center(2, "*") + (sep + "*".center(2, "*")) * 4
    return "*" * len(v)


def _mask_secret(_v: str) -> str:
    """密钥/凭据：完全阻断，不留任何痕迹。"""
    return "[已脱敏]"


def _mask_id_head(v: str) -> str:
    """护照：保留首字母，其余掩码。E********"""
    return v[0] + "*" * max(len(v) - 1, 1)


TYPE_META: Dict[str, TypeMeta] = {
    # ---- 身份标识 ----
    "ID_CARD": TypeMeta("ID_CARD", "身份证号", "身份标识", "high", 6, 4, priority=100),
    "ID_CARD_15": TypeMeta("ID_CARD_15", "身份证号(15位老证)", "身份标识", "high", 6, 4, priority=98),
    "PASSPORT": TypeMeta("PASSPORT", "护照号", "身份标识", "medium", 0, 0, mask_fn=_mask_id_head, priority=80),
    "HK_ID_CARD": TypeMeta("HK_ID_CARD", "香港身份证", "身份标识", "high", 0, 0, mask_fn=_mask_hk_id, priority=90),
    "HK_MO_TW_PERMIT": TypeMeta("HK_MO_TW_PERMIT", "港澳台通行证", "身份标识", "medium", 2, 2, priority=78),
    "PERSON_NAME": TypeMeta("PERSON_NAME", "自然人姓名", "身份标识", "high", 0, 0, mask_fn=_mask_person, priority=70),
    "US_SSN": TypeMeta("US_SSN", "美国社会保障号", "身份标识", "high", 2, 2, priority=97),
    "COMMENT_AUTHOR_IDENTITY": TypeMeta("COMMENT_AUTHOR_IDENTITY", "批注插入人", "文档元数据", "high", 0, 0, priority=88),
    "REVISION_AUTHOR_IDENTITY": TypeMeta("REVISION_AUTHOR_IDENTITY", "修订人", "文档元数据", "high", 0, 0, priority=89),
    # ---- 联系方式 ----
    "PHONE_MOBILE": TypeMeta("PHONE_MOBILE", "手机号码", "联系方式", "high", 3, 4, priority=95),
    "PHONE_INTERNATIONAL": TypeMeta("PHONE_INTERNATIONAL", "国际电话号码", "联系方式", "high", 0, 0, priority=91),
    "PHONE_LANDLINE": TypeMeta("PHONE_LANDLINE", "固定电话", "联系方式", "medium", 0, 0, mask_fn=_mask_landline, priority=60),
    "EMAIL": TypeMeta("EMAIL", "电子邮箱", "联系方式", "high", 0, 0, mask_fn=_mask_email, priority=92),
    "ADDRESS": TypeMeta("ADDRESS", "住址/通信地址", "联系方式", "high", 0, 0, mask_fn=_mask_address, priority=68),
    "POSTCODE": TypeMeta("POSTCODE", "邮政编码", "联系方式", "low", 0, 0, priority=40),
    "SOCIAL_ACCOUNT": TypeMeta("SOCIAL_ACCOUNT", "即时通讯账号(微信/QQ)", "联系方式", "low", 1, 2, priority=35),
    # ---- 金融账户 ----
    "BANK_CARD": TypeMeta("BANK_CARD", "银行卡号", "金融账户", "high", 4, 4, priority=96),
    "BANK_ACCOUNT": TypeMeta("BANK_ACCOUNT", "银行账号/对公账号", "金融账户", "high", 4, 4, priority=72),
    "IBAN": TypeMeta("IBAN", "国际银行账号 IBAN", "金融账户", "high", 4, 4, priority=103),
    "SWIFT": TypeMeta("SWIFT", "SWIFT/BIC代码", "金融账户", "high", 0, 0, priority=79),
    "ABA_ROUTING": TypeMeta("ABA_ROUTING", "美国银行汇款路由号", "金融账户", "high", 2, 2, priority=78),
    # ---- 组织主体 ----
    "ORG_NAME": TypeMeta("ORG_NAME", "组织机构名称", "组织主体", "medium", 2, 4, priority=66),
    "USCC": TypeMeta("USCC", "统一社会信用代码", "组织主体", "medium", 2, 2, priority=94),
    "TAX_ID": TypeMeta("TAX_ID", "税务登记号/纳税人识别号", "组织主体", "medium", 4, 4, priority=74),
    "US_EIN": TypeMeta("US_EIN", "美国雇主识别号", "组织主体", "medium", 2, 2, priority=76),
    "VAT_ID": TypeMeta("VAT_ID", "境外增值税号", "组织主体", "medium", 2, 2, priority=77),
    "REGISTRATION_NO": TypeMeta("REGISTRATION_NO", "境外主体登记号", "组织主体", "medium", 2, 2, priority=75),
    "LICENSE_NO": TypeMeta("LICENSE_NO", "证照/执业编号", "组织主体", "medium", 3, 3, priority=73),
    # ---- 商业信息 ----
    "AMOUNT": TypeMeta("AMOUNT", "合同金额", "商业信息", "medium", 0, 0, mask_fn=_mask_amount, priority=50),
    "CONTRACT_NO": TypeMeta("CONTRACT_NO", "合同/发票/订单编号", "商业信息", "medium", 2, 2, priority=52),
    "DATE": TypeMeta("DATE", "日期", "商业信息", "low", 4, 0, priority=30),
    "PLATE": TypeMeta("PLATE", "车牌号", "其他", "low", 1, 2, priority=28),
    # ---- 法律信息 ----
    "CASE_NO": TypeMeta("CASE_NO", "案件号/公文字号", "法律信息", "medium", 2, 2, priority=54),
    # ---- 凭据（block 级）----
    "SECRET": TypeMeta("SECRET", "密钥/凭据", "凭据信息", "high", 0, 0, mask_fn=_mask_secret, priority=99),
    # ---- 网络信息（默认关闭，可在规则清单中开启）----
    "IPV4": TypeMeta("IPV4", "IP 地址", "网络信息", "low", 0, 0, mask_fn=_mask_ipv4, priority=26, enabled=False),
    "MAC_ADDRESS": TypeMeta("MAC_ADDRESS", "MAC 物理地址", "网络信息", "low", 0, 0, mask_fn=_mask_mac, priority=25, enabled=False),
    # ---- 其他 ----
    "URL": TypeMeta("URL", "网址链接", "其他", "low", 0, 0, priority=24),
}

# 校验基线：内置类型的不可变副本。load_rules_md 会按既有约定改写全局 TYPE_META，
# 用户规则校验必须从这份副本出发，避免受同进程此前加载结果影响。
BUILTIN_TYPE_META: Dict[str, TypeMeta] = dict(TYPE_META)


def mask_value(value: str, type_id: str) -> str:
    """按类型策略生成脱敏预览串。"""
    meta = TYPE_META.get(type_id)
    if meta is None:
        return "*" * 6
    if meta.mask_fn:
        return meta.mask_fn(value)
    if meta.replacement:
        return meta.replacement
    n = len(value)
    h, t = meta.keep_head, meta.keep_tail
    if h + t >= n or n <= 2:
        return meta.mask_char * max(min(n, 8), 4)
    return value[:h] + meta.mask_char * (n - h - t) + (value[n - t:] if t else "")


# ==============================================================================
# 1. 校验位算法（全部本地实现，符合国家标准）
# ==============================================================================

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"


def id_card_check(value: str) -> Optional[bool]:
    """18 位身份证校验（GB 11643-1999，ISO 7064:1983 MOD 11-2）。
    返回 True/False；None 表示不是 18 位身份证号。"""
    v = value.strip().upper()
    if not re.fullmatch(r"\d{17}[\dX]", v):
        return None
    total = sum(int(v[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK_CHARS[total % 11] == v[17]


def id_card_calc_check_digit(first17: str) -> str:
    total = sum(int(first17[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK_CHARS[total % 11]


_USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"  # 31 个字符，剔除 I O S V Z
_USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
_USCC_FIRST_OK = set("123456789ANY")  # 登记管理部门代码合法取值


def uscc_check(value: str) -> Optional[bool]:
    """统一社会信用代码校验（GB 32100-2015）。None 表示长度/字符集不合法。"""
    v = value.strip().upper()
    if len(v) != 18 or any(c not in _USCC_CHARSET for c in v):
        return None
    total = sum(_USCC_CHARSET.index(c) * _USCC_WEIGHTS[i] for i, c in enumerate(v[:17]))
    r = 31 - (total % 31)
    return _USCC_CHARSET[r if r < 31 else 0] == v[17]


def uscc_calc_check_digit(first17: str) -> str:
    """计算统一社会信用代码第 18 位校验码（造测试数据用）。"""
    total = sum(_USCC_CHARSET.index(c) * _USCC_WEIGHTS[i] for i, c in enumerate(first17[:17].upper()))
    r = 31 - (total % 31)
    return _USCC_CHARSET[r if r < 31 else 0]


def uscc_dept_code_ok(value: str) -> bool:
    return bool(value) and value[0].upper() in _USCC_FIRST_OK


def luhn_check(value: str) -> bool:
    """Luhn 算法（ISO/IEC 7812-1），用于银行卡号。"""
    digits = re.sub(r"\D", "", value)
    if not (12 <= len(digits) <= 19) or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_check(value: str) -> Optional[bool]:
    """IBAN 本地 MOD-97 校验；常见国家校验总长度，其他国家走通用格式。"""
    compact = re.sub(r"[\s-]", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", compact):
        return None
    country_lengths = {"GB": 22, "DE": 22, "NL": 18, "FI": 18, "FR": 27,
                       "ES": 24, "IT": 27, "CH": 21, "BE": 16, "IE": 22}
    if compact[:2] in country_lengths and len(compact) != country_lengths[compact[:2]]:
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged)
    remainder = 0
    for ch in digits:
        remainder = (remainder * 10 + int(ch)) % 97
    return remainder == 1


def aba_routing_check(value: str) -> Optional[bool]:
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"\d{9}", digits):
        return None
    weighted = (3 * (int(digits[0]) + int(digits[3]) + int(digits[6])) +
                7 * (int(digits[1]) + int(digits[4]) + int(digits[7])) +
                int(digits[2]) + int(digits[5]) + int(digits[8]))
    return weighted % 10 == 0


def us_ssn_check(value: str) -> Optional[bool]:
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"\d{9}", digits):
        return None
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    return area not in {"000", "666"} and not area.startswith("9") and group != "00" and serial != "0000"


def digits_of(value: str) -> str:
    return re.sub(r"\D", "", value)


# ==============================================================================
# 2. 文本归一化（保持下标映射，确保定位精准）
# ==============================================================================

_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff", "\u00ad", "\u2060"}
_DASHES = {"\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212", "\uff0d", "\u30fc"}


class NormalizedText:
    """归一化文本：norm 串与 raw 串通过 index_map 一一对应（长度一致的下标映射）。"""

    __slots__ = ("raw", "norm", "_idx")

    def __init__(self, raw: str):
        chars: List[str] = []
        idx: List[int] = []
        for i, ch in enumerate(raw):
            if ch in _ZERO_WIDTH:
                continue
            o = ord(ch)
            if 0xFF01 <= o <= 0xFF5E:          # 全角 ASCII → 半角
                ch = chr(o - 0xFEE0)
            elif ch == "\u3000":
                ch = " "
            elif ch in _DASHES:
                ch = "-"
            elif ch in ("\t", "\r"):
                ch = " "
            chars.append(ch)
            idx.append(i)
        self.raw = raw
        self.norm = "".join(chars)
        self._idx = idx

    def to_raw(self, pos: int) -> int:
        """归一化下标 → 原文下标（pos 可为 len(norm)，返回 len(raw)）。"""
        if pos < 0:
            pos = 0
        if pos >= len(self._idx):
            return len(self.raw)
        return self._idx[pos]

    def to_raw_span(self, start: int, end: int) -> Tuple[int, int]:
        """归一化区间 → 原文区间（右开）。"""
        if start >= len(self._idx):
            return (len(self.raw), len(self.raw))
        raw_start = self._idx[start]
        raw_end = self._idx[end - 1] + 1 if end - 1 < len(self._idx) else len(self.raw)
        return (raw_start, raw_end)

    def raw_slice(self, start: int, end: int) -> str:
        a, b = self.to_raw_span(start, end)
        return self.raw[a:b]

    def __len__(self) -> int:
        return len(self.norm)


# ==============================================================================
# 3. 解析层：docx / pdf / txt
# ==============================================================================

@dataclass
class WordBox:
    start: int          # 块内偏移
    end: int
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass
class Block:
    text: str
    block_type: str = "paragraph"   # paragraph / table_cell / textbox / header / footer
    location: str = ""
    page: Optional[int] = None
    word_boxes: List[WordBox] = field(default_factory=list)
    # 表格坐标 (表序号, 行序号, grid 起始列, 跨列数)，均从 1 起算；仅 DOCX 表格单元格有值。
    # 列号是**按 gridSpan 展开后的视觉列**，不是 <w:tc> 的原始序号——表头若横向合并
    # （如「单价」跨 3 列），按 tc 序号对齐会整体错位（v2 实测「总单价/合价」整列漏检即此因）。
    # 用途：供「表格感知」的金额识别做**列对齐**——列头写「金额/单价」的那一列，
    # 其下方每个单元格都按金额处理，不再依赖全文关键词窗口（见 TableAmountDetector）。
    table_ref: Optional[Tuple[int, int, int, int]] = None
    part_name: str = "word/document.xml"
    part_block_index: int = -1
    comment_id: Optional[str] = None
    field_kind: str = "text"
    attribute_name: Optional[str] = None
    element_index: Optional[int] = None
    element_path: Optional[Tuple[int, ...]] = None
    linked_comment_initials: Dict[str, str] = field(default_factory=dict)
    original_initials: str = ""
    target_metadata: Dict[str, Any] = field(default_factory=dict)
    revision_units: List[Dict[str, Any]] = field(default_factory=list)
    target_offset_map: List[Tuple[int, int]] = field(default_factory=list)
    target_original: str = ""

    def bbox_for_range(self, start: int, end: int) -> Optional[Tuple[float, float, float, float]]:
        """把块内字符区间合并成一个矩形（PDF 涂黑用）。"""
        if not self.word_boxes:
            return None
        boxes = [w for w in self.word_boxes if w.end > start and w.start < end]
        if not boxes:
            return None
        return (
            min(w.x0 for w in boxes), min(w.top for w in boxes),
            max(w.x1 for w in boxes), max(w.bottom for w in boxes),
        )


@dataclass
class Document:
    source: str
    file_type: str
    blocks: List[Block]
    sha256: str = ""
    warnings: List[str] = field(default_factory=list)
    unreadable_pages: List[int] = field(default_factory=list)
    revision_analysis: Optional[Dict[str, Any]] = None

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


class PdfTextLayerError(ValueError):
    """PDF 正文未完整读取，不能继续作出脱敏结果。"""


def require_pdf_text(doc: Document) -> None:
    if doc.file_type != "pdf":
        return
    if not doc.text.strip():
        raise PdfTextLayerError(
            "未读取到 PDF 正文文字：该文件可能是扫描件或图片型 PDF。"
            "本次未启用自动 OCR —— 请改用 PDF 模式（内置本机 OCR，逐页识别并人工核对），"
            "或先转换为带文字层的 PDF / DOCX / TXT 再上传。这不代表文件没有敏感信息。")
    if doc.unreadable_pages:
        pages = "、".join(str(n) for n in doc.unreadable_pages[:20])
        raise PdfTextLayerError(
            f"PDF 第 {pages} 页只有图片或解析失败，正文尚未完整读取，已停止处理。"
            "请改用 PDF 模式逐页识别并人工核对，或先完成这些页面的本机 OCR / 转换后重新上传，"
            "不能只依据已读取页面的结果外发。")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- DOCX ---------

_DOCX_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_para_text(p) -> str:
    """段落全文：w:t 顺序拼接；tab→空格、br/cr→换行。"""
    out: List[str] = []
    from contract_docx_revisions import iter_story_content_nodes
    for node in iter_story_content_nodes(p):
        tag = node.tag
        if tag == _DOCX_W + "t":
            out.append(node.text or "")
        elif tag == _DOCX_W + "tab":
            out.append(" ")
        elif tag in (_DOCX_W + "br", _DOCX_W + "cr"):
            out.append("\n")
    return "".join(out)


def docx_para_units(p):
    """返回 (text, units)：text 与 docx_para_text(p) 逐字一致。

    units 与 text 等长，每项为：
      ('t', w:t 序号, 该节点内字符下标)  —— 真实可写字符；
      ('keep', 'tab' | 'br')             —— tab/br 折算出的**不可写保留位**。

    用途（R6）：让改写/还原阶段与检测阶段使用**同一套段落文本口径**，
    消除历史缺陷「检测把 <w:tab/> 折算成空格、改写忽略 tab」造成的偏移错位。
    改写时保留位原样跳过，故制表符/换行的数量与位置不受替换影响。
    """
    parts: List[str] = []
    units: List[Tuple] = []
    ti = -1
    from contract_docx_revisions import iter_story_content_nodes
    for node in iter_story_content_nodes(p):
        tag = node.tag
        if tag == _DOCX_W + "t":
            ti += 1
            s = node.text or ""
            for j, ch in enumerate(s):
                parts.append(ch)
                units.append(("t", ti, j))
        elif tag == _DOCX_W + "tab":
            parts.append(" ")
            units.append(("keep", "tab"))
        elif tag in (_DOCX_W + "br", _DOCX_W + "cr"):
            parts.append("\n")
            units.append(("keep", "br"))
    return "".join(parts), units


def docx_story_parts(doct):
    """枚举实际定义的普通、首页及偶数页页眉页脚，共享部件仅处理一次。"""
    seen = set()
    for si, section in enumerate(doct.sections, 1):
        for kind, label, story in (
                ("header", "页眉", section.header),
                ("footer", "页脚", section.footer),
                ("header", "首页页眉", section.first_page_header),
                ("footer", "首页页脚", section.first_page_footer),
                ("header", "偶数页页眉", section.even_page_header),
                ("footer", "偶数页页脚", section.even_page_footer)):
            if story.is_linked_to_previous:
                continue
            name = str(story.part.partname)
            if name in seen:
                continue
            seen.add(name)
            yield si, kind, label, story


def docx_para_targets(doct) -> List[Tuple[str, str, Any]]:
    """按与 extract_docx 完全一致的顺序，枚举 (block_type, location, 段落元素)。
    改写阶段（contract_redactor）依赖此顺序保证 block_index 对齐。"""
    W = _DOCX_W

    def classify(p) -> str:
        kind, tbl_depth = "paragraph", 0
        for anc in p.iterancestors():
            if anc.tag == W + "tbl":
                tbl_depth += 1
            elif anc.tag == W + "txbxContent":
                kind = "textbox"
        if tbl_depth:
            kind = "table_cell"
        return kind

    targets: List[Tuple[str, str, Any]] = []
    counters = {"paragraph": 0, "table_cell": 0, "textbox": 0}

    def excluded_alternate_textbox(paragraph):
        for ancestor in paragraph.iterancestors():
            if isinstance(ancestor.tag, str) and ancestor.tag in {
                    "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent",
                    "{http://schemas.openxmlformats.org/markup-compatibility/2006}Choice",
                    "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"}:
                return True
        return False

    for p in doct.element.body.iter(W + "p"):
        if excluded_alternate_textbox(p) or not docx_para_text(p).strip():
            continue
        kind = classify(p)
        counters[kind] = counters.get(kind, 0) + 1
        if kind == "table_cell":
            loc = f"正文表格第 {counters['table_cell']} 个单元格段落"
        elif kind == "textbox":
            loc = f"正文文本框第 {counters['textbox']} 段"
        else:
            loc = f"正文第 {counters['paragraph']} 段"
        targets.append((kind, loc, p))

    try:
        for si, kind, label, part in docx_story_parts(doct):
            for hi, p in enumerate(part._element.iter(W + "p"), 1):
                if excluded_alternate_textbox(p) or not docx_para_text(p).strip():
                    continue
                loc = f"第 {si} 节{label}第 {hi} 段"
                targets.append((kind, loc, p))
    except Exception:
        pass
    return targets


def _table_ref(p) -> Optional[Tuple[int, int, int, int]]:
    """取段落所在的表格坐标 (表序号, 行序号, grid 起始列, 跨列数)，均 1 起算；不在表格里返回 None。

    列号按 **gridSpan 展开后的视觉列** 计算，而非 <w:tc> 的原始序号：
    表格常把「单价」等列头横向合并（span=3），第一行因而只有 8 个 <w:tc>、
    而数据行有 10 个，若按 tc 序号对齐会整体错位——v2 实测「总单价/合价」
    整列漏检即由此引起。改按 grid 列后，表头与数据行才在同一坐标系里。

    只用于「表格感知」识别（列对齐），不参与 block 顺序，因此不影响改写阶段的
    block_index 对齐。嵌套表格取最内层。
    """
    W = _DOCX_W

    def _nearest(tag: str, node):
        for anc in node.iterancestors():
            if anc.tag == tag:
                return anc
        return None

    tbl = _nearest(W + "tbl", p)
    if tbl is None:
        return None
    tr = _nearest(W + "tr", p)
    tc = _nearest(W + "tc", p)
    if tr is None or tc is None:
        return None
    t_idx = r_idx = 0
    parent = tbl.getparent()
    if parent is not None:
        for i, t in enumerate(parent.iterchildren(W + "tbl"), 1):
            if t is tbl:
                t_idx = i
                break
    for i, r in enumerate(tbl.iterchildren(W + "tr"), 1):
        if r is tr:
            r_idx = i
            break
    # 沿行内 <w:tc> 累加 gridSpan，得到本单元格的 grid 起始列与跨列数
    col = 0
    c_start = c_span = 0
    for c in tr.iterchildren(W + "tc"):
        span = 1
        tcPr = c.find(W + "tcPr")
        if tcPr is not None:
            gs = tcPr.find(W + "gridSpan")
            if gs is not None and gs.get(W + "val"):
                try:
                    span = max(1, int(gs.get(W + "val")))
                except ValueError:
                    span = 1
        if c is tc:
            c_start, c_span = col + 1, span
            break
        col += span
    if not (t_idx and r_idx and c_start):
        return None
    return (t_idx, r_idx, c_start, c_span)


def _decode_hyperlink_target_for_scan(target: str) -> Tuple[str, List[Tuple[int, int]]]:
    """Decode URI unreserved and UTF-8 escapes while retaining raw XML offsets."""
    out: List[str] = []
    spans: List[Tuple[int, int]] = []
    i = 0
    unreserved = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    while i < len(target):
        if (target[i] == "%" and i + 2 < len(target) and
                re.fullmatch(r"[0-9A-Fa-f]{2}", target[i + 1:i + 3])):
            byte = int(target[i + 1:i + 3], 16)
            if byte < 128:
                char = chr(byte)
                if char in unreserved or (char == "@" and "?" in target[:i]):
                    out.append(char)
                    spans.append((i, i + 3))
                else:
                    out.extend(target[i:i + 3])
                    spans.extend((j, j + 1) for j in range(i, i + 3))
                i += 3
                continue
            start = i
            byte_values = []
            while (i + 2 < len(target) and target[i] == "%" and
                   re.fullmatch(r"[0-9A-Fa-f]{2}", target[i + 1:i + 3])):
                value = int(target[i + 1:i + 3], 16)
                if value < 128:
                    break
                byte_values.append(value)
                i += 3
            try:
                decoded = bytes(byte_values).decode("utf-8")
                cursor = start
                for char in decoded:
                    width = len(char.encode("utf-8")) * 3
                    out.append(char)
                    spans.append((cursor, cursor + width))
                    cursor += width
            except (UnicodeDecodeError, ValueError):
                for offset in range(start, i, 3):
                    out.extend(target[offset:offset + 3])
                    spans.extend((j, j + 1) for j in range(offset, offset + 3))
            continue
        out.append(target[i])
        spans.append((i, i + 1))
        i += 1
    return "".join(out), spans


def extract_docx(path: str, include_headers: bool = True) -> Document:
    try:
        from docx import Document as DocxDocument  # type: ignore
    except ImportError:  # pragma: no cover
        raise RuntimeError("读取 .docx 需要 python-docx，请先执行： pip install python-docx")

    doct = DocxDocument(path)
    blocks: List[Block] = []
    doc_warnings: List[str] = []
    from contract_docx_revisions import (
        analyze_docx_revisions, read_external_hyperlink_targets,
        read_vml_text_targets, REVISION_TAGS,
    )
    revision_tags = {_DOCX_W + name for name in REVISION_TAGS}
    try:
        revision_analysis = analyze_docx_revisions(path)
    except Exception:
        has_revision_markup = any(
            node.tag in revision_tags for node in doct.element.iter())
        revision_analysis = {
            "complete": False,
            "has_revisions": has_revision_markup,
            "passthrough_safe": False,
            "inventory_scope_unknown": True,
            "unsupported_manifest": [],
            "unsupported_object_manifest": [],
            "unsafe_field_manifest": [],
            "paragraphs": [],
            "deleted_blocks": [],
            "author_blocks": [],
            "inventory": {
                "complete": False,
                "has_revisions": has_revision_markup,
                "inventory_scope_unknown": True,
                "unscanned_parts": [],
                "unsupported_digest": "",
                "records": [],
                "unredacted_content_parts": [],
            },
        }
    field_instruction_count = int(
        revision_analysis.get("unsafe_field_instruction_count") or 0)
    if field_instruction_count:
        doc_warnings.append(
            "当前DOCX含%d个无法自动检查的Word字段；工具只扫描字段显示文字，"
            "相关指令将原样保留，请人工核对后再导出。" % field_instruction_count)
    revision_inventory = revision_analysis.get("inventory") or {}
    revision_paragraphs = {
        (row["part"], int(row["paragraph_index"])): row
        for row in revision_analysis.get("paragraphs", [])
    }
    paragraph_targets: Dict[Any, Tuple[str, int]] = {}
    for index, paragraph in enumerate(doct.element.body.iter(_DOCX_W + "p")):
        paragraph_targets[paragraph] = ("word/document.xml", index)
    for _si, _kind, _label, part in docx_story_parts(doct):
        part_name = str(part.part.partname).lstrip("/")
        for index, paragraph in enumerate(part._element.iter(_DOCX_W + "p")):
            paragraph_targets[paragraph] = (part_name, index)

    rejected_view_keys = set()
    for kind, loc, p in docx_para_targets(doct):
        text = docx_para_text(p)
        blk = Block(text=text, block_type=kind, location=loc)
        identity = paragraph_targets.get(p)
        analyzed = None
        if revision_inventory.get("has_revisions"):
            analyzed = revision_paragraphs.get(identity) if identity else None
            if analyzed is None:
                revision_analysis["supported"] = False
                revision_analysis["can_preserve_and_redact"] = False
                revision_analysis["can_export_after_ack"] = False
                revision_inventory["can_export_after_ack"] = False
                revision_analysis.setdefault("unsupported", []).append({
                    "part": identity[0] if identity else "",
                    "kind": "paragraph",
                    "reason": "正文提取段落无法与修订目标索引对应",
                })
                if revision_analysis.get("inventory_scope_unknown"):
                    part, paragraph_index = identity or ("word/document.xml", index)
                    has_revision_markup = any(
                        node.tag in revision_tags for node in p.iter())
                    blk.part_name = part
                    blk.field_kind = "revision_visible_text" if has_revision_markup else "text"
                    blk.target_metadata = {
                        "kind": blk.field_kind,
                        "part": part,
                        "paragraph_index": paragraph_index,
                        **({"rewrite_supported": False,
                           "rewrite_block_reason": "修订盘点未完成，该段按原文保留"}
                           if has_revision_markup else {}),
                    }
            else:
                if analyzed.get("text", "") != text:
                    revision_analysis["supported"] = False
                    revision_analysis["can_preserve_and_redact"] = False
                    revision_analysis["can_export_after_ack"] = False
                    revision_inventory["can_export_after_ack"] = False
                    revision_analysis.setdefault("unsupported", []).append({
                        "part": analyzed["part"], "kind": "paragraph",
                        "reason": "修订视图与正文提取文本不一致",
                    })
                blk.field_kind = ("revision_visible_text" if analyzed.get("has_revision")
                                  else "text")
                blk.part_name = analyzed["part"]
                blk.target_metadata = dict(analyzed["target"])
                blk.target_metadata["source_text_sha256"] = hashlib.sha256(
                    analyzed.get("text", "").encode("utf-8")).hexdigest()
                if revision_analysis.get("passthrough_safe") and analyzed.get("has_revision"):
                    blk.target_metadata["rewrite_supported"] = False
                    blk.target_metadata["rewrite_block_reason"] = (
                        "修订盘点不完整时，含修订标记的段落整体按原样透传")
                if analyzed.get("has_revision"):
                    blk.revision_units = list(analyzed.get("units", []))
        if kind == "table_cell":
            # 记录表格坐标，供表格感知识别（金额列对齐）使用
            blk.table_ref = _table_ref(p)
        blocks.append(blk)

        if analyzed and analyzed.get("rejected_view_supported"):
            rejected_text = analyzed.get("rejected_text", "")
            if rejected_text:
                target = dict(analyzed["target"])
                target.update({
                    "kind": "revision_rejected_text",
                    "revision_view": "rejected",
                    "source_text_sha256": hashlib.sha256(
                        rejected_text.encode("utf-8")).hexdigest(),
                })
                if revision_analysis.get("passthrough_safe"):
                    target["rewrite_supported"] = False
                    target["rewrite_block_reason"] = (
                        "修订盘点不完整时，含修订标记的段落整体按原样透传")
                rejected_block = Block(
                    text=rejected_text, block_type=kind,
                    location=loc + "·拒绝修订视图",
                    part_name=analyzed["part"], field_kind="revision_rejected_text",
                    part_block_index=int(analyzed["paragraph_index"]),
                    target_metadata=target,
                    revision_units=list(analyzed.get("rejected_units", [])))
                if kind == "table_cell":
                    rejected_block.table_ref = _table_ref(p)
                blocks.append(rejected_block)
                rejected_view_keys.add(identity)

    if revision_inventory.get("has_revisions") and not revision_analysis.get("passthrough_safe"):
        for row in revision_analysis.get("deleted_blocks", []):
            if (row["part"], int(row["paragraph_index"])) in rejected_view_keys:
                continue
            blocks.append(Block(
                text=row["text"], block_type="revision_deleted_text",
                location=row["location"], part_name=row["part"],
                field_kind="revision_deleted_text",
                part_block_index=row["paragraph_index"],
                target_metadata=dict(row["target"]),
                revision_units=list(row.get("units", []))))
        for row in revision_analysis.get("author_blocks", []):
            blocks.append(Block(
                text=row["text"], block_type="revision_author",
                location=row["location"], part_name=row["part"],
                field_kind="revision_author",
                part_block_index=row["paragraph_index"],
                attribute_name=row["target"].get("attribute"),
                target_metadata=dict(row["target"])))

    from contract_docx_comments import extract_comment_blocks, inspect_comment_package
    comment_info = inspect_comment_package(path)
    blocks.extend(extract_comment_blocks(comment_info))
    doc_warnings.extend(comment_info.warnings)

    target_rows = read_external_hyperlink_targets(path)
    unscanned_parts = {str(row.get("part") or "") for row in
                       (revision_inventory.get("unscanned_parts") or [])}
    skipped_external_targets = sum(1 for row in target_rows
                                   if row["part"] in unscanned_parts)
    revision_inventory["external_target_unscanned_count"] = skipped_external_targets
    for row in target_rows:
        if row["part"] in unscanned_parts:
            continue
        target = row["target"]
        scan_text, offset_map = _decode_hyperlink_target_for_scan(target)
        blocks.append(Block(
            text=scan_text, block_type="external_target",
            location="外部超链接目标", part_name=row["part"],
            field_kind="external_relationship_target",
            target_metadata={
                "relationship_id": row["relationship_id"],
                "relationship_kind": "hyperlink",
                "attribute": "Target",
                "target_sha256": row["target_sha256"],
            },
            target_offset_map=offset_map, target_original=target,
        ))

    for row in read_vml_text_targets(path):
        part_kind = row.get("part_kind", "paragraph")
        scope = {"paragraph": "正文", "header": "页眉", "footer": "页脚"}.get(
            part_kind, "文档")
        paragraph_index = int(row.get("paragraph_index", -1))
        location = ("%s第%d段·图形文字" % (scope, paragraph_index + 1)
                    if paragraph_index >= 0 else "%s图形文字" % scope)
        blocks.append(Block(
            text=row["value"], block_type="textbox", location=location,
            part_name=row["part"], field_kind="vml_textpath",
            part_block_index=paragraph_index,
            target_metadata={
                "node_path": row["node_path"],
                "attribute": row["attribute"],
                "target_sha256": row["target_sha256"],
                "structure_sha256": row["structure_sha256"],
            }))

    revision_inventory.update(
        supported=bool(revision_analysis.get("supported")),
        can_preserve_and_redact=bool(revision_analysis.get("can_preserve_and_redact")),
        can_export_after_ack=bool(revision_analysis.get("can_export_after_ack")),
        unsupported=list(revision_analysis.get("unsupported") or []))

    if not blocks:
        raise RuntimeError("文档中没有可提取的文字（可能是空文档或图片型文件）")
    return Document(source=path, file_type="docx", blocks=blocks, sha256=_sha256(path),
                    warnings=doc_warnings, revision_analysis=revision_analysis)


# ----------------------------------------------------------------- PDF ---------

def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) or (0x3000 <= o <= 0x303F)


_PDF_CELL_BOUNDARY = "\ue000"
_PDF_COLUMN_GAP = 18.0


def _pdf_word_lines(words: Sequence[dict]) -> List[List[dict]]:
    lines: List[List[dict]] = []
    current: List[dict] = []
    current_top: Optional[float] = None
    for word in sorted(words, key=lambda item: (round(item["top"], 1), item["x0"])):
        if current_top is None or abs(word["top"] - current_top) <= 3.0:
            current.append(word)
            if current_top is None:
                current_top = word["top"]
        else:
            lines.append(sorted(current, key=lambda item: item["x0"]))
            current = [word]
            current_top = word["top"]
    if current:
        lines.append(sorted(current, key=lambda item: item["x0"]))
    return lines


def _append_pdf_words(parts: List[str], boxes: List[WordBox], words: Sequence[dict],
                      pos: int, line_separator: str = "\n",
                      split_columns: bool = False,
                      geoms: Optional[List[str]] = None) -> Tuple[int, bool]:
    """geoms 与 parts 同步追加逐字符定位标签（character/word/none），
避免单个词级框拖累同页其他精确词。"""
    char_geometry = True
    for line_index, line in enumerate(_pdf_word_lines(words)):
        if line_index:
            parts.append(line_separator)
            pos += len(line_separator)
            if geoms is not None:
                geoms.extend(["none"] * len(line_separator))
        segments: List[List[dict]] = [[]]
        for word in line:
            if (split_columns and segments[-1] and
                    word["x0"] - segments[-1][-1]["x1"] > _PDF_COLUMN_GAP):
                segments.append([])
            segments[-1].append(word)
        for segment_index, segment in enumerate(segments):
            if segment_index:
                parts.append(_PDF_CELL_BOUNDARY)
                pos += len(_PDF_CELL_BOUNDARY)
                if geoms is not None:
                    geoms.extend(["none"] * len(_PDF_CELL_BOUNDARY))
            for word_index, word in enumerate(segment):
                if word_index:
                    previous = segment[word_index - 1]["text"]
                    following = word["text"]
                    separator = "" if (previous and _is_cjk(previous[-1])) or (
                        following and _is_cjk(following[0])
                    ) else " "
                    if separator:
                        parts.append(separator)
                        pos += len(separator)
                        if geoms is not None:
                            geoms.extend(["none"] * len(separator))
                parts.append(word["text"])
                chars = word.get("chars") or []
                exact_chars = ("".join(ch.get("text", "") for ch in chars) == word["text"])
                if exact_chars:
                    try:
                        exact_chars = all(
                            len(ch.get("text", "")) == 1 and
                            all(math.isfinite(float(ch[key])) for key in
                                ("x0", "top", "x1", "bottom")) and
                            float(ch["x1"]) > float(ch["x0"]) and
                            float(ch["bottom"]) > float(ch["top"])
                            for ch in chars
                        )
                    except (KeyError, TypeError, ValueError):
                        exact_chars = False
                if exact_chars:
                    for offset, char in enumerate(chars):
                        boxes.append(WordBox(
                            pos + offset, pos + offset + 1,
                            float(char["x0"]), float(char["top"]),
                            float(char["x1"]), float(char["bottom"]),
                        ))
                    if geoms is not None:
                        geoms.extend(["character"] * len(word["text"]))
                else:
                    char_geometry = False
                    boxes.append(WordBox(
                        pos, pos + len(word["text"]),
                        float(word["x0"]), float(word["top"]),
                        float(word["x1"]), float(word["bottom"]),
                    ))
                    if geoms is not None:
                        geoms.extend(["word"] * len(word["text"]))
                pos += len(word["text"])
    return pos, char_geometry


def _compact_pdf_labeled_fields(block):
    """仅合并明确字段内的分字间隔，并同步原始字形坐标。"""
    patterns = (
        (r"(?:联系电话|手机号码|手机号|电话)", r"(?:\+?86)?1[3-9]\d{9}"),
        (r"(?:电子邮箱|邮箱|E-?mail)", r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        (r"(?:银行账号|银行帐号|收款账号|收款帐号)", r"\d{12,30}"),
        (r"(?:合同金额|请求金额|价款|金额)", r"(?:人民币|[¥￥])?\d[\d,]*(?:\.\d{1,2})?元?"),
        (r"(?:签署日期|生效日期|到期日期|日期)", r"\d{4}年\d{1,2}月\d{1,2}日"),
    )
    removed = set()
    for line in re.finditer(r"[^\n\ue000]+", block.text):
        for label, value_pattern in patterns:
            match = re.fullmatch(r"\s*" + label + r"[：:]\s*(?P<value>[^：:]{1,150})", line.group(), re.I)
            if not match:
                continue
            value = match.group("value")
            # 只有至少四处分字间隔才启动；普通英文单词间空格不因此消失。
            if len(re.findall(r"(?<=\S) +(?=\S)", value)) < 4:
                continue
            compact = value.replace(" ", "")
            if not re.fullmatch(value_pattern, compact):
                continue
            start = line.start() + match.start("value")
            removed.update(start + i for i, ch in enumerate(value) if ch == " ")
    if not removed:
        return block
    offsets = [0]
    for i in range(len(block.text)):
        offsets.append(offsets[-1] + (i not in removed))
    block.text = "".join(ch for i, ch in enumerate(block.text) if i not in removed)
    block.word_boxes = [WordBox(offsets[w.start], offsets[w.end], w.x0, w.top, w.x1, w.bottom)
                        for w in block.word_boxes if offsets[w.end] > offsets[w.start]]
    # 逐字符定位标签与文本同步重排，防止索引错位。
    geoms = getattr(block, "ocr_geometries", None)
    if isinstance(geoms, list) and len(geoms) == len(offsets) - 1:
        block.ocr_geometries = [g for i, g in enumerate(geoms) if i not in removed]
    else:
        block.ocr_geometries = None
    return block


def _build_from_pdfplumber(page, page_no: int) -> Optional[Block]:
    words = page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False,
                               use_text_flow=False, return_chars=True)
    if not words:
        return None
    table_rows: List[Tuple[float, int, List[Tuple[float, float, float, float]]]] = []
    table_cells: List[Tuple[int, Tuple[float, float, float, float]]] = []
    try:
        if len(page.horizontal_edges) >= 2 and len(page.vertical_edges) >= 2:
            for table in page.find_tables():
                for row in table.rows:
                    row_index = len(table_rows)
                    cells = sorted({cell for cell in row.cells if cell is not None})
                    if cells:
                        table_rows.append((row.bbox[1], row_index, cells))
                        table_cells.extend((row_index, cell) for cell in cells)
    except Exception:
        table_rows = []
        table_cells = []

    assigned: Dict[int, Dict[Tuple[float, float, float, float], List[dict]]] = {
        row_index: {cell: [] for cell in cells}
        for _, row_index, cells in table_rows
    }
    outside: List[dict] = []
    for word in words:
        cx = (word["x0"] + word["x1"]) / 2
        cy = (word["top"] + word["bottom"]) / 2
        owners = [
            ((cell[2] - cell[0]) * (cell[3] - cell[1]), row_index, cell)
            for row_index, cell in table_cells
            if cell[0] <= cx <= cell[2] and cell[1] <= cy <= cell[3]
        ]
        if owners:
            _, row_index, cell = min(owners, key=lambda item: item[0])
            assigned[row_index][cell].append(word)
        else:
            outside.append(word)

    events: List[Tuple[float, str, Any, Any]] = []
    for top, row_index, cells in table_rows:
        if any(assigned[row_index][cell] for cell in cells):
            events.append((top, "table", row_index, cells))
    for line in _pdf_word_lines(outside):
        events.append((line[0]["top"], "outside", line, None))
    events.sort(key=lambda event: (event[0], event[1] != "table"))

    parts: List[str] = []
    boxes: List[WordBox] = []
    geoms: List[str] = []
    char_geometry = True
    pos = 0
    previous_kind: Optional[str] = None
    for _, kind, value, cells in events:
        if parts:
            separator = _PDF_CELL_BOUNDARY if kind == "table" or previous_kind == "table" else "\n"
            parts.append(separator)
            pos += len(separator)
            geoms.extend(["none"] * len(separator))
        if kind == "table":
            row_index = value
            for cell_index, cell in enumerate(cells):
                if cell_index:
                    parts.append(_PDF_CELL_BOUNDARY)
                    pos += len(_PDF_CELL_BOUNDARY)
                    geoms.extend(["none"] * len(_PDF_CELL_BOUNDARY))
                pos, exact = _append_pdf_words(
                    parts, boxes, assigned[row_index][cell], pos,
                    line_separator=" ", split_columns=False, geoms=geoms,
                )
                char_geometry = char_geometry and exact
        else:
            pos, exact = _append_pdf_words(
                parts, boxes, value, pos,
                line_separator="\n", split_columns=True, geoms=geoms,
            )
            char_geometry = char_geometry and exact
        previous_kind = kind

    block = Block(text="".join(parts), block_type="page", location=f"第 {page_no} 页",
                  page=page_no, word_boxes=boxes)
    block.ocr_geometry = "character" if char_geometry and boxes else "word"
    # 逐字符定位标签；长度对开时回退整块标记，不推断精度。
    if len(geoms) == len(block.text):
        block.ocr_geometries = geoms
    else:
        block.ocr_geometries = None
    # pdfplumber保留裁切前的绝对坐标；预览与导出采用裁切后的可见画布。
    left, top = page.bbox[:2]
    if left or top:
        block.word_boxes = [WordBox(w.start, w.end, w.x0 - left, w.top - top,
                                    w.x1 - left, w.bottom - top)
                            for w in block.word_boxes]
    return _compact_pdf_labeled_fields(block)


def extract_pdf(path: str) -> Document:
    warnings: List[str] = []
    blocks: List[Block] = []
    unreadable_pages: List[int] = []

    try:
        import pdfplumber  # type: ignore
    except ImportError:
        pdfplumber = None  # type: ignore

    if pdfplumber is not None:
        try:
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages, 1):
                    try:
                        visible_page = page.crop(page.cropbox, strict=False) if page.cropbox else page
                        blk = _build_from_pdfplumber(visible_page, i)
                    except Exception as exc:  # 单页失败不拖垮整体
                        warnings.append(f"第 {i} 页解析异常：{exc}")
                        unreadable_pages.append(i)
                        blk = None
                    if blk and blk.text.strip():
                        blocks.append(blk)
                    elif i not in unreadable_pages and page.images:
                        unreadable_pages.append(i)
                total = len(pdf.pages)
        except Exception as exc:
            raise RuntimeError(f"PDF 解析失败：{exc}")
    else:
        try:
            import pypdf  # type: ignore
        except ImportError:
            raise RuntimeError("读取 PDF 需要 pdfplumber 或 pypdf： pip install pdfplumber pypdf")
        try:
            reader = pypdf.PdfReader(path)
        except Exception as exc:
            raise RuntimeError(f"PDF 解析失败（可能已加密或损坏）：{exc}")
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                raise RuntimeError("PDF 已加密，请先解密或提供可提取文本的版本")
        warnings.append("未安装 pdfplumber，已降级为 pypdf：PDF 坐标信息不可用（不影响识别）")
        total = len(reader.pages)
        for i, page in enumerate(reader.pages, 1):
            try:
                t = page.extract_text() or ""
            except Exception:
                unreadable_pages.append(i)
                t = ""
            if t.strip():
                blocks.append(Block(text=t, block_type="page", location=f"第 {i} 页", page=i))
            elif i not in unreadable_pages and page.images:
                unreadable_pages.append(i)

    if unreadable_pages:
        warnings.append("未完整读取第 " + "、".join(str(n) for n in unreadable_pages[:20]) +
                        " 页：图片页或解析失败，需要先完成 OCR / 转换。未识别不代表没有敏感信息。")
    if not blocks or len("".join(b.text for b in blocks).strip()) < 10:
        warnings.append("未提取到有效文本层：该 PDF 很可能是扫描件/图片型，需要 OCR 后才能识别")
    return Document(source=path, file_type="pdf", blocks=blocks, sha256=_sha256(path),
                    warnings=warnings, unreadable_pages=unreadable_pages)


# ----------------------------------------------------------------- TXT ---------

def extract_text_file(path: str) -> Document:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    blocks = [Block(text=line, block_type="line", location=f"第 {i} 行")
              for i, line in enumerate(raw.splitlines(), 1) if line.strip()]
    if not blocks:
        blocks = [Block(text=raw, block_type="line", location="全文")]
    return Document(source=path, file_type="txt", blocks=blocks, sha256=_sha256(path))


def load_document(path: str, include_headers: bool = True) -> Document:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return extract_docx(path, include_headers)
    if ext == ".doc":
        raise RuntimeError("不支持旧版二进制 .doc，请先另存为 .docx 或导出 PDF（本工具不联网转换）")
    if ext == ".pdf":
        return extract_pdf(path)
    if ext in (".txt", ".md", ".markdown"):
        return extract_text_file(path)
    raise RuntimeError(f"不支持的文件类型：{ext}（支持 .docx / .pdf / .txt / .md）")


# ==============================================================================
# 4. 检测层
# ==============================================================================

@dataclass
class RawMatch:
    type_id: str
    start: int              # 归一化全文下标
    end: int
    confidence: float
    reasons: List[str] = field(default_factory=list)
    value: str = ""         # 归一化后的值
    attrs: Dict[str, Any] = field(default_factory=dict)


def _iter_text_lines(text: str):
    offset = 0
    for line in text.splitlines(keepends=True):
        value = line.rstrip("\r\n")
        yield offset, value
        offset += len(line)
    if text and not text.endswith(("\n", "\r")) and not text.splitlines(keepends=True):
        yield 0, text


@dataclass
class ScanContext:
    nt: NormalizedText
    text: str               # == nt.norm
    # 原始块列表（与 text 同序）。表格感知识别（TableAmountDetector）靠它拿列对齐信息；
    # 用 txt/pdf 路径时可为空，此时表格感知检测器自动退化为不产出。
    blocks: List[Any] = field(default_factory=list)

    def before(self, start: int, n: int = 40) -> str:
        return self.text[max(0, start - n):start]

    def after(self, end: int, n: int = 40) -> str:
        return self.text[end:end + n]

    def window(self, start: int, end: int, n: int = 40) -> str:
        return self.text[max(0, start - n):start] + " " + self.text[end:end + n]

    def has_kw(self, start: int, end: int, keywords: Sequence[str], window: int = 30) -> Optional[str]:
        ctx = self.before(start, window) + "|" + self.after(end, window)
        for kw in keywords:
            if re.search(r"[A-Za-z]", kw):
                line_start = self.text.rfind("\n", 0, start) + 1
                line_end = self.text.find("\n", end)
                if line_end < 0:
                    line_end = len(self.text)
                same_line = self.text[max(line_start, start - window):min(line_end, end + window)]
                if kw.casefold() in {"account", "acct"}:
                    current_prefix = self.text[line_start:start]
                    if (line_start > 0 and
                            re.search(r"(?i)\b" + re.escape(kw) + r"\s*$", current_prefix)):
                        prev_end = line_start - 1
                        prev_start = self.text.rfind("\n", 0, max(0, prev_end - 1)) + 1
                        prev_line = self.text[prev_start:prev_end].rstrip()
                        if re.search(r"(?i)\bbank\s*$", prev_line):
                            return kw
                    if re.search(r"(?i)\b" + re.escape(kw) + r"\b", same_line):
                        return kw
                elif kw.casefold() in same_line.casefold():
                    return kw
                previous = self.text[max(0, line_start - window):line_start].rstrip()
                if previous.endswith((":", "：")) and kw.casefold() in previous.casefold():
                    return kw
            elif kw in ctx:
                return kw
        return None


# ------------------------------------------------------------------------------
# 4.1 通用正则检测器
# ------------------------------------------------------------------------------

@dataclass
class RegexSpec:
    type_id: str
    pattern: str
    conf_ok: float
    conf_weak: float = 0.4
    flags: int = 0
    validator: Optional[Callable[[str], Optional[bool]]] = None
    ok_reason: str = "校验位通过"
    weak_reason: str = "校验位未通过"
    keywords: Sequence[str] = ()
    kw_boost: float = 0.12
    require_keyword: bool = False
    anti_keywords: Sequence[str] = ()
    group: int = 0
    strip_chars: str = ""
    max_per_doc: Optional[int] = None
    context_chars: int = 30  # 关键词搜索窗口大小
    source: str = "内置规则"
    label: str = ""
    bounded: bool = False


USER_RULE_SOURCE = "用户自定义规则"
RULES_PATTERN_MAX_LEN = 300
RULES_MATCH_TIMEOUT_SECONDS = 2.0

try:
    import regex as _bounded_regex
except ImportError:  # 缺依赖时由校验层明确报错，不静默降级
    _bounded_regex = None


class RulesFormatError(ValueError):
    """规则文件未通过校验。code 供页面区分提示，missing 携带缺失或待确认类型。"""

    def __init__(self, message: str, code: str = "RULES_INVALID",
                 missing: Optional[Sequence[str]] = None):
        super().__init__(message)
        self.code = code
        self.missing = list(missing or [])


class RuleMatchTimeout(RuntimeError):
    """用户自定义正则单次匹配超过时限。"""


def _bounded_matches(regex_obj, text: str, type_id: str):
    """有界执行：regex 模块单次匹配带超时；超时中止并抛出明确错误。"""
    try:
        for m in regex_obj.finditer(text, timeout=RULES_MATCH_TIMEOUT_SECONDS):
            yield m
    except TimeoutError as exc:
        raise RuleMatchTimeout(
            "规则 %s 的正则匹配超过 %g 秒时限，已停止本次识别；请修正该正则后重试"
            % (type_id, RULES_MATCH_TIMEOUT_SECONDS)) from exc


class RegexDetector:
    def __init__(self, spec: RegexSpec):
        self.spec = spec
        self.bounded = bool(getattr(spec, "bounded", False))
        if self.bounded:
            if _bounded_regex is None:
                raise RulesFormatError(
                    "运行环境缺少具备超时能力的 regex 组件，用户自定义正则未启用；"
                    "请先安装依赖（pip install -r requirements.txt）再重试",
                    code="RULES_BOUNDED_UNAVAILABLE")
            self.regex = _bounded_regex.compile(spec.pattern, spec.flags)
        else:
            self.regex = re.compile(spec.pattern, spec.flags)

    def run(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        if self.bounded:
            matches = _bounded_matches(self.regex, ctx.text, self.spec.type_id)
        else:
            matches = self.regex.finditer(ctx.text)
        for m in matches:
            g = m.group(self.spec.group)
            if not g:
                continue
            s, e = m.span(self.spec.group)
            # 剥离分两步记账。先按空白剥，再按 spec.strip_chars 剥，
            # 两段剥离的长度都要从起点右移，两段都要记账：
            # 一旦 strip_chars 里出现能落在外层的字符（如 EMAIL 前导的 '_'），
            # lead 会少算，使 e = s + len(value) 整体偏左、末尾若干字符以明文残留。
            v1 = g.strip()
            lead = len(g) - len(g.lstrip())
            if self.spec.strip_chars:
                v2 = v1.strip(self.spec.strip_chars)
                lead += len(v1) - len(v1.lstrip(self.spec.strip_chars))
            else:
                v2 = v1
            value = v2
            if not value:
                continue
            if (self.spec.type_id == "CONTRACT_NO" and
                    value.casefold() in {"agreement", "contract", "reference", "invoice",
                                         "purchase", "number", "order", "ref", "no"}):
                continue
            s += lead
            e = s + len(value)

            conf = self.spec.conf_ok
            meta = TYPE_META.get(self.spec.type_id)
            label = self.spec.label or (meta.label if meta else self.spec.type_id)
            reasons: List[str] = [f"匹配 {label} 模式", f"来源：{self.spec.source}"]

            verdict: Optional[bool] = None
            if self.spec.validator is not None:
                verdict = self.spec.validator(value)
                if verdict is True:
                    conf = self.spec.conf_ok
                    reasons.append(self.spec.ok_reason)
                elif verdict is False:
                    conf = self.spec.conf_weak
                    reasons.append(self.spec.weak_reason)
                else:
                    conf = self.spec.conf_weak
                    reasons.append("格式不完整")
            else:
                reasons.append("格式匹配")

            kw = ctx.has_kw(s, e, self.spec.keywords, window=self.spec.context_chars) if self.spec.keywords else None
            if kw:
                conf += self.spec.kw_boost
                reasons.append(f"上下文含关键词「{kw}」")
            elif self.spec.require_keyword:
                continue

            if self.spec.anti_keywords:
                bad = ctx.has_kw(s, e, self.spec.anti_keywords, window=12)
                if bad:
                    # 反关键词只做「降权」而非「一票否决」：扣分幅度不得把「带关键词+命中反关键词」的有效读数压到门槛以下。
                    conf -= 0.15
                    reasons.append(f"疑似非敏感上下文「{bad}」")

            conf = max(0.0, min(conf, 0.99))
            attrs: Dict[str, Any] = {}
            if self.spec.validator is not None:
                attrs["validator_result"] = verdict
            if kw:
                attrs["matched_keyword"] = kw
            out.append(RawMatch(self.spec.type_id, s, e, conf, reasons, value, attrs))
            if self.spec.max_per_doc and len(out) >= self.spec.max_per_doc:
                break
        return out


# ------------------------------------------------------------------------------
# 4.2 姓名检测器（上下文标签 + 姓氏表 + 右边界校验）
# ------------------------------------------------------------------------------

NAME_LABELS = (
    "甲方", "乙方", "丙方", "丁方", "戊方", "法定代表人", "法人代表", "法人", "负责人",
    "联系人", "授权代表", "签约代表", "经办人", "承办人", "签字", "签名", "署名", "姓名",
    "受托人", "委托人", "代理人", "当事人", "开户名", "户名", "账户名", "收款人", "付款人",
    "借款人", "出借人", "保证人", "抵押人", "质权人", "员工", "劳动者", "乙方代表", "甲方代表",
    "项目负责人", "技术负责人", "项目经理", "设计师", "工程师", "顾问", "证人", "申请人",
    # 角色类（表格行：角色 + 姓名）
    "产品负责人", "业务负责人", "商务负责人", "财务负责人", "内容负责人", "运营负责人",
    "客户负责人", "客户经理", "法务负责人", "法务合规", "艺人商务", "舆情值班",
    # 补充：复合标签（已按长度倒序，长标签优先，不遮蔽其中的「代表」「联系人」）
    "销售代表", "主要联系人", "业务联系人", "账单联系人",
    # 表格列头
    "角色", "人员", "代表",
    # 表单字段
    "主持人", "主播", "艺人", "代言人", "嘉宾", "对接人", "接口人", "资源方",
)
# 重要：正则的 | 是**有序**匹配；长标签必须在前面，否则短标签先匹配掉
NAME_LABELS = tuple(sorted(NAME_LABELS, key=len, reverse=True))

# 常见非人名 2~4 字词（虽然首字可能是常见姓氏，但本身不是人名）
# 收录「商务 / 商业 / 权利 / 解除 / 权限 / 双确认 / 方式签署」之类
NAME_STOPWORDS = frozenset({
    "商务", "商业", "权利", "义务", "解除", "终止", "权限", "确认", "签署", "签订",
    "生效", "失效", "变更", "转让", "授权", "委托", "代理", "保证", "抵押", "质押",
    "违约", "赔偿", "损失", "费用", "价款", "金额", "付款", "收款", "结算",
    "开始", "结束", "截止", "起算", "计算", "通知", "送达", "披露", "保密",
    "书面", "口头", "电子", "网络", "线上", "线下", "现场", "当面",
    "甲方", "乙方", "丙方", "双方", "各方", "对方", "第三方", "本人",
    "公司", "企业", "单位", "部门", "小组", "团队", "中心",
    "合同", "协议", "附件", "附录", "清单", "确认单", "变更单",
    "双确认", "方式签署", "商业合作", "合法权益", "商业秘密", "个人信息",
    "不可抗力", "适用法律", "争议解决", "仲裁条款",
    "艺人商务", "舆情值班", "法务合规",
    "中国大陆", "中华人民共和国",
})
# 当事方标签（甲方/乙方…）后面经常跟的是「甲方应于…」「甲方项目经理：」，
# 因此这类标签必须紧跟冒号或括号，否则不作为人名引导词。
_PARTY_LABELS = ("甲方", "乙方", "丙方", "丁方", "戊方")
# C2（v1.3.0）：落款 / 署名块常见的「团队／小组」类角色标签。
# 实测「检查组：张明、周敏」整条漏检——除顿号并列只取首人外，更因为 NAME_LABELS 里
# 根本没有这类词，label 路径**完全不触发**（加了冒号也不认）。
# 与当事方标签同口径：**要求冒号**，避免「检查组发现…」「工作组已成立」这类正文被误当引导词。
_TEAM_LABELS = (
    "检查组", "审核组", "验收组", "评估组", "评审组", "考察组", "调查组", "核查组",
    "审计组", "谈判组", "工作组", "项目组", "专项组", "小组", "团队",
    "参会人员", "参加人员", "出席人员", "与会人员",
)
# 必须紧跟冒号的标签 = 当事方 + 团队/小组类
_COLON_LABELS = tuple(sorted(set(_PARTY_LABELS) | set(_TEAM_LABELS), key=len, reverse=True))
_OTHER_LABELS = tuple(x for x in NAME_LABELS if x not in _PARTY_LABELS)
_STRONG_NO_COLON_NAME_LABELS = frozenset({
    "法定代表人", "法人代表", "联系人", "授权代表", "签约代表", "经办人", "承办人",
    "签字", "签名", "署名", "姓名", "受托人", "委托人", "代理人", "开户名", "户名",
    "账户名", "收款人", "付款人", "借款人", "出借人", "保证人", "抵押人", "质权人",
    "乙方代表", "甲方代表", "项目负责人", "技术负责人", "项目经理", "设计师", "工程师",
    "证人", "申请人", "产品负责人", "业务负责人", "商务负责人", "财务负责人",
    "内容负责人", "运营负责人", "客户负责人", "客户经理", "法务负责人", "法务合规",
    "艺人商务", "舆情值班", "销售代表", "主要联系人", "业务联系人", "账单联系人",
    "主持人", "主播", "艺人", "代言人", "嘉宾", "对接人", "接口人", "资源方",
})

_PAREN = r"(?:\s*[（(【][^)）】]{0,12}[）)】])?"
# 标签与冒号之间可夹角色修饰语（「主要联系人**及职位**：」「授权代表**（姓名）**：」）。
#     只白名单到固定词，不会吞人名——错位时仍会走后续五道否决（职务词/停用词/姓氏/右边界）。
_MID = r"(?:\s*(?:及|与|和)?\s*(?:职位|职务|职称|岗位|称呼|姓名|签字|签章|签名))?"
# 标签与姓名之间常是填写线/点线/空白（「联系人：__钱睿____」）。上限 24 位，防跨段误抓。
_FILLER = r"[_＿．.\-·\s\u00a0\u3000]{0,24}"
_NAME_LABEL_RE = re.compile(
    "(?:"
    + "|".join(_COLON_LABELS) + r")\s*" + _PAREN + r"\s*[：:＝=][ \t]*" + _FILLER   # 当事方/团队：必须有冒号
    + "|"
    + "(?:" + "|".join(_OTHER_LABELS) + r")\s*" + _PAREN + r"\s*" + _MID + r"\s*[：:＝=]?[ \t]*" + _FILLER  # 其余：冒号可选
)
_ENGLISH_CHINESE_NAME_LABEL_RE = re.compile(
    r"(?i)\b(?:chinese\s+(?:full\s+)?name|name\s*\(\s*chinese\s*\))\b"
    r"\s*[:：＝=][ \t]*"
)
_NAME_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,8}(?:·[\u4e00-\u9fff]{1,6})*")
# 并列姓名的分隔符。**刻意只收非汉字分隔符**（顿号/逗号）——「及/和/与」本身是汉字，
# 会被 _NAME_RUN_RE 吞进同一个 run，收了也没用，反而放宽边界。
_NAME_SEP_RE = re.compile(r"\s*[、，,]\s*")
_NAME_ACTION_TAILS = (
    "形成", "参与", "提出", "提交", "提供", "签署", "签订", "确认", "主张", "表示",
    "担任", "负责", "沟通", "发送", "接收", "出具", "签收", "取得", "支付", "收取",
    "认可", "完成", "制作", "保管", "保存", "核对", "建立", "掌握", "说明", "记录",
    "回复", "参加", "出席",
)
_SURNAME_PATTERN = "(?:" + "|".join(
    re.escape(name) for name in sorted(COMPOUND_SURNAMES, key=len, reverse=True)
) + "|[" + re.escape("".join(sorted(SURNAMES))) + "])"
_ACTION_CONTEXT_NAME_RE = re.compile(
    r"由(?P<raw>" + _SURNAME_PATTERN +
    r"[\s\u00a0\u3000]{0,3}[\u4e00-\u9fff](?:[\s\u00a0\u3000]{0,3}[\u4e00-\u9fff])?)"
    r"\s*(?P<tail>" + "|".join(sorted(_NAME_ACTION_TAILS, key=len, reverse=True)) + r")"
)


def _iter_name_runs(text: str, pos: int) -> List[Tuple[int, int, str]]:
    """从 pos 起解析「姓名 (、姓名)*」序列，返回 [(起点, 终点, 字面), ...]。

    落款常见「检查组：张明、周敏、李强」——并列的多人须在**同一个标签**下逐个取出，
    每个人仍要各自通过后续的姓氏/职务词/停用词闸门，不会放宽既有误报防护。
    """
    runs: List[Tuple[int, int, str]] = []
    i = pos
    while len(runs) < 8:
        run = _NAME_RUN_RE.match(text, i)
        if not run:
            break
        runs.append((i, run.end(), run.group(0)))
        sep = _NAME_SEP_RE.match(text, run.end())
        if not sep:
            break
        i = sep.end()
    return runs

# 职务/角色词：这些词即使首字是常见姓氏，也不能当人名
ROLE_WORDS = (
    "经理", "总监", "主管", "主任", "秘书", "助理", "专员", "代表", "工程师", "设计师",
    "顾问", "律师", "会计", "出纳", "监理", "施工", "项目", "负责", "联系", "经办",
    "承办", "签字", "签名", "署名", "姓名", "当事", "受托", "委托", "代理", "保证",
    "抵押", "质权", "员工", "劳动", "证人", "申请", "顾问", "甲方", "乙方", "丙方",
    "双方", "公司", "单位", "部门", "小组", "团队", "中心", "科室", "应于", "应在",
    "应当", "应向", "应为", "已将", "将于", "并于", "需于", "应自", "应就",
)
_NAME_CUE_RE = re.compile(r"身份证|证件号|联系电话|手机号|电话|邮箱|住址|联系方式|先生|女士|工号|员工|护照|项目|产品|商务|内容|法务|财务|运营|客户|艺人|代言|对接|联系|渠道|邮箱|项目群|企业微信|项目负责人|产品负责人|业务负责人|商务负责人|内容负责人|法务负责人|艺人商务|舆情值班")
_NAME_REPEAT_CONTEXT_PREFIXES = tuple(sorted(
    set(_STRONG_NO_COLON_NAME_LABELS) | set(_PARTY_LABELS), key=len, reverse=True
))


_LAWYER_ACTIONS = ("出庭", "提交", "代理", "签署", "表示", "认为", "参加", "发表", "负责")
_LAWYER_TAIL_RE = re.compile(
    r"(?P<name>" + _SURNAME_PATTERN + r"[\u4e00-\u9fff]{1,2})\s*律师"
    r"(?=$|[\s，。；、,:：）)]|" + "|".join(_LAWYER_ACTIONS) + r")"
)
_LAWYER_HEAD_RE = re.compile(
    r"律师[ \t：:]*(?P<name>" + _SURNAME_PATTERN + r"[\u4e00-\u9fff]{1,2})"
    r"(?=$|[\s，。；、,:：）)]|" + "|".join(_LAWYER_ACTIONS) + r")"
)
_LAWYER_AGENT_LABEL_RE = re.compile(
    r"(?:代理律师|委托律师|受托律师|承办律师|经办律师|律师代理人)"
    r"\s*[：:]\s*(?P<name>" + _SURNAME_PATTERN + r"[\u4e00-\u9fff]{1,2})"
    r"(?=律师|$|[\s，。；、,:：）)])"
)


def _iter_lawyer_name_spans(text):
    """称谓不能代替姓名证据，保留原始物理区间。"""
    for pattern, direction in ((_LAWYER_AGENT_LABEL_RE, "label"),
                               (_LAWYER_HEAD_RE, "head"),
                               (_LAWYER_TAIL_RE, "tail")):
        for hit in pattern.finditer(text):
            start, end = hit.span("name")
            value = text[start:end]
            if (value in NAME_STOPWORDS or any(word in value for word in ROLE_WORDS) or
                    value in {"应当", "费用", "事务", "出庭", "提交", "代理", "认为", "表示"}):
                continue
            prefix = text[max(0, start - 16):start]
            if direction == "tail" and start and "\u4e00" <= text[start - 1] <= "\u9fff":
                if not prefix.endswith(("由", "请", "向", "及", "和", "与", "代理人", "事务所")):
                    continue
            yield start, end, value


class PersonNameDetector:
    type_id = "PERSON_NAME"

    def __init__(self, min_confidence_without_surname: float = 0.45):
        self.min_conf_no_surname = min_confidence_without_surname

    def run(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        text = ctx.text
        label_matches = list(_NAME_LABEL_RE.finditer(text))
        label_matches.extend(_ENGLISH_CHINESE_NAME_LABEL_RE.finditer(text))
        for m in sorted(label_matches, key=lambda item: item.start()):
            label = m.group(0).strip()
            if not label:
                continue
            strict = bool(re.search(r"[：:＝=]", label))
            label_key = next((item for item in NAME_LABELS if label.startswith(item)), "")
            if not label_key and _ENGLISH_CHINESE_NAME_LABEL_RE.fullmatch(label):
                label_key = "Chinese Name"
            strong_noncolon_label = not strict and label_key in _STRONG_NO_COLON_NAME_LABELS
            start0 = m.end()
            # 标签后可能是「姓名、姓名、姓名」的并列落款 —— 逐个取出（每人各自过闸门）
            seq = _iter_name_runs(text, start0)
            if not seq:
                continue
            cue = bool(_NAME_CUE_RE.search(text[start0:start0 + 40]))
            for k, (rpos, rend, run_txt) in enumerate(seq):
                picked = False
                preferred_action_len = 0
                short_len = 3 if run_txt[:2] in COMPOUND_SURNAMES else 2
                short_name = run_txt[:short_len]
                short_tail = run_txt[short_len:]
                short_surname_hit = bool(short_name) and (
                    short_name[0] in SURNAMES or short_name[:2] in COMPOUND_SURNAMES
                )
                if (strong_noncolon_label and len(short_name) == short_len and short_tail and
                        short_surname_hit and short_name not in NAME_STOPWORDS and
                        not any(word in short_name for word in ROLE_WORDS) and
                        any(short_tail.startswith(word) for word in _NAME_ACTION_TAILS)):
                    preferred_action_len = short_len
                for L in (4, 3, 2):
                    if L > len(run_txt):
                        continue
                    if preferred_action_len and L != preferred_action_len:
                        continue
                    name = run_txt[:L]
                    rest = run_txt[L:]
                    action_tail = strong_noncolon_label and any(
                        rest.startswith(word) for word in _NAME_ACTION_TAILS
                    )
                    if (rest and rest[0] not in NAME_BOUNDARY and
                            not any(rest.startswith(w) for w in NAME_TAIL_WORDS) and
                            not action_tail):
                        continue
                    # 职务/角色词不是人名，且不应再往短截（避免「项目经理」→「项目」）
                    if name in NAME_LABELS or name in ORG_STOPWORDS or any(w in name for w in ROLE_WORDS):
                        break
                    # 常见非人名词（商务/权利/解除 等），即便首字是常见姓氏也不算人名
                    if name in NAME_STOPWORDS:
                        break
                    surname_hit = name[0] in SURNAMES or name[:2] in COMPOUND_SURNAMES
                    if not surname_hit:
                        break  # 姓氏不命中就别再短截了，通常是机构名或普通词
                    conf = 0.78
                    if not strict:
                        conf -= 0.08
                    if k == 0:
                        reasons = [f"标签「{label_key or label}」后出现中文人名", "姓氏命中常见姓氏表"]
                    else:
                        reasons = [f"标签「{label_key or label}」后顿号并列的第 {k + 1} 位姓名", "姓氏命中常见姓氏表"]
                    if cue and re.search(r"身份证|证件号|手机号|联系电话|住址|联系方式", text[rpos + L:rpos + L + 30]):
                        conf += 0.1
                        reasons.append("后续紧跟证件/联系方式")
                    if not strict and not cue and not strong_noncolon_label:
                        # 无冒号引导、又无证件/联系方式佐证 → 大概率是普通行文，降级处理
                        conf = min(conf, self.min_conf_no_surname)
                        reasons.append("无冒号引导且缺少佐证，降级")
                    out.append(RawMatch("PERSON_NAME", rpos, rpos + L, min(conf, 0.97), reasons, name,
                                        {"label": label, "surname_hit": surname_hit, "strict": strict}))
                    picked = True
                    break
                if not picked:
                    # 第一个 run 未命中姓氏/被判为普通词 → 整条放弃（与旧行为一致）；
                    # 后续 run 未命中 → 停下，不再往后找（避免跨过非姓名片段误抓）。
                    break
        for start, end, value in _iter_lawyer_name_spans(text):
            if not any(item.start <= start and end <= item.end for item in out):
                out.append(RawMatch("PERSON_NAME", start, end, 0.78,
                                    ["受控律师称谓语境", "姓氏及姓名边界校验"], value,
                                    {"lawyer_context": True}))
        for match in _ACTION_CONTEXT_NAME_RE.finditer(text):
            raw_name = match.group("raw")
            name = re.sub(r"\s+", "", raw_name)
            if (len(name) not in (2, 3, 4) or name in NAME_STOPWORDS or
                    any(word in name for word in ROLE_WORDS)):
                continue
            if not (name[0] in SURNAMES or name[:2] in COMPOUND_SURNAMES):
                continue
            start, end = match.span("raw")
            if any(existing.value == name and existing.start <= end and existing.end >= start
                   for existing in out):
                continue
            out.append(RawMatch(
                "PERSON_NAME", start, end, 0.72,
                ["姓氏命中常见姓氏表", "姓名位于“由……负责/确认”等动作语境"], name,
                {"action_context": True},
            ))
        return out


# 英语争议解决角色标签：长标签在前，避免 Counsel 吃掉 Counsel for the Claimant。
# 仅作受控字段标签，不做通用英文人名识别。
_ENGLISH_PERSON_ROLE_LABELS = (
    "Counsel for the Claimant", "Counsel for the Respondent",
    "Counsel for the Plaintiff", "Counsel for the Defendant",
    "Attorney for the Plaintiff", "Attorney for the Defendant",
    "Attorney for the Claimant", "Attorney for the Respondent",
    "Solicitors for the Claimant", "Solicitors for the Respondent",
    "Solicitor for the Claimant", "Solicitor for the Respondent",
    "Sole Arbitrator", "Presiding Arbitrator", "Tribunal Member",
    "Plaintiff", "Defendant", "Claimant", "Respondent",
    "Petitioner", "Applicant", "Appellant", "Appellee",
    "Arbitrator", "Witness", "Expert", "Instructed by",
    "Of Counsel", "Lead Counsel", "Counsel",
)
_ENGLISH_PERSON_ROLE_LABELS_RE = "|".join(
    r"\b" + re.escape(label) + r"\b" for label in _ENGLISH_PERSON_ROLE_LABELS
)
_ENGLISH_CAPTION_ROLE_LABELS = (
    "Plaintiff", "Defendant", "Claimant", "Respondent",
    "Petitioner", "Applicant", "Appellant", "Witness", "Expert",
    "Arbitrator", "Sole Arbitrator", "Presiding Arbitrator", "Tribunal Member",
)
_ENGLISH_CAPTION_ROLE_RE = "|".join(
    re.escape(label) for label in _ENGLISH_CAPTION_ROLE_LABELS
)
_ENGLISH_NAME_ORG_SUFFIX_RE = re.compile(
    r"(?i)\b(?:limited|ltd\.?|inc\.?|llc|llp|plc|gmbh|corp(?:oration)?\.?|"
    r"b\.v\.|n\.v\.|s\.a\.|pte\s+ltd\.?|pty\s+ltd\.?|company|co\.?,?\s*ltd)\s*$"
)
_ENGLISH_NAME_ORG_ANYWHERE_RE = re.compile(
    r"(?i)\b(?:limited|ltd\.?|inc\.?|llc|llp|plc|gmbh|corp(?:oration)?\.?|"
    r"b\.?v\.?|n\.?v\.?|s\.?a\.?|pte\.?|pty\.?|co\.?,?\s*ltd)\b"
)
_ENGLISH_NAME_ORG_TRUNC_RE = re.compile(
    r"(?i)\b(?:co|corp|inc|ltd|gmbh|bv|nv|pte|pty|llc|llp|plc|sa|ag|ab|oy|aps)\.?$"
)
_ENGLISH_PARTY_NAME_LABELS = {
    "claimant", "respondent", "plaintiff", "defendant", "applicant",
    "petitioner", "appellant", "appellee",
}


def _english_name_has_org_suffix(value: str) -> bool:
    return bool(_ENGLISH_NAME_ORG_SUFFIX_RE.search(value) or
                _ENGLISH_NAME_ORG_ANYWHERE_RE.search(value) or
                _ENGLISH_NAME_ORG_TRUNC_RE.search(value))


def _is_complete_english_company_value(value: str) -> bool:
    """True only when a recognized company span covers the entire role value."""
    candidate = (value or "").strip(" \t,;")
    if not candidate or len(candidate) > 120:
        return False
    return any(start == 0 and end == len(candidate) and kind == "company"
               for start, end, _name, kind in _english_org_candidates(candidate))


class EnglishPersonNameDetector:
    """仅在明确的英文姓名标签或签署行附近识别，避免扫描普通大写词。"""

    type_id = "PERSON_NAME"
    non_person_table_label_re = re.compile(
        r"(?ix)^\s*(?:"
        r"(?:party|seller|buyer|supplier|customer|provider|client|vendor|lessee|lessor)"
        r"(?:\s+[a-z0-9]+)?|"
        r"(?:registered|notice|mailing|delivery|billing|postal|principal|office|service|business)?\s*address|"
        r"(?:registered|principal|business)\s+office|"
        r"(?:telephone|phone|mobile|cell|fax|e-?mail|contact\s+details)|"
        r"(?:date\s+(?:completed|signed|executed)|"
        r"(?:(?:effective|commencement|expiry|expiration|start|end|execution|contract|agreement)\s+)?date)|"
        r"(?:(?:contract|agreement|purchase|transaction)\s+)?(?:value|amount|price|fee|consideration|sum)|"
        r"(?:(?:tax|vat|registration|company\s+registration|account|bank\s+account|iban|swift|routing|ein|ssn)"
        r"(?:\s+(?:number|no\.?|id))?)|"
        r"(?:title|position|capacity|signature|signatory|department)|"
        r"(?:送达地址|联系地址|通讯地址|注册地址|地址|电话|手机|传真|邮箱|电子邮箱|"
        r"生效日期|签署日期|到期日期|日期|合同金额|金额|合同价值|价款|账户|账号|税号|"
        r"登记号|公司名称|主体名称)"
        r")(?:\s*(?:[/／|]\s*[\u4e00-\u9fff（）()]{1,24}|"
        r"\(\s*[\u4e00-\u9fff]{1,16}\s*\)|（\s*[\u4e00-\u9fff]{1,16}\s*）))?"
        r"\s*[:：=|]?\s*$"
    )
    label_re = re.compile(
        r"(?P<signed_label>\bsigned\s+by\b)\s*[:：|]?\s*(?:/s/\s*)?"
        r"|(?P<by_label>^\s*by\s*[:：]\s*(?:/s/\s*)?|^\s*by\s+/s/\s*)"
        r"|(?P<label>\blegal representative\b|\bauthorized signatory\b|"
        r"\bauthorized representative\b|"
        r"\bbusiness owner details\b|\bcompleted by\b|\bsignature\b|"
        r"\b(?:primary|client|procurement|contract|notices) contact (?:name|person|partner|manager|lead)\b|"
        r"\bcontact(?:\s+(?:person|partner|manager|lead))?\b|"
        r"\b(?:primary contact|procurement contact|client contact|contract contact|contact name)\b|"
        r"\b(?:pinyin name|romanized (?:chinese )?name)\b|"
        r"\b(?:(?:chinese|english) name|name)\s*\(\s*pinyin\s*\)|"
        r"\bgeneral counsel\b|\bceo\b|\bcto\b|\bcfo\b|\bdirector\b|"
        r"\battn(?:\.|\b)|\battention\b|\benglish name\b|"
        + _ENGLISH_PERSON_ROLE_LABELS_RE
        + r"|\bname\b)"
        r"(?:\s*(?:[/／|]\s*[\u4e00-\u9fff（）()]{1,24}|"
        r"\(\s*[\u4e00-\u9fff]{1,16}\s*\)|（\s*[\u4e00-\u9fff]{1,16}\s*）))?"
        r"(?P<separator>\s*[:：|]\s*|\s+(?:\w+\s+){0,3}is\s+)?",
        re.IGNORECASE,
    )
    nested_name_label_re = re.compile(
        r"(?i)^\s*(?:(?:english|chinese|pinyin)\s+(?:full\s+)?name|"
        r"romanized(?:\s+chinese)?\s+name|name\s*\(\s*pinyin\s*\))\s*[:：]"
    )
    token_re = re.compile(
        r"[A-Z]\.|[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+|[A-Z]{2,}|"
        r"(?i:\b(?:van|der|de|del|von|da|dos|bin|al)\b)"
    )
    pinyin_token_re = re.compile(
        r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:['’\-][A-Za-zÀ-ÖØ-öø-ÿ]+)*"
    )
    pinyin_labels = {
        "pinyin name", "romanized name", "romanized chinese name",
        "name (pinyin)", "chinese name (pinyin)", "english name (pinyin)",
    }
    stop_words = {
        "AGREEMENT", "COMPANY", "PARTY", "BUYER", "SELLER", "CUSTOMER",
        "SUPPLIER", "CLIENT", "CONTRACT", "SIGNATURE", "TITLE", "DATE",
        "AUTHORIZED", "REPRESENTATIVE", "DIRECTOR", "CFO", "CEO", "CTO",
        "THE", "THIS", "HEREIN", "WITNESS", "FOR", "AND", "AS",
        "CHIEF", "EXECUTIVE", "OFFICER", "PRESIDENT", "SECRETARY",
        "MANAGER", "PARTNER", "TREASURER", "SIGNATORY", "COUNSEL",
    }
    strong_labels = {
        "legal representative", "authorized signatory", "authorized representative",
        "general counsel", "signed by", "by", "name", "contact person",
        "primary contact name", "primary contact person", "client contact name",
        "client contact person", "procurement contact name", "procurement contact person",
        "contract contact name", "contract contact person", "notices contact name",
        "notices contact person",
        "business owner details", "completed by", "signature",
        "contact", "contact partner", "contact manager", "contact lead",
        "primary contact", "procurement contact", "client contact",
        "contract contact", "contact name", "attn", "attn.", "attention",
        "client contact partner", "procurement contact partner", "english name",
        "director", "ceo", "cto", "cfo",
    } | pinyin_labels | {x.casefold() for x in _ENGLISH_PERSON_ROLE_LABELS}

    def _table_name_label(self, label_text: str) -> str:
        normalized = re.sub(r"\s+", " ", label_text).strip()
        match = self.label_re.fullmatch(normalized)
        label = (match.group("label") or "").strip().casefold() if match else ""
        if label in self.strong_labels:
            return label

        owner_label = re.match(
            r"(?is)^business owner details\b(?P<annotation>.*)$", normalized)
        if not owner_label:
            return ""
        annotation = owner_label.group("annotation").strip()
        if not annotation:
            return "business owner details"
        if (annotation.startswith("(") and annotation.endswith(")") and
                re.search(r"(?i)\b(?:insert|enter|provide|capture)\b", annotation) and
                re.search(r"(?i)\bname\b", annotation)):
            return "business owner details"
        return ""

    def run(self, ctx: ScanContext, _include_table_cells: bool = True) -> List[RawMatch]:
        out: List[RawMatch] = []
        lines = list(_iter_text_lines(ctx.text))
        for line_index, (line_start, line) in enumerate(lines):
            for match in self.label_re.finditer(line):
                label = (match.group("label") or "").strip().casefold()
                has_explicit_boundary = any(
                    match.group(key) for key in ("signed_label", "by_label", "separator"))
                if not has_explicit_boundary and label not in {"attn", "attn.", "attention"}:
                    continue
                tail_start = match.end()
                tail = line[tail_start:]
                if (label in _ENGLISH_PARTY_NAME_LABELS and
                        _is_complete_english_company_value(tail)):
                    continue
                if self.nested_name_label_re.match(tail):
                    continue
                candidate_line_start = line_start
                leading = len(tail) - len(tail.lstrip())
                if label in {"attn", "attn.", "attention"} and not match.group("separator"):
                    tail_start += leading
                    tail = tail[leading:]
                    leading = 0
                if leading < len(tail) and tail[leading] in {'"', "'", "“", "‘"}:
                    opening = tail[leading]
                    closing = {'"': '"', "'": "'", "“": "”", "‘": "’"}[opening]
                    close_at = tail.find(closing, leading + 1)
                    if close_at < 0:
                        continue
                    tail_start += leading + 1
                    tail = tail[leading + 1:close_at]
                honorific = re.match(r"(?i)(?:mr|mrs|ms|miss|dr|prof|esq)\.?\s+", tail)
                if honorific:
                    tail_start += honorific.end()
                    tail = tail[honorific.end():]
                boundary = re.search(r"[,;|():：]", tail)
                if boundary:
                    tail = tail[:boundary.start()]
                label = (match.group("label") or "").strip().casefold()
                is_pinyin_label = label in self.pinyin_labels
                token_re = self.pinyin_token_re if is_pinyin_label else self.token_re
                tokens = list(token_re.finditer(tail))
                if len(tokens) < 2 and not line[match.end():].strip() and line_index + 1 < len(lines):
                    candidate_line_start, next_line = lines[line_index + 1]
                    tail_start = len(next_line) - len(next_line.lstrip())
                    tail = next_line[tail_start:]
                    tokens = list(token_re.finditer(tail))
                    if (len(tokens) < 2 or tokens[0].start() != 0 or
                            tokens[-1].end() != len(tail)):
                        continue
                leading_space = len(tail) - len(tail.lstrip())
                if len(tokens) < 2 or tokens[0].start() != leading_space:
                    continue
                chosen = []
                max_tokens = 3 if is_pinyin_label else 4
                for token in tokens[:max_tokens]:
                    word = token.group(0)
                    if word.upper() in self.stop_words:
                        break
                    if chosen and not re.fullmatch(
                            r"[\s\"“”'’]+", tail[chosen[-1].end():token.start()]):
                        break
                    chosen.append(token)
                if len(chosen) < 2:
                    continue
                name = tail[chosen[0].start():chosen[-1].end()].strip()
                if len(name) < 5 or any(w in self.stop_words for w in re.findall(r"[A-Za-z]+", name.upper())):
                    continue
                if _english_name_has_org_suffix(name):
                    continue
                label = (match.group("label") or match.group("signed_label") or
                         match.group("by_label") or "").strip().casefold()
                signed = bool(re.search(r"(?i)/s/|signed by|^\s*by\s*:", line))
                confidence = 0.78 if signed or label in self.strong_labels else 0.48
                start = candidate_line_start + tail_start + chosen[0].start()
                end = candidate_line_start + tail_start + chosen[-1].end()
                out.append(RawMatch(
                    "PERSON_NAME", start, end, confidence,
                    [f"英文姓名位于{match.group('label')}字段"],
                    name, {"script": "latin", "label": label},
                ))
        if _include_table_cells:
            out.extend(self._scan_caption_role_names(ctx))
            out.extend(self._scan_unpunctuated_table_fields(ctx))
        return out

    def _scan_caption_role_names(self, ctx: ScanContext) -> List[RawMatch]:
        """同行 `姓名, 角色`（Emma Example, Claimant）。span 只盖姓名。

        公司后缀不当人名；角色词本身不是姓名。不做通用英文姓名扫描。
        """
        out: List[RawMatch] = []
        pattern = re.compile(
            r"(?P<name>(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+"
            r"(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+){1,3}))"
            r"\s*,\s*(?:" + _ENGLISH_CAPTION_ROLE_RE + r")\b"
        )
        for m in pattern.finditer(ctx.text):
            name = m.group("name").strip()
            if not (5 <= len(name) <= 60):
                continue
            if _english_name_has_org_suffix(name):
                continue
            words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’\-]+", name)
            if not (2 <= len(words) <= 5):
                continue
            if any(w.upper() in self.stop_words for w in words):
                continue
            start, end = m.span("name")
            out.append(RawMatch(
                "PERSON_NAME", start, end, 0.76,
                ["英文姓名位于caption角色前"],
                name, {"script": "latin", "label": "caption_role"},
            ))
        return out

    def _scan_unpunctuated_table_fields(self, ctx: ScanContext) -> List[RawMatch]:
        """Use DOCX row structure to recognize a name in the cell after its label."""
        rows: Dict[Tuple[int, int], List[Tuple[Block, int, int]]] = {}
        table_cells: Dict[int, List[Tuple[int, int, int, Block, int, int]]] = {}
        cursor = 0
        for block in ctx.blocks:
            raw = block.text.rstrip("\n")
            normalized = NormalizedText(raw).norm
            if not normalized:
                cursor += 1
                continue
            start = ctx.text.find(normalized, cursor)
            if start < 0:
                continue
            end = start + len(normalized)
            ref = block.table_ref
            if ref:
                rows.setdefault((ref[0], ref[1]), []).append((block, start, end))
                table_cells.setdefault(ref[0], []).append(
                    (ref[1], ref[2], ref[3], block, start, end))
            cursor = end + 1

        out: List[RawMatch] = []
        for cells in rows.values():
            cells.sort(key=lambda item: (item[0].table_ref[2], item[1]))
            for label_cell, value_cell in zip(cells, cells[1:]):
                label_block, _label_start, _label_end = label_cell
                value_block, value_start, _value_end = value_cell
                if label_block.table_ref[2] >= value_block.table_ref[2]:
                    continue
                label_text = label_block.text.strip()
                if not label_text or re.search(r"[:：|]", label_text):
                    continue
                label = self._table_name_label(label_text)
                if not label:
                    continue

                prefix = label + ": "
                value_text = value_block.text.rstrip("\n")
                synthetic_raw = prefix + value_text
                synthetic_nt = NormalizedText(synthetic_raw)
                prefix_length = len(NormalizedText(prefix).norm)
                value_length = len(NormalizedText(value_text).norm)
                synthetic_ctx = ScanContext(synthetic_nt, synthetic_nt.norm)
                for match in self.run(synthetic_ctx, _include_table_cells=False):
                    if (match.start < prefix_length or
                            match.end > prefix_length + value_length):
                        continue
                    relative_start = match.start - prefix_length
                    relative_end = match.end - prefix_length
                    attrs = dict(match.attrs)
                    attrs["table_field"] = True
                    reasons = list(match.reasons) + ["相邻表格单元格为明确姓名字段"]
                    out.append(RawMatch(
                        match.type_id, value_start + relative_start,
                        value_start + relative_end, match.confidence,
                        reasons, match.value, attrs,
                    ))

        # A label may be a column heading or occupy its own row in signature tables.
        # Follow only the same visual column and stop at the next recognized field label.
        for table_id, cells in table_cells.items():
            cells.sort(key=lambda item: (item[0], item[1], item[4]))
            for label_row, label_col, label_span, label_block, _start, _end in cells:
                label_text = label_block.text.strip()
                label = self._table_name_label(label_text)
                if not label:
                    continue
                for distance in range(1, 4):
                    target_row = label_row + distance
                    candidates = [item for item in cells
                                  if item[0] == target_row and
                                  item[1] < label_col + label_span and
                                  label_col < item[1] + max(1, item[2])]
                    if not candidates:
                        continue
                    candidates.sort(key=lambda item: (item[1], item[4]))
                    value_row_blocked = False
                    for _row, _col, _span, value_block, value_start, _value_end in candidates:
                        value_text = value_block.text.strip()
                        if not value_text:
                            continue
                        if (self._table_name_label(value_text) or
                                self.non_person_table_label_re.fullmatch(value_text)):
                            value_row_blocked = True
                            break
                        synthetic = NormalizedText(label + ": " + value_text)
                        prefix = len(NormalizedText(label + ": ").norm)
                        value_length = len(NormalizedText(value_text).norm)
                        synthetic_ctx = ScanContext(synthetic, synthetic.norm)
                        for match in self.run(synthetic_ctx, _include_table_cells=False):
                            if match.start < prefix or match.end > prefix + value_length:
                                continue
                            relative_start = match.start - prefix
                            relative_end = match.end - prefix
                            attrs = dict(match.attrs)
                            attrs["table_field"] = True
                            out.append(RawMatch(
                                match.type_id, value_start + relative_start,
                                value_start + relative_end, match.confidence,
                                list(match.reasons) + ["同一表格列的姓名标签对应值"],
                                match.value, attrs,
                            ))
                        break
                    if value_row_blocked:
                        break
        return out


class DisputePersonNameDetector:
    """英语争议解决文书的补充姓名识别。

    角色标签（Claimant: Name）与同行 caption（Name, Claimant）已归
    EnglishPersonNameDetector，本类只做三样，且限案件标题区：
      1. 多行 caption（姓名行 + 下一行角色行）
      2. A v. B / A v B
      3. BETWEEN 编号当事人块
    公司后缀、角色词本身、普通行文不得命中。不与 EnglishPersonNameDetector 重复扫 Role: 标签。
    """

    type_id = "PERSON_NAME"

    token_re = re.compile(r"[A-Z]\.|[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+")
    honorific_re = re.compile(
        r"(?i)^(?:mr|mrs|ms|miss|dr|prof|esq|"
        r"the\s+hon(?:ourable|orable)?\.?|lord|lady|justice|judge)\.?\s+"
    )
    role_line_re = re.compile(
        r"(?i)^\s*(?:the\s+)?"
        r"(?P<role>"
        r"plaintiffs?|defendants?|claimants?|respondents?|appellants?|appellees?|"
        r"petitioners?|applicants?|arbitrators?|sole\s+arbitrator|"
        r"presiding\s+arbitrator|umpire|witnesses?|experts?|expert\s+witness|"
        r"deponents?|affiants?|counsel)"
        r"\s*,?\s*$"
    )
    vs_re = re.compile(
        r"(?<![A-Za-z0-9])"
        r"(?P<left>[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+){0,3})"
        r"\s+v(?:s)?\.\s+"
        r"(?P<right>[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+){0,3})"
        r"(?![A-Za-z0-9])"
    )
    between_re = re.compile(r"(?im)^\s*between\s*:?\s*$")
    numbered_party_re = re.compile(
        r"(?m)^\s*[（(]?\s*\d{1,2}\s*[)）]?\s+"
        r"(?P<name>[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+){0,4})"
        r"\s*,?\s*$"
    )
    stop = {
        "THE", "THIS", "THAT", "AND", "OR", "OF", "IN", "ON", "AT", "TO",
        "FOR", "BY", "WITH", "AS", "IS", "ARE", "WAS", "WILL", "SHALL",
        "PLAINTIFF", "DEFENDANT", "CLAIMANT", "RESPONDENT", "APPELLANT",
        "APPELLEE", "PETITIONER", "APPLICANT", "COUNSEL", "WITNESS",
        "EXPERT", "ARBITRATOR", "TRIBUNAL", "COURT", "CASE", "ACTION",
        "AGREEMENT", "CONTRACT", "PARTY", "PARTIES", "BETWEEN", "WHEREAS",
        "NOW", "THEREFORE", "HEREBY", "WITNESSETH", "VERSUS",
    }

    def _clean_name(self, raw: str) -> str:
        raw = (raw or "").strip().strip(",").strip()
        if not (5 <= len(raw) <= 60):
            return ""
        if _english_name_has_org_suffix(raw):
            return ""
        if any(ch in raw for ch in "&@#$") or "http" in raw.lower():
            return ""
        core = self.honorific_re.sub("", raw, count=1)
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’\-]+", core)
        if not (2 <= len(words) <= 5):
            return ""
        for w in words:
            if w.upper() in self.stop or w.lower() in {
                    "plaintiff", "defendant", "claimant", "respondent",
                    "appellant", "appellee", "petitioner", "applicant",
                    "arbitrator", "umpire", "witness", "expert",
                    "deponent", "affiant", "counsel"}:
                return ""
        return core

    def _scan_caption(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        lines = list(_iter_text_lines(ctx.text))
        for i, (_start, line) in enumerate(lines):
            if not self.role_line_re.match(line):
                continue
            for j in range(i - 1, max(-1, i - 4), -1):
                prev_start, prev_line = lines[j]
                name = self._clean_name(prev_line)
                if not name:
                    break
                pos = prev_line.find(name)
                if pos < 0:
                    break
                out.append(RawMatch(
                    "PERSON_NAME", prev_start + pos, prev_start + pos + len(name),
                    0.74, ["英文姓名位于caption角色行上方"], name,
                    {"script": "latin", "label": "caption"},
                ))
                break
        return out

    def _emit_vs_side(self, span: str, offset: int, reason: str) -> List[RawMatch]:
        name = self._clean_name(span)
        if not name:
            return []
        rel = span.find(name)
        if rel < 0:
            return []
        start = offset + rel
        return [RawMatch(
            "PERSON_NAME", start, start + len(name), 0.70,
            [reason], name, {"script": "latin", "label": "caption_vs"},
        )]

    def _scan_vs(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        for m in self.vs_re.finditer(ctx.text):
            out.extend(self._emit_vs_side(
                m.group("left"), m.start("left"),
                "英文姓名位于 v. 对抗式左侧"))
            out.extend(self._emit_vs_side(
                m.group("right"), m.start("right"),
                "英文姓名位于 v. 对抗式右侧"))
        return out

    def _scan_between(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        lines = list(_iter_text_lines(ctx.text))
        remain = 0
        for start, line in lines:
            if self.between_re.match(line):
                remain = 12
                continue
            if remain <= 0:
                continue
            remain -= 1
            m = self.numbered_party_re.match(line)
            if not m:
                continue
            name = self._clean_name(m.group("name"))
            if not name:
                continue
            pos = line.find(name)
            if pos < 0:
                continue
            out.append(RawMatch(
                "PERSON_NAME", start + pos, start + pos + len(name),
                0.70, ["英文姓名位于BETWEEN当事人块"], name,
                {"script": "latin", "label": "between"},
            ))
        return out

    def _in_title_zone(self, ctx: ScanContext, pos: int) -> bool:
        """agent3：v./BETWEEN/多行 caption 只认案件标题区，不扫正文。"""
        if pos > 2500:
            return False
        head = ctx.text[max(0, pos - 800):pos + 80]
        if re.search(
                r"(?i)\b(?:IN\s+THE|BETWEEN|COURT|CLAIM\s+FORM|COMPLAINT)\b",
                head):
            return True
        line_start = ctx.text.rfind("\n", 0, pos) + 1
        line_end = ctx.text.find("\n", pos)
        if line_end < 0:
            line_end = len(ctx.text)
        return (line_end - line_start) <= 80

    def run(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        for m in self._scan_caption(ctx):
            if self._in_title_zone(ctx, m.start):
                out.append(m)
        for m in self._scan_vs(ctx):
            if self._in_title_zone(ctx, m.start):
                out.append(m)
        for m in self._scan_between(ctx):
            if self._in_title_zone(ctx, m.start):
                out.append(m)
        seen = set()
        uniq: List[RawMatch] = []
        for m in out:
            key = (m.start, m.end, m.value)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(m)
        return uniq


class EnglishAddressDetector:
    """在英文地址标签后定位同一行地址，并要求街道、门牌或邮编结构证据。"""

    type_id = "ADDRESS"
    label_re = re.compile(
        r"(?i)\b(?P<label>registered office|registered address|principal office|"
        r"principal place of business|notice address|address for notices|mailing address|"
        r"delivery address|billing address|postal address|"
        r"address for service|address for service of process|address for delivery|service address|"
        r"service of process address|counsel address|law firm address|"
        r"counsel(?:'s|\u2019s)?\s+chambers|chambers|"
        r"address)\b"
        r"\s*(?:[:：=|]\s*|\s+|(?=\s*$))"
    )
    address_evidence = re.compile(
        r"(?i)(?:\b\d{1,6}\s+[A-Z0-9.'’\- ]{2,50}\b(?:street|st\.?|road|rd\.?|"
        r"avenue|ave\.?|boulevard|blvd\.?|lane|ln\.?|drive|dr\.?|way|court|ct\.?|"
        r"plaza|highway|hwy\.?|parkway|pkwy\.?|place|square|sq\.?)\b|"
        r"\bP\.?\s*O\.?\s+Box\s+\d+\b|\b(?:Suite|Ste\.?|Floor|Fl\.?|Unit|Apt\.?|"
        r"Apartment|Room)\s+[A-Z0-9-]+\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|"
        r"\b\d{5}(?:-\d{4})?\b)")

    def run(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = self._scan_table_fields(ctx)
        lines = list(_iter_text_lines(ctx.text))
        for line_index, (line_start, line) in enumerate(lines):
            for match in self.label_re.finditer(line):
                start = match.end()
                end = len(line)
                for stop in re.finditer(r"[;\t|]", line[start:]):
                    end = min(end, start + stop.start())
                    break
                value = line[start:end].strip(" \t,;.")
                value_line_start = line_start
                value_line = line
                value_search_start = start
                if (not value and not line[start:].strip() and line_index + 1 < len(lines)):
                    next_start, next_line = lines[line_index + 1]
                    next_value = next_line.strip(" \t,;.")
                    if len(next_value) >= 8 and self.address_evidence.search(next_value):
                        value = next_value
                        value_line_start = next_start
                        value_line = next_line
                        value_search_start = 0
                if len(value) < 8 or not self.address_evidence.search(value):
                    continue
                # Keep address fields on their source line; do not absorb a following sentence.
                for sentence_end in re.finditer(r"[.!?]\s+(?=[A-Z])", value):
                    prefix = value[:sentence_end.start() + 1]
                    if re.search(
                            r"(?i)(?:\b(?:p|o|st|rd|ave|blvd|ln|dr|ct|pl|hwy|pkwy|"
                            r"fl|ste|apt|no)|\bp\.o)\.$", prefix):
                        continue
                    value = value[:sentence_end.start()].rstrip()
                    break
                offset = value_line.find(value, value_search_start)
                if offset < value_search_start:
                    continue
                conf = 0.78 if re.search(r"\b\d{5}(?:-\d{4})?\b|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", value, re.I) else 0.58
                out.append(RawMatch(
                    "ADDRESS", value_line_start + offset, value_line_start + offset + len(value), conf,
                    [f"英文地址位于{match.group('label')}字段"],
                    value, {"script": "latin", "label": match.group("label").casefold()},
                ))

        # Unlabeled addresses are considered only inside an identifiable party block.
        street_start = re.compile(
            r"(?i)^\s*(?:\d{1,6}\s+[A-Z0-9.'’\- ]{2,50}\b"
            r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?|"
            r"lane|ln\.?|drive|dr\.?|way|court|ct\.?|plaza|highway|hwy\.?|"
            r"parkway|pkwy\.?|place|square|sq\.?)\b|P\.?\s*O\.?\s+Box\s+\d+\b)"
        )
        party_heading = re.compile(
            r"(?i)\b(?:party\s+[a-z0-9]+|seller|buyer|service provider|customer|"
            r"contracting party|party details|party information)\b"
        )
        org_line = re.compile(r"(?:" + EN_ORG_RE.pattern + r")")
        for line_index, (line_start, line) in enumerate(lines):
            candidate = line.strip(" \t,;")
            if not street_start.search(candidate) or not self.address_evidence.search(candidate):
                continue
            prior_lines = [item[1].strip() for item in lines[max(0, line_index - 3):line_index]]
            prior_text = "\n".join(prior_lines)
            if re.search(r"(?i)\b(?:delivery|service|performance|project|site|venue)\s+(?:address|location|site)\b",
                         prior_text):
                continue
            has_party_context = bool(party_heading.search(prior_text))
            if not has_party_context:
                has_party_context = any(EN_ORG_RE.fullmatch(value) or
                                        EN_ORG_ASSOC_RE.fullmatch(value)
                                        for value in prior_lines)
            if not has_party_context:
                continue
            end_line_index = line_index
            value = candidate
            if line_index + 1 < len(lines):
                next_start, next_line = lines[line_index + 1]
                next_value = next_line.strip(" \t,;")
                if (re.search(r"\b\d{5}(?:-\d{4})?\b|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b",
                              next_value, re.I) and
                        not re.match(r"(?i)^\s*(?:telephone|phone|email|contact)\b", next_value)):
                    value = ctx.text[line_start:next_start + len(next_line)].strip()
                    end_line_index = line_index + 1
            value_start = line_start + len(line) - len(line.lstrip(" \t"))
            value_end = lines[end_line_index][0] + len(lines[end_line_index][1].rstrip("\r\n \t"))
            if any(row.start < value_end and row.end > value_start for row in out):
                continue
            value = ctx.text[value_start:value_end]
            out.append(RawMatch(
                "ADDRESS", value_start, value_end, 0.62,
                ["主体信息块内由门牌、道路和邮政编码结构识别；无地址标签，需人工核对"],
                value, {"script": "latin", "unlabeled_party_address": True},
            ))
        return out

    def _scan_table_fields(self, ctx: ScanContext) -> List[RawMatch]:
        """DOCX 表格：标签格同行右侧第一个满足 address_evidence 的值格。"""
        out: List[RawMatch] = []
        rows: Dict[Tuple[int, int], List[Tuple[Block, int, int, int]]] = {}
        cursor = 0
        for block in ctx.blocks:
            raw = block.text.rstrip("\n")
            if not raw.strip():
                cursor += 1
                continue
            normalized = NormalizedText(raw).norm
            if not normalized:
                cursor += 1
                continue
            start = ctx.text.find(normalized, cursor)
            if start < 0:
                continue
            end = start + len(normalized)
            cursor = end + 1
            ref = block.table_ref
            if not ref:
                continue
            # ref = (表序号, 行序号, grid 起始列, 跨列数)
            rows.setdefault((ref[0], ref[1]), []).append(
                (block, start, end, ref[2]))
        for cells in rows.values():
            cells_sorted = sorted(cells, key=lambda x: x[3])
            for i, (blk, start, end, col) in enumerate(cells_sorted):
                label_text = blk.text.strip()
                if not self.label_re.search(label_text):
                    continue
                for right in cells_sorted[i + 1:]:
                    value = right[0].text.strip(" \t,;.")
                    if len(value) >= 8 and self.address_evidence.search(value):
                        out.append(RawMatch(
                            "ADDRESS", right[1], right[2], 0.72,
                            ["英文地址位于表格送达字段右侧"], value,
                            {"script": "latin", "label": "table"},
                        ))
                        break
        return out


# ------------------------------------------------------------------------------
# 4.3 地址检测器（标签引导 + 行政区划分量校验）
# ------------------------------------------------------------------------------

ADDR_LABELS = ("通讯地址", "通信地址", "联系地址", "详细地址", "送达地址", "注册地址", "住所地",
               "住所", "住址", "地址", "经营场所", "所在地", "注册地", "办公地址", "邮寄地址",
               # 工程建设 / 服务类合同常用的地点字段名（「工程地点：上海市……」这类标签路径）：
               "工程地点", "施工地点", "项目地点", "服务地点", "履行地点", "交付地点",
               "交货地点", "履约地点", "项目地址", "施工地址", "办公场所", "经营地址", "户籍地", "户籍地址", "户口所在地", "现住址", "现居住地")
ADDR_LABELS = tuple(sorted(ADDR_LABELS, key=len, reverse=True))
# 标签后可以是 冒号 / 等号 / 换行 / 表格竖线 / 空格制表（合同表格常把标签和值分两列或两行）。
#   strong 组：冒号/等号/换行/竖线——强语法，允许值本身较短；
#   非 strong（仅空格/制表）：弱语法，要求值里必须出现行政区划或门牌要素，避免误吞整句话。
ADDR_LABEL_RE = re.compile(
    "(?:" + "|".join(ADDR_LABELS) + r")\s*"
    r"(?:(?P<addr_strong>[：:＝=])\s*|[为是]\s*|\n\s*|" +
    re.escape(_PDF_CELL_BOUNDARY) + r"+\s*|[|｜]\s*|[ \t]+(?=\S)|"
    r"(?=[\u4e00-\u9fff]{2,12}(?:省|自治区|市|县|区|镇|乡|村)))"
)
ADDR_STOP = "，。；、,;；\n\t（）()【】[]：:|" + _PDF_CELL_BOUNDARY
ADDR_WRAP_PREFIX_RE = re.compile(
    r"(?:路|街|道|巷|弄|大道|园区)[0-9零一二三四五六七八九十百\-]{0,12}"
    r"(?:号楼|号|栋|幢)?$"
)
ADDR_WRAP_SUFFIX_RE = re.compile(
    r"^[0-9零一二三四五六七八九十百]{1,8}(?:号楼|号|栋|幢|楼|单元|座|室|层)"
)
# 地址后面常紧跟下一个字段名，遇到就截断，避免把整行吞进来
ADDR_NEXT_FIELD = ("邮编", "邮政编码", "电话", "手机", "邮箱", "电子邮箱", "传真",
                   "联系人", "开户", "账号", "帐号", "税号", "网址", "法定代表人",
                   "户籍地", "户籍地址", "住所地", "现住址", "姓名", "原告", "被告")
ADDR_COMPONENTS = ("省", "自治区", "市", "区", "县", "旗", "镇", "乡", "村", "路", "街", "道",
                   "巷", "弄", "号", "栋", "幢", "楼", "单元", "室", "层", "号楼", "大院", "园区")
# 地址值两端的「填空线」字符（半角/全角下划线 + 各类空白）。
# 它们属于「待填位置」的版式，不属于地址本身——替换时若一并覆盖，
# 落款那一行的填空线会消失、版面被破坏。注意**不能**并入 ADDR_STOP：
# 标签后紧跟的 '_' 会令向后扫描在起点处立即截断，导致整条地址漏检。
# 正确做法是「判据用剥前的值（识别行为不变），只把替换区间收紧」。
ADDR_FILLER_CHARS = "_\uff3f \t\u3000\u00a0\u201c\u201d\u300c\u300d\u300e\u300f\"'"


def _trim_addr_filler(text: str, s: int, e: int) -> Tuple[int, int]:
    """把值两端属于填空线的字符排除在替换区间之外（不改变识别判据）。"""
    while s < e and text[s] in ADDR_FILLER_CHARS:
        s += 1
    while e > s and text[e - 1] in ADDR_FILLER_CHARS:
        e -= 1
    return s, e


class AddressDetector:
    type_id = "ADDRESS"

    def run(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        text = ctx.text
        for m in ADDR_LABEL_RE.finditer(text):
            start = m.end()
            end = start
            wrapped_lines = 0
            while end < len(text):
                if text[end] in "\r\n":
                    prefix = re.sub(r"\s+", "", text[start:end])
                    line_break_length = 2 if text.startswith("\r\n", end) else 1
                    following = text[end + line_break_length:]
                    continuation = following.lstrip(" \t")
                    administrative_wrap = (
                        re.search(r"(?:省|自治区|市|区|县|旗|镇|乡|村)$", prefix) and
                        re.match(r"[\u4e00-\u9fff]{1,10}(?:市|区|县|旗|镇|乡|村|路|街|道|巷|弄)", continuation) and
                        not any(continuation.startswith(k) for k in ADDR_NEXT_FIELD))
                    if (wrapped_lines < 2 and (administrative_wrap or (
                            ADDR_WRAP_PREFIX_RE.search(prefix) and
                            ADDR_WRAP_SUFFIX_RE.match(continuation)))):
                        end += line_break_length
                        wrapped_lines += 1
                        continue
                    break
                if text[end] in ADDR_STOP:
                    break
                if end > start + 4 and any(text.startswith(k, end) for k in ADDR_NEXT_FIELD):
                    break
                end += 1
            value = text[start:end].strip()
            if len(value) < 4:
                continue
            # 纯数字/纯 ASCII（如「服务器地址：192.168.10.24」）不是住址
            if not re.search(r"[\u4e00-\u9fff]", value):
                continue
            comps = sum(1 for c in ADDR_COMPONENTS if c in value)
            has_street = bool(re.search(r"[路街巷道号栋幢楼室层]", value))
            # 长度规则：≥6 字照收；4~5 字只在同时满足「≥2 个要素 + 含街道/门牌类字」时收，
            # 以免把「地址：见附件」这类无关指令当成地址。
            if len(value) < 6 and not (comps >= 2 and has_street):
                continue
            # 弱分隔符（只有空格/制表）时，值里必须真的含地址要素
            if m.group("addr_strong") is None and comps == 0:
                continue
            conf = 0.55 + 0.1 * min(comps, 3)
            reasons = [f"地址标签「{m.group(0).strip()}」引导", f"含 {comps} 个行政区划/门牌要素"]
            if re.search(r"\d", value):
                conf += 0.08
                reasons.append("含门牌数字")
            # 判据一律基于剥前的 value（识别行为完全不变），
            # 只把「替换区间」收紧到不含两端填空线，保住落款版式。
            s2, e2 = _trim_addr_filler(text, start, end)
            out.append(RawMatch("ADDRESS", s2, e2, min(conf, 0.95), reasons, text[s2:e2],
                                {"components": comps}))

        # 无标签的显式行政区划串（省/市/区 + 路/街/号）
        # 两条并列前缀（覆盖直辖市与不写省名的地级市裸地址）：
        #            · 省/自治区 + 市/自治州/地区/盟
        #            · 任意「市 + 区/县」（覆盖直辖市与「深圳市南山区」这类写法）
        bare = re.compile(
            r"(?:"
            r"[\u4e00-\u9fff]{2,7}(?:省|自治区)[\u4e00-\u9fff]{2,10}(?:市|自治州|地区|盟)"
            r"|[\u4e00-\u9fff]{2,8}市[\u4e00-\u9fff]{2,10}(?:区|县)"
            r")"
            # 此处不放 `[\u4e00-\u9fff]{2,20}?` 一类的惰性段：它与下一行
            # `{2,30}?` 叠加会把「路名前至少 4 个汉字」变成硬约束，「示例路」
            # 这类 3 字路名会整条失配。中间层级（「科技园中区」）由 `{2,30}?`
            # 的惰性展开负责吃掉。
            r"(?:[\u4e00-\u9fff]{1,12}(?:区|县|市|旗))?"
            r"[\u4e00-\u9fff0-9]{2,30}?(?:路|街|道|巷|弄|大道|园区|工业区)"
            # Visible tracked deletions can wrap a bare address before its house number.
            r"(?:\n(?=[0-9零一二三四五六七八九十百]{1,8}(?:号楼|号|栋|幢|楼|单元|座|室|层)))?"
            r"[\u4e00-\u9fff0-9\-]{0,20}?(?:号楼|号|栋|幢|室|层)"
            r"(?:[0-9A-Za-z零一二三四五六七八九十百\-]{1,8}(?:号楼|号|栋|幢|楼|单元|座|室|层)){0,4}"
        )

        def _trim_left(pattern, s: int, e: int, span: int = 8) -> int:
            """向左修剪：把「项目位于上海市……」里叙述性的地名前缀剥掉。

            从 s+1 起逐个字符试算，只要仍能匹配到同一结尾就继续右移，
            第一个失败的位置即行政区划名的真实起点。避免把「项目位于」一起脱敏。
            """
            lo = s
            for i in range(s + 1, min(s + span, e)):
                m2 = pattern.match(text, i)
                if m2 and m2.end() == e:
                    # 切点左侧紧邻行政区划名时**不采纳**该切点，否则会把
                    # 「上海市…」切成「市…」，地名前两字以明文留在正文（真泄漏）。
                    if not _preceded_by_region_prefix(text, 0, i):
                        lo = i
                else:
                    break
            return lo

        for m in bare.finditer(text):
            s = _trim_left(bare, m.start(), m.end())
            address_end = m.end()
            tail_components = re.match(
                r"(?:(?:[东南西北中]区\d{1,4}号)|"
                r"(?:[A-Za-z0-9零一二三四五六七八九十百\-]{1,8}"
                r"(?:号楼|栋|幢|楼|单元|座|室|层))){1,8}",
                text[address_end:],
            )
            if tail_components:
                address_end += tail_components.end()
            value = text[s:address_end]
            conf = 0.6 + 0.05 * sum(1 for c in ADDR_COMPONENTS if c in value)
            out.append(RawMatch("ADDRESS", s, address_end, min(conf, 0.92),
                                ["显式行政区划+门牌结构"], value))

        # 只到「区/县」一级、没有门牌的裸地址（如「上海市浦东新区」）。
        # 边界收紧（匹配后必须紧跟空白/标点/行尾，不能仍是中文），
        # 以免把「北京市海淀区人民法院」这类机构名当成地址。
        bare_admin = re.compile(
            r"(?:[\u4e00-\u9fff]{2,7}(?:省|自治区))?"
            r"[\u4e00-\u9fff]{2,8}市"
            r"[\u4e00-\u9fff]{2,10}(?:区|县)"
            r"(?=[\s，。；、,;:：|｜\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\uff08\uff09()\u3010\u3011\[\]]|$)"
        )
        for m in bare_admin.finditer(text):
            s = _trim_left(bare_admin, m.start(), m.end())
            value = text[s:m.end()]
            if len(value) < 5:
                continue
            out.append(RawMatch("ADDRESS", s, m.end(), 0.6,
                                ["显式行政区划（区/县级，无门牌）"], value))
        return out


# ------------------------------------------------------------------------------
# 4.4 组织机构名检测器
# ------------------------------------------------------------------------------

ORG_BODY_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9（）()·&.\-'\"]")
# 左边界字：向左扩张时，遇到这些字就在此处切断（避免把「本合同由XX公司」整段吞进来）
ORG_LEFT_BOUNDARY = set("由与和或给被把以并为在及至向从对等第是为向：:，,。;；、（）[]{}<>《》\"' \t\n")
# 机构名左扩张使用的「多字虚词 / 角色短语」——其**结束位置**也作为候选切点。
# 在「取最靠右切点」的规则下，单字边界字会漏掉更靠右的短语切点：
#   「被告泽远技术…」       → 候选 {被后, 被告后}，取「被告」之后 → 泽远技术…（不再残留「被」）
#   「…建设的位于宜宾志远…」→ 取「位于」之后 → 宜宾志远…（不再吞掉 8 个字的正文）
ORG_LEFT_PHRASES: Tuple[str, ...] = (
    "原告", "被告", "上诉人", "被上诉人", "申请人", "被申请人", "再审申请人",
    "第三人", "委托代理人", "法定代表人", "法定代理人", "被执行人",
    "位于", "坐落于", "地址为", "名称为",
)

# 机构名前残留的指代词/量词（按 2 字一组，便于一次性剔除「本人」「该公」「号院」等错误前缀）
ORG_LEAD_PRONOUNS = ("本人", "该公司", "该司", "该行", "该店", "该厂", "该社", "该院", "该中心",
                     "其本", "我方", "你方", "对方", "本行", "本院", "本店", "本厂", "本社", "本中心",
                     "号院", "号楼", "号室", "号层", "号店", "号厂", "号楼", "号区")

# 机构名候选「字号部分」（body = 候选剔除末尾后缀）的反语境词。
# 背景：后缀锚点 + 全汉字左扩张会把叙述整段吞进来（`部分分公司`、`故建议分公司`、
#       `最终报送人民银行`），而唯一的长度门槛 len(value)>=5 又把后缀自身算进去了
#       （`分公司` 白送 3 字、`银行`/`分行` 白送 2 字），于是「左侧任意 2–3 汉字 + 后缀」
#       即被判为机构名（conf 0.88，永不掉 min_confidence=0.5 门槛）。
# 判据：候选的 body 中出现反语境词即拒绝。
# ⚠️ 词表刻意**只收多字词**，并刻意排除以下高风险项：
#     ① 单字虚词（各/该/由/向/归）——它们会被 `ORG_LEFT_BOUNDARY` 截断或被「剥前缀」规则
#        剥掉，残留在紧邻位置反而会误杀真名（如「本合同由北京宏远…有限公司」的「由」）；
#        且这些单字场景本就被 len>=5 门槛挡住（`各分公司` 4 字、`由分公司` 3 字），收录无收益。
#     ② 「上报」「提交」——「上海报业」含子串「上报」，收进来会误杀真机构名。
# 故本表宁可小：可回归、可扩展。新增词必须逐词跑 tests/test_org_false_positive.py。
ORG_ANTI_CONTEXT = (
    "部分", "根据", "建议", "采用", "报送",
    "全部", "所有", "其中", "下属",
)

# 公权力机关 / 监管机构通用名——不是合同当事人，不含可识别的当事人信息，一律不脱敏。
# 用户 2026-09-30 口径：`人民银行` 及其同义写法 `中国人民银行` / `人行` / `央行`。
# ⚠️ 按「**尾匹配**」判定，而非并入 `ORG_STOPWORDS`：后者是**精确匹配**（`value in ORG_STOPWORDS`），
#    拦不住被左扩张吞长的误报值（如 `最终报送人民银行` ≠ `人民银行`）。
# ⚠️ `人行` / `央行` 本身不含机构后缀，`ORG_SUFFIX_RE` 不会锚定它们，登记属**防御性**
#    （防将来新增机构后缀规则时回归）。
# 取舍：若合同主体确为某人民银行分支机构（如「中国人民银行××市中心支行」），本名单会将其
#      漏检 —— 与 R7 同口径的「宁漏不错」，可由复核页「添加漏识别的字段」手动补录兜底。
ORG_NONPARTY_TAILS = (
    "中国人民银行", "人民银行",
    "人行", "央行",
)

# 仅排除完整的产品/服务通称，不以“含银行”为由排除真实机构名称。
ORG_GENERIC_NON_ENTITIES = frozenset({
    "企业网上银行", "企业网银", "网上银行", "网上銀行", "手机银行", "手機銀行", "电子银行", "電子銀行",
})
ORG_GENERIC_BRANCH_REFERENCE_RE = re.compile(
    r"^(?:(?:不|未|无需|不得)?(?:包括|包含|含有|含|例如|以及|通知|告知|适用|涉及|涵盖|覆盖))"
    r"(?:各|所有|全部|相关|有关|下属|所属|各级|任何)?$"
)
ORG_BANK_DAY_COUNT_BEFORE_BANK = re.compile(
    r"(?:\d+|[零一二两三四五六七八九十]+)(?:\s*个)?\s*$"
)
ORG_BANK_WORKDAY_AFTER_BANK = re.compile(r"^\s*工作日")


def _org_is_bank_workday_phrase(text: str, bank_start: int, bank_end: int) -> bool:
    """只排除数量表达 + 银行 + 工作日构成的时限短语。"""
    return (
        ORG_BANK_DAY_COUNT_BEFORE_BANK.search(text[:bank_start]) is not None
        and ORG_BANK_WORKDAY_AFTER_BANK.match(text[bank_end:]) is not None
    )


def _org_short_candidate_length_allowed(value: str, suffix: str) -> bool:
    return len(value) >= 5 or (len(value) == 4 and suffix in ORG_SHORT_SUFFIXES)


def _org_is_short_party_label(text: str, start: int) -> bool:
    before = text[max(0, start - 32):start]
    before = re.split(r"[\r\n。！？；!?;]", before)[-1]
    return ORG_SHORT_PARTY_LABEL_RE.search(before) is not None


def _org_has_span_field_label(text: str, start: int) -> bool:
    before = text[max(0, start - 30):start]
    before = re.split(r"[\r\n。！？；!?;]", before)[-1]
    return ORG_SPAN_FIELD_LABEL_RE.search(before) is not None


ORG_SPAN_GENERIC_SUFFIXES = frozenset({
    "银行", "支行", "分行", "分公司", "子公司", "营业部", "医院", "大学", "学院", "研究院", "中心",
})
ORG_SPAN_REVIEW_REASON = "机构后缀前存在未确认文本，候选跨度需人工核对"


def _org_span_needs_review(text: str, start: int, value: str, suffix: str) -> bool:
    if suffix not in ORG_SPAN_GENERIC_SUFFIXES:
        return False
    body = value[:-len(suffix)]
    if _org_is_short_party_label(text, start) or _org_has_span_field_label(text, start):
        return False
    if suffix in {"分公司", "子公司"} and _org_region_anchor(body) == 0:
        return False
    nested_suffixes = set(ORG_SUFFIX_STRONG) | set(ORG_SUFFIX_WEAK) | {"公司"}
    if any(nested in body for nested in nested_suffixes):
        return False
    return True


def _org_is_short_entity_action(text: str, end: int) -> bool:
    after = text[end:end + 32]
    after = re.split(r"[\r\n。！？；!?;]", after)[0]
    return ORG_SHORT_ACTION_RE.match(after) is not None


def _org_is_generic_branch_reference(body: str) -> bool:
    """识别“谓词/范围词 + 泛指分支类别”，不把它当成专名。"""
    normalized = re.sub(r"[\s\u3000，,、:：]+", "", body)
    return ORG_GENERIC_BRANCH_REFERENCE_RE.fullmatch(normalized) is not None


def _org_has_only_enumeration_prefix(body: str) -> bool:
    """序号加通用机构类别不是机构专名。"""
    return ORG_ENUMERATION_ONLY_RE.fullmatch(body) is not None


# 机构名「叙述前缀」结构性剥离。
# 背景：反语境词黑名单列举**无法穷举** —— 漏挡 `进一步规范` / `促进` /
#       `通过审阅` / `通过调阅`，叙述被整段吞进候选（conf 0.88，必进复核页），
#       替换后正文变成「为[组织机构名称#N]反洗钱工作」，**语义被改写**。
# 判据（结构性，四个条件**全部满足**才重切）：
#   ① body 里有行政区划名，且**不在开头**（p > 0）；
#   ② 地名**之前**的片段不含机构后缀（左侧不是另一个机构名，而是叙述）；
#   ③ 地名**之前**的片段不含括号（括号内地名属字号，如 `宏远档案管理（上海）有限公司`）；
#   ④ 地名**之前**的片段含叙述词（严护栏；否则视为字号，不重切）。★ 安全关键
# 动作：把候选**右对齐重切**到该地名处 —— 只切掉前面的叙述，**不拒绝**，真名保留。
#   为进一步规范上海分公司        → 上海分公司
#   通过调阅反洗钱系统中上海分公司      → 上海分公司
#   宏远集团上海分公司           → ②左侧含「集团」→ 不重切，整名保留
#   泽远技术上海分公司           → ④左侧无叙述词 → 不重切，整名保留（不丢字号）
#   被告泽远工业技术服务有限公司       → body 无区划名 → 不重切，整名保留
# 取舍：宁可少覆盖字号，也不误改正文。
# 无地名锚点的泛指分支表达由 ORG_GENERIC_BRANCH_REFERENCE_RE 排除；其他缺少实体证据的
# 分公司/子公司宽跨度候选由 ORG_SPAN_REVIEW_REASON 降级为人工复核。
ORG_NARRATIVE_MARKERS = (
    "为进一步", "进一步", "通过", "经过", "依据", "根据", "按照", "依照", "遵照",
    "鉴于", "关于", "针对", "结合", "规范", "促进", "加强", "开展", "推进", "完善",
    "确保", "落实", "贯彻", "执行", "实施", "履行", "采用", "使用", "运用", "报送",
    "报请", "审批", "审核", "审阅", "审查", "调阅", "调查", "查询", "核对", "检查",
    "监督", "评估", "评定", "统计", "汇总", "分析", "研究", "讨论", "确定", "明确",
    "统一", "分别", "逐项", "全面", "整改", "改进", "优化", "提升", "提高", "强化",
    "深化", "健全", "建立", "部分", "全部", "所有", "其中", "下属", "所属", "上述",
    "下列", "有关", "相关", "日常",
)


def _org_region_anchor(body: str) -> int:
    """body 中**最靠左**的行政区划名前缀起点；无则 -1。"""
    best = -1
    for r in ORG_REGION_PREFIXES_SORTED:
        i = body.find(r)
        if i >= 0 and (best < 0 or i < best):
            best = i
    return best


def _org_strip_narrative_prefix(value: str, suffix: str) -> str:
    """把 body 中「地名之前的叙述」剥离（不满足四个条件时原样返回 value）。"""
    body = value[:len(value) - len(suffix)]
    p = _org_region_anchor(body)
    if p <= 0:
        return value
    left = body[:p]
    # ORG_GENERIC_SUFFIXES 定义在本文件后半段，此处按**调用时**解析（运行期已可用）
    if any(t in left for t in ORG_GENERIC_SUFFIXES):
        return value
    # 括号内的地名是字号的一部分（`宏远档案管理（上海）有限公司`），不是叙述 —— 不重切
    if "（" in left or "(" in left:
        return value
    if not any(mk in left for mk in ORG_NARRATIVE_MARKERS):
        return value
    return value[p:]


def _is_nonparty_org(value: str) -> bool:
    """候选若以公权力机关通用名结尾，则不是合同当事人，不脱敏。"""
    v = (value or "").strip()
    return any(v.endswith(t) for t in ORG_NONPARTY_TAILS)


# 无地名锚点的「叙述词／日期 ＋ 强后缀」误报专用判据。
# ⚠ 刻意与 ORG_NARRATIVE_MARKERS **分开**：后者用于叙述前缀的**重切**（会改写候选值，
#    例如把「进一步规范上海分公司」切成「上海分公司」）；本表只用于**是否拒绝**的结构判据，
#    避免影响叙述前缀重切的既有行为（如「世界贸易组织上海办事处」不应被误切）。
ORG_NO_ANCHOR_NARRATIVE = tuple(ORG_NARRATIVE_MARKERS) + (
    "目前", "督促", "现已", "如今", "组织",
)


def _org_is_no_anchor_narrative(body: str) -> bool:
    """body 无区划锚点、且剥掉叙述词/数字/日期单位后不足 2 个汉字
    ⇒ 判为「叙述片段 / 日期 + 强后缀」误报。

    实测误报：`目前分公司` / `检查分公司` / `2024年10月23日分公司` / `督促分公司` /
              `通过审阅分公司` / `组织各分公司`（中间都没有地名，重切逻辑无锚可切）。
    真名不误伤：`汇安人寿保险股份有限公司`（body=汇安人寿保险股份，剥无可剥 ⇒ 保留）、
               `上海分公司`（有区划锚点 ⇒ 不走本判据）。
    """
    if _org_region_anchor(body) >= 0:
        return False
    rest = body
    for mk in ORG_NO_ANCHOR_NARRATIVE:
        if mk in rest:
            rest = rest.replace(mk, "")
    rest = "".join(ch for ch in rest if ch not in ORG_LEFT_BOUNDARY)
    rest = re.sub(r"[0-9０-９]", "", rest)
    rest = rest.replace("年", "").replace("月", "").replace("日", "")
    rest = rest.strip("．.·-—、,， 　\t")
    return len(rest) < 2


# 无地名锚点的「动词＋于」叙述前缀。基于区划锚点的重切依赖**区划锚点**，
# 无锚点时（如「否存放于宏远档案中心」「文件归档于泽远工业技术服务有限公司」）切不动，
# 会把叙述前缀连同真名一起标记。这里按「动词＋于」边界做右对齐重切，只保留真名部分。
ORG_NO_ANCHOR_BOUNDARIES = (
    "存放于", "储存于", "保存于", "归档于", "移交于", "交付于", "委托于",
    "设立于", "成立于", "登记于", "注册于", "位于",
)


def _org_strip_no_anchor_boundary(value: str, suffix: str) -> str:
    """无地名锚点时按「动词＋于」边界把叙述右对齐重切（不满足则原样返回）。

    仅在 body 无区划锚点时生效——有锚点的走 `_org_strip_narrative_prefix`，
    避免误切名字内部含边界词的机构名（如「北京位于科技有限公司」body 有「北京」锚点）。
    """
    body = value[:len(value) - len(suffix)]
    if _org_region_anchor(body) >= 0:
        return value
    cut = -1
    for bw in ORG_NO_ANCHOR_BOUNDARIES:
        i = body.rfind(bw)
        if i >= 0:
            cut = max(cut, i + len(bw))
    if cut <= 0 or len(body) - cut < 2:
        return value
    return value[cut:]


ORG_SUFFIX_RE = re.compile(
    "|".join(re.escape(s) for s in sorted(set(ORG_SUFFIX_STRONG) | set(ORG_SUFFIX_WEAK) | {"公司"},
                                          key=len, reverse=True))
)
ORG_ENUMERATION_ONLY_RE = re.compile(
    r"^\s*[（(\[【]?\s*(?:[ivxlcdm]+|\d+|[一二三四五六七八九十百]+)"
    r"\s*[）)\]】、.．:：-]?\s*$",
    re.IGNORECASE,
)
EN_ORG_SUFFIX_RE = (
    r"(?:" +
    r"(?i:(?:Company[ \t]+Limited|Co\.?,?[ \t]+(?:Ltd\.?|Limited)|"
    r"Pte\.?[ \t]+Ltd\.?|Pty\.?[ \t]+Ltd\.?|Sp\.[ \t]*z[ \t]*o\.o\.|"
    r"S\.p\.A\.?|S\.?A\.?|B\.V\.?|N\.V\.?|K\.K\.?|A\.G\.?|L\.?P\.?|"
    r"Limited|Ltd\.?|Inc\.?|Corporation|Corp\.?|LLC|LLP|PLC|GmbH|"
    r"AG|AB|Oy|ApS|Ltda\.?|SARL|SRL))|AS|A/S" + r")"
)
EN_ORG_CAP_TOKEN = (
    r"(?:[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ0-9&.'’\-]*|&|and|of|the|de|van|der|von|da|dos|"
    r"\([A-Z][A-Za-z-]{1,24}\))"
)
EN_ORG_LOWER_BRAND_TOKEN = (
    r"(?!(?:and|of|the|de|van|der|von|da|dos|between|among|by|for|from|to|in|on|at|with|as|or|nor|but|"
    r"shall|will|may|must|should|could|would|can|has|have|had|does|did|been|being|is|are|was|were|be|"
    r"into|upon|such|this|that|these|those|while|when|after|before|until|within|without|company|"
    r"corporation|limited|incorporated|llc|ltd)(?![A-Za-z0-9]))"
    r"[a-z][A-Za-z0-9&.'’\-]{4,}"
)
EN_ORG_RE = re.compile(
    r"(?<![\w])(?!(?i:we),)(?!(?i:and|of|the|de|van|der|von|da|dos|between|among|by|for)[ \t])(?:"
    + EN_ORG_CAP_TOKEN + r"(?:,?[ \t]+" + EN_ORG_CAP_TOKEN + r"){0,7},?[ \t]+|"
    + EN_ORG_CAP_TOKEN + r"(?:,?[ \t]+" + EN_ORG_CAP_TOKEN + r"){0,2}[ \t]+"
    + EN_ORG_LOWER_BRAND_TOKEN + r"[ \t]+" + EN_ORG_CAP_TOKEN
    + r"(?:,?[ \t]+" + EN_ORG_CAP_TOKEN + r"){0,2},?[ \t]+"
    + r")" + EN_ORG_SUFFIX_RE + r"(?![\w])"
)
EN_ORG_ASSOC_RE = re.compile(
    r"(?<![\w])(?!(?i:and|of|the|de|van|der|von|da|dos|between|among|by|for)[ \t])"
    + EN_ORG_CAP_TOKEN + r"(?:,?[ \t]+" + EN_ORG_CAP_TOKEN + r"){0,7}[ \t]+"
    + r"(?i:Association)(?![\w])"
)


def _english_org_candidates(text: str) -> List[Tuple[int, int, str, str]]:
    """Return complete company/association spans, splitting only verified parties."""
    rows: Dict[Tuple[int, int], Tuple[str, str]] = {}
    for match in EN_ORG_RE.finditer(text):
        value = match.group(0).strip()
        cuts = list(re.finditer(r"(?i)\band\b", value))
        split_rows = []
        for cut in cuts:
            left = value[:cut.start()].rstrip(" ,\t")
            right = value[cut.end():].lstrip(" ,\t")
            left_start = match.start() + value.find(left)
            right_start = match.start() + value.find(right, cut.end())
            left_kind = ("association" if EN_ORG_ASSOC_RE.fullmatch(left) else
                         "company" if EN_ORG_RE.fullmatch(left) else "")
            right_kind = ("association" if EN_ORG_ASSOC_RE.fullmatch(right) else
                          "company" if EN_ORG_RE.fullmatch(right) else "")
            if left_kind and right_kind:
                split_rows.extend(((left_start, left_start + len(left), left, left_kind),
                                   (right_start, right_start + len(right), right, right_kind)))
                break
        if split_rows:
            for start, end, name, kind in split_rows:
                rows[(start, end)] = (name, kind)
        else:
            rows[(match.start(), match.end())] = (value, "company")

    for match in EN_ORG_ASSOC_RE.finditer(text):
        rows.setdefault((match.start(), match.end()), (match.group(0), "association"))
    return [(start, end, name, kind)
            for (start, end), (name, kind) in sorted(rows.items())]


EN_ARB_ABBR_RE = re.compile(
    r"(?<![A-Za-z])(HKIAC|SIAC|LCIA|ICSID|CIETAC|SCC)(?![A-Za-z])", re.I)
EN_ARB_ABBR_CONTEXT_RE = re.compile(
    r"(?i)(arbitration|arbitral|award|tribunal|rules|notice of arbitration|仲裁|裁决)")
EN_ARB_ABBR_FULL_NAMES = {
    "HKIAC": ("Hong Kong International Arbitration Centre",),
    "SIAC": ("Singapore International Arbitration Centre",),
    "LCIA": ("London Court of International Arbitration",),
    "ICSID": ("International Centre for Settlement of Investment Disputes",),
    "CIETAC": ("China International Economic and Trade Arbitration Commission",),
    "SCC": ("Arbitration Institute of the Stockholm Chamber of Commerce",),
}


EN_ORG_SHORT_SUFFIXES = {"AG", "A.G.", "AB", "AS", "A/S", "S.A.", "SA", "LP", "L.P.", "Oy", "ApS"}
_STRONG_SET = set(ORG_SUFFIX_STRONG)


def _preceded_by_region_prefix(text: str, start: int, i: int) -> bool:
    """判断位置 i 的单字边界是否「左侧紧邻行政区划前缀」。

    例「广州至诚系统集成有限公司」的「至」——左邻恰是「广州」，属于名字内部用字，
    不能当作叙述前缀的切点；否则会切出「诚系统集成有限公司」，
    正文残留明文「广州至」，同时把条款语义改掉。
    """
    for p in ORG_REGION_PREFIXES_SORTED:
        if i - len(p) >= start and text[i - len(p):i] == p:
            return True
    return False


def _is_org_boundary(text, start, index):
    if text[index] not in ORG_LEFT_BOUNDARY or _preceded_by_region_prefix(text, start, index):
        return False
    if text[index] in "和与及" and index > start:
        left = text[start:index]
        return any(left.endswith(suffix) for suffix in ORG_SUFFIX_STRONG)
    return True


def _expand_org_left(text: str, end: int) -> Tuple[int, str]:
    """以「后缀结束位置」为锚点向左扩张出完整机构名。

    R8-A：候选切点由旧的「逐字覆盖（等价于取最后遇到的边界字）」改为
    「在所有候选切点中取最靠右的一个」，并对单字边界字排除
    「左侧紧邻行政区划前缀」的情形。旧实现会在名字内部含边界字时多切：
      广州「至」诚系统集成有限公司  → 诚系统集成有限公司（残留明文「广州至」）
      被「告」泽远技术…             → 告泽远技术…（残留「被」）
      …建设的「位于」宜宾志远光电…   → 吞掉 8 个字的正文，条款语义被改写
    """
    start = end
    while start > 0 and ORG_BODY_RE.match(text[start - 1]):
        start -= 1
    cuts = [start]
    # (a) 单字边界字：排除「左侧紧邻行政区划前缀」的情形（属名字内部用字）
    for i in range(start, end):
        if _is_org_boundary(text, start, i):
            cuts.append(i + 1)
    # (b) 多字虚词 / 角色短语：其结束位置同样作为候选切点
    for phrase in ORG_LEFT_PHRASES:
        j = start
        while True:
            k = text.find(phrase, j, end)
            if k < 0:
                break
            cuts.append(k + len(phrase))
            j = k + 1
    start = max(cuts)
    return start, text[start:end].strip(" ·-&\"'")


class OrgNameDetector:
    type_id = "ORG_NAME"

    def _english_arb_abbr_candidates(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        for match in EN_ARB_ABBR_RE.finditer(ctx.text):
            window = ctx.text[max(0, match.start() - 120):match.end() + 120]
            if not EN_ARB_ABBR_CONTEXT_RE.search(window):
                continue
            value = match.group(0)
            reasons = ["仲裁机构缩写（语境锚定）"]
            full_name = next((name for name in EN_ARB_ABBR_FULL_NAMES[value.upper()]
                              if re.search(re.escape(name), ctx.text, re.I)), "")
            if full_name:
                reasons.append(f"与「{full_name}」同指的仲裁机构缩写")
            out.append(RawMatch(
                "ORG_NAME", match.start(), match.end(), 0.70, reasons, value,
                {"script": "latin", "arb_abbr": True},
            ))
        return out

    def run(self, ctx: ScanContext) -> List[RawMatch]:
        out: List[RawMatch] = []
        seen: Dict[Tuple[int, int], bool] = {}
        text = ctx.text
        for m in ORG_SUFFIX_RE.finditer(text):
            suffix = m.group(0)
            start, value = _expand_org_left(text, m.end())
            if not _org_short_candidate_length_allowed(value, suffix) or value in ORG_STOPWORDS:
                continue
            if suffix == "公司" and (
                len(value) != 4
                or any(value.endswith(long_suffix) for long_suffix in ORG_SUFFIX_STRONG if long_suffix != "公司")
            ):
                continue
            # 去掉左侧残留的虚词（如「的」「与」）
            while value and value[0] in ORG_LEFT_BOUNDARY:
                start += 1
                value = value[1:]
            # 去掉指代词/量词前缀（本人、号院 等）
            for p in ORG_LEAD_PRONOUNS:
                if value.startswith(p) and len(value) - len(p) >= 4:
                    start += len(p)
                    value = value[len(p):]
                    break
            # 去掉「数字 + 号」前缀（门牌号不是机构名的一部分：88 号曜境中心 → 曜境中心）
            m2 = re.match(r"^\d+\s*号", value)
            if m2 and len(value) - m2.end() >= 4:
                start += m2.end()
                value = value[m2.end():]
            # 去掉单独前缀「号」（门牌量词）：号曜境中心 → 曜境中心
            if value.startswith("号") and len(value) > 4 and not value[1].isdigit():
                start += 1
                value = value[1:]
            # 去掉单字前缀「本/该/其/某」+机构名（本人工作室 → 工作室）
            if value and value[0] in "本该其某贵":
                # 仅当去掉后剩余仍是合理机构名才剥
                if len(value) > 4:
                    start += 1
                    value = value[1:]
            if not _org_short_candidate_length_allowed(value, suffix) or value in ORG_STOPWORDS:
                continue
            if not value.endswith(suffix):
                continue
            # 反语境词——剔除末尾后缀后的 body 含叙述词则拒绝
            # （`部分分公司` body=`部分`；`最终报送人民银行` body=`最终报送人民`）。
            body = value[:len(value) - len(suffix)]
            if _org_has_only_enumeration_prefix(body):
                continue
            if any(value.endswith(term) for term in ORG_GENERIC_NON_ENTITIES):
                continue
            if suffix == "银行" and _org_is_bank_workday_phrase(text, m.start(), m.end()):
                continue
            if suffix in {"分公司", "子公司"} and _org_is_generic_branch_reference(body):
                continue
            if any(w in body for w in ORG_ANTI_CONTEXT):
                continue
            # 公权力机关通用名（尾匹配）不是合同当事人，不脱敏。
            if _is_nonparty_org(value):
                continue
            # C1/C3（v1.3.0）：无地名锚点的「叙述词／日期 ＋ 强后缀」→ 拒绝
            # （实测「目前分公司」「检查分公司」「2024年10月23日分公司」「督促分公司」等）
            if suffix in _STRONG_SET and _org_is_no_anchor_narrative(body):
                continue
            # 结构性剥离叙述前缀（按区划名右对齐重切；不满足条件则原样保留）
            _nv = _org_strip_narrative_prefix(value, suffix)
            if _nv != value:
                # 位置用「后缀结束位置 − 新值长度」反推，免受前面各类 strip 的影响
                start = m.end() - len(_nv)
                value = _nv
                if not _org_short_candidate_length_allowed(value, suffix) or value in ORG_STOPWORDS:
                    continue
            # C3（v1.3.0）：无地名锚点的「动词＋于」叙述前缀 → 右对齐重切
            # （实测「否存放于宏远档案中心」→「宏远档案中心」；
            #   「文件归档于泽远工业技术服务有限公司」→「泽远工业技术服务有限公司」）
            _nb = _org_strip_no_anchor_boundary(value, suffix)
            if _nb != value:
                start = m.end() - len(_nb)
                value = _nb
                if not _org_short_candidate_length_allowed(value, suffix) or value in ORG_STOPWORDS:
                    continue
            is_short = len(value) == 4 and suffix in ORG_SHORT_SUFFIXES
            if is_short:
                if suffix == "公司" and any(
                    value.endswith(long_suffix) for long_suffix in ORG_SUFFIX_STRONG if long_suffix != "公司"
                ):
                    continue
                party_label = _org_is_short_party_label(text, start)
                entity_action = _org_is_short_entity_action(text, m.end())
                if not (party_label or entity_action):
                    continue
            key = (start, m.end())
            if key in seen:
                continue
            seen[key] = True
            conf = 0.88 if suffix in _STRONG_SET else 0.62
            reasons = [f"含组织机构后缀「{suffix}」"]
            if is_short and party_label:
                reasons.append("4字短名称命中主体标签")
            elif is_short and entity_action:
                reasons.append("4字短名称命中机构行为结构")
            if ctx.has_kw(start, m.end(), ("甲方", "乙方", "丙方", "供方", "需方", "卖方", "买方", "签约", "受托方", "委托方")):
                conf += 0.06
                reasons.append("位于合同主体上下文")
            if is_short and entity_action and not party_label:
                conf = min(conf, 0.49)
            if _org_span_needs_review(text, start, value, suffix):
                conf = min(conf, 0.42)
                reasons.append(ORG_SPAN_REVIEW_REASON)
            out.append(RawMatch("ORG_NAME", start, m.end(), min(conf, 0.95), reasons, value,
                                {"suffix": suffix}))
        for start, end, value, kind in _english_org_candidates(text):
            if kind == "association":
                has_party_context = bool(ctx.has_kw(
                    start, end,
                    ("between", "by and between", "party", "party a", "party b",
                     "seller", "buyer", "contracting party", "first party", "second party",
                     "entered into by", "entered by"),
                    window=100,
                ))
                if not has_party_context:
                    continue
                out.append(RawMatch(
                    "ORG_NAME", start, end, 0.76,
                    ["合同当事人上下文中的Association类机构名称"],
                    value, {"script": "latin", "organization_kind": "association"},
                ))
                continue
            suffix = re.search(EN_ORG_SUFFIX_RE + r"$", value, re.I)
            short_suffix = suffix.group(0).upper() in EN_ORG_SHORT_SUFFIXES if suffix else False
            has_party_context = bool(ctx.has_kw(
                start, end,
                ("between", "by and between", "hereinafter", "herein called",
                 "organized under", "party", "the company", "for and on behalf of"),
                window=100,
            ))
            conf = 0.9 if not short_suffix else (0.66 if has_party_context else 0.42)
            reasons = ["含英文公司法定后缀"]
            if short_suffix and has_party_context:
                reasons.append("英文短法定后缀带合同主体上下文")
            elif short_suffix:
                reasons.append("英文短后缀缺少主体上下文，需人工核对")
            out.append(RawMatch("ORG_NAME", start, end, conf,
                                reasons, value, {"script": "latin"}))
        out.extend(self._english_arb_abbr_candidates(ctx))
        return out


# ------------------------------------------------------------------------------
# 4.5 检测器装配
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# 4.5 表格感知的金额检测器（P0 修复：表格金额整列漏检）
# ------------------------------------------------------------------------------
# 背景（见《漏洞清单与识别机制说明.md》M1/M2/M3/M5）：
#   表格里「只放数字、不带单位」的金额，原来只有一条正则能碰——
#   要求数字带千分位，且**全文 120 字窗口**内出现「金额/价款」等关键词。
#   真实合同的费用清单有两万多个字，列头与数据行相距远超窗口，于是整列漏检。
#
# 修复思路：不再靠全文窗口，而是**按表格结构定位**——
#   1. 解析层已经给每个表格单元格记了 (表序号, 行序号, 单元格序号) 坐标；
#   2. 找到「列头行」（整行都是短标签，且其中有「金额/单价/合计」这类列头词）；
#   3.  该行里写「金额」的列号，其下方所有行的同列单元格，一律按金额处理。
#   这样「金额」列的数字无论有没有千分位、离列头多远，都能被识别；
#   而「序号 / 数量 / 单位」列不会被误伤。
#
# 局限（有意为之，写入文档留痕）：列头行识别依赖「整行都是短标签」，
#   合并单元格的大标题行、以及完全无列头的表格不走此路径，仍由正则路径兜底。

# 第三条 AMOUNT（裸千分位、无单位）的**反语境单位表**——数字后紧邻这些计量单位
# 时不判为金额（「数量列数字被当金额」的误报治理）。仅用于正则尾部的负向前瞻。
#
# 红线（改动前务必逐条确认）：
#   1) **不含**金额单位 `元/万元/亿/圆/块`——这些必须保留判定；
#   2) **不含** `/`——`20,000 元/次` 数字后紧邻是「元」，前瞻不命中，仍判金额（「只做紧邻」）；
#   3) 长词优先：`厘米|毫米|平方米|立方米|毫升` 排在 `米|升` 之前（Python 交替左优先）；
#   4) `人` 必须写作 `人(?!民币)`——把货币「人民币」与数量单位「人」分开。若只写 `人`，
#      `1,000 人民币` 会被前瞻排除，而第一条 AMOUNT（末尾强制 `元/圆/万元/亿元`）与表格
#      解析（_AMOUNT_CELL_RE 的 unit 组不含「人民币」）都没有兜底 → 全路径漏检。
#      **不可**改成「把 `人民币` 并列进前瞻集合」：负向前瞻 (?!A|B) 只会扩大排除集合，
#      而「人民币」以「人」开头、「人」已在表内，该写法原地无效。
_AMOUNT_UNIT_AFTER = (
    "条|个|件|台|套|份|人(?!民币)|名|次|页|张|支|批|项|册|本|辆|部|户|家|处|笔|根|组|种|只|枚"
    "|吨|公斤|千克|克|斤|厘米|毫米|平方米|立方米|毫升|米|升|㎡|年|月|日|季"
)

# 列头里代表「金额」的词（命中即认为该列是金额列）
AMOUNT_COL_HEADERS = (
    "金额", "价款", "价税合计", "合计", "小计", "总计", "总额", "总价", "合价", "单价",
    "报价", "费用", "收费", "计费", "费率", "税率", "税额", "成本", "预算", "尾款",
    "首付款", "预付款", "服务费", "报酬", "酬金", "对价", "应收", "应付", "结算",
    "合同金额", "含税金额", "不含税金额", "税前", "税后", "人民币", "元", "万元",
    "刊例价", "刊例费", "代言费", "出场费", "工时费", "工价",
)
# 这些词出现在单元格里，基本说明它是「表格标题」而不是「列头」
_AMOUNT_HEADER_TITLE_WORDS = ("明细", "清单", "一览", "汇总表", "说明书", "备注", "名称", "内容")
# 列头词之外**允许**附带的修饰字（「金额（元）」「含税金额」「费用合计」「单价/月」…）。
# 白名单以外一律不算列头——否则数据行里的「带宽费用」会被当成列头。
_AMOUNT_HEADER_EXTRA_CHARS = set("（）()元人民币万含不含税价金额款费合计小总标的准月年天次人台项月")
# 整格就是一个金额的判定用（见 _parse_amount_cell）
_AMOUNT_CELL_RE = re.compile(
    r"^\s*(?:人民币|RMB|CNY|￥|¥)?\s*"
    r"(?P<num>[0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"\s*(?P<unit>亿元|万元|亿|万|元|圆|块)?"
    r"(?:\s*/\s*(?:月|年|日|季|人|台|个|次|份|小时|天|㎡|平方米|户|项))?"
    r"(?:\s*(?:整|含税|不含税|税前|税后))?"
    r"(?:\s*[（(][^（()）]{0,12}[)）])?"
    r"\s*$"
)
_HEADER_STRIP_RE = re.compile(r"[\s\u3000:：()（）\[\]【】|｜,，.。、]")


def _is_amount_header(cell: str) -> bool:
    """该单元格文本是不是「金额类列头」。

    判定要点：列头词必须**几乎占满整个单元格**——除列头词外只允许「元/含税/合计」
    这类修饰字（见 _AMOUNT_HEADER_EXTRA_CHARS）。否则数据行里的
    「带宽费用」「服务器租用费用」都会被当成列头，该行数据整体被跳过。
    """
    t = _HEADER_STRIP_RE.sub("", cell or "")
    if not (1 <= len(t) <= 12):
        return False
    if len(t) >= 5 and any(w in t for w in _AMOUNT_HEADER_TITLE_WORDS):
        return False
    for w in AMOUNT_COL_HEADERS:
        if t == w:
            return True
        if w in t:
            extra = t.replace(w, "", 1)
            if len(extra) <= 3 and all(c in _AMOUNT_HEADER_EXTRA_CHARS for c in extra):
                return True
    return False


_PURE_NUMBER_CELL_RE = re.compile(r"^[\d.,，%]+$")


def _row_has_number(cells, text: str) -> bool:
    """该行是否含纯数字单元格——含则说明它是数据行，不能当列头行。

    cells 为 (grid 起始列, 跨列数, 起始, 结束) 四元组列表。
    """
    return any(_PURE_NUMBER_CELL_RE.match(text[s:e].strip()) for _c, _sp, s, e in cells)


def _parse_amount_cell(cell: str, min_digits: int = 2) -> Optional[Tuple[str, float, str]]:
    """把「整格就是一个金额」的单元格解析成 (值, 置信度, 理由)；不是则 None。

    min_digits：最少有效数字位数（默认 2，防把序号当金额）。在「已由列头确认是
    金额列」的路径下可放宽到 1——该语境下不存在序号列，像 ¥5/㎡ 这类 1 位单价
    是合法金额，不应漏掉。
    """
    m = _AMOUNT_CELL_RE.match(cell or "")
    if not m:
        return None
    num = m.group("num")
    unit = m.group("unit") or ""
    digits = re.sub(r"\D", "", num)
    # 位数过少多半是序号（默认路径）；超长数字是电话/证件号，都不当金额
    if len(digits) < min_digits or len(digits) > 15:
        return None
    if num.startswith("0") and len(digits) > 1 and "." not in num:
        return None
    has_sep = "," in num
    has_dec = "." in num
    if unit:
        conf, why = 0.80, "表格金额列（带单位）"
    elif has_sep or has_dec:
        conf, why = 0.76, "表格金额列（含千分位/小数）"
    elif len(digits) >= 4:
        conf, why = 0.72, "表格金额列（4 位以上整数）"
    else:
        conf, why = 0.62, "表格金额列（2~3 位整数）"
    return m.group(0).strip(), conf, why


# ---- 真空区：路径①在表格单元格上的 AMOUNT 候选按列头语义保守剔除 ----
#
# 背景：`scan_document` 把所有块（含表格单元格）用 "\n".join 拼成**一整串**再交给正则，
# 所以「路径①全文正则」根本不感知「列」。一旦 `TableAmountDetector` 因该表**没有任何
# 金额类列头**而整体退场（见其 `if not amount_cols: return []`），该表单元格就只剩
# 裸正则约束——数量列数字（如「数量 200,000 条」的 200,000）会被报成 AMOUNT。
# 其判定见 `_postprocess` 第 2.5 步；此处只提供列头上下文与三个判据正则。
#
# 列头命中这些词 → 该列不是金额列（数量/单位/规格类语义）
_AMOUNT_NON_COL_HEADER_RE = re.compile(
    r"数量|数目|个数|件数|台数|人数|份数|次数|序号|编号|单位|规格|型号|名称|备注|说明"
    r"|占比|比例|天数|期数|页数|张数"
)
# 候选值须形如「纯千分位数字」（无单位、无货币符号）才可能被剔
_AMOUNT_PURE_NUM_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
# 单元格内出现货币符号或金额单位 → 一律不剔（保护真金额，含「数量」列被误写金额的情形）
_AMOUNT_CELL_MONEY_RE = re.compile(r"[¥￥$€£]|万元|亿元|元|圆|块")


def _table_column_context(blocks: Sequence[Block]) -> Dict[int, Dict[int, str]]:
    """构建「表号 → grid 列 → 列头文本」映射，供 B 方案（真空区）判定列语义。

    - 只认每张表的**第 1 行**为列头行（与 TableAmountDetector 的列头行口径一致：
      行号从 1 起算）。
    - 若第 1 行含纯数字单元格，说明第 1 行其实是数据行、该表没有独立列头行 →
      **整表不建图**，回退为不干预（保守，宁可漏剔不可误剔）。
    - `table_ref` 仅 DOCX 表格单元格有值；非表格块（pdf/txt/md 段落）一律忽略，
      因此本方案只对 DOCX 生效。
    """
    cells_by_table: Dict[int, List[Tuple[int, int, str]]] = {}
    for b in blocks:
        ref = getattr(b, "table_ref", None)
        if ref is None:
            continue
        ti, ri, c_start, c_span = ref
        if ri != 1:
            continue
        cells_by_table.setdefault(ti, []).append(
            (c_start, c_span, getattr(b, "text", "") or ""))
    header_of: Dict[int, Dict[int, str]] = {}
    for ti, cells in cells_by_table.items():
        if any(_PURE_NUMBER_CELL_RE.match((t or "").strip()) for _c, _s, t in cells):
            continue          # 第 1 行像数据行 → 无独立列头行，放弃建图
        hmap: Dict[int, str] = {}
        for c_start, c_span, txt in cells:
            for gc in range(c_start, c_start + c_span):
                hmap[gc] = (txt or "").strip()
        header_of[ti] = hmap
    return header_of


class TableAmountDetector:
    """按表格结构识别「金额列」里的数字（含无单位、无千分位的裸数字）。"""

    type_id = "AMOUNT"

    def run(self, ctx: ScanContext) -> List[RawMatch]:
        blocks = getattr(ctx, "blocks", None) or []
        if not blocks:
            return []
        text = ctx.text

        # 1) 块 → 归一化全文区间 + 表格坐标（列号为 gridSpan 展开后的视觉列）
        #    下标算法与 DetectorEngine.scan_document 拼接 full_raw 的方式保持一致：
        #    块文本 rstrip("\n") 后逐块累加，块间补一个 "\n"。
        pos = 0
        rows: Dict[Tuple[int, int], List[Tuple[int, int, int, int]]] = {}
        for b in blocks:
            t = (getattr(b, "text", "") or "").rstrip("\n")
            ln = len(NormalizedText(t).norm)
            ref = getattr(b, "table_ref", None)
            if ref is not None and ln:
                ti, ri, c_start, c_span = ref
                rows.setdefault((ti, ri), []).append((c_start, c_span, pos, pos + ln))
            pos += ln + 1
        if not rows:
            return []

        # 2) 认「列头行」：整行都是短标签、其中有金额类列头、且**不含纯数字单元格**。
        #    要求 ≥2 个单元格，以免把合并单元格的表格大标题（如「服务费用明细表」）当列头；
        #    含纯数字则说明是数据行（如「2 | 带宽费用 | 6 | 3500 | 21000」）。
        #    列头若横向合并（gridSpan），其覆盖的**全部 grid 列**都算金额列——
        #    这样表头「单价」跨 3 列时，数据行的主材/人工/辅材都被覆盖，不会整体错位。
        header_rows: set = set()
        amount_cols: Dict[int, set] = {}
        for (ti, ri), cells in rows.items():
            if len(cells) < 2:
                continue
            if not all(len(_HEADER_STRIP_RE.sub("", text[s:e])) <= 14
                       for _c, _sp, s, e in cells):
                continue
            if _row_has_number(cells, text):
                continue
            hit_spans = [(c, sp) for c, sp, s, e in cells if _is_amount_header(text[s:e])]
            if not hit_spans:
                continue
            header_rows.add((ti, ri))
            bucket = amount_cols.setdefault(ti, set())
            for c, sp in hit_spans:
                bucket.update(range(c, c + sp))
        if not amount_cols:
            return []

        # 3) 列头行以下的每一个同行号单元格，只要**覆盖到**金额列就按金额处理。
        #    「已由列头确认是金额列」的语境下不存在序号列，故 min_digits 放宽到 1
        #    （¥5/㎡ 这类 1 位单价是合法金额，不应漏）。
        out: List[RawMatch] = []
        for (ti, ri), cells in rows.items():
            if (ti, ri) in header_rows:
                continue
            cols = amount_cols.get(ti)
            if not cols:
                continue
            for c, sp, s, e in cells:
                if not any(x in cols for x in range(c, c + sp)):
                    continue
                parsed = _parse_amount_cell(text[s:e], min_digits=1)
                if not parsed:
                    continue
                value, conf, why = parsed
                sel_start = s + (len(text[s:e]) - len(text[s:e].lstrip()))
                out.append(RawMatch("AMOUNT", sel_start, sel_start + len(value), conf,
                                    [why, f"表格第 {ti} 张第 {ri} 行第 {c} 列（列头定位）"],
                                    value, {"table": ti, "row": ri, "col": c}))
        return out


def _url_syntax_valid(value: str) -> Optional[bool]:
    candidate = (value or "").strip()
    if not re.match(r"(?i)^https?://", candidate):
        candidate = "https://" + candidate
    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    labels = hostname.split(".")
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if (parsed.scheme.casefold() not in ("http", "https") or not hostname or
            authority.endswith(":") or (port is not None and not 0 <= port <= 65535) or
            len(hostname) > 253 or len(labels) < 2):
        return False
    return all(label and len(label) <= 63 and label[0].isalnum() and
               label[-1].isalnum() and
               all(char.isalnum() or char == "-" for char in label)
               for label in labels)


def _date_syntax_valid(value: str) -> Optional[bool]:
    candidate = re.sub(r"\s+", " ", (value or "").strip())
    numeric = re.fullmatch(
        r"((?:19|20)\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?",
        candidate)
    if numeric:
        try:
            datetime(*(int(part) for part in numeric.groups()))
            return True
        except ValueError:
            return False
    month_only = re.fullmatch(r"((?:19|20)\d{2})\s*年\s*\d{1,2}\s*月", candidate)
    if month_only:
        year_month = re.fullmatch(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月", candidate)
        try:
            datetime(int(year_month.group(1)), int(year_month.group(2)), 1)
            return None
        except ValueError:
            return False
    english = re.fullmatch(
        r"(?i)([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+((?:19|20)\d{2})",
        candidate)
    if english:
        month, day, year = english.groups()
        for month_format in ("%B", "%b"):
            try:
                month_number = datetime.strptime(month.rstrip("."), month_format).month
                datetime(int(year), month_number, int(day))
                return True
            except ValueError:
                continue
        return False
    day_first = re.fullmatch(
        r"(?i)(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?[,]?\s+((?:19|20)\d{2})",
        candidate)
    if day_first:
        day, month, year = day_first.groups()
        for month_format in ("%B", "%b"):
            try:
                month_number = datetime.strptime(month.rstrip("."), month_format).month
                datetime(int(year), month_number, int(day))
                return True
            except ValueError:
                continue
        return False
    return None


def _date_of_birth_syntax_valid(value: str) -> Optional[bool]:
    """DOB 数字日期：月/日两种顺序都试；无效日历不得高置信。"""
    candidate = (value or "").strip()
    numeric = re.fullmatch(
        r"(\d{1,2})([/.-])(\d{1,2})\2((?:19|20)\d{2})",
        candidate)
    if not numeric:
        return _date_syntax_valid(value)
    first, second, year = (
        int(numeric.group(1)), int(numeric.group(3)), int(numeric.group(4)))
    for month, day in ((first, second), (second, first)):
        try:
            datetime(year, month, day)
            return True
        except ValueError:
            continue
    return False


_AMOUNT_NUMERIC_VALUE_RE = re.compile(
    r"(?i)(?:人民币|RMB|CNY|¥|￥)?\s*((?:\d{1,3}(?:[,，]\d{3})+|\d+)"
    r"(?:\.\d{1,2})?)\s*(?:万元|亿元|元|圆)(?:整)?")


def _amount_syntax_valid(value: str) -> Optional[bool]:
    match = _AMOUNT_NUMERIC_VALUE_RE.fullmatch((value or "").strip())
    return bool(match)


def build_detectors(custom_specs: Sequence[RegexSpec] = ()) -> List[Any]:
    specs: List[RegexSpec] = [
        # ---------------- 身份证 ----------------
        RegexSpec(
            "ID_CARD",
            r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
            conf_ok=0.96, conf_weak=0.42, validator=id_card_check,
            ok_reason="GB 11643 校验位正确", weak_reason="校验位错误，疑似误识别/编造号",
            keywords=("身份证", "证件号", "身份证明", "证件号码", "公民身份"),
        ),
        RegexSpec(
            "ID_CARD_15",
            r"(?<!\d)[1-9]\d{7}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}(?!\d)",
            conf_ok=0.8, conf_weak=0.8, keywords=("身份证", "证件号", "老身份证"),
        ),
        # ---------------- 统一社会信用代码 ----------------
        # 说明：真实合同（尤其是样例/测试合同、扫描件、人工录入件）里经常出现
        # 「校验码算错」的统一社会信用代码。旧配置 conf_weak=0.3 会被 min_confidence
        # 直接滤掉，导致「统一社会信用代码：91xxxxxxxx」整行漏检。
        # 现改为：结构命中的基准分压到门槛以下（0.45），但一旦上下文出现
        # 「统一社会信用代码 / 信用代码 / 营业执照」等强标签，就提升到 0.75 以上被召回，
        # 既保证带标签的一定识别出来，又不会把无标签的 18 位随机串当成信用代码。
        RegexSpec(
            "USCC",
            r"(?<![0-9A-Za-z])[0-9ABCDEFGHJKLMNPQRTUWXY]{2}\d{6}[0-9ABCDEFGHJKLMNPQRTUWXY]{10}(?![0-9A-Za-z])",
            conf_ok=0.94, conf_weak=0.45, validator=uscc_check,
            ok_reason="GB 32100 校验码正确",
            weak_reason="校验码错误（疑似测试数据/录入有误），但符合统一社会信用代码结构",
            keywords=("统一社会信用代码", "信用代码", "统一代码", "营业执照"),
            kw_boost=0.32,
        ),
        # ---------------- 银行卡 / 账号 ----------------
        RegexSpec(
            "BANK_CARD",
            r"(?<![\d-])(?:\d{4}[ -]?){3}\d{4}(?:[ -]?\d{3})?(?!\d)",
            conf_ok=0.93, conf_weak=0.22, validator=luhn_check,
            ok_reason="Luhn 校验通过", weak_reason="Luhn 校验未通过",
            keywords=("银行卡", "卡号", "借记卡", "信用卡", "开户行", "收款账户",
                      "Credit Card", "Debit Card", "Card Number", "Card No", "Card #"),
        ),
        RegexSpec(
            "BANK_CARD",
            r"(?<![\d-])\d{16,19}(?!\d)",
            conf_ok=0.9, conf_weak=0.2, validator=luhn_check,
            ok_reason="Luhn 校验通过", weak_reason="Luhn 校验未通过（可能是账号或普通数字）",
            keywords=("银行卡", "卡号", "卡号:", "储蓄卡",
                      "Credit Card", "Debit Card", "Card Number", "Card No", "Card #"),
        ),
        RegexSpec(
            "BANK_ACCOUNT",
            # 兼容两种排版：① 连写 9~25 位；② 按 3~6 位分组、用空格/连字符分隔
            #   （合同常写成「6222 0000 0000 0000 000」——旧规则不容许空格，整条失配）。
            r"(?<![\d.,])(?:\d{9,25}|\d{3,6}(?:[\s\-]\d{3,6}){2,4})(?![\d.])",
            conf_ok=0.55, conf_weak=0.35,
            keywords=("账号", "帐号", "账户", "开户", "汇款", "收款", "转账", "银行", "对公",
                      "account", "acct", "结算账户",
                      "Bank Account", "Account No", "Account Number", "Wire Transfer",
                      "Beneficiary Account", "Sort Code", "Bank Details"),
            kw_boost=0.3, require_keyword=True,
            # 分段写法会与「分段座机号」形近，故补电话类反关键词降权（require_keyword 仍兜底）
            anti_keywords=("金额", "人民币", "¥", "￥", "价款", "元",
                           "电话", "传真", "座机", "服务热线"),
        ),
        RegexSpec(
            "IBAN",
            r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]){10,30}(?![A-Z0-9])",
            conf_ok=0.95, conf_weak=0.35, validator=iban_check,
            ok_reason="IBAN MOD-97校验通过", weak_reason="IBAN校验失败",
            keywords=("IBAN", "International Bank Account", "Account Number"),
            kw_boost=0.2, require_keyword=True, context_chars=48,
        ),
        RegexSpec(
            "SWIFT",
            r"(?<![A-Z0-9])[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?(?![A-Z0-9])",
            conf_ok=0.72, conf_weak=0.72,
            keywords=("SWIFT", "BIC", "Bank Identifier Code"),
            kw_boost=0.2, require_keyword=True,
        ),
        RegexSpec(
            "ABA_ROUTING",
            r"(?<!\d)\d{9}(?!\d)",
            conf_ok=0.72, conf_weak=0.25, validator=aba_routing_check,
            ok_reason="ABA校验通过", weak_reason="ABA校验失败",
            keywords=("ABA", "Routing Number", "Routing No"),
            kw_boost=0.18, require_keyword=True,
        ),
        RegexSpec(
            "US_SSN",
            r"(?<!\d)(?:\d{3}-\d{2}-\d{4}|\d{9})(?!\d)",
            conf_ok=0.82, conf_weak=0.34, validator=us_ssn_check,
            ok_reason="SSN区段格式通过", weak_reason="SSN区段格式无效",
            keywords=("SSN", "Social Security Number", "Social Security No"),
            kw_boost=0.12, require_keyword=True,
        ),
        RegexSpec(
            "US_EIN",
            r"(?<!\d)\d{2}-\d{7}(?!\d)",
            conf_ok=0.72, conf_weak=0.72,
            keywords=("EIN", "Employer Identification Number", "Federal Tax ID"),
            kw_boost=0.16, require_keyword=True, context_chars=48,
        ),
        RegexSpec(
            "VAT_ID",
            r"(?<![A-Z0-9])(?:GB|DE|NL|FI|FR|ES|IT|CH|BE|IE)[ -]?[A-Z0-9]{8,12}(?![A-Z0-9])",
            conf_ok=0.68, conf_weak=0.68,
            keywords=("VAT", "VAT ID", "VAT Number", "Tax Identification Number",
                      "GST Number"),
            kw_boost=0.16, require_keyword=True,
        ),
        RegexSpec(
            "REGISTRATION_NO",
            r"(?<![A-Z0-9])(?=[A-Z0-9-]{5,24}(?![A-Z0-9]))(?=[A-Z0-9-]*\d)"
            r"[A-Z0-9][A-Z0-9-]{4,23}(?![A-Z0-9])",
            conf_ok=0.56, conf_weak=0.56,
            keywords=("Company No", "Company Number", "Registration No",
                      "Registration Number", "UEN", "ABN", "Business Number"),
            kw_boost=0.18, require_keyword=True,
        ),
        # ---------------- 手机 / 座机 ----------------
        RegexSpec(
            "PHONE_MOBILE",
            # 兼容分段写法「138 0000 0000」/「138-0000-0000」。
            r"(?<!\d)(?:1[3-9]\d{9}|1[3-9]\d(?:[\s\-]?\d{4}){2})(?!\d)",
            conf_ok=0.88, conf_weak=0.88,
            keywords=("手机", "电话", "联系", "联系方式", "联系电话", "移动电话", "号码"),
        ),
        RegexSpec(
            "PHONE_INTERNATIONAL",
            r"(?<![A-Z0-9])\+\d{1,3}[ .\-]?(?:\(\d{1,4}\)|\d{1,4})"
            r"(?:[ .\-]\d{2,6}){1,3}(?:\s*(?:ext\.?|x)\s*\d{1,6})?(?!\d)",
            conf_ok=0.78, conf_weak=0.78,
            keywords=("Tel", "Telephone", "Phone", "Mobile", "Fax", "Contact"),
            kw_boost=0.12,
        ),
        RegexSpec(
            "PHONE_LANDLINE",
            r"(?<!\d)(?:\(\d{3}\)[ \-]?|\d{3}[ \-])\d{3}[ \-]\d{4}"
            r"(?:\s*(?:ext\.?|x)\s*\d{1,6})?(?!\d)",
            conf_ok=0.55, conf_weak=0.55,
            keywords=("Tel", "Telephone", "Phone", "Mobile", "Fax", "Contact"),
            kw_boost=0.22, require_keyword=True, anti_keywords=("IBAN",),
        ),
        RegexSpec(
            "PHONE_LANDLINE",
            # 兼容四种常见写法：
            #   0755-86329871 / 0755 86329871       号码连写（7~8 位）
            #   010-8666 2188 / 010-8666-2188       号码分段（3~4 + 4 位）
            #   010 8666 2188                       区号后空格
            #   400-800-8888 / 800-800-8888         服务热线
            r"(?<!\d)"
            r"(?:"
            r"\(?0\d{2,3}\)?\s?[- ]?\s?\d{3,4}[- ]\d{4}"      # 分段号码（必须有分隔符）
            r"|\(?0\d{2,3}\)?\s?[- ]?\s?\d{7,8}(?:[- ]\d{1,6})?"  # 连写号码（可带分机）
            r"|(?:400|800)[- ]?\d{3}[- ]?\d{4}"
            r")"
            r"(?!\d)",
            conf_ok=0.55, conf_weak=0.55,
            strip_chars=" \t",
            keywords=("电话", "座机", "传真", "联系", "Tel", "总机", "客服"),
            kw_boost=0.3,
            anti_keywords=("IBAN",),
        ),
        # ---------------- 邮箱 / 邮编 ----------------
        RegexSpec(
            "EMAIL",
            r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{2,}\.[A-Za-z]{2,24}",
            # 把填空下划线一并列入剥离字符。否则「邮箱：__x@y.com」的前导
            # '__' 会被圈进替换区间（连带吞掉落款处的填空线）。
            # 注意：剥离字符与 RegexDetector.lead 的两步记账须保持一致，否则
            # 区间末尾会偏左、留下若干字符的明文残留。
            conf_ok=0.9, conf_weak=0.9, strip_chars=".,;:，。；：)）_＿",
        ),
        RegexSpec(
            "POSTCODE",
            r"(?<!\d)[1-9]\d{5}(?!\d)",
            conf_ok=0.45, conf_weak=0.45,
            keywords=("邮编", "邮政编码", "邮码", "Zip"), kw_boost=0.3, require_keyword=True,
        ),
        RegexSpec(
            "POSTCODE",
            r"(?<![A-Z0-9])(?:\d{5}(?:-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}|"
            r"[A-Z]\d[A-Z]\s?\d[A-Z]\d)(?![A-Z0-9])",
            conf_ok=0.48, conf_weak=0.48,
            keywords=("ZIP", "ZIP Code", "Postcode", "Postal Code"),
            kw_boost=0.28, require_keyword=True,
        ),
        # ---------------- 护照 / 通行证 ----------------
        RegexSpec(
            "PASSPORT",
            r"(?<![A-Z0-9])[EW][A-Z0-9]{8}(?![A-Z0-9])",
            conf_ok=0.65, conf_weak=0.65, keywords=("护照", "passport", "PASSPORT"),
            kw_boost=0.2, require_keyword=True,
        ),
        RegexSpec(
            "PASSPORT",
            r"(?<![A-Z0-9])[A-Z0-9]{7,12}(?![A-Z0-9])",
            conf_ok=0.58, conf_weak=0.58,
            keywords=("Passport No", "Passport Number", "Travel Document"),
            kw_boost=0.28, require_keyword=True,
        ),
        RegexSpec(
            "HK_MO_TW_PERMIT",
            r"(?<![A-Z0-9])[HM]\d{10}(?![A-Z0-9])",
            conf_ok=0.6, conf_weak=0.6, keywords=("通行证", "港澳", "来往内地"), kw_boost=0.2,
        ),
        # ---------------- 纳税人识别号（非统一社会信用代码的老税号）----------------
        RegexSpec(
            "TAX_ID",
            r"(?<![0-9A-Z])[A-Z0-9]{15}(?![0-9A-Z])",
            conf_ok=0.7, conf_weak=0.7,
            keywords=("纳税人识别号", "税号", "税务登记", "国税", "地税"),
            kw_boost=0.2, require_keyword=True,
        ),
        RegexSpec(
            "TAX_ID",
            r"(?<![A-Z0-9])[A-Z0-9][A-Z0-9-]{7,19}(?![A-Z0-9])",
            conf_ok=0.55, conf_weak=0.55,
            keywords=("Tax ID", "Tax Identification Number", "Tax Number"),
            kw_boost=0.2, require_keyword=True,
        ),
        # ---------------- 金额 ----------------
        RegexSpec(
            "AMOUNT",
            r"(?:人民币|RMB|CNY|¥|￥)?\s?\d[\d,，]*(?:\.\d{1,2})?\s?(?:万元|亿元|元|圆)(?:整)?",
            conf_ok=0.68, conf_weak=0.42, validator=_amount_syntax_valid,
            ok_reason="金额数字与单位格式有效", weak_reason="金额数字分组或单位格式可疑",
        ),
        RegexSpec(
            "AMOUNT",
            r"(?:人民币)?\s?[壹贰叁肆伍陆柒捌玖拾佰仟万亿零角分]{3,}[元圆]?[整正]?",
            conf_ok=0.72, conf_weak=0.72,
        ),
        # 表格中只放数字的金额（无单位）—— 必须带千分位（4 位以下裸数字风险太高）
        # 关键词窗口 200 字；附带 anti_keywords 排除身份证/统一代码/年份前缀等
        RegexSpec(
            "AMOUNT",
            # 「数字后紧邻计量单位」负向前瞻——排除数量列数字的误报。
            # 单位表见模块级常量 _AMOUNT_UNIT_AFTER（其中 `人` 写作 `人(?!民币)`）。
            # `[\s_]*` 允许数字与单位间夹空白/下划线（归一化后全角与零宽已折成这两种）。
            r"(?<![0-9.,])(?:[0-9]{1,3}(?:,\d{3})+(?:\.\d+)?)(?![0-9])"
            r"(?![\s_]*(?:" + _AMOUNT_UNIT_AFTER + r"))",
            conf_ok=0.45, conf_weak=0.45,
            keywords=("金额", "价款", "总价", "款项", "费用", "付款", "支付",
                      "结算", "预算", "应收", "应付", "报酬", "对价",
                      "工时费", "金额（元）", "尾款", "比例", "付款节点",
                      "付款条件", "含税", "报价", "刊例", "CPM", "CPC",
                      "KOL", "媒体", "刊例价", "刊例费", "投放", "采购",
                      "服务费", "代言费", "出场费",
                      "单价", "合价", "合计", "小计", "总计", "总额", "价款总额",
                      "价税合计", "含税金额", "不含税金额", "税额", "金额合计",
                      "单价（元）", "单价(元)", "费用合计", "收费", "计费", "费率"),
            kw_boost=0.25, require_keyword=True,
            context_chars=200,
            anti_keywords=("统一社会信用代码", "证件号码", "身份证", "证件号",
                           "纳税人识别号", "银行账号", "账号", "合同编号",
                           "协议编号", "工单号", "发票号码"),
        ),
        RegexSpec(
            "AMOUNT",
            r"(?<![A-Z0-9])(?:US\$|CA\$|A\$|C\$|USD|EUR|GBP|HKD|SGD|AUD|CAD|JPY|€|£|\$)"
            r"\s?\d[\d,]*(?:\.\d{1,2})?(?!\d)",
            conf_ok=0.78, conf_weak=0.78,
            keywords=("Fee", "Payment", "Purchase Price", "Consideration", "Amount",
                      "Total", "Rent", "Salary", "Deposit", "Price"),
            kw_boost=0.08,
        ),
        RegexSpec(
            "AMOUNT",
            r"(?<!\d)\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP|HKD|SGD|AUD|CAD|JPY|"
            r"dollars?|euros?|pounds?)(?![A-Z])",
            conf_ok=0.72, conf_weak=0.72,
        ),
        # ---------------- 日期 ----------------
        RegexSpec(
            "DATE",
            r"(?:19|20)\d{2}\s?[年\-/.]\s?\d{1,2}\s?[月\-/.]\s?\d{1,2}\s?日?",
            conf_ok=0.62, conf_weak=0.42, validator=_date_syntax_valid,
            ok_reason="日历日期有效", weak_reason="日历日期无效",
        ),
        RegexSpec(
            "DATE",
            r"(?:19|20)\d{2}\s?年\s?\d{1,2}\s?月(?:\s?\d{1,2}\s?日)?",
            conf_ok=0.62, conf_weak=0.42, validator=_date_syntax_valid,
            ok_reason="日历日期有效", weak_reason="日期无效或仅有年月",
        ),
        RegexSpec(
            "DATE",
            r"(?i)(?<![A-Za-z])(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
            r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+(?:19|20)\d{2}"
            r"(?!\d)",
            conf_ok=0.62, conf_weak=0.42, validator=_date_syntax_valid,
            ok_reason="日历日期有效", weak_reason="日历日期无效",
        ),
        RegexSpec(
            "DATE",
            r"(?i)(?<!\d)\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|"
            r"Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
            r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?,?\s+(?:19|20)\d{2}(?!\d)",
            conf_ok=0.62, conf_weak=0.42, validator=_date_syntax_valid,
            ok_reason="日历日期有效", weak_reason="日历日期无效",
        ),
        RegexSpec(
            "DATE",
            r"(?i)\b(?:date\s+of\s+birth|birth\s+date|dob)\s*[:#]?\s*"
            r"((?:(?:0?[1-9]|[12]\d|3[01])[/.-](?:0?[1-9]|1[0-2])[/.-](?:19|20)\d{2})"
            r"|(?:(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])))"
            r"(?![A-Z0-9/.-])",
            conf_ok=0.70, conf_weak=0.40,
            validator=_date_of_birth_syntax_valid, group=1,
            ok_reason="出生日期日历有效", weak_reason="出生日期日历无效",
        ),
        # ---------------- 车牌 ----------------
        RegexSpec(
            "PLATE",
            r"(?<![A-Z0-9])[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领]"
            r"[A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳领]?(?![A-Z0-9])",
            conf_ok=0.5, conf_weak=0.5, keywords=("车牌", "车辆", "号牌"), kw_boost=0.25,
        ),
        # ---------------- 微信 / QQ ----------------
        RegexSpec(
            "SOCIAL_ACCOUNT",
            r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_\-]{5,19}(?![A-Za-z0-9])",
            conf_ok=0.35, conf_weak=0.35,
            keywords=("微信", "wechat", "WeChat", "微信号"), kw_boost=0.35, require_keyword=True,
        ),
        RegexSpec(
            "SOCIAL_ACCOUNT",
            r"(?<!\d)\d{5,12}(?!\d)",
            conf_ok=0.32, conf_weak=0.32,
            keywords=("QQ", "qq", "Q Q"), kw_boost=0.35, require_keyword=True,
        ),
        # ---------------- IP / URL ----------------
        RegexSpec(
            "IPV4",
            r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?![\d.])",
            conf_ok=0.6, conf_weak=0.6,
        ),
        RegexSpec(
            "URL",
            r"(?i)(?<![A-Z0-9@])(?:https?://|www\.)[^\s<>\"'，。；）)】\]]{4,4096}",
            conf_ok=0.62, conf_weak=0.42, validator=_url_syntax_valid,
            ok_reason="网址结构有效", weak_reason="网址结构可疑",
            strip_chars=".,;:，。；：)）】]",
        ),
        RegexSpec(
            "URL",
            r"(?i)(?<![A-Z0-9@._-])(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:com|cn)(?::\d{1,5})?(?:/[A-Z0-9._~%!$&'()*+,;=:@/?#-]{0,4096})?(?![A-Z0-9-])",
            conf_ok=0.42, conf_weak=0.32, validator=_url_syntax_valid,
            ok_reason="域名结构有效", weak_reason="域名结构可疑",
            strip_chars=".,;:，。；：)）】]",
        ),
        # ---------------- 香港 / 港澳台身份证 ----------------
        RegexSpec(
            "HK_ID_CARD",
            r"(?<![A-Z0-9])[A-Z]{1,2}\d{6}(?:[（(]\d[)）])?(?![0-9A-Za-z])",
            conf_ok=0.78, conf_weak=0.78,
            validator=lambda v: True if re.search(r"[（(]\d[)）]$", v) else None,
            ok_reason="含括号校验位", weak_reason="无括号校验位，疑似其他编号",
            keywords=("香港身份证", "香港居民", "港方", "HKID", "hkid"), kw_boost=0.15,
        ),
        # ---------------- 16 位税务登记证号 ----------------
        RegexSpec(
            "TAX_ID",
            r"(?<![0-9A-Za-z])\d{16}(?![0-9A-Za-z])",
            conf_ok=0.75, conf_weak=0.75,
            keywords=("税务登记", "税号", "国税", "地税", "纳税人"), kw_boost=0.2,
            require_keyword=True,
        ),
        # ---------------- 营业执照编号 / 注册号（15 位）----------------
        RegexSpec(
            "LICENSE_NO",
            r"(?<![\dA-Za-z])\d{15}(?![\dA-Za-z])",
            conf_ok=0.75, conf_weak=0.75,
            keywords=("营业执照", "注册号", "执照编号"), kw_boost=0.2, require_keyword=True,
            anti_keywords=("身份证", "金额", "人民币", "账号", "卡号"),
        ),
        RegexSpec(
            "LICENSE_NO",
            r"(?<![0-9A-Za-z])(?:LAW[- ]?\d{6,12}|\d{17})(?![0-9A-Za-z])",
            conf_ok=0.82, conf_weak=0.82,
            keywords=("律师执业证号", "律师执业编号", "律师证号", "执业证号", "执业编号"),
            kw_boost=0.15, require_keyword=True,
            anti_keywords=("身份证", "金额", "人民币", "账号", "卡号"),
        ),
        RegexSpec(
            "LICENSE_NO",
            r"(?i)\b(?:attorney\s+id(?:\s+no\.?)?|(?:state\s+)?bar\s+(?:no\.?|number)|"
            r"sra\s+(?:no\.?|number|id)|practising\s+certificate\s+(?:no\.?|number))"
            r"\s*[:：]?\s*([A-Z0-9]{4,12})",
            conf_ok=0.82, conf_weak=0.72, group=1,
            ok_reason="执业号标签命中", weak_reason="执业号标签命中但格式可疑",
        ),
        # ---------------- 案件号 / 公文字号 ----------------
        RegexSpec(
            "CASE_NO",
            r"[（(]\s*(?:19|20)\d{2}\s*[)）][\u4e00-\u9fff]{1,10}?\d{1,8}号",
            conf_ok=0.85, conf_weak=0.85,
        ),
        RegexSpec(
            "CASE_NO",
            r"[\u4e00-\u9fff]{2,14}[〔[]\s*(?:19|20)\d{2}\s*[〕]]\s*\d{1,6}号",
            conf_ok=0.85, conf_weak=0.85,
        ),
        # ---------------- 合同 / 发票 / 订单编号 ----------------
        RegexSpec(
            "CONTRACT_NO",
            r"[A-Za-z][0-9A-Za-z\-—/]{4,28}[0-9A-Za-z]",
            conf_ok=0.72, conf_weak=0.72, strip_chars="-—/",
            keywords=("合同编号", "协议编号", "合同号", "订单号", "发票号码", "发票号",
                      "工单号", "单号", "编号", "Contract No", "Contract Number",
                      "Agreement No", "PO No", "Purchase Order", "Invoice No",
                      "Order No", "Reference No", "Ref No", "Contract Reference"),
            kw_boost=0.15, require_keyword=True,
            anti_keywords=("邮箱", "@", "网址", "http", "www"),
        ),
        # ---------------- 密钥 / 凭据（block 级）----------------
        RegexSpec(
            "SECRET",
            r"(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}",
            conf_ok=0.9, conf_weak=0.9,
        ),
        RegexSpec(
            "SECRET",
            r"(?:AKIA|ASIA)[0-9A-Z]{16}",
            conf_ok=0.9, conf_weak=0.9,
        ),
        RegexSpec(
            "SECRET",
            r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
            conf_ok=0.9, conf_weak=0.9,
        ),
        RegexSpec(
            "SECRET",
            r"xox[baprs]-[A-Za-z0-9\-]{10,}",
            conf_ok=0.9, conf_weak=0.9,
        ),
        RegexSpec(
            "SECRET",
            r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----",
            conf_ok=0.95, conf_weak=0.95,
        ),
        RegexSpec(
            "SECRET",
            r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
            conf_ok=0.9, conf_weak=0.9,
        ),
        RegexSpec(
            "SECRET",
            r'''["']?(?i:password|passwd|pwd|passcode|secret|token|api[ _-]?key|access[ _-]?key|client[ _-]?secret|private[ _-]?key|credentials?|session[_-]?(?:id|token))["']?\s*[:=]\s*["']([A-Za-z0-9_./+=~!$%#@^&*?-]{8,})["']''',
            conf_ok=0.9, conf_weak=0.9, group=1,
        ),
        RegexSpec(
            "SECRET",
            r'''["']?(?i:password|passwd|pwd|passcode|secret|token|api[ _-]?key|access[ _-]?key|client[ _-]?secret|private[ _-]?key|credentials?|session[_-]?(?:id|token))["']?\s*[:=]\s*((?=[A-Za-z0-9_./+=~!$%#@^&*?-]{8,}(?=$|[\s;,)\]}]|\.(?=\s|$)))(?:(?=[A-Za-z0-9_./+=~!$%#@^&*?-]{0,80}[0-9_./+=~!$%#@^&*?-])[A-Za-z0-9_./+=~!$%#@^&*?-]{8,}?|(?=[A-Za-z0-9_./+=~!$%#@^&*?-]{0,80}[A-Z])(?=[A-Za-z0-9_./+=~!$%#@^&*?-]{0,80}[a-z])[A-Za-z0-9_./+=~!$%#@^&*?-]{16,}?|[A-Za-z0-9_./+=~!$%#@^&*?-]{24,}?)(?=$|[\s;,)\]}]|\.(?=\s|$)))''',
            conf_ok=0.9, conf_weak=0.9, group=1,
        ),
        RegexSpec(
            "SECRET",
            r'''(?i:\bauthorization\s*:\s*(?:bearer|basic)\s+)([A-Za-z0-9_./+=~!$%#@^&*?-]{8,}?)(?=$|[\s;,)\]}]|\.(?=\s|$))''',
            conf_ok=0.9, conf_weak=0.9, group=1,
        ),
        RegexSpec(
            "SECRET",
            r'''(?i:\bcookie\s*:\s*[^\r\n]*?\b(?:session(?:[_-]?(?:id|token))?|sid|auth(?:orization)?|token|key)\s*=\s*)([A-Za-z0-9_./+=~!$%#@^&*?-]{8,}?)(?=$|[;\s,)}\]]|\.(?=\s|$))''',
            conf_ok=0.9, conf_weak=0.9, group=1,
        ),
        # ---------------- MAC 地址（默认未启用）----------------
        RegexSpec(
            "MAC_ADDRESS",
            r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])",
            conf_ok=0.88, conf_weak=0.88,
        ),
        RegexSpec(
            "ORG_NAME",
            r"(?i)\blaw\s+offices?\s+of\s+[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+){0,3}",
            conf_ok=0.78, conf_weak=0.50,
            ok_reason="Law Offices 结构", weak_reason="Law Offices 后接词不可靠",
        ),
        RegexSpec(
            "ORG_NAME",
            r"(?i)\b[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+){0,2}\s+chambers\b"
            r"(?!\s*(?:street|road|were|are|was))",
            conf_ok=0.72, conf_weak=0.46,
            ok_reason="Chambers 结构", weak_reason="Chambers 需人工核对",
        ),
    ]
    specs.extend(custom_specs)
    detectors: List[Any] = [RegexDetector(s) for s in specs]
    detectors.append(PersonNameDetector())
    detectors.append(EnglishPersonNameDetector())
    detectors.append(DisputePersonNameDetector())
    detectors.append(AddressDetector())
    detectors.append(EnglishAddressDetector())
    detectors.append(OrgNameDetector())
    # 表格列对齐的金额识别（列头是「金额/单价/合计」的整列数字）
    detectors.append(TableAmountDetector())
    return detectors


# ==============================================================================
# 5. 引擎：召回 → 判别 → 消解 → 汇总
# ==============================================================================

@dataclass
class Occurrence:
    page: Optional[int]
    location: str
    block_type: str
    start: int              # 原文全文偏移
    end: int
    context: str
    bbox: Optional[Tuple[float, float, float, float]] = None
    block_index: int = -1        # 归属块序号（改写阶段用）
    local_start: int = 0         # 块内偏移（改写阶段用）
    local_end: int = 0
    text: str = ""               # 该次出现的**真实字面文本**（可能与该实体的代表值不同）
    target: Optional[Dict[str, Any]] = None


@dataclass
class Candidate:
    cid: int
    type_id: str
    label: str
    category: str
    severity: str
    value: str
    masked: str
    confidence: float
    reasons: List[str]
    occurrences: List[Occurrence] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.occurrences)


def _occurrence_target(block: Block, block_index: int, local_start: int,
                       local_end: int) -> Optional[Dict[str, Any]]:
    if block.field_kind == "text" and not block.target_metadata:
        return None
    target: Dict[str, Any] = {
        "kind": block.field_kind,
        "part": block.part_name,
        "block_index": block_index,
        "local_offset": [local_start, local_end],
    }
    target.update(block.target_metadata)
    target["kind"] = block.field_kind
    target["part"] = block.part_name
    target["block_index"] = block_index
    target["local_offset"] = [local_start, local_end]
    if block.revision_units:
        units = block.revision_units[local_start:local_end]
        if (len(units) != local_end - local_start or
                any(not unit.get("writable") for unit in units)):
            target["rewrite_supported"] = False
            target["rewrite_block_reason"] = (
                "敏感内容包含无法直接改写的制表符或换行等控制字符；"
                "请先在Word副本中拆分或处理这处修订。")
        elif target.get("rewrite_supported") is not False:
            target["rewrite_supported"] = True
            segments: List[Dict[str, Any]] = []
            for unit in units:
                node_path = unit.get("node_path")
                node_offset = unit.get("node_offset")
                node_kind = unit.get("node_kind")
                if not node_path or node_offset is None or not node_kind:
                    segments = []
                    break
                if (segments and segments[-1]["node_path"] == node_path and
                        segments[-1]["node_kind"] == node_kind and
                        segments[-1]["end"] == node_offset):
                    segments[-1]["end"] = node_offset + 1
                else:
                    segments.append({"node_path": node_path,
                                     "node_kind": node_kind,
                                     "start": node_offset,
                                     "end": node_offset + 1})
            if segments:
                target["physical_segments"] = segments
    if block.comment_id is not None:
        target["comment_id"] = block.comment_id
    if block.field_kind == "comment_body":
        target["paragraph_index"] = block.part_block_index
    if block.attribute_name:
        target["attribute"] = block.attribute_name
    if block.original_initials:
        target["original_initials"] = block.original_initials
    if block.element_index is not None:
        target["element_index"] = block.element_index
    if block.element_path is not None:
        target["element_path"] = list(block.element_path)
    if block.linked_comment_initials:
        target["linked_comment_initials"] = dict(block.linked_comment_initials)
    return target


@dataclass
class Settings:
    min_confidence: float = 0.5
    min_severity: str = "low"
    disabled: set = field(default_factory=set)
    enabled_only: Optional[set] = None
    allowlist: set = field(default_factory=set)
    extra_specs: List[RegexSpec] = field(default_factory=list)
    extra_type_meta: Dict[str, TypeMeta] = field(default_factory=dict)
    profile: str = "contract"
    context_chars: int = 30
    propagate_names: bool = True   # 已确认人名在全文的其它出现处一并标记（提升表格/落款召回）
    force_enabled: set = field(default_factory=set)  # 强制启用（默认关闭的类型）
    # v1.3.0「低置信可见可勾」：复核路径开启后，min_confidence 以下的候选不再物理删除，
    # 而是保留到 display_min_confidence 下限，由前端标注「把握较低」并默认不勾。
    # 下限以下的候选只计数（engine.last_scan_stats），不静默丢弃。
    # 默认关闭 —— 保持 CLI / 既有回归的行为与计数不变。
    retain_low_confidence: bool = False
    display_min_confidence: float = 0.3


def _norm_key(value: str) -> str:
    """归一化比对键：全角转半角、去空格与分隔符、英文转大写。

    先做 NFKC 归一化，这样「９１１１０１０８ＭＡ…」与「91110108MA…」会被视为同一实体，
    不会因为录入时用了全角字符而拿到两个不同的编号。
    """
    v = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"[\s\-—_()（）]", "", v).upper()


def is_english_dominant(text: str, min_letters: int = 60, min_ratio: float = 0.6) -> bool:
    """按拉丁字母在全部字母中的占比判断英文主导文本，不读取文件名或路径。"""
    letters = [ch for ch in text or "" if ch.isalpha()]
    if len(letters) < min_letters:
        return False
    latin = sum(1 for ch in letters if "A" <= ch <= "Z" or "a" <= ch <= "z")
    return latin / len(letters) >= min_ratio


def _resolve_overlaps(matches: List[RawMatch], type_meta=None) -> List[RawMatch]:
    """重叠消解：按（优先级×置信度、长度）贪心保留互不重叠的候选项。"""
    # 明确标成账号的数字不得仅因碰巧通过 Luhn 就改判为银行卡；明确标成卡号时保留卡类别。
    account_spans = {
        (m.start, m.end, _norm_key(m.value))
        for m in matches
        if m.type_id == "BANK_ACCOUNT" and m.attrs.get("matched_keyword")
    }
    card_labels = {"credit card", "debit card", "card number", "card no", "card #",
                   "银行卡", "卡号", "借记卡", "信用卡"}
    matches = [
        m for m in matches
        if not (
            (m.type_id == "BANK_CARD"
             and (m.start, m.end, _norm_key(m.value)) in account_spans
             and str(m.attrs.get("matched_keyword") or "").casefold() not in card_labels)
            or
            (m.type_id == "USCC"
             and (m.start, m.end, _norm_key(m.value)) in account_spans)
        )
    ]

    def score(m: RawMatch) -> Tuple[float, int]:
        meta = (type_meta or TYPE_META).get(m.type_id)
        prio = meta.priority if meta else 10
        return (prio * 100 + m.confidence * 100, m.end - m.start)

    ordered = sorted(matches, key=score, reverse=True)
    kept: List[RawMatch] = []
    for m in ordered:
        if any(not (m.end <= k.start or m.start >= k.end) for k in kept):
            continue
        kept.append(m)
    return kept


# ==============================================================================
# 5.5 实体归并 & 占位符编号
# ------------------------------------------------------------------------------
# 目标：**同一个人 / 同一个企业（含简称）在整篇合同里必须共用同一个编号**。
#       张三在第 1 段和第 30 段都要写成 [自然人姓名#1]；
#       北京星海智能科技有限公司 与其简称「星海智能」都要写成 [组织机构名称#1]。
#
# 为什么重要：如果每出现一次就换一个编号，一篇合同会冒出几十个「人1、人2……人37」，
#            审核人无法判断「人7 到底是不是刚才的人1」，脱敏稿就失去可读性。
#
# 实现要点：
#   1. 编号只跟「实体」绑定，与出现次数无关；
#   2. 编号按该实体在**文档中首次出现的位置**顺序分配（与调用方迭代顺序无关），
#      因此 detector / 改写器 / 复核页三处算出来的编号必然一致；
#   3. 组织机构的全称与简称做归并，简称来源见 `_propagate_org_aliases`。
# ==============================================================================

# 行政区划前缀（剥离后得到机构「字号」，用于把简称并到全称）
ORG_REGION_PREFIXES: Tuple[str, ...] = (
    "中华人民共和国", "中国", "中华", "全国",
    "北京", "上海", "天津", "重庆",
    "广东", "广西", "深圳", "广州", "珠海", "东莞", "佛山", "中山", "惠州",
    "江苏", "南京", "苏州", "无锡", "常州", "南通", "徐州", "扬州", "昆山",
    "浙江", "杭州", "宁波", "温州", "嘉兴", "绍兴", "金华", "台州", "义乌",
    "山东", "济南", "青岛", "烟台", "潍坊", "临沂",
    "福建", "福州", "厦门", "泉州", "漳州",
    "湖南", "长沙", "湖北", "武汉", "河南", "郑州", "洛阳",
    "河北", "石家庄", "唐山", "四川", "成都", "绵阳",
    "陕西", "西安", "安徽", "合肥", "江西", "南昌",
    "山西", "太原", "辽宁", "沈阳", "大连", "吉林", "长春",
    "黑龙江", "哈尔滨", "云南", "昆明", "贵州", "贵阳", "南宁",
    "海南", "海口", "甘肃", "兰州", "青海", "西宁",
    "宁夏", "银川", "新疆", "乌鲁木齐", "内蒙古", "呼和浩特", "西藏", "拉萨",
)

# 机构名通用后缀（强/弱后缀 + 泛化的「公司」），用于剥离出字号
ORG_GENERIC_SUFFIXES: Tuple[str, ...] = tuple(sorted(
    set(ORG_SUFFIX_STRONG) | set(ORG_SUFFIX_WEAK) | set(EN_ORG_SUFFIXES) | {"公司"},
    key=len, reverse=True,
))

# 剥离机构名前后缀时保留的最小字号长度（避免「中国银行」被剥成「中国」这类泛化词）
ORG_CORE_MIN = 3


# 剥离后不具区分度的「空壳」名字（不能拿来判断两家机构是否同一主体）
ORG_MEANINGLESS = set(ORG_GENERIC_SUFFIXES) | set(ORG_REGION_PREFIXES)

# 无关键词括号定义的「通名 / 地名 / 纯后缀」拒绝集。
# 「字符全含」护栏挡不住这类词——「集成」「系统」的每个字都出现在
# 「…系统集成有限公司」里；但一旦登记成简称，全文的「集成」「系统」
# 都会被替换成机构占位符，那才是真正毁正文的事故。故单列一张拒绝表。
ORG_ALIAS_BANAL = set(ORG_GENERIC_SUFFIXES) | set(ORG_REGION_PREFIXES) | {
    "集成", "系统", "科技", "技术", "智能", "信息", "网络", "电子", "实业", "集团",
    "工程", "建设", "管理", "服务", "咨询", "贸易", "物流", "制造", "设备", "材料",
    "发展", "设计", "销售", "安装", "维修", "运输", "有限", "责任", "股份", "企业",
}

# 无「简称 / 称」关键词的括号定义，形如 （“至诚集成”）。
_ORG_NO_KEY_ALIAS_RE = re.compile(
    r"[（(]\s*[「“\"'‘]\s*([\u4e00-\u9fffA-Za-z0-9]{2,20})\s*[」”\"'’]\s*[)）]"
)

# 护栏①「紧邻」判定时，容许夹在本机构全称与括号之间的连接词
_ORG_ALIAS_GLUE = set("之的与及和、 \u3000:：,，;；")

# 前缀按长度倒序匹配，保证「中华人民共和国」先于「中国」被剥掉
ORG_REGION_PREFIXES_SORTED: Tuple[str, ...] = tuple(
    sorted(ORG_REGION_PREFIXES, key=len, reverse=True)
)


def _strip_org_variants(name: str) -> List[str]:
    """一次剥离能得到的所有结果：去一个行政区划前缀 / 去一个机构后缀。

    前缀排在前面，`org_core` 取第一个即「先剥行政区划、再剥机构类型」，
    最符合中文机构「行政区划 + 字号 + 类型」的构词顺序。
    每种剥离只尝试**最长**的那个匹配词，避免把「上海分公司」切成「上海分」。
    """
    if re.search(r"[A-Z]", name) and not re.search(r"[\u4e00-\u9fff]", name):
        compact = re.sub(r"[^A-Z0-9]", "", name.upper())
        for suffix in sorted(EN_ORG_SUFFIXES, key=len, reverse=True):
            compact_suffix = re.sub(r"[^A-Z0-9]", "", suffix.upper())
            if compact.endswith(compact_suffix) and len(compact) - len(compact_suffix) >= ORG_CORE_MIN:
                return [compact[:-len(compact_suffix)]]
        return []
    out: List[str] = []
    for p in ORG_REGION_PREFIXES_SORTED:
        if name.startswith(p):
            if len(name) - len(p) >= ORG_CORE_MIN:
                out.append(name[len(p):])
            break
    for suf in ORG_GENERIC_SUFFIXES:
        if name.endswith(suf):
            if len(name) - len(suf) >= ORG_CORE_MIN:
                out.append(name[: -len(suf)])
            break
    return out


def org_core(name: str) -> str:
    """取机构「字号」的规范形式（反复剥离行政区划前缀与机构类型后缀）。

    北京星海智能科技有限公司 → 星海智能科技
    中国工商银行股份有限公司   → 工商银行
    中国银行                   → 中国银行
    上海分公司                 → 上海分公司（剥成「分公司」没有区分度，故保留）
    """
    s = (re.sub(r"[^A-Z0-9]", "", name.upper())
         if re.search(r"[A-Z]", name) and not re.search(r"[\u4e00-\u9fff]", name)
         else name)
    for _ in range(3):
        v = [x for x in _strip_org_variants(s) if x not in ORG_MEANINGLESS]
        if not v:
            break
        s = v[0]
    return s


def org_core_variants(name: str, max_depth: int = 3) -> set:
    """返回机构名在**不同剥离顺序**下可能得到的全部「字号」。

    不同顺序结果不同，例如：
        中国工商银行              → 先剥前缀得「工商银行」
        中国工商银行股份有限公司    → 先剥后缀得「中国工商银行」，再剥前缀仍是「工商银行」
    因此判断两家机构是否同指一体时，比较的是「变体集合有没有交集」，
    这样与剥离顺序无关，结果更稳定。
    """
    start = (re.sub(r"[^A-Z0-9]", "", name.upper())
             if re.search(r"[A-Z]", name) and not re.search(r"[\u4e00-\u9fff]", name)
             else name)
    out = {start}
    frontier = [start]
    for _ in range(max_depth):
        nxt: List[str] = []
        for s in frontier:
            for v in _strip_org_variants(s):
                if v not in out:
                    out.add(v)
                    nxt.append(v)
        frontier = nxt
        if not frontier:
            break
    return {v for v in out if len(v) >= ORG_CORE_MIN and v not in ORG_MEANINGLESS}


def _org_same_entity(a: str, b: str) -> bool:
    """判断两个机构名是否指向同一主体（全称 vs 简称）。"""
    va, vb = org_core_variants(a), org_core_variants(b)
    if not va or not vb:
        return False
    if (re.search(r"[A-Z]", a) or re.search(r"[A-Z]", b)) and not (
            re.search(r"[\u4e00-\u9fff]", a) or re.search(r"[\u4e00-\u9fff]", b)):
        def english_core(name: str) -> Tuple[Tuple[str, ...], bool]:
            tokens = tuple(re.findall(r"[A-Z0-9]+", name.upper()))
            core = tokens
            suffixes = sorted(
                {tuple(re.findall(r"[A-Z0-9]+", suffix)) for suffix in EN_ORG_SUFFIXES},
                key=len, reverse=True,
            )
            changed = True
            while changed:
                changed = False
                for suffix in suffixes:
                    if len(core) > len(suffix) and core[-len(suffix):] == suffix:
                        core = core[:-len(suffix)]
                        changed = True
                        break
            return core, core != tokens

        a_core, a_is_legal = english_core(a)
        b_core, b_is_legal = english_core(b)
        a_full = tuple(re.findall(r"[A-Z0-9]+", a.upper()))
        b_full = tuple(re.findall(r"[A-Z0-9]+", b.upper()))
        if a_full == b_full:
            return True
        # Latin-script entities need the same complete business name and an
        # explicit legal suffix on both candidates; shared brand words alone
        # cannot merge separate subsidiaries or companies.
        return bool(a_is_legal and b_is_legal and a_core and a_core == b_core)
    for x in va:
        for y in vb:
            if x == y:
                return True
            short, long_ = (x, y) if len(x) <= len(y) else (y, x)
            # 只认「字号前缀」关系（星海智能 ⊂ 星海智能科技），
            # 不用「任意子串」，避免把「工商」误并到别的机构上。
            if len(long_) >= 4 and long_.startswith(short):
                return True
    return False


def build_entity_number_map(
    items: Iterable[Dict[str, Any]],
) -> Dict[Tuple[str, str], int]:
    """为每个「实体」分配固定编号。

    :param items: detector 报告里的 per-entity 条目（需含 type / value / occurrences）
    :return: ``{(type_id, _norm_key(value)): 编号}``

    同一个实体的不同写法（全称/简称）都会指向同一个编号。
    编号按实体首次出现位置，在各自类型内独立从 1 开始。
    """
    source_items = list(items or ())
    entries: List[Tuple[int, str, str]] = []
    for it in source_items:
        t = it.get("type") or ""
        key = _norm_key(it.get("value") or "")
        if not t or not key:
            continue
        starts = []
        for o in (it.get("occurrences") or ()):
            off = o.get("offset")
            if off:
                starts.append(off[0])
        entries.append((min(starts) if starts else 0, t, key))
    if not entries:
        return {}

    keys: List[Tuple[str, str]] = list(dict.fromkeys((t, k) for _, t, k in entries))
    first_pos: Dict[Tuple[str, str], int] = {}
    for p, t, k in sorted(entries, key=lambda x: x[0]):
        first_pos.setdefault((t, k), p)

    parent: Dict[Tuple[str, str], Tuple[str, str]] = {k: k for k in keys}

    def find(x: Tuple[str, str]) -> Tuple[str, str]:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: Tuple[str, str], b: Tuple[str, str]) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # 根取「首次出现更早」的一个，保证结果稳定
        if first_pos.get(ra, 0) > first_pos.get(rb, 0):
            ra, rb = rb, ra
        parent[rb] = ra

    # 机构保留既有显式简称关系；姓名只接受扫描阶段建立的明确中英别名关系。
    explicit_alias_pairs = set()
    by_entity_key = {key: key for key in keys}
    for item in source_items:
        type_id = item.get("type") or ""
        if type_id not in {"ORG_NAME", "PERSON_NAME"}:
            continue
        alias_key = _norm_key(item.get("value") or "")
        for reason in item.get("reasons") or ():
            reason_text = str(reason)
            if type_id == "ORG_NAME":
                linked = re.search(r"与「(.+?)」同指的", reason_text)
            else:
                linked = re.search(r"与「(.+?)」同指的明确中英实体别名", reason_text)
            target_value = linked.group(1) if linked else ""
            target_key = _norm_key(target_value)
            left = (type_id, alias_key)
            right = (type_id, target_key)
            if alias_key and target_key and left in by_entity_key and right in by_entity_key:
                explicit_alias_pairs.add((left, right))
    for left, right in explicit_alias_pairs:
        union(left, right)

    # 组织机构：全称 / 简称归并
    org_keys = [k for k in keys if k[0] == "ORG_NAME"]
    for i in range(len(org_keys)):
        for j in range(i + 1, len(org_keys)):
            if _org_same_entity(org_keys[i][1], org_keys[j][1]):
                union(org_keys[i], org_keys[j])

    person_by_core: Dict[str, Tuple[str, str]] = {}
    for item in source_items:
        if item.get("type") != "PERSON_NAME":
            continue
        value = str(item.get("value") or "").strip()
        core_value = re.sub(r"(?i)^(?:mr|mrs|ms|miss|dr|prof|esq)\.?\s+", "", value, count=1)
        core = _norm_key(core_value)
        key = ("PERSON_NAME", _norm_key(value))
        if not core or key not in parent:
            continue
        previous = person_by_core.get(core)
        if previous is None:
            person_by_core[core] = key
        else:
            union(previous, key)

    # 编号：同一实体取「最早出现位置」，在类型内按位置排序后从 1 递增
    root_first: Dict[Tuple[str, str], int] = {}
    for k in keys:
        r = find(k)
        p = first_pos.get(k, 0)
        if r not in root_first or p < root_first[r]:
            root_first[r] = p

    by_type: Dict[str, List[Tuple[int, Tuple[str, str]]]] = {}
    for r, p in root_first.items():
        by_type.setdefault(r[0], []).append((p, r))
    numbers: Dict[Tuple[str, str], int] = {}
    for t, lst in by_type.items():
        lst.sort(key=lambda x: x[0])
        for idx, (_p, root) in enumerate(lst, 1):
            numbers[(t, root)] = idx

    return {k: numbers[(k[0], find(k))] for k in keys}


class EntityNumberer:
    """实体 → 编号 的统一入口（detector / 改写器 / 复核页三处共用）。

    只要三处都拿同一份 detector 报告来构造，编号就一定一致。
    """

    def __init__(self, items: Optional[Iterable[Dict[str, Any]]] = None,
                 overrides: Optional[Dict[Tuple[str, str], int]] = None):
        self._map: Dict[Tuple[str, str], int] = build_entity_number_map(items or ())
        if overrides:
            for (tid, value), number in overrides.items():
                self._map[(tid, _norm_key(value))] = number
        self._next: Dict[str, int] = {}
        for (t, _k), n in self._map.items():
            if n > self._next.get(t, 0):
                self._next[t] = n

    def number(self, type_id: str, value: str) -> int:
        """取得（必要时分配）某实体在本类型内的编号。"""
        key = (type_id, _norm_key(value))
        n = self._map.get(key)
        if n is None:
            # 兜底：正常不会走到这里（报告里必有该实体），
            # 万一漏了也保证「同名同号、异名异号」。
            n = self._next.get(type_id, 0) + 1
            self._next[type_id] = n
            self._map[key] = n
        return n

    def placeholder(self, type_id: str, value: str, label: str) -> str:
        return placeholder_text(label, self.number(type_id, value))

    def entities(self) -> List[Dict[str, Any]]:
        """按 (类型, 编号) 汇总，便于写入 mapping 供人核对。"""
        out = [
            {"type": t, "key": k, "number": n}
            for (t, k), n in self._map.items()
        ]
        out.sort(key=lambda x: (x["type"], x["number"]))
        return out


def placeholder_text(label: str, number: int) -> str:
    """占位符的字面形式：``[组织机构名称#1]``。

    全工程只有这一处定义格式，detector / 改写器 / 还原器必须保持一致。
    """
    return f"[{label}#{number}]"


PLACEHOLDER_RE = re.compile(r"\[([^\[\]#\s]{1,20})#(\d{1,6})\]")


def parse_placeholder(value: str) -> Optional[Tuple[str, int]]:
    """解析占位符，返回 (label, number)；不是占位符则返回 None。"""
    m = PLACEHOLDER_RE.fullmatch((value or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


class DetectorEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.type_meta = dict(TYPE_META)
        self.type_meta.update(settings.extra_type_meta)
        for spec in settings.extra_specs:
            if spec.type_id not in self.type_meta:
                label = spec.label or spec.type_id
                self.type_meta[spec.type_id] = TypeMeta(spec.type_id, label, "自定义", "medium", 2, 2, priority=50)
        self.detectors = build_detectors(settings.extra_specs)
        # 最近一次 scan_document 的统计（供调用方读取，不改变 scan_document 的返回类型）
        self.last_scan_stats: Dict[str, Any] = {}
        enable = set(self.type_meta)
        force = settings.force_enabled

        def _on(t: str) -> bool:
            meta = self.type_meta.get(t)
            return (meta.enabled if meta else True) or t in force

        self.active_types = {
            t for t in enable
            if t not in settings.disabled
            and (settings.enabled_only is None or t in settings.enabled_only)
            and _on(t)
        }

    # ------------------------------------------------------------------ 扫描 --
    def scan_document(self, doc: Document) -> List[Candidate]:
        blocks = doc.blocks
        # 拼全文 + 记录块区间
        parts: List[str] = []
        spans: List[Tuple[int, int, Block]] = []
        starts: List[int] = []
        pos = 0
        for b in blocks:
            t = b.text.rstrip("\n")
            parts.append(t)
            spans.append((pos, pos + len(t), b))
            starts.append(pos)
            pos += len(t) + 1
        full_raw = "\n".join(parts)

        nt = NormalizedText(full_raw)
        ctx = ScanContext(nt, nt.norm, blocks)

        raw_matches: List[RawMatch] = []
        for det in self.detectors:
            if getattr(det, "type_id", getattr(getattr(det, "spec", None), "type_id", "")) not in self.active_types:
                continue
            try:
                raw_matches.extend(det.run(ctx))
            except RuleMatchTimeout:
                raise  # 用户自定义正则超时必须中止识别，不能静默丢弃该规则
            except Exception as exc:  # 单个检测器异常不影响整体
                sys.stderr.write(f"[warn] 检测器 {type(det).__name__} 异常：{exc}\n")

        norm_cursor = 0
        for block in blocks:
            raw_block = block.text.rstrip("\n")
            norm_block = NormalizedText(raw_block).norm
            if not norm_block:
                norm_cursor += 1
                continue
            block_start = ctx.text.find(norm_block, norm_cursor)
            if block_start < 0:
                continue
            block_end = block_start + len(norm_block)
            if block.field_kind in ("comment_author", "comment_person_identity") and "COMMENT_AUTHOR_IDENTITY" in self.active_types:
                value = norm_block
                lowered = value.casefold()
                generic = {"reviewer", "author", "administrator", "user", "审阅者", "作者", "管理员"}
                confident = bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", value) or
                                 re.search(r"律师事务所|有限公司|有限责任公司|公司|法务部|合规部", value))
                confidence = 0.42 if lowered in generic else (0.86 if confident else 0.58)
                raw_matches.append(RawMatch(
                    "COMMENT_AUTHOR_IDENTITY", block_start, block_end, confidence,
                    ["来源：DOCX批注人员元数据"], value, {"comment_identity": True}))
            if block.field_kind == "revision_author" and "REVISION_AUTHOR_IDENTITY" in self.active_types:
                value = norm_block
                lowered = value.casefold()
                generic = {"reviewer", "author", "administrator", "user", "审阅者", "作者", "管理员"}
                confident = bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", value) or
                                 re.search(r"律师事务所|有限公司|有限责任公司|公司|法务部|合规部", value))
                confidence = 0.58 if lowered in generic else (0.86 if confident else 0.62)
                raw_matches.append(RawMatch(
                    "REVISION_AUTHOR_IDENTITY", block_start, block_end, confidence,
                    ["来源：DOCX修订人元数据"], value, {"revision_identity": True}))
            if block.field_kind == "comment_body":
                signature_patterns = (
                    (r"(?:^|[\n；;])([^\n；;]{2,30}?律师事务所)([\u4e00-\u9fff]{2,4})律师\s*$", 1, 2),
                    (r"(?:^|[\n；;])([^\n；;]{2,30}?)(?:法务部|合规部)([\u4e00-\u9fff]{2,4})\s*$", 1, 2),
                )
                for pattern, org_group, person_group in signature_patterns:
                    for match in re.finditer(pattern, norm_block):
                        org_start, org_end = match.span(org_group)
                        person_start, person_end = match.span(person_group)
                        raw_matches.append(RawMatch("ORG_NAME", block_start + org_start, block_start + org_end,
                                                     0.74, ["批注署名结构识别"], norm_block[org_start:org_end]))
                        raw_matches.append(RawMatch("PERSON_NAME", block_start + person_start, block_start + person_end,
                                                     0.76, ["批注署名结构识别"], norm_block[person_start:person_end]))
            norm_cursor = block_end + 1

        if self.settings.propagate_names:
            raw_matches.extend(self._propagate_names(raw_matches, ctx))
            if "ORG_NAME" in self.active_types:
                raw_matches.extend(self._propagate_org_aliases(raw_matches, ctx))

        self._link_explicit_bilingual_aliases(raw_matches, ctx, spans)

        return self._postprocess(raw_matches, nt, blocks, starts, spans, full_raw)

    @staticmethod
    def _link_explicit_bilingual_aliases(
            matches: List[RawMatch], ctx: ScanContext,
            spans: List[Tuple[int, int, Block]]) -> None:
        """Link only cross-script names explicitly presented as bilingual aliases."""
        scopes: List[Tuple[int, int, str]] = []
        text = ctx.text
        cursor = 0
        for line in text.splitlines(keepends=True):
            content = line.rstrip("\r\n")
            if content:
                scopes.append((cursor, cursor + len(content), content))
            cursor += len(line)

        table_rows: Dict[Tuple[int, int], List[Tuple[int, int, Block]]] = {}
        for start, end, block in spans:
            ref = block.table_ref
            if ref:
                table_rows.setdefault((ref[0], ref[1]), []).append((start, end, block))
        for rows in table_rows.values():
            start = min(item[0] for item in rows)
            end = max(item[1] for item in rows)
            if end > start:
                scopes.append((start, end, text[start:end]))

        seen_scopes = set()
        person_cn_label = re.compile(
            r"(?i)\b(?:chinese\s+(?:full\s+)?name|name\s*\(\s*chinese\s*\))\b|"
            r"中文姓名|中文名"
        )
        person_latin_label = re.compile(
            r"(?i)\b(?:pinyin\s+name|romanized\s+(?:chinese\s+)?name|english\s+name)\b|"
            r"拼音姓名|英文名"
        )
        org_cn_label = re.compile(r"(?i)\bchinese\s+(?:company\s+)?name\b|中文(?:公司)?名称")
        org_latin_label = re.compile(r"(?i)\benglish\s+(?:legal\s+)?(?:company\s+)?name\b|英文(?:公司)?名称")

        def add_link(left: RawMatch, right: RawMatch) -> None:
            if _norm_key(left.value) == _norm_key(right.value):
                return
            reason_left = f"与「{right.value}」同指的明确中英实体别名"
            reason_right = f"与「{left.value}」同指的明确中英实体别名"
            if reason_left not in left.reasons:
                left.reasons.append(reason_left)
            if reason_right not in right.reasons:
                right.reasons.append(reason_right)

        def is_chinese(match: RawMatch) -> bool:
            return bool(re.search(r"[\u4e00-\u9fff]", match.value))

        def is_latin(match: RawMatch) -> bool:
            return bool(re.search(r"[A-Za-z]", match.value)) and not is_chinese(match)

        for start, end, scope in scopes:
            scope_key = (start, end)
            if scope_key in seen_scopes or end - start > 900:
                continue
            seen_scopes.add(scope_key)
            in_scope = [m for m in matches if start <= m.start and m.end <= end]

            if person_cn_label.search(scope) and person_latin_label.search(scope):
                people = [m for m in in_scope if m.type_id == "PERSON_NAME"]
                chinese_people = { _norm_key(m.value): m for m in people if is_chinese(m) }
                latin_people = { _norm_key(m.value): m for m in people if is_latin(m) }
                explicit_latin = []
                for match in latin_people.values():
                    label = str(match.attrs.get("label") or "").casefold()
                    if label in {"pinyin name", "romanized name", "romanized chinese name", "english name"}:
                        explicit_latin.append(match)
                        continue
                    tail = text[match.end:min(end, match.end + 180)]
                    group = re.match(r"\s*\(([^()]*)\)", tail)
                    if group and person_cn_label.search(group.group(1)) and person_latin_label.search(group.group(1)):
                        explicit_latin.append(match)
                if len(chinese_people) == 1 and explicit_latin:
                    chinese_person = next(iter(chinese_people.values()))
                    for latin_person in explicit_latin:
                        add_link(chinese_person, latin_person)

            orgs = [m for m in in_scope if m.type_id == "ORG_NAME"]
            chinese_orgs = [m for m in orgs if is_chinese(m)]
            latin_orgs = [m for m in orgs if is_latin(m)]
            if (len({_norm_key(m.value) for m in chinese_orgs}) == 1 and
                    len({_norm_key(m.value) for m in latin_orgs}) == 1 and
                    org_cn_label.search(scope) and org_latin_label.search(scope)):
                add_link(chinese_orgs[0], latin_orgs[0])

            if (len({_norm_key(m.value) for m in chinese_orgs}) == 1 and
                    len({_norm_key(m.value) for m in latin_orgs}) == 1):
                chinese_org = chinese_orgs[0]
                latin_org = latin_orgs[0]
                for opening in re.finditer(r"\(", scope):
                    closing = scope.find(")", opening.end())
                    if closing < 0:
                        continue
                    inner = scope[opening.end():closing].strip().strip("\"'“”‘’ ")
                    inner_key = _norm_key(inner)
                    if inner_key != _norm_key(latin_org.value):
                        continue
                    absolute_open = start + opening.start()
                    absolute_close = start + closing + 1
                    gap = text[chinese_org.end:absolute_open] if chinese_org.end <= absolute_open else ""
                    if (chinese_org.end <= absolute_open and
                            not gap.strip(" \t,:：\"'“”‘’")):
                        add_link(chinese_org, latin_org)
                        break
                    if (absolute_close <= chinese_org.start and
                            not text[absolute_close:chinese_org.start].strip(" \t,:：\"'“”‘’")):
                        add_link(chinese_org, latin_org)
                        break

    @staticmethod
    def _propagate_org_aliases(matches: List[RawMatch],
                               ctx: ScanContext) -> List[RawMatch]:
        """组织机构简称对齐。

        合同首部常写「北京星海智能科技有限公司（以下简称"星海智能"）」，
        正文则一律使用简称。若只识别全称，简称就会漏检/或被当成别的主体，
        导致同一家公司在脱敏稿里拿到不同编号。

        这里为「已确认的机构全称」补充两类简称命中：
          1. 紧跟全称之后的显式定义 ``（以下简称"X"）``；
          2. 由全称推算的「字号」（去掉行政区划与通用后缀），如 星海智能、星海智能科技公司。
        命中只作为候选加入，最终编号由 build_entity_number_map 归并到全称。
        """
        text = ctx.text
        orgs = [
            m for m in matches
            if m.type_id == "ORG_NAME" and len(m.value) >= 5 and m.confidence >= 0.5
            and ORG_SPAN_REVIEW_REASON not in m.reasons
        ]
        if not orgs:
            return []

        table_rows: Dict[Tuple[int, int], List[Tuple[int, int, Block, int, int]]] = {}
        cursor = 0
        for block in ctx.blocks:
            normalized = NormalizedText(block.text.rstrip("\n")).norm
            if not normalized:
                cursor += 1
                continue
            block_start = ctx.text.find(normalized, cursor)
            if block_start < 0:
                continue
            block_end = block_start + len(normalized)
            if block.table_ref:
                table_id, row_id, col_start, col_span = block.table_ref
                table_rows.setdefault((table_id, row_id), []).append(
                    (col_start, col_span, block, block_start, block_end))
            cursor = block_end + 1

        table_aliases: Dict[Tuple[int, int], List[str]] = {}
        legal_labels = {"legal name", "registered name", "company name", "organization name"}
        alias_labels = {"short name", "alias", "trading as", "doing business as", "abbreviation"}
        for (table_id, header_row), cells in table_rows.items():
            legal_headers = [cell for cell in cells if cell[2].text.strip().casefold() in legal_labels]
            alias_headers = [cell for cell in cells if cell[2].text.strip().casefold() in alias_labels]
            if not legal_headers or not alias_headers:
                continue
            value_rows = table_rows.get((table_id, header_row + 1), [])
            for legal_header in legal_headers:
                for alias_header in alias_headers:
                    if legal_header is alias_header:
                        continue
                    legal_values = [cell for cell in value_rows
                                    if cell[0] < legal_header[0] + legal_header[1] and
                                    legal_header[0] < cell[0] + max(1, cell[1])]
                    alias_values = [cell for cell in value_rows
                                    if cell[0] < alias_header[0] + alias_header[1] and
                                    alias_header[0] < cell[0] + max(1, cell[1])]
                    for legal_value in legal_values:
                        company_match = next((org for org in orgs
                                              if legal_value[3] <= org.start and
                                              org.end <= legal_value[4]), None)
                        if company_match is None:
                            continue
                        for alias_value in alias_values:
                            alias = alias_value[2].text.strip()
                            if (alias and len(alias) <= 24 and
                                    re.fullmatch(r"[A-Za-z0-9&.'’()\- ]+", alias)):
                                table_aliases.setdefault((company_match.start, company_match.end), []).append(alias)

        blockers = list(matches)
        seen: set = set()
        extra: List[RawMatch] = []

        def _covered(s: int, e: int) -> bool:
            return any(m.start <= s and e <= m.end for m in blockers)

        for om in orgs:
            alts: List[Tuple[str, float, str]] = []
            for alias in table_aliases.get((om.start, om.end), []):
                alts.append((alias, 0.9, "同一主体表格中的Short Name/Alias字段"))
            # 1) 显式简称定义
            # 窗口取全称之后 120 字，并用「下一个机构全称的起点」封顶——
            # 既覆盖「全称，其后再定义简称」的写法，又不会把后一家公司的
            # 简称错记到前一家头上（两家主体被并成同一个编号是更严重的错误）。
            next_org_start = min((m.start for m in orgs if m.start >= om.end), default=len(text))
            _zone = text[om.end: min(om.end + 120, next_org_start)]
            # 窗口除按「下一个机构全称的起点」封顶外，同时按句子边界收敛，
            # 避免紧邻的、属于**其它对象**的括号定义被错记成这家公司的简称；
            # 实例：「宏远档案管理（上海）有限公司（“宏远”）将按本合同随附或引用之附表
            #       提供服务（下称“附表”），客户则…」→「附表」被登记为宏远公司的简称。
            # 这里把窗口收敛到「全称所在句内」：遇到句末标点或换行即停。
            _b = re.search(r"[。；;！？!?\n]", _zone)
            tail = _zone[:_b.start()] if _b else _zone
            if om.attrs.get("script") == "latin":
                # 双语合同常写作「[Acme Technologies Limited]（“ACME”）」；
                # 全称后可能有一层方括号，但不要求出现“以下简称”。
                for dm in re.finditer(
                    r"^[\]】]?\s*[（(]\s*(?:the\s+)?[“\"'‘]([A-Za-z][A-Za-z0-9&.-]{1,19})[”\"'’]\s*[)）]",
                    tail,
                ):
                    alts.append((dm.group(1), 0.92, "英文主体简称定义"))
            for dm in re.finditer(
                r"[（(]\s*(?:以下|下)?\s*(?:简称|称)\s*[:：]?\s*[「“\"'‘]?"
                r"([\u4e00-\u9fffA-Za-z0-9]{2,20})\s*[」”\"'’]?\s*[)）]",
                tail,
            ):
                alts.append((dm.group(1), 0.9, "显式简称定义"))
            # 无「简称 / 称」关键词的括号定义 —— 形如「公司全称（“至诚集成”）」。
            # 合同里 (…“X”) 是通用定义语法，多数时候定义的是文件 / 期限 / 角色而非
            # 公司简称（实测 10 份文书 9 个词条中 6 个非敏感），故必须串上四道护栏：
            #   ① 紧邻：括号须紧贴本机构全称（中间只容 之/的/与/及/和/、 与空白）；
            #   ② 字符全含：引号内**每个字**都出现在全称里 —— 用「全含」而非重合率，
            #      否则 2 字真简称（如「宏远」）会被误杀；
            #   ③ 黑名单：复用 R7 的 _is_nonparty_alias()（下方统一出口还会再查一次）；
            #   ④ 通名 / 地名：单独成词的 集成/系统/公司/上海 一类一律拒，
            #      否则会把全文的「集成」「广州」整体替换掉。
            if om.attrs.get("script") != "latin":
                for dm in _ORG_NO_KEY_ALIAS_RE.finditer(tail):
                    alias = dm.group(1)
                    left = text[:om.end + dm.start()]
                    hit = om.value if left.endswith(om.value) else ""
                    if not hit:
                        # 容许夹 ≤2 个连接词（「的」「与」「和」…）
                        cut = left
                        for _ in range(2):
                            if cut and cut[-1] in _ORG_ALIAS_GLUE:
                                cut = cut[:-1]
                                if cut.endswith(om.value):
                                    hit = om.value
                                    break
                            else:
                                break
                    if not hit:
                        continue                                   # ① 未紧邻本机构全称
                    if not all(ch in hit for ch in alias):
                        continue                                   # ② 字符非全含
                    if _is_nonparty_alias(alias):
                        continue                                   # ③ 文件 / 条款 / 期限 / 角色词
                    if alias in ORG_ALIAS_BANAL:
                        continue                                   # ④ 通名 / 地名 / 纯后缀
                    alts.append((alias, 0.86, "无关键词括号定义"))
            # 2) 字号 / 字号+公司
            core = org_core(_norm_key(om.value))
            if len(core) >= 4:
                alts.append((core, 0.72, "字号"))
                alts.append((core + "公司", 0.7, "字号+公司"))
            elif len(core) >= 2:
                alts.append((core, 0.66, "字号"))

            full_key = _norm_key(om.value)
            for alt, conf, why in alts:
                if not alt or _norm_key(alt) == full_key:
                    continue
                # 文件 / 条款 / 期限 / 角色词不得当作机构简称。
                if _is_nonparty_alias(alt):
                    continue
                start = 0
                while True:
                    i = text.find(alt, start)
                    if i < 0:
                        break
                    start = i + 1
                    e = i + len(alt)
                    # 边界：只对「字母/数字」做邻接检查（避免切出半个英文词）。
                    # 中文词间没有空格，不能再要求邻字不是汉字，否则
                    # 「向星海智能提交报告」这类正常行文会被误杀。
                    if i > 0 and re.match(r"[A-Za-z0-9]", text[i - 1]):
                        continue
                    if e < len(text) and re.match(r"[A-Za-z0-9]", text[e]):
                        continue
                    if (i, e) in seen or _covered(i, e):
                        continue
                    seen.add((i, e))
                    extra.append(RawMatch(
                        "ORG_NAME", i, e, conf,
                        [f"与「{om.value}」同指的{why}"], text[i:e],
                        {"alias_of": om.value},
                    ))
        return extra

    @staticmethod
    def _propagate_names(matches: List[RawMatch], ctx: ScanContext) -> List[RawMatch]:
        """人名没有可校验的固定模式，靠标签只能抓到带标签的那几处。
        对已高置信确认的人名，回扫全文把其它出现位置也标出来（常见于表格、落款、账号名）。"""
        confirmed = {
            (m.value, m.attrs.get("script") == "latin") for m in matches
            if m.type_id == "PERSON_NAME" and m.confidence >= 0.7 and
            ((m.attrs.get("script") == "latin" and 5 <= len(m.value) <= 60 and
              len(re.findall(r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*", m.value)) >= 2) or
             (m.attrs.get("script") != "latin" and 2 <= len(m.value) <= 4))
        }
        extra: List[RawMatch] = []
        if not confirmed:
            return extra
        # 只有「可信的」既有匹配才占位；低分姓名候选不该挡住传播
        blockers = [m for m in matches
                    if not (m.type_id == "PERSON_NAME" and m.confidence < 0.6)]
        for name, is_latin in confirmed:
            boundary = r"A-Za-zÀ-ÖØ-öø-ÿ0-9'’\-"
            pattern = re.compile(
                r"(?<![" + boundary + r"])" + re.escape(name) +
                r"(?![" + boundary + r"])", re.IGNORECASE
            ) if is_latin else re.compile(re.escape(name))
            for hit in pattern.finditer(ctx.text):
                i, end = hit.span()
                if any(m.start <= i and end <= m.end for m in blockers):
                    continue
                if not is_latin and i > 0 and end < len(ctx.text):
                    before_char, after_char = ctx.text[i - 1], ctx.text[end]
                    before_is_han = "\u4e00" <= before_char <= "\u9fff"
                    after_is_han = "\u4e00" <= after_char <= "\u9fff"
                    if before_is_han and after_is_han:
                        before = ctx.text[max(0, i - 16):i]
                        after = ctx.text[end:end + 8]
                        has_role_prefix = any(
                            before.endswith(prefix) for prefix in _NAME_REPEAT_CONTEXT_PREFIXES
                        )
                        has_honorific = after.startswith(("先生", "女士", "老师", "老師", "律师"))
                        has_role_prefix = has_role_prefix or before.endswith("律师")
                        has_action_context = (
                            before.endswith(("向", "给", "為", "为", "由"))
                            and any(after.startswith(tail) for tail in _NAME_ACTION_TAILS)
                        )
                        if not (has_role_prefix or has_honorific or has_action_context):
                            continue
                extra.append(RawMatch("PERSON_NAME", i, end, 0.72,
                                      ["与已确认姓名一致的另一处出现"], hit.group(0),
                                      {"propagated": True, **({"script": "latin"} if is_latin else {})}))
        return extra

    # -------------------------------------------------------------- 后处理 --
    def _postprocess(self, matches: List[RawMatch], nt: NormalizedText,
                     blocks: List[Block], starts: List[int],
                     spans: List[Tuple[int, int, Block]], full_raw: str) -> List[Candidate]:
        st = self.settings

        # 1) 类型过滤
        matches = [m for m in matches if m.type_id in self.active_types]

        # 2) 占位符 / 白名单过滤
        filtered: List[RawMatch] = []
        for m in matches:
            raw_value = nt.raw_slice(m.start, m.end)
            if m.type_id == "URL":
                raw_start, _raw_end = nt.to_raw_span(m.start, m.end)
                block_index = bisect.bisect_right(starts, raw_start) - 1
                if (0 <= block_index < len(blocks) and
                        blocks[block_index].field_kind == "external_relationship_target"):
                    continue
            # 400 / 800 服务热线单独放行（如 800 800 8888 只含 0 和 8，会被误判成占位数据）
            if is_placeholder(raw_value) and not (
                    m.type_id == "PHONE_LANDLINE" and HOTLINE_RE.match(raw_value.strip())):
                continue
            if _norm_key(raw_value) in {_norm_key(a) for a in st.allowlist}:
                continue
            m.value = raw_value
            filtered.append(m)

        # 2.5) 真空区：表格上下文过滤
        #      「路径①」全文正则在**表格单元格**上产出的 AMOUNT 候选不受列约束；一旦
        #      TableAmountDetector 因该表无任何金额类列头而整体退场，数量列数字就会被
        #      误报为金额。这里按「列头语义」对这些候选做**保守剔除**——四条同时成立才剔：
        #        ① 所属块是 table_cell 且带 table_ref；
        #        ② 所在 grid 列的列头命中非金额语义（数量/单位/规格/序号…）；
        #        ③ 候选值是**纯千分位数字**（无单位、无货币符号）；
        #        ④ 所在单元格文本**不含**货币符号/金额单位。
        #      豁免：路径②产出（attrs 带 "table"）、列头行自身（ri==1）、非表格块——一律不动。
        header_of = _table_column_context(blocks)
        if header_of:
            kept: List[RawMatch] = []
            for m in filtered:
                if m.type_id != "AMOUNT" or "table" in m.attrs:
                    kept.append(m)
                    continue
                rs, _r_end = nt.to_raw_span(m.start, m.end)
                bi = bisect.bisect_right(starts, rs) - 1
                blk = blocks[bi] if 0 <= bi < len(blocks) else None
                ref = getattr(blk, "table_ref", None) if blk is not None else None
                if ref is None:
                    kept.append(m)
                    continue
                ti, ri, c_start, c_span = ref
                if ri == 1:                       # 列头行自身不作为数据剔除
                    kept.append(m)
                    continue
                hmap = header_of.get(ti)
                if not hmap:
                    kept.append(m)
                    continue
                htext = " ".join(hmap.get(gc, "") for gc in range(c_start, c_start + c_span))
                cell = getattr(blk, "text", "") or ""
                if (_AMOUNT_NON_COL_HEADER_RE.search(htext)
                        and _AMOUNT_PURE_NUM_RE.match(m.value)
                        and not _AMOUNT_CELL_MONEY_RE.search(cell)):
                    continue                     # 剔除：明确非金额列里的裸数字
                kept.append(m)
            filtered = kept

        # 3) 置信度门槛
        # v1.3.0「低置信可见可勾」：复核路径（retain_low_confidence=True）下，低于
        # min_confidence 的候选不再从结果里删除，而是保留到 display_min_confidence 下限，
        # 由前端标注「把握较低」并默认不勾；下限以下仍不展示，但按页计数、不静默丢弃
        # （计数写入 last_scan_stats，供页面提示和导出前清单使用）。
        # 默认（retain_low_confidence=False）保持旧行为：< min_confidence 一律删除。
        floor = st.min_confidence
        if st.retain_low_confidence:
            floor = min(st.min_confidence, st.display_min_confidence)
        below_floor_matches = [m for m in filtered if m.confidence < floor]
        below_floor_pages: Dict[Any, Dict[str, Any]] = {}
        for match in below_floor_matches:
            raw_start, _ = nt.to_raw_span(match.start, match.end)
            block_index = bisect.bisect_right(starts, raw_start) - 1
            page = None
            if 0 <= block_index < len(spans):
                page = spans[block_index][2].page
            page_key = page if isinstance(page, int) else None
            page_stats = below_floor_pages.setdefault(
                page_key, {"page": page_key, "count": 0, "by_type": {}})
            page_stats["count"] += 1
            page_stats["by_type"][match.type_id] = (
                page_stats["by_type"].get(match.type_id, 0) + 1)
        below_floor = len(below_floor_matches)
        filtered = [m for m in filtered if m.confidence >= floor]

        # 4) 重叠消解
        # v1.3.0：在「保留低置信后的全集」上消解，避免同一位置出现一高一低两条重复候选。
        filtered = _resolve_overlaps(filtered, self.type_meta)

        self.last_scan_stats = {
            "retain_low_confidence": bool(st.retain_low_confidence),
            "min_confidence": st.min_confidence,
            "display_floor": floor,
            # 低于展示下限（默认 0.3）的规则命中数，不代表去重后的候选数
            "below_display_floor": below_floor,
            "below_display_floor_by_page": [
                below_floor_pages[page] for page in sorted(
                    below_floor_pages, key=lambda value: (value is None, value or 0))],
        }

        # 5) 严重度过滤
        filtered = [
            m for m in filtered
            if SEVERITY_ORDER.get(self.type_meta.get(m.type_id, TypeMeta(m.type_id, "", "", "low")).severity, 1)
            >= SEVERITY_ORDER.get(st.min_severity, 1)
        ]

        # 6) 分组聚合
        groups: Dict[Tuple[str, str], Candidate] = {}
        seen_physical_targets: Dict[Tuple[str, str], set] = {}
        order: List[Candidate] = []
        cid = 0
        for m in filtered:
            meta = self.type_meta[m.type_id]
            key = (m.type_id, _norm_key(m.value))
            cand = groups.get(key)
            if cand is None:
                cid += 1
                cand = Candidate(cid, m.type_id, meta.label, meta.category, meta.severity,
                                 m.value, mask_value(m.value, m.type_id),
                                 round(m.confidence, 3), list(dict.fromkeys(m.reasons)))
                groups[key] = cand
                order.append(cand)
            else:
                cand.confidence = round(max(cand.confidence, m.confidence), 3)
                for r in m.reasons:
                    if r not in cand.reasons:
                        cand.reasons.append(r)

            # 定位：归一化偏移 → 原文偏移 → 归属块
            rs, re_ = nt.to_raw_span(m.start, m.end)
            bi = bisect.bisect_right(starts, rs) - 1
            if bi < 0:
                bi = 0
            b_start, b_end, block = spans[bi]
            local_s = max(0, rs - b_start)
            local_e = min(len(block.text), max(local_s, re_ - b_start))
            target = _occurrence_target(block, bi, local_s, local_e)
            physical_segments = (target or {}).get("physical_segments") or []
            if physical_segments:
                signature = tuple(
                    (target.get("part", ""), segment["node_path"],
                     segment["node_kind"], segment["start"], segment["end"])
                    for segment in physical_segments)
                seen = seen_physical_targets.setdefault(key, set())
                if signature in seen:
                    continue
                seen.add(signature)
            occurrence_text = full_raw[rs:re_]
            if block.field_kind == "external_relationship_target":
                if (len(block.target_offset_map) != len(block.text) or
                        local_s >= local_e):
                    continue
                raw_s = block.target_offset_map[local_s][0]
                raw_e = block.target_offset_map[local_e - 1][1]
                target["local_offset"] = [raw_s, raw_e]
                target["scan_local_offset"] = [local_s, local_e]
                target["display_text"] = full_raw[rs:re_]
                occurrence_text = block.target_original[raw_s:raw_e]
                context = "外部超链接目标候选"
                local_s, local_e = raw_s, raw_e
            else:
                left = full_raw[max(0, rs - st.context_chars):rs].replace("\n", " ")
                right = full_raw[re_:re_ + st.context_chars].replace("\n", " ")
                context = f"{left}【{m.value}】{right}"
            cand.occurrences.append(Occurrence(
                page=block.page, location=block.location, block_type=block.block_type,
                start=rs, end=re_, context=context,
                bbox=block.bbox_for_range(local_s, local_e),
                block_index=bi, local_start=local_s, local_end=local_e,
                text=occurrence_text, target=target,
            ))

        # 7) 姓名与身份证号互相增强（跨行也能认出「张三 / 身份证号：...」）
        id_spans = [m for m in filtered if m.type_id in ("ID_CARD", "ID_CARD_15")]
        if id_spans:
            for cand in order:
                if cand.type_id != "PERSON_NAME":
                    continue
                for occ in cand.occurrences:
                    if any(abs(o - occ.start) <= 60 for o in
                           [nt.to_raw_span(m.start, m.end)[0] for m in id_spans]):
                        cand.confidence = round(min(cand.confidence + 0.08, 0.99), 3)
                        if "邻近身份证号，佐证为自然人" not in cand.reasons:
                            cand.reasons.append("邻近身份证号，佐证为自然人")
                        break

        order.sort(key=lambda c: (-SEVERITY_ORDER.get(c.severity, 1), -c.confidence, -c.count, c.cid))
        for i, c in enumerate(order, 1):
            c.cid = i
        return order


# ==============================================================================
# 6. 输出层
# ==============================================================================

def build_report(doc: Document, candidates: List[Candidate], settings: Settings) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    total_occ = 0
    for c in candidates:
        by_type[c.type_id] = by_type.get(c.type_id, 0) + c.count
        by_severity[c.severity] = by_severity.get(c.severity, 0) + c.count
        total_occ += c.count
    return {
        "schema": "contract-sensitive-candidates/v1",
        "engine": {"name": ENGINE_NAME, "version": __version__, "mode": "offline"},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "path": os.path.abspath(doc.source),
            "name": os.path.basename(doc.source),
            "type": doc.file_type,
            "sha256": doc.sha256,
            "blocks": len(doc.blocks),
            "chars": len(doc.text),
        },
        "settings": {
            "profile": settings.profile,
            "min_confidence": settings.min_confidence,
            "min_severity": settings.min_severity,
            "disabled_types": sorted(settings.disabled),
        },
        "warnings": doc.warnings,
        "summary": {
            "candidate_types": len(candidates),
            "occurrences": total_occ,
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "by_severity": by_severity,
        },
        "items": [
            {
                "id": c.cid,
                "type": c.type_id,
                "label": c.label,
                "category": c.category,
                "severity": c.severity,
                "value": c.value,
                "masked": c.masked,
                "confidence": c.confidence,
                "count": c.count,
                "reasons": c.reasons,
                "sources": list(dict.fromkeys(
                    reason.split("：", 1)[1] for reason in c.reasons if reason.startswith("来源："))),
                "occurrences": [
                    {
                        "page": o.page,
                        "location": o.location,
                        "block_type": o.block_type,
                        "offset": [o.start, o.end],
                        "block_index": o.block_index,
                        "local_offset": [o.local_start, o.local_end],
                        "bbox": list(o.bbox) if o.bbox else None,
                        "context": o.context,
                        # 真实字面文本：同一实体的不同写法（全称/简称、带空格/不带空格）
                        # 靠它才能精确还原，不能只用实体的代表值。
                        "text": o.text or "",
                        "target": o.target,
                    }
                    for o in c.occurrences
                ],
            }
            for c in candidates
        ],
    }


def write_json(report: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def write_csv(report: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "类型", "类型名称", "归类", "严重度", "原文值", "脱敏预览",
                    "置信度", "出现次数", "位置", "页码", "上下文", "判定依据"])
        for it in report["items"]:
            for i, o in enumerate(it["occurrences"], 1):
                w.writerow([
                    it["id"], it["type"], it["label"], it["category"],
                    SEVERITY_LABEL.get(it["severity"], it["severity"]),
                    it["value"], it["masked"], it["confidence"], it["count"],
                    o["location"], o["page"] or "", o["context"],
                    "；".join(it["reasons"]) if i == 1 else "",
                ])


def write_mapping(report: Dict[str, Any], path: str) -> Dict[str, Any]:
    """写入「脱敏 ↔ 原文」双向映射文件，供还原模式按字符占位符回填原文。

    输出 schema（contract-mapping/v1）：
        {
          "version": 1,
          "schema": "contract-mapping/v1",
          "engine": {...},
          "source": {path, sha256, type, ...},
          "generated_at": ...,
          "items": [
            {type, label, severity, category,
             original, placeholder, masked,
             page, block_index, block_location,
             start, end, bbox, confidence}
          ]
        }

    设计原则
    --------
    1. 每个 occurrence 一条记录（不是 per candidate 聚合），保证唯一可定位。
    2. placeholder 是 redactor 在「还原模式」下真正写入 docx 的字符串，
       形如 "[自然人姓名#1]"，与原文任何 token 都不会混淆，还原器找它替换即可。
    3. **同一个实体（同一个人/企业，含简称）的所有 occurrence 共用同一个 placeholder**，
       编号由 build_entity_number_map 统一分配，与改写器、复核页完全一致。
    4. masked 字段保留 detector 默认的格式掩码（如 "张**"），
       仅供人类阅读 + 对老版本 redactor 兼容。
    """
    numberer = EntityNumberer(report["items"])
    items: List[Dict[str, Any]] = []
    for it in report["items"]:
        placeholder_text_ = numberer.placeholder(it["type"], it["value"], it["label"])
        for o in it["occurrences"]:
            items.append({
                "type": it["type"],
                "label": it["label"],
                "severity": it["severity"],
                "category": it["category"],
                # 还原时写入文档的字面文本：取「该次出现的真实文本」，
                # 这样同一实体的不同写法（全称/简称）都能各自还原正确。
                "original": o.get("text") or it["value"],
                "entity_value": it["value"],
                "placeholder": placeholder_text_,
                "masked": it.get("masked") or it["value"],
                "page": o.get("page"),
                "block_index": o.get("block_index"),
                "block_location": o.get("location"),
                "start": o["offset"][0],
                "end": o["offset"][1],
                "bbox": o.get("bbox"),
                "confidence": it["confidence"],
                **({"target": o.get("target")} if report.get("source", {}).get("type") == "docx" and o.get("target") else {}),
                **({"original_initials": (o.get("target") or {}).get("original_initials", "")}
                   if (o.get("target") or {}).get("kind") == "comment_author" else {}),
            })

    # 实体级汇总：一眼看清「一共几种主体、各自用了哪个编号」
    label_by_type = {it["type"]: it["label"] for it in report["items"]}
    counts: Dict[Tuple[str, int], int] = {}
    for rec in items:
        parsed = parse_placeholder(rec["placeholder"])
        if parsed:
            counts[(rec["type"], parsed[1])] = counts.get((rec["type"], parsed[1]), 0) + 1
    entities = [
        {
            "type": t,
            "label": label_by_type.get(t, t),
            "number": n,
            "placeholder": placeholder_text(label_by_type.get(t, t), n),
            "occurrences": c,
        }
        for (t, n), c in sorted(counts.items(), key=lambda x: (x[0][0], x[0][1]))
    ]

    is_docx = report.get("source", {}).get("type") == "docx"
    payload = {
        "version": 2 if is_docx else 1,
        "schema": "contract-mapping/v2" if is_docx else "contract-mapping/v1",
        "engine": report.get("engine", {"name": ENGINE_NAME, "version": __version__, "mode": "offline"}),
        "source": report.get("source", {}),
        "generated_at": report.get("generated_at"),
        "entities": entities,
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def build_annotated_text(doc: Document, candidates: List[Candidate], full_raw: str) -> str:
    marks: List[Tuple[int, int, str]] = []
    for c in candidates:
        for o in c.occurrences:
            marks.append((o.start, o.end, f"<<{c.type_id}#{c.cid}|{c.confidence}|{c.masked}>>"))
    marks.sort(key=lambda x: x[0], reverse=True)
    out = full_raw
    for s, e, tag in marks:
        out = out[:s] + tag + out[e:]
    return out


def build_masked_text(doc: Document, candidates: List[Candidate], full_raw: str) -> str:
    marks = [(o.start, o.end, c.masked) for c in candidates for o in c.occurrences]
    marks.sort(key=lambda x: x[0], reverse=True)
    out = full_raw
    for s, e, rep in marks:
        out = out[:s] + rep + out[e:]
    return out


_REVIEW_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>合同脱敏复核 · __TITLE__</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e5e7eb;--txt:#1f2328;--sub:#6b7280;
--hi:#dc2626;--md:#d97706;--lo:#0284c7;--acc:#2563eb}
*{box-sizing:border-box}
body{margin:0;padding:20px;background:var(--bg);color:var(--txt);font:14px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1280px;margin:0 auto}
h1{font-size:19px;font-weight:500;margin:0 0 4px}
.meta{color:var(--sub);font-size:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sevbtn{border:1px solid var(--line);background:#fff;border-radius:999px;padding:3px 14px;font-size:13px;cursor:pointer;color:var(--txt)}
.sevbtn.on{color:#fff}
.sevbtn.on[data-sev="all"]{background:#334155}
.sevbtn.on[data-sev="high"]{background:var(--hi)}
.sevbtn.on[data-sev="medium"]{background:var(--md)}
.sevbtn.on[data-sev="low"]{background:var(--lo)}
.cat{font-size:12px;color:var(--sub);margin:10px 0 6px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:3px 12px;font-size:13px;cursor:pointer;user-select:none;background:#fff}
.chip .dot{width:8px;height:8px;border-radius:50%}
.chip input{display:none}
.chip.on[data-sev="high"]{border-color:var(--hi);background:#fef2f2}
.chip.on[data-sev="medium"]{border-color:var(--md);background:#fffbeb}
.chip.on[data-sev="low"]{border-color:var(--lo);background:#f0f9ff}
.chip.off{opacity:.45}
.mini{font-size:12px;color:var(--acc);cursor:pointer}
.compare{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.panel h3{margin:0 0 8px;font-size:13px;font-weight:500;color:var(--sub)}
.doc{white-space:pre-wrap;word-break:break-word;line-height:2;font-size:13px}
.loc{color:var(--acc);font-size:12px;margin:12px 0 2px;font-weight:500}
mark{padding:0 2px;border-radius:3px;color:#1f2328}
mark.high{background:#fecaca}
mark.medium{background:#fde68a}
mark.low{background:#bae6fd}
.off{border-bottom:1px dotted #bbb;color:#9ca3af}
button.primary{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:9px 22px;font-size:14px;cursor:pointer}
button.primary:disabled{background:#94a3b8;cursor:default}
#result{font-size:13px}
#result code{background:#f1f5f9;padding:1px 5px;border-radius:4px}
textarea{width:100%;height:120px;font:12px ui-monospace,Menlo,Consolas,monospace;margin-top:8px}
.warn{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;padding:8px 12px;border-radius:8px;margin-bottom:12px;font-size:13px}
@media(max-width:900px){.compare{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>合同脱敏 · 人工复核与确认</h1>
<div class="meta">__META__</div>
<div id="warns"></div>
<div class="card">
  <div class="bar">
    <strong style="font-size:13px">按严重度筛选</strong>
    <span class="sevbtn on" data-sev="all">全部</span>
    <span class="sevbtn" data-sev="high">高</span>
    <span class="sevbtn" data-sev="medium">中</span>
    <span class="sevbtn" data-sev="low">低</span>
    <span style="flex:1"></span>
    <span class="mini" id="selAll">全选类型</span>
    <span class="mini" id="selNone">清空</span>
  </div>
  <div id="types"></div>
</div>
<div class="compare">
  <div class="panel"><h3>脱敏前（原文，高亮 = 将被脱敏，虚线 = 本次不处理）</h3><div id="left" class="doc"></div></div>
  <div class="panel"><h3>脱敏后（预览，随选择实时变化）</h3><div id="right" class="doc"></div></div>
</div>
<div class="card">
  <div class="bar">
    <button class="primary" id="go">确认并执行脱敏改写</button>
    <span id="stat" style="font-size:13px;color:var(--sub)"></span>
  </div>
  <div id="result"></div>
</div>
<div class="meta">本页由 contract_sensitive_detector.py 本地生成：无外部资源、无网络请求（服务模式仅监听 127.0.0.1）。</div>
<script>
const DATA = __PAYLOAD__;
const API = __API__;

let enabled = new Set(DATA.types.map(function(t){return t.id;}));
let sev = 'all';

function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function active(m){return enabled.has(m.t) && (sev==='all' || m.sev===sev);}

function renderTypes(){
  const box = document.getElementById('types'); box.innerHTML='';
  const cats = {};
  DATA.types.forEach(function(t){ (cats[t.cat] = cats[t.cat]||[]).push(t); });
  const rank = {high:0, medium:1, low:2};
  Object.keys(cats).forEach(function(cat){
    const d = document.createElement('div'); d.className='cat'; d.textContent=cat; box.appendChild(d);
    const chips = document.createElement('div'); chips.className='chips';
    cats[cat].slice().sort(function(a,b){return rank[a.sev]-rank[b.sev];}).forEach(function(t){
      const c = document.createElement('label');
      c.className = 'chip ' + (enabled.has(t.id) ? ('on') : 'off');
      c.setAttribute('data-sev', t.sev);
      c.innerHTML = '<input type="checkbox" '+(enabled.has(t.id)?'checked':'')+'>'+
        '<span class="dot" style="background:var(--'+t.sev+')"></span>'+esc(t.label)+
        ' <span style="color:var(--sub)">'+t.count+'</span>';
      c.querySelector('input').addEventListener('change', function(ev){
        if(ev.target.checked){enabled.add(t.id);} else {enabled.delete(t.id);}
        render();
      });
      chips.appendChild(c);
    });
    box.appendChild(chips);
  });
}

function render(){
  document.querySelectorAll('.sevbtn').forEach(function(b){
    b.classList.toggle('on', b.getAttribute('data-sev')===sev);
  });
  renderTypes();
  const left=[], right=[]; let n=0;
  DATA.blocks.forEach(function(b){
    left.push('<div class="loc">'+esc(b.loc)+'</div>');
    right.push('<div class="loc">'+esc(b.loc)+'</div>');
    const ms = b.marks.slice().sort(function(a,c){return a.s-c.s;});
    let pos=0, L='', R='';
    ms.forEach(function(m){
      const ls=m.s-b.start, le=m.e-b.start;
      if(ls<pos) return;
      L += esc(b.text.slice(pos,ls)); R += esc(b.text.slice(pos,ls));
      if(active(m)){
        n++;
        L += '<mark class="'+m.sev+'" title="'+esc(m.label)+' · 置信度 '+m.conf+'">'+esc(b.text.slice(ls,le))+'</mark>';
        R += '<mark class="'+m.sev+'">'+esc(m.masked)+'</mark>';
      } else {
        L += '<span class="off" title="未勾选，本次不脱敏">'+esc(b.text.slice(ls,le))+'</span>';
        R += esc(b.text.slice(ls,le));
      }
      pos = le;
    });
    L += esc(b.text.slice(pos)); R += esc(b.text.slice(pos));
    left.push(L||'&nbsp;'); right.push(R||'&nbsp;');
  });
  document.getElementById('left').innerHTML = left.join('');
  document.getElementById('right').innerHTML = right.join('');
  document.getElementById('stat').textContent = '将脱敏 '+n+' 处，涉及 '+enabled.size+' 类';
}

document.querySelectorAll('.sevbtn').forEach(function(b){
  b.addEventListener('click', function(){ sev = b.getAttribute('data-sev'); render(); });
});
document.getElementById('selAll').addEventListener('click', function(){
  enabled = new Set(DATA.types.map(function(t){return t.id;})); render();
});
document.getElementById('selNone').addEventListener('click', function(){
  enabled = new Set(); render();
});

function selection(){
  const on = DATA.types.filter(function(t){return enabled.has(t.id);});
  let minSev = 'high';
  if (on.some(function(t){return t.sev==='low';})) minSev='low';
  else if (on.some(function(t){return t.sev==='medium';})) minSev='medium';
  return {source: DATA.source, enabled_types: on.map(function(t){return t.id;}),
          min_confidence: DATA.min_confidence, min_severity: minSev};
}

document.getElementById('go').addEventListener('click', async function(){
  const sel = selection();
  const out = document.getElementById('result');
  if(!sel.enabled_types.length){ out.innerHTML='<span style="color:#dc2626">未选择任何类型，请先勾选。</span>'; return; }
  if(API){
    const btn=document.getElementById('go'); btn.disabled=true;
    out.textContent='正在执行脱敏改写…';
    try{
      const r = await fetch(API+'/apply', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(sel)});
      const j = await r.json();
      if(j.ok){
        out.innerHTML='完成：输出 <a href="'+j.download+'"><code>'+esc(j.output)+'</code></a>（替换 '+j.applied+' 处，跳过 '+j.skipped+' 处）';
      } else {
        out.innerHTML='<span style="color:#dc2626">失败：'+esc(j.error||'未知错误')+'</span>';
      }
    }catch(e){ out.innerHTML='<span style="color:#dc2626">请求失败：'+esc(String(e))+'</span>'; }
    btn.disabled=false;
  } else {
    const blob = new Blob([JSON.stringify(sel,null,2)], {type:'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = DATA.selName; a.click();
    out.innerHTML = '已下载选择文件 <code>'+esc(DATA.selName)+'</code>。静态页面不能直接执行改写，请运行下面命令（或用 serve 模式在本页一键执行）：'+
      '<textarea readonly>'+esc(DATA.cmd)+'</textarea>';
  }
});

DATA.warnings.forEach(function(w){
  const d=document.createElement('div'); d.className='warn'; d.textContent=w;
  document.getElementById('warns').appendChild(d);
});
render();
</script>
</div></body></html>
"""


def _build_review_payload(report: "Dict[str, Any]", doc: Document, full_raw: str,
                          out_hint: str) -> Dict[str, Any]:
    """把报告转成复核页所需的最小 JSON（块级文本 + 命中区间）。"""
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    types: Dict[str, Dict[str, Any]] = {}
    for it in report["items"]:
        t = types.setdefault(it["type"], {
            "id": it["type"], "label": it["label"], "cat": it["category"],
            "sev": it["severity"], "count": 0,
        })
        t["count"] += it["count"]
        for o in it["occurrences"]:
            buckets.setdefault(o["block_index"], []).append({
                "s": o["offset"][0], "e": o["offset"][1], "t": it["type"],
                "sev": it["severity"], "label": it["label"],
                "masked": it["masked"], "conf": it["confidence"],
            })

    blocks: List[Dict[str, Any]] = []
    pos = 0
    for b in doc.blocks:
        text = b.text.rstrip("\n")
        blocks.append({"start": pos, "loc": b.location, "text": text,
                       "marks": buckets.get(len(blocks), [])})
        pos += len(text) + 1

    stem = os.path.splitext(os.path.basename(doc.source))[0]
    ext = os.path.splitext(doc.source)[1].lower() or ".txt"
    all_types = ",".join(types)
    return {
        "source": os.path.abspath(doc.source),
        "min_confidence": report["settings"]["min_confidence"],
        "selName": f"{stem}.selection.json",
        "cmd": (f'python contract_redactor.py "{os.path.abspath(doc.source)}" '
                f'--types {all_types} --out "{stem}_脱敏{ext}"'),
        "outHint": out_hint or f"{stem}_脱敏{ext}",
        "warnings": report["warnings"],
        "types": list(types.values()),
        "blocks": blocks,
    }


def write_review_html(report: Dict[str, Any], doc: Document, path: str, full_raw: str,
                      api_base: Optional[str] = None, out_hint: Optional[str] = None) -> None:
    payload = _build_review_payload(report, doc, full_raw, out_hint)
    meta = (f"{html.escape(os.path.basename(doc.source))} · "
            f"{html.escape(report['source']['type'].upper())} · {report['source']['chars']} 字 · "
            f"识别 {len(report['items'])} 类 / {report['summary']['occurrences']} 处 · "
            f"引擎 {html.escape(report['engine']['name'])} v{html.escape(report['engine']['version'])}（离线）· "
            f"生成于 {html.escape(report['generated_at'])}")
    body = (_REVIEW_TEMPLATE
            .replace("__TITLE__", html.escape(os.path.basename(doc.source)))
            .replace("__META__", meta)
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
            .replace("__API__", json.dumps(api_base)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


# ==============================================================================
# 7. 配置 / 白名单
# ==============================================================================

def load_allowlist(path: Optional[str]) -> set:
    if not path:
        return set()
    out = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out


# 命名掩码策略：供规则清单 Markdown 的「掩码参数 mask=xxx」引用
NAMED_MASKS: Dict[str, Callable[[str], str]] = {
    "person": _mask_person,
    "passport": _mask_id_head,
    "hk_id": _mask_hk_id,
    "landline": _mask_landline,
    "address": _mask_address,
    "ipv4": _mask_ipv4,
    "mac": _mask_mac,
    "secret": _mask_secret,
    "amount": _mask_amount,
    "email": _mask_email,
}

_SEV_CN = {"高": "high", "中": "medium", "低": "low"}
_TRUE = ("是", "✓", "x", "true", "y", "yes", "1", "开", "启用")


def _read_rules_text(path: str) -> str:
    """按 UTF-8 严格读取规则文件；编码错误转为可提示的校验错误。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError as exc:
        raise RulesFormatError("规则文件不存在：%s" % path, code="RULES_MISSING") from exc
    except OSError as exc:
        raise RulesFormatError("规则文件无法读取：%s" % exc, code="RULES_MISSING") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesFormatError(
            "规则文件不是UTF-8编码，无法读取。请用文本编辑器把文件另存为UTF-8后重试"
            "（Windows 记事本另存为时选择“UTF-8”，不要选择“ANSI”或“UTF-16”）。",
            code="RULES_ENCODING") from exc


class RulesRow:
    """正式规则表中的一行（原始单元格，未应用）。"""

    __slots__ = ("seq", "cells", "line_no")

    def __init__(self, seq, cells, line_no):
        self.seq = seq
        self.cells = cells
        self.line_no = line_no

    @property
    def type_id(self) -> str:
        return self.cells[1]


class RulesParse:
    """`## 规则表` 下的正式表格解析结果。"""

    def __init__(self, found, rows, fences_present):
        self.found = found
        self.rows = rows
        self.fences_present = fences_present


def _split_rule_row(line: str) -> List[str]:
    return [c.strip().replace("\\|", "|")
            for c in re.split(r"(?<!\\)\|", line.strip("|"))]


def parse_rules_md_text(text: str) -> RulesParse:
    """只解析 `## 规则表` 标题下的连续表格行。

    说明区、示例区和检查清单中的表格与代码围栏不参与解析；
    标题或表格行本身被代码围栏包裹时视为找不到正式表（调用方据此提示）。
    """
    lines = text.splitlines()
    fences_present = False
    fence = False
    heading_idx = None
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("```"):
            fences_present = True
            fence = not fence
            continue
        if fence:
            continue
        if s == "## 规则表":
            heading_idx = idx
            break
    if heading_idx is None:
        return RulesParse(False, [], fences_present)
    rows: List[RulesRow] = []
    header_seen = False
    separator_seen = False
    for idx in range(heading_idx + 1, len(lines)):
        s = lines[idx].strip()
        if s.startswith("```"):
            break  # 围栏开启：正式表区域按结束处理
        if not s:
            if not header_seen:
                continue  # 表头前允许空行
            break     # 表格后的空行/说明文字：正式表结束
        if not s.startswith("|"):
            break
        cells = _split_rule_row(s)
        if not header_seen:
            header_seen = True
            continue  # 表头行
        if not separator_seen:
            separator_seen = True
            continue  # 分隔行
        if len(cells) < 8:
            continue
        if cells[0] in ("#", "") or set(cells[0]) <= set("-: "):
            continue  # 重复表头 / 分隔行
        type_id = cells[1]
        if not type_id or type_id.startswith("-"):
            continue
        try:
            seq = int(cells[0])
        except ValueError:
            seq = None
        rows.append(RulesRow(seq, cells, idx + 1))
    return RulesParse(True, rows, fences_present)


_bundled_rules_cache = None


def bundled_rules_parse() -> RulesParse:
    """解析随包默认模板（scripts 同目录优先，其次 references）。"""
    global _bundled_rules_cache
    if _bundled_rules_cache is None:
        root = os.path.dirname(os.path.abspath(__file__))
        path = None
        for cand in (os.path.join(root, "脱敏规则清单.md"),
                     os.path.join(os.path.dirname(root), "references", "脱敏规则清单.md")):
            if os.path.exists(cand):
                path = cand
                break
        if path is None:
            _bundled_rules_cache = RulesParse(False, [], False)
        else:
            _bundled_rules_cache = parse_rules_md_text(_read_rules_text(path))
    return _bundled_rules_cache


def default_rules_type_ids() -> List[str]:
    """随包模板正式表中的默认类型 ID（保持模板行序）。"""
    return [row.type_id for row in bundled_rules_parse().rows]


def _apply_row_kwargs(row: RulesRow) -> Dict[str, Any]:
    """校验一行必需字段并解析掩码参数；返回可应用到 TypeMeta 的关键字。"""
    cells = row.cells
    sev = _SEV_CN.get(cells[3], cells[3].lower())
    if sev not in ("high", "medium", "low"):
        raise RulesFormatError(
            "第 %d 行（%s）严重度必须是 高 / 中 / 低" % (row.line_no, cells[1]),
            code="RULES_FIELD_INVALID")
    if cells[7] not in ("是", "否"):
        raise RulesFormatError(
            "第 %d 行（%s）“默认启用”必须是 是 / 否" % (row.line_no, cells[1]),
            code="RULES_FIELD_INVALID")
    for name, col in (("类别", 2), ("识别内容", 4), ("掩码方式", 5), ("掩码参数", 6)):
        if not cells[col]:
            raise RulesFormatError(
                "第 %d 行（%s）“%s”列不能为空" % (row.line_no, cells[1], name),
                code="RULES_FIELD_INVALID")
    kw: Dict[str, Any] = {"severity": sev, "enabled": cells[7] == "是"}
    for part in re.split(r"[;；]", cells[6]):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise RulesFormatError(
                "第 %d 行（%s）掩码参数“%s”缺少等号，应为分号分隔的 key=value"
                % (row.line_no, cells[1], part),
                code="RULES_FIELD_INVALID")
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "keep_head":
            try:
                kw["keep_head"] = int(v)
            except ValueError as exc:
                raise RulesFormatError(
                    "第 %d 行（%s）keep_head 不是整数：%s" % (row.line_no, cells[1], v),
                    code="RULES_FIELD_INVALID") from exc
        elif k == "keep_tail":
            try:
                kw["keep_tail"] = int(v)
            except ValueError as exc:
                raise RulesFormatError(
                    "第 %d 行（%s）keep_tail 不是整数：%s" % (row.line_no, cells[1], v),
                    code="RULES_FIELD_INVALID") from exc
        elif k == "replacement":
            if not v:
                raise RulesFormatError(
                    "第 %d 行（%s）replacement 不能为空" % (row.line_no, cells[1]),
                    code="RULES_FIELD_INVALID")
            kw["replacement"] = v
            kw["mask_fn"] = None
        elif k == "mask":
            if v not in NAMED_MASKS:
                raise RulesFormatError(
                    "第 %d 行（%s）未知命名掩码：%s" % (row.line_no, cells[1], v),
                    code="RULES_FIELD_INVALID")
            kw["mask_fn"] = NAMED_MASKS[v]
            kw["replacement"] = None
        else:
            raise RulesFormatError(
                "第 %d 行（%s）未知掩码参数：%s" % (row.line_no, cells[1], k),
                code="RULES_FIELD_INVALID")
    return kw


def find_fenced_rule_ids(text: str) -> List[str]:
    """收集代码围栏内表格样例行出现的类型 ID（区分围栏导致的解析缺失）。"""
    ids: List[str] = []
    fence = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            fence = not fence
            continue
        if not fence or not s.startswith("|"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", s.strip("|"))]
        if len(cells) >= 2 and cells[1] and not cells[1].startswith("-"):
            if cells[0] not in ("#", "") and not set(cells[0]) <= set("-: "):
                ids.append(cells[1])
    return ids


def build_rules_snapshot(path: str, *,
                         previous_custom_ids: Optional[Sequence[str]] = None,
                         reference_text: Optional[str] = None) -> Dict[str, Any]:
    """读取并校验规则文件，返回可直接应用到 Settings 的快照。

    返回：sha256 / text / rows / extra_specs / type_overrides / custom_ids /
    pending_deletions。校验失败抛 RulesFormatError；全程不修改全局 TYPE_META。
    """
    import dataclasses

    text = _read_rules_text(path)
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    parse = parse_rules_md_text(text)
    if not parse.found:
        hint = "正式规则表（## 规则表）未找到。"
        if parse.fences_present:
            hint += "文件中存在代码围栏，请检查“## 规则表”标题或整个表格是否被三个反引号包裹；围栏内内容不参与解析。"
        raise RulesFormatError(hint, code="RULES_TABLE_MISSING")
    if not parse.rows:
        hint = "正式规则表下没有解析到任何规则行。"
        if parse.fences_present:
            hint += "文件中存在代码围栏，请检查表头或规则行是否被三个反引号包裹。"
        raise RulesFormatError(hint, code="RULES_EMPTY")
    seen_seq: Dict[int, int] = {}
    seen_type: Dict[str, int] = {}
    for row in parse.rows:
        if row.seq is None:
            raise RulesFormatError(
                "第 %d 行序号不是数字：%s" % (row.line_no, row.cells[0]),
                code="RULES_FIELD_INVALID")
        if row.seq in seen_seq:
            raise RulesFormatError(
                "第 %d 行与第 %d 行序号重复：%d" % (seen_seq[row.seq], row.line_no, row.seq),
                code="RULES_DUPLICATE")
        seen_seq[row.seq] = row.line_no
        if row.type_id in seen_type:
            raise RulesFormatError(
                "第 %d 行与第 %d 行规则名重复：%s" % (seen_type[row.type_id], row.line_no, row.type_id),
                code="RULES_DUPLICATE")
        seen_type[row.type_id] = row.line_no
    missing_default = [t for t in default_rules_type_ids() if t not in seen_type]
    if missing_default:
        raise RulesFormatError(
            "正式规则表缺少以下默认规则行：" + "、".join(missing_default)
            + "。停用规则请把“默认启用”改为“否”，不要删除整行。",
            code="RULES_MISSING_DEFAULT", missing=missing_default)
    extra: List[RegexSpec] = []
    overrides: Dict[str, TypeMeta] = {}
    custom_ids: List[str] = []
    for row in parse.rows:
        kw = _apply_row_kwargs(row)
        type_id = row.type_id
        if type_id in BUILTIN_TYPE_META:
            overrides[type_id] = dataclasses.replace(BUILTIN_TYPE_META[type_id], **kw)
        else:
            pattern = row.cells[8] if len(row.cells) > 8 else ""
            if not pattern:
                raise RulesFormatError(
                    "第 %d 行新类型 %s 未在最后一列提供正则" % (row.line_no, type_id),
                    code="RULES_REGEX_REQUIRED", missing=[type_id])
            if len(pattern) > RULES_PATTERN_MAX_LEN:
                raise RulesFormatError(
                    "第 %d 行 %s 的正则超过 %d 字符上限"
                    % (row.line_no, type_id, RULES_PATTERN_MAX_LEN),
                    code="RULES_REGEX_INVALID", missing=[type_id])
            if _bounded_regex is None:
                raise RulesFormatError(
                    "运行环境缺少具备超时能力的 regex 组件，用户自定义正则未启用；"
                    "请先安装依赖后重试", code="RULES_BOUNDED_UNAVAILABLE")
            try:
                _bounded_regex.compile(pattern)
            except Exception as exc:
                raise RulesFormatError(
                    "第 %d 行 %s 的正则无法编译：%s" % (row.line_no, type_id, exc),
                    code="RULES_REGEX_INVALID", missing=[type_id]) from exc
            label = row.cells[4][:14] or type_id
            overrides[type_id] = TypeMeta(
                type_id, label, row.cells[2] or "自定义",
                kw.get("severity", "medium"),
                kw.get("keep_head", 2), kw.get("keep_tail", 2),
                priority=50, enabled=kw.get("enabled", True),
                replacement=kw.get("replacement"), mask_fn=kw.get("mask_fn"),
            )
            extra.append(RegexSpec(
                type_id=type_id, pattern=pattern,
                conf_ok=0.7, conf_weak=0.4,
                keywords=("编号", "号码", "账号"), kw_boost=0.1,
                source=USER_RULE_SOURCE, bounded=True,
            ))
            custom_ids.append(type_id)
    pending: List[str] = []
    if previous_custom_ids:
        current = set(custom_ids) | set(overrides)
        pending = [t for t in previous_custom_ids if t not in current]
        if pending:
            fenced = set(find_fenced_rule_ids(
                reference_text if reference_text is not None else text))
            blocked = sorted(set(pending) & fenced)
            if blocked:
                raise RulesFormatError(
                    "以下原有规则仍出现在代码围栏内，未被正式规则表解析："
                    + "、".join(blocked)
                    + "。请去掉包裹表格行的代码围栏后重试。",
                    code="RULES_FENCE", missing=blocked)
    return {
        "sha256": sha,
        "text": text,
        "rows": parse.rows,
        "extra_specs": extra,
        "type_overrides": overrides,
        "custom_ids": custom_ids,
        "pending_deletions": pending,
    }


def load_rules_md(path: str) -> List[RegexSpec]:
    """读取《脱敏规则清单.md》正式规则表：对已有类型覆盖严重度/掩码/启用开关；
    对带正则的新行自动注册为自定义类型。返回新增的 RegexSpec 列表。

    仅解析 `## 规则表` 下的连续表格行；说明区、示例与检查清单中的表格行和
    代码围栏内容不再被当作规则。命令行与改写器沿用“写入全局 TYPE_META”的
    既有约定；Web 服务校验走 build_rules_snapshot，不修改全局状态。
    """
    import dataclasses

    parse = parse_rules_md_text(_read_rules_text(path))
    if not parse.found:
        raise RulesFormatError("正式规则表（## 规则表）未找到", code="RULES_TABLE_MISSING")
    extra: List[RegexSpec] = []
    for row in parse.rows:
        kw = _apply_row_kwargs(row)
        type_id = row.type_id
        if type_id in TYPE_META:
            TYPE_META[type_id] = dataclasses.replace(TYPE_META[type_id], **kw)
        else:
            pattern = row.cells[8] if len(row.cells) > 8 else ""
            if not pattern:
                sys.stderr.write(f"[warn] 规则清单中的未知类型 {type_id} 且未提供正则，已跳过\n")
                continue
            label = row.cells[4][:14] or type_id
            TYPE_META[type_id] = TypeMeta(
                type_id, label, row.cells[2] or "自定义",
                kw.get("severity", "medium"),
                kw.get("keep_head", 2), kw.get("keep_tail", 2),
                priority=50, enabled=kw.get("enabled", True),
                replacement=kw.get("replacement"), mask_fn=kw.get("mask_fn"),
            )
            extra.append(RegexSpec(
                type_id=type_id, pattern=pattern,
                conf_ok=0.7, conf_weak=0.4,
                keywords=("编号", "号码", "账号"), kw_boost=0.1,
                source=USER_RULE_SOURCE, bounded=True,
            ))
    return extra


def load_config(path: Optional[str]) -> Tuple[Settings, List[RegexSpec]]:
    st = Settings()
    extra: List[RegexSpec] = []
    if not path:
        return st, extra
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    st.min_confidence = float(cfg.get("min_confidence", st.min_confidence))
    st.min_severity = cfg.get("min_severity", st.min_severity)
    st.disabled = set(cfg.get("disabled", []))
    st.enabled_only = set(cfg["only"]) if cfg.get("only") else None
    st.allowlist = set(cfg.get("allowlist", []))
    for r in cfg.get("custom_rules", []):
        extra.append(RegexSpec(
            type_id=r["type_id"],
            pattern=r["pattern"],
            conf_ok=float(r.get("confidence", 0.7)),
            conf_weak=float(r.get("confidence_weak", 0.4)),
            keywords=tuple(r.get("keywords", ())),
            kw_boost=float(r.get("kw_boost", 0.15)),
            require_keyword=bool(r.get("require_keyword", False)),
            validator=None,
        ))
        if r["type_id"] not in TYPE_META:
            TYPE_META[r["type_id"]] = TypeMeta(
                r["type_id"], r.get("label", r["type_id"]), "自定义",
                r.get("severity", "medium"), int(r.get("keep_head", 2)), int(r.get("keep_tail", 2)),
                priority=int(r.get("priority", 50)),
            )
    return st, extra


# ==============================================================================
# 8. 离线守卫
# ==============================================================================

def enforce_offline() -> None:
    """禁用 socket，任何联网尝试立即抛错 —— 从机制层面保证不外传。"""
    import socket

    def _deny(*_a, **_k):
        raise RuntimeError("离线模式已开启：本工具禁止任何网络连接")

    socket.socket = _deny          # type: ignore[assignment]
    socket.create_connection = _deny  # type: ignore[assignment]
    socket.getaddrinfo = _deny     # type: ignore[assignment]


# ==============================================================================
# 9. 自检
# ==============================================================================

SAMPLE_CONTRACT = """
技术开发（委托）合同

甲方（委托方）：北京星海智能科技有限公司
统一社会信用代码：91110108MA01KLMNX8
法定代表人：赵启明
地址：北京市海淀区中关村南大街 12 号院 3 号楼 1801 室
联系人：孙丽娟
联系电话：13812345678
电子邮箱：zhao.qiming@xinghai-tech.com.cn
开户银行：中国工商银行股份有限公司北京海淀支行
银行账号：0200049609200123456

乙方（受托方）：深圳市南山区云启软件工作室
纳税人识别号：440300123456789
负责人：欧阳博文
身份证号：440305199001012347
通讯地址：广东省深圳市南山区科技园南路 88 号 A 座 2203
手机号：13900139000
微信号：yunqi_bowen

第一条 合同金额
本合同技术开发费用总额为人民币 1,280,000.00 元（大写：壹佰贰拾捌万元整）。
甲方应于 2026年3月15日 前支付首期款 384,000.00 元至乙方上述账户。

第二条 保密条款
双方对因履行本合同而知悉的对方商业秘密、个人信息负有保密义务。
乙方联系人身份证号：11010519491231002X，护照号：E12345678。
甲方项目经理：陈嘉怡（工号 A20230987），直线电话：0755-86329871。

第三条 争议解决
因本合同发生争议，双方应协商解决；协商不成的，提交北京仲裁委员会仲裁。
合同签署日期：2026年2月28日
"""


# 用于「实体编号一致性」自检的样例：人名/机构名多次出现 + 显式简称 + 两类漏检场景
_ENTITY_SAMPLE = """甲方：北京星海智能科技有限公司（以下简称“星海智能”）
统一社会信用代码：91110108MA8A2X5K3R
联系电话：010-8666 2188
法定代表人：张三
乙方：上海星光数据服务有限公司
授权代表：李四
第一条 星海智能 委托乙方提供数据服务，乙方指派张三 作为项目负责人。
第二条 张三 应于每季度向星海智能 提交报告，同时抄送李四。
第三条 本协议由北京星海智能科技有限公司 与上海星光数据服务有限公司 共同签署。
第四条 星海智能 的联系电话为 010-86662188。
第五条 甲方法定代表人张三 签字，乙方授权代表李四 签字。
"""


def selftest(verbose: bool = True) -> int:
    """内置回归自检：校验算法 + 端到端识别。不需要任何外部文件。"""
    fails: List[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        if not cond:
            fails.append(name)
        if verbose:
            tail = f"  [{extra}]" if extra and not cond else ""
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}{tail}")

    print("== 校验算法 ==")
    check("身份证 11010519491231002X 校验通过", id_card_check("11010519491231002X") is True)
    check("身份证 110101199003070011 校验通过", id_card_check("110101199003070011") is True)
    check("身份证 440305199001012347 校验通过", id_card_check("440305199001012347") is True)
    check("身份证篡改后校验失败", id_card_check("11010519491231002" + "1") is False)
    check("身份证非18位返回 None", id_card_check("44030519900101234") is None)
    check("USCC 91350100M000100Y43 校验通过", uscc_check("91350100M000100Y43") is True)
    check("USCC 91110108MA01KLMNX8 校验通过", uscc_check("91110108MA01KLMNX8") is True)
    check("USCC 91440300MA5EXY9H78 校验通过", uscc_check("91440300MA5EXY9H78") is True)
    check("USCC 篡改后失败", uscc_check("91350100M000100Y44") is False)
    check("USCC 含非法字符 I 返回 None", uscc_check("91350100M000100YI3") is None)
    check("Luhn 4111111111111111 通过", luhn_check("4111111111111111") is True)
    check("Luhn 4111111111111112 失败", luhn_check("4111111111111112") is False)
    check("Luhn 支持空格分组", luhn_check("4111 1111 1111 1111") is True)

    print("== 归一化与下标映射 ==")
    nt = NormalizedText("甲方：１２３４５６７８９０１２３４５６７　张三")
    dm = re.search(r"\d+", nt.norm)
    check("全角数字归一化", bool(dm) and dm.group(0) == "12345678901234567")
    check("全角空格归一化", nt.norm == "甲方:12345678901234567 张三")
    rs, re_ = nt.to_raw_span(dm.start(), dm.end())
    check("归一化下标回映原文正确", nt.raw[rs:re_] == "１２３４５６７８９０１２３４５６７")

    print("== 端到端识别 ==")
    settings = Settings(min_confidence=0.5, min_severity="low")
    engine = DetectorEngine(settings)
    doc = Document(source="<selftest>", file_type="txt",
                   blocks=[Block(text=line, block_type="line", location=f"第 {i} 行")
                           for i, line in enumerate(SAMPLE_CONTRACT.splitlines(), 1) if line.strip()])
    cands = engine.scan_document(doc)
    found = {c.type_id for c in cands}
    values = {c.value for c in cands}

    def has(t: str, v: str) -> bool:
        return any(c.type_id == t and c.value == v for c in cands)

    check("识别统一社会信用代码", has("USCC", "91110108MA01KLMNX8"))
    check("识别身份证号 440305199001012347", has("ID_CARD", "440305199001012347"))
    check("识别身份证号 11010519491231002X", has("ID_CARD", "11010519491231002X"))
    check("识别手机号 13812345678", has("PHONE_MOBILE", "13812345678"))
    check("识别邮箱", any(c.type_id == "EMAIL" and "xinghai-tech.com.cn" in c.value for c in cands))
    check("识别姓名 赵启明", has("PERSON_NAME", "赵启明"))
    check("识别复姓姓名 欧阳博文", has("PERSON_NAME", "欧阳博文"))
    check("识别姓名 陈嘉怡", has("PERSON_NAME", "陈嘉怡"))
    check("识别机构名（含有限公司）", any(c.type_id == "ORG_NAME" and "北京星海智能科技有限公司" in c.value
                                  for c in cands))
    check("识别地址", any(c.type_id == "ADDRESS" and "中关村南大街" in c.value for c in cands))
    check("识别银行账号", any(c.type_id == "BANK_ACCOUNT" for c in cands))
    check("识别金额", any(c.type_id == "AMOUNT" and "1,280,000.00" in c.value for c in cands))
    check("识别大写金额", any(c.type_id == "AMOUNT" and "壹佰贰拾捌万元整" in c.value for c in cands))
    check("识别纳税人识别号", any(c.type_id == "TAX_ID" for c in cands))
    check("识别座机 0755-86329871", any(c.type_id == "PHONE_LANDLINE" and "0755-86329871" in c.value
                                   for c in cands))
    check("识别日期", any(c.type_id == "DATE" for c in cands))
    check("未把『北京仲裁委员会』误判为地址", not any(
        c.type_id == "ADDRESS" and "仲裁委员会" in c.value for c in cands))
    check("占位符被剔除", not any(is_placeholder(c.value) for c in cands))

    print("== 实体编号一致性（同一实体必须同一个 [标签#N]） ==")
    ent_doc = Document(source="<entity-selftest>", file_type="txt",
                       blocks=[Block(text=line, block_type="line", location=f"第 {i} 行")
                               for i, line in enumerate(_ENTITY_SAMPLE.splitlines(), 1)
                               if line.strip()])
    ent_cands = DetectorEngine(settings).scan_document(ent_doc)
    ent_report = build_report(ent_doc, ent_cands, settings)
    numberer = EntityNumberer(ent_report["items"])
    n_zhang = numberer.number("PERSON_NAME", "张三")
    n_li = numberer.number("PERSON_NAME", "李四")
    n_full = numberer.number("ORG_NAME", "北京星海智能科技有限公司")
    n_short = numberer.number("ORG_NAME", "星海智能")
    n_other = numberer.number("ORG_NAME", "上海星光数据服务有限公司")
    check("同一个人名只占一个号（张三）", n_zhang == 1, f"张三={n_zhang}")
    check("不同人名各自占号（李四）", n_li == 2, f"李四={n_li}")
    check("机构全称与其简称同号", n_full == n_short == 1,
          f"全称={n_full} 简称={n_short}")
    check("不同机构各自占号（乙方）", n_other == 2, f"乙方={n_other}")
    check("占位符格式统一 [标签#N]",
          placeholder_text("自然人姓名", 1) == "[自然人姓名#1]")
    check("占位符可被解析", parse_placeholder("[组织机构名称#12]") == ("组织机构名称", 12))

    print("== 覆盖检查：座机 / 统一社会信用代码 ==")
    check("座机带内部分组空格 010-8666 2188 能识别",
          any(c.type_id == "PHONE_LANDLINE" and "8666" in c.value and "2188" in c.value
              for c in ent_cands))
    check("校验码错误的统一社会信用代码仍能识别",
          any(c.type_id == "USCC" and c.value == "91110108MA8A2X5K3R"
              for c in ent_cands))
    check("座机脱敏保留区号与末 4 位",
          mask_value("010-8666 2188", "PHONE_LANDLINE") == "010-****2188")

    print("== 脱敏预览 ==")
    check("身份证脱敏 440305********2347", mask_value("440305199001012347", "ID_CARD") == "440305********2347")
    check("手机号脱敏 138****5678", mask_value("13812345678", "PHONE_MOBILE") == "138****5678")
    check("姓名脱敏 赵**", mask_value("赵启明", "PERSON_NAME") == "赵**")
    check("复姓姓名脱敏 欧阳**", mask_value("欧阳博文", "PERSON_NAME") == "欧阳**")
    m = mask_value("zhao.qiming@xinghai-tech.com.cn", "EMAIL")
    check("邮箱保留域名 " + m, m.startswith("z") and m.endswith("@xinghai-tech.com.cn"))

    print()
    if fails:
        print(f"自检失败 {len(fails)} 项：")
        for f in fails:
            print("  - " + f)
        return 1
    print("自检全部通过 ✔ 共识别 %d 类 / %d 处候选" % (
        len(cands), sum(c.count for c in cands)))
    return 0


# ==============================================================================
# 10. CLI
# ==============================================================================

def process_file(path: str, settings: Settings, out_dir: str, formats: set,
                 quiet: bool = False, include_headers: bool = True,
                 used_bases: Optional[set] = None,
                 review_api: Optional[str] = None) -> Dict[str, Any]:
    doc = load_document(path, include_headers=include_headers)
    require_pdf_text(doc)
    engine = DetectorEngine(settings)
    candidates = engine.scan_document(doc)
    report = build_report(doc, candidates, settings)

    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    base = os.path.join(out_dir, stem)
    if used_bases is not None:
        # 同名不同格式（合同.docx / 合同.pdf）不能互相覆盖
        if base in used_bases:
            base = f"{base}_{doc.file_type}"
        n = 2
        while base in used_bases:
            base = f"{os.path.join(out_dir, stem)}_{doc.file_type}{n}"
            n += 1
        used_bases.add(base)
    written: List[str] = []

    if "json" in formats:
        p = base + ".sensitive.json"
        write_json(report, p)
        written.append(p)
    if "csv" in formats:
        p = base + ".sensitive.csv"
        write_csv(report, p)
        written.append(p)

    full_raw = "\n".join(b.text.rstrip("\n") for b in doc.blocks)
    if "txt" in formats:
        p = base + ".annotated.txt"
        with open(p, "w", encoding="utf-8") as f:
            f.write(build_annotated_text(doc, candidates, full_raw))
        written.append(p)
    if "masked" in formats:
        p = base + ".masked.txt"
        with open(p, "w", encoding="utf-8") as f:
            f.write(build_masked_text(doc, candidates, full_raw))
        written.append(p)
    if "html" in formats:
        p = base + ".review.html"
        write_review_html(report, doc, p, full_raw, api_base=review_api)
        written.append(p)
    if "mapping" in formats:
        p = base + ".mapping.json"
        write_mapping(report, p)
        written.append(p)
    # 默认导出 selection.json（detector 已经把报告里所有类型打包好，
    # 让 redactor 在没有 UI 时也能直接使用）
    sel_p = base + ".selection.json"
    if not os.path.exists(sel_p) or True:
        enabled = [it["type"] for it in report["items"]]
        with open(sel_p, "w", encoding="utf-8") as f:
            json.dump({
                "source": os.path.abspath(path),
                "enabled_types": enabled,
                "min_confidence": settings.min_confidence,
                "min_severity": settings.min_severity,
                "generated_by": f"{report['engine']['name']} v{report['engine']['version']}",
                "generated_at": report["generated_at"],
            }, f, ensure_ascii=False, indent=2)
        written.append(sel_p)

    if not quiet:
        s = report["summary"]
        print(f"\n■ {os.path.basename(path)}  [{doc.file_type}] {report['source']['chars']} 字")
        for w in doc.warnings:
            print(f"   ⚠ {w}")
        print(f"   候选 {s['candidate_types']} 类 / {s['occurrences']} 处")
        for c in candidates[:20]:
            loc = c.occurrences[0].location
            print(f"   [{c.cid:>2}] {SEVERITY_LABEL.get(c.severity,'?')} "
                  f"{c.label:<12} {c.confidence:.2f}  ×{c.count}  "
                  f"{c.value[:28]:<30} → {c.masked[:22]:<24} @ {loc}")
        if len(candidates) > 20:
            print(f"   … 其余 {len(candidates)-20} 类见报告文件")
        for p in written:
            print(f"   → {p}")
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="contract_sensitive_detector.py",
        description="合同敏感信息候选项识别器（安装完成后文书处理不联网：正则 + 上下文 + 国标校验位）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python contract_sensitive_detector.py 合同.docx
  python contract_sensitive_detector.py a.pdf b.docx --out-dir out --format json,html
  python contract_sensitive_detector.py 合同.docx --min-severity high
  python contract_sensitive_detector.py 合同.docx --disable DATE,AMOUNT,PLATE
  python contract_sensitive_detector.py --selftest
""")
    ap.add_argument("inputs", nargs="*", help="待检测的 .docx / .pdf / .txt / .md 文件")
    ap.add_argument("--out-dir", default="./sensitive_reports", help="报告输出目录（默认 ./sensitive_reports）")
    ap.add_argument("--format", default="json,csv,txt",
                    help="输出格式，逗号分隔：json,csv,txt,masked,html,mapping（默认 json,csv,txt）")
    ap.add_argument("--min-confidence", type=float, default=0.5, help="置信度下限 0~1（默认 0.5）")
    ap.add_argument("--min-severity", default="low", choices=["low", "medium", "high"],
                    help="严重度下限（默认 low=全部）")
    ap.add_argument("--disable", default="", help="停用的类型，逗号分隔，如 DATE,AMOUNT")
    ap.add_argument("--only", default="", help="只启用这些类型，逗号分隔")
    ap.add_argument("--enable", default="", help="强制启用默认关闭的类型，逗号分隔（如 IPV4,MAC_ADDRESS）")
    ap.add_argument("--allowlist", help="白名单文件（每行一个值，命中即忽略）")
    ap.add_argument("--rules", help="脱敏规则清单 Markdown（默认读取脚本同目录的 脱敏规则清单.md）")
    ap.add_argument("--config", help="JSON 配置文件（可含 min_confidence/disabled/allowlist/custom_rules）")
    ap.add_argument("--context-chars", type=int, default=30, help="上下文摘录字数（默认 30）")
    ap.add_argument("--no-headers", action="store_true", help="不解析 docx 页眉页脚")
    ap.add_argument("--allow-network", action="store_true",
                    help="关闭离线守卫（默认开启，任何联网都会被阻断）")
    ap.add_argument("--selftest", action="store_true", help="运行内置自检并退出")
    ap.add_argument("--quiet", "-q", action="store_true", help="不打印明细")
    ap.add_argument("--review-api", help="HTML 复核页用：写入 review.html 的 API base（默认从 $REVIEW_API 读取）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    if not args.allow_network:
        enforce_offline()

    if args.selftest:
        return selftest(verbose=not args.quiet)

    if not args.inputs:
        ap.print_help()
        return 2

    settings, extra = load_config(args.config)
    if args.config is None:
        settings.min_confidence = args.min_confidence
        settings.min_severity = args.min_severity
        settings.disabled = {t.strip() for t in args.disable.split(",") if t.strip()}
        settings.enabled_only = {t.strip() for t in args.only.split(",") if t.strip()} or None
    else:
        if args.min_confidence != 0.5:
            settings.min_confidence = args.min_confidence
        if args.min_severity != "low":
            settings.min_severity = args.min_severity
        if args.disable:
            settings.disabled |= {t.strip() for t in args.disable.split(",") if t.strip()}
        if args.only:
            settings.enabled_only = {t.strip() for t in args.only.split(",") if t.strip()}
    settings.extra_specs = extra
    settings.allowlist |= load_allowlist(args.allowlist)
    settings.context_chars = args.context_chars
    settings.force_enabled = {t.strip() for t in args.enable.split(",") if t.strip()}

    rules_path = args.rules
    if rules_path is None:
        default_rules = os.path.join(os.path.dirname(os.path.abspath(__file__)), "脱敏规则清单.md")
        if os.path.exists(default_rules):
            rules_path = default_rules
    if rules_path:
        if not os.path.exists(rules_path):
            print(f"[错误] 规则清单不存在：{rules_path}", file=sys.stderr)
            return 1
        try:
            settings.extra_specs.extend(load_rules_md(rules_path))
        except RulesFormatError as exc:
            print(f"[错误] 规则清单未通过校验：{exc}", file=sys.stderr)
            return 1

    if args.review_api:
        os.environ["REVIEW_API"] = args.review_api
    formats = {f.strip() for f in args.format.split(",") if f.strip()}
    exit_code = 0
    used_bases: set = set()
    for path in args.inputs:
        if not os.path.exists(path):
            print(f"[错误] 文件不存在：{path}", file=sys.stderr)
            exit_code = 1
            continue
        try:
            process_file(path, settings, args.out_dir, formats, quiet=args.quiet,
                         include_headers=not args.no_headers, used_bases=used_bases,
                         review_api=args.review_api or os.environ.get("REVIEW_API"))
        except Exception as exc:
            print(f"[错误] {path} 处理失败：{exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
