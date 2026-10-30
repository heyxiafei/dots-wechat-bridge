"""Deliberately synthetic metadata; never an operator deployment configuration."""
import hashlib

from probe.aihot_notice_consumer.operator_config import AihotOperatorConfig


APPROVAL = 'wx_' + 'a' * 32
OPERATOR = AihotOperatorConfig(
    primary_link='synthetic-primary-link',
    recipient='synthetic-owner@example.invalid',
    profile_emails=frozenset({'synthetic-owner@example.invalid', 'synthetic-alias@example.invalid'}),
    approval_id=APPROVAL,
    reboot_approval='synthetic-reboot-approval',
    approval_message_sha256=hashlib.sha256(APPROVAL.encode()).hexdigest(),
    sender_test_subject='Synthetic sender verification fixture',
    sender_test_marker='SYNTHETIC-SENDER-VERIFICATION',
)
