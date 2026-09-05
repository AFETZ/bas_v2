# BAS v2 RC1: монтажные главы

1. **Пять БАС и customer-сцена.** Непрерывные камеры текущего Gazebo:
   обзор 10×10 км, синтетические поле/холмы и 15-этажная башня;
   настоящее окно MAVProxy с UAV2/ACK; отдельный подписанный flight run,
   пять взлётов, автономная миссия UAV1, LAND и разоружение.
2. **Геометрия и радио.** Тот же безопасный маршрут: открытая точка,
   коридор, obstructed candidate, возврат. Два направления UAV1 и контроль UAV2,
   реальные powers/paths/age/retries; разрывы не сглаживаются и кэш не сдвигается.
3. **UART и доступ к среде.** Живая группа, десять SERIAL1/SERIAL2,
   настоящие ответы SITL; десять раундов пяти одновременных REQUEST_MESSAGE;
   P2P в обоих направлениях, 20 multicast roots и пять receiver sets;
   native shared uplink, goodput/PDR/fairness завершённого окна.
4. **Помехи и карты.** Восемь отдельных native cases: baseline, continuous,
   front, back, pulsed, sweep, multiple, nonoverlap. Каждый переход содержит
   case/run_id; параметры и метки источников — аннотации конфигурации.
   Native baseline/jammer/delta heatmaps рассчитаны отдельно после capture.
5. **Интерфейсы и envelope.** Serial/PTy, TCP, UDP до реального UART SITL;
   безопасные команды MAVProxy, разрыв TCP и восстановление, затем остановка
   общего ns-3/Sionna при работающих SITL/Gazebo/UART. Постоянная подпись о
   неподключённом физическом FC. Далее явно подписанные результаты RC1:
   1/5/16 radio nodes и outage на дальностях; состав поставки и ограничения.

Точные времена определяются исполнением, а не заранее заданным зелёным статусом.
`edit_timeline.csv` фиксирует source frame time, host monotonic и время монтажа.
Сокращённые ожидания, отдельные runs и фактическая частота capture обозначаются.
