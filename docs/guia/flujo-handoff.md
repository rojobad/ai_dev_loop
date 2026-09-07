# Flujo de handoff

El handoff conecta una sesion interactiva de Codex con el loop automatizado. En el
flujo controller A recomendado, la sesion de planificacion (A) prepara el run con
modelo y reasoning congelados; el worker crea un unico reviewer B read-only en el
primer review y reanuda solo esa identidad despues.

## Flujo controller A (fresh B)

1. Instala la integracion correcta: `wsl-cli` o `codex-desktop-wsl`.
2. Abre Codex en el repositorio objetivo (sesion controlador A).
3. Discute y aprueba el cambio.
4. Genera un plan Markdown y un prompt separado para Cursor.
5. Usa el skill global `ai-dev-loop-handoff`.
6. Desde A, ejecuta `ai_dev_loop prepare` con `--controller-session-id`, `--codex-review-model` y `--codex-review-reasoning-effort` (sin `--codex-session-id`).
7. Desde A, usa el skill `ai-dev-loop-controller` (o `ai_dev_loop launch`) para lanzar el worker local detachado.
8. El worker crea B en el primer review (`codex exec` read-only), captura su session ID y lo reutiliza en reviews posteriores.
9. Desde A, consulta estado (`controller status`), aborta si hace falta e inspecciona cambios staged y artefactos.

No hay notificaciones automaticas al movil ni a ChatGPT: el estado se pide desde A cuando hace falta.

Si un run termina en `max_iterations_reached`, A puede conceder presupuesto adicional:

```bash
ai_dev_loop extend <run-id> --additional-review-iterations 1
ai_dev_loop launch <run-id> --controller-session-id "<exact-controller-session-id>"
```

## Roles A y B

| Sesion | Rol | Acciones |
| --- | --- | --- |
| A (controller) | Planificacion y control | `prepare`, `launch`, `extend`, `controller status`, `abort`, inspeccion read-only |
| B (reviewer) | Creado por el worker en el primer review | Solo reviews read-only; nunca controla el run |

Todo review automatizado reanuda exactamente la identidad B capturada en el primer
review. A nunca se pasa a `codex exec resume`.

## PR-review v2 (sin cambios)

Para adoptar un PR ya abierto con `pr_review_v2.enabled: true`, reviewer B ejecuta
`pr-review prepare` (read-only) y controller A ejecuta `pr-review start` como unica
puerta de efectos externos. Ese contrato permanece separado del flujo controller A
fresh B descrito arriba.

## Legacy directo (sin controller)

Omita `--controller-session-id`, pase `--codex-session-id` exacto y use `ai_dev_loop start`
tras salir del TUI de Codex. Ese camino legacy permanece hasta el cutover documentado.

## Skills globales

```text
ai-dev-loop-handoff
ai-dev-loop-controller
```

`ai-dev-loop-handoff` guia el prepare controller A con modelo/reasoning congelados.

`ai-dev-loop-controller` es solo para A: `controller status`, `launch`, `extend`, `abort` e inspeccion read-only.

Nunca adivine session IDs ni use `--last`.

## Prepare controller A

```bash
ai_dev_loop prepare \
  --repo-path /path/al/repo \
  --plan-path docs/plans/mi-plan.md \
  --prompt-source-path docs/plans/prompt_mi-plan.txt \
  --controller-session-id "<exact-controller-session-id>" \
  --codex-review-model "<exact-review-model>" \
  --codex-review-reasoning-effort "<exact-reasoning-effort>" \
  --output json < docs/plans/prompt_mi-plan.txt
```

Salida JSON esperada (campos relevantes):

- `requires_codex_exit`: `false`
- `launch_command`: presente
- `run_id`: identidad del run preparado
- ningun reviewer session ID enlazado todavia
