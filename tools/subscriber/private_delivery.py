"""Private data-only delivery. Credentials stay inside the user's GitHub CLI."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
import zipfile

REPOSITORY = '1996Neil/mtga-resource-acquisition-probe'
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_JSON = 3 * 1024 * 1024
MAX_UNPACKED = 768 * 1024 * 1024
RANGE_CHUNK = 4 * 1024 * 1024
HEX = re.compile(r'^[0-9a-f]{64}$')

class DeliveryError(RuntimeError):
    pass

def require(condition, message):
    if not condition:
        raise DeliveryError(message)

def fingerprint(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and path.is_file() and
            not path.is_symlink(), 'UNSAFE_LOCAL_FILE')
    before = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    after = path.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), 'LOCAL_FILE_CHANGED')
    return {'bytes': after.st_size, 'sha256': h.hexdigest()}

def valid_fingerprint(value, maximum=MAX_ARCHIVE):
    require(isinstance(value, dict) and set(value) == {'bytes','sha256'}, 'FINGERPRINT_FIELDS')
    require(type(value['bytes']) is int and 0 < value['bytes'] <= maximum and
            isinstance(value['sha256'], str) and HEX.fullmatch(value['sha256']),
            'FINGERPRINT_VALUE')
    return value

def regular_parents(path):
    require(path.is_absolute() and path.resolve() == path, 'SYMLINK_PARENT')
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'SYMLINK_PARENT')

def sync_directory(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def safe_member(name):
    require(isinstance(name, str) and '\\' not in name and '\0' not in name, 'ZIP_PATH')
    p = PurePosixPath(name)
    require(not p.is_absolute() and str(p) == name and
            len(p.parts) in (1,2) and all(part not in ('.','..') for part in p.parts),
            'ZIP_PATH')
    if len(p.parts) == 2:
        require(p.parts[0] == 'feed', 'ZIP_DIRECTORY_SCOPE')
    base = p.name
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,170}', base)), 'ZIP_FILENAME')
    allowed = base in {'profile.json','index-testing.json','delivery.json',
                       'report.json','package-manifest.json',
                       'card.delta','client.delta','resources.delta',
                       'bootstrap_fonts.delta','font_bundle.delta'}
    allowed |= bool(re.fullmatch(r'2026\.[0-9.]+-[0-9]+-r[0-9]+(?:-[a-z0-9-]+)?\.json', base))
    allowed |= bool(re.fullmatch(r'(?:card|client|resources|bootstrap_fonts|font_bundle)-[A-Za-z0-9._-]+\.delta', base))
    require(allowed, 'ZIP_DATA_ONLY')
    return p

def extract_data_archive(archive, destination, expected_files):
    """Inspect every member first, then extract only an exact verified inventory."""
    archive, destination = Path(archive), Path(destination)
    require(not destination.exists() and not destination.is_symlink(), 'FRESH_UNPACK_DIRECTORY')
    regular_parents(destination.parent)
    require(isinstance(expected_files, dict) and 1 <= len(expected_files) <= 20,
            'ZIP_INVENTORY_BOUND')
    for name, expected in expected_files.items():
        safe_member(name)
        valid_fingerprint(expected, MAX_UNPACKED)
    created = False
    try:
        with zipfile.ZipFile(archive) as source:
            items = source.infolist()
            require(len(items) == len(expected_files) and
                    len({i.filename for i in items}) == len(items), 'ZIP_DUPLICATES_OR_COUNT')
            require(set(i.filename for i in items) == set(expected_files), 'ZIP_EXACT_INVENTORY')
            require(sum(i.file_size for i in items) <= MAX_UNPACKED, 'ZIP_TOTAL_BOUND')
            for item in items:
                safe_member(item.filename)
                mode = item.external_attr >> 16
                require(not item.is_dir() and not item.flag_bits & 1 and
                        stat.S_IFMT(mode) in (0, stat.S_IFREG) and not mode & 0o111,
                        'ZIP_REGULAR_NONEXECUTABLE_ONLY')
                expected = expected_files[item.filename]
                require(item.file_size == expected['bytes'], 'ZIP_MEMBER_SIZE')
                if item.filename.endswith('.json'):
                    require(item.file_size <= MAX_JSON, 'ZIP_JSON_BOUND')
                require(item.file_size <= max(item.compress_size,1) * 500, 'ZIP_COMPRESSION_BOUND')
            destination.mkdir(mode=0o700)
            created = True
            for item in items:
                target = destination / item.filename
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                h = hashlib.sha256()
                size = 0
                with source.open(item) as src, target.open('xb') as dst:
                    os.chmod(target, 0o600)
                    for chunk in iter(lambda: src.read(1024 * 1024), b''):
                        size += len(chunk)
                        require(size <= expected_files[item.filename]['bytes'], 'ZIP_STREAM_BOUND')
                        h.update(chunk)
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                require({'bytes': size, 'sha256': h.hexdigest()} ==
                        expected_files[item.filename], 'ZIP_MEMBER_FINGERPRINT')
            return {name: fingerprint(destination/name) for name in expected_files}
    except BaseException:
        if created:
            shutil.rmtree(destination)
        raise

def _child_limits(maximum):
    resource.setrlimit(resource.RLIMIT_CORE, (0,0))
    _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    limit = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

class PrivateGitHub:
    """GET-only, one repository, pinned existing gh executable; never reads a token."""
    def __init__(self, gh_path, gh_fingerprint):
        self.gh = Path(gh_path)
        self.expected_gh = valid_fingerprint(gh_fingerprint, 128*1024*1024)
        require(fingerprint(self.gh) == self.expected_gh, 'GITHUB_CLI_IDENTITY')
        st = self.gh.stat()
        require(st.st_uid == os.getuid() and not st.st_mode & 0o022 and
                st.st_mode & stat.S_IXUSR, 'GITHUB_CLI_PERMISSIONS')

    def endpoint(self, tail):
        require(isinstance(tail,str) and not tail.startswith('/') and
                '#' not in tail and '\\' not in tail, 'GITHUB_ENDPOINT')
        allowed = tail == ''
        allowed |= bool(re.fullmatch(r'actions/runs/[1-9][0-9]*', tail))
        allowed |= bool(re.fullmatch(r'actions/artifacts/[1-9][0-9]*(?:/zip)?', tail))
        allowed |= bool(re.fullmatch(r'git/(?:commits/[0-9a-f]{40}|trees/[0-9a-f]{40}\?recursive=1)', tail))
        allowed |= bool(re.fullmatch(r'contents/delivery/latest\.json\?ref=[A-Za-z0-9%._-]+', tail))
        allowed |= bool(re.fullmatch(r'contents/cloud-maintenance/results/unattended-[0-9]+-[0-9]+\.json\?ref=[0-9a-f]{40}', tail))
        require(allowed and '..' not in tail, 'GITHUB_ENDPOINT_SCOPE')
        return 'repos/' + REPOSITORY + ('/' + tail if tail else '')

    def _run_to(self, endpoint, output, maximum, timeout):
        require(fingerprint(self.gh) == self.expected_gh, 'GITHUB_CLI_CHANGED')
        env = dict(os.environ)
        for key in ('GH_TOKEN','GITHUB_TOKEN','GH_ENTERPRISE_TOKEN',
                    'GITHUB_ENTERPRISE_TOKEN','GH_DEBUG'):
            env.pop(key, None)
        env.update(GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1',
                   GH_PAGER='/bin/cat', GH_HOST='github.com')
        try:
            result = subprocess.run(
                [str(self.gh),'api','--hostname','github.com','--method','GET',endpoint],
                stdout=output, stderr=subprocess.PIPE, env=env, timeout=timeout,
                preexec_fn=lambda: _child_limits(maximum))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeliveryError('PRIVATE_GITHUB_TRANSPORT_' + type(error).__name__) from None
        require(result.returncode == 0, 'PRIVATE_GITHUB_REQUEST_FAILED')
        require(fingerprint(self.gh) == self.expected_gh, 'GITHUB_CLI_CHANGED_DURING_REQUEST')

    def _range_archive(self, artifact_id, destination, expected):
        """Bounded parallel GETs; completed chunks are untrusted until full SHA passes.

        Popen/preexec runs only on this main thread, never a thread executor.
        No redirect URL, Authorization value or credential is exported.
        """
        parts=destination.with_name(destination.name+'.parts')
        regular_parents(parts)
        parts.mkdir(mode=0o700,exist_ok=True)
        meta={'artifact_id':artifact_id,'expected':expected,'chunk_bytes':RANGE_CHUNK}
        meta_path=parts/'metadata.json'
        if meta_path.exists():
            require(meta_path.is_file() and not meta_path.is_symlink() and meta_path.stat().st_size<4096 and
                    json.loads(meta_path.read_text())==meta,'RANGE_CACHE_IDENTITY')
        else:
            require(not any(parts.iterdir()),'RANGE_CACHE_UNKNOWN_FILES')
            with meta_path.open('x') as f:
                os.chmod(meta_path,0o600);json.dump(meta,f);f.flush();os.fsync(f.fileno())
            sync_directory(parts)
        count=(expected['bytes']+RANGE_CHUNK-1)//RANGE_CHUNK
        names={'metadata.json'}|{str(i)+suffix for i in range(count) for suffix in ('.part','.download')}
        require(all(p.name in names and p.is_file() and not p.is_symlink() for p in parts.iterdir()),
                'RANGE_CACHE_FILES')
        for p in parts.glob('*.download'):p.unlink()
        pending=[];active=[];attempts={};completed_bytes=0
        for i in range(count):
            size=min(RANGE_CHUNK,expected['bytes']-i*RANGE_CHUNK);p=parts/(str(i)+'.part')
            if p.exists():
                require(p.stat().st_size==size,'RANGE_CACHE_CHUNK_SIZE');completed_bytes+=size
            else:pending.append(i)
        print('完整数据包分段接收：已有 '+str(completed_bytes//1048576)+' MiB；最终仍须整包摘要通过。',flush=True)
        env=dict(os.environ)
        for key in ('GH_TOKEN','GITHUB_TOKEN','GH_ENTERPRISE_TOKEN','GITHUB_ENTERPRISE_TOKEN','GH_DEBUG'):env.pop(key,None)
        env.update(GH_PROMPT_DISABLED='1',GH_NO_UPDATE_NOTIFIER='1',GH_PAGER='/bin/cat',GH_HOST='github.com')
        try:
            while pending or active:
                while pending and len(active)<4:
                    i=pending.pop(0);start=i*RANGE_CHUNK;end=min(start+RANGE_CHUNK,expected['bytes'])-1
                    require(fingerprint(self.gh)==self.expected_gh,'GITHUB_CLI_CHANGED')
                    path=parts/(str(i)+'.download');out=path.open('xb');os.chmod(path,0o600)
                    try:
                        process=subprocess.Popen([str(self.gh),'api','--hostname','github.com','--method','GET',
                            '--header',f'Range: bytes={start}-{end}',self.endpoint('actions/artifacts/'+str(artifact_id)+'/zip')],
                            stdout=out,stderr=subprocess.DEVNULL,env=env,
                            preexec_fn=lambda bound=end-start+2:_child_limits(bound))
                    except BaseException:out.close();path.unlink();raise
                    active.append((i,process,out,path,time.monotonic(),end-start+1))
                changed=False
                for item in list(active):
                    i,p,out,path,started,size=item
                    if p.poll() is None and time.monotonic()-started<300:continue
                    changed=True
                    if p.poll() is None:p.kill()
                    p.wait();out.flush();os.fsync(out.fileno());out.close()
                    require(fingerprint(self.gh)==self.expected_gh,'GITHUB_CLI_CHANGED_DURING_REQUEST')
                    if p.returncode==0 and path.stat().st_size==size:
                        path.rename(parts/(str(i)+'.part'));sync_directory(parts);completed_bytes+=size
                        print('分段接收 '+str(completed_bytes//1048576)+' / '+str((expected['bytes']+1048575)//1048576)+' MiB',flush=True)
                    else:
                        path.unlink();attempts[i]=attempts.get(i,0)+1
                        require(attempts[i]<3,'PRIVATE_GITHUB_RANGE_FAILED_RETRY_PRESERVES_COMPLETED_CHUNKS')
                        pending.append(i)
                    active.remove(item)
                if not changed:time.sleep(0.1)
            with destination.open('xb') as out:
                os.chmod(destination,0o600)
                for i in range(count):
                    with (parts/(str(i)+'.part')).open('rb') as src:shutil.copyfileobj(src,out,1048576)
                out.flush();os.fsync(out.fileno())
            sync_directory(destination.parent)
            if fingerprint(destination)!=expected:
                destination.unlink();shutil.rmtree(parts)
                raise DeliveryError('ARTIFACT_ARCHIVE_FINGERPRINT')
            shutil.rmtree(parts)
        finally:
            for _,p,out,path,_,_ in active:
                if p.poll() is None:p.kill()
                p.wait();out.close()
                if path.exists():path.unlink()

    def json(self, tail):
        endpoint = self.endpoint(tail)
        require(not tail.endswith('/zip'), 'BINARY_ENDPOINT_REQUIRES_FINGERPRINT')
        with tempfile.TemporaryFile() as output:
            self._run_to(endpoint, output, MAX_JSON, 90)
            require(output.tell() <= MAX_JSON, 'GITHUB_JSON_BOUND')
            output.seek(0)
            try:
                result = json.loads(output.read().decode('utf-8'),
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                raise DeliveryError('PRIVATE_GITHUB_JSON_INVALID') from None
        require(isinstance(result,dict), 'GITHUB_JSON_OBJECT_REQUIRED')
        return result

    def contents(self, path, ref):
        report = bool(re.fullmatch(r'cloud-maintenance/results/unattended-[0-9]+-[0-9]+\.json',path))
        require(path == 'delivery/latest.json' or
                (report and bool(re.fullmatch(r'[0-9a-f]{40}',ref))), 'CONTENTS_PATH')
        require(re.fullmatch(r'[A-Za-z0-9._/-]+',ref) and '..' not in ref, 'CONTENTS_REF')
        return self.json('contents/'+path+'?ref='+urllib.parse.quote(ref,safe=''))

    def repository(self):
        data = self.json('')
        require(data.get('full_name') == REPOSITORY and data.get('private') is True,
                'PRIVATE_REPOSITORY_REQUIRED')
        return data

    def archive(self, artifact_id, destination, expected):
        require(type(artifact_id) is int and artifact_id > 0, 'ARTIFACT_ID')
        valid_fingerprint(expected)
        destination = Path(destination)
        regular_parents(destination.parent)
        require(not destination.exists() and not destination.is_symlink(), 'FRESH_ARCHIVE_PATH')
        try:
            if expected['bytes']>=2*RANGE_CHUNK:
                self._range_archive(artifact_id,destination,expected)
                return
            with destination.open('xb') as output:
                os.chmod(destination, 0o600)
                self._run_to(self.endpoint('actions/artifacts/'+str(artifact_id)+'/zip'),
                             output, expected['bytes']+1, 1200)
                output.flush()
                os.fsync(output.fileno())
            require(fingerprint(destination) == expected, 'ARTIFACT_ARCHIVE_FINGERPRINT')
        except BaseException:
            if destination.exists():
                destination.unlink()
            raise
