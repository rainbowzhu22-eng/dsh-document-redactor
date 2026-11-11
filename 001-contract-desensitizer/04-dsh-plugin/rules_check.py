"""Check an Agent proposal against the embedded rule parser; never apply it."""
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'core' / 'scripts'))
import contract_sensitive_detector as detector

# 只约束本次新增或改动的自定义规则；用户原有规则文件照常上传，不因此报错。
BARE_PATTERN = re.compile(r'[\u4e00-\u9fffA-Za-z（）()?:|]+')
TRANSACTION_WORDS = ('日期', '签署日', '生效日', '到期日', '付款日', '金额', '价款', '款项')
TRANSACTION_PREFIXES = ('AMOUNT_', 'DATE_', 'MONEY_')


def _custom_rows(snapshot):
    rows = {}
    for row in snapshot.get('rows', []):
        cells = list(row.cells)
        if len(cells) >= 9 and cells[8].strip():
            rows[cells[1].strip()] = cells
    return rows


def proposal_problems(previous, proposed):
    before = _custom_rows(previous)
    problems = []
    for type_id, cells in _custom_rows(proposed).items():
        pattern = cells[8].strip().replace('\\|', '|')
        if before.get(type_id, [None] * 9)[8] == cells[8]:
            continue
        if '\\' not in pattern and BARE_PATTERN.fullmatch(pattern):
            problems.append(f'新增规则「{type_id}」的正则只是固定文字，会把正文里所有同样的词都框出；'
                            '固定的名称请用工作台的“补充敏感原文”，规则只用于有结构的内容（如数字、字段标签）')
        intent = cells[1] + cells[4]
        if (any(word in intent for word in TRANSACTION_WORDS) and
                not type_id.startswith(TRANSACTION_PREFIXES)):
            problems.append(f'日期或金额类规则「{type_id}」的规则名须以 DATE_ 或 AMOUNT_ 开头，'
                            '这样才会跟随“保留金额、日期”的选择')
    return problems


try:
    previous = detector.build_rules_snapshot(sys.argv[1])
    proposed = detector.build_rules_snapshot(
        sys.argv[2], previous_custom_ids=previous['custom_ids'])
    problems = proposal_problems(previous, proposed)
    if problems:
        raise ValueError('；'.join(problems))
    print(json.dumps({'ok': True, 'deleted': proposed['pending_deletions']}, ensure_ascii=False))
except Exception as error:
    print(json.dumps({'ok': False, 'message': str(error)[:1200]}, ensure_ascii=False))
    sys.exit(1)
