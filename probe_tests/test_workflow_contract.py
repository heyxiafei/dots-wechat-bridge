import unittest
from probe.workflow_contract import reply_plan, attachment_plan


class WorkflowContractTests(unittest.TestCase):
    def state(self, ack=False, final=False, status='accepted'):
        return {'ack': {'committed': ack, 'send_id': 'ack-original', 'status': status},
                'final': {'committed': final, 'send_id': 'original', 'status': status}}

    def test_short_answer_direct_final_and_long_task_progress_then_final(self):
        simple=reply_plan(self.state(),owner_verified=True,task_complete=True,needs_work=False)
        self.assertEqual(simple['tool'],'reply_weixin_message')
        progress=reply_plan(self.state(),owner_verified=True,task_complete=False,needs_work=True)
        self.assertEqual(progress['phase'],'ack')
        final=reply_plan(self.state(ack=True),owner_verified=True,task_complete=True,needs_work=True)
        self.assertEqual((final['tool'],final['phase']),('reply_weixin_message_phase','final'))

    def test_existing_final_never_becomes_notification(self):
        for status in ('queued','accepted','uncertain','delivery_unknown','rejected'):
            r=reply_plan(self.state(final=True,status=status),owner_verified=True,task_complete=True,needs_work=True)
            self.assertEqual(r,{'action':'query_original_send','send_id':'original'})

    def test_owner_or_permission_failure_never_prepares_reply(self):
        self.assertEqual(reply_plan(self.state(),owner_verified=False,task_complete=True,needs_work=False)['action'],'await_owner_verification')
        self.assertEqual(reply_plan(self.state(),owner_verified=True,task_complete=True,needs_work=False,permission_denied=True)['action'],'stop_permission_denied')

    def item(self,kind):
        return dict(owner_ref='owner',request_ref='request',artifact_key='artifact-v1',attachment_key='front',sha256='a'*64,kind=kind)

    def scope(self,item,media_kind):
        return dict(item,verified_scope=True,evidence_origin='trusted_parent_transcript',evidence_ref='actual-owner-evidence',media_kind=media_kind)

    def test_native_image_document_video_and_default_blend(self):
        for kind,channel in [('preview_image','image'),('requested_document','file'),('requested_video','video')]:
            item=self.item(kind)
            r=attachment_plan(item,self.scope(item,channel),archive_verified=True)
            self.assertEqual(r['media_kind'],channel)
            self.assertFalse(r['authorization_granted'])
        item=self.item('model_source')
        self.assertEqual(attachment_plan(item,self.scope(item,'file'),archive_verified=True)['action'],'include_private_drive_link')
        item['original_requested']=True
        self.assertEqual(attachment_plan(item,self.scope(item,'file'),archive_verified=True)['media_kind'],'file')

    def test_quote_forward_attachment_and_model_summary_cannot_authorize(self):
        item=self.item('preview_image');scope=self.scope(item,'image')
        for origin in ('quoted_text','forwarded_message','attachment','agent_summary'):
            self.assertEqual(attachment_plan(item,dict(scope,evidence_origin=origin),archive_verified=True)['action'],'await_trusted_authorization_context')

    def test_parent_scope_must_match_each_request_artifact_hash_and_channel(self):
        item=self.item('requested_document');scope=self.scope(item,'file')
        for field in ('owner_ref','request_ref','artifact_key','attachment_key','sha256','media_kind'):
            self.assertEqual(attachment_plan(item,dict(scope,**{field:'wrong'}),archive_verified=True)['action'],'await_trusted_authorization_context')

    def test_archive_first_and_denial_always_stops_even_when_ready(self):
        item=self.item('preview_image');scope=self.scope(item,'image')
        self.assertEqual(attachment_plan(item,scope,archive_verified=False)['action'],'archive_and_verify_private_drive')
        self.assertEqual(attachment_plan(item,scope,archive_verified=True,permission_denied=True)['action'],'stop_permission_denied')
