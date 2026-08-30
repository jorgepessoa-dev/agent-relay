# agent-relay

Mailbox JSONL fiável entre N agentes de IA, cada um numa sessão tmux própria.
Substitui `tmux send-keys` fire-and-hope: sem recibo de entrega, sem
ordenação, mensagens engolidas se o receptor estiver a meio de turno.

Extraído e generalizado a partir de um mecanismo que já correu em produção
num sistema de trading algorítmico, coordenando um agente decisor (Claude)
com um agente gerador de hipóteses (DeepCode) — ver
`docs/superpowers/specs/2026-08-18-agent-relay-design.md` para o histórico
completo, incluindo os bugs reais que motivaram cada correcção.

## Instalar

Sem instalação. Copia `relay.py` para `scripts/` do teu projecto.

Config em `relay.yaml` (precisa de `pip install pyyaml`) ou `relay.json`
(zero dependências, stdlib apenas) — ver `relay.example.yaml` /
`relay.example.json`.

## Usar

```bash
python3 relay.py send      --from coord --to dc --body "mensagem"
python3 relay.py safe-send --from coord --to dc --body "mensagem"  # com guardas
python3 relay.py read      --as dc
python3 relay.py peek      --as dc
python3 relay.py doctor    --agent dc
```

## Garantias — e o que não são

**A mailbox em si é fiável**: `seq` estritamente ascendente e sem duplicados sob
sends concorrentes (`fcntl.flock` exclusivo), append durável no disco.

**O nudge (notificação por tmux) não é**: `send-keys` não verifica o próprio
returncode, e "delivered" significa "sessão existe e a captura do pane teve
sucesso" — não significa que o destinatário leu ou processou a mensagem. Read
receipt não existe, de propósito (ver Fora de âmbito).

**`safe-send` é uma heurística dependente de UI, não uma garantia formal**: lê o
texto do pane à procura de padrões configuráveis (`busy_regex`, `input_prefix`)
para decidir se o destinatário está livre. Isto depende da TUI específica de
cada agente, tem uma janela de corrida entre a leitura do pane e o envio, e
recusa (não bloqueia) enviar se a caixa de input já tem texto, se o destinatário
parece ocupado, ou se o corpo excede 1600 caracteres — com excepção do texto de
placeholder da TUI.

## Fora de âmbito (deliberado)

Sem fila de tarefas, sem routing por custo — isso é decisão de produto de
cada projecto, não deste mecanismo de transporte. Sem read receipt (garante
entrega, não leitura). Sem deduplicação de retries. Sem autenticação
cross-host — assume mesmo utilizador OS, mesmo host, agentes confiados.

## Usado por

- [`seed-experimental-framework`](https://github.com/jorgepessoa-dev/seed-experimental-framework)
  — usado na coordenação operacional/desenvolvimento entre Claude Code, Codex e
  DeepCode (verificação ortogonal multi-vendor). Não é uma dependência de runtime
  do motor SEED — o código do engine não importa `relay.py`.
- Um sistema de trading algorítmico privado (mesmo mecanismo, origem deste repo).

## Vários projectos, uma máquina — recomendação, não mecanismo imposto

O `relay.py` não sabe nada sobre "projectos" — namespaces, isolamento de sessões
e não-partilha de mailbox entre projectos são responsabilidade de quem opera,
não algo que o código valide ou imponha. Padrão recomendado: cada projecto com o
seu próprio `relay.yaml`/`relay.json` (`box_dir` absoluto) e nomes de agente
prefixados (ex: `ta.coordinator`, `seed.deepcode`) — nunca partilhar mailbox nem
sessão tmux entre projectos, mesmo que corram no mesmo host. Se precisares de
comunicação entre projectos, a recomendação é um config de "hub" à parte, com
apenas os coordinators de cada projecto registados — mas isto também é
convenção arquitectural, não um mecanismo separado que o código implemente.

## Licença

MIT.
