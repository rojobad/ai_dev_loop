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

### Excepcion acotada Phase 20.3: checkpoint de secuencia

Para secuencias multi-fase, el scheduler puede crear **un commit local sin firmar** entre
fases no finales aceptadas por Codex. Esta es la unica autoridad Git de escritura del
producto y solo aplica cuando:

- el run de secuencia esta en `checkpoint_pending` con un intent inmutable verificado;
- el arbol revisado y el patch staged coinciden byte a byte con artefactos protegidos;
- la reserva del worktree permanece activa y se transfiere al sucesor sin liberar el
  repositorio entre fases;
- Git usa `write-tree`, `commit-tree` y `update-ref` compare-and-swap con configuracion
  local por comando (`core.hooksPath` vacio, `commit.gpgsign=false`) sin mutar la
  configuracion del usuario ni del repositorio.

La fase final de una secuencia **no** crea commit: la secuencia pasa a
`awaiting_finalization`, libera la reserva y deja los cambios staged para revision
manual del operador. Los runs standalone siguen sin commits.

Los artefactos `sequence-checkpoints/intent.json` (inmutable),
`sequence-checkpoints/evidence.json` (evidencia autenticada) y
`sequence-checkpoints/result.json` viven solo bajo artefactos protegidos del scheduler.
No se copian prompts, reviews completos ni session IDs completos en salida CLI por defecto.
`scheduler cursor-retry --check` y `--force` siguen esa regla: el recibo y el
resultado muestran estado, razones y el id del run, no el prompt ni el chat completo.
El registro v1 del sucesor inicial y el registro v2 de una corrección, con su
presupuesto heredado, viven solo en artefactos protegidos. El intent de reemplazo
Cursor de una secuencia guarda la adopción y el digest del registro, no el prompt,
el parche ni el identificador completo del reviewer. El estado, el reporte y la
API de secuencia muestran `cursor_retry`, la generación y el sucesor aceptado
sin ese contenido.

`scheduler abort` durante `checkpoint_pending` impide nuevas mutaciones Git y deja la
evidencia durable para reconciliacion manual si un ref ya avanzo.

El CLI `pr-review` y el motor SQLite v2 fueron retirados en Phase 17.7. El workflow
local soportado no hace commit, push ni escritura GitHub desde el scheduler salvo el
checkpoint de secuencia descrito arriba. Las
credenciales GitHub, si se usan fuera de este producto, viven solo en la sesion `gh`
autenticada; el push Git usa SSH + `ssh-agent`.

El scheduler central separa congelado (`scheduler submit`) de ejecucion (`scheduler start`
+ `scheduler tick`). `scheduler abort` persiste primero y solo senala procesos locales
con ownership OS exacta. Status/history del scheduler no exponen prompts, patches,
tokens, session IDs completos, argv, PID/PGID ni environments. El namespace
`integration` (API local JSON) mantiene esa privacidad en resúmenes (`runs list`,
`run inspect`, `run history`, `attempts`, `timeline`). Los comandos explícitos
`integration run plan` e `integration run initial-prompt` devuelven el artefacto
congelado capturado en el submit (contenido sensible); no leen el worktree actual
ni reconstruyen historial ausente. `integration codex-capacity` lanza una sonda
Codex acotada bajo demanda (sin persistir telemetría ni cuota en el ledger); no
sustituye la política de espera del scheduler. **Gate A** valida con
fakes/process boundaries; **Gate B** es
aceptacion live pendiente hasta completar el ciclo controlado en parish360-poc.

El servidor `dashboard serve` es una superficie local sin login: enlaza únicamente
IPv4 `127.0.0.1` y exige Host/Origin same-origin exactos. No debe publicarse mediante
un proxy ni exponerse a LAN. Solo sirve un manifiesto de assets empaquetados y un
dispatcher de lecturas tipadas; no admite rutas de filesystem, SQL, comandos ni
acciones del scheduler. Los contenidos sensibles (`initial-prompt`, review y
stdout/stderr) conservan autenticación por run/attempt, paginación y carga
deliberada. No se registran cuerpos, parámetros, query strings, paths privados ni
stderr upstream, y las respuestas indican `no-store`.

El frontend instalado usa únicamente assets same-origin empaquetados: no carga
CDN, no registra service worker y no persiste consultas ni contenido. Solo el
tema visual puede guardarse en `localStorage`. Cambiar scope o intento cancela y
retira el contenido sensible en memoria; la evidencia ausente no se reconstruye.
Consulta [Dashboard local](dashboard-local.md) para el procedimiento seguro.

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
- `--codex-review-model` y `--codex-review-reasoning-effort`;
- `--controller-session-id` opcional cuando el operador registra proveniencia A;
- hashes SHA-256 de inputs inmutables.

La admision one-shot de branch, HEAD y estado del worktree (`require_clean_worktree`,
entre otros) ocurre en el primer tick, no en submit. El scheduler no ejecuta
comprobaciones continuas de baseline durante los turnos Cursor/Codex posteriores;
solo revalida los inputs congelados y los checkpoints durablemente registrados.

## Identidad de agentes

- Un run cuyo implementer es Cursor crea o reutiliza exactamente un Cursor chat ID.
- Un implementer Codex, cuando se selecciona, es una identidad distinta del reviewer B.
  Su session ID queda vacío hasta una captura autenticada y no se adivina. El
  scheduler la persiste en un artefacto protegido, reanuda exactamente esa sesión
  para correcciones y conserva `cursor_fix_prompt` byte por byte en el sobre de
  ejecución. Nunca comparte la sesión del reviewer.
- Todo review resume la identidad reviewer Codex exacta congelada para ese run.
- Un wait de capacidad persiste rol y evidencia. El probe no puede convertir un
  wait implementer en reviewer, y cuota, timeout o identidad incierta no autorizan
  staging ni review.
- En runs controller A frescos, A elige `--codex-review-model` y
  `--codex-review-reasoning-effort` en `scheduler submit`; no hay fallback desde
  YAML, sesion preexistente ni default de CLI en review time.
  El scheduler crea exactamente un B con `codex exec` en `--sandbox workspace-write` en
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

## Secuencias v2 y espera Codex

La autoridad Git y los límites del implementer se aplican a Cursor y Codex. Cada
fase tiene sus propias identidades; el reviewer permanece independiente y staged-only.
Una espera implementer no requiere identidad reviewer ni patch ya revisado.
El restart continúa la hoja reservada y no exige descartar cambios parciales ni
repite admisión limpia. Un abort durable cancela continuación y fases futuras;
la reserva se retiene mientras una terminación exacta siga incierta. El probe
model-agnostic es conservador y solo `AVAILABLE` autoriza una continuación.
La inspección explícita de output puede revelar prompts/IDs en bytes capturados;
los resúmenes y errores públicos mantienen la privacidad existente.

`scheduler implementer-retry` tampoco toma comandos ni prompts de la salida del
agente. Para Codex autentica el envelope terminal, la evidencia de invocación, la
sesión capturada y el intent inmutable antes de adelantar o crear un efecto. La
configuración y los bytes del prompt provienen únicamente del submit/checkpoint
congelado. La evidencia incierta o corrupta retiene la reserva; abort explícito solo
la libera después de resolver cualquier terminación pendiente.

Con `--force`, la misma autenticación se amplía únicamente al fallo genérico
elegible. El record protegido fija el intento fuente, la sesión implementer, los
settings, config, prompt exacto y estado parcial autenticado; en corrección fija también
reviewer B, fix, patch revisado y presupuesto. El origen y sus artefactos no se
reescriben. El sucesor toma o readquiere la reserva antes de publicar, no ejecuta
mientras la relación esté `pending`, y el abort de ese sucesor impide cerrar la
publicación. La comprobación puntual del estado parcial no es una admisión de baseline
limpio. Un status autenticado de cero bytes se publica explícitamente con su hash y
permisos protegidos; no habilita artefactos vacíos generales. La autoridad del record
solo cubre el turno recuperado y sus continuaciones timeout/capacidad: una corrección
posterior usa su propio checkpoint y prompt durable, conservando la misma identidad y
la ascendencia autenticada. No se aceptan sesiones sin ligar ni evidencia de
quota/timeout reutilizada. En secuencia, otro intent protegido liga la hoja vigente,
ordinal, generación, entrada congelada, reserva y digest del record. Solo la adopción
atómica hace dispatchable al sucesor; abort cancela una publicación pendiente y una
hoja obsoleta sin relación no puede revivirse.

El record también fija el consumo autenticado de reintentos automáticos del turno. Un
sucesor no recibe un presupuesto nuevo por cambiar de run: reconstruye el contador
desde eventos protegidos y su ascendencia same-turn, sin reutilizar el intento ni el
efecto anterior. Solo una corrección posterior con nueva iteración reinicia ese consumo.
