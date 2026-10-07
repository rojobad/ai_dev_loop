# Registro de incidentes

Este registro conserva incidentes observados durante la operación real de
`ai_dev_loop`. Sirve para reconocer recurrencias y decidir posteriormente si
conviene incorporar safeguards o un parche. Registrar un incidente no autoriza
ni implica implementar esas medidas.

Cada entrada debe distinguir hechos verificados, inferencias y causa pendiente;
incluir contexto, impacto, cronología, evidencia, recuperación y limitaciones.
Conserva referencias a los artefactos protegidos, no copias de transcripts,
prompts, credenciales ni IDs de sesiones de agentes.

## Incidentes registrados

| ID | Fecha | Incidente | Estado operativo | Causa inicial |
| --- | --- | --- | --- | --- |
| [INC-2026-10-06-001](2026-10-06-systemd-user-control-channel.md) | 2026-10-06 | Cursor completado; scheduler sin acceso al canal de control de `systemd --user` | Recuperado | No determinada |

Una recurrencia debe tener su propia entrada y enlazar los casos relacionados.
Conserva el diagnóstico original; agrega nueva evidencia identificando cuándo
se obtuvo. Los cambios de código, si se autorizan, se documentan por separado.
