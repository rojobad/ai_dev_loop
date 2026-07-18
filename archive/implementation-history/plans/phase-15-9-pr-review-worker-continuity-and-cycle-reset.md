# Phase 15.9 — Continuidad del worker PR-review y aislamiento por ciclo

## Goal

Corregir el bloqueo observado en el run
`crypto-sentinel-20260718T115934Z-176634` sobre CryptoSentinel PR #45.
Después de publicar el trigger del ciclo externo 2, el worker detonado terminó
sin dejar un poller activo. Además, el estado conservó el conjunto congelado de
hilos del ciclo 1, por lo que los dos hilos nuevos del ciclo 2 se tratarían como
drift aunque se reactivara el worker.

La fase debe:

1. Mantener un único worker vivo al pasar de publicación a polling.
2. Aislar el conjunto de hilos elegibles esperado por ciclo externo.
3. Permitir que el controlador A reenganche de manera explícita e idempotente
   un ciclo `awaiting_bot_review` cuyo worker registrado ya no vive, sin
   repetir efectos GitHub.

## Non-Goals

- No volver a publicar `@codex review`, crear un PR, hacer commit/push ni
  retargetear la rama durante la reanudación.
- No editar manualmente el estado ni los artefactos del run afectado.
- No volver a ejecutar Cursor para un ciclo que ya tiene un chat y una
  corrección publicada; Cursor sólo podrá recibir un prompt posterior si Codex
  B adjudica hallazgos accionables en el flujo normal.
- No modificar las políticas de acknowledgements/no-findings, SSH, merge,
  schemas de respuesta de Codex ni la recuperación `reviewing` de Phase 15.8.
- No interpretar Markdown, JSONL, reacciones o `last_error` como decisiones de
  adjudicación.

## Scope

- La transición `publishing_external_fix -> awaiting_bot_review` del worker
  en `src/ai_dev_loop/commands/pr_review.py`.
- El reset de datos de elegibilidad que son estrictamente de un ciclo externo.
- La detección segura de un worker de PR-review vivo/ausente/stale y la vía
  `pr-review resume` para reenganchar polling cuando el estado ya es
  `awaiting_bot_review`.
- Status, CLI/help, documentación, eventos seguros y tests del comportamiento
  de continuidad y recuperación.

## Out of Scope

- No cambiar `ai_dev_loop.yaml`, modelos configurados, hooks de Codex Desktop
  ni el proyecto CryptoSentinel.
- No crear un sucesor inmutable para el run actual: no está terminal; se debe
  reenganchar el mismo run sólo si el worker registrado no está vivo.
- No borrar locks de un proceso vivo, señalizar procesos, resetear Git ni
  reescribir la historia de la rama.
- No borrar artefactos históricos de ciclos anteriores. El reset aplica sólo a
  campos de estado que congelan la selección del siguiente ciclo.

## Required Context

- Run afectado: `crypto-sentinel-20260718T115934Z-176634`, PR #45, ciclo 2,
  estado `awaiting_bot_review`, SHA `c29e15e6608a...`. El bot ya dejó dos
  hilos nuevos sin resolver sobre ese SHA; no fueron adjudicados, respondidos
  ni resueltos.
- El lock `locks/pr-review-worker.json` conserva un PID que ya no vive. El
  worker publicó el trigger y retornó al encontrar
  `publishing_external_fix`; `_publish_external_fix` intentó spawnear otro
  worker, pero el helper vio el PID del worker actual como vivo y no creó uno.
- En `_run_pr_review_worker_loop_inner`, el camino de publicación hace
  `_publish_external_fix(...); return`. La corrección debe conservar el mismo
  worker y continuar el loop sólo cuando el estado persistido resulte
  `awaiting_bot_review`.
- `_run_publication_pipeline` limpia `eligible_thread_ids` para un nuevo ciclo
  pero actualmente no limpia `expected_eligible_thread_ids`. Este último es
  un snapshot no vacío que activa la comparación exacta y debe ser `None` al
  comenzar un nuevo ciclo; los hilos ya procesados/resueltos siguen siendo
  acumulativos para impedir reprocesos.
- La identidad A/B, el PR, el SHA ligado, el request marker y el chat Cursor
  son contratos de recuperación. El controller A exacto sigue siendo requisito
  para `pr-review resume`; Codex B se toma sólo de `state.codex.session_id`.
- Fases relacionadas: Phase 15.6 (polling y no-findings), Phase 15.7
  (adjudicación/recovery externa) y Phase 15.8 (recovery local reviewing).

## Cursor Rules And Skills

Lee y aplica todas las reglas de `.cursor/rules/`, en especial:

- `ai-dev-loop-governance.mdc`;
- `ai-dev-loop-orchestrator-contracts.mdc`;
- `ai-dev-loop-state-and-schema-contracts.mdc`;
- `ai-dev-loop-loop-and-resume-contracts.mdc`;
- `ai-dev-loop-codex-review-contracts.mdc`;
- `ai-dev-loop-global-integrations-contracts.mdc`;
- `ai-dev-loop-abort-contracts.mdc`; y
- `ai-dev-loop-docs-acceptance-contracts.mdc`.

Para documentación usa la skill `ai-dev-loop-docs-acceptance-governance`. No
uses la skill de staged review durante implementación. Los tests usan fakes de
`agent`, `codex` y `gh`; no ejecutan modelos, GitHub ni credenciales reales.

## Architecture Guardrails

- Un worker detonado que publica un fix debe continuar él mismo a polling; no
  debe depender de auto-spawnearse mientras aún posee su propio PID. Separar
  explícitamente el caso de publicación síncrona (que sí necesita programar un
  worker) del caso de publicación ejecutada desde el worker.
- Debe existir a lo sumo un worker por run. Antes de crear uno, validar que el
  lock pertenece al run y que su PID vive; un worker vivo implica no-op seguro,
  y un lock stale se reemplaza atómicamente sin borrar locks de otro proceso.
- `resume` en `awaiting_bot_review` es sólo una reanudación de polling: exige
  el controlador A exacto, toma locks de run/repo, no cambia PR/SHA/marker,
  no crea Cursor ni invoca Codex/GitHub durante el comando. El worker posterior
  conserva el mismo run y ejecuta el polling normal.
- El conjunto `expected_eligible_thread_ids` pertenece a una sola ventana de
  review: limpiarlo a `None` al publicar el siguiente trigger. Preservar
  `processed_thread_ids` y `resolved_thread_ids`, y no confundir la lineage de
  recovery `external_adjudication` con el snapshot operativo del siguiente
  ciclo.
- Antes de adjudicar, el worker vuelve a obtener PR/head/hilos y conserva las
  validaciones normales de SHA, PR abierto, autor permitido, fecha posterior al
  trigger y filtro de hilos ya procesados. Hilos nuevos deben ser elegibles; no
  son drift de un ciclo anterior.
- No usar `--last`, no inferir IDs de sesión, no ejecutar texto de agentes ni
  usar `shell=True`. Estado, launcher y eventos siguen siendo atómicos,
  redactados y bajo XDG, sin cuerpos de hilos/prompt/patch/session IDs completos
  en salida humana.
- Si el estado es ambiguo, el lock refiere a un proceso vivo, el PR/SHA ya
  deriva o faltan artefactos obligatorios, fallar seguro sin disparar un nuevo
  worker ni publicar nada.

## Implementation Plan

1. Extraer/ajustar el contrato de programación de worker para distinguir:
   publicación síncrona desde `create`/comando (programa un worker tras quedar
   en awaiting) y publicación desde el worker detonado (no se auto-spawnea;
   continúa el mismo loop). Mantener el lock y la metadata activa coherentes en
   ambos caminos.
2. En `_run_pr_review_worker_loop_inner`, tras una publicación desde worker,
   recargar estado durable. Si quedó exactamente `awaiting_bot_review` con
   lifecycle correspondiente, continuar la iteración de polling; si quedó
   terminal, atención de usuario, interrupción o publicación incompleta,
   retornar/fallar según el estado existente. No hacer polling con estado
   transitorio ni duplicar publicación.
3. Al publicar el trigger de un nuevo ciclo externo, resetear
   `expected_eligible_thread_ids` a `None` junto con los campos ya reiniciados
   de ciclo (`eligible_thread_ids`, acknowledgement y no-findings). Conservar
   los IDs procesados/resueltos acumulados y los artefactos de ciclos previos.
   Actualizar tests de modelo/schema sólo si son necesarios por invariantes ya
   existentes; no agregar un schema nuevo sin necesidad.
4. Añadir una consulta interna segura de liveness del worker basada en launcher
   validado y usarla en `pr-review resume`. Ampliar el comando para aceptar un
   run no terminal sólo cuando esté en `awaiting_bot_review` y el worker esté
   ausente/stale: validar A/B y locks, conservar el estado, registrar un evento
   seguro de reenganche y lanzar exactamente un poller. Si el worker está vivo,
   retornar una respuesta idempotente sin mutación ni segundo proceso.
5. Mantener los caminos existentes de `resume` para `interrupted`, publicación
   y `reviewing` de Phase 15.8. No convertir el run afectado a `interrupted` ni
   usar `recover`; el reenganche debe poder usar el mismo run y sus dos hilos
   actuales.
6. Añadir status/inspect seguro con liveness del worker (live/stale/absent) y
   una siguiente acción clara para `awaiting_bot_review` sin poller. No exponer
   PID, token, argv completo, contenido de comentarios ni IDs completos de
   sesiones en salida humana.
7. Actualizar README y documentación de flujo, CLI, estado/artefactos y
   troubleshooting: cómo funciona la continuidad post-publicación, qué significa
   un worker stale, cómo A reengancha polling sin re-postear el trigger y cómo
   el snapshot de hilos se reinicia por ciclo.

## Testing Criteria

- Unitarios de launcher/liveness:
  - launcher válido con PID vivo evita spawn duplicado;
  - launcher stale se reemplaza sólo para el mismo run y permite un único
    worker nuevo;
  - launcher malformado, con run distinto o PID ambiguo falla seguro;
  - status humano/JSON informa liveness sin filtrar token, PID, argv o sesiones.
- Integración de publicación→polling con fakes:
  - un worker que publica un fix entra al mismo loop `awaiting_bot_review` y
    detecta/adjudica feedback del ciclo 2 sin auto-spawn;
  - una publicación iniciada fuera del worker programa exactamente un poller;
  - no se publica un segundo request marker, no se crea segundo Cursor ni se
    duplican commits/pushes/replies/resolutions.
- Integración de aislamiento de ciclo:
  - tras ciclo 1 procesado/resuelto, publicar ciclo 2 limpia el snapshot
    esperado;
  - los dos IDs nuevos del ciclo 2 se aceptan como conjunto observado y no
    generan `thread_set_drift`;
  - IDs ya procesados siguen excluidos y un drift real dentro del mismo ciclo
    continúa deteniéndose en atención de usuario.
- Integración de reenganche:
  - `pr-review resume` para el estado real `awaiting_bot_review` sin worker
    vivo exige el controller A exacto, inicia un solo poller y no escribe en
    GitHub durante el comando;
  - con worker vivo es idempotente/no-op; con controlador incorrecto, estado
    distinto, PR/head drift o lock ambiguo rechaza sin side effects;
  - el run comparable a `crypto-sentinel-20260718T115934Z-176634` se reanuda,
    ve los dos hilos de ciclo 2 y los envía al Codex B preservado mediante fake
    Codex, nunca desde estado real del usuario.
- Ejecutar los tests con fakes; no hacer llamadas reales a Cursor, Codex,
  GitHub ni SSH.

## Validation

```bash
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -s \
  tests/integration/test_phase15_pr_review.py \
  tests/integration/test_phase15_5_independent_pr_review.py \
  tests/integration/test_phase15_7_adjudication_recovery.py \
  tests/integration/test_phase15_8_reviewing_recovery.py
TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run mkdocs build --strict
git diff --check
```

No ejecutar `pr-review resume`, `recover`, `start`, Cursor real, Codex real ni
GitHub real durante implementación/validación. La reanudación de PR #45 sólo
se realizará tras revisión staged, instalación local de la versión aceptada y
autorización explícita del usuario.

## Risks Or Recovery Notes

- El run actual no debe abortarse ni alterarse mientras se implementa: conserva
  el marker de ciclo 2 y las dos observaciones abiertas. Una versión corregida
  debe retomarlo con un solo comando desde A, no creando una cadena de
  sucesores.
- La reanudación posterior esperada será:

  ```bash
  cd ~/Projects/crypto-sentinel
  SSH_AUTH_SOCK="$HOME/.ssh/ai-dev-loop-ssh-agent.sock" \
    ai_dev_loop pr-review resume crypto-sentinel-20260718T115934Z-176634 \
    --controller-session-id <controller-A-vinculado>
  ```

  Debe reenganchar el polling y luego adjudicar los hilos existentes; no debe
  publicar otro `@codex review`.

## OpenQuestions

None.
