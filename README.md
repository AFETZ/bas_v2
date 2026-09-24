# BAS v2 — проверяемый RC1

Пять ArduPilot SITL в Gazebo, десять MAVLink UART и один НПУ. Реальные байты
проходят через TAP и штатные ns-3.48 SpectrumWifiPhy/802.11n PHY/MAC;
распространение рассчитывает Sionna RT внутри ns-3. Помехи — штатные
WaveformGenerator, а не команды потери пакетов.

Текущий статус поставки, ограничения и аппаратный blocker:
[DELIVERY_SCOPE](doc/DELIVERY_SCOPE.md), [VALIDATION_REPORT](doc/VALIDATION_REPORT.md).
Native Wi-Fi — некалиброванный reference profile, не модель LoRa/NR или конкретного модема.

## Быстрый запуск

На подготовленном Linux/NVIDIA/Docker стенде:

```bash
make demo-preflight DEMO_GUI=0
BAS_NATIVE_FIVE_RUN_ID=my-town01 BAS_NATIVE_SOURCES=network/config/native_jammers_town01.yaml make demo-town01 DEMO_GUI=0
```

Runner заканчивает полёт посадкой, проверяет отсутствие обходного канала и
останавливает процессы. Ненулевой код может означать провал инженерного
real-time порога при успешно завершённом полёте; см. `report.md`.

```bash
make prepare-customer
BAS_NATIVE_FIVE_RUN_ID=my-customer BAS_NATIVE_SOURCES=network/config/native_jammers_town01.yaml make demo-customer DEMO_GUI=0
```

Customer: исходная Town01 без масштабирования, настоящее внешнее поле/холмы
10×10 км и отдельное синтетическое здание с 15 заданными этажами.
Полётный маршрут остаётся в контрольном районе Town01.

Для ручного управления через готовый MAVProxy, в двух терминалах:

```bash
make operator DEMO_GUI=0
make gcs
```

Остановка из другого терминала: `make stop`. Отчёт:
`runs/native-radio-realtime/<RUN_ID>/report.md`; raw/annotated кадры, CSV и PCAP
находятся рядом. Открыть локально: `xdg-open runs/native-radio-realtime/my-town01/report.md`.

## Подготовка и отдельные проверки

`make demo-preflight DEMO_GUI=0 DEMO_BOOTSTRAP=1` создаёт отсутствующее окружение
по закреплённым версиям. Исходный CAVISE bundle нужен локально; он не скачивается
и не публикуется автоматически. Восстановление из поставленного образа и
dependency archive описано в [USER_GUIDE](doc/USER_GUIDE.md).

```bash
BAS_NATIVE_FIVE_RUN_ID=stationary BAS_NATIVE_LATENCY_MODE=1 make demo-town01 DEMO_GUI=0
make native-sources
make native-maps
make native-cache-study
make native-matrix
```

Последние три режима — самостоятельные native исследования. Heatmaps показывают
прогноз PSD/SINR, не измеренный PDR. Полные результаты остаются в ignored `runs/`.

[Инструкция оператора](doc/USER_GUIDE.md) · [Архитектура](doc/PRODUCT_ARCHITECTURE.md)
· [Окружение и assets](doc/ENVIRONMENT_AND_ASSETS.md) · [Требования](doc/PRODUCT_REQUIREMENTS.md)
