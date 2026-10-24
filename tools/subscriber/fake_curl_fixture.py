#!/usr/bin/env python3
"""Synthetic HTTP transport, injected only in TESTING native binaries."""
import json, os, sys, time
from pathlib import Path
args=sys.argv[1:]
def arg(key):return args[args.index(key)+1]
feed=Path(os.environ['MTGA_FIXTURE_FEED']); name=args[-1].rsplit('/',1)[-1]
body=(feed/name).read_bytes(); offset=int(arg('--range').rstrip('-')) if '--range' in args else 0
log=Path(os.environ['MTGA_FIXTURE_LOG'])
with log.open('a') as h:h.write(json.dumps({'name':name,'offset':offset,'pid':os.getpid()})+'\n')
out=Path(arg('--output')); mode=os.environ.get('MTGA_FIXTURE_MODE','normal')
if not name.endswith('.delta'):out.write_bytes(body);sys.exit(0)
status=206 if offset else 200; end=len(body)-1; total=len(body); suffix=body[offset:];code=0
if mode=='disconnect':suffix=suffix[:len(suffix)//2];code=18
if mode=='empty':sys.exit(7)
if mode=='range200' and offset:status=200;suffix=body
if mode=='range416' and offset:status=416;suffix=b'';code=22
if mode in ('wrongrange','wrongrange_cut') and offset:
    offset+=1
    if mode=='wrongrange_cut':suffix=suffix[:len(suffix)//2];code=18
if mode=='corrupt':suffix=b'X'*len(suffix)
headers=f'HTTP/2 {status}\r\nContent-Length: {total if status==200 else total-offset}\r\n'
if status==206:headers+=f'Content-Range: bytes {offset}-{end}/{total}\r\n'
Path(arg('--dump-header')).write_text(headers+'\r\n')
if mode=='wait':
    Path(os.environ['MTGA_FIXTURE_STARTED']).write_text(str(os.getpid()))
    while not Path(os.environ['MTGA_FIXTURE_RELEASE']).exists():time.sleep(.05)
out.write_bytes(suffix)
sys.exit(code)
