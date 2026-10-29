# Respaldo de estadísticas SQLite

Este procedimiento es documentación para una ventana de mantenimiento ya
autorizada. No instala el paquete, no importa el ledger vivo, no cambia el
timer, restic ni la retención, y no borra artefactos.

La frontera es: materialización aceptada, snapshot SQLite consistente, restaure
de un solo archivo en otra copia, equivalencia de solo lectura y evidencia del
operador. Una instantánea importada a medias permanece visiblemente parcial.
Perder la fuente no degrada un hecho conocido ya importado. Verificar una
restauración no autoriza ejecución, Git ni borrado.

Los contadores actuales por modelo, rol y etapa son autoridad de la base una
vez importados. Prompts, parches, reviews y la evidencia de sesión que permite
reanudar un agente no viajan en una copia de solo `engine.sqlite3`. El tamaño
depende de los hechos guardados; no hay un tamaño fijo ni una razón de
compresión prometida.

## 1. Aceptar e instalar el paquete final

Durante la ventana autorizada, instala el wheel o sdist ya aceptado con el
intérprete del entorno del scheduler. El paquete base exige Python 3.11 o
posterior y no exige el extra `cursor-sdk` para leer estadísticas. No cambies
el driver instalado mientras el árbol de fuentes todavía está en revisión, y
no hagas esa instalación como parte de la validación de esta fase.

## 2. Respaldo inicial consistente, conservando artefactos

Detén writers antes de copiar. Comprueba el timer solo si esa comprobación fue
autorizada, y no lo deshabilites desde este documento. Con el writer detenido,
toma una copia SQLite mediante la API de backup (`sqlite3 ... .backup` o
`Connection.backup`). Respalda juntos:

- esa copia consistente de `engine.sqlite3`;
- `artifacts/` completo;
- la configuración XDG y el `ai_dev_loop.yaml` aprobado.

No copies el archivo principal mientras un WAL sin checkpoint todavía contiene
commits: esa copia pierde la transacción. No trates `engine.sqlite3-wal` como
un snapshot. Conserva los permisos restrictivos. Este respaldo inicial retiene
los artefactos porque la importación todavía puede necesitarlos.

## 3. Comprobar y aplicar el backfill acotado

```bash
ai_dev_loop scheduler usage-backfill --all --check --output json
ai_dev_loop scheduler usage-backfill --all --apply --batch-size 32 --max-attempts 100 --output json
ai_dev_loop scheduler usage-backfill --resume JOB_ID --output json
```

Hace falta exactamente un alcance (`--all`, `--run-id` o `--project-name`) y
exactamente un modo (`--check` o `--apply`). `--check` no migra, no escribe y
no abre artefactos para validar observaciones ya comprometidas; si falta una
fila exigida, no pertenece al hecho o su contador no coincide, el comando falla
sin reparar. `--apply` abre el store normal, migra si el ledger todavía no está
en schema 22 e importa como máximo `--max-attempts` intentos. El recibo de
texto y el JSON incluyen `job_id` y `job_status`. `--resume` implica apply y
rechaza otro alcance u otros límites. Repite apply o resume hasta que el
trabajo termine o se detenga de forma explícita. No lances agentes y no borres
fuentes porque un job figure `completed`.

## 4. Comparar totales y preparación

Compara, por proyecto, run, secuencia y Dashboard:

```bash
ai_dev_loop scheduler usage RUN_ID --output json
ai_dev_loop scheduler sequence usage SEQUENCE_ID --output json
ai_dev_loop integration run inspect RUN_ID
ai_dev_loop integration sequence inspect SEQUENCE_ID
ai_dev_loop scheduler usage-backfill --all --check --output json
```

La consulta del Dashboard es `dashboard.token-usage`, con el mismo alcance
automático de DUX-06: `fromUtc` inclusivo, `toUtc` exclusivo y zona IANA. No
hay un botón Apply nuevo. Revisa `knownTotalTokens`, cobertura, modelos
configurados, rol, etapa y `statistically_ready`. Un intento terminal sin hecho
sigue visible como `backfill_required` y deja la preparación incompleta. Un
desconocido ya asentado no bloquea la preparación por sí solo. Una deuda de
asentamiento o un conflicto sí dejan `remaining` por encima de cero.

## 5. Respaldar solo SQLite y verificar la restauración

Cuando la comparación anterior sea aceptable para el operador, un respaldo
posterior de estadísticas puede ser únicamente la copia consistente de
`engine.sqlite3`. Para verificarla, restaura ese único archivo bajo un
`XDG_STATE_HOME` nuevo, sin `artifacts/`, sin comandos de proveedor y sin
credenciales, y repite las lecturas del paso 4. Los totales, nombres cortos,
estados, rondas de review, tiempo de proceso observado, buckets de calendario
y filtros de cohorte deben coincidir. Pueden cambiar la hora de observación de
la consulta y la disponibilidad de archivos.

Esas lecturas no migran ni reescriben la base. `scheduler sequence status`
no crea el root de artefactos. `aggregate_counts.checkpointed` cuenta eventos
`sequence_checkpoint_committed` del ledger con digest y payload validados, así
que ese conteo se conserva en una copia que solo tiene la base. La
disponibilidad del archivo de checkpoint, su prefijo y el prefijo del informe
de cierre siguen dependiendo de `artifacts/` y quedan ausentes si esos
archivos no están en la copia. El resto del estado sigue legible. `integration run review-content`
sobre un prompt cuyo archivo no está en la copia responde `available: false` y
`reason: not_recorded`, con el contenido vacío. No llama a un proveedor para
reconstruirlo. `integration run initial-prompt` sigue fallando con integridad
si el artefacto congelado no está, porque el ledger ata su digest.

## 6. Registrar desconocidos y conservar la evidencia

Anota intentos `unknown`, filas `backfill_required`, conflictos y diagnósticos
`usage_source_missing`. Conserva los artefactos originales hasta resolverlos.
Un reimport explícito informa el conflicto y conserva el total conocido
anterior. Perder la fuente después de una importación verificada no baja ese
total. Una copia tomada a medias se reconoce porque `--check` no está
`statistically_ready`.

La integridad numérica de SQLite es distinta de la autenticación del contenido
fuente y de la autoridad de recuperación. La copia de un solo archivo no
autoriza `scheduler start`, reintentos, review, commits, push, limpieza ni
borrado.
