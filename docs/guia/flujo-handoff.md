# Flujo de handoff

El handoff conecta una sesion interactiva de Codex (controller A) con el scheduler
central. La sesion de planificacion congela modelo y reasoning de review; el scheduler
crea un unico reviewer B en el primer review y reanuda solo esa identidad despues.

## Flujo controller A (fresh B)

1. Instala la integracion correcta: `wsl-cli` o `codex-desktop-wsl`.
2. Abre Codex en el repositorio objetivo (sesion controlador A).
3. Discute y aprueba el cambio.
4. Genera un plan Markdown y un prompt separado para Cursor.
5. Usa el skill global `ai-dev-loop-handoff`.
6. Desde A, ejecuta `ai_dev_loop scheduler submit` con `--controller-session-id`, `--codex-review-model` y `--codex-review-reasoning-effort` (sin `--codex-session-id`).
7. Desde A, autoriza el run con `ai_dev_loop scheduler start <run-id>`.
8. Ejecuta `ai_dev_loop scheduler tick` manualmente o habilita el timer systemd empaquetado (accion separada y explicita).
9. El scheduler crea B en el primer review, captura su session ID y lo reutiliza en reviews posteriores.
10. Desde A, consulta estado (`controller status`, `scheduler status`), aborta si hace falta e inspecciona cambios staged y artefactos.

No hay notificaciones automaticas: el estado se pide desde A cuando hace falta.

## Roles A y B

| Sesion | Rol | Acciones |
| --- | --- | --- |
| A (controller) | Planificacion y control | `scheduler submit`, `scheduler start`, `controller status`, `scheduler abort`, inspeccion read-only |
| B (reviewer) | Creado por el scheduler en el primer review | Solo revisa; nunca controla el run |

Todo review automatizado reanuda exactamente la identidad B capturada en el primer
review. A nunca se pasa a `codex exec resume`.

## Skills globales

```text
ai-dev-loop-handoff
ai-dev-loop-controller
```

`ai-dev-loop-handoff` guia el submit controller A con modelo/reasoning congelados.

`ai-dev-loop-controller` es solo para A: `controller status`, `scheduler start`,
`scheduler abort` e inspeccion read-only del scheduler.

Nunca adivine session IDs ni use `--last`.

## Submit controller A

```bash
ai_dev_loop scheduler submit \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --controller-session-id "<exact-controller-session-id>" \
  --codex-review-model "<exact-review-model>" \
  --codex-review-reasoning-effort "<exact-reasoning-effort>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

La siguiente accion segura es `ai_dev_loop scheduler start <run-id>` con el mismo
controller session ID.
