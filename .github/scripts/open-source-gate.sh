#!/usr/bin/env bash
set -euo pipefail

MODE="${1:---all}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

fail() {
  printf 'repository gate: %s\n' "$1" >&2
  exit 1
}

[[ -f README.md ]] || fail "README.md is required"
[[ -f LICENSE ]] || fail "LICENSE is required"

skill_file="001-contract-desensitizer/04-dsh-plugin/core/SKILL.md"
[[ -f "$skill_file" ]] || fail "embedded upstream Skill entry is required"
head -n 30 "$skill_file" | grep -Eq '^name:[[:space:]]*[^[:space:]]' || fail "upstream Skill name is required"
head -n 30 "$skill_file" | grep -Eq '^description:[[:space:]]*(>|[^[:space:]])' || fail "upstream Skill description is required"

if [[ "$MODE" == "--staged" ]]; then
  file_command=(git diff --cached --name-only --diff-filter=ACMR)
else
  file_command=(git ls-files)
fi

while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  case "$path" in
    *.DS_Store|.DS_Store|*.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|.env|.env.*)
      fail "sensitive or local-only filename is forbidden: $path"
      ;;
  esac
done < <("${file_command[@]}")

secret_pattern='AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,255}|sk-[A-Za-z0-9_-]{20,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
if [[ "$MODE" == "--staged" ]]; then
  if git diff --cached --no-ext-diff -U0 | grep '^+' | grep -Eq "$secret_pattern"; then
    fail "credential-like content detected in staged additions"
  fi
elif git grep -I -q -E "$secret_pattern" -- .; then
  fail "credential-like content detected in tracked files"
fi

if [[ -f CHECKSUMS.txt ]]; then
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c CHECKSUMS.txt >/dev/null || fail "CHECKSUMS.txt verification failed"
  else
    shasum -a 256 -c CHECKSUMS.txt >/dev/null || fail "CHECKSUMS.txt verification failed"
  fi
fi

printf 'repository gate: passed (%s)\n' "$MODE"
