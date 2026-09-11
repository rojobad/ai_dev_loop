# Seguridad y privacidad

`ai_dev_loop` esta diseñado para fallar de forma conservadora y preservar evidencia local.

## Operaciones Git permitidas

Permitido por el orquestador:

```bash
git add -A
git status
git diff
git rev-parse
```

Durante una correccion, Cursor puede mutar el index cuando el fix lo requiere:

```bash
git add
git restore --staged
git rm --cached
```

Cursor no puede commit, amend, reset, checkout/switch, stash, clean, merge, rebase, tag ni push. Durante el turno inicial y las correcciones, Cursor puede mutar el index (`git add`, `git restore --staged`, `git rm --cached`). El indice vacio/pre-staged se exige solo en el boundary confiable pre-Cursor del primer tick del scheduler, no despues de que Cursor termine. Tras Cursor, el orquestador siempre normaliza con `git add -A` (`stage_mode: all`) y Codex revisa el snapshot staged acumulativo completo. Solo unstagear un tracked no-ignored no lo excluye del snapshot final; para excluir un generado hay que actualizar `.gitignore` y quitarlo del index.

No permitido por el workflow local ordinario:

```bash
git commit
git push
git tag
git reset
git clean
git stash
```

El CLI `pr-review` y el motor SQLite v2 fueron retirados en Phase 17.7. El workflow
local soportado no hace commit, push ni escritura GitHub desde el scheduler. Las
credenciales GitHub, si se usan fuera de este producto, viven solo en la sesion `gh`
autenticada; el push Git usa SSH + `ssh-agent`.

El scheduler central separa congelado (`scheduler submit`) de ejecucion (`scheduler start`
+ `scheduler tick`). `scheduler abort` persiste primero y solo senala procesos locales
con ownership OS exacta. Status/history del scheduler no exponen prompts, patches,
tokens, session IDs completos, argv, PID/PGID ni environments. **Gate A** valida con
fakes/process boundaries; **Gate B** es
aceptacion live pendiente hasta completar el ciclo controlado en parish360-poc.

### Opcional: conservar la llave SSH durante la sesion WSL

Si la llave SSH tiene passphrase, se puede usar `keychain` para cargarla una vez
por sesion WSL, de modo que las terminales posteriores y los workers detached
hereden el agente:

```bash
sudo apt update
sudo apt install -y keychain
```

Anade manualmente esta linea a `~/.bashrc`:

```bash
eval "$(keychain --eval --quiet id_ed25519)"
```

La primera terminal abierta despues de reiniciar WSL/Windows pedira la
passphrase. No la pedira de nuevo mientras la misma sesion WSL siga activa; al
ejecutar `wsl --shutdown` o reiniciar, la llave deja de estar en memoria. No se
recomienda quitar la passphrase de la llave para evitar ese aviso.

Sin el ciclo GitHub, el usuario decide manualmente si commitea despues de revisar el resultado final.

## Clasificacion de limite de uso de Cursor

- La clasificacion usa stderr crudo capturado en artefactos protegidos del scheduler,
  no el texto resumido de `last_error`.
- Solo coincide la senal conservadora `ActionRequiredError` con marcadores de limite
  de uso y cambio de modelo; fallos genericos no califican.
- `scheduler status` y `scheduler history` usan codigos seguros y resumenes breves;
  no incluyen detalles de facturacion ni stderr completo en salida normal.
- Un limite de uso verificado transiciona el run a `waiting_usage_limit`, persiste
  un fingerprint y programa un timer de reintento; el mismo run continua cuando
  `scheduler tick` ejecuta tras `wait_until`. No crea un sucesor ni exige
  `scheduler submit` fresco salvo que el operador decida abandonar el run.

## Proteccion del contrato congelado en submit

`scheduler submit` congela en el ledger:

- la raiz canonica del worktree y su identidad de repositorio;
- plan aprobado y prompt exacto;
- configuracion fuente y efectiva;
- `--controller-session-id`, `--codex-review-model` y
  `--codex-review-reasoning-effort`;
- hashes SHA-256 de inputs inmutables.

La admision one-shot de branch, HEAD y estado del worktree (`require_clean_worktree`,
entre otros) ocurre en el primer tick, no en submit. El scheduler no ejecuta
comprobaciones continuas de baseline durante los turnos Cursor/Codex posteriores;
solo revalida los inputs congelados y los checkpoints durablemente registrados.

## Identidad de agentes

- Un run crea o reutiliza exactamente un Cursor chat ID.
- Todo review resume la identidad Codex exacta congelada para ese run.
- En runs controller A frescos, A elige `--codex-review-model` y
  `--codex-review-reasoning-effort` en `scheduler submit`; no hay fallback desde
  YAML, sesion preexistente ni default de CLI en review time.
  El scheduler crea exactamente un B con `codex exec` en `--sandbox read-only` en
  el primer review, captura su session ID y los reviews posteriores usan
  `codex exec resume <exact-id>` con el mismo modelo y reasoning congelados.
- En runs controller A, el controller session ID (A) se persiste solo para
  lookup/control; no se reanuda para review.
- Controller y reviewer IDs son sensibles: la salida por defecto (`status`, `controller status`, logs humanos) los acorta o muestra prefijos; no imprimas IDs completos en chats compartidos.
- Nunca se usa `--last`.
- El orquestador no adivina session IDs.
- Nunca se infieren valores heredados desde WSL `config.toml` ni desde el default de Codex CLI.
- No uses la UI del reviewer (B) en paralelo con el worker; en legacy, no uses la UI de la sesion unica con `start`/`resume`.

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
- session IDs completos de controller o reviewer;
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
