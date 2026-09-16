# Recuperación fresh-review (Phase 20.6)

La recuperación authenticated fresh-review reemplaza una fase bloqueada del scheduler con un run de recuperación que:

1. congela el árbol fuente revisado en `scheduler recovery prepare`;
2. materializa un worktree gestionado y un reviewer Codex nuevo en `scheduler recovery start`;
3. integra el árbol aceptado en el repositorio objetivo con autorización explícita;
4. limpia el worktree privado sin `reset`, `force` ni borrado amplio.

## Comandos

```bash
ai_dev_loop scheduler recovery prepare <source-run-id> --commit-message "<mensaje>"
ai_dev_loop scheduler recovery start <recovery-id>
ai_dev_loop scheduler recovery status <recovery-id>
ai_dev_loop scheduler recovery abort <recovery-id>
ai_dev_loop scheduler tick
```

`prepare` es de solo lectura respecto al worktree fuente. El cálculo del árbol staged usa un índice y un object store temporales aislados; no muta el índice, HEAD, refs ni los objetos del repositorio fuente.

`start` persiste un intento durable antes de crear el worktree gestionado, reacquiere la reserva liberada del run fuente bloqueado y la retiene durante la integración. Si la operación se interrumpe, un reintento autentica el intento original y continúa desde la evidencia ya escrita.

La integración separa la verificación del árbol fuente congelado de la verificación del árbol aceptado. Cuando el árbol aceptado difiere del fuente, se aplica un delta binario acotado antes del commit final.

`abort` durante `integration_pending` o `cleanup_pending` reconcilia primero un posible CAS exitoso en la rama objetivo. Si la integración ya quedó probada, registra el éxito y no revierte el commit.

Las secuencias finales recuperadas terminan en `recovery_integrated_finalization`, preservando el run materializado original y la línea de recovery, distinto de `awaiting_finalization` ordinario. Tras esa finalización se libera la reserva del repositorio objetivo; push, PR y merge siguen siendo acciones manuales.

Los holds de reconciliación de checkpoint solo se liberan tras un CAS verificado; un hold con el mismo intento de integración se reconcilia en reintentos y no se trata como abort ajeno. La eliminación del ref privado usa `update-ref -d` con el SHA esperado.

`integrate_recovery_pending` clasifica primero la evidencia de replay autenticada (checkout gestionado, CAS privado, delta, CAS de rama) antes de exigir el árbol fuente congelado. `abort` durante integración o limpieza completa la reconciliación idempotente (resolución de secuencia, resultado, `cleanup_pending`) en lugar de marcar `integrated` directamente.

## Límites de fase

- Sin publicación remota, resolución de conflictos ni `git push`.
- Sin modelos reales en tests automatizados.
- La autoridad Git estrecha permite un commit local sin firmar y un CAS de rama solo dentro de esta recuperación explícita.
