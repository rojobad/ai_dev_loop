# Phase 15.17 — Adjudicación externa durable, lineage acotado y handoff de publicación

## Goal(s)

Hacer recuperable, idempotente y verificable una ronda de feedback externo de PR desde que Codex B produjo un resultado estructurado hasta que el worker vuelve a esperar la siguiente revisión.

La fase debe desbloquear el mismo run de Crypto Sentinel crypto-sentinel-20260719T021751Z-66d03c (PR #45), sin sucesor, sin re-adjudicar el ciclo 5 y sin volver a publicar @codex review. Ya existen github/cycles/05/result.json, snapshots, report y prompts/fixes/github-05.txt; el prompt coincide byte a byte con cursor_fix_prompt. El estado todavía referencia github-04 y conserva lineage de una recovery de github-03.

Debe eliminar como un único contrato estas ventanas: timeout GraphQL después de los artefactos de Codex pero antes del estado; hash de una recovery histórica aplicado a ciclos posteriores; retorno del worker después de resume_run cuando el estado quedó en publishing_external_fix; y repetición de replies no accionables luego de una escritura ambigua.

## Non-Goals

- No ejecutar resume, recover, launch, start, Cursor, Codex, GitHub, SSH, commit, push ni publicación reales.
- No editar manualmente state.json, artefactos, worktree o la PR #45.
- No cambiar modelos, sesiones A/B, YAML objetivo, límites de ciclos, SSH, gh, hooks, ni las políticas de tests con riesgo residual.
- No aceptar timeout/red/auth/drift como éxito ni adoptar prompts, SHA, threads, patches o replies por logs, timestamps o errores.
- No mutar fuentes terminales ni convertir recover en una operación in-place. La continuación in-place es sólo para un run interrupted con evidencia durable del ciclo actual.

## Scope

- Modelo tipado, schema y persistencia de un checkpoint de adjudicación externa del ciclo actual, con rutas relativas y hashes.
- runners/codex_github.py y commands/pr_review.py: materialización, hidratación, reanudación y aplicación idempotente de la adjudicación.
- commands/pr_review_recover.py sólo lo indispensable para que sucesores compatibles conserven/entiendan el checkpoint sin copiar artefactos no allowlisted.
- Continuidad del worker después de workflow_engine.resume_run().
- Tests unitarios, schema, integración y regresiones Phase 15.7–15.16; documentación de CLI, artefactos, flujo, troubleshooting y trazabilidad.

## Out of Scope

- Reescribir publicación, staging/review local, no-findings/eyes o continue, salvo una integración mínima necesaria para conservar su idempotencia.
- Reintentos ciegos o infinitos a GitHub. Un error temporal deja un checkpoint interrupted; controller A decide cuándo ejecutar pr-review resume.
- Exponer cuerpos de comentarios, prompts, parches, sesiones completas, entornos o errores GraphQL crudos.
- Añadir una bandera de adopción manual: el único origen para reconstruir un prompt es el resultado estructurado válido.

## Required Context

### Estado real reproducible como fixture sintético

El run actual está interrupted tras timeout GraphQL. Su GPR tiene cycle_number=5, SHA publicado/enlazado, dos thread IDs elegibles actuales, external_cursor_iteration=4 y punteros last_external_result_path/external_fix_prompt_path todavía en ciclo 4. Recovery es external_feedback_cursor de iteración 3, con hash de github-03.

No obstante, github/cycles/05/result.json es all-actionable, cubre exactamente esos dos IDs y tiene snapshots, report, metadata y prompts/fixes/github-05.txt. El hash del prompt coincide con cursor_fix_prompt. No falta respuesta del bot ni cambio en Crypto Sentinel: run_codex_github_review escribió artefactos antes de que el worker llamara save_run_state; validate_independent_pre_cursor_baseline hizo GraphQL antes de esa persistencia y el timeout dejó los punteros antiguos.

### Simulaciones que se deben codificar

| Entrada o interrupción | Resultado correcto al reanudar | Nunca permitido |
| --- | --- | --- |
| Antes de result.json válido | Polling/adjudicación normal según estado actual | Inventar un resultado o Cursor prompt |
| Result válido, faltan report/prompt/punteros | Derivar sólo report/prompt desde el JSON validado, verificar IDs/hashes y persistir checkpoint | Reejecutar Codex B o usar github-NN previo |
| Resultado/prompt existen, falla GraphQL antes del preflight | El mismo pr-review resume revalida; con red disponible programa una sola corrección externa | Re-adjudicar, responder, resolver, publicar o repetir trigger |
| Sucesor antiguo con recovery externa original pendiente | Migrar sólo si iteración, ciclo, IDs, path y hash son exactamente los del source | Aplicar el hash de lineage a rondas posteriores |
| Recovery ya consumida y llega feedback N+1/N+2 | Usar exclusivamente checkpoint/artefactos del ciclo actual | Comparar github-03 contra github-04/05 |
| All-actionable y preflight correcto | Congelar IDs, preservar prompt exacto, asignar una única iteración externa y ejecutar Cursor | Dos chats, dos iteraciones o prompt reescrito |
| Not-applicable/uncertain | Intent durable por reply antes de write; éxito confirmado se persiste; ambigüedad espera al usuario o se verifica exactamente | Duplicar reply, resolver, Cursor o publicar |
| Cursor local aceptado deja publishing_external_fix | El worker recarga estado y publica in-process; luego continúa polling | Retornar silenciosamente o auto-spawn contra su PID |
| Publication/push/trigger fallan | Se preserva el checkpoint de publicación y resume retoma esa frontera | Rehacer Cursor/Codex, reset, force push |
| PR/SHA/thread/prompt/hash/baseline real deriva | Atención de usuario/error seguro sin write | Adoptar el valor live o editar estado |

### Contratos existentes que no se pueden degradar

- RecoveryState es lineage/auditoría de un sucesor, no guard global de todas las rondas. Un hash sólo puede proteger el checkpoint/ciclo que lo creó.
- GithubPrReviewResult estructurado, no Markdown ni logs, decide all_actionable, thread decisions, replies y cursor_fix_prompt.
- El primer Cursor de una ronda externa sigue usando el baseline limpio Phase 15.16; correcciones locales posteriores conservan la validación estricta de patch staged.
- El preflight de independent_pr continúa revalidando PR/SHA/threads/baseline antes de Cursor. Un timeout no invalida evidencia durable previa.
- Conservar locks run/repo, escrituras atómicas, paths relativos, permisos sensibles, argv arrays, shell=False, sesión Codex exacta, chat Cursor exacto y tests con fakes.

## Cursor Rules And Skills

Leer y aplicar todas las reglas de .cursor/rules/, especialmente ai-dev-loop-governance.mdc, ai-dev-loop-orchestrator-contracts.mdc, ai-dev-loop-state-and-schema-contracts.mdc, ai-dev-loop-loop-and-resume-contracts.mdc, ai-dev-loop-codex-review-contracts.mdc, ai-dev-loop-abort-contracts.mdc, ai-dev-loop-global-integrations-contracts.mdc y ai-dev-loop-docs-acceptance-contracts.mdc.

Para documentación usar ai-dev-loop-docs-acceptance-governance. No usar staged-review durante implementación. Usar sólo fakes de agent, Codex, GitHub/gh, Git y SSH; nunca servicios, credenciales, sockets, sesiones ni agentes reales.

## Architecture Guardrails

- Agregar un modelo tipado, por ejemplo ExternalAdjudicationCheckpoint, dentro del estado de PR-review. Debe incluir ciclo, bound SHA, IDs congelados, rutas y SHA256 de resultado/snapshot y, en all-actionable, ruta/SHA del prompt; además un estado tipado equivalente a cursor_pending, cursor_scheduled, replies_pending y consumed. Los nombres finales deben respetar convenciones existentes.
- Actualizar conjuntamente Pydantic, run-state-v1.json, lectores/escritores, manifest si aplica, status/inspect seguro, compatibilidad histórica, pruebas de schema y docs. No usar dicts ad hoc.
- Tras run_codex_github_review, validar resultado/IDs y persistir checkpoint bajo locks antes de binding remoto, chat, Cursor o GitHub write. Sólo se puede recrear un artefacto derivado faltante a partir de result.json válido y exacto.
- La hidratación sólo inspecciona rutas deterministas del cycle_number actual y exige concordancia exacta de resultado, snapshot, prompt, SHA e IDs contra el GPR actual. Cualquier mismatch, path inseguro o evidencia ambigua bloquea.
- Al publicar y abrir el ciclo siguiente, retirar o marcar consumed el checkpoint operativo anterior, manteniendo artefactos y eventos de auditoría. RecoveryState puede seguir existiendo, pero no vuelve a participar en un resume de ciclo posterior.
- Sustituir la rama global de resume basada sólo en recovery.recovered_checkpoint=external_feedback_cursor por un resolver del checkpoint actual. Para sucesores antiguos, sólo migrar desde recovery si iteración, ciclo, paths, hashes e IDs son los de su checkpoint original.
- Timeout, rate-limit, auth o red durante revalidación GraphQL deben dejar intacto el checkpoint y finalizar interrupted. pr-review resume debe reintentar esa lectura sin crear sucesor.
- Para replies no accionables, persistir intent antes de cada mutación y resultado inmediatamente después. Si no se puede probar exactamente si una mutación produjo su reply, esperar intervención en vez de duplicarlo.
- Tras resume_run dentro del poller, recargar estado. Con publishing_external_fix, continuar el loop y usar _publish_external_fix con schedule_worker=False; con awaiting_bot_review, seguir polling; con terminal/interrupted/waiting, salir. No self-spawn ni doble publicación.
- Mantener los checkpoints y validaciones de fingerprint/SSH de publication. La fase no los relaja.

## Implementation Plan

1. Mapear con tests la frontera de run_codex_github_review y extraer helpers para cargar/validar resultado externo, materializar artefactos derivados, construir checkpoint y resolver un checkpoint pendiente. Usar locks, escrituras atómicas, paths relativos, permisos sensibles y errores redactados.
2. Añadir el modelo y schema del checkpoint, con validaciones cruzadas de ciclo/SHA/IDs/rutas/hashes y estados de aplicación. Estados antiguos sin campo deben seguir leyéndose; definir migración en lectura estricta.
3. Reordenar worker: persistir resultado, snapshot, prompt/hashes y freeze de IDs antes de validate_independent_pre_cursor_baseline. En timeout posterior, _persist_worker_failure conserva un checkpoint reanudable.
4. Implementar hidratación idempotente al inicio de pr-review resume para un run interrupted. En el fixture ciclo 5, promover github-05 al checkpoint, actualizar punteros del GPR y programar exactamente la siguiente iteración externa. Si GraphQL falla otra vez, no perder ni re-adjudicar el checkpoint.
5. Acotar lineage: toda validación operacional usa el checkpoint de la ronda pendiente. Compatibilidad de recovery antigua sólo en su primer ciclo exacto; después de schedule/publication no se vuelve a elegir por mera presencia de state.recovery.
6. Aplicar resultados non-actionable desde el checkpoint, con intent previo y persistencia por reply. Implementar verification estricta o waiting_for_user_attention ante incertidumbre; nunca re-adjudicar ni duplicar comentario.
7. Cambiar el final de _run_pr_review_worker_loop_inner para no retornar incondicionalmente tras resume_run. Recargar estado y procesar publication/polling in-process. Probar tanto worker-driven como controller-driven, donde el spawn legítimo sigue siendo único.
8. Ajustar pr_review_recover y CLI/help sólo lo necesario: successors conservan el checkpoint/artefactos allowlisted; recover sigue siendo para terminales y resume es el camino in-place del run actual. No introducir comandos mutantes nuevos.
9. Actualizar README.md, docs/referencia/cli.md, docs/guia/flujo-completo.md, docs/operacion/estado-artefactos.md, docs/operacion/troubleshooting.md y docs/referencia/trazabilidad-fases.md con el comportamiento ya probado.

## Testing Criteria

- Schema/model: serialización, lectura histórica y rechazos de combinación, ciclo, SHA, ID, ruta o hash inválidos.
- Fixture ciclo 5: estado interrupted apunta a github-04 y lineage github-03, mientras github-05 válido existe. resume hidrata github-05, agenda una sola iteración y llama Cursor fake una vez; no Codex B, sucesor, trigger, reply ni resolve.
- Timeout GraphQL después de adjudicación: persiste checkpoint; segundo resume con red disponible sigue sin una segunda adjudicación.
- Matriz de artefactos: report/prompt faltante derivable, prompt distinto, result inválido, snapshots/IDs/ciclo distintos y path traversal. Sólo el caso demostrablemente derivable puede materializar.
- Lineage: recovery histórica pendiente migra; después de publicar, dos rondas posteriores usan sus prompts actuales y nunca el hash antiguo.
- Threads: drift de PR/SHA/set/baseline bloquea antes de Cursor. Para no aplicable/incierto, crash pre-write, éxito, crash post-write y respuesta ambigua prueban cero duplicados y cero Cursor/publicación.
- Handoff: integración de dos rondas prueba que el mismo worker publica una vez tras resume_run, resuelve sólo threads elegibles, publica un trigger único y continúa polling. Cubrir publicación controller-driven y fallos commit/push/trigger.
- Regresión Phase 15.7–15.16: baseline Phase 15.16, patch staged local, reviewing recovery, publication_pre_commit, SSH tipado, thread freeze, no-findings y A/B.
- Privacidad/idempotencia: outputs/eventos no incluyen datos sensibles y llamadas equivalentes no crean chat, iteración, worker, comentario, reply, resolve, commit o push adicionales.

## Validation

    TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -q tests/unit/test_phase15_12_external_feedback_iteration.py tests/unit/test_phase15_16_external_feedback_preflight.py tests/unit/test_phase15_17_durable_external_adjudication.py tests/integration/test_phase15_pr_review.py tests/integration/test_phase15_7_adjudication_recovery.py tests/integration/test_phase15_8_reviewing_recovery.py tests/integration/test_phase15_12_external_feedback_recovery.py tests/integration/test_phase15_13_publication_pre_commit_recovery.py tests/integration/test_phase15_15_publication_patch_fingerprint.py tests/integration/test_phase15_16_external_feedback_clean_baseline.py tests/integration/test_phase15_17_durable_external_adjudication.py tests/unit/test_recovery_planner.py
    TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -q
    uv run python -m ruff format --check src tests
    uv run python -m ruff check src tests
    uv run python -m mypy src
    uv run mkdocs build --strict
    git diff --check

No ejecutar operaciones reales sobre PR #45 ni el run actual durante implementación o validación.

## Risks Or Recovery Notes

- Esta fase no autoriza reanudar aún el run. Requiere revisión staged, aceptación, instalación local y publicación de la herramienta.
- Después de aceptar la versión, A debe ejecutar:

    ai_dev_loop pr-review resume crypto-sentinel-20260719T021751Z-66d03c --controller-session-id <sesion-A>

  El resultado esperado es recuperar el checkpoint durable de ciclo 5 y, si GitHub responde, iniciar una única corrección Cursor con github-05. B permanece inactiva. No usar pr-review recover ni editar estado para este caso.
- Si PR/SHA/branch, threads, prompt/hashes o baseline realmente no coinciden, debe parar sin side effects y requerir investigación nueva.
- Si GitHub no permite verificar exactamente una reply ambigua, es correcto parar y mencionar a @rojobad antes que duplicar el comentario.

## OpenQuestions

None.
