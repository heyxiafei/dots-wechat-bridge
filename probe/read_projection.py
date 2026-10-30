"""Opt-in reply read projection. No persistence, network, or write authority."""
import hashlib
import json
import math


def request_id_digest(request_id):
    if type(request_id) not in (str, int):
        return None
    raw = json.dumps(request_id, ensure_ascii=True, separators=(',', ':')).encode()
    return hashlib.sha256(b'read-rpc-v1\0' + raw).hexdigest()


def elapsed_ms(start, finish):
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in (start, finish))
            or finish < start):
        return None
    return round((finish - start) * 1000, 3)


def reply_result(content, observation):
    """Full source stays once in structuredContent; old clients use full mode."""
    projected = dict(content)
    projected['read_observation'] = dict(observation)
    timing = projected.get('transport_timing')
    if isinstance(timing, dict):
        timing = dict(timing)
        times = timing.get('timestamps', {})
        accepted = times.get('webhook_accepted_at') if isinstance(times, dict) else None
        timing['webhook_http_accepted_observed'] = type(accepted) in (int, float) and math.isfinite(accepted)
        timing['platform_admission_confirmed'] = None
        timing['webhook_acceptance_basis'] = 'http_2xx_only' if timing['webhook_http_accepted_observed'] else 'not_observed'
        projected['transport_timing'] = timing
    summary = dict(message_id=content['message_id'], source=content['source'],
        projection='reply', complete_source_location='structuredContent',
        text_summary_is_not_source=True, must_read_structured_content=True,
        mirror_inventory_omitted=True, server_read_work_elapsed_ms=observation['read_work_elapsed_ms'])
    return {'content': [{'type': 'text', 'text': json.dumps(summary, sort_keys=True,
                ensure_ascii=False, separators=(',', ':'))}], 'structuredContent': projected}
