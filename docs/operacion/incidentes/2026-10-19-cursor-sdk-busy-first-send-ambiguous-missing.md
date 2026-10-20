# INC-2026-10-19-001 — Primer `send` del Cursor SDK recibe `busy` y la fase queda en `cursor_sdk_ambiguous_missing`

- Fecha: 2026-10-19 (09:51–09:54 La Paz / 13:51–13:54 UTC)
- Detectado por: Brusy (supervisión), revisión de 10:21 La Paz
- Secuencia: `ai-dev-loop-seq-97f6aab2f8da` (phase-29-dashboard-token-analytics), fase 1/2 `phase-29-1-token-queries`
- Run: `ai-dev-loop-112ecb45dc9f` — estado `blocked`, block reason `cursor_sdk_ambiguous_missing`
- Repo/rama: `~/Projects/ai_dev_loop`, `codex/phase-29-dashboard-tokens`, HEAD `16ff586` (worktree limpio)
- Implementer: Cursor SDK `sdk-local` 1.0.37, `grok-4.7` (reasoning high); reviewer `gpt-6.1-sol` high
- Timeouts congelados (effective-config del run): cursor 90 min, codex 90 min
- Estado: Abierto (decisión de Rodrigo). Primera secuencia con implementer Cursor SDK.

## Síntoma
La fase quedó bloqueada ~3 min después de arrancar, sin que el implementer escribiera nada (`sdk-envelopes.jsonl` vacío, stderr vacío).

## Cronología (UTC)
| Hora | Etapa | Evidencia |
|---|---|---|
| 13:51:58 | Lanza `cursor.create_chat` (att-280ca103…) | journal de la unidad |
| 13:52:08.127 | El backend crea `run-c1e54848-1d1f-46a5-b2f5-430b56787a4f` asociado al agente, `status=queued`, `turnNumber=1`, sin inicio ni fin | `~/.local/state/ai_dev_loop/cursor-sdk/3f9a6139…/store/runs.ndjson:1` |
| 13:52:08.179 | `create_chat` liga `agent-dd1e7ca5-258f-45f3-b6ce-fcf5a1a8f67b` y termina `success` | `cursor/sdk-attempts/att-280ca103…/dispatch-journal.jsonl` |
| 13:53:04.209 | `cursor.run_turn` (att-39620af8…): `run_inventory_baseline` ya contiene `run-c1e54848…` | `cursor/sdk-attempts/att-39620af8…/dispatch-journal.jsonl:1` |
| 13:53:04.236 | `send` falla en ~20 ms con `sdk_failure {code: busy, operation: send}` → `cursor_sdk_send_uncertain`, exit 1 | `attempts/att-39620af8…/stdout.txt` |
| 13:54:02–13:54:33 | `cursor.reconcile_sdk_run` (att-c44bdf32…): 0 runs candidatos nuevos → `ambiguous_missing` → `blocked` | `cursor/sdk-reconciliation/…/record-v1.json` |

Artefactos: `~/.local/state/ai_dev_loop/artifacts/runs/3f9a61392db4d9d81975daaeb211cbba10dd688ef56520cdffe254d016bf30a8/`.

## RCA (Codex, solo lectura, 2026-10-19 ~10:25 La Paz)
- Causa raíz: al crear el agente (`client.create_agent()` → `CreateAgent` del SDK 1.0.37), el backend local dejó un run nativo `queued`. El primer `send` llegó con ese run aún asociado y Cursor respondió `busy` (`AgentBusyError` / "already has active run").
- Defecto de integración en ai_dev_loop: todo fallo de `send`, incluido `busy`, se clasifica como incierto (`src/ai_dev_loop/scheduler/cursor_attempt_runner.py:795`), y la reconciliación solo considera runs ausentes del baseline (`cursor_attempt_runner.py:1095`). Como el run `queued` ya estaba en el baseline, hubo cero candidatos y bloqueo. El adaptador ya reconoce `busy` (`src/ai_dev_loop/integrations/cursor_sdk/adapter.py:697`) pero no hay reintento transitorio ni manejo del run previo.
- Por qué no lo detectaron los tests 25.x: el SDK falso no crea un run en `create_agent` (`tests/integration/test_phase25_2_sdk_workflow.py:405`); se prueba `busy` solo como clasificación (`tests/unit/test_phase25_1_sdk_adapter.py:679`) y el envío incierto que bloquea (`test_phase25_2_sdk_workflow.py:630`), no la combinación CreateAgent → run `queued` → primer send `busy`.
- Riesgo de recurrencia: plausible y material en cada agente nuevo con esta configuración; con un solo caso no se puede estimar la tasa.
- Recuperación: `ai_dev_loop scheduler cursor-retry ai-dev-loop-112ecb45dc9f --check` → `ineligible` / `ineligible_block_reason` ("Block reason is not cursor_failure"), `recovery_supported=false` (verificado por Brusy 10:28 La Paz). `implementer-retry --force` también se rechaza para este bloqueo (`cursor_recovery_evidence.py:2186`, test en `test_phase25_2_sdk_workflow.py:714`). Única salida soportada: `ai_dev_loop scheduler sequence abort ai-dev-loop-seq-97f6aab2f8da` + secuencia nueva (puede repetir el fallo con el mismo SDK).

## Correcciones propuestas (no aplicadas)
1. Test de regresión con falso que crea run `queued` en `CreateAgent` — 0,5–1 día.
2. Manejo causal de `busy`: inspeccionar runs existentes del agente antes de enviar; esperar/resolver con política acotada; tras `busy`, re-observar antes de autorizar otro envío — 2–4 días.
3. Instrumentación + prueba de aceptación contra SDK 1.0.37 real (¿CreateAgent crea siempre el run `queued`?) — 1–2 días.

## Acciones de supervisión
Ninguna mutación: sin retry, sin abort, sin tocar git, auth ni la sesión `cursor-agent --trust`. Este archivo se dejó FUERA del worktree para no ensuciarlo (el run y cualquier secuencia nueva exigen `require_clean_worktree`).

## Addendum de implementación (2026-10-19)

El árbol de trabajo de Phase 25.6 cancela, antes del primer envío, un ocupante de creación autenticado, exclusivo y sin trabajo cuando su estado público es `queued` o `running`. La prueba usa un SDK falso y homes aislados. No cierra este incidente ni afirma aceptación contra el SDK real: la secuencia `ai-dev-loop-seq-97f6aab2f8da` sigue abortada e inmutable, y un run público que solo aparece como `error` no queda corregido automáticamente. El informe de contratos está en `archive/implementation-history/findings/phase-25-6-sdk-first-send-busy.md`.
