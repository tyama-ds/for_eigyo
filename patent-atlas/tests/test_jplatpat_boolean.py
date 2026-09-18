"""Boolean-equivalence checks for structured J-PlatPat bracket reduction."""
import copy
import itertools
import random
import unittest

from query_formats import (_simplify_jplatpat_tree, export_query, tree_data,
                           tree_from_data)
from query_library import parse_example


def term(value):
    return {'op': 'text', 'value': value}


def op(name, *children):
    return {'op': name, 'children': list(children)}


def evaluate(node, values):
    if node['op'] in ('text', 'class'):
        return values[node['value']]
    children = [evaluate(child, values) for child in node['children']]
    if node['op'] == 'and':
        return all(children)
    if node['op'] == 'or':
        return any(children)
    return children[0] and not children[1]


class JplatpatBooleanTests(unittest.TestCase):
    def assert_equivalent(self, original, actual, names=('alpha', 'beta', 'gamma', 'delta')):
        for bits in itertools.product((False, True), repeat=len(names)):
            values = dict(zip(names, bits))
            self.assertEqual(evaluate(original, values), evaluate(actual, values), (original, actual, values))

    def test_common_facets_exclusions_and_classifications_fit_three_levels(self):
        tree = op('and', op('not', op('and', op('or', term('alpha'), term('beta')), term('gamma')), term('delta')),
                  op('or', {'op': 'class', 'system': 'IPC', 'value': 'B60'},
                     {'op': 'class', 'system': 'IPC', 'value': 'B64'}))
        query = {'boolean_tree': tree, 'classifications': [{'kind': 'IPC', 'code': code, 'verified': True} for code in ('B60', 'B64')]}
        original = copy.deepcopy(query)
        output = export_query(query, 'jplatpat')
        self.assertTrue(output['can_copy'], output['problems'])
        current = maximum = 0
        for char in output['expression']:
            current += (1 if char == '[' else -1 if char == ']' else 0)
            maximum = max(maximum, current)
        self.assertLessEqual(maximum, 3)
        parsed = parse_example(output['expression'])
        self.assertEqual(parsed['status'], 'editable', parsed['issues'])
        self.assert_equivalent(tree, parsed['query']['boolean_tree'], ('alpha', 'beta', 'gamma', 'delta', 'B60', 'B64'))
        self.assertEqual(query, original)

    def test_not_of_and_and_or_of_not_keep_their_meanings(self):
        a, b, c, d = map(term, ('alpha', 'beta', 'gamma', 'delta'))
        examples = [op('not', a, op('and', b, c)),
                    op('and', op('not', a, op('and', b, c)), d),
                    op('or', op('not', a, b), op('and', c, d)),
                    op('not', op('not', a, b), op('and', c, d)),
                    op('and', op('not', a, b), op('not', c, d)),
                    op('not', a, op('not', b, c))]
        for tree in examples:
            with self.subTest(tree=tree):
                reduced = tree_data(_simplify_jplatpat_tree(tree_from_data(tree)))
                self.assert_equivalent(tree, reduced)
                output = export_query({'boolean_tree': tree}, 'jplatpat')
                self.assertTrue(output['can_copy'], output['problems'])
                imported = parse_example(output['expression'])
                self.assertEqual(imported['status'], 'editable', imported['issues'])
                self.assert_equivalent(tree, imported['query']['boolean_tree'])

    def test_associative_flattening_and_exclusion_extraction_obey_truth_tables(self):
        rng = random.Random(3917)
        names = ('alpha', 'beta', 'gamma', 'delta')

        def make_tree(depth):
            if depth == 0 or rng.random() < .28:
                return term(rng.choice(names))
            return op(rng.choice(('and', 'or', 'not')), make_tree(depth - 1), make_tree(depth - 1))

        copied = 0
        for _ in range(160):
            tree = make_tree(4)
            reduced = tree_data(_simplify_jplatpat_tree(tree_from_data(tree)))
            self.assert_equivalent(tree, reduced)
            output = export_query({'boolean_tree': tree}, 'jplatpat')
            if output['can_copy']:
                imported = parse_example(output['expression'])
                self.assertEqual(imported['status'], 'editable', imported['issues'])
                self.assert_equivalent(tree, imported['query']['boolean_tree'])
                copied += 1
            else:
                self.assertTrue(all('3階層' in p for p in output['problems']), output['problems'])
        self.assertGreater(copied, 30)

    def test_legacy_rendering_is_unchanged(self):
        output = export_query({'keywords': ['車両', '制御'], 'include_terms': ['運転', '誘導'],
                               'exclude_terms': ['玩具'], 'classifications': []}, 'jplatpat')
        self.assertEqual(output['expression'], '[[車両/TX]*[制御/TX]*[運転/TX+誘導/TX]]-[玩具/TX]')


if __name__ == '__main__':
    unittest.main()
