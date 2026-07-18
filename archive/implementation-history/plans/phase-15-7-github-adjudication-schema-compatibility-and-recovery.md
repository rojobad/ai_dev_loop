# Phase 15.7 — Compatibilidad de adjudicación GitHub y recuperación sin retrigger

## Goal

Corregir el fallo de adjudicación de feedback GitHub que ocurrió en el run
`crypto-sentinel-20260718T010234Z-317683`: Codex detectó dos hilos elegibles
en PR #45, pero el backend rechazó el schema de salida por usar
`uniqueItems`. Ningún hilo fue adjudicado, respondido o resuelto y no se creó
Cursor.

La fase debe:

1. Hacer compatible el schema de salida de adjudicación con el backend actual
   de Codex sin relajar las invariantes de aplicación.
2. Añadir una recuperación explícita y auditable para un run de PR-review que
   falló antes de cualquier side effect de adjudicación. La recuperación debe
   reutilizar el mismo PR, SHA, trigger, dos hilos, sesión Codex B y controlador
   A; nunca debe publicar un segundo `@codex review`.

## Non-Goals

- No cambiar, borrar, responder o resolver manualmente los comentarios de PR
  #45 durante la implementación.
- No reintentar automáticamente ni reabrir cualquier run `failed`.
- No usar Markdown, regex de observaciones, ni el cuerpo de los comentarios
  como fuente de decisiones de adjudicación.
- No modificar la política de no-merge, SSH, publicación GitHub ni las reglas
  de finalización sin hallazgos de Phase 15.6.
- No recuperar fallos de Codex arbitrarios: esta fase cubre sólo el rechazo
  identificable del schema de salida antes de producir un resultado.

## Scope

- `github-pr-review-result-v1.json` y la validación runtime de schemas enviados
  a Codex.
- Clasificación segura del rechazo `invalid_json_schema` / `uniqueItems`.
- Nuevo comando `ai_dev_loop pr-review recover` con `--dry-run` para sucesores
  de adjudicación GitHub seguros.
- Estado, schema, eventos, status, CLI, documentación y tests necesarios para
  preservar lineage y reanudar con la identidad A/B original.

## Out of Scope

- El schema persistido `run-state-v1.json` puede mantener `uniqueItems` donde
  sea parte de su contrato JSON Schema; ese archivo nunca se envía como
  `response_format` a Codex.
- No cambiar el schema ni la semántica de `github-publication-text-v1.json`
  salvo que una validación compartida pruebe que también es incompatible.
- No añadir un segundo trigger, crear un nuevo PR, hacer commit/push, o usar
  `pr-review prepare` para recuperar el run conocido.
- No editar manualmente `state.json`, restaurar un run fallido in-place, ni
  mutar un source terminal.
- No resolver el falso negativo de `ssh-add` cuando el socket sólo está en
  `~/.ssh/config`; es una corrección independiente.

## Required Context

- Fallo real: run `crypto-sentinel-20260718T010234Z-317683`, PR #45, SHA
  `0d9c4ce473d9…`, dos hilos elegibles, cero hilos procesados/respondidos/
  resueltos, sin Cursor. El error seguro fue `invalid_json_schema` porque
  `eligible_thread_ids` tenía `uniqueItems` en el schema response-format.
- Ejecutor: `src/ai_dev_loop/runners/codex_github.py`;
  clasificación/polling: `src/ai_dev_loop/commands/pr_review.py`;
  prepare/start independiente: `src/ai_dev_loop/commands/pr_review_independent.py`.
- Schema de salida: `src/ai_dev_loop/schemas/github-pr-review-result-v1.json`.
  La unicidad también se valida con Pydantic en
  `src/ai_dev_loop/github_pr_review_result.py` y debe conservarse allí.
- Estado/schemas: `src/ai_dev_loop/state.py` y
  `src/ai_dev_loop/schemas/run-state-v1.json`; recuperación existente:
  `src/ai_dev_loop/commands/recover.py`.
- CLI: `src/ai_dev_loop/cli.py`; tests base:
  `tests/unit/test_github_pr_review_result.py`,
  `tests/unit/test_github_client.py`,
  `tests/integration/test_phase15_pr_review.py` y
  `tests/integration/test_phase15_5_independent_pr_review.py`.

## Cursor Rules And Skills

Lee y aplica todas las reglas `.cursor/rules/ai-dev-loop-*.mdc`, en particular
`governance`, `orchestrator-contracts`, `state-and-schema-contracts`,
`loop-and-resume-contracts`, `global-integrations-contracts`,
`codex-review-contracts`, `abort-contracts` y `docs-acceptance-contracts`.

Para documentación usa la skill `ai-dev-loop-docs-acceptance-governance`.
No uses la skill de staged review mientras implementas; Codex la ejecutará
después. No ejecutes modelos reales, Cursor real, Codex real ni GitHub real en
tests o validación.

## Architecture Guardrails

- Quitar `uniqueItems` sólo del schema de respuesta que se entrega a Codex.
  La protección de IDs únicos permanece en `GithubPrReviewResult` y sus
  validadores cruzados; no aceptar duplicados por el cambio de transporte.
- Antes de lanzar Codex, validar localmente que el schema de output no contiene
  keywords conocidos como incompatibles con el response-format. La validación
  debe ser determinista, operar sobre JSON parseado y producir un error seguro;
  nunca requiere una llamada de modelo para descubrirlo.
- Si Codex devuelve el rechazo estructural conocido, clasificarlo como
  `adjudication_schema_incompatible`, persistir sólo un código/mensaje seguro y
  dejar el run recuperable. No copiar el error JSONL crudo a `last_error`.
- `pr-review recover` sólo admite un run `failed` con ese código, PR abierto,
  `bound_head_sha` aún igual al head remoto, trigger ya persistido, hilos
  elegibles no vacíos y sin side effects: cero `processed_thread_ids`,
  `replied_thread_ids`, `resolved_thread_ids`, Cursor chat, resultado Codex,
  publicación local o push posterior al trigger.
- El recovery crea un sucesor inmutable, nunca altera el source terminal. Debe
  mantener exactamente `codex.session_id` (B), `controller.session_id` (A),
  modelo/razonamiento congelados, plan/prompt snapshots, PR, SHA, comentario
  trigger y timestamp de solicitud.
- El sucesor guarda lineage tipado/versionado: source run, ciclo externo,
  checkpoint `external_adjudication`, reason
  `github_adjudication_schema_incompatible`, y los IDs esperados de los hilos;
  no guarda cuerpos, prompts ni IDs de sesión completos en eventos públicos.
- `recover` y `recover --dry-run` no escriben en GitHub ni en el repositorio.
  El recovery normal deja al sucesor en un checkpoint `interrupted` que sólo el
  controlador A original puede reanudar con `pr-review resume`.
- Antes de reanudar Codex B, releer el PR y exigir que el conjunto de hilos
  elegibles, no resueltos y ligados al SHA coincida exactamente con el conjunto
  guardado. Ante cierre de PR, drift de SHA, hilos añadidos/eliminados/resueltos
  o worker activo, detenerse en atención de usuario sin crear Cursor, publicar,
  contestar ni resolver.
- Tras la reanudación válida, Codex B recibe los hilos mediante el flujo normal
  y validado. Cursor se crea sólo si el resultado estructurado confirma que
  todos los hallazgos son accionables.
- No usar `--last`, no inferir IDs de sesiones, no ejecutar texto de agentes y
  no usar `shell=True`.

## Implementation Plan

1. Añadir un validador/revisor local para schemas de `response_format` usados
   por los runners Codex. Debe recorrer el JSON y rechazar `uniqueItems` con un
   diagnóstico corto que identifique el schema/ruta; aplicarlo tanto a la
   adjudicación GitHub como a otros schemas sólo si son enviados por el mismo
   camino. No alterar schemas persistidos.
2. Eliminar `uniqueItems` únicamente de
   `github-pr-review-result-v1.json`. Mantener y reforzar, si hiciera falta,
   los validadores Pydantic que exigen IDs únicos y cobertura exacta entre
   `eligible_thread_ids` y `thread_decisions`.
3. En `codex_github.py`, convertir un rechazo API comprobable de
   `invalid_json_schema` en un error tipado seguro. No analizar Markdown ni
   depender de frases libres; usar los eventos/metadata ya persistidos y una
   clasificación mínima que no exponga cuerpos de feedback.
4. En `pr_review.py`, mapear ese error al outcome
   `adjudication_schema_incompatible` y estado `interrupted` para fallos nuevos.
   Preservar el comportamiento `failed` para output inválido, falta de schema,
   y otros fallos que no cumplen el contrato seguro.
5. Diseñar `pr-review recover <failed-pr-review-run-id> [--dry-run]`:
   - analizar elegibilidad sin mutar source ni repositorio;
   - reconocer también el run histórico actual mediante sus artefactos seguros
     aunque su antiguo `worker_outcome` sea `worker_error`;
   - crear de forma idempotente un sucesor de recuperación con estado
     `interrupted`, lifecycle `interrupted` y lineage tipado;
   - copiar artefactos de identidad/auditoría necesarios y registrar un evento
     sin contenido sensible;
   - mostrar el comando exacto de `pr-review resume` y que no se republicará el
     trigger. No iniciar worker desde `recover`.
6. Extender `RecoveryState`, `GithubPrReviewState`, `run-state-v1.json`,
   renderizadores de status y manifests con los campos mínimos de lineage y
   conjunto esperado de hilos. Mantener lectura de todos los estados Phase
   15/15.5 históricos sin esos campos. Añadir invariantes que impidan recovery
   tras cualquier side effect.
7. En el camino de `pr-review resume` de un sucesor de adjudicación, validar
   PR/head/hilos antes de invocar Codex. Si la comparación exacta falla,
   persistir un checkpoint de atención de usuario con razón segura y no hacer
   writes GitHub. Si coincide, continuar el polling/adjudicación normal con la
   misma sesión B.
8. Actualizar help CLI, README, configuración/referencia del flujo, status y
   troubleshooting con: schema compatible, outcome recuperable, dry-run,
   sucesor inmutable, controlador A requerido y el procedimiento de recuperación
   de PR #45. Documentar explícitamente que no se re-postea `@codex review`.

## Testing Criteria

- Unitarios del schema/result:
  - el schema GitHub enviado a Codex no contiene `uniqueItems` en ninguna ruta;
  - un payload con IDs duplicados sigue siendo rechazado por Pydantic;
  - cobertura de decisiones/IDs sigue siendo exacta;
  - el validador local rechaza un fixture con `uniqueItems` y no rechaza el
    schema corregido.
- Unitarios del runner/clasificación con ejecutables Codex falsos:
  - el rechazo `invalid_json_schema` se clasifica al código seguro sin filtrar
    JSONL/stderr crudo;
  - fallos no relacionados continúan como `worker_error`/`failed`;
  - no se llama un modelo real.
- Integración Phase 15 y 15.5:
  - un nuevo fallo compatible termina `interrupted` y conserva los dos IDs;
  - `pr-review recover --dry-run` no crea estado ni toca GitHub/repositorio;
  - `recover` deja el source `failed` inmutable y crea un único sucesor
    idempotente con el mismo PR/SHA/trigger, Codex B y A;
  - `resume` del sucesor no llama `post_issue_comment`, no crea Cursor y pasa
    los dos hilos al fake Codex con la sesión exacta;
  - cuando el fake Codex devuelve decisiones accionables, el flujo normal crea
    un solo chat Cursor y conserva la continuidad;
  - drift de SHA, PR cerrado, thread set distinto, hilos procesados, cursor ya
    existente, artefactos ausentes y un run `failed` de otra causa se rechazan
    sin side effects;
  - test de compatibilidad del run histórico
    `crypto-sentinel-20260718T010234Z-317683` mediante fixtures anonimizados,
    nunca leyendo el estado real del usuario desde los tests.
- Regresión de schema/estado/CLI: fixtures históricos sin los nuevos campos
  cargan, `status` no expone cuerpos ni sesiones completas, y el comando nuevo
  muestra una siguiente acción segura.

## Validation

```bash
uv run pytest tests/unit/test_github_pr_review_result.py tests/unit/test_github_client.py tests/integration/test_phase15_pr_review.py tests/integration/test_phase15_5_independent_pr_review.py
uv run pytest
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run mkdocs build --strict
git diff --check
```

No ejecutar `pr-review recover`, `resume`, `start`, Cursor real, Codex real ni
llamadas GitHub reales como parte de la implementación o las pruebas. La
aceptación del run #45 ocurre sólo después de revisión humana explícita de los
cambios staged e instalación local de la versión corregida.

## Risks Or Recovery Notes

- Un schema de transporte menos restrictivo no puede reducir las invariantes de
  dominio: Pydantic sigue siendo la autoridad para unicidad/cobertura.
- Reanudar sin comparar los hilos podría adjudicar feedback nuevo o ya resuelto;
  la igualdad exacta del conjunto es un freno deliberado.
- El source `failed` conserva evidencia; el sucesor permite auditoría y evita
  editar estado manualmente. Si se repite el error o hay drift, no reintentar:
  detenerse y exponer la causa segura.
- Para el run actual, el orden posterior a una implementación aceptada será:

  ```bash
  ai_dev_loop pr-review recover crypto-sentinel-20260718T010234Z-317683 --dry-run
  ai_dev_loop pr-review recover crypto-sentinel-20260718T010234Z-317683 --output json
  # Desde el controlador A vinculado al sucesor:
  ai_dev_loop pr-review resume <successor-run-id>
  ```

  B permanece inactiva hasta que el worker reanude su sesión exacta. No se
  comenta otro `@codex review`.

## OpenQuestions

None.
