"""JSON-RPC validation and response projection over the assembled bridge services.

This adapter owns wire error mapping and response limits. Business admission,
ownership checks and durable commits remain in the called stores.
"""
import time

try:
    from .rpc_protocol import VERSION, BUILD_VERSION, MAX_ATTACHMENT_RPC_BYTES, RpcError, canonical
    from .weixin import WeixinError, MESSAGE_EVENT
    from .aihot_email import EMAIL_EVENT, email_tools
    from .dotcutover import cutover_tools
    from .dotjournal import journal_tools
    from .dotledger import ledger_tools
    from .read_projection import request_id_digest, elapsed_ms, reply_result
except ImportError:
    from rpc_protocol import VERSION, BUILD_VERSION, MAX_ATTACHMENT_RPC_BYTES, RpcError, canonical
    from weixin import WeixinError, MESSAGE_EVENT
    from aihot_email import EMAIL_EVENT, email_tools
    from dotcutover import cutover_tools
    from dotjournal import journal_tools
    from dotledger import ledger_tools
    from read_projection import request_id_digest, elapsed_ms, reply_result


def attachment_response(request_id, result, limit):
    """Bound the complete envelope, including an arbitrarily large request ID.

    Both image and binary reads use this boundary so their large data never
    escape the same transport budget through a different response shape.
    """
    response = {"jsonrpc": "2.0", "id": request_id, "result": result}
    if len(canonical(response).encode('utf-8')) > limit:
        response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32004,
                    "message": "Weixin operation unavailable", "data": {"reason": "attachment_response_too_large"}}}
        if len(canonical(response).encode('utf-8')) > limit:
            response['id'] = None
    return response


def dispatch(probe, request, *, attachment_response_limit=MAX_ATTACHMENT_RPC_BYTES):
    request_id = request.get("id") if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise RpcError(-32600, "Invalid request")
        method, params = request["method"], request.get("params", {})
        if not isinstance(params, dict):
            raise RpcError(-32602, "Object parameters required")
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "server/discover":
            result = {"resultType": "complete", "supportedVersions": [VERSION],
                      "serverInfo": {"name": "dots-wechat-bridge", "version": BUILD_VERSION},
                      "capabilities": {"tools": {}, "events": {}}}
        elif method == "initialize":
            result = {"protocolVersion": VERSION, "serverInfo": {"name": "dots-wechat-bridge", "version": BUILD_VERSION},
                      "capabilities": {"tools": {}, "events": {}}}
        elif method == "ping":
            result = {}
        elif method == "events/list":
            result = {"events": [probe.event(), probe.event(MESSAGE_EVENT), probe.event(EMAIL_EVENT)]}
        elif method == "events/subscribe":
            result = probe.subscribe(params)
        elif method == "events/unsubscribe":
            result = probe.unsubscribe(params)
        elif method == "tools/list":
            result = {"tools": probe.tools()}
        elif method == "tools/call":
            args = params.get("arguments", {})
            if not isinstance(args, dict):
                raise RpcError(-32602, "Object arguments required")
            if params.get("name") == "acknowledge_test_ping":
                content = probe.acknowledge(args)
            elif params.get("name") == "get_probe_status" and args == {}:
                content = probe.status()
            elif params.get("name") == "get_weixin_status" and args == {}:
                content = probe.weixin.status()
            elif (params.get("name") == "read_weixin_message" and set(args) in ({"message_id"}, {"message_id", "projection"})
                    and isinstance(args["message_id"], str) and args.get('projection', 'full') in ('full', 'reply')):
                reply_projection = args.get('projection', 'full') == 'reply'
                if reply_projection:
                    read_started_at, read_started_mono = probe.clock(), time.monotonic()
                content = probe.weixin.read_message(args["message_id"])
                if reply_projection:
                    source_ready_mono = time.monotonic()
                    content['dot_sync_completeness'] = {'available': False,
                        'omitted': True, 'reason': 'projection_not_requested', 'all_parts_verified': False}
                else:
                    try:
                        content['dot_sync_completeness'] = probe.dot_completeness.for_message(args['message_id'])
                    except Exception:
                        # Inventory failure must not break the independent reply
                        # path or look like a verified/empty set of expected parts.
                        content['dot_sync_completeness'] = {'available': False,
                            'reason': 'metadata_unavailable', 'all_parts_verified': False}
                try:
                    probe.delivery_timing.agent_read(args['message_id'], probe.clock())
                    content['transport_timing'] = probe.delivery_timing.for_message(args['message_id'])
                except Exception:
                    # Observability never turns a successful owner read into
                    # a failed business operation or releases a write claim.
                    content['transport_timing'] = {'available': False, 'reason': 'metadata_unavailable'}
                if reply_projection:
                    payload_ready_mono = time.monotonic()
                    observation = dict(version='read-work-v2', projection='reply',
                        message_id=args['message_id'], request_id_sha256=request_id_digest(request_id),
                        read_work_started_at=read_started_at, read_work_payload_ready_at=probe.clock(),
                        source_lookup_elapsed_ms=elapsed_ms(read_started_mono, source_ready_mono),
                        timing_projection_elapsed_ms=elapsed_ms(source_ready_mono, payload_ready_mono),
                        read_work_elapsed_ms=elapsed_ms(read_started_mono, payload_ready_mono),
                        measurement_scope='exact_owner_read_and_timing_projection_before_response_envelope',
                        elapsed_clock='host_monotonic', timestamp_clock='host_wall_clock',
                        includes_result_projection=False, includes_response_serialization=False,
                        includes_stdout_write=False, includes_network_rtt=False,
                        platform_admission_confirmed=None, model_start_observed=False,
                        phone_delivery_observed=False)
                    result = reply_result(content, observation)
                    return {"jsonrpc": "2.0", "id": request_id, "result": result}
            elif params.get("name") == "read_attachment_chunk" and set(args) == {"message_id", "attachment_id", "offset", "length"} and all(isinstance(args[k], str) for k in ("message_id", "attachment_id")):
                content = probe.media.read_chunk(args["message_id"], args["attachment_id"], args["offset"], args["length"])
                safe = {key: value for key, value in content.items() if key != 'data_base64'}
                result = {"content": [{"type": "text", "text": canonical(safe)}], "structuredContent": content}
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            elif params.get("name") == "read_attachment_block" and set(args) == {"message_id", "attachment_id", "offset", "length"} and all(isinstance(args[k], str) for k in ("message_id", "attachment_id")):
                content = probe.media.read_block(args["message_id"], args["attachment_id"], args["offset"], args["length"])
                summary = {key: content[key] for key in ('transfer_version', 'offset', 'size', 'total_size', 'next_offset', 'eof', 'block_sha256', 'max_raw_bytes')}
                result = {"content": [{"type": "text", "text": canonical(summary)}], "structuredContent": content}
                return attachment_response(request_id, result, attachment_response_limit)
            elif (params.get("name") == "read_attachment_image"
                    and set(args) in ({"message_id", "attachment_id"}, {"message_id", "attachment_id", "page"})
                    and all(isinstance(args[k], str) for k in ("message_id", "attachment_id"))
                    and ('page' not in args or type(args['page']) is int and 0 <= args['page'] <= 63)):
                content, block = probe.media.read_image(args['message_id'], args['attachment_id'], page=args.get('page'))
                result = {"content": [{"type": "text", "text": canonical(content)}, block], "structuredContent": content}
                return attachment_response(request_id, result, attachment_response_limit)
            elif params.get("name") == "reply_weixin_message" and set(args) == {"message_id", "text"} and isinstance(args["message_id"], str):
                content = probe.weixin.queue_reply(args["message_id"], args["text"])
            elif params.get("name") == "reply_weixin_message_phase" and set(args) == {"message_id", "phase", "text"} and isinstance(args["message_id"], str):
                content = probe.weixin.queue_reply_phase(args["message_id"], args["phase"], args["text"])
            elif params.get("name") == "send_weixin_notification" and set(args) == {"notification_id", "text"}:
                content = probe.weixin.queue_notification(args["notification_id"], args["text"])
            elif params.get("name") == "get_weixin_send_status" and set(args) == {"send_id"} and isinstance(args["send_id"], str):
                content = probe.weixin.send_status(args["send_id"])
            elif params.get("name") == "arm_aihot_wechat_notifications" and set(args) == {"approval_message_id"}:
                content = probe.arm_aihot_notifications(args["approval_message_id"])
            elif params.get("name") == "off_aihot_wechat_notifications" and args == {}:
                content = probe.off_aihot_notifications()
            elif params.get('name') in {tool['name'] for tool in email_tools()}:
                if probe.aihot_email is None:
                    raise RpcError(-32005, "AIHOT email unavailable")
                content = probe.aihot_email.call(params['name'], args)
            elif params.get('name') == 'begin_weixin_upload' and set(args) == {'upload_id','filename','kind','size','sha256'}:
                content = probe.outbound.begin(**args)
            elif params.get('name') == 'upload_weixin_chunk' and set(args) == {'upload_id','offset','data_base64','chunk_sha256'}:
                content = probe.outbound.chunk(**args)
            elif params.get('name') == 'finalize_weixin_upload' and set(args) == {'upload_id'}:
                content = probe.outbound.finalize(**args)
            elif params.get('name') == 'get_weixin_upload_status' and set(args) == {'upload_id'}:
                content = probe.outbound.status(args['upload_id'])
            elif params.get('name') == 'send_weixin_media' and set(args) == {'send_id','upload_id','message_id'} and isinstance(args['message_id'],str):
                content = probe.outbound.queue(**args)
            elif params.get('name') == 'get_weixin_dot_completeness' and set(args) == {'message_id'} and isinstance(args['message_id'], str) and 0 < len(args['message_id']) <= 256:
                content = probe.dot_completeness.for_message(args['message_id'])
            elif params.get('name') in {tool['name'] for tool in cutover_tools()}:
                content = probe.dot_journal.cutover.call(params['name'], args)
            elif params.get('name') in {tool['name'] for tool in journal_tools()}:
                content = probe.dot_journal.call(params['name'], args)
            elif params.get('name') in {tool['name'] for tool in ledger_tools()}:
                content = probe.dot_mirrors.call(params['name'], args)
            else:
                raise RpcError(-32602, "Unknown tool or invalid arguments")
            result = {"content": [{"type": "text", "text": canonical(content)}], "structuredContent": content}
        else:
            raise RpcError(-32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result} if "id" in request else None
    except WeixinError as error:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32004, "message": "Weixin operation unavailable", "data": {"reason": error.kind}}}
    except RpcError as error:
        result = {"code": error.code, "message": str(error)}
        if error.reason:
            result["data"] = {"reason": error.reason}
        return {"jsonrpc": "2.0", "id": request_id, "error": result}
    except Exception:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal error"}}

