# Ejecutar runs

Un run pasa por cinco comandos principales:

```bash
ai_dev_loop prepare
ai_dev_loop start <run-id>
ai_dev_loop resume <run-id>
ai_dev_loop abort <run-id>
ai_dev_loop recover <failed-run-id>
```

## `prepare`

`prepare` se ejecuta desde la sesion original de Codex cuando el plan y el prompt ya fueron aprobados.

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<session-id-exacto>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

Hace esto:

- lee el prompt exacto desde stdin;
- rechaza stdin vacio;
- resuelve configuracion;
- valida repositorio, branch, HEAD, paths y worktree;
- rechaza staged changes preexistentes;
- permite solo excepciones controladas para plan/prompt;
- copia snapshots del plan y prompt;
- calcula hashes SHA-256;
- crea `state.json`, `manifest.json`, configuracion efectiva y baseline Git;
- devuelve `start_command`;
- no ejecuta Cursor ni Codex.

Si el plan o prompt cambian despues de `prepare`, prepara un nuevo run.

## `start`

`start` ejecuta el loop completo:

```bash
ai_dev_loop start <run-id>
```

Antes de invocar agentes:

- adquiere locks de run y repositorio;
- revalida branch, HEAD, hashes y baseline;
- verifica `git`, `agent` y `codex`;
- verifica autenticacion cuando es seguro;
- verifica modelo Cursor;
- exige el session ID Codex capturado;
- advierte que no uses la UI interactiva original.

Luego coordina:

1. Cursor implementa en el chat del run.
2. `ai_dev_loop` ejecuta staging controlado con `git add -A`.
3. Codex revisa staged changes reanudando la sesion exacta.
4. Si hay findings, Codex devuelve un `cursor_fix_prompt`.
5. Cursor corrige usando el mismo chat (puede mutar el index; el orquestador vuelve a normalizar con `git add -A`).
6. Se repite hasta no findings o limite de iteraciones.

Estados terminales principales:

| Estado | Significado |
| --- | --- |
| `completed` | Sin findings accionables; tests pasaron o no aplican. |
| `completed_with_residual_risk` | Sin findings accionables, pero tests fallaron, fueron bloqueados o hay riesgo residual. |
| `max_iterations_reached` | Codex aun reporto findings en el ultimo review permitido. |
| `failed` | Fallo validacion, CLI externa, artefacto, schema o seguridad. |
| `interrupted` | Timeout o interrupcion recuperable. |
| `aborted` | Cancelacion solicitada por el usuario. |

## `resume`

```bash
ai_dev_loop resume <run-id>
```

`resume` continua desde checkpoints durables sin cambiar identidades:

- reusa `state.cursor.chat_id`;
- reusa `state.codex.session_id`;
- no repite turns completos si los artefactos prueban que terminaron;
- conserva intentos parciales antes de reintentar;
- valida que el staged patch actual coincida con el artefacto registrado cuando corresponde.

Checkpoints soportados:

- `prepared`;
- `waiting_for_cursor_fix`;
- `running_cursor`;
- `staging`;
- `reviewing`;
- `interrupted`.

Estados terminales como `completed`, `failed`, `aborted` o `max_iterations_reached` no se reanudan con `resume`. Para ciertos `failed` elegibles, usa `recover` (abajo).

## `recover`

```bash
ai_dev_loop recover --dry-run <failed-run-id>
ai_dev_loop recover <failed-run-id>
ai_dev_loop recover --dry-run <failed-run-id> --adopt-current-cursor-output
ai_dev_loop recover <failed-run-id> --adopt-current-cursor-output
ai_dev_loop recover --dry-run <failed-run-id> --cursor-model auto
ai_dev_loop recover <failed-run-id> --cursor-model auto
```

`recover` no edita el run terminal de origen. Crea un run sucesor distinto cuando el fallo es recuperable.

Checkpoints elegibles:

- `reviewing` / `process_review`: Cursor + staging completos; staged patch actual coincide; sin unstaged/untracked.
- `staging`: correccion con Cursor completo y staging incompleto; fingerprint post-Cursor verificado, o adopcion explicita historica.
- `cursor`: turno Cursor interrumpido por limite de uso del modelo configurado; fingerprint de contenido parcial verificado; requiere `--cursor-model <modelo>` (por ejemplo `auto`).

Requisitos comunes:

- el origen esta exactamente en `failed`;
- coinciden root/Git dirs/branch/HEAD del origen;
- el chat ID de Cursor y el session ID de Codex estan presentes y coherentes;
- no hay proceso hijo activo o metadata ambigua.

Para staging historico sin `git/cursor-output/NN.json`, `--adopt-current-cursor-output` exige que el status porcelain actual coincida exactamente con `NN-after-cursor.txt`. Es una atestacion del usuario (no prueba criptografica del intervalo historico).

No recupera:

- `completed`, `completed_with_residual_risk`, `max_iterations_reached`, `aborted`;
- turns de Cursor incompletos;
- drift de plan/prompt/branch/HEAD;
- fingerprint post-Cursor divergente (salvo adopcion valida);
- sesiones o chats faltantes.

Comportamiento:

- `--dry-run` solo analiza (sin locks persistentes, sin directorios nuevos, sin Git mutation);
- sin `--dry-run`, crea un sucesor `interrupted` con lineage `recovery` o reutiliza uno activo equivalente;
- preserva exactamente `codex.session_id` y `cursor.chat_id`;
- runs Phase 10 conservan runtime congelado; Phase 9 legacy puede capturar runtime de la sesion exacta (`phase9_session_capture`);
- no lanza Cursor, Codex ni updaters;
- para checkpoint `staging`, `resume` ejecuta staging y luego Codex sin re-ejecutar Cursor;
- el origen permanece `failed` e inmutable byte a byte.

Si un sucesor previo fallo, recupera ese sucesor (cadena), no saltes generaciones. Si el sucesor ya completo, no se crea otro para el mismo checkpoint.

Ejemplo sanitizado (review):

```text
Source run: sample-project-20260711T010911Z-abcdef
Recovery run: sample-project-20260711T011500Z-fedcba
Recovered checkpoint: Codex review, iteration 1
Cursor chat: 1c9d071f…
Session runtime: gpt-5.6-sol / high
Runtime migration: phase9_session_capture
Repository and staged patch: verified
Next command: ai_dev_loop resume sample-project-20260711T011500Z-fedcba
```

Ejemplo sanitizado (staging con adopcion historica):

```text
Source run: sample-project-20260711T133243Z-d32b0c
Recovery run: sample-project-20260711T140000Z-abcdef
Recovered checkpoint: staging, iteration 2
Cursor output: explicitly adopted from matching historical after-cursor status
Repository identity: verified
Git mutation performed by recover: none
Next command: ai_dev_loop resume <recovery-run-id>
```

Pasa `--update-tools` a `resume` solo si la compatibilidad de CLI lo requiere. `recover` nunca actualiza herramientas.

### Recovery por limite de uso de Cursor

Cuando Cursor falla con la condicion reconocida de limite de uso (`cursor_usage_limit`), el run origen queda en `failed` e inmutable. No uses `resume` sobre el origen.

Comportamiento:

- el origen conserva el mismo `cursor.chat_id`; el sucesor lo reutiliza sin crear chat nuevo;
- `--cursor-model` congela el modelo fallback solicitado en el sucesor (por ejemplo `auto`); no se infiere desde YAML;
- `recover` escribe un envelope de continuacion con el prompt exacto previo y valida el fingerprint de trabajo parcial;
- tras un Cursor exitoso en el sucesor, el flujo normal continua con staging (`git add -A`) y Codex review.

Recuperacion explicita:

```bash
ai_dev_loop recover --dry-run <failed-run-id> --cursor-model auto
ai_dev_loop recover <failed-run-id> --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

En TTY, si `start` o `resume` detectan este fallo durable, pueden ofrecer crear el sucesor con `--cursor-model auto` y continuar con `resume`. La respuesta por defecto es no.

En non-TTY nunca se cambia de modelo automaticamente: imprime el comando `recover` explicito y termina con error.

## `abort`

```bash
ai_dev_loop abort <run-id>
```

`abort` solicita cancelacion de un run no terminal.

Hace esto:

- escribe `locks/abort-request.json`;
- si hay un child Cursor/Codex activo, intenta terminar su process group;
- marca el run como `aborted` cuando es seguro;
- preserva working tree, index staged y artefactos;
- no elimina logs ni salidas parciales.

No hace:

- `git reset`;
- `git clean`;
- `git stash`;
- `git commit`;
- `git push`;
- unstaging;
- borrado de artefactos.

Si metadata de proceso es ambigua, falla de forma conservadora y deja diagnosticos.
