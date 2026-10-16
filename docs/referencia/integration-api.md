# API local de integración

La superficie `integration` expone un contrato JSON estable para que un Bridge
futuro supervise el scheduler local sin acceder a SQLite ni rutas privadas.

## Versión del contrato

- Versión actual de la API: **1.9** (`apiVersion.major` / `apiVersion.minor`).
- Misma major: compatible; campos desconocidos se ignoran en consumidores.
- Major distinta: el consumidor debe detenerse con `UPDATE_REQUIRED` y no emitir
  más peticiones de recursos.

## Formato de respuesta

Todas las órdenes bajo `integration` escriben **un único documento JSON** en stdout.

Éxito:

```json
{
  "apiVersion": { "major": 1, "minor": 9 },
  "ok": true,
  "observedAt": "2026-09-18T12:00:00Z",
  "data": {},
  "error": null
}
```

Error:

```json
{
  "apiVersion": { "major": 1, "minor": 9 },
  "ok": false,
  "observedAt": "2026-09-18T12:00:00Z",
  "data": null,
  "error": { "code": "INVALID_ARGUMENT", "message": "explicación segura" }
}
```

Códigos estables y códigos de salida:

| Código | Salida |
|--------|--------|
| `INTERNAL_ERROR` | 1 |
| `INVALID_ARGUMENT` | 2 |
| `NOT_FOUND` | 3 |
| `UNSUPPORTED` | 4 |
| `DATA_INTEGRITY` | 5 |
| `IO_ERROR` | 6 |

Los errores de argumentos y órdenes desconocidas dentro de `integration` usan este
sobre; no modifican el formato de error humano del resto de la CLI.

## `integration info`

```bash
ai_dev_loop integration info [--output json]
```

- `--output` es una opción del subcomando `info` (no del grupo `integration`).
- Solo admite `json` (valor por defecto en `info`).
- No requiere ledger del scheduler, no crea directorios XDG y no sondea Codex.
- `data.aiDevLoopVersion` es la versión del paquete instalado.
- `data.capabilities` declara de forma honesta qué lecturas existen en esta
  versión. En **1.9**, `runs`, `sequences`, `reviewInspection`, `processOutput`,
  `codexCapacity` y `dashboard` son `true`.

## Capacidad Codex local (API 1.5)

```bash
ai_dev_loop integration codex-capacity [--output json]
```

- Observación **bajo demanda** del contexto Codex local: resuelve el ejecutable
  `codex` en `PATH` del usuario del proceso (mismo requisito de despliegue que
  el Bridge en una sola instalación WSL).
- Reutiliza la sonda app-server acotada del scheduler (initialize → initialized →
  `account/rateLimits/read`); **no** escribe en el ledger, **no** cachea historial
  y **no** autoriza reanudar esperas del scheduler.
- `data.status`: `available`, `exhausted` o `unavailable`. `data.reason` solo
  cuando `unavailable` (motivos seguros; sin stderr ni payload de cuenta).
- `data.limits` lista ventanas numéricas válidas (`id`, `window`, porcentajes,
  duración/reinicio opcionales). Clasificación idéntica a la sonda del scheduler
  en formas soportadas.
- El hijo puede usar su configuración Codex local habitual; la API no promete
  cuota compartida entre instalaciones/ejecutables distintos en la misma máquina.

## Lectura de runs (API 1.1+)

Todas las órdenes aceptan `--output json` (único formato soportado).

```bash
ai_dev_loop integration runs list [--kind all|standalone|sequence] [--offset N --limit N]
ai_dev_loop integration run inspect RUN_ID
ai_dev_loop integration run attempts RUN_ID [--offset N --limit N]
ai_dev_loop integration run timeline RUN_ID [--offset N --limit N]
ai_dev_loop integration run history RUN_ID [--offset N --limit N]
ai_dev_loop integration run plan RUN_ID [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration run initial-prompt RUN_ID [--offset BYTE_OFFSET --limit BYTE_LIMIT]
```

- Las lecturas usan el ledger y artefactos del scheduler en modo **solo lectura**;
  no migran la base de datos, no adquieren reservas de escritura y no ejecutan Git.
- Los resúmenes (`list`, `inspect`, `history`) no incluyen cuerpos de prompts,
  revisiones ni salida de procesos. Los comandos `plan` e `initial-prompt` devuelven
  el artefacto congelado capturado en el submit (sensible; solo bajo demanda).
- `plan` / `initial-prompt` verifican el hash registrado antes de devolver cada trozo;
  no leen el worktree actual del repositorio.

## Lectura de secuencias (API 1.2)

```bash
ai_dev_loop integration sequences list [--offset N --limit N]
ai_dev_loop integration sequence inspect SEQUENCE_ID
ai_dev_loop integration sequence phase-runs SEQUENCE_ID --ordinal N [--offset N --limit N]
ai_dev_loop integration sequence phase-plan SEQUENCE_ID --ordinal N [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration sequence phase-prompt SEQUENCE_ID --ordinal N [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration sequence report SEQUENCE_ID [--offset BYTE_OFFSET --limit BYTE_LIMIT]
```

- Las lecturas usan el ledger y artefactos de secuencia en modo **solo lectura**;
  no migran la base de datos, no publican informes ni ejecutan Git.
- `inspect` incluye fases, linaje materializado (hasta 100 intentos por fase en
  línea) y disponibilidad del informe publicado. `attemptKind` puede ser
  `planned_run`, `same_reviewer_retry` o `cursor_retry`. El resumen no incluye
  prompts, parches ni el identificador completo del reviewer.
- `phase-plan` / `phase-prompt` leen la definición congelada de la secuencia, no
  el binding de un run materializado.
- `report` solo sirve `reports/completion-v1.json` ya publicado; ausencia explícita
  con `not_yet_produced` o `publication_pending`.

## Inspección de revisiones Codex (API 1.3)

```bash
ai_dev_loop integration run reviews RUN_ID [--offset N --limit N]
ai_dev_loop integration run review RUN_ID --attempt ATTEMPT_ID
ai_dev_loop integration run review-content RUN_ID --attempt ATTEMPT_ID --kind KIND [--offset BYTE_OFFSET --limit BYTE_LIMIT]
```

- `KIND`: `prompt`, `response`, `review-markdown`, `cursor-fix-prompt`.
- El índice lista todos los intentos Codex (reintentos incluidos) ordenados por
  iteración, `phaseAttempt` e `attemptId`.
- `review` expone metadatos y la respuesta estructurada validada cuando existe;
  `resultState` distingue `valid`, `invalid`, `not_produced` y `pending`.
- `review-content` devuelve bytes exactos por intento; los intentos anteriores a
  21.4 pueden carecer de `prompt` (`not_recorded`). No se reconstruyen prompts
  históricos.
- El scheduler registra antes de cada lanzamiento Codex:
  `codex/reviews/NN.<attempt_id>.prompt.txt` y
  `codex/reviews/NN.<attempt_id>.prompt-evidence.json` (esquema
  `scheduler-review-prompt-evidence-v1.json`).

## Salida de procesos hijo (API 1.4)

```bash
ai_dev_loop integration run output RUN_ID --attempt ATTEMPT_ID --stream stdout|stderr [--offset BYTE_OFFSET --limit BYTE_LIMIT]
```

- Devuelve los bytes capturados del **proceso hijo** (eventos Cursor/Codex o stderr),
  no el JSON de resultado del runner en `attempts/<id>/stdout.txt`.
- `availableBytes` es el tamaño observado en disco **antes** de la lectura (snapshot);
  las lecturas se limitan a ese snapshot y un shrink/reemplazo durante la lectura
  devuelve `DATA_INTEGRITY`.
- `complete` indica ejecución resuelta con escritor detenido (`completed`/`failed`,
  o `cancelled` solo tras reconciliación de aborto con el intento marcado `ingested`
  en el ledger del scheduler, sin exigir sobre de resultado autenticado).
  Independientemente de si quedan trozos por leer. Con `complete=true` puede
  haber `hasMore=true` en lecturas paginadas. Intentos `uncertain`, `launching`
  o `active` permanecen `complete=false`; los `cancelled` pendientes de
  reconciliación también. En EOF pollable con escritor activo, `hasMore=false` y
  `nextOffset` apunta al final observado; con escritor detenido, `nextOffset=null`.
- `truncatedAtSource` refleja el metadato final cuando existe; en intentos activos
  o históricos sin sidecar puede ser `null`.
- Los descriptors `processOutput` en `integration run review(s)` enlazan la misma
  operación `output` por intento y stream.

## Convenciones compartidas

- Identificadores opacos en camelCase; marcas de tiempo UTC RFC3339.
- Paginación: `--offset` / `--limit` con metadatos `items`, `offset`, `limit`,
  `nextOffset`, `hasMore`.
- Lecturas de artefactos por trozos en base64 con metadatos de offset; sin API
  genérica de rutas arbitrarias.

Los esquemas empaquetados viven en `src/ai_dev_loop/schemas/integration-api-*.json`.

## Consultas compartidas del dashboard (API 1.9)

```bash
ai_dev_loop integration dashboard projects [--offset N --limit N]
ai_dev_loop integration dashboard summary --from UTC --to UTC [--project NAME] [--time-zone IANA]
ai_dev_loop integration dashboard runs --from UTC --to UTC [--project NAME] [--time-zone IANA] [--offset N --limit N]
ai_dev_loop integration dashboard activity --from UTC --to UTC --metric submissions|review-rounds|process-hours [--project NAME] [--time-zone IANA]
```

- `from` es inclusivo y `to` exclusivo; ambos se normalizan a UTC. El rango
  máximo es 366 días. `timeZone` usa un nombre IANA y por defecto es
  `America/La_Paz`.
- `summary` y `runs` usan la cohorte cuyo primer lanzamiento de ejecución está
  dentro del rango. Incluyen turnos de implementer Cursor/Codex y reviews Codex,
  pero excluyen creación de chat, preflight y sondas. `runs` conserva provider y
  linaje de secuencia, incluido `implementer_retry`.
- `activity` fecha submissions y rounds de review por evento, no por la cohorte
  iniciada. Cada `(runId, iteration)` aporta como máximo un round. `process-hours`
  devuelve segundos de procesos resueltos, suma procesos paralelos y los divide
  por medianoche en la zona indicada; no inventa duración activa o incierta.
- Todos los resultados incluyen `observedAt`, filtros canónicos y cobertura
  explícita. Los totales de `summary` no dependen de la página de `runs`.
- Cada elemento `projects[]` de `summary` incluye `otherStates[]` con el valor
  crudo exacto de cada estado no reconocido y su `count`. Sus etiquetas son
  únicas, están ordenadas y la suma de sus conteos coincide exactamente con
  `stateCounts.other`; el campo evita derivar el agregado desde páginas de
  `dashboard.runs`.
- Las lecturas abren un snapshot SQLite validado en modo solo lectura. No migran,
  reparan ni controlan el scheduler. Tienen límites de 10.000 runs relevantes,
  100.000 intentos de ejecución contribuyentes, 5 segundos y 240 KiB; al
  excederlos fallan sin totales parciales. La admisión usa la operación y el rango
  solicitados sin perder la validación del historial autoritativo leído.
- Un ledger ausente produce colecciones vacías sin crearlo. Esquemas no
  soportados, corrupción y fallos de lectura usan los códigos seguros existentes.
- La consulta no incluye prompts, salida de procesos, sesiones, rutas privadas ni
  capacidad Codex actual. Esos contenidos conservan sus interfaces explícitas.

## Implementer, capacidad y timeout por rol (API 1.7+)

Los resúmenes de run incluyen `implementerProvider`; inspect agrega
`codexPendingRole`, `codexPendingTurn` y `codexCapacityReason` en espera de capacidad.
Sequence inspect agrega `currentImplementerProvider`, esos campos de espera y
`implementerProvider` por fase. Campos ausentes en inputs históricos conservan
Cursor; una espera sin continuación histórica conserva rol reviewer.
Run inspect y sequence inspect agregan `implementerRetryWaitUntil`,
`implementerTimeoutAutomaticRetries` e `implementerTimeoutRetryLimit` para el
timeout Codex de la hoja vigente. `safeNextAction.waitUntil` refleja esa espera; tras
agotarla queda `null` y la acción apunta a `scheduler implementer-retry`.
Status/history/timeline exponen provider/componente y rol/turno sin prompts,
respuestas ni IDs completos. El siguiente paso de espera implementer es tick.

Capabilities: `dualImplementers=true`, `mixedImplementerSequences=true`,
`codexImplementerManualRecovery=true`. API 1.8 la habilitó porque `--force` cubre
standalone y la hoja vigente de secuencia. `phase-runs` expone el intento aditivo
`implementer_retry`; los consumidores de la misma major deben tolerar este valor
nuevo. El retry acotado de timeout conserva los campos aditivos introducidos en 1.7.
`review(s)` sigue siendo el índice del reviewer. `run output` sirve stdout/stderr
de intentos `codex_implementer` bajo `implementer/turns/`, autenticando intent y
completion del rol correcto, paths confinados y hashes capturados cuando existen.
Los bytes son sensibles y se solicitan explícitamente, con los límites/snapshot
existentes. Metadatos ausentes en intentos históricos dan truncación desconocida;
no se fabrica contenido. Estas lecturas no migran ni mutan el ledger.

## Adaptador HTTP loopback del dashboard

`ai_dev_loop dashboard serve --port PORT` proyecta las mismas lecturas en proceso,
sin invocar la CLI como subprocess por petición. Enlaza solamente
`127.0.0.1:PORT` y acepta la ruta `POST /api/dashboard/read` con cuerpo JSON:

```json
{"operation":"dashboard.projects","parameters":{"offset":0,"limit":25}}
```

Las operaciones permitidas son `dashboard.projects`, `dashboard.summary`,
`dashboard.runs`, `dashboard.activity`, `run.inspect`, `run.timeline`,
`sequence.inspect`, `sequence.phase-runs`, `machine.codex-capacity`,
`run.initial-prompt`, `run.review`, `run.review-content` y `run.output`. No existe
dispatch genérico de comandos, SQL, argv o rutas. Los parámetros de rango usan
`fromUtc`, `toUtc`, `timeZone` y `projectName`; los identificadores usan `runId`,
`sequenceId` y `attemptId`. Páginas HTTP usan 25 por defecto y máximo 100. Las
lecturas de contenido usan `byteOffset` y `limit`, con máximo fijo de 65.536 bytes.

El cliente debe enviar `Host: 127.0.0.1:PORT` y, para la ruta API, el `Origin`
exacto `http://127.0.0.1:PORT`. No hay CORS wildcard, credenciales, login ni proxy
trust. El cuerpo de request está limitado a 16 KiB y el sobre de respuesta a
240 KiB. El servidor admite como máximo cuatro handlers y dos trabajos de
servicio, con una sola sonda de capacidad; saturación devuelve `503` sin crear una
cola ilimitada. Las respuestas usan `no-store`, CSP same-origin, `nosniff` y
`Referrer-Policy: no-referrer`.

`run.review` y `run.review-content` autentican exclusivamente intentos reviewer
Codex (`component=codex` más efecto de review); un intento
`codex_implementer` no es contenido reviewer. `run.output` conserva la
autenticación de output específica para Cursor, implementer Codex y reviewer.
Prompts y stdout/stderr se cargan solo por selección explícita, paginados y sin
cache durable del servidor. El servidor mantiene la semántica read-only de los
servicios existentes y no controla el scheduler.

El mapa local consume páginas explícitas de `dashboard.runs` y conserva cada
`runId` y binding de secuencia, incluidas generaciones de reemplazo. La línea de
tiempo seleccionada consume `run.inspect` y páginas de `run.timeline`: clasifica
por separado turnos implementer Cursor, turnos implementer Codex y reviews Codex,
y representa intentos sin lanzamiento o sin fin observado sin inventar duración.
Cambiar de run o de filtros cancela y retira respuestas anteriores. Estas vistas
no solicitan prompt, review-content ni output; ese contenido sigue requiriendo
una acción deliberada en su superficie asignada.
