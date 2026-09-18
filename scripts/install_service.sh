#!/usr/bin/env bash
# Устанавливает и включает автозапуск бота через systemd — бот будет
# стартовать сам после перезагрузки сервера и перезапускаться при падении.
#
# Запуск (из папки проекта, от пользователя с правами sudo/root):
#   ./scripts/install_service.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="tgparserwords"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
TEMPLATE="${APP_DIR}/systemd/tgparserwords.service.template"
SERVICE_USER="${SUDO_USER:-$(whoami)}"

echo "Папка проекта:   ${APP_DIR}"
echo "Пользователь:    ${SERVICE_USER}"
echo

if [[ $EUID -ne 0 ]]; then
    echo "Нужны права root (запись в /etc/systemd/system). Перезапустите так:" >&2
    echo "  sudo ./scripts/install_service.sh" >&2
    exit 1
fi

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    echo "Не найдено ${APP_DIR}/.venv/bin/python — сначала создайте venv и поставьте зависимости:" >&2
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
    exit 1
fi

if [[ ! -f "${APP_DIR}/.env" ]]; then
    echo "Не найден ${APP_DIR}/.env — скопируйте .env.example в .env и заполните значения." >&2
    exit 1
fi

if [[ ! -f "${APP_DIR}/parser.session" ]]; then
    echo "Не найден ${APP_DIR}/parser.session — сначала выполните вход в аккаунт:" >&2
    echo "  source .venv/bin/activate && python login.py" >&2
    exit 1
fi

sed \
    -e "s#{{APP_DIR}}#${APP_DIR}#g" \
    -e "s#{{SERVICE_USER}}#${SERVICE_USER}#g" \
    "${TEMPLATE}" > "${UNIT_PATH}"

echo "Юнит записан: ${UNIT_PATH}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo
echo "Готово. Бот запущен и будет стартовать автоматически после перезагрузки."
echo
echo "Полезные команды:"
echo "  systemctl status ${SERVICE_NAME}     — статус"
echo "  journalctl -u ${SERVICE_NAME} -f     — логи в реальном времени"
echo "  systemctl restart ${SERVICE_NAME}    — перезапуск"
echo "  systemctl stop ${SERVICE_NAME}       — остановка"
echo "  systemctl disable ${SERVICE_NAME}    — убрать из автозапуска"
