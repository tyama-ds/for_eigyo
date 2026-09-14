"""Search convergence uses observed, complete result sets and explicit verdicts."""
import copy
import unittest

from convergence_engine import build_record, publication_key, summarize


def patent(identifier, label='keep', ipc='H01M10/00', **values):
    return dict(id=identifier, label=label, ipc=ipc, label_source=values.pop('label_source', 'human'), **values)


def snapshot(identifier, rows, previous=(), *, hits='auto', scope='csv', keywords='battery'):
    return build_record(rows, dict(id=identifier, keywords=keywords, imported_at='2026-09-14T09:00:00Z',
                                   scope=scope, publication_keys=['untrusted-observation-id']), previous,
                        total_hits=len(rows) if hits == 'auto' else hits,
                        query={'id': 'query-' + identifier, 'format': 'espacenet', 'expression': 'private draft'})


class ConvergenceEngineTests(unittest.TestCase):
    def test_first_observation_has_no_invented_rates(self):
        record = snapshot('one', [patent('JP1')])
        for key in ('new_keep_count', 'new_keep_rate', 'new_ipc_count', 'new_ipc_rate', 'topic_shift_rate', 'hit_change_rate'):
            self.assertIsNone(record[key], key)
        self.assertFalse(record['comparable'])
        self.assertEqual(record['publication_keys'], ['JP1'])
        self.assertTrue(record['result_complete'])

    def test_deduplicates_publications_and_prefers_human_labels(self):
        rows = [patent('JP-1', 'exclude', label_source='agent'), patent('ＪＰ １'),
                patent('JP1', 'exclude', label_source='agent')]
        record = snapshot('one', rows, hits=1)
        self.assertEqual(publication_key('ｊｐ－１'), 'JP1')
        self.assertEqual((record['uploaded_count'], record['keep_count'], record['exclude_count']), (1, 1, 0))
        self.assertEqual(record['duplicate_count'], 2)
        self.assertEqual(record['human_count'], 1)
        self.assertTrue(record['result_complete'])

    def test_new_keep_is_compared_to_all_previous_observations(self):
        first = snapshot('one', [patent('JP1')])
        second = snapshot('two', [patent('JP2')], [first])
        third = snapshot('three', [patent('JP1'), patent('JP2')], [first, second])
        self.assertEqual(second['new_keep_rate'], 1)
        self.assertEqual(third['new_keep_count'], 0)
        self.assertEqual(third['new_keep_rate'], 0)

    def test_ipc_distribution_gives_each_patent_one_total_vote(self):
        first = snapshot('one', [patent('JP1', ipc='H01M10/00; G05D1/00'), patent('JP2')])
        self.assertEqual(first['ipc_distribution'], {'G05D': .25, 'H01M': .75})
        second = snapshot('two', [patent('JP1', ipc='G05D1/00'), patent('JP2')], [first])
        self.assertEqual(second['topic_shift_rate'], .25)
        self.assertEqual(second['new_ipc_count'], 0)

    def test_four_complete_observations_can_be_stable(self):
        rows = [patent('JP1'), patent('JP2', 'exclude')]
        records = []
        for index in range(4):
            records.append(snapshot(str(index), rows, records))
        result = summarize(records, rows, keywords='battery')
        self.assertEqual(result['stable_streak'], 3)
        self.assertEqual(result['status'], 'stable')
        self.assertTrue(result['eligible_to_complete'])
        self.assertEqual(result['blockers'], [])
        for item in result['records']:
            self.assertNotIn('publication_keys', item)
            self.assertNotIn('keep_publication_keys', item)
            self.assertNotIn('ipc_subclasses', item)
            self.assertNotIn('ipc_distribution', item)
            self.assertNotIn('expression', item['query'])

    def test_repeated_update_of_same_observation_does_not_manufacture_streak(self):
        rows = [patent('JP1')]
        first = snapshot('one', rows)
        duplicate = snapshot('one', rows, [first])
        self.assertIsNone(duplicate['new_keep_rate'])
        result = summarize([first, duplicate, duplicate, duplicate], rows, keywords='battery')
        self.assertEqual(len(result['records']), 1)
        self.assertFalse(result['eligible_to_complete'])

    def test_new_small_ipc_topic_resets_stability_even_below_rate_threshold(self):
        rows = [patent('JP' + str(index)) for index in range(100)]
        records = []
        for index in range(4):
            records.append(snapshot(str(index), rows, records))
        changed = rows + [patent('NEW', ipc='G05D1/00')]
        record = snapshot('new-topic', changed, records)
        self.assertLess(record['new_keep_rate'], .05)
        self.assertLess(record['topic_shift_rate'], .05)
        self.assertLess(record['hit_change_rate'], .05)
        self.assertEqual(record['new_ipc_count'], 1)
        result = summarize(records + [record], changed, keywords='battery')
        self.assertEqual(result['stable_streak'], 0)
        self.assertFalse(result['eligible_to_complete'])

    def test_missing_total_or_partial_csv_cannot_qualify(self):
        rows = [patent('JP1')]
        for total in (None, 2, True, -1, 0, float('nan'), '1'):
            with self.subTest(total=total):
                records = []
                for index in range(4):
                    records.append(snapshot(str(index), rows, records, hits=total))
                result = summarize(records, rows, keywords='battery')
                self.assertFalse(records[-1]['result_complete'])
                self.assertFalse(result['eligible_to_complete'])
                self.assertEqual(result['status'], 'insufficient')
        complete = snapshot('one', rows)
        partial = snapshot('two', rows, [complete], hits=2)
        self.assertEqual(partial['hit_change_rate'], .5)
        self.assertFalse(partial['comparable'])

    def test_baseline_never_becomes_complete_even_with_matching_total(self):
        first = snapshot('one', [patent('JP1')], scope='workspace_baseline')
        second = snapshot('two', [patent('JP1')], [first])
        self.assertFalse(first['result_complete'])
        self.assertFalse(second['comparable'])

    def test_deferred_and_unprocessed_global_rows_block_completion(self):
        rows = [patent('JP1')]
        records = []
        for index in range(4):
            records.append(snapshot(str(index), rows, records))
        deferred = patent('JP2', None, label_source='agent', agent_confidence=.3, label_reason='情報不足')
        fresh = patent('JP3', None, label_source=None)
        result = summarize(records, rows + [deferred, fresh], keywords='battery')
        self.assertEqual(result['pending'], {'deferred': 1, 'unprocessed': 1})
        self.assertEqual(result['stable_streak'], 3)
        self.assertFalse(result['eligible_to_complete'])
        self.assertTrue(any('人が確認' in message for message in result['blockers']))

    def test_prior_unresolved_observation_blocks_next_transition(self):
        rows = [patent('JP1'), patent('JP2', None, label_source='agent', agent_confidence=.3, label_reason='保留')]
        first = snapshot('one', rows)
        next_rows = [patent('JP1'), patent('JP2', 'exclude')]
        second = snapshot('two', next_rows, [first])
        self.assertFalse(second['comparable'])
        self.assertTrue(any('前回' in reason and '保留' in reason for reason in second['comparability_reasons']))

    def test_no_keep_or_missing_ipc_does_not_look_converged(self):
        for rows in ([patent('JP1', 'exclude')], [patent('JP1', ipc='')],
                     [patent('JP1'), patent('JP2', ipc='')], [patent('JP1', ipc='H98Z1/00')],
                     [patent('JP1', ipc='H')], []):
            with self.subTest(rows=rows):
                records = []
                for index in range(4):
                    records.append(snapshot(str(index), rows, records))
                result = summarize(records, rows, keywords='battery')
                self.assertFalse(result['eligible_to_complete'])
        record = snapshot('missing', [patent('JP1', ipc='', fi='H01M10/00', fterm='5H029AM11')])
        self.assertEqual(record['keep_with_ipc_count'], 0)
        self.assertEqual(record['ipc_distribution'], {})

    def test_ipc_lists_versions_and_historical_subgroups_support_subclass_proxy(self):
        record = snapshot('one', [patent('JP1', ipc=['Ｈ０１Ｍ１０／００ (2026.01)', 'G05D1/02'])])
        self.assertEqual(record['ipc_subclasses'], ['G05D', 'H01M'])
        self.assertEqual(record['ipc_coverage_rate'], 1)

    def test_topic_stream_change_does_not_compare_unrelated_observations(self):
        first = snapshot('one', [patent('JP1')])
        second = snapshot('two', [patent('JP2')], [first], keywords='vehicle')
        self.assertIsNone(second['new_keep_rate'])
        self.assertEqual(len(summarize([first, second], [], keywords='vehicle')['records']), 1)

    def test_missing_identifiers_and_malformed_old_data_fail_closed(self):
        rows = [patent('')]
        first = snapshot('one', rows)
        second = snapshot('two', rows, [first])
        self.assertFalse(second['comparable'])
        malformed = dict(id='legacy', keywords='battery', new_keep_rate=float('nan'), ipc_distribution={'H01M': float('nan')})
        result = summarize([None, {}, malformed], [], keywords='battery')
        self.assertEqual(result['status'], 'insufficient')
        self.assertIsNone(result['records'][0]['new_keep_rate'])
        self.assertFalse(result['eligible_to_complete'])
        self.assertFalse(snapshot('next', [patent('JP1')], [malformed])['comparable'])

    def test_snapshots_and_public_summary_are_detached(self):
        rows = [patent('JP1')]
        observation = dict(id='one', keywords='battery', scope='csv')
        before_rows, before_observation = copy.deepcopy(rows), copy.deepcopy(observation)
        first = build_record(rows, observation, [], total_hits=1)
        original = copy.deepcopy(first)
        second = build_record(rows, dict(id='two', keywords='battery', scope='csv'), [first], total_hits=1)
        summary = summarize([first, second], rows, keywords='battery')
        summary['records'][0]['comparability_reasons'].append('changed')
        summary['thresholds']['new_keep_rate'] = 1
        rows[0]['ipc'] = 'G05D1/00'
        self.assertEqual(first, original)
        self.assertEqual(observation, before_observation)
        self.assertEqual(before_rows[0]['ipc'], 'H01M10/00')


if __name__ == '__main__':
    unittest.main()
