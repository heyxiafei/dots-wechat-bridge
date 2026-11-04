#!/usr/bin/env python3
"""Render private configuration; values are identifiers/references, never secret bytes."""
import argparse
import os
from pathlib import Path
import re
import shlex


def render(root, tunnel_id, callback_hosts, replace=False, discover_callback=False, fresh_production=False,
           connection_only=False, connection_route_digest=None, connection_subscription_id=None,
           connection_duration_seconds=180):
    root=Path(root).resolve()
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(root)):
        raise ValueError('Use an absolute installation path without spaces or shell metacharacters')
    if not re.fullmatch(r'tunnel_[a-f0-9]{32}',tunnel_id):
        raise ValueError('Use the actual tunnel ID returned by Platform')
    if discover_callback and callback_hosts:
        raise ValueError('Discovery must deny all callbacks; do not combine it with callback hosts')
    if (not discover_callback and not 1<=len(callback_hosts)<=3) or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?',h) or '.' not in h or '..' in h for h in callback_hosts):
        raise ValueError('Supply 1..3 exact callback hostnames observed from the authorized platform subscription')
    if connection_only:
        if (fresh_production or discover_callback or len(callback_hosts) != 1
                or not isinstance(connection_route_digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}',connection_route_digest)
                or connection_subscription_id != 'sub_' + connection_route_digest[:32]
                or type(connection_duration_seconds) is not int or not 1 <= connection_duration_seconds <= 180):
            raise ValueError('Connection-only requires one verified original route and finite foreground lifetime')
    elif connection_route_digest is not None or connection_subscription_id is not None or connection_duration_seconds != 180:
        raise ValueError('Connection-only scope cannot be used in another mode')
    runtime=root/'runtime'; runtime.mkdir(mode=0o700,exist_ok=True)
    os.chmod(runtime,0o700)
    for name in ('state','secrets'):
        target=runtime/name; target.mkdir(mode=0o700,exist_ok=True); os.chmod(target,0o700)
    args=[str(root/'.venv/bin/python'),str(root/'probe/probe.py'),'--state-dir',str(runtime/'state')]
    if fresh_production:
        args.append('--fresh-production')
    if connection_only:
        args.extend(['--connection-only','--connection-route-digest',connection_route_digest,
                     '--connection-subscription-id',connection_subscription_id,
                     '--connection-duration-seconds',str(connection_duration_seconds)])
    for host in callback_hosts: args.extend(['--allow-callback-host',host])
    profile=(root/'config/tunnel-profile.yaml.example').read_text().replace('__TUNNEL_ID__',tunnel_id).replace('__ROOT__',str(root)).replace('__MCP_COMMAND__',shlex.join(args))
    service=(root/'config/dots-wechat-bridge.service.example').read_text().replace('__ROOT__',str(root))
    outputs = [('profile.yaml',profile)]
    if not connection_only:
        outputs.append(('dots-wechat-bridge.service',service))
    for name,text in outputs:
        flags=os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW|(os.O_TRUNC if replace else os.O_EXCL)
        with os.fdopen(os.open(runtime/name,flags,0o600),'w') as output: output.write(text)
    return runtime


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--tunnel-id',required=True)
    destination=parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--callback-host',action='append')
    destination.add_argument('--discover-callback',action='store_true',help='Deny all callbacks while discovering the authorized subscription hostname')
    parser.add_argument('--replace',action='store_true',help='Update only rendered config; preserves key and database')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--fresh-production',action='store_true',help='Render explicit future-only entry; does not approve or activate it')
    mode.add_argument('--connection-only',action='store_true',help='Render finite original-subscription handshake profile only; no service template')
    parser.add_argument('--connection-route-digest')
    parser.add_argument('--connection-subscription-id')
    parser.add_argument('--connection-duration-seconds',type=int,default=180)
    args=parser.parse_args()
    try:
        runtime=render(args.root,args.tunnel_id,args.callback_host or [],args.replace,args.discover_callback,args.fresh_production,
                       args.connection_only,args.connection_route_digest,args.connection_subscription_id,args.connection_duration_seconds)
        print('Rendered private profile and service in '+str(runtime))
    except (ValueError,OSError) as exc:
        parser.exit(1,type(exc).__name__+': config not generated; check inputs/file ownership\n')

if __name__=='__main__': main()
