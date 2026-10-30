"""Host capability adapter, not an HTTP client or credential discovery mechanism.

The trusted host supplies a callable for an actually available official tool or
an already-authorized App Server session. Never construct this from owner text,
quoted content, arbitrary JSON, a URL, or an agent's claimed tool availability.
"""
from dataclasses import dataclass
import inspect
import time
from typing import Callable, Optional
from .usage_display import UsageBinding, UsageObservation, CreditInfo
from .fixed_commands import prepare_weekly_usage


class OfficialReadDenied(Exception):
    """Host classified an actual permission refusal; no fallback or retry."""


@dataclass(frozen=True)
class OfficialCapability:
    source: str
    binding: UsageBinding
    binding_evidence_ref: str
    read: Callable
    credit_mapper: Optional[Callable] = None


class OfficialUsageProvider:
    def __init__(self, capability=None, clock=time.time):
        self.capability = capability
        self.clock = clock

    @classmethod
    def from_host_tools(cls, tools, binding, evidence_ref, credit_mapper=None, **options):
        # tools is the host's actual callable registry, never a chat-supplied map.
        reader = tools.get('mcp__codex_app__get_usage_limits')
        capability = (OfficialCapability('get_usage_limits', binding, evidence_ref, lambda: reader({}), credit_mapper)
                      if callable(reader) else None)
        return cls(capability, **options)

    @classmethod
    def from_existing_app_server(cls, authorized_rpc, binding, evidence_ref, credit_mapper=None, **options):
        # The host must already own an authorized session. No process/session is
        # opened here, and no login, account refresh, HTTP request or token read.
        capability = (OfficialCapability('account/rateLimits/read', binding, evidence_ref,
                                        lambda: authorized_rpc('account/rateLimits/read', {}), credit_mapper)
                      if callable(authorized_rpc) else None)
        return cls(capability, **options)

    def _preflight_result(self, capability, binding):
        """Reject invalid host evidence before either entry point calls a reader."""
        valid_binding = (isinstance(binding, UsageBinding) and binding.verified is True
                         and all(isinstance(x,str) and 0 < len(x) <= 256 for x in (binding.owner_ref,binding.account_ref)))
        if not valid_binding:
            return prepare_weekly_usage(None, binding, now=self.clock())
        if (not isinstance(capability, OfficialCapability) or not callable(capability.read)
                or capability.source not in ('get_usage_limits', 'account/rateLimits/read')
                or not isinstance(capability.binding_evidence_ref, str) or not capability.binding_evidence_ref):
            return prepare_weekly_usage(None, binding, now=self.clock())
        if capability.binding != binding:
            return prepare_weekly_usage(None, UsageBinding(binding.owner_ref,binding.account_ref,False), now=self.clock())
        return None

    async def prepare_async(self, binding):
        capability=self.capability
        rejected = self._preflight_result(capability, binding)
        if rejected is not None:
            return rejected
        started=self.clock()
        try:
            payload=capability.read()
            if inspect.isawaitable(payload):payload=await payload
            def completed():return payload
        except Exception as error:
            def completed(error=error):raise error
        # Reuse validation/formatting; completed returns the one already-finished
        # result. It never invokes the host tool a second time.
        adapter=OfficialUsageProvider(OfficialCapability(capability.source,binding,
                                      capability.binding_evidence_ref,completed,capability.credit_mapper),self.clock)
        result=adapter.prepare(binding)
        result['read_started_at']=started
        return result

    def prepare(self, binding):
        capability = self.capability
        rejected = self._preflight_result(capability, binding)
        if rejected is not None:
            return rejected
        started = self.clock()
        try:
            payload = capability.read()  # exactly one official invocation, no fallback
            if inspect.iscoroutine(payload):
                payload.close()
                result=prepare_weekly_usage(None,binding,now=self.clock())
                result['adapter_requires_async']=True
                return result
            value = payload
            for _ in range(4):
                if not isinstance(value, dict):break
                if value.get('isError') is True or 'error' in value:
                    raise RuntimeError('official_read_failed')
                if 'structuredContent' in value:value=value['structuredContent']
                elif 'result' in value:value=value['result']
                else:break
        except OfficialReadDenied:
            result = prepare_weekly_usage(None, binding, now=self.clock())
            result.update(status='permission_denied', official_call_attempted=True)
            result['text'] = result['text'].replace('官方读取未接通，未调用','官方读取被拒，已停止')
        except Exception:
            result = prepare_weekly_usage(None, binding, now=self.clock())
            result.update(status='read_failed', official_call_attempted=True)
            result['text'] = result['text'].replace('官方读取未接通，未调用','官方读取失败')
        else:
            finished = self.clock()
            credits = None
            if callable(capability.credit_mapper):
                try:
                    mapped = capability.credit_mapper(payload)
                    credits = mapped if isinstance(mapped, CreditInfo) else None
                except Exception:
                    pass  # unknown credits do not erase valid usage windows
            observation = UsageObservation(binding.owner_ref, binding.account_ref, finished,
                                           payload, capability.source, credits)
            result = prepare_weekly_usage(lambda _: observation, binding, now=finished)
        result.update(official_source=capability.source, read_started_at=started,
                      read_finished_at=self.clock(), binding_evidence_ref=capability.binding_evidence_ref)
        return result
