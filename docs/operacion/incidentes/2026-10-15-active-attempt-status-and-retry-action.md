# INC-2026-10-15-002 — Estado y acción recomendada no reflejan el intento activo

- Fecha: 2026-10-15, America/La_Paz (BOT).
- Estado: abierto; corrección diferida por solicitud del usuario.
- Run observado: `ai-dev-loop-implementer-recovery-20261015T150207776032Z-90526a0b`.
- Repositorio de ejecución: `/home/rojobad/Projects/ai_dev_loop-phase27-execution`.
- Alcance: presentación de estado/acciones de Codex y Cursor; no se autoriza
  intervenir en el run activo ni cambiar sus decisiones durables.

## Hechos verificados

La consulta de `controller status` mostró simultáneamente:

- `status` y `scheduler_state_kind`: `preflight_complete`.
- `block_reason_kind`: `codex_implementer_timeout_retry`.
- `next_safe_action`: comando `scheduler implementer-retry` para este run.
- `active_process_registered: true` y último evento `attempt_launch_requested`.

Sin embargo, `scheduler timeline` y los eventos protegidos del implementador
mostraron un nuevo intento activo y comandos de validación en ejecución.
El timeout correspondía al intento anterior, no a una espera de intervención
para el intento actual. El estado y la acción recomendada inducen a interpretar
que sigue pendiente un reintento que el scheduler ya lanzó.

## Cronología observada (BOT)

- 12:17:58: lanzamiento del segundo intento del run.
- 13:48:19: segundo intento termina tras aproximadamente 90 minutos; el estado
  conserva el diagnóstico de timeout y la opción de reintento manual.
- 14:18:47: lanzamiento del tercer intento, tras la espera automática de 30 minutos.
- 14:35:52: última escritura de eventos observada durante la consulta posterior;
  suite completa de pytest en ejecución. En ese intento constaban 70 tests de
  recuperación y 14 adicionales completados con éxito, además de checks estáticos.
- Aproximadamente 14:57: consulta confirma la coexistencia de intento activo y
  recomendación de `implementer-retry`. El usuario solicita registrar el incidente.

Estos datos son evidencia puntual, no una afirmación del estado posterior del run.

## Caso adicional reportado: Cursor

El usuario informa que, con Cursor como implementador, la salida sigue mostrando
`cursor_ready` mientras el agente ya trabaja. Se registra como problema relacionado
sobre la distinción entre checkpoint conversacional y ejecución activa.
No se capturó un run Cursor específico en esta investigación: reproducción y
correlación con sus eventos quedan pendientes. No afirmar causa idéntica sin probarla.

## Impacto y causa pendiente

El operador no puede distinguir con claridad trabajo activo de espera por una
acción manual. La recomendación puede provocar solicitudes de reintento innecesarias.
No se comprobó lanzamiento duplicado, pérdida de datos ni fallo de los fences.
La causa precisa en las proyecciones/controller queda pendiente de análisis;
la evidencia es compatible con exponer metadata del timeout anterior sin priorizar
el intento activo. Conservar un checkpoint interno no constituye por sí solo un
error de ejecución; el defecto observado es la presentación/acción recomendada.

## Evidencia y reproducción

Consultas de solo lectura empleadas: `controller status --run-id ... --output json`,
`scheduler timeline <run-id> --output json` y lectura de eventos JSONL protegidos
bajo el directorio de artefactos del run, en `implementer/turns/`.
No se copian transcripts, prompts, salida cruda ni IDs de sesiones de agentes.
Para reproducir automáticamente, usar backends/CLIs falsos y estado temporal:
provocar timeout, autorizar o vencer espera, lanzar siguiente intento y consultar
status/controller/integration mientras el nuevo intento sigue activo.

## Criterios de corrección futura

- La salida pública distingue intento activo, checkpoint interno, espera y fallo.
- Mientras existe un intento activo, la acción recomendada no solicita repetirlo;
  el diagnóstico previo se conserva como histórico sin presentarlo como bloqueo actual.
- Aplicar el criterio a Codex (`preflight_complete`) y Cursor (`cursor_ready`),
  manteniendo compatibilidad de schemas mediante presentación/campos versionados
  según corresponda, sin renombrar estados durables solo para mejorar una etiqueta.
- Cubrir controller/status, secuencia e integración donde compartan la proyección.
- Probar lanzamiento/reinicio, transición a espera real, finalización y rechazo
  de reintentos duplicados; no ocultar una espera válida cuando no hay intento activo.

## Intervención

Solo registro documental en el checkout principal. No se ejecutó retry, abort,
tick, stage, commit, instalación, cambio de timer ni modificación de ledger,
artefactos protegidos o worktree del agente activo.
