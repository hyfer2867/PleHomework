#!/bin/zsh
# Настройки читаются из существующего .env. Шаблон их не перезаписывает.
cd -- "${0:A:h}" || exit 1
if [[ -x .venv/bin/python ]]; then
    .venv/bin/python -m rea_bot
else
    python3 -m rea_bot
fi
result=$?
if [[ $result -ne 0 ]]; then
    print "Бот остановился с ошибкой. Сообщение об ошибке — выше."
    read "?Нажмите Enter, чтобы закрыть окно."
fi
exit $result
