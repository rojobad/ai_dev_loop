"""Final inspection of only this POC's files and processes. No secret output."""
import json
import os
from pathlib import Path
ROOT=Path(__file__).parent
key=os.environ.pop('CURSOR_API_KEY','').encode()
assert key
checked=0;matches=[]
for path in ROOT.rglob('*'):
    if path.is_file() and not any(p in ('venv','cache') for p in path.relative_to(ROOT).parts):
        checked+=1
        if key in path.read_bytes():matches.append(str(path.relative_to(ROOT)))
processes=[]
for path in Path('/proc').iterdir():
    if not path.name.isdigit():continue
    try:
        args=path.joinpath('cmdline').read_bytes().split(b'\0')
        cwd=path.joinpath('cwd').resolve()
        owned_runtime=bool(args and args[0].startswith(str(ROOT/'venv').encode()) and any(b'cursor-sdk-bridge' in arg for arg in args))
        owned_fixture=cwd==ROOT/'workspace' and any(arg.endswith((b'slow.py',b'crash.py',b'runtimecrash.py',b'mcp_stub.py')) for arg in args)
        owned_mcp=any(arg==str(ROOT/'mcp_stub.py').encode() for arg in args)
        if owned_runtime or owned_fixture or owned_mcp:
            processes.append({'pid':int(path.name),'executable':Path(args[0].decode()).name,'poc_owned':True})
    except (FileNotFoundError,PermissionError,ProcessLookupError):pass
result={'scanned_files_excluding_venv_and_cache':checked,'credential_found_in_files':bool(matches),'matching_file_names':matches,'remaining_poc_processes':processes,'key_removed_from_audit_environment':True}
(ROOT/'final-audit.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
assert not matches
assert not processes
