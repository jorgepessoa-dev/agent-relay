# agent-relay — design

**Status**: verificado pelo DeepCode (seq 155-157, 2026-08-17/18) — achados incorporados abaixo.
**Origem**: extraído de `/opt/agent-relay` no droplet do trading-advisor (ver `governance/adr/ADR-192_multi_agent_autoevolution_architecture.md` no repo tradingadvisor para o contexto de arquitectura que o motivou).
**Prior art confirmado** (pesquisa 2026-08-18): a convergência da comunidade em 2026 é exactamente file-based JSONL com file-locking para mailbox entre agentes em tmux (ex: AgentMail, agent-orchestrator issue #853) — o desenho base está alinhado com o estado da arte; não há motivo para reescrever para FIFO/stdin.

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
- Append protegido por `fcntl.flock` exclusivo sobre o ficheiro JSONL durante todo o ciclo ler-contagem+escrever (achado DeepCode Q1 nota 1 — `seq` por contagem de linhas não é atómico entre sends concorrentes; sem lock, dois agentes a escrever ao mesmo destinatário podem gerar `seq` duplicado e o cursor salta uma mensagem).
- Cada linha: `{seq, ts_utc, from, head, tokens, body}`. `head` = git HEAD curto do **repo do remetente**, recalculado no momento da leitura contra o `repo_path` desse agente (não comparado com o HEAD do leitor). Achado DeepCode Q1 nota 2: comparar com o HEAD do leitor produz falsos "SENDER HEAD DIFFERS" sempre que os agentes vivem em repos diferentes — a pergunta certa é "o mundo do remetente moveu-se desde que ele escreveu isto?", não "o meu repo é igual ao dele?".
- **Read receipt: fora de âmbito do v1, declarado explicitamente** (achado DeepCode Q1 nota 3). O relay garante entrega (mensagem no ficheiro + nudge tentado), não confirmação de leitura. Quem precisar de ack faz `read` seguido de um `send` de confirmação — é composição, não uma feature nova do transporte.
- **Trust model: same-host, agentes confiados.** Sem assinatura, sem verificação de `from`. Declarado, não escondido — se algum dia houver agentes multi-host, isto tem de ser revisto antes de os ligar (não é um "TODO" implícito).

## Comandos

```
relay.py send      --from A --to B --body "..." [--tokens N]
relay.py safe-send  --from A --to B --body "..."   # com guardas (ver abaixo)
relay.py read       --as B                          # imprime não lidas, avança cursor
relay.py peek        --as B                          # imprime não lidas, não avança
relay.py beat        --as A --tokens N               # heartbeat
```

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
