# Timer del scheduler en WSL

El timer empaquetado ejecuta un `ai_dev_loop scheduler tick` acotado como servicio
de usuario `systemd`. Es una alternativa al tick manual para que un run ya
autorizado progrese de forma eventual. No hace `submit`, no autoriza un run
`queued`, no habilita el timer por sí solo y no sustituye la aceptación humana
de los cambios staged.

Habilitarlo, deshabilitarlo o cambiar el arranque de WSL son decisiones
operativas explícitas del operador. Hazlo solamente tras validar la instalación
y aceptar manualmente el uso del timer en ese equipo.

## 1. Requisitos de WSL y systemd

El timer requiere una distribución WSL que ejecute `systemd` y un gestor de
servicios de usuario disponible. Compruébalo desde WSL:

```bash
ps -p 1 -o comm=
systemctl --user status --no-pager
```

El primer comando debe mostrar `systemd`. En una distribución WSL 2 donde no lo
esté, habilítalo como configuración de la distribución:

```ini
# /etc/wsl.conf
[boot]
systemd=true
```

Después, desde PowerShell o Símbolo del sistema de Windows, ejecuta
`wsl.exe --shutdown` y vuelve a iniciar la distribución. Consulta la
[documentación de Microsoft sobre systemd en WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd)
si la distribución no arranca con systemd.

Para que el gestor de usuario pueda iniciarse cuando arranca la distribución,
incluso sin una shell interactiva abierta, habilita *linger* una vez para el
usuario de WSL:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

El segundo comando debe informar `Linger=yes`. No ejecutes `systemctl --user`
con `sudo`.

`linger` no inicia WSL desde Windows ni impide que WSL se apague. Los servicios
systemd tampoco mantienen viva por sí solos una instancia WSL. Por tanto, el
timer empieza a funcionar cuando la distribución WSL arranca y progresa mientras
esa instancia permanece activa; automatizar el arranque de WSL desde Windows es
una decisión externa a `ai_dev_loop`.

## 2. Instalar y habilitar el timer

Instala primero una versión local validada de `ai_dev_loop`. Si actualizaste un
checkout local, la ruta habitual es:

```bash
uv tool install --force .
command -v ai_dev_loop
ai_dev_loop --version
```

No apuntes `CODEX_HOME` ni `CODEX_SQLITE_HOME` a la home de Codex Desktop bajo
`/mnt/c`. Si una shell heredó por error esos overrides de Windows, abre una
shell WSL normal o elimínalos solo para este comando, por ejemplo:

```bash
env -u CODEX_HOME -u CODEX_SQLITE_HOME ai_dev_loop scheduler timer validate --output json
```

Valida los assets empaquetados, instálalos y habilita el timer de forma
explícita:

```bash
ai_dev_loop scheduler timer validate --output json
ai_dev_loop scheduler timer install --enable --output json
ai_dev_loop scheduler timer status --output json
```

`install` escribe únicamente las unidades owned bajo
`~/.config/systemd/user/` y hace `daemon-reload`. Se niega a sobrescribir
unidades existentes que no pertenezcan a `ai_dev_loop`. La unidad de servicio
usa un `PATH` acotado que incluye `%h/.local/bin`, compatible con la instalación
habitual de `uv tool`; no depende del perfil de una shell interactiva.

## 3. Verificar el funcionamiento

Después de habilitarlo —o de reiniciar WSL sin trabajo activo— verifica la
programación, la proyección del producto y el journal:

```bash
systemctl --user list-timers --all ai-dev-loop-scheduler-tick.timer --no-pager
ai_dev_loop scheduler timer status --output json
journalctl --user -u ai-dev-loop-scheduler-tick.service -b --no-pager
```

En `scheduler timer status`, lo normal es ver las dos unidades instaladas,
propiedad y contenido correctos, y el timer `enabled` y `active`. Es normal que
`service_active` sea `inactive` entre ticks: la unidad de servicio es
`Type=oneshot` y termina al finalizar cada tick. También es normal que el
journal informe `Visited runs: 0` cuando no hay trabajo scheduler elegible.

El asset actual solicita el primer tick tras el arranque y ticks posteriores a
intervalos de 30 segundos, pero systemd puede agrupar o retrasar activaciones.
Trátalo como un mecanismo periódico de progreso eventual, no como un reloj de
tiempo real ni como garantía de una ejecución exacta cada 30 segundos.

No apagues WSL para probar el arranque si hay un intento Cursor o Codex activo.
Primero espera a un estado sin attempt activo o aborta el run de forma explícita
si esa es la decisión del operador.

## 4. Actualizar o recuperar unidades

Tras actualizar el paquete, refresca las unidades empaquetadas y comprueba que
coinciden con el paquete instalado:

```bash
uv tool install --force .
ai_dev_loop scheduler timer validate --output json
ai_dev_loop scheduler timer install --output json
ai_dev_loop scheduler timer status --output json
```

Si el timer ya estaba habilitado, `install` actualiza los archivos owned y hace
`daemon-reload`; confirma después que sigue `enabled` con `timer status`. Si
estaba deshabilitado, usa `install --enable` solo si deseas volver a activarlo.

Si `timer status` indica que los archivos instalados difieren de los assets
empaquetados, ejecuta `scheduler timer install`. Si el servicio termina con
`203/EXEC`, confirma `command -v ai_dev_loop`, reinstala la herramienta y repite
validación e instalación. Consulta también [Troubleshooting](troubleshooting.md).

## 5. Detener o desinstalar

Antes de desinstalar la herramienta, detén y deshabilita el timer:

```bash
ai_dev_loop scheduler timer disable --output json
ai_dev_loop scheduler timer status --output json
```

`disable` detiene el timer y evita ticks futuros, pero conserva los archivos de
unidad owned para una futura reinstalación. No deshabilites *linger* salvo que
hayas confirmado que ningún otro servicio de usuario de ese WSL lo necesita.

Después puedes seguir [Desinstalación y limpieza](desinstalacion-limpieza.md)
para retirar la herramienta o sus integraciones. Deshabilitar el timer no aborta
un run; los artefactos y el estado durable se preservan.
