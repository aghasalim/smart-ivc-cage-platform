#!/usr/bin/env bash
# Recompute the classification metrics and online-learning agreement from the
# raw data in eight independent implementations.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

pass=0 fail=0 skip=0

run () {
    local name="$1" tool="$2"; shift 2
    printf '\n=== %s ===\n' "$name"
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'skipped: %s is not installed\n' "$tool"
        skip=$((skip + 1)); return
    fi
    if "$@"; then pass=$((pass + 1)); else fail=$((fail + 1)); fi
}

check_c () {
    cc -std=c99 -O2 -Wall -Wextra -Wpedantic -Werror -o /tmp/report_c \
        verify/report.c -lm &&
    /tmp/report_c "$root"
}

check_report_sql () {
    local out
    out=$(sqlite3 :memory: -init verify/report.sql "" < /dev/null 2>&1 | tr -d '\r')
    if echo "$out" | grep -qi 'FAIL\|DISAGREE'; then
        echo "$out"
        return 1
    fi
    echo "$out"
}

check_agreement_sql () {
    local out
    out=$(sqlite3 :memory: -init verify/agreement.sql "" < /dev/null 2>&1 | tr -d '\r')
    if echo "$out" | grep -qi 'FAIL\|DISAGREE'; then
        echo "$out"
        return 1
    fi
    echo "$out"
}

check_go () { ( cd verify/gocheck && go run . -root "$root" ); }
check_py () { python3 verify/report.py "$root"; }
check_r  () { Rscript verify/report.R "$root"; }
check_rb () { ruby verify/report.rb "$root"; }

run "C, confusion matrix to metrics"         cc      check_c
run "SQL, confusion matrix to metrics"        sqlite3 check_report_sql
run "SQL, online-learning agreement"          sqlite3 check_agreement_sql
run "Go, confusion matrix to metrics"         go      check_go
run "Python, confusion matrix to metrics"     python3 check_py
run "R, confusion matrix to metrics"          Rscript check_r
run "Ruby, confusion matrix to metrics"       ruby    check_rb

printf '\n%s\n' "----------------------------------------"
printf '%d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ] || exit 1
[ "$pass" -gt 0 ] || { echo "nothing ran"; exit 1; }
