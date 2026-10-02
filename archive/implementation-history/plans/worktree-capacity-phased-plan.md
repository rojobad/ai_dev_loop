# Capacidad de agentes por worktree — propuesta en cuatro fases

Estado: propuesta para revisión del usuario. Este archivo no autoriza ejecución,
staging, commits, instalación ni operaciones sobre el scheduler real. Cada prompt
selecciona una sola fase; el reviewer evalúa únicamente sus contratos obligatorios.

## Goals

Permitir un agente activo por `worktree_key`, con agentes simultáneos en worktrees
distintos, incluidos los de un mismo repositorio. Entregar incrementos aceptables
por separado mediante el scheduler vigente y su reviewer Codex.

## Non-Goals

No introducir identidad de proyecto, agrupación por `git_common_dir`, límites por
proveedor, prioridades, una nueva configuración de concurrencia ni múltiples
ticks concurrentes. Las fases 1 y 2 conservan el límite global vigente. La fase 3
activa el comportamiento solicitado; la 4 amplía la evidencia de aceptación.

## Scope

API interna y SQL de capacidad, migración SQLite, sus consumidores de estado,
controller/abort/cleanup y pruebas de ejecución, recuperación y secuencias.
Documentación del comportamiento efectivamente entregado en cada fase.

## Out of Scope

`ai_dev_loop.yaml`, `.agents/skills/`, `.cursor/rules/`, hooks, instalación de
integraciones, sesiones desktop, timer/linger real, estado XDG real, comandos
retirados, política de cuotas Codex y acciones Git externas. Cada fase excluye
las otras tres; no anticipar su implementación para terminar la fase activa.

## Required Context

Evidencia examinada el 2026-10-02; volver a verificar prerrequisitos una vez antes
del trabajo dependiente. No exigir un HEAD exacto ni controles continuos de baseline.

- `docs/referencia/cli.md`, `docs/operacion/estado-artefactos.md`,
  `docs/guia/instalacion.md`, `AGENTS.md` y `pyproject.toml`.
- `scheduler/infrastructure/sqlite_store.py`: esquema vigente 11, migraciones
  verificadas por checksum, `try_acquire_capacity`, `release_capacity`,
  `get_capacity_row`, `reconcile_stale_tick_resources`, `verify_launch_authority`,
  `mark_attempt_active`, `has_unreleased_abort_resources`, `release_capacity_for_run`.
- `0002_tick_control.sql`: fila global única, un titular y `max_value = 1`.
- `domain/common.py`: `worktree_key` existente; `scheduler_runs` y reservas ya
  tienen esa clave. `attempt_identity.py` implementa el lock por worktree.
- `application/tick.py`, `attempt_service.py`, `systemd_backend.py`: tick central
  acotado y unidades independientes por intento, sin esperar la ejecución del agente.
- `application/status.py`, `controller_read.py`, `cutover_cleanup.py`: lectores
  que actualmente presuponen una sola fila de capacidad.
- `application/abort_reconcile.py`, `sequence_handoff.py`, `sequence_reconcile.py`
  y los callers vigentes de retry/recovery: conservar sus transiciones y autoridad.
- Pruebas de `test_sqlite_store.py`, `test_schema.py`,
  `test_schema_readonly_historical.py`, `test_tick.py`, `test_tick_regressions.py`,
  `test_attempt_executor.py`, `test_phase17_6_abort_corrections.py`,
  `test_phase17_5_codex_corrections.py` y familias `test_phase20_9_*`.

Los caminos anteriores son relativos a `src/ai_dev_loop/scheduler/`, salvo cuando
se indica `docs/` o `tests/unit/scheduler/`. La revisión previa ejecutó 86 pruebas
de SQLite/tick/intentos/abort y una reproducción temporal: A adquirió capacidad,
B fue rechazado, aunque ambos tenían reservas de worktree. Esa evidencia establece
el punto de partida; no sustituye pruebas de las nuevas fases.

## Cursor Rules And Skills

Leer `AGENTS.md` y las reglas en `.cursor/rules/`:
`ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
`ai-dev-loop-state-and-schema-contracts.mdc`, `ai-dev-loop-abort-contracts.mdc`,
`ai-dev-loop-loop-and-resume-contracts.mdc`, `ai-dev-loop-codex-review-contracts.mdc`,
`ai-dev-loop-docs-acceptance-contracts.mdc` y
`ai-dev-loop-global-integrations-contracts.mdc`. No hay `.cursor/skills/`.
Aplicar `.agents/skills/create-cursor-plan/SKILL.md` y el estándar de
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md` sin modificarlos.
La documentación/aceptación sigue también
`.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`: sus referencias
a fases originales y comandos retirados son históricas; prevalecen `AGENTS.md`,
la fase aprobada y la evidencia del runtime vigente.

## Architecture Guardrails

- Ledger y artefactos protegidos siguen siendo autoridad. Mantener reserva,
  identidad exacta, inputs inmutables, claims/fences y locks existentes.
- Conservar el lease global del tick. El paralelismo ocurre entre procesos
  independientes; no añadir workers, threads, otro daemon ni timers.
- La capacidad se decide transaccionalmente; la presentación no decide quién
  puede lanzar agentes. No consultar Git para obtener una clave ya persistida.
- Un titular incierto o con cancelación no reconciliada conserva su cupo hasta
  evidencia de resolución. Una generación de tick nueva no basta para liberarlo.
- Liberación y activación requieren coincidencia exacta de run, claim y generación
  donde el contrato vigente la exige; una liberación tardía no toca otro titular.
- Reservas y cupos son distintos: la reserva del worktree puede mantenerse entre
  intentos aunque el cupo de agente quede libre. No redefinir sus duraciones.
- Reads no migran, adquieren capacidad ni reparan recursos. Cleanup comprueba
  todos los cupos; un lector de run consulta el cupo de su worktree en esquema nuevo.
- Cambiar esquema mediante nueva migración; preservar checksums históricos,
  snapshots, eventos, claims, identidades y artefactos. No reescribir inputs congelados.
- Pruebas con XDG/repos temporales, fake CLIs/backends y relojes controlados.
  Ninguna fase autoriza llamadas reales a modelos, instalación o controles vivos.
- Cursor implementa y Codex revisa staged. El scheduler exterior conserva su
  autoridad vigente de staging y checkpoints autorizados de fases no finales;
  el agente no realiza commits autónomos, push, PR, merge ni Git destructivo.

## Implementation Plan

### Fase 1 — Encapsular el acceso a capacidad

Valor: aislar el contrato que cambia y proteger sus consumidores sin migrar ni
activar concurrencia. Dependencia: únicamente el runtime vigente.

Introducir una API interna explícita para consultar capacidad en el contexto de
un run y para enumerar titulares. En esta fase ambas conservan la semántica global
actual. Llevar status/controller, abort y cleanup a esa API; no devolver aún una
semántica local ficticia. Mantener el SQL de lanzamiento, el esquema y los outcomes.
Evitar una abstracción de políticas configurable o un refactor general del store.

- **P1-C01:** mismos outcomes y salidas públicas para capacidad libre/ocupada,
  incluso si el titular pertenece a otro worktree.
- **P1-C02:** lectores de run y comprobación global de recursos tienen API explícita
  y todos los callers afectados la usan; ningún lector escribe.
- **P1-C03:** incertidumbre, cancelación y liberación fenced conservan comportamiento.

Aceptación: ejecutar callers reales de status/controller/abort/cleanup contra
fixtures, además de las regresiones vigentes. Todavía A impide lanzar B.

### Fase 2 — Persistir cupos por worktree, conservando la exclusión global

Valor: entregar y revisar la migración y recuperación con el riesgo de paralelismo
todavía desactivado. Dependencia: fase 1 aceptada.

Añadir la siguiente migración disponible (12 según el baseline examinado).
Representación propuesta: `scheduler_capacity` con `worktree_key` como clave única,
`max_value = 1`, y los campos vigentes de titular/run/claim/generación/fecha.
Crear filas cuando el camino de escritura necesite el cupo; ausencia de fila
equivale a cupo libre en lectura, nunca a crearla. No aceptar claves arbitrarias:
obtener la clave desde el run durable correspondiente.

Migrar el titular global ocupado al `worktree_key` del run referenciado, conservando
claim y generación. Una fila global libre no exige crear un cupo ficticio. Si una
asociación ocupada es inválida, fallar y revertir la migración íntegra.
Adaptar todo el SQL de adquisición, liberación, autoridad de lanzamiento, activación,
reconciliación y abort a las filas por worktree. Conservar temporalmente una condición
global de admisión en adquisición: cualquier titular ocupado impide otro lanzamiento.
Ese guard transitorio es código interno, sin flag/configuración nueva.

- **P2-C01:** migración atómica y replayable; el titular vigente y sus fences sobreviven.
- **P2-C02:** operaciones de recurso actúan sobre el cupo correcto; reads de run y
  controller proyectan su worktree y cleanup detecta cualquier titular.
- **P2-C03:** siguen funcionando lecturas históricas soportadas de esquemas 4–11
  sin migrar; conservan la proyección global histórica. En esquema nuevo, mantener
  el campo público `capacity_holder_run_id`, con alcance del worktree seleccionado;
  ausencia/titular libre se proyecta como null. Documentar este cambio de significado.
- **P2-C04:** A todavía impide lanzar B mediante el guard transitorio. Retener los
  cupos inciertos y liberarlos eventualmente al reconciliar resolución comprobada.

Aceptación: fixtures auténticos de esquema anterior con cupo libre, ocupado y
cancelación pendiente; fallo inyectado a mitad de migración; reapertura; readers
históricos; abort/reinicio; exclusión global mediante el camino real de lanzamiento.
No posponer la seguridad de recuperación a una fase posterior.

### Fase 3 — Activar ejecución simultánea entre worktrees

Valor: comportamiento solicitado disponible. Dependencia: fase 2 aceptada,
incluidos todos los callers y contratos de recursos adaptados.

Retirar exclusivamente la condición global transitoria de adquisición. Cada cupo
permite un titular. Usar las transacciones y fences existentes; no cambiar el lease
global ni el backend de procesos. Actualizar la descripción vigente de concurrencia.

- **P3-C01:** dos runs con claves distintas lanzan y mantienen intentos activos
  simultáneamente desde `TickService`/`AttemptService`, con identidades distintas.
- **P3-C02:** intentos competidores por la misma clave no obtienen dos titulares.
  La reserva vigente sigue impidiendo runs incompatibles en el mismo worktree.
- **P3-C03:** incertidumbre, abort o liberación tardía en A no libera/bloquea el cupo
  independiente de B; B continúa y A converge cuando su terminación se confirma.
- **P3-C04:** reiniciar el servicio/reconciliar ticks conserva ambos titulares y
  evita relanzamientos duplicados y activaciones con autoridad ajena o antigua.

Aceptación: backend fake con intentos de varios ticks y barreras explícitas para
la carrera de adquisición. Afirmar dos intentos activos antes de completar uno;
no confundir dos lanzamientos consecutivos con simultaneidad. Un caso obligatorio
usa worktrees Git reales del mismo repositorio con ramas distintas. La prueba de
adquisición concurrente no crea dos runs válidos violando la reserva de producción.

### Fase 4 — Aceptación de ciclos completos y secuencias

Valor: demostrar que el paralelismo funciona durante el intercambio completo de
implementación/revisión/correcciones y los checkpoints. Dependencia: fase 3 aceptada.

Añadir aceptación con fake Cursor/Codex y servicios/CLI reales sobre fixtures
temporales. Cambios de runtime sólo para defectos concretos de esta integración;
si aparece una garantía obligatoria incumplida de fase 3, identificar su contrato
y corregir la regresión antes de aceptar, sin ocultarla como riesgo residual.

- **P4-C01:** ciclos A/B completan Cursor → staging → review, con una corrección
  en uno mientras el otro avanza; cada run mantiene su chat/reviewer exacto,
  artefactos, staging, presupuesto y resultado independientes.
- **P4-C02:** secuencia en un worktree y run/secuencia en otro avanzan de forma
  independiente. Checkpoint/handoff conserva la reserva propia y no afecta al otro.
  La fase final deja staged; los únicos commits son checkpoints ya autorizados.
- **P4-C03:** status/controller y lecturas integration reflejan los runs correctos
  durante la concurrencia; cuota Codex conserva su significado y política.
- **P4-C04:** paquete y docs vigentes describen un agente por worktree, múltiples
  features en ramas distintas, migración y límites de operación; registrar evidencia
  en `archive/implementation-history/findings/worktree-capacity-acceptance.md`.

No afirmar que toda la capacidad de un agente/proveedor permite concurrencia por
haber quitado el límite del orquestador. No ampliar la política de cuotas.

## Testing Criteria

Niveles: unit/regression para API/fences y migration/compatibility para persistencia;
integration/acceptance para ticks, workflow y CLI. Extender los archivos citados en
Required Context y añadir pruebas enfocadas de capacidad por worktree bajo
`tests/unit/scheduler/` y aceptación bajo `tests/integration/`.

Conectar cada ID activo con caller de producción y test de resultado independiente.
Usar fake backends para procesos inciertos y fake ejecutables para el flujo completo;
no simular aceptación invocando únicamente helpers privados. Tiempo determinista;
barreras/events para carreras, sin sleeps como sincronización. Readers históricos
usan fixtures del esquema real anterior, no snapshots regenerados por el writer nuevo.

En P1/P2 el test de exclusión global debe pasar. En P3 la prueba A/B debe fallar
contra P2 por `capacity_busy` y pasar al quitar el guard; mismo-worktree y fences
deben seguir protegiendo. En P4 comprobar contenido/identidades y outcomes reales,
no únicamente conteos de filas o de tests.

## Validation

Por fase ejecutar lint/type checks aplicables y suites afectadas, con comandos y
resultados exactos en el informe. Comandos base:

```bash
uv run python -m ruff check src/ai_dev_loop/scheduler tests/unit/scheduler
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler
```

Incluir tests de commands/controller/integration y las nuevas pruebas cuando cambien
esos callers. P4 requiere además suite completa, `uv run python -m build` y
`uv run mkdocs build --strict`. P2 requiere regresiones de schema/read-only además
de la migración nueva. Añadir `ruff format --check` sobre archivos Python cambiados.
Reportar contratos completos/incompletos y checks ejecutados/fallidos/no ejecutados;
no declarar aceptación si falta cobertura obligatoria de la fase activa.

## Risks Or Recovery Notes

Cada fase es un incremento aceptable; el reviewer no exige capacidades de fases
futuras. P2 debe preparar todos los mecanismos de recurso para múltiples filas
aunque el guard global siga impidiendo múltiples agentes. P3 no activa antes
de que migración/abort/reconciliación estén completos.

Ante interrupción de migración, rollback y esquema anterior intacto; ante ejecución
incierta, conservar cupo/fence y reconciliar con backend; al confirmarse resolución,
liberar sólo su titular y permitir el próximo intento. No hacer downgrade manual,
limpieza de estado ni borrar locks para recuperar.

Para usar ai_dev_loop sobre su propio código, ejecutar la secuencia exterior con
una instalación estable separada de la fuente que Cursor modifica. Esa instalación
conserva su esquema y comportamiento durante las cuatro fases. Las versiones nuevas
se prueban con XDG aislado; una actualización del runtime real es un paso operativo
posterior. No reemplazar ni reinstalar el scheduler que está conduciendo la secuencia.
Un manifest posterior debe congelar modelo/razonamiento de reviewer por fase y
mensajes de checkpoint no finales. No generar valores de review por defecto.

## OpenQuestions

- **OQ-01, sólo ejecución/rollout real:** verificar antes de iniciar que la instalación
  que conducirá el trabajo está separada del checkout y conserva versión estable;
  no se ha inspeccionado esa topología en esta propuesta. Si no lo está, acordar
  el setup aislado antes de iniciar. No bloquea implementación/pruebas temporales.
- **OQ-02, sólo envío al scheduler:** el usuario debe escoger los valores explícitos
  de modelo/razonamiento de review antes de submit o sequence prepare. No inferirlos
  de YAML ni de esta conversación. No bloquea el diseño o implementación local.

Las decisiones de representación y alcance público de P2 son propuestas explícitas
en este documento y deben formar parte del alcance aprobado antes de su ejecución.
