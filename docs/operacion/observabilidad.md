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
ledger. Un turno Cursor SDK añade la etapa de espera del primer resultado
(`sdk_wait_stage`), si esa espera sigue abierta
(`sdk_wait_waiting_for_initial_response`) y el tiempo transcurrido
(`sdk_wait_elapsed_seconds`). Sin evidencia utilizable los tres valores son
`unknown`. Una espera abierta acumula tiempo. Un cierre `interrupted` sin
handle nativo deja de contarse como espera activa y congela el tiempo en el
sello, o lo deja en `unknown` si el cierre no es medible; no afirma que el
handle o el resultado terminal existan. Con handle enlazado, el tiempo queda
congelado en ese enlace. Esa linea no interpreta silencio, CPU ni un run nativo `running`
como bloqueo o avance del modelo. No sustituye a `history` ni expone IDs
internos, prompts ni salidas raw. La cola y el preflight no aparecen como
duracion de fase.

Limite por defecto: 50 filas. `--limit` acepta valores positivos hasta 200; por
encima de 200 se trunca al maximo duro. Si hay mas intentos que el limite
efectivo, `truncated` es `true` en salida JSON.

## Uso de tokens observado

```bash
ai_dev_loop scheduler usage <run-id> --output text
ai_dev_loop scheduler sequence usage <sequence-id> --output json
```

Estas consultas autentican intentos y artefactos ya capturados; no llaman Cursor,
Codex ni servicios de billing. El scope directo se distingue del linaje de
recuperación y del linaje de fases. Los totales son cotas inferiores con cobertura
explícita: una captura ausente o interrumpida permanece `unknown`/`partial`, nunca
se convierte en cero. Los JSONL Codex históricos solo se proyectan cuando el
outcome autenticado fija ruta y hash; la lectura no crea observaciones ni cambia
los bytes originales.

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

## API local `integration` (Bridge)

```bash
ai_dev_loop integration info --output json
ai_dev_loop integration codex-capacity --output json
```

Emite un unico JSON por stdout. `info` no toca el ledger. `codex-capacity` observa
cupos Codex locales sin escribir SQLite ni artefactos protegidos; `observedAt` marca
el instante de la observacion, no la ultima espera del scheduler. Paginacion y
lecturas sensibles por trozos: ver [API de integracion local](../referencia/integration-api.md).

## Runs legacy

Los comandos `status`, `inspect` y `logs` de nivel superior para runs bajo `runs/`
fueron retirados en Phase 17.7. Para estado historico, consulta los artefactos en disco
o elimina el arbol legacy con `scheduler cutover cleanup` solo tras aceptacion humana
independiente (ver [Desinstalacion y limpieza](desinstalacion-limpieza.md)).
