"""Explicit trusted-host metadata for the optional private AIHOT integration.

There are no deployment defaults, environment lookups or configuration-file
discovery. Constructing this value does not authorize a send, arm a consumer,
verify an external approval, or create credentials. Existing owner, fencing,
subscription and platform-approval checks remain necessary.
"""
from dataclasses import dataclass
import hashlib
import re


@dataclass(frozen=True, slots=True, repr=False)
class AihotOperatorConfig:
    primary_link: str
    recipient: str
    profile_emails: frozenset[str]
    approval_id: str
    reboot_approval: str
    approval_message_sha256: str
    sender_test_subject: str
    sender_test_marker: str

    def __post_init__(self):
        def text(value, limit=256):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                return False
            try:
                return len(value.encode('utf-8')) <= limit and all(ord(char) >= 32 for char in value)
            except UnicodeEncodeError:
                return False

        def address(value):
            return (text(value, 254) and value == value.lower()
                    and re.fullmatch(r'[a-z0-9.!#$%&*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}', value))

        valid = (text(self.primary_link, 128)
                 and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', self.primary_link)
                 and address(self.recipient)
                 and isinstance(self.profile_emails, frozenset)
                 and self.recipient in self.profile_emails and 1 <= len(self.profile_emails) <= 16
                 and all(address(value) for value in self.profile_emails)
                 and isinstance(self.approval_id, str)
                 and re.fullmatch(r'wx_[a-f0-9]{32}', self.approval_id)
                 and text(self.reboot_approval)
                 and isinstance(self.approval_message_sha256, str)
                 and re.fullmatch(r'[a-f0-9]{64}', self.approval_message_sha256)
                 and self.approval_message_sha256 == hashlib.sha256(self.approval_id.encode()).hexdigest()
                 and text(self.sender_test_subject) and text(self.sender_test_marker))
        if not valid:
            raise ValueError('invalid_aihot_operator_configuration')


def require_operator_config(value):
    if not isinstance(value, AihotOperatorConfig):
        raise ValueError('aihot_operator_configuration_required')
    return value
