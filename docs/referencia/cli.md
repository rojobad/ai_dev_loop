# Referencia CLI

Comando raiz:

```bash
ai_dev_loop [OPTIONS] COMMAND [ARGS]...
```

Opciones globales:

```text
--version
--help
```

## `prepare`

```bash
ai_dev_loop prepare [OPTIONS]
```

Lee el prompt exacto desde stdin y crea un run preparado.

Opciones principales:

```text
--config-path PATH
--project-name TEXT
--repo-path PATH
--plan-path PATH
--prompt-source-path PATH
--codex-session-id TEXT
--controller-session-id TEXT
--cursor-command TEXT
--cursor-model TEXT
--cursor-output-format TEXT
--codex-command TEXT
--codex-review-model TEXT
--codex-review-reasoning-effort TEXT
--review-skill TEXT
--max-review-iterations INTEGER
--cursor-timeout-minutes INTEGER
--codex-timeout-minutes INTEGER
--output [text|json]
```

`--codex-review-model` y `--codex-review-reasoning-effort` son overrides opcionales e independientes. Si no se pasan y YAML omite/usa `null`, `prepare` captura el campo correspondiente de la sesion exacta. No hay flag para limpiar un override del YAML; configura herencia antes de `prepare`.

`--controller-session-id` es opcional. En el flujo A/B (skill handoff) es el session ID exacto del controller A y debe diferir de `--codex-session-id` (reviewer B). Con controller: `requires_codex_exit` es `false`, `reviewer_must_remain_inactive` es `true` y `launch_command` queda disponible. Sin controller: comportamiento legacy con `requires_codex_exit: true` y `start`.

Ejemplo A/B:

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<exact-reviewer-session-id>" \
  --controller-session-id "<exact-controller-session-id>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

Ejemplo legacy:

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<exact-codex-session-id>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

## `launch`

```bash
ai_dev_loop launch <run-id> --controller-session-id TEXT [--repo-path PATH] \
  [--update-tools|--skip-tool-update] [--allow-incompatible-tools] [--output text|json]
```

Lanza un run A/B preparado en un worker local detachado. Tambien reanuda el checkpoint `waiting_for_cursor_fix` despues de un `extend`. Requiere el controller session ID exacto del prepare. Es idempotente si el worker ya esta vivo. No sustituye `start` para runs legacy sin controller.

Los flags de compatibilidad de herramientas son los mismos que en `start`/`resume`, pero el worker es siempre non-interactive: nunca pregunta. `--update-tools` autoriza updaters; `--skip-tool-update` (o la omision) no actualiza; `--allow-incompatible-tools` permite continuar ante incompatibilidad confirmada. `--update-tools` y `--skip-tool-update` son mutuamente excluyentes.

## `controller status`

```bash
ai_dev_loop controller status \
  --controller-session-id TEXT \
  --repo-path PATH \
  [--run-id TEXT] \
  [--include-terminal] \
  [--output text|json]
```

Lookup read-only por controller session ID y repositorio. Ante ambiguedad (0 o N matches) no elige por timestamp; usa `--run-id` para desambiguar. Incluye liveness del worker en la respuesta.

## `github doctor`

```bash
ai_dev_loop github doctor [--repo-path PATH] [--output text|json]
```

Verifica disponibilidad de `gh`, autenticacion (cuenta redactada), schemas GitHub y, con `--repo-path`, si `github.enabled` y el remote SSH estan listos. No imprime tokens.

## `pr-review`

Ciclo opt-in post-PR (requiere `github.enabled: true`):

```bash
ai_dev_loop pr-review prepare \
  --repo-path PATH --pr N --branch BRANCH \
  --plan-path PLAN --prompt-source-path PROMPT \
  --codex-session-id <reviewer> \
  [--controller-session-id <controller-A>] \
  [--cursor-model MODEL] [otros overrides seguros] \
  [--output text|json]
ai_dev_loop pr-review set-cursor-model <run-id> --cursor-model MODEL [--output text|json]
ai_dev_loop pr-review start <run-id> [--controller-session-id <controller-A>] [--output text|json]
ai_dev_loop pr-review create <source-run-id> [--output text|json]
ai_dev_loop pr-review status <run-id> [--output text|json]
ai_dev_loop pr-review continue <run-id>
ai_dev_loop pr-review resume <run-id> [--controller-session-id <controller-A>]
ai_dev_loop pr-review recover <failed-run-id> [--dry-run] [--output text|json]
ai_dev_loop pr-review abort <run-id>
```

Hay dos origenes:

- **`create` (source_run):** unica puerta explicita para publicar el patch staged
  aceptado (commit + push no-force + PR a `master`) y pedir `@codex review`.
  Reutiliza el Cursor chat y la sesion Codex exactos del source.
- **`prepare` + `start` (independent_pr):** adopta un PR ya abierto. `prepare` es
  no mutante (sin comentario GitHub, sin chat Cursor, sin turnos Codex). Requiere
  PR/branch/plan/prompt/sesion Codex exactos y `HEAD` local igual al head del PR.
  `start` es la puerta de escritura: vuelve a verificar el binding, publica un
  marcador idempotente `@codex review` y lanza el worker. En A/B solo A puede
  hacer `start` con `--controller-session-id`; en sesion unica el reviewer debe
  quedar inactivo antes de `start`.

`set-cursor-model` solo aplica a ciclos `independent_pr` en
`prepared_independent` o `awaiting_bot_review` sin chat Cursor ni iteraciones; no
cambia el runtime Codex del reviewer ni reescribe `effective-config.yaml`.

`pr-review recover` crea un sucesor inmutable para checkpoints
artefacto-dirigidos:

- `external_adjudication`: fallo de adjudicacion por incompatibilidad del schema
  de salida Codex (`invalid_json_schema` / `uniqueItems`) antes de side effects;
- `reviewing` (`codex_review_result_artifact_missing`): Cursor y staging ya
  completaron la correccion local, pero falta `codex/reviews/NN.json`. El
  sucesor reintenta solo la revision Codex B; no reejecuta Cursor ni toca GitHub;
- `external_feedback_cursor` (`external_feedback_cursor_not_started`): la
  adjudicacion externa ya produjo un prompt accionable, pero la iteracion
  fresca de Cursor nunca arranco (p. ej. staging vacio sobre artefactos
  historicos). El sucesor reutiliza la adjudicacion persistida y, tras
  `pr-review resume`, abre Cursor con el prompt externo exacto antes de
  staging; no re-adjudica ni republica `@codex review`;
- `publication_pre_commit` (`publication_pre_commit_interrupted`): la revision
  local Codex acepto el patch staged y la publicacion quedo en
  `publication_phase: pre_commit` sin commit. El sucesor queda `interrupted`
  para `pr-review resume`, que publica solamente (no abre Cursor/Codex, no
  re-adjudica, no responde/resuelve hilos ni republica `@codex review` antes
  del flujo normal post-publicacion). La elegibilidad usa evidencia durable
  (fase, patch/SHA/PR/hilos, resultado local sin hallazgos, texto de
  publicacion); nunca `last_error` ni logs. Si falta la identidad del
  `ssh-agent`, carga la clave manualmente antes del `resume`.

El origen `failed` permanece terminal; el sucesor reutiliza el mismo PR, SHA,
trigger, hilos elegibles, sesion Codex B, chat Cursor y controlador A. **No**
republica `@codex review`. Usa `--dry-run` primero. El schema
`github-pr-review-result-v1.json` ya no envia `uniqueItems` a Codex; la
unicidad sigue validada en Pydantic.

En ciclos A/B, `pr-review resume` exige `--controller-session-id` del
controlador A original. Además de checkpoints `interrupted` (publicación,
polling o recovery), acepta un run no terminal en `awaiting_bot_review` cuyo
worker esté ausente/stale: reengancha un solo poller. Puede validar PR/head en
solo lectura, pero no escribe en GitHub, no republica el trigger, no crea Cursor
ni invoca Codex. Con worker vivo (identidad verificada) es no-op idempotente.
`pr-review status` reporta `worker_liveness` (`live`/`stale`/`absent`) y la
siguiente acción segura sin exponer PID, token ni argv.

Ante hallazgos no aplicables/inciertos responde inline con `@rojobad`, deja
threads unresolved y espera `@rojobad /ai-dev-loop continue`. No hace merge ni
force push. El ciclo independiente crea exactamente un Cursor chat nuevo solo
cuando todos los hallazgos elegibles son accionables. Con
`no_findings_completion` habilitado, un comentario general verificable del bot
(prefijo + `Reviewed commit:` ligado al SHA) puede completar el ciclo sin
Cursor; `status` puede mostrar acuse `eyes` o diagnostico de timeout, pero eso
nunca finaliza ni republica el trigger.

`pr-review continue` también admite una recuperación histórica **lineage-bound**
(Phase 15.10 / 15.11): si el run está en `waiting_for_user_attention` con
`eligible_thread_set_drift` y la lineage prueba que el freeze
`expected_eligible_thread_ids` pertenece a un ciclo anterior ya
procesado/resuelto (mientras el ciclo actual ya publicó un marker nuevo), un
comentario exacto nuevo de continue autoriza limpiar solo ese freeze obsoleto y
reanudar el mismo worker/run. La evidencia puede ser la recovery directa
`external_adjudication` del run actual, o un único ancestro terminal
verificado cuando la recovery actual es `reviewing` /
`codex_review_result_artifact_missing` (sin recorridos recursivos). Conserva
PR, SHA, marker, sesiones A/B y chat Cursor; **no** republica `@codex review`,
no crea sucesor y no relaja el drift legítimo del ciclo actual. Si un continue
previo ya consumió el comentario, hace falta uno nuevo. No edites `state.json`
a mano.

## `start`

```bash
ai_dev_loop start <run-id> [--update-tools|--skip-tool-update] [--allow-incompatible-tools]
```

Ejecuta el loop automatizado completo para un run preparado.

- `--update-tools`: autoriza ejecutar el updater oficial de cada CLI WSL incompatible, sin prompt.
- `--skip-tool-update`: nunca ejecuta updaters.
- `--allow-incompatible-tools`: permite continuar pese a incompatibilidad confirmada.

`--update-tools` y `--skip-tool-update` son mutuamente excluyentes. Sin flags, un TTY puede preguntar por cada herramienta incompatible y usa `no` por defecto. Non-TTY nunca pregunta y falla ante incompatibilidad salvo autorizacion explicita para actualizar o continuar.

## `resume`

```bash
ai_dev_loop resume <run-id> [--update-tools|--skip-tool-update] [--allow-incompatible-tools]
```

Continua un run checkpointed o interrumpido si el siguiente paso seguro puede derivarse de estado y artefactos.

Aplica la misma politica de compatibilidad y updates que `start`. Tras un update se vuelven a consultar version y catalogos (`agent models`, `codex debug models`). Un abort pendiente tiene prioridad.

## `extend`

```bash
ai_dev_loop extend <run-id> --additional-review-iterations INTEGER [--output text|json]
```

Solo aplica a un run detenido en `max_iterations_reached`. Requiere un entero positivo, aumenta ese presupuesto sin crear un run nuevo y restaura el checkpoint `waiting_for_cursor_fix` con el fix prompt exacto de la ultima review. Conserva el chat de Cursor, la sesion revisora Codex, los cambios staged y todos los artefactos.

Despues, en un run legacy usa `ai_dev_loop resume <run-id>`. En un run A/B deja B inactiva y usa `ai_dev_loop launch <run-id> --controller-session-id <exact-controller-session-id>`: el worker detecta el checkpoint y ejecuta `resume` de forma detachada.

## `recover`

```bash
ai_dev_loop recover <run-id> [--dry-run] [--adopt-current-cursor-output] [--cursor-model TEXT] [--output text|json]
```

Analiza un run `failed` y, si es elegible, crea un run sucesor `interrupted` sin mutar el origen ni el repositorio.

Checkpoints: `reviewing`, `process_review`, `staging` (Cursor completo / staging incompleto; `initial_staging_failed` o `correction_staging_failed`), y `cursor` (limite de uso de Cursor con turno incompleto).

- `--dry-run`: solo reporta elegibilidad, checkpoint, blockers y migracion de runtime.
- `--adopt-current-cursor-output`: atestacion explicita para fallos de staging historicos de **correccion** sin fingerprint post-Cursor, o para fallos historicos de limite de uso de Cursor sin fingerprint contemporaneo, cuando el status actual coincide con `NN-after-cursor.txt`. No aplica a `initial_staging_failed`. Para checkpoint `cursor` tambien requiere `--cursor-model`.
- `--cursor-model`: obligatorio para checkpoint `cursor`. Congela el modelo fallback solicitado en el sucesor (por ejemplo `auto`). Invalido para checkpoints `staging`/`reviewing`/`process_review`. No es un default implicito ni se lee desde YAML.
- Sin `--dry-run`: crea o reutiliza el sucesor y imprime `resume_command`.
- No lanza agentes ni updaters; pasa `--update-tools` a `resume` si hace falta.
- JSON incluye `recovery_run_id`, `checkpoint`, `runtime_migration`, `reused_existing_successor` y campos de fingerprint/adopcion cuando aplican.

TTY vs non-TTY:

- tras un fallo `cursor_usage_limit` en `start`/`resume`, un TTY puede ofrecer `recover --cursor-model auto` y continuar; la respuesta por defecto es no;
- non-TTY imprime el comando explicito y no cambia de modelo ni crea sucesor automaticamente.

Ejemplo de limite de uso:

```bash
ai_dev_loop recover --dry-run <failed-run-id> --cursor-model auto
ai_dev_loop recover <failed-run-id> --cursor-model auto
ai_dev_loop recover --dry-run <failed-run-id> --adopt-current-cursor-output --cursor-model auto
ai_dev_loop recover <failed-run-id> --adopt-current-cursor-output --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

## `abort`

```bash
ai_dev_loop abort <run-id>
```

Solicita cancelacion no destructiva de un run no terminal.

## `status`

```bash
ai_dev_loop status <run-id> [--output text|json]
```

Muestra estado resumido y siguiente accion segura.

## `list`

```bash
ai_dev_loop list [--project PROJECT] [--status STATUS] [--output text|json]
```

Lista runs encontrados bajo XDG state.

## `logs`

```bash
ai_dev_loop logs <run-id> [--component COMPONENT]
```

Componentes soportados:

```text
ai_dev_loop
cursor
codex
```

## `inspect`

```bash
ai_dev_loop inspect <run-id> [--output text|json] [--show-prompts]
```

Lista artefactos y resumen de iteraciones. `--show-prompts` imprime contenido sensible.

## `doctor`

```bash
ai_dev_loop doctor [--repo PATH] [--output text|json]
```

Verifica entorno local y, opcionalmente, configuracion de un repositorio objetivo. Es read-only.

## `config validate`

```bash
ai_dev_loop config validate [--repo PATH] [--config-path PATH] [--output text|json]
```

Valida `ai_dev_loop.yaml`.

## `integrations install`

```bash
ai_dev_loop integrations install [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
--install-session-bridge
```

## `integrations uninstall`

```bash
ai_dev_loop integrations uninstall [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
```

## `integrations status`

```bash
ai_dev_loop integrations status [OPTIONS]
```

Opciones:

```text
--output [text|json]
--target [wsl-cli|codex-desktop-wsl]
--windows-codex-home PATH
--wsl-distro TEXT
--wsl-hook-python TEXT
--wsl-hook-script-path PATH
```

## `integrations sessions`

```bash
ai_dev_loop integrations sessions install [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH]
ai_dev_loop integrations sessions status  [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH]
ai_dev_loop integrations sessions list    [--output text|json] [--windows-codex-home PATH] [--wsl-codex-home PATH] [--desktop-sessions-dir PATH] [--limit INTEGER]
ai_dev_loop integrations sessions remove  [--output text|json] [--wsl-codex-home PATH]
```

Gestiona el symlink seguro `sessions/from-desktop`.
