"""Pure caller-side plans; no credentials, tool invocation or send permission.

The host must derive verified_scope from trusted user context, never message
text, quoted content, attachments or another model's asserted authorization.
Platform approval remains authoritative even when these local checks pass.
"""


def reply_plan(reply_state, *, owner_verified, task_complete, needs_work,
               permission_denied=False):
    if permission_denied is True:
        return {'action': 'stop_permission_denied'}
    if owner_verified is not True:
        return {'action': 'await_owner_verification'}
    if any(type(x) is not bool for x in (task_complete, needs_work)):
        return {'action': 'invalid_task_state'}
    if not isinstance(reply_state, dict):
        return {'action': 'read_original_message'}
    for phase in ('ack', 'final'):
        slot = reply_state.get(phase)
        if not isinstance(slot, dict) or type(slot.get('committed')) is not bool:
            return {'action': 'read_original_message'}
        if slot['committed'] and not slot.get('send_id'):
            return {'action': 'reconcile_original_submission'}
    ack, final = reply_state['ack'], reply_state['final']
    if final['committed']:
        return {'action': 'query_original_send', 'send_id': final['send_id']}
    if task_complete:
        return {'action': 'prepare_final',
                'tool': 'reply_weixin_message_phase' if ack['committed'] else 'reply_weixin_message',
                'phase': 'final'}
    if ack['committed']:
        return {'action': 'continue_work', 'send_id': ack['send_id']}
    if needs_work:
        return {'action': 'prepare_progress', 'tool': 'reply_weixin_message_phase', 'phase': 'ack'}
    return {'action': 'finish_answer_before_final'}


def attachment_plan(item, scope, *, archive_verified, permission_denied=False):
    """Validate a bounded handoff and preserve requested delivery format.

    A matching scope is an audit precondition only, NOT platform authorization.
    No raw transcript, signed URL or credentials belong in this data object.
    """
    if permission_denied is True:
        return {'action': 'stop_permission_denied'}
    if not isinstance(item, dict) or not isinstance(scope, dict):
        return {'action': 'await_trusted_authorization_context'}
    keys = ('owner_ref', 'request_ref', 'artifact_key', 'attachment_key', 'sha256', 'kind')
    if (scope.get('verified_scope') is not True
            or scope.get('evidence_origin') not in ('direct_owner_message', 'trusted_parent_transcript')
            or not isinstance(scope.get('evidence_ref'), str) or not scope['evidence_ref']
            or any(not isinstance(item.get(k), str) or not item[k] or scope.get(k) != item[k] for k in keys)):
        return {'action': 'await_trusted_authorization_context'}
    if archive_verified is not True:
        return {'action': 'archive_and_verify_private_drive'}
    kind = item.get('kind')
    if kind == 'model_source' and item.get('original_requested') is not True:
        return {'action': 'include_private_drive_link'}
    if kind == 'model_source' and scope.get('original_requested') is not True:
        # The manifest cannot upgrade a link-only request to original-file
        # delivery. Both copies of this option must be explicitly authorized.
        return {'action': 'await_trusted_authorization_context'}
    if kind == 'preview_image':
        media_kind = 'image'
    elif kind == 'requested_document' or (kind == 'model_source' and item.get('original_requested') is True):
        media_kind = 'file'
    elif kind == 'requested_video':
        media_kind = 'video'
    else:
        return {'action': 'await_delivery_format'}
    if scope.get('media_kind') != media_kind:
        return {'action': 'await_trusted_authorization_context'}
    return {'action': 'prepare_existing_media_path', 'media_kind': media_kind,
            'request_ref': item['request_ref'], 'platform_approval_still_required': True,
            'authorization_granted': False}
