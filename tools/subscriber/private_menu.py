"""Chinese menu for the independently installed private cloud receiver."""
import argparse
from pathlib import Path
from private_receiver import Receiver, DeliveryError

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state',type=Path,required=True)
    args=parser.parse_args()
    print('MTGA 私有云端更新（测试）\n候选仅来自指定私有仓库；应用前请完全退出 MTGA。')
    print('新游戏版本需要匹配候选；没有匹配时会停止，不覆盖游戏。')
    choices={'1':'status','2':'check','3':'preview','4':'update','5':'rollback'}
    while True:
        print('\n1 查看本入口状态  2 拉取并检查  3 预演  4 应用  5 恢复  6 帮助  0 退出')
        try:choice=input('请选择：').strip()
        except (EOFError,KeyboardInterrupt):return 0
        if choice=='0':return 0
        if choice=='6':
            print('2：下载私有数据包，核验成功构建、源码身份、摘要与签名，不修改游戏。\n'
                  '3：原生工具重建并核验五层候选，不修改游戏。\n'
                  '4：退出游戏后手动应用，先备份；任何未知文件变化都会停止。\n'
                  '5：恢复本入口最近一次安装前的状态，可离线操作。\n'
                  '旧 r3 与“MTGA 恢复r3试装前.command”仍由原入口管理。\n'
                  '当前为固定 r3 交付验证；全新版本资源自动定位尚未接通。\n'
                  '需要本机已有 Python 3、GitHub CLI 及其私有仓库登录。')
            continue
        if choice not in choices:print('请输入菜单中的数字。');continue
        receiver=None
        try:
            receiver=Receiver(args.state.resolve());receiver.execute(choices[choice])
        except (DeliveryError,ValueError,KeyError,OSError,TypeError) as error:
            print('操作已停止：'+str(error)[:3000])
        except KeyboardInterrupt:
            print('\n操作被中断；若在应用阶段，请先选择恢复。')
        finally:
            if receiver is not None:receiver.close()

if __name__=='__main__':raise SystemExit(main())
