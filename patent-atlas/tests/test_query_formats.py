import copy
import unittest
from catalog import keywords
from query_formats import PROFILES, build_tree, export_query

def snapshot():
    return dict(id='q1',version=1,type='改善案',keywords=['solid electrolyte','interface resistance'],include_terms=['lithium','ceramic'],exclude_terms=['cooling','camera'],
                classifications=[dict(kind='IPC',code='H01M10/0562',verified=True),dict(kind='F-term',code='5H029AJ06',verified=True),dict(kind='CPC',code='H01M10/0565',verified=True)])

class QueryFormatTests(unittest.TestCase):
    def test_quoted_phrases_and_no_silent_token_loss(self):
        self.assertEqual(keywords('"solid electrolyte" "interface resistance"'),['solid electrolyte','interface resistance'])
        self.assertEqual(len(keywords(' '.join('term'+str(i) for i in range(24)))),24)
        for invalid in (' '.join('term'+str(i) for i in range(25)), '"solid electrolyte" OR "polymer electrolyte"','"unterminated','(solid OR liquid)','"solid electrolyte"battery','"solid""electrolyte"'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                keywords(invalid)

    def test_jplatpat_operators_tags_and_bracket_limit(self):
        result=export_query(snapshot(),'jplatpat')
        self.assertEqual(result['expression'],"[['solid electrolyte'/TX]*['interface resistance'/TX]*[H01M10/0562/IP+5H029AJ06/FT+H01M10/0565/CP]*['lithium'/TX+'ceramic'/TX]]-['cooling'/TX+'camera'/TX]")
        depth=0
        for char in result['expression']:
            depth+=(char=='[')-(char==']')
            self.assertLessEqual(depth,3)
            self.assertGreaterEqual(depth,0)
        self.assertEqual(depth,0)

    def test_espacenet_boolean_groups_and_separate_fields(self):
        result=export_query(snapshot(),'espacenet',omit_unsupported=True)
        self.assertEqual(result['expression'],'((ftxt="solid electrolyte" AND ftxt="interface resistance" AND (ipc=H01M10/0562 OR cpc=H01M10/0565) AND (ftxt="lithium" OR ftxt="ceramic")) NOT (ftxt="cooling" OR ftxt="camera"))')

    def test_uspto_phrases_use_adj_and_not_binary(self):
        result=export_query(snapshot(),'uspto',omit_unsupported=True)
        self.assertEqual(result['expression'],'(((solid ADJ electrolyte) AND (interface ADJ resistance) AND (H01M10/0562.IPC. OR H01M10/0565.CPC.) AND (lithium OR ceramic)) NOT (cooling OR camera))')

    def test_derwent_products_are_distinct(self):
        result=export_query(snapshot(),'derwent_innovation')
        self.assertTrue(result['can_copy'])
        self.assertIn('IC=(H01M 10/0562) OR FTC=(5H029AJ06) OR ACP=(H01M 10/0565)',result['expression'])
        self.assertIn('CTB=("solid electrolyte")',result['expression'])
        self.assertTrue(result['expression'].endswith(';'))
        dii=export_query(snapshot(),'derwent_dii',omit_unsupported=True)
        self.assertIn('TS=("solid electrolyte")',dii['expression'])
        self.assertIn('IP=(H01M-010/0562)',dii['expression'])
        self.assertNotIn('CPC=',dii['expression'])
        self.assertEqual(len(dii['omitted_classifications']),2)

    def test_patentscope_explicit_language_and_class_fields(self):
        result=export_query(snapshot(),'patentscope',omit_unsupported=True)
        self.assertIn('EN_ALLTXT:("solid electrolyte")',result['expression'])
        self.assertIn('IC_EX:("H01M 10/0562")',result['expression'])
        self.assertIn('CPC_EX:("H01M 10/0565")',result['expression'])
        self.assertIn(' ANDNOT ',result['expression'])
        q=dict(id='ja',keywords=['固体電解質'],classifications=[])
        self.assertEqual(export_query(q,'patentscope')['expression'],'JA_ALLTXT:("固体電解質")')

    def test_google_never_relabels_ipc_as_cpc(self):
        result=export_query(snapshot(),'google_patents')
        self.assertFalse(result['can_copy']);self.assertEqual(result['expression'],'')
        result=export_query(snapshot(),'google_patents',omit_unsupported=True)
        self.assertNotIn('CPC=H01M10/0562',result['expression'])
        self.assertIn('CPC=H01M10/0565',result['expression'])
        self.assertEqual([c['kind'] for c in result['omitted_classifications']],['IPC','F-term'])

    def test_classification_omission_is_explicit_and_non_mutating(self):
        q=snapshot();before=copy.deepcopy(q)
        strict=export_query(q,'espacenet')
        self.assertFalse(strict['can_copy']);self.assertEqual(strict['expression'],'')
        allowed=export_query(q,'espacenet',omit_unsupported=True)
        self.assertTrue(allowed['can_copy']);self.assertEqual(q,before)
        self.assertTrue(any('同じ検索ではありません' in w for w in allowed['warnings']))

    def test_language_replacement_is_explicit_and_preserves_phrase(self):
        q=dict(id='ja',keywords=['固体電解質','界面抵抗'],classifications=[],include_terms=[],exclude_terms=[])
        before=copy.deepcopy(q)
        self.assertFalse(export_query(q,'espacenet')['can_copy'])
        result=export_query(q,'espacenet',replacements={'固体電解質':'solid electrolyte','界面抵抗':'interface resistance'})
        self.assertEqual(result['expression'],'(ftxt="solid electrolyte" AND ftxt="interface resistance")')
        self.assertEqual(q,before);self.assertEqual(len(result['replacements']),2)

    def test_descendants_do_not_become_prefix_wildcards(self):
        for f in ('espacenet','google_patents'):
            out=export_query(snapshot(),f,omit_unsupported=True,descendants=True)
            self.assertTrue(out['can_copy']);self.assertIn('/low',out['expression']);self.assertNotIn('*',out['expression'])
        self.assertFalse(export_query(snapshot(),'uspto',omit_unsupported=True,descendants=True)['can_copy'])
        wipo=export_query(snapshot(),'patentscope',omit_unsupported=True,descendants=True)
        self.assertIn('IC:("H01M 10/0562")',wipo['expression']);self.assertNotIn('_EX',wipo['expression'])

    def test_unverified_classifications_need_acknowledgement(self):
        q=snapshot();q['classifications'][0]['verified']=False
        self.assertFalse(export_query(q,'jplatpat')['can_copy'])
        self.assertTrue(export_query(q,'jplatpat',allow_unverified=True)['can_copy'])

    def test_unknown_or_injection_like_inputs_are_not_silently_rewritten(self):
        q=snapshot()
        for word in ('alpha" OR anything','alpha]/TX+[anything','*',''):
            with self.subTest(word=word), self.assertRaises(ValueError):
                export_query(q,'jplatpat',replacements={'solid electrolyte':word})
        for kwargs in ({'format_id':'unknown'},{'format_id':'jplatpat','omit_unsupported':'false'}):
            with self.assertRaises(ValueError):
                export_query(q,**kwargs)
        for word in ('XOR','NEAR3','ADJ2','battery.TI.','solid-state'):
            with self.subTest(word=word), self.assertRaises(ValueError):
                export_query(dict(keywords=[word],classifications=[]),'uspto')

    def test_legacy_refinement_is_not_exported_with_missing_conditions(self):
        with self.assertRaises(ValueError):
            export_query(dict(type='改善案',keywords=['battery'],expression='TEXT=battery NOT TEXT=camera'),'jplatpat')

    def test_classification_only_initial_query_in_all_formats(self):
        cases={
            'jplatpat': ('IPC','H01M10/00/IP'),
            'derwent_innovation': ('IPC','IC=(H01M 10/00);'),
            'derwent_dii': ('IPC','IP=(H01M-010/00)'),
            'espacenet': ('IPC','ipc=H01M10/00'),
            'uspto': ('IPC','H01M10/00.IPC.'),
            'patentscope': ('IPC','IC_EX:("H01M 10/00")'),
            'google_patents': ('CPC','CPC=H01M10/00'),
        }
        self.assertEqual(set(cases),set(PROFILES))
        for format_id,(kind,expected) in cases.items():
            with self.subTest(format_id=format_id):
                q=dict(id='class-only',version=1,type='初案',keywords=[],classifications=[dict(kind=kind,code='H01M10/00',verified=True)])
                before=copy.deepcopy(q)
                result=export_query(q,format_id,replacements={})
                self.assertTrue(result['can_copy'])
                self.assertEqual(result['expression'],expected)
                self.assertEqual(result['terms'],[])
                self.assertEqual(result['replacements'],[])
                self.assertEqual(q,before)

    def test_classification_only_refinement_preserves_boolean_conditions(self):
        q=dict(type='改善案',keywords=[],classifications=[dict(kind='IPC',code='H01M10/0562',verified=True)],
               include_terms=['lithium','ceramic'],exclude_terms=['camera'])
        result=export_query(q,'espacenet')
        self.assertEqual(result['expression'],'((ipc=H01M10/0562 AND (ftxt="lithium" OR ftxt="ceramic")) NOT ftxt="camera")')

    def test_upper_ipc_branches_render_without_changing_fine_code_scope(self):
        cases = {
            'jplatpat': {'H': 'H/IP', 'H04': 'H04/IP', 'B': 'B/IP', 'B60': 'B60/IP',
                         'H04L': 'H04L/IP', 'B60W': 'B60W/IP'},
            'derwent_innovation': {'H04': 'IC=(H04);', 'H04L': 'IC=(H04L);'},
            'derwent_dii': {'H04': 'IP=(H04*)', 'H04L': 'IP=(H04L*)'},
            'espacenet': {'H': 'ipc=H', 'H04': 'ipc=H04', 'H04L': 'ipc=H04L'},
            'uspto': {'H': 'H$.CIPC.', 'H04': 'H04$.CIPC.', 'H04L': 'H04L$.CIPC.'},
            'patentscope': {'H': 'IC:("H")', 'H04': 'IC:("H04")', 'H04L': 'IC:("H04L")'},
        }
        for format_id, codes in cases.items():
            for code, expected in codes.items():
                for descendants in (False, True):
                    with self.subTest(format_id=format_id, code=code, descendants=descendants):
                        query = dict(type='初案', keywords=[], classifications=[dict(kind='IPC', code=code, verified=True)])
                        before = copy.deepcopy(query)
                        result = export_query(query, format_id, descendants=descendants)
                        self.assertTrue(result['can_copy'], result['problems'])
                        self.assertEqual(result['expression'], expected)
                        self.assertEqual(query, before)
                        self.assertTrue(any('枝全体' in message for message in result['warnings']))
        mixed = dict(keywords=['vehicle'], classifications=[dict(kind='IPC', code='H04L', verified=True),
                                                            dict(kind='IPC', code='H01M10/00', verified=True)])
        self.assertEqual(export_query(mixed, 'patentscope')['expression'],
                         '(EN_ALLTXT:("vehicle") AND (IC:("H04L") OR IC_EX:("H01M 10/00")))')
        self.assertFalse(export_query(mixed, 'uspto', descendants=True)['can_copy'])
        mixed_jp = dict(type='改善案', keywords=['自動運転'], include_terms=['制御'], exclude_terms=['鉄道'],
                        classifications=[dict(kind='IPC', code='B60', verified=True),
                                         dict(kind='IPC', code='G05D1/00', verified=True)])
        before = copy.deepcopy(mixed_jp)
        result = export_query(mixed_jp, 'jplatpat')
        self.assertTrue(result['can_copy'], result['problems'])
        self.assertEqual(result['expression'], '[[自動運転/TX]*[B60/IP+G05D1/00/IP]*[制御/TX]]-[鉄道/TX]')
        self.assertEqual(result['omitted_classifications'], [])
        self.assertEqual(mixed_jp, before)

    def test_unconfirmed_upper_scope_is_blocked_without_silent_omission(self):
        cases = {'derwent_dii': ('H',),
                 'derwent_innovation': ('H',)}
        for format_id, codes in cases.items():
            for code in codes:
                with self.subTest(format_id=format_id, code=code):
                    query = dict(keywords=['vehicle'], classifications=[dict(kind='IPC', code=code, verified=True)])
                    result = export_query(query, format_id, omit_unsupported=True, allow_unverified=True)
                    self.assertFalse(result['can_copy'])
                    self.assertEqual(result['expression'], '')
                    self.assertEqual(result['omitted_classifications'], [])
                    self.assertTrue(any('出力を保留' in message and code in message for message in result['problems']))

    def test_upper_ipc_is_not_relabelled_as_cpc_or_accepted_as_raw_wildcard(self):
        query = dict(keywords=['vehicle'], classifications=[dict(kind='IPC', code='H04L', verified=True)])
        result = export_query(query, 'google_patents')
        self.assertFalse(result['can_copy'])
        self.assertEqual(result['expression'], '')
        omitted = export_query(query, 'google_patents', omit_unsupported=True)
        self.assertEqual(omitted['expression'], '"vehicle"')
        self.assertEqual(omitted['omitted_classifications'], query['classifications'])
        for code in ('H0', 'H041', 'H04L*', 'H04L OR G06F', 'Y', 'H04L1', 'H04L/'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                export_query(dict(keywords=[], classifications=[dict(kind='IPC', code=code, verified=True)]), 'patentscope')
        unverified = dict(keywords=[], classifications=[dict(kind='IPC', code='H04L', verified=False)])
        self.assertFalse(export_query(unverified, 'patentscope')['can_copy'])
        self.assertTrue(export_query(unverified, 'patentscope', allow_unverified=True)['can_copy'])

    def test_refinement_terms_cannot_replace_missing_base_conditions(self):
        for include,exclude in (([],[]),(['lithium'],[]),([],['camera']),(['lithium'],['camera'])):
            with self.subTest(include=include,exclude=exclude), self.assertRaises(ValueError):
                build_tree(dict(keywords=[],classifications=[],include_terms=include,exclude_terms=exclude))

    def test_omitting_all_classifications_cannot_remove_the_query_base(self):
        for include in ([],['lithium']):
            with self.subTest(include=include), self.assertRaises(ValueError):
                export_query(dict(keywords=[],classifications=[dict(kind='IPC',code='H01M10/00',verified=True)],
                                  include_terms=include,exclude_terms=['camera']),
                             'google_patents',omit_unsupported=True)

    def test_all_formats_have_sources_and_no_live_validation_claim(self):
        for f,p in PROFILES.items():
            self.assertTrue(p['sources'],f)
            result=export_query(snapshot(),f,omit_unsupported=True)
            self.assertTrue(result['can_copy'],f)
            self.assertIn('未検証',result['verification'])

if __name__=='__main__':
    unittest.main()
