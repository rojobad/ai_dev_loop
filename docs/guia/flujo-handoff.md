# Flujo de handoff

El handoff conecta una sesion interactiva de Codex con el loop automatizado. En el flujo A/B recomendado, la sesion de planificacion (A) controla el run y una sesion fork aislada (B) es la unica que Codex reanuda para reviews.

## Flujo A/B recomendado

1. Instala la integracion correcta: `wsl-cli` o `codex-desktop-wsl` (instala ambos skills y el hook `SessionStart`).
2. Abre Codex en el repositorio objetivo (sesion controlador A).
3. Discute y aprueba el cambio.
4. Genera un plan Markdown y un prompt separado para Cursor.
5. Usa el skill global `ai-dev-loop-handoff`.
6. Tras la aprobacion explicita, A crea un fork same-directory a la sesion reviewer B (capacidad de la app Codex; no lo inventa el CLI).
7. B ejecuta `ai_dev_loop prepare` con `--codex-session-id` = B y `--controller-session-id` = A, pasando el prompt exacto por stdin.
8. B devuelve a A el `run_id` y el `launch_command` por el canal autorizado de la app, y deja B inactiva.
9. Desde A, usa el skill `ai-dev-loop-controller` (o `ai_dev_loop launch`) para lanzar el worker local detachado.
10. Desde A, consulta estado bajo demanda (`controller status`), aborta si hace falta e inspecciona cambios staged y artefactos.

No hay notificaciones automaticas al movil ni a ChatGPT: el estado se pide desde A cuando hace falta.

## Roles A y B

| Sesion | Rol | Acciones |
| --- | --- | --- |
| A (controller) | Planificacion y control | `launch`, `controller status`, `abort`, inspeccion read-only |
| B (reviewer) | Solo `prepare`, luego quietud | No ejecuta `start`, `launch`, `resume`, `abort` ni control |

Todo review automatizado reanuda exactamente `state.codex.session_id` (B). A nunca se pasa a `codex exec resume`.

## Skills globales

```text
ai-dev-loop-handoff
ai-dev-loop-controller
```

`ai-dev-loop-handoff` guia el ciclo A/B: fork, prepare en B, retorno de identidad a A, B inactiva.

`ai-dev-loop-controller` es solo para A. Atiende prompts como “¿cómo va el run?”, “lanza el run” o “aborta el run” con:

- `ai_dev_loop controller status`
- `ai_dev_loop launch`
- `ai_dev_loop abort`
- comandos read-only existentes (`status`, `logs`, `inspect`, `list`)

Si falta la capacidad de fork o de mensaje entre hilos, los skills se detienen con una explicacion acotada. No adivinan session IDs ni usan `--last`.

## Prepare A/B

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<exact-reviewer-session-id>" \
  --controller-session-id "<exact-controller-session-id>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

Salida JSON esperada (campos relevantes):

```json
{
  "schema_version": 1,
  "status": "prepared",
  "run_id": "my-project-20260704T134512Z-8f43c1",
  "project": "my-project",
  "start_command": "ai_dev_loop start my-project-20260704T134512Z-8f43c1",
  "launch_command": "ai_dev_loop launch my-project-20260704T134512Z-8f43c1 --controller-session-id <exact-controller-session-id>",
  "requires_codex_exit": false,
  "reviewer_must_remain_inactive": true,
  "controller_session_id_present": true
}
```

Para un prepare A/B valido: `requires_codex_exit` es `false` y `reviewer_must_remain_inactive` es `true`. A lanza con `launch`; B debe permanecer intacta.

## Flujo legacy (una sola sesion)

Si omites `--controller-session-id`, el prepare legacy sigue valido:

```json
{
  "start_command": "ai_dev_loop start my-project-20260704T134512Z-8f43c1",
  "requires_codex_exit": true
}
```

Sal de Codex con `/exit` (o deja de usar esa UI) y ejecuta `ai_dev_loop start <run-id>` desde WSL. Los runs historicos sin metadata de controller siguen ese camino.

## No uses `--last`

`ai_dev_loop` nunca debe usar `--last` para Codex. El session ID debe ser exacto.

Para Codex Desktop + WSL, el ID puede venir del hook `SessionStart` de Desktop. Como fallback manual, `integrations sessions list` puede listar IDs de rollout por nombre de archivo, pero solo debes usar un ID si sabes que corresponde a la sesion correcta.

## Runtime de review congelado

En runs nuevos, `prepare` busca el UUID exacto bajo `$CODEX_HOME/sessions` nativo y el puente validado `sessions/from-desktop`. Extrae solo metadata permitida de `session_meta`, `thread_settings_applied` y `turn_context`, incluidos wrappers `event_msg`.

Si no hay override, modelo y reasoning efectivos vienen de esa captura. Un override en YAML o CLI gana solo para su campo. Ambos valores efectivos y su procedencia quedan congelados y se pasan explicitamente a `codex exec resume`.

Los runs historicos de Fase 9 con ambos valores `null` y sin procedencia conservan el camino legacy sin overrides y muestran una advertencia para volver a preparar; esos `null` no significan captura de sesion.

## Quietud del reviewer

No uses la UI interactiva de B mientras el worker corre. El proceso automatizado reanudara B mediante `codex exec resume <exact-reviewer-session-id>`. Usar B en paralelo puede mezclar contexto y romper la auditabilidad del run.

En el flujo legacy sin controller, la misma regla aplica a la sesion unica: no ejecutes `start`/`resume` desde la UI que posee esa sesion.

## Continuidad del chat Cursor y limite de uso

Un run usa un unico Cursor chat ID para implementacion y correcciones. Si Cursor alcanza el limite de uso del modelo configurado, el run origen queda `failed` e inmutable; la continuacion requiere `recover --cursor-model auto` y luego `resume` del sucesor con el mismo chat.

En TTY, `start`/`resume` pueden ofrecer crear ese sucesor automaticamente; la respuesta por defecto es no. No edites ni descartes manualmente trabajo parcial unstaged/untracked antes de `recover`, salvo que decidas abandonar el run y preparar uno nuevo.
