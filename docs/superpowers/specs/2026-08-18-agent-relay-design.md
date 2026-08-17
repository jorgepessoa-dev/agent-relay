# agent-relay — design

**Status**: draft, pending DeepCode verification
**Origem**: extraído de `/opt/agent-relay` no droplet do trading-advisor (ver `governance/adr/ADR-192_multi_agent_autoevolution_architecture.md` no repo tradingadvisor para o contexto de arquitectura que o motivou).

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

`busy_regex` e `input_prefix` têm defaults razoáveis se omitidos, mas devem ser afinados por deployment — TUIs diferentes (Claude Code, Codex CLI, Aider) têm indicadores de "ocupado" e prefixos de caixa de input diferentes. Um regex errado falha silenciosamente (ver comentários no `safe_send.sh` original sobre `grep` demasiado amplo apanhar "Thinking" preso de um turno crashado).

## Mailbox

- Um ficheiro JSONL por agente-destinatário: `<box_dir>/to_<nome>.jsonl`, append-only.
- Um cursor por agente: `<box_dir>/.cursor_<nome>`.
- Cada linha: `{seq, ts_utc, from, head, tokens, body}`. `head` = git HEAD curto do remetente (se `repo_path` configurado) — permite ao receptor detectar que a mensagem foi escrita com uma imagem desactualizada do repo.

## Comandos

```
relay.py send      --from A --to B --body "..." [--tokens N]
relay.py safe-send  --from A --to B --body "..."   # com guardas (ver abaixo)
relay.py read       --as B                          # imprime não lidas, avança cursor
relay.py peek        --as B                          # imprime não lidas, não avança
relay.py beat        --as A --tokens N               # heartbeat
```

`send` sempre tenta o nudge (`tmux send-keys` para a sessão do destinatário) e verifica se o texto ficou preso na caixa de input em vez de assumir entrega.

## Guarda `safe-send`

Antes de enviar, recusa se:
1. A caixa de input do destinatário já tem texto por enviar (lida pela última linha que começa com `input_prefix`).
2. O destinatário está a meio de turno (`busy_regex` casa nas últimas 14 linhas do pane — âmbito limitado ao fim do scrollback, não ao pane inteiro, para não ficar preso por um "Thinking" residual de um turno crashado).
3. O corpo excede 1600 caracteres (detalhe vai para ficheiro, mensagem é um ponteiro).

## Fora de âmbito (YAGNI)

Sem fila de tarefas, sem routing por custo/criticidade — isso é decisão de produto de cada projecto (ver ADR-192 do trading-advisor como exemplo), não do mecanismo de transporte. Este módulo é só mailbox + nudge + guarda.

## Testes

- Envio/leitura roundtrip entre 2 agentes fictícios (sessões tmux de teste).
- Cursor avança correctamente com `read`, não avança com `peek`.
- `safe-send` recusa correctamente nos 3 casos de guarda (mockar `tmux capture-pane`).
- `head` cai para `"unknown"` se `repo_path` não é um repo git válido.
