"""Bounded, offline directed tests for the 1.4.2 recording-feedback fixes."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core/scripts'))
import contract_pdf_project as P
import contract_pdf_ui as UI
import contract_redaction_policy as Policy
import contract_sensitive_detector as D


def scan(text):
    with tempfile.TemporaryDirectory(prefix='redactor142-') as temp:
        path = Path(temp) / 'sample.txt'
        path.write_text(text, encoding='utf-8')
        engine = D.DetectorEngine(P._settings(False, None))
        return engine.scan_document(D.load_document(str(path)))


def values(candidates, type_id):
    return {c.value: [o.text for o in c.occurrences] for c in candidates if c.type_id == type_id}


class RecognitionFixes(unittest.TestCase):
    def test_kangxi_radicals_in_pdf_text_layer_are_unified(self):
        found = scan('签署⽇期：2021年10⽉11⽇\n1. 万物⼀期（厦门）创业投资合伙企业（有限合伙）\n')
        self.assertIn('2021年10⽉11⽇', values(found, 'DATE'))
        self.assertIn('万物⼀期（厦门）创业投资合伙企业（有限合伙）', values(found, 'ORG_NAME'))

    def test_party_list_name_with_id_number_and_propagation(self):
        found = scan('4. 宋学璞（与实际控制人合称为“创始人”），身份证号码：110101198805121236；\n'
                     '(9) 宋学璞\n3. 宋体字应当统一使用，身份证明文件除外。\n')
        names = values(found, 'PERSON_NAME')
        self.assertEqual(names.get('宋学璞'), ['宋学璞', '宋学璞'])
        self.assertFalse(any(name.startswith('宋体') for name in names))

    def test_cross_line_org_shares_one_entity_and_alias(self):
        found = scan('5. 爱宠优（北京）\n科贸有限公司（“爱宠优”），一家公司；\n爱宠优应当遵守。\n')
        orgs = values(found, 'ORG_NAME')
        self.assertEqual(orgs.get('爱宠优（北京）科贸有限公司'), ['爱宠优（北京）', '科贸有限公司'])
        self.assertEqual(len(orgs.get('爱宠优', [])), 2)

    def test_party_list_quoted_alias_and_enumeration_prefix(self):
        found = scan('1. 万物一期（厦门）创业投资合伙企业（有限合伙）（“万物资本”），一家合伙企业；\n'
                     '万物资本应当遵守。\n5.北京真宠科技有限公司（“公司”）\n'
                     '本次交易由泽远科技有限公司（“交易文件”）负责。\n')
        orgs = values(found, 'ORG_NAME')
        self.assertEqual(len(orgs.get('万物资本', [])), 2)
        self.assertIn('北京真宠科技有限公司', orgs)
        self.assertNotIn('交易文件', orgs)

    def test_clause_text_before_generic_suffix_is_not_an_org(self):
        found = scan('公司系一家依法设立并有效存续的有限责任公司。\n公司未设立分支机构，也未运营任何办事处。\n'
                     '创始人不直接或间接持有任何集团公司的股权，也不得诱导集团公司的员工离职。\n'
                     '其创始人应当促使各集团公司遵守本协议。\n一家在北京市注册成立的有限合伙企业。\n'
                     '未来科技有限公司\n应用材料（中国）有限公司\n我的世界网络科技有限公司\n')
        self.assertEqual(set(values(found, 'ORG_NAME')),
                         {'未来科技有限公司', '应用材料（中国）有限公司', '我的世界网络科技有限公司'})


class PolicyAndRules(unittest.TestCase):
    def test_custom_transaction_rules_follow_preserve_policy(self):
        self.assertTrue(Policy.is_excluded('DATE_SIGN', Policy.PRESERVE_TRANSACTION))
        self.assertTrue(Policy.is_excluded('DATE', Policy.PRESERVE_TRANSACTION))
        self.assertFalse(Policy.is_excluded('DATE_SIGN', Policy.PRIVACY_FIRST))
        self.assertFalse(Policy.is_excluded('SIGN_DATE', Policy.PRESERVE_TRANSACTION))
        project = {'policy_id': Policy.PRESERVE_TRANSACTION,
                   'policy_selection': Policy.PRESERVE_TRANSACTION,
                   'type_defaults': {}, 'type_choices': {},
                   'candidates': [{'type': 'DATE_SIGN'}, {'type': 'ORG_NAME'}]}
        P._register_policy_types(project)
        self.assertEqual(project['type_defaults'], {'DATE_SIGN': False})
        self.assertEqual(project['type_choices'], {'DATE_SIGN': False})

    def test_agent_proposal_rejects_bare_word_and_unprefixed_date_rules(self):
        base = ROOT / 'core/references/脱敏规则清单.md'
        lines = base.read_text(encoding='utf-8').split('\n')
        anchor = max(i for i, line in enumerate(lines) if line.startswith('| 35 |'))
        rows = {
            'bare': '| 36 | DATE_SIGN | 商业信息 | 低 | 协议签署日 | 全替换 | keep_head=0; keep_tail=0 | 是 | (?:本)?协议签署日 |',
            'unprefixed': '| 36 | PAY_DAY | 商业信息 | 低 | 付款日期 | 保留年份 | keep_head=4; keep_tail=0 | 是 | 付款日[：:]\\s*\\d{4}年\\d{1,2}月\\d{1,2}日 |',
            'structured': '| 36 | DATE_PAY | 商业信息 | 低 | 付款日期 | 保留年份 | keep_head=4; keep_tail=0 | 是 | 付款日[：:]\\s*\\d{4}年\\d{1,2}月\\d{1,2}日 |',
        }
        results = {}
        with tempfile.TemporaryDirectory(prefix='redactor142-rules-') as temp:
            for key, row in rows.items():
                proposal = Path(temp) / f'{key}.md'
                proposal.write_text('\n'.join(lines[:anchor + 1] + [row] + lines[anchor + 1:]), encoding='utf-8')
                done = subprocess.run([sys.executable, str(ROOT / 'rules_check.py'), str(base), str(proposal)],
                                      capture_output=True, text=True, timeout=60)
                results[key] = json.loads(done.stdout)
        self.assertFalse(results['bare']['ok'])
        self.assertIn('补充敏感原文', results['bare']['message'])
        self.assertFalse(results['unprefixed']['ok'])
        self.assertIn('DATE_', results['unprefixed']['message'])
        self.assertTrue(results['structured']['ok'])


class PdfReviewPage(unittest.TestCase):
    def test_preview_loads_separately_and_thumbnail_failure_is_retried(self):
        html = UI.page('a1b2c3d4e5f6').decode('utf-8')
        self.assertIn('id="previewstatus"', html)
        self.assertIn('delete thumbTasks[x.page]', html)
        self.assertNotIn('Promise.all([fetch(\'/api/pdf/page/', html)
        self.assertIn("addEventListener('scroll'", html)


if __name__ == '__main__':
    unittest.main()
