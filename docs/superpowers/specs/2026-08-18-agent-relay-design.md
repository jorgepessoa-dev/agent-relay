# agent-relay — design

**Status**: verificado pelo DeepCode em duas passagens (seq 155-158, 2026-08-17/18) — achados incorporados abaixo.
**Origem**: extraído de `/opt/agent-relay` no droplet do trading-advisor (ver `governance/adr/ADR-192_multi_agent_autoevolution_architecture.md` no repo tradingadvisor para o contexto de arquitectura que o motivou).
**Nota sobre prior-art** [UNCONFIRMED]: uma pesquisa web (2026-08-18) sugeriu que file-based JSONL+locking é o padrão comum para mailbox entre agentes em tmux, citando "AgentMail" e "agent-orchestrator issue #853". O DeepCode verificou essas duas fontes por pesquisa independente e nenhuma corresponde ao que foi afirmado (AgentMail é mailbox estilo email, não JSONL+flock; o issue #853 citado não é o mesmo projecto). **Removida a alegação — não decide a arquitectura.** A escolha mailbox JSONL + tmux nudge mantém-se pelo mérito próprio: é o mecanismo já validado em produção no droplet, não por conformidade a um padrão de mercado não confirmado.

## Propósito

Mailbox JSONL fiável entre N agentes de IA, cada um numa sessão tmux própria, substituindo `tmux send-keys` fire-and-hope (perda de mensagens sem recibo de entrega, sem ordenação, engolidas se o receptor estiver a meio de turno).

## Âmbito

N agentes nomeados (não 2 fixos). Ficheiro standalone, sem instalação — cada projecto copia `relay.py` para `scripts/`.

## Configuração — `relay.yaml`

Na raiz do projecto que usa o relay:

```yaml
box_dir: ./agent-relay-mail
agents:
  - name: coord
    tmux_session: claude
    busy_regex: 'status: (processing|pending)|esc to interrupt'
    input_prefix: "> "
    repo_path: /opt/some-project        # opcional, para o head-check
  - name: dc
    tmux_session: dc
    busy_regex: 'status: (processing|pending)|esc to interrupt'
    input_prefix: "> "
    repo_path: /opt/some-project
```

`tmux_session`, `busy_regex` e `input_prefix` são **obrigatórios por agente, sem default silencioso** (achado DeepCode Q2/Q3: um default partilhado é falso para TUIs diferentes — "status: processing|pending" é do DeepCode, "esc to interrupt" é do Claude Code; um regex errado falha silenciosamente). `relay.py doctor --agent X` valida a config contra a realidade antes do primeiro uso: `tmux has-session -t <tmux_session>`, captura o pane e pede confirmação visual de que `busy_regex`/`input_prefix` casam.

## Mailbox

- Um ficheiro JSONL por agente-destinatário: `<box_dir>/to_<nome>.jsonl`, append-only, **criado com permissões 0600** (achado DeepCode Q1 nota 4: mailbox 0666 permite qualquer processo no host forjar `from`; 0600 não resolve multi-host mas reduz a superfície local sem complexidade extra).
- Um cursor por agente: `<box_dir>/.cursor_<nome>`, escrito por write-temp-then-rename (atómico; achado DeepCode Q1 nota 5b — `write_text` directo pode truncar a meio de um crash).
- Append protegido por `fcntl.flock` **exclusivo** sobre o ficheiro JSONL durante todo o ciclo ler-contagem+escrever (achado DeepCode Q1 nota 1). `read`/`peek` tomam `flock` **partilhado** (`LOCK_SH`) durante a leitura — achado DeepCode v2 nota 4: sem lock também do lado da leitura, um `read` concorrente com um `send` de corpo grande (o `send` plain, ao contrário do `safe-send`, não tem limite de tamanho) pode apanhar a última linha a meio da escrita e crashar em `json.loads`; com `LOCK_SH` a leitura espera pelo `flock` exclusivo do writer soltar antes de ler.
- Cada linha: `{seq, ts_utc, from, head, tokens, body}`. `head` = git HEAD curto do remetente, **capturado no momento do `send`** e **comparado no `read`** contra o HEAD actual do `repo_path` desse mesmo agente (não contra o HEAD do leitor) — achado DeepCode Q1 nota 2 + clarificação v2: o campo guardado não é recomputado, é o HEAD-actual-do-remetente que é lido de novo no momento da leitura para a comparação.
- **Read receipt: fora de âmbito do v1, declarado explicitamente** (achado DeepCode Q1 nota 3). O relay garante entrega (mensagem no ficheiro + nudge tentado), não confirmação de leitura. Quem precisar de ack faz `read` seguido de um `send` de confirmação — é composição, não uma feature nova do transporte.
- **Idempotência de retry: fora de âmbito, declarado.** (achado DeepCode v2 nota 3) Reenviar uma mensagem após uma falha de nudge cria uma nova linha/`seq` — não há deduplicação. Um retry é, por definição, uma mensagem nova.
- **`box_dir` relativo resolve contra a CWD do processo que corre o `relay.py`, não contra a localização do script** (achado DeepCode v2 nota 2). `send`/`read`/`peek`/`doctor` devem correr a partir da raiz do projecto onde vive o `relay.yaml`.
- **Trust model: same-host, mesmo utilizador OS, agentes confiados.** Sem assinatura, sem verificação de `from`. Permissões 0600 no mailbox **assumem que todos os agentes correm como o mesmo user** (achado DeepCode v2 nota 5) — se um deployment precisar de agentes em users OS diferentes no mesmo host, usa grupo partilhado + 0660 em vez de 0600 (não é o default, é uma troca explícita que o operador faz). Declarado, não escondido — se algum dia houver agentes multi-host, isto tem de ser revisto antes de os ligar.

## Comandos

```
relay.py send      --from A --to B --body "..." [--tokens N]
relay.py safe-send  --from A --to B --body "..."   # com guardas (ver abaixo)
relay.py read       --as B                          # imprime não lidas, avança cursor
relay.py peek        --as B                          # imprime não lidas, não avança
relay.py doctor     --agent X                        # valida tmux_session/busy_regex/input_prefix contra a realidade
```

`beat` (heartbeat) sai do v1 — achado DeepCode v2 nota 1: com N agentes, "o destinatário do heartbeat" deixa de ser implícito ("o outro"), e generalizar para `--from A --to B` transforma-o num `send` com um corpo convencionado (`[heartbeat] tokens=N`). Não há mecanismo novo aqui — quem quiser heartbeat compõe com `send --body "[heartbeat] tokens=$N"`.

`send` sempre tenta o nudge (`tmux send-keys` para a sessão do destinatário) e verifica se o texto ficou preso na caixa de input em vez de assumir entrega.

**Nudge — sequência corrigida** (achado DeepCode, bug ao vivo apanhado durante a própria verificação, seq=156): a versão original conclui "delivered" quando `capture-pane` falha ou a sessão não existe, porque o veredicto lê a AUSÊNCIA do marcador numa captura vazia — indistinguível de "mensagem entregue e já saiu do ecrã". Sequência corrigida:
1. `tmux has-session -t <tmux_session>` — se falhar, reporta **"SESSÃO AUSENTE"** explicitamente, nunca "delivered".
2. `send-keys` do texto, sleep, `send-keys` do Enter (dois passos separados — combinar num só arrisca o Enter ser engolido pela TUI; padrão já validado no original).
3. `capture-pane`, **verificar o returncode antes de interpretar a saída**. Só com sessão confirmada + captura bem-sucedida é que a ausência do marcador na caixa de input conta como "delivered".

## Guarda `safe-send`

Antes de enviar, recusa se:
1. A caixa de input do destinatário já tem texto por enviar (lida pela última linha que começa com `input_prefix`), **com excepção do placeholder** (ex: `"Type your message..."` ou equivalente configurável) — achado DeepCode Q2 nota 2, confirmado contra o `safe_send.sh` real: o placeholder não é input pendente e a guarda original tinha esta excepção; a generalização tinha-a perdido.
2. O destinatário está a meio de turno (`busy_regex` casa nas últimas 14 linhas do pane — âmbito limitado ao fim do scrollback, não ao pane inteiro, para não ficar preso por um "Thinking" residual de um turno crashado).
3. O corpo excede 1600 caracteres (detalhe vai para ficheiro, mensagem é um ponteiro).

**Nota de calibração assimétrica** (achado DeepCode Q3 nota 3): no mecanismo original só a direcção coord→dc tinha `safe_send.sh` validado em produção; dc→coord nunca foi testada. O `busy_regex`/`input_prefix` de cada agente devem ser validados por `relay.py doctor` nos dois sentidos antes de assumir simetria — não copiar o mesmo regex para os dois lados por omissão.

## Fora de âmbito (YAGNI)

Sem fila de tarefas, sem routing por custo/criticidade — isso é decisão de produto de cada projecto (ver ADR-192 do trading-advisor como exemplo), não do mecanismo de transporte. Este módulo é só mailbox + nudge + guarda.

Confirmado pelo DeepCode: atomicidade de `seq`, semântica de staleness e o falso-positivo do nudge NÃO são YAGNI — são correcções ao próprio mecanismo de transporte, por isso entraram no v1 acima em vez de ficarem cortadas. Read receipt e autenticidade cross-host ficam mesmo fora, mas declarados (não omissos).

## Testes

- Envio/leitura roundtrip entre 2 agentes fictícios (sessões tmux de teste).
- Cursor avança correctamente com `read`, não avança com `peek`.
- `safe-send` recusa correctamente nos 3 casos de guarda (mockar `tmux capture-pane`).
- `safe-send` NÃO recusa quando a caixa de input mostra o placeholder configurado.
- `head` cai para `"unknown"` se `repo_path` não é um repo git válido.
- **Nudge para sessão tmux inexistente reporta "SESSÃO AUSENTE", nunca "delivered"** (regressão do bug ao vivo apanhado pelo DeepCode em seq=156).
- Dois `send` concorrentes ao mesmo destinatário (via threads/subprocessos em paralelo) produzem `seq` estritamente ascendente e sem duplicados.
- `relay.py doctor` detecta `tmux_session` inexistente e recusa avançar.
- `read` concorrente com um `send` de corpo grande a meio da escrita não crasha (espera pelo `LOCK_SH`, não lê linha parcial).
- `send`/`read` correndo de uma CWD diferente da raiz do projecto falham de forma legível (não silenciosamente contra o `box_dir` errado).
