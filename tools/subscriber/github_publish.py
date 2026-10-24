#!/usr/bin/env python3
"""Publish content-addressed assets, then atomically advance a signed public feed.

No key/token is uploaded. The unified public repository hosts source and delivery.
Retired private source repositories are not delivery targets. Normal push is CAS.
"""
import argparse
import base64
import datetime as dt
import json
import hashlib
import zipfile
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from package_installer import fixed_text
from publisher import canonical, check, dump, fingerprint, sign

SOURCE_REPO = '1996Neil/mtga-chinese-bilingual-maintainer'
RETIRED_REPOS = {SOURCE_REPO.lower(), '1996neil/mtga-resource-acquisition-probe'}


def run(argv, cwd=None):
    r = subprocess.run([str(a) for a in argv], cwd=cwd, text=True, capture_output=True)
    if r.returncode:
        raise RuntimeError('命令失败：' + str(argv[0]) + '\n' + r.stderr[-2500:])
    return r.stdout


def signed(path, client, public_file):
    run([client,'verify-envelope','--input',path,'--key',public_file])
    return json.loads(base64.b64decode(json.loads(Path(path).read_text())['payload'],validate=True))


def validate_repo(repo):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or repo.lower() in RETIRED_REPOS:
        raise ValueError('必须使用当前公开主仓库或明确选择的公开分发仓库，不能发布到已迁移的旧仓库。')


def merge_index(old, incoming, channel, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if incoming['schema']!='MTGA_CN_INDEX_V1' or incoming['channel']!=channel:
        raise ValueError('待发布索引渠道不一致。')
    if old and (old['schema']!='MTGA_CN_INDEX_V1' or old['channel']!=channel):
        raise ValueError('远端索引渠道不一致。')
    old_refs=(old or {}).get('releases',[])
    for ref in incoming['releases']:
        if ref in old_refs:continue  # An exact repeat is a renewal, not a downgrade.
        previous=[r['revision'] for r in old_refs if
                  (r['client_version'],r['steam_build'])==(ref['client_version'],ref['steam_build'])]
        if previous and ref['revision']<=max(previous):
            raise ValueError('同一游戏版本的新修订号必须递增。')
    refs={};identities={}
    for index in ([old] if old else [])+[incoming]:
        for ref in index['releases']:
            ident=(ref['client_version'],ref['steam_build'],ref['revision'])
            if ref['id'] in refs and refs[ref['id']]!=ref:
                raise ValueError('同一发布 ID 不可替换内容。')
            if ident in identities and identities[ident]!=ref['id']:
                raise ValueError('同一游戏修订不可有不同发布 ID。')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}',ref['manifest']['name']):
                raise ValueError('清单文件名无效。')
            refs[ref['id']]=ref;identities[ident]=ref['id']
    if len(refs)>10000:raise ValueError('索引版本数量超过客户端限制。')
    return {'schema':'MTGA_CN_INDEX_V1','channel':channel,
            'sequence':max((old or {}).get('sequence',0)+1,incoming['sequence'],int(now.timestamp()*1000)),
            'expires_at':(now+dt.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'releases':sorted(refs.values(),key=lambda r:(r['client_version'],r['steam_build'],r['revision']))}


def publication_plan(package, repo, tag, client, public_file):
    validate_repo(repo)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}',tag):raise ValueError('Release tag 无效。')
    package=Path(package).resolve();profile=json.loads((package/'profile.json').read_text())
    feed=f'https://raw.githubusercontent.com/{repo}/main/feed'
    if profile['feed']!=feed:raise ValueError('安装配置与目标分发仓库不匹配。')
    if profile['public_key']!=Path(public_file).read_text().strip():raise ValueError('公钥不匹配。')
    channel=profile['channel'];index=signed(package/f'index-{channel}.json',client,public_file)
    if len(index['releases'])!=1:raise ValueError('每次发布只接受一个已打包版本。')
    merge_index(None,index,channel)
    ref=index['releases'][0];manifest=package/ref['manifest']['name'];check(manifest,ref['manifest']['fingerprint'])
    release=signed(manifest,client,public_file)
    if any(release[k]!=ref[k] for k in ('id','revision','client_version','steam_build')):raise ValueError('索引与清单身份不一致。')
    if release['minimum_client']<2:raise ValueError('网络分发要求 v2 客户端。')
    if channel=='stable' and release['acceptance']['status']!='accepted':raise ValueError('测试内容不能发布到稳定渠道。')
    assets={}
    for resource in release['resources']:
        patch=resource['patch'];name=patch['name']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,140}\.delta',name):raise ValueError('资源名无效。')
        if patch.get('url')!=f'https://github.com/{repo}/releases/download/{tag}/{name}':raise ValueError('资源地址不属于目标 Release。')
        path=package/name
        if path.is_symlink() or path.resolve().parent!=package:raise ValueError('资源不在发布目录中。')
        check(path,patch['fingerprint']);assets[name]={'path':str(path),**patch['fingerprint']}
    if len(assets)!=5:raise ValueError('必须有五份差分。')
    for resource in release['resources']:
        if 'full' not in resource:continue
        full=resource['full'];name=full['name']
        if release['minimum_client']<4 or full['fingerprint']!=resource['output']:
            raise ValueError('完整资源须匹配签名输出且要求协议4。')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,140}\.full',name) or name in assets:
            raise ValueError('完整资源名无效或重复。')
        if full.get('url')!=f'https://github.com/{repo}/releases/download/{tag}/{name}':raise ValueError('完整资源地址不属于目标 Release。')
        path=package/name
        if path.is_symlink() or path.resolve().parent!=package:raise ValueError('完整资源不在发布目录中。')
        check(path,full['fingerprint']);assets[name]={'path':str(path),**full['fingerprint']}
    return {'repo':repo,'tag':tag,'feed':feed,'channel':channel,'index':index,'manifest':str(manifest),'release':release,'assets':assets}


def download(url, destination, limit=512*1024*1024, timeout=1800):
    run(['/usr/bin/curl','--disable','--fail','--silent','--show-error','--location','--max-redirs','5',
         '--proto','=https','--proto-redir','=https','--retry','2','--retry-max-time','120',
         '--speed-limit','1024','--speed-time','90','--connect-timeout','15','--max-time',str(timeout),'--max-filesize',str(limit),
         '--output',destination,url])


def inspect_installer(path, plan, public_file, client):
    expected={'mtga-cn','profile.json','安装 MTGA 中文维护.command','先读我.txt','SHA256.json'}
    with zipfile.ZipFile(path) as archive:
        entries=archive.infolist()
        if archive.comment or any(x.is_dir() or x.extra or x.comment for x in entries):
            raise ValueError('安装包含多余 ZIP 元数据。')
        if len(entries)!=len(expected) or sum(x.file_size for x in entries)>32*1024*1024:
            raise ValueError('联网安装包必须只包含客户端和公开安装配置。')
        roots=set(); data={}
        for info in entries:
            parts=Path(info.filename).parts
            if (len(parts)!=2 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}',parts[0]) or parts[1] not in expected or info.filename!=parts[0]+'/'+parts[1] or (info.external_attr>>16)&0o170000 not in (0,0o100000)):
                raise ValueError('安装包含未允许文件或链接。')
            roots.add(parts[0]);data[parts[1]]=archive.read(info)
        if len(roots)!=1 or set(data)!=expected:raise ValueError('安装包结构无效。')
        for name, expected_data in fixed_text().items():
            if data[name]!=expected_data:raise ValueError('安装脚本或说明与已审模板不一致。')
        profile=json.loads(data['profile.json'])
        if profile!={'schema':'MTGA_CN_PROFILE_V1','feed':plan['feed'],'public_key':Path(public_file).read_text().strip(),'channel':plan['channel']}:
            raise ValueError('安装包订阅配置不匹配或包含多余字段。')
        binary=fingerprint(client)
        if len(data['mtga-cn'])!=binary['bytes'] or hashlib.sha256(data['mtga-cn']).hexdigest()!=binary['sha256']:
            raise ValueError('安装包客户端与指定已审构建不一致。')
        inventory=json.loads(data['SHA256.json'])
        if set(inventory)!=expected-{'SHA256.json'}:raise ValueError('安装包哈希目录不完整。')
        for name,fp in inventory.items():
            if fp!={'bytes':len(data[name]),'sha256':hashlib.sha256(data[name]).hexdigest()}:raise ValueError('安装包文件哈希不一致。')
        if archive.testzip() is not None:raise ValueError('安装包 ZIP 校验失败。')


def freeze_publication(plan, installer, scratch, public_file, client):
    # Freeze only allowlisted files BEFORE any remote mutation. Original paths are
    # never used for upload after this point; signed hashes bind copied bytes.
    staged=scratch/'staged';staged.mkdir(mode=0o700)
    frozen=json.loads(json.dumps(plan))
    for name, item in frozen['assets'].items():
        target=staged/name
        shutil.copyfile(item['path'],target);check(target,item);item['path']=str(target)
    target=staged/Path(plan['manifest']).name
    shutil.copyfile(plan['manifest'],target)
    check(target,plan['index']['releases'][0]['manifest']['fingerprint'])
    if signed(target,client,public_file)!=plan['release']:raise ValueError('暂存清单变化。')
    frozen['manifest']=str(target)
    frozen_installer=None
    if installer:
        frozen_installer=staged/Path(installer).name
        if frozen_installer.exists():raise ValueError('安装包名称与资源冲突。')
        shutil.copyfile(installer,frozen_installer)
        inspect_installer(frozen_installer,plan,public_file,client)
    return frozen,frozen_installer


def verify_remote_asset(repo, tag, name, item, scratch, *, draft=False):
    # Fresh server digest binds uploaded bytes; anonymous HEAD separately proves
    # link availability. Only the subsequent full client download proves the
    # end-to-end anonymous body transfer and reconstruction.
    release=(release_state(repo,tag) if draft else
             json.loads(run(['gh','api',f'repos/{repo}/releases/tags/{tag}'])))
    if release is None:
        raise ValueError('远端 Release 缺失，无法核对资产。')
    matches=[a for a in release['assets'] if a['name']==name]
    if len(matches)!=1:raise ValueError('远端资产名称不唯一或缺失：'+name)
    asset=matches[0]
    if asset.get('state')!='uploaded' or asset['size']!=item['bytes']:
        raise ValueError('远端资产未上传完成或大小不符：'+name)
    url=f'https://github.com/{repo}/releases/download/{tag}/{name}'
    digest=asset.get('digest')
    if draft:
        # A draft has no anonymous download URL. Authenticate through gh's
        # existing credential transport, never export a token or claim a public
        # HEAD was checked. A missing digest requires a real authenticated body.
        if digest is not None:
            if not isinstance(digest,str) or digest!='sha256:'+item['sha256']:
                raise ValueError('GitHub 草稿资产指纹与冻结文件不符：'+name)
            return {'method':'DRAFT_SERVER_DIGEST','github_digest':digest,
                    'anonymous_body_verified':False,'anonymous_available':False}
        fetched=scratch/(name+'.draft-body')
        with fetched.open('wb') as output:
            result=subprocess.run(['gh','api','-H','Accept: application/octet-stream',
                                   f'repos/{repo}/releases/assets/{asset["id"]}'],stdout=output,stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError('草稿资源认证下载失败，未发布 Release。')
        check(fetched,item);fetched.unlink()
        return {'method':'DRAFT_AUTHENTICATED_BODY_SHA256','authenticated_body_verified':True,
                'anonymous_body_verified':False,'anonymous_available':False}
    if digest is None:
        fetched=scratch/name
        download(url,fetched,item['bytes']);check(fetched,item);fetched.unlink()
        return {'method':'FULL_ANONYMOUS_DOWNLOAD_SHA256','anonymous_body_verified':True}
    if not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest) or digest!='sha256:'+item['sha256']:
        raise ValueError('GitHub 资产指纹与冻结文件不符：'+name)
    headers=scratch/(name+'.headers')
    code=run(['/usr/bin/curl','--disable','--fail','--silent','--show-error','--head','--location',
        '--max-redirs','5','--proto','=https','--proto-redir','=https','--connect-timeout','15','--max-time','180',
        '--dump-header',headers,'--output','/dev/null','--write-out','%{http_code}',url]).strip()
    if code!='200':raise ValueError('匿名下载地址不可用：'+name)
    blocks=[b for b in headers.read_text().replace('\r\n','\n').split('\n\n') if b.startswith('HTTP/')]
    if not blocks:raise ValueError('匿名 HEAD 缺少响应头。')
    lengths=[]
    for line in blocks[-1].splitlines()[1:]:
        if line.lower().startswith('content-length:'):
            value=line.split(':',1)[1].strip()
            if not value.isdigit():raise ValueError('匿名 HEAD 长度无效。')
            lengths.append(int(value))
    if lengths and any(n!=item['bytes'] for n in lengths):raise ValueError('匿名下载资源长度不符：'+name)
    headers.unlink()
    return {'method':'SERVER_DIGEST_AND_ANONYMOUS_HEAD','github_digest':digest,
            'anonymous_head_status':200,'anonymous_content_length':lengths[0] if lengths else None,
            'anonymous_body_verified':False}


def release_state(repo, tag):
    query=subprocess.run(['gh','api',f'repos/{repo}/releases/tags/{tag}'],text=True,capture_output=True)
    if query.returncode:
        if not re.search(r'(?:HTTP\s+404|\(404\))',query.stderr):
            raise RuntimeError('无法读取 Release 状态。'+query.stderr[-1000:])
        # The tag endpoint documents published releases. Authenticated list
        # includes drafts, including a draft whose tag has not yet been created.
        listed=subprocess.run(['gh','api','--paginate','--slurp',
                               f'repos/{repo}/releases?per_page=100'],
                              text=True,capture_output=True)
        if listed.returncode:
            raise RuntimeError('无法列出草稿 Release；停止而非误判为不存在。'+listed.stderr[-1000:])
        pages=json.loads(listed.stdout)
        if not isinstance(pages,list) or not all(isinstance(page,list) for page in pages):
            raise ValueError('Release 分页响应格式无效。')
        matches=[row for page in pages for row in page
                 if isinstance(row,dict) and row.get('tag_name')==tag]
        if len(matches)>1:
            raise ValueError('同一 tag 的 Release 不唯一。')
        return matches[0] if matches else None
    return json.loads(query.stdout)


def prepare_release(plan, old, scratch):
    """New revisions get a unique draft; legacy published tags only renew.

    Called after freeze and local feed validation, before any asset upload.
    A historical tag may contain r1+r2: retain those signed URLs unchanged,
    but do not let a new ref reuse it or add files to it.
    """
    repo,tag=plan['repo'],plan['tag']
    ref=plan['index']['releases'][0]
    already_indexed=ref in (old or {}).get('releases',[])
    existing=release_state(repo,tag)
    canonical_tag='mtga-'+plan['release']['id']
    if not already_indexed and tag!=canonical_tag:
        raise ValueError('新修订必须使用独立 Release tag：'+canonical_tag)
    if existing is not None:
        if existing.get('draft'):
            if (tag!=canonical_tag or existing.get('name')!=plan['release']['id'] or
                    bool(existing.get('prerelease'))!=(plan['channel']=='testing')):
                raise ValueError('既有草稿的发布身份或渠道不一致。')
        elif not already_indexed:
            raise ValueError('已公开的 Release 尚无对应签名索引，拒绝作为新修订复用。')
    else:
        if already_indexed:
            raise ValueError('已签名索引引用的历史 Release 缺失，不能用续签重建。')
        notes=scratch/'release-notes.txt'
        notes.write_text('macOS + Steam 中文维护订阅资源。\n\n'+plan['release']['acceptance']['scope']+
                         '\n\n已安装用户：打开桌面「MTGA 订阅更新」，检查更新 → 预演 → 退出游戏 → 应用更新。'
                         '\n\n欢迎 Star 支持维护；订阅 Releases 可收到新版本通知。'
                         '\n\n客户端仅 ad-hoc 签名，尚未 Developer ID 公证；不要关闭 macOS 安全检查。此为社区内容，非官方发行。\n')
        args=['gh','release','create',tag,'--repo',repo,'--target','main',
              '--title',plan['release']['id'],'--notes-file',notes,'--draft']
        if plan['channel']=='testing':args+=['--prerelease']
        run(args)
        existing=release_state(repo,tag)
        if not existing or not existing.get('draft'):
            raise RuntimeError('未能确认新 Release 草稿，停止发布。')
    return existing


def upload_assets(plan, installer, cwd, scratch, *, release=None):
    repo,tag=plan['repo'],plan['tag']
    existing=release if release is not None else release_state(repo,tag)
    if existing is None:raise ValueError('上传前必须确认 Release 草稿或已登记历史发布。')
    by_name={a['name']:a for a in existing['assets']}
    assets=dict(plan['assets'])
    if installer:
        installer=Path(installer).resolve()
        if installer.suffix!='.zip' or installer.is_symlink():raise ValueError('安装包必须是本地 ZIP 文件。')
        assets[installer.name]={'path':str(installer),**fingerprint(installer)}
    for name,item in assets.items():
        if name in by_name:
            old=by_name[name]
            if old['size']!=item['bytes'] or (old.get('digest') and old['digest']!='sha256:'+item['sha256']):
                raise ValueError('已存在的同名 Release 文件不同，拒绝覆盖：'+name)
        else:
            if not existing.get('draft'):
                raise ValueError('历史 Release 续签不能新增附件：'+name)
            print('正在上传：'+name,flush=True)
            run(['gh','release','upload',tag,item['path'],'--repo',repo])
        print('核对 GitHub '+('草稿指纹：' if existing.get('draft') else '指纹及匿名下载入口：')+name,flush=True)
        item['verification']=verify_remote_asset(repo,tag,name,item,scratch,draft=bool(existing.get('draft')))
    return assets


def finalize_release(plan, state, assets, scratch):
    """Only publish/notify after the signed index has been pushed and read back."""
    if state.get('draft'):
        try:
            run(['gh','release','edit',plan['tag'],'--repo',plan['repo'],'--draft=false'])
        except RuntimeError as error:
            raise RuntimeError('签名索引已更新，Release 最终发布未确认；请重试同一冻结包，不要新建修订。') from error
        actual=release_state(plan['repo'],plan['tag'])
        if not actual or actual.get('draft'):
            raise RuntimeError('签名索引已更新，但 Release 仍为草稿；请重试同一冻结发布。')
    for name,item in assets.items():
        item['verification']=verify_remote_asset(plan['repo'],plan['tag'],name,item,scratch)
    return 'PUBLISHED_AFTER_SIGNED_INDEX' if state.get('draft') else 'EXISTING_PUBLISHED_RELEASE_RENEWED'


def publish(package, repo, tag, client, public_file, private_key, installer=None, output_receipt=None):
    plan=publication_plan(package,repo,tag,client,public_file)
    remote=json.loads(run(['gh','repo','view',repo,'--json','nameWithOwner,visibility']))
    if remote['nameWithOwner'].lower()!=repo.lower() or remote['visibility']!='PUBLIC':raise ValueError('目标必须是明确的公开分发仓库。')
    with tempfile.TemporaryDirectory(prefix='mtga-github-publish-') as directory:
        scratch=Path(directory).resolve();checkout=scratch/'repo'
        plan,installer=freeze_publication(plan,installer,scratch,public_file,client)
        run(['git','clone','--quiet','--branch','main',f'https://github.com/{repo}.git',checkout])
        old_head=run(['git','rev-parse','HEAD'],checkout).strip()
        feed=checkout/'feed'
        if feed.is_symlink():raise ValueError('远端 feed 不得是符号链接。')
        feed.mkdir(exist_ok=True)
        for path in [checkout/'download.json',*feed.iterdir()]:
            if path.is_symlink():raise ValueError('远端发布目标不得是符号链接。')
        index_path=feed/f"index-{plan['channel']}.json"
        old=signed(index_path,client,public_file) if index_path.exists() else None
        merged=merge_index(old,plan['index'],plan['channel'])
        target=feed/Path(plan['manifest']).name
        if target.exists() and target.read_bytes()!=Path(plan['manifest']).read_bytes():raise ValueError('既有发布清单不可修改。')
        for ref in (old or {}).get('releases',[]):
            check(feed/ref['manifest']['name'],ref['manifest']['fingerprint'])
            signed(feed/ref['manifest']['name'],client,public_file)
        release=prepare_release(plan,old,scratch)
        assets=upload_assets(plan,installer,checkout,scratch,release=release)
        shutil.copyfile(plan['manifest'],target)
        sign(client,private_key,merged,index_path,scratch)
        signed(index_path,client,public_file)
        public_installer=None
        if installer:
            name=Path(installer).name;public_installer=f'https://github.com/{repo}/releases/download/{tag}/{name}'
            dump(checkout/'download.json',{'installer_url':public_installer,'fingerprint':{k:assets[name][k] for k in ('sha256','bytes')},'channel':plan['channel'],'feed':plan['feed']})
        run(['git','add','feed',*(['download.json'] if installer else [])],checkout)
        run(['git','-c','user.name=MTGA Update Publisher','-c','user.email=mtga-updates@users.noreply.github.com','commit','-m','Publish signed '+plan['release']['id']],checkout)
        commit=run(['git','rev-parse','HEAD'],checkout).strip()
        # CAS: another publisher advancing main rejects this push, leaving index unchanged.
        try:
            run(['git','push','origin','HEAD:refs/heads/main'],checkout)
        except RuntimeError:
            actual=run(['git','ls-remote','origin','refs/heads/main'],checkout).split()
            if not actual or actual[0]!=commit:raise
        probe=scratch/'probe-index.json'
        download(f'https://raw.githubusercontent.com/{repo}/{commit}/feed/{index_path.name}',probe,2*1024*1024,180)
        check(probe,fingerprint(index_path));signed(probe,client,public_file)
        publication_status=finalize_release(plan,release,assets,scratch)
        receipt={'schema':'MTGA_CN_GITHUB_PUBLICATION_V1','repository':repo,'source_repository_visibility_changed':False,
                 'previous_commit':old_head,'commit':commit,'feed':plan['feed'],'index_sequence':merged['sequence'],
                 'release_id':plan['release']['id'],'channel':plan['channel'],'installer_url':public_installer,
                 'release_tag':tag,'release_publication':publication_status,
                 'assets_verified':{n:{k:a[k] for k in ('sha256','bytes','verification')} for n,a in assets.items()},
                 'full_client_network_verification':'PENDING_SEPARATE_VALIDATION',
                 'signed_index_commit_url_verified':True}
        if output_receipt:dump(output_receipt,receipt)
        return receipt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('plan','publish'));p.add_argument('--package',type=Path,required=True)
    p.add_argument('--repo',required=True);p.add_argument('--tag',required=True);p.add_argument('--client',type=Path,required=True)
    p.add_argument('--public-key-file',type=Path,required=True);p.add_argument('--key',type=Path)
    p.add_argument('--installer',type=Path);p.add_argument('--receipt',type=Path)
    a=p.parse_args()
    if a.command=='plan':
        result=publication_plan(a.package,a.repo,a.tag,a.client,a.public_key_file)
        print(json.dumps({'repo':a.repo,'tag':a.tag,'feed':result['feed'],'release':result['release']['id'],'asset_count':len(result['assets']),'asset_bytes':sum(x['bytes'] for x in result['assets'].values())},ensure_ascii=False,indent=2))
    else:
        if not a.key:p.error('publish requires --key')
        print(json.dumps(publish(a.package,a.repo,a.tag,a.client,a.public_key_file,a.key,a.installer,a.receipt),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
