#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="${REPO_OWNER:-biubiubiu125}"
REPO_NAME="${REPO_NAME:-gptimage2api}"
BRANCH="main"
INSTALL_DIR="${INSTALL_DIR:-/opt/gptimage2api}"
PORT="${GPTIMAGE2API_PORT:-${PORT:-3000}}"
THREAD_TOKENS="${GPTIMAGE2API_THREAD_TOKENS:-${THREAD_TOKENS:-120}}"
MODE="${MODE:-}"
AUTH_KEY="${GPTIMAGE2API_AUTH_KEY:-${AUTH_KEY:-}}"
DATABASE_MODE="postgres-local"
DATABASE_URL="${DATABASE_URL:-}"
IMAGE_QUEUE_DATABASE_URL="${GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL:-${IMAGE_QUEUE_DATABASE_URL:-}}"
POSTGRES_DB="gptimage2api_app"
POSTGRES_USER="${POSTGRES_USER:-gptimage2api}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-5432}"
INSTALL_LANG="${INSTALL_LANG:-}"
GPTIMAGE2API_IMAGE="${GPTIMAGE2API_IMAGE:-}"

if [[ -z "${UI_DEV:-}" ]]; then
  UI_DEV="/dev/tty"
  if [[ ! -r "${UI_DEV}" ]]; then
    UI_DEV="/dev/stdin"
  fi
fi

usage() {
  printf '%s\n\n' "$(text usage_title)"
  printf '%s\n' "$(text usage_usage)"
  cat <<'EOF'
  bash deploy/install.sh
  curl -fsSL https://raw.githubusercontent.com/biubiubiu125/gptimage2api/main/deploy/install.sh | sudo bash
EOF

  printf '\n%s\n' "$(text usage_env)"
  cat <<'EOF'
  INSTALL_DIR=/opt/gptimage2api
  PORT=3000
  GPTIMAGE2API_THREAD_TOKENS=120
  MODE=docker|python
  AUTH_KEY=your-auth-key
  POSTGRES_PASSWORD=generated-automatically
  INSTALL_LANG=zh|en
  GPTIMAGE2API_IMAGE=ghcr.io/biubiubiu125/gptimage2api:latest
EOF

  printf '\n%s\n' "$(text usage_flags)"
  cat <<'EOF'
  --mode docker|python
  --port 3000
  --thread-tokens 120
  --install-dir /opt/gptimage2api
  --auth-key your-auth-key
  --postgres-password your-postgres-password
  --repo-owner biubiubiu125
  --repo-name gptimage2api
  -h, --help
EOF
}

ui_print() {
  printf '%s' "$*" >"${UI_DEV}"
}

ui_println() {
  printf '%s\n' "$*" >"${UI_DEV}"
}

is_en() {
  [[ "${INSTALL_LANG}" =~ ^([Ee][Nn]|[Ee]nglish)$ ]]
}

normalize_language() {
  case "${INSTALL_LANG}" in
    en|EN|english|English|英文) INSTALL_LANG="en" ;;
    *) INSTALL_LANG="zh" ;;
  esac
}

echo_selected() {
  ui_println "$(text chosen_prefix)$*"
}

print_banner() {
  ui_println ""
  ui_println "========================================"
  ui_println "  GPTImage2API 安装向导 / Setup wizard"
  ui_println "========================================"
  ui_println "直接回车使用 [方括号] 默认值，并立刻显示已选择的结果。"
  ui_println "Press Enter to keep the default in [brackets]; the chosen value is printed immediately."
  ui_println ""
}

print_step() {
  local index="$1"
  local title="$2"
  local hint="$3"
  ui_println ""
  ui_println "----- $(text step_prefix) ${index}/6：${title} -----"
  ui_println "${hint}"
}

text() {
  local key="$1"
  if is_en; then
    case "${key}" in
      usage_title) printf 'GPTImage2API installer' ;;
      usage_usage) printf 'Usage:' ;;
      usage_env) printf 'Environment overrides:' ;;
      usage_flags) printf 'Flags:' ;;
      prefix_error) printf 'ERROR' ;;
      prefix_info) printf 'INFO' ;;
      prefix_warn) printf 'WARN' ;;
      prefix_done) printf 'OK' ;;
      wizard_title) printf 'setup wizard' ;;
      wizard_intro) printf 'Press Enter to keep the default shown in [brackets]. The chosen value is printed immediately.' ;;
      step_prefix) printf 'Step' ;;
      chosen_prefix) printf 'Selected: ' ;;
      step_language) printf 'Interface language' ;;
      hint_language) printf 'This language is used for the remaining installer prompts.' ;;
      step_mode) printf 'Run mode' ;;
      hint_mode) printf 'Docker starts the app and PostgreSQL 18 together. Python runs the app on the host and still starts PostgreSQL in Docker.' ;;
      label_mode_docker) printf 'Docker container (recommended)' ;;
      label_mode_python) printf 'Python source' ;;
      step_port) printf 'Web/API port' ;;
      hint_port) printf 'Browser and API clients will use this host port.' ;;
      step_thread_tokens) printf 'Backend sync concurrency' ;;
      hint_thread_tokens) printf 'Thread tokens for backend sync work. Image generation uses a separate queue limit.' ;;
      step_dir) printf 'Install directory' ;;
      hint_dir) printf 'Compose files, .env, and config.json are written here.' ;;
      step_auth) printf 'Admin login key' ;;
      hint_auth) printf 'Used to sign in to the console. Input is hidden. It cannot be empty, and you must type it twice.' ;;
      prompt_select) printf 'Select' ;;
      prompt_port) printf 'Web/API port' ;;
      prompt_thread_tokens) printf 'Backend sync concurrency (thread tokens)' ;;
      prompt_dir) printf 'Install directory' ;;
      prompt_auth) printf 'Admin login key' ;;
      prompt_auth_again) printf 'Type the admin login key again' ;;
      secret_saved) printf 'Saved. The key was not displayed.' ;;
      err_auth_empty) printf 'The admin login key cannot be empty. Type it, do not press Enter to skip.' ;;
      err_auth_mismatch) printf 'The two keys do not match. Try again.' ;;
      info_database) printf 'Database is fixed to a local PostgreSQL 18 container. SQLite and external URLs are not offered.' ;;
      label_database) printf 'PostgreSQL 18 local container' ;;
      label_branch) printf 'repository main (fixed)' ;;
      summary_title) printf 'Ready to install' ;;
      summary_language) printf 'Language' ;;
      summary_mode) printf 'Run mode' ;;
      summary_port) printf 'Port' ;;
      summary_tokens) printf 'Concurrency' ;;
      summary_dir) printf 'Directory' ;;
      summary_database) printf 'Database' ;;
      summary_git) printf 'Source' ;;
      summary_auth) printf 'Admin key' ;;
      summary_auth_set) printf 'set (hidden)' ;;
      confirm_install) printf 'Start installation now?' ;;
      confirm_yes) printf 'Yes, start installation' ;;
      confirm_no) printf 'Installation cancelled.' ;;
      label_lang_zh) printf 'Chinese' ;;
      label_lang_en) printf 'English' ;;
      err_missing_cmd) printf 'Missing command' ;;
      err_unknown_arg) printf 'Unknown argument' ;;
      err_mode) printf 'MODE must be docker or python.' ;;
      err_port) printf 'PORT must be a number.' ;;
      err_thread_tokens) printf 'GPTIMAGE2API_THREAD_TOKENS must be a positive number.' ;;
      err_postgres_password) printf 'POSTGRES_PASSWORD may only contain letters, numbers, underscores, and hyphens.' ;;
      err_frontend) printf 'npm is unavailable and no built frontend was found. Install Node.js/npm or provide web_dist/index.html.' ;;
      err_not_git) printf 'exists but is not a git repository.' ;;
      err_compose) printf 'docker compose plugin not found. Please install Docker Compose v2 first.' ;;
      err_postgres_ready) printf 'PostgreSQL container did not become ready.' ;;
      err_history_rewritten) printf 'Remote history was rewritten and cannot be fast-forwarded. Back up this directory, delete it, then rerun the installer.' ;;
      info_update) printf 'Updating' ;;
      info_clone) printf 'Cloning' ;;
      info_start_docker) printf 'Starting Docker service...' ;;
      info_start_postgres) printf 'Starting PostgreSQL 18 container...' ;;
      info_install_uv) printf 'uv not found, installing...' ;;
      warn_no_npm) printf 'npm not found, skipping frontend build. Existing web_dist will be used if present.' ;;
      info_build_vue) printf 'Building Vue console...' ;;
      info_install_py) printf 'Installing Python dependencies...' ;;
      info_start_app) printf 'Starting GPTImage2API on' ;;
      done_ready) printf 'GPTImage2API is ready' ;;
      done_auth) printf 'Admin auth key' ;;
      *) printf '%s' "${key}" ;;
    esac
    return
  fi

  case "${key}" in
    usage_title) printf 'GPTImage2API 安装脚本' ;;
    usage_usage) printf '用法：' ;;
    usage_env) printf '可用环境变量：' ;;
    usage_flags) printf '可用参数：' ;;
    prefix_error) printf '错误' ;;
    prefix_info) printf '信息' ;;
    prefix_warn) printf '警告' ;;
    prefix_done) printf '完成' ;;
    wizard_title) printf '安装向导' ;;
    wizard_intro) printf '直接回车即使用 [方括号] 里的默认值，并立刻显示已选择的结果。' ;;
    step_prefix) printf '步骤' ;;
    chosen_prefix) printf '已选择：' ;;
    step_language) printf '界面语言' ;;
    hint_language) printf '后续安装提示会使用这个语言。直接回车即中文。' ;;
    step_mode) printf '运行方式' ;;
    hint_mode) printf '推荐 Docker，会同时启动应用和 PostgreSQL 18。Python 源码在宿主机跑应用，数据库仍用 Docker 里的 PostgreSQL。' ;;
    label_mode_docker) printf 'Docker 容器（推荐）' ;;
    label_mode_python) printf 'Python 源码运行' ;;
    step_port) printf 'Web/API 端口' ;;
    hint_port) printf '浏览器打开控制台、调用 API 都走这个端口。' ;;
    step_thread_tokens) printf '后端同步并发' ;;
    hint_thread_tokens) printf '这是后端同步工作的线程令牌，不是图片队列并发。一般保持默认即可。' ;;
    step_dir) printf '安装目录' ;;
    hint_dir) printf 'Compose、.env 和 config.json 会写到这里。' ;;
    step_auth) printf '管理员登录密钥' ;;
    hint_auth) printf '用来登录管理控制台。输入时不显示。不能空回车，必须输入两次且一致。' ;;
    prompt_select) printf '请选择' ;;
    prompt_port) printf 'Web/API 端口' ;;
    prompt_thread_tokens) printf '后端同步并发容量（线程令牌）' ;;
    prompt_dir) printf '安装目录' ;;
    prompt_auth) printf '管理员登录密钥' ;;
    prompt_auth_again) printf '请再输入一次管理员登录密钥' ;;
    secret_saved) printf '已保存，输入内容未显示。' ;;
    err_auth_empty) printf '管理员登录密钥不能为空，不能直接回车跳过。' ;;
    err_auth_mismatch) printf '两次输入不一致，请重新输入。' ;;
    info_database) printf '数据库固定为 PostgreSQL 18 本地容器，不再提供 SQLite 或外部数据库地址。' ;;
    label_database) printf 'PostgreSQL 18 本地容器' ;;
    label_branch) printf '仓库 main（固定）' ;;
    summary_title) printf '即将安装' ;;
    summary_language) printf '界面语言' ;;
    summary_mode) printf '运行方式' ;;
    summary_port) printf '端口' ;;
    summary_tokens) printf '并发' ;;
    summary_dir) printf '安装目录' ;;
    summary_database) printf '数据库' ;;
    summary_git) printf '代码来源' ;;
    summary_auth) printf '管理员密钥' ;;
    summary_auth_set) printf '已设置（输入已隐藏）' ;;
    confirm_install) printf '确认开始安装？' ;;
    confirm_yes) printf '是，开始安装' ;;
    confirm_no) printf '已取消安装。' ;;
    label_lang_zh) printf '中文' ;;
    label_lang_en) printf 'English' ;;
    err_missing_cmd) printf '缺少命令' ;;
    err_unknown_arg) printf '未知参数' ;;
    err_mode) printf '运行模式只能是 docker 或 python。' ;;
    err_port) printf '端口必须是数字。' ;;
    err_thread_tokens) printf 'GPTIMAGE2API_THREAD_TOKENS 必须是正整数。' ;;
    err_postgres_password) printf 'POSTGRES_PASSWORD 只能包含字母、数字、下划线和连字符。' ;;
    err_frontend) printf '未找到 npm，且没有可用的前端构建结果。请安装 Node.js/npm，或提供 web_dist/index.html。' ;;
    err_not_git) printf '已存在，但不是 Git 仓库。' ;;
    err_compose) printf '未找到 docker compose 插件，请先安装 Docker Compose v2。' ;;
    err_postgres_ready) printf 'PostgreSQL 容器未在预期时间内就绪。' ;;
    err_history_rewritten) printf '远程仓库历史已改写，无法快进更新。请先备份后删除该安装目录，再重新运行安装脚本。' ;;
    info_update) printf '正在更新' ;;
    info_clone) printf '正在克隆' ;;
    info_start_docker) printf '正在启动 Docker 服务...' ;;
    info_start_postgres) printf '正在启动 PostgreSQL 18 容器...' ;;
    info_install_uv) printf '未找到 uv，正在安装...' ;;
    warn_no_npm) printf '未找到 npm，跳过前端构建；如果已有 web_dist，将继续使用现有文件。' ;;
    info_build_vue) printf '正在构建 Vue 控制台...' ;;
    info_install_py) printf '正在安装 Python 依赖...' ;;
    info_start_app) printf '正在启动 GPTImage2API' ;;
    done_ready) printf 'GPTImage2API 已就绪' ;;
    done_auth) printf '管理员登录密钥' ;;
    *) printf '%s' "${key}" ;;
  esac
}

prompt_input() {
  local label="$1"
  local default="${2-}"
  local echo_choice="${3:-1}"
  local answer=""

  if [[ -n "${default}" ]]; then
    ui_print "${label} [${default}]: "
  else
    ui_print "${label}: "
  fi

  IFS= read -r answer <"${UI_DEV}" || true
  if [[ -z "${answer}" ]]; then
    answer="${default}"
  fi
  if [[ "${echo_choice}" == "1" ]]; then
    echo_selected "${answer}"
  fi
  printf '%s' "${answer}"
}

prompt_secret_confirmed() {
  local first=""
  local second=""

  while true; do
    ui_print "$(text prompt_auth): "
    IFS= read -r -s first <"${UI_DEV}" || true
    ui_println ""
    if [[ -z "${first//[[:space:]]/}" ]]; then
      ui_println "[$(text prefix_error)] $(text err_auth_empty)"
      continue
    fi
    ui_print "$(text prompt_auth_again): "
    IFS= read -r -s second <"${UI_DEV}" || true
    ui_println ""
    if [[ "${first}" != "${second}" ]]; then
      ui_println "[$(text prefix_error)] $(text err_auth_mismatch)"
      continue
    fi
    ui_println "$(text secret_saved)"
    printf '%s' "${first}"
    return
  done
}

confirm_start() {
  local answer=""
  ui_print "$(text confirm_install) [Y]: "
  IFS= read -r answer <"${UI_DEV}" || true
  if [[ -z "${answer}" || "${answer}" =~ ^([Yy]|yes|YES)$ ]]; then
    echo_selected "$(text confirm_yes)"
    return 0
  fi
  ui_println "$(text confirm_no)"
  return 1
}

normalize_mode_choice() {
  local value="${1:-}"
  value="${value,,}"
  value="${value//[[:space:]]/}"
  case "${value}" in
    1|d|docker) printf 'docker' ;;
    2|p|py|python) printf 'python' ;;
    *) return 1 ;;
  esac
}

mode_label() {
  case "$1" in
    python) text label_mode_python ;;
    *) text label_mode_docker ;;
  esac
}

language_label() {
  if is_en; then
    text label_lang_en
  else
    text label_lang_zh
  fi
}

choose_language() {
  if [[ -n "${INSTALL_LANG}" ]]; then
    normalize_language
    echo_selected "$(language_label)"
    return
  fi

  local answer=""
  print_step "1" "$(text step_language)" "$(text hint_language)"
  ui_println "  1) 中文（默认）"
  ui_println "  2) English"
  answer="$(prompt_input "请选择 / Select" "1" "0")"
  case "${answer}" in
    2|en|EN|english|English) INSTALL_LANG="en" ;;
    *) INSTALL_LANG="zh" ;;
  esac
  normalize_language
  echo_selected "$(language_label)"
}

prompt_mode_choice() {
  local default="${1:-docker}"
  local normalized=""
  local answer=""
  normalized="$(normalize_mode_choice "${default}")" || normalized="docker"
  local default_choice="1"
  [[ "${normalized}" == "python" ]] && default_choice="2"

  print_step "2" "$(text step_mode)" "$(text hint_mode)"
  while true; do
    ui_println "  1) $(text label_mode_docker)"
    ui_println "  2) $(text label_mode_python)"
    answer="$(prompt_input "$(text prompt_select)" "${default_choice}" "0")"
    if normalized="$(normalize_mode_choice "${answer}")"; then
      echo_selected "$(mode_label "${normalized}")"
      printf '%s' "${normalized}"
      return
    fi
    ui_println "[$(text prefix_error)] $(text err_mode)"
  done
}

read_existing_env_value() {
  local key="$1"
  local env_file="${INSTALL_DIR}/.env"
  local value=""
  [[ -f "${env_file}" ]] || return 0
  value="$(sed -n "s/^${key}=//p" "${env_file}" | tail -n 1)"
  if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
    value="${value:1:${#value}-2}"
    value="${value//\\\"/\"}"
    value="${value//\\\\/\\}"
    value="${value//\$\$/\$}"
  fi
  printf '%s' "${value}"
}

configure_database() {
  local existing_user=""
  DATABASE_MODE="postgres-local"
  ui_println ""
  ui_println "$(text info_database)"
  echo_selected "$(text label_database)"

  existing_user="$(read_existing_env_value POSTGRES_USER)"
  if [[ -n "${existing_user}" ]]; then
    POSTGRES_USER="${existing_user}"
  fi
  if [[ -z "${POSTGRES_PASSWORD}" ]]; then
    POSTGRES_PASSWORD="$(read_existing_env_value POSTGRES_PASSWORD)"
  fi
  if [[ -z "${POSTGRES_PASSWORD}" ]]; then
    POSTGRES_PASSWORD="$(generate_secret)"
  fi

  if [[ "${MODE}" == "python" ]]; then
    DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_HOST_PORT}/${POSTGRES_DB}"
    IMAGE_QUEUE_DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_HOST_PORT}/gptimage2api_image_queue"
  else
    DATABASE_URL=""
    IMAGE_QUEUE_DATABASE_URL=""
  fi
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[$(text prefix_error)] $(text err_missing_cmd): $1" >&2
    exit 1
  fi
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
    return
  fi
  if [[ -r /proc/sys/kernel/random/uuid ]]; then
    tr -d '-' </proc/sys/kernel/random/uuid
    return
  fi
  date +%s%N
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        usage
        exit 0
        ;;
      --mode)
        MODE="${2:-}"
        shift 2
        ;;
      --port)
        PORT="${2:-}"
        shift 2
        ;;
      --thread-tokens)
        THREAD_TOKENS="${2:-}"
        shift 2
        ;;
      --install-dir)
        INSTALL_DIR="${2:-}"
        shift 2
        ;;
      --auth-key)
        AUTH_KEY="${2:-}"
        shift 2
        ;;
      --postgres-password)
        POSTGRES_PASSWORD="${2:-}"
        shift 2
        ;;
      --repo-owner)
        REPO_OWNER="${2:-}"
        shift 2
        ;;
      --repo-name)
        REPO_NAME="${2:-}"
        shift 2
        ;;
      *)
        echo "[$(text prefix_error)] $(text err_unknown_arg): $1" >&2
        usage >&2
        exit 1
        ;;
    esac
  done
}

validate_inputs() {
  local normalized=""

  normalized="$(normalize_mode_choice "${MODE}")" || { echo "[$(text prefix_error)] $(text err_mode)" >&2; exit 1; }
  MODE="${normalized}"

  if [[ -z "${PORT}" || ! "${PORT}" =~ ^[0-9]+$ ]]; then
    echo "[$(text prefix_error)] $(text err_port)" >&2
    exit 1
  fi

  if [[ -z "${THREAD_TOKENS}" || ! "${THREAD_TOKENS}" =~ ^[0-9]+$ || "${THREAD_TOKENS}" -lt 1 ]]; then
    echo "[$(text prefix_error)] $(text err_thread_tokens)" >&2
    exit 1
  fi

  if [[ -z "${AUTH_KEY//[[:space:]]/}" ]]; then
    echo "[$(text prefix_error)] $(text err_auth_empty)" >&2
    exit 1
  fi

  if [[ ! "${POSTGRES_PASSWORD}" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "[$(text prefix_error)] $(text err_postgres_password)" >&2
    exit 1
  fi
}

print_summary() {
  ui_println ""
  ui_println "$(text summary_title)"
  ui_println "  $(text summary_language): $(language_label)"
  ui_println "  $(text summary_mode): $(mode_label "${MODE}")"
  ui_println "  $(text summary_port): ${PORT}"
  ui_println "  $(text summary_tokens): ${THREAD_TOKENS}"
  ui_println "  $(text summary_dir): ${INSTALL_DIR}"
  ui_println "  $(text summary_database): $(text label_database)"
  ui_println "  $(text summary_git): $(text label_branch)"
  ui_println "  $(text summary_auth): $(text summary_auth_set)"
  ui_println ""
}

repo_url() {
  printf 'https://github.com/%s/%s.git' "${REPO_OWNER}" "${REPO_NAME}"
}

default_image() {
  if [[ -n "${GPTIMAGE2API_IMAGE}" ]]; then
    printf '%s' "${GPTIMAGE2API_IMAGE}"
    return
  fi
  printf 'ghcr.io/%s/%s:latest' "${REPO_OWNER}" "${REPO_NAME}"
}

raw_url() {
  printf 'https://raw.githubusercontent.com/%s/%s/%s/%s' "${REPO_OWNER}" "${REPO_NAME}" "${BRANCH}" "$1"
}

download_file() {
  local source_path="$1"
  local target_path="${INSTALL_DIR}/${source_path}"

  mkdir -p "$(dirname "${target_path}")"
  curl -fsSL "$(raw_url "${source_path}")" -o "${target_path}"
}

json_escape() {
  local value="${1-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "${value}"
}

dotenv_quote() {
  local value="${1-}"
  if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    echo "[$(text prefix_error)] dotenv values must not contain newlines." >&2
    return 1
  fi
  value="${value//\\/\\\\}"
  value="${value//\$/\$\$}"
  value="${value//\"/\\\"}"
  printf '"%s"' "${value}"
}

write_default_config_json() {
  local config_file="${INSTALL_DIR}/config.json"
  local tmp_file="${config_file}.tmp"

  if [[ -f "${config_file}" ]]; then
    return
  fi
  if [[ -e "${config_file}" ]]; then
    echo "[$(text prefix_error)] ${config_file} exists but is not a regular file." >&2
    exit 1
  fi

  cat >"${tmp_file}" <<EOF
{
  "auth-key": "$(json_escape "${AUTH_KEY}")"
}
EOF

  mv "${tmp_file}" "${config_file}"
  chmod 600 "${config_file}" || true
}

prepare_docker_bundle() {
  need_cmd curl

  mkdir -p "${INSTALL_DIR}"
  download_file "docker-compose.yml"
  download_file "docker-compose.postgres.yml"
  download_file "deploy/postgres-init/01-create-image-queue.sql"
}

prepare_repo() {
  need_cmd git

  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    ui_println "[$(text prefix_info)] $(text info_update) ${INSTALL_DIR}"
    (cd "${INSTALL_DIR}" && git fetch origin "${BRANCH}")
    (cd "${INSTALL_DIR}" && git checkout "${BRANCH}" >/dev/null 2>&1) || (cd "${INSTALL_DIR}" && git checkout -b "${BRANCH}" "origin/${BRANCH}")
    if (cd "${INSTALL_DIR}" && git ls-remote --exit-code --heads origin "${BRANCH}" >/dev/null 2>&1); then
      if ! (cd "${INSTALL_DIR}" && git merge --ff-only "origin/${BRANCH}" >/dev/null 2>&1); then
        echo "[$(text prefix_error)] ${INSTALL_DIR} $(text err_history_rewritten)" >&2
        exit 1
      fi
    fi
    return
  fi

  if [[ -e "${INSTALL_DIR}" && -n "$(find "${INSTALL_DIR}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    echo "[$(text prefix_error)] ${INSTALL_DIR} $(text err_not_git)" >&2
    exit 1
  fi

  mkdir -p "$(dirname "${INSTALL_DIR}")"
  ui_println "[$(text prefix_info)] $(text info_clone) $(repo_url) -> ${INSTALL_DIR}"
  git clone --branch "${BRANCH}" --depth 1 "$(repo_url)" "${INSTALL_DIR}"
}

write_env_file() {
  local env_file="${INSTALL_DIR}/.env"
  local tmp_file="${env_file}.tmp"

  cat >"${tmp_file}" <<EOF
GPTIMAGE2API_AUTH_KEY=$(dotenv_quote "${AUTH_KEY}")
GPTIMAGE2API_PORT=$(dotenv_quote "${PORT}")
GPTIMAGE2API_THREAD_TOKENS=$(dotenv_quote "${THREAD_TOKENS}")
GPTIMAGE2API_IMAGE=$(dotenv_quote "$(default_image)")
GPTIMAGE2API_BASE_URL=$(dotenv_quote "")
GPTIMAGE2API_GITHUB_REPOSITORY=$(dotenv_quote "${REPO_OWNER}/${REPO_NAME}")
DATABASE_URL=$(dotenv_quote "${DATABASE_URL}")
GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL=$(dotenv_quote "${IMAGE_QUEUE_DATABASE_URL}")
GPTIMAGE2API_IMAGE_QUEUE_ARTIFACT_ROOT=$(dotenv_quote "data/images")
GPTIMAGE2API_IMAGE_QUEUE_GENERATION_CONCURRENCY=$(dotenv_quote "4")
GPTIMAGE2API_IMAGE_QUEUE_MAX_BACKLOG=$(dotenv_quote "256")

DATABASE_MODE=$(dotenv_quote "${DATABASE_MODE}")
POSTGRES_DB=$(dotenv_quote "${POSTGRES_DB}")
POSTGRES_USER=$(dotenv_quote "${POSTGRES_USER}")
POSTGRES_PASSWORD=$(dotenv_quote "${POSTGRES_PASSWORD}")
POSTGRES_HOST_PORT=$(dotenv_quote "${POSTGRES_HOST_PORT}")
TZ=$(dotenv_quote "Asia/Shanghai")
EOF

  mv "${tmp_file}" "${env_file}"
  chmod 600 "${env_file}" || true
}

require_compose() {
  need_cmd docker
  if ! docker compose version >/dev/null 2>&1; then
    echo "[$(text prefix_error)] $(text err_compose)" >&2
    exit 1
  fi
}

run_docker() {
  require_compose
  ui_println "[$(text prefix_info)] $(text info_start_docker)"
  (cd "${INSTALL_DIR}" && docker compose -f docker-compose.yml -f docker-compose.postgres.yml pull)
  (cd "${INSTALL_DIR}" && docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d)
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  need_cmd curl
  ui_println "[$(text prefix_info)] $(text info_install_uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  need_cmd uv
}

build_frontend() {
  if ! command -v npm >/dev/null 2>&1; then
    if [[ ! -f "${INSTALL_DIR}/web_dist/index.html" && ! -f "${INSTALL_DIR}/web-vue/dist/index.html" ]]; then
      echo "[$(text prefix_error)] $(text err_frontend)" >&2
      exit 1
    fi
    ui_println "[$(text prefix_warn)] $(text warn_no_npm)"
    return
  fi

  ui_println "[$(text prefix_info)] $(text info_build_vue)"
  (cd "${INSTALL_DIR}/web-vue" && npm ci && npm run build)
  if [[ ! -f "${INSTALL_DIR}/web-vue/dist/index.html" ]]; then
    echo "[$(text prefix_error)] $(text err_frontend)" >&2
    exit 1
  fi
  rm -rf "${INSTALL_DIR}/web_dist"
  mkdir -p "${INSTALL_DIR}/web_dist"
  cp -R "${INSTALL_DIR}/web-vue/dist/." "${INSTALL_DIR}/web_dist/"
}

wait_for_postgres() {
  local attempt=0
  while [[ "${attempt}" -lt 30 ]]; do
    if (cd "${INSTALL_DIR}" && docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.host-postgres.yml exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1); then
      return 0
    fi
    attempt="$((attempt + 1))"
    sleep 2
  done
  echo "[$(text prefix_error)] $(text err_postgres_ready)" >&2
  exit 1
}

start_host_postgres() {
  require_compose
  ui_println "[$(text prefix_info)] $(text info_start_postgres)"
  (cd "${INSTALL_DIR}" && docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.host-postgres.yml up -d postgres image-queue-init)
  wait_for_postgres
}

run_python() {
  start_host_postgres
  ensure_uv
  build_frontend
  ui_println "[$(text prefix_info)] $(text info_install_py)"
  (cd "${INSTALL_DIR}" && uv sync)

  ui_println "[$(text prefix_info)] $(text info_start_app) http://localhost:${PORT}"
  cd "${INSTALL_DIR}"
  export GPTIMAGE2API_AUTH_KEY="${AUTH_KEY}"
  export GPTIMAGE2API_THREAD_TOKENS="${THREAD_TOKENS}"
  export DATABASE_URL="${DATABASE_URL}"
  export GPTIMAGE2API_IMAGE_QUEUE_DATABASE_URL="${IMAGE_QUEUE_DATABASE_URL}"
  export GPTIMAGE2API_IMAGE_QUEUE_ARTIFACT_ROOT="data/images"
  export GPTIMAGE2API_IMAGE_QUEUE_GENERATION_CONCURRENCY="4"
  export GPTIMAGE2API_IMAGE_QUEUE_MAX_BACKLOG="256"
  export GPTIMAGE2API_GITHUB_REPOSITORY="${REPO_OWNER}/${REPO_NAME}"
  print_ready
  exec uv run uvicorn main:app --host 0.0.0.0 --port "${PORT}"
}

mask_secret() {
  local value="${1-}"
  if [[ ${#value} -le 8 ]]; then
    printf '****'
  else
    printf '%s****%s' "${value:0:4}" "${value: -4}"
  fi
}

print_ready() {
  ui_println ""
  ui_println "[$(text prefix_done)] $(text done_ready): http://localhost:${PORT}"
  ui_println "[$(text prefix_done)] $(text done_auth): $(mask_secret "${AUTH_KEY}") (saved in .env and config.json)"
}

main() {
  parse_args "$@"
  print_banner
  choose_language

  if [[ -z "${MODE}" ]]; then
    MODE="$(prompt_mode_choice "docker")"
  else
    MODE="$(normalize_mode_choice "${MODE}")" || { echo "[$(text prefix_error)] $(text err_mode)" >&2; exit 1; }
    echo_selected "$(mode_label "${MODE}")"
  fi

  print_step "3" "$(text step_port)" "$(text hint_port)"
  while true; do
    PORT="$(prompt_input "$(text prompt_port)" "${PORT}" "0")"
    if [[ -n "${PORT}" && "${PORT}" =~ ^[0-9]+$ ]]; then
      echo_selected "${PORT}"
      break
    fi
    ui_println "[$(text prefix_error)] $(text err_port)"
  done
  print_step "4" "$(text step_thread_tokens)" "$(text hint_thread_tokens)"
  while true; do
    THREAD_TOKENS="$(prompt_input "$(text prompt_thread_tokens)" "${THREAD_TOKENS}" "0")"
    if [[ -n "${THREAD_TOKENS}" && "${THREAD_TOKENS}" =~ ^[0-9]+$ && "${THREAD_TOKENS}" -ge 1 ]]; then
      echo_selected "${THREAD_TOKENS}"
      break
    fi
    ui_println "[$(text prefix_error)] $(text err_thread_tokens)"
  done
  print_step "5" "$(text step_dir)" "$(text hint_dir)"
  INSTALL_DIR="$(prompt_input "$(text prompt_dir)" "${INSTALL_DIR}")"
  configure_database

  print_step "6" "$(text step_auth)" "$(text hint_auth)"
  if [[ -z "${AUTH_KEY}" || "${AUTH_KEY}" == "your_secret_key_here" ]]; then
    AUTH_KEY="$(prompt_secret_confirmed)"
  else
    echo_selected "$(text summary_auth_set)"
  fi

  validate_inputs
  print_summary
  if ! confirm_start; then
    exit 0
  fi
  if [[ "${MODE}" == "docker" ]]; then
    prepare_docker_bundle
  else
    prepare_repo
  fi
  write_default_config_json
  write_env_file

  if [[ "${MODE}" == "docker" ]]; then
    run_docker
    print_ready
  else
    run_python
  fi
}

if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]:-}" == "$0" ]]; then
  main "$@"
fi
