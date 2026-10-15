# INC-2026-10-14-002 — Límite de uso Codex en review resume termina en `waiting_codex_review_retry` (no `waiting_codex_capacity`)

- Detectado: 2026-10-14 17:46 (La Paz) por Brusy (supervisión).
- Run: `ai-dev-loop-19a59326a083` (seq `ai-dev-loop-seq-31ea242c98f8`, fase 1/4 phase-27-4-implementer-timeout-retry, implementer codex).
- Rama: `codex/phase-27-dual-implementer` en ~/Projects/ai_dev_loop-phase27-execution.

## Cronología (UTC)
- 20:54–20:58 review att1 (`codex.bootstrap_review`, cuenta2) falla por cupo 5 h → `waiting_codex_capacity` (correcto, se reanuda solo).
- 21:11 rotación autorizada cuenta2 → cuenta1 (sonda: available). 21:12 el tick lanza att2 (`codex.resume_review`, att-afc56e35…).
- 21:19:33 Codex emite `turn.failed`: "You’ve hit your usage limit … try again at 5:20 PM" (ventana 5 h de cuenta1, que estaba al 94 %).
- result de att2: `failure_code=codex_usage_limit`, `limit_evidence_kind=provider_message_limit`, exit 1.
- 21:20:22 el run queda en **`waiting_codex_review_retry`** (evento `codex_review_retryable_failure`), con `codex_capacity_reason=null`; requiere `scheduler review retry` manual. No se reanuda solo aunque el cupo vuelve.
- 21:46 sonda `integration codex-capacity`: primero `unavailable/timeout`, 30 s después `available` (5 h 0 %, semanal 16 %).
- 21:47 Brusy ejecuta `scheduler review retry ai-dev-loop-19a59326a083` → `awaiting_codex_review`.

## Observación
El mismo tipo de fallo (límite de uso Codex) se clasificó distinto según el intento: att1 → `waiting_codex_capacity` (auto), att2 → `waiting_codex_review_retry` (manual). Hipótesis no verificada: en resume_review, o cuando el mensaje trae hora de reintento ya cercana/pasada, o cuando la sonda posterior da timeout, el clasificador cae en fallo operativo reintentable. El run estuvo parado ~27 min sin necesidad.

## Impacto
Bajo: patch intacto, reviews no consumidas (0/7). Solo demora hasta la siguiente supervisión.

## Estado
Recuperado con `review retry`. Sin cambios de código. RCA de Codex no solicitado (causa inmediata clara; se pide si se repite).
