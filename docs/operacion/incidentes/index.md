# Registro de incidentes

Este registro conserva incidentes observados durante la operación real de
`ai_dev_loop`. Sirve para reconocer recurrencias y decidir posteriormente si
conviene incorporar safeguards o un parche. Registrar un incidente no autoriza
ni implica implementar esas medidas.

Cada entrada debe distinguir hechos verificados, inferencias y causa pendiente;
incluir contexto, impacto, cronología, evidencia, recuperación y limitaciones.
Conserva referencias a los artefactos protegidos, no copias de transcripts,
prompts, credenciales ni IDs de sesiones de agentes.

## Incidentes registrados

| ID | Fecha | Incidente | Estado operativo | Causa inicial |
| --- | --- | --- | --- | --- |
| [INC-2026-10-06-001](2026-10-06-systemd-user-control-channel.md) | 2026-10-06 | Cursor completado; scheduler sin acceso al canal de control de `systemd --user` | Recuperado | No determinada |
| [INC-2026-10-12-001](2026-10-12-cursor-out-of-usage-unclassified.md) | 2026-10-12 | Cursor "You're out of usage" no se clasifica como límite de uso; termina en `cursor_failure` sin clasificar | Recuperado | Clasificador sin la redacción actual de Cursor |
| [INC-2026-10-12-002](2026-10-12-codex-review-json-permissions-false-tamper.md) | 2026-10-12 | JSON de revisión de Codex en 0664 reportado como `corrupt_artifacts_tampered` con hash correcto | Recuperado | Artefacto sin normalizar a 0600; permisos y alteración se confunden |
| [INC-2026-10-12-003](2026-10-12-codex-review-json-permissions-recurrence.md) | 2026-10-12 | Recurrencia de INC-2026-10-12-002 en la fase 8/17: JSON de revisión en 0664 bloqueó `--force` | Recuperado | Mismo defecto de permisos |
| [INC-2026-10-12-004](2026-10-12-cursor-resource-exhausted-unclassified.md) | 2026-10-12 | Cursor `resource_exhausted` con cuota disponible termina en `cursor_failure` sin clasificar | Recuperado | Error reintentable de Cursor no reconocido por el clasificador |
| [INC-2026-10-14-001](2026-10-14-cursor-out-of-usage-recurrence.md) | 2026-10-14 | Recurrencia de INC-2026-10-12-001 en seq Phase 27 fase 2/3: "out of usage" de Cursor termina en `cursor_failure` | Abierto (sin cuota Cursor hasta 12 nov) | Mismo clasificador sin la redacción actual |
| [INC-2026-10-14-002](2026-10-14-codex-usage-limit-resume-review-retry.md) | 2026-10-14 | Límite de uso Codex en review resume deja el run en `waiting_codex_review_retry` (manual) en vez de `waiting_codex_capacity` (auto) | Recuperado (`review retry`) | Clasificación inconsistente del límite de uso Codex |

| [INC-2026-10-15-001](2026-10-15-codex-implementer-routing-timeout-blocked.md) | 2026-10-15 | Timeout "workspace routing discovery" en bootstrap del implementer Codex bloquea fase 4/4 (`codex_implementer_failure`) sin reintento en la versión instalada | Abierto (decisión de Rodrigo) | Clasificador del implementer no trata el routing timeout como transitorio |

| [INC-2026-10-15-002](2026-10-15-active-attempt-status-and-retry-action.md) | 2026-10-15 | Intento activo muestra `preflight_complete` y recomienda retry por timeout previo; caso relacionado `cursor_ready` reportado | Abierto; corrección diferida | Proyección de actividad/acción pendiente de análisis |

Una recurrencia debe tener su propia entrada y enlazar los casos relacionados.
Conserva el diagnóstico original; agrega nueva evidencia identificando cuándo
se obtuvo. Los cambios de código, si se autorizan, se documentan por separado.
