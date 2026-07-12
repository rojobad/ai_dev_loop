# Flujo de handoff

El handoff conecta una sesion interactiva de Codex con el loop automatizado. La regla central es que la misma sesion de Codex que discutio y aprobo el plan debe ser reanudada para todos los reviews.

## Flujo recomendado

1. Instala la integracion correcta: `wsl-cli` o `codex-desktop-wsl`.
2. Abre Codex en el repositorio objetivo.
3. Discute y aprueba el cambio.
4. Genera un plan Markdown y un prompt separado para Cursor.
5. Usa el skill global `ai-dev-loop-handoff` o ejecuta manualmente `ai_dev_loop prepare`.
6. `prepare` recibe el prompt exacto por stdin, resuelve el UUID exacto de sesion y captura su modelo/reasoning sin conservar contenido del transcript.
7. Copia el `start_command`.
8. Sal de Codex con `/exit` o deja de usar esa UI para esa sesion.
9. Ejecuta `ai_dev_loop start <run-id>` desde WSL.
10. Inspecciona cambios staged, review final y artefactos.

## Uso del skill global

El skill instalado se llama:

```text
ai-dev-loop-handoff
```

Su responsabilidad es guiar a Codex para:

- confirmar que el plan esta final y aprobado;
- confirmar que el prompt separado existe;
- leer `ai_dev_loop.yaml`;
- usar el session ID exacto inyectado por el hook `SessionStart`;
- ejecutar `ai_dev_loop prepare`;
- pasar el prompt exacto por stdin;
- devolver al usuario el comando `start`;
- no iniciar el loop desde la UI activa de Codex.

## Prepare manual

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --codex-session-id "<session-id-exacto>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

Salida JSON esperada:

```json
{
  "schema_version": 1,
  "status": "prepared",
  "run_id": "my-project-20260704T134512Z-8f43c1",
  "project": "my-project",
  "start_command": "ai_dev_loop start my-project-20260704T134512Z-8f43c1",
  "requires_codex_exit": true
}
```

## No uses `--last`

`ai_dev_loop` nunca debe usar `--last` para Codex. El session ID debe ser exacto.

Para Codex Desktop + WSL, el ID puede venir del hook `SessionStart` de Desktop. Como fallback manual, `integrations sessions list` puede listar IDs de rollout por nombre de archivo, pero solo debes usar un ID si sabes que corresponde a la sesion correcta.

## Runtime de review congelado

En runs nuevos, `prepare` busca el UUID exacto bajo `$CODEX_HOME/sessions` nativo y el puente validado `sessions/from-desktop`. Extrae solo metadata permitida de `session_meta`, `thread_settings_applied` y `turn_context`, incluidos wrappers `event_msg`.

Si no hay override, modelo y reasoning efectivos vienen de esa captura. Un override en YAML o CLI gana solo para su campo. Ambos valores efectivos y su procedencia quedan congelados y se pasan explicitamente a `codex exec resume`.

Los runs historicos de Fase 9 con ambos valores `null` y sin procedencia conservan el camino legacy sin overrides y muestran una advertencia para volver a preparar; esos `null` no significan captura de sesion.

## Regla de control de sesion

No ejecutes `start` ni `resume` desde la misma UI interactiva que posee la sesion original. El proceso automatizado reanudara esa sesion mediante `codex exec resume <session-id>` para escribir turns de review. Usar la UI simultaneamente puede mezclar contexto y romper la auditabilidad del run.

## Continuidad del chat Cursor y limite de uso

Un run usa un unico Cursor chat ID para implementacion y correcciones. Si Cursor alcanza el limite de uso del modelo configurado, el run origen queda `failed` e inmutable; la continuacion requiere `recover --cursor-model auto` y luego `resume` del sucesor con el mismo chat.

En TTY, `start`/`resume` pueden ofrecer crear ese sucesor automaticamente; la respuesta por defecto es no. No edites ni descartes manualmente trabajo parcial unstaged/untracked antes de `recover`, salvo que decidas abandonar el run y preparar uno nuevo.
