"""Offline mixed-category and no-replay tests; disposable ledger, no network."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from . import test_dual_producer as fixture
from .test_dual_producer import EID, OLD_CONFIRMED, source_activity, source_event, source_payload, T, m

BANKED='banked-1900000000000000003-1-1'

def banked():
    e=source_event(BANKED)
    e.update(type='reset_credit',status='confirmed',confirmationBasis='receipt_review',confirmedAt=None)
    e['presentation']['status']='confirmed'
    return e

class BankedTests(unittest.TestCase):
    # Reuse isolated test fixture without inheriting its test cases.
    def setUp(self):
        fixture.DualProducerTests.setUp(self)
        self.adapter=sys.modules[m.dual_package().capture_verified.__module__]
    ledger = fixture.DualProducerTests.ledger
    channel = fixture.DualProducerTests.channel
    relay = fixture.DualProducerTests.relay
    poll = fixture.DualProducerTests.poll
    pending = fixture.DualProducerTests.pending

    def test_unverified_default_allowlist_fails_closed(self):
        self.pending()
        with patch.object(self.adapter,'KNOWN_BANKED_TYPES',frozenset()):
            result=self.poll(T+1200,source_payload(T+1200,[banked()],[]))
        self.assertEqual(result['dual_capture'],'blocked')
        self.assertFalse(self.ledger()['healthy'])

    def test_mixed_banked_and_reset_capture_and_related_withdrawal(self):
        self.pending()
        a=source_activity(EID,action='withdraw',published=T+700);a['eventIds']=[BANKED,EID]
        outcome=self.poll(T+1200,source_payload(T+1200,[source_event(),banked()],[a]))
        self.assertEqual(outcome['dual_capture'],'captured')
        ledger=self.ledger();self.assertNotIn(BANKED,ledger['versions']);self.assertNotIn(BANKED,ledger['terminal'])
        self.assertEqual(ledger['terminal'][EID],'withdrawn')
        self.assertEqual(self.channel(channel='wechat')['state'],'suppressed')
        self.assertEqual(self.channel(channel='email')['state'],'suppressed')
    def test_unknown_and_contradictory_categories_fail_closed(self):
        for mutation in ('unknown_id','invalid_reset','reset_with_credit_type','missing_type','unknown_type','direct_reset','wrong_basis','confirmed_time','wrong_presentation','orphan_activity'):
            with self.subTest(mutation=mutation):
                self.pending();b=banked();activities=[]
                if mutation=='unknown_id':b['id']='other-1900000000000000003-1-1'
                elif mutation=='invalid_reset':b['id']='reset-malformed'
                elif mutation=='reset_with_credit_type':b['id']='reset-1900000000000000003-1-1'
                elif mutation=='missing_type':del b['type']
                elif mutation=='unknown_type':b['type']='unrecognized_fixture'
                elif mutation=='direct_reset':b['type']='direct_reset'
                elif mutation=='wrong_basis':b['confirmationBasis']='source_post'
                elif mutation=='confirmed_time':b['confirmedAt']='2026-10-18T22:01:00+08:00'
                elif mutation=='wrong_presentation':b['presentation']['status']='announced'
                else:
                    activities=[source_activity(BANKED,action='withdraw',published=T+700)];b=source_event()
                result=self.poll(T+1200,source_payload(T+1200,[b],activities))
                self.assertEqual(result['dual_capture'],'blocked');self.assertFalse(self.ledger()['healthy'])
                self.tmp.cleanup();self.setUp()
    def test_unhealthy_recovery_preserves_pending_attempts_and_later_new(self):
        self.pending();r=self.relay();claim=r.claim(EID,'announced','wechat',T+601)
        self.assertEqual(claim['result'],'prepared');before=copy.deepcopy(self.channel())
        r.suspend()
        old=source_event(OLD_CONFIRMED,created=T+800,updated=T+801)
        acts=[source_activity(EID),source_activity(OLD_CONFIRMED,published=T+800)]
        result=self.poll(T+1200,source_payload(T+1200,[source_event(),old,banked()],acts))
        self.assertEqual(result['dual_capture'],'captured');self.assertTrue(self.ledger()['healthy'])
        self.assertEqual(self.channel(),before)
        self.assertEqual(self.channel(channel='email')['state'],'pending')
        for channel in ('wechat','email'):self.assertEqual(self.channel(channel=channel,eid=OLD_CONFIRMED)['state'],'pending')
        recovered=r.claim(EID,'announced','wechat',T+1201)
        self.assertTrue(recovered['query_only']);self.assertEqual(recovered['request_id'],before['request_id'])
        new='reset-1900000000000000004-1-1';latest=source_event(new,created=T+1300,updated=T+1301)
        acts.append(source_activity(new,published=T+1300))
        self.poll(T+1800,source_payload(T+1800,[source_event(),old,banked(),latest],acts))
        for channel in ('wechat','email'):self.assertEqual(self.channel(channel=channel,eid=new)['state'],'pending')
        self.assertEqual(self.channel()['request_id'],before['request_id'])

    def test_recovery_uses_original_cutoff_for_old_and_new_sources(self):
        m.dual_toggle(self.data,True,now=T);self.poll(T,source_payload(T))
        self.relay().suspend()
        older=source_event(EID,created=T-100,updated=T-90)
        newer=source_event(OLD_CONFIRMED,created=T+50,updated=T+60)
        acts=[source_activity(EID,published=T-100),source_activity(OLD_CONFIRMED,published=T+50)]
        self.assertEqual(self.poll(T+600,source_payload(T+600,[older,newer,banked()],acts))['dual_capture'],'captured')
        for name in ('wechat','email'):
            self.assertEqual(self.channel(channel=name,eid=EID)['state'],'suppressed')
            self.assertEqual(self.channel(channel=name,eid=OLD_CONFIRMED)['state'],'pending')
        self.assertEqual(self.ledger()['activation_at'],T)

if __name__=='__main__':unittest.main()
