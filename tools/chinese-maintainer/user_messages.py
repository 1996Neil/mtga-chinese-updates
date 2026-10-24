"""Chinese presentation only; machine states and validation rules stay unchanged."""
import json

LABELS = {
    'PASS': '通过', 'FAIL': '未通过', 'PENDING': '待完成', 'OUTDATED': '需要重新检查',
    'trial': '临时测试中，尚未正式安装', 'installed': '已正式安装',
    'installing': '安装未完成', 'rolling-back': '回滚未完成', 'rolled-back': '已回滚',
    'backed-up': '已备份，安装尚未完成',
    'candidate generated; not installed': '候选已生成，尚未安装',
    'temporary test session; client validation pending': '临时测试中，等待游戏内验收',
    'installed; client validated': '已正式安装，游戏内验收通过',
    'rolled back to exact pre-transaction resources; not installed': '已恢复本次安装前的文件状态',
    'BUILD FAILED; not installed': '构建失败，未安装', 'building': '构建尚未完成',
    'card': '卡牌数据库', 'client': '客户端文本', 'resources': '界面资源',
    'bootstrap_fonts': '基础字体资源', 'font_bundle': '字体包',
    'english-60': '英文牌表', 'chinese-60': '中文牌表', 'mixed-60': '中英混合牌表',
    'qualified-60': '指定系列与编号的牌表', 'special-english-60': '特殊牌面结构英文牌表',
}


def label(value):
    return LABELS.get(value, value)


def base_message(result):
    version = result['release']['client_version']
    lines = [f'当前游戏：{version}（Steam 构建 {result["release"]["steam_build"]}）',
             '官方底稿：用于重建汉化的、经过核验的原始资源。']
    for row in result['layers']:
        state = '已核验' if row['status'] == 'verified' else '未核验'
        lines.append(f'  {label(row["layer"])}：{state}')
    if result['safe_to_rebuild']:
        lines += ['结果：底稿检查通过，可以按 3 构建候选。',
                  '注意：这不代表候选已构建、游戏内验收通过或已安装。']
    else:
        lines.append('结果：暂时不能构建候选。')
        for error in result.get('errors', []):
            if error == 'No trusted opaque base for this exact release':
                lines += [f'原因：维护器尚未登记适用于 {version} 的可信界面／基础字体底稿。',
                          '下一步：请维护者核验并接入这一版本的官方资源；完成后再按 2 检查、按 3 构建。',
                          '反复按 3 不会解决这个问题；不要用旧版备份替代新版底稿。']
            else:
                lines.append(error_message(RuntimeError(error)))
        if not result.get('errors'):
            lines += ['原因：部分当前资源或原始底稿未能通过校验。',
                      '下一步：请维护者检查诊断详情，确认来源后再构建；不要直接安装或回滚。']
    lines.append('本次底稿检查只读取文件，不修改游戏。')
    return '\n'.join(lines)


def error_message(error):
    raw = str(error)
    if raw.startswith('Official base unresolved: '):
        try:
            return '构建已停止，未安装。\n' + base_message(json.loads(raw.split(': ', 1)[1]))
        except (ValueError, KeyError, TypeError):
            pass
    rules = [
        (('MTGA is running',), '游戏仍在运行，或无法确认游戏已退出。', '完全退出 MTGA 后重试；不要只关闭游戏窗口。'),
        (('No trusted opaque base',), '当前版本缺少经过核验的界面或基础字体底稿。', '请维护者接入新版官方底稿，再按 2 检查。'),
        (('Incomplete/unknown candidate',), '没有完整可用的候选，最近一次构建可能已失败。', '先按 1 查看失败原因，再按 2 检查底稿；检查通过后按 3。'),
        (('Maintainer code changed', 'Reviewed translation inputs changed'), '维护程序或已审核译文已变化，原候选需要重新构建。', '先按 2 检查底稿；通过后按 3。新候选仍需独立审查和游戏内验收。'),
        (('SAFE TO INSTALL: NO',), '当前候选尚不满足本次安装／临时测试的条件。', '先完成下列阻塞项；游戏内验收说明见菜单 6。\n' + raw.partition('—')[2].strip()),
        (('Official update/non-target resource changed', 'MTGA update/resource drift', 'Update retained old patch', 'Unknown current '), '游戏版本或资源与原记录不一致，已拒绝继续使用旧安装／回滚记录。', '先按 2 检查新版底稿；不要用旧备份覆盖更新后的游戏。'),
        (('A test/recovery transaction is active', 'Active transaction', 'Unfinished installation transaction'), '存在尚未结束的临时测试、安装或恢复记录。', '先按 1 查看状态；测试中请记录真实验收结果，需恢复时退出游戏后按 5。若游戏已更新，请先让维护者检查。'),
        (('Test session already active',), '该候选已经处于临时测试状态。', '按菜单 6 的指南完成游戏内验收，或退出游戏后按 5 恢复。'),
        (('No Maintainer installation/test session exists',), '没有本工具的安装或临时测试记录，无法回滚。', '无需为一次构建失败执行回滚；按 1 查看状态。'),
        (('Start a current candidate test-session first', 'test-session requires --begin'), '需要先开始当前候选的受控临时测试。', '按菜单 6 阅读临时测试说明；静态检查及独立审查通过后才能开始。'),
        (('Actual user confirmation required', 'Runtime evidence must bind', 'Chinese/special-structure checks incomplete', 'One or more native import tests', 'New-card canonical/display', 'Native export identity'), '游戏内验收记录不完整、不匹配，或导入／导出结果未通过。', '根据菜单 6 完成真实测试并保存对应导出；未测项目不能填写通过。'),
        (('Rollback backup verification failed', 'Backup corrupted'), '回滚备份未通过完整性检查。', '保留当前游戏和备份，请维护者检查；不要继续覆盖文件。'),
        (('Active SQLite sidecar',), '数据库存在活动日志，当前文件不适合直接处理。', '退出 MTGA 并等待更新完成后重试；不要手动删除数据库日志。'),
        (('Expected one ', 'No official manifests', 'Raw/font missing'), '未找到唯一且完整的活动游戏资源。', '等待 Steam 和游戏内下载完成，退出游戏后重试；若仍失败，请维护者检查资源路径。'),
        (('Unknown ', 'Unsupported ', 'Duplicate ', 'System font lacks', 'Heiti SC'), '资源结构或字体未通过兼容性检查。', '请维护者检查这一版本的资源和字体，完成适配后再构建。'),
        (('Candidate ', 'Registry source changed', 'Source changed', 'Official source snapshot changed', 'Pinned ', 'System font changed'), '候选、来源或验证材料与原记录不一致。', '保留现场并请维护者检查；不要直接安装或覆盖文件。'),
    ]
    for prefixes, reason, action in rules:
        if raw.startswith(prefixes):
            return f'操作已停止。\n原因：{reason}\n下一步：{action}'
    if isinstance(error, FileNotFoundError):
        return f'操作已停止：找不到所需文件。\n文件：{error.filename}\n下一步：按 1 查看状态；若是首次使用，先按 2 检查底稿。请维护者检查缺失路径。'
    if isinstance(error, BlockingIOError):
        return '操作已停止：另一项维护操作正在运行。\n下一步：等待其完成后重试，不要同时打开多个维护窗口。'
    return f'操作已停止：未通过检查或发生文件处理错误。\n下一步：保留以下技术详情，请维护者检查后再继续。\n技术详情（原文）：{raw}'


GUIDE = '''MTGA 汉化与双语导入：操作指南

一、官方更新后
1. 等 Steam 更新完成，打开 MTGA 等待游戏内资源下载完成，再完全退出游戏。
2. 按 2 检查官方底稿。显示“检查通过”后，才能按 3。
   如果提示“尚未登记可信底稿”，需维护者适配新版；反复按 3、重装或回滚都不是这一步的处理办法。
3. 按 3 构建并验证候选。这只生成待测试文件，不会安装到游戏。
4. 候选静态检查及独立工程审查通过后，进入临时测试，完成游戏内验收。
5. 真实验收结果记录通过后，退出 MTGA，按 4 正式安装／保留。

二、临时测试与验收（目前需维护者协助，菜单 4 不会代替此步骤）
在仓库根目录的终端执行以下命令；mtga-cn 可替换为 ./bin/mtga-cn。
  mtga-cn test-session --begin
此命令会备份并临时替换游戏资源；要求游戏已退出、候选静态检查及独立审查通过。
打开 MTGA，语言保持 English（英语）：汉化资源使用这个语言入口。
检查中文界面、卡名、规则、字体、搜索和重启后的显示。
使用当前候选目录 fixtures/ 的英文、中文、混合、指定系列编号、特殊牌面五份牌表逐一导入，并各保存原生导出。
按 README 的完整验收清单检查新卡、双面／历险等结构；仅尝试一份牌表不算完整验收。
复制当前候选目录 runtime-template.json，填写真实结果和导出路径，未测项目保持“未验证”。
  mtga-cn record-runtime --evidence /你的绝对路径/真实验收.json
记录通过后退出游戏，回菜单按 4。不要直接把模板里的所有项目改成通过。

三、失败后怎么做
按 1 查看状态和最近构建失败原因；按 2 检查资源；先解决提示中的阻塞项。
构建失败没有安装资源，不需要为此按 5。
按 5 只恢复本工具最近一次安装／临时测试前的实际文件，不是撤销 Steam 更新。
游戏已更新或资源不匹配时，旧回滚可能被拒绝，应让维护者检查。
安装／回滚中断后，部分文件可能已变化：保留现场，按 1 查看状态并让维护者协助恢复。

四、常用命令
  mtga-cn status                     查看当前状态及失败原因
  mtga-cn diagnose-base --human      中文底稿诊断（只读）
  mtga-cn update                     构建候选，不安装
  mtga-cn guide                      显示本指南
  mtga-cn install                    验收后正式安装
  mtga-cn rollback                   恢复最近安装／临时测试前状态
技术诊断：diagnose-base 默认保留 JSON；其他命令加 --details 可显示原始错误。
'''
