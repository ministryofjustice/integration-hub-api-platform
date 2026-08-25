#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${repo_root}/build"
mkdir -p "${build_dir}"

for function in benefit_orchestrator request_authorizer; do
  package_name="${function//_/-}.zip"
  package_file="${build_dir}/${package_name}"
  rm -f "${package_file}"
  cd "${repo_root}/lambda/${function}"
  zip -q "${package_file}" lambda_function.py
  echo "Created ${package_file}"
done
