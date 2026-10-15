# INC-2026-10-14-001: recurrencia de INC-2026-10-12-001 ("out of usage" de Cursor no clasificado)

## Estado y alcance

- Fecha: **2026-10-14**. Horas en **America/La_Paz (UTC−04:00)**.
- Estado operativo: **abierto / bloqueado**. No se relanzó: la cuota mensual de
  Cursor está al 100 % y on-demand está apagado.
- Causa inicial: **misma que INC-2026-10-12-001** (el clasificador de fallos de
  Cursor no reconoce la redacción "You're out of usage. Switch to Auto").
  Hecho verificado por stderr y `/usage`; sin cambio de código.
- Registrado por el supervisor (Brusy). Esta entrada no incorpora parches ni
  safeguards.
- Relacionado: [INC-2026-10-12-001](2026-10-12-cursor-out-of-usage-unclassified.md).

## Contexto

| Referencia | Valor |
| --- | --- |
| Proyecto | `ai-dev-loop`, repo `~/Projects/ai_dev_loop-phase27-execution`, rama `codex/phase-27-dual-implementer` |
| Secuencia | `ai-dev-loop-seq-b7fd29dae934`, fase 2/3 `phase-27-2-codex-turns-and-capacity` |
| Run afectado | `ai-dev-loop-30bb4f1280b2` |
| Intento Cursor afectado | iteración 01, intento 2 (`att-bbc80bdccbcf9f7ded3e0141163dc810`), primer turno de implementación |
| Entorno | WSL del NucBox; plan Cursor Pro |

## Cronología

- 07:02:06–07:02:37 intento 1 (create_chat, 31 s) ok.
- 07:03:08 intento 2 (turno de implementación) lanzado.
- 07:31:01 intento 2 falla tras ~27,9 min. Run y secuencia pasan a `blocked`
  con `cursor_failure`; `Next action`: sin reintento automático.
- 07:43 el supervisor lee stderr y revisa `/usage` en cursor-agent (tmux).

## Evidencia

- `cursor/iterations/01/att-bbc80bdccbcf9f7ded3e0141163dc810/stderr.txt`:
  `ActionRequiredError: Increase limits for faster responses You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.`
- `/usage` (2026-10-14 07:43): Included 100 %, Auto 100 %, API 100 %,
  On-Demand **Disabled**, reinicio **12 nov**.

## Impacto

La fase 2/3 queda detenida hasta que haya cuota de Cursor (reinicio del ciclo
el 12 nov, otra cuenta o activar on-demand). La fase 3/3 no se materializa.

## Recuperación prevista

Cuando vuelva a haber cuota: `scheduler cursor-retry ai-dev-loop-30bb4f1280b2 --check`
y, si `recovery_supported=true`, `--force`. Si `--check` marca
`corrupt_artifacts_tampered`, aplicar el workaround autorizado de INC-2026-10-12-002.
