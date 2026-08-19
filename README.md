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

## Garantias

- `seq` estritamente ascendente e sem duplicados sob sends concorrentes
  (`fcntl.flock` exclusivo).
- Nunca reporta "delivered" se a sessão tmux do destinatário não existir ou
  a captura do pane falhar.
- `safe-send` recusa enviar se a caixa de input do destinatário já tem
  texto por enviar, se ele está a meio de turno, ou se o corpo excede 1600
  caracteres — com excepção do texto de placeholder da TUI.

## Fora de âmbito (deliberado)

Sem fila de tarefas, sem routing por custo — isso é decisão de produto de
cada projecto, não deste mecanismo de transporte. Sem read receipt (garante
entrega, não leitura). Sem deduplicação de retries. Sem autenticação
cross-host — assume mesmo utilizador OS, mesmo host, agentes confiados.

## Usado por

- [`seed-experimental-framework`](https://github.com/jorgepessoa-dev/seed-experimental-framework)
  — coordena Claude Code, Codex e DeepCode em verificação ortogonal multi-vendor.
- Um sistema de trading algorítmico privado (mesmo mecanismo, origem deste repo).

## Vários projectos, uma máquina

Cada projecto tem o seu próprio `relay.yaml`/`relay.json` com `box_dir` absoluto
e nomes de agente prefixados por projecto (ex: `ta.coordinator`, `seed.deepcode`)
— nunca partilhar mailbox nem sessão tmux entre projectos, mesmo que corram no
mesmo host. Se precisares de comunicação entre projectos, cria um config de "hub"
à parte, com apenas os coordinators de cada projecto registados — nunca ligar os
agentes de trabalho directamente uns aos outros.

## Licença

MIT.
