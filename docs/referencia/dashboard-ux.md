# Gobernanza de UX del Dashboard

Este es el contrato vigente para planificar, implementar y revisar la
presentación del Dashboard local de `ai_dev_loop`. Recoge las decisiones del
operador sobre gráficas, inspección y legibilidad. Se aplica a cambios del
Dashboard; no impone nuevas interfaces ni controles al scheduler o a otros
productos. Las instrucciones explícitas del operador pueden actualizarlo.

Los planes archivados conservan el contexto de sus fases. Sus capturas o
criterios anteriores no restablecen Apply, listados de tarjetas abiertos, IDs en
tooltips o saltos de página. Una propuesta futura que cambie estas decisiones
debe declarar ese cambio en su alcance, sin tratarlo como un detalle mecánico.
La política no autoriza nuevas operaciones, instalación, publicación ni exposición
remota. Los [contratos de datos](dashboard.md) y las
[fronteras de seguridad](../operacion/seguridad-privacidad.md) siguen vigentes.

## DUX-01 — Gráficas y jerarquía visual

- Dar prioridad a la gráfica, los valores útiles y los controles de inspección.
  Evitar párrafos permanentes, títulos técnicos del motor y datos duplicados.
- Mantener visibles título, selección relevante, progreso breve, frescura y
  errores o advertencias accionables. Los KPIs compactos de resumen pueden
  permanecer visibles.
- Los gráficos `Started runs by project and state` y `Project workload` ocupan
  cada uno el ancho completo. Mapa y timeline pueden compartir fila en pantallas
  amplias y apilarse en pantallas estrechas.
- Usar contadores secundarios discretos: el mapa muestra `40/40 loaded`, en gris,
  tamaño menor y peso normal, con el estilo del contador del timeline. El título
  conserva espacio; los contadores no deben convertirse en otro encabezado.
- Conservar unidades y etiquetas que permitan interpretar la gráfica. Eliminar
  títulos de implementación como `Distinct run position within group`.
  Evitar solapamientos: el timeline usa fechas del eje en vertical y omite ticks
  que no caben. No reducir el tamaño del texto hasta volverlo ilegible.

## DUX-02 — Explicaciones mediante el botón de información

Cada panel o gráfica ofrece una *i* en cursiva dentro de un círculo. El botón
abre las definiciones, cómo interpretar los datos, semántica de filtros y
procedencia detallada. El texto visible del botón no es `Information`.

- Conservar un nombre accesible contextual, por ejemplo `Information about
  Activity calendar`, además de `aria-expanded` y la relación con el diálogo.
- Funcionar con ratón, teclado y tacto. La explicación se abre por acción
  deliberada; no depende de hacer hover sobre el icono.
- Al abrir, llevar el foco al diálogo; contenerlo mientras sea modal, admitir
  Escape/cierre y devolverlo al activador con `preventScroll`.
- Ocultar las explicaciones cuando no se solicitan. Cobertura incompleta,
  errores, evidencia ausente y fallos de actualización relevantes siguen visibles;
  el botón no debe esconder un estado que cambie la interpretación de los datos.

Reutilizar `InformationControl` para mantener ese comportamiento coherente.

## DUX-03 — Inspección vinculada y detalle colapsado

- No mostrar una tarjeta por cada run o por cada fase de una secuencia. La
  cantidad de recursos no debe producir una pared de tarjetas.
- El mapa empieza sin run seleccionado. Una celda o el selector accesible carga
  exactamente ese run; otra selección reemplaza la anterior. No elegir el primer
  run automáticamente ni convertir la selección de una fase en todos sus runs.
- El encabezado del timeline conserva el nombre corto seleccionado, el contador
  loaded/recorded y el botón de información. Su ficha extensa y el linaje se
  consultan desde información; no ocupan espacio permanente antes de la gráfica.
- Las tarjetas de intentos permanecen en `Attempt details`, cerrado por defecto.
  La lista de días del calendario sigue el mismo patrón en `Show daily values`.
  Mantener un equivalente accesible a los clics de la gráfica.
- El clic en una barra abre el modal de ese intento, inicialmente con metadata.
  Un listado colapsado no debe abrirse ni recibir foco oculto por ese clic.
- El título humano del intento es `N - Iteration I - Implementer`, `Reviewer` o
  `Unclassified`, compartido entre eje, tooltip, tarjeta y modal. `N` es el
  correlativo del intento en el run: los reintentos tienen números distintos,
  estables al paginar y refrescar. No inferir el rol desde un ID o nombre.

## DUX-04 — Nombres legibles e IDs secundarios

Usar `shortName` y `sequenceShortName` como etiquetas de gráficas y títulos de
recursos. Son metadata de presentación del scheduler: no derivar un alias nuevo
analizando IDs, slugs o texto de estado en el navegador.

Los preparadores eligen nombres descriptivos de hasta **35 puntos de código
Unicode**, después de quitar espacios exteriores, para runs, secuencias y cada
run planificado. Validar nuevas entradas, sin truncarlas silenciosamente. El
backfill histórico puede truncar y volver a recortar espacios; una edición
posterior conserva IDs e inputs congelados. Consultar la
[CLI vigente](cli.md) para la versión de manifiesto y los comandos disponibles.

- Priorizar nombre, rol, estado, iteración, fechas y duración relevantes para
  una persona. Provider, componente y efecto son metadata secundaria.
- Los tooltips de gráficas no incluyen campos Run ID, Sequence ID ni Attempt ID.
  Los títulos tampoco usan esos identificadores como contenido principal.
- Si se muestran IDs en los detalles de inspección, ubicarlos al final, después
  de la información humana. Mantener disponibles nombres originales cuando
  aporten contexto.
- Conservar los IDs exactos en DTOs, claves de selección y callbacks. Dos aliases
  iguales siguen representando recursos distintos; un cambio de presentación
  nunca cambia identidad, linaje, pertenencia a proyecto ni autoridad.
- Escapar todo texto de origen en tooltips HTML; usar texto React normal en
  controles y detalles.

## DUX-05 — Layout y scroll estables

Los clics en gráficas, cambios de métrica y respuestas de refresco deben mantener
la gráfica inspeccionada dentro del plano visible. Conservar la posición de la
página durante cambios que no requieren navegación.

- No llamar a `scrollIntoView` ni desplazar la página para enfocar una tarjeta
  al seleccionar una celda o barra. Restaurar el foco con `preventScroll`.
  Si el modal vino de una gráfica y las tarjetas están colapsadas, volver al
  resumen accesible; si están abiertas, puede volver al intento correspondiente.
- Limitar el mapa de secuencias a **500 px** de alto y permitir scroll interno
  vertical y horizontal. Mantener espacio por fila y una región desplazable
  accesible por teclado. El scroll interno no debe arrastrar la página al alcanzar
  un borde ni comprimir todas las secuencias hasta deformar sus celdas.
- Reservar la geometría del calendario durante un cambio de métrica o carga.
  No dejar que su desaparición temporal reduzca el documento y provoque un
  reajuste del scroll. El resultado vacío o un error debe ser explícito.
- Reservar espacio y separar filas de filtros/acciones para que el foco, los
  campos y los botones no se solapen. Verificar pantallas estrechas y amplias.
- Los tooltips quedan dentro del viewport y por encima de los paneles, sin
  recortes por `overflow`. Van por debajo de los modales, respetan el tema,
  conservan el escape de texto y se retiran al desmontar su gráfica.

## DUX-06 — Scope automático y refresco coherente

Cambiar proyecto o preset válido aplica el scope inmediatamente. Editar fecha/hora
o zona aplica el borrador completo válido tras **400 ms**. Cambiar la
métrica de actividad solicita esa métrica directamente. No añadir un botón
Apply para completar esos cambios.

- Un borrador incompleto o inválido conserva la vista del último scope válido
  y muestra una explicación breve. No enviar fechas parciales ni adivinar la zona.
- From es inclusivo y Until exclusivo; proyecto usa el campo autoritativo.
  Mapa y resumen incluyen los runs cuyo primer lanzamiento de ejecución está
  en ese rango, aplicando el proyecto seleccionado. Actividad se fecha por evento.
- Un cambio de scope o recurso retira la selección incompatible, cancela sus
  lecturas y limpia contenido sensible. Respuestas atrasadas no pueden restaurar
  el scope o recurso anterior. Un debounce antiguo no revierte un proyecto
  elegido más recientemente.
- Conservar Refresh explícito, Pause/Resume, suspensión al ocultar la pestaña y
  el máximo de dos lecturas activas. Usar el coordinador compartido y las
  [cadencias y límites de lectura](dashboard.md), sin temporizadores por widget
  que eludan esas reglas.
- Cargar automáticamente la cohorte de runs, solicitando hasta 1000 por lote y
  reduciendo únicamente ante el error tipado de sobre demasiado grande. Mantener
  paginación interna, progreso y reintento. No reintroducir Load more para el mapa
  ni deducir totales de páginas parciales. Timeline y contenido conservan sus
  propios límites y paginación.
- Preservar las páginas históricas compatibles mientras se refrescan datos y
  reconciliar por ID exacto. Ninguna colección de observaciones fechadas pasa a
  ser un snapshot atómico por cómo se presenta en la pantalla.

## DUX-07 — Fechas, horas y zona

| Dato visible | Presentación |
| --- | --- |
| Instante, incluyendo filtros y metadata | `DD/MM/yyyy HH:mm`, reloj de 24 horas |
| Bucket diario ya calculado por el productor | `DD/MM/yyyy` |
| Tick de eje temporal con espacio limitado | `DD/MM HH:mm`, con instante completo disponible en tooltip/detalle |

Todos los instantes usan la **zona IANA seleccionada**, incluyendo creación,
lanzamiento, finalización, resets de capacidad y observaciones. No usar la zona
implícita de la máquina o del navegador. No aplicar otra conversión UTC a un
bucket que ya representa un día de esa zona.

Reutilizar `formatInstant`, `formatAxisInstant`, `formatCalendarDay` y la
validación de `time.ts`. Rechazar horas locales inexistentes; para horas ambiguas
por DST exigir un offset explícito válido para la zona. La API y la persistencia
conservan sus instantes UTC/offsets y precisión originales. Los bytes de logs,
prompts y contenido de origen se inspeccionan sin reformatearlos.

## DUX-08 — Duraciones humanas

Reutilizar `formatDuration` y `formatAge`; no escribir otro formato por widget.

| Segundos observados | Presentación |
| --- | --- |
| 0 | `00 h 00 m` |
| Positivos y menores de 60 | `<1 m` |
| 60 | `00 h 01 m` |
| 9300 | `02 h 35 m` |
| Duración desconocida o inválida | `Unavailable` o indisponibilidad tipada equivalente |

Truncar a minutos completos, con al menos dos dígitos para horas y minutos;
las horas pueden superar 99. Aplicar también a ventanas de capacidad, edades de
observaciones y totales de tiempo de proceso visibles. Mantener unidades
numéricas explícitas cuando las necesite la escala de la gráfica; sus tooltips
usan la duración humana. No fabricar finales o duraciones de procesos activos,
ni convertir ausencias en cero. El formato no cambia intervalos, sumas paralelas,
segundos del productor o límites de consulta.

## DUX-09 — Evidencia proporcional y fronteras

Mantener `DashboardDataSource` como frontera de componentes, DTOs compartidos
como contrato de datos y el scheduler como autoridad. Las lecturas del Dashboard
no migran ni reparan el ledger. Prompt, review y stdout/stderr requieren una
acción deliberada, quedan en memoria y se retiran al cerrar o cambiar de scope;
no son peticiones automáticas de un tooltip, selección o polling.

Planificar y revisar los contratos afectados con evidencia de sus caminos reales:

| Cambio | Evidencia relevante |
| --- | --- |
| Gráficas, etiquetas, nombres, IDs y títulos | `RunMap.test.tsx`, `AttemptTimeline.test.tsx`, `charts.test.ts`; callbacks conservan IDs exactos y texto escapado |
| Información y detalles colapsados | `InformationControl.test.tsx` y navegador: teclado, Escape, foco dentro del modal y retorno sin scroll |
| Layout, scroll interno o cambio de métrica | Navegador con muchas secuencias, final de página y respuesta demorada; comprobar geometría y scroll durante carga y tras resolver |
| Scope/refresco | `DashboardApp.test.tsx`, `refreshCoordinator.test.ts`, pruebas de calendario y navegador: borradores inválidos, carreras y respuestas retiradas |
| Tiempo | `time.test.ts`, pruebas de calendario: zona distinta a la máquina, DST, medianoche, día ya zonificado, menos de un minuto y duración desconocida |
| Assets entregados | Build del frontend y manifiesto coherente con sus fuentes; pruebas instaladas solo cuando el alcance afecte el empaquetado |

Seleccionar las comprobaciones según el comportamiento cambiado. Una captura
ayuda a la revisión visual; no demuestra cancelación, identidad o estabilidad del
scroll. Para un fallo de carga, demorar la respuesta en el camino de producción y
medir el layout, sin introducir el arreglo desde el test. Usar fixtures y
providers fake para las pruebas; la suite completa del repositorio conserva su
gate separado de integración. Cambios solo de gobernanza requieren coherencia,
enlaces y documentación válida, sin nuevas pruebas de runtime que comparen texto.
