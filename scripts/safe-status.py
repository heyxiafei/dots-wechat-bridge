#!/usr/bin/env python3
"""Read safe counts and bounded loopback health, without keys or message bodies."""
import argparse
import http.client
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

ENDPOINTS = ('/healthz', '/readyz')


def poll_observation(last_poll, now):
    """Report local timestamp arithmetic, without an invented freshness SLA."""
    observation = {'basis': 'local_database_last_poll', 'last_poll_at': None,
                   'age_status': 'unavailable', 'freshness': 'not_evaluated'}
    if last_poll is None:
        return None, observation
    if type(last_poll) not in (int, float) or not math.isfinite(last_poll) or last_poll < 0:
        observation['age_status'] = 'invalid_timestamp'
        return None, observation
    observation['last_poll_at'] = last_poll
    if last_poll > now:
        observation['age_status'] = 'future_timestamp'
        return None, observation
    observation['age_status'] = 'observed'
    return round(now - last_poll, 1), observation


def loopback_health(root):
    """Only configured loopback HTTP; do not follow redirects or read bodies.

    HTTP status is an observation of this local address, not identification of
    the online Tunnel, a remote host, or the matching bridge state directory.
    """
    observations = {endpoint: {'state': 'not_observed', 'status_code': None,
                              'error_kind': 'configuration_unavailable'} for endpoint in ENDPOINTS}
    try:
        with (root / 'service-health.url').open(encoding='ascii') as source:
            raw = source.read(2049)
        if len(raw) > 2048:
            raise ValueError('invalid_health_address')
        raw = raw.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in raw):
            raise ValueError('invalid_health_address')
        address = urlsplit(raw)
        if (address.scheme != 'http' or address.hostname != '127.0.0.1'
                or not address.port or address.username is not None or address.password is not None
                or address.path not in ('', '/') or address.query or address.fragment):
            raise ValueError('invalid_health_address')
    except Exception:
        return 'unavailable', observations

    health = {}
    for endpoint in ENDPOINTS:
        connection = None
        result = {'state': 'unavailable', 'status_code': None, 'error_kind': None}
        try:
            connection = http.client.HTTPConnection(address.hostname, address.port, timeout=2)
            connection.request('GET', endpoint)
            status = connection.getresponse().status
            if type(status) is not int or not 100 <= status <= 599:
                result['error_kind'] = 'invalid_response'
            else:
                result.update(state='http_response', status_code=status)
        except TimeoutError:
            result['error_kind'] = 'timeout'
        except OSError:
            result['error_kind'] = 'connection_error'
        except http.client.HTTPException:
            result['error_kind'] = 'protocol_error'
        except Exception:
            result['error_kind'] = 'diagnostic_error'
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        health[endpoint] = result['status_code'] if result['status_code'] is not None else 'unavailable'
        observations[endpoint] = result
    return health, observations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    root = Path(parser.parse_args().state_dir)
    observed_at = time.time()
    with sqlite3.connect('file:' + str(root / 'probe.sqlite') + '?mode=ro', uri=True) as db:
        account = db.execute('SELECT status,last_poll,last_error FROM weixin_accounts LIMIT 1').fetchone()
        age, poll = poll_observation(account[1] if account else None, observed_at)
        result = {'scope': 'local_snapshot', 'observed_at': observed_at,
                  'observed_at_clock': 'host_wall_clock', 'remote_host_mapping': 'unknown',
                  'account_status': account[0] if account else 'not_bound',
                  'last_error': account[2] if account else None,
                  'poll_age_seconds': age, 'poll_observation': poll,
                  'send_counts': dict(db.execute('SELECT status,count(*) FROM weixin_outbox GROUP BY status').fetchall()),
                  'active_subscriptions': dict(db.execute('SELECT name,count(*) FROM subscriptions WHERE (expires IS NULL OR expires>?) GROUP BY name', (observed_at,)).fetchall())}
    result['health'], result['health_observations'] = loopback_health(root)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
