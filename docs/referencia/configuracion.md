# Referencia de configuracion

Archivo:

```text
ai_dev_loop.yaml
```

Version soportada:

```yaml
version: 1
```

El controller remoto / sesion reviewer aislada (A/B) no agrega campos a `ai_dev_loop.yaml`. El flujo A/B usa flags de CLI (`--controller-session-id`, `launch`, `controller status`) y skills globales instalados.

La seccion opcional `github` habilita el ciclo autonomo post-PR. Ausente o `enabled: false` deja el workflow local sin cambios. No se permiten tokens ni credenciales en YAML; la autenticacion GitHub usa una sesion `gh` ya autenticada.

## Schema

Herencia recomendada (sin overrides de modelo ni reasoning):

```yaml
version: 1

project:
  name: my-project

cursor:
  command: agent
  model: composer-2.5-fast
  output_format: stream-json
  force: true
  trust_workspace: true
  sandbox: disabled

codex:
  command: codex
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write

workflow:
  max_review_iterations: 3
  require_clean_worktree: true
  stage_mode: all
  cursor_timeout_minutes: 90
  codex_timeout_minutes: 90

prompt:
  directory: docs/plans
  filename_template: prompt_{plan_stem}.txt
```

Overrides explicitos e independientes:

```yaml
codex:
  command: codex
  review_model: gpt-5.6-terra
  review_reasoning_effort: high
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write
```

## `project`

| Campo | Requerido | Descripcion |
| --- | --- | --- |
| `name` | Si | Slug del proyecto. Debe ser minuscula con guiones opcionales. |

Ejemplos validos:

```text
parish360-platform
my-project
```

## `cursor`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `command` | `agent` | Ejecutable Cursor CLI. |
| `model` | `composer-2.5-fast` | Modelo Cursor exacto. |
| `output_format` | `stream-json` | Formato de salida. |
| `force` | `true` | Pasa `--force` cuando aplica. |
| `trust_workspace` | `true` | Pasa `--trust` cuando aplica. |
| `sandbox` | `disabled` | Sandbox Cursor. |

Valores soportados:

```text
output_format: stream-json, json, text
sandbox: enabled, disabled
```

La forma de ejecucion esperada para Cursor es:

```text
agent -p --force --trust --workspace <repo> --resume <chat-id> --model <model> --output-format stream-json --sandbox disabled <prompt>
```

El prompt se pasa como argumento posicional, no por stdin.

`cursor.model` en YAML fija el modelo del run preparado. No configura el fallback de recovery por limite de uso: ese valor se congela solo en el sucesor via `recover --cursor-model <modelo>`. `auto` es una solicitud de enrutamiento de Cursor, no un modelo de proveedor fijado en YAML.

No uses IDs de modelo de Cursor Agent (por ejemplo `gpt-5.6-terra-high`) como `codex.review_model`. En Codex, modelo y reasoning effort son campos separados.

## `codex`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `command` | `codex` | Ejecutable Codex CLI en WSL. |
| `review_model` | `null` (captura) | Override opcional. En un run nuevo, omitido o `null` usa el modelo capturado de la sesion durante `prepare`. |
| `review_reasoning_effort` | `null` (captura) | Override opcional. En un run nuevo, omitido o `null` usa el reasoning capturado de la sesion durante `prepare`. |
| `review_skill` | `review-staged-cursor-execution` | Skill que Codex debe invocar para revisar staged changes. |
| `sandbox` | `workspace-write` | Sandbox para `codex exec`. |

Valores soportados de sandbox:

```text
read-only
workspace-write
danger-full-access
```

Valores soportados de `review_reasoning_effort`:

```text
minimal
low
medium
high
xhigh
max
ultra
```

Los overrides son independientes: puedes fijar solo el modelo, solo el reasoning, ambos, o ninguno. La captura de sesion completa el campo omitido; no se consulta WSL `config.toml` ni el default de Codex CLI.

`prepare` congela valores de sesion, valores efectivos y procedencia (`session` o `explicit`). Cambiar `ai_dev_loop.yaml` o el rollout despues no altera un run ya preparado; prepara un run nuevo.

Forma de review para runs nuevos (argv separados, sin shell):

```text
codex exec --cd <repo> --sandbox <sandbox> resume --model <model> -c model_reasoning_effort="high" --json --output-schema <schema> --output-last-message <result.json> <session-id> -
```

`--cd` y `--sandbox` van antes de `resume`; `--model` y `-c` van despues. Runs historicos de Fase 9 con ambos valores `null` y sin procedencia omiten ambos por compatibilidad y advierten que se vuelva a preparar. Esos `null` no significan captura de sesion.

## `workflow`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `max_review_iterations` | `3` | Numero maximo de reviews Codex, incluyendo el primero. |
| `require_clean_worktree` | `true` | Rechaza trabajo no relacionado antes de preparar. |
| `stage_mode` | `all` | Modo de staging. Solo `all` esta soportado. |
| `cursor_timeout_minutes` | `90` | Timeout por turno Cursor. |
| `codex_timeout_minutes` | `90` | Timeout por review Codex. |

Con `max_review_iterations: 3`:

```text
Cursor inicial
Review 1
Cursor fix 1
Review 2
Cursor fix 2
Review 3
Stop
```

Si Review 3 aun tiene findings, el estado final es `max_iterations_reached`.

## `prompt`

| Campo | Default | Descripcion |
| --- | --- | --- |
| `directory` | `docs/plans` | Directorio esperado para plan y prompt. |
| `filename_template` | `prompt_{plan_stem}.txt` | Template de prompt. Debe incluir `{plan_stem}`. |

## `github` (opcional)

Deshabilitado por defecto. Ejemplo opt-in:

```yaml
github:
  enabled: true
  command: gh
  reviewer_logins:
    - chatgpt-codex-connector
  review_trigger_body: "@codex review"
  poll_interval_seconds: 60
  poll_timeout_hours: 24
  max_external_cycles: 8
  user_mention: rojobad
  continue_command: "@rojobad /ai-dev-loop continue"
  external_review_skill: review-github-pr-feedback
  max_local_review_iterations: 3
  pr_base: master
  acknowledgement:
    enabled: true
    reaction: eyes
    timeout_seconds: 300
    on_timeout: diagnostic_only
  no_findings_completion:
    enabled: true
    accepted_comment_prefixes:
      - "Codex Review: Didn't find any major issues."
    reviewed_commit_prefix_length: 12
```

| Campo | Default | Descripcion |
| --- | --- | --- |
| `enabled` | `false` | Opt-in del ciclo post-PR. |
| `command` | `gh` | Ejecutable GitHub CLI. |
| `reviewer_logins` | `[chatgpt-codex-connector]` | Logins de bot elegibles. |
| `review_trigger_body` | `@codex review` | Cuerpo del comentario disparador. |
| `poll_interval_seconds` | `60` | Intervalo de polling local. |
| `poll_timeout_hours` | `24` | Timeout maximo de espera. |
| `max_external_cycles` | `8` | Maximo de ciclos externos. |
| `continue_command` | `@rojobad /ai-dev-loop continue` | Unica autorizacion para reanudar tras atencion del usuario. |
| `external_review_skill` | `review-github-pr-feedback` | Skill Codex para adjudicacion externa. |
| `max_local_review_iterations` | `3` | Presupuesto local de review tras feedback externo. |
| `pr_base` | `master` | Base fija del PR. |
| `acknowledgement.enabled` | `false` | Telemetria best-effort de la reaccion del bot sobre el trigger. |
| `acknowledgement.reaction` | `eyes` | Emoji de acuse esperado (solo diagnostico). |
| `acknowledgement.timeout_seconds` | `300` | Plazo diagnostico sin acuse; no reintenta el trigger. |
| `acknowledgement.on_timeout` | `diagnostic_only` | Unico valor admitido; nunca completa ni aborta. |
| `no_findings_completion.enabled` | `false` | Finalizacion verificable sin hallazgos via comentario general. |
| `no_findings_completion.accepted_comment_prefixes` | `[]` | Prefijos exactos del comentario de “sin hallazgos”; obligatorio si esta habilitado. |
| `no_findings_completion.reviewed_commit_prefix_length` | `12` | Longitud del SHA en la linea estructural `Reviewed commit:`. |

Campos de credenciales (`token`, `pat`, `access_token`, etc.) estan prohibidos.

La finalizacion sin hallazgos exige a la vez: autor en `reviewer_logins`, comentario
posterior a `request_created_at`, prefijo configurado, y linea `Reviewed commit:`
ligada al `bound_head_sha`. La ausencia de hilos, la presencia de `eyes` o la
retirada de esa reaccion **nunca** completan el ciclo. Mantén apagada la opción
remota de **Automatic reviews** de Codex mientras ai_dev_loop publica
`@codex review`; habilitar ambas vías puede duplicar revisiones.

Con `github.enabled: true` hay dos flujos CLI:

- `pr-review create <source-run-id>` para un run local completado (origen
  `source_run`);
- `pr-review prepare` + `pr-review start` para adoptar un PR ya abierto (origen
  `independent_pr`);
- `pr-review recover <failed-run-id>` para checkpoints artefacto-dirigidos
  (`external_adjudication`, `reviewing`, `external_feedback_cursor`,
  `publication_pre_commit`): sucesor inmutable; no republica `@codex review`.
  En `publication_pre_commit` el `resume` publica solamente tras cargar la
  identidad SSH manualmente; un hash GPR obsoleto solo se adopta en el
  sucesor si el patch live coincide con el artefacto via `normalize_patch_text`.

Prepare no escribe en GitHub; start publica el marcador de review.
`set-cursor-model` solo aplica al ciclo independiente antes de crear el chat
Cursor.

## `pr_review_v2` (opcional, temporal / pre-cutover)

Seccion aislada para el namespace CLI `pr-review-v2` (Phase 16.7). Ausente o
`enabled: false` no afecta `pr-review` / `github`. Schema version permanece `1`.
Campos de credenciales estan prohibidos (igual que `github`).

```yaml
pr_review_v2:
  enabled: false
  gh_command: gh
  git_command: git
  ssh_command: ssh
  remote_name: origin
  base_branch: master
  reviewer_logins:
    - chatgpt-codex-connector
  review_trigger_body: "@codex review"
  user_mention: rojobad
  external_review_skill: review-github-pr-feedback
  poll_interval_seconds: 60
  max_external_cycles: 8
  max_local_iterations: 3
  per_call_timeout_seconds: 60
  overall_timeout_seconds: 180
  max_pages: 20
  max_items: 500
  max_server_directed_wait_seconds: 3600
  no_findings:
    enabled: false
    accepted_comment_prefixes: []
    accept_bot_thumbs_up: false
    reviewed_commit_prefix_length: 12
  worker:
    lease_ttl_seconds: 30
    heartbeat_interval_seconds: 10
    idle_poll_seconds: 1
```

Validacion cruzada: `heartbeat_interval_seconds < lease_ttl_seconds`; comandos
argv-safe; si `no_findings.enabled: true` hace falta al menos una regla de
evidencia (`accepted_comment_prefixes` no vacio o `accept_bot_thumbs_up: true`);
`extra=forbid`. Los valores efectivos de un run preparado se congelan en el
execution context protegido.

### `no_findings` (Phase 16.8)

- Omision o `enabled: false` conserva el comportamiento Phase 16.7.
- `accept_bot_thumbs_up: true` reconoce solo reacciones GitHub `+1` del
  `reviewer_logins` allowlist sobre el comentario trigger exacto del ciclo
  (marker/opaco), con timestamp estrictamente posterior al trigger y sin hilos
  elegibles. Cero coincidencias sigue siendo polling normal; varias coincidencias,
  evidencia contradictoria o proveniencia incompleta fallan cerrado.
- `eyes` u otras reacciones no completan el run.
- La observacion verificada persiste evidencia tipada de reaccion (no solo un ID)
  en el artefacto protegido hash-verificado; los resumenes SQLite/status/history
  siguen acotados y redactados.

## Validacion

Inspecciona la configuracion efectiva antes de `prepare`:

```bash
ai_dev_loop config validate --repo /path/al/repo
```

Salida JSON:

```bash
ai_dev_loop config validate --repo /path/al/repo --output json
```

La salida muestra si `review_model` y `review_reasoning_effort` son explicitos o `inherited from session`.
