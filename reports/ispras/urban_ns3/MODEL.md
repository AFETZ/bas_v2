# Новая городская модель: спецификация до пилотов

28.10.2026. Статус: реализация/пилоты; основной эксперимент ещё не заморожен.
Основание `96123dc`; новые результаты не относятся к старым30/18/candidate8.

## Модель и область вывода

Используем upstream ns-3.48 `Building`/`BuildingsChannelConditionModel` для
пересечения прямой трёхмерной линии фактических позиций с корпусами. Затем тонкий
адаптер выбирает один upstream `LogDistancePropagationLossModel` LOS/NLOS.
PHY/MAC/ошибки приёма остаются native Wi-Fi. Без Matrix/iptables-инъекции в городской серии.

Профиль является геометрической эмпирической аппроксимацией средних потерь.
Он не вычисляет стены/проникновение, отражения, дифракцию/многолучёвость,
коррелированное случайное shadowing и реальное покрытие конкретного квартала.
Переход состояния на границе прямой видимости может быть резким — свойство
аппроксимации, не измеренный переход физического канала.

## Параметры — объявленные гипотезы, не калибровка

| Параметр | Значение/источник |
|---|---|
| Частота | 2,412 GHz, действующий native Wi-Fi channel1/20MHz |
| Мощность | 0,01W=10dBm, прежний native radio config |
| Антенны | isotropic single element,0dBi; исходный antenna point=Gazebo model origin, offset0 |
| Reference distance | 1m для обеих моделей |
| Reference loss | 40,09532929124565dB, isotropic Friis на1m/2,412GHz; проверить upstream probe |
| LOS exponent | 2,0 — свободнопространственный reference assumption |
| NLOS exponent nominal | 3,0 — исследуемая гипотеза |
| NLOS exponent low/high | 2,7/3,3 — заранее объявленная чувствительность |
| A2G/A2A | Одинаковые коэффициенты как явное упрощение, не эмпирическое доказательство равенства |
| Дополнительный packet-wise random shadowing | Отсутствует |

Это не оценка коэффициентов по RSSI в BAS и не перенос коэффициентов из одной
статьи на другую частоту/высоты. Три NLOS уровня заданы до пилотов для исследования
условной устойчивости выводов. Нельзя менять их после просмотра выигрыша failover.
Если исходы показывают отсутствие потерь/пользы либо полную изоляцию, сохранить их.

Первичные источники: [upstream propagation](https://www.nsnam.org/docs/models/html/propagation.html),
[Buildings](https://www.nsnam.org/docs/models/html/buildings-design.html).
Authoritative pinned checkout: ns-3 `d2add90b452d600cfb4859baed8e9ea633519447`,
`src/propagation/model/propagation-loss-model.cc`, `src/buildings/model/buildings-channel-condition-model.cc`.
Default ReferenceLoss46,6777dB относится к5,15GHz и здесь не используется.

Измерительная литература показывает зависимость коэффициентов от условий:
[Lee et al.,2022,2.4/5GHz A2G with obstacles](https://engjournal.kics.or.kr/digital-library/38153),
[Qiu et al.,2017,semiurban low-altitude A2G](https://doi.org/10.1155/2017/1587412).
Не обнаружен проверенный переносимый набор для обоих классов A2G2m↔12–20m и
A2A12–20m нашей площадки. Полные коэффициенты из abstract не извлекались.
Online Buildings доступен28.10; propagation URL в этой проверке не загрузился,
вывод о реализации основан на доступном pinned source.

## Геометрия и datum

`network/config/urban_two_building.json` — идеализированная новая площадка,
не Town01 и не реальный промышленный объект. Корпуса A[40..70,20..65,0..30] и
B[95..125,20..65,0..25]. Один JSON создаёт Gazebo visual/collision и ns-3 объёмы.
Плановые маршруты предварительно проверяются на пересечение3m safety envelope;
это не доказательство отсутствия фактических столкновений при полёте.

World origin ENU, ground surfaceZ=0. Model origin referenceZ=.195m получен из
статической геометрии iris base collision: centreZ=-.08, box height=.23.
Контактный residual/оценка EKF не подгоняются по Gazebo-позам.
ENU→LOCAL_NED следует явному gazeboXYZToNED в upstream-модели;
фактические оси и datum residual требуют отдельного пилота.
Spherical/home heading10deg сохраняет существующую конфигурацию среды.

## Воспроизведение подготовки (не измерение полёта)

```bash
python3 scripts/product/prepare_urban_gazebo.py --config network/config/urban_two_building.json
python3 scripts/product/prepare_urban_gazebo.py --config network/config/urban_axes_pilot.json
```

Выход `.external/urban_ns3/` заменяется только между запусками под runtime lock;
в каждом raw сохранить config/world/YAML snapshot. Pilot axes radio.mode=los_only:
физические корпуса остаются, их влияние на выбор radio loss объявленно выключено.
