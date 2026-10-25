"""Exact MCP read timing versus internal validation, with no network or private data."""
import json
import tempfile
import unittest
from unittest.mock import patch
from probe.probe import Probe


class TimingRPCIntegrationTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.getaddrinfo','socket.create_connection','socket.socket.connect','socket.socket.connect_ex'):
            guard=patch(name,side_effect=AssertionError('network prohibited'))
            guard.start(); self.addCleanup(guard.stop)
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now=1800000000.
        self.p=Probe(self.temp.name,clock=lambda:self.now)
        self.addCleanup(self.p.close)
        self.p.weixin.bind_confirmed({'status':'confirmed','ilink_bot_id':'fixture-bot',
            'ilink_user_id':'fixture-owner','bot_token':'synthetic-secret-fixture-0000000000000000'})
        self.p.weixin.ingest(self.p.weixin.account(),{'msgs':[{'message_id':'fixture-input',
            'from_user_id':'fixture-owner','to_user_id':'fixture-bot','message_type':1,'message_state':2,
            'context_token':'synthetic-secret-fixture-0000000000000000','item_list':[{'type':1,'text_item':{'text':'fixture question'}}]}]})
        self.mid=self.p.db.execute('SELECT id FROM weixin_inbox').fetchone()[0]

    def read(self, **args):
        return self.p.rpc({'jsonrpc':'2.0','id':1,'method':'tools/call',
                          'params':{'name':'read_weixin_message','arguments':args or {'message_id':self.mid}}})

    def test_only_successful_exact_rpc_counts_not_internal_validation(self):
        self.p.weixin.read_message(self.mid)
        self.p.weixin.queue_reply(self.mid,'fixture final')
        self.assertEqual(self.p.delivery_timing.for_message(self.mid)['agent_read_count'],0)
        first=self.read()['result']['structuredContent']['transport_timing']
        self.assertEqual(first['agent_read_count'],1)
        self.now+=2
        second=self.read()['result']['structuredContent']['transport_timing']
        self.assertEqual(second['agent_read_count'],2)
        self.assertEqual(first['timestamps']['first_agent_read_at'],second['timestamps']['first_agent_read_at'])
        self.assertEqual(second['timestamps']['last_agent_read_at'],self.now)
        self.assertFalse(second['dot_processing_confirmed'])
        self.assertFalse(second['phone_delivery_observed'])

    def test_unknown_or_invalid_rpc_does_not_record_success(self):
        self.assertIn('error',self.read(message_id='fixture-unknown'))
        self.assertIn('error',self.read(message_id=self.mid,extra=True))
        self.assertEqual(self.p.delivery_timing.for_message(self.mid)['agent_read_count'],0)

    def test_observation_failure_cannot_fail_owner_read_or_leak_exception(self):
        with patch.object(self.p.delivery_timing,'agent_read',side_effect=RuntimeError('EXCEPTION_CANARY')):
            result=self.read()
        self.assertNotIn('error',result)
        self.assertEqual(result['result']['structuredContent']['text'],'fixture question')
        self.assertEqual(result['result']['structuredContent']['transport_timing'],
                         {'available':False,'reason':'metadata_unavailable'})
        safe=json.dumps(result)
        for canary in ('EXCEPTION_CANARY','fixture-context-not-real','fixture-credential-not-real','fixture-owner'):
            self.assertNotIn(canary,safe)

    def test_historical_missing_timestamps_are_not_backfilled_by_rpc_read(self):
        with self.p.db:
            self.p.db.execute('DELETE FROM weixin_delivery_timing WHERE message_id=?',(self.mid,))
        timing=self.read()['result']['structuredContent']['transport_timing']
        for field in ('ingest_observed_at','ingest_commit_observed_at','event_queued_at','webhook_accepted_at'):
            self.assertIsNone(timing['timestamps'][field])
        self.assertEqual(timing['timestamps']['first_agent_read_at'],self.now)
        self.assertEqual(timing['agent_read_count'],1)

    def test_read_does_not_create_or_activate_system_receipts(self):
        self.read()
        self.assertFalse(self.p.receipts.status()['running'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM owner_receipt_intents').fetchone()[0],0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0],0)
