# PLANO DE EXECUÇÃO — Trava parental (TV da sala)

Data: 2026-09-29 · Autorização: Ronald, nesta conversa
Ferramenta: `tools/tv_lock.py` (só `pm disable-user` / `pm enable`, com journal)

## Decisões do Ronald (2026-09-29)
- Alvo: **TV da sala** (`<TV_SALA_IP>`, MiTV-MSSP3, Android 9).
  - "Smart Monitor" fica **fora**: é um device Cast (M70D, `192.168.1.147`), **sem ADB**.
    Não é possível bloquear apps nele por ADB. Ver "Limites" abaixo.
- Comportamento ao travar: **bloqueio total** — nenhuma app abre, a TV vai ao launcher.
- Destrava: **temporal, 2 horas**.
- Jellyfin **também** fica bloqueado enquanto travado.
- Interface: **Home Assistant + Telegram**.
- Não existe ainda utilizador da Márcia no HA (falta criar).

## Lote 1 — 15 apps (dry-run já validado, zero mutações)
Desativar com `pm disable-user --user 0`:
```
com.disney.disneyplus
com.google.android.youtube.tv
com.google.android.youtube.tvmusic
com.mediatek.androidbox
com.mediatek.wwtv.tvcenter
com.mitv.dream
com.mitv.gallery
com.mstar.netflixobserver
com.nathnetwork.xciptv
com.netflix.ninja
com.neutronemulation.super_retro_16
com.plexapp.android
com.stremio.one
com.yablio.sendfilestotv
org.jellyfin.androidtv
```

### ROLLBACK (uma linha por app, reversível)
```bash
for p in com.disney.disneyplus com.google.android.youtube.tv \
  com.google.android.youtube.tvmusic com.mediatek.androidbox \
  com.mediatek.wwtv.tvcenter com.mitv.dream com.mitv.gallery \
  com.mstar.netflixobserver com.nathnetwork.xciptv com.netflix.ninja \
  com.neutronemulation.super_retro_16 com.plexapp.android \
  com.stremio.one com.yablio.sendfilestotv org.jellyfin.androidtv; do
  adb -s <TV_SALA_IP> shell pm enable "$p"
done
```
Equivalente pelo journal (preferível, só reativa o que nós desativámos):
```bash
python3 tools/tv_lock.py unblock --tv tv_da_sala
```

## NUNCA tocar (protegido, validado no inventário)
Launcher `com.google.android.tvlauncher`, `com.mitv.tvhome.atv`, Settings
(`com.android.settings`, `com.android.tv.settings`), System UI, Package Installer,
Permission Controller, ADB/shell, Bluetooth, voz/remoto (`com.google.android.katniss`,
`com.google.android.tv.remote.service`), teclado, MediaShell, Play Store
(`com.android.vending`), `com.google.android.gms`, `com.google.android.gsf`, screensaver.
Os 2 pacotes **já** desativados antes de nós (`com.google.android.tvrecommendations`,
`com.miui.tv.analytics`) **não** são reativados pelo unlock: não são nossos.

## Ordem de execução e validação
1. `block --dry-run` (feito: 15 apps, 0 refusos protegidos).
2. Avisar Ronald: a TV vai reiniciar as apps em uso.
3. `block --tv tv_da_sala` → readback obrigatório (`pm list packages -d`).
4. O Ronald **testa** na TV: home, menu, comando, Jellyfin fechado, YouTube fechado.
5. Só depois: `unblock` de teste para confirmar o rollback, e `block` final.
6. Se algo falhar no passo 4 → rollback imediato do lote.

## Limites honestos desta solução
- ADB está aberto em `:5555` na TV. Quem souber usar ADB na rede local contorna a trava.
  Mitigação F5 (VLAN/fechar ADB) fica para depois; sem isso a trava é por Apps, não por
  segurança.
- Android 9, perfil único (user 0) → bloqueio por app, não por perfil de utilizador.
- Apps Netflix/Prime/Disney: o ADB bloqueia a app; **não** bloqueia conteúdo dentro dela nem
  as respetivas passwords.
- O "Smart Monitor" (M70D) não tem ADB: só se pode parar/mandar para o launcher via Cast,
  ou desligar a alimentação; não se pode bloquear apps.

## Dependências por construir (F2/F3)
- `tv_lock serve` exposto ao HA (`rest_command`) + token 0600.
- Utilizador da Márcia no HA (criado por ela, nunca por nós com password inventada).
- Telegram: bot com `travar`/`destravar`, autorização por user_id do Telegram.
- Temporizador de 2 h no HA (`input_datetime` + `automation`).
