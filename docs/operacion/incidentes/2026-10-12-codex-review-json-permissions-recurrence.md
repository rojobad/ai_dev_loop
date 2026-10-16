# INC-2026-10-12-003: recurrencia de INC-2026-10-12-002 (JSON de revisión de Codex en 0664)

## Estado y alcance

- Fecha: **2026-10-12**. Todas las horas de esta entrada usan
  **America/La_Paz (UTC−04:00)**.
- Estado operativo: **recuperado a las 22:32**.
- Causa inicial: **determinada**; es el mismo defecto de
  [INC-2026-10-12-002](2026-10-12-codex-review-json-permissions-false-tamper.md)
  (análisis de causa raíz con Codex en modo solo lectura).
- Relacionado: [INC-2026-10-12-004](2026-10-12-cursor-resource-exhausted-unclassified.md),
  el fallo de Cursor que dejó este run bloqueado.
- Decisión: el asistente supervisor aplicó el mismo workaround puntual
  (`chmod 0600`) bajo el permiso permanente del dueño para decisiones de
  bloqueo que no son de arquitectura ni de producto. No se tocó código.

## Contexto

| Referencia | Valor |
| --- | --- |
| Run afectado | `ai-dev-loop-hub-8b36e4ea939b` (secuencia `ai-dev-loop-hub-seq-1da48112f32b`, fase 8/17 `phase-14-8-machine-activity-ui`) |
| Artefacto marcado | `codex/reviews/02.att-237589c2eaeb81945b2104554cbac798.json` |
| Raíz del run | `~/.local/state/ai_dev_loop/artifacts/runs/a0d875976478a6b160dc329d5dd36daf1caaabbc33cfbe1e733d818a0175604e/` |
| Modo antes / después | 0664 / 0600 |
| SHA-256 (antes del cambio) | `a9337e5097c50d1c3164649eb3e97d81f1dcd026713c831e44b3ff3898fc6b15`, coincide con el hash registrado; 8015 bytes |
| Run sucesor | `ai-dev-loop-hub-20261013T023257Z-a1c878` |

## Síntomas

Tras el `cursor_failure` de la iteración 3 (21:54), `cursor-retry --check`
devolvía `evidence=corrupt`, `reason=corrupt_artifacts_tampered`,
`recovery_supported=no`, igual que en INC-2026-10-12-002.

## Diagnóstico

Mismo defecto: el JSON de salida de Codex queda con el umask del proceso
(0664) y la inspección de recuperación lo rechaza como "alteración" aunque el
contenido coincide con el hash. En este run, la revisión 01
(`codex/reviews/01.att-16c58a58e9e900501e477d23a944247c.json`) y varios
`attempts/*/result.json` también están en 0664; solo se cambió la revisión 02,
que era la necesaria para la recuperación.

## Recuperación

1. `stat` y `sha256sum` del JSON: 0664, hash igual al registrado.
2. `chmod 0600` solo sobre ese archivo.
3. `cursor-retry --check`: `evidence=authenticated`,
   `authenticated_cursor_failure`, `recovery_supported=yes`.
4. `cursor-retry --force ai-dev-loop-hub-8b36e4ea939b` creó el sucesor
   `ai-dev-loop-hub-20261013T023257Z-a1c878` (22:32).

## Observación

Segunda ocurrencia en un día; afecta a toda recuperación `--force` de un
turno de corrección. La corrección propuesta en INC-2026-10-12-002 sigue sin
implementar.
