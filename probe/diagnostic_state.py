"""Read-only recognition of one closed diagnostic that admitted no source.

This is not permission, a writer fence, a receipt import, or a new execution.
No value from the old approval/isolation evidence is read or used.
"""
import math

EMPTY_STOPPED_TRIAL = 'weixin_new_message_check'
TRIAL_COLUMNS = (
    ('singleton','INTEGER',0,1), ('round_id','TEXT',1,0), ('phase','TEXT',1,0),
    ('started_at','REAL',1,0), ('expires_at','REAL',1,0), ('binding_hash','TEXT',1,0),
    ('approval_hash','TEXT',1,0), ('isolation_hash','TEXT',1,0),
    ('source_message_id','TEXT',0,0), ('send_id','TEXT',0,0),
    ('error_kind','TEXT',1,0), ('updated_at','REAL',1,0))


class EmptyDiagnosticError(ValueError):
    pass


def validate_empty_stopped_trial(db, now):
    columns = db.execute('PRAGMA table_xinfo(weixin_new_message_check)').fetchall()
    expected = [(index,name,kind,required,None,primary,0)
                for index,(name,kind,required,primary) in enumerate(TRIAL_COLUMNS)]
    if [tuple(row) for row in columns] != expected:
        raise EmptyDiagnosticError('schema_mismatch')
    rows = db.execute('''SELECT singleton,phase,started_at,expires_at,
        source_message_id,send_id,updated_at FROM weixin_new_message_check''').fetchall()
    if len(rows) != 1 or type(now) not in (int,float) or not math.isfinite(now):
        raise EmptyDiagnosticError('not_empty_stopped')
    row = rows[0]
    start, expiry, updated = row[2], row[3], row[6]
    if (row[0] != 1 or row[1] != 'stopped' or row[4] is not None or row[5] is not None
            or any(type(value) not in (int,float) or not math.isfinite(value)
                   for value in (start,expiry,updated))
            or not 0 < start < expiry <= now or not 0 < expiry-start <= 180
            or not start <= updated <= now):
        raise EmptyDiagnosticError('not_empty_stopped')
