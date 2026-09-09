# Estado, logs e inspeccion

Los comandos read-only del scheduler permiten ver el estado de un run sin mutar el repositorio objetivo.

## Scheduler status

```bash
ai_dev_loop scheduler status <run-id>
ai_dev_loop scheduler status <run-id> --output json
```

Muestra estado resumido del ledger central, iteracion actual, identidad de agentes
(redactada), ultimo error y siguiente accion segura.

## Scheduler list

```bash
ai_dev_loop scheduler list
ai_dev_loop scheduler list --output json
```

Lista runs conocidos por el ledger central.

## Scheduler history

```bash
ai_dev_loop scheduler history <run-id>
ai_dev_loop scheduler history <run-id> --limit 20 --order newest
```

Devuelve eventos acotados y redactados (sin prompts, patches, session IDs completos ni argv).

## Controller status

```bash
ai_dev_loop controller status \
  --controller-session-id "<exact-controller-session-id>" \
  --repo-path /path/al/repo \
  [--run-id <run-id>] \
  [--output text|json]
```

Lookup read-only para runs del scheduler: no muta estado ni llama Cursor/Codex.
Ante 0 o N coincidencias, usa `--run-id` para desambiguar.

## Artefactos

Los artefactos sensibles viven bajo `$XDG_STATE_HOME/ai_dev_loop/artifacts/` y el
estado durable en `engine.sqlite3`. Los comandos de inspeccion del scheduler no imprimen
prompts, patches ni session IDs completos por defecto.

## Runs legacy

Los comandos `status`, `inspect` y `logs` de nivel superior para runs bajo `runs/`
fueron retirados en Phase 17.7. Para estado historico, consulta los artefactos en disco
o elimina el arbol legacy con `scheduler cutover cleanup` solo tras aceptacion humana
independiente (ver [Desinstalacion y limpieza](desinstalacion-limpieza.md)).
