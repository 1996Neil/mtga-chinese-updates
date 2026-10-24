import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import private_delivery as d

def data_fp(data):
    return {'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}

class PrivateDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.gh = self.root/'gh'
        self.gh.write_bytes(b'SYNTHETIC_EXECUTABLE_NOT_RUN')
        self.gh.chmod(0o700)
        self.client = d.PrivateGitHub(self.gh, d.fingerprint(self.gh))
    def tearDown(self):
        self.temp.cleanup()
    def archive(self, entries):
        target = self.root/'input.zip'
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
            for name,data,mode in entries:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                z.writestr(info,data)
        return target
    def test_fingerprint_types_and_bounds(self):
        for value in ({'bytes':True,'sha256':'0'*64},
                      {'bytes':-1,'sha256':'0'*64},
                      {'bytes':1,'sha256':'G'*64},
                      {'bytes':1,'sha256':'0'*64,'path':'unexpected'}):
            with self.subTest(value=value), self.assertRaises(d.DeliveryError):
                d.valid_fingerprint(value)
    def test_valid_exact_data_archive(self):
        body=b'{"schema":"SYNTHETIC"}'
        source=self.archive([('feed/profile.json',body,stat.S_IFREG|0o600),
                             ('feed/client-synthetic.delta',b'PATCH',stat.S_IFREG|0o644)])
        expected={'feed/profile.json':data_fp(body),'feed/client-synthetic.delta':data_fp(b'PATCH')}
        result=d.extract_data_archive(source,self.root/'out',expected)
        self.assertEqual(result,expected)
        self.assertEqual((self.root/'out/feed/profile.json').stat().st_mode&0o777,0o600)
    def test_unsafe_or_executable_archive_is_rejected_before_creation(self):
        for name,mode in [('../profile.json',stat.S_IFREG|0o600),
                          ('/profile.json',stat.S_IFREG|0o600),
                          ('feed/../profile.json',stat.S_IFREG|0o600),
                          ('profile.json',stat.S_IFLNK|0o777),
                          ('profile.json',stat.S_IFREG|0o755),
                          ('runner.py',stat.S_IFREG|0o600),
                          ('feed/nested/profile.json',stat.S_IFREG|0o600)]:
            with self.subTest(name=name,mode=mode):
                source=self.archive([(name,b'X',mode)])
                with self.assertRaises(d.DeliveryError):
                    d.extract_data_archive(source,self.root/'out',{name:data_fp(b'X')})
                self.assertFalse((self.root/'out').exists())
    def test_duplicate_extra_or_mismatched_payload_refused(self):
        expected={'profile.json':data_fp(b'RIGHT')}
        cases=[[('profile.json',b'RIGHT',0o100600),('profile.json',b'RIGHT',0o100600)],
               [('profile.json',b'WRONG',0o100600)],
               [('profile.json',b'RIGHT',0o100600),('report.json',b'EXTRA',0o100600)]]
        for entries in cases:
            with self.subTest(entries=entries):
                source=self.archive(entries)
                with self.assertRaises(d.DeliveryError):
                    d.extract_data_archive(source,self.root/'out',expected)
                self.assertFalse((self.root/'out').exists())
    def test_existing_output_and_symlink_parent_are_preserved(self):
        out=self.root/'out';out.mkdir();(out/'keep').write_bytes(b'KEEP')
        source=self.archive([('profile.json',b'X',0o100600)])
        with self.assertRaises(d.DeliveryError):
            d.extract_data_archive(source,out,{'profile.json':data_fp(b'X')})
        self.assertEqual((out/'keep').read_bytes(),b'KEEP')
        link=self.root/'alias';link.symlink_to(out,target_is_directory=True)
        with self.assertRaises(d.DeliveryError):
            d.extract_data_archive(source,link/'child',{'profile.json':data_fp(b'X')})
    def test_fixed_repository_read_only_endpoint_scope(self):
        for tail in ('../other','/user','actions/workflows/x/dispatches',
                     'contents/auth/token-capsule.json?ref=main',
                     'actions/runs/1/zip','contents/delivery/latest.json?ref=main&x=1'):
            with self.subTest(tail=tail),self.assertRaises(d.DeliveryError):
                self.client.endpoint(tail)
        self.assertEqual(self.client.endpoint('actions/artifacts/12'),
                         'repos/'+d.REPOSITORY+'/actions/artifacts/12')
    def test_saved_credential_transport_never_exports_token(self):
        def run(args,**kw):
            self.assertEqual(args[1:6],['api','--hostname','github.com','--method','GET'])
            self.assertNotIn('GH_TOKEN',kw['env'])
            self.assertNotIn('GITHUB_TOKEN',kw['env'])
            self.assertNotIn('GH_DEBUG',kw['env'])
            self.assertEqual(kw['env']['GH_PROMPT_DISABLED'],'1')
            kw['stdout'].write(json.dumps({'full_name':d.REPOSITORY,'private':True}).encode())
            return subprocess.CompletedProcess(args,0,stderr=b'')
        with patch.dict(os.environ,{'GH_TOKEN':'SYNTHETIC_NOT_A_SECRET',
                                    'GITHUB_TOKEN':'SYNTHETIC_NOT_A_SECRET',
                                    'GH_DEBUG':'api'}),patch.object(d.subprocess,'run',side_effect=run):
            self.assertTrue(self.client.repository()['private'])
    def test_public_or_other_repository_is_rejected(self):
        for value in ({'full_name':d.REPOSITORY,'private':False},
                      {'full_name':'someone/other','private':True}):
            with patch.object(self.client,'json',return_value=value),self.assertRaises(d.DeliveryError):
                self.client.repository()
    def test_cli_drift_symlink_or_unsafe_permissions_rejected(self):
        expected=d.fingerprint(self.gh)
        self.gh.write_bytes(b'CHANGED')
        with self.assertRaises(d.DeliveryError):
            self.client.json('')
        alias=self.root/'alias';alias.symlink_to(self.gh)
        with self.assertRaises(d.DeliveryError):
            d.PrivateGitHub(alias,d.fingerprint(self.gh))
        self.gh.chmod(0o777)
        with self.assertRaises(d.DeliveryError):
            d.PrivateGitHub(self.gh,d.fingerprint(self.gh))
    def test_archive_hash_failure_removes_only_own_download(self):
        def run(args,**kw):
            kw['stdout'].write(b'WRONG')
            return subprocess.CompletedProcess(args,0,stderr=b'')
        target=self.root/'archive.zip'
        with patch.object(d.subprocess,'run',side_effect=run),self.assertRaises(d.DeliveryError):
            self.client.archive(1,target,data_fp(b'RIGHT'))
        self.assertFalse(target.exists())
    def test_failure_and_timeout_do_not_echo_provider_output(self):
        with patch.object(d.subprocess,'run',return_value=subprocess.CompletedProcess([],1,stderr=b'SYNTHETIC_PRIVATE_OUTPUT')):
            with self.assertRaises(d.DeliveryError) as error:self.client.json('')
            self.assertNotIn('SYNTHETIC',str(error.exception))
        with patch.object(d.subprocess,'run',side_effect=subprocess.TimeoutExpired(['gh'],1)):
            with self.assertRaisesRegex(d.DeliveryError,'TRANSPORT_TimeoutExpired'):self.client.json('')

    def range_client(self,mode='normal'):
        import sys
        source=self.root/'range-source';source.write_bytes(os.urandom(10240))
        audit=self.root/'ranges.txt';marker=self.root/'failed-once'
        self.gh.write_text('#!'+sys.executable+'\n'+
            'import sys,os\nfrom pathlib import Path\n'+
            'assert not any(k in os.environ for k in ("GH_TOKEN","GITHUB_TOKEN","GH_DEBUG"))\n'+
            'assert sys.argv[1:6]==["api","--hostname","github.com","--method","GET"]\n'+
            'assert sys.argv[-1]=='+repr('repos/'+d.REPOSITORY+'/actions/artifacts/1/zip')+'\n'+
            'a,b=map(int,sys.argv[7].split("=")[1].split("-"))\n'+
            'fd=os.open('+repr(str(audit))+',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);os.write(fd,(str(a)+"\\n").encode());os.close(fd)\n'+
            'marker=Path('+repr(str(marker))+')\n'+
            ('if a==1024 and not marker.exists():marker.write_text("once");sys.exit(1)\n' if mode=='fail-once' else '')+
            (('import time\nPath('+repr(str(self.root))+'+"/child-"+str(a)+".pid").write_text(str(os.getpid()))\nwhile True: time.sleep(10)\n') if mode=='hang' else '')+
            'data=Path('+repr(str(source))+').read_bytes()\n'+
            ('sys.stdout.buffer.write(data)\n' if mode=='ignore' else 'sys.stdout.buffer.write(data[a:b+1])\n'))
        self.gh.chmod(0o700)
        return d.PrivateGitHub(self.gh,d.fingerprint(self.gh)),source,audit

    def seed_range(self,target,source,prefix=b''):
        parts=target.with_name(target.name+'.parts');parts.mkdir()
        (parts/'metadata.json').write_text(json.dumps({'artifact_id':1,'expected':d.fingerprint(source),'chunk_bytes':1024}))
        if prefix:(parts/'0.part').write_bytes(prefix)
        return parts

    def test_parallel_range_resume_retries_without_credentials_and_full_sha(self):
        client,source,audit=self.range_client('fail-once');target=self.root/'received.zip'
        parts=self.seed_range(target,source,source.read_bytes()[:1024])
        with patch.object(d,'RANGE_CHUNK',1024),patch.dict(os.environ,{'GH_TOKEN':'SYNTHETIC','GITHUB_TOKEN':'SYNTHETIC','GH_DEBUG':'api'}):
            client.archive(1,target,d.fingerprint(source))
        self.assertEqual(target.read_bytes(),source.read_bytes());self.assertFalse(parts.exists())
        ranges=audit.read_text().splitlines();self.assertNotIn('0',ranges);self.assertEqual(ranges.count('1024'),2)

    def test_range_ignored_or_wrong_size_cannot_be_accepted(self):
        client,source,audit=self.range_client('ignore');target=self.root/'received.zip'
        with patch.object(d,'RANGE_CHUNK',1024),self.assertRaisesRegex(d.DeliveryError,'RANGE_FAILED'):
            client.archive(1,target,d.fingerprint(source))
        self.assertFalse(target.exists())
        self.assertFalse(list(target.with_name(target.name+'.parts').glob('*.download')))

    def test_untrusted_cached_chunk_requires_final_full_archive_hash(self):
        client,source,audit=self.range_client();target=self.root/'received.zip'
        parts=self.seed_range(target,source,b'X'*1024)
        with patch.object(d,'RANGE_CHUNK',1024),self.assertRaisesRegex(d.DeliveryError,'ARCHIVE_FINGERPRINT'):
            client.archive(1,target,d.fingerprint(source))
        self.assertFalse(target.exists());self.assertFalse(parts.exists())

    def test_range_cache_identity_or_unknown_file_blocks_before_request(self):
        client,source,audit=self.range_client();target=self.root/'received.zip'
        parts=self.seed_range(target,source);(parts/'unexpected').write_bytes(b'X')
        with patch.object(d,'RANGE_CHUNK',1024),self.assertRaisesRegex(d.DeliveryError,'RANGE_CACHE_FILES'):
            client.archive(1,target,d.fingerprint(source))
        self.assertFalse(audit.exists());(parts/'unexpected').unlink()
        (parts/'metadata.json').write_text('{}')
        with patch.object(d,'RANGE_CHUNK',1024),self.assertRaisesRegex(d.DeliveryError,'RANGE_CACHE_IDENTITY'):
            client.archive(1,target,d.fingerprint(source))
        self.assertFalse(audit.exists())
        (parts/'metadata.json').unlink();(parts/'metadata.json').mkdir()
        with patch.object(d,'RANGE_CHUNK',1024),self.assertRaisesRegex(d.DeliveryError,'RANGE_CACHE_IDENTITY'):
            client.archive(1,target,d.fingerprint(source))
        self.assertFalse(audit.exists())

    def test_interruption_kills_active_children_and_preserves_completed_resume(self):
        import time
        client,source,audit=self.range_client('hang');target=self.root/'received.zip'
        parts=self.seed_range(target,source,source.read_bytes()[:1024]);real_sleep=time.sleep
        def interrupt(_):
            deadline=time.monotonic()+10
            while len(list(self.root.glob('child-*.pid')))<4 and time.monotonic()<deadline:real_sleep(0.05)
            self.assertEqual(len(list(self.root.glob('child-*.pid'))),4)
            raise KeyboardInterrupt
        with patch.object(d,'RANGE_CHUNK',1024),patch.object(d.time,'sleep',side_effect=interrupt),self.assertRaises(KeyboardInterrupt):
            client.archive(1,target,d.fingerprint(source))
        for path in self.root.glob('child-*.pid'):
            with self.assertRaises(ProcessLookupError):os.kill(int(path.read_text()),0)
        self.assertEqual((parts/'0.part').read_bytes(),source.read_bytes()[:1024])
        self.assertFalse(target.exists());self.assertFalse(list(parts.glob('*.download')))
        self.gh.write_text(self.gh.read_text().replace('while True: time.sleep(10)\n',''))
        client=d.PrivateGitHub(self.gh,d.fingerprint(self.gh))
        with patch.object(d,'RANGE_CHUNK',1024):client.archive(1,target,d.fingerprint(source))
        self.assertEqual(target.read_bytes(),source.read_bytes());self.assertFalse(parts.exists())

if __name__=='__main__':
    unittest.main()
