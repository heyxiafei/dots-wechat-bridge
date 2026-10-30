"""Opt-in, owner-bound local Weixin maintenance resume; no Gmail arm or sender.

The only setup entry point must be called by the explicitly approved maintenance
installer while the original monitor/dual/worker locks are held. Missing or
malformed authorization remains closed. Credentials never appear in results.
"""
import datetime as dt
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from .operator_config import require_operator_config

POLICY_VERSION = 'wx-auto-resume-v1'
POLICY_FIELDS = frozenset(('version','owner_pin','binding_epoch','credential_digest','wx_cutoff',
    'email_cutoff','email_route','email_proof_digest','subscriptions','hard_stop_at','approval_sha256','reboot_approval_sha256'))


def finite(v): return type(v) in (int,float) and math.isfinite(v)
def canonical(v): return json.dumps(v, ensure_ascii=True, sort_keys=True, separators=(',',':'))
def digest(v): return hashlib.sha256(canonical(v).encode()).hexdigest()
def owner_pin(row): return hashlib.sha256(b'aihot-owner-pin-v1\0'+json.dumps([row['id'],row['owner']],ensure_ascii=True,separators=(',',':')).encode()).hexdigest()


def read_hard_stop():
    """Read non-secret supervisor metadata only; do not read profile/key files."""
    repo=Path(__file__).resolve().parents[2]
    path=repo.parent/'wechat-tunnel-service/service-config.json'
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_size>65536:
        raise ValueError('unsafe_service_config')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r') as f:
        opened=os.fstat(f.fileno())
        if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino): raise ValueError('service_config_changed')
        config=json.load(f)
    if config.get('repo')!=str(repo):raise ValueError('service_repo_mismatch')
    raw=config.get('hard_stop_at')
    if not isinstance(raw,str):raise ValueError('hard_stop_missing')
    stamp=dt.datetime.fromisoformat(raw.replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('hard_stop_timezone')
    result=stamp.timestamp()
    if not finite(result):raise ValueError('hard_stop_invalid')
    return result


def snapshot(db, control, now, hard_stop, operator_config=None):
    operator = require_operator_config(operator_config)
    if not finite(now) or not finite(hard_stop) or now<=0 or now>=hard_stop:
        raise ValueError('time_or_deadline')
    accounts=db.execute('SELECT id,owner,token,base_url,status FROM weixin_accounts LIMIT 2').fetchall()
    if len(accounts)!=1 or accounts[0]['status']!='ready':raise ValueError('owner_not_ready')
    a=accounts[0]
    if not all(isinstance(a[k],str) and 0<len(a[k])<=8192 for k in ('id','owner','token','base_url')):raise ValueError('binding_invalid')
    epoch=db.execute('SELECT epoch FROM weixin_binding_epoch WHERE id=1').fetchone()
    if epoch is None or not isinstance(epoch['epoch'],str) or not re.fullmatch('[0-9a-f]{32}',epoch['epoch']):raise ValueError('binding_epoch_missing')
    pin=owner_pin(a)
    if control['enabled'] is not True or control['owner_pin']!=pin or not finite(control['consumer_activation_at']) or not 0<control['consumer_activation_at']<=now:raise ValueError('wechat_control_changed')
    row=db.execute('SELECT account,peer FROM weixin_inbox WHERE id=?',(operator.approval_id,)).fetchone()
    if not row or (row['account'],row['peer'])!=(a['id'],a['owner']):raise ValueError('approval_owner_changed')
    email=db.execute('SELECT enabled,activation,owner_pin,route_generation,sender_proof FROM aihot_email_config WHERE id=1').fetchone()
    if not email or email['enabled']!=1 or email['owner_pin']!=pin or not finite(email['activation']) or not 0<email['activation']<=now:raise ValueError('email_route_changed')
    route=email['route_generation']
    if not isinstance(route,str) or not re.fullmatch('mail-route-[0-9a-f]{32}',route) or route=='mail-route-'+'0'*32:raise ValueError('email_route_invalid')
    raw_proof=email['sender_proof']
    if not isinstance(raw_proof,str) or len(raw_proof)>8192:raise ValueError('sender_proof_invalid')
    proof=json.loads(raw_proof)
    if json.dumps(proof,ensure_ascii=False,sort_keys=True,separators=(',',':'))!=raw_proof:raise ValueError('sender_proof_invalid')
    required={'message_id','readback_id','link_id','from_address','to','subject','test_marker','sent_at','label_ids','body_verified'}
    if (not isinstance(proof,dict) or set(proof)!=required or proof['link_id']!=operator.primary_link
            or proof['from_address']!=operator.recipient or proof['to']!=operator.recipient or proof['body_verified'] is not True
            or not isinstance(proof['message_id'],str) or re.fullmatch('[a-fA-F0-9]{10,64}',proof['message_id']) is None
            or proof['readback_id']!=proof['message_id'] or proof['subject']!=operator.sender_test_subject
            or proof['test_marker']!=operator.sender_test_marker or not finite(proof['sent_at']) or not 0<proof['sent_at']<=now
            or not isinstance(proof['label_ids'],list) or not all(isinstance(x,str) for x in proof['label_ids'])
            or not {'SENT','INBOX'}<=set(proof['label_ids']) or bool({'DRAFT','TRASH'} & set(proof['label_ids']))):
        raise ValueError('sender_route_invalid')
    subscriptions={}
    for name in ('weixin.message','aihot.codex_reset'):
        rows=db.execute('SELECT id FROM subscriptions WHERE name=? AND (expires IS NULL OR expires>?)',(name,now)).fetchall()
        if len(rows)!=1 or not isinstance(rows[0]['id'],str) or not rows[0]['id']:raise ValueError('subscription_inactive')
        subscriptions[name]=rows[0]['id']
    return dict(version=POLICY_VERSION,owner_pin=pin,binding_epoch=epoch['epoch'],
        credential_digest=digest([a['id'],a['owner'],a['token'],a['base_url']]),
        wx_cutoff=control['consumer_activation_at'],email_cutoff=email['activation'],email_route=route,
        email_proof_digest=digest(proof),subscriptions=subscriptions,hard_stop_at=hard_stop,
        approval_sha256=hashlib.sha256(operator.approval_id.encode()).hexdigest(),
        reboot_approval_sha256=hashlib.sha256(operator.reboot_approval.encode()).hexdigest())


def initialize_approved(db, control, now, hard_stop, operator_config=None):
    """One-time maintenance installer; caller owns original worker and data locks."""
    operator = require_operator_config(operator_config)
    if db.execute('SELECT 1 FROM aihot_auto_resume WHERE id=1').fetchone():raise ValueError('already_configured')
    if db.execute('SELECT 1 FROM weixin_binding_epoch WHERE id=1').fetchone() is None:
        db.execute('INSERT INTO weixin_binding_epoch(id,epoch) VALUES(1,?)',(secrets.token_hex(16),))
    value=snapshot(db,control,now,hard_stop,operator)
    db.execute('INSERT INTO aihot_auto_resume(id,policy) VALUES(1,?)',(canonical(value),))
    return {'result':'configured','wechat_only':True,'historical_replay':False}


def check(db, control, now, hard_stop, operator_config=None):
    try:
        operator = require_operator_config(operator_config)
        row=db.execute('SELECT policy FROM aihot_auto_resume WHERE id=1').fetchone()
        if row is None:return 'auto_authorization_missing'
        raw=row['policy']
        if not isinstance(raw,str) or len(raw)>8192:return 'auto_authorization_invalid'
        policy=json.loads(raw)
        if not isinstance(policy,dict) or set(policy)!=POLICY_FIELDS or policy.get('version')!=POLICY_VERSION:return 'auto_authorization_invalid'
        current=snapshot(db,control,now,hard_stop,operator)
        if not hmac.compare_digest(canonical(policy),canonical(current)):return 'auto_authorization_changed'
        return 'allowed'
    except Exception:
        return 'auto_authorization_unverifiable'
