# Estado y artefactos

`ai_dev_loop` guarda estado fuera del repositorio objetivo. Esto evita mezclar auditoria del orquestador con el codigo que Cursor modifica.

## Autoridad del scheduler (Phase 17.7)

El flujo soportado persiste estado durable en:

```text
$XDG_STATE_HOME/ai_dev_loop/engine.sqlite3
$XDG_STATE_HOME/ai_dev_loop/artifacts/
```

Los comandos `scheduler status`, `scheduler list` y `scheduler history` leen solo
ese ledger central.

## Layout del scheduler

```text
$XDG_STATE_HOME/ai_dev_loop/
├── engine.sqlite3              # autoridad: runs, eventos, leases, attempts
├── artifacts/
│   └── runs/<sha256(run_id)>/  # snapshots inmutables verificados por hash
│       plan/plan.md
│       prompts/cursor-initial.txt
│       effective-config.yaml
│       source-config.yaml
│       implementer/session.json
│       implementer/timeouts/<attempt-id>.intent.json
│       implementer/iterations/<NN>/final.txt
│       implementer/turns/<NN>.<attempt-id>.{jsonl,stderr.txt,metadata.json,...}
│       cursor/sdk-identity.json
│       cursor/sdk-attempts/<attempt-id>/dispatch-journal.jsonl
│       cursor/iterations/<NN>/<attempt-id>/{sdk-envelopes.jsonl,sdk-metadata.json,final.txt,stderr.txt}
│       codex/fresh-reviewer-input.json
│       codex/fresh-reviewer-binding.json
│       git/admission-status.txt
├── repository-locks/
├── codex-sessions/             # metadata minima del hook SessionStart
└── locks/                      # locks de cutover y otros control-plane
```

- `scheduler submit` congela un run `queued` sin lanzar agentes ni mutar el repo.
- Schema 17 guarda aliases editables en `scheduler_run_presentation`,
  `scheduler_sequence_presentation` y `scheduler_planned_run_presentation`. La
  migración desde schema 16 rellena valores acotados sin tocar payloads/hashes y
  es rerunnable con `ON CONFLICT DO NOTHING`; una edición posterior sobrevive.
- `scheduler start` autoriza el run para ticks; la proveniencia controller A es opcional.
- `scheduler tick` ejecuta preflight, el implementer congelado, staging, review y
  correcciones segun el checkpoint.
- Un submit con implementer Codex congela provider, modelo, effort y timeout aparte
  del reviewer. La admisión prueba ese Codex sin exigir el binario Cursor. El tick
  captura una sesión implementer autenticada, conserva cada intento bajo
  `implementer/`, normaliza staging al completar el turno y solo entonces crea el
  reviewer B independiente. Los contextos históricos sin esa metadata siguen Cursor.
- Un submit Cursor con config v2 congela runtime SDK, versión, modelo+parámetros,
  referencia de credencial, fuentes, sandbox y policy en contexto v7. El worker
  persiste `create_intent` antes de crear, liga el agent exacto antes del primer
  send y persiste `send_intent` antes de inferir. Los envelopes NDJSON acotados,
  final, metadata y hashes permanecen separados. La identidad lógica no expone
  roots privados del host. La unidad systemd usa `KillMode=control-group` para
  incluir bridge y herramientas descendientes en timeout/abort.
- Una cuota por evidencia estructurada/provider o por un probe post-fallo
  `EXHAUSTED` autenticado durante implementación inicial o corrección entra en
  `waiting_codex_capacity` con rol, turno, prompt, identidad y evidencia del intento.
  `AVAILABLE` reanuda esa misma sesión; `EXHAUSTED` o `UNAVAILABLE` conserva la espera,
  los cambios parciales y los artefactos sin iniciar staging ni review. El único
  bootstrap sin sesión permitido es un reintento autenticado sin trabajo observado.
  Los fallos con identidad incierta o evidencia incompleta bloquean conservando la
  reserva; `scheduler abort` explícito permite cancelarlos sin borrar cambios ni
  artefactos. Si la terminación sigue incierta, la reserva permanece retenida.
- `require_clean_worktree` se congela en submit y se aplica en la admision one-shot del primer tick.

## Estado legacy retirado

Los arboles historicos siguientes ya no tienen comandos de ejecucion:

```text
$XDG_STATE_HOME/ai_dev_loop/runs/<project>/<run-id>/   # state.json legacy
$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/              # motor PR-review v2
```

Eliminalos solo con `scheduler cutover cleanup --confirm delete-legacy-state` tras
aceptacion humana independiente (ver [Desinstalacion y limpieza](desinstalacion-limpieza.md)).

El layout legacy `runs/` incluia `state.json`, `cursor/`, `codex/`, `git/`, etc.
Esa informacion ya no es la autoridad operativa; conservala solo para auditoria
manual hasta el cutover.

## Recuperacion

No existe `recover` publico en el scheduler central. Ante fallos:

- usa `scheduler status` y `scheduler history` para el checkpoint durable;
- usa `scheduler review retry` para una revisión recuperable, `scheduler
  cursor-retry` para un timeout Cursor elegible, `scheduler cursor-retry <run-id>
  --check` para inspeccionar evidencia de un fallo `cursor_failure` sin mutar el
  ledger, `scheduler cursor-retry <run-id> --force` para un turno inicial o una
  corrección ya terminados en `blocked(cursor_failure)`, incluido el de la hoja
  actual de una secuencia, o `scheduler extend` para
  autorizar más reviews tras agotar el presupuesto; consulta las condiciones en
  la [referencia CLI](../referencia/cli.md);
- usa `scheduler implementer-retry <run-id> --force` para un fallo genérico Codex
  terminado y con sesión ligada. La relación vive en
  `scheduler_codex_implementer_recoveries`; el sucesor guarda el intent y el record
  inmutables bajo `implementer/failure-recovery/`. Un estado `pending` conserva la
  reserva pero no puede ejecutar. Para la hoja vigente de secuencia,
  `scheduler_sequence_implementer_replacements` liga ordinal, generación, entrada
  congelada y adopción; lineage registra `implementer_retry`;
- para Cursor SDK v7/v8, el ledger
  `scheduler_cursor_sdk_reconciliations` y los records protegidos enlazan el
  intento fallido, journal, owner/key/agent, inventario pre-envío y resultado
  nativo. Solo una reconciliación exacta habilita el retry automático o manual.
  `cursor-retry --check/--force` y la rama Cursor de `implementer-retry` conservan
  agente, reviewer, presupuesto, ordinal y short name. Una creación incierta o
  cero/varios candidatos retienen la reserva sin redispatch; `scheduler abort`
  también espera cancelación/confirmación nativa antes de liberar ownership;
- `scheduler abort` cancela sin borrar artefactos ni cambios staged;
- para trabajo nuevo tras un run terminal, `scheduler submit` con
  `--short-name`, `--resubmission-id <uuid>`, modelo y razonamiento de review
  explícitos (controller A sigue siendo opcional);
  repetir submit sin esa opcion reutiliza el run terminal existente.

Los contratos de recovery legacy (`recover`, sucesores `interrupted`, checkpoints
`cursor`/`staging`) aplicaban solo al motor `runs/` retirado.

## Locks

`ai_dev_loop` usa:

- locks del scheduler en SQLite (tick lease, capacity, attempts);
- un lock por worktree bajo `$XDG_STATE_HOME/ai_dev_loop/repository-locks/`;
- `locks/cutover.cleanup.lock` durante cleanup destructivo legacy.

## SessionStart records

El hook global guarda metadata minima de sesiones Codex bajo:

```text
$XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
```

No guarda transcript content.

## Proyeccion `integration` (solo lectura)

Los comandos `integration run …`, `integration sequences …` y lecturas de contenido
leen `engine.sqlite3` y artefactos bajo `artifacts/` en transacciones de solo
lectura; no migran esquema ni reservas de escritura. `integration codex-capacity`
no persiste resultados de cuota: solo devuelve una proyeccion JSON efimera tras la
sonda acotada al CLI Codex en `PATH`.

## Secuencias con implementer por fase

Manifest v3 exige `short_name` de secuencia y fase, mientras definición/entrada
v2 sigue congelando runtimes históricos por ordinal y las hojas usan contexto
submitido v6. Manifest SDK v4 conserva los nombres y exige runtime/modelo/parámetros
completos por fase; definición/entrada v3 y contexto v8 congelan esa selección,
incluso en secuencias mixtas Cursor-SDK/Codex. Los envelopes de ciclo de vida de
secuencia siguen v1.
Entradas históricas v1 y contextos v4 conservan Cursor, identidad y hashes.
Preparaciones v1/v2 ya aceptadas conservan replay/materialización exactos; inputs
legacy nuevos deben actualizarse a v3.
El layout protegido `implementer/` aplica igualmente a hojas Codex. Una espera
inicial o de corrección conserva hoja, ordinal, reserva, prompt exacto, sesión y
trabajo parcial entre ticks/reinicio. Solo aceptación de review autoriza el
checkpoint no final; la fase final queda staged en `awaiting_finalization`.

Cada timeout Codex reintentable publica un intent write-once por intento fuente.
El ledger enlaza su hash, sesión, turno, prompt exacto, generación, contador y
espera. Un artefacto publicado antes de un CAS fallido queda huérfano e inerte;
`publish-or-verify` hace idempotente la convergencia del mismo intento.

Después de validar fuentes, instalar/actualizar el paquete es trabajo explícito
del operador. El scheduler instalado no carga automáticamente cambios unstaged,
staged o commits del worktree: ejecuta su versión instalada. No instalar ni
cambiar timers/linger como efecto de tests o revisión de esta fase. La validación
repository-wide, build y aceptación de instalación pertenecen al gate separado
de pipeline/operador. Los tests con fakes no equivalen a aceptación con modelos.

El dashboard instalado es otra proyección de solo lectura sobre esas mismas
autoridades. `dashboard serve` no crea ni repara `engine.sqlite3`, no migra el
schema y no cambia archivos bajo `artifacts/`. Prompt, review y output se leen
por los readers confinados existentes, solo después de una acción deliberada;
el navegador los conserva únicamente en memoria. Ver [Dashboard
local](dashboard-local.md).
