#!/usr/bin/env bash
# End-to-end test of tv_lock_service: auth, guard refusal, unlock window,
# auto-relock. Readable output, no hidden steps.
set -u
TOK=$(cat "$(dirname "$0")/state/api_token")
API=${TV_LOCK_SERVICE_URL:-http://127.0.0.1:8129}
TV=${TV_LOCK_TEST_TV:-tv_sala}
SERIAL=${TV_LOCK_TEST_SERIAL:-192.0.2.10:5555}

say() { printf "\n\033[1m%s\033[0m\n" "$*"; }
call() { # method path json
  local m=$1 p=$2 d=${3:-}
  if [ -n "$d" ]; then
    curl -s --max-time 180 -X "$m" -H "X-TV-Lock-Token: $TOK" \
      -H "Content-Type: application/json" -d "$d" "$API$p"
  else
    curl -s --max-time 180 -X "$m" -H "X-TV-Lock-Token: $TOK" "$API$p"
  fi
}
jq_() { python3 -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: print('  (sem json)'); raise SystemExit
$1" ; }

say "1) unlock 5 min (janela curta para o teste)"
call POST /unlock "{\"tv\":\"$TV\",\"minutes\":5,\"actor\":\"teste\"}" | jq_ "
print(' ok:', d.get('ok'), '| apps reativadas:', d.get('re_enabled'), '| expira:', d.get('expires_at'))
print(' janela:', d.get('window'))"

say "2) lock durante a janela de destrava -> deve RECUSAR (409)"
call POST /lock "{\"tv\":\"$TV\",\"actor\":\"teste\"}" | jq_ "
print(' erro:', d.get('error'), '| janela ate:', (d.get('window') or {}).get('expires_at'))"

say "3) verificar estado real na TV"
adb -s "$SERIAL" shell pm list packages -d | wc -l | sed 's/^/  desativadas agora: /'

say "4) forcar lock mesmo com janela aberta"
call POST /lock "{\"tv\":\"$TV\",\"force\":true,\"actor\":\"teste\"}" | jq_ "
print(' ok:', d.get('ok'), '| bloqueadas:', len(d.get('disabled',[])), '| falhas:', d.get('failed'))"

say "5) maintenance: reativar so a Play Store (30 min)"
call POST /maintenance "{\"tv\":\"$TV\",\"minutes\":30,\"actor\":\"teste\"}" | jq_ "
print(' ok:', d.get('ok'), '| reativadas:', d.get('enabled'))
print(' nota:', d.get('note'))"
adb -s "$SERIAL" shell pm list packages -d | grep -c vending | sed 's/^/  vending ainda bloqueada? (0=aberta): /'

say "6) maintenance com pacote nao autorizado -> deve RECUSAR (400)"
call POST /maintenance "{\"tv\":\"$TV\",\"packages\":[\"org.jellyfin.androidtv\"],\"actor\":\"teste\"}" | jq_ "
print(' erro:', d.get('error'))"

say "7) audit"
tail -6 ""$(dirname "$0")/state/audit.log"" | sed 's/^/  /'
