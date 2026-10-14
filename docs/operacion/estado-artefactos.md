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
│       implementer/iterations/<NN>/final.txt
│       implementer/turns/<NN>.<attempt-id>.{jsonl,stderr.txt,metadata.json,...}
│       codex/fresh-reviewer-input.json
│       codex/fresh-reviewer-binding.json
│       git/admission-status.txt
├── repository-locks/
├── codex-sessions/             # metadata minima del hook SessionStart
└── locks/                      # locks de cutover y otros control-plane
```

- `scheduler submit` congela un run `queued` sin lanzar agentes ni mutar el repo.
- `scheduler start` autoriza el run para ticks; la proveniencia controller A es opcional.
- `scheduler tick` ejecuta preflight, el implementer congelado, staging, review y
  correcciones segun el checkpoint.
- Un submit con implementer Codex congela provider, modelo, effort y timeout aparte
  del reviewer. La admisión prueba ese Codex sin exigir el binario Cursor. El tick
  captura una sesión implementer autenticada, conserva cada intento bajo
  `implementer/`, normaliza staging al completar el turno y solo entonces crea el
  reviewer B independiente. Los contextos históricos sin esa metadata siguen Cursor.
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
- `scheduler abort` cancela sin borrar artefactos ni cambios staged;
- para trabajo nuevo tras un run terminal, `scheduler submit` con
  `--resubmission-id <uuid>`, modelo y razonamiento de review explícitos
  (controller A sigue siendo opcional);
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
