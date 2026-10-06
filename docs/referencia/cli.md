# Referencia CLI

Comando raiz:

```bash
ai_dev_loop [OPTIONS] COMMAND [ARGS]...
```

Opciones globales:

```text
--version
--help
```

## Contrato vigente

El flujo de un run nuevo es `scheduler submit` -> `scheduler start` ->
`scheduler tick`. Para varias fases, usa `scheduler sequence prepare` ->
`scheduler sequence start` -> ticks. El ledger `engine.sqlite3` y los artefactos
protegidos son la autoridad; los `state.json` del motor anterior son históricos.

| Momento | Efecto |
| --- | --- |
| Submit / preparación de secuencia | Congela plan, prompt, configuración y modelos; no invoca agentes ni Git. |
| Start | Registra autorización; la ejecución progresa mediante ticks. |
| Admisión del primer tick elegible | Verifica identidad, entradas y baseline según la política congelada, antes del trabajo de agentes. |
| Turno Cursor completado | Normaliza staging y conserva el snapshot que revisará Codex. |
| Primera revisión | Crea reviewer B con `codex exec` y registra su identidad exacta. |
| Revisiones y reintentos posteriores | Reanuda el B autenticado con `codex exec resume`. |

Controller A es proveniencia opcional: no hace falta un fork, un mensaje a otra
conversación ni una sesión desktop preexistente para enviar o controlar un run.
Si se proporciona A, su ID debe ser exacto; no se usa como reviewer B. Nunca se
selecciona una sesión mediante `--last`.

La admisión no es una exigencia continua de worktree limpio durante el trabajo
de los agentes. Permanecen las comprobaciones implementadas de identidad,
reservas, entradas inmutables y artefactos revisados en sus respectivos límites.

## `scheduler submit`

```bash
ai_dev_loop scheduler submit [OPTIONS]
```

Lee el prompt exacto desde stdin y congela un run `queued` en el ledger central
(`engine.sqlite3`) mas artefactos protegidos bajo `$XDG_STATE_HOME/ai_dev_loop/artifacts/`.
No crea `state.json` legacy, no lanza agentes, no ejecuta preflight ni muta el repositorio
objetivo.

Opciones principales:

```text
--config-path PATH
--project-name TEXT
--repo-path PATH
--plan-path PATH
--prompt-source-path TEXT
--controller-session-id TEXT   (opcional; proveniencia A)
--codex-review-model TEXT      (obligatorio)
--codex-review-reasoning-effort TEXT (obligatorio)
--cursor-command TEXT
--cursor-model TEXT
--cursor-output-format TEXT
--codex-command TEXT
--review-skill TEXT
--max-review-iterations INTEGER
--cursor-timeout-minutes INTEGER
--codex-timeout-minutes INTEGER
--resubmission-id UUID   (opcional; envio fresco idempotente tras un run terminal)
--output [text|json]
```

No admite `--codex-session-id`. Repetir submit sin `--resubmission-id` reutiliza
el run existente y reporta su `state_kind` real (por ejemplo `aborted` sin accion
de `start`). Tras un run terminal, usa `--resubmission-id` con un UUID elegido
por el operador para crear un run `queued` distinto; reutiliza el mismo UUID
solo para replay idempotente de ese envio. El primer review del scheduler crea exactamente un
reviewer B con `codex exec` en `--sandbox workspace-write`; los reviews posteriores reanudan
esa misma sesion con `codex exec resume` (nunca `--last` ni un segundo B).

Modelo y razonamiento del reviewer deben ser explícitos en los flags de submit.
Los campos YAML `codex.review_model` y `codex.review_reasoning_effort`, los datos
de A y los defaults del CLI no sustituyen esos flags. Los valores quedan
congelados para bootstrap, correcciones y reintentos. `--cursor-model` selecciona
por separado el modelo del ejecutor; no cambia el proveedor ni el modelo de Codex.

El scheduler impone `workspace-write` al proceso Codex. El wrapper y la skill de
review siguen exigiendo revisar únicamente el staged y no editar, stagear ni
eliminar intencionalmente archivos del repositorio. Se permite ejecutar pruebas;
la capacidad de escritura no autoriza corregir código para que pasen.

Ejemplo:

```bash
ai_dev_loop scheduler submit \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --cursor-model "<cursor-model>" \
  --codex-review-model "<review-model>" \
  --codex-review-reasoning-effort high \
  --output json < docs/plans/prompt_mi-plan.txt
```

## `scheduler sequence prepare` / `start` / `status` / `abort`

```bash
ai_dev_loop scheduler sequence prepare --manifest /path/to/sequence.yaml [OPTIONS]
ai_dev_loop scheduler sequence start <sequence-id> [--output text|json]
ai_dev_loop scheduler sequence status <sequence-id> [--output text|json]
ai_dev_loop scheduler sequence abort <sequence-id> [--output text|json]
```

Phase 20.1 congela una definicion lineal inmutable de 2 a 32 fases sin reservar el
repositorio, sin invocar Git y sin crear filas en `scheduler_runs`. Cada fase congela
plan, prompt, configuracion efectiva, rutas ejecutables, limites de workflow, modelo y
reasoning de review, y (para fases no finales) el mensaje de commit intermedio. Los
artefactos viven bajo `$XDG_STATE_HOME/ai_dev_loop/artifacts/sequences/`.

`scheduler sequence start <sequence-id>` es la autorizacion explicita de la secuencia
congelada. Materializa solo la fase 1 como un run normal de scheduler con el
`planned_run_id` preasignado, reclama la reserva del repositorio, inserta los eventos
`run_submitted` y `run_authorized`, y deja el run en `authorized` para que el tick
existente haga la admision Git. El comando no invoca Git, Cursor, Codex ni systemd.

Phase 20.3 agrega checkpoint de secuencia entre fases no finales aceptadas por Codex.
Tras un review aceptado de fase intermedia, el run entra en `checkpoint_pending` y el
tick reconcilia un commit local sin firmar (via `write-tree` / `commit-tree` /
`update-ref` CAS), transfiere la reserva al sucesor y materializa la siguiente fase sin
liberar el repositorio. La fase final pasa la secuencia a `awaiting_finalization` sin
commit y libera la reserva con los cambios staged intactos. Los runs standalone no
crean commits.

Phase 20.4 completa el ciclo de vida de secuencias: estados `active`,
`abort_pending`, `blocked`, `aborted` y `awaiting_finalization`; reconciliacion de
outcomes terminales no exitosos del run materializado (`blocked`,
`max_iterations_reached`, `aborted` por abort de secuencia) sin materializar sucesores;
`scheduler sequence abort` como control no destructivo (prepared sin runs, active
delegando al abort de run existente); status enriquecido con conteos agregados,
marcadores de residual risk y prefijos de checkpoint; y reporte seguro
`reports/completion-v1.json` al llegar a `awaiting_finalization` (sin commit/push/PR).

Phase 20.7 y 20.8 anaden lineage de intentos por fase y reintentos manuales
`scheduler review retry` con sucesor same-reviewer. Phase 20.9 hace de esa lineage la
proyeccion de status/report (`attempt_count`, `accepted_run_id_prefix`,
`attempt_kind_labels`, conteo agregado `attempts`) y amplia la reconciliacion para
secuencias con holds de checkpoint en el run actual. La autoridad sigue siendo el ledger
validado mas la lineage autenticada. Los únicos commits del flujo son los
checkpoints locales de fases no finales autorizados al iniciar la secuencia;
no hay commit final, push, PR ni merge automáticos.

Tras `scheduler sequence prepare`, la accion segura es `scheduler sequence start <sequence-id>`; tras un
start exitoso, `scheduler tick` y `scheduler sequence status <sequence-id>`.
`scheduler sequence abort` persiste la intencion antes de delegar al run activo y
cancela fases futuras sin crear filas `scheduler_runs` para ellas.
`scheduler abort <run-id>` durante `checkpoint_pending` impide nuevas mutaciones Git;
un abort de secuencia despues de un ref CAS ya aplicado reconcilia el checkpoint ya
aplicado (sin nuevas mutaciones Git ni sucesor) y completa el abort registrado. Mientras
una secuencia activa o `abort_pending` gobierna el run, o queden holds de
checkpoint/proceso, `scheduler abort` no libera la reserva del repositorio. Un abort de
secuencia con el run materializado ya terminal (p. ej. `max_iterations_reached`) finaliza
la secuencia sin reintentar abort del run. El reporte `reports/completion-v1.json` se publica solo despues de persistir
`awaiting_finalization` y `finalized_at` en el ledger; `completion_report_sha256` se
registra en una reconciliacion replayable por `tick` (sin escribir el artefacto dentro de
la transaccion de finalizacion). Los checkpoints del reporte se verifican contra los
objetos commit reales del repositorio.

Opciones de repositorio, `--config-path`, `--controller-session-id`, `--resubmission-id`
y overrides globales siguen el contrato de `scheduler submit`. Cada fase del manifest
debe declarar `codex.review_model` y `codex.review_reasoning_effort`; la fase final no
puede incluir `commit_message`.
Los dos valores de review provienen del manifest de cada fase, no de flags
globales de review ni de una sesión previa. Cada run planificado crea su propio
B al llegar a su primera revisión; un sucesor de recuperación same-reviewer
conserva el B de su origen autenticado.

## `scheduler start` / `tick` / `status` / `list` / `abort` / `history` / `timeline`

```bash
ai_dev_loop scheduler start <run-id>
ai_dev_loop scheduler tick
ai_dev_loop scheduler status <run-id> [--output text|json]
ai_dev_loop scheduler list [--output text|json]
ai_dev_loop scheduler abort <run-id> [--output text|json]
ai_dev_loop scheduler history <run-id> [--limit N] [--order oldest|newest] [--output text|json]
ai_dev_loop scheduler timeline <run-id> [--limit N] [--order oldest|newest] [--output text|json]
```

`scheduler abort` persiste primero la cancelacion durable, invalida effects/timers/claims
pendientes y no borra artefactos ni cambios staged o unstaged del repositorio objetivo.

`scheduler history` devuelve eventos acotados y redactados.

`scheduler status` y `scheduler list` exponen `review_iterations_completed` y
`max_review_iterations` segun el techo efectivo de reviews del run. Cuando un run
fue extendido explicitamente, el techo efectivo puede superar el limite congelado en
el contexto enviado; la salida indica el limite enviado solo cuando difiere.

## `scheduler cursor-retry`

```bash
ai_dev_loop scheduler cursor-retry <run-id> [--check | --force] [--output text|json]
```

`--check` y `--force` son excluyentes. Sin ninguna de las dos banderas, el comando
sigue autorizando solo el reintento de un timeout Cursor ya terminado, con el mismo
prompt, chat y salida que antes.

Con `--check`, el comando es solo lectura: abre el ledger en modo readonly, reconstruye
la evidencia causal del fallo `cursor_failure` (si existe) y devuelve un recibo JSON
acotado (`evidence_status`, `turn_kind`, `reason_code`, `safe_summary`,
`sequence_id`, `ordinal`, `recovery_supported`). `recovery_supported` es `true` para
un turno inicial independiente autenticado, sin reviewer B y sin reviews completadas,
y también para una corrección independiente autenticada con reviewer B, el fix exacto,
el sobre fallido y el presupuesto autorizado. Una secuencia autenticada sigue en
`false`. Un run inspeccionado pero no elegible devuelve recibo, no un fallo de proceso
inventado. Los artefactos protegidos se resuelven con raíces confinadas existentes; no
se crean directorios ni se alteran permisos.

Con `--force`, un fallo terminal autenticado de `cursor.run_turn` en un turno inicial
o una corrección independiente publica un sucesor del mismo chat. El prompt efectivo
es el sobre exacto del intento fallido más una nota operativa fija. Una corrección
conserva el fix crudo, el reviewer B, la iteración, las reviews completadas y el techo
efectivo; no reescribe la configuración enviada. El sucesor conserva el índice y los
archivos parciales; no hace admisión limpia ni `git add`. La publicación pendiente no
despacha un agente. Solo el cierre listo instala un efecto `cursor.run_turn`. Un
sucesor que vuelva a fallar puede forzarse de nuevo con su propio intento y el prompt
base original, sin apilar la nota. La inspección, la publicación `--force`, la
repetición de un registro listo y el despacho autentican cada registro inicial
ancestro exigido, su configuración congelada y los bytes del acarreo de
presupuesto, aunque el fallo sea un solo intento ordinario y el intent siga en
schema 1. Secuencias, abortos, procesos activos y dueños de worktree en conflicto
se rechazan.

`reason_code` estable (resumen): `authenticated_cursor_failure` (evidencia causal
autenticada); `insufficient_evidence_history` / `insufficient_evidence_ambiguous_failure`
/ `incomplete_event_history` (historial incompleto o ambiguo); `insufficient_chat_binding`,
`insufficient_reviewer_binding`, `insufficient_correction_envelope` (faltan enlaces);
`insufficient_sequence_*` (secuencia/hash/ordinal no verificados); `ineligible_*`
(estado, abort, reserva, hoja de secuencia obsoleta); `corrupt_*` (ledger o artefactos
no autentican). `safe_summary` está acotado y no incluye prompts, parches ni salidas de
agentes.

Tras un timeout confirmado de un turno Cursor, el scheduler conserva el mismo run,
chat, reviewer, prompt e iteracion, incluidos los cambios staged y unstaged. Programa
un nuevo intento a los 30 minutos, con un maximo de tres reintentos automaticos por
turno. Cada intento conserva sus propios artefactos; no consume una review adicional.

El comando manual esta disponible desde el primer timeout: adelanta cualquier espera
pendiente y tambien permite continuar tras agotar los tres automaticos. Adelantar una
espera no consume un reintento automatico. El comando encola el intento para el siguiente
tick; repetirlo antes del lanzamiento no crea otro intento. Requiere que el proceso
anterior haya terminado, que el run conserve su reserva y que no haya un abort pendiente.
No repite la admision ni exige limpiar el worktree. Al terminar Cursor, siguen el staging
y la revision normales, incluida la continuacion de la misma secuencia.

`status` expone `cursor_wait_until` y el comando manual; `history` muestra
`cursor_timeout_retry` con la accion, el contador y la fecha. Tras agotar los automaticos,
el run conserva su reserva y espera el comando manual o `scheduler abort`.
Los bloqueos `cursor_timeout` de versiones anteriores no se migran. El timeout de
`create-chat`, los fallos de integridad y las terminaciones inciertas no son reintentables
por este comando.

## `scheduler extend`

```bash
ai_dev_loop scheduler extend <run-id> --max-review-iterations <higher-total> [--output text|json]
```

Autoriza un techo absoluto mayor de reviews Codex para el mismo run cuando esta en
`max_iterations_reached`. El comando es process-free: no invoca Cursor ni Codex;
reacquire la reserva del worktree, registra el evento `review_budget_extended`,
transiciona a `waiting_for_cursor_fix` con el fix prompt exacto de la review
agotada y encola un efecto de correccion Cursor. Repetir el mismo objetivo absoluto
es idempotente. Fuera de ese replay, un objetivo menor o igual al techo efectivo
actual se rechaza.

## `scheduler review retry`

```bash
ai_dev_loop scheduler review retry <run-id> [--output text|json]
```

Autoriza una revisión de nuevo sin lanzar procesos; el siguiente tick ejecuta
el intento. La acción depende del checkpoint durable:

| Estado de origen | Comportamiento |
| --- | --- |
| `waiting_codex_review_retry` | Encola un reintento del mismo run, reviewer, modelo, razonamiento e iteración. |
| `waiting_codex_capacity` | Permite autorizar explícitamente el reintento conservando el reviewer; no cambia de modelo ni garantiza cuota disponible. |
| `blocked` elegible | Crea o reutiliza un sucesor con el mismo reviewer y evidencia autenticada; conserva inmutable el origen. |

En una secuencia, la recuperación coordina el reemplazo con su lineage y reserva.
No vuelve a ejecutar una implementación Cursor ya completada para reintentar la
review. Un reintento operativo no consume por sí solo una revisión completada.
La repetición de una autorización ya registrada no debe duplicar el intento.

Cuando el fallo clasificado es `codex_workspace_routing_timeout` (mensaje terminal
exacto `workspace routing discovery timed out` en `turn.failed`), el scheduler
puede autorizar hasta doce reintentos automáticos por iteración de review: el
primer reintento queda debido 300 segundos después de registrar el fallo y cada
fallo elegible posterior programa otro retraso de 300 segundos. Los ticks en o
después de la hora debida comparten la misma autorización durable que
`scheduler review retry` (origen `automatic` en el ledger). Agotado el cupo, el
run permanece en `waiting_codex_review_retry` y requiere `scheduler review retry`
manual; un timeout de la sonda de capacidad no bloquea un reintento de routing
elegible. Los waits históricos no ganan autorización automática al actualizar.

No todos los bloqueos admiten recuperación: una identidad B incierta o evidencia
de integridad inválida no se resuelve creando otra sesión. Consulta `scheduler
status` y su acción segura; conserva los artefactos de diagnóstico. Este comando
no es el antiguo `recover` ni un envío fresco con `--resubmission-id`.

## Detalle de `scheduler timeline`

`scheduler timeline` devuelve una tabla acotada de intentos Cursor/Codex por
iteracion: fase, ordinal de reintento, estado seguro, marcas de tiempo durables
y `observed_duration_seconds` solo cuando existen `launch_requested_at` y
`completed_at`. No incluye cola, preflight, IDs internos, artefactos ni salidas
raw. Limite por defecto 50; maximo duro 200. Valores mayores se truncan y
`truncated` indica overflow.

## `scheduler cutover cleanup`

```bash
ai_dev_loop scheduler cutover cleanup --confirm delete-legacy-state [--dry-run] [--output text|json]
```

Elimina solo `$XDG_STATE_HOME/ai_dev_loop/runs/` y `pr-review-v2/` tras validar el state
root, rechazar symlinks, comprobar que no hay trabajo scheduler activo y adquirir
coordinacion contra ticks concurrentes.

En modo JSON, el anuncio de rutas exactas va a stderr; stdout contiene un unico documento JSON.

## `scheduler timer`

```bash
ai_dev_loop scheduler timer validate [--output text|json]
ai_dev_loop scheduler timer install [--enable] [--output text|json]
ai_dev_loop scheduler timer status [--output text|json]
ai_dev_loop scheduler timer disable [--output text|json]
```

`install` escribe unidades empaquetadas bajo `~/.config/systemd/user/` y recarga
`systemctl --user`. `--enable` es explicito; `submit` y `tick` no habilitan timers.

La unidad de servicio empaquetada invoca `ai_dev_loop scheduler tick` mediante
`/usr/bin/env` con un `PATH` acotado que incluye `%h/.local/bin` (instalacion
habitual con `uv tool`) mas los directorios binarios del sistema. No ejecuta un
shell ni lee archivos de perfil interactivos.

La configuración real en WSL (systemd, `linger`, habilitación, actualización,
verificación y deshabilitación) se documenta en
[Timer del scheduler en WSL](../operacion/timer-systemd-wsl.md).

## `controller status`

```bash
ai_dev_loop controller status \
  --repo-path PATH \
  (--run-id TEXT | --controller-session-id TEXT) \
  [--include-terminal] \
  [--output text|json]
```

Lookup read-only. Con `--run-id` y `--repo-path` basta; no requiere A. Con
`--controller-session-id` se conserva el descubrimiento legacy por proveniencia A.
Sin ninguno de los dos selectores, falla con un error de validacion. Ante
ambiguedad (0 o N matches legacy) no elige por timestamp; usa `--run-id`.

## `doctor`

```bash
ai_dev_loop doctor [--repo PATH] [--output text|json]
```

Verifica entorno local y, opcionalmente, configuracion de un repositorio objetivo. Es read-only.

## `config validate`

```bash
ai_dev_loop config validate [--repo PATH] [--config-path PATH] [--output text|json]
```

Valida `ai_dev_loop.yaml`.

## `integrations install`

```bash
ai_dev_loop integrations install [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
--install-session-bridge
```

## `integrations uninstall`

```bash
ai_dev_loop integrations uninstall [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
```

## `integrations status`

```bash
ai_dev_loop integrations status [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
```

## `integration` (API local JSON)

```bash
ai_dev_loop integration info [--output json]
ai_dev_loop integration runs list [--kind all|standalone|sequence] [--offset N --limit N]
ai_dev_loop integration run inspect RUN_ID
ai_dev_loop integration run attempts RUN_ID [--offset N --limit N]
ai_dev_loop integration run timeline RUN_ID [--offset N --limit N]
ai_dev_loop integration run history RUN_ID [--offset N --limit N]
ai_dev_loop integration run plan RUN_ID [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration run initial-prompt RUN_ID [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration run reviews RUN_ID [--offset N --limit N]
ai_dev_loop integration run review RUN_ID --attempt ATTEMPT_ID
ai_dev_loop integration run review-content RUN_ID --attempt ATTEMPT_ID --kind prompt|response|review-markdown|cursor-fix-prompt [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration sequences list [--offset N --limit N]
ai_dev_loop integration sequence inspect SEQUENCE_ID
ai_dev_loop integration sequence phase-runs SEQUENCE_ID --ordinal N [--offset N --limit N]
ai_dev_loop integration sequence phase-plan SEQUENCE_ID --ordinal N [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration sequence phase-prompt SEQUENCE_ID --ordinal N [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration sequence report SEQUENCE_ID [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration run output RUN_ID --attempt ATTEMPT_ID --stream stdout|stderr [--offset BYTE_OFFSET --limit BYTE_LIMIT]
ai_dev_loop integration codex-capacity [--output json]
```

Namespace singular para el contrato JSON que consumirá el Bridge futuro. Todas
las órdenes de este namespace emiten un único documento JSON en stdout; `--help`
sigue siendo ayuda normal de Typer. El namespace plural `integrations` (instalación
Codex global) no cambia.

La API **1.5** expone las capacidades `runs`, `sequences`, `reviewInspection`,
`processOutput` y `codexCapacity`. Las lecturas de runs, secuencias, revisiones y
salidas de procesos abren el ledger en solo lectura y no migran la base de datos.
`integration info` no requiere ledger. `integration codex-capacity` ejecuta una
sonda acotada de capacidad sin persistir cuotas en el ledger. El único formato
admitido es JSON; la presencia obligatoria del flag `--output json` depende del
subcomando, según su ayuda.

Detalle del sobre, códigos de error, paginación y lecturas sensibles:
[API de integración local](integration-api.md).

## `integrations sessions`

```bash
ai_dev_loop integrations sessions install [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH]
ai_dev_loop integrations sessions status  [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH]
ai_dev_loop integrations sessions list    [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH] [--desktop-sessions-dir PATH] [--limit INTEGER]
ai_dev_loop integrations sessions remove  [--output text|json] [--wsl-codex-home PATH]
```

Gestiona el symlink seguro `sessions/from-desktop`.

## Comandos retirados (Phase 17.7)

Los siguientes comandos ya no existen en la CLI publica:

- `prepare`, `start`, `resume`, `recover`, `extend`, `launch`
- `abort`, `status`, `list`, `logs`, `inspect` de nivel superior
- `pr-review` y `github doctor`

La accion segura para trabajo nuevo es `scheduler submit` + `scheduler start` + `scheduler tick`
(o timer habilitado explicitamente). Para retirar estado legacy, usa
`scheduler cutover cleanup` solo tras aceptacion humana independiente.
