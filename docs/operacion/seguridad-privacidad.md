# Seguridad y privacidad

`ai_dev_loop` esta diseñado para fallar de forma conservadora y preservar evidencia local.

## Operaciones Git permitidas

Permitido:

```bash
git add -A
git status
git diff
git rev-parse
```

No permitido por el orquestador:

```bash
git commit
git push
git tag
git reset
git clean
git stash
git restore --staged
```

El usuario decide manualmente si commitea despues de revisar el resultado final.

## Proteccion del contrato preparado

`prepare` captura:

- branch;
- HEAD;
- baseline Git status;
- plan aprobado;
- prompt exacto;
- configuracion fuente y efectiva;
- Codex session ID;
- modelo y reasoning capturados de la sesion;
- modelo y reasoning efectivos, con procedencia `session` o `explicit`;
- hashes SHA-256.

`start` y `resume` revalidan esos datos antes de mutar. Si plan, prompt, branch, HEAD o baseline cambian inesperadamente, el run falla.

## Identidad de agentes

- Un run crea o reutiliza exactamente un Cursor chat ID.
- Todo review usa `codex exec resume <session-id-exacto>`.
- Nunca se usa `--last`.
- El orquestador no adivina session IDs.
- El orquestador no crea una sesion nueva de Codex para review.
- En runs nuevos, `prepare` captura modelo y reasoning de la sesion exacta. Los overrides explicitos ganan por campo; cada review envia ambos valores efectivos.
- Nunca se infieren valores heredados desde WSL `config.toml` ni desde el default de Codex CLI.
- No uses la UI interactiva original de esa sesion en paralelo con `start`/`resume`.

## Prompts y findings

`ai_dev_loop` transporta texto, no lo reescribe:

- no regenera el prompt inicial;
- no resume findings para crear prompts;
- no traduce ni mejora `cursor_fix_prompt`;
- reenvia exactamente el fix prompt que Codex emitio en JSON validado.

## Output sensible

Por defecto, la CLI no imprime:

- prompts completos;
- prompts de correccion;
- patches staged completos;
- Markdown completo de reviews;
- JSONL crudo de agentes;
- transcript contents;
- tokens o auth payloads;
- entornos completos de procesos.

Los artefactos crudos se guardan localmente con permisos restrictivos cuando el filesystem lo permite.

`prepare` puede leer rollouts JSONL de una sesion UUID exacta, pero solo procesa eventos allowlisted (`session_meta`, `thread_settings_applied`, `turn_context`, incluidos wrappers `event_msg`). No retiene mensajes, prompts, herramientas ni payloads arbitrarios. `codex/session-runtime.json` contiene solo metadata permitida, un prefijo del session ID y ninguna ruta absoluta al rollout.

## Actualizaciones de CLIs

- Solo flags de `start`/`resume` o consentimiento interactivo autorizan updates; `ai_dev_loop.yaml` nunca puede autorizarlos.
- Se ejecuta unicamente `[configured_command, "update"]` con `shell=False`.
- Non-TTY nunca pregunta ni actualiza implicitamente.
- Un abort pendiente impide prompts y lanzamientos de updater.
- Solo se actualizan CLIs WSL, no aplicaciones Desktop de Windows.

## Hooks

El hook `SessionStart`:

- valida session IDs antes de usarlos como nombres de archivo;
- acepta solo `startup`, `resume`, `clear`, `compact`;
- escribe metadata minima;
- no lee transcript contents;
- no copia auth material;
- responde JSON valido incluso ante input incompleto.

## Codex Desktop/WSL

No compartas toda la home `.codex` entre Windows y WSL.

Seguro:

```text
~/.codex/sessions/from-desktop -> /mnt/c/Users/<usuario>/.codex/sessions
```

No seguro:

```text
CODEX_HOME=/mnt/c/Users/<usuario>/.codex
~/.codex -> /mnt/c/Users/<usuario>/.codex
```

El puente soportado expone rollouts de sesiones por ID. No expone SQLite, auth, indices ni toda la home.
