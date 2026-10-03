# Tarefa de revisão: rascunho de post X (validação de dor)

Revê APENAS o texto em `POST-DRAFT.md` (neste diretório). Não escrevas, não
edites, não publiques nada. Devolve crítica, não uma versão reescrita.

## Contexto real (verificado, 2026-10-01)

- Produto: `github.com/rarf/tv-lock-poc`, proof of concept público, MIT.
  Parental control para Android/Google TV por ADB, conduzido por Home Assistant.
- Estado real do serviço (do `audit.log` da máquina, 2026-09-30/10-01):
  - 20 apps configurados como alvo em `tv_da_sala`; 15 registados no diário.
  - `2026-09-30T12:30:05Z` `lock_refused_in_use` — "ha reproducao ativa — travar
    cortaria o visionamento | com.netflix.ninja"
  - `2026-09-30T12:30:47Z` janela de 180 min aberta
  - `2026-09-30T15:31:01Z` `auto_relock` — "janela expirou; 15 apps"
- Limites honestos do README: dissuasão e não segurança (ADB aberto na porta
  5555, qualquer pessoa na LAN reativa tudo); Android 9, perfil único;
  bloqueio por app e não por conteúdo; HDMI não é coberto; testado só em duas
  Xiaomi (MiTV-MSSP3 e MIBOX3).
- Contexto do autor: pai, PT-PT, conta X com cerca de 80 seguidores. O post
  anterior dele (anúncio de um plugin, com screenshot e link de repo) foi o de
  maior sucesso. Este post tem um objetivo diferente e específico: validar se a
  dor é widespread entre pais com crianças e TVs, não gerar produto ou hype.
- A imagem que acompanha o post é gerada por IA, não é um screenshot real.
  O texto não a menciona.

## O que quero saber

1. O texto tem gralhas, erros de PT-PT, ou texto corrompido? Lista-os tal
   como estão.
2. Lê-se como uma pessoa a falar, ou como texto gerado? Onde é que denuncia?
3. O pedido final (isto é widespread ou é só nosso?) funciona para elicitir
   respostas de outras pessoas? Ou soa a fechar-se, a justificação, ou a
   mendigar validação?
4. O corpo do post serve o objetivo (validar dor) ou está a fazer outra coisa?
5. Há alguma afirmação no texto que os factos acima não sustentam?
6. O que removerias, e o que acrescentarias (uma frase concreta, não um
   princípio)?

## Formato da resposta

Máximo 400 palavras. Lista de problemas concretos com o trecho exato citado, e
uma recomendação final de duas linhas. Nada de reformular o post inteiro, nada
de elogios, nada de repetir as minhas perguntas.
