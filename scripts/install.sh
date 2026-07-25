#!/usr/bin/env bash
# 交互式安装脚本 — Kiro API Proxy
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

ask() {
    # ask <变量名> <提示> [默认值]
    local varname="$1" prompt="$2" default="${3:-}"
    local display_prompt
    if [[ -n "$default" ]]; then
        display_prompt="$prompt [${default}]: "
    else
        display_prompt="$prompt: "
    fi
    while true; do
        read -rp "$(echo -e "${BOLD}${display_prompt}${RESET}")" value
        value="${value:-$default}"
        if [[ -n "$value" ]]; then
            printf -v "$varname" '%s' "$value"
            return
        fi
        warn "不能为空，请重新输入。"
    done
}

ask_path() {
    # ask_path <变量名> <提示> [默认值]  — 展开 ~ 并转为绝对路径
    local varname="$1" prompt="$2" default="${3:-}"
    local raw expanded
    ask raw "$prompt" "$default"
    expanded="${raw/#\~/$HOME}"
    printf -v "$varname" '%s' "$(realpath -m "$expanded")"
}

confirm() {
    # confirm <提示>  — 返回 0=yes 1=no
    local ans
    read -rp "$(echo -e "${BOLD}$1 [y/N]: ${RESET}")" ans
    [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
}

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

# ── 收集安装参数 ──────────────────────────────────────────────────────────────
heading "安装目录"
echo "建议将安装目录与源码目录分开，例如 ~/kiro-api-proxy 或 /opt/kiro-api-proxy。"
ask_path INSTALL_DIR "安装目录" "$HOME/kiro-api-proxy"

if [[ -d "$INSTALL_DIR/venv" ]]; then
    warn "检测到已有安装：$INSTALL_DIR"
    confirm "覆盖现有安装？（将备份 config/ 目录）" || die "已取消。"
    REINSTALL=1
else
    REINSTALL=0
fi

heading "服务配置"
ask PORT "监听端口" "3458"
ask_path WORK_DIR "Kiro 工作目录（KIRO_WORKING_DIRECTORY）" "$HOME/Code"

heading "凭据配置"
CREDS_DST="$INSTALL_DIR/config/runtime-credentials.json"
echo "需要提供 runtime-credentials.json（含 refresh_token、client_id 等）。"
echo "可以："
echo "  1. 现在指定一个已有凭据文件的路径"
echo "  2. 安装完成后手动编辑 $CREDS_DST"
if confirm "现在指定已有凭据文件？"; then
    ask_path CREDS_SRC "凭据文件路径（JSON）" ""
    [[ -f "$CREDS_SRC" ]] || die "文件不存在：$CREDS_SRC"
else
    CREDS_SRC=""
fi

ACCT_INDEX=""
if [[ -n "$CREDS_SRC" ]]; then
    if python3 -c "
import json,sys
d=json.load(open('$CREDS_SRC'))
sys.exit(0 if isinstance(d,list) else 1)
" 2>/dev/null; then
        ask ACCT_INDEX "凭据文件是账户数组，请输入要使用的零基索引" "0"
    fi
fi

# ── 确认摘要 ──────────────────────────────────────────────────────────────────
heading "安装摘要"
echo "  平台           : $PLATFORM"
echo "  源码目录        : $SOURCE_DIR"
echo "  安装目录        : $INSTALL_DIR"
echo "  监听地址        : 127.0.0.1:$PORT"
echo "  工作目录        : $WORK_DIR"
echo "  凭据来源        : ${CREDS_SRC:-（安装后手动填写）}"
[[ -n "$ACCT_INDEX" ]] && echo "  账户索引        : $ACCT_INDEX"
echo ""
confirm "确认以上配置并开始安装？" || die "已取消。"

# ── 备份（仅重装时）──────────────────────────────────────────────────────────
if [[ "$REINSTALL" == "1" && -d "$INSTALL_DIR/config" ]]; then
    BACKUP="$INSTALL_DIR/config.bak.$(date +%Y%m%d-%H%M%S)"
    cp -Rp "$INSTALL_DIR/config" "$BACKUP"
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

# ── 创建目录结构 ──────────────────────────────────────────────────────────────
heading "创建目录"
mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/scripts"
info "目录就绪：$INSTALL_DIR"

# ── 创建 venv 并安装 ──────────────────────────────────────────────────────────
heading "安装 Python 包"
if [[ "$REINSTALL" == "1" ]]; then
    rm -rf "$INSTALL_DIR/venv"
fi
"$PYTHON" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet "$SOURCE_DIR"
info "已安装到 $INSTALL_DIR/venv"

# ── 生成 API 密钥 ─────────────────────────────────────────────────────────────
heading "生成代理 API 密钥"
KEY_FILE="$INSTALL_DIR/config/.env.proxy-api-key"
if [[ ! -f "$KEY_FILE" || "$REINSTALL" == "1" ]]; then
    "$INSTALL_DIR/venv/bin/python" -c \
        "import secrets,sys; open(sys.argv[1],'w').write(secrets.token_hex(32))" \
        "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    info "已生成密钥：$KEY_FILE"
else
    info "保留已有密钥：$KEY_FILE"
fi

# ── 写入 .env ─────────────────────────────────────────────────────────────────
heading "写入配置文件"
ENV_FILE="$INSTALL_DIR/config/.env"

# 仅当 .env 不存在或是重装时覆盖（重装时已备份）
if [[ ! -f "$ENV_FILE" || "$REINSTALL" == "1" ]]; then
    cat > "$ENV_FILE" <<EOF
PROXY_API_KEY_FILE=${KEY_FILE}
DEFAULT_MODEL=auto
REQUEST_TIMEOUT_SECONDS=600
KIRO_WORKING_DIRECTORY=${WORK_DIR}
KIRO_EFFORT=
RESPONSE_LANGUAGE=简体中文

MODEL_CACHE_ENABLED=true
MODEL_CACHE_TTL_SECONDS=300
MODEL_CACHE_STALE_SECONDS=3600

INCREMENTAL_STREAMING=true

RUNTIME_CREDENTIALS_FILE=${CREDS_DST}
RUNTIME_ACCOUNT_INDEX=${ACCT_INDEX}
RUNTIME_ENDPOINT=
EOF
    chmod 600 "$ENV_FILE"
    info "已写入 $ENV_FILE"
else
    info "保留已有 .env，如需更新请手动编辑：$ENV_FILE"
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
    <string>--env-file</string>
    <string>${ENV_FILE}</string>
    <string>kiro_api_proxy.main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>
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
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    info "已加载 launchd 服务"

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
EnvironmentFile=${ENV_FILE}
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

# ── 验证 ──────────────────────────────────────────────────────────────────────
heading "验证服务"

# 若凭据未配置，跳过健康检查
if grep -q 'YOUR_REFRESH_TOKEN_HERE' "$CREDS_DST" 2>/dev/null; then
    warn "凭据尚未填写，服务可能无法启动。"
    warn "请编辑 $CREDS_DST 后运行："
    if [[ "$PLATFORM" == "macos" ]]; then
        echo "  launchctl kickstart -k \"gui/\$(id -u)/com.fun90.kiro-api-proxy\""
    else
        echo "  systemctl --user restart kiro-api-proxy.service"
    fi
else
    echo "等待服务启动..."
    sleep 3
    if curl -fsS "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1; then
        info "服务正常：http://127.0.0.1:${PORT}/health"
    else
        warn "健康检查未通过，请查看日志："
        if [[ "$PLATFORM" == "macos" ]]; then
            echo "  tail -f $HOME/Library/Logs/kiro-api-proxy/stderr.log"
        else
            echo "  journalctl --user -u kiro-api-proxy -f"
        fi
    fi
fi

# ── 完成摘要 ──────────────────────────────────────────────────────────────────
heading "安装完成"
echo ""
echo "  管理界面    : http://127.0.0.1:${PORT}/admin/"
echo "  API 文档    : http://127.0.0.1:${PORT}/docs"
echo "  凭据文件    : $CREDS_DST"
echo "  配置文件    : $ENV_FILE"
echo "  API 密钥    : $(cat "$KEY_FILE")"
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
