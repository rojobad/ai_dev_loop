# Ejecutar runs

Un run pasa por cuatro comandos principales:

```bash
ai_dev_loop prepare
ai_dev_loop start <run-id>
ai_dev_loop resume <run-id>
ai_dev_loop abort <run-id>
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
5. Cursor corrige usando el mismo chat.
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

Estados terminales como `completed`, `failed`, `aborted` o `max_iterations_reached` no se reanudan.

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
