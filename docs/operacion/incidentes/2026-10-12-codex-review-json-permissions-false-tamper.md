# INC-2026-10-12-002: JSON de revisión de Codex en 0664 reportado como `corrupt_artifacts_tampered`

## Estado y alcance

- Fecha: **2026-10-12**. Todas las horas de esta entrada usan
  **America/La_Paz (UTC−04:00)**.
- Estado operativo: **recuperado a las 11:23**.
- Causa inicial: **determinada** (análisis de causa raíz hecho con Codex en
  modo solo lectura).
- Decisión del operador: aprobar como workaround un `chmod` puntual y
  registrar el incidente. Esta entrada no incorpora safeguards ni parches.
- Relacionado: [INC-2026-10-12-001](2026-10-12-cursor-out-of-usage-unclassified.md).

## Contexto

| Referencia | Valor |
| --- | --- |
| Run afectado | `ai-dev-loop-hub-c78a3ad37d89` (secuencia `ai-dev-loop-hub-seq-1da48112f32b`, fase 3/17) |
| Artefacto marcado | `codex/reviews/01.att-46714b2327b6c03da8d2234458b5fa25.json` |
| Raíz del run | `~/.local/state/ai_dev_loop/artifacts/runs/ffd17f9d55a394e88ab0e5e0c858d370260903c8173c912de2e3820af40fa8cf/` |
| SHA-256 del artefacto | `387fe0625392f5e6ed95e861ac661a8e2e5ad0bc336e782491bfc850f0c59572` (coincide con el ledger) |

## Síntomas e impacto

Tras el `cursor_failure` de INC-2026-10-12-001,
`ai_dev_loop scheduler cursor-retry --check` devolvía:

```text
evidence_status=corrupt
recovery_supported=false
reason_code=corrupt_artifacts_tampered
safe_summary="Protected artifact content does not match its authenticated hash."
```

Con ese resultado, `cursor-retry --force` no era elegible, y ningún comando
documentado permitía continuar el run sin abortarlo.

## Diagnóstico

- La evidencia del intento fallido de Cursor sí se autentica: la invocación,
  el envelope, los hashes de stdout/stderr y el ledger coinciden.
- La inspección falla en el JSON de la revisión de Codex que salió bien. Ese
  archivo tenía modo **0664**, mientras que los demás artefactos de la carpeta
  estaban en 0600. Su contenido **coincide** con el hash del ledger, así que
  no hubo alteración.
- Causa: Codex crea el JSON de salida del schema con el umask del proceso, y
  `codex_attempt_runner` lo lee y calcula su hash, pero nunca lo deja en 0600.
  Otros JSON históricos de revisiones también están en 0664.
- Clasificación incorrecta:
  `scheduler/infrastructure/readonly_protected_artifacts.py` y
  `scheduler/application/cursor_recovery_evidence.py` asignan tanto
  "artifact hash mismatch" como "artifact has unsafe permissions" al código
  `corrupt_artifacts_tampered`, con un resumen que no corresponde.
- Por qué no se detectó: la ingesta normal valida el JSON y el hash, pero no
  los permisos. La verificación estricta solo ocurre en la inspección de
  recuperación, y las pruebas fuerzan 0600 con `chmod`, lo que esconde el modo
  que se usa en producción.
- Riesgo de recurrencia: **alto** para toda recuperación con `--force` de un
  turno de corrección.

## Workaround aplicado (aprobado por el dueño)

1. Se comprobó que en la carpeta `codex/reviews/` solo ese JSON estaba en 0664.
2. SHA-256 antes del cambio: `387fe0…c59572`.
3. `chmod 0600` únicamente sobre ese archivo.
4. SHA-256 después del cambio: idéntico.
5. `cursor-retry --check` pasó a devolver `evidence_status=authenticated`,
   `recovery_supported=true`, `authenticated_cursor_failure`.
6. `cursor-retry --force` creó el sucesor
   `ai-dev-loop-hub-20261012T152338Z-621ac3`.

Este `chmod` modifica los metadatos de evidencia protegida y **no** es un
procedimiento documentado. Sirvió como intervención puntual autorizada, no
como receta general.

## Corrección propuesta (no implementada)

- Dejar en 0600 todo artefacto producido externamente (revisiones y
  resultados) antes de autenticarlo y registrarlo.
- Distinguir `unsafe_permissions` de una alteración de contenido o hash en
  los códigos y resúmenes.
- Agregar un comando oficial y autenticado para reparar o migrar permisos de
  artefactos históricos.
- Agregar pruebas que usen el umask real en lugar de forzar 0600.

## Seguimiento

Si se repite, crear una nueva entrada enlazada a esta y registrar el modo y el
hash del artefacto antes de cualquier intervención.
