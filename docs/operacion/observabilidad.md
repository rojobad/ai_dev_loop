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

## Scheduler timeline

```bash
ai_dev_loop scheduler timeline <run-id>
ai_dev_loop scheduler timeline <run-id> --limit 20 --order newest
```

Proyeccion acotada de intentos Cursor/Codex por iteracion. Cada fila incluye fase,
ordinal de reintento, estado seguro, marcas de tiempo durables y
`observed_duration_seconds` solo cuando existen inicio y fin observados en el
ledger. No sustituye a `history` ni expone IDs internos, artefactos ni salidas
raw. La cola y el preflight no aparecen como duracion de fase.

Limite por defecto: 50 filas. `--limit` acepta valores positivos hasta 200; por
encima de 200 se trunca al maximo duro. Si hay mas intentos que el limite
efectivo, `truncated` es `true` en salida JSON.

## Controller status

```bash
ai_dev_loop controller status \
  --repo-path /path/al/repo \
  --run-id <run-id>
# o descubrimiento legacy:
# ai_dev_loop controller status \
#   --controller-session-id "<exact-controller-session-id>" \
#   --repo-path /path/al/repo \
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
