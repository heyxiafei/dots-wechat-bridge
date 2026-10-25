"""Regression for a real Dot message carrying two independent metadata keys.

This is synthetic, offline, and sends neither Dot nor WeChat messages. The
journal receipt accepts only its own exact projection after a positive readback.
"""
import copy
import unittest

import test_dotjournal as journal_fixtures
from probe.weixin import WeixinError


class JournalReceiptProjectionTests(unittest.TestCase):
    scope = journal_fixtures.DotJournalTests.scope
    target = journal_fixtures.DotJournalTests.target
    setUp = journal_fixtures.DotJournalTests.setUp
    open_probe = journal_fixtures.DotJournalTests.open_probe
    inbound = journal_fixtures.DotJournalTests.inbound
    activate = journal_fixtures.DotJournalTests.activate
    admitted = journal_fixtures.DotJournalTests.admitted
    prepare = journal_fixtures.DotJournalTests.prepare
    authorize = journal_fixtures.DotJournalTests.authorize
    evidence = journal_fixtures.DotJournalTests.evidence
    commit = journal_fixtures.DotJournalTests.commit
    row = journal_fixtures.DotJournalTests.row

    def test_full_remote_metadata_is_not_journal_receipt_evidence(self):
        prepared = self.admitted('fixture-with-extra-remote-metadata')
        self.assertTrue(self.authorize(prepared)['send_authorized'])
        record = next(r for r in self.p.dot_journal.for_message(self.mid)['records']
                      if r['part'] == 'original')
        journal_projection = copy.deepcopy(record['message_metadata'])
        self.assertEqual(set(journal_projection), {'weixin_dot_journal'})

        # A real remote message may also carry independently used mirror data.
        # Sending its entire metadata object as journal evidence is a mismatch.
        full_remote_metadata = copy.deepcopy(journal_projection)
        full_remote_metadata['weixin_mirror'] = {
            'version': 1, 'source': 'owner_weixin_dm', 'part': 'original'}
        wrong = self.evidence(prepared, source='history_readback')
        wrong['message_metadata'] = full_remote_metadata
        with self.assertRaisesRegex(WeixinError, 'receipt_evidence_mismatch'):
            self.commit(prepared, source='history_readback', evidence=wrong)
        self.assertEqual(self.row(prepared)['state'], 'authorized')
        self.assertIsNone(self.row(prepared)['receipt_evidence'])
        self.assertFalse(self.authorize(prepared)['send_authorized'])

        # This offline fixture simulates a caller reporting a same-room,
        # same-ID, partial=false readback; it cannot verify the remote message.
        # The receipt accepts only the exact journal part as a single-key object.
        readback = self.evidence(prepared, source='history_readback')
        readback['message_metadata'] = {
            'weixin_dot_journal': full_remote_metadata['weixin_dot_journal']}
        self.assertEqual(readback['message_metadata'], journal_projection)
        self.assertIs(readback['partial'], False)
        committed = self.commit(prepared, source='history_readback', evidence=readback)
        self.assertEqual(committed['state'], 'committed')
        self.assertEqual(committed['receipt_source'], 'history_readback')
        self.assertTrue(committed['positive_readback_recorded'])
        self.assertFalse(committed['target_id_verified_by_bridge'])
        self.assertFalse(committed['dot_visible_verified_by_bridge'])
        self.assertFalse(self.authorize(prepared)['send_authorized'])


if __name__ == '__main__':
    unittest.main()
