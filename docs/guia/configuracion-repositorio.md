# Configuración del repositorio objetivo

Cada repositorio controlado por `ai_dev_loop` necesita un `ai_dev_loop.yaml` en
su raíz. Para Cursor nuevo usa schema v2 y declara toda la selección SDK:

```yaml
version: 2

project:
  name: my-project

cursor:
  runtime: sdk-local
  sdk_version: 1.0.37
  model: grok-4.7
  parameters:
    context: 256k
    fast: false
    reasoning_effort: high
  credential_ref: cursor-default
  setting_sources: [project]
  sandbox: disabled

codex:
  command: codex
  review_skill: review-staged-ai-dev-loop-execution
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

`credential_ref` es un nombre, no el secreto. El operador provisiona
`$XDG_CONFIG_HOME/ai_dev_loop/credentials/cursor-default` fuera del repositorio:
directorios owned `0700`, archivo regular owned `0600`, una sola línea UTF-8 y
ningún symlink. El producto no crea ni rota la credencial.

## Implementer y reviewer independientes

Omitir `implementer` mantiene Cursor como default. Para un run Codex-only, el
submit puede seleccionar Codex explícitamente:

```bash
ai_dev_loop scheduler submit \
  --short-name "Implementación Codex" \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --implementer-provider codex \
  --implementer-model gpt-5.6-sol \
  --implementer-reasoning-effort xhigh \
  --codex-review-model gpt-6.1-sol \
  --codex-review-reasoning-effort high \
  --output json < docs/plans/prompt_mi-plan.txt
```

Modelo, razonamiento, timeout e identidad del implementer Codex quedan
congelados aparte de reviewer B. El reviewer se crea una sola vez en el primer
review y nunca usa `--last`. Una selección Codex funciona también con un config
v1 histórico sin resolver el campo Cursor inerte; una selección efectiva Cursor
v1 ya no se admite para trabajo nuevo.

## Reglas de validación

- config v2 exige `runtime: sdk-local`, `sdk_version: 1.0.37`, modelo,
  `parameters`, `credential_ref`, `setting_sources` y sandbox explícitos;
- `project.name` es un slug en minúsculas con guiones opcionales;
- `parameters.fast` es boolean, no string; context/effort son strings no vacíos;
- `setting_sources` acepta `project`, `user`, `team`, `mdm`, `plugins` y `all`,
  sin duplicados;
- `workflow.stage_mode` es `all`; timeouts y techo de review son positivos;
- `prompt.filename_template` incluye `{plan_stem}`;
- campos extra o mezclas v1/v2 se rechazan.

Valida sin lanzar agentes ni leer la credencial:

```bash
ai_dev_loop config validate --repo /path/al/repo
```

Para un fixture anidado puedes pasar `--config-path`. El submit Cursor tampoco
abre el bridge: package, credencial, autenticación y catálogo se comprueban en el
intento owned del primer tick.

## Precedencia y congelación

La configuración efectiva combina defaults del schema, configuración global
XDG, archivo del repositorio y overrides CLI todavía soportados. Submit congela
source/effective config, plan, prompt, identidad del repositorio y settings de
ambos roles. Los flags `--codex-review-model` y
`--codex-review-reasoning-effort` son obligatorios; no se heredan de YAML,
controller A, `config.toml` ni defaults del CLI.

Los flags nuevos de comando/modelo/formato Cursor CLI fueron retirados. Una
secuencia nueva usa manifest v3 si todas sus hojas son Codex y v4 si contiene
Cursor SDK. Una definición histórica con Cursor CLI se conserva para consultas y
replay exacto, pero no puede start, materializar, hacer handoff o recovery.

## Planes, prompts y control-plane

La convención esperada es:

```text
docs/plans/<plan>.md
docs/plans/prompt_<plan>.txt
```

Plan y prompt son inputs inmutables. Cambiarlos exige un submit/prepare fresco.
Editar el YAML fuente, instalar el paquete, provisionar la credencial y cambiar
el timer son decisiones distintas; ninguna se infiere de la aceptación del
código. Para migrar una instalación sigue el [cutover Cursor
SDK](../operacion/cutover-cursor-sdk.md).
