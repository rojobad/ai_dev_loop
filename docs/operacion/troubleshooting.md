# Troubleshooting

## MkDocs: `ERR_SSL_PROTOCOL_ERROR` en `localhost:8000`

Sintoma:

- el navegador muestra `ERR_SSL_PROTOCOL_ERROR` o "This site can't provide a secure connection";
- `uv run mkdocs serve` registra lineas ilegibles con `code 400`.

Causa:

- `mkdocs serve` expone HTTP plano en `http://127.0.0.1:8000/`;
- algunos navegadores, incluido Opera GX, fuerzan HTTPS cuando escribes `localhost:8000` sin esquema.

Accion:

1. Abre `http://127.0.0.1:8000/` (con `http://`, no `https://`).
2. O ejecuta `uv run mkdocs serve -o` para abrir la URL correcta automaticamente.
3. Si el navegador sigue forzando HTTPS, desactiva "Always use secure connections" / HTTPS-First para localhost o borra el estado HSTS de `localhost`.

Los warnings `code 400` con texto binario desaparecen cuando el navegador deja de enviar handshakes TLS al servidor HTTP.

## El worker A pide una passphrase SSH que no puedes introducir

Sintoma:

- `github doctor` informa `ssh-agent has no usable keys`;
- `ssh -T git@github.com` falla con `Permission denied (publickey)`;
- un worker detached A no tiene una terminal donde introducir la passphrase.

Causa: un `ssh-agent` cargado en otra terminal no siempre comparte
`SSH_AUTH_SOCK` con Codex Desktop o con un worker detached.

Accion: conserva la passphrase y usa un agente de usuario con socket fijo. Crea
`~/.config/systemd/user/ai-dev-loop-ssh-agent.service`:

```ini
[Unit]
Description=Persistent SSH agent for ai_dev_loop GitHub publication

[Service]
Type=simple
ExecStartPre=/usr/bin/rm -f %h/.ssh/ai-dev-loop-ssh-agent.sock
ExecStart=/usr/bin/ssh-agent -D -a %h/.ssh/ai-dev-loop-ssh-agent.sock
Restart=on-failure

[Install]
WantedBy=default.target
```

Agrega a `~/.ssh/config`:

```text
Host github.com
  IdentityAgent ~/.ssh/ai-dev-loop-ssh-agent.sock
```

Luego habilita el servicio y, desde cualquier terminal WSL donde sí puedas
introducir la passphrase, carga la clave:

```bash
mkdir -p ~/.config/systemd/user ~/.ssh
chmod 700 ~/.ssh
chmod 600 ~/.ssh/config ~/.config/systemd/user/ai-dev-loop-ssh-agent.service
systemctl --user daemon-reload
systemctl --user enable --now ai-dev-loop-ssh-agent.service
SSH_AUTH_SOCK="$HOME/.ssh/ai-dev-loop-ssh-agent.sock" ssh-add ~/.ssh/id_ed25519
ssh -T git@github.com
```

El preflight de publicación consulta la configuración efectiva de OpenSSH con
`ssh -G` sobre el destino del remote, toma el `IdentityAgent` resuelto (o, si
es `none`/ausente, un `SSH_AUTH_SOCK` heredado válido) y ejecuta `ssh-add -l`
sólo con ese socket. El worker detached no necesita heredar `SSH_AUTH_SOCK` ni
pedir la passphrase, pero la clave debe estar cargada en el agente que OpenSSH
usará. Comprueba finalmente:

```bash
ai_dev_loop github doctor --repo-path /ruta/al/repositorio
```

Tras reiniciar WSL o Windows, el servicio vuelve a iniciar pero no conserva la
clave descifrada: repite sólo `ssh-add` desde una terminal accesible. No elimines
la passphrase ni crees una deploy key sin cifrar para evitar el prompt.

## `prepare` rechaza el worktree

Causas comunes:

- staged changes preexistentes;
- archivos tracked modificados no relacionados;
- untracked files no permitidos;
- prompt source tracked o no ignorado;
- plan o config fuera del repo;
- symlink que escapa de la raiz del repo.

Acciones:

```bash
git status --short
git diff --cached --name-only
```

Limpia o mueve trabajo no relacionado antes de preparar el run. No uses `git reset` o `git clean` sin revisar manualmente.

## `start` falla por drift

`start` revalida el contrato de `prepare`. Falla si cambiaron:

- branch;
- HEAD;
- plan;
- prompt;
- baseline del worktree;
- repo path.

Accion: si el plan o prompt cambiaron legitimamente, ejecuta `prepare` de nuevo.

## Cursor auth/model probe falla

Verifica:

```bash
agent status --format json
agent models
```

El parser real acepta salidas como:

```text
composer-2.5-fast - Composer 2.5 Fast
```

Configura `cursor.model` con el identificador exacto, por ejemplo `composer-2.5-fast`.

## Codex review model no soportado

Sintoma comun:

- `start` o `resume` clasifica el modelo requerido como incompatible;
- `codex exec resume` indica que el modelo requiere una version mas nueva de Codex CLI.

Accion:

1. Consulta `codex --version` y `codex debug models`.
2. En TTY, acepta el prompt de update solo si quieres actualizar esa CLI WSL; la respuesta por defecto es no.
3. En non-TTY, usa `--update-tools` para autorizar el updater o ejecuta `codex update` manualmente.
4. El update puede agregar soporte, pero no garantiza que exista una version compatible. `ai_dev_loop` vuelve a probar version y catalogo.
5. `--allow-incompatible-tools` permite continuar bajo tu responsabilidad; no corrige la incompatibilidad.

Los updates no modifican Codex Desktop ni Cursor Desktop en Windows.

Para reasoning:

- Omite `review_reasoning_effort` para capturarlo de la sesion durante `prepare`.
- Si fijas un valor, usa solo `minimal`, `low`, `medium`, `high`, `xhigh`, `max` o `ultra`.

## Sesion grabada con un modelo y resume usa otro

En runs nuevos, esto no debe depender del default WSL: `prepare` captura modelo/reasoning de la sesion y cada review los pasa explicitamente.

Acciones:

1. Ejecuta `ai_dev_loop inspect <run-id>` y compara runtime de sesion, runtime efectivo y procedencia.
2. Si el run es historico de Fase 9 con ambos valores `null` y sin procedencia, su camino legacy omite overrides. Prepara un run nuevo; no interpretes esos `null` como session-derived.
3. Si existe un override explicito, revisa YAML/flags y vuelve a preparar para cambiarlo.

## `Failed to run pre-sampling compact`

Reanudar una sesion GPT-5.5 con GPT-5.6, o viceversa, puede requerir compactacion previa por diferencias entre familias. En versiones validadas se observo `pre-sampling compact`; no es un comportamiento universal.

Acciones:

1. Evita cambiar de familia dentro del mismo run; omite el override para usar el modelo capturado de la sesion.
2. Verifica que la Codex CLI WSL soporte ese modelo.
3. Si cambias YAML o flags, prepara un run nuevo.

Un mismatch de familia genera una advertencia operativa, no bloquea por si solo.

## Compatibilidad desconocida u offline

Si `agent models` o `codex debug models` no puede confirmar soporte, `ai_dev_loop` distingue `unknown` de incompatibilidad confirmada. Un resultado `unknown` no dispara el updater ni bloquea como una incompatibilidad confirmada, y no afirma que haya un update disponible.

Revisa conectividad/autenticacion y ejecuta los probes manualmente. `--update-tools` solo actua sobre incompatibilidades detectadas; `--allow-incompatible-tools` autoriza continuar cuando la incompatibilidad si fue confirmada.

## Falta SessionStart context

Verifica integracion:

```bash
ai_dev_loop integrations status --target wsl-cli
ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
```

Luego:

- abre `/hooks`;
- confia el hook;
- reinicia o reanuda la sesion;
- confirma que la sesion actual tiene el ID exacto.

Para Desktop, recuerda que la confianza se hace en Codex Desktop, no en WSL.

## Controller: 0 o N coincidencias

`controller status` resuelve por session ID exacto de controller y raiz del repo. No elige el run mas reciente.

- 0 matches no terminales: confirma el ID exacto de A, el `--repo-path` y que el prepare fue A/B; usa `--run-id` o `--include-terminal` solo si conoces el run.
- N matches: pasa `--run-id` con uno de los candidatos listados. No adivines por timestamp.

## Reviewer B activo o usado tras prepare

Sintoma: A lanzo el run pero B sigue recibiendo prompts o se usa la UI de B en paralelo.

Accion: deja B intacta. Todo review reanuda solo B. Si B se uso tras prepare, el contexto puede contaminarse; aborta si hace falta, inspecciona artefactos y prepara un run nuevo si el contrato ya no es confiable.

## Worker / launcher stale

`controller status` y `launch` verifican identidad del worker (PID vivo + PGID +
`/proc` starttime cuando esta disponible), no solo que el PID exista. Un PID
reutilizado o un registro `running` huerfano se trata como stale: no bloquea un
nuevo `launch` y no autoriza senalizacion. Inspecciona `locks/launcher.json` y
los logs del launcher si necesitas diagnostico manual.

Accion: conserva diagnosticos; usa `abort` (persiste el abort request) o
`status`/`inspect`/`logs`. No mates PIDs a mano por un registro dudoso. Si el
worker fallo tras progreso durable, usa `recover`/`resume` solo cuando la
elegibilidad lo permita.

## Falta capacidad de fork o mensaje A↔B

Los skills A/B dependen de capacidades de la app Codex (fork same-directory y mensaje autorizado de B a A).

Si faltan: los skills se detienen con explicacion acotada. No uses `--last`, no inventes session IDs y no scrapees rollouts para inferir parentesco. Completa handoff cuando la capacidad este disponible, o usa el flujo legacy sin `--controller-session-id` desde WSL.

## WSL distro ambiguo

Pasa el distro explicitamente:

```bash
ai_dev_loop integrations status \
  --target codex-desktop-wsl \
  --wsl-distro Ubuntu-22.04
```

Tambien puedes exportar:

```bash
export AI_DEV_LOOP_WSL_DISTRO=Ubuntu-22.04
```

## Windows Codex home no detectada

Pasa la home `.codex`, no el perfil de usuario:

```bash
ai_dev_loop integrations status \
  --target codex-desktop-wsl \
  --windows-codex-home "/mnt/c/Users/<usuario>/.codex"
```

La ruta debe ser absoluta y terminar en `.codex`.

## Puente `from-desktop` mal apuntado

Verifica:

```bash
ai_dev_loop integrations sessions status
```

Si reporta que el target no coincide, remueve y reinstala:

```bash
ai_dev_loop integrations sessions remove
ai_dev_loop integrations sessions install
```

`remove` solo borra el symlink `from-desktop`. Si hay un archivo o directorio no symlink en esa ruta, lo rechaza.

## `resume` rechaza drift de staged patch

Durante correcciones, el staged patch actual debe coincidir con el patch registrado por el orquestador. Si alguien modifico el index, `resume` falla.

Acciones:

- inspecciona `git/diffs/NN.patch`;
- revisa el index actual con `git diff --cached`;
- decide manualmente si debes abandonar el run o preparar uno nuevo.

## `recover` rechaza el run o pide dry-run

`resume` sigue rechazando `failed`. Para fallos elegibles:

```bash
ai_dev_loop recover --dry-run <failed-run-id>
```

Si el blocker es `post_cursor_fingerprint_missing` en un fallo de **correccion** (`correction_staging_failed`) y el status actual coincide con `NN-after-cursor.txt`:

```bash
ai_dev_loop recover --dry-run <failed-run-id> --adopt-current-cursor-output
ai_dev_loop recover <failed-run-id> --adopt-current-cursor-output
```

La recovery de staging inicial (`initial_staging_failed`, iteracion 1) exige `git/cursor-output/01.json` y coincidencia de fingerprint; no admite `--adopt-current-cursor-output`.

Si hay otros blockers (`staged_patch_drift`, `untracked_files`, `branch_mismatch`, `cursor_output_fingerprint_drift`, `initial_staging_does_not_support_adoption`, etc.), corrigelos o prepara un run nuevo. No mutes el run origen.

Si dry-run es elegible:

```bash
ai_dev_loop recover <failed-run-id>
ai_dev_loop resume <recovery-run-id> [--update-tools]
```

`recover` nunca actualiza CLIs ni invoca agentes. Si un sucesor tambien fallo, recupera ese sucesor (cadena), no el abuelo.

Para staging recovery (inicial o correccion), el sucesor `resume` ejecuta `git add -A` y Codex sin re-ejecutar Cursor.

## Cursor alcanzo el limite de uso del modelo

Sintoma:

- el run termina en `failed` durante un turno Cursor;
- `status` o la salida de `start`/`resume` indican limite de uso del modelo configurado;
- existe `git/cursor-output/NN.usage-limit-failure.json` en el run origen (Phase 13), o evidencia historica compatible con adopcion explicita (`stderr.txt` protegido + status after-cursor + ambos flags).

Por que `resume` no reabre el origen:

- `failed` es terminal; `resume` rechaza runs terminales;
- el origen queda inmutable para auditoria; la continuacion requiere un sucesor via `recover`.

Accion:

```bash
ai_dev_loop recover --dry-run <failed-run-id> --cursor-model auto
ai_dev_loop recover <failed-run-id> --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

Historico sin fingerprint contemporaneo (pre-Phase 13 o evidencia incompleta):

```bash
ai_dev_loop recover --dry-run <failed-run-id> --adopt-current-cursor-output --cursor-model auto
ai_dev_loop recover <failed-run-id> --adopt-current-cursor-output --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

No edites manualmente el repositorio entre `--dry-run` y el `recover` real; el segundo revalida status y fingerprint y rechaza drift.

Ejemplo operativo documentado (no ejecutar como validacion de implementacion):

```bash
ai_dev_loop recover --dry-run crypto-sentinel-20260712T205916Z-b2d828 \
  --adopt-current-cursor-output --cursor-model auto
ai_dev_loop recover crypto-sentinel-20260712T205916Z-b2d828 \
  --adopt-current-cursor-output --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

En TTY, `start`/`resume` pueden ofrecer crear el sucesor con el mismo chat y modelo `auto`. En scripts o CI, debes pasar `--cursor-model auto` explicitamente; no hay cambio automatico de modelo.

Trabajo parcial:

- no descartes manualmente cambios unstaged/untracked antes de `recover`; el fingerprint captura el contenido parcial al fallo;
- el envelope de continuacion embebe el prompt exacto previo para que Cursor retome el mismo chat;
- evita editar el worktree salvo que abandones el run y prepares uno nuevo.

Casos que `recover` rechaza:

- drift del fingerprint (`usage_limit_fingerprint_drift`);
- fingerprint ausente o invalido (`usage_limit_fingerprint_missing`, `usage_limit_fingerprint_invalid`);
- fallos Cursor ordinarios (timeout, auth, exit distinto) no clasificados como `cursor_usage_limit`;
- drift de branch/HEAD/plan/prompt, chat ID faltante, o proceso hijo activo.

## Pytest falla con temporales en `/mnt/c`

Usa temporales nativos WSL:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
```

Para simulaciones DrvFS puntuales, usa `-s` si la captura de pytest falla antes de coleccion.

## El worker de PR review no sale de `awaiting_bot_review` aunque el bot “aprobó”

Síntoma:

- el bot publicó un comentario general positivo;
- o aparece/desaparece la reacción `eyes` en el trigger;
- `status` sigue en `awaiting_bot_review` hasta timeout.

Causa: la ausencia de hilos, la presencia de `eyes` o la retirada de esa
reacción **no** completan el ciclo. Sólo cuenta un comentario general del login
en `github.reviewer_logins`, posterior a `request_created_at`, con un prefijo
exacto de `github.no_findings_completion.accepted_comment_prefixes` y una línea
estructural `Reviewed commit:` cuyo SHA coincide con el prefijo configurado del
`bound_head_sha`. `acknowledgement` es telemetría: timeout o reacción borrada
quedan como diagnóstico y el polling continúa sin republicar `@codex review`.

Acción:

1. Confirma `github.no_findings_completion.enabled: true` y el prefijo exacto
   del bot (un cambio de texto del bot se corrige en YAML, no relajando el
   matcher).
2. Verifica en `pr-review status` el acuse (`observed` / timeout diagnóstico) y
   que no haya hilos elegibles compitiendo con el comentario positivo.
3. Mantén **Automatic reviews** de Codex apagado mientras ai_dev_loop publica el
   trigger explícito.
4. Si el ciclo quedó `interrupted` por timeout de polling, aborta si hace falta
   y relanza/reanuda con los comandos existentes; **no** edites `state.json`.

```bash
ai_dev_loop pr-review status <run-id>
ai_dev_loop pr-review abort <run-id>
ai_dev_loop pr-review resume <run-id> [--controller-session-id <sesion-A>]
```

## Worker PR-review ausente/stale en `awaiting_bot_review`

Síntoma:

- `pr-review status` muestra `awaiting_bot_review` y `Worker: stale` o `absent`;
- el bot ya dejó hilos nuevos sobre el SHA ligado, pero no hay adjudicación;
- el lock `locks/pr-review-worker.json` apunta a un PID que ya no vive.

Causa típica (corregida en Phase 15.9): tras publicar el trigger de un ciclo
externo, el worker detonado terminaba sin continuar el polling y el intento de
auto-spawn veía su propio PID como vivo. Además, un
`expected_eligible_thread_ids` congelado del ciclo anterior podía marcar los
hilos nuevos como drift.

Acción (mismo run; no uses `recover` ni edites `state.json`):

```bash
ai_dev_loop pr-review status <run-id> --output json
# Desde el controlador A exacto:
ai_dev_loop pr-review resume <run-id> --controller-session-id <sesion-A>
```

Eso solo reengancha el poller. Puede validar PR/head en solo lectura, pero **no**
escribe en GitHub ni publica otro `@codex review`, y no crea Cursor ni invoca
Codex. Si el worker ya está `live` con identidad coincidente, el comando es
idempotente. Si hay drift de PR/head, launcher ambiguo (incluido reuso de PID),
o controlador incorrecto, falla sin writes.

## Freeze heredado entre ciclos externos (`eligible_thread_set_drift`)

Síntoma (histórico; Phase 15.9 ya evita el patrón en runs nuevos):

- `cycle_number` ya avanzó tras un `@codex review` válido;
- `waiting_for_user_attention` + `worker_outcome == eligible_thread_set_drift`;
- `expected_eligible_thread_ids` aún apunta a hilos del ciclo recuperado (ya
  procesados/resueltos), mientras el ciclo actual tiene hilos elegibles nuevos;
- lineage verificable: recovery directa `external_adjudication`, **o** recovery
  actual `reviewing` / `codex_review_result_artifact_missing` cuyo único source
  terminal tiene recovery `external_adjudication` con el mismo snapshot.

Causa: el freeze operativo se persistió antes del reset por ciclo de Phase 15.9.
No es fallo del bot, polling, SSH ni A/B.

Acción (mismo run; no uses `recover` ni edites `state.json`):

1. Publica en el PR un comentario **nuevo** y exacto (obligatorio también tras
   un continue previo que ya consumió el último comentario):

   ```text
   @rojobad /ai-dev-loop continue
   ```

2. Desde el controlador A:

   ```bash
   ai_dev_loop pr-review continue <run-id>
   ```

Eso limpia solo el freeze obsoleto del run actual, vuelve a
`awaiting_bot_review` y programa a lo sumo un worker. El source terminal queda
intacto. **No** republica `@codex review`, no crea sucesor, no invoca
Cursor/Codex en el comando y no relaja drift legítimo del ciclo actual. El
evento `pr_review_legacy_cycle_freeze_cleared` expone solo `source_cycle`,
`current_cycle` y `expected_thread_count`.

Dos caminos distintos cuando la limpieza histórica no aplica:

1. **Continue normal** (el run no es un candidato nested legacy-freeze): por
   ejemplo drift del mismo ciclo, sets distintos/vacíos, IDs no procesados u
   outcome distinto. El comentario se consume, el freeze válido del ciclo
   actual se conserva y el worker puede reanudarse; la resolución sigue siendo
   la habitual del usuario.
2. **Candidato nested inválido** (recovery actual
   `reviewing` / `codex_review_result_artifact_missing` con gates locales de
   drift, pero el source one-hop está ausente, no terminal, con identidad o
   ciclo inconsistente, o sin recovery `external_adjudication` verificada):
   fail-closed. La autorización de continue **no** se consume, no se muta
   estado ni eventos y no se programa worker. Corrige o resuelve esa lineage
   inválida antes de reintentar con un comentario exacto (el mismo sigue
   válido si no se consumió).

## Adjudicación GitHub falló con `invalid_json_schema` / `uniqueItems`

Causa histórica: el schema de respuesta enviado a Codex incluía `uniqueItems` en
`eligible_thread_ids`. El backend rechazó el schema; no hubo adjudicación, replies
ni Cursor. Los runs nuevos clasifican esto como
`adjudication_schema_incompatible` (`interrupted`). Los runs `failed` históricos
con evidencia estructurada en `github/cycles/NN/codex.events.jsonl` se recuperan
con un sucesor.

Acción (ejemplo PR #45 / run anonimizado del incidente):

```bash
ai_dev_loop pr-review recover crypto-sentinel-20260718T010234Z-317683 --dry-run
ai_dev_loop pr-review recover crypto-sentinel-20260718T010234Z-317683 --output json
ai_dev_loop pr-review resume <successor-run-id> --controller-session-id <sesion-A>
```

No uses `pr-review prepare` ni publiques otro `@codex review`. Si hay drift de SHA,
PR cerrado, hilos añadidos/eliminados/resueltos, o side effects previos
(processed/replied/resolved/Cursor), `recover`/`resume` se detienen sin writes.

## Revisión local Codex sin `codex/reviews/NN.json` tras Cursor

Causa observada (PR #45 / sucesor de adjudicación): Cursor y staging completaron
la corrección, pero `codex exec --output-last-message` no pudo escribir el
resultado estructurado porque el directorio padre no existía. El JSONL del
intento fallido no es fuente de decisión; hay que reintentar solo la revisión
local con la misma sesión B.

```bash
ai_dev_loop pr-review recover <failed-run-id> --dry-run
ai_dev_loop pr-review recover <failed-run-id> --output json
# Desde A, con el resume que devuelve recover:
ai_dev_loop pr-review resume <successor-run-id> --controller-session-id <sesion-A>
```

El checkpoint debe ser `reviewing` /
`codex_review_result_artifact_missing`. No reejecuta Cursor, no hace polling ni
adjudicación, y no publica otro `@codex review`. Si el patch staged o el
worktree driftaron, o ya hay replies/resolves/publicación, `recover` se detiene.

## Feedback externo accionable con staging vacío o baseline limpio antes de Cursor

Causa observada (PR #45 / run anonimizado `…176634`): tras adjudicación del
ciclo externo con hallazgos accionables, el orquestador reutilizó la iteración
local `01` completa y saltó a staging (`no staged changes after git add -A`)
sin enviar `prompts/fixes/github-02.txt` a Cursor.

Causa observada (PR #45 / run anonimizado `…a0f030`): tras publicar el fix y
recibir hallazgos del ciclo 3, el worktree estaba limpio en el HEAD enlazado,
pero el preflight de corrección comparó ese índice vacío con
`git/diffs/02.patch`. Un directorio `cursor/iterations/03` vacío creado antes
del preflight no es intento parcial ni debe clasificar el source como
`reviewing`.

Los runs nuevos programan una iteración fresca monotona
(`github_pr_review.external_cursor_iteration`), entregan el prompt externo
exacto y, para ese primer turno externo, exigen baseline limpio (identidad
Git, branch/HEAD = `initial_head`/`bound_head_sha`, indice/worktree limpios)
sin comparar un patch publicado anterior. Solo una corrección local posterior
a un finding Codex de la misma ronda exige el patch staged anterior.

Para el origen `failed` histórico con lifecycle `fixing_external_feedback`,
resultado/prompt externos validos, baseline limpio y sin evidencia durable de
la nueva iteración (entrada `iterations[NN]` o cualquier archivo bajo
`cursor/iterations/NN` / status / fingerprint / diff / review):

```bash
ai_dev_loop pr-review recover <failed-run-id> --dry-run
ai_dev_loop pr-review recover <failed-run-id> --output json
ai_dev_loop pr-review resume <successor-run-id> --controller-session-id <sesion-A>
```

El checkpoint debe ser `external_feedback_cursor` /
`external_feedback_cursor_not_started` (no `reviewing`). Reutiliza la
adjudicación persistida; no republica `@codex review` ni re-adjudica los
mismos hilos. Si PR/SHA/rama/hilos cambian, el baseline está sucio, el
prompt/resultado son invalidos, hay worker vivo, publicación parcial o un
intento Cursor parcial real, `recover` se detiene.

## Adjudicación externa con artefactos en disco pero estado atrasado

Causa observada (PR #45 / run anonimizado `…66d03c`): Codex B ya escribió
`github/cycles/05/result.json`, snapshot, report y `prompts/fixes/github-05.txt`,
pero un timeout GraphQL ocurrió antes de `save_run_state`. Los punteros
`last_external_*` / `external_fix_prompt_path` seguían en el ciclo 4 y
`recovery` conservaba el hash de `github-03` de una recovery anterior.

No edites `state.json` ni uses `pr-review recover` para este caso in-place.
Desde el controlador A:

```bash
ai_dev_loop pr-review resume <run-id> --controller-session-id <sesion-A>
```

`resume` hidrata `github_pr_review.external_adjudication` desde los artefactos
deterministas del `cycle_number` actual, agenda una sola corrección Cursor con
`github-05`, y no re-adjudica ni republica `@codex review`. El hash de recovery
histórico no bloquea ciclos posteriores. Si PR/SHA/hilos/prompt/baseline
realmente derivan, para sin side effects.

## Publicación detenida en `pre_commit` (ssh-agent sin identidad)

Causa observada (PR #45 / run anonimizado `…9488fe`): Cursor y la revisión local
Codex ya aceptaron el patch staged (sin hallazgos accionables). El worker llegó
a `publication_phase: pre_commit` con texto de publicación durable, pero el
preflight no encontró identidad utilizable en el agente SSH efectivo
(`IdentityAgent` vía `ssh -G`, o `SSH_AUTH_SOCK` heredado si aplica). Runs
históricos marcaron esto como `failed` vía `ValidationError` genérico; runs
nuevos lo clasifican como interrupción tipada `ssh_agent_no_identity` y
conservan el checkpoint reanudable.

Para el origen `failed` histórico con evidencia durable
(`lifecycle: failed` + `publication_phase: pre_commit`, HEAD/SHA sin commit
posterior, resultado local válido sin hallazgos, texto de publicación válido,
freeze de hilos intacto, PR abierto en el SHA enlazado, y patch staged live que
pasa baseline y coincide con `git/diffs/NN.patch` de la última iteración vía
`normalize_patch_text` — sólo CRLF y newline terminal):

```bash
# Cargar la clave en el socket persistente antes del resume (no durante recover).
SSH_AUTH_SOCK="$HOME/.ssh/ai-dev-loop-ssh-agent.sock" ssh-add ~/.ssh/id_ed25519

ai_dev_loop pr-review recover <failed-run-id> --dry-run
ai_dev_loop pr-review recover <failed-run-id> --output json
ai_dev_loop pr-review resume <successor-run-id> --controller-session-id <sesion-A>
```

El checkpoint debe ser `publication_pre_commit` /
`publication_pre_commit_interrupted`. El `resume` publica solamente: no abre
Cursor/Codex, no re-adjudica, no responde/resuelve hilos ni republica
`@codex review` antes del flujo normal tras una publicación exitosa. La
elegibilidad no usa `last_error` ni logs. Si el PR/SHA/rama/patch/hilos
cambiaron, hay worker vivo, fase `committed`/`pushed`/`pr_bound`, review local
accionable o texto corrupto, `recover` se detiene sin writes.

Un `github_pr_review.staged_patch_sha256` obsoleto (heredado del ciclo anterior
a la corrección externa) no es drift por sí solo. Si el artefacto de la última
iteración coincide normalizado con el index live, el sucesor adopta el hash raw
live de `git diff --cached --binary` (el mismo que verificará la publicación).
Eso no autoriza cambios de contenido distintos del artefacto ni edición del
`state.json` del origen. El hash de bytes del archivo artefacto
(`sha256_file`) no sustituye al fingerprint de publicación.
