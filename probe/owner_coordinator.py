"""Single local coordinator adapter with durable per-part submission records.

Host call_tool must use the original permission-checked platform tool surface.
Host permit must verify current writer/fencing and trusted owner authorization;
neither configuration nor this journal grants permission. Local flock does not
fence an old worker or another machine. No listeners/subscriptions are created.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from .workflow_contract import reply_plan, attachment_plan
from .official_usage_provider import OfficialUsageProvider
from .fixed_commands import classify as classify_fixed_command
from .usage_display import UsageBinding


class PlatformDenied(Exception):
    """A real platform denial; no alternate interface or retry is attempted."""


def from_host_configuration(config, directory, *, caller_id, call_tool, binding=None,
                            binding_evidence_ref=None, official_tools=None,
                            authorized_app_server_rpc=None, credit_mapper=None, permit=None,
                            reply_projection_supported=False):
    """Connect operator configuration to trusted live host capabilities.

    Configuration selects a provider; it cannot create credentials, manufacture
    an account binding, or enable a tool absent from the current host registry.
    No automatic fallback to another session after a rejection is supported.
    """
    if not isinstance(config,dict) or config.get('version')!=1:
        raise ValueError('invalid_coordinator_config')
    if config.get('system_ack') is not False or config.get('old_dot_mirror_writes') is not False:
        raise ValueError('unsupported_legacy_writes')
    source=config.get('usage_provider')
    if source=='get_usage_limits':
        provider=OfficialUsageProvider.from_host_tools(official_tools or {},binding,binding_evidence_ref,credit_mapper)
    elif source=='account/rateLimits/read':
        provider=OfficialUsageProvider.from_existing_app_server(authorized_app_server_rpc,binding,binding_evidence_ref,credit_mapper)
    else:
        raise ValueError('unknown_official_provider')
    return OwnerCoordinator(directory,coordinator_id=config.get('coordinator_id'),caller_id=caller_id,
                            call_tool=call_tool,mode=config.get('mode','dry_run'),permit=permit,
                            usage_provider=provider,reply_projection_supported=reply_projection_supported)


class OwnerCoordinator:
    def __init__(self, directory, *, coordinator_id, caller_id, call_tool,
                 mode='dry_run', permit=None, usage_provider=None, reply_projection_supported=False):
        if caller_id != coordinator_id or not coordinator_id:
            raise ValueError('coordinator_identity_mismatch')
        if mode not in ('dry_run','execute') or not callable(call_tool):
            raise ValueError('invalid_coordinator_configuration')
        if type(reply_projection_supported) is not bool:
            raise ValueError('invalid_reply_projection_capability')
        root=Path(directory); root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=os.open(root/'coordinator.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:
            os.close(self.lock);raise
        self.db=sqlite3.connect(root/'coordinator.sqlite',check_same_thread=False)
        os.chmod(root/'coordinator.sqlite',0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, send_id TEXT, api_state TEXT, owner_receipt TEXT)')
        self.serial=threading.RLock()
        self._fixed_inflight=set()
        self.call_tool=call_tool;self.mode=mode;self.permit=permit
        # Host attests the actual tool schema and decodes structuredContent.
        # Older tools retain their original arguments; never probe by retrying.
        self.reply_projection_supported=reply_projection_supported
        self.usage=usage_provider or OfficialUsageProvider()

    def close(self):
        self.db.close();fcntl.flock(self.lock,fcntl.LOCK_UN);os.close(self.lock)

    def prepare_usage(self, binding):
        return self.usage.prepare(binding)

    async def prepare_usage_async(self, binding):
        return await self.usage.prepare_async(binding)

    @staticmethod
    def _fixed_scope(scope):
        # These attestations come from the trusted host, never the message/hint.
        return tuple(scope.get(k) for k in
                     ('verified_scope','request_ref','evidence_origin','owner_ref','account_ref'))

    def _fixed_prior(self, message_id):
        # No freshly read quota/digest is needed to reconcile an earlier attempt.
        for identity in (['text',message_id,'final'], ['text',message_id,'ack'],
                         ['official_usage_read',message_id]):
            key=hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest()
            row=self.db.execute('SELECT state,send_id,api_state,owner_receipt FROM actions WHERE key=?',(key,)).fetchone()
            if row:
                return {'action':'stop_permission_denied' if row[0]=='denied' else
                        'query_original_send' if row[1] else 'reconcile_original_submission',
                        'key':key,'send_id':row[1],'api_state':row[2],'owner_received':bool(row[3])}
        return None

    def _fixed_check(self, message_id, scope, owner_intent_verified):
        if (owner_intent_verified is not True or not isinstance(scope,dict)
                or scope.get('verified_scope') is not True or scope.get('request_ref') != message_id
                or scope.get('evidence_origin') not in ('direct_owner_message','trusted_parent_transcript')):
            return None,None,{'action':'await_owner_verification'}
        message=self._owner_message(message_id)
        prior=self._fixed_prior(message_id)
        if prior:return None,None,prior
        state=message.get('reply_state')
        if (not isinstance(state,dict) or any(not isinstance(state.get(p),dict)
                or type(state[p].get('committed')) is not bool for p in ('ack','final'))):
            return None,None,{'action':'read_original_message'}
        for phase in ('final','ack'):
            slot=state[phase]
            if slot['committed']:
                sid=slot.get('send_id')
                return None,None,({'action':'query_original_send','send_id':sid}
                                  if isinstance(sid,str) and sid else {'action':'reconcile_original_submission'})
        hint=classify_fixed_command(message.get('text'),message.get('kind'),
                message.get('inbound_completeness'),message.get('quote_context'),
                message.get('attachments'),state,message.get('help_menu'))
        if not hint or hint.get('state') != 'candidate':
            return None,None,{'action':'not_fixed_command'}
        return message,hint,None

    def _begin_fixed(self, message_id, scope, owner_intent_verified):
        with self.serial:
            message,hint,stop=self._fixed_check(message_id,scope,owner_intent_verified)
            if stop:return None,stop
            if message_id in self._fixed_inflight:
                return None,{'action':'fixed_command_in_progress'}
            self._fixed_inflight.add(message_id)
            return (message['text'],hint,self._fixed_scope(scope)),None

    @staticmethod
    def _fixed_binding(binding, scope):
        # read_weixin_message does not expose an owner/account mapping. Only the
        # host can attest it, independently of text and accountId=null results.
        if (isinstance(binding,UsageBinding) and scope.get('owner_ref') == binding.owner_ref
                and scope.get('account_ref') == binding.account_ref):
            return binding
        return None

    def _finish_fixed(self, message_id, scope, owner_intent_verified, context, prepared):
        with self.serial:
            if prepared.get('status') == 'permission_denied':
                # Record the real refusal even if scope/message state changed
                # during the read, or this was a dry run. This is a stop record,
                # not a send claim, and must survive another handler invocation.
                key,digest,_=self._existing(['official_usage_read',message_id],{'status':'permission_denied'})
                with self.db:self.db.execute('INSERT OR IGNORE INTO actions VALUES(?,?,?,NULL,?,NULL)',
                                              (key,digest,'denied','permission_denied'))
                return {'action':'stop_permission_denied','usage_status':'permission_denied',
                        'text':prepared['text'],'official_call_attempted':True}
            message,hint,stop=self._fixed_check(message_id,scope,owner_intent_verified)
            if stop:return stop
            text,original_hint,original_scope=context
            if self._fixed_scope(scope) != original_scope:
                return {'action':'await_owner_verification'}
            if message['text'] != text or hint != original_hint:
                return {'action':'fixed_command_changed'}
            if prepared.get('adapter_requires_async') is True:
                return {'action':'requires_async_usage_reader','official_call_attempted':False}
            result=self._reply_checked(message_id,message,prepared['text'],
                                       task_complete=True,needs_work=False,scope=scope)
            return {**result,'fixed_command':hint['kind'],'usage_status':prepared.get('status'),
                    'official_call_attempted':prepared.get('official_call_attempted',False)}

    def handle_fixed_command(self, message_id, *, scope, binding=None, owner_intent_verified=False):
        """Trusted-host dispatch, not an event subscription or model authority.

        The host must verify direct owner intent (the protocol cannot prove
        forwarding provenance), scope.owner_ref/account_ref and provider proof.
        No hint, message text or JSON supplies those attestations. Missing quota
        capability/binding produces four unknown lines through the normal final
        path; every write still requires the live host permit. Existing ack/final
        commitments are reconciled before a reader is invoked.
        """
        context,stop=self._begin_fixed(message_id,scope,owner_intent_verified)
        if stop:return stop
        try:
            hint=context[1]
            prepared=(self.prepare_usage(self._fixed_binding(binding,scope))
                      if hint['kind']=='weekly_usage' else {'text':hint['prepared_text']})
            return self._finish_fixed(message_id,scope,owner_intent_verified,context,prepared)
        finally:
            with self.serial:self._fixed_inflight.discard(message_id)

    async def handle_fixed_command_async(self, message_id, *, scope, binding=None, owner_intent_verified=False):
        """Same contract, awaiting only the existing official-provider interface.

        A short shared marker fences sync/async duplicates; no threading lock is
        held over await. Cancellation propagates and releases the read-only
        marker. Tool submission remains the host's supported synchronous bridge.
        """
        context,stop=self._begin_fixed(message_id,scope,owner_intent_verified)
        if stop:return stop
        try:
            hint=context[1]
            prepared=(await self.prepare_usage_async(self._fixed_binding(binding,scope))
                      if hint['kind']=='weekly_usage' else {'text':hint['prepared_text']})
            return self._finish_fixed(message_id,scope,owner_intent_verified,context,prepared)
        finally:
            with self.serial:self._fixed_inflight.discard(message_id)

    def _owner_message(self, message_id):
        args={'message_id':message_id}
        if self.reply_projection_supported:
            args['projection']='reply'
        message=self.call_tool('read_weixin_message',args)
        if (not isinstance(message,dict) or message.get('message_id') != message_id
                or message.get('source') != 'owner_weixin_dm'):
            raise ValueError('owner_message_not_verified')
        if self.reply_projection_supported and any(not isinstance(message.get(key),kind)
                for key,kind in (('text',str),('attachments',list),('quote_context',dict),
                                 ('inbound_completeness',dict),('reply_state',dict))):
            raise ValueError('reply_projection_incomplete')
        return message

    def _existing(self, identity, payload):
        key=hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest()
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        old=self.db.execute('SELECT digest,state,send_id,api_state,owner_receipt FROM actions WHERE key=?',(key,)).fetchone()
        if not old:return key,digest,None
        # Re-generated content must not conceal a previous refusal or unknown
        # submission. Preserve its identity/status; report conflict separately.
        return key,digest,{'action':'stop_permission_denied' if old[1]=='denied' else
                          'query_original_send' if old[2] else 'reconcile_original_submission',
                          'key':key,'send_id':old[2],'api_state':old[3],'owner_received':bool(old[4]),
                          'content_conflict':old[0] != digest}

    def _submit(self, identity, payload, tool, arguments, scope):
        with self.serial:
            key,digest,old=self._existing(identity,payload)
            if old:return old
            if self.mode == 'dry_run':
                return {'action':'dry_run','tool':tool,'arguments':arguments,'key':key,'authorization_granted':False}
            try:
                permitted=callable(self.permit) and self.permit(scope,tool) is True
            except PlatformDenied:
                # A real refusal from the trusted host is durable even before
                # submission. False/missing permits merely wait; other errors
                # propagate without a claim or tool call.
                with self.db:self.db.execute('INSERT INTO actions VALUES(?,?,?,NULL,NULL,NULL)',(key,digest,'denied'))
                return {'action':'stop_permission_denied','key':key}
            if not permitted:
                return {'action':'await_current_writer_and_owner_permission','key':key}
            # Durable before the tool boundary; a crash/timeout remains uncertain.
            with self.db:self.db.execute('INSERT INTO actions VALUES(?,?,?,NULL,NULL,NULL)',(key,digest,'claimed'))
            try:
                result=self.call_tool(tool,arguments)
            except PlatformDenied:
                with self.db:self.db.execute("UPDATE actions SET state='denied' WHERE key=?",(key,))
                return {'action':'stop_permission_denied','key':key}
            except Exception:
                with self.db:self.db.execute("UPDATE actions SET state='unknown' WHERE key=?",(key,))
                return {'action':'reconcile_original_submission','key':key}
            sid=result.get('send_id') if isinstance(result,dict) else None
            status=result.get('status') if isinstance(result,dict) else None
            if not isinstance(sid,str) or not sid:
                with self.db:self.db.execute("UPDATE actions SET state='unknown' WHERE key=?",(key,))
                return {'action':'reconcile_original_submission','key':key}
            with self.db:self.db.execute("UPDATE actions SET state='submitted',send_id=?,api_state=? WHERE key=?",
                                        (sid,status if isinstance(status,str) else 'unknown',key))
            return {'action':'query_original_send','key':key,'send_id':sid,'api_state':status,'owner_received':False}

    def reply(self, message_id, text, *, task_complete, needs_work, scope):
        with self.serial:
            message=self._owner_message(message_id)
            return self._reply_checked(message_id,message,text,task_complete=task_complete,
                                       needs_work=needs_work,scope=scope)

    def _reply_checked(self, message_id, message, text, *, task_complete, needs_work, scope):
        # Caller holds serial and has just read/verified this owner's message.
        trusted=(isinstance(scope,dict) and scope.get('verified_scope') is True
                     and scope.get('request_ref')==message_id
                     and scope.get('evidence_origin') in ('direct_owner_message','trusted_parent_transcript'))
        plan=reply_plan(message.get('reply_state'),owner_verified=trusted,
                            task_complete=task_complete,needs_work=needs_work)
        if plan['action'] not in ('prepare_final','prepare_progress'):return plan
        # Bound inspection before hashing; defer errors until the original
        # durable action has had priority. Invalid replacement text cannot hide
        # a denial/unknown or consume a new claim, including in fixed commands.
        error=None
        if not isinstance(text,str):error='reply_text_required'
        elif len(text)>8192:error='reply_text_must_be_1_to_8192_bytes'
        elif not text.strip():error='reply_text_required'
        else:
            try:
                if len(text.encode('utf-8'))>8192:error='reply_text_must_be_1_to_8192_bytes'
            except UnicodeEncodeError:
                error='reply_text_must_be_1_to_8192_bytes'
        identity=['text',message_id,plan['phase']]
        # Keep even invalid strings comparable to legacy claims. None is only
        # a comparison sentinel for non-strings, never a submitted payload.
        payload={'text':text} if isinstance(text,str) else None
        _,_,old=self._existing(identity,payload)
        if old:return old
        if error:raise ValueError(error)
        args={'message_id':message_id,'text':text}
        if plan['tool']=='reply_weixin_message_phase':args['phase']=plan['phase']
        return self._submit(identity,payload,plan['tool'],args,scope)

    def media(self, item, scope, *, archive_verified, upload_id):
        with self.serial:
            plan=attachment_plan(item,scope,archive_verified=archive_verified)
            if plan['action']!='prepare_existing_media_path':return plan
            self._owner_message(item['request_ref'])  # existing text final does not consume attachments
            identity=['media',item['owner_ref'],item['request_ref'],item['artifact_key'],item['attachment_key']]
            payload={'sha256':item['sha256'],'kind':plan['media_kind']}
            _,_,old=self._existing(identity,payload)
            if old:return old
            upload=self.call_tool('get_weixin_upload_status',{'upload_id':upload_id})
            if (not isinstance(upload,dict) or upload.get('sha256')!=item['sha256']
                    or upload.get('kind')!=plan['media_kind'] or upload.get('status')!='ready'):
                return {'action':'query_original_upload'}
            send_key='delivery_'+hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest()
            args={'message_id':item['request_ref'],'send_id':send_key,'upload_id':upload_id}
            return self._submit(identity,payload,'send_weixin_media',args,scope)

    def record_owner_receipt(self, key, *, send_id, evidence_ref):
        if not isinstance(evidence_ref,str) or not evidence_ref:raise ValueError('receipt_evidence_required')
        with self.serial,self.db:
            changed=self.db.execute('UPDATE actions SET owner_receipt=? WHERE key=? AND send_id=? AND state=?',
                                    (evidence_ref,key,send_id,'submitted')).rowcount
            if changed!=1:raise ValueError('receipt_identity_mismatch')
