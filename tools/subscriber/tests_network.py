import base64
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
import package_installer
from unittest.mock import patch
import github_publish as gp
from publisher import canonical, fingerprint, package as build_package
from tests_subscriber import SubscriberTests, BINARY


class URLAndProtocolTests(SubscriberTests):
    # Inherited integration suite intentionally excluded by loader below.
    def test_cross_repo_and_ambiguous_urls_rejected(self):
        # Valid raw feed identity, but no network call needed: exercise releaseURL via utility.
        for url in ['https://github.com/other/repo/releases/download/v1/card.delta',
                    'https://github.com/owner/repo/releases/download/v1/card.delta?token=x',
                    'http://github.com/owner/repo/releases/download/v1/card.delta',
                    'https://github.com/owner/repo/releases/download/v1/%63ard.delta',
                    'https://github.com/owner/repo/releases/download/v1/a.delta',
                    'https://github.com:443/owner/repo/releases/download/v1/card.delta',
                    'https://user@github.com/owner/repo/releases/download/v1/card.delta']:
            result=self.invoke('validate-release-url','--input',url,'--profile','https://raw.githubusercontent.com/owner/repo/main/feed','--output','card.delta',base=False,ok=False)
            self.assertNotEqual(result.returncode,0,url)
        self.invoke('validate-release-url','--input','https://github.com/owner/repo/releases/download/v1/card.delta','--profile','https://raw.githubusercontent.com/owner/repo/main/feed','--output','card.delta',base=False)

    def test_new_protocol_requires_supported_client(self):
        self.release['minimum_client']=999;self.resign();self.refused(contains='需要升级维护工具')

    def test_setup_once_then_update_twice_same_installed_client(self):
        self.invoke('setup')
        installed=self.state/'mtga-cn'; profile=self.state/'profile.json'
        before={'binary':fingerprint(installed),'profile':fingerprint(profile)}
        def update():
            result=subprocess.run([str(installed),'update','--state',str(self.state),'--steam-library',str(self.library)],text=True,capture_output=True,env={**os.environ,'MTGA_TEST_ASSUME_STOPPED':'1'},timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        update();first=self.game_hashes();self.publish(2);update()
        self.assertNotEqual(first,self.game_hashes())
        self.assertEqual(before,{'binary':fingerprint(installed),'profile':fingerprint(profile)})
        self.assertEqual(json.loads((self.state/'installation.json').read_text())['release_id'],self.release['id'])

    def test_signature_verifier_rejects_wrong_public_key(self):
        public=self.root/'public.txt';public.write_text(self.pub)
        self.invoke('verify-envelope','--input',str(self.feed/'index-testing.json'),'--key',str(public),base=False)
        public.write_text(base64.b64encode(b'x'*32).decode())
        r=self.invoke('verify-envelope','--input',str(self.feed/'index-testing.json'),'--key',str(public),base=False,ok=False)
        self.assertNotEqual(r.returncode,0)


class PublicationTests(SubscriberTests):
    def publisher_run_fixture(self):
        run=self.root/'publisher-run';run.mkdir()
        snapshot={'client_version':'2026.64.0','steam_build':'25763111','data_versions':{},'files':{},'manifests':{}}
        report={'schema':'MTGA_CN_MAINTAINER_V1','run_id':'scope-fixture','candidate_files':{},'code_fingerprints':{},
                'localization':{'static_status':'PASS'},'import_compatibility':{'static_status':'PASS'}}
        meta={'layers':[]}
        for layer,rel in self.paths.items():
            base=run/'official'/rel;base.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(self.bases[layer],base)
            out=run/('candidate-'+layer);shutil.copyfile(self.root/(layer+'-r1'),out)
            snapshot['files'][layer]={'relative':rel,**fingerprint(base)}
            report['candidate_files'][layer]={'relative':rel,'path':str(out),**fingerprint(out)}
            meta['layers'].append({'layer':layer,'relative':rel,'fingerprint':fingerprint(base)})
        for name,value in [('snapshot.json',snapshot),('report.json',report),('official-base.json',meta)]:
            (run/name).write_bytes(canonical(value))
        return run,snapshot,report

    def test_local_acceptance_preserves_exact_test_scope(self):
        run,snapshot,report=self.publisher_run_fixture()
        expected={k:{x:v[x] for x in ('relative','sha256','bytes')} for k,v in report['candidate_files'].items()}
        local={'status':'PASS_USER_REPORTED_LOCAL_USE','user_quote':'two checks passed','scope':'Split Second tooltip and mixed 60-card import only',
               'client_version':snapshot['client_version'],'steam_build':snapshot['steam_build'],'run_id':report['run_id'],'installed_candidate_files':expected}
        evidence=self.root/'local-use.json';evidence.write_bytes(canonical(local))
        result=build_package(run,self.root/'scoped-package',BINARY,self.private,self.pub,2,local_acceptance=evidence)
        self.assertTrue(result['acceptance']['scope'].startswith(local['scope']))
        self.assertNotIn('重启复测',result['acceptance']['scope']);self.assertEqual(result['acceptance']['status'],'testing')
        local['scope']='';evidence.write_bytes(canonical(local))
        with self.assertRaisesRegex(ValueError,'实际测试范围'):
            build_package(run,self.root/'empty-scope',BINARY,self.private,self.pub,2,local_acceptance=evidence)
        self.assertFalse((self.root/'empty-scope').exists())

    def test_protocol_three_is_bound_in_signed_package_and_invalid_override_rejected(self):
        run,_,_=self.publisher_run_fixture()
        release=build_package(run,self.root/'v3-package',BINARY,self.private,self.pub,1,minimum_client=3)
        self.assertEqual(release['minimum_client'],3)
        envelope=json.loads((self.root/'v3-package'/(release['id']+'.json')).read_text())
        self.assertEqual(json.loads(base64.b64decode(envelope['payload']))['minimum_client'],3)
        self.assertEqual(release['acceptance']['status'],'testing')
        self.assertIn('真实游戏内验收尚未完成',release['acceptance']['scope'])
        for value in (True,0,1,4,'3'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'最低客户端'):
                build_package(run,self.root/('bad-minimum-'+str(value)),BINARY,self.private,self.pub,1,minimum_client=value)
        default=build_package(run,self.root/'default-protocol',BINARY,self.private,self.pub,1)
        self.assertEqual(default['minimum_client'],1)

    def test_draft_lookup_falls_back_to_all_authenticated_pages(self):
        missing=subprocess.CompletedProcess([],1,stdout='',stderr='gh: Not Found (HTTP 404)')
        draft={'id':12,'tag_name':'new-tag','draft':True,'assets':[]}
        pages=subprocess.CompletedProcess([],0,stdout=json.dumps([[{'id':1,'tag_name':'other'}],[draft]]),stderr='')
        with patch.object(gp.subprocess,'run',side_effect=[missing,pages]) as call:
            self.assertEqual(gp.release_state('example/repo','new-tag'),draft)
            self.assertEqual(call.call_args.args[0],
                             ['gh','api','--paginate','--slurp','repos/example/repo/releases?per_page=100'])

    def test_draft_lookup_permissions_bad_pagination_and_ambiguity_fail_closed(self):
        forbidden=subprocess.CompletedProcess([],1,stdout='',stderr='gh: Forbidden (HTTP 403)')
        missing=subprocess.CompletedProcess([],1,stdout='',stderr='gh: Not Found (HTTP 404)')
        with patch.object(gp.subprocess,'run',return_value=forbidden) as call:
            with self.assertRaises(RuntimeError):gp.release_state('example/repo','tag')
            self.assertEqual(call.call_count,1)
        with patch.object(gp.subprocess,'run',side_effect=[missing,forbidden]):
            with self.assertRaisesRegex(RuntimeError,'停止'):gp.release_state('example/repo','tag')
        for value in ({'tag_name':'tag'},[[{'tag_name':'tag'}],[{'tag_name':'tag'}]]):
            response=subprocess.CompletedProcess([],0,stdout=json.dumps(value),stderr='')
            with self.subTest(value=value),patch.object(gp.subprocess,'run',side_effect=[missing,response]):
                with self.assertRaises(ValueError):gp.release_state('example/repo','tag')
        response=subprocess.CompletedProcess([],0,stdout=json.dumps([[]]),stderr='')
        with patch.object(gp.subprocess,'run',side_effect=[missing,response]):
            self.assertIsNone(gp.release_state('example/repo','tag'))

    def network_package(self):
        self.repo='example/update-files';self.tag='mtga-'+self.release['id']
        self.release['minimum_client']=2
        for row in self.release['resources']:row['patch']['url']=f'https://github.com/{self.repo}/releases/download/{self.tag}/'+row['patch']['name']
        self.resign()
        profile=json.loads(self.profile.read_text());profile['feed']=f'https://raw.githubusercontent.com/{self.repo}/main/feed'
        (self.feed/'profile.json').write_bytes(canonical(profile))
        self.public=self.root/'public.txt';self.public.write_text(self.pub)
        return gp.publication_plan(self.feed,self.repo,self.tag,BINARY,self.public)

    def test_merge_retains_versions_and_advances_remote_sequence(self):
        old=copy.deepcopy(self.index);old['sequence']=2**52
        incoming=copy.deepcopy(old);incoming['sequence']=1;incoming['releases'][0]['id']='new-build'
        incoming['releases'][0]['steam_build']='25763112'
        merged=gp.merge_index(old,incoming,'testing',dt.datetime(2026,1,1,tzinfo=dt.timezone.utc))
        self.assertEqual(merged['sequence'],old['sequence']+1);self.assertEqual(len(merged['releases']),2)
        same=gp.merge_index(merged,incoming,'testing');self.assertEqual(len(same['releases']),2)

    def test_release_id_or_revision_identity_cannot_change(self):
        incoming=copy.deepcopy(self.index);incoming['releases'][0]['manifest']['fingerprint']['sha256']='f'*64
        with self.assertRaises(ValueError):gp.merge_index(self.index,incoming,'testing')
        incoming=copy.deepcopy(self.index);incoming['releases'][0]['id']='another-id'
        with self.assertRaises(ValueError):gp.merge_index(self.index,incoming,'testing')

    def test_new_revision_must_increase_but_exact_legacy_renewal_is_allowed(self):
        old=copy.deepcopy(self.index);old['releases'][0]['revision']=3
        incoming=copy.deepcopy(self.index);incoming['releases'][0]['revision']=2
        incoming['releases'][0]['id']='same-build-r2'
        with self.assertRaisesRegex(ValueError,'递增'):gp.merge_index(old,incoming,'testing')
        self.assertEqual(len(gp.merge_index(old,old,'testing')['releases']),1)
        incoming['releases'][0]['revision']=4
        self.assertEqual(len(gp.merge_index(old,incoming,'testing')['releases']),2)

    def test_plan_rejects_source_repo_or_wrong_asset_origin_or_tamper(self):
        self.network_package()
        with self.assertRaises(ValueError):gp.validate_repo(gp.SOURCE_REPO)
        self.release['resources'][0]['patch']['url']='https://github.com/else/repo/releases/download/v1/file.delta';self.resign()
        with self.assertRaises(ValueError):gp.publication_plan(self.feed,self.repo,self.tag,BINARY,self.public)
        self.network_package();row=self.release['resources'][0]
        (self.feed/row['patch']['name']).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):gp.publication_plan(self.feed,self.repo,self.tag,BINARY,self.public)

    def installer(self):
        out=self.root/'installer'
        package_installer.package(BINARY,self.feed/'profile.json',out)
        return Path(str(out)+'.zip')

    def test_installer_exact_contents_and_tampering(self):
        plan=self.network_package(); original=self.installer()
        gp.inspect_installer(original,plan,self.public,BINARY)
        with zipfile.ZipFile(original) as archive: data={i.filename:archive.read(i) for i in archive.infolist()}
        for variant in ('extra','script','binary','profile','comment','metadata','directory','absolute','dotpath'):
            target=self.root/(variant+'.zip');changed=dict(data);root=next(iter(data)).split('/')[0]
            if variant=='extra':changed[root+'/secret.key']=b'private'
            if variant in ('script','binary','profile'):
                name={'script':'安装 MTGA 中文维护.command','binary':'mtga-cn','profile':'profile.json'}[variant]
                changed[root+'/'+name]=b'evil'
            with zipfile.ZipFile(target,'w') as archive:
                for name,content in changed.items():
                    info=zipfile.ZipInfo(('/'+name.split('/',1)[1]) if variant=='absolute' else name.replace('/', '/./',1) if variant=='dotpath' else name)
                    if variant=='metadata':info.extra=b'\x01\x00\x00\x00'
                    archive.writestr(info,content)
                if variant=='comment':archive.comment=b'secret'
                if variant=='directory':archive.writestr(root+'/hidden/',b'secret')
            with self.assertRaises(ValueError,msg=variant):gp.inspect_installer(target,plan,self.public,BINARY)

    def test_frozen_bytes_ignore_later_original_changes(self):
        plan=self.network_package();installer=self.installer();scratch=self.root/'stage';scratch.mkdir()
        frozen,zip_path=gp.freeze_publication(plan,installer,scratch,self.public,BINARY)
        for asset in plan['assets'].values():Path(asset['path']).write_bytes(b'changed')
        Path(plan['manifest']).write_bytes(b'changed');installer.write_bytes(b'changed')
        for asset in frozen['assets'].values():gp.check(asset['path'],asset)
        gp.check(frozen['manifest'],plan['index']['releases'][0]['manifest']['fingerprint'])
        gp.inspect_installer(zip_path,plan,self.public,BINARY)

    def test_mutation_between_plan_and_freeze_rejected(self):
        plan=self.network_package();scratch=self.root/'stage';scratch.mkdir()
        Path(plan['manifest']).write_bytes(b'changed')
        with self.assertRaises(ValueError):gp.freeze_publication(plan,None,scratch,self.public,BINARY)

    def test_remote_symlink_rejected_before_upload(self):
        self.network_package();bare,work=self.bare_repo()
        (work/'feed').symlink_to('/tmp')
        gp.run(['git','add','.'],work);gp.run(['git','-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','bad link'],work);gp.run(['git','push','origin','main'],work)
        with patch.object(gp,'run',side_effect=self.local_run(bare)),patch.object(gp,'upload_assets') as upload:
            with self.assertRaises(ValueError):gp.publish(self.feed,self.repo,self.tag,BINARY,self.public,self.private)
            upload.assert_not_called()

    def asset_verifier_fixture(self):
        plan=self.network_package(); name,item=next(iter(plan['assets'].items()))
        scratch=self.root/'remote-verify';scratch.mkdir()
        asset={'name':name,'size':item['bytes'],'digest':'sha256:'+item['sha256'],'state':'uploaded'}
        def mock_run(headers,code='200'):
            def run(argv,cwd=None):
                argv=list(map(str,argv))
                if argv[:2]==['gh','api']:return json.dumps({'assets':[asset]})
                self.assertEqual(argv[0],'/usr/bin/curl');self.assertIn('--head',argv)
                Path(argv[argv.index('--dump-header')+1]).write_text(headers)
                return code
            return run
        return name,item,scratch,asset,mock_run

    def test_server_digest_and_final_anonymous_head_are_distinct_evidence(self):
        name,item,scratch,asset,mock=self.asset_verifier_fixture()
        headers='HTTP/1.1 302 Found\r\nContent-Length: 0\r\n\r\nHTTP/2 200\r\nContent-Length: '+str(item['bytes'])+'\r\n\r\n'
        with patch.object(gp,'run',side_effect=mock(headers)),patch.object(gp,'download') as full:
            result=gp.verify_remote_asset(self.repo,self.tag,name,item,scratch)
            self.assertEqual(result['method'],'SERVER_DIGEST_AND_ANONYMOUS_HEAD')
            self.assertFalse(result['anonymous_body_verified']);full.assert_not_called()

    def test_remote_state_digest_and_head_mismatch_are_rejected(self):
        name,item,scratch,asset,mock=self.asset_verifier_fixture()
        baseline=dict(asset)
        for field,value in [('state','starter'),('size',item['bytes']+1),('digest','sha256:'+'0'*64),('digest','bad-digest')]:
            asset.clear();asset.update(baseline);asset[field]=value
            with patch.object(gp,'run',side_effect=mock('HTTP/2 200\n\n')),patch.object(gp,'download') as full:
                with self.assertRaises(ValueError):gp.verify_remote_asset(self.repo,self.tag,name,item,scratch)
                full.assert_not_called()
        asset.clear();asset.update(baseline)
        for code,headers in [('403','HTTP/2 403\n\n'),('200','HTTP/2 200\nContent-Length: 1\n\n'),('200','HTTP/2 200\nContent-Length: '+str(item['bytes'])+'\nContent-Length: 1\n\n')]:
            with patch.object(gp,'run',side_effect=mock(headers,code)):
                with self.assertRaises(ValueError):gp.verify_remote_asset(self.repo,self.tag,name,item,scratch)

    def test_missing_server_digest_requires_full_verified_download(self):
        name,item,scratch,asset,mock=self.asset_verifier_fixture();asset['digest']=None
        def download(url,path,limit):
            self.assertEqual(limit,item['bytes']);shutil.copyfile(item['path'],path)
        with patch.object(gp,'run',side_effect=mock('')),patch.object(gp,'download',side_effect=download) as full:
            result=gp.verify_remote_asset(self.repo,self.tag,name,item,scratch)
            self.assertTrue(result['anonymous_body_verified']);self.assertEqual(result['method'],'FULL_ANONYMOUS_DOWNLOAD_SHA256')
            self.assertEqual(full.call_count,1)
        with patch.object(gp,'run',side_effect=mock('')),patch.object(gp,'download',side_effect=lambda url,path,limit:Path(path).write_bytes(b'bad')):
            with self.assertRaises(ValueError):gp.verify_remote_asset(self.repo,self.tag,name,item,scratch)

    def bare_repo(self):
        bare=self.root/'remote.git';subprocess.run(['git','init','--bare','--initial-branch=main',str(bare)],check=True,capture_output=True)
        work=self.root/'seed';subprocess.run(['git','clone',str(bare),str(work)],check=True,capture_output=True)
        (work/'README.md').write_text('distribution only')
        for argv in (['git','add','.'],['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','initial'],['git','push','origin','main']):subprocess.run(argv,cwd=work,check=True,capture_output=True)
        return bare,work

    def local_run(self,bare):
        real=gp.run
        def run(argv,cwd=None):
            argv=list(map(str,argv))
            if argv[:3]==['gh','repo','view']:return json.dumps({'nameWithOwner':self.repo,'visibility':'PUBLIC'})
            if argv[:2]==['git','clone']:argv[-2]=str(bare)
            return real(argv,cwd)
        return run

    def test_upload_failure_never_advances_feed(self):
        self.network_package();bare,work=self.bare_repo();before=gp.run(['git','rev-parse','HEAD'],work)
        with patch.object(gp,'run',side_effect=self.local_run(bare)),patch.object(gp,'prepare_release',return_value={'draft':True}),patch.object(gp,'upload_assets',side_effect=RuntimeError('upload failed')):
            with self.assertRaisesRegex(RuntimeError,'upload failed'):gp.publish(self.feed,self.repo,self.tag,BINARY,self.public,self.private)
        self.assertEqual(before,gp.run(['git','--git-dir',bare,'rev-parse','main']))
        self.assertFalse((work/'feed').exists())

    def test_concurrent_push_rejected_without_overwrite(self):
        self.network_package();bare,work=self.bare_repo()
        def race(*args,**kwargs):
            (work/'parallel.txt').write_text('other publisher')
            gp.run(['git','add','.'],work);gp.run(['git','-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','parallel'],work);gp.run(['git','push','origin','main'],work)
            return {}
        with patch.object(gp,'run',side_effect=self.local_run(bare)),patch.object(gp,'prepare_release',return_value={'draft':True}),patch.object(gp,'upload_assets',side_effect=race):
            with self.assertRaises(RuntimeError):gp.publish(self.feed,self.repo,self.tag,BINARY,self.public,self.private)
        self.assertEqual(gp.run(['git','rev-parse','HEAD'],work),gp.run(['git','--git-dir',bare,'rev-parse','main']))

    def test_new_revision_requires_own_tag_and_creates_only_draft(self):
        plan=self.network_package();scratch=self.root/'draft-stage';scratch.mkdir()
        plan['tag']='old-shared-game-tag'
        with patch.object(gp,'release_state',return_value=None),patch.object(gp,'run') as run:
            with self.assertRaisesRegex(ValueError,'独立 Release tag'):gp.prepare_release(plan,None,scratch)
            run.assert_not_called()
        plan['tag']='mtga-'+plan['release']['id']
        draft={'draft':True,'name':plan['release']['id'],'prerelease':True,'assets':[]}
        with patch.object(gp,'release_state',side_effect=[None,draft]),patch.object(gp,'run') as run:
            self.assertEqual(gp.prepare_release(plan,None,scratch),draft)
            command=run.call_args.args[0]
            self.assertIn('--draft',command);self.assertIn('--prerelease',command)
            self.assertNotIn('--draft=false',command)

    def test_legacy_signed_reference_renews_without_new_release_or_new_files(self):
        plan=self.network_package();plan['tag']='legacy-shared-r1'
        existing={'draft':False,'assets':[]}
        old=copy.deepcopy(plan['index'])
        with patch.object(gp,'release_state',return_value=existing),patch.object(gp,'run') as run:
            self.assertEqual(gp.prepare_release(plan,old,self.root),existing)
            run.assert_not_called()
            with self.assertRaisesRegex(ValueError,'不能新增附件'):
                gp.upload_assets(plan,None,self.root,self.root,release=existing)
            run.assert_not_called()

    def test_published_tag_cannot_add_unindexed_revision_and_draft_identity_must_match(self):
        plan=self.network_package()
        for existing in ({'draft':False},{'draft':True,'name':'other','prerelease':True},
                         {'draft':True,'name':plan['release']['id'],'prerelease':False}):
            with patch.object(gp,'release_state',return_value=existing),patch.object(gp,'run') as run:
                with self.subTest(existing=existing),self.assertRaises(ValueError):
                    gp.prepare_release(plan,None,self.root)
                run.assert_not_called()

    def test_draft_digest_check_does_not_claim_anonymous_access(self):
        name,item,scratch,asset,mock=self.asset_verifier_fixture()
        with patch.object(gp,'release_state',return_value={'assets':[asset]}) as state,patch.object(gp,'run') as run:
            result=gp.verify_remote_asset(self.repo,self.tag,name,item,scratch,draft=True)
            self.assertEqual(result['method'],'DRAFT_SERVER_DIGEST')
            self.assertFalse(result['anonymous_available'])
            state.assert_called_once_with(self.repo,self.tag);run.assert_not_called()
        asset['digest']='sha256:'+'0'*64
        with patch.object(gp,'release_state',return_value={'assets':[asset]}):
            with self.assertRaises(ValueError):gp.verify_remote_asset(self.repo,self.tag,name,item,scratch,draft=True)

    def test_draft_missing_digest_requires_authenticated_complete_body(self):
        name,item,scratch,asset,mock=self.asset_verifier_fixture()
        asset['digest']=None;asset['id']=12345
        def body(argv,stdout,stderr):
            self.assertEqual(argv,['gh','api','-H','Accept: application/octet-stream',
                                   f'repos/{self.repo}/releases/assets/12345'])
            stdout.write(Path(item['path']).read_bytes())
            return subprocess.CompletedProcess(argv,0,stderr=b'')
        with patch.object(gp,'release_state',return_value={'assets':[asset]}),patch.object(gp.subprocess,'run',side_effect=body):
            result=gp.verify_remote_asset(self.repo,self.tag,name,item,scratch,draft=True)
            self.assertTrue(result['authenticated_body_verified'])
            self.assertFalse(result['anonymous_body_verified'])
        def corrupt(argv,stdout,stderr):
            stdout.write(b'corrupt');return subprocess.CompletedProcess(argv,0,stderr=b'')
        with patch.object(gp,'release_state',return_value={'assets':[asset]}),patch.object(gp.subprocess,'run',side_effect=corrupt):
            with self.assertRaises(ValueError):gp.verify_remote_asset(self.repo,self.tag,name,item,scratch,draft=True)

    def test_notification_finalization_only_after_pushed_verified_index(self):
        self.network_package();bare,work=self.bare_repo();events=[]
        def prepare(plan,old,scratch):
            self.assertIsNone(old);events.append('draft')
            return {'draft':True}
        def upload(*args,**kwargs):
            events.append('assets')
            self.assertNotIn('verified-index',events)
            self.assertFalse((work/'feed').exists())
            return {}
        def download(url,path,limit,timeout):
            head=gp.run(['git','--git-dir',bare,'rev-parse','main']).strip()
            self.assertIn('/'+head+'/feed/index-testing.json',url)
            Path(path).write_text(gp.run(['git','--git-dir',bare,'show',head+':feed/index-testing.json']))
            events.append('verified-index')
        def final(plan,state,assets,scratch):
            self.assertEqual(events,['draft','assets','verified-index'])
            events.append('notification');return 'PUBLISHED_AFTER_SIGNED_INDEX'
        with patch.object(gp,'run',side_effect=self.local_run(bare)),patch.object(gp,'prepare_release',side_effect=prepare),\
             patch.object(gp,'upload_assets',side_effect=upload),patch.object(gp,'download',side_effect=download),\
             patch.object(gp,'finalize_release',side_effect=final):
            receipt=gp.publish(self.feed,self.repo,self.tag,BINARY,self.public,self.private)
        self.assertEqual(events[-1],'notification')
        self.assertEqual(receipt['release_publication'],'PUBLISHED_AFTER_SIGNED_INDEX')

    def test_failed_public_index_readback_does_not_publish_notification(self):
        self.network_package();bare,work=self.bare_repo()
        with patch.object(gp,'run',side_effect=self.local_run(bare)),\
             patch.object(gp,'prepare_release',return_value={'draft':True}),\
             patch.object(gp,'upload_assets',return_value={}),\
             patch.object(gp,'download',side_effect=RuntimeError('readback failed')),\
             patch.object(gp,'finalize_release') as final:
            with self.assertRaisesRegex(RuntimeError,'readback failed'):
                gp.publish(self.feed,self.repo,self.tag,BINARY,self.public,self.private)
            final.assert_not_called()

    def test_final_publish_failure_keeps_retry_instruction_and_testing_channel(self):
        plan=self.network_package()
        with patch.object(gp,'run',side_effect=RuntimeError('network failure')) as run:
            with self.assertRaisesRegex(RuntimeError,'重试同一冻结包'):
                gp.finalize_release(plan,{'draft':True}, {}, self.root)
            command=run.call_args.args[0]
            self.assertEqual(command,['gh','release','edit',self.tag,'--repo',self.repo,'--draft=false'])
            self.assertNotIn('--prerelease=false',command)


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for cls in (URLAndProtocolTests,PublicationTests):
        for name in cls.__dict__:
            if name.startswith('test_'):suite.addTest(cls(name))
    return suite
