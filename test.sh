#!/usr/bin/env bash
set -uo pipefail # без -e, чтобы не останавливалось после проваленного теста

API="http://127.0.0.1:8080"
STATE="/var/lib/vm-api/state.json"
VIRSH=(virsh -c qemu:///system)
BODY=/tmp/vmapi.body
PASS=0
FAIL=0

[[ "$EUID" -eq 0 ]] || { echo "Запустите: sudo ./check.sh"; exit 1; }


ok()   { PASS=$((PASS+1)); echo "PASS: $*"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $*"; }

# assert "<описание>" "<ожидаемое>" "<фактическое>"
assert() {
    [[ "$2" == "$3" ]] && ok "$1" || bad "$1 (ожидалось '$2', получено '$3')"
}

get()  { curl -s -o "$BODY" -w '%{http_code}' "$API$1"; }
post() { shift; curl -s -o "$BODY" -w '%{http_code}' -X POST "$@" ; }
post_api() { local p="$1"; shift; curl -s -o "$BODY" -w '%{http_code}' -X POST "$API$p" "$@"; }
del_api()  { curl -s -o "$BODY" -w '%{http_code}' -X DELETE "$API$1"; }
state_of() {
    local state
    if state=$("${VIRSH[@]}" domstate "$1" 2>/dev/null); then
        echo "$state"
    else
        echo "NONE"
    fi
}

cleanup() {
    for vm in $("${VIRSH[@]}" list --all --name 2>/dev/null | grep '^test-' || true); do
        "${VIRSH[@]}" destroy "$vm" >/dev/null 2>&1 || true
        "${VIRSH[@]}" undefine "$vm" --remove-all-storage >/dev/null 2>&1 || true
        rm -f "/var/lib/libvirt/images/$vm.qcow2" 2>/dev/null || true
    done
    if [[ -f "$STATE" ]]; then
        jq 'with_entries(select(.key | startswith("test-") | not))' "$STATE" > "$STATE.tmp" \
            && mv "$STATE.tmp" "$STATE"
    fi
}

# Старт
echo "== Старт =="
cleanup
code=$(get /vms)
[[ "$code" == "200" ]] && ok "API отвечает" || { echo "FAIL: API недоступен"; exit 1; }

# Создание
echo
echo "== Создание =="
code=$(post_api /vms -H 'Content-Type: application/json' -d '{"name":"test-1"}')
assert "POST /vms test-1 → 201" "201" "$code"
sleep 2
assert "test-1 запущена" "running" "$(state_of test-1)"
assert "state.json содержит test-1" "yes" "$(grep -q test-1 "$STATE" && echo yes || echo no)"

# Валидация
echo
echo "== Валидация =="
code=$(post_api /vms -H 'Content-Type: application/json' -d '{"name":"test-1"}')
assert "дубликат → 409" "409" "$code"
code=$(post_api /vms -H 'Content-Type: application/json' -d '{"name":"bad/name"}')
assert "плохое имя → 400" "400" "$code"
code=$(post_api /vms -H 'Content-Type: application/json' -d 'not json')
assert "битый JSON → 400" "400" "$code"
code=$(post_api /vms)
assert "пустое тело → 400" "400" "$code"

# stop / start
echo
echo "== Stop / Start =="
code=$(post_api '/vms/test-1/stop?force=1')
assert "stop → 200" "200" "$code"
sleep 5
assert "test-1 выключена" "shut off" "$(state_of test-1)"
assert "state.json: desired=stopped" "yes" "$(grep -q '"desired": "stopped"' "$STATE" && echo yes || echo no)"

code=$(post_api /vms/test-1/start)
assert "start → 200" "200" "$code"
sleep 2
assert "test-1 снова работает" "running" "$(state_of test-1)"

# reconcile крах
echo
echo "== Reconcile: крах =="
"${VIRSH[@]}" destroy test-1 >/dev/null 2>&1
sleep 1
assert "имитация краха: shut off" "shut off" "$(state_of test-1)"
python3 -c "import app; app.reconcile()" 2>/dev/null
sleep 3
assert "reconcile запустил test-1" "running" "$(state_of test-1)"

# reconcile уважает stop
echo
echo "== Reconcile: уважает stop =="
post_api '/vms/test-1/stop?force=1' >/dev/null
sleep 1
"${VIRSH[@]}" start test-1 >/dev/null 2>&1
python3 -c "import app; app.reconcile()" 2>/dev/null
sleep 2
assert "reconcile остановил нежелательно запущенную ВМ" "shut off" "$(state_of test-1)"

# reconcile потеря
echo
echo "== Reconcile: потеря =="
post_api /vms/test-1/start >/dev/null
sleep 2
"${VIRSH[@]}" destroy test-1 >/dev/null 2>&1
"${VIRSH[@]}" undefine test-1 >/dev/null 2>&1
assert "test-1 потеряна" "NONE" "$(state_of test-1)"
python3 -c "import app; app.reconcile()" 2>/dev/null
sleep 3
assert "reconcile восстановил test-1" "running" "$(state_of test-1)"

# delete
echo
echo "== Delete =="
code=$(del_api /vms/test-1)
assert "DELETE → 200" "200" "$code"
assert "test-1 удалена из libvirt" "NONE" "$(state_of test-1)"
assert "test-1 удалена из state.json" "yes" "$(grep -q test-1 "$STATE" 2>/dev/null && echo no || echo yes)"

# delete не воскрешается
echo
echo "== Delete не воскрешается =="
python3 -c "import app; app.reconcile()" 2>/dev/null
sleep 1
assert "reconcile не воскресил удалённую" "NONE" "$(state_of test-1)"

# Финал
echo
cleanup
echo "== Итог: $PASS PASS, $FAIL FAIL =="
[[ "$FAIL" -eq 0 ]] && echo "OK" || echo "FAIL"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
