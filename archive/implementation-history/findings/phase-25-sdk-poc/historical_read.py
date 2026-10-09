"""Read committed historical fixtures; never connect to the live scheduler."""
import hashlib
import json
import sqlite3
from pathlib import Path
from ai_dev_loop.scheduler.domain.state import parse_scheduler_state
from ai_dev_loop.scheduler.domain.events import parse_scheduler_event

ROOT=Path(__file__).parent
REPO=Path('/home/rojobad/Projects/ai_dev_loop')
FIXTURES=REPO/'tests/fixtures'
results=[]
p=FIXTURES/'phase22_historical/manual_wait_state_pre_phase22_v1.json'
before=p.read_bytes()
state=parse_scheduler_state(json.loads(before))
results.append({'fixture':str(p),'state_kind':state.kind,'schema_version':state.schema_version,'unchanged_bytes':before==p.read_bytes(),'cli_used':False})
p=FIXTURES/'phase23_1_historical/dual_failure_engine.sqlite3'
before=hashlib.sha256(p.read_bytes()).hexdigest()
with sqlite3.connect(p.as_uri()+'?mode=ro&immutable=1',uri=True) as conn:
    conn.row_factory=sqlite3.Row
    states=[]
    for row in conn.execute('SELECT * FROM scheduler_runs'):
        payload=row['state_payload']
        assert hashlib.sha256(payload.encode()).hexdigest()==row['state_payload_sha256']
        state=parse_scheduler_state(json.loads(payload));states.append(state.kind)
    events=0
    for row in conn.execute('SELECT * FROM scheduler_events ORDER BY sequence'):
        payload=row['event_payload']
        assert hashlib.sha256(payload.encode()).hexdigest()==row['event_payload_sha256']
        parse_scheduler_event(json.loads(payload));events+=1
results.append({'fixture':str(p),'state_kinds':states,'validated_event_count':events,'payload_hashes_valid':True,'unchanged_bytes':before==hashlib.sha256(p.read_bytes()).hexdigest(),'readonly_immutable_sqlite':True,'cli_used':False})
(ROOT/'historical-read.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps(results))
