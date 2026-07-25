#!/usr/bin/env bash
# 无交互安装脚本 — Kiro API Proxy
# 支持 macOS (launchd) 和 Linux (systemd --user)
set -euo pipefail

# ── 颜色 ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${GREEN}✔${RESET} $*"; }
warn()    { echo -e "${YELLOW}!${RESET} $*"; }
err()     { echo -e "${RED}✘${RESET} $*" >&2; }
heading() { echo -e "\n${BOLD}$*${RESET}"; }
die()     { err "$*"; exit 1; }

# 本脚本全程无交互，所有安装参数取固定默认值（见下方“确定安装参数”）。

# ── 检测平台 ─────────────────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM=macos ;;
    Linux)  PLATFORM=linux ;;
    *)      die "不支持的平台：$OS（仅支持 macOS 和 Linux）" ;;
esac

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 检查前置条件 ──────────────────────────────────────────────────────────────
heading "检查前置条件"

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if cmd="$(command -v "$candidate" 2>/dev/null)"; then
        ver="$("$cmd" -c 'import sys; print(sys.version_info[:2])')"
        if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PYTHON="$cmd"
            info "找到 Python：$PYTHON ($ver)"
            break
        fi
    fi
done
[[ -n "$PYTHON" ]] || die "未找到 Python 3.11+，请先安装。"

if [[ -f "$SOURCE_DIR/pyproject.toml" ]]; then
    info "源码目录：$SOURCE_DIR"
else
    die "未找到 pyproject.toml，请在项目根目录或 scripts/ 子目录中运行此脚本。"
fi

# ── 确定安装参数（无交互，全部固定默认值）─────────────────────────────────────
# 安装目录固定为当前目录；配置目录、凭据文件均在其下的 .config/。
INSTALL_DIR="$(pwd)"
PORT=3458
# 工作目录根放开为整个文件系统（该值仅作为可传给上游的路径白名单，非真实文件权限）。
WORK_DIR="/"
# 不在安装期指定凭据，安装后经 /admin 登录或手动编辑 runtime-credentials.json。
CREDS_SRC=""
ACCT_INDEX=""
CREDS_DST="$INSTALL_DIR/.config/runtime-credentials.json"

# 已有安装则自动备份 .config/ 后覆盖（不再询问）。
if [[ -d "$INSTALL_DIR/venv" ]]; then
    warn "检测到已有安装，将备份 .config/ 后覆盖：$INSTALL_DIR"
    REINSTALL=1
else
    REINSTALL=0
fi

heading "安装摘要"
echo "  平台           : $PLATFORM"
echo "  源码目录        : $SOURCE_DIR"
echo "  安装目录        : $INSTALL_DIR"
echo "  监听地址        : 127.0.0.1:$PORT"
echo "  工作目录        : $WORK_DIR"
echo "  凭据来源        : （安装后手动填写或经 /admin 登录）"
echo ""

# ── 备份（仅重装时）──────────────────────────────────────────────────────────
if [[ "$REINSTALL" == "1" && -d "$INSTALL_DIR/.config" ]]; then
    BACKUP="$INSTALL_DIR/.config.bak.$(date +%Y%m%d-%H%M%S)"
    cp -Rp "$INSTALL_DIR/.config" "$BACKUP"
    info "已备份配置到 $BACKUP"
fi

# ── 停止旧服务 ────────────────────────────────────────────────────────────────
if [[ "$REINSTALL" == "1" ]]; then
    heading "停止旧服务"
    if [[ "$PLATFORM" == "macos" ]]; then
        launchctl bootout "gui/$(id -u)/com.fun90.kiro-api-proxy" 2>/dev/null && info "已停止 launchd 服务" || true
    else
        systemctl --user stop kiro-api-proxy.service 2>/dev/null && info "已停止 systemd 服务" || true
    fi
fi

# ── 创建 venv 并安装 ──────────────────────────────────────────────────────────
heading "安装 Python 包"
if [[ "$REINSTALL" == "1" ]]; then
    rm -rf "$INSTALL_DIR/venv"
fi
"$PYTHON" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet "$SOURCE_DIR"
info "已安装到 $INSTALL_DIR/venv"

# ── 写入配置文件 ──────────────────────────────────────────────────────────────
heading "写入配置文件"
CONFIG_FILE="$INSTALL_DIR/.config/config.json"

# 仅当 config.json 不存在或是重装时写入（重装时已备份）。api_key 默认为空、
# 不再预生成，安装后到 /admin 生成并保存；配置目录与工作目录通过服务定义的
# 环境变量传入，无需 .env。
if [[ ! -f "$CONFIG_FILE" || "$REINSTALL" == "1" ]]; then
    "$INSTALL_DIR/venv/bin/python" - "$CONFIG_FILE" "$PORT" "$ACCT_INDEX" <<'PY'
import json, stat, sys
from pathlib import Path

# 凭据路径固定为 config.json 同目录下的 runtime-credentials.json，不写入配置。
# argv[3]（账户索引）可能缺省：用 len 守卫，避免空参在某些 shell 下丢失时越界。
path, port = sys.argv[1], int(sys.argv[2])
acct = sys.argv[3] if len(sys.argv) > 3 else ""
data = {
    "api_host": "127.0.0.1",
    "api_port": port,
}
if acct.strip():
    data["runtime_account_index"] = int(acct)
p = Path(path)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
p.chmod(stat.S_IRUSR | stat.S_IWUSR)
PY
    info "已写入 $CONFIG_FILE"
else
    info "保留已有配置：$CONFIG_FILE"
fi

# ── 凭据文件 ──────────────────────────────────────────────────────────────────
if [[ -n "$CREDS_SRC" ]]; then
    install -m 600 "$CREDS_SRC" "$CREDS_DST"
    info "已复制凭据到 $CREDS_DST"
else
    if [[ ! -f "$CREDS_DST" ]]; then
        install -m 600 "$SOURCE_DIR/credentials.example.json" "$CREDS_DST"
        warn "已复制凭据示例，请在启动服务前编辑：$CREDS_DST"
    else
        info "保留已有凭据：$CREDS_DST"
    fi
fi

# ── 注册系统服务 ──────────────────────────────────────────────────────────────
heading "注册系统服务"

if [[ "$PLATFORM" == "macos" ]]; then
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST="$PLIST_DIR/com.fun90.kiro-api-proxy.plist"
    LOG_DIR="$HOME/Library/Logs/kiro-api-proxy"
    mkdir -p "$PLIST_DIR" "$LOG_DIR"

    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.fun90.kiro-api-proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>${INSTALL_DIR}/venv/bin/uvicorn</string>
    <string>kiro_api_proxy.main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>KIRO_PROXY_CONFIG_DIRECTORY</key>
    <string>${INSTALL_DIR}/.config</string>
    <key>KIRO_WORKING_DIRECTORY</key>
    <string>${WORK_DIR}</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>${INSTALL_DIR}</string>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/stderr.log</string>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>5</integer>
</dict>
</plist>
EOF
    # launchd 幂等注册：bootout 是异步的，若紧接 bootstrap 撞上尚未卸载完的
    # 同名服务，会报 "5: Input/output error"。故先无条件卸载并轮询确认从
    # domain 移除，再对 bootstrap 做重试，最后用 kickstart 兜底。
    domain="gui/$(id -u)"
    service="$domain/com.fun90.kiro-api-proxy"
    launchctl bootout "$service" 2>/dev/null || true
    for _ in $(seq 1 20); do
        launchctl print "$service" >/dev/null 2>&1 || break
        sleep 0.5
    done
    loaded=0
    for _ in $(seq 1 10); do
        if launchctl bootstrap "$domain" "$PLIST" 2>/dev/null; then
            loaded=1
            break
        fi
        sleep 1
    done
    if [[ "$loaded" == "1" ]]; then
        info "已加载 launchd 服务"
    elif launchctl kickstart -k "$service" 2>/dev/null; then
        info "已重启 launchd 服务"
    else
        die "launchd 服务加载失败，请手动检查：launchctl print $service"
    fi

elif [[ "$PLATFORM" == "linux" ]]; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_DIR/kiro-api-proxy.service" <<EOF
[Unit]
Description=Kiro OpenAI 与 Anthropic 兼容 API 代理
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=KIRO_PROXY_CONFIG_DIRECTORY=${INSTALL_DIR}/.config
Environment=KIRO_WORKING_DIRECTORY=${WORK_DIR}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn kiro_api_proxy.main:app --host 127.0.0.1 --port ${PORT}
Restart=on-failure
RestartSec=2
KillMode=control-group
TimeoutStopSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now kiro-api-proxy.service
    info "已启用 systemd 用户服务"
fi

# ── 完成摘要 ──────────────────────────────────────────────────────────────────
heading "安装完成"
echo ""
echo "  管理界面    : http://127.0.0.1:${PORT}/admin/"
echo "  API 文档    : http://127.0.0.1:${PORT}/docs"
echo "  凭据文件    : $CREDS_DST"
echo "  配置文件    : $CONFIG_FILE"
echo "  API 密钥    : 未设置，请访问管理界面在“设置”页生成并保存"
echo ""

if [[ "$PLATFORM" == "macos" ]]; then
    echo "  重启服务    : launchctl kickstart -k \"gui/\$(id -u)/com.fun90.kiro-api-proxy\""
    echo "  停止服务    : launchctl bootout \"gui/\$(id -u)/com.fun90.kiro-api-proxy\""
else
    echo "  重启服务    : systemctl --user restart kiro-api-proxy.service"
    echo "  停止服务    : systemctl --user stop kiro-api-proxy.service"
    echo "  查看日志    : journalctl --user -u kiro-api-proxy -f"
fi
echo ""
