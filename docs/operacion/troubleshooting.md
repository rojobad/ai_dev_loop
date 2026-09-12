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
ai_dev_loop doctor --repo /ruta/al/repositorio
```

Tras reiniciar WSL o Windows, el servicio vuelve a iniciar pero no conserva la
clave descifrada: repite sólo `ssh-add` desde una terminal accesible. No elimines
la passphrase ni crees una deploy key sin cifrar para evitar el prompt.

## El primer `scheduler tick` rechaza el worktree

Phase 17.7 retiro `prepare` y `start`. La admision one-shot del worktree ocurre en
el primer tick, no en `scheduler submit`.

Causas comunes:

- staged changes preexistentes cuando `require_clean_worktree: true`;
- archivos tracked modificados no relacionados;
- untracked files no permitidos;
- prompt source tracked o no ignorado;
- plan o config fuera del repo;
- symlink que escapa de la raiz del repo.

Acciones:

```bash
git status --short
git diff --cached --name-only
ai_dev_loop scheduler status <run-id>
```

Limpia o mueve trabajo no relacionado antes del primer tick. Si el plan o prompt
cambiaron legitimamente, haz un `scheduler submit` fresco.

## `scheduler tick` falla por drift de inputs congelados

El tick revalida inputs congelados en submit. Falla si cambiaron branch, HEAD,
plan, prompt o repo path respecto al ledger.

Accion: si el plan o prompt cambiaron legitimamente, ejecuta `scheduler submit` de nuevo.

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

## Cursor o Codex no se encuentra desde `scheduler tick`

Los servicios de usuario de systemd suelen usar un `PATH` más reducido que la
terminal WSL. Desde esta versión, `scheduler submit` resuelve los nombres de
`cursor.command` y `codex.command` en la terminal de A y congela rutas absolutas
en los artefactos protegidos; no hace falta —ni conviene— guardar rutas locales
en `ai_dev_loop.yaml` ni modificar el `PATH` global de systemd.

La unidad empaquetada `ai-dev-loop-scheduler-tick.service` localiza el propio CLI
con `/usr/bin/env` y un `PATH` acotado que incluye `%h/.local/bin` (layout habitual
de `uv tool`) mas los binarios del sistema. No invoca un shell ni lee perfiles
interactivos. Si el timer falla con exit 203/EXEC, reinstala con
`ai_dev_loop scheduler timer validate` y `scheduler timer install` tras confirmar
que `command -v ai_dev_loop` funciona en una terminal normal.

Si submit indica que no encuentra uno de los ejecutables, verifica desde la
misma terminal de A:

```bash
command -v agent
command -v codex
```

Repara la instalación o el `PATH` de esa terminal y prepara un `scheduler submit`
fresco. Un run que ya quedó bloqueado preserva su contexto congelado y no se
reintenta automáticamente.

## Cursor chat creation timeout o fallo ambiguo

`agent create-chat` corre con timeout acotado, registro de proceso activo y
control de abort igual que los turnos de implementacion. Si la creacion hace
timeout, sale con codigo distinto de cero, devuelve un ID invalido, o se aborta
antes de persistir un chat ID durable, el run falla cerrado como `failed` o
`aborted` terminal.

- Inspecciona artefactos protegidos bajo `cursor/create-chat/` en el directorio del run.
- No `resume` el mismo run esperando un chat de reemplazo; prepara un run nuevo.
- Una prueba standalone exitosa de `agent create-chat` no repara un run ambiguo.

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

Para reasoning en runs nuevos del scheduler, pasa `--codex-review-reasoning-effort`
explicito en `scheduler submit`. No se captura de la sesion Codex ni de YAML en
submit time.

## Modelo o reasoning de review no coinciden con lo esperado

En el scheduler, modelo y reasoning se congelan en submit:

1. Ejecuta `ai_dev_loop scheduler status <run-id>` y revisa artefactos
   `codex/fresh-reviewer-input.json` bajo `artifacts/`.
2. Si necesitas otro modelo o reasoning, haz `scheduler submit` fresco con flags
   explicitos; no hay override silencioso desde YAML tras submit.

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

## Reviewer B creado o usado fuera del worker

Sintoma: el worker ya creo B en el primer review, pero otra sesion Codex recibe prompts de review o se usa su UI en paralelo.

Accion: deja intacta la identidad B capturada por el worker. Todo review reanuda solo esa session ID. Si otra sesion participo en el review, el contexto puede contaminarse; aborta si hace falta, inspecciona artefactos y prepara un run nuevo si el contrato ya no es confiable.

En runs controller A frescos, B no existe en `prepare`: el worker lo crea una sola vez en el primer review con `codex exec` read-only. No pases `--codex-session-id` en `prepare` ni `scheduler submit`.

## Bootstrap de reviewer ambiguo o bloqueado

Sintoma: el primer review fallo con bootstrap incierto (`fresh Codex reviewer bootstrap is uncertain`) o el run quedo bloqueado sin session ID de B.

Accion: inspecciona `codex/events/NN.jsonl` y `codex/fresh-reviewer-bootstrap-uncertainty.json`. No reintentes bootstrap ni crees un segundo B. Prepara o `scheduler submit` un run fresco con `--codex-review-model` y `--codex-review-reasoning-effort` explicitos.

## Run bloqueado o tick sin progreso

Usa:

```bash
ai_dev_loop scheduler status <run-id>
ai_dev_loop scheduler history <run-id>
ai_dev_loop controller status --repo-path ... --run-id <run-id>
```

Accion: conserva diagnosticos; usa `scheduler abort` si necesitas cancelar sin
borrar artefactos ni cambios staged. No existe `recover` publico en el scheduler;
para trabajo nuevo, `scheduler submit` fresco.

## Reviewer B duplicado o sesion incorrecta

Los runs controller A frescos no admiten `--codex-session-id` en submit. El scheduler
crea exactamente un B en el primer review con `codex exec` read-only y reanuda
solo esa sesion despues. No uses `--last` ni un segundo B.

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

## Correccion con staged patch drift

Durante correcciones, el staged patch actual debe coincidir con el checkpoint del
scheduler. Si alguien modifico el index fuera del tick, el siguiente tick puede fallar.

Acciones:

- revisa el index actual con `git diff --cached`;
- consulta `scheduler history <run-id>`;
- decide manualmente si abortas el run o haces `scheduler submit` fresco.

## Recuperacion de runs fallidos

`recover` y `resume` legacy fueron retirados en Phase 17.7. El scheduler no crea
sucesores automaticos desde runs `failed`. Ante un fallo:

- inspecciona `scheduler status` y `scheduler history`;
- preserva artefactos bajo `artifacts/` para auditoria manual;
- para continuar trabajo, usa `scheduler submit` con `--resubmission-id <uuid>`
  y los mismos inputs congelados si aun aplican.

## Repetir submit tras abort sin crear un run nuevo

Sintoma:

- tras `scheduler abort`, el mismo `scheduler submit` devuelve `reused_existing: true`
  con `state_kind: aborted` y sin accion de `scheduler start`;
- `scheduler start` sobre el run abortado falla porque la reserva ya se libero.

Causa: el submit idempotente base reutiliza el run terminal inmutable; no revive
ni reencola ese run.

Accion segura:

1. conserva el run abortado como registro de auditoria;
2. elige un UUID nuevo (`uuidgen`) y repite submit con `--resubmission-id`;
3. reutiliza exactamente ese UUID si necesitas repetir el comando sin duplicar;
4. autoriza el run nuevo con `scheduler start` cuando quede `queued`.

## Cursor alcanzo el limite de uso del modelo

Sintoma:

- `scheduler status` muestra `waiting_usage_limit` con `cursor_wait_until`;
- `scheduler history` registra `cursor_usage_limit_detected`;
- artefactos protegidos bajo `artifacts/` incluyen fingerprint y envelope de
  continuacion para el mismo chat ID.

Accion segura:

```bash
ai_dev_loop scheduler status <run-id>
ai_dev_loop scheduler history <run-id>
```

Espera hasta `cursor_wait_until` y ejecuta `ai_dev_loop scheduler tick`. El
scheduler reanuda el mismo run con el chat ID preservado y el reintento verificado
de usage-limit. No descartes trabajo parcial unstaged/untracked antes del tick.

El run solo debe tratarse como terminal si `scheduler status` indica un bloqueo
distinto (`blocked`, `failed`, etc.) o si decides abortar explicitamente con
`scheduler abort`. Los contratos legacy `recover --cursor-model` aplicaban solo al
motor `runs/` retirado.

## Pytest falla con temporales en `/mnt/c`

Usa temporales nativos WSL:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
```

Para simulaciones DrvFS puntuales, usa `-s` si la captura de pytest falla antes de coleccion.

## PR-review v2 y `github doctor` retirados (Phase 17.7)

Los comandos `pr-review`, `github doctor` y el motor bajo
`$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/` ya no tienen superficie CLI publica.
El workflow soportado es el scheduler central.

Para retirar estado legacy de forma controlada:

```bash
ai_dev_loop scheduler cutover cleanup --dry-run --confirm delete-legacy-state
```

La eliminacion real requiere aceptacion humana independiente. Consulta
[Desinstalacion y limpieza](desinstalacion-limpieza.md) y el historial archivado
en `archive/implementation-history/` para contexto de PR-review v2.

## Scheduler central: abort, bloqueos y timer (Phase 17.6)

Ledger: `$XDG_STATE_HOME/ai_dev_loop/engine.sqlite3` y artefactos bajo
`$XDG_STATE_HOME/ai_dev_loop/artifacts/`.

Comandos utiles:

```bash
ai_dev_loop scheduler status <run-id> --output json
ai_dev_loop scheduler history <run-id> --limit 50 --order newest --output json
ai_dev_loop scheduler abort <run-id>
ai_dev_loop scheduler timer validate
systemctl --user status ai-dev-loop-scheduler-tick.timer   # manual; no auto-enable en CI
```

- `abort` es durable-first: no confies en matar procesos sin el evento `run_aborted`.
- Si un run `aborted` conserva capacity/reservation sin attempts activos, ejecuta
  `scheduler tick` (la accion segura en `status`) para completar la liberacion.
- Runs `blocked` muestran `block_reason_kind`; la accion segura es inspeccionar
  artefactos protegidos, no reintentar automaticamente.
- `waiting_usage_limit` programa `retry_due`; espera `cursor_wait_until` o ejecuta
  `scheduler tick` tras ese instante (solo retry verificado de usage-limit).
- El timer empaquetado es un mecanismo periódico de progreso eventual, no un
  reloj de tiempo real: aunque el asset solicita intervalos de 30 segundos,
  systemd puede agrupar o retrasar activaciones. Funciona cuando la distribución
  WSL está activa; no despierta Windows ni sustituye `scheduler tick` manual en
  tests. Consulta [Timer del scheduler en WSL](timer-systemd-wsl.md) para
  `linger`, verificación y límites de ciclo de vida.
- La aceptacion manual de systemd user units no esta completada hasta validacion
  explicita fuera de CI.

## Codex: artefacto de eventos alcanzo su limite acotado

Sintoma:

- un intento Codex del scheduler termina con `block_reason_kind:
  codex_review_output_truncated`, o los metadatos protegidos del review muestran
  `stdout_truncated: true` / `stderr_truncated: true`;
- el run queda `blocked` aunque el proceso Codex haya salido con exito aparente.

Causa:

- la traza JSONL de eventos supero el limite duro de captura (8 MiB) antes de
  producir un resultado de review valido segun el esquema;
- la truncacion de salida es distinta de un timeout real (`timed_out` solo indica
  vencimiento del plazo congelado).

Accion segura:

1. Inspecciona solo resumenes seguros: `scheduler status`, `scheduler history`,
   rutas de artefactos en metadatos protegidos (`codex/reviews/NN.metadata.json`).
   No copies trazas JSONL completas, prompts ni IDs de sesion en tickets.
2. Si el review no puede validarse tras truncacion, el run queda bloqueado de
   forma no reanudable para ese intento de review; no se crea un segundo reviewer B.
3. Tras instalar la correccion, presenta un `scheduler submit` fresco con un
   reviewer B nuevo si necesitas repetir el ciclo. Un run ya bloqueado antes del
   fix no se repara automaticamente.
4. Un timeout real de Codex sigue clasificandose como timeout, no como truncacion.
