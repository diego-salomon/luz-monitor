#!/bin/bash
# Guarda una contraseña en el Llavero pidiéndola en una ventana de macOS (se puede pegar).
# Uso: ./guardar_contrasena.sh <servicio> <cuenta> <nombre visible>
set -u
SERVICIO="$1"; CUENTA="$2"; NOMBRE="${3:-la cuenta}"
CLAVE=$(/usr/bin/osascript -e "text returned of (display dialog \"Pega o escribe la contraseña de $NOMBRE ($CUENTA). Se guardará en el Llavero.\" default answer \"\" with hidden answer with title \"Monitor de luz\" buttons {\"Cancelar\", \"Guardar\"} default button \"Guardar\")") || { echo "Cancelado."; exit 1; }
if [ -z "$CLAVE" ]; then echo "No escribiste nada."; exit 1; fi
/usr/bin/security add-generic-password -U -s "$SERVICIO" -a "$CUENTA" -w "$CLAVE" && echo "Guardada en el Llavero ($SERVICIO / $CUENTA)."
