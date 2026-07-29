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

Cursor no puede commit, amend, reset, checkout/switch, stash, clean, merge, rebase, tag ni push. Durante el turno inicial y las correcciones, Cursor puede mutar el index (`git add`, `git restore --staged`, `git rm --cached`). El indice vacio/pre-staged se exige solo en el boundary confiable pre-Cursor (`prepare`/`start`), no despues de que Cursor termine. Tras Cursor, el orquestador siempre normaliza con `git add -A` (`stage_mode: all`) y Codex revisa el snapshot staged acumulativo completo. Solo unstagear un tracked no-ignored no lo excluye del snapshot final; para excluir un generado hay que actualizar `.gitignore` y quitarlo del index.

No permitido por el workflow local ordinario:

```bash
git commit
git push
git tag
git reset
git clean
git stash
```

Tras `pr-review create` (opt-in GitHub, origen `source_run`), el worker puede hacer `git commit` solo del patch staged aceptado y `git push` no-force de la rama preparada, despues de verificar el head remoto esperado. Tras `pr-review prepare` (origen `independent_pr`) no hay escritura GitHub hasta `pr-review start`, que publica el marcador de review y arranca el worker. Siguen prohibidos merge, force push, reset, clean, stash y unstage. Las credenciales GitHub viven solo en la sesion `gh` autenticada; el push Git usa SSH + `ssh-agent`.

El CLI `pr-review` (motor SQLite v2) separa preparacion de ejecucion:
`create`/`prepare` solo congelan `PreparedState` (sin workers, agentes, writes,
commits ni pushes). `start` es la unica puerta a efectos externos y lanza un
supervisor detached con metadata de ownership (token/PID/PGID/start time/
executable/run binding); nunca reporta `spawned` sin proceso propio. Status/history
v2 no exponen prompts, patches, bodies de threads, tokens, session IDs
completos, argv, PID/PGID ni environments; los resumenes de adjudicacion en
eventos son operacionales fijos. Abort v2 persiste primero y solo senala
procesos locales con ownership OS exacta. Phase 16.8 (Gate A) anade evidencia
fail-closed de reaccion `+1` sobre el trigger exacto (`accept_bot_thumbs_up`);
los artefactos protegidos guardan proveniencia tipada y hash-verificada, no solo
un reaction ID. **Gate A** valida con fakes/process boundaries; **Gate B** es
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

`recover` es solo lectura sobre el repositorio: no hace `git add`, no altera el index y no reescribe el working tree. El staging del sucesor ocurre solo con `resume`.

## Clasificacion de limite de uso de Cursor

- La clasificacion usa stderr crudo capturado en artefactos protegidos (`cursor/iterations/NN/stderr.txt`), no el texto de `last_error`.
- Solo coincide la senal conservadora `ActionRequiredError` con marcadores de limite de uso y cambio de modelo; fallos genericos no califican.
- `status`, `inspect` y eventos estructurados usan codigos seguros (`cursor_usage_limit`) y resumenes breves; no incluyen detalles de facturacion ni stderr completo en salida normal.
- `recover` valida el fingerprint de contenido parcial (`git/cursor-output/NN.usage-limit-failure.json` en Phase 13, o `git/cursor-output/NN.usage-limit-adopted.json` tras adopcion historica explicita) contra el worktree actual antes de crear un sucesor.
- El run origen permanece `failed` e inmutable; el sucesor conserva el chat ID y congela el modelo fallback solicitado con `--cursor-model`.

## Proteccion del contrato preparado

`prepare` captura:

- branch;
- HEAD;
- baseline Git status;
- plan aprobado;
- prompt exacto;
- configuracion fuente y efectiva;
- Codex session ID (reviewer);
- controller session ID cuando se paso `--controller-session-id`;
- modelo y reasoning capturados de la sesion;
- modelo y reasoning efectivos, con procedencia `session` o `explicit`;
- hashes SHA-256.

`start` y `resume` revalidan esos datos antes de mutar. Si plan, prompt, branch, HEAD o baseline cambian inesperadamente, el run falla.

## Identidad de agentes

- Un run crea o reutiliza exactamente un Cursor chat ID.
- Todo review usa `codex exec resume <exact-reviewer-session-id>` (sesion B en flujo A/B).
- En runs A/B, el controller session ID (A) se persiste solo para lookup/control; no se reanuda para review.
- Controller y reviewer IDs son sensibles: la salida por defecto (`status`, `controller status`, logs humanos) los acorta o muestra prefijos; no imprimas IDs completos en chats compartidos.
- Nunca se usa `--last`.
- El orquestador no adivina session IDs.
- El orquestador no crea una sesion nueva de Codex para review ni forks de conversacion (el fork A→B es accion de la app Codex).
- En runs nuevos, `prepare` captura modelo y reasoning de la sesion exacta. Los overrides explicitos ganan por campo; cada review envia ambos valores efectivos.
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
