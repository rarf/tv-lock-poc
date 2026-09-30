# PLANO — HA como hub + controlo familiar das TVs

Regra transversal: **cada fase fecha com evidência real** (comando + output) e cada mudança
em TV tem rollback escrito antes. Nada avança sem decisão do Ronald nos pontos marcados 🔴.

## F0 — Recon ✅ (2026-09-21)
Estado do HA, inventário das duas TVs, alavancas disponíveis e limites medidos:
`docs/RECON-2026-09-21.md`. Monitor read-only já funcional (`tools/tv_sala.py`).

## F1 — Base sã do HA (sem tocar nas TVs)
Objetivo: HA fiável antes de lhe dar responsabilidades.
1. Corrigir o `configuration.yaml`: IP stale (`192.168.1.155` → atual), limpar comentários
   das entidades que não existem.
2. Decidir o destino das entidades Cast quebradas: corrigir o cliente Cast ou removê-las e
   substituir pelo controlo real (F2).
3. Ligar o MQTT da casa ao HA (broker `127.0.0.1:1883` já a correr) — necessário para TeslaMate
   e para futuros eventos das TVs.
4. Criar **token de longa duração** para automação externa (Hermes/cron) e guardar em ficheiro
   modo 0600, nunca no chat.
5. Decidir exposição externa (zrok/cloudflared) só se houver caso de uso claro.
6. Backups: confirmar que o `backup` do HA cobre `/config` para storage fora do container.

**Gate F1:** `curl -s -o /dev/null -w '%{http_code}' http://localhost:8123/` = 302,
MQTT ligado, token a funcionar numa chamada real a `/api/states`.

## F2 — Controlo real das TVs (`tv-bridge`)
Objetivo: HA consegue ligar/desligar/abrir/bloquear apps nas duas TVs e ler o estado.
1. Container `casa-tv-bridge` (python + `android-tools`), na rede da casa, com
   `adb connect <IP_ANTIGO>:5555` e `<TV_QUARTO_IP>` (já autorizados; sem PIN).
2. API HTTP mínima: `GET /tv/{id}/status` (ecrã, app em foco, reprodução, posição),
   `POST /tv/{id}/key/{home|sleep|wake|back|power}`, `POST /tv/{id}/open/{package}`,
   `POST /tv/{id}/block|unblock/{package}` (só `pm disable-user`/`pm enable`, com rollback).
3. `tools/tv_sala.py` passa a ser cliente/fallback da mesma API (uma só fonte de verdade).
4. No HA: `rest_command` + template sensors por TV (`tv_sala_app`, `tv_sala_estado`, …).

**Gate F2:** abrir e fechar a Netflix nas duas TVs por comando do HA, ler o app em foco de
volta no HA, e confirmar a TV a tocar (via `PlaybackState`) sem intervenção manual.

## F3 — Políticas familiares (o que as crianças veem)
Objetivo: **uma única expressão de controlo total**: travar ou destravar a TV da sala.
Regras de produto decididas em 2026-09-24:
- Primeiro alvo: **TV da sala** (`<IP_ANTIGO>`, sujeito a revalidar o IP atual).
- **Travar** significa bloquear todas as apps utilizáveis; a única intervenção de正常运行 é o launcher/sistema.
- **Destravar** significa reativar tudo e permanece assim até nova ação manual.
- Apenas **Ronald** e **Márcia**,through contas HA separadas, podem destravar.
- A duração não é fixa: os pais configuram limites temporais como no Family Link
  (horário escolar, fim de semana, hora de dormir e exceções).
- Antes de qualquer `pm disable-user`, criar a lista de pacotes, snapshot do estado e rollback
  escrito. Nunca incluir launcher, Settings, System UI, input, Bluetooth, ADB, Google services
  essenciais ou pacotes necessários ao arranque/cast.

1. Construir um serviço de autorização no `tv-bridge` que verifique a identidade autenticada do HA
   e aceite apenas os IDs de adulto configurados; não aceitar `user_id` do cliente como prova.
2. Expor no HA `lock.tv_da_sala` e `input_boolean`/helpers para schedules e modo manual.
3. Fazer `travar`: parar a app atual, enviar Home, capturar estado e desabilitar o inventário
   autorizado de apps, uma de cada vez ou em lotes pequenos, com readback e rollback.
4. Fazer `destravar`: reativar todos os pacotes que o próprio sistema colocou em lock, nunca
   uma lista manual desatualizada ou pacotes já desativados antes do sistema.
5. Aplicar horários/bedtime como automações HA; avisos 5 minutos antes e registo de
   Ronald/Márcia, hora, TV e ação. Exceções manuais dos pais ficam auditáveis.
6. Só depois de uma semana sem falhas, avaliar a TV do quarto e a segunda etapa de segurança
   (VLAN/fechar ADB sem fios) para reduzir contornos técnicos.

**Gate F3:** adulto autorizado destrava a TV; a outra conta não; após nova trava, todas as
apps voltam a ficar bloqueadas; uma exceção temporal expira e volta a travar; o HA e a TV
permanecem utilizáveis; log de auditoria confirma cada transição.

## F4 — Visibilidade e relatórios
1. Amostragem (30–60 s) do app em foco + estado por TV → **minutos por app, por dia**.
2. Digest diário no Telegram: minutos por TV/app + títulos Jellyfin do dia.
3. Honestidade de dados: onde só há app+minutos (Netflix/Prime/Disney) o relatório di-lo
   explicitamente; onde há Jellyfin, mostra o título.
4. Semanalmente: tendência + sugestões (ex.: padrão de uso tardio).

**Gate F4:** 3 dias seguidos de digest com números que batem certo com o uso real observado.

## F5 — Opcional: filtragem de conteúdo por DNS
AdGuard Home (ou NextDNS) por dispositivo, para idade/conteúdo em vez de só apps.
Riscos a resolver primeiro: DHCP do router (DIGI), NordVPN na TV da sala, e o precedente
já conhecido em que DNS filtrado/VPN na TV do quarto partiu a resolução de nomes públicos
(skill `android-tv-adb` §3.5). Só entra se as fases anteriores não chegarem.

## Ordem recomendada
F1 → F2 → F3 → F4 (→ F5 se necessário). F3 e F4 só fazem sentido depois de F2 estar estável,
porque sem controlo real do bridge não há enforcement nem medição fiável.
