# Phase 25.5 findings — integración y corte de Cursor SDK

Fecha: 2026-10-18 (America/La_Paz)

Estado de implementación: **LISTO PARA REVIEW INDEPENDIENTE**. Esto no declara
aceptación de fase, validación global, instalación, commit, push ni autorización
operativa.

## Autoridad y estado preservado

- El trabajo se completó manualmente en
  `codex/phase-25-sdk-replanned`, sobre
  `1e0f0bacb15738c78197316130fcb4ab74d4401b`, checkpoint de fase 25.4.
  La rama y HEAD no se cambiaron.
- Al inicio el índice estaba vacío y el worktree contenía 105 archivos
  modificados sin staging y cuatro archivos nuevos reportados por el operador.
  Se conservó ese trabajo y se corrigió incrementalmente; no se hizo reset,
  restore, clean, stash, checkout, commit ni push. El staging sólo se realizó
  después, por instrucción expresa del usuario, antes de esta última ronda de
  corrección.
- Se leyeron `AGENTS.md`, el plan y overview de fase 25, las diez reglas
  `.cursor/rules`, `create-cursor-plan` y
  `ai-dev-loop-docs-acceptance-governance`. Las skills de planificación y review
  no fueron modificadas. La skill de gobernanza mantuvo esta evidencia ligada a
  código, tests y CLI actuales, y mantuvo separados los límites operativos.
- No se controló el scheduler, no se invocaron modelos reales, no se instalaron
  paquetes en el entorno del usuario y no se tocaron credenciales, timer,
  lingering, ledger ni artefactos operativos.

## Correcciones completadas sobre la implementación existente

1. Se retiraron de `scheduler/cursor_attempt_runner.py` las funciones ejecutables
   de Cursor CLI (`create_chat` y `execute_prompt`) y sus imports. El worker ahora
   exige `runtime=sdk-local` antes de cualquier operación externa y sólo despacha
   create/send/reconcile del SDK.
2. Se añadió una política única por provider efectivo + runtime. `submit`, start,
   preflight, tick/dispatch, `cursor-retry`, `implementer-retry`, `review retry`,
   `extend`, start/materialización/handoff/recovery de secuencias rechazan Cursor
   CLI histórico. Un contexto Codex con campo Cursor inerte sigue siendo válido.
3. La validación de secuencias revisa la definición completa en start y también
   en materialización. Una hoja Cursor CLI posterior impide materializar incluso
   una primera hoja Codex; no se publican runs, artefactos ni checkpoints
   parciales.
4. La recuperación de review bloqueado ahora autentica y conserva checkpoints
   SDK: owner, conversation key, agent, native run, identity y journal. El
   successor usa el mismo store/agente y puede ejecutar la siguiente corrección
   sin recrear conversación ni modificar el source bloqueado.
5. La recuperación equivalente de un implementer Codex autentica el intento
   completado, sesión, binding, metadata y respuesta final. Se restauró cobertura
   de tamper, exceso de tamaño y symlink contra esos artefactos reales en vez de
   omitirla por el retiro del fixture CLI.
6. Se reforzó la línea de autoridad de successors SDK en dispatch y
   reconciliation: sólo una relación durable de review recovery o recovery SDK
   autorizada permite heredar owner/key/agent.
7. Se completaron los tests nuevos de corte y aceptación con callers de
   producción: guards sin mutación, secuencia histórica completa, flujo SDK
   initial/correction, review successor, ceiling+extend, métricas por rol/etapa,
   vistas read-only, Codex v1 sin Cursor, wheel base Python 3.11 y secuencias
   mixtas en ambas direcciones.
8. Se corrigió la guía de cutover para no avanzar ante holds sin resolver y para
   usar los valores de coverage reales: `complete`, `partial`, `unknown` y
   `not_applicable`.

## Correcciones posteriores al review independiente

El review independiente identificó dos P2 reales en la recuperación de review y
una ronda posterior encontró un caso faltante dentro del primer P2. Se
reprodujeron con callers de producción, se corrigieron y quedaron cubiertos sin
modificar el source bloqueado ni relajar autenticación:

1. **Replay nativo SDK autenticado.** Un send interrumpido conserva correctamente
   un journal terminal `uncertain`; el resultado exitoso aparece en una relación
   de reconciliación `ready`, no reescribiendo ese journal como `success`.
   `review_recovery.py` ahora autentica la cadena completa: journal e intento
   source, intent/record de reconciliación, intento/envelope de replay,
   `terminal_recovered`, native run, fingerprint y respuesta final. El successor
   de review se crea y completa sin reenviar el prompt de implementación; el
   contador de `send` permanece exactamente en uno.
2. **Respuesta Codex en el prompt del reviewer successor.** La respuesta final
   autenticada del intento ya se copiaba por su path attempt-specific, pero el
   wrapper lee `implementer/iterations/NN/final.txt`. El manifest de recovery
   ahora permite un destino explícito y publica los mismos bytes verificados en
   ese path canónico del successor. Una regresión inspecciona el prompt realmente
   publicado: contiene la respuesta original y no contiene el fallback de
   respuesta no disponible.
3. **Send perdido antes de enlazar el native run.** El journal válido de tres
   registros (`run_inventory_baseline`, `send_intent`, `terminal uncertain`) no
   contiene `native_run_bound` y por diseño conserva incompletos el agent/native
   ID del outcome source. Review recovery ahora acepta ese shape únicamente con
   el autenticador de journals de reconciliación y deriva los datos ausentes de
   la cadena verificada intent/record/replay. El flujo normal sigue exigiendo los
   IDs explícitos y cualquier ID presente, incluso en el source incierto, debe
   coincidir. La regresión descubre un único run terminado, autentica
   `terminal_recovered`, crea y completa el review successor y confirma un solo
   `send`.

El índice que ya contenía el patch sometido a review se preservó. Las dos
primeras correcciones se añadieron al índice sólo tras una instrucción expresa
del usuario; la corrección posterior de lost-send permanece unstaged encima de
los 114 paths preparados. No se reescribió ni se retiró contenido del índice.

Los módulos de tests que sólo creaban trabajo nuevo mediante Cursor CLI se
mantienen como helpers históricos no recolectables o casos explícitamente
superseded. No se usaron para obtener el verde de los contratos vigentes: sus
equivalentes SDK de identidad, timeout, capacidad, incertidumbre, abort, recovery,
secuencia y output se ejecutaron en las suites 25.2/25.3 seleccionadas. Las
assertions de integridad de artefactos que seguían siendo provider-independientes
se migraron a Codex y quedaron activas.

## Mapeo G-01–G-04

### G-01 — frontera final de ejecución: satisfecho

Evidencia de producción:

- `cursor_runtime_policy.py` clasifica el provider efectivo, no la mera presencia
  de un binding histórico.
- `submission.py`, `start.py`, `scheduler_preflight.py`, `attempt_service.py`,
  servicios de retry/recovery/extend y los callers de secuencia aplican la misma
  frontera.
- `cursor_attempt_runner.py` ya no contiene fallback ejecutable a Cursor CLI.
  `runners/cursor.py` y parsers/output legacy sólo permanecen para lectores y
  evidencia histórica; ninguna ruta de scheduler los llama para ejecución.
- Los flags exclusivos `--cursor-command`, `--cursor-model` y
  `--cursor-output-format` ya no aparecen en submit/prepare. Se preservan flags
  genéricos de implementer Codex y su preflight.
- Los errores son accionables y no convierten ni reescriben contextos,
  definiciones, aliases o hashes históricos.

Evidencia de tests:

- `test_phase25_5_cutover.py`: help actual, discriminación Codex/SDK/CLI, start y
  recovery check read-only, cuatro callers mutantes sin cambios de ledger o
  artefactos, y rechazo de secuencia completa antes de la primera
  materialización.
- `test_phase25_5_sdk_acceptance.py`: Codex desde config/contexto v1 completa
  initial+correction sin binario, clave ni SDK de Cursor.
- Los tests históricos/read-only y de replay seleccionados siguieron verdes.

### G-02 — integración independiente de ambos providers: satisfecho con gate global pendiente

Evidencia final fake-backed a través de producción:

- SDK standalone: initial, correction, review `findings/no_findings`, techo 1,
  `extend` explícito, staging final, reserva, identidad de B, owner/store/agent y
  ausencia de commit autónomo.
- Review recovery SDK: source bloqueado queda byte/estado-inmutable, el successor
  conserva identidad/journal y completa una corrección posterior con el mismo
  agente.
- Review recovery después de replay SDK: autentica la reconciliación terminal
  tanto con native ID previamente enlazado como cuando el send perdió la
  respuesta antes de `native_run_bound`; ambos completan el successor con un
  solo `send` total del implementer.
- Codex implementer/reviewer: sesiones independientes, bootstrap/resume exactos,
  initial+correction, respuesta autenticada presente en el prompt de review del
  successor y wheel base sin dependencia Cursor.
- Manifest v4 nombrado en órdenes Cursor SDK→Codex y Codex→Cursor SDK: inputs
  congelados, un checkpoint commit sólo en la fase no final y cambios finales
  staged.
- Fases 25.2/25.3: timeout, capacidad, proceso/descendientes, send ambiguo,
  reconciliación, successor inicial/corrección, abort, restart y reemplazo de
  hoja SDK.
- Fases 27.4–27.7: continuidad y recovery Codex, incluyendo timeout, capacity,
  force, abort y secuencia Codex-only.
- Métricas exactas del caso 25.5: total `58`, sin duplicación: Cursor implementer
  initial `21`, Cursor implementer correction `21`, Codex reviewer review `16`.
  Las métricas no cambian decisiones de review, Git ni recovery.
- Integration API/Dashboard: nombres, provider, filtros/paging, actividad y
  consultas read-only; se compararon bytes del ledger antes/después en el caso
  central.

El pytest global requerido por el plan **no se ejecutó** por decisión expresa del
usuario. No se repartió la suite completa en lotes ni se infiere validación global
desde las selecciones. Queda como gate posterior separado y su ausencia no difiere
ningún contrato funcional implementable de esta entrega.

### G-03 — empaquetado y operación: satisfecho; D-02 separado

- El build aislado ejecutó el hook de Hatch y produjo sdist y wheel.
- El test de empaquetado 25.1 construyó el wheel, instaló
  `[cursor-sdk]` con `cursor-sdk==1.0.37` en Python 3.11, ejecutó bridge health sin
  Node externo y verificó el adapter con transporte offline.
- El test 25.5 instaló el wheel base en un venv Python 3.11, confirmó que
  `cursor_sdk` no estaba instalado y completó un flujo Codex initial+correction.
- MkDocs estricto pasa. La documentación describe config v2, credencial por
  referencia, sandbox/settings, store mutable, retención, incertidumbre,
  recovery, métricas, historia read-only, short names/manifests y el cutover
  quiescente. Phase 24 sigue etiquetada como no implementada donde corresponde.
- No se ejecutaron instalación real, timer, Dashboard live ni smoke con modelo.

`node` y `npm` no existen en el shell de esta workstation. Los tres casos de
frontend/browser del installed Dashboard se omitieron exclusivamente con la razón
D-02 aprobada; no se instaló toolchain ni se eludió el build hook. Los seis casos
Python del installed Dashboard pasaron.

### G-04 — configuración fuente y aceptación final: implementación satisfecha; aceptación manual pendiente

`ai_dev_loop.yaml` contiene exactamente config v2 SDK-local con:

- SDK `1.0.37`, modelo `grok-4.7`;
- parámetros `context: 256k`, `reasoning_effort: high`, `fast: false`;
- `credential_ref: cursor-default`, `setting_sources: [project]` y sandbox
  `disabled`;
- valores Codex, workflow y prompt preexistentes conservados;
- sin `command`, `output_format`, `force` ni `trust_workspace` de Cursor CLI;
- sin seleccionar Codex como implementer por defecto ni crear una credencial.

El test exacto carga una copia byte-a-byte de esa fuente con credencial y catálogo
falsos y congela los valores esperados en el contexto v7. Este cambio no altera
inputs ya preparados. La aceptación manual independiente del propietario sobre el
diff exacto de `ai_dev_loop.yaml` sigue pendiente.

## Revalidación de F/W/R/U

| Contratos | Resultado y evidencia focalizada |
| --- | --- |
| F-01–F-04 | Verdes: 25.1 config/adapter/empaquetado verifica Python 3.11, extra pinneado, base sin SDK, referencias/roots seguros, errores tipados y replay público. |
| W-01–W-04 | Verdes: 25.2 contracts/worker/workflow verifica manifest v4, identidad durable, intents antes de efectos, captura acotada/redactada, timeout/abort de árbol de procesos y flujo SDK normal/mixto. |
| R-01–R-04 | Verdes: 25.3 recovery/uncertainty verifica continuación del mismo agente, successors y secuencia, adopción sin resend, cancelación, abort/restart y retención de reserva. La nueva recuperación de review SDK cubre el hueco de 25.5. |
| U-01–U-03 | Verdes: captura/publicación idempotente, agregación por scope/rol/etapa y lecturas CLI/API read-only sin runtime. |
| U-04 | Verde para fixtures históricos y proyección autenticada; D-01 permanece como riesgo residual aceptado para capturas genuinas pre-25 con tokens de ambos roles Codex. |
| U-05 | Backend/API/Dashboard Python verde y compatible; frontend/browser queda únicamente bajo D-02. |

Schema de ledger permanece 18; contextos SDK standalone/secuencia son v7/v8,
manifest fuente v4 se congela como definición/entry v3 e Integration API es 1.12.
No se agregó migración ni se reescribió historia para efectuar el corte.

## Comandos y resultados

Todos los pytest se ejecutaron con selección explícita. No se usó `pytest -q` sin
paths/casos ni scripts que reconstruyeran la suite completa.

### Estado final

1. Contratos propios de 25.5:

   ```text
   .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_5_cutover.py tests/integration/test_phase25_5_sdk_acceptance.py
   19 passed in 61.22s
   ```

2. Regresiones de autenticación añadidas/cercanas a los P2:

   ```text
   .venv/bin/python -m pytest -q tests/integration/test_phase25_5_sdk_acceptance.py::test_sdk_lost_send_response_replays_unique_run_then_recovers_review_without_resend tests/integration/test_phase25_5_sdk_acceptance.py::test_sdk_native_replay_can_recover_blocked_review_without_resending_prompt tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py::TestHistoricalRecoveryEligibility::test_codex_review_successor_prompt_contains_authenticated_implementer_response
   3 passed in 15.37s

   .venv/bin/python -m pytest -q 'tests/integration/test_phase25_2_sdk_workflow.py::test_sdk_lost_identity_result_holds_reservation_and_rejects_recovery[FAKE_CURSOR_SDK_SEND_UNCERTAIN-cursor_sdk_ambiguous_missing-3]'
   1 passed in 4.34s

   # Repetidas individualmente después del endurecimiento final del autenticador
   .venv/bin/python -m pytest -q tests/integration/test_phase25_5_sdk_acceptance.py::test_sdk_native_replay_can_recover_blocked_review_without_resending_prompt
   1 passed in 6.78s

   .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py::TestHistoricalRecoveryEligibility::test_codex_review_successor_prompt_contains_authenticated_implementer_response
   1 passed in 5.87s

   .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py::TestRecoveryArtifactAuthentication tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py::TestHistoricalRecoveryEligibility::test_bootstrap_resume_blocked_run_recovers_without_cursor tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py::TestHistoricalRecoveryEligibility::test_codex_review_successor_prompt_contains_authenticated_implementer_response
   9 passed in 30.61s

   .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py tests/integration/test_phase25_3_sdk_recovery.py::test_failed_worker_without_result_stops_owned_descendants_before_native_replay
   10 passed in 8.50s
   ```

3. Foundation, worker, recovery, métricas/historia y recovery Codex afectados:

   ```text
   .venv/bin/python -m pytest -q tests/unit/test_phase25_1_sdk_config.py tests/unit/test_phase25_1_sdk_adapter.py tests/integration/test_phase25_1_sdk_packaging.py tests/unit/scheduler/test_phase25_2_sdk_contracts.py tests/unit/scheduler/test_phase25_2_sdk_worker.py tests/unit/scheduler/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py tests/unit/scheduler/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/unit/scheduler/test_schema_readonly_historical.py tests/unit/scheduler/test_phase27_4_implementer_timeout_retry.py tests/unit/scheduler/test_phase27_5_implementer_failure_recovery.py tests/unit/scheduler/test_phase27_6_sequence_implementer_recovery.py
   163 passed in 293.83s
   ```

4. Flujos integrados SDK, métricas, Dashboard/API, nombres/paging, secuencia y
   recovery:

   ```text
   .venv/bin/python -m pytest -q tests/integration/test_phase25_2_sdk_workflow.py tests/integration/test_phase25_3_sdk_recovery.py tests/integration/test_phase25_4_token_usage.py tests/integration/test_phase25_4_dashboard_compatibility.py tests/integration/test_phase27_7_recovery_acceptance.py tests/integration/test_phase28_short_names.py tests/integration/test_phase28_dashboard_paging.py tests/integration/test_phase21_6_integration_acceptance.py tests/integration/test_phase20_9_multi_run_sequence_end_to_end.py tests/integration/test_phase26_7_installed_dashboard.py
   104 passed, 3 skipped in 785.10s
   ```

   Los tres skips se confirmaron aisladamente:

   ```text
   .venv/bin/python -m pytest -q -rs tests/integration/test_phase26_7_installed_dashboard.py
   6 passed, 3 skipped in 7.50s
   SKIPPED: D-02; installed Dashboard/browser requiere Node no disponible
   ```

5. Regresiones de review/retry/start/secuencia ejecutadas durante la corrección,
   antes del último ajuste puramente mecánico de formato/import:

   ```text
   .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py tests/unit/scheduler/test_phase20_8_sequence_review_retry_corrections.py tests/unit/scheduler/test_review_budget_extend.py tests/unit/scheduler/test_phase27_1_implementer_inputs.py tests/unit/scheduler/test_start.py tests/unit/scheduler/test_cursor_workflow_corrections.py tests/integration/test_phase17_4_cursor_workflow.py tests/integration/test_cursor_timeout_retry.py tests/integration/test_phase27_3_implementer_sequences.py tests/unit/scheduler/test_phase20_9_terminal_replay_bindings.py
   144 passed, 5 skipped, 11 warnings in 408.85s
   ```

   Los módulos completamente superseded no recolectan ejecución CLI nueva; los
   skips restantes son fixtures CLI sintéticos retirados. Las 11 warnings son del
   serializador Pydantic de fixtures históricos de terminal replay y no fueron
   silenciadas.

6. Autenticación de artefactos de review recovery, después de migrar el fixture a
   Codex:

   ```text
   .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py::TestRecoveryArtifactAuthentication tests/unit/scheduler/test_phase20_1_reviewer_retry_corrections.py::TestHistoricalRecoveryEligibility::test_pre_phase_cursor_outcome_without_binding_rejects_recovery
   8 passed in 36.33s
   ```

7. Checks finales:

   ```text
   .venv/bin/python -m ruff format --check .
   415 files already formatted

   .venv/bin/python -m ruff check .
   All checks passed!

   .venv/bin/python -m mypy src/ai_dev_loop
   Success: no issues found in 215 source files

   .venv/bin/python -m mkdocs build --strict --site-dir <tmp>/site
   exit 0; documentación construida en 0.97s

   .venv/bin/python -m build --outdir <tmp>/dist
   Successfully built ai_dev_loop-0.1.0.tar.gz and ai_dev_loop-0.1.0-py3-none-any.whl

   git diff --check
   exit 0
   ```

   MkDocs sólo informó su aviso upstream sobre Material/MkDocs 2 y ocho páginas de
   incidentes conocidas fuera de `nav`; `--strict` terminó correctamente.

### Ejecuciones anteriores, no confundidas con el estado final

- La implementación heredada pasó inicialmente los dos archivos 25.5 con
  `10 passed in 43.20s`, antes de agregar las regresiones y corregir review
  recovery/runner CLI.
- Antes de aplicar los dos P2 del review independiente, el conjunto 25.5 pasaba
  `17 passed`; el reviewer informó además `14 passed` en su selección focalizada.
  Esos verdes no cubrían el replay→review-recovery ni inspeccionaban el contenido
  del prompt Codex successor. El estado posterior a las correcciones es el
  `18 passed` más las selecciones de 9 y 10 pruebas registradas arriba. Ese
  estado aún no cubría el send perdido antes de `native_run_bound`; después de
  completar ese caso, el estado final es `19 passed`.
- La primera ejecución de la nueva regresión lost-send llegó incorrectamente a
  `ambiguous_missing` porque el fake exponía el run también en el inventario
  baseline. La segunda alcanzó `terminal_recovered` pero no produjo cambios de
  worktree durante el replay fake y bloqueó en staging. Corregidos ambos detalles
  del fixture, la reproducción llegó al defecto real (`SDK completed turn
  authority mismatch`) y el mismo caso pasó después de la corrección de
  producción. No se relajaron assertions.
- Una ejecución intermedia de los tests recién ampliados tuvo siete fallos de
  authoring: fixture de definición histórica, argumento `newest_first`, breakdown
  esperado por etapa y reloj de `extend`. Se corrigieron las fixtures/expectativas
  contra el contrato real, sin eliminar assertions; el resultado de aquella
  ronda fue `17 passed`.
- No se encontró un log local autoritativo que identificara los aproximadamente
  21 fallos del pytest global previo reportado por el operador.
  `.pytest_cache/v/cache/lastfailed` mezcla ejecuciones históricas y no permite
  atribuirlos. No se reconstruyó ese dato ejecutando la suite completa.

## Checks no ejecutados y pendientes separados

1. **Gate global pytest:** pendiente por instrucción expresa del usuario; no se
   afirma validación repository-wide.
2. **D-01:** capturas genuinas pre-Phase-25 con tokens autenticados para ambos
   roles Codex; riesgo residual ya diferido por el propietario del plan.
3. **D-02:** typecheck/Vitest/Playwright/browser frontend; `node` y `npm` devuelven
   `command not found`. No se instaló toolchain.
4. **Aceptación manual de `ai_dev_loop.yaml`:** obligatoria y pendiente; review
   de código o tests no la sustituyen.
5. **Instalación/cutover real y smoke SDK real:** pendientes de autorización
   operativa separada. Esta implementación no los ejecutó.
6. **Review independiente:** pendiente. Reviewer B no fue creado, reutilizado ni
   suplantado por este trabajo.

El índice conserva los 114 paths preparados por instrucción expresa; la última
corrección lost-send está unstaged y se informa separadamente. La entrega está
preparada para que una revisión independiente evalúe el diff completo sin
confundir este estado con aceptación, deploy o permiso de commit.
