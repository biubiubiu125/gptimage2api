#!/bin/sh
set -eu

seed_root=/opt/gptimage2api
runtime_root=/app
marker_name=.gptimage2api-image-version
seed_version="$(tr -d '\r\n' < "${seed_root}/VERSION")"
installed_image_version=""

if [ -f "${runtime_root}/${marker_name}" ]; then
  installed_image_version="$(tr -d '\r\n' < "${runtime_root}/${marker_name}")"
fi

if [ ! -f "${runtime_root}/VERSION" ] || [ "${installed_image_version}" != "${seed_version}" ]; then
  mkdir -p "${runtime_root}"
  rm -rf "${runtime_root}/.venv"
  find "${runtime_root}" -mindepth 1 -maxdepth 1 \
    ! -name data \
    ! -name config.json \
    ! -name .venv \
    ! -name "${marker_name}" \
    -exec rm -rf -- {} +

  for source in "${seed_root}"/* "${seed_root}"/.[!.]* "${seed_root}"/..?*; do
    [ -e "${source}" ] || continue
    [ "$(basename "${source}")" = ".venv" ] && continue
    cp -a "${source}" "${runtime_root}/"
  done
  cp -a "${seed_root}/.venv" "${runtime_root}/.venv"

  marker_tmp="${runtime_root}/${marker_name}.tmp"
  printf '%s\n' "${seed_version}" > "${marker_tmp}"
  mv "${marker_tmp}" "${runtime_root}/${marker_name}"
fi

if [ ! -x "${seed_root}/.venv/bin/python" ]; then
  echo "gptimage2api: bundled Python environment is missing from the image" >&2
  exit 1
fi
if [ ! -x "${runtime_root}/.venv/bin/python" ]; then
  rm -rf "${runtime_root}/.venv"
  cp -a "${seed_root}/.venv" "${runtime_root}/.venv"
fi
if [ ! -x "${runtime_root}/.venv/bin/python" ]; then
  echo "gptimage2api: bundled Python environment could not be installed in /app" >&2
  exit 1
fi

cd "${runtime_root}"
export VIRTUAL_ENV="${runtime_root}/.venv"
export PATH="${VIRTUAL_ENV}/bin:${PATH}"
exec "$@"
