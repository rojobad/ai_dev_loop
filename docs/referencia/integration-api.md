# API local de integración

La superficie `integration` expone un contrato JSON estable para que un Bridge
futuro supervise el scheduler local sin acceder a SQLite ni rutas privadas.

## Versión del contrato

- Versión actual de la API: **1.4** (`apiVersion.major` / `apiVersion.minor`).
- Misma major: compatible; campos desconocidos se ignoran en consumidores.
- Major distinta: el consumidor debe detenerse con `UPDATE_REQUIRED` y no emitir
  más peticiones de recursos.

## Formato de respuesta

Todas las órdenes bajo `integration` escriben **un único documento JSON** en stdout.

Éxito:

```json
{
  "apiVersion": { "major": 1, "minor": 4 },
  "ok": true,
  "observedAt": "2026-09-18T12:00:00Z",
  "data": {},
  "error": null
}
```

Error:

```json
{
  "apiVersion": { "major": 1, "minor": 4 },
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
  versión. En **1.4**, `runs`, `sequences`, `reviewInspection` y `processOutput`
  son `true`; `codexCapacity` permanece `false` hasta fases posteriores.

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
  línea) y disponibilidad del informe publicado.
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
