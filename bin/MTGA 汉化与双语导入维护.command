#!/bin/zsh
# Thin menu over the existing guarded CLI; no maintenance happens on launch.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MTGA_CLI="$SCRIPT_DIR/mtga-cn"
if [[ ! -x "$MTGA_CLI" ]]; then
  print '找不到同目录的 MTGA 维护器，请从完整仓库的 bin 目录打开。'
  read -r '?按回车关闭。'
  exit 1
fi
if (( $# )); then
  exec "$MTGA_CLI" "$@"
fi
while true; do
  print '\nMTGA 中文汉化与双语导入维护'
  print '1  查看状态／最近一次失败原因（不改游戏）'
  print '2  检查新版官方底稿与备份（只读，更新后先选此项）'
  print '3  构建并验证汉化候选（不安装，须先通过底稿检查）'
  print '4  正式安装／保留（须已记录游戏内验收，先退出游戏）'
  print '5  恢复最近安装／临时测试前状态（会改游戏，先退出游戏）'
  print '6  阅读中文操作指南／临时测试与验收说明'
  print '0  退出'
  print '\n更新后的顺序：Steam 与游戏内下载完成 → 退出游戏 → 2 → 3 → 临时测试并记录验收 → 4。'
  print '提示缺少新版底稿时，需要维护者适配；重复按 3 无效。临时测试步骤见 6。'
  read -r 'MTGA_MENU_CHOICE?请选择：' || exit 0
  case "$MTGA_MENU_CHOICE" in
    1) "$MTGA_CLI" status ;;
    2) "$MTGA_CLI" diagnose-base --human ;;
    3) print '开始构建候选：只写维护工作目录，不安装到游戏。'; "$MTGA_CLI" update ;;
    4) print '正在检查安装条件：游戏须退出，且当前候选的真实验收已记录通过。'; "$MTGA_CLI" install ;;
    5) print '回滚用于恢复最近安装／临时测试前的文件；不能撤销 Steam 更新。'; "$MTGA_CLI" rollback ;;
    6) "$MTGA_CLI" guide ;;
    0) exit 0 ;;
    *) print '请输入 0 到 6。'; continue ;;
  esac
  MTGA_MENU_RESULT=$?
  if (( MTGA_MENU_RESULT != 0 )); then
    print "\n操作未完成（退出码 $MTGA_MENU_RESULT）。请按上方中文原因与下一步处理。"
    print '可按 1 查看状态、按 6 查看指南；构建失败无需为此回滚。'
  fi
  read -r '?按回车返回菜单。' || exit 0
done
